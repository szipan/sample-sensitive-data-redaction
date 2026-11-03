"""Unit tests for `agent/tools/extraction_pdf.py` (`extract_pdf_content`).

Covers design.md's `extract_document_content` tool description for `.pdf`:

- Known page text is extracted as `ExtractedTextUnit`s with `zone="body"`.
- Page count / page order structural metadata is correct for a
  multi-page document.
- An embedded image produces an `ImageRegion` with a populated `boundingBox`,
  populated `imageBytes` (decoded by `pdf_images.decode_image`), and no `recognizedText`
  yet.
- Extraction is best-effort about image bytes: when the image
  cannot be decoded (`_extract_image_bytes` returns `None`), the
  region still exists with `imageBytes=None` and extraction does not crash.
- A corrupted/non-PDF byte stream raises `ExtractionError`.

Test PDFs are built on the fly with `tests/pdf_fixtures.py` (no external
fixture files).
"""
import io
import sys
from pathlib import Path

import pikepdf
import pytest
from PIL import Image

# Ensure `agent/` and `shared/` are importable the same way the Strands
# Agents SDK Runtime would, mirroring the `sys.path` convention used by
# `tests/test_upload_handler.py` and friends.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools import extraction_pdf
from tests.pdf_fixtures import (
    simple_image_pdf,
    simple_multi_page_pdf,
    simple_text_pdf,
)
from agent.tools.extraction_pdf import (
    ExtractionError,
    _extract_image_bytes,
    extract_pdf_content,
)


def _build_single_page_text_pdf(text: str) -> bytes:
    return simple_text_pdf([text])


def _build_multi_page_pdf(page_texts):
    return simple_multi_page_pdf([[text] for text in page_texts])


def _build_single_page_pdf_with_image() -> bytes:
    """A one-page PDF with one embedded 50x50 RGB raster image."""
    return simple_image_pdf(50, 50, kind="rgb", color=(255, 0, 0))


def _first_image_xref(pdf_bytes: bytes) -> int:
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        return next(
            obj.objgen[0]
            for obj in pdf.objects
            if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image"
        )


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
    # imageBytes is now populated by `pdf_images.decode_image`
    # Rather than left `None` as a placeholder.
    assert isinstance(image_region.imageBytes, bytes)
    assert len(image_region.imageBytes) > 0


def test_extracted_image_bytes_are_decodable_as_an_image():
    """The bytes populated on the region are the raw embedded image stream,
    so a downstream consumer (the OCR / in-image redaction tools) can decode
    them - here re-opened via Pillow as a sanity check."""
    pdf_bytes = _build_single_page_pdf_with_image()

    result = extract_pdf_content(pdf_bytes)

    image_bytes = result.imageRegions[0].imageBytes
    assert image_bytes is not None
    with Image.open(io.BytesIO(image_bytes)) as decoded:
        decoded.load()
        assert decoded.width > 0
        assert decoded.height > 0
        # Pixel size equals the image object's /Width and /Height.
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            xref = int(result.imageRegions[0].sourceLocation.rsplit(":", 1)[1])
            obj = pdf.get_object((xref, 0))
            assert decoded.size == (int(obj["/Width"]), int(obj["/Height"]))


def test_image_region_still_emitted_when_bytes_cannot_be_extracted(monkeypatch):
    """Best-effort posture: if the image bytes cannot be
    materialized, `_extract_image_bytes` returns `None`, and the region is
    still emitted with `imageBytes=None` while `sourceLocation`/`boundingBox`
    remain populated - extraction does not crash."""
    monkeypatch.setattr(
        extraction_pdf, "_extract_image_bytes", lambda pdf, xref: None
    )
    pdf_bytes = _build_single_page_pdf_with_image()

    result = extract_pdf_content(pdf_bytes)

    assert len(result.imageRegions) == 1
    image_region = result.imageRegions[0]
    assert image_region.imageBytes is None
    assert image_region.boundingBox is not None
    assert image_region.sourceLocation.startswith("page:0:xref:")


def test_extract_image_bytes_returns_decodable_bytes_for_real_image():
    pdf_bytes = _build_single_page_pdf_with_image()
    xref = _first_image_xref(pdf_bytes)

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        data = _extract_image_bytes(pdf, xref)
        obj = pdf.get_object((xref, 0))
        expected_size = (int(obj["/Width"]), int(obj["/Height"]))

    assert isinstance(data, bytes)
    assert len(data) > 0
    with Image.open(io.BytesIO(data)) as decoded:
        decoded.load()
        assert decoded.size == expected_size


def _pdf_with_unusable_image_object(case: str):
    """Return `(pdf, xref)` where `xref` is not a decodable image."""
    pdf = pikepdf.new()
    if case == "missing_object":
        return pdf, 9999
    if case == "not_an_image":
        return pdf, pdf.make_indirect(pikepdf.Dictionary()).objgen[0]
    stream = pikepdf.Stream(pdf, b"")
    stream["/Type"] = pikepdf.Name("/XObject")
    stream["/Subtype"] = pikepdf.Name("/Image")
    stream["/Width"] = 4
    stream["/Height"] = 4
    stream["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
    stream["/BitsPerComponent"] = 8
    if case == "undecodable_filter":
        stream.write(b"not image data", filter=pikepdf.Name("/DCTDecode"))
    elif case != "empty_stream":
        raise ValueError(case)
    return pdf, pdf.make_indirect(stream).objgen[0]


@pytest.mark.parametrize(
    "case", ["missing_object", "not_an_image", "empty_stream", "undecodable_filter"]
)
def test_extract_image_bytes_returns_none_for_unusable_image_object(case):
    pdf, xref = _pdf_with_unusable_image_object(case)
    try:
        assert _extract_image_bytes(pdf, xref) is None
    finally:
        pdf.close()


def test_raises_extraction_error_for_corrupted_pdf_bytes():
    with pytest.raises(ExtractionError):
        extract_pdf_content(b"this is not a valid pdf file")
