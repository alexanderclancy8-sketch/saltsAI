"""PO intake: classifying inbound email, matching it to a sent quote, and queuing the
combined accept-quote + book-job action - plus the action's execution (quote accepted,
job booked, PO number recorded, customer acknowledged) once approved."""

from __future__ import annotations

from jarvis.core import Jarvis
from jarvis.config import Settings
from tests.fakes import FakeClient


def make(settings):
    return Jarvis(settings, client=FakeClient())


class FakeMail:
    demo = False

    def __init__(self, messages):
        self._messages = messages
        self.sent: list[tuple] = []

    async def list_messages(self, unread_only=True, top=20, **kw):
        return [{k: v for k, v in m.items() if k != "body"} for m in self._messages]

    async def get_message(self, message_id, **kw):
        return next(m for m in self._messages if m["id"] == message_id)

    async def send_mail(self, to, subject, body_html, cc=None, bcc=None, sensitivity=None):
        self.sent.append((to, subject, body_html))
        return type("G", (), {"to": to, "changes": []})()


class FakeFSM:
    demo = False

    def __init__(self, quotes):
        self._quotes = quotes
        self.writes: list[tuple] = []

    async def quotes(self, status=None):
        return [q for q in self._quotes if not status or q.get("status") == status]

    async def write(self, method, path, body=None):
        self.writes.append((method, path, body))
        if method == "POST" and path == "/jobs":
            return {"job": {"id": "job-999", **body}}
        return {"status": 200}


def po_email(id_="m1", subject="Authorised Purchase Order SAL-0001", body="PO SAL-0001 for quote Q1180"):
    return {"id": id_, "subject": subject, "from_name": "Jane Buyer", "from_email": "jane@customer.example.co.uk",
            "body": body, "preview": body}


async def test_po_matched_by_quote_reference_queues_combined_action(tmp_path):
    j = make(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None))
    j.po_intake.mail = FakeMail([po_email()])
    j.po_intake.fsm = FakeFSM([{"id": "Q1180", "title": "Vigilon panel upgrade", "customer": "Wharfedale Academy Trust",
                               "site": "Ilkley Grammar Annexe", "value": 14850, "status": "sent"}])
    j.client.beta.messages.parse_result = {
        "is_purchase_order": True, "customer_guess": "Wharfedale Academy Trust",
        "po_number": "SAL-0001", "quote_reference": "Q1180"}

    found = await j.po_intake.scan_inbox()

    assert found == 1
    pending = j.db.pending_actions()
    assert len(pending) == 1
    action = pending[0]
    assert action["kind"] == "accept_quote_from_po"
    assert action["payload"]["quote_id"] == "Q1180"
    assert action["payload"]["po_number"] == "SAL-0001"
    assert action["payload"]["ack_to"] == "jane@customer.example.co.uk"
    assert action["payload"]["job_body"]["customer"] == "Wharfedale Academy Trust"
    await j.http.aclose()


async def test_non_po_email_is_ignored(tmp_path):
    j = make(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None))
    j.po_intake.mail = FakeMail([po_email(subject="Re: your newsletter")])
    j.po_intake.fsm = FakeFSM([{"id": "Q1180", "status": "sent"}])
    j.client.beta.messages.parse_result = {"is_purchase_order": False}

    found = await j.po_intake.scan_inbox()

    assert found == 0
    assert j.db.pending_actions() == []
    await j.http.aclose()


async def test_po_with_no_matching_quote_notifies_instead_of_queueing(tmp_path):
    j = make(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None))
    j.po_intake.mail = FakeMail([po_email()])
    j.po_intake.fsm = FakeFSM([])  # nothing sent, so nothing can match
    j.client.beta.messages.parse_result = {
        "is_purchase_order": True, "customer_guess": "Someone Unknown", "po_number": "SAL-0001", "quote_reference": ""}
    seen = []

    class FakeNotifier:
        async def notify(self, *a, **kw):
            seen.append((a, kw))

    j.po_intake.notifier = FakeNotifier()

    found = await j.po_intake.scan_inbox()

    assert found == 1
    assert j.db.pending_actions() == []
    assert seen and "no matching quote" in seen[0][0][0]
    await j.http.aclose()


async def test_same_email_is_only_processed_once(tmp_path):
    j = make(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None))
    j.po_intake.mail = FakeMail([po_email()])
    j.po_intake.fsm = FakeFSM([{"id": "Q1180", "status": "sent", "customer": "Wharfedale Academy Trust"}])
    j.client.beta.messages.parse_result = {
        "is_purchase_order": True, "customer_guess": "Wharfedale Academy Trust", "po_number": "SAL-0001",
        "quote_reference": "Q1180"}

    first = await j.po_intake.scan_inbox()
    second = await j.po_intake.scan_inbox()

    assert first == 1 and second == 0
    assert len(j.db.pending_actions()) == 1
    await j.http.aclose()


async def test_approving_the_action_books_job_records_po_and_emails_customer(tmp_path):
    j = make(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None))
    fake_mail = FakeMail([])
    fake_fsm = FakeFSM([{"id": "Q1180", "status": "sent"}])
    j.actions.mail = fake_mail
    j.actions.fsm = fake_fsm
    action_id = j.actions.queue("accept_quote_from_po", "Accept quote and book job from PO", {
        "quote_id": "Q1180", "job_body": {"site": "Ilkley Grammar Annexe", "type": "install",
                                          "description": "Vigilon panel upgrade", "customer": "Wharfedale Academy Trust"},
        "po_number": "SAL-0001", "ack_to": "jane@customer.example.co.uk", "ack_name": "Jane"})

    await j.actions.approve(action_id)
    import asyncio
    await asyncio.sleep(0)  # let the spawned _run task complete

    kinds = [w[0:2] for w in fake_fsm.writes]
    assert ("PATCH", "/quotes/Q1180") in kinds
    assert ("POST", "/jobs") in kinds
    assert ("PUT", "/jobs/job-999/customer-po") in kinds
    po_write = next(w for w in fake_fsm.writes if w[1] == "/jobs/job-999/customer-po")
    assert po_write[2] == {"poNumber": "SAL-0001"}
    assert len(fake_mail.sent) == 1
    assert fake_mail.sent[0][0] == ["jane@customer.example.co.uk"]
    assert "SAL-0001" in fake_mail.sent[0][2]
    await j.http.aclose()
