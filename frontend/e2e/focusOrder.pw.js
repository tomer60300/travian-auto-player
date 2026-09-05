/**
 * Focus order against visual order, on the stages an accessibility census
 * reported three backwards jumps on.
 *
 * The UI Definition of Done's item 2: "every interactive element is reachable by
 * Tab, in an order that matches the visual order". A backwards jump is not
 * cosmetic in these tables -- they are fifteen columns wide and are filled in by
 * keyboard, so a Tab that moves the caret up the screen loses the operator's
 * place in a grid of near-identical cells.
 *
 * NONE OF THE THREE REPRODUCE, and this file is the measurement that says so as
 * much as it is the guard that keeps it true. What was reported:
 *
 *   1. the Targets edit-by-resource tables visiting 02 -> 11 -> 13 while "13"
 *      rendered above the other two;
 *   2. every role row in the templates panel leaving its last mode cell for a
 *      spend cell that rendered to the LEFT of it;
 *   3. the Plan stage reaching "Preview" before "Reconcile all villages" while
 *      Reconcile rendered above it, and the diagnostic disclosures being visited
 *      in a different order than they stack.
 *
 * Walked here with real Tab presses. The Targets tables at 1440 with every
 * village on an explicit mode, so the value box beside each mode select is
 * editable and therefore visited at all; the role templates with the panel
 * forced open and a template that fills every box; the Plan stage at 375 AND
 * 1440 with every disclosure open. The tables come out in row order, the role
 * rows read left to right, and Preview precedes Reconcile both on screen and in
 * the DOM at both widths.
 *
 * The likely cause of all three is the measurement rather than the page.
 * TABBING SCROLLS. Moving focus into an off-screen control makes the browser
 * scroll it into view -- the page vertically, and `ScrollableTable`'s container
 * horizontally -- so consecutive `getBoundingClientRect()` readings are taken in
 * different scroll states, and comparing two of them reports jumps that are not
 * there. It reports them systematically, which fits a census that found the same
 * shape in "all 4 resources" and "every role row". `tabOrder` below corrects for
 * both axes, which is the only interesting thing about it.
 *
 * The second correction is COLUMN ADVANCE. The controlled-run fields are a
 * three-column grid at 1440, so leaving the bottom of one column for the top of
 * the next goes up on screen and is exactly right; only a move up that is not
 * also to the right is a jump.
 *
 * Kept as a guard rather than deleted with the finding: the invariant is real
 * even where the reported breaches were not, and a `flex-wrap` or an `order:`
 * added later would break it silently.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test focusOrder
 */

import { expect, test } from '@playwright/test'

import { PLAN, SNAPSHOT, isolate, seed, village } from './plannerHarness'

/** Every village on an absolute target for every resource, so the value boxes
 *  beside the mode selects are editable rather than disabled. */
const ALL_ABSOLUTE = Object.fromEntries(
  ['lumber', 'clay', 'iron', 'crop'].map((r) => [
    r,
    Object.fromEntries([20002, 20011, 20013].map((id) => [id, { mode: 'absolute', value: 1000 }])),
  ])
)

/** Three villages, named as the census account names them, so a row order
 *  problem has somewhere to show up. */
const THREE = {
  ...SNAPSHOT,
  villages: [village(20002, '02', 0, 0), village(20011, '11', 4, 0), village(20013, '13', -6, 2)],
}

/** Where the caret actually goes, by pressing Tab, and where each stop renders.
 *
 * Real Tab presses rather than a `querySelectorAll` in document order: document
 * order is only the tab order while nothing carries a positive `tabindex` and
 * nothing sits inside a closed disclosure, and the point of the measurement is
 * to find out what the browser really does.
 *
 * THE COORDINATES ARE SCROLL-CORRECTED, and that is the whole difficulty.
 * Tabbing into an off-screen cell makes the browser scroll it into view -- the
 * page vertically, and `ScrollableTable`'s own container horizontally -- so a
 * raw `getBoundingClientRect()` is measured in a different scroll state at
 * every stop, and comparing two of them reports jumps that are not there. `y`
 * is taken against the document (`+ window.scrollY`) and `x` against the
 * nearest horizontally scrolling ancestor's content box, so both are fixed
 * properties of the layout rather than of the caret's history.
 */
async function tabOrder(page, limit = 400) {
  await page.evaluate(() => document.body.focus())
  await page.keyboard.press('Tab')
  const stops = []
  const seen = new Set()
  for (let i = 0; i < limit; i += 1) {
    const stop = await page.evaluate(() => {
      const el = document.activeElement
      if (!el || el === document.body) return null
      const rect = el.getBoundingClientRect()
      let x = rect.left
      for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
        if (n.scrollWidth > n.clientWidth + 1) {
          x = rect.left + n.scrollLeft - n.getBoundingClientRect().left
          break
        }
      }
      const path = []
      for (let n = el; n && n !== document.body; n = n.parentElement) path.push(n.tagName)
      const label = el.getAttribute('aria-label') || (el.textContent || '').trim()
      const row = el.closest('tr')
      return {
        key: path.join('/') + '|' + label.slice(0, 40),
        name: label.slice(0, 50) || '<' + el.tagName.toLowerCase() + '>',
        y: Math.round(rect.top + window.scrollY),
        x: Math.round(x),
        row: row ? row.rowIndex : -1,
        table: row && row.closest('table') ? row.closest('table').dataset.probe || '' : '',
      }
    })
    if (stop == null) break
    if (seen.has(stop.key)) break
    seen.add(stop.key)
    stops.push(stop)
    await page.keyboard.press('Tab')
  }
  return stops
}

/** Every place the caret moves BACKWARDS on screen.
 *
 * Two separate readings, because a table row and a page column fail
 * differently:
 *
 *   * DOWN the page -- a later stop whose top edge is higher than the previous
 *     one's, when the two are not in the same table row. `tolerance` absorbs a
 *     checkbox sitting a few pixels lower than the select beside it.
 *   * ACROSS a row -- a later stop in the SAME row whose left edge is left of
 *     the previous one's. Within one row the reading direction is horizontal,
 *     so this is the same defect turned ninety degrees.
 */
function backwardsJumps(stops, tolerance = 24) {
  const out = []
  for (let i = 1; i < stops.length; i += 1) {
    const from = stops[i - 1]
    const to = stops[i]
    const sameRow = from.row !== -1 && from.row === to.row && from.table === to.table
    if (sameRow) {
      if (to.x < from.x - tolerance) {
        out.push(from.name + ' (x=' + from.x + ') -> ' + to.name + ' (x=' + to.x + ')')
      }
      continue
    }
    // Moving UP the page is only a jump when it is not a COLUMN ADVANCE. The
    // controlled-run fields are a three-column grid at 1440, so leaving the
    // bottom of column one for the top of column two goes up on screen and is
    // exactly the right reading order -- the defect would be arriving in a
    // column the eye has already passed. Strictly to the right is the test.
    if (to.y < from.y - tolerance && to.x <= from.x + tolerance) {
      out.push(from.name + ' (y=' + from.y + ') -> ' + to.name + ' (y=' + to.y + ')')
    }
  }
  return out
}

/** Just the accessible names, for the "did the walk get there" guards. */
const names = (stops) => stops.map((t) => t.name)

async function open(page, stage, extra = {}) {
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    return undefined
  })
  // `seed` writes the harness's two-village snapshot first and then applies
  // `extra` over it, so naming the key here is what installs the third village.
  await seed(page, { planner_snapshot: THREE, ...extra })
  await page.goto('/resource-planner')
  if (stage) await page.getByRole('button', { name: stage }).click()
}

test.describe('focus order follows the page down', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('Targets: the four edit-by-resource tables', async ({ page }) => {
    // Every village on an explicit mode, so the VALUE box beside each mode
    // select is editable and therefore in the tab order at all. Where every row
    // is "Keep own" those boxes are disabled and the walk never visits them.
    await open(page, 'Targets', { planner_profiles: { Day: ALL_ABSOLUTE } })
    await expect(page.getByLabel('Lumber value for 11')).toBeEnabled()

    const stops = await tabOrder(page)
    // The guard needs guarding: a walk that never reached these cells would
    // report no jumps for the wrong reason.
    expect(names(stops)).toContain('Lumber value for 11')
    expect(names(stops)).toContain('Select 13 for batch edit of Lumber')
    expect(backwardsJumps(stops)).toEqual([])
  })

  test('Targets: the role templates panel, every box editable', async ({ page }) => {
    // With real modes the VALUE box beside each mode select is enabled, so the
    // walk visits both halves of every target cell rather than only the select.
    await open(page, 'Targets', {
      planner_village_roles: { 20011: 'def' },
      planner_role_templates: {
        def: {
          allocations: {
            lumber: { mode: 'absolute', value: 8372 },
            clay: { mode: 'absolute', value: 5168 },
            iron: { mode: 'absolute', value: 5809 },
            crop: { mode: 'sustain', value: 100 },
          },
          consumption: { lumber: 8372 },
        },
      },
    })
    await page.evaluate(() => {
      for (const d of document.querySelectorAll('details')) d.open = true
    })

    const stops = await tabOrder(page)
    // The pair the census named: a role's last mode cell, and its first spend
    // cell. Both are visited, and in that order, left to right.
    expect(names(stops)).toContain('DEF Crop mode')
    expect(names(stops)).toContain('Lumber spent per hour by a DEF village')
    expect(backwardsJumps(stops)).toEqual([])
  })

  test('Targets: the role templates panel', async ({ page }) => {
    await open(page, 'Targets', {
      planner_village_roles: { 20011: 'def' },
      planner_role_templates: { def: { allocations: {}, consumption: { lumber: 8372 } } },
    })
    // The panel is a disclosure, so nothing inside it is focusable until it is
    // open -- which is why a closed-panel walk cannot see this at all.
    await page.evaluate(() => {
      for (const d of document.querySelectorAll('details')) d.open = true
    })

    expect(backwardsJumps(await tabOrder(page))).toEqual([])
  })
})

/** The Plan stage carries the write path, so a caret that loses its place there
 *  is the most expensive of the three. Measured at both ends of the responsive
 *  range because the census reported the jump as present at 375 too, where the
 *  two-column rows collapse. */
test.describe('focus order on the Plan stage', () => {
  for (const width of [375, 1440]) {
    test(`the write path reads downwards at ${width}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1200 })
      await open(page, null)
      await page.getByRole('button', { name: /^Build plan/ }).click()
      await page.getByRole('button', { name: 'Plan', exact: true }).click()
      await expect(page.getByText(/^Routes$/)).toBeVisible()
      await page.evaluate(() => {
        for (const d of document.querySelectorAll('details')) d.open = true
      })

      const stops = await tabOrder(page)
      const order = names(stops)
      expect(order).toContain('Preview (0 requests)')
      expect(order).toContain('Reconcile all villages')
      // The census's third finding, stated as the invariant it was about.
      expect(order.indexOf('Preview (0 requests)')).toBeLessThan(
        order.indexOf('Reconcile all villages')
      )
      expect(backwardsJumps(stops)).toEqual([])
    })
  }
})
