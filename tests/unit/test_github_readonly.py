from pathlib import Path

import httpx
import pytest

from github_bot.cli import main
from github_bot.github.client import GitHubClient, GitHubError
from github_bot.github.validator import validate_against_github
from github_bot.identities.resolver import Resolved
from github_bot.models import Task

TOKEN = "ghp_SECRET_TOKEN_123"


def client(handler, **kw) -> GitHubClient:  # type: ignore[no-untyped-def]
    return GitHubClient(TOKEN, transport=httpx.MockTransport(handler), **kw)


def ok_handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p == "/repos/o/r":
        return httpx.Response(200, json={"full_name": "o/r", "archived": False, "has_issues": True, "permissions": {"push": True}})
    if p == "/repos/o/r/assignees/dev-a":
        return httpx.Response(204)
    if p.startswith("/repos/o/r/assignees/"):
        return httpx.Response(404)
    if p == "/repos/o/r/labels":
        return httpx.Response(200, json=[{"name": "Backend"}, {"name": "auth"}])
    return httpx.Response(500)


def task(tid: str = "TASK-001", labels: list[str] | None = None) -> Task:
    return Task(id=tid, title="t", labels=labels or [], source_file="x.md", source_reference=tid)


def check(c: GitHubClient, assignees: list[str], labels: list[str] | None = None) -> list[str]:
    t = task(labels=labels)
    r = Resolved(task_id=t.id, assignees=assignees)
    return [d.code for d in validate_against_github(c, "o/r", [t], [r])]


def test_only_get_and_headers():
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return ok_handler(req)

    assert check(client(h), ["dev-a"], ["backend"]) == []
    assert {r.method for r in seen} == {"GET"} and len(seen) == 3
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}" and seen[0].headers["user-agent"]
    assert not any(m in dir(GitHubClient) for m in ("post", "put", "patch", "delete"))


def test_blocks_for_missing_label_and_unassignable():
    assert check(client(ok_handler), ["dev-x"], ["nope"]) == ["NOT_ASSIGNABLE", "LABEL_MISSING"]


@pytest.mark.parametrize(
    ("resp", "code"),
    [
        (httpx.Response(404), "REPO_NOT_FOUND"),
        (httpx.Response(401), "AUTH_FAILED"),
        (httpx.Response(403), "FORBIDDEN"),
        (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), "RATE_LIMITED"),
        (httpx.Response(429), "RATE_LIMITED"),
        (httpx.Response(500), "SERVER_ERROR"),
        (httpx.Response(301, headers={"location": "https://evil.example/"}), "REDIRECT"),
        (httpx.Response(200, text="not json"), "BAD_RESPONSE"),
        (httpx.Response(200, json={"full_name": "o/r", "archived": True, "has_issues": True}), "REPO_READONLY"),
        (httpx.Response(200, json={"full_name": "o/r", "archived": False, "has_issues": False}), "ISSUES_DISABLED"),
        (httpx.Response(200, json={}), "BAD_RESPONSE"),
        (httpx.Response(200, json={"full_name": "o/r", "archived": False, "has_issues": True}), "NO_ASSIGN_PERMISSION"),
    ],
)
def test_repo_level_failures_fail_closed(resp: httpx.Response, code: str):
    assert check(client(lambda _r: resp), ["dev-a"]) == [code]


def test_network_error_blocks_without_leaking_token():
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"fail {req.headers['authorization']}")

    diags = validate_against_github(client(boom), "o/r", [task()], [Resolved(task_id="TASK-001")])
    assert [d.code for d in diags] == ["NETWORK"] and TOKEN not in diags[0].message


def test_midway_error_blocks_and_stops():
    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(429) if "assignees" in req.url.path else ok_handler(req)

    assert check(client(h), ["dev-a", "dev-b"]) == ["RATE_LIMITED"]
    assert len(calls) == 2  # repo + 1 assignee, không gọi tiếp


def test_label_pagination_and_page_limit():
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/o/r":
            return httpx.Response(200, json={"full_name": "o/r", "archived": False, "has_issues": True})
        page = int(req.url.params["page"])
        return httpx.Response(200, json=[{"name": f"l{page}-{i}"} for i in range(100)])

    c = client(h, max_pages=2)
    assert check(c, [], ["l1-5"]) == ["TOO_MANY_PAGES"]  # luôn đầy trang -> không chắc đã đủ


def test_invalid_refs_never_reach_network():
    def h(req: httpx.Request) -> httpx.Response:
        raise AssertionError("không được gọi mạng")

    c = client(h)
    for owner, repo in (("o/../x", "r"), ("o", "r/x"), ("o", ".."), ("", "r")):
        with pytest.raises(GitHubError):
            c.get_repo(owner, repo)
    with pytest.raises(GitHubError):
        c.can_assign("o", "r", "a/b")


def test_token_required_https_only_and_repr_hides_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(GitHubError) as e:
        GitHubClient()
    assert e.value.code == "NO_TOKEN"
    with pytest.raises(ValueError):
        GitHubClient(TOKEN, base_url="http://api.github.com")
    assert TOKEN not in repr(client(ok_handler))
    assert GitHubClient(" x ")._http.headers["authorization"] == "Bearer x"


def test_cli_offline_by_default_and_opt_in(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "m.yaml").write_text("members:\n  TV01: {display_name: A, github: dev-a}\n")
    (tmp_path / "t.md").write_text("## TASK-001: a\n- Phụ trách: TV01\n- Labels: nope\n")
    base = ["issues", "validate", "--workspace", str(tmp_path), "--input", "t.md", "--members", "m.yaml"]

    def forbidden(req: httpx.Request) -> httpx.Response:
        raise AssertionError("offline phải không gọi mạng")

    assert main(base, transport=httpx.MockTransport(forbidden)) == 0  # không --check-github
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    gh = [*base, "--check-github", "--repo", "o/r"]
    assert main(gh, transport=httpx.MockTransport(ok_handler)) == 1
    out = capsys.readouterr().out
    assert "BLOCK [LABEL_MISSING] TASK-001" in out and TOKEN not in out
    assert main([*base, "--check-github"]) == 2  # thiếu --repo
    assert main([*base, "--repo", "o/r"], transport=httpx.MockTransport(forbidden)) == 0  # --repo mà không opt-in: vẫn offline
    monkeypatch.delenv("GITHUB_TOKEN")
    assert main(gh) == 2 and "GITHUB_TOKEN" in capsys.readouterr().out
