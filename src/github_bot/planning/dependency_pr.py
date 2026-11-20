"""Lập preview PR từ Git index bằng các thao tác chỉ đọc."""

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from github_bot.github.client import GitHubClient
from github_bot.planning.planner import PlanError

ALLOWED_FILES = {"pyproject.toml", "uv.lock"}
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ud800-\udfff]")


def _git_result(
    root: Path, *args: str, data: bytes | None = None,
    index: Path | None = None, allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot") if key in os.environ}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_ATTR_NOSYSTEM="1",
        GIT_ALLOW_PROTOCOL="",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
    )
    command = [
        "git", "--no-optional-locks", "-c", "core.fsmonitor=false",
        "-c", f"core.hooksPath={os.devnull}",
    ]
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
        command.extend(["-c", "core.splitIndex=false", "-c", "core.sparseCheckout=false"])
    try:
        result = subprocess.run(
            [*command, *args], cwd=root, env=env, input=data if data is not None else b"",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise PlanError("Không thể thực hiện thao tác Git cục bộ.") from None
    if result.returncode not in allowed:
        raise PlanError(f"Git cục bộ từ chối thao tác (mã {result.returncode}).")
    return result


def _git(
    root: Path, *args: str, data: bytes | None = None, index: Path | None = None,
) -> bytes:
    return _git_result(root, *args, data=data, index=index).stdout


def dependency_plan_hash(plan: dict[str, object]) -> str:
    canonical = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _worktree_matches(path: Path, mode: str, oid: str) -> bool:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        info = os.fstat(file.fileno())
        if not os.path.samestat(before, info):
            return False
        actual_mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
        if actual_mode != mode:
            return False
        algorithm = "sha1" if len(oid) == 40 else "sha256"
        digest = hashlib.new(algorithm, f"blob {info.st_size}\0".encode("ascii"))
        hashlib.file_digest(file, lambda: digest)
        return digest.hexdigest() == oid


def dependency_pr_plan(
    workspace: Path, repo: str, branch: str, title: str, files: list[str],
) -> dict[str, object]:
    owner, _, name = repo.partition("/")
    GitHubClient._repo_path(owner, name)
    if not branch or branch.startswith("-") or branch == "@" or "@{" in branch or _UNSAFE.search(branch):
        raise PlanError("Tên nhánh không hợp lệ.")
    if not title.strip() or len(title) > 256 or _UNSAFE.search(title):
        raise PlanError("Tiêu đề phải có nội dung, tối đa 256 ký tự và không có ký tự điều khiển.")
    selected = set(files)
    if not selected or len(selected) != len(files) or not selected <= ALLOWED_FILES:
        raise PlanError("Chỉ chọn pyproject.toml và uv.lock, mỗi tệp tối đa một lần.")

    try:
        root = workspace.resolve(strict=True)
        git_root = _git(root, "rev-parse", "--show-toplevel").removesuffix(b"\n")
        if Path(os.fsdecode(git_root)).resolve() != root:
            raise PlanError("Workspace phải là thư mục gốc của Git worktree.")
        _git(root, "check-ref-format", "--branch", branch)
        base = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        if not _OBJECT_ID.fullmatch(base):
            raise PlanError("Không xác định được SHA gốc.")
        diff_args = (
            "diff", "--cached", "--name-status", "--no-ext-diff", "--no-textconv",
            "--no-renames", f"-O{os.devnull}", "-z", base, "--",
        )
        staged = _git(root, *diff_args)
        expected = {b"M\0" + file.encode("ascii") for file in selected}
        fields = staged.split(b"\0")[:-1]
        if len(fields) != 2 * len(selected) or {
            b"\0".join(fields[i:i + 2]) for i in range(0, len(fields), 2)
        } != expected:
            raise PlanError("Index phải chỉ chứa thay đổi sửa tệp đã chọn, không có tệp staged khác.")

        index_args = ("ls-files", "--stage", "-z", "--", *sorted(selected))
        index = _git(root, *index_args)
        entries = []
        for row in index.decode("ascii").split("\0")[:-1]:
            metadata, path = row.split("\t")
            mode, oid, stage = metadata.split()
            if (
                path not in selected or mode not in {"100644", "100755"} or stage != "0"
                or not _OBJECT_ID.fullmatch(oid) or not _worktree_matches(root / path, mode, oid)
            ):
                raise PlanError("Tệp đã chọn phải là file thường và khớp nội dung staged.")
            entries.append({"path": path, "mode": mode, "blob_oid": oid})
        if {entry["path"] for entry in entries} != selected or len(entries) != len(selected):
            raise PlanError("Index thiếu mục đã chọn hoặc có xung đột.")
        if (
            _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip() != base
            or _git(root, *diff_args) != staged or _git(root, *index_args) != index
        ):
            raise PlanError("HEAD hoặc index đã đổi trong lúc lập preview.")
    except (OSError, ValueError, RuntimeError):
        raise PlanError("Không kiểm tra được workspace hoặc dữ liệu Git cục bộ.") from None

    plan: dict[str, object] = {
        "repo": repo, "base_sha": base, "branch": branch, "title": title, "files": entries,
    }
    plan["plan_sha256"] = dependency_plan_hash(plan)
    return plan
