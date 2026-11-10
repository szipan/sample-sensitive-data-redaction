import { useState, type FormEvent } from 'react'
import { CognitoAuthError } from '../auth/cognito'
import type { AuthSession, PendingChallenge } from '../auth/session'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n'

export interface LoginViewProps {
  session: Pick<AuthSession, 'signIn' | 'completeNewPassword'>
}

const ERROR_KEYS: Record<string, MessageKey> = {
  NotAuthorizedException: 'auth.err.credentials',
  UserNotFoundException: 'auth.err.credentials',
  PasswordResetRequiredException: 'auth.err.resetRequired',
  InvalidPasswordException: 'auth.err.passwordPolicy',
  TooManyRequestsException: 'auth.err.tooMany',
  LimitExceededException: 'auth.err.tooMany',
  NetworkError: 'auth.err.network',
}

/** Sign-in form, plus the "set a new password" step for invited users. */
export function LoginView({ session }: LoginViewProps) {
  const { t } = useI18n()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [challenge, setChallenge] = useState<PendingChallenge | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function describe(err: unknown): string {
    if (err instanceof CognitoAuthError && err.code in ERROR_KEYS) {
      return t(ERROR_KEYS[err.code])
    }
    return t('auth.err.generic')
  }

  async function run(action: () => Promise<PendingChallenge | null>) {
    setBusy(true)
    setError(null)
    try {
      setChallenge(await action())
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(false)
    }
  }

  function onSignIn(event: FormEvent) {
    event.preventDefault()
    void run(() => session.signIn(email.trim(), password))
  }

  function onSetPassword(event: FormEvent) {
    event.preventDefault()
    if (!challenge) return
    if (newPassword !== confirmPassword) {
      setError(t('auth.err.mismatch'))
      return
    }
    void run(() => session.completeNewPassword(challenge, newPassword))
  }

  return (
    <section aria-labelledby="login-heading">
      <h2 id="login-heading">{challenge ? t('auth.newPasswordTitle') : t('auth.title')}</h2>
      {challenge ? (
        <form onSubmit={onSetPassword}>
          <p>{t('auth.newPasswordHint')}</p>
          <div className="form-row">
            <label htmlFor="new-password">{t('auth.newPassword')}</label>
            <input
              id="new-password"
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
            />
          </div>
          <div className="form-row">
            <label htmlFor="confirm-password">{t('auth.confirmPassword')}</label>
            <input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
            />
          </div>
          <button type="submit" className="btn-primary" disabled={busy}>
            {t('auth.setPassword')}
          </button>
        </form>
      ) : (
        <form onSubmit={onSignIn}>
          <div className="form-row">
            <label htmlFor="login-email">{t('auth.email')}</label>
            <input
              id="login-email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div className="form-row">
            <label htmlFor="login-password">{t('auth.password')}</label>
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button type="submit" className="btn-primary" disabled={busy}>
            {busy ? t('auth.signingIn') : t('auth.signIn')}
          </button>
        </form>
      )}
      {error && (
        <p role="alert" className="status-danger">
          {error}
        </p>
      )}
    </section>
  )
}
