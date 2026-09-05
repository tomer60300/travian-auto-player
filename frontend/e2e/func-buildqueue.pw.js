/**
 * Build Queue, driven the way it is used: pick buildings off the village, put
 * them in order, take one back out, and ask the server to check the plan.
 *
 * The queue is built entirely client-side and leaves the browser exactly once
 * before an execution -- as `yaml_content` on `POST /queue/validate`. That
 * string IS the operator's intent: the order of its entries is the build
 * order, `target` is how far each goes, and `slot` is which of the village's
 * identical resource fields is meant. A spec that only checks that a row
 * appeared in the right-hand panel checks none of that, which is why this one
 * asserts the generated YAML byte for byte after a reorder and a removal.
 *
 * Three tests, because a Playwright test costs ~7s before it asserts anything.
 *
 * A fourth test does open the execution socket, to cover the Execution Log --
 * which runs over the same kind of resumable-operation socket as Oasis Raider
 * and carried the identical dead `mountedRef` bug (`func-oasisraider.pw.js`
 * documents the regression): `BuildQueue`'s mount effect cleared the ref on
 * unmount but never set it back on mount, so under StrictMode's
 * mount-cleanup-remount the ref was stuck `false` from the first render and
 * `handleQueueMessage`'s guard dropped every frame the execution socket sent.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know, the two village reads are fixtures, and
 * the execution socket is never opened in the first three tests (the harness
 * closes sockets and none of them confirms the Execute dialog). There is a
 * live Travian account on this machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const BUILDINGS = [
  { slot_id: 1, name: 'Woodcutter', level: 3 },
  { slot_id: 19, name: 'Main Building', level: 5 },
  { slot_id: 20, name: 'Barracks', level: 2 },
]

const VALIDATION = {
  messages: ['2 step(s) planned, 0 already done'],
  items: [
    {
      building: 'Main Building',
      slot_id: 19,
      current_level: 5,
      target: 6,
      status: 'pending',
      is_construction: false,
    },
    {
      building: 'Barracks',
      slot_id: 20,
      current_level: 2,
      target: 3,
      status: 'skipped',
      is_construction: false,
    },
  ],
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

/** The queue panel, so its rows can be read in ORDER -- which is the whole
 *  point of the reorder buttons and the one thing a per-row lookup loses. */
function queueNames(page) {
  return page
    .locator('div.card')
    .filter({ hasText: /^Queue \(/ })
    .locator('span.text-gold.font-mono')
    .allTextContents()
}

test('a queue built, reordered and trimmed leaves as the YAML the operator sees', async ({
  page,
}) => {
  await isolateApp(page, {
    '/buildings': { village_id: CAPITAL, buildings: BUILDINGS },
    '/buildings/queue': { village_id: CAPITAL, queue: [] },
    '/queue/validate': VALIDATION,
  })
  const sent = await record(page)

  await page.goto('/queue')
  await expect(page.getByRole('heading', { name: 'Village Buildings' })).toBeVisible()
  // Nothing is queued yet, and the page says so rather than showing an empty
  // table that looks like a plan.
  await expect(page.getByText('Click a building to add it to the queue')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Validate' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Execute Queue' })).toBeDisabled()

  // ── ADD ───────────────────────────────────────────────────────────
  await page.getByRole('button', { name: 'Add Woodcutter (slot #1) to queue' }).click()
  await page.getByRole('button', { name: 'Add Main Building (slot #19) to queue' }).click()
  await page.getByRole('button', { name: 'Add Barracks (slot #20) to queue' }).click()
  await expect(page.getByRole('heading', { name: 'Queue (3 items)' })).toBeVisible()
  expect(await queueNames(page)).toEqual(['#1', '#19', '#20'])

  // ── REORDER ───────────────────────────────────────────────────────
  // Main Building before the Woodcutter: on a real account the Main Building
  // is what shortens every later build, so its position in this list is the
  // decision, not a cosmetic one.
  await page.getByRole('button', { name: 'Move Main Building (slot #19) earlier' }).click()
  expect(await queueNames(page)).toEqual(['#19', '#1', '#20'])

  // ── REMOVE ────────────────────────────────────────────────────────
  await page.getByRole('button', { name: 'Remove Woodcutter (slot #1) from queue' }).click()
  await expect(page.getByRole('heading', { name: 'Queue (2 items)' })).toBeVisible()
  expect(await queueNames(page)).toEqual(['#19', '#20'])

  // The preview the operator can open is the same string that will be sent.
  await page.getByRole('button', { name: 'Show generated YAML' }).click()
  const previewed = await page.locator('pre').first().textContent()

  // ── VALIDATE ──────────────────────────────────────────────────────
  await page.getByRole('button', { name: 'Validate' }).click()

  // 1. THE REQUEST carries the order, the targets, the priorities and the
  //    slots -- and the village the local selector is on, not the global one.
  const post = () => sent.find((s) => s.method === 'POST' && s.path.endsWith('/queue/validate'))
  await expect.poll(() => !!post()).toBe(true)
  expect(post().body.yaml_content).toBe(
    `village_id: ${CAPITAL}\n` +
      'plan:\n' +
      '  - building: "Main Building"\n    target: 6\n    priority: 1\n    slot: 19\n' +
      '  - building: "Barracks"\n    target: 3\n    priority: 1\n    slot: 20\n'
  )
  // What was shown and what was sent are the same string. A preview that can
  // drift from the payload is worse than no preview.
  expect(previewed).toBe(post().body.yaml_content)

  // 2. THE PAGE AFTERWARDS renders the SERVER's verdict, including the status
  //    it gave each step -- `skipped` is the server saying the build is
  //    already done, and the queue panel beside it cannot know that.
  await expect(page.getByRole('heading', { name: 'Validation Results' })).toBeVisible()
  await expect(page.getByText('2 step(s) planned, 0 already done')).toBeVisible()
  const barracks = page.getByRole('row', { name: /Barracks/ })
  await expect(barracks).toContainText('skipped')
  await expect(page.getByRole('row', { name: /Main Building/ })).toContainText('pending')
})

test('a refused validation says why and leaves no verdict table behind', async ({ page }) => {
  const REASON = 'plan step 1: Main Building is already level 20'
  await isolateApp(page, {
    '/buildings': { village_id: CAPITAL, buildings: BUILDINGS },
    '/buildings/queue': { village_id: CAPITAL, queue: [] },
    '/queue/validate': { status: 422, json: { detail: REASON } },
  })

  await page.goto('/queue')
  await page.getByRole('button', { name: 'Add Main Building (slot #19) to queue' }).click()
  await page.getByRole('button', { name: 'Validate' }).click()

  // 3. THE FAILURE BRANCH: the server's own sentence, in the error tone.
  await expect(toast(page)).toHaveClass(/toast-error/)
  await expect(toast(page)).toContainText(REASON)
  // And no table: a stale verdict under a refusal would read as an approval.
  await expect(page.getByRole('heading', { name: 'Validation Results' })).toHaveCount(0)
  // The queue itself is untouched -- a refused check is not a reason to lose
  // the operator's typing.
  await expect(page.getByRole('heading', { name: 'Queue (1 items)' })).toBeVisible()
})

test('a village that could not be read does not read as a village with nothing in it', async ({
  page,
}) => {
  let broken = true
  await isolateApp(page, { '/buildings/queue': { village_id: CAPITAL, queue: [] } })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.endsWith('/buildings')) return route.fallback()
    if (broken) {
      return route.fulfill({ status: 500, json: { detail: 'the game returned a login page' } })
    }
    return route.fulfill({ json: { village_id: CAPITAL, buildings: [] } })
  })

  await page.goto('/queue')

  // ── THE FAILED READ ───────────────────────────────────────────────
  const alert = page.getByRole('alert')
  await expect(alert).toContainText("Could not read this village's buildings")
  await expect(alert).toContainText('the game returned a login page')
  await expect(alert.getByRole('button', { name: 'Retry' })).toBeVisible()
  // "No buildings available. Connect to a server first." is both wrong -- we
  // ARE connected -- and identical to the empty state below.
  await expect(page.getByText('No buildings available. Connect to a server first.')).toHaveCount(0)

  // ── THE EMPTY VILLAGE, through the Retry the alert offers ─────────
  broken = false
  await alert.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByText('No buildings available. Connect to a server first.')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('execution reports every frame the operation sends', async ({ page }) => {
  const wsState = {}
  await isolateApp(page, {
    '/buildings': { village_id: CAPITAL, buildings: BUILDINGS },
    '/buildings/queue': { village_id: CAPITAL, queue: [] },
  })
  // AFTER `isolateApp`, whose blanket socket close would otherwise win.
  await page.routeWebSocket(/.*/, (ws) => {
    const path = new URL(ws.url()).pathname
    if (!path.endsWith('/ws/queue/run')) return ws.close()
    ws.onMessage((m) => {
      wsState.config = JSON.parse(String(m)).config
      ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-queue-run' }))
      ws.send(JSON.stringify({ type: 'step_complete', building: 'Woodcutter', level: 4, success: true }))
      ws.send(JSON.stringify({ type: 'error', message: 'the game returned a login page' }))
      ws.send(JSON.stringify({ type: 'complete' }))
    })
  })
  await page.addInitScript(() => localStorage.removeItem('resumableOp:queue'))

  await page.goto('/queue')
  await page.getByRole('button', { name: 'Add Woodcutter (slot #1) to queue' }).click()
  await page.getByRole('button', { name: 'Execute Queue' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Execute' }).click()

  await expect.poll(() => !!wsState.config).toBe(true)
  expect(wsState.config).toEqual({
    yaml_content: `village_id: ${CAPITAL}\nplan:\n  - building: "Woodcutter"\n    target: 4\n    priority: 1\n    slot: 1\n`,
    poll_interval: 30,
    use_video: true,
    verbose: false,
  })

  // Fails today: `BuildQueue.jsx`'s mount effect (lines 519-525) clears
  // `mountedRef` on unmount but never sets it back on mount, so under
  // StrictMode's mount-cleanup-remount the ref is stuck `false` from the
  // first render and `handleQueueMessage`'s guard drops every frame the
  // execution socket sends -- the Execution Log stays empty for the whole run.
  await expect(
    page.getByText('Session: e2e-queue-run (viewable from /sessions)')
  ).toBeVisible({ timeout: 3000 })
  await expect(page.getByText('Woodcutter -> Level 4: Done')).toBeVisible()
  await expect(page.getByText('the game returned a login page')).toBeVisible()
  await expect(page.getByText('Build queue completed!')).toBeVisible()
  // Execution finished: the Stop button is gone again.
  await expect(page.getByRole('button', { name: 'Stop' })).toHaveCount(0)
})
