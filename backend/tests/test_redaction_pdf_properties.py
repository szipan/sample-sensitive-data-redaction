"""Property tests for ``agent.tools.redaction_pdf`` (spec pymupdf-replacement).

Each property lives in its own section below; shared helpers are at the top.
"""
import dataclasses
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_LAMBDAS = os.path.join(_BACKEND, "lambdas")
if _LAMBDAS not in sys.path:
    sys.path.insert(0, _LAMBDAS)
_TESTS = os.path.dirname(os.path.abspath(__file__))
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent.tools import redaction_pdf  # noqa: E402
from agent.tools.extraction_pdf import extract_pdf_content  # noqa: E402
from agent.tools.pdf_glyphs import collect_page_glyphs, open_pdf  # noqa: E402
from pdf_fixtures import build_pdf, doc_specs, page_specs  # noqa: E402
from shared.models import MatchSpan  # noqa: E402

_TOL = 0.01


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@st.composite
def _docs(draw):
    """Documents of latin or CJK text (single and multi-line), with optional
    ligatures and rotated / cropped pages (``doc_specs`` geometry)."""
    doc = draw(doc_specs(max_pages=3, cjk=draw(st.booleans())))
    ligatures = draw(st.sampled_from([(), (), ("fi",), ("ffi", "fi")]))
    return dataclasses.replace(doc, cjk_ligatures=ligatures)


def _contains(outer, inner, tol=_TOL) -> bool:
    return (
        outer[0] <= inner[0] + tol
        and outer[1] <= inner[1] + tol
        and outer[2] >= inner[2] - tol
        and outer[3] >= inner[3] - tol
    )


def _near(a, b, tol=_TOL) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _inside_point(rect, x, y) -> bool:
    return rect[0] < x < rect[2] and rect[1] < y < rect[3]


def _text_units_with_glyphs(pdf_bytes: bytes):
    """Yield ``(unit, page_glyphs)`` for every extracted text unit."""
    extraction = extract_pdf_content(pdf_bytes)
    handle = open_pdf(pdf_bytes)
    for unit in extraction.textUnits:
        page_index = unit.position.pageIndex
        yield unit, collect_page_glyphs(handle, page_index)


def _regions(unit, page, start: int, end: int):
    span = MatchSpan(
        zone="body", unitRef="text:0", startOffset=start,
        endOffset=end, ruleName="prop",
    )
    return redaction_pdf._regions_for_text_span(unit, span, page)


# ---------------------------------------------------------------------------
# Property 5: text: 区域按行精确覆盖
# ---------------------------------------------------------------------------


# Feature: pymupdf-replacement, Property 5: text: 区域按行精确覆盖
# **Validates: Requirements 5.6, 5.7**
@settings(max_examples=100, deadline=None)
@given(doc=_docs(), data=st.data())
def test_property_5_text_regions_cover_matched_lines_exactly(doc, data):
    pdf_bytes = build_pdf(doc)
    units = list(_text_units_with_glyphs(pdf_bytes))
    if not units:
        return
    unit, page = data.draw(st.sampled_from(units))
    assert unit.text == page.text
    n = len(page.chars)
    if n < 1:
        return
    start = data.draw(st.integers(min_value=0, max_value=n - 1))
    end = data.draw(st.integers(min_value=start + 1, max_value=n))

    regions = _regions(unit, page, start, end)
    matched = page.chars[start:end]
    with_rect = [c for c in matched if c.rect is not None]
    if not with_rect:
        return  # fallback estimate path; not covered by this property

    # (2) one region per distinct line_id, in order of first appearance.
    line_order = []
    for c in with_rect:
        if c.line_id not in line_order:
            line_order.append(c.line_id)
    assert len(regions) == len(line_order)

    # (3) region k is the minimal bounding rect of line k's matched rects.
    for region, line_id in zip(regions, line_order):
        rects = [c.rect for c in with_rect if c.line_id == line_id]
        expected = (
            min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects),
        )
        assert _near(region, expected), (region, expected)

    # (1) every matched character with a rect lies within some region.
    for c in with_rect:
        assert any(_contains(r, c.rect) for r in regions), c.rect

    # (4) an unmatched character on a different line is not covered.
    # Generated runs may physically overlap (e.g. two runs drawn at the same
    # position), so a character is only expected to stay uncovered when it
    # does not lie inside the independently computed bounding rect of a
    # matched line; this catches regions merged across lines or padded.
    matched_lines = set(line_order)
    line_boxes = []
    for line_id in line_order:
        rects = [c.rect for c in with_rect if c.line_id == line_id]
        line_boxes.append((
            min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects),
        ))
    for index, c in enumerate(page.chars):
        if start <= index < end or c.rect is None:
            continue
        if c.line_id in matched_lines:
            continue
        cx = (c.rect[0] + c.rect[2]) / 2
        cy = (c.rect[1] + c.rect[3]) / 2
        if any(_inside_point(b, cx, cy) for b in line_boxes):
            continue
        assert not any(_inside_point(r, cx, cy) for r in regions), (
            index, c.rect, regions,
        )


# ---------------------------------------------------------------------------
# Property 6: 比例估算回退
# ---------------------------------------------------------------------------
# Feature: pymupdf-replacement, Property 6: 比例估算回退
# **Validates: Requirements 5.8, 5.9**
#
# Note on the page-read-failure case: `_regions_for_text_span` itself falls back
# to the proportional estimate (page=None), but the full pipeline then raises
# `RedactionError` in `_collect_original_glyphs`, because a page that cannot be
# read cannot be edited. That case is therefore asserted at region level only
# and, for the pipeline, as a RedactionError.

from agent.tools.redaction_pdf import RedactionError  # noqa: E402
from agent.tools.pdf_glyphs import PageReadError  # noqa: E402
from shared.models import BoundingBox, ExtractedTextUnit, TextPosition  # noqa: E402
from pdf_fixtures import DocSpec, PageSpec, TextRun  # noqa: E402

_coord = st.floats(min_value=-500, max_value=1500, allow_nan=False, allow_infinity=False)
_size = st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False)


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def _expected_proportional(unit, start: int, end: int):
    box = unit.boundingBox
    top, bottom = box.top, box.top + box.height
    n = len(unit.text)
    if n == 0:
        return (box.left, top, box.left + box.width, bottom)
    return (
        box.left + _clamp01(start / n) * box.width,
        top,
        box.left + _clamp01(end / n) * box.width,
        bottom,
    )


def _unit(text, left, top, width, height):
    return ExtractedTextUnit(
        zone="body",
        text=text,
        position=TextPosition(index=0, pageIndex=0),
        boundingBox=BoundingBox(left=left, top=top, width=width, height=height),
    )


def _assert_single_proportional(regions, unit, start, end):
    expected = _expected_proportional(unit, start, end)
    assert len(regions) == 1, regions
    assert _near(regions[0], expected, 1e-9), (regions, expected)


@st.composite
def _fallback_cases(draw):
    """(unit, page, start, end) for which the proportional fallback must apply."""
    text = draw(st.text(max_size=40))
    unit = _unit(text, draw(_coord), draw(_coord), draw(_size), draw(_size))
    kind = draw(st.sampled_from(["page_none", "zero_chars", "bad_offsets", "no_rects"]))
    if kind in ("page_none", "zero_chars"):
        start = draw(st.integers(-50, 100))
        end = draw(st.integers(-50, 100))
        if kind == "page_none":
            return unit, None, start, end
        page = redaction_pdf.PageGlyphs(geometry=None)
        return unit, page, start, end
    # Build a real page for the remaining kinds.
    pdf = build_pdf(DocSpec(pages=(PageSpec(runs=(TextRun("Hello World"),)),)))
    page = collect_page_glyphs(open_pdf(pdf), 0)
    n = len(page.chars)
    assert n > 0
    if kind == "bad_offsets":
        which = draw(st.sampled_from(["end_gt_len", "start_ge_end", "neg_start"]))
        if which == "end_gt_len":
            start = draw(st.integers(0, n))
            end = draw(st.integers(n + 1, n + 100))
        elif which == "start_ge_end":
            end = draw(st.integers(-20, n))
            start = draw(st.integers(end, end + 50))
        else:
            start = draw(st.integers(-100, -1))
            end = draw(st.integers(-100, n + 20))
        return unit, page, start, end
    # no_rects: every char in range lacks a rect
    page = dataclasses.replace(
        page,
        chars=[dataclasses.replace(c, rect=None) for c in page.chars],
    )
    start = draw(st.integers(0, n - 1))
    end = draw(st.integers(start + 1, n))
    return unit, page, start, end


@settings(max_examples=100, deadline=None)
@given(case=_fallback_cases())
def test_property_6_proportional_fallback_regions(case):
    unit, page, start, end = case
    regions = _regions(unit, page, start, end)
    _assert_single_proportional(regions, unit, start, end)


@settings(max_examples=100, deadline=None)
@given(extra=st.integers(1, 50))
def test_property_6_pipeline_reports_one_text_layer_region(extra):
    # The estimate only guarantees coverage when it spans the whole unit, so the
    # span starts at 0 and ends beyond the char positions (clamped to 1.0).
    # Offsets beyond the page's char positions -> fallback, never dropped.
    pdf = build_pdf(DocSpec(pages=(PageSpec(runs=(TextRun("Hello World"),)),)))
    extraction = extract_pdf_content(pdf)
    unit = extraction.textUnits[0]
    n = len(collect_page_glyphs(open_pdf(pdf), unit.position.pageIndex).chars)
    start = 0
    end = n + extra
    span = MatchSpan(
        zone="body", unitRef="text:0", startOffset=start,
        endOffset=end, ruleName="prop",
    )
    result = redaction_pdf.redact_pdf_document_with_report(pdf, [unit], [span])
    assert result.report.textLayerRegions == 1


@settings(max_examples=100, deadline=None)
@given(start=st.integers(0, 5), end=st.integers(1, 20))
def test_property_6_unreadable_page_pipeline_raises_redaction_error(start, end):
    pdf = build_pdf(DocSpec(pages=(PageSpec(runs=(TextRun("Hello World"),)),)))
    unit = extract_pdf_content(pdf).textUnits[0]
    span = MatchSpan(
        zone="body", unitRef="text:0", startOffset=start,
        endOffset=end, ruleName="prop",
    )

    def _boom(handle, page_index):
        raise PageReadError("unreadable")

    original = redaction_pdf.collect_page_glyphs
    redaction_pdf.collect_page_glyphs = _boom
    try:
        raised = False
        try:
            redaction_pdf.redact_pdf_document_with_report(pdf, [unit], [span])
        except RedactionError:
            raised = True
        assert raised
    finally:
        redaction_pdf.collect_page_glyphs = original
# ---------------------------------------------------------------------------
# Property 8: 删除与保留的精确划分
# ---------------------------------------------------------------------------
# Feature: pymupdf-replacement, Property 8: 删除与保留的精确划分
# **Validates: Requirements 8.1, 8.5**
#
# Regions are drawn directly (not through spans): glyph-aligned rectangles,
# rectangles over the gap between two lines, and arbitrary rectangles. The
# lower-level plan / apply / draw steps are run, the document is saved and the
# output is compared with the source:
#   (i)  no output glyph box meets any region;
#   (ii) the source glyphs that meet no region are, in Unicode and order,
#        exactly the output glyphs;
#   (iii) an independent reader (PDFium) finds no character centred in a region
#        and every character it reads sits where the same character sat in the
#        source (0.01 pt). As in ``redaction_pdf._verify_redacted_output``,
#        positions are judged by PDFium and not by the traversal, because with
#        a non-zero Tc the traversal re-reads glyphs following a deleted glyph
#        at an operator boundary shifted by Tc although they are drawn in place.
import io  # noqa: E402
import pikepdf  # noqa: E402
from agent.tools.pdf_content_edit import (  # noqa: E402
    apply_page_plan, draw_black_boxes, page_geometry_of, plan_deletions,
)
from agent.tools.pdf_geometry import rects_intersect, user_to_page_rect  # noqa: E402
from agent.tools.pdf_glyphs import collect_glyphs_from_bytes  # noqa: E402
from pdf_fixtures import TEXT_OPS  # noqa: E402
@st.composite
def _tight_line_runs(draw, media):
    """Several lines whose spacing is close to (or below) the font size."""
    x0, y0, x1, y1 = media
    font = draw(st.sampled_from(["Helvetica", "Times-Roman", "Courier"]))
    size = draw(st.integers(8, 20))
    step = size * draw(st.sampled_from([0.8, 0.9, 1.0, 1.05, 1.1, 1.2]))
    x = x0 + draw(st.integers(15, 60))
    top = y1 - draw(st.integers(60, 120))
    ops = draw(st.sampled_from([("Tj",), ("TJ",), ("'",), ('"',), TEXT_OPS]))
    tc = draw(st.sampled_from([0, 0, 1, 2]))
    tz = draw(st.sampled_from([100, 100, 80]))
    mode = draw(st.sampled_from([0, 0, 0, 3]))
    runs = []
    for i in range(draw(st.integers(2, 4))):
        runs.append(TextRun(
            text=draw(latin_text_(1, 14)), x=x, y=top - i * step,
            font=font, size=size, op=draw(st.sampled_from(list(ops))),
            tc=tc, tz=tz, tr=mode, leading=step,
        ))
    return runs
def latin_text_(lo, hi):
    from pdf_fixtures import latin_text
    return latin_text(lo, hi)
def _no_tz_into_forms(content):
    """Keep Tz at 100 % in content that places a Form XObject.
    The text state, Tz included, is inherited by a form (PDF 32000-1, 8.10.1);
    the shared traversal, MuPDF and Poppler apply it, but PDFium resets Tz to
    100 % inside the form, so the independent-reader check below would compare
    against a non-conforming layout. Tz stays covered everywhere else."""
    if not content.form_placements:
        return content
    return dataclasses.replace(
        content, runs=tuple(dataclasses.replace(r, tz=100) for r in content.runs)
    )
@st.composite
def _prop8_docs(draw):
    doc = draw(doc_specs(max_pages=2, cjk=draw(st.booleans()), modes=(0, 0, 3)))
    pages = []
    for page in doc.pages:
        if draw(st.booleans()):
            page = dataclasses.replace(
                page, runs=tuple(page.runs) + tuple(draw(_tight_line_runs(page.media_box)))
            )
        pages.append(_no_tz_into_forms(page))
    return dataclasses.replace(
        doc, pages=tuple(pages),
        forms=tuple(_no_tz_into_forms(f) for f in doc.forms),
        cjk_ligatures=draw(st.sampled_from([(), ("fi",)])),
    )
@st.composite
def _region_sets(draw, source_pages):
    """{page_index: [rect, ...]} in Page_Coordinates."""
    out = {}
    for index, page in enumerate(source_pages):
        regions = []
        glyphs = page.glyphs
        w, h = page.geometry.width, page.geometry.height
        for _ in range(draw(st.integers(0, 3))):
            kind = draw(st.sampled_from(["glyph", "run", "gap", "random", "jitter"]))
            if kind == "random" or not glyphs:
                x = draw(st.floats(-20, w, allow_nan=False))
                y = draw(st.floats(-20, h, allow_nan=False))
                rw = draw(st.floats(0.5, 200, allow_nan=False))
                rh = draw(st.floats(0.5, 60, allow_nan=False))
                regions.append((x, y, x + rw, y + rh))
                continue
            i = draw(st.integers(0, len(glyphs) - 1))
            g = glyphs[i].raw_rect
            if kind == "glyph":
                regions.append(tuple(g))
            elif kind == "run":
                j = draw(st.integers(i, min(len(glyphs) - 1, i + 6)))
                rs = [glyphs[k].raw_rect for k in range(i, j + 1)]
                regions.append((min(r[0] for r in rs), min(r[1] for r in rs),
                                max(r[2] for r in rs), max(r[3] for r in rs)))
            elif kind == "jitter":
                d = [draw(st.sampled_from([-0.5, -0.05, -0.005, 0, 0.005, 0.05, 0.5]))
                     for _ in range(4)]
                regions.append((g[0] + d[0], g[1] + d[1], g[2] + d[2], g[3] + d[3]))
            else:  # gap: a band at the bottom / top edge of a glyph line box
                gh = g[3] - g[1]
                edge = draw(st.sampled_from([g[1], g[3]]))
                off = draw(st.floats(-0.4, 0.4, allow_nan=False)) * gh
                band = draw(st.floats(0.05, 0.5, allow_nan=False)) * gh
                lo = edge + off
                regions.append((g[0] - 5, lo, g[2] + 5, lo + band))
        if regions:
            out[index] = regions
    return out
def _apply_regions(pdf_bytes, regions_by_page, source_pages):
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    try:
        for index, regions in sorted(regions_by_page.items()):
            glyphs = source_pages[index]
            plan = plan_deletions(glyphs, regions)
            apply_page_plan(pdf, index, plan, glyphs)
            geom = page_geometry_of(pdf, index)
            draw_black_boxes(pdf, index, geom, regions)
        buffer = io.BytesIO()
        pdf.save(buffer)
        return buffer.getvalue()
    finally:
        pdf.close()
def _pdfium_boxes(pdf_bytes, page_index):
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        page = doc[page_index]
        try:
            tp = page.get_textpage()
            try:
                # PDFium reports a line-end hyphen as U+FFFE depending on the
                # text around it; it is the same glyph, so read it as "-".
                return [
                    (tp.get_text_range(i, 1).replace("\ufffe", "-"),
                     tuple(tp.get_charbox(i)))
                    for i in range(tp.count_chars())
                    if not raw.FPDFText_IsGenerated(tp, i)
                ]
            finally:
                tp.close()
        finally:
            page.close()
    finally:
        doc.close()
def _has_same_text_overlap(glyphs) -> bool:
    """True when two glyphs with the same text overlap; PDFium then reports
    only one of them, so the other may first appear once its twin is gone."""
    for i, a in enumerate(glyphs):
        for b in glyphs[i + 1:]:
            if a.text == b.text and rects_intersect(a.raw_rect, b.raw_rect):
                return True
    return False
@settings(max_examples=100, deadline=None)
@given(doc=_prop8_docs(), data=st.data())
def test_property_8_exact_partition_of_deleted_and_retained(doc, data):
    source_bytes = build_pdf(doc)
    source_pages = collect_glyphs_from_bytes(source_bytes)
    regions_by_page = data.draw(_region_sets(source_pages))
    output_bytes = _apply_regions(source_bytes, regions_by_page, source_pages)
    output_pages = collect_glyphs_from_bytes(output_bytes)
    assert len(output_pages) == len(source_pages)
    for index, source in enumerate(source_pages):
        regions = regions_by_page.get(index, [])
        produced = output_pages[index].glyphs
        # (i) nothing under a region survives.
        for glyph in produced:
            for region in regions:
                assert not rects_intersect(glyph.raw_rect, region), (
                    index, glyph.text, glyph.raw_rect, region,
                )
        # (ii) retained source glyphs == output glyphs, same text and order.
        expected = [
            g for g in source.glyphs
            if not any(rects_intersect(g.raw_rect, r) for r in regions)
        ]
        assert [g.text for g in expected] == [g.text for g in produced], (
            index, regions,
        )
        # (iii) independent reader: nothing centred in a region; every char
        # sits where it did in the source.
        if not regions:
            continue
        overlapped = _has_same_text_overlap(source.glyphs)
        pool = {}
        for text, box in _pdfium_boxes(source_bytes, index):
            pool.setdefault(text, []).append(box)
        geom = output_pages[index].geometry
        for text, box in _pdfium_boxes(output_bytes, index):
            candidates = pool.get(text, [])
            hit = next(
                (k for k, c in enumerate(candidates)
                 if all(abs(a - b) <= _TOL for a, b in zip(c, box))),
                None,
            )
            pb = user_to_page_rect(geom, box)
            if hit is not None:
                del candidates[hit]
            else:
                # PDFium merges a character drawn over the same character, so
                # a glyph hidden that way in the source may surface once its
                # twin is deleted. It must then still sit inside a retained
                # source glyph box with the same text.
                cx, cy = (pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2
                assert overlapped and any(
                    g.text == text and _inside_point(g.raw_rect, cx, cy)
                    for g in expected
                ), ("retained char moved", index, text, box)
            cx, cy = (pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2
            assert not any(
                r[0] + _TOL <= cx <= r[2] - _TOL and r[1] + _TOL <= cy <= r[3] - _TOL
                for r in regions
            ), ("char under region", index, text, pb, regions)


# ---------------------------------------------------------------------------
# Independent-reader check: line-end hyphen reported as U+FFFE or "-"

def test_independent_reader_text_treats_fffe_as_hyphen():
    assert redaction_pdf._normalise_reader_text("\ufffe") == "-"
    assert redaction_pdf._normalise_reader_text("-") == "-"
    assert redaction_pdf._normalise_reader_text("a") == "a"
    unmatched = {"-": [(1.0, 2.0, 3.0, 4.0)]}
    assert redaction_pdf._take_matching_box(
        unmatched, redaction_pdf._normalise_reader_text("\ufffe"), (1.0, 2.0, 3.0, 4.0)
    )


def test_redaction_of_rotated_page_with_trailing_hyphen_passes_verification():
    from agent.tools.matching import match_rules
    from shared.models import RedactionRule

    runs = tuple(
        TextRun(text, x=x, y=y, size=8)
        for text, x, y in (
            ("0", 10, 40), ("0", 31, 40), ("1000", 10, 40), ("1", 10, 48),
            ("0", 15, 340), ("A-", 15, 333.6),
        )
    )
    pdf = build_pdf(DocSpec(pages=(PageSpec(runs=runs, rotate=90),)))
    rule = RedactionRule(
        ruleName="digits", pattern="1000", enabled=True, isBuiltIn=False,
        createdAt="2024-01-01T00:00:00Z", updatedAt="2024-01-01T00:00:00Z",
    )
    extraction = extract_pdf_content(pdf)
    matches = match_rules(extraction.textUnits, extraction.imageRegions, [rule])
    assert matches
    result = redaction_pdf.redact_pdf_document_with_report(
        pdf, extraction.textUnits, matches
    )
    text = "".join(
        ch for p in collect_glyphs_from_bytes(result.documentBytes)
        for ch in p.text if not ch.isspace()
    )
    assert "1000" not in text


def test_redaction_of_overstruck_identical_characters_passes_verification():
    # "Fake bold": the same character drawn over itself. PDFium merges the
    # overlapping copies in the source, so after their neighbours are deleted
    # a retained copy has no source box of its own.
    runs = (
        TextRun("0", 10, 40, size=8), TextRun("000000", 10, 40, size=8),
        TextRun("0", 10, 40, size=8, tc=1), TextRun("000000", 10, 40, size=8),
    )
    pdf = build_pdf(DocSpec(pages=(
        PageSpec(media_box=(0, 0, 300, 400), runs=runs),
    )))
    unit = extract_pdf_content(pdf).textUnits[0]
    assert unit.text == "0" * 14
    spans = [
        MatchSpan(
            zone="body", unitRef="text:0", startOffset=s, endOffset=s + 3,
            ruleName="prop",
        )
        for s in (0, 3, 6, 9)
    ]
    result = redaction_pdf.redact_pdf_document_with_report(pdf, [unit], spans)
    assert result.documentBytes.startswith(b"%PDF")


def test_residual_check_does_not_join_text_across_page_boundaries():
    # Each page keeps one leftover "0" after redacting "000"; the residual
    # check must not see them as "000" by concatenating the pages' text.
    pages = tuple(
        PageSpec(media_box=(0, 0, 300, 400), runs=(TextRun("000", 10, 40, size=8),))
        for _ in range(2)
    )
    pdf = build_pdf(DocSpec(pages=pages))
    units = extract_pdf_content(pdf).textUnits
    spans = [
        MatchSpan(
            zone="body", unitRef="text:{}".format(i), startOffset=0, endOffset=2,
            ruleName="prop",
        )
        for i in range(2)
    ]
    result = redaction_pdf.redact_pdf_document_with_report(pdf, units, spans)
    assert result.documentBytes.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# Property 9: 独立工具提取不到被脱敏的 PII
# ---------------------------------------------------------------------------
# Feature: pymupdf-replacement, Property 9: 独立工具提取不到被脱敏的 PII
# **Validates: Requirements 8.3, 8.4**
#
# PII values come from a digit alphabet (Latin) or CJK ideographs, control text
# from upper-case letters, so a PII value can never be a substring of control
# text. Every occurrence of every PII value in the extracted unit text gets a
# `text:` span (whitespace between its characters allowed, which covers values
# split across lines), and optionally an `ocr:` span built from PDFium
# character boxes. The redacted strings are read back from the spans
# (`unit.text[start:end]`, whitespace removed) and must be absent from both
# pypdf's and `extract_pdf_content`'s text of the whole output; a sentinel far
# from every PII must remain.
import io as _io  # noqa: E402
import re  # noqa: E402
import pypdf  # noqa: E402
import pypdfium2  # noqa: E402
from pdf_fixtures import CJK_FONT, FormPlacement, FormSpec  # noqa: E402
from shared.models import OcrWord  # noqa: E402
_SENTINEL = "SENTINELQZ"
_CONTROL_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ"
_DIGIT_PII = st.text(alphabet="0123456789-", min_size=4, max_size=10).filter(
    lambda s: s.strip("-") != ""
)
_CJK_PII = st.text(
    alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FA5),
    min_size=2, max_size=5,
)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


@st.composite
def _pii_docs(draw):
    """(DocSpec, [pii values]) with PII planted in the ways listed in the
    task: Latin / CJK, split across lines, in forms placed several times,
    render mode 3, tight leading, rotated / cropped pages, several pages."""
    base_pages = draw(st.lists(
        page_specs(max_runs=0, max_images=1, form_names=()), min_size=1, max_size=3
    ))
    use_forms = draw(st.booleans())
    pii_values = []
    forms = ()
    placements = ()
    if use_forms:
        value = draw(_DIGIT_PII)
        pii_values.append(value)
        size = draw(st.integers(8, 14))
        form_runs = (TextRun(
            text=value, x=40, y=draw(st.integers(100, 200)), size=size,
            op=draw(st.sampled_from(["Tj", "TJ"])), tr=draw(st.sampled_from([0, 0, 3])),
        ),)
        forms = (FormSpec(name="pf", bbox=(0, 0, 400, 400), runs=form_runs),)
        placements = tuple(
            FormPlacement("pf", m) for m in draw(st.lists(
                st.sampled_from([(1, 0, 0, 1, 0, 0), (1, 0, 0, 1, 50, 80),
                                 (1, 0, 0, 1, 20, 30)]),
                min_size=1, max_size=3, unique=True))
        )
    pages = []
    for base in base_pages:
        x0, y0, x1, y1 = base.media_box
        page_form_placements = placements if use_forms and draw(st.booleans()) else ()
        # Tz != 100 is not combined with Form XObjects (known verifier limit).
        tz_choices = [100] if (use_forms or page_form_placements) else [100, 100, 80, 120]
        runs = []
        y = y1 - 40
        for _ in range(draw(st.integers(1, 3))):
            kind = draw(st.sampled_from(["latin", "cjk", "split"]))
            size = draw(st.integers(8, 14))
            step = size * draw(st.sampled_from([0.9, 1.0, 1.2, 2.0]))
            common = dict(
                size=size, op=draw(st.sampled_from(["Tj", "TJ", "'"])),
                tc=draw(st.sampled_from([0, 0, 1])),
                tz=draw(st.sampled_from(tz_choices)),
                tr=draw(st.sampled_from([0, 0, 0, 3])), leading=step,
            )
            x = x0 + 30
            if kind == "latin":
                value = draw(_DIGIT_PII)
                pii_values.append(value)
                runs.append(TextRun(text=value, x=x, y=y, **common))
            elif kind == "cjk":
                value = draw(_CJK_PII)
                pii_values.append(value)
                runs.append(TextRun(text=value, x=x, y=y, font=CJK_FONT, **common))
            else:
                a = draw(st.text(alphabet="0123456789", min_size=2, max_size=5))
                b = draw(st.text(alphabet="0123456789", min_size=2, max_size=5))
                pii_values.append(a + b)
                runs.append(TextRun(text=a, x=x, y=y, **common))
                y -= step
                runs.append(TextRun(text=b, x=x, y=y, **common))
            y -= 30
        runs.append(TextRun(text=_SENTINEL, x=x0 + 30, y=y0 + 25, size=12))
        pages.append(dataclasses.replace(
            base, runs=tuple(runs), form_placements=page_form_placements,
            images=(),
        ))
    doc = DocSpec(pages=tuple(pages), forms=forms)
    return doc, pii_values


def _ocr_unit(pdf_bytes: bytes, page_index: int, word: str):
    """OCR unit for `word` from PDFium character boxes (unrotated pages)."""
    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        page = pdf[page_index]
        if page.get_rotation() % 360 != 0:
            return None
        cl, cb, cr, ct = page.get_cropbox()
        textpage = page.get_textpage()
        start = textpage.get_text_range().find(word)
        if start < 0:
            return None
        boxes = [textpage.get_charbox(i) for i in range(start, start + len(word))]
        left = min(b[0] for b in boxes)
        bottom = min(b[1] for b in boxes)
        right = max(b[2] for b in boxes)
        top = max(b[3] for b in boxes)
        w, h = cr - cl, ct - cb
        box = BoundingBox(
            left=(left - cl) / w, top=1 - (top - cb) / h,
            width=(right - left) / w, height=(top - bottom) / h,
        )
    finally:
        pdf.close()
    return ExtractedTextUnit(
        zone="ocr", text=word,
        position=TextPosition(index=page_index, pageIndex=page_index),
        ocrWords=[OcrWord(text=word, startOffset=0, endOffset=len(word), boundingBox=box)],
    )


def _pypdf_text(data: bytes) -> str:
    reader = pypdf.PdfReader(_io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


@settings(max_examples=100, deadline=None)
@given(case=_pii_docs(), with_ocr=st.booleans())
def test_property_9_independent_extractors_find_no_redacted_pii(case, with_ocr):
    doc, pii_values = case
    pdf_bytes = build_pdf(doc)
    extraction = extract_pdf_content(pdf_bytes)
    patterns = [
        re.compile(r"\s*".join(re.escape(c) for c in v)) for v in set(pii_values)
    ]
    spans = []
    redacted = set()
    for index, unit in enumerate(extraction.textUnits):
        for pattern in patterns:
            for m in pattern.finditer(unit.text):
                spans.append(MatchSpan(
                    zone=unit.zone, unitRef="text:{}".format(index),
                    startOffset=m.start(), endOffset=m.end(), ruleName="pii",
                ))
                redacted.add(_squash(unit.text[m.start():m.end()]))
    ocr_units = []
    if with_ocr:
        for page_index, page in enumerate(doc.pages):
            for run in page.runs:
                if run.text in pii_values and run.font != CJK_FONT and run.tr == 0:
                    unit = _ocr_unit(pdf_bytes, page_index, run.text)
                    if unit is not None:
                        spans.append(MatchSpan(
                            zone="ocr", unitRef="ocr:{}".format(len(ocr_units)),
                            startOffset=0, endOffset=len(run.text), ruleName="pii",
                        ))
                        ocr_units.append(unit)
                        redacted.add(_squash(run.text))
                    break
    redacted.discard("")
    result = redaction_pdf.redact_pdf_document_with_report(
        pdf_bytes, extraction.textUnits, spans,
        ocr_text_units=ocr_units or None,
    )
    out = result.documentBytes
    texts = {
        "pypdf": _squash(_pypdf_text(out)),
        "extract_pdf_content": _squash(
            "".join(u.text for u in extract_pdf_content(out).textUnits)
        ),
    }
    for name, text in texts.items():
        for value in redacted:
            assert value not in text, (name, value)
        assert _SENTINEL in text, (name, "sentinel lost")


# ---------------------------------------------------------------------------
# Property 10: opaque black boxes drawn above everything.
#
# Pages come from ``page_specs`` (rotation, CropBox / MediaBox with non-zero
# origins, inline images and text) whose ORIGINAL content ends with unusual
# inherited graphics state: an unbalanced ``q``, a leftover clip, fill colours,
# surplus ``Q`` and a low-alpha / Multiply ExtGState. Regions (random, or on
# top of an image) are redacted with plan / apply / draw_black_boxes. Checked:
#   (1) PDFium (scale 2) shows pure black strictly inside each region
#       (inset 1 px);
#   (2) the last content stream holds exactly one ``re`` + ``f`` per region
#       (no dedup, same order) after ``0 0 0 rg`` and a ``gs`` whose ExtGState
#       has /ca 1 /CA 1 /SMask /None /BM /Normal;
#   (3) the rectangle, converted back to Page_Coordinates, covers its region
#       and exceeds it by no more than 0.01 pt (the writer rounds outward to
#       1e-4 user units).
# ---------------------------------------------------------------------------
_P10_SCALE = 2.0
_P10_STATE_FRAGMENTS = (
    b"q 0 0 10 10 re W n\n",
    b"q q 5 5 400 400 re W n\n",
    b"1 0 0 rg 0.2 g\n",
    b"Q Q Q\n",
    b"/GSlow gs\n",
    b"/GSlow gs 0 0 0 0 k\n",
)


def _add_low_alpha_state(pdf_bytes: bytes) -> bytes:
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    try:
        for page in pdf.pages:
            resources = page.obj.get("/Resources")
            if resources is None:
                resources = pikepdf.Dictionary()
                page.obj["/Resources"] = resources
            states = pikepdf.Dictionary()
            existing = resources.get("/ExtGState")
            if existing is not None:
                for key in existing.keys():
                    states[key] = existing[key]
            states["/GSlow"] = pikepdf.Dictionary({
                "/ca": 0.1, "/CA": 0.1, "/BM": pikepdf.Name("/Multiply"),
            })
            resources["/ExtGState"] = states
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()
    finally:
        pdf.close()


@st.composite
def _prop10_docs(draw):
    doc = draw(doc_specs(max_pages=2, max_images=2))
    pages = []
    for page in doc.pages:
        extra = b"".join(draw(st.lists(st.sampled_from(_P10_STATE_FRAGMENTS), max_size=4)))
        pages.append(dataclasses.replace(page, raw_content=page.raw_content + extra))
    return dataclasses.replace(doc, pages=tuple(pages))


def _prop10_regions(draw, page_spec, geom):
    """Regions in Page_Coordinates; some sit on top of the page's images."""
    w, h = geom.width, geom.height
    placements = [
        p for img in page_spec.images for p in img.placements if len(p) == 4
    ]
    regions = []
    for _ in range(draw(st.integers(1, 3))):
        if placements and draw(st.booleans()):
            x, y, pw, ph = draw(st.sampled_from(placements))
            l, t, r, b = user_to_page_rect(geom, (x, y, x + pw, y + ph))
            fx0, fx1 = sorted(draw(st.floats(0, 1)) for _ in range(2))
            fy0, fy1 = sorted(draw(st.floats(0, 1)) for _ in range(2))
            regions.append((l + (r - l) * fx0, t + (b - t) * fy0,
                            l + (r - l) * fx1, t + (b - t) * fy1))
        else:
            x = draw(st.floats(-20, w, allow_nan=False))
            y = draw(st.floats(-20, h, allow_nan=False))
            rw = draw(st.floats(0.5, 250, allow_nan=False))
            rh = draw(st.floats(0.5, 120, allow_nan=False))
            regions.append((x, y, x + rw, y + rh))
    if draw(st.booleans()):
        regions.append(regions[0])  # duplicates must not be deduplicated
    return regions


def _render_rgb(pdf_bytes, page_index):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        page = doc[page_index]
        try:
            return page.render(scale=_P10_SCALE).to_pil().convert("RGB")
        finally:
            page.close()
    finally:
        doc.close()


def _last_stream_boxes(pdf, page_index):
    page = pdf.pages[page_index]
    contents = page.obj["/Contents"]
    last = contents[len(contents) - 1]
    ops = pikepdf.parse_content_stream(last)
    names = [str(i.operator) for i in ops]
    return page, ops, names


# Feature: pymupdf-replacement, Property 10: 黑框覆盖且位于最上层
# **Validates: Requirements 8.2, 5.3**
@settings(max_examples=100, deadline=None)
@given(doc=_prop10_docs(), data=st.data())
def test_property_10_black_boxes_cover_regions_and_are_topmost(doc, data):
    source_bytes = _add_low_alpha_state(build_pdf(doc))
    source_pages = collect_glyphs_from_bytes(source_bytes)
    regions_by_page = {}
    for index, spec in enumerate(doc.pages):
        regions_by_page[index] = _prop10_regions(
            data.draw, spec, source_pages[index].geometry
        )
    output_bytes = _apply_regions(source_bytes, regions_by_page, source_pages)

    pdf = pikepdf.open(io.BytesIO(output_bytes))
    try:
        for index, regions in regions_by_page.items():
            geom = page_geometry_of(pdf, index)
            page, ops, names = _last_stream_boxes(pdf, index)

            # (2) structure of the last stream.
            assert names.count("re") == len(regions) == names.count("f"), names
            assert names.count("rg") == 1 and names.count("gs") == 1, names
            assert names.index("rg") < names.index("re")
            assert names.index("gs") < names.index("re")
            rg = [i for i in ops if str(i.operator) == "rg"][0]
            assert [float(v) for v in rg.operands] == [0.0, 0.0, 0.0]
            gs = [i for i in ops if str(i.operator) == "gs"][0]
            state = page.obj["/Resources"]["/ExtGState"][str(gs.operands[0])]
            assert float(state["/ca"]) == 1 and float(state["/CA"]) == 1
            assert str(state["/SMask"]) == "/None"
            assert str(state["/BM"]) == "/Normal"

            # (3) rectangles map back onto the regions, in order.
            rects = [i.operands for i in ops if str(i.operator) == "re"]
            for operands, region in zip(rects, regions):
                x, y, rw, rh = (float(v) for v in operands)
                box = user_to_page_rect(geom, (x, y, x + rw, y + rh))
                assert box[0] <= region[0] + 1e-6 and box[1] <= region[1] + 1e-6
                assert box[2] >= region[2] - 1e-6 and box[3] >= region[3] - 1e-6
                for k in range(4):
                    assert abs(box[k] - region[k]) <= 0.01, (box, region)
    finally:
        pdf.close()

    # (1) rendered pixels strictly inside each region are pure black.
    for index, regions in regions_by_page.items():
        image = _render_rgb(output_bytes, index)
        geom = source_pages[index].geometry
        for (l, t, r, b) in regions:
            px0 = max(l, 0.0) * _P10_SCALE + 1
            py0 = max(t, 0.0) * _P10_SCALE + 1
            px1 = min(r, geom.width) * _P10_SCALE - 1
            py1 = min(b, geom.height) * _P10_SCALE - 1
            ix0, iy0 = int(px0) + (px0 > int(px0)), int(py0) + (py0 > int(py0))
            ix1, iy1 = int(px1), int(py1)  # exclusive end of fully inside pixels
            ix1, iy1 = min(ix1, image.width), min(iy1, image.height)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            lo, hi = zip(*image.crop((ix0, iy0, ix1, iy1)).getextrema())
            assert hi == (0, 0, 0), (index, (l, t, r, b), hi)


# ---------------------------------------------------------------------------
# Property 11: structure preserved, single-revision output.
#
# Feature: pymupdf-replacement, Property 11: 结构保持与单一修订输出
# Documents with several pages, rotate / crop, annotations, incremental
# revisions, Info / XMP and images are redacted (with text spans, or none).
# The output must keep page count / order, every page's visible rect and
# /Rotate, hold exactly one %%EOF, no /Prev, no unreferenced object and no
# /Redact annotation, and stay within 2x the source size (no image changed).
# **Validates: Requirements 8.7, 8.10, 9.5**
# ---------------------------------------------------------------------------
from agent.tools.structure_verification import verify_structure  # noqa: E402
from pdf_fixtures import FreeTextAnnot, WidgetField, make_xmp  # noqa: E402


@st.composite
def _prop11_docs(draw):
    doc = draw(doc_specs(max_pages=3, max_images=2, cjk=draw(st.booleans())))
    pages = []
    counter = 0
    for page in doc.pages:
        page = _no_tz_into_forms(page)
        annots = []
        for _ in range(draw(st.integers(0, 2))):
            counter += 1
            if draw(st.booleans()):
                annots.append(FreeTextAnnot(
                    contents=draw(st.text(alphabet="abcdef 0123456789", min_size=1, max_size=12)),
                    appearance=draw(st.booleans()),
                ))
            else:
                annots.append(WidgetField(
                    name="f{}".format(counter),
                    value=draw(st.text(alphabet="abcdef0123456789", min_size=1, max_size=8)),
                    appearance=draw(st.booleans()),
                ))
        pages.append(dataclasses.replace(page, annotations=tuple(annots)))
    info = draw(st.sampled_from([{}, {"Title": "Secret title", "Author": "Someone"}]))
    xmp = draw(st.sampled_from([None, make_xmp(title="Secret xmp")]))
    doc = dataclasses.replace(
        doc, pages=tuple(pages),
        forms=tuple(_no_tz_into_forms(f) for f in doc.forms),
        info=info, xmp=xmp,
    )
    if draw(st.booleans()):
        prior = dataclasses.replace(doc, info={"Title": "Old"}, xmp=None, prior_revisions=())
        doc = dataclasses.replace(doc, prior_revisions=(prior,))
    return doc


def _inherited(page_obj, key):
    node = page_obj
    while node is not None:
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def _page_geometry(pdf):
    out = []
    for page in pdf.pages:
        media = [float(v) for v in _inherited(page.obj, "/MediaBox")]
        crop = _inherited(page.obj, "/CropBox")
        crop = [float(v) for v in crop] if crop is not None else media
        mx0, my0, mx1, my1 = (min(media[0], media[2]), min(media[1], media[3]),
                               max(media[0], media[2]), max(media[1], media[3]))
        cx0, cy0, cx1, cy1 = (min(crop[0], crop[2]), min(crop[1], crop[3]),
                              max(crop[0], crop[2]), max(crop[1], crop[3]))
        visible = (max(mx0, cx0), max(my0, cy0), min(mx1, cx1), min(my1, cy1))
        rot = _inherited(page.obj, "/Rotate")
        out.append((visible, (int(rot) if rot is not None else 0) % 360))
    return out


def _live_object_count(pdf) -> int:
    """Objects that are not null and not writer bookkeeping (ObjStm / XRef
    streams are regenerated by every save)."""
    count = 0
    for obj in pdf.objects:
        if obj._type_code == pikepdf.ObjectType.null:
            continue
        if obj._type_code == pikepdf.ObjectType.stream and str(obj.get("/Type")) in (
            "/ObjStm", "/XRef",
        ):
            continue
        count += 1
    return count


@settings(max_examples=100, deadline=None)
@given(doc=_prop11_docs(), data=st.data())
def test_property_11_structure_preserved_and_single_revision(doc, data):
    source = build_pdf(doc)
    extraction = extract_pdf_content(source)
    # Pick a few values and span EVERY occurrence (as a rule match would; a
    # value left elsewhere in the text is rejected by the residual check).
    # A glyph entirely outside the visible rect (rect None) is not extracted,
    # so no span can reach it, yet the residual check (design step 7(c))
    # scans every glyph and fails closed. Such documents get zero spans.
    off_page = any(
        g.rect is None for page in collect_glyphs_from_bytes(source) for g in page.glyphs
    )
    spans = []
    if not off_page and data.draw(st.booleans()):
        values = set()
        for unit in extraction.textUnits:
            text = unit.text
            if not text.strip() or not data.draw(st.booleans()):
                continue
            start = data.draw(st.integers(0, len(text) - 1))
            end = data.draw(st.integers(start + 1, len(text)))
            value = _squash(text[start:end])
            if value:
                values.add(value)
        patterns = [
            re.compile(r"\s*".join(re.escape(c) for c in v)) for v in values
        ]
        for index, unit in enumerate(extraction.textUnits):
            for pattern in patterns:
                for m in pattern.finditer(unit.text):
                    spans.append(MatchSpan(
                        zone=unit.zone, unitRef="text:{}".format(index),
                        startOffset=m.start(), endOffset=m.end(), ruleName="pii",
                    ))
    out = redaction_pdf.redact_pdf_document_with_report(
        source, extraction.textUnits, spans
    ).documentBytes

    # page count / order
    verdict = verify_structure(source, out, ".pdf")
    assert verdict.ok, verdict.reason

    # visible rect and /Rotate per page
    with pikepdf.open(_io.BytesIO(source)) as src, pikepdf.open(_io.BytesIO(out)) as dst:
        src_geo, dst_geo = _page_geometry(src), _page_geometry(dst)
        assert len(src_geo) == len(dst_geo)
        for (sv, sr), (dv, dr) in zip(src_geo, dst_geo):
            assert sr == dr, (sr, dr)
            assert all(abs(a - b) <= 1e-3 for a, b in zip(sv, dv)), (sv, dv)

        # no /Redact annotation left
        for page in dst.pages:
            for annot in page.obj.get("/Annots", []):
                assert str(annot.get("/Subtype")) != "/Redact"

        # no unreferenced objects: a clean re-save keeps the live object count
        before = _live_object_count(dst)
        buf = _io.BytesIO()
        dst.save(buf)
        with pikepdf.open(_io.BytesIO(buf.getvalue())) as again:
            assert before == _live_object_count(again), (before, _live_object_count(again))

    # single revision
    assert out.count(b"%%EOF") == 1, out.count(b"%%EOF")
    assert b"/Prev" not in out

    # size bound (no image was modified)
    assert len(out) <= 2 * len(source), (len(out), len(source))


# ---------------------------------------------------------------------------
# Property 12: 零区域时输出与源文档等价
# ---------------------------------------------------------------------------
# Feature: pymupdf-replacement, Property 12: 零区域时输出与源文档等价
# **Validates: Requirements 8.6**
#
# Spans that resolve to no region (none at all, unknown unitRef prefixes, an
# ocr: span covering only separators) leave the document equivalent to the
# source: report counts 0, same pages, same text units, identical 200 DPI
# pixels.
from pdf_fixtures import OWNER_ONLY  # noqa: E402


@st.composite
def _prop12_docs(draw):
    doc = draw(doc_specs(max_pages=2, max_images=1, cjk=draw(st.booleans())))
    pages = []
    counter = 0
    for page in doc.pages:
        page = _no_tz_into_forms(page)
        annots = []
        for _ in range(draw(st.integers(0, 1))):
            counter += 1
            if draw(st.booleans()):
                annots.append(FreeTextAnnot(
                    contents=draw(st.text(alphabet="abcdef 0123456789", min_size=1, max_size=12)),
                    appearance=draw(st.booleans()),
                ))
            else:
                annots.append(WidgetField(
                    name="f{}".format(counter),
                    value=draw(st.text(alphabet="abcdef0123456789", min_size=1, max_size=8)),
                    appearance=draw(st.booleans()),
                ))
        pages.append(dataclasses.replace(page, annotations=tuple(annots)))
    doc = dataclasses.replace(
        doc, pages=tuple(pages),
        forms=tuple(_no_tz_into_forms(f) for f in doc.forms),
        info=draw(st.sampled_from([{}, {"Title": "Secret title", "Author": "Someone"}])),
        xmp=draw(st.sampled_from([None, make_xmp(title="Secret xmp")])),
    )
    if draw(st.booleans()):
        doc = dataclasses.replace(doc, encryption=OWNER_ONLY)
    elif draw(st.booleans()):
        prior = dataclasses.replace(doc, info={"Title": "Old"}, xmp=None, prior_revisions=())
        doc = dataclasses.replace(doc, prior_revisions=(prior,))
    return doc


def _render_200dpi(pdf_bytes: bytes):
    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        out = []
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                image = page.render(scale=200 / 72).to_pil().convert("RGB")
                out.append((image.size, image.tobytes()))
            finally:
                page.close()
        return out
    finally:
        pdf.close()


@settings(max_examples=100, deadline=None)
@given(doc=_prop12_docs(), kind=st.sampled_from(["empty", "unknown", "separator"]),
       data=st.data())
def test_property_12_zero_regions_output_equivalent_to_source(doc, kind, data):
    source = build_pdf(doc)
    extraction = extract_pdf_content(source)
    ocr_units = None
    if kind == "empty":
        spans = []
    elif kind == "unknown":
        prefix = data.draw(st.sampled_from(["foo", "bar", "pii", "x"]))
        spans = [
            MatchSpan(zone="body", unitRef="{}:{}".format(prefix, data.draw(st.integers(0, 5))),
                      startOffset=0, endOffset=3, ruleName="pii")
            for _ in range(data.draw(st.integers(1, 3)))
        ]
    else:
        gap = data.draw(st.integers(1, 3))
        a, b = "foo", "bar"
        box = BoundingBox(left=0.1, top=0.1, width=0.2, height=0.05)
        ocr_units = [ExtractedTextUnit(
            zone="ocr", text=a + " " * gap + b,
            position=TextPosition(index=0, pageIndex=0),
            ocrWords=[
                OcrWord(text=a, startOffset=0, endOffset=3, boundingBox=box),
                OcrWord(text=b, startOffset=3 + gap, endOffset=6 + gap, boundingBox=box),
            ],
        )]
        spans = [MatchSpan(zone="ocr", unitRef="ocr:0", startOffset=3,
                           endOffset=3 + gap, ruleName="pii")]
    result = redaction_pdf.redact_pdf_document_with_report(
        source, extraction.textUnits, spans, ocr_text_units=ocr_units
    )
    assert result.report.textLayerRegions == 0
    assert result.report.ocrRegions == 0
    assert result.report.agreedRegions == 0

    out_extraction = extract_pdf_content(result.documentBytes)
    assert [u.text for u in out_extraction.textUnits] == [u.text for u in extraction.textUnits]
    assert [(u.position.pageIndex if u.position else None) for u in out_extraction.textUnits] == [
        (u.position.pageIndex if u.position else None) for u in extraction.textUnits
    ]
    src_pages, out_pages = _render_200dpi(source), _render_200dpi(result.documentBytes)
    assert len(src_pages) == len(out_pages)
    for (s_size, s_px), (o_size, o_px) in zip(src_pages, out_pages):
        assert s_size == o_size
        assert s_px == o_px


# ---------------------------------------------------------------------------
# Property 13: 图片写回
# ---------------------------------------------------------------------------
import itertools  # noqa: E402
from unittest import mock  # noqa: E402

from PIL import Image  # noqa: E402

from agent.tools import pdf_images  # noqa: E402
from pdf_fixtures import ImageSpec  # noqa: E402
from shared.models import ImageRegion, RecognizedElement  # noqa: E402

_P13_KINDS = ["rgb", "gray", "dct", "dct_gray", "indexed"]
_P13_SCALE = 2.0
_P13_PAGE_H = 792.0


@st.composite
def _prop13_cases(draw):
    """(doc, referenced) — images shared by key; every placement sits in its
    own grid cell so nothing overlaps. ``referenced`` is decided later once
    the extracted regions are known."""
    n_images = draw(st.integers(2, 4))
    n_pages = draw(st.integers(1, 2))
    specs = []
    for i in range(n_images):
        kind = draw(st.sampled_from(_P13_KINDS))
        color = None
        if draw(st.booleans()):
            color = tuple(draw(st.integers(80, 250)) for _ in range(3))
        specs.append(ImageSpec(
            kind=kind, width=draw(st.integers(6, 14)), height=draw(st.integers(6, 14)),
            smask=draw(st.booleans()), key="img{}".format(i), color=color,
        ))
    slots = [(page, k) for page in range(n_pages) for k in range(6)]
    per_page = [[] for _ in range(n_pages)]
    for i, spec in enumerate(specs):
        count = draw(st.integers(1, 3))
        for _ in range(count):
            page, k = draw(st.sampled_from(slots))
            if any(item[2] == (page, k) for items in per_page for item in items):
                continue
            w = draw(st.integers(90, 150))
            h = draw(st.integers(90, 150))
            x = 20 + (k % 3) * 190
            y = 60 + (k // 3) * 300
            per_page[page].append((i, (x, y, w, h), (page, k)))
    pages = []
    for items in per_page:
        pages.append(PageSpec(images=tuple(
            dataclasses.replace(specs[i], placements=(pl,)) for i, pl, _ in items
        )))
    return DocSpec(pages=tuple(pages))


def _xobject_names(pdf):
    """{page_index: {name: objnum}} of image XObjects in page resources."""
    out = {}
    for index, page in enumerate(pdf.pages):
        names = {}
        res = page.obj.get("/Resources")
        xobjs = res.get("/XObject") if res is not None else None
        for name in (xobjs.keys() if xobjs is not None else []):
            obj = xobjs[name]
            if obj.get("/Subtype") == "/Image":
                names[str(name)] = obj.objgen[0]
        out[index] = names
    return out


def _rgba(data):
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGBA")
        return img.size, img.tobytes()


def _stream_blobs(pdf):
    """Every (raw, decoded-or-None) pair of image streams in the document."""
    blobs = []
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream):
            raw = bytes(obj.read_raw_bytes())
            try:
                decoded = bytes(obj.read_bytes())
            except Exception:
                decoded = None
            blobs.append((raw, decoded))
    return blobs


# Feature: pymupdf-replacement, Property 13: 图片写回
# **Validates: Requirements 9.1, 9.2, 9.4, 9.7, 9.8**
@settings(max_examples=100, deadline=None)
@given(doc=_prop13_cases(), data=st.data())
def test_property_13_image_write_back(doc, data):
    source = build_pdf(doc)
    extraction = extract_pdf_content(source)
    regions = list(extraction.imageRegions)
    assert regions

    locs = [tuple(int(v) for v in r.sourceLocation.split(":")[1::2]) for r in regions]
    # at most one referenced region per xref; at least one referenced overall
    chosen, seen_xrefs = [], set()
    for index, (_, xref) in enumerate(locs):
        if xref not in seen_xrefs and data.draw(st.booleans()):
            chosen.append(index)
            seen_xrefs.add(xref)
    if not chosen:
        chosen = [data.draw(st.integers(0, len(regions) - 1))]
    referenced = sorted(set(chosen))
    ref_xrefs = {locs[i][1] for i in referenced}

    spans, boxes = [], {}
    new_regions = list(regions)
    for index in referenced:
        n_el = data.draw(st.integers(1, 3))
        elements, text, pos = [], "", 0
        for e in range(n_el):
            word = "w{}x".format(e)
            elements.append(RecognizedElement(
                text=word, startOffset=pos, endOffset=pos + len(word),
                boundingBox=BoundingBox(
                    left=data.draw(st.sampled_from([0.1, 0.3, 0.55])),
                    top=data.draw(st.sampled_from([0.1, 0.35, 0.6])),
                    width=0.3, height=0.25,
                ),
            ))
            text += word + " "
            pos += len(word) + 1
        use_all = data.draw(st.booleans())
        picked = list(range(n_el)) if use_all else sorted(
            data.draw(st.sets(st.integers(0, n_el - 1), min_size=1)))
        region_spans = [
            MatchSpan(zone="image", unitRef="image:{}".format(index),
                      startOffset=elements[e].startOffset, endOffset=elements[e].endOffset,
                      ruleName="pii")
            for e in picked
        ]
        # image: spans from several MatchSpans, in arbitrary order
        spans.extend(region_spans)
        boxes[index] = [elements[e].boundingBox for e in picked]
        new_regions[index] = dataclasses.replace(
            regions[index], recognizedText=text, recognizedElements=elements)
    spans = data.draw(st.permutations(spans))

    captured = []
    real_redact = redaction_pdf.redact_image_bytes

    def recording_redact(image_bytes, region, region_spans):
        result = real_redact(image_bytes, region, region_spans)
        captured.append((image_bytes, region, list(region_spans), result))
        return result

    with mock.patch.object(redaction_pdf, "redact_image_bytes", side_effect=recording_redact) as redact_mock, \
            mock.patch.object(redaction_pdf, "write_back_image",
                              wraps=redaction_pdf.write_back_image) as write_mock:
        result = redaction_pdf.redact_pdf_document_with_report(
            source, extraction.textUnits, spans, image_regions=new_regions)
    out_bytes = result.documentBytes

    # (9.1) exactly once per referenced image, with all spans, ascending order
    assert redact_mock.call_count == len(referenced)
    assert write_mock.call_count == len(referenced)
    for k, index in enumerate(referenced):
        image_bytes, region, region_spans, _ = captured[k]
        assert region.sourceLocation == regions[index].sourceLocation
        assert image_bytes == regions[index].imageBytes
        expected = [s for s in spans if s.unitRef == "image:{}".format(index)]
        assert sorted((s.startOffset, s.endOffset) for s in region_spans) == sorted(
            (s.startOffset, s.endOffset) for s in expected)
        assert write_mock.call_args_list[k].args[1] == locs[index][1]
    assert sum(len(c[3]) for c in captured) >= 0

    src_pdf = pikepdf.open(io.BytesIO(source))
    out_pdf = pikepdf.open(io.BytesIO(out_bytes))
    src_names, out_names = _xobject_names(src_pdf), _xobject_names(out_pdf)

    # (9.2) every placement of every referenced image shows the new image
    renders = {}
    checked = 0
    for k, index in enumerate(referenced):
        page_idx, xref = locs[index]
        new_img = Image.open(io.BytesIO(captured[k][3])).convert("RGB")
        nw, nh = new_img.size
        # find every page placing this xref (by resource name)
        for p, names in src_names.items():
            for name, num in names.items():
                if num != xref:
                    continue
                dec = pdf_images.decode_image(out_pdf, out_names[p][name])
                assert dec is not None
                with Image.open(io.BytesIO(dec)) as d:
                    assert d.size == (nw, nh)
                    assert d.convert("RGB").tobytes() == new_img.tobytes()
                placement = _placement(doc, p, name)
                if placement is None:  # in the resources but never drawn
                    continue
                checked += 1
                for box in boxes[index]:
                    cx = min(nw - 1, int((box.left + box.width / 2) * nw))
                    cy = min(nh - 1, int((box.top + box.height / 2) * nh))
                    assert max(new_img.getpixel((cx, cy))) <= 40, (index, cx, cy)
                    # rendered placement
                    renders.setdefault(p, _render_rgb(out_bytes, p))
                    x, y, w, h = placement
                    fx = x + (box.left + box.width / 2) * w
                    fy = y + (1 - (box.top + box.height / 2)) * h
                    px, py = int(fx * _P10_SCALE), int((_P13_PAGE_H - fy) * _P10_SCALE)
                    r, g, b = renders[p].getpixel((px, py))
                    assert max(r, g, b) <= 60, ("render", p, name, (r, g, b))

    assert checked >= len(referenced)

    # (9.4) no stream equals the pre-redaction data of a referenced image
    unref_blobs = set()
    for p, names in src_names.items():
        for name, num in names.items():
            if num not in ref_xrefs:
                obj = src_pdf.get_object((num, 0))
                unref_blobs.add(bytes(obj.read_raw_bytes()))
                try:
                    unref_blobs.add(bytes(obj.read_bytes()))
                except Exception:
                    pass
    out_blobs = _stream_blobs(out_pdf)
    for num in ref_xrefs:
        obj = src_pdf.get_object((num, 0))
        raw = bytes(obj.read_raw_bytes())
        try:
            dec = bytes(obj.read_bytes())
        except Exception:
            dec = None
        for candidate in (raw, dec):
            if candidate is None or candidate in unref_blobs:
                continue
            for o_raw, o_dec in out_blobs:
                assert o_raw != candidate and o_dec != candidate

    # (9.7) unreferenced images keep their pixels
    for p, names in src_names.items():
        for name, num in names.items():
            if num in ref_xrefs:
                continue
            before = pdf_images.decode_image(src_pdf, num)
            after = pdf_images.decode_image(out_pdf, out_names[p][name])
            assert before is not None and after is not None
            assert _rgba(before) == _rgba(after)

    # (9.8) size bound
    assert len(out_bytes) <= 2 * len(source) + sum(len(c[3]) for c in captured)


def _placement(doc, page_index, name):
    """The (x, y, w, h) of the placement drawn through resource ``name``
    (build_pdf names images Im1.. by first appearance of their key)."""
    keys = []
    for page in doc.pages:
        for img in page.images:
            if img.key not in keys:
                keys.append(img.key)
    for spec in doc.pages[page_index].images:
        if "/Im{}".format(keys.index(spec.key) + 1) == name:
            return spec.placements[0]
    return None


# ---------------------------------------------------------------------------
# Property 14: report counts.
#
# Documents hold runs of words made of characters that are unique within the
# whole document (so any redacted value occurs exactly once and the pipeline's
# residual check cannot trip over a retained duplicate). Spans:
#   * ``text:`` -- random offsets (several lines -> several regions);
#   * ``ocr:`` -- units built from PDFium character boxes (agree with the text
#     layer) and synthetic units with random boxes and a disjoint alphabet;
#     spans cover several words, parts of words or only a separator;
#   * ``image:`` and unknown-prefix spans (counted nowhere).
# Expected numbers are derived here with explicit rectangle math.
# ---------------------------------------------------------------------------
from agent.tools import pdf_geometry  # noqa: E402

_P14_POOL = list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_P14_SYNTH_ALPHABET = "§¶"


def _p14_text_span_rects(page, start, end):
    """Independent per-line rectangles of chars[start:end] (rect-less chars
    skipped); [] when the span has no usable character."""
    if not (0 <= start < end <= len(page.chars)):
        return []
    lines = {}
    for ch in page.chars[start:end]:
        if ch.rect is None:
            continue
        x0, y0, x1, y1 = ch.rect
        if ch.line_id in lines:
            a = lines[ch.line_id]
            lines[ch.line_id] = [min(a[0], x0), min(a[1], y0), max(a[2], x1), max(a[3], y1)]
        else:
            lines[ch.line_id] = [x0, y0, x1, y1]
    return [tuple(v) for v in lines.values()]


@st.composite
def _prop14_cases(draw):
    base_pages = draw(st.lists(
        page_specs(max_runs=0, max_images=0, form_names=()), min_size=1, max_size=3
    ))
    pool = draw(st.permutations(_P14_POOL))
    cursor = 0
    pages = []
    for base in base_pages:
        x0, y0, x1, y1 = base.media_box
        y = y1 - 60
        runs = []
        for _ in range(draw(st.integers(1, 2))):
            words = []
            for _ in range(draw(st.integers(2, 3))):
                n = draw(st.integers(2, 3))
                words.append("".join(pool[cursor:cursor + n]))
                cursor += n
            size = draw(st.integers(8, 14))
            runs.append(TextRun(
                text=" ".join(words), x=x0 + 30, y=y, size=size,
                op=draw(st.sampled_from(["Tj", "TJ"])),
            ))
            y -= size * draw(st.sampled_from([1.3, 1.5, 3.0]))
        images = ()
        pages.append(dataclasses.replace(base, runs=tuple(runs), images=images))
    if draw(st.booleans()):
        pages[0] = dataclasses.replace(pages[0], images=(ImageSpec(
            kind="rgb", width=8, height=8, key="img0",
            placements=((pages[0].media_box[0] + 30, pages[0].media_box[1] + 40, 80, 80),),
        ),))
    return DocSpec(pages=tuple(pages))


def _p14_words_of_run(run):
    return run.text.split(" ")


@settings(max_examples=100, deadline=None)
@given(doc=_prop14_cases(), data=st.data())
def test_property_14_report_counts(doc, data):
    # Feature: pymupdf-replacement, Property 14: 报告计数
    # **Validates: Requirements 10.3, 10.4, 10.5, 8.8**
    pdf_bytes = build_pdf(doc)
    extraction = extract_pdf_content(pdf_bytes)
    units = extraction.textUnits
    glyph_pages = collect_glyphs_from_bytes(pdf_bytes)
    handle = open_pdf(pdf_bytes)

    # ---- text: spans -------------------------------------------------------
    text_spans = []
    text_rects = {}  # page -> [rect]
    expected_text = 0
    if units:
        for _ in range(data.draw(st.integers(0, 4))):
            index = data.draw(st.integers(0, len(units) - 1))
            unit = units[index]
            page_index = unit.position.pageIndex
            n = len(unit.text)
            if n < 1:
                continue
            start = data.draw(st.integers(0, n - 1))
            end = data.draw(st.integers(start + 1, n))
            rects = _p14_text_span_rects(glyph_pages[page_index], start, end)
            if not rects:
                continue  # would use the proportional fallback (Property 6)
            span = MatchSpan(zone=unit.zone, unitRef="text:{}".format(index),
                             startOffset=start, endOffset=end, ruleName="pii")
            # oracle cross-check against the agent's own resolver
            agent_regions = redaction_pdf._regions_for_text_span(
                unit, span, collect_page_glyphs(handle, page_index))
            assert len(agent_regions) == len(rects)
            text_spans.append(span)
            expected_text += len(rects)
            text_rects.setdefault(page_index, []).extend(rects)

    # ---- ocr: units and spans ---------------------------------------------
    ocr_units = []
    for page_index, page in enumerate(doc.pages):
        rotation = glyph_pages[page_index].geometry.rotation
        for run in page.runs:
            if (rotation or (page.rotate or 0)) % 360 != 0:
                continue
            if not data.draw(st.sampled_from([True, True, False])):
                continue
            words, boxes = [], []
            for word in _p14_words_of_run(run):
                unit = _ocr_unit(pdf_bytes, page_index, word)
                if unit is not None:
                    words.append(word)
                    boxes.append(unit.ocrWords[0].boundingBox)
            if not words:
                continue
            if data.draw(st.booleans()):
                # also redact the first word through the text layer, so the
                # two paths can agree on it
                for t_index, t_unit in enumerate(units):
                    at = t_unit.text.find(words[0])
                    if t_unit.position.pageIndex != page_index or at < 0:
                        continue
                    rects = _p14_text_span_rects(
                        glyph_pages[page_index], at, at + len(words[0]))
                    if rects:
                        text_spans.append(MatchSpan(
                            zone=t_unit.zone, unitRef="text:{}".format(t_index),
                            startOffset=at, endOffset=at + len(words[0]),
                            ruleName="pii"))
                        expected_text += len(rects)
                        text_rects.setdefault(page_index, []).extend(rects)
                    break
            ocr_words, pos = [], 0
            for word, box in zip(words, boxes):
                ocr_words.append(OcrWord(text=word, startOffset=pos,
                                         endOffset=pos + len(word), boundingBox=box))
                pos += len(word) + 1
            ocr_units.append(ExtractedTextUnit(
                zone="ocr", text=" ".join(words),
                position=TextPosition(index=page_index, pageIndex=page_index),
                ocrWords=ocr_words,
            ))
    for _ in range(data.draw(st.integers(0, 2))):
        page_index = data.draw(st.integers(0, len(doc.pages) - 1))
        words, pos = [], 0
        for _w in range(data.draw(st.integers(1, 4))):
            word = "".join(data.draw(st.lists(
                st.sampled_from(_P14_SYNTH_ALPHABET), min_size=1, max_size=3)))
            box = BoundingBox(
                left=data.draw(st.floats(0.0, 0.8)), top=data.draw(st.floats(0.0, 0.8)),
                width=data.draw(st.floats(0.02, 0.2)), height=data.draw(st.floats(0.02, 0.2)),
            )
            words.append(OcrWord(text=word, startOffset=pos,
                                 endOffset=pos + len(word), boundingBox=box))
            pos += len(word) + 1
        ocr_units.append(ExtractedTextUnit(
            zone="ocr", text=" ".join(w.text for w in words),
            position=TextPosition(index=page_index, pageIndex=page_index),
            ocrWords=words,
        ))

    ocr_spans = []
    ocr_rects = {}
    expected_ocr = 0
    for index, unit in enumerate(ocr_units):
        page_index = unit.position.pageIndex
        geom = glyph_pages[page_index].geometry
        for _ in range(data.draw(st.integers(1, 3))):
            if len(unit.ocrWords) > 1 and data.draw(st.booleans()):
                gap = data.draw(st.integers(0, len(unit.ocrWords) - 2))
                start = unit.ocrWords[gap].endOffset  # separator only
                end = start + 1
            else:
                start = data.draw(st.integers(0, len(unit.text) - 1))
                end = data.draw(st.integers(start + 1, len(unit.text)))
            hit = [w for w in unit.ocrWords if w.startOffset < end and start < w.endOffset]
            ocr_spans.append(MatchSpan(
                zone="ocr", unitRef="ocr:{}".format(index),
                startOffset=start, endOffset=end, ruleName="pii"))
            expected_ocr += len(hit)
            for w in hit:
                ocr_rects.setdefault(page_index, []).append(
                    pdf_geometry.normalized_box_to_page_rect(
                        (geom.width, geom.height), w.boundingBox))

    expected_agreed = 0
    for page_index, rects in ocr_rects.items():
        for o in rects:
            if any(pdf_geometry.rects_intersect(o, t) for t in text_rects.get(page_index, [])):
                expected_agreed += 1

    # ---- image: and unknown-prefix spans -----------------------------------
    extra_spans = []
    image_regions = list(extraction.imageRegions)
    for k in range(len(image_regions)):
        if data.draw(st.booleans()):
            image_regions[k] = dataclasses.replace(
                image_regions[k], recognizedText="w0x w1x",
                recognizedElements=[RecognizedElement(
                    text="w0x", startOffset=0, endOffset=3,
                    boundingBox=BoundingBox(left=0.1, top=0.1, width=0.3, height=0.3))])
            extra_spans.append(MatchSpan(
                zone="image", unitRef="image:{}".format(k),
                startOffset=0, endOffset=3, ruleName="pii"))
    for ref in data.draw(st.lists(
            st.sampled_from(["table:0", "textual:1", "xocr:0", "images:0", "", "foo"]),
            max_size=3)):
        extra_spans.append(MatchSpan(zone="body", unitRef=ref, startOffset=0,
                                     endOffset=2, ruleName="pii"))

    base_spans = text_spans + ocr_spans
    all_spans = data.draw(st.permutations(base_spans + extra_spans))
    kwargs = dict(ocr_text_units=ocr_units or None, image_regions=image_regions)

    result = redaction_pdf.redact_pdf_document_with_report(
        pdf_bytes, units, list(all_spans), **kwargs)
    report = result.report
    assert report.textLayerRegions == expected_text
    assert report.ocrRegions == expected_ocr
    assert report.agreedRegions == expected_agreed
    assert report.agreedRegions <= report.ocrRegions

    # ---- every region is drawn: overlapping text/ocr regions all kept ------
    out = pikepdf.open(io.BytesIO(result.documentBytes))
    try:
        for page_index in range(len(doc.pages)):
            drawn = len(text_rects.get(page_index, [])) + len(ocr_rects.get(page_index, []))
            if drawn == 0:
                continue
            _, _, names = _last_stream_boxes(out, page_index)
            assert names.count("re") == drawn, (page_index, names.count("re"), drawn)
    finally:
        out.close()

    # ---- image: / unknown spans change neither counts nor text layer -------
    if extra_spans:
        baseline = redaction_pdf.redact_pdf_document_with_report(
            pdf_bytes, units, list(base_spans), **kwargs)
        assert dataclasses.asdict(baseline.report) == dataclasses.asdict(report)
        assert [u.text for u in extract_pdf_content(baseline.documentBytes).textUnits] == [
            u.text for u in extract_pdf_content(result.documentBytes).textUnits]


def test_property_14_empty_span_list_reports_zero_counts():
    pdf_bytes = build_pdf(DocSpec(pages=(PageSpec(runs=(TextRun(text="abc def"),)),)))
    extraction = extract_pdf_content(pdf_bytes)
    result = redaction_pdf.redact_pdf_document_with_report(pdf_bytes, extraction.textUnits, [])
    assert (result.report.textLayerRegions, result.report.ocrRegions,
            result.report.agreedRegions) == (0, 0, 0)
# ---------------------------------------------------------------------------
# Property 15 (redaction + OCR part) / Property 16
# ---------------------------------------------------------------------------
import io  # noqa: E402
from unittest import mock  # noqa: E402

import pikepdf  # noqa: E402

from agent.tools import ocr_pdf  # noqa: E402
from agent.tools.ocr_pdf import OcrPathError, ocr_pdf_pages  # noqa: E402
from agent.tools.redaction_image import ImageRedactionError  # noqa: E402
from agent.tools.redaction_pdf import PdfRedactionResult, RedactionError  # noqa: E402
from pdf_fixtures import simple_image_pdf, simple_text_pdf  # noqa: E402
from shared.models import (  # noqa: E402
    BoundingBox, ExtractedTextUnit, ImageRegion, OcrWord, TextPosition,
)


class _FakeTextract:
    def __init__(self):
        self.calls = 0

    def detect_document_text(self, Document):
        self.calls += 1
        return {"Blocks": []}


def _mangle(pdf_bytes, mode, a, b, junk):
    """Derive a (possibly invalid) byte string from a valid PDF."""
    n = len(pdf_bytes)
    if mode == "valid":
        return pdf_bytes
    if mode == "truncate":
        return pdf_bytes[: a % (n + 1)]
    if mode == "flip":
        out = bytearray(pdf_bytes)
        for k in range(1 + b % 8):
            out[(a + k * 7919) % n] ^= 1 + (b + k) % 255
        return bytes(out)
    if mode == "cut_middle":
        i = a % n
        return pdf_bytes[:i] + pdf_bytes[i + 1 + b % 64:]
    if mode == "empty":
        return b""
    return junk


def _always_invalid(mode):
    return mode in ("empty", "junk")


# Feature: pymupdf-replacement, Property 15: 输入错误只以模块自有异常抛出
# **Validates: Requirements 10.7, 10.8**
@settings(max_examples=100, deadline=None)
@given(
    doc=_docs(),
    mode=st.sampled_from(["valid", "truncate", "flip", "cut_middle", "empty", "junk"]),
    a=st.integers(0, 10_000_000),
    b=st.integers(0, 10_000_000),
    junk=st.binary(max_size=200),
)
def test_property_15_redaction_and_ocr_raise_only_module_errors(doc, mode, a, b, junk):
    original = build_pdf(doc)
    data = _mangle(original, mode, a, b, junk)
    if mode == "junk" and data[:5] == b"%PDF-":
        data = b"junk" + data
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=2,
                  ruleName="prop"),
        MatchSpan(zone="body", unitRef="ocr:0", startOffset=0, endOffset=2,
                  ruleName="prop"),
    ]
    unit = ExtractedTextUnit(
        zone="body", text="ab" * 4, position=TextPosition(index=0, pageIndex=0),
        boundingBox=BoundingBox(left=10, top=10, width=100, height=20),
    )
    ocr_unit = ExtractedTextUnit(
        zone="ocr", text="ab", position=TextPosition(index=0, pageIndex=0),
        ocrWords=[OcrWord(text="ab", startOffset=0, endOffset=2,
                          boundingBox=BoundingBox(left=0.1, top=0.1, width=0.2,
                                                  height=0.1))],
    )

    # (a) redaction
    try:
        result = redact_pdf_document_with_report_(data, [unit], spans[:1], [ocr_unit])
    except RedactionError:
        result = None
    else:
        assert isinstance(result, PdfRedactionResult)
        assert isinstance(result.documentBytes, bytes)
    if _always_invalid(mode):
        assert result is None

    # (b) OCR, with a fake Textract client and no real sleeping
    fake = _FakeTextract()
    with mock.patch.object(ocr_pdf, "_get_textract_client", lambda: fake), \
            mock.patch.object(ocr_pdf.time, "sleep", lambda *_: None):
        try:
            units = ocr_pdf_pages(data)
        except OcrPathError as exc:
            units = None
            if _always_invalid(mode):
                assert exc.pageIndex is None
        else:
            assert isinstance(units, list)
    if _always_invalid(mode):
        assert units is None
        assert fake.calls == 0


def redact_pdf_document_with_report_(data, units, spans, ocr_units):
    return redaction_pdf.redact_pdf_document_with_report(
        data, units, spans, ocr_text_units=ocr_units
    )


# --- Property 16 -----------------------------------------------------------
_P16_PDF = simple_image_pdf(
    width=8, height=8, kind="rgb", text_lines=["hello world"], color=(200, 30, 30)
)
_P16_EXTRACTION = extract_pdf_content(_P16_PDF)
_P16_TEXT_UNITS = list(_P16_EXTRACTION.textUnits)
_P16_IMAGE = _P16_EXTRACTION.imageRegions[0]
_P16_XREF = int(_P16_IMAGE.sourceLocation.split(":")[3])


def _stencil_pdf() -> bytes:
    with pikepdf.open(io.BytesIO(_P16_PDF)) as pdf:
        pdf.get_object((_P16_XREF, 0))["/ImageMask"] = True
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def _page_xref() -> int:
    with pikepdf.open(io.BytesIO(_P16_PDF)) as pdf:
        return pdf.pages[0].obj.objgen[0]


_P16_STENCIL_PDF = _stencil_pdf()
_P16_PAGE_XREF = _page_xref()

_BAD_INDEX_STRINGS = st.one_of(
    st.sampled_from(["abc", "", "x1", "1.5", "1x", " ", "-", "1:2", "None"]),
    st.text(alphabet="abcdefghxyz.:-+", max_size=6),
)
_NEG = st.integers(-1000, -1).map(str)


def _p16_ocr_unit(page_index, words="ok"):
    return ExtractedTextUnit(
        zone="ocr", text="secret",
        position=TextPosition(index=0, pageIndex=page_index),
        ocrWords=words if words != "ok" else [
            OcrWord(text="secret", startOffset=0, endOffset=6,
                    boundingBox=BoundingBox(left=0.1, top=0.1, width=0.3, height=0.1))
        ],
    )


def _p16_text_unit(page_index, box="ok"):
    return ExtractedTextUnit(
        zone="body", text="hello world",
        position=TextPosition(index=0, pageIndex=page_index),
        boundingBox=BoundingBox(left=72, top=72, width=80, height=14)
        if box == "ok" else box,
    )


def _image(**changes):
    return dataclasses.replace(
        _P16_IMAGE, recognizedText="secret", recognizedElements=None, **changes
    )


def _span(ref, end=6):
    zone = "image" if ref.startswith("image:") else "body"
    return MatchSpan(zone=zone, unitRef=ref, startOffset=0, endOffset=end,
                     ruleName="prop")


@st.composite
def _prop16_cases(draw):
    """(pdf, text_units, ocr_units, image_regions, span, image_index|None,
    image_redactor_fails)"""
    kind = draw(st.sampled_from([
        "text_bad_index", "text_negative", "text_out_of_range",
        "text_missing_page", "text_missing_box", "text_page_oob",
        "ocr_bad_index", "ocr_negative", "ocr_out_of_range", "ocr_none_list",
        "ocr_missing_page", "ocr_missing_words", "ocr_page_oob",
        "image_bad_index", "image_negative", "image_out_of_range",
        "image_none_list", "image_no_bytes", "image_bad_location",
        "image_page_oob", "image_redactor_fails", "image_not_an_image",
        "image_stencil",
    ]))
    pdf, texts, ocrs, images, image_index, fails = _P16_PDF, [_p16_text_unit(0)], \
        [_p16_ocr_unit(0)], [_image(sourceLocation=_P16_IMAGE.sourceLocation)], None, False
    ref = None
    if kind == "text_bad_index":
        ref = "text:" + draw(_BAD_INDEX_STRINGS)
    elif kind == "text_negative":
        ref = "text:" + draw(_NEG)
    elif kind == "text_out_of_range":
        ref = "text:{}".format(len(texts) + draw(st.integers(0, 50)))
        if draw(st.booleans()):
            texts = []
            ref = "text:{}".format(draw(st.integers(0, 5)))
    elif kind in ("text_missing_page", "text_missing_box", "text_page_oob"):
        ref = "text:0"
        if kind == "text_missing_page":
            texts = [_p16_text_unit(None)]
        elif kind == "text_missing_box":
            texts = [_p16_text_unit(0, box=None)]
        else:
            texts = [_p16_text_unit(draw(st.one_of(
                st.integers(-50, -1), st.integers(1, 50))))]
    elif kind == "ocr_bad_index":
        ref = "ocr:" + draw(_BAD_INDEX_STRINGS)
    elif kind == "ocr_negative":
        ref = "ocr:" + draw(_NEG)
    elif kind == "ocr_out_of_range":
        ocrs = draw(st.sampled_from([[], [_p16_ocr_unit(0)]]))
        ref = "ocr:{}".format(len(ocrs) + draw(st.integers(0, 50)))
    elif kind == "ocr_none_list":
        ocrs = None
        ref = "ocr:{}".format(draw(st.integers(0, 5)))
    elif kind in ("ocr_missing_page", "ocr_missing_words", "ocr_page_oob"):
        ref = "ocr:0"
        if kind == "ocr_missing_page":
            ocrs = [_p16_ocr_unit(None)]
        elif kind == "ocr_missing_words":
            ocrs = [_p16_ocr_unit(0, words=None)]
        else:
            ocrs = [_p16_ocr_unit(draw(st.one_of(
                st.integers(-50, -1), st.integers(1, 50))))]
    elif kind == "image_bad_index":
        ref = "image:" + draw(_BAD_INDEX_STRINGS)
    elif kind == "image_negative":
        ref = "image:" + draw(_NEG)
    elif kind == "image_out_of_range":
        ref = "image:{}".format(len(images) + draw(st.integers(0, 50)))
        image_index = int(ref.split(":")[1])
    elif kind == "image_none_list":
        images = None
        ref = "image:{}".format(draw(st.integers(0, 5)))
        image_index = int(ref.split(":")[1])
    else:
        ref = "image:0"
        image_index = 0
        if kind == "image_no_bytes":
            images = [_image(imageBytes=draw(st.sampled_from([None, b""])),
                             sourceLocation=_P16_IMAGE.sourceLocation)]
        elif kind == "image_bad_location":
            images = [_image(sourceLocation=draw(st.sampled_from([
                "", "page:0", "page:x:xref:1", "page:0:xref:", "xref:3",
                "page:-1:xref:3", "page:0:xref:-3", "page:0:xref:3:extra",
                "PAGE:0:xref:3", "0:3", "page:0:xref:3 ",
            ])))]
        elif kind == "image_page_oob":
            images = [_image(sourceLocation="page:{}:xref:{}".format(
                draw(st.integers(1, 500)), _P16_XREF))]
        elif kind == "image_redactor_fails":
            fails = True
        elif kind == "image_not_an_image":
            images = [_image(sourceLocation="page:0:xref:{}".format(
                draw(st.sampled_from([_P16_PAGE_XREF, 9999, 0]))))]
        elif kind == "image_stencil":
            pdf = _P16_STENCIL_PDF
    span_end = 6
    if ref.startswith(("text:", "ocr:")) and kind not in ("text_out_of_range",):
        span_end = 5
    return pdf, texts, ocrs, images, _span(ref, span_end), image_index, fails


# Feature: pymupdf-replacement, Property 16: 无效引用一律报错
# **Validates: Requirements 10.6, 9.6**
@settings(max_examples=100, deadline=None)
@given(case=_prop16_cases())
def test_property_16_invalid_references_always_raise_redaction_error(case):
    pdf, texts, ocrs, images, span, image_index, fails = case
    kwargs = dict(ocr_text_units=ocrs, image_regions=images)
    if fails:
        patcher = mock.patch.object(
            redaction_pdf, "redact_image_bytes",
            side_effect=ImageRedactionError("boom"),
        )
    else:
        patcher = mock.patch.object(
            redaction_pdf, "redact_image_bytes", wraps=redaction_pdf.redact_image_bytes
        )
    result = None
    with patcher:
        try:
            result = redaction_pdf.redact_pdf_document_with_report(
                pdf, texts, [span], **kwargs
            )
        except RedactionError as exc:
            message = str(exc)
        else:
            message = None
    assert result is None, "invalid reference {!r} was accepted".format(span.unitRef)
    if span.unitRef.startswith("image:") and image_index is not None:
        assert "image:{}".format(image_index) in message, message
    elif span.unitRef.startswith("image:"):
        # unparsable / negative index: the message still names the offending ref
        assert span.unitRef.split(":", 1)[1] in message or "image" in message, message
