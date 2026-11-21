"""Preflight chỉ đọc cho workflow duyệt ghi issue thử nghiệm."""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from github_bot.execution.issue_executor import ApplyRejected, load_plan
from github_bot.github.client import GitHubClient, GitHubError
from github_bot.identities.resolver import GITHUB_LOGIN
from github_bot.markdown.loader import LoaderError, read_workspace_file
from github_bot.planning.planner import SCHEMA

TARGET_REPO = "AIVIETNAM-AIO-luciusHo/realcheck"
ENVIRONMENT = "realcheck-issue-write"
MAIN_REF = "refs/heads/main"
MANIFEST_SCHEMA = "issue-apply-manifest-v1"


@dataclass(frozen=True)
class RunContext:
    source_repo: str
    source_sha: str
    event_sha: str
    ref: str
    event_name: str
    run_id: int
    run_attempt: int
    actor: str
    triggering_actor: str


def _run_repo(context: RunContext) -> str:
    if (
        context.event_name != "workflow_dispatch" or context.ref != MAIN_REF
        or type(context.run_id) is not int or context.run_id <= 0
        or type(context.run_attempt) is not int or context.run_attempt != 1
        or not re.fullmatch(r"[0-9a-f]{40}", context.source_sha)
        or context.source_sha != context.event_sha
        or not GITHUB_LOGIN.fullmatch(context.actor)
        or not GITHUB_LOGIN.fullmatch(context.triggering_actor)
    ):
        raise ApplyRejected("Run cần workflow_dispatch trên main, đúng SHA và lần chạy đầu tiên.")
    owner, _, repo = context.source_repo.partition("/")
    try:
        return GitHubClient._repo_path(owner, repo)
    except GitHubError:
        raise ApplyRejected("Repo nguồn của run không hợp lệ.") from None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("JSON có trường lặp.")
    return values


def _read_object(workspace: Path, path: str) -> dict[str, object]:
    try:
        if (workspace / path).is_symlink():
            raise ValueError("Đường dẫn JSON là symlink.")
        text, _ = read_workspace_file(path, workspace, (".json",))
        value = json.loads(text, object_pairs_hook=_unique_keys)
    except (LoaderError, ValueError, OSError, RuntimeError):
        raise ApplyRejected("Không đọc được tệp JSON hợp lệ trong workspace.") from None
    if not isinstance(value, dict):
        raise ApplyRejected("Tệp JSON phải là object.")
    return value


def build_manifest(workspace: Path, plan_path: str, context: RunContext) -> dict[str, object]:
    _run_repo(context)
    document = _read_object(workspace, plan_path)
    digest = document.get("plan_sha256")
    if not isinstance(digest, str):
        raise ApplyRejected("Plan thiếu SHA-256.")
    plan = load_plan(plan_path, workspace, digest)
    if (
        plan.schema_version != SCHEMA or plan.repo.casefold() != TARGET_REPO.casefold()
        or plan.source_file != "tasks.md" or len(plan.items) != 1
        or plan.items[0].action not in ("CREATE", "SKIP")
    ):
        raise ApplyRejected("Plan phải dùng schema plan-v1, target realcheck và đúng một task từ tasks.md.")
    return {
        "schema": MANIFEST_SCHEMA,
        "source_repo": context.source_repo,
        "source_sha": context.source_sha,
        "target_repo": plan.repo,
        "source_path": plan.source_file,
        "run_id": context.run_id,
        "run_attempt": context.run_attempt,
        "plan_sha256": digest,
        "actions": [{"task_id": item.task_id, "action": item.action} for item in plan.items],
    }


def _reviewer(payload: object, context: RunContext) -> str:
    if not isinstance(payload, list):
        raise ApplyRejected("Dữ liệu approval không hợp lệ.")
    candidates: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise ApplyRejected("Dữ liệu approval không hợp lệ.")
        environments, state, user = entry.get("environments"), entry.get("state"), entry.get("user")
        if (
            not isinstance(environments, list) or not environments
            or not all(isinstance(env, dict) and isinstance(env.get("name"), str) for env in environments)
            or state not in ("approved", "rejected")
            or not isinstance(user, dict) or not isinstance(user.get("login"), str)
            or not GITHUB_LOGIN.fullmatch(user["login"])
        ):
            raise ApplyRejected("Dữ liệu approval không hợp lệ.")
        if not any(env["name"] == ENVIRONMENT for env in environments):
            continue
        if state != "approved" or sum(env["name"] == ENVIRONMENT for env in environments) != 1:
            raise ApplyRejected("Approval cho environment bị từ chối hoặc mâu thuẫn.")
        candidates.append(user["login"])
    forbidden = {context.actor.casefold(), context.triggering_actor.casefold()}
    if len(candidates) != 1 or candidates[0].casefold() in forbidden:
        raise ApplyRejected("Cần đúng một reviewer độc lập đã duyệt environment.")
    return candidates[0]


def _verify(
    workspace: Path,
    plan_path: str,
    manifest_path: str,
    context: RunContext,
    *,
    actions_token: str,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, dict[str, object]]:
    source = _run_repo(context)
    manifest = _read_object(workspace, manifest_path)
    expected = build_manifest(workspace, plan_path, context)
    if json.dumps(manifest, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ApplyRejected("Manifest không khớp run, SHA, repo hoặc kế hoạch đã duyệt.")
    try:
        client = GitHubClient(token=actions_token, transport=transport)
        try:
            ref = client._get(f"{source}/git/ref/heads/main")
            if ref.status_code != 200:
                raise ApplyRejected("Không xác minh được ref main.")
            current = ref.json()
            obj = current.get("object") if isinstance(current, dict) else None
            if (
                not isinstance(obj, dict) or current.get("ref") != MAIN_REF
                or obj.get("sha") != context.source_sha or obj.get("type", "commit") != "commit"
            ):
                raise ApplyRejected("SHA main đã thay đổi hoặc không xác minh được.")
            approvals = client._get(f"{source}/actions/runs/{context.run_id}/approvals")
            if approvals.status_code != 200 or "next" in approvals.links:
                raise ApplyRejected("Không xác minh được toàn bộ lịch sử approval.")
            return _reviewer(approvals.json(), context), expected
        finally:
            client.close()
    except (GitHubError, ValueError):
        raise ApplyRejected("Không đọc được ref main hoặc lịch sử approval.") from None


def verify_approval(
    workspace: Path,
    plan_path: str,
    manifest_path: str,
    context: RunContext,
    *,
    actions_token: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    reviewer, _ = _verify(
        workspace, plan_path, manifest_path, context, actions_token=actions_token, transport=transport,
    )
    return reviewer


def _context(checkout_sha: str) -> RunContext:
    return RunContext(
        source_repo=os.environ["GITHUB_REPOSITORY"],
        source_sha=checkout_sha,
        event_sha=os.environ["GITHUB_SHA"],
        ref=os.environ["GITHUB_REF"],
        event_name=os.environ["GITHUB_EVENT_NAME"],
        run_id=int(os.environ["GITHUB_RUN_ID"]),
        run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
        actor=os.environ["GITHUB_ACTOR"],
        triggering_actor=os.environ["GITHUB_TRIGGERING_ACTOR"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Xác minh provenance và approval của workflow.")
    parser.add_argument("command", choices=("manifest", "verify"))
    parser.add_argument("--checkout-sha", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    try:
        context = _context(args.checkout_sha)
        if args.command == "manifest":
            manifest = build_manifest(args.workspace, "plan.json", context)
            (args.workspace / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            print("## Plan issue Realcheck chờ duyệt\n")
            print(f"- Repo đích: `{manifest['target_repo']}`")
            print(f"- Nguồn: `{manifest['source_path']}` tại `{manifest['source_sha']}`")
            print(f"- Run: `{context.run_id}`, lần `{context.run_attempt}`")
            print(f"- SHA-256: `{manifest['plan_sha256']}`")
            print(f"- Hành động: `{json.dumps(manifest['actions'], ensure_ascii=False)}`")
            print("- Đối chiếu nội dung artifact trước khi duyệt environment.")
        else:
            reviewer, provenance = _verify(
                args.workspace, "plan.json", "manifest.json", context,
                actions_token=os.environ.get("ACTIONS_READ_TOKEN", ""),
            )
            provenance.update(reviewer=reviewer, actor=context.actor, triggering_actor=context.triggering_actor)
            (args.workspace / "apply-provenance.json").write_text(
                json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            print(provenance["plan_sha256"])
    except ApplyRejected as error:
        print(f"TỪ CHỐI: {error}", file=sys.stderr)
        return 2
    except (KeyError, OSError, ValueError, RuntimeError):
        print("TỪ CHỐI: Không xác minh được dữ liệu workflow.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
