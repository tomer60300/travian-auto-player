/**
 * Saving a setup the page itself refuses to plan from.
 *
 * `Build plan` has refused a marked cell since `plannerBlockers.js` -- the same
 * predicates the cells use, so a mark and a refusal cannot come from two rules.
 * The two SAVE controls beside it were gated on `busy` alone, so a document
 * carrying an even `map_span` or a `0` speed went to the server with a 200 and
 * could never be loaded again: `parseSetup` refuses both on the way back in, and
 * refuses the whole file rather than half-loading it. Export wrote the same file
 * to disk.
 *
 * That is the worst shape the loss can take -- the operator's only backup is a
 * document their own build will not read -- so the two writers are held back
 * while any cell is marked, and the panel says which cell.
 *
 * Loading is deliberately NOT gated. A load is the way OUT of a bad state.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test setupSaveGate
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolate, seed } from './plannerHarness'

/** A model the page marks: a world is centred on 0|0, so its width is odd. */
const EVEN_SPAN = {
  base_capacity: 2500,
  bonus_per_to_level: 0.2,
  merchant_reserve: 2,
  merchant_headroom: 0.1,
  map_span: 400,
}

const saveToServer = (page) => page.getByRole('button', { name: 'Save setup to server' })
const saveToFile = (page) => page.getByRole('button', { name: 'Save setup to file' })
const loadFromFile = (page) => page.getByRole('button', { name: 'Load setup from file' })

async function isolateCounting(page) {
  const puts = []
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/setup') && route.request().method() === 'PUT') {
      puts.push(route.request().postDataJSON())
      await route.fulfill({ json: { saved_at: new Date().toISOString() } })
      return 'handled'
    }
    return undefined
  })
  return puts
}

async function openAccountStage(page) {
  await page.goto('/resource-planner')
  await expect(saveToServer(page)).toBeVisible()
}

test.describe('a setup the plan refuses is not saved either', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('an even map span holds both writers back', async ({ page }) => {
    const puts = await isolateCounting(page)
    await seed(page, {
      planner_merchant_model: EVEN_SPAN,
      planner_trade_office: { [CAPITAL]: 10 },
    })
    await openAccountStage(page)

    await expect(saveToServer(page)).toBeDisabled()
    await expect(saveToFile(page)).toBeDisabled()
    expect(puts).toHaveLength(0)
  })

  test('the disabled controls name the cell rather than only greying out', async ({ page }) => {
    await isolateCounting(page)
    await seed(page, {
      planner_merchant_model: EVEN_SPAN,
      planner_trade_office: { [CAPITAL]: 10 },
    })
    await openAccountStage(page)

    // A disabled control with no reason beside it is the defect this replaces:
    // the operator pressed Save, nothing happened, and nothing said why.
    const reason = page.getByRole('status').filter({ hasText: /Map span/ })
    await expect(reason).toBeVisible()
    await expect(reason).toContainText('odd')
    // Named to a screen reader too, not only to the eye.
    await expect(saveToServer(page)).toHaveAttribute('aria-describedby', /.+/)
    await expect(saveToFile(page)).toHaveAttribute('aria-describedby', /.+/)
  })

  test('loading is left alone, because a load is the way out', async ({ page }) => {
    await isolateCounting(page)
    await seed(page, {
      planner_merchant_model: EVEN_SPAN,
      planner_trade_office: { [CAPITAL]: 10 },
    })
    await openAccountStage(page)

    await expect(loadFromFile(page)).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Paste setup' })).toBeEnabled()
  })

  test('a clean page saves exactly as it did before', async ({ page }) => {
    const puts = await isolateCounting(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 10 } })
    await openAccountStage(page)

    await expect(saveToServer(page)).toBeEnabled()
    await expect(saveToFile(page)).toBeEnabled()
    await expect(page.getByRole('status').filter({ hasText: /Map span/ })).toHaveCount(0)

    await saveToServer(page).click()
    await expect.poll(() => puts.length).toBe(1)
    expect(puts[0].merchant_model.map_span).toBeUndefined()
  })

  test('a merchant cap past the fleet holds them back as well', async ({ page }) => {
    // A second, unrelated predicate: the gate is the whole blocker list and not
    // one special-cased field.
    await isolateCounting(page)
    await seed(page, { planner_max_busy: { [CAPITAL]: 99 } })
    await openAccountStage(page)

    await expect(saveToServer(page)).toBeDisabled()
    await expect(saveToFile(page)).toBeDisabled()
  })
})
