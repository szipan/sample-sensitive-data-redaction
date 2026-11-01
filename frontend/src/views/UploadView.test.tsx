// @vitest-environment jsdom
/**
 * Component tests for `UploadView`.
 *
 * `pollJobStatus` (./../api/polling) is mocked here rather than exercised
 * for real: the polling module already has its own thorough test suite
 * (polling.test.ts) covering interval timing, error handling, etc. These
 * tests only need to verify that `UploadView` (a) starts polling with the
 * right `jobId`-bound fetch function after a successful upload, (b) renders
 * whatever status `onUpdate` delivers via `getJobStatusDisplay`, and (c)
 * calls `stop()` on unmount. Mocking the poll handle keeps these tests fast
 * and avoids the fake-timers/`@testing-library` `waitFor` interaction
 * pitfalls that come with driving the real 2s interval.
 *
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ApiClient } from '../api/client'
import { ApiError } from '../api/types'
import type { PollCallbacks, PollHandle } from '../api/polling'
import * as polling from '../api/polling'
import { SUPPORTED_EXTENSIONS, UploadView } from './UploadView'

function createMockApiClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    baseUrl: 'https://api.example.com',
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

/** Simulates the user picking `file` in the file input. */
function selectFile(input: HTMLInputElement, file: File): void {
  Object.defineProperty(input, 'files', {
    value: [file],
    configurable: true,
  })
  fireEvent.change(input)
}

describe('UploadView', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('shows every supported extension in the input accept attribute', () => {
    const apiClient = createMockApiClient()
    render(<UploadView apiClient={apiClient} />)

    const input = screen.getByLabelText(/选择要上传的文档/i) as HTMLInputElement
    for (const ext of SUPPORTED_EXTENSIONS) {
      expect(input.accept).toContain(ext)
    }
  })

  it('rejects an unsupported file: shows the rejection message and does not call uploadDocument', async () => {
    const uploadDocument = vi.fn()
    const apiClient = createMockApiClient({ uploadDocument })
    render(<UploadView apiClient={apiClient} />)

    const input = screen.getByLabelText(/选择要上传的文档/i) as HTMLInputElement
    const unsupportedFile = new File(['plain text'], 'notes.txt', { type: 'text/plain' })

    selectFile(input, unsupportedFile)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('.docx')
    expect(alert.textContent).toContain('.pdf')
    expect(uploadDocument).not.toHaveBeenCalled()
  })

  it('uploads a supported file, then displays the jobId and the processing status from pollJobStatus', async () => {
    const uploadDocument = vi
      .fn()
      .mockResolvedValue({ jobId: 'job-123', status: 'processing', fileName: 'report.docx' })
    const getJobStatus = vi.fn()
    const apiClient = createMockApiClient({ uploadDocument, getJobStatus })

    let capturedFetchFn: (() => Promise<{ status: 'processing' | 'completed' | 'failed' }>) | undefined
    let capturedCallbacks: PollCallbacks<{ status: 'processing' | 'completed' | 'failed' }> | undefined
    const fakeHandle: PollHandle = { start: vi.fn(), stop: vi.fn(), isStopped: false }
    vi.spyOn(polling, 'pollJobStatus').mockImplementation((fetchFn, callbacks) => {
      capturedFetchFn = fetchFn as typeof capturedFetchFn
      capturedCallbacks = callbacks as typeof capturedCallbacks
      return fakeHandle
    })

    render(<UploadView apiClient={apiClient} />)

    const input = screen.getByLabelText(/选择要上传的文档/i) as HTMLInputElement
    const supportedFile = new File(['docx bytes'], 'report.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    })

    selectFile(input, supportedFile)

    await waitFor(() => expect(uploadDocument).toHaveBeenCalledWith(supportedFile, 'llm', 'llm', 'review'))
    await waitFor(() => expect(screen.getByText(/job-123/)).toBeInTheDocument())

    // pollJobStatus must have been started for the jobId returned by upload.
    expect(polling.pollJobStatus).toHaveBeenCalledTimes(1)
    expect(fakeHandle.start).toHaveBeenCalledTimes(1)
    expect(capturedFetchFn).toBeDefined()
    expect(capturedCallbacks).toBeDefined()

    // The fetch function handed to pollJobStatus must call getJobStatus
    // bound to this job's id.
    await capturedFetchFn!()
    expect(getJobStatus).toHaveBeenCalledWith('job-123')

    // Simulate an intermediate "processing" update delivered by the poller.
    act(() => {
      capturedCallbacks!.onUpdate({ status: 'processing' })
    })
    expect(screen.getByText(/处理中/)).toBeInTheDocument()

    // Simulate the poller reaching the terminal "completed" status.
    act(() => {
      capturedCallbacks!.onUpdate({ status: 'completed' })
    })
    expect(screen.getByText(/已完成/)).toBeInTheDocument()
  })

  it('passes the selected ADDRESS and NAME detection sources to uploadDocument', async () => {
    const uploadDocument = vi
      .fn()
      .mockResolvedValue({ jobId: 'job-9', status: 'processing', fileName: 'report.pdf' })
    const apiClient = createMockApiClient({ uploadDocument })
    vi.spyOn(polling, 'pollJobStatus').mockReturnValue({
      start: vi.fn(),
      stop: vi.fn(),
      isStopped: false,
    })

    render(<UploadView apiClient={apiClient} />)

    // Pick a non-default source for each switch, then upload.
    const addressSelect = screen.getByLabelText(/地址的识别来源/i) as HTMLSelectElement
    fireEvent.change(addressSelect, { target: { value: 'llm' } })
    const nameSelect = screen.getByLabelText(/姓名的识别来源/i) as HTMLSelectElement
    fireEvent.change(nameSelect, { target: { value: 'guardrails' } })

    const input = screen.getByLabelText(/选择要上传的文档/i) as HTMLInputElement
    const supportedFile = new File(['pdf bytes'], 'report.pdf', { type: 'application/pdf' })
    selectFile(input, supportedFile)

    await waitFor(() =>
      expect(uploadDocument).toHaveBeenCalledWith(supportedFile, 'llm', 'guardrails', 'review'),
    )
  })

  it('shows a failure message when uploadDocument rejects, without starting polling', async () => {
    const uploadDocument = vi.fn().mockRejectedValue(new ApiError('boom', 500, 'INTERNAL'))
    const apiClient = createMockApiClient({ uploadDocument })
    const pollJobStatusSpy = vi.spyOn(polling, 'pollJobStatus')

    render(<UploadView apiClient={apiClient} />)

    const input = screen.getByLabelText(/选择要上传的文档/i) as HTMLInputElement
    const supportedFile = new File(['docx bytes'], 'report.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    })

    selectFile(input, supportedFile)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('boom')
    expect(pollJobStatusSpy).not.toHaveBeenCalled()
  })

  it('calls stop() on the poll handle when the component unmounts', async () => {
    const uploadDocument = vi
      .fn()
      .mockResolvedValue({ jobId: 'job-789', status: 'processing', fileName: 'report.docx' })
    const apiClient = createMockApiClient({ uploadDocument })

    const fakeHandle: PollHandle = { start: vi.fn(), stop: vi.fn(), isStopped: false }
    vi.spyOn(polling, 'pollJobStatus').mockReturnValue(fakeHandle)

    const { unmount } = render(<UploadView apiClient={apiClient} />)

    const input = screen.getByLabelText(/选择要上传的文档/i) as HTMLInputElement
    const supportedFile = new File(['docx bytes'], 'report.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    })

    selectFile(input, supportedFile)
    await waitFor(() => expect(fakeHandle.start).toHaveBeenCalledTimes(1))

    expect(fakeHandle.stop).not.toHaveBeenCalled()
    unmount()
    expect(fakeHandle.stop).toHaveBeenCalledTimes(1)
  })
})
