"""Content-stream rewriting that physically removes glyphs from a PDF.

Glyphs whose boxes meet a redaction region are cut out of the strings of the
text-showing operators (``Tj``, ``TJ``, ``'``, ``"``). Each removed glyph is
replaced by a numeric TJ displacement equal to the advance the glyph had, so
the glyphs that remain keep their exact positions.

The module first plans which glyphs go and which TJ number takes the place of
each one. Plans are grouped by context path (the chain of Form XObjects leading
to the stream that holds the glyph) so that each stream can be rewritten in a
single pass. The rewrite then checks that every planned position agrees with
what the glyph traversal recorded, and refuses to guess when it does not.
"""
import html
import io
import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pikepdf

from agent.tools.pdf_geometry import (
    PageGeometry,
    Rect,
    make_page_geometry,
    page_to_user_rect,
    rects_intersect,
    user_to_page_rect,
)
from agent.tools.pdf_glyphs import (
    ContextPath,
    GlyphRecord,
    PageGlyphs,
    collect_glyphs_from_bytes,
)


class ContentEditError(Exception):
    """A content stream cannot be rewritten safely."""


@dataclass
class GlyphDeletion:
    """One glyph to cut out of a content stream."""

    op_ordinal: int  # ordinal of the text-showing operator in its stream
    elem_index: int  # index of the string element within a TJ array (0 for Tj)
    byte_start: int  # byte range of the code inside the string operand
    byte_end: int
    tj_adjust: float  # TJ number that takes the place of the glyph


def tj_adjust_for(glyph: GlyphRecord) -> float:
    """TJ number whose displacement equals the advance of ``glyph``.

    A glyph advances the text position by
      horizontal: tx = (w0 * Tfs + Tc + Tw') * Th
      vertical:   ty = w1 * Tfs + Tc + Tw'
    where Tw' is the word spacing only for a single-byte code 32. A TJ number
    n moves the position by -n / 1000 * Tfs (* Th when horizontal), hence the
    values below.

    Raises ``ContentEditError`` when the font size or the horizontal scale is
    zero, because no number can then reproduce the displacement.
    """
    tfs = glyph.font_size
    if tfs == 0:
        raise ContentEditError("cannot preserve glyph positions: font size is 0")
    word_space = glyph.word_space if glyph.single_byte_space else 0.0
    if glyph.vertical:
        ty = glyph.advance * tfs + glyph.char_space + word_space
        return -ty * 1000.0 / tfs
    th = glyph.horizontal_scale
    if th == 0:
        raise ContentEditError(
            "cannot preserve glyph positions: horizontal scaling is 0"
        )
    tx = (glyph.advance * tfs + glyph.char_space + word_space) * th
    return -tx * 1000.0 / (tfs * th)


def plan_deletions(
    page_glyphs: PageGlyphs, regions: Sequence[Rect]
) -> Dict[ContextPath, List[GlyphDeletion]]:
    """Plan the removal of every glyph that meets any of ``regions``.

    The decision is made per glyph on its unclipped box, so glyphs that
    produce no character position, invisible text and glyphs drawn twice at
    the same place are all included. Plans are keyed by context path; within a
    path the deletions follow drawing order.
    """
    plan: Dict[ContextPath, List[GlyphDeletion]] = {}
    for glyph in page_glyphs.glyphs:
        if not any(rects_intersect(glyph.raw_rect, region) for region in regions):
            continue
        plan.setdefault(glyph.context, []).append(
            GlyphDeletion(
                op_ordinal=glyph.op_ordinal,
                elem_index=glyph.elem_index,
                byte_start=glyph.byte_start,
                byte_end=glyph.byte_end,
                tj_adjust=tj_adjust_for(glyph),
            )
        )
    return plan


# ---------------------------------------------------------------------------
# Rewriting
# ---------------------------------------------------------------------------

_TEXT_SHOWING_OPERATORS = ("Tj", "TJ", "'", '"')


def _number(value: float) -> Decimal:
    """A TJ number with enough digits to keep positions within 1e-6 units."""
    return Decimal(repr(round(value, 6))).normalize()


def _operator_name(instruction: Any) -> str:
    return str(instruction.operator)


def _cut_string(
    data: bytes, deletions: Sequence[GlyphDeletion]
) -> List[Any]:
    """Split ``data`` around ``deletions`` into kept strings and TJ numbers.

    Each removed range becomes its adjust number; numbers that end up next to
    each other are added together.
    """
    pieces: List[Any] = []
    pending = 0.0
    has_pending = False
    cursor = 0
    for deletion in sorted(deletions, key=lambda d: d.byte_start):
        if deletion.byte_start > cursor:
            if has_pending:
                pieces.append(_number(pending))
                pending, has_pending = 0.0, False
            pieces.append(pikepdf.String(data[cursor:deletion.byte_start]))
        pending += deletion.tj_adjust
        has_pending = True
        cursor = deletion.byte_end
    if cursor < len(data):
        if has_pending:
            pieces.append(_number(pending))
            pending, has_pending = 0.0, False
        pieces.append(pikepdf.String(data[cursor:]))
    if has_pending:
        pieces.append(_number(pending))
    return pieces


def _check_string_alignment(
    data: bytes,
    deletions: Sequence[GlyphDeletion],
    glyphs: Sequence[GlyphRecord],
) -> None:
    """The glyphs recorded for one string must tile it exactly."""
    for deletion in deletions:
        if not 0 <= deletion.byte_start < deletion.byte_end <= len(data):
            raise ContentEditError("glyph byte range lies outside its string")
    ranges = sorted((g.byte_start, g.byte_end) for g in glyphs)
    cursor = 0
    for start, end in ranges:
        if start != cursor or end <= start:
            raise ContentEditError("recorded glyphs do not tile their string")
        cursor = end
    if cursor != len(data):
        raise ContentEditError("recorded glyphs do not cover their string")
    for glyph in glyphs:
        if glyph.single_byte_space and data[glyph.byte_start:glyph.byte_end] != b" ":
            raise ContentEditError("recorded space glyph differs from the stream")


def rewrite_instructions(
    instructions: Sequence[Any],
    deletions: Sequence[GlyphDeletion],
    stream_glyphs: Sequence[GlyphRecord],
) -> List[Any]:
    """Apply ``deletions`` to the parsed instructions of one content stream.

    ``stream_glyphs`` are all glyph records of that stream (not only the
    deleted ones); they are used to check that operator ordinals, operator
    names and string bytes agree with the traversal. Ordinals refer to the
    instructions as given, so the whole stream is handled in one pass and
    inserted instructions cannot shift later ordinals.

    Returns the new instruction list; the input is not modified.
    """
    by_ordinal: Dict[int, List[GlyphDeletion]] = defaultdict(list)
    for deletion in deletions:
        by_ordinal[deletion.op_ordinal].append(deletion)
    glyphs_by_ordinal: Dict[int, List[GlyphRecord]] = defaultdict(list)
    for glyph in stream_glyphs:
        if glyph.op_ordinal in by_ordinal:
            glyphs_by_ordinal[glyph.op_ordinal].append(glyph)
    for ordinal in by_ordinal:
        if not 0 <= ordinal < len(instructions):
            raise ContentEditError("operator ordinal lies outside the stream")

    result: List[Any] = []
    for ordinal, instruction in enumerate(instructions):
        op_deletions = by_ordinal.get(ordinal)
        if op_deletions is None:
            result.append(instruction)
            continue
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            raise ContentEditError("operator ordinal does not hold text")
        name = _operator_name(instruction)
        if name not in _TEXT_SHOWING_OPERATORS:
            raise ContentEditError(
                "operator at ordinal %d is %s, not a text-showing operator"
                % (ordinal, name)
            )
        result.extend(
            _rewrite_operator(
                instruction, name, op_deletions, glyphs_by_ordinal[ordinal]
            )
        )
    return result


def _rewrite_operator(
    instruction: Any,
    name: str,
    deletions: List[GlyphDeletion],
    glyphs: List[GlyphRecord],
) -> List[Any]:
    operands = list(instruction.operands)
    arity = {"Tj": 1, "TJ": 1, "'": 1, '"': 3}[name]
    if len(operands) != arity:
        raise ContentEditError("operator %s has unexpected operands" % name)

    by_elem: Dict[int, List[GlyphDeletion]] = defaultdict(list)
    for deletion in deletions:
        by_elem[deletion.elem_index].append(deletion)
    glyphs_by_elem: Dict[int, List[GlyphRecord]] = defaultdict(list)
    for glyph in glyphs:
        glyphs_by_elem[glyph.elem_index].append(glyph)

    if name == "TJ":
        array = operands[0]
        if not isinstance(array, pikepdf.Array):
            raise ContentEditError("TJ operand is not an array")
        elements = list(array)
    else:
        string = operands[-1]
        if not isinstance(string, pikepdf.String):
            raise ContentEditError("%s operand is not a string" % name)
        elements = [string]

    new_elements: List[Any] = []
    for index, element in enumerate(elements):
        elem_deletions = by_elem.pop(index, None)
        if elem_deletions is None:
            new_elements.append(element)
            continue
        if not isinstance(element, pikepdf.String):
            raise ContentEditError("array element at a deleted glyph is not a string")
        data = bytes(element)
        _check_string_alignment(data, elem_deletions, glyphs_by_elem[index])
        new_elements.extend(_cut_string(data, elem_deletions))
    if by_elem:
        raise ContentEditError("glyph element index lies outside its operator")

    show = pikepdf.ContentStreamInstruction(
        [pikepdf.Array(new_elements)], pikepdf.Operator("TJ")
    )
    if name in ("Tj", "TJ"):
        return [show]
    out: List[Any] = []
    if name == '"':
        out.append(
            pikepdf.ContentStreamInstruction([operands[0]], pikepdf.Operator("Tw"))
        )
        out.append(
            pikepdf.ContentStreamInstruction([operands[1]], pikepdf.Operator("Tc"))
        )
    out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
    out.append(show)
    return out


def _pattern_has_text(pattern: Any) -> bool:
    try:
        instructions = pikepdf.parse_content_stream(pattern)
    except Exception as exc:  # unreadable content cannot be proven free of text
        raise ContentEditError("cannot read a tiling pattern: %s" % exc) from exc
    for instruction in instructions:
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            continue
        if _operator_name(instruction) in _TEXT_SHOWING_OPERATORS:
            return True
    return False


def _resources_use_text_pattern(
    resources: Any, seen: Set[Tuple[int, int]]
) -> bool:
    """Whether resources (or those of nested forms) hold a text tiling pattern."""
    if resources is None or not isinstance(resources, pikepdf.Dictionary):
        return False
    patterns = resources.get("/Pattern")
    if isinstance(patterns, pikepdf.Dictionary):
        for key in patterns.keys():
            pattern = patterns[key]
            if (
                isinstance(pattern, pikepdf.Stream)
                and int(pattern.get("/PatternType", 0)) == 1
                and _pattern_has_text(pattern)
            ):
                return True
    xobjects = resources.get("/XObject")
    if isinstance(xobjects, pikepdf.Dictionary):
        for key in xobjects.keys():
            xobj = xobjects[key]
            if not isinstance(xobj, pikepdf.Stream):
                continue
            if xobj.get("/Subtype") != "/Form":
                continue
            ident = xobj.objgen
            if ident in seen:
                continue
            seen.add(ident)
            if _resources_use_text_pattern(xobj.get("/Resources"), seen):
                return True
    return False


def ensure_no_text_patterns(pdf: "pikepdf.Pdf", page_index: int) -> None:
    """Refuse pages whose tiling patterns draw text.

    Text inside a pattern is not part of the glyph traversal, so it could not
    be removed and would stay under the black boxes.
    """
    page = pdf.pages[page_index]
    if _resources_use_text_pattern(page.resources, set()):
        raise ContentEditError("page uses a tiling pattern that draws text")


def _page_stream_glyphs(
    page_glyphs: PageGlyphs, context: ContextPath
) -> List[GlyphRecord]:
    return [g for g in page_glyphs.glyphs if g.context == context]


def apply_page_plan(
    pdf: "pikepdf.Pdf",
    page_index: int,
    plan: Dict[ContextPath, List[GlyphDeletion]],
    page_glyphs: PageGlyphs,
) -> None:
    """Cut the planned glyphs out of the content of one page.

    Call this for every page that has redaction regions, also when its plan is
    empty, so that pages with text-drawing tiling patterns are refused.

    The page stream and the Form XObjects below it are rewritten in one pass
    that works on the original operator ordinals. A form that holds deletions
    is copied for the one placement concerned: the copy is registered in the
    parent's resources under a fresh name and only that ``Do`` operator is
    pointed at it, so other placements of the form and other pages keep the
    untouched original. The old page content streams are replaced by a single
    new one.
    """
    ensure_no_text_patterns(pdf, page_index)
    page = pdf.pages[page_index]
    plan = {context: dels for context, dels in plan.items() if dels}
    if not plan:
        return
    try:
        instructions = pikepdf.parse_content_stream(page)
    except Exception as exc:
        raise ContentEditError("cannot parse the page content: %s" % exc) from exc

    has_children = any(len(context) > 0 for context in plan)
    resources = _own_page_resources(page) if has_children else None
    rewritten = _rewrite_level(
        pdf, instructions, resources, (), plan, page_glyphs
    )
    page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(rewritten))


def _inherited_resources(page: Any) -> Any:
    """The page's /Resources, looked up through the page tree."""
    node = page.obj
    while node is not None:
        resources = node.get("/Resources")
        if isinstance(resources, pikepdf.Dictionary):
            return resources
        node = node.get("/Parent")
    return None


def _clone_resources(source: Any) -> "pikepdf.Dictionary":
    """A new direct resource dictionary that can take new names safely.

    The entries are shared with ``source`` except /XObject, which is copied so
    that registering a name does not touch the original dictionary.
    """
    clone = pikepdf.Dictionary()
    if isinstance(source, pikepdf.Dictionary):
        for key in source.keys():
            clone[key] = source[key]
    xobjects = clone.get("/XObject")
    fresh = pikepdf.Dictionary()
    if isinstance(xobjects, pikepdf.Dictionary):
        for key in xobjects.keys():
            fresh[key] = xobjects[key]
    clone["/XObject"] = fresh
    return clone


def _own_page_resources(page: Any) -> "pikepdf.Dictionary":
    """Give the page its own direct resources and return them."""
    clone = _clone_resources(_inherited_resources(page))
    page.obj["/Resources"] = clone
    return clone


def _unused_form_name(xobjects: "pikepdf.Dictionary", counter: List[int]) -> str:
    while True:
        name = "/RdxFm%d" % counter[0]
        counter[0] += 1
        if name not in xobjects:
            return name


def _copy_form(pdf: "pikepdf.Pdf", form: Any, parent_resources: Any) -> Any:
    """A new stream with the dictionary and data of ``form``.

    The copy owns a resource dictionary (a clone of the form's own, or of the
    parent's when the form has none and therefore inherits).
    """
    try:
        data = form.read_bytes()
    except Exception as exc:
        raise ContentEditError("cannot decode a Form XObject: %s" % exc) from exc
    copy = pdf.make_stream(data)
    for key in form.keys():
        if key in ("/Length", "/Filter", "/DecodeParms"):
            continue
        copy[key] = form[key]
    own = form.get("/Resources")
    copy["/Resources"] = _clone_resources(
        own if isinstance(own, pikepdf.Dictionary) else parent_resources
    )
    return copy


def _rewrite_level(
    pdf: "pikepdf.Pdf",
    instructions: Sequence[Any],
    resources: Any,
    prefix: ContextPath,
    plan: Dict[ContextPath, List[GlyphDeletion]],
    page_glyphs: PageGlyphs,
    counter: Optional[List[int]] = None,
) -> List[Any]:
    """Rewrite one stream (the page or a form copy) and the forms below it.

    ``resources`` is a resource dictionary owned by this stream, or None when
    no form below it needs a copy. Replacing a ``Do`` operand keeps the number
    of instructions, so the ordinals of the planned deletions stay valid.
    """
    if counter is None:
        counter = [0]
    children: List[Tuple[int, int]] = []
    for context in plan:
        if len(context) > len(prefix) and context[: len(prefix)] == prefix:
            child = context[len(prefix)]
            if child not in children:
                children.append(child)

    result = list(instructions)
    for ordinal, objid in children:
        if resources is None or not 0 <= ordinal < len(result):
            raise ContentEditError("Form XObject placement lies outside its stream")
        instruction = result[ordinal]
        if (
            isinstance(instruction, pikepdf.ContentStreamInlineImage)
            or _operator_name(instruction) != "Do"
            or len(instruction.operands) != 1
            or not isinstance(instruction.operands[0], pikepdf.Name)
        ):
            raise ContentEditError("operator at ordinal %d is not a Do" % ordinal)
        xobjects = resources.get("/XObject")
        old_name = str(instruction.operands[0])
        form = xobjects.get(old_name) if isinstance(xobjects, pikepdf.Dictionary) else None
        if (
            not isinstance(form, pikepdf.Stream)
            or form.get("/Subtype") != "/Form"
            or form.objgen[0] != objid
        ):
            raise ContentEditError("Do operand does not name the recorded form")

        copy = _copy_form(pdf, form, resources)
        try:
            form_instructions = pikepdf.parse_content_stream(form)
        except Exception as exc:
            raise ContentEditError("cannot parse a Form XObject: %s" % exc) from exc
        child_prefix = prefix + ((ordinal, objid),)
        new_instructions = _rewrite_level(
            pdf,
            form_instructions,
            copy["/Resources"],
            child_prefix,
            plan,
            page_glyphs,
            counter,
        )
        copy.write(pikepdf.unparse_content_stream(new_instructions))

        new_name = _unused_form_name(xobjects, counter)
        xobjects[new_name] = copy
        result[ordinal] = pikepdf.ContentStreamInstruction(
            [pikepdf.Name(new_name)], pikepdf.Operator("Do")
        )

    deletions = plan.get(prefix)
    if deletions:
        result = rewrite_instructions(
            result, deletions, _page_stream_glyphs(page_glyphs, prefix)
        )
    return result


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def _inherited_attribute(page: Any, key: str) -> Any:
    """``key`` of the page, looked up through the page tree."""
    node = page.obj
    seen: Set[Tuple[int, int]] = set()
    while isinstance(node, pikepdf.Dictionary):
        if key in node:
            return node[key]
        ident = node.objgen
        if ident in seen:
            break
        seen.add(ident)
        node = node.get("/Parent")
    return None


def _numbers(array: Any) -> Optional[List[float]]:
    if not isinstance(array, pikepdf.Array):
        return None
    try:
        return [float(v) for v in array]
    except (TypeError, ValueError):
        return None


def page_geometry_of(pdf: "pikepdf.Pdf", page_index: int) -> PageGeometry:
    """Geometry of a page from its (inherited) boxes and rotation."""
    page = pdf.pages[page_index]
    rotate = _inherited_attribute(page, "/Rotate")
    try:
        rotate_value = None if rotate is None else float(rotate)
    except (TypeError, ValueError):
        rotate_value = None
    return make_page_geometry(
        page_index,
        _numbers(_inherited_attribute(page, "/MediaBox")),
        _numbers(_inherited_attribute(page, "/CropBox")),
        rotate_value,
    )


def _annotation_page_rect(geom: PageGeometry, annot: Any) -> Optional[Rect]:
    if not isinstance(annot, pikepdf.Dictionary):
        return None
    values = _numbers(annot.get("/Rect"))
    if values is None or len(values) != 4:
        return None
    return user_to_page_rect(geom, (values[0], values[1], values[2], values[3]))


def _remove_from_fields(fields: Any, removed: Set[Tuple[int, int]]) -> List[Any]:
    """``fields`` without the removed widgets and the fields left empty by it."""
    kept: List[Any] = []
    for item in list(fields):
        if item.is_indirect and item.objgen in removed:
            continue
        if isinstance(item, pikepdf.Dictionary):
            kids = item.get("/Kids")
            if isinstance(kids, pikepdf.Array) and len(kids) > 0:
                remaining = _remove_from_fields(kids, removed)
                if not remaining:
                    continue
                if len(remaining) != len(kids):
                    item["/Kids"] = pikepdf.Array(remaining)
        kept.append(item)
    return kept


def remove_intersecting_annotations(
    pdf: "pikepdf.Pdf",
    page_index: int,
    geom: PageGeometry,
    regions: Sequence[Rect],
) -> int:
    """Remove annotations whose rectangle meets a region; return how many.

    Annotation appearances are drawn above the page content and would cover
    the black boxes (and keep their own text). A removed widget is also taken
    out of the form's field tree, with any parent field left without kids.
    Popup annotations of a removed annotation go with it.
    """
    page = pdf.pages[page_index]
    annots = page.obj.get("/Annots")
    if not isinstance(annots, pikepdf.Array) or not regions:
        return 0

    items = list(annots)
    drop = [False] * len(items)
    for index, annot in enumerate(items):
        rect = _annotation_page_rect(geom, annot)
        if rect is not None and any(rects_intersect(rect, r) for r in regions):
            drop[index] = True
    dropped_ids = {
        a.objgen for a, d in zip(items, drop) if d and a.is_indirect
    }
    for index, annot in enumerate(items):
        if drop[index] or not isinstance(annot, pikepdf.Dictionary):
            continue
        if annot.get("/Subtype") == "/Popup":
            parent = annot.get("/Parent")
            if isinstance(parent, pikepdf.Dictionary) and parent.is_indirect:
                if parent.objgen in dropped_ids:
                    drop[index] = True
    count = sum(drop)
    if count == 0:
        return 0

    removed_widgets: Set[Tuple[int, int]] = set()
    for annot, gone in zip(items, drop):
        if (
            gone
            and annot.is_indirect
            and isinstance(annot, pikepdf.Dictionary)
            and annot.get("/Subtype") == "/Widget"
        ):
            removed_widgets.add(annot.objgen)

    page.obj["/Annots"] = pikepdf.Array(
        [a for a, gone in zip(items, drop) if not gone]
    )
    if removed_widgets:
        acroform = pdf.Root.get("/AcroForm")
        fields = acroform.get("/Fields") if isinstance(acroform, pikepdf.Dictionary) else None
        if isinstance(fields, pikepdf.Array):
            acroform["/Fields"] = pikepdf.Array(
                _remove_from_fields(fields, removed_widgets)
            )
    return count


# ---------------------------------------------------------------------------
# Black boxes
# ---------------------------------------------------------------------------

_BOX_STATE_NAME = "/RdxGS"


def _page_own_resources(page: Any) -> "pikepdf.Dictionary":
    """The page's direct /Resources, created from the inherited ones if needed.

    Direct resources already belong to the page (for instance after the page
    plan was applied) and are kept as they are.
    """
    current = page.obj.get("/Resources")
    if isinstance(current, pikepdf.Dictionary) and not current.is_indirect:
        return current
    source = current if isinstance(current, pikepdf.Dictionary) else _inherited_resources(page)
    return _own_resources_from(page, source)


def _own_resources_from(page: Any, source: Any) -> "pikepdf.Dictionary":
    clone = _clone_resources(source)
    page.obj["/Resources"] = clone
    return page.obj["/Resources"]


def _register_box_state(page: Any) -> str:
    """Add the opaque graphics state to the page resources; return its name."""
    resources = _page_own_resources(page)
    states = pikepdf.Dictionary()
    existing = resources.get("/ExtGState")
    if isinstance(existing, pikepdf.Dictionary):
        for key in existing.keys():
            states[key] = existing[key]
    name = _BOX_STATE_NAME
    suffix = 1
    while name in states:
        name = "%s%d" % (_BOX_STATE_NAME, suffix)
        suffix += 1
    states[name] = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/ExtGState"),
            "/ca": 1,
            "/CA": 1,
            "/SMask": pikepdf.Name("/None"),
            "/BM": pikepdf.Name("/Normal"),
        }
    )
    resources["/ExtGState"] = states
    return name


def _unclosed_saves(page: Any) -> int:
    """How many ``q`` of the page content are never closed by a ``Q``."""
    try:
        instructions = pikepdf.parse_content_stream(page)
    except Exception:
        return 0
    depth = 0
    for instruction in instructions:
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            continue
        operator = _operator_name(instruction)
        if operator == "q":
            depth += 1
        elif operator == "Q" and depth > 0:
            depth -= 1
    return depth


def _coordinate(value: float, up: bool) -> str:
    """A number for the stream, rounded outward so the box never shrinks."""
    scaled = value * 10000.0
    rounded = math.ceil(scaled) if up else math.floor(scaled)
    return ("%.4f" % (rounded / 10000.0)).rstrip("0").rstrip(".") or "0"


def draw_black_boxes(
    pdf: "pikepdf.Pdf",
    page_index: int,
    geom: PageGeometry,
    regions: Sequence[Rect],
) -> None:
    """Paint an opaque black rectangle for each region above the page content.

    The existing content is wrapped in ``q … Q`` and a new stream is appended,
    so the boxes are drawn last (above images) with default graphics state
    apart from an added state that forces full opacity, no soft mask and the
    Normal blend mode. Every region gets its own rectangle.
    """
    if not regions:
        return
    page = pdf.pages[page_index]
    state_name = _register_box_state(page)

    rectangles = []
    for region in regions:
        x0, y0, x1, y1 = page_to_user_rect(geom, region)
        left, bottom = _coordinate(x0, False), _coordinate(y0, False)
        right, top = _coordinate(x1, True), _coordinate(y1, True)
        width = _coordinate(float(right) - float(left), True)
        height = _coordinate(float(top) - float(bottom), True)
        rectangles.append("%s %s %s %s re f" % (left, bottom, width, height))
    boxes = "q\n%s gs\n0 0 0 rg\n%s\nQ\n" % (state_name, "\n".join(rectangles))

    closing = b"\n".join([b"Q"] * (1 + _unclosed_saves(page))) + b"\n"
    old = page.obj.get("/Contents")
    if isinstance(old, pikepdf.Array):
        existing = list(old)
    elif isinstance(old, pikepdf.Stream):
        existing = [old]
    else:
        existing = []
    page.obj["/Contents"] = pikepdf.Array(
        [pdf.make_stream(b"q\n")]
        + existing
        + [pdf.make_stream(closing), pdf.make_stream(boxes.encode("ascii"))]
    )


# ---------------------------------------------------------------------------
# Document-level residue
# ---------------------------------------------------------------------------

_APPEARANCE_KEYS = ("/N", "/D", "/R")


def _squeeze(text: str) -> str:
    """``text`` without any whitespace or NUL characters."""
    return "".join(ch for ch in text if not ch.isspace() and ch != "\x00")


def _string_variants(value: "pikepdf.String") -> List[str]:
    """Whitespace-free readings of a PDF string.

    The string is read as PDFDocEncoding / UTF-16BE (as the library decodes
    it) and, because producers are not always careful, also as UTF-8 and as
    raw latin-1 bytes.
    """
    variants: List[str] = []
    try:
        variants.append(str(value))
    except Exception:
        pass
    try:
        raw = bytes(value)
    except Exception:
        raw = b""
    if raw:
        variants.append(raw.decode("utf-8", errors="ignore"))
        variants.append(raw.decode("latin-1"))
        if raw[:2] == b"\xff\xfe":
            variants.append(raw[2:].decode("utf-16-le", errors="ignore"))
        if raw[:3] == b"\xef\xbb\xbf":
            variants.append(raw[3:].decode("utf-8", errors="ignore"))
    return [_squeeze(v) for v in variants]


def _contains_any(readings: Sequence[str], needles: Sequence[str]) -> bool:
    return any(n in r for r in readings for n in needles)


def _stream_bytes(stream: Any) -> List[bytes]:
    """Decoded bytes of a stream, plus the stored bytes when they differ."""
    results: List[bytes] = []
    try:
        results.append(bytes(stream.read_bytes()))
    except Exception:
        pass
    try:
        raw = bytes(stream.read_raw_bytes())
        if not results or raw != results[0]:
            results.append(raw)
    except Exception:
        pass
    return results


def _byte_readings(data: bytes) -> List[str]:
    """Whitespace-free readings of ``data`` in the encodings used by text."""
    readings = [data.decode("latin-1"), data.decode("utf-8", errors="ignore")]
    for codec in ("utf-16-be", "utf-16-le"):
        # Try both byte alignments so an odd leading byte cannot hide text.
        readings.append(data.decode(codec, errors="ignore"))
        readings.append(data[1:].decode(codec, errors="ignore"))
    unescaped = [html.unescape(r) for r in readings if "&" in r]
    return [_squeeze(r) for r in readings + unescaped]


def _stream_has_pii(stream: Any, needles: Sequence[str]) -> bool:
    return any(
        _contains_any(_byte_readings(data), needles)
        for data in _stream_bytes(stream)
    )


def _children(obj: Any) -> List[Tuple[Any, Any]]:
    """(key or index, value) pairs of a dictionary, stream or array."""
    if isinstance(obj, pikepdf.Array):
        return [(i, obj[i]) for i in range(len(obj))]
    if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
        return [(k, obj[k]) for k in list(obj.keys())]
    return []


def _is_container(obj: Any) -> bool:
    return isinstance(obj, (pikepdf.Dictionary, pikepdf.Array, pikepdf.Stream))


def _is_in(item: Any, ids: Set[Tuple[int, int]]) -> bool:
    """Whether ``item`` is an indirect object whose id is in ``ids``."""
    return bool(getattr(item, "is_indirect", False)) and item.objgen in ids


def _replace_items(array: Any, items: Sequence[Any]) -> None:
    """Make ``array`` hold exactly ``items``, in place."""
    for index in range(len(array) - 1, -1, -1):
        del array[index]
    for item in items:
        array.append(item)


def _appearance_streams(annot: Any) -> List[Any]:
    """Appearance streams (normal, rollover, down; every state) of ``annot``."""
    appearance = annot.get("/AP") if isinstance(annot, pikepdf.Dictionary) else None
    if not isinstance(appearance, pikepdf.Dictionary):
        return []
    found: List[Any] = []
    for key in _APPEARANCE_KEYS:
        entry = appearance.get(key)
        if isinstance(entry, pikepdf.Stream):
            found.append(entry)
        elif isinstance(entry, pikepdf.Dictionary):
            for state in list(entry.keys()):
                if isinstance(entry[state], pikepdf.Stream):
                    found.append(entry[state])
    return found


def _appearance_text(stream: Any) -> str:
    """Whitespace-free text drawn by an appearance stream.

    The stream is wrapped as a Form XObject in a one-page document, which is
    then traversed with the shared glyph traversal, so fonts and their
    ToUnicode maps decide what the text is.
    """
    mini = pikepdf.new()
    form = mini.copy_foreign(stream)
    form["/Subtype"] = pikepdf.Name("/Form")
    if "/BBox" not in form:
        form["/BBox"] = pikepdf.Array([-20000, -20000, 20000, 20000])
    mini.add_blank_page(page_size=(612, 792))
    page = mini.pages[0]
    page.obj["/Resources"] = pikepdf.Dictionary(
        {"/XObject": pikepdf.Dictionary({"/RdxAp": form})}
    )
    page.obj["/Contents"] = mini.make_stream(b"/RdxAp Do\n")
    buffer = io.BytesIO()
    mini.save(buffer)
    mini.close()
    pages = collect_glyphs_from_bytes(buffer.getvalue())
    return _squeeze("".join(g.text for p in pages for g in p.glyphs))


def _appearance_has_pii(
    stream: Any, needles: Sequence[str], cache: Dict[Tuple[int, int], bool]
) -> bool:
    key = stream.objgen if stream.is_indirect else None
    if key is not None and key in cache:
        return cache[key]
    # Literal strings of the stream are checked directly as well, which also
    # covers a stream the glyph traversal cannot handle.
    found = _stream_has_pii(stream, needles)
    if not found:
        try:
            found = any(n in _appearance_text(stream) for n in needles)
        except Exception:
            found = False
    if key is not None:
        cache[key] = found
    return found


def _drop_annotations(pdf: "pikepdf.Pdf", page: Any, drop_ids: Set[int]) -> None:
    """Remove the annotations at the given indexes of the page's /Annots."""
    annots = page.obj.get("/Annots")
    if not isinstance(annots, pikepdf.Array) or not drop_ids:
        return
    items = list(annots)
    removed_widgets: Set[Tuple[int, int]] = set()
    removed_ids: Set[Tuple[int, int]] = set()
    for index in drop_ids:
        annot = items[index]
        if annot.is_indirect:
            removed_ids.add(annot.objgen)
            if (
                isinstance(annot, pikepdf.Dictionary)
                and annot.get("/Subtype") == "/Widget"
            ):
                removed_widgets.add(annot.objgen)
    kept = []
    for index, annot in enumerate(items):
        if index in drop_ids:
            continue
        if isinstance(annot, pikepdf.Dictionary) and annot.get("/Subtype") == "/Popup":
            parent = annot.get("/Parent")
            if (
                isinstance(parent, pikepdf.Dictionary)
                and parent.is_indirect
                and parent.objgen in removed_ids
            ):
                continue
        kept.append(annot)
    page.obj["/Annots"] = pikepdf.Array(kept)
    if removed_widgets:
        acroform = pdf.Root.get("/AcroForm")
        fields = (
            acroform.get("/Fields") if isinstance(acroform, pikepdf.Dictionary) else None
        )
        if isinstance(fields, pikepdf.Array):
            acroform["/Fields"] = pikepdf.Array(
                _remove_from_fields(fields, removed_widgets)
            )


def scrub_document_strings(pdf: "pikepdf.Pdf", pii_values: Sequence[str]) -> None:
    """Remove PII that survives outside the page content streams.

    ``pii_values`` are the redacted strings with whitespace removed. Every
    string object reachable from the trailer (information dictionary,
    bookmarks, name trees, annotation and field values, JavaScript) that
    contains one of them once decoded and stripped of whitespace is replaced
    by an empty string. A metadata stream holding one is unreferenced, an
    embedded file holding one is detached, and an annotation whose appearance
    draws one is dropped. The walk is bounded by a visited set, so cyclic
    structures are harmless.
    """
    needles = [n for n in (_squeeze(v) for v in pii_values) if n]
    if not needles:
        return

    visited: Set[Tuple[int, int]] = set()
    doomed_files: Set[Tuple[int, int]] = set()
    name_arrays: List[Any] = []
    other_arrays: List[Any] = []
    stack: List[Any] = [pdf.trailer]
    empty = pikepdf.String("")

    while stack:
        obj = stack.pop()
        if obj.is_indirect:
            if obj.objgen in visited:
                continue
            visited.add(obj.objgen)
        if isinstance(obj, pikepdf.Array):
            other_arrays.append(obj)
        for key, value in _children(obj):
            if key == "/Encrypt":
                continue
            if isinstance(value, pikepdf.String):
                if _contains_any(_string_variants(value), needles):
                    obj[key] = empty
                continue
            if not _is_container(value):
                continue
            if isinstance(value, pikepdf.Stream):
                if key == "/Metadata" and _stream_has_pii(value, needles):
                    del obj[key]
                    continue
                if key == "/JS":
                    if _stream_has_pii(value, needles):
                        obj[key] = empty
                    continue
                if (value.get("/Type") == "/EmbeddedFile") and not isinstance(key, int):
                    # Handled with its file specification below.
                    continue
            if key == "/EF" and isinstance(value, pikepdf.Dictionary):
                if any(
                    isinstance(value[k], pikepdf.Stream)
                    and _stream_has_pii(value[k], needles)
                    for k in list(value.keys())
                ):
                    del obj[key]
                    if "/RF" in obj:
                        del obj["/RF"]
                    if obj.is_indirect:
                        doomed_files.add(obj.objgen)
                    continue
            if key == "/Names" and isinstance(value, pikepdf.Array):
                name_arrays.append(value)
            stack.append(value)

    # Detach file specifications that lost their embedded file.
    if doomed_files:
        for array in name_arrays:
            items = list(array)
            kept: List[Any] = []
            for i in range(0, len(items) - 1, 2):
                partner = items[i + 1]
                if _is_in(partner, doomed_files):
                    continue
                kept.extend([items[i], partner])
            if len(kept) != len(items):
                _replace_items(array, kept)
        for array in other_arrays:
            items = list(array)
            kept = [
                i for i in items if not _is_in(i, doomed_files)
            ]
            if len(kept) != len(items):
                _replace_items(array, kept)

    # Annotations: attachments that were detached and appearances with PII.
    cache: Dict[Tuple[int, int], bool] = {}
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        drop: Set[int] = set()
        for index, annot in enumerate(list(annots)):
            if not isinstance(annot, pikepdf.Dictionary):
                continue
            spec = annot.get("/FS")
            if (
                isinstance(spec, pikepdf.Dictionary)
                and spec.is_indirect
                and spec.objgen in doomed_files
            ):
                drop.add(index)
                continue
            if any(
                _appearance_has_pii(stream, needles, cache)
                for stream in _appearance_streams(annot)
            ):
                drop.add(index)
        _drop_annotations(pdf, page, drop)
