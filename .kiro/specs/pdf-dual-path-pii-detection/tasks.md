# Implementation Plan: PDF Dual-Path PII Detection

## Overview

Implemented in Python (backend/agent, backend/lambdas, backend/infra) and TypeScript (frontend), matching the existing codebase. The order is: extend the data models, build the OCR path tool in isolation, teach the two detection tools to accept OCR units, teach the PDF redactor to resolve `ocr:` spans to rectangles, then wire everything through `handle_job` and surface the detection summary through the API and UI.

Span merging (`merge_spans_within_unit`, `_regroup_and_merge`) is deliberately **not** modified — the `ocr:` namespace works with the existing group-by-`unitRef` logic as-is. Tasks 7.3–7.5 exist to prove that rather than to change it.

## Tasks

- [x] 1. Extend shared data models
  - [x] 1.1 Add `OcrWord`, `ExtractedTextUnit.ocrWords`, the `"ocr"` zone value, and `RedactionJob.detectionSummary`
    - Edit `backend/shared/models.py` and mirror the identical changes into `backend/lambdas/shared/models.py`
    - Add `"ocr"` to `TextUnitZone` and `MatchZone`
    - Add `OcrWord(text, startOffset, endOffset, boundingBox)` with a docstring noting its `boundingBox` is Textract-normalized (0–1), unlike every other `BoundingBox` in the module
    - Add optional `ocrWords: Optional[List[OcrWord]] = None` to `ExtractedTextUnit` and optional `detectionSummary: Optional[dict] = None` to `RedactionJob`
    - Keep every addition optional and defaulted so existing constructor call sites are unaffected
    - _Requirements: 1.5, 1.6, 5.1_

- [x] 2. Implement the PDF OCR path tool
  - [x] 2.1 Create `backend/agent/tools/ocr_pdf.py` with `ocr_pdf_pages`
    - Implement `ocr_pdf_pages(file_bytes) -> List[ExtractedTextUnit]`: one unit per page, in page order, `zone="ocr"`, `position.pageIndex == list index`, `boundingBox = page rectangle`, `ocrWords` populated
    - Implement `_read_raster_dpi()` reading `PDF_OCR_DPI`, defaulting to 200 and clamping to `[72, 400]`
    - Implement `_render_page_png(page, dpi)` via `pypdfium2.PdfPage.render(scale=dpi/72)` encoded as PNG
    - Implement `_detect_with_retry` with 3 total attempts and exponential backoff for throttling/5xx, duck-typing the error shape as `guardrails_pii_detect._is_retryable` does
    - Implement the oversized-image fallback: re-render at 150 dpi once when the PNG exceeds Textract's synchronous input limit
    - Implement `_build_page_unit`: walk `LINE` blocks and their child `WORD` blocks, joining words with `" "` within a line and `"\n"` between lines, recording each word's offset range and normalized bounding box
    - Emit a unit with `text=""` for pages Textract returns no words for, so `ocr:{index}` always equals the page index
    - Define `OcrPathError` and raise it, carrying the failing page index and reason, when a page cannot be rasterized or recognized
    - Reuse the lazy Textract client pattern from `agent/tools/ocr.py`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 3.1, 3.2, 3.3, 3.4, 4.7_

  - [ ]* 2.2 Write property test for per-page unit and call accounting
    - **Property 1: The OCR path produces one unit and one Textract call per page**
    - **Validates: Requirements 1.1, 1.3**

  - [ ]* 2.3 Write property test for word offset bookkeeping
    - **Property 2: Every recorded word slices back to its own text**
    - **Validates: Requirements 1.4, 1.5**

  - [ ]* 2.4 Write property test for Textract retry accounting
    - **Property 11: Transient Textract failures are retried up to three attempts**
    - **Validates: Requirements 3.1, 3.2**

  - [ ]* 2.5 Write property test for raster resolution resolution
    - **Property 17: The raster resolution always resolves to a usable value**
    - **Validates: Requirement 4.7**

  - [ ]* 2.6 Write unit tests for the OCR tool's call shape and edge branches
    - Assert `PdfPage.render` receives the resolved dpi for each page (_Requirements: 1.2_)
    - Assert the oversized-image branch re-renders exactly once at 150 dpi (_Requirements: 3.2_)
    - Assert a page with no recognized words yields a unit with empty text rather than a failure
    - Mock Textract with `unittest.mock.patch`, not moto
    - _Requirements: 1.2, 3.2_

- [x] 3. Accept OCR units in both detection tools
  - [x] 3.1 Add the `ocr_text_units` collection to `match_rules`
    - Edit `backend/agent/tools/matching.py`: add `_ocr_unit_ref(index) -> "ocr:{index}"` and an optional `ocr_text_units` parameter
    - Iterate OCR units after text units and image regions, emitting spans with `zone=unit.zone`
    - Leave the group-by-`unitRef` and merge logic untouched
    - _Requirements: 1.7, 4.1_

  - [ ]* 3.2 Write property test for the OCR unitRef namespace
    - **Property 3: OCR spans carry the `ocr:{pageIndex}` namespace**
    - **Validates: Requirement 1.6**

  - [ ]* 3.3 Write property test for path-independent rule matching
    - **Property 4: Rule matching is independent of which path supplied the text**
    - **Validates: Requirement 1.7**

  - [x] 3.4 Add the `ocr_text_units` collection to `detect_pii_with_guardrails`
    - Edit `backend/agent/tools/guardrails_pii_detect.py`: add an optional `ocr_text_units` parameter, one `ApplyGuardrail` call per non-empty OCR unit, `unitRef="ocr:{index}"`
    - Preserve the existing never-raise posture and partial-result behavior
    - _Requirements: 1.8, 3.7_

  - [ ]* 3.5 Write property test for per-unit Guardrails invocation
    - **Property 5: Guardrails is invoked once per non-empty OCR unit**
    - **Validates: Requirement 1.8**

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Resolve OCR spans to page rectangles in the PDF redactor
  - [x] 5.1 Add OCR geometry resolution helpers to `redaction_pdf.py`
    - Implement `_normalized_bbox_to_page_rect(page_rect, box)` scaling normalized coordinates onto the page rectangle
    - Implement `_rects_for_ocr_span(page, unit, span)` returning the bounding box of every `OcrWord` whose offset range intersects the span's range
    - Raise `RedactionError` for an out-of-range `ocr:` index or a unit missing `pageIndex`/`ocrWords`, matching the existing text-layer precondition handling
    - _Requirements: 2.2, 2.3, 2.5_

  - [ ]* 5.2 Write property test for OCR span to word resolution
    - **Property 7: An OCR span resolves to exactly the words its offsets touch**
    - **Validates: Requirements 2.2, 2.5**

  - [ ]* 5.3 Write property test for the coordinate mapping
    - **Property 8: Normalized bounding boxes map onto the page rectangle**
    - **Validates: Requirement 2.3**

  - [x] 5.4 Dispatch on the `unitRef` prefix and return a provenance report
    - Add `PdfRedactionReport(textLayerRegions, ocrRegions, agreedRegions)` and `PdfRedactionResult(documentBytes, report)`
    - Add `redact_pdf_document_with_report(file_bytes, text_units, match_spans, ocr_text_units=None)`; keep `redact_pdf_document` as a bytes-returning wrapper so existing callers and tests are unaffected
    - Collect rectangles per page tagged with their originating path; dispatch `text:` to the existing glyph resolver and `ocr:` to `_rects_for_ocr_span`; keep skipping `image:` spans
    - Compute `agreedRegions` as the number of OCR rectangles intersecting at least one text-layer rectangle on the same page
    - Apply a `pdf_content_edit` glyph removal and black box for every rectangle from both paths, once per page
    - _Requirements: 2.1, 2.4, 2.6, 5.2, 5.3_

  - [ ]* 5.5 Write property test for redaction block application
    - **Property 9: Every resolved rectangle receives a solid redaction block**
    - **Validates: Requirements 2.4, 2.6**

  - [ ]* 5.6 Write property test for per-path region counts
    - **Property 18: Per-path region counts match the rectangles applied**
    - **Validates: Requirement 5.2**

  - [ ]* 5.7 Write property test for the agreement count
    - **Property 19: The agreement count is bounded and matches its definition**
    - **Validates: Requirement 5.3**

  - [ ]* 5.8 Write property test for the zero-match case
    - **Property 10: An empty match set leaves visible content unchanged**
    - **Validates: Requirement 2.7**

  - [ ]* 5.9 Write unit tests for rotated pages and cross-path overlap
    - Build a `/Rotate 90` page, resolve an OCR span, and assert the rectangle lands on the rendered word — verifying the page-rectangle rotation assumption rather than assuming it
    - Build one `text:` span and one overlapping `ocr:` span and assert both rectangles are applied and no residual text remains
    - _Requirements: 2.3, 2.6_

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Wire the second path through the orchestration pipeline
  - [x] 7.1 Invoke `ocr_pdf_pages` for `.pdf` jobs and thread OCR units through the pipeline
    - Edit `backend/agent/orchestration.py`: after extraction and embedded-image OCR, call `ocr_pdf_pages(source_bytes)` when `job.sourceExtension == EXTENSION_PDF`, outside any local try/except so `OcrPathError` reaches the top-level handler
    - Pass `ocr_text_units` to `match_rules`, `detect_pii_with_guardrails`, and `_redact_document`
    - Extend `_redact_document` with the `ocr_text_units` parameter and have it return a `PdfRedactionResult`; the `.docx` branch returns a zero-count report
    - Leave `_regroup_and_merge` and `merge_spans_within_unit` unmodified
    - Keep the Guardrails call wrapped in its existing try/except, so the error-handling asymmetry between the two paths is explicit in the code
    - _Requirements: 1.9, 1.10, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [ ]* 7.2 Write property test for unrecoverable OCR failures
    - **Property 12: Any unrecoverable page failure fails the job and stores nothing**
    - **Validates: Requirements 3.3, 3.4, 3.5, 3.6**

  - [ ]* 7.3 Write property test for union preservation across both paths
    - **Property 6: Merging preserves the union of both paths' detections**
    - **Validates: Requirement 1.9**

  - [ ]* 7.4 Write property test for namespace isolation during merging
    - **Property 13: Spans never merge across `unitRef` namespaces**
    - **Validates: Requirement 4.1**

  - [ ]* 7.5 Write property test for merge backward compatibility
    - **Property 14: Text-layer-only merging is unchanged**
    - **Validates: Requirement 4.2**

  - [x] 7.6 Build the Detection_Summary in the orchestration layer
    - Implement `_build_detection_summary(report, text_units, ocr_text_units)` producing `textLayerRegions`, `ocrRegions`, `agreedRegions`, and `pageHealth`
    - Implement `_non_whitespace_count_for_page(text_units, page_index)` summing non-whitespace characters across text units whose `position.pageIndex` matches, yielding 0 for pages the text layer dropped
    - Pass the summary to `update_job_status` on the `completed` transition
    - Read no health value anywhere in the control flow
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 7.7 Write property test for page-health completeness
    - **Property 20: The health signal covers every page exactly once**
    - **Validates: Requirement 5.4**

  - [ ]* 7.8 Write property test for routing independence from the health signal
    - **Property 21: The health signal never influences detection**
    - **Validates: Requirement 5.5**

  - [ ]* 7.9 Write unit tests for pipeline wiring regressions
    - `.docx` job: assert `ocr_pdf_pages` is never called (_Requirements: 1.10_)
    - Guardrails tool raising: assert the job still reaches `completed` (_Requirements: 3.7_)
    - Document with an embedded image: assert `recognize_image_text` still runs and its spans use `image:` refs (_Requirements: 4.5_)
    - Completed `.pdf` job: assert `update_job_status` received a non-null `detection_summary` (_Requirements: 5.1_)
    - _Requirements: 1.10, 3.7, 4.5, 5.1_

- [x] 8. Persist and expose the detection summary
  - [x] 8.1 Accept `detection_summary` in `update_job_status`
    - Edit `backend/agent/tools/job_status_update.py`: add an optional `detection_summary` parameter and set `detectionSummary` in the `completed` `UpdateExpression` when it is provided
    - Keep the expression naming only the changed attributes so unrelated attributes stay intact
    - _Requirements: 5.1, 5.10_

  - [ ]* 8.2 Write property test for identity-attribute preservation
    - **Property 23: Recording the summary leaves identity attributes untouched**
    - **Validates: Requirement 5.10**

  - [x] 8.3 Return `detectionSummary` from `GET /jobs/{jobId}`
    - Edit `backend/lambdas/job_status_handler.py`: add `detectionSummary` to the 200 response body
    - Implement a recursive `_to_jsonable` converting DynamoDB `Decimal` values to `int` (or `float` when not integral), since `json.dumps` raises on `Decimal`
    - Return `null` when the record carries no summary
    - _Requirements: 5.6, 5.7_

  - [ ]* 8.4 Write property test for the summary persistence round trip
    - **Property 22: The detection summary survives the persistence round trip**
    - **Validates: Requirement 5.6**

  - [ ]* 8.5 Write unit test for the absent-summary response
    - Assert a job item without `detectionSummary` yields a body with the key present and `null`
    - _Requirements: 5.7_

- [x] 9. Surface detection counts in the frontend
  - [x] 9.1 Add `DetectionSummary` types to the API client
    - Edit `frontend/src/api/types.ts`: add `PageHealth` and `DetectionSummary` interfaces and add `detectionSummary: DetectionSummary | null` to `JobStatusResponse`
    - _Requirements: 5.6_

  - [x] 9.2 Render detection counts in `JobStatusView`
    - Edit `frontend/src/views/JobStatusView.tsx`: accept an optional `detectionSummary` prop and render one line with the total and per-path counts when `status === 'completed'` and the summary is present
    - Render exactly as before when the summary is absent, keeping the component a pure function of its props
    - _Requirements: 5.8, 5.9_

  - [ ]* 9.3 Write property test for the detection-count rendering
    - **Property 24: The job status view renders detection counts exactly when they exist**
    - **Validates: Requirements 5.8, 5.9**

- [ ] 10. Guard the invariants the new path must not disturb
  - [ ]* 10.1 Write property test for verbatim text-layer extraction
    - **Property 15: The text layer is recorded verbatim**
    - **Validates: Requirement 4.3**

  - [ ]* 10.2 Write property test for structure preservation under dual-path redaction
    - **Property 16: Dual-path redaction preserves document structure**
    - **Validates: Requirement 4.4**

  - [ ]* 10.3 Write CDK template test for the Textract grant
    - Assert the synthesized agent execution role policy contains `textract:DetectDocumentText`
    - _Requirements: 4.6_

- [x] 11. Add the raster resolution configuration to the stack
  - [x] 11.1 Inject `PDF_OCR_DPI` into the AgentCore Runtime environment
    - Edit `backend/infra/redaction_stack.py`: add `PDF_OCR_DPI` alongside the existing `GUARDRAIL_ID` / `GUARDRAIL_VERSION` environment variables
    - Leave the existing `textract:DetectDocumentText` grant as-is; it already covers this path
    - _Requirements: 4.6, 4.7_

- [x] 12. Prove the defect is fixed
  - [x]* 12.1 Write the `Patti.pdf` regression integration test
    - Run the dual-path pipeline against the repository PDF with the broken ToUnicode CMap
    - Assert redaction rectangles cover `Patti`, `Martin`, and `Christina` — the values the single-path pipeline covered at 0%, 0%, and 56.6%
    - Assert the recorded `pageHealth` entry reports the text-layer/OCR character-count asymmetry (19 versus 23)
    - Marked optional because it calls real Textract; it is nevertheless the test that validates the feature's premise
    - _Requirements: 1.1, 1.9, 2.2, 2.4, 5.4_

- [x] 13. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; task 12.1 is the one optional task worth not skipping, since it is the only test that verifies the original defect is actually gone.
- No changes to `merge_spans_within_unit` or `_regroup_and_merge` are required. Tasks 7.3–7.5 verify that the `ocr:` namespace works with the existing logic unchanged.
- No changes to `verify_structure` are required. OCR text units never reach it — it re-extracts both documents itself and compares page count and order.
- Every property test uses Hypothesis (Python) or fast-check (TypeScript), runs at least 100 iterations, and is tagged `Feature: pdf-dual-path-pii-detection, Property {number}: {property title}`.
- Textract, Bedrock Runtime, DynamoDB, and S3 are faked or mocked in all property tests. Real service calls appear only in task 12.1.
- No new third-party dependencies: pdfminer.six, pikepdf, pypdfium2, boto3, Hypothesis, and fast-check are all already in use.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "11.1"] },
    { "id": 1, "tasks": ["2.1", "3.1", "3.4", "5.1", "8.1", "8.3", "9.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6", "3.2", "3.3", "3.5", "5.2", "5.3", "5.4", "8.2", "8.4", "8.5", "9.2", "10.1", "10.3"] },
    { "id": 3, "tasks": ["5.5", "5.6", "5.7", "5.8", "5.9", "7.1", "9.3"] },
    { "id": 4, "tasks": ["7.2", "7.3", "7.4", "7.5", "7.6"] },
    { "id": 5, "tasks": ["7.7", "7.8", "7.9", "10.2"] },
    { "id": 6, "tasks": ["12.1"] }
  ]
}
```
