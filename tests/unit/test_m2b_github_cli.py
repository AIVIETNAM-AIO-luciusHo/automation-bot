"""M2b black-box tests: `issues validate --check-github --repo OWNER/REPO` against a fake GitHub.

Contract assumed (flag to leader if coder differs):
- Offline by default: without --check-github ZERO HTTP requests, even if --repo is given.
- `--check-github` requires `--repo owner/repo` (strict format, validated BEFORE any URL is built)
  and env GITHUB_TOKEN (no --token flag). Missing/blank token or missing --repo -> exit != 0,
  clear message (mentions GITHUB_TOKEN / --repo), zero requests, no Traceback.
- HTTP goes through httpx.Client (sync); tests inject httpx.MockTransport by patching
  httpx.Client.__init__. Only GET to https://api.github.com. Order: repo first (stop on failure),
  then assignee eligibility for each distinct login that resolved in M2a, then labels.
- Every network/HTTP failure (401/403/404/429/5xx, timeout, bad JSON, missing fields) -> BLOCK
  (exit != 0), never a pass. Strict success: repo 200 + JSON object with matching full_name and
  has_issues true and archived false; when any task needs an assignee, `permissions.push` or
  `permissions.triage` must be true (GitHub silently drops assignees otherwise) else BLOCK with
  the reason named; assignee exactly 204; label found (case-insensitive) in the
  full paginated list. Missing label is BLOCK, never auto-created.
- Redirects/Link headers pointing at another host are never trusted (token never sent; the answer
  of a foreign host cannot make validation pass).
- 429 and rate-limit 403 (X-RateLimit-Remaining: 0): BLOCK immediately, exactly 1 request, no
  sleep at all, message includes the reset time when X-RateLimit-Reset / Retry-After is present.
- Other transient errors: retries (if any) bounded (<= 4 requests per endpoint), never sleep > 60s.
Every test additionally runs fake_github.assert_requests_safe() on teardown.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_github import HOST, OMIT, TOKEN, FakeGitHub, assert_requests_safe

from github_bot.cli import main

MEMBERS = """members:
  TV01: {display_name: "Nguyễn Văn A", github: "example-dev-1", aliases: ["Anh A"]}
  TV02: {display_name: "Trần Văn B", github: "example-dev-2"}
  TV03: {display_name: "Lê Văn C", github: "example-dev-3"}
"""
TASKS = """## TASK-001: Một
- Phụ trách: TV01
- Labels: backend, auth

## TASK-002: Hai
- Phụ trách: TV02, TV03
- Labels: database
"""
GH = ["--check-github", "--repo", "owner/repo"]


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    import time

    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.chdir(w)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    st = SimpleNamespace(ws=w, fake=FakeGitHub(), history=[], sleeps=[])
    st.history.append(st.fake)
    orig_init = httpx.Client.__init__

    def patched(self: httpx.Client, *a: Any, **k: Any) -> None:
        k["transport"] = httpx.MockTransport(lambda req: st.fake(req))
        orig_init(self, *a, **k)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    monkeypatch.setattr(time, "sleep", lambda s: st.sleeps.append(float(s)))
    yield st
    for f in st.history:
        assert_requests_safe(f)
    assert max(st.sleeps, default=0) <= 60, "sleeps longer than 60s"


def run(
    gh: Any, capsys: pytest.CaptureFixture[str], fake: FakeGitHub | None = None,
    md: str = TASKS, args: list[str] | None = None, members: str = MEMBERS,
) -> tuple[int, str]:
    if fake is not None:
        gh.fake = fake
        gh.history.append(fake)
    (gh.ws / "TASKS.md").write_text(md, encoding="utf-8")
    (gh.ws / "members.yaml").write_text(members, encoding="utf-8")
    argv = ["issues", "validate", "--input", "TASKS.md", "--members", "members.yaml"]
    try:
        rc = main(argv + (GH if args is None else args))
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    cap = capsys.readouterr()
    out = cap.out + cap.err
    if "unrecognized arguments" in out and "--check-github" in out:
        pytest.fail(f"M2b CLI contract not available: {out.strip()[:200]}")
    assert TOKEN not in out, "token leaked into CLI output"
    assert "Traceback" not in out, out
    return int(rc or 0), out


# ---- opt-in / token handling -------------------------------------------------------------

def test_default_is_offline_zero_requests(gh, capsys) -> None:
    rc, out = run(gh, capsys, args=[])
    assert rc == 0, out
    assert gh.fake.requests == []


def test_repo_flag_alone_does_not_touch_network(gh, capsys) -> None:
    rc, _ = run(gh, capsys, args=["--repo", "owner/repo"])
    assert gh.fake.requests == []
    assert rc == 0


def test_check_github_without_repo_is_a_clear_error(gh, capsys) -> None:
    rc, out = run(gh, capsys, args=["--check-github"])
    assert rc != 0 and "--repo" in out
    assert gh.fake.requests == []


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_or_blank_token_fails_clearly_without_requests(
    gh, capsys, monkeypatch, token
) -> None:
    if token is None:
        monkeypatch.delenv("GITHUB_TOKEN")
    else:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    rc, out = run(gh, capsys)
    assert rc != 0 and "GITHUB_TOKEN" in out
    assert gh.fake.requests == []


def test_token_cannot_be_passed_on_the_command_line(gh, capsys) -> None:
    rc, out = run(gh, capsys, args=[*GH, "--token", "ghp_cli_secret_value"])
    assert rc != 0
    assert "ghp_cli_secret_value" not in out
    assert gh.fake.requests == []


# ---- happy path + read-only ---------------------------------------------------------------

def test_happy_path_passes_and_is_get_only(gh, capsys) -> None:
    rc, out = run(gh, capsys)
    assert rc == 0, out
    assert gh.fake.hits("repo") and gh.fake.hits("labels") and gh.fake.hits("assignee")
    assert {r.method for r in gh.fake.requests} == {"GET"}
    assert all(r.headers.get("authorization", "").endswith(TOKEN) for r in gh.fake.requests)


# ---- repo failures: every one BLOCKs, stops, never leaks -----------------------------------

@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502, 503])
def test_repo_http_errors_block_and_stop(gh, capsys, status) -> None:
    fake = FakeGitHub(repo_status=status)
    rc, out = run(gh, capsys, fake)
    assert rc != 0 and out.strip()
    assert fake.hits("assignee") == [] and fake.hits("labels") == []
    assert len(fake.requests) <= 4, "unbounded retries"


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c"),
     httpx.RemoteProtocolError("p")],
    ids=["connect_timeout", "read_timeout", "connect_error", "protocol_error"],
)
def test_network_faults_block(gh, capsys, exc) -> None:
    def boom(req: httpx.Request) -> httpx.Response | None:
        raise exc

    fake = FakeGitHub(override=boom)
    rc, out = run(gh, capsys, fake)
    assert rc != 0 and out.strip()
    assert len(fake.requests) <= 4


@pytest.mark.parametrize(
    "body",
    ["not json{", "", "[]", "null", "42", '"str"', {"message": "ok"}, {},
     {"full_name": "other/repo", "has_issues": True, "archived": False, "permissions": {"push": True}},
     {"full_name": "owner/repo", "has_issues": False, "archived": False, "permissions": {"push": True}},
     {"full_name": "owner/repo", "has_issues": True, "archived": True, "permissions": {"push": True}}],
    ids=["bad_json", "empty", "list", "null", "number", "string", "no_fields", "empty_obj",
         "name_mismatch", "issues_disabled", "archived"],
)
def test_repo_200_with_bad_or_incomplete_body_blocks(gh, capsys, body) -> None:
    rc, out = run(gh, capsys, FakeGitHub(repo_body=body))
    assert rc != 0 and out.strip()


def test_repo_name_match_is_case_insensitive(gh, capsys) -> None:
    fake = FakeGitHub(
        repo_body={"full_name": "Owner/Repo", "has_issues": True, "archived": False, "permissions": {"push": True}}
    )
    rc, out = run(gh, capsys, fake)
    assert rc == 0, out


# ---- assignee eligibility -----------------------------------------------------------------

def test_ineligible_login_blocks_and_names_it(gh, capsys) -> None:
    fake = FakeGitHub(assignees=["example-dev-1", "example-dev-3"])  # dev-2 -> 404
    rc, out = run(gh, capsys, fake)
    assert rc != 0 and "example-dev-2" in out


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500, 503])
def test_unexpected_assignee_status_blocks(gh, capsys, status) -> None:
    fake = FakeGitHub(assignee_status={"example-dev-1": status})
    rc, out = run(gh, capsys, fake)
    assert rc != 0 and "example-dev-1" in out


def test_assignee_network_error_blocks(gh, capsys) -> None:
    def boom(req: httpx.Request) -> httpx.Response | None:
        if "/assignees/" in req.url.path:
            raise httpx.ReadTimeout("t")
        return None

    rc, out = run(gh, capsys, FakeGitHub(override=boom))
    assert rc != 0 and out.strip()


def test_no_api_call_for_logins_blocked_offline(gh, capsys) -> None:
    md = TASKS + "\n## TASK-003: Ba\n- Phụ trách: TV99, @bad--login, @not-in-registry\n"
    fake = FakeGitHub()
    rc, _ = run(gh, capsys, fake, md=md)
    assert rc != 0
    called = {x.casefold() for x in fake.assignee_hits()}
    assert not called & {"tv99", "bad--login", "not-in-registry"}, called


def test_each_distinct_login_checked_at_most_once(gh, capsys) -> None:
    md = TASKS + "\n## TASK-003: Ba\n- Phụ trách: TV01, @Example-Dev-1\n- Labels: backend\n"
    fake = FakeGitHub()
    rc, out = run(gh, capsys, fake, md=md)
    assert rc == 0, out
    hits = [h.casefold() for h in fake.assignee_hits()]
    assert len(hits) == len(set(hits)), hits


def test_task_without_assignee_makes_no_assignee_call(gh, capsys) -> None:
    fake = FakeGitHub()
    rc, out = run(gh, capsys, fake, md="## TASK-001: Một\n- Labels: backend\n")
    assert rc == 0, out
    assert fake.assignee_hits() == []


# ---- labels --------------------------------------------------------------------------------

def test_missing_label_blocks_names_it_and_never_creates(gh, capsys) -> None:
    fake = FakeGitHub(labels=["backend", "auth"])  # 'database' missing
    rc, out = run(gh, capsys, fake)
    assert rc != 0 and "database" in out
    assert {r.method for r in fake.requests} == {"GET"}


def test_label_match_is_case_insensitive_like_github(gh, capsys) -> None:
    rc, out = run(gh, capsys, FakeGitHub(labels=["Backend", "AUTH", "Database"]))
    assert rc == 0, out


def test_labels_beyond_first_page_are_found(gh, capsys) -> None:
    many = [f"label-{i:03d}" for i in range(250)]
    md = "## TASK-001: Một\n- Phụ trách: TV01\n- Labels: label-000, label-130, label-249\n"
    fake = FakeGitHub(labels=many)
    rc, out = run(gh, capsys, fake, md=md)
    assert rc == 0, out
    assert len(fake.hits("labels")) >= 3


def test_missing_label_with_pagination_still_blocks(gh, capsys) -> None:
    md = "## TASK-001: Một\n- Phụ trách: TV01\n- Labels: label-999\n"
    rc, out = run(gh, capsys, FakeGitHub(labels=[f"label-{i:03d}" for i in range(250)]), md=md)
    assert rc != 0 and "label-999" in out


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, None), (403, None), (404, None), (429, None), (500, None),
     (200, "not json{"), (200, {"message": "x"}), (200, [{"id": 1}]), (200, [None, 3])],
    ids=["401", "403", "404", "429", "500", "bad_json", "object", "no_names", "junk_items"],
)
def test_label_listing_failures_block(gh, capsys, status, body) -> None:
    rc, out = run(gh, capsys, FakeGitHub(label_status=status, label_body=body))
    assert rc != 0 and out.strip()


def test_labels_never_requested_when_no_task_has_labels(gh, capsys) -> None:
    fake = FakeGitHub()
    rc, out = run(gh, capsys, fake, md="## TASK-001: Một\n- Phụ trách: TV01\n")
    assert rc == 0, out
    assert fake.hits("labels") == []


# ---- injection / SSRF ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "repo",
    ["owner/repo/../..", "owner/repo?x=1", "owner/%2e%2e", "owner/repo#frag", "../repo",
     "owner/..", "owner/.", "owner\\repo", "owner/repo\n", "own er/repo", "owner/repo/extra",
     "", "/", "owner", "owner/", "/repo", "evil.com/x@", "owner/repo%00", "http://evil.example/x",
     "owner/repo\r\nHost: evil", "@owner/repo", "owner@evil/repo", "a" * 40 + "/repo",
     "owner/" + "r" * 101, "owner/rep\x00o", "ｏｗｎｅｒ/repo"],
)
def test_malformed_repo_rejected_before_any_url_is_built(gh, capsys, repo) -> None:
    rc, out = run(gh, capsys, args=["--check-github", "--repo", repo])
    assert rc != 0 and out.strip()
    assert gh.fake.requests == [], repo


@pytest.mark.parametrize(
    "label",
    ["../../orgs/x", "a?x=1", "%2e%2e/x", "a/b", "x\r\nHost: evil", "a b#c", "..", "ａ", "a%0d%0ab"],
)
def test_hostile_label_never_changes_the_request_target(gh, capsys, label) -> None:
    md = f"## TASK-001: Một\n- Phụ trách: TV01\n- Labels: {label}\n"
    fake = FakeGitHub()
    rc, _ = run(gh, capsys, fake, md=md)
    assert rc != 0, f"unknown hostile label {label!r} must BLOCK"
    assert fake.foreign == []  # assert_requests_safe() runs on teardown for host/path/method


@pytest.mark.parametrize("login", ["a/b", "a?x=1", "%2e%2e", "..", "a b", "a\nb", "ư", "a@evil"])
def test_hostile_login_is_blocked_offline_and_never_reaches_url(gh, capsys, login) -> None:
    md = f"## TASK-001: Một\n- Phụ trách: @{login}\n"
    fake = FakeGitHub()
    rc, _ = run(gh, capsys, fake, md=md)
    assert rc != 0
    assert fake.assignee_hits() == []


# ---- redirects / foreign hosts / token leakage ----------------------------------------------

def _redirect_to_evil(match: str):  # type: ignore[no-untyped-def]
    def h(req: httpx.Request) -> httpx.Response | None:
        if match in req.url.path:
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        return None

    return h


@pytest.mark.parametrize("where", ["/repos/owner/repo", "/assignees/", "/labels"])
def test_cross_host_redirect_is_not_trusted_and_gets_no_token(gh, capsys, where) -> None:
    def h(req: httpx.Request) -> httpx.Response | None:
        exact = req.url.path.rstrip("/") == "/repos/owner/repo"
        if (where == "/repos/owner/repo" and exact) or (where != "/repos/owner/repo" and where in req.url.path):
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        return None

    fake = FakeGitHub(override=h)
    rc, _ = run(gh, capsys, fake)
    assert rc != 0, "validation must not pass on the word of a foreign host"
    # assert_requests_safe(): no Authorization / token on any request to evil.example


def test_link_header_to_foreign_host_is_not_followed_with_token(gh, capsys) -> None:
    def h(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/labels"):
            return httpx.Response(
                200, json=[{"id": 1, "name": "backend"}, {"id": 2, "name": "auth"}],
                headers={"Link": '<https://evil.example/labels?page=2>; rel="next"'},
            )
        return None

    fake = FakeGitHub(override=h)  # 'database' only exists on the (foreign) "next" page
    rc, out = run(gh, capsys, fake)
    assert rc != 0 and "database" in out


def test_http_downgrade_redirect_blocks(gh, capsys) -> None:
    def h(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.rstrip("/") == "/repos/owner/repo":
            return httpx.Response(301, headers={"Location": f"http://{HOST}/repos/owner/repo"})
        return None

    rc, _ = run(gh, capsys, FakeGitHub(override=h))
    assert rc != 0  # plain-http is never used with a token (assert_requests_safe checks scheme)


@pytest.mark.parametrize(
    ("scenario", "status"),
    [("echo_401", 401), ("echo_500", 500), ("echo_403", 403), ("echo_200_badjson", 200)],
)
def test_token_never_leaks_even_if_server_echoes_it(gh, capsys, caplog, scenario, status) -> None:
    import logging

    caplog.set_level(logging.DEBUG)

    def echo(req: httpx.Request) -> httpx.Response | None:
        auth = req.headers.get("authorization", "")
        return httpx.Response(
            status, content=json.dumps({"message": f"Bad credentials {auth} {TOKEN}"}) + "{"
        )

    rc, out = run(gh, capsys, FakeGitHub(override=echo))
    assert rc != 0
    assert TOKEN not in out and TOKEN not in caplog.text, scenario
    assert "Bearer" not in out and "token ghp_" not in out


def test_token_not_in_exception_text_on_transport_failure(gh, capsys, caplog) -> None:
    def boom(req: httpx.Request) -> httpx.Response | None:
        raise httpx.ConnectError(f"cannot connect with {req.headers.get('authorization')}")

    rc, out = run(gh, capsys, FakeGitHub(override=boom))
    assert rc != 0
    assert TOKEN not in out and TOKEN not in caplog.text


def test_retry_after_is_bounded(gh, capsys) -> None:
    def h(req: httpx.Request) -> httpx.Response | None:
        return httpx.Response(429, headers={"Retry-After": "999999"}, json={"message": "slow"})

    fake = FakeGitHub(override=h)
    rc, _ = run(gh, capsys, fake)
    assert rc != 0
    assert len(fake.requests) <= 4
    assert max(gh.sleeps, default=0) <= 60


# ---- repo permissions: assignees are silently dropped by GitHub without push/triage ---------

NO_ASSIGNEE_MD = "## TASK-001: Một\n- Labels: backend\n"


@pytest.mark.parametrize(
    "perms",
    [{"admin": False, "push": False, "triage": False, "pull": True},
     {"pull": True}, {}, None, "yes", [], {"push": "true", "triage": 1}, OMIT],
    ids=["read_only", "only_pull", "empty", "null", "string", "list", "truthy_non_bool", "missing"],
)
def test_assignees_need_push_or_triage_else_block_with_reason(gh, capsys, perms) -> None:
    fake = FakeGitHub(permissions=perms)
    rc, out = run(gh, capsys, fake)
    assert rc != 0
    low = out.lower()
    assert "push" in low or "triage" in low or "permission" in low or "quyền" in low, out
    assert fake.assignee_hits() == [], "do not probe assignees when the token cannot assign"


@pytest.mark.parametrize(
    "perms",
    [{"push": True, "triage": False, "pull": True}, {"push": False, "triage": True, "pull": True},
     {"admin": True, "maintain": True, "push": True, "triage": True, "pull": True}],
    ids=["push_only", "triage_only", "admin"],
)
def test_push_or_triage_is_enough(gh, capsys, perms) -> None:
    rc, out = run(gh, capsys, FakeGitHub(permissions=perms))
    assert rc == 0, out


@pytest.mark.parametrize("perms", [{"pull": True}, OMIT, None], ids=["read_only", "missing", "null"])
def test_no_assignee_anywhere_needs_no_push_permission(gh, capsys, perms) -> None:
    fake = FakeGitHub(permissions=perms)
    rc, out = run(gh, capsys, fake, md=NO_ASSIGNEE_MD)
    assert rc == 0, out
    assert fake.assignee_hits() == []


def test_permission_check_covers_assignees_given_only_via_github_field(gh, capsys) -> None:
    md = "## TASK-001: Một\n- GitHub: @example-dev-1\n"
    rc, _ = run(gh, capsys, FakeGitHub(permissions={"pull": True}), md=md)
    assert rc != 0


def test_permission_check_ignores_assignees_of_tasks_blocked_offline(gh, capsys) -> None:
    # the only task is already BLOCKed by the resolver (TV99); there is nothing to assign
    md = "## TASK-001: Một\n- Phụ trách: TV99\n"
    fake = FakeGitHub(permissions={"pull": True})
    rc, _ = run(gh, capsys, fake, md=md)
    assert rc != 0 and fake.assignee_hits() == []


# ---- rate limits: BLOCK immediately, no retry, no sleeping ----------------------------------

RESET = "1893456000"  # 2030-01-01T00:00:00Z


def _limited(status: int, headers: dict[str, str]):  # type: ignore[no-untyped-def]
    def h(req: httpx.Request) -> httpx.Response | None:
        return httpx.Response(status, headers=headers, json={"message": "API rate limit exceeded"})

    return h


LIMITS = {
    "429_retry_after": (429, {"Retry-After": "30"}),
    "429_reset": (429, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": RESET}),
    "403_rate_limit": (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": RESET}),
    "429_bare": (429, {}),
}


@pytest.mark.parametrize("name", sorted(LIMITS))
def test_rate_limit_blocks_immediately_without_retry_or_sleep(gh, capsys, name) -> None:
    status, headers = LIMITS[name]
    fake = FakeGitHub(override=_limited(status, headers))
    rc, out = run(gh, capsys, fake)
    assert rc != 0 and out.strip()
    assert len(fake.requests) == 1, f"retried a rate-limited request: {len(fake.requests)}"
    assert gh.sleeps == [], f"slept {gh.sleeps}"
    low = out.lower()
    assert "rate" in low or "giới hạn" in low or "429" in out or "403" in out


@pytest.mark.parametrize("name", ["429_reset", "403_rate_limit"])
def test_rate_limit_message_names_the_reset_time_when_header_present(gh, capsys, name) -> None:
    status, headers = LIMITS[name]
    _, out = run(gh, capsys, FakeGitHub(override=_limited(status, headers)))
    assert RESET in out or "2030-01-01" in out, out


@pytest.mark.parametrize("endpoint", ["/assignees/", "/labels"])
def test_rate_limit_on_later_endpoints_stops_the_run(gh, capsys, endpoint) -> None:
    def h(req: httpx.Request) -> httpx.Response | None:
        if endpoint in req.url.path:
            return httpx.Response(
                429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": RESET}, json={}
            )
        return None

    fake = FakeGitHub(override=h)
    rc, _ = run(gh, capsys, fake)
    assert rc != 0
    assert len([r for r in fake.requests if endpoint in r.url.path]) == 1
    assert gh.sleeps == []


def test_plain_403_without_rate_limit_headers_is_a_permission_block(gh, capsys) -> None:
    fake = FakeGitHub(repo_status=403)
    rc, _ = run(gh, capsys, fake)
    assert rc != 0 and len(fake.requests) == 1  # no pointless retry of a permission error


# ---- hardening round: oversized bodies, label list too long, control chars in --repo ---------

def test_oversized_response_body_is_not_accepted(gh, capsys) -> None:
    pad = b"x" * 20_000_000  # a real repo JSON is ~5 KB; anything this big must be refused

    def big(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.rstrip("/") == "/repos/owner/repo":
            body = (b'{"full_name":"owner/repo","has_issues":true,"archived":false,'
                    b'"permissions":{"push":true},"pad":"' + pad + b'"}')
            return httpx.Response(200, content=body)
        return None

    rc, out = run(gh, capsys, FakeGitHub(override=big))
    assert rc != 0 and out.strip(), "20 MB repo response must be BLOCKed (response size cap)"


def test_oversized_label_page_is_not_accepted(gh, capsys) -> None:
    def big(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/labels"):
            items = [{"id": 1, "name": "backend", "pad": "x" * 20_000_000}]
            return httpx.Response(200, content=json.dumps(items))
        return None

    md = "## TASK-001: Một\n- Phụ trách: TV01\n- Labels: backend\n"
    rc, _ = run(gh, capsys, FakeGitHub(override=big), md=md)
    assert rc != 0


def test_label_list_longer_than_the_page_budget_is_never_treated_as_complete(gh, capsys) -> None:
    many = [f"label-{i:04d}" for i in range(1500)]
    md = "## TASK-001: Một\n- Phụ trách: TV01\n- Labels: label-9999\n"
    rc, out = run(gh, capsys, FakeGitHub(labels=many), md=md)
    assert rc != 0 and out.strip()


def test_control_characters_in_repo_arg_are_not_echoed_raw(gh, capsys) -> None:
    rc, out = run(gh, capsys, args=["--check-github", "--repo", "owner/repo\x1b[31mX\r"])
    assert rc != 0
    assert "\x1b" not in out and "\r" not in out, repr(out)
    assert gh.fake.requests == []


# ---- M3.0: response size cap (~2MB), also without Content-Length; bidi escaping --------------

def _stream(total: int, chunk: int = 65536):  # type: ignore[no-untyped-def]
    """Body with NO Content-Length (chunked); a client that reads it all consumes `total` bytes."""
    sent = {"n": 0}

    def gen():  # type: ignore[no-untyped-def]
        while sent["n"] < total:
            sent["n"] += chunk
            yield b" " * chunk

    return gen(), sent


def test_chunked_repo_response_without_content_length_is_capped(gh, capsys) -> None:
    def big(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.rstrip("/") == "/repos/owner/repo":
            body, _ = _stream(50_000_000)
            return httpx.Response(200, content=body)
        return None

    rc, out = run(gh, capsys, FakeGitHub(override=big))
    assert rc != 0 and out.strip()


def test_oversized_assignee_response_is_blocked(gh, capsys) -> None:
    def big(req: httpx.Request) -> httpx.Response | None:
        if "/assignees/" in req.url.path:
            return httpx.Response(204, content=b"x" * 20_000_000)
        return None

    rc, out = run(gh, capsys, FakeGitHub(override=big))
    assert rc != 0 and out.strip()


def test_oversized_label_page_without_content_length_is_capped(gh, capsys) -> None:
    def big(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/labels"):
            body, _ = _stream(50_000_000)
            return httpx.Response(200, content=body)
        return None

    md = "## TASK-001: Một\n- Phụ trách: TV01\n- Labels: backend\n"
    rc, _ = run(gh, capsys, FakeGitHub(override=big), md=md)
    assert rc != 0


def test_size_cap_stops_reading_early_not_after_the_whole_body(gh, capsys) -> None:
    seen = {}

    def big(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.rstrip("/") == "/repos/owner/repo":
            body, sent = _stream(200_000_000)
            seen["sent"] = sent
            return httpx.Response(200, content=body)
        return None

    rc, _ = run(gh, capsys, FakeGitHub(override=big))
    assert rc != 0
    assert seen["sent"]["n"] < 20_000_000, "client kept reading far past the cap"


def test_normal_sized_responses_still_pass_after_the_cap(gh, capsys) -> None:
    rc, out = run(gh, capsys, FakeGitHub(labels=[*(f"l{i}" for i in range(250)), "backend", "auth", "database"]))
    assert rc == 0, out


BIDI = [chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), *range(0x200B, 0x2010))]


@pytest.mark.parametrize("ch", BIDI, ids=[f"U+{ord(c):04X}" for c in BIDI])
def test_bidi_and_zero_width_chars_are_escaped_in_output(gh, capsys, ch) -> None:
    md = f"| Mã | Công việc |\n|---|---|\n| T{ch}1 | a{ch} |\n## TASK-009: x{ch}\n"
    rc, out = run(gh, capsys, md=md, args=[])
    assert rc != 0
    assert ch not in out, f"U+{ord(ch):04X} reached the terminal unescaped"


def test_bidi_in_repo_arg_and_members_error_is_escaped(gh, capsys) -> None:
    rc, out = run(gh, capsys, args=["--check-github", "--repo", "owner/re\u202epo"])
    assert rc != 0 and "\u202e" not in out
    rc, out = run(gh, capsys, members="members:\n  TV01: {display_name: \"A\u202e\", github: bad login}\n", args=[])
    assert rc != 0 and "\u202e" not in out


def test_exact_cap_passes_and_cap_plus_one_blocks_on_client() -> None:
    import json as _json

    from github_bot.github.client import MAX_RESPONSE_BYTES, GitHubClient, GitHubError

    def body(total: int) -> bytes:
        d = {"full_name": "owner/repo", "has_issues": True, "archived": False, "pad": ""}
        d["pad"] = "x" * (total - len(_json.dumps(d, separators=(",", ":"))))
        return _json.dumps(d, separators=(",", ":")).encode()

    def client(n: int) -> GitHubClient:
        return GitHubClient(
            token=TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body(n)))
        )

    assert client(MAX_RESPONSE_BYTES).get_repo("owner", "repo")
    with pytest.raises(GitHubError):
        client(MAX_RESPONSE_BYTES + 1).get_repo("owner", "repo")


def test_client_asks_for_identity_encoding_to_avoid_decompression_bombs(gh, capsys) -> None:
    rc, _ = run(gh, capsys)
    assert rc == 0
    assert gh.fake.requests
    assert all(r.headers.get("accept-encoding", "").strip().lower() == "identity" for r in gh.fake.requests)
