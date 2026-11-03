r"""`ocr_pdf_pages` Agent tool.

Produces the OCR_Path's text units for a `.pdf` document: every page is
rasterized with pypdfium2 and handed to Amazon Textract's synchronous
`DetectDocumentText`, yielding one `zone="ocr"` `ExtractedTextUnit` per page
that carries the recognized page text plus per-word geometry.

Why this exists
---------------

A PDF can carry a broken font ToUnicode CMap, so a page *renders*
`Patti、Martin、Christina` while its text layer *reads* `Pa#、Mar&n、Chris&na`.
A single-path pipeline would redact 1 of 3 names and still report the
Redaction Job `completed`. This module is the second, independent detection
path: a ToUnicode defect breaks the text layer but not OCR, an OCR misread
breaks OCR but not a clean text layer, and a scanned page has no text layer
at all. To leak a PII value, *both* paths must fail on that same value.

Relationship to `ocr.py`
------------------------

`ocr.py`'s `recognize_image_text` OCRs *embedded images* and is deliberately
not reused here: it reads only `"LINE"`-level `Text` and discards
`Geometry` entirely, because its consumer (`match_rules`) only needs the
recognized string. This path needs `"WORD"` geometry, since an OCR-derived
`MatchSpan` has no character rectangles to fall back on - the word list is
the only bridge from an offset in the unit's text back to a place on the
page. The lazy-client pattern and Textract call style are reused; the block
walk is not.

Interface decisions (they affect callers and tests)
---------------------------------------------------

1. **This module raises.** Unlike `guardrails_pii_detect.py`, which swallows
   every failure because it only *supplements* the regex rules, the OCR_Path
   is one of exactly two primary detection sources for a PDF - and the only
   one that works when the text layer is corrupt. An OCR failure that
   degraded silently would rebuild the exact defect this path exists to
   remove: PII missing from the output while the Job reads `completed`. So a
   failure on any page raises `OcrPathError` instead of skipping the page,
   and its message includes the failing page index. `handle_job` lets the
   error reach its top-level handler, driving the Job to `failed` with the
   page index in `errorMessage` and nothing stored. A loud failure is
   recoverable by re-uploading; a silent one is not detectable at all.

2. **One unit per page, always - even an empty one.** A page Textract
   recognized no words on still yields a unit with `text=""`. That keeps
   `ocr:{index}` identical to the page index, so nothing downstream needs a
   unit-index -> page-index map, and both detection tools already skip empty
   text (`detect_pii_with_guardrails` checks `if not unit.text`;
   `re.finditer` over `""` yields nothing). This deliberately differs from
   `extraction_pdf.py`, which *drops* whitespace-only pages - hence
   `text:{index}` is NOT a page index there and `position.pageIndex` must be
   read instead.

3. **The visible page size** (`page.get_size()`, which already accounts for
   the page's `/Rotate`) is the unit's `boundingBox`. It is exactly the
   area `page.render` rasterizes, so the normalized Textract coordinates
   recorded on each `OcrWord` are fractions *of this rectangle*. Using the
   unrotated media box here would silently mis-place every word on a
   `/Rotate 90` page.

4. **Normalized coordinates are stored raw.** `OcrWord.boundingBox` holds
   Textract's `[0, 1]` fractions verbatim, NOT page points - see the
   WARNING on `OcrWord` in `shared/models.py`. The redactor scales them onto
   the page size at redaction time, which keeps the page-size dependency in
   one place. Note the mapping is independent of the Raster_Resolution: the
   rasterized image always covers the whole visible page, so dpi affects
   recognition quality and payload size only, never placement.

5. **Word granularity is accepted.** Textract's smallest geometric unit is a
   word, so OCR-derived redaction is word-granular: matching `Patti` inside
   `Patti's` blacks out `Patti's`. Over-redaction is the safe direction, and
   the Text_Layer_Path still contributes character-exact rectangles whenever
   the text layer is sound.

6. **Word separators are accepted too.** OCR text carries a space between
   words and a newline between lines, so a PII value Textract splits across
   words is matched only if the pattern tolerates that separator
   (`+86 138 0000 0000` reads back with its spaces intact and matches a
   pattern written for it). This is the mirror image of the Text_Layer_Path,
   which inserts no separators at all and therefore *joins* line-wrapped
   values - a further reason to keep both paths.

7. **Synchronous `DetectDocumentText`**, not `StartDocumentTextDetection`:
   it keeps the control flow a plain loop, matches the existing sync pattern
   in `ocr.py`, and avoids introducing a polling state machine. The
   trade-off is page-serial latency for multi-page documents.

8. **Bounded Textract effort per page.** Each page gets at most
   `MAX_ATTEMPTS` Textract calls (one initial attempt plus retries on
   transient errors). A page image over Textract's 10 MB synchronous limit
   is re-rendered once at `FALLBACK_RASTER_DPI` (150 DPI) before the call.
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

import io

import pypdfium2 as pdfium

from agent.logging_config import describe_exception
from shared.models import BoundingBox, ExtractedTextUnit, OcrWord, TextPosition

logger = logging.getLogger(__name__)

__all__ = ["OcrPathError", "ocr_pdf_pages"]

#: Environment variable holding the Raster_Resolution, injected into the
#: AgentCore Runtime by `infra/redaction_stack.py` alongside `GUARDRAIL_ID` /
#: `GUARDRAIL_VERSION`.
RASTER_DPI_ENV_VAR = "PDF_OCR_DPI"

#: Used when `PDF_OCR_DPI` is absent, blank, non-numeric, or outside
#: `[MIN_RASTER_DPI, MAX_RASTER_DPI]`.
DEFAULT_RASTER_DPI = 200

#: Re-render resolution for the oversized-image fallback 
FALLBACK_RASTER_DPI = 150

MIN_RASTER_DPI = 72
MAX_RASTER_DPI = 400

#: Textract's synchronous `DetectDocumentText` input size limit (10 MB).
TEXTRACT_MAX_SYNC_BYTES = 10 * 1024 * 1024

#: Total attempts per page (1 initial + 2 retries), and
#: matching `guardrails_pii_detect.MAX_RETRIES`.
MAX_ATTEMPTS = 3

#: Backoff before retry attempt *n* is ``RETRY_BASE_DELAY_SECONDS * 2 ** n``,
#: i.e. 1s, 2s, 4s...
RETRY_BASE_DELAY_SECONDS = 1.0

_TEXTRACT_LINE_BLOCK_TYPE = "LINE"
_TEXTRACT_WORD_BLOCK_TYPE = "WORD"
_TEXTRACT_CHILD_RELATIONSHIP_TYPE = "CHILD"

_OCR_ZONE = "ocr"
_WORD_SEPARATOR = " "
_LINE_SEPARATOR = "\n"

#: `botocore` error codes treated as transient and therefore retryable
#: (see `_is_retryable`). Kept identical to
#: `guardrails_pii_detect._RETRYABLE_ERROR_CODES` minus the Bedrock-only
#: `ModelTimeoutException`, plus Textract's own transient codes.
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "ProvisionedThroughputExceededException",
        "ServiceUnavailableException",
        "ServiceUnavailable",
        "InternalServerException",
        "InternalServerError",
        "InternalFailure",
    }
)

# Lazily-initialized so a single client is reused across invocations within
# the same Agent Runtime process, consistent with the lazy-client pattern
# used elsewhere in this backend (e.g. `agent/tools/ocr.py`,
# `agent/tools/guardrails_pii_detect.py`).
_textract_client = None


class OcrPathError(Exception):
    """Unrecoverable OCR_Path failure, carrying the page it happened on.

    Propagates out of `ocr_pdf_pages` to `handle_job`'s top-level handler,
    which drives the Redaction Job to `failed` with this message as
    `errorMessage` and stores no Redacted Document


    `pageIndex` is the failing 0-based source page index, or `None` when the
    failure was not attributable to one page (e.g. the document could not be
    opened at all). The page index is also interpolated into the message, so
    a caller that only records `str(error)` still surfaces it.
    """

    def __init__(self, reason: str, page_index: Optional[int] = None) -> None:
        self.pageIndex = page_index
        self.reason = reason
        if page_index is None:
            super().__init__("OCR path failed: {}".format(reason))
        else:
            super().__init__(
                "OCR path failed on page {}: {}".format(page_index, reason)
            )


def _get_textract_client():
    global _textract_client
    if _textract_client is None:
        import boto3

        _textract_client = boto3.client("textract")
    return _textract_client


def _read_raster_dpi() -> int:
    """Resolve the Raster_Resolution from `PDF_OCR_DPI`.

    Returns the parsed value when it is an integer inside
    `[MIN_RASTER_DPI, MAX_RASTER_DPI]`, and `DEFAULT_RASTER_DPI` otherwise —
    absent, blank, non-numeric, and out-of-range all fall back to the
    default rather than being clamped to the nearest bound. Clamping would
    turn an obviously-wrong value (`5`, `100000`) into a plausible-looking
    one and hide the misconfiguration; falling back to a known-good default
    keeps the resolved value inside the range either way, which is what the
    constraint actually requires.
    """
    raw_value = (os.environ.get(RASTER_DPI_ENV_VAR) or "").strip()
    if not raw_value:
        return DEFAULT_RASTER_DPI

    try:
        dpi = int(raw_value)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using %d dpi",
            RASTER_DPI_ENV_VAR,
            raw_value,
            DEFAULT_RASTER_DPI,
        )
        return DEFAULT_RASTER_DPI

    if not MIN_RASTER_DPI <= dpi <= MAX_RASTER_DPI:
        logger.warning(
            "%s=%d is outside [%d, %d]; using %d dpi",
            RASTER_DPI_ENV_VAR,
            dpi,
            MIN_RASTER_DPI,
            MAX_RASTER_DPI,
            DEFAULT_RASTER_DPI,
        )
        return DEFAULT_RASTER_DPI

    return dpi


def _render_page_png(page: "pdfium.PdfPage", dpi: int) -> bytes:
    """Rasterize `page`'s full visible area to PNG bytes at `dpi`.

    `page.render` applies the page's `/Rotate` and covers exactly the
    rectangle `page.get_size()` reports, which is the rectangle the recorded
    normalized word coordinates are relative to. The bitmap is encoded to PNG
    with Pillow.
    """
    image = page.render(scale=dpi / 72, draw_annots=True).to_pil()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _is_retryable(error: Exception) -> bool:
    """Return True for throttling and 5xx service errors.

    Reads `botocore`'s `ClientError.response` shape duck-typed (via
    `getattr`) rather than importing `botocore.exceptions`, exactly as
    `guardrails_pii_detect._is_retryable` does, so this module stays
    importable — and unit-testable with a plain fake client — without
    boto3/botocore installed.
    """
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False

    error_code = (response.get("Error") or {}).get("Code")
    if error_code in _RETRYABLE_ERROR_CODES:
        return True

    status_code = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return isinstance(status_code, int) and status_code >= 500


def _detect_with_retry(client, png_bytes: bytes) -> dict:
    """Call `DetectDocumentText` for one page image, retrying transient
    failures with exponential backoff up to `MAX_ATTEMPTS` total attempts.

    Makes exactly `min(transient_failures + 1, MAX_ATTEMPTS)` attempts: a
    non-retryable error is re-raised immediately rather than burning
    attempts on a failure that cannot change outcome. The final exception is
    re-raised for `ocr_pdf_pages` to convert into an `OcrPathError` carrying
    the page index (which this function does not know).
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            return client.detect_document_text(Document={"Bytes": png_bytes})
        except Exception as error:  # noqa: BLE001 - re-raised below
            is_last_attempt = attempt == MAX_ATTEMPTS - 1
            if not _is_retryable(error) or is_last_attempt:
                raise
            # `describe_exception`, not `error`. This call's request body *is*
            # the rasterized page image (`Document={"Bytes": png_bytes}`), so a
            # botocore `ParamValidationError` quoting the offending parameter
            # would echo the page image into the log record through
            # `str(exc)`. The sanitizer keeps the part the retry decision
            # needs — `ThrottlingException`,
            # `ProvisionedThroughputExceededException` still read as
            # themselves via the AWS error code — and discards the free text.
            # Do not unwrap this as noise.
            logger.warning(
                "DetectDocumentText failed on attempt %d/%d; retrying: %s",
                attempt + 1,
                MAX_ATTEMPTS,
                describe_exception(error),
            )
            time.sleep(RETRY_BASE_DELAY_SECONDS * (2 ** attempt))

    # Unreachable: the loop either returns or raises on its last attempt.
    raise AssertionError("_detect_with_retry exhausted its loop without a result")


def _detect_page_text(client, page: "pdfium.PdfPage", page_index: int, dpi: int) -> dict:
    """Rasterize `page` and return its `DetectDocumentText` response.

    Applies the oversized-image fallback: when the PNG
    exceeds Textract's synchronous input limit, the page is re-rasterized
    once at `FALLBACK_RASTER_DPI` and called again. Re-rendering is skipped
    when the resolved dpi already equals `FALLBACK_RASTER_DPI`, since that
    would reproduce byte-identical output; the oversized bytes are sent and
    Textract's own rejection becomes the failure reason.

    Raises `OcrPathError` when the page cannot be rasterized or when
    recognition fails after the retries above.
    """
    try:
        png_bytes = _render_page_png(page, dpi)
    except Exception as error:  # noqa: BLE001 - pypdfium2 raises its own types
        raise OcrPathError(
            "could not rasterize page at {} dpi: {}".format(dpi, error),
            page_index=page_index,
        ) from error

    if len(png_bytes) > TEXTRACT_MAX_SYNC_BYTES and dpi != FALLBACK_RASTER_DPI:
        logger.warning(
            "Page %d rasterized to %d bytes at %d dpi, over Textract's %d byte "
            "synchronous limit; re-rendering at %d dpi",
            page_index,
            len(png_bytes),
            dpi,
            TEXTRACT_MAX_SYNC_BYTES,
            FALLBACK_RASTER_DPI,
        )
        try:
            png_bytes = _render_page_png(page, FALLBACK_RASTER_DPI)
        except Exception as error:  # noqa: BLE001 - pypdfium2 raises its own types
            raise OcrPathError(
                "could not rasterize page at {} dpi: {}".format(
                    FALLBACK_RASTER_DPI, error
                ),
                page_index=page_index,
            ) from error

    try:
        return _detect_with_retry(client, png_bytes)
    except Exception as error:  # noqa: BLE001 - converted to OcrPathError
        raise OcrPathError(
            "DetectDocumentText failed after {} attempt(s): {}".format(
                MAX_ATTEMPTS, error
            ),
            page_index=page_index,
        ) from error


def _index_blocks_by_id(blocks: List[dict]) -> dict:
    """Map every block's `Id` to the block itself, so a `LINE`'s
    `Relationships[CHILD].Ids` can be resolved to its `WORD` blocks.

    Textract's response is a flat `Blocks[]` list; parent/child structure
    exists only as these id references, so the map has to be built before
    the `LINE` walk.
    """
    return {
        block["Id"]: block
        for block in blocks
        if isinstance(block, dict) and block.get("Id")
    }


def _child_word_blocks(line_block: dict, blocks_by_id: dict) -> List[dict]:
    """Return `line_block`'s child `WORD` blocks, in the order its
    `Relationships[CHILD].Ids` list gives them.

    Ids that resolve to nothing, or to a non-`WORD` block, are skipped: a
    `LINE` may legitimately reference other block types, and a dangling id
    is a malformed response we cannot place on the page anyway.
    """
    words: List[dict] = []
    for relationship in line_block.get("Relationships") or []:
        if relationship.get("Type") != _TEXTRACT_CHILD_RELATIONSHIP_TYPE:
            continue
        for child_id in relationship.get("Ids") or []:
            child = blocks_by_id.get(child_id)
            if child and child.get("BlockType") == _TEXTRACT_WORD_BLOCK_TYPE:
                words.append(child)
    return words


def _normalized_bounding_box(word_block: dict) -> Optional[BoundingBox]:
    """Return a `WORD` block's `Geometry.BoundingBox` as a `BoundingBox`,
    keeping Textract's normalized `[0, 1]` values verbatim (interface decision 4), or
    `None` when the block carries no usable geometry.

    A word without geometry cannot be placed on the page, and inventing a
    zero-sized box for it would produce a rectangle that silently covers
    nothing — so such a word is dropped from the unit entirely rather than
    recorded unplaceably.
    """
    box = (word_block.get("Geometry") or {}).get("BoundingBox")
    if not isinstance(box, dict):
        return None
    try:
        return BoundingBox(
            left=float(box["Left"]),
            top=float(box["Top"]),
            width=float(box["Width"]),
            height=float(box["Height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _build_page_unit(
    page: "pdfium.PdfPage", page_index: int, response: dict
) -> ExtractedTextUnit:
    """Assemble one page's OCR_Text_Unit from a `DetectDocumentText` response.

    Walks `LINE` blocks in Textract's returned order and, within each line,
    its child `WORD` blocks in the order the `CHILD` relationship lists
    them. The unit text is built from `WORD` text — not from `LINE` text —
    so that every character of the unit belongs to a word with known
    geometry, which is what makes an arbitrary offset range resolvable to
    rectangles.

    Words are joined with a single space within a line and a single newline
    between lines. Those separator characters belong to NO word, so a span
    covering only separators resolves to zero rectangles, while a span
    touching any part of a word resolves to that whole word.

    Separators are emitted only *between* words that were actually recorded,
    so a line whose words were all unusable (empty text or missing
    geometry) leaves no stray blank line behind and the text never starts or
    ends with a separator.
    """
    blocks = [
        block for block in response.get("Blocks") or [] if isinstance(block, dict)
    ]
    blocks_by_id = _index_blocks_by_id(blocks)

    text_parts: List[str] = []
    words: List[OcrWord] = []
    offset = 0

    for line_block in blocks:
        if line_block.get("BlockType") != _TEXTRACT_LINE_BLOCK_TYPE:
            continue

        is_first_word_on_line = True
        for word_block in _child_word_blocks(line_block, blocks_by_id):
            word_text = word_block.get("Text") or ""
            bounding_box = _normalized_bounding_box(word_block)
            if not word_text or bounding_box is None:
                continue

            if offset > 0:
                separator = (
                    _LINE_SEPARATOR if is_first_word_on_line else _WORD_SEPARATOR
                )
                text_parts.append(separator)
                offset += len(separator)

            words.append(
                OcrWord(
                    text=word_text,
                    startOffset=offset,
                    endOffset=offset + len(word_text),
                    boundingBox=bounding_box,
                )
            )
            text_parts.append(word_text)
            offset += len(word_text)
            is_first_word_on_line = False

    try:
        page_width, page_height = page.get_size()
    except Exception as error:  # noqa: BLE001 - pypdfium2 raises its own types
        raise OcrPathError(
            "could not read page size: {}".format(error), page_index=page_index
        ) from error

    return ExtractedTextUnit(
        zone=_OCR_ZONE,
        # A page Textract recognized nothing on still gets a unit, with
        # `text=""` (one unit per page, always) - "Textract was consulted and found
        # nothing" is a result, not a failure.
        text="".join(text_parts),
        position=TextPosition(index=page_index, pageIndex=page_index),
        boundingBox=BoundingBox(
            left=0,
            top=0,
            width=page_width,
            height=page_height,
        ),
        ocrWords=words,
    )


def ocr_pdf_pages(file_bytes: bytes) -> List[ExtractedTextUnit]:
    """Recognize every page of a `.pdf` via Textract and return one
    OCR_Text_Unit per page.

    Parameters
    ----------
    file_bytes:
        The raw bytes of the source `.pdf` document — the same bytes handed
        to `extract_pdf_content`, read from S3 by the Agent orchestration.

    Returns
    -------
    List[ExtractedTextUnit]
        Exactly one unit per page, in page order, each with `zone="ocr"`,
        `position.pageIndex` equal to its own list index, `boundingBox` set
        to the visible page size, and `ocrWords` holding every recognized word's
        offset range and normalized bounding box. Pages Textract recognized
        no words on still appear, with `text=""`, so the
        `ocr:{index}` Unit_Ref index *is* the page index.

    Raises
    ------
    OcrPathError
        If the document cannot be opened (corrupt, empty, or password
        protected; `pageIndex` is `None`), or if any page cannot be
        rasterized or recognized (`pageIndex` is that page's index and is
        included in the message). Nothing partial is returned: a caller
        must not be able to mistake a degraded detection run for a clean one.
    """
    if not file_bytes:
        raise OcrPathError("could not open PDF: empty document")

    try:
        document = pdfium.PdfDocument(file_bytes)
    except Exception as error:  # noqa: BLE001 - pypdfium2 raises its own types
        # Covers corrupt input and a document that needs a user password.
        raise OcrPathError("could not open PDF: {}".format(error)) from error

    try:
        dpi = _read_raster_dpi()

        try:
            client = _get_textract_client()
        except Exception as error:  # noqa: BLE001 - e.g. no credentials/region
            raise OcrPathError(
                "could not create textract client: {}".format(error)
            ) from error

        units: List[ExtractedTextUnit] = []
        for page_index in range(len(document)):
            try:
                page = document[page_index]
            except Exception as error:  # noqa: BLE001 - pypdfium2's own types
                raise OcrPathError(
                    "could not load page: {}".format(error), page_index=page_index
                ) from error

            try:
                response = _detect_page_text(client, page, page_index, dpi)
                units.append(_build_page_unit(page, page_index, response))
            finally:
                page.close()

        return units
    finally:
        document.close()
