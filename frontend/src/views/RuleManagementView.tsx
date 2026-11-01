/**
 * Rule management view.
 *
 * Shows the rule list (name, pattern, enabled state, built-in or not), the
 * form for adding a rule, the enable/disable toggle and the delete button
 * (disabled for Built-in Rules).
 *
 * Behaviour:
 *   - Provides an interface to submit a new Redaction Rule (name +
 *     pattern).
 *   - A rejected create submission with a duplicate name or an invalid
 *     pattern identifies that specifically (not a generic error).
 *   - A rejected enable/disable/delete request for an unrecognized
 *     Redaction Rule name identifies that specifically.
 *   - A delete request for a Built-in Rule is rejected and the UI both
 *     prevents it (disabled button) and would identify the specific
 *     rejection reason if it were ever submitted anyway (e.g. a race with
 *     another client).
 *   - Displays the current Redaction Rule collection, including each
 *     Redaction Rule's name, pattern, and enabled state (plus whether it
 *     is a Built-in Rule).
 *
 * Design choices, consistent with `UploadView`/`JobStatusView`:
 *   - This component owns its own `pollRuleOperationStatus` handles (one
 *     per in-flight Rule Operation: the create form, or a single row's
 *     enable/disable/delete). All active handles are tracked in a ref set
 *     and stopped on unmount so no fetch/timer outlives the component.
 *   - Rule Operation rejection reasons are mapped to specific, translated
 *     user-facing messages (never a generic "operation failed").
 *   - On `applied`, the rule collection is re-fetched via `listRules()`
 *     rather than optimistically patched locally, so the displayed
 *     collection always reflects the backend's authoritative state.
 */
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import type { ApiClient } from '../api/client'
import type { RedactionRuleDto, RuleOperationErrorReason } from '../api/types'
import { ApiError } from '../api/types'
import { pollRuleOperationStatus } from '../api/polling'
import { useI18n } from '../i18n'
import type { MessageKey, TranslateFn } from '../i18n'
import type { PollHandle } from '../api/polling'

export interface RuleManagementViewProps {
  /** API client used to list rules and submit/poll Rule Operations. */
  apiClient: ApiClient
}

/**
 * Specific, per-`errorReason` rejection messages. Never fall back to a single
 * generic "operation failed" message when
 * the backend has identified a specific reason.
 */
const RULE_OPERATION_ERROR_KEYS: Record<RuleOperationErrorReason, MessageKey> = {
  DUPLICATE_NAME: 'rules.err.DUPLICATE_NAME',
  INVALID_PATTERN: 'rules.err.INVALID_PATTERN',
  INVALID_LENGTH: 'rules.err.INVALID_LENGTH',
  UNKNOWN_RULE_NAME: 'rules.err.UNKNOWN_RULE_NAME',
  BUILT_IN_NOT_DELETABLE: 'rules.err.BUILT_IN_NOT_DELETABLE',
}

function describeRejection(
  t: TranslateFn,
  errorReason: RuleOperationErrorReason | null,
): string {
  if (errorReason && errorReason in RULE_OPERATION_ERROR_KEYS) {
    return t(RULE_OPERATION_ERROR_KEYS[errorReason])
  }
  return t('rules.err.unknown')
}

function describeSubmitFailure(t: TranslateFn, error: unknown): string {
  return error instanceof ApiError ? error.message : t('rules.err.submitFailed')
}

/** Local state for the "new rule" creation form. */
type CreateFormStatus =
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'rejected'; message: string }

/** Local state for a single row's in-flight enable/disable/delete operation. */
type RowOperationStatus =
  | { kind: 'idle' }
  | { kind: 'pending' }
  | { kind: 'rejected'; message: string }

const IDLE_ROW_STATUS: RowOperationStatus = { kind: 'idle' }

/**
 * Displays the Redaction Rule collection and lets the Anonymous
 * Demonstration User create, enable/disable, or delete Redaction Rules,
 * polling every submitted Rule Operation to its Terminal Result.
 */
export function RuleManagementView({ apiClient }: RuleManagementViewProps) {
  const { t } = useI18n()
  const [rules, setRules] = useState<RedactionRuleDto[]>([])
  const [listError, setListError] = useState<string | null>(null)

  const [newRuleName, setNewRuleName] = useState('')
  const [newRulePattern, setNewRulePattern] = useState('')
  const [createStatus, setCreateStatus] = useState<CreateFormStatus>({ kind: 'idle' })

  const [rowStatuses, setRowStatuses] = useState<Record<string, RowOperationStatus>>({})

  // Every currently-active poll handle (create form + any row operations),
  // so all of them can be stopped on unmount.
  const activeHandlesRef = useRef<Set<PollHandle>>(new Set())

  useEffect(() => {
    void refreshRules()
    return () => {
      for (const handle of activeHandlesRef.current) {
        handle.stop()
      }
      activeHandlesRef.current.clear()
    }
  }, [])

  async function refreshRules(): Promise<void> {
    try {
      const response = await apiClient.listRules()
      setRules(response.rules)
      setListError(null)
    } catch (error) {
      setListError(describeSubmitFailure(t, error))
    }
  }

  function setRowStatus(ruleName: string, status: RowOperationStatus): void {
    setRowStatuses((prev) => ({ ...prev, [ruleName]: status }))
  }

  /**
   * Polls `operationId` to its Terminal Result and dispatches to
   * `onApplied`/`onRejected` accordingly. Tracks the poll handle so it can
   * be stopped on unmount.
   */
  function pollOperation(
    operationId: string,
    handlers: { onApplied: () => void; onRejected: (errorReason: RuleOperationErrorReason | null) => void },
  ): void {
    const handle = pollRuleOperationStatus(() => apiClient.getRuleOperationStatus(operationId), {
      onUpdate: () => {
        // Intermediate `queued`/`processing` updates need no rendering
        // beyond the "pending" state already set by the caller.
      },
      onTerminal: (result) => {
        activeHandlesRef.current.delete(handle)
        if (result.status === 'applied') {
          handlers.onApplied()
        } else {
          handlers.onRejected(result.errorReason)
        }
      },
    })
    activeHandlesRef.current.add(handle)
    handle.start()
  }

  async function handleCreateSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    const ruleName = newRuleName.trim()
    const pattern = newRulePattern.trim()
    if (ruleName.length === 0 || pattern.length === 0) {
      return
    }

    setCreateStatus({ kind: 'submitting' })
    try {
      const response = await apiClient.submitRuleOperation({ type: 'create', ruleName, pattern })
      pollOperation(response.operationId, {
        onApplied: () => {
          setCreateStatus({ kind: 'idle' })
          setNewRuleName('')
          setNewRulePattern('')
          void refreshRules()
        },
        onRejected: (errorReason) => {
          setCreateStatus({ kind: 'rejected', message: describeRejection(t, errorReason) })
        },
      })
    } catch (error) {
      setCreateStatus({ kind: 'rejected', message: describeSubmitFailure(t, error) })
    }
  }

  async function handleToggleEnabled(rule: RedactionRuleDto): Promise<void> {
    setRowStatus(rule.ruleName, { kind: 'pending' })
    try {
      const response = await apiClient.submitRuleOperation({
        type: 'setEnabled',
        ruleName: rule.ruleName,
        enabled: !rule.enabled,
      })
      pollOperation(response.operationId, {
        onApplied: () => {
          setRowStatus(rule.ruleName, IDLE_ROW_STATUS)
          void refreshRules()
        },
        onRejected: (errorReason) => {
          setRowStatus(rule.ruleName, { kind: 'rejected', message: describeRejection(t, errorReason) })
        },
      })
    } catch (error) {
      setRowStatus(rule.ruleName, { kind: 'rejected', message: describeSubmitFailure(t, error) })
    }
  }

  async function handleDelete(rule: RedactionRuleDto): Promise<void> {
    if (rule.isBuiltIn) {
      // The delete button is already disabled for
      // Built-in Rules; this is just a defensive guard against any
      // programmatic click bypassing the `disabled` attribute.
      return
    }

    setRowStatus(rule.ruleName, { kind: 'pending' })
    try {
      const response = await apiClient.submitRuleOperation({ type: 'delete', ruleName: rule.ruleName })
      pollOperation(response.operationId, {
        onApplied: () => {
          setRowStatus(rule.ruleName, IDLE_ROW_STATUS)
          void refreshRules()
        },
        onRejected: (errorReason) => {
          setRowStatus(rule.ruleName, { kind: 'rejected', message: describeRejection(t, errorReason) })
        },
      })
    } catch (error) {
      setRowStatus(rule.ruleName, { kind: 'rejected', message: describeSubmitFailure(t, error) })
    }
  }

  return (
    <section aria-labelledby="rule-management-heading">
      <h2 id="rule-management-heading">{t('rules.title')}</h2>

      {listError && <p role="alert">{listError}</p>}

      <table className="data-table">
        <thead>
          <tr>
            <th>{t('rules.col.name')}</th>
            <th>{t('rules.col.pattern')}</th>
            <th>{t('rules.col.status')}</th>
            <th>{t('rules.col.type')}</th>
            <th style={{ textAlign: 'right' }}>{t('rules.col.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {rules.map((rule) => {
            const rowStatus = rowStatuses[rule.ruleName] ?? IDLE_ROW_STATUS
            const isPending = rowStatus.kind === 'pending'
            return (
              <tr key={rule.ruleName} data-testid={`rule-row-${rule.ruleName}`}>
                <td style={{ fontWeight: 600 }}>{rule.ruleName}</td>
                <td><code style={{ fontSize: '13px' }}>{rule.pattern}</code></td>
                <td style={{ color: rule.enabled ? 'var(--color-success-fg)' : 'var(--color-fg-muted)' }}>
                  {rule.enabled ? t('rules.enabled') : t('rules.disabled')}
                </td>
                <td style={{ fontSize: '12px', color: 'var(--color-fg-subtle)' }}>
                  {rule.isBuiltIn ? t('rules.builtIn') : t('rules.custom')}
                </td>
                <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <button
                    type="button"
                    disabled={isPending}
                    onClick={() => void handleToggleEnabled(rule)}
                    style={{ marginRight: '8px' }}
                  >
                    {rule.enabled ? t('rules.disable') : t('rules.enable')}
                  </button>
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={rule.isBuiltIn || isPending}
                    onClick={() => void handleDelete(rule)}
                  >
                    {t('rules.delete')}
                  </button>
                  {rule.isBuiltIn && <span style={{ fontSize: '12px', color: 'var(--color-fg-subtle)', marginLeft: '8px' }}>{t('rules.builtInHint')}</span>}
                  {rowStatus.kind === 'rejected' && <p role="alert">{rowStatus.message}</p>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      <form onSubmit={(event) => void handleCreateSubmit(event)} style={{ marginTop: '24px', width: '50%' }}>
        <h3>{t('rules.newTitle')}</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <label htmlFor="new-rule-name">{t('rules.col.name')}</label>
          <input
            id="new-rule-name"
            type="text"
            value={newRuleName}
            onChange={(event) => setNewRuleName(event.target.value)}
          />

          <label htmlFor="new-rule-pattern">{t('rules.col.pattern')}</label>
          <input
            id="new-rule-pattern"
            type="text"
            style={{ flex: 1 }}
            value={newRulePattern}
            onChange={(event) => setNewRulePattern(event.target.value)}
          />

          <button type="submit" className="btn-primary" disabled={createStatus.kind === 'submitting'}>
            {t('rules.create')}
          </button>
        </div>

        {createStatus.kind === 'rejected' && <p role="alert">{createStatus.message}</p>}
      </form>
    </section>
  )
}

export default RuleManagementView
