"""M2b structural guards for the GitHub client package (read-only, token hygiene).

Contract assumed (flag to leader if coder differs):
  github_bot.github.client.GitHubClient()  # reads GITHUB_TOKEN from the environment; raises if
                                           # missing/blank (message mentions GITHUB_TOKEN)
Behaviour is covered end-to-end in test_m2b_github_cli.py; here only static/structural checks.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fake_github import TOKEN, FakeGitHub

SRC = Path(__file__).resolve().parents[2] / "src" / "github_bot"


def _src_lines() -> list[tuple[str, int, str]]:
    out = []
    for f in sorted(SRC.rglob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            out.append((f.relative_to(SRC).as_posix(), n, code))
    return out


def _hits(pattern: str, flags: int = 0) -> list[str]:
    rx = re.compile(pattern, flags)
    return [f"{f}:{n}: {c.strip()}" for f, n, c in _src_lines() if rx.search(c)]


def test_github_package_exists() -> None:
    assert (SRC / "github").is_dir(), "M2b client package github_bot/github/ not found"


def test_no_write_http_methods_anywhere_in_src() -> None:
    assert not _hits(r"""\.(post|put|patch|delete)\s*\("""), "write-method call"
    assert not _hits(r"""["'](POST|PUT|PATCH|DELETE)["']""", re.IGNORECASE), "write verb literal"
    assert not _hits(r"""method\s*=\s*["'](?!GET["'])""", re.IGNORECASE), "non-GET method="


def test_https_only_and_tls_verification_never_disabled() -> None:
    assert not _hits(r"""http://"""), "plain http URL"
    assert not _hits(r"""verify\s*=\s*False"""), "TLS verification disabled"


def test_token_comes_from_env_only() -> None:
    assert _hits(r"""GITHUB_TOKEN"""), "GITHUB_TOKEN env var not referenced"
    assert not _hits(r"""dotenv|\.env["']"""), "token must not be read from a .env file"
    assert not _hits(r"""add_argument\(\s*["']--(token|github-token|auth)"""), "token CLI flag"


def test_token_is_never_printed_or_logged() -> None:
    bad = _hits(r"""(print|log\w*\.\w+|logger\.\w+|warn\w*)\s*\(.*(token|authorization|bearer)""",
                re.IGNORECASE)
    assert not bad, bad


def test_no_fstring_or_format_of_response_headers_into_messages() -> None:
    bad = _hits(r"""f["'].*\{[^}]*(headers|request\.|\.text|\.content)[^}]*\}""")
    github_only = [b for b in bad if b.startswith("github/")]
    assert not github_only, github_only  # error text must not interpolate raw headers/bodies


# ---- behavioural, adapter-based (client class name/ctor is the only assumption) ------------

@pytest.fixture
def client_cls(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from github_bot.github.client import GitHubClient

    fake = FakeGitHub()
    orig = httpx.Client.__init__

    def patched(self: httpx.Client, *a: object, **k: object) -> None:
        k["transport"] = httpx.MockTransport(fake)
        orig(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    return GitHubClient


def test_client_repr_and_str_never_show_the_token(client_cls) -> None:
    c = client_cls()
    for text in (repr(c), str(c), f"{c}", f"{c!r:>200}"):
        assert TOKEN not in text


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_client_without_usable_token_raises_clear_error(client_cls, monkeypatch, bad) -> None:
    if bad is None:
        monkeypatch.delenv("GITHUB_TOKEN")
    else:
        monkeypatch.setenv("GITHUB_TOKEN", bad)
    with pytest.raises(Exception) as e:
        client_cls()
    assert "GITHUB_TOKEN" in str(e.value)
    assert not isinstance(e.value, (AttributeError, TypeError, KeyError, ImportError))


def test_client_exposes_no_mutating_operations(client_cls) -> None:
    names = [n for n in dir(client_cls) if not n.startswith("_")]
    rx = re.compile(r"(post|put|patch|delete|create|add|update|remove|set_|assign|merge|reopen)")
    bad = [n for n in names if rx.match(n.lower())]
    assert not bad, bad
