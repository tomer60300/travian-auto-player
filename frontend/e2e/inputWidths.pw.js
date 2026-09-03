/**
 * Every `.input-field` in the planner, MEASURED.
 *
 * `index.css` starts with `@import "tailwindcss"`, so Tailwind's utilities live
 * in `@layer utilities` while the hand-written component classes below are
 * UNLAYERED -- and unlayered normal declarations beat layered ones whatever the
 * source order. `.input-field { width: 100% }` therefore beat `w-20` / `w-24` /
 * `w-16` on every control in the app that asked for a width, and a percentage
 * width contributes almost nothing to a table's intrinsic size: the columns
 * collapsed to the header text, the controls collapsed with them, and the
 * `overflow-x-auto` wrapper never overflowed so it never scrolled either.
 * Measured at 1440 before the fix, in the Role-templates panel: mode selects
 * 51.7px carrying 83px of options, spend boxes 51.7px reading "non", wrapper
 * client 1122 == scroll 1122.
 *
 * So this spec asserts the thing that was wrong, at the three viewports the UI
 * Definition of Done names, over every surface in the planner that puts an
 * `.input-field` in a table:
 *
 *   1. no control is narrower than its own content;
 *   2. a wrapper whose content overflows can actually be scrolled;
 *   3. no scrolling container is itself wider than the viewport.
 *
 * It measures rather than screenshots deliberately. A baseline PNG of a
 * collapsed table is a baseline of the defect, and it would have been accepted
 * as "no diff" for as long as the defect stood. A width against its own content
 * is a claim that cannot be baselined wrong.
 *
 * Reading a control's content width takes two methods, because the DOM offers
 * no single one:
 *
 *   * a `<select>` never overflows -- it clips its label instead -- so it is
 *     cloned with `width: auto` and the clone's natural width is what it
 *     needed. The clone keeps its className, so `text-xs` and `.input-field`
 *     still apply to it.
 *   * a NUMBER input scrolls its text, so `scrollWidth` against `clientWidth`
 *     is the direct question and needs no font arithmetic. A figure is the one
 *     kind of content where scrolling is not an acceptable answer: a spend box
 *     showing "837" of "8372" is not truncated, it is WRONG, and nothing on
 *     screen says so.
 *   * a FREE-TEXT input is measured against its PLACEHOLDER instead, because
 *     its content has no bound -- an ally's village name can be longer than any
 *     column, and scrolling it is what every text field on the web does. What
 *     must fit is the empty state's prompt, or the field cannot even be
 *     identified before it is filled.
 *
 * NO BACKEND AND NO GAME REQUEST, by the two fail-closed mechanisms
 * `roleTemplates.pw.js` documents: `page.route('** /api/**')` answers the two
 * calls the shell makes and ABORTS everything else, and the snapshot is seeded
 * into localStorage rather than fetched.
 *
 * Running it:
 *   cd frontend
 *   npx playwright install chromium   # once per machine
 *   npx playwright test inputWidths
 *   MEASURE=1 npx playwright test inputWidths   # print the numbers table
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 20002
const DEF_A = 20011
const DEF_B = 20013

// The three widths the UI Definition of Done names.
const VIEWPORTS = [
  { width: 375, height: 900 },
  { width: 768, height: 1000 },
  { width: 1440, height: 1200 },
]

function village(id, name, x, y) {
  return {
    village_id: id,
    name,
    x,
    y,
    merchants_total: 20,
    merchants_free: 20,
    lumber_per_hour: 8372,
    clay_per_hour: 5168,
    iron_per_hour: 5809,
    crop_per_hour: 2200,
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
    village(DEF_A, '11', 4, 0),
    village(DEF_B, '13', 0, 4),
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
    return route.abort('blockedbyclient')
  })
}

/**
 * An account with every measured box FILLED.
 *
 * Five-figure numbers throughout, and they are the point rather than
 * decoration: a control is only measurably too narrow for content it actually
 * holds, and an empty box would let a collapsed column pass. The figures are
 * profile section 2's defensive spend, which is what these boxes hold on the
 * real account.
 */
async function seed(page) {
  await page.addInitScript(
    ([key, snapshot, defA, defB]) => {
      const set = (name, value) => localStorage.setItem(`${name}::${key}`, JSON.stringify(value))
      localStorage.setItem('token', 'e2e-not-a-real-token')
      set('planner_snapshot', snapshot)
      set('planner_snapshot_at', Date.now())
      set('planner_village_roles', { [defA]: 'def', [defB]: 'def' })
      set('planner_role_templates', {
        def: {
          allocations: {
            lumber: { mode: 'absolute', value: 8372 },
            clay: { mode: 'percentage', value: 12 },
          },
          consumption: { lumber: 8372, clay: 5168, iron: 5809 },
          may_relay: false,
          crop_negative_by_design: true,
        },
      })
      set('planner_trade_office', { [defA]: 19, [defB]: 15 })
      set('planner_max_busy', { [defA]: 18, [defB]: 12 })
      set('planner_crop_ceiling', { [defA]: 380_000, [defB]: 250_000 })
      // A FRACTION of the warehouse, shown as a percent -- 0.35 renders "35".
      // Seeding 35 would render "3500" and measure a box against a figure the
      // input's own max of 95 forbids.
      set('planner_stock_floor', { [defA]: 0.35, [defB]: 0.95 })
      set('planner_consumption', {
        [defA]: { lumber: 8372, clay: 5168, iron: 5809 },
        [defB]: { lumber: 8372, clay: 5168, iron: 5809 },
      })
      set('planner_may_relay', { [defA]: true })
      set('planner_foreign_targets', [
        {
          name: 'Rheinbund-Aussenposten',
          x: -117,
          y: 143,
          crop_per_hour: 12_500,
          safety_margin_pct: 5,
          route_eligible: true,
        },
      ])
      set('planner_allocations_v2', null)
    },
    [KEY, SNAPSHOT, DEF_A, DEF_B],
  )
}

/**
 * Width against content for every visible `.input-field`, grouped by surface.
 *
 * Runs in the page because it needs layout: a Playwright locator can report a
 * bounding box but not what the box needed to be.
 */
const MEASURE = () => {
  const scroller = (el) => {
    for (let node = el.parentElement; node; node = node.parentElement) {
      const overflowX = getComputedStyle(node).overflowX
      if (overflowX === 'auto' || overflowX === 'scroll') return node
    }
    return null
  }

  // Which of the planner's surfaces this control belongs to, named the way the
  // operator would name it, so a failure says WHERE.
  const surfaceOf = (el) => {
    const table = el.closest('table')
    if (table == null) return el.closest('label') ? 'World & merchants' : 'other'
    const heads = [...table.querySelectorAll('thead th')].map((th) => th.textContent.trim())
    // Lumber/h first: the Snapshot table has a Role column too, so testing for
    // "Role" ahead of it filed half the village table under Role templates.
    if (heads.some((h) => h.startsWith('Lumber/h'))) return 'Snapshot table'
    if (heads.some((h) => h.startsWith('Role'))) return 'Role templates'
    if (heads.some((h) => h === 'X') && heads.some((h) => h === 'Y')) return 'Foreign targets'
    if (heads.some((h) => h.startsWith('Mode'))) return 'Allocate grid'
    return 'table: ' + heads.slice(0, 3).join('/')
  }

  const ctx = document.createElement('canvas').getContext('2d')
  const out = []
  for (const el of document.querySelectorAll('.input-field')) {
    const rect = el.getBoundingClientRect()
    if (rect.width === 0 && rect.height === 0) continue
    const style = getComputedStyle(el)
    const borders = parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth)
    let needed
    let basis
    if (el.tagName === 'SELECT') {
      // A select clips rather than scrolls, so ask a clone what it wanted.
      const clone = el.cloneNode(true)
      clone.style.cssText = 'width:auto;position:absolute;left:-9999px;top:0'
      document.body.appendChild(clone)
      needed = clone.getBoundingClientRect().width
      clone.remove()
      basis = 'options'
    } else if (el.tagName === 'INPUT' && el.type !== 'number') {
      ctx.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
      needed =
        ctx.measureText(el.placeholder ?? '').width +
        parseFloat(style.paddingLeft) +
        parseFloat(style.paddingRight) +
        borders
      basis = 'placeholder'
    } else {
      needed = el.scrollWidth + borders
      basis = 'value'
    }
    const wrapper = scroller(el)
    out.push({
      surface: surfaceOf(el),
      label: el.getAttribute('aria-label') ?? el.tagName.toLowerCase(),
      tag: el.tagName.toLowerCase(),
      basis,
      width: Math.round(rect.width * 10) / 10,
      needed: Math.round(needed * 10) / 10,
      wrapperClient: wrapper ? wrapper.clientWidth : null,
      wrapperScroll: wrapper ? wrapper.scrollWidth : null,
      wrapperScrollable: wrapper ? wrapper.scrollWidth > wrapper.clientWidth : null,
      // Where the scrolling container's own box ENDS. A table may be wider than
      // the viewport; its container may not.
      wrapperRight: wrapper ? Math.round(wrapper.getBoundingClientRect().right) : null,
    })
  }
  return {
    controls: out,
    pageScrollWidth: document.documentElement.scrollWidth,
    pageClientWidth: document.documentElement.clientWidth,
  }
}

/** One line per surface: the narrowest control and how much it was short by.
 *
 * `globalThis.process` rather than `process`: eslint gives these files the
 * browser globals (they are `**\/*.js` under frontend/, and only
 * `*.config.js` is configured as Node), so the bare name is a `no-undef`
 * error even though the test body does run in Node. */
function report(where, measured) {
  if (!globalThis.process?.env?.MEASURE) return
  const bySurface = new Map()
  for (const c of measured.controls) {
    const worst = bySurface.get(c.surface)
    if (worst == null || c.needed - c.width > worst.needed - worst.width) bySurface.set(c.surface, c)
  }
  const lines = [...bySurface.entries()].map(([surface, c]) => {
    const n = measured.controls.filter((x) => x.surface === surface).length
    const clipped = measured.controls.filter((x) => x.surface === surface && x.width + 1 < x.needed).length
    return (
      `    ${surface.padEnd(20)} ${String(n).padStart(3)} controls, ${String(clipped).padStart(3)} clipped` +
      ` | worst ${c.tag} "${c.label}" ${c.width}px for ${c.needed}px` +
      ` | wrapper ${c.wrapperClient}/${c.wrapperScroll}`
    )
  })
  console.log(
    `  ${where}: page ${measured.pageClientWidth} client / ${measured.pageScrollWidth} scroll\n` +
      lines.join('\n'),
  )
}

/**
 * The three claims, over whatever is on screen.
 *
 * A tolerance of 1px absorbs sub-pixel text rounding only; the defect this
 * guards was 31px on an 83px control, so nothing near the tolerance is
 * interesting.
 */
function assertFits(where, measured, viewport) {
  expect(measured.controls.length, `${where}: nothing measured`).toBeGreaterThan(0)

  const clipped = measured.controls
    .filter((c) => c.width + 1 < c.needed)
    .map((c) => `${c.surface} / ${c.tag} "${c.label}": ${c.width}px for ${c.needed}px of ${c.basis}`)
  expect(clipped, `${where}: controls narrower than their content`).toEqual([])

  // A wrapper that overflows must be reachable. `overflow-x: auto` makes that
  // true by construction, so this catches the other half: a wrapper whose
  // content was squeezed to fit reports no overflow at all, which is exactly
  // how a table full of collapsed controls hid.
  const stuck = measured.controls
    .filter((c) => c.wrapperClient != null && c.wrapperScroll > c.wrapperClient && !c.wrapperScrollable)
    .map((c) => `${c.surface}: ${c.wrapperClient}/${c.wrapperScroll}`)
  expect(stuck, `${where}: wrapper overflows but cannot scroll`).toEqual([])

  // Item 1 of the UI Definition of Done: a wide table scrolls inside its own
  // container, never the body. Giving the controls their widths back makes every
  // one of these tables wider, so this is the guard on the FIX as much as on the
  // defect -- a column that grew past its container would have traded a
  // collapsed control for a page that scrolls sideways.
  //
  // Asserted on the container's own right edge, and NOT on
  // `document.documentElement.scrollWidth`, which this app inflates whatever the
  // widths are: measured on the Snapshot stage before any of this changed, the
  // document reported 874px of scroll at a 375px viewport while `document.body`
  // reported 375 and not one overflowing element lacked a clipping ancestor.
  // (Chrome counts layout overflow from inside a nested scroller and from the
  // decorative `.md3-blur-shape` divs, which are clipped by their own
  // `overflow-hidden` parent.) A number that reads 874 when nothing is wrong
  // cannot be the guard on whether something is wrong.
  const escaped = measured.controls
    .filter((c) => c.wrapperRight != null && c.wrapperRight > viewport.width + 1)
    .map((c) => `${c.surface}: container ends at ${c.wrapperRight} in a ${viewport.width} viewport`)
  expect(escaped, `${where}: a scroll container is wider than the viewport`).toEqual([])
}

async function openSnapshot(page) {
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Trade Office level for 11')).toBeVisible()
}

async function openRoleTemplates(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Allocate' }).click()
  await page.getByText('Role templates', { exact: true }).click()
  await expect(page.getByLabel('DEF Lumber value')).toBeVisible()
}

async function openAllocateGrid(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Allocate' }).click()
  await page.getByRole('button', { name: 'Edit by resource' }).click()
  await expect(page.getByLabel('Lumber mode for 11')).toBeVisible()
}

const SURFACES = [
  // The Snapshot stage carries three of the five at once: the village table,
  // the foreign-targets table under it, and the World & merchants bar.
  ['Snapshot stage (village table + foreign targets + World & merchants)', openSnapshot],
  ['Role templates panel', openRoleTemplates],
  ['Allocate grid (Edit by resource)', openAllocateGrid],
]

for (const viewport of VIEWPORTS) {
  test.describe(`every .input-field fits its content at ${viewport.width}px`, () => {
    test.use({ viewport })

    test.beforeEach(async ({ page }) => {
      await isolate(page)
      await seed(page)
    })

    for (const [name, open] of SURFACES) {
      test(name, async ({ page }) => {
        await open(page)
        const measured = await page.evaluate(MEASURE)
        report(`${viewport.width}px ${name}`, measured)
        assertFits(`${viewport.width}px ${name}`, measured, viewport)
      })
    }
  })
}
