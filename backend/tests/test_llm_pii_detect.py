"""Tests for `agent/tools/llm_pii_detect.py`
(llm-cjk-pii-recall design.md "Component 2: `backend/agent/tools/llm_pii_detect.py`").

This module is kept free of PDF-library/`python-docx` imports so it runs on
developer machines without those native dependencies — the tool under test
only depends on `shared.models`, `agent.tools.pii_offsets`, and (lazily)
boto3, and the boto3 client is always replaced with an in-test fake here.

Bedrock is mocked with `unittest.mock` only (never moto), mirroring
`tests/test_guardrails_pii_detect.py`: a plain fake `converse` client, and
transient errors raised as objects carrying a duck-typed `response` dict so
`guardrails_pii_detect._is_retryable` (reused by the tool) treats them as
retryable.

This file covers:

- Requests are issued only for a pinned model identifier
- Response parsing is total
- Transient failures are retried up to three attempts
- Every request pins the inference configuration
- The tool never raises and never discards collected spans
- The audit record identifies the run without recording PII
- Unit tests for the tool's call shape and structural guarantees
"""
import ast
import hashlib
import inspect
import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import os  # noqa: E402

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent.tools import llm_pii_detect  # noqa: E402
from agent.tools.llm_pii_detect import detect_pii_with_llm  # noqa: E402
from shared.models import (  # noqa: E402
    ExtractedTextUnit,
    ImageRegion,
    MatchSpan,
    TextPosition,
)

#: A pinned, versioned inference-profile id (ends in `-v1:0`), the same shape
#: `CDK_Stack` supplies. Accepted by `_is_pinned_model_id`.
PINNED_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

TEXT_UNIT_ZONES = ("body", "table", "header", "footer")


# ---------------------------------------------------------------------------
# Fakes and helpers (unittest.mock only — no moto)
# ---------------------------------------------------------------------------


def _fake_response(assistant_text: str) -> dict:
    """A Bedrock `Converse` response whose single content block carries
    `assistant_text` (the shape `_parse_detections` walks)."""
    return {"output": {"message": {"content": [{"text": assistant_text}]}}}


def _detections_json(detections) -> str:
    """A JSON array of `{"type", "match"}` objects, as the model is asked to
    emit. `detections` is an iterable of `(type, match)` pairs."""
    return json.dumps([{"type": t, "match": m} for t, m in detections])


def _unwrap(sent: str) -> str:
    """The unit text inside the document tags of a sent user message."""
    open_tag = llm_pii_detect.DOCUMENT_OPEN_TAG + "\n"
    close_tag = "\n" + llm_pii_detect.DOCUMENT_CLOSE_TAG
    assert sent.startswith(open_tag) and sent.endswith(close_tag), sent
    return sent[len(open_tag) : -len(close_tag)]


class FakeConverseClient:
    """Stand-in for the `bedrock-runtime` client's `converse` surface.

    Records every call's kwargs and returns a canned response, either looked
    up by the exact user text sent or a fixed default.
    """

    def __init__(self, response_for_text=None, default_response=None):
        self.calls = []
        self.response_for_text = response_for_text or {}
        self.default_response = default_response

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        text = _unwrap(kwargs["messages"][0]["content"][0]["text"])
        if text in self.response_for_text:
            return self.response_for_text[text]
        if self.default_response is not None:
            return self.default_response
        return _fake_response("[]")


class FakeThrottlingError(Exception):
    """Duck-typed like a botocore `ThrottlingException` — retryable via its
    `response["Error"]["Code"]`."""

    def __init__(self):
        super().__init__("throttled")
        self.response = {"Error": {"Code": "ThrottlingException"}}


class Fake5xxError(Exception):
    """Duck-typed like a botocore 5xx service error — retryable via its
    `response["ResponseMetadata"]["HTTPStatusCode"]`."""

    def __init__(self):
        super().__init__("service unavailable")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": 503}}


class FakeNonRetryableError(Exception):
    """Duck-typed like a botocore `ValidationException` — NOT retryable."""

    def __init__(self):
        super().__init__("validation error")
        self.response = {"Error": {"Code": "ValidationException"}}


class SequencedConverseClient:
    """A fake whose `converse` raises `error_factory()` for its first
    `failures` calls, then returns `success_response`. Counts calls."""

    def __init__(self, failures, error_factory, success_response):
        self.remaining_failures = failures
        self.error_factory = error_factory
        self.success_response = success_response
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise self.error_factory()
        return self.success_response


@contextmanager
def llm_configured(client, model_id=PINNED_MODEL_ID):
    """Set `LLM_PII_MODEL_ID` and inject `client` as the module's lazily
    created `bedrock-runtime` client, restoring both afterwards.

    A context manager rather than a fixture so it can be used inside
    Hypothesis-driven tests without function-scoped-fixture reuse across
    examples, mirroring `test_guardrails_pii_detect.guardrails_configured`.
    Pass `model_id=None` to leave the variable unset.
    """
    previous_client = llm_pii_detect._bedrock_runtime_client
    previous_env = os.environ.get(llm_pii_detect.MODEL_ID_ENV_VAR)
    llm_pii_detect._bedrock_runtime_client = client
    if model_id is None:
        os.environ.pop(llm_pii_detect.MODEL_ID_ENV_VAR, None)
    else:
        os.environ[llm_pii_detect.MODEL_ID_ENV_VAR] = model_id
    try:
        yield client
    finally:
        llm_pii_detect._bedrock_runtime_client = previous_client
        if previous_env is None:
            os.environ.pop(llm_pii_detect.MODEL_ID_ENV_VAR, None)
        else:
            os.environ[llm_pii_detect.MODEL_ID_ENV_VAR] = previous_env


def _text_unit(index, text, zone="body"):
    return ExtractedTextUnit(zone=zone, text=text, position=TextPosition(index=index))


# ===========================================================================
# Requests are issued only for a pinned model identifier
# ===========================================================================


@st.composite
def _model_id_and_pinned(draw):
    """Draw `(env_value, is_pinned)` across the five id shapes: absent, blank,
    a pinned `...-v<N>:<N>` id, an unversioned id, and a `...latest` alias."""
    kind = draw(
        st.sampled_from(["absent", "blank", "pinned", "unversioned", "latest"])
    )
    if kind == "absent":
        return None, False
    if kind == "blank":
        return draw(st.sampled_from(["", "   ", "\t", "  \n "])), False
    if kind == "pinned":
        base = draw(
            st.sampled_from(
                [
                    "anthropic.claude-haiku-4-5-20251001",
                    "us.anthropic.claude-haiku-4-5-20251001",
                    "amazon.nova-lite",
                    "eu.anthropic.claude-3-5-sonnet-20240620",
                ]
            )
        )
        major = draw(st.integers(min_value=0, max_value=9))
        minor = draw(st.integers(min_value=0, max_value=9))
        return f"{base}-v{major}:{minor}", True
    if kind == "unversioned":
        return (
            draw(
                st.sampled_from(
                    [
                        "anthropic.claude-haiku-4-5",
                        "amazon.nova-lite",
                        "us.anthropic.claude-3-sonnet",
                        "anthropic.claude-instant",
                    ]
                )
            ),
            False,
        )
    return (
        draw(
            st.sampled_from(
                [
                    "anthropic.claude-haiku-latest",
                    "amazon.nova-lite-latest",
                    "us.anthropic.claude-3-sonnet-latest",
                ]
            )
        ),
        False,
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(_model_id_and_pinned())
def test_property_16_requests_only_for_pinned_model_id(sample):
    """`detect_pii_with_llm` issues at least one `converse` call iff
    `LLM_PII_MODEL_ID` is a pinned, versioned id, and otherwise issues zero
    calls and returns no spans.

    Requests are issued only for a pinned model identifier
    """
    model_value, is_pinned = sample
    text = "联系人 张伟"
    unit = _text_unit(0, text)
    client = FakeConverseClient(
        default_response=_fake_response(_detections_json([("NAME", "张伟")]))
    )

    with llm_configured(client, model_id=model_value):
        spans = detect_pii_with_llm([unit], [])

    if is_pinned:
        assert len(client.calls) >= 1
    else:
        assert client.calls == []
        assert spans == []


def test_property_16_example_pinned_and_alias():
    """Example-level check: the exact `CDK_Stack` id is active; a `latest`
    alias and an unversioned id are dormant.

    Requests are issued only for a pinned model identifier
    """
    text = "身份证号 110101199003078888"
    unit = _text_unit(0, text)

    client = FakeConverseClient(default_response=_fake_response("[]"))
    with llm_configured(client, model_id=PINNED_MODEL_ID):
        detect_pii_with_llm([unit], [])
    assert len(client.calls) == 1

    for dormant in ("anthropic.claude-haiku-latest", "anthropic.claude-haiku-4-5", ""):
        client = FakeConverseClient(default_response=_fake_response("[]"))
        with llm_configured(client, model_id=dormant if dormant else None):
            spans = detect_pii_with_llm([unit], [])
        assert client.calls == []
        assert spans == []


# ===========================================================================
# Response parsing is total
# ===========================================================================


#: Text that avoids the two characters the outermost-bracket extraction keys
#: on and the fence delimiter, so a generated well-formed array is not
#: confused by chatter around it.
_SAFE_STR = st.text(
    alphabet=st.characters(blacklist_characters="[]`"), max_size=6
)

_MAYBE_TYPE = st.one_of(
    st.sampled_from(sorted(llm_pii_detect.ENTITY_VOCABULARY)),
    _SAFE_STR,
    st.none(),
    st.integers(),
)
_MAYBE_MATCH = st.one_of(
    _SAFE_STR,
    st.just(""),
    st.none(),
    st.integers(),
    st.booleans(),
)

_ARRAY_ELEMENT = st.one_of(
    st.integers(),
    st.none(),
    st.booleans(),
    _SAFE_STR,
    st.builds(lambda t, m: {"type": t, "match": m}, _MAYBE_TYPE, _MAYBE_MATCH),
    st.builds(lambda t: {"type": t}, _MAYBE_TYPE),
    st.builds(lambda m: {"match": m}, _MAYBE_MATCH),
)

_CHATTER = st.text(alphabet=st.characters(blacklist_characters="[]`"), max_size=12)


def _expected_detections(elements):
    """The detections `_parse_detections` should keep from a well-formed
    array of `elements`: dict elements carrying a non-empty string `match`,
    with an unknown/missing `type` mapped to `UNSPECIFIED`."""
    expected = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        match_text = element.get("match")
        if not isinstance(match_text, str) or not match_text:
            continue
        entity_type = element.get("type")
        if not (isinstance(entity_type, str) and entity_type in llm_pii_detect.ENTITY_VOCABULARY):
            entity_type = llm_pii_detect.UNSPECIFIED_ENTITY_TYPE
        expected.append({"type": entity_type, "match": match_text})
    return expected


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.one_of(
        st.text(),
        st.text().map(lambda s: "```json\n[" + s),  # truncated fenced array
        st.text().map(lambda s: "[" + s),  # truncated open bracket
        st.text().map(lambda s: "prefix " + s + " suffix"),
        st.text().map(lambda s: "```\n" + s + "\n```"),
    )
)
def test_property_4_parsing_is_total_never_raises(model_output):
    """For any string a model might emit — prose, fenced arrays, truncated
    JSON, chatter — `_parse_detections` returns a `list` and never raises.

    Response parsing is total
    """
    result = llm_pii_detect._parse_detections(_fake_response(model_output))
    assert isinstance(result, list)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(_ARRAY_ELEMENT, max_size=6),
    st.sampled_from(["none", "fence", "chatter"]),
    _CHATTER,
    _CHATTER,
)
def test_property_4_wellformed_array_keeps_matching_dicts(
    elements, wrap_mode, prefix, suffix
):
    """For a well-formed embedded JSON array — optionally fenced or wrapped in
    bracket-free chatter — `_parse_detections` returns exactly the dict
    elements carrying a non-empty string `match`, with unknown `type` mapped
    to `UNSPECIFIED`.

    Response parsing is total
    """
    raw = json.dumps(elements)
    if wrap_mode == "fence":
        payload = "```json\n" + raw + "\n```"
    elif wrap_mode == "chatter":
        payload = prefix + raw + suffix
    else:
        payload = raw

    result = llm_pii_detect._parse_detections(_fake_response(payload))
    assert result == _expected_detections(elements)


def test_property_4_example_unknown_type_becomes_unspecified():
    """Example-level check: a valid `match` with an out-of-vocabulary `type`
    is kept as `UNSPECIFIED`; an element without a usable `match` is dropped.

    Response parsing is total
    """
    payload = (
        'Here you go:\n```json\n'
        '[{"type": "NAME", "match": "张伟"}, '
        '{"type": "MADE_UP_TYPE", "match": "秘密"}, '
        '{"type": "NAME", "match": ""}, '
        '{"type": "NAME"}, 42, null]\n```'
    )
    result = llm_pii_detect._parse_detections(_fake_response(payload))
    assert result == [
        {"type": "NAME", "match": "张伟"},
        {"type": llm_pii_detect.UNSPECIFIED_ENTITY_TYPE, "match": "秘密"},
    ]


# ===========================================================================
# Transient failures are retried up to three attempts
# ===========================================================================


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.integers(min_value=0, max_value=5),
    st.sampled_from(["throttle", "5xx"]),
)
def test_property_12_transient_failures_retried_up_to_three_attempts(k, error_kind):
    """With `k` leading transient failures then success, `converse` is called
    exactly `min(k + 1, MAX_RETRIES)` times, and `_converse_with_retry`
    returns the response iff `k < MAX_RETRIES`, else `None`.

    Transient failures are retried up to three attempts
    """
    error_factory = FakeThrottlingError if error_kind == "throttle" else Fake5xxError
    success = _fake_response("[]")
    client = SequencedConverseClient(k, error_factory, success)

    with patch("agent.tools.llm_pii_detect.time.sleep") as sleep:
        result = llm_pii_detect._converse_with_retry(client, PINNED_MODEL_ID, "text")

    assert client.calls == min(k + 1, llm_pii_detect.MAX_RETRIES)
    if k < llm_pii_detect.MAX_RETRIES:
        assert result is success
    else:
        assert result is None
    # Never sleeps more than (attempts - 1) times, and never on the successful
    # or last attempt.
    assert sleep.call_count <= llm_pii_detect.MAX_RETRIES - 1


def test_property_12_non_retryable_error_makes_single_attempt():
    """A non-retryable error stops after one attempt and returns `None`.

    Transient failures are retried up to three attempts
    """
    client = SequencedConverseClient(5, FakeNonRetryableError, _fake_response("[]"))
    with patch("agent.tools.llm_pii_detect.time.sleep") as sleep:
        result = llm_pii_detect._converse_with_retry(client, PINNED_MODEL_ID, "text")

    assert client.calls == 1
    assert result is None
    assert sleep.call_count == 0


# ===========================================================================
# Every request pins the inference configuration
# ===========================================================================


@st.composite
def _three_collections(draw):
    """Draw `(text_units, image_regions, ocr_units)` with at least one
    non-empty text unit so at least one `converse` call is guaranteed."""
    text_texts = draw(st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=4))
    text_units = [
        _text_unit(i, t, zone=draw(st.sampled_from(TEXT_UNIT_ZONES)))
        for i, t in enumerate(text_texts)
    ]
    image_texts = draw(st.lists(st.text(min_size=1, max_size=20), max_size=3))
    image_regions = [
        ImageRegion(
            zone="image", sourceLocation=f"p1-i{i}", recognizedText=t
        )
        for i, t in enumerate(image_texts)
    ]
    ocr_texts = draw(st.lists(st.text(min_size=1, max_size=20), max_size=3))
    ocr_units = [
        ExtractedTextUnit(zone="ocr", text=t, position=TextPosition(index=i))
        for i, t in enumerate(ocr_texts)
    ]
    return text_units, image_regions, ocr_units


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_three_collections())
def test_property_15_every_request_pins_inference_config(collections):
    """Every `converse` request pins `temperature == 0.0` and
    `maxTokens == MAX_OUTPUT_TOKENS`, with `topP` deliberately omitted because
    Claude Haiku 4.5 rejects a request specifying both `temperature` and
    `topP`.

    Every request pins the inference configuration
    """
    text_units, image_regions, ocr_units = collections
    client = FakeConverseClient(default_response=_fake_response("[]"))

    with llm_configured(client):
        detect_pii_with_llm(text_units, image_regions, ocr_units)

    assert client.calls, "expected at least one request for a non-empty unit"
    for call in client.calls:
        config = call["inferenceConfig"]
        assert config["temperature"] == 0.0, call
        assert "topP" not in config, call
        assert config["maxTokens"] == llm_pii_detect.MAX_OUTPUT_TOKENS, call


# ===========================================================================
# The tool never raises and never discards collected spans
# ===========================================================================


class BehaviorConverseClient:
    """A fake whose `converse` behaves per-unit: raise, return malformed
    output, or return a parseable detection — keyed on the sent text."""

    def __init__(self, behavior_by_text):
        self.behavior_by_text = behavior_by_text
        self.calls = []

    def converse(self, **kwargs):
        text = _unwrap(kwargs["messages"][0]["content"][0]["text"])
        self.calls.append(text)
        behavior, payload = self.behavior_by_text[text]
        if behavior == "raise":
            raise FakeNonRetryableError()
        if behavior == "malformed":
            return _fake_response("not json, sorry, no array here")
        return _fake_response(_detections_json(payload))


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(["valid", "malformed", "raise"]), min_size=1, max_size=6))
def test_property_13_never_raises_and_retains_successful_spans(behaviors):
    """For any per-unit pattern of client behaviour, `detect_pii_with_llm`
    returns a `list`, raises nothing, and the result contains exactly the
    spans of the units whose requests succeeded — a failing unit costs only
    its own spans, so earlier collected spans survive.

    The tool never raises and never discards collected spans
    """
    units = []
    behavior_by_text = {}
    for index, behavior in enumerate(behaviors):
        token = f"token{index}X"
        text = f"记录{index} 值 {token} 结束"
        units.append(_text_unit(index, text))
        behavior_by_text[text] = (behavior, [("NAME", token)])

    client = BehaviorConverseClient(behavior_by_text)
    with llm_configured(client):
        spans = detect_pii_with_llm(units, [])

    assert isinstance(spans, list)
    for index, behavior in enumerate(behaviors):
        ref = f"text:{index}"
        unit_spans = [span for span in spans if span.unitRef == ref]
        if behavior == "valid":
            assert len(unit_spans) == 1, (index, behavior, unit_spans)
            span = unit_spans[0]
            assert units[index].text[span.startOffset : span.endOffset] == f"token{index}X"
            assert span.ruleName == "llm:NAME"
            assert span.zone == "body"
        else:
            assert unit_spans == [], (index, behavior, unit_spans)


def test_property_13_outer_handler_retains_spans_when_a_later_unit_raises():
    """When processing a LATER unit raises an unexpected error, the tool's
    outer handler returns the spans already collected from earlier units
    rather than discarding them, and never raises.

    The tool never raises and never discards collected spans
    """
    first = _text_unit(0, "first unit 张伟")
    second = _text_unit(1, "second unit 李四")

    early_span = MatchSpan(
        zone="body",
        unitRef="text:0",
        startOffset=0,
        endOffset=1,
        ruleName="llm:NAME",
    )

    def fake_spans(client, model_id, zone, unit_ref, text):
        if unit_ref == "text:0":
            return [early_span]
        raise RuntimeError("unexpected boom on a later unit")

    client = FakeConverseClient(default_response=_fake_response("[]"))
    with patch.object(llm_pii_detect, "_spans_for_text", side_effect=fake_spans):
        with llm_configured(client):
            spans = detect_pii_with_llm([first, second], [])

    assert spans == [early_span]


# ===========================================================================
# The audit record identifies the run without recording PII
# ===========================================================================

_CJK_TOKEN = st.text(
    alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),
    min_size=2,
    max_size=4,
)


@st.composite
def _cjk_text_and_detections(draw):
    """Draw `(text, detections)` where every detection's CJK `match` is
    embedded in `text`, so a naive logger would leak it. `text` is always
    non-empty."""
    n = draw(st.integers(min_value=0, max_value=4))
    types = draw(
        st.lists(
            st.sampled_from(sorted(llm_pii_detect.ENTITY_VOCABULARY)),
            min_size=n,
            max_size=n,
        )
    )
    matches = draw(st.lists(_CJK_TOKEN, min_size=n, max_size=n))
    detections = list(zip(types, matches))
    segments = [f"信息{i}是{m}" for i, (_, m) in enumerate(detections)]
    filler = draw(_CJK_TOKEN)
    text = "，".join(segments + [filler]) if segments else filler
    return text, detections


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(_cjk_text_and_detections())
def test_property_17_audit_record_identifies_run_without_pii(caplog, sample):
    """The per-request audit record contains the `unitRef`, model id,
    `PROMPT_VERSION`, the SHA-256 digest and length of the exact text sent,
    and each emitted entity type with its offset ranges — and contains
    neither the unit text nor any detection's match text.

    The audit record identifies the run without recording PII
    """
    text, detections = sample
    client = FakeConverseClient(
        default_response=_fake_response(_detections_json(detections))
    )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agent.tools.llm_pii_detect"):
        spans = llm_pii_detect._spans_for_text(
            client, PINNED_MODEL_ID, "body", "text:0", text
        )

    audit_records = [
        record
        for record in caplog.records
        if "LLM PII audit" in record.getMessage()
    ]
    assert len(audit_records) == 1, [r.getMessage() for r in caplog.records]
    record = audit_records[0]
    message = record.getMessage()

    assert record.levelno == logging.INFO
    # Positive: the record identifies the run.
    assert "text:0" in message
    assert PINNED_MODEL_ID in message
    assert llm_pii_detect.PROMPT_VERSION in message
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() in message
    assert f"textLen={len(text)}" in message

    # Positive: each emitted entity type paired with its offset ranges. Build
    # the expected per-type ranges from the emitted spans in emission order —
    # exactly how `_spans_for_text` builds its audit ranges.
    expected_ranges = {}
    for span in spans:
        entity_type = span.ruleName.split(":", 1)[1]
        expected_ranges.setdefault(entity_type, []).append(
            (span.startOffset, span.endOffset)
        )
    assert repr(expected_ranges) in message
    for entity_type in expected_ranges:
        assert entity_type in message

    # Negative: no PII. The record is entirely ASCII by construction, so any
    # CJK unit text or match would only appear if the logger leaked it.
    assert text not in message
    for _, match_text in detections:
        assert match_text not in message


# ===========================================================================
# Unit tests: the tool's call shape and structural guarantees
# ===========================================================================


def test_2_16_two_positional_args_and_ocr_units_optional_last():
    """`detect_pii_with_llm` accepts two positional arguments, with
    `ocr_text_units` optional and last in the signature.
    """
    signature = inspect.signature(detect_pii_with_llm)
    params = list(signature.parameters)
    assert params == ["text_units", "image_regions", "ocr_text_units"]
    assert signature.parameters["ocr_text_units"].default is None

    unit = _text_unit(0, "联系人 张伟")
    client = FakeConverseClient(default_response=_fake_response("[]"))

    # Two-positional call works.
    with llm_configured(client):
        result_two = detect_pii_with_llm([unit], [])
    assert isinstance(result_two, list)

    # Third collection is accepted too.
    ocr_unit = ExtractedTextUnit(zone="ocr", text="扫描 李四", position=TextPosition(index=0))
    client = FakeConverseClient(default_response=_fake_response("[]"))
    with llm_configured(client):
        result_three = detect_pii_with_llm([unit], [], [ocr_unit])
    assert isinstance(result_three, list)


def test_2_16_prompt_template_states_contract_verbatim_and_recall_bias():
    """`PROMPT_TEMPLATE` states the JSON-array output contract, the verbatim
    rule (at least twice), the person-scoped recall rule, and the NOT-PII
    exclusions (prompt v3).
    """
    template = llm_pii_detect.PROMPT_TEMPLATE
    assert "JSON array" in template
    # The verbatim-substring requirement is stated at least twice on purpose.
    assert template.lower().count("verbatim") >= 2
    assert template.count("character-for-character") >= 2
    # Recall applies to a specific person's PII; organizations, places on
    # their own, and URLs are excluded explicitly.
    assert "RECALL FOR REAL PII" in template
    assert "INCLUDE" in template
    assert "NOT PII" in template
    assert "natural person" in template
    # Empty-array contract when nothing is found.
    assert "[]" in template


def _pii_entity_types_from_stack():
    """The `pii_entity_types` list literal declared in `infra/redaction_stack.py`.

    Read via `ast` rather than by importing the module: `pii_entity_types` is
    a local variable inside `RedactionStack.__init__`, so there is no
    importable symbol, and importing the module would require `aws_cdk`. This
    reads the exact literal that configures the `PiiGuardrail`, which is the
    drift guard the runtime tool's duplicated list must satisfy. It is fine
    for a test to touch `infra/` even though the runtime module must not.
    """
    stack_source = (BACKEND_ROOT / "infra" / "redaction_stack.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(stack_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pii_entity_types":
                    return ast.literal_eval(node.value)
    raise AssertionError("pii_entity_types not found in redaction_stack.py")


def test_2_16_guardrails_entity_types_mirror_redaction_stack():
    """`GUARDRAILS_ENTITY_TYPES` equals `redaction_stack.py`'s
    `pii_entity_types` as a set (the drift guard).
    """
    stack_types = _pii_entity_types_from_stack()
    assert set(llm_pii_detect.GUARDRAILS_ENTITY_TYPES) == set(stack_types)


def test_2_16_cn_resident_id_in_vocabulary_and_prompt():
    """`CN_RESIDENT_ID_NUMBER` is in `ENTITY_VOCABULARY` and appears in
    `PROMPT_TEMPLATE`.
    """
    assert "CN_RESIDENT_ID_NUMBER" in llm_pii_detect.ENTITY_VOCABULARY
    assert "CN_RESIDENT_ID_NUMBER" in llm_pii_detect.PROMPT_TEMPLATE


def test_prompt_instructs_date_of_birth_recall_including_cjk_format():
    """The prompt explicitly instructs the model to recall dates of birth in
    any format — including the Chinese `YYYY年MM月DD日` form — and to label them
    AGE. This closes the observed miss where a Chinese-format DOB
    (1995年01月15日) went undetected by every source.
    """
    template = llm_pii_detect.PROMPT_TEMPLATE
    # DOB guidance is present, keyed on the CJK markers and the CJK date shape.
    assert "出生日期" in template
    assert "年" in template and "月" in template and "日" in template
    # It reuses the AGE vocabulary type (already redactable), not a new type.
    assert "AGE" in llm_pii_detect.ENTITY_VOCABULARY
    # Version was bumped for the prompt change so audit records stay traceable.
    assert llm_pii_detect.PROMPT_VERSION == "llm-cjk-pii-recall/v4"


def test_2_16_module_has_no_agentic_machinery_and_single_public_function():
    """The module imports no MCP client, no `strands`, and no sub-agent
    machinery, and exposes exactly one public function.
    """
    assert llm_pii_detect.__all__ == ["detect_pii_with_llm"]

    source = Path(llm_pii_detect.__file__).read_text(encoding="utf-8")
    for forbidden in ("strands", "mcp", "subagent", "sub_agent"):
        assert f"import {forbidden}" not in source, forbidden
        assert f"from {forbidden}" not in source, forbidden

    # Defensive: importing the tool did not pull in any agentic runtime.
    for forbidden in ("strands", "mcp"):
        assert forbidden not in sys.modules, forbidden


def test_2_16_boto3_client_constructed_once_across_two_invocations():
    """`boto3.client` is constructed once across two `detect_pii_with_llm`
    invocations (the shared lazy client).
    """
    unit = _text_unit(0, "联系人 张伟")
    fake = FakeConverseClient(default_response=_fake_response("[]"))

    previous_client = llm_pii_detect._bedrock_runtime_client
    previous_env = os.environ.get(llm_pii_detect.MODEL_ID_ENV_VAR)
    llm_pii_detect._bedrock_runtime_client = None
    os.environ[llm_pii_detect.MODEL_ID_ENV_VAR] = PINNED_MODEL_ID
    try:
        with patch("boto3.client", return_value=fake) as mock_client:
            detect_pii_with_llm([unit], [])
            detect_pii_with_llm([unit], [])
            assert mock_client.call_count == 1
            mock_client.assert_called_with("bedrock-runtime")
    finally:
        llm_pii_detect._bedrock_runtime_client = previous_client
        if previous_env is None:
            os.environ.pop(llm_pii_detect.MODEL_ID_ENV_VAR, None)
        else:
            os.environ[llm_pii_detect.MODEL_ID_ENV_VAR] = previous_env


def test_2_16_client_construction_failure_yields_empty_list():
    """A client-construction failure yields `[]` and raises nothing.
    """
    unit = _text_unit(0, "联系人 张伟")
    previous_env = os.environ.get(llm_pii_detect.MODEL_ID_ENV_VAR)
    os.environ[llm_pii_detect.MODEL_ID_ENV_VAR] = PINNED_MODEL_ID
    try:
        with patch.object(
            llm_pii_detect,
            "_get_bedrock_runtime_client",
            side_effect=RuntimeError("no credentials configured"),
        ):
            result = detect_pii_with_llm([unit], [])
        assert result == []
    finally:
        if previous_env is None:
            os.environ.pop(llm_pii_detect.MODEL_ID_ENV_VAR, None)
        else:
            os.environ[llm_pii_detect.MODEL_ID_ENV_VAR] = previous_env


def test_2_16_request_body_contains_only_prompt_and_unit_text():
    """The captured request carries only the prompt (`system`) and the unit
    text (`messages[0].content[0].text`) — no `MatchSpan`, no other source's
    spans, and no extra content blocks.
    """
    text = "联系人 张伟，电话 13800138000"
    unit = _text_unit(0, text)
    client = FakeConverseClient(default_response=_fake_response("[]"))

    with llm_configured(client):
        detect_pii_with_llm([unit], [])

    assert len(client.calls) == 1
    call = client.calls[0]
    assert set(call.keys()) == {"modelId", "system", "messages", "inferenceConfig"}
    assert call["system"] == [{"text": llm_pii_detect.PROMPT_TEMPLATE}]
    assert call["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "text": f"{llm_pii_detect.DOCUMENT_OPEN_TAG}\n{text}\n"
                    f"{llm_pii_detect.DOCUMENT_CLOSE_TAG}"
                }
            ],
        }
    ]
    # Exactly one user content block: the unit text, nothing else.
    assert len(call["messages"][0]["content"]) == 1
    # No MatchSpan (or any span object) travels in the request body.
    for value in call.values():
        assert "MatchSpan" not in repr(value)


def test_2_16_occurrence_warning_fires_just_above_threshold_not_below(caplog):
    """The occurrence-count warning fires for a detection whose match occurs
    just above `OCCURRENCE_WARN_THRESHOLD` and not just below it.
    """
    threshold = llm_pii_detect.OCCURRENCE_WARN_THRESHOLD
    match_text = "名"  # non-digit, so the digit-boundary rule never applies

    # Just above: threshold + 1 occurrences -> warning.
    above_text = match_text * (threshold + 1)
    client = FakeConverseClient(
        default_response=_fake_response(_detections_json([("NAME", match_text)]))
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent.tools.llm_pii_detect"):
        above_spans = llm_pii_detect._spans_for_text(
            client, PINNED_MODEL_ID, "body", "text:0", above_text
        )
    assert len(above_spans) == threshold + 1
    above_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "over-redacting" in record.getMessage()
    ]
    assert above_warnings, [r.getMessage() for r in caplog.records]

    # Just below: exactly threshold occurrences -> no warning.
    below_text = match_text * threshold
    client = FakeConverseClient(
        default_response=_fake_response(_detections_json([("NAME", match_text)]))
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent.tools.llm_pii_detect"):
        below_spans = llm_pii_detect._spans_for_text(
            client, PINNED_MODEL_ID, "body", "text:0", below_text
        )
    assert len(below_spans) == threshold
    below_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "over-redacting" in record.getMessage()
    ]
    assert not below_warnings, [r.getMessage() for r in caplog.records]


def test_2_16_prompt_version_in_audit_record_via_module_logger(caplog):
    """`PROMPT_VERSION` appears in the audit record, captured through the
    module logger `agent.tools.llm_pii_detect`.
    """
    text = "张伟 联系方式"
    client = FakeConverseClient(
        default_response=_fake_response(_detections_json([("NAME", "张伟")]))
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agent.tools.llm_pii_detect"):
        llm_pii_detect._spans_for_text(client, PINNED_MODEL_ID, "body", "text:0", text)

    audit_records = [
        record
        for record in caplog.records
        if record.name == "agent.tools.llm_pii_detect"
        and "LLM PII audit" in record.getMessage()
    ]
    assert audit_records, [r.getMessage() for r in caplog.records]
    assert llm_pii_detect.PROMPT_VERSION in audit_records[0].getMessage()


# ===========================================================================
# Both detection tools recover identical offsets
#
# `detect_pii_with_llm` and `detect_pii_with_guardrails` both report matched
# *text* (never coordinates) and recover offsets through the SAME shared
# `pii_offsets.resolve_match_occurrences`. This property drives both tools
# with fakes returning the same single detection for the same text and
# asserts they emit byte-for-byte identical offset ranges — the structural
# guarantee that neither offset path can silently drift from the other.
# ===========================================================================

from agent.tools import guardrails_pii_detect  # noqa: E402
from agent.tools import pii_offsets  # noqa: E402
from agent.tools.guardrails_pii_detect import detect_pii_with_guardrails  # noqa: E402


@contextmanager
def _guardrails_configured(client, guardrail_id="gr-test", guardrail_version="1"):
    """Set `GUARDRAIL_ID`/`GUARDRAIL_VERSION` (both non-blank) and inject
    `client` as the guardrails module's lazily-created `bedrock-runtime`
    client, restoring env and the module global afterwards.

    A minimal replica of `test_guardrails_pii_detect.guardrails_configured`
    (kept local so this file does not depend on importing another test
    module). Resetting the module global around the call means no stale real
    client can leak into the fake-driven call.
    """
    previous_client = guardrails_pii_detect._bedrock_runtime_client
    previous_id = os.environ.get(guardrails_pii_detect.GUARDRAIL_ID_ENV_VAR)
    previous_version = os.environ.get(guardrails_pii_detect.GUARDRAIL_VERSION_ENV_VAR)
    guardrails_pii_detect._bedrock_runtime_client = client
    os.environ[guardrails_pii_detect.GUARDRAIL_ID_ENV_VAR] = guardrail_id
    os.environ[guardrails_pii_detect.GUARDRAIL_VERSION_ENV_VAR] = guardrail_version
    try:
        yield client
    finally:
        guardrails_pii_detect._bedrock_runtime_client = previous_client
        if previous_id is None:
            os.environ.pop(guardrails_pii_detect.GUARDRAIL_ID_ENV_VAR, None)
        else:
            os.environ[guardrails_pii_detect.GUARDRAIL_ID_ENV_VAR] = previous_id
        if previous_version is None:
            os.environ.pop(guardrails_pii_detect.GUARDRAIL_VERSION_ENV_VAR, None)
        else:
            os.environ[guardrails_pii_detect.GUARDRAIL_VERSION_ENV_VAR] = previous_version


class _FakeGuardrailClient:
    """Stand-in for the `bedrock-runtime` client's `apply_guardrail` surface,
    mirroring the response shape `test_guardrails_pii_detect.FakeGuardrailClient`
    proves out and that `guardrails_pii_detect._extract_pii_entities` walks:
    `assessments[].sensitiveInformationPolicy.piiEntities[]`, each
    `{"type", "match"}`."""

    def __init__(self, entities_by_text):
        self.entities_by_text = entities_by_text
        self.calls = []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs["content"][0]["text"]["text"]
        entities = [
            {"type": pii_type, "match": match}
            for pii_type, match in self.entities_by_text.get(text, [])
        ]
        return {
            "action": "GUARDRAIL_INTERVENED" if entities else "NONE",
            "assessments": [
                {"sensitiveInformationPolicy": {"piiEntities": entities}}
            ],
        }


def _text_offsets(spans, unit_ref="text:0"):
    """The `(startOffset, endOffset)` pairs a tool emitted for `unit_ref`, in
    emission order."""
    return [
        (span.startOffset, span.endOffset)
        for span in spans
        if span.unitRef == unit_ref
    ]


def _run_both_tools(text, match_text):
    """Drive both detection tools with the SAME single `{"type": "NAME",
    "match": match_text}` detection over `text` in one text-layer unit, each
    with a fresh fake client and its own env, and return
    `(llm_spans, guardrails_spans)`.
    """
    unit = ExtractedTextUnit(zone="body", text=text, position=TextPosition(index=0))

    llm_client = FakeConverseClient(
        default_response=_fake_response(_detections_json([("NAME", match_text)]))
    )
    with llm_configured(llm_client):
        llm_spans = detect_pii_with_llm([unit], [])

    guardrails_client = _FakeGuardrailClient({text: [("NAME", match_text)]})
    with _guardrails_configured(guardrails_client):
        guardrails_spans = detect_pii_with_guardrails([unit], [])

    return llm_spans, guardrails_spans


CJK_ALPHABET = st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF)
_cjk_token = st.text(alphabet=CJK_ALPHABET, min_size=1, max_size=4)

#: Surrogate code points (category ``Cs``) are excluded because
#: `llm_pii_detect._spans_for_text` hashes ``text.encode("utf-8")`` for its
#: audit record, which would raise on a lone surrogate; the tool would then
#: swallow the error and emit nothing for that unit while the guardrails tool
#: (which does not encode) still emitted spans — a spurious inequality that
#: has nothing to do with the shared offset logic. Brackets and backticks are
#: excluded so generated match text cannot perturb the LLM parser's
#: outermost-`[`…`]` extraction and markdown-fence stripping.
_SAFE_ALPHABET = st.characters(
    blacklist_categories=("Cs",), blacklist_characters="[]`"
)
_SAFE_TEXT = st.text(alphabet=_SAFE_ALPHABET, min_size=1, max_size=30)
_SAFE_MATCH = st.text(alphabet=_SAFE_ALPHABET, min_size=1, max_size=5)


@st.composite
def _text_and_match(draw):
    """Draw a `(text, match_text)` pair with `match_text` non-empty and `text`
    always non-empty. The branches deliberately reach the shapes that exercise
    the digit-boundary rule and its emit-all fallback — an ASCII-digit match
    embedded in a longer number, a match inside a licence plate, repeated
    short substrings, matches at end-of-string — as well as non-digit matches
    and arbitrary pairs where the match need not occur in the text at all."""
    kind = draw(
        st.sampled_from(
            [
                "digit_decoy",
                "plate_age",
                "repeated",
                "end_of_string",
                "cjk_name",
                "arbitrary",
            ]
        )
    )

    if kind == "digit_decoy":
        # `456` sits inside the card number `6222021234567890123` (index 9,
        # preceded by `3` and followed by `7` — digit-flanked). Optionally a
        # clean, non-flanked real CVV at end-of-string. With no real value,
        # every occurrence is flanked, exercising the emit-all fallback.
        head = draw(_cjk_token)
        text = f"{head}银行卡号6222021234567890123"
        if draw(st.booleans()):
            text = text + "，CVV码456"
        return text, "456"

    if kind == "plate_age":
        # `34` sits inside the plate `京A·12345` (digit-flanked on both sides).
        # Optionally a clean real age `今年34岁`.
        text = "车牌京A·12345"
        if draw(st.booleans()):
            text = text + "，今年34岁"
        return text, "34"

    if kind == "repeated":
        token = draw(_cjk_token)
        count = draw(st.integers(min_value=1, max_value=4))
        separator = draw(st.sampled_from(["", "，", " ", "和"]))
        text = separator.join([token] * count) or token
        if draw(st.booleans()):
            # Flank a copy with ASCII digits; the match is non-digit, so the
            # digit-boundary rule must NOT apply and both tools keep it.
            text = "12" + token + "34" + text
        return text, token

    if kind == "end_of_string":
        head = draw(_cjk_token)
        match = draw(
            st.one_of(_cjk_token, st.sampled_from(["456", "34", "88", "abc"]))
        )
        return f"{head}{match}", match

    if kind == "cjk_name":
        name = draw(_cjk_token)
        text = draw(
            st.sampled_from([f"联系人{name}", f"{name}与{name}", f"姓名：{name}"])
        )
        return text, name

    # arbitrary: match_text need not occur in text — both tools should then
    # emit nothing (asserted equal-empty).
    text = draw(_SAFE_TEXT)
    match = draw(st.one_of(_SAFE_MATCH, _cjk_token))
    return text, match


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(_text_and_match())
def test_property_7_both_tools_recover_identical_offsets(sample):
    """For any `(text, match_text)`, the offset ranges `detect_pii_with_llm`
    emits for a detection of that match equal the ranges
    `detect_pii_with_guardrails` emits for the same detection in the same
    text — and both equal `resolve_match_occurrences(text, match_text)`,
    the shared code both tools route through. Every emitted span slices back
    to exactly `match_text`.

    Both detection tools recover identical offsets
    """
    text, match_text = sample
    llm_spans, guardrails_spans = _run_both_tools(text, match_text)

    llm_offsets = _text_offsets(llm_spans)
    guardrails_offsets = _text_offsets(guardrails_spans)

    # Core requirement: the two tools recover identical offsets.
    assert llm_offsets == guardrails_offsets

    # Stronger check: both equal the shared resolver's occurrences directly,
    # so neither tool can drift from the shared code.
    expected = pii_offsets.resolve_match_occurrences(text, match_text).occurrences
    assert llm_offsets == expected

    # Every emitted span slices back to the match text (both tools guarantee).
    for start, end in llm_offsets:
        assert text[start:end] == match_text


def test_property_7_example_decoy_cases_identical_offsets():
    """Example-level check on the two documented decoys: the CVV `456` inside
    the card number `6222021234567890123`, and the age `34` inside the plate
    `京A·12345`. Both tools must produce identical offsets, and the
    digit-flanked decoy occurrence must be excluded in favour of the real,
    non-flanked value (the fallback would only fire if ALL were flanked).

    Both detection tools recover identical offsets
    """
    # Decoy 1: CVV inside a bank card number.
    text_cvv = "银行卡号 6222021234567890123，CVV码456"
    llm_cvv, guardrails_cvv = _run_both_tools(text_cvv, "456")
    offsets_cvv = _text_offsets(llm_cvv)
    assert offsets_cvv == _text_offsets(guardrails_cvv)
    real_cvv = text_cvv.index("CVV码") + len("CVV码")
    assert offsets_cvv == [(real_cvv, real_cvv + 3)]
    card_decoy = text_cvv.index("6222021234567890123") + 9
    assert text_cvv[card_decoy : card_decoy + 3] == "456"  # the decoy really is "456"
    assert (card_decoy, card_decoy + 3) not in offsets_cvv  # ...and is excluded
    for start, end in offsets_cvv:
        assert text_cvv[start:end] == "456"

    # Decoy 2: age inside a licence plate.
    text_age = "车牌 京A·12345，今年34岁"
    llm_age, guardrails_age = _run_both_tools(text_age, "34")
    offsets_age = _text_offsets(llm_age)
    assert offsets_age == _text_offsets(guardrails_age)
    real_age = text_age.index("今年") + len("今年")
    assert offsets_age == [(real_age, real_age + 2)]
    plate_decoy = text_age.index("12345") + 2
    assert text_age[plate_decoy : plate_decoy + 2] == "34"  # the decoy really is "34"
    assert (plate_decoy, plate_decoy + 2) not in offsets_age  # ...and is excluded
    for start, end in offsets_age:
        assert text_age[start:end] == "34"
