/**
 * The guards on a live write, DRIVEN — what the default run authorises, and
 * what the page says about what was typed.
 *
 * Each case here is a defect the audit reached by driving the real page. They
 * share one surface (the Plan stage's controlled-run bar) and one property: the
 * page has every fact it needs to stop the operator being surprised, and was
 * not using it.
 *
 * Asserted on REQUEST BODIES and on rendered warnings, never on a box's own
 * value: a box showing the right number confirms React re-rendered without
 * confirming what it sends.
 *
 * NO BACKEND AND NO GAME REQUEST: `plannerHarness.isolate` answers what the
 * shell asks for and aborts everything else fail-closed, and the only execute
 * path driven is the PREVIEW, which is `dry_run: true` and mocked besides.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test liveRunGuards
 */

import { expect, test } from '@playwright/test'

import { PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** Drive a preview and hand back the body `/distribution/execute` was sent. */
async function previewBody(page) {
  let body = null
  await isolate(page, (path, route) => {
    if (path.endsWith('/distribution/execute')) {
      body = route.request().postDataJSON()
      return PREVIEW
    }
    return undefined
  })
  await seed(page)
  await openPlan(page)
  await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
  await expect(page.getByText(/route\(s\) would be created/)).toBeVisible()
  return body
}

test.describe('the row cap the page explains is on by default', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('a default run bounds ROWS, not only routes', async ({ page }) => {
    // The defect: `max_routes_per_run` defaulted to 3 and
    // `max_game_rows_per_run` was absent, so the server's own default of 0 --
    // unbounded -- applied. Travian turns one "repeat every N hours" request
    // into 24/N daily rows, so three routes on a one-hour cycle is
    // seventy-two rows, and the page's own copy calls a row "the unit the
    // operator actually authorises".
    const body = await previewBody(page)

    expect(body.max_routes_per_run).toBe(3)
    expect(
      body.max_game_rows_per_run,
      'the row cap travels rather than falling through to the server default of unbounded',
    ).toBeGreaterThan(0)
  })

  test('the box says what blank means, in the box', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    const box = page.getByLabel('Max rows this run')
    // A default the operator can see and change, not an empty box over a
    // sentence in a label.
    await expect(box).not.toHaveValue('')
    await expect(box).toHaveAttribute('placeholder', 'no limit')
  })

  test('clearing the box still means no limit', async ({ page }) => {
    // Blank has to stay unbounded. The cap is a default, not a new floor: an
    // operator who deliberately wants a whole-day provisioning pass must be
    // able to say so, and the way they say it is by emptying the box.
    let body = null
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/execute')) {
        body = route.request().postDataJSON()
        return PREVIEW
      }
      return undefined
    })
    await seed(page)
    await openPlan(page)
    await page.getByLabel('Max rows this run').fill('')
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect(page.getByText(/route\(s\) would be created/)).toBeVisible()

    expect(body).not.toBeNull()
    expect('max_game_rows_per_run' in body).toBe(false)
  })
})

test.describe('a protect_destinations typo is undetectable by the server', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('a bare integer no village has is named back, with the coordinate reading', async ({
    page,
  }) => {
    // Driven before the fix: typing 4688 sent `protect_destinations: ["4688"]`
    // with no inline warning at all. It is shape-valid as a village id, so the
    // server's `_protected_entries_are_parseable` passes it -- and the server
    // cannot do better, because it does not hold this account's village list.
    await isolate(page)
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable').fill('4688')
    await expect(page.getByText('no village named 4688 — did you mean 46|88?')).toBeVisible()
  })

  test('coordinates are never flagged, however foreign', async ({ page }) => {
    // The routes worth protecting are the hand-made ones to targets this
    // account does not own, so a pair matching no village is the normal case.
    await isolate(page)
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable').fill('46|133')
    await expect(page.getByText(/no village named/)).toHaveCount(0)
  })

  test('a real village id is left alone', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable').fill('20011')
    await expect(page.getByText(/no village named/)).toHaveCount(0)
  })

  test('the warning does not block the run, because the entry may be right', async ({ page }) => {
    // A foreign village id pasted from a Travian link is legitimate and is not
    // in the snapshot. So this is a warning and not a gate -- the same call the
    // foreign-target exclusion field makes.
    let body = null
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/execute')) {
        body = route.request().postDataJSON()
        return PREVIEW
      }
      return undefined
    })
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable').fill('4688')
    await expect(page.getByText(/no village named 4688/)).toBeVisible()
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect(page.getByText(/route\(s\) would be created/)).toBeVisible()

    expect(body.protect_destinations).toEqual(['4688'])
  })
})
