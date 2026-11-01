import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    // Default environment stays 'node' for plain logic/unit tests
    // (src/**/*.test.ts). Component tests (src/**/*.test.tsx) render into a
    // DOM via @testing-library/react, so they opt into jsdom individually
    // via a `// @vitest-environment jsdom` docblock comment at the top of
    // the file (see src/views/UploadView.test.tsx) rather than switching
    // the environment globally for every test.
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    setupFiles: ['./src/testSetup.ts'],
  },
})
