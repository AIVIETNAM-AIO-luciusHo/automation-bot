import hashlib
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fake_github import TOKEN

from github_bot.cli import main

REPO = "owner/repo"
SOURCE_SHA = "a" * 40
ARTIFACTS = {"plan.json", "PREVIEW.md", "metadata.json"}
TASKS = """## TASK-001: API
- Phụ trách: TV01, TV02
- Labels: backend

## TASK-002: Tài liệu
- Labels: docs
"""
MEMBERS = """members:
  TV01: {display_name: Thành viên A, github: example-dev-1}
  TV02: {display_name: Thành viên B, github: example-dev-2}
"""


@pytest.fixture
def preview(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    (tmp_path / "tasks.md").write_text(TASKS, encoding="utf-8")
    (tmp_path / "members.yaml").write_text(MEMBERS, encoding="utf-8")

    def no_http(request):
        pytest.fail(f"Lệnh offline gửi HTTP {request.method}")

    transport = httpx.MockTransport(no_http)

    def invoke(*args):
        rc = main(["issues", *args, "--workspace", str(tmp_path)], transport=transport)
        cap = capsys.readouterr()
        assert TOKEN not in cap.out + cap.err
        return rc, cap.out + cap.err

    def run(*, sha=SOURCE_SHA, repo=REPO, output="preview"):
        return invoke(
            "preview", "--input", "tasks.md", "--members", "members.yaml", "--repo", repo,
            "--allowed-repo", "other/repo", "--allowed-repo", "OWNER/REPO",
            "--source-sha", sha, "--output-dir", output,
        )

    return run, invoke


def test_preview_writes_exact_artifacts_with_stable_plan_and_provenance(tmp_path, preview):
    run, _ = preview
    rc, diagnostics = run()
    assert rc == 0, diagnostics
    destination = tmp_path / "preview"
    assert {path.name for path in destination.iterdir()} == ARTIFACTS
    files = {path.name: path.read_text(encoding="utf-8") for path in destination.iterdir()}
    assert all(TOKEN not in text for text in files.values())
    plan = json.loads(files["plan.json"])
    metadata = json.loads(files["metadata.json"])
    assert plan["schema"] == "offline-preview-v1" and plan["remote_verified"] is False
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    assert set(tasks) == {"TASK-001", "TASK-002"}
    assert tasks["TASK-001"]["title"] == "API"
    assert set(tasks["TASK-001"]["assignees"]) == {"example-dev-1", "example-dev-2"}
    assert tasks["TASK-001"]["labels"] == ["backend"]
    assert tasks["TASK-002"]["assignees"] == [] and tasks["TASK-002"]["labels"] == ["docs"]
    assert all("action" not in task for task in tasks.values())
    assert "NO_ASSIGNEE" in diagnostics or "NO_ASSIGNEE" in files["PREVIEW.md"]
    assert all(tid in files["PREVIEW.md"] for tid in tasks)
    digest = plan["plan_sha256"]
    canonical = json.dumps(
        {key: value for key, value in plan.items() if key != "plan_sha256"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == digest
    assert metadata["plan_sha256"] == digest
    assert metadata["source_sha"] == SOURCE_SHA and metadata["source"] == "tasks.md"
    assert metadata["target_repo"].casefold() == REPO and metadata["tool_version"]
    assert datetime.fromisoformat(metadata["created_at"]).utcoffset() == timedelta(0)

    rc, diagnostics = run(output="repeat")
    assert rc == 0, diagnostics
    repeated = json.loads((tmp_path / "repeat/plan.json").read_text(encoding="utf-8"))
    assert repeated == plan


@pytest.mark.parametrize("case", ["repo", "sha", "markdown", "unresolved", "symlink", "outside"])
def test_preview_rejects_invalid_input_without_artifacts(tmp_path, preview, case):
    run, _ = preview
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    kwargs = {}
    if case == "repo":
        kwargs["repo"] = "untrusted/repo"
    if case == "sha":
        kwargs["sha"] = "main"
    if case == "markdown":
        (tmp_path / "tasks.md").write_text("## TASK: thiếu mã\n", encoding="utf-8")
    if case == "unresolved":
        (tmp_path / "tasks.md").write_text(TASKS.replace("TV02", "TV99"), encoding="utf-8")
    if case == "symlink":
        (tmp_path / "preview").symlink_to(outside, target_is_directory=True)
    if case == "outside":
        kwargs["output"] = str(outside)

    rc, diagnostics = run(**kwargs)
    assert rc != 0 and diagnostics.strip()
    assert not list(outside.iterdir())
    assert all(not (tmp_path / "preview" / name).exists() for name in ARTIFACTS)


def test_apply_refuses_offline_preview_even_with_matching_hash(tmp_path, preview):
    run, invoke = preview
    rc, diagnostics = run()
    assert rc == 0, diagnostics
    plan = json.loads((tmp_path / "preview/plan.json").read_text(encoding="utf-8"))
    rc, diagnostics = invoke(
        "apply", "--plan", "preview/plan.json", "--approve-hash", plan["plan_sha256"], "--write",
    )
    assert rc != 0 and diagnostics.strip()


def test_preview_renders_untrusted_markup_as_text(tmp_path, preview):
    run, _ = preview
    title = "@someone ![ảnh](https://example.invalid/a) [link](https://example.invalid/) <img src=x>"
    (tmp_path / "tasks.md").write_text(f"## TASK-001: {title}\n", encoding="utf-8")
    rc, diagnostics = run()
    assert rc == 0, diagnostics
    markdown = (tmp_path / "preview/PREVIEW.md").read_text(encoding="utf-8")
    assert "TASK-001" in markdown and "ảnh" in markdown and "link" in markdown
    assert all(part not in markdown for part in ("@someone", "![", "https://", "<img", "[link]("))


def test_preview_rejects_artifact_symlink_before_writing(tmp_path, preview):
    run, _ = preview
    output = tmp_path / "preview"
    output.mkdir()
    original = tmp_path / "original.json"
    original.write_text("nguyên bản", encoding="utf-8")
    (output / "metadata.json").symlink_to(original)
    rc, diagnostics = run()
    assert rc != 0 and "symlink" in diagnostics
    assert {path.name for path in output.iterdir()} == {"metadata.json"}
    assert original.read_text(encoding="utf-8") == "nguyên bản"
