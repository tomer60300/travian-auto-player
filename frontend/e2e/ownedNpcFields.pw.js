/**
 * The two owned fields the backend has always accepted and this app could not
 * send, DRIVEN — `VillageConfig.npc_feedstock` and
 * `RoleTemplate.assumed_crop_per_hour`.
 *
 * Asserted on the plan REQUEST BODY, for the reason `npcAttendance.pw.js` gives:
 * what these controls have to do is put a value in a payload, and a rendered
 * input showing the right text would confirm React re-rendered without
 * confirming what it sends. The two fields fail in opposite directions if
 * mis-wired, and both are covered here:
 *
 *   * `npc_feedstock` must be OMITTED for almost every village. Absent means
 *     "derive it" — everything the village is not drawing on — which is the
 *     honest default and what section 7 describes for the capital. An empty
 *     list is the picker mid-edit and the backend refuses one, so a wiring that
 *     sent `[]` would 422 every plan the moment a box was opened.
 *   * `assumed_crop_per_hour` must be SENT with its sign, and 0 must be sent
 *     while blank must not. It moves no cargo — its only effect is a drift
 *     warning — so a value silently dropped here is a check that silently
 *     stops happening, which is the failure nobody notices.
 *
 * NO BACKEND AND NO GAME REQUEST: every `/api` call is answered here or
 * ABORTED, and the snapshot is seeded into localStorage rather than fetched.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test ownedNpcFields
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 40002
const HAMMER = 40001

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
    crop_per_hour: -5880,
    crop_draining: true,
    lumber_stock: 100_000,
    clay_stock: 100_000,
    iron_stock: 100_000,
    crop_stock: 100_000,
    warehouse_capacity: 400_000,
    granary_capacity: 400_000,
  }
}

const SNAPSHOT = {
  villages: [village(CAPITAL, '02', 0, 0), village(HAMMER, '01', 3, 0)],
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
  plan_digest: 'b'.repeat(64),
}

async function isolate(page) {
  const sent = []
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
      sent.push(route.request().postDataJSON())
      return route.fulfill({ json: EMPTY_PLAN })
    }
    return route.abort('blockedbyclient')
  })
  return sent
}

/**
 * A connected account with a fresh snapshot. No profile windows and no stock
 * floor by default, so `npc_attended` is not in play here — this spec is about
 * the other two fields, and a required-attendance gate would only obscure them.
 */
async function seed(page, { floor = null, roles = null } = {}) {
  await page.addInitScript(
    ([key, snap, capital, stockFloor, roleMap]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      // One round-the-clock profile: no window, so no attendance is required.
      localStorage.setItem(`planner_profiles::${key}`, JSON.stringify({ Always: {} }))
      localStorage.setItem(`planner_profile_windows::${key}`, JSON.stringify({}))
      if (stockFloor != null) {
        localStorage.setItem(
          `planner_stock_floor::${key}`,
          JSON.stringify({ [capital]: stockFloor })
        )
      }
      if (roleMap != null) {
        localStorage.setItem(`planner_village_roles::${key}`, JSON.stringify(roleMap))
      }
    },
    [KEY, SNAPSHOT, CAPITAL, floor, roles]
  )
}

/** The config row for one village out of the last plan request. */
function configFor(body, villageId) {
  return body.config.find((row) => row.village_id === villageId)
}

/** The feedstock cell's disclosure, by the village it belongs to.
 *
 * The `<summary>` and not the screen-reader prefix inside it: the prefix names
 * the column for someone who cannot see the header, and the ANSWER is the text
 * node beside it. Asserting on the span alone would pass while the cell showed
 * nothing at all, which is the exact defect "derived is the resting state" is
 * about.
 */
function feedstockCell(page, name) {
  return page.locator('summary').filter({ hasText: `NPC converts from, for ${name}:` })
}

test.describe('npc_feedstock, per village', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('derived is the resting state, and it sends nothing', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { floor: 0.3 })
    await page.goto('/resource-planner')

    // The cell says the answer rather than sitting empty: "derived" IS an
    // answer, and a blank box would read as something nobody has decided.
    await expect(feedstockCell(page, '02')).toHaveText(/derived/)

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(configFor(sent[0], CAPITAL)).not.toHaveProperty('npc_feedstock')
  })

  test('an override rides the request in the game resource order', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { floor: 0.3 })
    await page.goto('/resource-planner')

    // Ticked out of order on purpose: the request must not depend on the click
    // order, because `/plan` digests its own response and `/plan/yaml` demands
    // that digest back.
    await feedstockCell(page, '02').click()
    await page.getByLabel('NPC may convert Crop at 02').check()
    await page.getByLabel('NPC may convert Clay at 02').check()

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(configFor(sent[0], CAPITAL).npc_feedstock).toEqual(['clay', 'crop'])
  })

  test('a picker opened and not ticked is not an override', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { floor: 0.3 })
    await page.goto('/resource-planner')

    await feedstockCell(page, '02').click()
    // Tick and untick: the stored value is now an empty list, which is the
    // shape the backend refuses.
    await page.getByLabel('NPC may convert Clay at 02').check()
    await page.getByLabel('NPC may convert Clay at 02').uncheck()

    await expect(page.getByText(/Nothing ticked, so this is not an override yet/)).toBeVisible()

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    // Dropped, not sent as `[]`.
    expect(configFor(sent[0], CAPITAL)).not.toHaveProperty('npc_feedstock')
  })

  test('back to derived removes the override entirely', async ({ page }) => {
    await isolate(page)
    await seed(page, { floor: 0.3 })
    await page.goto('/resource-planner')

    await feedstockCell(page, '02').click()
    await page.getByLabel('NPC may convert Clay at 02').check()
    await page.getByRole('button', { name: 'Back to derived' }).click()

    await expect(feedstockCell(page, '02')).toHaveText(/derived/)
    const stored = await page.evaluate(
      (key) => localStorage.getItem(`planner_npc_feedstock::${key}`),
      KEY
    )
    expect(JSON.parse(stored)).toEqual({})
  })

  test('a village with no floor is told the override converts nothing', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    await feedstockCell(page, '01').click()
    // Scoped to this village's own picker. A closed `<details>` keeps its
    // subtree in the DOM, so an unscoped match finds the note in every row
    // that has no floor -- and would pass while the OPEN one said nothing.
    await expect(
      page
        .getByRole('group', { name: 'Stores NPC may convert from at 01' })
        .getByText(/No stock floor here, so nothing converts/)
    ).toBeVisible()
  })
})

test.describe('assumed_crop_per_hour, per role', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  async function openRolePanel(page) {
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: 'Targets' }).click()
    await page.getByText('Role templates', { exact: true }).click()
  }

  test('a negative assumption reaches the request with its sign', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { roles: { [HAMMER]: 'full_off' } })
    await openRolePanel(page)

    // 01 reads -5,880/h and is crop-negative BY DESIGN. A non-negative bound
    // would refuse the account's own figure.
    await page
      .getByLabel('Assumed net crop per hour for a Full off (Hammer) village')
      .fill('-5880')
    await expect(page.getByText('checked, ships nothing')).toBeVisible()

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(sent[0].roles.full_off.assumed_crop_per_hour).toBe(-5880)
  })

  test('zero is a real claim and is sent as one', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { roles: { [HAMMER]: 'def' } })
    await openRolePanel(page)

    await page.getByLabel('Assumed net crop per hour for a DEF village').fill('0')

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(sent[0].roles.def.assumed_crop_per_hour).toBe(0)
  })

  test('an assumption alone is a template, so the role stops being refused', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { roles: { [HAMMER]: 'def' } })
    await openRolePanel(page)

    // Before: a village claiming a role with no template at all is skipped
    // from `roles`, which is what makes the backend's 422 reachable. The
    // panel's own warning reads the same `isEmptyTemplate` predicate the
    // request does, which is the agreement being pinned here.
    await expect(page.getByText(/DEF has villages\s+but no template/)).toBeVisible()

    await page.getByLabel('Assumed net crop per hour for a DEF village').fill('-100')

    // After: the assumption moves nothing, but it IS a template — and a role
    // whose template is read as "empty" is dropped from the request entirely,
    // which would silently drop the only thing the figure can do.
    await expect(page.getByText(/DEF has villages\s+but no template/)).toHaveCount(0)
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(sent[0].roles.def.assumed_crop_per_hour).toBe(-100)
  })

  test('a blank box is no assumption, not an assumption of zero', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page, { roles: { [HAMMER]: 'def' } })
    await openRolePanel(page)

    await page.getByLabel('DEF Lumber mode').selectOption('absolute')
    await page.getByLabel('Assumed net crop per hour for a DEF village').fill('500')
    await page.getByLabel('Assumed net crop per hour for a DEF village').fill('')
    await expect(page.getByText('not checked')).toHaveCount(5)

    await page.getByRole('button', { name: /^Build plan/ }).click()
    await page.getByRole('button', { name: 'Plan', exact: true }).click()
    await expect(page.getByText(/^Routes$/)).toBeVisible()
    expect(sent[0].roles.def).not.toHaveProperty('assumed_crop_per_hour')
  })
})
