/* JARVIS console - charts.
 *
 * Draws a chart from a validated JSON spec as INLINE SVG built with DOM APIs. The spec is DATA: nothing in it is ever parsed as
 * HTML or script - every label, title and series name goes in through textContent / setAttribute, never innerHTML - and the spec is
 * checked again here (closed chart types, the same caps as jarvis/services/charts.py, finite numbers) before anything is drawn.
 *
 *   spec = { type: "bar"|"line"|"pie"|"donut"|"stacked_bar", title, x_label, y_label, unit: "number"|"gbp"|"percent",
 *            series: [{ name, points: [{ label, value }] }] }
 *
 * What a chart gets: the bars/lines/slices; a legend (always for 2+ series, and for pie/donut); "nice" axis ticks (1/2/5 x 10^n) with
 * money / percent formatting; a tooltip on hover, touch and keyboard focus (real <button>s laid over the marks, arrow keys move
 * between them); a role="img" summary for screen readers; a collapsible data table with every value; "Download PNG" (the SVG is
 * drawn onto a canvas in the browser, exactly like the advert export in hud.js) and "Copy as CSV" (cells that look like spreadsheet
 * formulas are neutralised). Colours come from the console's CSS tokens (--panel, --text, --muted, --line) plus a fixed, colour-vision
 * checked categorical palette stepped for each theme; the chart redraws when the theme changes and when its box changes size.
 *
 * Exposes window.JarvisCharts = { mount, validate, niceTicks, toCsv, toPng, formatValue, summary, LIMITS }.
 */
(() => {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  const TYPES = ["bar", "line", "pie", "donut", "stacked_bar"];
  const UNITS = ["number", "gbp", "percent"];
  const TYPE_NAME = { bar: "Bar", line: "Line", pie: "Pie", donut: "Donut", stacked_bar: "Stacked bar" };
  // type -> [max series, max points per series]  (mirrors jarvis/services/charts.py)
  const LIMITS = { bar: [1, 24], line: [6, 60], pie: [1, 12], donut: [1, 12], stacked_bar: [8, 24] };
  const MAX_TOTAL = 240, MAX_TITLE = 120, MAX_AXIS = 60, MAX_LABEL = 60, MAX_NAME = 40, MAX_VALUE = 1e15;
  const EXPORT_W = 1600, EXPORT_H = 900;
  const FONT = "'IBM Plex Sans', 'Segoe UI', system-ui, Arial, sans-serif";
  // Eight categorical hues in a fixed order (never cycled), validated for colour-vision separation on the console's panel colour.
  const PALETTE = {
    light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
  };
  const NEUTRAL = { light: ["#76839a", "#9aa5b8", "#5f6c82", "#b4bdcc"], dark: ["#8fa3c4", "#6f84a8", "#aebfd9", "#586e94"] };
  const CONTROL = /[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁠-⁯﻿]/g;

  // ------------------------------------------------------------------------------------------------- validation
  const clean = (v, n) => {
    const s = String(v ?? "").replace(CONTROL, " ").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n).trimEnd() + "…" : s;
  };
  function num(v) {
    if (typeof v === "boolean" || v === null || v === undefined) return null;
    const n = typeof v === "number" ? v : typeof v === "string" ? Number(v.trim().replace(/,/g, "").replace(/^£/, "")) : NaN;
    return Number.isFinite(n) && Math.abs(n) <= MAX_VALUE ? n : null;
  }
  /** {ok:true, spec} (a normalised copy) or {ok:false, error}. Never throws. */
  function validate(raw) {
    try {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { ok: false, error: "The chart wasn't in the expected shape." };
      const type = String(raw.type || "").toLowerCase();
      if (!TYPES.includes(type)) return { ok: false, error: "Unknown chart type." };
      const title = clean(raw.title, MAX_TITLE);
      if (!title) return { ok: false, error: "The chart has no title." };
      const unit = UNITS.includes(raw.unit) ? raw.unit : "number";
      if (!Array.isArray(raw.series) || !raw.series.length) return { ok: false, error: "The chart has no data." };
      const [maxSeries, maxPoints] = LIMITS[type];
      if (raw.series.length > maxSeries) return { ok: false, error: "Too many series for this chart." };
      const series = [], cats = [];
      let total = 0;
      for (const [i, s] of raw.series.entries()) {
        if (!s || typeof s !== "object" || !Array.isArray(s.points) || !s.points.length) return { ok: false, error: "A series has no points." };
        if (s.points.length > maxPoints) return { ok: false, error: "Too many points for this chart." };
        total += s.points.length;
        if (total > MAX_TOTAL) return { ok: false, error: "Too many points." };
        const points = [], seen = new Set();
        for (const p of s.points) {
          const label = clean(p && p.label, MAX_LABEL), value = num(p && p.value);
          if (!label || value === null || seen.has(label)) return { ok: false, error: "A point has no usable label or value." };
          if (value < 0 && (type === "pie" || type === "donut" || type === "stacked_bar")) return { ok: false, error: "This chart can't show negative values." };
          seen.add(label);
          points.push({ label, value });
          if (!cats.includes(label)) cats.push(label);
        }
        series.push({ name: clean(s.name, MAX_NAME) || (raw.series.length === 1 ? title.slice(0, MAX_NAME) : `Series ${i + 1}`), points });
      }
      if (cats.length > maxPoints) return { ok: false, error: "Too many labels for this chart." };
      if ((type === "pie" || type === "donut") && !(series[0].points.reduce((a, p) => a + p.value, 0) > 0)) return { ok: false, error: "The values add up to nothing." };
      return { ok: true, spec: { type, title, unit, x_label: clean(raw.x_label, MAX_AXIS), y_label: clean(raw.y_label, MAX_AXIS), series, categories: cats } };
    } catch (e) {
      return { ok: false, error: "The chart couldn't be read." };
    }
  }

  // ------------------------------------------------------------------------------------------------- numbers
  function niceNum(range, round) {
    const exp = Math.floor(Math.log10(range)), f = range / 10 ** exp;
    const nf = round ? (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) : (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10);
    return nf * 10 ** exp;
  }
  /** Axis ticks on 'nice' numbers (1, 2, 5 x 10^n) covering [min, max]. */
  function niceTicks(min, max, count = 5) {
    if (!Number.isFinite(min) || !Number.isFinite(max)) { min = 0; max = 1; }
    if (min > max) [min, max] = [max, min];
    if (min === max) { if (min === 0) max = 1; else if (min > 0) min = 0; else max = 0; }
    const range = niceNum(max - min, false), step = niceNum(range / Math.max(1, count - 1), true);
    const lo = Math.floor(min / step + 1e-9) * step, hi = Math.ceil(max / step - 1e-9) * step, ticks = [];
    for (let v = lo, i = 0; v <= hi + step * 0.5 && i < 50; v += step, i++) ticks.push(Math.round(v / step) * step);
    return { ticks: ticks.map((t) => Number(t.toPrecision(12))), min: ticks[0], max: ticks[ticks.length - 1], step };
  }
  const group = (n, dp) => new Intl.NumberFormat("en-GB", { minimumFractionDigits: dp, maximumFractionDigits: dp }).format(n);
  const trim = (n, max) => new Intl.NumberFormat("en-GB", { maximumFractionDigits: max }).format(n);
  const pct = (x) => `${new Intl.NumberFormat("en-GB", { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(x)}%`;
  /** A value as text. mode "full" = tooltips and tables, "tick" = a short axis label (step decides how many decimals). */
  function formatValue(v, unit, mode = "full", step = 1) {
    if (!Number.isFinite(v)) return "-";
    const dec = Math.max(0, Math.min(4, -Math.floor(Math.log10(step || 1) + 1e-9)));
    if (mode === "full") {
      if (unit === "gbp") return (v < 0 ? "-£" : "£") + group(Math.abs(v), 2);
      if (unit === "percent") return trim(v, 1) + "%";
      return trim(v, 4);
    }
    const a = Math.abs(v), sign = v < 0 ? "-" : "";
    const compact = (x) => {
      if (a >= 1e6 && step >= 1e5) return `${trim(x / 1e6, 2)}m`;
      if (a >= 1e3 && step >= 1e3) return `${trim(x / 1e3, 1)}k`;
      return trim(x, dec);
    };
    if (unit === "gbp") return `${sign}£${compact(a)}`;
    if (unit === "percent") return `${trim(v, dec)}%`;
    return `${sign}${compact(a)}`;
  }

  // ------------------------------------------------------------------------------------------------- colours and text
  function parseRgb(css) {
    const m = /rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)/.exec(css || "");
    return m ? [+m[1], +m[2], +m[3]] : null;
  }
  const luminance = ([r, g, b]) => 0.2126 * r + 0.7152 * g + 0.0722 * b;
  function colours(from) {
    const cs = getComputedStyle(from || document.documentElement);
    const get = (n, d) => (cs.getPropertyValue(n) || "").trim() || d;
    const surface = get("--panel", "#101f38");
    // custom properties give the raw token text; resolve it to rgb through a probe so #hex / rgb() both work
    const probe = document.createElement("span");
    probe.style.color = surface; probe.style.display = "none"; (from || document.body).appendChild(probe);
    const rgb = parseRgb(getComputedStyle(probe).color); probe.remove();
    const dark = rgb ? luminance(rgb) < 140 : true;
    const mode = dark ? "dark" : "light";
    return { dark, surface, text: get("--text", dark ? "#e6eefb" : "#0a1830"), muted: get("--muted", dark ? "#8fa3c4" : "#435a80"),
             line: get("--line", dark ? "#1d3155" : "#bccde6"), series: PALETTE[mode], neutral: NEUTRAL[mode] };
  }
  const slotColour = (c, i, name) => (name === "Other" ? c.neutral[0] : i < c.series.length ? c.series[i] : c.neutral[(i - c.series.length) % c.neutral.length]);
  let mctx = null;
  function measure(text, size, weight = 400) {
    try {
      mctx = mctx || document.createElement("canvas").getContext("2d");
      mctx.font = `${weight} ${size}px ${FONT}`;
      return mctx.measureText(text).width;
    } catch (e) { return String(text).length * size * 0.58; }
  }
  function ellipsize(text, size, maxW) {
    if (measure(text, size) <= maxW) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (measure(text.slice(0, mid) + "…", size) <= maxW) lo = mid; else hi = mid - 1; }
    return lo > 0 ? text.slice(0, lo).trimEnd() + "…" : "…";
  }
  function E(tag, attrs, parent) {
    const el = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}) el.setAttribute(k, String(attrs[k]));
    if (parent) parent.appendChild(el);
    return el;
  }
  function T(parent, x, y, text, o = {}) {
    const t = E("text", { x, y, "font-family": FONT, "font-size": o.size || 12, "font-weight": o.weight || 400, fill: o.fill, "text-anchor": o.anchor || "start",
                          "dominant-baseline": o.baseline || "alphabetic" }, parent);
    t.textContent = text;
    return t;
  }

  // ------------------------------------------------------------------------------------------------- the drawing
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  /** A bar from the baseline to its data end, with the data end rounded (r px) and the baseline end square. dir: up|down|left|right. */
  function barPath(x, y, w, h, r, dir) {
    r = Math.max(0, Math.min(r, w / 2, h / 2));
    if (dir === "up") return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
    if (dir === "down") return `M${x},${y}V${y + h - r}Q${x},${y + h} ${x + r},${y + h}H${x + w - r}Q${x + w},${y + h} ${x + w},${y + h - r}V${y}Z`;
    if (dir === "right") return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
    return `M${x + w},${y}H${x + r}Q${x},${y} ${x},${y + r}V${y + h - r}Q${x},${y + h} ${x + r},${y + h}H${x + w}Z`;
  }
  function summary(spec) {
    const name = TYPE_NAME[spec.type];
    const rows = spec.series[0].points;
    const fv = (v) => formatValue(v, spec.unit);
    let body;
    if (spec.type === "pie" || spec.type === "donut") {
      const total = sum(rows.map((p) => p.value)), top = rows.reduce((a, p) => (p.value > a.value ? p : a), rows[0]);
      body = `${rows.length} slices, total ${fv(total)}; largest ${top.label} ${fv(top.value)} (${pct((top.value / total) * 100)}).`;
    } else if (spec.series.length === 1) {
      const hi = rows.reduce((a, p) => (p.value > a.value ? p : a), rows[0]), lo = rows.reduce((a, p) => (p.value < a.value ? p : a), rows[0]);
      body = `${rows.length} ${spec.type === "line" ? "points" : "bars"}; highest ${hi.label} ${fv(hi.value)}, lowest ${lo.label} ${fv(lo.value)}.`;
    } else {
      body = `${spec.series.length} series (${spec.series.map((s) => s.name).join(", ")}) over ${spec.categories.length} categories.`;
    }
    return `${name} chart: ${spec.title}. ${body} The full data is in the table below the chart.`;
  }

  /** Build the SVG for a spec. Returns { svg, hits, legend, height }; hits describe the focusable regions (px, relative to the svg). */
  function build(spec, c, o) {
    const W = o.width, exportMode = !!o.export;
    const fs = o.fontScale || 1, base = 12 * fs;
    const svg = E("svg", { xmlns: SVGNS, width: W, height: 10, viewBox: `0 0 ${W} 10`, "font-family": FONT });
    const hits = [], marks = [];
    const kind = spec.type;
    let y0 = 0;                                          // top of what we draw (export puts the title above)
    if (exportMode) {
      E("rect", { x: 0, y: 0, width: W, height: o.height, fill: c.surface }, svg);
      const tsz = 30; T(svg, 40, 56, ellipsize(spec.title, tsz, W - 80), { size: tsz, weight: 600, fill: c.text });
      y0 = 84;
    }
    const pad = exportMode ? 40 : 0;
    const ax = (t, step) => formatValue(t, spec.unit, "tick", step);
    const cats = spec.categories;
    const fvFull = (v) => formatValue(v, spec.unit);
    let height;

    if (kind === "pie" || kind === "donut") {
      const rows = spec.series[0].points, total = sum(rows.map((p) => p.value));
      const size = exportMode ? Math.min(o.height - y0 - 40, W * 0.5) : Math.min(W, 300);
      const cx = exportMode ? pad + size / 2 + 20 : W / 2, cy = y0 + size / 2 + 8, R = size / 2 - 4;
      let a0 = -Math.PI / 2;
      rows.forEach((p, i) => {
        const frac = p.value / total, a1 = a0 + frac * Math.PI * 2, col = slotColour(c, i, p.label);
        let el;
        if (rows.length === 1 || frac >= 0.9999) {
          el = E("circle", { cx, cy, r: R, fill: col }, svg);
        } else {
          const large = a1 - a0 > Math.PI ? 1 : 0;
          const pt = (a, r) => `${(cx + Math.cos(a) * r).toFixed(2)},${(cy + Math.sin(a) * r).toFixed(2)}`;
          el = E("path", { d: `M${cx},${cy}L${pt(a0, R)}A${R},${R} 0 ${large} 1 ${pt(a1, R)}Z`, fill: col }, svg);
        }
        el.setAttribute("stroke", c.surface); el.setAttribute("stroke-width", "2"); el.setAttribute("class", "chart-mark");
        const mid = (a0 + a1) / 2, hr = kind === "donut" ? R * 0.79 : R * 0.62;
        marks.push(el);
        hits.push({ idx: i, x: cx + Math.cos(mid) * hr - 14, y: cy + Math.sin(mid) * hr - 14, w: 28, h: 28, ax: cx + Math.cos(mid) * R * 0.8, ay: cy + Math.sin(mid) * R * 0.8,
                    label: `${p.label}: ${fvFull(p.value)}, ${pct(frac * 100)}`, mark: el });
        a0 = a1;
      });
      if (kind === "donut") {
        E("circle", { cx, cy, r: R * 0.58, fill: c.surface }, svg);
        T(svg, cx, cy - 2, fvFull(total), { size: Math.round(17 * fs), weight: 600, fill: c.text, anchor: "middle" });
        T(svg, cx, cy + 16 * fs, "Total", { size: base, fill: c.muted, anchor: "middle" });
      }
      height = y0 + size + 16;
      if (exportMode) {
        let ly = y0 + 20; const lx = cx + R + 60;
        rows.forEach((p, i) => {
          E("rect", { x: lx, y: ly - 12, width: 16, height: 16, rx: 3, fill: slotColour(c, i, p.label) }, svg);
          T(svg, lx + 26, ly, `${ellipsize(p.label, 20, 360)}   ${fvFull(p.value)} (${pct((p.value / total) * 100)})`, { size: 20, fill: c.text });
          ly += 34;
        });
      }
    } else {
      // ---- cartesian: bar / stacked_bar / line
      const stacked = kind === "stacked_bar", isLine = kind === "line", n = cats.length;
      const lookup = spec.series.map((s) => new Map(s.points.map((p) => [p.label, p.value])));
      const val = (si, ci) => lookup[si].get(cats[ci]);
      const colTotals = cats.map((_, ci) => sum(spec.series.map((_s, si) => val(si, ci) || 0)));
      const all = spec.series.flatMap((s) => s.points.map((p) => p.value));
      let lo, hi;
      if (stacked) { lo = 0; hi = Math.max(...colTotals); }
      else if (isLine) { lo = Math.min(...all); hi = Math.max(...all); if (lo >= 0) lo = 0; }
      else { lo = Math.min(0, ...all); hi = Math.max(0, ...all); }
      let nt = niceTicks(lo, hi, 5), step = nt.step;
      let tickLabels = nt.ticks.map((t) => ax(t, step));
      const maxTick = Math.max(...tickLabels.map((t) => measure(t, base)));
      const labelSz = base;
      const maxLabel = Math.max(...cats.map((l) => measure(l, labelSz)));
      const top = y0 + (spec.y_label ? 28 * fs : 8);
      // vertical columns if every category's label fits under its column, else horizontal rows with the labels on the left
      const left0 = pad + maxTick + 12, right0 = W - pad - 12;
      const slot0 = (right0 - left0) / Math.max(1, n);
      const horizontal = !isLine && (n > 12 || slot0 < Math.min(maxLabel, 110) + 8 || slot0 < 28);
      if (horizontal) {
        // long labels get their own line above each bar (the whole width to read them in) instead of a squeezed column on the left
        const fullLabelW = Math.max(...cats.map((l) => measure(l, labelSz))) + 10, above = fullLabelW > W * 0.38;
        const labelW = above ? 0 : Math.min(fullLabelW + 12, W * 0.38);
        const left = pad + (above ? 4 : labelW), right = W - pad - 24 * fs - 28, plotW = Math.max(60, right - left);
        nt = niceTicks(lo, hi, Math.max(2, Math.min(6, Math.floor(plotW / 64)))); step = nt.step;            // fewer ticks when the plot is narrow
        tickLabels = nt.ticks.map((t) => ax(t, step));
        const rowH = Math.round((above ? 48 : 30) * fs), barH = Math.min(24, Math.round(18 * fs));
        const X = (v) => left + ((v - nt.min) / (nt.max - nt.min)) * plotW, x0 = X(0);
        const plotH = n * rowH, bottom = top + plotH;
        nt.ticks.forEach((t, i) => {
          E("line", { x1: X(t), x2: X(t), y1: top, y2: bottom, stroke: c.line, "stroke-width": 1 }, svg);
          const tw = measure(tickLabels[i], base); let tx = X(t), anchor = "middle";                    // the end labels stay inside the picture
          if (tx - tw / 2 < 2) { tx = 2; anchor = "start"; } else if (tx + tw / 2 > W - 2) { tx = W - 2; anchor = "end"; }
          T(svg, tx, bottom + 16 * fs, tickLabels[i], { size: base, fill: c.muted, anchor });
        });
        cats.forEach((cat, ci) => {
          const rowTop = top + ci * rowH, cy = above ? rowTop + rowH - barH / 2 - 6 : rowTop + rowH / 2;
          if (above) T(svg, left, rowTop + 15 * fs, ellipsize(cat, labelSz, W - pad * 2 - 8), { size: labelSz, fill: c.text });
          else T(svg, left - 8, cy, ellipsize(cat, labelSz, labelW - 12), { size: labelSz, fill: c.text, anchor: "end", baseline: "central" });
          let pos = 0, neg = 0, mk = [];
          spec.series.forEach((s, si) => {
            const v = val(si, ci); if (v === undefined) return;
            const col = slotColour(c, si, s.name), a = stacked ? (v >= 0 ? pos : neg) : 0, b = a + v;
            if (stacked) { if (v >= 0) pos = b; else neg = b; }
            const xa = X(Math.min(a, b)), xb = X(Math.max(a, b)), w = Math.max(1, xb - xa - (stacked ? 2 : 0));
            const last = !stacked || si === spec.series.length - 1 || spec.series.slice(si + 1).every((_s, k) => val(si + 1 + k, ci) === undefined);
            const d = barPath(xa + (stacked ? 1 : 0), cy - barH / 2, w, barH, last ? 4 : 0, v >= 0 ? "right" : "left");
            const el = E("path", { d, fill: col, class: "chart-mark" }, svg); mk.push(el);
          });
          const total = stacked ? colTotals[ci] : val(0, ci);
          const tip = X(stacked ? Math.max(colTotals[ci], 0) : Math.max(total, 0));
          if (n <= 12 || exportMode) T(svg, Math.max(x0, tip) + 6, cy, fvFull(total), { size: base, fill: c.muted, baseline: "central" });
          hits.push({ idx: ci, x: pad, y: cy - rowH / 2, w: W - pad * 2, h: rowH, ax: Math.min(W - 40, tip), ay: cy, label: hitLabel(spec, ci, fvFull), marks: mk });
        });
        height = bottom + (spec.x_label ? 52 : 30) * fs;
        if (spec.y_label) T(svg, pad, y0 + 12 * fs, spec.y_label, { size: base, fill: c.muted });
        if (spec.x_label) T(svg, left + plotW / 2, height - 6, spec.x_label, { size: base, fill: c.muted, anchor: "middle" });
      } else {
        const left = left0, right = right0, plotW = right - left;
        const plotH = exportMode ? Math.max(200, o.height - top - 130) : Math.round(Math.max(190, Math.min(340, W * 0.5)));
        const bottom = top + plotH;
        const Y = (v) => bottom - ((v - nt.min) / (nt.max - nt.min)) * plotH, y0v = Y(0);
        nt.ticks.forEach((t, i) => {
          E("line", { x1: left, x2: right, y1: Y(t), y2: Y(t), stroke: c.line, "stroke-width": 1 }, svg);
          T(svg, left - 8, Y(t), tickLabels[i], { size: base, fill: c.muted, anchor: "end", baseline: "central" });
        });
        const slot = plotW / n;
        const xAt = (ci) => (isLine ? left + (n === 1 ? plotW / 2 : 10 + (ci * (plotW - 20)) / (n - 1)) : left + slot * (ci + 0.5));
        const spacing = isLine ? (n === 1 ? plotW : (plotW - 20) / (n - 1)) : slot;
        const every = Math.max(1, Math.ceil((maxLabel + 14) / spacing));            // label every k-th category when they would collide
        cats.forEach((cat, ci) => {
          if (ci % every !== 0) return;
          const shown = ellipsize(cat, labelSz, Math.max(40, spacing * every - 6)), tw = measure(shown, labelSz);
          let lx = xAt(ci), anchor = "middle";                                      // keep the first and last labels inside the picture
          if (lx - tw / 2 < 2) { lx = 2; anchor = "start"; } else if (lx + tw / 2 > W - 2) { lx = W - 2; anchor = "end"; }
          T(svg, lx, bottom + 16 * fs, shown, { size: labelSz, fill: c.muted, anchor });
        });
        if (isLine) {
          const xs = cats.map((_, ci) => xAt(ci));
          const cross = E("line", { class: "chart-cross", x1: 0, x2: 0, y1: top, y2: bottom, stroke: c.muted, "stroke-width": 1, visibility: "hidden" }, svg);
          spec.series.forEach((s, si) => {
            const col = slotColour(c, si, s.name);
            const pts = cats.map((_, ci) => (val(si, ci) === undefined ? null : [xs[ci], Y(val(si, ci))]));
            let d = "", pen = false;
            pts.forEach((p) => { if (!p) { pen = false; return; } d += `${pen ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`; pen = true; });
            E("path", { d, fill: "none", stroke: col, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
            pts.forEach((p, ci) => {
              if (!p) return;
              if (n <= 31 || ci === n - 1) E("circle", { cx: p[0], cy: p[1], r: 4, fill: col, stroke: c.surface, "stroke-width": 2 }, svg);
            });
          });
          cats.forEach((_, ci) => {
            const w = Math.max(6, Math.min(n === 1 ? plotW : (plotW - 20) / Math.max(1, n - 1), 48));
            hits.push({ idx: ci, x: xs[ci] - w / 2, y: top, w, h: plotH, ax: xs[ci], ay: Y(Math.max(...spec.series.map((_s, si) => val(si, ci) ?? -Infinity))), cross, crossX: xs[ci],
                        label: hitLabel(spec, ci, fvFull) });
          });
        } else {
          const bw = Math.max(4, Math.min(24, slot * 0.7));
          cats.forEach((cat, ci) => {
            const x = xAt(ci) - bw / 2;
            let pos = 0, neg = 0, mk = [];
            spec.series.forEach((s, si) => {
              const v = val(si, ci); if (v === undefined) return;
              const col = slotColour(c, si, s.name), a = stacked ? (v >= 0 ? pos : neg) : 0, b = a + v;
              if (stacked) { if (v >= 0) pos = b; else neg = b; }
              const ya = Y(Math.max(a, b)), yb = Y(Math.min(a, b)), h = Math.max(1, yb - ya - (stacked ? 2 : 0));
              const last = !stacked || si === spec.series.length - 1 || spec.series.slice(si + 1).every((_s, k) => val(si + 1 + k, ci) === undefined);
              const el = E("path", { d: barPath(x, ya + (stacked ? 1 : 0), bw, h, last ? 4 : 0, v >= 0 ? "up" : "down"), fill: col, class: "chart-mark" }, svg); mk.push(el);
            });
            const total = stacked ? colTotals[ci] : val(0, ci), tipY = Y(Math.max(total, 0));
            if (n <= 12 && bw >= 14 && measure(fvFull(total), base) < slot - 4) T(svg, xAt(ci), total < 0 ? Y(total) + 15 * fs : tipY - 6, fvFull(total), { size: base, fill: c.muted, anchor: "middle" });
            hits.push({ idx: ci, x: xAt(ci) - Math.max(slot, 24) / 2, y: top, w: Math.max(slot, 24), h: plotH, ax: xAt(ci), ay: tipY, label: hitLabel(spec, ci, fvFull), marks: mk });
          });
          if (nt.min < 0) E("line", { x1: left, x2: right, y1: y0v, y2: y0v, stroke: c.muted, "stroke-width": 1 }, svg);
        }
        height = bottom + (spec.x_label ? 46 : 28) * fs;
        if (spec.y_label) T(svg, pad, y0 + 12 * fs, spec.y_label, { size: base, fill: c.muted });
        if (spec.x_label) T(svg, left + plotW / 2, height - 6, spec.x_label, { size: base, fill: c.muted, anchor: "middle" });
      }
      if (exportMode && spec.series.length > 1) {
        let lx = pad; const ly = o.height - 36;
        spec.series.forEach((s, si) => {
          E("rect", { x: lx, y: ly - 12, width: 16, height: 16, rx: 3, fill: slotColour(c, si, s.name) }, svg);
          const label = ellipsize(s.name, 20, 220); T(svg, lx + 24, ly, label, { size: 20, fill: c.text });
          lx += 24 + measure(label, 20) + 36;
        });
      }
    }
    if (exportMode) height = o.height;
    svg.setAttribute("height", String(height)); svg.setAttribute("viewBox", `0 0 ${W} ${height}`);
    return { svg, hits, height };
  }
  function hitLabel(spec, ci, fv) {
    const cat = spec.categories[ci];
    const parts = spec.series.map((s) => { const p = s.points.find((q) => q.label === cat); return p ? `${spec.series.length > 1 ? s.name + " " : ""}${fv(p.value)}` : null; }).filter(Boolean);
    return `${cat}: ${parts.join(", ")}`;
  }

  // ------------------------------------------------------------------------------------------------- CSV and PNG
  const csvCell = (v) => {
    let s = String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;                      // a label that would run as a spreadsheet formula is made plain text
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  /** The chart's data as CSV: one row per label, one column per series (blank where a series has no point). */
  function toCsv(spec) {
    const rows = [[spec.x_label || "Label", ...spec.series.map((s) => s.name)].map(csvCell).join(",")];
    for (const cat of spec.categories) {
      rows.push([csvCell(cat), ...spec.series.map((s) => { const p = s.points.find((q) => q.label === cat); return p ? String(p.value) : ""; })].join(","));
    }
    return rows.join("\r\n") + "\r\n";
  }
  /** Draw the chart onto a canvas exactly EXPORT_W x EXPORT_H and give back the PNG. */
  async function toPng(spec, from) {
    const c = colours(from);
    const { svg } = build(spec, c, { width: EXPORT_W, height: EXPORT_H, export: true, fontScale: 1.45 });
    const xml = new XMLSerializer().serializeToString(svg);
    const img = new Image();
    await new Promise((ok, no) => {
      img.onload = ok; img.onerror = () => no(new Error("the browser could not draw this chart"));
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
    });
    if (img.decode) { try { await img.decode(); } catch (e) { /* drawn below anyway */ } }
    const canvas = document.createElement("canvas"); canvas.width = EXPORT_W; canvas.height = EXPORT_H;
    canvas.getContext("2d").drawImage(img, 0, 0, EXPORT_W, EXPORT_H);
    return await new Promise((ok, no) => {
      try { canvas.toBlob((b) => (b ? ok({ blob: b, width: EXPORT_W, height: EXPORT_H }) : no(new Error("the picture came out empty"))), "image/png"); } catch (e) { no(e); }
    });
  }
  function download(blob, filename) {
    const url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }
  async function copyText(text) {
    try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; } } catch (e) { /* fall through */ }
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }

  // ------------------------------------------------------------------------------------------------- the component
  const H = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const slug = (s) => (String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50) || "chart");

  /** Draw `raw` (a chart spec) into `host`. Returns { destroy, spec } or null when the spec is not acceptable (a note is shown instead). */
  function mount(host, raw, opts = {}) {
    host.textContent = "";
    const v = validate(raw);
    if (!v.ok) {
      const p = H("p", "chart-status bad", `This chart can't be shown: ${v.error}`); p.setAttribute("role", "status"); host.appendChild(p);
      return null;
    }
    const spec = v.spec;
    const fig = H("figure", `chart chart-${spec.type}`);
    fig.dataset.chartType = spec.type;
    const cap = H("figcaption", opts.hideTitle ? "chart-title sr-only" : "chart-title", spec.title);   // the display header may already say it
    const legend = H("ul", "chart-legend");
    const plot = H("div", "chart-plot");
    const overlay = H("div", "chart-overlay");
    overlay.setAttribute("role", "group"); overlay.setAttribute("aria-label", `${spec.title}: data points. Use the arrow keys to move between them.`);
    const tip = H("div", "chart-tip"); tip.setAttribute("role", "status"); tip.hidden = true;
    const status = H("p", "chart-status"); status.setAttribute("role", "status");
    const actions = H("div", "chart-actions");
    const btn = (label, cls) => { const b = H("button", `icon-btn ${cls}`, label); b.type = "button"; actions.appendChild(b); return b; };
    const pngBtn = btn("Download PNG", "chart-png"), csvBtn = btn("Copy as CSV", "chart-csv");
    const details = H("details", "chart-data");
    details.appendChild(H("summary", "", "Data table"));
    const table = H("table"), thead = H("thead"), tr0 = H("tr");
    tr0.appendChild(H("th", "", spec.x_label || (spec.type === "pie" || spec.type === "donut" ? "Slice" : "Label")));
    spec.series.forEach((s) => tr0.appendChild(H("th", "", s.name)));
    if (spec.type === "pie" || spec.type === "donut") tr0.appendChild(H("th", "", "Share"));
    thead.appendChild(tr0); table.appendChild(thead);
    const tbody = H("tbody");
    const pieTotal = sum(spec.series[0].points.map((p) => p.value));
    for (const cat of spec.categories) {
      const tr = H("tr"); tr.appendChild(H("th", "", cat)); tr.firstChild.setAttribute("scope", "row");
      spec.series.forEach((s) => { const p = s.points.find((q) => q.label === cat); tr.appendChild(H("td", "num", p ? formatValue(p.value, spec.unit) : "")); });
      if (spec.type === "pie" || spec.type === "donut") { const p = spec.series[0].points.find((q) => q.label === cat); tr.appendChild(H("td", "num", `${pct((p.value / pieTotal) * 100)}`)); }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    const scroller = H("div", "chart-table-scroll"); scroller.appendChild(table); details.appendChild(scroller);
    plot.append(overlay, tip);
    const body = H("div", "chart-body");
    if (spec.type === "pie" || spec.type === "donut") body.append(plot, legend); else body.append(legend, plot);   // a pie's key sits beside it
    fig.append(cap, body, actions, status, details);
    host.appendChild(fig);

    let svgEl = null, hits = [], current = -1, active = [], lastW = 0, destroyed = false, raf = 0;

    function paintLegend(c) {
      legend.textContent = "";
      const pie = spec.type === "pie" || spec.type === "donut";
      legend.hidden = !(pie || spec.series.length > 1);
      legend.classList.toggle("rows", pie);
      const items = pie ? spec.series[0].points.map((p, i) => ({ name: p.label, col: slotColour(c, i, p.label), extra: `${formatValue(p.value, spec.unit)} · ${pct((p.value / pieTotal) * 100)}` }))
                        : spec.series.map((s, i) => ({ name: s.name, col: slotColour(c, i, s.name), extra: "" }));
      for (const it of items) {
        const li = H("li"); const sw = H("span", pie || spec.type === "bar" || spec.type === "stacked_bar" ? "sw box" : "sw line"); sw.style.background = it.col; sw.setAttribute("aria-hidden", "true");
        li.append(sw, H("span", "nm", it.name)); if (it.extra) li.appendChild(H("span", "ex", it.extra)); legend.appendChild(li);
      }
    }
    function tipHtml(idx, c) {
      tip.textContent = "";
      const pie = spec.type === "pie" || spec.type === "donut";
      tip.appendChild(H("div", "tip-title", spec.categories[idx]));
      if (pie) {
        const p = spec.series[0].points.find((q) => q.label === spec.categories[idx]);
        const row = H("div", "tip-row"); const k = H("span", "tip-key box"); k.style.background = slotColour(c, idx, p.label);
        row.append(k, H("strong", "", formatValue(p.value, spec.unit)), H("span", "tip-name", `${pct((p.value / pieTotal) * 100)} of the total`)); tip.appendChild(row); return;
      }
      spec.series.forEach((s, si) => {
        const p = s.points.find((q) => q.label === spec.categories[idx]); if (!p) return;
        const row = H("div", "tip-row"); const k = H("span", "tip-key"); k.style.background = slotColour(c, si, s.name);
        row.append(k, H("strong", "", formatValue(p.value, spec.unit)), H("span", "tip-name", s.name)); tip.appendChild(row);
      });
    }
    function show(idx) {
      const h = hits[idx]; if (!h) return;
      const c = colours(fig); current = idx;
      tipHtml(idx, c); tip.hidden = false;
      const box = plot.getBoundingClientRect(), tw = tip.offsetWidth, th = tip.offsetHeight;
      let left = Math.max(4, Math.min(h.ax - tw / 2, box.width - tw - 4)), topPos = h.ay - th - 12;
      if (topPos < 4) topPos = Math.min(h.ay + 14, box.height - th - 4);
      tip.style.left = `${left}px`; tip.style.top = `${Math.max(0, topPos)}px`;
      active.forEach((m) => m.classList.remove("is-active")); active = [];
      (h.marks || (h.mark ? [h.mark] : [])).forEach((m) => { m.classList.add("is-active"); active.push(m); });
      if (h.cross) { h.cross.setAttribute("x1", h.crossX); h.cross.setAttribute("x2", h.crossX); h.cross.setAttribute("visibility", "visible"); }
    }
    function hide() {
      current = -1; tip.hidden = true;
      active.forEach((m) => m.classList.remove("is-active")); active = [];
      const cross = svgEl && svgEl.querySelector(".chart-cross"); if (cross) cross.setAttribute("visibility", "hidden");
    }
    function render() {
      if (destroyed) return;
      const w = Math.round(Math.max(260, Math.min(plot.clientWidth || host.clientWidth || 640, 1100)));
      lastW = w;
      const c = colours(fig);
      const built = build(spec, c, { width: w });
      if (svgEl) svgEl.remove();
      svgEl = built.svg;
      svgEl.setAttribute("role", "img"); svgEl.setAttribute("aria-label", summary(spec));
      svgEl.setAttribute("class", "chart-svg"); svgEl.style.display = "block";
      plot.insertBefore(svgEl, overlay);
      overlay.textContent = ""; overlay.style.height = `${built.height}px`; plot.style.minHeight = `${built.height}px`;
      hits = built.hits; hide();
      hits.forEach((h, i) => {
        const b = H("button", "chart-hit"); b.type = "button";
        b.style.cssText = `left:${h.x}px;top:${h.y}px;width:${h.w}px;height:${h.h}px`;
        b.setAttribute("aria-label", h.label); b.tabIndex = i === 0 ? 0 : -1; b.dataset.idx = String(i);
        b.addEventListener("pointerenter", () => show(i)); b.addEventListener("pointerleave", () => { if (document.activeElement !== b) hide(); });
        b.addEventListener("focus", () => show(i)); b.addEventListener("blur", hide);
        b.addEventListener("pointerdown", () => show(i));
        b.addEventListener("keydown", (ev) => {
          const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
          let next = null;
          if (ev.key in keys) next = (i + keys[ev.key] + hits.length) % hits.length; else if (ev.key === "Home") next = 0; else if (ev.key === "End") next = hits.length - 1;
          else if (ev.key === "Escape" && !tip.hidden) { ev.preventDefault(); ev.stopPropagation(); hide(); return; }   // the first Escape closes the tooltip, the next the panel
          if (next === null) return;
          ev.preventDefault(); ev.stopPropagation();
          const target = overlay.children[next]; overlay.children[i].tabIndex = -1; target.tabIndex = 0; target.focus();
        });
        overlay.appendChild(b);
      });
      paintLegend(c);
      if (spec.type === "line") {          // nearest-point tooltip anywhere over a line chart, for mouse and touch
        overlay.onpointermove = overlay.onpointerdown = (ev) => {
          if (ev.target !== overlay && ev.target.classList && !ev.target.classList.contains("chart-hit")) return;
          const r = overlay.getBoundingClientRect(), x = ev.clientX - r.left;
          let best = 0, bd = Infinity; hits.forEach((h, i) => { const d = Math.abs(h.crossX - x); if (d < bd) { bd = d; best = i; } });
          if (best !== current) show(best);
        };
        overlay.onpointerleave = () => { if (!overlay.contains(document.activeElement)) hide(); };
      }
    }
    pngBtn.addEventListener("click", async () => {
      pngBtn.disabled = true; status.className = "chart-status"; status.textContent = "Preparing the PNG…";
      try {
        const out = await toPng(spec, fig);
        download(out.blob, `${slug(spec.title)}.png`);
        status.textContent = `Saved ${out.width} × ${out.height} px PNG.`;
      } catch (e) {
        console.warn("[chart] PNG export failed", e);
        status.className = "chart-status bad";
        status.textContent = `Couldn't make the PNG in this browser (${(e && e.message) || "blocked"}). Use Copy as CSV, or open the data table.`;
      } finally { pngBtn.disabled = false; }
    });
    csvBtn.addEventListener("click", async () => {
      const ok = await copyText(toCsv(spec));
      status.className = ok ? "chart-status" : "chart-status bad";
      status.textContent = ok ? `Copied ${spec.categories.length} row${spec.categories.length === 1 ? "" : "s"} as CSV.` : "Couldn't copy - open the data table and select the numbers instead.";
    });
    const redraw = () => { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; if (!destroyed && Math.abs((plot.clientWidth || 0) - lastW) > 1) render(); }); };
    let ro = null;
    if (typeof ResizeObserver !== "undefined") { ro = new ResizeObserver(redraw); ro.observe(plot); } else window.addEventListener("resize", redraw);
    const onTheme = () => render();
    window.addEventListener("jarvis-theme", onTheme);
    render();
    return {
      spec,
      destroy() { destroyed = true; if (ro) ro.disconnect(); else window.removeEventListener("resize", redraw); window.removeEventListener("jarvis-theme", onTheme); if (raf) cancelAnimationFrame(raf); },
    };
  }

  window.JarvisCharts = { mount, validate, niceTicks, toCsv, toPng, formatValue, summary, LIMITS, EXPORT_W, EXPORT_H };
})();
