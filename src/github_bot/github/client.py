"""Client GitHub REST CHỈ ĐỌC (GET). Không có phương thức ghi nào; token chỉ từ GITHUB_TOKEN."""

import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from github_bot.identities.resolver import GITHUB_LOGIN

DEFAULT_BASE_URL = "https://api.github.com"
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
PER_PAGE = 100  # tối đa của GitHub cho endpoint danh sách
MAX_RESPONSE_BYTES = 2_000_000  # repo/label thật chỉ vài KB; lớn hơn là bất thường -> từ chối


class GitHubError(Exception):
    """Lỗi đã phân loại (code ổn định). Thông điệp không bao giờ chứa token hay header."""

    def __init__(self, code: str, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status  # HTTP status nếu có
        self.retry_after = retry_after  # giây, nếu GitHub cho biết (Retry-After / x-ratelimit-reset)


def _list_of_dicts(r: httpx.Response) -> list[dict[str, Any]]:
    """Endpoint danh sách phải trả JSON list các object; `{}`/null/chuỗi/`[{}]`-sai-kiểu là phản hồi hỏng."""
    data = r.json()
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        raise TypeError("không phải list các object")
    return data


class IssueRef(BaseModel):
    number: int
    state: str
    url: str
    body: str
    assignees: list[str] | None = None  # None = thiếu/không đọc được -> không xác minh được


def _logins(raw: object) -> list[str] | None:
    if not isinstance(raw, list) or not all(isinstance(a, dict) and isinstance(a.get("login"), str) for a in raw):
        return None
    return [a["login"] for a in raw]


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        max_pages: int = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        token = (os.environ.get("GITHUB_TOKEN", "") if token is None else token).strip()
        if not token:
            raise GitHubError("NO_TOKEN", "Thiếu GITHUB_TOKEN trong biến môi trường.")
        if not base_url.startswith("https://"):
            raise ValueError("base_url phải là https://")
        self._base_url = base_url
        self._max_pages = max_pages
        # Không theo redirect: tránh gửi token sang host khác.
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Accept-Encoding": "identity",  # JSON nhỏ; tránh gzip bomb giải nén cả chunk trước khi kiểm ngưỡng
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-automation-bot",
            },
        )

    def __repr__(self) -> str:
        return f"GitHubClient(base_url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    # -- nội bộ ----------------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, int | str] | None = None) -> httpx.Response:
        """GET; chỉ trả 200/204/404. Mọi trạng thái khác -> GitHubError (fail closed). Không retry."""
        return self._request("GET", path, params, None, (200, 204, 404))

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, int | str] | None,
        json_body: dict[str, object] | None,
        ok: tuple[int, ...],
    ) -> httpx.Response:
        """Gửi và phân loại phản hồi. Lớp chỉ-đọc chỉ gọi qua _get; ghi nằm riêng ở execution/."""
        try:
            with self._http.stream(method, path, params=params, json=json_body) as resp:
                body = bytearray()
                for chunk in resp.iter_bytes():  # đọc stream, chặn kể cả khi thiếu Content-Length
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise GitHubError("BAD_RESPONSE", f"Phản hồi lớn hơn {MAX_RESPONSE_BYTES} byte.")
                r = httpx.Response(resp.status_code, headers=resp.headers, content=bytes(body))
        except httpx.HTTPError as e:
            raise GitHubError("NETWORK", f"Lỗi mạng ({type(e).__name__}).") from None
        s = r.status_code
        if s in ok:
            return r
        if s == 401:
            raise GitHubError("AUTH_FAILED", "Token bị từ chối (401).", s)
        if s == 429 or (s == 403 and self._rate_limited(r)):
            reset = r.headers.get("x-ratelimit-reset") or r.headers.get("retry-after") or "?"
            raise GitHubError("RATE_LIMITED", f"Bị giới hạn tốc độ (reset/retry: {reset[:20]}).", s, self._wait_seconds(r))
        if s == 403:
            raise GitHubError("FORBIDDEN", "Token không đủ quyền (403).", s)
        if 300 <= s < 400:
            raise GitHubError("REDIRECT", f"GitHub chuyển hướng ({s}); repo có thể đã đổi tên/chuyển.", s)
        if s >= 500:
            raise GitHubError("SERVER_ERROR", f"GitHub lỗi máy chủ ({s}).", s)
        if s == 404:
            raise GitHubError("NOT_FOUND", "Không tìm thấy (404).", s)
        raise GitHubError("UNEXPECTED_STATUS", f"Trạng thái không mong đợi ({s}).", s)

    @staticmethod
    def _wait_seconds(r: httpx.Response) -> float | None:
        """Số giây phải chờ theo Retry-After, hoặc x-ratelimit-reset (epoch). None nếu không biết."""
        try:
            if "retry-after" in r.headers:
                return max(0.0, float(r.headers["retry-after"]))
            if "x-ratelimit-reset" in r.headers:
                return max(0.0, float(r.headers["x-ratelimit-reset"]) - time.time())
        except ValueError:
            pass
        return None

    @staticmethod
    def _rate_limited(r: httpx.Response) -> bool:
        if r.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in r.headers:
            return True
        try:
            msg = r.json().get("message", "")
        except (ValueError, AttributeError):
            return False
        return isinstance(msg, str) and "rate limit" in msg.lower()

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        if not GITHUB_LOGIN.match(owner) or not _REPO_NAME.match(repo) or repo in (".", ".."):
            raise GitHubError("INVALID_REPO", "owner/repo sai định dạng.")
        return f"/repos/{quote(owner)}/{quote(repo)}"

    # -- API chỉ đọc -----------------------------------------------------------------------
    def get_repo(self, owner: str, repo: str) -> dict[str, object] | None:
        """GET /repos/{owner}/{repo}. None nếu 404 (không tồn tại HOẶC không có quyền thấy)."""
        r = self._get(self._repo_path(owner, repo))
        if r.status_code == 404:
            return None
        try:
            data = r.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise GitHubError("BAD_RESPONSE", "Phản hồi repo không phải JSON object.")
        return data

    def can_assign(self, owner: str, repo: str, login: str) -> bool:
        """GET /repos/{o}/{r}/assignees/{login}: 204 = assign được, 404 = không."""
        if not GITHUB_LOGIN.match(login):
            raise GitHubError("INVALID_LOGIN", "login sai định dạng.")
        r = self._get(f"{self._repo_path(owner, repo)}/assignees/{quote(login)}")
        if r.status_code == 204:
            return True
        if r.status_code == 404:
            return False
        raise GitHubError("UNEXPECTED_STATUS", f"Trạng thái không mong đợi ({r.status_code}).")

    def label_names(self, owner: str, repo: str) -> set[str]:
        """GET /repos/{o}/{r}/labels (phân trang). Trả tên đã casefold. Vượt max_pages -> lỗi."""
        path = f"{self._repo_path(owner, repo)}/labels"
        names: set[str] = set()
        for page in range(1, self._max_pages + 1):
            r = self._get(path, {"per_page": PER_PAGE, "page": page})
            if r.status_code != 200:
                raise GitHubError("NOT_FOUND", "Không đọc được danh sách label.")
            try:
                items = _list_of_dicts(r)
                batch = [i["name"] for i in items]
            except (ValueError, TypeError, KeyError):
                raise GitHubError("BAD_RESPONSE", "Phản hồi label không hợp lệ.") from None
            if any(not isinstance(name, str) for name in batch):
                raise GitHubError("BAD_RESPONSE", "Phản hồi label có tên không phải chuỗi.")
            names.update(n.casefold() for n in batch)
            if len(items) < PER_PAGE:
                return names
        raise GitHubError("TOO_MANY_PAGES", f"Hơn {self._max_pages} trang label; danh sách chưa đầy đủ.")

    def list_issues(self, owner: str, repo: str) -> list[IssueRef]:
        """GET /repos/{o}/{r}/issues?state=all (phân trang). Bỏ pull request (endpoint này trả cả PR).
        Danh sách không đầy đủ (vượt max_pages) -> lỗi: không thể bảo đảm idempotency."""
        path = f"{self._repo_path(owner, repo)}/issues"
        out: list[IssueRef] = []
        for page in range(1, self._max_pages + 1):
            r = self._get(path, {"state": "all", "per_page": PER_PAGE, "page": page})
            if r.status_code != 200:
                raise GitHubError("NOT_FOUND", "Không đọc được danh sách issue.")
            try:
                items = _list_of_dicts(r)
                for i in items:
                    pull_request = i.get("pull_request")
                    if isinstance(pull_request, dict) and pull_request:
                        continue
                    out.append(IssueRef(number=i["number"], state=i["state"], url=i["html_url"], body=i.get("body") or "", assignees=_logins(i.get("assignees"))))
            except (ValueError, TypeError, KeyError, ValidationError):
                raise GitHubError("BAD_RESPONSE", "Phản hồi issue không hợp lệ.") from None
            if len(items) < PER_PAGE:
                return out
        raise GitHubError("TOO_MANY_PAGES", f"Hơn {self._max_pages} trang issue; danh sách chưa đầy đủ.")
