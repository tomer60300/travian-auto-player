/**
 * A `problems` entry is a sentence, and the page must print it as one.
 *
 * `ExecuteResponse.problems` is `list[str]` and nothing more: the server
 * composes each line and the page has never been entitled to parse one. 53ab710
 * added a shape that would have caught a page which did — "<village>: two reads
 * of this marketplace did not agree (…), so nothing was deleted here …", built
 * around `village_label(origin, names)` in the verify block of `execute_routes`
 * — because it is the one problem that reports a delete NOT happening, and it
 * carries a trailing sentence about the rows left in place.
 *
 * Two surfaces, and they were not equal. The result panel renders every string
 * verbatim. The reconciliation sweep collected them, counted them, put the
 * FIRST into a toast that closes itself, and rendered the rest nowhere — and the
 * sweep is a live disabling run, so it is exactly where this line arises. The
 * count said "1 problem(s)" and the sentence explaining that nothing was deleted
 * was only ever in the Activity Log.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test problemLines
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** The line as the server composes it, verbatim -- prefix, parenthetical,
 *  em dash and the trailing sentence about the strays. */
const UNSETTLED =
  '02 (0|0): two reads of this marketplace did not agree (1 row appeared, 2 changed ' +
  'minute), so nothing was deleted here — a delete on an unsettled page can remove a ' +
  'row this run had just made. Row(s) [9101, 9102] depart outside the profile hours ' +
  'and were left in place.'

/** A second entry, so a surface that shows only the first is caught. */
const REFUSED = '11 (4|0): the Gold Club refused this route'

const LIVE = {
  ...PREVIEW,
  dry_run: false,
  created: 1,
  actions: [{ ...PREVIEW.actions[0], status: 'created', detail: 'route 9001' }],
  problems: [UNSETTLED, REFUSED],
}

/** One chunk of sweep, finished on the first pass, carrying both problems. */
const SWEPT = {
  ...LIVE,
  swept_origins: [CAPITAL],
  unswept_origins: [],
  next_chunk_wait_seconds: 0,
  remaining: 0,
}

async function arrive(page, live) {
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    const body = route.request().postDataJSON()
    await route.fulfill({ json: body.execution_mode === 'live' ? live : PREVIEW })
    return 'handled'
  })
  await seed(page)
  await openPlan(page)
}

test.describe('every problem the run reports is legible on the page', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the result panel prints an unfamiliar line whole', async ({ page }) => {
    await arrive(page, LIVE)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()

    // Whole, and not summarised: this line's SECOND sentence names the rows the
    // run chose to leave alone, which is the part an operator has to act on.
    await expect(page.getByRole('listitem').filter({ hasText: UNSETTLED })).toBeVisible()
    await expect(page.getByRole('listitem').filter({ hasText: REFUSED })).toBeVisible()
  })

  test('the sweep prints them too, rather than counting them', async ({ page }) => {
    await arrive(page, SWEPT)

    await page.getByRole('button', { name: 'Reconcile all villages' }).click()
    await expect(page.getByText(/2 problem\(s\)/)).toBeVisible()

    // The count was all there was. A sweep that disabled nothing at a village
    // and said so only in a toast is a sweep the operator reads as clean.
    await expect(page.getByRole('listitem').filter({ hasText: UNSETTLED })).toBeVisible()
    await expect(page.getByRole('listitem').filter({ hasText: REFUSED })).toBeVisible()
  })
})
