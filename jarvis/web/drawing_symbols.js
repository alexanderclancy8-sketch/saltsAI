/* JARVIS console - the symbol library for drawings on floor plans (device layouts and zone charts).
 *
 * ONE file, used twice: the editor (web/drawings.js) draws every device with it in the browser, and the server
 * (services/plan_drawings.py) reads the JSON block below - exactly the text between the two marker comments - to draw the
 * same symbols into the exported PDF / PNG. So a symbol is changed here and nowhere else.
 *
 * These are clear, consistent symbols of the kind commonly used on UK fire and security layouts (letters in circles for
 * detectors, squares for manual / interface devices, a speaker for sounders, rays for visual alarms). They are NOT a formal
 * BS symbol set and nothing here claims they are; every exported drawing carries its own legend.
 *
 * Geometry: each symbol lives in a 24 x 24 box centred on (0, 0), y pointing DOWN (SVG's way). Primitives, all plain data:
 *   ["circle", cx, cy, r, style]        style: "s" = outline in the symbol colour on a white fill (legible over plan lines),
 *   ["rect", x, y, w, h, style]                 "f" = filled with the symbol colour, "n" = outline with no fill
 *   ["poly", [x1, y1, x2, y2, ...], style]   (closed)
 *   ["line", x1, y1, x2, y2]
 *   ["text", x, y, "TXT", size]          centred on (x, y), bold sans-serif, in the symbol colour
 * A type with "rotates": true (the CCTV camera) is drawn pointing UP and turned by the device's view direction (degrees,
 * clockwise, 0 = up on the plan as uploaded); the renderers add a light view cone in front of it.
 * Keep the block strict JSON (double quotes, no trailing commas, no comments): Python parses it with json.loads.
 *
 * Another drawing feature (system schematics) may bring its own symbol file; if both land, reconcile them into one.
 *
 * Exposes window.DrawingSymbols = { DATA, TYPES, ZONE_COLOURS, label(type), colour(type), draw(parent, type, x, y, size, opts), icon(type, px) }.
 */
(() => {
  "use strict";
  const DATA = /* DRAWING-SYMBOLS-JSON-START */
{
  "version": 1,
  "box": 24,
  "stroke": 1.7,
  "colours": {"fire": "#c62828", "security": "#1565c0", "control": "#37474f"},
  "zone_colours": ["#e53935", "#1e88e5", "#43a047", "#fb8c00", "#8e24aa", "#00897b", "#d81b60", "#6d4c41",
                   "#3949ab", "#7cb342", "#f4511e", "#546e7a"],
  "types": {
    "smoke": {"label": "Smoke detector", "group": "fire",
              "prims": [["circle", 0, 0, 10, "s"], ["text", 0, 0, "S", 12]]},
    "heat": {"label": "Heat detector", "group": "fire",
             "prims": [["circle", 0, 0, 10, "s"], ["text", 0, 0, "H", 12]]},
    "multi": {"label": "Multi-sensor detector", "group": "fire",
              "prims": [["circle", 0, 0, 10, "s"], ["text", 0, 0, "M", 12]]},
    "call_point": {"label": "Manual call point", "group": "fire",
                   "prims": [["rect", -9, -9, 18, 18, "s"], ["circle", 0, 0, 4.5, "f"]]},
    "sounder": {"label": "Sounder", "group": "fire",
                "prims": [["poly", [-10, -4, -5, -4, 3, -10, 3, 10, -5, 4, -10, 4], "s"],
                          ["line", 6, -5, 10, -8], ["line", 7, 0, 11, 0], ["line", 6, 5, 10, 8]]},
    "vad": {"label": "Visual alarm device (beacon)", "group": "fire",
            "prims": [["circle", 0, 0, 6, "f"], ["line", 0, -8, 0, -11.5], ["line", 0, 8, 0, 11.5], ["line", -8, 0, -11.5, 0],
                      ["line", 8, 0, 11.5, 0], ["line", -5.7, -5.7, -8.1, -8.1], ["line", 5.7, -5.7, 8.1, -8.1],
                      ["line", -5.7, 5.7, -8.1, 8.1], ["line", 5.7, 5.7, 8.1, 8.1]]},
    "sounder_beacon": {"label": "Sounder-beacon", "group": "fire",
                       "prims": [["poly", [-11, -3, -7, -3, -1, -8, -1, 8, -7, 3, -11, 3], "s"], ["circle", 6.5, 0, 4, "f"],
                                 ["line", 6.5, -6, 6.5, -9], ["line", 6.5, 6, 6.5, 9], ["line", 11, -4, 12, -6], ["line", 11, 4, 12, 6]]},
    "panel": {"label": "Control panel", "group": "control",
              "prims": [["rect", -12, -8, 24, 16, "s"], ["rect", -12, -8, 24, 4.5, "f"], ["text", 0, 2.3, "PANEL", 6.5]]},
    "repeater": {"label": "Repeater panel", "group": "control",
                 "prims": [["rect", -12, -8, 24, 16, "s"], ["text", 0, 0, "REP", 8]]},
    "interface": {"label": "Interface / I-O unit", "group": "fire",
                  "prims": [["rect", -10, -10, 20, 20, "s"], ["text", 0, 0, "I/O", 8]]},
    "beam": {"label": "Beam detector", "group": "fire",
             "prims": [["rect", -11, -7, 22, 14, "s"], ["line", -7, 0, 4, 0], ["poly", [3, -3.5, 8, 0, 3, 3.5], "f"]]},
    "aspirating": {"label": "Aspirating sampling point", "group": "fire",
                   "prims": [["circle", 0, 0, 9, "s"], ["circle", 0, 0, 3.2, "f"], ["line", -12, 0, -9, 0], ["line", 9, 0, 12, 0]]},
    "door_holder": {"label": "Door holder", "group": "fire",
                    "prims": [["rect", -10, -10, 20, 20, "s"], ["text", 0, 0, "DH", 9]]},
    "pir": {"label": "PIR detector", "group": "security",
            "prims": [["poly", [0, 9, -11, -4, -8, -8, -3, -10.5, 3, -10.5, 8, -8, 11, -4], "s"], ["text", 0, -3, "PIR", 6.5]]},
    "door_contact": {"label": "Door contact", "group": "security",
                     "prims": [["rect", -11, -5, 9, 10, "s"], ["rect", 2, -5, 9, 10, "f"]]},
    "keypad": {"label": "Keypad", "group": "security",
               "prims": [["rect", -9, -11, 18, 22, "s"], ["rect", -6, -8, 12, 4, "n"],
                         ["circle", -4, 1, 1.4, "f"], ["circle", 0, 1, 1.4, "f"], ["circle", 4, 1, 1.4, "f"],
                         ["circle", -4, 6, 1.4, "f"], ["circle", 0, 6, 1.4, "f"], ["circle", 4, 6, 1.4, "f"]]},
    "cctv": {"label": "CCTV camera", "group": "security", "rotates": true,
             "prims": [["rect", -5, -3, 10, 13, "s"], ["poly", [-3, -3, -7, -11, 7, -11, 3, -3], "s"]]},
    "access_reader": {"label": "Access control reader", "group": "security",
                      "prims": [["rect", -8, -11, 16, 22, "s"], ["line", -4, -6, 4, -6], ["text", 0, 3, "AC", 7.5]]}
  }
}
  /* DRAWING-SYMBOLS-JSON-END */;

  const SVGNS = "http://www.w3.org/2000/svg";
  const TYPES = Object.keys(DATA.types);
  const FONT = "'IBM Plex Sans', 'Segoe UI', system-ui, Arial, sans-serif";
  const label = (type) => (DATA.types[type] || {}).label || "Device";
  const colour = (type) => DATA.colours[(DATA.types[type] || {}).group] || DATA.colours.control;

  function el(name, attrs) {
    const n = document.createElementNS(SVGNS, name);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    return n;
  }

  /** Draw one symbol into `parent` (an SVG element), centred on (x, y), `size` units across. opts: {direction (deg), cone (bool)}.
   *  Built with DOM APIs only: the type picks a symbol from the closed list above, nothing from a drawing is parsed as markup. */
  function draw(parent, type, x, y, size, opts = {}) {
    const def = DATA.types[type];
    if (!def) return null;
    const c = colour(type), k = size / DATA.box;
    const g = el("g", { class: "sym", transform: `translate(${x} ${y})` });
    if (def.rotates && Number.isFinite(opts.direction) && opts.cone !== false) {
      // a light view cone in front of the camera: 60 degrees wide, three symbol sizes long
      const a = (opts.direction - 90) * Math.PI / 180, r = size * 3, h = Math.PI / 6;
      const p1 = [Math.cos(a - h) * r, Math.sin(a - h) * r], p2 = [Math.cos(a + h) * r, Math.sin(a + h) * r];
      g.appendChild(el("path", { d: `M0 0 L${p1[0]} ${p1[1]} A${r} ${r} 0 0 1 ${p2[0]} ${p2[1]} Z`, fill: c, "fill-opacity": 0.14,
                                  stroke: c, "stroke-opacity": 0.45, "stroke-width": Math.max(0.6, k * 0.8) }));
    }
    const inner = el("g", { transform: `${def.rotates && Number.isFinite(opts.direction) ? `rotate(${opts.direction}) ` : ""}scale(${k})` });
    const sw = DATA.stroke;
    const paint = (style) => style === "f" ? { fill: c, stroke: c, "stroke-width": sw * 0.5 }
      : style === "n" ? { fill: "none", stroke: c, "stroke-width": sw } : { fill: "#ffffff", stroke: c, "stroke-width": sw };
    for (const p of def.prims) {
      const kind = p[0];
      if (kind === "circle") inner.appendChild(el("circle", { cx: p[1], cy: p[2], r: p[3], ...paint(p[4]) }));
      else if (kind === "rect") inner.appendChild(el("rect", { x: p[1], y: p[2], width: p[3], height: p[4], ...paint(p[5]) }));
      else if (kind === "poly") inner.appendChild(el("polygon", { points: p[1].join(" "), "stroke-linejoin": "round", ...paint(p[2]) }));
      else if (kind === "line") inner.appendChild(el("line", { x1: p[1], y1: p[2], x2: p[3], y2: p[4], stroke: c, "stroke-width": sw, "stroke-linecap": "round" }));
      else if (kind === "text") {
        const t = el("text", { x: p[1], y: p[2], fill: c, "font-size": p[4], "font-weight": 700, "font-family": FONT, "text-anchor": "middle",
                                "dominant-baseline": "central" });
        t.textContent = String(p[3]);
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
    draw(svg, type, 0, 0, 24, { direction: 0, cone: false });
    return svg;
  }

  window.DrawingSymbols = { DATA, TYPES, ZONE_COLOURS: DATA.zone_colours, label, colour, draw, icon };
})();
