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

import { CAPITAL, DEF_A, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

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

/** 241664's circuit breaker, verbatim: once one marketplace has stopped
 *  agreeing with itself, every later verdict this run would reach rests on a
 *  page that would not hold still, so the run stops before the next village. */
const BREAKER =
  'marketplace reads disagreed at 02; no further villages were written to — re-run once ' +
  'the marketplace is quiet'

test.describe('the sweep stops when the run has stopped', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  /** One chunk that trips the breaker, with villages still outstanding and a
   *  pause the loop would otherwise sit through before asking again. */
  const TRIPPED = {
    ...LIVE,
    swept_origins: [CAPITAL],
    unswept_origins: [DEF_A],
    next_chunk_wait_seconds: 1,
    remaining: 0,
    problems: [BREAKER],
  }

  async function sweep(page) {
    const calls = { n: 0 }
    await isolate(page, async (path, route) => {
      if (!path.endsWith('/distribution/execute')) return undefined
      const body = route.request().postDataJSON()
      if (body.execution_mode !== 'live') {
        await route.fulfill({ json: PREVIEW })
        return 'handled'
      }
      calls.n += 1
      await route.fulfill({ json: TRIPPED })
      return 'handled'
    })
    await seed(page)
    await openPlan(page)
    await page.getByRole('button', { name: 'Reconcile all villages' }).click()
    return calls
  }

  test('the breaker line is printed whole', async ({ page }) => {
    await sweep(page)

    await expect(page.getByRole('listitem').filter({ hasText: BREAKER })).toBeVisible()
  })

  test('and no further chunk is requested', async ({ page }) => {
    const calls = await sweep(page)

    // The button coming back IS the loop having ended: while it is sweeping it
    // reads "Stop after this chunk". Without the break the loop pauses one
    // second and asks the same village set again -- for ever, against a
    // marketplace the server has already said it will not write to.
    await expect(page.getByRole('button', { name: 'Reconcile all villages' })).toBeVisible({
      timeout: 15_000,
    })
    expect(calls.n).toBe(1)

    // And the progress line says why it stopped, rather than reading as a
    // sweep that merely ran out of villages per chunk.
    await expect(page.getByText(/STOPPED — the marketplace stopped agreeing with itself/)).toBeVisible()
  })

  test('an ordinary problem does not stop the sweep', async ({ page }) => {
    // The breaker is the one problem that means the run is over. A Gold Club
    // refusal at one village is not, and chunking must survive it.
    const calls = { n: 0 }
    await isolate(page, async (path, route) => {
      if (!path.endsWith('/distribution/execute')) return undefined
      const body = route.request().postDataJSON()
      if (body.execution_mode !== 'live') {
        await route.fulfill({ json: PREVIEW })
        return 'handled'
      }
      calls.n += 1
      await route.fulfill({
        json:
          calls.n === 1
            ? { ...TRIPPED, problems: [REFUSED] }
            : { ...TRIPPED, problems: [], unswept_origins: [], next_chunk_wait_seconds: 0 },
      })
      return 'handled'
    })
    await seed(page)
    await openPlan(page)
    await page.getByRole('button', { name: 'Reconcile all villages' }).click()

    await expect(page.getByText(/COMPLETE — nothing stale left/)).toBeVisible({ timeout: 15_000 })
    expect(calls.n).toBe(2)
  })
})
