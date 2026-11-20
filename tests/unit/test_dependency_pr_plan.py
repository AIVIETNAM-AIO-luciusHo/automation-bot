import json
import re
import subprocess
from pathlib import Path

import pytest

from github_bot.cli import main


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    for name in ("pyproject.toml", "uv.lock", "notes.md"):
        (tmp_path / name).write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "--", "pyproject.toml", "uv.lock", "notes.md")
    git(
        tmp_path, "-c", "user.name=Tester", "-c", "user.email=tester@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture",
    )
    return tmp_path


def snapshot(repo: Path) -> tuple[str, bytes, str, str]:
    return (
        git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        (repo / ".git/index").read_bytes(),
        git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        (repo / ".git/HEAD").read_text(encoding="utf-8"),
    )


def plan(repo: Path, capsys: pytest.CaptureFixture[str], *files: str) -> tuple[int, str, str]:
    argv = [
        "dependencies", "pr-plan", "--workspace", str(repo), "--repo", "owner/repo",
        "--branch", "deps/update", "--title", "Cập nhật phụ thuộc",
    ]
    for file in files:
        argv.extend(["--file", file])
    before = snapshot(repo)
    try:
        rc = main(argv)
    finally:
        assert snapshot(repo) == before
    out, err = capsys.readouterr()
    return rc, out, err


def stage(repo: Path, name: str, content: str = "staged\n") -> None:
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "--", name)


def test_pr_plan_uses_index_and_stable_hash(repo, capsys) -> None:
    stage(repo, "pyproject.toml")
    stage(repo, "uv.lock")
    rc, out, err = plan(repo, capsys, "pyproject.toml", "uv.lock")
    assert rc == 0, out + err
    data = json.loads(out)
    assert data["repo"] == "owner/repo"
    assert data["base_sha"] == git(repo, "rev-parse", "HEAD")
    assert data["branch"] == "deps/update" and data["title"] == "Cập nhật phụ thuộc"
    assert sorted(item["path"] for item in data["files"]) == ["pyproject.toml", "uv.lock"]
    for item in data["files"]:
        assert git(repo, "rev-parse", f":{item['path']}") in item.values()
    assert re.fullmatch(r"[0-9a-f]{64}", data["plan_sha256"])

    rc, repeated, err = plan(repo, capsys, "pyproject.toml", "uv.lock")
    assert rc == 0, repeated + err
    assert json.loads(repeated)["plan_sha256"] == data["plan_sha256"]

    stage(repo, "uv.lock", "different staged blob\n")
    rc, changed, err = plan(repo, capsys, "pyproject.toml", "uv.lock")
    assert rc == 0, changed + err
    assert json.loads(changed)["plan_sha256"] != data["plan_sha256"]


@pytest.mark.parametrize(
    "case", ["no-diff", "omitted-staged", "selected-unchanged", "unexpected-staged",
             "deleted", "symlink", "unstaged", "new"],
)
def test_pr_plan_rejects_invalid_index_without_partial_plan(repo, capsys, case) -> None:
    selected = ["pyproject.toml"]
    if case in {"omitted-staged", "selected-unchanged", "unexpected-staged", "unstaged"}:
        stage(repo, "pyproject.toml")
    if case == "omitted-staged":
        stage(repo, "uv.lock")
    if case == "selected-unchanged":
        selected.append("uv.lock")
    if case == "unexpected-staged":
        stage(repo, "notes.md")
    if case in {"deleted", "new"}:
        git(repo, "rm", "--", "pyproject.toml")
    if case == "symlink":
        (repo / "pyproject.toml").unlink()
        (repo / "pyproject.toml").symlink_to("notes.md")
        git(repo, "add", "--", "pyproject.toml")
    if case == "unstaged":
        (repo / "pyproject.toml").write_text("unstaged\n", encoding="utf-8")
    if case == "new":
        git(repo, "-c", "user.name=Tester", "-c", "user.email=tester@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "fixture without pyproject")
        stage(repo, "pyproject.toml")

    rc, out, err = plan(repo, capsys, *selected)
    assert rc != 0 and (out + err).strip()
    assert "plan_sha256" not in out and not out.lstrip().startswith("{")
