/**
 * The Rest radio, DRIVEN in both directions.
 *
 * "No remainder village" is the initial state of every account and a state the
 * Plan stage reports on ("no remainder village set") -- and it was UNREACHABLE
 * once left. There was no `none` option, no clear, the page's setter only ever
 * SET, and clicking the already-checked radio fires no change event at all. So
 * the first Rest tick was a one-way door, and no unit test could see it: the
 * function was reachable, the widget was not.
 *
 * Worse, the row that stopped absorbing the slack then read `Keep own` in its
 * Mode select -- which is not what the plan does with a Rest village, and on a
 * village with a role it is a silent override of the role's own figure.
 *
 * So this spec asserts on the REQUEST BODY either side of the round trip, plus
 * on what the Mode select says while the row is the remainder.
 *
 * NO BACKEND AND NO GAME REQUEST: `isolate` answers the shell's calls and
 * aborts everything else fail-closed.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test restRadio
 */

import { expect, test } from '@playwright/test'

import { DEF_A, PLAN, isolate, seed } from './plannerHarness'

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

/** The Targets stage, on the view that can actually set a target. */
async function openEditByResource(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Targets' }).click()
  await page.getByRole('button', { name: 'Edit by resource' }).click()
  await expect(page.getByLabel('Send remaining Lumber to 11')).toBeVisible()
}

test.describe('the Rest radio', () => {
  test('sets a remainder village, and the request says so', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await openEditByResource(page)

    await page.getByLabel('Send remaining Lumber to 11').check()
    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].allocations.lumber[String(DEF_A)]).toEqual({ mode: 'remainder', value: 0 })
  })

  test('has a none option, so no remainder village is reachable again', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await openEditByResource(page)

    await page.getByLabel('Send remaining Lumber to 11').check()
    const none = page.getByLabel(/no village absorbs the remaining Lumber/)
    await expect(none).not.toBeChecked()
    await none.check()
    await expect(page.getByLabel('Send remaining Lumber to 11')).not.toBeChecked()

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    // Not "keep" written over it, and not present at all: absent is the resting
    // state, which is what hands a role village back its template's figure.
    expect(posts[0].allocations.lumber).toBeUndefined()
  })

  test('none is checked on a fresh account, because that IS the initial state', async ({
    page,
  }) => {
    await isolateCountingPlans(page)
    await seed(page)
    await openEditByResource(page)
    await expect(page.getByLabel(/no village absorbs the remaining Lumber/)).toBeChecked()
  })

  test('each resource has its own none, and one does not clear another', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await openEditByResource(page)

    await page.getByLabel('Send remaining Lumber to 11').check()
    await page.getByLabel('Send remaining Clay to 11').check()
    await page.getByLabel(/no village absorbs the remaining Lumber/).check()

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].allocations.lumber).toBeUndefined()
    expect(posts[0].allocations.clay[String(DEF_A)]).toEqual({ mode: 'remainder', value: 0 })
  })

  test('the Mode select says what the row does, and cannot be used to say otherwise', async ({
    page,
  }) => {
    await isolateCountingPlans(page)
    await seed(page)
    await openEditByResource(page)

    const mode = page.getByLabel('Lumber mode for 11')
    await expect(mode).toHaveValue('keep')
    await expect(mode).toBeEnabled()

    await page.getByLabel('Send remaining Lumber to 11').check()
    // The reported misreport: this used to read "Keep own", which is not what
    // the plan does with a Rest village.
    await expect(mode).toHaveValue('remainder')
    await expect(mode).toContainText('Rest — absorbs the slack')
    await expect(mode).toBeDisabled()

    await page.getByLabel(/no village absorbs the remaining Lumber/).check()
    await expect(mode).toBeEnabled()
    await expect(mode).toHaveValue('keep')
  })
})
