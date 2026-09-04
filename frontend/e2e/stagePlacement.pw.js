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

test.describe('the Targets stage opens on the view that sets a target', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  // The stage is called Targets, it opened on the read-only grid, and that
  // grid's own hint read "Edit the targets in the other view."
  test('the editor is showing, unasked', async ({ page }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')
    await stageTab(page, 'Targets').click()

    await expect(page.getByRole('button', { name: 'Edit by resource' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    // A control that can only exist on the editing view.
    await expect(page.getByLabel('Lumber value for 11')).toBeVisible()
    await expect(page.getByText(/Edit the targets in the other view/)).toHaveCount(0)
  })

  test('the read-only grid is still one click away, and says what it is', async ({ page }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')
    await stageTab(page, 'Targets').click()

    await page.getByRole('button', { name: 'Result by village' }).click()
    await expect(page.getByText(/What each village keeps per hour once the routes run/))
      .toBeVisible()
    await expect(page.getByLabel('Lumber value for 11')).toHaveCount(0)
  })
})

test.describe('the night derivation sits beside the hours it derives from', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  const DERIVE = 'Derive an idle-window profile from your stores'

  // It was the first card on Targets, carrying that stage's only large filled
  // CTA -- and the sentence inside it told the operator not to press it.
  test('it is on Day & night, and no longer on Targets', async ({ page }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')

    await stageTab(page, 'Targets').click()
    await expect(page.getByText(DERIVE)).toHaveCount(0)
    await expect(page.getByRole('button', { name: /^Derive from stores/ })).toHaveCount(0)

    await stageTab(page, 'Day & night').click()
    await expect(page.getByText(DERIVE)).toBeVisible()
    await expect(page.getByRole('button', { name: /^Derive from stores/ })).toBeVisible()
  })

  // The pair the panel derives AGAINST and the check that grades against it are
  // now one glance apart. On separate stages they came to disagree -- 30/80 in
  // the boxes against the server's own 25/60 -- with nobody seeing both.
  test('the 25/60 pair and the check that grades against it are on one stage', async ({
    page,
  }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')
    await stageTab(page, 'Day & night').click()

    await expect(page.getByText('(full% − empty%) × capacity ÷ hours')).toBeVisible()
    await expect(page.getByRole('button', { name: /^Run \(0 requests\)/ })).toBeVisible()
    // And the panel that edits the windows the derivation reads is right there.
    await expect(page.getByText('The day, window by window')).toBeVisible()
  })

  // The window is edited on this stage, so the warning about a 16h profile is
  // now readable in the same glance as the figure that makes it true.
  test('the daytime warning reacts to the window edited beside it', async ({ page }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')
    await stageTab(page, 'Day & night').click()

    // Day is 07:00-23:00 by default: 16 of 24 hours.
    await expect(page.getByText(/This profile runs 16h of the day/)).toBeVisible()

    // `.first()`: the profile bar and the Day & night table both carry a
    // control named "Day window start", which is a duplicate accessible name on
    // that stage -- reported, not fixed here.
    await page.getByLabel('Day window start').first().fill('22:00')
    await expect(page.getByText(/This profile runs 16h of the day/)).toHaveCount(0)
  })
})

test.describe('the crop alert level is typed where it is read', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  // It was column 10 of the Account table. The only things that read it -- the
  // full-day check's ALERT AT column and its "crosses its crop ceiling at
  // 04:00" warning -- are two stages away on Day & night.
  test('it is on Day & night, and gone from the Account table', async ({ page }) => {
    await isolatePlanning(page)
    await seed(page)
    await page.goto('/resource-planner')

    await expect(page.getByLabel('Crop stock alert level for 02')).toHaveCount(0)
    await expect(page.getByRole('columnheader', { name: 'Crop alert' })).toHaveCount(0)

    await stageTab(page, 'Day & night').click()
    await expect(page.getByLabel('Crop stock alert level for 02')).toBeVisible()
  })

  // The table it lives in used to be gated on a simulation having run, so
  // moving the input there without this would have made it untypable until the
  // operator pressed a button they had no reason to press first.
  test('it is typable before the simulation has ever run, and survives it', async ({ page }) => {
    const dayChecks = []
    await isolate(page, async (path, route) => {
      if (path.endsWith('/distribution/day-check')) {
        dayChecks.push(route.request().postDataJSON())
        await route.fulfill({
          json: {
            morning_floor: 0.6,
            pre_night_baseline: 0.25,
            morning_shortfalls: [],
            pre_night_over_baseline: [],
            night_overruns: [],
            warnings: [],
            skipped: [],
            villages: [
              {
                village_id: 20002,
                village_name: '02',
                resource: 'crop',
                low: 1000,
                high: 250000,
                daily_net: 12,
                settled: true,
              },
            ],
          },
        })
        return 'handled'
      }
      return undefined
    })
    await seed(page)
    await page.goto('/resource-planner')
    await stageTab(page, 'Day & night').click()

    // Before any run: the box is there and the computed columns say so.
    await expect(page.getByText('not simulated yet').first()).toBeVisible()
    await page.getByLabel('Crop stock alert level for 02').fill('300000')

    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect.poll(() => dayChecks.length).toBe(1)
    expect(dayChecks[0].crop_ceilings).toEqual({ 20002: 300000 })
    // And the box still holds it once the rows are real.
    await expect(page.getByLabel('Crop stock alert level for 02')).toHaveValue('300000')
  })
})

test.describe('a cell says where its figure came from', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  const DEF_TEMPLATE = { def: { allocations: { lumber: { mode: 'absolute', value: 8372 } } } }

  async function openTargetsWithRole(page) {
    await seed(page, {
      planner_village_roles: { 20011: 'def' },
      planner_role_templates: DEF_TEMPLATE,
    })
    await page.goto('/resource-planner')
    await stageTab(page, 'Targets').click()
    await expect(page.getByLabel('Lumber value for 11')).toBeVisible()
  }

  // The reported case: the cell read `absolute / 8372` off the DEF template
  // with nothing saying where it came from, while an OVERRIDE was correctly
  // marked. So the operator could not tell, before touching the cell, that
  // touching it creates an override.
  test('an inherited figure is marked as inherited', async ({ page }) => {
    await isolatePlanning(page)
    await openTargetsWithRole(page)

    const box = page.getByLabel('Lumber value for 11')
    await expect(box).toHaveValue('8372')
    await expect(page.getByText('from DEF')).toBeVisible()
    // And a screen reader hears the same fact the chip carries.
    await expect(box).toHaveAttribute('aria-describedby', 'inherits-lumber-20011')
  })

  test('the chip is dropped the moment the cell is overridden', async ({ page }) => {
    await isolatePlanning(page)
    await openTargetsWithRole(page)

    await page.getByLabel('Lumber value for 11').fill('12000')
    // One provenance note, not two: the deviation line now says where the
    // figure came from and what the role asked for.
    await expect(page.getByText('from DEF')).toHaveCount(0)
    await expect(page.getByText(/≠ DEF: Absolute \/h 8,372/)).toBeVisible()
    await expect(page.getByLabel('Lumber value for 11')).toHaveAttribute(
      'aria-describedby',
      'deviates-lumber-20011',
    )
  })

  test('a resource the template says nothing about is not marked', async ({ page }) => {
    await isolatePlanning(page)
    await openTargetsWithRole(page)
    // Clay has no DEF figure, so its cell is the village's own default.
    await expect(page.getByLabel('Clay value for 11')).not.toHaveAttribute(
      'aria-describedby',
      /inherits/,
    )
  })
})
