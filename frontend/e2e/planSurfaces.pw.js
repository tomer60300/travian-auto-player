/**
 * Every result surface the backend computed and nothing rendered.
 *
 * Seven fields, one spec: `npc_reserves`, `npc_triggers`, `night_overruns` (on
 * the plan AND on the day check), `morning_shortfalls`, `pre_night_over_baseline`
 * with their two thresholds, `unallocated`'s NPC pair, and the seven new
 * finding kinds. The plan and day-check responses are FIXTURES with known
 * figures, so each assertion is about the arithmetic on screen rather than about
 * a live planner's output.
 *
 * Three claims are worth stating, because each is a way to render these wrongly
 * and look right:
 *
 *   1. **`fill` is a fraction, not a percentage.** 0.42 must read as 42%. A
 *      renderer that printed it raw would show "0%" for every row and pass any
 *      test that only checked the row existed.
 *   2. **The NPC allowance is a ceiling and the draw is what was spent.** A
 *      village with a 22,000/h ceiling that drew nothing must not read as
 *      having converted 22,000/h. That is the one mistake `plannerAllocation.js`
 *      already avoids, and these panels must not reintroduce it.
 *   3. **`projected` is not the present tense.** "True now" is something to act
 *      on before the plan runs; "true after a day of this plan" is a
 *      consequence of running it.
 *
 * NO BACKEND AND NO GAME REQUEST: every `/api` call is answered here or
 * ABORTED, and the snapshot is seeded into localStorage rather than fetched.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test planSurfaces
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 50002
const DEF_A = 50011

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

/** A plan with every new surface populated, and known figures throughout. */
const RICH_PLAN = {
  rows: [],
  budgets: [],
  shortfalls: [],
  unallocated: [
    {
      resource: 'lumber',
      total_production: 12_000,
      total_npc_allowance: 22_000,
      total_npc_draw: 15_000,
      unallocated: 3000,
      remainder_village_id: DEF_A,
    },
    {
      resource: 'clay',
      total_production: 12_000,
      // A large ceiling with nothing drawn against it: the normal case, and
      // the one a renderer is most likely to misreport as production.
      total_npc_allowance: 9000,
      total_npc_draw: 0,
      unallocated: -4200,
      remainder_village_id: null,
    },
  ],
  total_merchants: 14,
  feasible: true,
  verdict: {
    executable: true,
    clean: false,
    blockers: [],
    covers: ['every merchant budget'],
    unweighed: ['npc_capacity_short'],
    critical_findings: 1,
  },
  relays: [],
  role_deviations: [],
  village_nets: [
    {
      village_id: CAPITAL,
      resource: 'lumber',
      own_per_hour: 6000,
      npc_allowance_per_hour: 22_000,
      npc_draw_per_hour: 15_000,
      target_per_hour: 21_000,
      ship_per_hour: 0,
      consumption_per_hour: 0,
      net_per_hour: 21_000,
    },
    {
      village_id: DEF_A,
      resource: 'lumber',
      own_per_hour: 6000,
      npc_allowance_per_hour: 9000,
      npc_draw_per_hour: 0,
      target_per_hour: 6000,
      ship_per_hour: 0,
      consumption_per_hour: 0,
      net_per_hour: 6000,
    },
  ],
  night_overruns: [
    {
      origin: CAPITAL,
      origin_name: '02',
      destination: DEF_A,
      destination_name: '11',
      cycle_hours: 4,
      last_dispatch_minute: 360,
      last_dispatch_clock: '06:00',
      round_trip_minutes: 108,
      overrun_minutes: 48,
    },
  ],
  npc_reserves: [
    {
      village_id: CAPITAL,
      village_name: '02',
      floor_level: 120_000,
      allowance_per_day: 528_000,
      allowance_per_hour: 22_000,
      feedstock: ['clay', 'crop'],
      feedstock_shares: [0.6, 0.4],
      drawn: ['lumber'],
    },
    {
      village_id: DEF_A,
      village_name: '11',
      floor_level: 40_000,
      allowance_per_day: 216_000,
      allowance_per_hour: 9000,
      feedstock: ['crop'],
      feedstock_shares: [1],
      drawn: [],
    },
  ],
  npc_triggers: [
    {
      village_id: CAPITAL,
      village_name: '02',
      kind: 'wood_low',
      resource: 'lumber',
      level: 95_000,
      threshold: 120_000,
      projected: false,
    },
    {
      village_id: CAPITAL,
      village_name: '02',
      kind: 'crop_banked',
      resource: 'crop',
      level: 742_000,
      threshold: 700_000,
      projected: true,
    },
  ],
  warnings: ['02 cannot fund 4,000/h of lumber by conversion', '01 crop reading has drifted'],
  diagnostics: {
    headline: 'Two things need a decision.',
    total_loss_per_day: 96_000,
    loss_by_resource: [{ resource: 'lumber', per_day: 96_000 }],
    counts: { critical: 1, warning: 1, note: 0 },
    groups: [
      {
        key: 'npc_capacity_short',
        severity: 'critical',
        headline: '02 is short 4,000/h of conversion capacity',
        action: 'Lower its target, or raise the stock floor it converts out of.',
        count: 1,
        loss_per_day: 96_000,
        findings: [
          {
            category: 'npc_capacity_short',
            severity: 'critical',
            message: '02 is short 4,000/h',
            detail: '02 needs 19,000/h and can convert 15,000/h',
            village: '02',
          },
        ],
      },
      {
        key: 'crop_profile_drift',
        severity: 'warning',
        headline: '01 nets 31% away from its assumed crop figure',
        action: 'Re-read the village and update the role template.',
        count: 1,
        loss_per_day: 0,
        findings: [
          {
            category: 'crop_profile_drift',
            severity: 'warning',
            message: '01 has drifted 31%',
            detail: 'assumed -5,880/h, actual -7,700/h',
            village: '01',
          },
        ],
      },
    ],
  },
  plan_digest: 'c'.repeat(64),
}

/** A day check with both switch rules reporting, and known fills. */
const RICH_DAY_CHECK = {
  villages: [
    {
      village_id: CAPITAL,
      village_name: '02',
      resource: 'crop',
      daily_net: 12_000,
      low: 80_000,
      high: 190_000,
      settled: true,
    },
  ],
  warnings: ['11 is below the morning floor on clay'],
  morning_floor: 0.6,
  pre_night_baseline: 0.25,
  morning_shortfalls: [
    {
      village_id: DEF_A,
      village_name: '11',
      resource: 'clay',
      store: 'warehouse',
      stock: 168_000,
      capacity: 400_000,
      // A FRACTION. 0.42 must read as 42%, never as 0%.
      fill: 0.42,
    },
  ],
  pre_night_over_baseline: [
    {
      village_id: CAPITAL,
      village_name: '02',
      resource: 'iron',
      store: 'warehouse',
      stock: 260_000,
      capacity: 400_000,
      fill: 0.65,
    },
  ],
  night_overruns: [
    {
      origin: CAPITAL,
      origin_name: '02',
      destination: DEF_A,
      destination_name: '11',
      cycle_hours: 4,
      last_dispatch_minute: 360,
      last_dispatch_clock: '06:00',
      round_trip_minutes: 108,
      overrun_minutes: 48,
    },
  ],
}

async function isolate(page) {
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
    if (path.endsWith('/distribution/plan')) return route.fulfill({ json: RICH_PLAN })
    if (path.endsWith('/distribution/day-check')) return route.fulfill({ json: RICH_DAY_CHECK })
    return route.abort('blockedbyclient')
  })
}

async function seed(page) {
  await page.addInitScript(
    ([key, snap]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      localStorage.setItem(`planner_profiles::${key}`, JSON.stringify({ Day: {}, Night: {} }))
      localStorage.setItem(
        `planner_profile_windows::${key}`,
        JSON.stringify({ Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] })
      )
      // Answered up front: the attendance gate is `npcAttendance.pw.js`'s
      // subject, and here it would only stand between the spec and the panels.
      localStorage.setItem(
        `planner_npc_attended::${key}`,
        JSON.stringify({ Day: true, Night: false })
      )
    },
    [KEY, SNAPSHOT]
  )
}

async function buildPlan(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

test.describe('the plan stage renders what the planner computed', () => {
  test.use({ viewport: { width: 1440, height: 1600 } })

  test('the NPC ceiling and the NPC draw are never confused', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await buildPlan(page)

    const reserves = page.getByRole('row').filter({ hasText: '120,000' })
    // The buffer, the ceiling per hour and per day, and what was actually spent.
    await expect(reserves).toContainText('22,000/h')
    await expect(reserves).toContainText('528,000/day')
    await expect(reserves).toContainText('15,000/h')
    // Which stores paid for it, with their shares.
    await expect(reserves).toContainText('Clay 60%')
    await expect(reserves).toContainText('Crop 40%')
    await expect(reserves).toContainText('Lumber')

    // The village with a 9,000/h ceiling drew NOTHING, and must say so in
    // words rather than showing its ceiling in the spent column.
    const idle = page.getByRole('row').filter({ hasText: '40,000' })
    await expect(idle).toContainText('nothing drawn')
    await expect(idle).toContainText('the floor funded no cargo')
  })

  test('a trigger says whether it is true now or after a day of this plan', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await buildPlan(page)

    const woodLow = page.getByRole('listitem').filter({ hasText: 'at or below your floor' })
    await expect(woodLow).toContainText('95,000 against 120,000')
    await expect(woodLow).toContainText('true now')

    const banked = page.getByRole('listitem').filter({ hasText: 'banked past the trigger' })
    await expect(banked).toContainText('742,000 against 700,000')
    await expect(banked).toContainText('after a day of this plan')

    // Advice, never an action.
    await expect(page.getByText(/nothing here presses the NPC button/i)).toBeVisible()
  })

  test('the account totals keep production and conversion apart', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await buildPlan(page)

    const lumber = page.getByRole('row').filter({ hasText: 'Lumber' }).first()
    await expect(lumber).toContainText('12,000')
    await expect(lumber).toContainText('22,000')
    await expect(lumber).toContainText('15,000')
    await expect(lumber).toContainText('+3,000')
    await expect(lumber).toContainText('11')

    // A ceiling with nothing spent reads "none", and an over-allocation is
    // named in words as well as coloured.
    const clay = page.getByRole('row').filter({ hasText: 'Clay' }).first()
    await expect(clay).toContainText('none')
    await expect(clay).toContainText('-4,200')
    await expect(clay).toContainText('over-allocated')
    await expect(clay).toContainText('no remainder village')
  })

  test('a night overrun shows the arithmetic, not just the verdict', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await buildPlan(page)

    const overrun = page.getByRole('row').filter({ hasText: '02 → 11' })
    await expect(overrun).toContainText('06:00')
    await expect(overrun).toContainText('1.8h')
    await expect(overrun).toContainText('48m late')
  })

  test('the new finding kinds render like any other', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await buildPlan(page)

    // Critical: open by default, with its action beside it.
    await expect(
      page.getByText('02 is short 4,000/h of conversion capacity')
    ).toBeVisible()
    await expect(page.getByText(/Lower its target, or raise the stock floor/)).toBeVisible()

    // Warning: the drift flag, which costs no resources and so carries no chip.
    await expect(
      page.getByText('01 nets 31% away from its assumed crop figure')
    ).toBeVisible()
  })
})

test.describe('the day & night stage renders both switch rules', () => {
  test.use({ viewport: { width: 1440, height: 1600 } })

  test('a fill is a fraction on the wire and a percentage on screen', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()

    // 0.42 must read as 42%, and against the 60% it missed -- a bare
    // percentage means nothing without its threshold.
    // Scoped by the store, not by the village name: "11" also appears in the
    // warnings list above, and matching that would assert on the wrong element
    // while still finding one.
    const morning = page.getByRole('listitem').filter({ hasText: 'Clay · warehouse' })
    await expect(morning).toContainText('42%')
    await expect(morning).toContainText('168,000 of 400,000')
    await expect(morning).toContainText('72,000 short of the floor')
    await expect(page.getByText(/threshold 60%/)).toBeVisible()

    // And the other end of the night, fullest first, over its baseline.
    const preNight = page.getByRole('listitem').filter({ hasText: 'Iron · warehouse' })
    await expect(preNight).toContainText('65%')
    await expect(preNight).toContainText('160,000 over the baseline')
    await expect(page.getByText(/threshold 25%/)).toBeVisible()
  })

  test('the overrun table is the same one the plan stage renders', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()

    const overrun = page.getByRole('row').filter({ hasText: '02 → 11' })
    await expect(overrun).toContainText('48m late')
    await expect(overrun).toContainText('06:00')
  })
})
