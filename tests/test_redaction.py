"""Secrets (webhook signatures, tokens, keys) must not reach logs, tool results, notifications or the display.

Every value below is an obviously fake placeholder - no real secret belongs in this file."""

from __future__ import annotations

import logging
import traceback

import httpx
import pytest

from jarvis.brain.tools import serialise
from jarvis.core import Jarvis
from jarvis.integrations.microsoft365 import TeamsDeliveryError, TeamsNotifier
from jarvis.redact import REDACTED, describe_http_error, install_log_redaction, redact_text
from jarvis.services import connection_tests
from tests.fakes import FakeClient

FAKE_SIG = "FAKESIGNATUREVALUE123"
WEBHOOK = ("https://prod-12.uksouth.logic.azure.com:443/workflows/fakeworkflowid/triggers/manual/paths/invoke"
           f"?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig={FAKE_SIG}")


@pytest.fixture(autouse=True)
def redaction_installed():
    install_log_redaction()


# --------------------------------------------------------------------------- redact_text
@pytest.mark.parametrize("name", ["sig", "key", "token", "code", "secret", "password", "api_key", "access_token",
                                  "client_secret", "refresh_token", "SIG", "x-api-key"])
def test_sensitive_query_values_are_masked(name):
    out = redact_text(f"GET https://example.test/path?a=1&{name}=supersecretvalue&b=2")
    assert "supersecretvalue" not in out
    assert f"{name}={REDACTED}" in out
    assert "a=1" in out and "b=2" in out


def test_first_query_parameter_is_masked_too():
    assert "abc123" not in redact_text("https://example.test/report?key=abc123")


def test_html_escaped_ampersand_is_handled():
    assert FAKE_SIG not in redact_text(f"https://example.test/x?a=1&amp;sig={FAKE_SIG}")


def test_harmless_parameters_are_left_alone():
    url = "https://graph.microsoft.com/v1.0/users/x/messages?$top=15&$orderby=receivedDateTime%20desc&api-version=1"
    assert redact_text(url) == url


def test_power_automate_webhook_url_is_fully_masked():
    out = redact_text(f'HTTP Request: POST {WEBHOOK} "HTTP/1.1 202 Accepted"')
    assert FAKE_SIG not in out
    assert "fakeworkflowid" not in out  # the path identifies the flow, so it goes too
    assert "HTTP/1.1 202 Accepted" in out


def test_teams_incoming_webhook_url_is_masked():
    out = redact_text("posting to https://outlook.office.com/webhook/FAKE-GUID/IncomingWebhook/fakeid/fakeid2 now")
    assert "FAKE-GUID" not in out and "now" in out


def test_ordinary_outlook_link_is_kept():
    link = "https://outlook.office.com/owa/?ItemID=AAMk&exvsurl=1"
    assert redact_text(link) == link


def test_credentials_in_url_and_bearer_tokens_are_masked():
    assert "hunter22" not in redact_text("https://user:hunter22@example.test/x")
    assert "abcdefghijklmnop" not in redact_text("Authorization: Bearer abcdefghijklmnop")
    assert "abcdefghijklmnop" not in redact_text("header was Bearer abcdefghijklmnop today")
    assert "sk-ant-oat01-FAKEFAKEFAKE" not in redact_text("token sk-ant-oat01-FAKEFAKEFAKE rejected")


def test_redaction_is_idempotent_and_safe_on_odd_input():
    once = redact_text(WEBHOOK)
    assert redact_text(once) == once
    assert redact_text(None) == ""
    assert redact_text("") == ""
    assert redact_text(ValueError(f"bad {WEBHOOK}")).count(FAKE_SIG) == 0


# --------------------------------------------------------------------------- logging
def test_log_messages_and_arguments_are_redacted(caplog):
    caplog.set_level(logging.INFO)
    logging.getLogger("jarvis.test").info("calling %s", WEBHOOK)
    logging.getLogger("jarvis.test").warning("failed: https://example.test/x?access_token=%s", "tok-FAKE-VALUE")
    assert FAKE_SIG not in caplog.text and "tok-FAKE-VALUE" not in caplog.text
    assert REDACTED in caplog.text


def test_httpx_info_request_log_does_not_contain_the_signature(caplog):
    # httpx logs "HTTP Request: POST <full url>" at INFO. It is quietened to WARNING ...
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    # ... and even if someone switches it back to INFO, the URL is redacted.
    caplog.set_level(logging.INFO, logger="httpx")
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(202))) as client:
        client.post(WEBHOOK, json={})
    assert "HTTP Request" in caplog.text
    assert FAKE_SIG not in caplog.text


def test_httpx_is_silent_at_default_level(caplog):
    caplog.set_level(logging.INFO)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(202))) as client:
        client.post(WEBHOOK, json={})
    assert "HTTP Request" not in caplog.text


def test_tracebacks_are_redacted(caplog):
    caplog.set_level(logging.INFO)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(400))) as client:
        try:
            client.post(WEBHOOK, json={}).raise_for_status()
        except httpx.HTTPStatusError:
            logging.getLogger("jarvis.test").exception("post failed")
    assert "post failed" in caplog.text and "HTTPStatusError" in caplog.text
    assert FAKE_SIG not in caplog.text


# --------------------------------------------------------------------------- Teams webhook
async def test_teams_failure_does_not_expose_the_webhook_url():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(400))) as http:
        teams = TeamsNotifier(WEBHOOK, http)
        assert teams.enabled
        with pytest.raises(TeamsDeliveryError) as info:
            await teams.post("Title", "Body")
    assert "400" in str(info.value)
    shown = "".join(traceback.format_exception(type(info.value), info.value, info.value.__traceback__))
    assert FAKE_SIG not in str(info.value) and FAKE_SIG not in shown
    assert FAKE_SIG not in repr(teams) and "logic.azure.com" not in repr(teams)


async def test_teams_transport_error_does_not_expose_the_webhook_url():
    def boom(request):
        raise httpx.ConnectError("could not connect to " + str(request.url), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
        with pytest.raises(TeamsDeliveryError) as info:
            await TeamsNotifier(WEBHOOK, http).post("Title", "Body")
    assert FAKE_SIG not in str(info.value)


def test_describe_http_error_never_includes_the_url():
    request = httpx.Request("POST", WEBHOOK)
    err = httpx.HTTPStatusError("boom " + WEBHOOK, request=request, response=httpx.Response(500, request=request))
    text = describe_http_error(err)
    assert "500" in text and FAKE_SIG not in text


# --------------------------------------------------------------------------- tool results, notifications, UI
def test_tool_results_are_redacted():
    text = serialise({"error": f"HTTPStatusError: Client error '401' for url '{WEBHOOK}'", "ok": 1})
    assert FAKE_SIG not in text and '"ok": 1' in text


def test_connection_test_messages_are_redacted():
    assert FAKE_SIG not in connection_tests._why(RuntimeError(f"failed calling {WEBHOOK}"))


async def test_notifications_are_redacted_before_they_are_stored_or_published(settings):
    j = Jarvis(settings, client=FakeClient())
    queue = j.bus.subscribe()
    await j.notifier.notify("Fix failed", f"HTTPStatusError for url '{WEBHOOK}'", level="warning", push=False)
    stored = j.db.recent_notifications(5)
    assert stored and all(FAKE_SIG not in str(n) for n in stored)
    assert FAKE_SIG not in str(queue.get_nowait())
    await j.http.aclose()


async def test_teams_delivery_failure_shown_to_the_owner_is_redacted(settings):
    class LeakyTeams:
        enabled = True

        async def post(self, title, body):
            raise RuntimeError(f"Client error '400' for url '{WEBHOOK}'")

    settings.owner_email = "owner@example.test"
    j = Jarvis(settings, client=FakeClient())
    j.notifier.teams = LeakyTeams()
    result = await j.notifier.send_engineering_update("Subject", "Body")
    assert "not delivered" in result
    assert all(FAKE_SIG not in str(n) for n in j.db.recent_notifications(10))
    await j.http.aclose()


def test_webhook_url_is_a_secret_setting_and_is_not_sent_to_the_browser(settings, tmp_path):
    from jarvis.settings_store import FIELDS, SettingsStore

    assert FIELDS["teams_webhook_url"].kind == "secret"
    settings.teams_webhook_url = WEBHOOK
    view = SettingsStore(settings).view(Jarvis(settings, client=FakeClient()).db, {"base_url": "", "app_name": ""})
    teams_section = next(s for s in view["sections"] if s["id"] == "teams")
    field = next(f for f in teams_section["fields"] if f["key"] == "teams_webhook_url")
    assert field["is_set"] is True and "value" not in field
    assert "fakeworkflowid" not in str(view) and "logic.azure.com" not in str(view)
    assert FAKE_SIG[-4:] not in field["hint"]  # not even the tail of the signature
