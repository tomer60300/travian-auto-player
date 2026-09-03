/**
 * Auto-scout, STARTED and finished, against the dev server.
 *
 * `AutoScoutPanel` keeps two refs -- `mountedRef` and `loopStoppedRef` -- and
 * set them in an effect that had a cleanup and NO body. React re-runs an
 * effect's cleanup and body once on mount under `StrictMode` (which
 * `src/main.jsx` wraps the whole app in), so the cleanup fired on a perfectly
 * healthy mount and left `mountedRef.current === false` and
 * `loopStoppedRef.current === true` from then on. `ScanConfigPanel` had the
 * same bug and fixed it 300 lines above; this panel did not.
 *
 * What that cost, all of it downstream of those two booleans:
 *
 *   * `handleAutoMessage` opens with `if (!data || !mountedRef.current) return`,
 *     so every progress frame was dropped;
 *   * `runOnePass` opens with `if (!mountedRef.current ||
 *     loopStoppedRef.current) { resolve(); return }`, so it resolved WITHOUT
 *     opening a socket -- the pass never started at all;
 *   * `handleStart`'s three `if (mountedRef.current) setRunning(false)` sites
 *     therefore never ran, and "Start Auto-Scout" turned into a Stop button
 *     that stayed for ever.
 *
 * So this spec asserts the two observable ends of that: clicking Start opens
 * `/ws/scout/auto`, and the terminal frame puts the Start button back. Both are
 * false before the fix -- the first because no socket is opened, the second
 * because `setRunning(false)` is behind the dead guard.
 *
 * It has to be a BROWSER test. `renderToString` runs no effects, so neither
 * `pagesRender.test.jsx` nor a vitest render can reach a bug that only exists
 * because an effect ran; and the double-invoke that triggers it is
 * development-only, which is exactly the build the operator uses on :5173.
 *
 * NO BACKEND AND NO GAME REQUEST: `page.route('** /api/**')` answers the two
 * calls the shell makes and ABORTS everything else, and both sockets are
 * played by Playwright -- nothing leaves the browser. See
 * `inputWidths.pw.js` for the same two fail-closed mechanisms.
 */

import { expect, test } from '@playwright/test'

const PLAYER = 'e2e-operator'
const CAPITAL = 20002

const SCAN_TILES = [
  { x: -117, y: 143, name: 'Rheinbund-Aussenposten', population: 512, player_name: 'Bergvolk' },
  { x: -112, y: 139, name: 'Oase 47', population: 0, player_name: '' },
]

/** Every socket the run needs, and a hard close for anything else.
 *
 * `opened` collects the paths so the test can assert that the auto-scout
 * socket was reached at all -- which is the direct question "did the pass
 * start", with no dependence on what the frames then say. */
function sockets(opened) {
  return (ws) => {
    const path = new URL(ws.url()).pathname
    opened.push(path)
    if (path.endsWith('/ws/scout/scan')) {
      return ws.onMessage(() => {
        ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-scan' }))
        ws.send(JSON.stringify({ type: 'complete', tiles: SCAN_TILES, stats: { time_seconds: 1 } }))
        ws.send(JSON.stringify({ type: 'operation_complete', status: 'completed' }))
      })
    }
    if (path.endsWith('/ws/scout/auto')) {
      // The two frames `useResumableOperation` and `handleAutoMessage`
      // actually key off: `session_init` moves the op to running and persists
      // the session, `operation_complete` fires the pass resolver that
      // `runOnePass` parked in `passResolverRef`.
      return ws.onMessage(() => {
        ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-auto' }))
        ws.send(JSON.stringify({ type: 'operation_complete', status: 'completed' }))
      })
    }
    return ws.close()
  }
}

async function isolate(page, opened) {
  await page.routeWebSocket(/.*/, sockets(opened))
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/users/me')) {
      return route.fulfill({ json: { id: 1, username: PLAYER, is_active: true } })
    }
    if (path.endsWith('/travian/status')) {
      return route.fulfill({
        json: {
          connected: true,
          server_url: 'https://ts2.x1.europe.travian.com',
          player_name: PLAYER,
          tribe_id: 1,
          active_village_id: CAPITAL,
          villages: [{ id: CAPITAL, name: '02', x: 0, y: 0 }],
        },
      })
    }
    return route.abort('blockedbyclient')
  })
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-not-a-real-token')
    // A stored session from an earlier run would make the panel mount
    // straight into `running` and hide the Start button this spec clicks.
    localStorage.removeItem('resumableOp:scout-auto')
  })
}

test('a single auto-scout pass starts and then hands the Start button back', async ({ page }) => {
  const opened = []
  await isolate(page, opened)

  await page.goto('/scout')
  await page.getByRole('button', { name: 'Scan Map' }).click()

  // A fresh scan selects every result, so the panel's Start button carries the
  // target count as soon as the results table appears.
  const start = page.getByRole('button', { name: /^Start Auto-Scout \(2 targets\)$/ })
  await expect(start).toBeVisible()
  await start.click()

  // 1. The pass really started. Before the fix `runOnePass` resolved on its
  //    own mount guard and never called `scoutAutoOp.start`, so no socket was
  //    ever opened for it.
  await expect
    .poll(() => opened.filter((p) => p.endsWith('/ws/scout/auto')).length, { timeout: 10_000 })
    .toBe(1)

  // 2. And it ended. `handleStart` awaits the pass and then calls
  //    `setRunning(false)` behind `if (mountedRef.current)`.
  await expect(start).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole('button', { name: 'Stop' })).toHaveCount(0)
})
