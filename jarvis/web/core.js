/* JARVIS core - the canvas animation at the heart of the console and the sign-in page.
 *
 * Ported from the design mockup (docs/redesign/jarvis-console-mockup.html): a soft glow, a waveform ring whose energy
 * follows the agent's state, and three rotating arc rings. Colour and energy follow the real state: calm dim cyan when
 * idle ("standby"), bright cyan while listening, amber while working, green while speaking. Colours come from the CSS
 * custom properties (--core-dim-rgb, --core-rgb, --ember-rgb, --ok-rgb) so both themes and any later retune of the
 * tokens apply without touching this file. Under prefers-reduced-motion nothing animates: a single static frame is
 * drawn and redrawn only when the state or the theme changes. */
(() => {
  "use strict";
  // tok: which colour token; energy: how lively the waveform ring is (0..1); fast: the arcs spin 3x faster (working).
  const STATES = {
    idle: { tok: "dim", energy: 0.15, fast: false },
    listening: { tok: "core", energy: 0.6, fast: false },
    awaiting: { tok: "core", energy: 0.45, fast: false },
    thinking: { tok: "ember", energy: 0.35, fast: true },
    speaking: { tok: "ok", energy: 1, fast: false },
  };
  const FALLBACK = { dim: "28,111,143", core: "79,216,255", ember: "255,171,74", ok: "95,224,168" };

  function readTokens() {
    const cs = getComputedStyle(document.documentElement);
    const get = (name, fb) => (cs.getPropertyValue(name).trim() || fb);
    return { dim: get("--core-dim-rgb", FALLBACK.dim), core: get("--core-rgb", FALLBACK.core), ember: get("--ember-rgb", FALLBACK.ember), ok: get("--ok-rgb", FALLBACK.ok) };
  }

  // t is in seconds; level (0..1) is the live audio level, which adds to the state's base energy.
  function draw(ctx, w, t, name, level, tokens) {
    const st = STATES[name] || STATES.idle;
    const rgb = tokens[st.tok];
    const r = w / 2;
    const energy = Math.min(1, st.energy + level * 0.4);
    ctx.clearRect(0, 0, w, w); ctx.save(); ctx.translate(r, r);
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, r * 0.5);
    g.addColorStop(0, `rgb(${rgb})`); g.addColorStop(1, `rgba(${rgb},0)`);
    ctx.globalAlpha = 0.25 + 0.15 * Math.sin(t * 2) * energy; ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, r * 0.5, 0, 7); ctx.fill();
    ctx.globalAlpha = 1; ctx.strokeStyle = `rgb(${rgb})`; ctx.lineCap = "round"; ctx.lineWidth = w * 0.008; ctx.beginPath();
    for (let i = 0; i <= 120; i++) {
      const a = i / 120 * Math.PI * 2;
      const rr = r * 0.36 + r * 0.07 * energy * Math.sin(a * 6 + t * 5) * Math.sin(a * 3 - t * 3);
      const x = Math.cos(a) * rr, y = Math.sin(a) * rr;
      if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    }
    ctx.closePath(); ctx.stroke();
    const sp = st.fast ? 3 : 1;
    [[0.58, 0.012, 1, 0.9, 3], [0.72, 0.006, -0.6, 0.5, 5], [0.88, 0.004, 0.35, 0.35, 2]].forEach((p) => {
      ctx.lineWidth = w * p[1]; ctx.globalAlpha = p[3];
      const n = p[4];
      for (let k = 0; k < n; k++) {
        const s = t * p[2] * sp + k * Math.PI * 2 / n;
        ctx.beginPath(); ctx.arc(0, 0, r * p[0], s, s + Math.PI * 2 / n * 0.7); ctx.stroke();
      }
    });
    ctx.restore();
  }

  // mount(canvas, { frame: (ms) => ({ state, level }), reduced: () => boolean }) -> { redraw() }
  function mount(canvas, opts = {}) {
    const ctx = canvas.getContext("2d");
    const mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    const reduced = opts.reduced || (() => !!(mq && mq.matches));
    const frame = opts.frame || (() => ({ state: "idle", level: 0 }));
    let tokens = readTokens(), lastKey = "";
    window.addEventListener("jarvis-theme", () => { tokens = readTokens(); lastKey = ""; });
    if (mq && mq.addEventListener) mq.addEventListener("change", () => { lastKey = ""; });
    function loop(ms) {
      const f = frame(ms) || {};
      const still = reduced();
      const key = still ? `${f.state}|${canvas.width}|${tokens.core}|${tokens.ok}` : "";
      if (!still || key !== lastKey) {
        draw(ctx, canvas.width, still ? 0 : ms / 1000, f.state, still ? 0 : (f.level || 0), tokens);
        lastKey = key;
      }
      requestAnimationFrame(loop);
    }
    requestAnimationFrame(loop);
    return { redraw() { lastKey = ""; tokens = readTokens(); } };
  }

  window.JarvisCore = { mount, draw, STATES, readTokens };
})();
