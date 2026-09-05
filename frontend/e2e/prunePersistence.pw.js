/**
 * The window prune, which was carried by NEITHER persistence path.
 *
 * It decides whether `/execute` DELETES game rows. Travian cannot confine a
 * route to part of the day, but its fan-out can be trimmed -- "repeat every N
 * hours" is 24/N individually deletable rows -- and the prune is what removes
 * the ones departing outside the profile. Without it the window is a fiction
 * the game ignores and the destination receives every firing.
 *
 * It lived as a `useState(true)` inside the page: not in localStorage, not in
 * the document. So an operator who turned it off saw it back ON after a reload,
 * and the next run left every out-of-window firing live in the game. That is
 * the exact criterion that earned `reserved_window` its v9 bump, met harder --
 * the reserved window at least survived a reload on the same origin.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test prunePersistence
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, KEY, PLAN, isolate, seed } from './plannerHarness'

async function isolateStore(page) {
  const state = { saved: null, puts: [] }
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    if (!path.endsWith('/distribution/setup')) return undefined
    const method = route.request().method()
    if (method === 'PUT') {
      state.saved = route.request().postDataJSON()
      state.puts.push(state.saved)
      await route.fulfill({ json: { saved_at: '2026-09-05T04:00:00Z' } })
      return 'handled'
    }
    if (method === 'GET') {
      if (state.saved == null) {
        await route.fulfill({
          status: 404,
          json: { detail: 'No planner setup is saved for this account.' },
        })
        return 'handled'
      }
      await route.fulfill({ json: { setup: state.saved, saved_at: '2026-09-05T04:00:00Z' } })
      return 'handled'
    }
    return undefined
  })
  return state
}

const prune = (page) =>
  page.getByRole('checkbox', { name: 'Trim the fan-out to the profile hours' })

/** Toggling it CLEARS THE PLAN -- the switch is one of the inputs a route sheet
 *  is computed from, so the run panel holding it unmounts on the same click.
 *  Pre-existing and correct (a sheet is retyped into the game's dialog, so one
 *  that outlived its inputs is wrong instructions), but it means `uncheck()`
 *  cannot verify its own result: the box is gone by the time it looks. */
async function turnPruneOff(page) {
  await expect(prune(page)).toBeChecked()
  await prune(page).click()
  await expect(prune(page)).toHaveCount(0)
}

/** What localStorage holds, which is the half a reload reads back. */
async function storedPrune(page) {
  const raw = await page.evaluate(
    (key) => localStorage.getItem(`planner_prune_to_window::${key}`),
    KEY
  )
  return raw == null ? null : JSON.parse(raw)
}

/** The Plan stage, where the switch lives -- beside the run it changes. */
async function openPlanStage(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

test.describe('the window prune is remembered', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('survives a reload on the same origin', async ({ page }) => {
    await isolateStore(page)
    await seed(page, {
      planner_profile_windows: { Day: ['07:00', '23:00'] },
      planner_trade_office: { [CAPITAL]: 13 },
    })
    await openPlanStage(page)

    await turnPruneOff(page)
    expect(await storedPrune(page)).toBe(false)

    await page.reload()
    await openPlanStage(page)
    // ON after a reload is the defect: the operator's deliberate OFF was gone
    // and the next run deleted nothing while the plan reported an enforced
    // window.
    await expect(prune(page)).not.toBeChecked()
  })

  test('is written into the document, both ways round', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, {
      planner_profile_windows: { Day: ['07:00', '23:00'] },
      planner_trade_office: { [CAPITAL]: 13 },
    })
    await openPlanStage(page)
    await turnPruneOff(page)

    await page.getByRole('button', { name: 'Account', exact: true }).click()
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(store.puts[0].prune_to_window).toBe(false)
    // The version rose for it, on the rule this document follows: a build that
    // cannot read the field must refuse the file rather than half-load it.
    expect(store.puts[0].version).toBe(10)
  })

  test('comes back out of the store as the answer that was saved', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, {
      planner_profile_windows: { Day: ['07:00', '23:00'] },
      planner_trade_office: { [CAPITAL]: 13 },
    })
    await openPlanStage(page)
    await turnPruneOff(page)
    await page.getByRole('button', { name: 'Account', exact: true }).click()
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()
    expect(store.saved).not.toBeNull()

    // A different origin is a fresh localStorage. Simulated by clearing it,
    // which is the failure the storage panel warns about in words.
    await page.evaluate((key) => localStorage.removeItem(`planner_prune_to_window::${key}`), KEY)
    await page.reload()
    await page.getByRole('button', { name: 'Load setup from server' }).click()
    await expect(page.getByText(/from the saved setup/).first()).toBeVisible()

    await openPlanStage(page)
    await expect(prune(page)).not.toBeChecked()
  })
})
