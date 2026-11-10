/**
 * Minimal Amazon Cognito User Pools client for the browser.
 *
 * Talks to the Cognito Identity Provider JSON API directly (no SDK): sign in
 * with USER_PASSWORD_AUTH over HTTPS, answer the NEW_PASSWORD_REQUIRED
 * challenge that an administrator-created account gets on first sign-in,
 * refresh tokens, and revoke the refresh token on sign-out.
 */

export interface CognitoConfig {
  region: string
  clientId: string
}

export interface CognitoTokens {
  accessToken: string
  idToken: string
  /** Absent in a refresh response; the caller keeps the existing one. */
  refreshToken?: string
  /** Seconds until the access/ID token expires. */
  expiresIn: number
}

export type SignInResult =
  | { kind: 'signedIn'; tokens: CognitoTokens }
  | { kind: 'newPasswordRequired'; session: string; username: string }

/** A Cognito error, carrying its type (e.g. `NotAuthorizedException`). */
export class CognitoAuthError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'CognitoAuthError'
    this.code = code
  }
}

interface AuthenticationResult {
  AccessToken: string
  IdToken: string
  RefreshToken?: string
  ExpiresIn: number
}

async function call(
  config: CognitoConfig,
  target: string,
  body: Record<string, unknown>,
  fetchImpl: typeof fetch,
): Promise<Record<string, unknown>> {
  let response: Response
  try {
    response = await fetchImpl(`https://cognito-idp.${config.region}.amazonaws.com/`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': `AWSCognitoIdentityProviderService.${target}`,
      },
      body: JSON.stringify(body),
    })
  } catch {
    throw new CognitoAuthError('NetworkError', 'Could not reach the sign-in service.')
  }
  let payload: Record<string, unknown> = {}
  try {
    payload = (await response.json()) as Record<string, unknown>
  } catch {
    /* empty or non-JSON body */
  }
  if (!response.ok) {
    const rawType = typeof payload.__type === 'string' ? payload.__type : 'UnknownError'
    // `__type` may be prefixed, e.g. "com.amazonaws...#NotAuthorizedException".
    const code = rawType.split('#').pop() ?? rawType
    const message = typeof payload.message === 'string' ? payload.message : code
    throw new CognitoAuthError(code, message)
  }
  return payload
}

function toTokens(result: AuthenticationResult): CognitoTokens {
  return {
    accessToken: result.AccessToken,
    idToken: result.IdToken,
    refreshToken: result.RefreshToken,
    expiresIn: result.ExpiresIn,
  }
}

function signedInOrChallenge(payload: Record<string, unknown>, username: string): SignInResult {
  const result = payload.AuthenticationResult as AuthenticationResult | undefined
  if (result) {
    return { kind: 'signedIn', tokens: toTokens(result) }
  }
  if (payload.ChallengeName === 'NEW_PASSWORD_REQUIRED' && typeof payload.Session === 'string') {
    const params = (payload.ChallengeParameters ?? {}) as Record<string, string>
    return {
      kind: 'newPasswordRequired',
      session: payload.Session,
      // With email sign-in the challenge must name the internal username.
      username: params.USER_ID_FOR_SRP || username,
    }
  }
  throw new CognitoAuthError(
    'UnsupportedChallenge',
    `Unsupported sign-in step: ${String(payload.ChallengeName ?? 'unknown')}`,
  )
}

export async function signIn(
  config: CognitoConfig,
  username: string,
  password: string,
  fetchImpl: typeof fetch = fetch,
): Promise<SignInResult> {
  const payload = await call(
    config,
    'InitiateAuth',
    {
      AuthFlow: 'USER_PASSWORD_AUTH',
      ClientId: config.clientId,
      AuthParameters: { USERNAME: username, PASSWORD: password },
    },
    fetchImpl,
  )
  return signedInOrChallenge(payload, username)
}

export async function completeNewPassword(
  config: CognitoConfig,
  username: string,
  newPassword: string,
  session: string,
  fetchImpl: typeof fetch = fetch,
): Promise<SignInResult> {
  const payload = await call(
    config,
    'RespondToAuthChallenge',
    {
      ChallengeName: 'NEW_PASSWORD_REQUIRED',
      ClientId: config.clientId,
      Session: session,
      ChallengeResponses: { USERNAME: username, NEW_PASSWORD: newPassword },
    },
    fetchImpl,
  )
  return signedInOrChallenge(payload, username)
}

export async function refreshTokens(
  config: CognitoConfig,
  refreshToken: string,
  fetchImpl: typeof fetch = fetch,
): Promise<CognitoTokens> {
  const payload = await call(
    config,
    'InitiateAuth',
    {
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: config.clientId,
      AuthParameters: { REFRESH_TOKEN: refreshToken },
    },
    fetchImpl,
  )
  const result = payload.AuthenticationResult as AuthenticationResult | undefined
  if (!result) {
    throw new CognitoAuthError('RefreshFailed', 'Token refresh returned no tokens.')
  }
  return toTokens(result)
}

export async function revokeRefreshToken(
  config: CognitoConfig,
  refreshToken: string,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  await call(config, 'RevokeToken', { Token: refreshToken, ClientId: config.clientId }, fetchImpl)
}
