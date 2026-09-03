/**
 * Section 7's attendance answer, DRIVEN — and the request bodies it produces.
 *
 * The bug this pins: `npc_attended` is required by the backend as soon as any
 * village keeps a `stock_floor_fraction` above 0 and the request carries a
 * `dispatch_window`, and the page could not send it at ALL. Setting a stock
 * floor and using day/night profiles therefore failed with a 422 naming village
 * ids, with no control anywhere to fix it.
 *
 * Asserted on the REQUEST BODY rather than on the rendered controls, and
 * deliberately: what the feature has to do is put a boolean in a payload, and a
 * `<select>` showing the right option would confirm React re-rendered without
 * confirming what it sends. The day/night asymmetry is the same assertion twice
 * with opposite answers — Day carries true, Night carries false, in one
 * segmented request — because a single profile's answer would be satisfied by a
 * hardcoded default and this field exists precisely because there is none.
 *
 * NO BACKEND AND NO GAME REQUEST, on the same two mechanisms
 * `roleTemplates.pw.js` uses and for the same reason: every `/api` call is
 * either answered here or ABORTED, and the snapshot is seeded into
 * localStorage rather than fetched. There is no code path in this spec that
 * could ask the game for anything.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test npcAttendance
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

/** A plan response with nothing in it, so the Plan stage renders without a
 *  backend. `warnings: []` keeps the diagnostics panel out of the picture. */
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

/**
 * Everything the shell asks for, a recorder for the planner calls, and a hard
 * stop for anything else.
 */
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
    // Fail closed: anything unanticipated would be proxied to the debug
    // backend, and this suite must never depend on one running.
    return route.abort('blockedbyclient')
  })
  return sent
}

/**
 * A connected account with a fresh snapshot, two profiles with hours, and a
 * stock floor on the capital — which is what makes the attendance answer
 * REQUIRED rather than optional.
 */
async function seed(page, { floor = 0.3, attendance = null } = {}) {
  await page.addInitScript(
    ([key, snap, capital, stockFloor, stored]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      localStorage.setItem(
        `planner_profiles::${key}`,
        JSON.stringify({ Day: {}, Night: {} })
      )
      // The hours the account actually runs. Seeded rather than left to the
      // page's own defaults so the spec states the windows it asserts about.
      localStorage.setItem(
        `planner_profile_windows::${key}`,
        JSON.stringify({ Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] })
      )
      if (stockFloor != null) {
        localStorage.setItem(
          `planner_stock_floor::${key}`,
          JSON.stringify({ [capital]: stockFloor })
        )
      }
      if (stored != null) {
        localStorage.setItem(`planner_npc_attended::${key}`, JSON.stringify(stored))
      }
    },
    [KEY, SNAPSHOT, CAPITAL, floor, attendance]
  )
}

async function openDayStage(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Day & night' }).click()
  await expect(page.getByRole('heading', { name: 'The day, window by window' })).toBeVisible()
}

/** The stored attendance map, which is what the payloads are built out of. */
async function storedAttendance(page) {
  const raw = await page.evaluate(
    (key) => localStorage.getItem(`planner_npc_attended::${key}`),
    KEY
  )
  return raw == null ? null : JSON.parse(raw)
}

test.describe('NPC attendance', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('the plan is refused, by name, while a profile has not said', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: /^Build plan/ }).click()

    // The refusal names the PROFILE and the stage that answers it. A 422 from
    // the backend names village ids instead, which is the defect.
    await expect(page.getByText(/needs to know whether you are trading/i)).toBeVisible()
    // And nothing was sent: the guard is before the request, not after it.
    expect(sent.plan).toEqual([])
    // It also takes the operator to the control, rather than describing it.
    await expect(page.getByRole('heading', { name: 'The day, window by window' })).toBeVisible()
  })

  test('nothing is asked of an account that declares no floor', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { floor: null })
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()

    expect(sent.plan).toHaveLength(1)
    // Omitted, not guessed: with no floor the field is not required, and
    // sending one would be a claim nobody made.
    expect(sent.plan[0]).not.toHaveProperty('npc_attended')
  })

  test('the two answers are opposite, and each rides its own segment', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page)
    await openDayStage(page)

    // The asymmetry, typed. Both rows are on screen at once, which is the
    // whole reason this is a table of every profile rather than one checkbox.
    await page
      .getByLabel('Who is trading during Day')
      .selectOption('awake')
    await page
      .getByLabel('Who is trading during Night')
      .selectOption('asleep')
    expect(await storedAttendance(page)).toEqual({ Day: true, Night: false })

    // The single-window path: `/plan` carries the ACTIVE profile's answer.
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(sent.plan).toHaveLength(1)
    expect(sent.plan[0].npc_attended).toBe(true)
    expect(sent.plan[0].dispatch_window).toEqual([420, 1380])

    // The segmented path: every segment answers for its own hours.
    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
    await expect(page.getByText(/No store crosses its cap/i)).toBeVisible()
    expect(sent.dayCheck).toHaveLength(1)
    const segments = sent.dayCheck[0].segments
    expect(segments.map((s) => [s.name, s.npc_attended])).toEqual([
      ['Day', true],
      ['Night', false],
    ])
    // The top-level pair is stripped on a segmented request, because a profile
    // carries its own of each and the backend refuses both together.
    expect(sent.dayCheck[0]).not.toHaveProperty('npc_attended')
    expect(sent.dayCheck[0]).not.toHaveProperty('dispatch_window')
  })

  test('the full-day check is refused while one window is silent', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { attendance: { Day: true } })
    await openDayStage(page)

    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()

    await expect(page.getByText(/Night has not said/i)).toBeVisible()
    expect(sent.dayCheck).toEqual([])
  })

  test('false survives a reload, because false is an answer', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openDayStage(page)

    await page.getByLabel('Who is trading during Night').selectOption('asleep')
    await page.reload()
    await page.getByRole('button', { name: 'Day & night' }).click()

    await expect(page.getByLabel('Who is trading during Night')).toHaveValue('asleep')
    // In words under the control, not only as a selected option: the state is
    // never carried by a widget position alone.
    await expect(page.getByText('nobody is trading', { exact: true })).toBeVisible()
  })

  test('the clock suggests and never decides', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openDayStage(page)

    // Unanswered on arrival: the window says 23:00-07:00 and the page still
    // does not fill it in, because "the operator is asleep" is a fact about
    // the operator.
    await expect(page.getByLabel('Who is trading during Night')).toHaveValue('')
    // Unanswered is the ABSENCE of a key. The map itself is written on
    // hydration, so "nothing stored" is an empty map rather than no map.
    expect(await storedAttendance(page)).toEqual({})

    // The suggestion names the answer it will write, so it is never a mystery
    // button — and the two profiles are offered opposite ones.
    await page.getByRole('button', { name: /These hours look like you are asleep/ }).click()
    await page.getByRole('button', { name: /These hours look like you are awake/ }).click()
    expect(await storedAttendance(page)).toEqual({ Night: false, Day: true })
  })

  test('the bar states the active profile answer and leads to the editor', async ({ page }) => {
    await isolate(page)
    await seed(page, { attendance: { Day: true, Night: false } })
    await page.goto('/resource-planner')

    // In words, on the bar that scopes every stage. Day is the active profile.
    const badge = page.getByRole('button', { name: /^NPC:/ })
    await expect(badge).toHaveText(/at the marketplace/i)
    await badge.click()
    await expect(page.getByRole('heading', { name: 'The day, window by window' })).toBeVisible()
  })
})
