"""Detection results builder for the review-mode workflow.

Transforms the Agent's raw match spans into a deduplicated, ID'd
Detection_Results structure suitable for human review. Also provides a
span filter that narrows the full span set down to only the spans the
user confirmed.

This module is intentionally pure — no AWS/boto3 imports. It operates
solely on the in-memory data models defined in `shared.models`.

Requirements covered:
    - 3.1: sequential integer IDs starting from 1.
    - 3.2: deduplication by matched text (one row per value; see
            `build_detection_results`).
    - 3.3: each Detection_Value carries id, entityType, value, count, pages,
            contextSnippet.
    - 3.4: context snippet uses ±15 chars around first occurrence.
    - 3.5: fewer than 15 chars before → use all available preceding chars.
    - 3.6: fewer than 15 chars after → use all available following chars.
    - 4.3: filter_spans_by_selections maps confirmed IDs back to spans.
    - 4.4: None confirmed_ids (timeout) → return all spans unchanged.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from shared.models import ExtractedTextUnit, ImageRegion, MatchSpan

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

CONTEXT_RADIUS = 15


@dataclass
class DetectionValue:
    """A single deduplicated PII value in the detection results."""

    id: int
    entityType: str
    value: str
    count: int
    pages: List[int]
    contextSnippet: str


@dataclass
class DetectionResults:
    """The complete detection results for a review-mode job."""

    values: List[DetectionValue] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Context snippet helper
# ---------------------------------------------------------------------------


def _build_context_snippet(source_text: str, start: int, end: int) -> str:
    """Build a context snippet around a matched span.

    Extracts ±CONTEXT_RADIUS characters around the match and wraps the
    matched text in 「」 brackets.
    """
    matched = source_text[start:end]
    ctx_start = max(0, start - CONTEXT_RADIUS)
    ctx_end = min(len(source_text), end + CONTEXT_RADIUS)
    before = source_text[ctx_start:start]
    after = source_text[end:ctx_end]
    return f"{before}「{matched}」{after}"


# ---------------------------------------------------------------------------
# unitRef resolution helpers
# ---------------------------------------------------------------------------


def _parse_unit_ref(unit_ref: str) -> Tuple[str, int]:
    """Parse a unitRef string like 'text:0' into (namespace, index)."""
    namespace, _, idx_str = unit_ref.partition(":")
    return namespace, int(idx_str)


def _get_source_text(
    unit_ref: str,
    text_units: List[ExtractedTextUnit],
    ocr_text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
) -> Optional[str]:
    """Resolve a unitRef to the source text it refers to."""
    namespace, idx = _parse_unit_ref(unit_ref)
    if namespace == "text":
        if 0 <= idx < len(text_units):
            return text_units[idx].text
    elif namespace == "ocr":
        if 0 <= idx < len(ocr_text_units):
            return ocr_text_units[idx].text
    elif namespace == "image":
        if 0 <= idx < len(image_regions):
            return image_regions[idx].recognizedText
    return None


def _get_page_number(
    unit_ref: str,
    text_units: List[ExtractedTextUnit],
    ocr_text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
) -> Optional[int]:
    """Resolve a unitRef to a 1-based page number, or None if unavailable."""
    namespace, idx = _parse_unit_ref(unit_ref)
    if namespace == "text":
        if 0 <= idx < len(text_units):
            page_idx = text_units[idx].position.pageIndex
            if page_idx is not None:
                return page_idx + 1
    elif namespace == "ocr":
        if 0 <= idx < len(ocr_text_units):
            page_idx = ocr_text_units[idx].position.pageIndex
            if page_idx is not None:
                return page_idx + 1
    elif namespace == "image":
        if 0 <= idx < len(image_regions):
            # Image regions encode page info in sourceLocation, e.g. "page:0:img:1"
            # Extract page index from sourceLocation if it follows the pattern
            src_loc = image_regions[idx].sourceLocation
            if src_loc.startswith("page:"):
                parts = src_loc.split(":")
                if len(parts) >= 2:
                    try:
                        return int(parts[1]) + 1
                    except ValueError:
                        pass
    return None


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


def build_detection_results(
    spans: List[MatchSpan],
    text_units: List[ExtractedTextUnit],
    ocr_text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
) -> DetectionResults:
    """Build deduplicated detection results from match spans.

    Deduplicates by matched text: one row per distinct value, however many
    sources or entity types reported it (the same company name used to show up
    as three rows: guardrails:NAME, guardrails:ADDRESS, llm:NAME). The row's
    `entityType` lists every contributing rule name, comma-separated, in
    first-seen order. Each unique value gets a
    sequential integer ID starting from 1. Computes occurrence count,
    page list (1-based, deduped, sorted), and context snippet (±15 chars
    around first occurrence, value wrapped in 「」).

    Args:
        spans: All MatchSpan objects produced by the matching step.
        text_units: The text-layer extracted text units.
        ocr_text_units: The OCR-path extracted text units.
        image_regions: The embedded image regions (with recognizedText).

    Returns:
        A DetectionResults with values sorted by ascending ID.
    """
    # Track unique value_text → aggregation data
    # Order of first appearance determines ID assignment
    seen: Dict[str, Dict] = {}

    for span in spans:
        source_text = _get_source_text(
            span.unitRef, text_units, ocr_text_units, image_regions
        )
        if source_text is None:
            continue

        matched_text = source_text[span.startOffset : span.endOffset]
        key = matched_text
        rule_names = [span.ruleName, *(span.mergedFromRuleNames or [])]

        if key not in seen:
            # First occurrence — capture context snippet info
            context_snippet = _build_context_snippet(
                source_text, span.startOffset, span.endOffset
            )
            page = _get_page_number(
                span.unitRef, text_units, ocr_text_units, image_regions
            )
            seen[key] = {
                "value": matched_text,
                "ruleNames": list(dict.fromkeys(rule_names)),
                "count": 1,
                "pages": {page} if page is not None else set(),
                "contextSnippet": context_snippet,
            }
        else:
            # Subsequent occurrence — increment count, add page
            seen[key]["count"] += 1
            for name in rule_names:
                if name not in seen[key]["ruleNames"]:
                    seen[key]["ruleNames"].append(name)
            page = _get_page_number(
                span.unitRef, text_units, ocr_text_units, image_regions
            )
            if page is not None:
                seen[key]["pages"].add(page)

    # Build DetectionValue list with sequential IDs
    values: List[DetectionValue] = []
    for idx, data in enumerate(seen.values(), start=1):
        values.append(
            DetectionValue(
                id=idx,
                entityType=", ".join(data["ruleNames"]),
                value=data["value"],
                count=data["count"],
                pages=sorted(data["pages"]),
                contextSnippet=data["contextSnippet"],
            )
        )

    return DetectionResults(values=values)


# ---------------------------------------------------------------------------
# Span filter (for confirmation phase)
# ---------------------------------------------------------------------------


def filter_spans_by_selections(
    all_spans: List[MatchSpan],
    detection_results: DetectionResults,
    confirmed_ids: Optional[List[int]],
    text_units: List[ExtractedTextUnit],
    ocr_text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
) -> List[MatchSpan]:
    """Return only the spans belonging to confirmed Detection_Value IDs.

    If confirmed_ids is None (timeout), returns all_spans unchanged —
    the safety fallback that redacts everything.

    Args:
        all_spans: The full set of match spans from the detection phase.
        detection_results: The DetectionResults that were presented to the user.
        confirmed_ids: The list of Detection_Value IDs the user confirmed,
            or None if the timeout expired.
        text_units: The text-layer extracted text units.
        ocr_text_units: The OCR-path extracted text units.
        image_regions: The embedded image regions.

    Returns:
        The subset of all_spans whose matched text equals a confirmed
        Detection_Value's value (rows are keyed by value only, so confirming
        or rejecting a value applies to every occurrence of it).
    """
    if confirmed_ids is None:
        return all_spans

    # Build the set of confirmed values
    confirmed_id_set = set(confirmed_ids)
    confirmed_keys: set = set()
    for dv in detection_results.values:
        if dv.id in confirmed_id_set:
            confirmed_keys.add(dv.value)

    # Keep spans whose matched text is a confirmed value
    result: List[MatchSpan] = []
    for span in all_spans:
        source_text = _get_source_text(
            span.unitRef, text_units, ocr_text_units, image_regions
        )
        if source_text is None:
            continue
        matched_text = source_text[span.startOffset : span.endOffset]
        if matched_text in confirmed_keys:
            result.append(span)

    return result


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


def to_json_dict(results: DetectionResults) -> dict:
    """Serialize DetectionResults to a JSON-compatible dict for S3 persistence.

    Returns:
        A dict matching the Detection_Results JSON schema defined in design.md.
    """
    return {
        "values": [
            {
                "id": dv.id,
                "entityType": dv.entityType,
                "value": dv.value,
                "count": dv.count,
                "pages": dv.pages,
                "contextSnippet": dv.contextSnippet,
            }
            for dv in results.values
        ]
    }
