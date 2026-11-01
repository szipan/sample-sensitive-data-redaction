"""Property test: no sensitive value reaches an emitted log record.

No Sensitive_Value reaches an emitted record

This is the check the whole feature rests on: turning log delivery on must not
create the leak the product exists to prevent. It composes the two fixtures
built for it — `log_capture` (redirects the Logging_Configurator's
marked handler onto an in-memory buffer) and `fake_pipeline` (drives the REAL
`handle_job` with the AWS boundaries faked, planting marker values across all
five Sensitive_Value classes). The assertion is over the handler's *serialized*
output (`CapturedLogStream.text`), never `caplog.records`: a record's `args`
are innocent until `getMessage()` and `JsonLogFormatter` render them, and the
leak lives in that rendering.

Why this test draws what it draws (see the design doc):

- Marker values are drawn, not hardcoded, so the assertion cannot depend on
  strings that are also literals in `conftest.py`. They are drawn from
  `[A-Za-z0-9]` (so `rule_pattern` stays a usable regex literal) with a
  leading letter and length >= 16, so an incidental substring collision with
  a Safe_Reference already in the stream (the fixed jobId of all digits and
  hyphens, the "72" dpi, logger names, the sanitized replacement strings) is
  implausible.
- The extension is drawn across {".docx", ".pdf"}: the fixture matrix
  establishes that the Guardrails log sites fire only on `.docx` and the
  Textract one only on `.pdf`, and the two never co-occur in one run, so a
  single-extension generator would leave half the leak channels untested.
- `raising` is drawn across {None} u RAISING_MODES, biased so None is drawn
  well under half the time, because the raising channel is the one that
  `exc_info=True` and the two `describe_exception` call sites open — the
  channel a test of deliberate message arguments would declare clean.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from tests.conftest import (  # noqa: E402
    RAISE_BOTOCORE,
    RAISING_MODES,
    PlantedMarkers,
)

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ALNUM = _LETTERS + "0123456789"


def _marker() -> st.SearchStrategy:
    """A distinctive marker drawn from [A-Za-z0-9].

    Leads with a letter and is at least 16 characters, so it is a valid regex
    literal (no escaping needed, which would otherwise break the substring
    search) and is implausible as an incidental substring of any
    Safe_Reference the pipeline legitimately emits.
    """
    return st.builds(
        lambda head, tail: head + tail,
        st.sampled_from(list(_LETTERS)),
        st.text(alphabet=_ALNUM, min_size=15, max_size=23),
    )


# {None} u RAISING_MODES, with RAISING_MODES listed twice so None is drawn
# roughly one draw in seven — comfortably under half, keeping the
# exc_info/describe_exception channel exercised on the large majority of runs.
_RAISING = st.sampled_from((None,) + RAISING_MODES + RAISING_MODES)


def _matrix_emits_records(extension: str, raising) -> bool:
    """Whether the fixture matrix says this (extension, raising) run emits at
    least one Log_Record (see `fake_pipeline`'s docstring table).

    Records are emitted only when:
      * `.docx` with any raising mode on (the two Guardrails units log once
        each on their final attempt via `guardrails_pii_detect.py:277`), or
      * `.pdf` with the retryable botocore mode (the real retry loop logs
        `ocr_pdf.py:305` on attempts 1 and 2 before raising on 3).

    Every other combination emits zero: the two clean runs (`.docx`+None,
    `.pdf`+None) by the logging requirements, and the two non-retryable `.pdf`
    rows because the error is re-raised into an `OcrPathError` that is never
    logged. Those still must leak nothing — which the substring sweep below
    checks unconditionally.
    """
    if extension == ".docx":
        return raising is not None
    return raising == RAISE_BOTOCORE


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ],
)
@given(
    document_text=_marker(),
    matched_value=_marker(),
    object_identifier=_marker(),
    rule_pattern=_marker(),
    exception_message=_marker(),
    extension=st.sampled_from((".docx", ".pdf")),
    raising=_RAISING,
)
def test_property_1_no_sensitive_value_reaches_an_emitted_record(
    log_capture,
    fake_pipeline,
    document_text,
    matched_value,
    object_identifier,
    rule_pattern,
    exception_message,
    extension,
    raising,
):
    """No marker planted in any Sensitive_Value class appears as a substring
    of anything the Logging_Configurator's handler emits, across both
    extensions and every raising mode."""
    markers = PlantedMarkers(
        document_text=document_text,
        matched_value=matched_value,
        object_identifier=object_identifier,
        rule_pattern=rule_pattern,
        exception_message=exception_message,
    )

    # log_capture() FIRST — it is what redirects the marked handler's stream to
    # the buffer; records emitted before it go to real stdout and are invisible.
    captured = log_capture()
    run = fake_pipeline(markers=markers, extension=extension, raising=raising)

    # Non-vacuity, part 1: the pipeline reached a terminal status rather than
    # being left in `processing` (which would mean `update_job_status` was
    # never reached and nothing ran).
    assert run.status in ("completed", "failed"), (
        "pipeline never reached a terminal status: extension={!r} raising={!r} "
        "status={!r}".format(extension, raising, run.status)
    )

    # The property itself: substring, not equality — a marker embedded in a
    # longer string still counts as a leak. Asserted over the serialized
    # output, so the filter and formatter are exercised, not bypassed.
    output = captured.text
    for marker in run.markers.all_values():
        assert marker not in output, (
            "LEAK: Sensitive_Value {!r} reached an emitted log record.\n"
            "  extension={!r} raising={!r} status={!r}\n"
            "  emitted output:\n{}".format(
                marker, extension, raising, run.status, output
            )
        )

    # Non-vacuity, part 2: in the cases the fixture matrix says emit records,
    # confirm the buffer actually holds parseable records — otherwise the leak
    # channel was never exercised and the sweep above passed over an empty
    # buffer. The zero-record cases are deliberately NOT asserted non-empty;
    # for them the sweep above having found no marker is the whole assertion.
    if _matrix_emits_records(extension, raising):
        assert captured.entries(), (
            "expected at least one emitted record for extension={!r} "
            "raising={!r} (matrix says this run logs), but the buffer was "
            "empty — the leak channel was not exercised".format(extension, raising)
        )
