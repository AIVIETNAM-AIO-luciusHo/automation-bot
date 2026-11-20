import hashlib
import json

import pytest
from test_dependency_pr_plan import git, plan, snapshot, stage
from test_dependency_pr_plan import repo as repo_fixture

from github_bot.cli import main

PLAN_FILE = "dependency-plan.json"
repo = repo_fixture


def write_plan(repo, data) -> None:
    (repo / PLAN_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def apply(repo, capsys, approved_hash, *, write=True):
    args = [
        "dependencies", "pr-apply", "--workspace", str(repo), "--plan", PLAN_FILE,
        "--approve-hash", approved_hash,
    ]
    if write:
        args.append("--write")
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def refs(repo) -> dict[str, str]:
    return dict(
        line.split(" ", 1)
        for line in git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    )


@pytest.fixture
def approved(repo, capsys):
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "tester@example.invalid")
    git(repo, "config", "core.filemode", "true")
    (repo / "uv.lock").chmod(0o755)
    stage(repo, "pyproject.toml", "project update\n")
    stage(repo, "uv.lock", "lock update\n")
    rc, out, err = plan(repo, capsys, "pyproject.toml", "uv.lock")
    assert rc == 0, out + err
    data = json.loads(out)
    write_plan(repo, data)
    return data


def test_pr_apply_creates_exact_commit_then_returns_existing(repo, capsys, approved) -> None:
    prior_refs = refs(repo)
    status, index, _, head = snapshot(repo)
    tree_before = {path: (repo / path).read_bytes() for path in ("pyproject.toml", "uv.lock", "notes.md", PLAN_FILE)}

    rc, out, err = apply(repo, capsys, approved["plan_sha256"])
    assert rc == 0, out + err
    result = json.loads(out)
    branch, commit = result["branch"], result["commit_sha"]
    assert result["status"] == "created" and branch == approved["branch"]
    assert refs(repo) == prior_refs | {f"refs/heads/{branch}": commit}
    assert git(repo, "rev-list", "--parents", "-n", "1", commit) == f"{commit} {approved['base_sha']}"
    assert git(repo, "show", "-s", "--format=%s", commit) == approved["title"]
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines() == [
        "pyproject.toml", "uv.lock"
    ]
    assert {item["mode"] for item in approved["files"]} == {"100644", "100755"}
    for item in approved["files"]:
        record = f"{item['mode']} blob {item['blob_oid']}\t{item['path']}"
        assert git(repo, "ls-tree", commit, "--", item["path"]) == record

    rc, out, err = apply(repo, capsys, approved["plan_sha256"])
    assert rc == 0, out + err
    again = json.loads(out)
    assert (again["status"], again["branch"], again["commit_sha"]) == ("existing", branch, commit)
    assert snapshot(repo) == (
        status, index,
        "\n".join(f"{ref} {oid}" for ref, oid in sorted((prior_refs | {f"refs/heads/{branch}": commit}).items())),
        head,
    )
    assert all((repo / path).read_bytes() == content for path, content in tree_before.items())


@pytest.mark.parametrize(
    "case", ["wrong-hash", "no-write", "head-moved", "stage-moved", "unstaged",
             "tampered-content", "forged-valid-hash", "foreign-branch"],
)
def test_pr_apply_rejects_without_changing_git(repo, capsys, approved, case) -> None:
    approved_hash = approved["plan_sha256"]
    if case == "wrong-hash":
        approved_hash = ("0" if approved_hash[0] != "0" else "1") + approved_hash[1:]
    if case == "head-moved":
        new_head = git(
            repo, "-c", "user.name=Tester", "-c", "user.email=tester@example.invalid",
            "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"), "-p", approved["base_sha"],
            "-m", "fixture moved HEAD",
        )
        git(repo, "update-ref", "HEAD", new_head)
    if case == "stage-moved":
        stage(repo, "pyproject.toml", "new staged contents\n")
    if case == "unstaged":
        (repo / "uv.lock").write_text("unstaged contents\n", encoding="utf-8")
    if case == "tampered-content":
        approved["title"] = "Nội dung chưa duyệt"
        write_plan(repo, approved)
    if case == "forged-valid-hash":
        approved["files"][0]["blob_oid"] = git(repo, "rev-parse", "HEAD:notes.md")
        payload = {key: value for key, value in approved.items() if key != "plan_sha256"}
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        approved_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        approved["plan_sha256"] = approved_hash
        write_plan(repo, approved)
    if case == "foreign-branch":
        git(repo, "branch", approved["branch"], approved["base_sha"])

    before = snapshot(repo)
    try:
        rc, out, err = apply(repo, capsys, approved_hash, write=case != "no-write")
    finally:
        assert snapshot(repo) == before
    assert rc != 0 and (out + err).strip()
    assert not out.lstrip().startswith("{")
