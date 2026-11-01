"""Tests for `agent/tools/redaction_image.py` — the shared image-manipulation
core of the in-image-redaction feature (design.md "Component 2:
`backend/agent/tools/redaction_image.py` — new module").

This module decodes an embedded image with Pillow, decides between *targeted*
rectangles (reliable per-element geometry) and the *Whole_Image_Fallback*
(untrustworthy geometry, or any span that maps to zero rectangles), draws
filled black rectangles, and re-encodes in the same format. The correctness
spine is: detected in-image PII must never remain visible.

Because these tests decode real PNGs with Pillow (a real dev/runtime
dependency), this module belongs in the SECOND (real-`fitz`/`docx`) pytest
process group, alongside `tests/test_redaction_pdf.py` /
`tests/test_redaction_docx.py`.

This file covers:

- Every detected in-image PII span is covered by a rectangle
- Offset overlap decides box inclusion, exactly
- Padding expands the rectangle and then clamps within bounds
- Every untrustworthy-localization mode covers the whole image
- Example/edge unit tests: targeted drawing, P-mode handling, format
  preservation, and undecodable bytes -> ImageRedactionError

Every property test uses Hypothesis, runs >= 100 iterations, and is tagged in
its docstring `Feature: in-image-redaction, Property {n}: {title}`.
"""
from io import BytesIO
from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from PIL import Image  # noqa: E402

from agent.tools import redaction_image  # noqa: E402
from agent.tools.redaction_image import (  # noqa: E402
    BOX_PADDING_ENV_VAR,
    DEFAULT_BOX_PADDING_PX,
    ImageRedactionError,
    _pad_and_clamp_rect,
    _rects_for_image_span,
    redact_image_bytes,
)
from shared.models import (  # noqa: E402
    BoundingBox,
    ImageRegion,
    MatchSpan,
    RecognizedElement,
)


# ---------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------


def _png_bytes(width: int, height: int, color=(255, 255, 255), mode: str = "RGB") -> bytes:
    """A real, decodable PNG of the given pixel dimensions/mode."""
    buffer = BytesIO()
    Image.new(mode, (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(width: int, height: int, color=(255, 255, 255)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _element(text: str, start: int, end: int, box: BoundingBox) -> RecognizedElement:
    return RecognizedElement(
        text=text, startOffset=start, endOffset=end, boundingBox=box
    )


def _span(start: int, end: int, unit_ref: str = "image:0") -> MatchSpan:
    return MatchSpan(
        zone="image",
        unitRef=unit_ref,
        startOffset=start,
        endOffset=end,
        ruleName="test-rule",
    )


def _region(recognized_elements, source_location: str = "image:0") -> ImageRegion:
    return ImageRegion(
        zone="image",
        sourceLocation=source_location,
        recognizedElements=recognized_elements,
    )


def _scaled_rect(box: BoundingBox, W: int, H: int):
    """The pixel rect `_rects_for_image_span` is expected to produce for `box`,
    computed by an independent formula so the assertion is not a tautology."""
    left = box.left * W
    top = box.top * H
    return (left, top, left + box.width * W, top + box.height * H)


def _overlaps(element: RecognizedElement, span: MatchSpan) -> bool:
    """The half-open offset-overlap oracle."""
    return element.startOffset < span.endOffset and span.startOffset < element.endOffset


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


@st.composite
def _box_strategy(draw) -> BoundingBox:
    """A well-formed normalized `[0,1]` bounding box with positive area that
    stays within the unit square."""
    left = draw(st.floats(min_value=0.0, max_value=0.8))
    top = draw(st.floats(min_value=0.0, max_value=0.8))
    width = draw(st.floats(min_value=0.05, max_value=1.0 - left))
    height = draw(st.floats(min_value=0.05, max_value=1.0 - top))
    return BoundingBox(left=left, top=top, width=width, height=height)


@st.composite
def _elements_and_span(draw):
    """Draw a list of recognized elements (each with distinct, non-adjacent
    offset ranges so overlap is unambiguous) plus a span and image dimensions."""
    count = draw(st.integers(min_value=0, max_value=5))
    elements = []
    cursor = 0
    for i in range(count):
        gap = draw(st.integers(min_value=0, max_value=3))
        start = cursor + gap
        length = draw(st.integers(min_value=1, max_value=6))
        end = start + length
        box = draw(_box_strategy())
        elements.append(_element("x" * length, start, end, box))
        cursor = end
    # Span somewhere in (and possibly beyond) the offset space.
    span_start = draw(st.integers(min_value=0, max_value=max(cursor, 1)))
    span_len = draw(st.integers(min_value=0, max_value=8))
    span = _span(span_start, span_start + span_len)
    W = draw(st.integers(min_value=10, max_value=2000))
    H = draw(st.integers(min_value=10, max_value=2000))
    return elements, span, W, H


# ===========================================================================
# Every detected in-image PII span is covered
# ===========================================================================


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_elements_and_span())
def test_property_2_span_rectangles_cover_overlapping_elements(sample):
    """Under reliable geometry, the targeted rectangle set produced for a span
    covers the union of the boxes of every element the span overlaps.

    Every detected in-image PII span is covered by a rectangle
    """
    elements, span, W, H = sample

    rects = _rects_for_image_span(elements, span, W, H)
    overlapping = [e for e in elements if _overlaps(e, span)]

    # Every overlapping element's scaled box appears among the rectangles, so
    # the union of rects covers the union of overlapping boxes.
    for element in overlapping:
        assert _scaled_rect(element.boundingBox, W, H) in rects

    # No spurious rectangles: exactly one per overlapping element.
    assert len(rects) == len(overlapping)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    _box_strategy(),
    st.integers(min_value=20, max_value=200),
    st.integers(min_value=20, max_value=200),
)
def test_property_2_span_with_no_overlap_forces_whole_image(box, W, H):
    """A span that overlaps no element (even with reliable geometry present)
    forces the Whole_Image_Fallback, so the span's PII is never left uncovered:
    the whole decoded image comes back black.

    Every detected in-image PII span is covered by a rectangle
    """
    # Reliable geometry: an element at offsets [0, 5). The span [10, 15)
    # overlaps nothing, so no rectangle resolves -> whole-image fallback.
    element = _element("abcde", 0, 5, box)
    region = _region([element])
    span = _span(10, 15)

    out = redact_image_bytes(_png_bytes(W, H, color=(200, 30, 30)), region, [span])

    decoded = Image.open(BytesIO(out)).convert("RGB")
    for xy in ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1), (W // 2, H // 2)):
        assert decoded.getpixel(xy) == (0, 0, 0)


# ===========================================================================
# Offset overlap decides box inclusion, exactly
# ===========================================================================


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_elements_and_span())
def test_property_3_offset_overlap_decides_inclusion(sample):
    """`_rects_for_image_span` includes an element's box iff the half-open
    ranges overlap (`element.startOffset < span.endOffset and span.startOffset
    < element.endOffset`), and never includes a non-overlapping element's box.

    Offset overlap decides box inclusion, exactly
    """
    elements, span, W, H = sample

    rects = _rects_for_image_span(elements, span, W, H)

    # The included set is exactly the overlapping elements, in element order:
    # every overlapping element contributes its (scaled) box and nothing else,
    # so a non-overlapping element never contributes a rectangle.
    expected = [
        _scaled_rect(e.boundingBox, W, H) for e in elements if _overlaps(e, span)
    ]
    assert rects == expected
    assert len(rects) == sum(1 for e in elements if _overlaps(e, span))


# ===========================================================================
# Padding expands then clamps within bounds
# ===========================================================================


@st.composite
def _rect_dims_padding(draw):
    """A well-ordered pixel rect entirely within `[0,W] x [0,H]`, plus a
    non-negative padding."""
    W = draw(st.integers(min_value=1, max_value=2000))
    H = draw(st.integers(min_value=1, max_value=2000))
    left = draw(st.floats(min_value=0.0, max_value=float(W)))
    right = draw(st.floats(min_value=left, max_value=float(W)))
    top = draw(st.floats(min_value=0.0, max_value=float(H)))
    bottom = draw(st.floats(min_value=top, max_value=float(H)))
    padding = draw(st.floats(min_value=0.0, max_value=500.0))
    return (left, top, right, bottom), W, H, padding


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_rect_dims_padding())
def test_property_7_padding_expands_then_clamps(sample):
    """The padded rectangle contains the original rectangle and lies entirely
    within `[0, W] x [0, H]`.

    Padding expands the rectangle and then clamps within bounds
    """
    rect, W, H, padding = sample
    left, top, right, bottom = rect

    p_left, p_top, p_right, p_bottom = _pad_and_clamp_rect(rect, W, H, padding)

    # Contains the original (the original is within bounds by construction).
    eps = 1e-9
    assert p_left <= left + eps
    assert p_top <= top + eps
    assert p_right >= right - eps
    assert p_bottom >= bottom - eps

    # Lies entirely within the image bounds.
    assert 0.0 - eps <= p_left <= float(W) + eps
    assert 0.0 - eps <= p_top <= float(H) + eps
    assert 0.0 - eps <= p_right <= float(W) + eps
    assert 0.0 - eps <= p_bottom <= float(H) + eps


# ===========================================================================
# Every untrustworthy-localization mode covers the whole image
# ===========================================================================


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.integers(min_value=16, max_value=160),
    st.integers(min_value=16, max_value=160),
    st.tuples(
        st.integers(min_value=0, max_value=255),
        st.integers(min_value=0, max_value=255),
        st.integers(min_value=0, max_value=255),
    ),
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=50), st.integers(min_value=0, max_value=50)),
        min_size=1,
        max_size=4,
    ),
)
def test_property_9_untrustworthy_localization_covers_whole_image(W, H, color, span_ranges):
    """For any region with >= 1 `image:` span whose localization is
    untrustworthy (`recognizedElements is None`), `redact_image_bytes` fills the
    entire image: decoded corner (and centre) pixels are black.

    Every untrustworthy-localization mode covers the whole image
    """
    region = _region(None)  # untrustworthy geometry
    spans = [_span(start, start + length) for start, length in span_ranges]

    out = redact_image_bytes(_png_bytes(W, H, color=color), region, spans)

    decoded = Image.open(BytesIO(out)).convert("RGB")
    for xy in ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1), (W // 2, H // 2)):
        assert decoded.getpixel(xy) == (0, 0, 0)


# ===========================================================================
# Unit tests — targeted drawing, P-mode, format preservation, decode failure
# ===========================================================================


def test_targeted_redaction_blacks_out_expected_region(monkeypatch):
    """A targeted redaction blacks out exactly the element's region on a
    generated PNG while leaving distant pixels untouched."""
    # Pin padding to 0 so the covered region is exactly the element's box.
    monkeypatch.setenv(BOX_PADDING_ENV_VAR, "0")

    W, H = 100, 100
    # Element box: fractions (0.1, 0.1) size (0.3, 0.3) -> pixels (10,10)-(40,40).
    box = BoundingBox(left=0.1, top=0.1, width=0.3, height=0.3)
    element = _element("secret", 0, 6, box)
    region = _region([element])
    span = _span(0, 6)

    out = redact_image_bytes(_png_bytes(W, H, color=(255, 255, 255)), region, [span])

    decoded = Image.open(BytesIO(out)).convert("RGB")
    # Inside the element region -> black.
    assert decoded.getpixel((25, 25)) == (0, 0, 0)
    # Far from the element region -> untouched white.
    assert decoded.getpixel((90, 90)) == (255, 255, 255)


def test_palettized_p_mode_image_is_handled():
    """A `P`-mode (palettized) image is converted so black is exact.
    Whole-image fallback here, so every pixel is black."""
    # Build a palettized PNG (mode "P") with a non-black colour index.
    buffer = BytesIO()
    Image.new("P", (40, 30), color=5).save(buffer, format="PNG")
    p_bytes = buffer.getvalue()
    assert Image.open(BytesIO(p_bytes)).mode == "P"

    region = _region(None)  # whole-image fallback
    out = redact_image_bytes(p_bytes, region, [_span(0, 4)])

    decoded = Image.open(BytesIO(out)).convert("RGB")
    assert decoded.getpixel((0, 0)) == (0, 0, 0)
    assert decoded.getpixel((39, 29)) == (0, 0, 0)


def test_output_format_matches_input_png():
    """A PNG in comes back as a PNG (same container)."""
    region = _region(None)
    out = redact_image_bytes(_png_bytes(30, 20), region, [_span(0, 3)])
    assert Image.open(BytesIO(out)).format == "PNG"


def test_output_format_matches_input_jpeg():
    """A JPEG in comes back as a JPEG."""
    region = _region(None)
    out = redact_image_bytes(_jpeg_bytes(30, 20), region, [_span(0, 3)])
    assert Image.open(BytesIO(out)).format == "JPEG"


def test_undecodable_bytes_raise_image_redaction_error():
    """An undecodable byte string raises `ImageRedactionError`,
    which the redactors convert into `RedactionError` to fail the Job loudly."""
    region = _region(None)
    with pytest.raises(ImageRedactionError):
        redact_image_bytes(b"not a real image", region, [_span(0, 3)])


def test_default_box_padding_constant():
    """The documented default padding is 4 px."""
    assert DEFAULT_BOX_PADDING_PX == 4
    assert BOX_PADDING_ENV_VAR == "IMAGE_REDACTION_BOX_PADDING_PX"
