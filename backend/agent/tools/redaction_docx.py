"""`.docx` implementation of the `redact_document` Agent tool.

Contract of `redact_document` (`.docx` half):

    Input:  the source document and the `MatchSpan[]`
    Output: the Redacted Document bytes
    Notes:  only the run / table-cell XML nodes that contain a match are
            modified: the matched text is covered with a solid redaction and
            cleared, and every other node is preserved as is

Scope note: this module implements the `.docx` redaction logic
ONLY. The `.pdf` equivalent (using content-stream
editing via pikepdf, with pdfminer.six text extraction and pypdfium2 rasterization) is implemented separately in a
sibling module. `verify_structure` is also out of scope here.

--------------------------------------------------------------------------
CRITICAL PRECONDITION: `text_units` identity with the `match_rules` input
--------------------------------------------------------------------------

`redact_docx_document`'s `text_units` argument MUST be the *exact same
list* (same object identity is not required, but same order and same
indices) that was passed as the `text_units` argument to `match_rules`
(`agent/tools/matching.py`) when it produced `match_spans`.

This is because `MatchSpan.unitRef` values look like `"text:3"`, where `3`
is a *positional index into the `text_units` list `match_rules` was
called with* — not a stable id, not `ExtractedTextUnit.position.index`,
and not derivable from the `ExtractedTextUnit` itself. If a caller passes
a `text_units` list that has been reordered, filtered, or rebuilt (e.g. by
re-running `extract_docx_content` and getting a differently-ordered list)
relative to the list used to produce `match_spans`, this function will
silently redact the wrong text units. Callers (the Agent's orchestration
layer) MUST thread the same `text_units` list through
`extract_document_content -> match_rules -> redact_document` unchanged.

Similarly, `file_bytes` MUST be the *same source document bytes* that
`text_units` was extracted from (same paragraphs/tables/sections in the
same order), since navigation from an `ExtractedTextUnit` back to a
`python-docx` object (a `Paragraph` or a `_Cell`) is done by re-walking the
freshly-reopened document with the position information captured at
extraction time.

--------------------------------------------------------------------------
"Solid Redaction Block" technique for `.docx`
--------------------------------------------------------------------------

The design leaves the solid graphic overlay implementation
latitude for `.docx`, noting that `.docx` runs (`<w:r>`) don't expose a
high-level API suited to inline "black box over specific characters".
`python-docx` has no API to draw an actual opaque shape/rectangle
positioned over an arbitrary character range — that would require raw
OOXML drawing/shape XML anchored at a precise glyph position, which is far
outside what `python-docx`'s object model exposes and is not needed to
satisfy the correctness guarantees (the essential ones:
(a) none of the original matched characters remain, (b) the redacted
region is visually distinct/solid-looking, (c) everything else is
unchanged).

The technique implemented here, per matched sub-range:

1. Locate every `<w:r>` run that overlaps the matched character range
   (a match may span multiple runs, and multiple non-adjacent matches may
   fall within a single run — `match_rules` only merges
   matches that overlap or are *adjacent*, not matches with a gap between
   them).
2. For a run that overlaps at least one matched sub-range, split it into
   an ordered sequence of sibling `<w:r>` runs: unmatched text keeps its
   own run (a clone of the original run, so all run-level formatting such
   as bold/italic/font is preserved byte-for-byte), and each matched
   sub-range becomes its own run whose text is replaced with a fixed
   placeholder of solid block characters (`"█"`, `U+2588 FULL BLOCK`,
   repeated to the same character count as the redacted text — so the
   run's rendered width visually indicates roughly how much text was
   redacted, without leaking the actual content or its length in a
   precise/decodable way) and whose highlight color is set to
   `WD_COLOR_INDEX.BLACK` (`<w:highlight w:val="black"/>`) plus black font
   color, so it renders as a solid black block in any `.docx` viewer.
3. Runs that contain no match are left **completely untouched** — not
   cloned, not re-parented, not touched in any way — so only the runs and cells that contain a
   match are modified and every other XML node is preserved as is.

Because the placeholder text has the exact same character count as the
text it replaces, splitting/rebuilding runs never changes the total
character count of the paragraph/cell, which keeps every *other* match's
precomputed character offsets valid even when multiple matches are
redacted within the same paragraph/cell in one pass (no offset
re-computation needed between edits).
"""
from __future__ import annotations

import io
from copy import deepcopy
from typing import List, Optional

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.shared import RGBColor
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from agent.tools import redaction_image
from shared.models import ExtractedTextUnit, ImageRegion, MatchSpan

REDACTION_BLOCK_CHAR = "█"
"""U+2588 FULL BLOCK — the fixed placeholder character used to build the
Solid Redaction Block's replacement text (repeated once per redacted
character; see module docstring)."""


class RedactionError(Exception):
    """Raised when a `.docx` document or a `MatchSpan`'s referenced text
    unit cannot be redacted (e.g. corrupted `.docx` bytes, or a `MatchSpan`/
    `text_units` pairing that does not satisfy this module's documented
    precondition). Mirrors `extraction_docx.ExtractionError`'s role in the
    Agent's error-handling pipeline (design.md Error Handling table)."""


def redact_docx_document(
    file_bytes: bytes,
    text_units: List[ExtractedTextUnit],
    match_spans: List[MatchSpan],
    image_regions: Optional[List[ImageRegion]] = None,
) -> bytes:
    """Produce a Redacted Document from `.docx` `file_bytes` by covering
    every matched character span with a Solid Redaction Block, leaving
    everything else in the document unchanged.

    In-image redaction: when `image_regions` is supplied and `match_spans` includes
    `image:`-namespaced spans, this function ALSO redacts the detected PII on
    the embedded images themselves and re-embeds the modified image bytes,
    after the text-run redaction and before the single `document.save()`. When
    `image_regions` is `None` (the default) the function behaves exactly as it
    did before this feature — text-run redaction only — so every existing
    caller is unaffected.

    Args:
        file_bytes: raw bytes of the source `.docx` document that
            `text_units` was extracted from (see module docstring's
            precondition on identical source document + `text_units`).
        text_units: the exact same list (same order/indices) passed to
            `match_rules` when it produced `match_spans` — see the
            module-level "CRITICAL PRECONDITION" docstring above.
        match_spans: match spans to redact, as produced by `match_rules`
            and the downstream detection sources. Spans whose
            `unitRef` starts with `"text:"` drive the text-run redaction;
            spans whose `unitRef` starts with `"image:"` drive in-image
            redaction against `image_regions`. Spans with any other `unitRef`
            prefix are ignored here.
        image_regions: the `ImageRegion`s emitted by `.docx` extraction
            (each carrying `imageBytes` and a `docx:part:{partname}` re-embed
            handle in `sourceLocation`), threaded from orchestration. An
            `image:` span's `unitRef` (`"image:{index}"`) is a 0-based index
            into this list. `None` (the default) means "no image redaction",
            matching pre-feature behavior.

    Returns:
        The redacted `.docx` file's raw bytes.

    Raises:
        RedactionError: if `file_bytes` cannot be parsed as a `.docx`; if a
            `MatchSpan`'s `unitRef` index cannot be resolved against
            `text_units`/`image_regions` or the reopened document (e.g. the
            precondition above was violated); or if an embedded image
            carrying `image:` spans cannot be decoded/drawn/re-encoded or its
            media part cannot be located for re-embedding.
    """
    try:
        document = Document(io.BytesIO(file_bytes))
    except Exception as exc:  # python-docx raises various underlying errors
        raise RedactionError(f"Failed to parse .docx content for redaction: {exc}") from exc

    spans_by_unit_index: dict[int, List[MatchSpan]] = {}
    for span in match_spans:
        if not span.unitRef.startswith("text:"):
            continue
        try:
            unit_index = int(span.unitRef[len("text:") :])
        except ValueError as exc:
            raise RedactionError(f"Malformed unitRef, expected 'text:<int>': {span.unitRef!r}") from exc
        spans_by_unit_index.setdefault(unit_index, []).append(span)

    for unit_index, spans_for_unit in spans_by_unit_index.items():
        if unit_index < 0 or unit_index >= len(text_units):
            raise RedactionError(
                f"MatchSpan references text_units[{unit_index}], but text_units has "
                f"{len(text_units)} entries. Ensure the same text_units list passed to "
                "match_rules is passed to redact_docx_document (see module precondition)."
            )
        unit = text_units[unit_index]
        paragraphs = _resolve_paragraphs_for_unit(document, unit)
        _redact_paragraphs(paragraphs, spans_for_unit)

    # In-image redaction: after the text-run redaction and before the single
    # save, cover detected in-image PII on the embedded images themselves and
    # replace the media part's bytes in place (in-image-redaction feature).
    _redact_image_regions(document, match_spans, image_regions)

    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _redact_image_regions(
    document: Document,
    match_spans: List[MatchSpan],
    image_regions: Optional[List[ImageRegion]],
) -> None:
    """Redact each embedded image referenced by an `image:` span and replace
    its media part's bytes in place on the reopened `document` (Requirements
    1.1, 1.2, 7.3).

    `image:` spans are grouped by their region index (`unitRef` =
    `"image:{index}"`, a 0-based index into `image_regions`). For each
    referenced region the detected PII is covered — targeted rectangles when
    reliable geometry is available, otherwise the whole-image fallback —
    entirely inside `redaction_image.redact_image_bytes`; here we only resolve
    the region, convert any `ImageRedactionError` into `RedactionError`,
    and re-embed the result by the `docx:part:` handle.

    When there are no `image:` spans this is a no-op, so a caller that passes
    no `image_regions` (or a document with no image PII) sees exactly the
    pre-feature behavior. Text-run redaction and its "runs with no match are
    untouched" guarantee are unaffected — this runs after
    it and only mutates media-part blobs.
    """
    spans_by_region_index: dict[int, List[MatchSpan]] = {}
    for span in match_spans:
        if not span.unitRef.startswith("image:"):
            continue
        try:
            region_index = int(span.unitRef[len("image:") :])
        except ValueError as exc:
            raise RedactionError(
                f"Malformed unitRef, expected 'image:<int>': {span.unitRef!r}"
            ) from exc
        spans_by_region_index.setdefault(region_index, []).append(span)

    if not spans_by_region_index:
        return

    regions = image_regions or []
    for region_index, spans_for_region in spans_by_region_index.items():
        if region_index < 0 or region_index >= len(regions):
            raise RedactionError(
                f"MatchSpan references image_regions[{region_index}], but image_regions "
                f"has {len(regions)} entries. Ensure the same image_regions list produced "
                "during extraction is passed to redact_docx_document."
            )
        region = regions[region_index]
        if not region.imageBytes:
            raise RedactionError(
                f"MatchSpan references image_regions[{region_index}] "
                f"({region.sourceLocation!r}), which has no imageBytes to redact."
            )
        try:
            new_bytes = redaction_image.redact_image_bytes(
                region.imageBytes, region, spans_for_region
            )
        except redaction_image.ImageRedactionError as exc:
            raise RedactionError(
                f"Failed to redact embedded image {region.sourceLocation!r}: {exc}"
            ) from exc
        _reembed_image_part(document, region.sourceLocation, new_bytes)


def _reembed_image_part(document: Document, source_location: str, new_bytes: bytes) -> None:
    """Replace the bytes of the media part identified by `source_location`
    (a `docx:part:{partname}` handle) with `new_bytes`, in place on the
    reopened `document`.

    The partname is recovered by stripping the `docx:part:` prefix (e.g.
    `docx:part:/word/media/image1.png` -> `/word/media/image1.png`) and the
    matching part is found by comparing `str(part.partname)` among
    `document.part.package.iter_parts()`. Its blob is overwritten via the
    private `_blob` attribute — mirroring this module's existing reliance on
    python-docx internals (`run._r`, `run._parent`) where the public API
    exposes no equivalent. Replacing the media part's bytes updates every
    `<a:blip>` that references it, which is safe over-redaction when an image
    is reused.

    Raises:
        RedactionError: if `source_location` is not a `docx:part:` handle, or
            no matching media part exists in the reopened document.
    """
    prefix = "docx:part:"
    if not source_location.startswith(prefix):
        raise RedactionError(
            f"ImageRegion.sourceLocation {source_location!r} is not a "
            f"'{prefix}<partname>' handle; cannot locate the media part to re-embed."
        )
    partname = source_location[len(prefix) :]
    for part in document.part.package.iter_parts():
        if str(getattr(part, "partname", "")) == partname:
            part._blob = new_bytes  # noqa: SLF001 - python-docx has no public setter
            return
    raise RedactionError(
        f"No media part named {partname!r} found in the document to re-embed the "
        f"redacted image (from sourceLocation {source_location!r})."
    )


def _resolve_paragraphs_for_unit(document: Document, unit: ExtractedTextUnit) -> List[Paragraph]:
    """Navigate from an `ExtractedTextUnit`'s `zone`/`position` back to the
    `python-docx` paragraph(s) it was extracted from, mirroring
    `extraction_docx.py`'s traversal exactly.

    Returns a list of one or more `Paragraph` objects: a single-element
    list for `body`/`header`/`footer` zones (one paragraph), or every
    paragraph of the referenced table cell for `zone == "table"` (a cell
    may contain multiple paragraphs; `_Cell.text` joins their `.text`
    values with `"\\n"`, and `extraction_docx.py` uses `cell.text` as the
    unit's text, so redaction must operate over that same joined text).

    Known limitation (inherited from `extraction_docx.py`'s per-section
    paragraph indexing): `extract_docx_content` resets `position.index` to
    0 for each section's header/footer paragraphs, so a `position.index`
    value alone cannot disambiguate *which section* a header/footer
    `ExtractedTextUnit` came from when a document has more than one
    section. This implementation always resolves `header`/`footer`
    navigation against `document.sections[0]`, matching the common
    single-section case (the default for documents produced by
    `python-docx`, and for most real-world `.docx` files). Multi-section
    documents with distinct per-section headers/footers are out of scope
    for this POC.

    Raises:
        RedactionError: if `position` is missing the fields required for
            `unit.zone`, or if the referenced paragraph/table/row/column
            index does not exist in `document`.
    """
    position = unit.position
    try:
        if unit.zone == "body":
            return [document.paragraphs[_require_index(position.index, "body position.index")]]
        if unit.zone == "header":
            index = _require_index(position.index, "header position.index")
            return [document.sections[0].header.paragraphs[index]]
        if unit.zone == "footer":
            index = _require_index(position.index, "footer position.index")
            return [document.sections[0].footer.paragraphs[index]]
        if unit.zone == "table":
            table_index = _require_index(position.tableIndex, "table position.tableIndex")
            row_index = _require_index(position.rowIndex, "table position.rowIndex")
            col_index = _require_index(position.colIndex, "table position.colIndex")
            table = document.tables[table_index]
            cell = table.rows[row_index].cells[col_index]
            return list(cell.paragraphs)
    except IndexError as exc:
        raise RedactionError(
            f"MatchSpan references a {unit.zone} position that does not exist in the "
            f"reopened document ({position!r}): {exc}"
        ) from exc

    raise RedactionError(f"Unsupported ExtractedTextUnit.zone for .docx redaction: {unit.zone!r}")


def _require_index(value: Optional[int], field_description: str) -> int:
    if value is None:
        raise RedactionError(f"Missing required {field_description} for .docx redaction navigation")
    return value


def _redact_paragraphs(paragraphs: List[Paragraph], spans: List[MatchSpan]) -> None:
    """Apply every span in `spans` (all referencing the same text unit,
    whose text is the `"\\n"`-joined concatenation of `paragraphs`' texts —
    see `_resolve_paragraphs_for_unit`) as a Solid Redaction Block over the
    matching runs, leaving every run with no overlapping span completely
    untouched.
    """
    if not spans:
        return

    for run, run_start, run_end in _iter_runs_with_offsets(paragraphs):
        if run_end <= run_start:
            continue  # empty run: nothing to redact, nothing to touch

        local_ranges = _local_redaction_ranges(spans, run_start, run_end)
        if not local_ranges:
            continue  # this run has no matched sub-range: leave it untouched

        _split_run_with_redactions(run, local_ranges)


def _iter_runs_with_offsets(paragraphs: List[Paragraph]):
    """Yield `(run, start, end)` for every run across `paragraphs`, where
    `start`/`end` are character offsets into the paragraphs' `"\\n"`-joined
    text (matching `_Cell.text`'s `"\\n".join(p.text for p in paragraphs)`
    for multi-paragraph cells, and simply `paragraph.text` for a
    single-paragraph list).

    The `"\\n"` separator inserted between paragraphs is virtual (it does
    not belong to any run) and is therefore never itself covered by a
    redaction range.
    """
    offset = 0
    for paragraph_index, paragraph in enumerate(paragraphs):
        if paragraph_index > 0:
            offset += 1  # virtual "\n" joiner between paragraphs
        for run in paragraph.runs:
            run_length = len(run.text)
            yield run, offset, offset + run_length
            offset += run_length


def _local_redaction_ranges(
    spans: List[MatchSpan], run_start: int, run_end: int
) -> List[tuple]:
    """Return the sorted, run-local `(start, end)` sub-ranges of `spans`
    that overlap `[run_start, run_end)`, converted to offsets relative to
    the run's own text (i.e. `0` == the run's first character)."""
    local_ranges: List[tuple] = []
    for span in spans:
        overlap_start = max(span.startOffset, run_start)
        overlap_end = min(span.endOffset, run_end)
        if overlap_start < overlap_end:
            local_ranges.append((overlap_start - run_start, overlap_end - run_start))
    local_ranges.sort()
    return local_ranges


def _split_run_with_redactions(run: Run, local_ranges: List[tuple]) -> None:
    """Replace `run`'s single `<w:r>` element with an ordered sequence of
    sibling `<w:r>` elements: unredacted text keeps the original run's
    text (in its own clone, so formatting is preserved), and each range in
    `local_ranges` becomes its own clone whose text is replaced by a
    same-length run of `REDACTION_BLOCK_CHAR` with black highlighting.
    """
    text = run.text
    paragraph_parent = run._parent  # noqa: SLF001 - python-docx has no public accessor

    pieces: List[tuple] = []  # (slice_text, is_redacted)
    cursor = 0
    for start, end in local_ranges:
        if start > cursor:
            pieces.append((text[cursor:start], False))
        pieces.append((text[start:end], True))
        cursor = end
    if cursor < len(text):
        pieces.append((text[cursor:], False))

    original_element = run._r  # noqa: SLF001 - python-docx has no public accessor
    for slice_text, is_redacted in pieces:
        new_element = deepcopy(original_element)
        new_run = Run(new_element, paragraph_parent)
        if is_redacted:
            new_run.text = REDACTION_BLOCK_CHAR * len(slice_text)
            new_run.font.highlight_color = WD_COLOR_INDEX.BLACK
            new_run.font.color.rgb = RGBColor(0x00, 0x00, 0x00)
        else:
            new_run.text = slice_text
        original_element.addprevious(new_element)

    original_element.getparent().remove(original_element)
