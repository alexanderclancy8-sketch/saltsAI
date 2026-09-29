"""The one Fernet cipher every at-rest secret in Jarvis is encrypted with, derived from
`Settings.jarvis_secret_key`. Used by `settings_store.py` (`connections.enc`) and `services/site_access.py`
(site access/engineer codes) - anywhere Jarvis persists something as sensitive as a credential, this is what
encrypts it, so there is exactly one key-derivation scheme to get right rather than one per call site.
"""

from __future__ import annotations

import base64
import hashlib


def fernet(secret_key: str, purpose: str):
    """A Fernet cipher scoped to `purpose` (e.g. "jarvis-settings", "jarvis-site-codes") so the same
    `jarvis_secret_key` produces a different key per use - one store being compromised or its key rotated
    independently doesn't imply anything about another."""
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(f"{purpose}:".encode() + secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))
