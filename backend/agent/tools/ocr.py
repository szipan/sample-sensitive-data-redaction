"""`recognize_image_text` Agent tool.

Performs Image Text Recognition (OCR) on the `ImageRegion[]` produced (as
placeholders, with `recognizedText` unset) by `extract_document_content`,
filling each region's `recognizedText`. Its public signature and contract are
unchanged — `list[ImageRegion] -> list[ImageRegion]`, same instances, same
order — so nothing downstream (`match_rules`, `detect_pii_with_guardrails`,
`detect_pii_with_llm`, the redactor) changes.

--- What changed, and why ---------------------------------------------------

The embedded-image path's OCR engine is being moved off Amazon Textract onto a
multimodal Amazon Bedrock foundation model, addressed through the
`bedrock-runtime` `Converse` API's image content block. Textract's synchronous
`DetectDocumentText` is scoped to the Standard English alphabet and ASCII
symbols; against this repository's Chinese fixtures every CJK run came back as
Latin garbage, so PII living only inside a Chinese embedded image was never
redacted — a silent leak. A multimodal model reads the image bytes directly
and returns the recognized characters as text, with strong CJK support.

The model is selected by a pinned, versioned `IMAGE_OCR_MODEL_ID` read from a
new environment variable, exactly as `agent/tools/llm_pii_detect.py` selects
its own model — so the engine can be retargeted (Haiku -> Sonnet -> a
Nova-family model) by configuration alone, with no code change.

--- What this module contains ----------------------------------------------

    - The module constants, the OCR prompt template, the pinned-model-id
      predicate (`_is_pinned_model_id`), the model-config reader
      (`_read_model_config`), and the lazy `bedrock-runtime` client getter
      (`_get_bedrock_runtime_client`).
    - `_detect_image_format` (magic-byte format detection).
    - The Converse-with-retry helper (`_converse_with_retry`), the defensive
      response read (`_assistant_text`), the per-region processor
      (`_recognize_one_region`) with its size guard and audit record, and the
      full per-region loop inside `recognize_image_text`.

The module is deliberately **structurally parallel** to
`guardrails_pii_detect.py` / `llm_pii_detect.py`: the same lazy module-global
client with `import boto3` *inside* the getter, the same
pinned/versioned-model-id predicate shape, the same environment-driven off
switch, and the same never-raise / partial-results posture. It is meant to be
reviewed side-by-side with `llm_pii_detect.py`.

Importability guarantee: `boto3` is imported lazily inside
`_get_bedrock_runtime_client`, never at module top, so this module imports
with no `boto3`, `strands`, or `bedrock_agentcore` installed and stays
unit-testable with a plain fake client. The module imports no Strands symbol.

Logging: this module uses a module-level `logging.getLogger(__name__)`
(`agent.tools.ocr`). The agent-log-delivery Logging_Configurator delivers
`agent.*` records to CloudWatch, so this logger's output is real.

Requirements covered by this task (2.1):
    - 1.5, 1.6, 1.7: `OCR_PROMPT` is a module-level constant carrying
      `OCR_PROMPT_VERSION`; it instructs recognition of CJK as well as
      Latin-script text, transcription of only the visible characters in
      reading order, and forbids translation/transliteration/commentary.
    - 2.5: the lazy `_get_bedrock_runtime_client` shares one client across
      invocations within an Agent Runtime process.
    - 2.6: the module imports no Strands symbol and stays importable without
      `boto3`/`strands`/`bedrock_agentcore` (boto3 is imported inside the
      getter).
    - 4.3, 5.3, 6.1, 6.2: `_read_model_config` / `_is_pinned_model_id` gate
      detection on a pinned, versioned Model_Id supplied by the environment,
      independent of the model family.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import List, Optional, Tuple

from agent.logging_config import describe_exception
from agent.tools.guardrails_pii_detect import _is_retryable
from shared.models import BoundingBox, ImageRegion, RecognizedElement

logger = logging.getLogger(__name__)

__all__ = ["recognize_image_text"]

# --- Configuration constants ------------------------------------------------

#: The environment variable `CDK_Stack` sets alongside `GUARDRAIL_ID` /
#: `GUARDRAIL_VERSION` / `PDF_OCR_DPI` / `LLM_PII_MODEL_ID`, carrying the
#: pinned image-OCR Model_Id. Deliberately DISTINCT from `LLM_PII_MODEL_ID`
#: so the image-OCR model and the LLM text-detection model
#: are configured independently.
IMAGE_OCR_MODEL_ID_ENV_VAR = "IMAGE_OCR_MODEL_ID"

#: Identifies one exact revision of `OCR_PROMPT`. Bump this
#: string whenever the prompt text changes, so an audit record pins the prompt
#: that produced a given recognized-text-plus-geometry result. Bumped for the
#: in-image-redaction feature: the prompt now asks for per-line boxes, not just
#: text.
OCR_PROMPT_VERSION = "in-image-redaction/v1"

#: Total attempts per Converse call (1 initial + 2 retries).
MAX_RETRIES = 3

#: Backoff before retry attempt *n* is ``RETRY_BASE_DELAY_SECONDS * 2 ** n``,
#: i.e. 1s, 2s, 4s..., matching `llm_pii_detect.RETRY_BASE_DELAY_SECONDS`.
RETRY_BASE_DELAY_SECONDS = 1.0

#: `maxTokens` for the Converse `inferenceConfig`. A transcription of one
#: embedded image's visible text fits comfortably in 4096 tokens.
MAX_OUTPUT_TOKENS = 4096

#: Maximum encoded image byte size the Bedrock `Converse` image content block
#: accepts for a single image (3.75 MB). A region whose
#: `imageBytes` exceeds this is skipped with a warning rather than sent to be
#: rejected; the per-region size guard enforces this.
#: See the Bedrock Runtime `Message` reference: "Each image's size, height,
#: and width must be no more than 3.75 MB, 8000 px, and 8000 px".
IMAGE_SIZE_LIMIT_BYTES = int(3.75 * 1024 * 1024)  # 3,932,160

#: Separator inserted between consecutive line entries when assembling an
#: Image_Region's `recognizedText` from the localization response. A newline
#: belongs to NO recognized element, mirroring `ocr_pdf._build_page_unit`'s
#: between-line separator, so an offset landing on it resolves to zero
#: rectangles while every element's own offset range slices back to exactly
#: its text.
_LINE_SEPARATOR = "\n"

# --- Prompt -----------------------------------------------------------------

#: The module-level instruction text sent as the Converse `system` block with
#: every OCR_Request. Carries `OCR_PROMPT_VERSION` so a
#: run is traceable to an exact prompt revision.
#:
#: The prompt now asks for text AND geometry in a single call (resolved
#: decision A1): the model returns a JSON array of per-LINE entries, each an
#: object `{"text": ..., "box": [x0, y0, x1, y1]}`. Line granularity is
#: deliberate — a line box keeps a whole PII value (an 18-digit id, a name)
#: contiguous within one element's offset range and covers it fully, mirroring
#: `ocr_pdf.py`'s word-granular stance (over-redaction is the safe direction).
#:
#: The "no translation / no transliteration / no commentary" instruction is
#: load-bearing: the recognized text is fed verbatim to the PII detectors, so
#: a model that helpfully translates `身份证号` to "ID number" would defeat the
#: entire point. Stating it in the prompt is the only control available, since
#: the tool cannot detect a translation after the fact. The "JSON only, no
#: markdown, no fences" instruction is likewise load-bearing, but the parser
#: (`_parse_localization_response`) is defensive about stray prose or fences
#: anyway.
OCR_PROMPT = f"""\
You are an optical character recognition (OCR) engine, prompt version \
{OCR_PROMPT_VERSION}. You are given a single image extracted from a document. \
Your job is to transcribe the text visibly present in the image and report \
where each line of text sits on the image.

The text in the image is often Chinese, and may be Japanese or Korean, and it \
may mix in Latin-script (English) text. Recognize Chinese, Japanese, and \
Korean characters just as carefully as Latin-script text — do not skip CJK \
characters and do not approximate them with Latin letters.

OUTPUT CONTRACT — read carefully:
- Output ONLY a JSON array and nothing else. Each element of the array MUST be \
an object of the form {{"text": "<characters on one line>", "box": [x0, y0, \
x1, y1]}}, where "text" is exactly the characters visible on that one line, in \
natural reading order, and "box" is the tight rectangle around that line's \
glyphs given as [left, top, right, bottom].
- Produce one array element per visible line of text, in natural reading order \
(top to bottom).
- Transcribe CJK characters exactly. Do NOT translate the text. Do NOT \
transliterate it or convert it to pinyin or romaji. Do NOT summarize it. Do \
NOT add any commentary, explanation, labels, or description of the image.
- Do NOT wrap the output in markdown, code fences, or quotation marks around \
the array. Emit the raw JSON array and nothing else.
- If the image contains no legible text, return an empty JSON array: [].
"""

# --- Model-id validation and configuration ----------------------------------
#
# Mirrors `llm_pii_detect._is_pinned_model_id` / `_read_model_config` exactly,
# reading a DIFFERENT environment variable (`IMAGE_OCR_MODEL_ID`, not
# `LLM_PII_MODEL_ID`). The predicate is family-independent:
# it validates the *shape* of the id, not the vendor, so
# Haiku, Sonnet, and Nova ids all pass.

#: A Model_Id is pinned when its final segment matches `-v<major>:<minor>`.
#: Anchored at end-of-string so the version segment must be the last thing in
#: the id. Covers `...-v1:0`, `...-v2:1`, and the `amazon.nova-lite-v1:0`
#: shape; rejects an id with no version segment and anything ending in a bare
#: alias like `latest`.
_PINNED_MODEL_ID_SUFFIX = re.compile(r"-v\d+:\d+$")


def _is_pinned_model_id(model_id: Optional[str]) -> bool:
    """True when `model_id` is a pinned, versioned Bedrock model or inference
    profile identifier.

    Accepts an id whose final segment matches `-v\\d+:\\d+`, which covers:
        - `anthropic.claude-haiku-4-5-20251001-v1:0`
        - `us.anthropic.claude-haiku-4-5-20251001-v1:0` (the geo profile)
        - `amazon.nova-lite-v1:0`

    Rejects a blank/`None` value, an id carrying no version segment (e.g.
    `anthropic.claude-haiku-4-5`), and anything ending in `latest`. An
    unversioned or alias id is exactly the floating alias this rules
    out: the bytes it resolves to can change under a deployed compliance
    pipeline without any change to the repository.

    Family-independent: it validates the id's shape, not the
    vendor, so retargeting across model families requires no code change.
    """
    if not model_id:
        return False
    return _PINNED_MODEL_ID_SUFFIX.search(model_id) is not None


def _read_model_config() -> Optional[str]:
    """Return the pinned Image_OCR_Model_Id from the environment, or `None`
    (having logged a warning) when it is absent, blank, or not pinned.

    This is the analogue of `llm_pii_detect._read_model_config`. Returning
    `None` makes the tool dormant: `recognize_image_text` issues no request and
    returns the regions unchanged, leaving the regex, Guardrails, LLM text, and
    (for PDFs) full-page Textract sources to complete the job. Clearing
    `IMAGE_OCR_MODEL_ID` is the documented off switch.
    """
    model_id = (os.environ.get(IMAGE_OCR_MODEL_ID_ENV_VAR) or "").strip()

    if not model_id:
        logger.warning(
            "%s not set; skipping embedded-image LLM OCR",
            IMAGE_OCR_MODEL_ID_ENV_VAR,
        )
        return None

    if not _is_pinned_model_id(model_id):
        logger.warning(
            "%s is not a pinned, versioned model id (must end in -v<N>:<N>, "
            "not a floating alias); skipping embedded-image LLM OCR",
            IMAGE_OCR_MODEL_ID_ENV_VAR,
        )
        return None

    return model_id


# --- Lazy Bedrock client ----------------------------------------------------
#
# Lazily-initialized so a single client is reused across invocations within
# the same Agent Runtime process, mirroring
# `llm_pii_detect._get_bedrock_runtime_client` exactly. `boto3` is imported
# INSIDE the getter (never at module top) so the module imports with no
# `boto3`/`strands`/`bedrock_agentcore` installed.
_bedrock_runtime_client = None

#: Max concurrent image-OCR Converse calls (bounded to limit Bedrock throttling).
OCR_MAX_WORKERS = int(os.environ.get("OCR_MAX_WORKERS", "4"))


def _get_bedrock_runtime_client():
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        import boto3

        _bedrock_runtime_client = boto3.client("bedrock-runtime")
    return _bedrock_runtime_client


# --- Image-format detection -------------------------------------------------
#
# The Converse `ImageBlock` requires a `format` field declaring the encoding
# (valid values `png | jpeg | gif | webp`). `ImageRegion` carries only
# `imageBytes` — there is no caller-supplied format hint — so the format is
# detected from the bytes' leading magic number. Declaring a format that does
# not match the bytes (e.g. `png` for JPEG bytes) is a `ValidationException` at
# invocation time, so reading the format from the actual bytes is what keeps
# the declared `format` honest.


def _detect_image_format(image_bytes: bytes) -> Optional[str]:
    """Return the Converse image `format` (`"png"`/`"jpeg"`/`"gif"`/`"webp"`)
    matching `image_bytes`' leading magic number, or `None` when the bytes
    match none of the four supported formats or are too short to classify.

    Magic numbers checked (see the `Converse` `ImageBlock` reference):

        - PNG:  ``89 50 4E 47 0D 0A 1A 0A``
        - JPEG: ``FF D8 FF``
        - GIF:  ``47 49 46 38`` (``GIF8``)
        - WEBP: ``52 49 46 46`` (``RIFF``) at offset 0 **and**
                ``57 45 42 50`` (``WEBP``) at offset 8

    A `RIFF`-led container that is not WEBP (e.g. a WAV/AVI, or a truncated
    WEBP header shorter than 12 bytes), a BMP (`42 4D`), a TIFF, and any byte
    string shorter than the shortest magic number all return `None`, so the
    caller skips that region rather than sending bytes the model would reject.
    The checks are ordered longest-discriminating-prefix
    first; each guards its own length, so no slice reads past the end.
    """
    if not image_bytes:
        return None

    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "gif"
    if (
        len(image_bytes) >= 12
        and image_bytes[0:4] == b"RIFF"
        and image_bytes[8:12] == b"WEBP"
    ):
        return "webp"

    return None


# --- Converse call with retry -----------------------------------------------
#
# Structurally parallel to `llm_pii_detect._converse_with_retry`: the same
# bounded retry loop, the same never-raise posture, the same
# `describe_exception`-not-`str(error)` log hygiene. The retry *decision*
# (`_is_retryable`) is imported from `guardrails_pii_detect` rather than
# reimplemented, so all three Bedrock/Textract callers agree on what
# "transient" means and none drifts.


def _converse_with_retry(client, model_id, image_format, image_bytes) -> Optional[dict]:
    """Call the Bedrock `Converse` API for a single image region, retrying
    transient failures, and return the raw response dict — or `None`.

    The user content block is an **image**, not text: `image_bytes` is passed
    RAW into `source.bytes` (the AWS SDK base64-encodes it, so this tool does
    not — see the Converse inference guide), and `image_format` is the
    magic-byte-derived `Converse` format from `_detect_image_format`. The
    `inferenceConfig` sends ONLY ``temperature=0.0`` — deliberately not
    ``topP`` — because Claude Haiku 4.5 (and models like it) reject a Converse
    request that specifies both ``temperature`` and ``topP`` with a
    `ValidationException`. ``temperature=0.0`` alone is the mandated
    deterministic setting; omitting ``topP`` is fine because the
    nucleus-sampling pin only applies where the model accepts it. `maxTokens` is
    `MAX_OUTPUT_TOKENS`; the OCR prompt is sent as the `system` block.

    Retries throttling and 5xx service errors up to `MAX_RETRIES` **total**
    attempts (1 initial + 2 retries) with exponential backoff
    (``RETRY_BASE_DELAY_SECONDS * 2 ** attempt`` — 1s, 2s, ...). Returns `None`
    — never raises — after the attempts are
    exhausted or on the first non-retryable error (e.g. `ValidationException`,
    `AccessDeniedException`), having logged a warning. The
    caller (`recognize_image_text`) treats `None` as "no recognized text for
    this region": the region's `recognizedText` is left unchanged and
    processing continues, so a Converse failure degrades to one un-OCR'd
    region rather than failing the Redaction Job.

    The warning logs `describe_exception(error)`, never `str(error)` and never
    `exc_info=True` on this path: this Converse request body carries the image
    bytes, which may contain PII, and a `botocore` `ParamValidationError`
    echoes the offending parameter — i.e. the whole image — through
    `str(exc)`. `describe_exception` keeps the diagnostically load-bearing
    part (a `ThrottlingException` still reads as one) and discards the free
    text around it. `image_bytes` itself is never logged.
    """
    for attempt in range(MAX_RETRIES):
        try:
            response = client.converse(
                modelId=model_id,
                system=[{"text": OCR_PROMPT}],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "image": {
                                    "format": image_format,
                                    "source": {"bytes": image_bytes},
                                }
                            }
                        ],
                    }
                ],
                inferenceConfig={
                    "temperature": 0.0,
                    "maxTokens": MAX_OUTPUT_TOKENS,
                },
            )
        except Exception as error:  # noqa: BLE001 - never-raise, see docstring
            is_last_attempt = attempt == MAX_RETRIES - 1
            if not _is_retryable(error) or is_last_attempt:
                # `describe_exception`, not `error`/`str(error)`, and no
                # `exc_info=True`: this request body *is* the image bytes, so a
                # `ParamValidationError` would echo them back through
                # `str(exc)`. See module docstring and `logging_config`.
                logger.warning(
                    "Converse call failed on attempt %d/%d; skipping this image region: %s",
                    attempt + 1,
                    MAX_RETRIES,
                    describe_exception(error),
                )
                return None
            time.sleep(RETRY_BASE_DELAY_SECONDS * (2 ** attempt))
            continue

        return response

    return None


# --- Response read ----------------------------------------------------------
#
# The raw assistant text is read with the same defensive walk
# `llm_pii_detect._assistant_text` uses. Unlike the shipped `llm-image-ocr`
# version, that text is now a JSON array (`[{"text": ..., "box": [...]}, ...]`)
# rather than the recognized characters directly, so it is handed to
# `_parse_localization_response` before use.


def _assistant_text(response) -> str:
    """Concatenate the `text` of every content block in a Bedrock `Converse`
    response's `output.message.content`.

    Defensive at every level: `response`, `output`, `message`, `content`, and
    each block may be missing or the wrong type. Any deviation from the
    expected shape yields `""` rather than raising, so a malformed response is
    handled the same as an empty one.
    """
    if not isinstance(response, dict):
        return ""
    output = response.get("output")
    if not isinstance(output, dict):
        return ""
    message = output.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


# --- Localization-response parsing ------------------------------------------
#
# The assistant text is a JSON array of per-line `{"text": ..., "box": [...]}`
# objects (see `OCR_PROMPT`). This parser turns that text into `(text, raw_box)`
# pairs, tolerating the two most common ways a model breaks the contract — stray
# prose or markdown fences around the array — and dropping any entry that is not
# the expected shape. It NEVER raises: an unparseable response is a localization
# miss, handled by the caller exactly like an empty one (no `image:` spans, and
# — because `recognizedElements` stays `None` — the redactor whole-images any
# region that does carry spans). Coordinate validity is NOT judged here; that is
# `_normalize_box`'s job. This function only enforces the JSON *shape*.


def _parse_localization_response(
    assistant_text: str,
) -> Optional[List[Tuple[str, list]]]:
    """Extract the `(text, raw_box)` pairs from a localization response.

    Locates the first `[` and last `]` in `assistant_text` (so leading prose or
    a ```` ```json ```` fence around the array is tolerated), `json.loads`es
    that slice, and returns the list of `(text, raw_box)` pairs — skipping any
    element that is not shaped `{"text": <str>, "box": [<4 items>]}`.

    Returns `None` when the text contains no bracketed region, the slice is not
    valid JSON, or the decoded value is not a list. Returns a (possibly empty)
    list otherwise; the caller treats `None` and an empty list identically. The
    four box items are returned verbatim and un-validated — `_normalize_box`
    decides whether they are usable coordinates.

    This function never raises.
    """
    if not isinstance(assistant_text, str):
        return None

    start = assistant_text.find("[")
    end = assistant_text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None

    try:
        decoded = json.loads(assistant_text[start : end + 1])
    except (ValueError, TypeError):
        return None

    if not isinstance(decoded, list):
        return None

    pairs: List[Tuple[str, list]] = []
    for element in decoded:
        if not isinstance(element, dict):
            continue
        text = element.get("text")
        box = element.get("box")
        if not isinstance(text, str):
            continue
        if not isinstance(box, list) or len(box) != 4:
            continue
        pairs.append((text, box))
    return pairs


# --- Coordinate normalization -----------------------------------------------
#
# The Image_Coordinate_Normalizer (design.md "Component 1"). A feasibility
# experiment found Sonnet returns PIXEL coordinates even when asked for
# fractions, so the normalizer must robustly detect pixel-vs-fractional and
# normalize against the image's ACTUAL pixel dimensions (derived from the bytes,
# never from any model-reported dimension). It is a pure,
# total function: any implausible input yields `None` (which drives the
# Whole_Image_Fallback for the region), and it never raises.


def _image_pixel_dimensions(image_bytes: bytes) -> Tuple[int, int]:
    """Return `(width_px, height_px)` of the encoded `image_bytes`.

    Pillow is imported INSIDE this function (never at module top) so the module
    stays importable — and the pure `_normalize_box` stays unit-testable with
    explicit dimensions — without Pillow installed.
    """
    from PIL import Image  # lazy import: keeps the module Pillow-free to import

    with Image.open(BytesIO(image_bytes)) as image:
        return image.size


def _normalize_box(
    raw_box, image_width_px: int, image_height_px: int
) -> Optional[BoundingBox]:
    """Normalize a model-returned box to a `[0, 1]`-fraction `BoundingBox`, or
    return `None` when it is implausible.

    Algorithm:
    - **Numeric guard.** Any of the four coordinates non-numeric (or `bool`,
      `NaN`, `inf`) → `None`.
    - **Pixel-vs-fractional detection (deterministic).**
      If ANY coordinate is `> 1.0` the box is in pixels and is divided by
      `image_width_px` (x) / `image_height_px` (y); otherwise it is treated as
      already-fractional `[0, 1]`.
    - **Inverted guard.** After scaling, `x1 <= x0` or `y1 <= y0` → `None`.
    - **Clamp** all edges into `[0, 1]`.
    - **Positive-area guard.** If clamping collapses the box to zero/negative
      area → `None`.

    Never raises; returns either a positive-area `BoundingBox` with edges inside
    `[0, 1]`, or `None`.
    """
    if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
        return None

    coords: List[float] = []
    for value in raw_box:
        # `bool` is a subclass of `int`; a boolean coordinate is not a real
        # number here and is rejected as implausible.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        # Reject NaN (`number != number`) and +/- infinity.
        if number != number or number in (float("inf"), float("-inf")):
            return None
        coords.append(number)

    x0, y0, x1, y1 = coords

    # Pixel-vs-fractional: any coordinate above 1.0 means the box is in pixels.
    if any(coordinate > 1.0 for coordinate in coords):
        if image_width_px <= 0 or image_height_px <= 0:
            return None
        x0 /= image_width_px
        x1 /= image_width_px
        y0 /= image_height_px
        y1 /= image_height_px

    # Inverted / degenerate corner order (checked on the raw corners after
    # scaling, per the design's algorithm).
    if x1 <= x0 or y1 <= y0:
        return None

    left = min(x0, x1)
    top = min(y0, y1)
    right = max(x0, x1)
    bottom = max(y0, y1)

    # Clamp to the image bounds.
    left = max(0.0, min(1.0, left))
    top = max(0.0, min(1.0, top))
    right = max(0.0, min(1.0, right))
    bottom = max(0.0, min(1.0, bottom))

    width = right - left
    height = bottom - top
    if width <= 0.0 or height <= 0.0:
        return None

    return BoundingBox(left=left, top=top, width=width, height=height)


# --- Per-region processing --------------------------------------------------


def _emit_ocr_audit(
    region: ImageRegion,
    model_id: str,
    image_bytes: bytes,
    recognized_elements: Optional[List[RecognizedElement]],
) -> None:
    """Emit one PII-safe audit record for a completed localization request.

    Records `sourceLocation`, the model id, `OCR_PROMPT_VERSION`, the image's
    SHA-256 digest and byte length, the number of rectangles that will be
    redacted for the image, and whether the Whole_Image_Fallback will apply.
    The rectangle count is the localization outcome at OCR time: the number of
    Recognized_Elements when geometry is reliable, or `0` with
    `wholeImageFallback=True` when `recognizedElements is None` (the redactor
    will cover the whole image if the region carries any `image:` span — the
    single full-image rectangle).

    It records NEITHER the image bytes, the recognized text, NOR any box
    coordinates or content: only a count and a boolean.
    """
    if recognized_elements is None:
        rectangle_count = 0
        whole_image_fallback = True
    else:
        rectangle_count = len(recognized_elements)
        whole_image_fallback = False

    logger.info(
        "LLM image OCR audit: sourceLocation=%s modelId=%s promptVersion=%s "
        "imageSha256=%s imageBytes=%d rectangles=%d wholeImageFallback=%s",
        region.sourceLocation,
        model_id,
        OCR_PROMPT_VERSION,
        hashlib.sha256(image_bytes).hexdigest(),
        len(image_bytes),
        rectangle_count,
        whole_image_fallback,
    )


def _recognize_and_localize_one_region(
    client, model_id: str, region: ImageRegion
) -> None:
    """Run OCR-with-geometry on a single eligible `region`, writing its
    `recognizedText` and `recognizedElements` in place, or skip it (leaving
    both unchanged) when it cannot be sent.

    Precondition (checked by the caller): `region.imageBytes` is present and
    non-empty. This helper then:

    - detects the image format from magic bytes; an unsupported/undeterminable
      format is skipped with a warning, both fields untouched;
    - applies the byte-size guard: a region larger than `IMAGE_SIZE_LIMIT_BYTES`
      is skipped with a warning, both fields untouched;
    - issues exactly one Converse request (never batched). A `None` response
      means the request did not complete (a warning was already logged): both
      fields are left unchanged and NO audit record is emitted;
    - on a completed request, parses the JSON localization response and:
        - on `None`/empty → `recognizedText = ""`, `recognizedElements = None`
          (untrustworthy — nothing to redact anyway), one audit record;
        - otherwise assembles `recognizedText` by joining the entry texts with
          `_LINE_SEPARATOR`, tracking each entry's half-open
          `[startOffset, endOffset)` so an `image:` span's offsets and a
          Recognized_Element's offsets index the same string (mirroring
          `ocr_pdf._build_page_unit`), and normalizes each entry's box against
          the image's ACTUAL pixel dimensions (from the bytes);
        - if EVERY entry produced a plausible box and there is >=1 element →
          `recognizedElements = [RecognizedElement...]` (reliable geometry);
        - if SOME entries produced an implausible box, those entries are
          dropped (their text stays in `recognizedText`) and the remaining
          plausible boxes are kept, so one bad line no longer discards the
          whole region's geometry (in-image-redaction bugfix); a span landing
          on a dropped line resolves to zero rectangles and the redactor
          whole-images the region for that span (safe);
        - only if NO entry produced a plausible box → `recognizedElements =
          None` (untrustworthy), while `recognizedText` stays set so
          downstream detection still produces spans and the redactor
          whole-images the region;
      then emits exactly one audit record.
    """
    image_bytes = region.imageBytes

    image_format = _detect_image_format(image_bytes)
    if image_format is None:
        logger.warning(
            "Image region %s has unsupported or undeterminable image format; "
            "skipping OCR (recognizedText/recognizedElements left unchanged)",
            region.sourceLocation,
        )
        return

    if len(image_bytes) > IMAGE_SIZE_LIMIT_BYTES:
        logger.warning(
            "Image region %s is %d bytes, exceeding the %d-byte Converse image "
            "limit; skipping OCR (recognizedText/recognizedElements left unchanged)",
            region.sourceLocation,
            len(image_bytes),
            IMAGE_SIZE_LIMIT_BYTES,
        )
        return

    response = _converse_with_retry(client, model_id, image_format, image_bytes)
    if response is None:
        # The request did not complete — a warning was already logged inside
        # `_converse_with_retry`. Leave both fields as their placeholders (this
        # is the "request failed" case, distinct from "completed with empty
        # text"), and emit NO audit record (audit is per completed request only).
        return

    entries = _parse_localization_response(_assistant_text(response))

    if not entries:
        # Unparseable or empty response: no trustworthy text or geometry. With
        # an empty `recognizedText` the downstream detectors produce no
        # `image:` spans, so there is nothing to redact; `recognizedElements`
        # is `None` regardless. One audit record still fires
        # for the completed request.
        region.recognizedText = ""
        region.recognizedElements = None
        _emit_ocr_audit(region, model_id, image_bytes, recognized_elements=None)
        return

    # Assemble `recognizedText` from the entry texts, joined with
    # `_LINE_SEPARATOR`, tracking each entry's half-open [start, end) offset
    # range into the assembled string (mirroring `ocr_pdf._build_page_unit`).
    text_parts: List[str] = []
    entry_offsets: List[Tuple[int, int]] = []
    cursor = 0
    for entry_index, (entry_text, _raw_box) in enumerate(entries):
        if entry_index > 0:
            text_parts.append(_LINE_SEPARATOR)
            cursor += len(_LINE_SEPARATOR)
        start = cursor
        text_parts.append(entry_text)
        cursor += len(entry_text)
        entry_offsets.append((start, cursor))

    region.recognizedText = "".join(text_parts)

    # Normalize every entry's box against the image's ACTUAL pixel dimensions,
    # derived from the bytes — never a model-reported dimension (Requirement
    # 4.2). If the image cannot be measured, no geometry can be trusted.
    try:
        image_width_px, image_height_px = _image_pixel_dimensions(image_bytes)
    except Exception:  # noqa: BLE001 - untrustworthy geometry, safe fallback
        logger.warning(
            "Could not read pixel dimensions of image region %s; "
            "treating geometry as untrustworthy (whole-image fallback)",
            region.sourceLocation,
            exc_info=True,
        )
        region.recognizedElements = None
        _emit_ocr_audit(region, model_id, image_bytes, recognized_elements=None)
        return

    recognized_elements: List[RecognizedElement] = []
    for (entry_text, raw_box), (start, end) in zip(entries, entry_offsets):
        box = _normalize_box(raw_box, image_width_px, image_height_px)
        if box is None:
            # Per-element degradation (in-image-redaction bugfix): drop ONLY
            # this line's geometry and keep every other line's good box, rather
            # than nulling the whole region. A single implausible box
            # (frequently on a non-PII line such as a title or the photo
            # placeholder, and non-deterministic across Converse calls) used to
            # collapse `recognizedElements` to None and black out the ENTIRE
            # image. Now the bad line simply carries no geometry: its text is
            # still in `recognizedText`, so if an `image:` PII span lands on it
            # the redactor finds zero rectangles for that span and whole-images
            # the region (safe), while spans on the good lines are redacted
            # with targeted boxes.
            continue
        recognized_elements.append(
            RecognizedElement(
                text=entry_text,
                startOffset=start,
                endOffset=end,
                boundingBox=box,
            )
        )

    if recognized_elements:
        # At least one line has trustworthy geometry -> targeted redaction is
        # possible for spans overlapping those lines.
        region.recognizedElements = recognized_elements
    else:
        # No usable geometry at all (every box implausible, or no elements):
        # untrustworthy. Leave `recognizedText` set (downstream detection still
        # runs; the redactor whole-images the region if it carries spans).
        region.recognizedElements = None

    _emit_ocr_audit(
        region,
        model_id,
        image_bytes,
        recognized_elements=region.recognizedElements,
    )


def recognize_image_text(image_regions: List[ImageRegion]) -> List[ImageRegion]:
    """Populate `recognizedText` AND `recognizedElements` on each eligible
    `ImageRegion` via a multimodal Bedrock model addressed through the
    `Converse` API.

    Returns the same `ImageRegion` instances in the same order (Requirement
    9.2), leaving `boundingBox`/`zone`/`sourceLocation`/`imageBytes` untouched
    and mutating only `recognizedText` and (additively) `recognizedElements`,
    and only on eligible regions. `recognizedText` remains a plain string so
    downstream detection is unchanged. An empty input is
    returned unchanged with zero requests, checked at the very top before the
    model id is read. When `IMAGE_OCR_MODEL_ID` is unset/blank/not pinned, the
    tool is dormant and returns the regions unchanged.
    When the `bedrock-runtime` client cannot be constructed, the regions are
    likewise returned unchanged.

    Each region whose `imageBytes` is present and non-empty gets exactly one
    Converse request (never batched); regions with absent/empty `imageBytes`,
    an unsupported/undeterminable format, or bytes over `IMAGE_SIZE_LIMIT_BYTES`
    are skipped without a request, their `recognizedText`/`recognizedElements`
    left as their placeholders. Each region's body is wrapped so an unexpected
    error skips that one region and the remaining regions still process; the
    function returns a `list[ImageRegion]` for every input and raises nothing.
    """
    # Empty input: return unchanged, issue zero requests, before the model id
    # is even read.
    if not image_regions:
        return image_regions

    model_id = _read_model_config()
    if model_id is None:
        # Dormant: no pinned model id configured (a warning was already logged
        # inside `_read_model_config`).
        return image_regions

    try:
        client = _get_bedrock_runtime_client()
    except Exception:  # noqa: BLE001 - e.g. no credentials/region configured
        logger.warning(
            "Could not create bedrock-runtime client; skipping embedded-image LLM OCR",
            exc_info=True,
        )
        return image_regions

    def _process(region: ImageRegion) -> None:
        try:
            _recognize_and_localize_one_region(client, model_id, region)
        except Exception:  # noqa: BLE001 - never-raise, partial results kept
            # An unexpected error on one region must not abort the collection.
            # This region keeps its placeholder `recognizedText`/
            # `recognizedElements`; the rest still process.
            logger.warning(
                "Unexpected error during OCR of image region %s; "
                "leaving its recognizedText/recognizedElements unchanged and continuing",
                region.sourceLocation,
                exc_info=True,
            )

    # Regions with absent/empty `imageBytes` are skipped without a request,
    # leaving `recognizedText`/`recognizedElements` as their placeholders.
    # Each region is independent and mutated only by its own worker, so the
    # (slow, ~10s each) multimodal calls run concurrently; the input order of
    # the returned list is unchanged.
    eligible = [region for region in image_regions if region.imageBytes]
    if len(eligible) <= 1:
        for region in eligible:
            _process(region)
    else:
        with ThreadPoolExecutor(max_workers=OCR_MAX_WORKERS) as pool:
            list(pool.map(_process, eligible))
    return image_regions
