/**
 * The boxes whose only bound was the spinner.
 *
 * `min` and `max` on a number input bound the SPINNER and nothing else. A typed
 * or pasted figure sails straight past both: `aria-invalid` stays null, the
 * cell says nothing, and the button beside it posts the figure to be refused as
 * a server 422 -- a response that names a pydantic field path, arrives after a
 * round trip, and leads nobody back to the cell that caused it.
 *
 * Browser-confirmed before any of this went in: typing 21 into a Trade Office
 * box posted `trade_office_level: 21` with `aria-invalid = null`.
 *
 * Eight boxes, each on the button it actually reaches:
 *
 *   * Trade Office level ......... Build plan
 *   * Merchant base capacity ..... Build plan, and both Save writers
 *   * Crop stock alert ........... Run the full day
 *   * Foreign-target margin % .... Build plan
 *   * Emptied to % / Full to % ... Derive from stores
 *   * Routes this run ............ Preview / live run
 *   * Max rows this run .......... Preview / live run
 *   * Never disable .............. Preview / live run / the sweep
 *
 * The pattern copied throughout is the merchant-model row's: one predicate,
 * shared by the cell's message and by the gate, so a mark and a refusal cannot
 * come from two different rules.
 *
 * NO BACKEND AND NO GAME REQUEST. Every call is counted and aborted; the point
 * of each test is that the count stays at zero.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test plannerBounds
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, DEF_A, PLAN, isolate, seed } from './plannerHarness'

/** Count every planner call, answering only the ones a stage needs to render.
 *
 * `/plan` is answered because the run-panel tests need a plan on screen before
 * the boxes they are about exist at all; everything else falls through to the
 * harness's fail-closed abort, so a request that should not have been sent
 * shows up as a count rather than as a passing test.
 */
async function countCalls(page) {
  const calls = { plan: 0, dayCheck: 0, execute: 0, nightProfile: 0 }
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      calls.plan += 1
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    if (path.endsWith('/distribution/day-check')) {
      calls.dayCheck += 1
      await route.abort('blockedbyclient')
      return 'handled'
    }
    if (path.endsWith('/distribution/execute')) {
      calls.execute += 1
      await route.abort('blockedbyclient')
      return 'handled'
    }
    if (path.endsWith('/distribution/night-profile')) {
      calls.nightProfile += 1
      await route.abort('blockedbyclient')
      return 'handled'
    }
    return undefined
  })
  return calls
}

const WINDOWS = { planner_profile_windows: { Day: ['07:00', '23:00'] } }

async function openAccount(page, extra = {}) {
  await seed(page, extra)
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Allocation profile')).toBeVisible()
}

async function goToStage(page, label) {
  await page.getByRole('button', { name: label, exact: true }).click()
}

/** Replace a box's contents with a figure the spinner would never produce.
 *
 * By ROLE, not by `getByLabel`: three of the run-panel boxes sit inside a
 * `<label>` that also holds a `Why` disclosure named after the same field, so
 * "Never disable" resolves to two elements and a bare label query is a strict
 * mode violation. The box is the textbox or the spinbutton; the disclosure is
 * neither.
 */
function box(page, label, role = 'spinbutton') {
  return page.getByRole(role, { name: label, exact: true })
}

async function type(page, label, value, role = 'spinbutton') {
  const target = box(page, label, role)
  await target.fill(String(value))
  return target
}

/** Build a plan and go and look at it. Building deliberately does NOT move the
 *  stage -- the refusal that used to throw the operator off the table they were
 *  editing is what that rule exists to prevent -- so the click is explicit. */
async function buildAndOpenPlan(page) {
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

test.describe('a figure past the bound is marked and not sent', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('Trade Office level past 20', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)

    const cell = await type(page, 'Trade Office level for 11', 21)
    await expect(cell).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('a whole level from 0 to 20')).toBeVisible()

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/Trade Office \(11\)/)).toBeVisible()
    expect(calls.plan).toBe(0)
  })

  test('a Trade Office level the building actually reaches is left alone', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)

    const cell = await type(page, 'Trade Office level for 11', 20)
    await expect(cell).not.toHaveAttribute('aria-invalid', 'true')

    await buildAndOpenPlan(page)
    expect(calls.plan).toBe(1)
  })

  // The one the merchant row itself missed. `merchant_base_capacity` is an
  // `int` on every request that carries it, and this predicate was the only
  // integer lever's that did not say `Number.isInteger` -- so a fractional
  // calibration was marked nowhere, posted verbatim, and SAVED, leaving a
  // document `parseSetup` read back happily and every later plan refused over.
  test('a fractional merchant base capacity', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)

    const cell = await type(page, 'Merchant base capacity', 2500.5)
    await expect(cell).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('a whole number of units, more than 0')).toBeVisible()

    // The two writers that would have put it on disk and in the store, held
    // back by the same list.
    await expect(page.getByRole('button', { name: 'Save setup to server' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Save setup to file' })).toBeDisabled()

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/Base capacity — a whole number of units/)).toBeVisible()
    expect(calls.plan).toBe(0)
  })

  test('a whole calibration is left alone', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)

    const cell = await type(page, 'Merchant base capacity', 3200)
    await expect(cell).not.toHaveAttribute('aria-invalid', 'true')

    await buildAndOpenPlan(page)
    expect(calls.plan).toBe(1)
  })

  test('a negative crop stock alert', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await goToStage(page, 'Day & night')

    const cell = await type(page, 'Crop stock alert level for 11', -5)
    await expect(cell).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('0 or more')).toBeVisible()

    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect(page.getByText(/Crop stock alert \(11\)/)).toBeVisible()
    expect(calls.dayCheck).toBe(0)
  })

  test('a foreign-target safety margin past 100', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)

    await page.getByRole('button', { name: '+ Add target' }).click()
    await type(page, 'Foreign target 1 name', 'Ally', 'textbox')
    await type(page, 'Foreign target 1 crop per hour', 25700)
    const cell = await type(page, 'Foreign target 1 safety margin', 150)
    await expect(cell).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('0 to 100', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/Margin % \(Ally\)/)).toBeVisible()
    expect(calls.plan).toBe(0)
  })

  test('an emptied-to fill past 95%', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await goToStage(page, 'Day & night')

    const cell = await type(page, 'Emptied to %', 96)
    await expect(cell).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('0 to 95%')).toBeVisible()

    await page.getByRole('button', { name: /^Derive from stores/ }).click()
    await expect(page.getByText(/Emptied to %/)).toBeVisible()
    expect(calls.nightProfile).toBe(0)
  })

  // The PAIR rule, and the one bound no attribute could ever have carried: it
  // is a statement about both boxes at once. `_target_is_above_baseline` on the
  // server says the same thing -- equal fills leave no room for anything to
  // arrive in.
  test('a full-to fill that is not above the emptied-to one', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await goToStage(page, 'Day & night')

    await type(page, 'Emptied to %', 60)
    const cell = box(page, 'Full to %')
    await expect(cell).toHaveAttribute('aria-invalid', 'true')
    await expect(
      page.getByText('above the emptied-to figure, or the night has no room to fill')
    ).toBeVisible()

    await page.getByRole('button', { name: /^Derive from stores/ }).click()
    await expect(page.getByText(/Full to %/)).toBeVisible()
    expect(calls.nightProfile).toBe(0)
  })

  test('more than 50 routes in one run', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await buildAndOpenPlan(page)

    const cell = await type(page, 'Routes this run', 51)
    await expect(cell).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('0 to 50', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: /^Preview/ }).click()
    await expect(page.getByText(/Routes this run — 0 to 50/)).toBeVisible()
    expect(calls.execute).toBe(0)
  })

  // 0 is the backend's documented "reconcile only", and the box offers it. The
  // bound must not swallow it.
  test('0 routes still means 0', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await buildAndOpenPlan(page)

    const cell = await type(page, 'Routes this run', 0)
    await expect(cell).not.toHaveAttribute('aria-invalid', 'true')
    await page.getByRole('button', { name: /^Preview/ }).click()
    await expect.poll(() => calls.execute).toBe(1)
  })

  test('more than 2000 game rows in one run', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await buildAndOpenPlan(page)

    const cell = await type(page, 'Max rows this run', 2001)
    await expect(cell).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('0 to 2000', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: /^Preview/ }).click()
    await expect(page.getByText(/Max rows this run — 0 to 2000/)).toBeVisible()
    expect(calls.execute).toBe(0)
  })

  // The SHAPE, which is all the server can check -- and it 422s the WHOLE run
  // over it rather than dropping the entry, because a protection that protects
  // nothing while looking like it does is worse than none.
  test('a "Never disable" entry that is neither an id nor coordinates', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await buildAndOpenPlan(page)

    const entry = await type(page, 'Never disable', '53629, ally hub', 'textbox')
    await expect(entry).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('a village id, or coordinates like 46|133')).toBeVisible()

    await page.getByRole('button', { name: /^Preview/ }).click()
    await expect(page.getByText(/Never disable \(ally hub\)/)).toBeVisible()
    expect(calls.execute).toBe(0)
  })

  test('coordinates and ids are both accepted', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await buildAndOpenPlan(page)

    const entry = await type(page, 'Never disable', `${CAPITAL}, -46|133`, 'textbox')
    await expect(entry).not.toHaveAttribute('aria-invalid', 'true')
    await page.getByRole('button', { name: /^Preview/ }).click()
    await expect.poll(() => calls.execute).toBe(1)
  })

  // A SHAPE-valid entry ("4688" is a plausible village id) that names nothing
  // on this account: the server can only check the shape, so this miss is
  // knowable only here (`unresolvedProtectedEntries` in `villageRefs.js`). It
  // used to render visibly (a `<span>` under the box) with no `id` at all, so
  // a screen-reader user focused on the box heard nothing about it and
  // `aria-invalid` stayed absent -- the entry protects nothing, and the box
  // gave no indication.
  test('a "Never disable" entry that is shape-valid but names no village', async ({ page }) => {
    await countCalls(page)
    await openAccount(page, WINDOWS)
    await buildAndOpenPlan(page)

    const entry = await type(page, 'Never disable', '53629, 4688', 'textbox')
    await expect(entry).toHaveAttribute('aria-invalid', 'true')

    const described = await entry.getAttribute('aria-describedby')
    const text = (
      await Promise.all(described.split(' ').map((id) => page.locator(`#${id}`).innerText()))
    ).join(' ')
    expect(text).toContain('4688')
  })

  // The RECONCILIATION SWEEP, which was the only write path with no client gate
  // at all: `executePlan` opens with `[...blockers, ...runIssues]` -- for the
  // preview as well as the live run -- and this one checked only that a plan
  // existed and then posted `dry_run: false`. So every marked cell Preview
  // refuses went straight to a live, disabling run, on the one write button that
  // carries no live-run confirmation either.
  test('the sweep refuses a malformed "Never disable" the same way Preview does', async ({
    page,
  }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await buildAndOpenPlan(page)

    // Not a plan input, so the plan -- and the sweep button with it -- stays on
    // screen while the cell is marked. This is the state that was measured.
    await type(page, 'Never disable', '53629, ally hub', 'textbox')

    await page.getByRole('button', { name: 'Reconcile all villages' }).click()
    await expect(page.getByText(/Never disable \(ally hub\)/)).toBeVisible()
    expect(calls.execute).toBe(0)
  })

  // And the other list. The crop alert is the one `planBlockers` entry that is
  // not a plan input, so it can be wrong while a built plan is still on screen
  // -- which is exactly how a marked cell reached a live sweep.
  test('the sweep refuses a marked plan cell too', async ({ page }) => {
    const calls = await countCalls(page)
    await openAccount(page, WINDOWS)
    await buildAndOpenPlan(page)

    await goToStage(page, 'Day & night')
    await type(page, 'Crop stock alert level for 11', -5)
    await goToStage(page, 'Plan')

    await page.getByRole('button', { name: 'Reconcile all villages' }).click()
    await expect(page.getByText(/Crop stock alert \(11\)/)).toBeVisible()
    expect(calls.execute).toBe(0)
  })

  // The refusal is not only a sentence: it switches to the stage that mounts
  // the cell and drops the caret in it, which is what a 422 could never do.
  test('the refusal sends the caret to the cell that caused it', async ({ page }) => {
    await countCalls(page)
    await openAccount(page, WINDOWS)
    await type(page, 'Trade Office level for 11', 21)

    await goToStage(page, 'Day & night')
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()

    await expect(box(page, 'Trade Office level for 11')).toBeFocused()
    expect(DEF_A).toBe(20011)
  })
})
