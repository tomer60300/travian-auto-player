import { expect, test } from '@playwright/test'
import { CAPITAL, DEF_A, KEY, PREVIEW, isolate, openDayNight, openPlan, seed } from './plannerHarness'

const profiles = {
  planner_profiles: { Day: {}, Night: {} },
  planner_profile_windows: { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] },
  planner_npc_attended: { Day: true, Night: false },
  planner_trade_office: { [CAPITAL]: 13 },
}

test('an account switch discards an in-flight history response', async ({ page }) => {
  let release
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/run-history')) return undefined
    await new Promise((resolve) => { release = resolve })
    await route.fulfill({ json: { runs: [], rollup: {} } })
    return 'handled'
  })
  await seed(page, profiles)
  await openPlan(page)
  await page.getByText(/^Run history/).click()
  await expect.poll(() => typeof release).toBe('function')
  await page.evaluate(async () => {
    const { default: store } = await import('/src/stores/gameStore.js')
    store.setState({ playerName: 'another-account' })
  })
  const response = page.waitForResponse('**/distribution/run-history*')
  release()
  await response
  await expect(page.getByText('Reading traces…')).toHaveCount(0)
  await expect(page.getByText(/No live run has been recorded/)).toHaveCount(0)
})

test('an unchanged night derivation still updates its profile', async ({ page }) => {
  await isolate(page, (path) => path.endsWith('/distribution/night-profile')
    ? { allocations: { lumber: { [DEF_A]: { mode: 'absolute', value: 999 } } }, unmet: {} }
    : undefined)
  await seed(page, profiles)
  await openDayNight(page)
  await page.getByRole('button', { name: /^Derive from stores/ }).click()
  await expect.poll(() => page.evaluate(([key, id]) =>
    JSON.parse(localStorage.getItem(`planner_profiles::${key}`)).Day.lumber?.[id]?.value, [KEY, DEF_A])
  ).toBe(999)
})

test('a changed fill discards an in-flight night derivation', async ({ page }) => {
  let release
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/night-profile')) return undefined
    await new Promise((resolve) => { release = resolve })
    await route.fulfill({ json: { allocations: { lumber: { [DEF_A]: { mode: 'absolute', value: 999 } } }, unmet: {} } })
    return 'handled'
  })
  await seed(page, profiles)
  await openDayNight(page)
  await page.getByRole('button', { name: /^Derive from stores/ }).click()
  await expect.poll(() => typeof release).toBe('function')
  await page.getByLabel('Emptied to %').fill('30')
  release()
  await expect(page.getByRole('button', { name: /^Derive from stores/ })).toBeEnabled()
  const saved = await page.evaluate((key) => JSON.parse(localStorage.getItem(`planner_profiles::${key}`)), KEY)
  expect(saved.Day.lumber?.[DEF_A]?.value).not.toBe(999)
})

test('whole-day sweep discloses writes and preserves deferred creates when stopped', async ({ page }) => {
  let writes = 0
  await isolate(page, (path) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    writes += 1
    return { ...PREVIEW, dry_run: false, remaining: 2, swept_origins: [CAPITAL, DEF_A],
      unswept_origins: [], next_chunk_wait_seconds: 15, problems: [] }
  })
  await seed(page, profiles)
  await openPlan(page)
  await page.getByRole('checkbox', { name: 'Whole day — execute all profiles at once' }).check()
  await page.getByRole('button', { name: 'Reconcile all villages', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('create, disable, re-enable, update')
  expect(writes).toBe(0)
  await page.getByRole('button', { name: 'Start live reconciliation', exact: true }).click()
  await expect.poll(() => writes).toBe(1)
  await page.getByRole('button', { name: 'Stop after this chunk', exact: true }).click()
  await expect(page.getByText(/2 create\(s\) deferred/).first()).toBeVisible()
  await expect(page.getByText(/COMPLETE — nothing stale left/)).toHaveCount(0)
  expect(writes).toBe(1)
})

test('a sweep stops before its next chunk when the snapshot expires', async ({ page }) => {
  const start = new Date()
  await page.clock.setFixedTime(start)
  let writes = 0
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    writes += 1
    await page.clock.setFixedTime(new Date(start.getTime() + 31 * 60000))
    await route.fulfill({ json: { ...PREVIEW, dry_run: false, remaining: 0,
      swept_origins: [CAPITAL], unswept_origins: [DEF_A], next_chunk_wait_seconds: 1, problems: [] } })
    return 'handled'
  })
  await seed(page, profiles)
  await openPlan(page)
  await page.getByRole('button', { name: 'Reconcile all villages', exact: true }).click()
  await expect(page.getByText(/Snapshot is too old to write from/).first()).toBeVisible()
  expect(writes).toBe(1)
  await expect(page.getByText(/COMPLETE — nothing stale left/)).toHaveCount(0)
})

test('a sweep goes back for a village an earlier chunk deferred', async ({ page }) => {
  // The reproduction the stall guard used to fail on. Chunk 1 defers work at the
  // capital and leaves the other village unvisited; chunk 2 is narrowed to that
  // village and finishes it. The aggregate count reads 1 both times -- one
  // deferred village before, one after -- so the old guard called it a stall and
  // stopped without ever going back for the capital. They are two different
  // villages, not two failed attempts at the same work.
  const asked = []
  await isolate(page, (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    const body = JSON.parse(route.request().postData() || '{}')
    asked.push(body.only_origins ?? null)
    if (asked.length === 1) {
      return { ...PREVIEW, dry_run: false, remaining: 1, swept_origins: [CAPITAL],
        deferred_origins: [CAPITAL], unswept_origins: [DEF_A],
        next_chunk_wait_seconds: 1, problems: [] }
    }
    if (asked.length === 2) {
      // The unvisited village, finished. Nothing deferred here -- but the
      // capital still owes work, and only this side knows it.
      return { ...PREVIEW, dry_run: false, remaining: 0, swept_origins: [DEF_A],
        deferred_origins: [], unswept_origins: [], next_chunk_wait_seconds: 1, problems: [] }
    }
    return { ...PREVIEW, dry_run: false, remaining: 0, swept_origins: [CAPITAL],
      deferred_origins: [], unswept_origins: [], next_chunk_wait_seconds: null, problems: [] }
  })
  await seed(page, profiles)
  await openPlan(page)
  await page.getByRole('checkbox', { name: 'Whole day — execute all profiles at once' }).check()
  await page.getByRole('button', { name: 'Reconcile all villages', exact: true }).click()
  await page.getByRole('button', { name: 'Start live reconciliation', exact: true }).click()

  // Three chunks: everything, then the unvisited village, then back for the
  // capital. The old guard stopped after two.
  await expect.poll(() => asked.length, { timeout: 30_000 }).toBe(3)
  expect(asked[1]).toEqual([DEF_A])
  expect(asked[2]).toEqual([CAPITAL])
  await expect(page.getByText(/COMPLETE — nothing stale left/)).toBeVisible({ timeout: 30_000 })
})

test('a village capped at one route per chunk is provisioned over several chunks', async ({ page }) => {
  // "Still owed" is not "getting nowhere". The capital needs three routes and
  // the cap allows one per chunk, so it stays pending after chunk one and after
  // chunk two -- with a route created each time. The guard used to read that as
  // no progress and stop a chunk short.
  const asked = []
  await isolate(page, (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    const body = JSON.parse(route.request().postData() || '{}')
    asked.push(body.only_origins ?? null)
    const left = 3 - asked.length // 2, then 1, then 0
    return {
      ...PREVIEW, dry_run: false, created: 1, remaining: Math.max(0, left),
      swept_origins: [CAPITAL], deferred_origins: left > 0 ? [CAPITAL] : [],
      unswept_origins: [], next_chunk_wait_seconds: left > 0 ? 1 : null, problems: [],
    }
  })
  await seed(page, profiles)
  await openPlan(page)
  await page.getByRole('checkbox', { name: 'Whole day — execute all profiles at once' }).check()
  await page.getByRole('button', { name: 'Reconcile all villages', exact: true }).click()
  await page.getByRole('button', { name: 'Start live reconciliation', exact: true }).click()

  await expect.poll(() => asked.length, { timeout: 30_000 }).toBe(3)
  await expect(page.getByText(/COMPLETE — nothing stale left/)).toBeVisible({ timeout: 30_000 })
})

test('a sweep that writes nothing twice stops instead of looping', async ({ page }) => {
  // The other side of the guard. A blocked account -- Gold Club refused,
  // repeated failures -- defers the same work every pass and writes nothing.
  // Requiring "no mutation" as well as "still owed" must not cost this: without
  // it the sweep would ask for the same chunk for ever.
  let chunks = 0
  await isolate(page, (path) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    chunks += 1
    return { ...PREVIEW, dry_run: false, created: 0, remaining: 1,
      swept_origins: [CAPITAL], deferred_origins: [CAPITAL], unswept_origins: [],
      next_chunk_wait_seconds: 1, problems: [] }
  })
  await seed(page, profiles)
  await openPlan(page)
  await page.getByRole('checkbox', { name: 'Whole day — execute all profiles at once' }).check()
  await page.getByRole('button', { name: 'Reconcile all villages', exact: true }).click()
  await page.getByRole('button', { name: 'Start live reconciliation', exact: true }).click()

  await expect(page.getByText(/made no progress when asked again/).first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(/COMPLETE — nothing stale left/)).toHaveCount(0)
  // Two: the unfiltered opener, then the one that asked again and got nowhere.
  expect(chunks).toBe(2)
})

test('leaving the planner stops the sweep instead of writing on unseen', async ({ page }) => {
  // The loop lives in a closure, not in React, so unmounting the page used to
  // leave it requesting chunk after chunk -- writing to the game with the Stop
  // button no longer on screen.
  let chunks = 0
  await isolate(page, (path) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    chunks += 1
    return { ...PREVIEW, dry_run: false, created: 1, remaining: 5,
      swept_origins: [CAPITAL], deferred_origins: [CAPITAL], unswept_origins: [],
      next_chunk_wait_seconds: 2, problems: [] }
  })
  await seed(page, profiles)
  await openPlan(page)
  await page.getByRole('checkbox', { name: 'Whole day — execute all profiles at once' }).check()
  await page.getByRole('button', { name: 'Reconcile all villages', exact: true }).click()
  await page.getByRole('button', { name: 'Start live reconciliation', exact: true }).click()
  await expect.poll(() => chunks, { timeout: 20_000 }).toBe(1)

  // Navigate away while the sweep is holding between chunks.
  await page.getByRole('link', { name: 'Dashboard' }).click()
  const afterLeaving = chunks
  await page.waitForTimeout(6000)

  expect(chunks).toBe(afterLeaving)
})
