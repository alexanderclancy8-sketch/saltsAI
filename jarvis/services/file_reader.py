"""Reading files that come from outside the company - email attachments and files attached in the console.

Everything here is pure Python (pypdf, python-docx, openpyxl and the standard library), because the app runs on Azure
App Service Linux where nothing can be apt-installed: no tesseract, no poppler, no ffmpeg.

What lives here, and why it is separate from ``documents.py``:

* ``sniff`` - what a file REALLY is, from its first bytes (and, for the zip-based Office formats, the parts inside it),
  never from its name or the browser's / Graph's claimed type.
* ``FileProblem`` - every refusal carries a plain-English sentence, so the owner is told what went wrong with which file
  ("this PDF is password protected") and not "I can't open it".
* ``pptx_to_markdown`` - slide titles, text, tables and speaker notes straight from the XML (no python-pptx needed).
* PDF helpers - the text layer (``pdf_extract``), page chunks and page images for the scan fallback.

Files read here are UNTRUSTED. Nothing in this module executes anything inside a file: Office files are only unzipped and
their XML read (a ``vbaProject.bin`` macro part is never touched), and a zip-bomb guard caps entries and inflated size.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------------------------------------------- limits
MAX_ZIP_ENTRIES = 3000          # an Office file has tens to a few hundred parts
MAX_UNZIPPED_BYTES = 50_000_000  # total inflated size of an Office zip
MAX_XML_PART_BYTES = 25_000_000  # one XML part
MAX_READ_CHARS = 60_000
MAX_PDF_PAGES = 30              # pages of text read from a PDF
MAX_SLIDES = 200

EMAIL_PDF_MAX_BYTES = 25_000_000   # a PDF attached to an email
EMAIL_OFFICE_MAX_BYTES = 15_000_000
UPLOAD_MAX_FILE_BYTES = 20_000_000  # a file attached in the console
UPLOAD_MAX_TOTAL_BYTES = 25_000_000
UPLOAD_MAX_FILES = 5

# control characters (except tab / newline / carriage return) and the invisible bidi overrides that can hide text
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f‪-‮⁦-⁩]")


def clean_text(text: str) -> str:
    """Text from an untrusted file with the control characters removed."""
    return _CONTROL.sub("", text or "")


def limit_text(text: str, limit: int = MAX_READ_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n\n…[truncated]"


def human_size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 100_000 else f"{max(n, 1) / 1000:.0f} KB"


class FileProblem(ValueError):
    """A file that can't be read, with a plain sentence saying why (no file name in it - the caller adds that).

    ``code`` is a stable word for tests and for choosing wording: not_pdf, password, corrupt, no_pages, dependency,
    scan_unavailable, empty, too_large, link, item, download_failed, legacy, macro, unsupported, mismatch, zip_bomb."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def describe_failure(name: str, exc: BaseException) -> str:
    """The sentence the owner sees for a file that failed: names the file and says what went wrong."""
    label = f"'{(name or 'the file').strip()[:80]}'"
    if isinstance(exc, FileProblem):
        return f"I couldn't read {label}: {exc.message}"
    if isinstance(exc, ValueError):
        return f"I couldn't read {label}: {str(exc)[:160]}"
    return f"I couldn't read {label}: something unexpected went wrong ({type(exc).__name__})."


# ------------------------------------------------------------------------------------------------------------ sniffing
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
KIND_LABEL = {"pdf": "a PDF", "docx": "a Word (.docx) document", "xlsx": "an Excel (.xlsx) workbook",
              "pptx": "a PowerPoint (.pptx) presentation", "zip": "a zip archive", "ole": "an old-format Office file",
              "png": "a PNG image", "jpeg": "a JPEG image", "gif": "a GIF image", "webp": "a WebP image",
              "text": "a plain text file", "binary": "a file of unknown type"}
_EXT_KIND = {"pdf": "pdf", "docx": "docx", "xlsx": "xlsx", "pptx": "pptx", "png": "png", "jpg": "jpeg", "jpeg": "jpeg",
             "gif": "gif", "webp": "webp", "txt": "text", "csv": "text", "md": "text", "json": "text"}
LEGACY_EXT = {"doc": "Word (.doc)", "xls": "Excel (.xls)", "ppt": "PowerPoint (.ppt)"}
LEGACY_TO = {"doc": ".docx", "xls": ".xlsx", "ppt": ".pptx"}
MACRO_EXT = ("docm", "dotm", "xlsm", "xlsb", "xltm", "pptm", "potm", "ppsm")
OFFICE_KINDS = ("docx", "xlsx", "pptx")


def ext_of(name: str) -> str:
    base = (name or "").strip().lower()
    return base.rsplit(".", 1)[-1] if "." in base else ""


def _zip_kind(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except (zipfile.BadZipFile, ValueError, OSError):
        return "binary"
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    return "zip"


def _is_text(data: bytes) -> bool:
    head = data[:8192]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        # a multi-byte character cut off at the end of the sample is still text
        return e.start >= len(head) - 3 or sum(1 for b in head if b < 9 or 13 < b < 32) == 0


def sniff(data: bytes) -> str:
    """What the bytes are: pdf, docx, xlsx, pptx, zip, ole, png, jpeg, gif, webp, text or binary."""
    data = data or b""
    head = data[:16]
    stripped = data[:1024].lstrip()
    if stripped.startswith(b"%PDF-"):
        return "pdf"
    if b"%PDF-" in data[:1024] and (b"%%EOF" in data[-2048:] or b"startxref" in data[-2048:]):
        return "pdf"  # a PDF with a few bytes of junk in front of the header (some scanners and mail gateways add them)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _zip_kind(data)
    if head[:8] == _OLE:
        return "ole"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if not data:
        return "text"
    return "text" if _is_text(data) else "binary"


def classify_upload(name: str, data: bytes) -> str:
    """The kind of a file attached in the console, or a FileProblem saying (in plain words) why it is refused.

    The extension decides what the owner MEANT; the first bytes decide what the file IS; the two must agree."""
    ext = ext_of(name)
    if ext in LEGACY_EXT:
        raise FileProblem("legacy", f"old {LEGACY_EXT[ext]} files can't be read reliably - open it and use Save As "
                                    f"{LEGACY_TO[ext]}, then attach that copy.")
    if ext in MACRO_EXT:
        raise FileProblem("macro", "macro-enabled Office files are not opened. Save a copy as a normal .docx / .xlsx / "
                                   ".pptx (without macros) and attach that.")
    want = _EXT_KIND.get(ext)
    if want is None:
        raise FileProblem("unsupported", "I can read photos (PNG, JPG, GIF, WebP), PDFs, Word (.docx), Excel (.xlsx), "
                                         "PowerPoint (.pptx) and plain text (.txt, .csv, .md, .json) files, not "
                                         f"'.{ext}' files." if ext else
                                         "it has no file extension, so I can't tell what it is. Add .pdf, .docx, .xlsx or "
                                         ".pptx to the name, or use one of the supported types.")
    got = sniff(data)
    if got == want:
        return got
    if got == "ole":
        raise FileProblem("legacy", f"it is named '.{ext}' but it's an old-format Office file inside. Open it and use "
                                    "Save As the newer format (.docx / .xlsx / .pptx), then attach that copy.")
    raise FileProblem("mismatch", f"it is named '.{ext}' but the contents look like {KIND_LABEL.get(got, 'something else')}"
                                  f", not {KIND_LABEL[want]}. Check it is the right file, or re-save it in the right format.")


def kind_for_email(name: str, data: bytes) -> str:
    """The kind of an email attachment: the CONTENT decides (names and Graph content types are often wrong). Raises a
    FileProblem for the legacy / macro formats, with the same plain wording as the console uses."""
    ext = ext_of(name)
    if ext in LEGACY_EXT or ext in MACRO_EXT:
        return classify_upload(name, data)
    got = sniff(data)
    if got == "ole":
        raise FileProblem("legacy", "it's an old-format Office file (.doc / .xls / .ppt), which can't be read reliably - "
                                    "ask for it saved as .docx / .xlsx / .pptx.")
    return got


# ------------------------------------------------------------------------------------------------------------ zip safety
def open_office_zip(data: bytes, label: str, max_unzipped: int | None = None) -> zipfile.ZipFile:
    """An Office (.docx / .xlsx / .pptx) zip, refused if it is not a zip or would inflate absurdly (a zip bomb).
    Only ever read as XML afterwards: nothing in it is executed."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise FileProblem("corrupt", f"it isn't a valid {label} file (the file is damaged, or not really a {label} file)."
                          ) from None
    infos = z.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        z.close()
        raise FileProblem("zip_bomb", f"the {label} file holds an unusual number of parts ({len(infos)}), so I won't "
                                      "open it.")
    cap = MAX_UNZIPPED_BYTES if max_unzipped is None else max_unzipped
    if sum(i.file_size for i in infos) > cap or any(i.file_size > MAX_XML_PART_BYTES for i in infos
                                                                    if i.filename.endswith((".xml", ".rels"))):
        z.close()
        raise FileProblem("zip_bomb", f"the {label} file is far larger inside than it looks (it expands to over "
                                      f"{max(cap // 1_000_000, 1)} MB), so I won't open it.")
    return z


# ------------------------------------------------------------------------------------------------------------- PowerPoint
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


_BAD_XML_BYTES = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]|&#(?:x0*(?:[0-8bcef]|1[0-9a-f])|0*(?:[0-8]|11|12|1[4-9]|2[0-9]|3[01]));", re.I)


def _xml(z: zipfile.ZipFile, part: str) -> ET.Element | None:
    """One XML part parsed safely: a DOCTYPE / entity declaration (the 'billion laughs' trick) is refused outright."""
    try:
        raw = z.read(part)
    except KeyError:
        return None
    if b"<!ENTITY" in raw or b"<!DOCTYPE" in raw:
        raise FileProblem("zip_bomb", "the file contains XML that tries to define entities, so I won't open it.")
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        pass
    # XML 1.0 forbids control characters, but real files sometimes carry one (a stray bell character in pasted text): drop them and retry
    try:
        return ET.fromstring(_BAD_XML_BYTES.sub(b"", raw))
    except ET.ParseError:
        return None


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """relationship id -> target path (resolved against the part's folder) for a part's .rels file."""
    folder, _, fname = part.rpartition("/")
    root = _xml(z, f"{folder}/_rels/{fname}.rels" if folder else f"_rels/{fname}.rels")
    out: dict[str, str] = {}
    if root is None:
        return out
    for rel in root.iter(f"{_REL}Relationship"):
        rid, target = rel.get("Id"), rel.get("Target") or ""
        if not rid or rel.get("TargetMode") == "External":
            continue
        parts: list[str] = [] if target.startswith("/") else (folder.split("/") if folder else [])
        for seg in target.lstrip("/").split("/"):
            if seg == "..":
                if parts:
                    parts.pop()
            elif seg and seg != ".":
                parts.append(seg)
        out[rid] = "/".join(parts)
    return out


def _para_text(p: ET.Element) -> str:
    out: list[str] = []
    for node in p.iter():
        if node.tag == f"{_A}t" and node.text:
            out.append(node.text)
        elif node.tag in (f"{_A}br",):
            out.append("\n")
    return clean_text("".join(out)).strip()


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("|", "/")).strip()


def _shape_lines(node: ET.Element, lines: list[str], tables: list[str], *, skip_ph: tuple[str, ...]) -> str:
    """Walk a slide's shape tree in order: text frames (placeholders of the types in ``skip_ph`` left out) and tables.
    Returns the slide's title text, if it has a title placeholder."""
    title = ""
    for child in node:
        if child.tag == f"{_P}sp":
            ph = child.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
            ptype = (ph.get("type") if ph is not None else "") or ("body" if ph is not None else "")
            body = child.find(f"{_P}txBody")
            if body is None or ptype in skip_ph:
                continue
            paras = [t for t in (_para_text(p) for p in body.findall(f"{_A}p")) if t]
            if not paras:
                continue
            if ptype in ("title", "ctrTitle"):
                title = title or " ".join(paras)
            else:
                lines.extend(paras)
        elif child.tag == f"{_P}graphicFrame":
            rows = []
            for tr in child.iter(f"{_A}tr"):
                rows.append([_cell(" ".join(t for t in (_para_text(p) for p in tc.iter(f"{_A}p")) if t))
                             for tc in tr.findall(f"{_A}tc")])
            if rows and any(any(c for c in r) for r in rows):
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                md = ["| " + " | ".join(r) + " |" for r in rows]
                md.insert(1, "|" + "---|" * width)
                tables.append("\n".join(md))
                lines.append(f"\x00TABLE{len(tables) - 1}\x00")
        elif child.tag == f"{_P}grpSp":
            title = title or _shape_lines(child, lines, tables, skip_ph=skip_ph)
    return title


def pptx_to_markdown(data: bytes) -> str:
    """Text of a PowerPoint file as markdown: per slide a '## Slide N: title' heading, its text frames and tables in
    order, then the speaker notes. Pictures, charts and animations are not read. Capped at MAX_SLIDES / MAX_READ_CHARS."""
    z = open_office_zip(data, "PowerPoint")
    try:
        pres = _xml(z, "ppt/presentation.xml")
        if pres is None:
            raise FileProblem("corrupt", "it isn't a valid PowerPoint file (the presentation part is missing or damaged).")
        rels = _rels(z, "ppt/presentation.xml")
        slide_parts = [rels[s.get(f"{_R}id")] for s in pres.iter(f"{_P}sldId") if s.get(f"{_R}id") in rels]
        if not slide_parts:
            slide_parts = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                                 key=lambda n: int(re.findall(r"\d+", n)[-1]))
        if not slide_parts:
            raise FileProblem("no_pages", "the presentation has no slides.")
        out: list[str] = []
        for n, part in enumerate(slide_parts[:MAX_SLIDES], 1):
            root = _xml(z, part)
            if root is None:
                out.append(f"## Slide {n}\n\n_(this slide could not be read)_")
                continue
            lines: list[str] = []
            tables: list[str] = []
            tree = root.find(f"{_P}cSld/{_P}spTree")
            title = _shape_lines(tree, lines, tables, skip_ph=("sldNum", "dt", "ftr", "sldImg")) if tree is not None else ""
            hidden = " (hidden slide)" if root.get("show") == "0" else ""
            block = [f"## Slide {n}{': ' + _cell(title) if title else ''}{hidden}"]
            for ln in lines:
                m = re.fullmatch(r"\x00TABLE(\d+)\x00", ln)
                block.append(tables[int(m.group(1))] if m else ln)
            notes = ""
            for target in _rels(z, part).values():
                if target.startswith("ppt/notesSlides/"):
                    nroot = _xml(z, target)
                    ntree = nroot.find(f"{_P}cSld/{_P}spTree") if nroot is not None else None
                    if ntree is not None:
                        nlines: list[str] = []
                        _shape_lines(ntree, nlines, [], skip_ph=("sldNum", "sldImg", "dt", "ftr", "hdr"))
                        notes = "\n".join(nlines)
            if notes.strip():
                block.append("**Speaker notes:** " + notes.strip())
            out.append("\n\n".join(block))
        if len(slide_parts) > MAX_SLIDES:
            out.append(f"_(truncated: only the first {MAX_SLIDES} of {len(slide_parts)} slides are shown)_")
        return limit_text("\n\n".join(out))
    finally:
        z.close()


# ------------------------------------------------------------------------------------------------------------------ PDFs
@dataclass
class PdfText:
    text: str
    pages_total: int
    pages_read: int
    empty_pages: list[int] = field(default_factory=list)  # 1-based pages read that had no text layer


def _pypdf():
    try:
        import pypdf
    except ImportError:
        raise FileProblem("dependency", "the PDF-reading component (pypdf) isn't installed on this server. Ask whoever "
                                        "looks after Jarvis to check the deployment - this file itself is fine.") from None
    return pypdf


def _open_pdf(data: bytes):
    """A pypdf reader on the PDF, decrypted if it has no password. Raises FileProblem in plain words otherwise."""
    if sniff(data) != "pdf":
        raise FileProblem("not_pdf", "it isn't a valid PDF file (it may be damaged, or a different kind of file named "
                                     ".pdf).")
    pypdf = _pypdf()
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception as e:  # noqa: BLE001 - e.g. pypdf's DependencyError for an AES PDF with no crypto library
                if "cryptography" in str(e).lower() or "pycryptodome" in str(e).lower() or "dependency" in type(e).__name__.lower():
                    raise FileProblem("dependency", "this PDF is encrypted with a method that needs a decryption "
                                                    "component which isn't installed on this server (cryptography). "
                                                    "Ask whoever looks after Jarvis, or send an unprotected copy.") from None
                raise FileProblem("password", "this PDF is password protected, so I can't open it. Send me an "
                                              "unprotected copy (or the password-free version).") from None
            if not ok:
                raise FileProblem("password", "this PDF is password protected, so I can't open it. Send me an "
                                              "unprotected copy (or the password-free version).")
        if len(reader.pages) == 0:
            raise FileProblem("no_pages", "the PDF has no pages.")
        return reader
    except FileProblem:
        raise
    except Exception as e:  # noqa: BLE001 - a corrupt/odd file is the sender's problem, not a crash
        raise FileProblem("corrupt", f"the PDF looks damaged and I couldn't open it ({type(e).__name__}).") from e


def pdf_extract(data: bytes, max_pages: int = MAX_PDF_PAGES, max_chars: int = MAX_READ_CHARS) -> PdfText:
    reader = _open_pdf(data)
    parts: list[str] = []
    empty: list[int] = []
    total, read = 0, 0
    pages = reader.pages
    count = len(pages)
    try:
        for n, page in enumerate(pages, 1):
            if n > max_pages or total > max_chars:
                parts.append(f"…[truncated: only the first {n - 1} of {count} pages are read]")
                break
            read = n
            text = clean_text(page.extract_text() or "").strip()
            if text:
                parts.append(f"--- page {n} ---\n{text}")
                total += len(text)
            else:
                empty.append(n)
    except FileProblem:
        raise
    except Exception as e:  # noqa: BLE001
        raise FileProblem("corrupt", f"the PDF looks damaged part-way through and I couldn't read it "
                                     f"({type(e).__name__}).") from e
    return PdfText(limit_text("\n\n".join(parts), max_chars), count, read, empty)


_PAGE_MARK = re.compile(r"--- page \d+ ---")
MIN_PDF_TEXT_CHARS = 40  # less text than this in a whole PDF = a scan (pictures, no text layer)


def has_text(text: str, minimum: int = MIN_PDF_TEXT_CHARS) -> bool:
    """True if text extracted from a PDF holds real content (not just page markers / whitespace)."""
    return len(re.sub(r"\s+", "", _PAGE_MARK.sub("", text or ""))) >= minimum


def pdf_page_count(data: bytes) -> int | None:
    """Pages in the PDF, or None if it can't be opened (best effort; never raises)."""
    try:
        return len(_open_pdf(data).pages)
    except Exception:  # noqa: BLE001
        return None


def pdf_chunks(data: bytes, size: int, max_pages: int) -> tuple[list[bytes], int | None]:
    """The first ``max_pages`` pages of a PDF as separate PDFs of up to ``size`` pages (a small file the model can take in
    one go), plus the real page count. A PDF that fits in one chunk is returned untouched; one that can't be split (or
    parsed) is returned as it is, with a page count of None."""
    try:
        reader = _open_pdf(data)
    except Exception:  # noqa: BLE001
        return [data], None
    count = len(reader.pages)
    if count <= size and count <= max_pages:
        return [data], count
    try:
        pypdf = _pypdf()
        out: list[bytes] = []
        for start in range(0, min(count, max_pages), size):
            w = pypdf.PdfWriter()
            for i in range(start, min(start + size, count, max_pages)):
                w.add_page(reader.pages[i])
            buf = io.BytesIO()
            w.write(buf)
            out.append(buf.getvalue())
        return out, count
    except Exception:  # noqa: BLE001 - can't split it: send the original
        return [data], count


def pdf_page_images(data: bytes, max_pages: int = 8, max_edge: int = 1600) -> list[bytes]:
    """The pages of a SCANNED PDF as JPEG images (the largest picture on each page, shrunk to ``max_edge`` pixels), so a
    model can read them without any PDF renderer on the server. Scans are almost always one full-page picture per page.
    Returns [] if the pages have no extractable pictures (or Pillow isn't available) - the caller then sends the PDF."""
    try:
        from PIL import Image

        reader = _open_pdf(data)
        out: list[bytes] = []
        for page in list(reader.pages)[:max_pages]:
            best = None
            for img in page.images:
                pil = img.image
                if best is None or pil.width * pil.height > best.width * best.height:
                    best = pil
            if best is None:
                return []
            im = best.convert("L" if best.mode in ("1", "L") else "RGB")
            im.thumbnail((max_edge, max_edge))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=72)
            out.append(buf.getvalue())
        return out
    except Exception:  # noqa: BLE001 - not extractable: fall back to the whole PDF
        return []
