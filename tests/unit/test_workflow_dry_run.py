import json

import httpx
import pytest
from fake_github import TOKEN, FakeGitHub, make_issue

from github_bot.cli import main

REPO = "AIVIETNAM-AIO-luciusHo/realcheck"
MARKER = "<!-- github-automation-bot: source=tasks.md; task_id=TASK-012; schema=v1 -->"
TASK = """## TASK-012: Kiểm tra tạo issue mẫu
- Phụ trách: TV01

### Mô tả
Nội dung giả cho lần kiểm tra BOT-012.
"""
MEMBERS = """members:
  TV01:
    display_name: Thành viên thử nghiệm
    github: login-thu-nghiem
"""


@pytest.mark.parametrize("case", ["skip", "missing-token", "invalid-input", "block"])
def test_dry_run_cli_contract(tmp_path, monkeypatch, capsys, case) -> None:
    if case == "missing-token":
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    (tmp_path / "tasks.md").write_text("" if case == "invalid-input" else TASK, encoding="utf-8")
    (tmp_path / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    fake = FakeGitHub(
        repo=REPO,
        assignees=[] if case == "block" else ["login-thu-nghiem"],
        issues=[make_issue(202, MARKER)],
    )
    transport = httpx.MockTransport(fake)
    inputs = ["--workspace", str(tmp_path), "--input", "tasks.md", "--members", "members.yaml"]
    validate = main(["issues", "validate", *inputs], transport=transport)
    output = tmp_path / "preview"
    assert not fake.requests

    if case == "invalid-input":
        assert validate != 0
        assert "NO_TASKS" in capsys.readouterr().out
        assert not output.exists()
        return

    assert validate == 0
    result = main(
        ["issues", "plan", *inputs, "--repo", REPO, "--max-tasks", "1", "--output-dir", output.name],
        transport=transport,
    )
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    if case == "missing-token":
        assert result != 0
        assert "GITHUB_TOKEN" in captured.out
        assert not fake.requests and not output.exists()
        return

    assert fake.requests and {request.method for request in fake.requests} == {"GET"}
    assert all(request.url.path.startswith(f"/repos/{REPO}") for request in fake.requests)
    assert not fake.posts and not fake.foreign
    data = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    [item] = data["items"]
    assert data["repo"] == REPO and data["source_file"] == "tasks.md"
    assert item["task_id"] == "TASK-012"
    if case == "block":
        assert result != 0 and "1 BLOCK" in captured.out
        assert item["action"] == "BLOCK" and item["reasons"]
    else:
        assert result == 0
        assert (item["action"], item["issue_number"]) == ("SKIP", 202)
        assert {path.name for path in output.iterdir()} == {"PREVIEW.md", "PREVIEW.json", "plan.json"}
        assert all(TOKEN not in path.read_text(encoding="utf-8") for path in output.iterdir())
