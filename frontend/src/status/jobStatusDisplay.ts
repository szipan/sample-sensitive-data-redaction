/**
 * Pure mapping from a Redaction Job's `JobStatus` to a display descriptor.
 *
 * The displayed status and the download visibility are a pure function of
 * the job status.
 *
 * - `processing` -> shows the processing indicator, hides the download action.
 * - `completed`  -> shows the completed/download-ready indicator, shows the
 *                   download action.
 * - `failed`     -> shows the failed indicator, hides the download action,
 *                   and additionally surfaces a failure indication.
 *
 * Invariant: `downloadVisible` is `true` if and only if the status is
 * `completed`. Any other status MUST have `downloadVisible: false`.
 */

import type { JobStatus } from '../api/types'

/** Display descriptor produced from a `JobStatus` value. */
export interface JobStatusDisplay {
  /** Which status indicator to render. Mirrors `JobStatus` for display purposes. */
  indicator: JobStatus
  /** Whether the download action for the Redacted Document should be shown. */
  downloadVisible: boolean
  /** Whether a failure indication should be shown to the user. */
  showFailure: boolean
}

/**
 * Maps a Redaction Job's `status` to a `JobStatusDisplay` descriptor.
 *
 * Pure function: no side effects, no I/O, deterministic for a given input.
 *
 */
export function getJobStatusDisplay(status: JobStatus): JobStatusDisplay {
  return {
    indicator: status,
    downloadVisible: status === 'completed',
    showFailure: status === 'failed',
  }
}
