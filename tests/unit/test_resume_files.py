import io
import zipfile
from pathlib import Path

import pytest

from jobpulse.resume_files import MAX_BYTES, ResumeFileError, read_resume_file

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_bytes(document_xml: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def document(*paragraphs: str) -> str:
    return f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{"".join(paragraphs)}</w:body></w:document>'


def pdf_bytes(*lines: str) -> bytes:
    """A one-page PDF with ``lines`` in Helvetica, offsets computed so pypdf reads it cleanly."""
    text = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def test_reads_plain_text(tmp_path: Path) -> None:
    path = tmp_path / "resume.txt"
    path.write_text("﻿Built Python services.\n", encoding="utf-8")
    resume = read_resume_file(path)
    assert resume.text == "Built Python services."
    assert resume.media_type == "text/plain"


def test_reads_markdown(tmp_path: Path) -> None:
    path = tmp_path / "resume.md"
    path.write_text("# Me\n- Built Go services\n", encoding="utf-8")
    assert read_resume_file(path).media_type == "text/markdown"


def test_text_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "resume.txt"
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ResumeFileError, match="UTF-8"):
        read_resume_file(path)


def test_reads_a_pdf(tmp_path: Path) -> None:
    path = tmp_path / "resume.pdf"
    path.write_bytes(pdf_bytes("Built Python services.", "Owned PostgreSQL migrations."))
    resume = read_resume_file(path)
    assert "Built Python services." in resume.text
    assert "Owned PostgreSQL migrations." in resume.text
    assert resume.media_type == "application/pdf"


def test_a_broken_pdf_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4\nnot really")
    with pytest.raises(ResumeFileError, match="PDF"):
        read_resume_file(path)


def test_reads_docx_paragraphs_with_list_items_as_bullets(tmp_path: Path) -> None:
    path = tmp_path / "resume.docx"
    path.write_bytes(
        docx_bytes(
            document(
                "<w:p><w:r><w:t>Experience</w:t></w:r></w:p>",
                "<w:p><w:pPr><w:numPr><w:numId w:val='1'/></w:numPr></w:pPr>"
                "<w:r><w:t>Built Python</w:t></w:r><w:r><w:t xml:space='preserve'> services.</w:t></w:r></w:p>",
                "<w:p><w:r><w:t>A</w:t><w:tab/><w:t>B</w:t></w:r></w:p>",
            )
        )
    )
    resume = read_resume_file(path)
    assert resume.text == "Experience\n• Built Python services.\nA\tB"
    assert resume.media_type.endswith("wordprocessingml.document")


def test_docx_with_a_doctype_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "resume.docx"
    path.write_bytes(
        docx_bytes('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + document())
    )
    with pytest.raises(ResumeFileError, match="DOCTYPE"):
        read_resume_file(path)


def test_docx_with_an_oversized_member_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "resume.docx"
    big = document("<w:p><w:r><w:t>" + "a" * (MAX_BYTES * 3) + "</w:t></w:r></w:p>")
    path.write_bytes(docx_bytes(big))  # compresses far below MAX_BYTES
    assert path.stat().st_size < MAX_BYTES
    with pytest.raises(ResumeFileError, match="too large"):
        read_resume_file(path)


def test_a_file_that_is_not_a_docx_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "resume.docx"
    path.write_bytes(b"not a zip")
    with pytest.raises(ResumeFileError, match="DOCX"):
        read_resume_file(path)


def test_files_over_the_size_cap_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "resume.txt"
    path.write_bytes(b"a" * (MAX_BYTES + 1))
    with pytest.raises(ResumeFileError, match="larger than 2 MB"):
        read_resume_file(path)


def test_unknown_types_and_empty_files_are_refused(tmp_path: Path) -> None:
    rtf = tmp_path / "resume.rtf"
    rtf.write_text("{\\rtf1}", encoding="utf-8")
    with pytest.raises(ResumeFileError, match=r"\.pdf, \.docx, \.txt or \.md"):
        read_resume_file(rtf)
    empty = tmp_path / "resume.txt"
    empty.write_text("  \n", encoding="utf-8")
    with pytest.raises(ResumeFileError, match="no text"):
        read_resume_file(empty)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ResumeFileError, match="not found"):
        read_resume_file(tmp_path / "nope.pdf")
