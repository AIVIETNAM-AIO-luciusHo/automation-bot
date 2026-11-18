import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fake_github import TOKEN, FakeGitHub

from github_bot.cli import main


class _FullDisk:
    def __enter__(self) -> "_FullDisk":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def write(self, _s: str) -> int:
        raise OSError(28, "No space left on device")

    def flush(self) -> None:
        raise OSError(28, "No space left on device")


def test_audit_write_failure_stops_cleanly_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    fake = FakeGitHub()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    orig = httpx.Client.__init__

    def patched(self: httpx.Client, *a: Any, **k: Any) -> None:
        k["transport"] = httpx.MockTransport(fake)
        orig(self, *a, **k)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    (tmp_path / "T.md").write_text("## TASK-001: a\n- Phụ trách: TV01\n\n## TASK-002: b\n- Phụ trách: TV01\n", encoding="utf-8")
    (tmp_path / "m.yaml").write_text('members:\n  TV01: {display_name: A, github: "example-dev-1"}\n', encoding="utf-8")
    assert main(["issues", "plan", "--input", "T.md", "--members", "m.yaml", "--repo", "owner/repo", "--output-dir", "out"]) == 0
    plan = json.loads((tmp_path / "out" / "plan.json").read_text(encoding="utf-8"))
    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: _FullDisk() if self.name.startswith("audit-") else real_open(self, *a, **k))
    capsys.readouterr()
    rc = main(["issues", "apply", "--plan", "out/plan.json", "--approve-hash", plan["plan_sha256"], "--write"])
    out = capsys.readouterr().out
    assert rc == 1 and "Traceback" not in out and "không ghi được audit" in out and "CREATED TASK-001" in out
    assert len(fake.posts) == 1  # dừng ngay: không tạo thêm issue khi không còn audit
