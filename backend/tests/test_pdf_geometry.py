"""Tests for ``agent.tools.pdf_geometry`` (pure coordinate functions)."""
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_LAMBDAS = os.path.join(_BACKEND, "lambdas")
if _LAMBDAS not in sys.path:
    sys.path.insert(0, _LAMBDAS)

import pytest  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent.tools.pdf_geometry import (  # noqa: E402
    make_page_geometry,
    page_to_user_point,
    page_to_user_rect,
    rects_intersect,
    user_to_page_point,
    user_to_page_rect,
)

_TOL = 0.01
_EDGE_EPS = 1e-6  # points this close to a box edge are ambiguous under float rounding
_coord = st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False)
_size = st.floats(min_value=10, max_value=2000, allow_nan=False, allow_infinity=False)
_frac = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@st.composite
def _geometry_cases(draw):
    """A page geometry plus a Page_Coordinates rect inside it and a user-space point."""
    x0, y0 = draw(_coord), draw(_coord)
    w, h = draw(_size), draw(_size)
    media = (x0, y0, x0 + w, y0 + h)

    crop = None
    if draw(st.booleans()):
        # Inner sub-rectangle of the MediaBox, at least 5 pt in each direction.
        fx0 = draw(st.floats(min_value=0.0, max_value=0.45))
        fx1 = draw(st.floats(min_value=0.55, max_value=1.0))
        fy0 = draw(st.floats(min_value=0.0, max_value=0.45))
        fy1 = draw(st.floats(min_value=0.55, max_value=1.0))
        crop = (x0 + fx0 * w, y0 + fy0 * h, x0 + fx1 * w, y0 + fy1 * h)

    rotate = draw(st.integers(min_value=-100000, max_value=100000))
    geom = make_page_geometry(0, media, crop, rotate)

    a, b = sorted((draw(_frac), draw(_frac)))
    c, d = sorted((draw(_frac), draw(_frac)))
    rect = (a * geom.width, c * geom.height, b * geom.width, d * geom.height)

    px, py = draw(st.floats(min_value=-500, max_value=3500)), draw(
        st.floats(min_value=-500, max_value=3500)
    )
    return geom, rect, (px, py)


# Feature: pymupdf-replacement, Property 1: 坐标换算往返
# **Validates: Requirements 5.4, 5.2**
@settings(max_examples=100, deadline=None)
@given(case=_geometry_cases())
def test_property_1_coordinate_round_trip(case):
    geom, rect, (px, py) = case

    # Round trip: Page_Coordinates -> user space -> Page_Coordinates.
    back = user_to_page_rect(geom, page_to_user_rect(geom, rect))
    for original, restored in zip(rect, back):
        assert abs(original - restored) <= _TOL

    # A user-space point maps into (0, 0, width, height) iff it is in the visible box.
    vx0, vy0, vx1, vy1 = geom.visible_box
    X, Y = user_to_page_point(geom, px, py)
    inside_page = 0 <= X <= geom.width and 0 <= Y <= geom.height
    # Only assert when the point is clearly inside or clearly outside the box,
    # i.e. farther than _EDGE_EPS from every edge it could be confused with.
    deep_inside = (
        vx0 + _EDGE_EPS < px < vx1 - _EDGE_EPS and vy0 + _EDGE_EPS < py < vy1 - _EDGE_EPS
    )
    clearly_outside = (
        px < vx0 - _EDGE_EPS
        or px > vx1 + _EDGE_EPS
        or py < vy0 - _EDGE_EPS
        or py > vy1 + _EDGE_EPS
    )
    if deep_inside:
        assert inside_page
    elif clearly_outside:
        assert not inside_page


# --- Example tests: design formula table, CropBox origin (50, 50) -----------
_MEDIA = (0.0, 0.0, 400.0, 300.0)
_CROP = (50.0, 50.0, 350.0, 250.0)  # W0 = 300, H0 = 200
_USER_PT = (100.0, 200.0)

# rotate -> (width, height, expected (X, Y) for _USER_PT per the design table)
_FORMULA_CASES = [
    (0, 300.0, 200.0, (50.0, 50.0)),    # X = x - vx0, Y = vy1 - y
    (90, 200.0, 300.0, (150.0, 50.0)),  # X = y - vy0, Y = x - vx0
    (180, 300.0, 200.0, (250.0, 150.0)),  # X = vx1 - x, Y = y - vy0
    (270, 200.0, 300.0, (50.0, 250.0)),  # X = vy1 - y, Y = vx1 - x
]


@pytest.mark.parametrize("rotate,width,height,expected", _FORMULA_CASES)
def test_rotation_formula_table_with_offset_cropbox(rotate, width, height, expected):
    geom = make_page_geometry(0, _MEDIA, _CROP, rotate)
    assert geom.visible_box == _CROP
    assert geom.rotation == rotate
    assert (geom.width, geom.height) == (width, height)
    assert user_to_page_point(geom, *_USER_PT) == expected
    assert page_to_user_point(geom, *expected) == _USER_PT
    # Visible corners map onto the page corners.
    assert user_to_page_rect(geom, _CROP) == (0.0, 0.0, width, height)


def test_rects_intersect_edge_touching_is_false():
    a = (0.0, 0.0, 10.0, 10.0)
    assert rects_intersect(a, (10.0, 0.0, 20.0, 10.0)) is False  # shared vertical edge
    assert rects_intersect(a, (0.0, 10.0, 10.0, 20.0)) is False  # shared horizontal edge
    assert rects_intersect(a, (10.0, 10.0, 20.0, 20.0)) is False  # shared corner


def test_rects_intersect_floating_point_noise_is_false():
    a = (0.0, 0.0, 10.0, 10.0)
    assert rects_intersect(a, (10.0 - 1e-13, 0.0, 20.0, 10.0)) is False
    assert rects_intersect(a, (0.0, 10.0 - 1e-13, 10.0, 20.0)) is False


def test_rects_intersect_real_overlap_is_true():
    a = (0.0, 0.0, 10.0, 10.0)
    assert rects_intersect(a, (9.0, 9.0, 20.0, 20.0)) is True


# ---------------------------------------------------------------------------
# Property 2: page geometry agrees with PDFium
# ---------------------------------------------------------------------------

import pypdfium2 as pdfium  # noqa: E402

from agent.tools.pdf_glyphs import collect_page_glyphs, open_pdf  # noqa: E402
from tests.pdf_fixtures import DocSpec, PageSpec, build_pdf  # noqa: E402


@st.composite
def _pdfium_page_specs(draw):
    # A CropBox entirely outside the MediaBox is deliberately not generated:
    # PDFium reports a 0x0 page for it (nothing renderable), whereas
    # pdf_geometry falls back to the MediaBox as the design specifies. That
    # case is covered by the pure-function tests, not by PDFium comparison.
    x0 = draw(st.integers(min_value=-500, max_value=500))
    y0 = draw(st.integers(min_value=-500, max_value=500))
    w = draw(st.integers(min_value=20, max_value=1500))
    h = draw(st.integers(min_value=20, max_value=1500))
    media = (x0, y0, x0 + w, y0 + h)
    crop = None
    kind = draw(st.sampled_from(["none", "inside", "partial"]))
    if kind == "inside":
        cx0 = draw(st.integers(min_value=x0, max_value=x0 + w - 10))
        cy0 = draw(st.integers(min_value=y0, max_value=y0 + h - 10))
        cx1 = draw(st.integers(min_value=cx0 + 10, max_value=x0 + w))
        cy1 = draw(st.integers(min_value=cy0 + 10, max_value=y0 + h))
        crop = (cx0, cy0, cx1, cy1)
    elif kind == "partial":
        # Overlaps the MediaBox but sticks out on at least the left/bottom side.
        cx0 = draw(st.integers(min_value=x0 - 300, max_value=x0 - 1))
        cy0 = draw(st.integers(min_value=y0 - 300, max_value=y0 - 1))
        cx1 = draw(st.integers(min_value=x0 + 10, max_value=x0 + w + 300))
        cy1 = draw(st.integers(min_value=y0 + 10, max_value=y0 + h + 300))
        crop = (cx0, cy0, cx1, cy1)
    rotate = draw(
        st.one_of(st.none(), st.integers(min_value=-8, max_value=8).map(lambda n: n * 90))
    )
    return PageSpec(media_box=media, crop_box=crop, rotate=rotate)


# Feature: pymupdf-replacement, Property 2: 页面几何与 PDFium 一致
# **Validates: Requirements 4.4, 7.3, 7.4, 5.2**
@settings(max_examples=100, deadline=None)
@given(spec=_pdfium_page_specs(), dpi=st.integers(min_value=72, max_value=400))
def test_property_2_page_geometry_matches_pdfium(spec, dpi):
    data = build_pdf(DocSpec(pages=(spec,)))

    handle = open_pdf(data)
    geom = collect_page_glyphs(handle, 0).geometry

    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[0]
        pw, ph = page.get_size()
        assert abs(geom.width - pw) <= _TOL, (spec, geom, pw, ph)
        assert abs(geom.height - ph) <= _TOL, (spec, geom, pw, ph)
        assert geom.rotation == page.get_rotation() % 360, (spec, geom)

        image = page.render(scale=dpi / 72).to_pil()
        assert abs(image.width - geom.width * dpi / 72) <= 1, (spec, dpi, image.size)
        assert abs(image.height - geom.height * dpi / 72) <= 1, (spec, dpi, image.size)
    finally:
        pdf.close()
