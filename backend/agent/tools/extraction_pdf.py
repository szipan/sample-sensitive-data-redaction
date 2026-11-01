r"""`.pdf` content extraction (design.md "AgentCore Agent 工具编排设计" ->
`extract_document_content` row, task 13.2).

Scope note (avoids a merge conflict with the concurrently-implemented `.docx`
extractor, task 13.1): this module implements ONLY the `.pdf` half of the
design's `extract_document_content` tool, under the distinct function name
`extract_pdf_content` (not `extract_document_content`). A later integration
task will add the extension-dispatching wrapper
`extract_document_content(file_bytes, extension)` that calls this function
for `extension == ".pdf"` and the `.docx` module's `extract_docx_content`
(e.g. in `extraction_docx.py`) for `extension == ".docx"`. Do not implement
that dispatcher here.

Uses `PyMuPDF` (import name `fitz`) to open the PDF from bytes and walk its
pages in document order, producing:

- One `ExtractedTextUnit` (zone="body") per page, with
  `position.pageIndex` and `boundingBox` populated from the block's page
  and on-page rectangle (task 16.2 additive extension - see the
  `TextPosition.pageIndex`/`ExtractedTextUnit.boundingBox` docstrings in
  `shared/models.py` for why: PyMuPDF's redaction API,
  `page.add_redact_annot(rect, ...)`, needs a `fitz.Page` object plus a
  rectangle, and the only place that page+rect pairing is naturally known
  is right here during extraction, block by block. Without recording it
  now, the redaction tool would have to re-run text extraction and guess
  which block corresponds to which `MatchSpan.unitRef` index - fragile and
  duplicative. This field is optional/defaulted on both dataclasses, so it
  is fully backward-compatible with every existing caller, including the
  `.docx` extractor and this module's own pre-existing tests).
- One placeholder `ImageRegion` per embedded image per page, with
  `boundingBox` populated from the image's position on the page and
  `recognizedText` left `None` (Requirement 4.7: filled in later by the
  Image Text Recognition / OCR tool, task 13.4).
- Structural metadata (page count, page order) for later use by
  `verify_structure` (task 16.5, Requirement 4.2).

Text source: `page.get_text("rawdict")` (NOT `get_text("blocks")` / `"text"`)
-----------------------------------------------------------------------

The page text is built by concatenating every rendered glyph reported by
`page.get_text("rawdict")`, walking `blocks -> lines -> spans -> chars` in
the order PyMuPDF returns them, inserting NO separators of any kind (no
`\n`, no spaces). This is deliberate and load-bearing:

- `redaction_pdf.py` resolves a match's on-page rectangle by walking the
  exact same `rawdict` traversal and indexing into the flat character list
  with the match's `startOffset`/`endOffset`. Because the extracted text is
  literally the concatenation of those characters, offset `i` in
  `ExtractedTextUnit.text` IS index `i` in that character list - 1:1, with
  no offset-mapping function and no assumption that two different
  `get_text` modes agree on block ordering or block filtering.

  `get_text("blocks")` cannot provide that guarantee: it injects a `\n` at
  every line break plus a trailing `\n` per block (characters that do not
  exist in `rawdict`), returns blocks in sorted reading order rather than
  raw content-stream order, and any per-block filtering applied on one side
  but not the other silently shifts every subsequent character index.

- As a direct consequence, PII that the PDF renders across a line break
  becomes contiguous in the extracted text: a social-security number laid
  out as `123-45-` / `6789` on two lines reads as `123-45-6789`, so both
  the regex rules in `matching.py` and Bedrock Guardrails can match it. With
  a synthetic `\n` in the middle, `\d{3}-\d{2}-\d{4}` never matches and a
  Guardrails-reported `match="123-45-6789"` cannot be located in the text at
  all, so the detection is dropped and the PII survives redaction.

  Where the PDF *does* render a space at a wrap point (the common case for
  ordinary prose) that space is a real glyph and is preserved, so normal
  text still reads naturally.

No per-block filtering is applied here (not even skipping whitespace-only
blocks) for the same reason: any conditional skipping must be mirrored
byte-for-byte by the redaction traversal, and the cheapest way to guarantee
that is for neither side to skip anything. Only a page whose *entire*
concatenated text is empty or whitespace-only is dropped, and dropping it
emits no unit at all, so no character indices are affected.

Zone choice for PDF text blocks (see `shared.models.TextUnitZone`): PDFs do
not expose a structural header/footer/table concept at the `PyMuPDF`
text-block level the way `.docx` exposes header/footer XML parts and table
XML elements - a PDF page is just a sequence of positioned text blocks. So
every extracted PDF text block is classified `zone="body"` here, even if it
is visually positioned like a header or footer. Design.md does not call out
a PDF-specific header/footer zone, so this uniform "body" classification is
this task's intentional take on the ambiguity.
"""
from dataclasses import dataclass, field
from typing import List, Optional

import fitz  # PyMuPDF

from shared.models import BoundingBox, ExtractedTextUnit, ImageRegion, TextPosition


class ExtractionError(Exception):
    """Raised when a `.pdf` file cannot be parsed (design.md Error Handling:
    "Agent 解析失败（损坏文件、加密 PDF 等）" -> `extract_document_content`
    raises `ExtractionError`, caught by the Agent orchestration)."""


@dataclass
class ExtractionResult:
    """Container returned by `extract_pdf_content` (and, later, the shared
    `extract_document_content` dispatcher for the `.docx` equivalent).

    `pageCount` and `pageOrder` are the structural metadata described in
    design.md's `extract_document_content` row ("页面/表格结构元数据"),
    used later by `verify_structure` (Requirement 4.2) to confirm the
    Redacted Document has the same page count and page order as the source.
    """

    textUnits: List[ExtractedTextUnit] = field(default_factory=list)
    imageRegions: List[ImageRegion] = field(default_factory=list)
    pageCount: int = 0
    pageOrder: List[int] = field(default_factory=list)


def extract_pdf_content(file_bytes: bytes) -> ExtractionResult:
    """Extract text units and image region placeholders from a `.pdf` file.

    Parameters
    ----------
    file_bytes:
        The raw bytes of the source `.pdf` document (e.g. read from S3 by
        the Agent orchestration before calling this tool).

    Returns
    -------
    ExtractionResult
        `textUnits` (one per text block, `zone="body"`), `imageRegions`
        (one placeholder per embedded image, `recognizedText` unset), and
        page-count/page-order structural metadata.

    Raises
    ------
    ExtractionError
        If `file_bytes` is not a readable PDF (e.g. corrupted or
        password-encrypted without a supplied password), matching
        design.md's documented failure mode for `extract_document_content`.
    """
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises its own exception types
        raise ExtractionError("Failed to open PDF: {}".format(exc)) from exc

    try:
        if document.is_encrypted:
            raise ExtractionError("PDF is encrypted and cannot be parsed.")

        text_units: List[ExtractedTextUnit] = []
        image_regions: List[ImageRegion] = []
        page_order: List[int] = []

        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            page_order.append(page_index)

            text_units.extend(_extract_text_units(page, page_index))
            image_regions.extend(_extract_image_regions(document, page, page_index))

        return ExtractionResult(
            textUnits=text_units,
            imageRegions=image_regions,
            pageCount=document.page_count,
            pageOrder=page_order,
        )
    finally:
        document.close()


def _extract_text_units(page: "fitz.Page", page_index: int) -> List[ExtractedTextUnit]:
    """Return ONE `ExtractedTextUnit` (zone="body") per page, whose `text` is
    the concatenation of every rendered glyph on the page as reported by
    `page.get_text("rawdict")`.

    See the module docstring's "Text source" section for why `rawdict` is
    used rather than `get_text("blocks")`: offset `i` in the returned
    `text` is index `i` in the identical character traversal performed by
    `redaction_pdf.py`, which is what makes the redaction rectangles line up
    and what makes line-wrapped PII matchable.

    The traversal is unconditional: every `type == 0` (text) block
    contributes every character of every line of every span, in the order
    `rawdict` returns them, with no separators inserted and no
    whitespace-only blocks skipped. Any conditional skipping here would have
    to be mirrored exactly on the redaction side or every subsequent
    character index would drift.

    The bounding box is the full page mediabox: the unit spans the whole
    page, and the precise rectangles are resolved per character at redaction
    time.
    """
    try:
        raw = page.get_text("rawdict")
    except Exception as exc:  # PyMuPDF raises its own exception types
        raise ExtractionError(
            "Failed to extract text from page {}: {}".format(page_index, exc)
        ) from exc

    # blocks -> lines -> spans -> chars, in the order rawdict returns them.
    # `block["type"] == 0` marks a text block (`1` is an image block, handled
    # separately below via `get_images`/`get_image_rects`).
    char_values: List[str] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span_data in line.get("spans", []):
                for char_data in span_data.get("chars", []):
                    # `c` is a single character in practice, but PyMuPDF may
                    # in principle report a multi-character string (e.g. an
                    # expanded ligature) or an empty one. It is appended
                    # verbatim: `redaction_pdf.py` repeats each char entry's
                    # bbox `len(c)` times, so the 1:1 offset <-> rectangle
                    # correspondence holds for any `len(c)`, including 0.
                    char_values.append(char_data.get("c", ""))

    page_text = "".join(char_values)
    if page_text.strip() == "":
        # Nothing rendered on this page (or only whitespace): emit no unit.
        # Dropping the whole page is safe for index alignment because the
        # unit itself never exists, unlike skipping a block *within* a page.
        return []

    # Use the full page mediabox as the bounding box for this merged unit.
    media_rect = page.mediabox
    return [
        ExtractedTextUnit(
            zone="body",
            text=page_text,
            position=TextPosition(index=0, pageIndex=page_index),
            boundingBox=BoundingBox(
                left=media_rect.x0,
                top=media_rect.y0,
                width=media_rect.x1 - media_rect.x0,
                height=media_rect.y1 - media_rect.y0,
            ),
        )
    ]


def _extract_image_regions(
    document: "fitz.Document", page: "fitz.Page", page_index: int
) -> List[ImageRegion]:
    """Return one placeholder `ImageRegion` per embedded image on `page`.

    `boundingBox` is populated from the image's rectangle on the page
    (via `page.get_image_rects`); `recognizedText` is left `None` for the
    later OCR tool (task 13.4) to fill in. `sourceLocation` identifies the
    page and image so the OCR tool (or a later re-extraction step) can find
    the same image again.

    `imageBytes` is populated from the raw embedded image stream via the
    document-level `document.extract_image(xref)` (a `fitz.Document` method,
    not a `fitz.Page` method - hence `document` is threaded in here), which
    returns a dict shaped like `{"image": <bytes>, "ext": "png", ...}`
    (in-image-redaction task 2.1, Requirement 1.1: the redactor needs the raw
    image bytes to decode, black out the PII region(s), and re-embed the
    image). The `xref` remains the re-embed handle recorded in
    `sourceLocation`, so that format is unchanged.

    Extraction is best-effort: if `extract_image` raises or returns no/empty
    stream (e.g. a mask-only or exotic-colorspace image PyMuPDF cannot
    materialize as bytes), `imageBytes` is left `None`. Such a region is then
    skipped by the OCR tool (it can carry no `image:` spans) and never touched
    by the redactor, and - crucially - a single un-extractable image never
    breaks extraction of the rest of the document.
    """
    regions: List[ImageRegion] = []
    for image_info in page.get_images(full=True):
        xref = image_info[0]
        rects = page.get_image_rects(xref)
        bounding_box: Optional[BoundingBox] = None
        if rects:
            rect = rects[0]
            bounding_box = BoundingBox(
                left=rect.x0,
                top=rect.y0,
                width=rect.x1 - rect.x0,
                height=rect.y1 - rect.y0,
            )
        image_bytes = _extract_image_bytes(document, xref)
        source_location = "page:{}:xref:{}".format(page_index, xref)
        regions.append(
            ImageRegion(
                zone="image",
                sourceLocation=source_location,
                recognizedText=None,
                boundingBox=bounding_box,
                imageBytes=image_bytes,
            )
        )
    return regions


def _extract_image_bytes(document: "fitz.Document", xref: int) -> Optional[bytes]:
    """Return the raw embedded image bytes for `xref`, or `None` if they
    cannot be materialized.

    Wraps `document.extract_image(xref)` defensively (in-image-redaction
    task 2.1): any exception, a falsy result, or a missing/empty `"image"`
    entry yields `None` rather than propagating, so a single un-extractable
    image cannot break extraction of the whole document.
    """
    try:
        extracted = document.extract_image(xref)
    except Exception:  # PyMuPDF raises its own exception types
        return None
    if not extracted:
        return None
    image_bytes = extracted.get("image")
    if not image_bytes:
        return None
    return image_bytes
