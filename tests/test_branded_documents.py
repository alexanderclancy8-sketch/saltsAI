"""PDF output from draft_office_document and the Salts branded template (navy, name + address footer, optional logo).

Drafts only: nothing here sends anything - sending stays with the approval-gated email_send tool."""

import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import documents
from tests.fakes import FakeClient

GOOD_ID = "c" * 32
ADDRESS = "1 Example Street, Leeds LS1 1AA"
MARKDOWN = """# Quarterly report

Intro with **bold** text.

| Item | Qty |
|---|---|
| Detector | 4 |

- first point
- second point
"""


def make_doc(markdown=MARKDOWN):
    return {"id": GOOD_ID, "created_at": "2026-09-30T10:00:00+00:00", "kind": "report", "title": "Quarterly report",
            "markdown": markdown}


def make_logo(tmp_path, name="logo.png"):
    pil = pytest.importorskip("PIL.Image")
    path = tmp_path / name
    pil.new("RGB", (60, 20), (11, 31, 75)).save(path)
    return path


def pdf_text(data: bytes) -> str:
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(data))
    return re.sub(r"\s+", " ", " ".join(p.extract_text() or "" for p in reader.pages[1:]))  # body pages, not the cover


def image_count(data: bytes) -> int:
    return len(re.findall(rb"/Subtype\s*/Image", data))


def _jarvis(tmp_path, **over):
    return Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, **over), client=FakeClient())


# ------------------------------------------------------------------ PDF output from the tool
async def test_tool_can_produce_a_pdf_draft_and_never_sends(tmp_path):
    from jarvis.brain.tools import TOOLS_BY_NAME, OfficeDocumentIn, draft_office_document

    tool = TOOLS_BY_NAME["draft_office_document"]
    assert tool.approval is False and "PDF" in tool.description
    assert "email_send" in tool.description and "never sent" in tool.description
    assert "TO CONFIRM" in tool.description  # placeholder or unconfirmed figures must be labelled
    j = _jarvis(tmp_path)

    async def boom(*a, **k):
        raise AssertionError("an email was sent")

    j.mail.send_mail = boom
    j.mail.create_reply_draft = boom
    out = await draft_office_document(j, OfficeDocumentIn(format="pdf", title="Quarterly report", content=MARKDOWN))
    assert out["format"] == "pdf" and out["download_url"] == f"/api/documents/{out['doc_id']}/pdf"
    assert "not been sent" in out["note"] and "approval" in out["note"]
    assert j.documents.get(out["doc_id"])["markdown"] == MARKDOWN
    await j.http.aclose()


def test_pdf_is_rendered_from_the_markdown():
    data = documents.render_pdf(make_doc(), "Salts Fire and Security")
    assert data.startswith(b"%PDF")
    text = pdf_text(data)
    assert "Quarterly report" in text and "Intro with bold text." in text
    assert "Item" in text and "Detector" in text and "first point" in text


# ------------------------------------------------------------------ branding
def test_pdf_footer_carries_company_name_and_address():
    text = pdf_text(documents.render_pdf(make_doc(), "Salts Fire and Security", address=ADDRESS))
    assert f"Salts Fire and Security | {ADDRESS}" in text
    without = pdf_text(documents.render_pdf(make_doc(), "Salts Fire and Security"))
    assert ADDRESS not in without  # no address configured -> no address printed, nothing invented


def test_pdf_header_logo_is_drawn_only_when_a_logo_is_supplied(tmp_path):
    logo = make_logo(tmp_path)
    plain = documents.render_pdf(make_doc(), "Salts Fire and Security")
    branded = documents.render_pdf(make_doc(), "Salts Fire and Security", logo=logo)
    assert image_count(branded) == image_count(plain) + 1
    assert "Salts Fire and Security" in pdf_text(plain)  # name-only header/footer when there's no logo


def test_pdf_tolerates_an_unreadable_logo(tmp_path):
    bad = tmp_path / "logo.png"
    bad.write_bytes(b"not an image")
    assert documents.render_pdf(make_doc(), "Salts", logo=bad).startswith(b"%PDF")


def _docx_parts(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        read = lambda prefix: "".join(z.read(n).decode() for n in names if n.startswith(prefix))  # noqa: E731
        return {"document": z.read("word/document.xml").decode(), "styles": z.read("word/styles.xml").decode(),
                "header": read("word/header"), "footer": read("word/footer"),
                "media": [n for n in names if n.startswith("word/media/")]}


def test_docx_uses_navy_headings_table_headers_and_footer_address():
    parts = _docx_parts(documents.render_docx(make_doc(), "Salts Fire and Security", address=ADDRESS))
    assert "0B1F4B" in parts["styles"]  # navy heading colour
    assert 'w:fill="0B1F4B"' in parts["document"]  # navy table header row
    assert "Salts Fire and Security" in parts["footer"] and ADDRESS in parts["footer"] and "PAGE" in parts["footer"]
    assert "Salts Fire and Security" in parts["header"]  # name-only header when there is no logo
    assert ADDRESS not in _docx_parts(documents.render_docx(make_doc(), "Salts"))["footer"]


def test_docx_header_logo_only_when_supplied(tmp_path):
    plain = _docx_parts(documents.render_docx(make_doc(), "Salts"))
    branded = _docx_parts(documents.render_docx(make_doc(), "Salts", logo=make_logo(tmp_path)))
    assert len(branded["media"]) == len(plain["media"]) + 1


def test_brand_logo_only_accepts_a_small_real_image(tmp_path, monkeypatch):
    good = make_logo(tmp_path)
    assert documents.brand_logo(str(good)) == good
    assert documents.brand_logo("") is None and documents.brand_logo(None) is None
    assert documents.brand_logo(str(tmp_path / "missing.png")) is None
    svg = tmp_path / "logo.svg"
    svg.write_text("<svg/>")
    assert documents.brand_logo(str(svg)) is None
    monkeypatch.setattr(documents, "MAX_LOGO_BYTES", 10)
    assert documents.brand_logo(str(good)) is None


# ------------------------------------------------------------------ the no-logo path is announced
async def test_tool_says_when_no_logo_is_set(tmp_path):
    from jarvis.brain.tools import OfficeDocumentIn, draft_office_document

    # A configured path that doesn't work is "no logo" (an UNSET path now falls back to the bundled Salts logo)
    j = _jarvis(tmp_path, company_logo_path=str(tmp_path / "gone.png"))
    out = await draft_office_document(j, OfficeDocumentIn(format="pdf", title="Report", content=MARKDOWN))
    assert "no company logo" in out["branding_note"].lower() and "navy" in out["branding_note"]
    out = await draft_office_document(j, OfficeDocumentIn(format="docx", title="Report", content=MARKDOWN))
    assert "no company logo" in out["branding_note"].lower()
    out = await draft_office_document(j, OfficeDocumentIn(format="xlsx", title="Stock", content=MARKDOWN))
    assert "branding_note" not in out  # a spreadsheet has no logo slot, nothing to announce
    await j.http.aclose()


async def test_tool_reports_the_logo_when_one_is_set(tmp_path):
    from jarvis.brain.tools import OfficeDocumentIn, draft_office_document

    j = _jarvis(tmp_path, company_logo_path=str(make_logo(tmp_path)))
    out = await draft_office_document(j, OfficeDocumentIn(format="pdf", title="Report", content=MARKDOWN))
    assert "logo" in out["branding_note"].lower() and "no company logo" not in out["branding_note"].lower()
    await j.http.aclose()


def test_header_logo_defaults_to_the_bundled_salts_logo_when_unset(tmp_path, monkeypatch):
    assert documents.LOGO_PATH.name == "salts-logo.jpg" and documents.LOGO_PATH.is_file()
    assert documents.header_logo("") == documents.LOGO_PATH and documents.header_logo(None) == documents.LOGO_PATH
    good = make_logo(tmp_path)
    assert documents.header_logo(str(good)) == good  # an explicit logo wins
    # a path that is set but doesn't work is NOT replaced by the default, so the owner is told
    assert documents.header_logo(str(tmp_path / "missing.png")) is None
    svg = tmp_path / "logo.svg"
    svg.write_text("<svg/>")
    assert documents.header_logo(str(svg)) is None
    monkeypatch.setattr(documents, "MAX_LOGO_BYTES", 10)  # the size check still applies to the default too
    assert documents.header_logo("") is None and documents.header_logo(str(good)) is None


async def test_unset_logo_path_uses_the_default_logo_and_shows_no_missing_logo_note(tmp_path):
    from jarvis.brain.tools import OfficeDocumentIn, draft_office_document

    j = _jarvis(tmp_path)  # COMPANY_LOGO_PATH unset
    for fmt in ("pdf", "docx"):
        out = await draft_office_document(j, OfficeDocumentIn(format=fmt, title="Report", content=MARKDOWN))
        assert "no company logo" not in out["branding_note"].lower() and "logo" in out["branding_note"].lower()
    await j.http.aclose()


def test_download_pdf_carries_the_default_logo_when_unset(settings):
    settings.company_logo_path = ""
    j = Jarvis(settings, client=FakeClient())
    j.db.add_document(GOOD_ID, "report", "Quarterly report", MARKDOWN)
    with TestClient(create_app(settings, j)) as c:
        r = c.get(f"/api/documents/{GOOD_ID}/pdf")
        assert r.status_code == 200
        assert image_count(r.content) > image_count(documents.render_pdf(make_doc(), settings.company_name))


async def test_a_logo_path_that_does_not_work_counts_as_no_logo(tmp_path):
    from jarvis.brain.tools import OfficeDocumentIn, draft_office_document

    j = _jarvis(tmp_path, company_logo_path=str(tmp_path / "gone.png"))
    out = await draft_office_document(j, OfficeDocumentIn(format="pdf", title="Report", content=MARKDOWN))
    assert "no company logo" in out["branding_note"].lower()
    await j.http.aclose()


# ------------------------------------------------------------------ the download link uses the branding
def test_download_pdf_uses_company_address_and_stays_behind_login(settings):
    settings.jarvis_owner_password = "s3cret"
    settings.company_address = ADDRESS
    j = Jarvis(settings, client=FakeClient())
    j.db.add_document(GOOD_ID, "report", "Quarterly report", MARKDOWN)
    app = create_app(settings, j)
    with TestClient(app) as c:
        assert c.get(f"/api/documents/{GOOD_ID}/pdf").status_code == 401
        c.post("/login", data={"password": "s3cret"}, follow_redirects=False)
        r = c.get(f"/api/documents/{GOOD_ID}/pdf")
        assert r.status_code == 200 and r.headers["content-type"] == documents.PDF_MIME
        assert ADDRESS in pdf_text(r.content)
        assert c.get(f"/api/documents/{GOOD_ID}/xlsx").status_code == 200  # shared renderer signature still works
