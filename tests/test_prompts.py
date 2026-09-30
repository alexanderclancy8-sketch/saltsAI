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
