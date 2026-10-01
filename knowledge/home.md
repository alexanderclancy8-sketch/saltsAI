---
title: Home
type: moc
tags: [moc]
updated: 2026-09-29
---

# Salts Knowledge Vault — Home

Single entry point for this vault, for a human opening it in Obsidian and for Jarvis reading it as a knowledge base ([jarvis/knowledge.py](../jarvis/knowledge.py)). [[company-moc|Company]] and [[fsm-moc|Salts FSM]] are always loaded into Jarvis's system prompt; everything else is retrieved on demand by search.

## Topic MOCs
- [[company-moc]] — who we are, access recovery
- [[fsm-moc]] — the field service management app
- [[finance-moc]] — UK tax and accounting
- [[legislation-moc]] — fire safety law, certification and competency
- [[operations-moc]] — contracts, scheduling, SLAs, KPIs
- [[standards-moc]] — British Standards by system type

## Private knowledge
`private/` is git-ignored and not listed here — see its `README.md` for what belongs there (pricing, key customers, staff matters). Jarvis loads everything in it regardless.

## Conventions
- Every note has frontmatter: `title`, `aliases`, `tags`, `type`, `summary`, `created`, `updated`, `status`, `related`.
- `tags` are drawn from a fixed vocabulary — reuse existing tags rather than inventing new ones (see any note's frontmatter for the current set: company, fsm, finance, tax, legislation, fire-safety-law, certification, standards, fire-alarms, emergency-lighting, extinguishers, intruder-alarms, cctv, access-control, gdpr, operations, ppm, access-recovery, moc).
- Wikilinks (`[[note]]`) resolve by filename, not path — every filename in this vault is unique, so bare `[[note-name]]` works from anywhere.
- Rename or move a note from inside Obsidian, not by editing the file directly, so backlinks don't break.
