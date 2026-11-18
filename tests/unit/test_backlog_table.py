from pathlib import Path

from github_bot.markdown.parsers import parse_markdown
from github_bot.planning.planner import build_body

BACKLOG = Path(__file__).resolve().parents[2] / "docs" / "IMPLEMENTATION_TASKS.md"


def test_backlog_file_parses_fully_without_silent_loss():
    r = parse_markdown(BACKLOG.read_text(encoding="utf-8"), "docs/IMPLEMENTATION_TASKS.md")
    assert len(r.tasks) == 18 and r.diagnostics == []
    t = r.tasks[1]
    assert (t.id, t.priority, t.epic, t.dependencies) == ("BOT-002", "high", "Foundation", ["BOT-001"])
    assert t.acceptance_criteria == ["Test workflow pass"] and r.tasks[0].dependencies == []
    assert [x.priority for x in r.tasks if x.id in ("BOT-013", "BOT-017")] == ["medium", "low"]
    body = build_body(t, [], [])
    assert "- Epic: Foundation" in body and "- Phụ thuộc: BOT-001" in body and "- [ ] Test workflow pass" in body
    assert t.labels == []  # Epic KHÔNG thành label


def test_unknown_columns_are_reported_not_dropped_silently():
    md = "| ID | Công việc | Foo | Trạng thái |\n|---|---|---|---|\n| T1 | a | x | y |\n"
    r = parse_markdown(md, "x.md")
    assert [d.code for d in r.diagnostics] == ["UNKNOWN_COLUMN"]
    assert "'Foo'" in r.diagnostics[0].message and "'Trạng thái'" in r.diagnostics[0].message
    assert len(r.tasks) == 1


def test_task_without_extra_sections_keeps_old_body():
    md = "| ID | Công việc |\n|---|---|\n| T1 | a |\n"
    assert "Thông tin thêm" not in build_body(parse_markdown(md, "x.md").tasks[0], [], [])
