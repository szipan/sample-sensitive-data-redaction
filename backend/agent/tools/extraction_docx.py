"""`.docx` implementation of the `extract_document_content` Agent tool

Scope note: this module implements the `.docx` extraction logic
ONLY. The `.pdf` equivalent is implemented separately (using `PyMuPDF`) in a
sibling module. The extension-dispatching wrapper lives in
`orchestration.py`:

    def extract_document_content(file_bytes: bytes, extension: str) -> ExtractionResult:
        if extension == ".docx":
            return extract_docx_content(file_bytes)
        if extension == ".pdf":
            return extract_pdf_content(file_bytes)  # implemented in extraction_pdf.py
        raise ExtractionError(f"Unsupported extension: {extension}")

That wrapper is intentionally NOT implemented here to avoid a name/file
collision with the concurrently-developed `.pdf` module; this module only
exposes `extract_docx_content`, which the wrapper will call.

Contract of `extract_document_content`:
    Input:  the source document and its extension (here: raw file bytes,
            already fetched by the orchestration layer)
    Output: `ExtractedTextUnit[]`, `ImageRegion[]` (one per embedded media
            part, carrying `imageBytes` and a `docx:part:{partname}`
            re-embedding handle, with `recognizedText` still to be filled
            in), and page/table structure metadata
    Notes:  for `.docx`, `python-docx` walks the paragraph / table /
            header / footer XML nodes

`ExtractedTextUnit`/`ImageRegion`/`TextPosition` shapes come from
`shared/models.py` and are reused as-is.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from docx import Document
from docx.table import Table

from shared.models import ExtractedTextUnit, ImageRegion, TextPosition


class ExtractionError(Exception):
    """Raised when a Supported Document cannot be parsed for extraction.

    A corrupt file, an encrypted PDF, etc. make `extract_document_content`
    raise `ExtractionError`; the orchestration layer catches it and calls
    `update_job_status(failed)`.
    """


@dataclass
class TableStructureMetadata:
    """Structural metadata for a single table, kept for `verify_structure`
    to later compare source vs. redacted table dimensions.

    Shape is intentionally simple/flat (index + row/column counts) since the
    exact consumer shape for `verify_structure` isn't finalized yet; this is
    a reasonable, documented placeholder for the "every table keeps its row
    and column counts" verification.
    """

    tableIndex: int
    rowCount: int
    colCount: int


@dataclass
class DocumentStructureMetadata:
    """Page/table structural metadata for a `.docx` extraction result.

    `.docx` has no fixed "page" concept at the XML level (page breaks are a
    rendering-time concern), so `pageCount` is left as `None` here; the
    `.pdf` extraction path is expected to populate a real page
    count. `tables` holds one entry per table in document order, giving
    `verify_structure` the per-table row/column counts it needs.
    """

    tables: list[TableStructureMetadata] = field(default_factory=list)
    pageCount: int | None = None


@dataclass
class ExtractionResult:
    """Container returned by `extract_docx_content` (and, informally, the
    shape the `.pdf` extraction path is expected to mirror).

    Field names match the orchestration code: `content.textUnits`,
    `content.imageRegions`.
    """

    textUnits: list[ExtractedTextUnit]
    imageRegions: list[ImageRegion]
    structureMetadata: DocumentStructureMetadata


def extract_docx_content(file_bytes: bytes) -> ExtractionResult:
    """Extract text units, placeholder image regions, and structural
    metadata from a `.docx` file's raw bytes.

    Traverses (in this order):
    - Header/footer paragraphs for every section (`zone="header"` /
      `zone="footer"`), which `python-docx` exposes per-`Section` even
      though the underlying XML is often the same header/footer part
      shared across sections.
    - Body paragraphs, in document order (`zone="body"`).
    - Tables, cell by cell (`zone="table"`), with `position.tableIndex`,
      `position.rowIndex`, `position.colIndex` set.

    Image regions: this implementation emits one `ImageRegion` per embedded
    media part (in-image-redaction feature). `.docx` embeds images as package
    parts rather than inline objects tied to a specific paragraph position in
    a way `python-docx`'s public API exposes directly, so the regions carry no
    reading-position/paragraph anchor and `boundingBox` is left `None`
    (in-image redaction works from the image's own pixel dimensions, not
    document geometry). Instead of a paragraph anchor, each region's
    `sourceLocation` is the stable re-embedding handle `docx:part:{partname}`
    (e.g. `docx:part:/word/media/image1.png`) that the `.docx` redactor uses to
    locate and replace the part's bytes. The parts are enumerated once via
    `document.part.package.iter_parts()`, so a media part referenced by several
    `<a:blip>` elements yields exactly one `ImageRegion`.

    Raises:
        ExtractionError: if `file_bytes` cannot be parsed as a `.docx`
            (e.g. corrupted or not a valid OOXML zip).
    """
    try:
        document = Document(io.BytesIO(file_bytes))
    except Exception as exc:  # python-docx raises various underlying errors
        raise ExtractionError(f"Failed to parse .docx content: {exc}") from exc

    text_units: list[ExtractedTextUnit] = []

    # Headers and footers, per section, in section order.
    for section in document.sections:
        for paragraph_index, paragraph in enumerate(section.header.paragraphs):
            text_units.append(
                ExtractedTextUnit(
                    zone="header",
                    text=paragraph.text,
                    position=TextPosition(index=paragraph_index),
                )
            )
        for paragraph_index, paragraph in enumerate(section.footer.paragraphs):
            text_units.append(
                ExtractedTextUnit(
                    zone="footer",
                    text=paragraph.text,
                    position=TextPosition(index=paragraph_index),
                )
            )

    # Body paragraphs, in document order.
    for paragraph_index, paragraph in enumerate(document.paragraphs):
        text_units.append(
            ExtractedTextUnit(
                zone="body",
                text=paragraph.text,
                position=TextPosition(index=paragraph_index),
            )
        )

    # Tables, cell by cell.
    table_metadata: list[TableStructureMetadata] = []
    for table_index, table in enumerate(document.tables):
        table_metadata.append(_table_structure_metadata(table_index, table))
        for row_index, row in enumerate(table.rows):
            for col_index, cell in enumerate(row.cells):
                text_units.append(
                    ExtractedTextUnit(
                        zone="table",
                        text=cell.text,
                        position=TextPosition(
                            tableIndex=table_index,
                            rowIndex=row_index,
                            colIndex=col_index,
                        ),
                    )
                )

    # Embedded images: one ImageRegion per media part, enumerated once from
    # the package so a part reused by several <a:blip> references is emitted a
    # single time. No paragraph anchor is needed for redaction (which replaces
    # the part's bytes), so boundingBox stays None and sourceLocation carries
    # the stable re-embed handle "docx:part:{partname}".
    image_regions: list[ImageRegion] = []
    for part in document.part.package.iter_parts():
        content_type = getattr(part, "content_type", "") or ""
        partname = str(getattr(part, "partname", ""))
        if content_type.startswith("image/") or partname.startswith("/word/media/"):
            image_regions.append(
                ImageRegion(
                    zone="image",
                    sourceLocation=f"docx:part:{partname}",
                    imageBytes=part.blob,
                    boundingBox=None,
                )
            )

    structure_metadata = DocumentStructureMetadata(
        tables=table_metadata,
        pageCount=None,
    )

    return ExtractionResult(
        textUnits=text_units,
        imageRegions=image_regions,
        structureMetadata=structure_metadata,
    )


def _table_structure_metadata(table_index: int, table: Table) -> TableStructureMetadata:
    row_count = len(table.rows)
    col_count = len(table.columns)
    return TableStructureMetadata(
        tableIndex=table_index,
        rowCount=row_count,
        colCount=col_count,
    )
