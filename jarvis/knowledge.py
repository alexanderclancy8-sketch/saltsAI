"""Company knowledge base: markdown files under knowledge/ (including the git-ignored
knowledge/private/ folder for confidential notes), split into sections and searched
with a small BM25 ranker. Company + FSM docs are also placed directly in Jarvis'
system prompt; the rest is retrieved on demand."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_STOP = set("a an and are as at be by for from has have how i in is it its of on or that the this to was what when "
            "where which who why will with you your we our do does can".split())


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOP]


@dataclass
class Chunk:
    doc: str
    heading: str
    text: str
    tokens: list[str]


class KnowledgeBase:
    ALWAYS_LOADED = ("company", "fsm")  # sub-folders injected into the system prompt

    def __init__(self, root: Path):
        self.root = root
        self.chunks: list[Chunk] = []
        self.docs: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self.chunks.clear()
        self.docs.clear()
        if not self.root.exists():
            return
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root).as_posix()
            if rel.endswith("README.md"):
                continue
            text = path.read_text(encoding="utf-8")
            self.docs[rel] = text
            heading, buf = rel, []
            for line in text.splitlines():
                if line.startswith("## ") and buf:
                    self._add(rel, heading, "\n".join(buf))
                    heading, buf = line[3:].strip(), []
                buf.append(line)
            if buf:
                self._add(rel, heading, "\n".join(buf))
        self._df = Counter(t for c in self.chunks for t in set(c.tokens))
        self._avg = sum(len(c.tokens) for c in self.chunks) / max(len(self.chunks), 1)

    def _add(self, doc: str, heading: str, text: str) -> None:
        if text.strip():
            self.chunks.append(Chunk(doc, heading, text.strip(), _tokens(heading + " " + text)))

    def search(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        q = _tokens(query)
        if not q or not self.chunks:
            return []
        n = len(self.chunks)
        scored = []
        for c in self.chunks:
            tf = Counter(c.tokens)
            score = 0.0
            for t in q:
                if t not in tf:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                score += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(c.tokens) / self._avg))
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"doc": c.doc, "section": c.heading, "text": c.text[:4000]} for _, c in scored[:limit]]

    def core_documents(self) -> str:
        parts = []
        for rel, text in self.docs.items():
            top = rel.split("/")[0]
            if top in self.ALWAYS_LOADED or rel.startswith("private/"):
                parts.append(f"<document path=\"{rel}\">\n{text}\n</document>")
        return "\n\n".join(parts)

    def index(self) -> list[str]:
        return sorted(self.docs)
