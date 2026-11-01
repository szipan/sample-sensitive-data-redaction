"""Unit tests for `agent/tools/extraction_pdf.py` (`extract_pdf_content`).

Covers design.md's `extract_document_content` tool description for `.pdf`:

- Known page text is extracted as `ExtractedTextUnit`s with `zone="body"`.
- Page count / page order structural metadata is correct for a
  multi-page document.
- An embedded image produces an `ImageRegion` with a populated `boundingBox`,
  populated `imageBytes` (from `document.extract_image(xref)`), and no `recognizedText`
  yet.
- Extraction is best-effort about image bytes: when `document.extract_image`
  cannot materialize a stream (`_extract_image_bytes` returns `None`), the
  region still exists with `imageBytes=None` and extraction does not crash.
- A corrupted/non-PDF byte stream raises `ExtractionError`.

Test PDFs are built on the fly with `PyMuPDF` itself (no external fixture
files, no `reportlab` dependency needed).
"""
import sys
from pathlib import Path

import fitz  # PyMuPDF
import pytest

# Ensure `agent/` and `shared/` are importable the same way the Strands
# Agents SDK Runtime would, mirroring the `sys.path` convention used by
# `tests/test_upload_handler.py` and friends.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools import extraction_pdf
from agent.tools.extraction_pdf import (
    ExtractionError,
    _extract_image_bytes,
    extract_pdf_content,
)


def _build_single_page_text_pdf(text: str) -> bytes:
    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), text)
        return document.tobytes()
    finally:
        document.close()


def _build_multi_page_pdf(page_texts):
    document = fitz.open()
    try:
        for text in page_texts:
            page = document.new_page()
            page.insert_text((72, 72), text)
        return document.tobytes()
    finally:
        document.close()


def _build_single_page_pdf_with_image() -> bytes:
    """Build a one-page PDF containing a small embedded raster image, by
    first rendering a tiny source PDF page to a pixmap (PNG bytes) and then
    inserting that image into a fresh page - avoiding any dependency on an
    external image file or library."""
    source_document = fitz.open()
    try:
        source_page = source_document.new_page(width=50, height=50)
        source_page.draw_rect(fitz.Rect(5, 5, 45, 45), fill=(1, 0, 0))
        pixmap = source_page.get_pixmap()
        image_bytes = pixmap.tobytes("png")
    finally:
        source_document.close()

    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_image(fitz.Rect(50, 50, 150, 150), stream=image_bytes)
        return document.tobytes()
    finally:
        document.close()


def test_extracts_text_units_with_body_zone_for_known_page_text():
    pdf_bytes = _build_single_page_text_pdf("Hello Redaction POC")

    result = extract_pdf_content(pdf_bytes)

    assert result.pageCount == 1
    assert result.pageOrder == [0]
    assert len(result.textUnits) == 1
    unit = result.textUnits[0]
    assert unit.zone == "body"
    assert "Hello Redaction POC" in unit.text
    assert unit.position.index == 0


def test_reports_correct_page_count_and_order_for_multi_page_pdf():
    pdf_bytes = _build_multi_page_pdf(["Page one text", "Page two text", "Page three text"])

    result = extract_pdf_content(pdf_bytes)

    assert result.pageCount == 3
    assert result.pageOrder == [0, 1, 2]
    extracted_text = " ".join(unit.text for unit in result.textUnits)
    assert "Page one text" in extracted_text
    assert "Page two text" in extracted_text
    assert "Page three text" in extracted_text


def test_extracts_image_region_with_bounding_box_and_bytes():
    pdf_bytes = _build_single_page_pdf_with_image()

    result = extract_pdf_content(pdf_bytes)

    assert result.pageCount == 1
    assert len(result.imageRegions) == 1
    image_region = result.imageRegions[0]
    assert image_region.zone == "image"
    assert image_region.recognizedText is None
    # boundingBox and sourceLocation are unchanged.
    assert image_region.boundingBox is not None
    assert image_region.boundingBox.width > 0
    assert image_region.boundingBox.height > 0
    assert image_region.sourceLocation.startswith("page:0:xref:")
    # imageBytes is now populated from `document.extract_image(xref)`
    # Rather than left `None` as a placeholder.
    assert isinstance(image_region.imageBytes, bytes)
    assert len(image_region.imageBytes) > 0


def test_extracted_image_bytes_are_decodable_as_an_image():
    """The bytes populated on the region are the raw embedded image stream,
    so a downstream consumer (the OCR / in-image redaction tools) can decode
    them - here re-opened via PyMuPDF as a sanity check."""
    pdf_bytes = _build_single_page_pdf_with_image()

    result = extract_pdf_content(pdf_bytes)

    image_bytes = result.imageRegions[0].imageBytes
    assert image_bytes is not None
    pixmap = fitz.Pixmap(image_bytes)
    assert pixmap.width > 0
    assert pixmap.height > 0


def test_image_region_still_emitted_when_bytes_cannot_be_extracted(monkeypatch):
    """Best-effort posture: if the image bytes cannot be
    materialized, `_extract_image_bytes` returns `None`, and the region is
    still emitted with `imageBytes=None` while `sourceLocation`/`boundingBox`
    remain populated - extraction does not crash."""
    monkeypatch.setattr(
        extraction_pdf, "_extract_image_bytes", lambda document, xref: None
    )
    pdf_bytes = _build_single_page_pdf_with_image()

    result = extract_pdf_content(pdf_bytes)

    assert len(result.imageRegions) == 1
    image_region = result.imageRegions[0]
    assert image_region.imageBytes is None
    assert image_region.boundingBox is not None
    assert image_region.sourceLocation.startswith("page:0:xref:")


class _FakeDocument:
    """Minimal stand-in for a `fitz.Document` exposing only the
    `extract_image(xref)` method that `_extract_image_bytes` calls."""

    def __init__(self, behavior):
        self._behavior = behavior

    def extract_image(self, xref):
        return self._behavior(xref)


def test_extract_image_bytes_returns_bytes_when_present():
    document = _FakeDocument(lambda xref: {"image": b"\x89PNG-bytes", "ext": "png"})

    assert _extract_image_bytes(document, 7) == b"\x89PNG-bytes"


def test_extract_image_bytes_returns_none_when_extract_raises():
    def _raise(xref):
        raise RuntimeError("cannot materialize image")

    document = _FakeDocument(_raise)

    assert _extract_image_bytes(document, 7) is None


@pytest.mark.parametrize("empty_result", [None, {}, {"image": b""}, {"ext": "png"}])
def test_extract_image_bytes_returns_none_for_empty_or_missing_stream(empty_result):
    document = _FakeDocument(lambda xref: empty_result)

    assert _extract_image_bytes(document, 7) is None


def test_raises_extraction_error_for_corrupted_pdf_bytes():
    with pytest.raises(ExtractionError):
        extract_pdf_content(b"this is not a valid pdf file")
