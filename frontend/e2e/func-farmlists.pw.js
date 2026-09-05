/**
 * FarmLists, driven as an operator drives it: create a list, add a target,
 * delete a target, delete the list, and copy a selection into a list that
 * refuses part of it.
 *
 * Three questions per flow, and the middle one is the one a name-pinning spec
 * cannot ask:
 *   1. did the app send the request the user asked for -- method, path, and
 *      the body fields that carry the intent;
 *   2. does the page afterwards say what the SERVER said, not what the click
 *      hoped;
 *   3. when the endpoint refuses, does the page say so, and does it stop
 *      short of reporting success.
 *
 * `farmMoveCap.pw.js` already owns MOVE with a partial refusal (the 2026-09-04
 * regression). This file owns its sibling, COPY, which shares `handleTransfer`
 * and its `addFail` counter but not its toast.
 *
 * Four tests, not twelve. A Playwright test costs ~7s in context setup and
 * first paint before it asserts anything, so a flow per test and a reload
 * where the fixture has to change buys the same coverage inside the suite's
 * time budget.
 *
 * A fifth test covers Loop Send Mode, which runs over the same kind of
 * resumable-operation socket as Oasis Raider and carried the identical dead
 * `mountedRef` bug (`func-oasisraider.pw.js` documents the regression):
 * `FarmLists`'s mount effect returned only a cleanup, so under StrictMode's
 * mount-cleanup-remount the ref was stuck `false` from the first render and
 * `handleOpMessage`'s guard dropped every frame the loop's socket sent.
 *
 * NO BACKEND AND NO GAME REQUEST. `appHarness.isolateApp` answers the shell
 * and ABORTS every path it does not know; the fixtures below are registered
 * AFTER it and `route.fallback()` anything they do not recognise, so the
 * fail-closed default is never widened. There is a live Travian account on
 * this machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const SOURCE_ID = 1
const DEST_ID = 2

function listRow(id, name, slots) {
  return { id, name, slots_amount: slots, active_slots: slots, total_booty: 0 }
}

function slot(id, n) {
  return {
    id,
    x: n,
    y: 0,
    name: `Target ${n}`,
    population: 100,
    distance: 1.5,
    is_active: true,
    troops: {},
    total_booty: 0,
    total_raids: 0,
    last_raid: null,
  }
}

/** Records every /api call, then hands it back to whatever answers it.
 *
 * Registered AFTER the answering handler on purpose: Playwright matches the
 * most recently registered route first, so this sees the request, and
 * `route.fallback()` lets the fixture below (or the harness's abort) decide
 * the response. */
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

/** A live model of the account's farm lists, so a refetch shows what the
 *  mutations actually did rather than a frozen fixture. */
function model({ lists, slots }) {
  return {
    lists: new Map(lists.map((l) => [l.id, l])),
    slots: new Map(slots.map(([listId, ids]) => [listId, new Set(ids)])),
  }
}

/** Answers the farm endpoints out of `state`; everything else falls through
 *  to the harness (which aborts it). `hooks` is read on every call, so a test
 *  can turn a refusal on part-way through. */
async function serveFarm(page, state, hooks = {}) {
  await page.route('**/api/**', async (route) => {
    const req = route.request()
    const method = req.method()
    const path = new URL(req.url()).pathname

    if (method === 'GET' && path.endsWith('/farm/lists')) {
      if (hooks.listsStatus) {
        return route.fulfill({ status: hooks.listsStatus, json: { detail: hooks.listsDetail } })
      }
      const rows = [...state.lists.values()].map((l) =>
        listRow(l.id, l.name, state.slots.get(l.id)?.size ?? 0)
      )
      return route.fulfill({ json: rows })
    }

    const detailMatch = path.match(/\/farm\/lists\/(\d+)$/)
    if (method === 'GET' && detailMatch) {
      if (hooks.detailStatus) {
        return route.fulfill({ status: hooks.detailStatus, json: { detail: hooks.detailDetail } })
      }
      const id = Number(detailMatch[1])
      const list = state.lists.get(id)
      if (!list) return route.fulfill({ status: 404, json: { detail: 'no such list' } })
      const ids = [...(state.slots.get(id) ?? [])].sort((a, b) => a - b)
      return route.fulfill({
        json: { id, name: list.name, slots: ids.map((s) => slot(s, s - 500)) },
      })
    }
    if (method === 'DELETE' && detailMatch) {
      if (hooks.refuseDeleteList) {
        return route.fulfill({ status: 409, json: { detail: hooks.refuseDeleteList } })
      }
      const id = Number(detailMatch[1])
      state.lists.delete(id)
      state.slots.delete(id)
      return route.fulfill({ status: 204 })
    }

    if (method === 'POST' && path.endsWith('/farm/lists')) {
      if (hooks.refuseCreate) {
        return route.fulfill({ status: 409, json: { detail: hooks.refuseCreate } })
      }
      const id = Math.max(0, ...state.lists.keys()) + 1
      state.lists.set(id, { id, name: req.postDataJSON().name })
      state.slots.set(id, new Set())
      return route.fulfill({ status: 201, json: { id } })
    }

    const addMatch = path.match(/\/farm\/lists\/(\d+)\/targets$/)
    if (method === 'POST' && addMatch) {
      const id = Number(addMatch[1])
      const verdict = hooks.onAdd?.(id)
      if (verdict) return route.fulfill(verdict)
      const next = Math.max(500, ...(state.slots.get(id) ?? [500])) + 1
      state.slots.get(id).add(next)
      return route.fulfill({ status: 201, json: { list_id: id, x: 0, y: 0 } })
    }

    const delMatch = path.match(/\/farm\/lists\/(\d+)\/targets\/(\d+)$/)
    if (method === 'DELETE' && delMatch) {
      state.slots.get(Number(delMatch[1]))?.delete(Number(delMatch[2]))
      return route.fulfill({ status: 204 })
    }

    return route.fallback()
  })
}

/** The FIRST toast, as an element, so its TONE can be asserted.
 *
 * `toHaveCount(0)` on `.toast-success` is not a safe way to say "not a
 * success": toasts self-dismiss after 4s and the assertion retries for 5s, so
 * it passes on a page that DID celebrate. Asserting the class of the toast
 * that is actually up fails fast and for the right reason. */
function toast(page) {
  return page.locator('.toast').first()
}

test('one operator session: create a list, add a target, delete it, delete the list', async ({
  page,
}) => {
  const state = model({ lists: [{ id: SOURCE_ID, name: 'Source' }], slots: [[SOURCE_ID, [501]]] })
  await isolateApp(page)
  await serveFarm(page, state)
  const sent = await record(page)

  await page.goto('/farm')
  await expect(page.getByRole('cell', { name: 'Source', exact: true })).toBeVisible()

  // ── CREATE ────────────────────────────────────────────────────────
  // The village selector defaults to the active village once the status read
  // lands; typing before it does would send `village_id: undefined`.
  await expect(page.locator('#new-list-village')).toHaveValue(String(CAPITAL))
  await page.getByPlaceholder('New farm list').fill('Night raids')
  await page.getByRole('button', { name: 'Create List' }).click()

  // 1. THE REQUEST: the typed name AND the chosen village. A list created
  //    against the wrong village raids from the wrong rally point, which no
  //    label on this page would reveal.
  const post = () => sent.find((s) => s.method === 'POST' && s.path.endsWith('/farm/lists'))
  await expect.poll(() => !!post()).toBe(true)
  expect(post().body).toEqual({ name: 'Night raids', village_id: CAPITAL })

  // 2. THE PAGE AFTERWARDS shows the server's list, not the typed string: the
  //    row is there because the refetch returned it. And the field is cleared,
  //    so a second Enter cannot create a duplicate.
  await expect(page.getByRole('cell', { name: 'Night raids', exact: true })).toBeVisible()
  await expect(toast(page)).toHaveClass(/toast-success/)
  await expect(page.getByPlaceholder('New farm list')).toHaveValue('')

  // ── ADD A TARGET ──────────────────────────────────────────────────
  await page.getByRole('cell', { name: 'Source', exact: true }).click()
  await expect(page.getByText('Target 1', { exact: true })).toBeVisible()
  await page.getByLabel('X', { exact: true }).fill('-37')
  await page.getByLabel('Y', { exact: true }).fill('142')
  await page.getByRole('checkbox', { name: 'Force' }).check()
  await page.getByRole('button', { name: 'Add Target' }).click()

  const add = () => sent.find((s) => s.method === 'POST' && /\/targets$/.test(s.path))
  await expect.poll(() => !!add()).toBe(true)
  expect(add().path).toBe(`/api/farm/lists/${SOURCE_ID}/targets`)
  // `force` is the difference between "add it" and "add it even though the
  // game says no", so it must travel as the operator set it.
  expect(add().body).toEqual({ x: -37, y: 142, force: true })
  await expect(page.getByText('Target 2', { exact: true })).toBeVisible()
  await expect(page.getByLabel('X', { exact: true })).toHaveValue('')

  // ── DELETE THAT TARGET ────────────────────────────────────────────
  await page.getByRole('button', { name: 'Delete target (2, 0)' }).click()
  // Asked before anything destructive leaves the browser.
  await expect(page.getByRole('dialog')).toContainText('remove this target from the farm list')
  expect(sent.filter((s) => s.method === 'DELETE')).toHaveLength(0)
  await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click()

  await expect
    .poll(() => sent.filter((s) => s.method === 'DELETE').map((s) => s.path))
    .toEqual([`/api/farm/lists/${SOURCE_ID}/targets/502`])
  await expect(page.getByText('Target 2', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Target 1', { exact: true })).toBeVisible()

  // ── DELETE THE LIST ───────────────────────────────────────────────
  await page
    .getByRole('row', { name: /Night raids/ })
    .getByRole('button', { name: 'Delete' })
    .click()
  // The dialog names the list, so the operator can see they picked the right
  // one before the call goes.
  await expect(page.getByRole('dialog')).toContainText(
    'Are you sure you want to delete "Night raids"?'
  )
  await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click()

  await expect
    .poll(() => sent.filter((s) => s.method === 'DELETE' && /lists\/\d+$/.test(s.path)).length)
    .toBe(1)
  await expect(page.getByRole('cell', { name: 'Night raids', exact: true })).toHaveCount(0)
  await expect(page.getByRole('cell', { name: 'Source', exact: true })).toBeVisible()
})

test('every refusal is named, and nothing that was refused appears to have happened', async ({
  page,
}) => {
  const CREATE_REASON = 'the account already has 25 farm lists'
  const ADD_REASON = 'errorRaidListSlotLimit'
  const DELETE_REASON = 'a farm run is using this list'
  const state = model({ lists: [{ id: SOURCE_ID, name: 'Source' }], slots: [[SOURCE_ID, [501]]] })
  const hooks = { refuseCreate: CREATE_REASON }
  await isolateApp(page)
  await serveFarm(page, state, hooks)
  await record(page)

  await page.goto('/farm')
  await expect(page.locator('#new-list-village')).toHaveValue(String(CAPITAL))

  // ── A REFUSED CREATE ──────────────────────────────────────────────
  await page.getByPlaceholder('New farm list').fill('Night raids')
  await page.getByRole('button', { name: 'Create List' }).click()
  await expect(toast(page)).toHaveClass(/toast-error/)
  await expect(toast(page)).toContainText(CREATE_REASON)
  await expect(page.getByRole('cell', { name: 'Night raids', exact: true })).toHaveCount(0)

  // ── A REFUSED ADD ─────────────────────────────────────────────────
  hooks.refuseCreate = null
  hooks.onAdd = () => ({ status: 502, json: { detail: ADD_REASON } })
  await page.getByRole('cell', { name: 'Source', exact: true }).click()
  await expect(page.getByText('Target 1', { exact: true })).toBeVisible()
  await page.getByLabel('X', { exact: true }).fill('4')
  await page.getByLabel('Y', { exact: true }).fill('4')
  await page.getByRole('button', { name: 'Add Target' }).click()
  await expect(toast(page)).toHaveClass(/toast-error/)
  await expect(toast(page)).toContainText(ADD_REASON)
  await expect(page.getByText('Target 2', { exact: true })).toHaveCount(0)

  // ── A REFUSED DELETE ──────────────────────────────────────────────
  hooks.refuseDeleteList = DELETE_REASON
  await page
    .getByRole('row', { name: /Source/ })
    .getByRole('button', { name: 'Delete' })
    .click()
  await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click()
  await expect(toast(page)).toHaveClass(/toast-error/)
  await expect(toast(page)).toContainText(DELETE_REASON)
  // Still there. A list the server would not delete must not vanish from the
  // table, or the operator believes it is gone and stops raiding with it.
  await expect(page.getByRole('cell', { name: 'Source', exact: true })).toBeVisible()
})

test('a copy the destination partly refused keeps the source and does not celebrate', async ({
  page,
}) => {
  const ACCEPTS = 2
  const SELECTED = 6
  const state = model({
    lists: [
      { id: SOURCE_ID, name: 'Source' },
      { id: DEST_ID, name: 'Dest' },
    ],
    slots: [
      [SOURCE_ID, Array.from({ length: SELECTED }, (_, i) => 501 + i)],
      [DEST_ID, []],
    ],
  })
  let attempts = 0
  await isolateApp(page)
  await serveFarm(page, state, {
    onAdd: (id) => {
      if (id !== DEST_ID) return null
      attempts += 1
      return attempts <= ACCEPTS ? null : { status: 502, json: { detail: 'errorRaidListSlotLimit' } }
    },
  })
  const sent = await record(page)

  await page.goto('/farm')
  await page.getByRole('cell', { name: 'Source', exact: true }).click()
  await expect(page.getByText('Target 1', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: /^Select All/ }).click()
  await expect(page.getByText(`${SELECTED} selected`)).toBeVisible()
  await page
    .locator('select')
    .filter({ has: page.locator('option', { hasText: '-- Destination --' }) })
    .selectOption({ label: 'Dest' })
  await page.getByRole('button', { name: `Copy ${SELECTED}` }).click()

  await expect.poll(() => sent.filter((s) => s.method === 'POST').length).toBe(SELECTED)
  await expect(toast(page)).toBeVisible()

  // Copy means copy. Only the MOVE branch may delete, and only what the
  // destination confirmed (farmMoveCap.pw.js pins that half).
  expect(sent.filter((s) => s.method === 'DELETE')).toHaveLength(0)
  for (const n of [1, 3, 6]) {
    await expect(page.getByText(`Target ${n}`, { exact: true })).toBeVisible()
  }

  // `handleTransfer` (src/pages/FarmLists.jsx) ends the copy branch with an
  // unconditional `toast.success("Copied N target(s) ... (M failed)")`, so a
  // destination that refused four of six announces itself in the SUCCESS tone
  // with the refusal reason dropped entirely. The move branch beside it gets
  // this right -- it switches to `toast.warning` and names the reason -- and
  // both branches read the same `addFail` / `addFailReason` counters.
  //
  // What the copy branch should do, matching its sibling: warn, name both
  // counts, and carry the destination's own sentence.
  await expect(toast(page)).toHaveClass(/toast-warning/, { timeout: 2000 })
  await expect(toast(page)).toContainText('errorRaidListSlotLimit', { timeout: 2000 })
})

test('a failed read and an empty account do not look alike', async ({ page }) => {
  const state = model({ lists: [{ id: SOURCE_ID, name: 'Source' }], slots: [[SOURCE_ID, [501]]] })
  const hooks = { listsStatus: 500, listsDetail: 'upstream is down' }
  await isolateApp(page)
  await serveFarm(page, state, hooks)

  // ── THE FAILED READ ───────────────────────────────────────────────
  await page.goto('/farm')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Could not read your farm lists')
  await expect(alert).toContainText('upstream is down')
  await expect(alert.getByRole('button', { name: 'Retry' })).toBeVisible()
  // The sentence that invites the operator to rebuild lists they may already
  // have must not be on screen at the same time.
  await expect(page.getByText('No farm lists found. Create one above.')).toHaveCount(0)

  // ── THE EMPTY ACCOUNT, through the same Retry the alert offers ─────
  hooks.listsStatus = null
  state.lists.clear()
  await alert.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByText('No farm lists found. Create one above.')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  // And "Send All Lists" is not offered for nothing.
  await expect(page.getByRole('button', { name: 'Send All Lists' })).toHaveCount(0)

  // ── A LIST WHOSE DETAIL READ FAILS ────────────────────────────────
  state.lists.set(SOURCE_ID, { id: SOURCE_ID, name: 'Source' })
  hooks.detailStatus = 500
  hooks.detailDetail = 'the game timed out'
  await page.reload()
  await page.getByRole('cell', { name: 'Source', exact: true }).click()
  const detailAlert = page.getByRole('alert')
  await expect(detailAlert).toContainText('Could not read this farm list')
  await expect(detailAlert).toContainText('the game timed out')
  // "No targets in this list." is a claim about the account that the failed
  // read did not establish -- the overview row beside it says one slot.
  await expect(page.getByText('No targets in this list.')).toHaveCount(0)
})

test('loop mode reports every frame the operation sends', async ({ page }) => {
  const state = model({ lists: [{ id: SOURCE_ID, name: 'Source' }], slots: [[SOURCE_ID, [501]]] })
  const wsState = {}
  await isolateApp(page)
  await serveFarm(page, state)
  // AFTER `isolateApp`, whose blanket socket close would otherwise win.
  await page.routeWebSocket(/.*/, (ws) => {
    const path = new URL(ws.url()).pathname
    if (!path.endsWith('/ws/farm/run-all')) return ws.close()
    ws.onMessage((m) => {
      wsState.config = JSON.parse(String(m)).config
      ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-farm-loop' }))
      ws.send(JSON.stringify({ type: 'cycle_start', cycle: 1 }))
      ws.send(JSON.stringify({ type: 'error', message: 'no idle troops in this village' }))
      ws.send(JSON.stringify({ type: 'complete', total_cycles: 1, total_success: 0, total_fail: 1 }))
    })
  })
  await page.addInitScript(() => localStorage.removeItem('resumableOp:farm-all'))

  await page.goto('/farm')
  await expect(page.getByRole('cell', { name: 'Source', exact: true })).toBeVisible()
  await page.getByRole('checkbox', { name: 'Source' }).check()
  await page.getByRole('button', { name: 'Start Loop' }).click()

  await expect.poll(() => !!wsState.config).toBe(true)

  // Fails today: `FarmLists.jsx`'s mount effect (line 159) is cleanup-only, so
  // under StrictMode's mount-cleanup-remount `mountedRef.current` is stuck
  // `false` from the first render and `handleOpMessage`'s guard drops every
  // frame the loop socket sends -- this panel stays on "No messages yet..."
  // for the whole loop.
  await expect(
    page.getByText('Session: e2e-farm-loop (viewable from /sessions)')
  ).toBeVisible({ timeout: 3000 })
  await expect(page.getByText('Cycle 1 started')).toBeVisible()
  await expect(page.getByText('no idle troops in this village')).toBeVisible()
  await expect(page.getByText(/Completed after 1 cycle\(s\)/)).toBeVisible()
  await expect(toast(page)).toHaveClass(/toast-success/)
  await expect(page.getByRole('button', { name: 'Start Loop' })).toBeVisible()
})
