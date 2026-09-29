---
title: Taking over a customer's existing system — getting proper access
aliases: [system takeover, access recovery, engineer code recovery]
tags: [company, access-recovery]
type: note
summary: The right process for getting access to a system Salts is taking over or has been locked out of — never by sourcing another company's codes.
created: 2026-09-29
updated: 2026-09-29
status: evergreen
related: ["[[salts-fire-and-security]]", "[[company-moc]]"]
---

# Taking over a customer's existing system - getting proper access

Summary: what to do when a customer wants Salts to take over maintenance of a fire alarm, intruder, CCTV or
access control system another company installed, and the outgoing installer won't hand over the engineer
code - **or** when it's a system Salts itself installed or already holds the maintenance contract for, and a
different company has since been in and changed the engineer code, locking Salts out of its own work. Either
way, the system belongs to the customer, not whichever installer last touched it - but the right way to get
access is still a proper process, never searching for someone else's codes online.

## "We installed it, but someone else changed the code"

This happens - a customer brings in another company (sometimes without telling Salts, sometimes mid-dispute
over who holds the contract) and that company changes the engineer code during their visit. The process below
is exactly the same as a normal takeover: Salts doesn't own the code any more than the other company does: the
customer does, and getting back in still runs through them, in writing, then a manufacturer-documented reset
if needed. Being the original installer doesn't change that, and doesn't justify trying to work around it any
other way. Once Salts is back in, use `site_access_code_update` to record the (now Salts-set) code against the
site so this doesn't recur next time someone forgets to update it - see below.

## The principle

The customer owns the hardware. An installer's engineer/access code exists to control who can change
settings on that system, not to lock the customer out of their own property. A customer who wants to switch
maintenance provider is entitled to get into their own system - through the right channel, not by Jarvis (or
anyone) sourcing another company's access codes from forums, leaked lists or anywhere else. Codes found that
way aren't attached to any record of who they actually belong to, so using one found online to get into a
system risks accessing a site that has nothing to do with the customer in question - that's unauthorised
access to someone else's property, an offence under the Computer Misuse Act regardless of where the code came
from, and it's not how this gets solved anyway.

## The right process, in order

1. **Ask the customer to request the handover directly.** BAFE (SP203-1), SSAIB and NSI all expect an
   accredited company to cooperate with a legitimate handover when a customer moves their maintenance
   elsewhere - most of the time asking is all it takes. Put it in writing so there's a record if it's needed
   later.
2. **If the previous installer won't cooperate**, a factory reset is usually possible - most panels, NVRs and
   access control systems have a manufacturer-documented reset procedure for exactly this situation. The
   procedure and default codes vary by model and firmware, so always check the **current official
   documentation for that exact model** (the manufacturer's own site or technical support line) rather than
   relying on memory or a general guide - use `web_search` for this when it comes up, since procedures do get
   updated. Never guess at a reset procedure on a live fire alarm panel.
3. **A factory reset on a live system is safety-sensitive work**, not just an access step - it can affect
   life-safety functionality (zones, outputs, monitoring) until it's reconfigured. Only a competent engineer
   does this, on site, following the manufacturer's procedure, with the same care as any other panel
   reconfiguration: warn anyone on site, isolate outputs as appropriate, and fully recommission and test
   before leaving.
4. **Document the takeover** - note the reset, who authorised it (the customer, in writing), and what was
   reconfigured, the same as any other job.

## Recording the code once Salts is back in

`site_access_code_update` records an engineer/access code against a site and system, encrypted at rest, and
`site_access_code` looks one up by site - a secure replacement for an engineer's paper site-code book, for
systems Salts actually installs or maintains. Use it once a code is legitimately known (set during
commissioning, given by the customer, or the result of the process above) so it's available for the next
engineer who needs it. Never record a code obtained any other way, and never use these tools to build up codes
for systems Salts has no maintenance relationship with.

## Current reset procedures and documentation

Reset procedures and default codes vary by model and firmware and do get updated, so look them up fresh with
`web_search` against the manufacturer's own current documentation or technical support line rather than
relying on memory or a general guide (`knowledge_search` first for anything Salts has already documented about
a specific model). Never guess at a reset procedure on a live fire alarm panel.

## What this is not

This is not a reason to collect or store access codes for systems generally. Jarvis should never search
forums, leaked-credential sites or similar for anyone's engineer codes or passwords, for this or any other
purpose - only ever use the process above, for a specific customer's own property, when it actually comes up.
