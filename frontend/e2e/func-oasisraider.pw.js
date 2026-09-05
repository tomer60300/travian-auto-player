/**
 * Oasis Raider sends real troops at real oases in a loop, and everything it is
 * told travels in ONE WebSocket frame at start. That frame is the whole
 * intent: which troops, how far out, which bonus types, how often to repeat,
 * and -- the one that decides whether anything is written to the game at all
 * -- `dry_run`.
 *
 * So the two questions here are whether the frame matches the form, and
 * whether the page's status and log tell the truth about what came back. A
 * sweep that reports "Completed" over a server error would send the operator
 * away from a village whose troops never left.
 *
 * BOTH TESTS FAIL TODAY, and on the same line: nothing the socket sends ever
 * reaches the page. `OasisRaider`'s mount effect is cleanup-ONLY --
 *
 *     const mountedRef = useRef(true)
 *     useEffect(() => { return () => { mountedRef.current = false } }, [])
 *
 * -- and `src/main.jsx` wraps the app in `StrictMode`, which re-runs an
 * effect's cleanup and body once on mount. The cleanup fires on a perfectly
 * healthy mount and the body never sets the ref back, so `mountedRef.current`
 * is `false` from the first render onwards and `handleOpMessage`'s opening
 * `if (!mountedRef.current || !data) return` drops every frame: no log lines,
 * no status transitions, no summary card, and no error toast.
 *
 * This is the regression `autoScoutRun.pw.js` documents, in the one place that
 * has not taken the fix. `AutoScout` sets `mountedRef.current = true` in BOTH
 * of its panels' effect bodies (lines 345 and 1354); `FarmLists` (line 159) and
 * `BuildQueue` (line 522) have the cleanup-only version too.
 *
 * The frame assertions below pass, so the CONFIG half of this page is sound --
 * it is only the report of what happened that never arrives.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know; the raider's socket is played by
 * Playwright. There is a live Travian account on this machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

/** Plays the raider socket and records the config frame the page sent. */
function sockets(state) {
  return (ws) => {
    const path = new URL(ws.url()).pathname
    if (!path.endsWith('/ws/oasis-raider')) return ws.close()
    ws.onMessage((m) => {
      // `useResumableOperation.start` wraps the config: the first client frame
      // on a starter socket is `{action: "start", config: {...}}`.
      state.config = JSON.parse(String(m)).config
      ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-oasis' }))
      if (state.fail) {
        ws.send(JSON.stringify({ type: 'error', message: 'no rally point in this village' }))
        ws.send(JSON.stringify({ type: 'operation_complete', status: 'failed' }))
        return
      }
      ws.send(
        JSON.stringify({
          type: 'log',
          data: { emoji: '🎯', category: 'SCAN', message: '18 oases in radius', level: 'info' },
        })
      )
      ws.send(JSON.stringify({ type: 'status', data: { state: 'running' } }))
      ws.send(
        JSON.stringify({
          type: 'summary',
          data: { total: 18, sent: 11, skipped_animals: [1, 2], skipped_troops: 5, duration: 92 },
        })
      )
      ws.send(JSON.stringify({ type: 'status', data: { state: 'completed' } }))
      ws.send(JSON.stringify({ type: 'operation_complete', status: 'completed' }))
    })
  }
}

async function arrive(page, state) {
  await isolateApp(page)
  // AFTER `isolateApp`, whose blanket socket close would otherwise win.
  await page.routeWebSocket(/.*/, sockets(state))
  await page.addInitScript(() => localStorage.removeItem('resumableOp:oasis-raider'))
  await page.goto('/oasis-raider')
}

function toast(page) {
  return page.locator('.toast').first()
}

test('a dry run refuses to start with no troops, then carries the whole form', async ({ page }) => {
  const state = {}
  await arrive(page, state)

  // Every `w-24` spinbox on this page, in DOM order: one per troop row, then
  // Max Targets, Sleep Interval and Repeat Interval. None of the six labels on
  // this page carries `htmlFor` or wraps its box, so position is what there is
  // -- and the indices shift when a troop row is removed, which is why the
  // order of the steps below matters.
  const boxes = page.locator('input.input-field.w-24')

  // ── THE GUARD ─────────────────────────────────────────────────────
  // Zeroing both rows leaves nothing to send. This must cost no socket: an
  // empty troop dict reaching the server is a request that can only be
  // refused, and the page can see that from the form.
  await boxes.nth(0).fill('0')
  await boxes.nth(1).fill('0')
  await page.getByRole('button', { name: 'Dry Run' }).click()
  await expect(toast(page)).toHaveClass(/toast-warning/)
  await expect(toast(page)).toContainText('No troops configured')
  expect(state.config, 'no socket was opened').toBeUndefined()

  // ── THE FORM ──────────────────────────────────────────────────────
  await boxes.nth(0).fill('50')
  // Row 2 is t6, which for Romans (tribe 1) is Equites Caesaris. Removing it
  // leaves one troop row, so the three settings below shift down one index.
  await page
    .getByRole('button', { name: 'Remove row 2 (Equites Caesaris) from the composition' })
    .click()
  await expect(boxes).toHaveCount(4)
  await page.getByRole('checkbox', { name: 'Crop' }).uncheck()
  await page.locator('input.input-field.w-32').fill('9') // Scan Radius, the only w-32
  await boxes.nth(1).fill('12') // Max Targets
  await boxes.nth(2).fill('45') // Sleep Interval
  await boxes.nth(3).fill('3600') // Repeat Interval

  await page.getByRole('button', { name: 'Dry Run' }).click()

  // 1. THE FRAME. `dry_run: true` is the difference between a rehearsal and
  //    eleven raids leaving the village; `bonus_filter` is lowercased and only
  //    sent when it is actually a filter (all four checked means no filter).
  await expect.poll(() => !!state.config).toBe(true)
  expect(state.config.dry_run).toBe(true)
  expect(state.config.radius).toBe(9)
  expect(state.config.troops).toEqual({ t1: 50 })
  expect(state.config.bonus_filter).toEqual(['wood', 'clay', 'iron'])
  expect(state.config.max_targets).toBe(12)
  expect(state.config.sleep_interval).toBe(45)
  expect(state.config.repeat_interval_seconds).toBe(3600)
  expect(state.config.village_id).toBe(CAPITAL)

  // 2. THE PAGE reflects the frames that came back: the server's log line, its
  //    status, and its summary figures -- including the two kinds of skip,
  //    which are the difference between "nothing there" and "no troops left".
  //
  //    Fails at the first line: see the header. Every frame is dropped by the
  //    dead `mountedRef` guard, so the panel below the form stays empty for
  //    the whole sweep and the operator watches a run they cannot see.
  await expect(page.getByText('18 oases in radius')).toBeVisible({ timeout: 3000 })
  await expect(page.getByText('Total targets: 18')).toBeVisible()
  await expect(page.getByText('Raids sent: 11')).toBeVisible()
  await expect(page.getByText('Skipped (animals): 2')).toBeVisible()
  await expect(page.getByText('Skipped (no troops): 5')).toBeVisible()
  await expect(page.getByText('Completed', { exact: true }).first()).toBeVisible()
})

test('a live sweep the server refused is not left reading as Completed', async ({ page }) => {
  const state = { fail: true }
  await arrive(page, state)

  await page.getByRole('button', { name: 'Start Raiding' }).click()

  // 1. THE FRAME says live. This is the one control that decides whether the
  //    game is written to.
  await expect.poll(() => !!state.config).toBe(true)
  expect(state.config.dry_run).toBe(false)

  // 3. THE FAILURE BRANCH. The refusal reaches the log AND the toast, and the
  //    status chip must not settle on the success word: `operation_complete`
  //    with `status: "failed"` is not a completed sweep.
  //
  //    Fails at the first line, for the reason in the header: the `error`
  //    frame's `addLog` and `toast.error` both sit behind the dead
  //    `mountedRef` guard, so a sweep the game refused says NOTHING at all --
  //    which is worse than saying the wrong thing.
  await expect(page.getByText('no rally point in this village')).toBeVisible({ timeout: 3000 })
  await expect(toast(page)).toHaveClass(/toast-error/)
  await expect(page.getByText('Completed', { exact: true })).toHaveCount(0)
  // And no summary card is invented for a sweep that produced none.
  await expect(page.getByRole('heading', { name: 'Summary' })).toHaveCount(0)
})
