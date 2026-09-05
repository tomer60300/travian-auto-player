/**
 * The bill the preview quotes, and the read the operator never asked for.
 *
 * `ExecuteResponse.requests_forecast` gained `stabilise_reads` in 53ab710: a
 * delete now needs TWO agreeing reads of the marketplace before it fires,
 * because a page still settling can show a row this very run had just made as
 * one to remove. So every origin whose creates fan out past its window pays
 * THREE requests to trim rather than two, and `estimated_total` moved with it
 * (creates + reads + verify reads + 3 per trimming origin).
 *
 * The page quoted the old bill: four of the six terms, no stabilising read, and
 * a breakdown that no longer summed to the total beside it. An operator
 * comparing the quote against what the run actually spent would have found it
 * short by two per trimming village, on the one screen whose whole job is
 * saying what going live costs.
 *
 * Backend twin: the `requests_forecast` dict built on the `execution_mode ==
 * "preview"` branch of `execute_routes` in
 * `src/travian_api/web/routes/distribution.py`.
 *
 * NO BACKEND AND NO GAME REQUEST: `isolate` answers `/distribution/execute`
 * from this file and ABORTS anything it does not recognise.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test requestsForecast
 */

import { expect, test } from '@playwright/test'

import { PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** A forecast with every term distinct, so no assertion can match by accident.
 *  9 + 4 + 4 + 3 * 2 = 23, and 23 + 9 + 4 - 2 = 34. */
const TRIMMING = {
  ...PREVIEW,
  requests_forecast: {
    marketplace_reads: 9,
    creates: 4,
    verify_reads: 4,
    stabilise_reads: 2,
    trim_deletes: 2,
    trim_verify_reads: 2,
    estimated_total: 23,
    estimated_total_max: 34,
  },
}

/** The same run with nothing to trim: no origin overruns its window, so no
 *  stabilising read is bought and the sentence about them has no subject. */
const UNTRIMMED = {
  ...PREVIEW,
  requests_forecast: {
    marketplace_reads: 9,
    creates: 4,
    verify_reads: 4,
    stabilise_reads: 0,
    trim_deletes: 0,
    trim_verify_reads: 0,
    estimated_total: 17,
    estimated_total_max: 30,
  },
}

/** The paragraph that quotes the bill, found by the phrase only it carries. */
const bill = (page) => page.locator('p').filter({ hasText: /^Going live spends/ })

/** The line about the stabilising reads, which stands on its own. Matched on
 *  its opening clause and not on the words "stabilising read", because the bill
 *  above it names the term too -- that is the point of the two together. */
const steadying = (page) => page.locator('p').filter({ hasText: /of those are/ })

async function previewOf(page, response) {
  await isolate(page, (path) => (path.endsWith('/distribution/execute') ? response : undefined))
  await seed(page)
  await openPlan(page)
  await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
  await expect(bill(page)).toBeVisible()
}

test.describe('the forecast quotes every request the run will spend', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the stabilising reads get a line of their own, and it says why', async ({ page }) => {
    await previewOf(page, TRIMMING)

    const line = steadying(page)
    await expect(line).toBeVisible()
    // The reason, not just the count: a number nobody can account for reads as
    // overhead, and this one is the difference between a delete that is safe
    // and one that can remove a row this run had just made.
    await expect(line).toContainText('2')
    await expect(line).toContainText(/before any trim/)
    await expect(line).toContainText(/disagrees with/)
  })

  test('the breakdown adds up to the estimate it is a breakdown of', async ({ page }) => {
    await previewOf(page, TRIMMING)

    const text = await bill(page).innerText()
    // Every one of the six terms, so the arithmetic is checkable on screen.
    // `verify_reads`, `stabilise_reads`, `trim_deletes` and `trim_verify_reads`
    // are 4, 2, 2 and 2 here precisely so a missing term cannot be covered for
    // by a term that happens to carry the same figure.
    expect(text).toMatch(/~23/)
    expect(text).toMatch(/34/)
    expect(text).toMatch(/9 read/)
    expect(text).toMatch(/4 create/)
    expect(text).toMatch(/4 verify read/)
    expect(text).toMatch(/2 stabilising read/)
    expect(text).toMatch(/2 trim/)
    expect(text).toMatch(/2 confirming read/)

    const summed = 9 + 4 + 4 + 2 + 2 + 2
    expect(summed).toBe(TRIMMING.requests_forecast.estimated_total)
  })

  test('a run with nothing to trim does not explain a read it never buys', async ({ page }) => {
    await previewOf(page, UNTRIMMED)

    await expect(bill(page)).toBeVisible()
    await expect(steadying(page)).toHaveCount(0)
  })
})
