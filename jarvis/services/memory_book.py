"""What Jarvis has learned about the business, for the console's Memory pop-up: list, edit, delete.

Three stores feed it, all existing ones (nothing new is stored):

* "Things Jarvis should know" - the `jarvis_notes` setting. Jarvis copies each line into its memory table when it
  starts (`Jarvis._seed_notes`), so a fact that came from there lives in BOTH places. Editing or deleting one here
  therefore also rewrites the setting, or the next start/reload would quietly put the old wording back.
* Things Jarvis has learned - the rest of the `memory` table (the `remember` tool and the nightly self-reflection).
* Learned replies - the `reply_habits` table behind the grey suggestion in the message box.

This is the owner's own console path, called only from main.py's authenticated, same-origin endpoints. It is
deliberately NOT a brain tool: nothing the model says can reach these methods (the model can already `remember` and
`forget` through its existing tools; it gets no way to reword or clear things through this one).

After any change the system prompt is rebuilt (`brain.refresh_system()`), which re-reads the memory table, so a
deleted or reworded fact is what Jarvis reads from his very next turn.
"""

from __future__ import annotations

import re
from typing import Any, Callable

MIN_FACT, MAX_FACT = 3, 1000      # the same bounds as the `remember` tool
_CONTROL = re.compile("[" + "".join(re.escape(chr(a)) + "-" + re.escape(chr(b)) for a, b in (
    (0, 8), (11, 31), (127, 159), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0xFEFF, 0xFEFF))) + "]")


class MemoryEditError(Exception):
    """A memory edit/delete that can't be done. `status` is the HTTP status the endpoint answers with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class MemoryBook:
    def __init__(self, j, save_notes: Callable[[list[str]], None]):
        """`save_notes(lines)` persists the "Things Jarvis should know" setting (main.py passes the SettingsStore)."""
        self.j = j
        self._save_notes = save_notes

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.split()).lower().rstrip(".").strip()

    def _note_lines(self) -> list[str]:
        return [n.strip() for n in str(self.j.settings.jarvis_notes or "").split("|") if n.strip()]

    def _in_notes(self, fact: str) -> bool:
        key = self._key(fact)
        return any(self._key(n) == key for n in self._note_lines())

    def _refresh(self) -> None:
        refresh = getattr(self.j.brain, "refresh_system", None)
        if refresh:
            refresh()

    # ------------------------------------------------------------------ list
    def listing(self) -> dict[str, list[dict[str, Any]]]:
        facts = []
        for m in self.j.db.memories():
            facts.append({"id": m["id"], "text": m["fact"], "added": m["created_at"],
                          "source": "notes" if self._in_notes(m["fact"]) else "learned"})
        replies = [{"id": r["id"], "text": r["display"], "context": r["context"], "uses": r["uses"],
                    "last_used": r["last_used"]} for r in self.j.reply_suggestions.rows()]
        return {"notes": [f for f in facts if f["source"] == "notes"],
                "learned": [f for f in facts if f["source"] == "learned"], "replies": replies}

    # ------------------------------------------------------------------ facts
    def _fact(self, fact_id: int) -> dict[str, Any]:
        row = self.j.db.get_memory(fact_id)
        if not row:
            raise MemoryEditError("That memory no longer exists.", 404)
        return row

    def edit_fact(self, fact_id: int, text: Any) -> dict[str, Any]:
        row = self._fact(fact_id)
        if not isinstance(text, str):
            raise MemoryEditError("The wording must be text.")
        text = " ".join(text.split())
        if len(text) < MIN_FACT:
            raise MemoryEditError("That is too short to be worth remembering.")
        if len(text) > MAX_FACT:
            raise MemoryEditError(f"Keep it under {MAX_FACT} characters.")
        if _CONTROL.search(text):
            raise MemoryEditError("That contains characters that can't be stored.")
        other = self.j.db.find_memory(text)
        if other is not None and other != fact_id:
            raise MemoryEditError("Jarvis already knows that.", 409)
        if self._in_notes(row["fact"]):
            if "|" in text:
                raise MemoryEditError("Please leave out the | character here - it separates the notes in Settings.")
            old = self._key(row["fact"])
            self._save_notes([text if self._key(n) == old else n for n in self._note_lines()])
        self.j.db.update_memory(fact_id, text)
        self._refresh()
        return {"id": fact_id, "text": text}

    def delete_fact(self, fact_id: int) -> None:
        row = self._fact(fact_id)
        if self._in_notes(row["fact"]):
            old = self._key(row["fact"])
            self._save_notes([n for n in self._note_lines() if self._key(n) != old])
        self.j.db.forget(fact_id)
        self._refresh()

    # ------------------------------------------------------------------ learned replies
    def edit_reply(self, reply_id: int, text: Any) -> dict[str, Any]:
        if not isinstance(text, str):
            raise MemoryEditError("The reply must be text.")
        try:
            return self.j.reply_suggestions.edit(reply_id, text)
        except ValueError as e:
            raise MemoryEditError(str(e), 404 if "no longer" in str(e) else 422) from None

    def delete_reply(self, reply_id: int) -> None:
        if not self.j.reply_suggestions.delete(reply_id):
            raise MemoryEditError("That learned reply no longer exists.", 404)

