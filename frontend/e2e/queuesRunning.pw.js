/**
 * `queues_running` — whether a profile's hours actually run the material spend
 * declared in section 2 — DRIVEN, and the request bodies it produces.
 *
 * The defect this pins. The spend is entered once for the account, and every
 * planning path netted it off production for EVERY profile. On an operator who
 * queues only while awake that charged a DAY figure to the night: each army
 * village read as needing its whole training spend delivered between 23:00 and
 * 07:00, `/night-profile` reported 49,370 lumber, 28,018 clay and 43,020
 * iron per hour of demand "no village could cover", and none of it was demand
 * any hour of that profile creates. Declaring the queues stopped takes the same
 * account to no shortfall at all.
 *
 * Why it is NOT derived from `overnight`, which is the obvious shortcut and is
 * wrong: a build or training queue runs unattended, so it keeps consuming while
 * its owner sleeps. Being asleep stops the manual NPC conversion
 * (`npc_attended`) and does not stop a queue. Silence therefore has to mean
 * SPENDING -- the opposite resting state from the two declarations beside it --
 * and only an explicit answer turns it off. `tests/test_night_profile_endpoint.py`
 * holds the server half of that rule.
 *
 * Asserted on the REQUEST BODY, on `overnightProfile.pw.js`'s reasoning: what
 * the feature has to do is put a boolean in a payload, and a ticked box would
 * confirm React re-rendered without confirming what it sends. Both transports
 * are asserted, because a field that reaches `/plan` and not `/day-check` is a
 * field the operator cannot check their day with -- and the day check is
 * exactly where this one is visible, since it is the replay that runs both
 * profiles over one set of stores.
 *
 * NO BACKEND AND NO GAME REQUEST: every `/api` call is answered here or
 * ABORTED, and the snapshot is seeded into localStorage rather than fetched.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test queuesRunning
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 30002
const ARMY = 30011

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
  villages: [village(CAPITAL, '02', 0, 0), village(ARMY, '11', 4, 0)],
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

async function isolate(page) {
  const sent = { plan: [], dayCheck: [] }
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
    // Fail closed, so this suite can never depend on a backend running.
    return route.abort('blockedbyclient')
  })
  return sent
}

/** The operator's own shape: a day they queue in and a night they do not. */
const DAY_AND_NIGHT = {
  profiles: { Day: {}, Night: {} },
  windows: { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] },
  active: 'Night',
}

async function seed(page, { profiles, windows, active }) {
  await page.addInitScript(
    ([key, snap, profileMap, windowMap, activeProfile]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      localStorage.setItem(`planner_profiles::${key}`, JSON.stringify(profileMap))
      localStorage.setItem(`planner_profile_windows::${key}`, JSON.stringify(windowMap))
      localStorage.setItem(`planner_active_profile::${key}`, JSON.stringify(activeProfile))
    },
    [KEY, SNAPSHOT, profiles, windows, active]
  )
}

async function openDayStage(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Day & night' }).click()
  await expect(page.getByRole('heading', { name: 'The day, window by window' })).toBeVisible()
}

const boxFor = (page, name) =>
  page.getByLabel(`Do your build and training queues run during ${name}`)

/** The stored map, which is what every payload is built from. */
async function stored(page) {
  const raw = await page.evaluate(
    (key) => localStorage.getItem(`planner_queues_running::${key}`),
    KEY
  )
  return raw == null ? null : JSON.parse(raw)
}

test.describe('the queues-running declaration', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('every profile arrives spending, because that is what queues do', async ({ page }) => {
    await isolate(page)
    await seed(page, DAY_AND_NIGHT)
    await openDayStage(page)

    await expect(boxFor(page, 'Day')).toBeChecked()
    await expect(boxFor(page, 'Night')).toBeChecked()
    await expect(page.getByText('Spends').first()).toBeVisible()
  })

  test('stopping one profile writes only that profile, and the default is an absence', async ({
    page,
  }) => {
    await isolate(page)
    await seed(page, DAY_AND_NIGHT)
    await openDayStage(page)

    await boxFor(page, 'Night').uncheck()

    // The TICKED box is the absence here -- the opposite of the two
    // declarations beside it. Storing every profile's `true` would fill the map
    // with the default and make an untouched account look answered.
    expect(await stored(page)).toEqual({ Night: false })
    await expect(page.getByText('Only fills')).toBeVisible()

    await boxFor(page, 'Night').check()
    expect(await stored(page)).toEqual({})
  })

  test('the answer survives a reload', async ({ page }) => {
    await isolate(page)
    await seed(page, DAY_AND_NIGHT)
    await openDayStage(page)

    await boxFor(page, 'Night').uncheck()
    await page.reload()
    await page.getByRole('button', { name: 'Day & night' }).click()

    await expect(boxFor(page, 'Night')).not.toBeChecked()
    await expect(boxFor(page, 'Day')).toBeChecked()
  })

  test('/plan carries the active profile answer', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, DAY_AND_NIGHT)
    await openDayStage(page)

    // Always sent, unlike `overnight`: this field has no third state, so absent
    // and true mean the same thing at both ends and saying so costs nothing.
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(sent.plan[0].queues_running).toBe(true)

    await page.getByRole('button', { name: 'Day & night' }).click()
    await boxFor(page, 'Night').uncheck()
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()

    expect(sent.plan).toHaveLength(2)
    expect(sent.plan[1].queues_running).toBe(false)
  })

  test('/day-check carries it per segment, which is the whole point', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, DAY_AND_NIGHT)
    await openDayStage(page)

    await boxFor(page, 'Night').uncheck()

    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect(page.getByText(/No store crosses its cap/i)).toBeVisible()

    expect(sent.dayCheck).toHaveLength(1)
    // One replay over one set of stores, with the same village burning by day
    // and only filling by night. A single account-wide spend can describe only
    // one of those, which is why the answer rides on the segment.
    expect(sent.dayCheck[0].segments.map((s) => [s.name, s.queues_running])).toEqual([
      ['Day', true],
      ['Night', false],
    ])
  })
})
