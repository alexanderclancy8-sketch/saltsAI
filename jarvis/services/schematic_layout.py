"""Deterministic layout for system schematics: a VALIDATED spec (services/schematics.py) in, a scene of plain primitives out.

The model never places anything: it writes the spec (what is connected to what, in which order), and this module works out every
coordinate. The same spec always gives the same scene (no clock, no randomness, no dict-order surprises), so a drawing can be
re-exported or revised and come out the same.

A scene is ``{"w", "h", "items", "breaks", "header_h", "symbols"}``:

* ``items`` - primitives in drawing units (1 unit = 1 CSS pixel on screen, scaled to the page for PDF):
  ``line`` (x1 y1 x2 y2), ``rect`` (x y w h rx f), ``circle`` (cx cy r f), ``poly`` (pts z f), ``text`` (x y s fs a w rot) and ``sym``
  (k x y sz code) - a symbol from services/schematic_symbols.py, expanded by the renderers. Every item has a colour ROLE ``c``
  (ink | muted | accent | assumed | line), a stroke width ``sw`` and ``d`` (1 = dashed). Fills are roles too (paper | soft | ink | accent).
* ``breaks`` - y positions where a page may be split (between loops, zones, matrix rows, system blocks): nothing crosses them.
* ``header_h`` - the height at the top that repeats on every PDF page (the cause-and-effect column headings), else 0.
* ``symbols`` - the symbol keys used, for the legend.

Two sizes: ``wide`` (desktop console, every export) and ``narrow`` (a phone: about 320 units across, fewer columns). Text is
measured with Helvetica metrics (reportlab, pure Python) and cut with an ellipsis to the room it has, so labels never run into
each other; the console and the exports name Helvetica / Arial first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reportlab.pdfbase.pdfmetrics import stringWidth

from . import schematic_symbols as symbols

DISCLAIMER = "Draft schematic prepared with Jarvis – to be checked by a competent person."
FONT, FONT_BOLD = "Helvetica", "Helvetica-Bold"
MODES = ("wide", "narrow")
SOURCE_TEXT = {"fsm": "Salts FSM records", "description": "a description given to Jarvis", "quote": "a quote / specification",
               "survey": "a site survey", "mixed": "Salts FSM records plus a description"}


# --------------------------------------------------------------------------------------------- text measuring
def tw(text: str, fs: float, bold: bool = False) -> float:
    return stringWidth(text, FONT_BOLD if bold else FONT, fs)


def fit(text: str, fs: float, max_w: float, bold: bool = False) -> str:
    """``text`` cut (with an ellipsis) so it is at most ``max_w`` wide at size ``fs``."""
    text = " ".join(str(text or "").split())
    if max_w <= 0 or not text:
        return "" if max_w <= 0 else text
    if tw(text, fs, bold) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tw(text[:mid].rstrip() + "…", fs, bold) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + "…") if lo > 0 else ""


def wrap(text: str, fs: float, max_w: float, lines: int = 2, bold: bool = False) -> list[str]:
    """Word-wrap into at most ``lines`` lines; the last one is cut with an ellipsis if the text still doesn't fit."""
    words = " ".join(str(text or "").split()).split(" ")
    out: list[str] = []
    cur = ""
    i = 0
    while i < len(words) and words != [""]:
        w = words[i]
        trial = f"{cur} {w}".strip()
        if tw(trial, fs, bold) <= max_w or not cur:
            if not cur and tw(w, fs, bold) > max_w and len(out) < lines - 1:
                # one long word: cut it onto this line and carry on
                cut = fit(w, fs, max_w, bold)
                out.append(cut)
                i += 1
                continue
            cur = trial
            i += 1
        else:
            out.append(cur)
            cur = ""
            if len(out) == lines - 1:
                cur = " ".join(words[i:])
                break
    if cur:
        out.append(cur)
    out = out[:lines]
    if out:
        out[-1] = fit(out[-1], fs, max_w, bold)
    return [o for o in out if o]


def r2(v: float) -> float:
    return round(float(v), 2)


# --------------------------------------------------------------------------------------------- the scene
class Scene:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.w = 0.0
        self.h = 0.0
        self.breaks: list[float] = []
        self.header_h = 0.0
        self.used: list[str] = []
        self.assumed = False

    def line(self, x1, y1, x2, y2, c="ink", sw=1.4, d=False):
        self.items.append({"t": "line", "x1": r2(x1), "y1": r2(y1), "x2": r2(x2), "y2": r2(y2), "c": c, "sw": sw, "d": 1 if d else 0})

    def poly(self, pts, c="ink", sw=1.4, d=False, z=False, f=None):
        clean = []
        for x, y in pts:
            p = [r2(x), r2(y)]
            if not clean or clean[-1] != p:
                clean.append(p)
        self.items.append({"t": "poly", "pts": clean, "c": c, "sw": sw, "d": 1 if d else 0, "z": bool(z), "f": f})

    def rect(self, x, y, w, h, c="ink", sw=1.0, d=False, f=None, rx=0.0, at=None):
        """``at``: insert at that index (a box drawn UNDER what was already put inside it)."""
        item = {"t": "rect", "x": r2(x), "y": r2(y), "w": r2(w), "h": r2(h), "rx": r2(rx), "c": c, "sw": sw, "d": 1 if d else 0, "f": f}
        if at is None:
            self.items.append(item)
        else:
            self.items.insert(at, item)

    def text(self, x, y, s, fs=10.0, a="start", w=400, c="ink", max_w=None, rot=0):
        s = fit(s, fs, max_w, w >= 600) if max_w is not None else " ".join(str(s or "").split())
        if s:
            self.items.append({"t": "text", "x": r2(x), "y": r2(y), "s": s, "fs": fs, "a": a, "w": w, "c": c, "rot": rot})

    def sym(self, key, x, y, sz, assumed=False, code=""):
        self.items.append({"t": "sym", "k": key, "x": r2(x), "y": r2(y), "sz": sz, "c": "assumed" if assumed else "ink",
                           "d": 1 if assumed else 0, "code": code})
        if key not in self.used:
            self.used.append(key)
        if assumed:
            self.assumed = True

    def done(self) -> dict[str, Any]:
        return {"w": r2(self.w), "h": r2(self.h), "items": self.items, "breaks": [r2(b) for b in self.breaks],
                "header_h": r2(self.header_h), "symbols": list(self.used)}


def _shade(assumed: bool, base: str = "ink") -> str:
    return "assumed" if assumed else base


# --------------------------------------------------------------------------------------------- legend, notes, disclaimer
def _legend_and_notes(sc: Scene, spec: dict[str, Any], x: float, y: float, width: float, fs: float, extra_key: list[tuple[str, str]] = (),
                      extra_notes: list[str] = ()) -> float:
    """The key (every symbol used + 'assumed'), the notes (source, assumptions, notes) and the disclaimer. Returns the bottom y."""
    sc.breaks.append(y)
    y += 8
    sc.line(x, y, x + width, y, c="line", sw=1)
    y += 18
    sc.text(x, y, "Key", fs=fs + 1, w=600, max_w=width)
    y += 10
    item_w = 220 if width >= 440 else max(140, width)
    cols = max(1, int(width // item_w))
    entries: list[tuple[str, str, bool]] = [(k, symbols.label(k), False) for k in sc.used]
    entries += [("", f"{code}  {text}", False) for code, text in extra_key]
    if sc.assumed or spec.get("_any_assumed"):
        entries.append(("other", "Assumed - not from records; check on site", True))
    row_h = 26
    for i, (key, text, dashed) in enumerate(entries):
        cx = x + (i % cols) * item_w
        cy = y + (i // cols) * row_h + 12
        if key:
            sc.items.append({"t": "sym", "k": key, "x": r2(cx + 10), "y": r2(cy), "sz": 18, "c": "assumed" if dashed else "ink",
                             "d": 1 if dashed else 0, "code": "?" if dashed else ""})
            sc.text(cx + 26, cy + fs * 0.35, text, fs=fs - 0.5, c="muted" if not dashed else "assumed", max_w=item_w - 32)
        else:
            sc.text(cx, cy + fs * 0.35, text, fs=fs - 0.5, c="muted", max_w=item_w - 6)
    y += ((len(entries) + cols - 1) // cols) * row_h + 10
    notes: list[str] = []
    src = SOURCE_TEXT.get(spec.get("source") or "description", "a description given to Jarvis")
    notes.append(f"Built from {src}" + (f" ({spec['source_ref']})" if spec.get("source_ref") else "") + ".")
    notes += list(extra_notes)
    notes += [f"Assumed: {a}" for a in spec.get("assumptions") or []]
    notes += list(spec.get("notes") or [])
    sc.breaks.append(y)
    sc.text(x, y + 14, "Notes", fs=fs + 1, w=600, max_w=width)
    y += 20
    for n in notes:
        for k, ln in enumerate(wrap(n, fs - 0.5, width - 12, lines=3)):
            y += fs + 4
            if k == 0:
                sc.text(x, y, "•", fs=fs - 0.5, c="muted")
            sc.text(x + 10, y, ln, fs=fs - 0.5, c="muted", max_w=width - 12)
    y += 22
    for ln in wrap(DISCLAIMER, fs, width, lines=2, bold=True):
        sc.text(x, y, ln, fs=fs, w=600, c="accent", max_w=width)
        y += fs + 4
    return y + 6


# ============================================================================================= fire alarm loops / zones
@dataclass(frozen=True)
class FireGeo:
    cols: int
    cell_w: float
    cell_h: float
    sym: float
    margin: float
    strip_w: float
    gutter: float
    right: float
    fs: float
    fs_small: float
    head_w: float  # 0 = the full width


FIRE_GEO = {"wide": FireGeo(cols=7, cell_w=112, cell_h=96, sym=30, margin=20, strip_w=44, gutter=34, right=30, fs=10.5, fs_small=9,
                            head_w=360),
            "narrow": FireGeo(cols=2, cell_w=116, cell_h=96, sym=28, margin=10, strip_w=30, gutter=28, right=24, fs=10, fs_small=8.5,
                              head_w=0)}
CABLE_DY = 38      # cable height inside a cell, from the cell's top
ISO_SIZE = 14


def _fire_cells(g: FireGeo, n: int, band_left: float, rows_top: float) -> list[tuple[float, float, int, bool]]:
    """(cx, cy, row, left_to_right) for each of n cells, laid out as a snake (row 0 left to right, row 1 right to left, ...)."""
    out = []
    for i in range(n):
        r, k = divmod(i, g.cols)
        ltr = r % 2 == 0
        col = k if ltr else g.cols - 1 - k
        out.append((band_left + col * g.cell_w + g.cell_w / 2, rows_top + r * g.cell_h + CABLE_DY, r, ltr))
    return out


def _fire_band(sc: Scene, g: FireGeo, y: float, tag_out: str, tag_in: str, title: str, devices: list[dict[str, Any]],
               returns: str | None, eol: dict[str, Any] | None) -> float:
    """One loop / zone / network chain from the panel's terminal strip. ``returns``: None (an open chain), "confirmed" or
    "unconfirmed" (the B end is drawn dashed with a note). ``eol``: an end-of-line marker after the last device (a zone)."""
    band_left = g.margin + g.strip_w + g.gutter
    band_w = g.cols * g.cell_w
    strip_x = g.margin
    strip_r = g.margin + g.strip_w
    sc.breaks.append(y)
    top = y
    first = len(sc.items)
    sc.text(band_left, y + 16, title, fs=g.fs + 0.5, w=600, max_w=band_w)
    rows_top = y + 24
    seq = list(devices) + ([{"type": "eol", "label": eol.get("label") or "EOL", "assumed": bool(eol.get("assumed")), "_eol": True}]
                           if eol else [])
    cells = _fire_cells(g, len(seq), band_left, rows_top)
    nrows = (len(seq) + g.cols - 1) // g.cols
    # ---- the cable, drawn first so symbols sit on top of it
    cy0 = cells[0][1]
    path: list[tuple[float, float]] = [(strip_r, cy0)]
    for i, (cx, cy, r, ltr) in enumerate(cells):
        if i > 0 and cells[i - 1][2] != r:
            pcx, pcy, _pr, pltr = cells[i - 1]
            ch = band_left + band_w + 12 if pltr else band_left - 12
            path += [(ch, pcy), (ch, cy)]
        path.append((cx, cy))
    sc.poly(path, c="ink", sw=1.6)
    bottom = rows_top + nrows * g.cell_h
    if returns is not None:
        lcx, lcy, _lr, lltr = cells[-1]
        x_exit = lcx + (g.cell_w / 2 if lltr else -g.cell_w / 2)
        lane = bottom + 4
        dashed = returns != "confirmed"
        sc.poly([(lcx, lcy), (x_exit, lcy), (x_exit, lane), (strip_r, lane)], c="assumed" if dashed else "ink", sw=1.6, d=dashed)
        if dashed:
            sc.text(band_left + 4, lane + 14, "Return to the panel (B end) not confirmed", fs=g.fs_small, c="assumed", max_w=band_w - 8)
            bottom = lane + 22
        else:
            bottom = lane + 12
        sc.text(strip_x + g.strip_w / 2, lane + g.fs_small * 0.35 - 6, tag_in, fs=g.fs_small, a="middle", w=600, max_w=g.strip_w - 4)
    bottom += 6
    # ---- the terminal strip segment for this band (one per band, so a page split never cuts one)
    sc.rect(strip_x, top, g.strip_w, bottom - top, c="ink", sw=1.2, f="soft", at=first)
    sc.text(strip_x + g.strip_w / 2, cy0 - 6, tag_out, fs=g.fs_small, a="middle", w=600, max_w=g.strip_w - 4)
    # ---- the devices
    lab_w = g.cell_w - 12
    for (cx, cy, r, ltr), dev in zip(cells, seq):
        assumed = bool(dev.get("assumed"))
        key = dev["type"]
        if dev.get("isolator") and key != "isolator":
            # on the cable just before the device: at the cell's edge on the side the cable comes in from
            sc.sym("isolator", cx - g.cell_w / 2 if ltr else cx + g.cell_w / 2, cy, ISO_SIZE, assumed=assumed)
        size = g.sym * (0.62 if key == "isolator" else 1.0)
        sc.sym(key, cx, cy, size, assumed=assumed, code=dev.get("code", ""))
        top_txt = " · ".join(p for p in (dev.get("address", ""), f"Z{dev['zone']}" if dev.get("zone") else "") if p)
        if top_txt:
            sc.text(cx, cy - g.sym / 2 - 6, top_txt, fs=g.fs_small, a="middle", w=600, c=_shade(assumed), max_w=g.cell_w - 10)
        name = dev.get("label") or ("" if dev.get("_eol") else symbols.label(key))
        lines = wrap(name, g.fs, lab_w, lines=2)
        ly = cy + g.sym / 2 + 13
        for ln in lines:
            sc.text(cx, ly, ln, fs=g.fs, a="middle", c=_shade(assumed, "ink" if dev.get("label") else "muted"), max_w=lab_w)
            ly += g.fs + 2
    return bottom


def layout_fire(spec: dict[str, Any], mode: str = "wide") -> dict[str, Any]:
    g = FIRE_GEO[mode]
    sc = Scene()
    W = g.margin + g.strip_w + g.gutter + g.cols * g.cell_w + g.right
    sc.w = W
    head_w = g.head_w or (W - 2 * g.margin)
    # ---- the panel head
    panel = spec["panel"]
    pa = bool(panel.get("assumed"))
    x0, y0 = g.margin, g.margin
    inner = head_w - 16
    lines: list[tuple[str, float, int, str]] = []
    for ln in wrap(panel.get("label") or "Fire alarm panel", g.fs + 2, inner, lines=2, bold=True):
        lines.append((ln, g.fs + 2, 600, _shade(pa)))
    conventional = spec["system_type"] == "conventional"
    n_circuits = len(spec["zones"] if conventional else spec["loops"])
    kind_line = (f"Conventional · {n_circuits} zone{'s' if n_circuits != 1 else ''}" if conventional
                 else f"Addressable · {n_circuits} loop{'s' if n_circuits != 1 else ''}")
    lines.append((kind_line, g.fs_small, 400, "muted"))
    if panel.get("model"):
        lines.append((f"Model: {panel['model']}", g.fs_small, 400, _shade(pa, "muted")))
    if panel.get("location"):
        lines.append((f"Location: {panel['location']}", g.fs_small, 400, _shade(pa, "muted")))
    for io in spec.get("panel_io") or []:
        lines.append((f"- {io['label']}" + (" (assumed)" if io.get("assumed") else ""), g.fs_small, 400, _shade(bool(io.get("assumed")))))
    y = y0 + 8
    first = len(sc.items)
    for text, fs, w, c in lines:
        y += fs + 4
        sc.text(x0 + 8, y, text, fs=fs, w=w, c=c, max_w=inner)
    head_h = y - y0 + 10
    sc.rect(x0, y0, head_w, head_h, c=_shade(pa), sw=1.6, d=pa, f="paper", rx=4, at=first)
    if pa:
        sc.assumed = True
    y = y0 + head_h
    # ---- the bands: the panel network first, then each loop / zone
    if spec.get("network"):
        y = _fire_band(sc, g, y, "NET", "", "Panel network", spec["network"], None, None) + 8
    if conventional:
        for z in spec["zones"]:
            title = f"Zone {z['number']}" + (f" — {z['label']}" if z.get("label") else "") + f" · {len(z['devices'])} devices"
            y = _fire_band(sc, g, y, f"Z{z['number']}", "", title, z["devices"], None,
                           {"label": z.get("eol") or "EOL", "assumed": z.get("eol_assumed")}) + 8
    else:
        for lp in spec["loops"]:
            title = f"Loop {lp['number']}" + (f" — {lp['label']}" if lp.get("label") else "") + f" · {len(lp['devices'])} devices"
            y = _fire_band(sc, g, y, f"{lp['number']}A", f"{lp['number']}B", title, lp["devices"],
                           "confirmed" if lp.get("return_confirmed", True) else "unconfirmed", None) + 8
    extra = []
    if not conventional and any(d.get("isolator") for lp in spec["loops"] for d in lp["devices"]):
        extra.append("A small isolator mark on the cable before a device = an isolator built into that device's base or module.")
    if not conventional:
        extra.append("Each loop leaves the panel on its A terminal and returns on its B terminal; devices are drawn in loop order.")
    y = _legend_and_notes(sc, spec, g.margin, y + 6, W - 2 * g.margin, g.fs, extra_notes=extra)
    sc.h = y + g.margin
    return sc.done()


# ============================================================================================= cause and effect
@dataclass(frozen=True)
class CEGeo:
    col_w: float
    row_h: float
    head_max: float
    head_min: float
    fs: float
    fs_small: float
    margin: float
    label_max: float


CE_GEO = {"wide": CEGeo(col_w=30, row_h=30, head_max=300, head_min=150, fs=10.5, fs_small=9, margin=20, label_max=170),
          "narrow": CEGeo(col_w=27, row_h=30, head_max=150, head_min=110, fs=10, fs_small=8.5, margin=10, label_max=130)}
CE_CATEGORY_TEXT = {"sounders": ("Sounders", "SND"), "door_holders": ("Door holders", "DH"), "plant_shutdown": ("Plant shutdown", "PLT"),
                    "aov": ("AOVs", "AOV"), "signalling": ("Fire signalling", "SIG"), "lifts": ("Lifts", "LIFT"),
                    "access_release": ("Access release", "ACC"), "gas_shutoff": ("Gas shut-off", "GAS"),
                    "suppression": ("Suppression", "SUP"), "other": ("Other", "OTH")}
CE_ACTION_KEY = [("X", "operate / activate"), ("C", "continuous tone (evacuate)"), ("P", "pulsing tone (alert)"), ("R", "release"),
                 ("S", "shut down"), ("T", "transmit signal"), ("30", "(a number after the letter) delay in seconds before it acts")]


def layout_cause_effect(spec: dict[str, Any], mode: str = "wide") -> dict[str, Any]:
    g = CE_GEO[mode]
    sc = Scene()
    ins, outs = spec["inputs"], spec["outputs"]
    head_w = max(g.head_min, min(g.head_max, max(tw(f"{i['id']}  {i['label']}", g.fs) for i in ins) + 18))
    gx = g.margin + head_w
    grid_w = len(outs) * g.col_w
    W = gx + grid_w + g.margin
    # category bands
    y = g.margin
    cat_h = 22
    i = 0
    while i < len(outs):
        j = i
        while j + 1 < len(outs) and outs[j + 1]["category"] == outs[i]["category"]:
            j += 1
        span = (j - i + 1) * g.col_w
        long, short = CE_CATEGORY_TEXT.get(outs[i]["category"], ("Other", "OTH"))
        text = long if tw(long, g.fs_small, True) <= span - 6 else short
        sc.rect(gx + i * g.col_w, y, span, cat_h, c="ink", sw=1, f="soft")
        sc.text(gx + i * g.col_w + span / 2, y + cat_h / 2 + g.fs_small * 0.35, text, fs=g.fs_small, a="middle", w=600, max_w=span - 4)
        i = j + 1
    # rotated output headings
    lab_h = min(g.label_max, max(tw(f"{o['id']} {o['label']}", g.fs) for o in outs) + 14)
    hy = y + cat_h
    hb = hy + lab_h
    for k, o in enumerate(outs):
        x = gx + k * g.col_w
        sc.rect(x, hy, g.col_w, lab_h, c="ink", sw=1)
        sc.text(x + g.col_w / 2 + g.fs * 0.35, hb - 6, f"{o['id']} {o['label']}", fs=g.fs, c=_shade(bool(o.get("assumed"))),
                max_w=lab_h - 12, rot=-90)
    sc.text(g.margin + 6, hb - 22, "Effects (outputs) across", fs=g.fs_small, c="muted", max_w=head_w - 10)
    sc.text(g.margin + 6, hb - 8, "Causes (inputs) down", fs=g.fs_small, c="muted", max_w=head_w - 10)
    sc.header_h = hb
    # rows
    eff = {(e["input"], e["output"]): e for e in spec["effects"]}
    for r, inp in enumerate(ins):
        ry = hb + r * g.row_h
        sc.breaks.append(ry)
        ia = bool(inp.get("assumed"))
        sc.rect(g.margin, ry, head_w + grid_w, g.row_h, c="ink", sw=1, f="soft" if r % 2 else None)
        lines = wrap(f"{inp['id']}  {inp['label']}", g.fs, head_w - 12, lines=2)
        if len(lines) == 1:
            sc.text(g.margin + 6, ry + g.row_h / 2 + g.fs * 0.35, lines[0], fs=g.fs, c=_shade(ia), max_w=head_w - 12)
        else:
            sc.text(g.margin + 6, ry + 12.5, lines[0], fs=g.fs - 0.5, c=_shade(ia), max_w=head_w - 12)
            sc.text(g.margin + 6, ry + 24.5, lines[1], fs=g.fs - 0.5, c=_shade(ia), max_w=head_w - 12)
        sc.line(gx + grid_w, ry, gx + grid_w, ry + g.row_h, c="ink", sw=1)
        for k, o in enumerate(outs):
            x = gx + k * g.col_w
            sc.line(x, ry, x, ry + g.row_h, c="ink", sw=1)
            e = eff.get((inp["id"], o["id"]))
            if e:
                a = bool(e.get("assumed"))
                if a:
                    sc.rect(x + 3, ry + 3, g.col_w - 6, g.row_h - 6, c="assumed", sw=1, d=True)
                    sc.assumed = True
                sc.text(x + g.col_w / 2, ry + g.row_h / 2 + g.fs * 0.36, e["code"], fs=g.fs if len(e["code"]) <= 2 else g.fs_small,
                        a="middle", w=600, c="assumed" if a else "ink", max_w=g.col_w - 4)
    gb = hb + len(ins) * g.row_h
    sc.rect(g.margin, y, head_w, hb - y, c="ink", sw=1)   # the top-left corner box
    if any(o.get("assumed") for o in outs) or any(i.get("assumed") for i in ins):
        sc.assumed = True
    text_w = max(W - 2 * g.margin, 300.0 if mode == "narrow" else 520.0)
    sc.w = max(W, text_w + 2 * g.margin)
    y = _legend_and_notes(sc, spec, g.margin, gb + 4, text_w, g.fs, extra_key=CE_ACTION_KEY)
    sc.h = y + g.margin
    return sc.done()


# ============================================================================================= security / network topology
@dataclass(frozen=True)
class NetGeo:
    block_w: float
    row_h: float
    indent: float
    sym: float
    fs: float
    fs_small: float
    margin: float
    gap: float
    max_cols: int


NET_GEO = {"wide": NetGeo(block_w=300, row_h=42, indent=30, sym=24, fs=10.5, fs_small=9, margin=20, gap=26, max_cols=3),
           "narrow": NetGeo(block_w=300, row_h=42, indent=24, sym=24, fs=10, fs_small=8.5, margin=10, gap=16, max_cols=1)}
NET_KIND_TEXT = {"cctv": "CCTV", "access": "Access control", "intruder": "Intruder alarm", "signalling": "Alarm signalling",
                 "fire": "Fire alarm interface", "network": "Network", "door_entry": "Door entry", "other": "Other"}
LINK_TEXT = {"ethernet": "Ethernet", "poe": "PoE", "fibre": "Fibre", "wifi": "Wi-Fi", "rs485": "RS-485", "wiegand": "Wiegand",
             "osdp": "OSDP", "bus": "Bus", "zone": "Zone wiring", "relay": "Relay", "hardwired": "Hard-wired", "radio": "Radio",
             "4g": "4G", "ip": "IP", "pstn": "PSTN", "coax": "Coax", "power": "Power", "other": "Other link"}


def _net_block(sc: Scene, g: NetGeo, system: dict[str, Any], bx: float, by: float) -> float:
    title = NET_KIND_TEXT.get(system["kind"], "System") + (f" — {system['label']}" if system.get("label") else "")
    sc.text(bx, by + 15, title, fs=g.fs + 1, w=600, max_w=g.block_w)
    sc.line(bx, by + 22, bx + g.block_w, by + 22, c="line", sw=1)
    rows_top = by + 30
    pos: dict[str, tuple[float, float]] = {}
    last_child: dict[str, float] = {}
    order = system["order"]
    nodes = {n["id"]: n for n in system["nodes"]}
    for i, nid in enumerate(order):
        n = nodes[nid]
        sx = bx + 14 + n["_depth"] * g.indent
        cy = rows_top + i * g.row_h + 15
        pos[nid] = (sx, cy)
        if n.get("parent"):
            last_child[n["parent"]] = cy
    # connectors first (under the symbols)
    for nid, child_y in last_child.items():
        px, py = pos[nid]
        sc.line(px, py + g.sym / 2, px, child_y, c="ink", sw=1.4)
    for nid in order:
        n = nodes[nid]
        if not n.get("parent"):
            continue
        px, _py = pos[n["parent"]]
        sx, cy = pos[nid]
        a = bool(n.get("assumed"))
        sc.line(px, cy, sx - g.sym / 2 - 1, cy, c=_shade(a), sw=1.4, d=a)
        if n.get("secondary_link"):
            sc.poly([(px - 4, cy - 2), (px - 4, cy + 5), (sx - g.sym / 2 - 1, cy + 5)], c=_shade(a, "accent"), sw=1.2, d=True)
    for nid in order:
        n = nodes[nid]
        sx, cy = pos[nid]
        a = bool(n.get("assumed"))
        sc.sym(n["type"], sx, cy, g.sym, assumed=a, code=n.get("code", ""))
        tx = sx + g.sym / 2 + 8
        room = bx + g.block_w - tx - 2
        sc.text(tx, cy - 1, n.get("label") or symbols.label(n["type"]), fs=g.fs, w=600, c=_shade(a), max_w=room)
        bits = [symbols.label(n["type"]) if n.get("label") else ""]
        if n.get("link"):
            link = LINK_TEXT.get(n["link"], n["link"])
            if n.get("secondary_link"):
                link = f"dual path: {link} + {LINK_TEXT.get(n['secondary_link'], n['secondary_link'])}"
            bits.append(link + (f" port {n['port']}" if n.get("port") else ""))
        elif n.get("port"):
            bits.append(f"port {n['port']}")
        if n.get("location"):
            bits.append(n["location"])
        if a:
            bits.append("assumed")
        sc.text(tx, cy + g.fs_small + 2.5, " · ".join(b for b in bits if b), fs=g.fs_small, c=_shade(a, "muted"), max_w=room)
    return rows_top + len(order) * g.row_h + 4


def layout_network(spec: dict[str, Any], mode: str = "wide") -> dict[str, Any]:
    g = NET_GEO[mode]
    sc = Scene()
    systems = spec["systems"]
    ncols = max(1, min(g.max_cols, len(systems)))
    heights = [g.margin] * ncols
    blocks: list[tuple[float, float]] = []
    for s in systems:
        c = min(range(ncols), key=lambda k: (heights[k], k))
        bx = g.margin + c * (g.block_w + g.gap)
        by = heights[c]
        bottom = _net_block(sc, g, s, bx, by)
        blocks.append((by, bottom))
        heights[c] = bottom + 18
    W = 2 * g.margin + ncols * g.block_w + (ncols - 1) * g.gap
    sc.w = W
    # a page may break where no block spans the line
    for top, _bottom in blocks:
        if not any(t < top < b for t, b in blocks):
            sc.breaks.append(top)
    y = max(heights)
    y = _legend_and_notes(sc, spec, g.margin, y, W - 2 * g.margin, g.fs)
    sc.h = y + g.margin
    return sc.done()


LAYOUTS = {"fire_loop": layout_fire, "cause_effect": layout_cause_effect, "network": layout_network}


def layout(kind: str, spec: dict[str, Any], mode: str = "wide") -> dict[str, Any]:
    """The scene for a validated spec. ``mode``: wide (desktop and every export) or narrow (a phone)."""
    if mode not in MODES:
        mode = "wide"
    return LAYOUTS[kind](spec, mode)


def expand(scene: dict[str, Any]) -> list[dict[str, Any]]:
    """The scene's items with every symbol replaced by its primitives (what the console and the renderers draw)."""
    out: list[dict[str, Any]] = []
    for it in scene["items"]:
        if it["t"] == "sym":
            out += symbols.expand(it["k"], it["x"], it["y"], it["sz"], it["c"], bool(it["d"]), it.get("code", ""))
        else:
            out.append(it)
    return out


def text_box(it: dict[str, Any]) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of a text item, from the same Helvetica metrics the layout used."""
    width = tw(it["s"], it["fs"], it.get("w", 400) >= 600)
    asc, desc = it["fs"] * 0.78, it["fs"] * 0.24
    if it.get("rot"):
        # rotated -90 about (x, y), anchored at its start: it runs upwards from y
        return (it["x"] - asc, it["y"] - width, it["x"] + desc, it["y"])
    a = it.get("a", "start")
    x0 = it["x"] - (width / 2 if a == "middle" else width if a == "end" else 0)
    return (x0, it["y"] - asc, x0 + width, it["y"] + desc)


def item_box(it: dict[str, Any]) -> tuple[float, float, float, float]:
    t = it["t"]
    if t == "text":
        return text_box(it)
    if t == "sym":
        h = it["sz"] / 2
        return (it["x"] - h, it["y"] - h, it["x"] + h, it["y"] + h)
    if t == "rect":
        return (it["x"], it["y"], it["x"] + it["w"], it["y"] + it["h"])
    if t == "circle":
        return (it["cx"] - it["r"], it["cy"] - it["r"], it["cx"] + it["r"], it["cy"] + it["r"])
    if t == "line":
        return (min(it["x1"], it["x2"]), min(it["y1"], it["y2"]), max(it["x1"], it["x2"]), max(it["y1"], it["y2"]))
    xs = [p[0] for p in it["pts"]]
    ys = [p[1] for p in it["pts"]]
    return (min(xs), min(ys), max(xs), max(ys))
