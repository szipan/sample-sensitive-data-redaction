r"""`.pdf` content extraction (the `.pdf` half of the `extract_document_content`
tool).

This module implements only the `.pdf` extractor, under the function name
`extract_pdf_content`. The extension-dispatching wrapper that chooses between
this function and the `.docx` extractor lives elsewhere.

The document is opened with `pdf_glyphs.open_pdf` and each page is walked in
document order, producing:

- One `ExtractedTextUnit` (zone="body") per page whose text is not blank,
  with `position.pageIndex` and `boundingBox` set to the visible page
  rectangle in Page_Coordinates, `(0, 0, width, height)`.
- One placeholder `ImageRegion` per image XObject the page references, with
  `boundingBox` taken from the first placement of the image (`None` when the
  image is never drawn) and `recognizedText` left `None` for the image text
  recognition step to fill in later.
- Structural metadata (page count, page order) used by `verify_structure` to
  confirm the redacted document has the same page count and page order as
  the source.

Text source: `pdf_glyphs.collect_page_glyphs`
---------------------------------------------

The page text is the concatenation of every glyph that
`collect_page_glyphs` reports, with no separators of any kind (no newline,
no space). This is deliberate and load-bearing:

- `redaction_pdf.py` maps a match's `startOffset` / `endOffset` back to page
  rectangles using the very same traversal. Extraction and redaction share
  one character traversal, so offset `i` in `ExtractedTextUnit.text` is
  character `i` of that traversal by construction (the Character_Offset_Mapping),
  with no offset-translation function in between.
- PII that the PDF renders across a line break becomes contiguous in the
  extracted text: a social-security number laid out as `123-45-` / `6789`
  on two lines reads `123-45-6789`, so both the regex rules in `matching.py`
  and Bedrock Guardrails can match it. A synthetic newline in the middle
  would stop `\d{3}-\d{2}-\d{4}` from matching and would make a reported
  match impossible to locate in the text.
  Where the PDF does render a space at a wrap point, that space is a real
  glyph and is preserved.
- Nothing is filtered inside a page. Only a page whose whole text is empty or
  whitespace-only emits no unit, which cannot shift any character index.

Zone choice: a PDF page is a sequence of positioned glyphs with no header,
footer or table structure, so every text unit is classified `zone="body"`.

Any failure to open or read the document raises `ExtractionError`; a partial
result is never returned.
"""
from dataclasses import dataclass, field
from typing import List, Optional

import pikepdf

from agent.tools.pdf_glyphs import (
    PageReadError,
    PdfOpenError,
    PdfPasswordError,
    collect_page_glyphs,
    open_pdf,
)
from agent.tools.pdf_images import decode_image, iter_page_image_refs
from shared.models import BoundingBox, ExtractedTextUnit, ImageRegion, TextPosition


class ExtractionError(Exception):
    """Raised when a `.pdf` file cannot be parsed (corrupt file, document
    that needs a user password, unreadable page). The Agent orchestration
    catches it."""


@dataclass
class ExtractionResult:
    """Container returned by `extract_pdf_content`.

    `pageCount` and `pageOrder` are the structural metadata used later by
    `verify_structure` to confirm the redacted document has the same page
    count and page order as the source.
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
        The raw bytes of the source `.pdf` document.

    Returns
    -------
    ExtractionResult
        `textUnits` (one per non-blank page, `zone="body"`), `imageRegions`
        (one placeholder per referenced image, `recognizedText` unset, ordered
        by page and then by `iter_page_image_refs` order), and page-count /
        page-order metadata.

    Raises
    ------
    ExtractionError
        If `file_bytes` is not a readable PDF, the document needs a user
        password, or any page cannot be read.
    """
    try:
        handle = open_pdf(file_bytes)
    except PdfPasswordError as exc:
        raise ExtractionError(
            "PDF requires a password and cannot be parsed: {}".format(exc)
        ) from exc
    except PdfOpenError as exc:
        raise ExtractionError("Failed to open PDF: {}".format(exc)) from exc
    except Exception as exc:
        raise ExtractionError("Failed to open PDF: {}".format(exc)) from exc

    try:
        text_units: List[ExtractedTextUnit] = []
        image_regions: List[ImageRegion] = []

        for page_index in range(handle.page_count):
            try:
                page_glyphs = collect_page_glyphs(handle, page_index)
            except PageReadError as exc:
                raise ExtractionError(
                    "Failed to extract text from page {}: {}".format(page_index, exc)
                ) from exc
            except Exception as exc:
                raise ExtractionError(
                    "Failed to extract text from page {}: {}".format(page_index, exc)
                ) from exc

            if page_glyphs.text.strip() != "":
                text_units.append(
                    ExtractedTextUnit(
                        zone="body",
                        text=page_glyphs.text,
                        position=TextPosition(index=0, pageIndex=page_index),
                        boundingBox=BoundingBox(
                            left=0,
                            top=0,
                            width=page_glyphs.geometry.width,
                            height=page_glyphs.geometry.height,
                        ),
                    )
                )

            try:
                refs = iter_page_image_refs(handle, page_index, page_glyphs)
            except Exception as exc:
                raise ExtractionError(
                    "Failed to extract images from page {}: {}".format(page_index, exc)
                ) from exc
            for ref in refs:
                bounding_box: Optional[BoundingBox] = None
                if ref.bounding_box is not None:
                    left, top, right, bottom = ref.bounding_box
                    bounding_box = BoundingBox(
                        left=left,
                        top=top,
                        width=right - left,
                        height=bottom - top,
                    )
                image_regions.append(
                    ImageRegion(
                        zone="image",
                        sourceLocation="page:{}:xref:{}".format(page_index, ref.xref),
                        recognizedText=None,
                        boundingBox=bounding_box,
                        imageBytes=_extract_image_bytes(handle.pikepdf_doc, ref.xref),
                    )
                )

        return ExtractionResult(
            textUnits=text_units,
            imageRegions=image_regions,
            pageCount=handle.page_count,
            pageOrder=list(range(handle.page_count)),
        )
    finally:
        try:
            handle.pikepdf_doc.close()
        except Exception:
            pass


def _extract_image_bytes(pdf: "pikepdf.Pdf", xref: int) -> Optional[bytes]:
    """Return decoded bytes of the image object `xref` that Pillow can open,
    or `None` if they cannot be produced.

    Best effort: a single undecodable image must not break extraction of the
    rest of the document. A region with `None` bytes is skipped by the OCR
    step and never touched by the redactor.
    """
    try:
        return decode_image(pdf, xref)
    except Exception:
        return None
