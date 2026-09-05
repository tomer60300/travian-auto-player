/**
 * The accessible names the wave-4 census found still missing.
 *
 * `accessibleNames.pw.js` fixed an earlier batch on BuildQueue, FarmLists and
 * AutoScout. The wave-4 census, driving the real Chromium accessibility tree at
 * 375/768/1440, found more -- controls with no name at all, and controls whose
 * name is real but identical to three others on the same screen.
 *
 * Both kinds are asserted the same way: the control resolves to EXACTLY ONE
 * element by its name. That is the regression. "Has some name" would pass for
 * six buttons all called "Logs".
 *
 * `title` is not a name where there is text content: the accessible-name
 * algorithm takes the content first, which is why the reorder arrows read as
 * "↑" and "↓" despite carrying `title="Move up"`.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test moreNames
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

test('Buildings: the detail panel close button names the slot it closes', async ({ page }) => {
  await isolateApp(page, {
    '/buildings/queue': { village_id: CAPITAL, queue: [] },
    '/buildings': { village_id: CAPITAL, buildings: [{ slot_id: 19, name: 'Barracks', level: 1 }] },
    '/buildings/19': { slot_id: 19, name: 'Barracks', level: 1 },
  })
  await page.goto('/buildings')
  await page.getByRole('button', { name: /Barracks/ }).click()

  await expect(page.getByRole('button', { name: 'Close slot 19 details' })).toHaveCount(1)
})

test('BuildQueue: the reorder arrows name the row they move', async ({ page }) => {
  await isolateApp(page, {
    '/buildings/queue': { village_id: CAPITAL, queue: [] },
    '/buildings': {
      village_id: CAPITAL,
      buildings: [
        { slot_id: 1, name: 'Woodcutter', level: 3 },
        { slot_id: 19, name: 'Barracks', level: 1 },
      ],
    },
  })
  await page.goto('/queue')
  await page.getByRole('button', { name: 'Add Woodcutter (slot #1) to queue' }).click()
  await page.getByRole('button', { name: 'Add Barracks (slot #19) to queue' }).click()

  // Two rows, so `title`-only names would have collided into two pairs of
  // identical buttons.
  await expect(page.getByRole('button', { name: 'Move Barracks (slot #19) earlier' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Move Woodcutter (slot #1) later' })).toHaveCount(1)
})

test('Sessions: each card names the session its Logs and Rerun act on', async ({ page }) => {
  const session = (id, label) => ({
    id,
    label,
    session_type: 'queue',
    status: 'disconnected',
    created_at: new Date().toISOString(),
    message_count: 4,
  })
  await isolateApp(page, {
    '/sessions': [session('s1', 'Build queue run'), session('s2', 'Farm loop run')],
  })
  await page.goto('/sessions')

  await expect(page.getByRole('button', { name: 'View logs for Build queue run' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Rerun Farm loop run' })).toHaveCount(1)
  // The bare names are gone, which is the half that made a six-session list
  // unusable from a screen reader.
  await expect(page.getByRole('button', { name: 'Logs', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Rerun', exact: true })).toHaveCount(0)
})

test('AutoScout: the two Clears and the exclusion chips are distinguishable', async ({ page }) => {
  await isolateApp(page, {
    '/recon/status': { configured: true, manageable: true, source: 'stored', username: 'scout-acct' },
  })
  await page.goto('/scout')

  await expect(page.getByRole('button', { name: 'Clear the saved background account' })).toHaveCount(1)

  // The bonus filter's Clear only exists once the filter is non-default.
  await page.getByRole('button', { name: 'Total bonus level 25%' }).click()
  await expect(page.getByRole('button', { name: 'Clear the bonus filter' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Clear', exact: true })).toHaveCount(0)

  // An exclusion chip's remove button used to be named "x", once per chip.
  await page.getByPlaceholder('Alliance name or ID').fill('WW')
  await page.getByRole('button', { name: 'Add alliance' }).click()
  await expect(page.getByRole('button', { name: 'Stop excluding alliance WW' })).toHaveCount(1)
})

test('OasisRaider: the troop picker and its remove button are named', async ({ page }) => {
  await isolateApp(page)
  await page.goto('/oasis-raider')

  await expect(page.getByRole('combobox', { name: 'Troop type for row 1' })).toHaveCount(1)

  // A second row makes the remove buttons appear, and they used to be two
  // buttons both named "x". The names carry the ROW as well as the troop type,
  // because two rows can hold the same type -- naming by type alone would have
  // replaced one collision with another.
  await page.getByRole('button', { name: /Add troop/i }).click()
  const removes = page.getByRole('button', { name: /^Remove row \d+ .* from the composition$/ })
  const names = await removes.evaluateAll((els) => els.map((el) => el.getAttribute('aria-label')))
  expect(names.length, 'a remove button per row').toBeGreaterThanOrEqual(2)
  // ALL DISTINCT is the assertion, not the count: the defect was N buttons
  // sharing one name.
  expect(new Set(names).size, `remove buttons: ${names}`).toBe(names.length)
})

test('Logs: the four filter controls are reachable by their visible label', async ({ page }) => {
  await isolateApp(page)
  await page.goto('/logs')

  await expect(page.getByLabel('Source:')).toHaveCount(1)
  await expect(page.getByLabel('Level:')).toHaveCount(1)
  await expect(page.getByLabel('Origin:')).toHaveCount(1)
  await expect(page.getByLabel('Search:')).toHaveCount(1)
})
