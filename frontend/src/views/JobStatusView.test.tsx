// @vitest-environment jsdom
/**
 * Component tests for `JobStatusView`.
 *
 * Covers the rule that the displayed status and the download visibility are
 * a pure function of the job status, for each `JobStatus` value, plus
 * clicking the download button (calls `downloadJob` and triggers a browser
 * download) and the download-failure fallback.
 *
 * Also covers the detection-count line: rendered if and only if the
 * status is `completed` and a Detection Summary is present, showing the
 * total (`textLayerRegions + ocrRegions`) plus each per-path count.
 *
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ApiClient } from '../api/client'
import type { DetectionSummary } from '../api/types'
import { JobStatusView } from './JobStatusView'

function makeClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    baseUrl: 'https://example.test',
    uploadDocument: vi.fn(),
    getJobStatus: vi.fn(),
    downloadJob: vi.fn(),
    listRules: vi.fn(),
    submitRuleOperation: vi.fn(),
    getRuleOperationStatus: vi.fn(),
    getDetections: vi.fn(),
    submitRedaction: vi.fn(),
    ...overrides,
  }
}

/**
 * A `.pdf`-shaped Detection Summary: all dual-path keys present, plus the
 * always-present LLM counts.
 */
function makeSummary(overrides: Partial<DetectionSummary> = {}): DetectionSummary {
  return {
    textLayerRegions: 1,
    ocrRegions: 3,
    agreedRegions: 1,
    pageHealth: [{ pageIndex: 0, textLayerChars: 19, ocrChars: 23 }],
    llmSpans: 7,
    llmOnlySpans: 2,
    ...overrides,
  }
}

/**
 * A `.docx`-shaped Detection Summary: the dual-path keys are absent (a
 * `.docx` Job has no text-layer-vs-OCR distinction), only the LLM counts are
 * present.
 */
function makeDocxSummary(overrides: Partial<DetectionSummary> = {}): DetectionSummary {
  return {
    llmSpans: 5,
    llmOnlySpans: 1,
    ...overrides,
  }
}

describe('JobStatusView', () => {
  beforeEach(() => {
    // jsdom does not implement these; component tests spy on/replace them.
    if (!('createObjectURL' in URL)) {
      // @ts-expect-error jsdom polyfill for test environment
      URL.createObjectURL = () => 'blob:mock'
    }
    if (!('revokeObjectURL' in URL)) {
      // @ts-expect-error jsdom polyfill for test environment
      URL.revokeObjectURL = () => {}
    }
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-url')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('processing: shows the processing indicator and hides the download button and failure indication', () => {
    const client = makeClient()
    render(<JobStatusView jobId="job-1" status="processing" client={client} />)

    expect(screen.getByTestId('status-indicator')).toHaveTextContent('处理中')
    expect(screen.queryByRole('button', { name: /下载/ })).not.toBeInTheDocument()
    expect(screen.queryByTestId('failure-indication')).not.toBeInTheDocument()
  })

  it('completed: shows the completed indicator and the download button, hides the failure indication', () => {
    const client = makeClient()
    render(<JobStatusView jobId="job-1" status="completed" client={client} />)

    expect(screen.getByTestId('status-indicator')).toHaveTextContent('完成')
    expect(screen.getByRole('button', { name: /下载/ })).toBeInTheDocument()
    expect(screen.queryByTestId('failure-indication')).not.toBeInTheDocument()
  })

  it('failed: shows the failed indicator and failure indication, hides the download button', () => {
    const client = makeClient()
    render(<JobStatusView jobId="job-1" status="failed" client={client} />)

    expect(screen.getByTestId('status-indicator')).toHaveTextContent('失败')
    expect(screen.getByTestId('failure-indication')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /下载/ })).not.toBeInTheDocument()
  })

  it('completed: clicking the download button calls downloadJob and triggers a browser download', async () => {
    const blob = new Blob(['redacted content'], { type: 'application/octet-stream' })
    const downloadJob = vi.fn().mockResolvedValue(blob)
    const client = makeClient({ downloadJob })
    const user = userEvent.setup()

    render(<JobStatusView jobId="job-42" status="completed" client={client} />)
    await user.click(screen.getByRole('button', { name: /下载/ }))

    await waitFor(() => {
      expect(downloadJob).toHaveBeenCalledWith('job-42')
    })
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
    expect(screen.queryByTestId('download-error')).not.toBeInTheDocument()
  })

  it('completed: shows an inline unavailable message, without crashing, when the download call fails', async () => {
    const downloadJob = vi.fn().mockRejectedValue(new Error('409 job no longer completed'))
    const client = makeClient({ downloadJob })
    const user = userEvent.setup()

    render(<JobStatusView jobId="job-42" status="completed" client={client} />)
    await user.click(screen.getByRole('button', { name: /下载/ }))

    expect(await screen.findByTestId('download-error')).toBeInTheDocument()
    expect(downloadJob).toHaveBeenCalledWith('job-42')
  })

  it('completed with a .pdf summary: renders the dual-path line, the LLM-only count, and the download button', () => {
    const client = makeClient()
    render(
      <JobStatusView
        jobId="job-1"
        status="completed"
        client={client}
        detectionSummary={makeSummary()}
      />,
    )

    // Dual-path line: total = textLayerRegions + ocrRegions = 1 + 3
    const dualPath = screen.getByTestId('dual-path-counts')
    expect(dualPath).toHaveTextContent('已遮蔽 4 处敏感信息')
    expect(dualPath).toHaveTextContent('文本层 1 处')
    expect(dualPath).toHaveTextContent('OCR 3 处')
    expect(dualPath).toHaveTextContent('两路一致 1 处')
    // LLM-only count (llmOnlySpans = 2) rendered alongside the per-path counts.
    expect(screen.getByTestId('llm-only-count')).toHaveTextContent('其中仅大模型发现 2 处')
    // The whole summary is announced to assistive technology as one status region.
    expect(screen.getByRole('status')).toBe(screen.getByTestId('detection-summary'))
    expect(screen.getByRole('button', { name: /下载/ })).toBeInTheDocument()
  })

  it('completed with a .docx-shaped summary: renders the LLM-only count and no dual-path line, never printing undefined', () => {
    const client = makeClient()
    render(
      <JobStatusView
        jobId="job-1"
        status="completed"
        client={client}
        detectionSummary={makeDocxSummary()}
      />,
    )

    // The dual-path keys are absent for .docx, so that line must not render.
    expect(screen.queryByTestId('dual-path-counts')).not.toBeInTheDocument()
    // The LLM-only count is always present.
    expect(screen.getByTestId('llm-only-count')).toHaveTextContent('其中仅大模型发现 1 处')
    // Guard against the missing-key regression: no stray undefined/NaN text.
    const summaryRegion = screen.getByTestId('detection-summary')
    expect(summaryRegion).not.toHaveTextContent('undefined')
    expect(summaryRegion).not.toHaveTextContent('NaN')
    expect(screen.getByRole('button', { name: /下载/ })).toBeInTheDocument()
  })

  it('completed with a zero-count summary: still renders the line, with zeros', () => {
    const client = makeClient()
    render(
      <JobStatusView
        jobId="job-1"
        status="completed"
        client={client}
        detectionSummary={makeSummary({
          textLayerRegions: 0,
          ocrRegions: 0,
          agreedRegions: 0,
        })}
      />,
    )

    expect(screen.getByTestId('detection-summary')).toHaveTextContent('已遮蔽 0 处敏感信息')
  })

  it('completed with a null summary: renders identically to the summary-less rendering', () => {
    const client = makeClient()
    render(<JobStatusView jobId="job-1" status="completed" client={client} detectionSummary={null} />)

    expect(screen.queryByTestId('detection-summary')).not.toBeInTheDocument()
    expect(screen.getByTestId('status-indicator')).toHaveTextContent('完成')
    expect(screen.getByRole('button', { name: /下载/ })).toBeInTheDocument()
    expect(screen.queryByTestId('failure-indication')).not.toBeInTheDocument()
  })

  it('completed with the summary prop omitted: renders no detection-count line', () => {
    const client = makeClient()
    render(<JobStatusView jobId="job-1" status="completed" client={client} />)

    expect(screen.queryByTestId('detection-summary')).not.toBeInTheDocument()
  })

  it.each(['processing', 'failed'] as const)(
    '%s with a summary present: renders no detection-count line',
    (status) => {
      const client = makeClient()
      render(
        <JobStatusView jobId="job-1" status={status} client={client} detectionSummary={makeSummary()} />,
      )

      expect(screen.queryByTestId('detection-summary')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /下载/ })).not.toBeInTheDocument()
    },
  )
})
