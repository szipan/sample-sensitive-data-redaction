"""Tests for `agent/orchestration.py`'s `_regroup_and_merge` helper
(bedrock-guardrails-pii-integration spec).

This module hosts the Correctness Property tests for the span-merging step
that combines regex `match_rules` output with Bedrock Guardrails PII
detections. Property test functions are prefixed by the property they cover
(e.g. `test_property_3_backward_compat_*`) so the properties stay
individually traceable.

Properties covered here:

- merge backward compatibility — *for any* list of `MatchSpan`
  objects produced exclusively by `match_rules` (no Guardrails spans),
  passing them through `_regroup_and_merge` produces an identical result to
  calling `match_rules` directly.
- merge produces non-overlapping spans per `unitRef`.
- graceful degradation preserves regex results — when Guardrails
  detection raises or returns nothing, the matches reaching redaction are
  exactly `match_rules`' output.
- `mergedFromRuleNames` tracks all contributing sources, across
  repeated merge passes.
"""
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# `agent.orchestration` transitively imports the document extraction/redaction
# tools, which import the native `fitz` (PyMuPDF) and `docx` (python-docx)
# packages. Those are only installed in the deployed agent bundle, not in
# every local dev environment, and none of them are exercised by these tests
# (only the pure-Python `_regroup_and_merge`/`merge_spans_within_unit` logic
# is). Register lightweight stand-ins before importing orchestration so the
# import chain resolves when the real packages are missing.
#
# We stub only modules that are genuinely *not importable*: attempting the real
# import first (rather than `sys.modules.setdefault`, which only checks whether
# the module happens to have been imported already) makes this independent of
# test collection order. With `setdefault`, running this module before
# `test_extraction_docx.py` / `test_redaction_pdf.py` in the same process would
# poison `sys.modules` with a MagicMock and break those real-`docx`/real-`fitz`
# tests.
for _stubbed_module in (
    "fitz",
    "docx",
    "docx.enum",
    "docx.enum.text",
    "docx.shared",
    "docx.table",
    "docx.text",
    "docx.text.paragraph",
    "docx.text.run",
):
    if _stubbed_module in sys.modules:
        continue
    try:
        importlib.import_module(_stubbed_module)
    except Exception:  # noqa: BLE001 - any import failure means "stub it"
        sys.modules[_stubbed_module] = MagicMock()

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent import orchestration  # noqa: E402
from agent.tools.guardrails_pii_detect import detect_pii_with_guardrails  # noqa: E402
from agent.tools.matching import match_rules, merge_spans_within_unit  # noqa: E402
from agent.tools.redaction_pdf import (  # noqa: E402
    PdfRedactionReport,
    PdfRedactionResult,
)
from agent.tools.structure_verification import VerificationResult  # noqa: E402
from shared.models import (  # noqa: E402
    ExtractedTextUnit,
    ImageRegion,
    MatchSpan,
    RedactionJob,
    RedactionRule,
    TextPosition,
)


def _rule(rule_name: str, pattern: str) -> RedactionRule:
    return RedactionRule(
        ruleName=rule_name,
        pattern=pattern,
        enabled=True,
        isBuiltIn=False,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
    )


def _text_unit(text: str, zone: str = "body") -> ExtractedTextUnit:
    return ExtractedTextUnit(zone=zone, text=text, position=TextPosition(index=0))


def _image_region(recognized_text) -> ImageRegion:
    return ImageRegion(
        zone="image",
        sourceLocation="page-1-image-0",
        recognizedText=recognized_text,
    )


def _reference_regroup(spans):
    """Independent reference for "group by `unitRef` (first-seen order), then
    merge each group with `merge_spans_within_unit`" — the documented
    postcondition of `_regroup_and_merge`."""
    groups: dict = {}
    unit_ref_order = []
    for span in spans:
        if span.unitRef not in groups:
            groups[span.unitRef] = []
            unit_ref_order.append(span.unitRef)
        groups[span.unitRef].append(span)

    merged = []
    for unit_ref in unit_ref_order:
        merged.extend(merge_spans_within_unit(groups[unit_ref]))
    return merged


# Rules whose patterns can overlap or sit adjacent to each other over the
# generated alphabet, so the generated documents actually exercise merging.
_RULES = [
    _rule("digits3", r"\d{3}"),
    _rule("ab_run", r"ab+"),
    _rule("x_pair", r"x."),
]

# Constrained alphabet: dense in characters the rules above can match, so a
# meaningful fraction of generated texts produce (and merge) spans.
_MATCHABLE_TEXT = st.text(alphabet="0123456789abx -", min_size=0, max_size=40)

_UNIT_REFS = ["text:0", "text:1", "text:2", "image:0", "image:1"]


@st.composite
def _match_spans(draw):
    """Generate a `MatchSpan` list spanning several `unitRef`s with a mix of
    overlapping, adjacent and disjoint offsets. `zone` is kept consistent
    per `unitRef` (as both real detection sources do)."""
    count = draw(st.integers(min_value=0, max_value=12))
    spans = []
    for _ in range(count):
        unit_ref = draw(st.sampled_from(_UNIT_REFS))
        start = draw(st.integers(min_value=0, max_value=25))
        length = draw(st.integers(min_value=1, max_value=8))
        rule_name = draw(st.sampled_from(["digits3", "ab_run", "x_pair"]))
        spans.append(
            MatchSpan(
                zone="image" if unit_ref.startswith("image:") else "body",
                unitRef=unit_ref,
                startOffset=start,
                endOffset=start + length,
                ruleName=rule_name,
            )
        )
    return spans


# ---------------------------------------------------------------------------
# Merge backward compatibility
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    unit_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=4),
    image_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=2),
)
def test_property_3_backward_compat_regex_only_spans_pass_through_unchanged(
    unit_texts, image_texts
):
    """Regex-only spans routed through `_regroup_and_merge` reproduce
    `match_rules`' own output exactly.
    """
    text_units = [_text_unit(text) for text in unit_texts]
    image_regions = [_image_region(text) for text in image_texts]

    regex_spans = match_rules(text_units, image_regions, _RULES)

    assert orchestration._regroup_and_merge(regex_spans) == regex_spans


@settings(max_examples=200)
@given(
    unit_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=4),
    image_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=2),
)
def test_property_3_backward_compat_matches_reference_grouping_for_regex_spans(
    unit_texts, image_texts
):
    """For regex-only input, `_regroup_and_merge` equals the reference
    "group by unitRef, merge per group" computation — the same grouping
    `match_rules` performs internally.
    """
    text_units = [_text_unit(text) for text in unit_texts]
    image_regions = [_image_region(text) for text in image_texts]

    regex_spans = match_rules(text_units, image_regions, _RULES)

    assert orchestration._regroup_and_merge(regex_spans) == _reference_regroup(
        regex_spans
    )


@settings(max_examples=300)
@given(spans=_match_spans())
def test_property_3_backward_compat_arbitrary_spans_match_reference_grouping(spans):
    """`_regroup_and_merge` on arbitrary span lists (overlapping, adjacent
    and disjoint, across several `unitRef`s) is exactly "group by `unitRef`
    in first-seen order, then `merge_spans_within_unit` per group" — the
    existing merge behavior, unchanged.
    """
    assert orchestration._regroup_and_merge(spans) == _reference_regroup(spans)


@settings(max_examples=300)
@given(spans=_match_spans())
def test_property_3_backward_compat_regroup_is_idempotent(spans):
    """Re-merging an already-merged result changes nothing, so wiring the
    helper into the pipeline cannot alter regex-only outcomes even if it
    were applied more than once.
    """
    merged_once = orchestration._regroup_and_merge(spans)

    assert orchestration._regroup_and_merge(merged_once) == merged_once


# ---------------------------------------------------------------------------
# Example-based checks for the same property (concrete, readable cases).
# ---------------------------------------------------------------------------


def test_property_3_backward_compat_example_empty_input_returns_empty_list():
    assert orchestration._regroup_and_merge([]) == []


def test_property_3_backward_compat_example_overlapping_regex_spans_preserved():
    """The canonical `match_rules` merge example ("abcd" + "cdef" -> 0..6
    with both rule names) survives a `_regroup_and_merge` round-trip."""
    unit = _text_unit("abcdef")
    rules = [_rule("rule_a", r"abcd"), _rule("rule_b", r"cdef")]

    regex_spans = match_rules([unit], [], rules)
    assert len(regex_spans) == 1
    assert regex_spans[0].mergedFromRuleNames == ["rule_a", "rule_b"]

    assert orchestration._regroup_and_merge(regex_spans) == regex_spans


def test_property_3_backward_compat_example_first_seen_unit_ref_order_kept():
    """Group ordering follows first appearance of each `unitRef`, matching
    `match_rules`' output ordering (text units before image regions, in
    input order)."""
    spans = [
        MatchSpan(zone="image", unitRef="image:0", startOffset=0, endOffset=2, ruleName="r1"),
        MatchSpan(zone="body", unitRef="text:1", startOffset=5, endOffset=7, ruleName="r2"),
        MatchSpan(zone="image", unitRef="image:0", startOffset=10, endOffset=12, ruleName="r3"),
        MatchSpan(zone="body", unitRef="text:1", startOffset=1, endOffset=3, ruleName="r4"),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert [span.unitRef for span in merged] == [
        "image:0",
        "image:0",
        "text:1",
        "text:1",
    ]
    assert [(span.startOffset, span.endOffset) for span in merged] == [
        (0, 2),
        (10, 12),
        (1, 3),
        (5, 7),
    ]


def test_property_3_backward_compat_example_spans_in_different_units_never_merge():
    """Identical offsets in different `unitRef`s stay separate — offsets are
    only meaningful within a single text unit / image region."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=4, ruleName="r"),
        MatchSpan(zone="body", unitRef="text:1", startOffset=0, endOffset=4, ruleName="r"),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert merged == spans

# ---------------------------------------------------------------------------
# Merge produces non-overlapping spans per unitRef
# ---------------------------------------------------------------------------

_GUARDRAILS_RULE_NAMES = [
    "guardrails:NAME",
    "guardrails:EMAIL",
    "guardrails:PHONE",
    "guardrails:ADDRESS",
]

_REGEX_RULE_NAMES = ["phone", "id_card", "digits3"]


@st.composite
def _mixed_source_spans(draw):
    """Generate the real mixed-source case: regex-style and
    `"guardrails:NAME"`-style spans interleaved in the *same* `unitRef`,
    with deliberately clustered offsets so overlaps and exact adjacency are
    common. Mirrors `orchestration.handle_job`'s
    `regex_matches + guardrails_matches` concatenation."""
    unit_refs = draw(
        st.lists(st.sampled_from(_UNIT_REFS), min_size=1, max_size=3, unique=True)
    )
    count = draw(st.integers(min_value=1, max_value=14))

    spans = []
    for _ in range(count):
        unit_ref = draw(st.sampled_from(unit_refs))
        # Small offset window => spans routinely overlap or touch.
        start = draw(st.integers(min_value=0, max_value=12))
        length = draw(st.integers(min_value=1, max_value=6))
        rule_name = draw(
            st.one_of(
                st.sampled_from(_REGEX_RULE_NAMES),
                st.sampled_from(_GUARDRAILS_RULE_NAMES),
            )
        )
        spans.append(
            MatchSpan(
                zone="image" if unit_ref.startswith("image:") else "body",
                unitRef=unit_ref,
                startOffset=start,
                endOffset=start + length,
                ruleName=rule_name,
            )
        )
    return spans


def _assert_no_overlaps_within_unit_ref(spans):
    """Assert the non-overlap guarantee on a `_regroup_and_merge` result: for any two output
    spans A, B sharing a `unitRef`, either `A.endOffset <= B.startOffset` or
    `B.endOffset <= A.startOffset`."""
    groups: dict = {}
    for span in spans:
        groups.setdefault(span.unitRef, []).append(span)

    for unit_ref, group in groups.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                assert (
                    a.endOffset <= b.startOffset or b.endOffset <= a.startOffset
                ), (
                    "overlapping output spans in unitRef {!r}: "
                    "({}, {}) and ({}, {})".format(
                        unit_ref, a.startOffset, a.endOffset, b.startOffset, b.endOffset
                    )
                )


@settings(max_examples=300)
@given(spans=_match_spans())
def test_property_4_non_overlapping_arbitrary_spans_produce_disjoint_output(spans):
    """`_regroup_and_merge` output never contains two overlapping spans
    within the same `unitRef`, for arbitrary span input.
    """
    _assert_no_overlaps_within_unit_ref(orchestration._regroup_and_merge(spans))


@settings(max_examples=300)
@given(spans=_mixed_source_spans())
def test_property_4_non_overlapping_mixed_regex_and_guardrails_spans(spans):
    """The mixed-source case (regex rule names + `"guardrails:*"` names in
    the same `unitRef`, offsets deliberately clustered so they collide)
    still collapses into non-overlapping output.
    """
    _assert_no_overlaps_within_unit_ref(orchestration._regroup_and_merge(spans))


@settings(max_examples=200)
@given(
    unit_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=3),
    image_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=2),
    guardrails_spans=_mixed_source_spans(),
)
def test_property_4_non_overlapping_real_regex_output_plus_guardrails_spans(
    unit_texts, image_texts, guardrails_spans
):
    """End-to-end shape of the pipeline: real `match_rules` output (already
    merged once) concatenated with Guardrails-style spans, re-merged. The
    combined result is still disjoint per `unitRef`, which is what the
    redaction step relies on.
    """
    regex_spans = match_rules(
        [_text_unit(text) for text in unit_texts],
        [_image_region(text) for text in image_texts],
        _RULES,
    )

    merged = orchestration._regroup_and_merge(regex_spans + guardrails_spans)

    _assert_no_overlaps_within_unit_ref(merged)


@settings(max_examples=300)
@given(spans=_mixed_source_spans())
def test_property_4_non_overlapping_output_covers_same_offsets_as_input(spans):
    """Non-overlap is not achieved by dropping coverage: every input offset
    is still covered by exactly one output span (the "union of
    both ranges").
    """
    merged = orchestration._regroup_and_merge(spans)

    def covered_offsets(span_list):
        return {
            (span.unitRef, offset)
            for span in span_list
            for offset in range(span.startOffset, span.endOffset)
        }

    assert covered_offsets(merged) == covered_offsets(spans)


# ---------------------------------------------------------------------------
# Example-based checks for the non-overlap guarantee (concrete merge-boundary cases).
# ---------------------------------------------------------------------------


def test_property_4_non_overlapping_example_regex_and_guardrails_overlap_collapse():
    """A regex span and a Guardrails span overlapping in the same `unitRef`
    become one span covering the union, carrying both names."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=4, endOffset=12, ruleName="phone"),
        MatchSpan(
            zone="body",
            unitRef="text:0",
            startOffset=8,
            endOffset=20,
            ruleName="guardrails:PHONE",
        ),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert len(merged) == 1
    assert (merged[0].startOffset, merged[0].endOffset) == (4, 20)
    assert merged[0].mergedFromRuleNames == ["phone", "guardrails:PHONE"]
    _assert_no_overlaps_within_unit_ref(merged)


def test_property_4_non_overlapping_example_exactly_adjacent_spans_merge():
    """Adjacency (`A.endOffset == B.startOffset`, no characters between)
    merges into a single span, per `merge_spans_within_unit`'s documented
    semantics."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=5, ruleName="id_card"),
        MatchSpan(
            zone="body",
            unitRef="text:0",
            startOffset=5,
            endOffset=9,
            ruleName="guardrails:NAME",
        ),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert len(merged) == 1
    assert (merged[0].startOffset, merged[0].endOffset) == (0, 9)
    assert merged[0].mergedFromRuleNames == ["id_card", "guardrails:NAME"]


def test_property_4_non_overlapping_example_positive_gap_does_not_merge():
    """A one-character gap keeps the spans separate — and the two outputs
    are disjoint, satisfying the non-overlap guarantee."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=5, ruleName="id_card"),
        MatchSpan(
            zone="body",
            unitRef="text:0",
            startOffset=6,
            endOffset=9,
            ruleName="guardrails:NAME",
        ),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert [(span.startOffset, span.endOffset) for span in merged] == [(0, 5), (6, 9)]
    assert [span.mergedFromRuleNames for span in merged] == [None, None]
    _assert_no_overlaps_within_unit_ref(merged)


def test_property_4_non_overlapping_example_chain_of_overlaps_collapses_to_one():
    """A transitive chain (0-4, 3-8, 7-11) collapses to a single 0-11 span
    rather than leaving pairwise-disjoint fragments."""
    spans = [
        MatchSpan(zone="image", unitRef="image:0", startOffset=0, endOffset=4, ruleName="phone"),
        MatchSpan(
            zone="image",
            unitRef="image:0",
            startOffset=3,
            endOffset=8,
            ruleName="guardrails:EMAIL",
        ),
        MatchSpan(zone="image", unitRef="image:0", startOffset=7, endOffset=11, ruleName="id_card"),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert len(merged) == 1
    assert (merged[0].startOffset, merged[0].endOffset) == (0, 11)
    _assert_no_overlaps_within_unit_ref(merged)


def test_property_4_non_overlapping_example_same_offsets_different_units_kept():
    """Overlap is only ever evaluated within a `unitRef`: identical offsets
    in different units both survive, and the non-overlap guarantee is unaffected."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=2, endOffset=6, ruleName="phone"),
        MatchSpan(
            zone="body",
            unitRef="text:1",
            startOffset=2,
            endOffset=6,
            ruleName="guardrails:NAME",
        ),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert merged == spans
    _assert_no_overlaps_within_unit_ref(merged)

# ---------------------------------------------------------------------------
# Graceful degradation preserves regex results
#
# The pipeline harness below runs the *real* `handle_job` matching stage —
# real `match_rules`, real `_regroup_and_merge` — and stubs only the I/O
# boundaries (`_load_job`, S3 read, extraction, OCR, rules load, redaction,
# verification, storage, status update), mirroring `tests/test_orchestration.py`'s
# approach. That way a Guardrails failure is injected exactly where it happens
# in production (at the `detect_pii_with_guardrails` call site) and the
# assertion is made on the span list that actually reaches `_redact_document`.
# ---------------------------------------------------------------------------

_PIPELINE_JOB_ID = "22222222-2222-2222-2222-222222222222"


def _pipeline_job() -> RedactionJob:
    return RedactionJob(
        jobId=_PIPELINE_JOB_ID,
        status="processing",
        sourceKey="{}/source.docx".format(_PIPELINE_JOB_ID),
        sourceExtension=".docx",
        fileName="source.docx",
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )


def _run_pipeline(text_units, image_regions, rules, guardrails_impl):
    """Run `handle_job` with only the I/O boundaries stubbed and return
    `(match_spans_passed_to_redaction, update_job_status_mock)`.

    `guardrails_impl(text_units, image_regions)` stands in for the Guardrails
    detection tool, so a test can make it raise, return `[]`, or return spans.
    """
    captured = {}

    def _capture_redact(
        file_bytes,
        units,
        match_spans,
        extension,
        ocr_text_units=None,
        image_regions=None,
    ):
        captured["matches"] = match_spans
        # `_redact_document` returns a `PdfRedactionResult` for both
        # extensions now; `handle_job`
        # reads `.documentBytes` off it.
        return PdfRedactionResult(
            documentBytes=b"redacted-bytes",
            report=PdfRedactionReport(
                textLayerRegions=0, ocrRegions=0, agreedRegions=0
            ),
        )

    content = SimpleNamespace(
        textUnits=list(text_units), imageRegions=list(image_regions)
    )

    with patch.object(
        orchestration, "_load_job", return_value=_pipeline_job()
    ), patch.object(
        orchestration, "_read_source_bytes", return_value=b"source-bytes"
    ), patch.object(
        orchestration, "_extract_document_content", return_value=content
    ), patch.object(
        orchestration, "recognize_image_text", side_effect=lambda regions: regions
    ), patch.object(
        orchestration, "_load_enabled_rules", return_value=list(rules)
    ), patch.object(
        orchestration, "detect_pii_with_guardrails", side_effect=guardrails_impl
    ), patch.object(
        orchestration,
        "detect_pii_with_llm",
        new=MagicMock(return_value=[]),
    ), patch.object(
        orchestration, "_redact_document", side_effect=_capture_redact
    ), patch.object(
        orchestration, "verify_structure", return_value=VerificationResult.success()
    ), patch.object(
        orchestration, "store_redacted_document", return_value="redacted/key.docx"
    ), patch.object(
        orchestration, "update_job_status"
    ) as update_job_status:
        orchestration.handle_job(_PIPELINE_JOB_ID)

    return captured.get("matches"), update_job_status


def _raise_guardrails_error(_text_units, _image_regions, _ocr_text_units=None):
    raise RuntimeError("ApplyGuardrail unavailable")


def _no_guardrails_spans(_text_units, _image_regions, _ocr_text_units=None):
    return []


@settings(max_examples=200)
@given(
    unit_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=4),
    image_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=2),
)
def test_property_5_graceful_degradation_empty_guardrails_result_is_identity(
    unit_texts, image_texts
):
    """The "Guardrails produced nothing" case (missing env vars, API error,
    or genuinely no PII) leaves the regex result untouched: concatenating an
    empty span list and re-merging returns `match_rules`' output verbatim.
    """
    regex_spans = match_rules(
        [_text_unit(text) for text in unit_texts],
        [_image_region(text) for text in image_texts],
        _RULES,
    )

    assert orchestration._regroup_and_merge(regex_spans + []) == regex_spans


@settings(max_examples=50)
@given(
    unit_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=3),
    image_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=2),
)
def test_property_5_graceful_degradation_pipeline_falls_back_to_regex_on_exception(
    unit_texts, image_texts
):
    """When `detect_pii_with_guardrails` raises, the span list that reaches
    redaction is exactly what `match_rules` alone produces — the Guardrails
    layer's failure costs nothing but its own detections.
    """
    text_units = [_text_unit(text) for text in unit_texts]
    image_regions = [_image_region(text) for text in image_texts]

    matches, _update = _run_pipeline(
        text_units, image_regions, _RULES, _raise_guardrails_error
    )

    assert matches == match_rules(text_units, image_regions, _RULES)


@settings(max_examples=50)
@given(
    unit_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=3),
    image_texts=st.lists(_MATCHABLE_TEXT, min_size=0, max_size=2),
)
def test_property_5_graceful_degradation_exception_and_empty_list_are_indistinguishable(
    unit_texts, image_texts
):
    """A raising Guardrails tool, a Guardrails tool returning `[]` (its own
    internal error handling), and Guardrails being effectively disabled all
    produce byte-identical matches — the degradation path has no observable
    variants.
    """
    text_units = [_text_unit(text) for text in unit_texts]
    image_regions = [_image_region(text) for text in image_texts]

    raised_matches, _ = _run_pipeline(
        text_units, image_regions, _RULES, _raise_guardrails_error
    )
    empty_matches, _ = _run_pipeline(
        text_units, image_regions, _RULES, _no_guardrails_spans
    )

    assert raised_matches == empty_matches
    assert raised_matches == match_rules(text_units, image_regions, _RULES)


@settings(max_examples=200)
@given(spans=_match_spans())
def test_property_5_graceful_degradation_regex_spans_survive_empty_concatenation(spans):
    """For arbitrary regex-side span lists (not just ones a rule set happens
    to generate), appending zero Guardrails spans before re-merging cannot
    change, drop, or reorder anything already merged.
    """
    already_merged = orchestration._regroup_and_merge(spans)

    assert orchestration._regroup_and_merge(already_merged + []) == already_merged


def test_property_5_graceful_degradation_example_job_still_completes_after_failure():
    """Operational consequence: a Guardrails exception must
    not push the Job to `failed`; it completes on regex-only matches."""
    text_units = [_text_unit("call 13800001111 now")]

    matches, update_job_status = _run_pipeline(
        text_units, [], [_rule("digits3", r"\d{3}")], _raise_guardrails_error
    )

    assert matches == match_rules(text_units, [], [_rule("digits3", r"\d{3}")])
    assert matches != []
    update_job_status.assert_called_once_with(
        _PIPELINE_JOB_ID,
        orchestration.STATUS_COMPLETED,
        redacted_key="redacted/key.docx",
        # `.docx` is single-path, so its Detection_Summary carries ONLY the
        # two always-present LLM counts and OMITS the dual-path keys
        #. With no LLM env config the
        # real LLM source returns no spans, so both counts are 0.
        detection_summary={"llmSpans": 0, "llmOnlySpans": 0},
    )


def test_property_5_graceful_degradation_example_real_tool_without_env_vars():
    """The real `detect_pii_with_guardrails` (not a stub) with no
    `GUARDRAIL_ID`/`GUARDRAIL_VERSION` in the environment returns no spans,
    so the pipeline output is the regex-only result."""
    text_units = [_text_unit("id 123456 and ab")]
    rules = [_rule("digits3", r"\d{3}"), _rule("ab_run", r"ab+")]
    regex_only = match_rules(text_units, [], rules)

    stripped_env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("GUARDRAIL_ID", "GUARDRAIL_VERSION")
    }
    with patch.dict(os.environ, stripped_env, clear=True):
        matches, update_job_status = _run_pipeline(
            text_units, [], rules, detect_pii_with_guardrails
        )

    assert matches == regex_only
    update_job_status.assert_called_once_with(
        _PIPELINE_JOB_ID,
        orchestration.STATUS_COMPLETED,
        redacted_key="redacted/key.docx",
        # `.docx` records only the two always-present LLM counts; no LLM env
        # Config -> both 0.
        detection_summary={"llmSpans": 0, "llmOnlySpans": 0},
    )


def test_property_5_graceful_degradation_example_successful_guardrails_adds_spans():
    """Sanity counterpart: the harness genuinely does route Guardrails spans
    into the merge when detection succeeds, so the fallback assertions above
    are not passing merely because Guardrails output is ignored."""
    text_units = [_text_unit("id 123456 and ab")]
    rules = [_rule("digits3", r"\d{3}")]

    def _one_span(_text_units, _image_regions, _ocr_text_units=None):
        return [
            MatchSpan(
                zone="body",
                unitRef="text:0",
                startOffset=14,
                endOffset=16,
                # A type untouched by the default NAME/ADDRESS source switch
                # and by the plausibility filter.
                ruleName="guardrails:DRIVER_ID",
            )
        ]

    matches, _update = _run_pipeline(text_units, [], rules, _one_span)

    assert matches != match_rules(text_units, [], rules)
    assert any(span.ruleName == "guardrails:DRIVER_ID" for span in matches)


# ---------------------------------------------------------------------------
# `mergedFromRuleNames` tracks all contributing sources
# ---------------------------------------------------------------------------


def _contributing_names(span):
    """Every rule name carried by an *input* span: its full
    `mergedFromRuleNames` when it is itself an earlier merge result,
    otherwise just its `ruleName`."""
    if span.mergedFromRuleNames:
        return list(span.mergedFromRuleNames)
    return [span.ruleName]


def _expected_provenance(input_spans, output_span):
    """Reference `mergedFromRuleNames` for `output_span`: the de-duplicated,
    first-appearance-ordered names of every input span that fell inside the
    merged range (inputs are visited in `(startOffset, endOffset)` order),
    or `None` when only one distinct name contributed."""
    contributors = sorted(
        (
            span
            for span in input_spans
            if span.unitRef == output_span.unitRef
            and span.startOffset >= output_span.startOffset
            and span.endOffset <= output_span.endOffset
        ),
        key=lambda span: (span.startOffset, span.endOffset),
    )

    names = []
    for span in contributors:
        for name in _contributing_names(span):
            if name not in names:
                names.append(name)
    return names if len(names) > 1 else None


@st.composite
def _provenance_spans(draw):
    """Mixed regex/Guardrails spans in tightly clustered offsets, where some
    inputs are *already merged* spans carrying `mergedFromRuleNames` (the
    real shape of `match_rules` output being re-merged with Guardrails
    detections by `handle_job`)."""
    unit_refs = draw(
        st.lists(st.sampled_from(_UNIT_REFS), min_size=1, max_size=2, unique=True)
    )
    count = draw(st.integers(min_value=1, max_value=10))

    spans = []
    for _ in range(count):
        unit_ref = draw(st.sampled_from(unit_refs))
        start = draw(st.integers(min_value=0, max_value=10))
        length = draw(st.integers(min_value=1, max_value=6))
        names = draw(
            st.lists(
                st.sampled_from(_REGEX_RULE_NAMES + _GUARDRAILS_RULE_NAMES),
                min_size=1,
                max_size=3,
                unique=True,
            )
        )
        spans.append(
            MatchSpan(
                zone="image" if unit_ref.startswith("image:") else "body",
                unitRef=unit_ref,
                startOffset=start,
                endOffset=start + length,
                ruleName=names[0],
                mergedFromRuleNames=names if len(names) > 1 else None,
            )
        )
    return spans


@settings(max_examples=300)
@given(spans=_mixed_source_spans())
def test_property_7_provenance_merged_span_lists_every_contributing_name(spans):
    """Every output span's `mergedFromRuleNames` equals the de-duplicated,
    first-appearance-ordered list of the rule names that merged into it —
    regex names and `"guardrails:*"` names alike — or `None` when a single
    name contributed.
    """
    merged = orchestration._regroup_and_merge(spans)

    for output_span in merged:
        assert output_span.mergedFromRuleNames == _expected_provenance(
            spans, output_span
        )


@settings(max_examples=300)
@given(spans=_provenance_spans())
def test_property_7_provenance_survives_repeated_merge_passes(spans):
    """Inputs that already carry `mergedFromRuleNames` (an earlier merge's
    output) keep every one of those names after being merged again with
    Guardrails spans — provenance is never collapsed back to the scalar
    `ruleName`.
    """
    merged = orchestration._regroup_and_merge(spans)

    for output_span in merged:
        assert output_span.mergedFromRuleNames == _expected_provenance(
            spans, output_span
        )

    # And re-merging the result is provenance-stable (the pipeline may merge
    # more than once as sources are added).
    remerged = orchestration._regroup_and_merge(merged)
    assert remerged == merged


@settings(max_examples=300)
@given(spans=_provenance_spans())
def test_property_7_provenance_no_name_is_ever_lost_or_invented(spans):
    """The union of all output provenance names equals the union of all input
    names: merging neither drops a contributing source nor invents one.
    """
    merged = orchestration._regroup_and_merge(spans)

    input_names = {name for span in spans for name in _contributing_names(span)}
    output_names = {
        name for span in merged for name in _contributing_names(span)
    }

    assert output_names == input_names


@settings(max_examples=300)
@given(spans=_provenance_spans())
def test_property_7_provenance_is_deduplicated_and_only_set_when_multi_source(spans):
    """Structural invariants of the field: no duplicates, `ruleName` first,
    and `None` (never a one-element list) when a single name contributed.
    """
    for span in orchestration._regroup_and_merge(spans):
        names = span.mergedFromRuleNames
        if names is None:
            continue
        assert len(names) > 1
        assert len(names) == len(set(names))
        assert names[0] == span.ruleName


# ---------------------------------------------------------------------------
# Example-based checks for `mergedFromRuleNames` provenance (concrete cases).
# ---------------------------------------------------------------------------


def test_property_7_provenance_example_regex_and_guardrails_both_recorded():
    """The headline case: a regex span overlapping a Guardrails span in the
    same `unitRef` yields one span naming both sources."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=6, ruleName="id_card"),
        MatchSpan(
            zone="body",
            unitRef="text:0",
            startOffset=4,
            endOffset=10,
            ruleName="guardrails:NAME",
        ),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert len(merged) == 1
    assert merged[0].ruleName == "id_card"
    assert merged[0].mergedFromRuleNames == ["id_card", "guardrails:NAME"]


def test_property_7_provenance_example_order_follows_offset_not_input_order():
    """Names appear in offset order (spans are merged after sorting by
    `startOffset`), so the Guardrails span starting earlier is named first
    even though it is listed second in the input."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=5, endOffset=12, ruleName="phone"),
        MatchSpan(
            zone="body",
            unitRef="text:0",
            startOffset=1,
            endOffset=7,
            ruleName="guardrails:PHONE",
        ),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert merged[0].mergedFromRuleNames == ["guardrails:PHONE", "phone"]
    assert merged[0].ruleName == "guardrails:PHONE"


def test_property_7_provenance_example_repeated_rule_name_appears_once():
    """A rule name contributing several overlapping spans is recorded once;
    the list is de-duplicated, not a per-match tally."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=4, ruleName="digits3"),
        MatchSpan(zone="body", unitRef="text:0", startOffset=3, endOffset=8, ruleName="digits3"),
        MatchSpan(
            zone="body",
            unitRef="text:0",
            startOffset=6,
            endOffset=11,
            ruleName="guardrails:EMAIL",
        ),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert len(merged) == 1
    assert merged[0].mergedFromRuleNames == ["digits3", "guardrails:EMAIL"]


def test_property_7_provenance_example_single_source_merge_stays_none():
    """Two spans from the *same* rule merging is still a single-source span,
    so `mergedFromRuleNames` remains `None` rather than `["digits3"]`."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=4, ruleName="digits3"),
        MatchSpan(zone="body", unitRef="text:0", startOffset=2, endOffset=9, ruleName="digits3"),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert len(merged) == 1
    assert (merged[0].startOffset, merged[0].endOffset) == (0, 9)
    assert merged[0].mergedFromRuleNames is None


def test_property_7_provenance_example_multi_pass_merge_keeps_earlier_names():
    """Regression: an already-merged span (`mergedFromRuleNames=["a", "b"]`,
    as produced by `match_rules`) merged again with a Guardrails span keeps
    both original names alongside the new one, instead of collapsing to the
    scalar `ruleName`."""
    already_merged = MatchSpan(
        zone="body",
        unitRef="text:0",
        startOffset=0,
        endOffset=8,
        ruleName="a",
        mergedFromRuleNames=["a", "b"],
    )
    guardrails_span = MatchSpan(
        zone="body",
        unitRef="text:0",
        startOffset=6,
        endOffset=14,
        ruleName="guardrails:ADDRESS",
    )

    merged = orchestration._regroup_and_merge([already_merged, guardrails_span])

    assert len(merged) == 1
    assert (merged[0].startOffset, merged[0].endOffset) == (0, 14)
    assert merged[0].mergedFromRuleNames == ["a", "b", "guardrails:ADDRESS"]


def test_property_7_provenance_example_real_match_rules_output_plus_guardrails():
    """End-to-end provenance through the real pipeline shape: `match_rules`
    merges two regex rules into one span, then a Guardrails span overlapping
    it contributes a third name."""
    unit = _text_unit("abcdef")
    regex_spans = match_rules([unit], [], [_rule("rule_a", r"abcd"), _rule("rule_b", r"cdef")])
    assert regex_spans[0].mergedFromRuleNames == ["rule_a", "rule_b"]

    guardrails_span = MatchSpan(
        zone="body",
        unitRef="text:0",
        startOffset=5,
        endOffset=6,
        ruleName="guardrails:NAME",
    )

    merged = orchestration._regroup_and_merge(regex_spans + [guardrails_span])

    assert len(merged) == 1
    assert merged[0].mergedFromRuleNames == ["rule_a", "rule_b", "guardrails:NAME"]


def test_property_7_provenance_example_names_not_shared_across_unit_refs():
    """Provenance is per-`unitRef`: a Guardrails span in `text:1` never
    appears in the provenance of a regex span in `text:0`."""
    spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=5, ruleName="phone"),
        MatchSpan(
            zone="body",
            unitRef="text:1",
            startOffset=0,
            endOffset=5,
            ruleName="guardrails:PHONE",
        ),
    ]

    merged = orchestration._regroup_and_merge(spans)

    assert [span.mergedFromRuleNames for span in merged] == [None, None]
    assert [span.ruleName for span in merged] == ["phone", "guardrails:PHONE"]

# ---------------------------------------------------------------------------
# Merging is additive — coverage is the union of all three sources
#
# The `llm-cjk-pii-recall` feature adds a *third* detection source. The pipeline
# concatenates `regex_matches + guardrails_matches + llm_matches` (LLM last) and
# feeds that single list to the same, untouched `_regroup_and_merge`. These tests
# prove the merge is a pure union of covered offsets grouped by `unitRef`: adding
# the LLM source contributes coverage and never removes any — merging is additive.
# ---------------------------------------------------------------------------

# `"llm:*"`-namespaced rule names, mirroring `detect_pii_with_llm`'s
# `ruleName = f"llm:{PII_Entity_Type}"` output, including the `UNSPECIFIED`
# fallback label.
_LLM_RULE_NAMES = [
    "llm:NAME",
    "llm:CN_RESIDENT_ID_NUMBER",
    "llm:ADDRESS",
    "llm:UNSPECIFIED",
]


def _covered_offsets(span_list):
    """The set of `(unitRef, offset)` pairs a span list covers — the same helper
    idea used inside
    `test_property_4_non_overlapping_output_covers_same_offsets_as_input`,
    hoisted to module scope so the additivity tests can share it."""
    return {
        (span.unitRef, offset)
        for span in span_list
        for offset in range(span.startOffset, span.endOffset)
    }


@st.composite
def _three_source_spans(draw):
    """Generate three *separate* span lists — regex-named, `"guardrails:*"`-named
    and `"llm:*"`-named — over a shared small set of `unitRef`s with deliberately
    clustered offsets (mirroring `_mixed_source_spans`' windows) so spans from
    different sources frequently overlap and sit adjacent within the same
    `unitRef`.

    Returns `(regex_spans, guardrails_spans, llm_spans)` — the exact three
    collections `orchestration.handle_job` concatenates, kept separate so a test
    can concatenate them in the pipeline's order (regex + guardrails + llm)."""
    unit_refs = draw(
        st.lists(st.sampled_from(_UNIT_REFS), min_size=1, max_size=3, unique=True)
    )

    def _draw_source(rule_names):
        count = draw(st.integers(min_value=0, max_value=8))
        spans = []
        for _ in range(count):
            unit_ref = draw(st.sampled_from(unit_refs))
            # Small offset window => spans routinely overlap or touch, both
            # within a source and across the three sources sharing a unitRef.
            start = draw(st.integers(min_value=0, max_value=12))
            length = draw(st.integers(min_value=1, max_value=6))
            rule_name = draw(st.sampled_from(rule_names))
            spans.append(
                MatchSpan(
                    zone="image" if unit_ref.startswith("image:") else "body",
                    unitRef=unit_ref,
                    startOffset=start,
                    endOffset=start + length,
                    ruleName=rule_name,
                )
            )
        return spans

    regex_spans = _draw_source(_REGEX_RULE_NAMES)
    guardrails_spans = _draw_source(_GUARDRAILS_RULE_NAMES)
    llm_spans = _draw_source(_LLM_RULE_NAMES)
    return regex_spans, guardrails_spans, llm_spans


@settings(max_examples=200)
@given(sources=_three_source_spans())
def test_property_9_additive_merge_coverage_is_union_of_three_sources(sources):
    """`_regroup_and_merge`'s output covers, for each `unitRef`, exactly the
    union of the offsets covered by that `unitRef`'s regex, Guardrails and LLM
    input spans — no offset is dropped and none is invented. Concatenation order
    matches the pipeline: regex + guardrails + llm, LLM last.

    Merging is additive — coverage is
    the union of all three sources
    """
    regex_spans, guardrails_spans, llm_spans = sources
    combined = regex_spans + guardrails_spans + llm_spans

    merged = orchestration._regroup_and_merge(combined)

    assert _covered_offsets(merged) == _covered_offsets(combined)
    # Additivity must not be bought with overlapping output: the union coverage
    # is still delivered as disjoint spans per `unitRef`.
    _assert_no_overlaps_within_unit_ref(merged)


@settings(max_examples=200)
@given(sources=_three_source_spans())
def test_property_9_additive_llm_contribution_only_grows_coverage(sources):
    """The specific additive-only guarantee this feature rests on: adding the
    LLM spans never *reduces* coverage relative to the two-source
    (regex + Guardrails) merge, and the three-source coverage equals the
    two-source coverage unioned with the LLM spans' own coverage. This is the
    merge-layer encoding of "the LLM may only add".

    Merging is additive — coverage is
    the union of all three sources
    """
    regex_spans, guardrails_spans, llm_spans = sources

    two_source = orchestration._regroup_and_merge(regex_spans + guardrails_spans)
    three_source = orchestration._regroup_and_merge(
        regex_spans + guardrails_spans + llm_spans
    )

    two_source_coverage = _covered_offsets(two_source)
    three_source_coverage = _covered_offsets(three_source)

    # LLM is additive-only: it can only grow the covered-offset set.
    assert three_source_coverage >= two_source_coverage
    # And it grows it by exactly the LLM spans' own coverage — nothing else moves.
    assert three_source_coverage == two_source_coverage | _covered_offsets(llm_spans)


# ---------------------------------------------------------------------------
# Example-based checks for additive merging (concrete cases).
# ---------------------------------------------------------------------------


def test_property_9_additive_example_llm_only_span_survives_as_own_region():
    """A genuinely LLM-only contribution — an `"llm:CN_RESIDENT_ID_NUMBER"` span
    in a `unitRef` no other source touched — survives merging as its own region,
    unchanged and single-source (`mergedFromRuleNames is None`)."""
    regex_spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=0, endOffset=4, ruleName="digits3"),
    ]
    guardrails_spans = [
        MatchSpan(
            zone="body",
            unitRef="text:0",
            startOffset=10,
            endOffset=14,
            ruleName="guardrails:NAME",
        ),
    ]
    llm_spans = [
        MatchSpan(
            zone="body",
            unitRef="text:1",
            startOffset=2,
            endOffset=20,
            ruleName="llm:CN_RESIDENT_ID_NUMBER",
        ),
    ]

    merged = orchestration._regroup_and_merge(
        regex_spans + guardrails_spans + llm_spans
    )

    llm_only = [span for span in merged if span.unitRef == "text:1"]
    assert len(llm_only) == 1
    assert (llm_only[0].startOffset, llm_only[0].endOffset) == (2, 20)
    assert llm_only[0].ruleName == "llm:CN_RESIDENT_ID_NUMBER"
    assert llm_only[0].mergedFromRuleNames is None
    # The LLM-only region genuinely added coverage the other two sources lacked.
    assert ("text:1", 5) in _covered_offsets(merged)
    _assert_no_overlaps_within_unit_ref(merged)


def test_property_9_additive_example_llm_overlapping_guardrails_merges_llm_last():
    """An `"llm:NAME"` span overlapping a `"guardrails:NAME"` span in the same
    `unitRef` merges into one region covering the union of both ranges, carrying
    both names in `mergedFromRuleNames` with the `"llm:"` name last (LLM spans
    are concatenated last, so an existing source keeps the scalar `ruleName`)."""
    guardrails_spans = [
        MatchSpan(
            zone="body",
            unitRef="text:0",
            startOffset=4,
            endOffset=10,
            ruleName="guardrails:NAME",
        ),
    ]
    llm_spans = [
        MatchSpan(zone="body", unitRef="text:0", startOffset=8, endOffset=16, ruleName="llm:NAME"),
    ]

    merged = orchestration._regroup_and_merge(guardrails_spans + llm_spans)

    assert len(merged) == 1
    assert (merged[0].startOffset, merged[0].endOffset) == (4, 16)
    assert merged[0].ruleName == "guardrails:NAME"
    assert merged[0].mergedFromRuleNames == ["guardrails:NAME", "llm:NAME"]
    # Coverage is the union of the two input ranges — the LLM span extended it.
    assert _covered_offsets(merged) == _covered_offsets(guardrails_spans + llm_spans)
    _assert_no_overlaps_within_unit_ref(merged)
