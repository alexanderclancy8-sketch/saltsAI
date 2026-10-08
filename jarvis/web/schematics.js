/* JARVIS console - system schematics under a reply.
 *
 * The server lays the drawing out (jarvis/services/schematic_layout.py) and sends PRIMITIVES - line, rect, circle, poly, text - with
 * colour ROLES (ink, muted, accent, assumed, line; fills paper, soft, ink, accent). This file only draws them, as inline SVG built
 * with DOM APIs: every label goes in through textContent, nothing is parsed as HTML, and every primitive is checked against a closed
 * list (shape, finite numbers, known roles) before it is drawn. Colours come from CSS classes on the theme's tokens, so a drawing
 * follows light / dark with no redraw.
 *
 * Where it goes: IN the reply message, under its text (never an overlay, so it can't cover the chat). The reply event carries only
 * references {id, revision, number, rev, title, kind}; the drawing is fetched from /api/schematics/{id}?rev=&mode= - "narrow" when
 * the message is phone-width, "wide" otherwise (and again if the width crosses over). The drawing scrolls inside its own box, never
 * the page. Downloads (SVG, PNG, PDF A4 / A3) are plain links to /api/schematics/{id}/export/... - no approval, nothing is sent.
 *
 * Exposes window.JarvisSchematics = { attach, render, validateScene }.
 */
(() => {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  const ID = /^[a-f0-9]{12}$/;
  const SHAPES = new Set(["line", "rect", "circle", "poly", "text"]);
  const STROKES = new Set(["ink", "muted", "accent", "assumed", "line", "paper"]);
  const FILLS = new Set(["paper", "soft", "ink", "accent", "assumed"]);
  const MAX_ITEMS = 25000, MAX_COORD = 200000, NARROW_BELOW = 560;
  const FONT = "Helvetica, Arial, 'Liberation Sans', sans-serif";
  const KIND_TEXT = { fire_loop: "Fire alarm schematic", cause_effect: "Cause and effect matrix", network: "Security system schematic" };
  const DISCLAIMER = "Draft schematic prepared with Jarvis – to be checked by a competent person.";

  const ok = (v) => typeof v === "number" && Number.isFinite(v) && Math.abs(v) <= MAX_COORD;
  const H = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

  /** The primitives that pass the checks (anything else is dropped), or null when the scene itself is not usable. */
  function validateScene(scene) {
    if (!scene || typeof scene !== "object" || !ok(scene.w) || !ok(scene.h) || scene.w <= 0 || scene.h <= 0 || !Array.isArray(scene.items)) return null;
    const items = [];
    for (const p of scene.items.slice(0, MAX_ITEMS)) {
      if (!p || typeof p !== "object" || !SHAPES.has(p.t)) continue;
      if (p.c !== undefined && p.c !== null && !STROKES.has(p.c)) continue;
      if (p.f !== undefined && p.f !== null && !FILLS.has(p.f)) continue;
      if (p.sw !== undefined && !(ok(p.sw) && p.sw >= 0 && p.sw <= 20)) continue;
      if (p.t === "line" && ![p.x1, p.y1, p.x2, p.y2].every(ok)) continue;
      if (p.t === "rect" && !([p.x, p.y, p.w, p.h].every(ok) && p.w >= 0 && p.h >= 0)) continue;
      if (p.t === "circle" && !([p.cx, p.cy, p.r].every(ok) && p.r >= 0)) continue;
      if (p.t === "poly" && !(Array.isArray(p.pts) && p.pts.length >= 2 && p.pts.length <= 400 &&
                              p.pts.every((q) => Array.isArray(q) && q.length === 2 && ok(q[0]) && ok(q[1])))) continue;
      if (p.t === "text" && !([p.x, p.y, p.fs].every(ok) && p.fs > 0 && p.fs < 80 && typeof p.s === "string" && p.s.length <= 300)) continue;
      items.push(p);
    }
    return { w: scene.w, h: scene.h, items };
  }

  function E(tag, attrs, parent) {
    const el = document.createElementNS(SVGNS, tag);
    for (const k in attrs) el.setAttribute(k, String(attrs[k]));
    if (parent) parent.appendChild(el);
    return el;
  }
  const n = (v) => String(Math.round(v * 100) / 100);

  /** An <svg> for a validated scene. */
  function buildSvg(scene, label) {
    const svg = E("svg", { xmlns: SVGNS, viewBox: `0 0 ${n(scene.w)} ${n(scene.h)}`, width: n(scene.w), height: n(scene.h), "font-family": FONT,
                           class: "sch-svg", role: "img", "aria-label": label });
    for (const p of scene.items) {
      const cls = [];
      const stroke = p.c || "ink";
      let el;
      if (p.t === "text") {
        const anchor = p.a === "middle" ? "middle" : p.a === "end" ? "end" : "start";
        el = E("text", { x: n(p.x), y: n(p.y), "font-size": n(p.fs), "font-weight": p.w >= 600 ? 600 : 400, "text-anchor": anchor }, svg);
        if (p.rot === -90 || p.rot === 90) el.setAttribute("transform", `rotate(${p.rot} ${n(p.x)} ${n(p.y)})`);
        el.textContent = p.s;
        el.setAttribute("class", `t-${STROKES.has(stroke) ? stroke : "ink"}`);
        continue;
      }
      if (p.t === "line") el = E("line", { x1: n(p.x1), y1: n(p.y1), x2: n(p.x2), y2: n(p.y2) }, svg);
      else if (p.t === "rect") el = E("rect", { x: n(p.x), y: n(p.y), width: n(p.w), height: n(p.h), rx: n(ok(p.rx) ? p.rx : 0) }, svg);
      else if (p.t === "circle") el = E("circle", { cx: n(p.cx), cy: n(p.cy), r: n(p.r) }, svg);
      else el = E(p.z ? "polygon" : "polyline", { points: p.pts.map((q) => `${n(q[0])},${n(q[1])}`).join(" ") }, svg);
      cls.push(p.sw === 0 ? "s-none" : `s-${stroke}`);
      cls.push(p.f && (p.t !== "poly" || p.z) ? `f-${p.f}` : "f-none");
      el.setAttribute("class", cls.join(" "));
      el.setAttribute("stroke-width", n(p.sw === undefined ? 1 : p.sw));
      if (p.d) el.setAttribute("stroke-dasharray", "4 3");
    }
    return svg;
  }

  const cache = new Map();   // `${id}:${rev}:${mode}` -> promise of the view
  function fetchView(id, rev, mode) {
    const key = `${id}:${rev}:${mode}`;
    if (!cache.has(key)) {
      cache.set(key, fetch(`/api/schematics/${id}?rev=${rev}&mode=${mode}`, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then((r) => { if (!r.ok) throw new Error(r.status === 404 ? "This drawing isn't saved any more." : `The server said ${r.status}.`); return r.json(); })
        .catch((e) => { cache.delete(key); throw e; }));
    }
    return cache.get(key);
  }

  /** Draw one saved drawing into `fig` (a <figure class="sch">) from its reference. */
  function render(fig, ref) {
    fig.textContent = "";
    const head = H("figcaption", "sch-head");
    const title = H("span", "sch-title", String(ref.title || "Schematic").slice(0, 120));
    const meta = H("span", "sch-meta", `${String(ref.number || "").slice(0, 20)} · Rev ${String(ref.rev || "").slice(0, 6)} · ${KIND_TEXT[ref.kind] || "Schematic"}`);
    head.append(title, meta);
    const view = H("div", "sch-view");
    view.tabIndex = 0;
    view.setAttribute("role", "region");
    view.setAttribute("aria-label", `${ref.title || "Schematic"} - the drawing (scrolls if it is larger than the box)`);
    const status = H("p", "sch-status", "Drawing…");
    status.setAttribute("role", "status");
    const note = H("p", "sch-note", DISCLAIMER);
    const actions = H("div", "sch-actions");
    const zoom = H("button", "icon-btn sch-zoom", "Actual size"); zoom.type = "button"; zoom.setAttribute("aria-pressed", "false");
    actions.appendChild(zoom);
    const base = `/api/schematics/${ref.id}/export`;
    for (const [label, href] of [["SVG", `${base}/svg?rev=${ref.revision}`], ["PNG", `${base}/png?rev=${ref.revision}`],
                                 ["PDF A4", `${base}/pdf?rev=${ref.revision}&paper=a4`], ["PDF A3", `${base}/pdf?rev=${ref.revision}&paper=a3`]]) {
      const a = H("a", "icon-btn sch-dl", label); a.href = href; a.setAttribute("download", ""); a.setAttribute("aria-label", `Download ${label}`);
      actions.appendChild(a);
    }
    fig.append(head, view, note, actions, status);
    let mode = "", drawing = 0, actual = false;
    const pick = () => ((fig.clientWidth || view.clientWidth || 800) < NARROW_BELOW ? "narrow" : "wide");
    async function draw() {
      const want = pick();
      if (want === mode) return;
      mode = want;
      const my = ++drawing;
      try {
        const data = await fetchView(ref.id, ref.revision, want);
        if (my !== drawing) return;
        const scene = validateScene(data && data.scene);
        if (!scene) throw new Error("The drawing wasn't in the expected shape.");
        const d = data.drawing || {};
        const label = `${KIND_TEXT[d.kind] || "Schematic"}: ${String(d.title || ref.title || "").slice(0, 120)}. ${String(d.summary || "").slice(0, 200)}. ` +
                      `${String(d.number || "")} revision ${String(d.rev || "")}. ${DISCLAIMER}`;
        const svg = buildSvg(scene, label);
        view.textContent = "";
        view.appendChild(svg);
        fig.dataset.mode = want;
        fig.classList.toggle("actual", actual);
        status.textContent = "";
      } catch (e) {
        if (my !== drawing) return;
        mode = "";
        status.textContent = `Couldn't show the drawing: ${(e && e.message) || "it didn't load"}. The downloads may still work.`;
        status.classList.add("bad");
      }
    }
    zoom.addEventListener("click", () => {
      actual = !actual;
      fig.classList.toggle("actual", actual);
      zoom.textContent = actual ? "Fit to width" : "Actual size";
      zoom.setAttribute("aria-pressed", actual ? "true" : "false");
    });
    for (const a of actions.querySelectorAll("a.sch-dl")) a.addEventListener("click", () => { status.classList.remove("bad"); status.textContent = `Downloading ${a.textContent}…`; setTimeout(() => { if (status.textContent.startsWith("Downloading")) status.textContent = ""; }, 4000); });
    if (typeof ResizeObserver !== "undefined") {
      let raf = 0;
      new ResizeObserver(() => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (fig.isConnected) draw(); }); }).observe(fig);
    }
    draw();
  }

  /** Show the drawings a reply refers to, under its text. */
  function attach(msg, refs) {
    if (!msg || !Array.isArray(refs)) return;
    for (const ref of refs.slice(0, 4)) {
      if (!ref || typeof ref !== "object" || !ID.test(String(ref.id || "")) || !Number.isInteger(ref.revision) || ref.revision < 1 || ref.revision > 9999) continue;
      if (msg.querySelector(`figure.sch[data-ref="${ref.id}-${ref.revision}"]`)) continue;
      const fig = H("figure", "sch");
      fig.dataset.ref = `${ref.id}-${ref.revision}`;
      const before = msg.querySelector(":scope > .web-src, :scope > .src, :scope > .cov, :scope > .extras");
      if (before) msg.insertBefore(fig, before); else msg.appendChild(fig);
      msg.classList.add("has-sch");
      render(fig, ref);
    }
  }

  window.JarvisSchematics = { attach, render, validateScene };
})();
