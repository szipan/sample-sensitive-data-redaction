"""Fixed-order deterministic orchestration for the AgentCore Redaction Agent

`handle_job(job_id)` runs the Redaction Job pipeline in a single, fixed
order — extract → embedded-image OCR (only when the document contains image
regions) → page OCR (`.pdf` only) → match (regex rules + Bedrock Guardrails
PII detection over BOTH the text layer and the OCR pages, merged) → redact →
verify → store → update — with no LLM-driven planning, so the control flow
is deterministic and testable (a fixed-order pipeline that does not depend on
LLM-driven tool planning).

Outcome rules:
    - When the Agent completes a Redaction Job, the Job status becomes
      `completed` (only reached when extraction, matching, redaction,
      structure verification, AND storage all succeed).
    - If the Agent cannot parse the document, hits an internal processing
      error, or otherwise cannot produce a Redacted Document, the Job status
      becomes `failed`.
    - If the source document's structure cannot be retained
      (`verify_structure` returns `ok=False`), the Job status becomes
      `failed` and the structurally-broken document is NOT stored.
    - The Job reaches `completed` if and only if processing AND the structure
      checks both succeed; any exception anywhere in the pipeline, or a
      failed structure check, drives the Job to `failed` — it is never left
      stuck in `processing` (a top-level try/except catches everything).

Scope note: this module is a plain, directly-callable
Python function. It intentionally does NOT implement the Strands Agents SDK
tool-sequence registration or the AgentCore Runtime `InvokeAgentRuntime`
entrypoint — that is a separate layer (`runtime.py` / `main.py`), which WRAPS this
`handle_job` function.

Extension dispatch (the `.docx`/`.pdf` tools deliberately do not share an
`extract_document_content(file_bytes, extension)` wrapper): the small
extension-dispatching helpers `_extract_document_content`
and `_redact_document` live here. An unsupported/unexpected extension is
treated as a processing failure (→ Job `failed`), even though in practice
`UploadHandler` (`lambdas/upload_handler.py`) only ever admits `.docx` and
`.pdf`.
"""
from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

import boto3

from agent.tools.confirmation_poll import poll_for_confirmation
from agent.tools.detection_results import (
    build_detection_results,
    filter_spans_by_selections,
    to_json_dict,
)
from agent.tools.extraction_docx import extract_docx_content
from agent.tools.extraction_pdf import extract_pdf_content
from agent.tools.guardrails_pii_detect import detect_pii_with_guardrails
from agent.tools.llm_pii_detect import detect_pii_with_llm
from agent.tools.job_status_update import (
    ERROR_OCR_FAILED,
    ERROR_OCR_PAGE_FAILED,
    ERROR_PROCESSING_FAILED,
    ERROR_REVIEW_TIMEOUT,
    ERROR_STRUCTURE_VERIFICATION_FAILED,
    STATUS_COMPLETED,
    STATUS_DETECTED,
    STATUS_FAILED,
    update_job_status,
)
from agent.tools.matching import match_rules, merge_spans_within_unit
from agent.tools.ocr import recognize_image_text
from agent.tools.ocr_pdf import OcrPathError, ocr_pdf_pages
from agent.tools.redaction_docx import redact_docx_document
from agent.tools.span_validation import filter_implausible_spans
from agent.tools.redaction_pdf import (
    PdfRedactionReport,
    PdfRedactionResult,
    redact_pdf_document_with_report,
)
from agent.tools.storage import store_redacted_document
from agent.tools.structure_verification import verify_structure
from shared.models import (
    ExtractedTextUnit,
    ImageRegion,
    MatchSpan,
    RedactionJob,
    RedactionRule,
    DETECTION_SOURCE_DEFAULT,
)
from shared.rules_repository import RulesRepository

SOURCE_BUCKET_NAME_ENV_VAR = "SOURCE_BUCKET_NAME"
JOBS_TABLE_NAME_ENV_VAR = "JOBS_TABLE_NAME"
OUTPUT_BUCKET_NAME_ENV_VAR = "OUTPUT_BUCKET_NAME"

EXTENSION_DOCX = ".docx"
EXTENSION_PDF = ".pdf"

#: Prefix marking a rule name as originating from the LLM detection source.
_LLM_RULE_NAME_PREFIX = "llm:"

#: The exact `ruleName` each supplementary source stamps on a switchable
#: entity span (`detect_pii_with_guardrails` -> `guardrails:{TYPE}`,
#: `detect_pii_with_llm` -> `llm:{TYPE}`). The detection-source switches filter
#: on these EXACT strings, never a suffix, so the LLM's `IP_ADDRESS`/
#: `MAC_ADDRESS` entities are never caught by the ADDRESS policy.
_GUARDRAILS_ADDRESS_RULE_NAME = "guardrails:ADDRESS"
_LLM_ADDRESS_RULE_NAME = "llm:ADDRESS"
_GUARDRAILS_NAME_RULE_NAME = "guardrails:NAME"
_LLM_NAME_RULE_NAME = "llm:NAME"

#: llmOnlySpans count above which the over-redaction warning fires.
#: llmOnlySpans are regions ONLY the LLM found; a dense
#: Chinese page legitimately yields a few dozen unique CJK PII values (names,
#: addresses, IDs) no other source detects, so this sits above that and flags
#: a document where the model is painting far more boxes than plausible unique
#: PII. Heuristic and log-only — nothing branches on it.
LLM_ONLY_SPAN_WARN_THRESHOLD = 50

logger = logging.getLogger(__name__)


class OrchestrationError(Exception):
    """Raised for orchestration-level processing failures that are not
    already covered by a tool-specific exception (e.g. an unsupported
    source extension, or a `RedactionJobs` record that cannot be found).

    A subclass of `Exception`, so it is caught by `handle_job`'s top-level
    handler and converted into a `failed` Job status like any other
    processing error.
    """


# Lazily-initialized AWS clients/resources so they are created once per
# AgentCore Runtime process (warm-start reuse) rather than per invocation,
# matching the pattern used across this backend (e.g.
# `lambdas/upload_handler.py`, `agent/tools/storage.py`).
_jobs_table = None
_s3_client = None


def _get_jobs_table():
    global _jobs_table
    if _jobs_table is None:
        table_name = os.environ[JOBS_TABLE_NAME_ENV_VAR]
        _jobs_table = boto3.resource("dynamodb").Table(table_name)
    return _jobs_table


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _load_job(job_id: str) -> RedactionJob:
    """Read the `RedactionJobs` record for `job_id` and return it as a
    `RedactionJob` (mirrors the read pattern in
    `lambdas/job_status_handler.py`/`lambdas/download_handler.py`).

    Raises:
        OrchestrationError: if no record exists for `job_id`.
    """
    response = _get_jobs_table().get_item(Key={"jobId": job_id})
    item = response.get("Item")
    if item is None:
        raise OrchestrationError("No RedactionJob exists for jobId {!r}".format(job_id))
    return RedactionJob(
        jobId=item["jobId"],
        status=item["status"],
        sourceKey=item["sourceKey"],
        sourceExtension=item["sourceExtension"],
        fileName=item["fileName"],
        createdAt=item["createdAt"],
        updatedAt=item["updatedAt"],
        redactedKey=item.get("redactedKey"),
        errorMessage=item.get("errorMessage"),
        # Defaults to `DETECTION_SOURCE_DEFAULT` ("llm") when a record has no
        # value for these fields.
        addressDetectionSource=item.get("addressDetectionSource", DETECTION_SOURCE_DEFAULT),
        nameDetectionSource=item.get("nameDetectionSource", DETECTION_SOURCE_DEFAULT),
        # Defaults to "auto" for pre-feature records (backward-compatible).
        mode=item.get("mode", "auto"),
    )


def _read_source_bytes(source_key: str) -> bytes:
    """Fetch the source Supported Document's bytes from the source S3 bucket
    (`SOURCE_BUCKET_NAME`, the same variable `UploadHandler` writes to) by
    the Job's `sourceKey`."""
    bucket_name = os.environ[SOURCE_BUCKET_NAME_ENV_VAR]
    s3_object = _get_s3_client().get_object(Bucket=bucket_name, Key=source_key)
    return s3_object["Body"].read()


def _delete_source_document(source_key: str) -> None:
    """Remove the unredacted source once the Job has completed. Best effort:
    a failure is logged and left to the bucket's lifecycle expiry."""
    try:
        _get_s3_client().delete_object(
            Bucket=os.environ[SOURCE_BUCKET_NAME_ENV_VAR], Key=source_key
        )
    except Exception as exc:  # noqa: BLE001 - never fail a completed Job
        logger.warning(
            "Could not delete source document for completed Job: %s",
            type(exc).__name__,
        )


def _delete_detections(job_id: str) -> None:
    """Remove `{jobId}/detections.json` (matched values and context snippets)
    once the Job has left `detected`. Best effort: a failure is logged and
    left to the bucket's lifecycle expiry."""
    try:
        _get_s3_client().delete_object(
            Bucket=os.environ[OUTPUT_BUCKET_NAME_ENV_VAR],
            Key=f"{job_id}/detections.json",
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not change the Job outcome
        logger.warning(
            "Could not delete detections for Job %s: %s",
            job_id,
            type(exc).__name__,
        )


def _cause_type(exc: BaseException) -> str:
    """Type name of the exception's cause, for logs. Only type names are
    logged here: even this module's own errors wrap third-party messages
    (a Textract parameter error can quote the page image)."""
    cause = exc.__cause__ or exc.__context__
    return type(cause).__name__ if cause is not None else "none"


def _failure_message(exc: BaseException) -> str:
    """The `errorMessage` stored on a failed Job.

    The Job record is readable through the API and outlives the run, so it
    holds only the exception type (plus the audited extension message of
    `OrchestrationError`). Exception text from document libraries can quote
    the document itself, so it is stored neither here nor in the logs.
    """
    if isinstance(exc, OrchestrationError):
        return str(exc)
    return "Processing failed ({})".format(type(exc).__name__)


def _load_enabled_rules() -> List[RedactionRule]:
    """Load every stored Redaction Rule and return only the enabled ones."""
    return [rule for rule in RulesRepository().get_all() if rule.enabled]


def _extract_document_content(file_bytes: bytes, extension: str):
    """Extension-dispatching wrapper for `extract_document_content`
    (design.md tool table). Delegates to the `.docx` or `.pdf` extractor.

    Raises:
        OrchestrationError: for any extension other than `.docx`/`.pdf`.
    """
    if extension == EXTENSION_DOCX:
        return extract_docx_content(file_bytes)
    if extension == EXTENSION_PDF:
        return extract_pdf_content(file_bytes)
    raise OrchestrationError("Unsupported source extension: {!r}".format(extension))


def _redact_document(
    file_bytes: bytes,
    text_units: List[ExtractedTextUnit],
    match_spans: List[MatchSpan],
    extension: str,
    ocr_text_units: Optional[List[ExtractedTextUnit]] = None,
    image_regions: Optional[List[ImageRegion]] = None,
) -> PdfRedactionResult:
    """Extension-dispatching wrapper for `redact_document` (design.md tool
    table). Delegates to the `.docx` or `.pdf` redactor, threading the exact
    same `text_units` list produced by extraction and passed to
    `match_rules` — and, for `.pdf`, the exact same `ocr_text_units` list
    produced by `ocr_pdf_pages` and passed to the detection tools (both
    redaction tools' documented precondition: `"text:{i}"` indexes
    `text_units`, `"ocr:{i}"` indexes `ocr_text_units`).

    `image_regions` (in-image-redaction feature) is the exact same
    `content.imageRegions` list produced by extraction and mutated in place
    by `recognize_image_text`, forwarded to whichever redactor is dispatched
    so that `"image:{i}"`-namespaced spans can be acted on (`"image:{i}"`
    indexes this list). Defaulting to `None` preserves the pre-feature
    behavior — an unchanged image content region — for every existing caller.

    Returns:
        A `PdfRedactionResult` for both extensions, so the caller has a
        single shape to consume. `.pdf` carries the redactor's real
        `PdfRedactionReport`; `.docx` carries an all-zero report, since the
        `.docx` redactor reports no per-path counts and `.docx` is
        deliberately single-path. A `.docx` Job's
        Detection_Summary carries only the two LLM counts and OMITS the
        dual-path keys entirely, so this all-zero report
        is never surfaced for a `.docx`.

    Raises:
        OrchestrationError: for any extension other than `.docx`/`.pdf`.
    """
    if extension == EXTENSION_DOCX:
        return PdfRedactionResult(
            documentBytes=redact_docx_document(
                file_bytes, text_units, match_spans, image_regions
            ),
            report=PdfRedactionReport(
                textLayerRegions=0, ocrRegions=0, agreedRegions=0
            ),
        )
    if extension == EXTENSION_PDF:
        return redact_pdf_document_with_report(
            file_bytes, text_units, match_spans, ocr_text_units, image_regions
        )
    raise OrchestrationError("Unsupported source extension: {!r}".format(extension))


def _non_whitespace_count(text: str) -> int:
    """Number of non-whitespace characters in `text`.

    Whitespace is excluded because the two detection paths disagree about it
    by construction and not by defect: the text layer's per-character traversal
    records the document's own spacing characters, while the OCR_Path
    synthesizes separators (`" "` within a recognized line, `"\\n"` between
    lines). Counting whitespace would therefore make every page look
    asymmetric and drown out the signal this diagnostic exists to expose —
    characters the text layer *lost*.
    """
    return sum(1 for character in text if not character.isspace())


def _non_whitespace_count_for_page(
    text_units: List[ExtractedTextUnit], page_index: int
) -> int:
    """Total non-whitespace characters the Text_Layer_Path recorded for the
    0-based source page `page_index`.

    Sums across every text unit whose `position.pageIndex` matches, since
    the `.pdf` extractor emits one unit per page but the signature is
    deliberately tolerant of several units sharing a page. Units with no
    `pageIndex` (`.docx` extraction never sets one) contribute nothing.

    Returns 0 for a page the text layer dropped entirely — which is exactly
    the interesting case: an `ocrChars` count well above a `textLayerChars`
    of 0 says the page rendered text that the text layer never reported.
    """
    return sum(
        _non_whitespace_count(unit.text)
        for unit in text_units
        if unit.position is not None and unit.position.pageIndex == page_index
    )


def _llm_only_span_count(merged_spans: List[MatchSpan]) -> int:
    """Number of merged spans whose EVERY contributing rule name is
    `"llm:"`-prefixed — i.e. regions no other detection source found.
    Contributing names are `span.mergedFromRuleNames`
    when present and non-empty, else `[span.ruleName]`."""
    count = 0
    for span in merged_spans:
        names = span.mergedFromRuleNames or [span.ruleName]
        if names and all(name.startswith(_LLM_RULE_NAME_PREFIX) for name in names):
            count += 1
    return count


def _build_detection_summary(
    extension: str,
    merged_spans: List[MatchSpan],
    llm_span_count: int,
    report: PdfRedactionReport,
    text_units: List[ExtractedTextUnit],
    ocr_text_units: List[ExtractedTextUnit],
) -> dict:
    """Build the Detection_Summary recorded on the Job's `completed`
    transition.

    The two LLM keys are ALWAYS present, for BOTH extensions: `llmSpans` is
    the pre-merge count of spans the LLM source produced, and `llmOnlySpans`
    the count of merged regions no other source found. The LLM source runs for
    `.pdf` AND `.docx`, so both genuinely have
    these counts to report.

    The four dual-path keys — `textLayerRegions`, `ocrRegions`,
    `agreedRegions`, `pageHealth` — are present ONLY for `.pdf` and are
    ABSENT (never zeroed) for `.docx`. A `.docx` is single-path by design,
    so a `0` there would falsely read as "both paths ran
    and found nothing"; omitting the keys says truthfully "there was no
    second path to compare against".

    Args:
        extension: the Job's `sourceExtension` (`.pdf` or `.docx`), deciding
            whether the dual-path keys are included.
        merged_spans: the post-merge spans, over which `llmOnlySpans` is
            computed.
        llm_span_count: the pre-merge number of spans the LLM source emitted
            (`llmSpans`).
        report: the `PdfRedactionReport` the redactor returned, holding the
            number of rectangles resolved from `text:` spans, from `ocr:`
            spans, and the count of Agreed_Regions.
        text_units: the Text_Layer_Path units from extraction — the exact
            list passed to `match_rules` and the redactor.
        ocr_text_units: the OCR_Path units from `ocr_pdf_pages`, one per
            page in page order, so enumerating them enumerates the
            document's pages exactly once each.

    Returns:
        A JSON-shaped `dict` (only `int`s, `str`s, lists and maps, so the
        `boto3.resource` serializer takes it as-is) whose keys match the
        `DetectionSummary` shape `GET /jobs/{jobId}` serves and the frontend
        types declare, byte-for-byte.

    The `pageHealth` entries are a diagnostic *output* only. Nothing in this
    module — or anywhere downstream — reads them back to decide whether to
    run a path, which spans to resolve, or which rectangles to apply.
    Both PDF paths always run and every resolved
    rectangle is always redacted; branching on a character-count heuristic
    would reintroduce, as a silent false negative, precisely the blind spot
    this feature removes.
    """
    summary: dict = {
        "llmSpans": llm_span_count,
        "llmOnlySpans": _llm_only_span_count(merged_spans),
    }
    if extension == EXTENSION_PDF:
        summary["textLayerRegions"] = report.textLayerRegions
        summary["ocrRegions"] = report.ocrRegions
        summary["agreedRegions"] = report.agreedRegions
        summary["pageHealth"] = [
            {
                "pageIndex": index,
                "textLayerChars": _non_whitespace_count_for_page(text_units, index),
                "ocrChars": _non_whitespace_count(unit.text),
            }
            for index, unit in enumerate(ocr_text_units)
        ]
    return summary


def _regroup_and_merge(all_spans: List[MatchSpan]) -> List[MatchSpan]:
    """Group `all_spans` by `unitRef` and merge overlapping/adjacent spans
    within each group, using the exact same merge semantics `match_rules`
    applies to its own regex matches (`merge_spans_within_unit`).

    This exists so spans coming from *different* detection sources — the
    regex `match_rules` tool and the Bedrock Guardrails PII detection tool —
    can be concatenated and then re-merged as one set, instead of each
    source independently producing spans that overlap each other.

    Args:
        all_spans: `MatchSpan`s from any source, in any order. Offsets are
            only meaningful within a single text unit / image region, so
            spans are only ever merged with spans sharing their `unitRef`.

    Returns:
        Merged spans with no two output spans overlapping within the same
        `unitRef`. `mergedFromRuleNames` is populated by
        `merge_spans_within_unit` whenever more than one distinct rule name
        contributed to a merged span (so a regex rule name and a
        `"guardrails:*"` name are both retained). Groups appear in
        first-seen `unitRef` order, matching `match_rules`' own output
        ordering, so passing regex-only spans through this function
        reproduces `match_rules`' result unchanged.
    """
    spans_by_unit_ref: dict[str, List[MatchSpan]] = {}
    unit_ref_order: List[str] = []
    for span in all_spans:
        if span.unitRef not in spans_by_unit_ref:
            spans_by_unit_ref[span.unitRef] = []
            unit_ref_order.append(span.unitRef)
        spans_by_unit_ref[span.unitRef].append(span)

    merged: List[MatchSpan] = []
    for unit_ref in unit_ref_order:
        merged.extend(merge_spans_within_unit(spans_by_unit_ref[unit_ref]))
    return merged


def _apply_source_policy(
    detection_source: str,
    guardrails_matches: List[MatchSpan],
    llm_matches: List[MatchSpan],
    guardrails_rule_name: str,
    llm_rule_name: str,
) -> tuple[List[MatchSpan], List[MatchSpan]]:
    """Filter one entity type's spans per a `both|llm|guardrails` switch,
    returning the (possibly trimmed) `(guardrails_matches, llm_matches)`.

    - `"both"` (default): both lists pass through unchanged — the pre-feature
      behavior (every source's spans for this entity are kept).
    - `"llm"`: drop Guardrails' spans of this entity (keeps the LLM's).
    - `"guardrails"`: drop the LLM's spans of this entity (keeps Guardrails').

    Applied BEFORE `_regroup_and_merge`, and scoped to the EXACT rule name
    passed in (e.g. `"guardrails:ADDRESS"` / `"llm:ADDRESS"`, or the NAME
    pair) — so it never touches other entity types (`IP_ADDRESS`,
    `MAC_ADDRESS`, IDs, ...) or the regex sources. An unrecognized value is
    treated as `"both"` (no filtering).
    """
    if detection_source == "llm":
        guardrails_matches = [
            span for span in guardrails_matches if span.ruleName != guardrails_rule_name
        ]
    elif detection_source == "guardrails":
        llm_matches = [
            span for span in llm_matches if span.ruleName != llm_rule_name
        ]
    return guardrails_matches, llm_matches


def handle_job(job_id: str) -> None:
    """Run the fixed-order Redaction Job pipeline for `job_id`.

    Pipeline:
        extract → embedded-image OCR (only when the document has image
        regions) → page OCR (`.pdf` only, the OCR_Path) → match → redact →
        verify → store → update.

    For a `.pdf`, detection runs over TWO independent sources whose results
    are unioned before redaction: the text layer's units (`text:{i}` spans)
    and the OCR_Path's per-page units (`ocr:{pageIndex}` spans). A `.docx`
    keeps running single-path.

    A THIRD, supplementary detection source — the Bedrock LLM foundation
    model (`detect_pii_with_llm`), which recalls Chinese CJK PII the other
    sources cannot read — runs over the same units for BOTH `.pdf` and
    `.docx`, with its spans concatenated LAST before merging so an existing
    source keeps the scalar `ruleName` and the `llm:` name lands in
    `mergedFromRuleNames`. Like Guardrails, it degrades gracefully and never
    fails the Job.

    The Job reaches `completed` ONLY when extraction, matching, redaction,
    structure verification, AND storage all succeed. Any exception raised
    anywhere in the pipeline, or a `verify_structure` result of `ok=False`,
    drives the Job to `failed` — it is never left stuck in `processing`.

    `_load_job` runs before the top-level try (mirroring design.md's
    pseudocode `job = load_job(jobId)` outside the try): if the Job record
    cannot even be read, there is no Job state to transition, so the error
    propagates to the caller (the Runtime entrypoint) rather than
    being swallowed.
    """
    job = _load_job(job_id)
    # Idempotency: a Job is created as `processing` and only ever dispatched
    # once in that state. If the same Job is delivered again (a DynamoDB stream
    # retry re-sends a whole batch), it is already `detected` / `completed` /
    # `failed`, and re-running would redo detection and overwrite results.
    if job.status != "processing":
        logger.info(
            "Skipping job %s: status is %r, not 'processing' (duplicate dispatch)",
            job_id,
            job.status,
        )
        return
    # Set once detections.json (matched values + context) has been written;
    # it is deleted again when the Job leaves `detected`, whatever the outcome.
    detections_written = False
    try:
        source_bytes = _read_source_bytes(job.sourceKey)

        content = _extract_document_content(source_bytes, job.sourceExtension)

        # Embedded-image OCR is invoked only when the document actually
        # contains image regions,
        # for both `.docx` and `.pdf`.
        #
        # `recognize_image_text` is now a supplementary, best-effort detection
        # source (llm-image-ocr design.md) and already promises never to
        # raise — it swallows every failure mode internally and leaves the
        # affected region's `recognizedText` unpopulated. So reaching this
        # `except` means a *bug* broke that promise (an error in the tool's
        # own error path, a `BaseException`-derived error, or a future
        # unguarded edit). Failing the whole Job over such a bug would violate
        # the never-fail guarantee, so it is caught here and the pipeline continues on
        # the un-OCR'd regions — but a CloudWatch warning is emitted rather
        # than swallowing silently, keeping the defect observable (defense in
        # depth with visibility). This puts embedded-image OCR in the same
        # fail-open bracket as `detect_pii_with_guardrails` and
        # `detect_pii_with_llm` below.
        if content.imageRegions:
            try:
                content.imageRegions = recognize_image_text(content.imageRegions)
            except Exception:  # noqa: BLE001 - supplementary source, best-effort
                logger.warning(
                    "Embedded-image OCR raised unexpectedly; continuing without it",
                    exc_info=False,
                )
                # regions keep their placeholder recognizedText

        # The OCR_Path: for `.pdf` only, rasterize every page and recognize it
        # with Textract, producing one `ocr:{pageIndex}` unit per page.
        # `.docx` stays single-path — `word/document.xml`
        # stores real characters, so the ToUnicode class of defect this path
        # exists to defeat cannot occur there, and no page rasterization
        # Textract call is made for it.
        #
        # Deliberately NOT wrapped in a local try/except, unlike the
        # Guardrails call below: for a `.pdf` the OCR_Path is one of exactly
        # two PRIMARY detection sources, and the only one that still works
        # when the text layer is corrupt. So an `OcrPathError` propagates to
        # the top-level handler and drives the Job to `failed`, carrying the
        # failing page index in `errorMessage`, with no Redacted Document
        # stored; a `.pdf` Job reaches
        # `completed` only when every page produced a unit.
        # Swallowing this failure would rebuild, one layer up, the exact
        # "reports completed while PII leaks" defect the feature removes.
        ocr_text_units: List[ExtractedTextUnit] = []
        if job.sourceExtension == EXTENSION_PDF:
            ocr_text_units = ocr_pdf_pages(source_bytes)

        enabled_rules = _load_enabled_rules()
        regex_matches = match_rules(
            content.textUnits, content.imageRegions, enabled_rules, ocr_text_units
        )

        # Bedrock Guardrails is a *supplementary* ML-based detection layer on
        # top of the regex rules, run over the very same text units and image
        # regions (the Guardrail id/version come from the tool's own
        # environment variables, so no user configuration is involved).
        #
        # `detect_pii_with_guardrails` already swallows every failure mode
        # internally and returns partial/empty results, so this try/except is
        # belt-and-braces (bedrock-guardrails-pii-integration design.md
        # "Error Scenario 4"). It exists to guarantee that a
        # Guardrails failure must NEVER reach the top-level handler below and
        # push the Job to `failed` — the Job continues on regex-only matches.
        #
        # The contrast with the `ocr_pdf_pages` call above is the point:
        # Guardrails SUPPLEMENTS the regex rules, so losing it degrades recall
        # while both primary paths still ran; the OCR_Path IS a primary path,
        # so losing it must fail the Job.
        try:
            guardrails_matches = detect_pii_with_guardrails(
                content.textUnits, content.imageRegions, ocr_text_units
            )
        except Exception:  # noqa: BLE001 - graceful degradation, see above
            guardrails_matches = []

        # The LLM foundation-model source is the THIRD supplementary detection
        # layer, run over the very same `textUnits` / `imageRegions` /
        # `ocr_text_units` objects passed to the regex and Guardrails sources.
        # It exists to recall Chinese CJK PII that
        # neither Textract's OCR nor Guardrails can read (national ID numbers,
        # Chinese names/addresses, plates). It is invoked for BOTH `.pdf` and `.docx`.
        #
        # `detect_pii_with_llm` already swallows every failure mode internally
        # (missing/unpinned model id, client-construction failure, per-unit
        # Converse failure, malformed response) and returns partial/empty
        # results, so this try/except is belt-and-braces, mirroring the
        # Guardrails block above. It guarantees that an LLM
        # failure must NEVER reach the top-level handler and push the Job to
        # `failed` — the Job continues on the other sources' matches.
        #
        # Like Guardrails and unlike `ocr_pdf_pages`, this source SUPPLEMENTS
        # the primary paths: losing it degrades recall while every primary
        # path still ran, whereas the OCR_Path IS a primary `.pdf` source and
        # its failure must fail the Job.
        try:
            llm_matches = detect_pii_with_llm(
                content.textUnits, content.imageRegions, ocr_text_units
            )
        except Exception:  # noqa: BLE001 - graceful degradation, see above
            llm_matches = []

        # All three sources emit spans in the same `unitRef` namespace, so they
        # are concatenated and re-merged as one set. Overlapping spans collapse
        # into their union, and `mergedFromRuleNames` retains every
        # contributing rule name. The LLM spans are
        # concatenated LAST: where another source already
        # covered a value, that source's scalar `ruleName` is kept and the
        # `llm:` name lands in `mergedFromRuleNames`, so provenance reads
        # "regex/Guardrails found it, the LLM agreed". With no Guardrails or
        # LLM spans this reproduces `match_rules`' output unchanged.
        # Apply the Job's per-entity detection-source switches (ADDRESS and
        # NAME) BEFORE the union+merge below, so the unselected source's spans
        # for that entity never reach the redactor. Each switch is scoped to
        # its ONE entity type; every other PII type is unaffected. Both default
        # to "both" (no filtering) for backward compatibility.
        # Precision layer: drop model spans whose text cannot be the entity
        # type they are labelled with (public URLs, `4c, 16G` as MAC_ADDRESS,
        # a bare `43` as AGE, run-on matches). Regex rule spans are untouched.
        # Fail-open, see `span_validation`.
        guardrails_matches = filter_implausible_spans(
            guardrails_matches, content.textUnits, content.imageRegions, ocr_text_units
        )
        llm_matches = filter_implausible_spans(
            llm_matches, content.textUnits, content.imageRegions, ocr_text_units
        )

        guardrails_matches, llm_matches = _apply_source_policy(
            job.addressDetectionSource,
            guardrails_matches,
            llm_matches,
            _GUARDRAILS_ADDRESS_RULE_NAME,
            _LLM_ADDRESS_RULE_NAME,
        )
        guardrails_matches, llm_matches = _apply_source_policy(
            job.nameDetectionSource,
            guardrails_matches,
            llm_matches,
            _GUARDRAILS_NAME_RULE_NAME,
            _LLM_NAME_RULE_NAME,
        )

        matches = _regroup_and_merge(
            regex_matches + guardrails_matches + llm_matches
        )

        # --- Review-mode branch ---
        # For review-mode jobs, build deduplicated detection results, persist
        # them to S3, transition the job to "detected", then poll for user
        # confirmation before proceeding with redaction. Auto-mode jobs skip
        # this block entirely and proceed straight to redaction unchanged.
        if job.mode == "review":
            detection_results = build_detection_results(
                matches, content.textUnits, ocr_text_units, content.imageRegions
            )

            # Persist Detection_Results to S3 as {jobId}/detections.json
            detections_json = json.dumps(to_json_dict(detection_results))
            _get_s3_client().put_object(
                Bucket=os.environ[OUTPUT_BUCKET_NAME_ENV_VAR],
                Key=f"{job_id}/detections.json",
                Body=detections_json.encode("utf-8"),
            )
            detections_written = True

            # Transition job status to "detected" so the frontend can
            # display the review UI.
            update_job_status(job_id, STATUS_DETECTED)

            # Poll DynamoDB for user confirmation (5s intervals, 30min max).
            # Returns None on timeout → user abandoned the review.
            confirmed_ids = poll_for_confirmation(job_id)

            if confirmed_ids is None:
                # Timeout: the user did not confirm within 30 minutes.
                # This means they abandoned the review — no point doing
                # redaction. Mark the job as failed and return immediately.
                update_job_status(
                    job_id, STATUS_FAILED,
                    error_message="Review timed out; the job was cancelled.",
                    error_code=ERROR_REVIEW_TIMEOUT,
                )
                return

            # Narrow spans to only those the user confirmed.
            matches = filter_spans_by_selections(
                matches, detection_results, confirmed_ids,
                content.textUnits, ocr_text_units, content.imageRegions,
            )

        # Both paths' spans travel to the redactor as ONE collection;
        # the redactor dispatches on each span's `unitRef`
        # prefix to decide which offset space its offsets live in.
        result = _redact_document(
            source_bytes,
            content.textUnits,
            matches,
            job.sourceExtension,
            ocr_text_units,
            content.imageRegions,
        )
        redacted = result.documentBytes

        verification = verify_structure(source_bytes, redacted, job.sourceExtension)
        if not verification.ok:
            # Structure preservation failed: mark failed and do NOT store a
            # structurally-broken document.
            update_job_status(
                job_id,
                STATUS_FAILED,
                error_message=verification.reason,
                error_code=ERROR_STRUCTURE_VERIFICATION_FAILED,
            )
            return

        # A Detection_Summary is now recorded on the `completed` transition for
        # EVERY Job, whatever its extension: the LLM source
        # runs for both `.pdf` and `.docx`, so both genuinely have LLM counts
        # to report. The two LLM keys are always present; the dual-path keys
        # are present only for `.pdf` (they answer "did the OTHER path see
        # this too?", a question a single-path `.docx` cannot pose) and are
        # OMITTED rather than zeroed for `.docx`, so a `0` is never mistaken
        # for "both paths ran and found nothing".
        detection_summary = _build_detection_summary(
            job.sourceExtension,
            matches,
            len(llm_matches),
            result.report,
            content.textUnits,
            ocr_text_units,
        )

        # Over-redaction observability: flag a Job where the
        # LLM contributed an implausibly large number of regions no other
        # source found. Log-only; nothing branches on it.
        llm_only_count = _llm_only_span_count(matches)
        if llm_only_count > LLM_ONLY_SPAN_WARN_THRESHOLD:
            logger.warning(
                "Job %s: LLM detection contributed %d LLM-only redaction "
                "region(s), above the over-redaction threshold of %d",
                job_id,
                llm_only_count,
                LLM_ONLY_SPAN_WARN_THRESHOLD,
            )

        redacted_key = store_redacted_document(redacted, job_id, job.fileName)
        update_job_status(
            job_id,
            STATUS_COMPLETED,
            redacted_key=redacted_key,
            detection_summary=detection_summary,
        )
        _delete_source_document(job.sourceKey)
    except OcrPathError as exc:
        # The OCR_Path is a primary `.pdf` source, so its failure fails the
        # Job (see the call site). The failing page, when known, is reported
        # to the UI as a 1-based number. The stored message names only the
        # page: the error text can embed a third-party error message.
        logger.error(
            "Job %s: OCR path failed (page %s, cause %s)",
            job_id,
            exc.pageIndex,
            _cause_type(exc),
        )
        if exc.pageIndex is None:
            update_job_status(
                job_id,
                STATUS_FAILED,
                error_message="OCR failed",
                error_code=ERROR_OCR_FAILED,
            )
        else:
            update_job_status(
                job_id,
                STATUS_FAILED,
                error_message="OCR failed on page {}".format(exc.pageIndex + 1),
                error_code=ERROR_OCR_PAGE_FAILED,
                error_params={"page": exc.pageIndex + 1},
            )
    except Exception as exc:  # noqa: BLE001 - top-level catch-all is intentional
        # Top-level catch-all: every unexpected error becomes a `failed` status,
        # so a Job is never left stuck in `processing`.
        logger.error(
            "Job %s failed: %s (cause %s)", job_id, type(exc).__name__, _cause_type(exc)
        )
        update_job_status(
            job_id,
            STATUS_FAILED,
            error_message=_failure_message(exc),
            error_code=ERROR_PROCESSING_FAILED,
        )
    finally:
        if detections_written:
            _delete_detections(job_id)
