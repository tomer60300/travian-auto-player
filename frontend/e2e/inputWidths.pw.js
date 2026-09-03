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
 *     the first `TEXT_CAP_CHARS` characters of its own value. Both halves are
 *     load bearing. The placeholder is what must fit before the field is
 *     filled, or the field cannot even be identified -- and it is vacuous for
 *     the two `Day window` boxes, which have none, where it reduced to "is the
 *     box wider than its own padding". The value is what must be READABLE
 *     after, and measuring the placeholder alone reported zero clipped on a
 *     box that showed nine characters of a twenty-two character ally name.
 *
 *     The cap is the whole of what makes that second half a rule rather than a
 *     wish, and it was set to 32 characters, which NOTHING can satisfy: 32
 *     characters of an ally name is 296.1px of glyphs, so the box would have
 *     to be 328.1px inside a cell of 345px, inside a pinned column whose whole
 *     visible strip at 375px is 293px. Measured, by seeding exactly that name:
 *     224px for 296.1px, red at all three viewports, with no code defect and
 *     no reachable width. A rule that cannot be satisfied is worse than no
 *     rule -- and it had already done damage, because the 214px "clip" it
 *     reported for a 144px name box is what talked round 8 into `w-56` and 82%
 *     of the strip.
 *
 *     So the cap is what a column can actually afford. 10 characters, and the
 *     arithmetic is the app's, not a guess: `.input-field` spends 32px on
 *     padding and declares no left/right border, and its `font-size: 1rem`
 *     beats `text-xs` unlayered (below 640px the mobile `16px !important` rule
 *     pins it there anyway), so a box of width W has W-32 px for 16px Roboto.
 *     The narrowest free-text box in the app is the foreign-target `Not from`
 *     field at `w-28`: 80px, which holds its "02, 11, 13" at 68.2px and ten
 *     characters of a name-shaped run exactly. The pinned name box at `w-36`
 *     has 112px, room for thirteen. Only a PINNED column is bounded by the
 *     visible strip; a scrolling one can be widened a step whenever a real
 *     value outgrows it, which is why 10 is a number the design can meet and
 *     32 is not.
 *
 *     Read it as a floor on readability rather than a ceiling on the field: a
 *     free-text box must show at least ten characters of what is in it, and
 *     the rest may scroll. A NAME that scrolls is still legible -- the caret
 *     and the arrow keys reach the rest of it, and the operator typed it. That
 *     is the difference from a FIGURE, and it is why the fixture now seeds a
 *     32-character name on purpose: the spec's verdict no longer depends on
 *     how long the seeded name happens to be.
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
      // 32 characters, on purpose and past every cap: it is longer than any
      // box in the app can show, so it proves the free-text rule holds
      // independently of how long the seeded name happens to be. The rule
      // used to be calibrated to a 22-character seed and went red the moment
      // this string replaced it -- see the header comment on TEXT_CAP_CHARS.
      set('planner_foreign_targets', [
        {
          name: 'Rheinbund-Aussenposten-Nordwest3',
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
  // How much of a free-text VALUE the field is required to show. Ten, because
  // that is what the app's narrowest free-text box (`Not from`, `w-28`, 80px
  // of glyphs after `.input-field`'s 32px of padding) can actually hold; the
  // header comment has the arithmetic and the measurement that killed 32.
  // Declared in here rather than beside the other constants because this
  // function is serialised into the page and closes over nothing from Node.
  const TEXT_CAP_CHARS = 10

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
      // The greater of the empty state's prompt and the first TEXT_CAP_CHARS
      // characters of the value actually in the box.
      //
      // The placeholder ALONE is what this measured before, and it is vacuous
      // for a free-text input that has none -- both `Day window` boxes --
      // where it reduced to "is the box wider than its own padding".
      //
      // The cap is what makes the value half satisfiable: a name has no
      // bound, so a column asked to fit ANY value is asked for a width that
      // does not exist. 32 was exactly that (296.1px of glyphs in a 293px
      // strip, measured); 10 is what the narrowest free-text column already
      // affords. Read it as "must show ten characters of what is in it" --
      // the rest of a NAME may scroll, because the caret reaches it and it is
      // still legible. A FIGURE may not, which is the branch below.
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
      // Where the scrolling container's own box ENDS. A table may be wider than
      // the viewport; its container may not.
      wrapperRight: wrapper ? Math.round(wrapper.getBoundingClientRect().right) : null,
    })
  }
  // Second pass, after every control's geometry is read: can a wrapper that
  // overflows actually be scrolled? Asked by MOVING it and reading back where
  // it landed. The check that used to stand here re-derived
  // `scrollWidth > clientWidth` and then required its negation, so its filter
  // was `X && !X` and it could not fail for any wrapper on any page.
  //
  // Scrolled last and restored immediately: `scrollLeft` shifts every
  // `getBoundingClientRect` inside the wrapper, so probing it mid-measurement
  // would have moved the boxes being measured.
  const wrappers = []
  const seen = new Set()
  for (const el of document.querySelectorAll('.input-field')) {
    const w = scroller(el)
    if (w == null || seen.has(w)) continue
    seen.add(w)
    const before = w.scrollLeft
    w.scrollLeft = w.scrollWidth
    const reached = w.scrollLeft
    w.scrollLeft = before
    wrappers.push({
      surface: surfaceOf(el),
      clientWidth: w.clientWidth,
      scrollWidth: w.scrollWidth,
      overflow: w.scrollWidth - w.clientWidth,
      reached,
    })
  }

  // Third pass: what a PINNED column costs the strip beside it.
  //
  // `.sticky-col` holds the identity column still while the rest of the table
  // scrolls under it, so its width is taken off the visible strip PERMANENTLY
  // -- every scrolling column has to fit in what is left, one at a time. Round
  // 8 widened the foreign-target name box from `w-36` to `w-56` and took the
  // pinned column from 161px to 241px of a 293px strip at 375: 82%, leaving a
  // 52px window for nine columns of 96/96/112/96/96/128/63/60/56px. Not one of
  // them fitted. At 161px a 96px column did.
  //
  // Read off the header cells rather than the controls, because the cost is
  // per COLUMN and a column can be wider than the control in it. Gated on the
  // computed `position`, not on the class: it is the pinning that spends the
  // strip, and `.sticky-col` is inert wherever the table fits.
  const pinned = []
  for (const th of document.querySelectorAll('thead th.sticky-col')) {
    if (getComputedStyle(th).position !== 'sticky') continue
    const table = th.closest('table')
    const wrapper = scroller(table)
    if (wrapper == null) continue
    const others = [...table.querySelectorAll('thead th')]
      .filter((h) => h !== th)
      .map((h) => h.getBoundingClientRect().width)
      .filter((w) => w > 0)
    if (others.length === 0) continue
    const width = th.getBoundingClientRect().width
    pinned.push({
      surface: surfaceOf(th),
      width: Math.round(width * 10) / 10,
      strip: wrapper.clientWidth,
      left: Math.round((wrapper.clientWidth - width) * 10) / 10,
      narrowest: Math.round(Math.min(...others) * 10) / 10,
    })
  }

  // And the page itself: does the BODY slide sideways? Same method as the
  // wrappers -- ask by moving it -- because the two questions have different
  // answers and only one of them is a defect. A container that scrolls is the
  // design; a document that scrolls is item 1 of the UI Definition of Done.
  const pageBefore = document.scrollingElement.scrollLeft
  document.scrollingElement.scrollLeft = document.documentElement.scrollWidth
  const pageReached = document.scrollingElement.scrollLeft
  document.scrollingElement.scrollLeft = pageBefore

  return {
    controls: out,
    wrappers,
    pinned,
    pageScrollWidth: document.documentElement.scrollWidth,
    pageClientWidth: document.documentElement.clientWidth,
    pageScrollReached: pageReached,
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
/** What each pinned column costs the strip, or that nothing is pinned here.
 *
 * Printed in BOTH report modes, and printed even when the list is EMPTY: the
 * `crowded` clause in `assertFits` says nothing at a viewport where no column
 * is pinned, and a clause that can be vacuous has to say so out loud. This
 * file already carried one check that could not fail for any wrapper on any
 * page (`X && !X`) and it stood for weeks. */
function pinnedRows(measured) {
  if (measured.pinned.length === 0) return ['    pinned: none at this viewport']
  return measured.pinned.map(
    (p) =>
      `    pinned ${p.surface.padEnd(16)} ${p.width}px of a ${p.strip}px strip` +
      ` -> ${p.left}px left, narrowest scrolling column ${p.narrowest}px` +
      ` ${p.left < p.narrowest ? 'CROWDED' : 'ok'}`,
  )
}

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
        rows.concat(pinnedRows(measured)).join('\n'),
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
      lines.concat(pinnedRows(measured)).join('\n'),
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

  // A wrapper that overflows must be reachable -- and this asks by MOVING it.
  // The version this replaces filtered on `overflows && !overflows`, which is
  // false for every wrapper that has ever existed, so the regression its
  // comment claimed to catch would have passed in silence. Comparing against
  // where the scroll actually landed is the affirmative form of the same
  // claim, and it is the one that can fail: a wrapper whose content is clipped
  // by an ancestor, or pinned by a `position: sticky` child that does not
  // participate in its scroll width, reports overflow it cannot deliver.
  const stuck = measured.wrappers
    .filter((w) => w.overflow > 0 && w.reached < w.overflow - 1)
    .map((w) => `${w.surface}: ${w.clientWidth}/${w.scrollWidth} scrolled only to ${w.reached}`)
  expect(stuck, `${where}: wrapper overflows but cannot scroll`).toEqual([])

  // Item 1 of the UI Definition of Done: a wide table scrolls inside its own
  // container, never the body. Giving the controls their widths back makes every
  // one of these tables wider, so this is the guard on the FIX as much as on the
  // defect -- a column that grew past its container would have traded a
  // collapsed control for a page that scrolls sideways.
  const escaped = measured.controls
    .filter((c) => c.wrapperRight != null && c.wrapperRight > viewport.width + 1)
    .map((c) => `${c.surface}: container ends at ${c.wrapperRight} in a ${viewport.width} viewport`)
  expect(escaped, `${where}: a scroll container is wider than the viewport`).toEqual([])

  // A pinned column must leave a whole scrolling column beside it. Pinning
  // exists so that a figure being typed is attributable to the right row, and
  // a pinned column that swallows the strip defeats itself twice over: there
  // is nothing left to read beside the identity, and the operator scrolls a
  // column at a time through a window narrower than any column. Round 8's
  // `w-36` -> `w-56` on the foreign-target name box did exactly that, 241px of
  // a 293px strip, 52px left, narrowest column 55.7px -- and this is the
  // affirmative form of that arithmetic. The clause is scoped to columns whose
  // computed position really is `sticky`, so it says nothing at a viewport
  // where nothing is pinned; the report prints the count so a vacuous pass is
  // visible rather than silent.
  const crowded = measured.pinned
    .filter((p) => p.left < p.narrowest)
    .map(
      (p) =>
        `${p.surface}: pinned column ${p.width}px of a ${p.strip}px strip leaves ${p.left}px,` +
        ` and its narrowest scrolling column is ${p.narrowest}px`,
    )
  expect(crowded, `${where}: a pinned column leaves no room for any scrolling column`).toEqual([])

  // The same item, asked of the DOCUMENT. This used to be printed and not
  // asserted, on the stated grounds that the app "inflates it whatever the
  // widths are" and that no overflowing element lacked a clipping ancestor --
  // and that was WRONG on both counts. The number was reporting a real escape
  // the whole time: six `position: absolute` `.sr-only` spans inside the
  // Snapshot table sat at x=1261 in a 375 viewport, and an `overflow-x: auto`
  // ancestor does not clip an absolutely positioned box it is not the
  // containing block for. The document really scrolled -- `scrollLeft` reached
  // 887 of 1262 -- and the page really slid off, leaving the fixed bars over a
  // blank field.
  //
  // So both halves are asserted, because they say different things: the width
  // says a scrollable region exists, and `scrollLeft` says a reader can
  // actually be taken there. `scrollLeft` is the one that cannot be argued
  // with, which is why it is the one `login.visual.pw.js` reaches for too.
  expect(
    measured.pageScrollWidth,
    `${where}: the document overflows by ${measured.pageScrollWidth - measured.pageClientWidth}px`,
  ).toBeLessThanOrEqual(measured.pageClientWidth)
  expect(
    measured.pageScrollReached,
    `${where}: the page slid sideways to ${measured.pageScrollReached}px -- only its containers may scroll`,
  ).toBe(0)
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
    target_name: 'Rheinbund-Aussenposten-Nordwest3',
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
  // The filter bar renders only once the selected list has slots. Reached BY
  // LABEL, and both boxes are filled the same way: `getByText` and
  // `input[placeholder="any"]` are what this used before, and both were
  // concessions to markup that named nothing -- the visible "Max dist:" and
  // "Min pop:" were bare `<span>`s, so `getByLabel` resolved 0 elements and
  // the second filter had to be picked out by ordinal among identical
  // placeholders. Naming the inputs is WCAG 4.1.2; reaching them by that name
  // here is what stops the concession coming back.
  await expect(page.getByLabel('Max dist')).toBeVisible()
  // Filters the operator has actually typed. An empty number box measures as
  // wide as its own padding and would pass a collapsed column.
  await page.getByLabel('Max dist').fill('120')
  await page.getByLabel('Min pop').fill('500')
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
  { x: -117, y: 143, name: 'Rheinbund-Aussenposten-Nordwest3', population: 512, player_name: 'Bergvolk' },
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
  // BY LABEL. This used `getByText`, because the "Interval (s):" and
  // "Duration (min):" `<label>`s carry no `htmlFor` and do not wrap their
  // input, so they named nothing and `getByLabel` resolved 0 elements. The
  // inputs carry `aria-label` now (WCAG 4.1.2) and this asks for them that
  // way, so the concession cannot quietly return.
  await expect(page.getByLabel('Interval (s)')).toBeVisible()
  await expect(page.getByLabel('Duration (min)')).toBeVisible()
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

/**
 * The two DISCLOSURE pickers in the village table, measured the same way.
 *
 * `Ships only to` and `Relays for` are not `.input-field`s -- they are a
 * `<summary>` over a checkbox list -- so the sweep above has never looked at
 * them, and profile section 5's relay tier added the second one. A summary is
 * exactly as clippable as a number box and in the same way: `whitespace-nowrap`
 * plus a column narrower than the text is a name the operator cannot read, with
 * nothing on screen saying it was cut.
 *
 * Measured rather than screenshotted, and measured OPEN as well as closed, for
 * the same reason the sweep is: a baseline of a clipped summary is a baseline of
 * the defect. Open matters because the panel is where the village names live --
 * a closed summary reads "not a relay" and fits anything, while the open list is
 * 26 rows of names inside a `max-h-40 overflow-y-auto`.
 *
 * Kept as its own test rather than folded into MEASURE's selector so the
 * eighteen cases above keep saying exactly what they say now, and so a failure
 * here names the picker rather than arriving as one more line in a sweep.
 */
const PICKERS = [
  // `group` is the panel's own accessible name, which is how the checkboxes are
  // reached: the panel carries no visible heading, so a text filter finds
  // nothing and the ticking silently does not happen -- measured, and it left
  // every summary reading its own placeholder while the spec claimed to be
  // measuring names.
  {
    column: 'Ships only to',
    group: /may ship to$/,
    // What the summary must read once two villages are ticked. Asserted, so a
    // picker whose panel moves cannot leave this measuring the resting state.
    filled: /^Ships only to, for 02: \S+, \S+$/,
  },
  {
    column: 'Relays for',
    group: /forwards material to$/,
    filled: /^Relays for, for 02: \S+, \S+$/,
  },
]

for (const viewport of VIEWPORTS) {
  test.describe(`the village table's pickers fit their content at ${viewport.width}px`, () => {
    test.use({ viewport })

    for (const picker of PICKERS) {
      test(picker.column, async ({ page }) => {
        await isolate(page)
        await seed(page)
        await openSnapshot(page)

        // Every one of them, on every village row -- the defect this guards is
        // per column, but the content differs per row and only one row needs to
        // be too long for the operator to lose a name.
        const summaries = page.locator('summary').filter({ hasText: picker.column })
        const rows = await summaries.count()
        expect(rows, `${picker.column}: no picker found in the village table`).toBeGreaterThan(0)

        // Ticked on the first row, so the summary is measured carrying real
        // village names rather than its own resting placeholder. An unfilled
        // control measures as wide as its padding and would pass a collapsed
        // column -- the same reason this spec seeds values into the number
        // boxes above. And the result is ASSERTED: the first version of this
        // reached the panel by its text, found nothing, ticked nothing and
        // measured three summaries reading "not a relay" while claiming to
        // measure names.
        await summaries.first().click()
        const ticks = page.getByRole('group', { name: picker.group }).first().getByRole('checkbox')
        await expect(ticks.first()).toBeVisible()
        await ticks.nth(0).check()
        await ticks.nth(1).check()
        await expect(summaries.first()).toHaveText(picker.filled)

        const measured = await page.evaluate(() => {
          const out = []
          for (const el of document.querySelectorAll('summary')) {
            const rect = el.getBoundingClientRect()
            if (rect.width === 0 && rect.height === 0) continue
            let wrapper = null
            for (let node = el.parentElement; node; node = node.parentElement) {
              const overflowX = getComputedStyle(node).overflowX
              if (overflowX === 'auto' || overflowX === 'scroll') {
                wrapper = node
                break
              }
            }
            out.push({
              text: el.textContent.trim().slice(0, 48),
              clientWidth: el.clientWidth,
              scrollWidth: el.scrollWidth,
              width: Math.round(rect.width * 10) / 10,
              height: Math.round(rect.height * 10) / 10,
              wrapperRight: wrapper ? wrapper.getBoundingClientRect().right : null,
            })
          }
          return {
            summaries: out,
            pageClientWidth: document.documentElement.clientWidth,
            pageScrollWidth: document.documentElement.scrollWidth,
          }
        })

        const mine = measured.summaries.filter((sum) => sum.text.startsWith(picker.column))
        expect(mine.length, `${picker.column}: nothing measured`).toBeGreaterThan(0)
        // `globalThis.process`, as the sweep's own reporter does it: `process`
        // is not a declared global in this config, so the bare form is a lint
        // ERROR rather than a warning and fails the gate.
        if (globalThis.process?.env?.MEASURE) {
          console.log(
            `  ${viewport.width}px ${picker.column}: ${mine.length} summaries, ` +
              `page ${measured.pageClientWidth}/${measured.pageScrollWidth}\n` +
              mine
                .map(
                  (sum) =>
                    `    ${sum.clientWidth}/${sum.scrollWidth}px h${sum.height} "${sum.text}"`
                )
                .join('\n')
          )
        }

        // Integers from the same rounding rule, so this is exact -- see
        // `assertFits` on why the `value` basis gets no tolerance.
        const clipped = mine
          .filter((sum) => sum.scrollWidth > sum.clientWidth)
          .map((sum) => `"${sum.text}": ${sum.clientWidth}px for ${sum.scrollWidth}px`)
        expect(clipped, `${viewport.width}px ${picker.column}: summaries narrower than their text`)
          .toEqual([])

        // Item 1 of the UI Definition of Done, asked of the column this feature
        // added: a wider table must still scroll inside its own container.
        const escaped = mine
          .filter((sum) => sum.wrapperRight != null && sum.wrapperRight > viewport.width + 1)
          .map((sum) => `container ends at ${sum.wrapperRight} in a ${viewport.width} viewport`)
        expect(escaped, `${viewport.width}px ${picker.column}: a scroll container escaped`).toEqual(
          []
        )
        expect(
          measured.pageScrollWidth,
          `${viewport.width}px ${picker.column}: the document overflows by ` +
            `${measured.pageScrollWidth - measured.pageClientWidth}px`
        ).toBeLessThanOrEqual(measured.pageClientWidth)
      })
    }
  })
}

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
