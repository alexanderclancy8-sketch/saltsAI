"""The redactor must catch real secrets WITHOUT mangling harmless text, stay fast on hostile input, and never change
what is actually sent over the wire (it only changes what we print).

Every value below is an obviously fake placeholder - no real secret belongs in this file."""

from __future__ import annotations

import logging
import time
import traceback

import httpx
import pytest

from jarvis.integrations.microsoft365 import TeamsDeliveryError, TeamsNotifier
from jarvis.redact import REDACTED, describe_http_error, install_log_redaction, is_sensitive_param, redact_text

FAKE_SIG = "FAKESIGNATUREVALUE123"
WEBHOOK = ("https://prod-12.uksouth.logic.azure.com:443/workflows/fakeworkflowid/triggers/manual/paths/invoke"
           f"?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig={FAKE_SIG}")


@pytest.fixture(autouse=True)
def redaction_installed():
    install_log_redaction()


# (text, secret that must NOT survive) - every one of these is a real kind of secret.
MUST_REDACT = [
    ("POST https://x.test/hook?api-version=1&sig=FAKESIGVALUE123", "FAKESIGVALUE123"),
    ("https://acct.blob.core.windows.net/c/f.pdf?sv=2023-01-03&se=2026-10-01&sp=r&sig=FAKESASSIG%2Fabc%3D", "FAKESASSIG"),
    ("https://x.test/a?X-Amz-Signature=fakeawssignature99&X-Amz-Expires=60", "fakeawssignature99"),
    ("https://graph.facebook.com/v19.0/me?access_token=EAAFAKEGRAPHTOKEN123", "EAAFAKEGRAPHTOKEN123"),
    ("https://graph.microsoft.com/v1.0/me?$select=id&access_token=FAKEMSGRAPHTOKEN", "FAKEMSGRAPHTOKEN"),
    ("https://maps.googleapis.com/maps/api/geocode/json?address=x&key=AIzaFAKEGOOGLEKEY0000", "AIzaFAKEGOOGLEKEY0000"),
    ("https://x.test/a?apiKey=FAKECAMELKEY12345", "FAKECAMELKEY12345"),
    ("https://x.test/a?subscription-key=FAKESUBSCRIPTIONKEY", "FAKESUBSCRIPTIONKEY"),
    ("https://x.test/a?sas_key=FAKESASKEY00001", "FAKESASKEY00001"),
    ("https://x.test/a?private_key=FAKEPRIVATEKEY01", "FAKEPRIVATEKEY01"),
    ("https://x.test/cb?code=FAKEOAUTHCODE123&state=1", "FAKEOAUTHCODE123"),
    ("https://x.test/cb?auth_code=FAKEAUTHCODE456", "FAKEAUTHCODE456"),
    ("https://x.test/a?client_secret=FAKECLIENTSECRET1", "FAKECLIENTSECRET1"),
    ("https://x.test/a?refresh_token=FAKEREFRESHTOKEN1", "FAKEREFRESHTOKEN1"),
    ("https://x.test/a?password=FAKEPASSWORD99", "FAKEPASSWORD99"),
    ("https://x.test/a?passwd=FAKEPASSWD99", "FAKEPASSWD99"),
    ("https://x.test/a?sessionid=FAKESESSIONID77", "FAKESESSIONID77"),
    ("https://x.test/a?session_id=FAKESESSIONID78", "FAKESESSIONID78"),
    ("https://x.test/a?sid=FAKESID123456", "FAKESID123456"),
    ("https://x.test/a?jwt=FAKEJWTVALUE1234", "FAKEJWTVALUE1234"),
    ("https://x.test/a?otp=FAKE4821", "FAKE4821"),
    ("https://x.test/a?pin=FAKE7734", "FAKE7734"),
    ("https://x.test/a?credentials=FAKECREDS12345", "FAKECREDS12345"),
    ("https://x.test/a?Authorization=FAKEAUTHVALUE99", "FAKEAUTHVALUE99"),
    ("https://x.test/a?x=1&bearer=FAKEBEARERVALUE1", "FAKEBEARERVALUE1"),
    ("token sk-ant-oat01-FAKEFAKEFAKE rejected", "sk-ant-oat01-FAKEFAKEFAKE"),
    ("pushed with ghp_FAKEGITHUBTOKEN0123456789 today", "ghp_FAKEGITHUBTOKEN0123456789"),
    # assembled at run time so GitHub push protection does not mistake the fake for a real Slack token
    (f"bot {'xox' + 'b'}-1234567890-FAKESLACKTOKEN failed", "-1234567890-FAKESLACKTOKEN"),
    ("Authorization: Bearer FAKEBEARERTOKEN0123456789", "FAKEBEARERTOKEN0123456789"),
    ("got 401 for header Bearer eyJhbGciOiJIUzI1NiJ9.FAKEPAYLOAD.FAKESIGNATURE", "FAKEPAYLOAD"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("Authorization: Token FAKETOKENVALUE12345", "FAKETOKENVALUE12345"),
    ("authorization=FAKEOPAQUEAUTHVALUE0123456789", "FAKEOPAQUEAUTHVALUE0123456789"),
    ("x-api-key: FAKEAPIKEYHEADER12345", "FAKEAPIKEYHEADER12345"),
    ("connect https://admin:FAKEPASS99@db.example.test/x", "FAKEPASS99"),
    (WEBHOOK, FAKE_SIG),
    ("posting to https://outlook.office.com/webhook/FAKE-GUID/IncomingWebhook/fakeid/fakeid2", "FAKE-GUID"),
]

# Harmless text that must come out EXACTLY as it went in.
MUST_KEEP = [
    "https://x.test/find?postcode=SW1A1AA&radius=5",
    "https://x.test/find?keyword=extinguisher&page=2",
    "https://x.test/find?keyboard=uk&monkey=1",
    "https://x.test/find?design=panel&designer=bob",
    "https://x.test/find?signal=strong&signals=3",
    "https://x.test/find?passenger=2&passage=north",
    "https://x.test/find?promo_code=SPRING&code_review=yes",
    "https://x.test/find?author=alex&authority=council&authorised=yes",
    "https://x.test/find?session=morning&sessions=3&pinned=true&pinnacle=1",
    "https://x.test/find?sort=name&order=asc&limit=10&$top=15&api-version=2016-06-01",
    "https://x.test/find?tokenizer=simple&secretary=jo&passion=fire",
    "https://graph.microsoft.com/v1.0/users/x/messages?$top=15&$orderby=receivedDateTime%20desc",
    "Authorization: approved by Alex yesterday",
    "Authorization: Token expired",
    "Authorization: Basic approach",
    "the code review is done, authorization pending, see keyword list",
    "Invoice INV-1042 for £1,250.00 due 2026-10-01 (postcode CB1 2AB), approved by Alex on 30/09/2026.",
    "Meeting on 1 October 2026 at 09:30: the bearer instruments were discussed; password policy unchanged.",
    "Pass the keys to the signal engineer; design sign-off at 14:00 - total £89.99 incl. VAT.",
]


@pytest.mark.parametrize("text,secret", MUST_REDACT)
def test_real_secrets_are_redacted(text, secret):
    out = redact_text(text)
    assert secret not in out, out
    assert REDACTED in out
    assert redact_text(out) == out  # idempotent


@pytest.mark.parametrize("text", MUST_KEEP)
def test_harmless_text_is_untouched(text):
    assert redact_text(text) == text


def test_the_tables_are_big_enough():
    assert len(MUST_REDACT) + len(MUST_KEEP) >= 30 + 15


@pytest.mark.parametrize("name,expected", [
    ("sig", True), ("SIG", True), ("X-Amz-Signature", True), ("api_key", True), ("apiKey", True), ("APIKey", True),
    ("x-api-key", True), ("subscription-key", True), ("sas_key", True), ("key", True), ("code", True),
    ("auth_code", True), ("authCode", True), ("access_token", True), ("sessionId", True), ("client[secret]", True),
    ("postcode", False), ("keyword", False), ("keyboard", False), ("monkey", False), ("design", False),
    ("signal", False), ("passenger", False), ("promo_code", False), ("code_review", False), ("session", False),
    ("author", False), ("key_name", False), ("sort", False), ("", False),
])
def test_is_sensitive_param_is_word_based(name, expected):
    assert is_sensitive_param(name) is expected


# --------------------------------------------------------------------------- no catastrophic backtracking
@pytest.mark.parametrize("make", [
    lambda: "a=&" * 70_000,
    lambda: "?x=" * 70_000,
    lambda: "&x=" * 70_000,
    lambda: "?" + "a" * 200_000,
    lambda: "a" * 200_000,
    lambda: "https://" + "a." * 100_000,
    lambda: "http://a:" * 25_000,
    lambda: "Authorization: " + "a" * 200_000,
    lambda: "authorization:" + " " * 200_000,
    lambda: "Bearer " * 30_000,
    lambda: "x://a:b:b:b" * 20_000,
    lambda: "sk-ant-" * 30_000,
], ids=["a=&", "?x=", "&x=", "?aaaa", "aaaa", "https-dots", "scheme-userinfo", "auth-opaque", "auth-spaces",
        "bearer", "userinfo", "prefix"])
def test_redaction_is_fast_on_long_hostile_input(make):
    text = make()
    start = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - start < 2.0


# --------------------------------------------------------------------------- real request: URL sent intact, logs clean
async def test_teams_style_request_is_sent_in_full_but_never_logged_with_its_signature(caplog):
    """The redaction is for what we PRINT. The request itself must carry the complete, unmodified URL - and a later
    failure's log line, exception text and traceback must never contain the signature."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(202 if len(seen) == 1 else 500, request=request)

    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.INFO)  # as if someone turned the request log back on
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ok = await http.post(WEBHOOK, json={"text": "hi"})
            assert ok.status_code == 202
            # httpx normalises the URL, so compare httpx.URL objects, not raw strings.
            assert seen[0] == httpx.URL(WEBHOOK)
            assert seen[0].params["sig"] == FAKE_SIG and seen[0].params["sp"] == "/triggers/manual/run"

            failing = await http.post(WEBHOOK, json={"text": "hi"})
            assert failing.status_code == 500
            assert seen[1] == httpx.URL(WEBHOOK)
            try:
                failing.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logging.getLogger("jarvis.teams").exception("Teams post failed: %s", exc)
                logging.getLogger("jarvis.teams").error("request was %s", exc.request.url)
                shown = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                owner_sees = redact_text(str(exc))
                assert "HTTPStatusError" in shown
    finally:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    assert FAKE_SIG not in caplog.text
    assert "fakeworkflowid" not in caplog.text
    assert "Teams post failed" in caplog.text and REDACTED in caplog.text
    assert "HTTP Request" in caplog.text  # the httpx INFO line was emitted (redacted), not silently absent
    assert FAKE_SIG not in owner_sees and "500" in owner_sees
    assert FAKE_SIG not in redact_text(shown)
    assert FAKE_SIG not in describe_http_error(httpx.HTTPStatusError("x", request=failing.request, response=failing))
    for record in caplog.records:
        assert FAKE_SIG not in record.getMessage() and FAKE_SIG not in (record.exc_text or "")


async def test_teams_notifier_posts_the_full_url_and_hides_it_on_failure(caplog):
    seen: list[httpx.URL] = []
    status = [202]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(status[0], request=request)

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        teams = TeamsNotifier(WEBHOOK, http)
        await teams.post("Title", "Body")
        assert seen[-1] == httpx.URL(WEBHOOK)
        status[0] = 500
        with pytest.raises(TeamsDeliveryError) as info:
            await teams.post("Title", "Body")
    assert seen[-1] == httpx.URL(WEBHOOK)
    assert FAKE_SIG not in caplog.text and FAKE_SIG not in str(info.value)
    assert FAKE_SIG not in "".join(traceback.format_exception(type(info.value), info.value, info.value.__traceback__))
