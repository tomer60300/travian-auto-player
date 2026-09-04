/**
 * Freely-changing numbers, DRIVEN — the four classes the operator's figures
 * actually move through.
 *
 * "Dont code a hardcoded version, number are constantly change, the UI should
 * know to handle the input well so it work." The role-template crop figures
 * have moved three times, so nothing here asserts a particular figure is
 * right. What it asserts is that the UI cannot change one:
 *
 *   1. A NEGATIVE where negative is legitimate. Already covered, and by two
 *      suites: `ownedNpcFields.pw.js` pins -5,880, 0 and blank on
 *      `assumed_crop_per_hour` through the request, and
 *      `plannerSetup.test.js` pins the same three through a file round trip.
 *      Not repeated here.
 *   2. LARGE. A figure in the millions must arrive as itself, and a fraction a
 *      file supplied must not be rounded by the box that displays it and then
 *      re-committed at the rounded value.
 *   3. EMPTY vs ZERO. Blank means "unknown, do not guess for me" and 0 is an
 *      answer, and this codebase distinguishes them everywhere -- except at
 *      five payload gates, all fixed in the commit that adds this file.
 *   4. RETYPING. A plan built from the old figure must not survive the new
 *      one, because the route sheet is copied into the game's own trade-route
 *      dialog.
 *
 * Asserted on REQUEST BODIES and on stored state, not on rendered cells, for
 * the reason `npcAttendance.pw.js` gives: a box showing the right number
 * confirms React re-rendered without confirming what it sends.
 *
 * NO BACKEND AND NO GAME REQUEST, on the same two mechanisms every spec here
 * uses: every `/api` call is either answered by the route handler or ABORTED
 * fail-closed, and the snapshot is seeded into localStorage rather than
 * fetched. The only execute path driven is the PREVIEW, which is `dry_run:
 * true` and mocked besides -- nothing in this file can reach :8001, let alone
 * the game.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test plannerNumbers
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 40002
const RELAY = 40011
const FAR = 40013

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
  villages: [village(CAPITAL, '02', 0, 0), village(RELAY, '11', 4, 0), village(FAR, '13', 0, 4)],
  map_span: 401,
  speed_fields_per_hour: 16,
  requests_used: 0,
  warnings: [],
}

const DIGEST = 'b'.repeat(64)

/** A plan the Plan stage renders. `rows: []` on purpose: what these tests
 *  measure is which figures the REQUEST carried and whether the stage survives
 *  an edit, and a route table full of fixture rows measures neither. */
const PLAN = {
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
    covers: ['every merchant budget'],
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
  plan_digest: DIGEST,
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

const EMPTY_NIGHT_PROFILE = { allocations: {}, unmet: {}, notes: [] }

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

/** Every call the shell and the planner make, recorded; everything else stopped. */
async function isolate(page) {
  const sent = { plan: [], dayCheck: [], nightProfile: [], execute: [] }
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
      return route.fulfill({ json: PLAN })
    }
    if (path.endsWith('/distribution/day-check')) {
      sent.dayCheck.push(route.request().postDataJSON())
      return route.fulfill({ json: EMPTY_DAY_CHECK })
    }
    if (path.endsWith('/distribution/night-profile')) {
      sent.nightProfile.push(route.request().postDataJSON())
      return route.fulfill({ json: EMPTY_NIGHT_PROFILE })
    }
    if (path.endsWith('/distribution/execute')) {
      sent.execute.push(route.request().postDataJSON())
      return route.fulfill({ json: EMPTY_EXECUTE })
    }
    // Fail closed: an unanticipated call would be proxied to the debug backend.
    return route.abort('blockedbyclient')
  })
  return sent
}

async function seed(page, extra = {}) {
  await page.addInitScript(
    ([key, snap, more]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      localStorage.setItem(`planner_profiles::${key}`, JSON.stringify({ Day: {}, Night: {} }))
      // Hours, because the full-day check needs a segment per window. No stock
      // floor is seeded anywhere here, so the attendance answer is not owed
      // and is not what any of these tests are measuring.
      localStorage.setItem(
        `planner_profile_windows::${key}`,
        JSON.stringify({ Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] })
      )
      for (const [name, value] of Object.entries(more)) {
        localStorage.setItem(`${name}::${key}`, JSON.stringify(value))
      }
    },
    [KEY, SNAPSHOT, extra]
  )
}

async function stored(page, name) {
  const raw = await page.evaluate((k) => localStorage.getItem(k), `${name}::${KEY}`)
  return raw == null ? null : JSON.parse(raw)
}

/** Every disclosure open, so a control behind a summary is reachable.
 *  `inputWidths.pw.js` does the same thing for the same reason. */
async function openDisclosures(page) {
  await page.evaluate(() => {
    for (const d of document.querySelectorAll('details')) d.open = true
  })
  await expect(page.locator('details:not([open])')).toHaveCount(0)
}

/** The landing stage, which carries the village table, the foreign-targets
 *  table and the World & merchants bar -- all three surfaces measured here. */
async function openAccount(page) {
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Crop stock alert level for 02')).toBeVisible()
}

async function buildPlan(page, sent) {
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await expect.poll(() => sent.plan.length).toBe(1)
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

test.describe('a blank box is unknown; a typed zero is an answer', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('a crop alert of 0 rides the day check, because 0 is a level', async ({ page }) => {
    // The box offers 0 (`min="0"`), the state keeps 0 apart from blank, the
    // file writes 0 and `parseSetup` accepts 0 -- and then the day-check gate
    // collapsed 0 and blank into "no ceiling", so the one village whose alert
    // is "tell me when it is empty" was the one village never checked.
    const sent = await isolate(page)
    await seed(page)
    await openAccount(page)

    await page.getByLabel('Crop stock alert level for 02').fill('0')
    await page.getByLabel('Crop stock alert level for 11').fill('1500000')

    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)$/ }).click()
    await expect.poll(() => sent.dayCheck.length).toBe(1)

    expect(sent.dayCheck[0].crop_ceilings).toEqual({
      [CAPITAL]: 0,
      // And a ceiling over a million arrives as itself, unrounded and
      // unclamped: the box has no `max` for exactly this reason.
      [RELAY]: 1500000,
    })
  })

  test('an emptied merchant box is unknown, not a committed zero', async ({ page }) => {
    // Base capacity and the Trade Office bonus were the two boxes in this
    // panel with no `'' ?` guard, so `Number('')` wrote a 0 straight into the
    // model. The 0 then went two different wrong ways: base capacity was
    // dropped by `|| undefined` at the payload, so the box read 0 while the
    // plan was built at the default; the bonus was kept, so an accidentally
    // cleared box silently stopped every Trade Office level adding capacity.
    await isolate(page)
    await seed(page)
    await openAccount(page)

    await page.getByLabel('Merchant base capacity').fill('')
    await page.getByLabel('Trade Office bonus per level').fill('')

    const model = await stored(page, 'planner_merchant_model')
    expect(model.base_capacity).toBeUndefined()
    expect(model.bonus_per_to_level).toBeUndefined()
    // And the boxes say so, rather than showing a zero nobody typed.
    await expect(page.getByLabel('Merchant base capacity')).toHaveValue('')
    await expect(page.getByLabel('Trade Office bonus per level')).toHaveValue('')
  })

  test('a base capacity in the millions arrives as itself', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page)
    await openAccount(page)

    await page.getByLabel('Merchant base capacity').fill('1250000')
    await buildPlan(page, sent)

    expect(sent.plan[0].merchant_base_capacity).toBe(1250000)
  })

  test('a headroom a file supplied is not rounded by the box that shows it', async ({ page }) => {
    // The display was `Math.round(x * 1000) / 10`, so a server-calibrated
    // 0.1234 rendered as 12.3 -- and the box writes back what it renders, so
    // the first keystroke anywhere near it committed 0.123. A display that
    // silently rewrites the value it displays is not a display.
    await isolate(page)
    await seed(page, {
      planner_merchant_model: {
        base_capacity: 2500,
        bonus_per_to_level: 0.2,
        merchant_reserve: 2,
        merchant_headroom: 0.1234,
      },
    })
    await openAccount(page)

    await expect(
      page.getByLabel("Merchant headroom, percent of each village's budget")
    ).toHaveValue('12.34')
  })

  test('a blank fill is refused rather than derived from 0%', async ({ page }) => {
    // `Number('') / 100` is 0, so an emptied "Emptied to" box asked the server
    // to build a night profile that arrives at 0% -- which is not what a blank
    // box says. It says nothing, and nothing is not a threshold.
    const sent = await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')
    // Day & night, beside the windows it derives from: the panel moved off
    // Targets, where its own warning told the operator not to press it.
    await page.getByRole('button', { name: 'Day & night' }).click()

    await page.getByLabel('Emptied to').fill('')
    await page.getByRole('button', { name: /^Derive from stores/ }).click()

    await expect(page.getByText(/blank box is not 0%/)).toBeVisible()
    expect(sent.nightProfile).toHaveLength(0)
  })

  test('a foreign target with no coordinates is a draft, not a tribute at (0|0)', async ({
    page,
  }) => {
    // `Number(t.x) || 0` turned a blank coordinate into the map centre, and
    // the row was not marked a draft either -- so a half-typed tribute was
    // planned against a village that is not where it is, with distance, cycle
    // and merchant count all computed from the wrong tile.
    const sent = await isolate(page)
    await seed(page)
    await openAccount(page)
    await page.getByRole('button', { name: /Add target/ }).click()

    await page.getByLabel('Foreign target 1 name').fill('Ally hub')
    await page.getByLabel('Foreign target 1 crop per hour').fill('25700')
    // A new row seeds (0|0) -- the map centre -- and the operator clears the
    // box to type the real tile. That cleared box is the state under test.
    await page.getByLabel('Foreign target 1 x coordinate').fill('')

    await expect(page.getByTitle(/Needs a name, a crop rate and a coordinate/)).toBeVisible()

    await buildPlan(page, sent)
    expect(sent.plan[0].foreign_targets).toEqual([])
  })

  test('Routes this run at 0 asks for reconcile-only, not three live routes', async ({ page }) => {
    // The backend documents 0 as "reconcile only: read, disable what the plan
    // no longer wants, and create nothing -- the safe first half of a profile
    // switch". `Number(routesPerRun) || MAX_ROUTES_PER_RUN` turned that exact
    // request into three route creations, which is the opposite instruction.
    const sent = await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')
    await buildPlan(page, sent)

    await page.getByLabel('Routes this run', { exact: true }).fill('0')
    await page.getByRole('button', { name: /^Preview \(0 requests\)$/ }).click()
    await expect.poll(() => sent.execute.length).toBe(1)

    expect(sent.execute[0].dry_run).toBe(true)
    expect(sent.execute[0].max_routes_per_run).toBe(0)
  })
})

test.describe('retyping a figure drops the plan the old one produced', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('editing the relay tier clears the route sheet it produced', async ({ page }) => {
    // `relay_for` is in the plan payload and was in neither invalidation
    // dependency array, so the whole Plan stage -- whose rows are copied into
    // the game's own trade-route dialog -- survived a relay-tier edit and went
    // on describing the tier that had just been replaced.
    const sent = await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')
    await buildPlan(page, sent)

    await page.getByRole('button', { name: 'Account' }).click()
    await openDisclosures(page)
    await page
      .getByRole('group', { name: 'Villages 02 forwards material to' })
      .getByLabel('11', { exact: true })
      .check()

    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toHaveCount(0)
  })

})
