"""Engineer/access codes for systems Salts itself installs and maintains - a secure replacement for an
engineer's paper site-code book, not a general credential store.

This is deliberately narrow: it only ever holds a code for a named site + system that the owner or an
engineer tells Jarvis directly (`site_access_code_update`, approval-gated like any other write), and it is
only ever looked up by site (`site_access_code`) - never searched, listed in bulk over voice, or exposed to
anything but the authenticated owner. Codes are Fernet-encrypted at rest with the same key-derivation scheme
as the Settings page's connections.enc (see `crypto.py`), scoped to its own purpose so a compromise of one
store says nothing about the other.

What this is *not*: a way to look up or guess a code for a system Salts doesn't hold the maintenance
relationship for. `knowledge/company/system-takeover-access.md` covers what to do instead when access to a
customer's own system needs to be regained - the right process, never a found or leaked code.
"""

from __future__ import annotations

from ..crypto import fernet


class SiteAccessCodes:
    def __init__(self, j):
        self.j = j

    def _cipher(self):
        return fernet(self.j.settings.jarvis_secret_key, "jarvis-site-codes")

    def record(self, site: str, system: str, code: str, notes: str = "") -> dict:
        encrypted = self._cipher().encrypt(code.encode()).decode()
        self.j.db.upsert_site_access_code(site.strip(), system.strip(), encrypted, notes.strip())
        return {"site": site, "system": system, "recorded": True}

    def _decrypt(self, row: dict) -> dict:
        try:
            code = self._cipher().decrypt(row["code_encrypted"].encode()).decode()
        except Exception:  # noqa: BLE001 - jarvis_secret_key changed since this was recorded
            code = "(couldn't decrypt - Jarvis's secret key has changed since this was recorded; record it again)"
        return {"site": row["site"], "system": row["system"], "code": code, "notes": row["notes"],
                "updated_at": row["updated_at"]}

    def find(self, site: str) -> list[dict]:
        return [self._decrypt(r) for r in self.j.db.find_site_access_codes(site)]

    def list_all(self) -> list[dict]:
        return [self._decrypt(r) for r in self.j.db.list_site_access_codes()]
