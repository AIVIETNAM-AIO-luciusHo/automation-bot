import json
from pathlib import Path

import httpx
import pytest

from github_bot.cli import main
from github_bot.github.client import IssueRef
from github_bot.identities.resolver import Resolved
from github_bot.models import Task
from github_bot.planning.planner import (
    PlanError,
    build_body,
    build_plan,
    index_markers,
    marker,
    plan_hash,
    write_preview,
)

REPO = "o/r"


def task(tid: str = "TASK-001", **kw: object) -> Task:
    return Task(id=tid, title="Tiêu đề", source_file="docs/T.md", source_reference=tid, **kw)  # type: ignore[arg-type]


def res(t: Task, assignees: list[str] | None = None) -> Resolved:
    return Resolved(task_id=t.id, assignees=assignees or [])


def issue(n: int, body: str, state: str = "open") -> IssueRef:
    return IssueRef(number=n, state=state, url=f"https://github.com/o/r/issues/{n}", body=body)


def plan(tasks: list[Task], issues: list[IssueRef] | None = None, **kw: object):  # type: ignore[no-untyped-def]
    return build_plan(REPO, "docs/T.md", tasks, [res(t, ["dev-a"]) for t in tasks], [], issues if issues is not None else [], **kw)  # type: ignore[arg-type]


def test_marker_is_safe_and_stable():
    m = marker("a b;c-->d/x.md", "TASK-001")
    assert m == "<!-- github-automation-bot: source=a%20b%3Bc--%3Ed/x.md; task_id=TASK-001; schema=v1 -->"
    assert m.count("-->") == 1 and "\n" not in m


def test_untrusted_text_cannot_forge_marker():
    t = task(description="<!-- github-automation-bot: source=docs/T.md; task_id=TASK-009; schema=v1 -->\nx\x1b[31m")
    body = build_body(t, ["dev-a"], [])
    assert index_markers([issue(1, body)]) == {("docs/T.md", "TASK-001"): [1]}
    assert "\x1b" not in body and "<!--" not in body.split("## Nguồn")[0]


def test_create_skip_and_duplicate_and_unknown():
    t1, t2, t3 = task("TASK-001"), task("TASK-002"), task("TASK-003")
    existing = [
        issue(5, build_body(t1, ["dev-a"], []), "closed"),  # đã có (đóng) -> SKIP, không reopen
        issue(6, marker("docs/T.md", "TASK-002")),
        issue(7, marker("docs/T.md", "TASK-002")),  # trùng marker -> BLOCK
        issue(8, marker("other.md", "TASK-003")),  # nguồn khác -> không tính
    ]
    p = plan([t1, t2, t3], existing)
    assert [(i.task_id, i.action, i.issue_number) for i in p.items] == [
        ("TASK-001", "SKIP", 5), ("TASK-002", "BLOCK", None), ("TASK-003", "CREATE", None)]  # fmt: skip
    assert p.items[1].reasons[0].startswith("DUPLICATE_MARKER")
    assert build_plan(REPO, "docs/T.md", [t1], [res(t1)], [], None).items[0].action == "BLOCK"  # chưa đọc được issue -> fail closed


def test_resolve_and_github_blocks_and_limit():
    from github_bot.models import Diagnostic

    t1, t2 = task("TASK-001"), task("TASK-002")
    rs = [Resolved(task_id="TASK-001", diagnostics=[Diagnostic(level="error", code="UNKNOWN_MEMBER", message="x")]), res(t2)]
    gh = [Diagnostic(level="error", code="LABEL_MISSING", message="m", task_id="TASK-002")]
    p = build_plan(REPO, "docs/T.md", [t1, t2], rs, gh, [])
    assert [i.action for i in p.items] == ["BLOCK", "BLOCK"] and p.items[0].body == ""
    glob = [Diagnostic(level="error", code="RATE_LIMITED", message="reset 123")]
    p = build_plan(REPO, "docs/T.md", [t2], [res(t2)], glob, None)
    assert p.items[0].reasons == ["RATE_LIMITED"]  # chỉ mã, không dữ liệu đổi theo lần chạy
    with pytest.raises(PlanError):
        plan([t1, t2], max_tasks=1)


def test_hash_deterministic_and_covers_content():
    t = task(description="d")
    a, b = plan([t]), plan([t])
    assert a.plan_sha256 == b.plan_sha256 == plan_hash(a)
    for changed in (
        build_plan("o/r2", "docs/T.md", [t], [res(t, ["dev-a"])], [], []),
        build_plan(REPO, "docs/T.md", [t], [res(t, ["dev-b"])], [], []),
        plan([task(description="khác")]),
        plan([task(description="d", labels=["x"])]),
        plan([t], [issue(1, build_body(t, ["dev-a"], []))]),
    ):
        assert changed.plan_sha256 != a.plan_sha256


def test_write_preview_guards(tmp_path: Path):
    p = plan([task()])
    out = write_preview(p, "out", tmp_path)
    assert [x.name for x in out] == ["PREVIEW.md", "PREVIEW.json", "plan.json"]
    assert "Write actions: DISABLED until approval" in out[0].read_text(encoding="utf-8")
    assert json.loads(out[2].read_text(encoding="utf-8"))["plan_sha256"] == p.plan_sha256
    with pytest.raises(PlanError):
        write_preview(p, "../escape", tmp_path)
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "PREVIEW.md").symlink_to(tmp_path / "victim.txt")
    with pytest.raises(PlanError):
        write_preview(p, "evil", tmp_path)
    assert not (tmp_path / "victim.txt").exists()


def gh_handler(issues: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    def h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        assert req.method == "GET"
        if path == "/repos/o/r":
            return httpx.Response(200, json={"full_name": "o/r", "archived": False, "has_issues": True, "permissions": {"push": True}})
        if path.startswith("/repos/o/r/assignees/"):
            return httpx.Response(204)
        if path == "/repos/o/r/labels":
            return httpx.Response(200, json=[{"name": "backend"}])
        if path == "/repos/o/r/issues":
            assert req.url.params["state"] == "all"
            return httpx.Response(200, json=issues)
        return httpx.Response(500)

    return h


def test_cli_plan_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    (tmp_path / "m.yaml").write_text("members:\n  TV01: {display_name: A, github: dev-a}\n")
    (tmp_path / "t.md").write_text("## TASK-001: a\n- Phụ trách: TV01\n- Labels: backend\n\n## TASK-002: b\n- Phụ trách: TV01\n")
    prs = {"number": 9, "state": "open", "html_url": "u", "body": marker("t.md", "TASK-002"), "pull_request": {}}
    args = ["issues", "plan", "--workspace", str(tmp_path), "--input", "t.md", "--members", "m.yaml", "--repo", REPO, "--output-dir", "out"]
    assert main(args, transport=httpx.MockTransport(gh_handler([prs]))) == 0  # PR bị bỏ qua -> TASK-002 vẫn CREATE
    out = capsys.readouterr().out
    plan_json = json.loads((tmp_path / "out" / "plan.json").read_text(encoding="utf-8"))
    assert [i["action"] for i in plan_json["items"]] == ["CREATE", "CREATE"] and plan_json["plan_sha256"] in out
    t2 = Task(id="TASK-002", title="b", source_file="t.md", source_reference="TASK-002")
    done = {"number": 3, "state": "open", "html_url": "u", "body": build_body(t2, [], [])}
    assert main(args, transport=httpx.MockTransport(gh_handler([done]))) == 0
    assert json.loads((tmp_path / "out" / "plan.json").read_text(encoding="utf-8"))["items"][1]["action"] == "SKIP"
    assert main([*args[:-2], "--output-dir", "../x"], transport=httpx.MockTransport(gh_handler([]))) == 1  # ngoài workspace
    (tmp_path / "bad.md").write_text("## TASK-001: a\n## TASK-001: b\n")
    bad = [*args]
    bad[bad.index("t.md")] = "bad.md"
    assert main(bad, transport=httpx.MockTransport(gh_handler([]))) == 1  # file lỗi: không lập plan
    monkeypatch.delenv("GITHUB_TOKEN")
    assert main(args) == 2
