"""Property tests for ``agent.tools.extraction_pdf.extract_pdf_content``."""
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

from agent.tools.extraction_pdf import extract_pdf_content  # noqa: E402
from pdf_fixtures import (  # noqa: E402
    DocSpec,
    PageSpec,
    TextRun,
    build_pdf,
    doc_specs,
)

_TOL = 0.01


def _visible_size(page: PageSpec):
    """Independent computation of the visible rect size (width, height)."""
    x0, y0, x1, y1 = page.media_box
    if page.crop_box is not None:
        cx0, cy0, cx1, cy1 = page.crop_box
        x0, y0, x1, y1 = max(x0, cx0), max(y0, cy0), min(x1, cx1), min(y1, cy1)
    w, h = x1 - x0, y1 - y0
    if (page.rotate or 0) % 180 == 90:
        w, h = h, w
    return w, h


def _content_text(content, forms_by_name) -> str:
    """Text a correct extractor returns for a page/form: runs in drawing
    order, then (recursively) the placed forms, as ``build_pdf`` emits them."""
    parts = [run.shown_text for run in content.runs]
    for placement in content.form_placements:
        parts.append(_content_text(forms_by_name[placement.form], forms_by_name))
    return "".join(parts)


@st.composite
def _docs_with_blank_pages(draw):
    """Documents of latin and/or CJK text, split lines, with some pages
    replaced by blank or whitespace-only pages."""
    doc = draw(doc_specs(max_pages=4, cjk=draw(st.booleans())))
    pages = []
    for page in doc.pages:
        kind = draw(st.sampled_from(["keep", "keep", "keep", "blank", "space"]))
        if kind == "blank":
            page = dataclasses.replace(page, runs=(), form_placements=())
        elif kind == "space":
            page = dataclasses.replace(
                page,
                runs=(TextRun(text=" ", x=72, y=700), TextRun(text="  ", x=100, y=650)),
                form_placements=(),
            )
        elif draw(st.booleans()) and page.runs:
            # split-line PII: one string shown over two successive lines
            text = "".join(r.shown_text for r in page.runs[:1]) or "x"
            if len(text) >= 2:
                cut = len(text) // 2
                font = page.runs[0].font
                first = TextRun(text=text[:cut], x=72, y=700, font=font)
                second = TextRun(text=text[cut:], x=72, y=680, font=font)
                page = dataclasses.replace(page, runs=(first, second) + page.runs[1:])
        pages.append(page)
    return dataclasses.replace(doc, pages=tuple(pages))


# Feature: pymupdf-replacement, Property 3: 文本层单元结构与内容
# **Validates: Requirements 4.1, 4.2, 4.3, 4.5, 4.6, 4.7, 4.8**
@settings(max_examples=100, deadline=None)
@given(doc=_docs_with_blank_pages())
def test_property_3_text_unit_structure_and_content(doc: DocSpec):
    result = extract_pdf_content(build_pdf(doc))
    n = len(doc.pages)
    forms_by_name = {f.name: f for f in doc.forms}

    assert result.pageCount == n
    assert result.pageOrder == list(range(n))

    expected = {}
    for index, page in enumerate(doc.pages):
        text = _content_text(page, forms_by_name)
        if text.strip() != "":
            expected[index] = text

    indices = [u.position.pageIndex for u in result.textUnits]
    assert indices == sorted(set(indices)), "pageIndex must be strictly ascending"
    assert all(0 <= i <= n - 1 for i in indices)
    assert indices == sorted(expected), "one unit per non-blank page only"

    for unit in result.textUnits:
        page_index = unit.position.pageIndex
        assert unit.zone == "body"
        assert unit.position.index == 0
        assert unit.text.strip() != ""
        assert unit.text == expected[page_index]
        assert "\ufffd" not in unit.text
        width, height = _visible_size(doc.pages[page_index])
        box = unit.boundingBox
        assert abs(box.left - 0) <= _TOL
        assert abs(box.top - 0) <= _TOL
        assert abs(box.width - width) <= _TOL
        assert abs(box.height - height) <= _TOL


# ---------------------------------------------------------------------------
# Property 7: image region contract
# ---------------------------------------------------------------------------

import io  # noqa: E402
import re  # noqa: E402

from PIL import Image  # noqa: E402
from pypdf import PdfReader  # noqa: E402
from pdf_fixtures import (  # noqa: E402
    IMAGE_KINDS,
    FormPlacement,
    FormSpec,
    ImageSpec,
    InlineImageSpec,
)

_HANDLE = re.compile(r"^page:(\d+):xref:([1-9]\d*)$")
_SHARED_KEYS = ("A", "B", "C")


def _visible_box(page: PageSpec):
    x0, y0, x1, y1 = page.media_box
    if page.crop_box is not None:
        cx0, cy0, cx1, cy1 = page.crop_box
        x0, y0, x1, y1 = max(x0, cx0), max(y0, cy0), min(x1, cx1), min(y1, cy1)
    return x0, y0, x1, y1


def _expected_page_box(page: PageSpec, user_rect):
    """Page_Coordinates (left, top, width, height) of a user-space rect
    (x0, y0, x1, y1), derived from the visible box and /Rotate."""
    vx0, vy0, vx1, vy1 = _visible_box(page)
    rotate = (page.rotate or 0) % 360
    ux0, uy0, ux1, uy1 = user_rect
    corners = [(ux0, uy0), (ux0, uy1), (ux1, uy0), (ux1, uy1)]
    mapped = []
    for x, y in corners:
        if rotate == 0:
            mapped.append((x - vx0, vy1 - y))
        elif rotate == 90:
            mapped.append((y - vy0, x - vx0))
        elif rotate == 180:
            mapped.append((vx1 - x, y - vy0))
        else:
            mapped.append((vy1 - y, vx1 - x))
    xs = [p[0] for p in mapped]
    ys = [p[1] for p in mapped]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


@st.composite
def _image_docs(draw):
    """Documents whose pages draw shared (keyed) and private images several
    times, inline images, and a shared Form XObject containing an image.
    Returns (DocSpec, expected) where expected[page] maps an object id
    ("A", "B", "C", "F" or "p<page>_<n>") to the expected user-space rect of
    its first placement."""
    n_pages = draw(st.integers(1, 3))
    shared = {
        key: dict(
            kind=draw(st.sampled_from(IMAGE_KINDS)),
            width=draw(st.integers(1, 12)),
            height=draw(st.integers(1, 12)),
        )
        for key in _SHARED_KEYS + ("F",)
    }

    def spec(key, placements, smask, **extra):
        info = dict(shared[key]) if key in shared else extra
        return ImageSpec(
            kind=info["kind"],
            width=info["width"],
            height=info["height"],
            placements=tuple(placements),
            smask=False if info["kind"] == "mask" else smask,
            key=key if key in shared else None,
        )

    for key in shared:  # smask is per object, fixed per key
        shared[key]["smask"] = draw(st.booleans())

    form = FormSpec(
        name="Fshared",
        bbox=(0, 0, 100, 100),
        images=(
            ImageSpec(
                kind=shared["F"]["kind"],
                width=shared["F"]["width"],
                height=shared["F"]["height"],
                placements=((0, 0, 40, 40),),
                smask=(False if shared["F"]["kind"] == "mask" else shared["F"]["smask"]),
                key="F",
            ),
        ),
    )

    pages = []
    expected = []
    uses_form = False
    for _ in range(n_pages):
        page = draw(page_specs_geometry())
        vx0, vy0, vx1, vy1 = _visible_box(page)

        def draw_rect():
            w = draw(st.integers(20, 100))
            h = draw(st.integers(20, 100))
            x = draw(st.integers(int(vx0), int(vx1) - w))
            y = draw(st.integers(int(vy0), int(vy1) - h))
            return (x, y, w, h)

        page_expected = {}
        images = []
        keys = draw(st.lists(st.sampled_from(_SHARED_KEYS), unique=True, max_size=3))
        for key in keys:
            rects = [draw_rect() for _ in range(draw(st.integers(1, 3)))]
            images.append(spec(key, rects, shared[key]["smask"]))
            x, y, w, h = rects[0]
            page_expected[key] = (x, y, x + w, y + h)
        for n in range(draw(st.integers(0, 2))):
            info = dict(
                kind=draw(st.sampled_from(IMAGE_KINDS)),
                width=draw(st.integers(1, 12)),
                height=draw(st.integers(1, 12)),
            )
            rects = [draw_rect() for _ in range(draw(st.integers(1, 2)))]
            images.append(
                ImageSpec(
                    kind=info["kind"],
                    width=info["width"],
                    height=info["height"],
                    placements=tuple(rects),
                    smask=False if info["kind"] == "mask" else draw(st.booleans()),
                )
            )
            x, y, w, h = rects[0]
            page_expected["p%d" % n] = (x, y, x + w, y + h)
        inline = tuple(
            InlineImageSpec(
                width=draw(st.integers(1, 6)),
                height=draw(st.integers(1, 6)),
                placement=draw_rect(),
            )
            for _ in range(draw(st.integers(0, 2)))
        )
        placements = ()
        if draw(st.booleans()):
            uses_form = True
            fps = []
            for _ in range(draw(st.integers(1, 2))):
                tx = draw(st.integers(int(vx0), int(vx1) - 40))
                ty = draw(st.integers(int(vy0), int(vy1) - 40))
                fps.append(FormPlacement("Fshared", (1, 0, 0, 1, tx, ty)))
            placements = tuple(fps)
            tx, ty = fps[0].matrix[4], fps[0].matrix[5]
            page_expected["F"] = (tx, ty, tx + 40, ty + 40)
        pages.append(
            dataclasses.replace(
                page,
                images=tuple(images),
                inline_images=inline,
                form_placements=placements,
            )
        )
        expected.append(page_expected)
    doc = DocSpec(pages=tuple(pages), forms=(form,) if uses_form else ())
    return doc, expected


@st.composite
def page_specs_geometry(draw):
    x0 = draw(st.sampled_from([0, 25, -50]))
    y0 = draw(st.sampled_from([0, 40, -30]))
    width = draw(st.sampled_from([300, 612]))
    height = draw(st.sampled_from([400, 792]))
    crop = None
    if draw(st.booleans()):
        crop = (x0 + 10, y0 + 15, x0 + width - 20, y0 + height - 10)
    return PageSpec(
        media_box=(x0, y0, x0 + width, y0 + height),
        crop_box=crop,
        rotate=draw(st.sampled_from([None, 0, 90, 180, 270])),
    )


# Feature: pymupdf-replacement, Property 7: 图片区域契约
# **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.7**
@settings(max_examples=100, deadline=None)
@given(case=_image_docs())
def test_property_7_image_region_contract(case):
    doc, expected = case
    data = build_pdf(doc)
    result = extract_pdf_content(data)

    # 6.7: ordered by pageIndex ascending, stable across repeated extraction
    handles = [r.sourceLocation for r in result.imageRegions]
    parsed = []
    for handle in handles:
        match = _HANDLE.match(handle)
        assert match, handle  # 6.3 format
        parsed.append((int(match.group(1)), int(match.group(2))))
    page_indices = [p for p, _ in parsed]
    assert page_indices == sorted(page_indices)
    again = extract_pdf_content(data)
    assert [r.sourceLocation for r in again.imageRegions] == handles

    # 6.1: one region per distinct image object per page; inline -> none
    assert len(set(parsed)) == len(parsed)
    by_page = {}
    for region, (page_index, xref) in zip(result.imageRegions, parsed):
        by_page.setdefault(page_index, {})[xref] = region
    assert set(by_page) <= set(range(len(doc.pages)))

    xref_of_key = {}
    for page_index, page_expected in enumerate(expected):
        regions = by_page.get(page_index, {})
        assert len(regions) == len(page_expected)
        remaining = dict(regions)
        for obj_id in sorted(page_expected):
            user_rect = page_expected[obj_id]
            want = _expected_page_box(doc.pages[page_index], user_rect)
            matches = [
                xref
                for xref, region in remaining.items()
                if region.boundingBox is not None
                and abs(region.boundingBox.left - want[0]) <= _TOL
                and abs(region.boundingBox.top - want[1]) <= _TOL
                and abs(region.boundingBox.width - want[2]) <= _TOL
                and abs(region.boundingBox.height - want[3]) <= _TOL
            ]
            # 6.2: boundingBox of the first placement
            assert matches, (page_index, obj_id, want)
            if obj_id in _SHARED_KEYS + ("F",):
                xref = matches[0]
                # 6.3: the shared object keeps its xref across pages
                assert xref_of_key.setdefault(obj_id, xref) == xref
                remaining.pop(xref)
            else:
                remaining.pop(matches[0])
        assert not remaining

    reader = PdfReader(io.BytesIO(data))
    for region in result.imageRegions:
        assert region.zone == "image"
        assert region.recognizedText is None
        # 6.4: decodable with the declared size
        assert region.imageBytes
        with Image.open(io.BytesIO(region.imageBytes)) as img:
            img.load()
            size = img.size
        xref = int(_HANDLE.match(region.sourceLocation).group(2))
        obj = reader.get_object(xref)
        assert size == (int(obj["/Width"]), int(obj["/Height"]))
    assert len(set(xref_of_key.values())) == len(xref_of_key)



# ---------------------------------------------------------------------------
# Property 15 (extraction part): input errors surface only as ExtractionError
# ---------------------------------------------------------------------------
from agent.tools.extraction_pdf import ExtractionError, ExtractionResult  # noqa: E402


def _assert_result_or_extraction_error(data: bytes):
    try:
        result = extract_pdf_content(data)
    except ExtractionError:
        return None
    assert isinstance(result, ExtractionResult)
    return result


@st.composite
def _corrupted_pdfs(draw):
    doc = draw(doc_specs(max_pages=3, cjk=draw(st.booleans())))
    data = bytearray(build_pdf(doc))
    mode = draw(st.sampled_from(["truncate", "tamper", "both"]))
    if mode in ("tamper", "both"):
        n = draw(st.integers(min_value=1, max_value=8))
        for _ in range(n):
            pos = draw(st.integers(min_value=0, max_value=len(data) - 1))
            data[pos] = draw(st.integers(min_value=0, max_value=255))
    if mode in ("truncate", "both"):
        cut = draw(st.integers(min_value=0, max_value=len(data) - 1))
        data = data[:cut]
    return bytes(data)


# Feature: pymupdf-replacement, Property 15: 输入错误只以模块自有异常抛出
# **Validates: Requirements 4.9, 10.8**
@settings(max_examples=100, deadline=None)
@given(data=_corrupted_pdfs())
def test_property_15_corrupted_pdf_only_extraction_error(data: bytes):
    _assert_result_or_extraction_error(data)


# Feature: pymupdf-replacement, Property 15: 输入错误只以模块自有异常抛出
# **Validates: Requirements 4.9, 10.8**
@settings(max_examples=100, deadline=None)
@given(data=st.binary(max_size=2048))
def test_property_15_junk_bytes_raise_extraction_error(data: bytes):
    if data.lstrip().startswith(b"%PDF"):
        data = b"junk" + data  # keep this property on non-PDF input
    try:
        extract_pdf_content(data)
    except ExtractionError:
        return
    raise AssertionError("junk bytes must raise ExtractionError")


def test_property_15_empty_bytes_raise_extraction_error():
    try:
        extract_pdf_content(b"")
    except ExtractionError:
        return
    raise AssertionError("empty bytes must raise ExtractionError")


# ---------------------------------------------------------------------------
# Task 8.6: extraction-side example tests
# ---------------------------------------------------------------------------
import pikepdf  # noqa: E402
import pytest  # noqa: E402

from pdf_fixtures import CJK_FONT, OWNER_ONLY, USER_REQUIRED  # noqa: E402


def _mutate(data: bytes, mutate) -> bytes:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        mutate(pdf)
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def _two_page_doc() -> DocSpec:
    return DocSpec(pages=(
        PageSpec(runs=(TextRun(text="first"),)),
        PageSpec(runs=(TextRun(text="second"),)),
    ))


def test_ssn_split_across_lines_is_contiguous():
    """Validates: Requirements 4.4, 4.6"""
    doc = DocSpec(pages=(PageSpec(runs=(
        TextRun(text="123-45-", x=72, y=700),
        TextRun(text="6789", x=72, y=680),
    )),))
    result = extract_pdf_content(build_pdf(doc))
    assert len(result.textUnits) == 1
    assert result.textUnits[0].text == "123-45-6789"


def test_cjk_text_matches_per_code_point():
    """Validates: Requirements 4.6, 4.7"""
    text = "你好，世界。個人情報"
    doc = DocSpec(pages=(PageSpec(runs=(TextRun(text=text, font=CJK_FONT),)),))
    result = extract_pdf_content(build_pdf(doc))
    assert [u.text for u in result.textUnits] == [text]
    assert list(result.textUnits[0].text) == list(text)


def test_owner_password_only_document_is_extracted():
    """Validates: Requirements 4.10"""
    doc = DocSpec(
        pages=(PageSpec(runs=(TextRun(text="Hello"),)),), encryption=OWNER_ONLY
    )
    result = extract_pdf_content(build_pdf(doc))
    assert [u.text for u in result.textUnits] == ["Hello"]


def test_user_password_required_raises_extraction_error():
    """Validates: Requirements 4.10"""
    doc = DocSpec(
        pages=(PageSpec(runs=(TextRun(text="Hello"),)),), encryption=USER_REQUIRED
    )
    with pytest.raises(ExtractionError):
        extract_pdf_content(build_pdf(doc))


def test_page_level_font_failure_raises_extraction_error_with_page_index():
    """Validates: Requirements 4.11"""

    def break_font(pdf):
        fonts = pdf.pages[1].Resources.Font
        name = list(fonts.keys())[0]
        fonts[name] = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type0,
            BaseFont=pikepdf.Name("/Broken"),
            Encoding=pikepdf.Name("/Identity-H"),
            DescendantFonts=5,  # must be an array
        ))

    data = _mutate(build_pdf(_two_page_doc()), break_font)
    with pytest.raises(ExtractionError, match=r"page 1\b"):
        extract_pdf_content(data)


def test_page_level_corrupt_flate_raises_extraction_error_with_page_index():
    """Validates: Requirements 4.11"""

    def corrupt_contents(pdf):
        stream = pdf.make_stream(b"")
        stream.write(b"this is not zlib data", filter=None)
        stream.Filter = pikepdf.Name.FlateDecode
        pdf.pages[1].obj.Contents = stream

    data = _mutate(build_pdf(_two_page_doc()), corrupt_contents)
    with pytest.raises(ExtractionError, match=r"page 1\b"):
        extract_pdf_content(data)


def test_unplaced_image_has_no_bounding_box():
    """Validates: Requirements 6.8"""
    doc = DocSpec(pages=(PageSpec(images=(ImageSpec(placements=()),)),))
    result = extract_pdf_content(build_pdf(doc))
    assert len(result.imageRegions) == 1
    region = result.imageRegions[0]
    assert region.boundingBox is None
    assert region.imageBytes


def test_rotated_cropped_page_image_bounding_box():
    """Validates: Requirements 6.2, 6.8"""
    page = PageSpec(
        media_box=(0, 0, 612, 792),
        crop_box=(10, 15, 500, 700),
        rotate=90,
        images=(ImageSpec(placements=((10, 15, 490, 685),)),),
    )
    result = extract_pdf_content(build_pdf(DocSpec(pages=(page,))))
    w0, h0 = 490, 685  # visible box before rotation
    box = result.imageRegions[0].boundingBox
    assert abs(box.left - 0) <= _TOL
    assert abs(box.top - 0) <= _TOL
    assert abs(box.width - h0) <= _TOL
    assert abs(box.height - w0) <= _TOL
