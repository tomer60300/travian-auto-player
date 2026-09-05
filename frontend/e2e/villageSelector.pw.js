/**
 * Exactly one "Active village" selector per page, at every width.
 *
 * `Layout.jsx` mounts `VillageSelector` TWICE on purpose -- once in the mobile
 * top bar and once in the sidebar -- and CSS keeps exactly one of them visible
 * per breakpoint, which is fine and is what the `:visible` filter below is for.
 * Five pages ALSO rendered a THIRD, always-visible copy in their own body:
 * Dashboard, Buildings, Military, OasisRaider and RaidOptimizer. The census
 * confirmed it live on all five via the real accessibility tree: two `combobox`
 * nodes, both named exactly "Active village", simultaneously visible.
 *
 * They drive the same store action, so the two disagree about nothing -- the
 * cost is a screen-reader user meeting the same control twice under the same
 * name, and an extra tab stop in the page body. AutoScout had the identical
 * defect and had its own copy removed (5a62dd1); the fix was never applied to
 * its five siblings.
 *
 * Asserted at all three DoD widths, because the layout's own pair is
 * breakpoint-switched and a naive "count the DOM nodes" check would be wrong at
 * every width for the wrong reason. `getByLabel` matches hidden nodes too --
 * unlike the accessibility tree a screen reader (or the census) sees -- so the
 * `:visible` filter is what makes this mean what the census meant.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test villageSelector
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const FIXTURES = {
  '/buildings/resources': { wood: 1200, clay: 1100, iron: 900, crop: 800, warehouse: 8000, granary: 8000 },
  '/buildings/queue': { village_id: CAPITAL, queue: [] },
  '/buildings': { village_id: CAPITAL, buildings: [{ slot_id: 1, name: 'Woodcutter', level: 3 }] },
  '/military/troops': { t1: 1000, t6: 1000 },
  '/military/smithy': { found: true, research: { t1: 5, t6: 14 } },
}

// The six pages that mount `Layout`: the five that carried a duplicate, plus
// AutoScout, which is the precedent and must stay fixed.
const PAGES = [
  ['Dashboard', '/', 'Dashboard'],
  ['Buildings', '/buildings', 'Buildings'],
  ['Military', '/military', 'Military'],
  ['OasisRaider', '/oasis-raider', 'Oasis Raider'],
  ['RaidOptimizer', '/raid-optimizer', 'Raid Composition Optimizer'],
  ['AutoScout', '/scout', 'Auto Scout'],
]

const WIDTHS = [375, 768, 1440]

for (const width of WIDTHS) {
  test.describe(`at ${width}`, () => {
    test.use({ viewport: { width, height: 900 } })

    for (const [name, path, heading] of PAGES) {
      test(`${name}: one visible "Active village" selector`, async ({ page }) => {
        await isolateApp(page, FIXTURES)
        await page.goto(path)
        // The page's OWN heading, not `<main>`: `<main>` is visible a tick
        // before the route's body mounts, and in that tick only the layout's
        // selector exists -- so a count assertion made there passes for the
        // wrong reason, whatever the page renders a moment later.
        await expect(page.getByRole('heading', { name: heading, level: 2 })).toBeVisible()
        await expect(page.locator('[aria-label="Active village"]:visible')).toHaveCount(1)
      })
    }
  })
}
