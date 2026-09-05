/**
 * Cumulative Layout Shift, item 6 of the UI Definition of Done ("no layout
 * shift as data arrives -- reserve the space, or skeleton it").
 *
 * The census measured CLS over 0.1 at 768px on three pages and only at that
 * width: Dashboard 0.113-0.119, BuildQueue 0.107, FarmLists 0.106. 768 is where
 * the sidebar appears (200px) while the content column is still narrow, so a
 * card that is one line tall at 1440 wraps to two or three here -- and a
 * placeholder sized for the 1440 case then under-reserves by exactly that
 * difference.
 *
 * Measured the way the census measured it: a `PerformanceObserver` for
 * `layout-shift`, installed via `addInitScript` so it is running BEFORE the
 * document exists, summing `value` for entries that are not
 * `hadRecentInput`. That is the CLS definition, minus the session-window
 * capping that only matters for pages that shift repeatedly over minutes;
 * these shift once, as the first fetch lands, so the sum IS the window.
 *
 * The budget is asserted at all three widths, not just the failing one: a fix
 * that reserves space for 768 by over-reserving at 375 has moved the defect
 * rather than removed it.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test layoutStability
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const BUDGET = 0.1

const QUEUE = {
  village_id: CAPITAL,
  queue: [
    { event_id: 1, building_name: 'Woodcutter', target_level: 4, remaining_seconds: 900 },
    { event_id: 2, building_name: 'Main Building', target_level: 7, remaining_seconds: 3600 },
  ],
}

const FIXTURES = {
  '/buildings/resources': { wood: 1234, clay: 2345, iron: 3456, crop: 4567, warehouse: 8000, granary: 8000 },
  '/buildings/queue': QUEUE,
  '/buildings': {
    village_id: CAPITAL,
    buildings: Array.from({ length: 18 }, (_, i) => ({
      slot_id: i + 1,
      name: ['Woodcutter', 'Clay Pit', 'Iron Mine', 'Cropland'][i % 4],
      level: (i % 9) + 1,
    })),
  },
  '/farm/lists': Array.from({ length: 6 }, (_, i) => ({
    id: i + 1,
    name: `List ${i + 1}`,
    slots_amount: 10,
    active_slots: 7,
    total_booty: 1000 * (i + 1),
  })),
}

async function openWithCls(page, path) {
  await page.addInitScript(() => {
    window.__cls = 0
    window.__shifts = []
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (entry.hadRecentInput) continue
        window.__cls += entry.value
        window.__shifts.push(entry.value)
      }
    }).observe({ type: 'layout-shift', buffered: true })
  })
  await isolateApp(page, FIXTURES)
  // WARM FIRST, then measure on a second navigation. This runs against the
  // Vite DEV server, which transforms a route's modules on first request: that
  // latency varies by hundreds of milliseconds run to run, and it decides
  // whether the fetch lands before or after the skeleton paints. Measured
  // cold, BuildQueue came back 0.00, 0.13 and 0.52 on three consecutive runs
  // of the same fixture -- a property of the dev server, not of the page. The
  // second `goto` re-runs the init script (so `__cls` resets to zero) with
  // every module already transformed and cached.
  await page.goto(path)
  await page.waitForTimeout(600)
  await page.goto(path)
}

async function readCls(page) {
  // Two animation frames past the last fetch, so a shift caused by the swap
  // has been recorded before it is read.
  await page.waitForTimeout(1200)
  return page.evaluate(() => ({ cls: window.__cls, shifts: window.__shifts }))
}

const PAGES = [
  ['Dashboard', '/', 'Dashboard'],
  ['BuildQueue', '/queue', 'Build Queue'],
  ['FarmLists', '/farm', 'Farm Lists'],
]

for (const width of [375, 768, 1440]) {
  test.describe(`at ${width}`, () => {
    test.use({ viewport: { width, height: 900 } })

    for (const [name, path, heading] of PAGES) {
      test(`${name}: CLS stays inside the 0.1 budget`, async ({ page }) => {
        await openWithCls(page, path)
        await expect(page.getByRole('heading', { name: heading, level: 2 })).toBeVisible()

        const { cls, shifts } = await readCls(page)
        expect(cls, `${name} @${width}: ${shifts.map((s) => s.toFixed(4)).join(', ')}`).toBeLessThan(
          BUDGET,
        )
      })
    }
  })
}
