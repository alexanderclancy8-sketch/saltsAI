// Runs the real jarvis/web/ask.js against a tiny fake DOM and prints what happened as JSON.
// Driven by tests/test_ask_user.py (skipped there when `node` isn't installed). No npm packages needed.
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

class Node {
  constructor(tag) {
    this.tag = tag; this.tagName = tag.toUpperCase();
    this.attrs = {}; this.children = []; this.parent = null; this.listeners = {}; this.value = "";
  }
  get id() { return this.attrs.id || ""; }
  set id(v) { this.attrs.id = String(v); }
  get className() { return this.attrs.class || ""; }
  set className(v) { this.attrs.class = String(v); }
  get hidden() { return this.attrs.hidden !== undefined; }
  set hidden(v) { if (v) this.attrs.hidden = ""; else delete this.attrs.hidden; }
  get dataset() {
    const d = {};
    for (const k of Object.keys(this.attrs)) if (k.startsWith("data-")) d[camel(k.slice(5))] = this.attrs[k];
    return d;
  }
  get classes() { return (this.attrs.class || "").split(/\s+/).filter(Boolean); }
  get classList() {
    const self = this;
    return {
      toggle(c, force) {
        const s = new Set(self.classes);
        const want = force === undefined ? !s.has(c) : !!force;
        if (want) s.add(c); else s.delete(c);
        self.attrs.class = [...s].join(" ");
        return want;
      },
      contains: (c) => self.classes.includes(c),
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(n) { n.parent = this; this.children.push(n); return n; }
  contains(n) { for (let x = n; x; x = x.parent) if (x === this) return true; return false; }
  focus() { document.activeElement = this; }
  set innerHTML(html) { this.children = []; if (html) parse(this, html); }
  get innerHTML() { return ""; }
  matchesOne(sel) {
    if (sel.startsWith(".")) return this.classes.includes(sel.slice(1));
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    return this.tag === sel.toLowerCase();
  }
  matches(sel) {
    const parts = sel.trim().split(/\s+/);
    if (!this.matchesOne(parts[parts.length - 1])) return false;
    let i = parts.length - 2;
    for (let a = this.parent; a && i >= 0; a = a.parent) if (a.matchesOne && a.matchesOne(parts[i])) i--;
    return i < 0;
  }
  closest(sel) { for (let n = this; n; n = n.parent) if (n.matches && n.matches(sel)) return n; return null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c.matches(sel)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function parse(root, html) {
  const tag = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=>]+(?:="[^"]*")?)*)\s*>/g;
  const attr = /\s+([^\s=>]+)(?:="([^"]*)")?/g;
  const stack = [root];
  let m;
  while ((m = tag.exec(html))) {
    if (m[1]) { if (stack.length > 1) stack.pop(); continue; }
    const n = new Node(m[2]);
    let a;
    attr.lastIndex = 0;
    while ((a = attr.exec(m[3]))) n.attrs[a[1]] = a[2] === undefined ? "" : a[2];
    stack[stack.length - 1].appendChild(n);
    stack.push(n);
  }
}

const body = new Node("body");
const input = body.appendChild(new Node("textarea")); // the chat box (#input) that gets focus back after an answer
input.id = "input";
const docListeners = {};
const document = {
  body, activeElement: body,
  createElement: (t) => new Node(t),
  getElementById: (id) => (id === "input" ? input : null),
  addEventListener: (t, fn) => { (docListeners[t] ||= []).push(fn); },
};

function fire(type, target, extra = {}) {
  const ev = Object.assign({ type, target, shiftKey: false, isComposing: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopPropagation() { this._stopped = true; } }, extra);
  for (let n = target; n && !ev._stopped; n = n.parent) for (const fn of n.listeners[type] || []) fn(ev);
  if (!ev._stopped) for (const fn of docListeners[type] || []) fn(ev);
  return ev;
}
const click = (n) => fire("click", n);
const key = (n, k) => fire("keydown", n, { key: k });

const sandbox = { document };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "jarvis", "web", "ask.js"), "utf8"), sandbox);

const sent = [];
sandbox.JarvisAsk.init({ send: (t, m, o) => sent.push({ text: t, mode: m, opts: JSON.parse(JSON.stringify(o)) }),
  say() {}, speakNow: () => false, mode: () => "typed" });
const ask = () => body.children.find((c) => c.id === "ask");
const opts = () => ask().querySelectorAll(".ask-opt");
const EV = { id: "q1", question: "Which day for the Kestrel visit?",
  options: [{ label: "Tuesday", description: "Dan is free all day", recommended: true }, { label: "Thursday" }] };
const open = (over = {}) => sandbox.JarvisAsk.show({ ...EV, ...over });
const out = {};
const snap = () => sent.length;

// 1. clicking an option (here its inner label text, as a real click on the text would) sends it as Alex's reply
open();
out.questionShown = ask().querySelector(".ask-q") !== null && !ask().hidden;
out.optionTags = opts().map((o) => o.tag);
out.otherPresent = ask().querySelector(".ask-other") !== null;
out.singleSendHidden = ask().querySelector(".ask-send").hidden;
out.recommendedFocused = document.activeElement === opts()[0];
let n = snap();
click(opts()[1].querySelector(".ask-label"));
out.clickSent = sent.slice(n);
out.closedAfterClick = !sandbox.JarvisAsk.isOpen() && ask().hidden;
out.focusBackInChat = document.activeElement === input;

// 2. number key picks an option; arrows move focus
open();
key(opts()[0], "ArrowDown");
out.arrowMovedFocus = document.activeElement === opts()[1];
n = snap();
key(opts()[0], "1");
out.keySent = sent.slice(n);

// 3. "Other": opens the text box, Send submits the typed text; empty text sends nothing
open();
click(ask().querySelector(".ask-other"));
out.otherBoxOpen = !ask().querySelector(".ask-otherbox").hidden && !ask().querySelector(".ask-send").hidden;
n = snap();
click(ask().querySelector(".ask-send"));
out.emptyOtherSent = sent.slice(n);
out.stillOpenAfterEmpty = sandbox.JarvisAsk.isOpen();
ask().querySelector(".ask-otherbox textarea").value = "  Wednesday at 3pm  ";
click(ask().querySelector(".ask-send"));
out.otherSent = sent.slice(n);

// 3b. Enter inside the "Other" box also sends
open();
click(ask().querySelector(".ask-other"));
const ta = ask().querySelector(".ask-otherbox textarea");
ta.value = "Next week";
n = snap();
key(ta, "Enter");
out.otherEnterSent = sent.slice(n);

// 4. multi-select: toggles wait for Send, which joins them
open({ allow_multiple: true });
out.multiSendVisible = !ask().querySelector(".ask-send").hidden;
n = snap();
click(opts()[0]); click(opts()[1]);
out.multiSentEarly = sent.slice(n);
click(ask().querySelector(".ask-send"));
out.multiSent = sent.slice(n);

// 5. dismiss: button and Escape close it without sending anything
open();
n = snap();
click(ask().querySelector(".ask-dismiss"));
out.dismissClosed = !sandbox.JarvisAsk.isOpen() && ask().hidden;
open();
key(opts()[0], "Escape");
out.escapeClosed = !sandbox.JarvisAsk.isOpen() && ask().hidden;
out.dismissSent = sent.slice(n);

process.stdout.write(JSON.stringify(out));
