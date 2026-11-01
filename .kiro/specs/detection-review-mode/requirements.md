# Requirements Document

## Introduction

The Detection Review Mode feature adds an optional two-phase workflow to the PII Document Redaction POC. In this mode, the system first detects PII values in an uploaded document and presents them for human review before applying redaction. The user selects which detected values to redact, and only those selections are applied to the final document. This complements the existing fully-automatic mode (which remains the default and is unchanged) by giving the Demonstration User explicit control over what is redacted.

## Glossary

- **System**: The PII Document Redaction POC backend (API Gateway, Lambda handlers, AgentCore Runtime, DynamoDB, S3).
- **Frontend**: The React single-page application served by Vite that communicates with the System over HTTPS.
- **Redaction_Job**: A record in the `RedactionJobs` DynamoDB table tracking one document's lifecycle from upload to terminal status.
- **Job_Mode**: A string field on the Redaction_Job indicating the processing workflow: `"auto"` (fully automatic, existing behavior) or `"review"` (detect-then-confirm).
- **Detected_Status**: The Redaction_Job status value `"detected"`, indicating that PII detection has completed and the Detection_Results are available for review. Only reachable by `"review"` mode jobs.
- **Detection_Results**: A JSON object persisted to S3 containing the deduplicated PII values found in the source document, each carrying a stable sequential integer ID, entity type, occurrence count, page references, and a context snippet.
- **Detection_Value**: A single deduplicated PII value entry within the Detection_Results, identified by a unique stable integer ID.
- **Context_Snippet**: A short text excerpt (±15 characters around the first occurrence of a Detection_Value) with the value wrapped in 「」 brackets, giving the reviewer enough context to decide whether to redact.
- **Confirmed_Selections**: A list of Detection_Value integer IDs that the user has chosen to redact, written to the Redaction_Job DynamoDB record by the confirmation endpoint.
- **Auto_Redact_Timeout**: The maximum duration (30 minutes) the Agent waits for Confirmed_Selections before automatically redacting all detected values as a safety fallback.
- **Demonstration_User**: The anonymous user interacting with the Frontend to upload, review, and download documents.
- **UploadHandler**: The Lambda function handling `POST /jobs`.
- **DetectionsHandler**: The Lambda function handling `GET /jobs/{jobId}/detections`.
- **RedactHandler**: The Lambda function handling `POST /jobs/{jobId}/redact`.
- **Agent**: The AgentCore Redaction Agent executing the fixed-order deterministic pipeline.

## Requirements

### Requirement 1: Job Mode Selection

**User Story:** As a Demonstration_User, I want to choose between automatic redaction and review-before-redaction when uploading a document, so that I can control which PII values get redacted.

#### Acceptance Criteria

1. WHEN a document is uploaded with `mode` set to `"review"`, THE UploadHandler SHALL create a Redaction_Job record with `mode` equal to `"review"`.
2. WHEN a document is uploaded with `mode` set to `"auto"`, THE UploadHandler SHALL create a Redaction_Job record with `mode` equal to `"auto"`.
3. WHEN a document is uploaded without a `mode` field, THE UploadHandler SHALL create a Redaction_Job record with `mode` defaulting to `"auto"`.
4. WHEN a document is uploaded with an unrecognized `mode` value, THE UploadHandler SHALL default the `mode` field to `"auto"` and proceed without error.
5. THE UploadHandler SHALL include the `mode` field in the `POST /jobs` success response body alongside `jobId`, `status`, and `fileName`.

### Requirement 2: Review-Mode Detection Phase

**User Story:** As a Demonstration_User, I want the system to detect PII and present the results for my review before applying any redaction, so that I can decide what to keep and what to redact.

#### Acceptance Criteria

1. WHILE the Redaction_Job has `mode` equal to `"review"`, THE Agent SHALL execute the extraction, OCR, and matching steps identically to auto mode.
2. WHEN the Agent completes the detection steps for a review-mode job, THE Agent SHALL persist the Detection_Results as a JSON file at the S3 key `{jobId}/detections.json` in the output documents bucket.
3. WHEN the Agent successfully persists the Detection_Results, THE Agent SHALL update the Redaction_Job status to `"detected"`.
4. WHILE the Redaction_Job has `mode` equal to `"auto"`, THE Agent SHALL execute the existing pipeline unchanged and never set the status to `"detected"`.

### Requirement 3: Detection Results Structure

**User Story:** As a Demonstration_User, I want to see each unique PII value with its type, location, and surrounding context, so that I can make informed redaction decisions.

#### Acceptance Criteria

1. THE Agent SHALL assign each Detection_Value a stable sequential integer ID starting from 1.
2. THE Agent SHALL deduplicate PII values by their exact matched text and entity type, so that repeated occurrences of the same value under the same type produce a single Detection_Value with an incremented occurrence count.
3. THE Agent SHALL include for each Detection_Value: the integer ID, the entity type (rule name), the matched text value, the total occurrence count, a list of page numbers where the value appears, and a Context_Snippet.
4. THE Agent SHALL generate the Context_Snippet by locating the first occurrence of the matched text in the source text, extracting ±15 characters around it, and wrapping the matched text in 「」 brackets.
5. IF a Detection_Value's first occurrence starts fewer than 15 characters from the beginning of its source text unit, THEN THE Agent SHALL use all available preceding characters without padding.
6. IF a Detection_Value's first occurrence ends fewer than 15 characters from the end of its source text unit, THEN THE Agent SHALL use all available following characters without padding.

### Requirement 4: Agent Confirmation Wait

**User Story:** As a system operator, I want the agent to wait for user confirmation before redacting in review mode, while ensuring abandoned jobs still get fully redacted, so that no PII leaks from unattended review jobs.

#### Acceptance Criteria

1. WHEN the Agent sets the Redaction_Job status to `"detected"`, THE Agent SHALL begin polling the Redaction_Job DynamoDB record for the presence of a `confirmedSelections` attribute.
2. WHILE polling for confirmation, THE Agent SHALL check the DynamoDB record at intervals of 5 seconds.
3. WHEN the Agent detects a `confirmedSelections` attribute on the Redaction_Job record, THE Agent SHALL proceed to redact only the spans corresponding to the Detection_Value IDs listed in `confirmedSelections`.
4. IF the Agent has polled for 30 minutes without detecting a `confirmedSelections` attribute, THEN THE Agent SHALL proceed to redact all detected spans as if every Detection_Value had been selected.
5. WHEN the Agent proceeds to redact (whether by user confirmation or timeout), THE Agent SHALL execute the redaction, structure verification, and storage steps identically to auto mode.
6. WHEN the Agent completes redaction for a review-mode job, THE Agent SHALL update the Redaction_Job status to `"completed"` with the same Detection_Summary structure used in auto mode.
7. IF an error occurs during the redaction phase of a review-mode job, THEN THE Agent SHALL update the Redaction_Job status to `"failed"` with an error message.

### Requirement 5: Detections Retrieval API

**User Story:** As a Demonstration_User, I want to retrieve the list of detected PII values for my uploaded document, so that I can review them before confirming which to redact.

#### Acceptance Criteria

1. WHEN a `GET /jobs/{jobId}/detections` request is received for a Redaction_Job with status `"detected"`, THE DetectionsHandler SHALL return a 200 response containing the Detection_Results JSON.
2. WHEN a `GET /jobs/{jobId}/detections` request is received for a Redaction_Job with status other than `"detected"`, THE DetectionsHandler SHALL return a 409 response with error code `"DETECTIONS_NOT_AVAILABLE"`.
3. WHEN a `GET /jobs/{jobId}/detections` request is received for a non-existent `jobId`, THE DetectionsHandler SHALL return a 404 response with error code `"JOB_NOT_FOUND"`.

### Requirement 6: Redaction Confirmation API

**User Story:** As a Demonstration_User, I want to submit my redaction selections so that only the PII values I choose get redacted in the final document.

#### Acceptance Criteria

1. WHEN a `POST /jobs/{jobId}/redact` request is received with a valid list of Detection_Value integer IDs, THE RedactHandler SHALL write the `confirmedSelections` attribute (containing the submitted IDs) to the Redaction_Job DynamoDB record and return a 202 response.
2. WHEN a `POST /jobs/{jobId}/redact` request is received for a Redaction_Job with status other than `"detected"`, THE RedactHandler SHALL return a 409 response with error code `"CONFIRMATION_NOT_ALLOWED"`.
3. WHEN a `POST /jobs/{jobId}/redact` request is received for a non-existent `jobId`, THE RedactHandler SHALL return a 404 response with error code `"JOB_NOT_FOUND"`.
4. WHEN a `POST /jobs/{jobId}/redact` request is received with a body that is not a JSON object containing a `selectedIds` array of integers, THE RedactHandler SHALL return a 400 response with error code `"INVALID_REQUEST_BODY"`.
5. WHEN a `POST /jobs/{jobId}/redact` request is received for a Redaction_Job that already has a `confirmedSelections` attribute, THE RedactHandler SHALL return a 409 response with error code `"ALREADY_CONFIRMED"`.

### Requirement 7: Frontend Mode Toggle

**User Story:** As a Demonstration_User, I want a simple toggle in the upload view to choose between automatic and review mode, so that I can decide the workflow before uploading.

#### Acceptance Criteria

1. THE Frontend SHALL display a mode selector in the upload view with two options: "全自动" (auto) and "先审核再遮盖" (review).
2. THE Frontend SHALL default the mode selector to "全自动" (auto).
3. WHEN the Demonstration_User selects "先审核再遮盖" and uploads a document, THE Frontend SHALL include `mode: "review"` in the upload request.
4. WHEN the Demonstration_User selects "全自动" and uploads a document, THE Frontend SHALL include `mode: "auto"` in the upload request.

### Requirement 8: Frontend Detection Review View

**User Story:** As a Demonstration_User, I want to see all detected PII values grouped by type with context, so that I can efficiently select which values to redact.

#### Acceptance Criteria

1. WHEN the Redaction_Job reaches `"detected"` status, THE Frontend SHALL fetch the Detection_Results from `GET /jobs/{jobId}/detections` and display the detection review view.
2. THE Frontend SHALL group Detection_Values by entity type and display each group under a labeled heading.
3. THE Frontend SHALL display for each Detection_Value: a checkbox, the matched text, the Context_Snippet, the page number(s), and the occurrence count.
4. THE Frontend SHALL select all Detection_Values by default (all checkboxes checked).
5. THE Frontend SHALL provide a "确认遮盖" (Confirm Redaction) button that submits the selected Detection_Value IDs via `POST /jobs/{jobId}/redact`.
6. WHEN the Demonstration_User clicks "确认遮盖", THE Frontend SHALL send only the IDs of checked Detection_Values in the request body.
7. WHEN the confirmation request succeeds, THE Frontend SHALL resume polling the Redaction_Job status until it reaches `"completed"` or `"failed"`.

### Requirement 9: Job Status Extensions

**User Story:** As a Demonstration_User, I want the job status API to reflect the new `detected` state, so that the frontend can transition to the review view at the right time.

#### Acceptance Criteria

1. WHEN a `GET /jobs/{jobId}` request is received for a Redaction_Job with status `"detected"`, THE System SHALL return a 200 response with `status` equal to `"detected"`.
2. THE System SHALL treat `"detected"` as a non-terminal status for review-mode jobs (the job is still in progress, awaiting user confirmation).
3. THE System SHALL include the `mode` field in the `GET /jobs/{jobId}` response body.

### Requirement 10: Backward Compatibility

**User Story:** As an existing client of the System, I want my current integration to work without changes, so that the new feature does not break existing behavior.

#### Acceptance Criteria

1. WHILE a Redaction_Job has `mode` equal to `"auto"`, THE System SHALL never set its status to `"detected"`.
2. THE System SHALL accept `POST /jobs` requests without a `mode` field and process them identically to the pre-feature behavior.
3. THE System SHALL not modify the response shape of any existing API endpoint for auto-mode jobs.
4. THE UploadHandler SHALL continue to accept `POST /jobs` requests with only the `file` field (no `mode`) and produce a `"processing"` job as before.
