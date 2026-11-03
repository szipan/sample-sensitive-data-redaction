# Implementation Plan: LLM-Based CJK PII Recall Path

## Overview

Implemented in Python (`backend/agent`, `backend/infra`) and TypeScript (`frontend`), matching the existing codebase. The order is: extract the shared offset-recovery helper first (so the new tool has something to import instead of a copy), build the LLM detection tool in isolation, wire it into `handle_job` at a fixed position, extend the detection summary, then surface the count through the API and UI.

Two things are deliberately **not** changed, and tasks exist to prove that rather than to do it: `merge_spans_within_unit` / `_regroup_and_merge` (tasks 4.3–4.5), and `update_job_status` / `job_status_handler.py`, which are already shape-agnostic about `detectionSummary` (task 6.3).

The riskiest task is 1.4 — rewiring a shipped, tested file. Its acceptance bar is that `tests/test_guardrails_pii_detect.py` passes **unmodified**.

## Tasks

- [ ] 1. Extract the shared occurrence-scan helper
  - [x] 1.1 Create `backend/agent/tools/pii_offsets.py`
    - Move `_find_occurrences`, `_is_all_ascii_digits`, `_is_digit_flanked`, and `_ASCII_DIGITS` out of `guardrails_pii_detect.py` as public names, behaviour-identical
    - Add `OccurrenceResolution(occurrences, total_found, fallback_applied)` as a frozen dataclass
    - Add `resolve_match_occurrences(text, match_text) -> OccurrenceResolution`: scan all non-overlapping occurrences, apply the digit-boundary rule only when the match text is entirely ASCII digits, and emit all occurrences with `fallback_applied=True` when the rule rejects every one
    - Emit no log records from this module — callers log in their own vocabulary
    - Carry over the docstring rationale for the all-digits scoping, the either-side rejection, and the mandatory fallback, including the observed `456`-inside-`6222021234567890123` and `34`-inside-`京A·12345` failures
    - _Requirements: 2.1, 2.2, 2.3, 2.8_

  - [ ]* 1.2 Write property test for occurrence-scan completeness
    - **Property 5: The occurrence scan is complete and non-overlapping**
    - **Validates: Requirements 2.1, 2.8**

  - [ ]* 1.3 Write property test for the digit-boundary rule and its fallback
    - **Property 6: The digit-boundary rule filters exactly the flanked occurrences, and never everything**
    - **Validates: Requirements 2.2, 2.3**
    - Generators must construct digit runs embedded in longer numbers explicitly rather than relying on random text reaching those shapes

  - [x] 1.4 Rewire `guardrails_pii_detect.py` onto the shared helper
    - Import from `pii_offsets` and delete the local copies
    - Keep `_find_occurrences`, `_is_all_ascii_digits`, `_is_digit_flanked` as module-level aliases so existing imports keep resolving
    - Drive `_spans_for_text`'s existing log branches from `resolution.total_found` and `resolution.fallback_applied` instead of recomputing, preserving the current message wording exactly
    - Acceptance bar: `tests/test_guardrails_pii_detect.py` passes with no edits
    - _Requirements: 2.5_

  - [ ]* 1.5 Write unit tests for the extraction's compatibility surface
    - Assert the three alias names still exist on `guardrails_pii_detect` and are the shared-helper functions
    - Assert `resolve_match_occurrences` reports `fallback_applied=True` only when every occurrence was rejected
    - _Requirements: 2.3, 2.5_

- [ ] 2. Implement the LLM detection tool
  - [x] 2.1 Create `backend/agent/tools/llm_pii_detect.py` with configuration, vocabulary, and prompt
    - Define `MODEL_ID_ENV_VAR = "LLM_PII_MODEL_ID"`, `PROMPT_VERSION`, `MAX_RETRIES = 3`, `RETRY_BASE_DELAY_SECONDS`, `MAX_OUTPUT_TOKENS`, `OCCURRENCE_WARN_THRESHOLD`, `UNSPECIFIED_ENTITY_TYPE`
    - Define `GUARDRAILS_ENTITY_TYPES` mirroring `redaction_stack.py`'s `pii_entity_types`, `CJK_EXTENSION_TYPES` containing `CN_RESIDENT_ID_NUMBER`, `CN_PASSPORT_NUMBER`, `CN_SOCIAL_CREDIT_CODE`, and `ENTITY_VOCABULARY` as their union; duplicate rather than import, since `agent/` must not import `infra/`
    - Write `PROMPT_TEMPLATE`: JSON-array-only output contract, the verbatim-substring requirement stated twice, the explicit recall bias, an empty array when nothing is found, and the full vocabulary with the Chinese priorities called out
    - Implement `_is_pinned_model_id`: accept a trailing `-v\d+:\d+` segment, reject blank, unversioned, and `latest`-suffixed values
    - Implement `_read_model_config` returning `None` with a warning when the variable is absent, blank, or not pinned
    - Implement the lazy `_get_bedrock_runtime_client`, mirroring `guardrails_pii_detect.py`
    - _Requirements: 1.6, 1.7, 1.8, 1.13, 4.3, 5.3, 5.4, 5.5_

  - [x]* 2.2 Write property test for model-identifier gating
    - **Property 16: Requests are issued only for a pinned model identifier**
    - **Validates: Requirements 4.3, 5.3, 5.4**

  - [x] 2.3 Implement response parsing
    - Implement `_parse_detections(response) -> List[dict]`: concatenate the `text` blocks of `output.message.content`, strip an optional markdown fence, extract the outermost `[` … `]`, `json.loads` it
    - Keep only elements that are objects carrying a non-empty string `match`; map a missing or out-of-vocabulary `type` to `UNSPECIFIED` rather than dropping the detection
    - Never raise: any parse failure logs a warning and returns an empty list
    - _Requirements: 1.10, 1.11_

  - [x]* 2.4 Write property test for total response parsing
    - **Property 4: Response parsing is total**
    - **Validates: Requirement 1.11**

  - [x] 2.5 Implement the Converse call with retry
    - Implement `_converse_with_retry(client, model_id, text) -> Optional[dict]` calling `client.converse` with `system=[{"text": PROMPT_TEMPLATE}]`, the unit text as the sole user content block, and `inferenceConfig={"temperature": 0.0, "topP": 1.0, "maxTokens": MAX_OUTPUT_TOKENS}`
    - Reuse `guardrails_pii_detect._is_retryable`'s duck-typed error inspection so the module stays testable with a plain fake client and importable without botocore
    - Retry throttling and 5xx with exponential backoff, `MAX_RETRIES` total attempts; return `None` after exhaustion or on a non-retryable error, having logged a warning
    - _Requirements: 4.1, 4.2, 5.1, 5.2_

  - [x]* 2.6 Write property test for retry accounting
    - **Property 12: Transient failures are retried up to three attempts**
    - **Validates: Requirement 4.1**

  - [x]* 2.7 Write property test for the pinned inference configuration
    - **Property 15: Every request pins the inference configuration**
    - **Validates: Requirements 5.1, 5.2**

  - [x] 2.8 Implement span emission, the audit record, and the public entrypoint
    - Implement `_spans_for_text(client, model_id, zone, unit_ref, text)`: for each parsed detection, call `resolve_match_occurrences`, emit one `MatchSpan` per emitted occurrence with `ruleName = f"llm:{type}"`, skip detections whose match text is absent (debug log), warn on the fallback and above `OCCURRENCE_WARN_THRESHOLD`
    - Emit one audit record per completed request at info level carrying `unitRef`, model id, `PROMPT_VERSION`, the SHA-256 digest and character length of the exact text sent, and each entity type with its emitted offset ranges — and neither the unit text nor any match text
    - Implement `detect_pii_with_llm(text_units, image_regions, ocr_text_units=None)` with the same three-collection iteration order and `unitRef`/`zone` assignment as `detect_pii_with_guardrails`, skipping empty units
    - Wrap the whole iteration so any unexpected error returns the spans collected so far; never raise
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.9, 1.10, 1.12, 2.4, 2.6, 2.7, 4.4, 4.5, 4.6, 5.6, 5.7, 8.5_

  - [ ]* 2.9 Write property test for per-unit request accounting
    - **Property 1: One request per non-empty unit, none for empty ones**
    - **Validates: Requirements 1.1, 1.2**

  - [ ]* 2.10 Write property test for span namespace, index, and zone
    - **Property 2: Every span carries the right namespace, index, and zone**
    - **Validates: Requirements 1.4, 1.5, 6.5**

  - [ ]* 2.11 Write property test for entity-label to rule-name mapping
    - **Property 3: Entity labels map totally onto namespaced rule names**
    - **Validates: Requirements 1.9, 1.10**

  - [ ]* 2.12 Write property test for span validity and hallucinated matches
    - **Property 8: Every emitted span is valid and locatable**
    - **Validates: Requirements 2.4, 2.6**

  - [x]* 2.13 Write property test for the never-raise and partial-results contract
    - **Property 13: The tool never raises and never discards collected spans**
    - **Validates: Requirements 4.2, 4.5, 4.6**

  - [x]* 2.14 Write property test for the audit record
    - **Property 17: The audit record identifies the run without recording PII**
    - **Validates: Requirements 5.6, 5.7**

  - [x]* 2.15 Write property test for cross-tool offset equivalence
    - **Property 7: Both detection tools recover identical offsets**
    - **Validates: Requirement 2.5**
    - Drive both `detect_pii_with_llm` and `detect_pii_with_guardrails` with fakes returning the same single detection, and compare emitted offset ranges

  - [x]* 2.16 Write unit tests for the tool's call shape and structural guarantees
    - Two-positional-argument call works and `ocr_text_units` is last (_Requirements: 1.3_)
    - `PROMPT_TEMPLATE` states the JSON-array contract, the verbatim rule, and the recall bias (_Requirements: 1.6_)
    - `GUARDRAILS_ENTITY_TYPES` equals `redaction_stack.py`'s `pii_entity_types` as a set, and `CN_RESIDENT_ID_NUMBER` is in `ENTITY_VOCABULARY` and in the prompt (_Requirements: 1.7, 1.8_)
    - The module imports no MCP client, no `strands`, and no sub-agent machinery, and exposes one public function (_Requirements: 1.12_)
    - `boto3.client` is constructed once across two invocations (_Requirements: 1.13_)
    - A client-construction failure yields an empty list and no exception (_Requirements: 4.4_)
    - The captured request body contains only the prompt and the unit text — no existing `MatchSpan` (_Requirements: 3.2, 3.5_)
    - The occurrence-count warning fires just above the threshold and not just below (_Requirements: 2.7_)
    - `PROMPT_VERSION` appears in the audit record, and `caplog` captures it through the module logger (_Requirements: 5.5, 8.5_)
    - Mock Bedrock with `unittest.mock`, not moto
    - _Requirements: 1.3, 1.6, 1.7, 1.8, 1.12, 1.13, 2.7, 3.2, 3.5, 4.4, 5.5, 8.5_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - `tests/test_guardrails_pii_detect.py` must be passing unmodified at this point

- [ ] 4. Wire the third source into the pipeline and extend the detection summary
  - [x] 4.1 Invoke `detect_pii_with_llm` at a fixed position in `handle_job`
    - Edit `backend/agent/orchestration.py`: after the Guardrails block and before merging, call the tool with the same `content.textUnits`, `content.imageRegions`, and `ocr_text_units` objects
    - Wrap it in its own `try/except` degrading to `[]`, deliberately mirroring the Guardrails block's shape, with a comment recording that this source is supplementary while `ocr_pdf_pages` is primary
    - Concatenate LLM spans **last** into `_regroup_and_merge`, so an existing source keeps the scalar `ruleName` and the `llm:` name lands in `mergedFromRuleNames`
    - Invoke it for both `.pdf` and `.docx` jobs
    - Leave `_regroup_and_merge` and `merge_spans_within_unit` untouched
    - _Requirements: 3.1, 3.5, 4.7, 4.8, 6.1, 6.2, 6.3, 6.4, 6.7, 6.8, 6.9, 5.8_

  - [ ]* 4.2 Write property test for job survival under LLM failure
    - **Property 14: An LLM failure never fails the job**
    - **Validates: Requirement 4.8**

  - [x]* 4.3 Write property test for additive merging across three sources
    - **Property 9: Merging is additive — coverage is the union of all three sources**
    - **Validates: Requirements 3.1, 3.3, 6.3**

  - [ ]* 4.4 Write property test for backward-compatible merging
    - **Property 10: An empty LLM contribution reproduces the pre-feature result**
    - **Validates: Requirements 3.4, 6.4**

  - [ ]* 4.5 Write property test for merged provenance
    - **Property 11: Merged provenance retains every contributing source**
    - **Validates: Requirement 6.6**

  - [x] 4.6 Extend the detection summary with the LLM counts
    - Implement `_llm_only_span_count(merged_spans)` counting merged spans whose every contributing rule name is `llm:`-prefixed, reading `mergedFromRuleNames` with a fallback to `ruleName` exactly as `matching._contributing_rule_names` does
    - Extend `_build_detection_summary` so `llmSpans` (the pre-merge LLM span count) and `llmOnlySpans` are always present, and the `textLayerRegions` / `ocrRegions` / `agreedRegions` / `pageHealth` keys are added **only** for `.pdf` — omitted, never zeroed, for `.docx`
    - Record a summary on the `completed` transition for every extension, replacing the current `.docx`-records-nothing branch
    - Log a warning naming the `jobId` when `llmOnlySpans` exceeds the over-redaction threshold
    - Read no summary value anywhere in the control flow
    - _Requirements: 7.1, 7.2, 7.3, 7.7, 7.8, 7.9_

  - [ ]* 4.7 Write property test for the detection summary shape
    - **Property 18: The detection summary reports LLM counts, with dual-path keys present only for PDFs**
    - **Validates: Requirements 7.1, 7.2, 7.3**

  - [ ]* 4.8 Write unit tests for the pipeline wiring
    - Call order: the LLM call sits between the Guardrails call and merging (_Requirements: 5.8, 6.1_)
    - Both detection tools receive the same collection objects by identity (_Requirements: 6.2_)
    - A `.pdf` job invokes the tool (_Requirements: 6.7_)
    - A `.docx` job invokes the tool and records a summary carrying only the LLM keys (_Requirements: 6.8, 7.3_)
    - The tool raising still lets the job reach `completed` (_Requirements: 4.7_)
    - The over-redaction warning fires just above the threshold and not just below (_Requirements: 7.7_)
    - The summary is built after merging and its result is passed only to `update_job_status` (_Requirements: 7.9_)
    - With `LLM_PII_MODEL_ID` unset, the job completes and the summary reports zero LLM spans (_Requirements: 8.4_)
    - _Requirements: 4.7, 5.8, 6.1, 6.2, 6.7, 6.8, 7.3, 7.7, 7.9, 8.4_

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Verify the persistence and API contracts carry the new keys unchanged
  - [ ]* 6.1 Write property test for the persistence round trip
    - **Property 19: The LLM counts survive the persistence round trip**
    - **Validates: Requirement 7.4**

  - [ ]* 6.2 Write property test for identity-attribute preservation
    - **Property 21: Recording the summary leaves identity attributes untouched**
    - **Validates: Requirement 7.8**

  - [ ]* 6.3 Write unit test asserting no change was needed in the persistence layer
    - Assert `update_job_status` writes an unrecognized-key summary through as-is and `job_status_handler`'s `_to_jsonable` serves it, so the LLM keys need no code change in either file
    - _Requirements: 7.4_

- [ ] 7. Surface the LLM-only count in the frontend
  - [x] 7.1 Make the dual-path fields optional and add the LLM fields
    - Edit `frontend/src/api/types.ts`: mark `textLayerRegions`, `ocrRegions`, `agreedRegions`, and `pageHealth` optional on `DetectionSummary`, and add required `llmSpans` and `llmOnlySpans`
    - _Requirements: 7.4_

  - [x] 7.2 Render the LLM-only count in `JobStatusView`
    - Edit `frontend/src/views/JobStatusView.tsx`: render the LLM-only region count when `status === 'completed'` and the summary is present
    - Render per-path counts only for the keys the summary actually carries, so the `.docx` shape does not print `undefined`
    - Keep the summary-absent rendering byte-identical to its current behavior, and keep the component a pure function of its props
    - _Requirements: 7.5, 7.6_

  - [x]* 7.3 Write property test for the rendering
    - **Property 20: The job status view renders the LLM count exactly when it applies**
    - **Validates: Requirements 7.5, 7.6**
    - Generators must include summaries with the dual-path keys absent

- [ ] 8. Provision the model and its permission in CDK
  - [x] 8.1 Add the model environment variable and the scoped invoke grant
    - Edit `backend/infra/redaction_stack.py`: add `"LLM_PII_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0"` alongside `GUARDRAIL_ID` / `GUARDRAIL_VERSION` / `PDF_OCR_DPI`
    - Add a `bedrock:InvokeModel` statement scoped to **both** the inference-profile ARN in this account/Region and the underlying `arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0`; a Converse call through a cross-Region inference profile authorizes against both, and granting only one produces `AccessDeniedException`
    - Add no wildcard model resource
    - Comment that clearing the environment variable is the documented off switch
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

  - [x]* 8.2 Write CDK template tests for the grant and the variable
    - Assert the synthesized agent execution role policy contains `bedrock:InvokeModel` on the inference-profile and foundation-model ARNs and on no wildcard model resource (_Requirements: 8.1_)
    - Assert `LLM_PII_MODEL_ID` is present in the Runtime's environment variables (_Requirements: 8.2_)
    - Assert the stack's literal satisfies `llm_pii_detect._is_pinned_model_id`, so the stack and the runtime agree by construction (_Requirements: 8.3_)
    - Assert no new frontend request field or control was introduced (_Requirements: 6.9_)
    - _Requirements: 6.9, 8.1, 8.2, 8.3_

- [ ] 9. Update the shared model docstrings
  - [x] 9.1 Record the third detection source in `shared/models.py`
    - Edit `backend/shared/models.py` and mirror into `backend/lambdas/shared/models.py`
    - `MatchSpan.mergedFromRuleNames`: name `llm:*` as a third possible contributor alongside regex rule names and `guardrails:*`
    - `RedactionJob.detectionSummary`: record that the LLM counts are always present, that the dual-path keys are **absent rather than zero** for `.docx`, and why a false zero was rejected
    - No shape changes — both fields already accommodate this feature
    - _Requirements: 7.1, 7.2, 7.3, 6.6_

- [ ] 10. Prove the recall gap is closed
  - [x]* 10.1 Write the `chi.pdf` / `chi.docx` recall integration test
    - Run the pipeline with a real model call against both Chinese documents
    - Assert a **recall floor**, not an equality: the VIN `LSVAU2A89CN123456` — which Guardrails never detects — is covered, and the Chinese resident identity number and personal names are covered
    - Assert the recorded `detectionSummary` reports a non-zero `llmOnlySpans`, i.e. the new source contributed regions no other source found
    - Marked optional because it costs a real model invocation; it is nevertheless the only test that verifies the feature's premise
    - _Requirements: 1.1, 1.9, 3.3, 7.1, 7.2_

- [x] 11. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Task 10.1 is the one worth not skipping: without it, the claim that this feature closes the Chinese recall gap is untested.
- Task 1.4 is the only task that modifies shipped, tested code. Its acceptance bar is that `tests/test_guardrails_pii_detect.py` passes with **no edits** — that is the check that the extracted Occurrence_Scan is behaviour-identical.
- No changes to `merge_spans_within_unit` or `_regroup_and_merge` are required. Tasks 4.3–4.5 verify that a third source works with the existing group-by-`unitRef` logic unchanged.
- No changes to `update_job_status`, `job_status_handler.py`, or any redaction/verification tool are required. Task 6.3 verifies the persistence layer is already shape-agnostic.
- No new third-party dependencies: `boto3`, Hypothesis, and fast-check are all already in use.
- Every property test uses Hypothesis (Python) or fast-check (TypeScript), runs at least 100 iterations, and is tagged `Feature: llm-cjk-pii-recall, Property {number}: {property title}`.
- Bedrock is mocked with `unittest.mock`, not moto, following `tests/test_guardrails_pii_detect.py`. Real model calls appear only in task 10.1.
- Backend tests run as two separate processes, with `tests\test_regroup_and_merge.py` listed first in the first command because it registers PDF-library/`docx` MagicMock `sys.modules` stubs while the PDF tests need the real PDF libraries:
  ```
  .venv\Scripts\python.exe -m pytest tests\test_regroup_and_merge.py tests\test_orchestration.py tests\test_matching.py tests\test_guardrails_pii_detect.py tests\test_llm_pii_detect.py tests\test_pii_offsets.py -q
  .venv\Scripts\python.exe -m pytest tests\test_extraction_pdf.py tests\test_redaction_pdf.py -q
  ```
  Current baselines are 97 and 19 passed. `python-docx` is not installed, so `docx`-importing test files fail collection; pre-existing and out of scope.
- **Operational prerequisite, outside this plan's scope:** application logs never reach CloudWatch, because `agent/runtime.py` installs no logging handler. Every observability control this feature defines — the audit record, the occurrence warning, the fallback warning, the over-redaction warning — is written and discarded until that is fixed. Only the `detectionSummary` counts survive, because they go to DynamoDB. See the design's "Dependencies and risks".

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "7.1", "8.1", "9.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "2.1", "7.2", "8.2"] },
    { "id": 2, "tasks": ["1.5", "2.3", "7.3"] },
    { "id": 3, "tasks": ["2.5"] },
    { "id": 4, "tasks": ["2.8"] },
    { "id": 5, "tasks": ["2.2", "2.4", "2.6", "2.7", "2.9", "2.10", "2.11", "2.12", "2.13", "2.14", "2.15", "2.16", "4.1"] },
    { "id": 6, "tasks": ["4.6"] },
    { "id": 7, "tasks": ["4.2", "4.3", "4.4", "4.5", "4.7", "4.8", "6.1", "6.2", "6.3"] },
    { "id": 8, "tasks": ["10.1"] }
  ]
}
```
