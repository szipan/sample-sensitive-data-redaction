import { describe, expect, it } from 'vitest'
import { DEFAULT_LOCAL_DEV_BASE_URL, resolveApiBaseUrl } from './apiConfig'
import { createApiClient, toHttpsBaseUrl } from './client'

/**
 * Environment-specific API base URL wiring.
 *
 * `import.meta.env` is a build-time construct, so these tests exercise the
 * pure `resolveApiBaseUrl` resolution function directly (the same function
 * `apiClient.ts` feeds `import.meta.env` into) and confirm the resolved
 * value still flows through the `https://` enforcement of `toHttpsBaseUrl`
 * / `createApiClient`.
 */
describe('resolveApiBaseUrl (config-driven, independent deploy)', () => {
  it('uses a configured VITE_API_BASE_URL in production', () => {
    expect(
      resolveApiBaseUrl({ configuredBaseUrl: 'https://api.example.com', isProduction: true }),
    ).toBe('https://api.example.com')
  })

  it('uses a configured VITE_API_BASE_URL in development', () => {
    expect(
      resolveApiBaseUrl({ configuredBaseUrl: 'https://api.example.com', isProduction: false }),
    ).toBe('https://api.example.com')
  })

  it('trims surrounding whitespace on a configured value', () => {
    expect(
      resolveApiBaseUrl({ configuredBaseUrl: '  https://api.example.com  ', isProduction: true }),
    ).toBe('https://api.example.com')
  })

  it('falls back to the localhost dev default when unset in development', () => {
    expect(resolveApiBaseUrl({ configuredBaseUrl: undefined, isProduction: false })).toBe(
      DEFAULT_LOCAL_DEV_BASE_URL,
    )
    expect(resolveApiBaseUrl({ configuredBaseUrl: '', isProduction: false })).toBe(
      DEFAULT_LOCAL_DEV_BASE_URL,
    )
    expect(resolveApiBaseUrl({ configuredBaseUrl: '   ', isProduction: false })).toBe(
      DEFAULT_LOCAL_DEV_BASE_URL,
    )
  })
})

describe('resolveApiBaseUrl production hard-fail (no silent fallback)', () => {
  it('throws when VITE_API_BASE_URL is missing in a production build', () => {
    expect(() => resolveApiBaseUrl({ configuredBaseUrl: undefined, isProduction: true })).toThrow(
      /VITE_API_BASE_URL/,
    )
  })

  it('throws when VITE_API_BASE_URL is empty in a production build', () => {
    expect(() => resolveApiBaseUrl({ configuredBaseUrl: '', isProduction: true })).toThrow(
      /VITE_API_BASE_URL/,
    )
    expect(() => resolveApiBaseUrl({ configuredBaseUrl: '   ', isProduction: true })).toThrow(
      /VITE_API_BASE_URL/,
    )
  })

  it('never returns the localhost dev default in production', () => {
    expect(() => resolveApiBaseUrl({ configuredBaseUrl: undefined, isProduction: true })).toThrow()
    // Sanity: the dev default is only ever used in development mode.
    expect(resolveApiBaseUrl({ configuredBaseUrl: undefined, isProduction: false })).toBe(
      DEFAULT_LOCAL_DEV_BASE_URL,
    )
  })
})

describe('resolved base URL still enforces HTTPS', () => {
  it('constructs a client against a valid https:// resolved base URL', () => {
    const resolved = resolveApiBaseUrl({
      configuredBaseUrl: 'https://api.example.com',
      isProduction: true,
    })
    const client = createApiClient({ baseUrl: resolved })
    expect(client.baseUrl).toBe('https://api.example.com')
  })

  it('rejects a non-https configured base URL via toHttpsBaseUrl', () => {
    const resolved = resolveApiBaseUrl({
      configuredBaseUrl: 'http://api.example.com',
      isProduction: true,
    })
    // resolveApiBaseUrl passes the raw string through; HTTPS enforcement
    // happens downstream and must still reject it.
    expect(() => toHttpsBaseUrl(resolved)).toThrow(/https/i)
    expect(() => createApiClient({ baseUrl: resolved })).toThrow(/https/i)
  })

  it('the localhost dev default is itself https://', () => {
    expect(toHttpsBaseUrl(DEFAULT_LOCAL_DEV_BASE_URL)).toBe(DEFAULT_LOCAL_DEV_BASE_URL)
  })
})
