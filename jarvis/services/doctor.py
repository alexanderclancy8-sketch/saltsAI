"""Jarvis's self-diagnostics: what is quietly broken, one line per item, each ok / amber / red with a next step.

The read-only ``doctor`` tool runs nine checks and puts the result on the display:

1. plugins switched on in Settings but inert (``mcp_plugins.yaml``), or on with no entry there at all,
2. data sources still on DEMO sample data (``demo_guard``),
2b. what the FSM lets Jarvis read ("FSM data access: N groups, M resources, scope off: ..."),
3. which keys are set - by NAME only (never a value; includes the free Companies House key),
4. the owner's automations: last run, a long run of NOTHING_TO_REPORT, running too often out of hours,
5. engineering-agent runs that are stalled, failed or gave up in the last 24 hours,
6. open requests (issues, approvals) nobody has touched for over 24 hours,
7. open pull requests that are red for over 24 hours or have conflicts,
8. failing routine tests and issues that need a human.

Everything here only READS. Each check fails soft: if one breaks it becomes a single "could not check: <reason>" line and
the others still run. No secret value is ever put in a line, a log message or an error reason: a key is reported as
"set" or "not set", and any reason text is redacted and has every configured secret value removed before it is shown.
Nothing here approves, queues, sends or changes anything, and no setting is written.
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import demo_guard
from ..brain import plugins
from ..cron import cron_trigger
from ..integrations.github_pr import PRClient
from ..redact import redact_text
from ..settings_store import SECTIONS
from .activity import BASELINE, NO_CHANGE
from .agent_runs import AgentRuns

log = logging.getLogger(__name__)

OK, AMBER, RED = "ok", "amber", "red"
_RANK = {RED: 0, AMBER: 1, OK: 2}

STALE_AFTER = timedelta(hours=24)        # a request / PR / run untouched for this long is flagged
NOTHING_STREAK = 20                      # this many NOTHING_TO_REPORT runs in a row is worth a look
MIN_GAP_OUT_OF_HOURS = timedelta(minutes=30)
CRON_WINDOW = timedelta(days=7)
CRON_MAX_FIRES = 4000                    # bounds the schedule walk for an every-minute cron
MAX_LISTED = 5                           # ids named in one line
SAME_AS_LAST_TIME = "Same as last time."  # the activity detail automations record for a repeated finding

# label in Settings, the switch on Settings, the entry in mcp_plugins.yaml
PLUGINS = (
    ("Context7", "plugin_context7_enabled", "context7"),
    ("Superpowers", "plugin_superpowers_enabled", "superpowers"),
    ("Browser Use", "plugin_browser_use_enabled", "browser_use"),
    ("ThoughtProof", "plugin_thoughtproof_enabled", "thoughtproof"),
)

# The names an owner knows each key by -> the Settings field holding it. Only ever asked "is it set?".
KEYS = (
    ("OPENAI_API_KEY", "openai_api_key"),
    ("IMAGE_API_KEY", "image_api_key"),
    ("ELEVENLABS_API_KEY", "elevenlabs_api_key"),
)
RAM_KEYS = (
    ("RAM_CLIENT_ID", "ram_client_id"),
    ("RAM_API_KEY", "ram_api_key"),
    ("RAM_USERNAME", "ram_username"),
    ("RAM_PASSWORD", "ram_password"),
)
# Env-only credentials that are not on the Settings page (so not found through settings_store) - kept explicitly.
_EXTRA_SCRUB_FIELDS = ("anthropic_api_key", "claude_code_oauth_token", "jarvis_secret_key", "azure_kudu_password",
                       "ram_username")
# Any setting NAMED like a credential is scrubbed too, so a secret added later is covered without anyone remembering to
# list it here (a false match only means an error message loses a value it should not have shown anyway).
_SECRET_NAME = re.compile(r"(?:^|_)(?:secret|token|password|passwd|pass|apikey|sig)(?:$|_)|_key$|connection_string$|"
                          r"webhook_url$")


def secret_fields(settings: Any = None) -> tuple[str, ...]:
    """Every Settings attribute whose VALUE must never appear in anything shown (used only to REMOVE it from reason
    text): every secret-kind field on the Settings page, the env-only credentials above, and any setting whose name looks
    like a credential. Worked out on each call, so nothing has to be added here when a new secret appears."""
    found = {f.key for section in SECTIONS for f in section.fields if f.kind == "secret"}
    found.update(f for _, f in KEYS + RAM_KEYS)
    found.update(_EXTRA_SCRUB_FIELDS)
    fields = getattr(type(settings), "model_fields", None) or {}
    found.update(n for n, f in fields.items() if f.annotation is str and _SECRET_NAME.search(n))
    return tuple(sorted(found))


@dataclass(frozen=True)
class Item:
    check: str
    status: str          # ok | amber | red
    line: str
    next_step: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"check": self.check, "status": self.status, "line": self.line, "next_step": self.next_step}


def _when(text: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clip(text: Any, limit: int = 80) -> str:
    return redact_text(" ".join(str(text or "").split()))[:limit]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _is_set(settings: Any, field: str) -> bool:
    """True when the setting holds something. The value itself goes no further than this function."""
    return bool(str(getattr(settings, field, "") or "").strip())


class Doctor:
    def __init__(self, j: Any):
        self.j = j
        self.ran: list[str] = []
        self.last_items: list[Item] = []

    # ------------------------------------------------------------------ running
    # (name shown when the check itself breaks, method name) - looked up at run time, one broken check never stops the rest
    CHECKS = (
        ("Plugins", "_plugins"), ("Data sources", "_demo"), ("FSM data access", "_fsm_data"),
        ("FSM documents", "_fsm_documents"), ("Keys", "_keys"),
        ("Automations", "_automations"),
        ("Agent runs", "_agent_runs"), ("Open requests", "_requests"), ("Pull requests", "_pull_requests"),
        ("Tests and issues", "_tests_and_issues"), ("Question checks", "_question_checks"),
    )

    def _hide_secrets(self, text: str) -> str:
        s = self.j.settings
        for field in secret_fields(s):
            value = str(getattr(s, field, "") or "")
            if len(value) >= 4:
                text = text.replace(value, "[hidden]")
        return text

    def _reason(self, e: BaseException) -> str:
        what = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        try:
            return _clip(self._hide_secrets(what), 160)  # remove configured values first, then redact and shorten
        except Exception:  # noqa: BLE001 - even the scrubbing must not stop the report; say less instead
            return type(e).__name__

    async def run(self, now: datetime | None = None) -> list[Item]:
        """Every check's lines, worst first. A check that raises becomes one amber 'could not check' line."""
        now = now or datetime.now(timezone.utc)
        items: list[Item] = []
        ran: list[str] = []
        for name, method in self.CHECKS:
            try:
                got = getattr(self, method)(now)
                if inspect.isawaitable(got):
                    got = await got
                found = list(got)
                if not all(isinstance(i, Item) for i in found):
                    raise TypeError("the check returned something that is not a report line")
                items.extend(found)
                ran.append(name)
            except Exception as e:  # noqa: BLE001 - fail soft: this is the whole point
                reason = self._reason(e)
                log.warning("Doctor: the %s check could not run (%s)", name, type(e).__name__)
                items.append(Item(name, AMBER, f"could not check: {reason}",
                                  "Look in the Jarvis log for this check; the other checks still ran."))
        self.ran = ran          # which checks completed: the fault log (services/faults.py) only closes faults for these
        return sorted(items, key=lambda i: _RANK.get(i.status, 1))  # stable: keeps the check order within a status

    @staticmethod
    def markdown(items: list[Item]) -> str:
        counts = {s: sum(1 for i in items if i.status == s) for s in (RED, AMBER, OK)}
        lines = [f"**Jarvis health check** - {counts[RED]} red, {counts[AMBER]} amber, {counts[OK]} ok", ""]
        for i in items:
            step = f" -> Next: {i.next_step}" if i.next_step and i.status != OK else ""
            lines.append(f"- **{i.status.upper()}** | {i.check} | {i.line}{step}")
        return "\n".join(lines)

    async def diagnose(self, now: datetime | None = None) -> dict[str, Any]:
        """Run everything, put it on the display and hand the lines back. Showing it is also fail-soft."""
        items = await self.run(now)
        self.last_items = items
        text = redact_text(self.markdown(items))
        shown = True
        try:
            self.j.bus.publish("display", {"title": "Jarvis health check", "markdown": text})
        except Exception as e:  # noqa: BLE001
            shown = False
            log.warning("Doctor: could not put the report on the display (%s)", type(e).__name__)
        return {"summary": {s: sum(1 for i in items if i.status == s) for s in (RED, AMBER, OK)},
                "items": [i.as_dict() for i in items], "shown_on_display": shown}

    # ------------------------------------------------------------------ 1. plugins
    def _plugin_problems(self, key: str, spec: dict[str, Any]) -> list[str]:
        """Why a plugin that is switched on cannot work - asked of the SAME code that decides whether it starts, so a plugin
        is never reported ok while Jarvis itself treats it as inert."""
        s = self.j.settings
        if key == "context7":
            why = plugins.engineering_setup(s).problems.get("Context7", "")
        elif key == "browser_use":
            why = plugins.chat_setup(s).problems.get("Browser Use", "")
        elif key == "thoughtproof":
            why = self.j.verifier.problem()
        else:
            why = plugins.launch_config(spec)[1]
        return [why] if why else []

    async def _plugins(self, now: datetime) -> list[Item]:
        name = "Plugins"
        s = self.j.settings
        on = [(label, key) for label, flag, key in PLUGINS if getattr(s, flag, False)]
        if not on:
            return [Item(name, OK, "No plugins are switched on in Settings.")]
        specs = plugins.load_specs(s.plugins_file)
        if not specs:
            return [Item(name, AMBER, "Plugins are switched on but mcp_plugins.yaml could not be read or is empty.",
                         "Check mcp_plugins.yaml is present and valid; it is only changed by pull request.")]
        out: list[Item] = []
        for label, key in on:
            spec = specs.get(key)
            if spec is None:
                note = (" It only adds the plan/test/review wording, so nothing launches, but it is not documented there."
                        if key == "superpowers" else " It cannot start.")
                out.append(Item(name, AMBER, f"{label}: switched on in Settings but has no entry in mcp_plugins.yaml.{note}",
                                f"Add a {key} entry by pull request, or switch {label} off in Settings."))
                continue
            problems = self._plugin_problems(key, spec)
            if not problems:
                out.append(Item(name, OK, f"{label}: on and its launch spec looks complete."))
                continue
            # ThoughtProof fails closed: while it is on but broken, approved actions are refused.
            status = RED if key == "thoughtproof" else AMBER
            tail = " Approved actions are refused until it is fixed." if key == "thoughtproof" else " It is inert."
            out.append(Item(name, status, f"{label}: switched on but not working - {'; '.join(problems)}.{tail}",
                            "A human pins a verified version / confirms the sandbox in mcp_plugins.yaml by pull request, "
                            f"installs npx or uvx on the host, or switches {label} off in Settings."))
        return out

    # ------------------------------------------------------------------ 1b. the accuracy scorecard
    async def _question_checks(self, now: datetime) -> list[Item]:
        status, line, step = self.j.question_checks.doctor_line()
        return [Item("Question checks", AMBER if status == "amber" else OK, line, step)]

    # ------------------------------------------------------------------ 2. demo data
    async def _demo(self, now: datetime) -> list[Item]:
        demo = demo_guard.demo_now(self.j)
        out = []
        for key, source in demo_guard.SOURCES.items():
            if key in demo:
                label = source.label[:1].upper() + source.label[1:]
                out.append(Item("Data sources", AMBER, f"{label}: still on DEMO sample data, so Jarvis will not answer from it.",
                                source.connect + "."))
        return out or [Item("Data sources", OK, "No data source is on DEMO sample data.")]

    # ------------------------------------------------------------------ 2b. what the FSM lets Jarvis read
    async def _fsm_data(self, now: datetime) -> list[Item]:
        name = "FSM data access"
        info = await self.j.fsm_read.summary()
        state = info["state"]
        if state == "ok":
            off = info["scope_off"]
            return [Item(name, OK, f"FSM data access: {_plural(info['groups'], 'group')}, {_plural(info['resources'], 'resource')}, "
                                   f"scope off: {', '.join(off) if off else 'none'}.",
                         "A group that is scope off can be switched on in the FSM's Jarvis access settings." if off else "")]
        if state == "demo":
            return [Item(name, OK, "FSM data access: not available - Salts FSM is not connected yet (sample data only).",
                         "Set FSM_BASE_URL and the API key under Connections.")]
        if state == "unavailable":
            return [Item(name, AMBER, "FSM data access: the FSM doesn't expose its data API yet, so Jarvis uses its older "
                                      "FSM tools and its own vehicle/equipment register.",
                         "Nothing to do here - it starts working by itself once the FSM ships /api/jarvis/catalog.")]
        return [Item(name, AMBER, f"FSM data access: {_clip(info.get('message'), 120)}",
                     "Check the FSM key under Connections; Jarvis tries again by itself.")]

    # ------------------------------------------------------------------ 2c. reading inside FSM documents
    async def _fsm_documents(self, now: datetime) -> list[Item]:
        """'FSM documents: text on/off, files on/off' - from the same (cached) catalog the data line uses."""
        name = "FSM documents"
        info = await self.j.fsm_read.summary()
        state = info["state"]
        if state == "demo":
            return [Item(name, OK, "FSM documents: not available - Salts FSM is not connected yet (sample data only).",
                         "Set FSM_BASE_URL and the API key under Connections.")]
        cat = self.j.fsm_data.cached
        if state != "ok" or cat is None:
            return [Item(name, OK if state == "unavailable" else AMBER,
                         "FSM documents: text off, files off - " + ("the FSM doesn't expose its data API yet." if state == "unavailable"
                                                                    else f"{_clip(info.get('message'), 120)}"),
                         "" if state == "unavailable" else "Jarvis tries again by itself; check the FSM key under Connections.")]
        if not cat.document_text:
            return [Item(name, OK, "FSM documents: text off, files off - this FSM doesn't expose document reading yet.",
                         "Nothing to do here - it starts working by itself once the FSM ships its document text routes.")]
        files = cat.document_files
        return [Item(name, OK, f"FSM documents: text on, files {'on' if files else 'off'}."
                     + ("" if files else " Scans and photos can't be transcribed while the files switch is off."),
                     "" if files else "Switch on 'Jarvis may download document files' in the FSM (Settings > Integrations > "
                                      "Jarvis access) so scanned documents can be transcribed.")]

    # ------------------------------------------------------------------ 3. keys (names only)
    async def _keys(self, now: datetime) -> list[Item]:
        s = self.j.settings
        name = "Keys"
        out: list[Item] = []
        openai = _is_set(s, "openai_api_key")
        chosen_whisper = s.stt_provider == "whisper"
        if openai:
            out.append(Item(name, OK, "OPENAI_API_KEY: set"))
        elif chosen_whisper and s.effective_stt == "whisper":
            out.append(Item(name, RED, "OPENAI_API_KEY: not set, and Whisper is the chosen speech-to-text engine.",
                            "Set OPENAI_API_KEY in Settings, or choose Azure Speech / Deepgram instead."))
        else:
            out.append(Item(name, OK, "OPENAI_API_KEY: not set (optional - nothing needs it)"))
        image = _is_set(s, "image_api_key")
        if image:
            out.append(Item(name, OK, "IMAGE_API_KEY: set"))
        elif s.image_provider == "openai":
            out.append(Item(name, AMBER, "IMAGE_API_KEY: not set, but the image provider is 'openai' - graphics quietly use Claude.",
                            "Set IMAGE_API_KEY in Settings, or set the image provider to Claude."))
        else:
            out.append(Item(name, OK, "IMAGE_API_KEY: not set (not needed - Claude designs the graphics)"))
        eleven = _is_set(s, "elevenlabs_api_key")
        if eleven:
            out.append(Item(name, OK, "ELEVENLABS_API_KEY: set"))
        elif s.tts_provider == "elevenlabs":
            out.append(Item(name, RED, "ELEVENLABS_API_KEY: not set, but ElevenLabs is the chosen voice.",
                            "Set ELEVENLABS_API_KEY in Settings, or choose another voice."))
        else:
            out.append(Item(name, OK, "ELEVENLABS_API_KEY: not set (optional - another voice is used)"))
        if _is_set(s, "companies_house_api_key"):
            out.append(Item(name, OK, "COMPANIES_HOUSE_API_KEY: set" + ("" if s.companies_house_on_new_customers
                                                                       else " (new-customer approval line switched off)")))
        else:
            out.append(Item(name, OK, "COMPANIES_HOUSE_API_KEY: not set (optional - the pre-quote company check says it "
                                      "isn't connected yet)"))
        missing = [n for n, field in RAM_KEYS if not _is_set(s, field)]
        if not missing:
            out.append(Item(name, OK, "RAM Tracking: " + ", ".join(n for n, _ in RAM_KEYS) + " are all set"))
        elif len(missing) == len(RAM_KEYS):
            out.append(Item(name, AMBER, "RAM Tracking: none of " + ", ".join(n for n, _ in RAM_KEYS) +
                            " are set, so the vehicle data is DEMO.", "Enter the four RAM Tracking details under Connections."))
        else:
            out.append(Item(name, RED, "RAM Tracking: not set - " + ", ".join(missing) + "; vehicle tracking cannot connect.",
                            "Enter the missing RAM Tracking details under Connections."))
        return out

    # ------------------------------------------------------------------ 4. automations
    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.j.settings.timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    def _fast_out_of_hours(self, cron: str, now: datetime) -> tuple[int, str] | None:
        """(minutes between runs, when) for the first pair of runs less than 30 minutes apart where the later one falls
        outside working hours (Monday-Friday, the suggestions working-hours window, in TIMEZONE); None if there is none."""
        s = self.j.settings
        start_hour, end_hour = int(s.suggestions_fsm_hours_start), int(s.suggestions_fsm_hours_end)
        tz = self._zone()
        trigger = cron_trigger(cron, timezone=tz)
        end = now + CRON_WINDOW
        previous = None
        fire = trigger.get_next_fire_time(None, now.astimezone(tz))
        for _ in range(CRON_MAX_FIRES):
            if fire is None or fire > end:
                break
            local = fire.astimezone(tz)
            in_hours = local.weekday() < 5 and start_hour <= local.hour < end_hour
            if previous is not None and not in_hours and fire - previous < MIN_GAP_OUT_OF_HOURS:
                return int((fire - previous).total_seconds() // 60) or 1, local.strftime("%a %H:%M")
            previous = fire
            fire = trigger.get_next_fire_time(previous, previous)
        return None

    async def _automations(self, now: datetime) -> list[Item]:
        name = "Automations"
        rows = self.j.db.list_automations()
        if not rows:
            return [Item(name, OK, "No automations set up.")]
        out: list[Item] = []
        for a in rows:
            label = f"#{a['id']} {_clip(a['description'], 50)}"
            try:
                out.append(self._one_automation(a, label, now))
            except Exception as e:  # noqa: BLE001 - one odd automation must not hide the rest
                out.append(Item(name, AMBER, f"{label}: could not check: {self._reason(e)}",
                                "Check its schedule with the automations list."))
        return out

    def _one_automation(self, a: dict[str, Any], label: str, now: datetime) -> Item:
        name = "Automations"
        last = str(a.get("last_run_at") or "")
        when = f"last run {last[:16].replace('T', ' ')} UTC" if last else "never run yet"
        if not a.get("enabled"):
            return Item(name, OK, f"{label}: switched off, {when}.")
        flags: list[str] = []
        rows = self.j.db.query("SELECT outcome, detail FROM check_runs WHERE job_key = ? ORDER BY id DESC LIMIT ?",
                               (f"automation_{a['id']}", NOTHING_STREAK * 3))
        streak = 0
        for r in rows:
            if r["outcome"] in (NO_CHANGE, BASELINE) and (r["detail"] or "") != SAME_AS_LAST_TIME:
                streak += 1
            else:
                break
        if streak >= NOTHING_STREAK:
            flags.append(f"reported NOTHING_TO_REPORT {streak} times in a row")
        if str(a.get("last_result") or "").startswith("Failed:"):
            flags.append("its last run failed")
        fast = None
        try:
            fast = self._fast_out_of_hours(str(a["cron"]), now)
        except ValueError as e:
            flags.append(f"its schedule does not parse ({_clip(e, 60)})")
        if fast:
            flags.append(f"runs every {fast[0]} min or less outside working hours (e.g. {fast[1]})")
        if not flags:
            return Item(name, OK, f"{label}: schedule '{a['cron']}', {when}.")
        return Item(name, AMBER, f"{label}: {'; '.join(flags)}; {when}.",
                    "Slow its schedule, or delete it if it is no longer useful (ask me to remove the automation).")

    # ------------------------------------------------------------------ 5. agent runs
    async def _agent_runs(self, now: datetime) -> list[Item]:
        name = "Agent runs"
        runs = AgentRuns(self.j.db).recent(50, now=now)
        cutoff = now - STALE_AFTER
        out: list[Item] = []

        def recent(r: dict[str, Any]) -> bool:
            at = _when(r.get("last_activity_at"))
            return at is not None and at >= cutoff

        for r in runs:
            if r["status"] == "stalled":
                out.append(Item(name, RED, f"Run #{r['id']} ({r['kind']}) is stalled: no activity for {r['idle_minutes']} minutes.",
                                "Ask me for agent_runs to see where it stopped; it is closed as interrupted by itself "
                                "after two hours, or start the request again."))
        for status, level, word in (("failed", RED, "failed"), ("gave_up", AMBER, "gave up")):
            hit = [r for r in runs if r["status"] == status and recent(r)]
            if hit:
                ids = ", ".join(f"#{r['id']} ({r['kind']})" for r in hit[:MAX_LISTED])
                newest = _clip(hit[0].get("outcome"), 100)
                out.append(Item(name, level, f"{_plural(len(hit), 'run')} {word} in the last 24 hours: {ids}."
                                + (f" Newest: {newest}" if newest else ""),
                                "Ask me for agent_runs for the trail and reason, then ask again if it is still wanted."))
        return out or [Item(name, OK, f"No run is stalled, and none failed or gave up in the last 24 hours "
                                      f"({_plural(len(runs), 'recent run')} on record).")]

    # ------------------------------------------------------------------ 6. open requests
    async def _requests(self, now: datetime) -> list[Item]:
        name = "Open requests"
        cutoff = now - STALE_AFTER
        out: list[Item] = []
        stale_issues = [i for i in self.j.db.list_issues("open", limit=500)
                        if (at := _when(i.get("updated_at"))) is not None and at < cutoff]
        if stale_issues:
            listed = ", ".join(f"#{i['id']} {_clip(i['title'], 40)}" for i in stale_issues[:MAX_LISTED])
            more = f" and {len(stale_issues) - MAX_LISTED} more" if len(stale_issues) > MAX_LISTED else ""
            out.append(Item(name, AMBER, f"{_plural(len(stale_issues), 'open issue')} untouched for over 24 hours: {listed}{more}.",
                            "Open the Issues pop-up: resolve it, or ask me to fix it."))
        stale_actions = [p for p in self.j.db.pending_actions()
                         if (at := _when(p.get("created_at"))) is not None and at < cutoff]
        if stale_actions:
            listed = ", ".join(f"#{p['id']} ({_clip(p['kind'], 30)})" for p in stale_actions[:MAX_LISTED])
            more = f" and {len(stale_actions) - MAX_LISTED} more" if len(stale_actions) > MAX_LISTED else ""
            out.append(Item(name, AMBER, f"{_plural(len(stale_actions), 'approval')} waiting for over 24 hours: {listed}{more}.",
                            "Open the Approvals pop-up and approve, edit or deny them."))
        return out or [Item(name, OK, "No open issue or approval has been left untouched for over 24 hours.")]

    # ------------------------------------------------------------------ 7. pull requests
    async def _pull_requests(self, now: datetime) -> list[Item]:
        name = "Pull requests"
        gh = getattr(self.j, "self_github", None)
        if gh is None:
            return [Item(name, OK, "Jarvis's own repository is not connected, so pull requests were not checked.",
                         "Set JARVIS_REPO and a GitHub token if you want them watched.")]
        data = await PRClient(gh).list_open_prs(30)
        prs = data.get("pull_requests") or []
        cutoff = now - STALE_AFTER
        out: list[Item] = []
        for p in prs:
            at = _when(p.get("updated_at"))
            stale = at is not None and at < cutoff
            red = p.get("ci") == "failure"
            conflicted = p.get("merge_status") == "conflicts"
            if not (red or conflicted):
                continue
            if red and at is not None and not stale and not conflicted:
                continue  # red, but only just: someone is probably still on it
            parts = []
            if red:
                failed = ", ".join(_clip(f, 30) for f in (p.get("ci_failed") or [])[:3])
                parts.append("CI is red" + (f" ({failed})" if failed else "") + (" for over 24 hours" if stale else ""))
            if conflicted:
                parts.append("has merge conflicts" + (" and has not been touched for over 24 hours" if stale else ""))
            unknown = " (age unknown)" if at is None else ""
            step = ("Ask me to update the branch (pr_resolve_conflicts, needs your approval), or close it."
                    if conflicted else "Ask me for ci_log_excerpt on it, or close it if it is abandoned.")
            out.append(Item(name, RED if stale else AMBER, f"PR #{p.get('number')} " + " and ".join(parts) + f"{unknown}.", step))
        if len(out) > 8:
            extra = len(out) - 8
            out = out[:8] + [Item(name, AMBER, f"...and {extra} more pull requests need a look.", "Ask me for pr_list.")]
        return out or [Item(name, OK, f"{_plural(len(prs), 'open pull request')}; none red for over 24 hours or conflicted.")]

    # ------------------------------------------------------------------ 8. routine tests and issues that need a human
    async def _tests_and_issues(self, now: datetime) -> list[Item]:
        name = "Tests and issues"
        out: list[Item] = []
        results = self.j.db.latest_test_results()
        failing = [r for r in results if not r["ok"]]
        if failing:
            listed = ", ".join(f"{_clip(r['suite'], 20)}/{_clip(r['name'], 40)}" for r in failing[:MAX_LISTED])
            more = f" and {len(failing) - MAX_LISTED} more" if len(failing) > MAX_LISTED else ""
            out.append(Item(name, RED, f"{_plural(len(failing), 'routine test')} failing: {listed}{more}.",
                            "Open the Health pop-up (or ask for routine_tests_status) for the detail."))
        elif not results:
            out.append(Item(name, AMBER, "The routine tests have no recorded results yet.", "Ask me to run the routine tests."))
        else:
            out.append(Item(name, OK, f"All {_plural(len(results), 'routine test')} passed on their latest run."))
        needs = self.j.db.list_issues("needs_human", limit=500)
        if needs:
            out.append(Item(name, AMBER, f"{_plural(len(needs), 'open issue')} need{'s' if len(needs) == 1 else ''} a human.",
                            "Open the Issues pop-up and take a look."))
        else:
            out.append(Item(name, OK, "No open issue needs a human."))
        return out
