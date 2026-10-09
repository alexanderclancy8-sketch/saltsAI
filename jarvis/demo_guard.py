"""Demo data is never something Jarvis reasons from - and in production there is none.

Sample data is a switch (``Settings.sample_data`` / ``JARVIS_SAMPLE_DATA``, see ``config.default_sample_data``). It is
OFF in production (Azure) and wherever a .env is used: a source that isn't connected is then simply "not connected" -
its stand-in (``NoFSM``, ``NoMail``, ``NoFinance``, ``NoRamTracking``, the empty stock ledger, no staff register, no social
figures) serves nothing, calls ``touch(source, sample=False)`` where it would have served data, and a tool that needed it
returns ``refusal(..., sample=False)``: a plain ``not_connected`` result naming what to connect in Settings -> Connections,
with no "demo" or "sample" wording. ``not_connected_now(j)`` lists those sources; the console's pop-ups show a tidy
"Not connected yet" state from ``panel_messages(j)``.

With sample data ON (a bare local run, the test suite), where a source is not connected yet Jarvis shows believable sample data (DemoFinance, the example staff register,
the seeded stock, sample social followers, DemoRamTracking...) so the console is usable on day one. That is fine for
the console, which labels it "demo", but it must never reach the model as if it were the company's own figures: the
owner would hear "Kestrel Retail owes four thousand pounds" about a customer that only exists in the sample.

How it holds regardless of what the model does:

* Each demo data source calls ``touch("<source>")`` where it actually serves sample figures. Inside a tool call that
  stops the tool on the spot (``DemoDataBlocked``), before it can build an answer, draft or queued action from them.
* ``tools.dispatch()`` runs every read tool inside ``begin()`` / ``end()``. If anything under that tool touched a demo
  source, the tool's own result is thrown away and ``refusal()`` is returned instead: no sample name or figure is in
  it, just which sources are not connected and what to connect.
* A composite tool (the morning briefing, the wrap-up, the suggestions sweep, the business advisor) wraps each source
  in ``section()``: a demo section is replaced by a "not connected" stub and the rest of the answer still stands.

Outside a tool call (the console's own pop-ups, the scheduler) nothing is collected and nothing changes, so the visible
demo data and its "demo" labels stay exactly as they were.

Only a *read* is ever refused. Anything that changes something is queued for approval before any of this runs.

With sample data OFF the same machinery carries "not connected": nothing is sample, so ``demo_now()`` is always empty and
the safety net above never fires in production; it stays for any stand-in that might still serve sample data.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from typing import Any, Awaitable, Iterator

# The sources a tool can read sample data from while that source is not connected.
ACCOUNTS = "accounts"
SOCIALS = "socials"
STOCK = "stock"
STAFF = "staff"
VEHICLES = "vehicles"
# Only ever "not connected" (sample data off): with sample data on, the demo FSM and demo mailbox are every test's stand-in
# and are not gated (the prompt rule still covers them).
FSM = "fsm"
MAIL = "mail"

WHERE = "Settings → Connections"


@dataclass(frozen=True)
class Source:
    key: str
    label: str        # how Jarvis names it to the owner
    connect: str      # what needs connecting, in words he can say


SOURCES: dict[str, Source] = {s.key: s for s in (
    Source(ACCOUNTS, "the accounts (Sage)",
           "Connect Sage under Connections (client ID and secret, then Connect Sage), or add your Sage CSV exports"),
    Source(SOCIALS, "the social media and Google review figures",
           "Connect at least one of Facebook, Instagram, LinkedIn, TikTok or Google reviews under Connections"),
    Source(STOCK, "the stock records",
           "Connect Salts FSM under Connections so stock syncs from it, or clear the sample stock and enter your real items"),
    Source(STAFF, "the staff register",
           "Tell me each person's role, duties and targets (I will ask you to approve each one), or add the register file"),
    Source(VEHICLES, "RAM Tracking",
           "Enter the RAM Tracking client ID, client secret, API username and API password under Connections"),
    Source(FSM, "Salts FSM", "Enter the Salts FSM web address and API key under Connections"),
    Source(MAIL, "Outlook (Microsoft 365)", "Connect Microsoft 365 under Connections"),
)}
# The console's own words for each source in its empty state ("connect Sage in Settings -> Connections").
SHORT = {ACCOUNTS: "Sage", SOCIALS: "Facebook, Instagram, LinkedIn, TikTok or Google reviews", STOCK: "Salts FSM",
         STAFF: "the staff register", VEHICLES: "RAM Tracking", FSM: "Salts FSM", MAIL: "Microsoft 365"}

# None outside a tool call. Inside one, the sources touched so far -> True when one served (or would have served) SAMPLE data,
# False when it is simply not connected (sample data off).
_seen: contextvars.ContextVar[dict[str, bool] | None] = contextvars.ContextVar("jarvis_demo_seen", default=None)


class DemoDataBlocked(BaseException):
    """Raised by ``touch()`` inside a tool call. A BaseException on purpose: the many ``except Exception`` fallbacks in
    the services ("one broken source must not spoil the briefing") must not be able to swallow it and carry on with
    sample data. Only ``dispatch()`` and ``section()`` catch it."""

    def __init__(self, source: str, sample: bool = True) -> None:
        super().__init__(source)
        self.source = source
        self.sample = sample


def touch(source: str, sample: bool = True) -> None:
    """A demo source is serving sample figures right now (``sample=False``: a source that is not connected and has nothing
    to serve). A no-op unless a tool call is collecting; inside one it records the source and stops whatever was reading it."""
    seen = _seen.get()
    if seen is not None:
        seen[source] = seen.get(source, False) or sample
        raise DemoDataBlocked(source, sample)


def begin() -> contextvars.Token:
    return _seen.set({})


def finish(token: contextvars.Token) -> tuple[list[str], bool]:
    """Stop collecting: (the sources touched since ``begin()`` in a stable order, whether any of them was SAMPLE data)."""
    seen = _seen.get() or {}
    _seen.reset(token)
    return [k for k in SOURCES if k in seen], any(seen.values())


def end(token: contextvars.Token) -> list[str]:
    """Stop collecting; the demo sources touched since ``begin()``, in a stable order."""
    return finish(token)[0]


def active() -> bool:
    return _seen.get() is not None


def names(keys: list[str] | set[str]) -> str:
    labels = [SOURCES[k].label for k in SOURCES if k in keys]
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def needs(keys: list[str] | set[str], sample: bool = True) -> str:
    return " ".join((SOURCES[k].connect if sample else CONNECT_OFF.get(k, SOURCES[k].connect)) + "."
                    for k in SOURCES if k in keys)


# What to connect when sample data is off, where the usual wording mentions the sample data that isn't there.
CONNECT_OFF = {STOCK: "Connect Salts FSM under Connections so stock syncs from it, or enter your stock items"}


def refusal(tool: str, keys: list[str], owner: str = "the owner", sample: bool = True) -> dict[str, Any]:
    """What the model gets instead of a tool result that was built on sample data. Carries no sample name or figure.
    ``sample=False`` (sample data off): the plain ``not_connected`` result - which source, what to connect and where."""
    if not sample:
        return not_connected_result(tool, keys, owner)
    return {
        "demo_data_withheld": True,
        "tool": tool,
        "not_connected": [{"source": SOURCES[k].label, "to_connect": SOURCES[k].connect} for k in keys],
        "instruction": (
            f"This answer could only have come from sample data, because {names(keys)} "
            f"{'is' if len(keys) == 1 else 'are'} not connected yet, so it has been withheld. Do not quote, estimate "
            f"or paraphrase any name or figure from it, and do not answer from earlier sample figures either. Tell "
            f"{owner} plainly, in a sentence or two, that you can't answer this yet and what needs connecting: "
            f"{needs(keys)} Offer anything else you can do from real data."),
    }


def not_connected_result(tool: str, keys: list[str], owner: str = "the owner") -> dict[str, Any]:
    """A tool's answer when what it needs isn't connected (sample data off): no figures, no "demo" wording."""
    keys = [k for k in SOURCES if k in keys] or [FSM]
    one = len(keys) == 1
    plural = one and SOURCES[keys[0]].label.endswith(("records", "figures"))
    return {
        "connected": False,
        "tool": tool,
        "not_connected": [{"source": SOURCES[k].label, "to_connect": CONNECT_OFF.get(k, SOURCES[k].connect)} for k in keys],
        "where": WHERE,
        "instruction": (
            f"{_cap(names(keys))} {'is' if one and not plural else 'are'} not connected yet, so there is nothing to answer "
            f"this from. Tell {owner} in a sentence that you can't answer it until "
            f"{'it is' if one and not plural else 'they are'} connected, and what to connect ({WHERE}): "
            f"{needs(keys, sample=False)} Don't guess or estimate, and don't repeat this in later replies "
            f"unless asked. Offer anything else you can do."),
    }


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def is_not_connected(result: Any) -> bool:
    """A tool result (or a composite's section) that says its source isn't connected (sample data off)."""
    return isinstance(result, dict) and result.get("connected") is False and isinstance(result.get("not_connected"), list)


def stub(keys: list[str], sample: bool = True) -> dict[str, Any]:
    """Stands in for one section of a composite answer whose data was sample data. Shaped like a failed source (an
    ``error`` key) so every consumer already treats it as 'nothing usable here'. ``sample=False``: the source is simply
    not connected (sample data off) - said in those words."""
    if not sample:
        return {"error": f"Not connected: {names(keys)}.", "not_connected": [SOURCES[k].label for k in keys],
                "connected": False}
    return {"error": f"Not connected: {names(keys)} would only be sample data, so nothing from it is used. "
                     f"Do not mention any name or figure for it; say once that it can't be covered yet and what to "
                     f"connect: {needs(keys)}",
            "not_connected": [SOURCES[k].label for k in keys]}


async def section(coro: Awaitable[Any]) -> Any:
    """Await one source inside a composite answer. Inside a tool call, a source that turns out to be sample data is
    replaced by ``stub()`` and does not make the whole tool refuse; anywhere else this is just ``await coro``."""
    if not active():
        return await coro
    token = begin()
    try:
        data = await coro
    except DemoDataBlocked:
        data = None
    finally:
        touched, sampled = finish(token)
    return stub(touched, sample=sampled) if touched else data


@contextlib.contextmanager
def suspended() -> Iterator[None]:
    """Run something that is *meant* to work on whatever is there (the same suggestions sweep the scheduler runs),
    without it being stopped by sample data. What it produces is filtered afterwards, before the model sees it."""
    token = _seen.set(None)
    try:
        yield
    finally:
        _seen.reset(token)


def sample_on(j_or_settings: Any) -> bool:
    """Whether sample data is switched on for this Jarvis (or these settings). Anything without the setting (a test's fake
    settings) counts as on, which is how every stand-in behaved before the switch existed."""
    s = getattr(j_or_settings, "settings", j_or_settings)
    return bool(getattr(s, "sample_data", True))


def stand_ins(j: Any) -> set[str]:
    """Every source served by a stand-in right now (sample data or nothing at all): FSM, mailbox, accounts, socials, stock,
    staff register and vehicles that aren't connected. Defensive: anything unexpected means "connected"."""
    checks = {
        FSM: lambda: bool(j.fsm.demo),
        MAIL: lambda: bool(j.mail.demo),
        ACCOUNTS: lambda: bool(getattr(j.finance, "demo", False)),
        SOCIALS: lambda: not j.marketing.connected,
        STOCK: lambda: bool(j.stores.demo) or (bool(j.fsm.demo) and not j.stores.has_items()),
        STAFF: lambda: not j.register.path.exists(),
        VEHICLES: lambda: bool(j.ram.demo),
    }
    out: set[str] = set()
    for key, check in checks.items():
        try:
            if check():
                out.add(key)
        except Exception:  # noqa: BLE001
            continue
    return out


def not_connected_now(j: Any) -> set[str]:
    """With sample data OFF: the sources that are not connected (each one serves nothing). With it on: empty - they show
    sample data instead (``demo_now``)."""
    return set() if sample_on(j) else stand_ins(j)


def not_connected_sources(j: Any) -> list[dict[str, str]]:
    """The console's "Not connected" list: each unconnected source once, with what to connect (sample data off only)."""
    nc = not_connected_now(j)
    return [{"key": k, "name": _cap(SOURCES[k].label), "how": CONNECT_OFF.get(k, SOURCES[k].connect) + "."}
            for k in SOURCES if k in nc]


def panel_message(key: str) -> str:
    """A pop-up's empty state for a source that isn't connected."""
    if key == STAFF:
        return "Not set up yet - tell Jarvis each person's role, duties and targets"
    return f"Not connected yet - connect {SHORT.get(key, SOURCES[key].label)} in {WHERE}"


# Which source each console pop-up section rests on.
PANELS = {"inbox": MAIL, "staff": FSM, "overdue_jobs": FSM, "finance": ACCOUNTS, "presence": SOCIALS, "fleet": VEHICLES,
          "customer_watch": FSM}


def panel_messages(j: Any) -> dict[str, str]:
    """{section: "Not connected yet - ..."} for every console section whose source isn't connected (sample data off; {}
    with it on, where the pop-ups keep their sample data and "demo" labels)."""
    nc = not_connected_now(j)
    return {panel: panel_message(key) for panel, key in PANELS.items() if key in nc}


def not_connected_line(keys: set[str] | list[str]) -> str:
    """ONE short line for a briefing: "Not connected yet: Sage and RAM Tracking (Settings -> Connections)." ('' when none)."""
    labels = list(dict.fromkeys(SHORT.get(k, SOURCES[k].label) for k in SOURCES if k in keys))
    if not labels:
        return ""
    text = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    return f"Not connected yet: {text} ({WHERE})."


def leave_out(j: Any, data: dict[str, Any], parts: dict[str, tuple[str, ...]]) -> list[str]:
    """Sample data off: drop every part of a composite answer (briefing, wrap-up, advice) whose source isn't connected -
    it would only be empty - and every "not connected" stub a section left. Returns the source keys left out, so a
    briefing can say them in ONE line. With sample data on nothing is touched (stubs and demo labels stay, as before)."""
    if sample_on(j):
        return []
    nc = not_connected_now(j)
    out: set[str] = set()
    for name, keys in parts.items():
        hit = [k for k in keys if k in nc]
        if name in data and hit:
            data.pop(name)
            out.update(hit)
    labels = {s.label: k for k, s in SOURCES.items()}
    for name in [k for k, v in data.items() if isinstance(v, dict) and v.get("connected") is False and "error" in v]:
        out.update(labels[x] for x in data.pop(name).get("not_connected") or [] if x in labels)
    return [k for k in SOURCES if k in out]


# The trace's source names (brain/trace.py) -> the source keys here.
TRACE_SOURCES = {"Salts FSM": FSM, "Outlook": MAIL, "Sage": ACCOUNTS, "Stock records": STOCK, "RAM Tracking": VEHICLES}


def says_demo(result: Any) -> bool:
    """A tool result that flags itself as demo / sample (``"demo": true`` or an error of kind "demo")."""
    return isinstance(result, dict) and (result.get("demo") is True or result.get("kind") == "demo"
                                         or bool(result.get("demo_data_withheld")))


def keys_for_tool(j: Any, tool: str) -> list[str]:
    """Which unconnected sources a tool rests on (for its not_connected answer): the tool's own sources that aren't
    connected, else Salts FSM if that isn't, else everything that isn't."""
    nc = not_connected_now(j) or stand_ins(j)
    try:
        from .brain.trace import tool_info

        mine = {TRACE_SOURCES[n] for n in tool_info(tool)[0] if n in TRACE_SOURCES}
    except Exception:  # noqa: BLE001
        mine = set()
    hit = [k for k in SOURCES if k in mine and k in nc]
    if hit:
        return hit
    if FSM in nc:
        return [FSM]
    return [k for k in SOURCES if k in nc] or [FSM]


def demo_now(j: Any) -> set[str]:
    """The sources that are showing sample data right now. Each check is defensive: unexpected means "not demo".
    Always empty with sample data off: nothing is sample then; an unconnected source is "not connected" (``not_connected_now``)."""
    if not sample_on(j):
        return set()
    checks = {
        ACCOUNTS: lambda: bool(getattr(j.finance, "demo", False)),
        SOCIALS: lambda: bool(j.marketing.demo),
        STOCK: lambda: bool(j.stores.demo),
        STAFF: lambda: bool(j.register.demo),
        VEHICLES: lambda: bool(j.ram.demo),
    }
    out: set[str] = set()
    for key, check in checks.items():
        try:
            if check():
                out.add(key)
        except Exception:  # noqa: BLE001
            continue
    return out


# A stored suggestion is built from whatever the sweep could see, which includes sample data. Which source each kind
# rests on (the key prefix in services/suggestions.py) - the rest come from Salts FSM, email or the routine tests.
SUGGESTION_SOURCES = {"customer": ACCOUNTS, "renewal": ACCOUNTS, "concentration": ACCOUNTS, "credit": ACCOUNTS,
                      "payrisk": ACCOUNTS, "reorder": STOCK}


def suggestions_rest_on_sample_data(j: Any) -> bool:
    """True while a source the suggestions are built from (the accounts, the stock) is still sample data."""
    return bool(demo_now(j) & set(SUGGESTION_SOURCES.values()))


def visible_suggestions(j: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The stored suggestions minus any that rest on a source that is still sample data."""
    demo = demo_now(j)
    return [r for r in rows if SUGGESTION_SOURCES.get(str(r.get("key", "")).split(":")[0]) not in demo]

