# Implementation Plan: Detection Review Mode

## Overview

Add an optional two-phase workflow where the Agent first detects PII, presents results for human review via a new `"detected"` status, then redacts only the values the user confirms. The existing auto-mode pipeline remains the default and is unchanged. Implementation uses Python (backend) and TypeScript (frontend).

## Tasks

- [ ] 1. Extend shared models and job status update tool
  - [x] 1.1 Add `JobMode` type, `"detected"` status, and `mode` field to `RedactionJob` in `backend/shared/models.py`
    - Add `JobMode = Literal["auto", "review"]` and `JOB_MODE_DEFAULT`
    - Extend `JobStatus` to include `"detected"`
    - Add `mode: JobMode = JOB_MODE_DEFAULT` field to `RedactionJob` dataclass
    - Mirror changes in `backend/lambdas/shared/models.py`
    - _Requirements: 1.1, 1.2, 1.3, 2.3, 9.2_

  - [x] 1.2 Add `STATUS_DETECTED` support in `backend/agent/tools/job_status_update.py`
    - Add `STATUS_DETECTED = "detected"` constant
    - Handle the `"detected"` transition in `update_job_status()` (no `redactedKey` or `errorMessage`)
    - _Requirements: 2.3, 9.1_

- [x] 2. Implement detection results builder and span filter
  - [x] 2.1 Create `backend/agent/tools/detection_results.py` with `build_detection_results` function
    - Define `DetectionValue` and `DetectionResults` dataclasses
    - Implement deduplication by `(matched_text, ruleName)` key
    - Assign sequential integer IDs starting from 1
    - Compute occurrence counts, page lists (1-based), and context snippets (±15 chars with 「」 brackets)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [ ]* 2.2 Write property test for detection results deduplication (Property 4)
    - **Property 4: Detection results deduplication invariant**
    - **Validates: Requirements 3.2**

  - [ ]* 2.3 Write property test for detection results structural completeness (Property 5)
    - **Property 5: Detection results structural completeness**
    - **Validates: Requirements 3.1, 3.3**

  - [ ]* 2.4 Write property test for context snippet correctness (Property 6)
    - **Property 6: Context snippet correctness**
    - **Validates: Requirements 3.4, 3.5, 3.6**

  - [x] 2.5 Implement `filter_spans_by_selections` in `backend/agent/tools/detection_results.py`
    - Map confirmed Detection_Value IDs back to original spans via `(value, entityType)` keys
    - Return all spans unchanged when `confirmed_ids` is None (timeout case)
    - _Requirements: 4.3, 4.4_

  - [ ]* 2.6 Write property test for selective redaction filtering (Property 7)
    - **Property 7: Selective redaction from confirmed selections**
    - **Validates: Requirements 4.3**

- [x] 3. Implement confirmation poller
  - [x] 3.1 Create `backend/agent/tools/confirmation_poll.py` with `poll_for_confirmation` function
    - Poll DynamoDB every 5 seconds for `confirmedSelections` attribute
    - Return the list of IDs on confirmation, or `None` after 30-minute timeout
    - Use environment variable for table name (consistent with existing tools)
    - _Requirements: 4.1, 4.2, 4.4_

  - [ ]* 3.2 Write unit tests for confirmation poller
    - Test immediate confirmation, delayed confirmation, and timeout fallback with mocked time/DynamoDB
    - _Requirements: 4.1, 4.2, 4.4_

- [x] 4. Integrate review-mode branch into Agent orchestration
  - [x] 4.1 Add review-mode branch in `backend/agent/orchestration.py` after matching step
    - Branch on `job.mode == "review"` after match completes
    - Call `build_detection_results` to transform spans into Detection_Results
    - Persist `detections.json` to S3 at key `{jobId}/detections.json`
    - Set job status to `"detected"` via `update_job_status`
    - Call `poll_for_confirmation` to wait for user selections
    - Call `filter_spans_by_selections` to narrow spans to confirmed subset
    - Continue with existing redact → verify → store → update pipeline
    - Wrap entire review branch in try/except: any failure → job `"failed"`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 4.1, 4.3, 4.4, 4.5, 4.6, 4.7, 10.1_

  - [ ]* 4.2 Write unit tests for orchestration review-mode branch
    - Test detection persisted, status transitions (processing → detected → completed)
    - Test timeout path (redacts all)
    - Test failure during detection phase → job failed
    - Ensure auto-mode path unchanged
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 4.5, 4.6, 4.7, 10.1_

- [x] 5. Checkpoint - Ensure all agent/tool tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Modify UploadHandler to accept `mode` field
  - [x] 6.1 Update `backend/lambdas/upload_handler.py` to parse and persist `mode`
    - Parse optional `mode` text field from multipart/form-data body
    - Normalize unrecognized values to `"auto"` (same lenient approach as detection source fields)
    - Persist `mode` on the DynamoDB record
    - Include `mode` in the 200 success response body
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 10.2, 10.4_

  - [ ]* 6.2 Write unit tests for UploadHandler mode handling
    - Test mode="review" persisted and returned
    - Test mode="auto" persisted and returned
    - Test missing mode defaults to "auto"
    - Test unrecognized mode defaults to "auto"
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 6.3 Write property test for unrecognized mode normalization (Property 1)
    - **Property 1: Unrecognized mode normalizes to auto**
    - **Validates: Requirements 1.4**

- [x] 7. Implement DetectionsHandler Lambda
  - [x] 7.1 Create `backend/lambdas/detections_handler.py`
    - Extract `jobId` from path parameters
    - Read RedactionJobs record; return 404 if not found
    - Return 409 `DETECTIONS_NOT_AVAILABLE` if status ≠ `"detected"`
    - Read `{jobId}/detections.json` from output bucket
    - Return 200 with detection results JSON
    - _Requirements: 5.1, 5.2, 5.3_

  - [ ]* 7.2 Write unit tests for DetectionsHandler
    - Test 200 path (status is "detected", S3 object exists)
    - Test 404 path (job not found)
    - Test 409 path (status is not "detected")
    - _Requirements: 5.1, 5.2, 5.3_

  - [ ]* 7.3 Write property test for DetectionsHandler status guard (Property 8)
    - **Property 8: DetectionsHandler status guard**
    - **Validates: Requirements 5.2**

- [x] 8. Implement RedactHandler Lambda
  - [x] 8.1 Create `backend/lambdas/redact_handler.py`
    - Extract `jobId` from path parameters
    - Parse and validate request body (`selectedIds` array of integers)
    - Return 400 `INVALID_REQUEST_BODY` if body invalid
    - Conditional DynamoDB update: status must be "detected" AND `confirmedSelections` must not exist
    - Handle `ConditionalCheckFailedException` to distinguish 404/409 cases
    - Return 202 on success
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 8.2 Write unit tests for RedactHandler
    - Test 202 success path
    - Test 400 invalid body
    - Test 404 job not found
    - Test 409 wrong status
    - Test 409 already confirmed (race condition)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 8.3 Write property test for RedactHandler status guard (Property 9)
    - **Property 9: RedactHandler status guard**
    - **Validates: Requirements 6.2**

  - [ ]* 8.4 Write property test for RedactHandler body validation (Property 10)
    - **Property 10: RedactHandler body validation**
    - **Validates: Requirements 6.4**

- [x] 9. Update JobStatusHandler to include `mode` in response
  - [x] 9.1 Update `backend/lambdas/job_status_handler.py` to return `mode`
    - Read `mode` from DynamoDB record, default to `"auto"` for pre-feature records
    - Include `mode` in 200 response body
    - _Requirements: 9.1, 9.3, 12_

  - [ ]* 9.2 Write unit tests for JobStatusHandler mode field
    - Test mode included in response for review-mode jobs
    - Test mode defaults to "auto" for legacy records
    - _Requirements: 9.3_

- [x] 10. Add CDK infrastructure for new Lambda handlers and API routes
  - [x] 10.1 Add DetectionsHandler and RedactHandler Lambdas and API routes in `backend/infra/redaction_stack.py`
    - Create DetectionsHandler Lambda (env: `JOBS_TABLE_NAME`, `OUTPUT_BUCKET_NAME`; permissions: read on jobs table, read on output bucket)
    - Create RedactHandler Lambda (env: `JOBS_TABLE_NAME`; permissions: read+write on jobs table)
    - Add `GET /jobs/{jobId}/detections` route → DetectionsHandler integration
    - Add `POST /jobs/{jobId}/redact` route → RedactHandler integration
    - _Requirements: 5.1, 6.1_

  - [ ]* 10.2 Write CDK snapshot/assertion test for new resources
    - Verify new Lambdas, routes, and IAM permissions are synthesized correctly
    - _Requirements: 5.1, 6.1_

- [x] 11. Checkpoint - Ensure all backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Extend frontend types and API client
  - [x] 12.1 Update `frontend/src/api/types.ts` with new types
    - Extend `JobStatus` to include `'detected'`
    - Add `JobMode` type (`'auto' | 'review'`)
    - Add `mode` to `UploadResponse` and `JobStatusResponse`
    - Add `DetectionValue`, `DetectionResults`, `SubmitRedactionRequest` interfaces
    - _Requirements: 9.1, 9.3, 1.5_

  - [x] 12.2 Update `frontend/src/api/client.ts` with new methods
    - Add optional `mode` parameter to `uploadDocument`; append to form data
    - Add `getDetections(jobId)` method → `GET /jobs/{jobId}/detections`
    - Add `submitRedaction(jobId, selectedIds)` method → `POST /jobs/{jobId}/redact`
    - _Requirements: 5.1, 6.1, 1.5_

  - [ ]* 12.3 Write unit tests for new API client methods
    - Test `getDetections` success/error paths
    - Test `submitRedaction` success/error paths
    - Test `uploadDocument` includes mode in form data
    - _Requirements: 5.1, 6.1_

- [ ] 13. Update frontend polling for `"detected"` status
  - [x] 13.1 Update `frontend/src/api/polling.ts` to treat `"detected"` as terminal
    - `"detected"` stops the poll (status !== `'processing'`); the view transitions to review UI
    - After confirmation, the view starts a new poll to await `"completed"` or `"failed"`
    - _Requirements: 9.1, 9.2_

  - [x] 13.2 Update `frontend/src/status/jobStatusDisplay.ts` for `"detected"` status
    - `"detected"` → no download visible, no failure shown
    - _Requirements: 9.1_

  - [ ]* 13.3 Write unit tests for polling and status display with "detected"
    - Test polling stops on "detected"
    - Test jobStatusDisplay for "detected" indicator
    - _Requirements: 9.1, 9.2_

- [x] 14. Implement frontend mode toggle in UploadView
  - [x] 14.1 Add mode selector to `frontend/src/views/UploadView.tsx`
    - Add radio buttons: "全自动" (auto, default) / "先审核再遮盖" (review)
    - Pass selected `mode` to `apiClient.uploadDocument(...)` call
    - When job status reaches `"detected"`, transition to detection review view
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [ ]* 14.2 Write unit tests for UploadView mode toggle
    - Test default selection is "全自动"
    - Test upload includes mode="review" when selected
    - Test transition to review view on "detected" status
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [x] 15. Implement DetectionReviewView component
  - [x] 15.1 Create `frontend/src/views/DetectionReviewView.tsx`
    - Props: `apiClient`, `jobId`, `onConfirmed` callback
    - On mount, fetch detections via `apiClient.getDetections(jobId)`
    - Group `DetectionValues` by `entityType` under labeled headings
    - Render checkbox (default: checked), matched text, context snippet, pages, count per value
    - "确认遮盖" button calls `apiClient.submitRedaction(jobId, checkedIds)`
    - On success, invoke `onConfirmed()` callback so parent resumes polling
    - Handle loading and error states
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [ ]* 15.2 Write unit tests for DetectionReviewView
    - Test grouping by entity type
    - Test all checkboxes default to checked
    - Test unchecking excludes IDs from submission
    - Test confirm button calls submitRedaction with correct IDs
    - Test onConfirmed callback fired after successful submission
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [ ]* 15.3 Write property test for confirmation sends only checked IDs (Property 11)
    - **Property 11: Confirmation sends only checked IDs**
    - **Validates: Requirements 8.6**

- [x] 16. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- Backend tests use pytest (existing `backend/tests/` directory); frontend tests use vitest
- The `shared/models.py` changes must be mirrored in `backend/lambdas/shared/models.py` (existing convention)
- All existing auto-mode tests must continue passing without modification throughout

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "3.1", "6.1", "9.1", "12.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "6.2", "6.3", "7.1", "8.1", "9.2", "12.2"] },
    { "id": 3, "tasks": ["2.6", "3.2", "4.1", "7.2", "7.3", "8.2", "8.3", "8.4", "10.1", "12.3"] },
    { "id": 4, "tasks": ["4.2", "10.2", "13.1", "13.2"] },
    { "id": 5, "tasks": ["13.3", "14.1"] },
    { "id": 6, "tasks": ["14.2", "15.1"] },
    { "id": 7, "tasks": ["15.2", "15.3"] }
  ]
}
```
