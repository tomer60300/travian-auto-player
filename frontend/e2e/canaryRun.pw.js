/**
 * The first live run, as a control the server enforces rather than a checklist.
 *
 * `ExecuteRequest.canary` and `_canary_is_the_smallest_live_run`
 * (`src/travian_api/web/routes/distribution.py`, 671d8ce) refuse the request
 * unless every one of `docs/26-first-live-run.md` §2's eight conditions holds,
 * naming the one that failed. Seven are decided by the request alone; the
 * eighth -- `max_game_rows_per_run` equal to that one route's own fan-out -- is
 * checked in `post_execute` against the PLAN, because 24/N comes from the
 * route's cycle and "1 route" was never "1 row".
 *
 * §2 said "the page has no canary tick yet -- set the controls above by hand
 * and send `canary: true` with them", which is a protocol asking an operator to
 * get eight things right by hand on the one endpoint that writes to a real
 * account. This is the tick. It presets and locks the five controls the flag
 * fixes, computes the row budget off the plan's single route for the chosen
 * pair, shows the eight conditions with the unmet ones named, and holds the red
 * button until they all hold -- so a refusal is something the operator reads
 * before spending a request, not after.
 *
 * The server stays the authority. The page can be wrong about a condition and
 * the 422 is still the answer, so it is rendered verbatim where the operator
 * asked rather than only in a toast that closes itself.
 *
 * Preview never carries the flag: a preview writes nothing, so there is nothing
 * for it to make safe, and the validator refuses `canary` without
 * `execution_mode: "live"` outright.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test canaryRun
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, DEF_A, PLAN, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** The harness plan ships ONE route, 02 -> 11, on a 4h cycle. 24 / 4 = 6 rows,
 *  which is the only figure `max_game_rows_per_run` may carry on this canary. */
const FAN_OUT = 6

const CANARY = 'Canary: one route, create only'

const LIVE = {
  ...PREVIEW,
  dry_run: false,
  created: 1,
  actions: [{ ...PREVIEW.actions[0], status: 'created', detail: 'route 9001' }],
  trace_id: 'canary-trace',
  // The undo list: one new write, reversible by switching exactly these ids
  // off. A list of FAN_OUT ids, because 24/4h is 6 daily rows from one create.
  canary_rows_created: [9001, 9002, 9003, 9004, 9005, 9006],
}

/** Captures every `/distribution/execute` body, and answers by mode. */
async function arrive(page, { live = LIVE, refuse = null } = {}) {
  const sent = []
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    const body = route.request().postDataJSON()
    sent.push(body)
    if (body.execution_mode !== 'live') {
      await route.fulfill({ json: PREVIEW })
      return 'handled'
    }
    if (refuse) {
      await route.fulfill({ status: 422, json: { detail: refuse } })
      return 'handled'
    }
    await route.fulfill({ json: live })
    return 'handled'
  })
  await seed(page)
  await openPlan(page)
  return sent
}

const tick = (page) => page.getByRole('checkbox', { name: CANARY })

async function pickPair(page, { origin = String(CAPITAL), destination = String(DEF_A) } = {}) {
  if (origin !== null)
    await page.getByRole('spinbutton', { name: 'Only origin (village id)' }).fill(origin)
  if (destination !== null)
    await page
      .getByRole('spinbutton', { name: 'Only destination (village id)' })
      .fill(destination)
}

test.describe('the canary tick is the eight conditions, on the page', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('ticking it presets and locks the five controls the flag fixes', async ({ page }) => {
    await arrive(page)
    await tick(page).check()

    // Every value `_canary_is_the_smallest_live_run` refuses anything else for,
    // set for the operator instead of asked of them -- and locked, because a
    // control that can drift out from under a stated intent is the typo this
    // flag exists to remove.
    const routes = page.getByRole('spinbutton', { name: 'Routes this run' })
    await expect(routes).toHaveValue('1')
    await expect(routes).toBeDisabled()

    for (const name of [
      /^Also disable routes the plan no longer wants/,
      /^Whole day/,
      /^Trim the fan-out to the profile hours/,
      /^Correct cargo on routes that have drifted/,
    ]) {
      const box = page.getByRole('checkbox', { name })
      await expect(box).not.toBeChecked()
      await expect(box).toBeDisabled()
    }
  })

  test('the row budget comes off the plan’s own route, not off a default', async ({ page }) => {
    await arrive(page)
    await tick(page).check()
    await pickPair(page)

    // 24 / 4h. `max_routes_per_run: 1` never meant one row, and the footprint
    // is what would have to be deleted by hand if this goes wrong.
    await expect(
      page.getByRole('spinbutton', { name: 'Max rows this run' })
    ).toHaveValue(String(FAN_OUT))
  })

  test('a satisfied canary sends the flag and the eight values', async ({ page }) => {
    const sent = await arrive(page)
    await tick(page).check()
    await pickPair(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)
    await page.getByRole('button', { name: /^Create 1 route, disable nothing/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect.poll(() => sent.length).toBe(2)

    const body = sent[1]
    expect(body.canary).toBe(true)
    expect(body.execution_mode).toBe('live')
    expect(body.only_origins).toEqual([CAPITAL])
    expect(body.only_destinations).toEqual([DEF_A])
    expect(body.max_routes_per_run).toBe(1)
    expect(body.disable_existing).toBe(false)
    expect(body.update_drifted).toBe(false)
    expect(body.max_game_rows_per_run).toBe(FAN_OUT)
    // The two the payload states by ABSENCE, which is what the validator reads:
    // `prune_to_window` is only sent when the trim is on, and `segments` only on
    // a whole-day run. Both falsy, both refused if true.
    expect(body.prune_to_window ?? false).toBe(false)
    expect(body.segments ?? null).toBe(null)
  })

  test('a preview never carries the flag', async ({ page }) => {
    const sent = await arrive(page)
    await tick(page).check()
    await pickPair(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)

    // A preview writes nothing, so there is nothing for the flag to make safe --
    // and the validator refuses `canary` without `execution_mode: "live"`, so
    // sending it here would 422 the one action that costs nothing.
    expect(sent[0]).not.toHaveProperty('canary')
    expect(sent[0].execution_mode).toBe('preview')
    // And the preview still WORKS with the tick on: the run panel renders its
    // result, which is what §2 asks the operator to read before going live.
    await expect(page.getByText(/route\(s\) would be created/)).toBeVisible()
  })

  test('with no destination the button is held and the condition is named', async ({ page }) => {
    const sent = await arrive(page)
    await tick(page).check()
    await pickPair(page, { destination: null })

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)

    const go = page.getByRole('button', { name: /^Create 1 route, disable nothing/ })
    await expect(go).toBeDisabled()
    // Named, not merely greyed: "the button is off" is not a reason. Twice
    // over: in the checklist, with what it needs, and beside the held button.
    await expect(
      page
        .getByRole('listitem')
        .filter({ hasText: /Only destination \(village id\) — exactly one/ })
    ).toBeVisible()
    await expect(page.getByText(/Type one destination village id/)).toBeVisible()
    await expect(
      page.getByText(/Held by Canary: one route, create only: Only destination/)
    ).toBeVisible()
    expect(sent.length).toBe(1)
  })

  test('a pair the plan ships no single route for is named too', async ({ page }) => {
    // The condition the request alone cannot decide, and the server's own
    // wording for it: "pick a pair it ships exactly one route between".
    const sent = await arrive(page)
    await tick(page).check()
    await pickPair(page, { destination: String(CAPITAL) })

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)

    await expect(
      page.getByRole('button', { name: /^Create 1 route, disable nothing/ })
    ).toBeDisabled()
    await expect(page.getByText(/exactly one route between/)).toBeVisible()
  })

  test('unticking it gives the controls back', async ({ page }) => {
    await arrive(page)
    await tick(page).check()
    await tick(page).uncheck()

    await expect(page.getByRole('spinbutton', { name: 'Routes this run' })).toBeEnabled()
    await expect(
      page.getByRole('checkbox', { name: /^Also disable routes the plan no longer wants/ })
    ).toBeEnabled()
  })

  test('a satisfied checklist does not claim the server has nothing left to ask', async ({
    page,
  }) => {
    // 4c199b9 added a NINTH condition, and it is one the page cannot pre-check:
    // the origin's marketplace must not already hold rows for that destination,
    // or the read-back cannot say which rows the create made and
    // `canary_rows_created` -- the undo list -- cannot say which rows the undo
    // may switch off. That needs a game read, which nothing here has.
    //
    // So "all eight hold" must not read as "the server has nothing left to
    // refuse". The checklist is this page's half of the answer.
    await arrive(page)
    await tick(page).check()
    await pickPair(page)

    await expect(page.getByText(/All eight hold/)).toBeVisible()
    await expect(page.getByText(/already ship/)).toBeVisible()
  })

  test('a server refusal is printed where the operator asked', async ({ page }) => {
    const REFUSAL =
      'canary requires max_game_rows_per_run to equal this route’s own fan-out: a 4h cycle ' +
      'is 6 daily row(s), and the request authorised 24. Nothing was attempted.'
    const sent = await arrive(page, { refuse: REFUSAL })
    await tick(page).check()
    await pickPair(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)
    await page.getByRole('button', { name: /^Create 1 route, disable nothing/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect.poll(() => sent.length).toBe(2)

    // Verbatim, and on the page rather than only in a toast that closes itself:
    // the server is the authority on these eight, and its sentence names the
    // one that failed.
    await expect(page.getByRole('alert').filter({ hasText: REFUSAL })).toBeVisible()
  })
})

test.describe('the canary’s undo list, `canary_rows_created`', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  const HEADING = 'Rows this canary put in the game (the undo list):'

  test('a settled canary lists the rows under the undo heading', async ({ page }) => {
    const IDS = [101, 102, 103, 104]
    const sent = await arrive(page, { live: { ...LIVE, canary_rows_created: IDS } })
    await tick(page).check()
    await pickPair(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)
    await page.getByRole('button', { name: /^Create 1 route, disable nothing/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect.poll(() => sent.length).toBe(2)

    await expect(page.getByText(HEADING)).toBeVisible()
    for (const id of IDS) {
      await expect(page.locator('li', { hasText: new RegExp(`^${id}$`) })).toBeVisible()
    }
  })

  test('an empty list is the measurement that there is nothing to undo', async ({ page }) => {
    const sent = await arrive(page, { live: { ...LIVE, canary_rows_created: [] } })
    await tick(page).check()
    await pickPair(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)
    await page.getByRole('button', { name: /^Create 1 route, disable nothing/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect.poll(() => sent.length).toBe(2)

    // Still under the same heading -- `[]` is this heading's zero, stated as
    // a measurement rather than left as a bare "0".
    await expect(page.getByText(HEADING)).toBeVisible()
    await expect(
      page.getByText(/the create produced nothing, so there is nothing to undo/)
    ).toBeVisible()
  })

  test('null is a stop in the danger tone, never zero', async ({ page }) => {
    const sent = await arrive(page, { live: { ...LIVE, canary_rows_created: null } })
    await tick(page).check()
    await pickPair(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)
    await page.getByRole('button', { name: /^Create 1 route, disable nothing/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect.poll(() => sent.length).toBe(2)

    const stop = page
      .getByRole('alert')
      .filter({ hasText: /This run could not settle what it wrote/ })
    await expect(stop).toBeVisible()

    // The danger tone, measured against the token itself rather than assumed
    // from a class name -- `text-danger` flips between light and dark and a
    // hardcoded hex would only ever check one of them.
    const [stopColor, dangerColor] = await Promise.all([
      stop.evaluate((el) => getComputedStyle(el).color),
      page.evaluate(() => {
        const probe = document.createElement('span')
        probe.className = 'text-danger'
        document.body.appendChild(probe)
        const colour = getComputedStyle(probe).color
        probe.remove()
        return colour
      }),
    ])
    expect(stopColor).toBe(dangerColor)

    // Never "0 rows": the undo heading does not appear when the run could not
    // settle what it wrote, so there is no digit standing in for "nothing".
    await expect(page.getByText(HEADING)).not.toBeVisible()
  })
})

test.describe('the canary and the account-wide sweep are opposite runs', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the sweep is not one click away under a create-only tick', async ({ page }) => {
    await arrive(page)
    await tick(page).check()

    // "Reconcile all villages" is a live, disabling, account-wide run. It sends
    // no `canary`, so the server would not refuse it -- and it is the button
    // directly above this tick.
    await expect(page.getByRole('button', { name: 'Reconcile all villages' })).toBeDisabled()
  })
})

test.describe('the tick is reachable without a mouse', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('Space operates it, and it takes a focus ring', async ({ page }) => {
    await arrive(page)

    // Focus, then Space: the press is both the operability check (item 2 of the
    // UI Definition of Done) and what puts Chromium into keyboard modality, so
    // `:focus-visible` reports what a keyboard user would see. `openPlan` has
    // to click to get here, which is why the modality is established rather
    // than assumed.
    await tick(page).focus()
    await expect(tick(page)).toBeFocused()
    await page.keyboard.press('Space')
    await expect(tick(page)).toBeChecked()

    const ring = await tick(page).evaluate((el) => {
      const cs = getComputedStyle(el)
      return { style: cs.outlineStyle, width: cs.outlineWidth }
    })
    // A ring of some kind, and not the `outline: none` item 2 forbids without a
    // replacement indicator.
    expect(ring.style).not.toBe('none')
    expect(parseFloat(ring.width)).toBeGreaterThan(0)

    await page.keyboard.press('Space')
    await expect(tick(page)).not.toBeChecked()
  })
})

test.describe('at 375px on a coarse pointer', () => {
  test.use({ viewport: { width: 375, height: 900 }, hasTouch: true, isMobile: true })

  test('the tick is a 44px target and nothing scrolls sideways', async ({ page }) => {
    await isolate(page, async (path, route) => {
      if (!path.endsWith('/distribution/plan')) return undefined
      await route.fulfill({ json: PLAN })
      return 'handled'
    })
    await seed(page)
    await openPlan(page)

    // The tap target is the LABEL: a native checkbox is ~13px and inflating it
    // would eat a tenth of this viewport, so `pointer-coarse:min-h-11` sizes the
    // label and clicking anywhere on it toggles the box.
    const label = page.locator('label').filter({ hasText: CANARY })
    const box = await label.boundingBox()
    expect(Math.round(box.height)).toBeGreaterThanOrEqual(44)
    await label.click()
    await expect(tick(page)).toBeChecked()

    // Asked with the checklist OPEN, which is the widest this box ever gets.
    const scrollX = await page.evaluate(
      () => document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth
    )
    expect(scrollX, 'no horizontal page scroll').toBe(0)
  })
})
