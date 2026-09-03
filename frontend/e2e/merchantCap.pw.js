/**
 * The per-village merchant cap and the two account-wide merchant levers, DRIVEN.
 *
 * `plannerSetup.test.js` covers the file's round trip and the two predicates;
 * what it cannot see is a change event. Three new inputs on the Snapshot stage
 * write three pieces of state that decide the route set, and the same gap that
 * left the role panel's six callbacks uninvoked would leave these unpinned --
 * so they are driven where they live, through the page's own setters.
 *
 * The cap is the one worth driving hardest. It is the only owned field whose
 * loss or mistyping is INVISIBLE in the safe direction: a Trade Office level
 * typed too low over-provisions merchants, while a cap that never reaches the
 * request lets the plan commit sixteen merchants at a village the operator
 * holds to eight and report the sheet as feasible.
 *
 * NO BACKEND AND NO GAME REQUEST, by the two fail-closed mechanisms
 * `roleTemplates.pw.js` documents: `page.route('** /api/**')` answers the two
 * calls the shell makes and ABORTS everything else, and the snapshot is seeded
 * into localStorage rather than fetched, so no code path here can ask the game
 * for anything.
 *
 * The assertions read `localStorage`, because the stored maps are what the plan
 * request is built out of -- a rendered input's value would confirm React
 * re-rendered without confirming what it re-rendered FROM.
 *
 * Running it:
 *   cd frontend
 *   npx playwright install chromium   # once per machine
 *   npx playwright test merchantCap
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 20002
// 19 merchants, deliberately: it is the village on the real account where "cap
// at 8 busy" and "hold 12 back" stop being the same sentence, and it is the
// only shape in which the fleet bound can be seen to bite.
const NINETEEN = 20026
// Merchant count 0, which is `/snapshot`'s sentinel for a count it could not
// READ -- it warns "no merchant count read for ..." beside it. Unknown is not
// zero, so a cap here is not a mistake anyone can prove.
const UNREAD = 20031

function village(id, name, x, y, merchants) {
  return {
    village_id: id,
    name,
    x,
    y,
    merchants_total: merchants,
    merchants_free: merchants,
    lumber_per_hour: 3000,
    clay_per_hour: 1400,
    iron_per_hour: 1300,
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
    village(CAPITAL, '02', 0, 0, 20),
    village(NINETEEN, '26', 4, 0, 19),
    village(UNREAD, '31', 8, 0, 0),
  ],
  map_span: 800,
  speed_fields_per_hour: 16,
  requests_used: 0,
  warnings: [],
}

/** Everything the shell asks for, and a hard stop for anything else. */
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
    // Fail closed: a planner call that slipped through would be proxied to the
    // debug backend, and this suite must never depend on one running.
    return route.abort('blockedbyclient')
  })
}

async function seed(page) {
  await page.addInitScript(
    ([key, snapshot]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snapshot))
      // Fresh, so the stale-snapshot gate is not what this spec measures.
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
    },
    [KEY, SNAPSHOT],
  )
}

/** The village table lives on the Snapshot stage, which is where the page opens. */
async function openTable(page) {
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Most merchants busy at once for 02')).toBeVisible()
}

async function stored(page, key) {
  const raw = await page.evaluate((k) => localStorage.getItem(k), `${key}::${KEY}`)
  return raw == null ? null : JSON.parse(raw)
}

test.describe('the merchant cap, driven', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test.beforeEach(async ({ page }) => {
    await isolate(page)
    await seed(page)
  })

  test('typing a cap stores it against that village and no other', async ({ page }) => {
    await openTable(page)

    await page.getByLabel('Most merchants busy at once for 02').fill('8')

    expect(await stored(page, 'planner_max_busy')).toEqual({ [CAPITAL]: 8 })
  })

  test('clearing the box removes the ceiling rather than storing a zero', async ({ page }) => {
    // The two are different answers: no ceiling is the fleet less the reserve,
    // and 0 grounds the village. A cleared box that stored 0 would silently
    // stop a village shipping at all.
    await openTable(page)
    const box = page.getByLabel('Most merchants busy at once for 02')
    await box.fill('8')
    expect(await stored(page, 'planner_max_busy')).toEqual({ [CAPITAL]: 8 })

    await box.fill('')

    expect(await stored(page, 'planner_max_busy')).toEqual({})
  })

  test('a cap of 0 is stored, because it says every route from here breaches', async ({
    page,
  }) => {
    // Not "the village sends nothing", which is what four surfaces used to
    // claim: the budget is soft, so the routes are still planned and each one
    // becomes a breach that refuses the sheet.
    await openTable(page)

    await page.getByLabel('Most merchants busy at once for 02').fill('0')

    expect(await stored(page, 'planner_max_busy')).toEqual({ [CAPITAL]: 0 })
  })

  test('the column says what a cap of 0 does, not that it grounds the village', async ({
    page,
  }) => {
    // The tooltip is the only place the page explains the field, so a
    // falsehood here is the one the operator reads.
    await openTable(page)

    const title = await page
      .getByRole('columnheader', { name: 'Max busy' })
      .getAttribute('title')

    expect(title).not.toContain('grounds')
    expect(title).toContain('breach')
  })

  test('a cap past the village fleet is marked invalid and names the fleet', async ({ page }) => {
    // The bound the file cannot check, and the one the backend answers with a
    // 422 over the whole plan. "422" on a Build click is not a sentence that
    // leads anyone back to this cell, so the cell has to say it.
    await openTable(page)
    const box = page.getByLabel('Most merchants busy at once for 26')

    await box.fill('20')

    await expect(box).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('only 19 merchants here')).toBeVisible()

    // 19 is reachable, so the warning goes away rather than sticking.
    await box.fill('19')
    await expect(box).not.toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('only 19 merchants here')).toHaveCount(0)
  })

  test('a cap is not flagged where the merchant count was never read', async ({ page }) => {
    // The cell computes the fleet bound from live state so the operator sees
    // it where they typed, and the backend refuses the same thing with a 422.
    // Both skip a count of 0, because that is the snapshot saying it could not
    // read one -- and the cell has to agree, or it flags a plan that plans.
    await openTable(page)
    const box = page.getByLabel('Most merchants busy at once for 31')

    await box.fill('8')

    await expect(box).not.toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('only 0 merchants here')).toHaveCount(0)
    expect(await stored(page, 'planner_max_busy')).toEqual({ [UNREAD]: 8 })
  })

  test('the cap survives a reload, which is the whole point of storing it', async ({ page }) => {
    await openTable(page)
    await page.getByLabel('Most merchants busy at once for 02').fill('8')

    await page.reload()

    await expect(page.getByLabel('Most merchants busy at once for 02')).toHaveValue('8')
  })
})

test.describe('the account-wide merchant levers, driven', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test.beforeEach(async ({ page }) => {
    await isolate(page)
    await seed(page)
  })

  test('the reserve and the headroom write the merchant model', async ({ page }) => {
    // Both fields existed on the request and neither was ever sent, so the
    // backend's defaults were the only values this page could produce.
    await openTable(page)

    await page.getByLabel('Merchants held in reserve at every village').fill('4')
    await page.getByLabel("Merchant headroom, percent of each village's budget").fill('25')

    const model = await stored(page, 'planner_merchant_model')
    expect(model.merchant_reserve).toBe(4)
    // Stored as the fraction the request carries, shown as the percent typed.
    expect(model.merchant_headroom).toBeCloseTo(0.25, 6)
  })

  test('a reserve of 0 is kept, because shipping with everything is an answer', async ({
    page,
  }) => {
    await openTable(page)

    await page.getByLabel('Merchants held in reserve at every village').fill('0')

    expect((await stored(page, 'planner_merchant_model')).merchant_reserve).toBe(0)
  })

  test('a headroom of 0 is kept, because it is the tight-packing answer', async ({ page }) => {
    // `|| undefined` would drop it and silently restore the 10% default, which
    // is the opposite of what the operator asked for.
    await openTable(page)

    await page.getByLabel("Merchant headroom, percent of each village's budget").fill('0')

    expect((await stored(page, 'planner_merchant_model')).merchant_headroom).toBe(0)
  })

  test('they default to the planner values, so exposing them changes no plan', async ({ page }) => {
    await openTable(page)

    await expect(page.getByLabel('Merchants held in reserve at every village')).toHaveValue('2')
    await expect(
      page.getByLabel("Merchant headroom, percent of each village's budget"),
    ).toHaveValue('10')
  })

  test('a merchant model stored before these existed still shows what is in force', async ({
    page,
  }) => {
    // Every account that has ever opened this page has a stored model with two
    // keys and not four. Read as-is it would leave both boxes blank while the
    // plan used 2 and 10%, which is an input that does not say what it does.
    await page.addInitScript(
      ([key]) => {
        localStorage.setItem(
          `planner_merchant_model::${key}`,
          JSON.stringify({ base_capacity: 2200, bonus_per_to_level: 0.2 }),
        )
      },
      [KEY],
    )

    await openTable(page)

    // The calibration that WAS stored still wins; only the gaps are filled.
    await expect(page.getByLabel('Merchant base capacity')).toHaveValue('2200')
    await expect(page.getByLabel('Merchants held in reserve at every village')).toHaveValue('2')
    await expect(
      page.getByLabel("Merchant headroom, percent of each village's budget"),
    ).toHaveValue('10')
  })
})
