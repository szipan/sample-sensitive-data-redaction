import { describe, expect, it } from 'vitest'
import { getJobStatusDisplay } from './jobStatusDisplay'

describe('getJobStatusDisplay', () => {
  it('maps "processing" to the processing indicator with the download action hidden and no failure', () => {
    expect(getJobStatusDisplay('processing')).toEqual({
      indicator: 'processing',
      downloadVisible: false,
      showFailure: false,
    })
  })

  it('maps "completed" to the completed indicator with the download action visible and no failure', () => {
    expect(getJobStatusDisplay('completed')).toEqual({
      indicator: 'completed',
      downloadVisible: true,
      showFailure: false,
    })
  })

  it('maps "failed" to the failed indicator with the download action hidden and a failure indication', () => {
    expect(getJobStatusDisplay('failed')).toEqual({
      indicator: 'failed',
      downloadVisible: false,
      showFailure: true,
    })
  })
})
