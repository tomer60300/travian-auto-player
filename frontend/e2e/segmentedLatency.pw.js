/**
 * `max_latency_hours`, which the page had no business sending at all.
 *
 * The field was derived on the frontend from the ACTIVE profile's hours, on the
 * reasoning that "a route inside a profile has that profile's hours to deliver
 * in, not the two-hour default the backend falls back to". Two things were
 * wrong with that.
 *
 * First, it is the backend's own policy restated on the client. `_plan_account`
 * (`src/travian_api/web/routes/distribution.py`) already applies
 * `min(body.max_latency_hours, segment_window / 60)` -- the window can only
 * TIGHTEN the standing target, never loosen it -- so the page was not supplying
 * a missing fact, it was overriding the server's default with a number it had
 * computed itself. That is the duplicated-default shape: a backend change to
 * the target would be silently overridden by whatever this page last derived.
 *
 * Second, on a SEGMENTED request it was the wrong window. `runDayCheck` and
 * `buildExecutePayload` strip the four other active-tab fields --
 * `allocations`, `dispatch_window`, `npc_attended`, `overnight` -- and left this
 * one on, so clicking the Night tab before "run the whole day" planned the
 * 16-hour DAY segment against an 8-hour target: shorter cycles, more routes,
 * more merchants, more rows, on the endpoint that writes.
 *
 * So the field is not sent from anywhere, and each request is planned against
 * the backend's own target clamped by whatever window that request or segment
 * actually carries. The whole-day assertion is about the WHOLE body rather than
 * one field: the same typed state must produce the same segmented request
 * whatever tab happens to be selected, because the segments already carry
 * everything the tab knows.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test segmentedLatency
 */

import { expect, test } from '@playwright/test'

import { PLAN, isolate, seed } from './plannerHarness'

const EMPTY_DAY_CHECK = {
  villages: [],
  warnings: [],
  morning_floor: 0.6,
  pre_night_baseline: 0.25,
  morning_shortfalls: [],
  pre_night_over_baseline: [],
  night_overruns: [],
}

const EMPTY_EXECUTE = {
  dry_run: true,
  live_enabled: false,
  created: 0,
  created_unverified: 0,
  not_created: 0,
  remaining: 0,
  created_game_rows: 0,
  actions: [],
  disables: [],
  re_enables: [],
  problems: [],
  warnings: [],
  updates: [],
  filtered_to: null,
  requests_forecast: null,
  trace_id: 'seg-latency',
}

/** Day is 16 hours, Night is 8. The gap is the whole point: whichever tab is
 *  selected, the day segment must not be planned against the night's eight. */
const WINDOWS = { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] }

async function isolateCapturing(page) {
  const sent = { plan: [], dayCheck: [], execute: [] }
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/day-check')) {
      sent.dayCheck.push(route.request().postDataJSON())
      await route.fulfill({ json: EMPTY_DAY_CHECK })
      return 'handled'
    }
    if (path.endsWith('/distribution/execute')) {
      sent.execute.push(route.request().postDataJSON())
      await route.fulfill({ json: EMPTY_EXECUTE })
      return 'handled'
    }
    if (path.endsWith('/distribution/plan')) {
      sent.plan.push(route.request().postDataJSON())
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    return undefined
  })
  return sent
}

async function seedTwoProfiles(page, active) {
  await seed(page, {
    planner_profiles: { Day: {}, Night: {} },
    planner_profile_windows: WINDOWS,
    planner_active_profile: active,
  })
}

async function runWholeDayCheck(page, active) {
  const sent = await isolateCapturing(page)
  await seedTwoProfiles(page, active)
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Day & night' }).click()
  await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
  await expect.poll(() => sent.dayCheck.length).toBe(1)
  return sent.dayCheck[0]
}

async function runWholeDayPreview(page, active) {
  const sent = await isolateCapturing(page)
  await seedTwoProfiles(page, active)
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
  await page.getByRole('checkbox', { name: /^Whole day/ }).check()
  await page.getByRole('button', { name: /^Preview \(0 requests\)$/ }).click()
  await expect.poll(() => sent.execute.length).toBe(1)
  return sent.execute[0]
}

test.describe('the latency target belongs to the backend, not to this page', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('/day-check is byte-identical whichever profile is selected', async ({ page }) => {
    const fromDay = await runWholeDayCheck(page, 'Day')
    const fromNight = await runWholeDayCheck(page, 'Night')
    expect(JSON.stringify(fromNight)).toBe(JSON.stringify(fromDay))
    expect(fromDay).not.toHaveProperty('max_latency_hours')
    expect(fromDay.segments.map((s) => s.name)).toEqual(['Day', 'Night'])
  })

  test('a whole-day /execute preview is byte-identical too', async ({ page }) => {
    const fromDay = await runWholeDayPreview(page, 'Day')
    const fromNight = await runWholeDayPreview(page, 'Night')
    expect(JSON.stringify(fromNight)).toBe(JSON.stringify(fromDay))
    expect(fromDay).not.toHaveProperty('max_latency_hours')
  })

  // The unsegmented path too. A one-profile request carries its window, and the
  // backend clamps its own target by that window -- so a derived target here
  // would still be this page overriding a server default it does not own.
  test('a single-profile plan does not send one either', async ({ page }) => {
    const sent = await isolateCapturing(page)
    await seedTwoProfiles(page, 'Night')
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect.poll(() => sent.plan.length).toBe(1)
    expect(sent.plan[0]).not.toHaveProperty('max_latency_hours')
    // The window itself is still sent: it is a fact about the profile, and it
    // is what the backend clamps its own target against.
    expect(sent.plan[0].dispatch_window).toEqual([1380, 420])
    expect(sent.plan[0]).not.toHaveProperty('segments')
  })
})
