/**
 * The row count the operator is told about is the one the account is LEFT with.
 *
 * `ExecuteResponse` reports two figures and they are not interchangeable:
 *
 *   * `created_game_rows` is what the CREATES made, counted from the read-back
 *     that immediately follows them -- deliberately BEFORE this run's window
 *     trim, because it answers "did the game fan this request out the way the
 *     24/N model says", which is the only question that read-back can settle;
 *   * `live_game_rows` is what the run LEFT on the marketplace: the same
 *     measurement less the departures the trim actually removed, discounted
 *     only where the trim's own read-back confirmed they had gone.
 *
 * The page printed the first one under the word "Put", so a whole-day run at a
 * budget of 16 rows reported "Put 42 route row(s) in the game" over an account
 * holding 16. Forty-two is a true sentence about the writes and a false one
 * about the account, and the account is what the operator has to go and delete
 * by hand. The FOOTPRINT is the headline now; the pre-trim figure stays as the
 * explanation of what was trimmed, which is the only thing it can honestly say
 * on its own.
 *
 * `max_game_rows_per_run` is charged in the surviving unit too -- the live path
 * defers a route when `rows_written + would_add > row_cap`, and `would_add` is
 * `_rows_that_survive(...)`. The DRY RUN never consults it: it caps at
 * `max_routes_per_run` alone (`items[:cap]`) and forecasts the fan-out of that
 * slice. So a preview above the budget is not a refusal and must not read like
 * one -- it is the live run announcing that it will defer.
 *
 * NO BACKEND AND NO GAME REQUEST: every call is answered from a fixture or
 * aborted fail-closed. There is a live Travian account on this machine.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test rowFootprint
 */

import { expect, test } from '@playwright/test'

import { PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** A preview whose forecast fan-out is trimmed down to a smaller footprint:
 *  42 rows written, 16 left, which is the whole-day shape that was reported. */
const TRIMMED_PREVIEW = {
  ...PREVIEW,
  created_game_rows: 42,
  live_game_rows: 16,
}

/** The same run, live. `created` is the verified create count and is unrelated
 *  to either row figure -- one request became forty-two rows. */
const TRIMMED_LIVE = {
  ...TRIMMED_PREVIEW,
  dry_run: false,
  created: 1,
  actions: [{ ...PREVIEW.actions[0], status: 'created', detail: 'route 9001' }],
}

/** A round-the-clock route set: nothing departs outside the hours, so the trim
 *  removes nothing and the two figures agree. */
const UNTRIMMED_LIVE = {
  ...TRIMMED_LIVE,
  created_game_rows: 24,
  live_game_rows: 24,
}

/** The paragraph under test, found by the sentence only it carries. */
const footprint = (page) =>
  page.locator('p').filter({ hasText: /route row\(s\) in the game/ })

async function previewOf(page, response) {
  await isolate(page, (path) => (path.endsWith('/distribution/execute') ? response : undefined))
  await seed(page)
  await openPlan(page)
  await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
  await expect(footprint(page)).toBeVisible()
}

async function liveRunOf(page, response) {
  await isolate(page, (path, route) =>
    path.endsWith('/distribution/execute')
      ? route.request().postDataJSON().dry_run
        ? TRIMMED_PREVIEW
        : response
      : undefined,
  )
  await seed(page)
  await openPlan(page)
  await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
  await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
  await page.getByRole('button', { name: /^Go live/ }).click()
  await expect(page.getByText(/^Last live trade-route run/)).toBeVisible()
}

test.describe('the row count reported is the one the account is left holding', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('a live run headlines the held count, not the pre-trim count', async ({ page }) => {
    await liveRunOf(page, TRIMMED_LIVE)

    // The reported defect, in one assertion: 42 was the headline over an
    // account holding 16.
    await expect(footprint(page)).toContainText('This run leaves 16 route row(s) in the game')
    await expect(footprint(page)).not.toContainText(
      /(leaves|Put) 42 route row\(s\) in the game/,
    )
  })

  test('the pre-trim figure survives as the account of what was trimmed', async ({ page }) => {
    await liveRunOf(page, TRIMMED_LIVE)

    // Both numbers, each doing the one job it can do: 42 is what the writes
    // made, 26 is what the trim took back off.
    await expect(footprint(page)).toContainText('42 row(s)')
    await expect(footprint(page)).toContainText('26')
    await expect(footprint(page)).toContainText(/measured/)
  })

  test('a run the trim did not touch says so rather than dropping the sentence', async ({
    page,
  }) => {
    await liveRunOf(page, UNTRIMMED_LIVE)

    await expect(footprint(page)).toContainText('This run leaves 24 route row(s) in the game')
    // Not silence: a trim that removed nothing and a trim that silently failed
    // are the same reading here, and the honest word is what the read-back
    // confirmed -- none.
    await expect(footprint(page)).toContainText(/none of them/)
  })

  test('a preview headlines the forecast footprint and labels both as forecasts', async ({
    page,
  }) => {
    await previewOf(page, TRIMMED_PREVIEW)

    await expect(footprint(page)).toContainText('Would leave 16 route row(s) in the game')
    await expect(footprint(page)).toContainText('42 row(s)')
    await expect(footprint(page)).toContainText(/forecast/)
    // Nothing was read back, so nothing here may claim it was.
    await expect(footprint(page)).not.toContainText(/was measured/)
  })

  test('a preview above the row budget reads as deferral, not as a refusal', async ({ page }) => {
    await previewOf(page, TRIMMED_PREVIEW)

    // 16 forecast rows against a budget of 10. The dry run is capped by
    // `max_routes_per_run` alone, so this is the live run's plan, not an error.
    await page.getByLabel('Max rows this run', { exact: true }).fill('10')
    await expect(footprint(page)).toContainText(/defers the rest to a later run/)
    await expect(footprint(page)).not.toContainText(/refus|cannot|too many/i)
  })

  test('a preview inside the row budget says nothing about it', async ({ page }) => {
    await previewOf(page, TRIMMED_PREVIEW)

    await page.getByLabel('Max rows this run', { exact: true }).fill('40')
    await expect(footprint(page)).not.toContainText(/defers the rest/)
  })
})
