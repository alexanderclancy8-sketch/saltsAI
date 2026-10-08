"""System schematics: clean line diagrams of a fire alarm or security system, drawn by CODE from a spec the model writes.

The model never draws. It writes a structured spec - what is on each loop and in which order, what each input does to each output,
what hangs off which switch or controller - and this module checks it against a closed vocabulary with hard caps, then
``schematic_layout`` works out every coordinate and ``schematic_render`` draws SVG / PNG / PDF. Nothing the model writes is ever
markup or code: every label is plain text (control characters, HTML tags and secret-looking strings stripped), and no price may
appear anywhere (a schematic is engineering, never commercial).

Three kinds (``KINDS``), each with its own spec (``SPEC_HELP`` is the short form the tool description quotes):

* ``fire_loop`` - an addressable panel with its loops (devices in loop order with address / zone, isolators, the A end out and the
  B end back), repeaters / interfaces on the panel network, panel I/O; or a conventional panel with zones ending in an EOL device.
* ``cause_effect`` - a cause-and-effect matrix: inputs (zones, devices) down the side, outputs (sounders, door holders, plant
  shutdown, AOVs, fire signalling...) across the top, a short action code in each cell.
* ``network`` - CCTV / access control / intruder / signalling topology as trees: NVR -> switch -> cameras, controller -> readers /
  locks, panel -> expanders -> detectors, transmitter -> ARC (a dual path is shown as two links).

FACTS vs ASSUMPTIONS: anything on a drawing that does not come from a record (the FSM asset list, the job, a quote) is marked
``"assumed": true`` in the spec and is drawn grey and dashed, with the assumption listed in the notes. Every drawing (and every
export) carries "Draft schematic prepared with Jarvis – to be checked by a competent person." and never claims compliance.

Storage: ``schematics`` (one row per drawing: kind, title, site, system, job ref, latest revision, who made it) and
``schematic_revisions`` (each revision's normalised spec and change note). A revision is P1, P2, ... (preliminary - these are
drafts). Saving is a write: blocked during a question check (``checkmode.guard``), where a drawing is still laid out and
described but not kept. Exports are on demand, rendered from the stored spec; a save and every export leave a line in "What
Jarvis did" (audit kind ``schematic``).
"""

from __future__ import annotations

import difflib
import json
import re
import secrets
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..brain import checkmode
from ..db import now_iso
from ..integrations.fsm_data import clean_text
from . import schematic_layout as layout_mod
from . import schematic_symbols as symbols

KINDS = ("fire_loop", "cause_effect", "network")
KIND_TEXT = {"fire_loop": "Fire alarm schematic", "cause_effect": "Cause and effect matrix", "network": "Security system schematic"}
SOURCES = ("fsm", "description", "quote", "survey", "mixed")
DRAWING_ID = re.compile(r"^[a-f0-9]{12}$")
EXPORT_FORMATS = ("svg", "png", "pdf")
PAPERS = ("a4", "a3")
COMPANY = "Salts Fire & Security"

# ---- caps (deliberately generous for real sites, small enough to stay readable and fast)
MAX_ERRORS = 12
MAX_TITLE, MAX_SITE, MAX_SYSTEM, MAX_REF, MAX_NOTE, MAX_NOTES = 90, 80, 60, 30, 160, 12
MAX_LOOPS, MAX_LOOP_DEVICES, MAX_ZONES, MAX_ZONE_DEVICES, MAX_FIRE_DEVICES = 8, 250, 32, 40, 600
MAX_NETWORK_ITEMS, MAX_PANEL_IO = 16, 12
MAX_INPUTS, MAX_OUTPUTS, MAX_EFFECTS = 60, 30, 1200
MAX_SYSTEMS, MAX_NODES, MAX_SYSTEM_NODES, MAX_DEPTH, MAX_CHILDREN = 6, 300, 150, 8, 64
MAX_SPEC_BYTES = 300_000
LIST_MAX = 50

CE_CATEGORIES = ("sounders", "door_holders", "plant_shutdown", "aov", "signalling", "lifts", "access_release", "gas_shutoff",
                 "suppression", "other")
CE_CATEGORY_ALIASES = {"sounder": "sounders", "alarms": "sounders", "door_holder": "door_holders", "doors": "door_holders",
                       "plant": "plant_shutdown", "ahu": "plant_shutdown", "hvac": "plant_shutdown", "smoke_vent": "aov",
                       "smoke_vents": "aov", "aovs": "aov", "arc": "signalling", "fire_signalling": "signalling", "lift": "lifts",
                       "access": "access_release", "access_control": "access_release", "gas": "gas_shutoff", "sprinkler": "suppression"}
CE_ACTIONS = {"operate": "X", "evacuate": "C", "alert": "P", "release": "R", "shutdown": "S", "signal": "T"}
CE_ACTION_ALIASES = {"activate": "operate", "on": "operate", "x": "operate", "continuous": "evacuate", "evac": "evacuate",
                     "pulse": "alert", "pulsing": "alert", "intermittent": "alert", "drop": "release", "unlock": "release",
                     "open": "operate", "stop": "shutdown", "shut_down": "shutdown", "off": "shutdown", "transmit": "signal",
                     "call": "signal", "c": "evacuate", "p": "alert", "r": "release", "s": "shutdown", "t": "signal"}
NET_KINDS = ("cctv", "access", "intruder", "signalling", "fire", "network", "door_entry", "other")
NET_KIND_ALIASES = {"camera": "cctv", "cameras": "cctv", "video": "cctv", "access_control": "access", "acs": "access",
                    "intruder_alarm": "intruder", "alarm": "intruder", "ias": "intruder", "comms": "signalling",
                    "arc": "signalling", "intercom": "door_entry"}
LINK_TYPES = tuple(layout_mod.LINK_TEXT)
LINK_ALIASES = {"cat5": "ethernet", "cat6": "ethernet", "lan": "ethernet", "utp": "ethernet", "fiber": "fibre", "wireless": "wifi",
                "485": "rs485", "rs_485": "rs485", "lte": "4g", "gprs": "4g", "cellular": "4g", "mobile": "4g", "gsm": "4g",
                "broadband": "ip", "internet": "ip", "landline": "pstn", "hard_wired": "hardwired", "wired": "hardwired"}

PRICE = re.compile(r"£|\bGBP\b|\b(?:price|priced|cost|costs|rate)\s*[:=]?\s*\d", re.I)

SPEC_HELP = {
    "fire_loop": (
        "{title, site?, system?, job_ref?, source: fsm|description|quote|survey|mixed, source_ref?, assumptions?: [text], notes?: [text], "
        "system_type: addressable|conventional, panel: {label, model?, location?, assumed?}, panel_io?: [{label, assumed?}], "
        "network?: [{type: repeater|panel|graphics|interface|io|other, label, location?, assumed?}], "
        "loops (addressable): [{number, label?, return_confirmed?: true, devices: [DEVICE in loop order]}], "
        "zones (conventional): [{number, label?, eol?: 'EOL 4k7', eol_assumed?, devices: [DEVICE]}]}. "
        "DEVICE = {type: smoke|heat|multi|co|flame|beam|asd|mcp|sounder|vad|sounder_vad|io|input|output|interface|zone_module|"
        "door_holder|sprinkler_flow|isolator|other, address?, zone?, label? (location), isolator?: true if it has a built-in isolator, "
        "assumed?, code? (2-4 letters, for other)}. Up to 8 loops x 250 devices / 32 zones x 40, 600 devices in all."),
    "cause_effect": (
        "{title, site?, system?, job_ref?, source, source_ref?, assumptions?, notes?, inputs: [{id: 'Z1', label, assumed?}] (rows, up to 60), "
        "outputs: [{id: 'O1', label, category: sounders|door_holders|plant_shutdown|aov|signalling|lifts|access_release|gas_shutoff|"
        "suppression|other, assumed?}] (columns, up to 30), effects: [{input: 'Z1', output: 'O1', action: operate|evacuate|alert|"
        "release|shutdown|signal, delay_s?: seconds, assumed?}]}. Leave a cell out for 'no effect'."),
    "network": (
        "{title, site?, system?, job_ref?, source, source_ref?, assumptions?, notes?, systems: [{kind: cctv|access|intruder|signalling|"
        "fire|network|door_entry|other, label?, nodes: [NODE]}] (up to 6 systems, 300 nodes)}. NODE = {id (unique), type: nvr|dvr|"
        "camera|ptz|monitor|switch|poe_switch|router|server|controller|reader|keypad|lock|exit_button|door_contact|intercom|"
        "intruder_panel|expander|pir|dual_tech|shock|glass_break|panic_button|bellbox|beam|stu|arc|psu|smoke|other, label?, "
        "parent? (the id it connects to; none = a head-end), link?: ethernet|poe|fibre|wifi|rs485|wiegand|osdp|bus|zone|relay|"
        "hardwired|radio|4g|ip|pstn|coax|power|other, secondary_link? (a second path, e.g. 4g for dual-path signalling), port?, "
        "location?, assumed?, code?}."),
}


class SchematicError(ValueError):
    """The spec can't be drawn. ``problems`` lists what to fix (path: message); str() is what the model is told."""

    def __init__(self, problems: list[str]):
        self.problems = problems[:MAX_ERRORS]
        more = len(problems) - len(self.problems)
        super().__init__("The schematic spec needs fixing: " + "; ".join(self.problems) + (f" (and {more} more)" if more > 0 else ""))


# ============================================================================================= validation
class _V:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.any_assumed = False

    def err(self, path: str, msg: str) -> None:
        if len(self.errors) < MAX_ERRORS * 2:
            self.errors.append(f"{path}: {msg}" if path else msg)

    def obj(self, value: Any, path: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.err(path, "must be an object")
            return None
        return value

    def keys(self, obj: dict[str, Any], allowed: tuple[str, ...], path: str) -> None:
        for k in obj:
            if k not in allowed:
                near = difflib.get_close_matches(str(k), allowed, n=1, cutoff=0.6)
                self.err(f"{path}.{k}" if path else str(k), "unknown field" + (f" - did you mean '{near[0]}'?" if near else
                                                                                 f" (allowed: {', '.join(allowed)})"))

    def text(self, obj: dict[str, Any], key: str, path: str, limit: int, required: bool = False, default: str = "") -> str:
        raw = obj.get(key)
        if raw is None or raw == "":
            if required:
                self.err(f"{path}.{key}" if path else key, "is required")
            return default
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            self.err(f"{path}.{key}" if path else key, "must be text")
            return default
        value = clean_text(raw, limit)
        if PRICE.search(value):
            self.err(f"{path}.{key}" if path else key, "a schematic carries no prices or costs - take the money out")
            return default
        if required and not value:
            self.err(f"{path}.{key}" if path else key, "is required")
        return value

    def flag(self, obj: dict[str, Any], key: str, path: str, default: bool = False) -> bool:
        raw = obj.get(key, default)
        if raw is None:
            return default
        if not isinstance(raw, bool):
            self.err(f"{path}.{key}", "must be true or false")
            return default
        return raw

    def assumed(self, obj: dict[str, Any], path: str) -> bool:
        a = self.flag(obj, "assumed", path)
        if a:
            self.any_assumed = True
        return a

    def items(self, obj: dict[str, Any], key: str, path: str, cap: int, required: bool = False, noun: str = "items") -> list[Any]:
        raw = obj.get(key)
        p = f"{path}.{key}" if path else key
        if raw is None:
            if required:
                self.err(p, f"is required (a list of {noun})")
            return []
        if not isinstance(raw, list):
            self.err(p, f"must be a list of {noun}")
            return []
        if required and not raw:
            self.err(p, f"needs at least one of the {noun}")
        if len(raw) > cap:
            self.err(p, f"has {len(raw)} {noun}; at most {cap} fit on one drawing - split it into two drawings")
            return raw[:cap]
        return raw

    def choice(self, raw: Any, allowed: tuple[str, ...], aliases: dict[str, str], path: str, what: str, default: str | None = None) -> str | None:
        if raw is None or raw == "":
            if default is None:
                self.err(path, f"is required ({what}: {', '.join(allowed)})")
            return default
        key = symbols.norm_key(raw)
        key = aliases.get(key, key)
        if key in allowed:
            return key
        near = difflib.get_close_matches(key, allowed, n=2, cutoff=0.5)
        self.err(path, f"'{clean_text(raw, 30)}' isn't a {what}" + (f" - did you mean {' or '.join(repr(n) for n in near)}?" if near
                                                                       else f" (use one of: {', '.join(allowed)})"))
        return default

    def symbol_type(self, raw: Any, allowed: tuple[str, ...], path: str) -> str | None:
        if raw is None or raw == "":
            self.err(path, f"is required (one of: {', '.join(allowed)})")
            return None
        key = symbols.resolve(raw, allowed)
        if key:
            return key
        near = difflib.get_close_matches(symbols.norm_key(raw), allowed, n=2, cutoff=0.5)
        self.err(path, f"'{clean_text(raw, 30)}' isn't a device type here" + (f" - did you mean {' or '.join(repr(n) for n in near)}?"
                                                                                if near else f" (use one of: {', '.join(allowed)}, "
                                                                                             "or 'other' with a short code)"))
        return None

    def code(self, obj: dict[str, Any], key_type: str | None, path: str) -> str:
        code = self.text(obj, "code", path, 8)
        code = re.sub(r"[^A-Za-z0-9/+&-]", "", code)[:4].upper()
        if key_type == "other" and not code:
            self.err(f"{path}.code", "an 'other' device needs a short code (2-4 letters) to show in its symbol")
        return code

    def number(self, obj: dict[str, Any], key: str, path: str, lo: int, hi: int, default: int) -> int:
        raw = obj.get(key, default)
        if raw is None:
            return default
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            self.err(f"{path}.{key}", f"must be a whole number from {lo} to {hi}")
            return default
        try:
            n = int(str(raw).strip())
        except ValueError:
            self.err(f"{path}.{key}", f"must be a whole number from {lo} to {hi}")
            return default
        if not lo <= n <= hi:
            self.err(f"{path}.{key}", f"must be from {lo} to {hi}")
            return default
        return n

    def notes(self, obj: dict[str, Any], key: str, path: str) -> list[str]:
        out = []
        for i, n in enumerate(self.items(obj, key, path, MAX_NOTES, noun="short notes")):
            if not isinstance(n, (str, int, float)) or isinstance(n, bool):
                self.err(f"{key}[{i}]", "must be text")
                continue
            t = clean_text(n, MAX_NOTE)
            if PRICE.search(t):
                self.err(f"{key}[{i}]", "a schematic carries no prices or costs")
                continue
            if t:
                out.append(t)
        return out


COMMON = ("title", "site", "system", "job_ref", "source", "source_ref", "assumptions", "notes")


def _common(v: _V, spec: dict[str, Any], kind: str) -> dict[str, Any]:
    return {"title": v.text(spec, "title", "", MAX_TITLE, required=True),
            "site": v.text(spec, "site", "", MAX_SITE),
            "system": v.text(spec, "system", "", MAX_SYSTEM) or {"fire_loop": "Fire alarm system", "cause_effect": "Fire alarm cause and effect",
                                                                  "network": "Security systems"}[kind],
            "job_ref": v.text(spec, "job_ref", "", MAX_REF),
            "source": v.choice(spec.get("source"), SOURCES, {"fsm_assets": "fsm", "assets": "fsm", "records": "fsm",
                                                              "specification": "quote", "spec": "quote"}, "source",
                               "source", default="description"),
            "source_ref": v.text(spec, "source_ref", "", MAX_SITE),
            "assumptions": v.notes(spec, "assumptions", ""),
            "notes": v.notes(spec, "notes", "")}


DEVICE_KEYS = ("type", "address", "zone", "label", "location", "isolator", "assumed", "code", "note")


def _device(v: _V, raw: Any, path: str, allowed: tuple[str, ...]) -> dict[str, Any] | None:
    d = v.obj(raw, path)
    if d is None:
        return None
    v.keys(d, DEVICE_KEYS, path)
    key = v.symbol_type(d.get("type"), allowed, f"{path}.type")
    label = v.text(d, "label", path, 48) or v.text(d, "location", path, 48)
    out = {"type": key or "other", "address": v.text(d, "address", path, 8), "zone": v.text(d, "zone", path, 12), "label": label,
           "isolator": v.flag(d, "isolator", path), "assumed": v.assumed(d, path), "code": v.code(d, key, path)}
    note = v.text(d, "note", path, 80)
    if note:
        out["note"] = note
    return out


def _fire(v: _V, spec: dict[str, Any]) -> dict[str, Any]:
    v.keys(spec, COMMON + ("system_type", "panel", "panel_io", "network", "loops", "zones"), "")
    out = _common(v, spec, "fire_loop")
    stype = v.choice(spec.get("system_type"), ("addressable", "conventional"), {"analogue": "addressable", "analog": "addressable",
                                                                                 "analogue_addressable": "addressable"},
                     "system_type", "system type", default="addressable")
    out["system_type"] = stype
    praw = spec.get("panel") or {}
    panel = v.obj(praw, "panel") or {}
    v.keys(panel, ("label", "model", "location", "assumed"), "panel")
    out["panel"] = {"label": v.text(panel, "label", "panel", 60) or "Fire alarm panel", "model": v.text(panel, "model", "panel", 60),
                    "location": v.text(panel, "location", "panel", 60), "assumed": v.assumed(panel, "panel")}
    io = []
    for i, raw in enumerate(v.items(spec, "panel_io", "", MAX_PANEL_IO, noun="panel inputs / outputs")):
        p = f"panel_io[{i}]"
        item = v.obj(raw, p)
        if item is None:
            continue
        v.keys(item, ("label", "type", "assumed"), p)
        io.append({"label": v.text(item, "label", p, 60, required=True), "assumed": v.assumed(item, p)})
    out["panel_io"] = io
    net = []
    for i, raw in enumerate(v.items(spec, "network", "", MAX_NETWORK_ITEMS, noun="network items")):
        d = _device(v, raw, f"network[{i}]", symbols.FIRE_NETWORK_TYPES)
        if d is not None:
            net.append(d)
    out["network"] = net
    total = 0
    loops: list[dict[str, Any]] = []
    zones: list[dict[str, Any]] = []
    if stype == "addressable":
        if spec.get("zones"):
            v.err("zones", "zones (with EOL) are for conventional systems - for an addressable system put the devices on 'loops' and "
                           "give each device its 'zone'")
        seen_numbers: set[int] = set()
        for i, raw in enumerate(v.items(spec, "loops", "", MAX_LOOPS, required=True, noun="loops")):
            p = f"loops[{i}]"
            lp = v.obj(raw, p)
            if lp is None:
                continue
            v.keys(lp, ("number", "label", "return_confirmed", "devices"), p)
            number = v.number(lp, "number", p, 1, 99, i + 1)
            if number in seen_numbers:
                v.err(f"{p}.number", f"loop {number} appears twice")
            seen_numbers.add(number)
            devs, addresses = [], set()
            raw_devs = v.items(lp, "devices", p, MAX_LOOP_DEVICES, required=True, noun="devices")
            total += len(raw_devs)
            for k, rd in enumerate(raw_devs):
                d = _device(v, rd, f"{p}.devices[{k}]", symbols.FIRE_DEVICE_TYPES)
                if d is None:
                    continue
                if d["address"]:
                    if d["address"] in addresses:
                        v.err(f"{p}.devices[{k}].address", f"address {d['address']} is used twice on loop {number}")
                    addresses.add(d["address"])
                devs.append(d)
            loops.append({"number": number, "label": v.text(lp, "label", p, 40), "return_confirmed": v.flag(lp, "return_confirmed", p, True),
                          "devices": devs})
    else:
        if spec.get("loops"):
            v.err("loops", "loops are for addressable systems - for a conventional system use 'zones' (each ends in an EOL device)")
        seen_numbers = set()
        for i, raw in enumerate(v.items(spec, "zones", "", MAX_ZONES, required=True, noun="zones")):
            p = f"zones[{i}]"
            z = v.obj(raw, p)
            if z is None:
                continue
            v.keys(z, ("number", "label", "eol", "eol_assumed", "devices"), p)
            number = v.number(z, "number", p, 1, 999, i + 1)
            if number in seen_numbers:
                v.err(f"{p}.number", f"zone {number} appears twice")
            seen_numbers.add(number)
            raw_devs = v.items(z, "devices", p, MAX_ZONE_DEVICES, required=True, noun="devices")
            total += len(raw_devs)
            devs = [d for k, rd in enumerate(raw_devs) if (d := _device(v, rd, f"{p}.devices[{k}]", symbols.FIRE_DEVICE_TYPES))]
            eol_assumed = v.flag(z, "eol_assumed", p)
            if eol_assumed:
                v.any_assumed = True
            zones.append({"number": number, "label": v.text(z, "label", p, 40), "eol": v.text(z, "eol", p, 24) or "EOL",
                          "eol_assumed": eol_assumed, "devices": devs})
    if total > MAX_FIRE_DEVICES:
        v.err("loops" if stype == "addressable" else "zones", f"{total} devices in all; at most {MAX_FIRE_DEVICES} fit on one drawing - "
                                                              "split it (one drawing per panel or per floor)")
    out["loops"], out["zones"] = loops, zones
    return out


def _ce(v: _V, spec: dict[str, Any]) -> dict[str, Any]:
    v.keys(spec, COMMON + ("inputs", "outputs", "effects"), "")
    out = _common(v, spec, "cause_effect")

    def side(key: str, cap: int, noun: str, with_category: bool) -> list[dict[str, Any]]:
        rows, ids = [], set()
        for i, raw in enumerate(v.items(spec, key, "", cap, required=True, noun=noun)):
            p = f"{key}[{i}]"
            item = v.obj(raw, p)
            if item is None:
                continue
            v.keys(item, ("id", "label", "category", "assumed"), p)
            rid = v.text(item, "id", p, 10, required=True)
            if rid in ids:
                v.err(f"{p}.id", f"'{rid}' is used twice")
            ids.add(rid)
            row = {"id": rid, "label": v.text(item, "label", p, 60, required=True), "assumed": v.assumed(item, p)}
            if with_category:
                row["category"] = v.choice(item.get("category"), CE_CATEGORIES, CE_CATEGORY_ALIASES, f"{p}.category", "category",
                                           default="other")
            rows.append(row)
        return rows

    out["inputs"] = side("inputs", MAX_INPUTS, "inputs (causes)", False)
    out["outputs"] = side("outputs", MAX_OUTPUTS, "outputs (effects)", True)
    in_ids = [i["id"] for i in out["inputs"]]
    out_ids = [o["id"] for o in out["outputs"]]
    effects, seen = [], set()
    for i, raw in enumerate(v.items(spec, "effects", "", MAX_EFFECTS, noun="effects")):
        p = f"effects[{i}]"
        e = v.obj(raw, p)
        if e is None:
            continue
        v.keys(e, ("input", "output", "action", "delay_s", "assumed", "note"), p)
        inp = clean_text(e.get("input", ""), 10)
        outp = clean_text(e.get("output", ""), 10)
        if inp not in in_ids:
            near = difflib.get_close_matches(inp, in_ids, n=1, cutoff=0.5)
            v.err(f"{p}.input", f"'{inp}' isn't one of the inputs" + (f" - did you mean '{near[0]}'?" if near else ""))
            continue
        if outp not in out_ids:
            near = difflib.get_close_matches(outp, out_ids, n=1, cutoff=0.5)
            v.err(f"{p}.output", f"'{outp}' isn't one of the outputs" + (f" - did you mean '{near[0]}'?" if near else ""))
            continue
        if (inp, outp) in seen:
            v.err(p, f"{inp} -> {outp} is given twice; one cell holds one action")
            continue
        seen.add((inp, outp))
        action = v.choice(e.get("action"), tuple(CE_ACTIONS), CE_ACTION_ALIASES, f"{p}.action", "action", default="operate")
        delay = v.number(e, "delay_s", p, 0, 900, 0)
        code = CE_ACTIONS.get(action or "operate", "X") + (str(delay) if delay else "")
        effects.append({"input": inp, "output": outp, "action": action, "delay_s": delay, "code": code, "assumed": v.assumed(e, p)})
    # stable order: by row, then column
    effects.sort(key=lambda e: (in_ids.index(e["input"]), out_ids.index(e["output"])))
    out["effects"] = effects
    return out


NODE_KEYS = ("id", "type", "label", "parent", "link", "secondary_link", "port", "location", "assumed", "code", "note")


def _network(v: _V, spec: dict[str, Any]) -> dict[str, Any]:
    v.keys(spec, COMMON + ("systems",), "")
    out = _common(v, spec, "network")
    systems, all_ids, total = [], set(), 0
    for si, raw in enumerate(v.items(spec, "systems", "", MAX_SYSTEMS, required=True, noun="systems")):
        p = f"systems[{si}]"
        s = v.obj(raw, p)
        if s is None:
            continue
        v.keys(s, ("kind", "label", "nodes"), p)
        kind = v.choice(s.get("kind"), NET_KINDS, NET_KIND_ALIASES, f"{p}.kind", "system kind", default="other")
        raw_nodes = v.items(s, "nodes", p, MAX_SYSTEM_NODES, required=True, noun="nodes")
        total += len(raw_nodes)
        nodes: list[dict[str, Any]] = []
        for k, rn in enumerate(raw_nodes):
            np_ = f"{p}.nodes[{k}]"
            n = v.obj(rn, np_)
            if n is None:
                continue
            v.keys(n, NODE_KEYS, np_)
            nid = v.text(n, "id", np_, 24, required=True)
            if nid in all_ids:
                v.err(f"{np_}.id", f"'{nid}' is used twice - every node needs its own id")
            all_ids.add(nid)
            key = v.symbol_type(n.get("type"), symbols.SECURITY_NODE_TYPES, f"{np_}.type")
            node = {"id": nid, "type": key or "other", "label": v.text(n, "label", np_, 40),
                    "parent": clean_text(n["parent"], 24) if n.get("parent") not in (None, "") else "",
                    "link": v.choice(n.get("link"), LINK_TYPES, LINK_ALIASES, f"{np_}.link", "link type", default="") or "",
                    "secondary_link": v.choice(n.get("secondary_link"), LINK_TYPES, LINK_ALIASES, f"{np_}.secondary_link", "link type",
                                               default="") or "",
                    "port": v.text(n, "port", np_, 12), "location": v.text(n, "location", np_, 40), "assumed": v.assumed(n, np_),
                    "code": v.code(n, key, np_)}
            note = v.text(n, "note", np_, 80)
            if note:
                node["note"] = note
            nodes.append(node)
        # the tree: parents must be in the same system; no loops; not too deep
        ids = {n["id"] for n in nodes}
        by_parent: dict[str, list[str]] = {}
        for k, n in enumerate(nodes):
            if n["parent"] and n["parent"] not in ids:
                where = "another system" if n["parent"] in all_ids else "this system"
                near = difflib.get_close_matches(n["parent"], sorted(ids), n=1, cutoff=0.5)
                v.err(f"{p}.nodes[{k}].parent", f"'{n['parent']}' isn't a node of {where}" + (f" - did you mean '{near[0]}'?" if near else
                      " (a node connects to a node in the same system; put a cross-system link in the notes)"))
                n["parent"] = ""
            if n["parent"] == n["id"]:
                v.err(f"{p}.nodes[{k}].parent", "a node can't connect to itself")
                n["parent"] = ""
            by_parent.setdefault(n["parent"], []).append(n["id"])
        for parent, kids in by_parent.items():
            if parent and len(kids) > MAX_CHILDREN:
                v.err(p, f"'{parent}' has {len(kids)} things connected; at most {MAX_CHILDREN} - group them (e.g. by switch)")
        order: list[str] = []
        depth: dict[str, int] = {}

        def walk(nid: str, d: int, trail: tuple[str, ...]) -> None:
            if nid in trail:
                return
            depth[nid] = d
            order.append(nid)
            for kid in by_parent.get(nid, []):
                if kid not in depth:
                    walk(kid, d + 1, trail + (nid,))

        for root in by_parent.get("", []):
            walk(root, 0, ())
        missing = [n["id"] for n in nodes if n["id"] not in depth]
        if missing:
            v.err(p, f"these nodes connect round in a circle and never reach a head-end: {', '.join(missing[:6])} - "
                     "give the head-end no parent")
        if depth and max(depth.values()) >= MAX_DEPTH:
            v.err(p, f"the chain of connections is more than {MAX_DEPTH} deep - simplify it")
        for n in nodes:
            n["_depth"] = depth.get(n["id"], 0)
        systems.append({"kind": kind, "label": v.text(s, "label", p, 40), "nodes": nodes, "order": order})
    if total > MAX_NODES:
        v.err("systems", f"{total} nodes in all; at most {MAX_NODES} fit on one drawing - split it")
    out["systems"] = systems
    return out


VALIDATORS: dict[str, Callable[[_V, dict[str, Any]], dict[str, Any]]] = {"fire_loop": _fire, "cause_effect": _ce, "network": _network}


def validate(kind: Any, spec: Any) -> tuple[str, dict[str, Any]]:
    """(kind, the normalised spec) or SchematicError listing what to fix. Accepts a JSON string for the spec too."""
    k = symbols.norm_key(kind)
    k = {"fire": "fire_loop", "loop": "fire_loop", "fire_alarm": "fire_loop", "loops": "fire_loop", "c&e": "cause_effect",
         "c_e": "cause_effect", "cause_and_effect": "cause_effect", "matrix": "cause_effect", "security": "network",
         "topology": "network", "cctv": "network", "access": "network", "intruder": "network"}.get(k, k)
    if k not in KINDS:
        raise SchematicError([f"kind must be one of: {', '.join(KINDS)}"])
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            raise SchematicError(["spec must be a JSON object"]) from None
    if not isinstance(spec, dict):
        raise SchematicError(["spec must be an object - " + SPEC_HELP[k]])
    try:
        size = len(json.dumps(spec, default=str))
    except (TypeError, ValueError):
        raise SchematicError(["spec must be plain JSON"]) from None
    if size > MAX_SPEC_BYTES:
        raise SchematicError([f"spec is too big ({size // 1000} kB; at most {MAX_SPEC_BYTES // 1000} kB) - split the drawing"])
    v = _V()
    out = VALIDATORS[k](v, spec)
    if v.errors:
        raise SchematicError(v.errors)
    out["_any_assumed"] = v.any_assumed
    return k, out


def counts(kind: str, spec: dict[str, Any]) -> dict[str, int]:
    if kind == "fire_loop":
        devs = [d for c in (spec["loops"] or spec["zones"]) for d in c["devices"]]
        return {"circuits": len(spec["loops"] or spec["zones"]), "devices": len(devs),
                "isolators": sum(1 for d in devs if d["isolator"] or d["type"] == "isolator"),
                "assumed": sum(1 for d in devs if d["assumed"]), "network": len(spec["network"])}
    if kind == "cause_effect":
        return {"inputs": len(spec["inputs"]), "outputs": len(spec["outputs"]), "effects": len(spec["effects"]),
                "assumed": sum(1 for e in spec["effects"] if e["assumed"])}
    nodes = [n for s in spec["systems"] for n in s["nodes"]]
    return {"systems": len(spec["systems"]), "nodes": len(nodes), "assumed": sum(1 for n in nodes if n["assumed"])}


def describe(kind: str, spec: dict[str, Any]) -> str:
    """One sentence for the model / a screen reader: what the drawing shows."""
    c = counts(kind, spec)
    if kind == "fire_loop":
        what = "zone" if spec["system_type"] == "conventional" else "loop"
        text = f"{c['devices']} devices on {c['circuits']} {what}{'s' if c['circuits'] != 1 else ''}"
        if c["isolators"]:
            text += f", {c['isolators']} isolators"
        if c["network"]:
            text += f", {c['network']} items on the panel network"
    elif kind == "cause_effect":
        text = f"{c['inputs']} inputs x {c['outputs']} outputs, {c['effects']} effects"
    else:
        text = f"{c['nodes']} items in {c['systems']} system{'s' if c['systems'] != 1 else ''}"
    if c.get("assumed"):
        text += f"; {c['assumed']} marked assumed"
    return text


def public_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The spec as the model may edit it (internal keys dropped)."""
    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if not str(k).startswith("_") and k != "order" and not (k == "code" and x == "")}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return strip(spec)


def rev_label(revision: int) -> str:
    return f"P{int(revision)}"


def drawing_number(drawing_id: str) -> str:
    return "SCH-" + drawing_id[:8].upper()


# ============================================================================================= the service
class Schematics:
    def __init__(self, j, today: Callable[[], date] | None = None) -> None:
        self.j = j
        self._today = today

    # -- time (injectable; never read at import)
    def today(self) -> date:
        if self._today is not None:
            return self._today()
        try:
            tz = ZoneInfo(getattr(self.j.settings, "timezone", "") or "Europe/London")
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("UTC")
        return datetime.now(tz).date()

    # -- storage
    @property
    def db(self):
        return self.j.db

    def save(self, kind: str, spec: dict[str, Any], *, drawing_id: str = "", note: str = "", by: str = "", role: str = "") -> dict[str, Any]:
        """Keep a validated spec: a new drawing (P1) or the next revision of ``drawing_id``. Blocked during a question check."""
        checkmode.guard("Saving a schematic")
        stored = json.dumps(spec, ensure_ascii=False, sort_keys=True)
        when = now_iso()
        day = self.today().isoformat()
        if drawing_id:
            row = self.get_row(drawing_id)
            if row is None:
                raise SchematicError([f"drawing_id: there's no saved drawing {clean_text(drawing_id, 20)} - leave drawing_id out to start a new one"])
            if row["kind"] != kind:
                raise SchematicError([f"kind: drawing {drawing_number(drawing_id)} is a {row['kind']} - a revision keeps the same kind"])
            revision = int(row["revision"]) + 1
            self.db.execute("UPDATE schematics SET updated_at = ?, title = ?, site = ?, system = ?, job_ref = ?, revision = ? WHERE id = ?",
                            (when, spec["title"], spec["site"], spec["system"], spec["job_ref"], revision, drawing_id))
        else:
            drawing_id = secrets.token_hex(6)
            revision = 1
            self.db.execute("INSERT INTO schematics (id, created_at, updated_at, kind, title, site, system, job_ref, revision, created_by, "
                            "created_role) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (drawing_id, when, when, kind, spec["title"], spec["site"], spec["system"], spec["job_ref"], 1, by, role))
        self.db.execute("INSERT INTO schematic_revisions (drawing_id, revision, created_at, drawn_on, created_by, note, spec_json) "
                        "VALUES (?,?,?,?,?,?,?)", (drawing_id, revision, when, day, by, clean_text(note, 160), stored))
        what = (f"{'Revised' if revision > 1 else 'Drew'} schematic {drawing_number(drawing_id)} rev {rev_label(revision)}: {spec['title']}"
                + (f" ({clean_text(note, 80)})" if note and revision > 1 else ""))
        self.audit(by, what, drawing_id)
        return self.meta(drawing_id, revision)

    def audit(self, actor: str, what: str, drawing_id: str) -> None:
        feed = getattr(self.j, "activity_feed", None)
        if feed is not None:
            feed.record("schematic", actor or "Jarvis", what, drawing_number(drawing_id))

    def get_row(self, drawing_id: str) -> dict[str, Any] | None:
        if not DRAWING_ID.match(str(drawing_id or "")):
            return None
        return self.db.query_one("SELECT * FROM schematics WHERE id = ?", (drawing_id,))

    def load(self, drawing_id: str, revision: int | None = None) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """(drawing row, revision row with the parsed spec) or None."""
        row = self.get_row(drawing_id)
        if row is None:
            return None
        rev = int(revision) if revision else int(row["revision"])
        r = self.db.query_one("SELECT * FROM schematic_revisions WHERE drawing_id = ? AND revision = ?", (drawing_id, rev))
        if r is None:
            return None
        r = dict(r)
        r["spec"] = json.loads(r.pop("spec_json"))
        return dict(row), r

    def meta(self, drawing_id: str, revision: int | None = None) -> dict[str, Any]:
        loaded = self.load(drawing_id, revision)
        if loaded is None:
            return {}
        row, rev = loaded
        return {"id": row["id"], "number": drawing_number(row["id"]), "kind": row["kind"], "kind_text": KIND_TEXT[row["kind"]],
                "title": rev["spec"]["title"], "site": rev["spec"]["site"], "system": rev["spec"]["system"],
                "job_ref": rev["spec"]["job_ref"], "revision": rev["revision"], "rev": rev_label(rev["revision"]),
                "latest_revision": int(row["revision"]), "date": rev["drawn_on"], "note": rev["note"], "created_by": row["created_by"],
                "summary": describe(row["kind"], rev["spec"])}

    def list(self, site: str = "", kind: str = "", query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 20), LIST_MAX))
        sql, params = "SELECT * FROM schematics WHERE 1=1", []
        if kind in KINDS:
            sql += " AND kind = ?"
            params.append(kind)
        if site:
            sql += " AND site LIKE ?"
            params.append(f"%{site.strip()[:60]}%")
        if query:
            sql += " AND (title LIKE ? OR site LIKE ? OR system LIKE ? OR job_ref LIKE ?)"
            params += [f"%{query.strip()[:60]}%"] * 4
        rows = self.db.query(sql + " ORDER BY updated_at DESC, id LIMIT ?", tuple(params + [limit]))
        return [{"id": r["id"], "number": drawing_number(r["id"]), "kind": r["kind"], "title": r["title"], "site": r["site"],
                 "system": r["system"], "job_ref": r["job_ref"], "rev": rev_label(r["revision"]), "revision": r["revision"],
                 "updated": r["updated_at"][:10], "created_by": r["created_by"]} for r in rows]

    def revisions(self, drawing_id: str) -> list[dict[str, Any]]:
        return [{"revision": r["revision"], "rev": rev_label(r["revision"]), "date": r["drawn_on"], "note": r["note"], "by": r["created_by"]}
                for r in self.db.query("SELECT revision, drawn_on, note, created_by FROM schematic_revisions WHERE drawing_id = ? "
                                       "ORDER BY revision", (drawing_id,))]

    # -- drawing
    @staticmethod
    def scene(kind: str, spec: dict[str, Any], mode: str = "wide") -> dict[str, Any]:
        return layout_mod.layout(kind, spec, mode)

    def view(self, drawing_id: str, revision: int | None = None, mode: str = "wide") -> dict[str, Any] | None:
        """What the console draws: the drawing's details and the scene with every symbol expanded into primitives."""
        loaded = self.load(drawing_id, revision)
        if loaded is None:
            return None
        row, rev = loaded
        sc = layout_mod.layout(row["kind"], rev["spec"], mode)
        meta = self.meta(drawing_id, rev["revision"])
        return {"drawing": meta, "mode": mode if mode in layout_mod.MODES else "wide",
                "scene": {"w": sc["w"], "h": sc["h"], "items": layout_mod.expand(sc)},
                "disclaimer": layout_mod.DISCLAIMER, "exports": export_links(drawing_id, rev["revision"])}

    def export(self, drawing_id: str, fmt: str, revision: int | None = None, paper: str = "a3", by: str = "") -> tuple[bytes, str, str] | None:
        """(bytes, mime type, file name) of one export, or None for an unknown drawing. Leaves an activity line."""
        from . import schematic_render as render

        loaded = self.load(drawing_id, revision)
        if loaded is None:
            return None
        row, rev = loaded
        sc = layout_mod.layout(row["kind"], rev["spec"], "wide")
        meta = self.meta(drawing_id, rev["revision"])
        meta["company"] = COMPANY
        paper = paper if paper in PAPERS else "a3"
        if fmt == "svg":
            data, mime = render.to_svg_sheet(sc, meta).encode("utf-8"), "image/svg+xml"
        elif fmt == "png":
            data, mime = render.to_png_sheet(sc, meta), "image/png"
        elif fmt == "pdf":
            data, mime = render.to_pdf(sc, meta, paper), "application/pdf"
        else:
            raise ValueError("format")
        slug = re.sub(r"[^a-z0-9]+", "-", meta["title"].lower()).strip("-")[:40] or "schematic"
        name = f"{drawing_number(drawing_id)}-{meta['rev']}-{slug}" + (f"-{paper}" if fmt == "pdf" else "") + f".{fmt}"
        self.audit(by, f"Exported schematic {drawing_number(drawing_id)} rev {meta['rev']} as {fmt.upper()}"
                       + (f" ({paper.upper()})" if fmt == "pdf" else ""), drawing_id)
        return data, mime, name


def export_links(drawing_id: str, revision: int) -> list[dict[str, str]]:
    base = f"/api/schematics/{drawing_id}/export"
    return [{"label": "SVG", "href": f"{base}/svg?rev={revision}"}, {"label": "PNG", "href": f"{base}/png?rev={revision}"},
            {"label": "PDF A4", "href": f"{base}/pdf?rev={revision}&paper=a4"},
            {"label": "PDF A3", "href": f"{base}/pdf?rev={revision}&paper=a3"}]
