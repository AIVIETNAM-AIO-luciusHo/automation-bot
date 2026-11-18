"""Executor GHI duy nhất của bot: chỉ tạo issue (POST /repos/{o}/{r}/issues) từ plan đã duyệt bằng hash.

Mọi cổng kiểm tra chạy TRƯỚC khi có POST đầu tiên; từ chối = 0 POST. Không PUT/PATCH/DELETE, không đổi
state, không comment, không theo redirect (kế thừa từ client đọc)."""

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from github_bot.github.client import GitHubClient, GitHubError, IssueRef, _logins
from github_bot.github.validator import validate_against_github
from github_bot.identities.resolver import GITHUB_LOGIN, Resolved
from github_bot.markdown.loader import LoaderError, read_workspace_file
from github_bot.models import Task
from github_bot.planning.planner import Plan, marker, plan_hash

MAX_ITEMS = 50
MAX_ATTEMPTS = 3  # số lần thử POST mỗi task khi bị rate-limit
MAX_SLEEP = 60.0
_HASH = re.compile(r"[0-9a-f]{64}\Z")
# C0/C1 + bidi/zero-width (cùng bộ với cli._CTRL)
_CTRL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]")
_TASK_ID = re.compile(r"[A-Za-z]+-?[0-9]+")  # cùng dạng với models.Task.id


class ApplyRejected(Exception):
    """Plan/điều kiện bị từ chối trước khi POST: 0 issue được tạo."""


class IssueWriter(GitHubClient):
    """Client ghi: kế thừa cấu hình an toàn của client đọc, thêm đúng MỘT thao tác tạo issue."""

    def create_issue(self, owner: str, repo: str, payload: dict[str, object]) -> dict[str, Any]:
        r = self._request("POST", f"{self._repo_path(owner, repo)}/issues", None, payload, (201,))
        try:
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data["number"], int) or not isinstance(data["html_url"], str):
                raise TypeError
            logins = _logins(data.get("assignees"))
        except (ValueError, TypeError, KeyError):
            raise GitHubError("BAD_RESPONSE", "Phản hồi tạo issue không hợp lệ; kiểm tra thủ công.", 201) from None
        return {"number": data["number"], "url": data["html_url"], "assignees": logins}


def load_plan(path: str, workspace: str | Path, approve_hash: str) -> Plan:
    """Đọc + kiểm tra plan. Luôn tính lại hash từ nội dung; không tin plan_sha256 trong file."""
    if not _HASH.match(approve_hash):
        raise ApplyRejected("--approve-hash phải là 64 ký tự hex thường (SHA-256 của plan).")
    lexical = Path(workspace).resolve() / path
    if lexical.is_symlink():
        raise ApplyRejected("Đường dẫn plan là symlink, bị chặn.")
    try:
        text, _ = read_workspace_file(path, workspace, (".json",))
        plan = Plan.model_validate(json.loads(text))
    except LoaderError as e:
        raise ApplyRejected(str(e)) from None
    except (ValueError, ValidationError):
        raise ApplyRejected("plan.json hỏng hoặc sai hình dạng.") from None
    if plan_hash(plan) != approve_hash or plan.plan_sha256 != approve_hash:
        raise ApplyRejected("Hash không khớp nội dung plan (plan bị sửa hoặc hash sai); hãy chạy lại `issues plan`.")
    if not plan.items or len(plan.items) > MAX_ITEMS:
        raise ApplyRejected(f"Plan phải có 1..{MAX_ITEMS} item.")
    if not all(len(i.task_id) <= 64 and _TASK_ID.fullmatch(i.task_id) for i in plan.items):
        raise ApplyRejected("Plan có task_id sai định dạng.")
    if len({i.task_id.upper() for i in plan.items}) != len(plan.items):
        raise ApplyRejected("Plan có task_id trùng.")
    for i in plan.items:
        if i.action == "BLOCK":
            raise ApplyRejected(f"Plan có task BLOCK ({i.task_id}); all-or-nothing, hãy sửa rồi lập plan lại.")
        if i.action != "CREATE":
            continue
        last = i.body.rstrip().splitlines()[-1].strip() if i.body.strip() else ""
        if (
            not i.title.strip() or len(i.title) > 256 or _CTRL.search(i.title)
            or last != marker(plan.source_file, i.task_id) or i.body.count("<!-- github-automation-bot:") != 1
            or not all(GITHUB_LOGIN.match(a) for a in i.assignees)
            or not all(0 < len(x) <= 50 and not _CTRL.search(x) for x in i.labels)
        ):  # fmt: skip
            raise ApplyRejected(f"Item {i.task_id[:30]!r} không hợp lệ (title/body/marker/assignee/label).")
    return plan


def _revalidate(client: IssueWriter, plan: Plan) -> list[IssueRef]:
    """Plan cũ? Kiểm lại repo/quyền/assignee/label (validator M2b) và marker đã có. Trả danh sách issue."""
    create = [i for i in plan.items if i.action == "CREATE"]
    try:
        tasks = [Task(id=i.task_id, title=i.title, labels=i.labels, source_file=plan.source_file, source_reference=i.task_id) for i in create]
    except ValidationError:
        raise ApplyRejected("Plan có item không hợp lệ (id/title/label).") from None
    resolved = [Resolved(task_id=i.task_id, assignees=i.assignees) for i in create]
    diags = validate_against_github(client, plan.repo, tasks, resolved)
    if diags:
        raise ApplyRejected("Plan cũ/không còn hợp lệ: " + "; ".join(f"[{d.code}] {d.message}" for d in diags[:5]))
    owner, _, name = plan.repo.partition("/")
    try:
        issues = client.list_issues(owner, name)
    except GitHubError as e:
        raise ApplyRejected(f"Không đọc được issue hiện có ({e.code}); không tạo gì.") from None
    for i in create:
        if _existing(issues, plan.source_file, i.task_id):
            raise ApplyRejected(f"Plan cũ: {i.task_id} đã có issue cùng khóa. Hãy chạy lại `issues plan`.")
    return issues


def _existing(issues: list[IssueRef], source: str, task_id: str) -> IssueRef | None:
    needle = marker(source, task_id).casefold()
    return next((i for i in issues if needle in i.body.casefold()), None)


def apply_plan(
    plan_path: str, approve_hash: str, write: bool, repo: str | None, workspace: str | Path,
    transport: Any = None,
) -> tuple[int, list[str]]:
    """Trả (exit code, dòng báo cáo). ApplyRejected nếu bị từ chối trước khi POST."""
    if not write:
        raise ApplyRejected("Ghi GitHub bị tắt: cần cờ --write tường minh (mặc định từ chối).")
    plan = load_plan(plan_path, workspace, approve_hash)
    if repo is not None and repo.casefold() != plan.repo.casefold():
        raise ApplyRejected("--repo khác repo trong plan.")
    try:
        client = IssueWriter(transport=transport)
    except GitHubError as e:
        raise ApplyRejected(e.message) from None
    owner, _, name = plan.repo.partition("/")
    audit = (Path(workspace).resolve() / plan_path).resolve().parent / f"audit-{approve_hash[:8]}.jsonl"
    _preflight_audit(audit)  # trước POST đầu tiên: không thể để issue tạo xong mà không ghi được audit
    lines: list[str] = []
    failed = 0
    try:
        _revalidate(client, plan)
        try:
            log = audit.open("a", encoding="utf-8")
        except OSError:
            raise ApplyRejected("Không mở được file audit để ghi.") from None
        with log:
            stop = ""
            for it in plan.items:
                base: dict[str, object] = {"timestamp": datetime.now(UTC).isoformat(timespec="seconds"), "task_id": it.task_id}
                if stop:
                    row: dict[str, object] = {"action": "not_attempted", "error": stop}
                elif it.action != "CREATE":
                    row = {"action": "skip_existing", "issue_number": it.issue_number}
                else:
                    try:
                        row = _create_one(client, plan, it, owner, name)
                    except KeyboardInterrupt:
                        row, stop = {"action": "error", "error": "INTERRUPTED: bị ngắt giữa chừng, kết quả task này không rõ; chạy lại `issues plan`"}, "bị ngắt (Ctrl-C)"
                    except Exception as e:  # noqa: BLE001 - bug/transport lạ: dừng sạch, vẫn báo và ghi audit những gì đã làm
                        row, stop = {"action": "error", "error": f"UNEXPECTED: {type(e).__name__}; kết quả task này không rõ, chạy lại `issues plan`"}, "dừng sau lỗi bất ngờ"
                row = {**base, **row}
                log.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                log.flush()  # mỗi task một dòng, ngay lập tức
                ok = row["action"] in ("created", "created_recovered", "skip_existing") and "error" not in row
                failed += not ok
                lines.append(_line(row))
    finally:
        client.close()
    return (1 if failed else 0), lines


def _preflight_audit(path: Path) -> None:
    if path.is_symlink() or path.is_dir():
        raise ApplyRejected("File audit là symlink/thư mục, từ chối (chưa tạo issue nào).")
    target = path if path.exists() else path.parent
    if not os.access(target, os.W_OK):
        raise ApplyRejected("Không ghi được file audit, từ chối (chưa tạo issue nào).")


def _line(row: dict[str, object]) -> str:
    tid, act = row["task_id"], row["action"]
    if act == "skip_existing":
        return f"SKIPPED {tid} (đã có issue #{row.get('issue_number')})"
    if row.get("error"):
        return f"ERROR {tid} [{act}] {row['error']}" + (f" (HTTP {row['status']})" if row.get("status") else "")
    return f"CREATED {tid} #{row['issue_number']} {row['url']}" if act != "created_recovered" else f"CREATED {tid} #{row['issue_number']} (khôi phục sau lỗi mạng) {row['url']}"


def _create_one(client: IssueWriter, plan: Plan, it: Any, owner: str, name: str) -> dict[str, object]:
    payload: dict[str, object] = {"title": it.title, "body": it.body, "labels": it.labels, "assignees": it.assignees}
    for attempt in range(MAX_ATTEMPTS):
        try:
            seen = _existing(client.list_issues(owner, name), plan.source_file, it.task_id)  # đọc lại ngay trước POST
        except GitHubError as e:
            return {"action": "error", "error": f"không đọc lại được issue ({e.code}); không POST", "status": e.status}
        if seen:
            return {"action": "skipped_duplicate", "issue_number": seen.number, "url": seen.url,
                    "error": f"{it.task_id} đã có issue #{seen.number} (xuất hiện trong lúc chạy); không tạo trùng"}  # fmt: skip
        try:
            got = client.create_issue(owner, name, payload)
        except GitHubError as e:
            if e.code == "RATE_LIMITED" and attempt < MAX_ATTEMPTS - 1 and e.retry_after is not None and e.retry_after <= MAX_SLEEP:
                time.sleep(e.retry_after)  # chờ theo Retry-After rồi đọc lại trạng thái (vòng lặp) trước khi POST lại
                continue
            if e.code in ("NETWORK", "SERVER_ERROR", "BAD_RESPONSE"):  # kết quả không rõ: đọc lại, không POST mù
                try:
                    found = _existing(client.list_issues(owner, name), plan.source_file, it.task_id)
                except GitHubError:
                    found = None
                if found:
                    rec: dict[str, object] = {"action": "created_recovered", "issue_number": found.number, "url": found.url, "assignees": found.assignees}
                    if (bad := _assignee_error(it, found.assignees)):
                        rec["error"] = bad
                    return rec
                if e.code in ("NETWORK", "BAD_RESPONSE"):
                    return {"action": "error", "status": e.status, "error": f"{e.code}: kết quả không rõ, issue có thể đã được tạo; chạy lại `issues plan` trước khi làm gì tiếp"}
            return {"action": "error", "error": f"{e.code}: {e.message}", "status": e.status}
        row: dict[str, object] = {"action": "created", "issue_number": got["number"], "url": got["url"], "assignees": got["assignees"]}
        if (bad := _assignee_error(it, got["assignees"])):  # báo, không xóa/sửa issue
            row["error"] = bad
        return row
    return {"action": "error", "error": "RATE_LIMITED: hết số lần thử", "status": 429}


def _assignee_error(it: Any, actual: list[str] | None) -> str | None:
    """Assignee thực tế phải đúng bằng yêu cầu: thiếu (GitHub bỏ im lặng), thừa hoặc không xác minh được -> lỗi."""
    if actual is None:
        return f"ASSIGNEE_UNVERIFIED: không xác minh được assignee của {it.task_id}"
    want, got = {a.casefold(): a for a in it.assignees}, {a.casefold(): a for a in actual}
    lost, extra = [want[k] for k in want if k not in got], [got[k] for k in got if k not in want]
    if lost or extra:
        return f"ASSIGNEE_MISMATCH: {it.task_id} thiếu {lost} thừa {extra}"
    return None
