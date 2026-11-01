"""`match_rules` Agent tool.

Contract of `match_rules`:

    Input:  `ExtractedTextUnit[]` + the recognized `ImageRegion[]` + the
            enabled `RedactionRule[]` (read from DynamoDB)
    Output: `MatchSpan[]` (overlapping intervals already merged)
    Notes:  runs `re.finditer` for every text unit x every enabled rule, and
            merges overlapping/adjacent matches within the same text unit
            into a single interval

Behaviour:
    - Every enabled Redaction Rule is evaluated against extracted text.
    - Overlapping/adjacent match spans within the same text unit are merged
      into a single span.
    - OCR-recognized image text (`ImageRegion.recognizedText`) is evaluated
      with the exact same matching logic as body text — this module does
      not special-case `zone == "image"` beyond reading `recognizedText`
      instead of `text`.

Interface decisions (documented here since they affect callers/tests):

1. `unitRef` shape: `match_rules` searches three different input collections
   (`text_units`, `image_regions` and `ocr_text_units`), so a bare positional
   index would be ambiguous (e.g. index 0 could mean "the first text unit",
   "the first image region" or "the first OCR page unit"). `unitRef` is
   therefore a small namespaced string:
   ``f"text:{index}"`` for the `index`-th entry of `text_units`,
   ``f"image:{index}"`` for the `index`-th entry of `image_regions`, or
   ``f"ocr:{index}"`` for the `index`-th entry of `ocr_text_units` (all
   0-based, in input list order). This keeps `unitRef` a single `str`
   field (matching `shared/models.py`'s existing `MatchSpan.unitRef: str`
   type) while still letting a caller unambiguously look up which unit/
   region a `MatchSpan` came from, and — critically for merging — letting
   this module group raw matches by "same text unit, image region or OCR
   page unit" via a simple equality check on `unitRef`.

   The `ocr:` namespace (dual-path PDF detection additive extension) holds
   the OCR_Path's per-page units, whose offsets live in a *different*
   coordinate space from the same page's text-layer unit: the two paths
   read the same page independently, so offset 5 of `ocr:0` and offset 5
   of `text:0` are unrelated characters. Keeping them in separate
   namespaces is exactly what stops the grouping step below from merging
   them, which would be meaningless.

   OCR units are iterated **last**, after `text_units` and `image_regions`,
   so that adding them cannot shift any existing `text:`/`image:` index —
   the pre-existing namespaces keep numbering from 0 in their own input
   list order, and a call that passes no OCR units produces byte-identical
   output to before this parameter existed.

2. Disabled-rule defensiveness: design.md states the caller is expected to
   pass only enabled rules (`load_enabled_rules()` in the orchestration
   pseudocode), but this function defensively filters on
   `rule.enabled is True` anyway, so a caller that accidentally includes a
   disabled rule does not produce spurious matches.

3. Merge scope: merging only ever combines raw matches that share the same
   `unitRef` (i.e. the same text unit or image region). Matches in
   different units/regions are never merged, even if their offsets would
   overlap when compared naively — offsets are only meaningful *within*
   a single unit's text.

4. Merged-span `ruleName` representation: see `MatchSpan.mergedFromRuleNames`'s
   docstring in `shared/models.py` for the full rationale. In short: a
   merged span keeps the first contributing rule's name in `ruleName` (so
   existing single-rule-name callers keep working) and, only when more
   than one distinct rule contributed, sets `mergedFromRuleNames` to the
   de-duplicated, order-of-first-appearance list of every contributing
   rule name. Merging is provenance-preserving across passes: when an
   input span already carries `mergedFromRuleNames` (because it came out
   of an earlier merge, e.g. `match_rules`' output being re-merged with
   Guardrails spans), every name in that list is carried into the new
   merged span.
"""
from __future__ import annotations

import re
from typing import List, Optional

from shared.models import ExtractedTextUnit, ImageRegion, MatchSpan, RedactionRule

__all__ = ["match_rules", "merge_spans_within_unit"]


def _text_unit_ref(index: int) -> str:
    return f"text:{index}"


def _image_region_ref(index: int) -> str:
    return f"image:{index}"


def _ocr_unit_ref(index: int) -> str:
    return f"ocr:{index}"


def _raw_matches_for_text(
    zone,
    unit_ref: str,
    text: str,
    compiled_rules: List[tuple],
) -> List[MatchSpan]:
    """Run every compiled rule's `re.finditer` over `text` and return one
    (unmerged) `MatchSpan` per match found, in no particular guaranteed
    order across rules (merging, done by the caller, is order-independent)."""
    raw_matches: List[MatchSpan] = []
    for rule_name, compiled_pattern in compiled_rules:
        for match in compiled_pattern.finditer(text):
            raw_matches.append(
                MatchSpan(
                    zone=zone,
                    unitRef=unit_ref,
                    startOffset=match.start(),
                    endOffset=match.end(),
                    ruleName=rule_name,
                )
            )
    return raw_matches


def merge_spans_within_unit(spans: List[MatchSpan]) -> List[MatchSpan]:
    """Merge overlapping OR adjacent `MatchSpan`s that all share the same
    `unitRef` (callers must only pass spans already known to share one
    `unitRef` and `zone`; see `match_rules` for the grouping step).

    Two spans are considered overlapping-or-adjacent when, after sorting by
    `startOffset`, the next span's `startOffset` is `<=` the running merged
    span's `endOffset` (a gap of zero — i.e. immediately adjacent, no
    characters between them — still merges; any positive gap does not).

    This helper is part of the module's public surface: the orchestration layer
    reuses it to merge the concatenation of
    regex matches and Bedrock Guardrails PII detections, so the merge
    semantics stay identical across both detection sources.
    """
    if not spans:
        return []

    ordered = sorted(spans, key=lambda span: (span.startOffset, span.endOffset))

    merged: List[MatchSpan] = []
    current_start = ordered[0].startOffset
    current_end = ordered[0].endOffset
    current_rule_names: List[str] = _contributing_rule_names(ordered[0])

    for span in ordered[1:]:
        if span.startOffset <= current_end:
            # Overlaps or is immediately adjacent to the running span.
            current_end = max(current_end, span.endOffset)
            for rule_name in _contributing_rule_names(span):
                if rule_name not in current_rule_names:
                    current_rule_names.append(rule_name)
        else:
            merged.append(
                _build_merged_span(ordered[0], current_start, current_end, current_rule_names)
            )
            current_start = span.startOffset
            current_end = span.endOffset
            current_rule_names = _contributing_rule_names(span)

    merged.append(_build_merged_span(ordered[0], current_start, current_end, current_rule_names))
    return merged


def _contributing_rule_names(span: MatchSpan) -> List[str]:
    """Every rule name that contributed to `span`, in order of first
    appearance.

    An unmerged span contributes just its own `ruleName`. An *already
    merged* multi-rule span (as produced by a previous
    `merge_spans_within_unit` pass — e.g. `match_rules`' output being
    re-merged together with Guardrails spans by the orchestration layer)
    carries its full provenance in `mergedFromRuleNames`, which must be
    carried through rather than collapsed back to the scalar `ruleName`.
    `mergedFromRuleNames` already starts with
    `ruleName`, so returning it is a superset of `[span.ruleName]`.
    """
    if span.mergedFromRuleNames:
        rule_names = list(span.mergedFromRuleNames)
        if span.ruleName not in rule_names:
            # Defensive: keep `ruleName` as the primary name even if a
            # caller hand-built a span whose list omits it.
            rule_names.insert(0, span.ruleName)
        return rule_names
    return [span.ruleName]


def _build_merged_span(
    template: MatchSpan, start: int, end: int, rule_names: List[str]
) -> MatchSpan:
    return MatchSpan(
        zone=template.zone,
        unitRef=template.unitRef,
        startOffset=start,
        endOffset=end,
        ruleName=rule_names[0],
        mergedFromRuleNames=list(rule_names) if len(rule_names) > 1 else None,
    )


def match_rules(
    text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
    enabled_rules: List[RedactionRule],
    ocr_text_units: Optional[List[ExtractedTextUnit]] = None,
) -> List[MatchSpan]:
    """Evaluate every enabled Redaction Rule against every extracted text
    unit, every OCR-recognized image region, and every OCR_Path page unit,
    and return the merged set of resulting `MatchSpan`s.

    Args:
        text_units: body/table/header/footer text, as produced by
            `extract_document_content`.
        image_regions: image regions as produced by
            `extract_document_content` and populated by
            `recognize_image_text`. Regions whose
            `recognizedText` is `None` (never OCR'd, e.g. because the
            document had no images at all) are skipped.
        enabled_rules: the Redaction Rules to evaluate. Per design.md the
            caller is expected to pass only already-enabled rules; this
            function additionally filters on `rule.enabled is True`
            defensively, so any disabled rule accidentally included here
            still produces no matches.
        ocr_text_units: the OCR_Path's per-page units (`zone == "ocr"`), as
            produced by `ocr_pdf_pages`, or `None`/`[]` for callers with no
            second detection path (`.docx` jobs, and every call site that
            predates this parameter). Matched with the *identical* regex
            logic used for `text_units` — this function does not branch on
            `zone` — and iterated last so `text:`/`image:`
            indices are unaffected.

    Returns:
        One `MatchSpan` per group of overlapping/adjacent raw matches
        within a single text unit, image region, or OCR page unit.
        Matches in different units/regions are never merged
        with each other, regardless of their offsets — including across the
        `text:` and `ocr:` namespaces, whose offsets are not comparable.
    """
    compiled_rules = [
        (rule.ruleName, re.compile(rule.pattern))
        for rule in enabled_rules
        if rule.enabled is True
    ]

    raw_matches: List[MatchSpan] = []

    for index, unit in enumerate(text_units):
        raw_matches.extend(
            _raw_matches_for_text(
                zone=unit.zone,
                unit_ref=_text_unit_ref(index),
                text=unit.text,
                compiled_rules=compiled_rules,
            )
        )

    for index, region in enumerate(image_regions):
        if region.recognizedText is None:
            continue
        raw_matches.extend(
            _raw_matches_for_text(
                zone=region.zone,
                unit_ref=_image_region_ref(index),
                text=region.recognizedText,
                compiled_rules=compiled_rules,
            )
        )

    for index, unit in enumerate(ocr_text_units or []):
        if not unit.text:
            # A page Textract recognized no words on still yields a unit (so
            # `ocr:{index}` stays equal to the page index), and it can never
            # produce a match; skipped explicitly, mirroring the
            # `recognizedText is None` skip above.
            continue
        raw_matches.extend(
            _raw_matches_for_text(
                zone=unit.zone,
                unit_ref=_ocr_unit_ref(index),
                text=unit.text,
                compiled_rules=compiled_rules,
            )
        )

    matches_by_unit_ref: dict[str, List[MatchSpan]] = {}
    unit_ref_order: List[str] = []
    for span in raw_matches:
        if span.unitRef not in matches_by_unit_ref:
            matches_by_unit_ref[span.unitRef] = []
            unit_ref_order.append(span.unitRef)
        matches_by_unit_ref[span.unitRef].append(span)

    merged_matches: List[MatchSpan] = []
    for unit_ref in unit_ref_order:
        merged_matches.extend(merge_spans_within_unit(matches_by_unit_ref[unit_ref]))

    return merged_matches


# Backward-compatible alias for the pre-rename private name. Kept so any
# existing caller/test that imported `_merge_spans_within_unit` keeps working
# after the function became part of the module's public surface.
_merge_spans_within_unit = merge_spans_within_unit
