/**
 * Typed HTTPS API client for the PII Document Redaction POC backend.
 *
 * Covers all backend operations:
 *   - uploadDocument         -> POST   /jobs
 *   - getJobStatus           -> GET    /jobs/{jobId}
 *   - downloadJob            -> GET    /jobs/{jobId}/download
 *   - listRules              -> GET    /rules
 *   - submitRuleOperation    -> POST   /rules | PATCH /rules/{ruleName} | DELETE /rules/{ruleName}
 *   - getRuleOperationStatus -> GET    /rule-operations/{operationId}
 *
 * Request/response shapes match design.md's "API Design" section.
 *
 * HTTPS-only, no HTTP fallback:
 * -------------------------------------------------------------------------
 * The base URL is validated/normalized by `toHttpsBaseUrl` below, which is
 * the ONLY place a base URL is ever constructed. That function:
 *   - throws immediately if a caller-supplied URL explicitly starts with
 *     `http://` (or any scheme other than `https://`), and
 *   - always produces a URL that starts with `https://` when only a
 *     host/path is supplied (no scheme).
 * There is no environment variable, constructor option, or code path in
 * this module that can produce a `http://` base URL — `createApiClient`
 * always routes through `toHttpsBaseUrl`, and no function accepts a raw,
 * unvalidated base URL. Callers (e.g. Vite env config wiring)
 * may supply just a host/path via configuration, but they can never opt
 * into plaintext HTTP.
 *
 * Timeout handling:
 * -------------------------------------------------------------------------
 * Every request issued through this client is wrapped by `fetchWithTimeout`
 * below, which aborts the request via `AbortController` if no response is
 * received within `DEFAULT_TIMEOUT_MS` (30 seconds) and rejects with the
 * distinguishable `ApiTimeoutError` (an `ApiError` subclass with
 * `code === 'TIMEOUT'`) instead of a generic network/abort error.
 *
 * Scope boundary — read before touching this file:
 *   This module's ONLY responsibility on timeout is to abort the in-flight
 *   `fetch` call and throw a clearly-identifiable `ApiTimeoutError`. It has
 *   NO notion of "the last Redaction Job status successfully displayed to
 *   the user" and MUST NOT be given one — it does not read, cache, or
 *   mutate any such state. A timeout must preserve the previously
 *   displayed status, so it is the CALLING code
 *   (the views/components) that must catch
 *   `ApiTimeoutError`, show a failure indication, and leave whatever status
 *   it last rendered untouched. Do not add "previous status" tracking here.
 *
 * Polling helpers are intentionally NOT implemented here — this
 * module only builds and issues individual requests.
 */

import type {
  AddressDetectionSource,
  ApiErrorBody,
  CreateRuleRequest,
  DetectionResults,
  JobMode,
  JobStatusResponse,
  RedactionRuleDto,
  RuleOperationStatusResponse,
  RuleOperationSubmitResponse,
  RulesListResponse,
  SetRuleEnabledRequest,
  SubmitRedactionResponse,
  UploadResponse,
} from './types'
import { ApiError, ApiTimeoutError } from './types'

/** Per-request timeout: 30 seconds. */
export const DEFAULT_TIMEOUT_MS = 30_000

/**
 * Issues a single request via `doFetch`, aborting it and throwing
 * `ApiTimeoutError` if no response arrives within `timeoutMs`.
 *
 * This is the only place `AbortController`/timers are used for request
 * timeouts in this module — every request-issuing function below routes
 * through it so the 30s timeout applies uniformly.
 *
 * Note: this function does not accept or consult any external
 * "previously displayed status" — see the module-level comment above.
 */
async function fetchWithTimeout(
  doFetch: typeof fetch,
  input: string,
  init: RequestInit,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await doFetch(input, { ...init, signal: controller.signal })
  } catch (err) {
    if (controller.signal.aborted) {
      throw new ApiTimeoutError(timeoutMs)
    }
    throw err
  } finally {
    clearTimeout(timeoutId)
  }
}

export type { RedactionRuleDto }

/**
 * Normalizes/validates a base URL so it always uses the `https://` scheme.
 *
 * - A URL that already starts with `https://` is returned unchanged
 *   (trailing slashes trimmed).
 * - A URL with no scheme (e.g. `api.example.com`) has `https://` prepended.
 * - A URL with any other explicit scheme (in particular `http://`) is
 *   REJECTED by throwing — there is no way to opt into non-HTTPS.
 */
export function toHttpsBaseUrl(rawBaseUrl: string): string {
  const trimmed = rawBaseUrl.trim()
  if (trimmed.length === 0) {
    throw new Error('API base URL must not be empty')
  }

  const schemeMatch = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\//.exec(trimmed)
  let withScheme: string
  if (schemeMatch) {
    const scheme = schemeMatch[1].toLowerCase()
    if (scheme !== 'https') {
      throw new Error(
        `API base URL must use the https:// scheme, got "${scheme}://". ` +
          'Non-HTTPS API base URLs are not supported.',
      )
    }
    withScheme = trimmed
  } else {
    withScheme = `https://${trimmed}`
  }

  return withScheme.replace(/\/+$/, '')
}

export interface ApiClientOptions {
  /** Host/path or full `https://` URL of the backend API. */
  baseUrl: string
  /** Injectable fetch implementation, primarily for testing. */
  fetchImpl?: typeof fetch
}

export type RuleOperationRequest =
  | { type: 'create'; ruleName: string; pattern: string }
  | { type: 'setEnabled'; ruleName: string; enabled: boolean }
  | { type: 'delete'; ruleName: string }

export interface ApiClient {
  readonly baseUrl: string
  uploadDocument(
    file: File,
    addressDetectionSource?: AddressDetectionSource,
    nameDetectionSource?: AddressDetectionSource,
    mode?: JobMode,
  ): Promise<UploadResponse>
  getJobStatus(jobId: string): Promise<JobStatusResponse>
  downloadJob(jobId: string): Promise<Blob>
  listRules(): Promise<RulesListResponse>
  submitRuleOperation(request: RuleOperationRequest): Promise<RuleOperationSubmitResponse>
  getRuleOperationStatus(operationId: string): Promise<RuleOperationStatusResponse>
  getDetections(jobId: string): Promise<DetectionResults>
  submitRedaction(jobId: string, selectedIds: number[]): Promise<SubmitRedactionResponse>
}

async function parseErrorBody(response: Response): Promise<ApiErrorBody['error'] | null> {
  try {
    const body = (await response.json()) as Partial<ApiErrorBody>
    if (body && typeof body === 'object' && body.error && typeof body.error.code === 'string') {
      return body.error
    }
    return null
  } catch {
    return null
  }
}

async function throwApiError(response: Response): Promise<never> {
  const error = await parseErrorBody(response)
  throw new ApiError(
    error?.message ?? `Request failed with status ${response.status}`,
    response.status,
    error?.code ?? null,
  )
}

/**
 * Creates a typed API client bound to a validated `https://` base URL.
 *
 * @throws if `options.baseUrl` resolves to anything other than `https://`.
 */
export function createApiClient(options: ApiClientOptions): ApiClient {
  const baseUrl = toHttpsBaseUrl(options.baseUrl)
  const doFetch = options.fetchImpl ?? fetch

  function url(path: string): string {
    return `${baseUrl}${path}`
  }

  async function uploadDocument(
    file: File,
    addressDetectionSource?: AddressDetectionSource,
    nameDetectionSource?: AddressDetectionSource,
    mode?: JobMode,
  ): Promise<UploadResponse> {
    const formData = new FormData()
    formData.append('file', file)
    // Optional per-entity detection-source switches (detection-source-switch
    // feature). Each is only appended when provided; the backend defaults an
    // absent field to 'llm'.
    if (addressDetectionSource) {
      formData.append('addressDetectionSource', addressDetectionSource)
    }
    if (nameDetectionSource) {
      formData.append('nameDetectionSource', nameDetectionSource)
    }
    // Optional mode field (detection-review-mode feature). Only appended when
    // provided; the backend defaults an absent field to 'auto'.
    if (mode) {
      formData.append('mode', mode)
    }

    const response = await fetchWithTimeout(doFetch, url('/jobs'), {
      method: 'POST',
      body: formData,
    })
    if (!response.ok) {
      await throwApiError(response)
    }
    return (await response.json()) as UploadResponse
  }

  async function getJobStatus(jobId: string): Promise<JobStatusResponse> {
    const response = await fetchWithTimeout(doFetch, url(`/jobs/${encodeURIComponent(jobId)}`), {
      method: 'GET',
    })
    if (!response.ok) {
      await throwApiError(response)
    }
    return (await response.json()) as JobStatusResponse
  }

  async function downloadJob(jobId: string): Promise<Blob> {
    const response = await fetchWithTimeout(
      doFetch,
      url(`/jobs/${encodeURIComponent(jobId)}/download`),
      { method: 'GET' },
    )
    if (!response.ok) {
      await throwApiError(response)
    }
    return await response.blob()
  }

  async function listRules(): Promise<RulesListResponse> {
    const response = await fetchWithTimeout(doFetch, url('/rules'), {
      method: 'GET',
    })
    if (!response.ok) {
      await throwApiError(response)
    }
    return (await response.json()) as RulesListResponse
  }

  async function submitRuleOperation(
    request: RuleOperationRequest,
  ): Promise<RuleOperationSubmitResponse> {
    let response: Response
    switch (request.type) {
      case 'create': {
        const body: CreateRuleRequest = {
          ruleName: request.ruleName,
          pattern: request.pattern,
        }
        response = await fetchWithTimeout(doFetch, url('/rules'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        break
      }
      case 'setEnabled': {
        const body: SetRuleEnabledRequest = { enabled: request.enabled }
        response = await fetchWithTimeout(
          doFetch,
          url(`/rules/${encodeURIComponent(request.ruleName)}`),
          {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          },
        )
        break
      }
      case 'delete': {
        response = await fetchWithTimeout(
          doFetch,
          url(`/rules/${encodeURIComponent(request.ruleName)}`),
          { method: 'DELETE' },
        )
        break
      }
    }
    if (!response.ok) {
      await throwApiError(response)
    }
    return (await response.json()) as RuleOperationSubmitResponse
  }

  async function getRuleOperationStatus(
    operationId: string,
  ): Promise<RuleOperationStatusResponse> {
    const response = await fetchWithTimeout(
      doFetch,
      url(`/rule-operations/${encodeURIComponent(operationId)}`),
      { method: 'GET' },
    )
    if (!response.ok) {
      await throwApiError(response)
    }
    return (await response.json()) as RuleOperationStatusResponse
  }

  async function getDetections(jobId: string): Promise<DetectionResults> {
    const response = await fetchWithTimeout(
      doFetch,
      url(`/jobs/${encodeURIComponent(jobId)}/detections`),
      { method: 'GET' },
    )
    if (!response.ok) {
      await throwApiError(response)
    }
    return (await response.json()) as DetectionResults
  }

  async function submitRedaction(
    jobId: string,
    selectedIds: number[],
  ): Promise<SubmitRedactionResponse> {
    const response = await fetchWithTimeout(
      doFetch,
      url(`/jobs/${encodeURIComponent(jobId)}/redact`),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ selectedIds }),
      },
    )
    if (!response.ok) {
      await throwApiError(response)
    }
    return (await response.json()) as SubmitRedactionResponse
  }

  return {
    baseUrl,
    uploadDocument,
    getJobStatus,
    downloadJob,
    listRules,
    submitRuleOperation,
    getRuleOperationStatus,
    getDetections,
    submitRedaction,
  }
}
