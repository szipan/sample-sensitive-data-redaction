"""Page geometry and coordinate conversion for PDF pages (pure functions).

Two coordinate systems are involved:

* User space: the PDF default user space (origin bottom-left, y up), in which
  content-stream geometry and annotation rectangles are expressed.
* Page_Coordinates: origin at the top-left corner of the *visible* page as a
  viewer shows it (after applying ``/Rotate``), x to the right, y downward,
  in points. A rectangle is ``(left, top, right, bottom)``.

The visible rectangle of a page is the intersection of its CropBox and
MediaBox; when the CropBox is missing, invalid, or does not overlap the
MediaBox, the MediaBox is used. ``/Rotate`` is normalized with the same rules
PDFium uses, so rasterization and text geometry agree on page orientation.

This module deliberately imports no PDF library.
"""
import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple, Union

from shared.models import BoundingBox

# (left, top, right, bottom) in Page_Coordinates.
Rect = Tuple[float, float, float, float]

# Two rectangles count as intersecting only when they overlap by more than this
# many points along both axes. It absorbs floating-point noise (around 1e-13)
# that otherwise makes neighbouring glyphs sharing an edge look overlapping.
INTERSECT_EPS_PT = 1e-6

# Page size used when a page box is absent or degenerate (US Letter).
_DEFAULT_MEDIA_BOX = (0.0, 0.0, 612.0, 792.0)


@dataclass(frozen=True)
class PageGeometry:
    """Visible rectangle and orientation of one page."""

    page_index: int
    # (x0, y0, x1, y1) in user space; CropBox ∩ MediaBox, normalized so x0<x1, y0<y1.
    visible_box: Tuple[float, float, float, float]
    rotation: int  # 0, 90, 180 or 270
    width: float  # width of the visible rectangle after rotation, in points
    height: float  # height of the visible rectangle after rotation, in points


def normalize_rotation(raw) -> int:
    """Normalize a raw ``/Rotate`` value to 0, 90, 180 or 270 (PDFium rules).

    The integer part is taken, divided by 90 truncating toward zero, reduced
    modulo 4, and negative remainders are shifted into range. Values that are
    missing or not numeric give 0.
    """
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(value):
        return 0
    quarter_turns = int(int(value) / 90)  # truncates toward zero
    quarter_turns %= 4
    if quarter_turns < 0:
        quarter_turns += 4
    return quarter_turns * 90


def _normalize_box(box) -> Optional[Tuple[float, float, float, float]]:
    """Return ``box`` as (x0, y0, x1, y1) with x0<x1 and y0<y1, or None if unusable."""
    if box is None:
        return None
    try:
        values = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        return None
    x0, x1 = sorted((values[0], values[2]))
    y0, y1 = sorted((values[1], values[3]))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    return (x0, y0, x1, y1)


def make_page_geometry(
    page_index: int,
    media_box: Optional[Sequence[float]],
    crop_box: Optional[Sequence[float]],
    rotate,
) -> PageGeometry:
    """Build the geometry of a page from its raw boxes and ``/Rotate`` value."""
    media = _normalize_box(media_box) or _DEFAULT_MEDIA_BOX
    visible = media
    crop = _normalize_box(crop_box)
    if crop is not None:
        x0 = max(media[0], crop[0])
        y0 = max(media[1], crop[1])
        x1 = min(media[2], crop[2])
        y1 = min(media[3], crop[3])
        if x1 - x0 > 0 and y1 - y0 > 0:
            visible = (x0, y0, x1, y1)

    rotation = normalize_rotation(rotate)
    w0 = visible[2] - visible[0]
    h0 = visible[3] - visible[1]
    if rotation in (90, 270):
        width, height = h0, w0
    else:
        width, height = w0, h0
    return PageGeometry(
        page_index=page_index,
        visible_box=visible,
        rotation=rotation,
        width=width,
        height=height,
    )


def user_to_page_point(geom: PageGeometry, x: float, y: float) -> Tuple[float, float]:
    """Convert a user-space point to Page_Coordinates."""
    vx0, vy0, vx1, vy1 = geom.visible_box
    rot = geom.rotation
    if rot == 0:
        return (x - vx0, vy1 - y)
    if rot == 90:
        return (y - vy0, x - vx0)
    if rot == 180:
        return (vx1 - x, y - vy0)
    return (vy1 - y, vx1 - x)  # 270


def page_to_user_point(geom: PageGeometry, X: float, Y: float) -> Tuple[float, float]:
    """Convert a Page_Coordinates point back to user space."""
    vx0, vy0, vx1, vy1 = geom.visible_box
    rot = geom.rotation
    if rot == 0:
        return (X + vx0, vy1 - Y)
    if rot == 90:
        return (Y + vx0, X + vy0)
    if rot == 180:
        return (vx1 - X, Y + vy0)
    return (vx1 - Y, vy1 - X)  # 270


def user_to_page_rect(
    geom: PageGeometry, user_rect: Tuple[float, float, float, float]
) -> Rect:
    """Convert a user-space rectangle (x0, y0, x1, y1) to Page_Coordinates.

    The four corners are converted and their bounding rectangle is returned.
    """
    x0, y0, x1, y1 = user_rect
    points = [
        user_to_page_point(geom, x0, y0),
        user_to_page_point(geom, x1, y0),
        user_to_page_point(geom, x1, y1),
        user_to_page_point(geom, x0, y1),
    ]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def page_to_user_rect(
    geom: PageGeometry, rect: Rect
) -> Tuple[float, float, float, float]:
    """Convert a Page_Coordinates rectangle to a user-space (x0, y0, x1, y1)."""
    left, top, right, bottom = rect
    points = [
        page_to_user_point(geom, left, top),
        page_to_user_point(geom, right, top),
        page_to_user_point(geom, right, bottom),
        page_to_user_point(geom, left, bottom),
    ]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def page_rect_as_bounding_box(geom: PageGeometry) -> BoundingBox:
    """The whole visible page as a bounding box anchored at (0, 0)."""
    return BoundingBox(left=0.0, top=0.0, width=geom.width, height=geom.height)


def rects_intersect(a: Rect, b: Rect, eps: float = INTERSECT_EPS_PT) -> bool:
    """True when the overlap of ``a`` and ``b`` exceeds ``eps`` on both axes.

    Rectangles that only touch along an edge, or overlap by floating-point
    noise, do not intersect.
    """
    overlap_x = min(a[2], b[2]) - max(a[0], b[0])
    overlap_y = min(a[3], b[3]) - max(a[1], b[1])
    return overlap_x > eps and overlap_y > eps


def normalized_box_to_page_rect(
    geom_or_size: Union[PageGeometry, Tuple[float, float]], box: BoundingBox
) -> Rect:
    """Scale a box with [0, 1] fractions (left/top/width/height) to points.

    ``geom_or_size`` is either a ``PageGeometry`` or a ``(width, height)`` pair
    in points of the page the box refers to.
    """
    if isinstance(geom_or_size, PageGeometry):
        page_w, page_h = geom_or_size.width, geom_or_size.height
    else:
        page_w, page_h = geom_or_size
    left = box.left * page_w
    top = box.top * page_h
    return (left, top, left + box.width * page_w, top + box.height * page_h)
