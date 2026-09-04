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

test.describe('the gate on a live write is in the app, not the browser chrome', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  /** Every native dialog the page opens, which must be none. */
  function watchNativeDialogs(page) {
    const seen = []
    page.on('dialog', (dialog) => {
      seen.push(`${dialog.type()}: ${dialog.message().slice(0, 60)}`)
      dialog.dismiss()
    })
    return seen
  }

  test('the live-run manifest is an in-app dialog', async ({ page }) => {
    // `window.confirm` renders unstyled and theme-blind, cannot be re-read
    // after dismissal, and -- the part that matters -- Chrome's "Prevent this
    // page from creating additional dialogs" makes every later `confirm()`
    // return false SILENTLY. The live button then does nothing, with no
    // explanation, on the one action that writes to a real account.
    const natives = watchNativeDialogs(page)
    await isolate(page, (path) => (path.endsWith('/distribution/execute') ? PREVIEW : undefined))
    await seed(page)
    await openPlan(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()

    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    expect(natives, 'no native dialog was opened').toEqual([])
    // The manifest's own words, which are good and are kept.
    await expect(dialog).toContainText('Execute this plan against Travian now?')
    await expect(dialog).toContainText(/Create up to 1 new route/)
    await expect(dialog).toContainText(
      /If a create fails after a disable, old routes can remain disabled/,
    )
  })

  test('the manifest can be re-read after it is dismissed', async ({ page }) => {
    // The other thing a native dialog cannot do. Once dismissed its text is
    // gone, so an operator who wanted to check a count had to press the
    // irreversible button again to see it.
    await isolate(page, (path) => (path.endsWith('/distribution/execute') ? PREVIEW : undefined))
    await seed(page)
    await openPlan(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)

    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await expect(page.getByRole('dialog')).toContainText('Execute this plan against Travian now?')
  })

  test('cancelling writes nothing', async ({ page }) => {
    let executes = 0
    await isolate(page, (path) => {
      if (path.endsWith('/distribution/execute')) {
        executes += 1
        return PREVIEW
      }
      return undefined
    })
    await seed(page)
    await openPlan(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    expect(executes).toBe(1)
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    expect(executes, 'cancelling sent no second execute').toBe(1)
  })

  test('deleting a profile asks in the app', async ({ page }) => {
    const natives = watchNativeDialogs(page)
    await isolate(page)
    await seed(page, { planner_profiles: { Day: {}, Night: {} } })
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Delete' }).click()
    await expect(page.getByRole('dialog')).toContainText('Day')
    expect(natives).toEqual([])
  })

  test('naming a new profile asks in the app', async ({ page }) => {
    // `window.prompt` is suppressed by the same Chrome setting, and a
    // suppressed prompt returns null -- so "+ New" silently does nothing.
    const natives = watchNativeDialogs(page)
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: '+ New' }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    expect(natives).toEqual([])

    await dialog.getByRole('textbox').fill('Night')
    await dialog.getByRole('button', { name: /^(Create|Add|Confirm)/ }).click()
    await expect(page.getByLabel('Allocation profile')).toHaveValue('Night')
  })
})
