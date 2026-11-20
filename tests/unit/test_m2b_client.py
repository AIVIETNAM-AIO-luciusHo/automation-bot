"""M2b structural guards for the GitHub client package (read-only, token hygiene).

Contract assumed (flag to leader if coder differs):
  github_bot.github.client.GitHubClient()  # reads GITHUB_TOKEN from the environment; raises if
                                           # missing/blank (message mentions GITHUB_TOKEN)
Behaviour is covered end-to-end in test_m2b_github_cli.py; here only static/structural checks.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fake_github import TOKEN, FakeGitHub

from github_bot.github.client import GitHubClient, GitHubError

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


def test_no_write_http_methods_outside_the_executor_module() -> None:
    """M3b: exactly one package (src/github_bot/execution/) may issue POST; nobody may PUT/PATCH/DELETE."""
    def outside_execution(hits: list[str]) -> list[str]:
        return [h for h in hits if not h.startswith("execution/")]

    assert not outside_execution(_hits(r"""\.(post)\s*\(""")), "POST call outside execution/"
    assert not outside_execution(_hits(r"""["'](POST)["']""", re.IGNORECASE)), "POST literal outside execution/"
    assert not _hits(r"""\.(put|patch|delete)\s*\("""), "PUT/PATCH/DELETE call"
    assert not _hits(r"""["'](PUT|PATCH|DELETE)["']""", re.IGNORECASE), "PUT/PATCH/DELETE literal"
    assert not _hits(r"""method\s*=\s*["'](?!GET["']|POST["'])""", re.IGNORECASE), "unexpected method="


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


@pytest.mark.parametrize("over_limit", [False, True], ids=["exact-limit", "one-byte-over"])
def test_get_limits_stream_without_content_length(over_limit: bool) -> None:
    class RepoStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self) -> Iterator[bytes]:
            yield b"{}"
            yield b" " * 999_998
            yield b" " * 1_000_000
            if over_limit:
                yield b" "
                pytest.fail("GET đọc tiếp sau khi đã vượt giới hạn")

        def close(self) -> None:
            self.closed = True

    stream = RepoStream()

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        response = httpx.Response(200, stream=stream)
        assert "Content-Length" not in response.headers
        return response

    client = GitHubClient(token=TOKEN, transport=httpx.MockTransport(respond))
    try:
        if over_limit:
            with pytest.raises(GitHubError) as exc:
                client.get_repo("owner", "repo")
            assert exc.value.code == "BAD_RESPONSE"
        else:
            assert client.get_repo("owner", "repo") == {}
        assert stream.closed
    finally:
        client.close()
