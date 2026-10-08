/* JARVIS HUD - the Faults pop-up: what of Jarvis's own has broken (services/faults.py), owner and manager only.
 *
 * GET /api/faults lists the open faults (what broke, how often, when, the error, Jarvis's diagnosis) and the recently closed ones
 * ("Resolved itself" / "Marked fixed"). Each open fault has "Copy report for Claude" (GET /api/faults/{id}/report: a redacted,
 * self-contained markdown report put on the clipboard for the owner to paste into Claude Code) and "Mark fixed"
 * (POST /api/faults/{id}/fixed). "Copy all open faults" copies one report of them all (GET /api/faults/report).
 *
 * Nothing here sends a report anywhere: it only ever reaches the clipboard of the person who pressed the button. The count lives on
 * the rail (hud.js renderRail, from /api/status), never over the chat. Loaded before hud.js; exposes window.JarvisFaults.
 */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let host = null;   // { api(path, opts), toast(title, body, level) } - supplied by hud.js
  const when = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); };

  function itemHtml(f, open) {
    const times = f.count === 1 ? "once" : `${f.count} times`;
    const seen = f.count === 1 ? `Seen ${esc(when(f.last_seen))}` : `Seen ${times}, first ${esc(when(f.first_seen))}, last ${esc(when(f.last_seen))}`;
    const closed = open ? "" : `<small class="mem-meta">${esc(f.status_label)}${f.resolved_at ? ` ${esc(when(f.resolved_at))}` : ""}${f.resolved_by ? ` - ${esc(f.resolved_by)}` : ""}</small>`;
    const controls = open ? `<div class="row"><button type="button" class="btn" data-fault="copy">Copy report for Claude</button>
        <button type="button" class="btn go" data-fault="fixed">Mark fixed</button></div>` : "";
    return `<li class="mem-item fault-item" data-id="${Number(f.id)}">
      <p class="mem-text">${esc(f.title)}</p>
      <small class="mem-meta">${esc(f.source_label)} · ${seen}</small>${closed}
      ${f.diagnosis ? `<small class="mem-meta">Jarvis thinks: ${esc(f.diagnosis)}</small>` : ""}
      ${f.error ? `<details class="fault-detail"><summary>Error</summary><pre>${esc(f.error)}</pre></details>` : ""}
      ${controls}</li>`;
  }

  function showError(msg) { const e = $("#faults-error"); if (!e) return; e.textContent = msg; e.hidden = !msg; }

  async function load() {
    if (!host || !$("#faults-open")) return;
    let data;
    try { data = await (await host.api("/api/faults")).json(); }
    catch { showError("Couldn't load the fault reports. Try again in a moment."); return; }
    showError("");
    const open = data.open || [], closed = data.closed || [];
    $("#faults-count").textContent = open.length ? String(open.length) : "";
    $("#faults-open").innerHTML = open.length ? open.map((f) => itemHtml(f, true)).join("")
      : `<li class="empty">Nothing open - Jarvis hasn't noticed anything broken in himself.</li>`;
    $("#faults-closed").innerHTML = closed.length ? closed.map((f) => itemHtml(f, false)).join("") : `<li class="empty">None in the last two weeks.</li>`;
    $("#btn-faults-copy-all").hidden = open.length < 1;
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; }
    catch {
      const t = document.createElement("textarea");
      t.value = text; t.setAttribute("readonly", ""); t.style.position = "fixed"; t.style.opacity = "0";
      document.body.appendChild(t); t.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      t.remove();
      return ok;
    }
  }

  async function copyReport(path, btn, what) {
    btn.disabled = true;
    try {
      const r = await host.api(path);
      const d = await r.json().catch(() => ({}));
      if (!r.ok || typeof d.markdown !== "string") { showError(typeof d.detail === "string" ? d.detail : "The report couldn't be built."); return; }
      if (await copyText(d.markdown)) host.toast("Copied", `${what} is on your clipboard - paste it into Claude Code.`);
      else showError("Your browser didn't allow copying. Try again, or use another browser.");
    } catch { showError("The report couldn't be built - try again."); }
    finally { btn.disabled = false; }
  }

  async function markFixed(li, btn) {
    btn.disabled = true;
    try {
      const r = await host.api(`/api/faults/${li.dataset.id}/fixed`, { method: "POST" });
      if (!r.ok && r.status !== 404) { const d = await r.json().catch(() => ({})); showError(typeof d.detail === "string" ? d.detail : "That couldn't be saved."); btn.disabled = false; return; }
      host.toast("Marked fixed", "It moves to Recently closed.");
      await load();
    } catch { showError("That couldn't be saved - try again."); btn.disabled = false; }
  }

  document.addEventListener("click", (e) => {
    const all = e.target.closest("#btn-faults-copy-all");
    if (all) { copyReport("/api/faults/report", all, "The report of every open fault"); return; }
    const b = e.target.closest("#pop-faults [data-fault]");
    if (!b) return;
    const li = b.closest(".fault-item");
    if (b.dataset.fault === "copy") copyReport(`/api/faults/${li.dataset.id}/report`, b, "The report");
    if (b.dataset.fault === "fixed") markFixed(li, b);
  });

  window.JarvisFaults = { init(h) { host = h; }, load };
})();
