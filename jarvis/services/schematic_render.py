"""Exports of a system schematic: SVG, PNG and PDF (A4 / A3 landscape), each with a title block and the draft disclaimer.

Every format draws the SAME primitives: the scene from ``schematic_layout`` (symbols expanded by ``schematic_symbols``) placed on a
sheet with a frame and a title block (company, drawing title, site, system, job, drawing number, revision, date, sheet, status,
and "Draft schematic prepared with Jarvis – to be checked by a competent person."). Print colours only (black on white, red for
the fire accents, grey dashed for anything assumed) - an export never depends on the console's theme.

* SVG: text written with XML escaping; the only attributes are numbers and colours this module chose.
* PNG: Pillow, at up to 2x, from the same primitives (a TrueType sans font; text is squeezed back to the width the layout measured).
* PDF: reportlab (vector). The drawing is scaled to the page; when that would make it too small to read, it is split over several
  sheets at the scene's ``breaks`` (between loops / zones / matrix rows / system blocks), repeating the matrix headings.

Pure functions, no clock (the date printed is the revision's date), no network.
"""

from __future__ import annotations

import io
import math
from typing import Any
from xml.sax.saxutils import escape

from . import schematic_layout as L

PRINT = {"ink": "#1b1f24", "muted": "#58606b", "paper": "#ffffff", "soft": "#eef1f5", "accent": "#c62828", "assumed": "#8c929b",
         "line": "#c3cad4"}
PAD = 24.0
TB_H = 112.0
MIN_SCALE = 0.62      # below this a PDF sheet is split rather than shrunk further
MAX_SHEETS = 24
PAPER_PT = {"a4": (841.89, 595.28), "a3": (1190.55, 841.89)}
STATUS = "DRAFT - for checking"
FONT_STACK = "Helvetica, Arial, 'Liberation Sans', sans-serif"


def _date_text(iso: str) -> str:
    try:
        y, m, d = str(iso)[:10].split("-")
        return f"{d}/{m}/{y}"
    except ValueError:
        return str(iso or "")


# --------------------------------------------------------------------------------------------- moving primitives
def transform(prims: list[dict[str, Any]], dx: float, dy: float, s: float = 1.0) -> list[dict[str, Any]]:
    out = []
    for p in prims:
        q = dict(p)
        t = p["t"]
        if "sw" in q:
            q["sw"] = p["sw"] * s
        if t == "line":
            q.update(x1=dx + p["x1"] * s, y1=dy + p["y1"] * s, x2=dx + p["x2"] * s, y2=dy + p["y2"] * s)
        elif t == "rect":
            q.update(x=dx + p["x"] * s, y=dy + p["y"] * s, w=p["w"] * s, h=p["h"] * s, rx=p.get("rx", 0) * s)
        elif t == "circle":
            q.update(cx=dx + p["cx"] * s, cy=dy + p["cy"] * s, r=p["r"] * s)
        elif t == "poly":
            q["pts"] = [[dx + x * s, dy + y * s] for x, y in p["pts"]]
        elif t == "text":
            q.update(x=dx + p["x"] * s, y=dy + p["y"] * s, fs=p["fs"] * s)
        elif t == "sym":
            q.update(x=dx + p["x"] * s, y=dy + p["y"] * s, sz=p["sz"] * s)
        out.append(q)
    return out


def _cell(x: float, y: float, w: float, h: float, caption: str, value: str, fs: float = 10.5, bold: bool = False,
          colour: str = "ink") -> list[dict[str, Any]]:
    out = [{"t": "rect", "x": x, "y": y, "w": w, "h": h, "rx": 0, "c": "ink", "sw": 0.9, "d": 0, "f": None}]
    if caption:
        out.append({"t": "text", "x": x + 5, "y": y + 10, "s": caption.upper(), "fs": 6.8, "a": "start", "w": 600, "c": "muted", "rot": 0})
    val = L.fit(value or "-", fs, w - 10, bold)
    if val:
        out.append({"t": "text", "x": x + 5, "y": y + h - 8, "s": val, "fs": fs, "a": "start", "w": 600 if bold else 400, "c": colour,
                    "rot": 0})
    return out


def title_block(x: float, y: float, w: float, meta: dict[str, Any], sheet: str) -> list[dict[str, Any]]:
    """The title block, TB_H tall, in sheet units."""
    out: list[dict[str, Any]] = [{"t": "rect", "x": x, "y": y, "w": w, "h": TB_H, "rx": 0, "c": "ink", "sw": 1.4, "d": 0, "f": "paper"}]
    rows = [
        (34, [(0.30, "Company", meta.get("company", ""), 12.5, True), (0.70, "Drawing title", meta.get("title", ""), 12.5, True)]),
        (30, [(0.42, "Site", meta.get("site", ""), 10.5, False), (0.36, "System", meta.get("system", ""), 10.5, False),
              (0.22, "Job", meta.get("job_ref", ""), 10.5, False)]),
        (30, [(0.22, "Drawing no.", meta.get("number", ""), 10.5, True), (0.10, "Rev", meta.get("rev", ""), 10.5, True),
              (0.16, "Date", _date_text(meta.get("date", "")), 10.5, False), (0.14, "Sheet", sheet, 10.5, False),
              (0.38, "Status", STATUS, 10.5, False)]),
    ]
    cy = y
    for h, cells in rows:
        cx = x
        for frac, cap, val, fs, bold in cells:
            cw = w * frac
            out += _cell(cx, cy, cw, h, cap, val, fs, bold)
            cx += cw
        cy += h
    out.append({"t": "text", "x": x + 5, "y": cy + 12.5, "s": L.fit(L.DISCLAIMER, 9.5, w - 10, True), "fs": 9.5, "a": "start", "w": 600,
                "c": "accent", "rot": 0})
    return out


def sheet(scene: dict[str, Any], meta: dict[str, Any]) -> tuple[float, float, list[dict[str, Any]]]:
    """The whole drawing on one sheet at natural size (SVG and PNG): (width, height, primitives)."""
    W = max(scene["w"], 760.0) + 2 * PAD
    dx = PAD + (W - 2 * PAD - scene["w"]) / 2
    y_tb = PAD + scene["h"] + 10
    H = y_tb + TB_H + PAD
    prims: list[dict[str, Any]] = [{"t": "rect", "x": 0, "y": 0, "w": W, "h": H, "rx": 0, "c": "paper", "sw": 0, "d": 0, "f": "paper"},
                                   {"t": "rect", "x": PAD / 2, "y": PAD / 2, "w": W - PAD, "h": H - PAD, "rx": 0, "c": "ink", "sw": 1.2,
                                    "d": 0, "f": None}]
    prims += transform(L.expand(scene), dx, PAD)
    prims += title_block(PAD, y_tb, W - 2 * PAD, meta, "1 of 1")
    return W, H, prims


# --------------------------------------------------------------------------------------------- SVG
def _num(v: float) -> str:
    return f"{round(float(v), 2):g}"


def _col(role: Any, pal: dict[str, str]) -> str:
    return pal.get(str(role), "none") if role else "none"


def to_svg(W: float, H: float, prims: list[dict[str, Any]], pal: dict[str, str] = PRINT) -> str:
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{_num(W)}" height="{_num(H)}" viewBox="0 0 {_num(W)} {_num(H)}" '
           f'font-family="{FONT_STACK}">']
    for p in prims:
        t = p["t"]
        stroke = f' stroke="{_col(p.get("c", "ink"), pal)}" stroke-width="{_num(p.get("sw", 1))}"' if p.get("sw", 1) else ' stroke="none"'
        dash = ' stroke-dasharray="4 3"' if p.get("d") else ""
        if t == "line":
            out.append(f'<line x1="{_num(p["x1"])}" y1="{_num(p["y1"])}" x2="{_num(p["x2"])}" y2="{_num(p["y2"])}"{stroke}{dash} '
                       'stroke-linecap="round"/>')
        elif t == "rect":
            out.append(f'<rect x="{_num(p["x"])}" y="{_num(p["y"])}" width="{_num(p["w"])}" height="{_num(p["h"])}" '
                       f'rx="{_num(p.get("rx", 0))}" fill="{_col(p.get("f"), pal)}"{stroke}{dash}/>')
        elif t == "circle":
            out.append(f'<circle cx="{_num(p["cx"])}" cy="{_num(p["cy"])}" r="{_num(p["r"])}" fill="{_col(p.get("f"), pal)}"{stroke}{dash}/>')
        elif t == "poly":
            pts = " ".join(f"{_num(x)},{_num(y)}" for x, y in p["pts"])
            tag = "polygon" if p.get("z") else "polyline"
            out.append(f'<{tag} points="{pts}" fill="{_col(p.get("f"), pal) if p.get("z") else "none"}"{stroke}{dash} '
                       'stroke-linejoin="round" stroke-linecap="round"/>')
        elif t == "text":
            anchor = {"middle": "middle", "end": "end"}.get(p.get("a"), "start")
            rot = f' transform="rotate({int(p["rot"])} {_num(p["x"])} {_num(p["y"])})"' if p.get("rot") else ""
            out.append(f'<text x="{_num(p["x"])}" y="{_num(p["y"])}" font-size="{_num(p["fs"])}" font-weight="{int(p.get("w", 400))}" '
                       f'text-anchor="{anchor}" fill="{_col(p.get("c", "ink"), pal)}"{rot}>{escape(str(p["s"]))}</text>')
    out.append("</svg>")
    return "\n".join(out)


def to_svg_sheet(scene: dict[str, Any], meta: dict[str, Any]) -> str:
    W, H, prims = sheet(scene, meta)
    title = escape(f"{meta.get('number', '')} {meta.get('rev', '')} {meta.get('title', '')}".strip())
    svg = to_svg(W, H, prims)
    return svg.replace(">", f"><title>{title}</title>", 1)


# --------------------------------------------------------------------------------------------- PNG
_FONT_CACHE: dict[tuple[bool, int], Any] = {}


def _font_paths(bold: bool) -> list[str]:
    import os

    names = (["LiberationSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["LiberationSans-Regular.ttf", "arial.ttf", "Arial.ttf", "DejaVuSans.ttf"])
    dirs = ["/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/liberation2", "/usr/share/fonts/liberation-sans",
            "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts", "/System/Library/Fonts/Supplemental",
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")]
    out = [os.path.join(d, n) for n in names for d in dirs]
    try:
        import reportlab

        out.append(os.path.join(os.path.dirname(reportlab.__file__), "fonts", "VeraBd.ttf" if bold else "Vera.ttf"))
    except ImportError:  # pragma: no cover - reportlab is a dependency
        pass
    return out


def _font(size: float, bold: bool):
    from PIL import ImageFont

    key = (bold, max(4, int(round(size))))
    if key not in _FONT_CACHE:
        font = None
        for path in _font_paths(bold):
            try:
                font = ImageFont.truetype(path, key[1])
                break
            except OSError:
                continue
        _FONT_CACHE[key] = font or ImageFont.load_default()
    return _FONT_CACHE[key]


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _dashed(points: list[tuple[float, float]], on: float, off: float) -> list[list[tuple[float, float]]]:
    """A polyline cut into dash segments."""
    out: list[list[tuple[float, float]]] = []
    draw, left, cur = True, on, [points[0]]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        pos = 0.0
        while seg - pos > 1e-6:
            step = min(left, seg - pos)
            pos += step
            px, py = x0 + (x1 - x0) * pos / seg, y0 + (y1 - y0) * pos / seg
            if draw:
                cur.append((px, py))
            left -= step
            if left <= 1e-6:
                if draw and len(cur) > 1:
                    out.append(cur)
                draw = not draw
                left = on if draw else off
                cur = [(px, py)]
    if draw and len(cur) > 1:
        out.append(cur)
    return out


def _outline(p: dict[str, Any]) -> list[tuple[float, float]]:
    t = p["t"]
    if t == "rect":
        x, y, w, h = p["x"], p["y"], p["w"], p["h"]
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    if t == "circle":
        n = 36
        return [(p["cx"] + p["r"] * math.cos(2 * math.pi * i / n), p["cy"] + p["r"] * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]
    if t == "line":
        return [(p["x1"], p["y1"]), (p["x2"], p["y2"])]
    pts = [tuple(q) for q in p["pts"]]
    return pts + [pts[0]] if p.get("z") else pts


def to_png(W: float, H: float, prims: list[dict[str, Any]], pal: dict[str, str] = PRINT, scale: float = 2.0) -> bytes:
    from PIL import Image, ImageDraw

    k = min(scale, math.sqrt(36_000_000 / max(1.0, W * H)), 12000 / max(W, H))
    img = Image.new("RGB", (max(1, int(math.ceil(W * k))), max(1, int(math.ceil(H * k)))), _rgb(pal["paper"]))
    d = ImageDraw.Draw(img)
    for p in prims:
        t = p["t"]
        sw = max(1, int(round(p.get("sw", 1) * k))) if p.get("sw", 1) else 0
        stroke = _rgb(pal.get(p.get("c", "ink"), pal["ink"]))
        fill = _rgb(pal[p["f"]]) if p.get("f") in pal else None
        if t == "text":
            bold = p.get("w", 400) >= 600
            size = p["fs"] * k
            target = L.tw(p["s"], p["fs"], bold) * k
            font = _font(size, bold)
            real = d.textlength(p["s"], font=font) if hasattr(d, "textlength") else target
            if real > target * 1.03 and real > 0:
                font = _font(size * target / real, bold)
            colour = _rgb(pal.get(p.get("c", "ink"), pal["ink"]))
            anchor = {"middle": "ms", "end": "rs"}.get(p.get("a"), "ls")
            if p.get("rot"):
                tw_ = int(math.ceil(d.textlength(p["s"], font=font))) + 4 if hasattr(d, "textlength") else int(target) + 4
                th = int(math.ceil(size * 1.3)) + 2
                tmp = Image.new("RGBA", (tw_, th), (0, 0, 0, 0))
                ImageDraw.Draw(tmp).text((0, size), p["s"], font=font, fill=colour + (255,), anchor="ls")
                rot = tmp.rotate(90, expand=True)
                # after rotating, the text's start (baseline at x) sits at the bottom
                img.paste(rot, (int(p["x"] * k - size), int(p["y"] * k - rot.size[1])), rot)
            else:
                d.text((p["x"] * k, p["y"] * k), p["s"], font=font, fill=colour, anchor=anchor)
            continue
        if t in ("rect", "circle") or (t == "poly" and p.get("z")):
            if fill is not None:
                if t == "rect":
                    box = [p["x"] * k, p["y"] * k, (p["x"] + p["w"]) * k, (p["y"] + p["h"]) * k]
                    if p.get("rx"):
                        d.rounded_rectangle(box, radius=p["rx"] * k, fill=fill)
                    else:
                        d.rectangle(box, fill=fill)
                elif t == "circle":
                    d.ellipse([(p["cx"] - p["r"]) * k, (p["cy"] - p["r"]) * k, (p["cx"] + p["r"]) * k, (p["cy"] + p["r"]) * k], fill=fill)
                else:
                    d.polygon([(x * k, y * k) for x, y in p["pts"]], fill=fill)
        if not sw or p.get("c") == "paper":
            continue
        pts = [(x * k, y * k) for x, y in _outline(p)]
        if p.get("d"):
            for seg in _dashed(pts, 4 * k, 3 * k):
                d.line(seg, fill=stroke, width=sw)
        elif t == "circle":
            d.ellipse([(p["cx"] - p["r"]) * k, (p["cy"] - p["r"]) * k, (p["cx"] + p["r"]) * k, (p["cy"] + p["r"]) * k], outline=stroke, width=sw)
        elif t == "rect" and p.get("rx"):
            d.rounded_rectangle([p["x"] * k, p["y"] * k, (p["x"] + p["w"]) * k, (p["y"] + p["h"]) * k], radius=p["rx"] * k, outline=stroke,
                                width=sw)
        else:
            d.line(pts, fill=stroke, width=sw, joint="curve")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def to_png_sheet(scene: dict[str, Any], meta: dict[str, Any]) -> bytes:
    W, H, prims = sheet(scene, meta)
    return to_png(W, H, prims)


# --------------------------------------------------------------------------------------------- PDF
def _segments(scene: dict[str, Any]) -> list[tuple[float, float]]:
    hdr = scene.get("header_h") or 0.0
    bounds = sorted({b for b in scene.get("breaks", []) if hdr < b < scene["h"]} | {hdr, scene["h"]})
    if hdr == 0:
        bounds = sorted(set(bounds) | {0.0})
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0.5]


def _select(items: list[dict[str, Any]], y0: float, y1: float) -> list[dict[str, Any]]:
    return [it for it in items if y0 - 0.01 <= L.item_box(it)[1] < y1 - 0.01]


def pages(scene: dict[str, Any], area_w: float, area_h: float) -> list[tuple[float, list[dict[str, Any]], float, float]]:
    """How the scene goes onto sheets of drawing area ``area_w`` x ``area_h``: a list of (scale, items, width, height) in scene units."""
    s_one = min(area_w / scene["w"], area_h / scene["h"], 1.0)
    segs = _segments(scene)
    if s_one >= MIN_SCALE or len(segs) <= 1:
        return [(s_one, scene["items"], scene["w"], scene["h"])]
    hdr = scene.get("header_h") or 0.0
    head_items = _select(scene["items"], 0.0, hdr) if hdr else []
    s_w = min(area_w / scene["w"], 1.0)
    room = area_h / s_w - hdr
    groups: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    used = 0.0
    for a, b in segs:
        h = b - a
        if cur and used + h > room:
            groups.append(cur)
            cur, used = [], 0.0
        cur.append((a, b))
        used += h
    if cur:
        groups.append(cur)
    if len(groups) > MAX_SHEETS:
        return [(s_one, scene["items"], scene["w"], scene["h"])]
    out = []
    for g in groups:
        y0, y1 = g[0][0], g[-1][1]
        body = [dict(it) for it in transform(_select(scene["items"], y0, y1), 0, hdr - y0)]
        h = hdr + (y1 - y0)
        s = min(s_w, area_h / h)
        out.append((s, head_items + body, scene["w"], h))
    return out


def _pdf_text(s: str) -> str:
    """Helvetica (a standard PDF font) covers Windows-1252 only: anything else is shown as '?' rather than a broken glyph."""
    return "".join(ch if ch.encode("cp1252", "ignore") else "?" for ch in s)


def _pdf_draw(c, prims: list[dict[str, Any]], page_h: float, pal: dict[str, str]) -> None:
    from reportlab.lib.colors import HexColor

    def Y(y: float) -> float:
        return page_h - y

    for p in prims:
        t = p["t"]
        stroke_role = p.get("c", "ink")
        sw = p.get("sw", 1)
        c.setStrokeColor(HexColor(pal.get(stroke_role, pal["ink"])))
        c.setLineWidth(max(0.25, sw))
        c.setDash(4, 3) if p.get("d") else c.setDash()
        fill = p.get("f")
        has_fill = fill in pal
        if has_fill:
            c.setFillColor(HexColor(pal[fill]))
        do_stroke = 1 if sw and stroke_role != "paper" else 0
        if t == "line":
            c.line(p["x1"], Y(p["y1"]), p["x2"], Y(p["y2"]))
        elif t == "rect":
            if p.get("rx"):
                c.roundRect(p["x"], Y(p["y"] + p["h"]), p["w"], p["h"], p["rx"], stroke=do_stroke, fill=1 if has_fill else 0)
            else:
                c.rect(p["x"], Y(p["y"] + p["h"]), p["w"], p["h"], stroke=do_stroke, fill=1 if has_fill else 0)
        elif t == "circle":
            c.circle(p["cx"], Y(p["cy"]), p["r"], stroke=do_stroke, fill=1 if has_fill else 0)
        elif t == "poly":
            path = c.beginPath()
            x0, y0 = p["pts"][0]
            path.moveTo(x0, Y(y0))
            for x, y in p["pts"][1:]:
                path.lineTo(x, Y(y))
            if p.get("z"):
                path.close()
            c.drawPath(path, stroke=do_stroke, fill=1 if (has_fill and p.get("z")) else 0)
        elif t == "text":
            c.setFillColor(HexColor(pal.get(p.get("c", "ink"), pal["ink"])))
            c.setFont("Helvetica-Bold" if p.get("w", 400) >= 600 else "Helvetica", p["fs"])
            s = _pdf_text(str(p["s"]))
            c.saveState()
            c.translate(p["x"], Y(p["y"]))
            if p.get("rot"):
                c.rotate(-p["rot"])
            a = p.get("a", "start")
            if a == "middle":
                c.drawCentredString(0, 0, s)
            elif a == "end":
                c.drawRightString(0, 0, s)
            else:
                c.drawString(0, 0, s)
            c.restoreState()
    c.setDash()


def to_pdf(scene: dict[str, Any], meta: dict[str, Any], paper: str = "a3") -> bytes:
    from reportlab.pdfgen import canvas

    PW, PH = PAPER_PT.get(paper, PAPER_PT["a3"])
    M = 20.0
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PW, PH), invariant=1)
    c.setTitle(f"{meta.get('number', '')} {meta.get('rev', '')} {meta.get('title', '')}".strip())
    c.setAuthor(meta.get("company", ""))
    c.setSubject(L.DISCLAIMER)
    tb_y = PH - M - 6 - TB_H
    ax, ay = M + 10, M + 10
    aw, ah = PW - 2 * M - 20, tb_y - 8 - ay
    sheets = pages(scene, aw, ah)
    for n, (s, items, w, h) in enumerate(sheets, 1):
        dx = ax + (aw - w * s) / 2
        prims = [{"t": "rect", "x": M, "y": M, "w": PW - 2 * M, "h": PH - 2 * M, "rx": 0, "c": "ink", "sw": 1.2, "d": 0, "f": None}]
        prims += transform(L.expand({"items": items}), dx, ay, s)
        prims += title_block(M + 6, tb_y, PW - 2 * M - 12, meta, f"{n} of {len(sheets)}")
        _pdf_draw(c, prims, PH, PRINT)
        c.showPage()
    c.save()
    return buf.getvalue()
