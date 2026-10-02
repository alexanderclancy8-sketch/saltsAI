/* JARVIS core - the canvas animation at the heart of the console and the sign-in page.
 *
 * Its colour and energy follow the agent's state: calm cyan when idle, green while listening, amber while working,
 * brighter cyan while speaking. Colours come from the CSS custom properties (--core-rgb, --ok-rgb, --warn-rgb,
 * --core-hi-rgb, --track-rgb) so both themes and any later retune of the tokens apply without touching this file.
 * Under prefers-reduced-motion nothing animates: a single static frame is drawn and redrawn only when the state or
 * the theme changes. */
(() => {
  "use strict";
  const TAU = Math.PI * 2;
  // tok: which colour token; energy: 0..1 base brightness; spin: ring rotation speed multiplier.
  const STATES = {
    idle: { tok: "core", energy: 0.28, spin: 0.6 },
    listening: { tok: "ok", energy: 0.62, spin: 1.0 },
    thinking: { tok: "warn", energy: 0.8, spin: 2.4 },
    speaking: { tok: "core", energy: 0.72, spin: 1.2 },
    awaiting: { tok: "ok", energy: 0.5, spin: 0.8 },
  };
  const FALLBACK = { core: "34,211,238", ok: "52,211,153", warn: "251,191,36", hi: "255,255,255", track: "255,255,255" };

  function readTokens() {
    const cs = getComputedStyle(document.documentElement);
    const get = (name, fb) => (cs.getPropertyValue(name).trim() || fb);
    return {
      core: get("--core-rgb", FALLBACK.core), ok: get("--ok-rgb", FALLBACK.ok), warn: get("--warn-rgb", FALLBACK.warn),
      hi: get("--core-hi-rgb", FALLBACK.hi), track: get("--track-rgb", FALLBACK.track),
    };
  }

  function draw(ctx, w, h, t, name, level, tokens) {
    const st = STATES[name] || STATES.idle;
    const rgb = tokens[st.tok];
    const cx = w / 2, cy = h / 2, u = w / 340;           // everything is drawn for a 340px square and scaled
    const e = Math.min(1, st.energy + level * 0.35);
    const breathe = 0.5 + 0.5 * Math.sin(t / (name === "thinking" ? 260 : 900));
    ctx.clearRect(0, 0, w, h);
    ctx.save(); ctx.translate(cx, cy); ctx.scale(u, u);

    // soft halo behind everything
    const halo = ctx.createRadialGradient(0, 0, 20, 0, 0, 170);
    halo.addColorStop(0, `rgba(${rgb},${0.10 + e * 0.16})`); halo.addColorStop(1, `rgba(${rgb},0)`);
    ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(0, 0, 170, 0, TAU); ctx.fill();

    // outer track and the progress arc whose length is the energy
    ctx.lineCap = "round"; ctx.lineWidth = 3;
    ctx.strokeStyle = `rgba(${tokens.track},0.12)`;
    ctx.beginPath(); ctx.arc(0, 0, 148, 0, TAU); ctx.stroke();
    ctx.strokeStyle = `rgba(${rgb},${0.55 + e * 0.4})`;
    ctx.beginPath(); ctx.arc(0, 0, 148, -Math.PI / 2 + t * 0.0004 * st.spin, -Math.PI / 2 + t * 0.0004 * st.spin + (0.16 + e * 0.8) * TAU); ctx.stroke();

    // slow dashed tick ring, and a counter-rotating segmented ring that wakes up with energy
    ctx.save(); ctx.rotate(t * 0.00028 * st.spin);
    ctx.setLineDash([2, 15]); ctx.lineWidth = 1.6; ctx.strokeStyle = `rgba(${rgb},0.38)`;
    ctx.beginPath(); ctx.arc(0, 0, 124, 0, TAU); ctx.stroke(); ctx.restore();
    ctx.save(); ctx.rotate(-t * 0.0005 * st.spin);
    ctx.setLineDash([46, 38]); ctx.lineWidth = 2; ctx.strokeStyle = `rgba(${rgb},${0.18 + e * 0.5})`;
    ctx.beginPath(); ctx.arc(0, 0, 100, 0, TAU); ctx.stroke(); ctx.restore();
    ctx.setLineDash([]);

    // expanding pulse rings while there is something going on
    if (st.energy > 0.5) {
      for (let i = 0; i < 2; i++) {
        const p = ((t / 1800) + i * 0.5) % 1;
        ctx.strokeStyle = `rgba(${rgb},${(1 - p) * 0.35})`; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(0, 0, 44 + p * 70, 0, TAU); ctx.stroke();
      }
    }

    // the core itself
    const core = 34 + e * 18 + breathe * 4 * (1 - level);
    const g = ctx.createRadialGradient(0, 0, 2, 0, 0, core);
    g.addColorStop(0, `rgba(${tokens.hi},0.95)`); g.addColorStop(0.45, `rgba(${rgb},0.85)`); g.addColorStop(1, `rgba(${rgb},0)`);
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, core, 0, TAU); ctx.fill();
    ctx.restore();
  }

  // mount(canvas, { frame: (t) => ({ state, level }), reduced: () => boolean }) -> { redraw() }
  function mount(canvas, opts = {}) {
    const ctx = canvas.getContext("2d");
    const mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    const reduced = opts.reduced || (() => !!(mq && mq.matches));
    const frame = opts.frame || (() => ({ state: "idle", level: 0 }));
    let tokens = readTokens(), lastKey = "";
    window.addEventListener("jarvis-theme", () => { tokens = readTokens(); lastKey = ""; });
    if (mq && mq.addEventListener) mq.addEventListener("change", () => { lastKey = ""; });
    function loop(t) {
      const f = frame(t) || {};
      const still = reduced();
      const key = still ? `${f.state}|${canvas.width}|${tokens.core}|${tokens.hi}` : "";
      if (!still || key !== lastKey) {
        draw(ctx, canvas.width, canvas.height, still ? 0 : t, f.state, still ? 0 : (f.level || 0), tokens);
        lastKey = key;
      }
      requestAnimationFrame(loop);
    }
    requestAnimationFrame(loop);
    return { redraw() { lastKey = ""; tokens = readTokens(); } };
  }

  window.JarvisCore = { mount, draw, STATES, readTokens };
})();
