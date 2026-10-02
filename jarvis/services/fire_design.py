"""Draft fire alarm design estimator (BS 5839-1 style coverage) for quoting.

Pure, deterministic, read-only: it takes a structured room list (which the model extracts from a described or
uploaded floorplan) and returns an ESTIMATED device count, a draft device schedule and a draft specification.

Everything it produces is a DRAFT for review by a competent fire alarm designer. It is not a design, not
certified, and never a substitute for the design certificate. Coverage figures are the ones in
knowledge/standards/fire-detection-and-alarm-bs5839.md, which are 2017-era and marked "verify" there; they must be
checked against the current edition before anything is issued. We deliberately cite NO clause numbers: they change
between editions and we are not confident enough in them to quote them. The standard is named by topic only.
"""

from __future__ import annotations

import math
import re

DRAFT_STATUS = "DRAFT - NOT A CERTIFIED DESIGN"
REVIEWER = "a competent fire alarm designer"
DISCLAIMER = (
    "DRAFT ESTIMATE FOR QUOTATION ONLY - NOT A CERTIFIED DESIGN. Device numbers are a desk-top estimate from the "
    "room information supplied. This must be reviewed, corrected and signed off by a competent fire alarm designer "
    "(site survey, fire risk assessment, fire strategy and the current edition of BS 5839-1) before it is quoted "
    "as final, ordered or installed. It is not a design certificate and does not show compliance with BS 5839-1."
)

CATEGORIES = ("M", "L1", "L2", "L3", "L4", "L5", "P1", "P2")

# Planning figures. The coverage and height limits are 2017-era values from the knowledge note, flagged to verify.
SMOKE_RADIUS_M = 7.5
HEAT_RADIUS_M = 5.3
SMOKE_MAX_HEIGHT_M = 10.5
HEAT_MAX_HEIGHT_M = 7.5  # conservative: Class A1 heat detectors are allowed higher (about 9 m)
VAD_RADIUS_M = 6.0  # assumes a ceiling VAD with a 12 m diameter coverage cylinder (EN 54-23 C-3-12 style)
VAD_MAX_HEIGHT_M = 3.0
MAX_ZONE_AREA_M2 = 2000.0
SMALL_BUILDING_M2 = 300.0
ISOLATOR_SPACING_DEVICES = 32  # commonly quoted devices between isolators - verify
LOOP_PLANNING_CAP = 100  # planning figure only: real loop capacity depends on the panel manufacturer
DEFAULT_EXITS_PER_FLOOR = 2
MCP_AREA_M2 = 800.0  # planning heuristic: one extra call point per this floor area beyond the exits
SOUNDER_AREA_M2 = 150.0  # planning heuristic only - sound levels must be confirmed by calculation / survey
SOUNDER_RUN_M = 25.0  # planning heuristic for corridors and stairs

ESCAPE_USES = {"corridor", "stair", "stairs", "stairwell", "lobby", "landing", "hall", "hallway", "circulation"}
LINEAR_USES = {"corridor", "stair", "stairs", "stairwell", "landing", "hallway", "circulation"}
HEAT_USES = {"kitchen", "plant", "boiler", "boiler room", "plant room", "laundry"}
SMALL_USES = {"toilet", "wc", "store", "cupboard", "riser", "shower"}
SMALL_ROOM_M2 = 10.0

ASSUMPTIONS_ALWAYS = [
    "Counts come from a simple spacing calculation on a flat ceiling; sloping/beamed ceilings, obstructions, "
    "voids (floor and ceiling) and partitions not shown in the room list are NOT assessed.",
    "Detector spacing uses 2017-era coverage radii (smoke 7.5 m, heat 5.3 m) and height limits; sounder and "
    "call point numbers are planning heuristics, not calculated audibility or travel distances.",
    "An addressable system with loop-powered devices is assumed. Small premises may suit a conventional system.",
    "Stairwell, lift shaft and other vertical-shaft zones, door holders, interfaces (lifts, AHUs, maglocks, "
    "suppression) and the cause and effect are not included.",
    "Cabling, containment, builder's work, programming, commissioning, certification and handover documents are "
    "not quantified; they need a site survey.",
]

VERIFY_ALWAYS = [
    "Which edition of BS 5839-1 the design is to be assessed against (2025 is current; coverage figures used "
    "here are 2017-era and must be checked against the edition used).",
    "Category (L1/L2/L3/L4/L5/M/P1/P2) and who specified it - fire risk assessment, building control, insurer.",
    "Detector coverage, siting and ceiling-height limits, including detection in voids.",
    "Sounder audibility levels (calculated or measured) and VAD need and coverage volumes (BS EN 54-23).",
    "Manual call point positions and travel distances to the nearest call point.",
    "Zoning, short-circuit isolator placement, loop capacity, standby battery calculation and cable grade.",
]


def _num(value, default=None):
    return default if value is None else float(value)


def _room(raw: dict, idx: int) -> dict:
    name = str(raw.get("name") or f"Room {idx + 1}")
    area, length, width = _num(raw.get("area_m2")), _num(raw.get("length_m")), _num(raw.get("width_m"))
    notes: list[str] = []
    if length and width:
        calc = length * width
        if area and abs(area - calc) > 0.15 * calc:
            notes.append(f"area_m2 {area:g} differs from length x width {calc:g}; the dimensions were used")
        area = calc
    elif area:
        side = math.sqrt(area)
        length = width = side
        notes.append("dimensions not given: assumed square, so long thin spaces may be under-counted")
    else:
        raise ValueError(f"Room '{name}': give area_m2, or both length_m and width_m.")
    if area <= 0:
        raise ValueError(f"Room '{name}': area must be above zero.")
    use = str(raw.get("use") or "room").strip().lower()
    return {
        "name": name, "floor": str(raw.get("floor") or "Ground"), "use": use, "area": area,
        "length": max(length, width), "width": min(length, width),
        "height": _num(raw.get("ceiling_height_m"), 2.7),
        "escape": bool(raw.get("escape_route")) or use in ESCAPE_USES,
        "opens": bool(raw.get("opens_onto_escape_route")),
        "high_risk": bool(raw.get("high_risk")), "sleeping": bool(raw.get("sleeping")) or use in {"bedroom", "sleeping"},
        "needs_vad": bool(raw.get("needs_vad")), "detector_type": raw.get("detector_type"), "notes": notes,
    }


def _needs_detection(category: str, r: dict) -> bool:
    if category == "M":
        return False
    if category in ("L1", "P1"):
        return True
    if category == "L2":
        return r["escape"] or r["opens"] or r["high_risk"]
    if category == "L3":
        return r["escape"] or r["opens"]
    if category == "L4":
        return r["escape"]
    return r["high_risk"]  # L5 / P2: only the rooms the designer / specifier has flagged


def _grid_count(length: float, width: float, radius: float) -> int:
    """Detectors needed so no point of a length x width rectangle is further than `radius` from one."""
    length, width = max(length, width), min(length, width)
    across = max(1, math.ceil(width / (radius * math.sqrt(2))))
    cell_w = width / across
    along = 2 * math.sqrt(max(radius ** 2 - (cell_w / 2) ** 2, 0.0))
    return across * max(1, math.ceil(length / along))


def _detector_type(r: dict) -> str:
    if r["detector_type"] in ("smoke", "heat", "multi"):
        return r["detector_type"]
    return "heat" if r["use"] in HEAT_USES else "smoke"


def _sounders(r: dict) -> int:
    if r["use"] in SMALL_USES and r["area"] < SMALL_ROOM_M2:
        return 0  # assumed audible from the adjacent space - to be proved by survey
    if r["sleeping"]:
        return 1  # one per sleeping room (e.g. sounder base) so the bedhead level can be met
    if r["use"] in LINEAR_USES:
        return max(1, math.ceil(r["length"] / SOUNDER_RUN_M))
    return max(1, math.ceil(r["area"] / SOUNDER_AREA_M2))


def _assess_room(category: str, r: dict, vads_throughout: bool) -> dict:
    flags = list(r["notes"])
    dtype, detectors = None, 0
    beam = False
    if _needs_detection(category, r):
        dtype = _detector_type(r)
        limit = HEAT_MAX_HEIGHT_M if dtype == "heat" else SMOKE_MAX_HEIGHT_M
        if r["height"] > limit:
            beam, dtype = True, "beam/aspirating (designer to specify)"
            flags.append(f"ceiling {r['height']:g} m is above the point-detector limit ({limit:g} m): "
                         "beam, aspirating or other detection needed - not counted")
        else:
            detectors = _grid_count(r["length"], r["width"], HEAT_RADIUS_M if dtype == "heat" else SMOKE_RADIUS_M)
        if r["sleeping"] and dtype == "heat":
            flags.append("heat detection in a sleeping room: check against the 2025 edition's sleeping-room guidance")
    if r["sleeping"] and category in ("M", "L3", "L4", "L5", "P1", "P2"):
        flags.append("sleeping accommodation in a category that may not give enough warning - confirm the category")
    vads = 0
    if r["needs_vad"] or vads_throughout:
        vads = _grid_count(r["length"], r["width"], VAD_RADIUS_M)
        if r["height"] > VAD_MAX_HEIGHT_M:
            flags.append(f"ceiling {r['height']:g} m: choose a VAD whose coverage height suits it (EN 54-23)")
    return {"name": r["name"], "floor": r["floor"], "use": r["use"], "area_m2": round(r["area"], 1),
            "ceiling_height_m": r["height"], "detection_required": dtype is not None, "detector_type": dtype,
            "detectors": detectors, "beam_or_aspirating": beam, "sounders": _sounders(r), "vads": vads,
            "flags": flags}


def design(project: str, category: str, rooms: list[dict], exits_by_floor: dict | None = None,
           vads_throughout: bool = False, category_specified_by: str | None = None) -> dict:
    """Estimate devices and build the draft schedule + specification. Raises ValueError on unusable input."""
    category = (category or "").strip().upper()
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category '{category}'. Use one of: {', '.join(CATEGORIES)}.")
    if not rooms:
        raise ValueError("No rooms supplied - list each room or area with its floor and size.")
    parsed = [_room(raw, i) for i, raw in enumerate(rooms)]
    assessed = [_assess_room(category, r, vads_throughout) for r in parsed]

    warnings: list[str] = []
    assumptions = list(ASSUMPTIONS_ALWAYS)
    if category == "L5":
        warnings.append("L5 is an engineered category needing a written specification; only rooms flagged "
                        "high_risk were counted. The designer must define the coverage.")
    if category == "P2":
        warnings.append("P2 covers only defined areas; only rooms flagged high_risk were counted.")
    if not category_specified_by:
        assumptions.append("Who specified the category was not given; it is assumed, not confirmed.")

    floors: dict[str, dict] = {}
    for r, a in zip(parsed, assessed):
        f = floors.setdefault(r["floor"], {"area": 0.0, "smoke": 0, "heat": 0, "multi": 0, "beam_rooms": 0,
                                           "sounders": 0, "vads": 0})
        f["area"] += r["area"]
        f["sounders"] += a["sounders"]
        f["vads"] += a["vads"]
        if a["beam_or_aspirating"]:
            f["beam_rooms"] += 1
        elif a["detector_type"]:
            f[a["detector_type"]] += a["detectors"]

    exits_by_floor = {str(k): max(1, int(v)) for k, v in (exits_by_floor or {}).items()}
    assumed_exits = [fl for fl in floors if fl not in exits_by_floor]
    if assumed_exits:
        assumptions.append(f"Final exits per floor not given for {', '.join(assumed_exits)}: assumed "
                           f"{DEFAULT_EXITS_PER_FLOOR} each.")
    total_area = sum(f["area"] for f in floors.values())

    schedule: list[dict] = []
    totals = {"smoke": 0, "heat": 0, "multi": 0, "mcp": 0, "sounder": 0, "vad": 0, "isolator": 0, "zones": 0}
    loop_devices = 0
    zone_rows = []
    for name, f in floors.items():
        exits = exits_by_floor.get(name, DEFAULT_EXITS_PER_FLOOR)
        mcp = max(exits, math.ceil(f["area"] / MCP_AREA_M2))
        zones = 1 if total_area <= SMALL_BUILDING_M2 else max(1, math.ceil(f["area"] / MAX_ZONE_AREA_M2))
        dev = f["smoke"] + f["heat"] + f["multi"] + mcp + f["sounders"] + f["vads"]
        iso = max(zones, math.ceil(dev / ISOLATOR_SPACING_DEVICES))
        f.update(mcp=mcp, zones=zones, isolators=iso)
        zone_rows.append({"floor": name, "area_m2": round(f["area"], 1), "zones": zones})
        totals["zones"] += zones
        loop_devices += dev + iso
        for key, item, basis in (
            ("smoke", "Addressable optical smoke detector and base", "spacing calculation, smoke radius 7.5 m"),
            ("heat", "Addressable heat detector and base", "spacing calculation, heat radius 5.3 m"),
            ("multi", "Addressable multi-sensor detector and base", "spacing calculation, smoke radius 7.5 m"),
            ("mcp", "Addressable manual call point (resettable, EN 54-11)", "exits plus floor area heuristic"),
            ("sounders", "Sounder / sounder base (EN 54-3)", "planning heuristic - audibility to be confirmed"),
            ("vads", "Visual alarm device (EN 54-23)", "where flagged or specified - coverage volume to be confirmed"),
            ("isolators", "Short-circuit isolator (EN 54-17)", f"1 per zone / {ISOLATOR_SPACING_DEVICES} devices"),
        ):
            qty = f[key]
            if qty:
                schedule.append({"floor": name, "item": item, "qty": qty, "basis": basis, "notes": "DRAFT estimate"})
                short = {"sounders": "sounder", "vads": "vad", "isolators": "isolator"}.get(key, key)
                totals[short] += qty
        if f["beam_rooms"]:
            schedule.append({"floor": name, "item": "High-ceiling detection (beam / aspirating) - TBC",
                             "qty": f["beam_rooms"], "basis": "rooms over point-detector height limit",
                             "notes": "unit = rooms; designer to specify and size"})

    if total_area <= SMALL_BUILDING_M2:  # small building: one zone across all floors
        totals["zones"] = 1
        zone_rows = [{"floor": "All floors", "area_m2": round(total_area, 1), "zones": 1}]

    loops = max(1, math.ceil(loop_devices / LOOP_PLANNING_CAP))
    project_lines = [
        ("Fire alarm control and indicating equipment (EN 54-2), addressable", 1, "one panel assumed"),
        (f"Loop capacity (planning figure {LOOP_PLANNING_CAP} devices per loop; confirm with manufacturer)", loops,
         f"{loop_devices} loop devices estimated"),
        ("Power supply / standby batteries", 1, "battery calculation required - designer / manufacturer calculator"),
        ("Alarm transmission to ARC (EN 54-21)", 0, "only if required - confirm with client; qty TBC"),
        ("Fire-resisting cable, containment and fixings", 0, "quantity from site survey, not estimated"),
        ("Zone chart, logbook, as-fitted drawings, O&M and certificates", 1, "handover pack"),
    ]
    for item, qty, basis in project_lines:
        schedule.append({"floor": "Project", "item": item, "qty": qty, "basis": basis, "notes": "TBC by designer"})

    result = {
        "status": DRAFT_STATUS, "draft": True, "certified": False, "requires_review_by": REVIEWER,
        "disclaimer": DISCLAIMER, "project": project, "category": category,
        "category_specified_by": category_specified_by or "not stated",
        "standard_reference": "BS 5839-1 (edition to be confirmed by the designer). No clause numbers are cited "
                              "because they have not been verified against the purchased standard.",
        "summary": {"total_area_m2": round(total_area, 1), "rooms": len(assessed), "loops_estimated": loops,
                    "devices": {k: v for k, v in totals.items()}},
        "zones": zone_rows, "rooms": assessed, "schedule": schedule,
        "assumptions": assumptions, "verify": list(VERIFY_ALWAYS), "warnings": warnings,
    }
    result["specification_markdown"] = build_spec(result)
    return result


def build_spec(r: dict) -> str:
    """Draft specification text for a quote, bannered as a draft at both ends."""
    d = r["summary"]["devices"]
    banner = f"**{DRAFT_STATUS}** - for review by {REVIEWER}"
    lines = [
        banner, "", f"# Draft fire alarm specification - {r['project']}", "",
        "## 1. Basis", f"- System category: {r['category']} (specified by: {r['category_specified_by']}).",
        "- The system is to be designed, installed, commissioned and handed over in accordance with the "
        "recommendations of BS 5839-1 (edition to be confirmed). Variations, if any, are to be agreed and recorded.",
        "- Equipment is to be BS EN 54 compliant and from a compatible, approved range (EN 54-13).", "",
        "## 2. Scope (estimated quantities, to be confirmed)",
        f"- Addressable control and indicating equipment: 1 ({r['summary']['loops_estimated']} loop(s) estimated).",
        f"- Automatic detectors: {d['smoke']} optical smoke, {d['heat']} heat, {d['multi']} multi-sensor.",
        f"- Manual call points: {d['mcp']}.", f"- Sounders: {d['sounder']}; visual alarm devices: {d['vad']}.",
        f"- Short-circuit isolators: {d['isolator']}; estimated zones: {d['zones']}.",
        "- High-ceiling spaces needing beam/aspirating detection are listed separately in the schedule.", "",
        "## 3. Performance requirements to be confirmed by the designer",
        "- Audibility levels at occupied locations (including at sleeping positions where relevant).",
        "- Visual alarm device coverage where specified.", "- Cause and effect, to be agreed with the client and "
        "other trades and proved at commissioning.",
        "- Standby power supply duration and battery calculation.", "- Cable grade (standard or enhanced "
        "fire-resisting) and fixings.", "",
        "## 4. Certification and handover",
        "- Design, installation and commissioning certificates, each signed by a competent person for that stage.",
        "- Zone plan, as-fitted drawings, cause and effect matrix, battery calculation, logbook and user training.", "",
        "## 5. Assumptions", *[f"- {a}" for a in r["assumptions"]], "",
        "## 6. To be verified before issue", *[f"- {v}" for v in r["verify"]],
    ]
    if r["warnings"]:
        lines += ["", "## 7. Warnings", *[f"- {w}" for w in r["warnings"]]]
    lines += ["", banner, DISCLAIMER]
    return "\n".join(lines)


def cites_clause_numbers(text: str) -> bool:
    """True if the text contains something that looks like a clause citation (we deliberately cite none)."""
    return bool(re.search(r"\b(clause|cl\.|section)\s+\d", text, re.IGNORECASE))
