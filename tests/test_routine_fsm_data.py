"""The compliance suite's "FSM data" check must record a pass as well as a failure - otherwise a one-off failure
stays as the latest result on the HUD for ever, because only a newer row for the same check replaces it."""

from __future__ import annotations

from jarvis.config import Settings
from jarvis.core import Jarvis
from tests.fakes import FakeClient


def _fsm_data(j):
    return next(r for r in j.db.latest_test_results() if r["name"] == "FSM data")


async def test_a_failed_fsm_data_check_is_cleared_by_the_next_good_run(tmp_path, monkeypatch):
    j = Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None), client=FakeClient())
    real = j.tester.run_compliance

    async def broken(*a, **k):
        raise RuntimeError("503 Service Unavailable")

    monkeypatch.setattr(j.tester, "run_compliance", broken)
    await j.tester.run("compliance")
    assert not _fsm_data(j)["ok"] and "503" in _fsm_data(j)["detail"]

    monkeypatch.setattr(j.tester, "run_compliance", real)
    await j.tester.run("compliance")
    assert _fsm_data(j)["ok"]
    await j.http.aclose()
