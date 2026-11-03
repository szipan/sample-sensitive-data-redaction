"""Shared pytest fixtures for the backend suite.

Two fixtures, both from agent-log-delivery design.md's "Testing Strategy"
("Two fixtures carry most of the weight"):

- `log_capture` — the handler-output capture fixture, documented immediately
  below.
- `fake_pipeline` — the fake-pipeline fixture, documented further down, which
  drives the real `handle_job` with `bedrock-runtime`, `textract`, S3 and
  DynamoDB faked. The log-content property uses the two together: `log_capture` first, then
  `fake_pipeline`.

`log_capture`, the handler-output capture fixture from
agent-log-delivery design.md ("Testing Strategy" — "A capture fixture that
calls `configure_logging()` with the handler's stream redirected to an
in-memory buffer").

**Why this fixture exists at all, and the one way it must not be written.**
Six of that spec's eight properties (1, 2, 3, 4, 5, 7) assert on what the
Logging_Configurator's handler *emitted*, and they have to, because the two
things under test are `OwnedLoggerFilter` and `JsonLogFormatter`. A record's
`args` are innocent until `getMessage()` and the formatter render them, and
the leak the spec exists to prevent lives in that rendering. `caplog`
attaches its own handler to the root logger — unfiltered and unformatted — so
an assertion over `caplog.records` bypasses both the filter and the
formatter, and would declare the leak channel clean without ever exercising
it. Assert through `log_capture(...).entries()`, never `caplog.records`.

`caplog` is left working, and stays the right tool for the existing tests
that assert a statement *fired* rather than what it serialized to —
`test_guardrails_pii_detect.py` has three of those. This fixture neither
removes nor filters root's other handlers; it only redirects the stream of
the one handler `configure_logging` owns.
"""

import io
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class CapturedLogStream:
    """The in-memory stream one `log_capture(...)` call redirected the
    Logging_Configurator's handler onto, plus the two accessors every
    consuming property would otherwise re-implement.

    `entries()` is the one to reach for: it returns the parsed JSON objects,
    which is what the properties actually assert over. `text` and `lines()`
    are there for substring assertions (the log-content property asserts substring, not
    equality, so it wants the raw text) and for counting (the other properties
    count lines).
    """

    def __init__(self, handler: logging.Handler, buffer: io.StringIO) -> None:
        self.handler = handler
        self.buffer = buffer

    @property
    def text(self) -> str:
        """Everything written since the last `clear()`, verbatim."""
        self.handler.flush()
        return self.buffer.getvalue()

    def lines(self) -> List[str]:
        """The emitted lines, with the trailing empty line dropped.

        `StreamHandler` writes `terminator` after every record, so a stream
        holding N records splits into N lines plus one empty tail. Blank
        lines are dropped rather than the tail sliced off, so a buffer
        holding nothing yields `[]` rather than `[""]`.
        """
        return [line for line in self.text.splitlines() if line]

    def entries(self) -> List[Dict[str, Any]]:
        """The emitted lines parsed as JSON objects, in emission order.

        Raises `json.JSONDecodeError` if a line does not parse — which is
        itself the assertion the JSON-validity property makes, so failing loudly here is
        correct rather than something to guard.
        """
        return [json.loads(line) for line in self.lines()]

    def clear(self) -> None:
        """Discard what has been captured so far, keeping the redirection.

        For a test that configures once and then wants a per-phase count.
        """
        self.handler.flush()
        self.buffer.seek(0)
        self.buffer.truncate(0)


@pytest.fixture
def log_capture(monkeypatch) -> Callable[..., CapturedLogStream]:
    """Yield a factory that configures log delivery and captures its output.

    Usage:

        def test_something(log_capture):
            captured = log_capture()                  # LOG_LEVEL unset
            logging.getLogger("agent.thing").info("hello")
            assert captured.entries()[0]["message"] == "hello"

        def test_debug(log_capture):
            captured = log_capture(log_level="DEBUG")  # LOG_LEVEL="DEBUG"
            ...

    **A factory, not a plain fixture yielding a buffer.** Properties 2, 4 and
    6 vary `LOG_LEVEL` — and they vary it *inside* a Hypothesis `@given`
    body, once per draw. A fixture parameter is fixed at setup time and
    cannot follow a drawn value, and `pytest.mark.parametrize` over the five
    level names would be a second, worse generator sitting next to
    Hypothesis's. So the level is an argument to the factory, the factory may
    be called once per draw, and each call re-enters `configure_logging()`
    (which is idempotent by construction) and hands back a
    fresh buffer. A test using this inside `@given` must suppress
    `HealthCheck.function_scoped_fixture`, as the existing property tests in
    this suite already do.

    **On the handler being reused rather than fresh.** `agent/__init__.py`
    calls `configure_logging()` at package import, so by the time any test
    that imports an agent module runs, root already carries the marked
    handler — writing to real stdout. `configure_logging` finds and reuses it
    rather than installing a second one, which means the fixture cannot
    assume it owns a fresh handler. Two options: give the fixture its own
    distinguishable handler, or capture and restore the existing handler's
    original stream. This takes the second. Installing a *second* handler
    would test a handler the fixture built rather than the one the
    Configurator installs, and would double-deliver every record for the
    duration of the test — and the whole point of these properties is to
    assert on the real handler's real filter and real formatter. Restoring
    the stream keeps the object identity that `configure_logging()` returns.

    **Restoration is robust to a test failing mid-way.** Everything is
    snapshotted before the first `capture()` call and restored in a `finally`,
    so an assertion failure, an exception from the code under test, or a
    Hypothesis shrink that aborts a draw cannot leave root reconfigured for
    the next test. Four things are snapshotted, because `configure_logging`
    touches all four: the marked handler's stream, root's handler list,
    root's level (which it sets to a `max(level, INFO)` floor), and the level
    and `propagate` flag of every logger that already exists (it sets levels
    on `agent` and `shared` and every already-created descendant, and clears
    `propagate` on `bedrock_agentcore.app`). Loggers *created during* the test
    — the generated `agent.*` names Properties 2 and 4 draw — are not in the
    snapshot and cannot be removed from `logging`'s registry, so they are
    reset to `NOTSET` / `propagate=True` instead, which is the state a
    never-configured logger has.
    """
    # Imported here rather than at conftest import: importing `agent` runs
    # `configure_logging()` as a package-import side effect, and a conftest
    # doing that at collection time would reconfigure logging for every test
    # in the suite, including the ones that do not touch the agent at all.
    from agent.logging_config import _HANDLER_MARKER, configure_logging

    root = logging.getLogger()

    original_root_handlers = list(root.handlers)
    original_root_level = root.level
    original_levels: Dict[str, int] = {}
    original_propagate: Dict[str, bool] = {}
    for name, existing_logger in list(logging.root.manager.loggerDict.items()):
        # `PlaceHolder` entries are not loggers and carry no level to restore.
        if isinstance(existing_logger, logging.Logger):
            original_levels[name] = existing_logger.level
            original_propagate[name] = existing_logger.propagate

    # Keyed on the handler object, so a handler installed by a previous
    # `agent` import and a handler created by the first `capture()` call are
    # both restored, and neither is assumed.
    original_streams: Dict[logging.Handler, Any] = {
        handler: getattr(handler, "stream", None)
        for handler in original_root_handlers
        if getattr(handler, _HANDLER_MARKER, False)
    }

    def capture(log_level: Optional[str] = None) -> CapturedLogStream:
        """Configure logging at `log_level` and redirect its output to a buffer.

        `log_level` is written to the `LOG_LEVEL` environment variable before
        configuration, since that is the only input `configure_logging` reads;
        `None` removes the variable, which is how a test asks for the
        Default_Log_Level. Passing a deliberately invalid value is supported
        and is the point — the resulting warning record
        lands in the returned buffer like any other.
        """
        if log_level is None:
            monkeypatch.delenv("LOG_LEVEL", raising=False)
        else:
            monkeypatch.setenv("LOG_LEVEL", log_level)

        handler = configure_logging()
        original_streams.setdefault(handler, getattr(handler, "stream", None))

        buffer = io.StringIO()
        handler.setStream(buffer)
        return CapturedLogStream(handler, buffer)

    try:
        yield capture
    finally:
        for handler, stream in original_streams.items():
            if stream is not None:
                handler.setStream(stream)

        for handler in list(root.handlers):
            if handler not in original_root_handlers:
                root.removeHandler(handler)
        for handler in original_root_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(original_root_level)

        for name, existing_logger in list(logging.root.manager.loggerDict.items()):
            if not isinstance(existing_logger, logging.Logger):
                continue
            if name in original_levels:
                existing_logger.setLevel(original_levels[name])
                existing_logger.propagate = original_propagate[name]
            else:
                existing_logger.setLevel(logging.NOTSET)
                existing_logger.propagate = True

# ---------------------------------------------------------------------------
# The fake-pipeline fixture
# ---------------------------------------------------------------------------
#
# `fake_pipeline` drives the REAL `agent.orchestration.handle_job` over
# generated `.docx` / `.pdf` bytes with `bedrock-runtime`, `textract`, S3 and
# DynamoDB faked, so the log-content property ("No Sensitive_Value reaches an emitted
# record") can plant marker values in every Sensitive_Value class and then
# assert that none of them appears in anything the Logging_Configurator's
# handler emitted.
#
# Which dialect this follows, and why. Three patterns for faking this
# pipeline already exist in the suite, and this is deliberately the second of
# them rather than a third:
#
#   * `test_orchestration.py`'s `_Pipeline` patches EVERY tool boundary in
#     `agent.orchestration`. Right for asserting call order, useless here:
#     with `detect_pii_with_guardrails` and `ocr_pdf_pages` replaced by
#     mocks, not one of the 15 log statements this spec is about would ever
#     execute.
#   * `test_patti_regression.py` patches exactly the five INFRASTRUCTURE
#     boundaries — `_load_job`, `_read_source_bytes`, `_load_enabled_rules`,
#     `store_redacted_document`, `update_job_status` — and lets every tool
#     run for real. That is this fixture's shape: those five are the S3 and
#     DynamoDB fakes, and everything that logs runs unmocked.
#   * `test_guardrails_pii_detect.py`'s `FakeGuardrailClient` +
#     `guardrails_configured` inject a hand-written client object into the
#     tool module's lazily-created client global. That is this fixture's
#     shape for `bedrock-runtime` and `textract`, and it is why moto is not
#     used: moto cannot make a client raise an arbitrary caller-chosen
#     exception type carrying a duck-typed `response` dict, which is more
#     than half of what this fixture exists to do.
#
# Faking at the client (not the tool) boundary is the load-bearing choice.
# `_call_apply_guardrail`'s retry loop, `_detect_with_retry`'s retry loop,
# `_spans_for_text`'s digit-boundary filter and `_read_guardrail_config` all
# run for real, which is the only way their `logger.warning` calls — and the
# two `describe_exception` call sites this spec added — actually fire.

#: Fixed jobId, so `run()` is deterministic and the value is a Safe_Reference
#: that the log-content property may legitimately find in the output.
FAKE_PIPELINE_JOB_ID = "22222222-2222-2222-2222-222222222222"

#: Raster resolution used for the `.pdf` path. `MIN_RASTER_DPI` exactly, so
#: `_read_raster_dpi` accepts it without emitting its misconfiguration
#: warning (which would add a record whose count the record-count property would have to
#: account for) while keeping page rasterization cheap enough to run inside
#: a 100-iteration Hypothesis loop.
FAKE_PIPELINE_RASTER_DPI = "72"

#: The three ways `describe_exception` treats an exception differently. A
#: fixture that only ever raised one of these would under-test the
#: sanitizer, so `run(raising=...)` takes one of these and the log-content property is
#: expected to draw from them (`st.sampled_from(RAISING_MODES)`), with
#: `None` for the non-raising half of the draws.
RAISE_BOTOCORE = "botocore"
RAISE_UNAUDITED = "unaudited"
RAISE_PROJECT = "project"
RAISING_MODES = (RAISE_BOTOCORE, RAISE_UNAUDITED, RAISE_PROJECT)

#: What `describe_exception` renders for each mode at a site that logs. These
#: are the positive half of the log-content property: the marker must be gone, and *this*
#: must be what replaced it. Exported so a test can assert the sanitizer
#: actually ran rather than that the record happened to be empty.
BOTOCORE_ERROR_CODE = "ThrottlingException"
SANITIZED_BOTOCORE_MESSAGE = "AWS error code {}".format(BOTOCORE_ERROR_CODE)
SANITIZED_UNAUDITED_MESSAGE = "<message omitted: unaudited exception type>"

#: The message the `RAISE_PROJECT` mode's exception carries at the Guardrails
#: boundary. Marker-free on purpose — see `PlantedMarkers` below. Every value
#: in it is a Safe_Reference (a jobId, a page index, a dpi), which is exactly
#: the audit `describe_exception`'s project branch relies on.
PROJECT_EXCEPTION_AUDITED_MESSAGE = (
    "audited project message: jobId {} pageIndex 0 dpi {}".format(
        FAKE_PIPELINE_JOB_ID, FAKE_PIPELINE_RASTER_DPI
    )
)


class UnauditedThirdPartyError(Exception):
    """A plain exception type from a module root that is neither `agent` nor
    `shared`, so `describe_exception` omits its message entirely.

    Defined here rather than reusing a real third-party type because what
    `describe_exception` branches on is `type(error).__module__`, and this
    module's root (`tests`) is unowned in exactly the way `httpx`'s or
    `json`'s would be. It carries no `response` attribute, so
    `_is_retryable` is False for it in both tool modules and it is logged on
    the first attempt rather than after the backoff.
    """


class FakeBotocoreClientError(Exception):
    """An exception shaped like `botocore`'s `ClientError` without importing
    `botocore`.

    Both tool modules read the `response` dict duck-typed (`getattr(error,
    "response", None)`) precisely so they stay testable without botocore, and
    `describe_exception` reads it the same way, so a plain object with the
    right attribute exercises the real code path.

    The code defaults to `ThrottlingException`, which both
    `guardrails_pii_detect._RETRYABLE_ERROR_CODES` and
    `ocr_pdf._RETRYABLE_ERROR_CODES` treat as transient. That is deliberate
    and not incidental: **the retry warning is the only Textract log site
    `describe_exception` feeds** (`ocr_pdf.py:305`), and it is only reached
    on a retryable error that is not the last attempt. A non-retryable
    Textract error is re-raised straight into `OcrPathError` and never
    logged at all, so a fixture raising only non-retryable errors would
    leave that call site unexercised.
    """

    def __init__(self, message: str, code: str = BOTOCORE_ERROR_CODE) -> None:
        super().__init__(message)
        self.response = {
            # `Message` deliberately carries the marker too: it is the field a
            # real botocore error's `str()` renders, and `describe_exception`
            # must return only `Code`.
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": 429},
        }


@dataclass(frozen=True)
class PlantedMarkers:
    """One marker string per Sensitive_Value class, plus one for exception
    messages — the "knob" the log-content property turns.

    **Shape chosen: a frozen dataclass passed *into* the factory and echoed
    back on the result.** The alternative — a factory taking bare keyword
    strings — was rejected because the fixture *derives* values from these:
    `object_identifier` becomes a `fileName`, which becomes a `sourceKey`
    (`{jobId}/{fileName}`) and a `redactedKey`, and `rule_pattern` becomes a
    `RedactionRule.pattern` that is also planted in the document body. A
    caller cannot reliably reconstruct that set, and the log-content property asserts over
    the set, so the fixture hands back the exact object it planted
    (`PipelineRun.markers`) and `all_values()` is the list to iterate. Frozen
    so a run cannot mutate what a test is about to assert over.

    Field-to-class mapping (requirements.md Glossary):

    ==================== ============================================
    Field                Sensitive_Value class it plants
    ==================== ============================================
    `document_text`      Document_Text — in `ExtractedTextUnit.text`
                         (`.docx` paragraph / `.pdf` text layer) and in
                         the `.pdf` page-OCR unit text built from the fake
                         Textract response
    `matched_value`      Matched_Value — what the fake Guardrails client
                         reports as a `piiEntities[].match`
    `object_identifier`  Source_Object_Identifier — the `fileName` stem,
                         hence a substring of `sourceKey` and `redactedKey`
    `rule_pattern`       Rule_Pattern — a `RedactionRule.pattern`, also
                         planted in the body so it produces real spans
    `exception_message`  Not a Sensitive_Value class of its own; the value
                         planted inside the raising modes' exception
                         messages, which is the channel `exc_info=True`
                         and the two `describe_exception` call sites open
    ==================== ============================================

    **`rule_pattern` must be a regex literal.** It is compiled by the real
    `match_rules`, so a drawn marker has to be safe to use verbatim as a
    pattern; `re.escape` is not applied, because escaping would insert
    backslashes and the escaped pattern string would then no longer contain
    the marker the log-content property searches for. `run()` compiles it up front and
    fails loudly rather than letting a bad draw surface as an unrelated
    pipeline failure. Draw markers from `[A-Za-z0-9]` (the defaults below
    are the shape to copy).

    **One deliberate asymmetry, and it is not the fixture dodging a hard
    case.** `describe_exception` returns `str(error)` **in full** for any
    exception whose module root is `agent` or `shared`, on the stated
    grounds that those messages are audited to carry only Safe_References.
    So a marker planted in a project exception's message *at a site that
    logs* would appear in the output, and the log-content property would fail — correctly
    describing the fixture, and saying nothing true about the system. The
    fixture therefore splits the project mode by boundary:

      * At the **Guardrails** boundary (`guardrails_pii_detect.py:277`, which
        does log) the project exception carries
        `PROJECT_EXCEPTION_AUDITED_MESSAGE` — marker-free, Safe_References
        only. This exercises the full-`str()` branch and proves it smuggles
        nothing else in.
      * At the **Textract** boundary the project exception is a real
        `OcrPathError` carrying `exception_message`. design.md's stated
        limitation is that `OcrPathError`'s message interpolates an upstream
        error's `str()` and *that is only acceptable because `OcrPathError`
        is never logged* — it becomes a `failed` Job's DynamoDB
        `errorMessage`. The log-content property finding no marker is precisely what
        verifies that claim, and if a future change starts logging
        `OcrPathError` the property will fail, which is the right outcome.
    """

    document_text: str = "MARKERDOCTEXT7Q4Z"
    matched_value: str = "MARKERMATCH3H8K"
    object_identifier: str = "MARKEROBJECT5V2N"
    rule_pattern: str = "MARKERPATTERN9X6D"
    exception_message: str = "MARKEREXC4B1T"

    def all_values(self) -> List[str]:
        """Every planted marker, for a single `not any(m in text ...)` sweep."""
        return [
            self.document_text,
            self.matched_value,
            self.object_identifier,
            self.rule_pattern,
            self.exception_message,
        ]

    def planted_body_text(self) -> str:
        """The sentence written into the generated document's body.

        Carries `document_text`, `matched_value` and `rule_pattern` so all
        three reach `ExtractedTextUnit.text`, the fake Guardrails client has
        something to report a match on, and the real regex rule has
        something to match.
        """
        return "Contract for {} value {} pattern {}".format(
            self.document_text, self.matched_value, self.rule_pattern
        )


@dataclass
class PipelineRun:
    """What one `fake_pipeline(...)` call did — the evidence a test asserts on.

    `markers` is the object that was planted, so the log-content property never has to
    reconstruct the derived `sourceKey` / `redactedKey` / rule pattern.
    """

    markers: PlantedMarkers
    extension: str
    raising: Optional[str]

    #: Terminal status `handle_job` drove the Job to — `"completed"` or
    #: `"failed"`. `None` would mean `update_job_status` was never called,
    #: which is a bug in the pipeline (a Job is never left in `processing`).
    status: Optional[str] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    detection_summary: Optional[dict] = None
    redacted_bytes: Optional[bytes] = None
    redacted_key: Optional[str] = None

    #: The `apply_guardrail(**kwargs)` calls the fake received, so a test can
    #: prove Bedrock was actually consulted (and read back the exact text
    #: that was sent).
    guardrail_calls: List[Dict[str, Any]] = field(default_factory=list)
    #: One entry per `detect_document_text` call: the length of the page
    #: image sent. Length only — the image *is* Document_Text.
    textract_calls: List[int] = field(default_factory=list)
    #: Type names of every exception the fakes raised, in order.
    raised_exception_types: List[str] = field(default_factory=list)

    source_bytes: bytes = b""
    job: Any = None
    rules: List[Any] = field(default_factory=list)

    @property
    def fakes_were_called(self) -> bool:
        """True when at least one fake AWS boundary was actually exercised.

        Guardrails is called for every non-empty unit of both extensions;
        Textract only for `.pdf` (and for embedded images, which the
        generated documents have none of). So this is the honest "the
        pipeline really ran" check for either extension.
        """
        return bool(self.guardrail_calls) or bool(self.textract_calls)


class _FakeBedrockRuntimeClient:
    """Stand-in for the `bedrock-runtime` client used by
    `guardrails_pii_detect`.

    Non-raising: reports every planted marker that is present in the text it
    was sent as a `piiEntities[].match`, which is how Matched_Value gets into
    the pipeline. Raising: raises the mode's exception on every call, which
    is how the exception-message channel gets exercised.
    """

    def __init__(self, markers: PlantedMarkers, raising: Optional[str]) -> None:
        self.markers = markers
        self.raising = raising
        self.calls: List[Dict[str, Any]] = []
        self.raised: List[str] = []

    def apply_guardrail(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        text = kwargs["content"][0]["text"]["text"]

        if self.raising is not None:
            error = self._error()
            self.raised.append(type(error).__name__)
            raise error

        entities = [
            {"type": "NAME", "match": value, "action": "BLOCKED"}
            for value in self.markers.all_values()
            if value and value in text
        ]
        return {
            "action": "GUARDRAIL_INTERVENED" if entities else "NONE",
            "assessments": [
                {"sensitiveInformationPolicy": {"piiEntities": entities}}
            ],
        }

    def _error(self) -> Exception:
        if self.raising == RAISE_BOTOCORE:
            return FakeBotocoreClientError(
                "ApplyGuardrail rejected parameter content[0].text.text: "
                "{}".format(self.markers.exception_message)
            )
        if self.raising == RAISE_UNAUDITED:
            return UnauditedThirdPartyError(
                "third-party failure quoting {}".format(
                    self.markers.exception_message
                )
            )
        # RAISE_PROJECT. Marker-free by design — see `PlantedMarkers`.
        from agent.orchestration import OrchestrationError

        return OrchestrationError(PROJECT_EXCEPTION_AUDITED_MESSAGE)


class _FakeTextractClient:
    """Stand-in for the `textract` client used by `ocr_pdf`.

    (Embedded-image OCR, `ocr.recognize_image_text`, moved off Textract onto a
    multimodal Bedrock `Converse` model in the llm-image-ocr feature, so this
    fake now serves only the full-page OCR path.)

    Non-raising: returns a `DetectDocumentText` response whose `WORD` blocks
    are the planted markers, so the OCR_Text_Unit `text` the real
    `_build_page_unit` assembles carries Document_Text and the real
    Guardrails path then reports matches inside it.
    """

    def __init__(self, markers: PlantedMarkers, raising: Optional[str]) -> None:
        self.markers = markers
        self.raising = raising
        self.calls: List[int] = []
        self.raised: List[str] = []

    def detect_document_text(self, Document=None, **kwargs) -> dict:  # noqa: N803
        # Length only, never the bytes: the page image *is* Document_Text.
        self.calls.append(len((Document or {}).get("Bytes") or b""))

        if self.raising is not None:
            error = self._error()
            self.raised.append(type(error).__name__)
            raise error

        return _textract_response(
            [
                self.markers.document_text,
                self.markers.matched_value,
                self.markers.rule_pattern,
            ]
        )

    def _error(self) -> Exception:
        if self.raising == RAISE_BOTOCORE:
            return FakeBotocoreClientError(
                "DetectDocumentText rejected parameter Document.Bytes: "
                "{}".format(self.markers.exception_message)
            )
        if self.raising == RAISE_UNAUDITED:
            return UnauditedThirdPartyError(
                "third-party failure quoting {}".format(
                    self.markers.exception_message
                )
            )
        # RAISE_PROJECT: a real project exception carrying the marker, raised
        # at the one boundary where the design's "never logged" claim is what
        # keeps it safe. See `PlantedMarkers`.
        from agent.tools.ocr_pdf import OcrPathError

        return OcrPathError(
            "upstream failure quoting {}".format(self.markers.exception_message),
            page_index=0,
        )


def _textract_response(words: List[str]) -> dict:
    """A minimal `DetectDocumentText` response: one `LINE` whose `CHILD`
    relationship lists one `WORD` per entry of `words`.

    Matches the shape `ocr_pdf._build_page_unit` walks — it builds unit text
    from `WORD` text (not `LINE` text) and drops any word without geometry,
    so every word carries a normalized `[0, 1]` `BoundingBox`.
    """
    word_blocks = []
    child_ids = []
    for index, word in enumerate(words):
        block_id = "word-{}".format(index)
        child_ids.append(block_id)
        word_blocks.append(
            {
                "Id": block_id,
                "BlockType": "WORD",
                "Text": word,
                "Geometry": {
                    "BoundingBox": {
                        "Left": 0.1,
                        "Top": 0.1 + index * 0.05,
                        "Width": 0.4,
                        "Height": 0.03,
                    }
                },
            }
        )
    line_block = {
        "Id": "line-0",
        "BlockType": "LINE",
        "Text": " ".join(words),
        "Relationships": [{"Type": "CHILD", "Ids": child_ids}],
    }
    return {"Blocks": [line_block] + word_blocks}


def _build_docx_bytes(markers: PlantedMarkers) -> bytes:
    """A one-paragraph, one-table `.docx` carrying the planted markers.

    `python-docx` is imported here rather than at conftest import so a
    machine without it can still collect the rest of the suite — the same
    discipline `test_extraction_docx.py` follows by building its fixture
    document with `python-docx` itself.
    """
    import io as _io

    from docx import Document

    document = Document()
    document.add_paragraph(markers.planted_body_text())
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = markers.matched_value

    buffer = _io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _build_pdf_bytes(markers: PlantedMarkers) -> bytes:
    """A one-page `.pdf` whose text layer carries the planted markers."""
    from tests.pdf_fixtures import simple_text_pdf

    return simple_text_pdf([markers.planted_body_text()])


def _make_fake_job(markers: PlantedMarkers, extension: str):
    """A `processing` `RedactionJob` whose `fileName` and `sourceKey` carry
    the Source_Object_Identifier marker."""
    from shared.models import RedactionJob

    file_name = "{}{}".format(markers.object_identifier, extension)
    return RedactionJob(
        jobId=FAKE_PIPELINE_JOB_ID,
        status="processing",
        sourceKey="{}/{}".format(FAKE_PIPELINE_JOB_ID, file_name),
        sourceExtension=extension,
        fileName=file_name,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )


def _make_fake_rules(markers: PlantedMarkers) -> List[Any]:
    """One enabled `RedactionRule` whose `pattern` is the Rule_Pattern
    marker, used verbatim as a regex literal."""
    from shared.models import RedactionRule

    return [
        RedactionRule(
            ruleName="marker-literal-rule",
            pattern=markers.rule_pattern,
            enabled=True,
            isBuiltIn=False,
            createdAt="2024-01-01T00:00:00+00:00",
            updatedAt="2024-01-01T00:00:00+00:00",
        )
    ]


@pytest.fixture
def fake_pipeline(monkeypatch) -> Callable[..., PipelineRun]:
    """Yield a factory that drives the real `handle_job` with every AWS
    boundary faked, and returns what happened.

    Usage, and how it composes with `log_capture`:

        def test_property_1(log_capture, fake_pipeline):
            captured = log_capture()               # FIRST: redirect the handler
            run = fake_pipeline(                   # THEN: drive the pipeline
                markers=PlantedMarkers(...),
                extension=".pdf",
                raising=RAISE_BOTOCORE,
            )
            assert run.status in ("completed", "failed")
            for marker in run.markers.all_values():
                assert marker not in captured.text

    **Order matters, in one direction only.** `log_capture()` must be called
    before `fake_pipeline(...)`, because it is what redirects the marked
    handler's stream to the buffer; records emitted before that go to real
    stdout and are invisible to the assertion. Nothing else couples them:
    this factory never calls `configure_logging()`, never installs or removes
    a handler, and never sets a level, so it cannot disturb what
    `log_capture` snapshotted and restores.

    **What is faked, and at which seam.**

    ================================= ==========================================
    Seam                              Fake
    ================================= ==========================================
    `orchestration._load_job`         DynamoDB `RedactionJobs` read
    `orchestration._read_source_bytes` S3 source-object read
    `orchestration._load_enabled_rules` DynamoDB `RedactionRules` read
    `orchestration.store_redacted_document` S3 output write
    `orchestration.update_job_status`  DynamoDB `RedactionJobs` write
    `guardrails_pii_detect._bedrock_runtime_client` `_FakeBedrockRuntimeClient`
    `ocr_pdf._textract_client`         `_FakeTextractClient`
    `ocr._textract_client`             the same `_FakeTextractClient`
    ================================= ==========================================

    Everything else runs for real: `extract_docx_content` /
    `extract_pdf_content`, `ocr_pdf_pages`, `match_rules`,
    `detect_pii_with_guardrails` (including its retry loop, digit-boundary
    filter and config read), `_regroup_and_merge`, the redactors,
    `verify_structure` and `_build_detection_summary`. That is the point: the
    log statements this spec is about live in the tools, so replacing the
    tools would test nothing.

    `time.sleep` is patched to a no-op in both tool modules. Their backoff is
    1s then 2s per unit, and the raising modes make every unit fail, so
    without this a single `.pdf` draw would take ~9 seconds and a
    100-iteration property test would be unusable.

    **Terminal status and record yield by extension and mode.** Measured, not
    predicted. The log-content property asserts on log content either way, and the
    asymmetries below are deliberate rather than incidental, so the whole
    matrix is on the record:

    ========= ========== ========= ======= =================================
    extension raising    status    records what happened
    ========= ========== ========= ======= =================================
    `.docx`   `None`     completed 0       Clean run. Guardrails returns
                                           matches for 2 units; Textract is
                                           never called (no page OCR for
                                           `.docx`, no embedded images).
                                           **Zero records is correct** —
                                           the logging requirements forbid a
                                           per-unit or per-detection record —
                                           and it is exactly why the raising
                                           mode is mandatory: a fixture with
                                           only this row would let the log-content property
                                           pass over an empty buffer.
    `.docx`   botocore   completed 2       2 units × 3 attempts = 6 calls,
                                           each unit logging once on its last
                                           attempt via
                                           `guardrails_pii_detect.py:277`.
    `.docx`   unaudited  completed 2       Non-retryable, so one call and one
                                           record per unit.
    `.docx`   project    completed 2       Same, with the audited message
                                           rendered in full.
    `.pdf`    `None`     completed 0       Both detection paths run; a
                                           `detectionSummary` is recorded.
    `.pdf`    botocore   **failed** 2      Retryable, so the real retry loop
                                           logs `ocr_pdf.py:305` on attempts
                                           1 and 2 and raises on 3.
    `.pdf`    unaudited  **failed** 0      Non-retryable: re-raised on the
                                           first attempt, wrapped in
                                           `OcrPathError`, **never logged**.
    `.pdf`    project    **failed** 0      Same path, with a real
                                           `OcrPathError` as the cause.
    ========= ========== ========= ======= =================================

    Two things a the log-content property generator has to know from that table.

    **`.docx` is where the Guardrails log sites live, and `.pdf` is where the
    Textract one does — the two never appear in the same run.** For a `.pdf`,
    `ocr_pdf_pages` runs *before* `detect_pii_with_guardrails` and is not
    wrapped, so any raising mode aborts the Job before Guardrails is called at
    all (`guardrail_calls` is empty). So draw both extensions; a `.pdf`-only
    generator never exercises `guardrails_pii_detect.py:277`, and a
    `.docx`-only one never exercises `ocr_pdf.py:305`.

    **The two `.pdf` non-retryable rows emit nothing, and that is the
    assertion, not a gap.** design.md's stated limitation is that
    `OcrPathError`'s message interpolates an upstream error's `str()` and that
    this is acceptable *only because `OcrPathError` is never logged*. Those
    rows are that claim under test: the marker is in the exception, the Job
    fails, and no record exists to carry it. `PipelineRun.error_message`
    confirms where it went instead — a DynamoDB field, not a Log_Record. If a
    future change adds a `logger.exception` on that path, these rows start
    emitting and the log-content property starts failing, which is correct.

    The `failed` rows are the pdf-dual-path spec's asymmetry working as
    designed — losing a supplementary source degrades recall, losing a
    primary source must fail the Job — and are why the log-content property must accept
    either terminal status rather than requiring `completed`.

    Args:
        markers: the values to plant. Defaults to `PlantedMarkers()`; a
            property test should draw them instead, so the assertion does not
            depend on strings that are also source literals in this file.
        extension: `".docx"` or `".pdf"`.
        raising: `None`, or one of `RAISING_MODES`.

    Returns:
        A `PipelineRun`.

    Raises:
        ValueError: if `extension` is not supported, if `raising` is not a
            known mode, or if `markers.rule_pattern` is not a usable regex —
            all three fail here, loudly, rather than surfacing later as an
            unrelated `failed` Job.
    """

    def run(
        markers: Optional[PlantedMarkers] = None,
        extension: str = ".docx",
        raising: Optional[str] = None,
    ) -> PipelineRun:
        markers = markers if markers is not None else PlantedMarkers()

        if extension not in (".docx", ".pdf"):
            raise ValueError("unsupported extension: {!r}".format(extension))
        if raising is not None and raising not in RAISING_MODES:
            raise ValueError(
                "raising must be None or one of {}; got {!r}".format(
                    RAISING_MODES, raising
                )
            )
        try:
            re.compile(markers.rule_pattern)
        except re.error as error:
            raise ValueError(
                "markers.rule_pattern must be usable verbatim as a regex "
                "(draw markers from [A-Za-z0-9]); {!r} does not compile: "
                "{}".format(markers.rule_pattern, error)
            ) from error

        from agent import orchestration
        from agent.tools import guardrails_pii_detect, ocr, ocr_pdf

        bedrock_client = _FakeBedrockRuntimeClient(markers, raising)
        textract_client = _FakeTextractClient(markers, raising)

        monkeypatch.setattr(
            guardrails_pii_detect, "_bedrock_runtime_client", bedrock_client
        )
        monkeypatch.setattr(ocr_pdf, "_textract_client", textract_client)
        # `ocr` (embedded-image OCR) moved off Amazon Textract onto a
        # multimodal Bedrock model addressed via `Converse` (llm-image-ocr):
        # it now reads the shared `bedrock-runtime` client, not a Textract
        # client. The generated fixture documents embed no images, so
        # `recognize_image_text` is not invoked in this pipeline; wiring the
        # fake bedrock client keeps the tool consistent with
        # `guardrails_pii_detect` should an embedded image ever be added.
        monkeypatch.setattr(ocr, "_bedrock_runtime_client", bedrock_client)

        # Without these the tool logs its "not set; skipping" warning and
        # returns immediately, so the fake client would never be called and
        # the exception-message channel would stay closed.
        monkeypatch.setenv(guardrails_pii_detect.GUARDRAIL_ID_ENV_VAR, "gr-fake")
        monkeypatch.setenv(guardrails_pii_detect.GUARDRAIL_VERSION_ENV_VAR, "1")
        monkeypatch.setenv(ocr_pdf.RASTER_DPI_ENV_VAR, FAKE_PIPELINE_RASTER_DPI)

        monkeypatch.setattr(guardrails_pii_detect.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(ocr_pdf.time, "sleep", lambda _seconds: None)

        job = _make_fake_job(markers, extension)
        rules = _make_fake_rules(markers)
        source_bytes = (
            _build_docx_bytes(markers)
            if extension == ".docx"
            else _build_pdf_bytes(markers)
        )

        result = PipelineRun(
            markers=markers,
            extension=extension,
            raising=raising,
            source_bytes=source_bytes,
            job=job,
            rules=rules,
            guardrail_calls=bedrock_client.calls,
            textract_calls=textract_client.calls,
        )

        def _fake_store(redacted_bytes: bytes, job_id: str, file_name: str) -> str:
            result.redacted_bytes = redacted_bytes
            # Mirrors `storage.store_redacted_document`'s key shape, so the
            # Source_Object_Identifier marker travels into `redactedKey` too.
            return "{}/{}".format(job_id, file_name)

        def _fake_update(
            job_id: str,
            status: str,
            redacted_key: Optional[str] = None,
            error_message: Optional[str] = None,
            detection_summary: Optional[dict] = None,
            error_code: Optional[str] = None,
            error_params: Optional[dict] = None,
        ) -> None:
            result.status = status
            result.redacted_key = redacted_key
            result.error_message = error_message
            result.error_code = error_code
            result.detection_summary = detection_summary

        monkeypatch.setattr(orchestration, "_load_job", lambda _job_id: job)
        monkeypatch.setattr(
            orchestration, "_read_source_bytes", lambda _key: source_bytes
        )
        monkeypatch.setattr(orchestration, "_load_enabled_rules", lambda: rules)
        monkeypatch.setattr(orchestration, "store_redacted_document", _fake_store)
        monkeypatch.setattr(orchestration, "update_job_status", _fake_update)

        # The LLM PII source (`detect_pii_with_llm`) reads `LLM_PII_MODEL_ID`
        # from the real environment and, when it is set to a pinned id, builds
        # a real `bedrock-runtime` client and issues live `Converse` calls.
        # This fixture fakes only the Guardrails/Textract Bedrock seams, so on
        # a machine where `LLM_PII_MODEL_ID` is set at the machine level the
        # LLM source would reach real Bedrock during an offline run. Force it
        # dormant (its documented off state — it contributes no spans here)
        # so the fixture is deterministic and network-free regardless of
        # machine env; the log-content sites this fixture exercises live in the
        # Guardrails/Textract tools, not the LLM one.
        monkeypatch.setattr(
            orchestration, "detect_pii_with_llm", lambda *args, **kwargs: []
        )

        orchestration.handle_job(FAKE_PIPELINE_JOB_ID)

        result.raised_exception_types = bedrock_client.raised + textract_client.raised
        return result

    return run
