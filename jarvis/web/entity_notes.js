/* JARVIS HUD - the Memory pop-up's "Customers & sites" tab: Jarvis's notes on each customer and site (services/entity_memory.py).
 *
 * A search box and the list of customers / sites that have notes (name, when they were last updated, how many suggestions are
 * waiting); typing a name also lists matching Salts FSM records with no notes yet, so a first note can be added. Opening one shows
 * its pinned summary (Edit), the notes Jarvis suggested (Accept / Discard - only ever this click, never Jarvis himself), an Add note
 * field and the active notes (Edit / Delete, with a confirmation). The owner also gets "Forget everything on ...", after a confirm.
 *
 * Everything goes to the authenticated, same-origin endpoints under /api/entity-notes (owner and manager; a team console never has
 * this markup and the routes refuse it). Loaded before memory.js, which owns the tabs; exposes window.JarvisEntityNotes.
 */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let host = null;          // { api(path, opts), toast(title, body, level) } - supplied by hud.js through memory.js
  let current = null;       // { type, fsm_id } of the open customer / site, or null for the list
  let searchTimer = 0;
  let lastQuery = "";
  const TYPE = { customer: "Customer", site: "Site" };
  const day = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }); };
  const owner = () => document.body.dataset.role === "owner";
  const enc = encodeURIComponent;

  function showError(msg) { const e = $("#ent-error"); if (!e) return; e.textContent = msg; e.hidden = !msg; }
  async function json(r) { return r.json().catch(() => ({})); }
  const detail = (d, fallback) => (typeof d.detail === "string" ? d.detail : fallback);

  // ------------------------------------------------------------------ the list
  async function badge() {
    if (!host || !$("#ent-pending-count")) return;
    try {
      const d = await json(await host.api("/api/entity-notes"));
      const n = (d.entities || []).reduce((a, e) => a + (e.pending || 0), 0);
      $("#ent-pending-count").textContent = n ? String(n) : "";
    } catch { /* the badge is a nicety */ }
  }

  function rowHtml(e, fresh) {
    const meta = fresh ? `${TYPE[e.type]} · FSM ${esc(e.fsm_id)}${e.customer ? ` · ${esc(e.customer)}` : ""} · no notes yet`
      : `${TYPE[e.type]} · ${e.active} ${e.active === 1 ? "note" : "notes"} · updated ${esc(day(e.updated_at))}`;
    return `<li><button type="button" class="ent-row" data-ent="open" data-type="${esc(e.type)}" data-id="${esc(e.fsm_id)}" data-name="${esc(e.name)}">
      <span class="ent-name">${esc(e.name)}</span>${e.pending ? `<span class="ent-pending" title="Suggestions waiting">${e.pending} waiting</span>` : ""}
      <small class="ent-meta">${meta}</small></button></li>`;
  }

  async function list(q = lastQuery) {
    lastQuery = q;
    let d;
    try {
      const r = await host.api(`/api/entity-notes${q ? `?q=${enc(q)}` : ""}`);
      if (!r.ok) { showError(detail(await json(r), "Couldn't load the customer and site notes.")); return; }
      d = await json(r);
    } catch { showError("Couldn't load the customer and site notes. Try again in a moment."); return; }
    if (q !== lastQuery) return;                              // a newer search is on its way
    showError("");
    $("#ent-demo").hidden = !d.demo;
    const ents = d.entities || [];
    $("#ent-list").innerHTML = ents.length ? ents.map((e) => rowHtml(e, false)).join("")
      : `<li class="empty">${q ? "No notes match that." : "No notes yet. Find a customer or site above, or tell Jarvis \"remember for Acme: ...\"."}</li>`;
    const fsm = d.fsm_matches || [];
    $("#ent-fsm-sec").hidden = !fsm.length;
    $("#ent-fsm").innerHTML = fsm.map((e) => rowHtml(e, true)).join("");
    if (!q) { const n = ents.reduce((a, e) => a + (e.pending || 0), 0); $("#ent-pending-count").textContent = n ? String(n) : ""; }
  }

  function browse() {
    current = null;
    $("#ent-view").hidden = true; $("#ent-view").innerHTML = "";
    $("#ent-browse").hidden = false;
    return list();
  }

  // ------------------------------------------------------------------ one customer / site
  function noteHtml(n, pending) {
    const who = n.source === "jarvis-proposal" ? "Suggested by Jarvis" : `Added by ${esc(n.created_by || "someone")}`;
    const meta = `${who} · ${esc(day(n.created_at))}${n.kind === "summary" ? " · new summary" : ""}`;
    if (pending) {
      return `<li class="mem-item ent-item ent-suggestion" data-entry="${n.id}">
        ${n.flag ? `<p class="ent-flag">${esc(n.flag)}</p>` : ""}
        <p class="mem-text">${esc(n.text)}</p><small class="mem-meta">${meta}</small>
        <p class="appr-error" role="alert" hidden></p>
        <div class="row"><button type="button" class="btn go" data-ent="accept">Accept</button><button type="button" class="btn stop" data-ent="discard">Discard</button></div></li>`;
    }
    return `<li class="mem-item ent-item" data-entry="${n.id}">
      <div class="mem-view"><p class="mem-text">${esc(n.text)}</p><small class="mem-meta">${meta}</small>
        <div class="row"><button type="button" class="btn" data-ent="edit">Edit</button><button type="button" class="btn stop" data-ent="ask-delete">Delete</button></div></div>
      <form class="ent-edit" hidden>
        <textarea class="mem-input" rows="3" maxlength="300" aria-label="Reword this note">${esc(n.text)}</textarea>
        <p class="appr-error" role="alert" hidden></p>
        <div class="row"><button type="submit" class="btn go">Save</button><button type="button" class="btn" data-ent="cancel">Cancel</button></div>
      </form>
      <div class="mem-confirm" role="group" aria-label="Confirm delete" hidden>
        <p>Delete this note? Jarvis stops using it from his next message.</p>
        <div class="row"><button type="button" class="btn stop" data-ent="delete">Yes, delete</button><button type="button" class="btn" data-ent="keep">Keep it</button></div>
      </div></li>`;
  }

  function viewHtml(v) {
    const n = v.notes.length, p = v.pending.length;
    return `<button type="button" class="btn ent-back" data-ent="back">&larr; All customers &amp; sites</button>
      <h3 class="ent-title">${esc(v.name)}</h3><p class="ent-sub">${TYPE[v.type]} · Salts FSM ${esc(v.fsm_id)}</p>
      <div class="sec ent-summary-sec">
        <h3>Summary</h3>
        <div class="ent-summary-view"><p class="ent-summary ${v.summary ? "" : "empty"}">${v.summary ? esc(v.summary) : "No summary yet."}</p>
          <div class="row"><button type="button" class="btn" data-ent="edit-summary">${v.summary ? "Edit summary" : "Write a summary"}</button></div></div>
        <form class="ent-summary-edit" hidden>
          <textarea class="mem-input" rows="4" maxlength="800" aria-label="Summary">${esc(v.summary)}</textarea>
          <p class="appr-error" role="alert" hidden></p>
          <div class="row"><button type="submit" class="btn go">Save summary</button><button type="button" class="btn" data-ent="cancel-summary">Cancel</button></div>
        </form>
      </div>
      <div class="sec" id="ent-pending-sec"${p ? "" : " hidden"}>
        <h3>Suggested, waiting for you <span class="count">${p}</span></h3>
        <ul class="mem-list">${v.pending.map((x) => noteHtml(x, true)).join("")}</ul>
      </div>
      <div class="sec">
        <h3>Add a note</h3>
        <form class="ent-add">
          <textarea class="mem-input" id="ent-add-text" rows="2" maxlength="300" placeholder="e.g. Ring the site manager before sending anyone" aria-label="New note"${v.demo ? " disabled" : ""}></textarea>
          <p class="appr-error" role="alert" hidden></p>
          <div class="row"><button type="submit" class="btn go"${v.demo ? " disabled" : ""}>Add note</button></div>
          <small class="mem-meta">No codes, passwords, phone numbers or anything personal - those stay in Salts FSM.</small>
        </form>
      </div>
      <div class="sec">
        <h3>Notes <span class="count">${n}/${v.cap}</span></h3>
        <ul class="mem-list" id="ent-notes">${n ? v.notes.map((x) => noteHtml(x, false)).join("") : `<li class="empty">No notes yet.</li>`}</ul>
      </div>
      ${owner() ? `<div class="sec ent-forget">
        <button type="button" class="btn stop" data-ent="ask-forget">Forget everything on ${esc(v.name)}</button>
        <div class="mem-confirm" role="group" aria-label="Confirm forget" hidden>
          <p>Forget every note, suggestion and the summary on ${esc(v.name)}? This can't be undone.</p>
          <div class="row"><button type="button" class="btn stop" data-ent="forget">Yes, forget everything</button><button type="button" class="btn" data-ent="keep-all">Keep them</button></div>
        </div></div>` : ""}`;
  }

  async function open(type, id, focusBack = true) {
    let r;
    try { r = await host.api(`/api/entity-notes/${enc(type)}/${enc(id)}`); }
    catch { showError("Couldn't open those notes. Try again in a moment."); return; }
    if (r.status === 404) {            // a Salts FSM record with no notes yet: an empty view to add the first one
      const name = document.querySelector(`#ent-fsm [data-id="${CSS.escape(id)}"][data-type="${CSS.escape(type)}"]`)?.dataset.name || id;
      return render({ type, fsm_id: id, name, summary: "", notes: [], pending: [], cap: 40, demo: false, fresh: true }, focusBack);
    }
    if (!r.ok) { showError(detail(await json(r), "Couldn't open those notes.")); return; }
    render(await json(r), focusBack);
  }

  function render(v, focusBack) {
    current = { type: v.type, fsm_id: v.fsm_id };
    showError("");
    $("#ent-browse").hidden = true;
    const el = $("#ent-view");
    el.innerHTML = viewHtml(v);
    if (v.fresh) el.querySelector(".ent-summary-sec").hidden = true;   // a summary needs at least one note first
    el.hidden = false;
    if (focusBack) el.querySelector('[data-ent="back"]').focus({ preventScroll: true });
    $("#drawer-body") && ($("#drawer-body").scrollTop = 0);
  }

  const reopen = () => current && open(current.type, current.fsm_id, false);
  const base = () => `/api/entity-notes/${enc(current.type)}/${enc(current.fsm_id)}`;
  const send = (path, method, body) => host.api(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });

  async function run(btn, errEl, call, ok) {
    if (errEl) errEl.hidden = true;
    btn.disabled = true;
    try {
      const r = await call();
      const d = await json(r);
      if (!r.ok) {
        const msg = detail(d, "That couldn't be done.");
        if (errEl) { errEl.textContent = msg; errEl.hidden = false; } else showError(msg);
        return false;
      }
      if (ok) host.toast(ok[0], ok[1]);
      return true;
    } catch { const msg = "That couldn't be done - try again."; if (errEl) { errEl.textContent = msg; errEl.hidden = false; } else showError(msg); return false; }
    finally { btn.disabled = false; }
  }

  // ------------------------------------------------------------------ events
  document.addEventListener("input", (e) => {
    if (e.target.id === "ent-search") {
      clearTimeout(searchTimer);
      const q = e.target.value.trim();
      searchTimer = setTimeout(() => list(q), 250);
      return;
    }
    const err = e.target.closest?.("#ent-view form")?.querySelector(".appr-error"); if (err) err.hidden = true;
  });

  document.addEventListener("click", async (e) => {
    const b = e.target.closest("#pop-memory [data-ent]");
    if (!b || !host) return;
    const li = b.closest(".ent-item");
    const show = (which) => {
      li.querySelector(".mem-view").hidden = which !== "view";
      li.querySelector(".ent-edit").hidden = which !== "edit";
      li.querySelector(".mem-confirm").hidden = which !== "confirm";
    };
    switch (b.dataset.ent) {
      case "open": open(b.dataset.type, b.dataset.id); break;
      case "back": await browse(); $("#ent-search")?.focus({ preventScroll: true }); break;
      case "edit": show("edit"); li.querySelector(".mem-input").focus(); break;
      case "cancel": { const t = li.querySelector(".mem-input"); t.value = t.defaultValue; show("view"); li.querySelector('[data-ent="edit"]').focus(); break; }
      case "ask-delete": show("confirm"); li.querySelector('[data-ent="keep"]').focus(); break;
      case "keep": show("view"); li.querySelector('[data-ent="ask-delete"]').focus(); break;
      case "delete":
        if (await run(b, null, () => send(`/api/entity-notes/entry/${li.dataset.entry}`, "DELETE"), ["Deleted", "Jarvis won't use that note any more."])) reopen();
        break;
      case "accept":
      case "discard": {
        const err = li.querySelector(".appr-error");
        const done = b.dataset.ent === "accept" ? ["Accepted", "Jarvis will use it from his next message."] : ["Discarded", "Jarvis won't suggest that again."];
        if (await run(b, err, () => send(`/api/entity-notes/entry/${li.dataset.entry}/${b.dataset.ent}`, "POST"), done)) { reopen(); badge(); }
        break;
      }
      case "edit-summary": {
        const sec = b.closest(".ent-summary-sec");
        sec.querySelector(".ent-summary-view").hidden = true; sec.querySelector(".ent-summary-edit").hidden = false;
        sec.querySelector(".mem-input").focus(); break;
      }
      case "cancel-summary": {
        const sec = b.closest(".ent-summary-sec"), t = sec.querySelector(".mem-input");
        t.value = t.defaultValue; sec.querySelector(".ent-summary-edit").hidden = true; sec.querySelector(".ent-summary-view").hidden = false;
        sec.querySelector('[data-ent="edit-summary"]').focus(); break;
      }
      case "ask-forget": { const c = b.parentElement.querySelector(".mem-confirm"); c.hidden = false; b.hidden = true; c.querySelector('[data-ent="keep-all"]').focus(); break; }
      case "keep-all": { const sec = b.closest(".ent-forget"); sec.querySelector(".mem-confirm").hidden = true; const ask = sec.querySelector('[data-ent="ask-forget"]'); ask.hidden = false; ask.focus(); break; }
      case "forget":
        if (await run(b, null, () => send(`${base()}/forget`, "POST", { confirm: true }), ["Forgotten", "Every note on them has been removed."])) { await browse(); badge(); }
        break;
    }
  });

  document.addEventListener("submit", async (e) => {
    const form = e.target.closest?.("#ent-view form"); if (!form || !current) return;
    e.preventDefault();
    const btn = form.querySelector('[type="submit"]'), err = form.querySelector(".appr-error"), text = form.querySelector(".mem-input").value;
    if (form.classList.contains("ent-add")) {
      if (await run(btn, err, () => send(`${base()}/notes`, "POST", { text }), ["Noted", "Jarvis will use it from his next message."])) reopen();
    } else if (form.classList.contains("ent-summary-edit")) {
      if (await run(btn, err, () => send(`${base()}/summary`, "POST", { text }), ["Saved", "The summary is updated."])) reopen();
    } else if (form.classList.contains("ent-edit")) {
      const li = form.closest(".ent-item");
      if (await run(btn, err, () => send(`/api/entity-notes/entry/${li.dataset.entry}`, "POST", { text }), ["Saved", "Jarvis will use the new wording from his next message."])) reopen();
    }
  });

  window.JarvisEntityNotes = {
    init(h) { host = h; },
    badge,
    load() { if (!host) return; return current ? reopen() : list(); },
    open,
  };
})();
