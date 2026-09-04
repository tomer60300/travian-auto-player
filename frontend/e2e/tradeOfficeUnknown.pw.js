/**
 * "Unknown" and "typed 0" are different answers about a Trade Office, and the
 * REQUEST has to be able to tell them apart.
 *
 * The bug this pins: `distribution.py` filters the merchant-calibration
 * sample to villages with a config row -- `declared = {c.village_id for c in
 * body.config}` -- so that the finding never names a village whose level
 * nobody typed. Its own comment says why: naming one says "level 0, read the
 * base off this village", and if it is really Trade Office 13 the dialog reads
 * about 9,000, that becomes `merchant_base_capacity`, and every route in the
 * account is sized to cargo the merchants cannot carry. Account-killer #8,
 * reached THROUGH the mechanism meant to settle the model.
 *
 * And the page emitted a config row for EVERY snapshot village,
 * unconditionally, with `trade_office_level: Number(tradeOffice[vid] ?? 0)`.
 * So the filter was a tautology through the app: every village was
 * "declared", and every untyped one read 0.
 *
 * Two things had to change together, and this spec asserts both, because
 * either alone leaves the filter inert:
 *
 *   1. `trade_office_level` is sent only where the operator typed one. With
 *      the key absent the backend still fills 0 -- `Field(default=0)` -- and
 *      `.get(vid, 0)` still floors it for SIZING, which is the safe direction
 *      (understating capacity over-provisions merchants; overstating it
 *      breaches the budget invisibly). So this half is about what the wire
 *      SAYS, and it is what a future backend reading `model_fields_set` needs.
 *   2. a village that declares NOTHING gets no row. That is the half today's
 *      backend can actually act on, because `declared` is a set of row village
 *      ids and not of rows carrying the key.
 *
 * A village that declares something else -- a stock floor, say -- still gets
 * its row, and still gets no `trade_office_level`. That residue is deliberate
 * and is the backend's to close.
 *
 * NO BACKEND AND NO GAME REQUEST: every `/api` call is answered here or
 * ABORTED, and the snapshot is seeded into localStorage rather than fetched.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test tradeOfficeUnknown
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 30002
const TYPED = 30011
const SILENT = 30012

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
  villages: [
    village(CAPITAL, '02', 0, 0),
    village(TYPED, '11', 4, 0),
    village(SILENT, '12', 0, 4),
  ],
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

async function isolate(page) {
  const sent = { plan: [] }
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
    // Fail closed: anything unanticipated would be proxied to the debug backend.
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
      localStorage.setItem(`planner_profiles::${key}`, JSON.stringify({ Day: {} }))
      for (const [name, value] of Object.entries(more)) {
        localStorage.setItem(`${name}::${key}`, JSON.stringify(value))
      }
    },
    [KEY, SNAPSHOT, extra]
  )
}

async function buildPlan(page, sent) {
  const before = sent.plan.length
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await expect.poll(() => sent.plan.length).toBe(before + 1)
  return sent.plan[sent.plan.length - 1]
}

/** The config row for one village, or undefined when the request has none. */
function rowFor(body, villageId) {
  return body.config.find((c) => c.village_id === villageId)
}

test.describe('an untyped Trade Office is unknown on the wire', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('a level typed on the page rides its own row, and 0 is an answer', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByLabel('Trade Office level for 11').fill('13')
    // A typed 0 is a real answer -- "I checked in game, there is no Trade
    // Office here" -- and it is exactly the sample the backend wants to read
    // the merchant base off, so it must travel rather than being dropped as
    // if it were blank.
    await page.getByLabel('Trade Office level for 12').fill('0')

    const body = await buildPlan(page, sent)
    expect(rowFor(body, TYPED).trade_office_level).toBe(13)
    expect(rowFor(body, SILENT).trade_office_level).toBe(0)
  })

  test('a village nobody typed anything about has no row at all', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByLabel('Trade Office level for 11').fill('13')

    const body = await buildPlan(page, sent)
    // The one village with a typed level is the only row. `declared` on the
    // backend is a set of row village ids, so this is the half that makes the
    // calibration filter bite: the level-0 sample it names can no longer be a
    // village whose level nobody has checked in game.
    expect(body.config.map((c) => c.village_id)).toEqual([TYPED])
    expect(rowFor(body, CAPITAL)).toBeUndefined()
    expect(rowFor(body, SILENT)).toBeUndefined()
  })

  test('a village that declares something ELSE keeps its row without a level', async ({
    page,
  }) => {
    const sent = await isolate(page)
    // A floor makes section 7's attendance answer required, and the plan is
    // refused without it -- so it is seeded here rather than being what this
    // test accidentally measures. `npcAttendance.pw.js` owns that rule.
    await seed(page, { planner_npc_attended: { Day: true } })
    await page.goto('/resource-planner')

    await page
      .getByLabel('NPC-backed stock floor for 12, percent of warehouse')
      .fill('30')

    const body = await buildPlan(page, sent)
    const row = rowFor(body, SILENT)
    // The row exists, because the floor has to reach the planner.
    expect(row.stock_floor_fraction).toBeCloseTo(0.3)
    // And it says nothing about a Trade Office, which is the truth: nobody
    // typed one. The backend still reads 0 for sizing through
    // `VillageConfig.trade_office_level`'s own default, which is the safe
    // direction; what changed is that the request no longer CLAIMS 0.
    expect(row).not.toHaveProperty('trade_office_level')
    // And the villages with nothing typed still have no row.
    expect(body.config.map((c) => c.village_id)).toEqual([SILENT])
  })

  test('a level loaded from a stored setup travels too', async ({ page }) => {
    // The levels are hand-typed once per account and cached, so the common
    // path is a hydrated map rather than a keystroke.
    const sent = await isolate(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 20, [TYPED]: 0 } })
    await page.goto('/resource-planner')
    await expect(page.getByLabel('Trade Office level for 02')).toHaveValue('20')

    const body = await buildPlan(page, sent)
    expect(body.config.map((c) => c.village_id)).toEqual([CAPITAL, TYPED])
    expect(rowFor(body, CAPITAL).trade_office_level).toBe(20)
    expect(rowFor(body, TYPED).trade_office_level).toBe(0)
  })

  test('an emptied box goes back to unknown, and takes the row with it', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { planner_trade_office: { [TYPED]: 13 } })
    await page.goto('/resource-planner')

    await page.getByLabel('Trade Office level for 11').fill('')

    const body = await buildPlan(page, sent)
    // Blank is not 0. Clearing the box has to be able to UNDO an answer, or
    // the sample the backend reads a capacity off can never be corrected.
    expect(body.config).toEqual([])
  })
})
