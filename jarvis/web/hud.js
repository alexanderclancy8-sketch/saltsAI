/* JARVIS HUD - live display, conversation, voice in/out. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const store = {
    get(k, d) { try { const v = localStorage.getItem("jarvis." + k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("jarvis." + k, v); } catch { /* private mode */ } },
  };
  const S = {
    status: null, voice: { tts: "browser", stt: "browser", wake_word: "jarvis", language: "en-GB", ack_fillers: true, silence_ms: 1200 },
    ws: null, approvals: [], suggestions: [], hudState: "idle", level: 0, targetLevel: 0,
    listenMode: store.get("listen", "ptt"), speakPref: store.get("speak", "voice"), voiceId: store.get("voice", ""),
    lastMode: "typed", followUpUntil: 0, micUntil: 0, voiceTurn: false, attachments: [],
    // sent: what THIS tab has sent whose "user_message" has not come back yet. mine: whether the turn now streaming in
    // is one of them. Every open tab/device gets every event, so without this each one read the same reply aloud - two
    // (or more) overlapping voices. Only the tab that asked speaks the answer; the others just show it.
    sent: [], mine: true,
    // followUpUntil: until when a heard utterance may skip the wake word - only ever set by grantFollowUp(), i.e.
    // after a genuine exchange. micUntil: until when the real mic is kept open (extendFollowUp) - says nothing
    // about whether the wake word may be skipped. voiceTurn: false | "pending" (accepted spoken request, no reply
    // yet) | "replied" (reply received, waiting for Jarvis to finish speaking it).
    // Barge-in (talk over Jarvis with the wake word). On by default because the echo guards below are always in
    // place; the owner can switch it off in Settings if it misfires on speakers. See bargeInAllowed().
    bargeIn: store.get("bargein", "1") !== "0", captureUntil: 0,
    data: {}, // the latest panel data from /api/status, kept for the rail counts and the "Needs you" strip
    // Per-session mute for Jarvis-initiated messages (sessionStorage, so another tab or a fresh visit starts unmuted).
    // The server is told too (sendProactiveMute), so a muted session isn't sent them at all.
    proactiveMuted: (() => { try { return sessionStorage.getItem("jarvis.pmute") === "1"; } catch { return false; } })(),
  };

  // ------------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => (n === null || n === undefined || isNaN(n)) ? "-" : "£" + Math.round(n).toLocaleString("en-GB");
  const time = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); };
  // How a message is recognised when the server announces it ("user_message"): the same words, whatever the spacing.
  const turnKey = (t) => String(t || "").replace(/\s+/g, " ").trim().slice(0, 300);
  const dayMonth = (iso) => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }); };

  // A schedule setting is stored as a crontab string ("45 7 * * 1-5", or "5 9,13,16 * * 1-5" for a few times a
  // day) but nobody wants to type that - these convert it to/from a plain picker: one shared minute-past-the-
  // hour, a list of hours (almost always just one), and either days-of-the-week or a single day of the month.
  // Day index here is 0=Monday..6=Sunday throughout, matching how the picker lays its buttons out.
  const DOW_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const cronDowToIndex = (tok) => { const n = parseInt(tok, 10); if (Number.isNaN(n)) return null; const m = n % 7; return m === 0 ? 6 : m - 1; };
  const indexToCronDow = (i) => (i === 6 ? 0 : i + 1);
  function parseCron(cron) {
    const parts = String(cron || "").trim().split(/\s+/);
    if (parts.length !== 5) return { ok: false };
    const [min, hour, dom, month, dow] = parts;
    if (month !== "*" || !/^\d+$/.test(min)) return { ok: false };
    const hours = hour.split(",").map((h) => parseInt(h, 10));
    if (!hours.length || hours.some((h) => Number.isNaN(h))) return { ok: false };
    const minute = Number(min);
    if (dom !== "*") {
      // A day-of-month schedule ("on the 1st of the month at 7:45") - only the simple single-time case is
      // worth a picker for; the combination of a specific date AND several times a day doesn't happen here.
      const day = parseInt(dom, 10);
      if (Number.isNaN(day) || hours.length !== 1 || dow !== "*") return { ok: false };
      return { ok: true, mode: "dom", minute, hours, day };
    }
    let days;
    if (dow === "*") days = new Set([0, 1, 2, 3, 4, 5, 6]);
    else {
      days = new Set();
      for (const tok of dow.split(",")) {
        const range = tok.match(/^(\d+)-(\d+)$/);
        if (range) { for (let n = +range[1]; n <= +range[2]; n++) { const i = cronDowToIndex(String(n)); if (i === null) return { ok: false }; days.add(i); } }
        else { const i = cronDowToIndex(tok); if (i === null) return { ok: false }; days.add(i); }
      }
    }
    return { ok: true, mode: "dow", minute, hours, days };
  }
  function cronFromParts(minute, hours, extra) {
    const hourField = [...new Set(hours)].sort((a, b) => a - b).join(",");
    if (extra.mode === "dom") return `${minute} ${hourField} ${extra.day} * *`;
    const dowField = extra.days.size === 7 ? "*" : [...extra.days].map(indexToCronDow).sort((a, b) => a - b).join(",");
    return `${minute} ${hourField} * * ${dowField}`;
  }

  function md(src) {
    const blocks = [];
    let text = esc(src).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, l, code) => { blocks.push(`<pre><code>${code}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
    const inline = (t) => t
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const lines = text.split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
        let html = "<table><thead><tr>" + cells(line).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { html += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>"; i++; }
        out.push(html + "</tbody></table>");
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { out.push(`<h${Math.min(h[1].length + 1, 4)}>${inline(h[2])}</h${Math.min(h[1].length + 1, 4)}>`); i++; continue; }
      if (/^\s*([-*•]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        let html = ordered ? "<ol>" : "<ul>";
        while (i < lines.length && /^\s*([-*•]|\d+\.)\s+/.test(lines[i])) { html += "<li>" + inline(lines[i].replace(/^\s*([-*•]|\d+\.)\s+/, "")) + "</li>"; i++; }
        out.push(html + (ordered ? "</ol>" : "</ul>"));
        continue;
      }
      if (!line.trim()) { i++; continue; }
      let para = line;
      i++;
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*([-*•]|\d+\.)\s|\s*\|)/.test(lines[i])) { para += "<br>" + lines[i]; i++; }
      out.push(`<p>${inline(para)}</p>`);
    }
    return out.join("").replace(/\u0000(\d+)\u0000/g, (_, n) => blocks[+n]);
  }

  function toast(title, body = "", level = "info") {
    const el = document.createElement("div");
    el.className = `toast ${level}`;
    el.innerHTML = `<b>${esc(title)}</b>${esc(body).slice(0, 240)}`;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), level === "critical" ? 20000 : 8000);
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", ...opts });
    if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
    return r;
  }

  // ------------------------------------------------------------------ clock
  function tick() {
    const now = new Date();
    $("#clock").textContent = `${now.toLocaleTimeString("en-GB")} · ${now.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}`;
  }
  setInterval(tick, 1000); tick();

  // ------------------------------------------------------------------ HUD state + reactor
  // What he calls the owner follows the "How Jarvis talks" setting (the server sends it as status.address).
  const address = () => S.status?.address || "sir";
  const STATE_LABEL = { idle: "Online", listening: "Listening", thinking: "Working", speaking: "Speaking", get awaiting() { return `Yes, ${address()}?`; } };
  function setHud(state) {
    S.hudState = state;
    $("#state").textContent = STATE_LABEL[state] || state;
    document.body.dataset.hud = state; // the status chip and the core follow it (hud.css, core.js)
    // Send becomes Stop for as long as a reply is being worked out or spoken.
    const busy = ["thinking", "speaking"].includes(state);
    $("#btn-stop").hidden = !busy;
    $("#btn-send").hidden = busy;
    // A turn just started - don't let a sleep check meant for the *previous* lull fire part-way through it.
    if (state === "thinking") clearTimeout(sleepTimer);
  }
  function caption(text, interim = "") { const el = $("#caption"); el.classList.remove("error"); el.innerHTML = esc(text) + (interim ? ` <span class="interim">${esc(interim)}</span>` : ""); }
  function captionError(text) { caption(text); $("#caption").classList.add("error"); }

  // ------------------------------------------------------------------ pop-up drawer
  // ONE drawer component: every section of the console (and Settings / Connections) is a .pop inside #drawer-body.
  // Drawer.show(name) reveals one and slides the drawer in from the right; it is closed by the Close button,
  // Escape, or a click on the scrim (outside). It is never wider than the viewport (see .drawer in hud.css).
  const POPS = ["approvals", "comms", "issues", "health", "ops", "fleet", "finance", "presence", "upcoming", "demo", "settings", "connections"];
  const Drawer = {
    current: null, opener: null,
    isOpen() { return $("#drawer").classList.contains("open"); },
    show(name, opener) {
      if (!POPS.includes(name)) return;
      const first = !this.isOpen();
      if (first) this.opener = opener || document.activeElement;
      this.current = name;
      $$("#drawer-body .pop").forEach((p) => { p.hidden = p.id !== `pop-${name}`; });
      $("#drawer-title").textContent = $(`#pop-${name}`).dataset.title;
      $("#drawer-body").scrollTop = 0;
      $("#drawer").classList.add("open"); $("#scrim").classList.add("open");
      $("#drawer").setAttribute("aria-hidden", "false");
      $$(".rail-item, .tb-btn[data-pop]").forEach((b) => b.classList.toggle("is-active", b.dataset.pop === name));
      if (first) $("#drawer-close").focus({ preventScroll: true }); // keyboard/screen-reader users land inside the dialog
      if (name === "fleet") fitMapSoon();             // the map is laid out at 0x0 while hidden; re-measure now it can be seen
      if (name === "demo") renderDemo();
      if (name === "settings" || name === "connections") {
        if (!Settings.loaded) Settings.load();
        if (name === "connections") Settings.showList();
      }
    },
    // Returns false if the owner chose to keep unsaved connection edits instead of closing.
    close() {
      if (!this.isOpen()) return true;
      if (Settings.dirty() && !confirm("Discard unsaved connection changes?")) return false;
      Settings.revert();
      $("#drawer").classList.remove("open"); $("#scrim").classList.remove("open");
      $("#drawer").setAttribute("aria-hidden", "true");
      $$(".rail-item, .tb-btn[data-pop]").forEach((b) => b.classList.remove("is-active"));
      const back = this.opener; this.opener = null; this.current = null;
      if (back && document.contains(back) && typeof back.focus === "function") back.focus({ preventScroll: true });
      return true;
    },
  };
  $("#drawer-close").addEventListener("click", () => Drawer.close());
  $("#scrim").addEventListener("click", () => Drawer.close());
  document.addEventListener("click", (e) => { const b = e.target.closest("[data-pop]"); if (b) Drawer.show(b.dataset.pop, b); });
  // Keep Tab inside the open drawer.
  $("#drawer").addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const els = $$("#drawer button:not([disabled]), #drawer a[href], #drawer input:not([disabled]), #drawer select, #drawer textarea, #drawer summary")
      .filter((x) => x.offsetParent !== null);
    if (!els.length) return;
    const first = els[0], last = els[els.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  // ------------------------------------------------------------------ rail counts and the "Needs you" strip
  // Both are computed from the same status data the pop-ups render. A rail count turns amber when that section needs a
  // look and red when something is failing; "Needs you" shows the three most urgent things, each opening its pop-up.
  function setRail(name, n, level, text) {
    const item = $(`.rail-item[data-pop="${name}"]`), count = $(`#rc-${name}`);
    if (!item || !count) return;
    count.textContent = String(n);
    item.dataset.level = level || "";
    item.dataset.label = item.dataset.label || item.firstChild.textContent.trim();
    item.setAttribute("aria-label", `${item.dataset.label}: ${text}`);
  }
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const within24h = (iso) => { const t = Date.parse(iso); return !Number.isNaN(t) && Date.now() - t < 86400000; };
  function renderRail() {
    const d = S.data || {};
    const needs = [];
    const add = (weight, level, text, pop) => needs.push({ weight, level, text, pop });

    // Counts follow the design mockup: Approvals = actions waiting for a click (suggestions are listed inside the pop-up and
    // in "Needs you"); Comms = unread; Issues = open; Health = passed/total; Ops = things needing attention; Fleet reads
    // "off" when vehicle tracking is not connected; Finance = customers to watch; Presence has no count; Coming up = listed.
    const na = S.approvals?.length || 0, ns = S.suggestions?.length || 0;
    setRail("approvals", na, na ? "warn" : "", na ? `${plural(na, "action", "actions")} waiting for your approval` : "nothing waiting for you");
    if (na) add(80, "warn", `${plural(na, "action", "actions")} waiting for your approval`, "approvals");
    if (ns) add(35, "warn", `${plural(ns, "suggestion", "suggestions")} from Jarvis`, "approvals");

    const unread = Array.isArray(d.inbox?.unread) ? d.inbox.unread : [];
    const hot = unread.filter((m) => m.importance === "high").length;
    setRail("comms", unread.length, unread.length ? "warn" : "", `${plural(unread.length, "unread message", "unread messages")}${hot ? `, ${hot} important` : ""}`);
    if (unread.length) add(hot ? 40 : 20, "warn", `${unread.length} unread`, "comms");

    const issues = d.issues || [];
    const severe = issues.filter((i) => ["critical", "high"].includes(i.severity)).length;
    setRail("issues", issues.length, severe ? "bad" : issues.length ? "warn" : "", `${issues.length} open${severe ? `, ${severe} serious` : ""}`);
    if (severe) add(90, "bad", `${plural(severe, "serious issue", "serious issues")} open`, "issues");

    const tests = d.tests || [];
    const failing = tests.filter((t) => !t.ok).length;
    const alerts = (d.notifications || []).filter((n) => (n.level === "critical" || n.level === "warning") && within24h(n.created_at)).length;
    setRail("health", tests.length ? `${tests.length - failing}/${tests.length}` : "0", failing ? "bad" : alerts ? "warn" : "", tests.length ? `${tests.length - failing} of ${tests.length} routine tests passing${alerts ? `, ${plural(alerts, "alert", "alerts")}` : ""}` : "no test results yet");
    if (failing) add(100, "bad", `${failing} ${failing === 1 ? "check" : "checks"} failing`, "health");
    if (alerts && !failing) add(30, "warn", `${plural(alerts, "alert", "alerts")} in the last day`, "health");

    const staff = d.staff && !d.staff.error ? d.staff : null;
    const late = staff ? staff.late_starts.length : 0;
    const overdue = Array.isArray(d.overdue_jobs) ? d.overdue_jobs.length : 0;
    setRail("ops", late + overdue, late + overdue ? "warn" : "", staff ? `${plural(late, "late start", "late starts")}, ${overdue} overdue` : "unavailable");
    if (overdue) add(70, "warn", `${plural(overdue, "job", "jobs")} overdue`, "ops");
    if (late) add(60, "warn", `${plural(late, "late start", "late starts")} today`, "ops");

    const vans = S.tracking?.engineers?.length || 0;
    const fleet = fleetState();
    const tracked = fleet.live;
    setRail("fleet", tracked ? vans : fleet.failing ? "failing" : "off", fleet.failing ? "bad" : "",
      tracked ? `${plural(vans, "vehicle", "vehicles")} reporting` : fleet.failing ? `vehicle tracking is not connected: ${fleet.why}` : "vehicle tracking is not connected");
    if (fleet.failing) add(55, "bad", "RAM Tracking isn't connected", "fleet");

    const f = d.finance && !d.finance.error ? d.finance : null;
    const watch = d.customer_watch || [];
    const atRisk = watch.filter((c) => c.status === "at risk").length;
    setRail("finance", watch.length, watch.length ? "warn" : "", `${plural(watch.length, "customer", "customers")} to watch`);
    if (f && f.debtors_overdue > 0.3 * f.debtors_total) add(50, "warn", `${money(f.debtors_overdue)} overdue from customers`, "finance");
    if (atRisk) add(45, "warn", `${plural(atRisk, "customer", "customers")} at risk`, "finance");

    const dl = deadlineItems(d.deadlines, d.accreditations);
    const late2 = dl.filter((x) => x.days_left < 0).length;
    setRail("upcoming", dl.length, late2 ? "bad" : "", `${plural(dl.length, "reminder", "reminders")}${late2 ? `, ${late2} overdue` : ""}`);
    if (late2) add(75, "bad", `${plural(late2, "reminder", "reminders")} overdue`, "upcoming");
    const next = dl.find((x) => x.days_left >= 0 && x.days_left <= 7);
    if (next) add(10, "", next.what, "upcoming");

    needs.sort((a, b) => b.weight - a.weight);
    const top = needs.slice(0, 3);
    $("#needs-list").innerHTML = !S.status ? `<span class="needs-clear">Loading…</span>`
      : top.length ? top.map((n) => `<button type="button" class="need" data-pop="${n.pop}" data-level="${n.level}"><span class="need-dot" aria-hidden="true"></span><span class="need-text">${esc(n.text)}</span><span class="need-go" aria-hidden="true">›</span></button>`).join("")
      : `<span class="needs-clear">Nothing needs you right now.</span>`;
  }

  // Approvals/suggestions must never go silently unnoticed - the rail count and "Needs you" cover it, and a small
  // pulsing badge on the core itself opens the real Approvals pop-up (with its working Approve/Cancel buttons).
  function updateOrbBadge() {
    const n = (S.approvals?.length || 0) + (S.suggestions?.length || 0);
    const badge = $("#orb-badge");
    badge.hidden = n === 0;
    if (n) badge.textContent = String(n);
    renderRail();
  }
  $("#orb-badge").addEventListener("click", () => Drawer.show("approvals", $("#orb-badge")));

  // The core: a canvas animation whose colour and energy follow the agent's state (core.js). Clicking it starts or
  // stops listening exactly like the microphone button. Under prefers-reduced-motion it draws one static frame.
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const canvas = $("#reactor");
  window.JarvisCore?.mount(canvas, {
    reduced: () => reduceMotion.matches,
    frame(t) {
      S.level += (S.targetLevel - S.level) * 0.2;
      if (S.hudState === "speaking" && speaker.browserSpeaking) S.targetLevel = 0.3 + 0.25 * Math.abs(Math.sin(t / 90));
      return { state: S.hudState, level: S.level };
    },
  });
  $("#core-btn").addEventListener("click", () => $("#btn-mic").click());

  // ------------------------------------------------------------------ audio / speech output
  let audioCtx = null, analyser = null, levelData = null;
  const player = new Audio();
  player.crossOrigin = "anonymous";
  function ensureAudio() {
    if (audioCtx) { if (audioCtx.state === "suspended") audioCtx.resume(); return; }
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = audioCtx.createMediaElementSource(player);
      analyser = audioCtx.createAnalyser(); analyser.fftSize = 256;
      levelData = new Uint8Array(analyser.frequencyBinCount);
      src.connect(analyser); analyser.connect(audioCtx.destination);
      const meter = () => {
        if (analyser && !player.paused) { analyser.getByteFrequencyData(levelData); S.targetLevel = Math.min(1, levelData.reduce((a, b) => a + b, 0) / levelData.length / 90); }
        else if (!speaker.browserSpeaking && S.hudState !== "listening") S.targetLevel = 0;
        requestAnimationFrame(meter);
      };
      meter();
    } catch (e) { console.warn("audio context", e); }
  }
  ["click", "keydown", "touchstart"].forEach((ev) => window.addEventListener(ev, () => { ensureAudio(); }, { once: false, passive: true }));

  // Say when the real voice fails, instead of silently switching to the browser's robotic one.
  let lastVoiceProblem = 0;
  function voiceProblem(detail) {
    console.warn("voice fallback:", detail);
    if (Date.now() - lastVoiceProblem < 120000) return;
    lastVoiceProblem = Date.now();
    toast("Using the browser voice", `The ${S.voice.tts} voice didn't work: ${detail}`, "warning");
  }

  function pickBrowserVoice() {
    const voices = speechSynthesis.getVoices();
    const prefs = ["Daniel", "Google UK English Male", "Microsoft Ryan", "Arthur", "George", "Oliver"];
    for (const p of prefs) { const v = voices.find((x) => x.name.includes(p)); if (v) return v; }
    return voices.find((v) => v.lang === "en-GB") || null;
  }

  const speaker = {
    queue: [], buffer: "", active: false, browserSpeaking: false, onIdle: null, lastSpokeAt: 0, recent: [],
    playingFiller: false, // the item most recently taken off the queue was a thinking-time filler (see `filler`)
    item: null,           // the queue item being voiced right now (its callbacks settle it exactly once)
    // Bumped by stop(). Every async callback (audio fetch, play(), onended, speechSynthesis onend) remembers the
    // generation it started in and does nothing if it has changed - otherwise a cancelled sentence's late
    // callback either re-speaks it in the browser voice (a play() aborted by pause() rejects, which used to fall
    // through to speakBrowser) or calls next() a second time and overlaps the next reply.
    gen: 0,
    feed(delta) { this.buffer += delta; const parts = this.buffer.split(/(?<=[.!?…:])\s+(?=[A-Z0-9"'£(])/); this.buffer = parts.pop(); parts.forEach((p) => this.enqueue(p)); },
    flush() { if (this.buffer.trim()) this.enqueue(this.buffer); this.buffer = ""; },
    // `filler` marks the one-off "let me check the accounts" acknowledgment: it goes through exactly the same
    // queue, recent-speech list and echo window as any other speech, but next() treats it differently on the
    // way out (no HUD flip to "speaking", no follow-up window) because it is not a reply.
    enqueue(sentence, filler = false) {
      const clean = sentence.replace(/```[\s\S]*?```/g, " ").replace(/[#*_`>|]/g, " ").replace(/\s+/g, " ").trim();
      if (!clean || /^[-\s]+$/.test(clean)) return null;
      // Remember what we're about to say so the mic can recognise it as our own voice if it hears it back.
      const now = Date.now();
      this.recent = this.recent.filter((r) => now - r.at < RECENT_TTS_MS).slice(-30);
      this.recent.push({ text: clean, at: now });
      const item = { text: clean, filler, audio: S.voice.tts !== "browser" ? this.fetchAudio(clean) : null };
      this.queue.push(item);
      if (!this.active) this.next();
      return item;
    },
    async fetchAudio(text) {
      try {
        const r = await api("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, voice_id: S.voiceId || null }) });
        if (!r.ok) {
          let detail = `error ${r.status}`;
          try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
          voiceProblem(detail);
          return null;
        }
        return URL.createObjectURL(await r.blob());
      } catch { voiceProblem("couldn't reach the voice service"); return null; }
    },
      async next() {
        const item = this.queue.shift();
        if (!item) {
          const wasFiller = this.playingFiller; this.playingFiller = false;
          this.active = false; this.browserSpeaking = false; this.lastSpokeAt = Date.now(); // the echo tail applies to a filler too
          // A filler that finishes while the turn is still in flight is not a reply: the HUD stays on "thinking"
          // and it must not open the follow-up window or count as an exchange. If the turn has meanwhile ended
          // (the reply's own speech found the speaker busy and so skipped this), finish off as normal.
          if (wasFiller && filler.inFlight()) { if (this.onIdle) this.onIdle(); return; }
          if (S.hudState === "speaking" || (wasFiller && S.hudState === "thinking")) setHud("idle");
          if (S.voiceTurn === "replied") finishVoiceTurn();
          if (this.onIdle) this.onIdle();
          return;
        }
        this.playingFiller = !!item.filler;
        this.active = true; if (!item.filler) setHud("speaking");
        const gen = this.gen;
        const url = item.audio ? await item.audio : null;
        if (!this.active || gen !== this.gen) { if (url) URL.revokeObjectURL(url); return; }
        if (item.cancelled) {
          // A filler that hadn't started playing when the real reply (or the user) arrived: drop it silently.
          this.playingFiller = false;
          if (this.queue.length) { this.next(); return; }
          this.active = false;
          if (!filler.inFlight() && S.hudState === "thinking") { setHud("idle"); extendFollowUp(); }
          if (this.onIdle) this.onIdle();
          return;
        }
        item.started = true;
        this.item = item;
        if (!item.filler) noteFirstAudio(); // a thinking-time filler isn't the answer, so it doesn't count
        if (url) {
          // ONE way on per sentence. The clip ending, the clip erroring, a rejected play() and the browser-voice
          // fallback below can all report on the same sentence, and every one of them used to move the queue along:
          // a clip that could not be played did it twice (its `error` event AND the rejected play()), so the browser
          // voice read that sentence while the rest of the queue started over the top of it - two voices at once.
          const finish = () => { URL.revokeObjectURL(url); if (item.done || gen !== this.gen) return; item.done = true; this.next(); };
          player.onended = finish;
          player.onerror = () => {
            if (item.done || gen !== this.gen) return;
            if (item.playing) { finish(); return; }  // it was playing and broke part-way: move on to the next sentence
            item.errored = true;                      // it never started: the rejected play() below falls back to the browser voice
          };
          player.src = url;
          try { await player.play(); item.playing = true; if (item.errored) finish(); } catch {
            if (gen !== this.gen || item.done) return; // stopped while the audio was starting - not a blocked-autoplay problem
            player.onended = player.onerror = null; // the browser voice reads this sentence now; the dead clip must not advance the queue as well
            voiceProblem("the browser blocked the audio - click anywhere on the page and try again"); this.speakBrowser(item.text);
          }
        } else this.speakBrowser(item.text);
      },
    speakBrowser(text) {
      if (!("speechSynthesis" in window)) { this.next(); return; }
      const gen = this.gen, item = this.item;
      const u = new SpeechSynthesisUtterance(text);
      const v = pickBrowserVoice(); if (v) u.voice = v;
      u.lang = "en-GB"; u.rate = 1.02; u.pitch = 0.95;
      this.browserSpeaking = true;
      // Same rule as the clip: end and error can both arrive for one utterance, and only the first moves the queue on.
      u.onend = u.onerror = () => {
        if (gen !== this.gen || (item && item.done)) return;
        if (item) item.done = true;
        this.browserSpeaking = false; this.next();
      };
      player.pause(); // never over a clip that is still sounding
      speechSynthesis.speak(u);
    },
    stop() {
      // Only start the echo tail if something was actually being said - a push-to-talk press calls stop() on a
      // silent speaker, and that must not put the owner's own first words inside an "echo window".
      if (this.active || this.queue.length || this.browserSpeaking) this.lastSpokeAt = Date.now();
      this.gen++;
      this.queue = []; this.buffer = ""; this.active = false; this.browserSpeaking = false; this.playingFiller = false;
      filler.end(); // any stop (new message, push-to-talk, Stop button) also ends the thinking-time filler for that turn
      player.pause(); if ("speechSynthesis" in window) speechSynthesis.cancel(); setHud("idle"); },
  };
  const shouldSpeak = (mode) => S.speakPref === "always" || (S.speakPref === "voice" && mode === "voice");

  // What this tab says for a turn only when it is the tab that asked (see S.mine).
  const speaksThisTurn = (mode) => S.mine && shouldSpeak(mode);
  // Whatever reply text is still waiting for its full stop is its own sentence: it must not be glued onto this one
  // ("The letter is ready." + "Send it now or hold it?" was read as a single sentence).
  function say(text) { if (S.speakPref !== "off") { speaker.flush(); speaker.feed(text + " "); speaker.flush(); } }

  // Question prompt (ask_user) lives in ask.js; it only needs these four hooks. Its answers go back through send()
  // as ordinary chat text - never through decide()/the approvals path.
  window.JarvisAsk?.init({ send: (t, m, o) => send(t, m, o), say, speakNow: () => shouldSpeak(S.lastMode) && S.mine, mode: () => S.lastMode });

  // ------------------------------------------------------------------ self-echo guard
  // Without headphones the mic hears Jarvis's own voice. Nothing heard while he's talking, or in the short tail
  // after, may ever be treated as the owner asking something - so every recognised transcript goes through
  // looksLikeSelfEcho() before it is kept or submitted (in stt, sentry and utterance()).
  const ECHO_TAIL_MS = 2500;      // how long past the end of speech the mic might still be hearing its tail
  const RECENT_TTS_MS = 60000;    // how long we remember what we said
  const RECENT_MATCH_MS = 20000;  // outside the echo window, only compare against very recent speech
  const PTT_SILENCE_MS = 1200;    // push-to-talk: default for how long after the last final result counts as end of speech
  const normWords = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9\s]+/g, " ").split(/\s+/).filter(Boolean);

  // ------------------------------------------------------------------ end-of-turn tolerance
  // The silence that ends a push-to-talk turn is the `voice_silence_ms` setting (S.voice.silence_ms), clamped to a
  // sane range, and a little longer when the owner has clearly not finished: the last words are a filler/connective
  // ("and", "so", "um"...) or the text ends on a comma / ellipsis. Only affects how long stt.finalHeard() waits.
  const SILENCE_MIN_MS = 600, SILENCE_MAX_MS = 5000;
  const TRAILING_EXTRA_MS = 1200;
  const TRAILING_FILLERS = new Set(["and", "so", "um", "uh", "er", "erm", "but", "or", "then", "because", "like", "also", "plus", "well"]);
  function endOfTurnMs(text) {
    const configured = Number(S.voice.silence_ms);
    const base = Math.min(SILENCE_MAX_MS, Math.max(SILENCE_MIN_MS, Number.isFinite(configured) && configured > 0 ? configured : PTT_SILENCE_MS));
    const words = normWords(text);
    const unfinished = TRAILING_FILLERS.has(words[words.length - 1]) || /(,|…|\.\.\.)\s*$/.test(String(text || "").trim());
    return base + (unfinished ? TRAILING_EXTRA_MS : 0);
  }

  // ------------------------------------------------------------------ fuzzy echo match
  // Speech-to-text often garbles Jarvis's own voice coming back in, so an exact-substring match misses it. Anything
  // that is a close fuzzy match (word-overlap Dice similarity over the best-aligned stretch) for what Jarvis said in
  // the last ECHO_SIMILAR_MS is dropped. Needs ECHO_MIN_WORDS words so a bare "yes"/"go on" is never treated as echo.
  const ECHO_SIMILAR_MS = 10000;
  const ECHO_SIMILARITY = 0.8;
  const ECHO_MIN_WORDS = 3;
  const wordCounts = (words) => { const m = new Map(); words.forEach((w) => m.set(w, (m.get(w) || 0) + 1)); return m; };
  function windowSimilarity(heard, said) {
    if (!heard.length || !said.length) return 0;
    const n = Math.min(heard.length, said.length);
    const hc = wordCounts(heard);
    let best = 0;
    for (let i = 0; i + n <= said.length; i++) {
      const wc = wordCounts(said.slice(i, i + n));
      let overlap = 0;
      hc.forEach((c, w) => { overlap += Math.min(c, wc.get(w) || 0); });
      best = Math.max(best, (2 * overlap) / (heard.length + n));
    }
    return best;
  }
  function similarToRecentReply(words) {
    if (words.length < ECHO_MIN_WORDS) return false;
    const now = Date.now();
    const said = speaker.recent.filter((r) => now - r.at < ECHO_SIMILAR_MS).flatMap((r) => normWords(r.text));
    return windowSimilarity(words, said) >= ECHO_SIMILARITY;
  }
  const echoWindowOpen = () => speaker.active || speaker.browserSpeaking || Date.now() - speaker.lastSpokeAt < ECHO_TAIL_MS;

  // ------------------------------------------------------------------ conversation quality reporting
  // Fire-and-forget: the server keeps per-turn metrics (services/conversation_quality.py), but only the browser
  // knows when sound actually started and when it threw away something that was Jarvis's own voice. A failure
  // here must never affect speech or the conversation, so every call swallows its errors.
  const turnClock = { sentAt: 0, turnId: null }; // sentAt: performance.now() when a spoken request was sent
  function voiceEvent(body) {
    try { api("/api/voice-events", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch(() => {}); } catch { /* never matters */ }
  }
  function noteFirstAudio() {
    if (!turnClock.sentAt) return;
    const ms = Math.round(performance.now() - turnClock.sentAt);
    turnClock.sentAt = 0;
    voiceEvent({ kind: "first_audio", ms, turn_id: turnClock.turnId });
  }

  // ------------------------------------------------------------------ barge-in
  // Talking over Jarvis. Inside the echo window the allowlist is still exactly "the wake word or a stop phrase" -
  // barge-in only decides what happens when one of those is heard: speech is cut at once, and (for the wake
  // word) what the owner says next is captured. Guards against Jarvis's own voice triggering it:
  //  - the mic stream asks for echoCancellation/noiseSuppression/autoGainControl (MIC_CONSTRAINTS), and barge-in
  //    switches itself off if the browser reports it couldn't honour echoCancellation (stt.echoCancelled);
  //  - a wake-word match is ignored if the words around it appear in what Jarvis has been saying (matchesOwnSpeech);
  //  - the wake word must be in the first few words, be at least BARGE_IN_MIN_WAKE_CHARS long, and (as always)
  //    looksLikeSelfEcho() has already rejected repeated wake phrases and close copies of Jarvis's speech;
  //  - a stop phrase only counts as a short utterance (no long sentence that merely contains "stop").
  // Stop phrases ("Jarvis, stop", "quiet"...) always work, even with barge-in switched off in Settings.
  const MIC_CONSTRAINTS = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  const BARGE_IN_MIN_WAKE_CHARS = 4;   // a 1-3 letter wake word is too easily "heard" in noise to interrupt on
  const BARGE_IN_LEAD_WORDS = 4;       // the wake word has to open the utterance, as in the sentry listener
  const BARGE_IN_CAPTURE_MS = 8000;    // after a bare "Jarvis" barge-in, how long the next words count as addressed to him
  const STOP_MAX_WORDS = 4;            // same limit looksLikeSelfEcho() uses for "a short, explicit stop"
  const STOP_WORDS = new Set(["stop", "quiet", "enough", "cancel", "shut", "up"]);

  function bargeInAllowed() {
    const wakeChars = normWords(S.voice.wake_word || "jarvis").join("").length;
    return S.bargeIn && stt.echoCancelled !== false && wakeChars >= BARGE_IN_MIN_WAKE_CHARS;
  }

  // What, if anything, an utterance heard inside the echo window is: its wake word / stop phrase flags, and what is
  // left once wake word, fillers and stop words are taken away (empty for a bare "Jarvis, stop").
  function classifyInterrupt(text) {
    const words = normWords(text);
    const wakeWords = normWords(S.voice.wake_word || "jarvis");
    const has = (list, phrase) => phrase.length > 0 && ` ${list.join(" ")} `.includes(` ${phrase.join(" ")} `);
    const filler = new Set([...wakeWords, "hey", "ok", "okay"]);
    const rest = words.filter((w) => !filler.has(w));
    return {
      hasWake: has(words, wakeWords),
      wakeInLead: has(words.slice(0, BARGE_IN_LEAD_WORDS), wakeWords),
      stop: rest.length > 0 && rest.length <= STOP_MAX_WORDS && STOP_PHRASE_TEST_RE.test(rest.join(" ")),
      remaining: rest.filter((w) => !STOP_WORDS.has(w)).join(" "),
    };
  }

  // True if the wake word plus the couple of words after it appear in what Jarvis has recently said - i.e. the mic
  // is most likely hearing him say it, not the owner.
  function matchesOwnSpeech(text) {
    const words = normWords(text);
    const wakeWords = normWords(S.voice.wake_word || "jarvis");
    const at = words.indexOf(wakeWords[0]);
    if (at === -1) return false;
    const heard = words.slice(at, at + wakeWords.length + 2).join(" ");
    const now = Date.now();
    const spoken = ` ${speaker.recent.filter((r) => now - r.at < RECENT_TTS_MS).map((r) => normWords(r.text).join(" ")).join(" ")} `;
    return spoken.includes(` ${heard} `);
  }

  // The visible cue for why speech just stopped.
  function bargeInCue(kind) {
    const text = kind === "stop" ? "Stopped - I heard a stop phrase." : `Stopped - I heard "${S.voice.wake_word || "jarvis"}".`;
    toast("Interrupted", text);
    caption("", text);
  }

  function looksLikeSelfEcho(text) {
    const wake = (S.voice.wake_word || "jarvis").toLowerCase();
    const words = normWords(text);
    if (!words.length) return false;
    // A real person says the wake phrase once. "hey jarvis ... hey jarvis ... hey jarvis" mashed together in a
    // single transcript is the mic hearing overlapping playback, never a genuine command.
    if (words.filter((w) => w === wake).length >= 2) return true;
    const filler = new Set([wake, "hey", "ok", "okay"]);
    const content = words.filter((w) => !filler.has(w));
    if (!content.length) return false;
    // A short, explicit "stop"/"quiet" is always allowed through - that's how the owner interrupts.
    if (content.length <= 4 && STOP_PHRASE_TEST_RE.test(content.join(" "))) return false;
    if (similarToRecentReply(content)) return true; // a close (fuzzy) copy of what Jarvis said in the last ~10s
    const inWindow = echoWindowOpen();
    const now = Date.now();
    const recent = speaker.recent.filter((r) => now - r.at < (inWindow ? RECENT_TTS_MS : RECENT_MATCH_MS));
    if (!recent.length) return false;
    const spoken = recent.map((r) => normWords(r.text).join(" ")).join(" ");
    // Fuzzy match against what we recently said: the exact words back again...
    if (content.length >= 4 && ` ${spoken} `.includes(` ${content.join(" ")} `)) return true;
    // ...or, while (or just after) speaking, mostly made of words we just said - a garbled echo.
    if (inWindow && content.length >= 3) {
      const vocab = new Set(spoken.split(" "));
      if (content.filter((w) => vocab.has(w)).length / content.length >= 0.7) return true;
    }
    return false;
  }

  // Jarvis never speaks unprompted: there is deliberately no on-load greeting. Speech (TTS) only ever
  // follows something the owner asked (typed or voice) or an action they just took on the page.

  // ------------------------------------------------------------------ thinking-time acknowledgment filler
  // The ONE exception to "never speaks unprompted", and it is turn-scoped: only while a turn the owner started
  // by *speaking* is in flight (send(..., spoken=true) -> filler.begin()) and the real reply has not started.
  // There is no other way to reach filler.fire(): no turn object, no speech. Guarantees:
  //  - once per turn: turn.fired is set before anything is queued, and the single timer is never re-armed;
  //  - no overlap: fire() re-checks the user, STT and the speaker at the moment it would speak, and the first
  //    delta/reply/user speech/stop cancels the timer and drops a filler that hasn't started playing - one that
  //    is already playing is left to finish and the reply queues behind it in the normal speaker queue;
  //  - echo: it goes through speaker.enqueue() like any speech, so it is in speaker.recent and keeps the echo
  //    window open while it plays and for ECHO_TAIL_MS after; it is never passed to send()/utterance(), never
  //    added to the conversation or transcript, and speaker.next() skips extendFollowUp() for it, so it cannot
  //    set S.followUpUntil or count as an exchange.
  const FILLER_DELAY_MS = 1800; // silence after the turn starts before one acknowledgment is spoken (1.5-2s)
  const FILLER_DEFAULT_PHRASES = ["One moment.", "Let me think about that.", "Just a moment.", "Give me a second."];
  // First matching pattern wins; matched against the tool name carried on the "tool" start event.
  const FILLER_TOOL_PHRASES = [
    [/^(finance_|unbilled_jobs$|raise_invoices$|draft_credit_control$|business_health$)/, ["Let me check the accounts.", "Checking the accounts."]],
    [/^(fsm_|job_detail$|staff_|office_productivity$|ppm_|log_job$|create_(customer|site)$|accept_quote$|remedial_quotes$|contract_renewals$)/, ["Let me look at the jobs.", "Looking at the jobs now."]],
    [/^email_/, ["Checking your email.", "Let me check your email."]],
    [/^stock_/, ["Let me check the stock.", "Checking the stock."]],
    [/^(web_search|web_fetch|search_rankings$|seo_audit$|competitor_audit$)/, ["Let me look that up.", "Looking that up."]],
  ];
  const filler = {
    turn: null, lastPhrase: "",
    enabled() { return S.voice.ack_fillers !== false && S.speakPref !== "off"; },
    // Called from send() for a turn the owner spoke. Typed turns and click-shortcuts pass spoken=false: no filler.
    begin(spoken) {
      this.end();
      if (!spoken || !this.enabled()) return;
      const turn = { tool: "", blocked: false, fired: false, item: null, timer: null };
      turn.timer = setTimeout(() => this.fire(turn), FILLER_DELAY_MS);
      this.turn = turn;
    },
    inFlight() { return this.turn !== null; },
    // "tool" event: remember what is running so the phrase fits it if the timer hasn't fired yet. Never speaks itself.
    tool(ev) {
      if (!this.turn) return;
      if (ev.state === "start") { this.turn.tool = String(ev.name || ""); this.turn.toolId = ev.id; }
      else if (this.turn.toolId === ev.id) this.turn.tool = "";
    },
    // The reply has started, or the user is talking: no filler for the rest of this turn. A filler that hasn't
    // started playing is dropped; one that is mid-utterance finishes and the reply queues behind it.
    block() {
      const turn = this.turn;
      if (!turn) return;
      turn.blocked = true;
      clearTimeout(turn.timer); turn.timer = null;
      const item = turn.item;
      if (item && !item.started) {
        item.cancelled = true;
        const i = speaker.queue.indexOf(item);
        if (i >= 0) speaker.queue.splice(i, 1);
      }
    },
    end() { this.block(); this.turn = null; },
    phrase(tool) {
      let pool = FILLER_DEFAULT_PHRASES;
      for (const [re, phrases] of FILLER_TOOL_PHRASES) if (re.test(tool)) { pool = phrases; break; }
      const options = pool.filter((p) => p !== this.lastPhrase);
      this.lastPhrase = options[Math.floor(Math.random() * options.length)];
      return this.lastPhrase;
    },
    fire(turn) {
      if (this.turn !== turn || turn.blocked || turn.fired) return;
      turn.timer = null;
      if (!this.enabled()) return;
      if (S.hudState !== "thinking") return;                    // only while genuinely waiting on the reply
      if (current && current.dataset.raw) return;               // reply text has already started arriving
      if (speaker.active || speaker.queue.length || speaker.buffer.trim() || speaker.browserSpeaking) return;
      if (stt.finals.trim() || stt.silenceTimer) return;        // the owner is mid-sentence / STT has unsubmitted speech
      turn.fired = true;
      turn.item = speaker.enqueue(this.phrase(turn.tool), true);
    },
    // STT/VAD heard the owner (not our own voice): stay quiet for this turn.
    userSpeech() { this.block(); },
  };

  // ------------------------------------------------------------------ conversation
  let current = null;
  function addMessage(role, text, extra = "") {
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    el.innerHTML = `<div class="meta">${role === "user" ? esc(S.status?.owner || "You") : "Jarvis"}${extra ? " · " + esc(extra) : ""}</div><div class="md"></div>`;
    el.querySelector(".md").innerHTML = role === "assistant" ? md(text) : esc(text).replace(/\n/g, "<br>");
    $("#conversation").appendChild(el);
    $("#conversation").scrollTop = 1e9;
    document.body.classList.add("has-chat"); // phones: switch from the orb-first welcome to the chat-first layout
    return el;
  }

  // Good / wrong buttons under a reply. The verdict goes to /api/feedback (one per turn), and "wrong" then offers a
  // one-line note. They only record a verdict - nothing is sent, changed or approved by pressing them. Saying "that
  // was wrong" does the same thing by voice (handled server-side).
  function feedbackHtml(turnId) {
    return `<div class="fb" data-turn="${esc(turnId)}"><button type="button" data-rating="good" title="That was a good reply">Good</button>` +
      `<button type="button" data-rating="wrong" title="That was wrong">Wrong</button></div>`;
  }
  async function sendFeedback(turn, rating, note = "") {
    const r = await api("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rating, note, turn_id: Number(turn) }) });
    return r.ok;
  }
  $("#conversation").addEventListener("click", async (e) => {
    const box = e.target.closest(".fb");
    if (!box) return;
    const turn = box.dataset.turn;
    const rating = e.target.closest("button[data-rating]")?.dataset.rating;
    try {
      if (rating) {
        if (!(await sendFeedback(turn, rating))) { toast("Couldn't save that", "Try again in a moment.", "warning"); return; }
        box.innerHTML = rating === "good" ? `<span class="fb-done">Marked good</span>`
          : `<span class="fb-done">Marked wrong</span> <input class="fb-note" type="text" maxlength="500" placeholder="What was wrong? (Enter to save)" aria-label="What was wrong?">`;
        if (rating === "wrong") box.querySelector(".fb-note").focus();
      }
    } catch { /* offline or signed out: api() already handled the sign-out redirect */ }
  });
  $("#conversation").addEventListener("keydown", async (e) => {
    const input = e.target.closest?.(".fb-note");
    if (!input || e.key !== "Enter") return;
    e.preventDefault();
    const box = input.closest(".fb");
    try {
      if (await sendFeedback(box.dataset.turn, "wrong", input.value.trim())) box.innerHTML = `<span class="fb-done">Noted - thanks</span>`;
    } catch { /* see above */ }
  });

  async function loadTranscript() {
    try {
      const rows = await (await api("/api/transcript")).json();
      rows.slice(-20).forEach((r) => addMessage(r.role, r.text, time(r.created_at)));
    } catch { /* ignore */ }
  }

// `opts` may be shorthand `true` for { spoken: true } (kept for existing voice call sites). `spoken` is true
// only when the text came from the owner's actual speech (utterance()) - not for typed text or for click
// shortcuts that merely reply in voice. Only spoken turns are eligible for the thinking filler. `compose`
// marks a typed reply the owner composed themselves, so it may be learned as a "usual reply" (never buttons
// or speech).
function send(text, mode = "typed", opts = {}) {
  if (opts === true) opts = { spoken: true };
  const spoken = !!opts.spoken;
    text = text.trim();
    // A spoken reply while a question prompt is open: map "the second one" to that option, anything else stays as
    // free speech ("Other"). The prompt's own click/typed answers pass opts.ask and are sent exactly as given.
    if (mode === "voice" && !opts.ask && window.JarvisAsk) text = window.JarvisAsk.spokenReply(text);
    if (!text && !S.attachments.length) return;
    S.lastMode = mode;
    S.voiceTurn = false; // only utterance() marks a turn as a spoken one, after this returns
    speaker.stop();
    // A barge-in (or any new message sent while Jarvis is still mid-reply) must cancel the turn server-side,
    // not just silence local audio - otherwise the old turn keeps running, its own delta/tool/reply events
    // still arrive and get rendered and spoken, and this and the new turn's events interleave. stopEverything()
    // (the Stop button/Esc) always did this; ordinary sends via utterance()/the composer didn't. A harmless
    // no-op when nothing is actually running - the listen() loop on the backend awaits it before touching the
    // chat message that follows, so ordering is guaranteed.
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "stop" }));
    abandonCurrent();
    const payload = { type: "chat", text: text || "Please look at the attached file(s).", mode, attachments: S.attachments };
    // Only text the owner typed into the chat box may be learned as a "usual reply" (never buttons or speech).
    if (opts.compose && mode === "typed") payload.compose = true;
    S.attachments = []; renderAttachments();
    S.sent = S.sent.filter((x) => Date.now() - x.at < 120000).slice(-4);
    S.sent.push({ key: turnKey(payload.text), at: Date.now() }); S.mine = true; // this turn is ours: we speak its answer
    filler.begin(spoken && mode === "voice"); // after speaker.stop() above, which ended any previous turn's filler
    turnClock.sentAt = spoken && mode === "voice" ? performance.now() : 0; turnClock.turnId = null;
    if (S.ws && S.ws.readyState === 1) { httpTurnUntil = 0; S.ws.send(JSON.stringify(payload)); return; }
    setHud("thinking");
    if (streamCtl) streamCtl.abort(); // an older reply still streaming over the fallback: drop it, or its end would cut this one short
    streamChat(payload).catch(() => { toast("Couldn't reach Jarvis", "Check the connection.", "warning"); setHud("idle"); });
  }

  // The WebSocket is down (or hasn't connected yet) - falls back to the same conversation over a plain
  // POST, but still streamed word-by-word: each server-sent-event line is exactly the shape handle() already
  // knows how to render, so the experience matches the WebSocket path instead of waiting on the full reply.
  // `streamCtl` lets Stop abort a reply that is still streaming over this fallback path (over the WebSocket the
  // server is told to stop instead - see stopEverything()).
  let streamCtl = null, httpTurnUntil = 0;
  // While a reply is coming over the plain-HTTP stream - and for a moment after it ends, because the socket's copy of the
  // turn's last events lands just behind the stream's - the stream is what carries the turn.
  const httpCarriesTurn = () => streamCtl !== null || Date.now() < httpTurnUntil;
  const TURN_EVENTS = new Set(["user_message", "thinking", "delta", "tool", "reply", "error", "stopped", "ask"]); // main.py CHAT_STREAM_EVENTS
  async function streamChat(payload) {
    const ctl = streamCtl = new AbortController();
    try {
      const r = await api("/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: ctl.signal });
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop();
        for (const chunk of chunks) {
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (line) handle(JSON.parse(line.slice(6)));
        }
      }
    } catch (e) {
      if (e && e.name === "AbortError") return; // the owner pressed Stop - not an error
      throw e;
    } finally {
      if (streamCtl === ctl) { streamCtl = null; httpTurnUntil = Date.now() + 3000; }
    }
  }

  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault(); send($("#input").value, "typed", { compose: true }); $("#input").value = ""; autosize();
    RS.dismissed = null; RS.text = null; rsRender();
  });
  const autosize = () => { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; };
  $("#input").addEventListener("input", () => { autosize(); rsSoon(); });
  // Phones: the welcome block (orb + quick buttons) gives way while the keyboard is up.
  $("#input").addEventListener("focus", () => document.body.classList.add("composing"));
  $("#input").addEventListener("blur", () => document.body.classList.remove("composing"));
  // iOS Safari doesn't shrink the layout viewport for the on-screen keyboard (dvh stays full height), which hid the
  // composer behind it. Track the visual viewport instead and size the app shell to what's actually visible.
  if (window.visualViewport) {
    const fitShell = () => {
      document.documentElement.style.setProperty("--app-h", window.visualViewport.height + "px");
      if (window.visualViewport.offsetTop) window.scrollTo(0, 0); // iOS pans the page up to reveal a focused field; the shell is already sized to fit
    };
    window.visualViewport.addEventListener("resize", fitShell);
    window.visualViewport.addEventListener("scroll", fitShell);
    fitShell();
  }
  $("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });

  // ------------------------------------------------------------------ learned reply suggestion
  // The server learns the owner's usual typed replies (services/reply_suggestions.py) and offers the likeliest one
  // as a small greyed hint above the box. Right Arrow with the caret at the very end copies it into the box;
  // that is ALL it does - it never sends (Enter does, as normal) and has nothing to do with approvals. Esc dismisses.
  const RS = { text: null, dismissed: null, seq: 0, timer: null };
  const rsVisible = () => !$("#reply-hint").hidden;
  function rsRender() {
    const v = $("#input").value;
    const ok = RS.text && RS.text !== RS.dismissed && RS.text.length > v.length && RS.text.toLowerCase().startsWith(v.toLowerCase());
    $("#reply-hint").hidden = !ok;
    if (ok) $("#reply-hint-text").textContent = RS.text;
  }
  async function rsFetch() {
    const prefix = $("#input").value;
    const seq = ++RS.seq;
    if (prefix.length > 80 || prefix.includes("\n")) { RS.text = null; rsRender(); return; }
    try {
      const d = await (await api("/api/reply-suggestion?prefix=" + encodeURIComponent(prefix))).json();
      if (seq !== RS.seq || $("#input").value !== prefix) return; // typed on meanwhile
      RS.text = d.text || null;
    } catch { RS.text = null; }
    rsRender();
  }
  function rsSoon() {
    clearTimeout(RS.timer);
    const v = $("#input").value;
    // Already showing something that still fits what's typed: no need to ask again.
    if (v && RS.text && RS.text.length > v.length && RS.text.toLowerCase().startsWith(v.toLowerCase())) { rsRender(); return; }
    RS.text = null; rsRender();
    RS.timer = setTimeout(rsFetch, 150);
  }
  $("#input").addEventListener("keydown", (e) => {
    if (e.isComposing || !rsVisible()) return;
    if (e.key === "ArrowRight" && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
      const t = $("#input"), end = t.value.length;
      if (t.selectionStart !== end || t.selectionEnd !== end) return; // caret isn't at the end: normal cursor movement
      e.preventDefault();
      t.value = t.value + RS.text.slice(end);
      t.setSelectionRange(t.value.length, t.value.length);
      RS.text = null; rsRender(); autosize();
    } else if (e.key === "Escape") {
      RS.dismissed = RS.text; rsRender(); // other Esc handlers (stop speaking, close panels) still run as before
    }
  });
  $("#reply-hint-forget").addEventListener("click", async () => {
    const text = RS.text; if (!text) return;
    RS.text = null; rsRender();
    try { await api("/api/reply-suggestions/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) }); toast("Forgotten", `I won't suggest "${text}" again until you've used it a few more times.`); } catch { /* ignore */ }
    $("#input").focus();
  });
  rsSoon();
  // Tapping one of these is standing in for asking it out loud - so it should get spoken back the same way,
  // not go silent just because the question arrived as a click rather than actual speech.
  // The shortcuts menu sits on the message box: Briefing, Wrap-up, Team review, Business health, Cash flow, ...
  const quickMenu = {
    isOpen: () => !$("#quick").hidden,
    open() { $("#quick").hidden = false; $("#btn-shortcuts").setAttribute("aria-expanded", "true"); $("#quick .chip-btn")?.focus(); },
    close(refocus = false) {
      if (!this.isOpen()) return;
      $("#quick").hidden = true; $("#btn-shortcuts").setAttribute("aria-expanded", "false");
      if (refocus) $("#btn-shortcuts").focus();
    },
  };
  $("#btn-shortcuts").addEventListener("click", () => (quickMenu.isOpen() ? quickMenu.close() : quickMenu.open()));
  document.addEventListener("click", (e) => { if (quickMenu.isOpen() && !e.target.closest("#quick, #btn-shortcuts")) quickMenu.close(); });
  $("#quick").addEventListener("keydown", (e) => {
    const items = $$("#quick .chip-btn"), i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
  });
  $("#quick").addEventListener("click", (e) => {
    const q = e.target.closest("[data-q]");
    if (q) { quickMenu.close(); send(q.dataset.q, S.speakPref === "off" ? "typed" : "voice"); }
  });
  $("#btn-new-convo").addEventListener("click", async () => { await api("/api/conversation/reset", { method: "POST" }); });

  // attachments
  $("#btn-attach").addEventListener("click", () => $("#file").click());
  $("#file").addEventListener("change", async (e) => {
    for (const f of e.target.files) {
      if (f.size > 20e6) { toast("File too large", f.name + " is over 20 MB", "warning"); continue; }
      const data = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.readAsDataURL(f); });
      S.attachments.push({ name: f.name, mime: f.type || "text/plain", data });
    }
    e.target.value = ""; renderAttachments();
  });
  function renderAttachments() {
    $("#attachments").innerHTML = S.attachments.map((a, i) => `<span class="chip">${esc(a.name)} <button data-i="${i}" aria-label="Remove">✕</button></span>`).join("");
  }
  $("#attachments").addEventListener("click", (e) => { const b = e.target.closest("button[data-i]"); if (b) { S.attachments.splice(+b.dataset.i, 1); renderAttachments(); } });

  // ------------------------------------------------------------------ live events
  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    S.ws = ws;
    ws.onopen = () => sendProactiveMute();
    // While a reply is coming in over the plain-HTTP fallback, that stream carries the whole turn. The live connection
    // coming back part-way through hears the rest of the same turn as well: handling it twice fed every remaining word
    // to the speaker twice.
    ws.onmessage = (e) => { const ev = JSON.parse(e.data); if (httpCarriesTurn() && TURN_EVENTS.has(ev.type)) return; handle(ev); };
    ws.onclose = (e) => { if (e.code === 4401) { location.href = "/login"; return; } setTimeout(connect, 2500); };
    setInterval(() => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" })); }, 25000);
  }

  // ------------------------------------------------------------------ Jarvis speaking up on his own
  function sendProactiveMute() {
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "proactive_mute", muted: S.proactiveMuted }));
  }
  function renderProactiveMute() {
    const b = $("#btn-proactive-mute");
    if (!b) return;
    b.setAttribute("aria-pressed", S.proactiveMuted ? "true" : "false");
    b.classList.toggle("is-on", !S.proactiveMuted); b.classList.toggle("is-off", S.proactiveMuted);
    // Always a text label (never an icon alone): "Speaks up" when Jarvis may post by himself, "Muted" when not.
    const label = document.createElement("span");
    label.className = "btn-label";
    label.textContent = S.proactiveMuted ? "Muted" : "Speaks up";
    b.replaceChildren(label);
    b.title = S.proactiveMuted ? "Jarvis won't post into this session by himself - click to allow it again"
                                : "Mute Jarvis posting into this session by himself";
    b.setAttribute("aria-label", b.title);
  }
  $("#btn-proactive-mute")?.addEventListener("click", () => {
    S.proactiveMuted = !S.proactiveMuted;
    try { sessionStorage.setItem("jarvis.pmute", S.proactiveMuted ? "1" : "0"); } catch { /* private mode */ }
    renderProactiveMute(); sendProactiveMute();
  });
  renderProactiveMute();
  // Read aloud only in a voice session, only when nothing else is happening, and never while the owner is typing.
  // A message that can't be spoken right now is shown, not queued - Jarvis never talks over anyone.
  const proactiveMaySpeak = () => S.lastMode === "voice" && shouldSpeak("voice") && S.hudState === "idle" && !speaker.active
    && !S.voiceTurn && !$("#input").value.trim() && !filler.inFlight();
  // Several tabs of the same browser each get every proactive message; the first to claim one reads it, the rest only show it.
  // Every tab writes its own mark, waits a moment for the others to write theirs, and the mark that is left wins.
  function claimSpeech(id) {
    if (!id) return Promise.resolve(true);
    let key, mine;
    try {
      key = "jarvis.spoke." + id; mine = `${Date.now()}:${Math.random().toString(36).slice(2)}`;
      if (localStorage.getItem(key)) return Promise.resolve(false); // another tab got there first and has settled it
      localStorage.setItem(key, mine);
    } catch { return Promise.resolve(true); } // no storage: speak (a doubled voice is better than a silent Jarvis)
    return new Promise((resolve) => setTimeout(() => {
      try {
        const won = localStorage.getItem(key) === mine;
        for (let i = localStorage.length - 1; i >= 0; i--) { // claims are only needed for seconds: tidy the old ones away
          const k = localStorage.key(i);
          if (k && k !== key && k.startsWith("jarvis.spoke.") && Date.now() - Number(String(localStorage.getItem(k)).split(":")[0]) > 60000) localStorage.removeItem(k);
        }
        resolve(won);
      } catch { resolve(true); }
    }, 250));
  }
  function proactive(d) {
    if (S.proactiveMuted) return; // the server doesn't send these to a muted session; this is only a safety net
    addMessage("assistant", d.text, "on my own").classList.add("proactive");
    caption(d.text.replace(/[#*_`|]/g, "").slice(0, 180) + (d.text.length > 180 ? "…" : ""));
    if (d.speak && proactiveMaySpeak()) claimSpeech(d.id).then((won) => { if (won && proactiveMaySpeak()) say(d.text.replace(/[#*_`|]/g, "").replace(/\s+/g, " ").trim().slice(0, 280)); });
  }

  let toolsSeen = [];
  let refreshTimer = null;
  const refreshSoon = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 1500); };

  // Working-line and reply extras (Phase 2: "how Jarvis talks"). The step line above a reply names what he is checking
  // right now - it is fed only by real "tool" events from the server, never invented here. Once the reply is done,
  // the line goes and a source-and-time line goes underneath, with a button for the matching pop-up when there is
  // detail behind the answer and up to two follow-up questions (all of it sent by the server on the "reply" event).
  const showStep = (msg, text) => { const st = msg?.querySelector(".step"); if (st) { st.textContent = text; st.hidden = false; } };
  function replyExtras(msg, d, steps) {
    if (!msg || d.replace) return;
    if (typeof d.elapsed_ms === "number") {
      const secs = (d.elapsed_ms / 1000).toFixed(1) + "s";
      const src = d.sources?.length ? "Source: " + d.sources.join(", ") : "No systems checked";
      msg.insertAdjacentHTML("beforeend", `<div class="src"${steps.length ? ` title="${esc(steps.join(" · "))}"` : ""}>${esc(src)} · ${esc(secs)}</div>`);
    }
    const chips = [];
    if (window.JarvisAsk?.canReopen?.()) chips.push(`<button type="button" class="reply-chip answer" data-reask>Answer</button>`);
    if (d.panel && POPS.includes(d.panel)) chips.push(`<button type="button" class="reply-chip panel" data-pop="${esc(d.panel)}">Open ${esc(d.panel_title || d.panel)}</button>`);
    (Array.isArray(d.follow_ups) ? d.follow_ups : []).slice(0, 2).forEach((f) => { if (typeof f === "string" && f.trim()) chips.push(`<button type="button" class="reply-chip" data-follow="${esc(f)}">${esc(f)}</button>`); });
    if (chips.length) msg.insertAdjacentHTML("beforeend", `<div class="extras">${chips.join("")}</div>`);
  }
  $("#conversation").addEventListener("click", (e) => {
    const follow = e.target.closest("[data-follow]");
    if (follow) { send(follow.dataset.follow, "typed"); return; } // an ordinary chat message, exactly as if typed
    if (e.target.closest("[data-reask]")) window.JarvisAsk?.reopen?.();
  });

  function handle(ev) {
    const d = ev.data;
    // The owner pressed Stop: whatever the stopped turn still emits on its way out (a partial reply, a "problem" from
    // being interrupted) must not appear. The next turn starts with its own user_message, which lifts this.
    if (S.stopped && ["delta", "tool", "reply", "error"].includes(ev.type)) return;
    switch (ev.type) {
      case "user_message":
        S.stopped = false; window.JarvisAsk?.forget?.(); $$("#conversation [data-reask]").forEach((b) => b.remove());
        window.JarvisAsk?.close(); // any new message (typed, spoken, from another tab) answers/supersedes an open question
        { const i = S.sent.findIndex((x) => x.key === turnKey(d.text)); S.mine = i >= 0; if (i >= 0) S.sent.splice(i, 1); } // ours, or another tab's?
        addMessage("user", d.text + (d.attachments?.length ? `\n📎 ${d.attachments.join(", ")}` : ""), d.mode === "voice" ? "spoken" : "");
        S.lastMode = d.mode;
        break;
      case "thinking":
        setHud("thinking"); toolsSeen = [];
        turnClock.turnId = d.turn_id ?? null; // which server-side turn the first-audio measurement belongs to
        current = addMessage("assistant", ""); current.querySelector(".md").classList.add("typing");
        current.querySelector(".md").insertAdjacentHTML("beforebegin", `<div class="step">Thinking…</div>`);
        current.dataset.raw = "";
        break;
      case "delta":
        if (!current) { current = addMessage("assistant", ""); current.dataset.raw = ""; }
        current.querySelector(".step")?.setAttribute("hidden", ""); // the words have started - the "checking" line steps aside
        current.dataset.raw += d.text;
        current.querySelector(".md").innerHTML = md(current.dataset.raw);
        $("#conversation").scrollTop = 1e9;
        filler.block(); // the real reply has started - no filler, and drop one that hasn't begun playing
        if (speaksThisTurn(d.mode)) speaker.feed(d.text);
        break;
      case "tool": {
        const label = d.label || ""; // a bookkeeping tool (offer_next_steps) has no label and names nothing
        const line = d.state === "error" ? label + " - problem" : label + "…";
        if (label && (d.state === "start" || d.state === "error")) {
          if (d.state === "start") toolsSeen.push(label);
          if (current) { $("#toolline").textContent = ""; showStep(current, line); } else $("#toolline").textContent = "› " + line;
        }
        filler.tool(d); // lets a still-pending filler name the running tool; never speaks by itself
        break;
      }
      case "reply":
        if (d.turn_id && d.turn_id === S.lastReplyTurn) break; // the same reply delivered twice (stream and socket): once is enough
        S.lastReplyTurn = d.turn_id;
        filler.end(); // reply is ready: cancel the pending timer / unstarted filler (a playing one finishes first)
        $("#toolline").textContent = "";
        if (current) {
          if (d.replace || !current.dataset.raw) current.dataset.raw = d.text;
          const body = current.querySelector(".md");
          body.classList.remove("typing"); body.innerHTML = md(current.dataset.raw);
          current.querySelector(".step")?.remove();
          replyExtras(current, d, [...new Set(toolsSeen)]);
          if (d.turn_id) current.insertAdjacentHTML("beforeend", feedbackHtml(d.turn_id));
        } else {
          const el = addMessage("assistant", d.text);
          replyExtras(el, d, []);
        }
        if (speaksThisTurn(d.mode)) { if (d.replace) speaker.feed(d.text); speaker.flush(); }
        if (S.voiceTurn === "pending") S.voiceTurn = "replied";
        if (!speaker.active) { setHud("idle"); extendFollowUp(); if (S.voiceTurn === "replied") finishVoiceTurn(); }
        caption(d.text.replace(/[#*_`|]/g, "").slice(0, 180) + (d.text.length > 180 ? "…" : ""));
        current = null;
        $("#conversation").scrollTop = 1e9; // the source line and buttons add height under the reply
        refreshSoon();
        rsSoon(); // Jarvis's new reply changes the situation the suggestion is matched to
        break;
      case "error":
        filler.end();
        if (current) current.remove();
        current = null;
        addMessage("assistant", d.message).classList.add("error");
        // The transcript panel is hidden by default in the minimal orb view, so the caption is the only
        // place a failed reply is otherwise visible - without this an error would fail completely silently.
        caption(d.message);
        S.voiceTurn = false; // a failed turn is not a genuine exchange
        setHud("idle"); $("#toolline").textContent = ""; extendFollowUp();
        break;
      case "notification":
        // Displayed only - a pushed notification (briefing, suggestion, alert) never speaks unprompted,
        // whatever its server-side `speak` flag says.
        toast(d.title, d.body, d.level);
        refreshSoon();
        break;
      case "proactive": proactive(d); break; // Jarvis-initiated message: appears in the chat, may be read aloud
      case "owner_update":
        toast("Update sent", `${d.subject} → ${d.channels.join(", ") || "display"}`);
        break;
      case "display": openDisplay(d.title, d.markdown, d.doc_id); break;
      case "ask": window.JarvisAsk?.show(d); break; // small question pop-up (ask.js) - separate from approvals
      case "approvals": S.approvals = d; renderApprovals(); break;
      case "suggestions": S.suggestions = d; renderSuggestions(); break;
      case "issue": refreshSoon(); break;
      case "tests": renderTests(d); break;
      case "map": renderMap(d); break;
      case "conversation_reset": filler.end(); window.JarvisAsk?.close(); $("#conversation").innerHTML = ""; document.body.classList.remove("has-chat"); caption("Fresh start. What can I do for you?"); break;
      case "stopped": if (!speaker.active) setHud("idle"); extendFollowUp(); break;
      case "reload":
        toast("Settings applied", "Reconnecting…");
        if (S.ws) { S.ws.onclose = null; S.ws.close(); }
        setTimeout(connect, 400);
        setTimeout(refresh, 700);
        break;
    }
  }

  // ------------------------------------------------------------------ display overlay
  function openDisplay(title, markdown, docId) {
    $("#display-title").textContent = title;
    // Download buttons only for stored, drafted documents (the id is a 32-char hex string from the server).
    const dl = $("#display-downloads");
    if (docId && /^[0-9a-f]{32}$/.test(docId)) {
      $("#display-pdf").href = `/api/documents/${docId}/pdf`;
      $("#display-docx").href = `/api/documents/${docId}/docx`;
      $("#display-xlsx").href = `/api/documents/${docId}/xlsx`;
      dl.hidden = false;
    } else {
      dl.hidden = true;
    }
    $("#display-body").innerHTML = md(markdown);
    $("#display").classList.add("open");
    $("#display-close").focus({ preventScroll: true }); // keyboard/screen-reader users land inside the dialog
  }
  $("#display-close").addEventListener("click", () => $("#display").classList.remove("open"));
  // Escape closes the topmost thing first: the shortcuts menu, then the display overlay, then the drawer.
  // preventDefault marks the key as used, so the Stop handler further down does not also stop a running reply.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (quickMenu.isOpen()) { e.preventDefault(); quickMenu.close(true); return; }
    if ($("#display").classList.contains("open")) { e.preventDefault(); $("#display").classList.remove("open"); return; }
    if (Drawer.isOpen()) { e.preventDefault(); Drawer.close(); }
  });

  // ------------------------------------------------------------------ panels
  async function refresh() {
    try {
      const st = await (await api("/api/status")).json();
      S.status = st; S.voice = st.voice || S.voice; if (!stt.on) showSttEngine(sttEngine.next()); sttStatus.render(); S.approvals = st.approvals || []; S.suggestions = st.suggestions || [];
      S.data = { inbox: st.inbox, issues: st.issues, tests: st.tests, notifications: st.notifications, staff: st.staff, overdue_jobs: st.overdue_jobs,
        finance: st.finance, presence: st.presence, customer_watch: st.customer_watch, deadlines: st.deadlines, accreditations: st.accreditations };
      $("#company").textContent = (st.company || "").toUpperCase();
      renderPills(st.connections); renderInbox(st.inbox); renderIssues(st.issues); renderTests(st.tests);
      renderNotifications(st.notifications); renderOps(st.staff, st.overdue_jobs); renderFinance(st.finance);
      renderPresence(st.presence); renderCustomers(st.customer_watch); renderDeadlines(st.deadlines, st.accreditations); renderApprovals(); renderSuggestions(); renderSettings(st);
      renderActivity(st.activity);
      renderRail(); if (Drawer.current === "demo") renderDemo(); if (Drawer.current === "fleet") renderFleetStatus();
    } catch (e) { console.warn(e); }
  }

  // ------------------------------------------------------------------ scheduled checks (activity log)
  // A scheduled check that found nothing posts nothing into the chat. Every run is in the activity log and shows here as
  // ONE collapsed line per check ("Pull request watch · 7 checks since 09:30, no change") that opens to list each run
  // with its time. A check that did find something has also posted its message in the chat as usual.
  const activityOpen = new Set();
  function activityLine(job) {
    const parts = [];
    if (job.failed) parts.push(`${job.failed} failed`);
    if (job.changed) parts.push(plural(job.changed, "change", "changes"));
    return `${job.name} · ${plural(job.checks, "check", "checks")} since ${job.since}, ${parts.length ? parts.join(", ") : "no change"}`;
  }
  let activityShown = "";
  function renderActivity(a) {
    const box = $("#activity"); if (!box) return;
    const jobs = (a && a.jobs) || [];
    // Nothing new since the last look: leave the lines alone (a re-render would drop the focus and scroll of a line the
    // owner has open and is reading).
    const stamp = JSON.stringify(jobs); if (stamp === activityShown) return; activityShown = stamp;
    box.hidden = !jobs.length;
    box.innerHTML = jobs.map((job) => {
      const open = activityOpen.has(job.key);
      const runs = job.runs.map((r) => `<span class="${r.outcome === "changed" ? "changed" : r.outcome === "failed" ? "failed" : ""}">${esc(r.time)} ${esc(r.detail || (r.outcome === "no_change" ? "No change." : r.outcome))}</span>`).join("");
      return `<details class="auto" data-job="${esc(job.key)}"${job.failed ? ' data-level="bad"' : job.changed ? ' data-level="warn"' : ""}${open ? " open" : ""}>` +
        `<summary><span>${esc(activityLine(job))}</span><u>${open ? "Hide" : "Show"}</u></summary><div class="auto-runs">${runs}</div></details>`;
    }).join("");
  }
  // `toggle` does not bubble, so listen in the capture phase on the container (it survives the re-renders above).
  $("#activity")?.addEventListener("toggle", (e) => {
    const d = e.target; if (!d.matches?.("details.auto")) return;
    d.open ? activityOpen.add(d.dataset.job) : activityOpen.delete(d.dataset.job);
    const u = d.querySelector("summary u"); if (u) u.textContent = d.open ? "Hide" : "Show";
  }, true);

  // The top-bar pill: how many sources are still samples. Opens the Demo data pop-up.
  const isDemo = (v) => String(v).includes("DEMO");
  function renderPills(conns = {}) {
    const demo = Object.entries(conns).filter(([, v]) => isDemo(v)).map(([k]) => k);
    $("#pills").innerHTML = demo.length
      ? `<button type="button" class="pill demo" data-pop="demo" title="${esc(demo.join(", "))}">Demo data: ${demo.length} source${demo.length > 1 ? "s" : ""}</button>`
      : `<button type="button" class="pill live" data-pop="demo">All systems live</button>`;
  }
  // Demo data pop-up: which sources are still samples, and how to connect each one (the status text already says).
  function renderDemo() {
    const conns = S.status?.connections;
    $("#demo-intro").hidden = !conns;
    if (!conns) { $("#demo-list").innerHTML = `<p class="empty">Loading…</p>`; return; }
    const rows = Object.entries(conns).filter(([, v]) => isDemo(v));
    $("#demo-list").innerHTML = rows.length ? rows.map(([k, v]) => {
      const how = (String(v).match(/DEMO[^-:)]*[-:]\s*(.+)$/) || [])[1] || "";
      return `<div class="demo-row"><b>${esc(k)}</b><span>${how ? esc(how[0].toUpperCase() + how.slice(1)) : "Still showing sample figures"}</span></div>`;
    }).join("") : `<p class="empty">Everything is connected - no sample data is being shown.</p>`;
    $("#demo-intro").hidden = !rows.length;
  }

  function renderInbox(inbox) {
    const list = inbox?.unread || [];
    $("#inbox-count").textContent = Array.isArray(list) ? `${list.length} unread${inbox.demo ? " · demo" : ""}` : "";
    if (!Array.isArray(list)) { $("#inbox").innerHTML = `<li class="empty">${esc(list?.error || "Unavailable")}</li>`; return; }
    $("#inbox").innerHTML = list.length ? list.map((m) => `<li class="${m.importance === "high" ? "hot" : ""}">${esc(m.subject)}<span class="sub">${esc(m.from_name || m.from_email)} · ${time(m.received)}</span></li>`).join("")
      : `<li class="empty">Inbox clear.</li>`;
  }

  function renderIssues(issues = []) {
    $("#issues-count").textContent = `${issues.length} open`;
    $("#issues").innerHTML = issues.length ? issues.slice(0, 8).map((i) => {
      const cls = ["critical", "high"].includes(i.severity) ? "bad" : i.status === "fix_ready" ? "ok" : "warn";
      const pr = i.fix_pr_url ? ` · <a href="${esc(i.fix_pr_url)}" target="_blank" rel="noopener">PR</a>` : "";
      // severity is already shown by the row's accent colour - naming it again in text was noise.
      return `<li class="${cls}">#${i.id} ${esc(i.title)}<span class="sub">${esc(i.reporter)} · ${esc(i.status.replace("_", " "))}${pr}</span></li>`;
    }).join("") : `<li class="empty">No open issues.</li>`;
  }

  function renderTests(tests = []) {
    S.data.tests = tests; renderRail();
    const failing = tests.filter((t) => !t.ok);
    $("#tests-count").textContent = tests.length ? `${tests.length - failing.length}/${tests.length} passing` : "";
    const rows = [...failing, ...tests.filter((t) => t.ok)].slice(0, 10);
    $("#tests").innerHTML = rows.length ? rows.map((t) => `<li class="split ${t.ok ? "ok" : "bad"}"><span>${esc(t.name)}</span><small>${esc(t.detail).slice(0, 140)}</small></li>`).join("")
      : `<li class="empty">No results yet.</li>`;
  }

  function renderNotifications(list = []) {
    $("#notifications").innerHTML = list.length ? list.slice(0, 6).map((n) => `<li class="split ${n.level === "critical" ? "bad" : n.level === "warning" ? "warn" : ""}"><span>${esc(n.title)}</span><small>${time(n.created_at)}</small></li>`).join("")
      : `<li class="empty">Nothing to report.</li>`;
  }

  function kpi(label, value, cls = "") { return `<div class="kpi ${cls}"><div class="v">${value}</div><div class="l">${esc(label)}</div></div>`; }

  function renderOps(staff, overdue) {
    if (!staff || staff.error) { $("#ops").innerHTML = `<li class="empty">${esc(staff?.error || "FSM unavailable")}</li>`; return; }
    $("#ops-date").textContent = staff.demo ? "demo" : "";
    const onJob = staff.engineers.filter((e) => e.status === "on job").length;
    $("#ops-kpis").innerHTML = kpi("Jobs today", `${staff.completed_today}/${staff.jobs_today}`) +
      kpi("On site now", onJob) + kpi("Late starts", staff.late_starts.length, staff.late_starts.length ? "warn" : "good") +
      kpi("Overdue jobs", Array.isArray(overdue) ? overdue.length : "-", overdue?.length ? "bad" : "good");
    $("#ops").innerHTML = staff.engineers.map((e) => `<li class="split ok"><span>${esc(e.name)}</span><small>${esc(e.current_job || e.status)}${e.next_job ? " · next " + esc(e.next_job) : ""}</small></li>`).join("");
  }

  function renderFinance(f) {
    if (!f || f.error) { $("#finance").innerHTML = `<div class="empty">${esc(f?.error || "Accounts unavailable")}</div>`; return; }
    $("#finance-source").textContent = f.demo ? "demo" : f.source;
    $("#finance").innerHTML = kpi("Cash at bank", money(f.cash_at_bank)) + kpi("Owed to us", money(f.debtors_total)) +
      kpi(`Overdue (${f.debtors_overdue_count})`, money(f.debtors_overdue), f.debtors_overdue > 0.3 * f.debtors_total ? "bad" : "warn") +
      kpi("Debtor days", f.debtor_days ?? "-", f.debtor_days > 45 ? "warn" : "good") +
      kpi("We owe", money(f.creditors_total)) + kpi(`VAT due ${dayMonth(f.vat_due)}`, money(f.vat_quarter_estimate));
  }

  function renderPresence(p) {
    if (!p) return;
    const names = { facebook: "Facebook", instagram: "Instagram", linkedin: "LinkedIn", tiktok: "TikTok", google_reviews: "Google reviews" };
    const rows = Object.entries(p.platforms || {}).map(([k, v]) => {
      const m = v.followers || v.reviews; const r = v.rating;
      const change = m ? (m.change_7d > 0 ? `+${m.change_7d}` : m.change_7d) : "";
      return `<li class="split"><span>${names[k] || k} ${m ? Math.round(m.current).toLocaleString("en-GB") : "-"}${r ? ` · ${r.current}★` : ""}</span><small>${change} this week${p.demo ? " · demo" : ""}</small></li>`;
    });
    $("#presence").innerHTML = rows.join("") || `<li class="empty">Connect socials in settings.</li>`;
  }

  function renderCustomers(list = []) {
    $("#customers-count").textContent = list.length ? `${list.length} to watch` : "all healthy";
    $("#customers").innerHTML = list.length ? list.map((c) => `<li class="${c.status === "at risk" ? "bad" : "warn"}">${esc(c.customer)} · <b>${c.score}</b>
      <span class="sub">${esc(c.reasons.slice(0, 2).join("; "))}${c.renewal_in_days !== null && c.renewal_in_days <= 90 ? ` · renewal ${c.renewal_in_days < 0 ? "passed" : "in " + c.renewal_in_days + " days"}` : ""}</span></li>`).join("")
      : `<li class="empty">No customers showing warning signs.</li>`;
  }

  // Dated reminders: accreditations and deadlines together, soonest first (the Coming up pop-up and its rail count).
  function deadlineItems(deadlines = [], accreditations = []) {
    return [...(accreditations || []).map((a) => ({ what: a.what, due: a.date, days_left: a.days_left })), ...(deadlines || [])]
      .sort((a, b) => a.due < b.due ? -1 : 1).slice(0, 8);
  }
  function renderDeadlines(deadlines = [], accreditations = []) {
    const items = deadlineItems(deadlines, accreditations);
    $("#deadlines").innerHTML = !items.length ? `<li class="empty">Nothing dated coming up.</li>` : items.map((d) => `<li class="split ${d.days_left < 0 ? "bad" : d.days_left <= 14 ? "warn" : ""}"><span>${esc(d.what)}</span><small>${dayMonth(d.due)} · ${d.days_left < 0 ? Math.abs(d.days_left) + " days overdue" : d.days_left + " days"}</small></li>`).join("");
  }

  function renderApprovals() {
    const list = S.approvals || [];
    $("#approvals-panel").hidden = !list.length;
    $("#approvals-empty").hidden = !!(list.length || S.suggestions?.length);
    $("#approvals-count").textContent = list.length ? String(list.length) : "";
    $("#approvals").innerHTML = list.map((a) => `<div class="approval">#${a.id} ${esc(a.summary)}
      ${a.payload?.diff ? `<details class="diff"><summary>Show code change</summary><pre>${esc(a.payload.diff)}</pre></details>` : ""}
      ${a.kind === "email_send" ? `<details class="diff"><summary>Show email</summary><pre>${esc("To: " + a.payload.to.join(", ") + "\n\n" + a.payload.body)}</pre></details>` : ""}
      ${a.kind === "sage_invoices" ? `<details class="diff"><summary>Show invoices</summary><pre>${esc(a.payload.jobs.map((j) => `${j.job}  ${j.customer}  £${j.net_value} + VAT  (${j.site})`).join("\n"))}</pre></details>` : ""}
      ${a.kind === "review_requests" ? `<details class="diff"><summary>Show recipients</summary><pre>${esc(a.payload.requests.map((r) => `${r.email}  ${r.site}`).join("\n"))}</pre></details>` : ""}
      ${a.kind === "fsm_write" ? `<details class="diff"><summary>Show change</summary><pre>${esc(a.payload.method + " " + a.payload.path + "\n" + JSON.stringify(a.payload.body, null, 2))}</pre></details>` : ""}
      <div class="row"><button class="btn go" data-act="approve" data-id="${a.id}">Approve</button><button class="btn stop" data-act="deny" data-id="${a.id}">Cancel</button></div></div>`).join("");
    updateOrbBadge();
  }
  function renderSuggestions() {
    const list = S.suggestions || [];
    $("#suggestions-panel").hidden = !list.length;
    $("#approvals-empty").hidden = !!(list.length || S.approvals?.length);
    $("#suggestions-count").textContent = list.length ? String(list.length) : "";
    // Every other panel caps what it shows at once (issues 8, notifications 6) - suggestions didn't,
    // so a busy day's list of full-width action cards could bury COMMS/ISSUES/TESTS below the fold.
    $("#suggestions").innerHTML = list.slice(0, 4).map((s) => `<div class="suggestion p${s.priority}">${esc(s.title)}
      ${s.detail ? `<span class="sub">${esc(s.detail)}</span>` : ""}
      <div class="row"><button class="btn go" data-sug="done" data-key="${esc(s.key)}">Do it</button><button class="btn" data-sug="dismissed" data-key="${esc(s.key)}">Not now</button></div></div>`).join("");
    updateOrbBadge();
  }
  $("#suggestions").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-sug]");
    if (!b) return;
    const r = await api(`/api/suggestions/${encodeURIComponent(b.dataset.key)}/${b.dataset.sug}`, { method: "POST" });
    if (b.dataset.sug === "done" && r.ok) send((await r.json()).prompt, S.speakPref === "always" ? "voice" : "typed");
  });

  $("#approvals").addEventListener("click", (e) => { const b = e.target.closest("[data-act]"); if (b) decide(b.dataset.id, b.dataset.act); });
  async function decide(id, act) {
    const r = await (await api(`/api/approvals/${id}/${act}`, { method: "POST" })).json();
    toast(act === "approve" ? "Approved" : "Cancelled", r.result);
    say(act === "approve" ? "Right, on it." : "Right, I've dropped that one.");
  }

  $("#btn-run-tests").addEventListener("click", async () => { toast("Running routine tests…"); const r = await (await api("/api/tests/run", { method: "POST" })).json(); const bad = r.filter((t) => !t.ok).length; toast("Routine tests finished", bad ? `${bad} failing` : "All passing", bad ? "warning" : "info"); refresh(); });

  // ------------------------------------------------------------------ map
  let map = null, layer = null, tiles = null;
  // Esri's canvas basemaps are free and keyless; pick the one that matches the theme that is showing.
  const tileUrl = () => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${window.JarvisTheme?.effective() === "light" ? "Light" : "Dark"}_Gray_Base/MapServer/tile/{z}/{y}/{x}`;
  window.addEventListener("jarvis-theme", () => { if (tiles) tiles.setUrl(tileUrl()); });
  // The Fleet pop-up: real vehicles when RAM Tracking is connected, otherwise a clear "not connected" state.
  // live: RAM answers. failing: its details are entered but it is not answering (why = the reason, in words). Otherwise
  // it is still on sample data (why = what is still missing). Only "live" ever shows vehicles.
  function fleetState() {
    const conn = S.status?.connections?.["Vehicle tracking"] || "";
    const failing = /^NOT CONNECTED/.test(conn) || !!S.tracking?.ram_error;
    const live = !!conn && !isDemo(conn) && !failing;
    const detail = (String(conn).match(/^NOT CONNECTED\s*-\s*(?:RAM Tracking is failing:\s*)?(.+)$/) || [])[1] || S.tracking?.ram_error || "";
    const missing = (String(conn).match(/DEMO[^-:)]*[-:]\s*(.+)$/) || [])[1] || "";
    return { live, failing, why: failing ? detail : missing };
  }
  function renderFleetStatus() {
    const el = $("#fleet-status"), data = S.tracking;
    const conn = S.status?.connections?.["Vehicle tracking"] || "";
    const fleet = fleetState(), connected = fleet.live;
    const vans = data?.engineers?.length || 0;
    el.className = "fleet-note";
    if (!connected) {
      // Not connected (still sample data, or entered but not answering): say so plainly, with the reason, and show no
      // map (sample positions are not vehicles).
      el.innerHTML = `<span>Vehicle tracking is not connected, so there are no live vehicle positions. ${fleet.failing ? esc(fleet.why) : fleet.why ? esc(fleet.why[0].toUpperCase() + fleet.why.slice(1)) : "Add the RAM Tracking details under Connections."}</span>`;
      el.insertAdjacentHTML("beforeend", `<button class="btn small" type="button" data-pop="connections">Open Connections</button>`);
    } else {
      el.textContent = !data ? "Loading…" : data.rate_limited ? data.note : data.working_hours === false ? (data.note || "Outside working hours - locations are not shown.")
        : vans ? `Live from ${conn}: ${plural(vans, "vehicle", "vehicles")} reporting.` : "Connected, but no vehicles are reporting right now.";
    }
    $("#map").hidden = !connected || !vans;
  }
  function renderMap(data) {
    if (!data) return;
    S.tracking = data; renderFleetStatus(); renderRail();
    if (!window.L) {  // map library blocked/offline: show a list instead
      $("#map").innerHTML = `<ul class="list" style="padding:8px">${(data.engineers || []).map((e) =>
        `<li>${esc(e.engineer)}<span class="sub">${esc(e.current_job || e.status || "")}${e.eta_next_job_mins ? " · ETA next " + e.eta_next_job_mins + " min" : ""}</span></li>`).join("") ||
        `<li class="empty">${esc(data.note || "No vehicles reporting.")}</li>`}</ul>`;
      $("#map").style.height = "auto";
      return;
    }
    if (!map) {
      map = L.map("map", { zoomControl: false, attributionControl: true }).setView([53.83, -1.78], 10);
      // CARTO's basemaps now require a signed-up API key and render an "API KEY REQUIRED" watermark
      // without one - Esri's dark canvas is free, keyless, and still matches the dark theme.
      tiles = L.tileLayer(tileUrl(), { attribution: "© Esri, HERE, Garmin, OpenStreetMap contributors", maxZoom: 16 }).addTo(map);
      layer = L.layerGroup().addTo(map);
    }
    layer.clearLayers();
    $("#map-note").textContent = data.working_hours === false ? "outside hours" : data.demo ? "demo" : `${data.engineers.length} vans`;
    if (map && !$("#map").hidden) map.invalidateSize();
    const pts = [];
    (data.sites || []).forEach((s) => { L.circleMarker([s.lat, s.lng], { radius: 5, color: "#ff6a3d", weight: 2, fillOpacity: 0.6 }).bindTooltip(esc(s.name)).addTo(layer); pts.push([s.lat, s.lng]); });
    (data.engineers || []).forEach((e) => {
      L.circleMarker([e.lat, e.lng], { radius: 7, color: e.status === "driving" ? "#ffb020" : "#26d9ff", weight: 2, fillOpacity: 0.85 })
        .bindTooltip(`${esc(e.engineer)}<br>${esc(e.current_job || e.status || "")}${e.eta_next_job_mins ? `<br>ETA next: ${e.eta_next_job_mins} min` : ""}`).addTo(layer);
      pts.push([e.lat, e.lng]);
    });
    if (pts.length) map.fitBounds(pts, { padding: [20, 20], maxZoom: 12 });
  }
  // Leaflet measures its box once; call this whenever the map's container may have changed size or been revealed.
  function fitMapSoon() { requestAnimationFrame(() => { if (map) { map.invalidateSize(); refreshMap(); } }); }
  window.addEventListener("resize", fitMapSoon);
  async function refreshMap() { try { renderMap(await (await api("/api/tracking")).json()); } catch { /* ignore */ } }

  // ------------------------------------------------------------------ speech input
  const mic = $("#btn-mic");
  // Smallest recording worth sending to speech-to-text: anything below is just container headers / a mic click.
  const MIN_AUDIO_BYTES = 1000;
  // First container the browser can actually record, preferring webm/opus (Chrome/Firefox/Edge) and falling back
  // to mp4 (Safari/iOS). "" means let the browser choose its own default.
  function pickRecorderMime() {
    if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") return "";
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/ogg;codecs=opus", "audio/ogg"];
    return candidates.find((t) => MediaRecorder.isTypeSupported(t)) || "";
  }
  // File extension for the upload - Whisper decides the audio format from the filename, so it must match the data.
  function audioExtension(type) {
    const t = String(type || "").toLowerCase();
    return t.includes("mp4") || t.includes("m4a") || t.includes("aac") ? "mp4"
      : t.includes("ogg") ? "ogg" : t.includes("wav") ? "wav" : t.includes("mpeg") || t.includes("mp3") ? "mp3" : "webm";
  }
  // Speech-to-text engine choice and fallback. The server publishes the order in voice.stt_chain (selected engine
  // first, then Deepgram, Whisper, and browser speech recognition last - jarvis/integrations/stt_chain.py). This
  // object remembers which engine last worked (per browser, preferred for STT_GOOD_TTL_MS so the selected engine
  // is retried now and then) and which just failed (skipped for STT_COOLDOWN_MS so every press of the mic doesn't
  // wait on a broken engine). Browser speech recognition is never "remembered": it's the last resort, not a goal.
  const STT_LABEL = { deepgram: "Deepgram", whisper: "OpenAI Whisper", browser: "Browser speech recognition" };
  const STT_TIMEOUT_MS = 10000;      // one transcription request never waits longer than this
  const STT_TOTAL_MS = 25000;        // and the whole retry + fallback sequence stops starting new attempts after this
  const STT_COOLDOWN_MS = 5 * 60000;
  const STT_GOOD_TTL_MS = 12 * 3600000;
  const sttEngine = {
    failed: {}, good: "",
    chain() {
      const selected = S.voice.stt;
      let order = Array.isArray(S.voice.stt_chain) && S.voice.stt_chain.length ? S.voice.stt_chain.slice() : [selected];
      const [forSelected, good, at] = String(store.get("stt_good", "")).split(":");
      if (forSelected === selected && good && good !== "browser" && order.includes(good) && Date.now() - Number(at) < STT_GOOD_TTL_MS) {
        order = [good, ...order.filter((e) => e !== good)];
      }
      const usable = order.filter((e) => e === "browser" || !(this.failed[e] > Date.now() - STT_COOLDOWN_MS));
      return usable.length ? usable : order;
    },
    next() { return this.chain()[0] || "browser"; },
    markFailed(engine) { if (engine !== "browser") this.failed[engine] = Date.now(); },
    markGood(engine) {
      delete this.failed[engine];
      const value = `${S.voice.stt}:${engine}`;
      if (engine === "browser" || this.good === value) return; // nothing new to remember (live streams call this per result)
      this.good = value;
      store.set("stt_good", `${value}:${Date.now()}`);
    },
  };
  // Says which engine voice input is using right now (and why it changed), so a silent fallback is never a mystery.
  function showSttEngine(engine, note = "", warn = false) {
    sttStatus.engine(engine, note, warn);
    const el = $("#stt-engine"); if (!el) return;
    el.hidden = false; el.classList.toggle("warn", warn);
    el.textContent = `Voice input: ${STT_LABEL[engine] || engine}${note ? ` - ${note}` : ""}`;
  }
  // The top-bar label for voice input. Shown ONLY while it is not on the engine chosen in Settings (an engine with no
  // key, or one that just failed), in words, with the reason - so a fallback to the browser's speech recognition is never
  // silent. Hidden while everything is as chosen. (The line under the core says the same but hides once a chat starts.)
  const sttStatus = {
    runtime: "",
    engine(engine, note, warn) {
      const off = engine !== (S.voice && S.voice.stt);
      const why = off && stt.lastError ? ` (${stt.lastError.replace(/\s+/g, " ").slice(0, 80)})` : "";
      this.runtime = warn || off ? `Voice input: ${STT_LABEL[engine] || engine}${note ? ` - ${note}` : off ? " - switched automatically" : ""}${why}` : "";
      this.render();
    },
    render() {
      const el = $("#stt-status"); if (!el) return;
      const problem = (S.voice && S.voice.stt_problem) || "";
      const text = problem ? `Voice input: browser fallback - ${problem}` : this.runtime;
      el.hidden = !text; el.textContent = text; el.title = text;
    },
  };
  const stt = {
    mode: null, lastError: "", on: false, stream: null, rec: null, ws: null, finals: "", recognition: null, chunks: [], silenceTimer: null,
    // null = not known (browser speech recognition manages its own echo cancellation); false = the browser told us
    // the mic stream is NOT echo-cancelled, which switches barge-in off (see bargeInAllowed()).
    echoCancelled: null,
    // Some recognisers (e.g. Chrome on Android) send each "final" as the whole utterance so far ("ladder",
    // "ladder inspection", "ladder inspection jobs"). When a final just extends the previous one, replace it
    // instead of stacking them, so only the final version of the utterance is submitted and stored.
    lastFinal: "",
    addFinal(text) {
      const words = (s) => s.toLowerCase().replace(/[^a-z0-9'\s]/g, "").split(/\s+/).filter(Boolean);
      const prev = words(this.lastFinal), next = words(text), tail = this.lastFinal + " ";
      if (prev.length && next.length >= prev.length && prev.every((w, i) => w === next[i]) && this.finals.endsWith(tail))
        this.finals = this.finals.slice(0, this.finals.length - tail.length);
      this.finals += text + " "; this.lastFinal = text;
    },
    // Hands whatever final text has built up to utterance(). In wake mode the mic stays open for the next
    // wake phrase; in every other mode this is the end of the turn, so the mic closes rather than sitting on
    // "Listening…" with nothing ever submitted.
    commit() {
      clearTimeout(this.silenceTimer); this.silenceTimer = null;
      const text = this.finals.trim(); this.finals = "";
      if (text) utterance(text);
      if (S.listenMode !== "wake" && this.on) this.stop(false);
    },
    // A genuine final result is always submitted: immediately in wake mode, otherwise once speech has gone quiet.
    finalHeard() {
      if (S.listenMode === "wake") { this.commit(); return; }
      clearTimeout(this.silenceTimer);
      this.silenceTimer = setTimeout(() => this.commit(), endOfTurnMs(this.finals));
    },
    // keepSpeaking: the mic is being opened in the background (follow-up window, reconnect) rather than by the owner
    // pressing the mic - so it must not cut Jarvis off or overwrite the thinking/speaking display. Without it,
    // paid-STT wake mode had no microphone open at all while Jarvis was talking (the free wake-word listener
    // is stopped once it fires), so "Jarvis, stop" mid-sentence could not be heard.
    async start({ keepSpeaking = false } = {}) {
      if (this.on) return;
      ensureAudio();
      if (!keepSpeaking) speaker.stop();
      this.on = true; this.finals = ""; mic.classList.add("on");
      if (!keepSpeaking) { setHud("listening"); caption("", "Listening…"); }
      // Not simply S.voice.stt: skips an engine that just failed and prefers the one that last worked.
      const mode = sttEngine.next(); this.mode = mode;
      showSttEngine(mode, mode !== S.voice.stt ? "switched automatically" : "");
      try {
        if (mode === "browser") return this.startBrowser();
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: MIC_CONSTRAINTS });
        this.echoCancelled = (this.stream.getAudioTracks()[0]?.getSettings?.() || {}).echoCancellation !== false;
        if (!this.echoCancelled && S.bargeIn) toast("Barge-in off for now", "This microphone can't cancel echo, so talking over Jarvis is disabled. \"Jarvis, stop\" still works.", "warning");
        const picked = pickRecorderMime();
        this.rec = picked ? new MediaRecorder(this.stream, { mimeType: picked }) : new MediaRecorder(this.stream);
        const mime = this.rec.mimeType || picked || "audio/webm";
        console.info("[stt] recording", { mode, requested: picked || "(browser default)", actual: mime });
        if (mode === "deepgram") {
          this.ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/stt`);
          this.ws.onmessage = (e) => this.onDeepgram(JSON.parse(e.data));
          this.ws.onerror = () => this.liveFailed("Couldn't connect to the Deepgram stream.");
          this.ws.onopen = () => { this.rec.ondataavailable = (e) => { if (e.data.size && this.ws.readyState === 1) this.ws.send(e.data); }; this.rec.start(250); };
          this.ws.onclose = () => { if (this.on && S.listenMode === "wake") setTimeout(() => { this.stop(false); this.start({ keepSpeaking: true }); }, 1000); };
        } else {
          this.chunks = [];
          const rec = this.rec, stream = this.stream;
          rec.ondataavailable = (e) => { if (e.data && e.data.size) this.chunks.push(e.data); };
          rec.onerror = (e) => { console.warn("[stt] MediaRecorder error", e); toast("Recording failed", (e.error && e.error.message) || "The microphone recording stopped unexpectedly.", "warning"); };
          // The mic is released here, not in stop(): the final chunk arrives just before "stop", and cutting the
          // tracks first can truncate it (or lose it entirely on Safari).
          rec.onstop = () => { stream.getTracks().forEach((t) => t.stop()); this.transcribeChunks(mime, mode); };
          rec.start();
        }
      } catch (e) {
        toast("Microphone unavailable", e.message || "Allow microphone access in the browser.", "warning");
        this.stop(false);
      }
    },
    startBrowser() {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) {
        toast("Voice input not supported", "Use Chrome or Edge, or connect Deepgram.", "warning");
        captionError(this.lastError ? `Voice input failed: ${this.lastError} This browser has no built-in speech recognition to fall back on - type your message instead.`
          : "Voice input isn't supported in this browser - type your message instead.");
        showSttEngine("browser", "not available in this browser", true);
        this.stop(false); return;
      }
      const r = new SR(); this.recognition = r;
      r.lang = S.voice.language || "en-GB"; r.continuous = true; r.interimResults = true;
      r.onresult = (e) => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const heard = e.results[i][0].transcript;
          if (e.results[i].isFinal) {
            if (looksLikeSelfEcho(heard)) continue; // our own voice coming back in - never a command
            filler.userSpeech();
            this.addFinal(heard); this.finalHeard();
          } else if (!echoWindowOpen()) { interim += heard; filler.userSpeech(); }
        }
        caption(this.finals, interim);
      };
      r.onend = () => { if (this.on && S.listenMode === "wake") r.start(); else if (this.on) this.stop(true); };
      r.start();
    },
    onDeepgram(m) {
      if (m.type === "transcript") {
        if (m.text) sttEngine.markGood("deepgram");
        // Any transcript that isn't our own voice coming back means the owner is talking: no filler this turn.
        if (m.text && !echoWindowOpen() && !looksLikeSelfEcho(m.text)) filler.userSpeech();
        if (m.is_final && m.text && !looksLikeSelfEcho(m.text)) { this.addFinal(m.text); if (S.listenMode !== "wake") this.finalHeard(); }
        caption(this.finals, m.is_final || echoWindowOpen() ? "" : m.text);
        if (m.speech_final && this.finals.trim()) this.commit();
      } else if (m.type === "utterance_end" && this.finals.trim()) this.commit();
      else if (m.type === "speech_started" && speaker.active && S.listenMode === "wake") { /* barge-in handled on words */ }
      else if (m.type === "speech_started" && !echoWindowOpen()) filler.userSpeech(); // VAD heard the owner
      else if (m.type === "error") { this.liveFailed(m.message || "The Deepgram stream reported an error."); }
    },
    // The live Deepgram stream failed: skip it for a while and carry on with the next engine in the chain.
    liveFailed(reason) {
      if (!this.on || this.mode !== "deepgram") return;
      console.warn("[stt] live Deepgram stream failed", reason);
      sttEngine.markFailed("deepgram");
      this.lastError = `Deepgram: ${reason}`;
      const next = sttEngine.next();
      toast("Speech service problem", `${reason} Switching to ${STT_LABEL[next] || next} - please say that again.`, "warning");
      this.stop(false);
      this.start();
    },
    // One bounded request to one engine. Never throws except when signed out, and never waits past STT_TIMEOUT_MS.
    async postStt(blob, filename, engine) {
      const label = STT_LABEL[engine] || engine;
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), STT_TIMEOUT_MS);
      try {
        const fd = new FormData(); fd.append("audio", blob, filename);
        const r = await api(`/api/stt?engine=${encodeURIComponent(engine)}`, { method: "POST", body: fd, signal: ctl.signal });
        console.info("[stt] transcription response", { engine, status: r.status, ok: r.ok });
        let data = {};
        try { data = await r.json(); } catch { /* non-JSON error body */ }
        if (r.ok) return { ok: true, text: String(data.text || "").trim() };
        return { ok: false, transient: typeof data.transient === "boolean" ? data.transient : r.status >= 500,
          error: `${data.detail || `Speech-to-text returned ${r.status}`} (HTTP ${r.status}).` };
      } catch (e) {
        if (e && e.message === "signed out") throw e;
        console.warn("[stt] transcription request failed", engine, e);
        if (e && e.name === "AbortError") return { ok: false, transient: true, error: `${label} did not answer within ${STT_TIMEOUT_MS / 1000} seconds.` };
        return { ok: false, transient: true, error: (e && e.message) || `Couldn't reach ${label}.` };
      } finally { clearTimeout(timer); }
    },
    async transcribeChunks(mime, mode) {
      const chunks = this.chunks; this.chunks = [];
      const type = String(mime || (chunks[0] && chunks[0].type) || "audio/webm").split(";")[0];
      const blob = new Blob(chunks, { type });
      console.info("[stt] recording finished", { bytes: blob.size, type: blob.type, rawMime: mime, chunks: chunks.length });
      // The error text stays in the caption (red) until the next caption, not just in a toast that fades.
      const fail = (title, body) => { toast(title, body, "warning"); captionError(`${title}: ${body}`); if (S.hudState === "listening") setHud("idle"); };
      if (blob.size < MIN_AUDIO_BYTES) { fail("Nothing recorded", "No audio was captured. Hold the mic a little longer and check the microphone isn't muted."); return; }
      const filename = `speech.${audioExtension(type)}`;
      // Try the engine that recorded this, retrying once on a transient failure (timeout, network, upstream 5xx) and
      // then moving to the next server engine in the chain. A bad key / quota / bad audio moves on straight away.
      const order = sttEngine.chain();
      const engines = order.slice(Math.max(0, order.indexOf(mode))).filter((e) => e !== "browser");
      const startedAt = Date.now();
      let lastError = "";
      try {
        attempts: for (let i = 0; i < engines.length; i++) {
          const engine = engines[i], label = STT_LABEL[engine] || engine;
          for (let attempt = 1; attempt <= 2; attempt++) {
            if (Date.now() - startedAt > STT_TOTAL_MS) break attempts;
            showSttEngine(engine, attempt === 2 ? "retrying" : i ? "switched automatically" : "");
            caption("", `Transcribing with ${label}${attempt === 2 ? " (retrying)" : ""}…`);
            const res = await this.postStt(blob, filename, engine);
            if (res.ok) {
              sttEngine.markGood(engine); this.lastError = "";
              if (!res.text) { fail("Didn't catch that", "Speech-to-text returned no words. Please try again."); return; }
              caption(res.text, "");
              utterance(res.text);
              return;
            }
            lastError = `${label}: ${res.error}`;
            console.warn("[stt] engine failed", { engine, attempt, transient: res.transient, error: res.error });
            if (!res.transient) break;
          }
          sttEngine.markFailed(engine);
        }
      } catch (e) {
        if (e && e.message === "signed out") return;
        lastError = (e && e.message) || "Couldn't reach the speech-to-text service.";
      }
      // Every server engine failed (or none was left in time): fall back to the browser's own speech recognition
      // when there is one - it can't transcribe this recording, so the owner has to say it again.
      this.lastError = lastError;
      const browserOk = order.includes("browser") && (window.SpeechRecognition || window.webkitSpeechRecognition);
      if (browserOk && !this.on) {
        toast("Switched to browser voice input", `${lastError} Please say that again.`, "warning");
        this.start();
        return;
      }
      showSttEngine(engines[engines.length - 1] || mode, "failed", true);
      fail("Transcription failed", `${lastError || "No speech-to-text engine is available."} Type your message instead.`);
    },
    stop(submit = true) {
      if (!this.on) return;
      clearTimeout(this.silenceTimer); this.silenceTimer = null;
      this.on = false; mic.classList.remove("on");
      if (this.recognition) { const r = this.recognition; this.recognition = null; r.onend = null; r.stop(); }
      // Push-to-talk recordings (Whisper etc.) release the mic in rec.onstop, after the final chunk has arrived.
      const recActive = this.rec && this.rec.state !== "inactive";
      const deferRelease = recActive && !!this.rec.onstop;
      if (recActive) this.rec.stop();
      if (this.ws) { const ws = this.ws; this.ws = null; ws.onclose = null; ws.onerror = null; try { ws.send(JSON.stringify({ type: "Finalize" })); } catch { /* closed */ } setTimeout(() => { try { ws.send(JSON.stringify({ type: "CloseStream" })); ws.close(); } catch { /* closed */ } }, 900); }
      if (this.stream && !deferRelease) this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null; this.rec = null;
      const mode = this.mode || S.voice.stt; // the engine actually recording, which may differ from Settings after a fallback
      if (submit && mode !== "whisper") setTimeout(() => { if (this.finals.trim()) utterance(this.finals); this.finals = ""; }, mode === "deepgram" ? 1100 : 300);
      if (S.hudState === "listening") setHud("idle");
    },
  };

  // A free, always-on wake-word-only listener (the browser's own speech recognition, no API cost) used
  // whenever the real speech-to-text is a paid one (Deepgram/Whisper) - so "always listening" doesn't mean
  // continuously streaming audio to a paid service. It only ever escalates to the real microphone (stt)
  // once it hears the wake word; after a period of silence, stt goes back to sleep and this takes over again.
  const sentry = {
    rec: null, on: false,
    start() {
      if (this.on || stt.on || S.listenMode !== "wake" || S.voice.stt === "browser") return;
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) return; // no free fallback available in this browser - always-listening just stays on the paid stream
      this.on = true; mic.classList.add("sentry");
      const r = new SR(); this.rec = r;
      r.lang = S.voice.language || "en-GB"; r.continuous = true; r.interimResults = false;
      r.onresult = (e) => {
        const wake = (S.voice.wake_word || "jarvis").toLowerCase();
        const wakeRe = new RegExp(`\\b${wake}\\b`);
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const result = e.results[i];
          if (!result.isFinal) continue;
          const alt = result[0];
          const lead = alt.transcript.trim().toLowerCase().split(/\s+/).slice(0, 4).join(" ");
          // Genuine address starts with the wake word (maybe after "hey"/"ok"/a stray word) - a mention
          // buried partway into an unrelated sentence is almost always background chatter this free,
          // general-purpose listener misheard, not someone actually talking to Jarvis.
          if (!wakeRe.test(lead)) continue;
          if (looksLikeSelfEcho(alt.transcript)) continue; // Jarvis's own voice (or a mashed-up echo of it)
          // No confidence check here any more - a short wake-word utterance often scores low even when heard
          // correctly, and this listener silently drops anything it rejects with no retry, so a strict
          // threshold mostly just made Jarvis miss real attempts ("hit and miss"). The leading-word check
          // above is the real defence against background chatter.
          this.heard(alt.transcript);
          return;
        }
      };
      r.onerror = (e) => { if (this.on && e.error !== "no-speech" && e.error !== "aborted") { this.on = false; setTimeout(() => this.start(), 2000); } };
      r.onend = () => { if (this.on) { this.on = false; this.start(); } }; // browsers stop continuous recognition now and then - just restart it
      try { r.start(); } catch { /* already running */ }
    },
    heard(text) {
      if (!this.on) return;
      this.stop();
      utterance(text); // same wake-word/command parsing as the paid listener uses
    },
    stop() {
      if (!this.on) return;
      this.on = false; mic.classList.remove("sentry");
      if (this.rec) { const r = this.rec; this.rec = null; r.onend = null; r.onerror = null; try { r.stop(); } catch { /* already stopped */ } }
    },
  };

  function enterWakeMode() {
    sentry.stop(); stt.stop(false);
    if (S.voice.stt === "browser") stt.start(); else sentry.start();
  }
  function exitWakeMode() {
    sentry.stop(); stt.stop(false);
    clearTimeout(sleepTimer);
  }

  // Keeps the real (possibly paid) microphone up for a short spell after anything is heard or said, so a
  // follow-up doesn't need the wake word repeated - then, once that spell passes with nothing further, hands
  // back off to the free wake-word listener instead of streaming forever.
  const WAKE_LISTEN_MS = 20000; // a real back-and-forth has pauses for thinking - don't make them say "Jarvis" again
  let sleepTimer = null;
  // This only keeps the microphone open - it deliberately does NOT open the "no wake word needed" window, since
  // it is also called for errors, stops and Jarvis's own speech finishing. Only grantFollowUp() does that.
  function extendFollowUp(ms = WAKE_LISTEN_MS, { keepSpeaking = false } = {}) {
    S.micUntil = Date.now() + ms;
    if (S.listenMode !== "wake" || S.voice.stt === "browser") return;
    if (!stt.on) stt.start({ keepSpeaking: keepSpeaking || speaker.active });
    clearTimeout(sleepTimer);
    sleepTimer = setTimeout(checkSleep, ms + 250);
  }
  function checkSleep() {
    if (S.listenMode !== "wake" || S.voice.stt === "browser") return;
    const remaining = Math.max(S.micUntil, S.followUpUntil) - Date.now();
    if (remaining > 0) { sleepTimer = setTimeout(checkSleep, remaining + 250); return; }
    // Still talking: keep the mic up so a stop phrase / barge-in can be heard (the next idle re-arms this anyway).
    if (speaker.active) { sleepTimer = setTimeout(checkSleep, 2000); return; }
    if (stt.on) stt.stop(false);
    sentry.start();
  }

  // A genuine exchange just completed: the owner's spoken request was accepted and Jarvis answered it (and has
  // finished saying it). Only now may the next heard utterance skip the wake word - and utterance() still
  // refuses that whenever the echo window is open, so Jarvis's own voice tail can never ride on this.
  function finishVoiceTurn() {
    S.voiceTurn = false;
    S.followUpUntil = Date.now() + WAKE_LISTEN_MS;
    extendFollowUp(); // make sure the mic stays open for as long as the follow-up window does
  }

  const DROP_TOAST_MS = 15000;   // at most one "say my name" cue per this long
  let lastDropToastAt = 0;
  function missingWakeCue(wake, inEchoWindow) {
    // Inside the echo window a drop is most likely Jarvis's own voice coming back in - a toast would just turn
    // his echo into visible noise, so those stay caption-only.
    if (inEchoWindow) return;
    const now = Date.now();
    if (now - lastDropToastAt < DROP_TOAST_MS) return;
    lastDropToastAt = now;
    toast("Didn't catch that with my name", `Say '${S.voice.wake_word || wake}' first`);
  }

  // Two separate regex objects, deliberately - a single /g-flagged RegExp used with both .test() and .replace()
  // shares mutable lastIndex state between those calls, which silently skips or duplicates matches. Test and
  // strip need their own instances even though the pattern is identical.
  const STOP_PHRASE_TEST_RE = /\b(stop|quiet|enough|cancel|shut up)\b/;

  function utterance(raw) {
    const text = String(raw || "").trim();
    if (!text) return;
    const lower = text.toLowerCase();
    const wake = (S.voice.wake_word || "jarvis").toLowerCase();
    // Last line of defence for every listener (browser, Deepgram, Whisper, sentry): anything that looks like
    // Jarvis's own voice - repeated wake phrases mashed together, or a close match for what he recently said -
    // is dropped outright, in every listen mode, and can never be treated as the owner asking something.
    if (looksLikeSelfEcho(text)) { voiceEvent({ kind: "echo_suppressed", detail: "self-echo" }); return; }
    // Sampled once, up front, before anything below (e.g. speaker.stop()) can change it: the follow-up exception
    // and the drop toast both depend on this, and neither may ever apply while the echo window is open.
    const inEchoWindow = echoWindowOpen();
    // Echo cancellation is never perfect without headphones, and room echo/output buffering trails on past
    // the moment playback actually stops - so the mic can pick up the tail end of Jarvis's own voice just
    // after speaker.active has already gone false (right when extendFollowUp() opens the real mic back up).
    // Keep checking for a short tail past the end of speech, not only while still actively speaking. In
    // push-to-talk the owner deliberately opened the mic themselves (which also silenced Jarvis), so only the
    // similarity check above applies there; in wake mode the strict allowlist below does as well.
    if (S.listenMode === "wake" && echoWindowOpen()) {
      const heard = classifyInterrupt(text);
      // While actively speaking (or just finished), only actually respond to a stop phrase or the wake word -
      // anything else heard in this window is presumed to be the mic picking up Jarvis's own voice, not a
      // real interruption. A word-overlap heuristic used to sit here instead, judging echo by how much heard
      // text matched Jarvis's recent speech - but speech-to-text often mangles a TTS voice badly enough that
      // genuine echo scores a *low* match and sails straight through as if it were a real command. Requiring
      // the wake word or a stop phrase has no such failure mode: it's a strict allowlist, not a similarity
      // score, so mistranscribed echo is rejected the same as clearly-echoed echo.
      // A bare "stop"/"quiet"/"Jarvis, stop" - nothing left worth answering once the stop words and wake word
      // are stripped out - should just go quiet. Falling through to send() below would forward the word
      // "stop" itself to Jarvis as a fresh question, which it answers and speaks aloud - so saying "stop"
      // during a reply just started a new one every time, rather than ever actually going quiet. It has to be
      // stopEverything(), not just speaker.stop(): the reply is usually still streaming in, and every further
      // delta would otherwise be fed straight back into the speaker, so Jarvis carried on after "stop".
      if (heard.stop && heard.remaining.length < 3) { stopEverything(); bargeInCue("stop"); extendFollowUp(); return; }
      // Everything else needs the wake word, and barge-in on: with it off (or unsafe - see bargeInAllowed()) only
      // stop phrases cut Jarvis off, and the wake word is ignored like any other speech in the window.
      if (!heard.hasWake || !bargeInAllowed()) { if (S.captureUntil > Date.now()) caption("", "(still settling - say that again)"); return; }
      // The wake word must open the utterance and must not be Jarvis saying it himself.
      if (!heard.wakeInLead || matchesOwnSpeech(text)) return;
      // Barge-in: cut the speech and the turn that's producing it, then fall through to handle what follows the
      // wake word as a fresh command. A bare wake word just arms a short capture (below) for the next utterance.
      stopEverything(); bargeInCue("wake");
      if (!text.slice(lower.indexOf(wake) + wake.length).replace(/^[\s,.!?]+/, "")) {
        S.captureUntil = Date.now() + BARGE_IN_CAPTURE_MS;
        setHud("awaiting"); extendFollowUp(undefined, { keepSpeaking: true });
        return;
      }
    }
    const bare = lower.replace(new RegExp(`^\\s*(hey\\s+)?${wake}[\\s,.!?]*`), "").trim();
    // In always-listening mode a spoken approve/deny must be addressed to Jarvis by name - a bare "cancel" or
    // "go ahead" overheard from the room (or from Jarvis's own voice) is never a decision.
    const addressed = S.listenMode !== "wake" || lower.includes(wake);
    if (addressed && S.approvals.length && /^(approve|approved|confirm|confirmed|go ahead|yes,? (do it|send it|deploy it)|send it|deploy it)\b/.test(bare)) {
      if (S.approvals.length === 1) decide(S.approvals[0].id, "approve");
      else say(`Sir, there are ${S.approvals.length} approvals waiting - tap the one you mean.`);
      return;
    }
    if (addressed && S.approvals.length === 1 && /^(deny|cancel|don't|do not|no,? (cancel|don't))\b/.test(bare)) { decide(S.approvals[0].id, "deny"); return; }
    if (S.listenMode === "wake") {
      // The wake word is required unless there has just been a genuine exchange (S.followUpUntil, set only by
      // finishVoiceTurn()) AND the echo window is closed. The earlier version of this exception was a source of
      // false triggers because it applied even to a stray/delayed echo of Jarvis's own voice; here it can never
      // apply while echoWindowOpen(), where the strict wake-word-or-stop-phrase allowlist above still rules.
      const idx = lower.indexOf(wake);
      if (idx === -1) {
        // The one narrow exception: the owner barged in with a bare "Jarvis" a moment ago (see the echo-window
        // block above) - that wake word already addressed him, so take the next thing said as the command.
        // One-shot and short-lived, and only reachable outside the echo window, so echo can never use it.
        if (S.captureUntil > Date.now()) { S.captureUntil = 0; send(text, "voice"); S.voiceTurn = "pending"; extendFollowUp(undefined, { keepSpeaking: true }); return; }
        S.captureUntil = 0;
        if (inEchoWindow || Date.now() >= S.followUpUntil) {
          caption("", `(heard: "${text.slice(0, 60)}")`);
          missingWakeCue(wake, inEchoWindow);
          return;
        }
        send(text, "voice"); S.voiceTurn = "pending";
        return;
      }
      const cmd = text.slice(idx + wake.length).replace(/^[\s,.!?]+/, "");
      if (!cmd) {
        setHud("awaiting"); extendFollowUp(); say(`Yes, ${address()}?`);
        // The wake word alone and Jarvis's prompt back is an exchange too - the next words are the request.
        if (S.speakPref === "off" || !speaker.active) finishVoiceTurn(); else S.voiceTurn = "replied";
        return;
      }
      send(cmd, "voice", true); S.voiceTurn = "pending";
      // Keep the real mic open while the reply is thought about and spoken, so "Jarvis, stop" / a barge-in can be
      // heard mid-sentence (the free wake-word listener that just fired has stopped itself).
      extendFollowUp(undefined, { keepSpeaking: true });
    } else send(text, "voice", true);
  }

  mic.addEventListener("click", () => {
    // A tap must always have a real "off" to reach. Before this, tapping while the free wake-word listener
    // (sentry) was active jumped straight to starting the real microphone instead of stopping - so in
    // always-listening mode the mic looked permanently lit, since there was never a path back to fully off.
    if (micPressBargeIn()) return;
    if (stt.on) { stt.stop(true); return; }
    if (sentry.on) { sentry.stop(); return; }
    stt.start();
  });

  // ------------------------------------------------------------------ stop
  // Leave the reply that is still being written as it stands (or drop it if nothing has arrived yet), and ignore
  // anything that turn still sends while it winds down - see handle(). Used by Stop and by sending a new message.
  function abandonCurrent() {
    S.stopped = true;
    if (!current) return;
    const msg = current;
    current = null;
    const body = msg.querySelector(".md");
    body.classList.remove("typing");
    msg.querySelector(".step")?.remove();
    if (!msg.dataset.raw) { msg.remove(); return; }
    body.innerHTML = md(msg.dataset.raw);
    msg.insertAdjacentHTML("beforeend", `<div class="src">Stopped.</div>`);
  }
  function stopEverything() {
    filler.end();
    speaker.stop(); // instant - halts audio/browser speech straight away
    S.captureUntil = 0;
    abandonCurrent();
    S.voiceTurn = false; // an interrupted turn is not a genuine exchange
    setHud("idle");
    $("#toolline").textContent = "";
    // Tell the backend too, so it actually stops generating and the next message doesn't queue behind it.
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "stop" }));
    else api("/api/interrupt", { method: "POST" }).catch(() => {});
    if (streamCtl) streamCtl.abort(); // the plain-HTTP fallback stream: drop the in-flight request too
  }
  // Barge-in by hand: pressing the mic (or holding Space) while Jarvis is speaking cuts him off - audio, queued
  // sentences and the turn still streaming the rest of the reply - and opens the mic for the owner instead of
  // toggling it off. Returns true if it handled the press. Deliberately separate from the mic click handler and
  // stt.start()/stop() so it stays independent of the mic start/stop logic.
  function micPressBargeIn() {
    if (!(speaker.active || speaker.queue.length || speaker.browserSpeaking)) return false;
    stopEverything();
    sentry.stop();
    if (!stt.on) stt.start();
    else { setHud("listening"); caption("", "Listening…"); }
    return true;
  }
  $("#btn-stop").addEventListener("click", stopEverything);
  // Escape stops a running reply - unless an overlay (shortcuts menu, display, drawer) is open, where Escape closes that instead.
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && !e.defaultPrevented && !$("#btn-stop").hidden) stopEverything(); });
  let spaceHeld = false;
  window.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || ["TEXTAREA", "INPUT", "SELECT", "SUMMARY"].includes(document.activeElement?.tagName) || S.listenMode === "wake") return;
    e.preventDefault(); spaceHeld = true;  // (SUMMARY: Space opens and closes a scheduled check's line, as it does any disclosure)
    if (!micPressBargeIn()) stt.start();
  });
  window.addEventListener("keyup", (e) => { if (e.code === "Space" && spaceHeld) { spaceHeld = false; stt.stop(true); } });

  // ------------------------------------------------------------------ Settings pop-up: voice and display options
  // (The drawer itself, and opening Settings / Connections, is Drawer above; the buttons carry data-pop.)
  $("#set-listen").value = S.listenMode; $("#set-speak").value = S.speakPref;
  $("#set-listen").addEventListener("change", (e) => {
    S.listenMode = e.target.value; store.set("listen", S.listenMode);
    if (S.listenMode === "wake") { enterWakeMode(); toast("Always listening", `Say "${S.voice.wake_word}…" to talk to me.`); } else exitWakeMode();
  });
  // One place that changes the "speak replies" preference: the top-bar Voice button and the Settings select both use it.
  function setSpeakPref(v) {
    S.speakPref = v; store.set("speak", v);
    if (v !== "off") store.set("speak_on", v);
    $("#set-speak").value = v;
    if (v === "off") speaker.stop();
    renderVoiceButton();
  }
  function renderVoiceButton() {
    const on = S.speakPref !== "off", b = $("#btn-voice");
    b.setAttribute("aria-pressed", String(on));
    b.classList.toggle("is-on", on); b.classList.toggle("is-off", !on);
    b.textContent = on ? "Voice on" : "Voice off";
    b.title = on ? "Jarvis speaks his replies - click to keep him silent" : "Jarvis is silent - click to let him speak again";
  }
  $("#set-speak").addEventListener("change", (e) => setSpeakPref(e.target.value));
  $("#btn-voice").addEventListener("click", () => setSpeakPref(S.speakPref === "off" ? (store.get("speak_on", "voice") === "off" ? "voice" : store.get("speak_on", "voice")) : "off"));
  renderVoiceButton();
  $("#set-bargein").value = S.bargeIn ? "1" : "0";
  $("#set-bargein").addEventListener("change", (e) => { S.bargeIn = e.target.value !== "0"; store.set("bargein", S.bargeIn ? "1" : "0"); if (!S.bargeIn) S.captureUntil = 0; });
  $("#set-voice").addEventListener("change", (e) => { S.voiceId = e.target.value; store.set("voice", S.voiceId); });
  $("#btn-test-voice").addEventListener("click", () => { ensureAudio(); say(`Good to go, ${address()}. This is how I sound.`); });
  // Theme: Auto follows the device, or force Light / Dark. Kept by theme.js in localStorage (wrapped in try/catch).
  $("#set-theme").value = window.JarvisTheme?.get() || "auto";
  $("#set-theme").addEventListener("change", (e) => window.JarvisTheme?.set(e.target.value));
  // The staff report link carries a key, so it is never shown on screen: this copies it to the clipboard instead.
  $("#btn-copy-report").addEventListener("click", async () => {
    try {
      const link = await Settings.staffReportLink();
      if (!link) { toast("No staff report link yet", "Set a staff report key under Connections, then try again.", "warning"); return; }
      await navigator.clipboard.writeText(link);
      toast("Link copied", "The staff report link is on your clipboard.");
    } catch { toast("Couldn't copy the link", "Your browser blocked clipboard access.", "warning"); }
  });
  let voicesLoaded = false;
  async function renderSettings(st) {
    $("#btn-sage").hidden = !(st.sage?.configured && !st.sage?.connected);
    if (!voicesLoaded && S.voice.tts === "elevenlabs") {
      voicesLoaded = true;
      try {
        const voices = await (await api("/api/voices")).json();
        $("#set-voice").innerHTML = `<option value="">Default (${esc(S.voice.voice)})</option>` + voices.map((v) => `<option value="${esc(v.voice_id)}">${esc(v.name)}${v.accent ? " - " + esc(v.accent) : ""}</option>`).join("");
        $("#set-voice").value = S.voiceId;
      } catch { /* ignore */ }
    }
  }

  // ------------------------------------------------------------------ settings drawer: connections tab
  // The browser and password managers must not fill these boxes by themselves. An "API username" box beside a password box
  // looks like a sign-in form to them, and an autofilled value is saved as if it had been typed (with the wrong name or
  // password in it, a connection that was entered correctly stops working and nothing says why).
  const NO_AUTOFILL = 'autocapitalize="off" spellcheck="false" data-1p-ignore data-lpignore="true" data-form-type="other"';
  const Settings = {
    loaded: false, sections: [], edited: {}, cleared: new Set(), advanced: new Set(), testing: new Set(),
    view: null,            // null = the list of integrations; otherwise the id of the one whose setup form is showing
    // The staff report link carries a key, so it is never in the settings data or on screen: "Copy staff report link"
    // asks the server for it when pressed, copies it, and lets go of it.
    async staffReportLink() { return (await (await api("/api/staff-report-address")).json()).link || ""; },

    async load() {
      try {
        const data = await (await api("/api/settings")).json();
        this.loaded = true;
        this.sections = data.sections;
        this.render(data.problem);
      } catch {
        toast("Couldn't load settings", "Check the connection and try again.", "warning");
        if (!this.sections.length) $("#settings-sections").innerHTML = `<p class="empty">Couldn't load the connections. Close this and open it again to retry.</p>`;
      }
    },

    dirty() { return Object.keys(this.edited).length > 0 || this.cleared.size > 0; },

    revert() { this.edited = {}; this.cleared.clear(); this.render(); },

    field(key) { for (const s of this.sections) { const f = s.fields.find((x) => x.key === key); if (f) return f; } return null; },

    currentValue(key) {
      if (Object.prototype.hasOwnProperty.call(this.edited, key)) return this.edited[key];
      if (this.cleared.has(key)) return "";
      return this.field(key)?.value;
    },

    // A field with depends_on only makes sense to show once you can see the field it depends on (same
    // section, no ordering surprises) and only while that field's own current value matches - lets a
    // section with several providers (Voice: ElevenLabs/Azure/Piper) show just the one actually selected.
    visible(f) { return !f.depends_on || this.currentValue(f.depends_on[0]) === f.depends_on[1]; },

    render(problem = "") {
      $("#settings-problem").innerHTML = problem
        ? `<div class="set-problem">${esc(problem)}</div>` : "";
      $("#settings-sections").innerHTML = this.renderConnections();
      this.updateSaveBar();
      this.syncTalk();
    },

    // "How Jarvis talks" lives in Settings but is stored like every other setting here (talk_style, saved with the
    // same Save changes bar and the same /api/settings call), so it also appears under Connections > You and the business.
    syncTalk() {
      const sel = $("#set-talk");
      if (!sel) return;
      sel.disabled = !this.loaded;
      const style = this.currentValue("talk_style");
      if (style) sel.value = style;
      const name = (this.currentValue("owner_name") || "").trim(), salutation = (this.currentValue("owner_salutation") || "").trim();
      $("#set-talk-note").textContent = !this.loaded ? "Loading…"
        : sel.value === "formal" ? `Jarvis will call you "${salutation || name}" and keep a more formal tone.`
          : `Jarvis will call you ${name || salutation} and keep it friendly and plain.`;
    },
    setTalk(value) {
      const f = this.field("talk_style");
      if (!f) return;
      if (value === f.value) delete this.edited.talk_style; else this.edited.talk_style = value;
      this.cleared.delete("talk_style");
      this.updateSaveBar();
      this.syncTalk();
    },

    // Connections: a list of every integration with its status; each row opens its own setup form, which has a back link.
    showList() { this.view = null; this.render(); },
    openSection(id) {
      this.view = id; this.render();
      $("#drawer-body").scrollTop = 0;
      $("#settings-sections .back-link")?.focus({ preventScroll: true });
    },
    backToList() {
      const was = this.view; this.view = null; this.render();
      $("#drawer-body").scrollTop = 0;
      $(`#settings-sections [data-open-section="${was}"]`)?.focus({ preventScroll: true });
    },
    renderConnections() {
      if (!this.sections.length) return `<p class="empty">${this.loaded ? "Nothing to set up." : "Loading…"}</p>`;
      const sec = this.view && this.sections.find((x) => x.id === this.view);
      if (sec) return this.renderSection(sec);
      const row = (x) => { const st = this.status(x);
        return `<li><button type="button" class="conn-row" data-open-section="${esc(x.id)}"><span class="name">${esc(x.title)}</span><small class="${st.cls}">${esc(st.text)} ›</small></button></li>`; };
      return `<ul class="conn-list">${this.sections.map(row).join("")}</ul>`;
    },

    // The status shown on a Connections row and at the top of its form.
    status(sec) {
      if (!sec.show_badge) return { text: "Set up", cls: "" };
      const test = sec.last_test;
      if (test && !test.stale) return test.ok ? { text: "Working", cls: "ok" } : { text: "Test failed", cls: "red" };
      return sec.configured ? { text: "Connected", cls: "ok" } : { text: "Not set up", cls: "" };
    },

    renderSection(sec) {
      const shown = sec.fields.filter((f) => this.visible(f));
      const basics = shown.filter((f) => !f.advanced);
      const advanced = shown.filter((f) => f.advanced);
      const showAdvanced = this.advanced.has(sec.id);
      const guide = sec.guide?.length ? `<div class="set-guide"><strong>Setup</strong><ol>${sec.guide.map((g) => `<li>${esc(g)}</li>`).join("")}</ol></div>` : "";
      const test = sec.last_test;
      const testHtml = sec.test ? `
        <div class="set-section-actions">
          <button class="btn small" data-test="${esc(sec.id)}" type="button" ${this.testing.has(sec.id) ? "disabled" : ""}>
            ${this.testing.has(sec.id) ? "Testing…" : "Test connection"}
          </button>
        </div>
        ${test ? `<div class="set-test-result ${test.ok ? "ok" : "fail"}">${esc(test.detail)}${test.stale ? '<span class="stale-note">Settings changed since this test - test again.</span>' : ""}</div>` : ""}
      ` : "";
      return `
        <div class="set-section" data-section="${esc(sec.id)}">
          <button type="button" class="chip back-link" data-back>‹ All connections</button>
          <div class="form-head">
            <h3>${esc(sec.title)}</h3>
            <p class="blurb">${esc(sec.blurb)}</p>
            ${sec.show_badge ? `<p class="status-line"><span class="label">Status</span> <span class="${this.status(sec).cls}">${esc(this.status(sec).text)}</span></p>` : ""}
          </div>
          <div class="set-section-fields">
            ${guide}
            ${basics.map((f) => this.renderField(sec.id, f)).join("")}
            ${advanced.length ? (showAdvanced
              ? advanced.map((f) => this.renderField(sec.id, f)).join("") + `<button class="set-advanced-toggle" data-hide-advanced="${esc(sec.id)}" type="button">Hide advanced options</button>`
              : `<button class="set-advanced-toggle" data-show-advanced="${esc(sec.id)}" type="button">Show ${advanced.length} advanced option${advanced.length > 1 ? "s" : ""}</button>`) : ""}
            ${testHtml}
          </div>
        </div>`;
    },

    renderField(sectionId, f) {
      const hasEdit = Object.prototype.hasOwnProperty.call(this.edited, f.key);
      const isCleared = this.cleared.has(f.key);
      const error = this._errors?.[f.key];
      const sourceNote = f.source === "azure" ? "from Azure" : f.source === "here" ? "" : "";
      let control = "";
      if (f.kind === "bool") {
        const checked = hasEdit ? this.edited[f.key] : !!f.value;
        control = `<div class="set-checkbox"><input type="checkbox" id="f-${f.key}" data-field="${f.key}" ${checked ? "checked" : ""}>
          <label for="f-${f.key}">${esc(f.label)}</label></div>`;
        return `<div class="set-field${error ? " has-error" : ""}">${control}${f.help ? `<div class="field-help">${esc(f.help)}</div>` : ""}${error ? `<div class="field-error">${esc(error)}</div>` : ""}</div>`;
      }
      if (f.kind === "select") {
        const current = hasEdit ? this.edited[f.key] : f.value;
        control = `<select id="f-${f.key}" data-field="${f.key}">${f.options.map(([v, l]) => `<option value="${esc(v)}" ${v === current ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
      } else if (f.kind === "secret") {
        if (isCleared) {
          control = `<div class="set-secret-row"><span class="set-hint">will be cleared</span><button class="btn small" data-undo-clear="${f.key}" type="button">Undo</button></div>`;
        } else {
          const hint = hasEdit ? "new value entered" : (f.is_set ? f.hint : "not set");
          control = `<div class="set-secret-row">
            <input type="password" id="f-${f.key}" data-field="${f.key}" placeholder="${f.is_set ? "Leave blank to keep the current one" : esc(f.placeholder || "")}" autocomplete="new-password" ${NO_AUTOFILL}>
            <span class="set-hint">${esc(hint)}</span>
            ${f.is_set ? `<button class="btn small" data-clear="${f.key}" type="button">Clear</button>` : ""}
          </div>`;
        }
      } else if (f.kind === "textarea" || f.kind === "notes") {
        const current = hasEdit ? this.edited[f.key] : (f.value || "");
        control = `<textarea id="f-${f.key}" data-field="${f.key}" placeholder="${esc(f.placeholder || "")}">${esc(current)}</textarea>`;
      } else if (f.kind === "cron") {
        const current = hasEdit ? this.edited[f.key] : (f.value || "");
        const parsed = parseCron(current);
        if (!parsed.ok) {
          // Doesn't fit any of the shapes the picker understands - rare enough among these fields that a
          // plain cron box is a reasonable fallback rather than building a picker for every possible schedule.
          control = `<input type="text" id="f-${f.key}" data-field="${f.key}" value="${esc(current)}" placeholder="${esc(f.placeholder || "45 7 * * 1-5")}">
            <div class="field-help">A custom schedule, shown as cron (minute hour day month weekday).</div>`;
        } else {
          const mm = String(parsed.minute).padStart(2, "0");
          const timeChips = parsed.hours.map((h, i) => `<span class="cron-time-chip">
            <input type="time" data-field="${f.key}" data-cron-time="${i}" value="${String(h).padStart(2, "0")}:${mm}">
            ${parsed.hours.length > 1 ? `<button type="button" class="cron-time-remove" data-field="${f.key}" data-cron-remove-time="${i}" aria-label="Remove this time">✕</button>` : ""}
          </span>`).join("");
          const addTimeBtn = `<button type="button" class="linkish" data-field="${f.key}" data-cron-add-time="1">+ Add a time</button>`;
          if (parsed.mode === "dom") {
            control = `<div class="cron-picker">
              ${timeChips}
              <span class="cron-dom">on day <input type="number" min="1" max="28" data-field="${f.key}" data-cron-dom="1" value="${parsed.day}"> of the month</span>
            </div>`;
          } else {
            const dayBtns = DOW_SHORT.map((label, i) => `<button type="button" class="cron-day${parsed.days.has(i) ? " active" : ""}" data-field="${f.key}" data-cron-day="${i}">${label}</button>`).join("");
            control = `<div class="cron-picker">
              <div class="cron-times">${timeChips}${addTimeBtn}</div>
              <div class="cron-days">${dayBtns}</div>
            </div>
            <div class="cron-presets">
              <button type="button" class="linkish" data-field="${f.key}" data-cron-preset="weekdays">Weekdays</button>
              <button type="button" class="linkish" data-field="${f.key}" data-cron-preset="everyday">Every day</button>
              <button type="button" class="linkish" data-field="${f.key}" data-cron-preset="weekends">Weekends</button>
            </div>`;
          }
        }
      } else {
        let current = hasEdit ? this.edited[f.key] : (f.value ?? "");
        // A placeholder alone (grey hint text that vanishes the moment you click in) is easy to miss and type
        // straight past - an empty web-address box starts with "https://" already typed, so there's something
        // there to build on rather than a blank field that silently fails to save without it.
        if (f.kind === "url" && !current) current = "https://";
        const type = f.kind === "number" ? "number" : f.kind === "email" ? "email" : f.kind === "url" ? "url" : "text";
        const step = f.kind === "number" ? ' step="any"' : "";  // some settings (voice stability etc.) are fractional
        control = `<input type="${type}"${step} id="f-${f.key}" data-field="${f.key}" value="${esc(current)}" placeholder="${esc(f.placeholder || "")}" autocomplete="off" ${NO_AUTOFILL}>`;
      }
      return `<div class="set-field${error ? " has-error" : ""}">
        <label for="f-${f.key}">${esc(f.label)}${sourceNote ? `<span class="set-source">${sourceNote}</span>` : ""}</label>
        ${control}
        ${f.help ? `<div class="field-help">${esc(f.help)}</div>` : ""}
        ${error ? `<div class="field-error">${esc(error)}</div>` : ""}
      </div>`;
    },

    updateSaveBar() {
      const dirty = this.dirty();
      $("#settings-savebar").hidden = !dirty;
      $("#settings-status").textContent = dirty
        ? `${Object.keys(this.edited).length + this.cleared.size} change${(Object.keys(this.edited).length + this.cleared.size) === 1 ? "" : "s"} not yet saved`
        : "";
    },

    async test(sectionId) {
      this.testing.add(sectionId);
      this.render();
      try {
        const r = await api(`/api/settings/test/${encodeURIComponent(sectionId)}`, { method: "POST" });
        const result = await r.json();
        const sec = this.sections.find((s) => s.id === sectionId);
        if (sec) sec.last_test = result;
        toast(result.ok ? "Connected" : "Test failed", result.detail, result.ok ? "info" : "warning");
      } catch { toast("Couldn't run the test", "Check the connection and try again.", "warning"); }
      this.testing.delete(sectionId);
      this.render();
    },

    async save() {
      $("#btn-settings-save").disabled = true;
      $("#btn-settings-save").textContent = "Saving…";
      try {
        const r = await api("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ values: this.edited, clear: [...this.cleared] }),
        });
        const data = await r.json();
        if (!r.ok) {
          this._errors = data.errors || {};
          this.render();
          toast("Couldn't save", "Fix the highlighted fields.", "warning");
          return;
        }
        this._errors = {};
        this.edited = {}; this.cleared.clear();
        this.sections = data.sections;
        this.render(data.problem);
        if (data.signed_out) { toast("Password changed", "Sign in again with the new one."); setTimeout(() => location.href = "/login", 1200); return; }
        toast("Settings saved", "Jarvis picked up the changes.");
        refresh();
      } catch {
        toast("Couldn't save", "Check the connection and try again.", "warning");
      } finally {
        // Always re-enable, even after a validation error or a redirect-to-login - a stuck "Saving…" button
        // with no way to try again is worse than a button that's briefly clickable during the redirect.
        $("#btn-settings-save").disabled = false;
        $("#btn-settings-save").textContent = "Save changes";
      }
    },
  };

  $("#settings-sections").addEventListener("click", (e) => {
    const openRow = e.target.closest("[data-open-section]");
    if (openRow) { Settings.openSection(openRow.dataset.openSection); return; }
    if (e.target.closest("[data-back]")) { Settings.backToList(); return; }
    const test = e.target.closest("[data-test]");
    if (test) { Settings.test(test.dataset.test); return; }
    const showAdv = e.target.closest("[data-show-advanced]");
    if (showAdv) { Settings.advanced.add(showAdv.dataset.showAdvanced); Settings.render(); return; }
    const hideAdv = e.target.closest("[data-hide-advanced]");
    if (hideAdv) { Settings.advanced.delete(hideAdv.dataset.hideAdvanced); Settings.render(); return; }
    const clear = e.target.closest("[data-clear]");
    if (clear) { Settings.cleared.add(clear.dataset.clear); delete Settings.edited[clear.dataset.clear]; Settings.render(); return; }
    const undo = e.target.closest("[data-undo-clear]");
    if (undo) { Settings.cleared.delete(undo.dataset.undoClear); Settings.render(); return; }
    const cronKey = e.target.closest("[data-field]")?.dataset.field;
    const cronParsed = () => parseCron(Object.prototype.hasOwnProperty.call(Settings.edited, cronKey) ? Settings.edited[cronKey] : (Settings.field(cronKey)?.value || ""));
    const applyCron = (minute, hours, extra) => { Settings.edited[cronKey] = cronFromParts(minute, hours, extra); Settings.cleared.delete(cronKey); Settings.render(); Settings.updateSaveBar(); };
    const cronDay = e.target.closest("[data-cron-day]");
    if (cronDay) {
      const parsed = cronParsed();
      if (parsed.ok && parsed.mode === "dow") {
        const day = Number(cronDay.dataset.cronDay);
        if (parsed.days.has(day) && parsed.days.size > 1) parsed.days.delete(day); else parsed.days.add(day);
        applyCron(parsed.minute, parsed.hours, { mode: "dow", days: parsed.days });
      }
      return;
    }
    const cronPreset = e.target.closest("[data-cron-preset]");
    if (cronPreset) {
      const parsed = cronParsed();
      if (parsed.ok) {
        const preset = cronPreset.dataset.cronPreset;
        const days = preset === "weekdays" ? new Set([0, 1, 2, 3, 4]) : preset === "weekends" ? new Set([5, 6]) : new Set([0, 1, 2, 3, 4, 5, 6]);
        applyCron(parsed.minute, parsed.hours, { mode: "dow", days });
      }
      return;
    }
    const addTime = e.target.closest("[data-cron-add-time]");
    if (addTime) {
      const parsed = cronParsed();
      if (parsed.ok && parsed.mode === "dow") {
        const lastHour = Math.max(...parsed.hours);
        applyCron(parsed.minute, [...parsed.hours, Math.min(lastHour + 1, 23)], { mode: "dow", days: parsed.days });
      }
      return;
    }
    const removeTime = e.target.closest("[data-cron-remove-time]");
    if (removeTime) {
      const parsed = cronParsed();
      if (parsed.ok && parsed.hours.length > 1) {
        const hours = parsed.hours.filter((_, i) => i !== Number(removeTime.dataset.cronRemoveTime));
        applyCron(parsed.minute, hours, parsed.mode === "dom" ? { mode: "dom", day: parsed.day } : { mode: "dow", days: parsed.days });
      }
      return;
    }
  });
  // Unlike the click-driven cron edits above, typing shouldn't re-render the whole panel (that would steal
  // focus mid-keystroke) - just update the stored value and the "unsaved changes" bar, same as any other field.
  const applyCronInput = (key, minute, hours, extra) => { Settings.edited[key] = cronFromParts(minute, hours, extra); Settings.cleared.delete(key); Settings.updateSaveBar(); };
  $("#settings-sections").addEventListener("input", (e) => {
    const el = e.target.closest("[data-field]");
    if (!el) return;
    const key = el.dataset.field;
    const f = Settings.field(key);
    if (!f) return;
    if (el.dataset.cronTime !== undefined) {
      const current = Object.prototype.hasOwnProperty.call(Settings.edited, key) ? Settings.edited[key] : (f.value || "");
      const parsed = parseCron(current);
      const [hh, mm] = el.value.split(":").map(Number);
      if (parsed.ok && !Number.isNaN(hh) && !Number.isNaN(mm)) {
        const hours = [...parsed.hours]; hours[Number(el.dataset.cronTime)] = hh;
        applyCronInput(key, mm, hours, parsed.mode === "dom" ? { mode: "dom", day: parsed.day } : { mode: "dow", days: parsed.days });
      }
    }
    else if (el.dataset.cronDom !== undefined) {
      const current = Object.prototype.hasOwnProperty.call(Settings.edited, key) ? Settings.edited[key] : (f.value || "");
      const parsed = parseCron(current);
      const day = Number(el.value);
      if (parsed.ok && !Number.isNaN(day)) applyCronInput(key, parsed.minute, parsed.hours, { mode: "dom", day });
    }
    else if (f.kind === "bool") Settings.edited[key] = el.checked;
    else if (f.kind === "number") Settings.edited[key] = el.value === "" ? "" : Number(el.value);
    else Settings.edited[key] = el.value;
    Settings.cleared.delete(key);
    // A select/checkbox is a discrete, complete choice (unlike typing, there's no mid-keystroke focus to
    // lose) - re-render so any field whose depends_on names this one shows or hides immediately, e.g.
    // switching Voice provider swaps which provider's fields are visible without needing to save first.
    if (f.kind === "select" || f.kind === "bool") Settings.render(); else Settings.updateSaveBar();
  });
  $("#set-talk").addEventListener("change", (e) => Settings.setTalk(e.target.value));
  $("#btn-settings-save").addEventListener("click", () => Settings.save());
  $("#btn-settings-cancel").addEventListener("click", () => Settings.revert());

  // ------------------------------------------------------------------ boot
  (async () => {
    await refresh();
    await loadTranscript();
    connect();
    refreshMap();
    setInterval(refresh, 60000);
    setInterval(refreshMap, 60000);
    if (S.listenMode === "wake") toast("Always-listening mode", "Tap anywhere to enable the microphone and voice.");
    window.addEventListener("click", () => { if (S.listenMode === "wake" && !stt.on && !sentry.on) enterWakeMode(); }, { once: true });
  })();
})();
