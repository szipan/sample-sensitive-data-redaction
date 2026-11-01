/**
 * App-wide `ApiClient` singleton.
 *
 * Views (starting with `UploadView`) need a single `ApiClient` instance
 * rather than each constructing its own. This module creates that instance
 * once, bound to a base URL resolved from environment-specific config.
 *
 * Environment wiring:
 *   The backend API base URL is injected purely via the Vite env var
 *   `VITE_API_BASE_URL` at build time (configured per environment through
 *   `.env.development` / `.env.production` / deploy-time env — see
 *   `frontend/README.md`). `resolveApiBaseUrl` (in `apiConfig.ts`) owns the
 *   per-mode policy:
 *     - value set  -> use it (every mode),
 *     - unset + dev -> convenient localhost default,
 *     - unset + prod -> hard-fail (throw), NO localhost/HTTP fallback, so a
 *       misconfigured production deployment can never silently point at the
 *       wrong backend.
 *   Because the base URL crosses the deploy boundary as a single string,
 *   this module has no source dependency on the `backend/` package.
 *
 * `createApiClient`/`toHttpsBaseUrl` already enforce
 * HTTPS-only, no HTTP fallback — this module does not
 * duplicate that logic, it just supplies the resolved base URL, which is
 * then validated to be `https://` by `createApiClient`.
 */
import { createApiClient } from './client'
import { resolveApiBaseUrl } from './apiConfig'

const baseUrl = resolveApiBaseUrl({
  configuredBaseUrl: import.meta.env.VITE_API_BASE_URL,
  isProduction: import.meta.env.PROD,
})

/** The single `ApiClient` instance used throughout the app. */
export const apiClient = createApiClient({ baseUrl })
