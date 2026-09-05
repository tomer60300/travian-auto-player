/**
 * Accessible names on the planner stages, measured against the AX tree.
 *
 * Four findings from the census, all of the same family: a control whose name
 * says what it does but not what it does it TO. On a two-village fixture each
 * one is merely ambiguous; on the real 26-village account the ships-to list
 * alone repeats every village name 2x(N-1) times, so "11" as a checkbox name
 * appears fifty times on one screen and none of them says which row it belongs
 * to. A screen-reader user cannot tell them apart at all, and neither can a
 * `getByRole` locator.
 *
 * The enclosing `role="group"` already carries the context the checkbox lacks,
 * but a group label is not part of a checkbox's accessible name -- it is only
 * announced on entering the group, which is exactly the information lost when
 * the user tabs into the middle of one or navigates by control.
 *
 * The batch-edit checkboxes on the Targets stage already do this correctly
 * ("Select 11 for batch edit of Lumber"), so the pattern is the page's own.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test plannerNames
 */

import { expect, test } from '@playwright/test'

import { isolate, seed } from './plannerHarness'

async function openAccount(page, extra = {}) {
  await isolate(page)
  await seed(page, extra)
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Allocation profile')).toBeVisible()
}

/** Every cell picker is a native `<details>`, and a closed one keeps its
 *  contents out of the accessibility tree -- so a role query finds nothing
 *  until they are open. Same approach as `cellPickers.pw.js`. */
async function openEveryPicker(page) {
  await page.evaluate(() => {
    for (const d of document.querySelectorAll('tbody details')) d.open = true
  })
  await expect(page.locator('tbody details:not([open])')).toHaveCount(0)
}

test.describe('every control says what it acts on', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('a ships-to checkbox names the row it belongs to', async ({ page }) => {
    await openAccount(page)

    await openEveryPicker(page)
    // One name per (row, destination) pair, not one per destination.
    await expect(page.getByRole('checkbox', { name: '11: 02 may ship to' })).toHaveCount(1)
    await expect(page.getByRole('checkbox', { name: '02: 11 may ship to' })).toHaveCount(1)
    // And the bare village name is no longer a checkbox name anywhere.
    await expect(page.getByRole('checkbox', { name: '11', exact: true })).toHaveCount(0)
  })

  test('a relays-for checkbox does the same', async ({ page }) => {
    await openAccount(page)
    await openEveryPicker(page)

    await expect(
      page.getByRole('checkbox', { name: '11: 02 forwards material to' })
    ).toHaveCount(1)
    await expect(
      page.getByRole('checkbox', { name: '02: 11 forwards material to' })
    ).toHaveCount(1)
  })

  test('"clear this village’s own figures" names the village', async ({ page }) => {
    await openAccount(page, { planner_consumption: { 20002: { lumber: 8372 } } })
    await openEveryPicker(page)

    await expect(
      page.getByRole('button', { name: "Clear 02's own spend figures" })
    ).toHaveCount(1)
    // The generic name is gone, so N villages no longer offer N identical
    // buttons.
    await expect(
      page.getByRole('button', { name: "Clear this village's own figures" })
    ).toHaveCount(0)
  })

  test('the two window editors do not share a name', async ({ page }) => {
    await isolate(page)
    await seed(page, { planner_profiles: { Day: {}, Night: {} } })
    await page.goto('/resource-planner')

    // The profile bar's pair, which edits whichever profile is selected.
    await expect(page.getByLabel('Day window start, profile bar')).toHaveCount(1)
    await expect(page.getByLabel('Day window end, profile bar')).toHaveCount(1)

    await page.getByRole('button', { name: 'Day & night' }).click()
    // The table's pair, one row per profile. Both are on screen at once at this
    // width, so a shared "Day window start" named two different inputs.
    await expect(page.getByLabel('Day window start, day and night table')).toHaveCount(1)
    await expect(page.getByLabel('Night window start, day and night table')).toHaveCount(1)
    await expect(page.getByLabel('Day window start', { exact: true })).toHaveCount(0)
  })
})

test.describe('the day and night table says what the plan will do', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('a profile with no stored hours shows the ones the plan uses', async ({ page }) => {
    await isolate(page)
    // No `planner_profile_windows` at all, which is every fresh account.
    await seed(page, { planner_profiles: { Day: {}, Night: {} } })
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: 'Day & night' }).click()

    // `buildSegments` resolves `profileWindows[name] ?? DEFAULT_WINDOWS[name]`,
    // and so does the profile bar's own editor -- so this table showing blank
    // boxes and "skipped by the day check" was the one reading on the page that
    // disagreed with the plan. Day IS planned, at 07:00-23:00.
    await expect(page.getByLabel('Day window start, day and night table')).toHaveValue('07:00')
    await expect(page.getByLabel('Day window end, day and night table')).toHaveValue('23:00')
    await expect(page.getByLabel('Night window start, day and night table')).toHaveValue('23:00')
    await expect(page.getByText(/skipped by the day check/)).toHaveCount(0)
  })

  test('a profile the fallback does not cover still says it is skipped', async ({ page }) => {
    await isolate(page)
    await seed(page, { planner_profiles: { Day: {}, Weekend: {} } })
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: 'Day & night' }).click()

    // `DEFAULT_WINDOWS` covers Day and Night by name and nothing else, so this
    // one really is left out -- and now the sentence is only shown where it is
    // true.
    await expect(page.getByLabel('Weekend window start, day and night table')).toHaveValue('')
    await expect(page.getByText(/skipped by the day check/)).toHaveCount(1)
  })
})
