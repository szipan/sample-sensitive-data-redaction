"""Unit tests for `agent/tools/redaction_docx.py` (`redact_docx_document`).

Builds representative `.docx` files with `python-docx`, runs them through
the already-implemented `extract_docx_content` and `match_rules`
to produce realistic `text_units`/`match_spans`, then calls
`redact_docx_document` and re-parses the RESULT with `extract_docx_content`
again to assert (per design.md's `redact_document` tool description and
Correctness Properties 12/17):

- Matched text no longer appears anywhere in the redacted document.
- Non-matching paragraphs/cells/header/footer text is unchanged.
- Table structure (table/row/column counts) is unchanged.
- Zero matches produce output whose extracted text is identical to the
  source's extracted text.
"""
import io
import sys
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Inches
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools import redaction_image
from agent.tools.extraction_docx import extract_docx_content
from agent.tools.matching import match_rules
from agent.tools.redaction_docx import REDACTION_BLOCK_CHAR, RedactionError, redact_docx_document
from shared.models import (
    BoundingBox,
    ImageRegion,
    MatchSpan,
    RecognizedElement,
    RedactionRule,
)


def _rule(rule_name: str, pattern: str, enabled: bool = True) -> RedactionRule:
    return RedactionRule(
        ruleName=rule_name,
        pattern=pattern,
        enabled=enabled,
        isBuiltIn=False,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
    )


MOBILE_RULE = _rule("mobile", r"1[3-9]\d{9}")


def _build_sample_docx_bytes() -> bytes:
    document = Document()

    section = document.sections[0]
    section.header.paragraphs[0].text = "Sample Header 13800001111"
    section.footer.paragraphs[0].text = "Sample Footer, no PII here"

    p1 = document.add_paragraph()
    p1.add_run("call ")
    run = p1.add_run("13800001111")
    run.bold = True
    p1.add_run(" now")

    document.add_paragraph("Second body paragraph has no PII.")

    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "contact 15900002222 please"
    table.cell(0, 1).text = "no match here"
    table.cell(1, 0).text = "also clean"
    table.cell(1, 1).text = "another clean cell"

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def sample_docx_bytes() -> bytes:
    return _build_sample_docx_bytes()


def _extract_and_match(file_bytes: bytes, rules):
    extraction = extract_docx_content(file_bytes)
    spans = match_rules(extraction.textUnits, extraction.imageRegions, rules)
    return extraction, spans


def test_matched_text_no_longer_appears_after_redaction(sample_docx_bytes):
    extraction, spans = _extract_and_match(sample_docx_bytes, [MOBILE_RULE])
    assert len(spans) >= 1  # sanity: the fixture does contain matches

    redacted_bytes = redact_docx_document(sample_docx_bytes, extraction.textUnits, spans)
    redacted_extraction = extract_docx_content(redacted_bytes)

    all_redacted_text = "\n".join(u.text for u in redacted_extraction.textUnits)
    assert "13800001111" not in all_redacted_text
    assert "15900002222" not in all_redacted_text
    # The Solid Redaction Block placeholder is present where matches were.
    assert REDACTION_BLOCK_CHAR in all_redacted_text


def test_non_matching_units_are_unchanged(sample_docx_bytes):
    extraction, spans = _extract_and_match(sample_docx_bytes, [MOBILE_RULE])

    redacted_bytes = redact_docx_document(sample_docx_bytes, extraction.textUnits, spans)
    redacted_extraction = extract_docx_content(redacted_bytes)

    matched_unit_indices = set()
    for span in spans:
        assert span.unitRef.startswith("text:")
        matched_unit_indices.add(int(span.unitRef[len("text:") :]))

    assert len(redacted_extraction.textUnits) == len(extraction.textUnits)
    for index, (original_unit, redacted_unit) in enumerate(
        zip(extraction.textUnits, redacted_extraction.textUnits)
    ):
        if index in matched_unit_indices:
            continue
        assert redacted_unit.zone == original_unit.zone
        assert redacted_unit.text == original_unit.text
        assert redacted_unit.position == original_unit.position


def test_redacted_run_retains_character_count_and_surrounding_text_intact(sample_docx_bytes):
    extraction, spans = _extract_and_match(sample_docx_bytes, [MOBILE_RULE])

    redacted_bytes = redact_docx_document(sample_docx_bytes, extraction.textUnits, spans)
    redacted_extraction = extract_docx_content(redacted_bytes)

    body_units = [u for u in redacted_extraction.textUnits if u.zone == "body"]
    redacted_first_paragraph = next(u for u in body_units if "call" in u.text)
    assert redacted_first_paragraph.text == f"call {REDACTION_BLOCK_CHAR * 11} now"


def test_table_structure_is_preserved(sample_docx_bytes):
    extraction, spans = _extract_and_match(sample_docx_bytes, [MOBILE_RULE])

    redacted_bytes = redact_docx_document(sample_docx_bytes, extraction.textUnits, spans)
    redacted_extraction = extract_docx_content(redacted_bytes)

    assert redacted_extraction.structureMetadata.tables == extraction.structureMetadata.tables


def test_zero_matches_produce_output_identical_to_source_extracted_text(sample_docx_bytes):
    no_match_rule = _rule("never_matches", r"ZZZ_NO_MATCH_ZZZ")
    extraction, spans = _extract_and_match(sample_docx_bytes, [no_match_rule])
    assert spans == []

    redacted_bytes = redact_docx_document(sample_docx_bytes, extraction.textUnits, spans)
    redacted_extraction = extract_docx_content(redacted_bytes)

    assert len(redacted_extraction.textUnits) == len(extraction.textUnits)
    for original_unit, redacted_unit in zip(extraction.textUnits, redacted_extraction.textUnits):
        assert redacted_unit.zone == original_unit.zone
        assert redacted_unit.text == original_unit.text
        assert redacted_unit.position == original_unit.position
    assert redacted_extraction.structureMetadata.tables == extraction.structureMetadata.tables


def test_header_and_footer_matches_are_redacted():
    document = Document()
    section = document.sections[0]
    section.header.paragraphs[0].text = "Header phone 13800001111"
    section.footer.paragraphs[0].text = "Footer clean text"
    document.add_paragraph("Body clean text")

    buffer = io.BytesIO()
    document.save(buffer)
    file_bytes = buffer.getvalue()

    extraction, spans = _extract_and_match(file_bytes, [MOBILE_RULE])
    assert any(span.zone == "header" for span in spans)

    redacted_bytes = redact_docx_document(file_bytes, extraction.textUnits, spans)
    redacted_extraction = extract_docx_content(redacted_bytes)

    header_unit = next(u for u in redacted_extraction.textUnits if u.zone == "header")
    footer_unit = next(u for u in redacted_extraction.textUnits if u.zone == "footer")
    body_unit = next(u for u in redacted_extraction.textUnits if u.zone == "body")

    assert "13800001111" not in header_unit.text
    assert REDACTION_BLOCK_CHAR in header_unit.text
    assert footer_unit.text == "Footer clean text"
    assert body_unit.text == "Body clean text"


def test_invalid_docx_bytes_raise_redaction_error():
    with pytest.raises(RedactionError):
        redact_docx_document(b"not a docx", [], [])


def test_unit_ref_out_of_range_raises_redaction_error(sample_docx_bytes):
    extraction, _ = _extract_and_match(sample_docx_bytes, [MOBILE_RULE])
    from shared.models import MatchSpan

    bogus_span = MatchSpan(
        zone="body",
        unitRef=f"text:{len(extraction.textUnits) + 100}",
        startOffset=0,
        endOffset=1,
        ruleName="mobile",
    )

    with pytest.raises(RedactionError):
        redact_docx_document(sample_docx_bytes, extraction.textUnits, [bogus_span])


# ---------------------------------------------------------------------------
# In-image redaction (in-image-redaction feature)
#
# `redact_docx_document` now ACTS on `image:`-namespaced spans instead of
# skipping them: it redacts the detected PII on the embedded image itself
# (via `redaction_image.redact_image_bytes`) and replaces the media part's
# bytes in place, located by the `docx:part:{partname}` handle in the
# region's `sourceLocation`. These tests exercise that re-embedding for both
# the targeted and whole-image-fallback cases and assert the pre-feature
# text-run redaction is unaffected. They open real `.docx` (real python-docx)
# and decode real PNGs (real Pillow), so they belong in the second pytest
# process group.
# ---------------------------------------------------------------------------

MEDIA_PARTNAME_PREFIX = "/word/media/"


def _png_bytes(width: int, height: int, color=(255, 255, 255)) -> bytes:
    """A small solid-color PNG, used as an embedded image whose pixels can be
    decoded and asserted on after redaction."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _build_docx_with_image(
    png: bytes, body_text: str = "call 13800001111 now"
) -> bytes:
    """A one-paragraph `.docx` carrying `body_text` and one embedded PNG,
    so extraction yields a real `/word/media/imageN.png` part the redactor
    can locate and replace."""
    document = Document()
    document.add_paragraph(body_text)
    document.add_picture(io.BytesIO(png), width=Inches(1))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _media_region(extraction):
    """Return `(index, region)` for the embedded `/word/media/` image region
    in an extraction result (the index is the `image:{index}` unitRef value)."""
    for index, region in enumerate(extraction.imageRegions):
        if MEDIA_PARTNAME_PREFIX in region.sourceLocation:
            return index, region
    raise AssertionError("no /word/media/ image region found in extraction")


def _read_media_blob(docx_bytes: bytes, source_location: str) -> bytes:
    """Read the blob of the media part identified by a `docx:part:{partname}`
    source location out of a saved `.docx`."""
    partname = source_location[len("docx:part:") :]
    document = Document(io.BytesIO(docx_bytes))
    for part in document.part.package.iter_parts():
        if str(getattr(part, "partname", "")) == partname:
            return part.blob
    raise AssertionError(f"media part {partname!r} not found in redacted .docx")


def test_image_span_replaces_media_part_blob_with_redact_output(monkeypatch):
    """The redactor replaces the referenced media part's bytes with exactly
    what `redaction_image.redact_image_bytes` returns, located by the
    `docx:part:` handle."""
    png = _png_bytes(60, 40)
    file_bytes = _build_docx_with_image(png)
    extraction = extract_docx_content(file_bytes)
    region_index, region = _media_region(extraction)

    sentinel = b"SENTINEL-REDACTED-IMAGE-BYTES"
    calls = []

    def fake_redact(image_bytes, region_arg, spans_arg):
        calls.append((image_bytes, region_arg, spans_arg))
        return sentinel

    monkeypatch.setattr(redaction_image, "redact_image_bytes", fake_redact)

    image_span = MatchSpan(
        zone="image",
        unitRef=f"image:{region_index}",
        startOffset=0,
        endOffset=2,
        ruleName="mobile",
    )
    redacted_bytes = redact_docx_document(
        file_bytes, extraction.textUnits, [image_span], extraction.imageRegions
    )

    # The media part's blob is now exactly the redactor's output.
    assert _read_media_blob(redacted_bytes, region.sourceLocation) == sentinel
    # And it was called with the region's own bytes, region, and its spans.
    assert len(calls) == 1
    called_bytes, called_region, called_spans = calls[0]
    assert called_bytes == region.imageBytes
    assert called_region is region
    assert called_spans == [image_span]


def test_targeted_image_redaction_blacks_out_only_the_element_region():
    """With reliable per-element geometry, only the span's element region is
    blacked out; the rest of the image is left intact."""
    png = _png_bytes(100, 60, color=(255, 255, 255))
    file_bytes = _build_docx_with_image(png)
    extraction = extract_docx_content(file_bytes)
    region_index, region = _media_region(extraction)

    # Attach reliable geometry: one element covering the image's center,
    # whose offsets overlap the image span below.
    region.recognizedText = "\u5468\u6770"  # 周杰
    region.recognizedElements = [
        RecognizedElement(
            text="\u5468\u6770",
            startOffset=0,
            endOffset=2,
            boundingBox=BoundingBox(left=0.4, top=0.4, width=0.2, height=0.2),
        )
    ]

    image_span = MatchSpan(
        zone="image",
        unitRef=f"image:{region_index}",
        startOffset=0,
        endOffset=2,
        ruleName="cjk-name",
    )
    redacted_bytes = redact_docx_document(
        file_bytes, extraction.textUnits, [image_span], extraction.imageRegions
    )

    blob = _read_media_blob(redacted_bytes, region.sourceLocation)
    redacted_image = Image.open(io.BytesIO(blob)).convert("RGB")
    assert redacted_image.size == (100, 60)
    # Center of the element region is black ...
    assert redacted_image.getpixel((50, 30)) == (0, 0, 0)
    # ... but the corners (well outside the padded element box) are untouched.
    assert redacted_image.getpixel((2, 2)) == (255, 255, 255)
    assert redacted_image.getpixel((97, 57)) == (255, 255, 255)


def test_whole_image_fallback_blacks_out_entire_image():
    """With no trustworthy geometry (`recognizedElements is None`), a region
    carrying an image span is entirely blacked out."""
    png = _png_bytes(80, 50, color=(255, 255, 255))
    file_bytes = _build_docx_with_image(png)
    extraction = extract_docx_content(file_bytes)
    region_index, region = _media_region(extraction)

    # recognizedElements stays None (the default): untrustworthy localization.
    assert region.recognizedElements is None

    image_span = MatchSpan(
        zone="image",
        unitRef=f"image:{region_index}",
        startOffset=0,
        endOffset=5,
        ruleName="cjk-name",
    )
    redacted_bytes = redact_docx_document(
        file_bytes, extraction.textUnits, [image_span], extraction.imageRegions
    )

    blob = _read_media_blob(redacted_bytes, region.sourceLocation)
    redacted_image = Image.open(io.BytesIO(blob)).convert("RGB")
    assert redacted_image.size == (80, 50)
    # Every corner (and the center) is black: the whole image was covered.
    for point in [(0, 0), (79, 0), (0, 49), (79, 49), (40, 25)]:
        assert redacted_image.getpixel(point) == (0, 0, 0)


def test_text_redaction_unchanged_when_image_spans_present():
    """Redacting an image alongside text leaves the text-run redaction and the
    "runs with no match are untouched" guarantee exactly as before
    ."""
    png = _png_bytes(60, 40, color=(255, 255, 255))
    file_bytes = _build_docx_with_image(png, body_text="call 13800001111 now")

    extraction = extract_docx_content(file_bytes)
    text_spans = match_rules(extraction.textUnits, extraction.imageRegions, [MOBILE_RULE])
    assert text_spans, "fixture should contain a phone-number match"

    region_index, region = _media_region(extraction)
    region.recognizedElements = None  # whole-image fallback for the image
    image_span = MatchSpan(
        zone="image",
        unitRef=f"image:{region_index}",
        startOffset=0,
        endOffset=3,
        ruleName="mobile",
    )

    redacted_bytes = redact_docx_document(
        file_bytes,
        extraction.textUnits,
        list(text_spans) + [image_span],
        extraction.imageRegions,
    )
    redacted_extraction = extract_docx_content(redacted_bytes)

    body_unit = next(u for u in redacted_extraction.textUnits if u.zone == "body" and u.text)
    # Matched phone number gone, replaced by same-length block, surround intact.
    assert "13800001111" not in body_unit.text
    assert body_unit.text == f"call {REDACTION_BLOCK_CHAR * 11} now"

    # And the image was still redacted (whole-image fallback -> all black).
    blob = _read_media_blob(redacted_bytes, region.sourceLocation)
    redacted_image = Image.open(io.BytesIO(blob)).convert("RGB")
    assert redacted_image.getpixel((0, 0)) == (0, 0, 0)


def test_image_span_with_no_matching_region_raises_redaction_error(sample_docx_bytes):
    """The pre-feature behavior of silently ignoring `image:` spans is
    reversed: an `image:` span that references a region absent from
    `image_regions` is a wiring bug and raises `RedactionError`, matching the
    existing out-of-range guards."""
    extraction, _ = _extract_and_match(sample_docx_bytes, [MOBILE_RULE])
    image_span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=3, ruleName="mobile"
    )

    with pytest.raises(RedactionError):
        redact_docx_document(
            sample_docx_bytes, extraction.textUnits, [image_span], image_regions=[]
        )


def test_image_part_without_bytes_raises_redaction_error(sample_docx_bytes):
    """A span referencing a region with no `imageBytes` is a wiring bug and
    raises `RedactionError` rather than silently skipping."""
    extraction, _ = _extract_and_match(sample_docx_bytes, [MOBILE_RULE])
    empty_region = ImageRegion(
        zone="image",
        sourceLocation="docx:part:/word/media/image1.png",
        imageBytes=None,
    )
    image_span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=3, ruleName="mobile"
    )

    with pytest.raises(RedactionError):
        redact_docx_document(
            sample_docx_bytes, extraction.textUnits, [image_span], [empty_region]
        )
