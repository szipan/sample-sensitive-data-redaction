"""Shared image-manipulation core for in-image redaction (design.md
"Component 2: `backend/agent/tools/redaction_image.py` — new module").

Both redactors (`redaction_pdf.py`, `redaction_docx.py`) delegate all Pillow
interaction here, so each of them only decides *which* image bytes to pass and
*how* to re-embed the result. This module holds the decode -> decide
targeted-vs-whole-image -> draw filled black rectangles -> re-encode pipeline.

The correctness spine of the feature lives in the decision
made by `redact_image_bytes`: detected in-image PII must never remain visible
in the output. A region is redacted with *targeted* rectangles only when it
carries trustworthy per-element geometry (`region.recognizedElements is not
None`) AND every span resolves to at least one rectangle; in every other case
the whole image is blacked out (Whole_Image_Fallback).

Error posture: an unrecoverable decode/draw/encode failure
raises `ImageRedactionError`, which the redactors convert into their own
`RedactionError` so the Redaction Job fails loudly rather than emitting a
document with visible in-image PII. A *localization* failure, by contrast, is
never fatal here — it manifests as `recognizedElements is None`, which this
module handles by covering the whole image.

Geometry contract (mirrors `redaction_pdf._rects_for_ocr_span` +
`_normalized_bbox_to_page_rect`): a `MatchSpan`'s `[startOffset, endOffset)`
range and a `RecognizedElement`'s `[startOffset, endOffset)` range both index
into the same `ImageRegion.recognizedText` string, so box inclusion is a
half-open offset-overlap test. Each included element's `boundingBox` is
NORMALIZED to `[0, 1]` of the image's own pixel dimensions, so it is scaled by
the decoded image's actual `(W, H)` to produce a pixel rectangle.

Import constraints: this module imports no Strands / `bedrock_agentcore` symbol
and is importable without Pillow installed — Pillow (`PIL`) is imported lazily
inside `redact_image_bytes`, so the pure offset->box and padding helpers can be
unit-tested with no Pillow present.
"""
from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import List, Optional, Tuple

from shared.models import ImageRegion, MatchSpan, RecognizedElement

logger = logging.getLogger(__name__)

# A pixel rectangle, as (left, top, right, bottom) in the image's own pixel
# space. Kept as a plain tuple so the pure helpers need no Pillow type.
Rect = Tuple[float, float, float, float]


class ImageRedactionError(Exception):
    """Unrecoverable failure to decode, draw on, or re-encode an image.
    The redactors convert this into `RedactionError`,
    which drives the Redaction Job to `failed` with nothing stored — the
    only safe response to "the image carrying detected PII could not be
    manipulated", since the alternative is emitting visible in-image PII."""


DEFAULT_BOX_PADDING_PX = 4
BOX_PADDING_ENV_VAR = "IMAGE_REDACTION_BOX_PADDING_PX"


# Magic-byte prefixes used to infer a re-encode format when Pillow could not
# report the decoded image's original format. Kept small and conservative;
# anything unrecognized falls back to PNG (a safe, lossless container).
_MAGIC_BYTE_FORMATS = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"RIFF", "WEBP"),  # RIFF....WEBP; prefix check is adequate here
)

# Pillow modes that carry a palette or otherwise do not paint an exact opaque
# black via an RGB fill; converted to "RGB" before drawing so (0, 0, 0) is
# genuinely black (Solid_Redaction_Block).
_MODES_NEEDING_RGB = ("P", "PA", "LA")

# Single-channel modes: black is the integer 0, not an RGB tuple (ImageDraw
# rejects a 3-tuple fill here). Kept in their own mode so a grayscale image
# stays grayscale in the output.
_SINGLE_CHANNEL_MODES = ("L", "1")


def _read_box_padding() -> int:
    """Read `Box_Padding` from the environment at use-time, falling back to
    `DEFAULT_BOX_PADDING_PX`.

    Robust by design: an unset, blank, non-integer, or negative value must
    never crash a redaction, so any of those degrade silently to the default.
    A misconfigured padding is an operability nuisance, not a reason to fail a
    compliance Job.
    """
    raw = os.environ.get(BOX_PADDING_ENV_VAR)
    if raw is None:
        return DEFAULT_BOX_PADDING_PX
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        logger.warning(
            "%s=%r is not an integer; falling back to default padding %d px",
            BOX_PADDING_ENV_VAR,
            raw,
            DEFAULT_BOX_PADDING_PX,
        )
        return DEFAULT_BOX_PADDING_PX
    if value < 0:
        logger.warning(
            "%s=%r is negative; falling back to default padding %d px",
            BOX_PADDING_ENV_VAR,
            raw,
            DEFAULT_BOX_PADDING_PX,
        )
        return DEFAULT_BOX_PADDING_PX
    return value


def _rects_for_image_span(
    recognized_elements: List[RecognizedElement],
    span: MatchSpan,
    W: int,
    H: int,
) -> List[Rect]:
    """Return the pixel rectangles to redact for `span`, one per recognized
    element whose offset range overlaps the span's range.

    Mirrors `redaction_pdf._rects_for_ocr_span` + `_normalized_bbox_to_page_rect`:

    - Inclusion is a half-open offset-overlap test —
      `element.startOffset < span.endOffset and span.startOffset <
      element.endOffset`. An element merely *adjacent* to the span's boundary
      (its end exactly at the span's start, or its start exactly at the span's
      end) shares no character with the span and contributes nothing.
    - Each included element's `boundingBox`, normalized to `[0, 1]` of the
      image's own pixel dimensions, is scaled to a pixel rectangle
      `(left*W, top*H, (left+width)*W, (top+height)*H)`.

    Returns the list in `recognized_elements` order (possibly empty, when no
    element overlaps — a legitimate outcome the caller treats as a trigger for
    the Whole_Image_Fallback). Pure (no Pillow), so it is unit-tested directly.
    """
    rects: List[Rect] = []
    for element in recognized_elements or []:
        if (
            element.startOffset < span.endOffset
            and span.startOffset < element.endOffset
        ):
            box = element.boundingBox
            left = box.left * W
            top = box.top * H
            rects.append(
                (
                    left,
                    top,
                    left + box.width * W,
                    top + box.height * H,
                )
            )
    return rects


def _pad_and_clamp_rect(rect: Rect, W: int, H: int, padding: float) -> Rect:
    """Expand `rect` by `padding` px on every side, then clamp it to
    `[0, W] x [0, H]`.

    The padded rectangle always contains the original (padding is
    non-negative) and always lies within the image bounds after clamping, so
    a slightly-tight localized box still fully covers the glyphs it means to
    hide without spilling off the image. Pure (no Pillow).
    """
    left, top, right, bottom = rect
    # Keep the rectangle well-ordered before padding so a caller-supplied
    # inverted rect cannot invert the clamp.
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top

    left = max(0.0, min(float(W), left - padding))
    top = max(0.0, min(float(H), top - padding))
    right = max(0.0, min(float(W), right + padding))
    bottom = max(0.0, min(float(H), bottom + padding))
    return (left, top, right, bottom)


def _infer_format_from_magic_bytes(image_bytes: bytes) -> Optional[str]:
    """Best-effort re-encode format from the leading magic bytes, used only
    when Pillow does not report a decoded image's original format. Returns
    `None` when nothing matches, leaving the caller to default sensibly."""
    for prefix, fmt in _MAGIC_BYTE_FORMATS:
        if image_bytes.startswith(prefix):
            return fmt
    return None


def redact_image_bytes(
    image_bytes: bytes,
    region: ImageRegion,
    spans_for_region: List[MatchSpan],
) -> bytes:
    """Return `image_bytes` with the detected in-image PII covered by solid,
    opaque black rectangles, re-encoded in the same format it arrived in.

    Decision (the correctness spine):

    - **Targeted** iff `region.recognizedElements is not None` AND every span
      in `spans_for_region` resolves to at least one rectangle (via
      `_rects_for_image_span`). The rectangle set is the union of every span's
      rectangles, each padded (`Box_Padding`) and clamped to the image bounds.
    - **Whole_Image_Fallback** otherwise — untrustworthy geometry
      (`recognizedElements is None`), or any span that mapped to zero
      rectangles (belt-and-braces for the spine: a real PII span always
      overlaps a line element, so this only fires on a pathological mismatch,
      and when it does we cover everything rather than risk a leak). The
      rectangle set is a single full-image rectangle `(0, 0, W, H)`.

    Palettized (`P`) and alpha-palette / luminance-alpha (`PA`/`LA`) images are
    converted to `RGB` before drawing so the fill is exact black
    (Solid_Redaction_Block).

    Args:
        image_bytes: the raw encoded bytes of the embedded image.
        region: the `ImageRegion` the bytes came from; its
            `recognizedElements` drive the targeted-vs-fallback decision.
        spans_for_region: the `image:` `MatchSpan`s detected for this region.

    Returns:
        The redacted image's bytes, re-encoded in the same format the input
        arrived in (magic-byte inference / PNG fallback when Pillow reports no
        format), so the surrounding container's stream/content-type stays
        valid.

    Raises:
        ImageRedactionError: on any decode, draw, or encode failure.
    """
    # Lazy import so this module imports without Pillow installed and the pure
    # helpers above stay unit-testable with no PIL present.
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # pragma: no cover - environment/packaging issue
        raise ImageRedactionError(
            "Pillow (PIL) is required to redact images but is not importable: "
            "{}".format(exc)
        ) from exc

    # --- Decode -----------------------------------------------------------
    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
    except Exception as exc:
        raise ImageRedactionError(
            "Failed to decode image for redaction: {}".format(exc)
        ) from exc

    original_format = image.format  # remembered before any mode conversion
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ImageRedactionError(
            "Decoded image has non-positive dimensions {}x{}".format(width, height)
        )

    # --- Decide targeted vs Whole_Image_Fallback --------------------------
    padding = _read_box_padding()
    rects: List[Rect] = []
    targeted = region.recognizedElements is not None
    if targeted:
        for span in spans_for_region:
            span_rects = _rects_for_image_span(
                region.recognizedElements, span, width, height
            )
            if not span_rects:
                # A span mapped to zero rectangles: fall back to whole-image
                # rather than risk leaving this span's PII uncovered.
                targeted = False
                break
            rects.extend(span_rects)

    if targeted:
        rects = [_pad_and_clamp_rect(rect, width, height, padding) for rect in rects]
    else:
        rects = [(0.0, 0.0, float(width), float(height))]

    logger.info(
        "redact_image_bytes sourceLocation=%s mode=%s rects=%d fallback=%s",
        region.sourceLocation,
        "targeted" if targeted else "whole_image",
        len(rects),
        not targeted,
    )

    # --- Draw -------------------------------------------------------------
    try:
        if image.mode in _MODES_NEEDING_RGB:
            image = image.convert("RGB")
        black = 0 if image.mode in _SINGLE_CHANNEL_MODES else (0, 0, 0)
        draw = ImageDraw.Draw(image)
        for left, top, right, bottom in rects:
            # ImageDraw fills the rectangle inclusive of the given corners;
            # rounding to ints keeps the coverage at least as large as the
            # float rect (over-redaction is the safe direction).
            draw.rectangle(
                [
                    int(left),
                    int(top),
                    int(round(right)),
                    int(round(bottom)),
                ],
                fill=black,
            )
    except Exception as exc:
        raise ImageRedactionError(
            "Failed to draw redaction rectangles on image: {}".format(exc)
        ) from exc

    # --- Re-encode in the same format -------------------------------------
    save_format = original_format or _infer_format_from_magic_bytes(image_bytes) or "PNG"
    try:
        buf = BytesIO()
        image.save(buf, format=save_format)
        return buf.getvalue()
    except Exception as exc:
        raise ImageRedactionError(
            "Failed to re-encode redacted image as {}: {}".format(save_format, exc)
        ) from exc
