/* JARVIS HUD - question pop-up (the `ask_user` tool).
 *
 * A centred pop-up over a dimmed backdrop: "Jarvis is asking", a short question, 2-4 answers as real <button>s (click,
 * or keys 1-4 / arrows + Enter, each with an optional one-line description and one optionally marked "Recommended"),
 * and an always-present "Type my own answer" button that opens a text box. Escape, Dismiss or a click on the backdrop
 * closes it without sending anything. Multi-select shows toggles plus a Send button. Whatever is chosen is sent back as an ordinary chat
 * message - exactly as if it had been typed - through the `send` hook hud.js hands to init().
 *
 * This is deliberately NOT part of the approval mechanism: nothing here touches the Approve/Cancel buttons or the
 * approvals endpoints, and it makes no network calls of its own. An answer is just text in the conversation.
 *
 * Kept in its own file so changes to hud.js stay to a handful of hook lines (the "ask" event case, close() on a new
 * user message, and spokenReply() inside send()). Loaded before hud.js; exposes window.JarvisAsk.
 */
(() => {
  "use strict";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const NUMBER_WORDS = ["one", "two", "three", "four", "five"];
  let host = null;      // { send(text, mode, opts), say(text), speakNow(), mode() } - supplied by hud.js
  let state = null;     // { id, question, options, allow_multiple, mode, picked:Set, otherOpen }
  let el = null;
  let scrim = null;     // the dimmed backdrop; a real element so it takes clicks (a click on it dismisses)
  let last = null;      // the question as sent, kept until it is answered so an "Answer" button can bring it back
  let opener = null;    // what had focus before the pop-up opened, so closing without an answer can give it back

  // ---------------------------------------------------------------- spoken choice (voice sessions)
  const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9\s]+/g, " ").split(/\s+/).filter(Boolean).join(" ");
  const ORDINALS = { one: 0, first: 0, "1": 0, two: 1, second: 1, "2": 1, three: 2, third: 2, "3": 2, four: 3, fourth: 3, "4": 3 };
  const isOrd = (w) => Object.prototype.hasOwnProperty.call(ORDINALS, w);
  const FILLER = new Set(["option", "options", "number", "choice", "the", "a", "one", "ones", "and", "please", "go", "with",
    "pick", "choose", "take", "ill", "i", "ll", "will", "id", "like", "want", "both", "plus", "also", "then", "that"]);
  const NEGATIONS = new Set(["not", "no", "dont", "don", "except", "neither", "nor", "without", "never"]);
  const MAX_SPOKEN_CHOICE_WORDS = 12;

  // Map something said aloud to the option(s) it names. Anything that isn't clearly a choice - long speech, a
  // negation, unmatched words, "something else" - comes back unchanged as free speech, i.e. the "Other" answer.
  function spokenReply(text) {
    if (!state) return text;
    const t = norm(text);
    const words = t.split(" ").filter(Boolean);
    if (!words.length || words.length > MAX_SPOKEN_CHOICE_WORDS || words.some((w) => NEGATIONS.has(w))) return text;
    const opts = state.options;
    const hits = new Set();
    opts.forEach((o, i) => { const l = norm(o.label); if (l && (t === l || ` ${t} `.includes(` ${l} `))) hits.add(i); });
    const isRec = (w) => /^(recommended|recommendation|suggested)$/.test(w);
    if (!hits.size && words.every((w) => FILLER.has(w) || isOrd(w) || isRec(w) || w === "your")) {
      if (words.some(isRec)) { const r = opts.findIndex((o) => o.recommended); if (r >= 0) hits.add(r); }
      else for (const w of words) if (isOrd(w) && ORDINALS[w] < opts.length) hits.add(ORDINALS[w]);
    }
    if (!hits.size || (hits.size > 1 && !state.allow_multiple)) return text;
    return [...hits].sort((a, b) => a - b).map((i) => opts[i].label).join(", ");
  }

  // The brief version read aloud in a voice session: question, numbered labels (no descriptions), and the way out.
  function speechFor(s) {
    const parts = [s.question.replace(/\s*[?]*\s*$/, "") + "?"];
    s.options.forEach((o, i) => parts.push(`Option ${NUMBER_WORDS[i] || i + 1}: ${o.label}${o.recommended ? ", which I'd recommend" : ""}.`));
    parts.push(s.allow_multiple ? "You can pick more than one, or say something else." : "Or say something else.");
    return parts.join(" ");
  }

  // ---------------------------------------------------------------- rendering
  function ensureEl() {
    if (el) return el;
    scrim = document.createElement("div");
    scrim.id = "ask-scrim"; scrim.className = "ask-scrim"; scrim.hidden = true;
    scrim.addEventListener("click", () => dismiss());
    document.body.appendChild(scrim);
    el = document.createElement("div");
    el.id = "ask"; el.className = "ask"; el.hidden = true;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.addEventListener("click", onClick);
    el.addEventListener("keydown", onKey);
    document.body.appendChild(el);
    return el;
  }

  const optButtons = () => Array.from(el.querySelectorAll(".ask-opt"));

  function render() {
    const s = state, multi = s.allow_multiple;
    el.setAttribute("aria-labelledby", "ask-q");
    el.innerHTML = `<div class="ask-title">Jarvis is asking</div>
      <div class="ask-q" id="ask-q">${esc(s.question)}</div>
      ${multi ? `<div class="ask-hint">Choose any that apply, then send.</div>` : ""}
      <div class="ask-opts">
        ${s.options.map((o, i) => `<button type="button" class="ask-opt" data-i="${i}"${multi ? ` aria-pressed="false"` : ""}>
          <kbd>${i + 1}</kbd><span class="ask-text"><span class="ask-label">${esc(o.label)}${o.recommended ? ` <span class="ask-rec">Recommended</span>` : ""}</span>${o.description ? `<span class="ask-desc">${esc(o.description)}</span>` : ""}</span></button>`).join("")}
        <button type="button" class="ask-opt ask-other" data-other="1"${multi ? ` aria-pressed="false"` : ` aria-expanded="false"`}>
          <kbd>${s.options.length + 1}</kbd><span class="ask-text"><span class="ask-label">Type my own answer</span></span></button>
      </div>
      <div class="ask-otherbox" hidden><textarea rows="2" maxlength="2000" placeholder="Type your own answer… (Enter to send)" aria-label="Your own answer"></textarea></div>
      <div class="ask-actions"><button type="button" class="btn go small ask-send"${multi ? "" : " hidden"}>Send</button><button type="button" class="btn small ask-dismiss">Dismiss</button></div>`;
  }

  function show(ev) {
    if (!ev || !Array.isArray(ev.options) || ev.options.length < 2) return;
    close();
    ensureEl();
    last = ev;
    state = { id: ev.id, question: String(ev.question || ""), allow_multiple: !!ev.allow_multiple, mode: host ? host.mode() : "typed",
      options: ev.options.slice(0, 4).map((o) => ({ label: String(o.label || ""), description: String(o.description || ""), recommended: !!o.recommended })),
      picked: new Set(), otherOpen: false };
    opener = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    render();
    scrim.hidden = false; el.hidden = false;
    // Move focus to the recommended (else first) option so the keyboard works straight away - but never steal it
    // from a half-typed message in the chat box.
    const active = document.activeElement;
    const typing = active && ["TEXTAREA", "INPUT"].includes(active.tagName) && active.value;
    if (!typing) { const rec = state.options.findIndex((o) => o.recommended); optButtons()[rec >= 0 ? rec : 0]?.focus({ preventScroll: true }); }
    // Voice session: read the question and options aloud, briefly. Typed sessions stay silent.
    if (host && host.speakNow()) host.say(speechFor(state));
  }

  function close() {
    state = null;
    if (el) { el.hidden = true; el.innerHTML = ""; }
    if (scrim) scrim.hidden = true;
  }

  // Closing WITHOUT answering (Escape, Dismiss, the backdrop): nothing is sent. Focus goes back to where it was.
  function dismiss() {
    if (!state) return;
    const back = opener;
    close();
    const target = back && back.focus && document.contains?.(back) !== false ? back : document.getElementById("input");
    target?.focus?.({ preventScroll: true });
  }

  // ---------------------------------------------------------------- answering
  function submit(text) {
    text = String(text || "").trim();
    if (!state || !text) return;
    const mode = state.mode;
    close();
    last = null; // answered
    if (host) host.send(text, mode, { ask: true }); // a plain chat message - never an approval
    // Hand the keyboard back to the chat box once the answer has gone.
    document.getElementById("input")?.focus({ preventScroll: true });
  }

  function answerText() {
    const parts = [...state.picked].sort((a, b) => a - b).map((i) => state.options[i].label);
    const other = el.querySelector(".ask-otherbox textarea")?.value.trim();
    if (state.otherOpen && other) parts.push(other);
    return parts.join(", ");
  }

  function toggleOther(open) {
    state.otherOpen = open;
    const box = el.querySelector(".ask-otherbox"), btn = el.querySelector(".ask-other");
    box.hidden = !open;
    if (state.allow_multiple) btn.setAttribute("aria-pressed", String(open)); else btn.setAttribute("aria-expanded", String(open));
    btn.classList.toggle("on", open);
    // Single-select: "Other" is answered from the text box, so it needs its own Send. Multi-select always has one.
    el.querySelector(".ask-send").hidden = !(open || state.allow_multiple);
    if (open) box.querySelector("textarea").focus({ preventScroll: true });
  }

  function choose(btn) {
    if (!state || !btn) return;
    const multi = state.allow_multiple;
    if (btn.dataset.other) { toggleOther(!state.otherOpen); return; }
    const i = Number(btn.dataset.i);
    if (!multi) { submit(state.options[i].label); return; }
    if (state.picked.has(i)) state.picked.delete(i); else state.picked.add(i);
    btn.setAttribute("aria-pressed", String(state.picked.has(i)));
    btn.classList.toggle("on", state.picked.has(i));
  }

  function onClick(e) {
    if (!state) return;
    if (e.target.closest(".ask-dismiss")) { dismiss(); return; }
    if (e.target.closest(".ask-send")) { submit(answerText()); return; }
    const b = e.target.closest(".ask-opt");
    if (b) choose(b);
  }

  function onKey(e) {
    if (!state) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); dismiss(); return; }
    if (e.key === "Tab") { trapTab(e); return; }
    if (e.target.tagName === "TEXTAREA") {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); e.stopPropagation(); submit(answerText()); }
      return; // everything else is just typing - digits included
    }
    // Space/Enter on a focused option is the native button click; keep hud.js's hold-Space-to-talk out of it.
    if (e.key === " " || e.key === "Enter") { e.stopPropagation(); return; }
    const btns = optButtons();
    const at = btns.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); btns[(at + 1) % btns.length]?.focus(); }
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); btns[(at - 1 + btns.length) % btns.length]?.focus(); }
    else if (/^[1-9]$/.test(e.key) && btns[Number(e.key) - 1]) { e.preventDefault(); choose(btns[Number(e.key) - 1]); }
  }

  // Keep Tab inside the pop-up while it is open (it is modal).
  function trapTab(e) {
    const items = Array.from(el.querySelectorAll("button, textarea")).filter((x) => !x.hidden && x.tagName && !(x.closest && x.closest("[hidden]")));
    if (!items.length) return;
    const at = items.indexOf(document.activeElement);
    const next = e.shiftKey ? (at <= 0 ? items.length - 1 : at - 1) : (at < 0 || at === items.length - 1 ? 0 : at + 1);
    e.preventDefault(); items[next].focus();
  }

  // Escape closes it from anywhere on the page, not only when focus is inside it. Registered before hud.js's own
  // Escape handlers, and stopImmediatePropagation keeps those from also closing the drawer or stopping a reply.
  document.addEventListener("keydown", (e) => {
    if (!state || e.key !== "Escape") return;
    e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation?.();
    dismiss();
  });

  // Number keys also work with nothing focused (e.g. after a click on the page background) - but never while
  // typing in the chat box or any other field, and never with a modifier held.
  document.addEventListener("keydown", (e) => {
    if (!state || el.contains(e.target) || e.ctrlKey || e.altKey || e.metaKey) return;
    if (document.activeElement && document.activeElement !== document.body) return;
    if (/^[1-9]$/.test(e.key) && optButtons()[Number(e.key) - 1]) { e.preventDefault(); choose(optButtons()[Number(e.key) - 1]); }
  });

  // After a dismissed question the reply keeps an "Answer" button (hud.js); it brings the same question back.
  const canReopen = () => !!last;
  const reopen = () => { if (last && !state) show(last); };
  const forget = () => { last = null; };

  window.JarvisAsk = { init(h) { host = h; }, show, close, dismiss, canReopen, reopen, forget, spokenReply, speechFor, isOpen: () => !!state };
})();
