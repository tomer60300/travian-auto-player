/**
 * Where the planner puts things, DRIVEN.
 *
 * The stages are the page's whole architecture: four of them, so that tuning a
 * target and re-planning is free. Three findings in the audit were about that
 * architecture betraying itself, and all three are invisible to a unit test
 * because they are about what is on screen after a press.
 *
 *   * `Build plan` ended with an unconditional `setStage('plan')`, so editing a
 *     15-column table on Account and pressing it threw the operator onto
 *     another stage -- and navigating back remounted the table with every
 *     `<details>` CLOSED, because `open` is DOM state React does not restore.
 *
 * NO BACKEND AND NO GAME REQUEST: `isolate` answers the shell's calls and
 * aborts everything else fail-closed.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test stagePlacement
 */

import { expect, test } from '@playwright/test'

import { PLAN, PLAN_BLOCKED, isolate, seed } from './plannerHarness'

async function isolatePlanning(page, plan = PLAN) {
  const posts = []
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      posts.push(route.request().postDataJSON())
      await route.fulfill({ json: plan })
      return 'handled'
    }
    return undefined
  })
  return posts
}

const buildPlan = (page) => page.getByRole('button', { name: /^Build plan/ })
const stageTab = (page, name) => page.getByRole('button', { name, exact: true })

/** Which stage is showing, read off the tab strip's own `aria-current`. */
async function currentStage(page) {
  return page.locator('nav[aria-label="Planner stages"] button[aria-current="page"]').innerText()
}

test.describe('re-planning does not move the operator', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('the stage stays where it was, and the chip is the acknowledgement', async ({ page }) => {
    const posts = await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')
    expect(await currentStage(page)).toBe('Account')

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)

    // The whole finding: this used to read "Plan".
    await expect(page.getByRole('button', { name: 'Ready to run' })).toBeVisible()
    expect(await currentStage(page)).toBe('Account')
  })

  test('the chip counts what is outstanding, and is one click from the detail', async ({
    page,
  }) => {
    await isolatePlanning(page, PLAN_BLOCKED)
    await seed(page)
    await page.goto('/resource-planner')

    await buildPlan(page).click()
    const chip = page.getByRole('button', { name: 'Cannot run · 2 blockers' })
    await expect(chip).toBeVisible()
    expect(await currentStage(page)).toBe('Account')

    await chip.click()
    expect(await currentStage(page)).toBe('Plan')
    await expect(page.getByText(/^Routes$/)).toBeVisible()
  })

  // The cost the stage jump was charging: `<details open>` is DOM state React
  // does not restore, so every picker the operator had opened to read across a
  // row shut itself on a press whose whole purpose is being free.
  test('an open picker on Account survives a re-plan', async ({ page }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')

    const group = page.getByRole('group', { name: 'Villages 02 may ship to' })
    await expect(group).toBeHidden()
    await page.locator('summary').filter({ hasText: 'Ships only to, for 02' }).click()
    await expect(group).toBeVisible()

    await buildPlan(page).click()
    await expect(page.getByRole('button', { name: 'Ready to run' })).toBeVisible()
    // The assertion the stage jump used to fail: React does not restore
    // `<details open>`, so a remount closed every picker on the row.
    await expect(group).toBeVisible()
  })

  test('the Plan stage is still reachable from the tab, and shows the plan', async ({ page }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')
    await buildPlan(page).click()
    await expect(page.getByRole('button', { name: 'Ready to run' })).toBeVisible()

    await stageTab(page, 'Plan').click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
  })

  test('no chip before a plan exists, so it never claims an answer it has not got', async ({
    page,
  }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')
    await expect(page.getByRole('button', { name: /^(Ready to run|Cannot run|Runs, not clean)/ }))
      .toHaveCount(0)
  })
})
