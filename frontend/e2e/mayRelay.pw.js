/**
 * `may_relay`, DRIVEN — the field that had no input at all.
 *
 * It existed on the request, it travelled in the setup document, and the page
 * carried it only so `parseSetup`'s answer did not fall on the floor. The
 * source said as much: *"No input for it yet; it arrives from the setup file."*
 * So the one case the field exists for -- a DEF village on the only road to a
 * corner of the map, where the operator wants THAT village relaying and not its
 * three siblings -- could not be stated from the page at all.
 *
 * Which makes this a request-body spec rather than a rendering one: a control
 * that writes state nothing sends is the same defect by another door.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test mayRelay
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
const configFor = (body, id) => body.config.find((row) => row.village_id === id)

/** A DEF template, so the villages that claim the role have one. */
const DEF = { def: { consumption: { lumber: 8372 } } }

test.describe('a village may be told whether it relays', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('the resting state sends nothing, because unset is not an answer', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')

    await expect(page.getByLabel('Whether 11 may relay')).toHaveValue('')
    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    // No row at all for a village that declares nothing else either.
    expect(configFor(posts[0], DEF_A)).toBeUndefined()
  })

  test('the singular case, typed: one DEF village relays and its sibling does not', async ({
    page,
  }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page, {
      planner_village_roles: { [CAPITAL]: 'def', [DEF_A]: 'def' },
      planner_role_templates: DEF,
    })
    await page.goto('/resource-planner')

    // Both are DEF, and the role default is "may not" -- so this is exactly the
    // case that could not be stated: give the permission to ONE of them.
    await expect(page.getByLabel('Whether 11 may relay')).toHaveValue('')
    await page.getByLabel('Whether 11 may relay').selectOption('yes')

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(configFor(posts[0], DEF_A).may_relay).toBe(true)
    expect(configFor(posts[0], CAPITAL)).not.toHaveProperty('may_relay')
  })

  test('false is an answer and is sent as one', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByLabel('Whether 02 may relay').selectOption('no')
    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(configFor(posts[0], CAPITAL).may_relay).toBe(false)
  })

  test('going back to the default drops the field rather than sending a null', async ({ page }) => {
    const posts = await isolateCountingPlans(page)
    await seed(page, { planner_may_relay: { [DEF_A]: true } })
    await page.goto('/resource-planner')

    await expect(page.getByLabel('Whether 11 may relay')).toHaveValue('yes')
    await page.getByLabel('Whether 11 may relay').selectOption('')

    await buildPlan(page).click()
    await expect.poll(() => posts.length).toBe(1)
    expect(configFor(posts[0], DEF_A)).toBeUndefined()
  })

  test('it survives a reload, like every other owned column', async ({ page }) => {
    await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByLabel('Whether 11 may relay').selectOption('no')
    await page.reload()
    await expect(page.getByLabel('Whether 11 may relay')).toHaveValue('no')
  })

  // The resting option names WHICH of three answers it means, because the
  // answer depends on the role, on that role's template, and on the sign of
  // this village's own crop.
  test('the unset option names what it resolves to', async ({ page }) => {
    await isolateCountingPlans(page)
    await seed(page)
    await page.goto('/resource-planner')

    const displayed = (name) =>
      page.getByLabel(name).evaluate((el) => el.options[el.selectedIndex].textContent)

    // No role: the crop sign, and the harness villages make +2,200/h.
    await expect(displayed('Whether 11 may relay')).resolves.toBe('From the crop sign (may)')

    await page.getByLabel('Role for 11').selectOption('def')
    await expect(displayed('Whether 11 may relay')).resolves.toBe('Role default (may not)')

    await page.getByLabel('Role for 11').selectOption('feeder')
    await expect(displayed('Whether 11 may relay')).resolves.toBe('Role default (may)')
  })

  test('a template that states a permission is named as the source', async ({ page }) => {
    await isolateCountingPlans(page)
    await seed(page, {
      planner_village_roles: { [DEF_A]: 'def' },
      planner_role_templates: { def: { consumption: { lumber: 8372 }, may_relay: true } },
    })
    await page.goto('/resource-planner')

    await expect(
      page
        .getByLabel('Whether 11 may relay')
        .evaluate((el) => el.options[el.selectedIndex].textContent)
    ).resolves.toBe('DEF template (may)')
  })
})
