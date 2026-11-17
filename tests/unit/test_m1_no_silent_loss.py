"""M1 fail-closed: a task must never vanish without a diagnostic (spec §3.2 "cảnh báo yêu cầu
chuẩn hóa thay vì phân tích sai", §4.2 fail closed). Black-box on parse_markdown/load_markdown."""

from pathlib import Path

import pytest

from github_bot.markdown.loader import load_markdown
from github_bot.markdown.parsers import parse_markdown

HDR = "| Mã | Công việc |\n"

# Input where the author clearly intended tasks but the parser yields none/fewer.
# Expectation: tasks are parsed OR at least one diagnostic (warning/error) is emitted.
CASES = {
    "table_without_separator": HDR + "| T01 | a |\n| T02 | b |\n",
    "separator_single_dash_aligned": HDR + "|:-:|:-:|\n| T01 | a |\n",
    "separator_two_dashes": HDR + "|--|--|\n| T01 | a |\n",
    "table_without_leading_pipe": "Mã | Công việc\n---|---\nT01 | a\n",
    "unclosed_code_fence_swallows_tasks": "```\nnote\n## TASK-001: a\n- Phụ trách: TV01\n",
    "task_heading_level_3": "### TASK-001: a\n- Phụ trách: TV01\n",
    "blank_line_splits_table": HDR + "|---|---|\n| T01 | a |\n\n| T02 | b |\n",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_silent_task_loss(name: str) -> None:
    r = parse_markdown(CASES[name], "x.md")
    assert r.tasks or r.diagnostics, f"{name}: nothing parsed and no diagnostic (silent loss)"


def test_blank_line_split_table_reports_or_keeps_second_row() -> None:
    r = parse_markdown(CASES["blank_line_splits_table"], "x.md")
    ids = {t.id for t in r.tasks}
    assert ids == {"T01", "T02"} or r.diagnostics, f"T02 silently dropped, got {ids}"


def test_zero_tasks_is_not_a_clean_pass() -> None:
    for md in ("", "# Chỉ có văn bản\nkhông có task\n"):
        r = parse_markdown(md, "x.md")
        assert r.tasks == []
        assert r.diagnostics, "file with zero tasks must warn/err, not pass silently"


def test_utf8_bom_does_not_hide_first_heading(tmp_path: Path) -> None:
    (tmp_path / "bom.md").write_bytes("﻿## TASK-001: a\n".encode())
    text, src = load_markdown("bom.md", tmp_path)
    r = parse_markdown(text, src)
    assert [t.id for t in r.tasks] == ["TASK-001"] or r.diagnostics


def test_loader_size_limit_and_non_utf8(tmp_path: Path) -> None:
    from github_bot.markdown.loader import MAX_BYTES, LoaderError

    (tmp_path / "big.md").write_bytes(b"#" * (MAX_BYTES + 1))
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "notmd.txt").write_text("x")
    for n in ("big.md", "bin.md", "notmd.txt"):
        with pytest.raises(LoaderError):
            load_markdown(n, tmp_path)


# Partial loss: one good task present, a second task-like item dropped. The "0 tasks" backstop
# (NO_TASKS) cannot catch these, so each dropped item itself needs a diagnostic.
_OK = "## TASK-001: ok\n- Phụ trách: TV01\n\n"
_TBL = "| Mã | Công việc |\n|---|---|\n| T01 | a |\n\n"
PARTIAL = {
    "heading_no_separator_TASK001": _OK + "## TASK001: b\n",
    "heading_underscore": _OK + "## TASK_001: b\n",
    "heading_hash_number": _OK + "## Task #2: b\n",
    "heading_no_space_after_hashes": _OK + "##TASK-002: b\n",
    "orphan_row_different_width": _TBL + "| T02 | b | c |\n",
    "orphan_row_single_cell": _TBL + "| T02 |\n",
}


@pytest.mark.parametrize("name", sorted(PARTIAL))
def test_no_silent_partial_loss(name: str) -> None:
    r = parse_markdown(PARTIAL[name], "x.md")
    dropped_ok = len(r.tasks) >= 2
    assert dropped_ok or r.diagnostics, f"{name}: second task dropped with no diagnostic"


# --- M1.5 (Task B): written BEFORE the fix, red until coder lands it. ---
_GOOD = "## TASK-001: ok\n- Phụ trách: TV01\n\n"
TASKISH_HEADINGS = [
    "## TASK–002: b",  # en dash
    "## TASK.002: b",
    "## **TASK-002**: b",
    "## [TASK-002] b",
    "## 1. TASK-002: b",
]
NOT_TASKS = ["## TASKS", "## Task list", "## Tasking notes", "## Taskforce", "# TASKS — My Project"]


@pytest.mark.parametrize("heading", TASKISH_HEADINGS)
def test_taskish_heading_variants_report_malformed_id(heading: str) -> None:
    r = parse_markdown(_GOOD + heading + "\n- Phụ trách: TV02\n", "x.md")
    assert [t.id for t in r.tasks] == ["TASK-001"]
    assert "MALFORMED_ID" in {d.code for d in r.diagnostics}, heading


@pytest.mark.parametrize("heading", NOT_TASKS)
def test_non_task_headings_not_flagged(heading: str) -> None:
    r = parse_markdown(heading + "\n" + _GOOD, "x.md")
    assert [t.id for t in r.tasks] == ["TASK-001"]
    assert r.diagnostics == [], [d.code for d in r.diagnostics]


def test_valid_four_backtick_fence_is_not_unclosed() -> None:
    md = _GOOD + "````\n```\n## TASK-002: hidden\n````\n"
    r = parse_markdown(md, "x.md")
    assert "UNCLOSED_FENCE" not in {d.code for d in r.diagnostics}
    assert [t.id for t in r.tasks] == ["TASK-001"]  # fenced content stays data, not a task


def test_html_comment_heading_is_not_a_task_and_warns() -> None:
    r = parse_markdown(_GOOD + "<!--\n## TASK-002: hidden\n-->\n", "x.md")
    assert [t.id for t in r.tasks] == ["TASK-001"]
    assert any(d.level == "warning" for d in r.diagnostics), "commented task must warn"
    # one-line comment form too
    r = parse_markdown(_GOOD + "<!-- ## TASK-003: hidden -->\n", "x.md")
    assert [t.id for t in r.tasks] == ["TASK-001"]
