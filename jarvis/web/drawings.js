/* JARVIS console - the Drawings pop-up: device layouts and zone charts on a floor plan (services/plan_drawings.py).
 *
 * Jarvis proposes, a person adjusts, then exports. The list (GET /api/drawings) opens a drawing in the editor: the plan picture
 * (GET /api/drawings/{id}/plan) under an SVG overlay drawn with the shared symbol set (services/schematic_symbols.py, drawn by drawing_symbols.js). In the editor you can
 *   - Move: drag a device, drag a zone's corner dots or the whole zone, or drag empty space to pan a zoomed plan;
 *   - Add device: pick a symbol in the palette, then tap / click the plan (keep the finger down to slide it into place);
 *   - Draw zone: tap the corners, then Finish (or tap the first corner again); set its number, name and floor;
 *   - You are here (zone charts): tap where the panel / the person reading the chart stands;
 *   - relabel, change type, set a camera's view direction, delete, undo (button or Ctrl+Z), turn the plan 90 degrees either way
 *     (a zone chart must read the right way round for someone at the panel), zoom; Delete / arrow keys work on the selection;
 *   - fill in the title block, Save (POST /api/drawings/{id}, with the version it was opened at), export a PDF or PNG on A3 / A4
 *     (unsaved changes are saved first), and - owner / managers - ask Jarvis to propose (POST .../propose: a draft that is loaded
 *     into the editor, NOT saved, so the person checks it first), upload a new plan, or delete a drawing.
 * Mouse and touch are the same Pointer Events. Office sign-ins get a view-only editor (the server refuses their saves anyway).
 *
 * Everything from a drawing (labels, names, titles) is set through textContent / value / setAttribute - never parsed as markup.
 * Loaded after drawing_symbols.js and before hud.js; exposes window.JarvisDrawings = { init, load, open, dirty, close }.
 */
(() => {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clamp = (v) => Math.min(1, Math.max(0, v));
  const round = (v) => Math.round(v * 100000) / 100000;
  const S = () => window.DrawingSymbols;
  const MAX_UNDO = 60, MAX_UPLOAD = 25 * 1024 * 1024;
  const KIND_LABEL = { devices: "Device layout", zones: "Zone chart" };
  const META_FIELDS = [["title", "Title", 120], ["site_name", "Site", 120], ["address", "Address", 240], ["panel_location", "Panel location", 120],
    ["job_ref", "Job ref", 40], ["revision", "Revision", 12], ["drawing_date", "Date", 10]];
  let host = null;            // { api(path, opts), toast(title, body, level), role, teamRole } - from hud.js
  let E = null;               // the open drawing's editor state (null on the list)

  const el = (name, attrs, text) => {
    const n = document.createElementNS(SVGNS, name);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const when = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }); };
  function showError(msg, where = "#drw-error") { const e = $(where); if (!e) return; e.textContent = msg || ""; e.hidden = !msg; }
  async function readJson(r) { try { return await r.json(); } catch { return {}; } }
  const detail = (d, fallback) => (typeof d?.detail === "string" ? d.detail : typeof d?.error === "string" ? d.error : fallback);
  function wide(on) { $("#drawer")?.classList.toggle("drw-wide", !!on); }

  // ------------------------------------------------------------------------------------------------------------------ the list
  async function load() {
    if (E && dirty() && !confirm("Discard unsaved drawing changes?")) return;
    await showList();
  }

  async function showList() {
    if (!host || !$("#pop-drawings")) return;
    E = null;
    wide(false);
    $("#drw-editor").hidden = true;
    $("#drw-editor").textContent = "";
    $("#drw-list-view").hidden = false;
    showError("");
    let data;
    try {
      const r = await host.api("/api/drawings");
      data = await readJson(r);
      if (!r.ok) throw new Error(detail(data, "Couldn't load the drawings."));
    } catch (e) { if (e?.message !== "signed out") showError(e?.message || "Couldn't load the drawings. Try again in a moment."); return; }
    S().setData(data.symbols);       // the ONE shared symbol set (services/schematic_symbols.py), sent by the server
    const list = data.drawings || [];
    $("#drw-count").textContent = list.length ? String(list.length) : "";
    $("#drw-list").innerHTML = list.length ? list.map((d) => `<li class="mem-item drw-item" data-id="${Number(d.id)}">
        <p class="mem-text"><b>${esc(d.title)}</b> <span class="drw-ref">${esc(d.ref)}</span></p>
        <small class="mem-meta">${esc(d.kind_label)}${d.site_name ? ` · ${esc(d.site_name)}` : ""}${d.job_ref ? ` · Job ${esc(d.job_ref)}` : ""}${d.revision ? ` · Rev ${esc(d.revision)}` : ""}</small>
        <small class="mem-meta">${d.kind === "zones" ? `${Number(d.zones)} zone${d.zones === 1 ? "" : "s"}` : `${Number(d.devices)} device${d.devices === 1 ? "" : "s"}`} · saved ${esc(when(d.updated_at))}${d.updated_by ? ` by ${esc(d.updated_by)}` : ""}</small>
        <div class="row"><button type="button" class="btn go" data-drw-open="${Number(d.id)}">Open</button></div></li>`).join("")
      : `<li class="empty">${esc(data.can_manage ? "No drawings yet. Upload a floor plan above to start one." : (data.team_note || "No drawings yet."))}</li>`;
  }

  async function createFromUpload(form) {
    const file = $("#drw-file").files[0];
    showError("", "#drw-new-error");
    if (!file) { showError("Choose a floor plan file first.", "#drw-new-error"); return; }
    if (!/\.(pdf|png|jpe?g|webp)$/i.test(file.name)) { showError("A plan has to be a PDF, PNG, JPEG or WebP file.", "#drw-new-error"); return; }
    if (file.size > MAX_UPLOAD) { showError("That file is over the 25 MB limit for a plan.", "#drw-new-error"); return; }
    const fd = new FormData();
    fd.append("plan", file, file.name);
    fd.append("kind", $("#drw-kind").value);
    fd.append("page", String(Math.max(1, Number($("#drw-page").value) || 1)));
    fd.append("title", $("#drw-title").value.trim());
    fd.append("site_name", $("#drw-site").value.trim());
    fd.append("job_ref", $("#drw-job").value.trim());
    const btn = $("#drw-create");
    btn.disabled = true; btn.textContent = "Uploading…";
    try {
      const r = await host.api("/api/drawings", { method: "POST", body: fd });
      const d = await readJson(r);
      if (!r.ok) { showError(detail(d, "The plan couldn't be uploaded."), "#drw-new-error"); return; }
      form.reset();
      await open(d.id, d);
    } catch (e) { if (e?.message !== "signed out") showError("The plan couldn't be uploaded - try again.", "#drw-new-error"); }
    finally { btn.disabled = false; btn.textContent = "Upload and open"; }
  }

  // ------------------------------------------------------------------------------------------------------------- the editor
  const EDITOR = `
    <div class="drw-head">
      <button type="button" class="btn small" data-drw="back">‹ All drawings</button>
      <div class="drw-title"><b data-f="heading"></b><small data-f="sub"></small></div>
      <span class="drw-status" data-f="status" role="status" aria-live="polite"></span>
    </div>
    <div class="drw-tools" role="toolbar" aria-label="Drawing tools">
      <div class="drw-group drw-edit-only" data-f="modes">
        <button type="button" class="btn small" data-mode="move" aria-pressed="true">Move</button>
        <button type="button" class="btn small drw-dev-only" data-mode="add" aria-pressed="false">Add device</button>
        <button type="button" class="btn small drw-zone-only" data-mode="zone" aria-pressed="false">Draw zone</button>
        <button type="button" class="btn small drw-zone-only" data-mode="here" aria-pressed="false">You are here</button>
      </div>
      <div class="drw-group">
        <button type="button" class="btn small drw-edit-only" data-drw="undo" disabled>Undo</button>
        <button type="button" class="btn small drw-edit-only" data-drw="rotl" title="Turn the plan 90° anticlockwise">⟲ Turn</button>
        <button type="button" class="btn small drw-edit-only" data-drw="rotr" title="Turn the plan 90° clockwise">Turn ⟳</button>
        <button type="button" class="btn small" data-drw="zout" aria-label="Zoom out">−</button>
        <button type="button" class="btn small" data-drw="zin" aria-label="Zoom in">+</button>
      </div>
      <div class="drw-group">
        <button type="button" class="btn small primary drw-edit-only" data-drw="save">Save</button>
        <label class="drw-paper">Paper <select data-f="paper"><option value="A3">A3</option><option value="A4">A4</option></select></label>
        <button type="button" class="btn small" data-drw="pdf">Export PDF</button>
        <button type="button" class="btn small" data-drw="png">Export PNG</button>
      </div>
    </div>
    <div class="drw-palette drw-dev-only" data-f="palette" role="listbox" aria-label="Device to add" hidden></div>
    <div class="drw-zonebar" data-f="zonebar" hidden>
      <span>Tap each corner of the zone, then Finish (or tap the first corner again).</span>
      <button type="button" class="btn small go" data-drw="finish">Finish zone</button>
      <button type="button" class="btn small" data-drw="cancelzone">Cancel</button>
    </div>
    <div class="drw-body">
      <div class="drw-main">
        <div class="drw-stage" data-f="stage" tabindex="0" aria-label="Floor plan. Select a device, then use the arrow keys to move it or Delete to remove it."></div>
        <p class="drw-disclaimer" data-f="disclaimer"></p>
      </div>
      <div class="drw-side">
        <div class="drw-sel" data-f="sel"></div>
        <div class="drw-propose" data-f="propose" hidden>
          <h4>Ask Jarvis to propose</h4>
          <label>What should Jarvis draw?<textarea data-f="brief" rows="3" maxlength="1500"></textarea></label>
          <div class="row"><button type="button" class="btn small" data-drw="propose">Propose a draft</button></div>
          <p class="drw-hint">Jarvis looks at the plan and suggests positions. They are approximate - check and move every one before you export.</p>
          <ul class="drw-notes" data-f="notes" hidden></ul>
        </div>
        <div class="drw-legend-box">
          <h4 data-f="legend-title">Legend</h4>
          <ul class="drw-legend" data-f="legend"></ul>
        </div>
        <details class="drw-meta" open>
          <summary>Title block</summary>
          <div data-f="meta"></div>
        </details>
        <div class="row drw-manage" data-f="manage" hidden>
          <button type="button" class="btn small stop" data-drw="delete">Delete drawing</button>
        </div>
      </div>
    </div>`;

  const F = (name) => $(`[data-f="${name}"]`, $("#drw-editor"));

  async function open(id, preloaded) {
    if (!host) return;
    if (E && dirty() && E.d.id !== Number(id) && !confirm("Discard unsaved drawing changes?")) return;
    showError("");
    let d = preloaded;
    if (!d) {
      try {
        const r = await host.api(`/api/drawings/${Number(id)}`);
        d = await readJson(r);
        if (!r.ok) { showError(detail(d, "That drawing couldn't be opened.")); return; }
      } catch (e) { if (e?.message !== "signed out") showError("That drawing couldn't be opened - try again."); return; }
    }
    S().setData(d.symbols);
    E = { d, meta: {}, content: JSON.parse(JSON.stringify(d.content)), saved: "", undo: [], mode: "move", addType: "smoke", sel: null,
          draft: [], zoom: 1, drag: null, busy: false };
    for (const [k] of META_FIELDS) E.meta[k] = d[k] || "";
    E.saved = snapshot();
    $("#drw-list-view").hidden = true;
    const ed = $("#drw-editor");
    ed.innerHTML = EDITOR;     // a constant skeleton: nothing from the drawing is in it
    ed.hidden = false;
    ed.dataset.kind = d.kind;
    ed.classList.toggle("view-only", !d.can_edit);
    wide(true);
    buildPalette();
    buildMeta();
    F("disclaimer").textContent = d.disclaimer + " Positions are approximate and are not a design calculation.";
    F("paper").value = E.content.paper || "A3";
    F("propose").hidden = !d.can_manage;
    F("manage").hidden = !d.can_manage;
    F("brief").placeholder = d.kind === "zones" ? "e.g. Divide into zones, one per floor, stairs separate" : "e.g. L2 fire alarm: smoke detectors, call points at exits, sounders";
    buildStage();
    render();
    $("#drawer-body") && ($("#drawer-body").scrollTop = 0);
  }

  function buildPalette() {
    const pal = F("palette");
    pal.textContent = "";
    for (const t of S().TYPES) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "drw-pal"; b.dataset.type = t;
      b.setAttribute("role", "option"); b.setAttribute("aria-selected", String(t === E.addType));
      b.appendChild(S().icon(t, 26));
      const s = document.createElement("span"); s.textContent = S().label(t); b.appendChild(s);
      pal.appendChild(b);
    }
  }

  function buildMeta() {
    const box = F("meta");
    box.textContent = "";
    for (const [k, label, max] of META_FIELDS) {
      const l = document.createElement("label");
      l.className = "drw-field";
      l.append(label);
      const i = document.createElement("input");
      i.type = k === "drawing_date" ? "date" : "text"; i.maxLength = max; i.value = E.meta[k] || ""; i.dataset.meta = k;
      i.disabled = !E.d.can_edit || (k === "job_ref" && !E.d.can_manage);
      l.appendChild(i);
      box.appendChild(l);
    }
  }

  function buildStage() {
    const stage = F("stage");
    stage.textContent = "";
    const svg = el("svg", { class: "drw-svg", role: "img", "aria-label": `${KIND_LABEL[E.d.kind]} on the floor plan` });
    stage.appendChild(svg);
    E.svg = svg;
    svg.addEventListener("pointerdown", onDown);
    svg.addEventListener("pointermove", onMove);
    svg.addEventListener("pointerup", onUp);
    svg.addEventListener("pointercancel", onUp);
    stage.addEventListener("keydown", onStageKey);
  }

  // --- geometry: drawings are stored in 0-1 coordinates of the plan AS UPLOADED; the view may be turned by 90-degree steps
  function dims() {
    const W = Number(E.d.plan.width) || 1000, H = Number(E.d.plan.height) || 700;
    return E.content.rotation % 180 ? [H, W] : [W, H];
  }
  function toView(x, y) {
    const r = E.content.rotation;
    const [rx, ry] = r === 90 ? [1 - y, x] : r === 180 ? [1 - x, 1 - y] : r === 270 ? [y, 1 - x] : [x, y];
    const [VW, VH] = dims();
    return [rx * VW, ry * VH];
  }
  function fromView(sx, sy) {
    const [VW, VH] = dims();
    const a = clamp(sx / VW), b = clamp(sy / VH), r = E.content.rotation;
    const p = r === 90 ? [b, 1 - a] : r === 180 ? [1 - a, 1 - b] : r === 270 ? [1 - b, a] : [a, b];
    return [round(p[0]), round(p[1])];
  }
  function svgPoint(evt) {
    const m = E.svg.getScreenCTM();
    if (!m) return [0, 0];
    const p = new DOMPoint(evt.clientX, evt.clientY).matrixTransform(m.inverse());
    return [p.x, p.y];
  }
  function symSize() {
    const [VW, VH] = dims();
    const w = E.svg.getBoundingClientRect().width || 600;
    return Math.max(0.026 * Math.max(VW, VH), (24 * VW) / w);   // never under ~24 CSS px on screen, however far it is zoomed out
  }
  const zoneColour = (n) => S().ZONE_COLOURS[(Math.max(1, n) - 1) % S().ZONE_COLOURS.length];
  function centroid(pts) {
    let a = 0, cx = 0, cy = 0;
    for (let i = 0; i < pts.length; i++) {
      const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length], c = x0 * y1 - x1 * y0;
      a += c; cx += (x0 + x1) * c; cy += (y0 + y1) * c;
    }
    if (Math.abs(a) < 1e-12) return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
    return [cx / (3 * a), cy / (3 * a)];
  }

  // ------------------------------------------------------------------------------------------------------------- drawing it
  function render() {
    if (!E) return;
    renderSvg(); renderSide(); renderLegend(); renderStatus(); renderTools();
  }

  function renderSvg() {
    const svg = E.svg, [VW, VH] = dims(), W = Number(E.d.plan.width), H = Number(E.d.plan.height), r = E.content.rotation;
    svg.textContent = "";
    svg.setAttribute("viewBox", `0 0 ${VW} ${VH}`);
    svg.style.width = `${E.zoom * 100}%`;
    svg.appendChild(el("rect", { x: 0, y: 0, width: VW, height: VH, fill: "#ffffff" }));
    const t = r === 90 ? `translate(${H} 0) rotate(90)` : r === 180 ? `translate(${W} ${H}) rotate(180)` : r === 270 ? `translate(0 ${W}) rotate(270)` : "";
    const img = el("image", { x: 0, y: 0, width: W, height: H, preserveAspectRatio: "none", class: "drw-plan" });
    img.setAttribute("href", E.d.plan.url);
    if (t) img.setAttribute("transform", t);
    svg.appendChild(img);
    const s = symSize();
    if (E.d.kind === "zones") {
      const zl = el("g", { class: "drw-zones" });
      E.content.zones.forEach((z, i) => {
        const col = zoneColour(z.number), pts = z.polygon.map(([x, y]) => toView(x, y));
        const g = el("g", { class: "drw-zone" + (E.sel?.kind === "zone" && E.sel.i === i ? " is-sel" : ""), "data-z": i });
        g.appendChild(el("polygon", { points: pts.map((p) => p.join(",")).join(" "), fill: col, "fill-opacity": 0.24, stroke: col,
                                      "stroke-width": s * 0.09, "stroke-linejoin": "round", class: "drw-zone-shape" }));
        const [cx, cy] = toView(...centroid(z.polygon));
        g.appendChild(el("circle", { cx, cy, r: s * 0.62, fill: "#ffffff", stroke: col, "stroke-width": s * 0.09, class: "drw-zone-badge" }));
        g.appendChild(el("text", { x: cx, y: cy, fill: col, "font-size": s * 0.68, "font-weight": 700, "text-anchor": "middle", "dominant-baseline": "central",
                                   class: "drw-zone-num" }, String(z.number)));
        zl.appendChild(g);
      });
      svg.appendChild(zl);
      if (E.sel?.kind === "zone" && E.content.zones[E.sel.i] && E.d.can_edit) {
        const hg = el("g", { class: "drw-handles" });
        E.content.zones[E.sel.i].polygon.forEach(([x, y], v) => {
          const [hx, hy] = toView(x, y);
          hg.appendChild(el("circle", { cx: hx, cy: hy, r: s * 0.32, class: "drw-vtx", "data-v": v, "stroke-width": s * 0.08 }));
        });
        svg.appendChild(hg);
      }
      if (E.draft.length) {
        const pts = E.draft.map(([x, y]) => toView(x, y));
        const dg = el("g", { class: "drw-draft" });
        dg.appendChild(el("polyline", { points: pts.map((p) => p.join(",")).join(" "), fill: "none", "stroke-width": s * 0.1, class: "drw-draft-line" }));
        pts.forEach(([x, y], k) => dg.appendChild(el("circle", { cx: x, cy: y, r: s * (k === 0 ? 0.36 : 0.26), class: "drw-draft-pt" + (k === 0 ? " first" : ""),
                                                                    "stroke-width": s * 0.08 })));
        svg.appendChild(dg);
      }
      const here = E.content.you_are_here;
      if (here) {
        const [hx, hy] = toView(here.x, here.y);
        const g = el("g", { class: "drw-here" });
        g.appendChild(el("circle", { cx: hx, cy: hy, r: s * 0.38, fill: "#d50000", stroke: "#ffffff", "stroke-width": s * 0.1 }));
        const fs = s * 0.5, label = "YOU ARE HERE", tw = fs * 0.68 * label.length, left = hx + s * 0.6 + tw + s * 0.4 > VW;
        const bx = left ? hx - s * 0.6 - tw - s * 0.3 : hx + s * 0.6;
        g.appendChild(el("rect", { x: bx, y: hy - fs * 0.85, width: tw + s * 0.3, height: fs * 1.7, fill: "#ffffff", stroke: "#d50000", "stroke-width": s * 0.06 }));
        g.appendChild(el("text", { x: bx + s * 0.15, y: hy, fill: "#d50000", "font-size": fs, "font-weight": 700, "dominant-baseline": "central" }, label));
        svg.appendChild(g);
      }
    } else {
      const dl = el("g", { class: "drw-devices" });
      E.content.devices.forEach((d, i) => dl.appendChild(deviceNode(d, i, s)));
      svg.appendChild(dl);
    }
  }

  function deviceNode(d, i, s) {
    const [x, y] = toView(d.x, d.y);
    const g = el("g", { class: "drw-dev" + (E.sel?.kind === "device" && E.sel.i === i ? " is-sel" : ""), "data-i": i, transform: `translate(${x} ${y})` });
    g.appendChild(el("circle", { cx: 0, cy: 0, r: s * 0.95, class: "drw-hit" }));
    if (E.sel?.kind === "device" && E.sel.i === i) g.appendChild(el("circle", { cx: 0, cy: 0, r: s * 0.78, class: "drw-selring", "stroke-width": s * 0.1 }));
    const dir = Number.isFinite(d.direction) ? (d.direction + E.content.rotation) % 360 : undefined;
    S().draw(g, d.type, 0, 0, s, { direction: dir });
    if (d.label) g.appendChild(el("text", { x: s * 0.62, y: 0, "font-size": s * 0.42, class: "drw-label", "dominant-baseline": "central",
                                            "stroke-width": s * 0.14 }, d.label));
    return g;
  }

  function renderTools() {
    $$("[data-mode]", $("#drw-editor")).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === E.mode)));
    F("palette").hidden = !(E.mode === "add" && E.d.kind === "devices");
    $$(".drw-pal", F("palette")).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.type === E.addType)));
    F("zonebar").hidden = E.mode !== "zone";
    $('[data-drw="finish"]', $("#drw-editor")).disabled = E.draft.length < 3;
    $('[data-drw="undo"]', $("#drw-editor")).disabled = !E.undo.length;
    E.svg.dataset.mode = E.mode;
  }

  function renderStatus() {
    const d = E.d, c = E.content;
    F("heading").textContent = `${E.meta.title || d.title} (${d.ref})`;
    F("sub").textContent = `${d.kind_label} · ${c.rotation ? `turned ${c.rotation}°` : "as uploaded"}${d.can_edit ? "" : " · view only"}`;
    F("status").textContent = E.busy ? "Saving…" : dirty() ? "Unsaved changes" : `Saved${d.updated_by ? ` by ${d.updated_by}` : ""}`;
    F("status").dataset.state = dirty() ? "dirty" : "saved";
  }

  function renderLegend() {
    const ul = F("legend");
    ul.textContent = "";
    if (E.d.kind === "devices") {
      F("legend-title").textContent = "Legend and counts";
      const counts = {};
      for (const d of E.content.devices) counts[d.type] = (counts[d.type] || 0) + 1;
      const types = S().TYPES.filter((t) => counts[t]);
      for (const t of types) {
        const li = document.createElement("li");
        li.appendChild(S().icon(t, 24));
        const n = document.createElement("span"); n.className = "nm"; n.textContent = S().label(t);
        const c = document.createElement("b"); c.className = "ct"; c.textContent = String(counts[t]);
        li.append(n, c); ul.appendChild(li);
      }
      const total = document.createElement("li"); total.className = "total";
      const n = document.createElement("span"); n.className = "nm"; n.textContent = types.length ? "Total devices" : "No devices yet - choose Add device, then tap the plan.";
      total.appendChild(n);
      if (types.length) { const c = document.createElement("b"); c.className = "ct"; c.textContent = String(E.content.devices.length); total.appendChild(c); }
      ul.appendChild(total);
    } else {
      F("legend-title").textContent = "Zones";
      const zones = E.content.zones.map((z, i) => [z, i]).sort((a, b) => a[0].number - b[0].number);
      for (const [z, i] of zones) {
        const li = document.createElement("li"); li.className = "zone-row";
        const b = document.createElement("button"); b.type = "button"; b.className = "drw-zone-pick"; b.dataset.z = String(i);
        const sw = document.createElement("span"); sw.className = "sw"; sw.style.background = zoneColour(z.number); sw.textContent = String(z.number);
        const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = z.name + (z.floor ? ` (${z.floor})` : "");
        b.append(sw, nm); li.appendChild(b); ul.appendChild(li);
      }
      const dup = E.content.zones.map((z) => z.number).filter((n, k, a) => a.indexOf(n) !== k);
      const note = document.createElement("li"); note.className = "total";
      note.textContent = !E.content.zones.length ? "No zones yet - choose Draw zone, then tap the corners."
        : dup.length ? `Zone number ${[...new Set(dup)].join(", ")} is used twice - each zone needs its own number, matching the panel.`
        : E.content.you_are_here ? "Zone numbers should match the panel. “You are here” is placed." : "Zone numbers should match the panel. Place “You are here” at the panel.";
      ul.appendChild(note);
    }
  }

  function field(label, input) { const l = document.createElement("label"); l.className = "drw-field"; l.append(label, input); return l; }
  function input(kind, value, attrs = {}) {
    const i = document.createElement(kind === "select" ? "select" : "input");
    if (kind !== "select") i.type = kind;
    for (const [k, v] of Object.entries(attrs)) i[k] = v;
    if (value !== undefined && kind !== "select") i.value = value;
    i.disabled = !E.d.can_edit;
    return i;
  }

  function renderSide() {
    const box = F("sel");
    box.textContent = "";
    const h = document.createElement("h4");
    if (E.sel?.kind === "device" && E.content.devices[E.sel.i]) {
      const d = E.content.devices[E.sel.i];
      h.textContent = "Selected device"; box.appendChild(h);
      const sel = input("select", undefined, {}); sel.dataset.dev = "type";
      for (const t of S().TYPES) { const o = document.createElement("option"); o.value = t; o.textContent = S().label(t); sel.appendChild(o); }
      sel.value = d.type;
      box.appendChild(field("Type", sel));
      const lab = input("text", d.label || "", { maxLength: 40 }); lab.dataset.dev = "label";
      box.appendChild(field("Label", lab));
      const note = input("text", d.note || "", { maxLength: 160 }); note.dataset.dev = "note";
      box.appendChild(field("Note", note));
      if (d.type === "camera") {
        const dir = input("number", Number.isFinite(d.direction) ? String(d.direction) : "", { min: 0, max: 359, step: 5 }); dir.dataset.dev = "direction";
        box.appendChild(field("View direction (degrees clockwise from up, as uploaded)", dir));
      }
      if (E.d.can_edit) {
        const row = document.createElement("div"); row.className = "row";
        const del = document.createElement("button"); del.type = "button"; del.className = "btn small stop"; del.dataset.drw = "deldev"; del.textContent = "Delete device";
        row.appendChild(del); box.appendChild(row);
      }
    } else if (E.sel?.kind === "zone" && E.content.zones[E.sel.i]) {
      const z = E.content.zones[E.sel.i];
      h.textContent = "Selected zone"; box.appendChild(h);
      const num = input("number", String(z.number), { min: 1, max: 999, step: 1 }); num.dataset.zone = "number";
      box.appendChild(field("Zone number (as on the panel)", num));
      const nm = input("text", z.name, { maxLength: 60 }); nm.dataset.zone = "name";
      box.appendChild(field("Name", nm));
      const fl = input("text", z.floor || "", { maxLength: 40 }); fl.dataset.zone = "floor";
      box.appendChild(field("Floor", fl));
      if (E.d.can_edit) {
        const p = document.createElement("p"); p.className = "drw-hint"; p.textContent = "Drag the corner dots to reshape it, or drag inside it to move it.";
        const row = document.createElement("div"); row.className = "row";
        const del = document.createElement("button"); del.type = "button"; del.className = "btn small stop"; del.dataset.drw = "delzone"; del.textContent = "Delete zone";
        row.appendChild(del); box.append(p, row);
      }
    } else {
      h.textContent = E.d.can_edit ? "How to edit" : "View only"; box.appendChild(h);
      const p = document.createElement("p"); p.className = "drw-hint";
      p.textContent = !E.d.can_edit ? "You can view and export this drawing. Changes are made by an engineer, a manager or the owner."
        : E.d.kind === "devices" ? "Tap a device to select it and drag it into place. Add device puts a new one where you tap. Undo takes back the last change."
        : "Draw zone, then tap the corners of each zone. Tap a zone to rename or reshape it. Turn the plan so it reads the way someone at the panel is facing, and place “You are here”.";
      box.appendChild(p);
    }
  }

  // ---------------------------------------------------------------------------------------------------------- state changes
  function snapshot() { return JSON.stringify({ meta: E.meta, content: E.content }); }
  function dirty() { return !!E && snapshot() !== E.saved; }
  function pushUndo(snap) {
    E.undo.push(snap);
    if (E.undo.length > MAX_UNDO) E.undo.shift();
  }
  function change(fn) { if (!E.d.can_edit) return; const before = snapshot(); fn(); if (snapshot() !== before) pushUndo(before); render(); }
  function undo() {
    if (!E.undo.length) return;
    const s = JSON.parse(E.undo.pop());
    E.meta = s.meta; E.content = s.content;
    if (E.sel && ((E.sel.kind === "device" && !E.content.devices[E.sel.i]) || (E.sel.kind === "zone" && !E.content.zones[E.sel.i]))) E.sel = null;
    buildMeta(); render();
  }
  function setMode(mode) {
    if (!E.d.can_edit) return;
    E.mode = mode;
    if (mode !== "zone") E.draft = [];
    render();
  }
  function finishZone() {
    if (E.draft.length < 3) return;
    const pts = E.draft.slice();
    change(() => {
      const n = Math.max(0, ...E.content.zones.map((z) => z.number)) + 1;
      E.content.zones.push({ number: n, name: `Zone ${n}`, floor: "", polygon: pts });
      E.sel = { kind: "zone", i: E.content.zones.length - 1 };
      E.draft = []; E.mode = "move";
    });
  }
  function deleteSelected() {
    if (!E.sel) return;
    const sel = E.sel;
    change(() => {
      if (sel.kind === "device") E.content.devices.splice(sel.i, 1);
      if (sel.kind === "zone") E.content.zones.splice(sel.i, 1);
      E.sel = null;
    });
  }

  // --------------------------------------------------------------------------------------------------- pointer: mouse + touch
  function onDown(e) {
    if (!E || e.button > 0) return;
    const [sx, sy] = svgPoint(e);
    const [x, y] = fromView(sx, sy);
    const devNode = e.target.closest?.(".drw-dev"), vtx = e.target.closest?.(".drw-vtx"), zoneNode = e.target.closest?.(".drw-zone");
    const start = snapshot();
    const capture = () => { try { E.svg.setPointerCapture(e.pointerId); } catch { /* not capturable */ } };
    if (E.d.can_edit && E.mode === "add" && E.d.kind === "devices") {
      e.preventDefault();
      const dev = { type: E.addType, x, y, label: "", note: "" };
      if (dev.type === "camera") dev.direction = 0;
      E.content.devices.push(dev);
      E.sel = { kind: "device", i: E.content.devices.length - 1 };
      pushUndo(start);
      E.drag = { kind: "device", i: E.sel.i, moved: false, start: null };   // keep the finger down to slide it into place
      capture(); render(); return;
    }
    if (E.d.can_edit && E.mode === "zone") {
      e.preventDefault();
      if (E.draft.length >= 3) {
        const [fx, fy] = toView(...E.draft[0]);
        if (Math.hypot(fx - sx, fy - sy) < symSize() * 0.6) { finishZone(); return; }
      }
      E.draft.push([x, y]);
      render(); return;
    }
    if (E.d.can_edit && E.mode === "here") {
      e.preventDefault();
      change(() => { E.content.you_are_here = { x, y }; E.mode = "move"; });
      return;
    }
    if (E.d.can_edit && vtx && E.sel?.kind === "zone") {
      e.preventDefault();
      E.drag = { kind: "vertex", z: E.sel.i, v: Number(vtx.dataset.v), moved: false, start };
      capture(); return;
    }
    if (devNode) {
      e.preventDefault();
      E.sel = { kind: "device", i: Number(devNode.dataset.i) };
      if (E.d.can_edit) { E.drag = { kind: "device", i: E.sel.i, moved: false, start }; capture(); }
      render(); return;
    }
    if (zoneNode) {
      e.preventDefault();
      const i = Number(zoneNode.dataset.z);
      const was = E.sel?.kind === "zone" && E.sel.i === i;
      E.sel = { kind: "zone", i };
      if (E.d.can_edit && was) { E.drag = { kind: "zone", z: i, from: [x, y], orig: E.content.zones[i].polygon.map((p) => p.slice()), moved: false, start }; capture(); }
      render(); return;
    }
    // empty plan: deselect, and drag to pan a zoomed-in plan
    if (E.sel) { E.sel = null; render(); }
    const stage = F("stage");
    E.drag = { kind: "pan", x0: e.clientX, y0: e.clientY, sl: stage.scrollLeft, st: stage.scrollTop, moved: false };
    capture();
  }

  function onMove(e) {
    const g = E?.drag;
    if (!g) return;
    e.preventDefault();
    if (g.kind === "pan") {
      const stage = F("stage");
      stage.scrollLeft = g.sl - (e.clientX - g.x0);
      stage.scrollTop = g.st - (e.clientY - g.y0);
      return;
    }
    const [x, y] = fromView(...svgPoint(e));
    g.moved = true;
    if (g.kind === "device") {
      const d = E.content.devices[g.i];
      if (!d) return;
      d.x = x; d.y = y;
      const node = $(`.drw-dev[data-i="${g.i}"]`, E.svg), [vx, vy] = toView(x, y);
      if (node) node.setAttribute("transform", `translate(${vx} ${vy})`);
    } else if (g.kind === "vertex") {
      const z = E.content.zones[g.z];
      if (!z) return;
      z.polygon[g.v] = [x, y];
      renderSvg();
    } else if (g.kind === "zone") {
      const z = E.content.zones[g.z];
      if (!z) return;
      let dx = x - g.from[0], dy = y - g.from[1];
      for (const [px, py] of g.orig) { dx = Math.min(Math.max(dx, -px), 1 - px); dy = Math.min(Math.max(dy, -py), 1 - py); }
      z.polygon = g.orig.map(([px, py]) => [round(px + dx), round(py + dy)]);
      renderSvg();
    }
  }

  function onUp() {
    const g = E?.drag;
    if (!g) return;
    E.drag = null;
    if (g.moved && g.start) pushUndo(g.start);
    if (g.kind !== "pan") render();
  }

  function onStageKey(e) {
    if (!E?.d.can_edit || E.sel?.kind !== "device") return;
    const d = E.content.devices[E.sel.i];
    if (!d) return;
    const step = e.shiftKey ? 0.01 : 0.002;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelected(); return; }
    const m = moves[e.key];
    if (!m) return;
    e.preventDefault();
    // the arrows move it the way it looks on screen, whatever way the plan is turned
    const [vx, vy] = toView(d.x, d.y), [VW, VH] = dims();
    change(() => { const [nx, ny] = fromView(vx + m[0] * VW, vy + m[1] * VH); d.x = nx; d.y = ny; });
  }

  // ------------------------------------------------------------------------------------------------------------ server calls
  async function save() {
    if (!E?.d.can_edit || E.busy) return false;
    E.busy = true; renderStatus();
    try {
      const body = { version: E.d.version, meta: E.meta, content: E.content };
      const r = await host.api(`/api/drawings/${E.d.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const d = await readJson(r);
      if (!r.ok) { showError(detail(d, "The drawing couldn't be saved.")); return false; }
      const keepSel = E.sel, keepZoom = E.zoom, keepMode = E.mode, undoStack = E.undo;
      E.d = d;
      E.content = JSON.parse(JSON.stringify(d.content));
      for (const [k] of META_FIELDS) E.meta[k] = d[k] || "";
      E.saved = snapshot(); E.sel = keepSel; E.zoom = keepZoom; E.mode = keepMode; E.undo = undoStack;
      showError("");
      host.toast("Saved", `${d.ref} is saved.`);
      return true;
    } catch (e) { if (e?.message !== "signed out") showError("The drawing couldn't be saved - try again."); return false; }
    finally { E.busy = false; if (E) { buildMeta(); render(); } }
  }

  async function exportAs(fmt) {
    if (!E) return;
    if (dirty() && E.d.can_edit && !(await save())) return;
    const paper = F("paper").value === "A4" ? "A4" : "A3";
    const btn = $(`[data-drw="${fmt}"]`, $("#drw-editor"));
    btn.disabled = true;
    try {
      const r = await host.api(`/api/drawings/${E.d.id}/export/${fmt}?paper=${paper}`);
      if (!r.ok) { const d = await readJson(r); showError(detail(d, "The export didn't work.")); return; }
      const blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || "", m = /filename="([^"]+)"/.exec(cd);
      const name = m ? m[1] : `drawing-${E.d.id}.${fmt}`;
      const url = URL.createObjectURL(blob), a = document.createElement("a");
      a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    } catch (e) { if (e?.message !== "signed out") showError("The export didn't work - try again."); }
    finally { btn.disabled = false; }
  }

  async function propose() {
    if (!E?.d.can_manage) return;
    const btn = $('[data-drw="propose"]', $("#drw-editor"));
    btn.disabled = true; btn.textContent = "Jarvis is looking at the plan…";
    const notes = F("notes");
    try {
      const r = await host.api(`/api/drawings/${E.d.id}/propose`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brief: F("brief").value.trim(), kind: E.d.kind }) });
      const d = await readJson(r);
      if (!r.ok) { showError(detail(d, "Jarvis couldn't propose a layout.")); return; }
      const key = E.d.kind === "zones" ? "zones" : "devices";
      if (E.content[key].length && !confirm(`Replace the ${key} on this drawing with Jarvis's proposal? (Undo brings them back.)`)) return;
      change(() => {
        E.content[key] = d[key] || [];
        if (d.panel_location && !E.meta.panel_location) E.meta.panel_location = d.panel_location;
        E.sel = null;
      });
      buildMeta();
      notes.textContent = "";
      for (const n of [`Proposed ${key === "zones" ? `${(d.zones || []).length} zones` : `${(d.devices || []).length} devices`} - not saved yet. Check every position, then Save.`, ...(d.notes || [])]) {
        const li = document.createElement("li"); li.textContent = n; notes.appendChild(li);
      }
      notes.hidden = false;
      showError("");
    } catch (e) { if (e?.message !== "signed out") showError("Jarvis couldn't propose a layout - try again."); }
    finally { btn.disabled = false; btn.textContent = "Propose a draft"; }
  }

  async function remove() {
    if (!E?.d.can_manage || !confirm(`Delete ${E.d.ref} (${E.d.title})? This can't be undone.`)) return;
    try {
      const r = await host.api(`/api/drawings/${E.d.id}`, { method: "DELETE" });
      if (!r.ok && r.status !== 404) { const d = await readJson(r); showError(detail(d, "It couldn't be deleted.")); return; }
      E.saved = snapshot();
      host.toast("Deleted", "The drawing has been deleted.");
      await showList();
    } catch (e) { if (e?.message !== "signed out") showError("It couldn't be deleted - try again."); }
  }

  async function backToList() { await load(); }

  // ------------------------------------------------------------------------------------------------------------------ events
  document.addEventListener("click", (e) => {
    if (!e.target.closest?.("#pop-drawings")) return;
    const opener = e.target.closest("[data-drw-open]");
    if (opener) { open(opener.dataset.drwOpen); return; }
    if (!E) return;
    const mode = e.target.closest("[data-mode]");
    if (mode) { setMode(mode.dataset.mode); return; }
    const pal = e.target.closest(".drw-pal");
    if (pal) { E.addType = pal.dataset.type; renderTools(); return; }
    const zp = e.target.closest(".drw-zone-pick");
    if (zp) { E.sel = { kind: "zone", i: Number(zp.dataset.z) }; render(); return; }
    const b = e.target.closest("[data-drw]");
    if (!b) return;
    const act = b.dataset.drw;
    if (act === "back") backToList();
    else if (act === "undo") undo();
    else if (act === "rotl" || act === "rotr") change(() => { E.content.rotation = (E.content.rotation + (act === "rotr" ? 90 : 270)) % 360; });
    else if (act === "zin" || act === "zout") { E.zoom = Math.min(4, Math.max(1, E.zoom * (act === "zin" ? 1.5 : 1 / 1.5))); renderSvg(); }
    else if (act === "save") save();
    else if (act === "pdf" || act === "png") exportAs(act);
    else if (act === "finish") finishZone();
    else if (act === "cancelzone") { E.draft = []; E.mode = "move"; render(); }
    else if (act === "deldev" || act === "delzone") deleteSelected();
    else if (act === "propose") propose();
    else if (act === "delete") remove();
  });

  // Text fields: the snapshot is taken when a field is entered, and becomes one undo step when it is left changed.
  let fieldStart = null;
  document.addEventListener("focusin", (e) => { if (E && e.target.closest?.("#drw-editor") && e.target.matches("input, select, textarea")) fieldStart = snapshot(); });
  document.addEventListener("input", (e) => {
    if (!E || !e.target.closest?.("#drw-editor")) return;
    const t = e.target;
    if (t.dataset.meta) { E.meta[t.dataset.meta] = t.value; renderStatus(); return; }
    if (t.dataset.dev && E.sel?.kind === "device") {
      const d = E.content.devices[E.sel.i];
      if (!d) return;
      if (t.dataset.dev === "direction") { const v = Number(t.value); if (t.value !== "" && Number.isFinite(v)) d.direction = ((v % 360) + 360) % 360; else delete d.direction; }
      else if (t.dataset.dev === "type") { d.type = t.value; if (d.type !== "camera") delete d.direction; }
      else d[t.dataset.dev] = t.value.slice(0, t.dataset.dev === "label" ? 40 : 160);
      renderSvg(); renderLegend(); renderStatus();
      if (t.dataset.dev === "type") renderSide();
      return;
    }
    if (t.dataset.zone && E.sel?.kind === "zone") {
      const z = E.content.zones[E.sel.i];
      if (!z) return;
      if (t.dataset.zone === "number") { const v = Math.round(Number(t.value)); if (v >= 1 && v <= 999) z.number = v; }
      else z[t.dataset.zone] = t.value.slice(0, t.dataset.zone === "name" ? 60 : 40);
      renderSvg(); renderLegend(); renderStatus();
    }
  });
  document.addEventListener("change", (e) => {
    if (!E || !e.target.closest?.("#drw-editor")) return;
    if (e.target.matches('[data-f="paper"]')) { E.content.paper = e.target.value; renderStatus(); }
    if (fieldStart && fieldStart !== snapshot()) { pushUndo(fieldStart); fieldStart = snapshot(); renderTools(); }
  });
  document.addEventListener("submit", (e) => {
    if (e.target.id !== "drw-new") return;
    e.preventDefault();
    createFromUpload(e.target);
  });
  // Escape abandons a zone being drawn (instead of closing the pop-up); Ctrl+Z undoes outside text fields.
  window.addEventListener("keydown", (e) => {
    if (!E || $("#drw-editor")?.hidden) return;
    if (e.key === "Escape" && E.mode === "zone" && E.draft.length) { e.preventDefault(); e.stopPropagation(); E.draft = []; render(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.target.matches?.("input, textarea")) { e.preventDefault(); undo(); }
  }, true);
  window.addEventListener("resize", () => { if (E && !E.drag) renderSvg(); });

  window.JarvisDrawings = {
    init(h) { host = h; },
    load,
    open,
    dirty,
    close() { E = null; wide(false); },
  };
})();
