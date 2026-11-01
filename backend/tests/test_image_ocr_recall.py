r"""End-to-end Chinese-PII embedded-image recall integration test for
`recognize_image_text` — the ONLY test that verifies
this feature's premise, that a multimodal Bedrock model recovers Chinese PII
from an embedded image that Amazon Textract's `DetectDocumentText` reads back
as Latin garbage.

The gap this closes
-------------------

Embedded-image text reaches the redactor through exactly one tool:
`recognize_image_text(list[ImageRegion]) -> list[ImageRegion]`, which fills
each region's `recognizedText`. The old engine — Textract's synchronous
`DetectDocumentText` — is scoped to the Standard English alphabet and ASCII
symbols; measured against this repository's Chinese fixtures every CJK run
came back as Latin garbage and zero Chinese characters survived. This feature
replaces that engine with a multimodal Amazon Bedrock foundation model
addressed through the `Converse` API's image content block, which reads the
image bytes directly and returns the recognized characters as text, with
strong CJK support.

What this test asserts, and what it does not
--------------------------------------------

Model output is not reproducible, so this test asserts a **recall floor**, not
an equality. Concretely, after one real `Converse` call through
`recognize_image_text` against an image carrying Chinese PII:

1. the returned region's `recognizedText` is a populated string (the tool ran
   and wrote something);
2. that text CONTAINS the mainland Chinese resident identity number
   `110101199003072316`;
3. that text CONTAINS the Chinese personal name `张伟` (CJK
   recognition).

Both value assertions are one-sided: a run may recognize MORE than this floor
(labels, punctuation, extra characters). Passing does not certify the model —
only that the embedded-image OCR path recovers Chinese PII end to end on an
image whose text Textract cannot read.

Running it deliberately
-----------------------

This test makes a REAL Amazon Bedrock `Converse` call, so it costs money,
needs credentials AND model access enabled for the account/Region, and (when
generating its own fixture) needs Pillow plus a CJK-capable font installed on
the machine. It is therefore guarded by an environment variable and skips
(never fails) when anything it needs is missing, so `python -m pytest tests`
stays offline and free.

There is no pytest configuration file in this repository, so an
environment-variable guard is used rather than a custom marker (mirroring
`test_llm_cjk_recall.py` and `test_patti_regression.py`). From `backend/`:

    export RUN_IMAGE_OCR_RECALL=1
    export IMAGE_OCR_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
    export AWS_DEFAULT_REGION=us-west-2
    .venv/bin/python -m pytest tests/test_image_ocr_recall.py -q -s

`IMAGE_OCR_MODEL_ID` is what `recognize_image_text` itself reads; if it is
unset this test defaults it to the pinned Haiku profile above so the tool is
not dormant. `CHI_IMAGE_PATH` overrides the fixture image with a real
document image on disk; `CHI_FONT_PATH` overrides the CJK font used when the
test generates its own PNG. The test prints every number it measured, so a
run doubles as an audit record.

Invoke it as its own pytest run, not as part of `pytest tests`: sibling
modules install moto's fake AWS credentials into `os.environ` at module scope
for the rest of the process, so in a whole-suite run the credential probe
below finds unusable credentials and skips.

Requirements: 1.1, 1.2, 1.5.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools import ocr  # noqa: E402
from agent.tools.ocr import recognize_image_text  # noqa: E402
from shared.models import ImageRegion  # noqa: E402

#: Set this to run the test. Absent -> skipped, so the default suite stays
#: offline and free.
RUN_FLAG_ENV_VAR = "RUN_IMAGE_OCR_RECALL"

#: The environment variable `recognize_image_text` itself reads to select its
#: model. Imported from the tool so the two never drift.
MODEL_ID_ENV_VAR = ocr.IMAGE_OCR_MODEL_ID_ENV_VAR  # "IMAGE_OCR_MODEL_ID"

#: Defaulted into the environment when unset, so the tool is not dormant. The
#: same pinned Haiku inference profile the CDK stack provisions.
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

#: Model access for the default profile is granted in this Region; a Converse
#: call elsewhere would fail with AccessDenied, which the tool swallows into a
#: silent skip — so a wrong Region is a skip here, not a misleading failure.
REQUIRED_REGION = "us-west-2"

#: Optional override: a real document image on disk. When set and readable it
#: replaces the generated PNG (and Pillow/font are not needed).
IMAGE_PATH_ENV_VAR = "CHI_IMAGE_PATH"

#: Optional override: the CJK font file used when generating the fixture PNG.
FONT_PATH_ENV_VAR = "CHI_FONT_PATH"

#: The recall floor. A mainland Chinese resident identity number and a Chinese
#: personal name — both values Textract's `DetectDocumentText` returns as Latin
#: garbage, so recovering them is the proof the multimodal engine reads CJK.
RESIDENT_ID = "110101199003072316"
CHINESE_NAME = "张伟"

#: Candidate CJK-capable font files, tried in order after `CHI_FONT_PATH`.
#: Covers this repo's Linux hosts (Noto CJK, Droid Sans Fallback), plus common
#: macOS/Windows locations, so the generator works on a range of machines.
_CJK_FONT_CANDIDATES = (
    "/home/ec2-user/.fonts/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-vf/NotoSansCJK-VF.ttc",
    "/usr/share/fonts/google-droid-sans-fonts/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
)


# ---------------------------------------------------------------------------
# Guards: everything missing is a skip, never a failure
# ---------------------------------------------------------------------------


def _resolve_region() -> Optional[str]:
    """The effective AWS Region boto3 would use, or None if none is set."""
    import boto3

    region = boto3.session.Session().region_name
    if region:
        return region
    for name in ("AWS_DEFAULT_REGION", "AWS_REGION"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _skip_unless_runnable() -> str:
    """Return the pinned model id to use, or skip with the reason it cannot
    run. Every missing prerequisite is a skip, never a failure, so the default
    offline suite stays green."""
    if not (os.environ.get(RUN_FLAG_ENV_VAR) or "").strip():
        pytest.skip(
            "{}, calls real Bedrock (Converse); set {}=1 to run "
            "(see this module's docstring)".format(__name__, RUN_FLAG_ENV_VAR)
        )

    # Default the model id when unset, so the tool is not dormant. If the
    # operator set a non-pinned/floating value, that is a misconfiguration:
    # the tool would go dormant and prove nothing, so skip rather than fail.
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
            "AWS region is {!r}, not {!r}; the default model's access is granted "
            "in {!r}, so a Converse call elsewhere would be a silent skip".format(
                region, REQUIRED_REGION, REQUIRED_REGION
            )
        )

    # Credentials *existing* is not enough: several sibling test modules set
    # moto's fake keys into `os.environ` at module scope for the rest of the
    # process, so in a whole-suite run this test would inherit credentials
    # that look present and are rejected by Bedrock. Probe them once, and skip
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


def _find_cjk_font() -> Optional[str]:
    """A path to a CJK-capable font file, or None if none can be found.

    Order: `CHI_FONT_PATH`, then a list of common absolute locations, then a
    best-effort `fc-match` query. Returning None is a skip signal for the
    caller, never a failure.
    """
    override = (os.environ.get(FONT_PATH_ENV_VAR) or "").strip()
    if override:
        return override if Path(override).is_file() else None

    for candidate in _CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    # Best-effort: ask fontconfig for a font that covers Chinese. Any failure
    # (fc-match absent, non-zero exit, empty output) just means "no font".
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":lang=zh"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = (result.stdout or "").strip()
    return path if path and Path(path).is_file() else None


def _generate_chinese_pii_png() -> bytes:
    """Render a white PNG carrying the resident id and Chinese name in a
    CJK-capable font, or skip when Pillow or a CJK font is unavailable.

    Pillow is deliberately NOT a hard test dependency (the tool needs only
    `boto3`), so its absence is a skip, resolved by supplying `CHI_IMAGE_PATH`
    instead.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        pytest.skip(
            "Pillow is not installed and {} was not supplied; set {} to a "
            "document image on disk to run without Pillow".format(
                IMAGE_PATH_ENV_VAR, IMAGE_PATH_ENV_VAR
            )
        )

    font_path = _find_cjk_font()
    if font_path is None:
        pytest.skip(
            "no CJK-capable font found and {} was not supplied; set {} or "
            "{} to run".format(IMAGE_PATH_ENV_VAR, FONT_PATH_ENV_VAR, IMAGE_PATH_ENV_VAR)
        )

    # Lines resembling a Chinese ID card: labels give the model natural
    # context, but the recall floor only checks the raw values below.
    lines = [
        "居民身份证",
        "姓名  {}".format(CHINESE_NAME),
        "公民身份号码",
        RESIDENT_ID,
    ]

    try:
        font = ImageFont.truetype(font_path, 48)
    except OSError:
        pytest.skip(
            "CJK font at {!r} could not be loaded by Pillow; set {} to a "
            "document image instead".format(font_path, IMAGE_PATH_ENV_VAR)
        )

    width, height, margin, line_height = 1000, 520, 60, 110
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((margin, margin + index * line_height), line, fill="black", font=font)

    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    png_bytes = buffer.getvalue()

    # Sanity: if this font silently rendered the CJK glyphs as tofu boxes the
    # test would be measuring the model against a blank, so at least confirm
    # the encoder produced a plausible image. (Glyph coverage itself cannot be
    # asserted here without OCR — the real model call is that check.)
    print(
        "[measured] generated fixture PNG: font={!r} bytes={}".format(
            font_path, len(png_bytes)
        )
    )
    return png_bytes


def _resolve_image_bytes() -> bytes:
    """The fixture image bytes: `CHI_IMAGE_PATH` when supplied, else a freshly
    generated PNG. Any missing prerequisite is a skip."""
    override = (os.environ.get(IMAGE_PATH_ENV_VAR) or "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            pytest.skip(
                "fixture image not found at {}; set {} to its location".format(
                    path, IMAGE_PATH_ENV_VAR
                )
            )
        return path.read_bytes()
    return _generate_chinese_pii_png()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_image_ocr_chinese_pii_recall_floor():
    """`recognize_image_text` recovers a Chinese resident identity number and a
    Chinese personal name from an embedded image — values Textract's
    `DetectDocumentText` returns as Latin garbage.

    Recall floor, not equality: the recognized text must CONTAIN both values;
    a run may recognize more.
    """
    model_id = _skip_unless_runnable()
    image_bytes = _resolve_image_bytes()

    region = ImageRegion(
        zone="image",
        sourceLocation="image:0",
        imageBytes=image_bytes,
    )

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
        "[measured] contains resident id {!r}: {}".format(
            RESIDENT_ID, isinstance(recognized, str) and RESIDENT_ID in recognized
        )
    )
    print(
        "[measured] contains name {!r}: {}".format(
            CHINESE_NAME, isinstance(recognized, str) and CHINESE_NAME in recognized
        )
    )

    # The tool leaves `recognizedText` as its placeholder (None) when it went
    # dormant, skipped the image, or the request did not complete. A populated
    # String is the first proof the Converse path ran.
    assert isinstance(recognized, str) and recognized, (
        "recognizedText was not populated ({!r}); the OCR request did not "
        "complete — check model access in {} and the fixture image".format(
            recognized, REQUIRED_REGION
        )
    )

    # The recall floor (one-sided): both CJK PII values must be present.
    assert RESIDENT_ID in recognized, (
        "the resident identity number {!r} was not recovered from the image; "
        "recognizedText={!r}".format(RESIDENT_ID, recognized)
    )
    assert CHINESE_NAME in recognized, (
        "the Chinese name {!r} was not recovered from the image (this is the "
        "CJK-recognition premise Textract fails); recognizedText={!r}".format(
            CHINESE_NAME, recognized
        )
    )
