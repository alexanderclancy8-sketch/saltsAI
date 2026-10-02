"""SSML for Azure Neural TTS: natural pacing, an optional conversational style, and slower numbers.

Input is text that has already been through voice.speakable() (markdown, symbols, URLs and machine IDs gone,
money/dates/times already written the way a person says them). This module only decides *how* it is said:

- a short pause between sentences and at clause breaks (; and :) - commas are left to the voice, which already
  pauses on them and sounds stilted if a second pause is stacked on top;
- numbers, times and references (anything with a digit in it, plus "pounds"/"percent" after one) at a slightly
  slower rate so figures are easy to catch;
- the "chat" speaking style, only for voices that actually support it (others would ignore it, but leaving it
  off keeps the SSML honest and the request small).

Every piece of text is XML-escaped; nothing from a reply can add an SSML element of its own.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

SENTENCE_BREAK_MS = 300
CLAUSE_BREAK_MS = 160
NUMBER_RATE = "-10%"

# Styles the en-GB neural voices accept in <mstts:express-as> (Azure's "voice styles" table). Anything not listed
# here is spoken in the voice's normal delivery.
VOICE_STYLES: dict[str, frozenset[str]] = {
    "en-GB-RyanNeural": frozenset({"chat", "cheerful"}),
    "en-GB-SoniaNeural": frozenset({"cheerful", "sad"}),
}

_SENTENCES = re.compile(r"(?<=[.!?…])\s+(?=[A-Z0-9\"'£(])")
_CLAUSES = re.compile(r"(?<=[;:])\s+")
# A figure, time or reference: a word containing a digit ("9am", "2:30pm", "J24100", "5839", "1.2.3-rc1"), plus
# the unit words speakable() leaves after a sum of money ("73 thousand pounds", "12 pounds 50", "20 percent").
_SLOW = re.compile(r"\b\w*\d(?:[\w:./-]*\w)?(?:\s(?:thousand|million|billion))?(?:\s(?:pounds?|percent)(?:\s\d{2}\b)?)?")


def _attr(value: str) -> str:
    return escape(value, {"'": "&apos;"})


def _slow_numbers(segment: str) -> str:
    out, pos = [], 0
    for m in _SLOW.finditer(segment):
        out.append(escape(segment[pos:m.start()]))
        out.append(f"<prosody rate='{NUMBER_RATE}'>{escape(m.group(0))}</prosody>")
        pos = m.end()
    out.append(escape(segment[pos:]))
    return "".join(out)


def _sentence(sentence: str) -> str:
    return f"<break time='{CLAUSE_BREAK_MS}ms'/>".join(_slow_numbers(c) for c in _CLAUSES.split(sentence))


def style_for(voice: str, style: str) -> str:
    """The speaking style to use with this voice, or "" if it doesn't support the one asked for."""
    return style if style and style in VOICE_STYLES.get(voice, ()) else ""


def build_ssml(text: str, voice: str, style: str = "") -> str:
    sentences = [s for s in (p.strip() for p in _SENTENCES.split(text or "")) if s]
    spoken = f"<break time='{SENTENCE_BREAK_MS}ms'/>".join(_sentence(s) for s in sentences)
    style = style_for(voice, style)
    if style:
        spoken = f"<mstts:express-as style='{_attr(style)}'>{spoken}</mstts:express-as>"
    return (
        "<speak version='1.0' xml:lang='en-GB' xmlns='http://www.w3.org/2001/10/synthesis' "
        "xmlns:mstts='https://www.w3.org/2001/mstts'>"
        f"<voice name='{_attr(voice)}'>{spoken}</voice></speak>"
    )
