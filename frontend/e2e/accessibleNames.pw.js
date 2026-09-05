/**
 * Accessible-name fixes on BuildQueue, FarmLists, and AutoScout.
 *
 * A live accessible-name census (Chromium AX tree, 375/768/1440) found a
 * batch of controls with no name at all -- bare checkboxes, a bulk-priority
 * `<select>`, icon-only "+"/"×" buttons whose `title` loses to their text
 * content, and `<label>`s that were never linked to their control -- plus one
 * page-specific duplicate: AutoScout embedded its own `<VillageSelector/>`
 * next to the "build: wd12" marker, which shares the SAME store action and
 * the SAME "Active village" name as the layout's sidebar/mobile-bar one, so
 * `getByLabel('Active village')` resolved 2 elements on that page alone.
 *
 * Each test below drives just enough of its page to render the flagged
 * control, then asserts it resolves by `getByLabel`/`getByRole(name)`
 * UNIQUELY -- the actual regression, not merely "has some name". The
 * AutoScout duplicate is asserted the same way: a single "Active village"
 * match once its own embedded selector is gone (the layout's own compact
 * vs. sidebar pair never both count -- only one is visible per breakpoint's
 * CSS, and Playwright's accessible-name locators skip hidden elements).
 *
 * NO BACKEND AND NO GAME REQUEST: `route.fulfill` answers the exact
 * `/api/**` calls each page needs to render its rows, `route.abort` closes
 * everything else, and the one WS this spec needs (AutoScout's map scan) is
 * played back locally via `page.routeWebSocket` -- see `autoScoutRun.pw.js`
 * for the same two fail-closed mechanisms. There is a live Travian account
 * on this machine.
 */

import { expect, test } from '@playwright/test'

const PLAYER = 'e2e-operator'
const SERVER = 'https://ts2.x1.europe.travian.com'
const CAPITAL = 20002

/** `/users/me` + `/travian/status`, answered the same way on every page;
 * `extra` maps a path SUFFIX to the JSON body for that page's own reads.
 * Anything unrecognised is aborted, never reaching a real server. */
async function isolateApi(page, extra = {}) {
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
          villages: [{ id: CAPITAL, name: '02', x: 0, y: 0 }],
        },
      })
    }
    for (const [suffix, json] of Object.entries(extra)) {
      if (path.endsWith(suffix)) return route.fulfill({ json })
    }
    return route.abort('blockedbyclient')
  })
}

test('BuildQueue: add/select/remove controls each resolve to one element', async ({ page }) => {
  await isolateApi(page, {
    '/buildings': [
      { slot_id: 1, name: 'Woodcutter', level: 3 },
      { slot_id: 19, name: 'Barracks', level: 1 },
    ],
    '/buildings/queue': { queue: [] },
  })
  // Belt-and-braces: BuildQueue opens no socket without a stored session_id
  // (which this test never sets), but close anything that tries anyway.
  await page.routeWebSocket(/.*/, (ws) => ws.close())
  await page.addInitScript(() => localStorage.setItem('token', 'e2e-not-a-real-token'))

  await page.goto('/queue')

  // "+" add-to-queue button: named for its own building + slot, not just "+".
  const addWoodcutter = page.getByRole('button', { name: 'Add Woodcutter (slot #1) to queue' })
  await expect(addWoodcutter).toHaveCount(1)
  await addWoodcutter.click()

  // Per-row queue-item checkbox: named for the row it selects.
  const rowCheckbox = page.getByRole('checkbox', { name: 'Select Woodcutter (slot #1)' })
  await expect(rowCheckbox).toHaveCount(1)
  await rowCheckbox.check()

  // Selecting a row reveals the bulk-priority bar -- its `<select>` had no
  // name at all before this fix.
  await expect(page.getByRole('combobox', { name: 'Bulk priority' })).toHaveCount(1)

  // "×" remove button: named for the row it removes, not just "×".
  await expect(page.getByRole('button', { name: 'Remove Woodcutter (slot #1) from queue' })).toHaveCount(1)
})

test('FarmLists: village select, slot table, and Interval/Duration/X/Y inputs each resolve to one element', async ({ page }) => {
  await isolateApi(page, {
    '/farm/lists': [{ id: 1, name: 'List A', slots_amount: 1, active_slots: 1, total_booty: 100 }],
    '/farm/lists/1': {
      id: 1,
      name: 'List A',
      slots: [
        {
          id: 501, x: 5, y: -3, name: 'Barbarian Camp',
          population: 120, distance: 3.2, is_active: true,
          troops: {}, total_booty: 500, total_raids: 2, last_raid: null,
        },
      ],
    },
  })
  await page.routeWebSocket(/.*/, (ws) => ws.close())
  await page.addInitScript(() => localStorage.setItem('token', 'e2e-not-a-real-token'))

  await page.goto('/farm')

  // Create-list "Village" <select>: its visible <label> was never linked.
  // `exact: true` matters here -- a substring match on "Village" also picks
  // up the layout's "Active village" selector (getByLabel is case-insensitive
  // substring matching by default).
  await expect(page.getByLabel('Village', { exact: true })).toHaveCount(1)

  // `getByText('List A')` is ambiguous -- the loop-mode chip below repeats
  // the list name -- so target the overview table's row specifically.
  await page.getByRole('cell', { name: 'List A' }).click()
  await expect(page.getByText('Barbarian Camp')).toBeVisible()

  // Slot table select-all + per-row checkbox: neither had a name.
  await expect(page.getByRole('checkbox', { name: 'Select all targets' })).toHaveCount(1)
  await expect(page.getByRole('checkbox', { name: 'Select target (5, -3)' })).toHaveCount(1)

  // "Del" button: named for the target it deletes, not just "Del".
  await expect(page.getByRole('button', { name: 'Delete target (5, -3)' })).toHaveCount(1)

  // Add-target X/Y inputs: both were unlabeled, both named "0" by their
  // shared placeholder before this fix. `exact: true` matters -- a
  // substring match on the single letter "X" also picks up the filter
  // bar's "Max dist" input (aria-label counts as a label for getByLabel).
  await expect(page.getByLabel('X', { exact: true })).toHaveCount(1)
  await expect(page.getByLabel('Y', { exact: true })).toHaveCount(1)

  // Loop-mode Interval/Duration inputs: unlinked <label>s.
  await expect(page.getByLabel('Interval (seconds)')).toHaveCount(1)
  await expect(page.getByLabel('Duration (min, 0=forever)')).toHaveCount(1)
})

test('AutoScout: config labels, bonus buttons, scan-result controls, and a single Active village all resolve to one element', async ({ page }) => {
  const SCAN_TILES = [
    { x: -117, y: 143, village_name: 'Rheinbund-Aussenposten', population: 512, player_name: 'Bergvolk' },
    { x: -112, y: 139, village_name: 'Oase 47', population: 0, player_name: '' },
  ]

  await page.routeWebSocket(/.*/, (ws) => {
    const path = new URL(ws.url()).pathname
    if (path.endsWith('/ws/scout/scan')) {
      return ws.onMessage(() => {
        ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-scan' }))
        ws.send(JSON.stringify({ type: 'complete', tiles: SCAN_TILES, stats: { time_seconds: 1 } }))
        ws.send(JSON.stringify({ type: 'operation_complete', status: 'completed' }))
      })
    }
    return ws.close()
  })
  await isolateApi(page)
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-not-a-real-token')
    // A stored session from an earlier run would skip straight to the
    // results the "Scan Map" click below is meant to produce.
    localStorage.removeItem('resumableOp:scout-scan')
  })

  await page.goto('/scout')

  // Radius slider: the visible "Radius: {n}" label was never linked.
  await expect(page.getByLabel(/^Radius: \d+$/)).toHaveCount(1)
  // Min/Max Village Pop + Max Player Pop: unlinked labels (the last one was
  // named "no limit" from its own placeholder before this fix).
  await expect(page.getByLabel('Min Village Pop')).toHaveCount(1)
  await expect(page.getByLabel('Max Village Pop')).toHaveCount(1)
  await expect(page.getByLabel('Max Player Pop (all villages)')).toHaveCount(1)
  // 25/50/75/100 toggle buttons: named "25%" etc alone before this fix, with
  // no tie back to "Total bonus level".
  await expect(page.getByRole('button', { name: 'Total bonus level 25%' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Total bonus level 100%' })).toHaveCount(1)
  // Alliance/Player "Add" buttons: both named just "Add" before this fix.
  await expect(page.getByRole('button', { name: 'Add alliance' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Add player' })).toHaveCount(1)
  // The page used to embed a second <VillageSelector/> duplicating the
  // layout's; this must resolve to exactly one VISIBLE match now. The
  // layout itself renders two (mobile top-bar + desktop sidebar), one per
  // breakpoint, always hiding the other via CSS -- `getByLabel` matches DOM
  // nodes regardless of visibility, unlike the real accessibility tree a
  // screen reader (or the census) sees, so the `:visible` filter is what
  // makes this assertion mean what the census meant.
  await expect(page.locator('[aria-label="Active village"]:visible')).toHaveCount(1)

  await page.getByRole('button', { name: 'Scan Map' }).click()
  await expect(page.getByText('Rheinbund-Aussenposten')).toBeVisible()

  // Scan-results select-all + per-row checkbox: neither had a name.
  await expect(page.getByRole('checkbox', { name: 'Select all scan results' })).toHaveCount(1)
  await expect(page.getByRole('checkbox', { name: 'Select Rheinbund-Aussenposten (-117, 143)' })).toHaveCount(1)
  // "+Farm" button: named for the target it adds, not just "+Farm".
  await expect(page.getByRole('button', { name: 'Add Rheinbund-Aussenposten (-117, 143) to farm list' })).toHaveCount(1)
  // "Scouts per target": unlinked label.
  await expect(page.getByLabel('Scouts per target')).toHaveCount(1)
})
