import re
from pathlib import Path

import pytest
import yaml

from github_bot import __version__
from github_bot.cli import main

ROOT = Path(__file__).resolve().parents[2]


def _bot() -> dict:
    return yaml.safe_load((ROOT / "configs/bot.example.yaml").read_text(encoding="utf-8"))


def test_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_safe_defaults_in_example_config():
    c = _bot()
    assert c["bot"]["default_mode"] == "dry_run"
    assert c["bot"]["write_enabled"] is False
    assert c["bot"]["auto_merge"] is False
    assert c["issues"]["ensure_labels_exist"] is False
    assert c["issues"]["require_approval_hash"] is True
    assert c["ai"]["enabled"] is False
    assert c["dependabot"]["enabled"] is False
    assert c["dependabot"]["auto_merge"] is False


def test_policy_example_is_fail_closed():
    p = yaml.safe_load((ROOT / "configs/policy.example.yaml").read_text(encoding="utf-8"))["policy"]
    assert p["unresolved_assignee"] == "block"
    assert p["duplicate_marker"] == "fail_closed"
    assert p["allow_reopen_or_delete"] is False


# M0 has no GitHub client at all; M2 is read-only (GET). Revisit allowlist when execution/ lands (M3).
WRITE_CALL = re.compile(r"\.(post|put|patch|delete)\s*\(|\bhttpx\.(post|put|patch|delete)\b")


def test_src_has_no_github_write_calls():
    offenders = []
    for f in (ROOT / "src").rglob("*.py"):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if WRITE_CALL.search(line):
                offenders.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, offenders


def test_env_and_real_configs_are_gitignored():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in lines
    assert any(x in lines for x in ("configs/*.yaml", "configs/members.yaml"))
