// @vitest-environment jsdom
/**
 * Property-based test for `JobStatusView`.
 *
 * The job status view renders the LLM count exactly when it applies:
 *   "For any Redaction Job status and any optional Detection_Summary —
 *   including summaries carrying no text-layer or OCR keys — the
 *   Job_Status_View renders the LLM-only region count if and only if the
 *   status is `completed` and the summary is present, renders per-path counts
 *   only for the keys the summary actually carries, and otherwise renders a
 *   status indicator and download action identical to the summary-less
 *   rendering."
 *
 * The example-based coverage lives in `JobStatusView.test.tsx`.
 * This test's distinct value is the *generation strategy*: it draws the
 * `detectionSummary` across a space that deliberately reaches shapes the
 * examples cannot enumerate exhaustively — `null`, an omitted prop, the full
 * `.pdf` shape (all dual-path keys), the `.docx` shape (no dual-path keys),
 * and every partial shape where only some dual-path keys are present. Those
 * partial/`.docx` shapes are the reason this is a property and not just the
 * examples: they exercise the `hasDualPathCounts` guard, proving it keys off
 * real per-key presence rather than a single flag, and that a summary missing
 * dual-path keys never renders `undefined`/`NaN`.
 */
import { cleanup, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import fc from 'fast-check'
import type { ApiClient } from '../api/client'
import type { DetectionSummary, JobStatus } from '../api/types'
import { JobStatusView } from './JobStatusView'

/**
 * Expected status-indicator text per status. Mirrors the component's private
 * `STATUS_INDICATOR_TEXT`; duplicated here so the test asserts the contract
 * independently rather than importing the value under test.
 */
const EXPECTED_STATUS_TEXT: Record<JobStatus, string> = {
  processing: '处理中…',
  detected: '检测完成，待审核',
  completed: '处理完成',
  failed: '处理失败',
}

function makeClient(): ApiClient {
  return {
    baseUrl: 'https://example.test',
    uploadDocument: vi.fn(),
    getJobStatus: vi.fn(),
    // Never invoked: this property never clicks the download button.
    downloadJob: vi.fn(),
    listRules: vi.fn(),
    submitRuleOperation: vi.fn(),
    getRuleOperationStatus: vi.fn(),
    getDetections: vi.fn(),
    submitRedaction: vi.fn(),
    listUsers: vi.fn(),
    createUser: vi.fn(),
    setUserRole: vi.fn(),
    deleteUser: vi.fn(),
    resendInvite: vi.fn(),
  }
}

/** Counts including 0 and large values, per the task's generation strategy. */
const countArb = fc.integer({ min: 0, max: 1_000_000 })

const pageHealthArb = fc.array(
  fc.record({
    pageIndex: fc.nat({ max: 50 }),
    textLayerChars: countArb,
    ocrChars: countArb,
  }),
  { maxLength: 3 },
)

/**
 * A present `DetectionSummary`. `llmSpans`/`llmOnlySpans` are always present
 * (both `.pdf` and `.docx`); each dual-path key is independently present or
 * absent, so the space reaches the full `.pdf` shape (all four present), the
 * `.docx` shape (all four absent), and every partial shape in between.
 */
const presentSummaryArb: fc.Arbitrary<DetectionSummary> = fc
  .record({
    llmSpans: countArb,
    llmOnlySpans: countArb,
    textLayerRegions: countArb,
    ocrRegions: countArb,
    agreedRegions: countArb,
    pageHealth: pageHealthArb,
    hasTextLayer: fc.boolean(),
    hasOcr: fc.boolean(),
    hasAgreed: fc.boolean(),
    hasPageHealth: fc.boolean(),
  })
  .map((r) => {
    const summary: DetectionSummary = {
      llmSpans: r.llmSpans,
      llmOnlySpans: r.llmOnlySpans,
    }
    if (r.hasTextLayer) summary.textLayerRegions = r.textLayerRegions
    if (r.hasOcr) summary.ocrRegions = r.ocrRegions
    if (r.hasAgreed) summary.agreedRegions = r.agreedRegions
    if (r.hasPageHealth) summary.pageHealth = r.pageHealth
    return summary
  })

/** The `detectionSummary` prop space: null, omitted (undefined), or present. */
type SummaryOption =
  | { tag: 'null' }
  | { tag: 'undefined' }
  | { tag: 'present'; summary: DetectionSummary }

const summaryOptionArb: fc.Arbitrary<SummaryOption> = fc.oneof(
  fc.constant<SummaryOption>({ tag: 'null' }),
  fc.constant<SummaryOption>({ tag: 'undefined' }),
  presentSummaryArb.map<SummaryOption>((summary) => ({ tag: 'present', summary })),
)

const statusArb: fc.Arbitrary<JobStatus> = fc.constantFrom('processing', 'completed', 'failed')

describe('JobStatusView property', () => {
  it('renders the LLM-only count iff completed & summary present, per-path counts only for present keys, and status/download depend only on status', () => {
    const client = makeClient()
    let iterations = 0
    // Track that the generator actually reached the shapes that make this a
    // property rather than the examples: a present .docx-shaped summary (no
    // dual-path keys) and a present partial shape (some but not all keys).
    let reachedDocxShape = false
    let reachedPartialShape = false
    let reachedFullPdfShape = false

    fc.assert(
      fc.property(statusArb, summaryOptionArb, (status, option) => {
        iterations += 1

        const summary = option.tag === 'present' ? option.summary : undefined
        const summaryPresent = summary != null

        if (summaryPresent) {
          const hasText = summary.textLayerRegions != null
          const hasOcr = summary.ocrRegions != null
          if (!hasText && !hasOcr && summary.agreedRegions == null && summary.pageHealth == null) {
            reachedDocxShape = true
          } else if (hasText && hasOcr) {
            reachedFullPdfShape = true
          } else {
            reachedPartialShape = true
          }
        }

        const detectionSummary = option.tag === 'null' ? null : option.tag === 'undefined' ? undefined : summary

        const { container } = render(
          <JobStatusView jobId="job-prop" status={status} client={client} detectionSummary={detectionSummary} />,
        )
        try {
          const llmOnly = container.querySelector('[data-testid="llm-only-count"]')
          const dualPath = container.querySelector('[data-testid="dual-path-counts"]')
          const statusIndicator = container.querySelector('[data-testid="status-indicator"]')
          const downloadButton = container.querySelector('button')

          // LLM-only line present iff completed && summary present.
          const expectLlm = status === 'completed' && summaryPresent
          expect(Boolean(llmOnly)).toBe(expectLlm)

          // Dual-path line present iff completed && summary present && BOTH
          // textLayerRegions and ocrRegions carried by the summary.
          const expectDual =
            status === 'completed' &&
            summaryPresent &&
            summary.textLayerRegions != null &&
            summary.ocrRegions != null
          expect(Boolean(dualPath)).toBe(expectDual)

          // Nothing rendered may ever leak a missing-key artifact.
          const renderedText = container.textContent ?? ''
          expect(renderedText).not.toContain('undefined')
          expect(renderedText).not.toContain('NaN')

          // When the LLM line renders, it carries the drawn llmOnlySpans value.
          if (expectLlm && summary) {
            expect(llmOnly?.textContent ?? '').toContain(String(summary.llmOnlySpans))
          }

          // Status indicator text is a pure function of status — identical
          // whether or not a summary is present.
          expect(statusIndicator?.textContent).toBe(EXPECTED_STATUS_TEXT[status])

          // Download-button visibility is a pure function of status — visible
          // iff completed, independent of the summary.
          expect(Boolean(downloadButton)).toBe(status === 'completed')
        } finally {
          cleanup()
        }
      }),
      { numRuns: 300 },
    )

    // Evidence the run exercised 100+ examples and reached the shapes that
    // distinguish this property from the example-based tests.
    // eslint-disable-next-line no-console
    console.info(
      `[JobStatusView property] iterations=${iterations}
      reachedFullPdfShape=${reachedFullPdfShape} ` +
        `reachedPartialShape=${reachedPartialShape} reachedDocxShape=${reachedDocxShape}`,
    )
    expect(iterations).toBeGreaterThanOrEqual(100)
    expect(reachedDocxShape).toBe(true)
    expect(reachedPartialShape).toBe(true)
    expect(reachedFullPdfShape).toBe(true)
  })
})
