"""Unit tests for ``agent.tools.pdf_content_edit`` (glyph removal, black boxes,
annotation removal and document-level scrubbing)."""
import dataclasses
import io
import os
import sys
from typing import Dict, List, Sequence, Tuple

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_LAMBDAS = os.path.join(_BACKEND, "lambdas")
if _LAMBDAS not in sys.path:
    sys.path.insert(0, _LAMBDAS)

import pikepdf  # noqa: E402
import pypdfium2  # noqa: E402
from pypdf import PdfReader  # noqa: E402

from agent.tools.pdf_content_edit import (  # noqa: E402
    ContentEditError,
    GlyphDeletion,
    apply_page_plan,
    draw_black_boxes,
    page_geometry_of,
    plan_deletions,
    remove_intersecting_annotations,
    rewrite_instructions,
    scrub_document_strings,
    tj_adjust_for,
)
from agent.tools.pdf_geometry import user_to_page_rect  # noqa: E402
from agent.tools.pdf_glyphs import GlyphRecord, collect_glyphs_from_bytes  # noqa: E402
from tests.pdf_fixtures import (  # noqa: E402
    DocSpec,
    FormPlacement,
    FormSpec,
    FreeTextAnnot,
    PageSpec,
    TextRun,
    WidgetField,
    build_pdf,
    make_xmp,
)

POSITION_TOL = 0.01

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _region_of(glyphs: Sequence[GlyphRecord]) -> Tuple[float, float, float, float]:
    """A region covering ``glyphs`` but staying clear of their neighbours."""
    m = 0.2
    return (
        min(g.raw_rect[0] for g in glyphs) + m,
        min(g.raw_rect[1] for g in glyphs) + m,
        max(g.raw_rect[2] for g in glyphs) - m,
        max(g.raw_rect[3] for g in glyphs) - m,
    )


def _redact(data: bytes, regions_by_page: Dict[int, list], boxes: bool = True) -> bytes:
    """Run plan -> apply -> boxes for the given pages and return new bytes."""
    pages = collect_glyphs_from_bytes(data)
    pdf = pikepdf.open(io.BytesIO(data))
    for index, regions in regions_by_page.items():
        plan = plan_deletions(pages[index], regions)
        apply_page_plan(pdf, index, plan, pages[index])
        if boxes:
            draw_black_boxes(pdf, index, page_geometry_of(pdf, index), regions)
    out = io.BytesIO()
    pdf.save(out)
    pdf.close()
    return out.getvalue()


def _text_of(data: bytes, page: int = 0) -> str:
    return PdfReader(io.BytesIO(data)).pages[page].extract_text()


def _count_ops(data: bytes, page: int, name: str) -> int:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return sum(
            1
            for ins in pikepdf.parse_content_stream(pdf.pages[page])
            if not isinstance(ins, pikepdf.ContentStreamInlineImage)
            and str(ins.operator) == name
        )


def _assert_survivors_unchanged(before, after, deleted_idx: Sequence[int]) -> None:
    kept = [g for i, g in enumerate(before) if i not in set(deleted_idx)]
    assert [g.text for g in after] == [g.text for g in kept]
    for old, new in zip(kept, after):
        for a, b in zip(old.raw_rect, new.raw_rect):
            assert abs(a - b) <= POSITION_TOL


def _one_page(run: TextRun) -> bytes:
    return build_pdf(DocSpec(pages=(PageSpec(runs=(run,)),)))


# ---------------------------------------------------------------------------
# Operator rewriting preserves the surviving glyphs
# ---------------------------------------------------------------------------

_TEXT = "Hello SECRET world"
_RUNS = {
    "Tj": TextRun(_TEXT, op="Tj"),
    "TJ": TextRun(_TEXT, op="TJ", kerning=-30),
    "quote": TextRun(_TEXT, op="'", leading=14),
    "dquote": TextRun(_TEXT, op='"', tw=2, tc=0.5, leading=14),
    "Tj_tc": TextRun(_TEXT, op="Tj", tc=1.5, tw=3),
    "Tj_tz": TextRun(_TEXT, op="Tj", tz=150),
}


@pytest.mark.parametrize("key", list(_RUNS))
def test_operator_rewrite_keeps_survivors_and_removes_deleted(key):
    data = _one_page(_RUNS[key])
    before = collect_glyphs_from_bytes(data)[0].glyphs
    first = _TEXT.index("SECRET")
    idx = list(range(first, first + 6))  # interior of the operator
    region = _region_of([before[i] for i in idx])

    out = _redact(data, {0: [region]})
    after = collect_glyphs_from_bytes(out)[0].glyphs

    assert len(after) == len(before) - len(idx)
    _assert_survivors_unchanged(before, after, idx)
    joined = "".join(g.text for g in after)
    assert "SECRET" not in joined and "SECR" not in joined
    assert "SECRET" not in _text_of(out).replace(" ", "")
    assert "Hello" in _text_of(out) and "world" in _text_of(out)


def test_plan_includes_every_glyph_in_region_and_nothing_else():
    data = _one_page(TextRun("ABCDEF"))
    page = collect_glyphs_from_bytes(data)[0]
    region = _region_of(page.glyphs[2:4])
    plan = plan_deletions(page, [region])
    deletions = [d for dels in plan.values() for d in dels]
    assert len(deletions) == 2
    assert sorted(d.byte_start for d in deletions) == [2, 3]


def test_deleting_first_glyph_with_zero_tc_keeps_positions():
    data = _one_page(TextRun("SECRET tail"))
    before = collect_glyphs_from_bytes(data)[0].glyphs
    out = _redact(data, {0: [_region_of(before[:6])]})
    after = collect_glyphs_from_bytes(out)[0].glyphs
    _assert_survivors_unchanged(before, after, range(6))


def test_deleting_last_glyphs_with_zero_tc_keeps_positions():
    data = _one_page(TextRun("head SECRET"))
    before = collect_glyphs_from_bytes(data)[0].glyphs
    out = _redact(data, {0: [_region_of(before[5:])]})
    after = collect_glyphs_from_bytes(out)[0].glyphs
    _assert_survivors_unchanged(before, after, range(5, 11))


def test_render_mode_3_text_is_removed():
    data = _one_page(TextRun("invisible SECRET", tr=3))
    before = collect_glyphs_from_bytes(data)[0].glyphs
    assert before, "invisible text is still traversed"
    out = _redact(data, {0: [_region_of(before)]})
    assert collect_glyphs_from_bytes(out)[0].glyphs == []
    assert "SECRET" not in _text_of(out)


# ---------------------------------------------------------------------------
# Form XObjects: copy on write
# ---------------------------------------------------------------------------


def _form_doc() -> bytes:
    form = FormSpec(name="F", bbox=(0, 0, 612, 792), runs=(TextRun("SECRET", x=100, y=500),))
    page = PageSpec(
        form_placements=(
            FormPlacement("F"),
            FormPlacement("F", (1, 0, 0, 1, 0, -100)),
        )
    )
    return build_pdf(DocSpec(pages=(page,), forms=(form,)))


def test_same_page_two_placements_only_first_is_deleted():
    data = _form_doc()
    glyphs = collect_glyphs_from_bytes(data)[0].glyphs
    assert len(glyphs) == 12
    first = sorted(glyphs, key=lambda g: g.raw_rect[1])[:6]
    out = _redact(data, {0: [_region_of(first)]})
    after = collect_glyphs_from_bytes(out)[0].glyphs
    assert len(after) == 6
    assert "".join(g.text for g in after) == "SECRET"
    # the surviving placement is the lower one
    assert after[0].raw_rect[1] == pytest.approx(
        min(g.raw_rect[1] for g in first) + 100, abs=POSITION_TOL
    )
    assert _text_of(out).count("SECRET") == 1


def test_form_shared_across_pages_is_not_modified_on_other_page():
    form = FormSpec(name="F", runs=(TextRun("SECRET", x=100, y=500),))
    doc = DocSpec(
        pages=(
            PageSpec(form_placements=(FormPlacement("F"),)),
            PageSpec(form_placements=(FormPlacement("F"),)),
        ),
        forms=(form,),
    )
    data = build_pdf(doc)
    pages = collect_glyphs_from_bytes(data)
    out = _redact(data, {0: [_region_of(pages[0].glyphs)]})
    assert "SECRET" not in _text_of(out, 0)
    assert "SECRET" in _text_of(out, 1)
    assert collect_glyphs_from_bytes(out)[1].glyphs and not collect_glyphs_from_bytes(out)[0].glyphs


def test_nested_forms_are_copied_down_the_chain():
    inner = FormSpec(name="Inner", runs=(TextRun("SECRET", x=100, y=500),))
    outer = FormSpec(name="Outer", form_placements=(FormPlacement("Inner"),))
    doc = DocSpec(
        pages=(
            PageSpec(form_placements=(FormPlacement("Outer"),)),
            PageSpec(form_placements=(FormPlacement("Outer"),)),
        ),
        forms=(inner, outer),
    )
    data = build_pdf(doc)
    pages = collect_glyphs_from_bytes(data)
    assert pages[0].glyphs[0].context and len(pages[0].glyphs[0].context) == 2
    out = _redact(data, {0: [_region_of(pages[0].glyphs[2:5])]})
    assert "".join(g.text for g in collect_glyphs_from_bytes(out)[0].glyphs) == "SET"
    assert _text_of(out, 1).count("SECRET") == 1
    assert "SECRET" not in _text_of(out, 0)


# ---------------------------------------------------------------------------
# Black boxes
# ---------------------------------------------------------------------------


def test_overlapping_text_and_ocr_regions_both_planned_and_each_boxed():
    data = _one_page(TextRun("ABCDEFGHIJ"))
    page = collect_glyphs_from_bytes(data)[0]
    text_region = _region_of(page.glyphs[1:6])  # B..F   (text: source)
    ocr_region = _region_of(page.glyphs[4:9])  # E..I   (ocr: source), overlaps
    plan = plan_deletions(page, [text_region, ocr_region])
    planned = sorted(d.byte_start for dels in plan.values() for d in dels)
    assert planned == list(range(1, 9))

    out = _redact(data, {0: [text_region, ocr_region]})
    assert _count_ops(out, 0, "re") == 2
    assert _count_ops(out, 0, "f") == 2
    assert "".join(g.text for g in collect_glyphs_from_bytes(out)[0].glyphs) == "AJ"


def test_black_box_renders_opaque_black():
    data = _one_page(TextRun("SECRET", size=40))
    page = collect_glyphs_from_bytes(data)[0]
    region = _region_of(page.glyphs)
    out = _redact(data, {0: [region]})
    pdf = pypdfium2.PdfDocument(out)
    image = pdf[0].render(scale=1).to_pil().convert("RGB")
    cx = int((region[0] + region[2]) / 2)
    cy = int((region[1] + region[3]) / 2)
    assert image.getpixel((cx, cy)) == (0, 0, 0)
    assert image.getpixel((int(region[0]) + 1, int(region[1]) + 1)) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _first_page_instructions(data: bytes):
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return pikepdf.parse_content_stream(pdf.pages[0])


def test_ordinal_pointing_at_non_text_operator_raises():
    data = _one_page(TextRun("ABC"))
    glyphs = collect_glyphs_from_bytes(data)[0].glyphs
    instructions = _first_page_instructions(data)
    g = glyphs[0]
    bad = GlyphDeletion(g.op_ordinal - 1, 0, 0, 1, 0.0)  # the Tm / Tf before
    with pytest.raises(ContentEditError):
        rewrite_instructions(instructions, [bad], glyphs)


def test_ordinal_outside_stream_raises():
    data = _one_page(TextRun("ABC"))
    glyphs = collect_glyphs_from_bytes(data)[0].glyphs
    instructions = _first_page_instructions(data)
    with pytest.raises(ContentEditError):
        rewrite_instructions(instructions, [GlyphDeletion(999, 0, 0, 1, 0.0)], glyphs)


def test_byte_range_outside_string_raises():
    data = _one_page(TextRun("ABC"))
    glyphs = collect_glyphs_from_bytes(data)[0].glyphs
    instructions = _first_page_instructions(data)
    g = glyphs[0]
    bad = GlyphDeletion(g.op_ordinal, g.elem_index, 0, 99, 0.0)
    with pytest.raises(ContentEditError):
        rewrite_instructions(instructions, [bad], glyphs)


def test_glyph_records_not_tiling_string_raises():
    data = _one_page(TextRun("ABC"))
    glyphs = collect_glyphs_from_bytes(data)[0].glyphs
    instructions = _first_page_instructions(data)
    g = glyphs[0]
    good = GlyphDeletion(g.op_ordinal, g.elem_index, g.byte_start, g.byte_end, 0.0)
    with pytest.raises(ContentEditError):
        rewrite_instructions(instructions, [good], glyphs[:2])  # one record missing


def test_zero_font_size_raises():
    data = _one_page(TextRun("ABC"))
    page = collect_glyphs_from_bytes(data)[0]
    zero = dataclasses.replace(page.glyphs[0], font_size=0.0)
    with pytest.raises(ContentEditError):
        tj_adjust_for(zero)
    page.glyphs[0] = zero
    with pytest.raises(ContentEditError):
        plan_deletions(page, [_region_of(page.glyphs)])


def test_zero_horizontal_scale_raises():
    data = _one_page(TextRun("ABC"))
    glyph = collect_glyphs_from_bytes(data)[0].glyphs[0]
    with pytest.raises(ContentEditError):
        tj_adjust_for(dataclasses.replace(glyph, horizontal_scale=0.0))


def test_tiling_pattern_with_text_raises():
    data = _one_page(TextRun("ABC"))
    page = collect_glyphs_from_bytes(data)[0]
    pdf = pikepdf.open(io.BytesIO(data))
    pdf_page = pdf.pages[0]
    font = list(pdf_page.Resources.Font.values())[0]
    pattern = pdf.make_stream(b"BT /F1 12 Tf 0 0 Td (PATTERNSECRET) Tj ET")
    pattern["/Type"] = pikepdf.Name("/Pattern")
    pattern["/PatternType"] = 1
    pattern["/PaintType"] = 1
    pattern["/TilingType"] = 1
    pattern["/BBox"] = pikepdf.Array([0, 0, 50, 50])
    pattern["/XStep"] = 50
    pattern["/YStep"] = 50
    pattern["/Resources"] = pikepdf.Dictionary(
        {"/Font": pikepdf.Dictionary({"/F1": font})}
    )
    pdf_page.Resources["/Pattern"] = pikepdf.Dictionary({"/P1": pattern})
    plan = plan_deletions(page, [_region_of(page.glyphs[:1])])
    with pytest.raises(ContentEditError):
        apply_page_plan(pdf, 0, plan, page)
    with pytest.raises(ContentEditError):  # also with an empty plan
        apply_page_plan(pdf, 0, {}, page)


def test_tiling_pattern_without_text_is_accepted():
    data = _one_page(TextRun("ABC"))
    page = collect_glyphs_from_bytes(data)[0]
    pdf = pikepdf.open(io.BytesIO(data))
    pattern = pdf.make_stream(b"0 0 10 10 re f")
    pattern["/PatternType"] = 1
    pattern["/PaintType"] = 1
    pattern["/TilingType"] = 1
    pattern["/BBox"] = pikepdf.Array([0, 0, 10, 10])
    pattern["/XStep"] = 10
    pattern["/YStep"] = 10
    pdf.pages[0].Resources["/Pattern"] = pikepdf.Dictionary({"/P1": pattern})
    apply_page_plan(pdf, 0, {}, page)  # no error


# ---------------------------------------------------------------------------
# Annotations and widgets
# ---------------------------------------------------------------------------

_ANNOT_RECT = (72, 400, 300, 430)
_WIDGET_RECT = (72, 300, 300, 330)


def _annot_doc(**kwargs) -> bytes:
    page = PageSpec(
        runs=(TextRun("body text", y=700),),
        annotations=(
            FreeTextAnnot("John Smith note", rect=_ANNOT_RECT),
            WidgetField("name", "John Smith", rect=_WIDGET_RECT),
        ),
    )
    return build_pdf(DocSpec(pages=(page,), **kwargs))


def test_intersecting_annotations_and_widgets_are_removed():
    pdf = pikepdf.open(io.BytesIO(_annot_doc()))
    geom = page_geometry_of(pdf, 0)
    regions = [
        user_to_page_rect(geom, _ANNOT_RECT),
        user_to_page_rect(geom, _WIDGET_RECT),
    ]
    assert remove_intersecting_annotations(pdf, 0, geom, regions) == 2
    assert len(pdf.pages[0].obj.Annots) == 0
    assert len(pdf.Root.AcroForm.Fields) == 0


def test_non_intersecting_annotation_is_kept():
    pdf = pikepdf.open(io.BytesIO(_annot_doc()))
    geom = page_geometry_of(pdf, 0)
    region = user_to_page_rect(geom, _ANNOT_RECT)
    assert remove_intersecting_annotations(pdf, 0, geom, [region]) == 1
    annots = pdf.pages[0].obj.Annots
    assert len(annots) == 1 and annots[0].Subtype == "/Widget"
    assert len(pdf.Root.AcroForm.Fields) == 1


# ---------------------------------------------------------------------------
# Document-level scrubbing
# ---------------------------------------------------------------------------


def _attach(pdf: "pikepdf.Pdf", payload: bytes) -> None:
    file_stream = pdf.make_stream(payload)
    file_stream["/Type"] = pikepdf.Name("/EmbeddedFile")
    spec = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Filespec"),
                "/F": pikepdf.String("notes.txt"),
                "/EF": pikepdf.Dictionary({"/F": file_stream}),
            }
        )
    )
    names = pikepdf.Dictionary({"/Names": pikepdf.Array([pikepdf.String("notes.txt"), spec])})
    pdf.Root["/Names"] = pikepdf.Dictionary({"/EmbeddedFiles": names})


def test_scrub_document_strings_clears_info_xmp_fields_and_attachments():
    data = _annot_doc(
        info={"Title": "Report for John Smith", "Author": "Someone Else"},
        xmp=make_xmp(title="John Smith file"),
    )
    pdf = pikepdf.open(io.BytesIO(data))
    _attach(pdf, b"attachment mentions JohnSmith here")

    scrub_document_strings(pdf, ["JohnSmith"])

    out = io.BytesIO()
    pdf.save(out)
    pdf.close()
    saved = out.getvalue()

    with pikepdf.open(io.BytesIO(saved)) as check:
        assert str(check.docinfo.get("/Title", "")) == ""
        assert str(check.docinfo.get("/Author")) == "Someone Else"
        assert "/Metadata" not in check.Root
        names = check.Root.get("/Names")
        if names is not None and "/EmbeddedFiles" in names:
            assert len(names.EmbeddedFiles.Names) == 0
        assert not any(
            "John" in str(a.get("/V", "")) for a in check.pages[0].obj.get("/Annots", [])
        )
        # annotations whose appearance draws the PII are gone
        assert len(check.pages[0].obj.get("/Annots", [])) == 0
    assert b"John" not in saved and b"attachment mentions" not in saved
    assert b"/Filespec" not in saved and b"notes.txt" not in saved
    reader = PdfReader(io.BytesIO(saved))
    assert not reader.attachments
    assert "John" not in str(reader.metadata)
    assert "body text" in reader.pages[0].extract_text()


def test_scrub_with_no_values_changes_nothing():
    data = _annot_doc(info={"Title": "Report for John Smith"})
    pdf = pikepdf.open(io.BytesIO(data))
    scrub_document_strings(pdf, [])
    scrub_document_strings(pdf, ["   "])
    assert str(pdf.docinfo["/Title"]) == "Report for John Smith"
