/* JARVIS console - draws the device symbols of a drawing on a floor plan (device layouts and zone charts, web/drawings.js).
 *
 * There is ONE symbol set for every drawing Jarvis makes: jarvis/services/schematic_symbols.py (shared with system schematics). This
 * file holds no shapes of its own: the server sends the floor-plan subset (services/plan_drawings.symbol_library(), in the drawings
 * list and in every drawing) and this file only DRAWS it, so a smoke detector looks the same on a zone chart, a device layout, a loop
 * schematic and every export. They are clear, consistent symbols of the kind commonly used on UK fire and security drawings - NOT a
 * formal BS symbol set, and nothing here claims they are; every export carries its own legend.
 *
 * The data: { types: { key: { label, family, colour, rotates, items: [primitive...] } }, soft, zone_colours: [...] }. A primitive is in
 * a unit box -1..1 (y down): circle {cx, cy, r}, rect {x, y, w, h, rx}, line {x1, y1, x2, y2}, poly {pts: [[x, y]...], z: closed},
 * text {x, y, s, fs, w}; optional fill role f (paper | soft | ink | accent) and stroke role c. Roles become this device's family colour
 * (white / a soft tint for the paper / soft fills). Everything is checked here (known shape, finite numbers) and built with DOM APIs:
 * nothing is parsed as markup. A type that "rotates" (the camera) is drawn by the shared set pointing RIGHT; its view direction here
 * is degrees clockwise from UP, and it gets a light view cone.
 *
 * Exposes window.DrawingSymbols = { setData(data), ready(), TYPES, ZONE_COLOURS, label(type), colour(type), draw(parent, type, x, y,
 * size, opts), icon(type, px) }.
 */
(() => {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  const FONT = "'IBM Plex Sans', 'Segoe UI', system-ui, Arial, sans-serif";
  const SHAPES = new Set(["circle", "rect", "line", "poly", "text"]);
  const SW = 2 / 22;          // the shared set's stroke width (size / 22) in unit-box units
  let DATA = { types: {}, soft: "#eef1f5", zone_colours: ["#e53935", "#1e88e5", "#43a047", "#fb8c00"] };
  const api = { TYPES: [], ZONE_COLOURS: DATA.zone_colours };

  const fin = (v) => typeof v === "number" && Number.isFinite(v) && Math.abs(v) <= 4;
  const hex = (v, d) => (typeof v === "string" && /^#[0-9a-f]{6}$/i.test(v) ? v : d);
  function okItem(p) {
    if (!p || !SHAPES.has(p.t)) return false;
    if (p.t === "circle") return [p.cx ?? 0, p.cy ?? 0, p.r].every(fin);
    if (p.t === "rect") return [p.x, p.y, p.w, p.h].every(fin);
    if (p.t === "line") return [p.x1, p.y1, p.x2, p.y2].every(fin);
    if (p.t === "poly") return Array.isArray(p.pts) && p.pts.length >= 2 && p.pts.length <= 64 && p.pts.every((q) => Array.isArray(q) && q.length === 2 && q.every(fin));
    return [p.x ?? 0, p.y ?? 0, p.fs].every(fin) && typeof p.s === "string" && p.s.length <= 6;
  }

  function setData(d) {
    if (!d || typeof d !== "object" || !d.types || typeof d.types !== "object") return;
    const types = {};
    for (const [k, v] of Object.entries(d.types)) {
      if (!/^[a-z_]{1,24}$/.test(k) || !v || !Array.isArray(v.items)) continue;
      types[k] = { label: String(v.label || k).slice(0, 60), colour: hex(v.colour, "#37474f"), rotates: !!v.rotates, items: v.items.filter(okItem).slice(0, 40) };
    }
    const zones = Array.isArray(d.zone_colours) ? d.zone_colours.map((c) => hex(c, null)).filter(Boolean) : [];
    DATA = { types, soft: hex(d.soft, "#eef1f5"), zone_colours: zones.length ? zones : DATA.zone_colours };
    api.TYPES = Object.keys(types);
    api.ZONE_COLOURS = DATA.zone_colours;
  }

  const label = (type) => (DATA.types[type] || {}).label || "Device";
  const colour = (type) => (DATA.types[type] || {}).colour || "#37474f";
  function el(name, attrs) {
    const n = document.createElementNS(SVGNS, name);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    return n;
  }

  /** Draw one symbol into `parent` (an SVG element), centred on (x, y), `size` units across. opts: {direction (deg), cone (bool)}. */
  function draw(parent, type, x, y, size, opts = {}) {
    const def = DATA.types[type];
    if (!def) return null;
    const c = def.colour, h = size / 2;
    const fillOf = (role) => role === "paper" ? "#ffffff" : role === "soft" ? DATA.soft : (role === "ink" || role === "accent") ? c : "none";
    const strokeOf = (role) => role === "paper" ? "#ffffff" : c;
    const g = el("g", { class: "sym", transform: `translate(${x} ${y})` });
    const turn = def.rotates && Number.isFinite(opts.direction);
    if (turn && opts.cone !== false) {
      const a = (opts.direction - 90) * Math.PI / 180, r = size * 3, hw = Math.PI / 6;
      const p1 = [Math.cos(a - hw) * r, Math.sin(a - hw) * r], p2 = [Math.cos(a + hw) * r, Math.sin(a + hw) * r];
      g.appendChild(el("path", { d: `M0 0 L${p1[0]} ${p1[1]} A${r} ${r} 0 0 1 ${p2[0]} ${p2[1]} Z`, fill: c, "fill-opacity": 0.14,
                                  stroke: c, "stroke-opacity": 0.45, "stroke-width": Math.max(0.6, size * 0.03) }));
    }
    const inner = el("g", { transform: `${turn ? `rotate(${opts.direction - 90}) ` : ""}scale(${h})` });
    for (const p of def.items) {
      const stroke = { stroke: strokeOf(p.c), "stroke-width": SW, "stroke-linejoin": "round", "stroke-linecap": "round" };
      if (p.t === "circle") inner.appendChild(el("circle", { cx: p.cx || 0, cy: p.cy || 0, r: p.r, fill: fillOf(p.f ?? "paper"), ...stroke }));
      else if (p.t === "rect") inner.appendChild(el("rect", { x: p.x, y: p.y, width: p.w, height: p.h, rx: fin(p.rx) ? p.rx : 0, fill: fillOf(p.f ?? "paper"), ...stroke }));
      else if (p.t === "line") inner.appendChild(el("line", { x1: p.x1, y1: p.y1, x2: p.x2, y2: p.y2, ...stroke }));
      else if (p.t === "poly") inner.appendChild(el(p.z ? "polygon" : "polyline", { points: p.pts.map((q) => q.join(",")).join(" "), fill: p.z ? fillOf(p.f ?? "paper") : "none", ...stroke }));
      else if (p.t === "text") {
        const t = el("text", { x: p.x || 0, y: (p.y || 0) + p.fs * 0.36, fill: c, "font-size": p.fs, "font-weight": Number(p.w) >= 600 ? 700 : 400,
                                "font-family": FONT, "text-anchor": "middle" });
        t.textContent = p.s;
        inner.appendChild(t);
      }
    }
    g.appendChild(inner);
    parent.appendChild(g);
    return g;
  }

  /** A small standalone <svg> of one symbol (the palette and the legend). */
  function icon(type, px = 28) {
    const svg = el("svg", { viewBox: "-14 -14 28 28", width: px, height: px, "aria-hidden": "true", focusable: "false", class: "sym-icon" });
    draw(svg, type, 0, 0, 24, { cone: false });
    return svg;
  }

  Object.assign(api, { setData, ready: () => api.TYPES.length > 0, label, colour, draw, icon });
  window.DrawingSymbols = api;
})();
