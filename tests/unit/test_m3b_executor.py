"""M3b black-box tests: `issues apply` (first WRITE path) against a fake GitHub with an issue store.

Contract assumed (flag to leader if coder differs; adapter constants/functions at the top):
  github-bot issues apply --plan plan.json --approve-hash H --write [--repo owner/repo] [--workspace W]
  - plan.json comes from `issues plan` (same workspace). Needs env GITHUB_TOKEN.
  - REFUSE (exit != 0, ZERO POST, clear message) when: write flag missing; hash missing/wrong (must equal
    plan_sha256 AND the recomputed sha256 of the plan content); plan.json tampered in any way / not JSON /
    wrong shape; ANY item is BLOCK; unknown action; CREATE item whose body does not end with its own
    marker; > 50 items; --repo given and != plan.repo; plan path outside workspace; token missing;
    plan stale (an issue with the key of a CREATE item already exists) or repo/label/assignee no
    longer valid for an item.
  - Exactly ONE kind of write: POST /repos/{o}/{r}/issues {title, body, labels, assignees}. Never
    PATCH/PUT/DELETE, no state change, no comments.
  - Before EACH POST: re-read issues (state=all) and re-check the marker; if now present -> no POST for
    that item (reported), continue with the others.
  - POST 422 / failure of one item never stops the others; each item gets a report line; exit != 0 if
    any item failed. POST timeout/5xx: never a blind re-POST (re-read first); 429: bounded retries after
    re-reading, sleeps <= 60s.
  - After create, compare actual assignees with requested; mismatch -> error naming task+login (GitHub drops
    assignees silently); issue is NOT deleted/edited.
  - Audit log: JSON lines file (*.jsonl, written inside the workspace) with task_id, action, issue_number/url,
    assignees, timestamp, error; never token/headers/raw body.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_github import TOKEN, FakeGitHub, assert_requests_safe, make_issue

from github_bot.cli import main

WRITE_FLAG = "--write"  # coder picks the final name; change only here
MEMBERS = """members:
  TV01: {display_name: "Nguyễn Văn A", github: "example-dev-1", aliases: ["Anh A"]}
  TV02: {display_name: "Trần Văn B", github: "example-dev-2"}
  TV03: {display_name: "Lê Văn C", github: "example-dev-3"}
"""
TASKS5 = "\n".join(
    f"## TASK-{n:03d}: Việc {n}\n- Phụ trách: {a}\n- Labels: backend\n\n### Mô tả\nMô tả số {n}.\n\n"
    f"### Tiêu chí nghiệm thu\n- [ ] Xong {n}\n"
    for n, a in ((1, "TV01"), (2, "TV02"), (3, "TV03"), (4, "TV01, TV02"), (5, "TV03"))
)


def canon_hash(plan: dict[str, Any]) -> str:
    body = {k: v for k, v in plan.items() if k != "plan_sha256"}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    import time

    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.chdir(w)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    st = SimpleNamespace(ws=w, tmp=tmp_path, fake=FakeGitHub(), history=[], sleeps=[])
    st.history.append(st.fake)
    orig_init = httpx.Client.__init__

    def patched(self: httpx.Client, *a: Any, **k: Any) -> None:
        k["transport"] = httpx.MockTransport(lambda req: st.fake(req))
        orig_init(self, *a, **k)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    monkeypatch.setattr(time, "sleep", lambda s: st.sleeps.append(float(s)))
    yield st
    for f in st.history:
        assert_requests_safe(f, allow_post=True)
        bad = [r for r in f.requests if r.method not in ("GET", "POST")]
        assert not bad, f"forbidden write verb(s): {[(r.method, str(r.url)) for r in bad]}"
        for r in f.posts:
            assert set(json.loads(r.content)) <= {"title", "body", "labels", "assignees"}, r.content
    assert max(st.sleeps, default=0) <= 60


def _cli(env: Any, capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str]:
    try:
        rc = main(argv)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert TOKEN not in out and "Traceback" not in out, out
    return int(rc or 0), out


def make_plan(
    env: Any, capsys: pytest.CaptureFixture[str], md: str = TASKS5, fake: FakeGitHub | None = None
) -> dict[str, Any]:
    if fake is not None:
        env.fake = fake
        env.history.append(fake)
    (env.ws / "TASKS.md").write_text(md, encoding="utf-8")
    (env.ws / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    rc, out = _cli(env, capsys, ["issues", "plan", "--input", "TASKS.md", "--members", "members.yaml",
                                 "--repo", "owner/repo", "--output-dir", "out"])  # fmt: skip
    if "invalid choice" in out:
        pytest.fail(f"`issues plan` contract not available: {out[:200]}")
    assert rc == 0, out
    return json.loads((env.ws / "out" / "plan.json").read_text(encoding="utf-8"))


def apply(
    env: Any, capsys: pytest.CaptureFixture[str], plan: dict[str, Any] | None = None, *,
    h: str | None = None, write: bool = True, plan_path: str = "out/plan.json", extra: list[str] | None = None,
) -> tuple[int, str]:
    pj = env.ws / "out" / "plan.json"
    plan = json.loads(pj.read_text(encoding="utf-8")) if plan is None else plan
    argv = ["issues", "apply", "--plan", plan_path]
    argv += ["--approve-hash", plan.get("plan_sha256", "") if h is None else h]
    if write:
        argv.append(WRITE_FLAG)
    rc, out = _cli(env, capsys, argv + (extra or []))
    if "invalid choice" in out and "apply" in out:
        pytest.fail(f"`issues apply` CLI contract not available: {out.strip()[:200]}")
    return rc, out


def write_plan(env: Any, plan: dict[str, Any], rehash: bool = False, name: str = "out/plan.json") -> None:
    if rehash:
        plan["plan_sha256"] = canon_hash(plan)
    p = env.ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def no_posts(env: Any) -> None:
    assert env.fake.posts == [], f"unexpected POST(s): {len(env.fake.posts)}"


def markers_in_store(fake: FakeGitHub) -> list[str]:
    return [m for i in fake.issues for m in re.findall(r"task_id=([A-Za-z0-9-]+)", str(i["body"]))]


# ---- acceptance (§6 M3) -----------------------------------------------------------------------------

def test_five_tasks_create_exactly_five_issues_then_rerun_creates_none(env, capsys) -> None:
    plan = make_plan(env, capsys)
    assert [i["action"] for i in plan["items"]] == ["CREATE"] * 5
    rc, out = apply(env, capsys)
    assert rc == 0, out
    assert len(env.fake.posts) == 5 and len(env.fake.issues) == 5
    assert sorted(markers_in_store(env.fake)) == [f"TASK-{n:03d}" for n in range(1, 6)]
    # second apply of the SAME approved plan: stale / all-skip, but never a new issue
    # leader decision: the second apply of the same plan is REFUSED as a stale plan (same-key issues exist)
    rc2, out2 = apply(env, capsys)
    assert rc2 != 0 and out2.strip(), out2
    assert len(env.fake.posts) == 5 and len(env.fake.issues) == 5, out2
    assert "plan" in out2.lower()  # tells the operator to run `issues plan` again
    # a fresh plan now says SKIP for everything and applies nothing
    plan2 = make_plan(env, capsys, fake=env.fake)
    assert [i["action"] for i in plan2["items"]] == ["SKIP"] * 5
    _, _ = apply(env, capsys)
    assert len(env.fake.posts) == 5


def test_posts_carry_exactly_the_planned_fields(env, capsys) -> None:
    plan = make_plan(env, capsys)
    rc, out = apply(env, capsys)
    assert rc == 0, out
    by_title = {i["title"]: i for i in plan["items"]}
    for r in env.fake.posts:
        pl = json.loads(r.content)
        want = by_title[pl["title"]]
        assert pl["body"] == want["body"], "body must be byte-identical to the approved plan"
        assert pl["assignees"] == want["assignees"] and pl["labels"] == want["labels"]
        assert r.url.path == "/repos/owner/repo/issues" and r.method == "POST"
        assert r.headers["authorization"].endswith(TOKEN)
    got = {i["title"]: [a["login"] for a in i["assignees"]] for i in env.fake.issues}  # type: ignore[index,union-attr]
    assert got["Việc 4"] == ["example-dev-1", "example-dev-2"]


def test_apply_without_a_fresh_plan_never_edits_or_closes_existing_issues(env, capsys) -> None:
    existing = make_issue(1, "human issue", state="closed", title="Việc 1")
    make_plan(env, capsys, fake=FakeGitHub(issues=[existing]))
    apply(env, capsys)
    assert env.fake.issues[0]["state"] == "closed" and env.fake.issues[0]["body"] == "human issue"
    assert {r.method for r in env.fake.requests} <= {"GET", "POST"}


# ---- gates: every refusal = 0 POST ----------------------------------------------------------------------

def test_missing_write_flag_refuses(env, capsys) -> None:
    make_plan(env, capsys)
    rc, out = apply(env, capsys, write=False)
    assert rc != 0 and out.strip()
    no_posts(env)


def test_missing_approve_hash_refuses_without_any_request(env, capsys) -> None:
    make_plan(env, capsys)
    before = len(env.fake.requests)
    rc, _ = _cli(env, capsys, ["issues", "apply", "--plan", "out/plan.json", WRITE_FLAG])
    assert rc != 0 and len(env.fake.requests) == before


def test_correct_hash_but_no_write_flag_refuses(env, capsys) -> None:
    plan = make_plan(env, capsys)
    rc, _ = apply(env, capsys, h=plan["plan_sha256"], write=False)
    assert rc != 0
    no_posts(env)


@pytest.mark.parametrize(
    "mut",
    [lambda h: "", lambda h: "0" * 64, lambda h: h[:-1] + ("0" if h[-1] != "0" else "1"),
     lambda h: h[:32], lambda h: h + "0", lambda h: "sha256:" + h, lambda h: "g" * 64, lambda h: h[::-1]],
    ids=["empty", "zeros", "last_nibble", "prefix", "extra_char", "prefixed", "not_hex", "reversed"],
)
def test_wrong_hash_refuses(env, capsys, mut) -> None:
    plan = make_plan(env, capsys)
    rc, out = apply(env, capsys, h=mut(plan["plan_sha256"]))
    assert rc != 0 and out.strip()
    no_posts(env)


def _tamper(plan: dict[str, Any], what: str) -> None:
    it = plan["items"]
    if what == "title":
        it[0]["title"] += "x"
    elif what == "body_char":
        it[1]["body"] = it[1]["body"].replace("Mô tả số 2.", "Mô tả số 3.")
    elif what == "assignee":
        it[0]["assignees"] = ["example-dev-3"]
    elif what == "label":
        it[0]["labels"] = ["auth"]
    elif what == "drop_item":
        it.pop()
    elif what == "add_item":
        extra = copy.deepcopy(it[0])
        extra["task_id"] = "TASK-099"
        it.append(extra)
    elif what == "reorder":
        it.reverse()
    elif what == "repo":
        plan["repo"] = "evil/other"
    elif what == "source":
        plan["source_file"] = "OTHER.md"
    elif what == "action":
        it[0]["action"] = "SKIP"
    elif what == "reasons":
        it[0]["reasons"] = ["x"]


@pytest.mark.parametrize(
    "what",
    ["title", "body_char", "assignee", "label", "drop_item", "add_item", "reorder", "repo", "source",
     "action", "reasons"],
)
def test_tampered_plan_with_the_approved_hash_refuses(env, capsys, what) -> None:
    plan = make_plan(env, capsys)
    approved = plan["plan_sha256"]
    _tamper(plan, what)
    write_plan(env, plan)  # plan_sha256 field left as is
    rc, out = apply(env, capsys, h=approved)
    assert rc != 0 and out.strip(), what
    no_posts(env)


def test_one_character_edit_in_the_raw_file_refuses(env, capsys) -> None:
    plan = make_plan(env, capsys)
    pj = env.ws / "out" / "plan.json"
    raw = pj.read_text(encoding="utf-8")
    i = raw.index("Mô tả số 1.")
    pj.write_text(raw[:i] + "Mô tả số 7." + raw[i + len("Mô tả số 1.") :], encoding="utf-8")
    rc, _ = apply(env, capsys, h=plan["plan_sha256"])
    assert rc != 0
    no_posts(env)


def test_tampered_plan_with_self_updated_hash_still_needs_the_operator_to_approve_that_hash(env, capsys) -> None:
    plan = make_plan(env, capsys)
    approved = plan["plan_sha256"]
    _tamper(plan, "title")
    write_plan(env, plan, rehash=True)  # attacker also fixes the embedded hash
    rc, _ = apply(env, capsys, h=approved)
    assert rc != 0
    no_posts(env)


def test_plan_with_a_block_item_refuses_even_with_matching_hash(env, capsys) -> None:
    (env.ws / "TASKS.md").write_text(TASKS5 + "\n## TASK-006: Sáu\n- Phụ trách: TV99\n", encoding="utf-8")
    (env.ws / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    rc, out = _cli(env, capsys, ["issues", "plan", "--input", "TASKS.md", "--members", "members.yaml",
                                 "--repo", "owner/repo", "--output-dir", "out"])  # fmt: skip
    assert rc != 0
    plan = json.loads((env.ws / "out" / "plan.json").read_text(encoding="utf-8"))
    assert any(i["action"] == "BLOCK" for i in plan["items"])
    rc, out = apply(env, capsys)  # hash is genuine and the write flag is given
    assert rc != 0 and out.strip()
    no_posts(env)  # all-or-nothing: the five good tasks are NOT created either


@pytest.mark.parametrize("raw", ["", "not json{", "[]", "null", "42", '"s"', "{}", '{"items": []}',
                                 '{"repo": "owner/repo", "items": "x", "plan_sha256": "a"}'])
def test_plan_file_that_is_not_a_valid_plan_refuses(env, capsys, raw) -> None:
    make_plan(env, capsys)
    (env.ws / "out" / "plan.json").write_text(raw, encoding="utf-8")
    rc, out = apply(env, capsys, plan={"plan_sha256": "a" * 64}, h="a" * 64)
    assert rc != 0 and out.strip() and "Traceback" not in out
    no_posts(env)


def test_missing_plan_file_refuses_cleanly(env, capsys) -> None:
    rc, out = apply(env, capsys, plan={"plan_sha256": "a" * 64}, h="a" * 64, plan_path="nope.json")
    assert rc != 0 and out.strip()
    no_posts(env)


@pytest.mark.parametrize("which", ["abs", "dotdot", "symlink"])
def test_plan_path_outside_workspace_refuses(env, capsys, which) -> None:
    plan = make_plan(env, capsys)
    outside = env.tmp / "outside_plan.json"
    outside.write_text((env.ws / "out" / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
    if which == "symlink":
        (env.ws / "link.json").symlink_to(outside)
    path = {"abs": str(outside), "dotdot": "../outside_plan.json", "symlink": "link.json"}[which]
    rc, _ = apply(env, capsys, h=plan["plan_sha256"], plan_path=path)
    assert rc != 0
    no_posts(env)


@pytest.mark.parametrize("bad_action", ["UPDATE", "DELETE", "create", "", "CLOSE", None])
def test_unknown_or_non_create_actions_in_a_self_consistent_plan_refuse(env, capsys, bad_action) -> None:
    plan = make_plan(env, capsys)
    plan["items"][0]["action"] = bad_action
    write_plan(env, plan, rehash=True)
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


@pytest.mark.parametrize("what", ["no_marker", "other_task_marker", "marker_not_last", "empty_body", "no_title"])
def test_forged_self_consistent_plan_with_a_bad_body_refuses(env, capsys, what) -> None:
    plan = make_plan(env, capsys)
    it = plan["items"][0]
    if what == "no_marker":
        it["body"] = "just text"
    elif what == "other_task_marker":
        it["body"] = it["body"].replace("task_id=TASK-001", "task_id=TASK-002")
    elif what == "marker_not_last":
        it["body"] += "\nextra text after the marker\n"
    elif what == "empty_body":
        it["body"] = ""
    else:
        it["title"] = "  "
    write_plan(env, plan, rehash=True)
    rc, out = apply(env, capsys)
    assert rc != 0 and out.strip()
    no_posts(env)


def test_plan_above_max_items_refuses_even_if_self_consistent(env, capsys) -> None:
    plan = make_plan(env, capsys)
    first = plan["items"][0]
    plan["items"] = []
    for n in range(1, 61):
        it = copy.deepcopy(first)
        it["task_id"] = f"TASK-{n:03d}"
        it["title"] = f"t{n}"
        it["body"] = it["body"].replace("task_id=TASK-001", f"task_id=TASK-{n:03d}")
        plan["items"].append(it)
    write_plan(env, plan, rehash=True)
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


def test_repo_flag_must_match_the_plan(env, capsys) -> None:
    make_plan(env, capsys)
    rc, out = apply(env, capsys, extra=["--repo", "owner/other"])
    assert rc != 0 and out.strip()
    no_posts(env)


def test_missing_token_refuses_without_requests(env, capsys, monkeypatch) -> None:
    make_plan(env, capsys)
    monkeypatch.delenv("GITHUB_TOKEN")
    before = len(env.fake.requests)
    rc, out = apply(env, capsys)
    assert rc != 0 and "GITHUB_TOKEN" in out and len(env.fake.requests) == before


def test_token_cannot_be_passed_on_the_apply_command_line(env, capsys) -> None:
    make_plan(env, capsys)
    rc, out = apply(env, capsys, extra=["--token", "ghp_cli_secret"])
    assert rc != 0 and "ghp_cli_secret" not in out
    no_posts(env)


# ---- stale plan / revalidation -------------------------------------------------------------------------------

def test_stale_plan_because_an_issue_with_the_key_appeared_refuses_without_posting(env, capsys) -> None:
    make_plan(env, capsys)
    env.fake.issues.append(make_issue(50, "x\n\n<!-- github-automation-bot: source=TASKS.md; task_id=TASK-003; schema=v1 -->\n"))
    rc, out = apply(env, capsys)
    assert rc != 0 and out.strip()
    no_posts(env)


def test_label_deleted_after_plan_means_no_post_for_that_task(env, capsys) -> None:
    make_plan(env, capsys)
    env.fake.labels = ["auth"]  # 'backend' (used by every task) vanished
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


def test_assignee_no_longer_eligible_means_no_post_for_that_task(env, capsys) -> None:
    make_plan(env, capsys)
    env.fake.assignees.discard("example-dev-3")
    rc, out = apply(env, capsys)
    assert rc != 0 and "example-dev-3" in out
    titles = {json.loads(r.content)["title"] for r in env.fake.posts}
    assert "Việc 3" not in titles and "Việc 5" not in titles


def test_repo_lost_push_permission_after_plan_refuses(env, capsys) -> None:
    make_plan(env, capsys)
    env.fake.permissions = {"pull": True}
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


# ---- races / idempotency ------------------------------------------------------------------------------------

def test_concurrent_apply_creating_a_task_mid_run_never_duplicates(env, capsys) -> None:
    def other_runner(fake: FakeGitHub, n: int) -> None:
        if n == 1:  # right after we created the first issue, "another apply" creates TASK-002
            fake.issues.append(make_issue(
                900, "## Mô tả\nx\n\n<!-- github-automation-bot: source=TASKS.md; task_id=TASK-002; schema=v1 -->\n"))  # fmt: skip

    make_plan(env, capsys)
    env.fake.after_post = other_runner
    _, out = apply(env, capsys)
    ids = markers_in_store(env.fake)
    assert ids.count("TASK-002") == 1, f"duplicate TASK-002: {ids}"
    assert all(ids.count(t) == 1 for t in ("TASK-001", "TASK-003", "TASK-004", "TASK-005"))
    assert "TASK-002" in out


def test_post_timeout_after_server_created_the_issue_is_not_posted_again(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 2:
            fake.create(json.loads(req.content))  # the server DID create it
            raise httpx.ReadTimeout("response lost")
        return None

    make_plan(env, capsys)
    env.fake.on_post = hook
    _, _ = apply(env, capsys)
    ids = markers_in_store(env.fake)
    assert sorted(ids) == [f"TASK-{n:03d}" for n in range(1, 6)], ids
    assert len(env.fake.posts) == 5, "blind re-POST after an ambiguous timeout"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_on_post_is_not_blindly_retried(env, capsys, status) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 3:
            return httpx.Response(status, json={"message": "boom"})
        return None

    make_plan(env, capsys)
    env.fake.on_post = hook
    rc, out = apply(env, capsys)
    ids = markers_in_store(env.fake)
    assert all(ids.count(t) <= 1 for t in set(ids)), ids
    assert len(env.fake.posts) <= 6, "unbounded retries"
    assert "TASK-003" in out and rc != 0
    for t in ("TASK-001", "TASK-002", "TASK-004", "TASK-005"):
        assert t in ids, f"{t} lost because another task failed"


def test_422_on_one_task_does_not_lose_the_others_and_is_reported(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 2:
            return httpx.Response(422, json={"message": "Validation Failed", "errors": [{"field": "assignees"}]})
        return None

    make_plan(env, capsys)
    env.fake.on_post = hook
    rc, out = apply(env, capsys)
    ids = markers_in_store(env.fake)
    assert rc != 0
    assert sorted(ids) == ["TASK-001", "TASK-003", "TASK-004", "TASK-005"]
    assert len(env.fake.posts) == 5, "422 must not be retried and must not stop the run"
    assert "TASK-002" in out and "422" in out
    for t in ("TASK-001", "TASK-003", "TASK-004", "TASK-005"):
        assert t in out, f"no report line for {t}"
    # partial failure is recoverable: re-plan shows only the failed task as CREATE
    env.fake.on_post = None
    plan2 = make_plan(env, capsys, fake=env.fake)
    acts = {i["task_id"]: i["action"] for i in plan2["items"]}
    assert acts["TASK-002"] == "CREATE" and [a for t, a in acts.items() if t != "TASK-002"] == ["SKIP"] * 4
    rc2, _ = apply(env, capsys)
    assert rc2 == 0 and sorted(markers_in_store(env.fake)) == [f"TASK-{n:03d}" for n in range(1, 6)]


def test_rate_limit_on_post_is_bounded_and_never_sleeps_long(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n >= 2:
            return httpx.Response(429, headers={"Retry-After": "999999"}, json={"message": "slow"})
        return None

    make_plan(env, capsys)
    env.fake.on_post = hook
    rc, _ = apply(env, capsys)
    assert rc != 0
    assert len(env.fake.posts) <= 1 + 3 * 4, f"{len(env.fake.posts)} POSTs under a permanent 429"
    assert max(env.sleeps, default=0) <= 60
    assert markers_in_store(env.fake).count("TASK-001") == 1


def test_post_redirect_to_another_host_is_not_followed_and_gets_no_token(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        return httpx.Response(307, headers={"Location": "https://evil.example/steal"})

    make_plan(env, capsys)
    env.fake.on_post = hook
    rc, _ = apply(env, capsys)
    assert rc != 0 and env.fake.foreign == [] and markers_in_store(env.fake) == []


# ---- assignee reconciliation -----------------------------------------------------------------------------------

def test_assignee_silently_dropped_by_github_is_reported_loudly(env, capsys) -> None:
    make_plan(env, capsys)
    env.fake.drop_assignees = {"example-dev-2"}
    rc, out = apply(env, capsys)
    assert rc != 0
    assert "example-dev-2" in out and ("TASK-002" in out or "TASK-004" in out)
    assert len(env.fake.issues) == 5, "issues stay created (never deleted or edited)"
    assert {r.method for r in env.fake.requests} <= {"GET", "POST"}


# ---- audit log -------------------------------------------------------------------------------------------------

def _audit(env: Any) -> list[dict[str, Any]]:
    files = sorted(env.ws.rglob("*.jsonl"))
    assert files, "no JSON-lines audit log written inside the workspace"
    plan = json.loads((env.ws / "out" / "plan.json").read_text(encoding="utf-8"))
    expected = env.ws / "out" / f"audit-{plan['plan_sha256'][:8]}.jsonl"  # leader decision
    assert files == [expected], f"audit log must be exactly {expected.name} next to plan.json, got {files}"
    rows = []
    for f in files:
        for ln in f.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                rows.append(json.loads(ln))
    return rows


def test_audit_log_records_every_task_without_secrets(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 2:
            return httpx.Response(422, json={"message": f"bad {TOKEN} Bearer {TOKEN}", "documentation_url": "x"})
        return None

    make_plan(env, capsys)
    env.fake.on_post = hook
    apply(env, capsys)
    rows = _audit(env)
    ids = [r["task_id"] for r in rows]
    assert sorted(set(ids)) == [f"TASK-{n:03d}" for n in range(1, 6)]
    ok = [r for r in rows if r["task_id"] == "TASK-001"][-1]
    assert ok.get("issue_number") and ok.get("action") and ok.get("timestamp")
    assert "example-dev-1" in json.dumps(ok)
    bad = [r for r in rows if r["task_id"] == "TASK-002"][-1]
    assert bad.get("error") or bad.get("status") in {"FAILED", "ERROR"} or "fail" in json.dumps(bad).lower()
    blob = "\n".join(f.read_text(encoding="utf-8") for f in env.ws.rglob("*.jsonl")).lower()
    assert TOKEN.lower() not in blob and "authorization" not in blob and "bearer" not in blob
    assert "mô tả số" not in blob, "raw issue body leaked into the audit log"
    assert "documentation_url" not in blob, "raw response body leaked into the audit log"


def test_refusals_never_log_a_created_issue(env, capsys) -> None:
    plan = make_plan(env, capsys)
    apply(env, capsys, h="0" * 64)
    for f in env.ws.rglob("*.jsonl"):
        for ln in f.read_text(encoding="utf-8").splitlines():
            assert "issue_number" not in ln or json.loads(ln).get("issue_number") in (None, 0)
    no_posts(env)
    assert plan


# ---- static guards on the source tree ------------------------------------------------------------------------------

SRC = Path(__file__).resolve().parents[2] / "src" / "github_bot"


def _py() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_only_one_module_may_issue_post_and_nobody_may_patch_put_delete() -> None:
    post_rx = re.compile(r"""\.post\s*\(|["']POST["']|method\s*=\s*["']POST""")
    other_rx = re.compile(r"""\.(put|patch|delete)\s*\(|["'](PUT|PATCH|DELETE)["']""")
    posters = []
    for f in _py():
        text = "\n".join(ln.split("#", 1)[0] for ln in f.read_text(encoding="utf-8").splitlines())
        assert not other_rx.search(text), f"{f.name}: PUT/PATCH/DELETE"
        if post_rx.search(text):
            posters.append(f.relative_to(SRC).as_posix())
    assert len(posters) == 1, posters
    assert posters[0].startswith("execution/"), posters
    assert "client" not in posters[0] and "planner" not in posters[0] and "validator" not in posters[0]


def test_the_read_only_client_class_still_has_no_write_methods() -> None:
    from github_bot.github.client import GitHubClient

    bad = [n for n in dir(GitHubClient) if n.lower().startswith(("post", "put", "patch", "delete", "create", "update", "reopen", "comment", "close_issue", "assign"))]
    assert not bad, bad


def test_write_module_never_follows_redirects_or_disables_tls() -> None:
    text = "\n".join(f.read_text(encoding="utf-8") for f in _py())
    assert "follow_redirects=True" not in text and "verify=False" not in text


def test_audit_log_is_appended_not_truncated_and_refusals_keep_it(env, capsys) -> None:
    plan = make_plan(env, capsys)
    rc, out = apply(env, capsys)
    assert rc == 0, out
    log = env.ws / "out" / f"audit-{plan['plan_sha256'][:8]}.jsonl"
    before = log.read_text(encoding="utf-8")
    assert before.count("\n") >= 5
    rc2, _ = apply(env, capsys)  # refused as stale; must not truncate or rewrite history
    assert rc2 != 0
    after = log.read_text(encoding="utf-8")
    assert after.startswith(before), "audit history was truncated/rewritten"


# ---- round 2: self-critique probes (first real write path) ---------------------------------------------

def test_audit_path_that_cannot_be_written_is_refused_before_any_post(env, capsys) -> None:
    plan = make_plan(env, capsys)
    victim = env.tmp / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    (env.ws / "out" / f"audit-{plan['plan_sha256'][:8]}.jsonl").symlink_to(victim)
    rc, out = apply(env, capsys)
    assert rc != 0 and out.strip()
    assert victim.read_text(encoding="utf-8") == "precious"
    no_posts(env)  # refusing AFTER creating issues would leave them unlogged and mislabelled "refused"


def test_audit_path_being_a_directory_is_refused_before_any_post(env, capsys) -> None:
    plan = make_plan(env, capsys)
    (env.ws / "out" / f"audit-{plan['plan_sha256'][:8]}.jsonl").mkdir()
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


def test_unexpected_crash_mid_run_still_reports_and_logs_the_issues_already_created(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 3:
            raise RuntimeError("unexpected bug in transport/handler")
        return None

    plan = make_plan(env, capsys)
    env.fake.on_post = hook
    try:
        rc, out = apply(env, capsys)
    except RuntimeError:
        rc, out = 99, "RuntimeError escaped"
    created = sorted(set(markers_in_store(env.fake)))
    assert created, "setup: some issues were created before the crash"
    log = env.ws / "out" / f"audit-{plan['plan_sha256'][:8]}.jsonl"
    logged = {json.loads(ln)["task_id"] for ln in log.read_text(encoding="utf-8").splitlines()} if log.is_file() else set()
    assert set(created) <= logged, f"created {created} but audit only has {sorted(logged)} (audit must be flushed per task)"
    assert rc not in (0, 99) and "Traceback" not in out


@pytest.mark.parametrize("task_id", ["bad id", "TASK-001\n", "x" * 100, "ünï-1", "T-1; drop"])
def test_self_consistent_plan_with_malformed_task_id_is_refused_cleanly(env, capsys, task_id) -> None:
    plan = make_plan(env, capsys)
    it = plan["items"][0]
    old = it["task_id"]
    it["task_id"] = task_id
    it["body"] = it["body"].replace(f"task_id={old}", f"task_id={task_id}")
    write_plan(env, plan, rehash=True)
    rc, out = apply(env, capsys)
    assert rc != 0 and "Traceback" not in out
    no_posts(env)


@pytest.mark.parametrize("field", ["title"])
@pytest.mark.parametrize("bad", ["a\u202eb", "a\u200bb", "x\u2066y"])
def test_bidi_or_zero_width_in_a_self_consistent_plan_is_refused(env, capsys, field, bad) -> None:
    plan = make_plan(env, capsys)
    if field == "title":
        plan["items"][0]["title"] = "Việc " + bad
    else:
        plan["items"][0]["labels"] = [bad]
    write_plan(env, plan, rehash=True)
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


def test_second_marker_in_a_valid_looking_body_is_refused(env, capsys) -> None:
    plan = make_plan(env, capsys)
    it = plan["items"][0]
    it["body"] = it["body"].replace("## Mô tả", "## Mô tả\n<!-- github-automation-bot: source=TASKS.md; task_id=TASK-002; schema=v1 -->", 1)
    write_plan(env, plan, rehash=True)
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


def test_marker_of_another_task_on_the_last_line_is_refused(env, capsys) -> None:
    plan = make_plan(env, capsys)
    it = plan["items"][0]
    it["body"] = it["body"].replace("task_id=TASK-001", "task_id=TASK-002")
    write_plan(env, plan, rehash=True)
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


@pytest.mark.parametrize("variant", ["upper", "padded", "newline", "prefix_0x", "short"])
def test_approve_hash_must_be_exactly_the_lowercase_hex_digest(env, capsys, variant) -> None:
    plan = make_plan(env, capsys)
    h = plan["plan_sha256"]
    bad = {"upper": h.upper(), "padded": f" {h} ", "newline": h + "\n", "prefix_0x": "0x" + h, "short": h[:63]}[variant]
    rc, _ = apply(env, capsys, h=bad)
    assert rc != 0
    no_posts(env)


def test_skip_only_plan_posts_nothing_and_succeeds(env, capsys) -> None:
    make_plan(env, capsys)
    assert apply(env, capsys)[0] == 0
    plan2 = make_plan(env, capsys, fake=env.fake)
    assert {i["action"] for i in plan2["items"]} == {"SKIP"}
    before = len(env.fake.posts)
    rc, out = apply(env, capsys)
    assert len(env.fake.posts) == before and rc in (0, 2), out


def test_mixed_skip_and_create_plan_creates_only_the_missing_ones(env, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(1, "x\n\n<!-- github-automation-bot: source=TASKS.md; task_id=TASK-001; schema=v1 -->\n")])
    plan = make_plan(env, capsys, fake=fake)
    assert [i["action"] for i in plan["items"]].count("SKIP") == 1
    rc, out = apply(env, capsys)
    assert rc == 0, out
    assert len(env.fake.posts) == 4 and markers_in_store(env.fake).count("TASK-001") == 1


@pytest.mark.parametrize("val", [None, "x", 3])
def test_null_or_wrong_type_labels_and_assignees_in_plan_are_refused(env, capsys, val) -> None:
    plan = make_plan(env, capsys)
    plan["items"][0]["labels"] = val
    plan["items"][1]["assignees"] = val
    write_plan(env, plan, rehash=True)
    rc, out = apply(env, capsys)
    assert rc != 0 and "Traceback" not in out
    no_posts(env)


def test_unknown_extra_fields_in_plan_are_refused(env, capsys) -> None:
    plan = make_plan(env, capsys)
    plan["items"][0]["state"] = "closed"
    write_plan(env, plan, rehash=True)
    rc, _ = apply(env, capsys)
    assert rc != 0
    no_posts(env)


@pytest.mark.parametrize("ra", ["-5", "abc", "0", "61", "60", "1e9", "nan", "inf", "Wed, 21 Oct 2026 07:28:00 GMT", ""])
def test_retry_after_oddities_never_cause_long_sleeps_or_unbounded_posts(env, capsys, ra) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n >= 2:
            return httpx.Response(429, headers={"Retry-After": ra}, json={"message": "slow"})
        return None

    make_plan(env, capsys)
    env.fake.on_post = hook
    rc, _ = apply(env, capsys)
    assert rc != 0
    assert max(env.sleeps, default=0) <= 60 and len(env.sleeps) <= 8
    assert len(env.fake.posts) <= 1 + 3 * 4


def test_assignee_case_difference_in_the_response_is_not_a_mismatch(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        payload = json.loads(req.content)
        item = fake.create(payload)
        item["assignees"] = [{"login": a["login"].upper()} for a in item["assignees"]]
        return httpx.Response(201, json=item)

    make_plan(env, capsys)
    env.fake.on_post = hook
    rc, out = apply(env, capsys)
    assert rc == 0, out


@pytest.mark.parametrize("assignees", [None, "oops", [None], [{"nologin": 1}]], ids=["null", "string", "null_item", "no_login"])
def test_malformed_assignees_in_the_create_response_are_reported_not_crashing(env, capsys, assignees) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        item = fake.create(json.loads(req.content))
        item["assignees"] = assignees
        return httpx.Response(201, json=item)

    make_plan(env, capsys)
    env.fake.on_post = hook
    rc, out = apply(env, capsys)
    assert "Traceback" not in out
    assert rc != 0, "assignees could not be confirmed -> must not look like success"


def test_output_never_echoes_raw_control_or_bidi_characters_from_plan_or_server(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 1:
            return httpx.Response(422, json={"message": "bad \x1b[31m \u202e"})
        return None

    plan = make_plan(env, capsys)
    env.fake.on_post = hook
    rc, out = apply(env, capsys)
    assert rc != 0
    assert not [c for c in out if (ord(c) < 32 and c != "\n") or 0x7F <= ord(c) < 0xA0 or c in "\u202e\u2066\u200b"]
    assert plan


def test_no_test_in_the_suite_can_reach_the_real_github() -> None:
    root = Path(__file__).resolve().parent
    for f in root.glob("test_*.py"):
        text = f.read_text(encoding="utf-8")
        if f.name == Path(__file__).name:
            continue  # this guard names the patterns it forbids
        assert "socket.create_connection" not in text and "urllib.request" not in text, f.name
        assert "httpx.get(" not in text and "httpx.post(" not in text and "requests." not in text, f.name


# ---- round 3: interrupt / crash semantics, recovered-issue verification ---------------------------

def _rows(env: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    log = env.ws / "out" / f"audit-{plan['plan_sha256'][:8]}.jsonl"
    assert log.is_file(), "audit file missing"
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.mark.parametrize("exc", [KeyboardInterrupt, RuntimeError, ValueError, OSError], ids=lambda e: e.__name__)
def test_interrupt_or_crash_on_task_2_logs_task_1_marks_the_rest_not_attempted(env, capsys, exc) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 2:
            raise exc(f"boom {TOKEN} Bearer {TOKEN}")
        return None

    plan = make_plan(env, capsys)
    env.fake.on_post = hook
    rc, out = apply(env, capsys)
    assert rc != 0 and "Traceback" not in out
    assert len(env.fake.posts) == 2, "no POST may follow the interrupt/crash"
    rows = {r["task_id"]: r for r in _rows(env, plan)}
    assert set(rows) == {f"TASK-{n:03d}" for n in range(1, 6)}, "every task needs an audit line"
    assert rows["TASK-001"]["action"] == "created" and rows["TASK-001"].get("issue_number")
    assert rows["TASK-002"].get("error")
    assert {rows[f"TASK-00{n}"]["action"] for n in (3, 4, 5)} == {"not_attempted"}
    for t in ("TASK-001", "TASK-002", "TASK-003", "TASK-004", "TASK-005"):
        assert t in out, f"report is missing {t}"
    blob = (out + json.dumps(rows)).lower()
    assert TOKEN.lower() not in blob and "bearer" not in blob and "boom" not in blob
    assert "documentation_url" not in blob


def test_crash_while_reading_before_the_first_post_means_zero_posts_and_clean_exit(env, capsys) -> None:
    def boom(req: httpx.Request) -> httpx.Response | None:
        if req.method == "GET" and req.url.path.endswith("/labels"):
            raise RuntimeError(f"label endpoint exploded {TOKEN}")
        return None

    make_plan(env, capsys)
    env.fake.override = boom
    rc, out = apply(env, capsys)
    assert rc != 0 and out.strip()
    assert "Traceback" not in out and TOKEN not in out
    no_posts(env)


def test_crash_in_the_pre_post_reread_is_logged_and_stops_the_run(env, capsys) -> None:
    state = {"gets": 0}

    def boom(req: httpx.Request) -> httpx.Response | None:
        if req.method == "GET" and req.url.path.endswith("/issues"):
            state["gets"] += 1
            if state["gets"] >= 4:  # revalidation reads happen first; fail on a per-task re-read
                raise RuntimeError("reread exploded")
        return None

    plan = make_plan(env, capsys)
    state["gets"] = 0
    env.fake.override = boom
    rc, out = apply(env, capsys)
    assert rc != 0 and "Traceback" not in out
    rows = _rows(env, plan)
    assert len(rows) == 5 and all(r["task_id"] for r in rows)
    created = {r["task_id"] for r in rows if r["action"].startswith("created")}
    assert created == set(markers_in_store(env.fake)), "audit and store disagree about what was created"


def _recovered(env: Any, capsys: Any, mutate: Any) -> tuple[int, str, dict[str, Any]]:
    """Server creates the issue, then the response is lost (timeout) -> executor re-reads and recovers."""
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 2:
            item = fake.create(json.loads(req.content))
            mutate(item)
            raise httpx.ReadTimeout("response lost")
        return None

    plan = make_plan(env, capsys)
    env.fake.on_post = hook
    rc, out = apply(env, capsys)
    return rc, out, plan


def test_recovered_issue_with_correct_assignees_is_a_success(env, capsys) -> None:
    rc, out, _ = _recovered(env, capsys, lambda item: None)
    assert rc == 0, out
    assert sorted(markers_in_store(env.fake)) == [f"TASK-{n:03d}" for n in range(1, 6)]
    assert len(env.fake.posts) == 5


def test_recovered_issue_with_assignee_case_difference_is_a_success(env, capsys) -> None:
    rc, out, _ = _recovered(env, capsys, lambda item: item.update(assignees=[{"login": a["login"].upper()} for a in item["assignees"]]))
    assert rc == 0, out


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("missing", lambda item: item.update(assignees=[])),
        ("extra", lambda item: item.update(assignees=[*item["assignees"], {"login": "someone-else"}])),
        ("wrong_person", lambda item: item.update(assignees=[{"login": "example-dev-3"}])),
        ("null", lambda item: item.update(assignees=None)),
        ("string", lambda item: item.update(assignees="oops")),
        ("null_item", lambda item: item.update(assignees=[None])),
        ("no_login", lambda item: item.update(assignees=[{"nologin": 1}])),
        ("key_absent", lambda item: item.pop("assignees")),
    ],
)
def test_recovered_issue_with_unverifiable_or_wrong_assignees_is_an_error(env, capsys, name, mutate) -> None:
    rc, out, plan = _recovered(env, capsys, mutate)
    assert rc != 0, name
    assert "TASK-002" in out and "Traceback" not in out
    row = [r for r in _rows(env, plan) if r["task_id"] == "TASK-002"][-1]
    assert row.get("error") and ("ASSIGNEE" in row["error"]), row
    assert len(env.fake.posts) == 5, "recovered issue must never be POSTed again"


def test_created_issue_with_extra_assignee_in_the_response_is_a_mismatch(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        item = fake.create(json.loads(req.content))
        if n == 1:
            item["assignees"] = [*item["assignees"], {"login": "uninvited"}]
        return httpx.Response(201, json=item)

    make_plan(env, capsys)
    env.fake.on_post = hook
    rc, out = apply(env, capsys)
    assert rc != 0 and "uninvited" in out


def test_every_exit_path_prints_a_report_and_a_nonzero_code_on_any_error(env, capsys) -> None:
    for status in (422, 500, 403):
        env.fake = FakeGitHub()
        env.history.append(env.fake)
        env.fake.on_post = lambda f, r, n, s=status: httpx.Response(s, json={"message": "no", "documentation_url": "https://x"}) if n == 1 else None
        make_plan(env, capsys, fake=env.fake)
        env.fake.on_post = lambda f, r, n, s=status: httpx.Response(s, json={"message": "no", "documentation_url": "https://x"}) if n == 1 else None
        rc, out = apply(env, capsys)
        assert rc != 0, status
        assert all(f"TASK-00{n}" in out for n in range(1, 6)), (status, out)
        assert "documentation_url" not in out


def test_audit_rows_never_carry_bodies_or_raw_server_text_in_any_branch(env, capsys) -> None:
    def hook(fake: FakeGitHub, req: httpx.Request, n: int) -> httpx.Response | None:
        if n == 2:
            return httpx.Response(422, json={"message": "MÔ TẢ SỐ 2 leaked", "documentation_url": "https://docs"})
        if n == 3:
            raise RuntimeError("Mô tả số 3 leaked")
        return None

    plan = make_plan(env, capsys)
    env.fake.on_post = hook
    apply(env, capsys)
    blob = (env.ws / "out" / f"audit-{plan['plan_sha256'][:8]}.jsonl").read_text(encoding="utf-8").lower()
    for bad in ("leaked", "documentation_url", "mô tả số", "authorization", TOKEN.lower()):
        assert bad not in blob, bad


def test_no_new_write_path_exists_besides_create_issue() -> None:
    import ast

    tree = ast.parse((SRC / "execution" / "issue_executor.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_request"]
    assert len(calls) == 1, "exactly one call to the shared request primitive in the write module"
    arg = calls[0].args[0]
    assert isinstance(arg, ast.Constant) and arg.value == "POST"
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(SRC.rglob("*.py")))
    assert len(re.findall(r"_request\(\s*['\"](?!GET)", text)) == 1


def test_ctrl_c_during_the_pre_post_checks_means_zero_posts_and_a_clean_message(env, capsys) -> None:
    def boom(req: httpx.Request) -> httpx.Response | None:
        if req.method == "GET" and req.url.path.endswith("/labels"):
            raise KeyboardInterrupt
        return None

    make_plan(env, capsys)
    env.fake.override = boom
    rc, out = apply(env, capsys)
    assert rc != 0 and out.strip() and "Traceback" not in out
    no_posts(env)
