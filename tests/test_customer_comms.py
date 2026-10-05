from datetime import date, timedelta

from jarvis.brain.tools import TOOLS_BY_NAME
from jarvis.config import Settings
from jarvis.core import Jarvis
from tests.fakes import FakeClient

TODAY = date(2026, 10, 1)


def iso(offset: int, hour: int = 9) -> str:
    return f"{(TODAY + timedelta(days=offset)).isoformat()}T{hour:02d}:00:00"


class FakeFSM:
    demo = False

    def __init__(self):
        self._jobs = [
            {"id": "J1", "ref": "J1", "type": "service", "status": "scheduled", "customer": "Acme Ltd",
             "site": "Acme House", "engineer": "Dan Harper", "scheduled_start": iso(1)},
            {"id": "J2", "ref": "J2", "type": "callout", "status": "en_route", "customer": "Acme Ltd",
             "site": "Acme House", "engineer": "Priya Shah", "scheduled_start": iso(0)},
            {"id": "J3", "ref": "J3", "type": "service", "status": "completed", "customer": "Acme Ltd",
             "site": "Acme House", "engineer": "Dan Harper", "scheduled_start": iso(-1), "completed_at": iso(-1, 12),
             "extra": {"certificateUrl": "https://example.com/cert/J3"}},
            {"id": "J4", "ref": "J4", "type": "service", "status": "completed", "customer": "Nobody Ltd",
             "site": "Unknown Site", "engineer": "Dan Harper", "scheduled_start": iso(-1), "completed_at": iso(-1, 12)},
            {"id": "J5", "ref": "J5", "type": "service", "status": "completed", "customer": "Acme Ltd",
             "site": "Acme House", "engineer": "Dan Harper", "scheduled_start": iso(-9), "completed_at": iso(-9, 12)},
        ]

    async def contracts(self):
        return [{"id": "C1", "customer": "Acme Ltd", "site": "Acme House", "contact_email": "ops@acme.example.com",
                 "contact_name": "Sam"}]

    async def jobs(self, date_from=None, date_to=None, status=None, engineer=None):
        return [dict(j) for j in self._jobs]

    async def job_detail(self, job_id):
        return {"extra": {"notes": [{"text": "Tested all devices; replaced one detector."}]}}

    async def systems(self):
        return [
            {"id": "S1", "customer": "Acme Ltd", "site": "Acme House", "type": "fire_alarm", "make_model": "Gent",
             "next_service_due": (TODAY + timedelta(days=10)).isoformat(), "contract_id": "C1"},
            {"id": "S2", "customer": "Acme Ltd", "site": "Acme House", "type": "emergency_lighting",
             "next_service_due": (TODAY + timedelta(days=20)).isoformat(), "contract_id": "C1"},
            {"id": "S3", "customer": "Acme Ltd", "site": "Acme House", "type": "cctv",
             "next_service_due": (TODAY + timedelta(days=200)).isoformat(), "contract_id": "C1"},
        ]

    async def quotes(self, status=None):
        return [
            {"id": "Q1", "title": "Detector upgrade", "customer": "Acme Ltd", "site": "Acme House", "value": 1200,
             "status": "sent", "sent_date": (TODAY - timedelta(days=7)).isoformat()},
            {"id": "Q2", "title": "Too recent", "customer": "Acme Ltd", "site": "Acme House", "value": 100,
             "status": "sent", "sent_date": (TODAY - timedelta(days=1)).isoformat()},
            {"id": "Q3", "title": "Already accepted", "customer": "Acme Ltd", "site": "Acme House", "value": 100,
             "status": "accepted", "sent_date": (TODAY - timedelta(days=7)).isoformat()},
        ]


def make(tmp_path) -> Jarvis:
    j = Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None), client=FakeClient())
    j.fsm = FakeFSM()
    return j


async def test_drafts_every_lifecycle_event_as_pending_email_send(tmp_path):
    j = make(tmp_path)
    sent = []

    async def no_send(*args, **kwargs):  # the mail layer must never be touched by drafting
        sent.append(args)

    j.mail.send_mail = no_send
    res = await j.customer_comms.draft_all(today=TODAY)
    by_event = {}
    for q in res["queued"]:
        by_event.setdefault(q["event"], []).append(q)
    assert set(by_event) == {"booked", "on_the_way", "complete", "certificate", "service_due", "quote_followup"}
    assert [q["ref"] for q in by_event["booked"]] == ["J1"]
    assert [q["ref"] for q in by_event["on_the_way"]] == ["J2"]
    assert [q["ref"] for q in by_event["complete"]] == ["J3"]  # J4 has no email, J5 is too old
    assert [q["ref"] for q in by_event["certificate"]] == ["J3"]
    assert [q["ref"] for q in by_event["quote_followup"]] == ["Q1"]
    assert len(by_event["service_due"]) == 1  # both due systems at the one site go in one email

    assert sent == []
    pending = j.db.pending_actions()
    assert len(pending) == len(res["queued"]) == 6
    for action in pending:
        assert action["kind"] == "email_send" and action["status"] == "pending"
        assert action["payload"]["to"] == ["ops@acme.example.com"]
        assert action["payload"]["subject"] and action["payload"]["body"].startswith("Hello Sam,")
    bodies = {a["payload"]["subject"]: a["payload"]["body"] for a in pending}
    complete_body = next(b for s, b in bodies.items() if s.startswith("Work completed"))
    assert "replaced one detector" in complete_body
    assert any("https://example.com/cert/J3" in b for b in bodies.values())
    service_body = next(b for s, b in bodies.items() if s.startswith("Service due"))
    assert "fire alarm" in service_body and "emergency lighting" in service_body and "cctv" not in service_body
    await j.http.aclose()


async def test_missing_email_is_skipped_not_queued(tmp_path):
    j = make(tmp_path)
    res = await j.customer_comms.draft_all(["complete"], today=TODAY)
    assert any(s["ref"] == "J4" and "no customer email" in s["reason"] for s in res["skipped"])
    assert all(q["ref"] != "J4" for q in res["queued"])
    await j.http.aclose()


async def test_second_sweep_queues_no_duplicates(tmp_path):
    j = make(tmp_path)
    first = await j.customer_comms.draft_all(today=TODAY)
    second = await j.customer_comms.draft_all(today=TODAY)
    assert first["queued"] and second["queued"] == []
    assert len(j.db.pending_actions()) == len(first["queued"])
    await j.http.aclose()


async def test_event_filter_and_unknown_events(tmp_path):
    j = make(tmp_path)
    res = await j.customer_comms.draft_all(["quote_followup", "nonsense"], today=TODAY)
    assert [q["event"] for q in res["queued"]] == ["quote_followup"]
    assert res["unknown_events"] == ["nonsense"]
    none = await j.customer_comms.draft_all(["nonsense"], today=TODAY)
    assert none["queued"] == [] and "Choose from" in none["note"]
    await j.http.aclose()


async def test_tool_is_registered_and_only_drafts(tmp_path, monkeypatch):
    # The tool takes no date, so it uses the real "today". The fixtures are built around TODAY, so pin the module's
    # clock to it - otherwise this passes on the day it was written and fails every day after.
    import jarvis.services.customer_comms as comms

    class PinnedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(comms, "date", PinnedDate)
    tool = TOOLS_BY_NAME["draft_customer_emails"]
    assert tool.approval is False  # it only queues; the email_send approval gate is untouched
    assert TOOLS_BY_NAME["email_send"].approval is True
    j = make(tmp_path)
    res = await tool.handler(j, tool.model.model_validate({"events": ["booked"]}))
    assert [q["event"] for q in res["queued"]] == ["booked"]
    assert [a["status"] for a in j.db.pending_actions()] == ["pending"]
    await j.http.aclose()


async def test_scheduled_sweep_is_off_by_default(tmp_path):
    from jarvis.services.scheduler import build_scheduler

    j = make(tmp_path)
    assert j.settings.customer_comms_enabled is False
    assert build_scheduler(j).get_job("customer_comms") is None
    await j.http.aclose()
    on = Jarvis(Settings(data_dir=tmp_path / "on", scheduler_enabled=False, customer_comms_enabled=True, _env_file=None),
                client=FakeClient())
    assert build_scheduler(on).get_job("customer_comms") is not None
    await on.http.aclose()
