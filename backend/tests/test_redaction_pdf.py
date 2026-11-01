"""Unit tests for `agent/tools/redaction_pdf.py` (`redact_pdf_document`).

Builds test PDFs on the fly with `fitz` (PyMuPDF), runs the real
`extract_pdf_content` + `match_rules` pipeline to
produce realistic `text_units`/`match_spans`, then calls
`redact_pdf_document` and re-parses the RESULT with `extract_pdf_content`
again to assert (per design.md's `redact_document` `.pdf` row and
Correctness Properties 12/16):

- Matched text no longer appears anywhere in the redacted document's
  extracted text (permanent erasure via `apply_redactions`).
- Page count and page order are unchanged.
- Non-matching text elsewhere on the page survives.
- Zero matches leaves the extracted text unchanged.

The final group of tests covers the dual-path additions:
`redact_pdf_document_with_report` dispatching on the `unitRef` prefix,
`"ocr:"` spans resolving through Textract word geometry, and the per-path /
agreement counts in `PdfRedactionReport`.
"""
import sys
from io import BytesIO
from pathlib import Path
from unittest import mock

import fitz  # PyMuPDF
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools.extraction_pdf import extract_pdf_content  # noqa: E402
from agent.tools.matching import match_rules  # noqa: E402
from agent.tools.redaction_pdf import (  # noqa: E402
    PdfRedactionReport,
    RedactionError,
    redact_pdf_document,
    redact_pdf_document_with_report,
)
from shared.models import (  # noqa: E402
    BoundingBox,
    ExtractedTextUnit,
    ImageRegion,
    MatchSpan,
    OcrWord,
    RecognizedElement,
    RedactionRule,
    TextPosition,
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


def _build_single_page_pdf(text: str) -> bytes:
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


def _extracted_text(pdf_bytes: bytes) -> str:
    result = extract_pdf_content(pdf_bytes)
    return " ".join(unit.text for unit in result.textUnits)


# ---------------------------------------------------------------------------
# Embedded-image helpers
#
# Build small real PNGs with Pillow and embed them in a real PDF so the
# `image:` redaction path (decode -> draw -> re-encode -> re-embed) can be
# exercised end to end. `Page.replace_image` is stubbed in the tests so the
# assertions focus on WHAT the redactor hands to re-embedding rather than on
# PyMuPDF's own stream-swap side effects.
# ---------------------------------------------------------------------------


def _make_png(width: int, height: int, color=(255, 255, 255)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _build_pdf_with_image(page_text: str, image_bytes: bytes) -> tuple:
    """Return `(pdf_bytes, xref)` for a one-page PDF carrying `page_text` and a
    single embedded image, where `xref` is the image's XObject number as seen
    when the serialized bytes are reopened (the same value the redactor derives
    from `sourceLocation`)."""
    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), page_text)
        page.insert_image(fitz.Rect(72, 120, 272, 220), stream=image_bytes)
        pdf_bytes = document.tobytes()
    finally:
        document.close()

    reopened = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        xref = reopened.load_page(0).get_images(full=True)[0][0]
    finally:
        reopened.close()
    return pdf_bytes, xref


def _element(
    text: str,
    start: int,
    end: int,
    left: float,
    top: float,
    width: float,
    height: float,
) -> RecognizedElement:
    return RecognizedElement(
        text=text,
        startOffset=start,
        endOffset=end,
        boundingBox=BoundingBox(left=left, top=top, width=width, height=height),
    )


def _image_region(
    source_location: str,
    image_bytes,
    recognized_text=None,
    elements=None,
) -> ImageRegion:
    return ImageRegion(
        zone="image",
        sourceLocation=source_location,
        recognizedText=recognized_text,
        imageBytes=image_bytes,
        recognizedElements=elements,
    )


def test_matched_text_no_longer_appears_after_redaction():
    pdf_bytes = _build_single_page_pdf("Contact 13800001111 for details")
    extraction = extract_pdf_content(pdf_bytes)
    rule = _rule("mobile", r"1[3-9]\d{9}")
    matches = match_rules(extraction.textUnits, extraction.imageRegions, [rule])
    assert len(matches) == 1  # sanity check the fixture actually matched

    redacted_bytes = redact_pdf_document(pdf_bytes, extraction.textUnits, matches)

    redacted_text = _extracted_text(redacted_bytes)
    assert "13800001111" not in redacted_text


def test_page_count_and_order_preserved_across_redaction():
    pdf_bytes = _build_multi_page_pdf(
        ["Page one has 13800001111 in it", "Page two is clean", "Page three text"]
    )
    extraction = extract_pdf_content(pdf_bytes)
    rule = _rule("mobile", r"1[3-9]\d{9}")
    matches = match_rules(extraction.textUnits, extraction.imageRegions, [rule])

    redacted_bytes = redact_pdf_document(pdf_bytes, extraction.textUnits, matches)

    redacted_extraction = extract_pdf_content(redacted_bytes)
    assert redacted_extraction.pageCount == extraction.pageCount == 3
    assert redacted_extraction.pageOrder == extraction.pageOrder == [0, 1, 2]


def test_non_matching_text_elsewhere_on_page_survives_redaction():
    pdf_bytes = _build_single_page_pdf("Phone 13800001111 unrelated notes here")
    extraction = extract_pdf_content(pdf_bytes)
    rule = _rule("mobile", r"1[3-9]\d{9}")
    matches = match_rules(extraction.textUnits, extraction.imageRegions, [rule])

    redacted_bytes = redact_pdf_document(pdf_bytes, extraction.textUnits, matches)

    redacted_text = _extracted_text(redacted_bytes)
    assert "unrelated notes here" in redacted_text
    assert "13800001111" not in redacted_text


def test_zero_matches_produces_output_identical_extracted_text():
    pdf_bytes = _build_single_page_pdf("Nothing sensitive in this sentence at all")
    extraction = extract_pdf_content(pdf_bytes)
    rule = _rule("mobile", r"1[3-9]\d{9}")
    matches = match_rules(extraction.textUnits, extraction.imageRegions, [rule])
    assert matches == []  # sanity check: no match expected

    redacted_bytes = redact_pdf_document(pdf_bytes, extraction.textUnits, matches)

    source_text = _extracted_text(pdf_bytes)
    redacted_text = _extracted_text(redacted_bytes)
    assert redacted_text == source_text


def test_multiple_matches_across_pages_each_redacted():
    pdf_bytes = _build_multi_page_pdf(
        ["First page id 110101199003077777 here", "Second page mobile 13800001111 here"]
    )
    extraction = extract_pdf_content(pdf_bytes)
    rules = [
        _rule("id_card", r"\d{17}[\dX]"),
        _rule("mobile", r"1[3-9]\d{9}"),
    ]
    matches = match_rules(extraction.textUnits, extraction.imageRegions, rules)
    assert len(matches) == 2

    redacted_bytes = redact_pdf_document(pdf_bytes, extraction.textUnits, matches)

    redacted_text = _extracted_text(redacted_bytes)
    assert "110101199003077777" not in redacted_text
    assert "13800001111" not in redacted_text

    redacted_extraction = extract_pdf_content(redacted_bytes)
    assert redacted_extraction.pageCount == 2
    assert redacted_extraction.pageOrder == [0, 1]


def test_image_referenced_span_is_now_acted_on_not_skipped():
    """in-image-redaction reversal of the pre-feature behavior: an `image:`
    span used to be dropped with a bare `continue`, leaving detected in-image
    PII visible. It must now be acted on — the referenced embedded image is
    redacted and re-embedded via `page.replace_image`, while the page's own
    non-image text is left untouched.
    """
    png = _make_png(200, 100, (255, 255, 255))
    pdf_bytes, xref = _build_pdf_with_image("Plain text with no matches", png)
    region = _image_region(
        f"page:0:xref:{xref}",
        png,
        recognized_text="12345678",
        elements=[_element("12345678", 0, 8, 0.1, 0.1, 0.5, 0.3)],
    )
    image_span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=8, ruleName="mobile"
    )

    with mock.patch.object(fitz.Page, "replace_image") as replace_image:
        redacted_bytes = redact_pdf_document(
            pdf_bytes, [], [image_span], image_regions=[region]
        )

    # The image span was acted on (not skipped): the embedded image was
    # re-embedded exactly once with the redacted bytes for the right xref.
    assert replace_image.call_count == 1
    call = replace_image.call_args
    assert call.args[0] == xref
    assert isinstance(call.kwargs["stream"], bytes) and call.kwargs["stream"]
    # The page's own (non-image) text is unchanged by image redaction.
    assert "Plain text with no matches" in _extracted_text(redacted_bytes)


def test_out_of_range_unit_ref_raises_redaction_error():
    pdf_bytes = _build_single_page_pdf("Some text")
    extraction = extract_pdf_content(pdf_bytes)
    from shared.models import MatchSpan

    bad_match = MatchSpan(
        zone="body", unitRef="text:99", startOffset=0, endOffset=1, ruleName="mobile"
    )

    with pytest.raises(RedactionError):
        redact_pdf_document(pdf_bytes, extraction.textUnits, [bad_match])


def test_invalid_pdf_bytes_raise_redaction_error():
    with pytest.raises(RedactionError):
        redact_pdf_document(b"not a pdf", [], [])

# ---------------------------------------------------------------------------
# Dual-path dispatch and provenance report
#
# `redact_pdf_document_with_report` dispatches on the `unitRef` prefix and
# reports how many rectangles each detection path contributed. OCR units are
# built by hand here (rather than by calling Textract): the real word geometry
# is taken from the page itself via `page.search_for`, then normalized to
# `[0, 1]` exactly the way Textract reports it, so the fixtures exercise the
# real coordinate conversion instead of an invented one.
# ---------------------------------------------------------------------------


def _normalized_box_for(pdf_bytes: bytes, page_index: int, needle: str) -> BoundingBox:
    """Locate `needle` on a page and return its box in Textract's normalized
    (0-1, fractions of the page rectangle) coordinate space."""
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = document.load_page(page_index)
        hits = page.search_for(needle)
        assert hits, "fixture text {!r} not found on page {}".format(needle, page_index)
        rect = hits[0]
        page_rect = page.rect
        return BoundingBox(
            left=(rect.x0 - page_rect.x0) / page_rect.width,
            top=(rect.y0 - page_rect.y0) / page_rect.height,
            width=rect.width / page_rect.width,
            height=rect.height / page_rect.height,
        )
    finally:
        document.close()


def _ocr_unit(page_index: int, words) -> ExtractedTextUnit:
    """Build one `zone="ocr"` unit the way `ocr_pdf_pages` does: words joined
    with a single space, each carrying its offset range and normalized box."""
    text = ""
    ocr_words = []
    for word_text, box in words:
        if text:
            text += " "
        start = len(text)
        ocr_words.append(
            OcrWord(
                text=word_text,
                startOffset=start,
                endOffset=start + len(word_text),
                boundingBox=box,
            )
        )
        text += word_text
    return ExtractedTextUnit(
        zone="ocr",
        text=text,
        position=TextPosition(index=page_index, pageIndex=page_index),
        ocrWords=ocr_words,
    )


def test_wrapper_returns_same_bytes_as_report_variant():
    pdf_bytes = _build_single_page_pdf("Contact 13800001111 for details")
    extraction = extract_pdf_content(pdf_bytes)
    matches = match_rules(
        extraction.textUnits, extraction.imageRegions, [_rule("mobile", r"1[3-9]\d{9}")]
    )

    wrapper_bytes = redact_pdf_document(pdf_bytes, extraction.textUnits, matches)
    result = redact_pdf_document_with_report(pdf_bytes, extraction.textUnits, matches)

    assert isinstance(wrapper_bytes, bytes)
    assert _extracted_text(wrapper_bytes) == _extracted_text(result.documentBytes)
    assert result.report.textLayerRegions == 1
    assert result.report.ocrRegions == 0
    assert result.report.agreedRegions == 0


def test_ocr_referenced_span_is_redacted_and_counted():
    pdf_bytes = _build_single_page_pdf("Contact 13800001111 for details")
    ocr_units = [
        _ocr_unit(
            0,
            [
                ("Contact", _normalized_box_for(pdf_bytes, 0, "Contact")),
                ("13800001111", _normalized_box_for(pdf_bytes, 0, "13800001111")),
            ],
        )
    ]
    span = MatchSpan(
        zone="ocr",
        unitRef="ocr:0",
        startOffset=ocr_units[0].ocrWords[1].startOffset,
        endOffset=ocr_units[0].ocrWords[1].endOffset,
        ruleName="mobile",
    )

    result = redact_pdf_document_with_report(
        pdf_bytes, [], [span], ocr_text_units=ocr_units
    )

    redacted_text = _extracted_text(result.documentBytes)
    assert "13800001111" not in redacted_text
    assert "Contact" in redacted_text
    assert result.report.textLayerRegions == 0
    assert result.report.ocrRegions == 1
    assert result.report.agreedRegions == 0


def test_overlapping_text_and_ocr_spans_both_applied_and_counted_as_agreed():
    pdf_bytes = _build_single_page_pdf("Contact 13800001111 for details")
    extraction = extract_pdf_content(pdf_bytes)
    text_matches = match_rules(
        extraction.textUnits, extraction.imageRegions, [_rule("mobile", r"1[3-9]\d{9}")]
    )
    assert len(text_matches) == 1
    ocr_units = [
        _ocr_unit(
            0, [("13800001111", _normalized_box_for(pdf_bytes, 0, "13800001111"))]
        )
    ]
    ocr_span = MatchSpan(
        zone="ocr", unitRef="ocr:0", startOffset=0, endOffset=11, ruleName="mobile"
    )

    result = redact_pdf_document_with_report(
        pdf_bytes,
        extraction.textUnits,
        text_matches + [ocr_span],
        ocr_text_units=ocr_units,
    )

    assert "13800001111" not in _extracted_text(result.documentBytes)
    assert result.report.textLayerRegions == 1
    assert result.report.ocrRegions == 1
    assert result.report.agreedRegions == 1


def test_non_overlapping_paths_report_zero_agreement():
    pdf_bytes = _build_single_page_pdf("Contact 13800001111 for details")
    extraction = extract_pdf_content(pdf_bytes)
    text_matches = match_rules(
        extraction.textUnits, extraction.imageRegions, [_rule("mobile", r"1[3-9]\d{9}")]
    )
    # The OCR path found a different word on the same page.
    ocr_units = [
        _ocr_unit(0, [("details", _normalized_box_for(pdf_bytes, 0, "details"))])
    ]
    ocr_span = MatchSpan(
        zone="ocr", unitRef="ocr:0", startOffset=0, endOffset=7, ruleName="keyword"
    )

    result = redact_pdf_document_with_report(
        pdf_bytes,
        extraction.textUnits,
        text_matches + [ocr_span],
        ocr_text_units=ocr_units,
    )

    redacted_text = _extracted_text(result.documentBytes)
    assert "13800001111" not in redacted_text
    assert "details" not in redacted_text
    assert result.report.textLayerRegions == 1
    assert result.report.ocrRegions == 1
    assert result.report.agreedRegions == 0


def test_ocr_span_covering_only_a_separator_resolves_to_no_rectangle():
    pdf_bytes = _build_single_page_pdf("Contact 13800001111 for details")
    ocr_units = [
        _ocr_unit(
            0,
            [
                ("Contact", _normalized_box_for(pdf_bytes, 0, "Contact")),
                ("13800001111", _normalized_box_for(pdf_bytes, 0, "13800001111")),
            ],
        )
    ]
    separator_span = MatchSpan(
        zone="ocr", unitRef="ocr:0", startOffset=7, endOffset=8, ruleName="mobile"
    )

    result = redact_pdf_document_with_report(
        pdf_bytes, [], [separator_span], ocr_text_units=ocr_units
    )

    assert result.report == PdfRedactionReport(0, 0, 0)
    assert "13800001111" in _extracted_text(result.documentBytes)


def test_out_of_range_ocr_unit_ref_raises_redaction_error():
    pdf_bytes = _build_single_page_pdf("Some text")
    span = MatchSpan(
        zone="ocr", unitRef="ocr:3", startOffset=0, endOffset=1, ruleName="mobile"
    )

    with pytest.raises(RedactionError):
        redact_pdf_document_with_report(pdf_bytes, [], [span], ocr_text_units=[])


def test_image_referenced_spans_are_counted_in_neither_path():
    """The `PdfRedactionReport` stays a text-vs-OCR dual-path diagnostic: even
    though `image:` spans are now acted on, their in-image rectangles are folded
    into neither `textLayerRegions` nor `ocrRegions` (nor `agreedRegions`).
    """
    png = _make_png(200, 100, (255, 255, 255))
    pdf_bytes, xref = _build_pdf_with_image("Plain text with no matches", png)
    region = _image_region(
        f"page:0:xref:{xref}",
        png,
        recognized_text="12345678",
        elements=[_element("12345678", 0, 8, 0.1, 0.1, 0.5, 0.3)],
    )
    image_span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=8, ruleName="mobile"
    )

    with mock.patch.object(fitz.Page, "replace_image") as replace_image:
        result = redact_pdf_document_with_report(
            pdf_bytes, [], [image_span], image_regions=[region]
        )

    # The image was acted on, but contributes to none of the report counts.
    assert replace_image.call_count == 1
    assert result.report == PdfRedactionReport(0, 0, 0)
    assert "Plain text with no matches" in _extracted_text(result.documentBytes)


# ---------------------------------------------------------------------------
# In-image redaction: `image:` span handling and manipulation-failure
# semantics.
# ---------------------------------------------------------------------------


_MOBILE_RULE = _rule("mobile", r"1[3-9]\d{9}")
# Hypothesis strategy for values the mobile rule matches (a Chinese-style
# 11-digit number, 1[3-9] + 9 digits).
_mobile_numbers = st.from_regex(r"1[3-9][0-9]{9}", fullmatch=True)


@settings(max_examples=100, deadline=None)
@given(secrets=st.lists(_mobile_numbers, min_size=1, max_size=3, unique=True))
def test_property_11_text_and_ocr_spans_redacted_unchanged(secrets):
    """Text: and ocr: spans are
    redacted unchanged.

    For any set of `text:`/`ocr:` spans and NO `image:` spans, the `.pdf`
    output is identical to the pre-feature output. The pre-feature call
    omitted `image_regions` entirely; the feature adds an optional
    `image_regions` parameter and an `image:` handler. This asserts that
    parameter is inert for the `text:`/`ocr:` paths: redacting with
    `image_regions` omitted and with `image_regions` supplied (but with no
    `image:` span referencing anything) yields byte-for-byte the same
    redactions — same extracted text and same per-path report — while the
    detected values are still erased.
    """
    document = fitz.open()
    try:
        page = document.new_page()
        for i, secret in enumerate(secrets):
            page.insert_text((72, 72 + i * 24), f"row {i} value {secret}")
        pdf_bytes = document.tobytes()
    finally:
        document.close()

    extraction = extract_pdf_content(pdf_bytes)
    text_matches = match_rules(
        extraction.textUnits, extraction.imageRegions, [_MOBILE_RULE]
    )
    # Add one ocr: span too, derived from real page geometry, so the property
    # covers both text: and ocr: prefixes.
    first_box = _normalized_box_for(pdf_bytes, 0, secrets[0])
    ocr_units = [_ocr_unit(0, [(secrets[0], first_box)])]
    ocr_span = MatchSpan(
        zone="ocr",
        unitRef="ocr:0",
        startOffset=0,
        endOffset=len(secrets[0]),
        ruleName="mobile",
    )
    spans = text_matches + [ocr_span]

    # Pre-feature call shape: image_regions omitted.
    out_omitted = redact_pdf_document_with_report(
        pdf_bytes, extraction.textUnits, spans, ocr_text_units=ocr_units
    )
    # Feature call shape: image_regions supplied, but no image: span references
    # it, so the image path is never entered.
    unrelated_region = _image_region(
        "page:0:xref:9999", _make_png(8, 8), recognized_text="x", elements=None
    )
    out_with_regions = redact_pdf_document_with_report(
        pdf_bytes,
        extraction.textUnits,
        spans,
        ocr_text_units=ocr_units,
        image_regions=[unrelated_region],
    )

    assert out_omitted.report == out_with_regions.report
    assert _extracted_text(out_omitted.documentBytes) == _extracted_text(
        out_with_regions.documentBytes
    )
    # And the text:/ocr: spans really were redacted.
    redacted_text = _extracted_text(out_omitted.documentBytes)
    for secret in secrets:
        assert secret not in redacted_text


def test_image_span_targeted_redaction_blacks_out_element_region():
    """Reliable geometry + a span that overlaps an element -> targeted
    redaction: the redactor hands `page.replace_image` bytes whose element
    region is black while the rest of the image is untouched (Requirements
    1.1, 1.2)."""
    png = _make_png(200, 100, (255, 255, 255))
    pdf_bytes, xref = _build_pdf_with_image("doc text", png)
    region = _image_region(
        f"page:0:xref:{xref}",
        png,
        recognized_text="12345678",
        elements=[_element("12345678", 0, 8, 0.1, 0.1, 0.5, 0.3)],
    )
    span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=8, ruleName="mobile"
    )

    with mock.patch.object(fitz.Page, "replace_image") as replace_image:
        redact_pdf_document_with_report(
            pdf_bytes, [], [span], image_regions=[region]
        )

    assert replace_image.call_count == 1
    call = replace_image.call_args
    assert call.args[0] == xref  # re-embedded against the correct xref
    new_bytes = call.kwargs["stream"]
    assert isinstance(new_bytes, bytes)

    img = Image.open(BytesIO(new_bytes)).convert("RGB")
    w, h = img.size
    # A pixel inside the element box (x 0.1..0.6, y 0.1..0.4) is blacked out.
    assert img.getpixel((int(0.3 * w), int(0.2 * h))) == (0, 0, 0)
    # A far corner outside the (padded) box is untouched -> targeted, not
    # whole-image.
    assert img.getpixel((w - 1, h - 1)) == (255, 255, 255)


def test_image_span_whole_image_fallback_blacks_out_entire_image():
    """Untrustworthy geometry (`recognizedElements is None`) on a region that
    carries an `image:` span -> Whole_Image_Fallback: the entire image is
    blacked out."""
    png = _make_png(200, 100, (255, 255, 255))
    pdf_bytes, xref = _build_pdf_with_image("doc text", png)
    region = _image_region(
        f"page:0:xref:{xref}",
        png,
        recognized_text="12345678",
        elements=None,  # localization untrustworthy -> fallback
    )
    span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=8, ruleName="mobile"
    )

    with mock.patch.object(fitz.Page, "replace_image") as replace_image:
        redact_pdf_document_with_report(
            pdf_bytes, [], [span], image_regions=[region]
        )

    assert replace_image.call_count == 1
    new_bytes = replace_image.call_args.kwargs["stream"]
    img = Image.open(BytesIO(new_bytes)).convert("RGB")
    w, h = img.size
    for point in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, h // 2)]:
        assert img.getpixel(point) == (0, 0, 0)


def test_image_decode_failure_surfaces_as_redaction_error():
    """An image whose bytes cannot be decoded raises `ImageRedactionError`
    inside `redaction_image`, which the PDF redactor converts into
    `RedactionError` so the Job fails loudly."""
    pdf_bytes, xref = _build_pdf_with_image("doc text", _make_png(10, 10))
    region = _image_region(
        f"page:0:xref:{xref}",
        b"this is definitely not a decodable image",
        recognized_text="x",
        elements=None,
    )
    span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=1, ruleName="mobile"
    )

    with pytest.raises(RedactionError):
        redact_pdf_document_with_report(pdf_bytes, [], [span], image_regions=[region])


def _displayed_image_black_fraction(pdf_bytes: bytes, page_index: int) -> float:
    """Render `page_index` of `pdf_bytes` and return the fraction of near-black
    pixels. Rendering (rather than reading an image xref) is what proves which
    image the page actually DISPLAYS — the exact thing the orphan-xref bug got
    wrong."""
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pix = document.load_page(page_index).get_pixmap(dpi=100)
        image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    finally:
        document.close()
    w, h = image.size
    px = image.load()
    black = 0
    total = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = px[x, y]
            total += 1
            if r < 40 and g < 40 and b < 40:
                black += 1
    return black / max(total, 1)


def test_image_whole_image_fallback_survives_overlapping_text_redaction():
    """Regression (in-image-redaction bugfix): a `text:`/`ocr:` redaction
    rectangle that OVERLAPS an embedded image must not defeat the image's own
    `image:` redaction.

    The bug: `page.apply_redactions()` (default image handling) rewrites any
    image a redaction annotation touches into a NEW xref. Since the `image:`
    redaction re-embeds via `page.replace_image(xref, ...)` using the
    extraction-time xref, the redacted image landed on the now-orphaned old
    xref while the page kept displaying the untouched, renumbered image — so
    in-image PII stayed visible even though a fully-black image was written
    into the file. The fix applies redactions with
    `images=PDF_REDACT_IMAGE_NONE`, keeping image xrefs stable.

    This builds a PDF whose text sits ON TOP of a white embedded image, marks
    that text for redaction (a `text:` span whose rectangle overlaps the
    image), and marks the image itself for a Whole_Image_Fallback redaction
    (`recognizedElements=None`). It then RENDERS the output page and asserts
    the displayed image region is predominantly black — i.e. `replace_image`
    landed on the image the page actually shows.
    """
    png = _make_png(400, 300, (255, 255, 255))
    document = fitz.open()
    try:
        page = document.new_page(width=420, height=320)
        # Image fills almost the whole page ...
        page.insert_image(fitz.Rect(10, 10, 410, 310), stream=png)
        # ... and the sensitive text is drawn ON TOP of the image, so its
        # redaction rectangle necessarily overlaps the image.
        page.insert_text((40, 60), "ID 13800001111 on the image")
        pdf_bytes = document.tobytes()
    finally:
        document.close()

    reopened = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        xref = reopened.load_page(0).get_images(full=True)[0][0]
    finally:
        reopened.close()

    extraction = extract_pdf_content(pdf_bytes)
    text_matches = match_rules(
        extraction.textUnits, extraction.imageRegions, [_rule("mobile", r"1[3-9]\d{9}")]
    )
    assert text_matches, "fixture should contain a phone-number text match"

    # Whole-image fallback for the embedded image (untrustworthy geometry).
    region = _image_region(
        f"page:0:xref:{xref}", png, recognized_text="周杰 13800001111", elements=None
    )
    image_span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=2, ruleName="cjk-name"
    )

    result = redact_pdf_document_with_report(
        pdf_bytes,
        extraction.textUnits,
        list(text_matches) + [image_span],
        image_regions=[region],
    )

    # The displayed image on page 0 must be blacked out (whole-image fallback
    # actually took effect on the image the page shows, not on an orphan).
    black = _displayed_image_black_fraction(result.documentBytes, 0)
    assert black > 0.80, (
        "expected the displayed page to be mostly black after whole-image "
        "fallback, got {:.1%} — the redacted image is likely an orphan xref "
        "(the pre-fix bug)".format(black)
    )
    # And the overlapping text span was still redacted from the text layer.
    assert "13800001111" not in _extracted_text(result.documentBytes)


def test_redacted_output_is_not_ballooned_by_reembedding():
    """Regression (in-image-redaction bugfix): re-embedding a redacted image
    must not leave the orphaned original in the file. `document.tobytes` is
    called with `garbage=4, deflate=True`, so the output stays close to the
    source size instead of inflating by the size of every replaced image.
    """
    png = _make_png(600, 400, (255, 255, 255))
    pdf_bytes, xref = _build_pdf_with_image("cover text", png)
    region = _image_region(
        f"page:0:xref:{xref}", png, recognized_text="周杰", elements=None
    )
    image_span = MatchSpan(
        zone="image", unitRef="image:0", startOffset=0, endOffset=2, ruleName="cjk-name"
    )

    result = redact_pdf_document_with_report(
        pdf_bytes, [], [image_span], image_regions=[region]
    )

    # The orphaned original image is garbage-collected, so the output does not
    # carry two copies of the image. Allow generous headroom over the source.
    assert len(result.documentBytes) < len(pdf_bytes) + len(png) * 2


def test_text_redaction_unchanged_when_image_regions_present_but_no_image_span():
    """Supplying `image_regions` while the spans reference no image leaves the
    text redaction exactly as before and never touches the image (the no-span
    image is left alone)."""
    png = _make_png(60, 60, (255, 255, 255))
    pdf_bytes, xref = _build_pdf_with_image(
        "Contact 13800001111 for details", png
    )
    extraction = extract_pdf_content(pdf_bytes)
    matches = match_rules(extraction.textUnits, extraction.imageRegions, [_MOBILE_RULE])
    assert len(matches) == 1
    region = _image_region(
        f"page:0:xref:{xref}", png, recognized_text="x", elements=None
    )

    with mock.patch.object(fitz.Page, "replace_image") as replace_image:
        result = redact_pdf_document_with_report(
            pdf_bytes, extraction.textUnits, matches, image_regions=[region]
        )

    assert replace_image.call_count == 0  # no image: span -> image untouched
    redacted_text = _extracted_text(result.documentBytes)
    assert "13800001111" not in redacted_text
    assert "details" in redacted_text
