from jarvis.brain.prompts import PERSONA
from jarvis.services.advisor import ADVISOR_SYSTEM


def _persona(settings) -> str:
    return PERSONA.format(owner=settings.owner_name, company=settings.company_name,
                          salutation=settings.owner_salutation, issue_tag=settings.issue_email_tag,
                          core_docs="(docs)")


def test_persona_formats_and_keeps_existing_guidance(settings):
    text = _persona(settings)
    assert "Golden rule: suggest, never act on your own" in text
    assert "default short" in text
    assert "[spoken ...]" in text


def test_persona_spoken_guidance_is_tight_and_echo_aware(settings):
    text = _persona(settings)
    assert "never repeat the question back" in text
    assert "free of wake phrases" in text
    assert "at most one question" in text


def test_hud_voice_input_guards_against_self_echo_and_submits_in_every_mode():
    from pathlib import Path

    hud = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")
    # Self-echo guard is wired into every listener and the central utterance() path.
    assert "function looksLikeSelfEcho" in hud
    assert hud.count("looksLikeSelfEcho(") >= 5
    assert "ECHO_TAIL_MS" in hud and "speaker.recent" in hud
    # Push-to-talk / non-wake modes commit finals after end-of-speech instead of waiting for stop().
    assert "finalHeard()" in hud and "PTT_SILENCE_MS" in hud
    assert "if (S.listenMode === \"wake\") { utterance(this.finals)" not in hud
    # Still no unprompted greeting.
    assert "no on-load greeting" in hud


def _hud_source() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")


def test_hud_wake_mode_follow_up_exception_never_applies_in_echo_window():
    hud = _hud_source()
    # The strict allowlist inside the echo window is untouched.
    assert 'if (S.listenMode === "wake" && echoWindowOpen()) {' in hud
    assert "if (!isStopPhrase) return;" in hud
    # The follow-up exception is gated on the echo window being closed and on S.followUpUntil.
    assert "const inEchoWindow = echoWindowOpen();" in hud
    assert "if (inEchoWindow || Date.now() >= S.followUpUntil) {" in hud
    # A dropped utterance keeps its caption and gets a rate-limited, echo-window-suppressed toast.
    assert "(heard:" in hud
    assert "Didn't catch that with my name" in hud
    assert "DROP_TOAST_MS" in hud
    assert "if (inEchoWindow) return;" in hud


def test_hud_follow_up_window_only_set_after_a_genuine_exchange():
    hud = _hud_source()
    # S.followUpUntil has exactly one writer besides its initial value: finishVoiceTurn().
    assert hud.count("S.followUpUntil = ") == 1
    assert "S.followUpUntil = Date.now() + WAKE_LISTEN_MS;" in hud
    # extendFollowUp (called on errors, stops and any TTS ending) only keeps the mic open.
    assert "S.micUntil = Date.now() + ms;" in hud
    # A turn only counts once a spoken request was accepted by utterance() and answered.
    assert 'S.voiceTurn = "pending"' in hud and 'S.voiceTurn = "replied"' in hud


def test_persona_technical_authority_and_discipline(settings):
    text = _persona(settings)
    for std in ("BS 5839", "BS 5266", "BS EN 50131", "PD 6662", "BS 8243", "BS EN 62676", "BS EN 60839-11"):
        assert std in text
    assert "triage" in text and "audit" in text and "deliver" in text
    assert "Discipline for multi-step requests" in text
    assert "Self-audit" in text or "self-audit" in text


def test_advisor_system_formats_with_new_steps():
    text = ADVISOR_SYSTEM.format(owner="Alex", company="Salts", focus="")
    assert "Headline" in text and "90-day plan" in text  # existing structure preserved
    assert "labour" in text and "hardware" in text and "maintenance-contract" in text
    assert "three concrete recommendations" in text
    assert "risk mitigation" in text and "tax efficiency" in text and "business development" in text
    assert "HMRC" in text and "qualified accountant" in text
