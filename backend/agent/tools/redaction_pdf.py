"""`.pdf` implementation of the `redact_document` Agent tool (design.md
"AgentCore Agent 工具编排设计" tool table, `.pdf` row; task 16.2).

design.md's tool table row for `redact_document` (`.pdf` half):

    `.pdf`：使用 `page.add_redact_annot(rect, fill=(0,0,0))` 标记后
    `page.apply_redactions()` 永久擦除并填充黑色

Requirements covered:
    - 3.2: matched text spans are replaced with a Solid Redaction Block.
    - 3.4: zero matches leaves the document's visible content intact.
    - 4.3: non-matching visible text renders identically to the source, in
      the same reading position.
    - 4.4: table structure (row/column counts, cell-content mapping) is
      preserved. PDFs have no native "table" concept at the `PyMuPDF`
      text-block level (see `extraction_pdf.py`'s zone-choice docstring -
      every PDF text block is classified `zone="body"`), so there is no
      PDF-specific table XML to special-case here; visual table-like
      content is just more text blocks, already handled generically.

Scope note (task 16.2): this module implements ONLY the `.pdf` half of
`redact_document`, under the distinct function name `redact_pdf_document`
(not `redact_document`), mirroring the `extraction_pdf.py` /
`extraction_docx.py` split (task 13.1/13.2). The `.docx` half (task 16.1)
is implemented concurrently in a sibling module (`redaction_docx.py`). A
later integration task is expected to add an extension-dispatching wrapper
that calls this function for `.pdf` and the `.docx` module's function for
`.docx`; that wrapper is NOT implemented here.

Precondition (important - see `redact_pdf_document`'s docstring): `text_units`
passed to this function MUST be the exact same list, in the exact same
order, that was originally passed as `match_rules`'s `text_units` argument
when producing `match_spans`. `MatchSpan.unitRef` values of the form
`"text:{index}"` (see `matching.py`) are 0-based indices into that list;
if the list differs (different filtering, different order, appended/removed
entries) the indices no longer point at the correct text unit and
redactions will be applied to the wrong text or fail to apply at all.

Sub-rectangle determination approach: two offset spaces, two resolvers
----------------------------------------------------------------------

    A `MatchSpan`'s offsets only mean something relative to the unit they
    were found in, and this module now sees units from two different
    detection paths (dual-path PDF PII detection). The `unitRef` prefix says
    which offset space the span lives in, and therefore which resolver turns
    its offsets into rectangles:

    ==========  =====================================  ============================
    prefix      offset space                           rectangle source
    ==========  =====================================  ============================
    `text:`     the page's `rawdict` character walk    per-character `bbox`,
                                                       grouped per `rawdict` line
    `ocr:`      the OCR-recognized page text           Textract `WORD` bounding
                                                       boxes, scaled to `page.rect`
    `image:`    an `ImageRegion.recognizedText`        out of scope here (skipped)
    ==========  =====================================  ============================

    The two spaces are unrelated - the same PII value sits at different
    offsets in each - which is exactly why spans from the two paths are never
    merged with each other upstream (`_regroup_and_merge` groups on the whole
    `unitRef` string, prefix included). The same value being redacted twice,
    with slightly different rectangles, is expected and harmless.

Text-layer path: direct character indexing
------------------------------------------

    `extract_pdf_content` builds each page's `ExtractedTextUnit.text` by
    concatenating every rendered glyph from `page.get_text("rawdict")`,
    walking `blocks -> lines -> spans -> chars` in the order `rawdict`
    returns them, with no separators inserted and no blocks skipped (see
    that module's "Text source" docstring section).

    This module performs the *identical* traversal, so a match's
    `startOffset`/`endOffset` index directly into the flat character list:
    offset `i` in the unit's text is character `i` in the list, 1:1. No
    offset-mapping, no block lookup by bounding box, no assumption that two
    `get_text` modes agree on ordering or filtering. Getting the exact
    character rectangles is then just `chars[startOffset:endOffset]`.

    Any divergence between the two traversals is what previously broke
    alignment: `get_text("blocks")` injects newline characters that do not
    exist in `rawdict` (so a line-wrapped SSN could not be matched at all),
    returns blocks in sorted reading order rather than content-stream order,
    and per-block whitespace filtering applied on one side only shifted
    every later character index - visible as a black block landing exactly
    `len("https://")` characters to the right of the URL it should cover.

    Multi-line matches: the matched characters are grouped by their
    `rawdict` line, and one rectangle is emitted per line group, so a match
    that wraps traces the wrapped shape (a rect on the tail of one line plus
    a rect on the head of the next) instead of one bounding box that would
    swallow the unmatched text between them.

    Proportional fallback: `_sub_rectangle_for_match` still contains the old
    proportional-fraction estimate for the case where `rawdict` parsing
    fails outright. With extraction and redaction now reading the same
    structure it should be unreachable in practice; it is kept only so the
    function stays total and never silently drops a redaction.

OCR path: word-granular geometry from Textract
----------------------------------------------

    OCR text has no per-character geometry, so the character-indexing
    approach above has nothing to index. Textract's smallest geometric unit
    is a `WORD`, and `ocr_pdf_pages` records each one's offset range within
    the unit's text plus its bounding box on `ExtractedTextUnit.ocrWords`.
    Resolution is therefore an intersection test rather than a slice:
    `_rects_for_ocr_span` emits the whole bounding box of every word whose
    `[startOffset, endOffset)` overlaps the span's range (Requirement 2.5).

    Word granularity means matching `Patti` inside `Patti's` blacks out
    `Patti's`. Over-redaction is the safe direction, and the text-layer path
    still contributes character-exact rectangles whenever the text layer is
    sound.

    The word boxes arrive NORMALIZED to `[0, 1]` (see the WARNING on
    `OcrWord.boundingBox`), so `_normalized_bbox_to_page_rect` scales them
    onto `page.rect` before they can be handed to `add_redact_annot`.

Why not `page.search_for(matched_substring)` (the other option the task
mentions): `search_for` finds the first/all on-page occurrences of a
*string*, not of "the match at this specific character offset" - if the
same PII string appears more than once in a block (or in the document),
`search_for` cannot disambiguate which occurrence corresponds to which
`MatchSpan` without additional positional reasoning that ends up
equivalent to (or more complex than) the proportional-fraction approach
already used here. The proportional approach also has the advantage of
working even when the matched substring contains characters `search_for`
might not match exactly (e.g. due to font ligature substitutions in the
extracted text vs. the rendered glyphs).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import fitz  # PyMuPDF

from agent.tools import redaction_image
from shared.models import BoundingBox, ExtractedTextUnit, ImageRegion, MatchSpan

TEXT_UNIT_REF_PREFIX = "text:"
OCR_UNIT_REF_PREFIX = "ocr:"
IMAGE_UNIT_REF_PREFIX = "image:"


class RedactionError(Exception):
    """Raised when a `.pdf` file cannot be re-opened/redacted (mirrors
    `extraction_pdf.ExtractionError`'s role for the extraction side)."""


@dataclass
class PdfRedactionReport:
    """Per-detection-path provenance for one redacted `.pdf`
    (Requirements 5.2, 5.3).

    Counts *rectangles actually redacted*, not `MatchSpan`s: one span can
    resolve to several rectangles (a text-layer match that wraps across a
    line, an OCR match that spans several words), and it is the rectangles
    that are the visible outcome a reviewer is judging.

    - `textLayerRegions`: rectangles resolved from `"text:"`-referenced spans.
    - `ocrRegions`: rectangles resolved from `"ocr:"`-referenced spans.
    - `agreedRegions`: OCR rectangles intersecting at least one text-layer
      rectangle on the same page — "did the other path see this too?".
      Bounded by `ocrRegions`, and 0 whenever either path produced nothing.

    The interesting number is the *difference*: an OCR region with no
    agreeing text-layer region is PII the text layer lost, which is the
    entire premise of the dual-path design. Counting agreement geometrically
    (rectangle intersection on a shared page) rather than by offsets is
    deliberate — the two paths' offsets live in unrelated coordinate spaces,
    so only the page geometry is comparable between them.

    `"image:"`-referenced spans are counted in neither field: the report is
    a text-vs-OCR dual-path diagnostic, and in-image rectangles belong to
    neither path (they are drawn on the image, not the page). The redactor
    now acts on `"image:"` spans (see `redact_pdf_document_with_report`), but
    their per-image rectangle counts are captured in the OCR audit log rather
    than folded into these counts.
    """

    textLayerRegions: int
    ocrRegions: int
    agreedRegions: int


@dataclass
class PdfRedactionResult:
    """A redacted `.pdf`'s bytes plus the provenance report for it.

    Exists so the orchestration layer can record a Detection_Summary
    (Requirement 5.1) without re-deriving counts it cannot see — only the
    redactor knows how many rectangles each span resolved to, and rectangles
    are gone by the time the caller holds bytes.
    """

    documentBytes: bytes
    report: PdfRedactionReport


def redact_pdf_document(
    file_bytes: bytes,
    text_units: List[ExtractedTextUnit],
    match_spans: List[MatchSpan],
    image_regions: Optional[List[ImageRegion]] = None,
) -> bytes:
    """Bytes-returning wrapper over `redact_pdf_document_with_report`.

    Kept at its original signature and return type so every caller and test
    written before the dual-path feature keeps working unchanged; callers
    that need the per-path counts (the orchestration layer, building the
    Detection_Summary) call `redact_pdf_document_with_report` directly and
    read `.documentBytes` off the result.

    Note this wrapper takes no `ocr_text_units`, so a `"ocr:"`-referenced
    span passed through it always references an empty list and raises
    `RedactionError` — correct, since resolving it is impossible without the
    unit list, and silently skipping a PII redaction is the one outcome worth
    failing loudly over.

    `image_regions` (in-image-redaction additive extension) is threaded
    straight through so a caller using this thin wrapper can still redact
    `"image:"`-referenced spans; defaulting to `None` preserves the
    pre-feature behavior for every existing caller.
    """
    return redact_pdf_document_with_report(
        file_bytes, text_units, match_spans, image_regions=image_regions
    ).documentBytes


def redact_pdf_document_with_report(
    file_bytes: bytes,
    text_units: List[ExtractedTextUnit],
    match_spans: List[MatchSpan],
    ocr_text_units: Optional[List[ExtractedTextUnit]] = None,
    image_regions: Optional[List[ImageRegion]] = None,
) -> PdfRedactionResult:
    """Produce a Redacted Document from a `.pdf`'s bytes by permanently
    erasing every matched text span and filling it with a black Solid
    Redaction Block (Requirements 3.2, 3.4, 4.3, 4.4), reporting how many
    rectangles each detection path contributed (Requirements 2.1, 2.4, 2.6,
    5.2, 5.3).

    Precondition: `text_units` MUST be the same list, in the same order,
    that was passed as `match_rules`'s `text_units` argument when
    `match_spans` was produced - `MatchSpan.unitRef` values of the form
    `"text:{index}"` are 0-based indices into that exact list (see
    `matching.py`'s `_text_unit_ref`). Passing a different list/order will
    silently redact the wrong text or raise `RedactionError` if an index is
    out of range.

    The same precondition applies to `ocr_text_units` for `"ocr:{index}"`
    spans, which index into *that* list (see `_resolve_ocr_unit_and_page`).
    The two lists are separate namespaces, so a `text:` index and an `ocr:`
    index of the same value refer to different units.

    Dispatch is on the `unitRef` prefix, because the prefix is what says
    which offset space a span's offsets live in (see the module docstring's
    two-resolvers table):

    - `"text:"` -> the page's `rawdict` character walk, resolved by
      `_sub_rectangle_for_match` (Requirement 2.1).
    - `"ocr:"` -> Textract `WORD` geometry, resolved by `_rects_for_ocr_span`
      (Requirement 2.2).
    - `"image:"` -> a span found in an embedded image's `recognizedText`
      (in-image-redaction feature). Grouped by region index, resolved to an
      `ImageRegion` in `image_regions`, and acted on by drawing black
      rectangles ON the image itself via `redaction_image.redact_image_bytes`
      and re-embedding the modified image with `page.replace_image(xref, ...)`
      (Requirements 1.1, 1.2). Before this feature these spans were silently
      skipped, leaving detected in-image PII visible in the output — the leak
      this feature closes.
    - anything else - out of scope; silently skipped.

    Rectangles from both paths are applied to the same page and the page's
    redactions are applied once, so overlapping rectangles from the two paths
    are both drawn rather than de-duplicated (Requirement 2.6) - overlapping
    black boxes are harmless, and dropping one would mean trusting one path's
    geometry over the other's.

    Each `ExtractedTextUnit` referenced by at least one match must carry
    `position.pageIndex` and `boundingBox` (as populated by
    `extract_pdf_content`, task 13.2/16.2) - these are required to know
    which page and rectangle to pass to `page.add_redact_annot`. OCR units
    must carry `position.pageIndex` and `ocrWords` instead (as populated by
    `ocr_pdf_pages`); they need no `boundingBox`, since their rectangles come
    from the words.

    Args:
        file_bytes: the raw bytes of the source `.pdf` document.
        text_units: the exact `ExtractedTextUnit` list originally passed to
            `match_rules` (see precondition above).
        match_spans: the `MatchSpan`s produced by `match_rules` and
            `detect_pii_with_guardrails` for this document, from both paths
            as one collection (Requirement 1.9).
        ocr_text_units: the exact OCR unit list originally passed to
            `match_rules` / `detect_pii_with_guardrails`, i.e.
            `ocr_pdf_pages`'s output. `None`/omitted for `.pdf`s processed
            without the OCR path, in which case no `"ocr:"` span can resolve.
        image_regions: the exact `ImageRegion` list originally passed to
            `match_rules` (i.e. `content.imageRegions`), carrying the raw
            `imageBytes`, the `"page:{pageIndex}:xref:{xref}"` re-embed handle
            in `sourceLocation`, and any `recognizedElements` geometry.
            `"image:{index}"` is a 0-based index into this exact list (the same
            precondition as `text_units`/`ocr_text_units`). `None`/omitted when
            the document has no embedded-image spans, in which case no
            `"image:"` span can resolve. Image rectangles are NOT folded into
            the `PdfRedactionReport` counts (the report is a text-vs-OCR
            dual-path diagnostic); per-image rectangle counts are captured in
            the OCR audit log instead.

    Returns:
        A `PdfRedactionResult` carrying the redacted `.pdf`'s bytes and the
        per-path `PdfRedactionReport`. When `match_spans` resolves to no
        rectangles at all (including the empty-list case), the bytes
        represent the document with no redaction applied (Requirement 2.7)
        and every report count is 0 - re-serialized rather than the original
        bytes object, since `PyMuPDF` requires opening/closing the document
        to produce output bytes either way, but the resulting content is
        unchanged from the source.

    Raises:
        RedactionError: if `file_bytes` cannot be opened as a PDF, or if a
            `MatchSpan` references a unit index that is out of range, a
            page index outside the document, or a unit missing the
            page/geometry information its extractor is expected to have
            supplied; also if an `"image:"` span references an out-of-range
            image region, a region with no `imageBytes`, or a region whose
            `sourceLocation` is not the expected `"page:{pageIndex}:xref:{xref}"`
            handle, or if the image cannot be decoded/drawn/re-encoded
            (`ImageRedactionError`) or re-embedded (Requirement 7.3).
    """
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises its own exception types
        raise RedactionError("Failed to open PDF for redaction: {}".format(exc)) from exc

    try:
        # Kept as two maps rather than one map of tagged rects: the report's
        # per-path counts and the agreement metric are both per-page
        # comparisons between the paths, and the application loop wants the
        # union - all three read naturally off this shape.
        text_rects_by_page: Dict[int, List["fitz.Rect"]] = {}
        ocr_rects_by_page: Dict[int, List["fitz.Rect"]] = {}
        # `"image:"`-referenced spans, grouped by their region index so each
        # embedded image is decoded/redrawn/re-embedded exactly once even when
        # several spans land in it (see the image-replacement pass below).
        image_spans_by_region: Dict[int, List[MatchSpan]] = {}

        for span in match_spans:
            if span.unitRef.startswith(TEXT_UNIT_REF_PREFIX):
                unit, page_index, rect = _resolve_unit_page_and_rect(text_units, span)
                page = _load_page(document, page_index)
                # One rect per line the match occupies (usually exactly one).
                text_rects_by_page.setdefault(page_index, []).extend(
                    _sub_rectangle_for_match(unit, span, rect, page=page)
                )
            elif span.unitRef.startswith(OCR_UNIT_REF_PREFIX):
                unit, page_index = _resolve_ocr_unit_and_page(ocr_text_units, span)
                page = _load_page(document, page_index)
                # One rect per overlapping Textract WORD; possibly none, when
                # the span covers only inter-word/inter-line separators.
                ocr_rects_by_page.setdefault(page_index, []).extend(
                    _rects_for_ocr_span(page, unit, span)
                )
            elif span.unitRef.startswith(IMAGE_UNIT_REF_PREFIX):
                region_index = _parse_image_region_index(span)
                image_spans_by_region.setdefault(region_index, []).append(span)
            else:
                # Any other (unknown) namespace is out of scope; skipped.
                continue

        for page_index in sorted(set(text_rects_by_page) | set(ocr_rects_by_page)):
            page = document.load_page(page_index)
            for rect in text_rects_by_page.get(page_index, []):
                page.add_redact_annot(rect, fill=(0, 0, 0))
            for rect in ocr_rects_by_page.get(page_index, []):
                page.add_redact_annot(rect, fill=(0, 0, 0))
            # Apply once per page (not per match), per design.md's
            # `redact_document` `.pdf` row.
            #
            # `images=PDF_REDACT_IMAGE_NONE` is REQUIRED for correct in-image
            # redaction (in-image-redaction bugfix): a text:/ocr: redaction
            # rectangle frequently overlaps an embedded image (full-page
            # Textract OCR recognizes the image's own text and emits ocr:
            # rectangles right on top of it). With the default image handling,
            # `apply_redactions` rewrites any image a redaction annotation
            # touches into a NEW xref, which invalidates the extraction-time
            # xref carried in `ImageRegion.sourceLocation` — the subsequent
            # `page.replace_image(xref, ...)` in `_redact_image_regions` then
            # writes the redacted image to the now-orphaned old xref while the
            # page keeps displaying the untouched, renumbered image, leaving
            # in-image PII visible. Leaving images untouched here keeps every
            # image xref stable so `replace_image` lands on the displayed
            # image; the redaction annotation's black fill rectangle is still
            # painted on the page regardless of this flag, so the text/OCR
            # span's own coverage is unchanged.
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # In-image redaction (Requirements 1.1, 1.2, 7.3): draw black
        # rectangles ON each referenced embedded image and swap the modified
        # bytes back into the image XObject in place. Independent of the
        # text/OCR `apply_redactions` above (which paint the page's own
        # content), so it runs after them; the single `document.tobytes()`
        # below still serializes once. `apply_redactions` above uses
        # `PDF_REDACT_IMAGE_NONE` so the image xrefs it referenced stay valid
        # for the `replace_image` calls here.
        _redact_image_regions(document, image_spans_by_region, image_regions)

        report = PdfRedactionReport(
            textLayerRegions=sum(len(rects) for rects in text_rects_by_page.values()),
            ocrRegions=sum(len(rects) for rects in ocr_rects_by_page.values()),
            agreedRegions=_count_agreed_regions(text_rects_by_page, ocr_rects_by_page),
        )
        # `garbage=4` drops objects orphaned by `replace_image` (the original
        # pre-redaction image streams) and `deflate=True` compresses streams,
        # so re-embedding a redacted image does not balloon the output (an
        # un-garbage-collected save left the orphaned originals in the file,
        # inflating a 76 KB source to several MB and pushing it past the
        # download API's response-size limit). Neither option alters the
        # visible/redacted content.
        return PdfRedactionResult(
            documentBytes=document.tobytes(garbage=4, deflate=True), report=report
        )
    finally:
        document.close()


def _load_page(document: "fitz.Document", page_index: int) -> "fitz.Page":
    """Load `page_index` from `document`, turning an out-of-range index into
    a `RedactionError` rather than a PyMuPDF error."""
    if page_index < 0 or page_index >= document.page_count:
        raise RedactionError(
            "Match references out-of-range page index {}".format(page_index)
        )
    return document.load_page(page_index)


def _parse_image_region_index(span: MatchSpan) -> int:
    """Extract the 0-based region index from an `"image:{index}"` `unitRef`,
    turning a malformed ref into a `RedactionError` (mirrors the `text:`/`ocr:`
    resolvers' posture)."""
    try:
        return int(span.unitRef.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise RedactionError("Malformed unitRef: {!r}".format(span.unitRef)) from exc


def _resolve_image_region(
    image_regions: "List[ImageRegion] | None", region_index: int
) -> ImageRegion:
    """Look up the `ImageRegion` an `"image:{index}"`-referenced span points at,
    validating the same list/order precondition documented on
    `redact_pdf_document_with_report`.

    Raises `RedactionError` on an out-of-range index — a wiring bug (the
    `image_regions` passed here is not the list `match_rules` indexed), matching
    the existing out-of-range guards for `text:`/`ocr:` units. Silently
    dropping the span would leave detected in-image PII visible, the one
    outcome worth failing loudly over.
    """
    regions = image_regions or []
    if region_index < 0 or region_index >= len(regions):
        raise RedactionError(
            "MatchSpan unitRef image:{} references an out-of-range image "
            "region index (image_regions has {} entries); image_regions must "
            "be the same list/order originally passed to match_rules.".format(
                region_index, len(regions)
            )
        )
    return regions[region_index]


def _parse_image_source_location(source_location: "str | None") -> tuple:
    """Parse an `ImageRegion.sourceLocation` of the form
    `"page:{pageIndex}:xref:{xref}"` into `(pageIndex, xref)`.

    This is the re-embed handle `extraction_pdf._extract_image_regions`
    produces; a value that does not match the expected shape is a wiring bug,
    so it raises `RedactionError` rather than guessing.
    """
    parts = (source_location or "").split(":")
    if len(parts) == 4 and parts[0] == "page" and parts[2] == "xref":
        try:
            return int(parts[1]), int(parts[3])
        except ValueError:
            pass
    raise RedactionError(
        "ImageRegion.sourceLocation {!r} is not of the expected form "
        "'page:{{pageIndex}}:xref:{{xref}}'".format(source_location)
    )


def _redact_image_regions(
    document: "fitz.Document",
    image_spans_by_region: Dict[int, List[MatchSpan]],
    image_regions: "List[ImageRegion] | None",
) -> None:
    """Redact every embedded image referenced by at least one `"image:"` span
    and re-embed the modified bytes in place (Requirements 1.1, 1.2, 7.3).

    For each referenced region: resolve `image:{index}` -> `ImageRegion`,
    require non-empty `imageBytes` (its absence is a wiring bug — the region was
    never OCR'd, so it could carry no spans), draw the redaction on the image
    via `redaction_image.redact_image_bytes` (converting an
    `ImageRedactionError` into `RedactionError`, Requirement 7.3), then swap the
    result into the image XObject with `page.replace_image(xref, stream=...)`,
    deriving `pageIndex`/`xref` from the region's `sourceLocation`.

    Regions are processed in ascending index order for deterministic behavior.
    Does nothing when there are no `"image:"` spans, so the pre-feature path is
    byte-for-byte unchanged.
    """
    for region_index in sorted(image_spans_by_region):
        spans = image_spans_by_region[region_index]
        region = _resolve_image_region(image_regions, region_index)

        if not region.imageBytes:
            raise RedactionError(
                "Image region at index {} (image:{}) has no imageBytes to "
                "redact; extract_document_content must populate imageBytes for "
                "any region that produced image: spans.".format(
                    region_index, region_index
                )
            )

        try:
            new_bytes = redaction_image.redact_image_bytes(
                region.imageBytes, region, spans
            )
        except redaction_image.ImageRedactionError as exc:
            raise RedactionError(
                "Failed to redact embedded image at image:{} "
                "(sourceLocation={!r}): {}".format(
                    region_index, region.sourceLocation, exc
                )
            ) from exc

        page_index, xref = _parse_image_source_location(region.sourceLocation)
        page = _load_page(document, page_index)
        try:
            page.replace_image(xref, stream=new_bytes)
        except Exception as exc:  # PyMuPDF raises its own exception types
            raise RedactionError(
                "Failed to re-embed redacted image at image:{} "
                "(page {}, xref {}): {}".format(
                    region_index, page_index, xref, exc
                )
            ) from exc


def _count_agreed_regions(
    text_rects_by_page: Dict[int, List["fitz.Rect"]],
    ocr_rects_by_page: Dict[int, List["fitz.Rect"]],
) -> int:
    """Count OCR rectangles that intersect at least one text-layer rectangle
    on the same page (Requirement 5.3).

    Counted from the OCR side, so the result is bounded by `ocrRegions` and
    reads as "how many of the OCR path's findings the text layer corroborated"
    - the direction that matters, since the OCR path exists to catch what the
    text layer misses. It is 0 whenever either path produced no rectangles.

    Intersection, not containment or equality: the two paths measure the same
    glyphs through different pipelines (character boxes versus Textract word
    boxes at some raster resolution), so their rectangles for one value are
    near-identical but never bit-identical. Touching edges do not count -
    `fitz.Rect.intersects` requires a non-empty overlap - so two rectangles
    that merely abut, e.g. adjacent words, are not treated as agreement.
    """
    agreed = 0
    for page_index, ocr_rects in ocr_rects_by_page.items():
        text_rects = text_rects_by_page.get(page_index)
        if not text_rects:
            continue
        for ocr_rect in ocr_rects:
            if any(ocr_rect.intersects(text_rect) for text_rect in text_rects):
                agreed += 1
    return agreed


def _resolve_unit_page_and_rect(
    text_units: List[ExtractedTextUnit], span: MatchSpan
) -> tuple:
    """Look up the `ExtractedTextUnit`, page index, and `fitz.Rect` a
    `"text:{index}"`-referenced `MatchSpan` points at, validating the
    precondition documented on `redact_pdf_document`."""
    try:
        index = int(span.unitRef.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise RedactionError("Malformed unitRef: {!r}".format(span.unitRef)) from exc

    if index < 0 or index >= len(text_units):
        raise RedactionError(
            "MatchSpan unitRef {!r} references an out-of-range text unit "
            "index (text_units has {} entries); text_units must be the "
            "same list/order originally passed to match_rules.".format(
                span.unitRef, len(text_units)
            )
        )

    unit = text_units[index]
    page_index = unit.position.pageIndex if unit.position else None
    bounding_box = unit.boundingBox

    if page_index is None or bounding_box is None:
        raise RedactionError(
            "Text unit at index {} is missing pageIndex/boundingBox; "
            "extract_pdf_content must populate both for redact_pdf_document "
            "to locate the correct page and rectangle.".format(index)
        )

    rect = fitz.Rect(
        bounding_box.left,
        bounding_box.top,
        bounding_box.left + bounding_box.width,
        bounding_box.top + bounding_box.height,
    )
    return unit, page_index, rect


def _collect_page_chars(page: "fitz.Page") -> "List[tuple] | None":
    """Return every rendered character on `page` as a flat list of
    `(line_id, bbox)` tuples, in `rawdict` order.

    This traversal MUST stay identical to `extraction_pdf._extract_text_units`:
    `page.get_text("rawdict")`, then `blocks -> lines -> spans -> chars` in
    the order returned, skipping only `block["type"] != 0` (non-text) blocks
    and applying no other filtering whatsoever. That is what makes index `i`
    of this list the character at offset `i` of the page's
    `ExtractedTextUnit.text`.

    `line_id` is a monotonically increasing counter per `rawdict` line, used
    to group a wrapped match's characters into one rectangle per line.

    Each char entry contributes `len(char["c"])` list positions (all sharing
    that entry's bbox), mirroring extraction appending `char["c"]` verbatim to
    the page text. `c` is a single character in practice, so this is normally
    one position per entry; doing it by length keeps offsets aligned even if
    PyMuPDF ever reports a multi-character (expanded ligature) or empty `c`.

    Returns `None` if `rawdict` cannot be read or the page has no characters.
    """
    try:
        raw = page.get_text("rawdict")
    except Exception:
        return None

    chars: List[tuple] = []
    line_id = 0
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            # type 0 = text block; type 1 = image block.
            continue
        for line in block.get("lines", []):
            for span_data in line.get("spans", []):
                for char_data in span_data.get("chars", []):
                    entry = (line_id, char_data.get("bbox"))
                    for _ in range(len(char_data.get("c", ""))):
                        chars.append(entry)
            line_id += 1

    return chars or None


def _get_char_rects_for_offset_range(
    page: "fitz.Page",
    start_offset: int,
    end_offset: int,
) -> "List[fitz.Rect] | None":
    """Return the on-page rectangles covering the characters at
    `[start_offset, end_offset)` of the page's `ExtractedTextUnit.text`.

    The offsets index directly into `_collect_page_chars(page)` - see that
    function and the module docstring for why no offset translation is
    needed. Characters are grouped by `rawdict` line and each group is
    unioned into one rectangle, so a match that wraps across a line break
    yields one rect per line rather than a single box spanning both lines
    plus everything between them.

    Returns `None` if the characters cannot be resolved (bad bounds or
    unreadable `rawdict`), leaving the caller to fall back.
    """
    chars = _collect_page_chars(page)
    if chars is None:
        return None

    if not (0 <= start_offset < end_offset <= len(chars)):
        return None

    rects_by_line: Dict[int, "fitz.Rect"] = {}
    ordered_line_ids: List[int] = []
    for line_id, bbox in chars[start_offset:end_offset]:
        if not bbox:
            continue
        rect = fitz.Rect(bbox)
        if line_id in rects_by_line:
            rects_by_line[line_id] = rects_by_line[line_id] | rect
        else:
            rects_by_line[line_id] = rect
            ordered_line_ids.append(line_id)

    rects = [rects_by_line[line_id] for line_id in ordered_line_ids]
    return rects or None


def _sub_rectangle_for_match(
    unit: ExtractedTextUnit, span: MatchSpan, block_rect: "fitz.Rect",
    page: "fitz.Page" = None,
) -> "List[fitz.Rect]":
    """Return the rectangle(s) to redact for `span.startOffset:endOffset`
    within `unit.text` - one rectangle per line the match occupies.

    Primary approach: index the page's `rawdict` character list directly
    with the match offsets (exact, and correct for variable-width fonts,
    CJK glyphs and line-wrapped matches alike).

    Fallback: the older proportional-fraction estimate over `block_rect`,
    used only when `page` is unavailable or `rawdict` cannot be parsed. Now
    that extraction and redaction walk the same `rawdict` structure this
    should be unreachable in practice; it exists so the function stays total
    and never silently skips a redaction.
    """
    matched_text = unit.text[span.startOffset:span.endOffset]

    if page is not None and matched_text:
        char_rects = _get_char_rects_for_offset_range(
            page, span.startOffset, span.endOffset
        )
        if char_rects:
            return char_rects

    # Fallback: proportional fraction over the unit's bounding box.
    text_length = len(unit.text)
    if text_length == 0:
        return [block_rect]

    start_fraction = max(0.0, min(1.0, span.startOffset / text_length))
    end_fraction = max(0.0, min(1.0, span.endOffset / text_length))
    if end_fraction < start_fraction:
        start_fraction, end_fraction = end_fraction, start_fraction

    width = block_rect.x1 - block_rect.x0
    left = block_rect.x0 + start_fraction * width
    right = block_rect.x0 + end_fraction * width

    return [fitz.Rect(left, block_rect.y0, right, block_rect.y1)]


# ---------------------------------------------------------------------------
# OCR path: resolving `ocr:`-referenced spans to page rectangles
# (dual-path PDF PII detection, Requirements 2.2, 2.3, 2.5)
# ---------------------------------------------------------------------------


def _normalized_bbox_to_page_rect(
    page_rect: "fitz.Rect", box: BoundingBox
) -> "fitz.Rect":
    """Map a Textract-normalized bounding box onto a page's rectangle
    (Requirement 2.3).

    `box`'s coordinates are fractions of the rasterized page image's width
    and height (`[0, 1]`), not points - see the WARNING on
    `OcrWord.boundingBox`. This function is the single place that conversion
    happens, so a normalized box never reaches `add_redact_annot` unscaled
    (which would black out a sliver in the page's top-left corner and look
    plausible while covering nothing).

    Note there is deliberately no `dpi` parameter, and it would be wrong to
    add one: `ocr_pdf_pages` rasterizes the *entire* `page.rect` via
    `page.get_pixmap(dpi=...)`, and Textract normalizes against that same
    image, so the normalized fractions are fractions of the whole page
    regardless of how many pixels it was rendered at. Raster resolution
    affects recognition quality and payload size only, never placement.

    `page.rect` (not `page.mediabox`) is the correct space to map into: it is
    rotation-aware, it is what `get_pixmap` rasterizes, and it is the space
    `page.add_redact_annot` consumes - so a `/Rotate 90` page stays
    consistent end to end without special-casing.
    """
    x0 = page_rect.x0 + box.left * page_rect.width
    y0 = page_rect.y0 + box.top * page_rect.height
    return fitz.Rect(
        x0,
        y0,
        x0 + box.width * page_rect.width,
        y0 + box.height * page_rect.height,
    )


def _rects_for_ocr_span(
    page: "fitz.Page", unit: ExtractedTextUnit, span: MatchSpan
) -> "List[fitz.Rect]":
    """Return the on-page rectangles to redact for an `"ocr:{index}"`-referenced
    `span` within `unit` (Requirements 2.2, 2.5).

    A word contributes its WHOLE bounding box when its offset range overlaps
    the span's range at all - OCR geometry is word-granular, so a span
    covering part of a word blacks out that entire word. Both ranges are
    half-open, so the test is `word.startOffset < span.endOffset and
    span.startOffset < word.endOffset`: a word merely *adjacent* to the
    span's boundary (its end exactly at the span's start, or its start
    exactly at the span's end) shares no character with the span and
    contributes nothing.

    Returns an empty list when no word overlaps - which is a legitimate
    outcome, not an error: the separator characters `ocr_pdf_pages` inserts
    between words (`" "`) and lines (`"\\n"`) belong to no word, so a span
    covering only separators resolves to zero rectangles. Ditto a page
    Textract recognized no words on.

    Rectangles are returned in `ocrWords` order (which is ascending by
    offset, per `OcrWord`'s invariant), one per overlapping word; they are
    NOT unioned, so a match spanning several words yields several boxes and
    a match that wraps across an OCR line traces the wrapped shape - the
    same treatment the text-layer resolver gives per-line groups.
    """
    rects: List["fitz.Rect"] = []
    for word in unit.ocrWords or []:
        if word.startOffset < span.endOffset and span.startOffset < word.endOffset:
            rects.append(_normalized_bbox_to_page_rect(page.rect, word.boundingBox))
    return rects


def _resolve_ocr_unit_and_page(
    ocr_text_units: "List[ExtractedTextUnit] | None", span: MatchSpan
) -> tuple:
    """Look up the OCR `ExtractedTextUnit` and page index an
    `"ocr:{index}"`-referenced `MatchSpan` points at.

    Mirrors `_resolve_unit_page_and_rect`'s role (and its precondition
    posture) for the OCR path: `ocr_text_units` MUST be the same list, in the
    same order, that was passed to `match_rules` / `detect_pii_with_guardrails`
    when `span` was produced, because `"ocr:{index}"` is a 0-based index into
    it. Unlike the text-layer path there is no per-unit `boundingBox` to
    resolve here - the rectangles come from `ocrWords`, so this returns just
    the unit and its page.

    Per `ocr_pdf_pages`, the OCR path emits exactly one unit per page in page
    order, so `index` equals `position.pageIndex`; the stored `pageIndex` is
    read rather than assumed, so a wiring bug surfaces as a `RedactionError`
    instead of a redaction landing on the wrong page.

    Raises:
        RedactionError: if `span.unitRef` is malformed, its index is out of
            range for `ocr_text_units`, or the referenced unit is missing
            `position.pageIndex` or `ocrWords`. All three are violated
            preconditions that `ocr_pdf_pages` cannot produce, so they mean
            the caller wired up the wrong list - worth failing loudly rather
            than silently skipping a PII redaction.
    """
    units = ocr_text_units or []

    try:
        index = int(span.unitRef.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise RedactionError("Malformed unitRef: {!r}".format(span.unitRef)) from exc

    if index < 0 or index >= len(units):
        raise RedactionError(
            "MatchSpan unitRef {!r} references an out-of-range OCR text unit "
            "index (ocr_text_units has {} entries); ocr_text_units must be "
            "the same list/order originally passed to match_rules.".format(
                span.unitRef, len(units)
            )
        )

    unit = units[index]
    page_index = unit.position.pageIndex if unit.position else None

    if page_index is None or unit.ocrWords is None:
        raise RedactionError(
            "OCR text unit at index {} is missing pageIndex/ocrWords; "
            "ocr_pdf_pages must populate both for redact_pdf_document to "
            "locate the correct page and word rectangles (a page Textract "
            "recognized no words on carries an empty ocrWords list, not "
            "None).".format(index)
        )

    return unit, page_index
