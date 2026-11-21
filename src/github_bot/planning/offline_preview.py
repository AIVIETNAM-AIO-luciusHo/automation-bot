"""Tạo artifact tham khảo từ kết quả xác thực cục bộ."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from string import punctuation
from tempfile import TemporaryDirectory

from github_bot import __version__
from github_bot.github.client import GitHubClient
from github_bot.identities.resolver import Resolved
from github_bot.models import Task
from github_bot.planning.planner import PlanError, canonical_plan_hash, preview_output_dir

_MARKUP = str.maketrans({ch: f"&#{ord(ch)};" for ch in punctuation if ch != "-"})
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")


def validate_preview_target(repo: str, allowed: list[str], sha: str) -> None:
    for slug in [repo, *allowed]:
        owner, _, name = slug.partition("/")
        GitHubClient._repo_path(owner, name)
    if repo.casefold() not in {slug.casefold() for slug in allowed}:
        raise PlanError("Repo đích nằm ngoài danh sách cho phép.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise PlanError("SHA nguồn phải có đúng 40 ký tự hex.")


def _literal(value: str) -> str:
    return _CONTROLS.sub(" ", value).translate(_MARKUP)


def write_offline_preview(
    *, tasks: list[Task], resolved: list[Resolved], workspace: Path, output_dir: str,
    source: str, repo: str, sha: str,
) -> str:
    entries = [
        {"task_id": task.id, "title": task.title, "assignees": identity.assignees, "labels": task.labels}
        for task, identity in zip(tasks, resolved, strict=True)
    ]
    provenance = {
        "source": source, "source_sha": sha.lower(), "target_repo": repo, "tool_version": __version__,
    }
    plan: dict[str, object] = {
        "schema": "offline-preview-v1", "remote_verified": False, "tasks": entries, **provenance,
    }
    digest = canonical_plan_hash(plan)
    plan["plan_sha256"] = digest
    metadata = {**provenance, "plan_sha256": digest, "created_at": datetime.now(UTC).isoformat()}
    markdown = [
        "# Preview task offline",
        "",
        "Chưa đối chiếu quyền gán người phụ trách, nhãn hoặc issue hiện có trên GitHub.",
        "",
    ]
    for task, identity in zip(tasks, resolved, strict=True):
        assignees = ", ".join(map(_literal, identity.assignees)) or "chưa có"
        labels = ", ".join(map(_literal, task.labels)) or "chưa có"
        markdown.append(f"- **{_literal(task.id)}** · {_literal(task.title)}")
        markdown.append(f"  - Phụ trách: {assignees}")
        markdown.append(f"  - Nhãn: {labels}")

    artifacts = {
        "plan.json": json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        "PREVIEW.md": "\n".join(markdown) + "\n",
        "metadata.json": json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    }
    target = preview_output_dir(output_dir, workspace, no_symlinks=True)
    for name in artifacts:
        path = target / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise PlanError("Đường dẫn artifact là symlink hoặc không phải tệp thường.")
    target.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target) as staging:
        for name, content in artifacts.items():
            (Path(staging) / name).write_text(content, encoding="utf-8")
        for name in artifacts:
            (Path(staging) / name).replace(target / name)
    return digest
