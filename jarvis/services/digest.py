"""Weekly digest of Jarvis' own routine engineering notices.

Routine notices ("PR ready", fix ready, deploy/merge results, routine-test recoveries, issue triage, non-critical
security findings, self-learning summaries) are no longer sent one by one. `Notifier.notify(..., kind=...)` writes
them to the `digest_items` table, and once a week (WEEKLY_DIGEST_CRON, default Monday 08:00 UK time) `WeeklyDigest`
compiles everything stored into ONE message: Teams only (never email), also shown on the display, and kept in the
`digests` table so it can be read again later. Stored items are marked as digested so nothing goes out twice.

What is digested and what is sent immediately is decided by NOTIFICATION_ROUTES below:

* a kind that is not in the map, or has no kind at all, is sent immediately ("when unsure, send immediately");
* kinds in ALWAYS_IMMEDIATE (life-safety/compliance alerts, failed deploys and outages, critical or high
  security findings, failing routine tests, anything needing approval by a deadline) can never be moved to the
  digest, not even by the NOTIFICATION_ROUTES setting;
* a notice at level "critical" is never held back, whatever its kind.

This module only reads and writes Jarvis' own local store. It never merges, deploys or sends anything except the
one digest message to the owner's Teams channel, and it does not touch the approval queue: pending approvals stay
on the display the moment they are created and are merely *listed* in the digest.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IMMEDIATE = "immediate"
DIGEST = "digest"

# The clear map: notification kind -> how it is delivered. Edit here (or override single entries with the
# NOTIFICATION_ROUTES setting, e.g. "ci_passed=immediate"). Entries marked (locked) are in ALWAYS_IMMEDIATE.
NOTIFICATION_ROUTES: dict[str, str] = {
    # --- held for the weekly digest ---------------------------------------------------------------------
    "pr_ready": DIGEST,                 # self-improvement PR opened against Jarvis' own repo
    "self_improve_ci_passed": DIGEST,
    "self_improve_ci_failed": DIGEST,
    "self_improve_nothing": DIGEST,     # "Nothing to propose"
    "fix_ready": DIGEST,                # fix PR opened for a reported issue (the approval itself stays queued)
    "fix_sent_to_claude": DIGEST,       # issue handed to the Claude Code GitHub Action
    "fix_ci_passed": DIGEST,
    "fix_ci_failed": DIGEST,
    "deploy_started": DIGEST,           # fix PR merged (after approval), deploy under way
    "deploy_succeeded": DIGEST,         # "fixed and live"
    "pr_merged": DIGEST,                # recorded by the digest itself when GitHub shows a PR was merged
    "pr_closed": DIGEST,
    "routine_test_recovered": DIGEST,   # routine-test pass results
    "issue_triaged": DIGEST,
    "security_finding_minor": DIGEST,   # low / medium severity only
    "security_review_clean": DIGEST,    # "nothing new"
    "self_learning_summary": DIGEST,
    # --- sent immediately ---------------------------------------------------------------------------------
    "deploy_failed": IMMEDIATE,         # (locked) failed deploy / production problem
    "routine_test_failed": IMMEDIATE,   # (locked) routine tests run against the live system
    "security_finding_urgent": IMMEDIATE,  # (locked) critical / high, or any severity we don't recognise
    "issue_triaged_urgent": IMMEDIATE,  # (locked) triage says critical / high
    "approval_deadline": IMMEDIATE,     # (locked) anything explicitly needing approval within a deadline
    "life_safety_alert": IMMEDIATE,     # (locked) e.g. lone-worker safety checks
    "compliance_alert": IMMEDIATE,      # (locked)
    "issue_reported": IMMEDIATE,        # a person reported a problem
    "fix_failed": IMMEDIATE,            # auto-fix attempt failed
    "fix_needs_you": IMMEDIATE,         # the engineer agent needs a human decision
    "self_improve_failed": IMMEDIATE,
    "security_review_failed": IMMEDIATE,
}

ALWAYS_IMMEDIATE = frozenset({
    "deploy_failed", "routine_test_failed", "security_finding_urgent", "issue_triaged_urgent", "approval_deadline",
    "life_safety_alert", "compliance_alert",
})

LOW_SEVERITIES = ("low", "medium")


def parse_route_overrides(text: str) -> dict[str, str]:
    """'ci_passed=immediate, issue_triaged=digest' -> {...}. Malformed entries are ignored."""
    out: dict[str, str] = {}
    for part in re.split(r"[,;\n]", text or ""):
        if "=" not in part:
            continue
        kind, route = (x.strip().lower() for x in part.split("=", 1))
        if kind and route in (IMMEDIATE, DIGEST):
            out[kind] = route
    return out


def route_for(kind: str | None, level: str = "info", overrides: dict[str, str] | None = None) -> str:
    """IMMEDIATE or DIGEST. Anything unsure is IMMEDIATE."""
    if not kind or level == "critical" or kind in ALWAYS_IMMEDIATE:
        return IMMEDIATE
    route = (overrides or {}).get(kind) or NOTIFICATION_ROUTES.get(kind)
    return route if route in (IMMEDIATE, DIGEST) else IMMEDIATE


def security_kind(severity: str) -> str:
    """Only clearly non-critical findings may wait for the digest; unknown severities are treated as urgent."""
    return "security_finding_minor" if str(severity).lower() in LOW_SEVERITIES else "security_finding_urgent"


def triage_kind(severity: str) -> str:
    return "issue_triaged" if str(severity).lower() in LOW_SEVERITIES else "issue_triaged_urgent"


# --------------------------------------------------------------------------------------------- the digest
PR_OPEN_KINDS = {"pr_ready", "fix_ready"}
PR_MERGED_KINDS = {"pr_merged", "deploy_started", "deploy_succeeded", "deploy_failed"}  # a deploy means it merged
PR_CLOSED_KINDS = {"pr_closed"}
STALE_PR_DAYS = 7
SECTION_LINES = 12
MAX_TEXT = 6000
ALL_CLEAR = "Weekly digest: all clear - nothing to report this week."

SECTION_OF_KIND = {
    "pr_ready": "opened", "fix_ready": "opened",
    "pr_merged": "merged", "deploy_started": "merged", "pr_closed": "merged",
    "deploy_succeeded": "deployed", "deploy_failed": "deploy_problems",
    "routine_test_failed": "tests", "routine_test_recovered": "tests",
    "issue_triaged": "issues", "issue_triaged_urgent": "issues", "issue_reported": "issues",
    "security_finding_minor": "security", "security_finding_urgent": "security", "security_review_clean": "security",
    "fix_ci_passed": "checks", "fix_ci_failed": "checks", "self_improve_ci_passed": "checks",
    "self_improve_ci_failed": "checks", "fix_sent_to_claude": "checks", "self_improve_nothing": "checks",
    "self_learning_summary": "learning",
    "fix_needs_you": "decide", "fix_failed": "decide", "self_improve_failed": "decide",
    "security_review_failed": "decide",
}

SECTION_TITLES = [
    ("opened", "Pull requests opened"),
    ("merged", "Merged / closed"),
    ("deployed", "Fixes deployed"),
    ("deploy_problems", "Deploy problems (already alerted at the time)"),
    ("tests", "Test failures and recoveries"),
    ("issues", "Issues"),
    ("security", "Security"),
    ("checks", "Checks and hand-offs"),
    ("learning", "Self-learning"),
    ("other", "Other"),
]


def _parse(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _line(item: dict) -> str:
    text = item["title"]
    if item.get("status"):
        text += f" [{item['status']}]"
    if item.get("link"):
        text += f" - {item['link']}"
    return f"- {text}"


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= SECTION_LINES:
        return lines
    return lines[:SECTION_LINES] + [f"- ...and {len(lines) - SECTION_LINES} more"]


class WeeklyDigest:
    def __init__(self, j):
        self.j = j
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ PR state
    def pr_states(self, now: datetime | None = None) -> list[dict]:
        """Every PR Jarvis has opened (from the store, however old) with open/merged/closed and its age."""
        db = self.j.db
        now = now or datetime.now(timezone.utc)
        ended_merged = {i["ref"] for i in db.digest_items_by_kind(PR_MERGED_KINDS) if i["ref"]}
        ended_closed = {i["ref"] for i in db.digest_items_by_kind(PR_CLOSED_KINDS) if i["ref"]}
        out = []
        for item in db.digest_items_by_kind(PR_OPEN_KINDS):
            state = ("merged" if item["ref"] in ended_merged else
                     "closed" if item["ref"] in ended_closed else "open")
            out.append({"item": item, "state": state, "age_days": (now - _parse(item["created_at"])).days})
        return out

    async def refresh_pr_states(self) -> None:
        """Ask GitHub (read-only) whether PRs we think are open were merged or closed in the meantime, and store
        that. Self-improvement PRs are merged by a human on GitHub, so Jarvis would otherwise never find out."""
        j = self.j
        checked = 0
        for p in self.pr_states():
            if p["state"] != "open" or checked >= 30:
                continue
            m = re.match(r"^(fix|self):(\d+)$", p["item"]["ref"] or "")
            gh = {"fix": j.github, "self": j.self_github}.get(m.group(1)) if m else None
            if gh is None:
                continue
            checked += 1
            try:
                pr = await gh.pr(int(m.group(2)))
            except Exception as e:  # noqa: BLE001 - a GitHub blip must not stop the digest
                log.info("Could not check PR %s for the digest: %s", p["item"]["ref"], e)
                continue
            item = p["item"]
            if pr.get("merged"):
                j.db.add_digest_item("pr_merged", f"Merged: {item['title']}", link=item["link"], status="merged",
                                     ref=item["ref"])
            elif pr.get("state") == "closed":
                j.db.add_digest_item("pr_closed", f"Closed without merging: {item['title']}", link=item["link"],
                                     status="closed", ref=item["ref"])

    # ------------------------------------------------------------------ building the text
    def build(self, now: datetime | None = None) -> dict:
        j = self.j
        db = j.db
        now = now or datetime.now(timezone.utc)
        try:
            local = now.astimezone(ZoneInfo(j.settings.timezone))
        except Exception:  # noqa: BLE001
            local = now
        pending = db.pending_digest_items()
        sections: dict[str, list[dict]] = defaultdict(list)
        for it in pending:
            sections[SECTION_OF_KIND.get(it["kind"], "other")].append(it)
        states = {p["item"]["id"]: p for p in self.pr_states(now)}
        open_prs = [p for p in states.values() if p["state"] == "open"]
        approvals = db.pending_actions()
        open_issues = db.list_issues("open", 10)
        failing = [t for t in db.latest_test_results() if not t["ok"]]
        needs_human = [i for i in open_issues if i["status"] == "needs_human"]

        blocks: list[tuple[str, list[str]]] = []
        for key, title in SECTION_TITLES:
            items = sections.get(key, [])
            lines = []
            if key == "opened":
                for it in items:
                    st = states.get(it["id"])
                    lines.append(_line({**it, "status": st["state"] if st else it["status"]}))
            elif key == "learning":
                if items:
                    latest = f"; latest: {items[-1]['body'][:200]}" if items[-1]["body"] else ""
                    lines = [f"- {len(items)} reflection(s) run{latest}"]
            else:
                lines = [_line(it) for it in items]
            if key == "tests" and failing:
                lines += [f"- Still failing now: {t['name']} - {t['detail'][:120]}" for t in failing]
            if key == "issues" and open_issues:
                lines += [f"- Open: #{i['id']} ({i['severity']}, {i['status']}) {i['title'][:100]}"
                          for i in open_issues]
            if lines:
                blocks.append((title, _capped(lines)))
            if key == "merged" and open_prs:
                waiting = []
                for p in sorted(open_prs, key=lambda p: -p["age_days"]):
                    flag = f"  ** open {p['age_days']} days - not merged **" if p["age_days"] >= STALE_PR_DAYS else ""
                    waiting.append(f"- {p['item']['title']}" + (f" - {p['item']['link']}" if p["item"]["link"] else "")
                                   + flag)
                blocks.append(("Still awaiting review / merge", _capped(waiting)))

        decide = [_line(it) for it in sections.get("decide", [])]
        decide += [f"- Approval #{a['id']}: {a['summary'][:160]}" for a in approvals]
        decide += [f"- Issue #{i['id']} needs a human: {i['title'][:100]}" for i in needs_human]
        if decide:
            blocks.insert(0, ("Needs your decision", _capped(decide)))

        has_content = bool(pending or open_prs or approvals or open_issues or failing)
        head = f"Weekly digest - {local:%A %d %B %Y}"
        body = "\n\n".join(f"{title}\n" + "\n".join(lines) for title, lines in blocks)
        text = f"{head}\n\n{body}".strip() if has_content else ALL_CLEAR
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT].rsplit("\n", 1)[0] + "\n...(truncated - the full digest is stored on the display)"
        return {"text": text, "item_ids": [it["id"] for it in pending], "has_content": has_content}

    # ------------------------------------------------------------------ running it
    async def run(self, source: str = "scheduled", deliver: bool = True) -> dict:
        """Compile, store and (if `deliver`) post ONE Teams message. `source` is 'scheduled' or 'on_demand'.
        The on-demand tool passes deliver=False: it is shown on the display / in the chat and stored, and the
        items are marked digested so the next scheduled digest doesn't repeat them."""
        j = self.j
        async with self._lock:
            await self.refresh_pr_states()
            built = self.build()
            text, ids = built["text"], built["item_ids"]
            if not built["has_content"] and source == "scheduled" and not j.settings.weekly_digest_all_clear:
                return {"sent": False, "reason": "nothing to report", "text": text}
            delivered, mark = "display", True
            if deliver and j.teams.enabled:
                subject = f"Weekly digest {datetime.now():%a %d %b}"
                via = await j.notifier.send_owner_update(subject, text, channels=("teams",))  # Teams only, no email
                if via == "Teams":
                    delivered = "Teams + display"
                else:
                    delivered, mark = "display only (Teams post failed)", False  # keep items for next time
            elif deliver:
                delivered = "display (Teams not set up)"
            digest_id = j.db.add_digest(source, len(ids), text, delivered)
            if mark:
                j.db.mark_digested(ids, digest_id)
            await j.notifier.notify("Weekly digest", text, level="info", push=False)  # the display
            j.bus.publish("digest", {"id": digest_id, "text": text, "delivered": delivered})
            return {"sent": deliver and delivered.startswith("Teams"), "id": digest_id, "items": len(ids),
                    "delivered": delivered, "text": text}

    async def scheduled(self) -> None:
        if not self.j.settings.weekly_digest_enabled:
            return
        await self.run("scheduled", deliver=True)

    def latest(self) -> dict | None:
        rows = self.j.db.list_digests(1)
        return rows[0] if rows else None
