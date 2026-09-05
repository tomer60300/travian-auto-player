/**
 * Auto Scout, end to end: configure a map scan, run it, choose which of the
 * results to scout, configure the sweep, and start it.
 *
 * Everything that matters on this page leaves over a WebSocket, so the "what
 * did the app actually send" half of a functional test is the FRAME, not a
 * request body. Both frames are worth pinning: the scan's carries the
 * population band and the exclusions that decide how many map reads the game
 * gets asked for, and the sweep's carries the target list, the scouts per
 * target and the stealth delays -- get any of those wrong and real scouts
 * leave a real village towards the wrong tiles.
 *
 * `autoScoutRun.pw.js` already pins that a pass starts at all and hands the
 * Start button back (the StrictMode mount-ref regression). This file asks what
 * the frames CONTAIN, and what the page says when the pass does not go well.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know; every socket is played by Playwright.
 * There is a live Travian account on this machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const TILES = [
  { x: -117, y: 143, village_name: 'Aussenposten', population: 512, player_name: 'Bergvolk' },
  { x: -112, y: 139, village_name: 'Zweite', population: 340, player_name: 'Talwacht' },
  { x: -108, y: 141, village_name: 'Dritte', population: 288, player_name: 'Talwacht' },
]

const SHELL = {
  '/farm/lists': [],
  '/farm/coord-map': {},
  '/recon/status': { configured: false, username: null },
}

/** The config out of a starter frame.
 *
 * `useResumableOperation.start` wraps it: the first client message on a
 * starter socket is `{action: "start", config: {...}}`, not the config on its
 * own. */
const configOf = (raw) => JSON.parse(String(raw)).config

/** Every socket the page can open, with the frames each test wants played
 *  back. `frames` collects what the CLIENT sent, keyed by path. */
function sockets(frames, { scanTiles = TILES, sweep = 'ok' } = {}) {
  return (ws) => {
    const path = new URL(ws.url()).pathname
    if (path.endsWith('/ws/scout/scan')) {
      return ws.onMessage((m) => {
        frames.scan = configOf(m)
        ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-scan' }))
        ws.send(JSON.stringify({ type: 'complete', tiles: scanTiles, stats: { time_seconds: 1 } }))
        ws.send(JSON.stringify({ type: 'operation_complete', status: 'completed' }))
      })
    }
    if (path.endsWith('/ws/scout/auto')) {
      return ws.onMessage((m) => {
        frames.auto = configOf(m)
        ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-auto' }))
        if (sweep === 'refused') {
          // What the server sends when the sweep cannot proceed -- no scouts
          // at home is the everyday case.
          ws.send(
            JSON.stringify({
              type: 'error',
              message: 'no idle scouts in this village — nothing was sent',
            })
          )
          ws.send(JSON.stringify({ type: 'operation_complete', status: 'failed' }))
          return
        }
        ws.send(
          JSON.stringify({ type: 'complete', successful: 2, total_sent: 2, total_time_seconds: 9 })
        )
        ws.send(JSON.stringify({ type: 'operation_complete', status: 'completed' }))
      })
    }
    return ws.close()
  }
}

async function arrive(page, opts = {}) {
  const frames = {}
  await isolateApp(page, { ...SHELL, ...(opts.extra ?? {}) })
  // AFTER `isolateApp`, whose blanket close would otherwise win.
  await page.routeWebSocket(/.*/, sockets(frames, opts))
  await page.addInitScript(() => {
    // A stored session would mount the panel straight into `running` and hide
    // the Start button these tests click.
    localStorage.removeItem('resumableOp:scout-auto')
    localStorage.removeItem('resumableOp:scout-scan')
  })
  await page.goto('/scout')
  return frames
}

// `.last()`, not `.first()`: the scan a moment earlier already left its own
// "Scan complete" success toast up (toasts self-dismiss after 4s, and this
// whole flow runs in well under that against a mocked socket), so the oldest
// toast on screen is not the sweep's. The newest one is the operator's last
// word on it.
function toast(page) {
  return page.locator('.toast').last()
}

test('the scan frame and the sweep frame carry what was configured, and nothing else', async ({
  page,
}) => {
  const frames = await arrive(page)

  // ── CONFIGURE THE SCAN ────────────────────────────────────────────
  await page.locator('#scan-min-pop').fill('250')
  await page.locator('#scan-max-pop').fill('900')
  await page.getByRole('radio', { name: /^Non-capital villages only/ }).check()
  await page.getByPlaceholder('Player name').fill('Talwacht')
  await page.getByRole('button', { name: 'Add player' }).click()

  await page.getByRole('button', { name: 'Scan Map' }).click()

  // 1. THE SCAN FRAME. The population band and the exclusions decide how many
  //    map reads the game is asked for, and `non_capitals` alone costs a
  //    profile fetch per unique player -- so a control that does not travel is
  //    a control that silently costs requests.
  await expect.poll(() => !!frames.scan).toBe(true)
  expect(frames.scan.min_pop).toBe(250)
  expect(frames.scan.max_pop).toBe(900)
  expect(frames.scan.non_capitals).toBe(true)
  expect(frames.scan.oasis_only).toBe(false)
  expect(frames.scan.exclude_player_names).toEqual(['Talwacht'])
  expect(frames.scan.village_id).toBe(CAPITAL)

  // 2. THE PAGE AFTERWARDS lists the tiles the socket sent, all selected.
  await expect(page.getByText('Aussenposten')).toBeVisible()
  const start = page.getByRole('button', { name: /^Start Auto-Scout \(3 targets\)$/ })
  await expect(start).toBeVisible()

  // ── CONFIGURE AND START THE SWEEP ─────────────────────────────────
  // Drop one target; the button must count what is actually selected, because
  // that count is what leaves.
  await page.getByRole('checkbox', { name: 'Select Zweite (-112, 139)' }).uncheck()
  const start2 = page.getByRole('button', { name: /^Start Auto-Scout \(2 targets\)$/ })
  await expect(start2).toBeVisible()

  await page.locator('#scouts-per-target').fill('2')
  await page.getByRole('radio', { name: 'Defenses' }).check()
  await page.getByLabel('Stealth delay minimum (s)').fill('7')
  await page.getByLabel('Stealth delay maximum (s)').fill('19')
  await start2.click()

  // 1. THE SWEEP FRAME. Two scouts each, defences, the two tiles that are
  //    still ticked -- and the origin village, because the backend's session
  //    default is the login village and scouts would leave from the wrong one.
  await expect.poll(() => !!frames.auto).toBe(true)
  expect(frames.auto.amount).toBe(2)
  expect(frames.auto.type).toBe('defenses')
  expect(frames.auto.delay_min).toBe(7)
  expect(frames.auto.delay_max).toBe(19)
  expect(frames.auto.village_id).toBe(CAPITAL)
  expect(frames.auto.start_index).toBe(0)
  expect(frames.auto.targets.map((t) => [t.x, t.y])).toEqual([
    [-117, 143],
    [-108, 141],
  ])

  // 2. And the pass ends: the Start button comes back, with the same count.
  await expect(start2).toBeVisible({ timeout: 10_000 })
  // `exact` matters: `getByRole`'s `name` is a SUBSTRING match by default, and
  // the excluded-player chip above carries "Stop excluding player Talwacht".
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(0)
})

test('a sweep the server refused is not announced as scouting complete', async ({ page }) => {
  await arrive(page, { sweep: 'refused' })

  await page.getByRole('button', { name: 'Scan Map' }).click()
  const start = page.getByRole('button', { name: /^Start Auto-Scout \(3 targets\)$/ })
  await expect(start).toBeVisible()
  await start.click()

  // The refusal reaches the log, which is right...
  await expect(page.getByText('no idle scouts in this village — nothing was sent')).toBeVisible({
    timeout: 10_000,
  })

  // ...and then `handleStart` (src/pages/AutoScout.jsx) fires
  // `toast.success('Scouting complete')` unconditionally once `runOnePass`
  // resolves. The resolver is `passResolverRef`, which the terminal
  // `operation_complete` frame fires REGARDLESS of its `status` -- so a pass
  // the server ended with `status: "failed"` and an `error` frame still
  // finishes with a green "Scouting complete", and the operator's last word on
  // the sweep is the wrong one.
  //
  // What it should do: read the terminal frame's status (or track whether an
  // `error` arrived) and end with the tone that matches.
  await expect(toast(page)).not.toHaveClass(/toast-success/, { timeout: 2000 })
})

test('the idle-scout pre-flight says whether it could count, not just a number', async ({
  page,
}) => {
  const state = { broken: true }
  await arrive(page)
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.endsWith('/military/troops')) return route.fallback()
    if (state.broken) return route.fulfill({ status: 502, json: { detail: 'rally point unread' } })
    return route.fulfill({ json: { t1: 900, t4: 37 } })
  })

  // The sweep panel -- and so its pre-flight Check -- only exists once a scan
  // has produced targets; `AutoScout` renders it behind `scanResults?.length`.
  await page.getByRole('button', { name: 'Scan Map' }).click()
  await expect(page.getByRole('button', { name: /^Start Auto-Scout/ })).toBeVisible()

  // ── THE READ FAILED ───────────────────────────────────────────────
  // A number the page invented would be the worst answer here: the sweep's
  // whole point is not to send more scouts than the village has.
  await page.getByRole('button', { name: 'Check' }).click()
  await expect(page.getByText('API not available')).toBeVisible()
  await expect(page.getByText(/scouts idle in village/)).toHaveCount(0)

  // ── THE READ WORKED ───────────────────────────────────────────────
  // Romans (tribe 1) scout with t4, so 37 -- not the 900 legionnaires beside
  // it, which is the mistake a units-agnostic read would make.
  state.broken = false
  await page.getByRole('button', { name: 'Check' }).click()
  await expect(page.getByText('37 scouts idle in village')).toBeVisible()
  await expect(page.getByText('API not available')).toHaveCount(0)
})
