"""Regulatory watch: UK tax, employment law and fire & security regulation changes that the
directors need to know about, researched on the web weekly and on request."""

from __future__ import annotations

import logging
from datetime import date

from ..brain import llm

log = logging.getLogger(__name__)

WATCH_SYSTEM = """You are Jarvis, keeping the directors of {company} - a UK (England, West Yorkshire) fire and
security installation and maintenance company, a limited company with employed engineers and office staff, vans,
VAT-registered and working in construction supply chains (CIS) - on top of legal and tax changes.

Research the web for changes announced, consulted on, or taking effect in roughly the last {window} and the next
12 months. Prefer primary sources: gov.uk, HMRC, legislation.gov.uk, ACAS, HSE, the Home Office / fire safety
guidance, BSI, BAFE, NSI, SSAIB, FIA. Cover:
1. Tax - corporation tax, VAT (incl. domestic reverse charge), CIS, PAYE/NIC, dividends and directors' pay,
   Making Tax Digital, capital allowances, company car / van benefit, pensions.
2. Employment law - Employment Rights Act changes (day-one rights, SSP, unfair dismissal, zero hours, fire and
   rehire, union rights), National Minimum/Living Wage, holiday pay, family leave, right to work, IR35.
3. Company law - Companies House identity verification, filing changes.
4. Fire & security regulation - Fire Safety (England) Regulations, Building Safety Act, BS 5839 / BS 5266 /
   BS EN 50131 revisions, BAFE/NSI/SSAIB scheme changes, the analogue/2G/3G switch-off, police alarm policy.
5. Health & safety and driving/vehicle rules relevant to a van fleet.

Write for {owner}{partner}: only what matters to this business. For each item: what's changing, the date, what
it means for us (with rough £ impact where you can), and the action to take. Put anything with a deadline in the
next 90 days first. Cite the source link for each item. Say plainly if something is proposed rather than law.
{focus}"""

TECHNICAL_WATCH_SYSTEM = """You are Jarvis, deepening {company}'s own technical competence in fire and security
systems - not tracking legal/regulatory changes (that's a separate watch), but building real installer-level
expertise: how to design, install, commission and maintain systems correctly, and where installers most often
get it wrong.

Research the web for the last {window}. Prefer primary and professional sources: BSI standard revisions and
their practical implications, FIA (Fire Industry Association) technical bulletins and guidance notes, BAFE/
NSI/SSAIB technical (not just scheme-administration) guidance, manufacturer technical bulletins for equipment
Salts commonly works with (fire panels, emergency lighting, intruder, CCTV, access control), and reputable UK
fire & security installer trade press and professional forums for real-world best practice and commonly
reported problems - never for anyone's access codes, passwords or credentials, which is out of scope here
under any circumstances; if a source is actually a credential/leak forum, skip it and don't cite it.

Cover: BS 5839-1/-6, BS EN 54, BS 5266-1, BS EN 50131, PD 6662, BS 8243, BS EN 62676, BS 8418, BS 7273-4,
BS 5306, and any related standard revisions or corrigenda; commissioning and handover best practice; common
causes of failed inspections/audits and false alarms; and anything genuinely new worth an engineer knowing.

Check what's already known before repeating it (below). For each finding: what it is, why it matters
practically for installation/maintenance work, and the source. Say plainly when something is guidance/opinion
rather than a normative requirement. If nothing genuinely new turned up, say so briefly rather than padding.
{focus}"""


class RegulatoryWatch:
    def __init__(self, settings, db, notifier, client, bus, mail):
        self.s = settings
        self.db = db
        self.notifier = notifier
        self.client = client
        self.bus = bus
        self.mail = mail
        self.actions = None  # set after construction

    def _names(self) -> str:
        return f" and {self.s.partner_name}" if self.s.partner_name else " and the business partner"

    async def briefing(self, focus: str | None = None, window: str = "month", deliver: bool = False) -> str:
        previous = self.db.get_kv("regwatch_last", "")
        focus_line = (f"Focus especially on: {focus}." if focus else "") + (
            f"\nLast time you reported (don't repeat unchanged items, just say 'no change' briefly):\n{previous[:6000]}"
            if previous and not focus else "")
        text = await llm.research(self.client, self.s,
                                  system=WATCH_SYSTEM.format(company=self.s.company_name, window=window,
                                                             owner=self.s.owner_name, partner=self._names(),
                                                             focus=focus_line),
                                  prompt=f"Today is {date.today():%d %B %Y}. Prepare the regulatory update.")
        self.bus.publish("display", {"title": "Tax, employment & regulation watch", "markdown": text})
        if not focus:
            self.db.set_kv("regwatch_last", text)
        if deliver:
            await self.notifier.notify("Weekly tax & employment law watch", text[:3000], level="info", push=True)
            if self.s.partner_email and self.actions is not None:
                self.actions.queue("email_send", f"Send this week's tax & employment law update to {self.s.partner_name or self.s.partner_email}",
                                   {"to": [self.s.partner_email], "cc": [], "subject": "[Jarvis] Tax & employment law watch",
                                    "body": text})
        return text

    async def weekly(self) -> None:
        await self.briefing(window="week", deliver=True)

    async def technical(self, focus: str | None = None, window: str = "month", deliver: bool = False) -> str:
        previous = self.db.get_kv("technical_watch_last", "")
        focus_line = (f"Focus especially on: {focus}." if focus else "") + (
            f"\nAlready known from last time (don't repeat unchanged items, just say 'no change' briefly):\n"
            f"{previous[:6000]}" if previous and not focus else "")
        text = await llm.research(self.client, self.s,
                                  system=TECHNICAL_WATCH_SYSTEM.format(company=self.s.company_name, window=window,
                                                                       focus=focus_line),
                                  prompt=f"Today is {date.today():%d %B %Y}. Prepare the technical/standards update.")
        self.bus.publish("display", {"title": "Fire & security technical watch", "markdown": text})
        if not focus:
            self.db.set_kv("technical_watch_last", text)
        if deliver:
            await self.notifier.notify("Weekly fire & security technical watch", text[:3000], level="info", push=True)
        return text

    async def technical_weekly(self) -> None:
        await self.technical(window="week", deliver=True)
