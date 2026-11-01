/**
 * Upload view.
 *
 * Lets the user pick a file, pre-checks its extension in the frontend (only
 * as a UX convenience: the backend still enforces it), and after the upload
 * shows the Job reference and status.
 *
 * Behaviour:
 *   - On selecting a Supported Document, upload it and display the created
 *     Redaction Job's reference.
 *   - On selecting an unsupported file, show the list of supported
 *     extensions and take no action to create a Redaction Job. This check
 *     is a frontend-only UX convenience — the backend's `UploadHandler`
 *     is the real enforcement boundary; this component's
 *     validation never substitutes for it.
 *   - While the created Job has `processing` status, display that
 *     status to the user (driven by short-polling `getJobStatus`).
 */
import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import type { ApiClient } from '../api/client'
import type {
  AddressDetectionSource,
  JobMode,
  JobStatus,
  JobStatusResponse,
} from '../api/types'
import { ApiError } from '../api/types'
import { pollJobStatus, pollUntilTerminal } from '../api/polling'
import type { PollHandle } from '../api/polling'
import { getJobStatusDisplay } from '../status/jobStatusDisplay'
import { translateJobError, useI18n } from '../i18n'
import type { MessageKey } from '../i18n'
import { DetectionReviewView } from './DetectionReviewView'

/**
 * Supported Document file extensions (requirements.md Glossary >
 * "Supported Document": `.docx` Word documents or text-extractable `.pdf`
 * files).
 */
export const SUPPORTED_EXTENSIONS = ['.docx', '.pdf']

function getExtension(fileName: string): string {
  const lastDot = fileName.lastIndexOf('.')
  return lastDot === -1 ? '' : fileName.slice(lastDot).toLowerCase()
}

function isSupportedFile(file: File): boolean {
  return SUPPORTED_EXTENSIONS.includes(getExtension(file.name))
}

/**
 * Selectable job mode options: fully automatic redaction (default) or
 * detection-review-then-redact (detection-review-mode feature).
 */
export const JOB_MODE_OPTIONS: ReadonlyArray<{
  value: JobMode
  labelKey: MessageKey
}> = [
  { value: 'auto', labelKey: 'mode.auto' },
  { value: 'review', labelKey: 'mode.review' },
]

/**
 * Selectable ADDRESS detection-source policies shown in the upload view
 * (detection-source-switch feature). The label text explains the tradeoff
 * so the Demonstration User can pick per document.
 */
export const ADDRESS_DETECTION_SOURCE_OPTIONS: ReadonlyArray<{
  value: AddressDetectionSource
  labelKey: MessageKey
}> = [
  { value: 'llm', labelKey: 'source.llm' },
  { value: 'both', labelKey: 'source.both' },
  { value: 'guardrails', labelKey: 'source.guardrails' },
]

/**
 * Selectable NAME detection-source policies (detection-source-switch
 * feature). Guardrails can over-trigger NAME on short CJK words (e.g.
 * the word for "photo" on an ID card), so `'llm'` lets the user drop those.
 */
export const NAME_DETECTION_SOURCE_OPTIONS: ReadonlyArray<{
  value: AddressDetectionSource
  labelKey: MessageKey
}> = [
  { value: 'llm', labelKey: 'source.llm' },
  { value: 'both', labelKey: 'source.both' },
  { value: 'guardrails', labelKey: 'source.guardrails' },
]

/**
 * 30-minute countdown timer displayed when job is in 'detected' status.
 */
function CountdownTimer({ startedAt }: { startedAt: number }) {
  const [remaining, setRemaining] = useState(() => {
    const elapsed = Math.floor((Date.now() - startedAt) / 1000)
    return Math.max(0, 30 * 60 - elapsed)
  })

  useEffect(() => {
    const timer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - startedAt) / 1000)
      const left = Math.max(0, 30 * 60 - elapsed)
      setRemaining(left)
      if (left <= 0) clearInterval(timer)
    }, 1000)
    return () => clearInterval(timer)
  }, [startedAt])

  const minutes = Math.floor(remaining / 60)
  const seconds = remaining % 60
  const timeStr = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  const urgent = remaining < 5 * 60

  return (
    <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600, color: urgent ? 'var(--color-danger-fg)' : 'var(--color-warning-fg)', marginLeft: '8px' }}>
      {timeStr}
    </span>
  )
}


export interface UploadViewProps {
  /** API client used to upload the document and poll its Job status. */
  apiClient: ApiClient
}

/** Local view state for the single in-flight/most-recent upload attempt. */
type UploadOutcome =
  | { kind: 'idle' }
  | { kind: 'uploading' }
  | {
      kind: 'jobCreated'
      jobId: string
      status: JobStatus
      fileName: string
      /** Why the Job failed; only set for `failed` Jobs. */
      failure?: JobFailure
    }
  // `message` is server-provided text; `messageKey` is one of our own
  // messages, translated at render time so a language switch applies at once.
  | { kind: 'uploadFailed'; message?: string; messageKey?: MessageKey }

/** The failure reason reported with a `failed` Job (see `JobStatusResponse`). */
interface JobFailure {
  code: string | null
  params: Record<string, string | number> | null
  /** Backend diagnostic text; only a last resort when the code is unknown. */
  message: string | null
}

/**
 * The failure info of a polled status, or `undefined` unless it is `failed`
 * and actually carries a reason (tests and older backends return only a
 * status).
 */
function failureOf(result: Partial<JobStatusResponse> & { status: JobStatus }): JobFailure | undefined {
  if (result.status !== 'failed') return undefined
  const code = result.errorCode ?? null
  const message = result.errorMessage ?? null
  if (!code && !message) return undefined
  return { code, params: result.errorParams ?? null, message }
}

function statusMessageKey(status: JobStatus): MessageKey {
  const display = getJobStatusDisplay(status)
  if (display.indicator === 'processing') {
    return 'status.processing'
  }
  if (display.indicator === 'detected') {
    return 'status.detected'
  }
  if (display.indicator === 'completed') {
    return 'status.completed'
  }
  return 'status.failed'
}

/**
 * Lets the Anonymous Demonstration User pick a `.docx`/`.pdf` file, uploads
 * it, then displays the created Redaction Job's reference and polls its
 * status until a terminal result is reached.
 */
export function UploadView({ apiClient }: UploadViewProps) {
  const { t } = useI18n()
  const [unsupportedFile, setUnsupportedFile] = useState(false)
  const [outcome, setOutcome] = useState<UploadOutcome>({ kind: 'idle' })
  const [selectedFileName, setSelectedFileName] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [addressDetectionSource, setAddressDetectionSource] =
    useState<AddressDetectionSource>('llm')
  const [nameDetectionSource, setNameDetectionSource] =
    useState<AddressDetectionSource>('llm')
  const [mode, setMode] = useState<JobMode>('review')
  const pollHandleRef = useRef<PollHandle | null>(null)
  const detectedAtRef = useRef<number>(0)
  const uploadStartedAtRef = useRef<number>(0)

  /** Processing timeout: 20 minutes. If job stays in 'processing' longer,
   * consider it a failed/orphan job and show timeout message. */
  const PROCESSING_TIMEOUT_MS = 20 * 60 * 1000

  // Stop any in-flight polling when the view unmounts, so no fetch/timer
  // outlives the component.
  useEffect(() => {
    return () => {
      pollHandleRef.current?.stop()
    }
  }, [])

  function startPolling(jobId: string, fileName: string): void {
    const handle = pollJobStatus(() => apiClient.getJobStatus(jobId), {
      onUpdate: (result) => {
        if (result.status === 'detected' && detectedAtRef.current === 0) {
          detectedAtRef.current = Date.now()
        }
        // Check processing timeout
        if (result.status === 'processing' && uploadStartedAtRef.current > 0) {
          if (Date.now() - uploadStartedAtRef.current > PROCESSING_TIMEOUT_MS) {
            pollHandleRef.current?.stop()
            setOutcome({ kind: 'uploadFailed', messageKey: 'upload.timeout' })
            return
          }
        }
        setOutcome({
          kind: 'jobCreated',
          jobId,
          status: result.status,
          fileName,
          failure: failureOf(result),
        })
      },
    })
    pollHandleRef.current = handle
    handle.start()
  }

  /**
   * Start polling that only stops on completed/failed (not on 'detected').
   * Used after the user confirms redaction selections — the job transitions
   * from 'detected' through a brief 'processing'-like phase to 'completed'.
   */
  function startPollingUntilDone(jobId: string, fileName: string): void {
    pollHandleRef.current?.stop()
    const handle = pollUntilTerminal(
      () => apiClient.getJobStatus(jobId),
      (result) => result.status === 'completed' || result.status === 'failed',
      {
        onUpdate: (result) => {
          setOutcome({
            kind: 'jobCreated',
            jobId,
            status: result.status,
            fileName,
            failure: failureOf(result),
          })
        },
      },
    )
    pollHandleRef.current = handle
    handle.start()
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = event.target.files?.[0]
    // Reset the input so re-selecting the same (e.g. previously rejected)
    // file still fires a change event.
    event.target.value = ''
    if (!file) {
      return
    }

    if (!isSupportedFile(file)) {
      setUnsupportedFile(true)
      return
    }
    setUnsupportedFile(false)
    setSelectedFileName(file.name)

    // A new upload supersedes any status polling from a previous one.
    pollHandleRef.current?.stop()
    setOutcome({ kind: 'uploading' })
    uploadStartedAtRef.current = Date.now()

    try {
      const response = await apiClient.uploadDocument(
        file,
        addressDetectionSource,
        nameDetectionSource,
        mode,
      )
      setOutcome({
        kind: 'jobCreated',
        jobId: response.jobId,
        status: response.status,
        fileName: response.fileName,
      })
      startPolling(response.jobId, response.fileName)
    } catch (error) {
      setOutcome(
        error instanceof ApiError
          ? { kind: 'uploadFailed', message: error.message }
          : { kind: 'uploadFailed', messageKey: 'upload.failed' },
      )
    }
  }

  return (
    <section aria-labelledby="upload-view-heading">
      <h2 id="upload-view-heading">{t('upload.title')}</h2>

      <div style={{ textAlign: 'left' }}>
        <div style={{ marginBottom: '16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
            <label htmlFor="job-mode-select" style={{ width: '200px', textAlign: 'right' }}>{t('upload.mode')}</label>
            <select
              id="job-mode-select"
              value={mode}
              onChange={(event) => {
                setMode(event.target.value as JobMode)
              }}
            >
              {JOB_MODE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {t(option.labelKey)}
                </option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
            <label htmlFor="address-detection-source-select" style={{ width: '200px', textAlign: 'right' }}>{t('upload.addressSource')}</label>
            <select
              id="address-detection-source-select"
              value={addressDetectionSource}
              onChange={(event) => {
                setAddressDetectionSource(event.target.value as AddressDetectionSource)
              }}
            >
              {ADDRESS_DETECTION_SOURCE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {t(option.labelKey)}
                </option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
            <label htmlFor="name-detection-source-select" style={{ width: '200px', textAlign: 'right' }}>{t('upload.nameSource')}</label>
            <select
              id="name-detection-source-select"
              value={nameDetectionSource}
              onChange={(event) => {
                setNameDetectionSource(event.target.value as AddressDetectionSource)
              }}
            >
              {NAME_DETECTION_SOURCE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {t(option.labelKey)}
                </option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <label htmlFor="upload-file-input" style={{ width: '200px', textAlign: 'right', whiteSpace: 'nowrap' }}>{t('upload.chooseFile')}</label>
            {selectedFileName ? (
              <span>{selectedFileName}</span>
            ) : (
              <>
                <input
                  id="upload-file-input"
                  type="file"
                  accept={SUPPORTED_EXTENSIONS.join(',')}
                  onChange={(event) => {
                    void handleFileChange(event)
                  }}
                />
                <span style={{ fontSize: '13px', color: 'var(--color-fg-muted)', whiteSpace: 'nowrap' }}>{t('upload.supported')}</span>
              </>
            )}
          </div>
        </div>

        {unsupportedFile && (
          <p role="alert">
            {t('upload.unsupportedType', {
              extensions: SUPPORTED_EXTENSIONS.join(t('common.listSeparator')),
            })}
          </p>
        )}

        {outcome.kind === 'uploading' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
            <label style={{ width: '200px', textAlign: 'right', whiteSpace: 'nowrap' }}>{t('upload.status')}</label>
            <span style={{ color: 'var(--color-warning-fg)' }}>{t('upload.uploading')}</span>
          </div>
        )}

        {outcome.kind === 'jobCreated' && (
          <>
            {outcome.status === 'detected' && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
                <label style={{ width: '200px', textAlign: 'right', whiteSpace: 'nowrap' }}>{t('upload.status')}</label>
                <span style={{ color: 'var(--color-danger-fg)' }}>{t('status.detected')}</span>
                {detectedAtRef.current > 0 && <CountdownTimer startedAt={detectedAtRef.current} />}
              </div>
            )}
            {outcome.status !== 'detected' && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
                <label style={{ width: '200px', textAlign: 'right', whiteSpace: 'nowrap' }}>{t('upload.status')}</label>
                <span style={outcome.status === 'completed' ? { color: 'var(--color-success-fg)' } : outcome.status === 'processing' ? { color: 'var(--color-warning-fg)' } : undefined}>{t(statusMessageKey(outcome.status))}</span>
              </div>
            )}
            {outcome.status === 'failed' && outcome.failure && (
              // A code we know is shown in the user's language. An unknown
              // code (newer backend) or a Job recorded before codes existed
              // falls back to the backend's diagnostic text.
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
                <label style={{ width: '200px', textAlign: 'right', whiteSpace: 'nowrap' }}>{t('upload.failureReason')}</label>
                <span style={{ color: 'var(--color-danger-fg)' }}>
                  {translateJobError(t, outcome.failure.code, outcome.failure.params) ?? outcome.failure.message}
                </span>
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
              <label style={{ width: '200px', textAlign: 'right', whiteSpace: 'nowrap' }}>{t('upload.jobRef')}</label>
              <span>{outcome.jobId}</span>
            </div>
            {outcome.status === 'processing' && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
                <label style={{ width: '200px' }}></label>
                <button
                  type="button"
                  onClick={() => {
                    pollHandleRef.current?.stop()
                    detectedAtRef.current = 0
                    setSelectedFileName(null)
                    setOutcome({ kind: 'idle' })
                  }}
                >
                  {t('common.back')}
                </button>
              </div>
            )}
          </>
        )}

        {outcome.kind === 'jobCreated' && outcome.status === 'detected' && (
          <>
            <hr />
            <DetectionReviewView
            apiClient={apiClient}
            jobId={outcome.jobId}
            onConfirmed={() => {
            startPollingUntilDone(outcome.jobId, outcome.fileName)
            }}
                onCancel={() => {
                  pollHandleRef.current?.stop()
                  detectedAtRef.current = 0
                  setSelectedFileName(null)
                  setOutcome({ kind: 'idle' })
                }}
              />
          </>
        )}

        {outcome.kind === 'jobCreated' && outcome.status === 'completed' && (
          <div style={{ marginLeft: '212px', marginTop: '16px', display: 'flex', gap: '12px' }}>
            <button
              type="button"
              className="btn-primary"
              disabled={downloading}
              onClick={() => {
                void (async () => {
                  setDownloading(true)
                  try {
                    const blob = await apiClient.downloadJob(outcome.jobId)
                    const url = URL.createObjectURL(blob)
                    const a = document.createElement('a')
                    a.href = url
                    a.download = `redacted_${outcome.fileName}`
                    document.body.appendChild(a)
                    a.click()
                    a.remove()
                    URL.revokeObjectURL(url)
                  } catch {
                    // Best-effort; the user can retry.
                  } finally {
                    setDownloading(false)
                  }
                })()
              }}
            >
              {downloading ? t('upload.downloading') : t('upload.download')}
            </button>
            <button
              type="button"
              onClick={() => {
                pollHandleRef.current?.stop()
                detectedAtRef.current = 0
                setSelectedFileName(null)
                setOutcome({ kind: 'idle' })
              }}
            >
              {t('common.back')}
            </button>
          </div>
        )}

        {outcome.kind === 'uploadFailed' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginTop: '8px' }}>
            <label style={{ width: '200px', textAlign: 'right', whiteSpace: 'nowrap' }}>{t('upload.status')}</label>
            <span role="alert" style={{ color: 'var(--color-danger-fg)' }}>{outcome.messageKey ? t(outcome.messageKey) : outcome.message}</span>
            <button
              type="button"
              onClick={() => {
                pollHandleRef.current?.stop()
                detectedAtRef.current = 0
                setSelectedFileName(null)
                setOutcome({ kind: 'idle' })
              }}
            >
              {t('common.back')}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
