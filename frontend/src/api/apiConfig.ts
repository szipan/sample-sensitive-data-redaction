/**
 * Environment-specific resolution of the backend API base URL.
 *
 * The deployed backend API base URL is injected into the frontend purely
 * via the Vite env var `VITE_API_BASE_URL` at build time (see the
 * `.env.*` files and `frontend/README.md`). The frontend build therefore
 * has NO source/compile dependency on the `backend/` package — only this
 * string flows across the deploy boundary, so the frontend and backend are
 * independently deployable.
 *
 * This module owns the *policy* for turning that env var into a base URL
 * string; the actual `https://`-scheme enforcement lives in
 * `toHttpsBaseUrl` and is applied by `createApiClient`. Keeping
 * the resolution logic here as a pure function (independent of
 * `import.meta.env`, which is a build-time construct) lets it be unit
 * tested directly.
 *
 * HTTPS-only, no non-HTTPS fallback:
 * -------------------------------------------------------------------------
 * Whatever string this function returns is still passed through
 * `toHttpsBaseUrl`, so a configured non-`https://` value can never be used.
 * In addition, there is deliberately NO localhost/HTTP fallback in a
 * production build: if `VITE_API_BASE_URL` is missing there, resolution
 * hard-fails (throws) rather than silently falling back to a dev default
 * that could mask a misconfigured deployment.
 */

/**
 * Base URL used only in development mode when `VITE_API_BASE_URL` is unset,
 * e.g. running `vite dev` without an `.env` file. Never used in a
 * production build. Not meant to point at a real backend.
 */
export const DEFAULT_LOCAL_DEV_BASE_URL = 'https://localhost:4000'

export interface ApiBaseUrlEnv {
  /** Raw value of `VITE_API_BASE_URL` (may be `undefined` or empty). */
  configuredBaseUrl: string | undefined
  /**
   * Whether this is a production build (`import.meta.env.PROD`). In
   * production there is no localhost fallback — a missing base URL throws.
   */
  isProduction: boolean
}

/**
 * Resolves the backend API base URL from environment-specific config.
 *
 * - If `VITE_API_BASE_URL` is set (non-empty after trimming), it is used
 *   in every mode.
 * - Otherwise, in development the convenient `DEFAULT_LOCAL_DEV_BASE_URL`
 *   is used.
 * - Otherwise, in a production build this throws — there is no non-HTTPS
 *   or localhost fallback that could mask a misconfiguration.
 *
 * The returned value is a raw base URL string; scheme enforcement is done
 * later by `toHttpsBaseUrl`/`createApiClient`.
 */
export function resolveApiBaseUrl(env: ApiBaseUrlEnv): string {
  const configured = env.configuredBaseUrl?.trim()
  if (configured && configured.length > 0) {
    return configured
  }

  if (env.isProduction) {
    throw new Error(
      'VITE_API_BASE_URL is required for a production build but was not set. ' +
        'The production frontend has no localhost/HTTP fallback; ' +
        'provide the deployed backend HTTPS API base URL at build time, e.g. ' +
        '`VITE_API_BASE_URL=https://api.example.com npm run build`.',
    )
  }

  return DEFAULT_LOCAL_DEV_BASE_URL
}

export interface CognitoEnv {
  region: string | undefined
  clientId: string | undefined
}

/**
 * Resolves the Cognito user pool client the app signs in against, from
 * `VITE_COGNITO_REGION` and `VITE_COGNITO_CLIENT_ID`. Both are required in
 * every mode: without them nobody can sign in, so failing fast is clearer
 * than a sign-in form that cannot work.
 */
export function resolveCognitoConfig(env: CognitoEnv): { region: string; clientId: string } {
  const region = env.region?.trim()
  const clientId = env.clientId?.trim()
  if (!region || !clientId) {
    throw new Error(
      'VITE_COGNITO_REGION and VITE_COGNITO_CLIENT_ID are required. ' +
        'Use the UserPoolClientId output of the backend stack and its region.',
    )
  }
  if (!/^[a-z]{2}(-[a-z]+)+-\d$/.test(region)) {
    throw new Error(`VITE_COGNITO_REGION is not an AWS region: "${region}"`)
  }
  return { region, clientId }
}
