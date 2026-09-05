/**
 * Sessions is the one page an operator opens to ask "is anything running on
 * this account right now, and can I stop it". Both halves of that are
 * destructive-adjacent: a wrong "nothing is running" leaves a sweep hitting a
 * live game, and a Stop that quietly did nothing is worse than a Stop that
 * says it failed.
 *
 * So this file drives the two stop paths for real -- the account-wide
 * `POST /sessions/stop-all` and the per-session `POST /sessions/{id}/stop`,
 * including its documented WebSocket fallback -- and pins that a failed poll
 * never renders as an idle machine.
 *
 * The session viewer needs a socket that TALKS, so this spec registers its own
 * `routeWebSocket` AFTER `isolateApp` (which closes every socket); Playwright
 * matches the most recently registered route first, so the order is load
 * bearing.
 *
 * NO BACKEND AND NO GAME REQUEST. There is a live Travian account on this
 * machine.
 */

import { expect, test } from '@playwright/test'

import { isolateApp } from './appHarness'

const RUNNING = {
  id: 'sess-run-1',
  session_type: 'farm-run-all',
  label: 'Farm run — all lists',
  status: 'running',
  created_at: Math.floor(Date.now() / 1000) - 90,
  message_count: 42,
}

const DONE = {
  id: 'sess-done-1',
  session_type: 'queue',
  label: 'Build queue — 02',
  status: 'disconnected',
  created_at: Math.floor(Date.now() / 1000) - 7200,
  message_count: 8,
}

async function record(page) {
  const sent = []
  await page.route('**/api/**', async (route) => {
    const req = route.request()
    let body = null
    try {
      body = req.postDataJSON()
    } catch {
      body = req.postData() ?? null
    }
    sent.push({ method: req.method(), path: new URL(req.url()).pathname, body })
    await route.fallback()
  })
  return sent
}

function toast(page) {
  return page.locator('.toast').first()
}

test('the list separates running from finished, and Stop All signals every running op', async ({
  page,
}) => {
  const state = { sessions: [RUNNING, DONE] }
  await isolateApp(page)
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/sessions')) return route.fulfill({ json: state.sessions })
    if (path.endsWith('/sessions/stop-all')) {
      state.sessions = state.sessions.map((s) => ({ ...s, status: 'disconnected' }))
      return route.fulfill({ json: { stopped: 1 } })
    }
    return route.fallback()
  })
  const sent = await record(page)

  await page.goto('/sessions')

  // 2. THE PAGE reflects the server's own `status` field, in two headed
  //    groups: a finished op under "Running" would be read as live.
  await expect(page.getByRole('heading', { name: 'Running (1)' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Completed (1)' })).toBeVisible()
  await expect(page.getByText('Farm run — all lists')).toBeVisible()
  // Only the finished one offers a Rerun; rerunning a live op would double it.
  await expect(page.getByRole('button', { name: 'Rerun Build queue — 02' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Rerun Farm run — all lists' })).toHaveCount(0)

  // 1. THE REQUEST. One running op, so no browser confirm() stands in the way
  //    (the guard is for a fan-out over several).
  await page.getByRole('button', { name: 'Stop All (1)' }).click()
  const post = () => sent.find((s) => s.method === 'POST' && s.path.endsWith('/sessions/stop-all'))
  await expect.poll(() => !!post()).toBe(true)
  await expect(toast(page)).toHaveClass(/toast-success/)
  await expect(toast(page)).toContainText('Stop signal sent to 1 operation')

  // And the page then shows what the server says afterwards, not what the
  // click hoped: the next poll moves it into Completed and the button goes.
  await expect(page.getByRole('heading', { name: 'Completed (2)' })).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole('button', { name: /^Stop All/ })).toHaveCount(0)
})

test('stopping one session uses the REST path, and says so when it falls back to the socket', async ({
  page,
}) => {
  let restBroken = true
  const wsFrames = []
  await isolateApp(page)
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/sessions')) return route.fulfill({ json: [RUNNING] })
    if (path.endsWith(`/sessions/${RUNNING.id}/stop`)) {
      if (restBroken) return route.fulfill({ status: 503, json: { detail: 'operation manager down' } })
      return route.fulfill({ json: { stopped: true } })
    }
    return route.fallback()
  })
  const sent = await record(page)

  // Registered AFTER `isolateApp`, whose blanket socket close would otherwise
  // win. The viewer's socket sends nothing on open, so the meta and history
  // frames are pushed unprompted, exactly as the server pushes them.
  await page.routeWebSocket(/.*/, (ws) => {
    const path = new URL(ws.url()).pathname
    if (!path.endsWith(`/ws/sessions/${RUNNING.id}/stream`)) return ws.close()
    ws.onMessage((m) => wsFrames.push(String(m)))
    ws.send(
      JSON.stringify({
        type: 'session_meta',
        id: RUNNING.id,
        label: RUNNING.label,
        session_type: RUNNING.session_type,
        status: 'running',
        created_at: RUNNING.created_at,
      })
    )
    ws.send(
      JSON.stringify({
        type: 'history',
        messages: [{ type: 'status', message: 'Cycle 3 started', ts: RUNNING.created_at }],
      })
    )
  })

  await page.goto('/sessions')
  await page.getByRole('button', { name: `View logs for ${RUNNING.label}` }).click()

  // 2. THE VIEWER renders what the socket said: the label from `session_meta`,
  //    the replayed history, and a Live badge because the meta says running.
  await expect(page.getByRole('heading', { name: RUNNING.label })).toBeVisible()
  await expect(page.getByText('Live', { exact: true })).toBeVisible()
  await expect(page.getByText('Cycle 3 started')).toBeVisible()

  // ── THE FALLBACK ──────────────────────────────────────────────────
  // With the REST endpoint down, the stop must still reach the op over the
  // channel that is already open -- and the toast must say which path carried
  // it, because the two are not equally reliable.
  await page.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(toast(page)).toContainText('Stop signal sent (via WebSocket)')
  expect(wsFrames.map((f) => JSON.parse(f))).toContainEqual({ action: 'stop' })

  // ── THE ORDINARY PATH ─────────────────────────────────────────────
  restBroken = false
  await page.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect
    .poll(() => sent.filter((s) => s.method === 'POST' && s.path.endsWith('/stop')).length)
    .toBe(2)
  await expect(toast(page)).toContainText('Stop signal sent')
})

test('a poll that failed does not claim the machine is idle', async ({ page }) => {
  const state = { broken: true }
  await isolateApp(page)
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.endsWith('/sessions')) return route.fallback()
    if (state.broken) return route.fulfill({ status: 500, json: { detail: 'database is locked' } })
    return route.fulfill({ json: [] })
  })

  await page.goto('/sessions')

  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Could not read the session list')
  await expect(alert).toContainText('database is locked')
  // The empty sentence is a claim about the machine that the failed poll did
  // not establish, and this is the one page where believing it is expensive.
  await expect(page.getByText('No active or recent sessions')).toHaveCount(0)

  // The genuinely empty answer, through the same five-second poll.
  state.broken = false
  await expect(page.getByText('No active or recent sessions')).toBeVisible({ timeout: 12_000 })
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^Stop All/ })).toHaveCount(0)
})
