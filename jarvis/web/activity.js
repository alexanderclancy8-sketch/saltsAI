/* JARVIS HUD - the "What Jarvis did" pop-up: one place listing everything Jarvis drafted, emailed, proposed or changed, and what a
 * person approved or declined, so it can be checked quickly.
 *
 * It only READS: GET /api/activity (owner or manager session, same-origin). There is no button in here that approves, declines,
 * sends, retries or changes anything - a row that still needs a person offers "Open in Approvals", which is the ordinary drawer
 * switch (data-pop), and the real Approve / Don't send buttons stay where they always were. "Export CSV" (the principal owner
 * only) is a plain download link to /api/activity/export.csv with the filters that are showing.
 *
 * The list is paged by the server (50 at a time; "Show more", or scrolling to the end, loads the next page), newest first. By default
 * it shows what changed or was proposed; the quiet scheduled checks that found nothing are ONE collapsed line, and the tick box
 * brings every one of them in as a row. It lives in its own file, like memory.js; hud.js hands it api() and opens it (Drawer.show("activity")
 * calls load()). Loaded before hud.js; exposes window.JarvisActivity.
 */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const PAGE = 50;
  let host = null;
  const S = { range: "today", offset: 0, next: null, token: 0, busy: false, shown: 0, observer: null, timer: null };

  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  function when(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const t = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    return sameDay(d, new Date()) ? t : `${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}, ${t}`;
  }
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

  function params(extra = {}) {
    const p = new URLSearchParams({ range: S.range });
    const set = (k, v) => { if (v) p.set(k, v); };
    set("kind", $("#activity-kind").value);
    set("status", $("#activity-status").value);
    set("who", $("#activity-who").value);
    set("q", $("#activity-search").value.trim());
    if ($("#activity-everything").checked) p.set("everything", "true");
    for (const [k, v] of Object.entries(extra)) p.set(k, String(v));
    return p;
  }
  const filtered = () => !!($("#activity-kind").value || $("#activity-status").value || $("#activity-who").value || $("#activity-search").value.trim());

  function itemHtml(it, i) {
    const uid = `act-d-${i}`;
    const rows = (it.detail || []).map((d) =>
      `<div class="appr-row${d.block ? " block" : ""}"><dt>${esc(d.label)}</dt><dd>${esc(d.value)}</dd></div>`).join("");
    const decided = it.decided_by ? ` ${esc(it.status === "declined" ? "Declined" : it.status === "edited" ? "Edited" : it.status === "dismissed" ? "Dismissed" : "Approved")} by ${esc(it.decided_by)}${it.decided_at ? ` at ${esc(when(it.decided_at))}` : ""}.` : "";
    let link = "";
    if (it.link && it.link.pop) link = `<button type="button" class="btn small" data-pop="${esc(it.link.pop)}">${esc(it.link.label || "Open")}</button>`;
    else if (it.link && /^https:\/\//.test(it.link.href || "")) link = `<a class="btn small" href="${esc(it.link.href)}" target="_blank" rel="noopener noreferrer">${esc(it.link.label || "Open")}</a>`;
    return `<li class="act-item" data-id="${esc(it.id)}" data-status="${esc(it.status)}" data-kind="${esc(it.kind)}">
      <button type="button" class="act-row" aria-expanded="false" aria-controls="${uid}">
        <span class="act-time">${esc(when(it.when))}</span>
        <span class="act-what"><span class="act-kind">${esc(it.kind_label)}</span>${esc(it.what)}${it.sample ? ' <span class="act-sample">sample data</span>' : ""}</span>
        <span class="act-chip" data-status="${esc(it.status)}">${esc(it.status_label)}</span>
      </button>
      <div class="act-detail" id="${uid}" hidden>
        <p class="act-who">Requested by ${esc(it.requested_by || "Jarvis")}.${decided}</p>
        ${it.error ? `<p class="appr-fail"><b>What went wrong:</b> ${esc(it.error)}</p>` : ""}
        ${it.chain ? `<p class="appr-note">${esc(it.chain)}</p>` : ""}
        ${rows ? `<dl class="appr-detail">${rows}</dl>` : ""}
        <p class="act-ref">${esc(it.source_ref)}${it.created_at ? ` - raised ${esc(when(it.created_at))}` : ""}</p>
        ${link ? `<div class="row">${link}</div>` : ""}
      </div></li>`;
  }

  function showError(msg) {
    const e = $("#activity-error");
    e.innerHTML = msg ? `${esc(msg)} <button type="button" class="btn small" id="activity-retry">Try again</button>` : "";
    e.hidden = !msg;
  }
  function renderQuiet(q) {
    const box = $("#activity-quiet");
    if (!q || !q.count) { box.hidden = true; return; }
    box.hidden = false;
    const btn = $("#activity-quiet-btn");
    btn.textContent = `${plural(q.count, "check")} with nothing to report`;
    $("#activity-quiet-list").innerHTML = q.jobs.map((j) => `<li><span>${esc(j.name)}</span><span>${plural(j.count, "run")}, last ${esc(when(j.last))}</span></li>`).join("")
      + (q.note ? `<li class="act-note">${esc(q.note)}</li>` : "");
  }
  function renderWho(list) {
    const sel = $("#activity-who"), cur = sel.value;
    sel.innerHTML = `<option value="">Anyone</option>` + (list || []).map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    if (cur && ![...sel.options].some((o) => o.value === cur)) sel.insertAdjacentHTML("beforeend", `<option value="${esc(cur)}">${esc(cur)}</option>`);
    sel.value = cur;
  }
  function updateFold() {
    const on = ["#activity-kind", "#activity-status", "#activity-who"].filter((s) => $(s).value).length + ($("#activity-everything").checked ? 1 : 0);
    $("#activity-filters-summary").textContent = on ? `More filters (${on} on)` : "More filters";
  }
  function updateExport() {
    const a = $("#activity-export");
    if (a) a.href = `/api/activity/export.csv?${params()}`;
  }

  async function fetchPage(reset) {
    if (!host) return;
    if (S.busy && !reset) return;
    const token = ++S.token;
    S.busy = true;
    const more = $("#activity-more");
    more.disabled = true;
    if (reset) { S.offset = 0; S.shown = 0; }
    let data;
    try {
      const r = await host.api(`/api/activity?${params({ limit: PAGE, offset: S.offset })}`);
      if (!r.ok) throw new Error(String(r.status));
      data = await r.json();
    } catch (e) {
      if (token !== S.token) return;
      S.busy = false; more.disabled = false;
      showError("Couldn't load what Jarvis did. Try again in a moment.");
      return;
    }
    if (token !== S.token) return;   // a newer request (another filter) took over
    S.busy = false; more.disabled = false;
    showError("");
    const list = $("#activity-list");
    if (reset) {
      list.innerHTML = "";
      if (data.summary) $("#activity-summary").textContent = data.summary.line;
      renderWho(data.facets && data.facets.who);
      renderQuiet(data.quiet);
    }
    list.insertAdjacentHTML("beforeend", data.items.map((it, i) => itemHtml(it, S.shown + i)).join(""));
    S.shown += data.items.length;
    S.next = data.next_offset;
    S.offset = data.next_offset ?? S.offset;
    more.hidden = S.next === null;
    const empty = $("#activity-empty");
    empty.hidden = S.shown > 0;
    if (S.shown === 0) {
      empty.textContent = filtered() ? "Nothing matches those filters. Try a longer period, or clear the search."
        : S.range === "today" ? "Nothing yet today. Jarvis hasn't proposed or changed anything, and nothing has failed."
        : "Nothing in that period. Jarvis hasn't proposed or changed anything, and nothing has failed.";
    }
    const note = $("#activity-note");
    note.hidden = !data.capped;
    note.textContent = data.capped ? "That is as far back as this list can go in one go. Choose a shorter period or a single kind to see the rest." : "";
    const ex = $("#activity-export"); if (ex) ex.hidden = !data.can_export;
  }

  function reload() { updateExport(); updateFold(); return fetchPage(true); }
  function load() { return reload(); }

  document.addEventListener("click", (e) => {
    const pop = e.target.closest("#pop-activity");
    if (!pop) return;
    const range = e.target.closest(".act-range");
    if (range) {
      S.range = range.dataset.range;
      document.querySelectorAll(".act-range").forEach((b) => { const on = b === range; b.classList.toggle("is-on", on); b.setAttribute("aria-pressed", String(on)); });
      reload(); return;
    }
    const row = e.target.closest(".act-row");
    if (row) {
      const open = row.getAttribute("aria-expanded") !== "true";
      row.setAttribute("aria-expanded", String(open));
      document.getElementById(row.getAttribute("aria-controls")).hidden = !open;
      return;
    }
    if (e.target.closest("#activity-more")) { fetchPage(false); return; }
    if (e.target.closest("#activity-retry")) { reload(); return; }
    const q = e.target.closest("#activity-quiet-btn");
    if (q) {
      const open = q.getAttribute("aria-expanded") !== "true";
      q.setAttribute("aria-expanded", String(open));
      $("#activity-quiet-list").hidden = !open;
    }
  });
  document.addEventListener("change", (e) => { if (e.target.closest("#pop-activity") && e.target.matches("select, input[type=checkbox]")) reload(); });
  document.addEventListener("input", (e) => {
    if (!e.target.matches || !e.target.matches("#activity-search")) return;
    clearTimeout(S.timer);
    S.timer = setTimeout(reload, 250);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches && e.target.matches("#activity-search")) { e.preventDefault(); clearTimeout(S.timer); reload(); } });

  // Scrolling to the end of the list loads the next page (the "Show more" button is always there too).
  function watchEnd() {
    const sentinel = $("#activity-sentinel");
    if (!sentinel || !("IntersectionObserver" in window)) return;
    S.observer = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting) && S.next !== null && !S.busy && $("#drawer").classList.contains("open")) fetchPage(false);
    }, { root: $("#drawer-body"), rootMargin: "0px 0px 120px 0px" });
    S.observer.observe(sentinel);
  }

  // The filters sit open on a wide screen and folded away on a phone, so the list itself is what you see first.
  window.JarvisActivity = { init(h) { host = h; watchEnd(); if (!window.matchMedia("(max-width: 760px)").matches) $("#activity-filters").open = true; }, load };
})();
