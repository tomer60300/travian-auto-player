/**
 * Checkbox and radio tap targets outside a table, item 4 of the UI Definition
 * of Done.
 *
 * index.css sized the IN-TABLE ones and only those: `@media (pointer: coarse)`
 * gives `.touch-target input[type=checkbox]` 24px plus roomy cell padding,
 * which is WCAG 2.5.8's documented table exemption, and `.touch-target` is worn
 * by planner tables alone. Every other checkbox and radio in the app -- 9 pages
 * of them, per the wave-4 census -- rendered at the browser-native **13x13**,
 * in a label box measured as small as 85x16 (Logs, "Show details"). The DoD's
 * 24px exemption is written for table checkboxes; these are not in tables, so
 * 44x44 is the number.
 *
 * Swept rather than enumerated: each page is opened and EVERY `<label>` that
 * directly wraps a checkbox or radio and is not inside a table gets measured.
 * A site the census could not read (its FarmBuilder sub-components) or a site
 * added later is therefore covered too, and the assertion cannot silently stop
 * applying to a control it used to cover.
 *
 * The control's own 24px and the row's 44px are asserted separately because
 * they are two different requirements: 24 is the control (2.5.8), 44 is the
 * hit area around it (2.5.5), and the label is what supplies the second.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test checkboxTargets
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const BUILDINGS = {
  '/buildings/resources': { wood: 1200, clay: 1100, iron: 900, crop: 800, warehouse: 8000, granary: 8000 },
  '/buildings/queue': { village_id: CAPITAL, queue: [] },
  '/buildings': {
    village_id: CAPITAL,
    buildings: [{ slot_id: 1, name: 'Woodcutter', level: 3 }],
  },
}

/** Every non-table label that directly wraps a checkbox or radio, with the
 *  control's box and the label's own box. Boxes come from
 *  `getBoundingClientRect`, so a `min-height` that does not apply (an INLINE
 *  label -- `min-height` is ignored on a non-replaced inline box) shows up as
 *  the real measurement rather than as the declared one. */
async function checkRows(page) {
  return page.evaluate(() => {
    const rows = []
    for (const label of document.querySelectorAll('label')) {
      const control = label.querySelector(':scope > input[type="checkbox"], :scope > input[type="radio"]')
      if (!control) continue
      if (label.closest('table')) continue
      const lb = label.getBoundingClientRect()
      const cb = control.getBoundingClientRect()
      if (lb.width === 0 && lb.height === 0) continue
      rows.push({
        text: (label.textContent || '').trim().slice(0, 32) || control.type,
        display: getComputedStyle(label).display,
        label: { w: Math.round(lb.width), h: Math.round(lb.height) },
        control: { w: Math.round(cb.width), h: Math.round(cb.height) },
      })
    }
    return rows
  })
}

/** The census's nine pages, minus the three whose controls need a multi-step
 *  interaction to render (Connect's add-server form, FarmBuilder's scan step,
 *  FarmLists' loop panel). The fix is CSS, so a page proves the rule rather
 *  than only itself -- but every page listed here is swept whole, so a site
 *  with a differently-built label still shows up. */
const PAGES = [
  ['Logs', '/logs', {}],
  ['Military', '/military', {}],
  ['OasisRaider', '/oasis-raider', {}],
  ['AutoScout', '/scout', {}],
  ['Buildings', '/buildings', BUILDINGS],
  ['BuildQueue', '/queue', BUILDINGS],
]

test.describe('checkbox rows on a coarse pointer', () => {
  test.use({ viewport: { width: 375, height: 900 }, hasTouch: true, isMobile: true })

  test('the emulation really is a coarse pointer', async ({ page }) => {
    await isolateApp(page)
    await page.goto('/logs')
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
  })

  for (const [name, path, fixture] of PAGES) {
    test(`${name}: every non-table checkbox row is 44 tall with a 24px control`, async ({
      page,
    }) => {
      await isolateApp(page, fixture)
      await page.goto(path)
      // POLLED: `<main>` becomes visible a tick before the route's own body
      // mounts, so a single read right after it finds zero rows on every page.
      await expect
        .poll(async () => (await checkRows(page)).length, {
          message: `${name} rendered no non-table checkbox row`,
        })
        .toBeGreaterThan(0)

      const rows = await checkRows(page)

      const dump = rows
        .map((r) => `${r.text} [${r.display}] label ${r.label.w}x${r.label.h} control ${r.control.w}x${r.control.h}`)
        .join(' | ')

      for (const row of rows) {
        expect(row.control.w, `control width — ${dump}`).toBeGreaterThanOrEqual(24)
        expect(row.control.h, `control height — ${dump}`).toBeGreaterThanOrEqual(24)
        expect(row.label.h, `hit-area height — ${dump}`).toBeGreaterThanOrEqual(44)
      }
    })
  }
})
