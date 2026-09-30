"""Keeps secrets out of log output.

Webhook and API URLs often carry their credential in the query string (a Power Automate / Teams Workflows trigger
URL has `sig=...`). httpx logs every request URL at INFO and exception messages quote them too, so two things
happen at start-up (see `install_log_redaction`): the httpx/httpcore loggers are held at WARNING, and a filter masks
the value of any sensitive query parameter in whatever is still logged. Only the log text is changed - the URLs
themselves, and the requests made with them, are untouched.
"""

from __future__ import annotations

import logging
import re

MASK = "***"

# A query parameter is sensitive if its name is (or ends with `_`/`-`/`.` + ) one of these words, e.g. `sig`,
# `access_token`, `api_key`, `client_secret`, `X-Amz-Signature`.
_WORDS = ("sig", "signature", "key", "apikey", "token", "code", "secret", "password", "passwd", "pwd", "auth",
          "authorization", "credential", "credentials")
_SENSITIVE_PARAM = re.compile(
    r"(?P<pre>[?&;])(?P<name>(?:[\w.\-]*[_.\-])?(?:" + "|".join(_WORDS) + r"))=(?P<value>[^&#\s\"'<>]*)",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Mask the value of every sensitive query-string parameter in `text`; everything else is left as is."""
    if not text or "=" not in text:
        return text
    return _SENSITIVE_PARAM.sub(lambda m: f"{m['pre']}{m['name']}={MASK}", text)


class RedactingFilter(logging.Filter):
    """Masks sensitive query-string values in a record's message and traceback. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = redact_secrets(message)
            if redacted != message:
                record.msg = redacted
                record.args = None
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = logging.Formatter().formatException(record.exc_info)
                record.exc_text = redact_secrets(record.exc_text)
        except Exception:  # noqa: BLE001 - logging must never break the caller
            pass
        return True


def install_log_redaction() -> None:
    """Quieten httpx/httpcore and put the redacting filter on every root log handler (safe to call repeatedly)."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
