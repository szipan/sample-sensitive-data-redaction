import { useCallback, useEffect, useState, type FormEvent } from 'react'
import type { ApiClient } from '../api/client'
import { ApiError } from '../api/types'
import type { ManagedUser, UserRole } from '../api/types'
import { useI18n } from '../i18n'
import type { MessageKey, TranslateFn } from '../i18n'

export interface UserManagementViewProps {
  apiClient: Pick<
    ApiClient,
    'listUsers' | 'createUser' | 'setUserRole' | 'deleteUser' | 'resendInvite'
  >
  /** Confirmation prompt before deleting; injectable for tests. */
  confirm?: (message: string) => boolean
}

const ERROR_KEYS: Record<string, MessageKey> = {
  INVALID_EMAIL: 'users.err.INVALID_EMAIL',
  INVALID_ROLE: 'users.err.INVALID_ROLE',
  USER_EXISTS: 'users.err.USER_EXISTS',
  USER_NOT_FOUND: 'users.err.USER_NOT_FOUND',
  CANNOT_MODIFY_SELF: 'users.err.CANNOT_MODIFY_SELF',
  LAST_ADMIN: 'users.err.LAST_ADMIN',
  ALREADY_ACTIVE: 'users.err.ALREADY_ACTIVE',
  FORBIDDEN: 'users.err.FORBIDDEN',
}

function describe(t: TranslateFn, error: unknown): string {
  if (error instanceof ApiError && error.code && error.code in ERROR_KEYS) {
    return t(ERROR_KEYS[error.code])
  }
  return t('users.err.generic')
}

function statusKey(status: string | null): MessageKey {
  return status === 'FORCE_CHANGE_PASSWORD' ? 'users.status.invited' : 'users.status.active'
}

/** Administrators invite users, change roles, resend invitations and delete users. */
export function UserManagementView({
  apiClient,
  confirm = (message) => window.confirm(message),
}: UserManagementViewProps) {
  const { t } = useI18n()
  const [users, setUsers] = useState<ManagedUser[]>([])
  const [listError, setListError] = useState<string | null>(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<UserRole>('user')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)

  const refresh = useCallback(async () => {
    try {
      setUsers((await apiClient.listUsers()).users)
      setListError(null)
    } catch (error) {
      setListError(describe(t, error))
    }
  }, [apiClient, t])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function run(action: () => Promise<unknown>, success: string) {
    setBusy(true)
    setMessage(null)
    try {
      await action()
      setMessage({ kind: 'ok', text: success })
      await refresh()
    } catch (error) {
      setMessage({ kind: 'error', text: describe(t, error) })
    } finally {
      setBusy(false)
    }
  }

  function onInvite(event: FormEvent) {
    event.preventDefault()
    const address = email.trim()
    void run(async () => {
      await apiClient.createUser(address, role)
      setEmail('')
      setRole('user')
    }, t('users.invited', { email: address }))
  }

  function onToggleRole(user: ManagedUser) {
    const next: UserRole = user.role === 'admin' ? 'user' : 'admin'
    void run(() => apiClient.setUserRole(user.username, next), t('users.roleChanged', { email: user.email ?? '' }))
  }

  function onDelete(user: ManagedUser) {
    if (!confirm(t('users.confirmDelete', { email: user.email ?? '' }))) return
    void run(() => apiClient.deleteUser(user.username), t('users.deleted', { email: user.email ?? '' }))
  }

  function onResend(user: ManagedUser) {
    void run(() => apiClient.resendInvite(user.username), t('users.resent', { email: user.email ?? '' }))
  }

  return (
    <section aria-labelledby="user-management-heading">
      <h2 id="user-management-heading">{t('users.title')}</h2>
      <p style={{ color: 'var(--color-fg-subtle)' }}>{t('users.rolesHint')}</p>
      {listError && <p role="alert" className="status-danger">{listError}</p>}
      {message && (
        <p role={message.kind === 'error' ? 'alert' : 'status'} className={message.kind === 'error' ? 'status-danger' : 'status-success'}>
          {message.text}
        </p>
      )}
      <table className="data-table">
        <thead>
          <tr>
            <th>{t('users.col.email')}</th>
            <th>{t('users.col.role')}</th>
            <th>{t('users.col.status')}</th>
            <th style={{ textAlign: 'right' }}>{t('users.col.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {users.map((user) => (
            <tr key={user.username} data-testid={`user-row-${user.email}`}>
              <td style={{ fontWeight: 600 }}>
                {user.email}
                {user.isSelf && <span style={{ marginLeft: '8px', fontSize: '12px', color: 'var(--color-fg-subtle)' }}>{t('users.you')}</span>}
              </td>
              <td>{user.role === 'admin' ? t('users.role.admin') : t('users.role.user')}</td>
              <td>{t(statusKey(user.status))}</td>
              <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                {user.status === 'FORCE_CHANGE_PASSWORD' && (
                  <button type="button" disabled={busy} onClick={() => onResend(user)} style={{ marginRight: '8px' }}>
                    {t('users.resend')}
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy || user.isSelf}
                  onClick={() => onToggleRole(user)}
                  style={{ marginRight: '8px' }}
                >
                  {user.role === 'admin' ? t('users.makeUser') : t('users.makeAdmin')}
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={busy || user.isSelf}
                  onClick={() => onDelete(user)}
                >
                  {t('users.delete')}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form onSubmit={onInvite} style={{ marginTop: '24px' }}>
        <h3>{t('users.inviteTitle')}</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <label htmlFor="invite-email">{t('users.col.email')}</label>
          <input
            id="invite-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            style={{ flex: 1 }}
          />
          <label htmlFor="invite-role">{t('users.col.role')}</label>
          <select id="invite-role" value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
            <option value="user">{t('users.role.user')}</option>
            <option value="admin">{t('users.role.admin')}</option>
          </select>
          <button type="submit" className="btn-primary" disabled={busy}>
            {t('users.invite')}
          </button>
          {/* Takes 2/3 of the free space, so the email input (flex 1) gets 1/3 */}
          <div aria-hidden="true" style={{ flex: 2 }} />
        </div>
        <p style={{ fontSize: '12px', color: 'var(--color-fg-subtle)' }}>{t('users.inviteHint')}</p>
      </form>
    </section>
  )
}
