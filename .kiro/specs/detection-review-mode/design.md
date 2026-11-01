# Design Document: Detection Review Mode

## Overview

Detection Review Mode adds an optional two-phase workflow where the Agent first detects PII, presents the results for human review, then redacts only the values the user confirms. This complements the existing fully-automatic mode (unchanged, remains the default) by giving the Demonstration User explicit control over what gets redacted.

The design preserves the existing single-AgentCore-invocation architecture: the Agent polls DynamoDB for user confirmation rather than requiring a second invocation or a callback mechanism.

## Architecture

### High-Level Flow

```
┌──────────┐   POST /jobs     ┌──────────────┐   DynamoDB Stream   ┌───────────────────┐
│ Frontend │ ───(mode=review)──▶│ UploadHandler │ ─────────────────▶  │ JobDispatcher      │
└──────────┘                   └──────────────┘                     └─────────┬─────────┘
                                                                              │
                                                                    InvokeAgentRuntime
                                                                              │
     ┌────────────────────────────────────────────────────────────────────────▼─────────┐
     │  AgentCore Redaction Agent (single invocation)                                    │
     │                                                                                   │
     │  extract → OCR → match ──┬── [auto] → redact → verify → store → completed        │
     │                          │                                                        │
     │                          └── [review] → build detections → persist S3 →           │
     │                                         set status="detected" →                   │
     │                                         poll DynamoDB (5s interval, 30min max) →  │
     │                                         filter spans by confirmedSelections →     │
     │                                         redact → verify → store → completed       │
     └───────────────────────────────────────────────────────────────────────────────────┘

     ┌──────────┐   GET /detections    ┌────────────────────┐   S3    ┌──────────┐
     │ Frontend │ ────────────────────▶ │ DetectionsHandler  │ ──────▶ │detections│
     └──────────┘                       └────────────────────┘         │  .json   │
          │                                                            └──────────┘
          │        POST /redact         ┌────────────────────┐
          └────────────────────────────▶│  RedactHandler     │──▶ DynamoDB (confirmedSelections)
                                        └────────────────────┘
```

### Design Decisions

1. **Single AgentCore invocation** — The Agent stays alive and polls DynamoDB for `confirmedSelections` rather than exiting after detection and requiring a re-invocation. This avoids cold-start latency on the second phase and keeps the detection context (text units, match spans, image regions) in memory.

2. **Detection results in S3** — `{jobId}/detections.json` is persisted to the output documents bucket (the same bucket used for redacted documents). This avoids bloating the DynamoDB record with potentially large detection payloads, keeps the retrieval path a simple S3 GetObject, and reuses the existing bucket/permissions.

3. **DynamoDB polling for confirmation** — The Agent reads the `RedactionJobs` record every 5 seconds looking for a `confirmedSelections` attribute. This is a simple, stateless mechanism that requires no additional infrastructure (no SQS queue, no Step Functions wait state, no callback URL). The 30-minute timeout ensures abandoned review jobs still get fully redacted.

4. **New "detected" status** — A new non-terminal status value inserted into the state machine between `processing` and `completed`/`failed`. Only reachable by `mode="review"` jobs. The existing `JobStatusHandler` returns whatever status the record carries, so it naturally serves `"detected"` without code changes beyond including `mode` in the response.

5. **Mode defaults to "auto"** — Absent, blank, or unrecognized mode values default to `"auto"`, preserving full backward compatibility. No existing client or API shape is modified for auto-mode jobs.

## Components

### Modified Components

#### UploadHandler (`backend/lambdas/upload_handler.py`)

**Changes:**
- Parse optional `mode` text field from `multipart/form-data` body (same pattern as `addressDetectionSource`).
- Normalize unrecognized values to `"auto"` (lenient, same approach as detection source fields).
- Persist `mode` on the `RedactionJobs` DynamoDB record.
- Include `mode` in the 200 success response body.

```python
JOB_MODE_VALUES = ("auto", "review")
JOB_MODE_DEFAULT = "auto"
JOB_MODE_FIELD_NAME = "mode"

def _normalize_job_mode(raw_value: Optional[str]) -> str:
    value = (raw_value or "").strip()
    if value in JOB_MODE_VALUES:
        return value
    return JOB_MODE_DEFAULT
```

#### JobStatusHandler (`backend/lambdas/job_status_handler.py`)

**Changes:**
- Include `mode` in the 200 response body (read from the DynamoDB record, default `"auto"` for pre-feature records).
- No new status validation needed — the handler already returns whatever `status` the record carries.

```python
return _json_response(200, {
    "jobId": job_item["jobId"],
    "status": job_item["status"],
    "mode": job_item.get("mode", "auto"),
    "errorMessage": job_item.get("errorMessage"),
    "detectionSummary": job_item.get("detectionSummary"),
})
```

#### Agent Orchestration (`backend/agent/orchestration.py`)

**Changes:**
- After matching, branch on `job.mode`:
  - `"auto"` → existing pipeline (unchanged).
  - `"review"` → build detection results, persist to S3, set status to `"detected"`, poll for confirmation, filter spans, then continue with redaction.

```python
# After match step completes:
if job.mode == "review":
    detection_results = _build_detection_results(matches, content.textUnits, ocr_text_units, content.imageRegions)
    _persist_detection_results(job_id, detection_results)
    _update_job_status_detected(job_id)
    confirmed_ids = _poll_for_confirmation(job_id)
    matches = _filter_spans_by_selections(matches, detection_results, confirmed_ids)

# Continue with: redact → verify → store → update (same for both modes)
```

#### Agent `update_job_status` tool (`backend/agent/tools/job_status_update.py`)

**Changes:**
- Add support for `status="detected"` transitions (new non-terminal status, no `redactedKey` or `errorMessage` needed).

```python
STATUS_DETECTED = "detected"

# In update_job_status():
elif status == STATUS_DETECTED:
    update_expression = "SET #status = :status, updatedAt = :updatedAt"
    expression_attribute_values = {
        ":status": STATUS_DETECTED,
        ":updatedAt": _now_iso(),
    }
```

#### Shared Models (`backend/shared/models.py`)

**Changes:**
- Extend `JobStatus` literal to include `"detected"`.
- Add `JobMode` type.
- Add `mode` field to `RedactionJob` dataclass.

```python
JobStatus = Literal["processing", "detected", "completed", "failed"]
JobMode = Literal["auto", "review"]
JOB_MODE_DEFAULT: JobMode = "auto"

@dataclass
class RedactionJob:
    # ... existing fields ...
    mode: JobMode = JOB_MODE_DEFAULT
```

#### Frontend Types (`frontend/src/api/types.ts`)

**Changes:**
- Extend `JobStatus` to include `'detected'`.
- Add `JobMode` type.
- Add `mode` to `UploadResponse` and `JobStatusResponse`.
- Add new request/response types for detections and redact endpoints.

```typescript
export type JobStatus = 'processing' | 'detected' | 'completed' | 'failed'
export type JobMode = 'auto' | 'review'

export interface UploadResponse {
  jobId: string
  status: JobStatus
  mode: JobMode
  fileName: string
}

export interface JobStatusResponse {
  jobId: string
  status: JobStatus
  mode: JobMode
  errorMessage: string | null
  detectionSummary: DetectionSummary | null
}
```

#### Frontend API Client (`frontend/src/api/client.ts`)

**Changes:**
- `uploadDocument` accepts optional `mode` parameter, appends it to the form data.
- Add `getDetections(jobId)` method → `GET /jobs/{jobId}/detections`.
- Add `submitRedaction(jobId, selectedIds)` method → `POST /jobs/{jobId}/redact`.

#### Frontend Polling (`frontend/src/api/polling.ts`)

**Changes:**
- Update `pollJobStatus`'s terminal predicate: for review-mode jobs, `"detected"` is NOT terminal (the frontend should transition to the review view, then resume polling after confirmation). The predicate becomes:
  - Terminal when `status === 'completed' || status === 'failed'`
  - `'detected'` triggers the `onUpdate` callback but does NOT stop polling — the view layer decides what to do.

Actually, re-examining: `"detected"` SHOULD stop polling temporarily (so the frontend can display the review UI), then polling resumes after the user confirms. The simplest approach: make `"detected"` trigger `onTerminal` (stopping the poll), let the view transition to review mode, and start a NEW poll after confirmation succeeds.

```typescript
export function pollJobStatus<T extends JobLikeStatus>(
  fetchFn: () => Promise<T>,
  callbacks: PollCallbacks<T>,
  intervalMs: number = DEFAULT_POLL_INTERVAL_MS,
): PollHandle {
  return pollUntilTerminal(
    fetchFn,
    (result) => result.status !== 'processing',
    callbacks,
    intervalMs,
  )
}
```

The existing predicate already works: `'detected' !== 'processing'` evaluates true, so polling stops when the job reaches `"detected"`. The view receives the `"detected"` status via `onUpdate`/`onTerminal` and transitions to the review UI. After confirmation, the view starts a new poll.

#### Frontend UploadView (`frontend/src/views/UploadView.tsx`)

**Changes:**
- Add mode toggle (radio buttons or select): "全自动" / "先审核再遮盖".
- Pass `mode` to `apiClient.uploadDocument(...)`.
- When status reaches `"detected"`, transition to the detection review view.

#### Frontend Job Status Display (`frontend/src/status/jobStatusDisplay.ts`)

**Changes:**
- Handle `'detected'` status: indicator shows "detected", download not visible, no failure.

```typescript
export function getJobStatusDisplay(status: JobStatus): JobStatusDisplay {
  return {
    indicator: status,
    downloadVisible: status === 'completed',
    showFailure: status === 'failed',
  }
}
```

The existing pure function already handles this correctly — `'detected'` is not `'completed'` (no download) and not `'failed'` (no failure indication). No change needed to the function body, only the type definition.

### New Components

#### DetectionsHandler (`backend/lambdas/detections_handler.py`)

New Lambda: `GET /jobs/{jobId}/detections`.

**Responsibilities:**
1. Extract `jobId` from path parameters.
2. Read the `RedactionJobs` record.
3. If not found → 404 `JOB_NOT_FOUND`.
4. If status ≠ `"detected"` → 409 `DETECTIONS_NOT_AVAILABLE`.
5. Read `{jobId}/detections.json` from the output documents bucket.
6. Return 200 with the JSON content.

```python
def handler(event, context):
    job_id = _extract_job_id(event)
    if not job_id:
        return _job_not_found_response()

    job_item = _get_jobs_table().get_item(Key={"jobId": job_id}).get("Item")
    if job_item is None:
        return _job_not_found_response()

    if job_item["status"] != "detected":
        return _error_response(409, "DETECTIONS_NOT_AVAILABLE",
            "Detection results are only available when job status is 'detected'.")

    detections_key = f"{job_id}/detections.json"
    obj = _get_s3_client().get_object(
        Bucket=os.environ["OUTPUT_BUCKET_NAME"], Key=detections_key
    )
    detections_body = obj["Body"].read().decode("utf-8")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": detections_body,
    }
```

**Environment variables:** `JOBS_TABLE_NAME`, `OUTPUT_BUCKET_NAME`.

**IAM permissions:** Read on `RedactionJobs` table, Read on `OutputDocumentsBucket`.

#### RedactHandler (`backend/lambdas/redact_handler.py`)

New Lambda: `POST /jobs/{jobId}/redact`.

**Responsibilities:**
1. Extract `jobId` from path parameters.
2. Parse request body as JSON; validate it is `{ "selectedIds": [int, ...] }`.
3. If body invalid → 400 `INVALID_REQUEST_BODY`.
4. Read the `RedactionJobs` record.
5. If not found → 404 `JOB_NOT_FOUND`.
6. If status ≠ `"detected"` → 409 `CONFIRMATION_NOT_ALLOWED`.
7. If `confirmedSelections` already present → 409 `ALREADY_CONFIRMED`.
8. Write `confirmedSelections` attribute (the list of integer IDs) to the DynamoDB record via conditional `update_item`.
9. Return 202 `{ "status": "accepted" }`.

```python
def handler(event, context):
    job_id = _extract_job_id(event)
    if not job_id:
        return _job_not_found_response()

    selected_ids = _parse_selected_ids(event)
    if selected_ids is None:
        return _error_response(400, "INVALID_REQUEST_BODY",
            "Body must be a JSON object with a 'selectedIds' array of integers.")

    # Conditional update: status must be "detected" AND confirmedSelections must not exist.
    try:
        _get_jobs_table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET confirmedSelections = :ids, updatedAt = :ts",
            ConditionExpression="#s = :detected AND attribute_not_exists(confirmedSelections)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ids": selected_ids,
                ":detected": "detected",
                ":ts": _now_iso(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Distinguish: job not found vs wrong status vs already confirmed
            return _resolve_conditional_failure(job_id)
        raise

    return _json_response(202, {"status": "accepted"})
```

**Environment variables:** `JOBS_TABLE_NAME`.

**IAM permissions:** Read+Write on `RedactionJobs` table.

#### Detection Results Builder (`backend/agent/tools/detection_results.py`)

New module: pure function that transforms the Agent's match spans into the deduplicated, ID'd Detection_Results structure.

```python
@dataclass
class DetectionValue:
    id: int
    entityType: str
    value: str
    count: int
    pages: List[int]
    contextSnippet: str

@dataclass
class DetectionResults:
    values: List[DetectionValue]

def build_detection_results(
    spans: List[MatchSpan],
    text_units: List[ExtractedTextUnit],
    ocr_text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
) -> DetectionResults:
    """Deduplicate spans by (text, ruleName), assign sequential IDs,
    compute occurrence counts, page lists, and context snippets."""
    ...
```

**Deduplication key:** `(matched_text, ruleName)` — two spans are the "same value" when their extracted text AND entity type match exactly.

**Context snippet algorithm:**
```python
CONTEXT_RADIUS = 15

def _build_context_snippet(source_text: str, start: int, end: int) -> str:
    matched = source_text[start:end]
    ctx_start = max(0, start - CONTEXT_RADIUS)
    ctx_end = min(len(source_text), end + CONTEXT_RADIUS)
    before = source_text[ctx_start:start]
    after = source_text[end:ctx_end]
    return f"{before}「{matched}」{after}"
```

**Page resolution:** For a `text:{i}` or `ocr:{i}` span, the page is `text_units[i].position.pageIndex` (or the OCR unit's implicit page index). For `image:{i}`, use the image region's source location to infer page. Page numbers in the output are 1-based (user-facing).

#### Confirmation Poller (`backend/agent/tools/confirmation_poll.py`)

New module: encapsulates the DynamoDB polling loop the Agent runs while waiting for user confirmation.

```python
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 30 * 60  # 30 minutes

def poll_for_confirmation(job_id: str) -> Optional[List[int]]:
    """Poll the RedactionJobs record for `confirmedSelections`.

    Returns:
        The list of selected Detection_Value IDs if confirmation arrives
        within the timeout window, or None if the timeout expires (meaning
        the caller should redact ALL values).
    """
    import time
    elapsed = 0
    while elapsed < POLL_TIMEOUT_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS
        item = _get_jobs_table().get_item(Key={"jobId": job_id}).get("Item")
        if item and "confirmedSelections" in item:
            return item["confirmedSelections"]
    return None  # timeout → redact all
```

#### Span Filter (`backend/agent/tools/detection_results.py`)

Function that maps confirmed Detection_Value IDs back to the original spans:

```python
def filter_spans_by_selections(
    all_spans: List[MatchSpan],
    detection_results: DetectionResults,
    confirmed_ids: Optional[List[int]],
) -> List[MatchSpan]:
    """Return only the spans belonging to confirmed Detection_Value IDs.

    If confirmed_ids is None (timeout), returns all_spans unchanged.
    """
    if confirmed_ids is None:
        return all_spans

    # Build a set of (value, entityType) pairs for confirmed IDs
    confirmed_keys = set()
    for dv in detection_results.values:
        if dv.id in confirmed_ids:
            confirmed_keys.add((dv.value, dv.entityType))

    return [
        span for span in all_spans
        if _span_key(span, ...) in confirmed_keys
    ]
```

#### Frontend Detection Review View (`frontend/src/views/DetectionReviewView.tsx`)

New component displayed when the job reaches `"detected"` status.

**Props:** `apiClient`, `jobId`, `onConfirmed` callback.

**Behavior:**
1. On mount, call `apiClient.getDetections(jobId)`.
2. Group `Detection_Values` by `entityType`.
3. Render each group under a heading with checkboxes (all checked by default).
4. "确认遮盖" button calls `apiClient.submitRedaction(jobId, checkedIds)`.
5. On success, invokes `onConfirmed()` so the parent view resumes polling.

## Data Models

### RedactionJobs Table (Extended)

| Attribute | Type | New? | Description |
|-----------|------|------|-------------|
| `jobId` | String (PK) | No | UUID |
| `status` | String | Extended | Now includes `"detected"` |
| `mode` | String | **Yes** | `"auto"` or `"review"`, defaults to `"auto"` |
| `confirmedSelections` | List<Number> | **Yes** | Detection_Value IDs confirmed by user (written by RedactHandler, read by Agent) |
| `sourceKey` | String | No | S3 key of uploaded document |
| `sourceExtension` | String | No | `.docx` or `.pdf` |
| `fileName` | String | No | Original file name |
| `createdAt` | String | No | ISO 8601 timestamp |
| `updatedAt` | String | No | ISO 8601 timestamp |
| `redactedKey` | String | No | S3 key of redacted document (set on `completed`) |
| `errorMessage` | String | No | Error description (set on `failed`) |
| `detectionSummary` | Map | No | Detection counts (set on `completed`) |
| `addressDetectionSource` | String | No | Detection source policy for ADDRESS |
| `nameDetectionSource` | String | No | Detection source policy for NAME |

### Detection_Results (S3 JSON: `{jobId}/detections.json`)

```json
{
  "values": [
    {
      "id": 1,
      "entityType": "guardrails:NAME",
      "value": "张三",
      "count": 3,
      "pages": [1, 2],
      "contextSnippet": "申请人姓名：「张三」，性别：男"
    },
    {
      "id": 2,
      "entityType": "guardrails:ADDRESS",
      "value": "北京市朝阳区建国路88号",
      "count": 1,
      "pages": [1],
      "contextSnippet": "户籍地址：「北京市朝阳区建国路88号」，邮编"
    }
  ]
}
```

### State Machine (Extended)

```
                    ┌─────────────────────────────────────────────────┐
                    │                                                 │
  [upload]          ▼                                                 │
    ─────▶  processing  ──[auto, success]──▶  completed              │
                │                                                     │
                │──[auto, failure]──────────▶  failed                 │
                │                                                     │
                │──[review, detection done]──▶  detected              │
                                                   │                  │
                                                   │──[confirm/timeout, success]──▶ completed
                                                   │
                                                   └──[confirm/timeout, failure]──▶ failed
```

Valid transitions:
- `processing → completed` (auto mode, success)
- `processing → failed` (auto mode, failure OR review mode detection failure)
- `processing → detected` (review mode, detection success)
- `detected → completed` (review mode, redaction success)
- `detected → failed` (review mode, redaction failure)

Terminal states: `completed`, `failed`.
Non-terminal states: `processing`, `detected`.

## API Design

### Modified Endpoints

#### `POST /jobs`

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | Binary | Yes | The document to redact |
| `mode` | String | No | `"auto"` (default) or `"review"` |
| `addressDetectionSource` | String | No | `"both"` (default), `"llm"`, or `"guardrails"` |
| `nameDetectionSource` | String | No | `"both"` (default), `"llm"`, or `"guardrails"` |

**Response (200):**
```json
{
  "jobId": "uuid",
  "status": "processing",
  "mode": "review",
  "fileName": "document.pdf"
}
```

#### `GET /jobs/{jobId}`

**Response (200):**
```json
{
  "jobId": "uuid",
  "status": "detected",
  "mode": "review",
  "errorMessage": null,
  "detectionSummary": null
}
```

### New Endpoints

#### `GET /jobs/{jobId}/detections`

**Success (200):** Returns the Detection_Results JSON (same shape as the S3 object).

**Error responses:**
- 404: `{ "error": { "code": "JOB_NOT_FOUND", "message": "..." } }`
- 409: `{ "error": { "code": "DETECTIONS_NOT_AVAILABLE", "message": "..." } }`

#### `POST /jobs/{jobId}/redact`

**Request body:**
```json
{
  "selectedIds": [1, 2, 5, 7]
}
```

**Success (202):**
```json
{
  "status": "accepted"
}
```

**Error responses:**
- 400: `{ "error": { "code": "INVALID_REQUEST_BODY", "message": "..." } }`
- 404: `{ "error": { "code": "JOB_NOT_FOUND", "message": "..." } }`
- 409: `{ "error": { "code": "CONFIRMATION_NOT_ALLOWED", "message": "..." } }` (status ≠ "detected")
- 409: `{ "error": { "code": "ALREADY_CONFIRMED", "message": "..." } }` (already has confirmedSelections)

## Infrastructure (CDK)

### New Resources in `redaction_stack.py`

1. **DetectionsHandler Lambda** — `GET /jobs/{jobId}/detections` route.
   - Environment: `JOBS_TABLE_NAME`, `OUTPUT_BUCKET_NAME`.
   - Permissions: Read on `RedactionJobs` table, Read on `OutputDocumentsBucket`.

2. **RedactHandler Lambda** — `POST /jobs/{jobId}/redact` route.
   - Environment: `JOBS_TABLE_NAME`.
   - Permissions: Read+Write on `RedactionJobs` table.

3. **API Gateway routes:**
   - `GET /jobs/{jobId}/detections` → DetectionsHandler integration.
   - `POST /jobs/{jobId}/redact` → RedactHandler integration.

4. **AgentCore Runtime environment variable update** — no new variables needed; the Agent already has `OUTPUT_BUCKET_NAME` and `JOBS_TABLE_NAME`.

### No Changes Required

- S3 buckets (reuse existing `OutputDocumentsBucket`).
- DynamoDB tables (DynamoDB is schemaless; new attributes `mode` and `confirmedSelections` are added without table changes).
- Agent execution role (already has S3 Put/Get on output bucket and DynamoDB Get/Update on jobs table).
- Existing Lambda handler permissions (unchanged).

## Error Handling

| Failure Point | Behavior |
|---------------|----------|
| Detection phase failure (review mode) | Job → `failed`, no `detections.json` persisted, no `"detected"` status set |
| S3 write of `detections.json` fails | Job → `failed` |
| DynamoDB update to `"detected"` fails | Job → `failed` |
| Polling interrupted (Agent crash during wait) | Job stays at `"detected"` permanently (orphaned); future enhancement could add a cleanup sweep |
| Timeout (30 min, no confirmation) | Agent proceeds to redact ALL detected values |
| Redaction phase failure (after confirmation/timeout) | Job → `failed` |
| RedactHandler concurrent confirmation race | Conditional `update_item` ensures exactly one writer wins; loser gets 409 `ALREADY_CONFIRMED` |
| `GET /detections` on non-existent S3 object | 500 (indicates bug — status shouldn't be `"detected"` without the file); logged |

## Frontend Component Structure

```
UploadView
├── Mode selector (radio: 全自动 / 先审核再遮盖)
├── Detection source selectors (existing)
├── File input (existing)
├── Upload status display (existing)
└── [when status === "detected"]
    └── DetectionReviewView
        ├── Detection_Value groups (by entityType)
        │   ├── Group heading (entity type label)
        │   └── Detection_Value rows
        │       ├── Checkbox (default: checked)
        │       ├── Matched text
        │       ├── Context snippet
        │       ├── Page numbers
        │       └── Occurrence count
        └── "确认遮盖" button
```

## Testing Strategy

### Backend Unit Tests (pytest, `tests/unit/`)

- `test_detection_results.py` — `build_detection_results` pure function: deduplication, sequential IDs, context snippets, page resolution.
- `test_confirmation_poll.py` — Mocked DynamoDB + time: tests polling interval, timeout fallback, immediate confirmation.
- `test_span_filter.py` — `filter_spans_by_selections`: subset filtering, full-set (timeout), empty set.
- `test_detections_handler.py` — Lambda handler: 200/404/409 paths.
- `test_redact_handler.py` — Lambda handler: 202/400/404/409 paths, conditional update.
- `test_upload_handler_mode.py` — Mode field parsing, normalization, response inclusion.

### Backend Integration Tests (pytest, `tests/integration/`)

- `test_review_mode_pipeline.py` — Full orchestration with mocked AWS: review-mode job goes processing → detected → completed.

### Frontend Unit Tests (vitest)

- `DetectionReviewView.test.tsx` — Grouping, checkbox state, confirm submission.
- `UploadView.test.tsx` — Mode toggle, upload includes mode, transition to review view on "detected".
- `client.test.ts` — New API methods: `getDetections`, `submitRedaction`.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Unrecognized mode normalizes to auto

*For any* string value of `mode` that is not exactly `"auto"` or `"review"`, the UploadHandler SHALL store `"auto"` as the mode on the created Redaction_Job record and return `"auto"` in the response body.

**Validates: Requirements 1.4**

### Property 2: Upload response always includes mode

*For any* successful `POST /jobs` request (regardless of whether a `mode` field was supplied, and regardless of its value), the 200 response body SHALL contain a `mode` field with value `"auto"` or `"review"`.

**Validates: Requirements 1.5, 10.2**

### Property 3: Auto-mode jobs never reach detected status

*For any* Redaction_Job with `mode` equal to `"auto"`, the system SHALL never set its `status` to `"detected"` at any point during processing.

**Validates: Requirements 2.4, 10.1**

### Property 4: Detection results deduplication invariant

*For any* set of match spans produced by the detection pipeline, the resulting Detection_Results SHALL contain no two Detection_Values with the same `(value, entityType)` pair, and each Detection_Value's `count` SHALL equal the number of input spans that share that `(value, entityType)` combination.

**Validates: Requirements 3.2**

### Property 5: Detection results structural completeness

*For any* non-empty Detection_Results, the Detection_Values SHALL have IDs forming a contiguous sequence `[1, 2, ..., N]`, and each Detection_Value SHALL contain all required fields: `id`, `entityType`, `value`, `count`, `pages`, `contextSnippet`.

**Validates: Requirements 3.1, 3.3**

### Property 6: Context snippet correctness

*For any* matched text occurring at offset `[start, end)` within a source text unit, the generated Context_Snippet SHALL equal `before + "「" + matched + "」" + after`, where `before` is `source[max(0, start-15):start]` and `after` is `source[end:min(len(source), end+15)]`.

**Validates: Requirements 3.4, 3.5, 3.6**

### Property 7: Selective redaction from confirmed selections

*For any* Detection_Results and any subset of Detection_Value IDs in `confirmedSelections`, the spans passed to the redactor SHALL be exactly those spans whose `(matched_text, ruleName)` pair matches a confirmed Detection_Value — no unconfirmed spans are included, and no confirmed spans are omitted.

**Validates: Requirements 4.3**

### Property 8: DetectionsHandler status guard

*For any* `GET /jobs/{jobId}/detections` request where the Redaction_Job exists but has status other than `"detected"` (i.e. `"processing"`, `"completed"`, or `"failed"`), the handler SHALL return a 409 response with error code `"DETECTIONS_NOT_AVAILABLE"`.

**Validates: Requirements 5.2**

### Property 9: RedactHandler status guard

*For any* `POST /jobs/{jobId}/redact` request where the Redaction_Job exists but has status other than `"detected"`, the handler SHALL return a 409 response with error code `"CONFIRMATION_NOT_ALLOWED"`.

**Validates: Requirements 6.2**

### Property 10: RedactHandler body validation

*For any* `POST /jobs/{jobId}/redact` request whose body is not a JSON object containing a `selectedIds` field that is an array of integers, the handler SHALL return a 400 response with error code `"INVALID_REQUEST_BODY"` regardless of the job's status.

**Validates: Requirements 6.4**

### Property 11: Confirmation sends only checked IDs

*For any* subset of Detection_Values that are checked (selected) in the frontend review view when the user clicks "确认遮盖", the `POST /jobs/{jobId}/redact` request body SHALL contain exactly the IDs of those checked values and no others.

**Validates: Requirements 8.6**

### Property 12: Job status response includes mode

*For any* `GET /jobs/{jobId}` request for an existing Redaction_Job (regardless of its status or mode), the 200 response body SHALL include a `mode` field with value `"auto"` or `"review"`.

**Validates: Requirements 9.3**

### Property 13: Detection pipeline mode equivalence

*For any* valid document processed through the extraction, OCR, and matching steps, the resulting match spans SHALL be identical regardless of whether the job's mode is `"auto"` or `"review"` — the mode affects only what happens AFTER matching, never the detection itself.

**Validates: Requirements 2.1**
