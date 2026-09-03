/**
 * Visual regression for the login page, at the three viewports the UI Definition
 * of Done names.
 *
 * `@playwright/test` plus its browser downloads is ~400MB, and this repo's frontend gate
 * (`npx eslint . && npm test && npm run build`) runs in a couple of seconds, so for a while
 * the spec shipped as a template with the dependency left out. The operator asked for it on
 * 2026-09-02; it is installed now and the baselines are committed.
 *
 * Running it:
 *   cd frontend
 *   npx playwright install chromium          # once per machine, ~115MB
 *   npx playwright test                      # compares against the committed baselines
 *   npx playwright test --update-snapshots   # after an INTENTIONAL layout change
 *
 * Baselines are platform-suffixed (`-win32.png`) because sub-pixel text rendering differs
 * between operating systems. A run on another OS diffs on font rendering alone, which is why
 * this is not in CI -- `maxDiffPixelRatio` absorbs run-to-run noise, not a different platform.
 *
 * Not every spec here is a visual one. `roleTemplates.pw.js` drives the Role-templates
 * panel's change handlers -- which `renderToString` cannot reach, because it runs no
 * events -- and asserts on stored state rather than on pixels, so the platform-suffixed
 * baseline reasoning above does not apply to it. It mocks every `/api` call and aborts
 * anything it did not anticipate, so it needs no backend either.
 *
 * Note the `.pw.js` suffix on specs, and `testMatch` below. It is not decoration: vitest's
 * default include pattern is `**\/*.{test,spec}.?(c|m)[jt]s?(x)`, so a conventionally-named
 * `*.spec.js` under frontend/ would be collected by `npm test` and fail there for want of
 * `@playwright/test`. The suffix keeps the two runners from fighting over the same files.
 */

import { defineConfig, devices } from '@playwright/test'

const PORT = process.env.PORT || 5173

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.pw.js',

  // Visual regression is worthless when it is flaky. One worker, no retries: a diff is either
  // real or the baseline is stale, and both deserve a human.
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'e2e/report' }]],

  use: {
    baseURL: `http://localhost:${PORT}`,
    // Never let a spec reach the real game. There is a live Travian account on this machine.
    ignoreHTTPSErrors: false,
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
  },

  expect: {
    toHaveScreenshot: {
      // Sub-pixel text rendering differs between runs and machines. 1.5% of pixels absorbs that
      // without absorbing a real one-element layout shift.
      maxDiffPixelRatio: 0.015,
      animations: 'disabled',
      scale: 'css',
    },
  },

  // Reuses the dev server the operator already has open; starts one only if 5173 is free.
  // 8001 is the debug backend. Port 80 is the operator's production server — nothing here may
  // start, stop, or proxy to it.
  webServer: {
    command: 'npm run dev',
    url: `http://localhost:${PORT}`,
    reuseExistingServer: true,
    timeout: 60_000,
  },
})
