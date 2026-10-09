"""The symbol set for system schematics (services/schematics.py): ONE module so it can be reconciled with any other drawing feature.

These are Salts' own clear, consistent drawing symbols for fire and security line diagrams. They are NOT a formal standard (no
BS / EN / IEC symbol set is claimed) - each one is a simple shape with a short code, explained in the legend printed on every
drawing. This is also the symbol set of the floor-plan drawings (device layouts and zone charts, services/plan_drawings.py: its
``DEVICE_TYPES`` are a subset of these keys and it draws these primitives in the editor and its exports), so ONE definition serves
both. Keep every symbol HERE, nowhere else.)

A symbol is DATA: a list of primitives in a unit box from -1 to 1 on both axes (y grows downwards), drawn centred on a point at a
given size by ``expand()``. The same primitives are rendered by every output - the console's SVG (web/schematics.js only draws
the expanded primitives it is sent), the exported SVG, PNG and PDF - so a symbol looks the same everywhere.

Primitive shapes (unit box)::

    {"t": "circle", "cx", "cy", "r"}                    {"t": "rect", "x", "y", "w", "h", "rx"?}
    {"t": "line", "x1", "y1", "x2", "y2"}               {"t": "poly", "pts": [[x, y], ...], "z": closed?}
    {"t": "text", "x", "y", "s": "S", "fs": size, "w": weight}   (fs is in unit-box units: 1.0 = half the symbol's size)

Optional on any primitive: ``"f"`` a fill role (paper | soft | ink | accent; default none for lines, paper for closed shapes) and
``"c"`` a stroke role (default the symbol's own colour). Colour ROLES, never colours: the console maps them to its theme tokens
(light / dark) and the exports to print colours.
"""

from __future__ import annotations

import math
from typing import Any

# --------------------------------------------------------------------------------------------- building blocks (unit box)


def _circle(r: float = 0.92, f: str = "paper", **kw: Any) -> dict[str, Any]:
    return {"t": "circle", "cx": kw.pop("cx", 0.0), "cy": kw.pop("cy", 0.0), "r": r, "f": f, **kw}


def _box(w: float = 1.84, h: float = 1.84, f: str = "paper", rx: float = 0.08, **kw: Any) -> dict[str, Any]:
    return {"t": "rect", "x": kw.pop("x", -w / 2), "y": kw.pop("y", -h / 2), "w": w, "h": h, "rx": rx, "f": f, **kw}


def _code(text: str, fs: float | None = None, y: float = 0.0, w: int = 600, **kw: Any) -> dict[str, Any]:
    size = fs if fs is not None else {1: 1.0, 2: 0.86, 3: 0.66, 4: 0.52}.get(len(text), 0.46)
    return {"t": "text", "x": kw.pop("x", 0.0), "y": y, "s": text, "fs": size, "w": w, **kw}


def _arc(cx: float, cy: float, r: float, a0: float, a1: float, n: int = 10) -> list[list[float]]:
    """Points along a circular arc, angles in degrees (0 = right, 90 = down)."""
    return [[round(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)), 4),
             round(cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n)), 4)] for i in range(n + 1)]


def _rays(r0: float, r1: float, n: int = 8) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        a = math.radians(22.5 + i * 360 / n)
        out.append({"t": "line", "x1": round(r0 * math.cos(a), 4), "y1": round(r0 * math.sin(a), 4),
                    "x2": round(r1 * math.cos(a), 4), "y2": round(r1 * math.sin(a), 4)})
    return out


def _detector(code: str) -> list[dict[str, Any]]:
    return [_circle(), _code(code)]


def _module(code: str) -> list[dict[str, Any]]:
    return [_box(), _code(code)]


def _wide(code: str) -> list[dict[str, Any]]:
    return [_box(1.96, 1.2, rx=0.1), _code(code, fs=0.62 if len(code) <= 3 else 0.5)]


_SPEAKER = {"t": "poly", "pts": [[-0.8, -0.34], [-0.36, -0.34], [0.2, -0.86], [0.2, 0.86], [-0.36, 0.34], [-0.8, 0.34]], "z": True, "f": "paper"}


# --------------------------------------------------------------------------------------------- the set
# key -> {"label": legend text, "family": fire | security | network | common, "items": [...]}
SYMBOLS: dict[str, dict[str, Any]] = {
    # ---- fire detection and alarm
    "smoke": {"label": "Smoke detector", "family": "fire", "items": _detector("S")},
    "heat": {"label": "Heat detector", "family": "fire", "items": _detector("H")},
    "multi": {"label": "Multi-sensor detector", "family": "fire", "items": _detector("M")},
    "co": {"label": "CO (carbon monoxide) fire detector", "family": "fire", "items": _detector("CO")},
    "flame": {"label": "Flame detector", "family": "fire", "items": _detector("F")},
    "beam": {"label": "Optical beam detector", "family": "fire",
             "items": [_box(1.96, 1.2, rx=0.1), _code("B", fs=0.62, x=-0.5),
                       {"t": "line", "x1": -0.12, "y1": 0.0, "x2": 0.7, "y2": 0.0},
                       {"t": "poly", "pts": [[0.5, -0.2], [0.8, 0.0], [0.5, 0.2]], "z": True, "f": "ink"}]},
    "asd": {"label": "Aspirating smoke detector (ASD)", "family": "fire", "items": _wide("ASD")},
    "mcp": {"label": "Manual call point", "family": "fire",
            "items": [_box(1.84, 1.84, rx=0.04), _circle(0.42, f="accent", c="accent")]},
    "sounder": {"label": "Sounder", "family": "fire",
                "items": [_SPEAKER, {"t": "poly", "pts": _arc(0.2, 0.0, 0.42, -50, 50, 6)},
                          {"t": "poly", "pts": _arc(0.2, 0.0, 0.7, -50, 50, 6)}]},
    "vad": {"label": "Visual alarm device (beacon)", "family": "fire",
            "items": [_circle(0.5, f="accent", c="accent"), *_rays(0.66, 0.98)]},
    "sounder_vad": {"label": "Sounder with visual alarm (beacon)", "family": "fire",
                    "items": [_SPEAKER, _circle(0.26, cx=0.62, cy=0.0, f="accent", c="accent"),
                              {"t": "line", "x1": 0.62, "y1": -0.42, "x2": 0.62, "y2": -0.82},
                              {"t": "line", "x1": 0.62, "y1": 0.42, "x2": 0.62, "y2": 0.82},
                              {"t": "line", "x1": 0.96, "y1": 0.0, "x2": 0.9, "y2": 0.0}]},
    "io": {"label": "Input / output module", "family": "fire", "items": _module("I/O")},
    "input": {"label": "Input module", "family": "fire", "items": _module("IN")},
    "output": {"label": "Output (relay) module", "family": "fire", "items": _module("OUT")},
    "interface": {"label": "Interface unit", "family": "fire", "items": _module("IF")},
    "zone_module": {"label": "Conventional zone module", "family": "fire", "items": _module("ZM")},
    "door_holder": {"label": "Door holder / door release", "family": "fire", "items": _module("DH")},
    "sprinkler_flow": {"label": "Sprinkler flow / tamper switch", "family": "fire", "items": _module("FS")},
    "isolator": {"label": "Short-circuit isolator", "family": "fire",
                 "items": [_box(1.84, 1.1, rx=0.06), {"t": "line", "x1": -0.92, "y1": 0.55, "x2": 0.92, "y2": -0.55},
                           _code("I", fs=0.5, x=-0.5, y=-0.1, w=700)]},
    "eol": {"label": "End-of-line device (EOL)", "family": "fire",
            "items": [{"t": "poly", "pts": [[-0.95, 0], [-0.7, 0], [-0.55, -0.45], [-0.3, 0.45], [-0.05, -0.45], [0.2, 0.45],
                                            [0.45, -0.45], [0.6, 0], [0.95, 0]]}]},
    "repeater": {"label": "Repeater / remote display", "family": "fire", "items": _wide("RPT")},
    "panel": {"label": "Control panel (on network)", "family": "fire", "items": _wide("CIE")},
    "graphics": {"label": "Graphics / PC front end", "family": "fire",
                 "items": [_box(1.9, 1.3, rx=0.06, y=-0.85), {"t": "line", "x1": 0, "y1": 0.45, "x2": 0, "y2": 0.75},
                           {"t": "line", "x1": -0.45, "y1": 0.8, "x2": 0.45, "y2": 0.8}, _code("PC", fs=0.5, y=-0.2)]},
    # ---- security: CCTV
    "nvr": {"label": "Network video recorder (NVR)", "family": "security", "items": _wide("NVR")},
    "dvr": {"label": "Digital video recorder (DVR)", "family": "security", "items": _wide("DVR")},
    "camera": {"label": "Camera (fixed)", "family": "security",
               "items": [{"t": "rect", "x": -0.95, "y": -0.42, "w": 1.25, "h": 0.84, "rx": 0.08, "f": "paper"},
                         {"t": "poly", "pts": [[0.3, -0.24], [0.95, -0.52], [0.95, 0.52], [0.3, 0.24]], "z": True, "f": "paper"}]},
    "ptz": {"label": "PTZ / dome camera", "family": "security",
            "items": [{"t": "poly", "pts": [[-0.9, -0.2], *_arc(0, -0.2, 0.9, 180, 0, 10)[1:-1], [0.9, -0.2]], "z": True, "f": "paper"},
                      {"t": "line", "x1": -0.95, "y1": -0.2, "x2": 0.95, "y2": -0.2}, _code("PTZ", fs=0.42, y=0.18)]},
    "monitor": {"label": "Monitor / viewing station", "family": "security",
                "items": [_box(1.9, 1.3, rx=0.06, y=-0.85), {"t": "line", "x1": 0, "y1": 0.45, "x2": 0, "y2": 0.75},
                          {"t": "line", "x1": -0.45, "y1": 0.8, "x2": 0.45, "y2": 0.8}]},
    # ---- network
    "switch": {"label": "Network switch", "family": "network",
               "items": [_box(1.96, 1.0, rx=0.08), _code("SW", fs=0.46, y=-0.12),
                         *[{"t": "rect", "x": -0.7 + i * 0.4, "y": 0.18, "w": 0.2, "h": 0.18, "f": "ink"} for i in range(4)]]},
    "poe_switch": {"label": "PoE network switch", "family": "network",
                   "items": [_box(1.96, 1.0, rx=0.08), _code("PoE", fs=0.42, y=-0.12),
                             *[{"t": "rect", "x": -0.7 + i * 0.4, "y": 0.18, "w": 0.2, "h": 0.18, "f": "ink"} for i in range(4)]]},
    "router": {"label": "Router / firewall", "family": "network", "items": [_circle(), _code("RTR", fs=0.5)]},
    "server": {"label": "Server / workstation", "family": "network", "items": _wide("SRV")},
    # ---- security: access control
    "controller": {"label": "Access controller", "family": "security", "items": _wide("ACU")},
    "reader": {"label": "Card / fob reader", "family": "security",
               "items": [{"t": "rect", "x": -0.55, "y": -0.95, "w": 1.1, "h": 1.9, "rx": 0.12, "f": "paper"},
                         {"t": "poly", "pts": _arc(0, 0.1, 0.22, 200, 340, 6)}, {"t": "poly", "pts": _arc(0, 0.1, 0.42, 205, 335, 6)},
                         {"t": "line", "x1": -0.25, "y1": 0.6, "x2": 0.25, "y2": 0.6}]},
    "keypad": {"label": "Keypad", "family": "security",
               "items": [_box(1.6, 1.84, rx=0.1),
                         *[{"t": "circle", "cx": -0.42 + c * 0.42, "cy": -0.5 + r * 0.42, "r": 0.1, "f": "ink"}
                           for r in range(3) for c in range(3)]]},
    "lock": {"label": "Lock (maglock / strike)", "family": "security",
             "items": [{"t": "poly", "pts": _arc(0, -0.15, 0.42, 180, 360, 8)},
                       {"t": "rect", "x": -0.7, "y": -0.15, "w": 1.4, "h": 1.05, "rx": 0.1, "f": "paper"},
                       {"t": "circle", "cx": 0, "cy": 0.32, "r": 0.13, "f": "ink"}]},
    "exit_button": {"label": "Request-to-exit button", "family": "security", "items": [_box(1.84, 1.84, rx=0.1), _circle(0.55), _code("EX", fs=0.42)]},
    "door_contact": {"label": "Door contact", "family": "security",
                     "items": [{"t": "rect", "x": -0.95, "y": -0.36, "w": 0.8, "h": 0.72, "rx": 0.06, "f": "paper"},
                               {"t": "rect", "x": 0.15, "y": -0.36, "w": 0.8, "h": 0.72, "rx": 0.06, "f": "soft"}]},
    "intercom": {"label": "Door entry / intercom station", "family": "security", "items": _module("INT")},
    # ---- security: intruder alarm
    "intruder_panel": {"label": "Intruder alarm panel", "family": "security", "items": _wide("IAS")},
    "expander": {"label": "Zone / bus expander", "family": "security", "items": _wide("EXP")},
    "pir": {"label": "PIR movement detector", "family": "security",
            "items": [{"t": "poly", "pts": [[0, -0.75], *_arc(0, -0.75, 1.5, 55, 125, 8)[1:-1], [0, -0.75]], "z": True, "f": "paper"},
                      {"t": "line", "x1": 0, "y1": -0.75, "x2": 0, "y2": 0.7}, _code("PIR", fs=0.34, y=0.2, x=0.0)]},
    "dual_tech": {"label": "Dual-technology detector", "family": "security",
                  "items": [{"t": "poly", "pts": [[0, -0.75], *_arc(0, -0.75, 1.5, 55, 125, 8)[1:-1], [0, -0.75]], "z": True, "f": "paper"},
                            _code("DT", fs=0.4, y=0.3)]},
    "shock": {"label": "Shock / vibration sensor", "family": "security", "items": [_circle(), _code("SH", fs=0.62)]},
    "glass_break": {"label": "Glass-break detector", "family": "security", "items": [_circle(), _code("GB", fs=0.62)]},
    "panic_button": {"label": "Personal attack / hold-up button", "family": "security", "items": [_box(1.84, 1.84, rx=0.5), _code("PA", fs=0.62)]},
    "bellbox": {"label": "External sounder (bell box)", "family": "security",
                "items": [_box(1.6, 1.84, rx=0.1), _code("BB", fs=0.55, y=-0.25), {"t": "poly", "pts": _arc(0, 0.35, 0.35, 200, 340, 6)}]},
    # ---- signalling and power (both)
    "stu": {"label": "Alarm signalling transmitter (STU / dual-path)", "family": "common", "items": _wide("STU")},
    "arc": {"label": "Alarm receiving centre (ARC)", "family": "common",
            "items": [{"t": "poly", "pts": [[-0.9, -0.15], [0, -0.92], [0.9, -0.15]], "z": True, "f": "paper"},
                      {"t": "rect", "x": -0.72, "y": -0.15, "w": 1.44, "h": 1.05, "f": "paper"}, _code("ARC", fs=0.44, y=0.4)]},
    "psu": {"label": "Power supply unit (PSU)", "family": "common", "items": _wide("PSU")},
    "other": {"label": "Other device (code shown)", "family": "common", "items": [_box(1.84, 1.84, rx=0.3)]},
}

# Device types that are placed INLINE on a fire loop's cable rather than as a device with an address (they still may carry one).
INLINE_TYPES = frozenset({"isolator"})

FIRE_DEVICE_TYPES = ("smoke", "heat", "multi", "co", "flame", "beam", "asd", "mcp", "sounder", "vad", "sounder_vad", "io", "input",
                     "output", "interface", "zone_module", "door_holder", "sprinkler_flow", "isolator", "other")
FIRE_NETWORK_TYPES = ("repeater", "panel", "graphics", "interface", "io", "other")
SECURITY_NODE_TYPES = ("nvr", "dvr", "camera", "ptz", "monitor", "switch", "poe_switch", "router", "server", "controller", "reader",
                       "keypad", "lock", "exit_button", "door_contact", "intercom", "intruder_panel", "expander", "pir", "dual_tech",
                       "shock", "glass_break", "panic_button", "bellbox", "beam", "stu", "arc", "psu", "smoke", "other")

# Words people (and models) commonly use for a type -> the key. Matching is on a normalised form (lower case, _ for spaces / dashes).
ALIASES: dict[str, str] = {
    "smoke_detector": "smoke", "optical": "smoke", "optical_smoke": "smoke", "ionisation": "smoke", "heat_detector": "heat",
    "rate_of_rise": "heat", "fixed_heat": "heat", "multisensor": "multi", "multi_sensor": "multi", "multi_criteria": "multi",
    "optical_heat": "multi", "co_detector": "co", "flame_detector": "flame", "beam_detector": "beam", "optical_beam": "beam",
    "aspirating": "asd", "aspirating_detector": "asd", "manual_call_point": "mcp", "call_point": "mcp", "break_glass": "mcp",
    "callpoint": "mcp", "bell": "sounder", "siren": "sounder", "beacon": "vad", "strobe": "vad", "flasher": "vad",
    "sounder_beacon": "sounder_vad", "sounder_strobe": "sounder_vad", "io_module": "io", "i/o": "io", "input_output": "io",
    "input_module": "input", "output_module": "output", "relay": "output", "relay_module": "output", "interface_unit": "interface",
    "door_release": "door_holder", "door_retainer": "door_holder", "flow_switch": "sprinkler_flow", "isolator_module": "isolator",
    "short_circuit_isolator": "isolator", "end_of_line": "eol", "repeater_panel": "repeater", "remote_display": "repeater",
    "network_panel": "panel", "sub_panel": "panel", "pc": "graphics", "front_end": "graphics", "bullet_camera": "camera",
    "turret_camera": "camera", "dome_camera": "ptz", "dome": "ptz", "ptz_camera": "ptz", "recorder": "nvr", "network_switch": "switch",
    "poe": "poe_switch", "firewall": "router", "workstation": "server", "access_controller": "controller", "acu": "controller",
    "door_controller": "controller", "card_reader": "reader", "fob_reader": "reader", "proximity_reader": "reader", "maglock": "lock",
    "mag_lock": "lock", "strike": "lock", "electric_strike": "lock", "rex": "exit_button", "request_to_exit": "exit_button",
    "push_to_exit": "exit_button", "exit": "exit_button", "contact": "door_contact", "magnetic_contact": "door_contact",
    "door_entry": "intercom", "intruder": "intruder_panel", "alarm_panel": "intruder_panel", "control_panel": "intruder_panel",
    "zone_expander": "expander", "rio": "expander", "pir_detector": "pir", "movement_detector": "pir", "motion": "pir",
    "dualtech": "dual_tech", "dual_technology": "dual_tech", "vibration": "shock", "glassbreak": "glass_break", "pa": "panic_button",
    "pa_button": "panic_button", "hold_up": "panic_button", "panic": "panic_button", "external_sounder": "bellbox", "bell_box": "bellbox",
    "communicator": "stu", "dual_path": "stu", "dualcom": "stu", "signalling": "stu", "transmitter": "stu", "alarm_receiving_centre": "arc",
    "power_supply": "psu",
}


def norm_key(raw: Any) -> str:
    return "_".join(str(raw or "").strip().lower().replace("-", " ").replace("/", " ").split())


def resolve(raw: Any, allowed: tuple[str, ...]) -> str | None:
    """The symbol key for a type the spec gave, if it is one of ``allowed`` (directly or through an alias); else None."""
    key = norm_key(raw)
    key = ALIASES.get(key, key)
    if str(raw or "").strip().lower() == "i/o":
        key = "io"
    return key if key in allowed else None


def label(key: str) -> str:
    return SYMBOLS.get(key, SYMBOLS["other"])["label"]


def expand(key: str, cx: float, cy: float, size: float, colour: str = "ink", dashed: bool = False,
           code: str = "") -> list[dict[str, Any]]:
    """The symbol ``key`` as absolute primitives centred on (cx, cy), ``size`` across. ``colour`` is the stroke role for the whole
    symbol (``assumed`` greys it); ``code`` is the short text an ``other`` symbol shows."""
    h = size / 2.0
    out: list[dict[str, Any]] = []
    items = list(SYMBOLS.get(key, SYMBOLS["other"])["items"])
    if key == "other" or key not in SYMBOLS:
        items.append(_code((code or "?")[:4]))
    sw = max(1.0, round(size / 22.0, 2))
    for it in items:
        t = it["t"]
        stroke = colour if colour == "assumed" else it.get("c", colour)
        fill = it.get("f")
        if colour == "assumed" and fill in ("ink", "accent"):
            fill = "assumed"
        base = {"c": stroke, "sw": sw, "d": 1 if dashed else 0}
        if t == "circle":
            out.append({"t": "circle", "cx": r2(cx + it["cx"] * h), "cy": r2(cy + it["cy"] * h), "r": r2(it["r"] * h), "f": fill, **base})
        elif t == "rect":
            out.append({"t": "rect", "x": r2(cx + it["x"] * h), "y": r2(cy + it["y"] * h), "w": r2(it["w"] * h), "h": r2(it["h"] * h),
                        "rx": r2(it.get("rx", 0) * h), "f": fill, **base})
        elif t == "line":
            out.append({"t": "line", "x1": r2(cx + it["x1"] * h), "y1": r2(cy + it["y1"] * h), "x2": r2(cx + it["x2"] * h),
                        "y2": r2(cy + it["y2"] * h), **base})
        elif t == "poly":
            out.append({"t": "poly", "pts": [[r2(cx + x * h), r2(cy + y * h)] for x, y in it["pts"]], "z": bool(it.get("z")),
                        "f": fill if it.get("z") else None, **base})
        elif t == "text":
            fs = r2(it["fs"] * h)
            out.append({"t": "text", "x": r2(cx + it.get("x", 0) * h), "y": r2(cy + it.get("y", 0) * h + fs * 0.36), "s": it["s"],
                        "fs": fs, "a": "middle", "w": it.get("w", 600), "c": "assumed" if colour == "assumed" else "ink"})
    return out


def r2(v: float) -> float:
    return round(float(v), 2)
