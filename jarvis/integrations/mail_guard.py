"""Recipient guard for outbound email: management/finance content never lands in a shared inbox.

Every email Jarvis sends goes through ``send_mail`` in the mail layer, which calls :func:`guard_message` first.

Sensitivity:
* ``MANAGEMENT`` - finance / management / HR content. May only be addressed to the management allowlist (the owner,
  the business partner and MANAGEMENT_EMAILS). A shared inbox in ``to`` is rewritten to the owner; shared inboxes
  (and anyone else not on the allowlist) are dropped from cc/bcc; any other non-management ``to`` rejects the send.
* ``GENERAL`` - customer/supplier/colleague mail that carries no internal figures. Passed through untouched.
* ``None`` (unknown) - safe side: if every recipient is internal (company domain, management or a shared inbox) it is
  an internal report and is treated as MANAGEMENT. If a customer or supplier is involved it is treated as GENERAL
  (those sends still go through the owner's approval queue first).

The guard never sends anything itself and does not touch the approval queue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from email.utils import parseaddr

from ..config import Settings

log = logging.getLogger(__name__)

MANAGEMENT = "management"
GENERAL = "general"


class MailGuardError(ValueError):
    """The message can't be sent without breaking the management-only recipient rule."""


@dataclass
class GuardedMessage:
    to: list[str]
    cc: list[str]
    bcc: list[str]
    sensitive: bool
    changes: list[str] = field(default_factory=list)


def _addr(value: str) -> str:
    return (parseaddr(value or "")[1] or (value or "")).strip().lower()


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[1] if "@" in addr else ""


def is_shared_mailbox(s: Settings, address: str) -> bool:
    addr = _addr(address)
    return any(_entry_matches(s, e, addr) for e in s.shared_mailbox_entries)


def _entry_matches(s: Settings, entry: str, addr: str) -> bool:
    if entry.endswith("@"):  # "info@" = that mailbox on our own domain
        return bool(s.company_domain) and addr == f"{entry}{s.company_domain.lower()}"
    if entry.startswith("@"):
        return _domain(addr) == entry[1:]
    return addr == entry


def management_addresses(s: Settings) -> set[str]:
    """Allowlisted management addresses. A shared inbox can never be management, even if misconfigured as one."""
    return {a for a in s.management_address_entries if not is_shared_mailbox(s, a)}


def _is_internal(s: Settings, addr: str, mgmt: set[str]) -> bool:
    return addr in mgmt or is_shared_mailbox(s, addr) or (bool(s.company_domain) and _domain(addr) == s.company_domain.lower())


def _dedupe(addrs: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for a in addrs:
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def classify(s: Settings, to: list[str], cc: list[str], bcc: list[str], sensitivity: str | None) -> bool:
    """True if the message must follow the management-only rule."""
    if sensitivity == MANAGEMENT:
        return True
    if sensitivity == GENERAL:
        return False
    mgmt = management_addresses(s)
    everyone = [_addr(a) for a in [*to, *cc, *bcc]]
    return all(_is_internal(s, a, mgmt) for a in everyone)


def guard_message(s: Settings, to: list[str], cc: list[str] | None = None, bcc: list[str] | None = None,
                  sensitivity: str | None = None) -> GuardedMessage:
    """Apply the management-only rule. Returns the recipients to actually use, or raises MailGuardError."""
    to_l, cc_l, bcc_l = [_addr(a) for a in to or []], [_addr(a) for a in cc or []], [_addr(a) for a in bcc or []]
    if not any(to_l):
        raise MailGuardError("Email has no recipient.")
    if not classify(s, to_l, cc_l, bcc_l, sensitivity):
        return GuardedMessage(_dedupe(to_l), _dedupe(cc_l), _dedupe(bcc_l), sensitive=False)

    mgmt = management_addresses(s)
    owner = _addr(s.owner_email)
    owner_ok = bool(owner) and owner in mgmt
    changes: list[str] = []
    new_to: list[str] = []
    for a in to_l:
        if a in mgmt:
            new_to.append(a)
        elif is_shared_mailbox(s, a):
            changes.append(f"to {a} -> owner")
        else:
            log.warning("Mail guard blocked a management-only email addressed to non-management %s", a)
            raise MailGuardError(f"Blocked: this email is management/finance-sensitive and {a} is not a management "
                                 "address (MANAGEMENT_EMAILS / owner / partner).")
    if not new_to:
        if not owner_ok:
            log.warning("Mail guard blocked a management-only email to %s: no owner address to redirect to", to_l)
            raise MailGuardError("Blocked: management/finance-sensitive email was addressed to a shared inbox and "
                                 "OWNER_EMAIL isn't a usable management address to redirect it to.")
        new_to.append(owner)
    new_cc = [a for a in cc_l if a in mgmt]
    new_bcc = [a for a in bcc_l if a in mgmt]
    changes += [f"cc {a} dropped" for a in cc_l if a not in mgmt]
    changes += [f"bcc {a} dropped" for a in bcc_l if a not in mgmt]
    if changes:
        log.warning("Mail guard rewrote a management-only email: %s", "; ".join(changes))
    return GuardedMessage(_dedupe(new_to), [a for a in _dedupe(new_cc) if a not in new_to],
                          [a for a in _dedupe(new_bcc) if a not in new_to], sensitive=True, changes=changes)
