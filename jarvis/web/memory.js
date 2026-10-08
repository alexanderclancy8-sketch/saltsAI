/* JARVIS HUD - the Memory pop-up: what Jarvis has learned about the business, each entry editable and deletable.
 *
 * Three lists, all from GET /api/memory: "Things Jarvis should know" (the notes saved in Settings), "Things Jarvis has
 * learned" (what he remembered himself) and "Learned replies" (the short replies offered in the message box). Edit
 * rewords an entry in place; Delete asks "Delete this?" first. Both go to the owner-authenticated endpoints under
 * /api/memory and only change what Jarvis reads from his next message on.
 *
 * "House rules" (services/rulebook.py) come first: how Jarvis works, each with who approved it and when. Only the principal owner
 * (can_edit_rules) sees Edit, Switch off / on and Delete; a manager sees the list. A new rule is never added here - Jarvis
 * suggests one and it waits in the owner's queue (this file never touches that queue).
 *
 * A second tab, "Customers & sites", holds Jarvis's notes on each customer and site (entity_notes.js); this file only switches
 * between the two tabs and hands the shared helpers on.
 *
 * It lives in its own file, like ask.js, so hud.js only needs a hook to open it (Drawer.show("memory") calls load())
 * and the shared api()/toast() helpers handed to init(). It has no connection to the approvals queue: nothing here
 * sends, approves or runs anything. Loaded before hud.js; exposes window.JarvisMemory.
 */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let host = null;        // { api(path, opts), toast(title, body, level) } - supplied by hud.js
  const SECTIONS = [
    { key: "notes", list: "#memory-notes", count: "#memory-notes-count", empty: "Nothing yet. Add notes under Settings, or tell Jarvis \"remember that...\".", kind: "fact" },
    { key: "learned", list: "#memory-learned", count: "#memory-learned-count", empty: "Nothing learned yet.", kind: "fact" },
    { key: "replies", list: "#memory-replies", count: "#memory-replies-count", empty: "No learned replies yet. Jarvis learns the short replies you keep typing.", kind: "reply" },
  ];
  let canEditRules = false;
  const day = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }); };

  function meta(kind, item) {
    if (kind === "reply") return `Used ${item.uses} ${item.uses === 1 ? "time" : "times"}${item.context && item.context !== "none" ? ` after ${esc(item.context)}s` : ""}`;
    return item.added ? `Added ${esc(day(item.added))}` : "";
  }
  function itemHtml(kind, it) {
    return `<li class="mem-item" data-kind="${kind}" data-id="${it.id}">
      <div class="mem-view"><p class="mem-text">${esc(it.text)}</p><small class="mem-meta">${meta(kind, it)}</small>
        <div class="row"><button type="button" class="btn" data-mem="edit">Edit</button><button type="button" class="btn stop" data-mem="ask-delete">Delete</button></div></div>
      <form class="mem-edit" hidden>
        ${kind === "reply" ? `<input type="text" class="mem-input" maxlength="80" value="${esc(it.text)}" aria-label="Reword this reply">`
          : `<textarea class="mem-input" rows="3" maxlength="1000" aria-label="Reword this memory">${esc(it.text)}</textarea>`}
        <p class="appr-error" role="alert" hidden></p>
        <div class="row"><button type="submit" class="btn go" data-mem="save">Save</button><button type="button" class="btn" data-mem="cancel">Cancel</button></div>
      </form>
      <div class="mem-confirm" role="group" aria-label="Confirm delete" hidden>
        <p>Delete this? ${kind === "fact" ? "Jarvis will stop using it from his next message." : "It will no longer be suggested."}</p>
        <div class="row"><button type="button" class="btn stop" data-mem="delete">Yes, delete</button><button type="button" class="btn" data-mem="keep">Keep it</button></div>
      </div></li>`;
  }

  // One house rule: the wording, whose Jarvis it applies to, who signed it off and when, and why. The owner gets the controls.
  function ruleHtml(r) {
    // (who signed it off and when, worded by the server: rulebook.listing's "signed_off" / "changed")
    const meta = [`Applies to: ${esc(r.scope_label)}`, esc(r.signed_off), esc(r.changed)].filter(Boolean).join(" · ") + (r.active ? "" : " · Switched off");
    const controls = canEditRules ? `<div class="row"><button type="button" class="btn" data-mem="edit">Edit</button>
        <button type="button" class="btn" data-mem="toggle" data-on="${r.active ? "0" : "1"}">${r.active ? "Switch off" : "Switch on"}</button>
        <button type="button" class="btn stop" data-mem="ask-delete">Delete</button></div>` : "";
    return `<li class="mem-item" data-kind="rule" data-id="${r.id}" data-off="${r.active ? "0" : "1"}">
      <div class="mem-view"><p class="mem-text">${esc(r.text)}</p><small class="mem-meta">${meta}</small>
        ${r.reason ? `<small class="mem-meta">Why: ${esc(r.reason)}</small>` : ""}${controls}</div>
      <form class="mem-edit" hidden>
        <textarea class="mem-input" rows="2" maxlength="200" aria-label="Reword this house rule">${esc(r.text)}</textarea>
        <p class="appr-error" role="alert" hidden></p>
        <div class="row"><button type="submit" class="btn go" data-mem="save">Save</button><button type="button" class="btn" data-mem="cancel">Cancel</button></div>
      </form>
      <div class="mem-confirm" role="group" aria-label="Confirm delete" hidden>
        <p>Delete this rule? Jarvis stops following it from his next message.</p>
        <div class="row"><button type="button" class="btn stop" data-mem="delete">Yes, delete</button><button type="button" class="btn" data-mem="keep">Keep it</button></div>
      </div></li>`;
  }
  function renderRules(block) {
    const rules = (block && block.rules) || [], waiting = (block && block.pending_text) || "";
    $("#memory-rules-count").textContent = rules.length ? String(rules.length) : "";
    $("#memory-rules").innerHTML = rules.length ? rules.map(ruleHtml).join("")
      : `<li class="empty">No house rules yet. When you correct Jarvis ("from now on..."), he suggests one for the owner to sign off.</li>`;
    const p = $("#memory-rules-pending");
    p.hidden = !waiting;
    p.textContent = waiting;
  }

  // Two tabs: "What Jarvis knows" (the three lists here) and "Customers & sites" (entity_notes.js, notes per Salts FSM record).
  function selectTab(name, focus) {
    document.querySelectorAll("#pop-memory .mem-tab").forEach((t) => {
      const on = t.dataset.memTab === name;
      t.setAttribute("aria-selected", on ? "true" : "false"); t.tabIndex = on ? 0 : -1;
      if (on && focus) t.focus();
    });
    document.querySelectorAll("#pop-memory .mem-panel").forEach((p) => { p.hidden = p.id !== `mem-panel-${name}`; });
    if (name === "entities") window.JarvisEntityNotes?.load();
  }
  document.addEventListener("click", (e) => { const t = e.target.closest("#pop-memory .mem-tab"); if (t) selectTab(t.dataset.memTab, false); });
  document.addEventListener("keydown", (e) => {
    const t = e.target.closest?.("#pop-memory .mem-tab"); if (!t || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const tabs = [...document.querySelectorAll("#pop-memory .mem-tab")], i = tabs.indexOf(t);
    const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
    e.preventDefault(); selectTab(tabs[next].dataset.memTab, true);
  });

  async function load() {
    if (!host) return;
    window.JarvisEntityNotes?.badge();
    let data;
    try { data = await (await host.api("/api/memory")).json(); }
    catch { showError("Couldn't load what Jarvis has learned. Try again in a moment."); return; }
    showError("");
    canEditRules = data.can_edit_rules === true;
    renderRules(data.rules);
    for (const sec of SECTIONS) {
      const items = data[sec.key] || [];
      $(sec.count).textContent = items.length ? String(items.length) : "";
      $(sec.list).innerHTML = items.length ? items.map((it) => itemHtml(sec.kind, it)).join("") : `<li class="empty">${esc(sec.empty)}</li>`;
    }
  }
  function showError(msg) { const e = $("#memory-error"); e.textContent = msg; e.hidden = !msg; }
  const path = (li) => `/api/memory/${{ reply: "replies", rule: "rules" }[li.dataset.kind] || "facts"}/${li.dataset.id}`;
  const show = (li, which) => {
    li.querySelector(".mem-view").hidden = which !== "view";
    li.querySelector(".mem-edit").hidden = which !== "edit";
    li.querySelector(".mem-confirm").hidden = which !== "confirm";
  };

  async function save(li) {
    const input = li.querySelector(".mem-input"), err = li.querySelector(".mem-edit .appr-error"), btn = li.querySelector('[data-mem="save"]');
    err.hidden = true; btn.disabled = true;
    try {
      const r = await host.api(path(li), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: input.value }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) { err.textContent = typeof data.detail === "string" ? data.detail : "That couldn't be saved."; err.hidden = false; return; }
      host.toast("Saved", "Jarvis will use the new wording from his next message.");
      await load();
    } catch { err.textContent = "That couldn't be saved - try again."; err.hidden = false; }
    finally { btn.disabled = false; }
  }
  async function toggle(li, b) {
    b.disabled = true;
    try {
      const r = await host.api(`${path(li)}/${b.dataset.on === "1" ? "on" : "off"}`, { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { showError(typeof d.detail === "string" ? d.detail : "That couldn't be changed."); b.disabled = false; return; }
      host.toast(b.dataset.on === "1" ? "Switched on" : "Switched off", "Jarvis reads the change from his next message.");
      await load();
    } catch { showError("That couldn't be changed - try again."); b.disabled = false; }
  }
  async function remove(li) {
    const btn = li.querySelector('[data-mem="delete"]'); btn.disabled = true;
    try {
      const r = await host.api(path(li), { method: "DELETE" });
      if (!r.ok && r.status !== 404) { const d = await r.json().catch(() => ({})); showError(typeof d.detail === "string" ? d.detail : "That couldn't be deleted."); btn.disabled = false; return; }
      host.toast("Deleted", { reply: "That reply won't be suggested any more.", rule: "Jarvis won't follow that rule any more." }[li.dataset.kind] || "Jarvis has forgotten it.");
      await load();
    } catch { showError("That couldn't be deleted - try again."); btn.disabled = false; }
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("#pop-memory [data-mem]");
    if (!b) return;
    const li = b.closest(".mem-item");
    switch (b.dataset.mem) {
      case "edit": show(li, "edit"); li.querySelector(".mem-input").focus(); break;
      case "cancel": li.querySelector(".mem-input").value = li.querySelector(".mem-input").defaultValue; li.querySelector(".mem-edit .appr-error").hidden = true; show(li, "view"); li.querySelector('[data-mem="edit"]').focus(); break;
      case "ask-delete": show(li, "confirm"); li.querySelector('[data-mem="keep"]').focus(); break;
      case "keep": show(li, "view"); li.querySelector('[data-mem="ask-delete"]').focus(); break;
      case "delete": remove(li); break;
      case "toggle": toggle(li, b); break;
    }
  });
  document.addEventListener("input", (e) => {
    const err = e.target.closest?.("#pop-memory .mem-edit")?.querySelector(".appr-error"); if (err) err.hidden = true;
  });
  document.addEventListener("submit", (e) => {
    const form = e.target.closest?.("#pop-memory .mem-edit"); if (!form) return;
    e.preventDefault(); save(form.closest(".mem-item"));
  });

  window.JarvisMemory = { init(h) { host = h; window.JarvisEntityNotes?.init(h); }, load, selectTab };
})();
