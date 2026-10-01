"""Routine tests.

* ``system`` suite - is Salts FSM up and behaving? HTTP smoke checks from
  routine_checks.yaml, TLS certificate expiry, and every connected integration.
* ``compliance`` suite - fire & security maintenance obligations from FSM data:
  systems overdue a service visit, contracts due for renewal, overdue call-outs
  and engineers' expiring qualifications.

A check that starts failing raises an alert (and an issue for system checks);
one that recovers clears it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from ..config import Settings
from ..db import Database

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    suite: str
    name: str
    ok: bool
    detail: str
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"suite": self.suite, "name": self.name, "ok": self.ok, "detail": self.detail,
                "duration_ms": self.duration_ms}


def _cert_days_left(host: str, port: int = 443) -> int:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock, ctx.wrap_socket(sock, server_hostname=host) as s:
        not_after = s.getpeercert()["notAfter"]
    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
    return (expires - datetime.utcnow()).days


class RoutineTester:
    def __init__(self, settings: Settings, db: Database, http: httpx.AsyncClient, *, fsm, staff, integrations: dict,
                 notifier, issues=None):
        self.s = settings
        self.db = db
        self.http = http
        self.fsm = fsm
        self.staff = staff
        self.integrations = integrations  # name -> object with async check()
        self.notifier = notifier
        self.issues = issues  # set after construction (circular)

    def _http_checks(self) -> list[dict[str, Any]]:
        if self.s.routine_checks_file.exists():
            data = yaml.safe_load(self.s.routine_checks_file.read_text()) or {}
            return data.get("http_checks", [])
        return []

    async def _http_check(self, spec: dict[str, Any]) -> CheckResult:
        name = spec.get("name") or spec.get("path") or spec.get("url")
        url = spec.get("url") or (self.s.fsm_base_url.rstrip("/") + spec.get("path", "/"))
        t0 = time.perf_counter()
        try:
            r = await self.http.request(spec.get("method", "GET"), url, timeout=spec.get("timeout", 20),
                                        follow_redirects=True)
            ms = int((time.perf_counter() - t0) * 1000)
            problems = []
            if r.status_code != spec.get("expect_status", 200):
                problems.append(f"HTTP {r.status_code} (expected {spec.get('expect_status', 200)})")
            if spec.get("expect_text") and spec["expect_text"] not in r.text:
                problems.append(f"missing text '{spec['expect_text']}'")
            if ms > spec.get("max_ms", 5000):
                problems.append(f"slow: {ms} ms (limit {spec.get('max_ms', 5000)} ms)")
            return CheckResult("system", f"HTTP {name}", not problems, "; ".join(problems) or f"OK in {ms} ms", ms)
        except httpx.HTTPError as e:
            return CheckResult("system", f"HTTP {name}", False, f"unreachable: {type(e).__name__}: {e}",
                               int((time.perf_counter() - t0) * 1000))

    async def run_system(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        if self.s.fsm_base_url:
            specs = self._http_checks() or [{"name": "FSM home page", "path": "/", "max_ms": 5000}]
            results += await asyncio.gather(*(self._http_check(s) for s in specs))
            host = urlparse(self.s.fsm_base_url).hostname
            if host and self.s.fsm_base_url.startswith("https"):
                try:
                    days = await asyncio.to_thread(_cert_days_left, host)
                    results.append(CheckResult("system", "FSM TLS certificate", days > 14, f"expires in {days} days"))
                except Exception as e:  # noqa: BLE001
                    results.append(CheckResult("system", "FSM TLS certificate", False, f"could not check: {e}"))
        for name, integ in self.integrations.items():
            t0 = time.perf_counter()
            try:
                detail = await integ.check()
                results.append(CheckResult("system", f"Integration: {name}", True, detail,
                                           int((time.perf_counter() - t0) * 1000)))
            except Exception as e:  # noqa: BLE001
                results.append(CheckResult("system", f"Integration: {name}", False, f"{type(e).__name__}: {e}"[:300],
                                           int((time.perf_counter() - t0) * 1000)))
        return results

    async def run_compliance(self, today: date | None = None) -> list[CheckResult]:
        today = today or date.today()
        results: list[CheckResult] = []
        systems = await self.fsm.systems()
        overdue, due_soon = [], []
        for s in systems:
            try:
                due = date.fromisoformat(str(s.get("next_service_due"))[:10])
            except ValueError:
                continue
            label = f"{s.get('site')} - {s.get('type')} ({s.get('make_model') or 'system'})"
            if due < today:
                overdue.append(f"{label}: {(today - due).days} days overdue")
            elif due <= today + timedelta(days=14):
                due_soon.append(f"{label}: due {due:%d %b}")
        results.append(CheckResult("compliance", "Service visits overdue", not overdue,
                                   f"{len(overdue)} overdue: " + "; ".join(overdue[:8]) if overdue else "none overdue"))
        results.append(CheckResult("compliance", "Service visits due in 14 days", True,
                                   f"{len(due_soon)} due: " + "; ".join(due_soon[:8]) if due_soon else "none due"))

        renewals, lapsed = [], []
        for c in await self.fsm.contracts():
            try:
                rd = date.fromisoformat(str(c.get("renewal_date"))[:10])
            except ValueError:
                continue
            if rd < today:
                lapsed.append(f"{c.get('customer')} / {c.get('site')} (renewal was {rd:%d %b})")
            elif rd <= today + timedelta(days=45):
                renewals.append(f"{c.get('customer')} / {c.get('site')} on {rd:%d %b} (£{c.get('annual_value') or '?'})")
        results.append(CheckResult("compliance", "Contracts past renewal date", not lapsed,
                                   f"{len(lapsed)}: " + "; ".join(lapsed[:8]) if lapsed else "none"))
        results.append(CheckResult("compliance", "Contracts renewing in 45 days", True,
                                   f"{len(renewals)}: " + "; ".join(renewals[:8]) if renewals else "none"))

        overdue_jobs = await self.staff.overdue_jobs()
        results.append(CheckResult("compliance", "Overdue jobs / call-outs", not overdue_jobs,
                                   "; ".join(f"{j['job']} {j['site']} ({j['engineer']}, {j['hours_overdue']}h)"
                                             for j in overdue_jobs[:8]) or "none"))
        certs = await self.staff.expiring_certifications(30, today)
        expired = [c for c in certs if c["expired"]]
        results.append(CheckResult("compliance", "Engineer qualifications", not expired,
                                   "; ".join(f"{c['engineer']}: {c['certificate']} "
                                             f"{'EXPIRED' if c['expired'] else 'expires'} {c['expires']}"
                                             for c in certs[:8]) or "all current for 30+ days"))
        return results

    async def run(self, suite: str = "all") -> list[dict[str, Any]]:
        results: list[CheckResult] = []
        if suite in ("system", "all"):
            results += await self.run_system()
        if suite in ("compliance", "all"):
            try:
                results += await self.run_compliance()
            except Exception as e:  # noqa: BLE001
                results.append(CheckResult("compliance", "FSM data", False, f"could not load FSM data: {e}"[:300]))
        for r in results:
            prev = self.db.previous_result(r.suite, r.name)
            self.db.add_test_run(r.suite, r.name, r.ok, r.detail, r.duration_ms)
            was_ok = prev is None or bool(prev["ok"])
            if was_ok and not r.ok:
                await self._on_failure(r)
            elif prev is not None and not prev["ok"] and r.ok:
                await self._on_recovery(r)
        self.notifier.bus.publish("tests", self.db.latest_test_results())
        return [r.as_dict() for r in results]

    async def _on_failure(self, r: CheckResult) -> None:
        level = "critical" if r.suite == "system" and r.name.startswith("HTTP") else "warning"
        # A failing system or compliance check needs someone to act, so it is "important" (HTTP/site down is
        # "urgent" via its critical level). The alert key stops a check that keeps failing from repeating.
        await self.notifier.notify(f"Routine test failed: {r.name}", r.detail, level=level,
                                   importance="urgent" if level == "critical" else "important",
                                   dedupe_key=f"routine:{r.suite}:{r.name}")
        if r.suite == "system" and self.issues is not None:
            title = f"Routine test failing: {r.name}"
            if not self.db.find_open_issue_by_title(title):
                await self.issues.report(reporter="Jarvis routine tests", title=title,
                                         description=f"Automated check '{r.name}' started failing.\n\nDetail: {r.detail}",
                                         severity="high", source="routine-test", system="Salts FSM", notify=False)

    async def _on_recovery(self, r: CheckResult) -> None:
        await self.notifier.notify(f"Recovered: {r.name}", r.detail, level="info", importance="info")
        issue = self.db.find_open_issue_by_title(f"Routine test failing: {r.name}")
        if issue:
            self.db.update_issue(issue["id"], status="resolved", notes=f"Check recovered: {r.detail}")
