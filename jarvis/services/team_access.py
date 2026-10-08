"""The team access codes: how engineers and office staff get into the cut-down console (Team mode).

The owner sets TWO codes in Settings > Team access (no code change, no redeploy) - an Office code and an Engineer code - and
tells each group theirs. At /login/team a person types their name and a code, and THE CODE DECIDES THE ROLE: the office code
gives an office session, the engineer code an engineer session (``auth.make_team_session``). What is stored, per role, is a
salted scrypt hash of the code in the database - never the code itself, never returned by any endpoint, never logged - plus
when it was set and by whom. Setting a new code, or switching one off, changes the digest THAT role's cookies are signed with,
so every session of that role (and only that role) is signed out at once. The two codes can never be the same: the code is
what picks the role, so a code that is already the other role's is refused.

Migration (2026-10-08): the single team code from before the split is stored under ``team_access`` - which is now the ENGINEER
code's key, unchanged - and its sessions are signed with the same key as before, so after the upgrade every existing team
code and team session is an ENGINEER one (least privilege: exactly what they could do before). The office code starts off
switched off (``team_access_office``) until the owner sets it.

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

from .. import access

KV_KEY = "team_access"                 # the ENGINEER code (the pre-split team code, unchanged)
KV_KEYS = {access.ENGINEER: KV_KEY, access.OFFICE: "team_access_office"}
MIN_CODE_LENGTH = 8
MAX_CODE_LENGTH = 200
_N, _R, _P = 2 ** 14, 8, 1


class CodeRejected(ValueError):
    """The code can't be used (too short / too long); the message is safe to show the owner."""


def _scrypt(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(code.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)


class CodeInUse(CodeRejected):
    """The code is already the other role's (the code is what decides the role, so the two must differ)."""


class TeamAccess:
    """One role's code. ``role`` is ``access.ENGINEER`` (the default: the pre-split team code) or ``access.OFFICE``."""

    def __init__(self, db, role: str = access.ENGINEER):
        self.db = db
        self.role = access.team_role_of(role)
        self.kv_key = KV_KEYS[self.role]

    def _record(self) -> dict:
        try:
            data = json.loads(self.db.get_kv(self.kv_key) or "null")
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
        self.db.set_kv(self.kv_key, json.dumps({"salt": salt.hex(), "hash": _scrypt(code, salt).hex(),
                                          "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                          "set_by": (by or "")[:80]}))

    def clear(self) -> None:
        self.db.set_kv(self.kv_key, "")

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
                "min_length": MIN_CODE_LENGTH, "role": self.role}


class TeamCodes:
    """Both codes. ``j.team_access`` stays the ENGINEER code (what it always was); this is the pair the sign-in page and the
    owner's Settings use."""

    def __init__(self, engineer: TeamAccess, office: TeamAccess):
        self.by_role = {access.ENGINEER: engineer, access.OFFICE: office}

    def __getitem__(self, role: str) -> TeamAccess:
        return self.by_role[access.team_role_of(role)]

    def digests(self) -> dict[str, str]:
        """{role: digest} - the cookie-signing key material for each role ("" = that role is switched off)."""
        return {role: code.digest() for role, code in self.by_role.items()}

    def set_code(self, role: str, code: str, by: str = "") -> None:
        """Set ``role``'s code, refusing one that is already the other role's code."""
        role = access.team_role_of(role)
        other = access.ENGINEER if role == access.OFFICE else access.OFFICE
        if self.by_role[other].verify(code):
            raise CodeInUse(f"That is already the {access.TEAM_ROLE_LABEL[other].lower()} code. Use a different code: the "
                            "code someone signs in with is what decides whether they are office or engineer.")
        self.by_role[role].set_code(code, by)

    def match(self, code: str) -> str | None:
        """Which role this code signs in as (``office`` / ``engineer``), or None. Both are checked every time (scrypt each),
        so how long it takes says nothing about which one matched."""
        found = [role for role, c in self.by_role.items() if c.verify(code)]
        return found[0] if len(found) == 1 else None

    def info(self) -> dict:
        return {role: c.info() for role, c in self.by_role.items()}
