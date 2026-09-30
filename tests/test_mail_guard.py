"""Management-only recipient rule: finance/management content never goes to a shared inbox."""

import asyncio

import pytest

from jarvis.core import Jarvis
from jarvis.integrations.mail_guard import GENERAL, MANAGEMENT, MailGuardError, guard_message, is_shared_mailbox
from tests.fakes import FakeClient

ALEX = "alex@saltsfireandsecurity.co.uk"
CHUN = "chun@saltsfireandsecurity.co.uk"
INFO = "info@saltsfireandsecurity.co.uk"


@pytest.fixture
def s(settings):
    settings.owner_email = ALEX
    settings.partner_email = CHUN
    return settings


def test_shared_mailbox_detection(s):
    assert is_shared_mailbox(s, INFO) and is_shared_mailbox(s, "Info <INFO@SaltsFireAndSecurity.co.uk>")
    assert not is_shared_mailbox(s, ALEX)
    assert not is_shared_mailbox(s, "info@customer.example.com")  # a customer's info@ is not ours
    s.ooh_mailbox = "calls@saltsfireandsecurity.co.uk"
    assert is_shared_mailbox(s, "calls@saltsfireandsecurity.co.uk")


def test_sensitive_to_info_is_redirected_to_owner(s):
    g = guard_message(s, [INFO], sensitivity=MANAGEMENT)
    assert g.to == [ALEX] and g.sensitive and g.changes


def test_sensitive_keeps_allowlisted_management_and_drops_shared_cc_bcc(s):
    g = guard_message(s, [ALEX, INFO], cc=[CHUN, INFO], bcc=[INFO], sensitivity=MANAGEMENT)
    assert g.to == [ALEX] and g.cc == [CHUN] and g.bcc == []


def test_allowlisted_recipients_still_work(s):
    g = guard_message(s, [ALEX, CHUN], sensitivity=MANAGEMENT)
    assert g.to == [ALEX, CHUN] and not g.changes
    s.management_emails = "finance.director@example.com"
    assert guard_message(s, ["Finance.Director@example.com"], sensitivity=MANAGEMENT).to == ["finance.director@example.com"]


def test_sensitive_to_non_management_is_rejected(s):
    with pytest.raises(MailGuardError):
        guard_message(s, ["customer@example.com"], sensitivity=MANAGEMENT)
    with pytest.raises(MailGuardError):
        guard_message(s, ["engineer@saltsfireandsecurity.co.uk"], sensitivity=MANAGEMENT)


def test_no_usable_owner_means_reject_not_send_to_shared(s):
    s.owner_email = INFO  # misconfigured: owner set to a shared inbox is never treated as management
    with pytest.raises(MailGuardError):
        guard_message(s, [INFO], sensitivity=MANAGEMENT)
    s.owner_email = ""
    with pytest.raises(MailGuardError):
        guard_message(s, [INFO], sensitivity=MANAGEMENT)


def test_unknown_internal_report_defaults_to_management(s):
    assert guard_message(s, [INFO]).to == [ALEX]  # internal, sensitivity unknown -> safe side
    with pytest.raises(MailGuardError):
        guard_message(s, ["engineer@saltsfireandsecurity.co.uk"])


def test_customer_and_supplier_emails_pass_through(s):
    g = guard_message(s, ["facilities@example-school.org.uk"])
    assert g.to == ["facilities@example-school.org.uk"] and not g.sensitive
    g = guard_message(s, ["orders@supplier.example.co.uk"], cc=[INFO], sensitivity=GENERAL)
    assert g.to == ["orders@supplier.example.co.uk"] and g.cc == [INFO]


def test_no_recipient_is_rejected(s):
    with pytest.raises(MailGuardError):
        guard_message(s, [])


async def test_send_mail_layer_applies_guard_and_logs(s, caplog):
    j = Jarvis(s, client=FakeClient([]))
    with caplog.at_level("WARNING"):
        sent = await j.mail.send_mail([INFO], "Cashflow", "<p>cash</p>", [INFO], sensitivity=MANAGEMENT)
    assert sent.to == [ALEX] and sent.cc == []
    assert "Mail guard" in caplog.text
    with pytest.raises(MailGuardError):
        await j.mail.send_mail(["customer@example.com"], "P&L", "<p>x</p>", sensitivity=MANAGEMENT)
    await j.http.aclose()


async def test_owner_update_email_not_sent_to_shared_owner_address(s):
    s.owner_email = INFO
    j = Jarvis(s, client=FakeClient([]))
    sent = []

    async def send_mail(to, subject, body, cc=None, bcc=None, sensitivity=None):
        sent.append((to, sensitivity))
        return guard_message(s, to, cc, bcc, sensitivity)

    j.mail.demo = False
    j.mail.send_mail = send_mail
    out = await j.notifier.send_owner_update("Cash position", "figures", channels=("email",))
    assert sent == [([INFO], MANAGEMENT)] and out.startswith("the display only")  # blocked by the guard, nothing delivered
    await j.http.aclose()


async def test_approved_email_to_owner_and_external_still_work(s):
    j = Jarvis(s, client=FakeClient([]))
    a = j.actions.queue("email_send", "tax watch", {"to": [CHUN], "cc": [], "subject": "Tax", "body": "x"})
    b = j.actions.queue("email_send", "renewal", {"to": ["customer@example.com"], "cc": [], "subject": "Renewal",
                                                  "body": "x"})
    assert {p["status"] for p in j.db.pending_actions()} == {"pending"}  # still queued, nothing auto-sent
    await j.actions.approve(a)
    await j.actions.approve(b)
    await asyncio.sleep(0.05)
    assert j.db.get_action(a)["status"] == "done" and j.db.get_action(b)["status"] == "done"
    await j.http.aclose()


async def test_approved_email_to_info_is_redirected_to_owner_not_sent_to_info(s):
    j = Jarvis(s, client=FakeClient([]))
    seen = []
    real = j.mail.send_mail

    async def spy(*a, **k):
        g = await real(*a, **k)
        seen.append(g)
        return g

    j.mail.send_mail = spy
    a = j.actions.queue("email_send", "finance", {"to": [INFO], "cc": [], "subject": "Debtors", "body": "x"})
    await j.actions.approve(a)
    await asyncio.sleep(0.05)
    assert seen and seen[0].to == [ALEX]
    await j.http.aclose()
