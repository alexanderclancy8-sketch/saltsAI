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
    status: null, voice: { tts: "browser", stt: "browser", wake_word: "jarvis", language: "en-GB" },
    ws: null, approvals: [], suggestions: [], hudState: "idle", level: 0, targetLevel: 0,
    listenMode: store.get("listen", "ptt"), speakPref: store.get("speak", "voice"), voiceId: store.get("voice", ""),
    lastMode: "typed", followUpUntil: 0, attachments: [],
    dashOpen: store.get("dashboard", "0") === "1",
  };

  // ------------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => (n === null || n === undefined || isNaN(n)) ? "-" : "£" + Math.round(n).toLocaleString("en-GB");
  const time = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); };
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
    $("#clock").innerHTML = `${now.toLocaleTimeString("en-GB")}<small>${now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" })}</small>`;
  }
  setInterval(tick, 1000); tick();

  // ------------------------------------------------------------------ HUD state + reactor
  const STATE_LABEL = { idle: "Online", listening: "Listening", thinking: "Thinking", speaking: "Speaking", awaiting: "Yes, sir?" };
  function setHud(state) {
    S.hudState = state;
    $("#state").textContent = STATE_LABEL[state] || state;
    $("#btn-stop").hidden = !["thinking", "speaking"].includes(state);
    // A turn just started - don't let a sleep check meant for the *previous* lull fire part-way through it.
    if (state === "thinking") clearTimeout(sleepTimer);
  }
  function caption(text, interim = "") { $("#caption").innerHTML = esc(text) + (interim ? ` <span class="interim">${esc(interim)}</span>` : ""); }

  // ------------------------------------------------------------------ dashboard reveal (orb-first HUD)
  // Idle view is just the orb, caption and composer - the three panel columns and the conversation
  // transcript are opt-in, remembered per-browser. The choice is also applied inline in <head> (same
  // jarvis.dashboard key) so there's no flash of the wrong layout before this script runs.
  function setDashOpen(open) {
    S.dashOpen = open;
    store.set("dashboard", open ? "1" : "0");
    document.body.classList.toggle("dash-open", open);
    const btn = $("#btn-dashboard");
    btn.setAttribute("aria-pressed", String(open));
    btn.title = open ? "Hide dashboard" : "Show dashboard";
  }
  $("#btn-dashboard").addEventListener("click", () => setDashOpen(!S.dashOpen));
  setDashOpen(S.dashOpen); // sync the button label/state with whatever <head> already applied to <body>

  // Approvals/suggestions must never go silently unnoticed just because the dashboard is tucked away -
  // a small pulsing badge on the orb itself covers that, and opens the real panels (with their working
  // Approve/Cancel buttons) rather than duplicating that rendering here.
  function updateOrbBadge() {
    const n = (S.approvals?.length || 0) + (S.suggestions?.length || 0);
    const badge = $("#orb-badge");
    badge.hidden = n === 0;
    if (n) badge.textContent = String(n);
  }
  $("#orb-badge").addEventListener("click", () => {
    setDashOpen(true);
    requestAnimationFrame(() => {
      const target = (S.approvals?.length ? $("#approvals-panel") : $("#suggestions-panel"));
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  const canvas = $("#reactor");
  const ctx = canvas.getContext("2d");
  function drawReactor(t) {
    const w = canvas.width, h = canvas.height, cx = w / 2, cy = h / 2;
    S.level += (S.targetLevel - S.level) * 0.2;
    if (S.hudState === "speaking" && speaker.browserSpeaking) S.targetLevel = 0.3 + 0.25 * Math.abs(Math.sin(t / 90));
    const colour = { idle: "76,141,255", listening: "52,211,153", thinking: "245,185,66", speaking: "76,141,255", awaiting: "52,211,153" }[S.hudState] || "76,141,255";
    const lvl = S.level;
    ctx.clearRect(0, 0, w, h);
    const speed = S.hudState === "thinking" ? 1.4 : 1;
    // A calm progress ring plus one slow-rotating tick ring - a status indicator, not a light show.
    ctx.save(); ctx.translate(cx, cy);
    ctx.lineWidth = 3; ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.beginPath(); ctx.arc(0, 0, 128, 0, Math.PI * 2); ctx.stroke();
    ctx.lineWidth = 3; ctx.strokeStyle = `rgba(${colour},${0.55 + lvl * 0.35})`; ctx.lineCap = "round";
    ctx.beginPath(); ctx.arc(0, 0, 128, -Math.PI / 2, -Math.PI / 2 + (0.18 + lvl * 0.7) * Math.PI * 2); ctx.stroke();
    ctx.rotate(t * 0.00025 * speed);
    ctx.setLineDash([2, 16]); ctx.lineWidth = 1.5; ctx.strokeStyle = `rgba(${colour},0.3)`;
    ctx.beginPath(); ctx.arc(0, 0, 108, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
    const core = 30 + lvl * 18;
    const g = ctx.createRadialGradient(cx, cy, 2, cx, cy, core);
    g.addColorStop(0, "rgba(255,255,255,0.9)"); g.addColorStop(0.5, `rgba(${colour},0.75)`); g.addColorStop(1, `rgba(${colour},0)`);
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, core, 0, Math.PI * 2); ctx.fill();
    requestAnimationFrame(drawReactor);
  }
  requestAnimationFrame(drawReactor);

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
    feed(delta) { this.buffer += delta; const parts = this.buffer.split(/(?<=[.!?…:])\s+(?=[A-Z0-9"'£(])/); this.buffer = parts.pop(); parts.forEach((p) => this.enqueue(p)); },
    flush() { if (this.buffer.trim()) this.enqueue(this.buffer); this.buffer = ""; },
    enqueue(sentence) {
      const clean = sentence.replace(/```[\s\S]*?```/g, " ").replace(/[#*_`>|]/g, " ").replace(/\s+/g, " ").trim();
      if (!clean || /^[-\s]+$/.test(clean)) return;
      // Remember what we're about to say so the mic can recognise it as our own voice if it hears it back.
      const now = Date.now();
      this.recent = this.recent.filter((r) => now - r.at < RECENT_TTS_MS).slice(-30);
      this.recent.push({ text: clean, at: now });
      const item = { text: clean, audio: S.voice.tts !== "browser" ? this.fetchAudio(clean) : null };
      this.queue.push(item);
      if (!this.active) this.next();
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
      if (!item) { this.active = false; this.browserSpeaking = false; this.lastSpokeAt = Date.now(); if (S.hudState === "speaking") setHud("idle"); extendFollowUp(); if (this.onIdle) this.onIdle(); return; }
      this.active = true; setHud("speaking");
      const url = item.audio ? await item.audio : null;
      if (!this.active) return;
      if (url) {
        player.src = url;
        player.onended = () => { URL.revokeObjectURL(url); this.next(); };
        player.onerror = () => this.next();
        try { await player.play(); } catch { voiceProblem("the browser blocked the audio - click anywhere on the page and try again"); this.speakBrowser(item.text); }
      } else this.speakBrowser(item.text);
    },
    speakBrowser(text) {
      if (!("speechSynthesis" in window)) { this.next(); return; }
      const u = new SpeechSynthesisUtterance(text);
      const v = pickBrowserVoice(); if (v) u.voice = v;
      u.lang = "en-GB"; u.rate = 1.02; u.pitch = 0.95;
      this.browserSpeaking = true;
      u.onend = u.onerror = () => { this.browserSpeaking = false; this.next(); };
      speechSynthesis.speak(u);
    },
    stop() {
      // Only start the echo tail if something was actually being said - a push-to-talk press calls stop() on a
      // silent speaker, and that must not put the owner's own first words inside an "echo window".
      if (this.active || this.queue.length || this.browserSpeaking) this.lastSpokeAt = Date.now();
      this.queue = []; this.buffer = ""; this.active = false; this.browserSpeaking = false; player.pause(); if ("speechSynthesis" in window) speechSynthesis.cancel(); setHud("idle"); },
  };
  const shouldSpeak = (mode) => S.speakPref === "always" || (S.speakPref === "voice" && mode === "voice");

  function say(text) { if (S.speakPref !== "off") { speaker.feed(text + " "); speaker.flush(); } }

  // ------------------------------------------------------------------ self-echo guard
  // Without headphones the mic hears Jarvis's own voice. Nothing heard while he's talking, or in the short tail
  // after, may ever be treated as the owner asking something - so every recognised transcript goes through
  // looksLikeSelfEcho() before it is kept or submitted (in stt, sentry and utterance()).
  const ECHO_TAIL_MS = 2500;      // how long past the end of speech the mic might still be hearing its tail
  const RECENT_TTS_MS = 60000;    // how long we remember what we said
  const RECENT_MATCH_MS = 20000;  // outside the echo window, only compare against very recent speech
  const PTT_SILENCE_MS = 1200;    // push-to-talk: this long after the last final result counts as end of speech
  const normWords = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9\s]+/g, " ").split(/\s+/).filter(Boolean);
  const echoWindowOpen = () => speaker.active || speaker.browserSpeaking || Date.now() - speaker.lastSpokeAt < ECHO_TAIL_MS;

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

  // ------------------------------------------------------------------ conversation
  let current = null;
  function addMessage(role, text, extra = "") {
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    el.innerHTML = `<div class="meta">${role === "user" ? esc(S.status?.owner || "You") : "Jarvis"}${extra ? " · " + esc(extra) : ""}</div><div class="md"></div>`;
    el.querySelector(".md").innerHTML = role === "assistant" ? md(text) : esc(text).replace(/\n/g, "<br>");
    $("#conversation").appendChild(el);
    $("#conversation").scrollTop = 1e9;
    return el;
  }

  async function loadTranscript() {
    try {
      const rows = await (await api("/api/transcript")).json();
      rows.slice(-20).forEach((r) => addMessage(r.role, r.text, time(r.created_at)));
    } catch { /* ignore */ }
  }

  function send(text, mode = "typed", opts = {}) {
    text = text.trim();
    if (!text && !S.attachments.length) return;
    S.lastMode = mode;
    speaker.stop();
    // A barge-in (or any new message sent while Jarvis is still mid-reply) must cancel the turn server-side,
    // not just silence local audio - otherwise the old turn keeps running, its own delta/tool/reply events
    // still arrive and get rendered and spoken, and this and the new turn's events interleave. stopEverything()
    // (the Stop button/Esc) always did this; ordinary sends via utterance()/the composer didn't. A harmless
    // no-op when nothing is actually running - the listen() loop on the backend awaits it before touching the
    // chat message that follows, so ordering is guaranteed.
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "stop" }));
    const payload = { type: "chat", text: text || "Please look at the attached file(s).", mode, attachments: S.attachments };
    // Only text the owner typed into the chat box may be learned as a "usual reply" (never buttons or speech).
    if (opts.compose && mode === "typed") payload.compose = true;
    S.attachments = []; renderAttachments();
    if (S.ws && S.ws.readyState === 1) { S.ws.send(JSON.stringify(payload)); return; }
    setHud("thinking");
    streamChat(payload).catch(() => { toast("Couldn't reach Jarvis", "Check the connection.", "warning"); setHud("idle"); });
  }

  // The WebSocket is down (or hasn't connected yet) - falls back to the same conversation over a plain
  // POST, but still streamed word-by-word: each server-sent-event line is exactly the shape handle() already
  // knows how to render, so the experience matches the WebSocket path instead of waiting on the full reply.
  async function streamChat(payload) {
    const r = await api("/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
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
  }

  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault(); send($("#input").value, "typed", { compose: true }); $("#input").value = ""; autosize();
    RS.dismissed = null; RS.text = null; rsRender();
  });
  const autosize = () => { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; };
  $("#input").addEventListener("input", () => { autosize(); rsSoon(); });
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
  $("#quick").addEventListener("click", (e) => {
    const q = e.target.closest("[data-q]");
    if (q) send(q.dataset.q, S.speakPref === "off" ? "typed" : "voice");
  });
  $("#btn-briefing").addEventListener("click", () => send("Give me my briefing", S.speakPref === "off" ? "typed" : "voice"));
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
    ws.onmessage = (e) => handle(JSON.parse(e.data));
    ws.onclose = (e) => { if (e.code === 4401) { location.href = "/login"; return; } setTimeout(connect, 2500); };
    setInterval(() => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" })); }, 25000);
  }

  let toolsSeen = [];
  let refreshTimer = null;
  const refreshSoon = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 1500); };

  function handle(ev) {
    const d = ev.data;
    switch (ev.type) {
      case "user_message":
        addMessage("user", d.text + (d.attachments?.length ? `\n📎 ${d.attachments.join(", ")}` : ""), d.mode === "voice" ? "spoken" : "");
        S.lastMode = d.mode;
        break;
      case "thinking":
        setHud("thinking"); toolsSeen = [];
        current = addMessage("assistant", ""); current.querySelector(".md").classList.add("typing");
        current.dataset.raw = "";
        break;
      case "delta":
        if (!current) { current = addMessage("assistant", ""); current.dataset.raw = ""; }
        current.dataset.raw += d.text;
        current.querySelector(".md").innerHTML = md(current.dataset.raw);
        $("#conversation").scrollTop = 1e9;
        if (shouldSpeak(d.mode)) speaker.feed(d.text);
        break;
      case "tool":
        if (d.state === "start") { $("#toolline").textContent = "› " + d.label + "…"; toolsSeen.push(d.label); }
        else if (d.state === "error") $("#toolline").textContent = "› " + d.label + " - problem";
        break;
      case "reply":
        $("#toolline").textContent = "";
        if (current) {
          if (d.replace || !current.dataset.raw) current.dataset.raw = d.text;
          const body = current.querySelector(".md");
          body.classList.remove("typing"); body.innerHTML = md(current.dataset.raw);
          if (toolsSeen.length) current.insertAdjacentHTML("beforeend", `<div class="tools">${esc([...new Set(toolsSeen)].join(" · "))}</div>`);
        } else addMessage("assistant", d.text);
        if (shouldSpeak(d.mode)) { if (d.replace) speaker.feed(d.text); speaker.flush(); }
        if (!speaker.active) { setHud("idle"); extendFollowUp(); }
        caption(d.text.replace(/[#*_`|]/g, "").slice(0, 180) + (d.text.length > 180 ? "…" : ""));
        current = null;
        refreshSoon();
        rsSoon(); // Jarvis's new reply changes the situation the suggestion is matched to
        break;
      case "error":
        if (current) current.remove();
        current = null;
        addMessage("assistant", d.message).classList.add("error");
        // The transcript panel is hidden by default in the minimal orb view, so the caption is the only
        // place a failed reply is otherwise visible - without this an error would fail completely silently.
        caption(d.message);
        setHud("idle"); $("#toolline").textContent = ""; extendFollowUp();
        break;
      case "notification":
        // Displayed only - a pushed notification (briefing, suggestion, alert) never speaks unprompted,
        // whatever its server-side `speak` flag says.
        toast(d.title, d.body, d.level);
        refreshSoon();
        break;
      case "owner_update":
        toast("Update sent", `${d.subject} → ${d.channels.join(", ") || "display"}`);
        break;
      case "display": openDisplay(d.title, d.markdown, d.doc_id); break;
      case "approvals": S.approvals = d; renderApprovals(); break;
      case "suggestions": S.suggestions = d; renderSuggestions(); break;
      case "issue": refreshSoon(); break;
      case "tests": renderTests(d); break;
      case "map": renderMap(d); break;
      case "conversation_reset": $("#conversation").innerHTML = ""; caption("Fresh start. What can I do for you?"); break;
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
      dl.hidden = false;
    } else {
      dl.hidden = true;
    }
    $("#display-body").innerHTML = md(markdown);
    $("#display").classList.add("open");
  }
  $("#display-close").addEventListener("click", () => $("#display").classList.remove("open"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#display").classList.remove("open"); $("#drawer").classList.remove("open"); } });

  // ------------------------------------------------------------------ panels
  async function refresh() {
    try {
      const st = await (await api("/api/status")).json();
      S.status = st; S.voice = st.voice || S.voice; S.approvals = st.approvals || []; S.suggestions = st.suggestions || [];
      $("#company").textContent = (st.company || "").toUpperCase();
      renderPills(st.connections); renderInbox(st.inbox); renderIssues(st.issues); renderTests(st.tests);
      renderNotifications(st.notifications); renderOps(st.staff, st.overdue_jobs); renderFinance(st.finance);
      renderPresence(st.presence); renderCustomers(st.customer_watch); renderDeadlines(st.deadlines, st.accreditations); renderApprovals(); renderSuggestions(); renderSettings(st);
    } catch (e) { console.warn(e); }
  }

  function renderPills(conns = {}) {
    const demo = Object.entries(conns).filter(([, v]) => String(v).includes("DEMO")).map(([k]) => k);
    $("#pills").innerHTML = demo.length
      ? `<span class="pill demo" title="${esc(demo.join(", "))}">Demo data: ${demo.length} source${demo.length > 1 ? "s" : ""}</span>`
      : `<span class="pill live">All systems live</span>`;
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
      return `<li class="${cls}">#${i.id} ${esc(i.title)}<span class="sub">${esc(i.reporter)} · ${esc(i.status.replace("_", " "))} · ${esc(i.severity)}${pr}</span></li>`;
    }).join("") : `<li class="empty">No open issues.</li>`;
  }

  function renderTests(tests = []) {
    const failing = tests.filter((t) => !t.ok);
    $("#tests-count").textContent = tests.length ? `${tests.length - failing.length}/${tests.length} passing` : "";
    const rows = [...failing, ...tests.filter((t) => t.ok)].slice(0, 10);
    $("#tests").innerHTML = rows.length ? rows.map((t) => `<li class="${t.ok ? "ok" : "bad"}"><span class="dot ${t.ok ? "ok" : "bad"}"></span>${esc(t.name)}<span class="sub">${esc(t.detail).slice(0, 140)}</span></li>`).join("")
      : `<li class="empty">No results yet.</li>`;
  }

  function renderNotifications(list = []) {
    $("#notifications").innerHTML = list.length ? list.slice(0, 6).map((n) => `<li class="${n.level === "critical" ? "bad" : n.level === "warning" ? "warn" : ""}">${esc(n.title)}<span class="sub">${time(n.created_at)}</span></li>`).join("")
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
    $("#ops").innerHTML = staff.engineers.map((e) => `<li class="${e.status === "on job" ? "ok" : ""}">${esc(e.name)}<span class="sub">${esc(e.current_job || e.status)}${e.next_job ? " · next " + esc(e.next_job) : ""}</span></li>`).join("");
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
      return `<li class="${m && m.change_7d > 0 ? "ok" : ""}">${names[k] || k}: <b>${m ? Math.round(m.current) : "-"}</b>${r ? ` · ${r.current}★` : ""}<span class="sub">${change} this week${p.demo ? " · demo" : ""}</span></li>`;
    });
    $("#presence").innerHTML = rows.join("") || `<li class="empty">Connect socials in settings.</li>`;
  }

  function renderCustomers(list = []) {
    $("#customers-count").textContent = list.length ? `${list.length} to watch` : "all healthy";
    $("#customers").innerHTML = list.length ? list.map((c) => `<li class="${c.status === "at risk" ? "bad" : "warn"}">${esc(c.customer)} · <b>${c.score}</b>
      <span class="sub">${esc(c.reasons.slice(0, 2).join("; "))}${c.renewal_in_days !== null && c.renewal_in_days <= 90 ? ` · renewal ${c.renewal_in_days < 0 ? "passed" : "in " + c.renewal_in_days + " days"}` : ""}</span></li>`).join("")
      : `<li class="empty">No customers showing warning signs.</li>`;
  }

  function renderDeadlines(deadlines = [], accreditations = []) {
    const items = [...(accreditations || []).map((a) => ({ what: a.what, due: a.date, days_left: a.days_left })), ...deadlines]
      .sort((a, b) => a.due < b.due ? -1 : 1).slice(0, 8);
    $("#deadlines").innerHTML = items.map((d) => `<li class="${d.days_left < 0 ? "bad" : d.days_left <= 14 ? "warn" : ""}">${esc(d.what)}<span class="sub">${dayMonth(d.due)} · ${d.days_left < 0 ? Math.abs(d.days_left) + " days overdue" : d.days_left + " days"}</span></li>`).join("");
  }

  function renderApprovals() {
    const list = S.approvals || [];
    $("#approvals-panel").hidden = !list.length;
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
    $("#suggestions-count").textContent = list.length ? String(list.length) : "";
    $("#suggestions").innerHTML = list.map((s) => `<div class="suggestion p${s.priority}">${esc(s.title)}
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
  let map = null, layer = null;
  function renderMap(data) {
    if (!data) return;
    if (!window.L) {  // map library blocked/offline: show a list instead
      $("#map").innerHTML = `<ul class="list" style="padding:8px">${(data.engineers || []).map((e) =>
        `<li>${esc(e.engineer)}<span class="sub">${esc(e.current_job || e.status || "")}${e.eta_next_job_mins ? " · ETA next " + e.eta_next_job_mins + " min" : ""}</span></li>`).join("") ||
        `<li class="empty">${esc(data.note || "No vehicles reporting.")}</li>`}</ul>`;
      $("#map").style.height = "auto";
      return;
    }
    if (!map) {
      map = L.map("map", { zoomControl: false, attributionControl: true }).setView([53.83, -1.78], 10);
      L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", { attribution: "© OpenStreetMap, © CARTO", maxZoom: 18 }).addTo(map);
      layer = L.layerGroup().addTo(map);
    }
    layer.clearLayers();
    $("#map-note").textContent = data.working_hours === false ? "outside hours" : data.demo ? "demo" : `${data.engineers.length} vans`;
    const pts = [];
    (data.sites || []).forEach((s) => { L.circleMarker([s.lat, s.lng], { radius: 5, color: "#ff6a3d", weight: 2, fillOpacity: 0.6 }).bindTooltip(esc(s.name)).addTo(layer); pts.push([s.lat, s.lng]); });
    (data.engineers || []).forEach((e) => {
      L.circleMarker([e.lat, e.lng], { radius: 7, color: e.status === "driving" ? "#ffb020" : "#26d9ff", weight: 2, fillOpacity: 0.85 })
        .bindTooltip(`${esc(e.engineer)}<br>${esc(e.current_job || e.status || "")}${e.eta_next_job_mins ? `<br>ETA next: ${e.eta_next_job_mins} min` : ""}`).addTo(layer);
      pts.push([e.lat, e.lng]);
    });
    if (pts.length) map.fitBounds(pts, { padding: [20, 20], maxZoom: 12 });
  }
  async function refreshMap() { try { renderMap(await (await api("/api/tracking")).json()); } catch { /* ignore */ } }

  // ------------------------------------------------------------------ speech input
  const mic = $("#btn-mic");
  const stt = {
    on: false, stream: null, rec: null, ws: null, finals: "", recognition: null, chunks: [], silenceTimer: null,
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
      this.silenceTimer = setTimeout(() => this.commit(), PTT_SILENCE_MS);
    },
    async start() {
      if (this.on) return;
      ensureAudio();
      speaker.stop();
      this.on = true; this.finals = ""; mic.classList.add("on"); setHud("listening"); caption("", "Listening…");
      const mode = S.voice.stt;
      try {
        if (mode === "browser") return this.startBrowser();
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/mp4";
        this.rec = new MediaRecorder(this.stream, { mimeType: mime });
        if (mode === "deepgram") {
          this.ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/stt`);
          this.ws.onmessage = (e) => this.onDeepgram(JSON.parse(e.data));
          this.ws.onopen = () => { this.rec.ondataavailable = (e) => { if (e.data.size && this.ws.readyState === 1) this.ws.send(e.data); }; this.rec.start(250); };
          this.ws.onclose = () => { if (this.on && S.listenMode === "wake") setTimeout(() => { this.stop(false); this.start(); }, 1000); };
        } else {
          this.chunks = [];
          this.rec.ondataavailable = (e) => this.chunks.push(e.data);
          this.rec.onstop = () => this.transcribeChunks(mime);
          this.rec.start();
        }
      } catch (e) {
        toast("Microphone unavailable", e.message || "Allow microphone access in the browser.", "warning");
        this.stop(false);
      }
    },
    startBrowser() {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) { toast("Voice input not supported", "Use Chrome or Edge, or connect Deepgram.", "warning"); this.stop(false); return; }
      const r = new SR(); this.recognition = r;
      r.lang = S.voice.language || "en-GB"; r.continuous = true; r.interimResults = true;
      r.onresult = (e) => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const heard = e.results[i][0].transcript;
          if (e.results[i].isFinal) {
            if (looksLikeSelfEcho(heard)) continue; // our own voice coming back in - never a command
            this.finals += heard + " "; this.finalHeard();
          } else if (!echoWindowOpen()) interim += heard;
        }
        caption(this.finals, interim);
      };
      r.onend = () => { if (this.on && S.listenMode === "wake") r.start(); else if (this.on) this.stop(true); };
      r.start();
    },
    onDeepgram(m) {
      if (m.type === "transcript") {
        if (m.is_final && m.text && !looksLikeSelfEcho(m.text)) { this.finals += m.text + " "; if (S.listenMode !== "wake") this.finalHeard(); }
        caption(this.finals, m.is_final || echoWindowOpen() ? "" : m.text);
        if (m.speech_final && this.finals.trim()) this.commit();
      } else if (m.type === "utterance_end" && this.finals.trim()) this.commit();
      else if (m.type === "speech_started" && speaker.active && S.listenMode === "wake") { /* barge-in handled on words */ }
      else if (m.type === "error") { toast("Speech service", m.message, "warning"); }
    },
    async transcribeChunks(mime) {
      const blob = new Blob(this.chunks, { type: mime });
      if (blob.size < 2000) return;
      const fd = new FormData(); fd.append("audio", blob, "speech.webm");
      caption("", "Transcribing…");
      const r = await api("/api/stt", { method: "POST", body: fd });
      if (r.ok) utterance((await r.json()).text || "");
    },
    stop(submit = true) {
      if (!this.on) return;
      clearTimeout(this.silenceTimer); this.silenceTimer = null;
      this.on = false; mic.classList.remove("on");
      if (this.recognition) { const r = this.recognition; this.recognition = null; r.onend = null; r.stop(); }
      if (this.rec && this.rec.state !== "inactive") this.rec.stop();
      if (this.ws) { const ws = this.ws; this.ws = null; ws.onclose = null; try { ws.send(JSON.stringify({ type: "Finalize" })); } catch { /* closed */ } setTimeout(() => { try { ws.send(JSON.stringify({ type: "CloseStream" })); ws.close(); } catch { /* closed */ } }, 900); }
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null; this.rec = null;
      if (submit && S.voice.stt !== "whisper") setTimeout(() => { if (this.finals.trim()) utterance(this.finals); this.finals = ""; }, S.voice.stt === "deepgram" ? 1100 : 300);
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
  function extendFollowUp(ms = WAKE_LISTEN_MS) {
    S.followUpUntil = Date.now() + ms;
    if (S.listenMode !== "wake" || S.voice.stt === "browser") return;
    if (!stt.on) stt.start();
    clearTimeout(sleepTimer);
    sleepTimer = setTimeout(checkSleep, ms + 250);
  }
  function checkSleep() {
    if (S.listenMode !== "wake" || S.voice.stt === "browser") return;
    const remaining = S.followUpUntil - Date.now();
    if (remaining > 0) { sleepTimer = setTimeout(checkSleep, remaining + 250); return; }
    if (stt.on) stt.stop(false);
    sentry.start();
  }

  // Two separate regex objects, deliberately - a single /g-flagged RegExp used with both .test() and .replace()
  // shares mutable lastIndex state between those calls, which silently skips or duplicates matches. Test and
  // strip need their own instances even though the pattern is identical.
  const STOP_PHRASE_TEST_RE = /\b(stop|quiet|enough|cancel|shut up)\b/;
  const STOP_PHRASE_STRIP_RE = /\b(stop|quiet|enough|cancel|shut up)\b/g;

  function utterance(raw) {
    const text = String(raw || "").trim();
    if (!text) return;
    const lower = text.toLowerCase();
    const wake = (S.voice.wake_word || "jarvis").toLowerCase();
    // Last line of defence for every listener (browser, Deepgram, Whisper, sentry): anything that looks like
    // Jarvis's own voice - repeated wake phrases mashed together, or a close match for what he recently said -
    // is dropped outright, in every listen mode, and can never be treated as the owner asking something.
    if (looksLikeSelfEcho(text)) return;
    // Echo cancellation is never perfect without headphones, and room echo/output buffering trails on past
    // the moment playback actually stops - so the mic can pick up the tail end of Jarvis's own voice just
    // after speaker.active has already gone false (right when extendFollowUp() opens the real mic back up).
    // Keep checking for a short tail past the end of speech, not only while still actively speaking. In
    // push-to-talk the owner deliberately opened the mic themselves (which also silenced Jarvis), so only the
    // similarity check above applies there; in wake mode the strict allowlist below does as well.
    if (S.listenMode === "wake" && echoWindowOpen()) {
      const isStopPhrase = STOP_PHRASE_TEST_RE.test(lower) || lower.includes(wake);
      // While actively speaking (or just finished), only actually respond to a stop phrase or the wake word -
      // anything else heard in this window is presumed to be the mic picking up Jarvis's own voice, not a
      // real interruption. A word-overlap heuristic used to sit here instead, judging echo by how much heard
      // text matched Jarvis's recent speech - but speech-to-text often mangles a TTS voice badly enough that
      // genuine echo scores a *low* match and sails straight through as if it were a real command. Requiring
      // the wake word or a stop phrase has no such failure mode: it's a strict allowlist, not a similarity
      // score, so mistranscribed echo is rejected the same as clearly-echoed echo.
      if (!isStopPhrase) return;
      if (speaker.active) speaker.stop();
      // A bare "stop"/"quiet"/"Jarvis, stop" - nothing left worth answering once the stop words and wake word
      // are stripped out - should just go quiet. Falling through to send() below would forward the word
      // "stop" itself to Jarvis as a fresh question, which it answers and speaks aloud - so saying "stop"
      // during a reply just started a new one every time, rather than ever actually going quiet.
      const remaining = lower.replace(STOP_PHRASE_STRIP_RE, " ").replace(new RegExp(`\\b${wake}\\b`, "g"), " ")
        .replace(/[^a-z0-9]+/g, " ").trim();
      if (isStopPhrase && remaining.length < 3) { extendFollowUp(); return; }
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
      // The wake word is required on every single utterance now, with no "quick follow-up, skip repeating
      // Jarvis" exception - that exception was a real, reported source of false triggers: anything heard
      // during the old 20s follow-up window (including a stray or delayed echo of Jarvis's own voice) got
      // treated as a genuine command unconditionally, with none of the active-speech echo protection above
      // applying to it. extendFollowUp()/WAKE_LISTEN_MS still matter for a different reason - keeping the
      // real microphone open rather than dropping back to the free wake-word-only spotter - just not for
      // skipping the wake word itself any more.
      const idx = lower.indexOf(wake);
      if (idx === -1) { caption("", `(heard: "${text.slice(0, 60)}")`); return; }
      const cmd = text.slice(idx + wake.length).replace(/^[\s,.!?]+/, "");
      if (!cmd) { setHud("awaiting"); extendFollowUp(); say("Yes, sir?"); return; }
      send(cmd, "voice");
    } else send(text, "voice");
  }

  mic.addEventListener("click", () => {
    // A tap must always have a real "off" to reach. Before this, tapping while the free wake-word listener
    // (sentry) was active jumped straight to starting the real microphone instead of stopping - so in
    // always-listening mode the mic looked permanently lit, since there was never a path back to fully off.
    if (stt.on) { stt.stop(true); return; }
    if (sentry.on) { sentry.stop(); return; }
    stt.start();
  });

  // ------------------------------------------------------------------ stop
  function stopEverything() {
    speaker.stop(); // instant - halts audio/browser speech straight away
    if (current) {
      const body = current.querySelector(".md");
      body.classList.remove("typing");
      if (!current.dataset.raw) current.remove(); else body.innerHTML = md(current.dataset.raw) + `<div class="tools">Stopped.</div>`;
      current = null;
    }
    setHud("idle");
    $("#toolline").textContent = "";
    // Tell the backend too, so it actually stops generating and the next message doesn't queue behind it.
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "stop" }));
    else api("/api/interrupt", { method: "POST" }).catch(() => {});
  }
  $("#btn-stop").addEventListener("click", stopEverything);
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#btn-stop").hidden) stopEverything(); });
  let spaceHeld = false;
  window.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement?.tagName) || S.listenMode === "wake") return;
    e.preventDefault(); spaceHeld = true; stt.start();
  });
  window.addEventListener("keyup", (e) => { if (e.code === "Space" && spaceHeld) { spaceHeld = false; stt.stop(true); } });

  // ------------------------------------------------------------------ settings drawer: voice/display tab
  function openDrawer() {
    $("#drawer").classList.add("open");
    if (!Settings.loaded) Settings.load();
  }
  $("#btn-settings").addEventListener("click", openDrawer);
  $("#drawer-close").addEventListener("click", () => {
    if (Settings.dirty() && !confirm("Discard unsaved connection changes?")) return;
    Settings.revert();
    $("#drawer").classList.remove("open");
  });
  $("#drawer-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".drawer-tab");
    if (!tab) return;
    $$(".drawer-tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$(".drawer-pane").forEach((p) => { p.hidden = p.id !== `pane-${tab.dataset.pane}`; });
  });
  $("#set-listen").value = S.listenMode; $("#set-speak").value = S.speakPref;
  $("#set-listen").addEventListener("change", (e) => {
    S.listenMode = e.target.value; store.set("listen", S.listenMode);
    if (S.listenMode === "wake") { enterWakeMode(); toast("Always listening", `Say "${S.voice.wake_word}…" to talk to me.`); } else exitWakeMode();
  });
  $("#set-speak").addEventListener("change", (e) => { S.speakPref = e.target.value; store.set("speak", S.speakPref); if (S.speakPref === "off") speaker.stop(); });
  $("#set-voice").addEventListener("change", (e) => { S.voiceId = e.target.value; store.set("voice", S.voiceId); });
  $("#btn-test-voice").addEventListener("click", () => { ensureAudio(); say("Good to go, sir. This is how I sound."); });
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
  const Settings = {
    loaded: false, sections: [], edited: {}, cleared: new Set(), open: new Set(), advanced: new Set(), testing: new Set(),

    async load() {
      try {
        const data = await (await api("/api/settings")).json();
        this.loaded = true;
        this.sections = data.sections;
        if (data.context?.staff_report_link) $("#report-link").textContent = data.context.staff_report_link.replace(/^https?:\/\//, "");
        this.render(data.problem);
      } catch { toast("Couldn't load settings", "Check the connection and try again.", "warning"); }
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
      $("#settings-sections").innerHTML = this.sections.map((s) => this.renderSection(s)).join("");
      this.updateSaveBar();
    },

    badge(sec) {
      if (!sec.show_badge) return "";
      const test = sec.last_test;
      if (test && !test.stale) return test.ok ? `<span class="set-badge on">Working</span>` : `<span class="set-badge fail">Test failed</span>`;
      if (sec.configured) return `<span class="set-badge on">Connected</span>`;
      return `<span class="set-badge off">Not set up</span>`;
    },

    renderSection(sec) {
      const isOpen = this.open.has(sec.id);
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
        <div class="set-section${isOpen ? " open" : ""}" data-section="${esc(sec.id)}">
          <div class="set-section-head" data-toggle="${esc(sec.id)}">
            <div class="set-section-titles"><h3>${esc(sec.title)}</h3><div class="blurb">${esc(sec.blurb)}</div></div>
            ${this.badge(sec)}
            <span class="set-chevron">▸</span>
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
            <input type="password" id="f-${f.key}" data-field="${f.key}" placeholder="${f.is_set ? "Leave blank to keep the current one" : esc(f.placeholder || "")}" autocomplete="new-password">
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
        control = `<input type="${type}"${step} id="f-${f.key}" data-field="${f.key}" value="${esc(current)}" placeholder="${esc(f.placeholder || "")}">`;
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
    const toggle = e.target.closest("[data-toggle]");
    if (toggle) { const id = toggle.dataset.toggle; Settings.open.has(id) ? Settings.open.delete(id) : Settings.open.add(id); Settings.render(); return; }
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
