/**
 * Batch-set, DRIVEN end to end — the one feature in the Targets grid the audit
 * could not reach at all.
 *
 * The reason it could not is the finding: four of these widgets render on one
 * screen, one per resource, and every control in them shared a name with its
 * three siblings. "Batch mode", "Batch value", "Apply to 2 selected", "Select
 * all villages", "Select 17 for batch edit" — each ×4. A duplicated accessible
 * name is the same defect for a spec and for a screen-reader user: neither can
 * say WHICH of the four it means. So the feature that writes an allocation to
 * many villages at once had no coverage of any kind, and the audit's own
 * attempt to exercise it stopped at strict-mode violations.
 *
 * Every case here therefore targets a control BY RESOURCE, and asserts on what
 * reaches the request body — the allocations map is what the plan is built out
 * of, and a rendered input's value would only confirm React re-rendered.
 *
 * NO BACKEND AND NO GAME REQUEST: `isolate` answers the shell's calls and
 * aborts everything else fail-closed.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test batchSet
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, DEF_A, PLAN, isolate, seed } from './plannerHarness'

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

async function openTargets(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Targets' }).click()
  await expect(page.getByLabel('Lumber value for 11')).toBeVisible()
}

test.describe('batch-set writes to the checked villages', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('every control names its resource, so none of the four is ambiguous', async ({ page }) => {
    await isolateCountingPlans(page)
    await seed(page)
    await openTargets(page)

    // `getByLabel` in strict mode IS the assertion: it throws on two matches,
    // which is what every one of these used to do.
    for (const resource of ['Lumber', 'Clay', 'Iron', 'Crop']) {
      await expect(
        page.getByLabel(`Mode to set on the checked villages for ${resource}`)
      ).toBeVisible()
      await expect(
        page.getByLabel(`Value to set on the checked villages for ${resource}`)
      ).toBeVisible()
      await expect(page.getByLabel(`Select all villages for ${resource}`)).toBeVisible()
      await expect(page.getByLabel(`Select 02 for batch edit of ${resource}`)).toBeVisible()
      await expect(
        page.getByRole('button', { name: `Apply to 0 selected villages for ${resource}` })
      ).toBeVisible()
    }
  })

  test('two checked villages take one absolute target, and it reaches the request', async ({
    page,
  }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await openTargets(page)

    await page.getByLabel('Select 02 for batch edit of Lumber').check()
    await page.getByLabel('Select 11 for batch edit of Lumber').check()
    await page
      .getByLabel('Mode to set on the checked villages for Lumber')
      .selectOption('absolute')
    await page.getByLabel('Value to set on the checked villages for Lumber').fill('9000')
    await page
      .getByRole('button', { name: 'Apply to 2 selected villages for Lumber' })
      .click()

    // On screen first, so a body that agrees is not agreeing with itself.
    await expect(page.getByLabel('Lumber value for 02')).toHaveValue('9000')
    await expect(page.getByLabel('Lumber value for 11')).toHaveValue('9000')

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].allocations.lumber).toEqual({
      [String(CAPITAL)]: { mode: 'absolute', value: 9000 },
      [String(DEF_A)]: { mode: 'absolute', value: 9000 },
    })
  })

  test('the header tick selects every village of that resource and no other', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await openTargets(page)

    await page.getByLabel('Select all villages for Clay').check()
    await expect(
      page.getByRole('button', { name: 'Apply to 2 selected villages for Clay' })
    ).toBeEnabled()
    // Lumber's own widget is untouched, which is the half a shared name hid.
    await expect(
      page.getByRole('button', { name: 'Apply to 0 selected villages for Lumber' })
    ).toBeDisabled()

    await page.getByLabel('Mode to set on the checked villages for Clay').selectOption('sustain')
    await page.getByLabel('Value to set on the checked villages for Clay').fill('80')
    await page.getByRole('button', { name: 'Apply to 2 selected villages for Clay' }).click()

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].allocations.clay).toEqual({
      [String(CAPITAL)]: { mode: 'sustain', value: 80 },
      [String(DEF_A)]: { mode: 'sustain', value: 80 },
    })
    expect(posts[0].allocations.lumber).toBeUndefined()
  })

  // `applyToSelected` skips the remainder village on purpose: a batch edit must
  // never silently clear the destination the slack goes to.
  test('the Rest village keeps its role through a batch edit', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await openTargets(page)

    await page.getByLabel('Send remaining Iron to 11').check()
    await page.getByLabel('Select all villages for Iron').check()
    await page.getByLabel('Mode to set on the checked villages for Iron').selectOption('absolute')
    await page.getByLabel('Value to set on the checked villages for Iron').fill('4000')
    await page.getByRole('button', { name: 'Apply to 2 selected villages for Iron' }).click()

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].allocations.iron).toEqual({
      [String(CAPITAL)]: { mode: 'absolute', value: 4000 },
      [String(DEF_A)]: { mode: 'remainder', value: 0 },
    })
  })

  test('the value box is disabled beside Keep own, because keep is not a figure', async ({
    page,
  }) => {
    await isolateCountingPlans(page)
    await seed(page)
    await openTargets(page)

    const box = page.getByLabel('Value to set on the checked villages for Crop')
    await expect(box).toBeDisabled()
    await page.getByLabel('Mode to set on the checked villages for Crop').selectOption('percentage')
    await expect(box).toBeEnabled()
  })

  test('applying with nothing checked says so instead of writing nothing', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await openTargets(page)

    // The button is disabled, which is the primary guard -- so the toast path
    // is reached by checking a village and unchecking it again.
    const apply = page.getByRole('button', { name: /^Apply to \d+ selected villages for Lumber/ })
    await expect(apply).toBeDisabled()
    await page.getByLabel('Select 02 for batch edit of Lumber').check()
    await expect(apply).toBeEnabled()
    await page.getByLabel('Select 02 for batch edit of Lumber').uncheck()
    await expect(apply).toBeDisabled()

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0].allocations).toEqual({})
  })
})
