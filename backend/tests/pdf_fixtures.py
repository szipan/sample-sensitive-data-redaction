"""Test-PDF constructor built only on pypdf (``PdfWriter`` + generic objects).

This module is *not* a test file.  It builds PDF bytes for the rest of the
suite and for Hypothesis strategies.  Nothing here imports a PDF library other
than ``pypdf``; Pillow is used only to make JPEG sample data and ``zlib`` only
to deflate raster samples.

Quick reference
===============

One-call helpers (return ``bytes``)::

    simple_text_pdf(lines, *, font="Helvetica", size=12, page_size=(612, 792),
                    x=72, top=72, leading=None, op="Tj")
    simple_multi_page_pdf(pages_of_lines, **same_options)
    simple_image_pdf(width=8, height=8, *, kind="rgb", smask=False,
                     placement=(72, 600, 144, 144), color=None, text_lines=())

Declarative description (frozen dataclasses, tuples only, hashable-friendly and
Hypothesis friendly)::

    DocSpec(pages=(PageSpec, ...), forms=(FormSpec, ...), info={...}, xmp=...,
            encryption=EncryptionSpec | None, prior_revisions=(DocSpec, ...),
            cjk_ligatures=("fi",), cjk_tounicode=True)
    PageSpec(media_box=(x0, y0, x1, y1), crop_box=None, rotate=None,
             contents_as_array=False, annotations=(...), <content fields>)
    FormSpec(name, bbox, matrix, inherit_resources, <content fields>)

    <content fields> (shared by PageSpec and FormSpec, drawn in this order):
        runs            tuple[TextRun, ...]
        images          tuple[ImageSpec, ...]          (image XObjects)
        inline_images   tuple[InlineImageSpec, ...]    (BI ... ID ... EI)
        form_placements tuple[FormPlacement, ...]      (Do of a Form XObject)
        raw_content     bytes                           (appended verbatim)

    build_pdf(doc: DocSpec) -> bytes

Text (``TextRun``)
------------------
``TextRun(text, x=72, y=700, font="Helvetica", size=12, op="Tj", tc=0, tw=0,
tz=100, ts=0, tr=0, leading=None, kerning=None, tj_pieces=None)``

* ``font`` is one of the standard 14 names (``STANDARD_14_FONTS``, WinAnsi
  encoding except Symbol / ZapfDingbats) or ``CJK_FONT`` for the document's
  Type0 / Identity-H font.
* ``op`` is ``"Tj"``, ``"TJ"``, ``"'"`` or ``'"'``.  ``tc``/``tw``/``tz``/
  ``ts``/``tr`` are the Tc / Tw / Tz / Ts / Tr state values; ``Tz`` is a
  percentage; they are only emitted when they differ from the PDF default
  (for ``"`` the word / character spacing are carried by the operator).
* ``TJ``: ``tj_pieces`` (strings and numbers, numbers are kerning
  adjustments in thousandths of text space units) wins; otherwise ``kerning``
  (a number) splits ``text`` into one piece per character with that
  adjustment between consecutive characters; otherwise a single piece.
* ``'`` and ``"`` move to the next line before showing, so the run is placed
  such that the *shown* text still lands at (``x``, ``y``).
* ``TextRun.shown_text`` is the text a correct extractor returns.

CJK font
--------
Every string shown with ``font=CJK_FONT`` is encoded with two-byte CIDs
allocated deterministically per document (first appearance order, starting
at 1).  The font is a non-embedded ``CIDFontType2`` with ``/DW 1000`` and a
self-made ToUnicode CMap (``build_tounicode_cmap``).  ``DocSpec.cjk_ligatures``
lists multi-character strings (e.g. ``"fi"`` or ``"ffi"``) that are encoded
as *one* glyph whose ToUnicode entry maps to several characters; matching is
greedy, longest first.  ``cjk_tounicode=False`` omits the CMap so extractors
fall back to ``(cid:N)``.  ``cjk_cid_map(doc)`` returns ``{cid: str}``.

Forms
-----
``DocSpec.forms`` holds the form definitions; ``FormPlacement(form, matrix)``
places the form named ``form`` (a placement may appear in a page or in
another form, any number of times, on any pages).  Each form is a single
shared indirect object.  Nesting cycles raise ``ValueError``.  With
``inherit_resources=True`` the form has no ``/Resources`` and relies on the
resources of whatever places it (those are merged in automatically).

Images
------
``ImageSpec(kind, width, height, placements, color, fill, smask, key, pixels,
decode_inverted, palette)`` with ``kind`` in ``IMAGE_KINDS``:
``"rgb"`` / ``"gray"`` (FlateDecode, 8 bit), ``"dct"`` / ``"dct_gray"``
(DCTDecode JPEG), ``"indexed"`` (Indexed over DeviceRGB, 8 bit),
``"mask"`` (1 bit ``/ImageMask``, painted with ``fill``).  ``smask=True`` adds
a FlateDecode gray ``/SMask`` (not for ``"mask"``).  ``placements`` entries are
``(x, y, w, h)`` boxes or full six-number CTMs; several entries draw the same
XObject several times.  Images with equal non-empty ``key`` share one XObject
document wide.  ``pixels`` overrides the generated raw samples (uncompressed,
already in the kind's layout; for ``dct`` it is the JPEG file).
``InlineImageSpec(width, height, color_space="RGB"|"G", placement, color,
filter="ahx"|"flate"|"raw")`` emits ``BI/ID/EI`` inline images.
``raw_samples(kind, w, h, ...)`` and ``jpeg_bytes(...)`` are exported.

Page geometry
-------------
``media_box`` may have a non-zero origin, ``crop_box`` is optional, ``rotate``
is ``None`` (absent) or any multiple of 90.  Content coordinates are plain
user space, so tests position text relative to ``media_box`` themselves
(``PageSpec.origin`` returns the lower-left corner; ``size`` the width/height).

Annotations and forms
---------------------
``FreeTextAnnot(contents, rect, font_size=12, appearance=True)`` and
``WidgetField(name, value, rect, appearance=True)`` go in
``PageSpec.annotations``.  Widgets are merged field/widget dictionaries
listed in ``/AcroForm /Fields`` and carry ``/V`` (and a ``/N`` appearance
stream when asked).  ``DocSpec.info`` (keys without slash, e.g. ``"Title"``)
fills the Info dictionary; ``DocSpec.xmp`` (``str``/``bytes``; see
``make_xmp``) the ``/Metadata`` stream.

Revisions
---------
``DocSpec.prior_revisions`` are earlier states of the same document, oldest
first; the document itself is the newest.  The first state is written normally
and each later one is appended as a real incremental update (pypdf
``incremental=True``), so the old content streams (and their text) stay in the
file bytes.  Every state must have the same page count.  Not combinable with
``encryption``.

Encryption
----------
``EncryptionSpec(user_password="", owner_password="owner",
algorithm="AES-128")``: an empty ``user_password`` gives an owner-password-only
file that opens without a password; a non-empty one requires the user
password.  Helpers: ``OWNER_ONLY`` and ``USER_REQUIRED`` presets.

Hypothesis
----------
``text_runs``, ``image_specs``, ``page_specs``, ``doc_specs`` (composite
strategies, keyword-configurable) plus ``latin_text`` and ``cjk_text``.
Documents they draw always build successfully with ``build_pdf``.
"""

from __future__ import annotations

import io
import string
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from hypothesis import strategies as st
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    ByteStringObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NullObject,
    NumberObject,
    PdfObject,
    RectangleObject,
    StreamObject,
    TextStringObject,
)

__all__ = [
    "CJK_FONT",
    "STANDARD_14_FONTS",
    "IMAGE_KINDS",
    "TEXT_OPS",
    "TextRun",
    "ImageSpec",
    "InlineImageSpec",
    "FormPlacement",
    "FormSpec",
    "FreeTextAnnot",
    "WidgetField",
    "PageSpec",
    "EncryptionSpec",
    "OWNER_ONLY",
    "USER_REQUIRED",
    "DocSpec",
    "build_pdf",
    "build_tounicode_cmap",
    "cjk_cid_map",
    "raw_samples",
    "jpeg_bytes",
    "make_xmp",
    "simple_text_pdf",
    "simple_multi_page_pdf",
    "simple_image_pdf",
    "latin_text",
    "cjk_text",
    "text_runs",
    "image_specs",
    "page_specs",
    "doc_specs",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentinel ``TextRun.font`` value selecting the Type0 / Identity-H font.
CJK_FONT = "CJK"

STANDARD_14_FONTS: Tuple[str, ...] = (
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-Oblique",
    "Helvetica-BoldOblique",
    "Times-Roman",
    "Times-Bold",
    "Times-Italic",
    "Times-BoldItalic",
    "Courier",
    "Courier-Bold",
    "Courier-Oblique",
    "Courier-BoldOblique",
    "Symbol",
    "ZapfDingbats",
)
_SYMBOLIC_FONTS = ("Symbol", "ZapfDingbats")

TEXT_OPS: Tuple[str, ...] = ("Tj", "TJ", "'", '"')
IMAGE_KINDS: Tuple[str, ...] = ("rgb", "gray", "dct", "dct_gray", "indexed", "mask")

Rect = Tuple[float, float, float, float]
Placement = Tuple[float, ...]
Color = Tuple[float, float, float]

_DEFAULT_PALETTE: Tuple[Tuple[int, int, int], ...] = (
    (0, 0, 0),
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
)

# ---------------------------------------------------------------------------
# Description data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextRun:
    """One ``BT ... ET`` block showing ``text`` (see module docstring)."""

    text: str
    x: float = 72
    y: float = 700
    font: str = "Helvetica"
    size: float = 12
    op: str = "Tj"
    tc: float = 0
    tw: float = 0
    tz: float = 100
    ts: float = 0
    tr: int = 0
    leading: Optional[float] = None
    kerning: Optional[float] = None
    tj_pieces: Optional[Tuple[Union[str, float], ...]] = None

    def __post_init__(self) -> None:
        if self.op not in TEXT_OPS:
            raise ValueError("unsupported text operator {!r}".format(self.op))
        if self.font != CJK_FONT and self.font not in STANDARD_14_FONTS:
            raise ValueError("unknown font {!r}".format(self.font))
        if self.tj_pieces is not None and self.op != "TJ":
            raise ValueError("tj_pieces needs op='TJ'")

    def pieces(self) -> Tuple[Union[str, float], ...]:
        """The TJ array content (strings and adjustments)."""
        if self.tj_pieces is not None:
            return self.tj_pieces
        if self.kerning is not None and len(self.text) > 1:
            out: List[Union[str, float]] = []
            for index, char in enumerate(self.text):
                if index:
                    out.append(self.kerning)
                out.append(char)
            return tuple(out)
        return (self.text,)

    @property
    def shown_text(self) -> str:
        """The characters this run shows, in order."""
        if self.op == "TJ":
            return "".join(p for p in self.pieces() if isinstance(p, str))
        return self.text


@dataclass(frozen=True)
class ImageSpec:
    """An image XObject placed one or more times (see module docstring)."""

    kind: str = "rgb"
    width: int = 8
    height: int = 8
    placements: Tuple[Placement, ...] = ((72.0, 600.0, 144.0, 144.0),)
    color: Optional[Tuple[int, int, int]] = None
    fill: Color = (0.0, 0.0, 0.0)
    smask: bool = False
    key: Optional[str] = None
    pixels: Optional[bytes] = None
    decode_inverted: bool = False
    palette: Tuple[Tuple[int, int, int], ...] = _DEFAULT_PALETTE

    def __post_init__(self) -> None:
        if self.kind not in IMAGE_KINDS:
            raise ValueError("unknown image kind {!r}".format(self.kind))
        if self.width < 1 or self.height < 1:
            raise ValueError("image size must be positive")
        if self.smask and self.kind == "mask":
            raise ValueError("an image mask cannot have an SMask")
        if not 1 <= len(self.palette) <= 256:
            raise ValueError("palette must have 1..256 entries")
        for placement in self.placements:
            if len(placement) not in (4, 6):
                raise ValueError("placement must have 4 or 6 numbers")


@dataclass(frozen=True)
class InlineImageSpec:
    """An inline image (``BI`` / ``ID`` / ``EI``)."""

    width: int = 4
    height: int = 4
    color_space: str = "RGB"
    placement: Placement = (72.0, 500.0, 72.0, 72.0)
    color: Optional[Tuple[int, int, int]] = None
    filter: str = "ahx"

    def __post_init__(self) -> None:
        if self.color_space not in ("RGB", "G"):
            raise ValueError("inline color_space must be 'RGB' or 'G'")
        if self.filter not in ("ahx", "flate", "raw"):
            raise ValueError("inline filter must be ahx, flate or raw")
        if len(self.placement) not in (4, 6):
            raise ValueError("placement must have 4 or 6 numbers")


@dataclass(frozen=True)
class FormPlacement:
    """``q <matrix> cm /FmN Do Q`` for the form named ``form``."""

    form: str
    matrix: Tuple[float, float, float, float, float, float] = (1, 0, 0, 1, 0, 0)


@dataclass(frozen=True, kw_only=True)
class _ContentSpec:
    runs: Tuple[TextRun, ...] = ()
    images: Tuple[ImageSpec, ...] = ()
    inline_images: Tuple[InlineImageSpec, ...] = ()
    form_placements: Tuple[FormPlacement, ...] = ()
    raw_content: bytes = b""


@dataclass(frozen=True, kw_only=True)
class FormSpec(_ContentSpec):
    """A Form XObject definition."""

    name: str
    bbox: Rect = (0, 0, 612, 792)
    matrix: Tuple[float, float, float, float, float, float] = (1, 0, 0, 1, 0, 0)
    inherit_resources: bool = False


@dataclass(frozen=True)
class FreeTextAnnot:
    contents: str
    rect: Rect = (72, 400, 300, 430)
    font_size: float = 12
    appearance: bool = True


@dataclass(frozen=True)
class WidgetField:
    name: str
    value: str
    rect: Rect = (72, 300, 300, 330)
    appearance: bool = True


Annotation = Union[FreeTextAnnot, WidgetField]


@dataclass(frozen=True, kw_only=True)
class PageSpec(_ContentSpec):
    """One page."""

    media_box: Rect = (0, 0, 612, 792)
    crop_box: Optional[Rect] = None
    rotate: Optional[int] = None
    contents_as_array: bool = False
    annotations: Tuple[Annotation, ...] = ()

    def __post_init__(self) -> None:
        x0, y0, x1, y1 = self.media_box
        if not (x1 > x0 and y1 > y0):
            raise ValueError("media_box must have positive size")
        if self.rotate is not None and self.rotate % 90 != 0:
            raise ValueError("rotate must be a multiple of 90")

    @property
    def origin(self) -> Tuple[float, float]:
        return (self.media_box[0], self.media_box[1])

    @property
    def size(self) -> Tuple[float, float]:
        return (
            self.media_box[2] - self.media_box[0],
            self.media_box[3] - self.media_box[1],
        )


@dataclass(frozen=True)
class EncryptionSpec:
    user_password: str = ""
    owner_password: str = "owner"
    algorithm: str = "AES-128"


#: Opens without a password; the owner password only governs permissions.
OWNER_ONLY = EncryptionSpec(user_password="", owner_password="owner-secret")
#: Needs the user password ("user-secret") to open.
USER_REQUIRED = EncryptionSpec(
    user_password="user-secret", owner_password="owner-secret"
)


@dataclass(frozen=True)
class DocSpec:
    pages: Tuple[PageSpec, ...] = (PageSpec(),)
    forms: Tuple[FormSpec, ...] = ()
    info: Mapping[str, str] = field(default_factory=dict)
    xmp: Optional[Union[str, bytes]] = None
    encryption: Optional[EncryptionSpec] = None
    prior_revisions: Tuple["DocSpec", ...] = ()
    cjk_ligatures: Tuple[str, ...] = ()
    cjk_tounicode: bool = True

    def __post_init__(self) -> None:
        if not self.pages:
            raise ValueError("a document needs at least one page")
        for ligature in self.cjk_ligatures:
            if len(ligature) < 2:
                raise ValueError("a ligature needs at least two characters")


# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------


def _num(value: float) -> bytes:
    number = float(value)
    if number == int(number):
        return str(int(number)).encode("ascii")
    text = "{:.5f}".format(number).rstrip("0").rstrip(".")
    return text.encode("ascii")


def _nums(values: Sequence[float]) -> bytes:
    return b" ".join(_num(v) for v in values)


def _literal(data: bytes) -> bytes:
    out = bytearray(b"(")
    for byte in data:
        if byte in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([byte])
        elif byte == 0x0D:
            out += b"\\r"
        elif byte == 0x0A:
            out += b"\\n"
        else:
            out.append(byte)
    out += b")"
    return bytes(out)


def _to_pdf(value: Any) -> Any:
    """Convert plain Python data to pypdf generic objects."""
    if isinstance(value, PdfObject):
        return value
    if value is None:
        return NullObject()
    if isinstance(value, bool):
        return BooleanObject(value)
    if isinstance(value, int):
        return NumberObject(value)
    if isinstance(value, float):
        return FloatObject(value)
    if isinstance(value, str):
        return TextStringObject(value)
    if isinstance(value, bytes):
        return ByteStringObject(value)
    if isinstance(value, Mapping):
        return DictionaryObject({NameObject(k): _to_pdf(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return ArrayObject([_to_pdf(v) for v in value])
    raise TypeError("cannot convert {!r} to a PDF object".format(type(value)))


def _stream(entries: Mapping[str, Any], data: bytes) -> StreamObject:
    """A stream.  If ``/Filter`` is among ``entries`` ``data`` is taken to be
    already encoded."""
    payload: Dict[Any, Any] = {NameObject(k): _to_pdf(v) for k, v in entries.items()}
    payload["__streamdata__"] = data
    return StreamObject.initialize_from_dictionary(payload)


def _encode_standard(text: str, font: str) -> bytes:
    codec = "latin-1" if font in _SYMBOLIC_FONTS else "cp1252"
    try:
        return text.encode(codec)
    except UnicodeEncodeError as error:
        raise ValueError(
            "text {!r} is not encodable for font {}".format(text, font)
        ) from error


# ---------------------------------------------------------------------------
# Raster sample generation
# ---------------------------------------------------------------------------


def raw_samples(
    kind: str,
    width: int,
    height: int,
    color: Optional[Tuple[int, int, int]] = None,
    palette_size: int = 4,
) -> bytes:
    """Uncompressed samples for ``kind`` (``rgb``, ``gray``, ``indexed``,
    ``mask``).  ``color`` gives a solid fill (a single ``int`` channel for
    gray is taken from ``color[0]``); otherwise a deterministic gradient.
    For ``mask`` the result is 1 bit per pixel, rows padded to bytes, with a
    checkerboard (or all ``1`` bits for ``color`` given)."""
    if kind == "rgb":
        out = bytearray()
        for y in range(height):
            for x in range(width):
                if color is not None:
                    out += bytes(color)
                else:
                    out += bytes(
                        (
                            x * 255 // max(width - 1, 1),
                            y * 255 // max(height - 1, 1),
                            (x + y) * 255 // max(width + height - 2, 1),
                        )
                    )
        return bytes(out)
    if kind == "gray":
        out = bytearray()
        for y in range(height):
            for x in range(width):
                out.append(
                    color[0]
                    if color is not None
                    else (x + y) * 255 // max(width + height - 2, 1)
                )
        return bytes(out)
    if kind == "indexed":
        return bytes(
            (x + y) % palette_size if color is None else min(color[0], palette_size - 1)
            for y in range(height)
            for x in range(width)
        )
    if kind == "mask":
        row_bytes = (width + 7) // 8
        out = bytearray()
        for y in range(height):
            row = 0
            bits = [
                1 if (color is not None or (x + y) % 2 == 0) else 0
                for x in range(width)
            ]
            for byte_index in range(row_bytes):
                row = 0
                for bit in range(8):
                    x = byte_index * 8 + bit
                    row = (row << 1) | (bits[x] if x < width else 0)
                out.append(row)
        return bytes(out)
    raise ValueError("no raw samples for kind {!r}".format(kind))


def jpeg_bytes(
    width: int,
    height: int,
    gray: bool = False,
    color: Optional[Tuple[int, int, int]] = None,
) -> bytes:
    """A baseline JPEG file made with Pillow."""
    from PIL import Image

    if gray:
        image = Image.new("L", (width, height), color[0] if color else 128)
        if color is None:
            image.putdata([(x + y) * 255 // max(width + height - 2, 1)
                           for y in range(height) for x in range(width)])
    else:
        image = Image.new("RGB", (width, height), color or (0, 0, 0))
        if color is None:
            image.putdata(
                [
                    (
                        x * 255 // max(width - 1, 1),
                        y * 255 // max(height - 1, 1),
                        (x + y) * 255 // max(width + height - 2, 1),
                    )
                    for y in range(height)
                    for x in range(width)
                ]
            )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# CJK font support
# ---------------------------------------------------------------------------


def _strings_of(content: _ContentSpec) -> List[str]:
    return [
        run.shown_text if run.op == "TJ" else run.text
        for run in content.runs
        if run.font == CJK_FONT
    ]


def _all_cjk_strings(doc: DocSpec) -> List[str]:
    strings: List[str] = []
    for page in doc.pages:
        strings.extend(_strings_of(page))
    for form in doc.forms:
        strings.extend(_strings_of(form))
    return strings


def _tokenize(text: str, ligatures: Sequence[str]) -> List[str]:
    ordered = sorted(set(ligatures), key=len, reverse=True)
    tokens: List[str] = []
    index = 0
    while index < len(text):
        for ligature in ordered:
            if text.startswith(ligature, index):
                tokens.append(ligature)
                index += len(ligature)
                break
        else:
            tokens.append(text[index])
            index += 1
    return tokens


def _cid_table(doc: DocSpec) -> Dict[str, int]:
    table: Dict[str, int] = {}
    for text in _all_cjk_strings(doc):
        for token in _tokenize(text, doc.cjk_ligatures):
            if token not in table:
                table[token] = len(table) + 1
    return table


def cjk_cid_map(doc: DocSpec) -> Dict[int, str]:
    """``{cid: unicode string}`` exactly as written to the ToUnicode CMap."""
    return {cid: token for token, cid in _cid_table(doc).items()}


def build_tounicode_cmap(mapping: Mapping[int, str]) -> bytes:
    """A ToUnicode CMap (two-byte codes) with ``bfchar`` entries.  A value of
    several characters yields one glyph -> many characters."""
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    items = sorted(mapping.items())
    for start in range(0, len(items), 100):
        chunk = items[start : start + 100]
        lines.append("{} beginbfchar".format(len(chunk)))
        for cid, text in chunk:
            lines.append(
                "<{:04X}> <{}>".format(cid, text.encode("utf-16-be").hex().upper())
            )
        lines.append("endbfchar")
    lines += [
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


# ---------------------------------------------------------------------------
# XMP
# ---------------------------------------------------------------------------


def make_xmp(**fields: str) -> str:
    """A small XMP packet; keyword names become ``dc:`` style elements of
    ``rdf:Description`` (e.g. ``make_xmp(title="x")`` -> ``<dc:title>``)."""
    from xml.sax.saxutils import escape

    body = "".join(
        "<dc:{0}><rdf:Alt><rdf:li xml:lang=\"x-default\">{1}</rdf:li></rdf:Alt>"
        "</dc:{0}>".format(name, escape(value))
        for name, value in fields.items()
    )
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        + body
        + "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class _Res:
    """Resource names used by one content owner (page or form)."""

    def __init__(self) -> None:
        self.fonts: Dict[str, Any] = {}
        self.xobjs: Dict[str, Any] = {}

    def to_pdf(self) -> DictionaryObject:
        resources = DictionaryObject()
        if self.fonts:
            resources[NameObject("/Font")] = DictionaryObject(
                {NameObject("/" + k): v for k, v in self.fonts.items()}
            )
        if self.xobjs:
            resources[NameObject("/XObject")] = DictionaryObject(
                {NameObject("/" + k): v for k, v in self.xobjs.items()}
            )
        return resources


@dataclass
class _FormInfo:
    ref: Any
    resname: str
    inherit: bool
    res: _Res


class _Builder:
    def __init__(self, writer: PdfWriter, doc: DocSpec) -> None:
        self.writer = writer
        self.doc = doc
        self.cid_table = _cid_table(doc)
        self._forms_by_name: Dict[str, FormSpec] = {}
        for form in doc.forms:
            if form.name in self._forms_by_name:
                raise ValueError("duplicate form name {!r}".format(form.name))
            self._forms_by_name[form.name] = form
        self._form_index = {form.name: i + 1 for i, form in enumerate(doc.forms)}
        self._forms: Dict[str, _FormInfo] = {}
        self._forms_building: List[str] = []
        self._font_refs: Dict[str, Any] = {}
        self._font_names: Dict[str, str] = {}
        self._image_refs: Dict[str, Tuple[str, Any]] = {}
        self._image_counter = 0
        self._widget_refs: List[Any] = []
        self._helv: Optional[Any] = None

    # -- object plumbing ---------------------------------------------------

    def add(self, obj: Any) -> Any:
        return self.writer._add_object(obj)

    # -- fonts ---------------------------------------------------------------

    def font_resource(self, font: str, res: _Res) -> str:
        if font not in self._font_names:
            self._font_names[font] = "F{}".format(len(self._font_names) + 1)
        name = self._font_names[font]
        if font not in self._font_refs:
            self._font_refs[font] = self.add(
                self._cjk_font() if font == CJK_FONT else self._standard_font(font)
            )
        res.fonts[name] = self._font_refs[font]
        return name

    @staticmethod
    def _standard_font(font: str) -> DictionaryObject:
        entries: Dict[str, Any] = {
            "/Type": NameObject("/Font"),
            "/Subtype": NameObject("/Type1"),
            "/BaseFont": NameObject("/" + font),
        }
        if font not in _SYMBOLIC_FONTS:
            entries["/Encoding"] = NameObject("/WinAnsiEncoding")
        return DictionaryObject({NameObject(k): v for k, v in entries.items()})

    def _cjk_font(self) -> DictionaryObject:
        descriptor = self.add(
            _to_pdf(
                {
                    "/Type": NameObject("/FontDescriptor"),
                    "/FontName": NameObject("/FixtureCJK"),
                    "/Flags": 4,
                    "/FontBBox": [0, -200, 1000, 900],
                    "/ItalicAngle": 0,
                    "/Ascent": 880,
                    "/Descent": -120,
                    "/CapHeight": 700,
                    "/StemV": 80,
                }
            )
        )
        descendant = self.add(
            _to_pdf(
                {
                    "/Type": NameObject("/Font"),
                    "/Subtype": NameObject("/CIDFontType2"),
                    "/BaseFont": NameObject("/FixtureCJK"),
                    "/CIDSystemInfo": {
                        "/Registry": "Adobe",
                        "/Ordering": "Identity",
                        "/Supplement": 0,
                    },
                    "/FontDescriptor": descriptor,
                    "/DW": 1000,
                    "/CIDToGIDMap": NameObject("/Identity"),
                }
            )
        )
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type0"),
                NameObject("/BaseFont"): NameObject("/FixtureCJK"),
                NameObject("/Encoding"): NameObject("/Identity-H"),
                NameObject("/DescendantFonts"): ArrayObject([descendant]),
            }
        )
        if self.doc.cjk_tounicode:
            cmap = build_tounicode_cmap(
                {cid: token for token, cid in self.cid_table.items()}
            )
            font[NameObject("/ToUnicode")] = self.add(_stream({}, cmap))
        return font

    def _cjk_hex(self, text: str) -> bytes:
        tokens = _tokenize(text, self.doc.cjk_ligatures)
        return b"<" + b"".join(
            "{:04X}".format(self.cid_table[t]).encode("ascii") for t in tokens
        ) + b">"

    # -- text ----------------------------------------------------------------

    def _string(self, run: TextRun, text: str) -> bytes:
        if run.font == CJK_FONT:
            return self._cjk_hex(text)
        return _literal(_encode_standard(text, run.font))

    def text_chunk(self, run: TextRun, font_name: str) -> bytes:
        leading = run.leading if run.leading is not None else run.size * 1.2
        parts: List[bytes] = [b"BT", b"/%s %s Tf" % (font_name.encode(), _num(run.size))]
        if run.op != '"':
            if run.tc:
                parts.append(_num(run.tc) + b" Tc")
            if run.tw:
                parts.append(_num(run.tw) + b" Tw")
        if run.tz != 100:
            parts.append(_num(run.tz) + b" Tz")
        if run.ts:
            parts.append(_num(run.ts) + b" Ts")
        if run.tr:
            parts.append(b"%d Tr" % run.tr)
        y = run.y
        if run.op in ("'", '"'):
            parts.append(_num(leading) + b" TL")
            y = run.y + leading
        parts.append(_nums((1, 0, 0, 1, run.x, y)) + b" Tm")
        if run.op == "Tj":
            parts.append(self._string(run, run.text) + b" Tj")
        elif run.op == "TJ":
            items = [
                self._string(run, p) if isinstance(p, str) else _num(p)
                for p in run.pieces()
            ]
            parts.append(b"[" + b" ".join(items) + b"] TJ")
        elif run.op == "'":
            parts.append(self._string(run, run.text) + b" '")
        else:
            parts.append(
                _num(run.tw) + b" " + _num(run.tc) + b" "
                + self._string(run, run.text) + b' "'
            )
        parts.append(b"ET")
        return b"\n".join(parts)

    # -- images ----------------------------------------------------------------

    def _image_xobject(self, spec: ImageSpec) -> StreamObject:
        w, h = spec.width, spec.height
        entries: Dict[str, Any] = {
            "/Type": NameObject("/XObject"),
            "/Subtype": NameObject("/Image"),
            "/Width": w,
            "/Height": h,
        }
        if spec.kind in ("rgb", "gray", "indexed"):
            samples = spec.pixels
            if samples is None:
                samples = raw_samples(
                    spec.kind, w, h, spec.color, palette_size=len(spec.palette)
                )
            entries["/BitsPerComponent"] = 8
            entries["/Filter"] = NameObject("/FlateDecode")
            if spec.kind == "rgb":
                entries["/ColorSpace"] = NameObject("/DeviceRGB")
            elif spec.kind == "gray":
                entries["/ColorSpace"] = NameObject("/DeviceGray")
            else:
                palette = b"".join(bytes(c) for c in spec.palette)
                entries["/ColorSpace"] = [
                    NameObject("/Indexed"),
                    NameObject("/DeviceRGB"),
                    len(spec.palette) - 1,
                    ByteStringObject(palette),
                ]
            data = zlib.compress(samples)
        elif spec.kind in ("dct", "dct_gray"):
            gray = spec.kind == "dct_gray"
            data = spec.pixels or jpeg_bytes(w, h, gray=gray, color=spec.color)
            entries["/BitsPerComponent"] = 8
            entries["/ColorSpace"] = NameObject(
                "/DeviceGray" if gray else "/DeviceRGB"
            )
            entries["/Filter"] = NameObject("/DCTDecode")
        else:  # mask
            samples = spec.pixels or raw_samples("mask", w, h, spec.color)
            entries["/ImageMask"] = True
            entries["/BitsPerComponent"] = 1
            entries["/Filter"] = NameObject("/FlateDecode")
            if spec.decode_inverted:
                entries["/Decode"] = [1, 0]
            data = zlib.compress(samples)
        if spec.smask:
            alpha = bytes(
                (x * 255 // max(w - 1, 1)) for _ in range(h) for x in range(w)
            )
            entries["/SMask"] = self.add(
                _stream(
                    {
                        "/Type": NameObject("/XObject"),
                        "/Subtype": NameObject("/Image"),
                        "/Width": w,
                        "/Height": h,
                        "/ColorSpace": NameObject("/DeviceGray"),
                        "/BitsPerComponent": 8,
                        "/Filter": NameObject("/FlateDecode"),
                    },
                    zlib.compress(alpha),
                )
            )
        return _stream(entries, data)

    def image_resource(self, spec: ImageSpec, res: _Res) -> str:
        if spec.key is not None and spec.key in self._image_refs:
            name, ref = self._image_refs[spec.key]
        else:
            self._image_counter += 1
            name = "Im{}".format(self._image_counter)
            ref = self.add(self._image_xobject(spec))
            if spec.key is not None:
                self._image_refs[spec.key] = (name, ref)
        res.xobjs[name] = ref
        return name

    @staticmethod
    def _ctm(placement: Placement) -> bytes:
        if len(placement) == 4:
            x, y, w, h = placement
            return _nums((w, 0, 0, h, x, y))
        return _nums(placement)

    def image_chunks(self, spec: ImageSpec, name: str) -> List[bytes]:
        chunks = []
        for placement in spec.placements:
            fill = b""
            if spec.kind == "mask":
                fill = _nums(spec.fill) + b" rg\n"
            chunks.append(
                b"q\n" + fill + self._ctm(placement) + b" cm\n/%s Do\nQ" % name.encode()
            )
        return chunks

    def inline_chunk(self, spec: InlineImageSpec) -> bytes:
        w, h = spec.width, spec.height
        kind = "rgb" if spec.color_space == "RGB" else "gray"
        samples = raw_samples(kind, w, h, spec.color)
        head = b"BI /W %d /H %d /CS /%s /BPC 8" % (
            w,
            h,
            spec.color_space.encode(),
        )
        if spec.filter == "ahx":
            body = b" /F /AHx ID\n" + samples.hex().encode("ascii") + b">"
        elif spec.filter == "flate":
            body = b" /F /Fl ID\n" + zlib.compress(samples)
        else:
            body = b" ID\n" + samples
        return (
            b"q\n" + self._ctm(spec.placement) + b" cm\n" + head + body + b"\nEI\nQ"
        )

    # -- forms -----------------------------------------------------------------

    def form(self, name: str) -> _FormInfo:
        if name in self._forms:
            return self._forms[name]
        if name not in self._forms_by_name:
            raise ValueError("unknown form {!r}".format(name))
        if name in self._forms_building:
            raise ValueError("form nesting cycle through {!r}".format(name))
        self._forms_building.append(name)
        try:
            spec = self._forms_by_name[name]
            res = _Res()
            chunks = self.content_chunks(spec, res)
            entries: Dict[str, Any] = {
                "/Type": NameObject("/XObject"),
                "/Subtype": NameObject("/Form"),
                "/BBox": RectangleObject(list(spec.bbox)),
            }
            if tuple(spec.matrix) != (1, 0, 0, 1, 0, 0):
                entries["/Matrix"] = [float(v) for v in spec.matrix]
            if not spec.inherit_resources:
                entries["/Resources"] = res.to_pdf()
            stream = _stream(entries, b"\n".join(chunks))
            info = _FormInfo(
                ref=self.add(stream),
                resname="Fm{}".format(self._form_index[name]),
                inherit=spec.inherit_resources,
                res=res,
            )
            self._forms[name] = info
            return info
        finally:
            self._forms_building.pop()

    # -- content -----------------------------------------------------------------

    def content_chunks(self, spec: _ContentSpec, res: _Res) -> List[bytes]:
        chunks: List[bytes] = []
        for run in spec.runs:
            chunks.append(self.text_chunk(run, self.font_resource(run.font, res)))
        for image in spec.images:
            chunks.extend(self.image_chunks(image, self.image_resource(image, res)))
        for inline in spec.inline_images:
            chunks.append(self.inline_chunk(inline))
        for placement in spec.form_placements:
            info = self.form(placement.form)
            res.xobjs[info.resname] = info.ref
            if info.inherit:
                res.fonts.update(info.res.fonts)
                res.xobjs.update(info.res.xobjs)
            chunks.append(
                b"q\n"
                + _nums(placement.matrix)
                + b" cm\n/%s Do\nQ" % info.resname.encode()
            )
        if spec.raw_content:
            chunks.append(spec.raw_content)
        return chunks

    # -- annotations -------------------------------------------------------------

    def _helvetica(self) -> Any:
        if self._helv is None:
            self._helv = self.add(self._standard_font("Helvetica"))
        return self._helv

    def _appearance(self, rect: Rect, text: str, size: float) -> Any:
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        body = (
            b"BT /Helv %s Tf 0 g 2 %s Td %s Tj ET"
            % (_num(size), _num(max(h - size - 2, 0)), _literal(_encode_standard(text, "Helvetica")))
        )
        return self.add(
            _stream(
                {
                    "/Type": NameObject("/XObject"),
                    "/Subtype": NameObject("/Form"),
                    "/BBox": RectangleObject([0, 0, w, h]),
                    "/Resources": {"/Font": {"/Helv": self._helvetica()}},
                },
                body,
            )
        )

    def annotation(self, annot: Annotation, page_ref: Any) -> Any:
        if isinstance(annot, FreeTextAnnot):
            entries: Dict[str, Any] = {
                "/Type": NameObject("/Annot"),
                "/Subtype": NameObject("/FreeText"),
                "/Rect": RectangleObject(list(annot.rect)),
                "/Contents": annot.contents,
                "/DA": "/Helv {} Tf 0 g".format(_num(annot.font_size).decode()),
                "/F": 4,
                "/P": page_ref,
            }
            if annot.appearance:
                entries["/AP"] = {
                    "/N": self._appearance(annot.rect, annot.contents, annot.font_size)
                }
            return self.add(_to_pdf(entries))
        entries = {
            "/Type": NameObject("/Annot"),
            "/Subtype": NameObject("/Widget"),
            "/FT": NameObject("/Tx"),
            "/T": annot.name,
            "/V": annot.value,
            "/Rect": RectangleObject(list(annot.rect)),
            "/DA": "/Helv 12 Tf 0 g",
            "/F": 4,
            "/P": page_ref,
        }
        if annot.appearance:
            entries["/AP"] = {"/N": self._appearance(annot.rect, annot.value, 12)}
        ref = self.add(_to_pdf(entries))
        self._widget_refs.append(ref)
        return ref

    # -- pages ---------------------------------------------------------------------

    def render_page(self, page: Any, spec: PageSpec) -> None:
        res = _Res()
        chunks = self.content_chunks(spec, res)
        page[NameObject("/MediaBox")] = RectangleObject(list(spec.media_box))
        self._set_or_delete(
            page,
            "/CropBox",
            RectangleObject(list(spec.crop_box)) if spec.crop_box else None,
        )
        self._set_or_delete(
            page,
            "/Rotate",
            NumberObject(spec.rotate) if spec.rotate is not None else None,
        )
        if spec.contents_as_array and len(chunks) > 1:
            contents: Any = ArrayObject([self.add(_stream({}, c)) for c in chunks])
        else:
            contents = self.add(_stream({}, b"\n".join(chunks)))
        page[NameObject("/Contents")] = contents
        page[NameObject("/Resources")] = res.to_pdf()
        page_ref = page.indirect_reference
        annots = [self.annotation(a, page_ref) for a in spec.annotations]
        self._set_or_delete(page, "/Annots", ArrayObject(annots) if annots else None)

    @staticmethod
    def _set_or_delete(page: Any, key: str, value: Any) -> None:
        if value is None:
            if key in page:
                del page[key]
        else:
            page[NameObject(key)] = value

    # -- document level --------------------------------------------------------------

    def finish(self) -> None:
        root = self.writer._root_object
        if self._widget_refs:
            root[NameObject("/AcroForm")] = _to_pdf(
                {
                    "/Fields": list(self._widget_refs),
                    "/DA": "/Helv 12 Tf 0 g",
                    "/DR": {"/Font": {"/Helv": self._helvetica()}},
                    "/NeedAppearances": False,
                }
            )
        elif "/AcroForm" in root:
            del root["/AcroForm"]

        info = self.writer._info
        if info is not None:
            info = info.get_object()
            for key in [k for k in info.keys() if k != "/Producer"]:
                del info[key]
        if self.doc.info:
            self.writer.add_metadata(
                {"/" + k.lstrip("/"): v for k, v in self.doc.info.items()}
            )

        if self.doc.xmp is not None:
            data = (
                self.doc.xmp.encode("utf-8")
                if isinstance(self.doc.xmp, str)
                else self.doc.xmp
            )
            root[NameObject("/Metadata")] = self.add(
                _stream(
                    {
                        "/Type": NameObject("/Metadata"),
                        "/Subtype": NameObject("/XML"),
                    },
                    data,
                )
            )
        elif "/Metadata" in root:
            del root["/Metadata"]


def _write(writer: PdfWriter) -> bytes:
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def build_pdf(doc: DocSpec) -> bytes:
    """Build the PDF described by ``doc`` (see module docstring)."""
    states = (*doc.prior_revisions, doc)
    if doc.encryption is not None and len(states) > 1:
        raise ValueError("encryption cannot be combined with prior_revisions")
    page_count = len(doc.pages)
    if any(len(s.pages) != page_count for s in states):
        raise ValueError("all revisions must have the same page count")
    for state in states:
        if state.prior_revisions and state is not doc:
            raise ValueError("prior_revisions must not be nested")

    writer = PdfWriter()
    builder = _Builder(writer, states[0])
    for page_spec in states[0].pages:
        width, height = page_spec.size
        page = writer.add_blank_page(width=width, height=height)
        builder.render_page(page, page_spec)
    builder.finish()
    if doc.encryption is not None:
        enc = doc.encryption
        writer.encrypt(
            user_password=enc.user_password,
            owner_password=enc.owner_password,
            algorithm=enc.algorithm,
        )
    data = _write(writer)

    for state in states[1:]:
        writer = PdfWriter(io.BytesIO(data), incremental=True)
        builder = _Builder(writer, state)
        for index, page_spec in enumerate(state.pages):
            builder.render_page(writer.pages[index], page_spec)
        builder.finish()
        data = _write(writer)
    return data


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def _line_runs(
    lines: Sequence[str],
    *,
    font: str,
    size: float,
    page_height: float,
    x: float,
    top: float,
    leading: Optional[float],
    op: str,
    origin: Tuple[float, float] = (0, 0),
) -> Tuple[TextRun, ...]:
    step = leading if leading is not None else size * 1.5
    return tuple(
        TextRun(
            text=line,
            x=origin[0] + x,
            y=origin[1] + page_height - top - index * step,
            font=font,
            size=size,
            op=op,
        )
        for index, line in enumerate(lines)
    )


def simple_text_pdf(
    lines: Sequence[str],
    *,
    font: str = "Helvetica",
    size: float = 12,
    page_size: Tuple[float, float] = (612, 792),
    x: float = 72,
    top: float = 72,
    leading: Optional[float] = None,
    op: str = "Tj",
) -> bytes:
    """A one-page document, one text run per line, top-down."""
    return simple_multi_page_pdf(
        [lines], font=font, size=size, page_size=page_size, x=x, top=top,
        leading=leading, op=op,
    )


def simple_multi_page_pdf(
    pages_of_lines: Sequence[Sequence[str]],
    *,
    font: str = "Helvetica",
    size: float = 12,
    page_size: Tuple[float, float] = (612, 792),
    x: float = 72,
    top: float = 72,
    leading: Optional[float] = None,
    op: str = "Tj",
) -> bytes:
    """A document with one page per entry of ``pages_of_lines`` (an empty
    entry gives a blank page)."""
    pages = tuple(
        PageSpec(
            media_box=(0, 0, page_size[0], page_size[1]),
            runs=_line_runs(
                lines, font=font, size=size, page_height=page_size[1], x=x,
                top=top, leading=leading, op=op,
            ),
        )
        for lines in pages_of_lines
    )
    return build_pdf(DocSpec(pages=pages))


def simple_image_pdf(
    width: int = 8,
    height: int = 8,
    *,
    kind: str = "rgb",
    smask: bool = False,
    placement: Placement = (72.0, 600.0, 144.0, 144.0),
    color: Optional[Tuple[int, int, int]] = None,
    text_lines: Sequence[str] = (),
) -> bytes:
    """A one-page document holding one image (plus optional text lines)."""
    page = PageSpec(
        runs=_line_runs(
            text_lines, font="Helvetica", size=12, page_height=792, x=72,
            top=72, leading=None, op="Tj",
        ),
        images=(
            ImageSpec(
                kind=kind, width=width, height=height, placements=(placement,),
                color=color, smask=smask,
            ),
        ),
    )
    return build_pdf(DocSpec(pages=(page,)))


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_LATIN_ALPHABET = string.ascii_letters + string.digits + " .,-:;@#"


def latin_text(min_size: int = 1, max_size: int = 24) -> st.SearchStrategy[str]:
    """Printable WinAnsi-safe text without leading / trailing blanks."""
    return (
        st.text(alphabet=_LATIN_ALPHABET, min_size=min_size, max_size=max_size)
        .map(str.strip)
        .filter(lambda s: len(s) >= max(min_size, 1))
    )


def cjk_text(min_size: int = 1, max_size: int = 8) -> st.SearchStrategy[str]:
    """Common CJK ideographs (U+4E00..U+9FA5)."""
    return st.text(
        alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FA5),
        min_size=min_size,
        max_size=max_size,
    )


@st.composite
def text_runs(
    draw: Callable[..., Any],
    *,
    bounds: Rect = (0, 0, 612, 792),
    fonts: Sequence[str] = ("Helvetica", "Times-Roman", "Courier"),
    ops: Sequence[str] = TEXT_OPS,
    spacing: bool = True,
    modes: Sequence[int] = (0,),
    cjk: bool = False,
) -> TextRun:
    """A ``TextRun`` placed inside ``bounds`` (x0, y0, x1, y1).  Set ``cjk`` to
    draw CJK text with ``CJK_FONT``.  ``modes`` lists allowed Tr values."""
    x0, y0, x1, y1 = bounds
    use_cjk = cjk
    font = CJK_FONT if use_cjk else draw(st.sampled_from(list(fonts)))
    text = draw(cjk_text() if use_cjk else latin_text())
    size = draw(st.integers(8, 24))
    x = draw(st.integers(int(x0) + 10, max(int(x0) + 10, int(x1) // 2)))
    y = draw(st.integers(int(y0) + 40, max(int(y0) + 40, int(y1) - 40)))
    op = draw(st.sampled_from(list(ops)))
    tc = tw = ts = 0
    tz = 100
    if spacing:
        tc = draw(st.sampled_from([0, 0, 0.5, 1, 2]))
        tw = draw(st.sampled_from([0, 0, 1, 3]))
        tz = draw(st.sampled_from([100, 100, 80, 120]))
        ts = draw(st.sampled_from([0, 0, 2, -2]))
    tr = draw(st.sampled_from(list(modes)))
    kerning = None
    if op == "TJ":
        kerning = draw(st.sampled_from([None, -20, 50, -120]))
    return TextRun(
        text=text, x=x, y=y, font=font, size=size, op=op, tc=tc, tw=tw, tz=tz,
        ts=ts, tr=tr, kerning=kerning,
    )


@st.composite
def image_specs(
    draw: Callable[..., Any],
    *,
    bounds: Rect = (0, 0, 612, 792),
    kinds: Sequence[str] = IMAGE_KINDS,
) -> ImageSpec:
    """An ``ImageSpec`` with one placement inside ``bounds``."""
    x0, y0, x1, y1 = bounds
    kind = draw(st.sampled_from(list(kinds)))
    width = draw(st.integers(1, 12))
    height = draw(st.integers(1, 12))
    w = draw(st.integers(20, 120))
    h = draw(st.integers(20, 120))
    x = draw(st.integers(int(x0), max(int(x0), int(x1) - w)))
    y = draw(st.integers(int(y0), max(int(y0), int(y1) - h)))
    smask = False if kind == "mask" else draw(st.booleans())
    return ImageSpec(
        kind=kind, width=width, height=height, placements=((x, y, w, h),),
        smask=smask,
    )


@st.composite
def page_specs(
    draw: Callable[..., Any],
    *,
    max_runs: int = 4,
    max_images: int = 1,
    cjk: bool = False,
    geometry: bool = True,
    form_names: Sequence[str] = (),
    **run_options: Any,
) -> PageSpec:
    """A ``PageSpec``.  With ``geometry`` the media box origin may be non-zero
    and crop box / rotation may be set; ``form_names`` lists forms (defined by
    the caller in the same ``DocSpec``) that may be placed."""
    if geometry:
        x0 = draw(st.sampled_from([0, 0, 25, -50]))
        y0 = draw(st.sampled_from([0, 0, 40, -30]))
        width = draw(st.sampled_from([300, 612]))
        height = draw(st.sampled_from([400, 792]))
    else:
        x0 = y0 = 0
        width, height = 612, 792
    media: Rect = (x0, y0, x0 + width, y0 + height)
    crop = None
    rotate = None
    if geometry:
        if draw(st.booleans()):
            crop = (x0 + 10, y0 + 10, x0 + width - 10, y0 + height - 10)
        rotate = draw(st.sampled_from([None, 0, 90, 180, 270]))
    runs = draw(st.lists(text_runs(bounds=media, **run_options), max_size=max_runs))
    if cjk:
        runs += draw(
            st.lists(text_runs(bounds=media, cjk=True, **run_options), max_size=2)
        )
    images = draw(st.lists(image_specs(bounds=media), max_size=max_images))
    placements: Tuple[FormPlacement, ...] = ()
    if form_names:
        placements = tuple(
            draw(
                st.lists(
                    st.builds(
                        FormPlacement,
                        form=st.sampled_from(list(form_names)),
                        matrix=st.sampled_from(
                            [(1, 0, 0, 1, 0, 0), (1, 0, 0, 1, 50, 80)]
                        ),
                    ),
                    max_size=2,
                )
            )
        )
    return PageSpec(
        media_box=media, crop_box=crop, rotate=rotate, runs=tuple(runs),
        images=tuple(images), form_placements=placements,
    )


@st.composite
def doc_specs(
    draw: Callable[..., Any],
    *,
    max_pages: int = 3,
    cjk: bool = False,
    with_forms: bool = True,
    **page_options: Any,
) -> DocSpec:
    """A ``DocSpec`` of 1..``max_pages`` pages; may define one or two Form
    XObjects (the second nested in the first) placed on its pages."""
    forms: Tuple[FormSpec, ...] = ()
    names: Tuple[str, ...] = ()
    if with_forms and draw(st.booleans()):
        inner = FormSpec(
            name="inner", bbox=(0, 0, 400, 400),
            runs=tuple(draw(st.lists(text_runs(bounds=(0, 0, 400, 400)), max_size=2))),
        )
        outer = FormSpec(
            name="outer", bbox=(0, 0, 600, 700),
            runs=tuple(draw(st.lists(text_runs(bounds=(0, 0, 400, 400)), max_size=2))),
            form_placements=(FormPlacement("inner", (1, 0, 0, 1, 20, 20)),),
        )
        forms = (outer, inner)
        names = ("outer", "inner")
    pages = tuple(
        draw(st.lists(page_specs(cjk=cjk, form_names=names, **page_options),
                      min_size=1, max_size=max_pages))
    )
    return DocSpec(pages=pages, forms=forms)
