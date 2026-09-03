/**
 * The Role-templates panel, DRIVEN.
 *
 * `RoleTemplates.test.jsx` renders it with `renderToString` and asserts what it
 * SAYS. What that cannot see is a change event, so not one of the panel's four
 * callbacks -- across six invocation sites -- had ever been invoked by a test,
 * and `setTemplateAllocation`'s "keep DELETES the entry" rule was unpinned.
 * That rule is not cosmetic: keep is the absence of a target, so an entry left
 * behind would stop the village's own answer falling through and hold four
 * defensive villages at whatever figure was last typed.
 *
 * Why Playwright rather than a DOM test renderer: `@playwright/test` is already
 * a devDependency (the login visual spec), so the handlers cost no new
 * dependency at all -- and the panel lives inside the planner's Allocate stage,
 * which only exists once a snapshot has arrived. Driving it there exercises the
 * page's own setters, which is where the rules under test actually live.
 *
 * NO BACKEND AND NO GAME REQUEST. Two mechanisms, both fail-closed:
 *
 *   1. `page.route('** /api/**')` answers the two calls the shell makes and
 *      ABORTS everything else, so a request this spec did not anticipate fails
 *      rather than reaching the Vite proxy (which forwards /api to the debug
 *      backend on 8001). `routeWebSocket` closes the log stream for the same
 *      reason.
 *   2. The snapshot is SEEDED into localStorage rather than fetched. The page
 *      hydrates it per account key, so the Allocate stage is reachable with
 *      zero network -- and there is no code path here that could ask the game
 *      for anything.
 *
 * The assertions read `localStorage` rather than the rendered inputs, and
 * deliberately: what a callback must do is change the stored template, and a
 * `<select>`'s rendered value would confirm React re-rendered without
 * confirming what it re-rendered FROM. The stored map is the thing the request
 * is built out of.
 *
 * Running it:
 *   cd frontend
 *   npx playwright install chromium   # once per machine
 *   npx playwright test roleTemplates
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
// `accountKey` in ResourcePlanner.jsx: server URL with trailing slashes
// stripped, a pipe, the player name. Every planner storage key is namespaced
// with it, because village ids are per account.
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 20002
const DEF_A = 20011
const DEF_B = 20013

function village(id, name, x, y, lumber) {
  return {
    village_id: id,
    name,
    x,
    y,
    merchants_total: 20,
    merchants_free: 20,
    lumber_per_hour: lumber,
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
    village(CAPITAL, '02', 0, 0, 60_000),
    village(DEF_A, '11', 4, 0, 1500),
    village(DEF_B, '13', 0, 4, 1500),
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
    // Fail closed. A planner call that slipped through would be proxied to the
    // debug backend, and this suite must never depend on one running -- nor
    // reach anything that could talk to the game.
    return route.abort('blockedbyclient')
  })
}

/** A connected account with a fresh snapshot and two villages already DEF. */
async function seed(page) {
  await page.addInitScript(
    ([key, snapshot, defA, defB]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snapshot))
      // Fresh, so the stale-snapshot gate is not what this spec is measuring.
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      localStorage.setItem(
        `planner_village_roles::${key}`,
        JSON.stringify({ [defA]: 'def', [defB]: 'def' }),
      )
    },
    [KEY, SNAPSHOT, DEF_A, DEF_B],
  )
}

async function openPanel(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Allocate' }).click()
  // The panel is collapsed by default; its own summary is the disclosure.
  await page.getByText('Role templates', { exact: true }).click()
  await expect(page.getByRole('button', { name: 'Clear' })).toHaveCount(0)
}

/** The stored role templates, which is what a plan request is built out of. */
async function stored(page) {
  const raw = await page.evaluate((key) => localStorage.getItem(`planner_role_templates::${key}`), KEY)
  return raw == null ? null : JSON.parse(raw)
}

test.describe('role templates, driven', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test.beforeEach(async ({ page }) => {
    await isolate(page)
    await seed(page)
  })

  test('the mode select and the value input both write the template', async ({ page }) => {
    await openPanel(page)

    // Site 1: onAllocation with a mode. The value box is disabled while the
    // mode is keep, so this has to come first -- which is the interaction
    // order the operator is forced into too.
    await page.getByLabel('DEF Lumber mode').selectOption('absolute')
    expect(await stored(page)).toEqual({ def: { allocations: { lumber: { mode: 'absolute', value: 0 } } } })

    // Site 2: onAllocation with a value.
    await page.getByLabel('DEF Lumber value').fill('8372')
    expect((await stored(page)).def.allocations.lumber).toEqual({ mode: 'absolute', value: 8372 })
  })

  test('setting a resource back to keep DELETES the entry', async ({ page }) => {
    // Keep is the ABSENCE of a target, not a target of its own: a resource the
    // template says keep about must fall through to whatever the village
    // itself says, which is exactly what an absent entry does. An entry left
    // behind as `{mode: 'keep'}` would answer for the village instead.
    await openPanel(page)
    await page.getByLabel('DEF Lumber mode').selectOption('absolute')
    await page.getByLabel('DEF Lumber value').fill('8372')
    await page.getByLabel('DEF Clay mode').selectOption('percentage')

    await page.getByLabel('DEF Lumber mode').selectOption('keep')

    const template = (await stored(page)).def
    expect(Object.keys(template.allocations)).toEqual(['clay'])
    // And only that resource: the whole point of a per-resource template is
    // that clearing its lumber leaves its clay alone.
    expect(template.allocations.clay.mode).toBe('percentage')
  })

  test('the spend box writes the template, and clearing it removes the figure', async ({ page }) => {
    // Site 3: onSpend. The empty string is a DELETE rather than a zero,
    // because zero says "measured, and it spends none", which is a claim.
    await openPanel(page)

    await page.getByLabel('Lumber spent per hour by a DEF village').fill('8372')
    expect((await stored(page)).def.consumption).toEqual({ lumber: 8372 })

    await page.getByLabel('Lumber spent per hour by a DEF village').fill('')
    expect((await stored(page)).def.consumption).toEqual({})
  })

  test('the relay select writes all three of its answers', async ({ page }) => {
    // Site 4: onPatch with may_relay. Three states, and the unset one is not
    // false -- it means "take the role's own answer", which is what almost
    // every template says.
    await openPanel(page)

    await page.getByLabel('Whether a DEF village may relay').selectOption('yes')
    expect((await stored(page)).def.may_relay).toBe(true)

    await page.getByLabel('Whether a DEF village may relay').selectOption('no')
    expect((await stored(page)).def.may_relay).toBe(false)

    await page.getByLabel('Whether a DEF village may relay').selectOption('')
    expect((await stored(page)).def.may_relay).toBeNull()
  })

  test('the by-design checkbox writes the template', async ({ page }) => {
    // Site 5: onPatch with crop_negative_by_design. It moves a finding's
    // severity, so a checkbox that did not persist would leave a CRITICAL the
    // operator believes they have downgraded.
    await openPanel(page)

    await page.getByLabel('A DEF village is crop-negative by design').check()
    expect((await stored(page)).def.crop_negative_by_design).toBe(true)

    await page.getByLabel('A DEF village is crop-negative by design').uncheck()
    expect((await stored(page)).def.crop_negative_by_design).toBe(false)
  })

  test('Clear removes the whole role, and the panel then warns about it', async ({ page }) => {
    // Site 6: onClear. The key going away is the point -- an empty template
    // left in the map is a role the plan is REFUSED over (the backend answers
    // a claimed role with no template with a 422), while an absent one is a
    // role the operator can see is missing.
    await openPanel(page)
    await page.getByLabel('DEF Lumber mode').selectOption('absolute')
    await expect(page.getByRole('button', { name: 'Clear' })).toHaveCount(1)

    await page.getByRole('button', { name: 'Clear' }).click()

    expect(await stored(page)).toEqual({})
    await expect(page.getByRole('button', { name: 'Clear' })).toHaveCount(0)
    // Two villages still claim DEF, so the panel has to name it.
    await expect(page.getByText('has villages')).toBeVisible()
  })

  // ── An EMPTIED template is not a template ────────────────────────────
  //
  // Every setter writes through the role KEY (`{...prev, [role]: {...}}`) and
  // none of them deletes the role when its last figure goes, so a template the
  // operator has emptied box by box survives as `{"def": {"consumption": {}}}`.
  // The backend accepts that -- it is a template, formally -- and plans the four
  // defensive villages at their own 1,500/h with spend 0 and an empty
  // `role_deviations`, reported feasible. `Clear` was the only door that led to
  // the 422; these three are the doors that led past it, and the panel stayed
  // silent through all of them because its warning read the same key.
  //
  // Driven rather than unit-tested because the shape left behind is the SETTER's
  // doing, and `rolesForRequest` can only be given a shape somebody believed in.

  test('emptying the last spend box warns, the same as never typing one', async ({ page }) => {
    await openPanel(page)
    await page.getByLabel('Lumber spent per hour by a DEF village').fill('8372')
    await expect(page.getByText('has villages')).toHaveCount(0)

    await page.getByLabel('Lumber spent per hour by a DEF village').fill('')

    // The key is still there, and that is the point: what must change is what
    // the page and the request make of it.
    expect(await stored(page)).toEqual({ def: { consumption: {} } })
    await expect(page.getByText('has villages')).toBeVisible()
    await expect(page.getByText('0 typed, covering 0 village(s)')).toBeVisible()
  })

  test('setting the last mode back to keep warns, because keep is not a figure', async ({
    page,
  }) => {
    await openPanel(page)
    await page.getByLabel('DEF Lumber mode').selectOption('absolute')
    await page.getByLabel('DEF Lumber value').fill('8372')
    await expect(page.getByText('has villages')).toHaveCount(0)

    await page.getByLabel('DEF Lumber mode').selectOption('keep')

    expect(await stored(page)).toEqual({ def: { allocations: {} } })
    await expect(page.getByText('has villages')).toBeVisible()
    await expect(page.getByText('0 typed, covering 0 village(s)')).toBeVisible()
  })

  test('unticking by-design warns, because false is stored rather than removed', async ({
    page,
  }) => {
    await openPanel(page)
    await page.getByLabel('A DEF village is crop-negative by design').check()
    await expect(page.getByText('has villages')).toHaveCount(0)

    await page.getByLabel('A DEF village is crop-negative by design').uncheck()

    expect(await stored(page)).toEqual({ def: { crop_negative_by_design: false } })
    await expect(page.getByText('has villages')).toBeVisible()
    await expect(page.getByText('0 typed, covering 0 village(s)')).toBeVisible()
  })

  test('a relay REFUSAL is a template, so it does not warn', async ({ page }) => {
    // The boundary of the rule above. Unset means "take the role's own
    // default", so `may_relay: false` is the operator overriding that default
    // and the one thing the template says. Treating it as emptiness would
    // refuse a plan over a template that exists.
    await openPanel(page)

    await page.getByLabel('Whether a DEF village may relay').selectOption('no')

    expect(await stored(page)).toEqual({ def: { may_relay: false } })
    await expect(page.getByText('has villages')).toHaveCount(0)
    await expect(page.getByText('1 typed, covering 2 village(s)')).toBeVisible()
  })

  test('the panel counts the villages a typed template stands in for', async ({ page }) => {
    // The claim of a template is "one profile, four villages", so the count is
    // the number that says whether it is doing that. Rendered from the page's
    // own `roleCounts`, which the string test can only pass in by hand.
    await openPanel(page)

    await expect(page.getByText('0 typed, covering 0 village(s)')).toBeVisible()
    await page.getByLabel('DEF Lumber mode').selectOption('absolute')
    await expect(page.getByText('1 typed, covering 2 village(s)')).toBeVisible()
  })
})
