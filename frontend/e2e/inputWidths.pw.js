/**
 * Every `.input-field` in the app's dense surfaces, MEASURED.
 *
 * Every one that is on screen, with the disclosures driven open so that
 * "on screen" is not a loophole -- `openDisclosures` opens every `<details>`
 * and asserts that none stayed closed, because nine of the Snapshot stage's 42
 * controls live behind a per-village Spends summary and the whole
 * Role-templates panel lives behind one. What is left over is reported by name
 * on the `census` line rather than implied: on these six surfaces that is one
 * control, the village-selector `<select>` the shell renders twice and shows
 * once, at whichever of the two breakpoints is not in force.
 *
 * "By name" is now true rather than asserted. That line read
 * `1 not on screen: select` at all eighteen cases, because `labelOf` fell
 * through every branch to the tag name -- the selector carried no
 * `aria-label`, no `title`, no `aria-labelledby`, no wrapping `<label>`, no
 * previous-sibling text and no placeholder -- and a control the sweep had
 * genuinely missed would have printed identically. The selector is named now
 * (`Active village`, one name for both renders), and `labelOf`'s last resort
 * points at a PLACE rather than a tag, so the two cases can no longer be
 * confused. Round 10 confirmed the double render independently -- two selects
 * with identical options and value, one in `HEADER.top-bar > DIV.mobile-only`
 * with a real rect and one in `ASIDE.sidebar` with a zero rect -- which is the
 * evidence for the sentence above; the census line is what makes it legible.
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
 * Reading a control's content width takes four methods, because the DOM offers
 * no single one and because the four kinds of control are not asked the same
 * question:
 *
 *   * a `<select>` never overflows -- it clips its label instead -- so it is
 *     cloned with `width: auto` and the clone's natural width is what it
 *     needed. The clone keeps its className, so `text-xs` and `.input-field`
 *     still apply to it.
 *   * a NUMBER input scrolls its text, so `scrollWidth` against `clientWidth`
 *     is the direct question and needs no font arithmetic. A figure is the one
 *     kind of content where scrolling is not an acceptable answer: a spend box
 *     showing "837" of "8372" is not truncated, it is WRONG, and nothing on
 *     screen says so. This is the glyph-fit rule, and figures are what it is
 *     for -- which is what the argument for it was always actually about.
 *   * a BROWSER-DRAWN input -- `time`, and the other UA-widget types the list
 *     in `MEASURE` names -- is cloned like a select, and for the same reason:
 *     the UA renders fields, separators, a spinner and a picker button out of
 *     nothing the DOM will show you, so `scrollWidth` reports no overflow and
 *     a canvas run over `value` measures a string the widget is not drawing.
 *     The two profile-window pickers are the case. They are `w-[74px]`, which
 *     leaves 42px of glyph room -- half of what the narrowest free-text box
 *     has -- and they passed the free-text branch by the coincidence that
 *     `ctx.measureText("07:00")` is 39.8px. Cloned at `width: auto` the UA
 *     asks for 106.8px for that widget and had been given 74, so it was
 *     dropping the picker affordance; they are `w-auto` now and get what the
 *     UA asks.
 *   * a FREE-TEXT input is measured against its PLACEHOLDER only, and its
 *     value is asked a different question entirely.
 *
 *     The placeholder is what must fit before the field is filled, or the
 *     field cannot even be identified. It is also the half a column can
 *     actually be sized to, because it is authored rather than typed: 35.3px
 *     for "none", 69.8px for "Ally name", 94.9px for "New farm list", 137.9px
 *     for "Alliance name or ID" -- every one a number the design owns.
 *
 *     The VALUE is not a width requirement and cannot be made into one. This
 *     spec has now tried twice. 32 characters of an ally name is 296.1px of
 *     glyphs, needing a 328.1px box inside a 293px strip: unsatisfiable, red
 *     at all three viewports with no code defect, and it had already done
 *     damage -- the 214px "clip" it reported for a 144px name box is what
 *     talked round 8 into `w-56` and 82% of the strip. Ten characters looked
 *     satisfiable only because it was never exercised: it was derived from
 *     `Not from` at `w-28`, 80px of glyph room, and the fixture seeded that
 *     box EMPTY, so the basis fell back to the placeholder every time. Filled
 *     with what an operator types there -- a comma-separated list of village
 *     names -- ten characters is 68.2px for "02, 11, 13" but 87.4px for
 *     "Muehlenbac", 94.5px for "Sommerwind", 86.9px for "Nordwestpo": eight of
 *     ten plausible runs over an 80px budget, again with no code defect. A
 *     cap on unbounded input is a wish whatever number it is set to, and the
 *     one calibration site it was derived from is the one that breaks it.
 *
 *     So the value's claim is not that it FITS but that the operator can get
 *     to it: the box must be scrollable to the end of its own content, asked
 *     by moving its scrollport and reading back where it landed, the same way
 *     the wrapper pass asks it of a container. A name that scrolls is still
 *     legible -- the caret and the arrow keys reach the rest, and the operator
 *     typed it. A name that is CLIPPED is not, and that is what this catches:
 *     an `overflow: hidden`, a `text-overflow: ellipsis`, a `readonly` box
 *     showing nine characters of thirty-two with no way to see the rest. It is
 *     exercised rather than vacuous -- the foreign-target name box holds 296px
 *     of value in 144px and reaches all 152px of the difference -- and it is
 *     satisfiable at any width, which the glyph cap was not.
 *
 *     A bare minimum box width was the other candidate and is not here: 80px
 *     or 100px would be a magic number with no argument behind it, where the
 *     placeholder is the app's own statement of what has to be legible.
 *
 * A `.input-field` on any other input type is reported as `unclassified` and
 * FAILS. Measuring a control by a method that does not fit it is how the
 * profile-window pickers passed for a year, and the next control type added
 * to this app should not get the same silence.
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
    if (path.endsWith('/distribution/setup')) {
      // Nothing saved on the server, which is the resting state for a fresh
      // account and the one the storage panel renders its invitation for. The
      // page probes this on arrival, so leaving it to the abort below would
      // measure the panel's ERROR line instead of the sentence an operator
      // actually reads.
      return route.fulfill({
        status: 404,
        json: { detail: 'No planner setup is saved for this account.' },
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
      // 32 characters, on purpose and longer than any box in the app can
      // show: the free-text rule has to hold independently of how long the
      // seeded name happens to be, and a 22-character seed is what let a rule
      // calibrated to it look satisfiable.
      //
      // `exclude_origins_text` is seeded for the same reason and it is the
      // more important half. This box held NOTHING before, so the one
      // free-text box the whole cap was calibrated against was never measured
      // carrying a value -- the basis fell back to its "none" placeholder at
      // 35.3px in 80px of room and the rule passed by not being exercised.
      // Filled, at that box's own computed font, ten characters of a real
      // exclusion list is 68.2px for "02, 11, 13" but 87.4px for
      // "Muehlenbac", 94.5px for "Sommerwind" and 85.5px for "Weinberg-W":
      // eight of ten plausible village names blow an 80px budget with no code
      // defect and no reachable width. This value is the one an operator
      // actually types -- a comma-separated list of village names -- so the
      // calibration point is exercised now and the rule above is the one that
      // can survive it.
      set('planner_foreign_targets', [
        {
          name: 'Rheinbund-Aussenposten-Nordwest3',
          x: -117,
          y: 143,
          crop_per_hour: 12_500,
          safety_margin_pct: 5,
          route_eligible: true,
          exclude_origins_text: '02, 11, 13',
        },
      ])
      // Two profiles, so the Day & night table has the pair whose contrast is
      // the whole point of it -- and the attendance answers that make it a
      // day/night asymmetry rather than one row twice.
      set('planner_profiles', { Day: {}, Night: {} })
      set('planner_npc_attended', { Day: true, Night: false })
      // Filled, like every other box in this fixture: an empty control
      // measures as wide as its padding and would let a collapsed column pass.
      set('planner_reserved_window', ['20:00', '21:00'])
      set('planner_npc_feedstock', { [defA]: ['clay', 'crop'] })
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
  // The three kinds of `<input>` this app puts `.input-field` on, because they
  // need three different questions. Declared in here rather than beside the
  // other constants because this function is serialised into the page and
  // closes over nothing from Node.
  //
  // BROWSER-DRAWN: the UA renders a widget of its own -- fields, separators, a
  // spinner, a picker button -- out of nothing the DOM will show you. Canvas
  // arithmetic over `value` does not model any of it, which is how the two
  // profile-window pickers passed for a year: `ctx.measureText("07:00")` is
  // 39.8px in 42px of glyph room, so the FREE-TEXT branch called them fine
  // while the UA wanted 106.8px for the widget and got 74px.
  //
  // FREE TEXT: unbounded operator input. See the header comment on why its
  // value cannot be a width requirement at all.
  //
  // Everything else here is `number`, which is the one kind where scrolling is
  // not an acceptable answer. Anything that is none of the three is reported
  // and asserted on rather than quietly measured by the last branch: a control
  // type this list has never seen is exactly the case that produced a
  // meaningless verdict before.
  const BROWSER_DRAWN = ['time', 'date', 'datetime-local', 'month', 'week', 'color', 'range', 'file']
  const FREE_TEXT = ['text', 'search', 'email', 'url', 'tel', 'password', '']

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
      // The Allocate stage's default view. Named after the two other planner
      // tables above it are ruled out, because its headers are the bare
      // resource labels and "Lumber" is a prefix of the Snapshot table's
      // "Lumber/h" and the Role-template table's "Lumber target".
      if (heads[0] === 'Village' && heads.some((h) => h === 'Lumber')) return 'By-village result'
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
    // Nothing names it, so say WHERE it is rather than WHAT it is. The tag
    // name this used to return is the one answer that cannot be acted on and
    // cannot be told apart from a control the sweep genuinely missed: the
    // census line read `1 not on screen: select` at all eighteen cases, which
    // is exactly what a real miss would have printed, so the docstring's claim
    // that the leftover is "reported by name" was carried by a word that named
    // nothing. Every control on these surfaces is named now (the sidebar
    // village selector was the last one), so this branch is the guard rather
    // than the normal case -- and when it does fire it points at a place.
    // Proven by taking the name back off both renders of that selector, at
    // 375: the visible one reports `UNNAMED select in DIV.min-h-screen >
    // HEADER.top-bar > DIV.mobile-only` and the census's leftover reports
    // `UNNAMED select in ASIDE.sidebar > DIV.flex > DIV.flex-1` -- the double
    // render round 10 confirmed, told apart by where each one is.
    const path = []
    for (let node = el.parentElement; node != null && path.length < 3; node = node.parentElement) {
      path.unshift(node.classList[0] ? `${node.tagName}.${node.classList[0]}` : node.tagName)
    }
    return `UNNAMED ${el.tagName.toLowerCase()} in ${path.join(' > ')}`
  }

  const ctx = document.createElement('canvas').getContext('2d')
  const out = []
  const skipped = []
  for (const el of document.querySelectorAll('.input-field')) {
    // `checkVisibility()`, not `rect.width === 0 && rect.height === 0`.
    //
    // A control inside a CLOSED `<details>` has a non-zero rect and mutually
    // overlapping coordinates -- nine of them on the Snapshot stage, every one
    // reporting x=1361..1381 inside a cell that ends at 1365 -- so the rect
    // test measured nine boxes that were not on screen, in a geometry that
    // means nothing, while skipping one that is real the moment the summary is
    // opened. Neither direction is a false pass today, but the docstring's
    // "every `.input-field` ... MEASURED" was untrue in both.
    //
    // The disclosures are driven OPEN before this runs (see
    // `openDisclosures`), so "visible" is not a way of quietly skipping the
    // controls behind a summary: on every surface here the only thing left in
    // `skipped` is the village-selector `<select>` that the shell renders twice
    // and shows once, at whichever of the two breakpoints is not in force.
    if (!el.checkVisibility()) {
      skipped.push(labelOf(el))
      continue
    }
    const style = getComputedStyle(el)
    const rect = el.getBoundingClientRect()
    const borders = parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth)
    let needed
    let basis
    // Free text only: whether the box can be scrolled to the end of what is
    // in it. Null everywhere else, which is what `assertFits` filters on.
    let reachedInside = null
    if (el.tagName === 'SELECT') {
      // A select clips rather than scrolls, so ask a clone what it wanted.
      const clone = el.cloneNode(true)
      clone.style.cssText = 'width:auto;position:absolute;left:-9999px;top:0'
      document.body.appendChild(clone)
      needed = clone.getBoundingClientRect().width
      clone.remove()
      basis = 'options'
    } else if (el.tagName === 'INPUT' && BROWSER_DRAWN.includes(el.type)) {
      // Asked exactly the way a `<select>` is asked, and for the same reason:
      // the UA draws the control and neither `scrollWidth` nor a canvas run
      // over `value` can see what it drew. A clone at `width: auto` reports
      // the intrinsic width the UA wants for the widget it is going to render
      // -- fields, separators, spinner, picker button and all -- so
      // `width < needed` is the statement "the UA has been given less room
      // than the control it was asked for".
      //
      // `value` is copied onto the clone because `cloneNode` copies the
      // ATTRIBUTE and these boxes are React-controlled: the attribute is
      // empty, and an empty time input can have a different intrinsic width
      // from a filled one.
      const clone = el.cloneNode(true)
      clone.value = el.value
      clone.style.cssText = 'width:auto;position:absolute;left:-9999px;top:0'
      document.body.appendChild(clone)
      needed = clone.getBoundingClientRect().width
      clone.remove()
      basis = 'widget'
    } else if (el.tagName === 'INPUT' && FREE_TEXT.includes(el.type)) {
      // The PLACEHOLDER, and nothing about the value -- see the header
      // comment. The placeholder is the app's own statement of what has to be
      // legible before the field is filled, it is authored rather than typed,
      // and it is therefore the one half of this that a column can be sized
      // to. The value is asked a different question, below and in
      // `unreachable`: not "does it fit" but "can the operator get to it".
      ctx.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
      needed =
        ctx.measureText(el.placeholder ?? '').width +
        parseFloat(style.paddingLeft) +
        parseFloat(style.paddingRight) +
        borders
      basis = 'placeholder'
      // Asked by MOVING it, as the wrapper pass asks the same question of a
      // container. An input's own scrollport has no descendants whose boxes
      // this could shift, and it is restored immediately either way.
      const before = el.scrollLeft
      el.scrollLeft = el.scrollWidth
      reachedInside = el.scrollLeft
      el.scrollLeft = before
    } else if (el.tagName === 'INPUT' && el.type === 'number') {
      needed = el.scrollWidth + borders
      basis = 'value'
    } else {
      // Neither a select nor any input type this spec knows how to ask about.
      // Measured the least wrong way and then FAILED on, rather than reported
      // as fine: a verdict from the wrong method is what `basis` exists to
      // make visible.
      needed = el.scrollWidth + borders
      basis = `unclassified ${el.tagName.toLowerCase()}/${el.type}`
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
      reachedInside,
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
    if (!el.checkVisibility()) continue
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
      // A TABLE is what makes sideways scrolling a design rather than a
      // squeeze: it has columns to hide and an identity column to pin. A flex
      // panel of controls has neither, so its overflow is only ever the panel
      // being narrower than the controls in it -- see `cramped`.
      hasTable: w.querySelector('table') != null,
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
    if (!th.checkVisibility()) continue
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

  // Fourth pass: an overflowing dense table must PIN its identity column, and
  // must say that it scrolls.
  //
  // `.sticky-col` used to be gated on `@media (max-width: 640px)`, on the
  // stated grounds that "at desktop widths the table does not overflow". False
  // at both other viewports: the Snapshot table's intrinsic width is 1408px in
  // a 470px container at 768 and a 1122px container at 1440, so the identity
  // cell was `position: static` and scrolled to left -688 -- off screen -- while
  // the four hand-typed columns stayed on it. A Trade Office level typed one
  // row off breaches that village's merchant budget with nothing on screen
  // saying so. The `sm:hidden` swipe hint was gone at those widths too, so
  // there was not even a warning that the columns existed.
  //
  // Three claims, asked of the geometry rather than of a breakpoint:
  //   * an overflowing table's identity column computes to `sticky`;
  //   * scrolled to the end, that cell is still inside its own container;
  //   * a scroll hint is visible exactly when the table overflows -- not
  //     only when it is narrow, and not when there is nothing to scroll.
  const tables = []
  for (const table of document.querySelectorAll('table')) {
    const wrapper = scroller(table)
    if (wrapper == null) continue
    // A table inside a CLOSED `<details>` still reports a scrollWidth and a
    // clientWidth -- the Role-templates panel reads 1546px of overflow while
    // collapsed, and `ScrollableTable`'s synchronous `measure()` even puts
    // `.table-overflowing` on it from that reading -- but it is not on screen
    // and its geometry is not what the operator will meet. `checkVisibility`
    // is the question `rect.width === 0 && rect.height === 0` was standing in
    // for, and it is asked of the TABLE rather than of a pinned header cell
    // so that a table with no pinned header is still collected. That is what
    // `unwired` needs: the pass used to `continue` on a missing
    // `thead th.sticky-col`, so the one shape the D5 rule exists to forbid --
    // an overflowing dense table that pins nothing -- was the one shape it
    // could not see.
    if (!table.checkVisibility()) continue
    const head = table.querySelector('thead th.sticky-col')
    const cell = table.querySelector('tbody .sticky-col')
    const hint = wrapper.parentElement?.querySelector(':scope > .scroll-hint') ?? null
    // `.row-focus-edge` and `.sticky-col` have to be the SAME cell. The pair
    // is the whole answer index.css gives to "the pinned cell sits out of the
    // row tint" -- an opaque background cannot show the tint, so the edge
    // colours the pinned cell's left border instead. Put the edge on a cell
    // that scrolls and the focused row has no marker on the only cell that
    // stays, which is the cell the marker exists for. Structural, so it is the
    // same answer at every viewport: at 1440 the Allocate grid does not even
    // overflow, and the mismatch was still there.
    const edge = table.querySelector('tbody .row-focus-edge')
    // What the hint has to agree with, read off the header row here rather
    // than taken from the component: the whole point of the claim below is
    // that the words on screen describe THIS table. `[aria-hidden]` children
    // are stripped because a sort control's arrow glyph is decoration by
    // definition -- `Village↕` is not a column name.
    const headerLabel = (th) => {
      const clone = th.cloneNode(true)
      for (const decoration of clone.querySelectorAll('[aria-hidden="true"]')) decoration.remove()
      return clone.textContent.replace(/\s+/g, ' ').trim()
    }
    const heads = [...table.querySelectorAll('thead tr:first-child th')]
    const scrolling = heads.filter((th) => th !== head).map(headerLabel).filter((l) => l !== '')
    const before = wrapper.scrollLeft
    wrapper.scrollLeft = wrapper.scrollWidth
    const pinnedLeft = cell ? Math.round(cell.getBoundingClientRect().left) : null
    wrapper.scrollLeft = before
    tables.push({
      surface: surfaceOf(head ?? table),
      overflow: wrapper.scrollWidth - wrapper.clientWidth,
      // Null rather than `static` when nothing is pinned at all, so the two
      // failures read differently: a pinned column that did not take effect
      // is `unpinned`, a table with nothing to pin is `unwired`.
      position: head == null ? null : getComputedStyle(head).position,
      wrapperLeft: Math.round(wrapper.getBoundingClientRect().left),
      pinnedLeft,
      hasHint: hint != null,
      hintVisible: hint != null && hint.checkVisibility(),
      edgeOnPinned: edge == null ? null : edge.classList.contains('sticky-col'),
      hintText: hint == null ? null : hint.textContent.replace(/\s+/g, ' ').trim(),
      pinnedLabel: head == null ? null : headerLabel(head),
      scrollingCount: scrolling.length,
      firstScrolling: scrolling[0] ?? null,
      lastScrolling: scrolling[scrolling.length - 1] ?? null,
      // Whether a figure is TYPED in here. The D5 rule's whole justification
      // is that an edited field must stay attributable to the right row, so
      // this is the scope of `unwired`: a read-only report that scrolls loses
      // a reader's place, which is a smaller thing than a Trade Office level
      // going into the wrong village.
      dense: [...table.querySelectorAll('.input-field')].some((el) => el.checkVisibility()),
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
    skipped,
    wrappers,
    pinned,
    tables,
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

/** What each pinned column costs the strip, or that nothing is pinned here.
 *
 * Printed in BOTH report modes, and printed even when the list is EMPTY: the
 * `crowded` clause in `assertFits` says nothing at a viewport where no column
 * is pinned, and a clause that can be vacuous has to say so out loud. This
 * file already carried one check that could not fail for any wrapper on any
 * page (`X && !X`) and it stood for weeks. */
function pinnedRows(measured) {
  const rows = [
    `    census ${measured.controls.length} measured, ${measured.skipped.length} not on screen` +
      `${measured.skipped.length ? ': ' + measured.skipped.join(', ') : ''}`,
  ].concat(
    measured.tables.map(
      (t) =>
        `    table  ${t.surface.padEnd(18)} overflow ${String(t.overflow).padStart(5)}px` +
        // `position: null` is a table with no pinned column AT ALL, which is
        // a different report from one whose pinning did not take effect --
        // and it is the report the pass could not produce before, because it
        // skipped any table without a `thead th.sticky-col`.
        (t.position == null
          ? ` NOTHING PINNED${t.dense ? ' (editable)' : ' (read-only)'},`
          : ` identity ${t.position}, scrolled to left ${t.pinnedLeft} of container ${t.wrapperLeft},`) +
        ` hint ${t.hasHint ? (t.hintVisible ? 'visible' : 'hidden') : 'absent'}` +
        // The WORDS, not just the presence. The hint is derived from the
        // header row, so printing it is how a reader checks the derivation
        // said something an operator can use rather than merely something
        // the assertion accepts.
        `${t.hintVisible ? `\n           "${t.hintText}"` : ''}`,
    ),
  )
  if (measured.pinned.length === 0) return rows.concat(['    pinned: none at this viewport'])
  return rows.concat(
    measured.pinned.map(
      (p) =>
        `    pinned ${p.surface.padEnd(16)} ${p.width}px of a ${p.strip}px strip` +
        ` -> ${p.left}px left, narrowest scrolling column ${p.narrowest}px` +
        ` ${p.left < p.narrowest ? 'CROWDED' : 'ok'}`,
    ),
  )
}

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

  // No control may be measured by a method that does not fit it. This is the
  // guard on `basis` itself: a `.input-field` on an input type the three
  // branches above have never seen would otherwise be measured as a number
  // box and reported as fine.
  const unclassified = measured.controls
    .filter((c) => c.basis.startsWith('unclassified'))
    .map((c) => `${c.surface} / "${c.label}": ${c.basis}`)
  expect(unclassified, `${where}: a control was measured by a method that does not fit it`).toEqual(
    [],
  )

  const clipped = measured.controls
    .filter(isClipped)
    .map((c) => `${c.surface} / ${c.tag} "${c.label}": ${c.width}px for ${c.needed}px of ${c.basis}`)
  expect(clipped, `${where}: controls narrower than their content`).toEqual([])

  // The other half of the free-text rule, and the half that replaces the
  // glyph cap. A name may be longer than its box -- that is what makes it
  // free text -- so the claim is not that it fits but that the operator can
  // GET to the rest of it: the caret and the arrow keys reach what scrolls,
  // and this asks by moving the box's own scrollport to the end and reading
  // back where it landed. It is what catches an `overflow: hidden`, a
  // `text-overflow: ellipsis` or a `readonly` box that shows nine characters
  // of a thirty-two character ally name with no way to see the rest, which is
  // the real defect the 32-character cap was reaching for and could not
  // state. Exercised rather than vacuous: the foreign-target name box holds
  // 296px of value in 144px and reaches all 152px of the difference.
  const unreachable = measured.controls
    .filter((c) => c.reachedInside != null)
    .filter((c) => c.scrollWidth - c.clientWidth > 0)
    .filter((c) => c.reachedInside < c.scrollWidth - c.clientWidth - 1)
    .map(
      (c) =>
        `${c.surface} / "${c.label}": ${c.clientWidth}/${c.scrollWidth} scrolled only to` +
        ` ${c.reachedInside}`,
    )
  expect(unreachable, `${where}: a free-text box hides part of its value`).toEqual([])

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

  // A scrolling container with no TABLE in it is a different claim entirely,
  // and the weaker one is not available to it: there are no columns to hide
  // and no identity to pin, so sideways scrolling is not a design here, it is
  // the panel being narrower than the controls inside it.
  //
  // The Build queue is the case. Its two panels are `flex-1 min-w-0` siblings
  // of one flex row at every width, so at 375 the queue card was 154px wide
  // holding 389px of bulk bar and queue rows -- 235px of overflow, over the
  // target-level box and the priority select -- and 250/397 at 768. Round 10
  // read that pair off the wrapper list and called it "the Build-queue
  // table": there is no table on this page at any of the three viewports (the
  // only `<table>` in `BuildQueue.jsx` is the validation result, which
  // `openBuildQueue` never renders), and the container is the queue card,
  // whose `overflow-y: auto` forces its `visible` horizontal axis to `auto`
  // per CSS Overflow -- which is the whole reason it appears in this pass at
  // all. So it cannot be wired through `ScrollableTable`, and the fix is for
  // the panels to stop sharing one row when there is not room for two.
  const cramped = measured.wrappers
    .filter((w) => w.overflow > 0 && !w.hasTable)
    .map(
      (w) =>
        `${w.surface}: ${w.clientWidth}/${w.scrollWidth} -- ${w.overflow}px of sideways scroll` +
        ' over controls, with no table to pin or announce',
    )
  expect(cramped, `${where}: a panel of controls is narrower than its controls`).toEqual([])

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

  // The other half of the same hazard: a table wide enough to hide its
  // identity column must pin it and must admit that it scrolls. Asked of the
  // measured overflow, not of a breakpoint -- `.sticky-col` and the swipe hint
  // were both gated on `max-width: 640px` / `sm:hidden` while the Snapshot
  // table overflowed by 938px at 768 and 286px at 1440.
  const unpinned = measured.tables
    .filter((t) => t.overflow > 0 && t.position != null && t.position !== 'sticky')
    .map((t) => `${t.surface}: overflows by ${t.overflow}px, identity column is position: ${t.position}`)
  expect(unpinned, `${where}: an overflowing table does not pin its identity column`).toEqual([])

  // And the table that pins NOTHING. `unpinned` above only ever looked at
  // tables that already had a `thead th.sticky-col` to look at, and so did
  // the pass that feeds it, so the one shape D5's rule exists to forbid was
  // the one shape the suite could not see: the next dense table added to this
  // app would have been invisible here exactly as the two round 10 found
  // were. Scoped to tables a figure is TYPED into, because that is what the
  // rule's justification is about -- a read-only report that scrolls loses a
  // reader's place, where a Trade Office level typed one row off breaches a
  // village's merchant budget with nothing on screen saying so.
  //
  // Vacuous today by construction, and said out loud rather than left to be
  // discovered: every `.input-field`-bearing table on these six surfaces is
  // wired. Proven to fire by injection, the same way `offset` is -- stripping
  // `sticky-col` off the two Snapshot-surface `thead` cells in the page
  // produces, at 375:
  //   Snapshot table: 1151px of overflow over editable controls, pinning
  //     nothing
  //   Foreign targets: 672px of overflow over editable controls, pinning
  //     nothing
  const unwired = measured.tables
    .filter((t) => t.overflow > 0 && t.dense && t.position == null)
    .map(
      (t) =>
        `${t.surface}: ${t.overflow}px of overflow over editable controls, pinning nothing`,
    )
  expect(unwired, `${where}: an overflowing dense table has no identity column at all`).toEqual([])

  // Pinning that does not hold. `position: sticky` with no scrollport, or a
  // `left` offset against the wrong containing block, reports as sticky and
  // still slides away -- so this asks where the cell LANDED after the
  // container was scrolled to its end.
  const slipped = measured.tables
    .filter((t) => t.overflow > 0 && t.pinnedLeft != null && t.pinnedLeft < t.wrapperLeft - 1)
    .map(
      (t) =>
        `${t.surface}: scrolled to the end, the identity cell sits at left ${t.pinnedLeft}` +
        ` while its container starts at ${t.wrapperLeft}`,
    )
  expect(slipped, `${where}: the identity column scrolled out of its own container`).toEqual([])

  // The other side of the same question, and nothing was asking it. `slipped`
  // tests `pinnedLeft < wrapperLeft - 1` only, and `crowded` reads header
  // WIDTHS rather than offsets, so a pinned column parked at a wrong POSITIVE
  // offset passed every clause in this file: injecting
  // `.table-overflowing .sticky-col { left: 400px }` fired nothing, while on
  // screen the identity column sits 400px into the strip permanently covering
  // four scrolling columns -- worse than not pinning at all, because the
  // columns it hides are hidden at every scroll position including zero.
  //
  // `left: 0` is the only offset that means "hold it at the edge of the
  // scrollport", so the claim is equality rather than a bound: scrolled to the
  // end, the pinned cell's left edge IS the container's left edge.
  //
  // Proven on that injection rather than assumed, at 375:
  //   Snapshot table: the identity cell is pinned 221px into its own
  //     container (cell at 262, container starts at 41)
  //   Foreign targets: pinned 132px in (cell at 173, container at 41)
  // 221 rather than 400 because a sticky offset is clamped by the cell's own
  // containing block -- which is exactly why the clause reports the DISTANCE
  // it measured rather than the offset somebody wrote.
  const offset = measured.tables
    .filter((t) => t.overflow > 0 && t.pinnedLeft != null && t.pinnedLeft > t.wrapperLeft + 1)
    .map(
      (t) =>
        `${t.surface}: the identity cell is pinned ${t.pinnedLeft - t.wrapperLeft}px into its own` +
        ` container (cell at ${t.pinnedLeft}, container starts at ${t.wrapperLeft}), covering the` +
        ' columns it is supposed to sit beside',
    )
  expect(offset, `${where}: the identity column is pinned inside the scrolling strip`).toEqual([])

  // The pinned cell has to be the cell that carries the focus edge. index.css
  // justifies pinning partly on that pair -- the pinned cell cannot show the
  // row tint through its opaque background, so the edge marks it instead --
  // and the pair only works if both classes land on one cell. The Allocate
  // grid put `.row-focus-edge` on the row-select checkbox cell and
  // `.sticky-col` on the village cell beside it, so the focused row had no
  // marker on the only cell that stays put, and the checkbox itself scrolled
  // off: measured at the end of the scroll, x -171..-158 in a container
  // starting at 41 (375) and 214..227 in one starting at 249 (768).
  const unmarked = measured.tables
    .filter((t) => t.edgeOnPinned === false)
    .map(
      (t) =>
        `${t.surface}: the row focus edge is on a cell that scrolls, not on the pinned` +
        ' identity cell',
    )
  expect(unmarked, `${where}: the pinned identity cell does not carry the row focus edge`).toEqual(
    [],
  )

  // And the hint, in BOTH directions: absent where the table scrolls is the
  // defect above; present where it does not is a table claiming to hide
  // columns it is showing.
  const misannounced = measured.tables
    // Scoped to the tables `ScrollableTable` owns -- identified by the pinned
    // header, which is the only thing it is asked to wrap for. A table that
    // pins nothing has no hint to match and is `unwired`'s business.
    .filter((t) => t.position != null)
    .filter((t) => t.hintVisible !== t.overflow > 0)
    .map(
      (t) =>
        `${t.surface}: overflow ${t.overflow}px but the scroll hint is` +
        ` ${t.hasHint ? (t.hintVisible ? 'visible' : 'hidden') : 'absent'}`,
    )
  expect(misannounced, `${where}: a table's scroll hint does not match its overflow`).toEqual([])

  // And what the hint SAYS, against the header row it claims to describe.
  //
  // The hints were hand-typed enumerations, and the one over the Snapshot
  // table had gone stale in the way an enumeration always does: it listed
  // "Merchants, Trade Office, Crop alert, Ships only to, Relays for, Stock
  // floor and Consumption" while the table runs Village / Lumber/h / Clay/h /
  // Iron/h / Net crop / Merchants / ROLE / Trade Office / MAX BUSY / Crop
  // alert / Ships only to / Relays for / Stock floor % / Consumption /h.
  // Role (233px, an editable select) and Max busy (96px, an editable number)
  // were never named, so an operator counting across from "Merchants" to
  // "Trade Office" landed on Role -- the exact failure the ScrollableTable
  // docstring says is worse than saying nothing, because the reader types a
  // figure into the column they counted to. The Allocate grid's omitted Own/h
  // the same way, and neither the foreign-target nor the Role-template hint
  // named a single header verbatim.
  //
  // So the hint is DERIVED from the header row now, and this is the claim
  // that keeps the derivation honest: it re-reads the headers here and
  // requires the words on screen to carry the pinned column, the first and
  // last scrolling column, and how many there are. Adding a column moves all
  // four together, which is what the enumeration could not do.
  const contradicted = measured.tables
    // Same scope as `misannounced`, and for the same reason: with nothing
    // pinned there is no pinned column for the words to name, and the table
    // is `unwired`'s business rather than this clause's.
    .filter((t) => t.position != null && t.hintVisible)
    .filter(
      (t) =>
        !t.hintText.includes(t.pinnedLabel) ||
        !t.hintText.includes(t.firstScrolling) ||
        !t.hintText.includes(t.lastScrolling) ||
        !t.hintText.includes(String(t.scrollingCount)),
    )
    .map(
      (t) =>
        `${t.surface}: hint "${t.hintText}" does not name ${t.scrollingCount} columns` +
        ` from "${t.firstScrolling}" to "${t.lastScrolling}" beside pinned "${t.pinnedLabel}"`,
    )
  expect(contradicted, `${where}: a scroll hint describes a table that is not there`).toEqual([])

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
  await page.getByRole('button', { name: 'Targets' }).click()
  await page.getByText('Role templates', { exact: true }).click()
  await expect(page.getByLabel('DEF Lumber value')).toBeVisible()
}

async function openAllocateGrid(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Targets' }).click()
  await page.getByRole('button', { name: 'Edit by resource' }).click()
  await expect(page.getByLabel('Lumber mode for 11')).toBeVisible()
}

/** The whole-day fixture, so the two switch rules and the overrun table are
 *  on screen with real figures rather than as empty sections. */
const DAY_CHECK = {
  villages: [
    {
      village_id: DEF_A,
      village_name: '11',
      resource: 'crop',
      daily_net: -12_000,
      low: 40_000,
      high: 190_000,
      settled: false,
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

/** A plan with every panel populated, so the Plan stage's tables are measured
 *  carrying content rather than as empty cards. */
const PLAN = {
  rows: [
    {
      origin: CAPITAL,
      origin_name: '02',
      destination: DEF_A,
      destination_name: '11',
      cargo: { lumber: 7920, clay: 5168, iron: 5809, crop: 0 },
      cycle_hours: 4,
      dispatch: '08:20',
      arrival: '09:48',
      merchants: 3,
    },
  ],
  budgets: [
    {
      village_id: CAPITAL,
      committed: 9,
      spare: 8,
      over_budget: true,
      trade_office_levels_needed: 2,
      explanation: 'The trip is the cost here, not the Trade Office.',
      legs: [
        {
          destination: '11',
          per_hour: 7920,
          distance_fields: 41,
          one_way_hours: 2.6,
          cycle_hours: 4,
          merchants_per_send: 3,
          sets_in_flight: 2,
          merchants: 6,
        },
      ],
    },
  ],
  shortfalls: [
    { village_id: DEF_A, village_name: '11', resource: 'crop', per_hour: 2200, reason: 'no origin in range' },
  ],
  unallocated: [
    {
      resource: 'lumber',
      total_production: 121_000,
      total_npc_allowance: 22_000,
      total_npc_draw: 15_000,
      unallocated: 3000,
      remainder_village_id: DEF_B,
    },
  ],
  total_merchants: 14,
  feasible: false,
  verdict: {
    executable: false,
    clean: false,
    blockers: ['02 commits 9 merchants against a ceiling of 8'],
    covers: ['every merchant budget', 'every receiver is routable'],
    unweighed: ['overflow'],
    critical_findings: 1,
  },
  relays: [],
  role_deviations: [],
  village_nets: [
    {
      village_id: CAPITAL,
      resource: 'lumber',
      own_per_hour: 60_000,
      npc_allowance_per_hour: 22_000,
      npc_draw_per_hour: 15_000,
      target_per_hour: 21_000,
      ship_per_hour: 0,
      consumption_per_hour: 0,
      net_per_hour: 21_000,
    },
  ],
  night_overruns: DAY_CHECK.night_overruns,
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
  ],
  warnings: ['02 is over its merchant ceiling'],
  diagnostics: {
    headline: 'One thing needs a decision.',
    total_loss_per_day: 96_000,
    loss_by_resource: [{ resource: 'lumber', per_day: 96_000 }],
    counts: { critical: 1, warning: 0, note: 0 },
    groups: [
      {
        key: 'npc_capacity_short',
        severity: 'critical',
        headline: '02 is short 4,000/h of conversion capacity',
        action: 'Lower its target, or raise the stock floor it converts out of.',
        count: 1,
        loss_per_day: 96_000,
        findings: [],
      },
    ],
  },
  plan_digest: 'd'.repeat(64),
}

function plannerRoutes(path) {
  if (path.endsWith('/distribution/day-check')) return DAY_CHECK
  if (path.endsWith('/distribution/plan')) return PLAN
  return undefined
}

async function openDayNight(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Day & night' }).click()
  // Run the composite, so the two switch rules and the overrun table are on
  // screen. Their tables carry no `.input-field`, but they carry pinned
  // identity columns and a scroll container, which is the other half of what
  // this file measures.
  await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
  await expect(page.getByText(/threshold 60%/)).toBeVisible()
}

async function openPlanStage(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

// ── Off the planner ──────────────────────────────────────────────────
//
// `.input-field { width: 100% }` was moved into `@layer components` for the
// planner and shipped to the whole app: 66 of the 111 `.input-field` LINES in
// `src/` also name a width utility, across thirteen files, and every one of
// those utilities went from dead to live in one commit. The planner was the
// only surface measured.
//
// The method is stated because the figures are not reproducible without it,
// and round 10 read them as off by two on the strength of a different one.
// Line-based, counting `w-*` and `w-full` and not `max-w-*`, from
// frontend/:
//
//   grep -rh 'input-field' src --include=*.jsx --include=*.js | wc -l
//     -> 111
//   grep -rh 'input-field' src --include=*.jsx --include=*.js \
//     | grep -cE '(^|[^-a-zA-Z])w-'
//     -> 66, in 13 files
//
// Counted per OCCURRENCE rather than per line it is 112, because one line in
// `VillageSelector.jsx` carries two class strings -- the `compact` ternary --
// and 68 of 112 if `max-w-*` counts as a width, which is the only reading
// that reaches 68 and is where round 10's number comes from. Both are true
// statements about different questions; neither is a correction of the other.
// `grep -rl` gives the 19 files that contain `.input-field` at all, which is
// a third question again, and `index.css`'s "48 sites across 7 files" is a
// fourth (the sites asking for a `py-*`/`px-*`/`text-xs`). Do not reconcile
// them.
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
  // The THIRD control on the same bar, and it was left behind when the other
  // two were named: identical `<span>` + control markup, so the census could
  // only report it as `select`.
  await expect(page.getByLabel('Status', { exact: true })).toBeVisible()
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
  // Six more on this surface that the census could only report by tag or by
  // the wrong text. The stealth pair sits under one `field-label-lg` that
  // names neither box, so `previousElementSibling` gave the first of them the
  // tag name and the second the em dash between them. The four bonus selects
  // ARE inside a wrapping `<label>`, which is why the census printed
  // "Wood-25%50%75%100%" -- a label element's text with the options folded in
  // is not a name a reader can act on, and the accessible name it computes
  // carries the current VALUE rather than the field.
  await expect(page.getByLabel('Stealth delay minimum (s)')).toBeVisible()
  await expect(page.getByLabel('Stealth delay maximum (s)')).toBeVisible()
  for (const resource of ['Wood', 'Clay', 'Iron', 'Crop']) {
    await expect(page.getByLabel(`${resource} minimum %`)).toBeVisible()
  }
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
  // The two stages this round added. Day & night carries the profile windows,
  // the attendance selects, the reserved-window pair and the two fill
  // sections; the Plan stage carries the NPC and totals tables, the sheet, the
  // overrun table and the whole controlled-run bar, which is the densest
  // collection of boxes in the app and had never been measured at all.
  { name: 'Day & night stage (windows + attendance + fills)', open: openDayNight, routes: plannerRoutes },
  { name: 'Plan stage (NPC + totals + sheet + controlled run)', open: openPlanStage, routes: plannerRoutes },
  // Off the planner. `seed` is the planner's fixture, so these take the plain
  // shell instead and answer their own page's calls.
  {
    name: 'Build queue (queue row + bulk bar)',
    open: openBuildQueue,
    routes: queueRoutes,
    seed: seedShell,
  },
  {
    // The second bar is Loop Send Mode, not the transfer bar: the transfer
    // selects only render once a slot is TICKED, and `openFarmFilters` selects
    // a list without ticking anything, so they have never been measured here.
    // The two boxes this surface really adds are the loop interval and
    // duration in section 3.
    name: 'Farm lists (filter bar + Loop Send Mode)',
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
  // The third picker, and the one whose RESTING state is a word rather than a
  // blank: "derived" is an answer, so this column is never empty and its
  // closed summary has real content to be clipped at every viewport.
  {
    column: 'NPC converts from',
    group: /^Stores NPC may convert from at /,
    filled: /^NPC converts from, for 02: \w+, \w+$/,
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
            // Same rule as the sweep's census, for the same reason: a nested
            // summary inside a collapsed one has a rect and no geometry worth
            // asserting on. This test opens exactly the pickers it measures
            // and leaves the rest of the page as the operator finds it, so it
            // does NOT call `openDisclosures`.
            if (!el.checkVisibility()) continue
            const rect = el.getBoundingClientRect()
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

/**
 * Every `<details>` on the surface, opened, and the layout let settle.
 *
 * `MEASURE` skips anything failing `checkVisibility()`, which is right -- a
 * control inside a closed summary has a non-zero rect and coordinates that mean
 * nothing -- but on its own it would turn "not measured because it is
 * degenerate" into "not measured at all". Nine of the Snapshot stage's 42
 * `.input-field`s live behind the per-village Spends summary, and the whole
 * Role-templates panel is behind one on the Allocate-grid surface. So they are
 * opened, and then they ARE measured, in the geometry they really have.
 *
 * Asserted rather than assumed. `expect(...).toHaveCount(0)` on
 * `details:not([open])` is what catches a disclosure that resists being opened
 * -- an earlier version of this ran the open loop before the table had
 * rendered, opened nothing, and reported nine controls as hidden while
 * claiming to have opened everything.
 *
 * Then the wait, and it is a wait for VISIBILITY as well as agreement.
 * Agreement alone was not a sound readiness gate: `ScrollableTable` measures
 * synchronously in its layout effect at mount, hidden or not, and a table
 * inside a closed `<details>` reports a real `scrollWidth` and `clientWidth`
 * -- so it arrives already classed from a measurement of a
 * `content-visibility: hidden` subtree. Measured on the Allocate-grid surface
 * before this runs: `overflow 1546px visible=false inClosedDetails=true
 * cls="relative overflow-x-auto table-overflowing mt-2"`. Harmless in effect,
 * because the closed and open numbers agree at 1546px and it even avoids a
 * shift when the panel opens -- but it means the class could satisfy this
 * condition before the reveal had been measured at all, which is a gate that
 * passes on the wrong evidence.
 *
 * `checkVisibility()` is the part a hidden subtree cannot fake, so it is in
 * the condition. Agreement is still the other half, and it is the same claim
 * `assertFits` then makes -- so a timeout here is a real failure, not a flake.
 */
async function openDisclosures(page) {
  await page.evaluate(() => {
    for (const d of document.querySelectorAll('details')) d.open = true
  })
  await expect(page.locator('details:not([open])')).toHaveCount(0)
  // Scoped to the containers `ScrollableTable` owns, which are the ones whose
  // class means anything -- identified by the pinned column inside them. The
  // first version asked it of every `.overflow-x-auto` on the page, including
  // the plain ones in the Role-templates panel, Farm lists and Auto-scout that
  // hold no identity column and are never given the class, so it waited for a
  // condition that could not become true and timed out on five surfaces.
  await page.waitForFunction(() =>
    [...document.querySelectorAll('.overflow-x-auto')]
      .filter((el) => el.querySelector('.sticky-col') != null)
      .every(
        (el) =>
          // ON SCREEN first. Without this the condition is satisfiable by a
          // class set from a hidden measurement -- see the docstring.
          el.checkVisibility() &&
          el.scrollWidth > el.clientWidth === el.classList.contains('table-overflowing'),
      ),
  )
}

for (const viewport of VIEWPORTS) {
  test.describe(`every .input-field fits its content at ${viewport.width}px`, () => {
    test.use({ viewport })

    for (const surface of SURFACES) {
      test(surface.name, async ({ page }) => {
        await isolate(page, surface.routes, surface.socket)
        await (surface.seed ?? seed)(page)
        await surface.open(page)
        await openDisclosures(page)
        const measured = await page.evaluate(MEASURE)
        report(`${viewport.width}px ${surface.name}`, measured)
        assertFits(`${viewport.width}px ${surface.name}`, measured, viewport)
      })
    }
  })
}
