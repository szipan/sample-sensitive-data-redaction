// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/types'
import type { ManagedUser } from '../api/types'
import { UserManagementView } from './UserManagementView'

const me: ManagedUser = {
  username: 'u-me', email: 'me@example.com', role: 'admin', status: 'CONFIRMED',
  enabled: true, createdAt: null, isSelf: true,
}
const invited: ManagedUser = {
  username: 'u-new', email: 'new@example.com', role: 'user', status: 'FORCE_CHANGE_PASSWORD',
  enabled: true, createdAt: null, isSelf: false,
}

function client(overrides: Record<string, unknown> = {}) {
  return {
    listUsers: vi.fn(async () => ({ users: [me, invited] })),
    createUser: vi.fn(async () => ({ user: invited })),
    setUserRole: vi.fn(async () => ({ user: invited })),
    deleteUser: vi.fn(async () => undefined),
    resendInvite: vi.fn(async () => undefined),
    ...overrides,
  }
}

describe('UserManagementView', () => {
  it('lists users with role and status; own account cannot be changed', async () => {
    render(<UserManagementView apiClient={client()} />)
    const self = await screen.findByTestId('user-row-me@example.com')
    expect(self).toHaveTextContent('管理员')
    expect(self).toHaveTextContent('(你)')
    expect(within(self).getByRole('button', { name: '设为普通用户' })).toBeDisabled()
    expect(within(self).getByRole('button', { name: '删除' })).toBeDisabled()
    expect(within(self).queryByRole('button', { name: '重发邀请' })).toBeNull()

    const other = screen.getByTestId('user-row-new@example.com')
    expect(other).toHaveTextContent('普通用户')
    expect(other).toHaveTextContent('已邀请，未首次登录')
    expect(within(other).getByRole('button', { name: '设为管理员' })).toBeEnabled()
  })

  it('invites an administrator and refreshes the list', async () => {
    const api = client()
    render(<UserManagementView apiClient={api} />)
    await screen.findByTestId('user-row-me@example.com')
    await userEvent.type(screen.getByLabelText('邮箱'), ' boss@example.com ')
    await userEvent.selectOptions(screen.getByLabelText('角色'), 'admin')
    await userEvent.click(screen.getByRole('button', { name: '发送邀请' }))
    expect(api.createUser).toHaveBeenCalledWith('boss@example.com', 'admin')
    expect(await screen.findByRole('status')).toHaveTextContent('已向 boss@example.com 发送邀请。')
    await waitFor(() => expect(api.listUsers).toHaveBeenCalledTimes(2))
  })

  it('changes role, resends invitations and deletes after confirmation', async () => {
    const api = client()
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true)
    render(<UserManagementView apiClient={api} confirm={confirm} />)
    const row = await screen.findByTestId('user-row-new@example.com')

    await userEvent.click(within(row).getByRole('button', { name: '设为管理员' }))
    expect(api.setUserRole).toHaveBeenCalledWith('u-new', 'admin')

    await userEvent.click(within(screen.getByTestId('user-row-new@example.com')).getByRole('button', { name: '重发邀请' }))
    expect(api.resendInvite).toHaveBeenCalledWith('u-new')

    await userEvent.click(within(screen.getByTestId('user-row-new@example.com')).getByRole('button', { name: '删除' }))
    expect(api.deleteUser).not.toHaveBeenCalled()
    await userEvent.click(within(screen.getByTestId('user-row-new@example.com')).getByRole('button', { name: '删除' }))
    expect(api.deleteUser).toHaveBeenCalledWith('u-new')
  })

  it('shows specific errors from the API', async () => {
    const api = client({
      createUser: vi.fn(async () => {
        throw new ApiError('exists', 409, 'USER_EXISTS')
      }),
      setUserRole: vi.fn(async () => {
        throw new ApiError('last', 409, 'LAST_ADMIN')
      }),
    })
    render(<UserManagementView apiClient={api} />)
    await screen.findByTestId('user-row-me@example.com')
    await userEvent.type(screen.getByLabelText('邮箱'), 'new@example.com')
    await userEvent.click(screen.getByRole('button', { name: '发送邀请' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('该邮箱的用户已存在。')

    await userEvent.click(within(screen.getByTestId('user-row-new@example.com')).getByRole('button', { name: '设为管理员' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('不能移除最后一位管理员。')
  })
})
