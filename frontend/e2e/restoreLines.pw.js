/**
 * `re_enables` reached the page and was rendered nowhere.
 *
 * `ExecuteResponse.re_enables` (`src/travian_api/web/routes/distribution.py`)
 * is kept apart from `disables` deliberately -- its own comment says folding
 * them together "reported a resumed route as a stopped one" -- and the page
 * counted it into a toast (`, N re-enabled`) and into the persisted run record,
 * and printed not one of the sentences. `disables` and `updates` both get a
 * list; this did not.
 *
 * Two shapes arrive on it, and fe84298 added the second:
 *
 *   * `"<origin>: re-enabled <detail>"` -- a route the plan still wants that
 *     was found switched off, started again.
 *   * `"<origin> -> <destination>: restored N disabled row(s) after the
 *     replacement was refused[; M were already back on]"` -- a destination the
 *     run emptied to rebuild, whose replacement the game refused, switched back
 *     on by the run itself against its own write-ahead record.
 *
 * The second is the one the copy has to get right. Nothing was created for that
 * destination and nothing about it is new: it is running the schedule it was
 * running before this run touched it. Reported under a heading that implied a
 * new route, it would read as a route the operator now holds -- and the whole
 * point of `docs/26-first-live-run.md` §2's restoration paragraph is that the
 * by-hand put-back is for what the run could NOT reverse itself.
 *
 * Which is also why `RevertRunPanel` is asserted here: its `restore_state`
 * heading is "Routes this run switched, to put back", and a restored row is
 * precisely one that is NOT in that list -- the run already put it back. The
 * two surfaces must not describe the same run differently.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test restoreLines
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, DEF_A, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** The restore line as `execute_routes` composes it: `village_label` is the
 *  bare name, the counts are the rows this REQUEST switched, and the trailing
 *  clause appears only when the dual had already put some back. */
const RESTORED =
  '02 -> 11: restored 7 disabled row(s) after the replacement was refused; 1 were already ' +
  'back on'

/** The plain form, from the disabled-desired branch further up the same run. */
const RESUMED = '02: re-enabled route(s) [8800, 8801] switched back on'

/** A disable, so "kept apart from disables" is asked as a comparison. */
const DISABLED = '02: disabled route(s) [7700]'

const LIVE = {
  ...PREVIEW,
  dry_run: false,
  created: 1,
  remaining: 0,
  actions: [{ ...PREVIEW.actions[0], status: 'created', detail: 'route 9001' }],
  disables: [DISABLED],
  re_enables: [RESTORED, RESUMED],
}

async function goLive(page) {
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    const body = route.request().postDataJSON()
    await route.fulfill({ json: body.execution_mode === 'live' ? LIVE : PREVIEW })
    return 'handled'
  })
  await seed(page)
  await openPlan(page)
  await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
  await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
  await page.getByRole('button', { name: /^Go live/ }).click()
}

test.describe('a restored destination is back where it started', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('both re-enable shapes are printed whole, under their own heading', async ({ page }) => {
    await goLive(page)

    await expect(page.getByText('2 route(s) switched back on:')).toBeVisible()
    // Whole: the trailing "; 1 were already back on" is the half that says the
    // counts in the line are the rows this REQUEST changed, not the total.
    await expect(page.getByRole('listitem').filter({ hasText: RESTORED })).toBeVisible()
    await expect(page.getByRole('listitem').filter({ hasText: RESUMED })).toBeVisible()
  })

  test('the copy says nothing new was created for them', async ({ page }) => {
    await goLive(page)

    // A resumed route is the OPPOSITE of a stopped one, and a restored
    // destination is neither: it is the schedule it was already running.
    await expect(page.getByText(/Nothing new was created for (them|these)/)).toBeVisible()
    await expect(page.getByText(/back where it started/)).toBeVisible()
  })

  test('a disable is still a disable', async ({ page }) => {
    await goLive(page)

    // The count over the re-enables must not swallow the disable list, and the
    // disable must not appear under "switched back on".
    await expect(page.getByRole('listitem').filter({ hasText: DISABLED })).toBeVisible()
    await expect(page.getByText('1 route(s) switched back on:')).toHaveCount(0)
  })

  test('a run that re-enabled nothing shows no heading at all', async ({ page }) => {
    await isolate(page, async (path, route) => {
      if (!path.endsWith('/distribution/execute')) return undefined
      const body = route.request().postDataJSON()
      await route.fulfill({
        json: body.execution_mode === 'live' ? { ...LIVE, re_enables: [] } : PREVIEW,
      })
      return 'handled'
    })
    await seed(page)
    await openPlan(page)
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()

    await expect(page.getByRole('listitem').filter({ hasText: DISABLED })).toBeVisible()
    await expect(page.getByText(/switched back on:/)).toHaveCount(0)
  })

  test('at 375 the lines wrap rather than scrolling the page sideways', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 900 })
    await goLive(page)

    await expect(page.getByRole('listitem').filter({ hasText: RESTORED })).toBeVisible()
    const scrollX = await page.evaluate(
      () => document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth
    )
    expect(scrollX, 'no horizontal page scroll').toBe(0)
  })
})

test.describe('the destination and the origin are named as the server named them', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('nothing on the page parses the line', async ({ page }) => {
    // An unfamiliar shape prints whole, the same way `problemLines.pw.js` asks
    // it of `problems`: `re_enables` is `list[str]` and nothing more.
    const ODD = `${CAPITAL} -> ${DEF_A}: something the page has never seen before`
    await isolate(page, async (path, route) => {
      if (!path.endsWith('/distribution/execute')) return undefined
      const body = route.request().postDataJSON()
      await route.fulfill({
        json: body.execution_mode === 'live' ? { ...LIVE, re_enables: [ODD] } : PREVIEW,
      })
      return 'handled'
    })
    await seed(page)
    await openPlan(page)
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()

    await expect(page.getByRole('listitem').filter({ hasText: ODD })).toBeVisible()
  })
})
