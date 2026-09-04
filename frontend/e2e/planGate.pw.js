/**
 * `Build plan` refuses the cells the page has already marked, DRIVEN.
 *
 * `plannerBlockers.test.js` covers the rule; what it cannot see is the button.
 * The reported defect is entirely about what leaves the page: typing 99 into
 * "Most merchants busy at once" on a village with fewer merchants renders
 * "only 19 merchants here" and sets `aria-invalid` -- and `Build plan` then
 * POSTed `max_busy_merchants: 99` anyway, so the refusal arrived as a server
 * 422 naming an internal village id. A unit test on the predicate cannot tell
 * a marked cell from a sent one; this spec counts the requests.
 *
 * So every case here asserts on the REQUEST COUNT to `/distribution/plan`
 * first, and on the toast and the caret second. A gate that shows the right
 * sentence and still posts is the bug, not the fix.
 *
 * NO BACKEND AND NO GAME REQUEST: `isolate` from the shared harness answers the
 * shell's two calls and aborts everything else fail-closed, and the snapshot is
 * seeded into localStorage rather than fetched.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test planGate
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, DEF_A, PLAN, isolate, seed } from './plannerHarness'

/** Isolation plus a counter on the one endpoint this spec is about. */
async function isolateCountingPlans(page) {
  const posts = []
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      posts.push(route.request().postDataJSON())
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    return undefined
  })
  return posts
}

const buildPlan = (page) => page.getByRole('button', { name: /^Build plan/ })

test.describe('the gate on Build plan', () => {
  test('a nothing-typed account still plans, so the gate cannot refuse a clean page', async ({
    page,
  }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')
    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
  })

  test('a merchant ceiling past the fleet is refused here, and nothing is sent', async ({
    page,
  }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')

    const cap = page.getByLabel('Most merchants busy at once for 11')
    await cap.fill('99')
    // The mark the page already made, which is what the gate is meant to read.
    await expect(cap).toHaveAttribute('aria-invalid', 'true')

    await buildPlan(page).click()
    await expect(
      page.getByText(/Most merchants busy at once \(11\)/),
    ).toBeVisible()
    await expect(page.getByText(/so nothing was sent/)).toBeVisible()
    // The whole point: no round trip, so no 422.
    expect(posts).toHaveLength(0)
    // And the caret is in the cell that caused it.
    await expect(cap).toBeFocused()
  })

  test('the refusal crosses back to the stage that holds the cell', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    // Typed already, so the operator is on another stage when they press it --
    // the case a DOM sweep for `aria-invalid` cannot see, because Targets does
    // not mount the Account table.
    await seed(page, { planner_max_busy: { [DEF_A]: 99 } })
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: 'Targets' }).click()
    await expect(page.getByText('Derive an idle-window profile from your stores')).toBeVisible()

    await buildPlan(page).click()
    expect(posts).toHaveLength(0)
    await expect(page.getByLabel('Most merchants busy at once for 11')).toBeFocused()
  })

  test('a fixed cell plans, so the gate is not a one-way door', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page, { planner_max_busy: { [DEF_A]: 99 } })
    await page.goto('/resource-planner')
    await buildPlan(page).click()
    expect(posts).toHaveLength(0)

    await page.getByLabel('Most merchants busy at once for 11').fill('12')
    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].config.find((row) => row.village_id === DEF_A).max_busy_merchants).toBe(12)
  })

  test('a stock floor past the ceiling is refused, naming the village', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')
    await page.getByLabel('NPC-backed stock floor for 02, percent of warehouse').fill('99')
    await buildPlan(page).click()
    expect(posts).toHaveLength(0)
    await expect(page.getByText(/Stock floor % \(02\)/)).toBeVisible()
  })

  // Marked NOWHERE before this: the backend refuses an even span outright and
  // the only sign of it was a 422 on the click.
  test('an even map span is refused, and the box says why', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')

    const span = page.getByLabel('Map span override')
    await span.fill('400')
    await expect(span).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('odd — a world is centred on 0|0')).toBeVisible()

    await buildPlan(page).click()
    expect(posts).toHaveLength(0)
    await expect(span).toBeFocused()

    await span.fill('401')
    await expect(span).not.toHaveAttribute('aria-invalid', 'true')
    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].map_span).toBe(401)
  })

  // The other unmarked one, and the one that disagreed with the file parser:
  // 25 was refused on import and accepted from a keystroke.
  test('a merchant reserve past 20 is refused at the parser bound', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')

    const reserve = page.getByLabel('Merchants held in reserve at every village')
    await reserve.fill('25')
    await expect(reserve).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('0 to 20 whole merchants')).toBeVisible()

    await buildPlan(page).click()
    expect(posts).toHaveLength(0)
    await expect(reserve).toBeFocused()
  })

  // Not a 422 at all: the unresolved name is DROPPED from `exclude_origins`, so
  // the run draws on a village the operator believes is excluded.
  test('an excluded origin no village answers to never reaches the request', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page, {
      planner_foreign_targets: [
        {
          name: 'ally',
          x: 12,
          y: 9,
          crop_per_hour: 2000,
          safety_margin_pct: 5,
          route_eligible: true,
        },
      ],
    })
    await page.goto('/resource-planner')

    const box = page.getByLabel('Foreign target 1 excluded origins')
    await box.fill('2')
    await expect(box).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('no village named 2')).toBeVisible()

    await buildPlan(page).click()
    expect(posts).toHaveLength(0)
    await expect(box).toBeFocused()

    // "02" is the village; "2" was the typo. Named right, it plans.
    await box.fill('02')
    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].foreign_targets[0].exclude_origins).toEqual([CAPITAL])
  })

  test('a role with no template is refused before the backend has to', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page, { planner_village_roles: { [DEF_A]: 'def' } })
    await page.goto('/resource-planner')
    await buildPlan(page).click()
    expect(posts).toHaveLength(0)
    await expect(page.getByText(/Role \(11\)/)).toBeVisible()
    await expect(page.getByLabel('Role for 11')).toBeFocused()
  })

  // Typed inside a collapsed disclosure, which is the only cell shape whose
  // control cannot be focused where it stands: React does not track
  // `<details open>`, so the jump has to open it.
  test('a negative spend is refused, and the picker holding it is opened', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page, { planner_consumption: { [DEF_A]: { lumber: -5 } } })
    await page.goto('/resource-planner')
    await buildPlan(page).click()
    expect(posts).toHaveLength(0)
    await expect(page.getByText(/Consumption \/h \(11\)/)).toBeVisible()
    await expect(page.getByLabel('Lumber spent per hour by 11')).toBeFocused()
  })
})
