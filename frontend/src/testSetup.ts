/**
 * Global Vitest setup: registers @testing-library/jest-dom's matchers
 * (e.g. `toBeInTheDocument`, `toHaveTextContent`) on Vitest's `expect`,
 * and unmounts/cleans up any React tree rendered by
 * @testing-library/react after every test.
 *
 * Without the `cleanup()` call below, DOM nodes rendered by one test
 * (e.g. via `render()`) persist into the next test in the same file,
 * causing false "multiple elements found" failures once more than one
 * test in a `.test.tsx` file calls `render()`.
 *
 * Loaded for every test file via `test.setupFiles` in vite.config.ts.
 * Safe to load for `.test.ts` (node environment) files too: `cleanup()`
 * is a no-op when nothing was rendered, and jest-dom's matchers don't
 * touch the DOM by themselves.
 */
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
