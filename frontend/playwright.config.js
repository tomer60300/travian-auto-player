/**
 * Visual regression for the login page, at the three viewports the UI Definition
 * of Done names.
 *
 * `@playwright/test` plus its browser downloads is ~400MB, and this repo's frontend gate
 * (`npx eslint . && npm test`) runs in a couple of seconds, so for a while the spec shipped
 * as a template with the dependency left out. The operator asked for it on 2026-09-02; it is
 * installed now and the baselines are committed.
 *
 * The gate stops at vitest. `npm run build` used to be named here as the third step, which
 * was wrong and was the last sentence in the repo still saying so: the build writes into
 * `src/travian_api/web/static`, which the production server on :80 serves directly, so on
 * this checkout a build IS a deploy. See CLAUDE.md, Phase 2.
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
 * baseline reasoning above does not apply to it. `inputWidths.pw.js` asserts on measured
 * geometry for the same reason it is not a screenshot spec: a baseline PNG of a collapsed
 * table is a baseline of the defect. Both mock every `/api` call and abort anything they
 * did not anticipate, so they need no backend either.
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

  // Only login.visual.pw.js does image comparison (`grep -l "toHaveScreenshot" e2e/*.pw.js`
  // -- one file out of 72). Visual regression is worthless when it is flaky, so THAT project
  // keeps one worker and no retries: a diff is either real or the baseline is stale, and both
  // deserve a human. Every other spec mocks its own network and asserts on the DOM, not
  // pixels, so it has no such flakiness to protect against and can run fully in parallel.
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'e2e/report' }]],

  // Playwright's default `toHaveScreenshot` path bakes in `{-projectName}` so two projects
  // that both screenshot the same test name do not collide. Only one project here ever does
  // (`visual`), so that segment only renamed every committed baseline out from under itself
  // the moment a second, named project existed (`login-tablet-768-win32.png` ->
  // `login-tablet-768-visual-win32.png`, "snapshot doesn't exist"). Dropping `{-projectName}`
  // restores the committed filenames regardless of which project runs the spec.
  snapshotPathTemplate: '{snapshotDir}/{testFileDir}/{testFileName}-snapshots/{arg}{-snapshotSuffix}{ext}',

  projects: [
    {
      name: 'functional',
      testIgnore: '**/login.visual.pw.js',
      fullyParallel: true,
      // This machine runs other agents' work concurrently (see CLAUDE.md's pytest -n 8
      // precedent) -- 8 of its 12 logical cores, not all of them.
      workers: 8,
    },
    {
      name: 'visual',
      testMatch: '**/login.visual.pw.js',
      fullyParallel: false,
      workers: 1,
    },
  ],

  use: {
    baseURL: `http://localhost:${PORT}`,
    // Never let a spec reach the real game. There is a live Travian account on this machine.
    ignoreHTTPSErrors: false,
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
  },

  expect: {
    // The default 5000ms was tuned for one worker against an uncontended dev server. At 8
    // workers a burst of concurrent navigations can push a single assertion past that --
    // observed once in 530 functional tests (profileCap.pw.js, 12 seeded profiles rendering
    // under load), reproduced 0/3 times in isolation, so it is the shared dev server under
    // contention and not the app. Doubling absorbs that without slowing anything that already
    // passes -- an assertion still resolves the moment it is satisfied.
    timeout: 10_000,
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
