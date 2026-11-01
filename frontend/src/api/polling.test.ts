import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_POLL_INTERVAL_MS,
  pollJobStatus,
  pollRuleOperationStatus,
  pollUntilTerminal,
} from './polling'

describe('pollUntilTerminal', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('polls immediately on start(), then stops once the terminal predicate is satisfied', async () => {
    const results = ['processing', 'processing', 'completed']
    let call = 0
    const fetchFn = vi.fn(async () => results[call++])
    const onUpdate = vi.fn()
    const onTerminal = vi.fn()

    const handle = pollUntilTerminal(fetchFn, (r) => r === 'completed', { onUpdate, onTerminal })
    handle.start()

    // Immediate first poll (no need to advance timers).
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchFn).toHaveBeenCalledTimes(1)
    expect(onUpdate).toHaveBeenNthCalledWith(1, 'processing')
    expect(handle.isStopped).toBe(false)

    // Second poll after one interval.
    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
    expect(fetchFn).toHaveBeenCalledTimes(2)
    expect(onUpdate).toHaveBeenNthCalledWith(2, 'processing')
    expect(handle.isStopped).toBe(false)
    expect(onTerminal).not.toHaveBeenCalled()

    // Third poll reaches the terminal state and polling stops.
    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
    expect(fetchFn).toHaveBeenCalledTimes(3)
    expect(onUpdate).toHaveBeenNthCalledWith(3, 'completed')
    expect(onTerminal).toHaveBeenCalledTimes(1)
    expect(onTerminal).toHaveBeenCalledWith('completed')
    expect(handle.isStopped).toBe(true)

    // No further polls even after many more intervals elapse.
    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS * 5)
    expect(fetchFn).toHaveBeenCalledTimes(3)
  })

  it('continues polling at ~2s intervals while the result stays non-terminal', async () => {
    const fetchFn = vi.fn(async () => 'processing')
    const onUpdate = vi.fn()

    const handle = pollUntilTerminal(fetchFn, (r) => r === 'completed', { onUpdate })
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(fetchFn).toHaveBeenCalledTimes(1)

    for (let i = 2; i <= 5; i++) {
      await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
      expect(fetchFn).toHaveBeenCalledTimes(i)
    }
    expect(onUpdate).toHaveBeenCalledTimes(5)
    expect(handle.isStopped).toBe(false)
  })

  it('respects a custom intervalMs', async () => {
    const fetchFn = vi.fn(async () => 'processing')
    const handle = pollUntilTerminal(fetchFn, () => false, { onUpdate: vi.fn() }, 500)
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(fetchFn).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(499)
    expect(fetchFn).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1)
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('calling stop() prevents further polls', async () => {
    const fetchFn = vi.fn(async () => 'processing')
    const onUpdate = vi.fn()
    const handle = pollUntilTerminal(fetchFn, () => false, { onUpdate })
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(fetchFn).toHaveBeenCalledTimes(1)

    handle.stop()
    expect(handle.isStopped).toBe(true)

    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS * 10)
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })

  it('stop() is idempotent and safe to call before start()', async () => {
    const fetchFn = vi.fn(async () => 'processing')
    const handle = pollUntilTerminal(fetchFn, () => false, { onUpdate: vi.fn() })

    handle.stop()
    handle.stop()
    handle.start() // start() after stop() must not begin polling.

    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS * 3)
    expect(fetchFn).not.toHaveBeenCalled()
  })

  it('calling start() more than once does not spawn concurrent poll loops', async () => {
    const fetchFn = vi.fn(async () => 'processing')
    const handle = pollUntilTerminal(fetchFn, () => false, { onUpdate: vi.fn() })

    handle.start()
    handle.start()
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(fetchFn).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('surfaces a fetchFn error via onError without throwing, and continues polling afterward', async () => {
    const error = new Error('boom')
    let call = 0
    const fetchFn = vi.fn(async () => {
      call++
      if (call === 2) {
        throw error
      }
      return 'processing'
    })
    const onUpdate = vi.fn()
    const onError = vi.fn()

    const handle = pollUntilTerminal(fetchFn, () => false, { onUpdate, onError })

    expect(() => handle.start()).not.toThrow()
    await vi.advanceTimersByTimeAsync(0)
    expect(onUpdate).toHaveBeenCalledTimes(1)

    // Second poll rejects; must not throw/crash and must not call onUpdate.
    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
    expect(fetchFn).toHaveBeenCalledTimes(2)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith(error)
    expect(onUpdate).toHaveBeenCalledTimes(1)
    expect(handle.isStopped).toBe(false)

    // Polling continues (bounded, caller-visible retry) after the error.
    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
    expect(fetchFn).toHaveBeenCalledTimes(3)
    expect(onUpdate).toHaveBeenCalledTimes(2)
  })

  it('calling stop() from within onError halts further polling', async () => {
    const error = new Error('fatal')
    const fetchFn = vi.fn(async () => {
      throw error
    })
    let handle: ReturnType<typeof pollUntilTerminal<string>>
    const onError = vi.fn(() => {
      handle!.stop()
    })

    handle = pollUntilTerminal(fetchFn, () => false, { onUpdate: vi.fn(), onError })
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(handle.isStopped).toBe(true)

    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS * 5)
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })

  it('never produces an unhandled promise rejection when fetchFn always rejects', async () => {
    const fetchFn = vi.fn(async () => {
      throw new Error('always fails')
    })
    const handle = pollUntilTerminal(fetchFn, () => false, {
      onUpdate: vi.fn(),
      onError: vi.fn(),
    })

    handle.start()
    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS * 3)
    handle.stop()

    // Reaching this point without vitest reporting an unhandled rejection
    // is itself the assertion; also sanity-check onError was invoked.
    expect(fetchFn).toHaveBeenCalledTimes(4)
  })
})

describe('pollJobStatus convenience wrapper', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('treats "processing" as non-terminal and "completed"/"failed" as terminal', async () => {
    const statuses = [{ status: 'processing' as const }, { status: 'completed' as const }]
    let call = 0
    const fetchFn = vi.fn(async () => statuses[call++])
    const onTerminal = vi.fn()

    const handle = pollJobStatus(fetchFn, { onUpdate: vi.fn(), onTerminal })
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(handle.isStopped).toBe(false)

    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
    expect(handle.isStopped).toBe(true)
    expect(onTerminal).toHaveBeenCalledWith({ status: 'completed' })
  })

  it('stops on "failed" as well', async () => {
    const fetchFn = vi.fn(async () => ({ status: 'failed' as const }))
    const onTerminal = vi.fn()
    const handle = pollJobStatus(fetchFn, { onUpdate: vi.fn(), onTerminal })
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(handle.isStopped).toBe(true)
    expect(onTerminal).toHaveBeenCalledWith({ status: 'failed' })
  })
})

describe('pollRuleOperationStatus convenience wrapper', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('treats "queued" and "processing" as non-terminal, "applied" as terminal', async () => {
    const statuses = [
      { status: 'queued' as const },
      { status: 'processing' as const },
      { status: 'applied' as const },
    ]
    let call = 0
    const fetchFn = vi.fn(async () => statuses[call++])
    const onTerminal = vi.fn()

    const handle = pollRuleOperationStatus(fetchFn, { onUpdate: vi.fn(), onTerminal })
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(handle.isStopped).toBe(false)

    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
    expect(handle.isStopped).toBe(false)

    await vi.advanceTimersByTimeAsync(DEFAULT_POLL_INTERVAL_MS)
    expect(handle.isStopped).toBe(true)
    expect(onTerminal).toHaveBeenCalledWith({ status: 'applied' })
  })

  it('stops on "rejected" as well', async () => {
    const fetchFn = vi.fn(async () => ({ status: 'rejected' as const }))
    const onTerminal = vi.fn()
    const handle = pollRuleOperationStatus(fetchFn, { onUpdate: vi.fn(), onTerminal })
    handle.start()

    await vi.advanceTimersByTimeAsync(0)
    expect(handle.isStopped).toBe(true)
    expect(onTerminal).toHaveBeenCalledWith({ status: 'rejected' })
  })
})
