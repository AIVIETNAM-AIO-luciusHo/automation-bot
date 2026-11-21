import hashlib
import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fake_github import TOKEN
from test_dependency_pr_plan import git, plan, snapshot, stage
from test_dependency_pr_plan import repo as repo_fixture

from github_bot.cli import main

repo = repo_fixture
SLUG = "owner/repo"
BRANCH = "deps/update"
PR_URL = f"https://github.com/{SLUG}/pull/42"


@pytest.fixture
def delivery(repo, tmp_path_factory, capsys, monkeypatch):
    remote = tmp_path_factory.mktemp("publish-remote") / "remote.git"
    git(repo, "init", "--bare", str(remote))
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "push", str(remote), "HEAD:refs/heads/main")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "tester@example.invalid")
    stage(repo, "pyproject.toml", "dependency update\n")
    rc, out, err = plan(repo, capsys, "pyproject.toml")
    assert rc == 0, out + err
    local = json.loads(out)
    (repo / "local-plan.json").write_text(out, encoding="utf-8")
    prefix = ["dependencies"]
    workspace = ["--workspace", str(repo)]
    rc = main([
        *prefix, "pr-apply", *workspace, "--plan", "local-plan.json",
        "--approve-hash", local["plan_sha256"], "--write",
    ])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    head = json.loads(captured.out)["commit_sha"]
    state = SimpleNamespace(prs=[], pushes=[], posts=[], mode="", base=base, head=head)
    real_run = subprocess.run

    def run_git(command, *args, **kwargs):
        if isinstance(command, list) and command[0] == "git" and (
            "ls-remote" in command or "push" in command
        ):
            assert str(remote) in command or remote.as_uri() in command, command
            if "push" in command:
                state.pushes.append(command)
                if state.mode == "racing-head":
                    set_remote(BRANCH, base)
                result = real_run(command, *args, **kwargs)
                if state.mode == "push-timeout":
                    state.mode = ""
                    raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
                return result
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run_git)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)

    def pr_payload(base_ref="main", sha=head):
        return {
            "number": 42, "html_url": PR_URL, "state": "open", "draft": True,
            "title": local["title"],
            "base": {"ref": base_ref, "sha": base, "repo": {"full_name": SLUG}},
            "head": {"ref": BRANCH, "sha": sha, "repo": {"full_name": SLUG},
                     "label": f"owner:{BRANCH}"},
        }

    def respond(request):
        assert request.url.host == "api.github.com" and request.url.scheme == "https"
        assert request.method in {"GET", "POST"}
        path = request.url.path
        if path == f"/repos/{SLUG}/pulls" and request.method == "GET":
            return httpx.Response(200, json=state.prs)
        if path == f"/repos/{SLUG}/pulls" and request.method == "POST":
            payload = json.loads(request.content)
            assert payload["draft"] is True and payload["base"] == "main"
            assert payload["head"] in (BRANCH, f"owner:{BRANCH}")
            assert payload["title"] == local["title"]
            state.posts.append(payload)
            state.prs.append(pr_payload())
            if state.mode == "create-timeout":
                state.mode = ""
                raise httpx.ReadTimeout("mock", request=request)
            return httpx.Response(201, json=state.prs[-1])
        assert request.method == "GET"
        if path == f"/repos/{SLUG}":
            return httpx.Response(200, json={"full_name": SLUG, "default_branch": "main"})
        return httpx.Response(404, json={"message": "Not found"})

    transport = httpx.MockTransport(respond)

    def cli(command, *argv):
        rc = main(
            [*prefix, command, *workspace, *argv],
            transport=transport, publish_remote=str(remote),
        )
        output = capsys.readouterr()
        assert TOKEN not in output.out + output.err
        return rc, output.out, output.err

    def preview():
        return cli("pr-publish-plan", "--plan", "local-plan.json", "--base", "main")

    def publish(sha, *, write=True):
        return cli("pr-publish", "--plan", "publish-plan.json", "--approve-hash", sha,
                   *(["--write"] if write else []))

    def set_remote(name, sha):
        real_run(["git", "-C", str(repo), "push", str(remote), f"{sha}:refs/heads/{name}"], check=True,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    return SimpleNamespace(repo=repo, remote=remote, state=state, preview=preview, publish=publish,
                           set_remote=set_remote, pr_payload=pr_payload)


def test_publish_preview_binds_target_and_is_read_only(delivery):
    before = snapshot(delivery.repo)
    rc, out, err = delivery.preview()
    assert rc == 0, out + err
    data = json.loads(out)
    assert set(data) == {
        "repo", "base_ref", "base_sha", "head_ref", "head_sha", "title", "source_plan", "plan_sha256",
    }
    assert data["source_plan"] == json.loads(
        (delivery.repo / "local-plan.json").read_text(encoding="utf-8")
    )
    canonical = json.dumps(
        {key: value for key, value in data.items() if key != "plan_sha256"},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == data["plan_sha256"]
    assert data["repo"] == SLUG and data["base_sha"] == delivery.state.base
    assert data["head_sha"] == delivery.state.head
    assert data["base_ref"] in ("main", "refs/heads/main")
    assert data["head_ref"] in (BRANCH, f"refs/heads/{BRANCH}")
    assert data["title"] == "Cập nhật phụ thuộc"
    rc, out, err = delivery.preview()
    assert rc == 0 and json.loads(out)["plan_sha256"] == data["plan_sha256"], err
    assert snapshot(delivery.repo) == before
    assert not delivery.state.pushes and not delivery.state.posts


@pytest.fixture
def publish_plan(delivery):
    rc, out, err = delivery.preview()
    assert rc == 0, out + err
    (delivery.repo / "publish-plan.json").write_text(out, encoding="utf-8")
    return json.loads(out)


@pytest.mark.parametrize("mode", ["", "push-timeout", "create-timeout"])
def test_publish_retry_never_duplicates_push_or_pr(delivery, publish_plan, mode):
    before = snapshot(delivery.repo)
    delivery.state.mode = mode
    rc, out, err = delivery.publish(publish_plan["plan_sha256"])
    if not mode:
        assert rc == 0, out + err
        created = json.loads(out)
        assert (created["status"], created["pr_number"], created["pr_url"]) == ("created", 42, PR_URL)
    rc, out, err = delivery.publish(publish_plan["plan_sha256"])
    assert rc == 0, out + err
    retried = json.loads(out)
    if mode == "push-timeout":
        # Lần đầu có thể đã đọc lại remote và hoàn tất PR sau timeout.
        assert retried["status"] in {"created", "existing"}
    else:
        assert retried["status"] == "existing"
    assert (retried["pr_number"], retried["pr_url"]) == (42, PR_URL)
    assert len(delivery.state.pushes) == len(delivery.state.posts) == len(delivery.state.prs) == 1
    assert git(delivery.remote, "rev-parse", f"refs/heads/{BRANCH}") == delivery.state.head
    assert snapshot(delivery.repo) == before


@pytest.mark.parametrize("case", ["wrong-hash", "no-write", "base-moved", "remote-head", "local-head", "pr-conflict"])
def test_publish_rejects_conflicts_without_writing(delivery, publish_plan, case):
    digest = publish_plan["plan_sha256"]
    if case == "wrong-hash":
        digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    if case == "base-moved":
        delivery.set_remote("main", delivery.state.head)
    if case == "remote-head":
        delivery.set_remote(BRANCH, delivery.state.base)
    if case == "local-head":
        git(delivery.repo, "update-ref", f"refs/heads/{BRANCH}", delivery.state.base)
    if case == "pr-conflict":
        delivery.set_remote(BRANCH, delivery.state.head)
        delivery.state.prs.append(delivery.pr_payload(base_ref="other"))
    before_local = snapshot(delivery.repo)
    before_remote = git(delivery.remote, "show-ref")
    rc, out, err = delivery.publish(digest, write=case != "no-write")
    assert rc != 0 and (out + err).strip()
    assert not delivery.state.pushes and not delivery.state.posts
    assert git(delivery.remote, "show-ref") == before_remote
    assert snapshot(delivery.repo) == before_local


def test_publish_does_not_overwrite_head_created_between_read_and_push(delivery, publish_plan):
    before = snapshot(delivery.repo)
    assert git(delivery.repo, "merge-base", delivery.state.base, delivery.state.head) == delivery.state.base
    delivery.state.mode = "racing-head"
    rc, out, err = delivery.publish(publish_plan["plan_sha256"])
    assert rc != 0 and (out + err).strip()
    assert len(delivery.state.pushes) == 1 and not delivery.state.posts
    assert git(delivery.remote, "rev-parse", f"refs/heads/{BRANCH}") == delivery.state.base
    assert snapshot(delivery.repo) == before


def test_publish_rejects_git_url_rewrite(delivery, publish_plan):
    git(delivery.repo, "config", "url.file:///invalid.git.pushInsteadOf", str(delivery.remote))
    rc, out, err = delivery.publish(publish_plan["plan_sha256"])
    assert rc != 0 and "URL rewrite" in out + err
    assert not delivery.state.pushes and not delivery.state.posts
