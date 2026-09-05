/**
 * Clearing a merchant lever, which made the whole setup unsaveable.
 *
 * "Base capacity" and "Bonus / TO level" may be emptied, and blank means "use
 * the planner's own": the plan request omits the field and the backend's
 * default stands. The DOCUMENT path did not read it that way. `buildSetup`
 * stored the model wholesale, so an emptied box travelled as an `undefined`
 * key -- the PUT came back 422 "Field required", the export wrote a file
 * `parseSetup` refused, and no cell on the page was marked on the way.
 *
 * Nothing is filled in to fix it. Writing 2,500 into an emptied box would make
 * it look like a calibration the operator asserted, and that figure sizes every
 * cargo the account ever ships -- it only recently stopped being 2,200.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test blankMerchantModel
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolate, seed } from './plannerHarness'

const BASE = 'Merchant base capacity'
const BONUS = 'Trade Office bonus per level'

/** The setup store, in memory: a PUT fills it, a GET hands it back verbatim --
 *  which is what the real store does, and what makes this a round trip. */
async function isolateStore(page) {
  const state = { saved: null, puts: [] }
  await isolate(page, async (path, route) => {
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
      await route.fulfill({
        json: { setup: state.saved, saved_at: '2026-09-05T04:00:00Z' },
      })
      return 'handled'
    }
    return undefined
  })
  return state
}

async function openPlanner(page) {
  await page.goto('/resource-planner')
  await expect(page.getByLabel(BASE)).toBeVisible()
}

/** The bonus box lives behind the "Non-Europe-2 world" disclosure, beside the
 *  map span: the row above holds the four levers an operator really does tune. */
async function openWorldOverrides(page) {
  await page.getByRole('group').filter({ hasText: 'Non-Europe-2 world' }).first().click()
  await expect(page.getByLabel(BONUS)).toBeVisible()
}

test.describe('an emptied merchant lever', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('saves, instead of coming back 422 over a box that reads blank', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openPlanner(page)

    await page.getByLabel(BASE).fill('')
    await expect(page.getByLabel(BASE)).toHaveValue('')

    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(store.puts).toHaveLength(1)
    const model = store.puts[0].merchant_model
    // Absent, not 0 and not 2,500. Blank is the operator declining to assert a
    // calibration, and the request omits the field for exactly the same reason.
    expect(model).not.toHaveProperty('base_capacity')
    expect(model.bonus_per_to_level).toBe(0.2)
  })

  test('comes back out of the store still blank, not filled in', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openPlanner(page)

    await page.getByLabel(BASE).fill('')
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()
    expect(store.saved).not.toBeNull()

    // A fresh page with nothing typed, then the saved document over it.
    await page.reload()
    await page.getByLabel(BASE).fill('9999')
    await page.getByRole('button', { name: 'Load setup from server' }).click()
    await expect(page.getByText(/from the saved setup/).first()).toBeVisible()

    await expect(page.getByLabel(BASE)).toHaveValue('')
    // The lever the document DOES carry still lands, so this is not the reader
    // dropping the model wholesale.
    await openWorldOverrides(page)
    await expect(page.getByLabel(BONUS)).toHaveValue('0.2')
  })

  test('the same for the Trade Office bonus, which is the sharper of the two', async ({
    page,
  }) => {
    // Sharper because an accidental clear used to be KEPT rather than dropped:
    // it silently stopped every Trade Office level adding any capacity at all.
    const store = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openPlanner(page)

    await openWorldOverrides(page)
    await page.getByLabel(BONUS).fill('')
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(store.puts[0].merchant_model).not.toHaveProperty('bonus_per_to_level')
    expect(store.puts[0].merchant_model.base_capacity).toBe(2500)
  })

  test('a typed calibration still travels, unchanged', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openPlanner(page)

    await page.getByLabel(BASE).fill('3200')
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(store.puts[0].merchant_model.base_capacity).toBe(3200)
  })
})
