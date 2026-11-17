from pathlib import Path

import pytest

from github_bot.cli import main
from github_bot.identities.resolver import Directory, MembersError, load_members, resolve_task
from github_bot.models import Task

YAML = """
members:
  TV01: {display_name: "Nguyễn Văn A", github: dev-a, aliases: ["Anh A", "Minh"]}
  TV02: {display_name: "Trần Văn B", github: Dev-B}
  TV03: {display_name: "Lê Văn C", github: dev-c}
"""
D = Directory.from_text(YAML)


def task(**kw: object) -> Task:
    return Task(id="TASK-001", title="t", source_file="x.md", source_reference="TASK-001", **kw)  # type: ignore[arg-type]


def codes(**kw: object) -> list[str]:
    return [d.code for d in resolve_task(task(**kw), D).diagnostics if d.level == "error"]


def test_resolves_id_name_alias_and_explicit_login():
    r = resolve_task(task(assignees=["TV01", "lê  văn c", "@dev-b"], collaborators=["anh a"]), D)
    assert r.assignees == ["dev-a", "dev-c", "Dev-B"]  # @dev-b khớp danh bạ -> dùng login chuẩn
    assert resolve_task(task(assignees=["DEV-A"]), D).assignees == ["dev-a"]
    assert r.collaborators == ["dev-a"] and not r.blocked


def test_no_guessing():
    assert codes(assignees=["Van A"]) == ["UNKNOWN_MEMBER"]  # không fuzzy
    assert codes(assignees=["nguyen van a"]) == ["UNKNOWN_MEMBER"]  # không bỏ dấu
    assert codes(assignees=["dev"]) == ["UNKNOWN_MEMBER"]  # login phải khớp chính xác


@pytest.mark.parametrize("bad", ["@@x y", "@-bad", "@bad-", "@a--b", "@" + "a" * 40, "@dev$", "@"])
def test_invalid_login_blocked(bad: str):
    assert codes(assignees=[bad]) == ["INVALID_LOGIN"]
    assert codes(github=[bad.removeprefix("@")]) == ["INVALID_LOGIN"]


def test_login_conflict_and_unknown_login_blocked():
    assert codes(assignees=["TV01"], github=["dev-c"]) == ["LOGIN_CONFLICT"]
    assert codes(assignees=["TV01"], github=["DEV-A"]) == []
    assert codes(github=["someone-new"]) == ["UNKNOWN_LOGIN"]


def test_dedupe_and_collaborators_not_assigned():
    r = resolve_task(task(assignees=["TV01", "@dev-a"], collaborators=["TV02"]), D)
    assert r.assignees == ["dev-a"] and r.collaborators == ["Dev-B"]
    assert resolve_task(task(assignees=["TV01"], collaborators=["TV02"]), D, True).assignees == ["dev-a", "Dev-B"]


@pytest.mark.parametrize(
    "text",
    ["", "- [", "members: []", "members:\n  X1: {display_name: a, github: a}",
     "members:\n  TV01: {display_name: a, github: -bad}",
     "members:\n  TV01: {display_name: a, github: a, extra: 1}",
     "members:\n  TV01: {display_name: a, github: A}\n  TV02: {display_name: b, github: a}",
     "members:\n  TV01: {display_name: a, github: a1, aliases: [x]}\n  TV02: {display_name: b, github: b1, aliases: [X]}"],
)  # fmt: skip
def test_bad_directory_raises_clean_error(text: str):
    with pytest.raises(MembersError):
        Directory.from_text(text)


def test_load_members_workspace_guard(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "out.yaml").write_text(YAML)
    (ws / "m.yaml").write_text(YAML)
    assert len(load_members("m.yaml", ws).members) == 3
    for bad in ("../out.yaml", "missing.yaml"):
        with pytest.raises(MembersError):
            load_members(bad, ws)


def test_cli_block_and_bad_members(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    (tmp_path / "m.yaml").write_text(YAML)
    (tmp_path / "bad.yaml").write_text("{")
    (tmp_path / "t.md").write_text("## TASK-001: a\n- Phụ trách: Nobody\n")
    base = ["issues", "validate", "--workspace", str(tmp_path), "--input", "t.md"]
    assert main([*base, "--members", "m.yaml"]) == 1
    assert "BLOCK [UNKNOWN_MEMBER] TASK-001" in capsys.readouterr().out
    assert main([*base, "--members", "bad.yaml"]) == 2


def test_no_assignee_warns_without_blocking_or_guessing():
    r = resolve_task(task(collaborators=["TV01"]), D)
    assert not r.blocked and r.assignees == []
    assert [d.code for d in r.diagnostics] == ["NO_ASSIGNEE"] and r.diagnostics[0].level == "warning"


def test_member_ids_differing_only_by_case_rejected():
    with pytest.raises(MembersError):
        Directory.from_text("members:\n  TV01: {display_name: a, github: a1}\n  tv01: {display_name: b, github: b1}\n")
    assert resolve_task(task(assignees=["tv01"]), D).assignees == ["dev-a"]
