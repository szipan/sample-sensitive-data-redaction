/**
 * TypeScript types for the PII Document Redaction POC backend HTTPS API.
 *
 * Field names and shapes mirror design.md's "API Design" section exactly.
 */

/** Redaction Job status values (see design.md Data Models > RedactionJob). */
export type JobStatus = 'processing' | 'detected' | 'completed' | 'failed'

/** Job mode: auto (default) or review (detection review before redaction). */
export type JobMode = 'auto' | 'review'

/**
 * Which detection source(s) may contribute ADDRESS spans for an uploaded
 * document (detection-source-switch feature). Sent as the optional
 * `addressDetectionSource` form field of `POST /jobs`.
 *
 * - `'llm'` (default): keep only the LLM foundation model's ADDRESS spans (it does not
 *   over-flag geopolitical boilerplate like a passport's issuing country).
 * - `'both'`: keep every source's ADDRESS detections (more recall, many more
 *   false positives on Chinese business documents).
 * - `'guardrails'`: keep only Amazon Bedrock Guardrails' ADDRESS spans.
 *
 * Scoped to the ADDRESS entity only; every other PII type is unaffected.
 */
export type AddressDetectionSource = 'both' | 'llm' | 'guardrails'

/** Rule Operation status values (see design.md Data Models > RuleOperation). */
export type RuleOperationStatus = 'queued' | 'processing' | 'applied' | 'rejected'

/** Rule Operation rejection reasons (see design.md Data Models > RuleOperation). */
export type RuleOperationErrorReason =
  | 'DUPLICATE_NAME'
  | 'INVALID_PATTERN'
  | 'UNKNOWN_RULE_NAME'
  | 'BUILT_IN_NOT_DELETABLE'
  | 'INVALID_LENGTH'

/**
 * Uniform error shape returned by the backend on failure responses.
 * Every response is JSON, and error responses always have the shape
 * `{ "error": { "code": string, "message": string } }`.
 */
export interface ApiErrorBody {
  error: {
    code: string
    message?: string
  }
}

/** Error thrown by the API client for any non-2xx response or network/parse failure. */
export class ApiError extends Error {
  readonly status: number | null
  readonly code: string | null

  constructor(message: string, status: number | null, code: string | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

/**
 * Error thrown by the API client when a request does not receive a response
 * within the per-request timeout (30s).
 *
 * This is a distinct, easily-detectable `ApiError` subclass so calling code
 * can branch on it in two equivalent ways:
 *   - `error instanceof ApiTimeoutError`
 *   - `error instanceof ApiError && error.code === 'TIMEOUT'`
 *
 * IMPORTANT (scope boundary): this error type only signals "the request
 * timed out". It carries no information about, and the API client has no
 * notion of, "the last status that was successfully displayed to the user".
 * Preserving a previously-displayed Redaction Job status on timeout is the
 * responsibility of the calling view/component, not of this API client module.
 */
export class ApiTimeoutError extends ApiError {
  constructor(timeoutMs: number) {
    super(`Request timed out after ${timeoutMs}ms`, null, 'TIMEOUT')
    this.name = 'ApiTimeoutError'
  }
}

/* -------------------------------------------------------------------------- */
/* POST /jobs                                                                  */
/* -------------------------------------------------------------------------- */

/** Success (200) response body for `POST /jobs`. */
export interface UploadResponse {
  jobId: string
  status: JobStatus
  fileName: string
  mode: JobMode
}

/* -------------------------------------------------------------------------- */
/* GET /jobs/{jobId}                                                          */
/* -------------------------------------------------------------------------- */

/**
 * Per-page text-layer health signal carried by a {@link DetectionSummary}.
 *
 * `textLayerChars` counts non-whitespace characters the text-layer path
 * extracted for the page; `ocrChars` counts those the OCR path recognized.
 * A large asymmetry is the diagnostic fingerprint of a damaged text layer
 * (e.g. a broken ToUnicode CMap). Purely informational — the backend writes
 * it and never routes on it, and the frontend must not branch on it either.
 */
export interface PageHealth {
  pageIndex: number
  textLayerChars: number
  ocrChars: number
}

/**
 * Diagnostic counts recorded for a Redaction Job.
 *
 * The dual-path fields — `textLayerRegions`, `ocrRegions`, `agreedRegions`,
 * and `pageHealth` — are **PDF-only** and therefore optional. `textLayerRegions`
 * and `ocrRegions` count the redaction rectangles each path contributed;
 * `agreedRegions` counts the OCR rectangles that overlap at least one
 * text-layer rectangle on the same page; the displayed dual-path total is
 * `textLayerRegions + ocrRegions`. A `.docx` Job has no text-layer-vs-OCR
 * distinction, so the backend omits these keys entirely rather than writing
 * zeros — a false `0` would read as "both paths ran and found nothing".
 * Consumers must treat their absence as "does not apply", not as zero.
 *
 * The LLM fields — `llmSpans` (the number of spans the LLM detection source
 * produced before merging) and `llmOnlySpans` (the number of merged regions
 * only the LLM found) — are **always present**, for both `.pdf` and `.docx`.
 *
 * Field names are byte-identical to the JSON the backend emits under
 * `detectionSummary`.
 */
export interface DetectionSummary {
  textLayerRegions?: number
  ocrRegions?: number
  agreedRegions?: number
  pageHealth?: PageHealth[]
  llmSpans: number
  llmOnlySpans: number
}

/**
 * Success (200) response body for `GET /jobs/{jobId}`.
 *
 * `detectionSummary` is `null` for `.docx` Jobs, for `failed` Jobs, and for
 * any Job recorded before dual-path detection existed, so
 * every consumer must handle the `null` case.
 */
export interface JobStatusResponse {
  jobId: string
  status: JobStatus
  /** English diagnostic text; not meant to be shown to the user. */
  errorMessage: string | null
  /**
   * Machine-readable failure reason of a `failed` Job (e.g. `REVIEW_TIMEOUT`),
   * translated by the UI. Absent/`null` for other statuses and for Jobs
   * recorded before this field existed.
   */
  errorCode?: string | null
  /** Values the failure text interpolates, e.g. `{ page: 3 }`. */
  errorParams?: Record<string, string | number> | null
  detectionSummary: DetectionSummary | null
  mode: JobMode
}

/* -------------------------------------------------------------------------- */
/* GET /rules                                                                  */
/* -------------------------------------------------------------------------- */

/** A single Redaction Rule as returned by `GET /rules`. */
export interface RedactionRuleDto {
  ruleName: string
  pattern: string
  enabled: boolean
  isBuiltIn: boolean
}

/** Success (200) response body for `GET /rules`. */
export interface RulesListResponse {
  rules: RedactionRuleDto[]
}

/* -------------------------------------------------------------------------- */
/* POST /rules, PATCH /rules/{ruleName}, DELETE /rules/{ruleName}              */
/* -------------------------------------------------------------------------- */

/** Success (202) response body shared by all Rule Operation submission endpoints. */
export interface RuleOperationSubmitResponse {
  operationId: string
}

/** Request body for `POST /rules` (create a Redaction Rule). */
export interface CreateRuleRequest {
  ruleName: string
  pattern: string
}

/** Request body for `PATCH /rules/{ruleName}` (enable/disable a Redaction Rule). */
export interface SetRuleEnabledRequest {
  enabled: boolean
}

/* -------------------------------------------------------------------------- */
/* GET /rule-operations/{operationId}                                        */
/* -------------------------------------------------------------------------- */

/** Success (200) response body for `GET /rule-operations/{operationId}`. */
export interface RuleOperationStatusResponse {
  operationId: string
  status: RuleOperationStatus
  errorReason: RuleOperationErrorReason | null
}

/* -------------------------------------------------------------------------- */
/* GET /jobs/{jobId}/detections                                               */
/* -------------------------------------------------------------------------- */

/** A single deduplicated detection value returned in the review-mode response. */
export interface DetectionValue {
  id: number
  entityType: string
  value: string
  count: number
  pages: number[]
  contextSnippet: string
}

/** Success (200) response body for `GET /jobs/{jobId}/detections`. */
export interface DetectionResults {
  values: DetectionValue[]
}

/* -------------------------------------------------------------------------- */
/* POST /jobs/{jobId}/redact                                                  */
/* -------------------------------------------------------------------------- */

/** Request body for `POST /jobs/{jobId}/redact`. */
export interface SubmitRedactionRequest {
  selectedIds: number[]
}

/** Success (202) response body for `POST /jobs/{jobId}/redact`. */
export interface SubmitRedactionResponse {
  status: string
}
