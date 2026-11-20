from pathlib import Path

import pytest

from github_bot.cli import main
from github_bot.markdown.loader import LoaderError, load_markdown
from github_bot.markdown.parsers import parse_markdown, split_row

MD = """# T
## TASK-001: Tạo API
- Phụ trách: TV02, TV03
- GitHub: @dev-2
- Ưu tiên: Cao
- Labels: backend, auth

### Mô tả
Dòng 1
### Tiêu chí nghiệm thu
- [ ] A
- [x] B

| Mã | NHIỆM VỤ | Owner | Nhãn | Deliverables |
|---|---|---|---|---|
| T01 | DB `a\\|b` | TV01<br>TV03 | db | x<br>y |
| T01 | trùng | TV01 | | |
|  | thiếu mã | TV01 | | |
"""


def test_parse_both_formats_and_diagnostics():
    r = parse_markdown(MD, "docs/T.md")
    a, t = r.tasks
    assert (a.id, a.assignees, a.github, a.priority) == ("TASK-001", ["TV02", "TV03"], ["dev-2"], "high")
    assert a.acceptance_criteria == ["A", "B"] and a.description == "Dòng 1"
    assert (t.id, t.title, t.assignees, t.acceptance_criteria) == ("T01", "DB `a|b`", ["TV01", "TV03"], ["x", "y"])
    assert sorted(d.code for d in r.errors) == ["DUPLICATE_ID", "MISSING_ID"]


def test_duplicate_across_formats_and_missing_column():
    r = parse_markdown("## TASK-001: a\n\n| id | Task |\n|---|---|\n| TASK-001 | b |\n", "x.md")
    assert [d.code for d in r.errors] == ["DUPLICATE_ID"] and "TASK-001" in r.errors[0].message
    r = parse_markdown("| Mã | Owner |\n|---|---|\n| T1 | a |\n", "x.md")
    assert {d.code for d in r.errors} == {"MISSING_COLUMN", "NO_TASKS"}


def test_fenced_content_ignored_and_malformed_heading():
    r = parse_markdown("```\n## TASK-009: x\n```\n## TASK: thiếu\n", "x.md")
    assert r.tasks == [] and sorted(d.code for d in r.errors) == ["MALFORMED_ID", "NO_TASKS"]


def test_split_row_escape():
    assert split_row(r"| a \| b | `c|d` | e |") == ["a | b", "`c|d`", "e"]


def test_loader_blocks_outside(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "out.md").write_text("x")
    (ws / "ok.md").write_text("x")
    (ws / "link.md").symlink_to(tmp_path / "out.md")
    assert load_markdown("ok.md", ws) == ("x", "ok.md")
    for bad in ("../out.md", str(tmp_path / "out.md"), "link.md", "missing.md"):
        with pytest.raises(LoaderError):
            load_markdown(bad, ws)


def test_cli_validate_exit_codes(tmp_path: Path, capsys):
    (tmp_path / "a.md").write_text("## TASK-001: ok\n")
    (tmp_path / "b.md").write_text("## TASK-001: a\n## TASK-001: b\n")
    (tmp_path / "m.yaml").write_text("members:\n  TV01: {display_name: A, github: dev-a}\n")
    base = ["issues", "validate", "--workspace", str(tmp_path), "--members", "m.yaml"]
    assert main([*base, "--input", "a.md"]) == 0
    assert main([*base, "--input", "b.md"]) == 1
    assert main([*base, "--input", "../x.md"]) == 2


def codes(md: str) -> set[str]:
    return {d.code for d in parse_markdown(md, "x.md").diagnostics}


def test_fail_closed_cases():
    assert "NOT_STANDARD_TABLE" in codes("| Mã | Công việc |\n| T01 | a |\n")
    assert "UNCLOSED_FENCE" in codes("```\n## TASK-001: a\n")
    assert "WRONG_HEADING_LEVEL" in codes("### TASK-001: a\n")
    assert "ORPHAN_TABLE_ROW" in codes("| Mã | Công việc |\n|:-:|:-:|\n| T01 | a |\n\n| T02 | b |\n")
    assert "NO_TASKS" in codes("")
    assert "DUPLICATE_ID" in codes("| Mã | Công việc |\n|--|--|\n| T01 | a |\n| t01 | b |\n")
    assert "NO_TASKS" not in codes("Mã | Công việc\n---|---\nT01 | a\n")


def test_malformed_lookalike_headings_and_long_id():
    ok = "## TASK-001: a\n"
    for bad in ("## TASK001: b", "## TASK_001: b", "## Task #2: b", "##TASK-002: b"):
        assert "MALFORMED_ID" in codes(ok + bad + "\n"), bad
    assert "MALFORMED_ID" not in codes(ok + "## TASKS overview\n## Task list\n")
    r = parse_markdown("## TASK-" + "1" * 5000 + ": a\n", "x.md")
    assert r.tasks == [] and all(len(d.message) < 200 for d in r.diagnostics)
    tbl = "| Mã | Công việc |\n|---|---|\n| T01 | a |\n\n"
    assert "ORPHAN_TABLE_ROW" in codes(tbl + "| T02 | b | c |\n")
    assert "ORPHAN_TABLE_ROW" in codes(tbl + "| T02 |\n")


def test_unclosed_comment_and_commented_task_warning():
    assert "UNCLOSED_COMMENT" in codes("## TASK-001: a\n<!--\n## TASK-002: b\n")
    r = parse_markdown("## TASK-001: a\n<!-- ## TASK-002: b -->\n", "x.md")
    assert [t.id for t in r.tasks] == ["TASK-001"] and [d.code for d in r.diagnostics] == ["COMMENTED_TASK"]


@pytest.mark.parametrize(
    ("hidden", "expected"),
    [
        pytest.param(
            "| Mã | Công việc |\n|---|---|\n| T02 | Ẩn |",
            [("warning", "COMMENTED_TASK", 2)],
            id="task-table",
        ),
        pytest.param(
            "- Phụ trách: TV99",
            [("warning", "COMMENTED_TASK", 2)],
            id="task-field",
        ),
        pytest.param(
            "- Labels:",
            [("warning", "COMMENTED_TASK", 2)],
            id="empty-task-field",
        ),
        pytest.param(
            "## TASK-002: Ẩn\n## TASK-003: Cũng ẩn\n- Labels:",
            [("warning", "COMMENTED_TASK", 2)],
            id="one-warning-at-opening-line",
        ),
        pytest.param("| Mã | Trạng thái |\n|---|---|\n| T02 | Xong |", [], id="table-without-title"),
    ],
)
def test_closed_comment_warns_without_changing_visible_task(hidden, expected) -> None:
    before = "## TASK-001: Công việc\n"
    after = "- Phụ trách: TV01\n- Labels: backend\n\n### Mô tả\nMô tả bên ngoài.\n"
    visible = parse_markdown(before + after, "x.md")
    result = parse_markdown(f"{before}<!--\n{hidden}\n-->\n{after}", "x.md")

    assert [task.model_dump(exclude={"source_reference"}) for task in result.tasks] == [
        task.model_dump(exclude={"source_reference"}) for task in visible.tasks
    ]
    assert [(d.level, d.code, d.line) for d in result.diagnostics] == expected
