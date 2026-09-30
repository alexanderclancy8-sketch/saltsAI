"""Paperwork: risk assessments & method statements (RAMS) per job, tender / pre-qualification questionnaire
answers drafted from the company's real evidence, HR documents (job postings, interview questions,
disciplinary/performance letters), and bid support - a go/no-go + pricing assessment grounded in real
capacity/cash/win-rate data, and a full narrative proposal document grounded in real evidence and comparable
past jobs, for tenders bigger than a plain PQQ answer_questionnaire response covers."""

from __future__ import annotations

import io
import json
import logging
import re
import textwrap
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from ..brain import llm

log = logging.getLogger(__name__)

# Drafted documents are stored (see Documents._save) so they can be rendered to PDF / Word on request.
# Rendering is pure Python only (reportlab + python-docx) - the app deploys to Azure App Service Linux,
# where system libraries such as Cairo/Pango or binaries such as wkhtmltopdf aren't available.
LOGO_PATH = Path(__file__).resolve().parent.parent / "web" / "assets" / "salts-logo.jpg"
TAGLINE = "PROTECTING WHAT MATTERS MOST"
DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
KIND_LABELS = {
    "rams": "Risk Assessment & Method Statement",
    "questionnaire": "Questionnaire Answers",
    "recruitment": "Recruitment Pack",
    "hr_letter": "HR Document",
    "bid_assessment": "Bid Assessment",
    "bid_document": "Bid Document",
}
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
NAVY_HEX, CARD_HEX, TEAL_HEX = "#0B1F4B", "#173A75", "#2FA4B8"


def valid_doc_id(doc_id: str) -> bool:
    return bool(DOC_ID_RE.fullmatch(doc_id or ""))


def download_filename(doc: dict[str, Any], ext: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(doc.get("title") or "")).strip("-")[:60] or "document"
    return f"{slug}.{ext}"


# ---------------------------------------------------------------- markdown -> blocks (shared by PDF and Word)
_INLINE_RE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*)")
_LIST_RE = re.compile(r"^([-*+]|\d+[.)])\s+(.*)$")


def _inline_tokens(text: str) -> list[tuple[str, bool, bool, bool]]:
    """Split inline markdown into (text, bold, italic, code) pieces."""
    out = []
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append((part[2:-2], True, False, False))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append((part[1:-1], False, False, True))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append((part[1:-1], False, True, False))
        else:
            out.append((part, False, False, False))
    return out


def _parse_markdown(src: str) -> list[tuple]:
    """A small markdown parser: headings, paragraphs, bullet/numbered lists, tables, rules, code fences."""
    blocks: list[tuple] = []
    lines = (src or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(code)))
            continue
        if not s:
            flush()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            blocks.append(("h", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            flush()
            blocks.append(("hr",))
            i += 1
            continue
        if s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):  # skip the |---|---| separator row
                    rows.append(cells)
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                blocks.append(("table", [r + [""] * (width - len(r)) for r in rows]))
            continue
        m = _LIST_RE.match(s)
        if m:
            flush()
            items: list[list[str]] = []
            while i < len(lines):
                cur = lines[i]
                lm = _LIST_RE.match(cur.strip())
                if lm:
                    marker = "•" if lm.group(1) in "-*+" else lm.group(1)
                    items.append([marker, lm.group(2)])
                elif cur.strip() and cur[:1].isspace() and items:  # continuation line of the previous item
                    items[-1][1] += " " + cur.strip()
                else:
                    break
                i += 1
            blocks.append(("list", [(a, b) for a, b in items]))
            continue
        para.append(s)
        i += 1
    flush()
    return blocks


def _latin1(text: str) -> str:
    """The built-in PDF fonts only cover Windows-1252; anything else becomes '?' rather than a black box."""
    return (text or "").encode("cp1252", errors="replace").decode("cp1252")


def _pdf_inline(text: str, bold_all: bool = False) -> str:
    out = []
    for piece, bold, italic, code in _inline_tokens(_latin1(text)):
        s = _xml_escape(piece)
        if code:
            s = f'<font face="Courier">{s}</font>'
        if bold or bold_all:
            s = f"<b>{s}</b>"
        if italic:
            s = f"<i>{s}</i>"
        out.append(s)
    return "".join(out)


def _pretty_date(created_at: str) -> str:
    try:
        return datetime.fromisoformat(created_at).strftime("%d %B %Y")
    except (TypeError, ValueError):
        return str(created_at or "")[:10]


def render_pdf(doc: dict[str, Any], company: str) -> bytes:
    """Branded PDF: navy cover page (rounded panels drawn on the reportlab canvas), then clean body pages."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader, simpleSplit
    from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, NextPageTemplate, PageBreak, PageTemplate,
                                    Paragraph, Preformatted, Spacer, Table, TableStyle)

    navy, card, teal = colors.HexColor(NAVY_HEX), colors.HexColor(CARD_HEX), colors.HexColor(TEAL_HEX)
    width, height = A4
    margin = 20 * mm
    title = _latin1(str(doc.get("title") or "Document"))
    company = _latin1(company or "Salts Fire and Security")
    kind_label = _latin1(KIND_LABELS.get(str(doc.get("kind")), "Document")).upper()
    date_text = _pretty_date(str(doc.get("created_at") or ""))

    def cover(canvas, _doc) -> None:
        canvas.saveState()
        canvas.setFillColor(navy)
        canvas.rect(0, 0, width, height, stroke=0, fill=1)
        m = 18 * mm
        # White rounded panel carrying the real Salts logo
        panel_w = width - 2 * m
        panel_top = height - 24 * mm
        panel_bottom = panel_top - 40 * mm
        try:
            logo = ImageReader(str(LOGO_PATH))
            iw, ih = logo.getSize()
            logo_w = panel_w - 14 * mm
            logo_h = logo_w * ih / iw
            panel_bottom = panel_top - logo_h - 12 * mm
            canvas.setFillColor(colors.white)
            canvas.roundRect(m, panel_bottom, panel_w, panel_top - panel_bottom, 6 * mm, stroke=0, fill=1)
            canvas.drawImage(logo, m + 7 * mm, panel_bottom + 6 * mm, width=logo_w, height=logo_h, mask=None)
        except Exception as e:  # noqa: BLE001 - a missing/unreadable logo must not stop the document rendering
            log.warning("cover logo not drawn: %s", e)
            canvas.setFillColor(colors.white)
            canvas.roundRect(m, panel_bottom, panel_w, panel_top - panel_bottom, 6 * mm, stroke=0, fill=1)
        # Accent bar
        canvas.setFillColor(teal)
        canvas.roundRect(m, panel_bottom - 12 * mm, 40 * mm, 3 * mm, 1.5 * mm, stroke=0, fill=1)
        # Title card
        lines = simpleSplit(title, "Helvetica-Bold", 26, panel_w - 20 * mm)[:4]
        card_h = 24 * mm + len(lines) * 32 + 18 * mm
        card_top = height * 0.58
        canvas.setFillColor(card)
        canvas.roundRect(m, card_top - card_h, panel_w, card_h, 6 * mm, stroke=0, fill=1)
        canvas.setFillColor(teal)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(m + 10 * mm, card_top - 12 * mm, kind_label)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 26)
        y = card_top - 24 * mm - 20
        for line in lines:
            canvas.drawString(m + 10 * mm, y, line)
            y -= 32
        canvas.setFillColor(colors.HexColor("#C9D6EE"))
        canvas.setFont("Helvetica", 11)
        canvas.drawString(m + 10 * mm, card_top - card_h + 9 * mm, date_text)
        # Company name + tagline in a rounded outline pill
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 18)
        canvas.drawCentredString(width / 2, 42 * mm, company)
        canvas.setStrokeColor(teal)
        canvas.setLineWidth(1.2)
        canvas.roundRect(width / 2 - 55 * mm, 24 * mm, 110 * mm, 10 * mm, 5 * mm, stroke=1, fill=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawCentredString(width / 2, 27.2 * mm, TAGLINE)
        canvas.restoreState()

    def body_page(canvas, _doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(teal)
        canvas.setLineWidth(1)
        canvas.line(margin, 16 * mm, width - margin, 16 * mm)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.setFont("Helvetica", 8.5)
        canvas.drawString(margin, 11 * mm, f"{company}  |  {title[:70]}")
        canvas.drawRightString(width - margin, 11 * mm, f"Page {canvas.getPageNumber()}")
        canvas.setFillColor(navy)
        canvas.rect(0, height - 4 * mm, width, 4 * mm, stroke=0, fill=1)
        canvas.restoreState()

    base = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14.5, textColor=colors.HexColor("#1F2933"),
                          spaceAfter=6)
    styles = {
        1: ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=navy,
                          spaceBefore=10, spaceAfter=8, keepWithNext=1),
        2: ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=navy,
                          spaceBefore=10, spaceAfter=6, keepWithNext=1),
        3: ParagraphStyle("h3", parent=base, fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=card,
                          spaceBefore=8, spaceAfter=4, keepWithNext=1),
    }
    bullet = ParagraphStyle("bullet", parent=base, leftIndent=16, bulletIndent=3, spaceAfter=3)
    cell = ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11, spaceAfter=0)
    head_cell = ParagraphStyle("headcell", parent=cell, textColor=colors.white)
    code_style = ParagraphStyle("code", parent=base, fontName="Courier", fontSize=8.5, leading=11,
                                backColor=colors.HexColor("#F1F4F9"), borderPadding=4, spaceAfter=8)

    avail = width - 2 * margin
    story: list = [Spacer(1, 1), NextPageTemplate("body"), PageBreak()]
    for block in _parse_markdown(str(doc.get("markdown") or "")):
        kind = block[0]
        if kind == "h":
            story.append(Paragraph(_pdf_inline(block[2], bold_all=False), styles[min(block[1], 3)]))
        elif kind == "p":
            story.append(Paragraph(_pdf_inline(block[1]), base))
        elif kind == "list":
            for marker, text in block[1]:
                story.append(Paragraph(_pdf_inline(text), bullet, bulletText=_latin1(marker)))
        elif kind == "hr":
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#C9D1DC"), spaceBefore=4,
                                    spaceAfter=8))
        elif kind == "code":
            wrapped = "\n".join(w for ln in _latin1(block[1]).split("\n") for w in (textwrap.wrap(ln, 95) or [""]))
            story.append(Preformatted(wrapped, code_style))
        elif kind == "table":
            rows = block[1]
            data = [[Paragraph(_pdf_inline(c, bold_all=(r == 0)), head_cell if r == 0 else cell) for c in row]
                    for r, row in enumerate(rows)]
            t = Table(data, colWidths=[avail / len(rows[0])] * len(rows[0]), repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), navy),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C2D0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FB")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(t)
            story.append(Spacer(1, 8))

    buf = io.BytesIO()
    pdf = BaseDocTemplate(buf, pagesize=A4, title=title, author=company, leftMargin=margin, rightMargin=margin,
                          topMargin=20 * mm, bottomMargin=22 * mm)
    pdf.addPageTemplates([
        PageTemplate(id="cover", frames=[Frame(margin, margin, avail, 50 * mm, id="cover")], onPage=cover),
        PageTemplate(id="body", frames=[Frame(margin, 22 * mm, avail, height - 42 * mm, id="body")],
                     onPage=body_page),
    ])
    pdf.build(story)
    return buf.getvalue()


def render_docx(doc: dict[str, Any], company: str) -> bytes:
    """Word version of the same document (python-docx, pure Python): title page, then the body with page numbers."""
    from docx import Document as Word
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    navy, teal = RGBColor(0x0B, 0x1F, 0x4B), RGBColor(0x2F, 0xA4, 0xB8)
    company = company or "Salts Fire and Security"
    title = str(doc.get("title") or "Document")

    def clean(s: str) -> str:
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s or "")

    def add_runs(par, text: str, bold_all: bool = False, size: float | None = None) -> None:
        for piece, bold, italic, code in _inline_tokens(clean(text)):
            run = par.add_run(piece)
            run.bold = bold or bold_all or None
            run.italic = italic or None
            if code:
                run.font.name = "Courier New"
            if size:
                run.font.size = Pt(size)

    w = Word()
    w.core_properties.title = clean(title)
    w.core_properties.author = clean(company)
    w.styles["Normal"].font.name = "Calibri"
    w.styles["Normal"].font.size = Pt(10.5)

    # Title page
    if LOGO_PATH.exists():
        p = w.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(str(LOGO_PATH), width=Inches(5.5))
    for text, size, color, bold, after in (
            (KIND_LABELS.get(str(doc.get("kind")), "Document").upper(), 12, teal, True, 6),
            (title, 28, navy, True, 12),
            (_pretty_date(str(doc.get("created_at") or "")), 11, None, False, 60),
            (company, 16, navy, True, 4),
            (TAGLINE, 10, teal, True, 0)):
        p = w.add_paragraph()
        p.paragraph_format.space_before = Pt(40) if text == title else Pt(0)
        p.paragraph_format.space_after = Pt(after)
        run = p.add_run(clean(text))
        run.bold = bold
        run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = color
    w.add_page_break()

    for block in _parse_markdown(str(doc.get("markdown") or "")):
        kind = block[0]
        if kind == "h":
            h = w.add_heading(level=min(block[1], 4))
            add_runs(h, block[2])
        elif kind == "p":
            add_runs(w.add_paragraph(), block[1])
        elif kind == "list":
            for marker, text in block[1]:
                add_runs(w.add_paragraph(style="List Bullet" if marker == "•" else "List Number"), text)
        elif kind == "hr":
            w.add_paragraph("_" * 60)
        elif kind == "code":
            run = w.add_paragraph().add_run(clean(block[1]))
            run.font.name = "Courier New"
            run.font.size = Pt(9)
        elif kind == "table":
            rows = block[1]
            table = w.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Table Grid"
            for r, row in enumerate(rows):
                for c, text in enumerate(row):
                    par = table.cell(r, c).paragraphs[0]
                    add_runs(par, text, bold_all=(r == 0), size=9)
            w.add_paragraph()

    # Footer with page numbers (not on the title page)
    section = w.sections[0]
    section.different_first_page_header_footer = True
    fp = section.footer.paragraphs[0]
    fp.add_run(clean(f"{company}  |  Page ")).font.size = Pt(8.5)
    run = fp.add_run()
    run.font.size = Pt(8.5)
    begin, instr, end = OxmlElement("w:fldChar"), OxmlElement("w:instrText"), OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end.set(qn("w:fldCharType"), "end")
    for el in (begin, instr, end):
        run._r.append(el)

    buf = io.BytesIO()
    w.save(buf)
    return buf.getvalue()


async def _safe(coro, label: str) -> Any:
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 - one failed data source shouldn't sink the whole draft
        log.warning("bid_assessment input %s failed: %s", label, e)
        return {"error": f"{type(e).__name__}: {e}"[:200]}

RAMS_SYSTEM = """You are Jarvis, drafting a Risk Assessment and Method Statement (RAMS) for {company}, a UK fire &
security contractor. Use the job details and the knowledge extracts provided. Output markdown with:
1. Job details (site, client, scope, date, engineers, emergency contacts: TO CONFIRM).
2. Scope and sequence of works (method statement) specific to the system type and job type, including isolation
   and liaison with the responsible person / ARC before testing, cause & effect testing, reinstatement and handover.
3. Risk assessment table: hazard | who is at risk | controls | residual risk (L/M/H). Cover working at height,
   electrical isolation, asbestos (check the asbestos register before drilling), lone working, occupied premises
   (false alarms, vulnerable occupants in care homes/schools), manual handling, dust, hot works if any, driving.
4. PPE, tools and access equipment; permits required.
5. Competence required (e.g. FIA / ECS cards) and emergency arrangements.
6. Sign-off block for engineer, supervisor and client.
Be specific and practical. Mark anything you don't know as TO CONFIRM - never invent site facts."""

PQQ_SYSTEM = """You are Jarvis, answering a tender / pre-qualification questionnaire (PQQ, SQ, Constructionline,
CHAS/SafeContractor-style) for {company}. Using ONLY the evidence provided, answer each question in the first
person plural, concisely and persuasively, and cite the evidence (accreditation, policy, insurance, competency,
records). Output markdown: a table | Q | Answer | Evidence | Status | where Status is Ready, Needs document, or
TO CONFIRM. Then list the documents to attach and the gaps to close. Never invent certificate numbers, figures,
policies or dates - mark them TO CONFIRM."""


RECRUITMENT_SYSTEM = """You are Jarvis, drafting recruitment material for {company}, a UK fire & security
installer/maintainer, for a role {owner} needs to fill. Use the staff register (how similar roles here are
actually described and measured) for tone and realistic expectations, and current UK employment law (right
to work, minimum wage, discrimination in job ads) where relevant.

Output markdown with two clearly headed parts:
1. **Job posting** - a real, publishable advert: role, what the job actually involves day to day at a UK fire
   & security SME (not generic corporate filler), person specification (essential vs desirable - qualifications
   like FIA/ECS where relevant, experience, driving licence if needed), what we offer, how to apply.
2. **Interview questions** - 8-10 questions mixing technical/role competence and behavioural, each with what a
   strong answer actually looks like for this role, plus 2-3 legally-safe questions to probe reliability/
   punctuality/safety attitude without straying into protected characteristics.
Be specific to the role given, not generic. Mark anything you're guessing at (salary, exact requirements) as
TO CONFIRM rather than inventing figures."""

HR_LETTER_SYSTEM = """You are Jarvis, drafting an HR letter/document for {company}, a UK fire & security
employer, for {owner} to review before it's sent - never send this yourself. Ground it in current UK
employment law (ACAS code of practice on disciplinary/grievance procedures, statutory notice, right to be
accompanied) and the real facts given; never invent dates, incidents or figures - mark anything missing as
TO CONFIRM.

Match the letter type requested (e.g. invite to a disciplinary/investigation meeting, written warning
confirmation, performance improvement plan, reference letter, probation outcome) to the correct ACAS-compliant
structure and tone: factual, proportionate, never pre-judging an outcome that hasn't been decided at a
meeting yet. Output markdown: the letter/document itself, then a short "Before sending" checklist of what
{owner} should double-check or that a solicitor should review this if the situation could end in dismissal or
looks legally contentious."""


BID_ASSESSMENT_SYSTEM = """You are Jarvis, giving {owner} a candid go/no-go and pricing recommendation for a
tender opportunity at {company}, a UK fire & security installer/maintainer - using the real business data
provided (cash position, team capacity/utilisation, quote win rate, customer concentration), not guesswork.

Structure your answer:
1. **Recommendation**: Bid or don't bid - one clear line, then why.
2. **Capacity**: Can the team actually deliver this on top of current work, based on the utilisation/workload
   data given? Flag if it would mean turning away or delaying other work.
3. **Pricing**: A suggested price range and margin, reasoned from typical UK fire & security margins (label
   these as typical ranges, not certainties) and the value/scope given - not a single invented number
   presented as precise.
4. **Risk**: Customer concentration (would winning this make one client too large a share of revenue?),
   payment/cash timing, competition if named, anything in the business data that raises a flag.
5. **If bidding, what to emphasise** - 2-3 concrete points that would make this bid actually win, given our
   real track record and quote conversion rate.
Be direct - this is a decision aid, not a cheerleading exercise. Mark anything you don't have real data for
as an assumption, not a fact."""

BID_DOCUMENT_SYSTEM = """You are Jarvis, drafting a full tender/proposal document for {company}, a UK fire &
security installer/maintainer, responding to a real opportunity - not just answering a PQQ's individual
questions (that's a separate tool), but writing the actual submission document.

Use ONLY the real evidence given: accreditations, company documents, and comparable past jobs (as case
studies - reference real job types/systems/scale, never invented client names or figures). Output markdown:
1. Cover letter / introduction - who we are, why we're a strong fit for this specific opportunity.
2. Understanding of requirements - reflect the brief back to show we've actually read it.
3. Proposed approach / methodology - how we'd deliver this, referencing our real accreditations and
   competencies.
4. Relevant experience - 2-4 case studies drawn from the comparable past jobs given, described generically
   enough to respect client confidentiality (system type, scale, outcome) unless the job data itself names
   the client.
5. Pricing summary - a placeholder structure (labour/materials/ongoing maintenance) for {owner} to fill in
   with real figures, not invented numbers.
6. Compliance & accreditation summary, and next steps.
Mark any gap as TO CONFIRM. Never invent a client name, contract value, or accreditation we don't hold."""


class Documents:
    def __init__(self, j):
        self.j = j

    def _save(self, kind: str, title: str, text: str) -> str | None:
        """Store the draft (so it can be downloaded as PDF/Word later) and show it on the HUD."""
        doc_id: str | None = uuid.uuid4().hex
        try:
            self.j.db.add_document(doc_id, kind, title, text)
        except Exception as e:  # noqa: BLE001 - failing to store must never lose the draft itself
            log.warning("could not store drafted document %r: %s", title, e)
            doc_id = None
        payload: dict[str, Any] = {"title": title, "markdown": text}
        if doc_id:
            payload["doc_id"] = doc_id
        self.j.bus.publish("display", payload)
        return doc_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return self.j.db.get_document(doc_id) if valid_doc_id(doc_id) else None

    async def _find_job(self, job_ref: str) -> dict[str, Any] | None:
        today = date.today()
        for jb in await self.j.fsm.jobs(today - timedelta(days=60), today + timedelta(days=60)):
            if str(jb.get("ref") or jb.get("id")).lower() == job_ref.lower():
                return jb
        return None

    async def rams(self, job_ref: str | None = None, description: str | None = None) -> str:
        j = self.j
        job = await self._find_job(job_ref) if job_ref else None
        if job_ref and not job and not description:
            return f"I couldn't find job {job_ref} in Salts FSM - describe the work and I'll draft the RAMS."
        systems = []
        if job:
            systems = [s for s in await j.fsm.systems() if s.get("site") == job.get("site")]
        topic = " ".join(filter(None, [str((job or {}).get("type") or ""), description or "",
                                       " ".join(str(s.get("type")) for s in systems)]))
        knowledge = j.kb.search(topic + " working at height asbestos testing isolation", limit=6)
        text = await llm.write(
            j.client, j.settings, system=RAMS_SYSTEM.format(company=j.settings.company_name),
            prompt=json.dumps({"job": job, "description": description, "systems_on_site": systems,
                               "knowledge_extracts": knowledge}, default=str)[:60000],
            effort="medium", max_tokens=12000)
        self._save("rams", f"RAMS - {(job or {}).get('ref') or 'draft'}", text)
        return text

    async def questionnaire(self, questions: str, buyer: str | None = None) -> str:
        j = self.j
        evidence = {}
        for scheme in ("BAFE", "SSAIB", "CHAS"):
            try:
                evidence[scheme] = await j.accreditations.gather_evidence(scheme)
            except Exception as e:  # noqa: BLE001
                evidence[scheme] = {"error": str(e)[:200]}
        company = j.kb.core_documents()[:30000]
        text = await llm.write(
            j.client, j.settings, system=PQQ_SYSTEM.format(company=j.settings.company_name),
            prompt=(f"Buyer: {buyer or 'not stated'}\n\n<questions>\n{questions[:40000]}\n</questions>\n\n"
                    f"<company_documents>\n{company}\n</company_documents>\n\n"
                    f"<evidence>\n{json.dumps(evidence, default=str)[:60000]}\n</evidence>"),
            effort="high", max_tokens=16000)
        self._save("questionnaire", f"Questionnaire answers{' - ' + buyer if buyer else ''}", text)
        return text

    async def recruitment(self, role: str, notes: str | None = None) -> str:
        j = self.j
        text = await llm.write(
            j.client, j.settings, system=RECRUITMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"role": role, "notes": notes, "staff_register": j.register.prompt_summary()},
                              default=str)[:40000],
            effort="medium", max_tokens=8000)
        self._save("recruitment", f"Recruitment - {role}", text)
        return text

    async def hr_letter(self, kind: str, person: str, details: str) -> str:
        j = self.j
        knowledge = j.kb.search(f"{kind} disciplinary employment law ACAS", limit=4)
        text = await llm.write(
            j.client, j.settings, system=HR_LETTER_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"letter_type": kind, "person": person, "details": details,
                               "knowledge_extracts": knowledge}, default=str)[:40000],
            effort="medium", max_tokens=8000)
        self._save("hr_letter", f"HR - {kind} ({person})", text)
        return text

    async def bid_assessment(self, opportunity: str, value: float | None, notes: str | None = None) -> str:
        j = self.j
        business = await _safe(j.advisor.gather(), "business data")
        text = await llm.write(
            j.client, j.settings, system=BID_ASSESSMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"opportunity": opportunity, "estimated_value": value, "notes": notes,
                               "business_data": business}, default=str)[:60000],
            effort="high", max_tokens=8000)
        self._save("bid_assessment", f"Bid assessment - {opportunity}", text)
        return text

    async def bid_document(self, opportunity: str, client: str | None, requirements: str,
                           notes: str | None = None) -> str:
        j = self.j
        evidence = {}
        for scheme in ("BAFE", "SSAIB", "CHAS"):
            try:
                evidence[scheme] = await j.accreditations.gather_evidence(scheme)
            except Exception as e:  # noqa: BLE001
                evidence[scheme] = {"error": str(e)[:200]}
        today = date.today()
        comparable_jobs = []
        try:
            jobs = await j.fsm.jobs(today - timedelta(days=730), today, status="completed")
            comparable_jobs = jobs[:15]  # a sample - the model picks what's actually relevant as case studies
        except Exception as e:  # noqa: BLE001
            comparable_jobs = [{"error": str(e)[:200]}]
        company = j.kb.core_documents()[:30000]
        text = await llm.write(
            j.client, j.settings, system=BID_DOCUMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"opportunity": opportunity, "client": client, "requirements": requirements,
                               "notes": notes, "company_documents": company, "evidence": evidence,
                               "comparable_past_jobs": comparable_jobs}, default=str)[:70000],
            effort="high", max_tokens=16000)
        self._save("bid_document", f"Bid document - {opportunity}", text)
        return text
