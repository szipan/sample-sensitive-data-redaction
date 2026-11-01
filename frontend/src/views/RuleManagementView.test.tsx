// @vitest-environment jsdom
/**
 * Component tests for `RuleManagementView`.
 *
 * `pollRuleOperationStatus` (../api/polling) is mocked here rather than
 * exercised for real, mirroring `UploadView.test.tsx`'s approach for
 * `pollJobStatus`: the polling module already has its own thorough test
 * suite (polling.test.ts). These tests only need to verify that
 * `RuleManagementView` (a) renders the rule collection from `listRules()`
 * with correct built-in/custom indication, (b) submits the right
 * `submitRuleOperation` request for create/setEnabled/delete, (c) refreshes
 * the list on a mocked `applied` terminal result, and (d) shows the
 * specific rejection message on a mocked `rejected` terminal result.
 *
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ApiClient, RuleOperationRequest } from '../api/client'
import type { PollCallbacks, PollHandle } from '../api/polling'
import type { RedactionRuleDto, RuleOperationStatusResponse } from '../api/types'
import * as polling from '../api/polling'
import { RuleManagementView } from './RuleManagementView'

function createMockApiClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    baseUrl: 'https://api.example.com',
    uploadDocument: vi.fn(),
    getJobStatus: vi.fn(),
    downloadJob: vi.fn(),
    listRules: vi.fn().mockResolvedValue({ rules: [] }),
    submitRuleOperation: vi.fn(),
    getRuleOperationStatus: vi.fn(),
    getDetections: vi.fn(),
    submitRedaction: vi.fn(),
    ...overrides,
  }
}

const builtInRule: RedactionRuleDto = {
  ruleName: '身份证号',
  pattern: '\\d{17}[\\dXx]',
  enabled: true,
  isBuiltIn: true,
}

const customRule: RedactionRuleDto = {
  ruleName: '自定义规则',
  pattern: 'foo',
  enabled: false,
  isBuiltIn: false,
}

/**
 * Captures the fetch function / callbacks handed to a mocked
 * `pollRuleOperationStatus` call and drives its terminal result.
 */
function mockPollRuleOperationStatus(): {
  resolveTerminal: (result: RuleOperationStatusResponse) => void
} {
  let capturedCallbacks: PollCallbacks<RuleOperationStatusResponse> | undefined
  const fakeHandle: PollHandle = { start: vi.fn(), stop: vi.fn(), isStopped: false }
  vi.spyOn(polling, 'pollRuleOperationStatus').mockImplementation((_fetchFn, callbacks) => {
    capturedCallbacks = callbacks as typeof capturedCallbacks
    return fakeHandle
  })

  return {
    resolveTerminal: (result) => {
      act(() => {
        capturedCallbacks!.onTerminal?.(result)
      })
    },
  }
}

describe('RuleManagementView', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders the rule collection from listRules with correct built-in/custom indication', async () => {
    const listRules = vi.fn().mockResolvedValue({ rules: [builtInRule, customRule] })
    const apiClient = createMockApiClient({ listRules })

    render(<RuleManagementView apiClient={apiClient} />)

    await waitFor(() => expect(listRules).toHaveBeenCalledTimes(1))

    const builtInRow = await screen.findByTestId(`rule-row-${builtInRule.ruleName}`)
    expect(builtInRow.textContent).toContain(builtInRule.ruleName)
    expect(builtInRow.textContent).toContain(builtInRule.pattern)
    expect(builtInRow.textContent).toContain('已启用')
    expect(builtInRow.textContent).toContain('内置规则')

    const customRow = screen.getByTestId(`rule-row-${customRule.ruleName}`)
    expect(customRow.textContent).toContain(customRule.ruleName)
    expect(customRow.textContent).toContain(customRule.pattern)
    expect(customRow.textContent).toContain('已禁用')
    expect(customRow.textContent).toContain('自定义规则')
  })

  it('submits a create Rule Operation and refreshes the list once it is applied', async () => {
    const listRules = vi
      .fn()
      .mockResolvedValueOnce({ rules: [] })
      .mockResolvedValueOnce({ rules: [customRule] })
    const submitRuleOperation = vi.fn().mockResolvedValue({ operationId: 'op-1' })
    const apiClient = createMockApiClient({ listRules, submitRuleOperation })
    const { resolveTerminal } = mockPollRuleOperationStatus()
    const user = userEvent.setup()

    render(<RuleManagementView apiClient={apiClient} />)
    await waitFor(() => expect(listRules).toHaveBeenCalledTimes(1))

    await user.type(screen.getByLabelText('名称'), customRule.ruleName)
    await user.type(screen.getByLabelText('正则表达式'), customRule.pattern)
    await user.click(screen.getByRole('button', { name: '新建' }))

    await waitFor(() =>
      expect(submitRuleOperation).toHaveBeenCalledWith({
        type: 'create',
        ruleName: customRule.ruleName,
        pattern: customRule.pattern,
      } satisfies RuleOperationRequest),
    )

    resolveTerminal({ operationId: 'op-1', status: 'applied', errorReason: null })

    await waitFor(() => expect(listRules).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId(`rule-row-${customRule.ruleName}`)).toBeInTheDocument()
  })

  it('shows a specific duplicate-name message when create is rejected with DUPLICATE_NAME', async () => {
    const submitRuleOperation = vi.fn().mockResolvedValue({ operationId: 'op-2' })
    const apiClient = createMockApiClient({ submitRuleOperation })
    const { resolveTerminal } = mockPollRuleOperationStatus()
    const user = userEvent.setup()

    render(<RuleManagementView apiClient={apiClient} />)
    await waitFor(() => expect(apiClient.listRules).toHaveBeenCalledTimes(1))

    await user.type(screen.getByLabelText('名称'), '重复名称')
    await user.type(screen.getByLabelText('正则表达式'), 'abc')
    await user.click(screen.getByRole('button', { name: '新建' }))

    await waitFor(() => expect(submitRuleOperation).toHaveBeenCalledTimes(1))

    resolveTerminal({ operationId: 'op-2', status: 'rejected', errorReason: 'DUPLICATE_NAME' })

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('该名称已存在')
  })

  it('toggling enable/disable calls submitRuleOperation with type setEnabled', async () => {
    const listRules = vi.fn().mockResolvedValue({ rules: [customRule] })
    const submitRuleOperation = vi.fn().mockResolvedValue({ operationId: 'op-3' })
    const apiClient = createMockApiClient({ listRules, submitRuleOperation })
    mockPollRuleOperationStatus()
    const user = userEvent.setup()

    render(<RuleManagementView apiClient={apiClient} />)
    const row = await screen.findByTestId(`rule-row-${customRule.ruleName}`)

    // customRule starts disabled, so the toggle button reads "启用".
    await user.click(within(row).getByRole('button', { name: '启用' }))

    await waitFor(() =>
      expect(submitRuleOperation).toHaveBeenCalledWith({
        type: 'setEnabled',
        ruleName: customRule.ruleName,
        enabled: true,
      } satisfies RuleOperationRequest),
    )
  })

  it('disables the delete button for built-in rules and enables it for custom rules; clicking a custom rule delete calls submitRuleOperation with type delete', async () => {
    const listRules = vi.fn().mockResolvedValue({ rules: [builtInRule, customRule] })
    const submitRuleOperation = vi.fn().mockResolvedValue({ operationId: 'op-4' })
    const apiClient = createMockApiClient({ listRules, submitRuleOperation })
    mockPollRuleOperationStatus()
    const user = userEvent.setup()

    render(<RuleManagementView apiClient={apiClient} />)

    const builtInRow = await screen.findByTestId(`rule-row-${builtInRule.ruleName}`)
    const customRow = screen.getByTestId(`rule-row-${customRule.ruleName}`)

    const builtInDeleteButton = within(builtInRow).getByRole('button', { name: '删除' })
    expect(builtInDeleteButton).toBeDisabled()

    const customDeleteButton = within(customRow).getByRole('button', { name: '删除' })
    expect(customDeleteButton).not.toBeDisabled()

    await user.click(customDeleteButton)

    await waitFor(() =>
      expect(submitRuleOperation).toHaveBeenCalledWith({
        type: 'delete',
        ruleName: customRule.ruleName,
      } satisfies RuleOperationRequest),
    )
  })
})
