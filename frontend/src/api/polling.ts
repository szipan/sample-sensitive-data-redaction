/**
 * Short-polling helper.
 *
 * Job status and Rule Operation status are both short-polled (e.g. every 2
 * seconds) until a terminal state is reached.
 *
 * This module provides one generic, reusable polling primitive
 * (`pollUntilTerminal`) plus two thin convenience wrappers for the two
 * concrete use cases:
 *   - `pollJobStatus`          -> terminal when `status !== 'processing'`
 *                                 (i.e. `completed` or `failed`).
 *   - `pollRuleOperationStatus` -> terminal when
 *                                 `status !== 'queued' && status !== 'processing'`
 *                                 (i.e. `applied` or `rejected`).
 *
 * API shape chosen: an event-emitter-style controller object with
 * `start()`/`stop()` plus callbacks supplied up front (`onUpdate`,
 * `onTerminal`, `onError`). Rationale, for whoever wires this into the
 * view components via a hook:
 *
 *   - `start()`/`stop()` map directly onto a React `useEffect` mount/cleanup
 *     pair: create the handle, call `start()`, return `stop` as the cleanup
 *     function. No extra bookkeeping is needed in the component.
 *   - Callbacks are supplied to the factory call (not registered later via
 *     an `onUpdate(cb)` method) so there is
 *     no window during which a poll could fire before a callback is
 *     attached — the callbacks are guaranteed to be in place before the
 *     first fetch is even scheduled. This removes a class of ordering bugs
 *     that an event-emitter with post-hoc `.on(...)` registration would
 *     otherwise allow.
 *   - This is intentionally NOT a Promise-that-resolves-once-terminal,
 *     because callers (the views) need to *render every intermediate
 *     status*, not just the final one (e.g. show "processing" immediately,
 *     then flip to "completed"/"failed"). A single resolved Promise cannot
 *     deliver those intermediate updates.
 *
 * Timing behavior:
 *   - `start()` issues the first `fetchFn()` call immediately (no initial
 *     delay), so the caller gets a status update as soon as possible rather
 *     than waiting a full interval first.
 *   - Each subsequent poll is scheduled `intervalMs` (default 2000ms) after
 *     the *previous* fetch settles — not on a fixed
 *     `setInterval` clock — so slow or retried fetches can never overlap
 *     with the next scheduled one.
 *   - Polling stops automatically, permanently, the first time a fetched
 *     result satisfies `isTerminal`.
 *   - `stop()` can be called at any time (including before `start()`, or
 *     multiple times) to cancel further polling, e.g. on component
 *     unmount. It is idempotent and safe to call from within any callback.
 *
 * Error handling (timeout semantics live in the view layer, not here):
 *   - If `fetchFn()` rejects (including with `ApiTimeoutError` from
 *     ./client.ts), the rejection is caught here and surfaced via the
 *     `onError` callback — it is never re-thrown and never becomes an
 *     unhandled promise rejection.
 *   - A single failed attempt does NOT stop polling: the next poll is still
 *     scheduled after `intervalMs`, so a transient timeout/network blip
 *     does not permanently strand the UI in a stale state.
 *   - This module does NOT silently retry forever "in the dark", either:
 *     every single failure — not just the first — is reported to the
 *     caller via `onError`, with no suppression or backoff hidden inside
 *     this module. That visibility is what bounds the retrying in
 *     practice: the caller (view layer) decides, with full information,
 *     whether/when to call `stop()` (e.g. after N consecutive errors, or
 *     immediately). This mirrors the API client's approach of keeping
 *     "what to do about a failed request" a view-layer decision, while
 *     this module's job is limited to "poll, and tell you everything that
 *     happens".
 */

/** Default: short-poll every 2 seconds. */
export const DEFAULT_POLL_INTERVAL_MS = 2_000

/** Callbacks supplied to `pollUntilTerminal` (and its convenience wrappers). */
export interface PollCallbacks<T> {
  /** Invoked after every successful `fetchFn()` call, terminal or not. */
  onUpdate: (result: T) => void
  /**
   * Invoked once, immediately after `onUpdate`, the first time a fetched
   * result satisfies `isTerminal`. Polling has already stopped by the time
   * this fires.
   */
  onTerminal?: (result: T) => void
  /**
   * Invoked whenever `fetchFn()` rejects. Polling is NOT stopped
   * automatically — call `handle.stop()` from within this callback (or
   * from elsewhere) if the caller decides repeated failures should end
   * polling.
   */
  onError?: (error: unknown) => void
}

/** Controller returned by `pollUntilTerminal` to start/stop the poll loop. */
export interface PollHandle {
  /**
   * Begins polling: issues an immediate `fetchFn()` call, then continues
   * every `intervalMs` until a terminal result is observed or `stop()` is
   * called. Calling `start()` more than once (before `stop()`) is a no-op —
   * it will not spawn a second concurrent poll loop.
   */
  start: () => void
  /**
   * Stops polling. Safe to call at any time, including before `start()`
   * or more than once. After `stop()`, no further `fetchFn()` calls will
   * be issued by this handle.
   */
  stop: () => void
  /** `true` once `stop()` has been called or a terminal result was reached. */
  readonly isStopped: boolean
}

/**
 * Generic short-polling helper.
 *
 * Polls `fetchFn()` every `intervalMs` (default 2000ms) and reports each
 * result via `callbacks.onUpdate`. Stops automatically the first time a
 * result satisfies `callbacks`'s terminal predicate `isTerminal`, firing
 * `callbacks.onTerminal` for that result. Can also be stopped early via the
 * returned handle's `stop()`.
 *
 * See the module-level doc comment above for the full rationale, timing
 * behavior, and error-handling semantics.
 *
 */
export function pollUntilTerminal<T>(
  fetchFn: () => Promise<T>,
  isTerminal: (result: T) => boolean,
  callbacks: PollCallbacks<T>,
  intervalMs: number = DEFAULT_POLL_INTERVAL_MS,
): PollHandle {
  let stopped = false
  let started = false
  let timerId: ReturnType<typeof setTimeout> | undefined

  async function tick(): Promise<void> {
    if (stopped) {
      return
    }

    let result: T
    try {
      result = await fetchFn()
    } catch (error) {
      if (stopped) {
        return
      }
      callbacks.onError?.(error)
      if (stopped) {
        return
      }
      timerId = setTimeout(() => void tick(), intervalMs)
      return
    }

    if (stopped) {
      return
    }
    callbacks.onUpdate(result)
    if (stopped) {
      return
    }

    if (isTerminal(result)) {
      stopped = true
      timerId = undefined
      callbacks.onTerminal?.(result)
      return
    }

    timerId = setTimeout(() => void tick(), intervalMs)
  }

  function start(): void {
    if (stopped || started) {
      return
    }
    started = true
    void tick()
  }

  function stop(): void {
    stopped = true
    if (timerId !== undefined) {
      clearTimeout(timerId)
      timerId = undefined
    }
  }

  return {
    start,
    stop,
    get isStopped() {
      return stopped
    },
  }
}

/* -------------------------------------------------------------------------- */
/* Convenience wrappers for the two concrete use cases                        */
/* -------------------------------------------------------------------------- */

/**
 * Minimal shape needed from a Job status fetch to decide termination.
 * Matches `JobStatusResponse` from ./types (kept structural here to avoid
 * this module depending on the full response shape).
 */
export interface JobLikeStatus {
  status: 'processing' | 'detected' | 'completed' | 'failed'
}

/**
 * Polls Job status (`GET /jobs/{jobId}` via the API client's
 * `getJobStatus`) every `intervalMs` until it reaches a terminal state:
 * `completed` or `failed` (i.e. any status other than `processing`).
 *
 */
export function pollJobStatus<T extends JobLikeStatus>(
  fetchFn: () => Promise<T>,
  callbacks: PollCallbacks<T>,
  intervalMs: number = DEFAULT_POLL_INTERVAL_MS,
): PollHandle {
  return pollUntilTerminal(fetchFn, (result) => result.status !== 'processing', callbacks, intervalMs)
}

/**
 * Minimal shape needed from a Rule Operation status fetch to decide
 * termination. Matches `RuleOperationStatusResponse` from ./types (kept
 * structural here to avoid this module depending on the full response
 * shape).
 */
export interface RuleOperationLikeStatus {
  status: 'queued' | 'processing' | 'applied' | 'rejected'
}

/**
 * Polls Rule Operation status (`GET /rule-operations/{operationId}` via the
 * API client's `getRuleOperationStatus`) every `intervalMs` until it
 * reaches a Terminal Result: `applied` or `rejected` (i.e. any status other
 * than `queued` or `processing`).
 *
 */
export function pollRuleOperationStatus<T extends RuleOperationLikeStatus>(
  fetchFn: () => Promise<T>,
  callbacks: PollCallbacks<T>,
  intervalMs: number = DEFAULT_POLL_INTERVAL_MS,
): PollHandle {
  return pollUntilTerminal(
    fetchFn,
    (result) => result.status !== 'queued' && result.status !== 'processing',
    callbacks,
    intervalMs,
  )
}
