"""Duyệt và xuất bản draft PR từ commit phụ thuộc đã kiểm chứng."""

import os
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from github_bot.execution.dependency_pr import _load_hashed_plan, _Plan
from github_bot.execution.issue_executor import IssueWriter
from github_bot.github.client import GitHubClient, GitHubError
from github_bot.planning.dependency_pr import (
    _OBJECT_ID,
    _UNSAFE,
    ALLOWED_FILES,
    _git,
    _git_result,
    dependency_plan_hash,
)
from github_bot.planning.planner import PlanError


class _PublishPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repo: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    title: str
    source_plan: _Plan
    plan_sha256: str


class _Repo(BaseModel):
    full_name: str


class _Side(BaseModel):
    ref: str
    sha: str
    repo: _Repo


class _Pull(BaseModel):
    model_config = ConfigDict(strict=True)

    number: int = Field(gt=0)
    html_url: str
    state: str
    draft: bool
    title: str
    base: _Side
    head: _Side


def _ref(root: Path, name: str) -> str:
    if not name or name.startswith("-") or name == "@" or "@{" in name or _UNSAFE.search(name):
        raise PlanError("Tên nhánh không hợp lệ.")
    _git(root, "check-ref-format", "--branch", name)
    return f"refs/heads/{name}"


def _local_commit(root: Path, plan: _Plan) -> str:
    top = _git(root, "rev-parse", "--show-toplevel").removesuffix(b"\n")
    if Path(os.fsdecode(top)).resolve() != root:
        raise PlanError("Workspace phải là thư mục gốc của Git worktree.")
    owner, _, repo = plan.repo.partition("/")
    GitHubClient._repo_path(owner, repo)
    if not plan.title.strip() or len(plan.title) > 256 or _UNSAFE.search(plan.title):
        raise PlanError("Tiêu đề không hợp lệ.")
    ref = _ref(root, plan.branch)
    if _git_result(root, "symbolic-ref", "-q", "--no-recurse", ref, allowed=(0, 1)).returncode == 0:
        raise PlanError("Nhánh cục bộ là symbolic ref.")
    head = _git(root, "show-ref", "--hash", "--verify", ref).decode("ascii").strip()
    paths = {item.path for item in plan.files}
    if (
        not _OBJECT_ID.fullmatch(head) or not _OBJECT_ID.fullmatch(plan.base_sha)
        or not paths or not paths <= ALLOWED_FILES or len(paths) != len(plan.files)
        or any(
            item.mode not in {"100644", "100755"} or not _OBJECT_ID.fullmatch(item.blob_oid)
            for item in plan.files
        )
    ):
        raise PlanError("Plan nguồn chứa SHA, đường dẫn hoặc mode không hợp lệ.")

    header, _, message = _git(root, "cat-file", "commit", head).partition(b"\n\n")
    parents = [line for line in header.split(b"\n") if line.startswith(b"parent ")]
    expected_message = f"{plan.title}\n\nDependency-Plan-SHA256: {plan.plan_sha256}\n".encode()
    if parents != [f"parent {plan.base_sha}".encode()] or message != expected_message:
        raise PlanError("Commit cục bộ không khớp parent và dấu hash của plan nguồn.")
    raw = _git(
        root, "diff-tree", "-r", "--raw", "-z", "--no-renames", "--no-abbrev",
        "--no-ext-diff", "--no-textconv", f"-O{os.devnull}", plan.base_sha, head, "--",
    )
    fields = raw.rstrip(b"\0").split(b"\0")
    expected = {item.path: (item.mode, item.blob_oid) for item in plan.files}
    if len(fields) == 2 * len(expected):
        for meta, path in zip(fields[::2], fields[1::2], strict=True):
            parts = meta.decode("ascii").split()
            if len(parts) != 5 or parts[0] not in {":100644", ":100755"} or parts[4] != "M":
                break
            if expected.pop(os.fsdecode(path), None) != (parts[1], parts[3]):
                break
        else:
            if not expected:
                return head
    raise PlanError("Cây commit khác danh sách blob và mode đã duyệt.")


@dataclass
class _Remote:
    git_dir: Path
    url: str
    env: dict[str, str]

    def _run(
        self, *args: str, hook: Path | None = None, allowed: tuple[int, ...] = (0,),
    ) -> bytes:
        options = ["-c", "http.followRedirects=false"]
        if hook is not None:
            options.extend(["-c", f"core.hooksPath={hook}"])
        return _git_result(
            self.git_dir, *options, *args, allowed=allowed, extra_env=self.env,
        ).stdout

    def refs(self, *refs: str) -> dict[str, str]:
        data = self._run("ls-remote", "--refs", "--", self.url, *refs)
        result: dict[str, str] = {}
        for line in data.decode("utf-8").splitlines():
            sha, sep, ref = line.partition("\t")
            if not sep or not _OBJECT_ID.fullmatch(sha) or ref not in refs or ref in result:
                raise PlanError("Git remote trả về ref không hợp lệ.")
            result[ref] = sha
        return result

    def create(self, head: str, ref: str) -> None:
        hook = self.git_dir / "pre-push"
        expected = shlex.quote(f"{head} {ref} {'0' * len(head)}")
        # Dùng old OID trong handshake để Git server kiểm đua khi tạo ref.
        hook.write_text(
            "#!/bin/sh\n"
            "IFS=' ' read -r local_ref local_oid remote_ref old_oid extra || exit 1\n"
            f'test "$local_oid $remote_ref $old_oid" = {expected} || exit 1\n'
            'test -z "$extra" || exit 1\n'
            "if IFS= read -r extra; then exit 1; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)
        self._run(
            "-c", "push.followTags=false", "push", "--no-signed", "--recurse-submodules=no",
            "--", self.url, f"{head}:{ref}", hook=self.git_dir,
        )


@contextmanager
def _remote(
    root: Path, repo: str, local_remote: str | None, transport: httpx.BaseTransport | None,
) -> Iterator[_Remote]:
    rewrites = _git_result(
        root, "config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$", allowed=(0, 1),
    )
    if rewrites.returncode == 0:
        raise PlanError("Git config có URL rewrite. Từ chối truy cập remote.")
    if local_remote is not None and (
        not isinstance(transport, httpx.MockTransport)
        or not Path(local_remote).is_absolute() or not Path(local_remote).is_dir()
    ):
        raise PlanError("Remote thử nghiệm cần một repo cục bộ và HTTP MockTransport.")

    with TemporaryDirectory(prefix="github-bot-publish-") as tmp:
        git_dir = Path(tmp)
        object_format = _git(root, "rev-parse", "--show-object-format").decode("ascii").strip()
        if object_format not in {"sha1", "sha256"}:
            raise PlanError("Định dạng object Git không được hỗ trợ.")
        _git(git_dir, "init", "--bare", "--template=", f"--object-format={object_format}")
        objects = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        env = {"GIT_OBJECT_DIRECTORY": os.fsdecode(objects.removesuffix(b"\n"))}
        if local_remote is not None:
            yield _Remote(git_dir, local_remote, env | {"GIT_ALLOW_PROTOCOL": "file"})
            return

        askpass = git_dir / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  Username*) printf "%s\\n" x-access-token ;;\n'
            '  Password*) printf "%s\\n" "$BOT_PUBLISH_TOKEN" ;;\n'
            '  *) exit 1 ;;\nesac\n',
            encoding="ascii",
        )
        askpass.chmod(0o700)
        env.update(
            GIT_ALLOW_PROTOCOL="https", GIT_ASKPASS=str(askpass),
            BOT_PUBLISH_TOKEN=os.environ.get("GITHUB_TOKEN", "").strip(),
        )
        yield _Remote(git_dir, f"https://github.com/{repo}.git", env)


def _verify_repo(client: GitHubClient, repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    metadata = client.get_repo(owner, name)
    full_name = metadata.get("full_name") if metadata is not None else None
    if not isinstance(full_name, str) or full_name.casefold() != repo.casefold():
        raise PlanError("Không xác minh được repo GitHub đích.")
    return owner, name


def publish_plan(
    workspace: Path, path: str, base: str, transport: httpx.BaseTransport | None,
    local_remote: str | None,
) -> dict[str, object]:
    source = _load_hashed_plan(workspace, path, _Plan, None)
    root = workspace.resolve()
    head = _local_commit(root, source)
    base_ref, head_ref = _ref(root, base), _ref(root, source.branch)
    if base_ref == head_ref:
        raise PlanError("Nhánh base và head phải khác nhau.")
    client = GitHubClient(transport=transport)
    try:
        _verify_repo(client, source.repo)
        with _remote(root, source.repo, local_remote, transport) as remote:
            refs = remote.refs(base_ref, head_ref)
        if refs.get(base_ref) != source.base_sha or refs.get(head_ref) not in (None, head):
            raise PlanError("Base hoặc head remote không khớp commit đã duyệt.")
        if _local_commit(root, source) != head:
            raise PlanError("Nhánh cục bộ đã thay đổi trong lúc lập plan.")
    finally:
        client.close()
    plan: dict[str, object] = {
        "repo": source.repo, "base_ref": base, "base_sha": source.base_sha,
        "head_ref": source.branch, "head_sha": head, "title": source.title,
        "source_plan": source.model_dump(),
    }
    plan["plan_sha256"] = dependency_plan_hash(plan)
    return plan


def _matching_pull(plan: _PublishPlan, raw: dict[str, object]) -> _Pull:
    try:
        pull = _Pull.model_validate(raw)
    except ValidationError:
        raise PlanError("Phản hồi PR không hợp lệ.") from None
    url = f"https://github.com/{plan.repo}/pull/{pull.number}"
    if (
        pull.state != "open" or not pull.draft or pull.title != plan.title
        or (pull.head.ref, pull.head.sha) != (plan.head_ref, plan.head_sha)
        or (pull.base.ref, pull.base.sha) != (plan.base_ref, plan.base_sha)
        or pull.head.repo.full_name.casefold() != plan.repo.casefold()
        or pull.base.repo.full_name.casefold() != plan.repo.casefold()
        or pull.html_url.casefold() != url.casefold()
    ):
        raise PlanError("PR cùng head xung đột với plan đã duyệt.")
    return pull


def _result(plan: _PublishPlan, pull: _Pull, status: str) -> dict[str, object]:
    return {
        "status": status, "pr_number": pull.number,
        "pr_url": f"https://github.com/{plan.repo}/pull/{pull.number}",
    }


def publish(
    workspace: Path, path: str, approved: str, write: bool,
    transport: httpx.BaseTransport | None, local_remote: str | None,
) -> dict[str, object]:
    if not write:
        raise PlanError("Xuất bản PR cần cờ --write.")
    plan = _load_hashed_plan(workspace, path, _PublishPlan, approved)
    source = plan.source_plan
    if (
        dependency_plan_hash(source.model_dump(exclude={"plan_sha256"})) != source.plan_sha256
        or (plan.repo, plan.base_sha, plan.head_ref, plan.title)
        != (source.repo, source.base_sha, source.branch, source.title)
    ):
        raise PlanError("Plan xuất bản khác plan nguồn.")
    root = workspace.resolve()
    if _local_commit(root, source) != plan.head_sha:
        raise PlanError("Nhánh cục bộ đã thay đổi.")
    base_ref, head_ref = _ref(root, plan.base_ref), _ref(root, plan.head_ref)
    if base_ref == head_ref:
        raise PlanError("Nhánh base và head phải khác nhau.")

    client = IssueWriter(transport=transport)
    try:
        owner, repo = _verify_repo(client, plan.repo)

        def existing() -> _Pull | None:
            pulls = client.list_pulls_for_head(owner, repo, plan.head_ref)
            if len(pulls) > 1:
                raise PlanError("Có nhiều PR cùng head. Cần đối chiếu thủ công.")
            return _matching_pull(plan, pulls[0]) if pulls else None

        with _remote(root, plan.repo, local_remote, transport) as remote:
            def recheck() -> str | None:
                if _local_commit(root, source) != plan.head_sha:
                    raise PlanError("Nhánh cục bộ đã thay đổi.")
                refs = remote.refs(base_ref, head_ref)
                head = refs.get(head_ref)
                if refs.get(base_ref) != plan.base_sha or head not in (None, plan.head_sha):
                    raise PlanError("Base hoặc head remote không khớp plan đã duyệt.")
                return head

            head = recheck()
            found = existing()
            if found is not None:
                if head is None or recheck() is None:
                    raise PlanError("PR hiện có nhưng head remote chưa được xác minh.")
                return _result(plan, found, "existing")

            if head is None and recheck() is None:
                try:
                    remote.create(plan.head_sha, head_ref)
                except PlanError:
                    if recheck() is None:
                        raise PlanError("Push chưa được xác nhận. Hãy kiểm tra trước khi chạy lại.") from None

            if recheck() is None:
                raise PlanError("Head remote chưa được xác nhận sau push.")
            found = existing()
            if found is not None:
                if recheck() is None:
                    raise PlanError("PR hiện có nhưng head remote chưa được xác minh.")
                return _result(plan, found, "existing")
            if recheck() is None:
                raise PlanError("Head remote đã biến mất trước khi tạo PR.")
            try:
                payload = client.create_draft_pull(
                    owner, repo, head=plan.head_ref, base=plan.base_ref, title=plan.title,
                )
                created = _matching_pull(plan, payload)
            except (PlanError, GitHubError):
                if recheck() is not None and (found := existing()) is not None and recheck() is not None:
                    return _result(plan, found, "existing")
                raise PlanError("Chưa xác minh được kết quả tạo PR. Hãy kiểm tra trước khi chạy lại.") from None
            if recheck() is None:
                raise PlanError("PR đã được tạo nhưng head remote không còn. Hãy kiểm tra thủ công.")
            return _result(plan, created, "created")
    finally:
        client.close()
