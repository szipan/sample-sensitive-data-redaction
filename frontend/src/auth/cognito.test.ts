import { describe, expect, it, vi } from 'vitest'
import { CognitoAuthError, completeNewPassword, refreshTokens, signIn } from './cognito'

const config = { region: 'us-west-2', clientId: 'client-123' }

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status })
}

describe('cognito client', () => {
  it('signs in with USER_PASSWORD_AUTH against the regional endpoint', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, {
        AuthenticationResult: { AccessToken: 'a', IdToken: 'i', RefreshToken: 'r', ExpiresIn: 3600 },
      }),
    )
    const result = await signIn(config, 'u@example.com', 'pw', fetchImpl)
    expect(result).toEqual({
      kind: 'signedIn',
      tokens: { accessToken: 'a', idToken: 'i', refreshToken: 'r', expiresIn: 3600 },
    })
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('https://cognito-idp.us-west-2.amazonaws.com/')
    const headers = init?.headers as Record<string, string>
    expect(headers['X-Amz-Target']).toBe('AWSCognitoIdentityProviderService.InitiateAuth')
    expect(JSON.parse(init?.body as string)).toEqual({
      AuthFlow: 'USER_PASSWORD_AUTH',
      ClientId: 'client-123',
      AuthParameters: { USERNAME: 'u@example.com', PASSWORD: 'pw' },
    })
  })

  it('returns the new-password challenge with the internal username', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, {
        ChallengeName: 'NEW_PASSWORD_REQUIRED',
        Session: 'sess',
        ChallengeParameters: { USER_ID_FOR_SRP: 'internal-id' },
      }),
    )
    expect(await signIn(config, 'u@example.com', 'temp', fetchImpl)).toEqual({
      kind: 'newPasswordRequired',
      session: 'sess',
      username: 'internal-id',
    })
  })

  it('answers the challenge with RespondToAuthChallenge', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, {
        AuthenticationResult: { AccessToken: 'a', IdToken: 'i', RefreshToken: 'r', ExpiresIn: 60 },
      }),
    )
    await completeNewPassword(config, 'internal-id', 'NewPassword123', 'sess', fetchImpl)
    const [, init] = fetchImpl.mock.calls[0]
    expect(new Headers(init?.headers).get('X-Amz-Target')).toBe(
      'AWSCognitoIdentityProviderService.RespondToAuthChallenge',
    )
    expect(JSON.parse(init?.body as string)).toEqual({
      ChallengeName: 'NEW_PASSWORD_REQUIRED',
      ClientId: 'client-123',
      Session: 'sess',
      ChallengeResponses: { USERNAME: 'internal-id', NEW_PASSWORD: 'NewPassword123' },
    })
  })

  it('maps Cognito error types, including prefixed ones', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse(400, { __type: 'com.amazon#NotAuthorizedException', message: 'Incorrect' }),
    )
    await expect(signIn(config, 'u', 'p', fetchImpl)).rejects.toMatchObject({
      code: 'NotAuthorizedException',
    })
  })

  it('reports a network failure as NetworkError', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => {
      throw new TypeError('Failed to fetch')
    })
    const err = await signIn(config, 'u', 'p', fetchImpl).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(CognitoAuthError)
    expect((err as CognitoAuthError).code).toBe('NetworkError')
  })

  it('refreshes tokens with REFRESH_TOKEN_AUTH', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, { AuthenticationResult: { AccessToken: 'a2', IdToken: 'i2', ExpiresIn: 3600 } }),
    )
    const tokens = await refreshTokens(config, 'r', fetchImpl)
    expect(tokens.accessToken).toBe('a2')
    expect(tokens.refreshToken).toBeUndefined()
    expect(JSON.parse(fetchImpl.mock.calls[0][1]?.body as string).AuthParameters).toEqual({
      REFRESH_TOKEN: 'r',
    })
  })
})
