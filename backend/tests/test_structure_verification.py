"""Unit tests for `agent/tools/structure_verification.py` (`verify_structure`).

Covers design.md's `verify_structure` tool description and Requirements
4.2, 4.4, 4.5:

- `.docx`: redacting a document whose matches don't touch table structure
  keeps `verify_structure` reporting `ok=True`.
- `.docx`: a "redacted" document with fewer tables than the source is
  detected as a table-count mismatch.
- `.pdf`: redacting a multi-page document keeps page count/order intact,
  so `verify_structure` reports `ok=True`.
- `.pdf`: a "redacted" document with a different page count than the
  source is detected as a page-count mismatch.
- Corrupted bytes (either extension) are reported as a parse-related
  failure, not an unhandled exception.
"""
import io
import sys
from pathlib import Path

import fitz  # PyMuPDF
import pytest
from docx import Document

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools.extraction_docx import extract_docx_content
from agent.tools.matching import match_rules
from agent.tools.extraction_pdf import extract_pdf_content
from agent.tools.redaction_docx import redact_docx_document
from agent.tools.redaction_pdf import redact_pdf_document
from agent.tools.structure_verification import verify_structure
from shared.models import RedactionRule


def _build_docx_bytes(table_rows_cols=(2, 2)) -> bytes:
    document = Document()
    document.add_paragraph("Contact ID number: 11010119900101001X please redact.")
    rows, cols = table_rows_cols
    table = document.add_table(rows=rows, cols=cols)
    for row_index in range(rows):
        for col_index in range(cols):
            table.cell(row_index, col_index).text = f"R{row_index}C{col_index}"

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _enabled_rule() -> RedactionRule:
    return RedactionRule(
        ruleName="identity_card",
        pattern=r"\d{17}[\dX]",
        enabled=True,
        isBuiltIn=True,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )


def _build_multi_page_pdf(page_texts) -> bytes:
    document = fitz.open()
    try:
        for text in page_texts:
            page = document.new_page()
            page.insert_text((72, 72), text)
        return document.tobytes()
    finally:
        document.close()


def test_docx_verify_structure_ok_when_redaction_preserves_table_shape():
    source_bytes = _build_docx_bytes(table_rows_cols=(2, 2))

    extraction = extract_docx_content(source_bytes)
    match_spans = match_rules(extraction.textUnits, extraction.imageRegions, [_enabled_rule()])
    redacted_bytes = redact_docx_document(source_bytes, extraction.textUnits, match_spans)

    result = verify_structure(source_bytes, redacted_bytes, ".docx")

    assert result.ok is True
    assert result.reason is None


def test_docx_verify_structure_detects_table_count_mismatch():
    source_bytes = _build_docx_bytes(table_rows_cols=(2, 2))
    # Simulate a corrupted "redacted" document that lost its table
    # entirely, by building a distinct sample docx with zero tables.
    other_document = Document()
    other_document.add_paragraph("No tables here.")
    buffer = io.BytesIO()
    other_document.save(buffer)
    redacted_bytes = buffer.getvalue()

    result = verify_structure(source_bytes, redacted_bytes, ".docx")

    assert result.ok is False
    assert result.reason == "table_count_mismatch"


def test_docx_verify_structure_detects_row_col_mismatch():
    source_bytes = _build_docx_bytes(table_rows_cols=(2, 2))
    # Same table count (1 table), but different row/column shape.
    redacted_bytes = _build_docx_bytes(table_rows_cols=(3, 2))

    result = verify_structure(source_bytes, redacted_bytes, ".docx")

    assert result.ok is False
    assert result.reason == "table_row_col_mismatch"


def test_pdf_verify_structure_ok_when_redaction_preserves_page_count_and_order():
    source_bytes = _build_multi_page_pdf(
        ["Page one 11010119900101001X", "Page two text", "Page three text"]
    )

    extraction = extract_pdf_content(source_bytes)
    match_spans = match_rules(extraction.textUnits, extraction.imageRegions, [_enabled_rule()])
    redacted_bytes = redact_pdf_document(source_bytes, extraction.textUnits, match_spans)

    result = verify_structure(source_bytes, redacted_bytes, ".pdf")

    assert result.ok is True
    assert result.reason is None


def test_pdf_verify_structure_detects_page_count_mismatch():
    source_bytes = _build_multi_page_pdf(["Page one", "Page two", "Page three"])
    redacted_bytes = _build_multi_page_pdf(["Only page"])

    result = verify_structure(source_bytes, redacted_bytes, ".pdf")

    assert result.ok is False
    assert result.reason == "page_count_mismatch"


def test_docx_verify_structure_reports_parse_error_for_corrupted_redacted_bytes():
    source_bytes = _build_docx_bytes()

    result = verify_structure(source_bytes, b"not a valid docx", ".docx")

    assert result.ok is False
    assert result.reason == "parse_error_redacted"


def test_pdf_verify_structure_reports_parse_error_for_corrupted_source_bytes():
    redacted_bytes = _build_multi_page_pdf(["Page one"])

    result = verify_structure(b"not a valid pdf", redacted_bytes, ".pdf")

    assert result.ok is False
    assert result.reason == "parse_error_source"


def test_unsupported_extension_returns_failure_without_raising():
    result = verify_structure(b"anything", b"anything", ".txt")

    assert result.ok is False
    assert result.reason == "unsupported_extension"
