"""Web research on the web tools both brains already have: how much searching one turn may do, and the sources it
really used.

There is no separate research tool. The API brain has Anthropic's server-side ``web_search`` / ``web_fetch`` tools (each
request's ``max_uses``); the Claude Max brain has Claude Code's own ``WebSearch`` / ``WebFetch``. ``WebTurn`` is one turn's
web budget and record on either:

* **Budget.** A question that clearly needs research (``is_research``: a standard, a regulation, "find three suppliers",
  "which panels support X", "compare") gets a bigger budget than an ordinary one, and every turn has a hard cap per kind
  (searches, page reads) whatever the question. The API brain asks for ``tools()`` before each request (a spent kind is
  left out of the request, so the model can't use it again this turn); the Max brain asks ``allow()`` from a PreToolUse
  hook, which denies the call once the cap is reached.
* **Sources.** Built only from what the tools really returned - never from text the model wrote. API: the citations the
  API attaches to the reply (``web_search_result_location``: url and title) and the pages ``web_fetch`` read. Max: the pages
  ``WebFetch`` read and, when it read none, the links ``WebSearch`` returned (Claude Code gives no citations). Only http(s)
  addresses are kept; titles are page text, so the console escapes them and nothing here ever feeds them back to a model.

Web content is untrusted data, never instructions (the system prompt's Security section; ``async_tools.is_untrusted_output``
already treats every ``web_`` tool so). Nothing here sends, writes or approves anything.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

# A question that clearly needs several look-ups. Deliberately narrow: anything else keeps the ordinary budget.
RESEARCH = re.compile(
    r"\b(?:research|look into|dig into|cross-?check|sources?|references?|cite|citations?|compare|comparison|versus|vs\.?|"
    r"pros and cons|alternatives?|latest|what(?:'s| has| have)? changed?|what does .{1,60}\bchange|"
    r"bs ?(?:en ?)?\d{3,5}|en ?54|pd ?6662|lps ?\d{3,4}|regulations?|legislation|guidance|standards?|"
    r"find (?:me |us )?(?:\w+ ){0,3}(?:suppliers?|manufacturers?|distributors?|wholesalers?|companies|firms|installers?|"
    r"products?|options)|which \w+(?: \w+){0,3} (?:supports?|are compatible|is compatible|works? with))\b", re.I)

# Per request (the API's max_uses): what an ordinary question has always had, and a research question's.
ORDINARY_REQUEST = {"web_search": 5, "web_fetch": 5}
RESEARCH_REQUEST = {"web_search": 10, "web_fetch": 8}
# Per turn, whatever happens: the hard cap (both brains).
ORDINARY_TURN = {"web_search": 8, "web_fetch": 6}
RESEARCH_TURN = {"web_search": 15, "web_fetch": 12}
MAX_SOURCES = 10
TITLE_CHARS = 120

SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search",
               "user_location": {"type": "approximate", "city": "Bradford", "region": "England", "country": "GB",
                                 "timezone": "Europe/London"}}
FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}
SDK_NAMES = {"WebSearch": "web_search", "WebFetch": "web_fetch"}   # Claude Code's tool -> the kind counted here
KIND_ORDER = {"cited": 0, "read": 1, "found": 2}


def is_research(text: Any) -> bool:
    return bool(RESEARCH.search(str(text or "")))


def server_tools(research: bool = False, used: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """The web tool definitions for one API request: the per-request budget, never past what is left of the turn's cap. A kind
    whose cap is spent is left out. With nothing used on an ordinary question this is exactly what every request always sent
    (so the prompt cache is unchanged for ordinary turns)."""
    used = used or {}
    per_request = RESEARCH_REQUEST if research else ORDINARY_REQUEST
    per_turn = RESEARCH_TURN if research else ORDINARY_TURN
    out = []
    for base in (SEARCH_TOOL, FETCH_TOOL):
        name = base["name"]
        left = per_turn[name] - int(used.get(name, 0))
        if left > 0:
            out.append({**base, "max_uses": min(per_request[name], left)})
    return out


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def clean_url(raw: Any) -> str:
    """An http(s) address, or ''. Nothing else (javascript:, data:, file:) ever becomes a link."""
    url = str(raw or "").strip()
    if not url or len(url) > 2000 or any(c in url for c in "\r\n\t <>\"'`"):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    return url


def _key(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), (p.netloc or "").lower(), p.path.rstrip("/"), p.query, ""))


def _title(raw: Any, url: str) -> str:
    text = " ".join("".join(c if c.isprintable() else " " for c in str(raw or "")).split())[:TITLE_CHARS]
    return text or (urlsplit(url).hostname or url)


class WebTurn:
    """One turn's web budget and the sources its web tools really used."""

    def __init__(self, research: bool = False):
        self.research = bool(research)
        self.used = {"web_search": 0, "web_fetch": 0}
        self.errors = 0
        self._sources: dict[str, dict[str, str]] = {}   # url key -> {title, url, kind}, in order of first appearance
        self._cited: list[str] = []                     # url keys in the order the reply first cites them

    @classmethod
    def for_question(cls, text: Any) -> "WebTurn":
        return cls(is_research(text))

    # ------------------------------------------------------------------ budget
    def tools(self) -> list[dict[str, Any]]:
        """API brain: the web tools for the next request of this turn."""
        return server_tools(self.research, self.used)

    def cap(self, kind: str) -> int:
        return (RESEARCH_TURN if self.research else ORDINARY_TURN)[kind]

    def allow(self, sdk_tool: str) -> str | None:
        """Max brain (PreToolUse): None to let the call run (and count it), or why it is refused."""
        kind = SDK_NAMES.get(str(sdk_tool or ""))
        if kind is None:
            return None
        if self.used[kind] >= self.cap(kind):
            what = "searches" if kind == "web_search" else "page reads"
            return (f"the web budget for this question is used up ({self.cap(kind)} {what}). Answer from what you already "
                    "have, cite it, and say plainly what you couldn't confirm.")
        self.used[kind] += 1
        return None

    # ------------------------------------------------------------------ sources
    def _add(self, url: Any, title: Any, kind: str) -> None:
        url = clean_url(url)
        if not url:
            return
        key = _key(url)
        if kind == "cited" and key not in self._cited:
            self._cited.append(key)
        have = self._sources.get(key)
        if have is None:
            self._sources[key] = {"title": _title(title, url), "url": url, "kind": kind}
        elif KIND_ORDER[kind] < KIND_ORDER[have["kind"]]:
            have["kind"] = kind   # a page that was read and is also cited is cited
            if title and have["title"] == (urlsplit(have["url"]).hostname or have["url"]):
                have["title"] = _title(title, url)

    def note_response(self, content: Any) -> None:
        """API brain: one response's content blocks - count the web tool uses, keep the pages read and the citations. Never
        raises: keeping a record must not be able to break the turn."""
        try:
            self._note_response(content)
        except Exception:  # noqa: BLE001
            log.exception("Could not read the web sources of a response")

    def _note_response(self, content: Any) -> None:
        for block in content or []:
            btype = _get(block, "type")
            if btype == "server_tool_use" and _get(block, "name") in self.used:
                self.used[_get(block, "name")] += 1
            elif btype == "web_fetch_tool_result":
                result = _get(block, "content")
                if _get(result, "type") == "web_fetch_result":
                    self._add(_get(result, "url"), _get(_get(result, "content"), "title"), "read")
                else:
                    self.errors += 1
            elif btype == "web_search_tool_result":
                result = _get(block, "content")
                if not isinstance(result, list):   # an error object instead of a list of results
                    self.errors += 1
            elif btype == "text":
                for cite in _get(block, "citations") or []:
                    if _get(cite, "type") == "web_search_result_location":
                        self._add(_get(cite, "url"), _get(cite, "title"), "cited")

    def note_sdk_result(self, sdk_tool: Any, tool_input: Any, tool_response: Any) -> None:
        """Max brain (PostToolUse): a finished WebFetch is a page read; a WebSearch's links are kept as search results. Never
        raises."""
        try:
            self._note_sdk_result(sdk_tool, tool_input, tool_response)
        except Exception:  # noqa: BLE001
            log.exception("Could not read the web sources of a Claude Code web call")

    def _note_sdk_result(self, sdk_tool: Any, tool_input: Any, tool_response: Any) -> None:
        name = str(sdk_tool or "")
        response = tool_response
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                pass
        if name == "WebFetch":
            code = _get(response, "code") if isinstance(response, dict) else None
            if isinstance(code, int) and code >= 400:
                self.errors += 1
                return
            url = (_get(response, "url") if isinstance(response, dict) else None) or _get(tool_input or {}, "url")
            self._add(url, None, "read")
        elif name == "WebSearch":
            for url, title in _links(response):
                self._add(url, title, "found")

    def sources(self) -> list[dict[str, Any]]:
        """Numbered: cited first (in the order the reply cites them), then pages read, then (Max only, when nothing was read)
        search results."""
        cited = [self._sources[k] for k in self._cited]
        items = cited + sorted((s for k, s in self._sources.items() if k not in self._cited), key=lambda s: KIND_ORDER[s["kind"]])
        if any(s["kind"] != "found" for s in items):
            items = [s for s in items if s["kind"] != "found"]
        return [{"n": i, **s} for i, s in enumerate(items[:MAX_SOURCES], start=1)]

    @property
    def searched(self) -> int:
        return self.used["web_search"] + self.used["web_fetch"]

    def summary(self) -> dict[str, Any]:
        return {"sources": self.sources(), "searches": self.used["web_search"], "reads": self.used["web_fetch"],
                "errors": self.errors}


def _links(value: Any, depth: int = 0) -> list[tuple[str, Any]]:
    """(url, title) pairs anywhere in a WebSearch result (Claude Code returns {results: [{content: [{title, url}]}]})."""
    found: list[tuple[str, Any]] = []
    if depth > 4:
        return found
    if isinstance(value, dict):
        if isinstance(value.get("url"), str):
            found.append((value["url"], value.get("title")))
        for v in value.values():
            if isinstance(v, (dict, list)):
                found += _links(v, depth + 1)
    elif isinstance(value, list):
        for v in value[:50]:
            found += _links(v, depth + 1)
    return found
