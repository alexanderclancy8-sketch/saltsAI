"""Secrets must not reach the logs - but the URLs themselves (and the calls made with them) stay exactly as they are."""

from __future__ import annotations

import logging

import httpx
import pytest

from jarvis.integrations.microsoft365 import TeamsNotifier
from jarvis.logredact import MASK, RedactingFilter, install_log_redaction, redact_secrets

SIG = "Zx9-fake_SIGNATURE-value0123456789"
# Same shape as a Power Automate / Teams Workflows trigger URL (fake values).
FLOW_URL = ("https://prod-12.uksouth.logic.azure.com:443/workflows/0123456789abcdef/triggers/manual/paths/invoke"
            f"?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig={SIG}")


@pytest.fixture
def restore_http_loggers():
    saved = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def test_masks_sensitive_query_values_only():
    out = redact_secrets(
        "GET https://x.test/a?api_key=K1&page=2&access_token=T1&code=C1&secret=S1&password=P1&token=T2&key=K2"
        "&client_secret=S2&sig=SIG1 done")
    for leaked in ("K1", "T1", "C1", "S1", "P1", "T2", "K2", "S2", "SIG1"):
        assert leaked not in out
    assert "page=2" in out and out.endswith(f"sig={MASK} done")


def test_leaves_harmless_text_and_params_alone():
    text = "https://x.test/p?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0 monkey=1 a key=b"
    assert redact_secrets(text) == text


def test_flow_url_masks_signature_but_keeps_the_rest():
    out = redact_secrets(f"HTTP Request: POST {FLOW_URL}")
    assert SIG not in out
    assert f"sig={MASK}" in out
    assert "api-version=2016-06-01" in out and "/triggers/manual/paths/invoke" in out


def test_install_holds_httpx_loggers_at_warning(restore_http_loggers):
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    install_log_redaction()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


async def test_current_style_url_still_works_but_signature_is_masked_in_logs(caplog, restore_http_loggers):
    install_log_redaction()
    caplog.handler.addFilter(RedactingFilter())  # install_log_redaction() covers real handlers; caplog's is per-test
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200 if len(seen) == 1 else 500)

    # Worst case: something turns httpx INFO logging back on. The filter must still mask the signature.
    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        teams = TeamsNotifier(FLOW_URL, http)
        await teams.post("Title", "Body")  # the outbound call is unchanged and succeeds with the current URL
        with pytest.raises(httpx.HTTPStatusError) as err:
            await teams.post("Title", "Body")
        logging.getLogger("jarvis.test").warning("Teams update failed: %s", err.value)
        logging.getLogger("jarvis.test").error("boom", exc_info=err.value)

    assert seen == [FLOW_URL, FLOW_URL]  # the real request still carries the full, unmodified signature
    assert teams.url == FLOW_URL
    assert caplog.records, "expected the httpx / warning records to be captured"
    assert SIG not in caplog.text
    assert f"sig={MASK}" in caplog.text
