"""Áp dụng kế hoạch phụ thuộc đã duyệt vào một nhánh Git cục bộ."""

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

from github_bot.markdown.loader import LoaderError, read_workspace_file
from github_bot.planning.dependency_pr import (
    _git,
    _git_result,
    dependency_plan_hash,
    dependency_pr_plan,
)
from github_bot.planning.planner import PlanError

_HASH = re.compile(r"[0-9a-f]{64}")
_PlanType = TypeVar("_PlanType", bound=BaseModel)


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    mode: str
    blob_oid: str


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repo: str
    base_sha: str
    branch: str
    title: str
    files: list[_File]
    plan_sha256: str


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("JSON có khóa lặp.")
    return values


def _load_hashed_plan(
    workspace: Path, path: str, model: type[_PlanType], approved: str | None,
) -> _PlanType:
    if approved is not None and not _HASH.fullmatch(approved):
        raise PlanError("--approve-hash phải là SHA-256 dạng 64 ký tự hex thường.")
    try:
        if (workspace / path).is_symlink():
            raise PlanError("Plan không được là symlink.")
        text, _ = read_workspace_file(path, workspace, (".json",))
        plan = model.model_validate(json.loads(text, object_pairs_hook=_unique_keys))
        digest = dependency_plan_hash(plan.model_dump(exclude={"plan_sha256"}))
    except (LoaderError, OSError, ValueError, RuntimeError):
        raise PlanError("Không đọc được plan JSON hợp lệ trong workspace.") from None
    if (approved is not None and digest != approved) or plan.model_dump()["plan_sha256"] != digest:
        raise PlanError("Hash không khớp nội dung plan đã duyệt.")
    return plan


def _branch_tip(root: Path, ref: str) -> str | None:
    if f"branch {ref}".encode() in _git(root, "worktree", "list", "--porcelain", "-z").split(b"\0"):
        raise PlanError("Nhánh đích đang được checkout trong một worktree.")
    if _git_result(root, "symbolic-ref", "-q", "--no-recurse", ref, allowed=(0, 1)).returncode == 0:
        raise PlanError("Nhánh đích là symbolic ref.")
    if _git_result(root, "show-ref", "--exists", ref, allowed=(0, 2)).returncode == 2:
        return None
    return _git(root, "show-ref", "--hash", "--verify", ref).decode("ascii").strip()


def _expected_tree(root: Path, plan: _Plan) -> str:
    records = "".join(f"{f.mode} {f.blob_oid}\t{f.path}\0" for f in plan.files).encode("ascii")
    with TemporaryDirectory(prefix="github-bot-index-") as tmp:
        index = Path(tmp) / "index"
        _git(root, "read-tree", plan.base_sha, index=index)
        _git(root, "update-index", "-z", "--index-info", data=records, index=index)
        return _git(root, "write-tree", index=index).decode("ascii").strip()


def dependency_pr_apply(workspace: Path, path: str, approved: str, write: bool) -> dict[str, str]:
    if not write:
        raise PlanError("Ghi nhánh cục bộ cần cờ --write.")
    plan = _load_hashed_plan(workspace, path, _Plan, approved)

    def verify_plan() -> None:
        current = dependency_pr_plan(
            workspace, plan.repo, plan.branch, plan.title, [entry.path for entry in plan.files],
        )
        if current != plan.model_dump():
            raise PlanError("HEAD, index hoặc nội dung không còn khớp plan đã duyệt.")

    verify_plan()
    root = workspace.resolve()
    ref = f"refs/heads/{plan.branch}"
    tip = _branch_tip(root, ref)
    message = f"{plan.title}\n\nDependency-Plan-SHA256: {approved}\n".encode()
    if tip is not None:
        header, _, body = _git(root, "cat-file", "commit", tip).partition(b"\n\n")
        parents = [line for line in header.split(b"\n") if line.startswith(b"parent ")]
        if body != message or parents != [f"parent {plan.base_sha}".encode("ascii")]:
            raise PlanError("Nhánh đích đã tồn tại và không thuộc plan đã duyệt.")

    try:
        tree = _expected_tree(root, plan)
    except OSError:
        raise PlanError("Không thể tạo hoặc dọn index tạm.") from None
    if tip is None:
        commit = _git(
            root, "-c", "i18n.commitEncoding=UTF-8", "commit-tree", "--no-gpg-sign",
            tree, "-p", plan.base_sha, "-F", "-", data=message,
        ).decode("ascii").strip()
        change = f"create {ref} {commit}"
        status = "created"
    else:
        if not header.startswith(f"tree {tree}\n".encode("ascii")):
            raise PlanError("Commit trên nhánh đích không khớp cây đã duyệt.")
        commit = tip
        change = f"verify {ref} {commit}"
        status = "existing"

    verify_plan()
    if _branch_tip(root, ref) != tip:
        raise PlanError("Nhánh đích đã thay đổi trong lúc áp dụng.")
    transaction = f"start\nverify HEAD {plan.base_sha}\noption no-deref\n{change}\nprepare\ncommit\n"
    _git(root, "update-ref", "--stdin", data=transaction.encode())
    return {"status": status, "branch": plan.branch, "commit_sha": commit}
