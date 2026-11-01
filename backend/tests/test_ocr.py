"""Tests for `agent/tools/ocr.py` — the in-image-redaction `recognize_image_text`
(in-image-redaction design.md "Component 1: `backend/agent/tools/ocr.py` —
extended (not rewritten)").

The embedded-image OCR engine now retargets to a Sonnet-class multimodal
Bedrock model and, in a single `Converse` call, returns both the recognized
text AND per-line geometry: the assistant text is a JSON array of
`{"text": ..., "box": [x0, y0, x1, y1]}` entries. `recognize_image_text` parses
that array defensively, assembles `recognizedText` from the entry texts, and
attaches `recognizedElements` (a `list[RecognizedElement]` for the lines whose
box is plausible — implausible-box lines are dropped individually — and `None`
only when no line produced a plausible box).

These tests therefore drive the tool against a **fake `converse` client**
(`unittest.mock` only — never moto, mirroring `tests/test_llm_pii_detect.py` /
`tests/test_guardrails_pii_detect.py`): a plain fake that records every call
and returns a canned **JSON** response, plus sequenced/behaviour fakes that
raise transient/terminal errors carrying a duck-typed `response` dict so the
imported `guardrails_pii_detect._is_retryable` (reused by the tool) classifies
them as it does in production.

This file covers:

- Requests are issued only for a pinned model identifier
- Coordinate normalization is total and never yields a bad box
- Pixel-vs-fractional interpretation is correct and deterministic
- Recognized text is assembled from the elements, so offsets are comparable
- Every localization request pins the inference configuration
- The audit record identifies the run without recording PII
- The same instances are returned, with only text and geometry touched
- Example, edge-case, and structural unit tests for `ocr.py` (prompt/
  version constants, `_parse_localization_response`, single-call
  text+geometry, list-vs-None geometry, and the retained-and-adapted
  coverage: retry accounting, never-raise/partial results, skip
  ineligible, lazy client, import-without-deps, distinct env var).
"""
import hashlib
import inspect
import json
import logging
import os
import sys
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent.tools import ocr  # noqa: E402
from agent.tools.ocr import recognize_image_text  # noqa: E402
from shared.models import BoundingBox, ImageRegion, RecognizedElement  # noqa: E402

#: A pinned, versioned Sonnet-class inference-profile id (ends in `-v<N>:<N>`),
#: the exact shape `CDK_Stack` now supplies via `IMAGE_OCR_MODEL_ID` for the
#: in-image-redaction feature. Accepted by `_is_pinned_model_id`.
PINNED_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


# ---------------------------------------------------------------------------
# Image byte helpers (valid magic numbers + near-misses + real PNGs)
# ---------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
GIF_MAGIC = b"GIF8"


def _png_bytes(trailing: bytes = b"\x00\x01\x02\x03") -> bytes:
    """A minimal byte string that classifies as PNG by magic number but is NOT
    a decodable image, so `_image_pixel_dimensions` cannot measure it (the
    geometry becomes untrustworthy while a request is still issued)."""
    return PNG_MAGIC + trailing


def _webp_bytes(middle: bytes = b"\x00\x00\x00\x00", trailing: bytes = b"\x00") -> bytes:
    """A minimal byte string that classifies as WEBP (`RIFF....WEBP`)."""
    return b"RIFF" + middle + b"WEBP" + trailing


def _build_png(width: int, height: int, color=(255, 255, 255)) -> bytes:
    """A real, decodable PNG of the given pixel dimensions (Pillow is a real
    dev/runtime dependency now, so tests that exercise `_image_pixel_dimensions`
    build small in-memory PNGs)."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


#: A single default real PNG, built once, so the property loops that only need
#: *a* decodable image do not re-encode one per example.
_DEFAULT_PNG = _build_png(120, 60)


def _real_png(width: int = 120, height: int = 60) -> bytes:
    if width == 120 and height == 60:
        return _DEFAULT_PNG
    return _build_png(width, height)


def _reference_format(image_bytes: bytes):
    """An independent oracle for the magic-byte format detection, written
    separately from `ocr._detect_image_format` so a property test asserting
    the two agree is not a tautology."""
    if not image_bytes:
        return None
    if image_bytes.startswith(PNG_MAGIC):
        return "png"
    if image_bytes.startswith(JPEG_MAGIC):
        return "jpeg"
    if image_bytes.startswith(GIF_MAGIC):
        return "gif"
    if len(image_bytes) >= 12 and image_bytes[0:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "webp"
    return None


# ---------------------------------------------------------------------------
# Fakes and helpers (unittest.mock only — no moto)
# ---------------------------------------------------------------------------


def _fake_response(assistant_text: str) -> dict:
    """A Bedrock `Converse` response whose single content block carries
    `assistant_text` verbatim (the shape `_assistant_text` walks)."""
    return {"output": {"message": {"content": [{"text": assistant_text}]}}}


def _json_text(entries) -> str:
    """Serialize `[(text, box), ...]` as the JSON array the Sonnet localization
    prompt asks for: `[{"text": ..., "box": [...]}, ...]`."""
    return json.dumps([{"text": text, "box": list(box)} for text, box in entries])


def _fake_json_response(entries) -> dict:
    """A `Converse` response whose assistant text is the JSON localization
    array for `entries` (a list of `(text, box)` pairs)."""
    return _fake_response(_json_text(entries))


def _sent_image(call: dict) -> dict:
    """The `image` block of a recorded `converse` call's single user content
    block."""
    return call["messages"][0]["content"][0]["image"]


class FakeConverseClient:
    """Stand-in for the `bedrock-runtime` client's `converse` surface.

    Records every call's kwargs and returns a canned response, either looked
    up by the exact image bytes sent or a fixed default.
    """

    def __init__(self, response_for_bytes=None, default_response=None):
        self.calls = []
        self.response_for_bytes = response_for_bytes or {}
        self.default_response = default_response

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        image_bytes = kwargs["messages"][0]["content"][0]["image"]["source"]["bytes"]
        if image_bytes in self.response_for_bytes:
            return self.response_for_bytes[image_bytes]
        if self.default_response is not None:
            return self.default_response
        return _fake_json_response([])


class FakeThrottlingError(Exception):
    """Duck-typed like a botocore `ThrottlingException` — retryable via its
    `response["Error"]["Code"]`."""

    def __init__(self):
        super().__init__("throttled")
        self.response = {"Error": {"Code": "ThrottlingException"}}


class Fake5xxError(Exception):
    """Duck-typed like a botocore 5xx service error — retryable via its
    `response["ResponseMetadata"]["HTTPStatusCode"]`."""

    def __init__(self):
        super().__init__("service unavailable")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": 503}}


class FakeNonRetryableError(Exception):
    """Duck-typed like a botocore `ValidationException` — NOT retryable."""

    def __init__(self):
        super().__init__("validation error")
        self.response = {"Error": {"Code": "ValidationException"}}


class SequencedConverseClient:
    """A fake whose `converse` raises `error_factory()` for its first
    `failures` calls, then returns `success_response`. Counts calls."""

    def __init__(self, failures, error_factory, success_response):
        self.remaining_failures = failures
        self.error_factory = error_factory
        self.success_response = success_response
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise self.error_factory()
        return self.success_response


class BehaviorConverseClient:
    """A fake whose `converse` behaves per-image: raise, return a malformed
    response, or return a valid JSON localization array — keyed on the sent
    bytes."""

    def __init__(self, behavior_by_bytes):
        self.behavior_by_bytes = behavior_by_bytes
        self.calls = []

    def converse(self, **kwargs):
        image_bytes = kwargs["messages"][0]["content"][0]["image"]["source"]["bytes"]
        self.calls.append(image_bytes)
        behavior, payload = self.behavior_by_bytes[image_bytes]
        if behavior == "raise":
            raise FakeNonRetryableError()
        if behavior == "malformed":
            return {"unexpected": "shape"}
        # A single-line localization array; `recognizedText` assembles to
        # exactly `payload`.
        return _fake_json_response([(payload, [0.1, 0.1, 0.5, 0.3])])


@contextmanager
def ocr_configured(client, model_id=PINNED_MODEL_ID):
    """Set `IMAGE_OCR_MODEL_ID` and inject `client` as the module's lazily
    created `bedrock-runtime` client, restoring both afterwards (and resetting
    the module-global `_bedrock_runtime_client` between tests).

    A context manager rather than a fixture so it can be used inside
    Hypothesis-driven tests without function-scoped-fixture reuse across
    examples, mirroring `test_llm_pii_detect.llm_configured`. Pass
    `model_id=None` to leave the variable unset.
    """
    previous_client = ocr._bedrock_runtime_client
    previous_env = os.environ.get(ocr.IMAGE_OCR_MODEL_ID_ENV_VAR)
    ocr._bedrock_runtime_client = client
    if model_id is None:
        os.environ.pop(ocr.IMAGE_OCR_MODEL_ID_ENV_VAR, None)
    else:
        os.environ[ocr.IMAGE_OCR_MODEL_ID_ENV_VAR] = model_id
    try:
        yield client
    finally:
        ocr._bedrock_runtime_client = previous_client
        if previous_env is None:
            os.environ.pop(ocr.IMAGE_OCR_MODEL_ID_ENV_VAR, None)
        else:
            os.environ[ocr.IMAGE_OCR_MODEL_ID_ENV_VAR] = previous_env


def _region(source_location="para[0].image[0]", image_bytes=None, bounding_box=None):
    return ImageRegion(
        zone="image",
        sourceLocation=source_location,
        imageBytes=image_bytes,
        boundingBox=bounding_box,
    )


# ===========================================================================
# Requests are issued only for a pinned model identifier
# ===========================================================================


@st.composite
def _model_id_and_pinned(draw):
    """Draw `(env_value, is_pinned)` across the id shapes: absent, blank, a
    pinned `...-v<N>:<N>` id of several families (Sonnet, Haiku, Nova), an
    unversioned near-miss, and a `...latest` alias."""
    kind = draw(st.sampled_from(["absent", "blank", "pinned", "unversioned", "latest"]))
    if kind == "absent":
        return None, False
    if kind == "blank":
        return draw(st.sampled_from(["", "   ", "\t", "  \n "])), False
    if kind == "pinned":
        base = draw(
            st.sampled_from(
                [
                    "us.anthropic.claude-sonnet-4-5-20250929",
                    "anthropic.claude-sonnet-4-5-20250929",
                    "anthropic.claude-haiku-4-5-20251001",
                    "us.anthropic.claude-haiku-4-5-20251001",
                    "amazon.nova-lite",
                    "eu.anthropic.claude-3-5-sonnet-20240620",
                    "us.amazon.nova-pro",
                ]
            )
        )
        major = draw(st.integers(min_value=0, max_value=9))
        minor = draw(st.integers(min_value=0, max_value=9))
        return f"{base}-v{major}:{minor}", True
    if kind == "unversioned":
        return (
            draw(
                st.sampled_from(
                    [
                        "anthropic.claude-sonnet-4-5",
                        "anthropic.claude-haiku-4-5",
                        "amazon.nova-lite",
                        "us.anthropic.claude-3-sonnet",
                    ]
                )
            ),
            False,
        )
    return (
        draw(
            st.sampled_from(
                [
                    "anthropic.claude-sonnet-latest",
                    "amazon.nova-lite-latest",
                    "us.anthropic.claude-3-sonnet-latest",
                ]
            )
        ),
        False,
    )


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_model_id_and_pinned())
def test_property_8_requests_only_for_pinned_model_id(sample):
    """`recognize_image_text` issues exactly one `converse` call and attaches
    geometry iff `IMAGE_OCR_MODEL_ID` is a pinned, versioned id (of any
    family), and otherwise issues zero calls and returns the region unchanged
    with `recognizedText`/`recognizedElements` unpopulated.

    Requests are issued only for a pinned model identifier
    """
    model_value, is_pinned = sample
    region = _region(image_bytes=_real_png())
    client = FakeConverseClient(
        default_response=_fake_json_response([("身份证 张伟", [0.1, 0.1, 0.6, 0.3])])
    )

    with ocr_configured(client, model_id=model_value):
        result = recognize_image_text([region])

    assert result is [region] or result == [region]
    if is_pinned:
        assert len(client.calls) == 1
        assert region.recognizedText == "身份证 张伟"
        assert region.recognizedElements is not None
        assert len(region.recognizedElements) == 1
    else:
        assert client.calls == []
        assert region.recognizedText is None
        assert region.recognizedElements is None


def test_property_8_example_pinned_and_alias():
    """Example-level check: the exact `CDK_Stack` Sonnet id is active; a
    `latest` alias, an unversioned id, and a blank value are all dormant.

    Requests are issued only for a pinned model identifier
    """
    client = FakeConverseClient(default_response=_fake_json_response([("t", [0.1, 0.1, 0.5, 0.3])]))
    with ocr_configured(client, model_id=PINNED_MODEL_ID):
        recognize_image_text([_region(image_bytes=_real_png())])
    assert len(client.calls) == 1

    for dormant in ("anthropic.claude-sonnet-latest", "anthropic.claude-sonnet-4-5", ""):
        region = _region(image_bytes=_real_png())
        client = FakeConverseClient(default_response=_fake_json_response([("t", [0.1, 0.1, 0.5, 0.3])]))
        with ocr_configured(client, model_id=dormant if dormant else None):
            recognize_image_text([region])
        assert client.calls == []
        assert region.recognizedText is None
        assert region.recognizedElements is None


# ===========================================================================
# Coordinate normalization is total, never a bad box
# ===========================================================================

_COORD = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-100000, max_value=100000),
    st.booleans(),
    st.text(max_size=3),
    st.none(),
)


@st.composite
def _raw_box_and_dims(draw):
    """Draw `(raw_box, width_px, height_px)` where `raw_box` is a list of
    0..6 values (numeric, non-numeric, bool, NaN, inf, or the wrong length)
    and the dimensions are positive."""
    length = draw(st.integers(min_value=0, max_value=6))
    raw_box = draw(st.lists(_COORD, min_size=length, max_size=length))
    width = draw(st.integers(min_value=1, max_value=5000))
    height = draw(st.integers(min_value=1, max_value=5000))
    return raw_box, width, height


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_raw_box_and_dims())
def test_property_5_normalization_is_total(sample):
    """`_normalize_box` returns either a positive-area `BoundingBox` whose
    edges lie within `[0, 1]`, or `None` — it never raises and never returns a
    degenerate, inverted, or out-of-bounds box.

    Coordinate normalization is total and never yields a bad box
    """
    raw_box, width, height = sample

    result = ocr._normalize_box(raw_box, width, height)  # must never raise

    if result is None:
        return
    assert isinstance(result, BoundingBox)
    assert 0.0 <= result.left <= 1.0
    assert 0.0 <= result.top <= 1.0
    assert result.width > 0.0
    assert result.height > 0.0
    # Positive area entirely within the unit square.
    assert result.left + result.width <= 1.0 + 1e-9
    assert result.top + result.height <= 1.0 + 1e-9


# ===========================================================================
# Pixel-vs-fractional interpretation, deterministic
# ===========================================================================


@st.composite
def _interpretable_box_case(draw):
    """Draw `(regime, raw_box, width, height)` where a `"fractional"` case has
    every coordinate `<= 1.0` and a `"pixel"` case has at least one coordinate
    `> 1.0`; both are well-ordered so they normalize to a plausible box."""
    regime = draw(st.sampled_from(["fractional", "pixel"]))
    width = draw(st.integers(min_value=10, max_value=2000))
    height = draw(st.integers(min_value=10, max_value=2000))
    if regime == "fractional":
        x0 = draw(st.floats(min_value=0.0, max_value=0.90))
        x1 = draw(st.floats(min_value=x0 + 0.02, max_value=1.0))
        y0 = draw(st.floats(min_value=0.0, max_value=0.90))
        y1 = draw(st.floats(min_value=y0 + 0.02, max_value=1.0))
        return regime, [x0, y0, x1, y1], width, height
    # Pixel: integers >= 2 guarantee at least one coordinate above 1.0.
    x0 = draw(st.integers(min_value=2, max_value=width - 1))
    x1 = draw(st.integers(min_value=x0 + 1, max_value=width))
    y0 = draw(st.integers(min_value=2, max_value=height - 1))
    y1 = draw(st.integers(min_value=y0 + 1, max_value=height))
    return regime, [x0, y0, x1, y1], width, height


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_interpretable_box_case())
def test_property_6_pixel_vs_fractional_interpretation(case):
    """A box whose coordinates are all `<= 1.0` is interpreted as fractional
    and returned (clamped) unchanged; a box with any coordinate `> 1.0` is
    interpreted as pixels and divided by the actual dimensions. The same input
    always yields the same interpretation.

    Pixel-vs-fractional interpretation is correct and deterministic
    """
    regime, raw_box, width, height = case
    x0, y0, x1, y1 = raw_box

    result = ocr._normalize_box(raw_box, width, height)
    assert result is not None

    if regime == "fractional":
        assert result.left == pytest.approx(x0)
        assert result.top == pytest.approx(y0)
        assert result.width == pytest.approx(x1 - x0)
        assert result.height == pytest.approx(y1 - y0)
    else:
        assert result.left == pytest.approx(x0 / width)
        assert result.top == pytest.approx(y0 / height)
        assert result.width == pytest.approx((x1 - x0) / width)
        assert result.height == pytest.approx((y1 - y0) / height)

    # Determinism: the same input yields an equal result every time.
    assert ocr._normalize_box(list(raw_box), width, height) == result


# ===========================================================================
# Recognized text assembled from elements, offsets comparable
# ===========================================================================

_LINE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=0x20, blacklist_categories=("Cs",)),
    max_size=12,
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_LINE_TEXT, min_size=1, max_size=6))
def test_property_1_recognized_text_assembled_from_elements(texts):
    """For any parsed list of localization entries, the assembled
    `recognizedText` contains each element's text at exactly
    `recognizedText[startOffset:endOffset]`, and the elements' offset ranges
    are non-overlapping and in reading order — so an `image:` span's offsets
    and a `RecognizedElement`'s offsets index the same string.

    Recognized text is assembled from the elements, so offsets are comparable
    """
    # Every box is plausible (fractional), so `recognizedElements` is a list.
    entries = [(text, [0.1, 0.1, 0.5, 0.3]) for text in texts]
    region = _region(image_bytes=_real_png())
    client = FakeConverseClient(default_response=_fake_json_response(entries))

    with ocr_configured(client):
        recognize_image_text([region])

    assert region.recognizedText == "\n".join(texts)

    elements = region.recognizedElements
    assert elements is not None
    assert len(elements) == len(texts)

    for element in elements:
        assert 0 <= element.startOffset <= element.endOffset <= len(region.recognizedText)
        assert region.recognizedText[element.startOffset : element.endOffset] == element.text

    # Non-overlapping and in reading order.
    for earlier, later in zip(elements, elements[1:]):
        assert earlier.endOffset <= later.startOffset


# ===========================================================================
# Every request pins the inference configuration
# ===========================================================================


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.integers(min_value=0, max_value=255), min_size=1, max_size=6))
def test_property_12_every_request_pins_inference_config(seeds):
    """Every `converse` request pins `temperature == 0.0` and omits `topP`
    (Sonnet/Haiku reject a request specifying both), with
    `maxTokens == MAX_OUTPUT_TOKENS`.

    Every localization request pins the inference configuration
    """
    regions = [_region(f"loc[{i}]", image_bytes=_png_bytes(bytes([s]))) for i, s in enumerate(seeds)]
    client = FakeConverseClient(default_response=_fake_json_response([("line", [0.1, 0.1, 0.5, 0.3])]))

    with ocr_configured(client):
        recognize_image_text(regions)

    assert client.calls, "expected at least one request for an eligible region"
    for call in client.calls:
        config = call["inferenceConfig"]
        assert config["temperature"] == 0.0, call
        assert "topP" not in config, call
        assert config["maxTokens"] == ocr.MAX_OUTPUT_TOKENS, call


# ===========================================================================
# Audit record identifies the run without recording PII
# ===========================================================================

_CJK_TOKEN = st.text(
    alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),
    min_size=2,
    max_size=6,
)


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(_CJK_TOKEN, st.binary(max_size=24))
def test_property_13_audit_record_identifies_run_without_pii(caplog, recognized, marker_tail):
    """The per-region audit record contains the `sourceLocation`, model id,
    prompt version, the SHA-256 digest and byte length of exactly the image
    sent, and the rectangle count — and contains neither the image bytes
    (probed with an ASCII marker embedded in them), the recognized text, nor
    any box coordinates.

    The audit record identifies the run without recording PII
    """
    marker = b"SECRET_IMG_MARKER"
    # Trailing bytes after a PNG's IEND are ignored by the decoder, so this is
    # still a measurable 120x60 image whose bytes carry the marker.
    image_bytes = _real_png() + marker + marker_tail
    region = _region("para[2].image[0]", image_bytes=image_bytes)
    # Distinctive fractional coordinates (each carries a "." so they cannot
    # coincidentally appear inside the hex digest / integer fields).
    box = [0.1234567, 0.2345678, 0.7654321, 0.8765432]
    client = FakeConverseClient(default_response=_fake_json_response([(recognized, box)]))

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agent.tools.ocr"):
        with ocr_configured(client):
            recognize_image_text([region])

    audit_records = [
        record for record in caplog.records if "LLM image OCR audit" in record.getMessage()
    ]
    assert len(audit_records) == 1, [r.getMessage() for r in caplog.records]
    record = audit_records[0]
    message = record.getMessage()

    assert record.levelno == logging.INFO
    # Positive: the record identifies the run.
    assert "para[2].image[0]" in message
    assert PINNED_MODEL_ID in message
    assert ocr.OCR_PROMPT_VERSION in message
    assert hashlib.sha256(image_bytes).hexdigest() in message
    assert f"imageBytes={len(image_bytes)}" in message
    # One plausible box -> one rectangle, no whole-image fallback.
    assert "rectangles=1" in message
    assert "wholeImageFallback=False" in message

    # Negative: neither the image bytes (via the embedded ASCII marker), the
    # recognized text, nor any box coordinate appears in the record.
    assert "SECRET_IMG_MARKER" not in message
    if recognized:
        assert recognized not in message
    for coordinate in box:
        assert str(coordinate) not in message


# ===========================================================================
# Same instances, only text + geometry touched
# ===========================================================================


@st.composite
def _mixed_regions(draw):
    """Draw a list mixing eligible (real PNG) and ineligible (absent/empty
    bytes) regions, each with a distinct `sourceLocation` and (sometimes) a
    `boundingBox`."""
    kinds = draw(st.lists(st.sampled_from(["eligible", "none", "empty"]), min_size=0, max_size=6))
    regions = []
    for index, kind in enumerate(kinds):
        bbox = None
        if draw(st.booleans()):
            bbox = BoundingBox(left=float(index), top=1.0, width=2.0, height=3.0)
        if kind == "eligible":
            regions.append(_region(f"loc[{index}]", image_bytes=_real_png(), bounding_box=bbox))
        elif kind == "none":
            regions.append(_region(f"loc[{index}]", image_bytes=None, bounding_box=bbox))
        else:
            regions.append(_region(f"loc[{index}]", image_bytes=b"", bounding_box=bbox))
    return regions


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_mixed_regions())
def test_property_14_same_instances_only_text_and_geometry_touched(regions):
    """`recognize_image_text` returns the same `ImageRegion` instances in the
    same order, mutates only `recognizedText` and `recognizedElements` and only
    on eligible regions, and leaves `zone`/`sourceLocation`/`boundingBox`/
    `imageBytes` untouched. An empty input is returned unchanged with zero
    requests.

    The same instances are returned, with only text and geometry touched
    """
    snapshot = [
        (id(r), r.zone, r.sourceLocation, r.boundingBox, r.imageBytes, bool(r.imageBytes))
        for r in regions
    ]
    client = FakeConverseClient(default_response=_fake_json_response([("line", [0.1, 0.1, 0.5, 0.3])]))

    with ocr_configured(client):
        result = recognize_image_text(regions)

    # Same list object, same instances, same order.
    assert result is regions
    assert [id(r) for r in result] == [rid for rid, *_ in snapshot]

    for region, (_, zone, loc, bbox, image_bytes, was_eligible) in zip(result, snapshot):
        assert region.zone == zone
        assert region.sourceLocation == loc
        assert region.boundingBox is bbox
        assert region.imageBytes == image_bytes
        if not was_eligible:
            # Ineligible regions are never touched — neither field is set.
            assert region.recognizedText is None
            assert region.recognizedElements is None

    # Empty input clause: returned unchanged, zero requests.
    empty_client = FakeConverseClient(default_response=_fake_json_response([]))
    empty = []
    with ocr_configured(empty_client):
        empty_result = recognize_image_text(empty)
    assert empty_result is empty
    assert empty_client.calls == []


# ===========================================================================
# Example / edge-case / structural unit tests
# ===========================================================================


def test_signature_is_list_in_list_out():
    """`recognize_image_text` takes a single `image_regions` positional
    argument (its public contract is unchanged).
    """
    params = list(inspect.signature(recognize_image_text).parameters)
    assert params == ["image_regions"]


def test_ocr_prompt_is_module_level_and_asks_for_text_and_box_json():
    """`OCR_PROMPT` is a module-level constant carrying
    `OCR_PROMPT_VERSION == "in-image-redaction/v1"`, asks for a JSON array of
    per-line `text`+`box` entries, and forbids translation, transliteration,
    and commentary.
    """
    assert ocr.OCR_PROMPT_VERSION == "in-image-redaction/v1"

    prompt = ocr.OCR_PROMPT
    assert isinstance(prompt, str)
    assert ocr.OCR_PROMPT_VERSION in prompt

    lowered = prompt.lower()
    # CJK recognition is called out.
    assert "chinese" in lowered
    assert "japanese" in lowered
    assert "korean" in lowered
    # Asks for text + box as JSON.
    assert "json" in lowered
    assert '"text"' in prompt
    assert '"box"' in prompt
    assert "reading order" in lowered
    # Forbids translation / transliteration / commentary.
    assert "not translate" in lowered
    assert "transliterate" in lowered
    assert "commentary" in lowered


def test_parse_localization_response_valid_array():
    """A well-formed JSON array parses to `(text, raw_box)` pairs verbatim.
    """
    text = json.dumps([{"text": "a", "box": [1, 2, 3, 4]}, {"text": "b", "box": [5, 6, 7, 8]}])
    assert ocr._parse_localization_response(text) == [("a", [1, 2, 3, 4]), ("b", [5, 6, 7, 8])]


def test_parse_localization_response_tolerates_prose_and_fences():
    """Leading prose and a ```json code fence around the array are tolerated
    (the parser locates the first `[` and last `]`).
    """
    array = json.dumps([{"text": "ok", "box": [0.1, 0.2, 0.3, 0.4]}])
    prose = "Sure, here is the transcription:\n" + array + "\nLet me know if you need more."
    fenced = "```json\n" + array + "\n```"

    expected = [("ok", [0.1, 0.2, 0.3, 0.4])]
    assert ocr._parse_localization_response(prose) == expected
    assert ocr._parse_localization_response(fenced) == expected


def test_parse_localization_response_skips_malformed_entries():
    """Entries that are not shaped `{"text": str, "box": [4 items]}` are
    skipped, while well-formed siblings are kept.
    """
    mixed = json.dumps(
        [
            {"text": "keep", "box": [1, 2, 3, 4]},
            {"text": "no box"},
            {"box": [1, 2, 3, 4]},
            {"text": "short box", "box": [1, 2, 3]},
            "not a dict",
            {"text": 123, "box": [1, 2, 3, 4]},
        ]
    )
    assert ocr._parse_localization_response(mixed) == [("keep", [1, 2, 3, 4])]


def test_parse_localization_response_garbage_returns_none():
    """Text with no bracketed region, invalid JSON, or an empty string returns
    `None` (never raising).
    """
    assert ocr._parse_localization_response("no brackets here") is None
    assert ocr._parse_localization_response("") is None
    assert ocr._parse_localization_response("[oops not json") is None
    assert ocr._parse_localization_response("[{]}") is None
    assert ocr._parse_localization_response(json.dumps({"text": "obj not array"})) is None


def test_single_converse_call_yields_text_and_geometry():
    """A single `Converse` call yields both the assembled recognized text and
    per-element geometry; pixel coordinates are normalized to `[0, 1]` of the
    image's actual pixel dimensions.
    """
    region = _region(image_bytes=_real_png(width=200, height=100))
    entries = [("身份证号 123", [20, 10, 120, 40]), ("张三", [20, 50, 80, 80])]
    client = FakeConverseClient(default_response=_fake_json_response(entries))

    with ocr_configured(client):
        recognize_image_text([region])

    assert len(client.calls) == 1
    assert region.recognizedText == "身份证号 123\n张三"

    elements = region.recognizedElements
    assert elements is not None
    assert [e.text for e in elements] == ["身份证号 123", "张三"]

    first = elements[0].boundingBox
    assert first.left == pytest.approx(20 / 200)
    assert first.top == pytest.approx(10 / 100)
    assert first.width == pytest.approx((120 - 20) / 200)
    assert first.height == pytest.approx((40 - 10) / 100)


def test_recognized_elements_list_when_all_boxes_plausible():
    """When every entry produces a plausible box and there is >=1 element,
    `recognizedElements` is a list of `RecognizedElement`.
    """
    region = _region(image_bytes=_real_png())
    entries = [("a", [0.1, 0.1, 0.4, 0.3]), ("b", [0.5, 0.5, 0.9, 0.8])]
    client = FakeConverseClient(default_response=_fake_json_response(entries))

    with ocr_configured(client):
        recognize_image_text([region])

    assert region.recognizedElements is not None
    assert len(region.recognizedElements) == 2
    assert all(isinstance(e, RecognizedElement) for e in region.recognizedElements)


def test_recognized_elements_drops_only_the_implausible_box():
    """in-image-redaction bugfix: a single implausible box drops ONLY that
    line's geometry and keeps the other plausible boxes, instead of nulling
    the whole region (which caused random whole-image blackouts). The dropped
    line's text still appears in `recognizedText`.
    """
    region = _region(image_bytes=_real_png())
    # The second box is inverted (x1 <= x0), hence implausible; the first and
    # third are fine.
    entries = [
        ("good1", [0.1, 0.1, 0.5, 0.3]),
        ("bad", [0.9, 0.9, 0.1, 0.1]),
        ("good2", [0.1, 0.5, 0.5, 0.7]),
    ]
    client = FakeConverseClient(default_response=_fake_json_response(entries))

    with ocr_configured(client):
        recognize_image_text([region])

    # recognizedText still contains every line, including the dropped one.
    assert region.recognizedText == "good1\nbad\ngood2"
    # Only the two plausible lines carry geometry; the bad one is gone.
    assert region.recognizedElements is not None
    assert [e.text for e in region.recognizedElements] == ["good1", "good2"]
    # The kept elements' offsets still index the assembled text correctly.
    for e in region.recognizedElements:
        assert region.recognizedText[e.startOffset:e.endOffset] == e.text


def test_recognized_elements_none_only_when_every_box_implausible():
    """`recognizedElements` is `None` only when NO entry produced a plausible
    box; `recognizedText` stays assembled so downstream detection still runs
    and the redactor whole-images the region.
    """
    region = _region(image_bytes=_real_png())
    # Both boxes inverted (x1 <= x0 / y1 <= y0) -> all implausible.
    entries = [("bad1", [0.9, 0.9, 0.1, 0.1]), ("bad2", [0.8, 0.8, 0.2, 0.2])]
    client = FakeConverseClient(default_response=_fake_json_response(entries))

    with ocr_configured(client):
        recognize_image_text([region])

    assert region.recognizedText == "bad1\nbad2"
    assert region.recognizedElements is None


def test_unmeasurable_image_yields_text_but_no_geometry():
    """When the image bytes cannot be measured (magic-byte PNG that does not
    decode), the request still completes: `recognizedText` is assembled but
    `recognizedElements` is `None` (untrustworthy geometry -> whole-image
    fallback).
    """
    region = _region(image_bytes=_png_bytes(b"not a real png body"))
    client = FakeConverseClient(default_response=_fake_json_response([("line", [0.1, 0.1, 0.5, 0.3])]))

    with ocr_configured(client):
        recognize_image_text([region])

    assert region.recognizedText == "line"
    assert region.recognizedElements is None


def test_request_block_is_image_with_raw_bytes_via_converse():
    """The captured request addresses `converse` and carries the unit image as
    `image={format, source={bytes}}` with RAW (un-base64'd) bytes, and only
    the prompt (`system`) plus that single image block.
    """
    image_bytes = _real_png()
    region = _region(image_bytes=image_bytes)
    client = FakeConverseClient(default_response=_fake_json_response([("t", [0.1, 0.1, 0.5, 0.3])]))

    with ocr_configured(client):
        recognize_image_text([region])

    assert len(client.calls) == 1
    call = client.calls[0]
    assert set(call.keys()) == {"modelId", "system", "messages", "inferenceConfig"}
    assert call["modelId"] == PINNED_MODEL_ID
    assert call["system"] == [{"text": ocr.OCR_PROMPT}]

    content = call["messages"][0]["content"]
    assert len(content) == 1
    image_block = content[0]["image"]
    assert image_block["format"] == "png"
    # RAW bytes: the exact input, not base64-encoded (the SDK does that).
    assert image_block["source"]["bytes"] == image_bytes
    assert isinstance(image_block["source"]["bytes"], bytes)


# --- Retained coverage: format detection ------------------------------------

_TRAILING = st.binary(max_size=8)


@st.composite
def _image_bytes_sample(draw):
    """Draw image bytes reaching every branch: valid PNG/JPEG/GIF/WEBP magic
    prefixes with random trailing bytes, and near-misses — a `RIFF...` that is
    not `...WEBP`, a BMP `BM` prefix, and byte strings shorter than the
    shortest magic number."""
    kind = draw(
        st.sampled_from(
            ["png", "jpeg", "gif", "webp", "riff_not_webp", "bmp", "too_short", "random"]
        )
    )
    if kind == "png":
        return PNG_MAGIC + draw(_TRAILING)
    if kind == "jpeg":
        return JPEG_MAGIC + draw(_TRAILING)
    if kind == "gif":
        return GIF_MAGIC + draw(_TRAILING)
    if kind == "webp":
        return b"RIFF" + draw(st.binary(min_size=4, max_size=4)) + b"WEBP" + draw(_TRAILING)
    if kind == "riff_not_webp":
        tag = draw(st.binary(min_size=4, max_size=4).filter(lambda b: b != b"WEBP"))
        return b"RIFF" + draw(st.binary(min_size=4, max_size=4)) + tag + draw(_TRAILING)
    if kind == "bmp":
        return b"BM" + draw(_TRAILING)
    if kind == "too_short":
        return draw(st.binary(max_size=2))
    return draw(st.binary(max_size=16))


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_image_bytes_sample())
def test_declared_format_matches_bytes(image_bytes):
    """`_detect_image_format` agrees with an independent magic-byte oracle, and
    `recognize_image_text` issues a `converse` request — declaring that same
    format — iff the bytes carry one of the four supported magic numbers.
    """
    expected = _reference_format(image_bytes)
    assert ocr._detect_image_format(image_bytes) == expected

    if not image_bytes:
        # An empty bytestring is "absent/empty", skipped before format
        # detection; there is nothing for this to assert about it.
        return

    region = _region(image_bytes=image_bytes)
    client = FakeConverseClient(default_response=_fake_json_response([("t", [0.1, 0.1, 0.5, 0.3])]))
    with ocr_configured(client):
        recognize_image_text([region])

    if expected is None:
        assert client.calls == []
        assert region.recognizedText is None
        assert region.recognizedElements is None
    else:
        assert len(client.calls) == 1
        image_block = _sent_image(client.calls[0])
        assert image_block["format"] == expected
        assert image_block["source"]["bytes"] == image_bytes


# --- Retained coverage: one request per eligible region ---------------------


@st.composite
def _regions_with_eligibility(draw):
    """Draw a list of `(region, eligible)` where an eligible region carries a
    non-empty, supported, within-limit byte string and an ineligible one
    carries absent or empty bytes."""
    kinds = draw(st.lists(st.sampled_from(["eligible", "none", "empty"]), min_size=1, max_size=6))
    pairs = []
    for index, kind in enumerate(kinds):
        if kind == "eligible":
            pairs.append((_region(f"loc[{index}]", image_bytes=_png_bytes(bytes([index % 256]))), True))
        elif kind == "none":
            pairs.append((_region(f"loc[{index}]", image_bytes=None), False))
        else:
            pairs.append((_region(f"loc[{index}]", image_bytes=b""), False))
    return pairs


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_regions_with_eligibility())
def test_one_request_per_eligible_region(pairs):
    """Exactly one `converse` call is issued per region with present,
    non-empty, supported, within-limit bytes; zero for regions with
    absent/empty bytes; and no call ever carries more than one image (never
    batched).
    """
    regions = [region for region, _ in pairs]
    eligible_count = sum(1 for _, eligible in pairs if eligible)
    client = FakeConverseClient(default_response=_fake_json_response([("t", [0.1, 0.1, 0.5, 0.3])]))

    with ocr_configured(client):
        recognize_image_text(regions)

    assert len(client.calls) == eligible_count
    for call in client.calls:
        content = call["messages"][0]["content"]
        assert len(content) == 1
        assert "image" in content[0]


# --- Retained coverage: ineligible images skipped without side effects ------


@st.composite
def _regions_with_reason(draw):
    """Draw a list of `(region, eligible, reason)` mixing eligible regions with
    unsupported-format and oversized ones. Sizes straddle a (test-lowered) size
    limit on both sides."""
    kinds = draw(
        st.lists(st.sampled_from(["eligible", "unsupported", "oversized"]), min_size=1, max_size=6)
    )
    limit = draw(st.integers(min_value=16, max_value=64))
    triples = []
    for index, kind in enumerate(kinds):
        if kind == "eligible":
            size = draw(st.integers(min_value=0, max_value=limit - len(PNG_MAGIC)))
            triples.append((_region(f"loc[{index}]", image_bytes=PNG_MAGIC + b"\x00" * size), True, kind))
        elif kind == "unsupported":
            triples.append((_region(f"loc[{index}]", image_bytes=b"BM" + b"\x00" * 20), False, kind))
        else:
            over = draw(st.integers(min_value=limit + 1, max_value=limit + 64))
            triples.append((_region(f"loc[{index}]", image_bytes=PNG_MAGIC + b"\x00" * over), False, kind))
    return triples, limit


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(_regions_with_reason())
def test_ineligible_regions_skipped(caplog, sample):
    """A region whose bytes are unsupported or exceed the size limit gets no
    request, keeps its `recognizedText`/`recognizedElements` unchanged, and
    produces a warning — while the remaining eligible regions are still
    processed. (Eligible regions here carry magic-byte-only PNGs that do not
    decode, so their geometry is `None` but their text is assembled.)
    """
    triples, limit = sample
    regions = [region for region, _, _ in triples]
    eligible_count = sum(1 for _, eligible, _ in triples if eligible)
    ineligible_count = len(triples) - eligible_count
    client = FakeConverseClient(default_response=_fake_json_response([("text", [0.1, 0.1, 0.5, 0.3])]))

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent.tools.ocr"):
        with patch.object(ocr, "IMAGE_SIZE_LIMIT_BYTES", limit):
            with ocr_configured(client):
                recognize_image_text(regions)

    assert len(client.calls) == eligible_count

    for region, eligible, _ in triples:
        if eligible:
            assert region.recognizedText == "text"
        else:
            assert region.recognizedText is None
            assert region.recognizedElements is None

    skip_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "skipping OCR" in record.getMessage()
    ]
    assert len(skip_warnings) == ineligible_count


# --- Retained coverage: retry accounting (_converse_with_retry) -------------


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.integers(min_value=0, max_value=5), st.sampled_from(["throttle", "5xx"]))
def test_transient_failures_retried_up_to_three_attempts(k, error_kind):
    """With `k` leading transient failures then success, `converse` is called
    exactly `min(k + 1, MAX_RETRIES)` times, and `_converse_with_retry` returns
    the response iff `k < MAX_RETRIES`, else `None`.
    """
    error_factory = FakeThrottlingError if error_kind == "throttle" else Fake5xxError
    success = _fake_json_response([("recovered", [0.1, 0.1, 0.5, 0.3])])
    client = SequencedConverseClient(k, error_factory, success)

    with patch("agent.tools.ocr.time.sleep") as sleep:
        result = ocr._converse_with_retry(client, PINNED_MODEL_ID, "png", _png_bytes())

    assert client.calls == min(k + 1, ocr.MAX_RETRIES)
    if k < ocr.MAX_RETRIES:
        assert result is success
    else:
        assert result is None
    assert sleep.call_count <= ocr.MAX_RETRIES - 1


def test_non_retryable_error_makes_single_attempt():
    """A non-retryable error stops after one attempt and returns `None`.
    """
    client = SequencedConverseClient(5, FakeNonRetryableError, _fake_json_response([]))
    with patch("agent.tools.ocr.time.sleep") as sleep:
        result = ocr._converse_with_retry(client, PINNED_MODEL_ID, "png", _png_bytes())

    assert client.calls == 1
    assert result is None
    assert sleep.call_count == 0


# --- Retained coverage: never raises, never discards work already done ------


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(["valid", "malformed", "raise"]), min_size=1, max_size=6))
def test_never_raises_and_retains_recognized_text(behaviors):
    """For any per-region client behaviour, `recognize_image_text` returns a
    list, raises nothing, and every region whose request succeeded keeps its
    recognized text — a failing region costs only its own text, so earlier
    results survive.
    """
    regions = []
    behavior_by_bytes = {}
    for index, behavior in enumerate(behaviors):
        image_bytes = PNG_MAGIC + f"region-{index}".encode("ascii")
        regions.append(_region(f"loc[{index}]", image_bytes=image_bytes))
        behavior_by_bytes[image_bytes] = (behavior, f"text-{index}")

    client = BehaviorConverseClient(behavior_by_bytes)
    with ocr_configured(client):
        result = recognize_image_text(regions)

    assert isinstance(result, list)
    assert result is regions
    for index, behavior in enumerate(behaviors):
        region = regions[index]
        if behavior == "valid":
            # Single-line JSON array -> recognizedText is exactly the payload.
            assert region.recognizedText == f"text-{index}"
        elif behavior == "malformed":
            # A malformed response yields no parseable array -> "".
            assert region.recognizedText == ""
        else:
            # A raising request leaves the placeholder untouched.
            assert region.recognizedText is None


def test_outer_handler_retains_results_when_a_later_region_raises():
    """When processing a LATER region raises an unexpected error, the tool's
    outer per-region handler keeps the text already written on earlier regions
    rather than discarding it, and never raises.
    """
    first = _region("loc[0]", image_bytes=_png_bytes(b"a"))
    second = _region("loc[1]", image_bytes=_png_bytes(b"b"))

    def fake_recognize(client, model_id, region):
        if region.sourceLocation == "loc[0]":
            region.recognizedText = "early"
            return
        raise RuntimeError("unexpected boom on a later region")

    client = FakeConverseClient(default_response=_fake_json_response([("unused", [0.1, 0.1, 0.5, 0.3])]))
    with patch.object(ocr, "_recognize_and_localize_one_region", side_effect=fake_recognize):
        with ocr_configured(client):
            result = recognize_image_text([first, second])

    assert result == [first, second]
    assert first.recognizedText == "early"
    assert second.recognizedText is None


# --- Retained coverage: lazy client, imports, distinct env var --------------


def test_lazy_client_constructed_once_across_two_invocations():
    """`boto3.client("bedrock-runtime")` is constructed once and reused across
    two `recognize_image_text` invocations (the shared lazy client).
    """
    fake = FakeConverseClient(default_response=_fake_json_response([("t", [0.1, 0.1, 0.5, 0.3])]))

    previous_client = ocr._bedrock_runtime_client
    previous_env = os.environ.get(ocr.IMAGE_OCR_MODEL_ID_ENV_VAR)
    ocr._bedrock_runtime_client = None
    os.environ[ocr.IMAGE_OCR_MODEL_ID_ENV_VAR] = PINNED_MODEL_ID
    try:
        with patch("boto3.client", return_value=fake) as mock_client:
            recognize_image_text([_region(image_bytes=_png_bytes())])
            recognize_image_text([_region(image_bytes=_png_bytes())])
            assert mock_client.call_count == 1
            mock_client.assert_called_with("bedrock-runtime")
    finally:
        ocr._bedrock_runtime_client = previous_client
        if previous_env is None:
            os.environ.pop(ocr.IMAGE_OCR_MODEL_ID_ENV_VAR, None)
        else:
            os.environ[ocr.IMAGE_OCR_MODEL_ID_ENV_VAR] = previous_env


def test_module_imports_no_strands_and_boto3_is_lazy():
    """The module exposes a single public function, imports no Strands symbol,
    and imports `boto3` lazily (inside the client getter, never at module top),
    so it stays importable without boto3/strands/bedrock_agentcore.
    """
    assert ocr.__all__ == ["recognize_image_text"]

    source = Path(ocr.__file__).read_text(encoding="utf-8")
    for forbidden in ("strands", "bedrock_agentcore"):
        assert f"import {forbidden}" not in source, forbidden
        assert f"from {forbidden}" not in source, forbidden

    source_lines = source.splitlines()
    assert "import boto3" not in source_lines, "boto3 must not be imported at module top"
    assert any(line.strip() == "import boto3" and line != "import boto3" for line in source_lines), (
        "boto3 should be imported lazily (indented) inside the client getter"
    )

    # Defensive: importing the tool pulled in no agentic runtime.
    for forbidden in ("strands", "bedrock_agentcore"):
        assert forbidden not in sys.modules, forbidden


def test_client_construction_failure_yields_regions_unchanged():
    """A `bedrock-runtime` client-construction failure returns the regions
    unchanged and raises nothing.
    """
    region = _region(image_bytes=_png_bytes())
    previous_env = os.environ.get(ocr.IMAGE_OCR_MODEL_ID_ENV_VAR)
    previous_client = ocr._bedrock_runtime_client
    ocr._bedrock_runtime_client = None
    os.environ[ocr.IMAGE_OCR_MODEL_ID_ENV_VAR] = PINNED_MODEL_ID
    try:
        with patch.object(
            ocr,
            "_get_bedrock_runtime_client",
            side_effect=RuntimeError("no credentials configured"),
        ):
            result = recognize_image_text([region])
        assert result == [region]
        assert region.recognizedText is None
        assert region.recognizedElements is None
    finally:
        ocr._bedrock_runtime_client = previous_client
        if previous_env is None:
            os.environ.pop(ocr.IMAGE_OCR_MODEL_ID_ENV_VAR, None)
        else:
            os.environ[ocr.IMAGE_OCR_MODEL_ID_ENV_VAR] = previous_env


def test_image_ocr_env_var_distinct_from_llm_pii():
    """`IMAGE_OCR_MODEL_ID_ENV_VAR` is `"IMAGE_OCR_MODEL_ID"` and is distinct
    from the LLM PII tool's `LLM_PII_MODEL_ID`.
    """
    from agent.tools import llm_pii_detect

    assert ocr.IMAGE_OCR_MODEL_ID_ENV_VAR == "IMAGE_OCR_MODEL_ID"
    assert ocr.IMAGE_OCR_MODEL_ID_ENV_VAR != llm_pii_detect.MODEL_ID_ENV_VAR


def test_warnings_and_audit_reach_module_logger(caplog):
    """Both a skip/dormant warning and the audit record are emitted through the
    module-level `agent.tools.ocr` logger.
    """
    # Warning: dormant config emits a warning on the module logger.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent.tools.ocr"):
        with ocr_configured(FakeConverseClient(), model_id=None):
            recognize_image_text([_region(image_bytes=_real_png())])
    dormant_warnings = [
        r for r in caplog.records if r.name == "agent.tools.ocr" and r.levelno == logging.WARNING
    ]
    assert dormant_warnings, [r.getMessage() for r in caplog.records]

    # Audit: a completed request emits an INFO audit record on the module logger.
    caplog.clear()
    client = FakeConverseClient(default_response=_fake_json_response([("t", [0.1, 0.1, 0.5, 0.3])]))
    with caplog.at_level(logging.INFO, logger="agent.tools.ocr"):
        with ocr_configured(client):
            recognize_image_text([_region(image_bytes=_real_png())])
    audit_records = [
        r
        for r in caplog.records
        if r.name == "agent.tools.ocr" and "LLM image OCR audit" in r.getMessage()
    ]
    assert audit_records, [r.getMessage() for r in caplog.records]
