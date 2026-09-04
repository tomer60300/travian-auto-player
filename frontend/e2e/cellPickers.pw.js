/**
 * The Account table's four pickers, and the height of a row that holds them.
 *
 * MEASURED, at all three viewports, because the finding is a number. Re-taken
 * on this fixture -- twelve villages, fourteen columns -- because the audit's
 * own figures were from a 26-village account with a fifteenth column that item
 * 9 has since moved off this stage. Before:
 *
 *     375px  closed rows 47.8-48.3, page 2743  |  open 298.5-299, page 5751
 *     768px  closed rows 47.8-48.3, page 1996  |  open 298.5-299, page 5004
 *    1440px  closed rows 47.8-48.3, page 1569  |  open 298.5-299, page 4576
 *
 * A row 6.2x taller for a disclosure, and twelve of the fourteen cells in it
 * dead space. A row's height must not depend on a disclosure.
 *
 * So the panels are out of flow: `position: absolute`, anchored to their own
 * cell, over the rows below rather than between them. The disclosure stays a
 * `<details>` -- see the note on `.cell-picker` in index.css for why the native
 * popover was not the answer here -- so what changes is the layout and nothing
 * about the semantics, the keyboard, or the way every other spec opens them.
 *
 * `MEASURE=1 npx playwright test cellPickers` prints the table.
 *
 * NO BACKEND AND NO GAME REQUEST.
 */

import { expect, test } from '@playwright/test'

import { VIEWPORTS, isolate, seed, village } from './plannerHarness'

/** Twelve villages, so the table is the shape the finding was measured on: a
 *  two-row fixture cannot show a row's height changing the page's. */
const MANY = {
  villages: Array.from({ length: 12 }, (_, i) =>
    village(20002 + i, String(i + 1).padStart(2, '0'), i % 5, Math.floor(i / 5)),
  ),
  map_span: 401,
  speed_fields_per_hour: 16,
  requests_used: 0,
  warnings: [],
}

/** Row heights, the document height, and the page's own sideways scroll. */
async function measure(page) {
  return page.evaluate(() => {
    const rows = [...document.querySelectorAll('tbody tr')].map(
      (tr) => Math.round(tr.getBoundingClientRect().height * 10) / 10,
    )
    return {
      rows: rows.length,
      minRow: Math.min(...rows),
      maxRow: Math.max(...rows),
      page: Math.round(document.documentElement.scrollHeight),
      sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }
  })
}

/** Every picker in the Account table, opened the way the operator does. */
async function openPickers(page) {
  await page.evaluate(() => {
    for (const d of document.querySelectorAll('tbody details')) d.open = true
  })
  await expect(page.locator('tbody details:not([open])')).toHaveCount(0)
  // One frame for the layout to settle, so a measurement is of the settled box.
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r())))
}

for (const viewport of VIEWPORTS) {
  test.describe(`a row's height does not depend on a disclosure at ${viewport.width}px`, () => {
    test.use({ viewport })

    test('opening every picker leaves the rows and the page as they were', async ({ page }) => {
      await isolate(page)
      await page.addInitScript(
        ([key, snapshot]) => {
          localStorage.setItem('token', 'e2e-not-a-real-token')
          localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snapshot))
          localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
        },
        ['https://ts2.x1.europe.travian.com|e2e-operator', MANY],
      )
      await page.goto('/resource-planner')
      await expect(page.getByLabel('Trade Office level for 01')).toBeVisible()

      const closed = await measure(page)
      await openPickers(page)
      const open = await measure(page)

      if (globalThis.process?.env?.MEASURE) {
        console.log(
          `${viewport.width}px  closed rows ${closed.minRow}-${closed.maxRow}, page ${closed.page}` +
            `  |  open rows ${open.minRow}-${open.maxRow}, page ${open.page}`,
        )
      }

      expect(closed.rows).toBe(12)
      // The whole finding, as one assertion: the tallest row with every picker
      // open is the tallest row with every picker closed.
      expect(open.maxRow).toBe(closed.maxRow)
      expect(open.minRow).toBe(closed.minRow)
      // And so the page does not grow either. It used to grow 2.1-2.9x.
      expect(open.page).toBe(closed.page)
      // Item 1 of the UI Definition of Done, which an out-of-flow panel is the
      // easiest thing in the world to break.
      expect(open.sideways).toBe(0)
      expect(closed.sideways).toBe(0)
    })

    test('an open panel is on screen, not clipped away by its own container', async ({ page }) => {
      await isolate(page)
      await page.addInitScript(
        ([key, snapshot]) => {
          localStorage.setItem('token', 'e2e-not-a-real-token')
          localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snapshot))
          localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
        },
        ['https://ts2.x1.europe.travian.com|e2e-operator', MANY],
      )
      await page.goto('/resource-planner')
      await expect(page.getByLabel('Trade Office level for 01')).toBeVisible()

      // The LAST row, which is the one an out-of-flow panel hangs off the end
      // of: `overflow-x: auto` computes `overflow-y` to `auto` as well, so a
      // panel past the scroller's bottom edge would be clipped there.
      const last = page
        .locator('summary')
        .filter({ hasText: 'Ships only to, for 12' })
      await last.click()
      const group = page.getByRole('group', { name: 'Villages 12 may ship to' })
      await expect(group).toBeVisible()

      const clipped = await group.evaluate((el) => {
        const scroller = el.closest('.overflow-x-auto')
        const panel = el.getBoundingClientRect()
        const box = scroller.getBoundingClientRect()
        return {
          belowBy: Math.round(panel.bottom - box.bottom),
          aboveBy: Math.round(box.top - panel.top),
        }
      })
      expect(clipped.belowBy).toBeLessThanOrEqual(0)
      expect(clipped.aboveBy).toBeLessThanOrEqual(0)
    })
  })
}

test.describe('the pickers still work as disclosures', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('the summary opens on Enter and the panel is reachable by keyboard', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    const summary = page.locator('summary').filter({ hasText: 'Ships only to, for 02' })
    const group = page.getByRole('group', { name: 'Villages 02 may ship to' })
    await expect(group).toBeHidden()

    await summary.focus()
    await page.keyboard.press('Enter')
    await expect(group).toBeVisible()
    // The first tick inside it is the next tab stop, which is what a disclosure
    // buys and a hand-rolled overlay has to earn.
    await page.keyboard.press('Tab')
    await expect(group.getByRole('checkbox').first()).toBeFocused()

    await summary.focus()
    await page.keyboard.press('Enter')
    await expect(group).toBeHidden()
  })

  test('a panel sits over the rows below it rather than between them', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    await page.locator('summary').filter({ hasText: 'Ships only to, for 02' }).click()
    const group = page.getByRole('group', { name: 'Villages 02 may ship to' })
    await expect(group).toBeVisible()
    // Out of flow: the cell it belongs to is no taller than the one beside it.
    const heights = await page.evaluate(() => {
      const cells = [...document.querySelectorAll('tbody tr:first-child td')]
      return cells.map((td) => Math.round(td.getBoundingClientRect().height))
    })
    expect(new Set(heights).size).toBe(1)
  })
})
