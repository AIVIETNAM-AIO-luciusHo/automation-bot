import re
from collections.abc import Callable

import httpx
import pytest
from fake_github import TOKEN

from github_bot.cli import main

REPO = "owner/repo"
PULLS = f"/repos/{REPO}/pulls"
URL = f"https://github.com/{REPO}/pull"


def pull(number: int, title: str, ecosystem: str) -> dict:
    return {
        "number": number,
        "state": "open",
        "title": title,
        "html_url": f"{URL}/{number}",
        "head": {"ref": f"dependabot/{ecosystem}/update"},
        "user": {"login": "dependabot[bot]", "type": "Bot"},
    }


def run_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    respond: Callable[[httpx.Request], httpx.Response],
    *groups: str,
) -> tuple[int, str, str, list[httpx.Request]]:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET" and request.url.scheme == "https"
        assert request.url.host == "api.github.com" and request.url.path == PULLS
        assert request.url.params["state"] == "open"
        return respond(request)

    argv = ["dependabot", "report", "--repo", REPO]
    for group in groups:
        argv.extend(["--group", group])
    rc = main(argv, transport=httpx.MockTransport(handle))
    out, err = capsys.readouterr()
    assert TOKEN not in out + err
    return rc, out, err, seen


def next_page(request: httpx.Request) -> dict[str, str]:
    return {"Link": f'<{request.url.copy_merge_params({"page": 2})}>; rel="next"'}


def section(report: str, name: str) -> str:
    headings = re.split(r"(?m)^#{1,6} +", report)
    for heading in headings:
        title, _, body = heading.partition("\n")
        if title.strip() == name:
            return body
    pytest.fail(f"Thiếu nhóm Markdown {name!r}")


def test_report_paginates_and_checks_bot_login_and_type(monkeypatch, capsys) -> None:
    pip = pull(11, "Bump requests", "pip")
    actions = pull(12, "Bump checkout", "github_actions")
    npm = pull(13, "Bump vite", "npm")
    uv = pull(16, "Bump uv lockfile", "uv")
    actions_hyphen = pull(17, "Bump setup-uv", "github-actions")
    unmatched = pull(18, "Ref ngoài mẫu", "pip") | {"head": {"ref": "updates/pip"}}
    human = pull(14, "Mạo danh bằng login", "pip") | {
        "user": {"login": "dependabot[bot]", "type": "User"}
    }
    other_bot = pull(15, "Bot khác", "pip") | {
        "user": {"login": "custom[bot]", "type": "Bot"}
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page", "1") == "1":
            return httpx.Response(200, json=[pip, human, other_bot], headers=next_page(request))
        assert request.url.params["page"] == "2"
        return httpx.Response(200, json=[actions, npm, uv, actions_hyphen, unmatched])

    rc, out, err, seen = run_report(monkeypatch, capsys, respond)
    assert rc == 0, out + err
    assert [req.url.params.get("page", "1") for req in seen] == ["1", "2"]
    for group, item in [
        ("Runtime", pip), ("Runtime", uv), ("CI", actions), ("CI", actions_hyphen),
        ("Chưa phân nhóm", npm), ("Chưa phân nhóm", unmatched),
    ]:
        body = section(out, group)
        assert f"#{item['number']}" in body and item["title"] in body and item["html_url"] in body
    for excluded in (human, other_bot):
        assert excluded["title"] not in out and excluded["html_url"] not in out


def test_report_group_options_add_and_override(monkeypatch, capsys) -> None:
    rc, out, err, _ = run_report(
        monkeypatch,
        capsys,
        lambda _: httpx.Response(
            200,
            json=[
                pull(21, "Bump requests", "pip"),
                pull(22, "Bump vite", "npm"),
                pull(23, "Bump checkout", "github_actions"),
            ],
        ),
        "pip=Ứng dụng",
        "npm=Ứng dụng",
        "github-actions=Tự động hóa",
    )
    assert rc == 0, out + err
    body = section(out, "Ứng dụng")
    assert "Bump requests" in body and "Bump vite" in body
    assert len(re.findall(r"(?m)^#{1,6} +Ứng dụng *$", out)) == 1
    assert "Bump checkout" in section(out, "Tự động hóa")


@pytest.mark.parametrize(
    "groups",
    [("github-actions=Alpha", "github_actions=Beta"), ("pip=",)],
    ids=["duplicate-normalized-key", "empty-group-name"],
)
def test_invalid_groups_are_rejected_before_http(monkeypatch, capsys, groups) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        pytest.fail("Không được gọi HTTP khi cấu hình nhóm không hợp lệ.")

    rc, out, err, seen = run_report(monkeypatch, capsys, respond, *groups)
    assert rc != 0 and (out + err).strip()
    assert not seen


def test_report_empty_is_successful(monkeypatch, capsys) -> None:
    rc, out, err, seen = run_report(
        monkeypatch, capsys, lambda _: httpx.Response(200, json=[])
    )
    assert rc == 0, out + err
    assert seen and out.strip() and f"{URL}/" not in out


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (503, {"message": "Service unavailable"}),
        (200, {"message": "Không phải danh sách"}),
        (200, [pull(22, "Mục có số sai kiểu", "pip") | {"number": "22"}]),
    ],
    ids=["api-error", "malformed-page", "malformed-pr"],
)
def test_report_discards_previous_page_on_failure(monkeypatch, capsys, status, payload) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page", "1") == "1":
            return httpx.Response(
                200, json=[pull(21, "Mục hợp lệ ở trang đầu", "pip")], headers=next_page(request)
            )
        return httpx.Response(status, json=payload)

    rc, out, err, seen = run_report(monkeypatch, capsys, respond)
    assert rc != 0 and len(seen) == 2
    assert (err + out).strip()
    assert "Mục hợp lệ ở trang đầu" not in out + err and f"{URL}/21" not in out + err


def test_report_renders_untrusted_title_safely(monkeypatch, capsys) -> None:
    title = "Bump pkg\n## Chèn mục <img src=x> [link](https://example.invalid/)\x1b\u202e"
    rc, out, err, _ = run_report(
        monkeypatch, capsys, lambda _: httpx.Response(200, json=[pull(31, title, "pip")])
    )
    assert rc == 0, out + err
    assert "Bump pkg" in out and f"{URL}/31" in out
    assert not re.search(r"(?m)^#{1,6}\s+Chèn mục", out)
    assert "<img src=x>" not in out and "[link](https://example.invalid/)" not in out
    assert "\x1b" not in out and "\u202e" not in out
