import { describe, expect, it, vi } from 'vitest'
import { AuthSession, decodeJwtPayload, userFromIdToken } from './session'

const config = { region: 'us-west-2', clientId: 'c' }

function jwt(payload: Record<string, unknown>): string {
  const b64 = (o: unknown) => btoa(JSON.stringify(o)).replace(/=+$/, '')
  return `${b64({ alg: 'none' })}.${b64(payload)}.sig`
}

function memoryStorage() {
  const data = new Map<string, string>()
  return {
    data,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  }
}

function tokensResponse(access: string, extra: Record<string, unknown> = {}): Response {
  return new Response(
    JSON.stringify({
      AuthenticationResult: {
        AccessToken: access,
        IdToken: jwt({ email: 'u@example.com', 'cognito:groups': ['admins'] }),
        ExpiresIn: 3600,
        ...extra,
      },
    }),
    { status: 200 },
  )
}

describe('jwt helpers', () => {
  it('decodes the payload and reads email and groups', () => {
    const token = jwt({ email: 'a@b.c', 'cognito:groups': ['admins', 'x'] })
    expect(decodeJwtPayload(token).email).toBe('a@b.c')
    expect(userFromIdToken(token)).toEqual({ email: 'a@b.c', groups: ['admins', 'x'], isAdmin: true })
    expect(userFromIdToken(jwt({})).isAdmin).toBe(false)
    expect(decodeJwtPayload('garbage')).toEqual({})
  })
})

describe('AuthSession', () => {
  it('signs in, stores tokens and notifies listeners', async () => {
    const storage = memoryStorage()
    const fetchImpl = vi.fn<typeof fetch>(async () => tokensResponse('a1', { RefreshToken: 'r1' }))
    const session = new AuthSession({ config, storage, fetchImpl, now: () => 0 })
    const listener = vi.fn()
    session.subscribe(listener)

    expect(await session.signIn('u@example.com', 'pw')).toBeNull()
    expect(session.isSignedIn).toBe(true)
    expect(session.user?.isAdmin).toBe(true)
    expect(listener).toHaveBeenCalledTimes(1)
    expect(await session.getAccessToken()).toBe('a1')
    // A new session in the same tab picks the stored tokens up.
    expect(new AuthSession({ config, storage, fetchImpl }).isSignedIn).toBe(true)
  })

  it('refreshes once near expiry and keeps the refresh token', async () => {
    let now = 0
    const storage = memoryStorage()
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(tokensResponse('a1', { RefreshToken: 'r1' }))
      .mockResolvedValueOnce(tokensResponse('a2'))
    const session = new AuthSession({ config, storage, fetchImpl, now: () => now })
    await session.signIn('u', 'p')

    now = 3600_000 - 30_000
    const [t1, t2] = await Promise.all([session.getAccessToken(), session.getAccessToken()])
    expect([t1, t2]).toEqual(['a2', 'a2'])
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    const refreshBody = JSON.parse(fetchImpl.mock.calls[1][1]?.body as string)
    expect(refreshBody.AuthParameters.REFRESH_TOKEN).toBe('r1')
    expect(JSON.parse(storage.data.values().next().value as string).refreshToken).toBe('r1')
  })

  it('signs out when the refresh fails', async () => {
    let now = 0
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(tokensResponse('a1', { RefreshToken: 'r1' }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ __type: 'NotAuthorizedException' }), { status: 400 }))
    const session = new AuthSession({ config, storage: memoryStorage(), fetchImpl, now: () => now })
    await session.signIn('u', 'p')
    now = 3600_000
    expect(await session.getAccessToken()).toBeNull()
    expect(session.isSignedIn).toBe(false)
  })

  it('returns a challenge without signing in, then completes it', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ ChallengeName: 'NEW_PASSWORD_REQUIRED', Session: 's', ChallengeParameters: {} }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(tokensResponse('a1', { RefreshToken: 'r1' }))
    const session = new AuthSession({ config, storage: memoryStorage(), fetchImpl })
    const challenge = await session.signIn('u@example.com', 'temp')
    expect(challenge).toEqual({ session: 's', username: 'u@example.com' })
    expect(session.isSignedIn).toBe(false)
    expect(await session.completeNewPassword(challenge!, 'NewPassword123')).toBeNull()
    expect(session.isSignedIn).toBe(true)
  })

  it('sign-out clears storage and revokes the refresh token', async () => {
    const storage = memoryStorage()
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(tokensResponse('a1', { RefreshToken: 'r1' }))
      .mockResolvedValueOnce(new Response('{}', { status: 200 }))
    const session = new AuthSession({ config, storage, fetchImpl })
    await session.signIn('u', 'p')
    session.signOut()
    expect(session.isSignedIn).toBe(false)
    expect(storage.data.size).toBe(0)
    expect(await session.getAccessToken()).toBeNull()
    const revoke = fetchImpl.mock.calls[1][1]
    expect(new Headers(revoke?.headers).get('X-Amz-Target')).toBe(
      'AWSCognitoIdentityProviderService.RevokeToken',
    )
  })
})
