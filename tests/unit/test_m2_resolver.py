"""M2a resolver API contract tests (spec §3.1, §3.3 rules 1-5, §10 T02-T04).

Assumed contract (flag to leader; the three adapter functions below are the ONLY place to adjust):
  github_bot.identities.resolver.load_members(path, workspace) -> Directory  (raises on bad file)
  github_bot.identities.resolver.resolve_task(task, directory, assign_collaborators=False)
      -> object with .blocked: bool, .assignees / .collaborators: list[str] (GitHub logins,
         no '@', de-duplicated, order of first appearance), .diagnostics: list[objects with .message];
         when blocked, .assignees must be empty (never a partial/guessed set).
Never fuzzy-match; never guess; BLOCK instead.
"""

from pathlib import Path
from typing import Any

import pytest

from github_bot.models import Task

MEMBERS = """members:
  TV01: {display_name: "Nguyễn Văn A", github: "example-dev-1", aliases: ["Anh A"]}
  TV02: {display_name: "Trần Văn B", github: "example-dev-2", aliases: []}
  TV03: {display_name: "Lê Văn C", github: "example-dev-3", aliases: ["C"]}
"""


# ---- adapters ----------------------------------------------------------------------------
def _load(tmp_path: Path, text: str = MEMBERS) -> Any:
    from github_bot.identities.resolver import load_members

    (tmp_path / "members.yaml").write_text(text, encoding="utf-8")
    return load_members("members.yaml", tmp_path)


def _resolve(members: Any, **fields: Any) -> Any:
    from github_bot.identities.resolver import resolve_task

    flag = fields.pop("assign_collaborators", False)
    t = Task(id="TASK-001", title="t", source_file="x.md", source_reference="TASK-001", **fields)
    return resolve_task(t, members, assign_collaborators=flag)


def _msgs(r: Any) -> str:
    return " | ".join(d.message for d in r.diagnostics)


@pytest.fixture
def members(tmp_path: Path) -> Any:
    return _load(tmp_path)


# ---- matching: ID, display name, alias, login; exact only --------------------------------
@pytest.mark.parametrize(
    ("ref", "login"),
    [
        ("TV01", "example-dev-1"),
        ("Nguyễn Văn A", "example-dev-1"),
        ("Anh A", "example-dev-1"),
        ("@example-dev-1", "example-dev-1"),
        ("example-dev-1", "example-dev-1"),    # bare login = exact (case-insensitive) name ref
        ("EXAMPLE-DEV-1", "example-dev-1"),
        ("tv01", "example-dev-1"),             # member ID is case-insensitive
        ("Tv02", "example-dev-2"),
        ("@Example-Dev-1", "example-dev-1"),   # GitHub logins are case-insensitive
        ("  TV02  ", "example-dev-2"),          # surrounding whitespace is not a different name
    ],
)
def test_exact_references_resolve(members: Any, ref: str, login: str) -> None:
    r = _resolve(members, assignees=[ref])
    assert not r.blocked, _msgs(r)
    assert [x.lower() for x in r.assignees] == [login]


@pytest.mark.parametrize(
    "ref",
    ["TV04", "TV1", "tv01x", "Anh", "Nguyen Van A", "Nguyễn Văn", "Văn A", "A", "example-dev",
     "example-dev-11", "@example-dev-9", "example-dev-9", "Hoàng Văn Z", ""],
)
def test_no_fuzzy_no_guess_unmapped_blocks(members: Any, ref: str) -> None:
    r = _resolve(members, assignees=[ref])
    assert r.blocked, ref
    if ref.strip():
        assert ref.strip().lstrip("@") in _msgs(r)


def test_ambiguous_name_blocks(tmp_path: Path) -> None:
    text = """members:
  TV01: {display_name: "Nguyễn Văn A", github: "example-dev-1", aliases: ["Minh"]}
  TV02: {display_name: "Trần Văn B", github: "example-dev-2", aliases: ["Minh"]}
"""
    try:
        m = _load(tmp_path, text)
    except Exception:  # noqa: BLE001 - rejecting the ambiguous registry at load time is equally fail-closed
        return
    r = _resolve(m, assignees=["Minh"])
    assert r.blocked
    assert "Minh" in _msgs(r)


# ---- T04: explicit login vs member ID ----------------------------------------------------
def test_conflict_login_vs_member_id_blocks_without_choosing(members: Any) -> None:
    r = _resolve(members, assignees=["TV02"], github=["example-dev-1"])
    assert r.blocked and r.assignees == []
    assert "TV02" in _msgs(r) and "example-dev-1" in _msgs(r)


def test_login_agreeing_with_member_id_is_ok(members: Any) -> None:
    r = _resolve(members, assignees=["TV02"], github=["Example-Dev-2"])
    assert not r.blocked, _msgs(r)
    assert [x.lower() for x in r.assignees] == ["example-dev-2"]


def test_two_assignees_two_logins_must_each_agree(members: Any) -> None:
    r = _resolve(members, assignees=["TV01", "TV02"], github=["example-dev-1", "example-dev-3"])
    assert r.blocked


# ---- GitHub login syntax (1-39 chars, [A-Za-z0-9-], no leading/trailing/double hyphen) ----
@pytest.mark.parametrize(
    "bad", ["x y", "-bad", "bad-", "a--b", "x" * 40, "a_b", "a.b", "a$(id)", "ư", "", " ", "a/b"]
)
def test_invalid_login_syntax_blocks(members: Any, bad: str) -> None:
    r = _resolve(members, assignees=["TV01"], github=[bad])
    assert r.blocked, repr(bad)


@pytest.mark.parametrize("bad", ["@-bad", "@a--b", "@" + "x" * 40, "@a_b", "@", "@@x"])
def test_invalid_login_in_assignee_column_blocks(members: Any, bad: str) -> None:
    r = _resolve(members, assignees=[bad])
    assert r.blocked, bad


# ---- no assignee: allowed, never auto-assigned (leader decision 2) ---------------------------
def test_no_assignee_is_warning_not_block(members: Any) -> None:
    r = _resolve(members, assignees=[])
    assert not r.blocked, _msgs(r)
    assert r.assignees == []
    assert any(d.code == "NO_ASSIGNEE" and d.level == "warning" for d in r.diagnostics)


def test_no_assignee_never_falls_back_to_collaborators_or_anyone(members: Any) -> None:
    r = _resolve(members, assignees=[], collaborators=["TV02"])
    assert not r.blocked, _msgs(r)
    assert r.assignees == []


# ---- sets: several people, duplicates, collaborators -------------------------------------
def test_multiple_assignees_deduplicated_in_order(members: Any) -> None:
    r = _resolve(members, assignees=["TV03", "TV01", "Anh A", "@example-dev-3", "TV01"])
    assert not r.blocked, _msgs(r)
    assert [x.lower() for x in r.assignees] == ["example-dev-3", "example-dev-1"]


def test_one_bad_assignee_blocks_whole_task_no_partial(members: Any) -> None:
    r = _resolve(members, assignees=["TV01", "TV99"])
    assert "TV99" in _msgs(r)


def test_collaborators_not_assigned_by_default(members: Any) -> None:
    r = _resolve(members, assignees=["TV01"], collaborators=["TV02"])
    assert not r.blocked, _msgs(r)
    assert [x.lower() for x in r.assignees] == ["example-dev-1"]
    assert [x.lower() for x in r.collaborators] == ["example-dev-2"]


def test_collaborators_merged_only_when_flag_set(members: Any) -> None:
    r = _resolve(members, assignees=["TV01"], collaborators=["TV02", "TV01"],
                 assign_collaborators=True)
    assert not r.blocked, _msgs(r)
    assert [x.lower() for x in r.assignees] == ["example-dev-1", "example-dev-2"]


def test_unmapped_collaborator_blocks(members: Any) -> None:
    r = _resolve(members, assignees=["TV01"], collaborators=["TV99"])
    assert r.blocked and "TV99" in _msgs(r)


# ---- registry loading --------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "members: [unclosed\n : :",
        "- list\n",
        "",
        "members:\n  TV01: {display_name: A}\n",
        (
            "members:\n  TV01: {display_name: A, github: a1, aliases: ['b2']}\n"
            "  TV02: {display_name: B, github: b2}\n"
        ),
        "members:\n  TV01: {display_name: b2, github: a1}\n  TV02: {display_name: B, github: b2}\n",
        "members:\n  TV01:\n    display_name: A\n    github: |\n      a1\n",
        "members:\n  TV01: {display_name: '  ', github: a1}\n",
        "members:\n  TV01: {display_name: A, github: a1}\n  tv01: {display_name: B, github: b2}\n",
        "members:\n  TV01: {display_name: A, github: 'bad login'}\n",
        "members:\n  TV01: {display_name: A, github: dup}\n  TV02: {display_name: B, github: DUP}\n",
        (
            "members:\n  TV01: {display_name: A, github: a1, aliases: [X]}\n"
            "  TV02: {display_name: B, github: b2, aliases: [X]}\n"
        ),
        "members:\n  TV01: !!python/object/apply:os.system ['true']\n",
    ],
)
def test_bad_registry_raises_clean_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(Exception) as e:
        _load(tmp_path, text)
    assert not isinstance(e.value, (ImportError, AttributeError, TypeError, KeyError))


def test_registry_is_loaded_with_safe_loader_only() -> None:
    src = Path(__file__).resolve().parents[2] / "src"
    code = "\n".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py"))
    assert "yaml.load(" not in code and "yaml.unsafe_load" not in code
    assert "yaml.full_load" not in code and "yaml.FullLoader" not in code
    assert "yaml.Loader" not in code


def test_resolved_login_never_contains_whitespace_or_control_chars(members: Any) -> None:
    for ref in ("TV01", "tv01", "@Example-Dev-1", "example-dev-1", "Anh A"):
        r = _resolve(members, assignees=[ref])
        assert all(x == x.strip() and x.isascii() and x.isprintable() for x in r.assignees), ref


def test_nfd_and_nbsp_names_resolve_to_the_same_person_only(members: Any) -> None:
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Nguyễn Văn A")
    r = _resolve(members, assignees=[nfd, "Nguyễn\u00a0Văn A"])
    assert not r.blocked and [x.lower() for x in r.assignees] == ["example-dev-1"]
    for bad in ("Nguyễn\u200bVăn A", "ＴＶ０１", "＠example-dev-1"):   # ZWSP / fullwidth: no guess
        assert _resolve(members, assignees=[bad]).blocked, repr(bad)
