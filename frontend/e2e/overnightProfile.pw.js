/**
 * Section 6's `overnight` declaration, DRIVEN — and the request bodies it
 * produces on all three endpoints that carry it.
 *
 * The bug this pins: `overnight` was added to `PlanRequest` and to
 * `DaySegmentInput`, threaded through the planner, and `grep -rn "overnight"
 * frontend/src frontend/e2e` returned ONE prose mention. `buildSegments`
 * emitted `name`, `window`, `allocations` and `npc_attended`; `buildPlanPayload`
 * emitted none of it. So the fix was API-only and nothing in the app could
 * reach it — and the operator CAN create a split night from this page
 * (`addProfile`), which is the exact configuration the fix exists for.
 *
 * Undeclared, the backend derives the night from a window that WRAPS past
 * midnight. That is right for a night stated as one 23:00-07:00 pair and wrong
 * twice over:
 *
 *   * a night SPLIT at midnight. 23:00-00:00 is `[1380, 0]` and does wrap;
 *     00:00-07:00 is `[0, 420]` and wraps in neither direction. Undeclared,
 *     a 600-minute round trip inside a 420-minute night is not reported as
 *     NIGHT_OVERRUN, and with only the pre-midnight half recognised the 60%
 *     morning floor is measured at 00:00 instead of 07:00.
 *   * a near-24h day profile. `[420, 419]` wraps and is not the night.
 *
 * Asserted on the REQUEST BODY, on `npcAttendance.pw.js`'s reasoning: what the
 * feature has to do is put a boolean in a payload, and a `<select>` showing the
 * right option would confirm React re-rendered without confirming what it
 * sends. All three transports are asserted, because a field that reaches
 * `/plan` and not `/day-check` is a field the operator cannot check their day
 * with — and the top-level strip is asserted too, because
 * `_overnight_needs_hours_to_be_overnight` REFUSES a declaration with no
 * window: leaving it on a segmented request (whose top-level
 * `dispatch_window` is stripped) would 422 the whole run.
 *
 * NO BACKEND AND NO GAME REQUEST: every `/api` call is either answered here or
 * ABORTED, and the snapshot is seeded into localStorage rather than fetched.
 * The only execute path driven is the PREVIEW, which writes nothing, and
 * it is answered from this file.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test overnightProfile
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 30002
const DEF_A = 30011

function village(id, name, x, y) {
  return {
    village_id: id,
    name,
    x,
    y,
    merchants_total: 20,
    merchants_free: 20,
    lumber_per_hour: 6000,
    clay_per_hour: 6000,
    iron_per_hour: 6000,
    crop_per_hour: 1200,
    crop_draining: false,
    lumber_stock: 100_000,
    clay_stock: 100_000,
    iron_stock: 100_000,
    crop_stock: 100_000,
    warehouse_capacity: 400_000,
    granary_capacity: 400_000,
  }
}

const SNAPSHOT = {
  villages: [village(CAPITAL, '02', 0, 0), village(DEF_A, '11', 4, 0)],
  map_span: 401,
  speed_fields_per_hour: 16,
  requests_used: 0,
  warnings: [],
}

const EMPTY_PLAN = {
  rows: [],
  budgets: [],
  shortfalls: [],
  unallocated: [],
  total_merchants: 0,
  feasible: true,
  verdict: {
    executable: true,
    clean: true,
    blockers: [],
    covers: ['every merchant budget', 'every receiver is routable', 'no allocation over-claims'],
    unweighed: [],
    critical_findings: 0,
  },
  relays: [],
  role_deviations: [],
  village_nets: [],
  night_overruns: [],
  npc_reserves: [],
  npc_triggers: [],
  warnings: [],
  diagnostics: {
    headline: 'Nothing to report.',
    total_loss_per_day: 0,
    loss_by_resource: [],
    groups: [],
    counts: { critical: 0, warning: 0, note: 0 },
  },
  plan_digest: 'a'.repeat(64),
}

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
  created: [],
  results: [],
  disables: [],
  re_enables: [],
  problems: [],
  warnings: [],
  remaining: 0,
  dry_run: true,
}

async function isolate(page) {
  const sent = { plan: [], dayCheck: [], execute: [] }
  await page.routeWebSocket(/.*/, (ws) => ws.close())
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/users/me')) {
      return route.fulfill({ json: { id: 1, username: PLAYER, is_active: true } })
    }
    if (path.endsWith('/travian/status')) {
      return route.fulfill({
        json: {
          connected: true,
          server_url: SERVER,
          player_name: PLAYER,
          tribe_id: 1,
          active_village_id: CAPITAL,
          villages: SNAPSHOT.villages.map((v) => ({ id: v.village_id, name: v.name })),
        },
      })
    }
    if (path.endsWith('/distribution/plan')) {
      sent.plan.push(route.request().postDataJSON())
      return route.fulfill({ json: EMPTY_PLAN })
    }
    if (path.endsWith('/distribution/day-check')) {
      sent.dayCheck.push(route.request().postDataJSON())
      return route.fulfill({ json: EMPTY_DAY_CHECK })
    }
    if (path.endsWith('/distribution/execute')) {
      sent.execute.push(route.request().postDataJSON())
      return route.fulfill({ json: EMPTY_EXECUTE })
    }
    // Fail closed: anything unanticipated would be proxied to the debug
    // backend, and this suite must never depend on one running.
    return route.abort('blockedbyclient')
  })
  return sent
}

/** The account's night SPLIT at midnight, which is the configuration the
 *  declaration exists for. No stock floor anywhere, so the attendance answer is
 *  not owed and is not what any of this measures. */
const SPLIT_NIGHT = {
  profiles: { Day: {}, 'Night early': {}, 'Night late': {} },
  windows: {
    Day: ['07:00', '23:00'],
    'Night early': ['23:00', '00:00'],
    'Night late': ['00:00', '07:00'],
  },
  active: 'Night late',
}

async function seed(page, { profiles, windows, active, overnight = null }) {
  await page.addInitScript(
    ([key, snap, profileMap, windowMap, activeProfile, declared]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      localStorage.setItem(`planner_profiles::${key}`, JSON.stringify(profileMap))
      localStorage.setItem(`planner_profile_windows::${key}`, JSON.stringify(windowMap))
      localStorage.setItem(`planner_active_profile::${key}`, JSON.stringify(activeProfile))
      if (declared != null) {
        localStorage.setItem(`planner_overnight::${key}`, JSON.stringify(declared))
      }
    },
    [KEY, SNAPSHOT, profiles, windows, active, overnight]
  )
}

async function openDayStage(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Day & night' }).click()
  await expect(page.getByRole('heading', { name: 'The day, window by window' })).toBeVisible()
}

/** The stored declaration map, which is what every payload is built from. */
async function storedOvernight(page) {
  const raw = await page.evaluate((key) => localStorage.getItem(`planner_overnight::${key}`), KEY)
  return raw == null ? null : JSON.parse(raw)
}

test.describe('the overnight declaration', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the control writes the declaration, and both polarities survive', async ({ page }) => {
    await isolate(page)
    await seed(page, SPLIT_NIGHT)
    await openDayStage(page)

    // Nothing declared on arrival: the resting state is DERIVE, and the panel
    // says which way it derives rather than leaving it to be inferred.
    await expect(page.getByLabel('Is Night late the overnight profile')).toHaveValue('')
    await expect(page.getByText('from the hours: not the night').first()).toBeVisible()
    await expect(page.getByText('from the hours: this is the night').first()).toBeVisible()

    await page.getByLabel('Is Night late the overnight profile').selectOption('night')
    // False is an answer too: a near-24h day profile wraps and is not the night.
    await page.getByLabel('Is Day the overnight profile').selectOption('day')

    expect(await storedOvernight(page)).toEqual({ 'Night late': true, Day: false })
    await expect(page.getByText('you said this is the night').first()).toBeVisible()
    await expect(page.getByText('you said this is not the night').first()).toBeVisible()
  })

  test('a declaration survives a reload, and derive is reachable again', async ({ page }) => {
    await isolate(page)
    await seed(page, SPLIT_NIGHT)
    await openDayStage(page)

    await page.getByLabel('Is Night late the overnight profile').selectOption('night')
    await page.reload()
    await page.getByRole('button', { name: 'Day & night' }).click()
    await expect(page.getByLabel('Is Night late the overnight profile')).toHaveValue('night')

    // Back to derived is the ABSENCE of a key, not a stored null: absent is
    // what asks the backend to read it off the window.
    await page.getByLabel('Is Night late the overnight profile').selectOption('')
    expect(await storedOvernight(page)).toEqual({})
  })

  test('a profile with no hours cannot declare, because the backend refuses one', async ({
    page,
  }) => {
    await isolate(page)
    await seed(page, { profiles: { 'All day': {} }, windows: {}, active: 'All day' })
    await openDayStage(page)

    // Disabled, and visibly so: `_overnight_needs_hours_to_be_overnight`
    // raises on a declaration with no `dispatch_window`, because section 6's
    // deadline is measured against the window's END.
    await expect(page.getByLabel('Is All day the overnight profile')).toBeDisabled()
    await expect(page.getByText(/no hours, so nothing to read it from/)).toBeVisible()
  })

  test('the split night is named before a plan is built', async ({ page }) => {
    await isolate(page)
    await seed(page, SPLIT_NIGHT)
    await openDayStage(page)

    // Detected off the clock alone: one window ends at 00:00 and another
    // starts there. Nothing is guessed and nothing is applied.
    // The warning names the profile that runs the half after midnight, so the
    // operator is not left to work out which row it means.
    await expect(page.getByText(/night looks split at midnight/i)).toContainText('Night late')

    await page.getByLabel('Is Night late the overnight profile').selectOption('night')
    await expect(page.getByText(/night looks split at midnight/i)).toHaveCount(0)
  })

  test('/plan carries the active profile declaration beside its window', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, SPLIT_NIGHT)
    await openDayStage(page)

    // Undeclared first: absent is what asks the backend to derive, so sending
    // a computed copy would only make the request look like a decision.
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(sent.plan).toHaveLength(1)
    expect(sent.plan[0]).not.toHaveProperty('overnight')
    // 00:00-07:00, the half that wraps in neither direction.
    expect(sent.plan[0].dispatch_window).toEqual([0, 420])

    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByLabel('Is Night late the overnight profile').selectOption('night')
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()

    expect(sent.plan).toHaveLength(2)
    expect(sent.plan[1].overnight).toBe(true)
    expect(sent.plan[1].dispatch_window).toEqual([0, 420])
  })

  test('/day-check carries it per segment, never at the top level', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, SPLIT_NIGHT)
    await openDayStage(page)

    await page.getByLabel('Is Night late the overnight profile').selectOption('night')
    await page.getByLabel('Is Day the overnight profile').selectOption('day')

    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect(page.getByText(/No store crosses its cap/i)).toBeVisible()

    expect(sent.dayCheck).toHaveLength(1)
    const segments = sent.dayCheck[0].segments
    // Both halves of the night, and the pre-midnight one left to derive --
    // it wraps, so the clock gets that one right on its own.
    expect(segments.map((s) => [s.name, s.overnight])).toEqual([
      ['Day', false],
      ['Night early', undefined],
      ['Night late', true],
    ])
    // The top-level field is stripped. Not merely redundant: with the
    // top-level `dispatch_window` stripped too, a top-level `overnight` is
    // exactly what `_overnight_needs_hours_to_be_overnight` raises on, so
    // leaving it on would 422 the whole check.
    expect(sent.dayCheck[0]).not.toHaveProperty('overnight')
    expect(sent.dayCheck[0]).not.toHaveProperty('dispatch_window')
  })

  test('a whole-day /execute preview carries it per segment too', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, SPLIT_NIGHT)
    await openDayStage(page)

    await page.getByLabel('Is Night late the overnight profile').selectOption('night')

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()

    await page.getByLabel(/Whole day — execute all profiles at once/).check()
    await page.getByRole('button', { name: /^Preview \(0 requests\)$/ }).click()
    await expect.poll(() => sent.execute.length).toBe(1)

    // Zero game requests: the preview is a dry run, and this file answered it.
    expect(sent.execute[0].execution_mode).toBe('preview')
    expect(sent.execute[0].segments.map((s) => [s.name, s.overnight])).toEqual([
      ['Day', undefined],
      ['Night early', undefined],
      ['Night late', true],
    ])
    expect(sent.execute[0]).not.toHaveProperty('overnight')
    expect(sent.execute[0]).not.toHaveProperty('dispatch_window')
  })
})
