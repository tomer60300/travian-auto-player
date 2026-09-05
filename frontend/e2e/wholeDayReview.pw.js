/**
 * A review on screen must correspond to the run the button beside it will send.
 *
 * `Whole day — execute all profiles at once` is NOT in the `planInputRev`
 * dependency array, so ticking it leaves the route sheet, the verdict banner
 * and every read-only panel exactly as they were. But the mode changes what
 * Execute sends: `buildExecutePayload` strips the top-level allocations, hours,
 * attendance and overnight declaration, replaces them with one segment per
 * profile, and forces `prune_to_window: true` (`prunesToWindow` is
 * `pruneToWindow || wholeDay`). The sheet on screen describes ONE profile
 * planned in its own hours; the run writes every profile's routes, each trimmed
 * to its own window. Reviewing the first and pressing the second is a false
 * review.
 *
 * INVALIDATING the sheet is not the fix, and the page proves it rather than
 * argues it: the whole-day checkbox, the prune checkbox and the Preview button
 * all live inside `{plan && (...)}` on the Plan stage. Clearing the plan on the
 * toggle would unmount the checkbox on the same click that ticked it — the mode
 * could never be turned on at all, and the Preview that IS the whole-day review
 * would go with it. (`pruneCoherence.pw.js` relies on exactly this: it ticks
 * whole day and then reads the prune box, which only exists while the plan
 * does.) So the sheet stays, and says whose sheet it is.
 *
 * NO BACKEND AND NO GAME REQUEST: every call is answered from a fixture or
 * aborted fail-closed. There is a live Travian account on this machine.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test wholeDayReview
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** Two profiles with hours and an attendance answer each, which is what
 *  whole-day execution requires before it will send anything. */
const TWO_PROFILES = {
  planner_profiles: { Day: {}, Night: {} },
  planner_profile_windows: { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] },
  planner_npc_attended: { Day: true, Night: false },
  planner_trade_office: { [CAPITAL]: 13 },
}

const wholeDayBox = (page) =>
  page.getByRole('checkbox', { name: 'Whole day — execute all profiles at once' })

/** The banner under test, found by the phrase only it carries. */
const oneProfileNotice = (page) => page.getByRole('status').filter({ hasText: /profile alone/ })

async function openPlanStage(page, extra = TWO_PROFILES) {
  await isolate(page, (path) => (path.endsWith('/distribution/execute') ? PREVIEW : undefined))
  await seed(page, extra)
  await openPlan(page)
}

test.describe('the sheet says whose sheet it is once whole day is on', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('an ordinary single-profile review carries no such notice', async ({ page }) => {
    await openPlanStage(page)

    await expect(page.getByText(/^Routes$/)).toBeVisible()
    await expect(oneProfileNotice(page)).toHaveCount(0)
  })

  test('ticking whole day marks the sheet as one profile, by name', async ({ page }) => {
    await openPlanStage(page)
    await wholeDayBox(page).check()

    await expect(oneProfileNotice(page)).toBeVisible()
    // The name, not "the active profile": the operator has two and the sheet is
    // one of them.
    await expect(oneProfileNotice(page)).toContainText('Day')
  })

  test('the notice names the Preview as the whole-day review', async ({ page }) => {
    await openPlanStage(page)
    await wholeDayBox(page).check()

    await expect(oneProfileNotice(page)).toContainText(/Preview \(0 requests\)/)
    // And what makes the sheet not that review: every profile is written, each
    // trimmed to its own hours.
    await expect(oneProfileNotice(page)).toContainText(/every profile/i)
  })

  test('it follows the profile the sheet was built for', async ({ page }) => {
    await openPlanStage(page, { ...TWO_PROFILES, planner_active_profile: 'Night' })
    await wholeDayBox(page).check()

    await expect(oneProfileNotice(page)).toContainText('Night')
  })

  test('unticking whole day takes the notice away with it', async ({ page }) => {
    await openPlanStage(page)
    await wholeDayBox(page).check()
    await expect(oneProfileNotice(page)).toBeVisible()

    await wholeDayBox(page).uncheck()
    await expect(oneProfileNotice(page)).toHaveCount(0)
  })

  // The reason the sheet is marked rather than invalidated, pinned so nobody
  // "fixes" it by adding `wholeDay` to `planInputRev`: the checkbox is inside
  // the plan-gated block, so clearing the plan would unmount the control that
  // was just clicked.
  test('the plan survives the toggle, so the mode can be turned on at all', async ({ page }) => {
    await openPlanStage(page)
    await wholeDayBox(page).check()

    await expect(wholeDayBox(page)).toBeChecked()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    await expect(page.getByRole('button', { name: /^Preview \(0 requests\)/ })).toBeVisible()
  })
})
