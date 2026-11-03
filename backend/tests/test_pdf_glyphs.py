"""Tests for ``agent.tools.pdf_glyphs`` (glyph / character traversal)."""
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_LAMBDAS = os.path.join(_BACKEND, "lambdas")
if _LAMBDAS not in sys.path:
    sys.path.insert(0, _LAMBDAS)

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent.tools.pdf_glyphs import collect_page_glyphs, open_pdf  # noqa: E402
from tests.pdf_fixtures import (  # noqa: E402
    CJK_FONT,
    DocSpec,
    PageSpec,
    TextRun,
    build_pdf,
    cjk_text,
)


# ---------------------------------------------------------------------------
# Property 4: character offset mapping invariant
# ---------------------------------------------------------------------------

@st.composite
def _ligature_docs(draw):
    """A document whose CJK font maps some glyphs to multi-character strings."""
    ligatures = tuple(
        draw(st.lists(cjk_text(min_size=2, max_size=3), min_size=1, max_size=3, unique=True))
    )
    runs = []
    for index in range(draw(st.integers(1, 3))):
        pieces = draw(
            st.lists(
                st.one_of(st.sampled_from(ligatures), cjk_text(min_size=1, max_size=3)),
                min_size=1,
                max_size=4,
            )
        )
        runs.append(
            TextRun(
                text="".join(pieces),
                x=72,
                y=700 - 40 * index,
                font=CJK_FONT,
                size=draw(st.integers(10, 20)),
            )
        )
    return DocSpec(pages=(PageSpec(runs=tuple(runs)),), cjk_ligatures=ligatures)


# Feature: pymupdf-replacement, Property 4: 字符偏移映射不变量
# **Validates: Requirements 5.5**
@settings(max_examples=100, deadline=None)
@given(doc=_ligature_docs())
def test_char_offset_mapping_invariant(doc):
    handle = open_pdf(build_pdf(doc))
    page = collect_page_glyphs(handle, 0)

    # len(text) equals the number of character positions; text[i] == chars[i].char
    assert len(page.text) == len(page.chars)
    for i, pos in enumerate(page.chars):
        assert page.text[i] == pos.char

    # Each glyph of text length n contributes n consecutive chars sharing its
    # rect; glyphs with empty text contribute none.
    cursor = 0
    multi_char_seen = False
    for glyph in page.glyphs:
        n = len(glyph.text)
        if n == 0:
            continue
        multi_char_seen = multi_char_seen or n > 1
        group = page.chars[cursor:cursor + n]
        assert len(group) == n
        assert "".join(c.char for c in group) == glyph.text
        for c in group:
            assert c.glyph is glyph
            assert c.rect == glyph.rect
        cursor += n
    assert cursor == len(page.chars)
    assert len(page.text) == sum(len(g.text) for g in page.glyphs)


# ---------------------------------------------------------------------------
# Example tests
# ---------------------------------------------------------------------------
import pytest  # noqa: E402

from agent.tools.pdf_glyphs import (  # noqa: E402
    PageReadError,
    PdfOpenError,
    PdfPasswordError,
)
from tests.pdf_fixtures import (  # noqa: E402
    OWNER_ONLY,
    USER_REQUIRED,
    FormPlacement,
    FormSpec,
)


def _page(doc: DocSpec):
    return collect_page_glyphs(open_pdf(build_pdf(doc)), 0)


def test_ssn_split_across_two_lines_without_space():
    doc = DocSpec(pages=(PageSpec(runs=(
        TextRun(text="123-4", x=72, y=700),
        TextRun(text="5-6789", x=72, y=680),
    )),))
    page = _page(doc)
    assert "123-45-6789" in page.text.replace("\n", "").replace(" ", "")
    first = [c for c in page.chars if c.char in "123-4"][:5]
    assert len({c.line_id for c in page.chars}) == 2
    assert len({c.line_id for c in first}) == 1
    start = page.text.index("5-6789")
    assert page.chars[start].line_id != page.chars[0].line_id


def test_multiple_tj_on_same_line_share_line_id():
    doc = DocSpec(pages=(PageSpec(runs=(
        TextRun(text="abc", x=72, y=700, op="TJ", tj_pieces=("ab", -200, "c")),
        TextRun(text="def", x=200, y=700, op="TJ"),
    )),))
    page = _page(doc)
    letters = [c for c in page.chars if c.char.isalpha()]
    assert "".join(c.char for c in letters) == "abcdef"
    assert len({c.line_id for c in letters}) == 1


def test_missing_tounicode_yields_replacement_not_cid():
    doc = DocSpec(
        pages=(PageSpec(runs=(TextRun(text="你好", font=CJK_FONT),)),),
        cjk_tounicode=False,
    )
    page = _page(doc)
    assert page.text
    assert "(cid:" not in page.text
    assert set(page.text) == {"\ufffd"}
    assert len(page.chars) == len(page.glyphs)


def test_render_mode_3_text_still_has_char_positions():
    doc = DocSpec(pages=(PageSpec(runs=(TextRun(text="Secret", tr=3),)),))
    page = _page(doc)
    assert page.text.strip() == "Secret"
    assert all(c.rect is not None for c in page.chars)


def test_form_xobject_glyphs_carry_context_path():
    form = FormSpec(name="Fm1", runs=(TextRun(text="InForm", x=72, y=500),))
    doc = DocSpec(
        pages=(PageSpec(
            runs=(TextRun(text="OnPage", x=72, y=700),),
            form_placements=(FormPlacement(form="Fm1"),),
        ),),
        forms=(form,),
    )
    handle = open_pdf(build_pdf(doc))
    page = collect_page_glyphs(handle, 0)
    on_page = [g for g in page.glyphs if g.context == ()]
    in_form = [g for g in page.glyphs if g.context]
    assert "".join(g.text for g in on_page) == "OnPage"
    assert "".join(g.text for g in in_form) == "InForm"
    contexts = {g.context for g in in_form}
    assert len(contexts) == 1
    ((do_ordinal, form_objid),) = contexts.pop()
    # Page stream: BT Tf Tm Tj ET q cm Do -> the Do is operator #7.
    assert do_ordinal == 7
    form_xobject = handle.pikepdf_doc.pages[0].Resources.XObject["/Fm1"]
    assert form_objid == form_xobject.objgen[0]
    # Ordinals inside the form count operators of the form stream (BT Tf Tm Tj).
    assert {g.op_ordinal for g in in_form} == {3}


def _mutate(data: bytes, mutate) -> bytes:
    """Apply ``mutate(pdf)`` with pikepdf and return the saved bytes."""
    import io

    import pikepdf

    with pikepdf.open(io.BytesIO(data)) as pdf:
        mutate(pdf)
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def _two_page_doc() -> bytes:
    return build_pdf(DocSpec(pages=(
        PageSpec(runs=(TextRun(text="first"),)),
        PageSpec(runs=(TextRun(text="second"),)),
    )))


def test_garbage_operands_in_content_stream_are_skipped():
    # pdfminer.six recovers from stray / mistyped operands; readable text survives.
    doc = DocSpec(pages=(PageSpec(
        runs=(TextRun(text="abc"),),
        raw_content=b"/A /B /C /D /E /F cm\nBT /F1 (x) Tf ET\n",
    ),))
    assert _page(doc).text.strip() == "abc"


def test_unreadable_font_raises_page_read_error_with_page_index():
    import pikepdf

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

    handle = open_pdf(_mutate(_two_page_doc(), break_font))
    assert collect_page_glyphs(handle, 0).text.strip() == "first"
    with pytest.raises(PageReadError) as info:
        collect_page_glyphs(handle, 1)
    assert info.value.page_index == 1


def test_corrupt_flate_content_stream_raises_page_read_error():
    import pikepdf

    def corrupt_contents(pdf):
        stream = pdf.make_stream(b"")
        stream.write(b"this is not zlib data", filter=None)
        stream.Filter = pikepdf.Name.FlateDecode
        pdf.pages[1].obj.Contents = stream

    handle = open_pdf(_mutate(_two_page_doc(), corrupt_contents))
    with pytest.raises(PageReadError) as info:
        collect_page_glyphs(handle, 1)
    assert info.value.page_index == 1


def test_owner_password_only_opens_and_reads():
    doc = DocSpec(
        pages=(PageSpec(runs=(TextRun(text="Hello"),)),), encryption=OWNER_ONLY
    )
    assert _page(doc).text.strip() == "Hello"


def test_user_password_required_raises_password_error():
    doc = DocSpec(
        pages=(PageSpec(runs=(TextRun(text="Hello"),)),), encryption=USER_REQUIRED
    )
    with pytest.raises(PdfPasswordError):
        open_pdf(build_pdf(doc))


@pytest.mark.parametrize("data", [b"", b"not a pdf at all", b"%PDF-1.7\njunk\n"])
def test_empty_or_junk_bytes_raise_open_error(data):
    with pytest.raises(PdfOpenError):
        open_pdf(data)
