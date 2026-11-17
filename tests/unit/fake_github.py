"""In-memory fake of the three read-only GitHub REST endpoints M2b may use (httpx.MockTransport).

Real-API facts assumed (verify against docs.github.com/en/rest when wiring E2E):
- GET /repos/{o}/{r}                      -> 200 JSON (full_name, has_issues, archived, permissions) | 404 | 403 | 401
- GET /repos/{o}/{r}/assignees/{login}    -> 204 = can be assigned, 404 = cannot
- GET /repos/{o}/{r}/labels?per_page&page -> 200 JSON list, max 100 per page, Link header rel="next"
  (GitHub's Link URLs use /repositories/{id}/...); label names are case-insensitive.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable

import httpx

HOST = "api.github.com"
TOKEN = "ghp_SENTINEL0123456789abcdefghijklmnopqrstu"
OMIT = object()  # sentinel: leave the `permissions` key out of the repo JSON
DEFAULT_PERMS = {"admin": False, "maintain": False, "push": True, "triage": True, "pull": True}


class FakeGitHub:
    def __init__(
        self,
        *,
        repo: str = "owner/repo",
        labels: Iterable[str] = ("backend", "auth", "database"),
        assignees: Iterable[str] = ("example-dev-1", "example-dev-2", "example-dev-3"),
        repo_status: int = 200,
        repo_body: object | None = None,
        label_status: int = 200,
        label_body: object | None = None,
        assignee_status: dict[str, int] | None = None,
        permissions: object = DEFAULT_PERMS,
        override: Callable[[httpx.Request], httpx.Response | None] | None = None,
    ) -> None:
        self.repo = repo
        self.labels = list(labels)
        self.assignees = {a.casefold() for a in assignees}
        self.repo_status = repo_status
        self.repo_body = repo_body
        self.label_status = label_status
        self.label_body = label_body
        self.assignee_status = {k.casefold(): v for k, v in (assignee_status or {}).items()}
        self.permissions = permissions
        self.override = override
        self.requests: list[httpx.Request] = []
        self.foreign: list[httpx.Request] = []

    # -- helpers ----------------------------------------------------------------------------
    def repo_json(self) -> dict[str, object]:
        body: dict[str, object] = {
            "id": 1, "full_name": self.repo, "private": False, "has_issues": True,
            "archived": False,
        }  # fmt: skip
        if self.permissions is not OMIT:
            body["permissions"] = self.permissions
        return body

    def hits(self, kind: str) -> list[httpx.Request]:
        def match(p: str) -> bool:
            if kind == "repo":
                return p == f"/repos/{self.repo.lower()}"
            if kind == "labels":
                return p.endswith("/labels")
            return "/assignees/" in p

        return [r for r in self.requests if match(r.url.path.lower().rstrip("/"))]

    def assignee_hits(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.hits("assignee")]

    # -- transport --------------------------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != HOST:
            self.foreign.append(request)
            return httpx.Response(200, json=self.repo_json())  # a spoof that looks valid
        if self.override is not None:
            resp = self.override(request)  # may also raise httpx errors
            if resp is not None:
                return resp
        path = request.url.path.rstrip("/")
        base = f"/repos/{self.repo}"
        if path.lower() == base.lower():
            return self._json(self.repo_status, self.repo_body, self.repo_json())
        if path.lower() == f"{base}/labels".lower() or path == "/repositories/1/labels":
            return self._labels(request)
        if path.lower().startswith(f"{base}/assignees/".lower()):
            login = path.rsplit("/", 1)[-1].casefold()
            status = self.assignee_status.get(login, 204 if login in self.assignees else 404)
            return httpx.Response(status, json={"message": "x"} if status >= 400 else None)
        return httpx.Response(404, json={"message": "Not Found"})

    def _json(self, status: int, body: object | None, default: object) -> httpx.Response:
        if isinstance(body, bytes | str):
            return httpx.Response(status, content=body)
        if body is not None or status != 200:
            return httpx.Response(status, json=body if body is not None else {"message": "err"})
        return httpx.Response(status, json=default)

    def _labels(self, request: httpx.Request) -> httpx.Response:
        if self.label_status != 200 or self.label_body is not None:
            return self._json(self.label_status, self.label_body, [])
        q = request.url.params
        per = max(1, min(int(q.get("per_page", "30")), 100))
        page = max(1, int(q.get("page", "1")))
        chunk = self.labels[(page - 1) * per : page * per]
        items = [{"id": i, "name": n, "color": "ededed"} for i, n in enumerate(chunk)]
        headers = {}
        if page * per < len(self.labels):
            nxt = f"https://{HOST}/repositories/1/labels?per_page={per}&page={page + 1}"
            headers["Link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, content=json.dumps(items), headers=headers)


def assert_requests_safe(fake: FakeGitHub, token: str = TOKEN) -> None:
    """Invariants for EVERY request any test causes: read-only, https, pinned host, no traversal."""
    for r in fake.requests:
        if r.url.host == HOST:
            assert r.method == "GET", f"non-GET request: {r.method} {r.url}"
            assert r.url.scheme == "https", r.url
            assert r.content == b"", "GET with a body"
            raw = r.url.raw_path.decode("ascii", "replace").lower()
            assert "%2e" not in raw and "%2f" not in raw and "%5c" not in raw, raw
            segs = r.url.path.split("/")
            assert ".." not in segs and "." not in segs, r.url.path
            assert r.url.path.startswith(("/repos/owner/repo", "/repositories/")), r.url.path
            assert r.url.userinfo == b"" and r.url.port is None, r.url
            assert "\r" not in str(r.url) and "\n" not in str(r.url)
    for r in fake.foreign:
        blob = " ".join(f"{k}={v}" for k, v in r.headers.items()) + str(r.url)
        assert token not in blob and "authorization" not in r.headers, "token sent to foreign host"
