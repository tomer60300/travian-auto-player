/**
 * "Blank or 0 is no limit" — a sentence the wire stopped making true.
 *
 * `ExecuteRequest.max_game_rows_per_run` used to default to 0, which is
 * unbounded, so omitting the field WAS asking for no limit and the box could
 * say so. `456bf02` moved that default to 24 — deliberately: an unbounded
 * default on the one endpoint that writes is the opposite of what every other
 * control on this page does. The page was not moved with it. It still omitted
 * the field whenever the box was blank or 0, so:
 *
 *   box=""    ->  <omitted>  ->  server default 24
 *   box="0"   ->  <omitted>  ->  server default 24
 *   box="24"  ->  24
 *
 * There was no figure the operator could type that reached the server as 0,
 * while the box's own `Why` still read "Blank or 0 is no limit, which is what a
 * whole-day provisioning pass wants" and the placeholder read "no limit". The
 * whole-day pass the copy describes was unaskable: the run stopped after 24 rows
 * and reported the rest as deferred.
 *
 * So the field is sent EXPLICITLY, on both write paths — the controlled run and
 * the reconciliation sweep — and blank is the 0 the copy promises.
 *
 * NO BACKEND AND NO GAME REQUEST: every call is answered from a fixture or
 * aborted fail-closed. There is a live Travian account on this machine.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test rowBudget
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

const ROWS = 'Max rows this run'

/** One chunk's worth of sweep answer, so the loop finishes on the first pass:
 *  nothing outstanding and no pause asked for. */
const SWEPT = {
  ...PREVIEW,
  dry_run: false,
  swept_origins: [CAPITAL],
  unswept_origins: [],
  problems: [],
  next_chunk_wait_seconds: 0,
  remaining: 0,
}

/** Every `/execute` body this page sends, in order. */
async function captureExecutes(page) {
  const bodies = []
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    bodies.push(route.request().postDataJSON())
    await route.fulfill({ json: SWEPT })
    return 'handled'
  })
  await seed(page)
  await openPlan(page)
  return bodies
}

const rowBox = (page) => page.getByRole('spinbutton', { name: ROWS, exact: true })

test.describe('the row budget reaches the server as a figure, never as an omission', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the typed cap travels as itself', async ({ page }) => {
    const bodies = await captureExecutes(page)

    await expect(rowBox(page)).toHaveValue('24')
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()

    await expect.poll(() => bodies.length).toBe(1)
    expect(bodies[0].max_game_rows_per_run).toBe(24)
  })

  test('a blank box asks for no limit, and the wire says 0', async ({ page }) => {
    const bodies = await captureExecutes(page)

    await rowBox(page).fill('')
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()

    await expect.poll(() => bodies.length).toBe(1)
    // Present, and 0. Omitted would be the server's own 24 -- the cap the
    // operator has just cleared.
    expect(bodies[0]).toHaveProperty('max_game_rows_per_run', 0)
  })

  test('a typed 0 is the same 0, not the same omission', async ({ page }) => {
    const bodies = await captureExecutes(page)

    await rowBox(page).fill('0')
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()

    await expect.poll(() => bodies.length).toBe(1)
    expect(bodies[0]).toHaveProperty('max_game_rows_per_run', 0)
  })

  // The second write path, which omitted the field on every run that was not
  // whole-day and therefore took the server's 24 whenever it provisioned.
  test('the reconciliation sweep sends it too', async ({ page }) => {
    const bodies = await captureExecutes(page)

    await rowBox(page).fill('')
    await page.getByRole('button', { name: 'Reconcile all villages' }).click()

    await expect.poll(() => bodies.length).toBe(1)
    expect(bodies[0]).toHaveProperty('max_game_rows_per_run', 0)
    expect(bodies[0].reconcile_all_origins).toBe(true)
  })
})
