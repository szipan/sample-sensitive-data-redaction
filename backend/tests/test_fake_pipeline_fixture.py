"""Self-tests for `conftest.py`'s `fake_pipeline` fixture
.

**Why these exist rather than being deleted after one manual run.** the log-content property
is the only consumer of `fake_pipeline`, and it is optional. If it
is never written, the fixture has no coverage at all and can rot silently —
and it is a fixture with a specific, named way of being wrong: design.md and
tasks.md both say the raising mode "is not an extra case; it is half the
property", because a fixture that only ever returns detections would declare
the exception-message channel clean without ever exercising it. These three
tests assert the fixture produces the material the log-content property needs, and nothing
about the absence of markers — that is the log-content property's assertion, deliberately
not duplicated here.

They are also the standing check on the one thing that makes the log-content property
meaningful: that all three of `describe_exception`'s branches are actually
reachable through this fixture.
"""
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tests.conftest import (  # noqa: E402
    PROJECT_EXCEPTION_AUDITED_MESSAGE,
    RAISE_BOTOCORE,
    RAISE_PROJECT,
    RAISE_UNAUDITED,
    RAISING_MODES,
    SANITIZED_BOTOCORE_MESSAGE,
    SANITIZED_UNAUDITED_MESSAGE,
    PlantedMarkers,
)


def test_docx_job_completes_with_every_marker_class_planted_and_raising_on(
    log_capture, fake_pipeline
):
    """The fixture's headline claim: markers planted in all five classes, the
    raising mode on, a terminal Job status, the fakes actually called, and at
    least one record emitted for the log-content property to assert over."""
    captured = log_capture()
    markers = PlantedMarkers()

    run = fake_pipeline(markers=markers, extension=".docx", raising=RAISE_UNAUDITED)

    # A Guardrails failure is swallowed by design (it is a supplementary
    # source), so the Job still completes.
    assert run.status == "completed"
    assert run.redacted_bytes, "a completed job must have stored a document"
    # Source_Object_Identifier really is planted in both keys.
    assert markers.object_identifier in run.job.sourceKey
    assert markers.object_identifier in (run.redacted_key or "")
    # Rule_Pattern really is planted in a rule.
    assert [rule.pattern for rule in run.rules] == [markers.rule_pattern]

    # The fake was actually consulted, and with the planted Document_Text.
    assert run.fakes_were_called
    assert run.guardrail_calls
    sent_texts = [
        call["content"][0]["text"]["text"] for call in run.guardrail_calls
    ]
    assert any(markers.document_text in text for text in sent_texts)

    # And it actually raised, rather than the raising mode being a no-op.
    assert run.raised_exception_types == ["UnauditedThirdPartyError"] * len(
        run.guardrail_calls
    )

    # There is material for the log-content property to assert over.
    assert captured.entries(), "no log record was emitted"


def test_pdf_job_fails_when_textract_raises_and_the_page_ocr_ran(
    log_capture, fake_pipeline
):
    """The documented asymmetry: `ocr_pdf_pages` is a primary path, so a
    Textract failure drives the Job to `failed` rather than degrading it, and
    the marker lands in the Job's DynamoDB `errorMessage` — not in a record."""
    captured = log_capture()
    markers = PlantedMarkers()

    run = fake_pipeline(markers=markers, extension=".pdf", raising=RAISE_BOTOCORE)

    assert run.status == "failed"
    assert run.redacted_bytes is None, "a failed job must store nothing"
    # `ThrottlingException` is retryable, so the real retry loop ran to
    # exhaustion — which is what reaches `ocr_pdf.py`'s retry warning, the
    # only Textract log site `describe_exception` feeds.
    assert len(run.textract_calls) == 3
    assert run.error_message and markers.exception_message in run.error_message
    assert captured.entries()


def test_pdf_job_completes_and_summarizes_when_nothing_raises(fake_pipeline):
    """The non-raising half: both PDF detection paths run, so the Job
    completes with a `detectionSummary` and the OCR unit carries the planted
    Document_Text back through the real `_build_page_unit`."""
    markers = PlantedMarkers()

    run = fake_pipeline(markers=markers, extension=".pdf", raising=None)

    assert run.status == "completed"
    assert run.raised_exception_types == []
    assert run.detection_summary is not None
    assert run.detection_summary["pageHealth"], "a .pdf job records per-page health"
    # The fake Textract response really was assembled into an OCR unit and
    # sent on to Guardrails, so `ocr:` spans are in play.
    sent_texts = [call["content"][0]["text"]["text"] for call in run.guardrail_calls]
    assert any(
        text.startswith(markers.document_text) for text in sent_texts
    ), "the OCR unit built from the fake Textract response never reached Guardrails"


@pytest.mark.parametrize(
    "raising, expected_fragment",
    [
        (RAISE_BOTOCORE, SANITIZED_BOTOCORE_MESSAGE),
        (RAISE_UNAUDITED, SANITIZED_UNAUDITED_MESSAGE),
        (RAISE_PROJECT, PROJECT_EXCEPTION_AUDITED_MESSAGE),
    ],
)
def test_every_raising_mode_reaches_a_distinct_describe_exception_branch(
    log_capture, fake_pipeline, raising, expected_fragment
):
    """All three of `describe_exception`'s branches are reachable through the
    fixture, and each renders its own thing.

    This is the positive half of the log-content property and the reason the raising mode
    spans three classes: a fixture producing only one would leave two
    branches of the sanitizer untested, and an assertion that the marker is
    merely *absent* cannot tell "sanitized" from "never logged at all".
    """
    assert raising in RAISING_MODES
    captured = log_capture()

    run = fake_pipeline(extension=".docx", raising=raising)

    assert run.raised_exception_types, "the raising mode did not raise"
    messages = [entry["message"] for entry in captured.entries()]
    assert any(expected_fragment in message for message in messages), (
        "expected a record carrying {!r}; got {}".format(expected_fragment, messages)
    )
