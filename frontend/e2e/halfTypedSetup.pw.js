/**
 * Half-typed state, which used to block the whole document.
 *
 * Two controls put the page into a state it could not save from, and neither
 * is an error -- both are the operator mid-edit:
 *
 *   * `+ Add target` seeds a tribute row with no name, no rate and 0|0. The
 *     REQUEST has always dropped it (`usableForeignTargets` filters on
 *     `foreignTargetIsDraft`, and the table's own "incomplete" badge reads the
 *     same predicate) while `buildSetup` wrote it raw, so the PUT came back 422
 *     on `name` and `crop_per_hour`.
 *   * a profile window is two `HH:MM` boxes, so there is a moment where only
 *     one is filled in. `["07:00", ""]` is refused by `_ClockTime` on the
 *     server and by `parseClockPair` here.
 *
 * So "Save setup to server" failed for as long as one box was mid-edit, over
 * rows and hours the plan itself was already ignoring.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test halfTypedSetup
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolate, seed } from './plannerHarness'

async function isolateStore(page) {
  const puts = []
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/setup') && route.request().method() === 'PUT') {
      puts.push(route.request().postDataJSON())
      await route.fulfill({ json: { saved_at: '2026-09-05T04:00:00Z' } })
      return 'handled'
    }
    return undefined
  })
  return puts
}

const save = (page) => page.getByRole('button', { name: 'Save setup to server' })

test.describe('a document is saveable while a cell is mid-edit', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('a fresh "+ Add target" row does not travel, and does not stop the save', async ({
    page,
  }) => {
    const puts = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: '+ Add target' }).click()
    // On screen it is visibly a draft -- the badge and the payload read the
    // same predicate, which is the point.
    await expect(page.getByLabel('Foreign target 1 excluded origins')).toBeVisible()

    await save(page).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(puts).toHaveLength(1)
    expect(puts[0]).not.toHaveProperty('foreign_targets')
  })

  test('a finished target beside a draft still travels', async ({ page }) => {
    const puts = await isolateStore(page)
    await seed(page, {
      // A Trade Office level as well, because `setupDocument` counts only
      // village columns, profiles and role templates when it decides there is
      // something to save -- a tribute on its own is refused before this gets
      // anywhere near the writer.
      planner_trade_office: { [CAPITAL]: 13 },
      planner_foreign_targets: [
        {
          name: '01Arb',
          x: 46,
          y: 133,
          crop_per_hour: 25700,
          safety_margin_pct: 0,
          route_eligible: true,
        },
      ],
    })
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: '+ Add target' }).click()
    await save(page).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(puts[0].foreign_targets).toHaveLength(1)
    expect(puts[0].foreign_targets[0].name).toBe('01Arb')
  })

  test('a window with only its start typed does not stop the save', async ({ page }) => {
    const puts = await isolateStore(page)
    await seed(page, {
      planner_trade_office: { [CAPITAL]: 13 },
      planner_profiles: { Day: {}, Night: {} },
      planner_profile_windows: { Day: ['07:00', '23:00'], Night: ['23:00', ''] },
    })
    await page.goto('/resource-planner')

    await save(page).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    // The finished profile keeps its hours; the one mid-edit is simply absent,
    // which is what "not answered yet" already means everywhere else here.
    expect(puts[0].profile_windows).toEqual({ Day: ['07:00', '23:00'] })
  })
})
