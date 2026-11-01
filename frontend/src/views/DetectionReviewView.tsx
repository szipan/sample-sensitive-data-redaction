/**
 * Detection Review View.
 *
 * Displayed when a review-mode job reaches the "detected" status.
 * Fetches detection results, shows them in a table (sensitive data /
 * context / occurrences), and lets the user select which values to redact
 * before confirming.
 *
 * Behaviour:
 *   - Display detected PII values (entity type shown in Debug mode).
 *   - All checkboxes default to checked.
 *   - Show matched value, context snippet, page numbers, count.
 *   - "Select all" / "Select none" quick-toggle buttons.
 *   - The confirm button submits the confirmation.
 *   - Only checked IDs are sent in the POST /redact request.
 *   - Handle loading and error states.
 */
import { useEffect, useState } from 'react'
import type { ApiClient } from '../api/client'
import type { DetectionResults } from '../api/types'
import { ApiError } from '../api/types'
import { useI18n } from '../i18n'

export interface DetectionReviewViewProps {
  /** API client used to fetch detections and submit redaction. */
  apiClient: ApiClient
  /** The job ID whose detections to review. */
  jobId: string
  /** Called after successful POST /redact so parent resumes polling. */
  onConfirmed: () => void
  /** Called when user wants to go back to the upload page. */
  onCancel: () => void
}

type ViewState =
  | { kind: 'loading' }
  // `message` is server-provided text, or null to show our own default.
  | { kind: 'error'; message: string | null }
  | { kind: 'loaded'; results: DetectionResults }

/**
 * Renders a context snippet with the detected value highlighted. The backend
 * wraps the value in 「」 (`「value」`); the brackets are replaced by a
 * highlight. If the snippet does not contain that exact marker, it is shown
 * unchanged.
 */
function renderContext(snippet: string, value: string) {
  const marker = `\u300c${value}\u300d`
  const at = snippet.indexOf(marker)
  if (at === -1) return snippet
  return (
    <>
      {snippet.slice(0, at)}
      <mark>{value}</mark>
      {snippet.slice(at + marker.length)}
    </>
  )
}

export function DetectionReviewView({ apiClient, jobId, onConfirmed, onCancel }: DetectionReviewViewProps) {
  const { t } = useI18n()
  const [viewState, setViewState] = useState<ViewState>({ kind: 'loading' })
  const [checkedIds, setCheckedIds] = useState<Set<number>>(new Set())
  const [submitting, setSubmitting] = useState(false)
  // `message: null` means the generic (translated) submit-failure text.
  const [submitError, setSubmitError] = useState<{ message: string | null } | null>(null)
  const [debugMode, setDebugMode] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function fetchDetections(): Promise<void> {
      try {
        const results = await apiClient.getDetections(jobId)
        if (cancelled) return
        setViewState({ kind: 'loaded', results })
        // Default all checkboxes to checked
        setCheckedIds(new Set(results.values.map((v) => v.id)))
      } catch (error) {
        if (cancelled) return
        setViewState({ kind: 'error', message: error instanceof ApiError ? error.message : null })
      }
    }

    void fetchDetections()

    return () => {
      cancelled = true
    }
  }, [apiClient, jobId])

  function handleToggle(id: number): void {
    setCheckedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  function handleSelectAll(): void {
    if (viewState.kind !== 'loaded') return
    setCheckedIds(new Set(viewState.results.values.map((v) => v.id)))
  }

  function handleSelectNone(): void {
    setCheckedIds(new Set())
  }

  async function handleConfirm(): Promise<void> {
    setSubmitting(true)
    setSubmitError(null)
    try {
      await apiClient.submitRedaction(jobId, Array.from(checkedIds))
      onConfirmed()
    } catch (error) {
      setSubmitError({ message: error instanceof ApiError ? error.message : null })
    } finally {
      setSubmitting(false)
    }
  }

  if (viewState.kind === 'loading') {
    return <p>{t('review.loading')}</p>
  }

  if (viewState.kind === 'error') {
    return (
      <p role="alert">
        {t('review.loadFailed', { message: viewState.message ?? t('review.loadFailedDefault') })}
      </p>
    )
  }

  const { results } = viewState
  const allChecked = results.values.length > 0 && checkedIds.size === results.values.length

  return (
    <section aria-labelledby="detection-review-heading" style={{ textAlign: 'left' }}>
      <h3 id="detection-review-heading">{t('review.title')}</h3>

      <div className="review-actions">
        <button type="button" onClick={handleSelectAll}>
          {t('review.selectAll')}
        </button>
        <button type="button" onClick={handleSelectNone}>
          {t('review.selectNone')}
        </button>
        <label className="review-debug">
          <input
            type="checkbox"
            checked={debugMode}
            onChange={() => setDebugMode(!debugMode)}
          />
          {t('review.debug')}
        </label>
      </div>

      <table className="data-table review-table">
        <thead>
          <tr>
            <th className="col-check">
              <input
                type="checkbox"
                aria-label={t('review.toggleAll')}
                checked={allChecked}
                ref={(el) => {
                  if (el) el.indeterminate = !allChecked && checkedIds.size > 0
                }}
                onChange={() => (allChecked ? handleSelectNone() : handleSelectAll())}
              />
            </th>
            <th className="col-value">{t('review.col.value')}</th>
            {debugMode && <th className="col-type">{t('review.col.type')}</th>}
            <th>{t('review.col.context')}</th>
            <th className="col-count">{t('review.col.count')}</th>
          </tr>
        </thead>
        <tbody>
          {results.values.map((detection) => {
            const checked = checkedIds.has(detection.id)
            return (
              <tr
                key={detection.id}
                className={checked ? undefined : 'is-unchecked'}
                onClick={() => handleToggle(detection.id)}
              >
                <td className="col-check">
                  <input
                    type="checkbox"
                    aria-label={t('review.selectRow', { value: detection.value })}
                    checked={checked}
                    onChange={() => {
                      /* toggled by the row's onClick (the click bubbles up) */
                    }}
                  />
                </td>
                <td className="col-value">
                  <span className="detection-value">{detection.value}</span>
                </td>
                {debugMode && (
                  <td className="col-type">
                    <span className="detection-type">{detection.entityType}</span>
                  </td>
                )}
                <td>
                  <span className="detection-context">
                    {renderContext(detection.contextSnippet, detection.value)}
                  </span>
                </td>
                <td className="col-count">
                  <span className="detection-count">
                    {t(detection.count === 1 ? 'review.count.one' : 'review.count.other', {
                      count: detection.count,
                    })}
                  </span>
                  {detection.pages.length > 0 && (
                    <span className="detection-pages">
                      {t(detection.pages.length === 1 ? 'review.pages.one' : 'review.pages.other', {
                        pages: detection.pages.join(', '),
                      })}
                    </span>
                  )}
                </td>
              </tr>
            )
          })}
          {results.values.length === 0 && (
            <tr>
              <td colSpan={debugMode ? 5 : 4} style={{ textAlign: 'center', color: 'var(--color-fg-muted)' }}>
                {t('review.empty')}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {submitError && (
        <p role="alert">{submitError.message ?? t('review.submitFailedDefault')}</p>
      )}

      <div className="review-footer">
        <button
          type="button"
          className="btn-primary"
          onClick={() => { void handleConfirm() }}
          disabled={submitting}
        >
          {submitting ? t('review.submitting') : t('review.confirm')}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
        >
          {t('common.back')}
        </button>
      </div>
    </section>
  )
}
