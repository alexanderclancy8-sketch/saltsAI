"""Stored drafts + PDF / Word rendering + the authenticated download endpoint."""

import io
import re
import zipfile

from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import documents
from tests.fakes import FakeClient

SAMPLE = """# Heading one

Intro paragraph with **bold**, *italic*, `code` & <angle> brackets, plus ⚠ and → symbols.

## Risks

| Hazard | Who | Controls |
|---|---|---|
| Working at height | Engineer | Use a **podium** step |
| Asbestos | All | Check the register |

- first point
- second point
  continued here

1. one
2. two

---

```
raw   code line
```
"""

GOOD_ID = "a" * 32


def make_doc(**over):
    return {"id": GOOD_ID, "created_at": "2026-09-30T10:00:00+00:00", "kind": "rams",
            "title": "RAMS - JOB-1 / test", "markdown": SAMPLE, **over}


def test_parse_markdown_blocks():
    blocks = documents._parse_markdown(SAMPLE)
    kinds = [b[0] for b in blocks]
    assert kinds == ["h", "p", "h", "table", "list", "list", "hr", "code"]
    assert blocks[3][1][0] == ["Hazard", "Who", "Controls"] and len(blocks[3][1]) == 3  # separator row dropped
    assert blocks[4][1][1] == ("•", "second point continued here")
    assert blocks[5][1][0] == ("1.", "one")


def test_render_pdf_is_a_real_pdf_with_cover_and_pages():
    data = documents.render_pdf(make_doc(), "Salts Fire and Security")
    assert data.startswith(b"%PDF") and b"%%EOF" in data[-64:]
    # Cover + at least one body page
    assert len(re.findall(rb"/Type\s*/Page(?![s\w])", data)) >= 2
    assert len(data) > 5000  # the logo image is embedded


def test_render_pdf_copes_with_empty_and_long_content():
    assert documents.render_pdf(make_doc(markdown=""), "Salts").startswith(b"%PDF")
    long = "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(60))
    assert documents.render_pdf(make_doc(markdown=long, title="T" * 300), "Salts").startswith(b"%PDF")


def test_render_docx_is_a_real_docx():
    data = documents.render_docx(make_doc(), "Salts Fire and Security")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode()
        assert "PROTECTING WHAT MATTERS MOST" in xml and "Hazard" in xml and "Working at height" in xml
        assert any(n.startswith("word/media/") for n in z.namelist())  # logo
        footer = "".join(z.read(n).decode() for n in z.namelist() if n.startswith("word/footer"))
        assert "PAGE" in footer


def test_doc_id_validation_and_filename():
    assert documents.valid_doc_id(GOOD_ID)
    for bad in ("", "../etc/passwd", "A" * 32, "a" * 31, "a" * 33, "g" * 32):
        assert not documents.valid_doc_id(bad)
    assert documents.download_filename({"title": 'RAMS - "x"/../y\r\n'}, "pdf") == "RAMS-x-y.pdf"
    assert documents.download_filename({"title": "!!!"}, "docx") == "document.docx"


async def test_drafts_are_stored_and_announced(tmp_path):
    from jarvis.config import Settings

    j = Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None), client=FakeClient())
    q = j.bus.subscribe()
    text = await j.documents.recruitment("Fire alarm service engineer", "FIA card needed")
    assert text == "Certainly, sir."
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    event = next(e for e in events if e["type"] == "display")
    assert documents.valid_doc_id(event["data"]["doc_id"])
    stored = j.documents.get(event["data"]["doc_id"])
    assert stored["kind"] == "recruitment" and stored["markdown"] == "Certainly, sir."
    assert stored["title"] == "Recruitment - Fire alarm service engineer" and stored["created_at"]
    assert j.documents.get("nope") is None and j.documents.get(GOOD_ID) is None
    await j.http.aclose()


def test_download_endpoint(settings):
    settings.jarvis_owner_password = "s3cret"
    j = Jarvis(settings, client=FakeClient())
    j.db.add_document(GOOD_ID, "hr_letter", "HR - warning (Sam)", SAMPLE)
    app = create_app(settings, j)
    with TestClient(app) as c:
        assert c.get(f"/api/documents/{GOOD_ID}/pdf").status_code == 401  # not signed in
        c.post("/login", data={"password": "s3cret"}, follow_redirects=False)
        r = c.get(f"/api/documents/{GOOD_ID}/pdf")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert r.headers["content-disposition"] == 'attachment; filename="HR-warning-Sam.pdf"'
        assert r.content.startswith(b"%PDF")
        r = c.get(f"/api/documents/{GOOD_ID}/docx")
        assert r.status_code == 200 and r.headers["content-type"] == documents.DOCX_MIME
        assert r.headers["content-disposition"].endswith('.docx"') and r.content.startswith(b"PK")
        assert c.get(f"/api/documents/{'b' * 32}/pdf").status_code == 404  # unknown id
        assert c.get(f"/api/documents/{GOOD_ID}/odt").status_code == 404  # unknown format
        assert c.get("/api/documents/not-a-valid-id/pdf").status_code == 400
        assert c.get("/api/documents/..%2F..%2Fetc%2Fpasswd/pdf").status_code in (400, 404)
