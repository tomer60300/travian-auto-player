/**
 * One tick box, three requests, and — until this — two answers.
 *
 * "Trim the fan-out to the profile hours" renders
 * `checked={pruneToWindow || wholeDay} disabled={wholeDay}`, so turning
 * whole-day on DISPLAYS it ticked while the state behind it stays whatever the
 * operator last set. The three builders then disagreed about what the tick
 * meant:
 *
 *   * `buildExecutePayload` forced `prune_to_window: true` in whole-day mode --
 *     it has to, because `ExecuteRequest._segments_are_coherent` refuses
 *     segments without it: both profiles' fan-outs would cover the whole day
 *     and no row could be attributed to either;
 *   * `buildPlanPayload` sent the untouched state, so `/plan` weighed all 24/N
 *     firings of every route as escaping the window;
 *   * `/day-check` inherits the plan payload, so it agreed with `/plan` --
 *     and that agreement was itself the defect, because `/day-check` is ALWAYS
 *     segmented and the only segmented `/execute` is the whole-day run, which
 *     forces the prune on. It sends `prune_to_window: true` unconditionally
 *     now, for the same reason `buildExecutePayload` does.
 *
 * Which means the plan the operator reviewed and the run that WROTE described
 * different route sets, and the sheet in between was graded against the wrong
 * one. The prune is not cosmetic: it is what deletes the rows departing outside
 * the profile, and without it the destination receives every firing -- about
 * three times the modelled cargo for an eight-hour profile.
 *
 * NO BACKEND AND NO GAME REQUEST: every call is answered from a fixture or
 * aborted, and what is asserted is the BODY that was sent.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test pruneCoherence
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, PLAN, PREVIEW, isolate, seed } from './plannerHarness'

/** Every planner request, kept by endpoint so one page state can be read off
 *  all three at once. */
async function recordBodies(page) {
  const sent = { plan: [], dayCheck: [], execute: [] }
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      sent.plan.push(route.request().postDataJSON())
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    if (path.endsWith('/distribution/day-check')) {
      sent.dayCheck.push(route.request().postDataJSON())
      // The panel only needs a shape it can render; the assertion is on the
      // request, not the answer.
      await route.fulfill({
        json: {
          villages: [],
          morning_floor: 0.6,
          pre_night_baseline: 0.25,
          night_overruns: [],
          warnings: [],
        },
      })
      return 'handled'
    }
    if (path.endsWith('/distribution/execute')) {
      sent.execute.push(route.request().postDataJSON())
      await route.fulfill({ json: PREVIEW })
      return 'handled'
    }
    return undefined
  })
  return sent
}

/** Both profiles carry hours, which is what whole-day execution requires --
 *  and the attendance answer each segment needs, so no guard fires before the
 *  request does. */
const TWO_PROFILES = {
  planner_profiles: { Day: {}, Night: {} },
  planner_profile_windows: { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] },
  planner_npc_attended: { Day: true, Night: false },
  planner_trade_office: { [CAPITAL]: 13 },
}

const prune = (page) =>
  page.getByRole('checkbox', { name: 'Trim the fan-out to the profile hours' })

const wholeDayBox = (page) =>
  page.getByRole('checkbox', { name: 'Whole day — execute all profiles at once' })

async function openPlanStage(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

/** Toggling the prune CLEARS THE PLAN -- it is one of the inputs a route sheet
 *  is computed from, so the run panel holding it unmounts on the same click.
 *  Pre-existing and correct; it just means the box has to be found again. */
async function turnPruneOff(page) {
  await expect(prune(page)).toBeChecked()
  await prune(page).click()
  await expect(prune(page)).toHaveCount(0)
}

/** The prune as a request states it. Absent is the backend's `false`, which is
 *  what the field has always meant: it does nothing without a window. */
const pruneOf = (body) => body?.prune_to_window ?? false

test.describe('the tick box, the plan and the run agree', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  // The reported state: prune OFF, whole day ON. The box displays ticked.
  test('whole day forces the prune on every request, not only the one that writes', async ({
    page,
  }) => {
    const sent = await recordBodies(page)
    await seed(page, TWO_PROFILES)
    await openPlanStage(page)

    await turnPruneOff(page)
    // The plan is gone; rebuild it so the run panel comes back.
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    await wholeDayBox(page).check()

    // What the operator sees: ticked, and not theirs to untick in this mode.
    await expect(prune(page)).toBeChecked()
    await expect(prune(page)).toBeDisabled()

    sent.plan.length = 0
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect.poll(() => sent.plan.length).toBe(1)

    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect.poll(() => sent.dayCheck.length).toBe(1)

    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await page.getByRole('button', { name: /^Preview/ }).click()
    await expect.poll(() => sent.execute.length).toBe(1)

    // One page state, one answer. Before this the execute said true while the
    // plan the operator had just reviewed said false.
    expect(pruneOf(sent.plan.at(-1))).toBe(true)
    expect(pruneOf(sent.dayCheck.at(-1))).toBe(true)
    expect(pruneOf(sent.execute.at(-1))).toBe(true)
  })

  // `/day-check` is the exception, and it is not an exception to the rule --
  // it is the rule applied to the one endpoint that always carries segments.
  // `segments` is `min_length=1` there, and the only segmented `/execute` is the
  // whole-day run, which `_segments_are_coherent` refuses without the prune.
  // Routing the field through the Plan stage's checkbox meant the full day was
  // CHECKED on all 24 firings of every route and on the full cycle set, while
  // the whole-day run that writes narrowed `allowed_cycles` to the divisors of
  // the window and deleted the out-of-window rows. Different cycles, different
  // merchant counts, different row counts: the plan the operator reviewed was
  // not the plan the run wrote.
  test('the full-day check prunes whatever the box says, because it always segments', async ({
    page,
  }) => {
    const sent = await recordBodies(page)
    await seed(page, TWO_PROFILES)
    await openPlanStage(page)

    await turnPruneOff(page)
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    await expect(prune(page)).not.toBeChecked()

    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect.poll(() => sent.dayCheck.length).toBe(1)

    expect(sent.dayCheck.at(-1).segments.length).toBeGreaterThan(0)
    expect(pruneOf(sent.dayCheck.at(-1))).toBe(true)
  })

  test('with whole day off, the operator’s own answer is what all three carry', async ({
    page,
  }) => {
    const sent = await recordBodies(page)
    await seed(page, TWO_PROFILES)
    await openPlanStage(page)

    await turnPruneOff(page)
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    await expect(prune(page)).not.toBeChecked()

    sent.plan.length = 0
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect.poll(() => sent.plan.length).toBe(1)

    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect.poll(() => sent.dayCheck.length).toBe(1)

    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await page.getByRole('button', { name: /^Preview/ }).click()
    await expect.poll(() => sent.execute.length).toBe(1)

    expect(pruneOf(sent.plan.at(-1))).toBe(false)
    // Except the full-day check, which is segmented by construction and
    // therefore prunes by construction -- see the case above.
    expect(pruneOf(sent.dayCheck.at(-1))).toBe(true)
    expect(pruneOf(sent.execute.at(-1))).toBe(false)
  })

  test('the resting state is on, and all three say so', async ({ page }) => {
    const sent = await recordBodies(page)
    await seed(page, TWO_PROFILES)
    await openPlanStage(page)
    await expect(prune(page)).toBeChecked()

    sent.plan.length = 0
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect.poll(() => sent.plan.length).toBe(1)

    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect.poll(() => sent.dayCheck.length).toBe(1)

    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await page.getByRole('button', { name: /^Preview/ }).click()
    await expect.poll(() => sent.execute.length).toBe(1)

    expect(pruneOf(sent.plan.at(-1))).toBe(true)
    expect(pruneOf(sent.dayCheck.at(-1))).toBe(true)
    expect(pruneOf(sent.execute.at(-1))).toBe(true)
  })

  // The mode must not overwrite the answer. `pruneToWindow` is persisted (v10)
  // and is the operator's own sentence about their route set; whole-day is a
  // way of running it, so unticking whole-day has to give the switch back the
  // way they left it.
  test('whole day does not overwrite the stored answer', async ({ page }) => {
    await recordBodies(page)
    await seed(page, TWO_PROFILES)
    await openPlanStage(page)

    await turnPruneOff(page)
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()

    await wholeDayBox(page).check()
    await expect(prune(page)).toBeChecked()
    await wholeDayBox(page).uncheck()
    await expect(prune(page)).not.toBeChecked()

    const stored = await page.evaluate(() =>
      Object.entries(localStorage)
        .filter(([key]) => key.startsWith('planner_prune_to_window::'))
        .map(([, value]) => value)
    )
    expect(stored).toEqual(['false'])
  })
})
