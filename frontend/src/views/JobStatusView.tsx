/**
 * Job status and download view.
 *
 * Shows the `processing` / `completed` / `failed` status, and the download
 * button only when the status is `completed`.
 *
 * The displayed status and the download visibility are a pure function of
 * the job status: `processing` shows the processing indicator and hides the
 * download action; `completed` shows the download action; any status other
 * than `completed` hides the download action; `failed` additionally displays
 * a failure indication.
 *
 * The detection-count line is rendered if and only if the status is
 * `completed` and a Detection Summary is present; when it is absent the
 * status indicator and download-action visibility are identical to the
 * summary-less rendering. When present, the total plus the per-path counts
 * are displayed.
 *
 * Design choice — presentational, not self-polling:
 * -------------------------------------------------------------------------
 * This component intentionally takes `status: JobStatus` as a prop rather
 * than owning its own `pollJobStatus` call. Rationale:
 *   - It keeps this component a pure function of its props, which makes it
 *     trivial to unit test every status/visibility combination
 *     without faking timers or the polling loop.
 *   - It stays reusable: a parent view (e.g. the Upload view)
 *     is the natural owner of "when did we start watching this jobId" and
 *     already needs the job status for its own purposes (e.g. deciding
 *     whether to allow a new upload), so it is the more natural place to
 *     own the `pollJobStatus` handle and its start/stop lifecycle.
 *   - It avoids two independent polling loops (one here, one in a parent)
 *     ever coexisting for the same `jobId`.
 * A parent component wires this up by calling `pollJobStatus` itself
 * (starting on mount, calling `handle.stop()` on unmount/cleanup) and
 * passing the latest observed `status` down as a prop.
 *
 * Download behavior:
 * -------------------------------------------------------------------------
 * Clicking the download button calls `client.downloadJob(jobId)` and, on
 * success, triggers a browser file download by creating a temporary
 * Object URL for the returned `Blob` and simulating a click on a hidden
 * `<a download>` anchor, then revoking the Object URL.
 *
 * If the download call itself fails (e.g. the backend rejects it because
 * the Redaction Job is no longer `completed` at the time of the request —
 * enforced server-side by the download handler), this
 * component does not crash: it catches the error and shows an inline
 * "unavailable" message instead.
 */

import { useState } from 'react'
import type { ApiClient } from '../api/client'
import type { DetectionSummary, JobStatus } from '../api/types'
import { getJobStatusDisplay } from '../status/jobStatusDisplay'
import { useI18n } from '../i18n'
import type { MessageKey, TranslateFn } from '../i18n'

export interface JobStatusViewProps {
  /** The Redaction Job's identifier, used to request the download. */
  jobId: string
  /** The Redaction Job's current status, as last observed by the caller. */
  status: JobStatus
  /** API client used to issue the download request. */
  client: ApiClient
  /**
   * The Redaction Job's Detection Summary, when the backend recorded one.
   *
   * `null`/absent for `failed` Jobs and Jobs recorded before detection
   * summaries existed. When absent this component renders exactly as it did
   * before this prop existed — no panel,
   * no zeroed counts.
   *
   * When present, the summary always carries the LLM counts (`llmSpans`,
   * `llmOnlySpans`); the dual-path keys (`textLayerRegions`, `ocrRegions`,
   * `agreedRegions`, `pageHealth`) are present only for `.pdf` Jobs and
   * omitted for `.docx`, so the per-path line renders only when they exist.
   */
  detectionSummary?: DetectionSummary | null
}

/** User-facing text for each `JobStatus` value's status indicator. */
const STATUS_INDICATOR_KEYS: Record<JobStatus, MessageKey> = {
  processing: 'job.status.processing',
  detected: 'job.status.detected',
  completed: 'job.status.completed',
  failed: 'job.status.failed',
}

/**
 * Triggers a browser download of `blob` named `fileName` by creating a
 * temporary Object URL and simulating a click on a hidden `<a download>`
 * anchor, then revoking the Object URL.
 *
 * Exported for testability (spying on `URL.createObjectURL` /
 * `HTMLAnchorElement.prototype.click`).
 */
export function triggerBrowserDownload(blob: Blob, fileName: string): void {
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = fileName
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(objectUrl)
}

/**
 * True when a Detection Summary carries the dual-path (PDF-only) counts.
 *
 * The dual-path keys (`textLayerRegions`, `ocrRegions`, `agreedRegions`,
 * `pageHealth`) are omitted — not zeroed — for `.docx` Jobs, which have no
 * text-layer-vs-OCR distinction. The
 * per-path line must therefore render only when those keys are actually
 * present, so a `.docx` summary never prints `undefined`/`NaN`.
 *
 * Per-path counts are rendered only for the keys the
 * summary actually carries.
 */
function hasDualPathCounts(
  summary: DetectionSummary,
): summary is DetectionSummary & { textLayerRegions: number; ocrRegions: number } {
  return summary.textLayerRegions != null && summary.ocrRegions != null
}

/**
 * Builds the dual-path (PDF-only) detection-count line.
 *
 * The displayed total is `textLayerRegions + ocrRegions` (design.md
 * Component 8). `pageHealth` is deliberately not rendered and not read: it is
 * a diagnostic the backend writes and nothing routes on.
 * Callers must gate this on {@link hasDualPathCounts}.
 */
function formatDualPathCounts(
  t: TranslateFn,
  textLayerRegions: number,
  ocrRegions: number,
  agreedRegions: number,
): string {
  return t('job.dualPath', {
    total: textLayerRegions + ocrRegions,
    textLayer: textLayerRegions,
    ocr: ocrRegions,
    agreed: agreedRegions,
  })
}

/**
 * Builds the LLM-only detection-count line.
 *
 * `llmOnlySpans` is the number of merged regions only the LLM detection
 * source found — the count a reviewer uses to judge whether the model
 * contributed recall or just painted extra black boxes. It is always present
 * on a Detection Summary, for both
 * `.pdf` and `.docx` Jobs.
 */
function formatLlmOnlyCount(t: TranslateFn, llmOnlySpans: number): string {
  return t('job.llmOnly', { count: llmOnlySpans })
}

/**
 * Renders the processing indicator, completed/download action, or failure
 * indication for a single Redaction Job, per the rules above, plus
 * the detection-count line when — and only when — the status is `completed`
 * and a Detection Summary is present.
 */
export function JobStatusView({ jobId, status, client, detectionSummary }: JobStatusViewProps) {
  const { t } = useI18n()
  const display = getJobStatusDisplay(status)
  const [downloadFailed, setDownloadFailed] = useState(false)
  const [isDownloading, setIsDownloading] = useState(false)

  async function handleDownloadClick(): Promise<void> {
    setDownloadFailed(false)
    setIsDownloading(true)
    try {
      const blob = await client.downloadJob(jobId)
      triggerBrowserDownload(blob, `${jobId}-redacted`)
    } catch {
      // The download itself was rejected
      // (e.g. the job is no longer `completed`, a 409 race).
      // Surface an inline message instead of throwing/crashing.
      setDownloadFailed(true)
    } finally {
      setIsDownloading(false)
    }
  }

  return (
    <div data-testid="job-status-view">
      <p data-testid="status-indicator">{t(STATUS_INDICATOR_KEYS[display.indicator])}</p>

      {display.showFailure && (
        <p data-testid="failure-indication" role="alert">
          {t('job.failure')}
        </p>
      )}

      {status === 'completed' && detectionSummary != null && (
        <div data-testid="detection-summary" role="status">
          {hasDualPathCounts(detectionSummary) && (
            <p data-testid="dual-path-counts">
              {formatDualPathCounts(
                t,
                detectionSummary.textLayerRegions,
                detectionSummary.ocrRegions,
                detectionSummary.agreedRegions ?? 0,
              )}
            </p>
          )}
          <p data-testid="llm-only-count">{formatLlmOnlyCount(t, detectionSummary.llmOnlySpans)}</p>
        </div>
      )}

      {display.downloadVisible && (
        <button type="button" onClick={() => void handleDownloadClick()} disabled={isDownloading}>
          {t('job.download')}
        </button>
      )}

      {downloadFailed && (
        <p data-testid="download-error" role="alert">
          {t('job.downloadUnavailable')}
        </p>
      )}
    </div>
  )
}

export default JobStatusView
