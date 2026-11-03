"""Example tests for ``agent.tools.ocr_pdf`` (the pypdfium2 OCR_Path).

No network: Textract is replaced by a fake client injected by patching
``ocr_pdf._get_textract_client``. ``time.sleep`` is patched to record the
backoff. The "PNG > 10 MB" cases wrap ``ocr_pdf._render_page_png`` (padding the
returned bytes) rather than building a genuinely huge page.

Requirements: 7.2, 7.3, 7.5, 7.7, 7.8, 7.10, 7.11
"""
import io
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_LAMBDAS = os.path.join(_BACKEND, "lambdas")
if _LAMBDAS not in sys.path:
    sys.path.insert(0, _LAMBDAS)

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from agent.tools import ocr_pdf  # noqa: E402
from agent.tools.ocr_pdf import OcrPathError, ocr_pdf_pages  # noqa: E402
from tests.pdf_fixtures import (  # noqa: E402
    USER_REQUIRED,
    DocSpec,
    PageSpec,
    build_pdf,
    simple_multi_page_pdf,
    simple_text_pdf,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _AwsError(Exception):
    """Duck-typed botocore ClientError (``ocr_pdf._is_retryable`` reads ``.response``)."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


def _word_response(*lines):
    """Textract response; each line is a list of (text, left, top, w, h)."""
    blocks = []
    n = 0
    for line in lines:
        ids = []
        for text, left, top, w, h in line:
            n += 1
            wid = "w%d" % n
            ids.append(wid)
            blocks.append(
                {
                    "BlockType": "WORD",
                    "Id": wid,
                    "Text": text,
                    "Geometry": {
                        "BoundingBox": {"Left": left, "Top": top, "Width": w, "Height": h}
                    },
                }
            )
        n += 1
        blocks.append(
            {
                "BlockType": "LINE",
                "Id": "l%d" % n,
                "Relationships": [{"Type": "CHILD", "Ids": ids}],
            }
        )
    return {"Blocks": blocks}


class FakeTextract:
    """Plays back ``script`` entries (dict response or Exception) per call;
    the last entry repeats once the script is exhausted."""

    def __init__(self, script=None):
        self.script = list(script) if script else [{"Blocks": []}]
        self.calls = []

    def detect_document_text(self, Document):
        self.calls.append(Document["Bytes"])
        index = min(len(self.calls) - 1, len(self.script) - 1)
        item = self.script[index]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(ocr_pdf.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def install(monkeypatch, sleeps):
    def _install(script=None):
        fake = FakeTextract(script)
        monkeypatch.setattr(ocr_pdf, "_get_textract_client", lambda: fake)
        return fake

    return _install


def _png_size(png):
    with Image.open(io.BytesIO(png)) as image:
        return image.size


# ---------------------------------------------------------------------------
# Rasterization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dpi", [72, 150, 200])
def test_png_pixel_size_matches_dpi(monkeypatch, install, dpi):
    monkeypatch.setenv(ocr_pdf.RASTER_DPI_ENV_VAR, str(dpi))
    fake = install()
    pdf = simple_text_pdf(["hello"], page_size=(300, 400))
    ocr_pdf_pages(pdf)
    width, height = _png_size(fake.calls[0])
    assert abs(width - 300 * dpi / 72) <= 1
    assert abs(height - 400 * dpi / 72) <= 1


def test_rotated_page_png_and_bounding_box_use_visible_size(monkeypatch, install):
    monkeypatch.setenv(ocr_pdf.RASTER_DPI_ENV_VAR, "144")
    fake = install()
    pdf = build_pdf(DocSpec(pages=(PageSpec(media_box=(0, 0, 300, 400), rotate=90),)))
    units = ocr_pdf_pages(pdf)
    width, height = _png_size(fake.calls[0])
    assert abs(width - 800) <= 1 and abs(height - 600) <= 1
    box = units[0].boundingBox
    assert (box.width, box.height) == pytest.approx((400, 300))


def test_cropbox_png_uses_cropped_size(monkeypatch, install):
    monkeypatch.setenv(ocr_pdf.RASTER_DPI_ENV_VAR, "72")
    fake = install()
    pdf = build_pdf(
        DocSpec(pages=(PageSpec(media_box=(0, 0, 300, 400), crop_box=(50, 50, 250, 350)),))
    )
    units = ocr_pdf_pages(pdf)
    width, height = _png_size(fake.calls[0])
    assert abs(width - 200) <= 1 and abs(height - 300) <= 1
    assert (units[0].boundingBox.width, units[0].boundingBox.height) == pytest.approx(
        (200, 300)
    )


def _oversize_renderer(monkeypatch, oversized_dpis):
    """Wrap ``_render_page_png``: pad output over the limit for the given dpis."""
    real = ocr_pdf._render_page_png
    dpis = []

    def wrapper(page, dpi):
        dpis.append(dpi)
        data = real(page, dpi)
        if dpi in oversized_dpis:
            data += b"\0" * (ocr_pdf.TEXTRACT_MAX_SYNC_BYTES + 1)
        return data

    monkeypatch.setattr(ocr_pdf, "_render_page_png", wrapper)
    return dpis


def test_oversized_png_is_rerendered_exactly_once_at_150(monkeypatch, install):
    monkeypatch.setenv(ocr_pdf.RASTER_DPI_ENV_VAR, "200")
    fake = install()
    dpis = _oversize_renderer(monkeypatch, {200})
    ocr_pdf_pages(simple_text_pdf(["x"]))
    assert dpis == [200, 150]
    assert len(fake.calls) == 1
    assert len(fake.calls[0]) <= ocr_pdf.TEXTRACT_MAX_SYNC_BYTES


def test_oversized_png_at_150_dpi_is_not_rerendered(monkeypatch, install):
    monkeypatch.setenv(ocr_pdf.RASTER_DPI_ENV_VAR, "150")
    fake = install()
    dpis = _oversize_renderer(monkeypatch, {150})
    ocr_pdf_pages(simple_text_pdf(["x"]))
    assert dpis == [150]
    assert len(fake.calls) == 1
    assert len(fake.calls[0]) > ocr_pdf.TEXTRACT_MAX_SYNC_BYTES


# ---------------------------------------------------------------------------
# Unit construction
# ---------------------------------------------------------------------------
def test_one_unit_per_page_with_empty_unit_for_page_without_words(install):
    useful = _word_response([("Patti", 0.1, 0.1, 0.2, 0.05)])
    fake = install([useful, {"Blocks": []}, useful])
    units = ocr_pdf_pages(simple_multi_page_pdf([["a"], [], ["b"]]))
    assert len(fake.calls) == 3
    assert [u.position.pageIndex for u in units] == [0, 1, 2]
    assert [u.zone for u in units] == ["ocr"] * 3
    assert units[0].text == "Patti" and len(units[0].ocrWords) == 1
    assert units[1].text == "" and units[1].ocrWords == []


def test_words_without_text_or_geometry_are_dropped(install):
    response = {
        "Blocks": [
            {"BlockType": "WORD", "Id": "a", "Text": "", "Geometry": {
                "BoundingBox": {"Left": 0, "Top": 0, "Width": 1, "Height": 1}}},
            {"BlockType": "WORD", "Id": "b", "Text": "nogeo"},
            {"BlockType": "LINE", "Id": "l",
             "Relationships": [{"Type": "CHILD", "Ids": ["a", "b"]}]},
        ]
    }
    install([response])
    units = ocr_pdf_pages(simple_text_pdf(["x"]))
    assert len(units) == 1
    assert units[0].text == "" and units[0].ocrWords == []


# ---------------------------------------------------------------------------
# Failures with a page index
# ---------------------------------------------------------------------------
def test_page_load_failure_raises_with_page_index(monkeypatch, install):
    fake = install()
    real = ocr_pdf.pdfium.PdfDocument.__getitem__

    def getitem(self, index):
        if index == 1:
            raise RuntimeError("boom")
        return real(self, index)

    monkeypatch.setattr(ocr_pdf.pdfium.PdfDocument, "__getitem__", getitem)
    with pytest.raises(OcrPathError) as info:
        ocr_pdf_pages(simple_multi_page_pdf([["a"], ["b"], ["c"]]))
    assert info.value.pageIndex == 1
    assert "1" in str(info.value)
    assert len(fake.calls) == 1  # page 0 only


def test_rasterize_failure_raises_with_page_index(monkeypatch, install):
    fake = install()
    real = ocr_pdf._render_page_png
    seen = []

    def render(page, dpi):
        seen.append(dpi)
        if len(seen) == 2:
            raise RuntimeError("render boom")
        return real(page, dpi)

    monkeypatch.setattr(ocr_pdf, "_render_page_png", render)
    with pytest.raises(OcrPathError) as info:
        ocr_pdf_pages(simple_multi_page_pdf([["a"], ["b"]]))
    assert info.value.pageIndex == 1
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Failures without a page index (Textract never called)
# ---------------------------------------------------------------------------
def test_unopenable_document_raises_without_page_index(install):
    fake = install()
    with pytest.raises(OcrPathError) as info:
        ocr_pdf_pages(b"this is not a pdf")
    assert info.value.pageIndex is None
    assert fake.calls == []


def test_user_password_required_raises_without_page_index(install):
    fake = install()
    pdf = build_pdf(DocSpec(encryption=USER_REQUIRED))
    with pytest.raises(OcrPathError) as info:
        ocr_pdf_pages(pdf)
    assert info.value.pageIndex is None
    assert fake.calls == []


def test_client_creation_failure_raises_without_page_index(monkeypatch, sleeps):
    def boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(ocr_pdf, "_get_textract_client", boom)
    with pytest.raises(OcrPathError) as info:
        ocr_pdf_pages(simple_text_pdf(["x"]))
    assert info.value.pageIndex is None
    assert sleeps == []


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [_AwsError("ThrottlingException", 400), _AwsError("SomethingOdd", 503)],
    ids=["throttle", "5xx"],
)
def test_transient_errors_are_retried_with_backoff(install, sleeps, error):
    ok = _word_response([("hi", 0.1, 0.1, 0.1, 0.1)])
    fake = install([error, error, ok])
    units = ocr_pdf_pages(simple_text_pdf(["x"]))
    assert len(fake.calls) == 3
    assert sleeps == [1.0, 2.0]
    assert units[0].text == "hi"


@pytest.mark.parametrize(
    "error",
    [_AwsError("ProvisionedThroughputExceededException", 400), _AwsError("Oops", 500)],
    ids=["throttle", "5xx"],
)
def test_retries_exhausted_raises_with_page_index(install, sleeps, error):
    fake = install([error])
    with pytest.raises(OcrPathError) as info:
        ocr_pdf_pages(simple_text_pdf(["x"]))
    assert info.value.pageIndex == 0
    assert len(fake.calls) == ocr_pdf.MAX_ATTEMPTS == 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    "error",
    [_AwsError("AccessDeniedException", 403), _AwsError("BadDocumentException", 400),
     ValueError("plain")],
    ids=["denied", "bad-doc", "plain"],
)
def test_non_retryable_error_is_not_retried(install, sleeps, error):
    fake = install([error])
    with pytest.raises(OcrPathError) as info:
        ocr_pdf_pages(simple_text_pdf(["x"]))
    assert info.value.pageIndex == 0
    assert len(fake.calls) == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# Property 17: one unit per page
# ---------------------------------------------------------------------------
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

_page_geometry = st.tuples(
    st.integers(min_value=100, max_value=400),
    st.integers(min_value=100, max_value=400),
    st.sampled_from([0, 90, 180, 270]),
)
_page_words = st.lists(
    st.text(alphabet="abcXYZ0189", min_size=1, max_size=6), min_size=0, max_size=4
)


# Feature: pymupdf-replacement, Property 17: OCR 每页一个单元
# Validates: Requirements 7.1, 7.9
# Note: the requirement allows N >= 0, but the fixture builder cannot produce a
# zero-page PDF, so N is drawn from 1..5 here.
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    pages=st.lists(st.tuples(_page_geometry, _page_words), min_size=1, max_size=5)
)
def test_property_17_one_ocr_unit_per_page(monkeypatch, install, pages):
    monkeypatch.setenv(ocr_pdf.RASTER_DPI_ENV_VAR, "72")
    script = [
        _word_response(
            [(word, 0.1, 0.1 + 0.1 * i, 0.2, 0.05) for i, word in enumerate(words)]
        )
        if words
        else {"Blocks": []}
        for _geometry, words in pages
    ]
    fake = install(script)
    pdf = build_pdf(
        DocSpec(
            pages=tuple(
                PageSpec(media_box=(0, 0, w, h), rotate=rotate)
                for (w, h, rotate), _words in pages
            )
        )
    )

    units = ocr_pdf_pages(pdf)

    assert len(units) == len(pages)
    assert len(fake.calls) == len(pages)  # one Textract call per page
    for i, (unit, ((w, h, rotate), words)) in enumerate(zip(units, pages)):
        assert unit.position.index == i
        assert unit.position.pageIndex == i
        assert unit.zone == "ocr"
        visible = (h, w) if rotate in (90, 270) else (w, h)
        assert (unit.boundingBox.width, unit.boundingBox.height) == pytest.approx(
            visible
        )
        assert (unit.text == "") == (not words)


# ---------------------------------------------------------------------------
# Property 18: DPI configuration parsing
# ---------------------------------------------------------------------------
import re  # noqa: E402

_WS = st.text(alphabet=" \t\n", max_size=3)
_ASCII_INT = re.compile(r"[+-]?[0-9]+")


def _dpi_expected(raw):
    """Independent oracle: ASCII integer in [72, 400] -> that int, else 200."""
    stripped = raw.strip()
    if _ASCII_INT.fullmatch(stripped):
        value = int(stripped)
        if 72 <= value <= 400:
            return value
    return 200


_dpi_values = st.one_of(
    st.none(),  # variable missing
    _WS,  # empty / whitespace-only
    st.text(alphabet="abcxyz!@#", min_size=1, max_size=8),  # non-numeric
    st.floats(min_value=0, max_value=1000, allow_nan=False).map(
        lambda f: f"{f:.1f}" if f != int(f) else f"{f:.1f}"
    ),  # non-integer-looking floats such as "200.5" / "200.0"
    st.integers(min_value=-1000, max_value=71).map(str),  # below range
    st.integers(min_value=401, max_value=100000).map(str),  # above range
    st.builds(
        lambda n, a, b: f"{a}{n}{b}", st.integers(72, 400), _WS, _WS
    ),  # in range, optionally padded
    st.builds(lambda n: f"+{n}", st.integers(72, 400)),  # explicit plus sign
    st.text(alphabet="0123456789+-. \t", max_size=8),  # arbitrary numeric-ish
)


# Feature: pymupdf-replacement, Property 18: DPI 配置解析
# **Validates: Requirements 7.6**
@settings(max_examples=100, deadline=None)
@given(raw=_dpi_values)
def test_property_18_dpi_config_parsing(raw):
    with pytest.MonkeyPatch.context() as mp:
        if raw is None:
            mp.delenv(ocr_pdf.RASTER_DPI_ENV_VAR, raising=False)
        else:
            mp.setenv(ocr_pdf.RASTER_DPI_ENV_VAR, raw)
        result = ocr_pdf._read_raster_dpi()
    assert result == _dpi_expected("" if raw is None else raw)
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Property 19: Textract response assembly
# ---------------------------------------------------------------------------
_unit_float = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
_ocr_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp")),
    min_size=1,
    max_size=8,
)
_valid_word = st.fixed_dictionaries(
    {
        "kind": st.just("valid"),
        "text": _ocr_text,
        "box": st.tuples(_unit_float, _unit_float, _unit_float, _unit_float),
    }
)
_unusable_word = st.one_of(
    st.fixed_dictionaries(
        {
            "kind": st.just("empty_text"),
            "text": st.sampled_from(["", None]),
            "box": st.tuples(_unit_float, _unit_float, _unit_float, _unit_float),
        }
    ),
    st.fixed_dictionaries(
        {
            "kind": st.just("bad_geometry"),
            "text": _ocr_text,
            "variant": st.sampled_from(
                [
                    "no_geometry",
                    "no_bbox",
                    "bbox_not_dict",
                    "missing_key",
                    "non_numeric",
                    "none_value",
                ]
            ),
            "box": st.tuples(_unit_float, _unit_float, _unit_float, _unit_float),
        }
    ),
)
_other_child = st.one_of(
    st.just({"kind": "dangling"}),
    st.fixed_dictionaries(
        {
            "kind": st.just("non_word"),
            "block_type": st.sampled_from(["SELECTION_ELEMENT", "CELL", "LINE"]),
            "text": _ocr_text,
        }
    ),
)
_child = st.one_of(_valid_word, _valid_word, _unusable_word, _other_child)
_line = st.fixed_dictionaries(
    {"children": st.lists(_child, max_size=5), "duplicate": st.booleans()}
)


def _bad_geometry(variant, box):
    left, top, width, height = box
    good = {"Left": left, "Top": top, "Width": width, "Height": height}
    if variant == "no_geometry":
        return {}
    if variant == "no_bbox":
        return {"Geometry": {}}
    if variant == "bbox_not_dict":
        return {"Geometry": {"BoundingBox": [left, top, width, height]}}
    if variant == "missing_key":
        del good["Width"]
        return {"Geometry": {"BoundingBox": good}}
    if variant == "non_numeric":
        good["Top"] = "not-a-number"
        return {"Geometry": {"BoundingBox": good}}
    good["Height"] = None
    return {"Geometry": {"BoundingBox": good}}


def _assemble(lines):
    """Build (response, expected_lines); expected_lines holds the
    (text, (left, top, width, height)) of every usable word, per LINE block."""
    blocks = [{"BlockType": "PAGE", "Id": "page", "Relationships": []}]
    expected = []
    counter = 0
    for line in lines:
        ids = []
        usable = []
        for child in line["children"]:
            counter += 1
            cid = "c%d" % counter
            kind = child["kind"]
            if kind == "dangling":
                ids.append("missing-%d" % counter)
                continue
            ids.append(cid)
            if kind == "non_word":
                blocks.append(
                    {"BlockType": child["block_type"], "Id": cid, "Text": child["text"]}
                )
                continue
            block = {"BlockType": "WORD", "Id": cid}
            if child["text"] is not None:
                block["Text"] = child["text"]
            if kind == "bad_geometry":
                block.update(_bad_geometry(child["variant"], child["box"]))
            else:
                left, top, width, height = child["box"]
                block["Geometry"] = {
                    "BoundingBox": {
                        "Left": left,
                        "Top": top,
                        "Width": width,
                        "Height": height,
                    }
                }
            blocks.append(block)
            if kind == "valid":
                usable.append((child["text"], child["box"]))
        counter += 1
        copies = 2 if line["duplicate"] else 1
        for copy in range(copies):
            blocks.append(
                {
                    "BlockType": "LINE",
                    "Id": "l%d_%d" % (counter, copy),
                    "Text": "ignored line text",
                    "Relationships": [
                        {"Type": "VALUE", "Ids": ["ignored"]},
                        {"Type": "CHILD", "Ids": list(ids)},
                    ],
                }
            )
            expected.append(usable)
    return {"Blocks": blocks}, expected


class _SizedPage:
    def get_size(self):
        return (612.0, 792.0)


# Feature: pymupdf-replacement, Property 19: Textract 响应拼接
# **Validates: Requirements 7.12, 7.2**
@settings(max_examples=100, deadline=None)
@given(lines=st.lists(_line, max_size=6))
def test_property_19_textract_response_assembly(lines):
    response, expected_lines = _assemble(lines)
    unit = ocr_pdf._build_page_unit(_SizedPage(), 0, response)

    non_empty = [words for words in expected_lines if words]
    expected_text = "\n".join(
        " ".join(text for text, _box in words) for words in non_empty
    )
    expected_words = [word for words in non_empty for word in words]

    assert unit.text == expected_text
    assert not unit.text.startswith(("\n", " "))
    assert not unit.text.endswith(("\n", " "))
    assert "\n\n" not in unit.text

    assert [w.text for w in unit.ocrWords] == [t for t, _ in expected_words]
    previous_end = -1
    for word, (text, (left, top, width, height)) in zip(unit.ocrWords, expected_words):
        assert unit.text[word.startOffset : word.endOffset] == text
        assert word.startOffset > previous_end or previous_end == -1
        assert word.startOffset < word.endOffset
        previous_end = word.endOffset
        box = word.boundingBox
        assert (box.left, box.top, box.width, box.height) == (left, top, width, height)

    if not expected_words:
        assert unit.text == ""
        assert unit.ocrWords == []
