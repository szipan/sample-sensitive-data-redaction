// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { CognitoAuthError } from '../auth/cognito'
import { LoginView } from './LoginView'

function session(overrides: Partial<Parameters<typeof LoginView>[0]['session']> = {}) {
  return {
    signIn: vi.fn(async () => null),
    completeNewPassword: vi.fn(async () => null),
    ...overrides,
  }
}

describe('LoginView', () => {
  it('signs in with the trimmed email and password', async () => {
    const s = session()
    render(<LoginView session={s} />)
    await userEvent.type(screen.getByLabelText('邮箱'), ' u@example.com ')
    await userEvent.type(screen.getByLabelText('密码'), 'pw')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))
    expect(s.signIn).toHaveBeenCalledWith('u@example.com', 'pw')
  })

  it('shows a specific message for wrong credentials', async () => {
    const s = session({
      signIn: vi.fn(async () => {
        throw new CognitoAuthError('NotAuthorizedException', 'Incorrect username or password.')
      }),
    })
    render(<LoginView session={s} />)
    await userEvent.type(screen.getByLabelText('邮箱'), 'u@example.com')
    await userEvent.type(screen.getByLabelText('密码'), 'bad')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('邮箱或密码不正确。')
  })

  it('asks for a new password on first sign-in and checks confirmation', async () => {
    const challenge = { session: 's', username: 'internal' }
    const s = session({ signIn: vi.fn(async () => challenge) })
    render(<LoginView session={s} />)
    await userEvent.type(screen.getByLabelText('邮箱'), 'u@example.com')
    await userEvent.type(screen.getByLabelText('密码'), 'temp')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    await userEvent.type(await screen.findByLabelText('新密码'), 'NewPassword123')
    await userEvent.type(screen.getByLabelText('确认新密码'), 'Different123')
    await userEvent.click(screen.getByRole('button', { name: '设置密码并登录' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('两次输入的密码不一致。')
    expect(s.completeNewPassword).not.toHaveBeenCalled()

    await userEvent.clear(screen.getByLabelText('确认新密码'))
    await userEvent.type(screen.getByLabelText('确认新密码'), 'NewPassword123')
    await userEvent.click(screen.getByRole('button', { name: '设置密码并登录' }))
    expect(s.completeNewPassword).toHaveBeenCalledWith(challenge, 'NewPassword123')
  })
})
