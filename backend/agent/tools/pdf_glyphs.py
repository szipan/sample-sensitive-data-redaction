"""Shared glyph traversal for PDF extraction and redaction.

The extractor builds Text_Unit text from the glyph stream of a page, and the
redactor maps character offsets back to rectangles and to content-stream bytes
from the very same traversal. Both sides MUST use this module so that the
character offset mapping holds by construction.

This module defines the document-opening step and the data model that the
traversal fills in:

* ``open_pdf`` opens the bytes with pdfminer.six (content interpretation) and
  pikepdf (content rewriting) and checks that both see the same page tree.
* ``GlyphRecord`` is one rendered glyph with its provenance in the content
  stream (context path, operator ordinal, TJ element index, byte range).
* ``CharPosition`` is one character of the page text, mapped to a glyph.
* ``ImagePlacement`` is one placement of an Image XObject.
* ``PageGlyphs`` is the complete result for a page.

Rectangles are ``(left, top, right, bottom)`` in Page_Coordinates.
"""
import io
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional, Sequence, Set, Tuple

import pikepdf
from pdfminer import settings as pdfminer_settings
from pdfminer.cmapdb import CMap, CMapBase, IdentityCMap, IdentityCMapByte
from pdfminer.layout import LTChar
from pdfminer.pdfdevice import PDFTextDevice
from pdfminer.pdfdocument import PDFDocument, PDFPasswordIncorrect
from pdfminer.pdffont import PDFCIDFont, PDFFont, PDFUnicodeNotDefined
from pdfminer.pdfinterp import (
    LITERAL_FORM,
    LITERAL_IMAGE,
    PDFContentParser,
    PDFInterpreterError,
    PDFPageInterpreter,
    PDFResourceManager,
)
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import (
    PDFException,
    PDFStream,
    dict_value,
    list_value,
    resolve_all,
    stream_value,
)
from pdfminer.psexceptions import PSEOF
from pdfminer.psparser import PSKeyword, keyword_name, literal_name
from pdfminer.utils import (
    MATRIX_IDENTITY,
    apply_matrix_rect,
    mult_matrix,
    translate_matrix,
)

from agent.tools.pdf_geometry import (
    PageGeometry,
    Rect,
    make_page_geometry,
    user_to_page_rect,
)

logger = logging.getLogger(__name__)

# Path from the page content stream to the stream being executed: a tuple of
# (ordinal of the ``Do`` operator in the parent stream, Form XObject object
# number). The page-level content stream has the empty path.
ContextPath = Tuple[Tuple[int, int], ...]


class PdfOpenError(Exception):
    """The bytes are empty or cannot be parsed as a PDF."""


class PdfPasswordError(PdfOpenError):
    """The document requires a non-empty user password."""


class PageReadError(Exception):
    """The document opened, but the characters of one page cannot be read."""

    def __init__(self, page_index: int, message: str = ""):
        self.page_index = page_index
        super().__init__(message or f"cannot read page {page_index}")


@dataclass
class PdfHandle:
    """An opened document seen through both libraries."""

    pdfminer_doc: PDFDocument
    pdfminer_pages: List[PDFPage]
    pikepdf_doc: "pikepdf.Pdf"
    page_count: int
    # Resource manager shared by all pages so fonts are parsed only once.
    resource_manager: Optional[PDFResourceManager] = None


@dataclass
class GlyphRecord:
    """One rendered glyph and where it came from in the content stream."""

    text: str  # Unicode text of the glyph; may be empty or several characters
    raw_rect: Rect  # unclipped glyph box in Page_Coordinates
    rect: Optional[Rect]  # raw_rect clipped to the visible page; None if empty
    context: ContextPath
    op_ordinal: int  # ordinal of the text-showing operator in its stream
    elem_index: int  # index of the string element within a TJ array (0 for Tj)
    byte_start: int  # byte range of the code inside the string operand
    byte_end: int
    cid: int
    # Text state needed to compute the displacement of the glyph.
    font_size: float = 0.0  # Tfs
    char_space: float = 0.0  # Tc
    word_space: float = 0.0  # Tw
    horizontal_scale: float = 1.0  # Th (1.0 == 100%)
    advance: float = 0.0  # glyph width in text space units (w0, or w1 if vertical)
    single_byte_space: bool = False  # single-byte code 32, so Tw applies
    vertical: bool = False
    line_id: int = -1  # assigned during line grouping
    # Geometry used for line grouping, in default user space.
    origin: Tuple[float, float] = (0.0, 0.0)  # glyph origin
    direction: Tuple[float, float] = (1.0, 0.0)  # unit vector along the baseline
    cross_extent: float = 0.0  # box size perpendicular to ``direction``


@dataclass
class CharPosition:
    """One character of the page text."""

    char: str
    rect: Optional[Rect]  # None when the glyph has no usable rectangle
    line_id: int
    glyph: GlyphRecord  # glyph this character came from


@dataclass
class ImagePlacement:
    """One placement of an Image XObject in drawing order."""

    xref: int  # object number of the image XObject
    rect: Rect  # bounding box of the unit square under the CTM, Page_Coordinates
    context: ContextPath
    op_ordinal: int


@dataclass
class PageGlyphs:
    """Result of traversing one page."""

    geometry: PageGeometry
    glyphs: List[GlyphRecord] = field(default_factory=list)
    chars: List[CharPosition] = field(default_factory=list)
    text: str = ""  # "".join(c.char for c in chars)
    image_placements: List[ImagePlacement] = field(default_factory=list)


def open_pdf(file_bytes: bytes) -> PdfHandle:
    """Open ``file_bytes`` with pdfminer.six and pikepdf.

    Documents protected only by an owner password open with the empty user
    password. Raises ``PdfPasswordError`` when a non-empty user password is
    required, and ``PdfOpenError`` for empty input, parse failures in either
    library, or disagreeing page counts.
    """
    if not file_bytes:
        raise PdfOpenError("empty input")

    try:
        parser = PDFParser(io.BytesIO(file_bytes))
        doc = PDFDocument(parser, password="")
        pages = list(PDFPage.create_pages(doc))
    except PDFPasswordIncorrect as exc:
        raise PdfPasswordError("password required") from exc
    except Exception as exc:
        raise PdfOpenError(f"cannot open PDF: {exc}") from exc

    try:
        pdf = pikepdf.open(io.BytesIO(file_bytes), password="")
    except pikepdf.PasswordError as exc:
        raise PdfPasswordError("password required") from exc
    except Exception as exc:
        raise PdfOpenError(f"cannot open PDF: {exc}") from exc

    if len(pages) != len(pdf.pages):
        pdf.close()
        raise PdfOpenError(
            f"page count mismatch: {len(pages)} vs {len(pdf.pages)}"
        )

    return PdfHandle(
        pdfminer_doc=doc,
        pdfminer_pages=pages,
        pikepdf_doc=pdf,
        page_count=len(pages),
    )


# ---------------------------------------------------------------------------
# Content-stream traversal
# ---------------------------------------------------------------------------

# Text of a glyph whose code has no Unicode mapping: exactly one character, so
# that the page text never gains characters the document does not contain.
_REPLACEMENT_CHAR = "\ufffd"


def _split_codes(font: PDFFont, data: bytes) -> Iterator[Tuple[int, int, int]]:
    """Split a string operand into ``(cid, byte_start, byte_end)`` triples.

    The split follows the font's decoding exactly: bytes that do not complete
    a code are skipped the same way ``font.decode`` skips them, so the cids
    produced here equal ``list(font.decode(data))``.
    """
    cmap = font.cmap if isinstance(font, PDFCIDFont) else None
    if cmap is None:
        # Simple fonts (including Type 3): one byte per code.
        for i, cid in enumerate(bytearray(data)):
            yield cid, i, i + 1
    elif isinstance(cmap, IdentityCMapByte):
        for i, cid in enumerate(bytearray(data)):
            yield cid, i, i + 1
    elif isinstance(cmap, IdentityCMap):
        for i in range(len(data) // 2):
            yield (data[2 * i] << 8) | data[2 * i + 1], 2 * i, 2 * i + 2
    elif isinstance(cmap, CMap):
        root = cmap.code2cid
        node = root
        start = 0
        for i, byte in enumerate(bytearray(data)):
            if byte in node:
                nxt = node[byte]
                if isinstance(nxt, int):
                    yield nxt, start, i + 1
                    node = root
                    start = i + 1
                else:
                    node = nxt
            else:
                node = root
                start = i + 1
    else:
        raise PDFInterpreterError(f"unsupported CMap type {type(cmap).__name__}")


def _ensure_decodable(stream: PDFStream) -> None:
    """Raise ``PageReadError`` when a content stream cannot be decoded.

    In its default lenient mode pdfminer turns an undecodable Flate stream
    into empty data, so the content would silently look blank. When a stream
    decodes to nothing although it has bytes, decode it again strictly: a
    genuinely empty stream passes, a corrupt one raises.
    """
    error = getattr(stream, "_glyph_decode_error", None)
    if error is None:
        error = ""
        raw = stream.rawdata
        if raw:
            stream.get_data()
            if not stream.data:
                error = _strict_decode_error(stream, raw)
        stream._glyph_decode_error = error
    if error:
        raise PageReadError(-1, f"undecodable content stream: {error}")


def _strict_decode_error(stream: PDFStream, raw: bytes) -> str:
    """Error text from decoding ``raw`` strictly, or "" when it decodes."""
    probe = PDFStream(stream.attrs, raw, stream.decipher)
    probe.objid, probe.genno = stream.objid, stream.genno
    previous = pdfminer_settings.STRICT
    pdfminer_settings.STRICT = True
    try:
        probe.get_data()
    except PDFException as exc:
        return str(exc) or "decode failure"
    finally:
        pdfminer_settings.STRICT = previous
    return ""


class _GlyphDevice(PDFTextDevice):
    """Records every glyph and image placement the interpreter renders."""

    def __init__(self, rsrcmgr: PDFResourceManager, geometry: PageGeometry):
        super().__init__(rsrcmgr)
        self.geometry = geometry
        self.glyphs: List[GlyphRecord] = []
        self.images: List[ImagePlacement] = []
        self._image_keys: Set[Tuple[ContextPath, int]] = set()
        # Position of the operator being executed.
        self.context: ContextPath = ()
        self.op_ordinal = -1
        self._textstate: Any = None

    # -- images ------------------------------------------------------------

    def add_image(self, xref: int, ctm: Tuple[float, ...]) -> None:
        key = (self.context, self.op_ordinal)
        if key in self._image_keys:
            return
        self._image_keys.add(key)
        user_rect = apply_matrix_rect(ctm, (0, 0, 1, 1))
        self.images.append(
            ImagePlacement(
                xref=xref,
                rect=user_to_page_rect(self.geometry, user_rect),
                context=self.context,
                op_ordinal=self.op_ordinal,
            )
        )

    # -- text --------------------------------------------------------------

    def render_string(self, textstate, seq, ncs, graphicstate) -> None:
        self._textstate = textstate
        super().render_string(textstate, seq, ncs, graphicstate)

    def render_string_horizontal(
        self, seq, matrix, pos, font, fontsize, scaling, charspace,
        wordspace, rise, dxscale, ncs, graphicstate,
    ):
        return self._render_string(
            False, seq, matrix, pos, font, fontsize, scaling, charspace,
            wordspace, rise, dxscale, ncs, graphicstate,
        )

    def render_string_vertical(
        self, seq, matrix, pos, font, fontsize, scaling, charspace,
        wordspace, rise, dxscale, ncs, graphicstate,
    ):
        return self._render_string(
            True, seq, matrix, pos, font, fontsize, scaling, charspace,
            wordspace, rise, dxscale, ncs, graphicstate,
        )

    def _render_string(
        self, vertical, seq, matrix, pos, font, fontsize, scaling, charspace,
        wordspace, rise, dxscale, ncs, graphicstate,
    ):
        """Walk a TJ array element by element, recording one glyph per code."""
        # Tc is part of every glyph's displacement (PDF 32000-1, 9.4.4), so it
        # is added after each glyph. pdfminer.six instead adds it before every
        # glyph but the first of the operator, also after a leading TJ number,
        # which shifts glyphs that follow a leading number (as left by a
        # deleted glyph) and drops the trailing Tc of the operator.
        x, y = pos
        for elem_index, obj in enumerate(seq):
            if isinstance(obj, (int, float)):
                if vertical:
                    y -= obj * dxscale
                else:
                    x -= obj * dxscale
            elif isinstance(obj, bytes):
                for cid, b0, b1 in _split_codes(font, obj):
                    adv = self._record_glyph(
                        translate_matrix(matrix, (x, y)),
                        font, fontsize, scaling, rise, cid, ncs, graphicstate,
                        elem_index, b0, b1, obj, vertical,
                    )
                    if vertical:
                        y += adv + charspace
                    else:
                        x += adv + charspace
                    if cid == 32 and wordspace:
                        if vertical:
                            y += wordspace
                        else:
                            x += wordspace
            else:
                logger.warning("ignoring invalid text array element %r", type(obj))
        return (x, y)

    def _record_glyph(
        self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate,
        elem_index, byte_start, byte_end, string, vertical,
    ) -> float:
        try:
            text = font.to_unichr(cid)
            if not isinstance(text, str):
                text = _REPLACEMENT_CHAR
        except (PDFUnicodeNotDefined, KeyError):
            text = _REPLACEMENT_CHAR
        textwidth = font.char_width(cid)
        textdisp = font.char_disp(cid)
        item = LTChar(
            matrix, font, fontsize, scaling, rise, text, textwidth, textdisp,
            ncs, graphicstate,
        )
        raw_rect = user_to_page_rect(self.geometry, item.bbox)
        ts = self._textstate
        direction = _baseline_direction(matrix, vertical)
        box_w = abs(item.bbox[2] - item.bbox[0])
        box_h = abs(item.bbox[3] - item.bbox[1])
        self.glyphs.append(
            GlyphRecord(
                text=text,
                raw_rect=raw_rect,
                rect=_clip_to_page(raw_rect, self.geometry),
                context=self.context,
                op_ordinal=self.op_ordinal,
                elem_index=elem_index,
                byte_start=byte_start,
                byte_end=byte_end,
                cid=cid,
                font_size=float(fontsize),
                char_space=float(ts.charspace),
                word_space=float(ts.wordspace),
                horizontal_scale=float(scaling),
                advance=float(textwidth),
                single_byte_space=(
                    byte_end - byte_start == 1 and string[byte_start] == 32
                ),
                vertical=bool(vertical),
                origin=(float(matrix[4]), float(matrix[5])),
                direction=direction,
                cross_extent=abs(direction[0]) * box_h + abs(direction[1]) * box_w,
            )
        )
        return item.adv


def _baseline_direction(matrix, vertical: bool) -> Tuple[float, float]:
    """Unit vector along which successive glyphs of a line advance."""
    if vertical:
        dx, dy = -float(matrix[2]), -float(matrix[3])
    else:
        dx, dy = float(matrix[0]), float(matrix[1])
    norm = math.hypot(dx, dy)
    if norm == 0:
        return (0.0, -1.0) if vertical else (1.0, 0.0)
    return (dx / norm, dy / norm)


def _clip_to_page(rect: Rect, geometry: PageGeometry) -> Optional[Rect]:
    """Clip ``rect`` to the visible page; None when nothing visible remains."""
    left = max(rect[0], 0.0)
    top = max(rect[1], 0.0)
    right = min(rect[2], geometry.width)
    bottom = min(rect[3], geometry.height)
    if right - left <= 0 or bottom - top <= 0:
        return None
    return (left, top, right, bottom)


class _GlyphInterpreter(PDFPageInterpreter):
    """Runs page content with an identity CTM and numbers every operator.

    The CTM starts as the identity, so everything the device sees is in the
    default user space; ``pdf_geometry`` maps it to Page_Coordinates.
    ``process_page`` is intentionally never used because it would pre-apply
    the page box and rotation.
    """

    def __init__(
        self,
        rsrcmgr: PDFResourceManager,
        device: _GlyphDevice,
        context: ContextPath = (),
    ):
        super().__init__(rsrcmgr, device)
        self.context = context
        self.op_ordinal = -1

    def dup(self) -> "_GlyphInterpreter":
        return _GlyphInterpreter(self.rsrcmgr, self.device, self.context)

    def run_page(self, page: PDFPage) -> None:
        self.init_resources(page.resources)
        self.init_state(MATRIX_IDENTITY)
        self.execute(page.contents)

    def execute(self, streams: Sequence[object]) -> None:
        """Like the base implementation, but counts every keyword operator."""
        valid_streams: List[PDFStream] = []
        self.stream_ids.clear()
        for obj in streams:
            stream = stream_value(obj)
            if stream.objid is None or stream.objid in self.parent_stream_ids:
                continue
            _ensure_decodable(stream)
            valid_streams.append(stream)
            self.stream_ids.add(stream.objid)
        try:
            parser = PDFContentParser(valid_streams)
        except PSEOF:
            return
        self.op_ordinal = -1
        while True:
            try:
                (_, obj) = parser.nextobject()
            except PSEOF:
                break
            if not isinstance(obj, PSKeyword):
                self.push(obj)
                continue
            self.op_ordinal += 1
            self.device.context = self.context
            self.device.op_ordinal = self.op_ordinal
            name = keyword_name(obj)
            method = "do_" + name.replace("*", "_a").replace('"', "_w").replace(
                "'", "_q"
            )
            func = getattr(self, method, None)
            if func is None:
                continue
            nargs = func.__code__.co_argcount - 1
            if nargs:
                args = self.pop(nargs)
                if len(args) == nargs:
                    func(*args)
            else:
                func()

    def do__w(self, aw, ac, s) -> None:
        """The ``"`` operator: set spacing, move to the next line, show text.

        The base implementation leaves out the move to the next line.
        """
        self.do_Tw(aw)
        self.do_Tc(ac)
        self.do__q(s)

    def do_Do(self, xobjid_arg) -> None:
        xobjid = literal_name(xobjid_arg)
        try:
            xobj = stream_value(self.xobjmap[xobjid])
        except KeyError:
            return
        subtype = xobj.get("Subtype")
        if subtype is LITERAL_FORM and "BBox" in xobj:
            matrix = list_value(xobj.get("Matrix", MATRIX_IDENTITY))
            xobjres = xobj.get("Resources")
            resources = dict_value(xobjres) if xobjres else self.resources.copy()
            _ensure_decodable(xobj)
            interpreter = self.subinterp()
            interpreter.context = self.context + (
                (self.op_ordinal, xobj.objid if xobj.objid is not None else -1),
            )
            # A form starts from the graphics state current at ``Do``, which
            # includes the text state (Tc, Tw, Tz, TL, Tf, Tr, Ts); pdfminer's
            # ``render_contents`` would reset it to the defaults.
            interpreter.init_resources(resources)
            interpreter.init_state(mult_matrix(matrix, self.ctm))
            interpreter.textstate = self.textstate.copy()
            interpreter.execute([xobj])
            # The nested interpreter replaced the device's CTM; restore ours.
            self.device.set_ctm(self.ctm)
        elif subtype is LITERAL_IMAGE and "Width" in xobj and "Height" in xobj:
            if xobj.objid is not None:
                self.device.add_image(xobj.objid, self.ctm)


def _page_geometry(page_index: int, page: PDFPage) -> PageGeometry:
    """Geometry of a pdfminer page from its (inherited) boxes and rotation."""
    attrs = page.attrs

    def box(key: str):
        try:
            value = resolve_all(attrs.get(key))
            return list(value) if isinstance(value, (list, tuple)) else None
        except Exception:
            return None

    try:
        rotate = resolve_all(attrs.get("Rotate"))
    except Exception:
        rotate = None
    return make_page_geometry(page_index, box("MediaBox"), box("CropBox"), rotate)


def _traverse_page(
    handle: PdfHandle, page_index: int
) -> Tuple[PageGeometry, List[GlyphRecord], List[ImagePlacement]]:
    """Raw glyph records and image placements of one page, in drawing order.

    Raises ``PageReadError`` when the content cannot be interpreted.
    """
    page = handle.pdfminer_pages[page_index]
    try:
        geometry = _page_geometry(page_index, page)
        if handle.resource_manager is None:
            handle.resource_manager = PDFResourceManager()
        rsrcmgr = handle.resource_manager
        device = _GlyphDevice(rsrcmgr, geometry)
        _GlyphInterpreter(rsrcmgr, device).run_page(page)
    except PageReadError as exc:
        raise PageReadError(page_index, str(exc)) from exc
    except Exception as exc:
        raise PageReadError(page_index, f"cannot read page {page_index}: {exc}") from exc
    return geometry, device.glyphs, device.images


# ---------------------------------------------------------------------------
# Character positions and line grouping
# ---------------------------------------------------------------------------


def _starts_new_line(prev: GlyphRecord, cur: GlyphRecord) -> bool:
    """Whether ``cur`` begins a new line after ``prev`` (drawing order)."""
    if prev.context != cur.context or prev.vertical != cur.vertical:
        return True
    ux, uy = prev.direction
    dx = cur.origin[0] - prev.origin[0]
    dy = cur.origin[1] - prev.origin[1]
    h = prev.cross_extent
    perpendicular = abs(dx * uy - dy * ux)
    along = dx * ux + dy * uy
    return perpendicular > 0.5 * h or along < -0.5 * h


def _assign_lines_and_chars(
    glyphs: List[GlyphRecord],
) -> Tuple[List[CharPosition], str]:
    """Set ``line_id`` on every glyph and expand glyphs into characters."""
    chars: List[CharPosition] = []
    line_id = 0
    prev: Optional[GlyphRecord] = None
    for glyph in glyphs:
        if prev is not None and _starts_new_line(prev, glyph):
            line_id += 1
        glyph.line_id = line_id
        for ch in glyph.text:
            chars.append(
                CharPosition(char=ch, rect=glyph.rect, line_id=line_id, glyph=glyph)
            )
        prev = glyph
    return chars, "".join(c.char for c in chars)


def collect_page_glyphs(handle: PdfHandle, page_index: int) -> PageGlyphs:
    """Traverse one page and return its glyphs, characters and images.

    Raises ``PageReadError`` when the page content cannot be interpreted.
    """
    geometry, glyphs, images = _traverse_page(handle, page_index)
    chars, text = _assign_lines_and_chars(glyphs)
    return PageGlyphs(
        geometry=geometry,
        glyphs=glyphs,
        chars=chars,
        text=text,
        image_placements=images,
    )


def collect_glyphs_from_bytes(file_bytes: bytes) -> List[PageGlyphs]:
    """Open ``file_bytes`` and traverse every page, in page order."""
    handle = open_pdf(file_bytes)
    try:
        return [collect_page_glyphs(handle, i) for i in range(handle.page_count)]
    finally:
        handle.pikepdf_doc.close()
