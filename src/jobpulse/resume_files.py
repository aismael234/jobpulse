"""Read a resume file (PDF, DOCX, plain text or Markdown) as plain text.

Files are capped at 2 MB and PDFs at 10 pages. A DOCX is read with the standard library: its
``word/document.xml`` is refused if it declares a DOCTYPE (no entity expansion) or unpacks
past the size cap. List paragraphs come back with a "• " bullet, so ``resume_lines`` finds
them. Callers in an event loop run this in a thread: PDF parsing is CPU-bound.
"""

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

MAX_BYTES = 2 * 1024 * 1024
MAX_PDF_PAGES = 10
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": DOCX_MEDIA_TYPE,
    ".txt": "text/plain",
    ".md": "text/markdown",
}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ResumeFileError(ValueError):
    """A resume file that can't be read; the message says why and names no content."""


@dataclass(frozen=True, slots=True)
class ResumeText:
    text: str
    media_type: str


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages[:MAX_PDF_PAGES]
        return "\n".join(page.extract_text() or "" for page in pages)
    except (PyPdfError, ValueError, KeyError, TypeError) as exc:
        raise ResumeFileError(f"can't read this PDF ({type(exc).__name__})") from None


def _docx_paragraph(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    text = "".join(parts).strip()
    if text and paragraph.find(f"{_W}pPr/{_W}numPr") is not None:
        text = f"• {text}"
    return text


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_BYTES:
                raise ResumeFileError("this DOCX's text is too large (over 2 MB unpacked)")
            with archive.open(info) as member:
                xml = member.read(MAX_BYTES + 1)
    except (zipfile.BadZipFile, KeyError):
        raise ResumeFileError("not a DOCX file (no word/document.xml)") from None
    if len(xml) > MAX_BYTES:
        raise ResumeFileError("this DOCX's text is too large (over 2 MB unpacked)")
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ResumeFileError("this DOCX declares a DOCTYPE, which resumes never need")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise ResumeFileError("this DOCX's document.xml isn't valid XML") from None
    paragraphs = (_docx_paragraph(p) for p in root.iter(f"{_W}p"))
    return "\n".join(p for p in paragraphs if p)


def read_resume_file(path: Path) -> ResumeText:
    """The text of a .pdf, .docx, .txt or .md resume, or ``ResumeFileError``."""
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise ResumeFileError(
            f"use a .pdf, .docx, .txt or .md file, not {path.suffix or path.name}"
        )
    try:
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ResumeFileError("the file is larger than 2 MB")
        data = path.read_bytes()
    except FileNotFoundError:
        raise ResumeFileError(f"file not found: {path}") from None
    except OSError as exc:
        raise ResumeFileError(f"can't read {path} ({type(exc).__name__})") from None
    if media_type == "application/pdf":
        text = _pdf_text(data)
    elif media_type == DOCX_MEDIA_TYPE:
        text = _docx_text(data)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ResumeFileError("the file isn't UTF-8 text") from None
    text = text.strip()
    if not text:
        raise ResumeFileError("no text found in the file (a scanned PDF has none)")
    return ResumeText(text, media_type)
