/* JARVIS HUD - live display, conversation, voice in/out. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const store = {
    get(k, d) { try { const v = localStorage.getItem("jarvis." + k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("jarvis." + k, v); } catch { /* private mode */ } },
  };
  const S = {
    status: null, voice: { tts: "browser", stt: "browser", wake_word: "jarvis", language: "en-GB" },
    ws: null, approvals: [], hudState: "idle", level: 0, targetLevel: 0,
    listenMode: store.get("listen", "ptt"), speakPref: store.get("speak", "voice"), voiceId: store.get("voice", ""),
    lastMode: "typed", followUpUntil: 0, attachments: [], greeted: false,
  };

  // ------------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => (n === null || n === undefined || isNaN(n)) ? "-" : "£" + Math.round(n).toLocaleString("en-GB");
  const time = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); };
  const dayMonth = (iso) => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }); };

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
  function setHud(state) { S.hudState = state; $("#state").textContent = STATE_LABEL[state] || state; }
  function caption(text, interim = "") { $("#caption").innerHTML = esc(text) + (interim ? ` <span class="interim">${esc(interim)}</span>` : ""); }

  const canvas = $("#reactor");
  const ctx = canvas.getContext("2d");
  function drawReactor(t) {
    const w = canvas.width, h = canvas.height, cx = w / 2, cy = h / 2;
    S.level += (S.targetLevel - S.level) * 0.25;
    if (S.hudState === "speaking" && speaker.browserSpeaking) S.targetLevel = 0.35 + 0.3 * Math.abs(Math.sin(t / 90));
    const colour = { idle: "38,217,255", listening: "61,220,151", thinking: "255,176,32", speaking: "38,217,255", awaiting: "61,220,151" }[S.hudState] || "38,217,255";
    const lvl = S.level;
    ctx.clearRect(0, 0, w, h);
    const glow = ctx.createRadialGradient(cx, cy, 10, cx, cy, w / 2);
    glow.addColorStop(0, `rgba(${colour},${0.35 + lvl * 0.5})`); glow.addColorStop(0.35, `rgba(${colour},0.08)`); glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow; ctx.fillRect(0, 0, w, h);
    const speed = S.hudState === "thinking" ? 3 : 1;
    const rings = [[150, 2, 0.0004, [40, 12]], [128, 6, -0.0007, [3, 9]], [108, 1.5, 0.001, [80, 20]], [88, 10, -0.0005, [14, 6]]];
    rings.forEach(([r, width, spin, dash], k) => {
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(t * spin * speed);
      ctx.setLineDash(dash); ctx.lineWidth = width;
      ctx.strokeStyle = `rgba(${colour},${0.25 + 0.2 * k / 3 + lvl * 0.4})`;
      ctx.beginPath(); ctx.arc(0, 0, r + (k === 3 ? lvl * 10 : 0), 0, Math.PI * 2); ctx.stroke(); ctx.restore();
    });
    ctx.save(); ctx.translate(cx, cy);
    for (let i = 0; i < 60; i++) {
      const a = (i / 60) * Math.PI * 2 + t * 0.0002;
      const len = i % 5 === 0 ? 10 : 4;
      ctx.strokeStyle = `rgba(${colour},0.45)`; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(Math.cos(a) * 160, Math.sin(a) * 160); ctx.lineTo(Math.cos(a) * (160 - len), Math.sin(a) * (160 - len)); ctx.stroke();
    }
    ctx.restore();
    const core = 34 + lvl * 26 + Math.sin(t / 600) * 2;
    const g = ctx.createRadialGradient(cx, cy, 2, cx, cy, core);
    g.addColorStop(0, "rgba(255,255,255,0.95)"); g.addColorStop(0.4, `rgba(${colour},0.9)`); g.addColorStop(1, `rgba(${colour},0)`);
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
  ["click", "keydown", "touchstart"].forEach((ev) => window.addEventListener(ev, () => { ensureAudio(); greet(); }, { once: false, passive: true }));

  function pickBrowserVoice() {
    const voices = speechSynthesis.getVoices();
    const prefs = ["Daniel", "Google UK English Male", "Microsoft Ryan", "Arthur", "George", "Oliver"];
    for (const p of prefs) { const v = voices.find((x) => x.name.includes(p)); if (v) return v; }
    return voices.find((v) => v.lang === "en-GB") || null;
  }

  const speaker = {
    queue: [], buffer: "", active: false, browserSpeaking: false, onIdle: null,
    feed(delta) { this.buffer += delta; const parts = this.buffer.split(/(?<=[.!?…:])\s+(?=[A-Z0-9"'£(])/); this.buffer = parts.pop(); parts.forEach((p) => this.enqueue(p)); },
    flush() { if (this.buffer.trim()) this.enqueue(this.buffer); this.buffer = ""; },
    enqueue(sentence) {
      const clean = sentence.replace(/```[\s\S]*?```/g, " ").replace(/[#*_`>|]/g, " ").replace(/\s+/g, " ").trim();
      if (!clean || /^[-\s]+$/.test(clean)) return;
      const item = { text: clean, audio: S.voice.tts !== "browser" ? this.fetchAudio(clean) : null };
      this.queue.push(item);
      if (!this.active) this.next();
    },
    async fetchAudio(text) {
      try {
        const r = await api("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, voice_id: S.voiceId || null }) });
        if (!r.ok) return null;
        return URL.createObjectURL(await r.blob());
      } catch { return null; }
    },
    async next() {
      const item = this.queue.shift();
      if (!item) { this.active = false; this.browserSpeaking = false; if (S.hudState === "speaking") setHud("idle"); S.followUpUntil = Date.now() + 8000; if (this.onIdle) this.onIdle(); return; }
      this.active = true; setHud("speaking");
      const url = item.audio ? await item.audio : null;
      if (!this.active) return;
      if (url) {
        player.src = url;
        player.onended = () => { URL.revokeObjectURL(url); this.next(); };
        player.onerror = () => this.next();
        try { await player.play(); } catch { this.speakBrowser(item.text); }
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
    stop() { this.queue = []; this.buffer = ""; this.active = false; this.browserSpeaking = false; player.pause(); if ("speechSynthesis" in window) speechSynthesis.cancel(); setHud("idle"); },
  };
  const shouldSpeak = (mode) => S.speakPref === "always" || (S.speakPref === "voice" && mode === "voice");

  function say(text) { if (S.speakPref !== "off") { speaker.feed(text + " "); speaker.flush(); } }

  function greet() {
    if (S.greeted || !S.status) return;
    S.greeted = true;
    const h = new Date().getHours();
    const part = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
    const st = S.status;
    const bits = [];
    const unread = st.inbox?.unread?.length || 0;
    if (unread) bits.push(`${unread} unread email${unread > 1 ? "s" : ""}`);
    const overdue = st.overdue_jobs?.length || 0;
    if (overdue) bits.push(`${overdue} overdue job${overdue > 1 ? "s" : ""}`);
    const issues = st.issues?.length || 0;
    if (issues) bits.push(`${issues} open issue${issues > 1 ? "s" : ""}`);
    if (S.approvals.length) bits.push(`${S.approvals.length} thing${S.approvals.length > 1 ? "s" : ""} waiting for your approval`);
    const failing = (st.tests || []).filter((t) => !t.ok).length;
    const line = `${part}, ${st.owner || "sir"}. ` + (bits.length ? `You have ${bits.join(", ")}.` : "All quiet on every front.") +
      (failing ? ` ${failing} routine check${failing > 1 ? "s are" : " is"} failing - details on the left.` : " All systems are running normally.");
    caption(line);
    if (store.get("greet", "1") === "1" && S.speakPref !== "off") say(line);
  }

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

  function send(text, mode = "typed") {
    text = text.trim();
    if (!text && !S.attachments.length) return;
    S.lastMode = mode;
    speaker.stop();
    const payload = { type: "chat", text: text || "Please look at the attached file(s).", mode, attachments: S.attachments };
    S.attachments = []; renderAttachments();
    if (S.ws && S.ws.readyState === 1) { S.ws.send(JSON.stringify(payload)); return; }
    addMessage("user", payload.text);
    setHud("thinking");
    api("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then((r) => r.json()).then((d) => { addMessage("assistant", d.reply); if (shouldSpeak(mode)) say(d.reply); else setHud("idle"); })
      .catch(() => { toast("Couldn't reach Jarvis", "Check the connection.", "warning"); setHud("idle"); });
  }

  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); send($("#input").value, "typed"); $("#input").value = ""; autosize(); });
  const autosize = () => { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; };
  $("#input").addEventListener("input", autosize);
  $("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#quick").addEventListener("click", (e) => { const q = e.target.closest("[data-q]"); if (q) send(q.dataset.q, "typed"); });
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
        if (!speaker.active) setHud("idle");
        caption(d.text.replace(/[#*_`|]/g, "").slice(0, 180) + (d.text.length > 180 ? "…" : ""));
        current = null;
        refreshSoon();
        break;
      case "error":
        if (current) current.remove();
        current = null;
        addMessage("assistant", d.message).classList.add("error");
        setHud("idle"); $("#toolline").textContent = "";
        break;
      case "notification":
        toast(d.title, d.body, d.level);
        if (d.speak && !speaker.active && S.hudState === "idle" && S.speakPref !== "off") say(`Sir, ${d.title}.`);
        refreshSoon();
        break;
      case "owner_update":
        toast("Update sent", `${d.subject} → ${d.channels.join(", ") || "display"}`);
        break;
      case "display": openDisplay(d.title, d.markdown); break;
      case "approvals": S.approvals = d; renderApprovals(); break;
      case "issue": refreshSoon(); break;
      case "tests": renderTests(d); break;
      case "map": renderMap(d); break;
      case "conversation_reset": $("#conversation").innerHTML = ""; caption("Fresh start. What can I do for you?"); break;
    }
  }

  // ------------------------------------------------------------------ display overlay
  function openDisplay(title, markdown) {
    $("#display-title").textContent = title;
    $("#display-body").innerHTML = md(markdown);
    $("#display").classList.add("open");
  }
  $("#display-close").addEventListener("click", () => $("#display").classList.remove("open"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#display").classList.remove("open"); $("#drawer").classList.remove("open"); } });

  // ------------------------------------------------------------------ panels
  async function refresh() {
    try {
      const st = await (await api("/api/status")).json();
      S.status = st; S.voice = st.voice || S.voice; S.approvals = st.approvals || [];
      $("#company").textContent = (st.company || "").toUpperCase();
      renderPills(st.connections); renderInbox(st.inbox); renderIssues(st.issues); renderTests(st.tests);
      renderNotifications(st.notifications); renderOps(st.staff, st.overdue_jobs); renderFinance(st.finance);
      renderPresence(st.presence); renderDeadlines(st.deadlines, st.accreditations); renderApprovals(); renderSettings(st);
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
  }
  $("#approvals").addEventListener("click", (e) => { const b = e.target.closest("[data-act]"); if (b) decide(b.dataset.id, b.dataset.act); });
  async function decide(id, act) {
    const r = await (await api(`/api/approvals/${id}/${act}`, { method: "POST" })).json();
    toast(act === "approve" ? "Approved" : "Cancelled", r.result);
    say(act === "approve" ? "Very good, sir. On it." : "Cancelled.");
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
    on: false, stream: null, rec: null, ws: null, finals: "", recognition: null, chunks: [],
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
          if (e.results[i].isFinal) { this.finals += e.results[i][0].transcript + " "; if (S.listenMode === "wake") { utterance(this.finals); this.finals = ""; } }
          else interim += e.results[i][0].transcript;
        }
        caption(this.finals, interim);
      };
      r.onend = () => { if (this.on && S.listenMode === "wake") r.start(); else if (this.on) this.stop(true); };
      r.start();
    },
    onDeepgram(m) {
      if (m.type === "transcript") {
        if (m.is_final && m.text) this.finals += m.text + " ";
        caption(this.finals, m.is_final ? "" : m.text);
        if (m.speech_final && S.listenMode === "wake" && this.finals.trim()) { utterance(this.finals); this.finals = ""; }
      } else if (m.type === "utterance_end" && S.listenMode === "wake" && this.finals.trim()) { utterance(this.finals); this.finals = ""; }
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

  function utterance(raw) {
    const text = String(raw || "").trim();
    if (!text) return;
    const lower = text.toLowerCase();
    const wake = (S.voice.wake_word || "jarvis").toLowerCase();
    if (speaker.active) {
      if (/\b(stop|quiet|enough|cancel|shut up)\b/.test(lower) || lower.includes(wake)) speaker.stop();
      else return; // ignore our own voice echoing back
    }
    const bare = lower.replace(new RegExp(`^\\s*(hey\\s+)?${wake}[\\s,.!?]*`), "").trim();
    if (S.approvals.length && /^(approve|approved|confirm|confirmed|go ahead|yes,? (do it|send it|deploy it)|send it|deploy it)\b/.test(bare)) {
      if (S.approvals.length === 1) decide(S.approvals[0].id, "approve");
      else say(`There are ${S.approvals.length} approvals waiting, sir - please tap the one you mean.`);
      return;
    }
    if (S.approvals.length === 1 && /^(deny|cancel|don't|do not|no,? (cancel|don't))\b/.test(bare)) { decide(S.approvals[0].id, "deny"); return; }
    if (S.listenMode === "wake") {
      const idx = lower.indexOf(wake);
      if (idx === -1 && Date.now() > S.followUpUntil) { caption("", `(heard: "${text.slice(0, 60)}")`); return; }
      const cmd = idx >= 0 ? text.slice(idx + wake.length).replace(/^[\s,.!?]+/, "") : text;
      if (!cmd) { setHud("awaiting"); S.followUpUntil = Date.now() + 8000; say("Yes, sir?"); return; }
      send(cmd, "voice");
    } else send(text, "voice");
  }

  mic.addEventListener("click", () => {
    if (stt.on) stt.stop(true); else stt.start();
  });
  let spaceHeld = false;
  window.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement?.tagName) || S.listenMode === "wake") return;
    e.preventDefault(); spaceHeld = true; stt.start();
  });
  window.addEventListener("keyup", (e) => { if (e.code === "Space" && spaceHeld) { spaceHeld = false; stt.stop(true); } });

  // ------------------------------------------------------------------ settings
  $("#btn-settings").addEventListener("click", () => $("#drawer").classList.add("open"));
  $("#drawer-close").addEventListener("click", () => $("#drawer").classList.remove("open"));
  $("#set-listen").value = S.listenMode; $("#set-speak").value = S.speakPref;
  $("#set-listen").addEventListener("change", (e) => {
    S.listenMode = e.target.value; store.set("listen", S.listenMode);
    if (S.listenMode === "wake") { stt.stop(false); stt.start(); toast("Always listening", `Say "${S.voice.wake_word}…" to talk to me.`); } else stt.stop(false);
  });
  $("#set-speak").addEventListener("change", (e) => { S.speakPref = e.target.value; store.set("speak", S.speakPref); if (S.speakPref === "off") speaker.stop(); });
  $("#set-voice").addEventListener("change", (e) => { S.voiceId = e.target.value; store.set("voice", S.voiceId); });
  $("#btn-test-voice").addEventListener("click", () => { ensureAudio(); say("At your service, sir. This is how I sound."); });
  let voicesLoaded = false;
  async function renderSettings(st) {
    $("#connections").innerHTML = Object.entries(st.connections || {}).map(([k, v]) => `<div class="conn"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
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

  // ------------------------------------------------------------------ boot
  (async () => {
    await refresh();
    await loadTranscript();
    connect();
    refreshMap();
    setInterval(refresh, 60000);
    setInterval(refreshMap, 60000);
    if (S.listenMode === "wake") toast("Always-listening mode", "Tap anywhere to enable the microphone and voice.");
    window.addEventListener("click", () => { if (S.listenMode === "wake" && !stt.on) stt.start(); }, { once: true });
  })();
})();
