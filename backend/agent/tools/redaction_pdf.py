"""`.pdf` implementation of the `redact_document` Agent tool.

Matched text spans are turned into page rectangles (Redaction_Regions), the
text under those rectangles is removed from the content streams, and a solid
black block is drawn over each rectangle.

This module owns the public entry points and the span-to-region resolution.
All rectangles in this module are `(left, top, right, bottom)` tuples in
Page_Coordinates (points, origin at the top-left corner of the visible page,
y pointing down, `/Rotate` applied). Conversion to the PDF user space happens
only at the point where content is edited.

Precondition: `text_units` must be the exact list, in the exact order, that
was passed to `match_rules` when the `MatchSpan`s were produced. A
`MatchSpan.unitRef` of the form `"text:{index}"` is a 0-based index into that
list; `"ocr:{index}"` indexes `ocr_text_units` and `"image:{index}"` indexes
`image_regions` in the same way.

Two offset spaces, two resolvers
--------------------------------

The `unitRef` prefix says which offset space a span lives in, and therefore
how its offsets become rectangles:

==========  ==============================  ===============================
prefix      offset space                    rectangle source
==========  ==============================  ===============================
`text:`     the page's glyph traversal      per-character rectangles of
                                            `pdf_glyphs`, grouped per line
`ocr:`      the OCR-recognized page text    Textract `WORD` boxes, scaled to
                                            the page size
`image:`    an `ImageRegion.recognizedText` drawn on the image itself
==========  ==============================  ===============================

The offsets of the first two spaces are unrelated, which is why spans from
the two paths are never merged upstream. Rectangles from both paths are
applied to the same page and are both drawn; overlapping rectangles are not
de-duplicated, because dropping one would mean trusting one path's geometry
over the other's.

Text-layer path
---------------

`extraction_pdf` builds a page's text from `pdf_glyphs.collect_page_glyphs`,
and this module reads the same traversal. Offset `i` of the unit's text is
therefore `PageGlyphs.chars[i]`, with no translation step. The characters of
a match are grouped by `line_id` and each group yields the minimal bounding
rectangle of its character rectangles, so a match that wraps lines traces the
wrapped shape instead of one box covering the unmatched text between the
lines. Characters without a rectangle (`rect is None`) do not take part.

When the characters cannot be used, the region is estimated proportionally
from the Text_Unit's `boundingBox`: the top and bottom edges are the box's,
and the left and right edges are placed at `start / len(text)` and
`end / len(text)` of its width (ratios clamped to `[0, 1]`; an empty text
yields the whole box). The estimate is used when the page cannot be read,
when the page has no characters, when the offsets are outside the page's
characters, and when no character in the range has a rectangle. A span is
never dropped silently.

OCR path
--------

OCR text has no per-character geometry. Textract's smallest geometric unit is
a `WORD`, so every word whose `[startOffset, endOffset)` overlaps the span
contributes its whole bounding box; a match inside a longer word redacts the
whole word, which errs on the safe side. Word boxes are normalized to
`[0, 1]` and are scaled by the size of the page the OCR unit belongs to.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from agent.tools.pdf_geometry import (
    Rect,
    normalized_box_to_page_rect,
    rects_intersect,
)
from agent.tools.pdf_content_edit import (
    ContentEditError,
    apply_page_plan,
    draw_black_boxes,
    page_geometry_of,
    plan_deletions,
    remove_intersecting_annotations,
    scrub_document_strings,
)
from agent.tools.pdf_images import ImageWriteBackError, write_back_image
from agent.tools.redaction_image import ImageRedactionError, redact_image_bytes
from agent.tools.pdf_glyphs import (
    PageGlyphs,
    PageReadError,
    PdfHandle,
    PdfOpenError,
    _page_geometry,
    collect_page_glyphs,
    collect_glyphs_from_bytes,
    open_pdf,
)
from shared.models import ExtractedTextUnit, ImageRegion, MatchSpan

TEXT_UNIT_REF_PREFIX = "text:"
OCR_UNIT_REF_PREFIX = "ocr:"
IMAGE_UNIT_REF_PREFIX = "image:"


class RedactionError(Exception):
    """Raised when a `.pdf` file cannot be opened or redacted (mirrors
    `extraction_pdf.ExtractionError`'s role for the extraction side)."""


@dataclass
class PdfRedactionReport:
    """Per-detection-path provenance for one redacted `.pdf`.

    Counts *regions*, not `MatchSpan`s: one span can resolve to several
    regions (a text-layer match that wraps a line, an OCR match that covers
    several words), and the regions are the visible outcome.

    - `textLayerRegions`: regions resolved from `"text:"`-referenced spans.
    - `ocrRegions`: regions resolved from `"ocr:"`-referenced spans.
    - `agreedRegions`: OCR regions that intersect (with positive area) at
      least one text-layer region on the same page, i.e. "did the other path
      see this too?". Bounded by `ocrRegions`; edges that merely touch do not
      count.

    An OCR region with no agreeing text-layer region is PII the text layer
    lost, which is the premise of the dual-path design. Agreement is judged by
    page geometry because the two paths' offsets are unrelated.

    `"image:"` spans are counted in neither field: in-image rectangles are
    drawn on the image, not the page, and belong to neither path.
    """

    textLayerRegions: int
    ocrRegions: int
    agreedRegions: int


@dataclass
class PdfRedactionResult:
    """A redacted `.pdf`'s bytes plus the provenance report for it."""

    documentBytes: bytes
    report: PdfRedactionReport


@dataclass
class _ResolvedRegions:
    """Everything the span-resolution step produces.

    Kept separate from the page-editing step so that step can consume it
    unchanged.
    """

    text_regions_by_page: Dict[int, List[Rect]] = field(default_factory=dict)
    ocr_regions_by_page: Dict[int, List[Rect]] = field(default_factory=dict)
    # `"image:"` spans grouped by region index, so every embedded image is
    # redacted exactly once even when several spans land in it.
    image_spans_by_region: Dict[int, List[MatchSpan]] = field(default_factory=dict)
    # Whitespace-free matched strings of every redacted span, used to clear
    # copies of the same text held outside the page content.
    pii_values: List[str] = field(default_factory=list)

    def pages_with_regions(self) -> List[int]:
        return sorted(set(self.text_regions_by_page) | set(self.ocr_regions_by_page))

    def regions_for_page(self, page_index: int) -> List[Rect]:
        """Text and OCR regions of one page as a single list (not de-duplicated)."""
        return list(self.text_regions_by_page.get(page_index, [])) + list(
            self.ocr_regions_by_page.get(page_index, [])
        )


def redact_pdf_document(
    file_bytes: bytes,
    text_units: List[ExtractedTextUnit],
    match_spans: List[MatchSpan],
    image_regions: Optional[List[ImageRegion]] = None,
) -> bytes:
    """Bytes-returning wrapper over `redact_pdf_document_with_report`.

    This wrapper takes no `ocr_text_units`, so an `"ocr:"`-referenced span
    passed through it references an empty list and raises `RedactionError`:
    silently skipping a PII redaction is the one outcome worth failing loudly
    over.
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
    """Redact the spans in a `.pdf` and report how many regions each
    detection path contributed.

    Dispatch is on the `unitRef` prefix (see the module docstring): `"text:"`
    and `"ocr:"` spans become page regions, `"image:"` spans are grouped by
    image region, and any other prefix is skipped without error.

    Args:
        file_bytes: the raw bytes of the source `.pdf`.
        text_units: the exact list originally passed to `match_rules`. Each
            unit referenced by a span must carry `position.pageIndex` and
            `boundingBox`.
        match_spans: the spans from both detection paths as one collection.
        ocr_text_units: the exact OCR unit list originally passed to the
            matcher. Each referenced unit must carry `position.pageIndex` and
            `ocrWords`. `None` means no `"ocr:"` span can resolve.
        image_regions: the exact `ImageRegion` list originally passed to the
            matcher. `None` means no `"image:"` span can resolve.

    Returns:
        The output bytes and the `PdfRedactionReport`. With no regions the
        report counts are all 0.

    Raises:
        RedactionError: if the input cannot be opened as a PDF, or a span
            references an index that is malformed, negative or out of range,
            a unit missing its page or geometry information, or a page outside
            the document.
    """
    try:
        handle = open_pdf(file_bytes)
    except PdfOpenError as exc:
        raise RedactionError("Failed to open PDF for redaction") from exc
    except Exception as exc:
        raise RedactionError("Failed to open PDF for redaction") from exc

    try:
        cache = _GlyphCache(handle)
        resolved = _resolve_regions(
            handle, text_units, match_spans, ocr_text_units, image_regions, cache
        )
        report = _build_report(resolved)
        # Glyphs of every page that will be edited are read from the original
        # document before anything is changed.
        page_glyphs = _collect_original_glyphs(resolved, cache)
        wrote_images = _write_back_images(handle, resolved, image_regions)
        has_regions = bool(resolved.pages_with_regions())
        if has_regions:
            _rewrite_pages(handle, resolved, page_glyphs)
        if has_regions or wrote_images:
            scrub_values = list(resolved.pii_values)
            if scrub_values:
                _scrub(handle, scrub_values)
        output = _save_normalized(handle)
        if has_regions:
            _verify_redacted_output(output, file_bytes, resolved, page_glyphs)
        return PdfRedactionResult(documentBytes=output, report=report)
    except RedactionError:
        raise
    except ContentEditError as exc:
        raise RedactionError(
            "Failed to rewrite page content: {}".format(exc)
        ) from exc
    except Exception as exc:
        raise RedactionError(
            "Failed to redact PDF ({})".format(type(exc).__name__)
        ) from exc
    finally:
        try:
            handle.pikepdf_doc.close()
        except Exception:
            pass


def _scrub(handle: PdfHandle, pii_values: List[str]) -> None:
    try:
        scrub_document_strings(handle.pikepdf_doc, pii_values)
    except Exception as exc:
        raise RedactionError(
            "Failed to scrub document strings ({})".format(type(exc).__name__)
        ) from exc


# Largest accepted drift, in points, of a retained glyph's rectangle edge, and
# the inset applied to a region when looking for characters under it.
_VERIFY_TOLERANCE_PT = 0.01


def _verify_redacted_output(
    output_bytes: bytes,
    source_bytes: bytes,
    resolved: _ResolvedRegions,
    page_glyphs: Dict[int, PageGlyphs],
) -> None:
    """Check the saved bytes before they are returned; fail closed.

    Three independent checks run on the output:

    * the page's glyph traversal (the one that located the text) finds no
      glyph under a region, and the glyphs that were not under a region are
      exactly the ones the source page had, same Unicode and same order;
    * PDFium, which reads content streams independently, finds no character
      centred inside a region, and every character it reads from a retained
      glyph sits where the same character sat in the source (edges within
      0.01 pt). A line-end hyphen that PDFium reads as U+FFFE in one
      document and as "-" in the other is treated as the same character.
      Positions are judged by PDFium as well as by the traversal, which adds
      the character spacing after every glyph and so tracks the drawn
      positions of the glyphs that follow a deleted one;
    * the text of every page, without whitespace, holds none of the redacted
      values.

    Known limitation: the traversal (like the PDF specification) lets a form
    XObject inherit the horizontal scaling (Tz) of the page content that
    invokes it, whereas PDFium does not. For such a page the two readers
    place glyphs differently, the independent-reader check cannot match the
    retained characters, and the page is rejected with `RedactionError`
    instead of being returned. This fails closed: nothing is leaked.

    Any failure, including a failure of the check itself, raises
    `RedactionError`. Messages name a page and a reason category only, never
    page text or redacted values.
    """
    try:
        output_pages = collect_glyphs_from_bytes(output_bytes)
        _verify_glyph_traversal(output_pages, resolved, page_glyphs)
        _verify_independent_reader(
            output_bytes, source_bytes, output_pages, resolved, page_glyphs
        )
        _verify_no_residual_values(output_pages, resolved.pii_values)
    except RedactionError:
        raise
    except Exception as exc:
        raise RedactionError(
            "Failed to verify redacted PDF ({})".format(type(exc).__name__)
        ) from exc


def _verify_glyph_traversal(
    output_pages: List[PageGlyphs],
    resolved: _ResolvedRegions,
    page_glyphs: Dict[int, PageGlyphs],
) -> None:
    for page_index in resolved.pages_with_regions():
        if page_index >= len(output_pages):
            raise RedactionError(
                "Redacted PDF lost page {}".format(page_index)
            )
        regions = resolved.regions_for_page(page_index)
        produced = output_pages[page_index].glyphs
        if any(
            rects_intersect(glyph.raw_rect, region)
            for glyph in produced
            for region in regions
        ):
            raise RedactionError(
                "Residual text under a redaction region on page {}".format(
                    page_index
                )
            )
        expected = [
            glyph
            for glyph in page_glyphs[page_index].glyphs
            if not any(rects_intersect(glyph.raw_rect, r) for r in regions)
        ]
        if len(produced) != len(expected) or any(
            new.text != old.text for old, new in zip(expected, produced)
        ):
            raise RedactionError(
                "Retained text changed on page {}".format(page_index)
            )


def _verify_independent_reader(
    output_bytes: bytes,
    source_bytes: bytes,
    output_pages: List[PageGlyphs],
    resolved: _ResolvedRegions,
    page_glyphs: Dict[int, PageGlyphs],
) -> None:
    """PDFium cross-check of the output (see `_verify_redacted_output`).

    PDFium merges identical characters drawn over each other (the "fake
    bold" trick) into one character, so the source can report fewer boxes
    than it has glyphs. Once the glyphs they were merged with are deleted,
    a retained one surfaces in the output with no source box of its own. Such
    a character is accepted only if its centre lies inside a retained source
    traversal glyph with the same text; anything else is still rejected.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    from agent.tools.pdf_geometry import user_to_page_rect

    inset = _VERIFY_TOLERANCE_PT
    document = pdfium.PdfDocument(output_bytes)
    source = pdfium.PdfDocument(source_bytes)
    try:
        for page_index in resolved.pages_with_regions():
            geometry = output_pages[page_index].geometry
            shrunk = [
                (r[0] + inset, r[1] + inset, r[2] - inset, r[3] - inset)
                for r in resolved.regions_for_page(page_index)
            ]
            regions = resolved.regions_for_page(page_index)
            retained = [
                glyph
                for glyph in page_glyphs[page_index].glyphs
                if not any(rects_intersect(glyph.raw_rect, r) for r in regions)
            ]
            page = document[page_index]
            try:
                textpage = page.get_textpage()
                try:
                    unmatched = _source_char_boxes(source, page_index)
                    for char_index in range(textpage.count_chars()):
                        if pdfium_raw.FPDFText_IsGenerated(textpage, char_index):
                            continue
                        # Char boxes are in unrotated user space.
                        text = _normalise_reader_text(
                            textpage.get_text_range(char_index, 1)
                        )
                        left, bottom, right, top = textpage.get_charbox(char_index)
                        box = user_to_page_rect(
                            geometry, (left, bottom, right, top)
                        )
                        cx = (box[0] + box[2]) / 2.0
                        cy = (box[1] + box[3]) / 2.0
                        if not _take_matching_box(
                            unmatched, text, (left, bottom, right, top)
                        ) and not any(
                            glyph.text == text
                            and glyph.raw_rect[0] <= cx <= glyph.raw_rect[2]
                            and glyph.raw_rect[1] <= cy <= glyph.raw_rect[3]
                            for glyph in retained
                        ):
                            raise RedactionError(
                                "Retained text moved on page {} (independent "
                                "reader)".format(page_index)
                            )
                        if any(
                            r[0] <= cx <= r[2] and r[1] <= cy <= r[3]
                            for r in shrunk
                        ):
                            raise RedactionError(
                                "Residual text under a redaction region on "
                                "page {} (independent reader)".format(page_index)
                            )
                finally:
                    textpage.close()
            finally:
                page.close()
    finally:
        source.close()
        document.close()


def _source_char_boxes(source, page_index: int) -> Dict[str, List[Tuple[float, ...]]]:
    """Boxes of the non-generated characters PDFium reads from a source
    page, grouped by Unicode text."""
    import pypdfium2.raw as pdfium_raw

    boxes: Dict[str, List[Tuple[float, ...]]] = {}
    page = source[page_index]
    try:
        textpage = page.get_textpage()
        try:
            for char_index in range(textpage.count_chars()):
                if pdfium_raw.FPDFText_IsGenerated(textpage, char_index):
                    continue
                boxes.setdefault(
                    _normalise_reader_text(textpage.get_text_range(char_index, 1)),
                    [],
                ).append(
                    tuple(textpage.get_charbox(char_index))
                )
        finally:
            textpage.close()
    finally:
        page.close()
    return boxes


def _normalise_reader_text(text: str) -> str:
    """PDFium reports a line-end hyphen as U+FFFE or as "-" depending on the
    characters around it, so the two are treated as the same text."""
    return "-" if text == "\ufffe" else text


def _take_matching_box(
    unmatched: Dict[str, List[Tuple[float, ...]]],
    text: str,
    box: Tuple[float, ...],
) -> bool:
    """Consume a source box with the same text and edges within tolerance."""
    candidates = unmatched.get(text, [])
    for position, candidate in enumerate(candidates):
        if all(abs(a - b) <= _VERIFY_TOLERANCE_PT for a, b in zip(candidate, box)):
            del candidates[position]
            return True
    return False


def _verify_no_residual_values(
    output_pages: List[PageGlyphs], pii_values: List[str]
) -> None:
    """Fail if any redacted value is still present in the output text.

    Each page's whitespace-free text is checked separately. A redacted value is
    matched within a single text unit (one page), so it can never legitimately
    span a page boundary; joining pages would create false positives from
    leftover characters at the end of one page and the start of the next. The
    check stays strict per page: every value must be absent from every page.
    """
    for page in output_pages:
        text = "".join(ch for ch in page.text if not ch.isspace())
        if any(value in text for value in pii_values):
            raise RedactionError(
                "A redacted value is still present in the output text"
            )


_SOURCE_LOCATION = re.compile(r"^page:(\d+):xref:(\d+)$")


def _collect_original_glyphs(
    resolved: _ResolvedRegions, cache: _GlyphCache
) -> Dict[int, PageGlyphs]:
    glyphs: Dict[int, PageGlyphs] = {}
    for page_index in resolved.pages_with_regions():
        page = cache.get(page_index)
        if page is None:
            raise RedactionError(
                "Cannot read the content of page {} to remove redacted text".format(
                    page_index
                )
            )
        glyphs[page_index] = page
    return glyphs


def _write_back_images(
    handle: PdfHandle,
    resolved: _ResolvedRegions,
    image_regions: Optional[List[ImageRegion]],
) -> bool:
    """Redact each referenced image once, in ascending index order, and store
    the result in the document. Returns whether any image was written."""
    wrote = False
    for index in sorted(resolved.image_spans_by_region):
        label = "image:{}".format(index)
        region = (image_regions or [])[index]
        if not region.imageBytes:
            raise RedactionError("{} has no image bytes to redact".format(label))
        match = _SOURCE_LOCATION.match(region.sourceLocation or "")
        if match is None:
            raise RedactionError(
                "{} has an unrecognized source location".format(label)
            )
        page_index, xref = int(match.group(1)), int(match.group(2))
        if page_index >= handle.page_count:
            raise RedactionError(
                "{} references a page outside the document".format(label)
            )
        try:
            new_bytes = redact_image_bytes(
                region.imageBytes, region, resolved.image_spans_by_region[index]
            )
        except ImageRedactionError as exc:
            raise RedactionError(
                "Failed to redact {}: {}".format(label, type(exc).__name__)
            ) from exc
        try:
            write_back_image(handle.pikepdf_doc, xref, new_bytes)
        except ImageWriteBackError as exc:
            raise RedactionError(
                "Failed to write back {}: {}".format(label, exc)
            ) from exc
        except Exception as exc:
            raise RedactionError(
                "Failed to write back {} ({})".format(label, type(exc).__name__)
            ) from exc
        wrote = True
    return wrote


def _rewrite_pages(
    handle: PdfHandle,
    resolved: _ResolvedRegions,
    page_glyphs: Dict[int, PageGlyphs],
) -> None:
    """Remove the text under each page's regions, then cover them.

    The text-layer and OCR regions of a page are handled as one list and are
    not de-duplicated. Glyphs are deleted before the boxes are drawn because
    drawing adds content that would shift the operator ordinals.
    """
    pdf = handle.pikepdf_doc
    for page_index in resolved.pages_with_regions():
        regions = resolved.regions_for_page(page_index)
        glyphs = page_glyphs[page_index]
        plan = plan_deletions(glyphs, regions)
        apply_page_plan(pdf, page_index, plan, glyphs)
        geom = page_geometry_of(pdf, page_index)
        remove_intersecting_annotations(pdf, page_index, geom, regions)
        draw_black_boxes(pdf, page_index, geom, regions)


def _save_normalized(handle: PdfHandle) -> bytes:
    """Serialize the document as a complete, freshly written file."""
    import pikepdf

    buffer = io.BytesIO()
    try:
        handle.pikepdf_doc.remove_unreferenced_resources()
        handle.pikepdf_doc.save(
            buffer,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            encryption=False,
        )
    except Exception as exc:
        raise RedactionError("Failed to save redacted PDF") from exc
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Span -> region resolution
# ---------------------------------------------------------------------------


class _GlyphCache:
    """Per-page cache of `PageGlyphs`, so a page is traversed at most once
    however many spans land on it. A page that cannot be read is cached as
    `None` and is not retried."""

    def __init__(self, handle: PdfHandle) -> None:
        self._handle = handle
        self._pages: Dict[int, Optional[PageGlyphs]] = {}

    def get(self, page_index: int) -> Optional[PageGlyphs]:
        if page_index not in self._pages:
            try:
                self._pages[page_index] = collect_page_glyphs(
                    self._handle, page_index
                )
            except PageReadError:
                self._pages[page_index] = None
            except Exception:
                # Any other failure to read the page's characters is treated
                # the same way: the caller falls back to an estimate.
                self._pages[page_index] = None
        return self._pages[page_index]


def _resolve_regions(
    handle: PdfHandle,
    text_units: List[ExtractedTextUnit],
    match_spans: List[MatchSpan],
    ocr_text_units: Optional[List[ExtractedTextUnit]],
    image_regions: Optional[List[ImageRegion]],
    cache: _GlyphCache,
) -> _ResolvedRegions:
    resolved = _ResolvedRegions()

    for span in match_spans:
        if span.unitRef.startswith(TEXT_UNIT_REF_PREFIX):
            unit, page_index = _resolve_text_unit(handle, text_units, span)
            _add_pii(resolved, unit.text[span.startOffset : span.endOffset])
            resolved.text_regions_by_page.setdefault(page_index, []).extend(
                _regions_for_text_span(unit, span, cache.get(page_index))
            )
        elif span.unitRef.startswith(OCR_UNIT_REF_PREFIX):
            unit, page_index = _resolve_ocr_unit(handle, ocr_text_units, span)
            _add_pii(resolved, unit.text[span.startOffset : span.endOffset])
            resolved.ocr_regions_by_page.setdefault(page_index, []).extend(
                _regions_for_ocr_span(handle, unit, span, page_index)
            )
        elif span.unitRef.startswith(IMAGE_UNIT_REF_PREFIX):
            region_index = _parse_ref_index(span)
            _check_image_region_index(image_regions, region_index)
            recognized = (image_regions or [])[region_index].recognizedText
            if recognized:
                _add_pii(resolved, recognized[span.startOffset : span.endOffset])
            resolved.image_spans_by_region.setdefault(region_index, []).append(span)
        # Any other namespace is out of scope and is skipped.

    return resolved


def _add_pii(resolved: _ResolvedRegions, text: Optional[str]) -> None:
    squeezed = "".join(ch for ch in (text or "") if not ch.isspace())
    if squeezed:
        resolved.pii_values.append(squeezed)


def _parse_ref_index(span: MatchSpan) -> int:
    """The 0-based index in `"<prefix>:{index}"`; a malformed ref is an error."""
    try:
        return int(span.unitRef.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise RedactionError(
            "Malformed unitRef: {!r}".format(span.unitRef)
        ) from exc


def _check_page_index(handle: PdfHandle, page_index: int) -> None:
    if page_index < 0 or page_index >= handle.page_count:
        raise RedactionError(
            "Match references out-of-range page index {}".format(page_index)
        )


def _check_image_region_index(
    image_regions: Optional[List[ImageRegion]], region_index: int
) -> None:
    count = len(image_regions or [])
    if region_index < 0 or region_index >= count:
        raise RedactionError(
            "MatchSpan unitRef image:{} references an out-of-range image "
            "region index (image_regions has {} entries); image_regions must "
            "be the same list/order originally passed to match_rules.".format(
                region_index, count
            )
        )


def _resolve_text_unit(
    handle: PdfHandle, text_units: List[ExtractedTextUnit], span: MatchSpan
) -> Tuple[ExtractedTextUnit, int]:
    """The unit and page a `"text:{index}"` span points at, validated."""
    index = _parse_ref_index(span)
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
    if page_index is None or unit.boundingBox is None:
        raise RedactionError(
            "Text unit at index {} is missing pageIndex/boundingBox".format(index)
        )
    _check_page_index(handle, page_index)
    return unit, page_index


def _resolve_ocr_unit(
    handle: PdfHandle,
    ocr_text_units: Optional[List[ExtractedTextUnit]],
    span: MatchSpan,
) -> Tuple[ExtractedTextUnit, int]:
    """The OCR unit and page an `"ocr:{index}"` span points at, validated.

    A page with no recognized words carries an empty `ocrWords` list; `None`
    means the unit was not produced by the OCR path.
    """
    units = ocr_text_units or []
    index = _parse_ref_index(span)
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
            "OCR text unit at index {} is missing pageIndex/ocrWords".format(index)
        )
    _check_page_index(handle, page_index)
    return unit, page_index


def _regions_for_text_span(
    unit: ExtractedTextUnit, span: MatchSpan, page: Optional[PageGlyphs]
) -> List[Rect]:
    """Regions for a `"text:"` span: one per line of the matched characters,
    or a single proportional estimate when the characters cannot be used."""
    if page is not None and 0 <= span.startOffset < span.endOffset <= len(page.chars):
        regions = _line_regions(page, span.startOffset, span.endOffset)
        if regions:
            return regions
    return [_proportional_region(unit, span)]


def _line_regions(page: PageGlyphs, start: int, end: int) -> List[Rect]:
    """Minimal bounding rectangle of the characters in `[start, end)` per
    `line_id`, in order of first appearance. Characters without a rectangle
    are skipped."""
    boxes: Dict[int, Rect] = {}
    for char in page.chars[start:end]:
        rect = char.rect
        if rect is None:
            continue
        current = boxes.get(char.line_id)
        if current is None:
            boxes[char.line_id] = rect
        else:
            boxes[char.line_id] = (
                min(current[0], rect[0]),
                min(current[1], rect[1]),
                max(current[2], rect[2]),
                max(current[3], rect[3]),
            )
    # dicts keep insertion order, which is the order of first appearance.
    return list(boxes.values())


def _proportional_region(unit: ExtractedTextUnit, span: MatchSpan) -> Rect:
    """Estimate a region from the unit's `boundingBox` by the span's share of
    the text length."""
    box = unit.boundingBox
    top = box.top
    bottom = box.top + box.height
    text_length = len(unit.text)
    if text_length == 0:
        return (box.left, top, box.left + box.width, bottom)
    start_ratio = min(1.0, max(0.0, span.startOffset / text_length))
    end_ratio = min(1.0, max(0.0, span.endOffset / text_length))
    return (
        box.left + start_ratio * box.width,
        top,
        box.left + end_ratio * box.width,
        bottom,
    )


def _regions_for_ocr_span(
    handle: PdfHandle, unit: ExtractedTextUnit, span: MatchSpan, page_index: int
) -> List[Rect]:
    """Whole-word regions for an `"ocr:"` span, in `ocrWords` order.

    Both ranges are half-open, so a word that merely abuts the span shares no
    character with it and contributes nothing. Separators between words belong
    to no word, so a span covering only separators resolves to no region, which
    is legitimate.
    """
    words = [
        word
        for word in unit.ocrWords or []
        if word.startOffset < span.endOffset and span.startOffset < word.endOffset
    ]
    if not words:
        return []
    if unit.boundingBox is not None:
        size = (unit.boundingBox.width, unit.boundingBox.height)
    else:
        geometry = _page_geometry(page_index, handle.pdfminer_pages[page_index])
        size = (geometry.width, geometry.height)
    return [normalized_box_to_page_rect(size, word.boundingBox) for word in words]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _build_report(resolved: _ResolvedRegions) -> PdfRedactionReport:
    return PdfRedactionReport(
        textLayerRegions=sum(len(r) for r in resolved.text_regions_by_page.values()),
        ocrRegions=sum(len(r) for r in resolved.ocr_regions_by_page.values()),
        agreedRegions=_count_agreed_regions(
            resolved.text_regions_by_page, resolved.ocr_regions_by_page
        ),
    )


def _count_agreed_regions(
    text_regions_by_page: Dict[int, List[Rect]],
    ocr_regions_by_page: Dict[int, List[Rect]],
) -> int:
    """OCR regions that intersect at least one text-layer region on the same
    page. Counted from the OCR side, so the result never exceeds the number of
    OCR regions. The two paths measure the same glyphs through different
    pipelines, so their rectangles are near-identical but never bit-identical;
    intersection (not equality) is the right test, and abutting edges are not
    agreement."""
    agreed = 0
    for page_index, ocr_regions in ocr_regions_by_page.items():
        text_regions = text_regions_by_page.get(page_index)
        if not text_regions:
            continue
        for ocr_region in ocr_regions:
            if any(rects_intersect(ocr_region, t) for t in text_regions):
                agreed += 1
    return agreed
