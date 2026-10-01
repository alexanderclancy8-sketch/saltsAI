"""Shared helpers for the GitHub pull-request tool tests: a GitHub client wired to a mocked transport."""

from __future__ import annotations

import io
import tarfile
from typing import Any, Callable

import httpx

from jarvis.integrations.github import GitHub

TOKEN = "s3cr3tToken-notShaped-9876"
REPO = "owner/jarvis"
BASE = f"/repos/{REPO}"


class Mock:
    """Routes (method, path) -> JSON body, bytes, or a callable(request) -> httpx.Response. Records every request."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None):
        self.routes: dict[tuple[str, str], Any] = dict(routes or {})
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[bytes] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.gh = GitHub(TOKEN, REPO, self.client, "main")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.calls.append(key)
        self.bodies.append(request.content)
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, json={"message": f"Not Found: {key}"})
        if callable(route):
            return route(request)
        if isinstance(route, bytes):
            return httpx.Response(200, content=route)
        return httpx.Response(200, json=route)

    def get(self, path: str, body: Any) -> None:
        self.routes[("GET", BASE + path)] = body

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]


def pr(number: int = 1, *, mergeable: bool | None = True, state: str = "clean", title: str = "Add a thing",
       ref: str = "feature", changed: int = 3, draft: bool = False, body: str = "Does a thing.", base: str = "main",
       sha: str | None = None, pr_state: str = "open", head_repo: str = REPO) -> dict[str, Any]:
    return {"number": number, "title": title, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "state": pr_state, "draft": draft, "merged": False, "user": {"login": "alex"},
            "head": {"ref": ref, "sha": sha or f"{number:040x}", "repo": {"full_name": head_repo}},
            "base": {"ref": base}, "mergeable": mergeable, "mergeable_state": state,
            "changed_files": changed, "body": body}


def runs(*items: tuple[str, str, str | None]) -> dict[str, Any]:
    """check-runs response from (name, status, conclusion) triples."""
    return {"check_runs": [{"name": n, "status": s, "conclusion": c, "html_url": f"https://ci/{n}",
                            "output": {"title": f"{n} result", "summary": f"{n} summary"}} for n, s, c in items]}


GREEN = runs(("tests", "completed", "success"))


def tarball(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            raw = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(f"owner-jarvis-abc123/{name}")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def respond(status: int, body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)
