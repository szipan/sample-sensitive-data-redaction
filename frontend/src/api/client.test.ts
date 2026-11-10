import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApiClient, DEFAULT_TIMEOUT_MS, toHttpsBaseUrl } from './client'
import type { DetectionSummary, JobStatusResponse } from './types'
import { ApiError, ApiTimeoutError } from './types'

describe('toHttpsBaseUrl (HTTPS-only, no HTTP fallback)', () => {
  it('rejects an explicit http:// base URL', () => {
    expect(() => toHttpsBaseUrl('http://api.example.com')).toThrow(/https/i)
  })

  it('rejects other non-https schemes', () => {
    expect(() => toHttpsBaseUrl('ftp://api.example.com')).toThrow(/https/i)
  })

  it('accepts an explicit https:// base URL unchanged (minus trailing slash)', () => {
    expect(toHttpsBaseUrl('https://api.example.com/')).toBe('https://api.example.com')
  })

  it('prepends https:// when no scheme is supplied', () => {
    expect(toHttpsBaseUrl('api.example.com')).toBe('https://api.example.com')
  })

  it('rejects an empty base URL', () => {
    expect(() => toHttpsBaseUrl('')).toThrow()
  })
})

describe('createApiClient', () => {
  it('throws when constructed with a non-https base URL', () => {
    expect(() => createApiClient({ baseUrl: 'http://api.example.com' })).toThrow(/https/i)
  })

  it('builds the correct request URL for getJobStatus', async () => {
    const fetchImpl = vi.fn<typeof fetch>(
      async () =>
        new Response(JSON.stringify({ jobId: 'job-1', status: 'processing', errorMessage: null }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    const result = await client.getJobStatus('job-1')

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [calledUrl, calledInit] = fetchImpl.mock.calls[0]
    expect(calledUrl).toBe('https://api.example.com/jobs/job-1')
    expect(calledInit?.method).toBe('GET')
    expect(result.jobId).toBe('job-1')
  })

  it('surfaces detectionSummary from getJobStatus', async () => {
    const summary: DetectionSummary = {
      textLayerRegions: 1,
      ocrRegions: 3,
      agreedRegions: 1,
      pageHealth: [{ pageIndex: 0, textLayerChars: 19, ocrChars: 23 }],
      llmSpans: 7,
      llmOnlySpans: 2,
    }
    const fetchImpl = vi.fn<typeof fetch>(
      async () =>
        new Response(
          JSON.stringify({
            jobId: 'job-1',
            status: 'completed',
            errorMessage: null,
            detectionSummary: summary,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    const result: JobStatusResponse = await client.getJobStatus('job-1')

    expect(result.detectionSummary).toEqual(summary)
  })

  it('surfaces a null detectionSummary from getJobStatus', async () => {
    const fetchImpl = vi.fn<typeof fetch>(
      async () =>
        new Response(
          JSON.stringify({
            jobId: 'job-1',
            status: 'completed',
            errorMessage: null,
            detectionSummary: null,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    const result: JobStatusResponse = await client.getJobStatus('job-1')

    expect(result.detectionSummary).toBeNull()
  })

  it('builds the correct request URL and body for listRules', async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(JSON.stringify({ rules: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    await client.listRules()

    expect(fetchImpl).toHaveBeenCalledWith(
      'https://api.example.com/rules',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('builds the correct request URL, method, and body for submitRuleOperation (create)', async () => {
    const fetchImpl = vi.fn<typeof fetch>(
      async () =>
        new Response(JSON.stringify({ operationId: 'op-1' }), {
          status: 202,
          headers: { 'Content-Type': 'application/json' },
        }),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    const result = await client.submitRuleOperation({
      type: 'create',
      ruleName: 'custom-rule',
      pattern: '^abc$',
    })

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [calledUrl, calledInit] = fetchImpl.mock.calls[0]
    expect(calledUrl).toBe('https://api.example.com/rules')
    expect(calledInit?.method).toBe('POST')
    expect(JSON.parse(calledInit?.body as string)).toEqual({
      ruleName: 'custom-rule',
      pattern: '^abc$',
    })
    expect(result.operationId).toBe('op-1')
  })

  it('builds the correct request URL and method for submitRuleOperation (setEnabled)', async () => {
    const fetchImpl = vi.fn<typeof fetch>(
      async () =>
        new Response(JSON.stringify({ operationId: 'op-2' }), {
          status: 202,
          headers: { 'Content-Type': 'application/json' },
        }),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    await client.submitRuleOperation({ type: 'setEnabled', ruleName: 'my rule', enabled: false })

    const [calledUrl, calledInit] = fetchImpl.mock.calls[0]
    expect(calledUrl).toBe('https://api.example.com/rules/my%20rule')
    expect(calledInit?.method).toBe('PATCH')
    expect(JSON.parse(calledInit?.body as string)).toEqual({ enabled: false })
  })

  it('builds the correct request URL for getRuleOperationStatus', async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ operationId: 'op-3', status: 'applied', errorReason: null }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    await client.getRuleOperationStatus('op-3')

    expect(fetchImpl).toHaveBeenCalledWith(
      'https://api.example.com/rule-operations/op-3',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('throws an ApiError with the backend error code on non-2xx responses', async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ error: { code: 'JOB_NOT_FOUND', message: 'not found' } }),
          { status: 404, headers: { 'Content-Type': 'application/json' } },
        ),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    await expect(client.getJobStatus('missing')).rejects.toMatchObject({
      code: 'JOB_NOT_FOUND',
      status: 404,
    })
  })
})

describe('request timeout (distinguishable timeout error)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('rejects with a distinguishable ApiTimeoutError if no response arrives within 30s', async () => {
    let capturedSignal: AbortSignal | undefined
    const fetchImpl = vi.fn<typeof fetch>((_input, init) => {
      capturedSignal = init?.signal ?? undefined
      // Simulate a fetch that never resolves on its own, but honors abort.
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          const abortError = new DOMException('The operation was aborted.', 'AbortError')
          reject(abortError)
        })
      })
    })

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    const resultPromise = client.getJobStatus('job-1')

    // Attach rejection assertions before advancing timers so the rejection
    // is observed instead of becoming an unhandled rejection.
    const assertion = expect(resultPromise).rejects.toBeInstanceOf(ApiTimeoutError)

    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS)

    await assertion
    await expect(resultPromise).rejects.toMatchObject({ code: 'TIMEOUT' })
    await expect(resultPromise).rejects.toBeInstanceOf(ApiError)

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(capturedSignal).toBeInstanceOf(AbortSignal)
    expect(capturedSignal?.aborted).toBe(true)
  })

  it('passes an AbortSignal to fetch that is not yet aborted before the timeout elapses', async () => {
    const fetchImpl = vi.fn<typeof fetch>(
      async () =>
        new Response(JSON.stringify({ rules: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    )

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    await client.listRules()

    const [, calledInit] = fetchImpl.mock.calls[0]
    expect(calledInit?.signal).toBeInstanceOf(AbortSignal)
    expect((calledInit?.signal as AbortSignal).aborted).toBe(false)
  })

  it('does not time out a request that resolves before the 30s deadline', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => {
      // Resolve quickly relative to the 30s timeout, without relying on
      // real elapsed time (fake timers are active).
      return new Response(
        JSON.stringify({ jobId: 'job-1', status: 'processing', errorMessage: null }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    const result = await client.getJobStatus('job-1')

    expect(result.jobId).toBe('job-1')
    // Advancing time after a successful resolution must not surface a
    // timeout error (the timer should have been cleared).
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS + 1_000)
  })

  it('propagates non-timeout fetch failures unchanged (no false-positive TimeoutError)', async () => {
    const networkError = new Error('network down')
    const fetchImpl = vi.fn<typeof fetch>(async () => {
      throw networkError
    })

    const client = createApiClient({ baseUrl: 'https://api.example.com', fetchImpl })
    await expect(client.getJobStatus('job-1')).rejects.toBe(networkError)
  })
})

describe('createApiClient authentication', () => {
  it('sends the bearer token and reports 401', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => new Response('{}', { status: 401 }))
    const onUnauthorized = vi.fn()
    const client = createApiClient({
      baseUrl: 'https://api.example.com',
      fetchImpl,
      getAccessToken: async () => 'tok',
      onUnauthorized,
    })
    await expect(client.listRules()).rejects.toBeInstanceOf(Error)
    const init = fetchImpl.mock.calls[0][1]
    expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer tok')
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('keeps existing headers and omits Authorization without a token', async () => {
    const fetchImpl = vi.fn<typeof fetch>(
      async () => new Response(JSON.stringify({ operationId: 'o', status: 'queued' }), { status: 202 }),
    )
    const client = createApiClient({
      baseUrl: 'https://api.example.com',
      fetchImpl,
      getAccessToken: async () => null,
    })
    await client.submitRuleOperation({ type: 'create', ruleName: 'r', pattern: 'x' })
    const headers = new Headers(fetchImpl.mock.calls[0][1]?.headers)
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.has('Authorization')).toBe(false)
  })
})
