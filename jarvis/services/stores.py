"""Stores / stock control: stock in the stores and on each engineer's van, movements against jobs,
reorder lists and purchase orders, stocktakes and usage analysis.

Source of truth: Salts FSM's stock records when the FSM API exposes them (synced into
a local working copy before each report; movements are posted back to the FSM).
Otherwise Jarvis keeps its own stock ledger. Every change is a movement:
receive (supplier -> location), issue (location -> job), transfer (location -> location),
return (job -> location) or adjust (stocktake correction).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .. import demo_guard
from ..db import Database, now_iso

log = logging.getLogger(__name__)
STORES = "Stores"
SYNC_SECONDS = 60
EXTERNAL = {"supplier", "job", "adjustment"}

DEMO_ITEMS = [
    # sku, name, category, unit_cost, reorder_level, reorder_qty, supplier
    ("GNT-S4-OPT", "Gent S-Quad optical heat multisensor", "Fire detection", 58.0, 12, 24, "Fire Alarm Wholesale Ltd"),
    ("GNT-S4-BASE", "Gent S-Quad base", "Fire detection", 9.5, 15, 30, "Fire Alarm Wholesale Ltd"),
    ("GNT-S4-SND", "Gent S-Quad sounder/strobe", "Fire detection", 96.0, 6, 12, "Fire Alarm Wholesale Ltd"),
    ("MCP-RED", "Addressable manual call point (red)", "Fire detection", 34.0, 8, 16, "Fire Alarm Wholesale Ltd"),
    ("APO-SOTEC-OPT", "Apollo Soteria optical detector", "Fire detection", 31.0, 10, 20, "Fire Alarm Wholesale Ltd"),
    ("BAT-12V7", "12V 7Ah SLA battery", "Power", 14.5, 20, 40, "Security Distribution UK"),
    ("BAT-12V17", "12V 17Ah SLA battery", "Power", 38.0, 8, 16, "Security Distribution UK"),
    ("FP200-100", "FP200 Gold 2-core 1.5mm red (100m)", "Cable", 92.0, 4, 8, "Cable & Fixings Direct"),
    ("EL-BH-3H", "3hr LED emergency bulkhead (self-test)", "Emergency lighting", 27.0, 10, 20, "Fire Alarm Wholesale Ltd"),
    ("EL-EXIT", "LED exit sign with legend", "Emergency lighting", 33.0, 6, 12, "Fire Alarm Wholesale Ltd"),
    ("PIR-G2", "Grade 2 PIR detector", "Intruder", 19.0, 10, 20, "Security Distribution UK"),
    ("CAM-4MP-DOME", "4MP IP turret camera", "CCTV", 74.0, 4, 8, "Security Distribution UK"),
    ("EXT-CO2-2", "2kg CO2 extinguisher", "Extinguishers", 41.0, 6, 12, "Fire Alarm Wholesale Ltd"),
    ("EXT-FOAM-6", "6L foam extinguisher", "Extinguishers", 36.0, 6, 12, "Fire Alarm Wholesale Ltd"),
]
DEMO_VANS = ["Van - Dan Harper", "Van - Priya Shah", "Van - Tom Wilkinson", "Van - Megan Lowe"]


class Stores:
    def __init__(self, db: Database, demo_seed: bool = False, fsm=None):
        self.db = db
        # Kept as the live router (not resolved to None here) so that, exactly like staff/accountant/tracker,
        # a Salts FSM connection made later on the Settings page is picked up without a Jarvis restart - see
        # sync()/record() below, which check whether it's still in demo mode fresh on every call instead.
        self.fsm = fsm
        self.source = "Jarvis stock ledger"
        self._synced = 0.0
        if demo_seed and not self.db.query_one("SELECT sku FROM stock_items LIMIT 1") \
                and not self.db.get_kv("stock_demo_seeded"):
            self._seed_demo()

    @property
    def demo(self) -> bool:
        return self.db.get_kv("stock_demo_seeded") == "1"

    # ------------------------------------------------------------------ Salts FSM sync
    async def sync(self, force: bool = False) -> None:
        """Refresh the working copy from Salts FSM (the system of record when it has stock data)."""
        if self.fsm is None or getattr(self.fsm, "demo", True) or (not force and time.time() - self._synced < SYNC_SECONDS):
            return
        try:
            rows = await self.fsm.stock()
        except Exception as e:  # noqa: BLE001
            log.info("Salts FSM stock not available (%s) - using Jarvis ledger", e)
            self.source = "Jarvis stock ledger (Salts FSM stock API not reachable)"
            self._synced = time.time()
            return
        for table in ("stock_levels", "stock_items"):
            self.db.execute(f"DELETE FROM {table}")
        for r in rows:
            if not r.get("sku") and not r.get("name"):
                continue
            sku = str(r.get("sku") or r.get("name"))
            self.upsert_item(sku, name=r.get("name") or sku, category=r.get("category") or "",
                             unit_cost=float(r.get("unit_cost") or 0), reorder_level=float(r.get("reorder_level") or 0),
                             reorder_qty=float(r.get("reorder_qty") or 0), supplier=r.get("supplier") or "")
            self._add(sku, r.get("location") or STORES, float(r.get("qty") or 0))
        try:
            moves = await self.fsm.stock_movements(date.today() - timedelta(days=180), date.today())
            self.db.execute("DELETE FROM stock_moves")
            for m in moves:
                kind = str(m.get("kind") or "issue").lower()
                kind = "issue" if kind in ("issue", "issued", "used", "consumed", "out") else kind
                self.db.execute("INSERT INTO stock_moves (created_at, sku, qty, kind, from_loc, to_loc, job_ref, note) "
                                "VALUES (?,?,?,?,?,?,?,?)", (str(m.get("date") or now_iso()), str(m.get("sku")),
                                                            abs(float(m.get("qty") or 0)), kind, m.get("location") or "",
                                                            "job" if kind == "issue" else "", m.get("job_ref") or "", "fsm"))
        except Exception as e:  # noqa: BLE001
            log.info("Salts FSM stock movements not available: %s", e)
        self.source = "Salts FSM"
        self._synced = time.time()

    async def record(self, kind: str, item: str, qty: float, **kw: Any) -> dict[str, Any]:
        """Record a movement - in Salts FSM when it is the system of record, else in Jarvis' ledger."""
        await self.sync()
        result = self.move(kind, item, qty, **kw)
        if self.fsm is not None and self.source == "Salts FSM":
            await self.fsm.record_stock_movement({
                "sku": result["sku"], "quantity": qty, "type": kind, "from_location": result["from"],
                "to_location": result["to"], "job": kw.get("job_ref") or None, "note": kw.get("note") or ""})
            await self.sync(force=True)
            result["recorded_in"] = "Salts FSM"
        else:
            result["recorded_in"] = "Jarvis stock ledger"
        return result

    # ------------------------------------------------------------------ items
    def upsert_item(self, sku: str, **fields: Any) -> dict[str, Any]:
        existing = self.item(sku)
        if existing:
            updates = {k: v for k, v in fields.items() if v is not None}
            if updates:
                cols = ", ".join(f"{k} = ?" for k in updates)
                self.db.execute(f"UPDATE stock_items SET {cols} WHERE sku = ?", (*updates.values(), sku))
        else:
            data = {"name": sku, "category": "", "unit": "each", "unit_cost": 0, "reorder_level": 0, "reorder_qty": 0,
                    "supplier": "", "notes": ""}
            data.update({k: v for k, v in fields.items() if v is not None})
            self.db.execute("INSERT INTO stock_items (sku, name, category, unit, unit_cost, reorder_level, reorder_qty,"
                            " supplier, notes) VALUES (?,?,?,?,?,?,?,?,?)",
                            (sku, data["name"], data["category"], data["unit"], data["unit_cost"],
                             data["reorder_level"], data["reorder_qty"], data["supplier"], data["notes"]))
        return self.item(sku)

    def item(self, sku: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM stock_items WHERE sku = ?", (sku,))

    def find_item(self, text: str) -> list[dict[str, Any]]:
        like = f"%{text.lower()}%"
        return self.db.query("SELECT * FROM stock_items WHERE lower(sku) LIKE ? OR lower(name) LIKE ? "
                             "OR lower(category) LIKE ? ORDER BY name", (like, like, like))

    def resolve_item(self, item: str) -> dict[str, Any]:
        """Public lookup by SKU or name/category text - the current record, including its latest unit cost."""
        return self._resolve(item)

    def _resolve(self, item: str) -> dict[str, Any]:
        exact = self.item(item) or self.item(item.upper())
        if exact:
            return exact
        hits = self.find_item(item)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise ValueError(f"No stock item matches '{item}'. Add it first with stock_item_update.")
        raise ValueError(f"'{item}' matches several items: " + "; ".join(f"{h['sku']} ({h['name']})" for h in hits[:8]))

    # ------------------------------------------------------------------ movements
    def _level(self, sku: str, location: str) -> float:
        row = self.db.query_one("SELECT qty FROM stock_levels WHERE sku = ? AND lower(location) = lower(?)", (sku, location))
        return row["qty"] if row else 0.0

    def _canonical_location(self, location: str) -> str:
        row = self.db.query_one("SELECT location FROM stock_levels WHERE lower(location) = lower(?) LIMIT 1", (location,))
        return row["location"] if row else location

    def _add(self, sku: str, location: str, delta: float) -> None:
        location = self._canonical_location(location)
        self.db.execute("INSERT INTO stock_levels (sku, location, qty) VALUES (?,?,?) ON CONFLICT(sku, location) "
                        "DO UPDATE SET qty = qty + excluded.qty", (sku, location, delta))

    def move(self, kind: str, item: str, qty: float, *, from_loc: str = "", to_loc: str = "", job_ref: str = "",
             note: str = "", allow_negative: bool = False) -> dict[str, Any]:
        if qty <= 0:
            raise ValueError("Quantity must be positive")
        it = self._resolve(item)
        sku = it["sku"]
        if kind == "receive":
            from_loc, to_loc = "supplier", to_loc or STORES
        elif kind == "issue":
            from_loc, to_loc = from_loc or STORES, "job"
        elif kind == "transfer":
            if not from_loc or not to_loc:
                raise ValueError("A transfer needs both from and to locations")
        elif kind == "return":
            from_loc, to_loc = "job", to_loc or STORES
        else:
            raise ValueError("kind must be receive, issue, transfer or return")
        if from_loc not in EXTERNAL:
            have = self._level(sku, from_loc)
            if have < qty and not allow_negative:
                raise ValueError(f"Only {have:g} {it['name']} recorded at {from_loc}. Check the location or do a stocktake.")
            self._add(sku, from_loc, -qty)
        if to_loc not in EXTERNAL:
            self._add(sku, to_loc, qty)
        self.db.execute("INSERT INTO stock_moves (created_at, sku, qty, kind, from_loc, to_loc, job_ref, note) "
                        "VALUES (?,?,?,?,?,?,?,?)", (now_iso(), sku, qty, kind, from_loc, to_loc, job_ref, note))
        return {"item": it["name"], "sku": sku, "qty": qty, "kind": kind, "from": from_loc, "to": to_loc,
                "job": job_ref, "now_at": {loc: self._level(sku, loc) for loc in (from_loc, to_loc) if loc not in EXTERNAL}}

    def stocktake(self, location: str, counts: dict[str, float]) -> dict[str, Any]:
        variances = []
        for item, counted in counts.items():
            it = self._resolve(item)
            recorded = self._level(it["sku"], location)
            diff = counted - recorded
            if diff:
                self._add(it["sku"], location, diff)
                self.db.execute("INSERT INTO stock_moves (created_at, sku, qty, kind, from_loc, to_loc, note) "
                                "VALUES (?,?,?,?,?,?,?)", (now_iso(), it["sku"], abs(diff), "adjust",
                                                           "adjustment" if diff > 0 else location,
                                                           location if diff > 0 else "adjustment", "stocktake"))
            variances.append({"sku": it["sku"], "item": it["name"], "recorded": recorded, "counted": counted,
                              "variance": diff, "variance_value": round(diff * it["unit_cost"], 2)})
        return {"location": location, "lines": variances,
                "net_variance_value": round(sum(v["variance_value"] for v in variances), 2)}

    # ------------------------------------------------------------------ reports
    def _sample(self) -> None:
        """Serving the seeded sample stock: tells a tool call (jarvis/demo_guard.py) not to hand it to the model."""
        if self.demo:
            demo_guard.touch(demo_guard.STOCK)

    def levels(self, location: str | None = None, search: str | None = None) -> dict[str, Any]:
        self._sample()
        rows = self.db.query("SELECT i.sku, i.name, i.category, i.unit, i.unit_cost, i.reorder_level, "
                             "l.location, l.qty FROM stock_items i LEFT JOIN stock_levels l ON l.sku = i.sku "
                             "ORDER BY i.category, i.name")
        items: dict[str, dict[str, Any]] = {}
        for r in rows:
            if search and search.lower() not in f"{r['sku']} {r['name']} {r['category']}".lower():
                continue
            it = items.setdefault(r["sku"], {"sku": r["sku"], "name": r["name"], "category": r["category"],
                                             "unit_cost": r["unit_cost"], "reorder_level": r["reorder_level"],
                                             "stores": 0.0, "vans": {}, "total": 0.0})
            if r["location"] is None or r["qty"] == 0:
                continue
            if location and location.lower() not in r["location"].lower():
                continue
            if r["location"] == STORES:
                it["stores"] += r["qty"]
            else:
                it["vans"][r["location"]] = r["qty"]
            it["total"] += r["qty"]
        out = list(items.values())
        for it in out:
            it["value"] = round(it["total"] * it["unit_cost"], 2)
            it["below_reorder"] = it["stores"] < it["reorder_level"]
        return {"demo": self.demo, "source": self.source, "items": out,
                "total_value": round(sum(i["value"] for i in out), 2)}

    def locations(self) -> list[dict[str, Any]]:
        self._sample()
        return self.db.query("SELECT l.location, SUM(l.qty) AS units, ROUND(SUM(l.qty * i.unit_cost), 2) AS value "
                             "FROM stock_levels l JOIN stock_items i ON i.sku = l.sku WHERE l.qty != 0 "
                             "GROUP BY l.location ORDER BY l.location")

    def reorder_list(self) -> dict[str, Any]:
        by_supplier: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for it in self.levels()["items"]:
            if it["stores"] < it["reorder_level"]:
                row = self.item(it["sku"])
                qty = max(row["reorder_qty"], row["reorder_level"] - it["stores"])
                by_supplier[row["supplier"] or "No supplier set"].append({
                    "sku": it["sku"], "item": it["name"], "in_stores": it["stores"], "on_vans": sum(it["vans"].values()),
                    "reorder_level": it["reorder_level"], "order_qty": qty, "unit_cost": row["unit_cost"],
                    "line_cost": round(qty * row["unit_cost"], 2)})
        orders = [{"supplier": sup, "lines": lines, "total_ex_vat": round(sum(l["line_cost"] for l in lines), 2)}
                  for sup, lines in by_supplier.items()]
        return {"demo": self.demo, "purchase_orders": orders,
                "grand_total_ex_vat": round(sum(o["total_ex_vat"] for o in orders), 2)}

    def usage(self, days: int = 90) -> dict[str, Any]:
        since = (date.today() - timedelta(days=days)).isoformat()
        used = {r["sku"]: r["qty"] for r in self.db.query(
            "SELECT sku, SUM(qty) AS qty FROM stock_moves WHERE kind = 'issue' AND created_at >= ? GROUP BY sku", (since,))}
        rows = []
        for it in self.levels()["items"]:
            u = used.get(it["sku"], 0)
            weekly = u / (days / 7)
            rows.append({"sku": it["sku"], "item": it["name"], "used": u, "per_week": round(weekly, 1),
                         "on_hand": it["total"], "weeks_cover": round(it["total"] / weekly, 1) if weekly else None,
                         "value_on_hand": it["value"]})
        rows.sort(key=lambda r: -r["used"])
        slow = [r for r in rows if r["used"] == 0 and r["on_hand"] > 0]
        return {"period_days": days, "fast_movers": rows[:10], "slow_or_dead_stock": slow,
                "dead_stock_value": round(sum(r["value_on_hand"] for r in slow), 2)}

    def job_materials(self, job_ref: str) -> dict[str, Any]:
        self._sample()
        rows = self.db.query("SELECT m.sku, i.name, m.kind, m.qty, i.unit_cost FROM stock_moves m JOIN stock_items i "
                             "ON i.sku = m.sku WHERE m.job_ref = ? ORDER BY m.id", (job_ref,))
        net = defaultdict(float)
        for r in rows:
            net[(r["sku"], r["name"], r["unit_cost"])] += r["qty"] if r["kind"] == "issue" else -r["qty"] if r["kind"] == "return" else 0
        lines = [{"sku": k[0], "item": k[1], "qty": q, "cost": round(q * k[2], 2)} for k, q in net.items() if q]
        return {"job": job_ref, "materials": lines, "materials_cost": round(sum(l["cost"] for l in lines), 2)}

    # ------------------------------------------------------------------ demo
    def _seed_demo(self) -> None:
        import random

        rng = random.Random(7)
        for sku, name, cat, cost, lvl, rq, sup in DEMO_ITEMS:
            self.upsert_item(sku, name=name, category=cat, unit_cost=cost, reorder_level=lvl, reorder_qty=rq, supplier=sup)
            self._add(sku, STORES, max(0, int(lvl * rng.uniform(0.3, 2.2))))
            for van in DEMO_VANS:
                if rng.random() < 0.6:
                    self._add(sku, van, rng.randint(1, 6))
            for back in range(rng.randint(0, 8)):
                self.db.execute("INSERT INTO stock_moves (created_at, sku, qty, kind, from_loc, to_loc, job_ref, note) "
                                "VALUES (?,?,?,?,?,?,?,?)",
                                ((date.today() - timedelta(days=rng.randint(1, 80))).isoformat() + "T10:00:00+00:00",
                                 sku, rng.randint(1, 6), "issue", rng.choice(DEMO_VANS), "job", f"J{23000 + rng.randint(1, 900)}",
                                 "demo"))
        self.db.set_kv("stock_demo_seeded", "1")

    def clear_demo(self) -> None:
        for table in ("stock_moves", "stock_levels", "stock_items"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.set_kv("stock_demo_seeded", "cleared")
