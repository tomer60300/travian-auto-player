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
 *      hydrates it per account key, so the Allocate stage is reachable with no
 *      backend at all -- and there is no code path here that could ask the game
 *      for anything.
 *
 * "No backend", not "zero network", and the difference was measured rather than
 * reasoned: with `page.on('request')` recording every request, four leave
 * localhost -- `fonts.googleapis.com/css2?family=Roboto` plus the three woff2
 * files it points at on `fonts.gstatic.com`, because `index.html` links Roboto
 * with a `<link rel="stylesheet">`. Every `/api` call is answered by the route
 * handler above (four of them, not two: React's StrictMode runs each effect
 * twice in dev). Nothing reaches the game and nothing reaches :8001, which is
 * the claim that matters -- but "zero network" was simply false, and an
 * isolation claim that is only nearly true is the kind nobody re-measures.
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
async function isolate(page, snapshot = SNAPSHOT) {
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
          villages: snapshot.villages.map((v) => ({ id: v.village_id, name: v.name })),
        },
      })
    }
    // Fail closed. A planner call that slipped through would be proxied to the
    // debug backend, and this suite must never depend on one running -- nor
    // reach anything that could talk to the game.
    return route.abort('blockedbyclient')
  })
}

/**
 * A connected account with a fresh snapshot and the roles its villages claim.
 *
 * Two villages already DEF by default, which is what every test below the
 * first describe block is written against. `roles` and `snapshot` are
 * parameters because "the warning is the way in" needs a village on
 * `troops_off` -- the role the operator actually assigned -- and the payload
 * test needs one village per role, five of them.
 */
async function seed(page, { snapshot = SNAPSHOT, roles = { [DEF_A]: 'def', [DEF_B]: 'def' } } = {}) {
  await page.addInitScript(
    ([key, snap, roleMap]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      // Fresh, so the stale-snapshot gate is not what this spec is measuring.
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      localStorage.setItem(`planner_village_roles::${key}`, JSON.stringify(roleMap))
    },
    [KEY, snapshot, roles],
  )
}

async function openPanel(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Targets' }).click()
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

  test('an emptied template also marks the VILLAGE rows that claim the role', async ({ page }) => {
    // The fourth reader of the same question, and the only surface that names
    // the village rather than the role. It read `roleTemplates[role] == null`
    // while the panel, `rolesForRequest` and `rolesMissingTemplates` had all
    // moved to `isEmptyTemplate` -- so an emptied template left the Snapshot
    // table saying nothing at all: no `aria-invalid`, no "no DEF template yet",
    // while the panel two clicks away said "0 typed" and the plan came back
    // 422. Whichever surface the operator happens to be looking at has to give
    // them the same answer.
    await openPanel(page)
    await page.getByLabel('Lumber spent per hour by a DEF village').fill('8372')

    await page.getByLabel('Lumber spent per hour by a DEF village').fill('')

    await page.getByRole('button', { name: 'Account' }).click()
    // Both villages claim DEF, and the row is where their names are.
    for (const name of ['11', '13']) {
      await expect(page.getByLabel(`Role for ${name}`)).toHaveAttribute('aria-invalid', 'true')
    }
    await expect(page.getByText('no DEF template yet')).toHaveCount(2)
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

// ג”€ג”€ The warning is the way IN ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€
//
// "no Troops off template yet" in the Snapshot row named the problem and
// offered no way to fix it. The figures are on ANOTHER stage, behind a
// COLLAPSED disclosure, in the widest table in the app -- so the operator had
// to know the panel existed, switch stage, find it, open it, and pick their
// role out of five rows. Verbatim: "If you decided to add the role assignment
// in the UI, first let create it in the UI and change resources value".
//
// The first test measures the hazard rather than assuming it: a closed
// `<details>` renders its content with `content-visibility: hidden`, so the
// row IS in the DOM, with a zero-size box, and a `focus()` or a
// `scrollIntoView()` into it silently does nothing. That is why the panel
// opens from its FIRST render when the page has sent the operator to a role,
// and not from an effect a frame later.

/** The village this fixture puts on Troops off -- '11'. */
const OFF = DEF_A
const OFF_NAME = '11'

test.describe('from the warning to the figures', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test.beforeEach(async ({ page }) => {
    await isolate(page)
    // One village, one role, no template: the state the operator was in.
    await seed(page, { roles: { [OFF]: 'troops_off' } })
  })

  test('the figures are in a closed subtree until the disclosure is driven open', async ({
    page,
  }) => {
    // The measurement the design rests on, taken on the path that does NOT go
    // through the new control: reach the Allocate stage by hand and the row is
    // present, laid out nowhere, and unfocusable.
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: 'Targets' }).click()

    const panel = page.locator('details').filter({ hasText: 'Role templates' })
    await expect(panel).toHaveCount(1)
    expect(await panel.evaluate((d) => d.open)).toBe(false)

    const target = page.getByLabel('Troops off Lumber mode')
    await expect(target).toHaveCount(1)
    await expect(target).toBeHidden()
    // The hazard, measured on the running app rather than reasoned about. The
    // box is not empty -- 135x47, so a test that asked for a zero rect would
    // have called this reachable -- but the subtree is SKIPPED: it does not
    // paint, `checkVisibility()` says so, and `focus()` on it leaves
    // `document.activeElement` where it was. That last number is the whole
    // design constraint: a jump that focused before opening would silently do
    // nothing, twice over, because `scrollIntoView` does not move to it either
    // (top 780 in a 1200 viewport, where centring would be 576).
    const closed = await target.evaluate((el) => {
      const box = el.getBoundingClientRect()
      const out = {
        laidOut: box.width > 0 && box.height > 0,
        visible: el.checkVisibility(),
      }
      el.focus()
      out.focusTook = document.activeElement === el
      return out
    })
    expect(closed).toEqual({ laidOut: true, visible: false, focusTook: false })
  })

  test('the warning leads to that role, with the panel open and the caret in it', async ({
    page,
  }) => {
    await page.goto('/resource-planner')
    // The Snapshot row is where the role was assigned and where the warning is.
    await expect(page.getByText('no Troops off template yet')).toBeVisible()

    // ONE action.
    await page.getByRole('button', { name: 'Type the Troops off figures' }).click()

    const panel = page.locator('details').filter({ hasText: 'Role templates' })
    expect(await panel.evaluate((d) => d.open)).toBe(true)

    const target = page.getByLabel('Troops off Lumber mode')
    await expect(target).toBeFocused()
    await expect(target).toBeVisible()

    // On screen, not merely laid out: the panel sits below a card of derivation
    // controls, so arriving without a scroll would arrive at nothing.
    const seen = await target.evaluate((el) => {
      const r = el.getBoundingClientRect()
      return {
        visible: el.checkVisibility(),
        width: Math.round(r.width),
        inViewport: r.top >= 0 && r.bottom <= window.innerHeight,
      }
    })
    expect(seen.visible).toBe(true)
    expect(seen.width).toBeGreaterThan(0)
    expect(seen.inViewport).toBe(true)

    // Unambiguously THAT row, by the pair index.css designs for a table wider
    // than its container: the row takes a tint and its PINNED identity cell
    // takes a coloured left edge. Neither is a focus indicator -- they answer
    // "which of the five rows is this".
    const marked = await target.evaluate((el) => {
      const row = el.closest('tr')
      const edge = row.querySelector('.row-focus-edge')
      return {
        role: row.querySelector('.sticky-col').textContent,
        tint: getComputedStyle(row).backgroundColor,
        edge: getComputedStyle(edge).borderLeftColor,
        edgeIsPinned: edge.classList.contains('sticky-col'),
      }
    })
    expect(marked.role).toContain('Troops off')
    expect(marked.tint).not.toBe('rgba(0, 0, 0, 0)')
    expect(marked.edge).not.toBe('rgba(0, 0, 0, 0)')
    expect(marked.edgeIsPinned).toBe(true)

    // And the table work it landed in the middle of still holds: 1839px of
    // columns in a 1122px container, so the container is marked as
    // overflowing, the Role column is pinned, and the hint is there.
    const table = await target.evaluate((el) => {
      const wrap = el.closest('.overflow-x-auto')
      const head = wrap.querySelector('thead th.sticky-col')
      return {
        client: wrap.clientWidth,
        scroll: wrap.scrollWidth,
        marked: wrap.classList.contains('table-overflowing'),
        pinned: getComputedStyle(head).position,
      }
    })
    expect(table.scroll).toBeGreaterThan(table.client)
    expect(table.marked).toBe(true)
    expect(table.pinned).toBe('sticky')
    await expect(page.locator('.scroll-hint')).toContainText('Role stays pinned')

    // The caret is where the first keystroke goes, and the keystrokes land.
    await target.selectOption('absolute')
    await page.getByLabel('Troops off Lumber value').fill('3200')
    expect((await stored(page)).troops_off.allocations.lumber).toEqual({
      mode: 'absolute',
      value: 3200,
    })

    // The loop closes: the warning that sent them here is gone.
    await page.getByRole('button', { name: 'Account' }).click()
    await expect(page.getByText('no Troops off template yet')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Type the Troops off figures' })).toHaveCount(0)
  })

  test('it is one Tab from the role select and fires on Enter', async ({ page }) => {
    // Item 2 of the UI Definition of Done, and the reason the control is a
    // BUTTON beside the warning rather than a click target on the warning
    // text: the operator who assigned the role with the keyboard is still on
    // the select when they read it.
    await page.goto('/resource-planner')
    await page.getByLabel(`Role for ${OFF_NAME}`).focus()
    await page.keyboard.press('Tab')

    const jump = page.getByRole('button', { name: 'Type the Troops off figures' })
    await expect(jump).toBeFocused()

    await page.keyboard.press('Enter')
    await expect(page.getByLabel('Troops off Lumber mode')).toBeFocused()
  })

  test('the warning is still the role select description, and the new control is named', async ({
    page,
  }) => {
    // The association the select carries has to survive the new control, and
    // the new control needs a name of its own. Text only inside the described
    // element: a screen reader flattens a description to its text, so an
    // interactive child would be announced as words and reachable only by
    // accident. Hence a SIBLING.
    await page.goto('/resource-planner')

    const select = page.getByLabel(`Role for ${OFF_NAME}`)
    await expect(select).toHaveAttribute('aria-invalid', 'true')
    const described = await select.getAttribute('aria-describedby')
    expect(described).toBe(`role-problem-${OFF}`)

    const problem = page.locator(`#${described}`)
    await expect(problem).toHaveText('no Troops off template yet')
    await expect(problem.locator('button')).toHaveCount(0)

    // Its accessible name contains its visible label (WCAG 2.5.3), because it
    // IS its visible label -- no aria-label overriding the words on screen.
    const jump = page.getByRole('button', { name: 'Type the Troops off figures' })
    await expect(jump).toHaveText('Type the Troops off figures')
  })

  test('a second press brings the operator back to the row', async ({ page }) => {
    // The panel is left open behind them, so nothing about the second press
    // changes state -- which is exactly how a jump that only fired once would
    // pass unnoticed. The row has to be re-focused.
    await page.goto('/resource-planner')
    await page.getByRole('button', { name: 'Type the Troops off figures' }).click()
    await expect(page.getByLabel('Troops off Lumber mode')).toBeFocused()

    await page.getByRole('button', { name: 'Account' }).click()
    await page.getByLabel('Trade Office level for 11').focus()
    await page.getByRole('button', { name: 'Type the Troops off figures' }).click()

    await expect(page.getByLabel('Troops off Lumber mode')).toBeFocused()
  })
})

// ג”€ג”€ Every figure, on every role, into the request ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€
//
// "change resources value", the second half of the complaint. The panel's
// columns are rendered by a `map` over five roles and four resources, so a
// column that is editable for DEF is editable for every role BY CONSTRUCTION
// -- but that is an argument about the source, and the source is not what the
// operator types into. Every one of the 35 boxes is typed here, with a
// distinct figure in each so a cross-wired row or column is caught by value
// rather than by presence, and then the plan request is READ: a figure that
// does not reach the payload is a figure the operator typed for nothing.
//
// Crop deliberately has a TARGET box and no spend box -- the snapshot's crop
// rate is already net of upkeep, so a role says what it should KEEP -- which
// is why the counts below are 4 and 3 rather than 4 and 4.

const FEEDER_V = 20031
const CAP_V = 20033

const FIVE = {
  ...SNAPSHOT,
  villages: [
    ...SNAPSHOT.villages,
    village(FEEDER_V, '31', 8, 0, 1500),
    village(CAP_V, '33', 0, 8, 1500),
  ],
}

/** One village per role, so `rolesForRequest` carries all five. */
const ONE_PER_ROLE = {
  [CAPITAL]: 'capital',
  [DEF_A]: 'troops_off',
  [DEF_B]: 'full_off',
  [FEEDER_V]: 'def',
  [CAP_V]: 'feeder',
}

// The labels the panel puts on its controls, mirroring `ROLE_LABEL` in
// src/constants/planner.js. Typed out rather than imported so that a rename
// fails here loudly instead of renaming both sides at once.
const ROLES = [
  ['capital', 'Capital / storage / NPC'],
  ['troops_off', 'Troops off'],
  ['full_off', 'Full off (Hammer)'],
  ['def', 'DEF'],
  ['feeder', 'Feeder'],
]
const TARGETS = [
  ['lumber', 'Lumber'],
  ['clay', 'Clay'],
  ['iron', 'Iron'],
  ['crop', 'Crop'],
]
const SPENDS = TARGETS.slice(0, 3)

test.describe('every figure, on every role', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test.beforeEach(async ({ page }) => {
    await isolate(page, FIVE)
    await seed(page, { snapshot: FIVE, roles: ONE_PER_ROLE })
  })

  test('all 35 boxes are editable, and every figure reaches the plan request', async ({
    page,
  }) => {
    test.setTimeout(120_000)

    // The plan POST is READ and then ABORTED. Registered after `isolate`, so
    // it takes precedence over the catch-all; aborting keeps the fail-closed
    // rule intact -- nothing reaches the debug backend, let alone the game.
    const sent = []
    await page.route('**/distribution/plan', (route) => {
      sent.push(route.request().postDataJSON())
      return route.abort('blockedbyclient')
    })

    await openPanel(page)

    const expected = {}
    for (const [i, [role, label]] of ROLES.entries()) {
      expected[role] = { allocations: {}, consumption: {} }
      for (const [j, [resource, resLabel]] of TARGETS.entries()) {
        const value = (i + 1) * 1000 + j * 10
        await page.getByLabel(`${label} ${resLabel} mode`).selectOption('absolute')
        await page.getByLabel(`${label} ${resLabel} value`).fill(String(value))
        expected[role].allocations[resource] = { mode: 'absolute', value }
      }
      for (const [j, [resource, resLabel]] of SPENDS.entries()) {
        const value = (i + 1) * 100 + j
        await page.getByLabel(`${resLabel} spent per hour by a ${label} village`).fill(String(value))
        expected[role].consumption[resource] = value
      }
    }

    // Crop is offered as a target and never as a spend, counted rather than
    // read off a comment.
    await expect(page.getByLabel(/ Crop value$/)).toHaveCount(ROLES.length)
    await expect(page.getByLabel(/^Crop spent per hour/)).toHaveCount(0)

    expect(await stored(page)).toEqual(expected)
    await expect(page.getByText('5 typed, covering 5 village(s)')).toBeVisible()

    await page.getByRole('button', { name: 'Build plan (0 requests)' }).click()
    await expect.poll(() => sent.length).toBe(1)

    const roles = sent[0].roles
    expect(Object.keys(roles).sort()).toEqual(ROLES.map(([role]) => role).sort())
    for (const [role, body] of Object.entries(expected)) {
      expect(roles[role], `${role} did not arrive as typed`).toEqual({
        ...body,
        may_relay: null,
        crop_negative_by_design: false,
      })
    }
  })

  test('a value box beside "Keep own" is visibly not editable', async ({ page }) => {
    // Keep is the ABSENCE of a target, so the value box is disabled until a
    // mode is chosen -- and all twenty of them start that way. A disabled box
    // that looks identical to an editable one is item 5 of the UI Definition
    // of Done, and it is the shape of "change resources value": the operator
    // clicks the figure, types, and nothing happens.
    await openPanel(page)

    const box = page.getByLabel('Troops off Lumber value')
    await expect(box).toBeDisabled()
    const off = await box.evaluate((el) => {
      const s = getComputedStyle(el)
      return { bg: s.backgroundColor, color: s.color, edge: s.borderBottomStyle, cursor: s.cursor }
    })

    await page.getByLabel('Troops off Lumber mode').selectOption('absolute')
    await expect(box).toBeEnabled()
    const on = await box.evaluate((el) => {
      const s = getComputedStyle(el)
      return { bg: s.backgroundColor, color: s.color, edge: s.borderBottomStyle, cursor: s.cursor }
    })

    // Three cues, so it is not carried by colour alone (WCAG 1.4.1).
    expect(off.bg).not.toBe(on.bg)
    expect(off.color).not.toBe(on.color)
    expect(off.edge).toBe('dashed')
    expect(on.edge).toBe('solid')
    expect(off.cursor).toBe('not-allowed')
  })
})
