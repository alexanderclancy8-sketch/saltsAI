"""A mocked Salts FSM `/api/jarvis` data API for the tests: httpx.MockTransport behind the real FSMClient, so the code under test
makes exactly the requests it would make for real. Nothing here reads the wall clock."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from jarvis.config import Settings
from jarvis.integrations.fsm import FSMClient


def resource(name: str, group: str, fields: list[str | tuple[str, str]], *, sensitive: bool = False, filters: list[str] | None = None,
             description: str = "") -> dict[str, Any]:
    fs = [{"name": f, "type": "string", "description": ""} if isinstance(f, str) else {"name": f[0], "type": f[1], "description": ""}
          for f in fields]
    return {"name": name, "group": group, "description": description or f"{name} records", "fields": fs,
            "filters": filters if filters is not None else [f["name"] for f in fs], "sensitive": sensitive}


def catalog(version: str = "v1", *, off: tuple[str, ...] = ("audit",), resources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    res = resources if resources is not None else [
        resource("jobs", "operations", ["id", "ref", "status", "site", "notes", ("scheduled_start", "datetime")]),
        resource("customers", "customer_sites_placeholder", ["id", "name"]),
        resource("invoices", "finance", ["id", "number", "customer", "total", ("due_date", "date")], sensitive=True),
        resource("payslips", "people", ["id", "employee", "gross", "net"], sensitive=True),
        resource("audit_log", "audit", ["id", "who", "what"]),
    ]
    res = [dict(r, group="customers_sites") if r["group"] == "customer_sites_placeholder" else r for r in res]
    groups = {g: {"enabled": g not in off, "description": f"{g} data"} for g in
              ("operations", "customers_sites", "assets", "compliance", "commercial", "finance", "people", "comms", "audit")
              if g in {r["group"] for r in res} or g in off}
    return {"version": version, "groups": groups, "resources": res}


class FakeFsmApi:
    """Routes requests to /api/jarvis/catalog and /api/jarvis/data/{resource}. ``rows`` maps a resource to its list of rows; a
    resource not in ``rows`` answers 404. ``script`` may hand back a response for a given (path, call number) to simulate errors."""

    def __init__(self, cat: dict[str, Any] | None = None, rows: dict[str, list[dict[str, Any]]] | None = None, page_cap: int = 500) -> None:
        self.cat = cat if cat is not None else catalog()
        self.rows = rows or {}
        self.page_cap = page_cap
        self.requests: list[httpx.Request] = []
        self.override: Callable[[httpx.Request, int], httpx.Response | None] | None = None

    def count(self, prefix: str) -> int:
        return sum(1 for r in self.requests if r.url.path.startswith(prefix))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            forced = self.override(request, len(self.requests))
            if forced is not None:
                return forced
        path = request.url.path
        if path == "/api/jarvis/catalog":
            return httpx.Response(200, json=self.cat)
        if path.startswith("/api/jarvis/data/"):
            name = path.rsplit("/", 1)[1]
            if name not in self.rows:
                return httpx.Response(404, json={"error": "unknown_resource", "message": f"no resource {name}"})
            p = request.url.params
            limit = min(int(p.get("limit", "100")), self.page_cap, 500)
            offset = int(p.get("offset", "0"))
            rows = self.rows[name]
            page = rows[offset:offset + limit]
            nxt = offset + limit if offset + limit < len(rows) else None
            return httpx.Response(200, json={"resource": name, "items": page, "total": len(rows), "next_offset": nxt,
                                             "truncated": False})
        return httpx.Response(404, text="Not Found")


class Clock:
    """A hand-wound monotonic clock and a sleep that only advances it - tests never wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class RealishFsm:
    """What FsmData needs from the FSM router: ``demo`` and ``jarvis_call`` - the real FSMClient over a MockTransport."""

    def __init__(self, api: FakeFsmApi, tmp_path, demo: bool = False) -> None:
        self.demo = demo
        self.api = api
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
        settings = Settings(data_dir=tmp_path, fsm_base_url="https://fsm.example", fsm_api_key="k-test-0000", scheduler_enabled=False,
                            _env_file=None)
        self._client = FSMClient(settings, self.http)

    async def jarvis_call(self, *a, **k):
        return await self._client.jarvis_call(*a, **k)

    async def aclose(self) -> None:
        await self.http.aclose()


def rows(n: int, **extra: Any) -> list[dict[str, Any]]:
    return [{"id": i, "ref": f"J{i:04d}", **extra} for i in range(n)]


def dump(value: Any) -> str:
    return json.dumps(value, default=str)
