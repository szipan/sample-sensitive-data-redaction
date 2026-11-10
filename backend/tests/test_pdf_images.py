"""Tests for ``agent.tools.pdf_images`` (listing, decoding, write-back)."""
import io
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_LAMBDAS = os.path.join(_BACKEND, "lambdas")
if _LAMBDAS not in sys.path:
    sys.path.insert(0, _LAMBDAS)

import pikepdf  # noqa: E402
import pypdfium2  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from agent.tools.pdf_glyphs import collect_page_glyphs, open_pdf  # noqa: E402
from agent.tools.pdf_images import (  # noqa: E402
    ImageWriteBackError,
    decode_image,
    iter_page_image_refs,
    write_back_image,
)
from tests.pdf_fixtures import (  # noqa: E402
    DocSpec,
    FormPlacement,
    FormSpec,
    ImageSpec,
    PageSpec,
    build_pdf,
)

W, H = 12, 7  # deliberately non-square, distinct from the placement size


def _page_refs(pdf_bytes, page_index=0):
    handle = open_pdf(pdf_bytes)
    glyphs = collect_page_glyphs(handle, page_index)
    return handle, iter_page_image_refs(handle, page_index, glyphs)


def _doc(*images, forms=(), **page_kwargs):
    return build_pdf(DocSpec(pages=(PageSpec(images=tuple(images), **page_kwargs),), forms=forms))


def _xobj_stream(pdf, xref):
    return pdf.get_object((xref, 0))


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kind,smask,fmt,mode",
    [
        ("rgb", False, "PNG", "RGB"),
        ("gray", False, "PNG", "L"),
        ("dct", False, "JPEG", "RGB"),
        ("dct_gray", False, "JPEG", "L"),
        ("rgb", True, "PNG", "RGBA"),
        ("gray", True, "PNG", "RGBA"),
        ("indexed", False, "PNG", "RGB"),
    ],
)
def test_decode_image_kinds(kind, smask, fmt, mode):
    handle, refs = _page_refs(_doc(ImageSpec(kind=kind, width=W, height=H, smask=smask)))
    assert len(refs) == 1
    stream = _xobj_stream(handle.pikepdf_doc, refs[0].xref)
    data = decode_image(handle.pikepdf_doc, refs[0].xref)
    assert data is not None
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        assert img.format == fmt
        assert img.size == (int(stream["/Width"]), int(stream["/Height"])) == (W, H)
        assert img.mode == mode


def test_decode_image_corrupt_flate_returns_none():
    pdf_bytes = _doc(ImageSpec(kind="rgb", width=W, height=H))
    handle, refs = _page_refs(pdf_bytes)
    stream = _xobj_stream(handle.pikepdf_doc, refs[0].xref)
    stream.write(b"\x00not a zlib stream\xff" * 3, filter=pikepdf.Name.FlateDecode)
    assert decode_image(handle.pikepdf_doc, refs[0].xref) is None


def test_decode_image_corrupt_dct_returns_none():
    handle, refs = _page_refs(_doc(ImageSpec(kind="dct", width=W, height=H)))
    stream = _xobj_stream(handle.pikepdf_doc, refs[0].xref)
    stream.write(b"\xff\xd8garbage-not-a-jpeg", filter=pikepdf.Name.DCTDecode)
    assert decode_image(handle.pikepdf_doc, refs[0].xref) is None


def test_decode_image_non_image_object_returns_none():
    handle, _ = _page_refs(_doc(ImageSpec()))
    page_xref = handle.pikepdf_doc.pages[0].obj.objgen[0]
    assert decode_image(handle.pikepdf_doc, page_xref) is None
    assert decode_image(handle.pikepdf_doc, 99999) is None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def test_refs_follow_drawing_order_and_dedup():
    a = ImageSpec(kind="rgb", width=W, height=H, placements=((72.0, 600.0, 50.0, 50.0), (300.0, 100.0, 40.0, 40.0)))
    b = ImageSpec(kind="gray", width=W, height=H, placements=((200.0, 300.0, 60.0, 30.0),))
    _, refs = _page_refs(_doc(a, b))
    assert len(refs) == 2
    assert len({r.xref for r in refs}) == 2
    first, second = refs
    # first placement rectangle of the first image, in drawing order
    assert first.bounding_box is not None and second.bounding_box is not None
    l, t, r, btm = first.bounding_box
    assert (round(l), round(r - l), round(btm - t)) == (72, 50, 50)
    l, t, r, btm = second.bounding_box
    assert (round(l), round(r - l), round(btm - t)) == (200, 60, 30)


def test_shared_key_gives_single_entry():
    a = ImageSpec(key="k", width=W, height=H, placements=((72.0, 600.0, 50.0, 50.0),))
    b = ImageSpec(key="k", width=W, height=H, placements=((200.0, 300.0, 50.0, 50.0),))
    _, refs = _page_refs(_doc(a, b))
    assert len(refs) == 1
    assert refs[0].bounding_box is not None


def test_unplaced_image_has_no_bounding_box_and_comes_last():
    placed = ImageSpec(kind="rgb", width=W, height=H)
    unplaced = ImageSpec(kind="gray", width=W, height=H, placements=())
    _, refs = _page_refs(_doc(placed, unplaced))
    assert len(refs) == 2
    assert refs[0].bounding_box is not None
    assert refs[1].bounding_box is None
    assert refs[0].xref != refs[1].xref


def test_image_only_inside_form_xobject_is_listed():
    form = FormSpec(
        name="Fm1",
        images=(ImageSpec(kind="rgb", width=W, height=H, placements=((10.0, 10.0, 80.0, 80.0),)),),
    )
    pdf_bytes = build_pdf(
        DocSpec(
            pages=(PageSpec(form_placements=(FormPlacement(form="Fm1", matrix=(1, 0, 0, 1, 100, 200)),)),),
            forms=(form,),
        )
    )
    handle, refs = _page_refs(pdf_bytes)
    assert len(refs) == 1
    stream = _xobj_stream(handle.pikepdf_doc, refs[0].xref)
    assert stream["/Subtype"] == "/Image"
    # not in the page's own resources, so it can only be reached through the form
    page_xobjects = handle.pikepdf_doc.pages[0].obj["/Resources"].get("/XObject", {})
    assert all(page_xobjects[k]["/Subtype"] != "/Image" for k in page_xobjects.keys())


def test_form_with_unplaced_image_listed_from_resources():
    form = FormSpec(name="Fm1", images=(ImageSpec(kind="gray", width=W, height=H, placements=()),))
    pdf_bytes = build_pdf(
        DocSpec(pages=(PageSpec(form_placements=(FormPlacement(form="Fm1"),)),), forms=(form,))
    )
    _, refs = _page_refs(pdf_bytes)
    assert len(refs) == 1
    assert refs[0].bounding_box is None


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------
def _png(mode, color):
    img = Image.new(mode, (20, 10), color)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _jpeg_black():
    out = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 0, 0)).save(out, format="JPEG", quality=95)
    return out.getvalue()


def _distinct_pixels(kind):
    n = 3 if kind == "rgb" else 1
    return bytes((i * 37 + 11) % 256 for i in range(W * H * n))


def _render_region(pdf_bytes, placement):
    """Pixels fully inside the placement box (scale 1, 612x792 page)."""
    x, y, w, h = placement
    doc = pypdfium2.PdfDocument(pdf_bytes)
    try:
        bitmap = doc[0].render(scale=1.0)
        img = bitmap.to_pil().convert("RGB")
    finally:
        doc.close()
    top = 792 - (y + h)
    box = (int(x) + 3, int(top) + 3, int(x + w) - 3, int(top + h) - 3)
    return img.crop(box), img


@pytest.mark.parametrize("variant", ["png_rgb", "png_rgba", "jpeg", "png_gray"])
def test_write_back_renders_black_and_drops_original_data(variant):
    placement = (72.0, 600.0, 144.0, 144.0)
    pixels = _distinct_pixels("rgb")
    spec = ImageSpec(kind="rgb", width=W, height=H, placements=(placement,), pixels=pixels, smask=(variant == "png_rgba"))
    pdf_bytes = _doc(spec)

    new_bytes = {
        "png_rgb": _png("RGB", (0, 0, 0)),
        "png_rgba": _png("RGBA", (0, 0, 0, 255)),
        "jpeg": _jpeg_black(),
        "png_gray": _png("L", 0),
    }[variant]

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        handle_refs = open_pdf(pdf_bytes)
        refs = iter_page_image_refs(handle_refs, 0, collect_page_glyphs(handle_refs, 0))
        xref = refs[0].xref
        original_raw = bytes(_xobj_stream(pdf, xref).read_raw_bytes())
        original_decoded = bytes(_xobj_stream(pdf, xref).read_bytes())
        assert original_decoded == pixels

        write_back_image(pdf, xref, new_bytes)

        out = io.BytesIO()
        pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    out_bytes = out.getvalue()

    # the original pixels are gone from the file
    assert original_raw not in out_bytes
    with pikepdf.open(io.BytesIO(out_bytes)) as pdf2:
        for obj in pdf2.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    assert bytes(obj.read_bytes()) != original_decoded
                except pikepdf.PdfError:
                    pass
        xobjs = pdf2.pages[0].obj["/Resources"]["/XObject"]
        stream = xobjs[sorted(xobjs.keys())[0]]
        assert stream["/Subtype"] == "/Image"
        if variant == "png_rgba":
            assert "/SMask" in stream
        else:
            assert "/SMask" not in stream
        if variant == "jpeg":
            assert str(stream["/Filter"]) == "/DCTDecode"
        else:
            assert str(stream["/Filter"]) == "/FlateDecode"

    region, _ = _render_region(out_bytes, placement)
    assert region.size[0] > 0 and region.size[1] > 0
    assert set(region.get_flattened_data()) == {(0, 0, 0)}


def test_write_back_removes_existing_smask_when_replacement_is_opaque():
    spec = ImageSpec(kind="rgb", width=W, height=H, smask=True)
    handle, refs = _page_refs(_doc(spec))
    pdf = handle.pikepdf_doc
    assert "/SMask" in _xobj_stream(pdf, refs[0].xref)
    write_back_image(pdf, refs[0].xref, _png("RGB", (0, 0, 0)))
    assert "/SMask" not in _xobj_stream(pdf, refs[0].xref)


def test_write_back_updates_dimensions_and_decodes():
    handle, refs = _page_refs(_doc(ImageSpec(kind="indexed", width=W, height=H)))
    pdf = handle.pikepdf_doc
    write_back_image(pdf, refs[0].xref, _png("RGB", (0, 0, 0)))
    stream = _xobj_stream(pdf, refs[0].xref)
    assert (int(stream["/Width"]), int(stream["/Height"])) == (20, 10)
    data = decode_image(pdf, refs[0].xref)
    assert data is not None
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (20, 10)


def test_write_back_image_mask_raises():
    handle, refs = _page_refs(_doc(ImageSpec(kind="mask", width=W, height=H)))
    with pytest.raises(ImageWriteBackError):
        write_back_image(handle.pikepdf_doc, refs[0].xref, _png("RGB", (0, 0, 0)))


def test_write_back_rejects_bad_input():
    handle, refs = _page_refs(_doc(ImageSpec(kind="rgb", width=W, height=H)))
    pdf = handle.pikepdf_doc
    with pytest.raises(ImageWriteBackError):
        write_back_image(pdf, refs[0].xref, b"not an image")
    with pytest.raises(ImageWriteBackError):
        write_back_image(pdf, refs[0].xref, b"")
    with pytest.raises(ImageWriteBackError):
        write_back_image(pdf, pdf.pages[0].obj.objgen[0], _png("RGB", (0, 0, 0)))
