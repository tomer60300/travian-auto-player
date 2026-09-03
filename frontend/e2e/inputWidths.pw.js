/**
 * Every `.input-field` in the app's dense surfaces, MEASURED.
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
 * Definition of Done names, over the planner's three stages AND the three
 * pages off it that own dense controls -- the fix was app-wide, so the
 * measurement has to be:
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
 *   * a FREE-TEXT input is measured against the GREATER of its placeholder and
 *     its own value, the value capped at 32 characters. Both halves are load
 *     bearing. The placeholder is what must fit before the field is filled, or
 *     the field cannot even be identified; the value is what must fit after,
 *     and measuring the placeholder alone hid a 70px clip in this spec's own
 *     fixture (`Foreign target 1 name`, 144px for 214px of
 *     "Rheinbund-Aussenposten") while reporting zero clipped. The cap is why
 *     the value can be measured at all -- a name field's content has no bound,
 *     so demanding that a column fit ANY value is unsatisfiable rather than
 *     strict. It is a stop, not a target: every value seeded here is well
 *     inside it and so is asserted in full.
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

/**
 * Everything the shell asks for, and a hard stop for anything else.
 *
 * `extra` answers the calls one PAGE makes on top of the shell's two. It is a
 * function of the pathname rather than a table of URLs because these endpoints
 * carry query strings and ids (`/buildings?village_id=20002`,
 * `/farm/lists/1`), and it returns `undefined` for anything it does not
 * recognise so the abort below stays the default. Fail-closed is the whole
 * safety model here: there is a live Travian account on this machine.
 */
async function isolate(page, extra = () => undefined, socket = (ws) => ws.close()) {
  await page.routeWebSocket(/.*/, socket)
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
          villages: SNAPSHOT.villages.map((v) => ({
            id: v.village_id,
            name: v.name,
            x: v.x,
            y: v.y,
          })),
        },
      })
    }
    const body = extra(path)
    if (body !== undefined) return route.fulfill({ json: body })
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
  // How much of a free-text VALUE the field is required to fit. See the
  // text-input branch below for why there is a cap at all. Declared in here
  // rather than beside the other constants because this function is
  // serialised into the page and closes over nothing from Node.
  const TEXT_CAP_CHARS = 32

  const scroller = (el) => {
    for (let node = el.parentElement; node; node = node.parentElement) {
      const overflowX = getComputedStyle(node).overflowX
      if (overflowX === 'auto' || overflowX === 'scroll') return node
    }
    return null
  }

  // Which surface this control belongs to, named the way the operator would
  // name it, so a failure says WHERE.
  const surfaceOf = (el) => {
    const table = el.closest('table')
    if (table != null) {
      const heads = [...table.querySelectorAll('thead th')].map((th) => th.textContent.trim())
      // Lumber/h first: the Snapshot table has a Role column too, so testing
      // for "Role" ahead of it filed half the village table under Role
      // templates.
      if (heads.some((h) => h.startsWith('Lumber/h'))) return 'Snapshot table'
      if (heads.some((h) => h.startsWith('Role'))) return 'Role templates'
      if (heads.some((h) => h === 'X') && heads.some((h) => h === 'Y')) return 'Foreign targets'
      if (heads.some((h) => h.startsWith('Mode'))) return 'Allocate grid'
      return 'table: ' + heads.slice(0, 3).join('/')
    }
    // Off the planner there are no tables at these sites: the controls sit in
    // filter bars, card headers and queue rows. The nearest card's heading is
    // what the operator reads above them.
    const heading = el.closest('.card, section, form')?.querySelector('h2, h3, h4')
    if (heading != null) return heading.textContent.trim().slice(0, 24)
    return el.closest('label') ? 'World & merchants' : 'other'
  }

  // An accessible name if the markup has one, and something a human can act on
  // if it does not. `aria-label` alone reported half the non-planner controls
  // as "input"/"select", which is not a defect report.
  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label')
    if (aria) return aria
    const title = el.getAttribute('title')
    if (title) return title
    const by = el.getAttribute('aria-labelledby')
    if (by) {
      const text = document.getElementById(by)?.textContent?.trim()
      if (text) return text
    }
    const label = el.closest('label')?.textContent?.trim()
    if (label) return label.slice(0, 32)
    const prev = el.previousElementSibling?.textContent?.trim()
    if (prev) return prev.slice(0, 32)
    if (el.placeholder) return `placeholder "${el.placeholder}"`
    return el.tagName.toLowerCase()
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
      // The greater of the empty state's prompt and the value actually in the
      // box, the value capped at TEXT_CAP_CHARS characters.
      //
      // The placeholder ALONE is what this measured before, and it hid a real
      // clip in this spec's own fixture: `Foreign target 1 name` is `w-36`
      // (144px) and holds the seeded "Rheinbund-Aussenposten", which wants
      // 214px -- and the spec reported nothing. It is also vacuous for a
      // free-text input with no placeholder at all, where it reduced to "is
      // the box wider than its own padding".
      //
      // The cap is why a value can be measured at all: a name field's content
      // has no bound, so demanding a column fit ANY value is unsatisfiable
      // rather than strict. The cap is a stop, not a target -- every value
      // this fixture seeds is well inside it, so every one of them is
      // asserted in full.
      ctx.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
      const placeholder = ctx.measureText(el.placeholder ?? '').width
      const value = ctx.measureText((el.value ?? '').slice(0, TEXT_CAP_CHARS)).width
      needed =
        Math.max(placeholder, value) +
        parseFloat(style.paddingLeft) +
        parseFloat(style.paddingRight) +
        borders
      basis = value > placeholder ? 'value (text)' : 'placeholder'
    } else {
      needed = el.scrollWidth + borders
      basis = 'value'
    }
    const wrapper = scroller(el)
    out.push({
      surface: surfaceOf(el),
      label: labelOf(el),
      tag: el.tagName.toLowerCase(),
      basis,
      width: Math.round(rect.width * 10) / 10,
      needed: Math.round(needed * 10) / 10,
      // Item 4 of the UI Definition of Done. Not asserted -- these are not all
      // coarse-pointer viewports -- but reported, because the padding that
      // makes these boxes 44px tall is the same padding a fix here could
      // reach for, and a fix that trades a clipped glyph for an untappable
      // control is not a fix.
      height: Math.round(rect.height * 10) / 10,
      // The integer pair behind the `value` basis, kept so the assertion can
      // ask `scrollWidth > clientWidth` directly instead of comparing two
      // rounded floats. See `assertFits` -- this is the difference between
      // catching a 1px overflow and absorbing it.
      clientWidth: el.clientWidth,
      scrollWidth: el.scrollWidth,
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

/** Whether one control is narrower than the content it holds.
 *
 * Shared by the report and the assertion on purpose: a summary that counted
 * clips by a different rule than the one that fails the test is a summary that
 * lies about why the test passed. See `assertFits` for why the `value` basis
 * gets no tolerance. */
const isClipped = (c) =>
  c.basis === 'value' ? c.scrollWidth > c.clientWidth : c.width + 1 < c.needed

/** One line per surface: the narrowest control and how much it was short by.
 *
 * `globalThis.process` rather than `process`: eslint gives these files the
 * browser globals (they are `**\/*.js` under frontend/, and only
 * `*.config.js` is configured as Node), so the bare name is a `no-undef`
 * error even though the test body does run in Node. */
function report(where, measured) {
  const mode = globalThis.process?.env?.MEASURE
  if (!mode) return
  // MEASURE=all prints every control rather than the worst per surface. The
  // summary answers "did anything break"; this answers "by how much, where",
  // which is what choosing between two CSS fixes needs.
  if (mode === 'all') {
    const rows = measured.controls.map(
      (c) =>
        `    ${c.surface.padEnd(22)} ${c.tag.padEnd(6)} ${String(c.width).padStart(6)}px` +
        ` for ${String(c.needed).padStart(6)}px h${String(c.height).padStart(5)}` +
        ` ${isClipped(c) ? 'CLIPPED' : 'ok     '} ${c.basis.padEnd(12)} "${c.label}"`,
    )
    console.log(
      `  ${where}: page ${measured.pageClientWidth} client / ${measured.pageScrollWidth} scroll\n` +
        rows.join('\n'),
    )
    return
  }
  const bySurface = new Map()
  for (const c of measured.controls) {
    const worst = bySurface.get(c.surface)
    if (worst == null || c.needed - c.width > worst.needed - worst.width) bySurface.set(c.surface, c)
  }
  const lines = [...bySurface.entries()].map(([surface, c]) => {
    const n = measured.controls.filter((x) => x.surface === surface).length
    const clipped = measured.controls.filter((x) => x.surface === surface && isClipped(x)).length
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
 * The 1px tolerance applies to the CANVAS and CLONE bases only. Those are
 * float measurements of glyph runs, so a pixel of slack absorbs sub-pixel text
 * rounding and nothing else; the defect this spec was written for was 31px on
 * an 83px control, so nothing near the tolerance is interesting there.
 *
 * The `value` basis gets NO tolerance, because it needs none and because the
 * pixel it was given is exactly the pixel that hid seven controls. A number
 * input's `scrollWidth` and `clientWidth` are integers produced by the same
 * rounding rule, so `scrollWidth > clientWidth` is an exact statement about
 * whether the box overflows -- and asking it that way, rather than comparing
 * the two rounded floats, is what makes the 1px case decidable at all: a
 * 60.8px box reports client 61 / scroll 61 and is NOT clipped, while a 64px
 * box holding two digits reports client 64 / scroll 65 and is. Seven controls
 * sat at exactly the second: `Trade Office level`, `Most merchants busy at
 * once` and `NPC-backed stock floor` on both DEF villages, plus
 * `Merchant headroom` -- every one of them 64px for 65px, every one of them
 * passing.
 */
function assertFits(where, measured, viewport) {
  expect(measured.controls.length, `${where}: nothing measured`).toBeGreaterThan(0)

  const clipped = measured.controls
    .filter(isClipped)
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

// ── Off the planner ──────────────────────────────────────────────────
//
// `.input-field { width: 100% }` was moved into `@layer components` for the
// planner and shipped to the whole app: 66 of the 111 `.input-field` sites in
// `src/` carry a width utility, across twelve files, and every one of those
// utilities went from dead to live in one commit. The planner was the only
// surface measured.
//
// It was also the only surface where that was the whole story. `.input-field`
// still declares `padding: .75rem 1rem` (32px across) and `font-size: 1rem`
// UNLAYERED, so on the dense controls off the planner -- which ask for
// `py-0.5 px-1 text-xs` and got none of it -- a box that used to be 203px
// wide became the 48px its `w-12` asked for while its glyphs stayed 16px in
// 32px of padding. 14px left for the number.
//
// So the three pages that own those controls are measured here too, by the
// same rules. Each needs the calls its own page makes, and each needs the
// interaction that puts the control on screen: a queue row does not exist
// until a building is added to it, a farm filter bar does not exist until a
// list with slots is selected, and the scout's loop boxes do not exist until
// loop mode is ticked. A spec that only measured the default render would
// have measured none of the seven controls that broke.

/** Buildings enough to populate every category the queue groups by. */
const BUILDINGS = [
  { slot_id: 1, name: 'Woodcutter', level: 10 },
  { slot_id: 2, name: 'Clay Pit', level: 10 },
  { slot_id: 19, name: 'Barracks', level: 5 },
  { slot_id: 26, name: 'Main Building', level: 10 },
  { slot_id: 27, name: 'Warehouse', level: 18 },
]

function queueRoutes(path) {
  if (path.endsWith('/buildings/queue')) return []
  if (path.endsWith('/buildings')) return BUILDINGS
  return undefined
}

const FARM_LIST = {
  id: 1,
  name: 'Rheinbund raids',
  owner_village_name: '02',
  slots_amount: 2,
  active_slots: 2,
  total_booty: 41_860,
}

const FARM_SLOTS = [
  {
    id: 11,
    target_name: 'Rheinbund-Aussenposten',
    x: -117,
    y: 143,
    distance: 34.2,
    population: 512,
    is_active: true,
    resources: 3200,
    capacity: 3200,
  },
  {
    id: 12,
    target_name: 'Oase 47',
    x: -112,
    y: 139,
    distance: 29.8,
    population: 0,
    is_active: true,
    resources: 900,
    capacity: 3200,
  },
]

function farmRoutes(path) {
  if (path.endsWith('/farm/lists')) return [FARM_LIST]
  if (/\/farm\/lists\/\d+$/.test(path)) return { ...FARM_LIST, slots: FARM_SLOTS }
  return undefined
}

/** Signed in, with a village selected, and nothing planner-specific. */
async function seedShell(page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-not-a-real-token')
  })
}

async function openBuildQueue(page) {
  await page.goto('/queue')
  // A queue row is where `w-12` / `w-14` live, and a row needs an item. The
  // building name is the click target the operator uses.
  await page.getByText('Main Building', { exact: true }).click()
  await expect(page.getByTitle('Target level')).toBeVisible()
  // The bulk bar is a third site, revealed only by a selection.
  await page.locator('.checkbox-gold').first().check()
  await expect(page.getByText('1 selected')).toBeVisible()
}

async function openFarmFilters(page) {
  await page.goto('/farm')
  // The list ROW, not the loop-mode checkbox label that carries the same text.
  await page.getByRole('cell', { name: /^Rheinbund raids/ }).click()
  // The filter bar renders only once the selected list has slots.
  await expect(page.getByText('Max dist:')).toBeVisible()
  // Filters the operator has actually typed. An empty number box measures as
  // wide as its own padding and would pass a collapsed column.
  await page.locator('input[placeholder="any"]').first().fill('120')
  await page.locator('input[placeholder="any"]').nth(1).fill('500')
}

/**
 * The scan the scout page's loop controls sit behind.
 *
 * Auto-scout's loop boxes -- the two `w-20 py-1 px-2 text-xs` inputs the
 * regression clipped -- live in `AutoScoutPanel`, which renders only when
 * `scanResults.length > 0`, and `scanResults` is set from a WebSocket frame.
 * So this spec plays the server for `/ws/scout/scan` and answers with the
 * three frames the page needs: a session, the results, and the end. Nothing
 * leaves the browser -- Playwright terminates the socket, exactly as
 * `route.abort` terminates the HTTP calls -- and the frames are the page's
 * own contract, not the game's.
 *
 * Every other socket still closes immediately.
 */
const SCAN_TILES = [
  { x: -117, y: 143, name: 'Rheinbund-Aussenposten', population: 512, player_name: 'Bergvolk' },
  { x: -112, y: 139, name: 'Oase 47', population: 0, player_name: '' },
]

function scanSocket(ws) {
  if (!ws.url().includes('/ws/scout/scan')) return ws.close()
  ws.onMessage(() => {
    ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-scan' }))
    ws.send(JSON.stringify({ type: 'complete', tiles: SCAN_TILES, stats: { time_seconds: 1 } }))
    ws.send(JSON.stringify({ type: 'operation_complete', status: 'completed' }))
  })
}

async function openScoutLoop(page) {
  await page.goto('/scout')
  await page.getByRole('button', { name: 'Scan Map' }).click()
  await expect(page.getByText('Loop mode')).toBeVisible()
  await page.getByLabel('Loop mode').check()
  // `getByText`, not `getByLabel`: the "Interval (s):" label carries no
  // `htmlFor` and does not wrap its input, so it names nothing.
  await expect(page.getByText('Interval (s):')).toBeVisible()
}

const SURFACES = [
  // The Snapshot stage carries three of the five at once: the village table,
  // the foreign-targets table under it, and the World & merchants bar.
  {
    name: 'Snapshot stage (village table + foreign targets + World & merchants)',
    open: openSnapshot,
  },
  { name: 'Role templates panel', open: openRoleTemplates },
  { name: 'Allocate grid (Edit by resource)', open: openAllocateGrid },
  // Off the planner. `seed` is the planner's fixture, so these take the plain
  // shell instead and answer their own page's calls.
  {
    name: 'Build queue (queue row + bulk bar)',
    open: openBuildQueue,
    routes: queueRoutes,
    seed: seedShell,
  },
  {
    name: 'Farm lists (filter bar + transfer bar)',
    open: openFarmFilters,
    routes: farmRoutes,
    seed: seedShell,
  },
  { name: 'Auto-scout (loop mode)', open: openScoutLoop, seed: seedShell, socket: scanSocket },
]

for (const viewport of VIEWPORTS) {
  test.describe(`every .input-field fits its content at ${viewport.width}px`, () => {
    test.use({ viewport })

    for (const surface of SURFACES) {
      test(surface.name, async ({ page }) => {
        await isolate(page, surface.routes, surface.socket)
        await (surface.seed ?? seed)(page)
        await surface.open(page)
        const measured = await page.evaluate(MEASURE)
        report(`${viewport.width}px ${surface.name}`, measured)
        assertFits(`${viewport.width}px ${surface.name}`, measured, viewport)
      })
    }
  })
}
