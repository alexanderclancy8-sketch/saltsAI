"""Demo data is never something Jarvis reasons from.

Where a source is not connected yet, Jarvis shows believable sample data (DemoFinance, the example staff register,
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
)}

# None outside a tool call. Inside one, the set of demo sources touched so far.
_seen: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar("jarvis_demo_seen", default=None)


class DemoDataBlocked(BaseException):
    """Raised by ``touch()`` inside a tool call. A BaseException on purpose: the many ``except Exception`` fallbacks in
    the services ("one broken source must not spoil the briefing") must not be able to swallow it and carry on with
    sample data. Only ``dispatch()`` and ``section()`` catch it."""

    def __init__(self, source: str) -> None:
        super().__init__(source)
        self.source = source


def touch(source: str) -> None:
    """A demo source is serving sample figures right now. A no-op unless a tool call is collecting; inside one it
    records the source and stops whatever was reading it."""
    seen = _seen.get()
    if seen is not None:
        seen.add(source)
        raise DemoDataBlocked(source)


def begin() -> contextvars.Token:
    return _seen.set(set())


def end(token: contextvars.Token) -> list[str]:
    """Stop collecting; the demo sources touched since ``begin()``, in a stable order."""
    seen = _seen.get() or set()
    _seen.reset(token)
    return [k for k in SOURCES if k in seen]


def active() -> bool:
    return _seen.get() is not None


def names(keys: list[str] | set[str]) -> str:
    labels = [SOURCES[k].label for k in SOURCES if k in keys]
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def needs(keys: list[str] | set[str]) -> str:
    return " ".join(SOURCES[k].connect + "." for k in SOURCES if k in keys)


def refusal(tool: str, keys: list[str], owner: str = "the owner") -> dict[str, Any]:
    """What the model gets instead of a tool result that was built on sample data. Carries no sample name or figure."""
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


def stub(keys: list[str]) -> dict[str, Any]:
    """Stands in for one section of a composite answer whose data was sample data. Shaped like a failed source (an
    ``error`` key) so every consumer already treats it as 'nothing usable here'."""
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
        touched = end(token)
    return stub(touched) if touched else data


@contextlib.contextmanager
def suspended() -> Iterator[None]:
    """Run something that is *meant* to work on whatever is there (the same suggestions sweep the scheduler runs),
    without it being stopped by sample data. What it produces is filtered afterwards, before the model sees it."""
    token = _seen.set(None)
    try:
        yield
    finally:
        _seen.reset(token)


def demo_now(j: Any) -> set[str]:
    """The sources that are showing sample data right now. Each check is defensive: unexpected means "not demo"."""
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

