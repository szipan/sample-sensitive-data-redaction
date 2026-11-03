"""Tests for `agent/tools/guardrails_pii_detect.py`
(bedrock-guardrails-pii-integration design.md "Component 2: Guardrails PII
Detection Tool").

This module is kept free of PDF-library/`python-docx` imports so it runs on
developer machines where those native dependencies are unavailable — the
tool under test only depends on `shared.models` and (lazily) boto3, and the
boto3 client is always replaced with an in-test fake here.
"""
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import os  # noqa: E402

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent.tools import guardrails_pii_detect  # noqa: E402
from agent.tools.guardrails_pii_detect import detect_pii_with_guardrails  # noqa: E402
from shared.models import ExtractedTextUnit, ImageRegion, TextPosition  # noqa: E402

TEXT_UNIT_ZONES = ("body", "table", "header", "footer")


class FakeGuardrailClient:
    """Stand-in for the `bedrock-runtime` client.

    Returns a canned `ApplyGuardrail` response whose `piiEntities[]` are
    looked up by the exact text that was sent to the API, mirroring the
    response shape documented in design.md ("ApplyGuardrail API Response").
    """

    def __init__(self, matches_by_text):
        self.matches_by_text = matches_by_text
        self.calls = []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs["content"][0]["text"]["text"]
        entities = [
            {"type": pii_type, "match": match, "action": "BLOCKED"}
            for pii_type, match in self.matches_by_text.get(text, [])
        ]
        return {
            "action": "GUARDRAIL_INTERVENED" if entities else "NONE",
            "assessments": [{"sensitiveInformationPolicy": {"piiEntities": entities}}],
        }


@contextmanager
def guardrails_configured(client, guardrail_id="gr-test", guardrail_version="1"):
    """Set the Guardrail env vars and inject `client` as the module's
    lazily-created `bedrock-runtime` client, restoring both afterwards.

    Implemented as a context manager rather than a pytest fixture so it can
    be used inside Hypothesis-driven tests without function-scoped-fixture
    reuse across examples.
    """
    previous_client = guardrails_pii_detect._bedrock_runtime_client
    previous_env = {
        key: os.environ.get(key)
        for key in (
            guardrails_pii_detect.GUARDRAIL_ID_ENV_VAR,
            guardrails_pii_detect.GUARDRAIL_VERSION_ENV_VAR,
        )
    }
    guardrails_pii_detect._bedrock_runtime_client = client
    os.environ[guardrails_pii_detect.GUARDRAIL_ID_ENV_VAR] = guardrail_id
    os.environ[guardrails_pii_detect.GUARDRAIL_VERSION_ENV_VAR] = guardrail_version
    try:
        yield client
    finally:
        guardrails_pii_detect._bedrock_runtime_client = previous_client
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Offset validity for all guardrails spans
# ---------------------------------------------------------------------------


@st.composite
def _text_with_substring_matches(draw):
    """Draw `(text, matches)` where every `match` is a real substring of `text`.

    This models the well-behaved case: `ApplyGuardrail` reports matched text
    that genuinely occurs in the content that was sent to it.
    """
    text = draw(st.text(min_size=1, max_size=60))
    match_count = draw(st.integers(min_value=0, max_value=3))
    matches = []
    for _ in range(match_count):
        start = draw(st.integers(min_value=0, max_value=len(text) - 1))
        end = draw(st.integers(min_value=start + 1, max_value=len(text)))
        pii_type = draw(st.sampled_from(["NAME", "EMAIL", "PHONE", "ADDRESS", "SSN"]))
        matches.append((pii_type, text[start:end]))
    return text, matches


@st.composite
def _text_with_arbitrary_matches(draw):
    """Draw `(text, matches)` where `match` may or may not occur in `text`.

    This models the adversarial case: a match the API reports that cannot be
    located in the source text must not become a span with bogus offsets.
    """
    text = draw(st.text(min_size=1, max_size=40))
    matches = draw(
        st.lists(
            st.tuples(
                st.sampled_from(["NAME", "EMAIL", "PHONE"]),
                st.text(min_size=1, max_size=10),
            ),
            max_size=3,
        )
    )
    return text, matches


def _text_units_and_client(samples):
    """Build `ExtractedTextUnit`s plus a fake client wired to `samples`."""
    matches_by_text = {}
    units = []
    for index, (zone, (text, matches)) in enumerate(samples):
        units.append(
            ExtractedTextUnit(zone=zone, text=text, position=TextPosition(index=index))
        )
        matches_by_text.setdefault(text, []).extend(matches)
    return units, FakeGuardrailClient(matches_by_text)


def _assert_offsets_valid(spans, source_text_by_unit_ref):
    for span in spans:
        source_text = source_text_by_unit_ref[span.unitRef]
        assert 0 <= span.startOffset, span
        assert span.startOffset < span.endOffset, span
        assert span.endOffset <= len(source_text), span


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(st.sampled_from(TEXT_UNIT_ZONES), _text_with_substring_matches()),
        min_size=1,
        max_size=4,
    )
)
def test_property_2_offset_validity_for_text_unit_spans(samples):
    """Every span from a text unit satisfies 0 <= start < end <= len(text),
    and slices back to exactly the reported match text.
    """
    units, client = _text_units_and_client(samples)

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails(units, [])

    source_text_by_unit_ref = {f"text:{i}": unit.text for i, unit in enumerate(units)}
    _assert_offsets_valid(spans, source_text_by_unit_ref)
    for span in spans:
        source_text = source_text_by_unit_ref[span.unitRef]
        matched = source_text[span.startOffset : span.endOffset]
        pii_type = span.ruleName.split(":", 1)[1]
        reported = [
            match
            for reported_type, match in client.matches_by_text[source_text]
            if reported_type == pii_type
        ]
        assert matched in reported, (span, reported)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(st.sampled_from(TEXT_UNIT_ZONES), _text_with_arbitrary_matches()),
        min_size=1,
        max_size=4,
    )
)
def test_property_2_offset_validity_with_matches_absent_from_source(samples):
    """Offset validity holds even when the API reports match text that does
    not occur in the source: such detections are skipped rather than turned
    into out-of-range spans.
    """
    units, client = _text_units_and_client(samples)

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails(units, [])

    source_text_by_unit_ref = {f"text:{i}": unit.text for i, unit in enumerate(units)}
    _assert_offsets_valid(spans, source_text_by_unit_ref)
    for span in spans:
        source_text = source_text_by_unit_ref[span.unitRef]
        matched = source_text[span.startOffset : span.endOffset]
        assert matched in source_text


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_text_with_substring_matches(), min_size=1, max_size=4))
def test_property_2_offset_validity_for_image_region_spans(samples):
    """Spans derived from `ImageRegion.recognizedText` satisfy the same
    offset bounds, measured against the recognized text.
    """
    matches_by_text = {}
    regions = []
    for index, (text, matches) in enumerate(samples):
        regions.append(
            ImageRegion(
                zone="image",
                sourceLocation=f"page-1-image-{index}",
                recognizedText=text,
            )
        )
        matches_by_text.setdefault(text, []).extend(matches)
    client = FakeGuardrailClient(matches_by_text)

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([], regions)

    source_text_by_unit_ref = {
        f"image:{i}": region.recognizedText for i, region in enumerate(regions)
    }
    _assert_offsets_valid(spans, source_text_by_unit_ref)
    for span in spans:
        source_text = source_text_by_unit_ref[span.unitRef]
        assert source_text[span.startOffset : span.endOffset] in source_text


def test_property_2_offset_validity_example_known_offsets():
    """Example-level check of the same property with hand-computed offsets."""
    text = "联系 张三，邮箱 zhangsan@example.com"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient(
        {text: [("NAME", "张三"), ("EMAIL", "zhangsan@example.com")]}
    )

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert len(spans) == 2
    for span in spans:
        assert 0 <= span.startOffset < span.endOffset <= len(text)
    name_span, email_span = spans
    assert text[name_span.startOffset : name_span.endOffset] == "张三"
    assert text[email_span.startOffset : email_span.endOffset] == "zhangsan@example.com"


# ---------------------------------------------------------------------------
# Guardrails spans use correct ruleName format
# ---------------------------------------------------------------------------

RULE_NAME_PREFIX = "guardrails:"

#: A representative slice of the PII entity types the CDK stack enables
#: (design.md "Component 1"), deliberately including the long
#: underscore-heavy ones most likely to be truncated or reformatted by a
#: buggy implementation.
REALISTIC_PII_TYPES = (
    "NAME",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "PIN",
    "AGE",
    "DRIVER_ID",
    "IP_ADDRESS",
    "AWS_SECRET_KEY",
    "CREDIT_DEBIT_CARD_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
    "UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "VEHICLE_IDENTIFICATION_NUMBER",
)

#: The tool copies the API's `type` string through verbatim, so the property
#: must hold for entity types it has never seen — including lowercase,
#: colon-containing and non-ASCII strings a future Guardrails release (or a
#: misbehaving API) could return.
_pii_types = st.one_of(
    st.sampled_from(REALISTIC_PII_TYPES),
    st.text(min_size=1, max_size=24),
)


@st.composite
def _text_with_typed_matches(draw):
    """Draw `(text, matches, type_by_token)` for a single text unit.

    Each drawn PII type is paired with its own unique, unambiguous token
    (`"T0X"`, `"T1X"`, ...) embedded in `text`. Unique tokens mean every
    detection is locatable via `str.find()` *and* that a resulting span's
    matched slice identifies exactly which reported entity produced it — so
    the test can check each span's `ruleName` against the right type rather
    than relying on output ordering.
    """
    pii_types = draw(st.lists(_pii_types, min_size=1, max_size=4))
    tokens = [f"T{index}X" for index in range(len(pii_types))]
    text = "PII: " + " | ".join(tokens) + " end"
    matches = list(zip(pii_types, tokens))
    return text, matches, dict(zip(tokens, pii_types))


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.sampled_from(TEXT_UNIT_ZONES),
    _text_with_typed_matches(),
)
def test_property_1_rulename_round_trips_api_pii_type(zone, sample):
    """Every span's `ruleName` is exactly `"guardrails:{PII_TYPE}"`, where
    `PII_TYPE` is the type string the API reported for that detection —
    byte-for-byte, with no case change and no truncation.
    """
    text, matches, type_by_token = sample
    unit = ExtractedTextUnit(zone=zone, text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: matches})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert len(spans) == len(matches)
    for span in spans:
        token = text[span.startOffset : span.endOffset]
        expected_type = type_by_token[token]
        assert span.ruleName == f"{RULE_NAME_PREFIX}{expected_type}", span


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(st.sampled_from(TEXT_UNIT_ZONES), _text_with_substring_matches()),
        min_size=1,
        max_size=4,
    )
)
def test_property_1_rulename_always_carries_guardrails_prefix(samples):
    """No matter how many units/detections are involved, every returned span
    carries the `"guardrails:"` prefix followed by a non-empty type — the
    marker that distinguishes ML detections from regex rule names once both
    sources are concatenated and merged.
    """
    units, client = _text_units_and_client(samples)

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails(units, [])

    for span in spans:
        assert span.ruleName.startswith(RULE_NAME_PREFIX), span
        assert span.ruleName[len(RULE_NAME_PREFIX) :], span


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_text_with_typed_matches())
def test_property_1_rulename_round_trips_for_image_region_spans(sample):
    """The same `ruleName` format holds for spans derived from an
    `ImageRegion`'s `recognizedText`, not just from `ExtractedTextUnit`s.
    """
    text, matches, type_by_token = sample
    region = ImageRegion(
        zone="image", sourceLocation="page-1-image-0", recognizedText=text
    )
    client = FakeGuardrailClient({text: matches})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([], [region])

    assert len(spans) == len(matches)
    for span in spans:
        token = text[span.startOffset : span.endOffset]
        assert span.ruleName == f"{RULE_NAME_PREFIX}{type_by_token[token]}", span


def test_property_1_rulename_for_mixed_pii_types_in_one_text_unit():
    """Example-level check: several different PII types detected in a single
    text unit each get their own correctly-typed `ruleName`.
    """
    text = "张三 zhangsan@example.com 4111111111111111 123-45-6789"
    matches = [
        ("NAME", "张三"),
        ("EMAIL", "zhangsan@example.com"),
        ("CREDIT_DEBIT_CARD_NUMBER", "4111111111111111"),
        ("US_SOCIAL_SECURITY_NUMBER", "123-45-6789"),
    ]
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: matches})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert [span.ruleName for span in spans] == [
        "guardrails:NAME",
        "guardrails:EMAIL",
        "guardrails:CREDIT_DEBIT_CARD_NUMBER",
        "guardrails:US_SOCIAL_SECURITY_NUMBER",
    ]
    for span, (_, match_text) in zip(spans, matches):
        assert text[span.startOffset : span.endOffset] == match_text


def test_property_1_rulename_preserves_unusual_pii_types_verbatim():
    """Example-level check that the type string is passed through untouched:
    no upper/lower-casing, no truncation, no separator rewriting.
    """
    unusual_types = [
        "us_social_security_number",
        "MixedCase_Type",
        "TYPE-WITH-DASHES",
        "TYPE:WITH:COLONS",
        "A" * 120,
        "身份证号",
    ]
    for index, pii_type in enumerate(unusual_types):
        text = f"value-{index} here"
        match_text = f"value-{index}"
        unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
        client = FakeGuardrailClient({text: [(pii_type, match_text)]})

        with guardrails_configured(client):
            spans = detect_pii_with_guardrails([unit], [])

        assert len(spans) == 1
        assert spans[0].ruleName == f"guardrails:{pii_type}"
        # The part after the prefix round-trips to the API's type exactly.
        assert spans[0].ruleName[len(RULE_NAME_PREFIX) :] == pii_type

# ---------------------------------------------------------------------------
# Empty text units produce no spans
# ---------------------------------------------------------------------------


def _texts_sent_to_api(client):
    """The exact text strings that reached `ApplyGuardrail`, in call order."""
    return [call["content"][0]["text"]["text"] for call in client.calls]


#: An "empty" text unit is one whose `text` is `""`. Whitespace-only text is
#: deliberately *not* treated as empty (only an empty string counts as empty content),
#: so it is drawn as a normal non-empty value below and is expected to be
#: sent to the API like any other content.
_maybe_empty_text = st.one_of(
    st.just(""),
    st.text(min_size=1, max_size=30),
    st.sampled_from([" ", "\n", "\t ", "张三", "a@b.com"]),
)


@st.composite
def _mixed_emptiness_units(draw):
    """Draw `(units, matches_by_text)` for a mix of empty and non-empty units.

    Every non-empty unit gets a locatable detection so the property has
    something to *not* find for the empty ones; empty units are additionally
    wired with a detection in the fake client, so that a buggy
    implementation which called the API for `""` would be caught by the
    span assertions as well as by the call-log assertions.
    """
    texts = draw(st.lists(_maybe_empty_text, min_size=1, max_size=6))
    units = []
    matches_by_text = {"": [("NAME", "ghost")]}
    for index, text in enumerate(texts):
        zone = draw(st.sampled_from(TEXT_UNIT_ZONES))
        units.append(
            ExtractedTextUnit(zone=zone, text=text, position=TextPosition(index=index))
        )
        if text:
            matches_by_text.setdefault(text, []).append(("NAME", text))
    return units, matches_by_text


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_mixed_emptiness_units())
def test_property_6_empty_units_are_never_sent_to_the_api(sample):
    """`ApplyGuardrail` is invoked exactly once per non-empty text unit, with
    that unit's text, and never for an empty one.
    """
    units, matches_by_text = sample
    client = FakeGuardrailClient(matches_by_text)

    with guardrails_configured(client):
        detect_pii_with_guardrails(units, [])

    assert _texts_sent_to_api(client) == [unit.text for unit in units if unit.text]
    assert "" not in _texts_sent_to_api(client)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_mixed_emptiness_units())
def test_property_6_empty_units_produce_no_spans(sample):
    """No returned span references an empty text unit.
    """
    units, matches_by_text = sample
    client = FakeGuardrailClient(matches_by_text)

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails(units, [])

    empty_refs = {f"text:{i}" for i, unit in enumerate(units) if not unit.text}
    for span in spans:
        assert span.unitRef not in empty_refs, span


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(TEXT_UNIT_ZONES), min_size=1, max_size=8))
def test_property_6_empty_units_all_empty_yields_no_calls_and_no_spans(zones):
    """When *every* text unit is empty, the tool makes zero API calls and
    returns an empty result.
    """
    units = [
        ExtractedTextUnit(zone=zone, text="", position=TextPosition(index=index))
        for index, zone in enumerate(zones)
    ]
    client = FakeGuardrailClient({"": [("NAME", "ghost")]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails(units, [])

    assert client.calls == []
    assert spans == []


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(st.booleans(), min_size=1, max_size=8).filter(
        lambda flags: not all(flags)
    ),
    st.sampled_from(TEXT_UNIT_ZONES),
)
def test_property_6_empty_units_do_not_shift_unitref_indices(is_empty_flags, zone):
    """Skipping empty units must not renumber the surviving ones: a span's
    `unitRef` index is the unit's position in the *input* list, not its
    position among the non-empty units.

    Each non-empty unit carries a token unique to its index, so a span's
    matched slice proves which input unit it really came from.
    """
    units = []
    matches_by_text = {"": [("NAME", "ghost")]}
    for index, is_empty in enumerate(is_empty_flags):
        text = "" if is_empty else f"unit-{index} token-{index} end"
        units.append(
            ExtractedTextUnit(zone=zone, text=text, position=TextPosition(index=index))
        )
        if text:
            matches_by_text[text] = [("NAME", f"token-{index}")]
    client = FakeGuardrailClient(matches_by_text)

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails(units, [])

    expected_indices = [i for i, is_empty in enumerate(is_empty_flags) if not is_empty]
    assert [span.unitRef for span in spans] == [
        f"text:{i}" for i in expected_indices
    ]
    for span, index in zip(spans, expected_indices):
        source_text = units[index].text
        assert source_text[span.startOffset : span.endOffset] == f"token-{index}"


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.sampled_from(["", None, "has-text"]),
        min_size=1,
        max_size=6,
    ).filter(lambda values: any(value == "has-text" for value in values))
)
def test_property_6_empty_units_image_regions_without_recognized_text_are_skipped(
    recognized_values,
):
    """`ImageRegion`s whose `recognizedText` is `""` *or* `None` (OCR not run
    / found nothing) are skipped with no API call and no span, and the
    surviving regions keep their original `image:{index}` refs.
    """
    regions = []
    matches_by_text = {"": [("NAME", "ghost")]}
    for index, value in enumerate(recognized_values):
        recognized = None if value is None else (value and f"img-{index} token-{index}")
        regions.append(
            ImageRegion(
                zone="image",
                sourceLocation=f"page-1-image-{index}",
                recognizedText=recognized,
            )
        )
        if recognized:
            matches_by_text[recognized] = [("NAME", f"token-{index}")]
    client = FakeGuardrailClient(matches_by_text)

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([], regions)

    expected_indices = [
        i for i, region in enumerate(regions) if region.recognizedText
    ]
    assert _texts_sent_to_api(client) == [
        regions[i].recognizedText for i in expected_indices
    ]
    assert [span.unitRef for span in spans] == [
        f"image:{i}" for i in expected_indices
    ]
    for span, index in zip(spans, expected_indices):
        source_text = regions[index].recognizedText
        assert source_text[span.startOffset : span.endOffset] == f"token-{index}"


def test_property_6_empty_units_example_interleaved_indices_preserved():
    """Example-level regression check for the index-shifting bug: units 0 and
    2 are empty, unit 1 is not, so the single span must be `"text:1"`.
    """
    units = [
        ExtractedTextUnit(zone="body", text="", position=TextPosition(index=0)),
        ExtractedTextUnit(
            zone="table", text="联系 张三", position=TextPosition(index=1)
        ),
        ExtractedTextUnit(zone="footer", text="", position=TextPosition(index=2)),
    ]
    client = FakeGuardrailClient({"联系 张三": [("NAME", "张三")]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails(units, [])

    assert len(client.calls) == 1
    assert _texts_sent_to_api(client) == ["联系 张三"]
    assert len(spans) == 1
    assert spans[0].unitRef == "text:1"
    assert spans[0].zone == "table"
    assert units[1].text[spans[0].startOffset : spans[0].endOffset] == "张三"


def test_property_6_empty_units_example_mixed_text_units_and_image_regions():
    """Example-level check that empty-unit skipping is independent per
    collection: empty text units do not disturb `image:{index}` refs and
    vice versa.
    """
    units = [
        ExtractedTextUnit(zone="body", text="", position=TextPosition(index=0)),
        ExtractedTextUnit(
            zone="body", text="email a@b.com", position=TextPosition(index=1)
        ),
    ]
    regions = [
        ImageRegion(zone="image", sourceLocation="p1-i0", recognizedText=None),
        ImageRegion(zone="image", sourceLocation="p1-i1", recognizedText=""),
        ImageRegion(zone="image", sourceLocation="p1-i2", recognizedText="scan 李四"),
    ]
    client = FakeGuardrailClient(
        {
            "email a@b.com": [("EMAIL", "a@b.com")],
            "scan 李四": [("NAME", "李四")],
            "": [("NAME", "ghost")],
        }
    )

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails(units, regions)

    assert _texts_sent_to_api(client) == ["email a@b.com", "scan 李四"]
    assert [(span.unitRef, span.ruleName) for span in spans] == [
        ("text:1", "guardrails:EMAIL"),
        ("image:2", "guardrails:NAME"),
    ]


# ---------------------------------------------------------------------------
# Offset recovery: every non-overlapping occurrence of the match text, minus
# the ones the digit-boundary rule rejects.
#
# `ApplyGuardrail` reports matched *text*, not offsets. Resolving only the
# first occurrence silently mis-places short matches, leaving the real PII
# unredacted; emitting *every* occurrence unconditionally over-redacts every
# digit run that happens to contain the match. When the match text is
# entirely ASCII digits, occurrences flanked by a digit on either side are
# therefore rejected — with a fallback to the unfiltered set if that would
# leave nothing (design.md "Offset Calculation: Handling Duplicate
# Substrings").
# ---------------------------------------------------------------------------

CHI_DOCX_CARD_NUMBER = "6222021234567890123"
CHI_DOCX_CVV = "456"
CHI_DOCX_AGE = "34"
CHI_DOCX_PLATE = "京A·12345"


def test_all_occurrences_chi_docx_cvv_survives_earlier_card_number():
    """Regression for the `chi.docx` under-redaction.

    Guardrails detects the CVV `456`. The bank card number
    `6222021234567890123` earlier in the same text unit contains `456` as a
    substring (index 9), so resolving only the first occurrence drew the span
    *inside the card number* — invisible, because the card number is
    separately redacted — and the real CVV survived into the output.

    With the digit-boundary rule the decoy is rejected (it is preceded by
    `3`) while the real CVV, which ends the unit and is preceded by `码`, is
    kept: exactly ONE span, on the real value. That single-span outcome is
    the refinement over emitting both — the decoy span was pure
    over-redaction.
    """
    text = f"银行卡号 {CHI_DOCX_CARD_NUMBER}，有效期 12/26，CVV码{CHI_DOCX_CVV}"
    decoy_offset = text.index(CHI_DOCX_CARD_NUMBER) + 9
    real_cvv_offset = text.index("CVV码") + len("CVV码")
    # Precondition of the bug: the decoy really does precede the real value.
    assert text[decoy_offset : decoy_offset + 3] == CHI_DOCX_CVV
    assert decoy_offset < real_cvv_offset
    # Precondition of the fix: the decoy is digit-flanked, the real one is not.
    assert text[decoy_offset - 1] == "3"
    assert text[real_cvv_offset - 1] == "码"
    assert real_cvv_offset + 3 == len(text)

    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient(
        {text: [("CREDIT_DEBIT_CARD_CVV", CHI_DOCX_CVV)]}
    )

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert [(span.startOffset, span.endOffset) for span in spans] == [
        (real_cvv_offset, real_cvv_offset + 3)
    ]
    span = spans[0]
    assert span.unitRef == "text:0"
    assert span.zone == "body"
    assert span.ruleName == "guardrails:CREDIT_DEBIT_CARD_CVV"
    assert text[span.startOffset : span.endOffset] == CHI_DOCX_CVV


def test_digit_boundary_chi_docx_age_skips_card_number_and_plate_decoys():
    """Regression for the over-redaction introduced by unconditional
    all-occurrences: a detected AGE `34` blacked out every `34` in the unit.

    The unit holds three occurrences of `34`: inside the card number
    `6222021234567890123` (index 8, between `2` and `5`), inside the licence
    plate `京A·12345` (index 5 of the plate, preceded by `2`) and the real age
    in `今年34 岁` (preceded by `年`, followed by a space). Only the last is
    kept.
    """
    text = (
        f"车牌 {CHI_DOCX_PLATE}，银行卡号 {CHI_DOCX_CARD_NUMBER}，"
        f"今年{CHI_DOCX_AGE} 岁"
    )
    plate_decoy = text.index(CHI_DOCX_PLATE) + 5
    card_decoy = text.index(CHI_DOCX_CARD_NUMBER) + 8
    real_age = text.index("今年") + len("今年")
    # Preconditions: all three really are occurrences of "34", in this order.
    for offset in (plate_decoy, card_decoy, real_age):
        assert text[offset : offset + 2] == CHI_DOCX_AGE
    assert plate_decoy < card_decoy < real_age
    # ...and only the real one is free of ASCII-digit neighbours.
    assert text[plate_decoy - 1] == "2" and text[plate_decoy + 2] == "5"
    assert text[card_decoy - 1] == "2" and text[card_decoy + 2] == "5"
    assert text[real_age - 1] == "年" and text[real_age + 2] == " "

    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("AGE", CHI_DOCX_AGE)]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert [(span.startOffset, span.endOffset) for span in spans] == [
        (real_age, real_age + 2)
    ]
    assert spans[0].ruleName == "guardrails:AGE"
    assert text[spans[0].startOffset : spans[0].endOffset] == CHI_DOCX_AGE


def test_digit_boundary_rule_does_not_apply_to_non_numeric_match_text():
    """The rule is scoped to all-digit match text: a repeated name still
    yields every occurrence, even where one sits against digits.
    """
    text = "张三 与 张三 是同一人，工号12张三34"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("NAME", "张三")]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    expected = []
    position = 0
    while True:
        start = text.find("张三", position)
        if start == -1:
            break
        expected.append((start, start + 2))
        position = start + 2
    # Three occurrences, one of them deliberately digit-flanked ("12张三34").
    assert len(expected) == 3
    assert [(span.startOffset, span.endOffset) for span in spans] == expected
    for span in spans:
        assert text[span.startOffset : span.endOffset] == "张三"
        assert span.ruleName == "guardrails:NAME"


def test_digit_boundary_rule_does_not_apply_to_match_text_with_separators():
    """An SSN-shaped match (`123-45-6789`) is not all digits, so the rule does
    not apply and every occurrence is emitted — including one embedded in a
    longer digit run.
    """
    ssn = "123-45-6789"
    text = f"SSN {ssn} / ref 9{ssn}0 / again {ssn}"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("US_SOCIAL_SECURITY_NUMBER", ssn)]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert len(spans) == 3
    middle = spans[1]
    # The middle occurrence is digit-flanked on both sides and still emitted.
    assert text[middle.startOffset - 1] == "9"
    assert text[middle.endOffset] == "0"
    for span in spans:
        assert text[span.startOffset : span.endOffset] == ssn
        assert span.ruleName == "guardrails:US_SOCIAL_SECURITY_NUMBER"


def test_digit_boundary_single_occurrence_not_flanked_is_unchanged():
    """A digit-only match occurring exactly once, with non-digit neighbours,
    is kept as-is — the rule must not perturb the ordinary case.
    """
    text = "手机号 13800138000 仅此一处"
    phone = "13800138000"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("PHONE", phone)]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert len(spans) == 1
    assert spans[0].startOffset == text.index(phone)
    assert text[spans[0].startOffset : spans[0].endOffset] == phone
    assert spans[0].ruleName == "guardrails:PHONE"


def test_digit_boundary_all_rejected_falls_back_to_every_occurrence(caplog):
    """Mandatory safety fallback: when the rule rejects *every* occurrence,
    all of them are emitted rather than none — dropping the detection would
    recreate the silent under-redaction the scan exists to eliminate — and
    the fallback is logged distinguishably from an ordinary narrowing.
    """
    text = "1234567 和 9345"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("AGE", "34")]})

    # Precondition: both occurrences of "34" are digit-flanked.
    assert text.find("34") == 2 and text[1] == "2" and text[4] == "5"
    second = text.find("34", 4)
    assert text[second - 1] == "9" and text[second + 2] == "5"

    with caplog.at_level(logging.DEBUG, logger=guardrails_pii_detect.__name__):
        with guardrails_configured(client):
            spans = detect_pii_with_guardrails([unit], [])

    assert [(span.startOffset, span.endOffset) for span in spans] == [
        (2, 4),
        (second, second + 2),
    ]
    fallback_records = [
        record
        for record in caplog.records
        if "rejected all" in record.message and "fallback" in record.message
    ]
    assert fallback_records, [record.message for record in caplog.records]
    assert fallback_records[0].levelno == logging.WARNING
    # Distinguishable from the "kept some" message, which never appears here.
    assert not [
        record for record in caplog.records if "kept" in record.message
    ]


def test_digit_boundary_partial_rejection_is_logged_distinguishably(caplog):
    """The narrowing case logs "kept N of M" at debug — the counterpart the
    operator must be able to tell apart from the all-rejected fallback.
    """
    text = "1234567 和 年龄34岁"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("AGE", "34")]})

    with caplog.at_level(logging.DEBUG, logger=guardrails_pii_detect.__name__):
        with guardrails_configured(client):
            spans = detect_pii_with_guardrails([unit], [])

    assert len(spans) == 1
    assert text[spans[0].startOffset : spans[0].endOffset] == "34"
    assert spans[0].startOffset == text.index("年龄") + len("年龄")
    kept_records = [record for record in caplog.records if "kept 1 of 2" in record.message]
    assert kept_records, [record.message for record in caplog.records]
    assert not [
        record for record in caplog.records if "rejected all" in record.message
    ]


def test_all_occurrences_single_occurrence_yields_exactly_one_span():
    """A match text occurring exactly once still yields exactly one span, at
    that offset — unchanged from the single-`find()` behavior.
    """
    text = "邮箱 zhangsan@example.com 是唯一联系方式"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("EMAIL", "zhangsan@example.com")]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert len(spans) == 1
    assert spans[0].startOffset == text.index("zhangsan@example.com")
    assert text[spans[0].startOffset : spans[0].endOffset] == "zhangsan@example.com"
    assert spans[0].ruleName == "guardrails:EMAIL"


def test_all_occurrences_match_absent_from_text_yields_no_span():
    """A reported match that does not occur in the text at all is skipped
    defensively — no span, no raise.
    """
    text = "没有敏感信息"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("NAME", "王五"), ("EMAIL", "a@b.com")]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert spans == []


def test_all_occurrences_are_non_overlapping():
    """Occurrences are found by advancing past each hit, so emitted spans
    never overlap each other: match `"aa"` in `"aaaa"` yields 0-2 and 2-4,
    not 0-2, 1-3, 2-4.
    """
    text = "aaaa"
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("NAME", "aa")]})

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert [(span.startOffset, span.endOffset) for span in spans] == [(0, 2), (2, 4)]
    ordered = sorted(spans, key=lambda span: span.startOffset)
    for earlier, later in zip(ordered, ordered[1:]):
        assert earlier.endOffset <= later.startOffset, (earlier, later)
    for span in spans:
        assert 0 <= span.startOffset < span.endOffset <= len(text)


def test_all_occurrences_repeated_pii_across_zones_and_ocr_units():
    """Every occurrence is covered in each collection the tool scans — text
    units, image regions and OCR units alike — with the right `unitRef`.
    """
    text = "张三 与 张三 是同一人"
    ocr_text = "扫描件：张三 张三"
    unit = ExtractedTextUnit(zone="table", text=text, position=TextPosition(index=0))
    region = ImageRegion(
        zone="image", sourceLocation="p1-i0", recognizedText=text
    )
    ocr_unit = ExtractedTextUnit(
        zone="ocr", text=ocr_text, position=TextPosition(index=0)
    )
    client = FakeGuardrailClient(
        {text: [("NAME", "张三")], ocr_text: [("NAME", "张三")]}
    )

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [region], [ocr_unit])

    by_ref = {}
    for span in spans:
        by_ref.setdefault(span.unitRef, []).append(span)
    assert sorted(by_ref) == ["image:0", "ocr:0", "text:0"]
    assert len(by_ref["text:0"]) == 2
    assert len(by_ref["image:0"]) == 2
    assert len(by_ref["ocr:0"]) == 2
    assert by_ref["ocr:0"][0].zone == "ocr"
    for ref, source in (("text:0", text), ("image:0", text), ("ocr:0", ocr_text)):
        for span in by_ref[ref]:
            assert source[span.startOffset : span.endOffset] == "张三"


def test_all_occurrences_high_count_is_warned_but_not_capped(caplog):
    """An unusually high occurrence count is logged at warning level so the
    over-redaction is observable — but never capped, since a cap would
    silently reintroduce the under-redaction being fixed.
    """
    occurrences = guardrails_pii_detect.OCCURRENCE_WARN_THRESHOLD + 5
    text = "8" * occurrences
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))
    client = FakeGuardrailClient({text: [("PIN", "8")]})

    with caplog.at_level(logging.WARNING, logger=guardrails_pii_detect.__name__):
        with guardrails_configured(client):
            spans = detect_pii_with_guardrails([unit], [])

    assert len(spans) == occurrences
    assert [(span.startOffset, span.endOffset) for span in spans] == [
        (i, i + 1) for i in range(occurrences)
    ]
    assert any(
        "occurrences" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_all_occurrences_never_raises_when_the_api_fails():
    """The never-raise posture is intact: an `ApplyGuardrail` failure costs
    that unit's spans and nothing more.
    """
    good_text = "邮箱 a@b.com"
    failing_unit = ExtractedTextUnit(
        zone="body", text="卡号 6222021234567890123", position=TextPosition(index=0)
    )
    good_unit = ExtractedTextUnit(
        zone="body", text=good_text, position=TextPosition(index=1)
    )

    client = mock.MagicMock()

    def apply_guardrail(**kwargs):
        text = kwargs["content"][0]["text"]["text"]
        if text == good_text:
            return {
                "action": "GUARDRAIL_INTERVENED",
                "assessments": [
                    {
                        "sensitiveInformationPolicy": {
                            "piiEntities": [{"type": "EMAIL", "match": "a@b.com"}]
                        }
                    }
                ],
            }
        raise RuntimeError("boom")

    client.apply_guardrail.side_effect = apply_guardrail

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([failing_unit, good_unit], [])

    # Partial results are kept: the surviving unit's span is still returned.
    assert [(span.unitRef, span.ruleName) for span in spans] == [
        ("text:1", "guardrails:EMAIL")
    ]


def test_all_occurrences_never_raises_on_malformed_response():
    """A response shape the tool cannot parse degrades to zero spans rather
    than propagating an exception.
    """
    unit = ExtractedTextUnit(
        zone="body", text="卡号 6222021234567890123", position=TextPosition(index=0)
    )
    client = mock.MagicMock()
    client.apply_guardrail.return_value = {"assessments": "not-a-list"}

    with guardrails_configured(client):
        spans = detect_pii_with_guardrails([unit], [])

    assert spans == []
