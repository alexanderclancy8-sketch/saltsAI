"""Keeps secrets out of logs, error messages and tool results.

Some services authenticate through the URL itself: the Power Automate / Teams "Workflows" trigger URL carries a
`sig=` access signature, Meta's Graph API takes `access_token=`, Google takes `key=`, Azure SAS links carry `sig=`.
Anything that prints such a URL (httpx's INFO request log, `HTTPStatusError` messages, tracebacks, uvicorn's access
log) would otherwise put the secret in the log stream or in front of the model / the display.

Three layers, all using `redact_text`:
  * `install_log_redaction()` - call once at start-up. Quietens the HTTP client loggers and redacts every log record
    (message, arguments, traceback) as it is created, whichever logger or handler it goes through.
  * `redact_text()` - call on any error string that is returned to a tool, notification or the display.
  * `describe_http_error()` - a short, URL-free description of an httpx failure.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote

REDACTED = "[REDACTED]"

# Query-string parameters whose value is a secret. The parameter NAME is split into words (on punctuation and
# camelCase boundaries) and judged word by word - see is_sensitive_param - never by substring, so `postcode`,
# `keyword`, `design`, `signal` and `passenger` are left alone.
SENSITIVE_WORDS = frozenset({
    "sig", "signature", "token", "tokens", "secret", "secrets", "password", "passwords", "passwd", "passcode",
    "pwd", "pass", "apikey", "auth", "authorization", "credential", "credentials", "sessionid", "jsessionid",
    "sid", "bearer", "jwt", "otp", "pin",
})
# Adjacent-word pairs that are secret together although neither word is on its own (session+id).
SENSITIVE_PAIRS = frozenset({("session", "id"), ("auth", "code"), ("access", "key"), ("private", "key")})

# Loggers that print full request URLs at INFO. Set to WARNING so routine traffic is not logged at all.
NOISY_HTTP_LOGGERS = (
    "httpx", "httpcore", "hpack",
    "azure.core.pipeline.policies.http_logging_policy",  # prints SAS URLs of blob requests
    "urllib3.connectionpool",
)

# Whole webhook URLs: Power Automate / Logic Apps triggers, Power Platform, and Teams incoming webhooks. The path
# itself identifies the flow, so the path and query are dropped, not just `sig`.
_WEBHOOK_URL = re.compile(
    r"(?P<scheme>https?://)(?P<host>"
    r"[A-Za-z0-9.\-]*(?:\.logic\.azure\.com|\.powerplatform\.com|\.powerautomate\.com|\.webhook\.office\.com)(?::\d+)?"
    r"|outlook\.office(?:365)?\.com)"
    r"(?P<rest>/[^\s\"'<>]*)?",
    re.IGNORECASE,
)
_OUTLOOK_WEBHOOK_PATH = re.compile(r"/webhook", re.IGNORECASE)
_QUERY_PARAM = re.compile(r"(?P<sep>\?|&(?:amp;)?)(?P<name>[A-Za-z0-9_.\-$%\[\]]{1,80})=(?P<val>[^&\s\"'<>#]*)")
# The scheme must start at the beginning of a run of scheme characters, otherwise a long run of letters would be
# rescanned from every position (quadratic time).
_USERINFO = re.compile(r"(?<![A-Za-z0-9+.\-])(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@")
_CRED_CHARS = r"[A-Za-z0-9._~+/=\-]"
_BEARER = re.compile(r"(?i)\b(?P<kind>Bearer)[ \t]+(?P<tok>" + _CRED_CHARS + r"{8,})")
_AUTH_HEADER = re.compile(
    r"(?P<name>\b(?:authorization|x-api-key|api-key|ocp-apim-subscription-key)[\"']?[ \t]*[:=][ \t]*[\"']?)"
    r"(?P<val>(?:(?P<scheme>Bearer|Basic|Token)[ \t]+(?P<tok>[^\s,\"'}&]{4,})|(?P<opaque>" + _CRED_CHARS + r"{8,})))",
    re.IGNORECASE)
_TOKEN_PREFIX = re.compile(r"\b(?:sk-ant-|ghp_|gho_|ghs_|github_pat_|xox[bpas]-)[A-Za-z0-9_\-]{8,}")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NAME_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def _words(name: str) -> list[str]:
    """`api_key`, `X-Api-Key`, `apiKey`, `client[secret]` -> ['api', 'key'] ... (lower-cased)."""
    name = unquote(name)
    return [w.lower() for w in _NAME_SPLIT.split(_CAMEL.sub("_", name)) if w]


def is_sensitive_param(name: str) -> bool:
    """True when the parameter name, read as whole words, names a secret: sig, signature, token, secret, password,
    apikey, auth, credential(s), session id, sid, bearer, jwt, otp, pin, `key` as a whole name or the last word of a
    multi-word name (api_key, subscription-key, sas_key) and `code` only as the whole name (OAuth `code`) or
    auth_code. NOT substrings: postcode, keyword, keyboard, design, signal, passenger, authority are fine."""
    words = _words(name)
    if not words:
        return False
    if any(w in SENSITIVE_WORDS for w in words):
        return True
    if words[-1] == "key":
        return True
    if words == ["code"]:
        return True
    return any(pair in SENSITIVE_PAIRS for pair in zip(words, words[1:]))


def _looks_like_credential(tok: str, scheme: bool) -> bool:
    """Is `tok` (the value after `Authorization:` / `Bearer`) plausibly a credential rather than an ordinary word?
    With an explicit scheme (Bearer/Basic/Token) 16+ characters is enough; otherwise a short value must contain a
    digit, a symbol or inner capitals, and a long one must not be just lower-case words joined by hyphens."""
    tok = tok.strip("\"'")
    if len(tok) < 8:
        return False
    if scheme and len(tok) >= 16:
        return True
    if len(tok) >= 16:
        return not re.fullmatch(r"[a-z\-]+", tok)
    return bool(re.search(r"[0-9+/=._]", tok) or re.search(r"[A-Z]", tok[1:]))


def _webhook(m: re.Match) -> str:
    host, rest = m.group("host"), m.group("rest") or ""
    if host.lower().startswith("outlook.office") and not _OUTLOOK_WEBHOOK_PATH.match(rest):
        return m.group(0)  # an ordinary Outlook web link, not a Teams webhook
    return f"{m.group('scheme')}{host}/{REDACTED}"


def _param(m: re.Match) -> str:
    if is_sensitive_param(m.group("name")) and m.group("val") != REDACTED:
        return f"{m.group('sep')}{m.group('name')}={REDACTED}"
    return m.group(0)


def _auth_header(m: re.Match) -> str:
    if m.group("scheme"):
        # `Authorization: Token expired` is prose: only mask what could be a credential.
        if not _looks_like_credential(m.group("tok"), scheme=True):
            return m.group(0)
        return f"{m.group('name')}{m.group('scheme')} {REDACTED}"
    if _looks_like_credential(m.group("opaque"), scheme=False):
        return f"{m.group('name')}{REDACTED}"
    return m.group(0)


def _bearer(m: re.Match) -> str:
    if _looks_like_credential(m.group("tok"), scheme=True) and m.group("tok") != REDACTED:
        return f"{m.group('kind')} {REDACTED}"
    return m.group(0)


def redact_text(text: Any) -> str:
    """`text` with webhook URLs, sensitive query-string values, credentials in URLs and bearer tokens masked."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return s
    s = _WEBHOOK_URL.sub(_webhook, s)
    s = _USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", s)
    s = _QUERY_PARAM.sub(_param, s)
    s = _AUTH_HEADER.sub(_auth_header, s)
    s = _BEARER.sub(_bearer, s)
    s = _TOKEN_PREFIX.sub(REDACTED, s)
    return s


def redact_url(url: Any) -> str:
    return redact_text(url)


def describe_http_error(e: BaseException) -> str:
    """A description of a failure that never includes the request URL (httpx's own message does)."""
    import httpx

    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url.host}"
    if isinstance(e, httpx.TimeoutException):
        return f"timed out talking to {e.request.url.host}" if getattr(e, "_request", None) else "request timed out"
    if isinstance(e, httpx.RequestError):
        return f"{type(e).__name__} talking to {e.request.url.host}" if getattr(e, "_request", None) else type(e).__name__
    return redact_text(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------------------- logging
class RedactingFilter(logging.Filter):
    """Masks secrets in a log record's message, arguments and traceback. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break logging
            message = str(record.msg)
        cleaned = redact_text(message)
        if cleaned != message or not isinstance(record.msg, str):
            record.msg = cleaned
            record.args = None
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                record.exc_text = None
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


_FILTER = RedactingFilter()


def install_log_redaction() -> None:
    """Call once at start-up (safe to call again). Redaction is applied when each record is created, so it covers
    every logger - ours, httpx, uvicorn's access log, third-party libraries - and every handler."""
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    previous = logging.getLogRecordFactory()
    if not getattr(previous, "_jarvis_redacting", False):
        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            _FILTER.filter(record)
            return record

        factory._jarvis_redacting = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)

    # Belt and braces for records made some other way (e.g. makeRecord overridden, or records replayed).
    for handler in logging.getLogger().handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)
