"""Unit tests for `agent/tools/matching.py`.

Covers the `match_rules` tool description: every enabled rule is evaluated
against extracted text (rule matching agrees with a direct/reference regex
application — sanity-checked here at the example level), overlapping/adjacent
matches merge into a single span, and OCR-recognized text is matched with the
same logic as body text.

Also includes a small Hypothesis-based sanity property test (in addition to
the required example-based tests) covering "matching + merging never drops
or duplicates a rule's matches within a single unit for non-overlapping
matches".
"""
import re
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent.tools.matching import match_rules  # noqa: E402
from shared.models import (  # noqa: E402
    ExtractedTextUnit,
    ImageRegion,
    RedactionRule,
    TextPosition,
)


def _rule(rule_name: str, pattern: str, enabled: bool = True) -> RedactionRule:
    return RedactionRule(
        ruleName=rule_name,
        pattern=pattern,
        enabled=enabled,
        isBuiltIn=False,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
    )


def _text_unit(text: str, zone: str = "body") -> ExtractedTextUnit:
    return ExtractedTextUnit(zone=zone, text=text, position=TextPosition(index=0))


def _image_region(recognized_text) -> ImageRegion:
    return ImageRegion(zone="image", sourceLocation="page-1-image-0", recognizedText=recognized_text)


def test_rule_matching_body_text_produces_span_with_correct_offsets():
    unit = _text_unit("call 13800001111 now")
    rule = _rule("mobile", r"1[3-9]\d{9}")

    spans = match_rules([unit], [], [rule])

    assert len(spans) == 1
    span = spans[0]
    assert span.zone == "body"
    assert span.ruleName == "mobile"
    assert span.startOffset == 5
    assert span.endOffset == 16
    assert unit.text[span.startOffset : span.endOffset] == "13800001111"
    assert span.mergedFromRuleNames is None


def test_rule_matching_image_recognized_text_produces_span():
    region = _image_region("id 110101199003077777 printed here")
    rule = _rule("id_card", r"\d{17}[\dX]")

    spans = match_rules([], [region], [rule])

    assert len(spans) == 1
    span = spans[0]
    assert span.zone == "image"
    assert span.ruleName == "id_card"
    assert region.recognizedText[span.startOffset : span.endOffset] == "110101199003077777"


def test_overlapping_matches_within_same_unit_merge_into_one_span():
    # "abcdef": rule A matches "abcd" (0-4), rule B matches "cdef" (2-6) —
    # overlapping ranges within the same text unit.
    unit = _text_unit("abcdef")
    rule_a = _rule("rule_a", r"abcd")
    rule_b = _rule("rule_b", r"cdef")

    spans = match_rules([unit], [], [rule_a, rule_b])

    assert len(spans) == 1
    span = spans[0]
    assert span.startOffset == 0
    assert span.endOffset == 6
    assert span.ruleName == "rule_a"
    assert span.mergedFromRuleNames == ["rule_a", "rule_b"]


def test_adjacent_matches_with_no_gap_merge_into_one_span():
    # "abcdef": rule A matches "abc" (0-3), rule B matches "def" (3-6) —
    # immediately adjacent (no gap) within the same text unit.
    unit = _text_unit("abcdef")
    rule_a = _rule("rule_a", r"abc")
    rule_b = _rule("rule_b", r"def")

    spans = match_rules([unit], [], [rule_a, rule_b])

    assert len(spans) == 1
    span = spans[0]
    assert span.startOffset == 0
    assert span.endOffset == 6
    assert span.mergedFromRuleNames == ["rule_a", "rule_b"]


def test_matches_in_different_text_units_are_never_merged():
    # Both units are "abcdef" with the same overlapping-rule setup as the
    # merge test above; because they are different units, each must
    # produce its own independent (merged-within-itself) span.
    unit_a = _text_unit("abcdef")
    unit_b = _text_unit("abcdef")
    rule_a = _rule("rule_a", r"abcd")
    rule_b = _rule("rule_b", r"cdef")

    spans = match_rules([unit_a, unit_b], [], [rule_a, rule_b])

    assert len(spans) == 2
    unit_refs = {span.unitRef for span in spans}
    assert unit_refs == {"text:0", "text:1"}
    for span in spans:
        assert span.startOffset == 0
        assert span.endOffset == 6


def test_disabled_rule_produces_no_matches_even_if_passed_in():
    unit = _text_unit("13800001111")
    disabled_rule = _rule("mobile", r"1[3-9]\d{9}", enabled=False)

    spans = match_rules([unit], [], [disabled_rule])

    assert spans == []


def test_non_overlapping_matches_within_a_unit_stay_separate():
    unit = _text_unit("13800001111 and then 15900002222 later")
    rule = _rule("mobile", r"1[3-9]\d{9}")

    spans = match_rules([unit], [], [rule])

    assert len(spans) == 2
    assert [(s.startOffset, s.endOffset) for s in spans] == [(0, 11), (21, 32)]


def test_image_region_with_no_recognized_text_is_skipped():
    region = _image_region(None)
    rule = _rule("mobile", r"1[3-9]\d{9}")

    spans = match_rules([], [region], [rule])

    assert spans == []


# ---------------------------------------------------------------------------
# Hypothesis sanity property test.
#
# This is deliberately narrow in scope: it only checks that, for a single
# fixed pattern applied to a single text unit, `match_rules`'s raw match
# count/offsets agree with a direct `re.finditer` reference call when none
# of the reference matches overlap or are adjacent (so no merging occurs
# and a 1:1 comparison is well-defined). The full "agrees with reference
# Implementation including merge scenarios" property and the
# Dedicated overlap/adjacency merge property are covered by
# their own tests — not duplicated here.
# ---------------------------------------------------------------------------

_DIGIT_TEXT = st.text(alphabet="0123456789 ", min_size=0, max_size=40)


@given(text=_DIGIT_TEXT)
def test_property_matches_agree_with_reference_finditer_when_non_overlapping(text):
    pattern = r"\d{3}"
    reference_matches = list(re.finditer(pattern, text))

    # Skip inputs where any two reference matches overlap/are adjacent,
    # since merging would legitimately reduce the span count below the
    # reference's raw match count (that scenario belongs to the merge tests).
    for first, second in zip(reference_matches, reference_matches[1:]):
        if second.start() <= first.end():
            return

    unit = _text_unit(text)
    rule = _rule("digits", pattern)

    spans = match_rules([unit], [], [rule])

    assert len(spans) == len(reference_matches)
    for span, reference in zip(spans, reference_matches):
        assert span.startOffset == reference.start()
        assert span.endOffset == reference.end()
        assert text[span.startOffset : span.endOffset] == reference.group()
