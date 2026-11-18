"""Offline characterization of the PLAN §7 backlog (BOT-001..BOT-018) through `issues plan` (fake GitHub, no network).

Purpose: show what the bot would create for the REAL backlog table before anyone runs it against a real repo.
The table below is a verbatim copy of docs/PLAN_GitHub_Automation_Bot_Claude.md §7 (docs/ is untracked).
Pins that every column of the table reaches the issue body (Epic, Deliverable, Phụ thuộc, P0-P2).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_github import TOKEN, FakeGitHub, assert_requests_safe, make_issue

from github_bot.cli import main

BACKLOG = """
| ID | Epic | Công việc | Ưu tiên | Deliverable | Phụ thuộc |
|---|---|---|---|---|---|
| BOT-001 | Foundation | Khởi tạo Python package + CLI | P0 | `pyproject.toml`, CLI help | — |
| BOT-002 | Foundation | Setup pytest/ruff/CI | P0 | Test workflow pass | BOT-001 |
| BOT-003 | Parser | Định nghĩa `TaskDraft` schema | P0 | Models + validation | BOT-001 |
| BOT-004 | Parser | Parse heading TASK | P0 | Parser + tests | BOT-003 |
| BOT-005 | Parser | Parse bảng Markdown tiếng Việt | P0 | Parser + tests | BOT-003 |
| BOT-006 | Identity | Định nghĩa members registry | P0 | `members.yaml` schema | BOT-003 |
| BOT-007 | Identity | Resolve + reject ambiguous names | P0 | Resolver + tests | BOT-006 |
| BOT-008 | GitHub | API client read-only | P0 | Repo/labels/assignees lookup | BOT-001 |
| BOT-009 | Planner | Issue source marker + dedupe | P0 | Planner + tests | BOT-004, BOT-005, BOT-008 |
| BOT-010 | Planner | Preview MD/JSON + plan hash | P0 | Dry-run artifact | BOT-009 |
| BOT-011 | Executor | Apply approved plan | P0 | Issue create + assignee | BOT-007, BOT-010 |
| BOT-012 | QA | Sandbox E2E + rerun idempotency | P0 | Logs + test report | BOT-011 |
| BOT-013 | Sync | Safe updates + conflict detection | P1 | Issue sync | BOT-012 |
| BOT-014 | Dependencies | Dependabot CLI adapter | P1 | Update artifacts | BOT-012 |
| BOT-015 | Pull Requests | Branch/commit/PR manager | P1 | Dependency PR demo | BOT-014 |
| BOT-016 | Governance | CI + policy + CODEOWNERS checks | P1 | PR gate report | BOT-015 |
| BOT-017 | AI Optional | Schema-constrained AI fallback | P2 | Adapter + tests | BOT-012 |
| BOT-018 | Platform | Multi-repo GitHub App | P2 | Installable app | BOT-016 |
"""
MEMBERS = """members:
  TV01: {display_name: "Nguyễn Văn A", github: "example-dev-1"}
"""
IDS = [f"BOT-{n:03d}" for n in range(1, 19)]


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.chdir(w)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    st = SimpleNamespace(ws=w, fake=FakeGitHub(labels=["documentation"]))
    orig_init = httpx.Client.__init__

    def patched(self: httpx.Client, *a: Any, **k: Any) -> None:
        k["transport"] = httpx.MockTransport(lambda req: st.fake(req))
        orig_init(self, *a, **k)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    yield st
    assert {r.method for r in st.fake.requests} <= {"GET"}, "planner must be read-only"
    assert_requests_safe(st.fake)


def run_plan(gh: Any, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, dict[str, Any]]:
    (gh.ws / "BACKLOG.md").write_text(BACKLOG, encoding="utf-8")
    (gh.ws / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    rc = main(["issues", "plan", "--input", "BACKLOG.md", "--members", "members.yaml", "--repo", "owner/repo",
               "--output-dir", "out", *extra])  # fmt: skip
    out = capsys.readouterr()
    text = out.out + out.err
    assert TOKEN not in text and "Traceback" not in text
    return rc, text, json.loads((gh.ws / "out" / "plan.json").read_text(encoding="utf-8"))


def test_backlog_table_yields_18_creates_without_block(gh, capsys) -> None:
    rc, _, plan = run_plan(gh, capsys)
    assert rc == 0
    assert [i["task_id"] for i in plan["items"]] == IDS
    assert {i["action"] for i in plan["items"]} == {"CREATE"}
    assert len(IDS) < 50  # max_tasks_per_run default


def test_backlog_titles_come_from_the_work_column_verbatim(gh, capsys) -> None:
    _, _, plan = run_plan(gh, capsys)
    titles = {i["task_id"]: i["title"] for i in plan["items"]}
    assert titles["BOT-001"] == "Khởi tạo Python package + CLI"
    assert titles["BOT-018"] == "Multi-repo GitHub App"
    assert all(t.strip() for t in titles.values())


def test_backlog_has_no_assignee_and_needs_no_label_or_member(gh, capsys) -> None:
    _, text, plan = run_plan(gh, capsys)
    assert all(i["assignees"] == [] and i["labels"] == [] for i in plan["items"])
    assert text.count("NO_ASSIGNEE") == 0 or "BLOCK" not in text.split("NO_ASSIGNEE")[0]  # warning only, never BLOCK
    # no assignee/label lookups were needed, so the backlog works on a repo with no labels at all
    assert not [r for r in gh.fake.requests if "/assignees/" in r.url.path]


def test_backlog_rerun_after_creation_is_all_skip(gh, capsys) -> None:
    gh.fake = FakeGitHub(
        labels=[],
        issues=[make_issue(n + 1, f"x\n\n<!-- github-automation-bot: source=BACKLOG.md; task_id={t}; schema=v1 -->\n")
                for n, t in enumerate(IDS)],
    )
    _, _, plan = run_plan(gh, capsys)
    assert {i["action"] for i in plan["items"]} == {"SKIP"}


def test_backlog_over_max_tasks_is_refused_not_truncated(gh, capsys) -> None:
    (gh.ws / "BACKLOG.md").write_text(BACKLOG, encoding="utf-8")
    (gh.ws / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    rc = main(["issues", "plan", "--input", "BACKLOG.md", "--members", "members.yaml", "--repo", "owner/repo",
               "--output-dir", "out", "--max-tasks", "10"])  # fmt: skip
    out = capsys.readouterr()
    assert rc != 0 and "Traceback" not in out.out + out.err
    plan = gh.ws / "out" / "plan.json"
    assert not plan.exists() or len(json.loads(plan.read_text(encoding="utf-8"))["items"]) == 18, "never a silent partial plan"


def _body(plan: dict[str, Any], task_id: str = "BOT-004") -> str:
    return next(i["body"] for i in plan["items"] if i["task_id"] == task_id)


def test_epic_column_is_kept_somewhere(gh, capsys) -> None:
    _, _, plan = run_plan(gh, capsys)
    assert "- Epic: Parser" in _body(plan) and not any(i["labels"] for i in plan["items"])  # Epic is body text, never a label


def test_deliverable_column_is_kept_somewhere(gh, capsys) -> None:
    _, _, plan = run_plan(gh, capsys)
    assert "- [ ] Parser + tests" in _body(plan)


def test_dependency_column_is_kept_somewhere(gh, capsys) -> None:
    _, _, plan = run_plan(gh, capsys)
    assert "- Phụ thuộc: BOT-003" in _body(plan)  # the body only: warnings that mention BOT-003 as a task id do not count


def test_p0_p1_p2_priorities_are_understood(gh, capsys) -> None:
    _, text, plan = run_plan(gh, capsys)
    assert "UNKNOWN_PRIORITY" not in text and "- Ưu tiên: High" in _body(plan)


def test_no_column_of_the_backlog_is_reported_as_ignored_and_no_block(gh, capsys) -> None:
    rc, text, _ = run_plan(gh, capsys)
    assert rc == 0 and "UNKNOWN_COLUMN" not in text and "BLOCK" not in text.replace("0 BLOCK", "")


def test_every_backlog_issue_carries_epic_dependencies_and_deliverable(gh, capsys) -> None:
    _, _, plan = run_plan(gh, capsys)
    for it in plan["items"]:
        assert "- Epic: " in it["body"] and "- [ ] " in it["body"], it["task_id"]
        assert it["body"].count("<!-- github-automation-bot:") == 1
    assert "- Phụ thuộc: BOT-004, BOT-005, BOT-008" in _body(plan, "BOT-009")
    assert "Phụ thuộc" not in _body(plan, "BOT-001"), "the em dash means 'none'"


HOSTILE = """| ID | Epic | Công việc | Phụ thuộc |
|---|---|---|---|
| E-1 | @victim #1 https://evil.example/x owner/repo#2 | Một | @victim, #5, https://x.io |
| E-2 | a<br>b | Hai | --> inj |
| E-3 | x\u202ey\u200bz\u2066w | Ba | E-1\u202e |
"""


def test_epic_and_dependencies_get_the_same_neutralization_as_the_description(gh, capsys) -> None:
    (gh.ws / "BACKLOG.md").write_text(HOSTILE, encoding="utf-8")
    (gh.ws / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    rc = main(["issues", "plan", "--input", "BACKLOG.md", "--members", "members.yaml", "--repo", "owner/repo", "--output-dir", "out"])
    capsys.readouterr()
    assert rc == 0
    plan = json.loads((gh.ws / "out" / "plan.json").read_text(encoding="utf-8"))
    for it in plan["items"]:
        extra = it["body"].split("## Thông tin thêm")[1].split("## Nguồn")[0]
        assert not re.findall(r"(?<!`)@\w+|(?<!`)#\d+|(?<!`)://", extra), extra
        assert not [c for c in it["body"] if c in "\u202e\u200b\u2066"]
        assert it["body"].count("<!-- github-automation-bot:") == 1
        assert it["body"].rstrip().splitlines()[-1].startswith("<!-- github-automation-bot:")


def test_overlong_epic_is_a_clean_error_not_a_crash_or_truncation(gh, capsys) -> None:
    (gh.ws / "BACKLOG.md").write_text("| ID | Epic | Công việc |\n|---|---|---|\n| E-1 | " + "L" * 150 + " | Một |\n", encoding="utf-8")
    (gh.ws / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    rc = main(["issues", "plan", "--input", "BACKLOG.md", "--members", "members.yaml", "--repo", "owner/repo", "--output-dir", "out"])
    out = capsys.readouterr()
    assert rc != 0 and "INVALID_TASK" in out.out + out.err and "Traceback" not in out.out + out.err
    assert not (gh.ws / "out" / "plan.json").exists()


def test_unknown_column_is_a_warning_naming_the_column_not_an_error(gh, capsys) -> None:
    (gh.ws / "BACKLOG.md").write_text("| ID | Công việc | Foo |\n|---|---|---|\n| E-1 | Một | x |\n", encoding="utf-8")
    (gh.ws / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    rc = main(["issues", "plan", "--input", "BACKLOG.md", "--members", "members.yaml", "--repo", "owner/repo", "--output-dir", "out"])
    out = capsys.readouterr()
    assert rc == 0 and "UNKNOWN_COLUMN" in out.out and "'Foo'" in out.out
