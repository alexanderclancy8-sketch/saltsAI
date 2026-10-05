"""Jarvis must never talk over himself: every sentence is spoken once, by one voice at a time, in the tab that asked.

Real headless Chrome through Playwright (skipped when it is not installed, like the other test_console_browser*
files whose fixtures this shares), against a real server whose Claude stand-in streams a word at a time.

I cannot hear the result, so the page is instrumented instead: `speechSynthesis` and the `play()` of the audio element
are replaced by recorders that note every sentence handed to them and how many voices are sounding at the same moment
(`maxSpeech` browser utterances, `maxAudio` audio clips, `maxBoth` the two together). The assertions are on those
counters: each sentence handed over exactly once, in order, and never more than one voice sounding.

What these pin down (the owner heard "two voices over himself"):
  * two tabs / devices open on the same Jarvis both read the same reply aloud - only the one that asked may;
  * a clip that cannot be played used to advance the queue twice (the media element's `error` event AND the rejected
    `play()` both did), so the browser voice read a sentence while the rest of the queue started over the top of it;
  * a reply and the question pop-up it ends with were glued into one sentence, and an HTTP-fallback stream that the
    WebSocket joined part-way delivered every remaining word twice.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("playwright.sync_api")

from tests.fakes import message, text_block  # noqa: E402
from tests.test_console_browser import browser  # noqa: E402,F401
from tests.test_console_browser_phase2 import ask_script, chat  # noqa: E402,F401

SENTENCES = ["Four jobs are on today.", "Two engineers are out already.", "Nothing is overdue."]
TEXT = " ".join(SENTENCES)

# Replaces the two speech outputs with recorders. `play`/`speak` hand a clip / utterance over and finish it 150 ms
# later; `cancel()`/`pause()` end it early, the way the real thing does (an utterance gets an "interrupted" error).
INSTRUMENT = """
(() => {
  const W = window.__voice = { speech: [], audio: [], maxSpeech: 0, maxAudio: 0, maxBoth: 0, speechNow: 0, audioNow: 0 };
  const note = () => { W.maxSpeech = Math.max(W.maxSpeech, W.speechNow); W.maxAudio = Math.max(W.maxAudio, W.audioNow);
                       W.maxBoth = Math.max(W.maxBoth, W.speechNow + W.audioNow); };
  const live = new Set();
  const synth = {
    speak(u) { W.speech.push(u.text); live.add(u); W.speechNow = live.size; note();
      setTimeout(() => { if (live.delete(u)) { W.speechNow = live.size; u.onend && u.onend({}); } }, 150); },
    cancel() { const l = [...live]; live.clear(); W.speechNow = 0; l.forEach((u) => u.onerror && setTimeout(() => u.onerror({}), 0)); },
    getVoices() { return []; }, pause() {}, resume() {}, speaking: false, pending: false, addEventListener() {}, removeEventListener() {},
  };
  Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
  const playing = new Set();
  window.__audioBehaviour = "ok";   // ok | unplayable (error event AND rejected play) | twice (ended and error both fire)
  HTMLMediaElement.prototype.play = function () {
    const el = this; W.audio.push(el.src);
    if (window.__audioBehaviour === "unplayable") {
      setTimeout(() => el.onerror && el.onerror(new Event("error")), 5);
      return Promise.reject(new DOMException("no supported source", "NotSupportedError"));
    }
    playing.add(el); W.audioNow = playing.size; note();
    setTimeout(() => {
      if (!playing.delete(el)) return;
      W.audioNow = playing.size;
      el.onended && el.onended();
      if (window.__audioBehaviour === "twice") el.onerror && el.onerror(new Event("error"));
    }, 150);
    return Promise.resolve();
  };
  HTMLMediaElement.prototype.pause = function () { playing.delete(this); W.audioNow = playing.size; };
})();
"""

# A speech recogniser whose `say()` the test calls: the owner "speaking" (voice mode, push to talk).
SR_MOCK = """
window.SpeechRecognition = window.webkitSpeechRecognition = class {
  constructor() { window.__sr = this; }
  start() {}
  stop() { if (this.onend) setTimeout(() => this.onend(), 0); }
  say(text) { const res = [[{ transcript: text, confidence: 0.9 }]]; res[0].isFinal = true; this.onresult({ resultIndex: 0, results: res }); }
};
"""

# While `__blockWs` is true every WebSocket the page opens fails at once (the live connection is down); the page keeps
# retrying every 2.5 s, so clearing it lets the real connection come back.
WS_GATE = """
(() => {
  const Real = window.WebSocket;
  window.__blockWs = false;
  window.WebSocket = function (url, protocols) {
    if (window.__blockWs && String(url).endsWith("/ws")) {
      const dead = { readyState: 3, send() {}, close() {} };
      setTimeout(() => dead.onclose && dead.onclose({ code: 1006 }), 0);
      return dead;
    }
    return protocols ? new Real(url, protocols) : new Real(url);
  };
  window.WebSocket.prototype = Real.prototype;
  for (const k of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) window.WebSocket[k] = Real[k];
})();
"""
WS_DOWN = [WS_GATE, "window.__blockWs = true;"]


def _new_context(browser, tts="browser", speak="always", extra_scripts=()):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    ctx.add_init_script(INSTRUMENT)
    ctx.add_init_script(f"localStorage.setItem('jarvis.speak', '{speak}');")
    for script in extra_scripts:
        ctx.add_init_script(script)
    if tts == "audio":
        ctx.route("**/api/tts", lambda r: r.fulfill(status=200, content_type="audio/wav", body=b"RIFF0000WAVE"))
    else:  # the server voice is unavailable: the browser's own voice is used for every sentence
        ctx.route("**/api/tts", lambda r: r.fulfill(status=503, content_type="application/json", body='{"detail":"no voice"}'))
    return ctx


def _open(ctx, url):
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need", timeout=15000)
    return page


def _ask(page, text):
    page.fill("#input", text)
    page.click("#btn-send")


def _voice(page, wait=2.0):
    """What the page handed to its speakers, after giving the last of it time to be spoken."""
    time.sleep(wait)
    return page.evaluate("JSON.parse(JSON.stringify(window.__voice))")


def _wait_for_reply(page):
    page.wait_for_selector(".msg.assistant .src", timeout=20000)


def _speak_to(page, text="what is on today"):
    """The owner speaking to Jarvis in this tab: push to talk, with the recogniser stand-in."""
    page.click("#btn-mic")
    page.wait_for_function("window.__sr")
    page.evaluate("text => __sr.say(text)", text)


# ---------------------------------------------------------------- one reply, spoken once
@pytest.mark.parametrize("tts", ["browser", "audio"])
@pytest.mark.parametrize("live_socket", [True, False], ids=["websocket", "http-fallback"])
def test_a_streamed_reply_is_spoken_once_whatever_carries_it(browser, chat, live_socket, tts):
    srv, j, client, gate = chat([message([text_block(TEXT)])], delay=0.005)
    ctx = _new_context(browser, tts, extra_scripts=[] if live_socket else WS_DOWN)
    page = _open(ctx, srv.url)
    try:
        _ask(page, "What's on today?")
        _wait_for_reply(page)
        v = _voice(page)
        if tts == "browser":
            assert v["speech"] == SENTENCES and v["audio"] == []
        else:
            assert len(v["audio"]) == len(SENTENCES) and v["speech"] == []
        assert v["maxSpeech"] <= 1 and v["maxAudio"] <= 1 and v["maxBoth"] <= 1, v
        assert page.errors == []
    finally:
        ctx.close()


# ---------------------------------------------------------------- only the tab that asked speaks
@pytest.mark.parametrize("tts", ["browser", "audio"])
def test_a_second_open_tab_shows_the_reply_but_does_not_read_it_aloud(browser, chat, tts):
    srv, j, client, gate = chat([message([text_block(TEXT)])], delay=0.005)
    ctx = _new_context(browser, tts, speak="voice", extra_scripts=[SR_MOCK])
    asker, other = _open(ctx, srv.url), _open(ctx, srv.url)
    try:
        _speak_to(asker)                             # voice mode, so every open tab would speak the reply
        _wait_for_reply(asker)
        other.wait_for_selector(".msg.assistant .src", timeout=20000)
        a, o = _voice(asker), _voice(other, 0.5)
        spoken = a["speech"] if tts == "browser" else a["audio"]
        assert len(spoken) == len(SENTENCES), a
        assert o["speech"] == [] and o["audio"] == [], o   # the other tab saw the reply and stayed quiet
        assert "Nothing is overdue." in other.inner_text("#conversation")
    finally:
        ctx.close()


def test_a_tab_does_not_speak_a_reply_to_something_typed_in_another_tab(browser, chat):
    srv, j, client, gate = chat([message([text_block(TEXT)])], delay=0.005)
    ctx = _new_context(browser, "browser", speak="always")
    typist, other = _open(ctx, srv.url), _open(ctx, srv.url)
    try:
        _ask(typist, "What's on today?")
        _wait_for_reply(typist)
        other.wait_for_selector(".msg.assistant .src", timeout=20000)
        assert _voice(typist)["speech"] == SENTENCES
        assert _voice(other, 0.5)["speech"] == []
    finally:
        ctx.close()


def test_each_tab_still_speaks_its_own_replies(browser, chat):
    """Ownership follows the turn, not the tab: after the other tab asks something, it is the one that speaks."""
    srv, j, client, gate = chat([message([text_block(TEXT)]), message([text_block("Bravo is next. Bravo is done.")])], delay=0.005)
    ctx = _new_context(browser, "browser", speak="always")
    first, second = _open(ctx, srv.url), _open(ctx, srv.url)
    try:
        _ask(first, "one")
        _wait_for_reply(first)
        second.wait_for_selector(".msg.assistant .src", timeout=20000)
        time.sleep(1.5)
        _ask(second, "two")
        second.wait_for_function("/Bravo is done/.test(document.body.innerText)", timeout=20000)
        assert _voice(first)["speech"] == SENTENCES
        assert _voice(second)["speech"] == ["Bravo is next.", "Bravo is done."]
    finally:
        ctx.close()


# ---------------------------------------------------------------- a clip that will not play
def test_a_clip_that_will_not_play_is_read_in_the_browser_voice_once_without_the_queue_starting_over_it(browser, chat):
    srv, j, client, gate = chat([message([text_block(TEXT)])], delay=0.005)
    ctx = _new_context(browser, "audio")
    page = _open(ctx, srv.url)
    page.evaluate("window.__audioBehaviour = 'unplayable'")
    try:
        _ask(page, "What's on today?")
        _wait_for_reply(page)
        v = _voice(page, 3.0)
        assert v["speech"] == SENTENCES, v          # every sentence, once, in order, in the browser voice
        assert len(v["audio"]) == len(SENTENCES), v  # each clip tried exactly once
        assert v["maxSpeech"] == 1 and v["maxBoth"] == 1, v   # never two voices at the same moment
    finally:
        ctx.close()


def test_a_clip_that_reports_both_ended_and_error_advances_the_queue_once(browser, chat):
    srv, j, client, gate = chat([message([text_block(TEXT)])], delay=0.005)
    ctx = _new_context(browser, "audio")
    page = _open(ctx, srv.url)
    page.evaluate("window.__audioBehaviour = 'twice'")
    try:
        _ask(page, "What's on today?")
        _wait_for_reply(page)
        v = _voice(page, 2.5)
        assert len(v["audio"]) == len(SENTENCES) and v["maxAudio"] == 1 and v["speech"] == [], v
    finally:
        ctx.close()


# ---------------------------------------------------------------- the question pop-up
@pytest.mark.parametrize("tts", ["browser", "audio"])
def test_the_question_is_read_once_and_apart_from_the_reply_before_it(browser, chat, tts):
    srv, j, client, gate = chat(ask_script(), delay=0.005)
    ctx = _new_context(browser, tts)
    page = _open(ctx, srv.url)
    try:
        _ask(page, "Draft the letter")
        page.wait_for_selector("#ask:not([hidden])", timeout=20000)
        v = _voice(page, 4.0)
        if tts == "browser":
            said = v["speech"]
            assert said[0] == "The letter is ready.", said     # the reply's own sentence is not glued to the question
            assert sum(s.startswith("Send it now or hold it?") for s in said) == 1, said
            assert sum("Option one" in s for s in said) == 1 and sum("Or say something else" in s for s in said) == 1, said
        else:
            assert len(v["audio"]) == 9 and v["speech"] == [], v   # reply sentence, question, 3 options x2, the way out
        assert v["maxBoth"] <= 1, v
    finally:
        ctx.close()


# ---------------------------------------------------------------- a new message while the old one is mid-reply
def test_http_fallback_second_message_gets_its_own_reply_and_the_old_one_is_not_read(browser, chat):
    a = "Alpha one is here. Alpha two is here. Alpha three is here. Alpha four is here."
    srv, j, client, gate = chat([message([text_block(a)]), message([text_block("Bravo one is here. Bravo two is here.")])],
                                delay=0.005, hold_after=6)
    ctx = _new_context(browser, "browser", extra_scripts=WS_DOWN)
    page = _open(ctx, srv.url)
    try:
        _ask(page, "first")
        page.wait_for_function("window.__voice.speech.length >= 1", timeout=20000)   # "Alpha one is here." is being read
        page.fill("#input", "second")            # Send has become Stop while a reply is coming in: submit with Enter
        page.press("#input", "Enter")
        client.gate.set()
        page.wait_for_function("/Bravo two is here/.test(document.body.innerText)", timeout=30000)
        page.wait_for_function("window.__voice.speech.some((s) => s.startsWith('Bravo two'))", timeout=15000)
        v = _voice(page, 1.5)    # ... and a moment for anything that should not be said to show up
        assert [s for s in v["speech"] if s.startswith("Bravo")] == ["Bravo one is here.", "Bravo two is here."], v
        assert not any(s.startswith("Alpha t") or s.startswith("Alpha f") for s in v["speech"]), v
        assert v["maxSpeech"] <= 1, v
    finally:
        ctx.close()


def test_the_websocket_joining_a_fallback_stream_part_way_does_not_deliver_the_rest_twice(browser, chat):
    text = "Alpha one is here. Alpha two is here. Alpha three is here. Alpha four is here."
    srv, j, client, gate = chat([message([text_block(text)])], delay=0.005, hold_after=6)
    ctx = _new_context(browser, "browser", extra_scripts=WS_DOWN)
    page = _open(ctx, srv.url)
    try:
        _ask(page, "tell me")                     # the live connection is down, so this goes over the plain HTTP stream
        page.wait_for_function("window.__voice.speech.length >= 1", timeout=20000)
        page.evaluate("window.__blockWs = false")  # ... and the live connection comes back while the reply is still being written
        time.sleep(3.5)                            # the page retries every 2.5 s
        client.gate.set()
        page.wait_for_function("/Alpha four is here/.test(document.body.innerText)", timeout=30000)
        page.wait_for_function("window.__voice.speech.length >= 4", timeout=15000)
        v = _voice(page, 1.5)    # ... and a moment for a second copy of anything to show up
        assert v["speech"] == ["Alpha one is here.", "Alpha two is here.", "Alpha three is here.", "Alpha four is here."], v
        assert page.inner_text("#conversation").count("Alpha four is here.") == 1
    finally:
        ctx.close()


# ---------------------------------------------------------------- Jarvis speaking up on his own
def test_a_proactive_message_is_read_by_one_tab_only(browser, chat):
    srv, j, client, gate = chat([message([text_block(TEXT)])], delay=0.005)
    app = srv.server.config.app

    @app.post("/__test/proactive")
    async def _proactive():  # runs on the server's own event loop, like a scheduled check posting into the open chat
        j.bus.publish("proactive", {"id": "p-1", "text": "The pull request was merged.", "source": "pr_watch", "speak": True})
        return {"ok": True}

    ctx = _new_context(browser, "browser", speak="voice", extra_scripts=[SR_MOCK])
    first, second = _open(ctx, srv.url), _open(ctx, srv.url)
    try:
        _speak_to(first)                              # a spoken turn: both tabs now know the session is a voice one
        _wait_for_reply(first)
        second.wait_for_selector(".msg.assistant .src", timeout=20000)
        time.sleep(1.5)
        before = len(first.evaluate("__voice.speech")) + len(second.evaluate("__voice.speech"))
        first.evaluate("fetch('/__test/proactive', {method: 'POST'})")
        for page in (first, second):
            page.wait_for_function("document.body.innerText.includes('The pull request was merged.')", timeout=15000)
        time.sleep(1.5)
        said = first.evaluate("__voice.speech") + second.evaluate("__voice.speech")
        assert said.count("The pull request was merged.") == 1, said
        assert len(said) == before + 1
    finally:
        ctx.close()
