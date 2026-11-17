"""M2a black-box CLI tests: `issues validate --members` (spec §3.1, §3.3, §10 T03/T04/T12/T17).

Contract assumed (flag to leader if coder differs):
  github-bot issues validate --input TASKS.md --members members.yaml [--workspace DIR]
  - members.yaml is loaded with yaml.safe_load, must live inside the workspace.
  - exit 0 = all tasks resolvable; != 0 = any BLOCK / invalid members file / path error.
  - diagnostics on stdout/stderr, no Traceback, offline, writes nothing.
Markdown and YAML are inline (no fixture files).
"""

import socket
from pathlib import Path

import pytest

from github_bot.cli import main

MEMBERS_OK = """members:
  TV01:
    display_name: "Nguyễn Văn A"
    github: "example-dev-1"
    aliases: ["Anh A"]
  TV02:
    display_name: "Trần Văn B"
    github: "example-dev-2"
    aliases: []
  TV03:
    display_name: "Lê Văn C"
    github: "example-dev-3"
    aliases: []
"""


def task(assignee: str = "TV02", extra: str = "") -> str:
    return f"## TASK-001: Việc\n- Phụ trách: {assignee}\n{extra}"


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.chdir(w)
    return w


def run(
    ws: Path, capsys: pytest.CaptureFixture[str], md: str | None, members: str | None,
    members_path: str = "members.yaml",
) -> tuple[int, str]:
    if md is not None:
        (ws / "TASKS.md").write_text(md, encoding="utf-8")
    if members is not None:
        (ws / "members.yaml").write_text(members, encoding="utf-8")
    try:
        rc = main(["issues", "validate", "--input", "TASKS.md", "--members", members_path])
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    cap = capsys.readouterr()
    out = cap.out + cap.err
    if "invalid choice" in out or "unrecognized arguments" in out:
        pytest.fail(f"CLI contract not available: {out.strip()[:200]}")
    return int(rc or 0), out


# ---- resolution through the CLI (T02 / T03 / T04) ----------------------------------------

@pytest.mark.parametrize(
    "assignee",
    ["TV02", "tv02", "Trần Văn B", "@example-dev-2", "@Example-Dev-2", "example-dev-2",
     "EXAMPLE-DEV-2", "TV01, TV03", "TV01, Anh A", "TV01, @example-dev-1"],
)
def test_resolvable_assignees_pass(ws, capsys, assignee) -> None:
    rc, out = run(ws, capsys, task(assignee), MEMBERS_OK)
    assert rc == 0, out


@pytest.mark.parametrize(
    ("assignee", "must_mention"),
    [
        ("TV99", "TV99"),                      # T03: no mapping
        ("Hoàng Văn Z", "Hoàng Văn Z"),         # real name without mapping, never guessed
        ("Anh", "Anh"),                         # prefix of an alias: no fuzzy match
        ("Nguyen Van A", "Nguyen Van A"),       # diacritics stripped: no fuzzy match
        ("@not-in-registry-xyz", "not-in-registry-xyz"),   # leader decision 1: BLOCK
        ("example-dev-9", "example-dev-9"),                # bare login not in registry: BLOCK
    ],
)
def test_unmapped_assignee_blocks(ws, capsys, assignee, must_mention) -> None:
    rc, out = run(ws, capsys, task(assignee), MEMBERS_OK)
    assert rc != 0 and "Traceback" not in out
    assert must_mention in out


def test_task_without_assignee_passes_with_no_assignee_warning(ws, capsys) -> None:
    rc, out = run(ws, capsys, "## TASK-001: Việc\n- Ưu tiên: High\n", MEMBERS_OK)
    assert rc == 0, out
    assert "NO_ASSIGNEE" in out and "Traceback" not in out


def test_login_conflicting_with_member_id_blocks_and_names_both(ws, capsys) -> None:
    # T04: TV02 maps to example-dev-2 but the task states @example-dev-1 -> BLOCK, no side chosen
    rc, out = run(ws, capsys, task("TV02", "- GitHub: @example-dev-1\n"), MEMBERS_OK)
    assert rc != 0
    assert "TV02" in out and "example-dev-1" in out


def test_login_matching_member_id_is_not_a_conflict(ws, capsys) -> None:
    rc, out = run(ws, capsys, task("TV02", "- GitHub: @example-dev-2\n"), MEMBERS_OK)
    assert rc == 0, out


@pytest.mark.parametrize(
    "bad", ["@@x y", "-bad", "bad-", "a--b", "x" * 40, "a_b", "a.b", "a b", "a$(id)", "ư"]
)
def test_invalid_github_login_blocks(ws, capsys, bad) -> None:
    rc, out = run(ws, capsys, task("TV02", f"- GitHub: {bad}\n"), MEMBERS_OK)
    assert rc != 0 and "Traceback" not in out, bad


@pytest.mark.parametrize("bad", ["@-bad", "@a--b", "@" + "x" * 40, "@a_b", "@"])
def test_invalid_login_in_assignee_column_blocks(ws, capsys, bad) -> None:
    rc, out = run(ws, capsys, task(bad), MEMBERS_OK)
    assert rc != 0 and "Traceback" not in out, bad


def test_login_in_table_assignee_column_is_handled(ws, capsys) -> None:
    md = "| Mã | Công việc | Phụ trách |\n|---|---|---|\n| T01 | a | @example-dev-3 |\n"
    rc, out = run(ws, capsys, md, MEMBERS_OK)
    assert rc == 0, out
    md = "| Mã | Công việc | Phụ trách |\n|---|---|---|\n| T01 | a | @@x y |\n"
    rc, _ = run(ws, capsys, md, MEMBERS_OK)
    assert rc != 0


def test_one_blocked_task_fails_the_run_but_reports_all(ws, capsys) -> None:
    md = task("TV02") + "\n## TASK-002: Hai\n- Phụ trách: TV99\n\n## TASK-003: Ba\n- Phụ trách: TV88\n"
    rc, out = run(ws, capsys, md, MEMBERS_OK)
    assert rc != 0
    assert "TV99" in out and "TV88" in out  # every BLOCK is reported, not just the first


# ---- members.yaml validation --------------------------------------------------------------

BAD_MEMBERS = {
    "not_yaml": "members: [unclosed\n  : : :",
    "not_a_mapping": "- just\n- a list\n",
    "empty_file": "",
    "no_members_key": "people:\n  TV01: {display_name: A, github: example-dev-1}\n",
    "member_missing_github": "members:\n  TV01:\n    display_name: A\n",
    "member_missing_display_name": "members:\n  TV01:\n    github: example-dev-1\n",
    "login_not_string": "members:\n  TV01:\n    display_name: A\n    github: 12345\n",
    "invalid_login_format": "members:\n  TV01:\n    display_name: A\n    github: 'bad login'\n",
    "member_ids_differ_only_by_case": (
        "members:\n  TV01: {display_name: A, github: example-dev-1}\n"
        "  tv01: {display_name: B, github: example-dev-2}\n"
    ),
    "alias_equals_other_members_login": (
        "members:\n  TV01: {display_name: A, github: example-dev-1, aliases: ['example-dev-2']}\n"
        "  TV02: {display_name: B, github: example-dev-2}\n"
    ),
    "display_name_equals_other_members_login": (
        "members:\n  TV01: {display_name: example-dev-2, github: example-dev-1}\n"
        "  TV02: {display_name: B, github: example-dev-2}\n"
    ),
    "login_with_trailing_newline": (
        "members:\n  TV01:\n    display_name: A\n    github: |\n      example-dev-1\n"
    ),
    "blank_display_name": "members:\n  TV01: {display_name: ' ', github: example-dev-1}\n",
    "blank_alias": "members:\n  TV01: {display_name: A, github: example-dev-1, aliases: ['']}\n",
    "duplicate_login_two_people": (
        "members:\n  TV01: {display_name: A, github: example-dev-1}\n"
        "  TV02: {display_name: B, github: example-dev-1}\n"
    ),
    "duplicate_login_case_variant": (
        "members:\n  TV01: {display_name: A, github: example-dev-1}\n"
        "  TV02: {display_name: B, github: Example-Dev-1}\n"
    ),
    "duplicate_alias_two_people": (
        "members:\n  TV01: {display_name: A, github: example-dev-1, aliases: ['Anh']}\n"
        "  TV02: {display_name: B, github: example-dev-2, aliases: ['Anh']}\n"
    ),
    "alias_equals_other_member_id": (
        "members:\n  TV01: {display_name: A, github: example-dev-1, aliases: ['TV02']}\n"
        "  TV02: {display_name: B, github: example-dev-2}\n"
    ),
    "duplicate_display_name_two_people": (
        "members:\n  TV01: {display_name: Same, github: example-dev-1}\n"
        "  TV02: {display_name: Same, github: example-dev-2}\n"
    ),
    "aliases_not_a_list": (
        "members:\n  TV01: {display_name: A, github: example-dev-1, aliases: 'Anh'}\n"
    ),
}


@pytest.mark.parametrize("name", sorted(BAD_MEMBERS))
def test_bad_members_file_is_rejected_cleanly(ws, capsys, name) -> None:
    rc, out = run(ws, capsys, task("TV01"), BAD_MEMBERS[name])
    assert rc != 0, name
    assert out.strip() and "Traceback" not in out, out


def test_duplicate_login_diagnostic_names_both_people(ws, capsys) -> None:
    _, out = run(ws, capsys, task("TV01"), BAD_MEMBERS["duplicate_login_two_people"])
    assert "TV01" in out and "TV02" in out


def test_members_file_missing_is_rejected(ws, capsys) -> None:
    rc, out = run(ws, capsys, task("TV01"), None, members_path="nope.yaml")
    assert rc != 0 and "Traceback" not in out


def test_members_outside_workspace_rejected(ws, tmp_path, capsys) -> None:
    (tmp_path / "outside.yaml").write_text(MEMBERS_OK, encoding="utf-8")
    (ws / "link.yaml").symlink_to(tmp_path / "outside.yaml")
    for p in ("../outside.yaml", str(tmp_path / "outside.yaml"), "link.yaml", "/etc/hostname"):
        rc, out = run(ws, capsys, task("TV01"), None, members_path=p)
        assert rc != 0 and "Traceback" not in out, p


def test_members_file_too_large_rejected(ws, capsys) -> None:
    big = MEMBERS_OK + "# " + "x" * 2_000_000 + "\n"
    rc, out = run(ws, capsys, task("TV01"), big)
    assert rc != 0 and "Traceback" not in out


# ---- injection: YAML and Markdown are data (T12) ------------------------------------------

@pytest.mark.parametrize(
    "yaml_text",
    [
        'members:\n  TV01:\n    display_name: "$(touch PWNED_name)"\n    github: example-dev-1\n',
        (
            "members:\n  TV01:\n    display_name: A\n    github: example-dev-1\n"
            '    aliases: ["`touch PWNED_alias`", "; touch PWNED_semi"]\n'
        ),
        'members:\n  TV01: !!python/object/apply:os.system ["touch PWNED_yaml"]\n',
        'members: !!python/object/new:subprocess.Popen [["touch", "PWNED_popen"]]\n',
        "members:\n  TV01: &a {display_name: A, github: example-dev-1}\n  TV02: *a\n",
    ],
)
def test_members_yaml_is_data_never_executed(ws, capsys, yaml_text) -> None:
    rc, out = run(ws, capsys, task("TV01"), yaml_text)
    assert "Traceback" not in out
    assert not list(ws.glob("PWNED*")), "members.yaml content was executed"
    assert not list(Path.cwd().glob("PWNED*"))
    if "!!python" in yaml_text:
        assert rc != 0  # unsafe tag must be rejected, not silently ignored


def test_shell_text_in_assignee_cell_is_just_an_unknown_name(ws, capsys) -> None:
    rc, out = run(ws, capsys, task("$(touch PWNED_cell) `touch PWNED_tick`"), MEMBERS_OK)
    assert rc != 0 and "Traceback" not in out
    assert not list(ws.glob("PWNED*"))


# ---- offline, deterministic, read-only ----------------------------------------------------

def test_validate_with_members_is_offline_deterministic_and_writes_nothing(
    ws, capsys, monkeypatch
) -> None:
    def no_net(*a: object, **k: object) -> None:
        raise AssertionError("network used by offline validate")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    first = run(ws, capsys, task("TV01, TV03"), MEMBERS_OK)
    before = sorted(p.name for p in ws.iterdir())
    second = run(ws, capsys, None, None)
    assert first == second and first[0] == 0
    assert sorted(p.name for p in ws.iterdir()) == before


# ---- CLI summary line counts every warning (resolver warnings included) ------------------------

def test_summary_line_counts_resolver_warnings_and_blocks(ws, capsys) -> None:
    md = (
        "## TASK-001: Một\n- Phụ trách: TV01\n\n"
        "## TASK-002: Hai\n- Ưu tiên: High\n\n"      # NO_ASSIGNEE -> 1 warning
        "## TASK-003: Ba\n- Ưu tiên: Low\n\n"          # NO_ASSIGNEE -> 2 warnings
        "## TASK-004: Bốn\n- Phụ trách: TV99\n"         # 1 BLOCK
    )
    rc, out = run(ws, capsys, md, MEMBERS_OK)
    assert rc != 0
    summary = [ln for ln in out.splitlines() if "task" in ln and "BLOCK" in ln][-1]
    assert "4 task" in summary and "2 cảnh báo" in summary and "1 BLOCK" in summary, summary


def test_summary_line_zero_when_clean(ws, capsys) -> None:
    rc, out = run(ws, capsys, task("TV01"), MEMBERS_OK)
    assert rc == 0
    summary = [ln for ln in out.splitlines() if "task" in ln][-1]
    assert "0 lỗi" in summary and "0 cảnh báo" in summary and "0 BLOCK" in summary, summary
