"""Leak tests for PDF redaction.

The output of `redact_pdf_document_with_report` is checked with pypdf, which is
not used by the redactor to read or write PDFs, so a bug shared by the
redactor and its own verification cannot hide a leak here.

The document has two pages:

- page 1: `John Smith` (Latin, `text:`), `张三丰` (CJK with ToUnicode,
  `text:`), `123-45-` / `6789` on two lines with no space at the break
  (`text:`), and the non-PII control string `Contact:` on the same line as
  `John Smith`;
- page 2: `13800001111`, covered ONLY by an `ocr:` span whose `ocrWords`
  come from the pypdfium2 character boxes of the page (no Textract, no network).
"""

import re
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

import pypdf  # plain import: a missing extractor must fail collection, not skip
import pypdfium2
import pytest
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    StreamObject,
    TextStringObject,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT / "tests"))

from agent.tools.extraction_pdf import extract_pdf_content  # noqa: E402
from agent.tools.redaction_pdf import redact_pdf_document_with_report  # noqa: E402
from pdf_fixtures import (  # noqa: E402
    CJK_FONT,
    DocSpec,
    FreeTextAnnot,
    PageSpec,
    TextRun,
    WidgetField,
    build_pdf,
    make_xmp,
)
from shared.models import (  # noqa: E402
    BoundingBox,
    ExtractedTextUnit,
    MatchSpan,
    OcrWord,
    TextPosition,
)

LATIN_PII = "John Smith"
CJK_PII = "张三丰"
SPLIT_PII = "123-45-6789"
OCR_PII = "13800001111"
ALL_PII = [LATIN_PII, CJK_PII, SPLIT_PII, OCR_PII]
CONTROL = "Contact:"

# Regex used to find each PII in the extracted page text; the split value may
# be broken by a line break with no space character.
_PII_PATTERNS = {
    LATIN_PII: re.compile(r"John Smith"),
    CJK_PII: re.compile("张三丰"),
    SPLIT_PII: re.compile(r"123-45-\s*6789"),
}


def _page_runs() -> Tuple[PageSpec, PageSpec]:
    page1 = PageSpec(
        runs=(
            TextRun(text=LATIN_PII, x=72, y=700),
            TextRun(text=CONTROL, x=200, y=700),
            TextRun(text=CJK_PII, x=72, y=650, font=CJK_FONT),
            TextRun(text="123-45-", x=72, y=600),
            TextRun(text="6789", x=72, y=585),
        )
    )
    page2 = PageSpec(runs=(TextRun(text=OCR_PII, x=72, y=700),))
    return page1, page2


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _pypdf_text(data: bytes) -> str:
    import io

    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _text_spans(text_units) -> List[MatchSpan]:
    """`text:` spans for the three text-layer PIIs (never for OCR_PII)."""
    spans = []
    for index, unit in enumerate(text_units):
        for name, pattern in _PII_PATTERNS.items():
            for match in pattern.finditer(unit.text):
                spans.append(
                    MatchSpan(
                        zone=unit.zone,
                        unitRef="text:{}".format(index),
                        startOffset=match.start(),
                        endOffset=match.end(),
                        ruleName="pii",
                    )
                )
    return spans


def _ocr_unit_for_page(pdf_bytes: bytes, page_index: int, word: str) -> ExtractedTextUnit:
    """An `ocr` unit for one word of a page, with `ocrWords` taken from the
    pypdfium2 character boxes normalized to [0, 1] of the page."""
    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        page = pdf[page_index]
        width, height = page.get_size()
        textpage = page.get_textpage()
        full = textpage.get_text_range()
        start = full.find(word)
        assert start >= 0, "word {!r} not on page {}".format(word, page_index)
        boxes = [textpage.get_charbox(i) for i in range(start, start + len(word))]
        left = min(b[0] for b in boxes)
        bottom = min(b[1] for b in boxes)
        right = max(b[2] for b in boxes)
        top = max(b[3] for b in boxes)
        box = BoundingBox(
            left=left / width,
            top=1 - top / height,
            width=(right - left) / width,
            height=(top - bottom) / height,
        )
    finally:
        pdf.close()
    return ExtractedTextUnit(
        zone="ocr",
        text=word,
        position=TextPosition(index=page_index, pageIndex=page_index),
        ocrWords=[OcrWord(text=word, startOffset=0, endOffset=len(word), boundingBox=box)],
    )


def _redact(pdf_bytes: bytes) -> bytes:
    extraction = extract_pdf_content(pdf_bytes)
    text_spans = _text_spans(extraction.textUnits)
    assert len(text_spans) == 3
    # the OCR value must not be covered by any text: span
    page2_units = [
        i for i, u in enumerate(extraction.textUnits) if u.position.pageIndex == 1
    ]
    assert all(
        s.unitRef != "text:{}".format(i) for s in text_spans for i in page2_units
    )
    ocr_unit = _ocr_unit_for_page(pdf_bytes, 1, OCR_PII)
    assert ocr_unit.text[ocr_unit.ocrWords[0].startOffset : ocr_unit.ocrWords[0].endOffset] == OCR_PII
    ocr_span = MatchSpan(
        zone="ocr",
        unitRef="ocr:0",
        startOffset=0,
        endOffset=len(OCR_PII),
        ruleName="mobile",
    )
    result = redact_pdf_document_with_report(
        pdf_bytes,
        extraction.textUnits,
        text_spans + [ocr_span],
        ocr_text_units=[ocr_unit],
    )
    assert result.report.ocrRegions >= 1
    return result.documentBytes


def _needles(values: List[str]) -> List[bytes]:
    out = set()
    for value in values:
        for variant in (value, _squash(value)):
            for enc in ("utf-8", "utf-16-be", "latin-1"):
                try:
                    out.add(variant.encode(enc))
                except UnicodeEncodeError:
                    pass
    return sorted(out)


def _walk(obj, seen, out: List[bytes]) -> None:
    """Collect every byte representation reachable from `obj`."""
    if isinstance(obj, IndirectObject):
        key = (obj.idnum, obj.generation)
        if key in seen:
            return
        seen.add(key)
        _walk(obj.get_object(), seen, out)
        return
    if isinstance(obj, StreamObject):
        try:
            out.append(bytes(obj.get_data()))
        except Exception:
            out.append(bytes(getattr(obj, "_data", b"") or b""))
        for key, value in obj.items():
            _walk(value, seen, out)
        return
    if isinstance(obj, DictionaryObject):
        for key, value in obj.items():
            out.append(str(key).encode("utf-8", "replace"))
            _walk(value, seen, out)
        return
    if isinstance(obj, ArrayObject):
        for value in obj:
            _walk(value, seen, out)
        return
    if isinstance(obj, ByteStringObject):
        out.append(bytes(obj))
        return
    if isinstance(obj, TextStringObject):
        out.append(str(obj).encode("utf-8"))
        out.append(str(obj).encode("utf-16-be"))
        out.append(getattr(obj, "original_bytes", b""))
        return
    if isinstance(obj, NameObject):
        out.append(str(obj).encode("utf-8", "replace"))
        return
    out.append(repr(obj).encode("utf-8", "replace"))


def _all_object_bytes(data: bytes) -> List[bytes]:
    import io

    reader = pypdf.PdfReader(io.BytesIO(data))
    out: List[bytes] = []
    seen: set = set()
    _walk(reader.trailer, seen, out)
    # every indirect object, reachable or not
    for generation, entries in reader.xref.items():
        for idnum in entries:
            _walk(IndirectObject(idnum, generation, reader), seen, out)
    for idnum in reader.xref_objStm:
        _walk(IndirectObject(idnum, 0, reader), seen, out)
    # XMP via the high-level API as well
    xmp = reader.xmp_metadata
    if xmp is not None:
        out.append(bytes(reader.trailer["/Root"]["/Metadata"].get_object().get_data()))
    return out


def _assert_no_leak(data: bytes, values: List[str]) -> None:
    needles = _needles(values)
    # raw file bytes (covers stale revisions and unreferenced data)
    for needle in needles:
        assert needle not in data, "raw bytes leak {!r}".format(needle)
    for chunk in _all_object_bytes(data):
        for needle in needles:
            assert needle not in chunk, "object leak {!r}".format(needle)


def _extracted_full_text(data: bytes) -> str:
    return _pypdf_text(data)


def test_redacted_pdf_has_no_leak_and_keeps_control_text():
    pdf_bytes = build_pdf(DocSpec(pages=_page_runs()))

    # pypdf can read every PII from the source (whitespace removed)
    source_text = _squash(_extracted_full_text(pdf_bytes))
    for value in ALL_PII:
        assert _squash(value) in source_text, "source lacks extractable {!r}".format(value)
    assert CONTROL in _extracted_full_text(pdf_bytes)

    output = _redact(pdf_bytes)

    text = _extracted_full_text(output)
    squashed = _squash(text)
    for value in ALL_PII:
        assert value not in text, "extracted text leaks {!r}".format(value)
        assert _squash(value) not in squashed, "squashed text leaks {!r}".format(value)
        assert _squash(value) not in text
    # no whitespace-stripped variant (space, tab, newline removed one kind at a time)
    for ws in (" ", "\t", "\n"):
        stripped = text.replace(ws, "")
        for value in ALL_PII:
            assert _squash(value) not in stripped
    assert CONTROL in text
    assert len(pypdf.PdfReader(__import__("io").BytesIO(output)).pages) == 2

    _assert_no_leak(output, ALL_PII)


def test_leak_hidden_in_metadata_annotations_forms_and_old_revisions():
    page1, page2 = _page_runs()
    old = DocSpec(
        pages=(page1, page2),
        info={"Title": "Old title " + LATIN_PII, "Author": OCR_PII},
        xmp=make_xmp(title="Old " + CJK_PII + " " + SPLIT_PII),
    )
    annotated_page1 = PageSpec(
        runs=page1.runs,
        annotations=(
            FreeTextAnnot(contents="Note about " + LATIN_PII + " " + SPLIT_PII),
            WidgetField(name="phone", value=OCR_PII, rect=(72, 300, 300, 330)),
            WidgetField(name="who", value=LATIN_PII, rect=(72, 250, 300, 280)),
        ),
    )
    doc = DocSpec(
        pages=(annotated_page1, page2),
        info={"Title": "Report for " + LATIN_PII, "Subject": SPLIT_PII, "Author": CJK_PII},
        xmp=make_xmp(title=LATIN_PII, description=OCR_PII + " " + CJK_PII),
        prior_revisions=(old,),
    )
    pdf_bytes = build_pdf(doc)
    assert pdf_bytes.count(b"%%EOF") >= 2
    assert OCR_PII.encode() in pdf_bytes or LATIN_PII.encode() in pdf_bytes

    output = _redact(pdf_bytes)

    assert output.count(b"%%EOF") == 1
    text = _extracted_full_text(output)
    for value in ALL_PII:
        assert value not in text
        assert _squash(value) not in _squash(text)
    assert CONTROL in text
    _assert_no_leak(output, ALL_PII)
