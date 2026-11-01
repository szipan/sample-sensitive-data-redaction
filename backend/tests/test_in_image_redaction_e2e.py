r"""End-to-end, env-guarded integration test proving the in-image-redaction
feature closes the demonstrated leak — the ONE test that verifies the feature's
premise against a real Sonnet-class model.

What this test proves, and why it is the only test that can
-----------------------------------------------------------

Every other in-image-redaction test fakes Bedrock (`unittest.mock`) or drives
the pure helpers directly, because a multimodal model's output is not a
reproducible function of its input. Those tests pin the deterministic surfaces
(coordinate normalization, offset->box mapping, the fallback decision, the
audit record, the drawing logic). None of them can answer the only question
that matters for the feature's claim: *does a real Sonnet Converse call recover
the Chinese PII AND localize it well enough that the redactor blacks it out?*

This test answers exactly that, end to end, mirroring the shipped
`tests/test_image_ocr_recall.py`:

1. it runs `recognize_image_text([region])` — a REAL Sonnet `Converse` call —
   against an image carrying Chinese PII, and asserts the **recall floor**: the
   returned region's `recognizedText` contains the mainland Chinese resident
   identity number `110101199003072316` AND the Chinese name `张伟`
   (the detection premise, reused verbatim from the
   recall test's floor);
2. it then runs the **redactor** over that same region — building `image:`
   `MatchSpan`s over the detected PII offsets and calling
   `redaction_image.redact_image_bytes` directly (the recall test operates at
   the raw-image level, so this is the natural seam) — and asserts the
   correctness spine: the redacted output image either
   (a) has the PII-region pixels blacked out (targeted redaction — a coarse
   "is this area covered" pixel check at each localized rectangle's center), or
   (b) was covered by the Whole_Image_Fallback (`recognizedElements is None`,
   or a span that mapped to zero rectangles), in which case the entire image is
   black.

Assertion (2) is deliberately one-sided in the safe direction: a targeted run
must cover every detected span's rectangle, and any localization the feature
deems untrustworthy must cover the whole image. Both outcomes satisfy the
governing rule — detected in-image PII is never left visible.

Running it deliberately
-----------------------

This test makes a REAL Amazon Bedrock `Converse` call, so it costs money, needs
credentials AND Sonnet model access enabled for the account/Region, and (when
generating its own fixture) needs Pillow plus a CJK-capable font. It is
therefore guarded by the `RUN_IN_IMAGE_REDACTION` environment variable and
SKIPS (never fails) when anything it needs is missing, so `python -m pytest
tests` stays offline and free.

There is no pytest configuration file in this repository, so an
environment-variable guard is used rather than a custom marker (mirroring
`test_image_ocr_recall.py`, `test_llm_cjk_recall.py`, `test_patti_regression.py`).
From `backend/`::

    export RUN_IN_IMAGE_REDACTION=1
    export IMAGE_OCR_MODEL_ID="us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    export AWS_DEFAULT_REGION=us-west-2
    .venv/bin/python -m pytest tests/test_in_image_redaction_e2e.py -q -s

`IMAGE_OCR_MODEL_ID` is what `recognize_image_text` itself reads; if it is unset
this test defaults it to the pinned **Sonnet** profile above, because in-image
localization is only compliance-grade on a Sonnet-class model (Haiku boxes are
unusable — a settled finding of the feasibility experiment). `CHI_IMAGE_PATH`
overrides the fixture image with a real driving-license image on disk;
`CHI_FONT_PATH` overrides the CJK font used when the test generates its own PNG.

Invoke it as its own pytest run, not as part of `pytest tests`: sibling modules
install moto's fake AWS credentials into `os.environ` at module scope for the
rest of the process, so in a whole-suite run the credential probe below finds
unusable credentials and skips.

Requirements: 1.1, 1.2, 6.2.
"""
from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools import ocr  # noqa: E402
from agent.tools import redaction_image  # noqa: E402
from agent.tools.ocr import recognize_image_text  # noqa: E402
from shared.models import ImageRegion, MatchSpan  # noqa: E402

# Reuse the recall test's fixture generation, region/credential probing, and
# the CJK recall-floor constants rather than reinventing them — the two tests
# share the same fixture and the same "everything missing is a skip" posture.
from tests.test_image_ocr_recall import (  # noqa: E402
    CHINESE_NAME,
    MODEL_ID_ENV_VAR,
    REQUIRED_REGION,
    RESIDENT_ID,
    _resolve_image_bytes,
    _resolve_region,
)

#: Set this to run the test. Absent -> skipped, so the default suite stays
#: offline and free.
RUN_FLAG_ENV_VAR = "RUN_IN_IMAGE_REDACTION"

#: Defaulted into the environment when `IMAGE_OCR_MODEL_ID` is unset, so the
#: tool is not dormant. The same pinned Sonnet inference profile the CDK stack
#: provisions (design.md "Component 9"): in-image localization is only
#: compliance-grade on a Sonnet-class model, so this test defaults to Sonnet
#: rather than the recall test's Haiku default.
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

#: The Unit_Ref namespace an embedded-image span carries; index 0 matches the
#: single region built below. The redactor resolves `image:{index}` -> region;
#: `redact_image_bytes` itself takes the region and its spans directly.
UNIT_REF = "image:0"

#: Per-channel tolerance for "this pixel is black". PNG (the generated fixture)
#: fills exact `(0, 0, 0)`, but a real JPEG driving-license fixture supplied via
#: `CHI_IMAGE_PATH` re-encodes with lossy artifacts, so a small tolerance keeps
#: the coverage check honest without turning a covered pixel into a failure.
_BLACK_TOLERANCE = 48


# ---------------------------------------------------------------------------
# Guards: everything missing is a skip, never a failure
# ---------------------------------------------------------------------------


def _skip_unless_runnable() -> str:
    """Return the pinned Sonnet-class model id to use, or skip with the reason
    it cannot run. Every missing prerequisite is a skip, never a failure, so
    the default offline suite stays green.

    Mirrors `test_image_ocr_recall._skip_unless_runnable` (same guard shape,
    same credential/region probe) but keys off `RUN_IN_IMAGE_REDACTION` and
    defaults `IMAGE_OCR_MODEL_ID` to a Sonnet-class id, because in-image
    localization needs Sonnet geometry, not Haiku's.
    """
    if not (os.environ.get(RUN_FLAG_ENV_VAR) or "").strip():
        pytest.skip(
            "{}, calls real Bedrock (Converse) and costs a model invocation; "
            "set {}=1 to run (see this module's docstring)".format(
                __name__, RUN_FLAG_ENV_VAR
            )
        )

    # Default the model id when unset. If the operator set a non-pinned/floating
    # value, the tool would go dormant and prove nothing, so skip rather than
    # fail.
    model_id = (os.environ.get(MODEL_ID_ENV_VAR) or "").strip() or DEFAULT_MODEL_ID
    if not ocr._is_pinned_model_id(model_id):
        pytest.skip(
            "{}={!r} is not a pinned, versioned model id; the tool would be "
            "dormant".format(MODEL_ID_ENV_VAR, model_id)
        )
    os.environ[MODEL_ID_ENV_VAR] = model_id

    try:
        import boto3
    except ImportError:  # pragma: no cover - boto3 is a project dependency
        pytest.skip("boto3 is not installed")

    if boto3.session.Session().get_credentials() is None:
        pytest.skip("no AWS credentials available for Bedrock")

    region = _resolve_region()
    if region != REQUIRED_REGION:
        pytest.skip(
            "AWS region is {!r}, not {!r}; the Sonnet profile's access is "
            "granted in {!r}, so a Converse call elsewhere would be a silent "
            "skip".format(region, REQUIRED_REGION, REQUIRED_REGION)
        )

    # Credentials *existing* is not enough: several sibling test modules set
    # moto's fake keys into `os.environ` at module scope for the rest of the
    # process, so in a whole-suite run this test would inherit credentials that
    # look present and are rejected by Bedrock. Probe them once, and skip
    # rather than fail if they are not usable.
    try:
        boto3.client("sts").get_caller_identity()
    except Exception as error:  # noqa: BLE001 - any failure means "cannot run"
        pytest.skip(
            "AWS credentials are not usable ({}); if this was a whole-suite "
            "run, other modules replaced them with moto's fake keys - invoke "
            "this file on its own".format(type(error).__name__)
        )

    return model_id


def _pixel_is_black(image, x: int, y: int) -> bool:
    """True when the pixel at `(x, y)` of an RGB `image` is black within
    `_BLACK_TOLERANCE` on every channel."""
    px = image.getpixel((x, y))
    if isinstance(px, int):  # single-band (should not happen after convert)
        return px <= _BLACK_TOLERANCE
    return all(channel <= _BLACK_TOLERANCE for channel in px[:3])


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_in_image_redaction_recall_and_coverage():
    """Real Sonnet `Converse` recovers the Chinese resident id and name from an
    embedded image (recall floor) AND the redactor
    covers that PII in the output image — targeted rectangles over the detected
    spans, or the Whole_Image_Fallback — so the detected in-image PII is never
    left visible.
    """
    model_id = _skip_unless_runnable()
    image_bytes = _resolve_image_bytes()

    region = ImageRegion(
        zone="image",
        sourceLocation=UNIT_REF,
        imageBytes=image_bytes,
    )

    # --- Step 1: real Sonnet OCR-with-geometry, and the recall floor ---------
    returned = recognize_image_text([region])

    # The tool's contract: same instance, in a one-element list.
    assert returned and returned[0] is region, (
        "recognize_image_text must return the same ImageRegion instances"
    )

    recognized = region.recognizedText

    print("\n[measured] model id: {}".format(model_id))
    print("[measured] image bytes sent: {}".format(len(image_bytes)))
    print(
        "[measured] recognizedText type={} chars={}".format(
            type(recognized).__name__,
            len(recognized) if isinstance(recognized, str) else "n/a",
        )
    )
    print("[measured] recognizedText: {!r}".format(recognized))
    print(
        "[measured] recognizedElements: {}".format(
            None
            if region.recognizedElements is None
            else "{} element(s)".format(len(region.recognizedElements))
        )
    )

    # A populated string is the first proof the Converse path ran.
    assert isinstance(recognized, str) and recognized, (
        "recognizedText was not populated ({!r}); the OCR request did not "
        "complete — check Sonnet model access in {} and the fixture image".format(
            recognized, REQUIRED_REGION
        )
    )

    # The recall floor (one-sided): both CJK PII values must be present.
    assert RESIDENT_ID in recognized, (
        "the resident identity number {!r} was not recovered from the image; "
        "recognizedText={!r}".format(RESIDENT_ID, recognized)
    )
    assert CHINESE_NAME in recognized, (
        "the Chinese name {!r} was not recovered from the image; "
        "recognizedText={!r}".format(CHINESE_NAME, recognized)
    )

    # --- Step 2: build image: spans over the detected PII offsets ------------
    # The detected PII is exactly the resident id and the name; their offsets
    # into `recognizedText` are the `image:` span ranges the redactor acts on.
    spans = []
    for value in (RESIDENT_ID, CHINESE_NAME):
        start = recognized.find(value)
        assert start != -1  # guaranteed by the recall-floor asserts above
        spans.append(
            MatchSpan(
                zone="image",
                unitRef=UNIT_REF,
                startOffset=start,
                endOffset=start + len(value),
                ruleName="e2e-image-pii",
            )
        )

    # --- Step 3: run the redactor end to end ---------------------------------
    redacted_bytes = redaction_image.redact_image_bytes(image_bytes, region, spans)
    assert redacted_bytes, "redact_image_bytes returned no bytes"

    from PIL import Image  # a real dependency here; _resolve_image_bytes skipped if absent

    out = Image.open(BytesIO(redacted_bytes)).convert("RGB")
    W, H = out.size

    # Mirror `redact_image_bytes`'s own targeted-vs-fallback decision so the
    # pixel assertion checks the right thing: targeted iff reliable geometry AND
    # every span resolves to >= 1 rectangle; otherwise Whole_Image_Fallback.
    targeted = region.recognizedElements is not None
    span_rects = []
    if targeted:
        for span in spans:
            rects = redaction_image._rects_for_image_span(
                region.recognizedElements, span, W, H
            )
            if not rects:
                targeted = False
                break
            span_rects.extend(rects)

    print(
        "[measured] redaction mode: {}".format(
            "targeted ({} rect(s))".format(len(span_rects))
            if targeted
            else "whole_image_fallback"
        )
    )

    if targeted:
        # (a) Each detected span's PII region is covered: sample the center of
        # every localized rectangle and assert it is black (a coarse "is this
        # area covered" check — the localized box is padded by the redactor, so
        # its center is comfortably inside the drawn rectangle).
        for left, top, right, bottom in span_rects:
            cx = min(max(int((left + right) / 2), 0), W - 1)
            cy = min(max(int((top + bottom) / 2), 0), H - 1)
            assert _pixel_is_black(out, cx, cy), (
                "PII-region pixel ({}, {}) is not black; the localized "
                "rectangle did not cover the detected in-image PII".format(cx, cy)
            )
    else:
        # (b) The Whole_Image_Fallback fired: the entire image is black. Sample
        # the four corners and the center — all must be black.
        for x, y in ((1, 1), (W - 2, 1), (1, H - 2), (W - 2, H - 2), (W // 2, H // 2)):
            assert _pixel_is_black(out, x, y), (
                "Whole_Image_Fallback was expected but pixel ({}, {}) is not "
                "black".format(x, y)
            )
