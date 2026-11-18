"""M3a black-box tests: `issues plan` (read-only planner) against a fake GitHub with an issue store.

Contract assumed (flag to leader if coder differs):
  github-bot issues plan --input F.md --members M.yaml --repo owner/repo --output-dir DIR
      [--max-tasks N] [--workspace W]        (needs env GITHUB_TOKEN; only GET requests)
  exit 0 = no BLOCK; != 0 = any BLOCK / error. Writes DIR/{PREVIEW.md,PREVIEW.json,plan.json}.
  plan.json = {"repo","source_file","items":[{"task_id","action":CREATE|SKIP|BLOCK,"title","body",
  "assignees","labels","issue_number","reasons"}],"plan_sha256"} (sha256 hex of canonical JSON of
  everything else). Idempotency key = repo + normalized source_file + task_id; the marker
  `<!-- github-automation-bot: source=SRC; task_id=ID; schema=v1 -->` is the last line of the body.
  Existing issues are read with state=all (closed count), paginated; PRs are ignored.
"""

from __future__ import annotations

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

MEMBERS = """members:
  TV01: {display_name: "Nguyễn Văn A", github: "example-dev-1", aliases: ["Anh A"]}
  TV02: {display_name: "Trần Văn B", github: "example-dev-2"}
  TV03: {display_name: "Lê Văn C", github: "example-dev-3"}
"""
TASKS = """## TASK-001: Một
- Phụ trách: TV01
- Labels: backend

### Mô tả
Mô tả một.

### Tiêu chí nghiệm thu
- [ ] A
- [ ] B

## TASK-002: Hai
- Phụ trách: TV02, TV03
- Labels: auth
"""
MARK = re.compile(
    r"<!-- github-automation-bot: source=[^;<>\s]+; task_id=[A-Za-z0-9-]+; schema=v1 -->"
)


def marker(task_id: str, source: str = "TASKS.md") -> str:
    return f"<!-- github-automation-bot: source={source}; task_id={task_id}; schema=v1 -->"


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
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
        assert_requests_safe(f)
        assert {r.method for r in f.requests} <= {"GET"}, "planner must be read-only"
    assert max(st.sleeps, default=0) <= 60


def plan(
    gh: Any, capsys: pytest.CaptureFixture[str], md: str = TASKS, fake: FakeGitHub | None = None,
    *, args: list[str] | None = None, input_name: str = "TASKS.md", members: str = MEMBERS,
    out: str = "out",
) -> tuple[int, str, dict[str, Any] | None]:
    if fake is not None:
        gh.fake = fake
        gh.history.append(fake)
    (gh.ws / input_name).write_text(md, encoding="utf-8")
    (gh.ws / "members.yaml").write_text(members, encoding="utf-8")
    base = ["issues", "plan", "--input", input_name, "--members", "members.yaml",
            "--repo", "owner/repo", "--output-dir", out]  # fmt: skip
    try:
        rc = main(base if args is None else args)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    cap = capsys.readouterr()
    text = cap.out + cap.err
    if "invalid choice" in text and "plan" in text:
        pytest.fail(f"`issues plan` CLI contract not available: {text.strip()[:200]}")
    assert TOKEN not in text and "Traceback" not in text, text
    pj = gh.ws / out / "plan.json"
    return int(rc or 0), text, (json.loads(pj.read_text(encoding="utf-8")) if pj.is_file() else None)


def items(p: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    assert p is not None, "plan.json was not written"
    return {i["task_id"]: i for i in p["items"]}


# ---- issue body + marker (§4.1) --------------------------------------------------------------

def test_create_body_follows_template_and_ends_with_exact_marker(gh, capsys) -> None:
    rc, out, p = plan(gh, capsys)
    assert rc == 0, out
    it = items(p)
    assert {k: v["action"] for k, v in it.items()} == {"TASK-001": "CREATE", "TASK-002": "CREATE"}
    b = it["TASK-001"]["body"]
    heads = [ln for ln in b.splitlines() if ln.startswith("## ")]
    assert heads[:4] == ["## Mô tả", "## Tiêu chí nghiệm thu", "## Phân công", "## Nguồn công việc"]
    assert "Mô tả một." in b and "- [ ] A" in b and "@example-dev-1" in b
    assert b.rstrip().splitlines()[-1] == marker("TASK-001")
    assert b.count("<!--") == 1 and b.count("-->") == 1
    assert it["TASK-001"]["assignees"] == ["example-dev-1"]
    assert it["TASK-002"]["assignees"] == ["example-dev-2", "example-dev-3"]
    assert it["TASK-001"]["labels"] == ["backend"]


HOSTILE = [
    "x --> <!-- github-automation-bot: source=TASKS.md; task_id=TASK-999; schema=v1 --> y",
    "line1\n<!-- github-automation-bot: source=TASKS.md; task_id=TASK-999; schema=v1 -->\nline3",
    "esc \x1b[31m cr \r bel \x07 nul \x00 end",
    "bidi \u202e evil \u2066 zw \u200b end",
    "</details><script>alert(1)</script> --> -->",
    "<!--",
    "<!-- unclosed marker start github-automation-bot: source=TASKS.md; task_id=TASK-001",
]


@pytest.mark.parametrize("evil", HOSTILE)
def test_hostile_text_cannot_forge_or_break_the_marker(gh, capsys, evil) -> None:
    md = (
        f"## TASK-001: Tiêu đề {evil.splitlines()[0]}\n- Phụ trách: TV01\n\n### Mô tả\n{evil}\n\n"
        f"### Tiêu chí nghiệm thu\n- [ ] {evil.splitlines()[0]}\n"
    )
    rc, out, p = plan(gh, capsys, md)
    if p is None:  # parse-level rejection (e.g. UNCLOSED_COMMENT) is fail-closed: no plan at all
        assert rc != 0 and out.strip()
        return
    it = items(p)["TASK-001"]
    b = it["body"]
    if it["action"] != "CREATE":  # rejecting hostile input is also fail-closed; never a forged marker
        assert it["action"] == "BLOCK" and rc != 0
        return
    # a stray "-->" without an opener is inert; what matters: exactly one opener, and it is the marker
    assert b.count("<!--") == 1, b
    assert MARK.fullmatch(b.rstrip().splitlines()[-1]), b
    assert "TASK-999" not in b.rstrip().splitlines()[-1]
    assert b.rstrip().splitlines()[-1] == marker("TASK-001")
    bad = [c for c in b if (ord(c) < 32 and c not in "\n") or 0x7F <= ord(c) < 0xA0 or c in "\u202e\u2066\u200b"]
    assert not bad, bad
    assert "\n" not in it["title"] and "\r" not in it["title"]
    assert not any(c in it["title"] for c in "\x1b\x00\x07\u202e\u2066\u200b")


@pytest.mark.parametrize(
    "name", ["my tasks.md", "a;b.md", "a-->b.md", "a<b>.md", "dấu tiếng việt.md", "a'b.md"]
)
def test_unusual_source_file_names_cannot_break_the_marker(gh, capsys, name) -> None:
    rc, out, p = plan(gh, capsys, input_name=name)
    assert rc == 0, out
    b = items(p)["TASK-001"]["body"]
    last = b.rstrip().splitlines()[-1]
    # only the marker line may open/close a comment (a visible "File:" line may contain a raw "-->")
    assert b.count("<!--") == 1 and last.count("<!--") == 1 and last.count("-->") == 1, b
    assert MARK.fullmatch(last), last
    assert p is not None and "TASK-001" in last and p["source_file"]


def test_marker_of_one_task_never_appears_in_another_task_body(gh, capsys) -> None:
    md = TASKS.replace("Mô tả một.", marker("TASK-002"))
    _, _, p = plan(gh, capsys, md)
    for tid, it in items(p).items():
        if it["action"] == "CREATE":
            assert it["body"].count("github-automation-bot") == 1
            assert marker(tid) in it["body"]


# ---- existing issues: key = repo + source + task_id ------------------------------------------

def body_of(tid: str, extra: str = "", source: str = "TASKS.md") -> str:
    return f"## Mô tả\n{extra}\n\n{marker(tid, source)}\n"


def test_existing_open_issue_is_skip_with_number(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(7, body_of("TASK-001"))])
    rc, out, p = plan(gh, capsys, fake=fake)
    it = items(p)
    assert it["TASK-001"]["action"] == "SKIP" and it["TASK-001"].get("issue_number") == 7
    assert it["TASK-002"]["action"] == "CREATE" and rc == 0, out


def test_closed_issue_still_counts_and_is_never_reopened(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(3, body_of("TASK-001"), state="closed")])
    _, _, p = plan(gh, capsys, fake=fake)
    assert items(p)["TASK-001"]["action"] == "SKIP"
    assert all(r.url.params.get("state") == "all" for r in fake.hits("issues"))


def test_changed_title_does_not_change_identity(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(5, body_of("TASK-001"), title="tên cũ hoàn toàn khác")])
    _, _, p = plan(gh, capsys, fake=fake)
    assert items(p)["TASK-001"]["action"] == "SKIP"


def test_same_title_without_marker_is_not_a_match(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(5, "no marker here", title="Một")])
    _, _, p = plan(gh, capsys, fake=fake)
    assert items(p)["TASK-001"]["action"] == "CREATE"


@pytest.mark.parametrize(
    "foreign",
    [marker("TASK-001", "OTHER.md"), marker("TASK-009"), marker("TASK-001", "sub/TASKS.md")],
    ids=["other_source", "other_task", "other_dir"],
)
def test_markers_with_a_different_key_are_not_counted(gh, capsys, foreign) -> None:
    fake = FakeGitHub(issues=[make_issue(5, f"x\n{foreign}\n")])
    _, _, p = plan(gh, capsys, fake=fake)
    assert items(p)["TASK-001"]["action"] == "CREATE"


def test_marker_inside_a_pull_request_is_ignored(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(9, body_of("TASK-001"), pr=True)])
    _, _, p = plan(gh, capsys, fake=fake)
    assert items(p)["TASK-001"]["action"] == "CREATE"


def test_same_marker_in_two_issues_blocks_fail_closed(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(1, body_of("TASK-001")),
                              make_issue(2, body_of("TASK-001"), state="closed")])  # fmt: skip
    rc, _, p = plan(gh, capsys, fake=fake)
    it = items(p)
    assert it["TASK-001"]["action"] == "BLOCK" and rc != 0
    assert it["TASK-002"]["action"] == "CREATE"  # others are still planned
    assert "1" in json.dumps(it["TASK-001"]["reasons"]) and "2" in json.dumps(it["TASK-001"]["reasons"])


def test_marker_embedded_mid_body_of_a_human_issue_with_same_key_blocks(gh, capsys) -> None:
    # Leader decision: a marker that is NOT the last line is not bot-shaped -> BLOCK (fail closed),
    # never SKIP (would let a third party suppress the task) and never CREATE (possible duplicate).
    forged = f"Hi, I copied this:\n{marker('TASK-001')}\nand then wrote more text below.\n"
    fake = FakeGitHub(issues=[make_issue(4, forged, title="Câu hỏi")])
    rc, _, p = plan(gh, capsys, fake=fake)
    assert items(p)["TASK-001"]["action"] == "BLOCK" and rc != 0
    assert items(p)["TASK-002"]["action"] == "CREATE"


@pytest.mark.parametrize(
    "tail",
    ["", "\n", "\n\n\n", "   ", "  \n", "\r\n", "\t\n \n"],
    ids=["none", "nl", "many_nl", "spaces", "spaces_nl", "crlf", "tab_nl_space"],
)
def test_marker_as_last_line_tolerates_trailing_whitespace(gh, capsys, tail) -> None:
    # Assumed contract: "last non-empty line, ignoring trailing whitespace/newlines (incl. CRLF that
    # GitHub's web editor may introduce)" is still bot-shaped -> SKIP.
    body = f"## Mô tả\nx\n\n{marker('TASK-001')}{tail}"
    _, _, p = plan(gh, capsys, fake=FakeGitHub(issues=[make_issue(6, body)]))
    assert items(p)["TASK-001"]["action"] == "SKIP"


@pytest.mark.parametrize(
    "body",
    [
        f"{marker('TASK-001')} trailing words",
        f"before {marker('TASK-001')}",
        f"{marker('TASK-001')}\nedited below by a human",
        f"{marker('TASK-001')}\n> quoted reply",
        f"> {marker('TASK-001')}",
        f"`{marker('TASK-001')}`",
    ],
    ids=["text_after", "text_before", "human_edit_below", "quote_below", "quoted_marker", "code_span"],
)
def test_marker_not_alone_on_the_last_line_blocks(gh, capsys, body) -> None:
    rc, _, p = plan(gh, capsys, fake=FakeGitHub(issues=[make_issue(6, body)]))
    assert items(p)["TASK-001"]["action"] == "BLOCK" and rc != 0


def test_marker_bot_shaped_in_one_issue_and_embedded_in_another_blocks(gh, capsys) -> None:
    fake = FakeGitHub(issues=[
        make_issue(1, body_of("TASK-001")),
        make_issue(2, f"x\n{marker('TASK-001')}\ny"),
    ])  # fmt: skip
    rc, _, p = plan(gh, capsys, fake=fake)
    assert items(p)["TASK-001"]["action"] == "BLOCK" and rc != 0


def test_task_id_match_is_case_insensitive_like_the_parser_dedupes(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(8, body_of("TASK-001").replace("TASK-001", "task-001"))])
    _, _, p = plan(gh, capsys, fake=fake)
    assert items(p)["TASK-001"]["action"] != "CREATE"


def test_issues_beyond_first_page_and_closed_ones_are_found(gh, capsys) -> None:
    filler = [make_issue(n, "nothing") for n in range(1, 251)]
    filler[205] = make_issue(206, body_of("TASK-001"), state="closed")  # page 3, closed
    filler.insert(120, make_issue(999, body_of("TASK-002"), pr=True))      # PR on page 2 ignored
    fake = FakeGitHub(issues=filler)
    _, _, p = plan(gh, capsys, fake=fake)
    it = items(p)
    assert it["TASK-001"]["action"] == "SKIP"
    assert it["TASK-002"]["action"] == "CREATE"
    assert len(fake.hits("issues")) >= 3


def test_issue_list_longer_than_the_page_budget_is_never_treated_as_complete(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(n, "nothing") for n in range(1, 2501)])
    _, _, p = plan(gh, capsys, fake=fake)
    acts = {i["action"] for i in (p or {"items": []})["items"]}
    pages = len(fake.hits("issues"))
    assert acts <= {"BLOCK"} or pages >= 25, "claimed CREATE without reading all 2500 issues"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502])
def test_issue_list_failure_blocks_every_task(gh, capsys, status) -> None:
    def h(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/issues"):
            return httpx.Response(status, json={"message": "x"})
        return None

    rc, out, p = plan(gh, capsys, fake=FakeGitHub(override=h))
    assert rc != 0 and out.strip()
    assert all(i["action"] == "BLOCK" for i in (p or {"items": []})["items"]), "never CREATE blind"


@pytest.mark.parametrize("body", ["not json{", "{}", "null", '[{"number": "x"}]', "[1, 2]"])
def test_issue_list_malformed_blocks_every_task(gh, capsys, body) -> None:
    def h(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/issues"):
            return httpx.Response(200, content=body)
        return None

    rc, _, p = plan(gh, capsys, fake=FakeGitHub(override=h))
    assert rc != 0
    assert all(i["action"] == "BLOCK" for i in (p or {"items": []})["items"])


def test_issue_list_timeout_blocks_every_task(gh, capsys) -> None:
    def h(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/issues"):
            raise httpx.ReadTimeout("t")
        return None

    rc, _, p = plan(gh, capsys, fake=FakeGitHub(override=h))
    assert rc != 0
    assert all(i["action"] == "BLOCK" for i in (p or {"items": []})["items"])


# ---- BLOCK reasons carried over from M2a/M2b, partial plans ------------------------------------

def test_blocked_tasks_do_not_stop_the_others_and_carry_no_issue_payload(gh, capsys) -> None:
    md = TASKS + "\n## TASK-003: Ba\n- Phụ trách: TV99\n\n## TASK-004: Bốn\n- Phụ trách: TV01\n- Labels: nope\n"
    rc, _, p = plan(gh, capsys, md)
    it = items(p)
    assert (it["TASK-001"]["action"], it["TASK-002"]["action"]) == ("CREATE", "CREATE")
    assert it["TASK-003"]["action"] == "BLOCK" and it["TASK-004"]["action"] == "BLOCK"
    assert rc != 0
    assert "TV99" in json.dumps(it["TASK-003"]["reasons"], ensure_ascii=False)
    assert "nope" in json.dumps(it["TASK-004"]["reasons"], ensure_ascii=False)
    for t in ("TASK-003", "TASK-004"):
        assert it[t]["assignees"] == [] and it[t]["body"] == ""  # nothing postable for a BLOCK


def test_duplicate_task_ids_never_produce_a_create(gh, capsys) -> None:
    md = TASKS + "\n## TASK-001: Trùng\n- Phụ trách: TV01\n"
    rc, out, p = plan(gh, capsys, md)
    assert rc != 0 and out.strip()
    if p is not None:
        assert all(i["action"] != "CREATE" for i in p["items"] if i["task_id"] == "TASK-001")


def test_repo_failure_blocks_everything(gh, capsys) -> None:
    rc, _, p = plan(gh, capsys, fake=FakeGitHub(repo_status=404))
    assert rc != 0
    assert all(i["action"] == "BLOCK" for i in (p or {"items": []})["items"])
    assert gh.fake.hits("issues") == []  # stop at the first failure, do not keep probing


def test_max_tasks_exceeded_is_a_clear_error_and_writes_nothing(gh, capsys) -> None:
    md = "\n".join(f"## TASK-{n:03d}: t{n}\n- Phụ trách: TV01\n" for n in range(1, 8))
    rc, out, p = plan(gh, capsys, md, args=[
        "issues", "plan", "--input", "TASKS.md", "--members", "members.yaml", "--repo",
        "owner/repo", "--output-dir", "out", "--max-tasks", "5"])  # fmt: skip
    assert rc != 0 and ("5" in out or "max" in out.lower()) and out.strip()
    assert p is None and not (gh.ws / "out" / "plan.json").exists()


def test_no_assignee_task_is_planned_and_has_no_assignees(gh, capsys) -> None:
    rc, out, p = plan(gh, capsys, "## TASK-001: Một\n- Labels: backend\n")
    assert rc == 0, out
    assert items(p)["TASK-001"]["action"] == "CREATE" and items(p)["TASK-001"]["assignees"] == []


# ---- plan hash ---------------------------------------------------------------------------------

def test_hash_is_deterministic_and_matches_every_output(gh, capsys) -> None:
    rc1, out1, p1 = plan(gh, capsys)
    raw1 = (gh.ws / "out" / "plan.json").read_bytes()
    md1 = (gh.ws / "out" / "PREVIEW.md").read_text(encoding="utf-8")
    rc2, _, p2 = plan(gh, capsys, fake=FakeGitHub())
    assert rc1 == rc2 == 0
    assert p1 is not None and p2 is not None
    h = p1["plan_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", h) and h == p2["plan_sha256"]
    assert raw1 == (gh.ws / "out" / "plan.json").read_bytes()
    assert h in out1 and h in md1
    pv = json.loads((gh.ws / "out" / "PREVIEW.json").read_text(encoding="utf-8"))
    assert pv["plan_sha256"] == h


def test_hash_covers_the_whole_plan_content(gh, capsys) -> None:
    _, _, p = plan(gh, capsys)
    assert p is not None
    h = p["plan_sha256"]
    # the stored hash must not be a hash of itself, and tampering with ANY item must be visible:
    # recompute over the canonical JSON of the plan without the hash field (documented contract).
    body = {k: v for k, v in p.items() if k != "plan_sha256"}
    candidates = {
        hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, indent=2).encode()).hexdigest(),
        hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }
    assert h in candidates, "plan_sha256 is not sha256 over canonical JSON of the plan content"


@pytest.mark.parametrize(
    ("what", "mut"),
    [
        ("description", lambda md: md.replace("Mô tả một.", "Mô tả khác.")),
        ("assignee", lambda md: md.replace("Phụ trách: TV01", "Phụ trách: TV03", 1)),
        ("label", lambda md: md.replace("Labels: backend", "Labels: database", 1)),
        ("title", lambda md: md.replace("TASK-001: Một", "TASK-001: Một khác", 1)),
        ("acceptance", lambda md: md.replace("- [ ] B", "- [ ] C")),
        ("task_order", lambda md: "## TASK-002" + md.split("## TASK-002")[1] + "\n## TASK-001" + md.split("## TASK-002")[0].split("## TASK-001")[1]),
        ("extra_task", lambda md: md + "\n## TASK-003: Ba\n- Phụ trách: TV01\n"),
    ],
)
def test_any_content_change_changes_the_hash(gh, capsys, what, mut) -> None:
    _, _, base = plan(gh, capsys)
    _, _, changed = plan(gh, capsys, mut(TASKS), fake=FakeGitHub(labels=["backend", "auth", "database"]))
    assert base and changed and base["plan_sha256"] != changed["plan_sha256"], what


def test_changes_outside_the_markdown_also_change_the_hash(gh, capsys) -> None:
    _, _, base = plan(gh, capsys)
    assert base is not None
    other_src = plan(gh, capsys, input_name="OTHER.md", fake=FakeGitHub())[2]
    members = MEMBERS.replace("example-dev-1", "example-dev-9")
    other_mem = plan(gh, capsys, members=members, fake=FakeGitHub(assignees=["example-dev-9", "example-dev-2", "example-dev-3"]))[2]
    exists = plan(gh, capsys, fake=FakeGitHub(issues=[make_issue(7, body_of("TASK-001"))]))[2]
    hashes = {base["plan_sha256"]}
    for q in (other_src, other_mem, exists):
        assert q is not None
        hashes.add(q["plan_sha256"])
    assert len(hashes) == 4, "source, registry, and existing-issue state must all be part of the hash"


def test_plan_files_have_no_volatile_values(gh, capsys) -> None:
    plan(gh, capsys)
    blob = "".join(
        (gh.ws / "out" / n).read_text(encoding="utf-8") for n in ("PREVIEW.md", "PREVIEW.json", "plan.json")
    )
    assert not re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", blob), "timestamp in plan output"
    assert not re.search(r"\b1[6-9]\d{8}\b", blob), "epoch time in plan output"
    assert str(gh.tmp) not in blob and str(gh.ws) not in blob, "absolute path in plan output"
    assert TOKEN not in blob and "authorization" not in blob.lower()


# ---- preview files -----------------------------------------------------------------------------

def test_preview_md_states_writes_are_disabled_and_summarises(gh, capsys) -> None:
    fake = FakeGitHub(issues=[make_issue(7, body_of("TASK-001"))])
    _, out, _ = plan(gh, capsys, fake=fake)
    md = (gh.ws / "out" / "PREVIEW.md").read_text(encoding="utf-8")
    assert "Write actions: DISABLED until approval" in md and "Write actions: DISABLED until approval" in out
    assert "owner/repo" in md and "TASKS.md" in md
    assert "CREATE: 1" in md and "SKIP: 1" in md and "BLOCK: 0" in md
    assert "TASK-001" in md and "TASK-002" in md
    assert (gh.ws / "out" / "PREVIEW.json").is_file()


def test_output_dir_outside_workspace_is_rejected(gh, capsys) -> None:
    outside = gh.tmp / "outside"
    for target in ("../outside", str(outside), "out/../../outside"):
        rc, out, _ = plan(gh, capsys, out=target) if False else plan(
            gh, capsys, args=["issues", "plan", "--input", "TASKS.md", "--members", "members.yaml",
                              "--repo", "owner/repo", "--output-dir", target])  # fmt: skip
        assert rc != 0 and out.strip(), target
        assert not outside.exists() or not list(outside.iterdir()), target


def test_output_dir_symlink_escaping_workspace_is_rejected(gh, capsys) -> None:
    outside = gh.tmp / "elsewhere"
    outside.mkdir()
    (gh.ws / "link").symlink_to(outside, target_is_directory=True)
    rc, _, _ = plan(gh, capsys, out="link")
    assert rc != 0 and list(outside.iterdir()) == []


def test_symlinked_preview_file_is_not_followed_and_foreign_files_survive(gh, capsys) -> None:
    out = gh.ws / "out"
    out.mkdir()
    victim = gh.tmp / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    (out / "PREVIEW.md").symlink_to(victim)
    (out / "notes.txt").write_text("mine", encoding="utf-8")
    rc, _, _ = plan(gh, capsys)
    assert victim.read_text(encoding="utf-8") == "precious", "wrote through a symlink"
    assert (out / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert rc != 0 or not (out / "PREVIEW.md").is_symlink()


def test_directory_named_like_a_preview_file_is_refused_cleanly(gh, capsys) -> None:
    (gh.ws / "out" / "plan.json").mkdir(parents=True)
    rc, out, _ = plan(gh, capsys)
    assert rc != 0 and out.strip()


def test_rerun_overwrites_previous_bot_output_but_nothing_else(gh, capsys) -> None:
    plan(gh, capsys)
    (gh.ws / "out" / "keep.txt").write_text("k", encoding="utf-8")
    rc, _, p = plan(gh, capsys, TASKS.replace("Mô tả một.", "Mô tả mới."), fake=FakeGitHub())
    assert rc == 0 and p is not None
    assert (gh.ws / "out" / "keep.txt").read_text(encoding="utf-8") == "k"
    assert "Mô tả mới." in items(p)["TASK-001"]["body"]


# ---- CLI contract ------------------------------------------------------------------------------

def test_missing_token_is_a_clear_error_with_zero_requests_and_no_files(gh, capsys, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    rc, out, p = plan(gh, capsys)
    assert rc != 0 and "GITHUB_TOKEN" in out
    assert gh.fake.requests == [] and p is None and not (gh.ws / "out").exists()


@pytest.mark.parametrize("drop", ["--input", "--members", "--repo", "--output-dir"])
def test_missing_required_flag_is_rejected_with_zero_requests(gh, capsys, drop) -> None:
    base = ["issues", "plan", "--input", "TASKS.md", "--members", "members.yaml",
            "--repo", "owner/repo", "--output-dir", "out"]  # fmt: skip
    i = base.index(drop)
    rc, _, _ = plan(gh, capsys, args=base[:i] + base[i + 2 :])
    assert rc != 0
    assert gh.fake.requests == []


@pytest.mark.parametrize(
    "repo", ["owner/repo/../..", "owner/repo?x=1", "../repo", "owner/", "", "a b/c", "owner/repo\n"]
)
def test_malformed_repo_is_rejected_before_any_request(gh, capsys, repo) -> None:
    rc, _, _ = plan(gh, capsys, args=["issues", "plan", "--input", "TASKS.md", "--members",
                                        "members.yaml", "--repo", repo, "--output-dir", "out"])  # fmt: skip
    assert rc != 0 and gh.fake.requests == []


def test_plan_makes_only_get_requests_to_the_three_read_areas(gh, capsys) -> None:
    rc, _, _ = plan(gh, capsys)
    assert rc == 0
    assert {r.method for r in gh.fake.requests} == {"GET"}
    kinds = [bool(gh.fake.hits(k)) for k in ("repo", "labels", "issues", "assignee")]
    assert all(kinds), kinds


def test_token_cannot_be_passed_on_the_command_line_to_plan(gh, capsys) -> None:
    rc, out, _ = plan(gh, capsys, args=["issues", "plan", "--input", "TASKS.md", "--members",
                                        "members.yaml", "--repo", "owner/repo", "--output-dir",
                                        "out", "--token", "ghp_cli_secret"])  # fmt: skip
    assert rc != 0 and "ghp_cli_secret" not in out and gh.fake.requests == []


def test_hostile_markdown_is_never_executed_by_plan(gh, capsys) -> None:
    md = "## TASK-001: $(touch PWNED_t) `touch PWNED_b`\n- Phụ trách: TV01\n\n### Mô tả\n$(touch PWNED_d); touch PWNED_s\n"
    plan(gh, capsys, md)
    assert not list(gh.ws.rglob("PWNED*")) and not list(Path.cwd().glob("PWNED*"))


def test_issue_list_empty_object_is_not_an_empty_issue_list(gh, capsys) -> None:
    """HTTP 200 `{}` must not be read as "zero issues" (that would CREATE blindly)."""

    def h(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/issues"):
            return httpx.Response(200, content="{}")
        return None

    rc, _, p = plan(gh, capsys, fake=FakeGitHub(override=h))
    assert rc != 0
    assert all(i["action"] == "BLOCK" for i in (p or {"items": []})["items"])


def test_same_content_in_two_workspaces_gives_the_same_hash(gh, capsys) -> None:
    _, _, a = plan(gh, capsys)
    other = gh.tmp / "ws2"
    other.mkdir()
    (other / "TASKS.md").write_text(TASKS, encoding="utf-8")
    (other / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    gh.fake = FakeGitHub()
    gh.history.append(gh.fake)
    rc = main(["issues", "plan", "--input", "TASKS.md", "--members", "members.yaml", "--repo",
               "owner/repo", "--output-dir", "out", "--workspace", str(other)])  # fmt: skip
    capsys.readouterr()
    assert rc == 0 and a is not None
    b = json.loads((other / "out" / "plan.json").read_text(encoding="utf-8"))
    assert a["plan_sha256"] == b["plan_sha256"]


def test_free_text_cannot_ping_people_or_cross_reference_issues(gh, capsys) -> None:
    md = (
        "## TASK-001: @octocat sửa #123\n- Phụ trách: TV01\n\n### Mô tả\n"
        "ping @octocat @org/team xem #123 owner/repo#9\n\n### Tiêu chí nghiệm thu\n- [ ] @victim #99\n"
    )
    _, _, p = plan(gh, capsys, md)
    it = items(p)["TASK-001"]
    body = it["body"]
    free = body.split("## Phân công")[0] + body.split("## Nguồn công việc")[1]
    assert not re.search(r"(?<![\w`\\])@[A-Za-z0-9]", free + it["title"]), "bare @mention in free text"
    assert not re.search(r"(?<![\w`\\&])#\d+", free + it["title"]), "bare #N reference in free text"
