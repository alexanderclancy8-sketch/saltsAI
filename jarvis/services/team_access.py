"""The team access code: how engineers and office staff get into the cut-down console (Team mode).

The owner sets ONE code in Settings > Team access (no code change, no redeploy) and tells the team. At /login/team a person
types their name and that code and receives a team session (``auth.make_team_session``). What is stored is a salted scrypt
hash of the code in the database - never the code itself, never returned by any endpoint, never logged - plus when it was set
and by whom. Setting a new code, or switching team access off, changes the digest the team cookies are signed with, so every
team session is signed out at once.

Only the principal owner can set, change or clear it (``/api/team-access`` is owner-only in ``access.ROUTE_POLICY``); a manager
or a team session is refused, and Jarvis has no tool for it. A team session can never become more than a team session: it is
read as a separate cookie with a separate key, and the routes and tools it can reach are the allowlists in ``jarvis/access.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

KV_KEY = "team_access"
MIN_CODE_LENGTH = 8
MAX_CODE_LENGTH = 200
_N, _R, _P = 2 ** 14, 8, 1


class CodeRejected(ValueError):
    """The code can't be used (too short / too long); the message is safe to show the owner."""


def _scrypt(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(code.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)


class TeamAccess:
    def __init__(self, db):
        self.db = db

    def _record(self) -> dict:
        try:
            data = json.loads(self.db.get_kv(KV_KEY) or "null")
        except ValueError:
            return {}
        return data if isinstance(data, dict) and data.get("hash") and data.get("salt") else {}

    @property
    def enabled(self) -> bool:
        return bool(self._record())

    def digest(self) -> str:
        """The stored hash (hex) - key material for signing team cookies; "" when team access is off. Not the code."""
        return str(self._record().get("hash") or "")

    def set_code(self, code: str, by: str = "") -> None:
        code = (code or "").strip()
        if len(code) < MIN_CODE_LENGTH:
            raise CodeRejected(f"Use at least {MIN_CODE_LENGTH} characters.")
        if len(code) > MAX_CODE_LENGTH:
            raise CodeRejected("That code is too long.")
        salt = secrets.token_bytes(16)
        self.db.set_kv(KV_KEY, json.dumps({"salt": salt.hex(), "hash": _scrypt(code, salt).hex(),
                                          "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                          "set_by": (by or "")[:80]}))

    def clear(self) -> None:
        self.db.set_kv(KV_KEY, "")

    def verify(self, code: str) -> bool:
        record = self._record()
        if not record or not code or len(code) > MAX_CODE_LENGTH:
            return False
        try:
            salt, expected = bytes.fromhex(record["salt"]), bytes.fromhex(record["hash"])
        except ValueError:
            return False
        return hmac.compare_digest(_scrypt(code.strip(), salt), expected)

    def info(self) -> dict:
        """What the Settings page may show: whether it is on and when it was last set. Never the code or its hash."""
        record = self._record()
        return {"enabled": bool(record), "updated_at": record.get("updated_at"), "set_by": record.get("set_by") or None,
                "min_length": MIN_CODE_LENGTH}
