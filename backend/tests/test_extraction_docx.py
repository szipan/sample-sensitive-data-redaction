"""Unit tests for `agent/tools/extraction_docx.py` (`extract_docx_content`).

Builds a representative `.docx` file on the fly using `python-docx` itself
(one body paragraph, one table, and header/footer text) and asserts the
resulting `ExtractedTextUnit`s have the correct `zone`/`position`/`text`
values, per design.md's `extract_document_content` tool description.
"""
import io
import sys
from pathlib import Path

import pytest
from docx import Document
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools.extraction_docx import ExtractionError, extract_docx_content


def _media_image_regions(result):
    """The `ImageRegion`s that correspond to actual embedded `/word/media/`
    images, filtering out the `/docProps/thumbnail.jpeg` that python-docx's
    default template always ships (see the module-level note below).
    """
    return [
        r
        for r in result.imageRegions
        if r.sourceLocation.startswith("docx:part:/word/media/")
    ]


def _png_bytes(color=(255, 0, 0), size=(24, 16)) -> bytes:
    """A small, valid PNG that python-docx's image parser accepts."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _build_sample_docx_bytes() -> bytes:
    document = Document()

    section = document.sections[0]
    section.header.paragraphs[0].text = "Sample Header"
    section.footer.paragraphs[0].text = "Sample Footer"

    document.add_paragraph("First body paragraph.")
    document.add_paragraph("Second body paragraph.")

    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "R0C0"
    table.cell(0, 1).text = "R0C1"
    table.cell(1, 0).text = "R1C0"
    table.cell(1, 1).text = "R1C1"

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _build_docx_with_images_bytes(image_blobs) -> bytes:
    document = Document()
    document.add_paragraph("Body paragraph with images.")
    for blob in image_blobs:
        document.add_picture(io.BytesIO(blob))

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def sample_docx_bytes() -> bytes:
    return _build_sample_docx_bytes()


def test_body_paragraphs_are_extracted_in_order(sample_docx_bytes):
    result = extract_docx_content(sample_docx_bytes)

    body_units = [u for u in result.textUnits if u.zone == "body"]
    assert [u.text for u in body_units] == [
        "First body paragraph.",
        "Second body paragraph.",
    ]
    assert [u.position.index for u in body_units] == [0, 1]


def test_table_cells_are_extracted_with_position(sample_docx_bytes):
    result = extract_docx_content(sample_docx_bytes)

    table_units = [u for u in result.textUnits if u.zone == "table"]
    assert len(table_units) == 4

    by_position = {
        (u.position.tableIndex, u.position.rowIndex, u.position.colIndex): u.text
        for u in table_units
    }
    assert by_position == {
        (0, 0, 0): "R0C0",
        (0, 0, 1): "R0C1",
        (0, 1, 0): "R1C0",
        (0, 1, 1): "R1C1",
    }


def test_header_and_footer_text_is_extracted(sample_docx_bytes):
    result = extract_docx_content(sample_docx_bytes)

    header_units = [u for u in result.textUnits if u.zone == "header"]
    footer_units = [u for u in result.textUnits if u.zone == "footer"]

    assert any(u.text == "Sample Header" for u in header_units)
    assert any(u.text == "Sample Footer" for u in footer_units)


def test_no_word_media_image_regions_when_no_images_embedded(sample_docx_bytes):
    # `extract_docx_content` emits one ImageRegion per embedded media part.
    # A document authored with python-docx embeds no `/word/media/` images
    # here, so there must be no media image regions. (python-docx's default
    # template DOES ship a `/docProps/thumbnail.jpeg`, which the extractor's
    # OR-predicate also catches, so `imageRegions` is not necessarily empty —
    # we assert specifically on the absence of `/word/media/` regions.)
    result = extract_docx_content(sample_docx_bytes)

    assert _media_image_regions(result) == []


def test_default_template_thumbnail_is_emitted_as_image_region(sample_docx_bytes):
    # Document the observed behavior explicitly: the default python-docx
    # template's `/docProps/thumbnail.jpeg` (content_type image/jpeg) is caught
    # by the extractor's `content_type.startswith("image/")` predicate and
    # surfaces as an ImageRegion even though no picture was added.
    result = extract_docx_content(sample_docx_bytes)

    thumbnail_regions = [
        r
        for r in result.imageRegions
        if r.sourceLocation == "docx:part:/docProps/thumbnail.jpeg"
    ]
    assert len(thumbnail_regions) == 1
    thumbnail = thumbnail_regions[0]
    assert thumbnail.zone == "image"
    assert thumbnail.boundingBox is None
    assert thumbnail.imageBytes  # non-empty bytes


def test_embedded_image_emits_one_image_region_per_media_part():
    png = _png_bytes()
    docx_bytes = _build_docx_with_images_bytes([png])

    result = extract_docx_content(docx_bytes)

    media_regions = _media_image_regions(result)
    assert len(media_regions) == 1

    region = media_regions[0]
    assert region.zone == "image"
    assert region.boundingBox is None
    # sourceLocation is the stable re-embed handle `docx:part:{partname}`.
    assert region.sourceLocation.startswith("docx:part:/word/media/image")
    # imageBytes is the media part's blob, i.e. the PNG we embedded.
    assert region.imageBytes == png


def test_multiple_embedded_images_each_yield_one_media_region():
    blobs = [
        _png_bytes(color=(255, 0, 0)),
        _png_bytes(color=(0, 255, 0)),
        _png_bytes(color=(0, 0, 255)),
    ]
    docx_bytes = _build_docx_with_images_bytes(blobs)

    result = extract_docx_content(docx_bytes)

    media_regions = _media_image_regions(result)
    assert len(media_regions) == len(blobs)

    for region in media_regions:
        assert region.zone == "image"
        assert region.boundingBox is None
        assert region.sourceLocation.startswith("docx:part:/word/media/")
        assert region.imageBytes  # each media part carries its own blob

    # Distinct media parts get distinct re-embed handles.
    source_locations = {r.sourceLocation for r in media_regions}
    assert len(source_locations) == len(blobs)


def test_structure_metadata_records_table_dimensions(sample_docx_bytes):
    result = extract_docx_content(sample_docx_bytes)

    assert len(result.structureMetadata.tables) == 1
    table_meta = result.structureMetadata.tables[0]
    assert table_meta.tableIndex == 0
    assert table_meta.rowCount == 2
    assert table_meta.colCount == 2


def test_invalid_docx_bytes_raise_extraction_error():
    with pytest.raises(ExtractionError):
        extract_docx_content(b"this is not a valid docx file")
