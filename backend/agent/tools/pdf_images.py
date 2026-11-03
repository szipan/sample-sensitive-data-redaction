"""Image XObject listing and decoding for PDF extraction and redaction.

* ``iter_page_image_refs`` lists the Image XObjects a page references, in a
  stable order: first the placed images in content-stream drawing order (one
  entry per object, rectangle of the first placement), then the images that
  are reachable from the page resources (including resources of Form XObjects
  used by the page) but never drawn, sorted by resource name.
* ``decode_image`` returns bytes that Pillow can open: the original JPEG when
  it can be used as is, otherwise a PNG.

Rectangles are ``(left, top, right, bottom)`` in Page_Coordinates.
"""
import io
import logging
import zlib
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

import pikepdf
from PIL import Image

from agent.tools.pdf_geometry import Rect
from agent.tools.pdf_glyphs import PageGlyphs, PdfHandle

logger = logging.getLogger(__name__)


class ImageWriteBackError(Exception):
    """Raised when a replacement image cannot be stored in the document.

    The message states the reason (undecodable bytes, not an image object,
    stencil mask, unsupported encoding).
    """


@dataclass(frozen=True)
class ImageRef:
    """An Image XObject referenced by a page."""

    xref: int  # object number of the image XObject
    # Rectangle of the first placement; None when the image is never drawn.
    bounding_box: Optional[Rect]


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _collect_resource_images(
    resources, visited: Set[Tuple[int, int]], out: List[int]
) -> None:
    """Append the xrefs of Image XObjects in ``resources`` (depth first, by
    resource name), descending into Form XObjects once each."""
    if resources is None:
        return
    try:
        xobjects = resources.get("/XObject")
        if xobjects is None or not isinstance(xobjects, pikepdf.Dictionary):
            return
        names = sorted(str(k) for k in xobjects.keys())
        for name in names:
            obj = xobjects.get(name)
            if obj is None or not isinstance(obj, pikepdf.Stream):
                continue
            subtype = obj.get("/Subtype")
            objgen = obj.objgen
            if subtype == "/Image":
                if objgen[0] > 0:
                    out.append(objgen[0])
            elif subtype == "/Form":
                if objgen in visited:
                    continue
                visited.add(objgen)
                _collect_resource_images(obj.get("/Resources"), visited, out)
    except Exception:  # damaged resources: list what was found so far
        logger.debug("cannot read XObject resources", exc_info=True)


def iter_page_image_refs(
    handle: PdfHandle, page_index: int, page_glyphs: PageGlyphs
) -> List[ImageRef]:
    """Image XObjects referenced by page ``page_index``, one entry per object."""
    refs: List[ImageRef] = []
    seen: Set[int] = set()
    for placement in page_glyphs.image_placements:
        if placement.xref in seen:
            continue
        seen.add(placement.xref)
        refs.append(ImageRef(xref=placement.xref, bounding_box=placement.rect))

    unplaced: List[int] = []
    try:
        page = handle.pikepdf_doc.pages[page_index]
        resources = page.obj.get("/Resources")
        if resources is None:
            resources = page.get("/Resources")
    except Exception:
        logger.debug("cannot read page resources", exc_info=True)
        resources = None
    _collect_resource_images(resources, set(), unplaced)
    for xref in unplaced:
        if xref in seen:
            continue
        seen.add(xref)
        refs.append(ImageRef(xref=xref, bounding_box=None))
    return refs


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _get_image_object(pdf: "pikepdf.Pdf", xref: int):
    obj = pdf.get_object((xref, 0))
    if not isinstance(obj, pikepdf.Stream) or obj.get("/Subtype") != "/Image":
        return None
    return obj


def _single_filter(obj) -> Optional[str]:
    flt = obj.get("/Filter")
    if flt is None:
        return None
    if isinstance(flt, pikepdf.Array):
        if len(flt) != 1:
            return None
        flt = flt[0]
    return str(flt)


def _passthrough_jpeg(obj, width: int, height: int) -> Optional[bytes]:
    """The raw JPEG bytes when the stream can be handed to Pillow unchanged."""
    if _single_filter(obj) != "/DCTDecode":
        return None
    if obj.get("/ColorSpace") not in ("/DeviceRGB", "/DeviceGray"):
        return None
    if "/SMask" in obj or "/Decode" in obj or "/Mask" in obj:
        return None
    if obj.get("/ImageMask", False):
        return None
    data = bytes(obj.read_raw_bytes())
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if img.size != (width, height):
                return None
    except Exception:
        return None
    return data


def _load_smask(obj, size: Tuple[int, int]) -> Optional[Image.Image]:
    smask = obj.get("/SMask")
    if smask is None or not isinstance(smask, pikepdf.Stream):
        return None
    alpha = pikepdf.PdfImage(smask).as_pil_image().convert("L")
    if alpha.size != size:
        alpha = alpha.resize(size, Image.BILINEAR)
    return alpha


def decode_image(pdf: "pikepdf.Pdf", xref: int) -> Optional[bytes]:
    """Bytes of image object ``xref`` that Pillow can open, or None."""
    try:
        obj = _get_image_object(pdf, xref)
        if obj is None:
            return None
        width = int(obj["/Width"])
        height = int(obj["/Height"])
        if width < 1 or height < 1:
            return None

        jpeg = _passthrough_jpeg(obj, width, height)
        if jpeg is not None:
            return jpeg

        img = pikepdf.PdfImage(obj).as_pil_image()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if img.size != (width, height):
            return None
        alpha = _load_smask(obj, img.size)
        if alpha is not None:
            if img.mode == "L":
                img = img.convert("RGB")
            img.putalpha(alpha)
        out = io.BytesIO()
        img.save(out, format="PNG")
        data = out.getvalue()
        return data or None
    except Exception:
        logger.debug("cannot decode image xref %s", xref, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Writing back
# ---------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_STALE_KEYS = ("/Decode", "/SMaskInData", "/Mask", "/Alternates")


def _png_flate_parts(img: Image.Image) -> Tuple[bytes, Optional[dict]]:
    """Encode ``img`` (mode L or RGB) as PNG and return the concatenated IDAT
    data with the matching ``/DecodeParms`` (None for a plain zlib stream)."""
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    png = out.getvalue()
    if not png.startswith(_PNG_SIGNATURE):
        raise ImageWriteBackError("PNG encoding failed")
    pos = len(_PNG_SIGNATURE)
    idat = []
    interlaced = False
    while pos + 8 <= len(png):
        length = int.from_bytes(png[pos:pos + 4], "big")
        kind = png[pos + 4:pos + 8]
        body = png[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            interlaced = body[12] != 0
        elif kind == b"IDAT":
            idat.append(body)
        pos += 12 + length
    data = b"".join(idat)
    if not data:
        raise ImageWriteBackError("PNG encoding produced no image data")
    if interlaced:
        raw = img.tobytes()
        return zlib.compress(raw, 9), None
    colors = 1 if img.mode == "L" else 3
    parms = {
        "/Predictor": 15,
        "/Colors": colors,
        "/BitsPerComponent": 8,
        "/Columns": img.width,
    }
    return data, parms


def _write_flate(pdf: "pikepdf.Pdf", stream, img: Image.Image) -> None:
    data, parms = _png_flate_parts(img)
    decode_parms = pikepdf.Dictionary(parms) if parms else None
    stream.write(data, filter=pikepdf.Name.FlateDecode, decode_parms=decode_parms)


def write_back_image(pdf: "pikepdf.Pdf", xref: int, new_bytes: bytes) -> None:
    """Replace the pixels of Image XObject ``xref`` with ``new_bytes``.

    JPEG input is stored as a DCTDecode stream; everything else becomes a
    FlateDecode stream with PNG predictors. Output with an alpha channel gets
    a new ``/SMask``; output without one drops any existing ``/SMask`` so the
    replaced area is opaque. Raises ImageWriteBackError on any failure.
    """
    try:
        obj = _get_image_object(pdf, xref)
    except Exception as exc:
        raise ImageWriteBackError(f"object {xref} is not readable: {exc}") from exc
    if obj is None:
        raise ImageWriteBackError(f"object {xref} is not an image")
    if bool(obj.get("/ImageMask", False)):
        raise ImageWriteBackError("stencil mask images are not supported")
    if not new_bytes:
        raise ImageWriteBackError("replacement image is empty")

    try:
        with Image.open(io.BytesIO(new_bytes)) as src:
            src.load()
            fmt = src.format
            img = src.copy()
    except Exception as exc:
        raise ImageWriteBackError(f"replacement image cannot be decoded: {exc}") from exc
    width, height = img.size
    if width < 1 or height < 1:
        raise ImageWriteBackError("replacement image has no pixels")

    try:
        alpha: Optional[Image.Image] = None
        if fmt == "JPEG" and img.mode in ("L", "RGB"):
            colour = img
            jpeg = new_bytes
        else:
            jpeg = None
            has_alpha = img.mode in ("RGBA", "LA", "PA") or (
                img.mode == "P" and "transparency" in img.info
            )
            if has_alpha:
                rgba = img.convert("RGBA") if img.mode != "LA" else img
                alpha = rgba.getchannel("A")
                if img.mode == "LA":
                    colour = rgba.getchannel("L")
                else:
                    colour = rgba.convert("RGB")
            elif img.mode in ("L", "1"):
                colour = img.convert("L")
            else:
                colour = img.convert("RGB")

        if jpeg is not None:
            obj.write(jpeg, filter=pikepdf.Name.DCTDecode)
        else:
            _write_flate(pdf, obj, colour)

        obj["/Width"] = width
        obj["/Height"] = height
        obj["/ColorSpace"] = pikepdf.Name(
            "/DeviceGray" if colour.mode == "L" else "/DeviceRGB"
        )
        obj["/BitsPerComponent"] = 8
        for key in _STALE_KEYS:
            if key in obj:
                del obj[key]
        if "/SMask" in obj:
            del obj["/SMask"]

        if alpha is not None:
            smask = pdf.make_stream(b"")
            _write_flate(pdf, smask, alpha)
            smask["/Type"] = pikepdf.Name.XObject
            smask["/Subtype"] = pikepdf.Name.Image
            smask["/Width"] = width
            smask["/Height"] = height
            smask["/ColorSpace"] = pikepdf.Name.DeviceGray
            smask["/BitsPerComponent"] = 8
            obj["/SMask"] = smask
    except ImageWriteBackError:
        raise
    except Exception as exc:
        raise ImageWriteBackError(f"cannot store replacement image: {exc}") from exc
