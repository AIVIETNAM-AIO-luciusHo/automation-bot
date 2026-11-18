"""Planner CHỈ ĐỌC: dựng issue body + marker, so với issue hiện có, ra plan CREATE/SKIP/BLOCK có hash."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel

from github_bot.github.client import IssueRef
from github_bot.identities.resolver import Resolved
from github_bot.models import Diagnostic, Task

Action = Literal["CREATE", "SKIP", "BLOCK"]  # UPDATE thuộc M4
SCHEMA = "plan-v1"
_MARKER = re.compile(r"<!-- github-automation-bot: source=([^;<>\s]*); task_id=([A-Za-z0-9-]+); schema=v1 -->")
_CTRL = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]")
PREVIEW_FILES = ("PREVIEW.md", "PREVIEW.json", "plan.json")


class PlanError(Exception):
    pass


class PlanItem(BaseModel):
    task_id: str
    action: Action
    title: str
    body: str
    assignees: list[str]
    labels: list[str]
    issue_number: int | None = None  # SKIP: issue đã có
    reasons: list[str] = []  # BLOCK/SKIP: mã (+ lý do xác định, không chứa dữ liệu đổi theo lần chạy)


class Plan(BaseModel):
    schema_version: str = SCHEMA
    repo: str
    source_file: str
    items: list[PlanItem]
    plan_sha256: str = ""


def marker(source_file: str, task_id: str) -> str:
    """Marker ổn định. source được percent-encode (không còn ; < > khoảng trắng) nên không phá comment."""
    return f"<!-- github-automation-bot: source={quote(source_file, safe='/._-')}; task_id={task_id}; schema=v1 -->"


# Tách ký hiệu khỏi phần còn lại bằng ranh giới inline-code để không còn @mention / #ref / URL / ảnh.
# shortcut: dựa vào việc bộ nhận diện mention/autolink làm việc trên chuỗi liền; docs GitHub không nói rõ,
# chưa kiểm chứng trên GitHub thật -> kiểm ở E2E sandbox trước khi tin hoàn toàn.
_NEUTRALIZE = (
    (re.compile(r"@(?=[A-Za-z0-9])"), "`@`"),
    (re.compile(r"#(?=[0-9])"), "`#`"),
    (re.compile(r"://"), "`://`"),
    (re.compile(r"!\["), "`!`["),
)


def _text(s: str) -> str:
    """Dữ liệu không đáng tin: bỏ ký tự điều khiển, '<' -> &lt; (không giả marker/HTML), vô hiệu hóa
    @mention, #N / owner/repo#N, URL (autolink) và ảnh ngoài. Chỉ mục Phân công do bot sinh mới có @login."""
    s = _CTRL.sub(" ", s).replace("<", "&lt;")
    for rx, repl in _NEUTRALIZE:
        s = rx.sub(repl, s)
    return s


def build_body(task: Task, assignees: list[str], collaborators: list[str]) -> str:
    ac = "\n".join(f"- [ ] {_text(a)}" for a in task.acceptance_criteria) or "_(chưa có)_"
    lines = [
        "## Mô tả", _text(task.description) or "_(chưa có)_", "",
        "## Tiêu chí nghiệm thu", ac, "",
        "## Phân công",
        f"- Phụ trách: {', '.join('@' + a for a in assignees) or '_(chưa có)_'}",
        f"- Phối hợp: {', '.join('@' + c for c in collaborators) or '_(chưa có)_'}",
        f"- Ưu tiên: {task.priority.capitalize() if task.priority else '_(chưa có)_'}", "",
        "## Nguồn công việc",
        f"- File: `{task.source_file.replace('`', chr(39))}`", f"- Task ID: `{task.id}`", "",
        marker(task.source_file, task.id),
    ]  # fmt: skip
    return "\n".join(lines) + "\n"


def index_markers(issues: list[IssueRef]) -> dict[tuple[str, str], list[tuple[int, bool]]]:
    """(source đã encode, task_id) -> [(số issue, marker có phải DÒNG CUỐI)]. Mỗi lần xuất hiện là một dấu vết;
    marker của bot luôn ở dòng cuối, marker giữa body (do người khác viết/trích) bị đánh dấu False."""
    hits: dict[tuple[str, str], list[tuple[int, bool]]] = {}
    for i in issues:
        lines = i.body.rstrip().splitlines()
        for n, line in enumerate(lines):
            for src, tid in _MARKER.findall(line):
                last = n == len(lines) - 1 and _MARKER.fullmatch(line.strip()) is not None
                hits.setdefault((src, tid.upper()), []).append((i.number, last))
    return hits


def plan_hash(plan: Plan) -> str:
    content = plan.model_dump(exclude={"plan_sha256"})
    blob = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_plan(
    repo: str,
    source_file: str,
    tasks: list[Task],
    resolved: list[Resolved],
    gh_diags: list[Diagnostic],
    issues: list[IssueRef] | None,
    max_tasks: int = 50,
) -> Plan:
    """issues=None nghĩa là chưa đọc được trạng thái -> mọi task BLOCK (fail closed)."""
    if len(tasks) > max_tasks:
        raise PlanError(f"{len(tasks)} task vượt giới hạn max_tasks_per_run={max_tasks}.")
    res = {r.task_id: r for r in resolved}
    glob = [d for d in gh_diags if d.task_id is None]
    per_task: dict[str, list[Diagnostic]] = {}
    for d in gh_diags:
        if d.task_id:
            per_task.setdefault(d.task_id, []).append(d)
    hits = index_markers(issues) if issues is not None else {}
    src_key = quote(source_file, safe="/._-")
    items: list[PlanItem] = []
    for t in tasks:
        r = res[t.id]
        reasons = [f"{d.code}: {d.message}" for d in r.diagnostics if d.level == "error"]
        reasons += [f"{d.code}: {d.message}" for d in per_task.get(t.id, [])]
        reasons += [d.code for d in glob]  # chỉ mã: thông điệp mạng có thể đổi theo lần chạy
        if issues is None and not glob:
            reasons.append("ISSUES_UNKNOWN")
        found = hits.get((src_key, t.id.upper()), [])
        number = None
        action: Action = "CREATE"
        if reasons:
            action = "BLOCK"
        elif len(found) > 1:
            action, reasons = "BLOCK", [f"DUPLICATE_MARKER: marker trùng ở issue {sorted({n for n, _ in found})}"]
        elif found and not found[0][1]:
            action, reasons = "BLOCK", [f"FOREIGN_MARKER: marker nằm giữa body issue #{found[0][0]}, không phải của bot"]
        elif found:
            action, number, reasons = "SKIP", found[0][0], ["EXISTS: issue đã có (UPDATE thuộc M4)"]
        body = build_body(t, r.assignees, r.collaborators) if action != "BLOCK" else ""
        items.append(PlanItem(
            task_id=t.id, action=action, title=_text(t.title).strip(), body=body,
            assignees=r.assignees if action != "BLOCK" else [], labels=t.labels if action != "BLOCK" else [],
            issue_number=number, reasons=reasons,
        ))  # fmt: skip
    plan = Plan(repo=repo, source_file=source_file, items=items)
    plan.plan_sha256 = plan_hash(plan)
    return plan


def render_md(plan: Plan) -> str:
    n = {a: sum(i.action == a for i in plan.items) for a in ("CREATE", "SKIP", "BLOCK")}
    out = [
        f"Repository: {plan.repo}", f"Source: {plan.source_file}",
        f"Tasks: {len(plan.items)} | CREATE: {n['CREATE']} | UPDATE: 0 | SKIP: {n['SKIP']} | BLOCK: {n['BLOCK']}", "",
    ]  # fmt: skip
    for i in plan.items:
        out.append(f"{i.action} {i.task_id} | {i.title}")
        if i.action == "CREATE":
            out += [f"  Assignees: {', '.join(i.assignees) or '-'}", f"  Labels: {', '.join(i.labels) or '-'}"]
        if i.issue_number:
            out.append(f"  Issue: #{i.issue_number}")
        out += [f"  Lý do: {r}" for r in i.reasons]
        out.append("")
    out += [f"Plan SHA256: {plan.plan_sha256}", "Write actions: DISABLED until approval", ""]
    return "\n".join(out)


def write_preview(plan: Plan, out_dir: str | Path, workspace: str | Path) -> list[Path]:
    """Ghi PREVIEW.md, PREVIEW.json, plan.json vào out_dir (phải nằm trong workspace). Từ chối symlink/thư mục trùng tên."""
    root = Path(workspace).resolve()
    target = (root / out_dir).resolve()
    if not target.is_relative_to(root):
        raise PlanError("output-dir nằm ngoài workspace, bị chặn.")
    target.mkdir(parents=True, exist_ok=True)
    js = json.dumps(plan.model_dump(), sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    paths = []
    for name, text in zip(PREVIEW_FILES, (render_md(plan), js, js), strict=True):
        p = target / name
        if p.is_symlink() or p.is_dir():
            raise PlanError(f"{name} là symlink/thư mục, từ chối ghi đè.")
        p.write_text(text, encoding="utf-8")
        paths.append(p)
    return paths
