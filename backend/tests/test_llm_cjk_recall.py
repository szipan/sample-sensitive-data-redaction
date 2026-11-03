r"""End-to-end `chi.pdf` / `chi.docx` LLM CJK PII recall integration test
 — the ONLY test that verifies this feature's
premise, that a Bedrock foundation model recovers Chinese PII the other
detection sources cannot read.

The gap this closes
-------------------

For a Chinese document, CJK-only PII reaches the redactor through exactly one
pre-existing route: the text layer feeding Bedrock Guardrails. Textract and
Bedrock Data Automation both return zero CJK characters. And even Guardrails,
which does read Chinese, still misses values: the VIN `LSVAU2A89CN123456` is
never detected in either `chi.pdf` or `chi.docx`, despite
`VEHICLE_IDENTIFICATION_NUMBER` being an enabled entity. This feature adds a
third source — `detect_pii_with_llm` — that reads the same extracted text and
returns PII spans as text, located locally by the same Occurrence_Scan the
Guardrails path uses.

What this test asserts, and what it does not
--------------------------------------------

Model output is not reproducible, so this test asserts a **recall floor**, not
an equality. Concretely, per document:

1. the Redaction Job reaches `completed`;
2. the recorded `detectionSummary` reports `llmSpans > 0` and
   `llmOnlySpans > 0` — the headline, that the LLM contributed regions no
   other source found;
3. the VIN `LSVAU2A89CN123456`, which Guardrails never detects, is covered by
   a merged span whose contributing rule names include an `llm:`-prefixed one
   — so an `llm:` contributor proves the LLM caught it;
4. a mainland Chinese resident identity number (`llm:CN_RESIDENT_ID_NUMBER`, a
   category Guardrails cannot even name) and a Chinese personal name
   (`llm:NAME`) each appear among the contributing rule names.

Every assertion is one-sided: a run may find MORE than this floor. Passing
does not certify the model — only that the path works end to end on the
documents that exposed the gap.

Running it deliberately
-----------------------

This test calls REAL Amazon Bedrock (the `Converse` foundation-model API and
`ApplyGuardrail`), so it costs money, needs credentials AND model access
enabled for the account/Region, and reads files that exist only on the machine
where the documents were collected. It is therefore guarded by an environment
variable and skips (never fails) when anything it needs is missing, so
`python -m pytest tests -q` stays offline and free.

There is no pytest configuration file in this repository, so an
environment-variable guard is used rather than a custom marker (mirroring
`test_patti_regression.py`). From `backend/`:

    $env:RUN_LLM_CJK_RECALL = "1"
    $env:LLM_PII_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    $env:GUARDRAIL_ID = "sbw1o5pg4f92"
    $env:GUARDRAIL_VERSION = "1"
    $env:AWS_DEFAULT_REGION = "us-west-2"
    .venv\Scripts\python.exe -m pytest tests\test_llm_cjk_recall.py -q -s

`CHI_PDF_PATH` / `CHI_DOCX_PATH` override the document locations. The test
prints every number it measured, so a run doubles as an audit record.

Invoke it as its own pytest run, not as part of `pytest tests`: sibling
modules install moto's fake AWS credentials into `os.environ` at module scope
for the rest of the process, so in a whole-suite run the credential probe
below finds unusable credentials and skips.

Requirements: 1.1, 1.9, 3.3, 7.1, 7.2.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import patch

import pdfminer  # noqa: F401 - imported like Patti so a missing PDF library fails collection loudly, matching the suite's other PDF tests
import pikepdf  # noqa: F401
import pypdfium2  # noqa: F401
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent import orchestration  # noqa: E402
from agent.tools.job_status_update import STATUS_COMPLETED  # noqa: E402
from shared.models import MatchSpan, RedactionJob  # noqa: E402

#: Set this to run the test. Absent -> skipped, so the default suite stays
#: offline and free.
RUN_FLAG_ENV_VAR = "RUN_LLM_CJK_RECALL"

#: The Guardrails configuration the tool reads. Both must be set so the OTHER
#: sources run too and `llmOnlySpans` is a meaningful "only the LLM found it".
GUARDRAIL_ENV_VARS = ("GUARDRAIL_ID", "GUARDRAIL_VERSION")

#: The pinned Model_Id the LLM tool reads. Without it the LLM source is
#: dormant and the test would prove nothing, so its absence is a skip.
MODEL_ID_ENV_VAR = "LLM_PII_MODEL_ID"

#: The value Guardrails never detects in either document — its coverage here,
#: with an `llm:` contributor, is the single clearest proof the LLM source
#: Added recall.
VIN = "LSVAU2A89CN123456"

#: `ruleName` prefix marking a contribution from the LLM source.
_LLM_PREFIX = "llm:"

#: The two Chinese-category contributing rule names the recall floor requires.
LLM_RESIDENT_ID_RULE = "llm:CN_RESIDENT_ID_NUMBER"
LLM_NAME_RULE = "llm:NAME"

#: (path env var, default path, source extension, file name). Parameterized so
#: the same recall-floor assertions run against both Chinese documents.
DOCUMENTS = [
    (
        "CHI_PDF_PATH",
        Path(r"D:\Users\szipan\Downloads\test\chi.pdf"),
        ".pdf",
        "chi.pdf",
    ),
    (
        "CHI_DOCX_PATH",
        Path(r"D:\Users\szipan\Downloads\test\chi.docx"),
        ".docx",
        "chi.docx",
    ),
]

JOB_ID = "chi-recall-0000-0000-0000-000000000000"

#: The real dispatcher, captured before it is patched, so the capturing
#: side_effect can delegate to it — the redacted document is still produced
#: and structure verification still runs (we only observe the merged spans).
_REAL_REDACT_DOCUMENT = orchestration._redact_document


# ---------------------------------------------------------------------------
# Guards: everything missing is a skip, never a failure
# ---------------------------------------------------------------------------


def _skip_unless_runnable(path_env_var: str, default_path: Path) -> Path:
    """Return the document path, or skip with the reason it cannot run."""
    if not (os.environ.get(RUN_FLAG_ENV_VAR) or "").strip():
        pytest.skip(
            "{}, calls real Bedrock (Converse + Guardrails); set {}=1 to run "
            "(see this module's docstring)".format(__name__, RUN_FLAG_ENV_VAR)
        )

    doc_path = Path(os.environ.get(path_env_var) or default_path)
    if not doc_path.is_file():
        pytest.skip(
            "source document not found at {}; set {} to its location".format(
                doc_path, path_env_var
            )
        )

    if not (os.environ.get(MODEL_ID_ENV_VAR) or "").strip():
        # Without a pinned model id the LLM source is dormant and the test
        # would assert nothing about the feature.
        pytest.skip(
            "{} unset; the LLM source would be dormant".format(MODEL_ID_ENV_VAR)
        )

    missing = [
        name
        for name in GUARDRAIL_ENV_VARS
        if not (os.environ.get(name) or "").strip()
    ]
    if missing:
        # If Guardrails is unconfigured, every non-LLM source contributes
        # nothing and `llmOnlySpans` becomes trivially satisfiable, so the
        # test would no longer prove the LLM found something the OTHERS did
        # not.
        pytest.skip(
            "Guardrails not configured; {} unset".format(", ".join(missing))
        )

    try:
        import boto3
    except ImportError:  # pragma: no cover - boto3 is a project dependency
        pytest.skip("boto3 is not installed")

    if boto3.session.Session().get_credentials() is None:
        pytest.skip("no AWS credentials available for Bedrock")

    # Credentials *existing* is not enough: several sibling test modules set
    # moto's fake keys into `os.environ` at module scope for the rest of the
    # process, so in a whole-suite run this test would inherit credentials
    # that look present and are rejected by Bedrock. Probe them once, and skip
    # rather than fail if they are not usable.
    try:
        boto3.client("sts").get_caller_identity()
    except Exception as error:  # noqa: BLE001 - any failure means "cannot run"
        pytest.skip(
            "AWS credentials are not usable ({}); if this was a whole-suite "
            "run, other modules replaced them with moto's fake keys - invoke "
            "this file on its own".format(type(error).__name__)
        )

    return doc_path


# ---------------------------------------------------------------------------
# The pipeline run
# ---------------------------------------------------------------------------


def _make_job(extension: str, file_name: str) -> RedactionJob:
    return RedactionJob(
        jobId=JOB_ID,
        status="processing",
        sourceKey="{}/{}".format(JOB_ID, file_name),
        sourceExtension=extension,
        fileName=file_name,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )


def _run_pipeline(source_bytes: bytes, extension: str, file_name: str) -> dict:
    """Run the real `handle_job` over `source_bytes` and capture what it
    stored, plus the MERGED spans that reached the redactor.

    Only the infrastructure boundaries are replaced — the Job read, the
    source-bytes read, the enabled-rules read (deliberately empty: no regex
    rule matches Chinese names or the VIN, so any detection of them must come
    from Guardrails or the LLM, and the VIN's `llm:` provenance is
    unambiguous), and the two writes. `_redact_document` is wrapped rather
    than replaced: the side_effect records the `match_spans` argument and then
    delegates to the real dispatcher, so the redacted document is still
    produced and `verify_structure` still runs. Everything the feature
    consists of — Guardrails detection, the LLM `Converse` calls, merging,
    redaction, verification — runs for real.
    """
    captured: dict = {
        "status": None,
        "error": None,
        "summary": None,
        "redacted": None,
        "spans": None,
    }

    def _capture_store(redacted_bytes: bytes, job_id: str, name: str) -> str:
        captured["redacted"] = redacted_bytes
        return "{}/{}".format(job_id, name)

    def _capture_update(
        job_id: str,
        status: str,
        redacted_key: Optional[str] = None,
        error_message: Optional[str] = None,
        detection_summary: Optional[dict] = None,
        error_code: Optional[str] = None,
        error_params: Optional[dict] = None,
    ) -> None:
        captured["status"] = status
        captured["error"] = error_message
        captured["summary"] = detection_summary

    def _capture_redact(
        file_bytes,
        text_units,
        match_spans,
        ext,
        ocr_text_units=None,
    ):
        # Record the merged spans reaching the redactor, then delegate to the
        # real dispatcher so redaction + structure verification still happen.
        captured["spans"] = list(match_spans)
        return _REAL_REDACT_DOCUMENT(
            file_bytes, text_units, match_spans, ext, ocr_text_units
        )

    with patch.object(
        orchestration, "_load_job", return_value=_make_job(extension, file_name)
    ), patch.object(
        orchestration, "_read_source_bytes", return_value=source_bytes
    ), patch.object(
        orchestration, "_load_enabled_rules", return_value=[]
    ), patch.object(
        orchestration, "store_redacted_document", side_effect=_capture_store
    ), patch.object(
        orchestration, "update_job_status", side_effect=_capture_update
    ), patch.object(
        orchestration, "_redact_document", side_effect=_capture_redact
    ):
        orchestration.handle_job(JOB_ID)

    return captured


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contributing_names(span: MatchSpan) -> List[str]:
    """Every rule name that contributed to `span` — `mergedFromRuleNames` when
    present and non-empty, else `[ruleName]` (mirrors
    `orchestration._llm_only_span_count` / `matching._contributing_rule_names`).
    """
    return span.mergedFromRuleNames or [span.ruleName]


def _locate_vin(source_bytes: bytes, extension: str) -> Optional[Tuple[str, int, int]]:
    """Find the VIN in the extracted text-layer units and return
    `(unitRef, start, end)`, or `None` if the extractor never surfaced it.

    Runs the real `_extract_document_content` — extraction is deterministic,
    so the `text:{i}` index here matches the index the pipeline's own
    extraction produced.
    """
    content = orchestration._extract_document_content(source_bytes, extension)
    for index, unit in enumerate(content.textUnits):
        offset = (unit.text or "").find(VIN)
        if offset != -1:
            return "text:{}".format(index), offset, offset + len(VIN)
    return None


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path_env_var,default_path,extension,file_name",
    DOCUMENTS,
    ids=["chi.pdf", "chi.docx"],
)
def test_llm_cjk_recall_floor(
    path_env_var: str,
    default_path: Path,
    extension: str,
    file_name: str,
):
    """The LLM source recovers the VIN Guardrails misses, plus a Chinese
    resident identity number and a personal name, and the recorded
    `detectionSummary` reports it contributed regions no other source found
    ."""
    doc_path = _skip_unless_runnable(path_env_var, default_path)
    source_bytes = doc_path.read_bytes()

    captured = _run_pipeline(source_bytes, extension, file_name)

    # --- 1. The job completed ----------------------------------------------
    assert captured["status"] == STATUS_COMPLETED, (
        "job did not complete ({}): {}".format(captured["status"], captured["error"])
    )
    assert captured["redacted"], "no redacted document was stored"

    spans: List[MatchSpan] = captured["spans"] or []
    llm_rule_names = sorted(
        {
            name
            for span in spans
            for name in _contributing_names(span)
            if name.startswith(_LLM_PREFIX)
        }
    )

    # --- 2. The headline: the LLM contributed regions no other source found -
    summary = captured["summary"]
    assert summary is not None, "a completed job must record a detectionSummary"
    print(
        "\n[measured] {} detectionSummary: llmSpans={} llmOnlySpans={}".format(
            file_name, summary.get("llmSpans"), summary.get("llmOnlySpans")
        )
    )
    print("[measured] {} distinct llm: rule names: {}".format(file_name, llm_rule_names))
    assert summary.get("llmSpans", 0) > 0, (
        "expected the LLM source to produce at least one span; summary={}".format(
            summary
        )
    )
    assert summary.get("llmOnlySpans", 0) > 0, (
        "expected at least one merged region only the LLM found; summary={}".format(
            summary
        )
    )

    # --- 3. VIN coverage with LLM provenance --------
    located = _locate_vin(source_bytes, extension)
    if located is None:
        # An instrument failure, not a recall regression: we cannot assert
        # coverage of a value the extractor never surfaced. Skip (this is the
        # optional live test) rather than fail.
        pytest.skip(
            "instrument: the VIN {!r} was not found in the extracted text of "
            "{}, so its coverage cannot be measured (font/extraction "
            "surprise)".format(VIN, file_name)
        )

    vin_unit_ref, vin_start, vin_end = located
    vin_covering_spans = [
        span
        for span in spans
        if span.unitRef == vin_unit_ref
        and span.startOffset <= vin_start
        and span.endOffset >= vin_end
    ]
    vin_llm_spans = [
        span
        for span in vin_covering_spans
        if any(name.startswith(_LLM_PREFIX) for name in _contributing_names(span))
    ]
    print(
        "[measured] {} VIN at {}[{}:{}]; covering spans={} with llm provenance={} "
        "contributors={}".format(
            file_name,
            vin_unit_ref,
            vin_start,
            vin_end,
            len(vin_covering_spans),
            len(vin_llm_spans),
            [_contributing_names(span) for span in vin_covering_spans],
        )
    )
    assert vin_llm_spans, (
        "the VIN {!r} at {}[{}:{}] is not covered by any merged span with an "
        "'llm:' contributor; Guardrails never detects this VIN, so an 'llm:' "
        "contributor is what proves the LLM caught it. Covering spans: {}".format(
            VIN,
            vin_unit_ref,
            vin_start,
            vin_end,
            [_contributing_names(span) for span in vin_covering_spans],
        )
    )

    # --- 4. Chinese-category recall floor -----------------------------------
    assert LLM_RESIDENT_ID_RULE in llm_rule_names, (
        "expected at least one span carrying {!r} (the LLM categorized a "
        "mainland resident identity number, a category Guardrails cannot "
        "name); saw llm: rule names {}".format(LLM_RESIDENT_ID_RULE, llm_rule_names)
    )
    assert LLM_NAME_RULE in llm_rule_names, (
        "expected at least one span carrying {!r} (a Chinese personal name); "
        "saw llm: rule names {}".format(LLM_NAME_RULE, llm_rule_names)
    )
