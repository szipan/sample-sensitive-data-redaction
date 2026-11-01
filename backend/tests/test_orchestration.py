"""Unit tests for `agent/orchestration.py`.

These tests assert the fixed-order orchestration *control flow* described
in the orchestration design, including the rule that a Job reaches
`completed` iff processing AND the structure checks both succeed, else
`failed`. The individual Agent tool functions, the source-bytes S3 read,
the job load, and the enabled-rules load are all mocked via
`unittest.mock.patch` at the `agent.orchestration` module boundary, so the
assertions are purely about how `handle_job` sequences those tools and how
it maps success/failure combinations onto the terminal Job status.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent import orchestration  # noqa: E402
from agent.tools import redaction_image  # noqa: E402
from agent.tools.extraction_docx import ExtractionError  # noqa: E402
from agent.tools.ocr_pdf import OcrPathError  # noqa: E402
from agent.tools.redaction_pdf import (  # noqa: E402
    PdfRedactionReport,
    PdfRedactionResult,
    RedactionError,
)
from agent.tools.structure_verification import VerificationResult  # noqa: E402
from shared.models import (  # noqa: E402
    ExtractedTextUnit,
    ImageRegion,
    MatchSpan,
    RedactionJob,
    TextPosition,
)

JOB_ID = "11111111-1111-1111-1111-111111111111"
REDACTED_KEY = "{}/source.docx".format(JOB_ID)

# A real `MatchSpan` rather than a placeholder string: `handle_job` now feeds
# `match_rules`' output through the *real* `_regroup_and_merge`, which reads
# `span.unitRef` (and the merge helper reads the offsets/rule name), so the
# stub has to be a structurally valid span.
_STUB_MATCH = MatchSpan(
    zone="body",
    unitRef="text:0",
    startOffset=0,
    endOffset=4,
    ruleName="stub-rule",
)


def _make_job(
    extension: str = ".docx",
    file_name: str = "source.docx",
    address_detection_source: str = "both",
    name_detection_source: str = "both",
) -> RedactionJob:
    return RedactionJob(
        jobId=JOB_ID,
        status="processing",
        sourceKey="{}/{}".format(JOB_ID, file_name),
        sourceExtension=extension,
        fileName=file_name,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
        addressDetectionSource=address_detection_source,
        nameDetectionSource=name_detection_source,
    )


def _redaction_result(
    document_bytes: bytes = b"redacted",
    text_layer_regions: int = 0,
    ocr_regions: int = 0,
    agreed_regions: int = 0,
) -> PdfRedactionResult:
    """`_redact_document` now returns a `PdfRedactionResult` for both
    extensions, so `handle_job` reads `.documentBytes` off it."""
    return PdfRedactionResult(
        documentBytes=document_bytes,
        report=PdfRedactionReport(
            textLayerRegions=text_layer_regions,
            ocrRegions=ocr_regions,
            agreedRegions=agreed_regions,
        ),
    )


def _content(text_units=None, image_regions=None) -> SimpleNamespace:
    """A minimal stand-in for an ExtractionResult: `handle_job` only reads
    `.textUnits` and reads/reassigns `.imageRegions`."""
    return SimpleNamespace(
        textUnits=text_units if text_units is not None else ["unit-0"],
        imageRegions=image_regions if image_regions is not None else [],
    )


class _Pipeline:
    """Patches every tool boundary in `agent.orchestration` and attaches
    each mock to one manager so call ORDER across tools can be asserted via
    `manager.mock_calls`."""

    def __init__(
        self,
        job,
        content,
        verification,
        redacted_bytes=b"redacted",
        ocr_units=None,
    ):
        self.manager = MagicMock()
        self._patchers = []

        specs = {
            "_load_job": MagicMock(return_value=job),
            "_read_source_bytes": MagicMock(return_value=b"source-bytes"),
            "_extract_document_content": MagicMock(return_value=content),
            "recognize_image_text": MagicMock(
                side_effect=lambda regions: regions
            ),
            "ocr_pdf_pages": MagicMock(
                return_value=ocr_units if ocr_units is not None else []
            ),
            "_load_enabled_rules": MagicMock(return_value=[]),
            "match_rules": MagicMock(return_value=[_STUB_MATCH]),
            "detect_pii_with_guardrails": MagicMock(return_value=[]),
            "detect_pii_with_llm": MagicMock(return_value=[]),
            "_redact_document": MagicMock(
                return_value=_redaction_result(redacted_bytes)
            ),
            "verify_structure": MagicMock(return_value=verification),
            "store_redacted_document": MagicMock(return_value=REDACTED_KEY),
            "update_job_status": MagicMock(return_value=None),
        }
        self.mocks = specs
        for name, mock in specs.items():
            self.manager.attach_mock(mock, name)
            patcher = patch.object(orchestration, name, mock)
            self._patchers.append(patcher)

    def __enter__(self):
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self._patchers:
            patcher.stop()
        return False

    def called_tool_order(self):
        """Ordered list of the tool/helper names invoked on the manager."""
        return [name for name, _args, _kwargs in self.manager.mock_calls if "." not in name]


def test_happy_path_completes_with_redacted_key_and_correct_order():
    """Positive case: all tools succeed -> Job `completed` with the
    redactedKey from store_redacted_document, tools called in fixed order
    including OCR (images present)."""
    job = _make_job(extension=".docx")
    content = _content(text_units=["u0"], image_regions=["img-0"])
    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        orchestration.handle_job(JOB_ID)

    order = pipeline.called_tool_order()
    assert order == [
        "_load_job",
        "_read_source_bytes",
        "_extract_document_content",
        "recognize_image_text",
        "_load_enabled_rules",
        "match_rules",
        "detect_pii_with_guardrails",
        "detect_pii_with_llm",
        "_redact_document",
        "verify_structure",
        "store_redacted_document",
        "update_job_status",
    ]

    pipeline.mocks["update_job_status"].assert_called_once_with(
        JOB_ID,
        orchestration.STATUS_COMPLETED,
        redacted_key=REDACTED_KEY,
        detection_summary={"llmSpans": 0, "llmOnlySpans": 0},
    )
    # The exact same text_units object from extraction is threaded into
    # match_rules and _redact_document (the redaction tools' precondition).
    _match_args, _ = pipeline.mocks["match_rules"].call_args
    assert _match_args[0] is content.textUnits
    _redact_args, _ = pipeline.mocks["_redact_document"].call_args
    assert _redact_args[1] is content.textUnits


def test_ocr_skipped_when_no_image_regions():
    """OCR must be skipped when the document has no image regions (design.md:
    "仅当文档含图片/扫描页时调用")."""
    job = _make_job()
    content = _content(image_regions=[])
    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["recognize_image_text"].assert_not_called()
    assert "recognize_image_text" not in pipeline.called_tool_order()
    # Still completes.
    pipeline.mocks["update_job_status"].assert_called_once_with(
        JOB_ID,
        orchestration.STATUS_COMPLETED,
        redacted_key=REDACTED_KEY,
        detection_summary={"llmSpans": 0, "llmOnlySpans": 0},
    )


def test_ocr_called_when_image_regions_present():
    job = _make_job()
    content = _content(image_regions=["img-0", "img-1"])
    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["recognize_image_text"].assert_called_once_with(["img-0", "img-1"])


def test_extraction_error_marks_failed_and_skips_downstream():
    """Negative case: extraction raises -> Job `failed`, and
    redact/verify/store are never reached."""
    job = _make_job()
    with _Pipeline(job, _content(), VerificationResult.success()) as pipeline:
        pipeline.mocks["_extract_document_content"].side_effect = ExtractionError(
            "corrupt .docx"
        )
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["match_rules"].assert_not_called()
    pipeline.mocks["_redact_document"].assert_not_called()
    pipeline.mocks["verify_structure"].assert_not_called()
    pipeline.mocks["store_redacted_document"].assert_not_called()
    pipeline.mocks["update_job_status"].assert_called_once_with(
        JOB_ID,
        orchestration.STATUS_FAILED,
        error_message="corrupt .docx",
        error_code=orchestration.ERROR_PROCESSING_FAILED,
    )


def test_verify_structure_failure_marks_failed_and_does_not_store():
    """`verify_structure` ok=False -> Job
    `failed` with the verification reason, and the structurally-broken
    document is NEVER stored."""
    job = _make_job()
    verification = VerificationResult.failure("page_count_mismatch")
    with _Pipeline(job, _content(), verification) as pipeline:
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["store_redacted_document"].assert_not_called()
    pipeline.mocks["update_job_status"].assert_called_once_with(
        JOB_ID,
        orchestration.STATUS_FAILED,
        error_message="page_count_mismatch",
        error_code=orchestration.ERROR_STRUCTURE_VERIFICATION_FAILED,
    )


def test_unexpected_exception_midpipeline_marks_failed_never_stuck_processing():
    """An unexpected error mid-pipeline (here
    redaction) -> Job `failed`, never left in `processing`."""
    job = _make_job()
    with _Pipeline(job, _content(), VerificationResult.success()) as pipeline:
        pipeline.mocks["_redact_document"].side_effect = RuntimeError(
            "boom during redaction"
        )
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["verify_structure"].assert_not_called()
    pipeline.mocks["store_redacted_document"].assert_not_called()
    pipeline.mocks["update_job_status"].assert_called_once_with(
        JOB_ID,
        orchestration.STATUS_FAILED,
        error_message="boom during redaction",
        error_code=orchestration.ERROR_PROCESSING_FAILED,
    )


def test_store_failure_marks_failed():
    """Storage failure after a successful verify -> Job `failed` (completed
    is only reached when storage also succeeds)."""
    job = _make_job()
    with _Pipeline(job, _content(), VerificationResult.success()) as pipeline:
        pipeline.mocks["store_redacted_document"].side_effect = RuntimeError(
            "s3 write failed"
        )
        orchestration.handle_job(JOB_ID)

    # update_job_status is invoked once, for the failed transition (the
    # completed call is never reached because store raised first).
    pipeline.mocks["update_job_status"].assert_called_once_with(
        JOB_ID,
        orchestration.STATUS_FAILED,
        error_message="s3 write failed",
        error_code=orchestration.ERROR_PROCESSING_FAILED,
    )


def test_docx_dispatches_to_docx_extractor_and_redactor():
    """`.docx` source dispatches extraction/redaction to the docx tools."""
    job = _make_job(extension=".docx")
    with patch.object(orchestration, "_load_job", return_value=job), patch.object(
        orchestration, "_read_source_bytes", return_value=b"docx-bytes"
    ), patch.object(
        orchestration, "extract_docx_content", return_value=_content()
    ) as extract_docx, patch.object(
        orchestration, "extract_pdf_content"
    ) as extract_pdf, patch.object(
        orchestration, "_load_enabled_rules", return_value=[]
    ), patch.object(
        orchestration, "match_rules", return_value=[]
    ), patch.object(
        orchestration, "detect_pii_with_guardrails", return_value=[]
    ), patch.object(
        orchestration, "detect_pii_with_llm", return_value=[]
    ), patch.object(
        orchestration, "redact_docx_document", return_value=b"red"
    ) as redact_docx, patch.object(
        orchestration, "redact_pdf_document_with_report"
    ) as redact_pdf, patch.object(
        orchestration, "ocr_pdf_pages"
    ) as ocr_pages, patch.object(
        orchestration, "verify_structure", return_value=VerificationResult.success()
    ), patch.object(
        orchestration, "store_redacted_document", return_value=REDACTED_KEY
    ), patch.object(
        orchestration, "update_job_status"
    ):
        orchestration.handle_job(JOB_ID)

    extract_docx.assert_called_once_with(b"docx-bytes")
    extract_pdf.assert_not_called()
    redact_docx.assert_called_once()
    redact_pdf.assert_not_called()
    # `.docx` stays single-path — zero page-rasterization
    # Textract calls.
    ocr_pages.assert_not_called()


def test_pdf_dispatches_to_pdf_extractor_and_redactor():
    """`.pdf` source dispatches extraction/redaction to the pdf tools."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    with patch.object(orchestration, "_load_job", return_value=job), patch.object(
        orchestration, "_read_source_bytes", return_value=b"pdf-bytes"
    ), patch.object(
        orchestration, "extract_docx_content"
    ) as extract_docx, patch.object(
        orchestration, "extract_pdf_content", return_value=_content()
    ) as extract_pdf, patch.object(
        orchestration, "_load_enabled_rules", return_value=[]
    ), patch.object(
        orchestration, "match_rules", return_value=[]
    ), patch.object(
        orchestration, "detect_pii_with_guardrails", return_value=[]
    ), patch.object(
        orchestration, "detect_pii_with_llm", return_value=[]
    ), patch.object(
        orchestration, "redact_docx_document"
    ) as redact_docx, patch.object(
        orchestration,
        "redact_pdf_document_with_report",
        return_value=_redaction_result(b"red"),
    ) as redact_pdf, patch.object(
        orchestration, "ocr_pdf_pages", return_value=[]
    ) as ocr_pages, patch.object(
        orchestration, "verify_structure", return_value=VerificationResult.success()
    ), patch.object(
        orchestration, "store_redacted_document", return_value=REDACTED_KEY
    ), patch.object(
        orchestration, "update_job_status"
    ):
        orchestration.handle_job(JOB_ID)

    extract_pdf.assert_called_once_with(b"pdf-bytes")
    extract_docx.assert_not_called()
    redact_pdf.assert_called_once()
    redact_docx.assert_not_called()
    ocr_pages.assert_called_once_with(b"pdf-bytes")


def test_unsupported_extension_marks_failed():
    """An unexpected/unsupported extension is treated as a processing
    failure -> Job `failed` (never reaches redaction/verify/store)."""
    job = _make_job(extension=".txt", file_name="source.txt")
    with patch.object(orchestration, "_load_job", return_value=job), patch.object(
        orchestration, "_read_source_bytes", return_value=b"bytes"
    ), patch.object(
        orchestration, "verify_structure"
    ) as verify, patch.object(
        orchestration, "store_redacted_document"
    ) as store, patch.object(
        orchestration, "update_job_status"
    ) as update:
        orchestration.handle_job(JOB_ID)

    verify.assert_not_called()
    store.assert_not_called()
    update.assert_called_once()
    args, kwargs = update.call_args
    assert args[0] == JOB_ID
    assert args[1] == orchestration.STATUS_FAILED
    assert "'.txt'" in kwargs["error_message"]


# ---------------------------------------------------------------------------
# OCR_Path wiring
# ---------------------------------------------------------------------------


def _ocr_unit(page_index: int, text: str = "Patti") -> ExtractedTextUnit:
    """A stand-in for one `ocr_pdf_pages` output unit. `handle_job` passes
    these through to the detection tools and reads only their `text` when
    building the Detection_Summary, so the words list can stay empty."""
    return ExtractedTextUnit(
        zone="ocr",
        text=text,
        position=TextPosition(pageIndex=page_index),
        ocrWords=[],
    )


def _text_unit(page_index: int, text: str = "Pa#") -> ExtractedTextUnit:
    """A stand-in for one Text_Layer_Path unit of a `.pdf`, carrying the
    `position.pageIndex` the page-health signal groups on."""
    return ExtractedTextUnit(
        zone="body",
        text=text,
        position=TextPosition(index=0, pageIndex=page_index),
    )


def test_pdf_job_invokes_ocr_path_and_threads_units_to_every_consumer():
    """For a `.pdf`, the OCR_Path runs on the source bytes
    and the exact same OCR unit list reaches rule matching, Guardrails
    detection, and redaction — so both paths' spans arrive at the redactor as
    one collection."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    ocr_units = [_ocr_unit(0), _ocr_unit(1, "Martin")]
    content = _content(text_units=[_text_unit(0), _text_unit(1)])
    with _Pipeline(
        job, content, VerificationResult.success(), ocr_units=ocr_units
    ) as pipeline:
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["ocr_pdf_pages"].assert_called_once_with(b"source-bytes")

    # The identical list object is threaded to all three consumers, which is
    # the precondition both detection tools and the redactor document:
    # `"ocr:{i}"` indexes into this exact list.
    match_args, _ = pipeline.mocks["match_rules"].call_args
    assert match_args[3] is ocr_units
    guardrails_args, _ = pipeline.mocks["detect_pii_with_guardrails"].call_args
    assert guardrails_args[2] is ocr_units
    redact_args, _ = pipeline.mocks["_redact_document"].call_args
    assert redact_args[4] is ocr_units

    _args, kwargs = pipeline.mocks["update_job_status"].call_args
    assert _args == (JOB_ID, orchestration.STATUS_COMPLETED)
    assert kwargs["redacted_key"] == REDACTED_KEY
    assert kwargs["detection_summary"] is not None


def test_pdf_job_runs_ocr_path_after_extraction_and_before_matching():
    """The OCR_Path sits between extraction (plus embedded-image OCR) and
    detection in the fixed pipeline order."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    content = _content(text_units=[_text_unit(0)], image_regions=["img-0"])
    with _Pipeline(
        job, content, VerificationResult.success(), ocr_units=[_ocr_unit(0)]
    ) as pipeline:
        orchestration.handle_job(JOB_ID)

    assert pipeline.called_tool_order() == [
        "_load_job",
        "_read_source_bytes",
        "_extract_document_content",
        "recognize_image_text",
        "ocr_pdf_pages",
        "_load_enabled_rules",
        "match_rules",
        "detect_pii_with_guardrails",
        "detect_pii_with_llm",
        "_redact_document",
        "verify_structure",
        "store_redacted_document",
        "update_job_status",
    ]


def test_docx_job_passes_empty_ocr_units_to_every_consumer():
    """A `.docx` Job never invokes the OCR_Path, and the
    downstream tools receive an empty OCR unit collection."""
    job = _make_job(extension=".docx")
    with _Pipeline(job, _content(), VerificationResult.success()) as pipeline:
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["ocr_pdf_pages"].assert_not_called()
    match_args, _ = pipeline.mocks["match_rules"].call_args
    assert match_args[3] == []
    guardrails_args, _ = pipeline.mocks["detect_pii_with_guardrails"].call_args
    assert guardrails_args[2] == []
    redact_args, _ = pipeline.mocks["_redact_document"].call_args
    assert redact_args[4] == []
    # A single-path `.docx` Job now records a Detection_Summary
    # carrying ONLY the two LLM keys; the dual-path keys are omitted (never
    # zeroed) because there was no second path to compare against.
    _args, kwargs = pipeline.mocks["update_job_status"].call_args
    assert _args == (JOB_ID, orchestration.STATUS_COMPLETED)
    assert kwargs["detection_summary"] == {"llmSpans": 0, "llmOnlySpans": 0}


def test_ocr_path_error_fails_job_with_page_index_and_stores_nothing():
    """An unrecoverable OCR_Path failure is
    NOT swallowed — it reaches the top-level handler, the Job becomes
    `failed` with the failing 0-based page index in `errorMessage`, and no
    Redacted Document is stored. This is the deliberate asymmetry with the
    Guardrails call, which stays wrapped."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    with _Pipeline(job, _content(), VerificationResult.success()) as pipeline:
        pipeline.mocks["ocr_pdf_pages"].side_effect = OcrPathError(
            "Textract failed", page_index=2
        )
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["match_rules"].assert_not_called()
    pipeline.mocks["_redact_document"].assert_not_called()
    pipeline.mocks["verify_structure"].assert_not_called()
    pipeline.mocks["store_redacted_document"].assert_not_called()

    pipeline.mocks["update_job_status"].assert_called_once()
    args, kwargs = pipeline.mocks["update_job_status"].call_args
    assert args == (JOB_ID, orchestration.STATUS_FAILED)
    assert "page 2" in kwargs["error_message"]
    assert "Textract failed" in kwargs["error_message"]
    # The UI gets a machine-readable code plus the 1-based page number.
    assert kwargs["error_code"] == orchestration.ERROR_OCR_PAGE_FAILED
    assert kwargs["error_params"] == {"page": 3}


def test_guardrails_failure_still_completes_pdf_job():
    """A Guardrails failure stays non-fatal even on the
    dual-path `.pdf` route — the Job completes on the detections collected
    from the remaining sources."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    with _Pipeline(
        job,
        _content(text_units=[_text_unit(0)]),
        VerificationResult.success(),
        ocr_units=[_ocr_unit(0)],
    ) as pipeline:
        pipeline.mocks["detect_pii_with_guardrails"].side_effect = RuntimeError(
            "bedrock down"
        )
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["update_job_status"].assert_called_once()
    _args, kwargs = pipeline.mocks["update_job_status"].call_args
    assert _args == (JOB_ID, orchestration.STATUS_COMPLETED)
    assert kwargs["redacted_key"] == REDACTED_KEY


def test_redact_document_returns_report_for_both_extensions():
    """`_redact_document` returns one shape for both extensions: the `.pdf`
    branch forwards the redactor's real report (so the Detection_Summary can be
    built from it), the `.docx` branch an all-zero one."""
    ocr_units = [_ocr_unit(0)]
    pdf_result = _redaction_result(b"pdf-out", text_layer_regions=1, ocr_regions=3,
                                  agreed_regions=1)
    with patch.object(
        orchestration, "redact_pdf_document_with_report", return_value=pdf_result
    ) as redact_pdf:
        result = orchestration._redact_document(
            b"src", ["u0"], [_STUB_MATCH], ".pdf", ocr_units
        )

    redact_pdf.assert_called_once_with(b"src", ["u0"], [_STUB_MATCH], ocr_units, None)
    assert result is pdf_result
    assert result.report.ocrRegions == 3

    with patch.object(
        orchestration, "redact_docx_document", return_value=b"docx-out"
    ) as redact_docx:
        docx_result = orchestration._redact_document(
            b"src", ["u0"], [_STUB_MATCH], ".docx", []
        )

    redact_docx.assert_called_once_with(b"src", ["u0"], [_STUB_MATCH], None)
    assert docx_result.documentBytes == b"docx-out"
    assert docx_result.report == PdfRedactionReport(
        textLayerRegions=0, ocrRegions=0, agreedRegions=0
    )


# ---------------------------------------------------------------------------
# Detection_Summary construction
# ---------------------------------------------------------------------------


def test_non_whitespace_count_for_page_sums_units_and_ignores_whitespace():
    """The health signal counts non-whitespace characters
    across every text unit on the page, so document spacing (which the two
    paths disagree about by construction) cannot skew it."""
    text_units = [
        _text_unit(0, "Pa#\t、"),
        _text_unit(0, "  Mar&n  "),
        _text_unit(1, "Chris&na"),
    ]

    assert orchestration._non_whitespace_count_for_page(text_units, 0) == 9
    assert orchestration._non_whitespace_count_for_page(text_units, 1) == 8


def test_non_whitespace_count_for_page_is_zero_for_a_page_the_text_layer_dropped():
    """A page with no text unit at all (a scanned page, or one the text layer
    dropped) reports 0 rather than raising — the asymmetry against a non-zero
    `ocrChars` is precisely the signal."""
    text_units = [_text_unit(0, "Pa#")]

    assert orchestration._non_whitespace_count_for_page(text_units, 1) == 0
    assert orchestration._non_whitespace_count_for_page([], 0) == 0


def test_non_whitespace_count_for_page_ignores_units_without_a_page_index():
    """`.docx` extraction never sets `position.pageIndex`; such units must not
    be attributed to page 0."""
    unit = ExtractedTextUnit(zone="body", text="Patti", position=TextPosition(index=0))

    assert orchestration._non_whitespace_count_for_page([unit], 0) == 0


def test_build_detection_summary_forwards_counts_and_covers_every_page_once():
    """The summary carries the redactor's per-path
    and agreement counts verbatim, and holds exactly one `pageHealth` entry
    per OCR unit — i.e. per page — in page order."""
    report = PdfRedactionReport(textLayerRegions=1, ocrRegions=3, agreedRegions=1)
    text_units = [_text_unit(0, "Pa#、Mar&n、Chris&na")]
    ocr_text_units = [
        _ocr_unit(0, "Patti、Martin、Christina"),
        _ocr_unit(1, "Second page"),
    ]

    summary = orchestration._build_detection_summary(
        ".pdf", [], 0, report, text_units, ocr_text_units
    )

    assert summary["llmSpans"] == 0
    assert summary["llmOnlySpans"] == 0
    assert summary["textLayerRegions"] == 1
    assert summary["ocrRegions"] == 3
    assert summary["agreedRegions"] == 1
    assert summary["pageHealth"] == [
        {"pageIndex": 0, "textLayerChars": 18, "ocrChars": 22},
        # Page 1 has no text-layer unit at all -> 0, against 10 OCR chars.
        {"pageIndex": 1, "textLayerChars": 0, "ocrChars": 10},
    ]
    # The keys are exactly the `DetectionSummary` shape the API and frontend
    # types declare — a rename here silently breaks both.
    assert set(summary) == {
        "llmSpans",
        "llmOnlySpans",
        "textLayerRegions",
        "ocrRegions",
        "agreedRegions",
        "pageHealth",
    }
    assert set(summary["pageHealth"][0]) == {
        "pageIndex",
        "textLayerChars",
        "ocrChars",
    }


def test_build_detection_summary_has_empty_page_health_without_ocr_units():
    report = PdfRedactionReport(textLayerRegions=2, ocrRegions=0, agreedRegions=0)

    summary = orchestration._build_detection_summary(
        ".pdf", [], 0, report, [_text_unit(0)], []
    )

    assert summary["pageHealth"] == []
    assert summary["textLayerRegions"] == 2


def test_completed_pdf_job_records_the_detection_summary():
    """A completed `.pdf` Job records the Detection_Summary
    built from the report the redactor returned."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    content = _content(text_units=[_text_unit(0, "Pa#、Mar&n、Chris&na")])
    ocr_units = [_ocr_unit(0, "Patti、Martin、Christina")]
    with _Pipeline(
        job, content, VerificationResult.success(), ocr_units=ocr_units
    ) as pipeline:
        pipeline.mocks["_redact_document"].return_value = _redaction_result(
            b"redacted", text_layer_regions=1, ocr_regions=3, agreed_regions=1
        )
        orchestration.handle_job(JOB_ID)

    _args, kwargs = pipeline.mocks["update_job_status"].call_args
    assert _args == (JOB_ID, orchestration.STATUS_COMPLETED)
    assert kwargs["redacted_key"] == REDACTED_KEY
    assert kwargs["detection_summary"] == {
        "llmSpans": 0,
        "llmOnlySpans": 0,
        "textLayerRegions": 1,
        "ocrRegions": 3,
        "agreedRegions": 1,
        "pageHealth": [{"pageIndex": 0, "textLayerChars": 18, "ocrChars": 22}],
    }


def test_failed_pdf_job_records_no_detection_summary():
    """A `failed` transition never carries a summary — there is no completed
    detection run to describe."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    verification = VerificationResult.failure("page_count_mismatch")
    with _Pipeline(
        job,
        _content(text_units=[_text_unit(0)]),
        verification,
        ocr_units=[_ocr_unit(0)],
    ) as pipeline:
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["update_job_status"].assert_called_once_with(
        JOB_ID,
        orchestration.STATUS_FAILED,
        error_message="page_count_mismatch",
        error_code=orchestration.ERROR_STRUCTURE_VERIFICATION_FAILED,
    )


# ---------------------------------------------------------------------------
# Embedded-image OCR failure never fails the Job (llm-image-ocr)
# ---------------------------------------------------------------------------

#: Exception types the belt-and-braces wrap around `recognize_image_text`
#: must swallow. All derive from `Exception` (the wrap catches `Exception`),
#: covering both builtin and stdlib-ish shapes so the property does not lean
#: on one exception class.
_OCR_FAILURE_EXCEPTION_TYPES = [
    ValueError,
    RuntimeError,
    TypeError,
    KeyError,
    ArithmeticError,
    orchestration.OrchestrationError,
]


@settings(max_examples=100, deadline=None)
@given(
    region_count=st.integers(min_value=1, max_value=5),
    failure_mode=st.sampled_from(["dormant", "raise"]),
    exc_type=st.sampled_from(_OCR_FAILURE_EXCEPTION_TYPES),
    message=st.text(min_size=0, max_size=40),
    extension=st.sampled_from([".docx", ".pdf"]),
)
def test_property9_embedded_image_ocr_failure_never_fails_the_job(
    region_count, failure_mode, exc_type, message, extension
):
    """An embedded-image OCR failure never fails the Job

    For any failure mode of `recognize_image_text` — (a) a dormant
    configuration that returns the image regions unchanged with
    `recognizedText` left unpopulated, and (b) an unexpected exception raised
    by the tool, which `handle_job`'s belt-and-braces `try/except` must
    swallow — a Redaction Job whose every other pipeline step succeeds still
    reaches status `completed` with a redacted document stored, and is NEVER
    driven to `failed`.

    The document under test always carries at least one image region, so the
    `if content.imageRegions:` call site is exercised on every example. All
    AWS/tool boundaries are faked via `_Pipeline`, so no real call is made.
    """
    file_name = "source.docx" if extension == ".docx" else "source.pdf"
    job = _make_job(extension=extension, file_name=file_name)
    image_regions = ["img-{}".format(index) for index in range(region_count)]
    content = _content(text_units=["u0"], image_regions=image_regions)

    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        if failure_mode == "dormant":
            # Dormant/off-switch: the tool returns the regions unchanged,
            # Having populated nothing.
            pipeline.mocks["recognize_image_text"].side_effect = (
                lambda regions: regions
            )
        else:
            # A bug breaks the tool's never-raise promise: the wrap in
            # `handle_job` must catch it and continue.
            pipeline.mocks["recognize_image_text"].side_effect = exc_type(message)

        orchestration.handle_job(JOB_ID)

    # A redacted document was stored, and the Job completed — never failed.
    pipeline.mocks["store_redacted_document"].assert_called_once()
    pipeline.mocks["update_job_status"].assert_called_once()
    status_args, status_kwargs = pipeline.mocks["update_job_status"].call_args
    assert status_args[0] == JOB_ID
    assert status_args[1] == orchestration.STATUS_COMPLETED
    assert status_args[1] != orchestration.STATUS_FAILED
    assert status_kwargs["redacted_key"] == REDACTED_KEY


# ---------------------------------------------------------------------------
# In-image redaction wiring
# ---------------------------------------------------------------------------


def _image_region(index: int, image_bytes: bytes) -> ImageRegion:
    """A minimal placeholder `ImageRegion` as extraction would emit it — the
    re-embed handle in `sourceLocation` and the raw `imageBytes`, with
    `recognizedText`/`recognizedElements` left for `recognize_image_text`."""
    return ImageRegion(
        zone="image",
        sourceLocation="page:0:xref:{}".format(index),
        imageBytes=image_bytes,
    )


def test_handle_job_threads_image_regions_into_redact_document():
    """`handle_job` forwards the
    document's `content.imageRegions` — the exact object left by
    `recognize_image_text` — to `_redact_document` as its trailing
    `image_regions` argument, so the redactor can act on `image:` spans."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    regions = [_image_region(0, b"img-0-bytes"), _image_region(1, b"img-1-bytes")]
    content = _content(text_units=["u0"], image_regions=regions)
    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        orchestration.handle_job(JOB_ID)

    # Positional args: (source_bytes, text_units, matches, extension,
    # ocr_text_units, image_regions).
    redact_args, _ = pipeline.mocks["_redact_document"].call_args
    assert redact_args[5] is content.imageRegions
    # `recognize_image_text` returns the same list it was handed, so the
    # object threaded to the redactor is the very one extraction produced.
    assert redact_args[5] is regions


def test_redact_document_forwards_image_regions_to_both_redactors():
    """`_redact_document` forwards the `image_regions` it receives to
    whichever redactor it dispatches: as the trailing arg (after
    `ocr_text_units`) of `redact_pdf_document_with_report` for `.pdf`, and as
    the trailing arg of `redact_docx_document` for `.docx`. This extends
    `test_redact_document_returns_report_for_both_extensions`, which pins the
    default (`None`) case, to a populated region list."""
    regions = [_image_region(0, b"img")]
    ocr_units = [_ocr_unit(0)]
    with patch.object(
        orchestration,
        "redact_pdf_document_with_report",
        return_value=_redaction_result(b"pdf-out"),
    ) as redact_pdf:
        orchestration._redact_document(
            b"src", ["u0"], [_STUB_MATCH], ".pdf", ocr_units, regions
        )
    redact_pdf.assert_called_once_with(
        b"src", ["u0"], [_STUB_MATCH], ocr_units, regions
    )

    with patch.object(
        orchestration, "redact_docx_document", return_value=b"docx-out"
    ) as redact_docx:
        orchestration._redact_document(
            b"src", ["u0"], [_STUB_MATCH], ".docx", [], regions
        )
    redact_docx.assert_called_once_with(b"src", ["u0"], [_STUB_MATCH], regions)


def test_image_regions_present_leaves_pipeline_order_and_posture_unchanged():
    """Image regions add no step and reorder none: with a
    document carrying image regions the fixed pipeline order is unchanged and
    the never-raise posture holds — the Job still reaches `completed`."""
    job = _make_job(extension=".pdf", file_name="source.pdf")
    regions = [_image_region(0, b"img")]
    content = _content(text_units=["u0"], image_regions=regions)
    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        orchestration.handle_job(JOB_ID)

    assert pipeline.called_tool_order() == [
        "_load_job",
        "_read_source_bytes",
        "_extract_document_content",
        "recognize_image_text",
        "ocr_pdf_pages",
        "_load_enabled_rules",
        "match_rules",
        "detect_pii_with_guardrails",
        "detect_pii_with_llm",
        "_redact_document",
        "verify_structure",
        "store_redacted_document",
        "update_job_status",
    ]
    status_args, _ = pipeline.mocks["update_job_status"].call_args
    assert status_args[1] == orchestration.STATUS_COMPLETED


# ---------------------------------------------------------------------------
# Localization failures degrade; manipulation failures fail
# ---------------------------------------------------------------------------

#: The localization-failure modes the design's error table
#: enumerate. At the orchestration boundary they are indistinguishable — each
#: leaves `recognizedElements is None` and `recognize_image_text` returns
#: WITHOUT raising — so the modes are sampled to document that any of them
#: maps to the same terminal outcome (`completed`, image over-redacted).
_LOCALIZATION_FAILURE_MODES = [
    "dormant",
    "retries_exhausted",
    "unparseable",
    "implausible_box",
]


@settings(max_examples=100, deadline=None)
@given(
    failure_mode=st.sampled_from(_LOCALIZATION_FAILURE_MODES),
    region_count=st.integers(min_value=1, max_value=4),
    extension=st.sampled_from([".docx", ".pdf"]),
)
def test_property10_localization_failure_degrades_to_completed(
    failure_mode, region_count, extension
):
    """Localization failures degrade; manipulation failures fail the Job

    Localization half: for ANY localization failure — a dormant/unpinned
    model, a `Converse` that fails after its retries, an unparseable/empty
    response, or a parsed-but-implausible box — the region carries no
    trustworthy geometry (`recognizedElements is None`) and
    `recognize_image_text` returns without raising. A Redaction Job whose
    every OTHER step succeeds therefore still reaches `completed`, with the
    image regions forwarded to the redactor (which whole-images any region
    carrying spans) and a Redacted Document stored — the Job is
    NEVER driven to `failed`.
    """
    file_name = "source.docx" if extension == ".docx" else "source.pdf"
    job = _make_job(extension=extension, file_name=file_name)
    regions = [
        ImageRegion(
            zone="image",
            sourceLocation="loc:{}".format(index),
            imageBytes=b"bytes-%d" % index,
            # Every localization failure mode collapses to untrustworthy
            # geometry, which drives the redactor's Whole_Image_Fallback.
            recognizedElements=None,
        )
        for index in range(region_count)
    ]
    content = _content(text_units=["u0"], image_regions=regions)

    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        # Localization failure never raises out of the tool; the regions come
        # back unchanged (recognizedElements still None).
        pipeline.mocks["recognize_image_text"].side_effect = lambda r: r
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["_redact_document"].assert_called_once()
    redact_args, _ = pipeline.mocks["_redact_document"].call_args
    assert redact_args[5] is content.imageRegions
    pipeline.mocks["store_redacted_document"].assert_called_once()

    status_args, status_kwargs = pipeline.mocks["update_job_status"].call_args
    assert status_args[0] == JOB_ID
    assert status_args[1] == orchestration.STATUS_COMPLETED
    assert status_args[1] != orchestration.STATUS_FAILED
    assert status_kwargs["redacted_key"] == REDACTED_KEY


@settings(max_examples=100, deadline=None)
@given(
    exc_message=st.text(min_size=0, max_size=40),
    region_count=st.integers(min_value=1, max_value=4),
    extension=st.sampled_from([".docx", ".pdf"]),
)
def test_property10_image_manipulation_failure_fails_the_job(
    exc_message, region_count, extension
):
    """Localization failures degrade; manipulation failures fail the Job

    Manipulation half: for ANY image decode/draw/encode/re-embed failure on an
    image carrying spans, the redactor turns the `ImageRedactionError` into a
    `RedactionError`, so `_redact_document` raises. That propagates to
    `handle_job`'s top-level handler and drives the Job to `failed` with
    NOTHING stored — `verify_structure` is never reached, no Redacted Document
    is stored, and there is no `completed` transition. This is the deliberate
    asymmetry with a localization failure: a manipulation failure is fatal
    because emitting the document would leave visible in-image PII.
    """
    file_name = "source.docx" if extension == ".docx" else "source.pdf"
    job = _make_job(extension=extension, file_name=file_name)
    regions = [
        ImageRegion(
            zone="image", sourceLocation="loc:{}".format(i), imageBytes=b"b"
        )
        for i in range(region_count)
    ]
    content = _content(text_units=["u0"], image_regions=regions)

    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        # Both redactors wrap `ImageRedactionError` into a `RedactionError`;
        # `_redact_document` (mocked here) stands in for either dispatch.
        pipeline.mocks["_redact_document"].side_effect = RedactionError(exc_message)
        orchestration.handle_job(JOB_ID)

    pipeline.mocks["verify_structure"].assert_not_called()
    pipeline.mocks["store_redacted_document"].assert_not_called()
    pipeline.mocks["update_job_status"].assert_called_once_with(
        JOB_ID,
        orchestration.STATUS_FAILED,
        error_message=exc_message,
        error_code=orchestration.ERROR_PROCESSING_FAILED,
    )


# ---------------------------------------------------------------------------
# An image with no spans is left byte-identical
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    region_count=st.integers(min_value=1, max_value=5),
    referenced_indices=st.sets(st.integers(min_value=0, max_value=4)),
    extension=st.sampled_from([".docx", ".pdf"]),
)
def test_property4_no_span_image_is_forwarded_byte_identical(
    region_count, referenced_indices, extension
):
    """An image with no spans is left byte-identical

    Orchestration half: for any set of match spans and image regions,
    `handle_job` forwards the COMPLETE `image_regions` list to the redactor
    unchanged and never itself decodes, draws on, or re-embeds an image. So an
    `ImageRegion` referenced by NO `image:` span reaches the redactor as the
    same instance with byte-identical `imageBytes`, and
    `redaction_image.redact_image_bytes` is never invoked from the
    orchestration path (a region is only ever decoded/redrawn/re-embedded by
    the redactor, and only when an `image:` span references it). The
    redactor-level pixel guarantee — that a no-span region's OUTPUT bytes
    equal its source bytes — is covered by the redactor tests in the real-
    `fitz`/`docx` process group.
    """
    file_name = "source.docx" if extension == ".docx" else "source.pdf"
    job = _make_job(extension=extension, file_name=file_name)

    source_bytes_by_index = {
        index: b"img-bytes-%d" % index for index in range(region_count)
    }
    regions = [
        ImageRegion(
            zone="image",
            sourceLocation="loc:{}".format(index),
            imageBytes=source_bytes_by_index[index],
            recognizedText="text-{}".format(index),
        )
        for index in range(region_count)
    ]
    content = _content(text_units=["u0"], image_regions=regions)

    valid_referenced = {i for i in referenced_indices if i < region_count}
    # `image:` spans for exactly the referenced regions, plus one text span so
    # the "any set of match spans" mix always includes non-image content.
    spans = [_STUB_MATCH] + [
        MatchSpan(
            zone="image",
            unitRef="image:{}".format(i),
            startOffset=0,
            endOffset=3,
            ruleName="stub-rule",
        )
        for i in sorted(valid_referenced)
    ]
    no_span_indices = [i for i in range(region_count) if i not in valid_referenced]

    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        pipeline.mocks["match_rules"].return_value = spans
        with patch.object(redaction_image, "redact_image_bytes") as redact_image_spy:
            orchestration.handle_job(JOB_ID)

    # Orchestration never decodes/draws/re-embeds — it delegates entirely.
    redact_image_spy.assert_not_called()

    redact_args, _ = pipeline.mocks["_redact_document"].call_args
    forwarded_regions = redact_args[5]
    assert forwarded_regions is content.imageRegions
    # Every region referenced by no `image:` span is the same instance with
    # unchanged bytes.
    for index in no_span_indices:
        assert forwarded_regions[index] is regions[index]
        assert forwarded_regions[index].imageBytes == source_bytes_by_index[index]


# ---------------------------------------------------------------------------
# Per-entity detection-source switch (detection-source-switch feature)
# ---------------------------------------------------------------------------


def _addr_span(rule_name: str, start: int = 0, end: int = 5) -> MatchSpan:
    return MatchSpan(
        zone="body",
        unitRef="text:0",
        startOffset=start,
        endOffset=end,
        ruleName=rule_name,
    )


_ADDR_RULES = ("guardrails:ADDRESS", "llm:ADDRESS")
_NAME_RULES = ("guardrails:NAME", "llm:NAME")


def test_apply_source_policy_both_keeps_all_spans():
    guardrails = [_addr_span("guardrails:ADDRESS"), _addr_span("guardrails:NAME", 10, 15)]
    llm = [_addr_span("llm:ADDRESS", 20, 25)]
    g, l = orchestration._apply_source_policy("both", guardrails, llm, *_ADDR_RULES)
    assert g == guardrails
    assert l == llm


def test_apply_source_policy_llm_drops_guardrails_address_only():
    guardrails = [_addr_span("guardrails:ADDRESS"), _addr_span("guardrails:NAME", 10, 15)]
    llm = [_addr_span("llm:ADDRESS", 20, 25)]
    g, l = orchestration._apply_source_policy("llm", guardrails, llm, *_ADDR_RULES)
    # Guardrails' ADDRESS is dropped; its NAME survives; the LLM's ADDRESS stays.
    assert [s.ruleName for s in g] == ["guardrails:NAME"]
    assert [s.ruleName for s in l] == ["llm:ADDRESS"]


def test_apply_source_policy_guardrails_drops_llm_address_but_not_ip_address():
    guardrails = [_addr_span("guardrails:ADDRESS")]
    llm = [
        _addr_span("llm:ADDRESS", 20, 25),
        _addr_span("llm:IP_ADDRESS", 30, 35),
        _addr_span("llm:NAME", 40, 45),
    ]
    g, l = orchestration._apply_source_policy("guardrails", guardrails, llm, *_ADDR_RULES)
    assert [s.ruleName for s in g] == ["guardrails:ADDRESS"]
    # Only the EXACT llm:ADDRESS is dropped; IP_ADDRESS/NAME are untouched.
    assert [s.ruleName for s in l] == ["llm:IP_ADDRESS", "llm:NAME"]


def test_apply_source_policy_unknown_value_is_treated_as_both():
    guardrails = [_addr_span("guardrails:ADDRESS")]
    llm = [_addr_span("llm:ADDRESS", 20, 25)]
    g, l = orchestration._apply_source_policy("banana", guardrails, llm, *_ADDR_RULES)
    assert g == guardrails and l == llm


def test_apply_source_policy_name_llm_drops_guardrails_name_only():
    """The NAME switch drops Guardrails' NAME (e.g. the “照片”/photo false
    positive) while keeping its ADDRESS and the LLM's NAME."""
    guardrails = [_addr_span("guardrails:NAME"), _addr_span("guardrails:ADDRESS", 10, 15)]
    llm = [_addr_span("llm:NAME", 20, 25)]
    g, l = orchestration._apply_source_policy("llm", guardrails, llm, *_NAME_RULES)
    assert [s.ruleName for s in g] == ["guardrails:ADDRESS"]
    assert [s.ruleName for s in l] == ["llm:NAME"]


def test_handle_job_llm_policy_drops_guardrails_address_before_redaction():
    """With `addressDetectionSource="llm"`, the Guardrails ADDRESS span is
    filtered out before the merge, so it never reaches `_redact_document`,
    while the Guardrails NAME span and the LLM ADDRESS span still do."""
    job = _make_job(
        extension=".pdf", file_name="source.pdf", address_detection_source="llm"
    )
    content = _content(text_units=["u0"], image_regions=[])
    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        pipeline.mocks["match_rules"].return_value = []
        pipeline.mocks["detect_pii_with_guardrails"].return_value = [
            _addr_span("guardrails:ADDRESS", 0, 5),
            _addr_span("guardrails:NAME", 10, 15),
        ]
        pipeline.mocks["detect_pii_with_llm"].return_value = [
            _addr_span("llm:ADDRESS", 20, 25)
        ]
        orchestration.handle_job(JOB_ID)

    redact_args, _ = pipeline.mocks["_redact_document"].call_args
    matches = redact_args[2]
    contributing = set()
    for span in matches:
        contributing.update(span.mergedFromRuleNames or [span.ruleName])
    assert "guardrails:ADDRESS" not in contributing
    assert "guardrails:NAME" in contributing
    assert "llm:ADDRESS" in contributing


def test_handle_job_name_llm_policy_drops_guardrails_name_before_redaction():
    """With `nameDetectionSource="llm"`, the Guardrails NAME span (e.g. the
    “照片” false positive) is filtered out before the merge, while the
    Guardrails ADDRESS span and the LLM NAME span still reach the redactor."""
    job = _make_job(
        extension=".pdf", file_name="source.pdf", name_detection_source="llm"
    )
    content = _content(text_units=["u0"], image_regions=[])
    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        pipeline.mocks["match_rules"].return_value = []
        pipeline.mocks["detect_pii_with_guardrails"].return_value = [
            _addr_span("guardrails:NAME", 0, 5),
            _addr_span("guardrails:ADDRESS", 10, 15),
        ]
        pipeline.mocks["detect_pii_with_llm"].return_value = [
            _addr_span("llm:NAME", 20, 25)
        ]
        orchestration.handle_job(JOB_ID)

    redact_args, _ = pipeline.mocks["_redact_document"].call_args
    matches = redact_args[2]
    contributing = set()
    for span in matches:
        contributing.update(span.mergedFromRuleNames or [span.ruleName])
    assert "guardrails:NAME" not in contributing
    assert "guardrails:ADDRESS" in contributing
    assert "llm:NAME" in contributing


def test_handle_job_default_both_policy_keeps_guardrails_address():
    """The default `"both"` policy leaves every source's ADDRESS spans intact
    (pre-feature behavior)."""
    job = _make_job(extension=".pdf", file_name="source.pdf")  # defaults to "both"
    content = _content(text_units=["u0"], image_regions=[])
    with _Pipeline(job, content, VerificationResult.success()) as pipeline:
        pipeline.mocks["match_rules"].return_value = []
        pipeline.mocks["detect_pii_with_guardrails"].return_value = [
            _addr_span("guardrails:ADDRESS", 0, 5),
            _addr_span("guardrails:NAME", 10, 15),
        ]
        pipeline.mocks["detect_pii_with_llm"].return_value = []
        orchestration.handle_job(JOB_ID)

    redact_args, _ = pipeline.mocks["_redact_document"].call_args
    matches = redact_args[2]
    contributing = set()
    for span in matches:
        contributing.update(span.mergedFromRuleNames or [span.ruleName])
    assert "guardrails:ADDRESS" in contributing
    assert "guardrails:NAME" in contributing
