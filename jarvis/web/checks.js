/* JARVIS HUD - "Question checks" in the Health drawer: the accuracy scorecard (services/question_checks.py).
 *
 * GET /api/checks (owner or manager): the latest run's score, per area, the trend over the last runs, the failing questions with
 * what was expected and what was said, and - for the principal owner only - the candidate checks made from replies marked Wrong,
 * "Run question checks now", and the marks for a check that is wrong or obsolete. A manager sees finance / people rows as
 * "Owner only" (the server hides them); a team console has no Health drawer at all (and the route refuses it).
 *
 * Nothing in here approves, sends or changes the business: Run asks the server to start a read-only check run, and the marks /
 * new checks only change the scorecard's own records. Loaded before hud.js; exposes window.JarvisChecks (hud.js calls load()
 * when the Health drawer opens).
 */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const AREAS = ["jobs", "engineers", "quotes", "money", "vans", "contracts", "stock", "upsells", "suggestions", "approvals", "policies", "standards", "refusals", "people", "other"];
  let host = null;
  let poll = null;
  let data = null;

  function when(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }) + " " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  const pct = (p) => (p === null || p === undefined ? "-" : `${p}%`);
  const level = (p) => (p === null || p === undefined ? "" : p >= 90 ? "ok" : p >= 70 ? "warn" : "bad");

  async function api(path, opts) { return host.api(path, opts); }

  function summaryText(d) {
    const sched = d.enabled ? `Weekly run on (${d.schedule}).` : "Weekly run is off - switch it on in Settings > Schedules.";
    if (d.running) return "Running the question checks now…";
    if (!d.last_run) return `No run yet. ${sched}`;
    const r = d.last_run;
    const graded = r.passed + r.failed;
    return `${Number(r.passed)} of ${Number(graded)} passed (${pct(r.pct)}) · ${when(r.at)} · ${Number(r.skipped)} skipped${r.errors ? ` · ${Number(r.errors)} errors` : ""}. ${sched}`;
  }

  function render(d) {
    data = d;
    const box = $("#qc");
    if (!box) return;
    $("#qc-score").textContent = d.last_run ? pct(d.last_run.pct) : "";
    $("#qc-score").dataset.level = d.last_run ? level(d.last_run.pct) : "";
    $("#qc-summary").textContent = summaryText(d);
    // trend: one bar per run, oldest first
    const trend = (d.trend || []).filter((t) => t.pct !== null && t.pct !== undefined);
    $("#qc-trend").innerHTML = trend.length > 1 ? trend.map((t) =>
      `<span class="qc-bar" data-level="${level(t.pct)}" style="height:${Math.max(4, Math.min(100, Number(t.pct) || 0))}%" title="${esc(when(t.at))}: ${esc(pct(t.pct))}"><span class="sr">${esc(when(t.at))}: ${esc(pct(t.pct))}</span></span>`).join("") : "";
    $("#qc-trend").hidden = trend.length <= 1;
    $("#qc-areas").innerHTML = (d.areas || []).map((a) =>
      `<li class="qc-area"><span>${esc(a.area)}</span><span class="qc-n">${Number(a.passed)}/${Number(a.passed) + Number(a.failed)}${a.skipped ? ` <span class="sub">(${Number(a.skipped)} skipped)</span>` : ""}</span><span class="qc-pct" data-level="${level(a.pct)}">${esc(pct(a.pct))}</span></li>`).join("");
    const failing = d.failing || [];
    $("#qc-failing").innerHTML = failing.length ? `<h4>Failing (${failing.length})</h4><ul class="list qc-fails">` + failing.map((f) => `
      <li class="qc-fail" data-check="${esc(f.check_id)}">
        <div class="qc-q"><span class="qc-tag">${esc(f.area)}${f.as !== "owner" ? ` · as ${esc(f.as)}` : ""}</span>${esc(f.question)}${f.flag ? ` <span class="qc-flag">${esc(f.flag)}</span>` : ""}</div>
        <dl class="qc-dl"><dt>Expected</dt><dd>${esc(f.expected)}</dd><dt>Said</dt><dd>${esc(f.given || (f.status === "error" ? "(no reply)" : ""))}</dd><dt>Why</dt><dd>${esc(f.reason)}</dd>
        ${f.coverage ? `<dt>Coverage</dt><dd>${esc(f.coverage)}</dd>` : ""}</dl>
        ${d.can_mark ? `<div class="row qc-marks"><button type="button" class="btn small" data-mark="wrong">Mark check wrong</button><button type="button" class="btn small" data-mark="obsolete">Mark obsolete</button>${f.flag ? `<button type="button" class="btn small" data-mark="clear">Clear mark</button>` : ""}</div>` : ""}
      </li>`).join("") + "</ul>" : (d.last_run ? `<p class="sub">Nothing failing in the last run.</p>` : "");
    const cands = d.candidates || [];
    $("#qc-candidates-wrap").hidden = !(d.can_run && cands.length);
    $("#qc-cand-count").textContent = cands.length ? `(${cands.length})` : "";
    $("#qc-candidates").innerHTML = cands.map((c) => `
      <li class="qc-cand" data-turn="${esc(c.turn_id)}">
        <div class="qc-q">${esc(c.question)}</div>
        ${c.note ? `<p class="sub">Your note: ${esc(c.note)}</p>` : ""}
        ${c.coverage ? `<p class="sub">${esc(c.coverage)}</p>` : ""}
        <div class="row"><button type="button" class="btn small" data-promote>Make it a check</button><button type="button" class="btn small" data-dismiss>Dismiss</button></div>
        <form class="qc-edit" hidden>
          <label>Question <input class="field" name="question" maxlength="400" value="${esc(c.question)}"></label>
          <label>Area <select class="field" name="area">${AREAS.map((a) => `<option${a === c.area ? " selected" : ""}>${a}</option>`).join("")}</select></label>
          <label>Expectation (JSON) <textarea class="field" name="expect" rows="6" spellcheck="false">${esc(JSON.stringify(c.template, null, 2))}</textarea></label>
          <p class="sub">Replace the placeholder with what the right answer must contain, or use number_from / must_mention_gap / checked_any (see checks/questions.yaml).</p>
          <p class="qc-err" role="alert" hidden></p>
          <div class="row"><button type="submit" class="btn go small">Save check</button><button type="button" class="btn small" data-cancel>Cancel</button></div>
        </form>
      </li>`).join("");
    const run = $("#btn-run-checks");
    run.hidden = !d.can_run;
    run.disabled = !!(d.running || d.run_blocked);
    run.title = d.run_blocked || "";
    $("#qc-run-note").textContent = d.can_run && d.run_blocked && !d.running ? d.run_blocked : "";
    $("#qc-problems").textContent = (d.problems || []).length ? "Suite problems: " + d.problems.join("; ") : "";
    clearTimeout(poll);
    if (d.running && $("#drawer").classList.contains("open") && !$("#pop-health").hidden) poll = setTimeout(load, 5000);
  }

  async function load() {
    if (!host || !$("#qc")) return;
    try {
      const r = await api("/api/checks");
      if (!r.ok) { $("#qc-summary").textContent = "The scorecard couldn't be loaded."; return; }
      render(await r.json());
    } catch { $("#qc-summary").textContent = "The scorecard couldn't be loaded."; }
  }

  async function post(path, body) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }

  document.addEventListener("click", async (e) => {
    if (!e.target.closest || !e.target.closest("#qc")) return;
    if (e.target.closest("#btn-run-checks")) {
      const r = await post("/api/checks/run");
      const out = r.ok ? await r.json() : {};
      host.toast(out.started ? "Question checks started" : "Couldn't start the checks", out.started ? "This takes a few minutes; the scorecard updates by itself." : (out.reason || "Try again later."), out.started ? "info" : "warning");
      load(); return;
    }
    const mark = e.target.closest("[data-mark]");
    if (mark) {
      const id = mark.closest("[data-check]").dataset.check;
      const r = await post(`/api/checks/${encodeURIComponent(id)}/mark`, { state: mark.dataset.mark });
      if (!r.ok) host.toast("Couldn't save that", "Try again in a moment.", "warning");
      load(); return;
    }
    const li = e.target.closest(".qc-cand");
    if (!li) return;
    if (e.target.closest("[data-promote]")) { li.querySelector(".qc-edit").hidden = false; li.querySelector("[name=expect]").focus(); return; }
    if (e.target.closest("[data-cancel]")) { li.querySelector(".qc-edit").hidden = true; return; }
    if (e.target.closest("[data-dismiss]")) { await post(`/api/checks/candidates/${encodeURIComponent(li.dataset.turn)}/dismiss`); load(); }
  });
  document.addEventListener("submit", async (e) => {
    const form = e.target.closest && e.target.closest(".qc-edit");
    if (!form) return;
    e.preventDefault();
    const li = form.closest(".qc-cand"), err = form.querySelector(".qc-err");
    let expect;
    try { expect = JSON.parse(form.expect.value); } catch { err.textContent = "The expectation isn't valid JSON."; err.hidden = false; return; }
    if (JSON.stringify(expect).includes("(replace with")) { err.textContent = "Replace the placeholder first."; err.hidden = false; return; }
    const r = await post(`/api/checks/candidates/${encodeURIComponent(li.dataset.turn)}`, { question: form.question.value.trim(), area: form.area.value, expect });
    if (!r.ok) { const body = await r.json().catch(() => ({})); err.textContent = typeof body.detail === "string" ? body.detail : "That expectation wasn't accepted."; err.hidden = false; return; }
    host.toast("Saved as a question check", "It runs with the others from now on.", "info");
    load();
  });

  window.JarvisChecks = { init(h) { host = h; }, load };
})();
