"""CONTRACT (red until .gitignore is fixed in its own PR): secret-bearing dotfiles must be ignored, not only `.env`.

Found 2026-11-18: an untracked `.envq` at the repo root was not covered by .gitignore, so `git add -A` would push a
token to a PUBLIC repo. Uses `git check-ignore` on throwaway names (never reads any real secret file).
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def ignored(name: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=ROOT, check=False)
    return r.returncode == 0


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production", ".envq", ".env.bak", ".env~", ".envrc"])
def test_secret_dotfiles_are_gitignored(name: str) -> None:
    assert ignored(name), f"{name} would be committed by `git add -A`"


@pytest.mark.parametrize("name", [".env.example"])
def test_env_example_stays_trackable(name: str) -> None:
    assert not ignored(name)


@pytest.mark.parametrize("name", ["configs/members.yaml", "configs/bot.yaml", "out/plan.json", "out/audit-ab12cd34.jsonl"])
def test_real_configs_and_run_artifacts_are_gitignored(name: str) -> None:
    assert ignored(name), f"{name} may carry real logins or issue bodies"
