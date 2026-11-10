/**
 * The signed-in user's session: tokens, identity and refresh.
 *
 * Tokens live in `sessionStorage`, so they are scoped to the browser tab and
 * dropped when it closes. `getAccessToken` refreshes shortly before expiry;
 * when refreshing fails the session is cleared and listeners are notified,
 * which sends the app back to the sign-in screen.
 */
import {
  completeNewPassword,
  refreshTokens,
  revokeRefreshToken,
  signIn,
  type CognitoConfig,
  type CognitoTokens,
  type SignInResult,
} from './cognito'

export const ADMIN_GROUP = 'admins'
const STORAGE_KEY = 'pii-redaction.auth'
/** Refresh when the token expires within this many milliseconds. */
const REFRESH_MARGIN_MS = 60_000

export interface StoredSession {
  accessToken: string
  idToken: string
  refreshToken: string
  /** Epoch milliseconds at which the access token expires. */
  expiresAt: number
}

export interface SessionUser {
  email: string | null
  groups: string[]
  isAdmin: boolean
}

export type PendingChallenge = { session: string; username: string }

/** Decodes a JWT payload without verifying it (the API verifies tokens). */
export function decodeJwtPayload(token: string): Record<string, unknown> {
  const part = token.split('.')[1]
  if (!part) return {}
  try {
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4)
    const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0))
    return JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>
  } catch {
    return {}
  }
}

export function userFromIdToken(idToken: string): SessionUser {
  const claims = decodeJwtPayload(idToken)
  const rawGroups = claims['cognito:groups']
  const groups = Array.isArray(rawGroups) ? rawGroups.map(String) : []
  return {
    email: typeof claims.email === 'string' ? claims.email : null,
    groups,
    isAdmin: groups.includes(ADMIN_GROUP),
  }
}

export interface AuthSessionOptions {
  config: CognitoConfig
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null
  fetchImpl?: typeof fetch
  now?: () => number
}

export class AuthSession {
  private readonly config: CognitoConfig
  private readonly storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null
  private readonly fetchImpl: typeof fetch
  private readonly now: () => number
  private current: StoredSession | null
  private refreshing: Promise<string | null> | null = null
  private readonly listeners = new Set<() => void>()

  constructor(options: AuthSessionOptions) {
    this.config = options.config
    this.storage = options.storage === undefined ? safeSessionStorage() : options.storage
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init))
    this.now = options.now ?? Date.now
    this.current = this.load()
  }

  get isSignedIn(): boolean {
    return this.current !== null
  }

  get user(): SessionUser | null {
    return this.current ? userFromIdToken(this.current.idToken) : null
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** Starts sign-in. Returns a challenge when a new password must be set. */
  async signIn(email: string, password: string): Promise<PendingChallenge | null> {
    return this.accept(await signIn(this.config, email, password, this.fetchImpl))
  }

  async completeNewPassword(
    challenge: PendingChallenge,
    newPassword: string,
  ): Promise<PendingChallenge | null> {
    return this.accept(
      await completeNewPassword(
        this.config,
        challenge.username,
        newPassword,
        challenge.session,
        this.fetchImpl,
      ),
    )
  }

  /** A valid access token, refreshed if needed; null when signed out. */
  async getAccessToken(): Promise<string | null> {
    const current = this.current
    if (!current) return null
    if (current.expiresAt - this.now() > REFRESH_MARGIN_MS) {
      return current.accessToken
    }
    if (!this.refreshing) {
      this.refreshing = this.refresh(current).finally(() => {
        this.refreshing = null
      })
    }
    return this.refreshing
  }

  /** Clears the session; the refresh token is revoked best-effort. */
  signOut(): void {
    const current = this.current
    this.setSession(null)
    if (current) {
      revokeRefreshToken(this.config, current.refreshToken, this.fetchImpl).catch(() => {
        /* the tokens are gone locally either way */
      })
    }
  }

  private async refresh(current: StoredSession): Promise<string | null> {
    try {
      const tokens = await refreshTokens(this.config, current.refreshToken, this.fetchImpl)
      this.setSession(this.toStored(tokens, current.refreshToken))
      return tokens.accessToken
    } catch {
      this.setSession(null)
      return null
    }
  }

  private accept(result: SignInResult): PendingChallenge | null {
    if (result.kind === 'newPasswordRequired') {
      return { session: result.session, username: result.username }
    }
    this.setSession(this.toStored(result.tokens))
    return null
  }

  private toStored(tokens: CognitoTokens, previousRefreshToken?: string): StoredSession {
    const refreshToken = tokens.refreshToken ?? previousRefreshToken
    if (!refreshToken) {
      throw new Error('Sign-in returned no refresh token')
    }
    return {
      accessToken: tokens.accessToken,
      idToken: tokens.idToken,
      refreshToken,
      expiresAt: this.now() + tokens.expiresIn * 1000,
    }
  }

  private setSession(next: StoredSession | null): void {
    this.current = next
    try {
      if (next) this.storage?.setItem(STORAGE_KEY, JSON.stringify(next))
      else this.storage?.removeItem(STORAGE_KEY)
    } catch {
      /* storage unavailable: keep the in-memory session */
    }
    this.listeners.forEach((listener) => listener())
  }

  private load(): StoredSession | null {
    try {
      const raw = this.storage?.getItem(STORAGE_KEY)
      if (!raw) return null
      const parsed = JSON.parse(raw) as Partial<StoredSession>
      if (
        typeof parsed.accessToken === 'string' &&
        typeof parsed.idToken === 'string' &&
        typeof parsed.refreshToken === 'string' &&
        typeof parsed.expiresAt === 'number'
      ) {
        return parsed as StoredSession
      }
    } catch {
      /* corrupt or unavailable storage */
    }
    return null
  }
}

function safeSessionStorage(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage
  } catch {
    return null
  }
}
