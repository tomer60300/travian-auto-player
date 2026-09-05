/**
 * The acknowledgement that the merchant model was read off the game.
 *
 * MERCHANT_MODEL_UNCALIBRATED fires whenever `trade_office_bonus_per_level`
 * still equals the shipped 0.20 and any village has a Trade Office. It is an
 * EQUALITY TEST against the default, so it cannot tell a measured 0.20 from an
 * untouched one -- and an operator who read a Marketplace capacity at two Trade
 * Office levels, found the default right and typed it back got the same warning
 * for ever, asking them to do the thing they had just done.
 *
 * The box is that operator saying they looked. It silences that one finding and
 * changes no number, which is why the `Why` says so out loud: a checkbox beside
 * six figures that quietly moved one of them would be the worse defect.
 *
 * Backend twins: `PlanRequest.merchant_model_measured` and
 * `SetupDocument.merchant_model_measured` in
 * `src/travian_api/web/routes/distribution.py` and
 * `src/travian_api/web/routes/planner_setup.py`.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test merchantMeasured
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, KEY, PLAN, isolate, seed } from './plannerHarness'

/** The label is the accessible name, and it names both figures it covers --
 *  the base capacity on this row and the Trade Office bonus one disclosure in.
 *  A checkbox called "measured" would be an operator asserting they know not
 *  what. */
export const MEASURED = 'I read the base capacity and the bonus off the Marketplace send form'

const measured = (page) => page.getByRole('checkbox', { name: MEASURED })

/** A setup store that remembers what was PUT, so a save and the load after it
 *  are the same document rather than two fixtures that agree by hand. */
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
      await route.fulfill({ json: { saved_at: '2026-09-05T10:00:00Z' } })
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
      await route.fulfill({ json: { setup: state.saved, saved_at: '2026-09-05T10:00:00Z' } })
      return 'handled'
    }
    return undefined
  })
  return state
}

/** The Account stage, which carries the World & merchants row. */
async function openAccount(page) {
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Merchant base capacity')).toBeVisible()
}

/** What localStorage holds, which is the half a reload reads back. */
async function stored(page) {
  const raw = await page.evaluate(
    (key) => localStorage.getItem(`planner_merchant_measured::${key}`),
    KEY
  )
  return raw == null ? null : JSON.parse(raw)
}

test.describe('the measured-merchant-model acknowledgement', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('sits with the figures it is about, and says what ticking it does', async ({ page }) => {
    await isolate(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)

    // Unticked on a page nobody has touched: the finding's own default is
    // "never measured", and a box that arrived ticked would silence it for
    // every operator who has not looked.
    await expect(measured(page)).not.toBeChecked()

    // Its reasoning is one click away, like every other field on this page. A
    // native <summary>, so it is reached by its accessible name rather than by
    // a role: a <button> inside a <summary> would be a control inside a
    // control. Same locator `liveRunGuards.pw.js` uses for the run panel's four.
    const why = page.getByLabel(`Why: ${MEASURED}`)
    await expect(why).toHaveCount(1)
    await why.click()
    // The two sentences that matter: what the plan says without it, and that
    // ticking it moves no number.
    await expect(page.getByText(/never measured/i)).toBeVisible()
    await expect(page.getByText(/changes no number/i)).toBeVisible()
  })

  test('survives a reload on the same origin', async ({ page }) => {
    await isolate(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)

    await measured(page).check()
    await expect.poll(() => stored(page)).toBe(true)

    await page.reload()
    await openAccount(page)
    // Back to unticked after a reload is the defect: the operator's reading is
    // work done in the game, and the finding returns the moment it is lost.
    await expect(measured(page)).toBeChecked()
  })

  test('is written into the document, and the version rose for it', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)
    await measured(page).check()

    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(store.puts[0].merchant_model_measured).toBe(true)
    // Both halves of the bump, or a fresh export answers 422 "NEWER build".
    expect(store.puts[0].version).toBe(11)
  })

  test('comes back out of the store as the answer that was saved', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)
    await measured(page).check()
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()
    expect(store.saved).not.toBeNull()

    // A different origin is a fresh localStorage -- the failure the storage
    // panel two cards up warns about in words. Simulated by clearing the one
    // key, so the document is the only thing carrying the answer.
    await page.evaluate((key) => localStorage.removeItem(`planner_merchant_measured::${key}`), KEY)
    await page.reload()
    await page.getByRole('button', { name: 'Load setup from server' }).click()
    await expect(page.getByText(/from the saved setup/).first()).toBeVisible()

    await expect(measured(page)).toBeChecked()
  })

  test('a v10 document loads with the box unticked', async ({ page }) => {
    // A build that never wrote the field is not an operator who declined to
    // measure, but the two plan the same -- so the box has to be UNTICKED
    // rather than absent-and-forgotten, or the operator cannot see that the
    // finding is about to return.
    const DOC = {
      format: 'travian-planner-owned-state',
      version: 10,
      exported_at: '2026-09-05T04:00:00Z',
      account: KEY,
      villages: [{ village_id: CAPITAL, name: '02', trade_office_level: 13 }],
    }
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/setup') && route.request().method() === 'GET') {
        return { account_key: KEY, setup: DOC, saved_at: DOC.exported_at }
      }
      return undefined
    })
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Load setup from server' }).click()
    await expect(page.getByText(/from the saved setup/).first()).toBeVisible()

    await expect(measured(page)).not.toBeChecked()
  })

  test('a ticked box is content the empty-document guard counts', async ({ page }) => {
    // `setupDocument()` refuses to write a document with no content, because
    // "you have saved nothing" and "you saved a blank sheet" are different
    // states the server distinguishes. A reading taken in the game is content:
    // it is the one field in the document nothing could re-derive.
    const store = await isolateStore(page)
    await seed(page)
    await openAccount(page)

    // Nothing typed: the guard is reachable, which is what makes the assertion
    // after it mean something.
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/Nothing typed yet/)).toBeVisible()
    expect(store.puts).toHaveLength(0)

    await measured(page).check()
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()
    expect(store.puts).toHaveLength(1)
    expect(store.puts[0].merchant_model_measured).toBe(true)
  })
})
