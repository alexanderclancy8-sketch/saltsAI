"""One-off clean-up of the sample rows an earlier demo run left in the live database (sample data off only).

Before sample data became a switch (``JARVIS_SAMPLE_DATA``), a source that wasn't connected showed sample data, and two kinds of
it were written to the database: the seeded stock (``Stores._seed_demo``) and the stored suggestions the sweep built from the
sample accounts / FSM / mailbox / stock. With sample data off they must not linger. This runs once when Jarvis starts with sample
data off (recorded in kv ``CLEANUP_KEY``, so it never runs twice) and deletes ONLY rows that are demonstrably sample data:

* Stock - only when kv ``stock_demo_seeded`` is "1" (the seed ran and was never cleared). A seeded item goes only if its row is
  still EXACTLY the seed (sku, name, category, unit cost, reorder level and quantity, supplier) and nothing but seeded movements
  ever touched it; then its levels at the seed locations (Stores and the four sample vans) go with it. The seeded movements are
  matched exactly too (note "demo", an issue from a sample van to a job). An item anyone moved, counted or edited is left, and
  counted as "kept" in the log line. ``stock_demo_seeded`` becomes "cleared" so nothing is ever seeded again.
* Suggestions - stored suggestions are written by Jarvis's own sweep, never by a person. One goes only when it is flagged as
  resting on a source (``RESTS_ON``, the same idea as ``demo_guard.SUGGESTION_SOURCES``) that is NOT connected now, so it could
  only have been built from that source's sample data: the accounts (Sage), Salts FSM, the mailbox, the seeded stock, or the
  example accreditations file. A suggestion with a Prepare handler (``kind`` set, kept by the FSM Action Centre) is never touched.

Nothing else is deleted: not notes, memories, transcripts, approvals, notifications, documents or anything an integration wrote.
Only counts are logged - never a name, figure or key from a row.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import demo_guard

log = logging.getLogger(__name__)

CLEANUP_KEY = "sample_data:cleanup_v1"

# The kind of stored suggestion (its key's prefix, services/suggestions.py) -> the sources it is built from. Gone when ANY of
# them is not connected now (it cannot have been built from real data for that source).
RESTS_ON: dict[str, tuple[str, ...]] = {
    "customer": (demo_guard.FSM, demo_guard.ACCOUNTS), "concentration": (demo_guard.ACCOUNTS,),
    "credit": (demo_guard.ACCOUNTS,), "payrisk": (demo_guard.ACCOUNTS,), "unbilled": (demo_guard.FSM, demo_guard.ACCOUNTS),
    "renewal": (demo_guard.FSM,), "remedials": (demo_guard.FSM,), "assign": (demo_guard.FSM,), "cert": (demo_guard.FSM,),
    "quote-priority": (demo_guard.FSM,), "ooh": (demo_guard.MAIL,), "meeting-actions": (demo_guard.MAIL,),
    "reorder": (demo_guard.STOCK,), "accred": ("accreditations_example",),
}


def _delete(db, table: str, where: str, params: tuple) -> int:
    """DELETE and return how many rows went (counted first: ``Database.execute`` returns a rowid for some statements)."""
    n = db.query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)["n"]
    if n:
        db.execute(f"DELETE FROM {table} WHERE {where}", params)
    return int(n)


def _seed_stock() -> tuple[dict[str, tuple], set[str]]:
    from .stores import DEMO_ITEMS, DEMO_VANS, STORES

    return {row[0]: row for row in DEMO_ITEMS}, {STORES, *DEMO_VANS}


def _remove_seeded_stock(db) -> dict[str, int]:
    from .stores import DEMO_VANS

    items, locations = _seed_stock()
    vans = tuple(DEMO_VANS)
    marks = ",".join("?" * len(vans))
    out = {"stock_items": 0, "stock_levels": 0, "stock_moves": 0, "stock_items_kept": 0}
    for sku, (_, name, category, cost, level, qty, supplier) in items.items():
        out["stock_moves"] += _delete(db, "stock_moves", f"sku = ? AND note = 'demo' AND kind = 'issue' AND to_loc = 'job' "
                                                         f"AND from_loc IN ({marks})", (sku, *vans))
        row = db.query_one("SELECT * FROM stock_items WHERE sku = ?", (sku,))
        if row is None:
            continue
        exact = (row["name"] == name and row["category"] == category and float(row["unit_cost"]) == float(cost)
                 and float(row["reorder_level"]) == float(level) and float(row["reorder_qty"]) == float(qty)
                 and row["supplier"] == supplier and (row.get("unit") or "each") == "each" and not (row.get("notes") or ""))
        touched = db.query_one("SELECT COUNT(*) AS n FROM stock_moves WHERE sku = ?", (sku,))["n"]
        elsewhere = [r["location"] for r in db.query("SELECT location FROM stock_levels WHERE sku = ?", (sku,))
                     if r["location"] not in locations]
        if not exact or touched or elsewhere:
            out["stock_items_kept"] += 1   # someone used, counted or edited it: not demonstrably sample any more
            continue
        lmarks = ",".join("?" * len(locations))
        out["stock_levels"] += _delete(db, "stock_levels", f"sku = ? AND location IN ({lmarks})", (sku, *sorted(locations)))
        out["stock_items"] += _delete(db, "stock_items", "sku = ?", (sku,))
    return out


def _remove_sample_suggestions(db, unconnected: set[str]) -> int:
    gone = 0
    for row in db.query("SELECT key, kind FROM suggestions"):
        if row.get("kind"):
            continue  # a Prepare-button suggestion: kept true by fsm_suggestions.sync from the real FSM only
        prefix = str(row["key"]).split(":")[0]
        rests = RESTS_ON.get(prefix)
        if rests and any(src in unconnected for src in rests):
            gone += _delete(db, "suggestions", "key = ?", (row["key"],))
    return gone


def remove_seeded_sample_data(db, j: Any) -> dict[str, int]:
    """Run the clean-up once (sample data off only). Returns the counts removed ({} when it already ran or sample data is on).
    ``j`` is the Jarvis being built: only its settings and its FSM / mailbox / accounts stand-ins are looked at."""
    if demo_guard.sample_on(j) or db.get_kv(CLEANUP_KEY):
        return {}
    try:
        seeded = db.get_kv("stock_demo_seeded") == "1"
        unconnected: set[str] = set()
        for key, obj in ((demo_guard.FSM, "fsm"), (demo_guard.MAIL, "mail"), (demo_guard.ACCOUNTS, "finance")):
            if getattr(getattr(j, obj, None), "demo", False):
                unconnected.add(key)
        if seeded:
            unconnected.add(demo_guard.STOCK)
        if not (j.settings.data_dir / "accreditations.yaml").exists():
            unconnected.add("accreditations_example")
        counts: dict[str, int] = {}
        if seeded:
            counts.update(_remove_seeded_stock(db))
            db.set_kv("stock_demo_seeded", "cleared")
        counts["suggestions"] = _remove_sample_suggestions(db, unconnected)
        db.set_kv(CLEANUP_KEY, "done")
        log.info("Sample data clean-up: removed %s", ", ".join(f"{k}={v}" for k, v in counts.items()) or "nothing")
        return counts
    except Exception:  # noqa: BLE001 - a clean-up must never stop Jarvis starting; it tries again next start
        log.exception("Sample data clean-up failed; nothing more was removed")
        return {}
