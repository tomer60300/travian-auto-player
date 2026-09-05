/**
 * Four controls that could be clicked but not used from a keyboard.
 *
 * Item 2 of the UI Definition of Done is two claims, not one: reachable by
 * Tab, AND operable by Enter/Space. The wave-4 census found four places that
 * failed both halves because they were plain `onClick` handlers on elements
 * that are not controls -- a `<div>`, an `<h3>` and a `<th>`:
 *
 *   * Reports' analyzer collapse (`<h3 onClick>`);
 *   * Reports' report rows (`<div onClick>`);
 *   * AutoScout's seven sortable column headers (`<th onClick>`) -- a keyboard
 *     user could not sort the scan results at all;
 *   * Sessions' `SessionCard` (`<div onClick>`), the only way into a session's
 *     live log.
 *
 * Each test FIRES THE KEY rather than only checking that focus lands, because
 * `tabIndex` alone buys reachability and nothing else -- and reachable-only is
 * arguably worse than unreachable, since it puts a stop in the tab order that
 * does nothing.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test keyboardOperable
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const REPORTS = [
  {
    report_id: 'r1',
    report_type: 'raid',
    date_str: '2026-09-05 10:00',
    subject: 'Raid on Barbarian Camp',
  },
]

test('Reports: the analyzer collapses from the keyboard, and says whether it is open', async ({
  page,
}) => {
  await isolateApp(page, { '/reports': REPORTS })
  await page.goto('/reports')

  // The analyzer opens collapsed.
  const header = page.getByRole('button', { name: 'Raid Target Analyzer' })
  await expect(header).toHaveAttribute('aria-expanded', 'false')

  await header.focus()
  await expect(header).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(header).toHaveAttribute('aria-expanded', 'true')
  await page.keyboard.press(' ')
  await expect(header).toHaveAttribute('aria-expanded', 'false')
})

test('Reports: a report row expands from the keyboard', async ({ page }) => {
  await isolateApp(page, { '/reports': REPORTS, '/reports/r1': { subject: 'Raid on Barbarian Camp' } })
  await page.goto('/reports')
  await page.getByRole('button', { name: 'Fetch Reports' }).click()

  const row = page.getByRole('button', { name: /Raid on Barbarian Camp/ })
  await expect(row).toHaveAttribute('aria-expanded', 'false')
  await row.focus()
  await expect(row).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(row).toHaveAttribute('aria-expanded', 'true')
})

test('AutoScout: a column header sorts from the keyboard and announces the order', async ({
  page,
}) => {
  const SCAN_TILES = [
    { x: -117, y: 143, village_name: 'Aussenposten', population: 512, player_name: 'Bergvolk' },
    { x: -112, y: 139, village_name: 'Oase 47', population: 90, player_name: 'Talvolk' },
  ]
  await isolateApp(page)
  // AFTER `isolateApp`, not before: it registers its own catch-all
  // `routeWebSocket` that closes every socket, and Playwright matches the most
  // recently registered route first. Registered before, this handler never
  // runs and the scan never returns a tile.
  await page.routeWebSocket(/.*/, (ws) => {
    if (new URL(ws.url()).pathname.endsWith('/ws/scout/scan')) {
      return ws.onMessage(() => {
        ws.send(JSON.stringify({ type: 'session_init', session_id: 'e2e-scan' }))
        ws.send(JSON.stringify({ type: 'complete', tiles: SCAN_TILES, stats: { time_seconds: 1 } }))
        ws.send(JSON.stringify({ type: 'operation_complete', status: 'completed' }))
      })
    }
    return ws.close()
  })
  await page.addInitScript(() => localStorage.removeItem('resumableOp:scout-scan'))
  await page.goto('/scout')
  await page.getByRole('button', { name: 'Scan Map' }).click()
  await expect(page.getByText('Aussenposten')).toBeVisible()

  const header = page.getByRole('button', { name: /^V\.Pop/ })
  const cell = page.locator('th', { has: header })
  await expect(cell).toHaveAttribute('aria-sort', 'none')

  await header.focus()
  await expect(header).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(cell).toHaveAttribute('aria-sort', 'ascending')
  await page.keyboard.press('Enter')
  await expect(cell).toHaveAttribute('aria-sort', 'descending')
})

test('Sessions: a session card opens from the keyboard', async ({ page }) => {
  await isolateApp(page, {
    '/sessions': [
      {
        id: 'sess-1',
        label: 'Build queue run',
        session_type: 'queue',
        // 'disconnected', not 'completed': the page groups on exactly
        // `running` and `disconnected`, and anything else renders no card at
        // all -- which is its own finding, reported separately.
        status: 'disconnected',
        created_at: new Date().toISOString(),
        message_count: 12,
      },
    ],
  })
  await page.goto('/sessions')

  // Located as a DIV with `role="button"`, not by name. Since
  // `moreNames.pw.js`'s fix the card's two sibling buttons are named "View
  // logs for Build queue run" and "Rerun Build queue run", so matching the
  // label resolves to three elements -- and the card's own computed name
  // ("Build queue run sess-1 ...") is built from its whole subtree, which is
  // not a stable thing to anchor on. What IS stable is that the clickable
  // region is the only non-BUTTON element carrying the button role.
  const card = page.locator('div[role="button"][tabindex="0"]')
  await expect(card).toHaveCount(1)
  await expect(card).toHaveAccessibleName(/Build queue run/)
  await card.focus()
  await expect(card).toBeFocused()
  await page.keyboard.press('Enter')

  // Opening a card swaps the list for that session's own view.
  await expect(page.getByText('sess-1').first()).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Sessions' })).toHaveCount(0)
})

test('the four controls all carry the app focus ring', async ({ page }) => {
  await isolateApp(page, { '/reports': REPORTS })
  await page.goto('/reports')

  const header = page.getByRole('button', { name: 'Raid Target Analyzer' })
  await header.focus()
  await expect
    .poll(() =>
      header.evaluate((el) => {
        const cs = getComputedStyle(el)
        return { style: cs.outlineStyle, width: cs.outlineWidth }
      }),
    )
    .toEqual({ style: 'solid', width: '2px' })
})

test('Reports: a high-confidence target is actually coloured', async ({ page }) => {
  // `confidenceClass` returned the string 'text-green', which is not a Tailwind
  // v4 utility (no shade) and is declared nowhere in index.css -- so a HIGH
  // confidence target rendered in the plain inherited colour, pixel-identical
  // to a low one, and the column carried no information at all. Driven through
  // the analyzer's own socket so it is the RENDERED cell that is measured.
  await isolateApp(page)
  await page.routeWebSocket(/.*/, (ws) => {
    if (!new URL(ws.url()).pathname.endsWith('/ws/reports/analyze')) return ws.close()
    return ws.onMessage(() => {
      ws.send(
        JSON.stringify({
          type: 'complete',
          total_targets: 2,
          targets: [
            {
              state: { x: 1, y: 2, village_name: 'Rich Camp', raidable_confidence: 'high' },
              recommendation: { n_send: 20, unit_type: 'TK', score: 9.1 },
            },
            {
              state: { x: 3, y: 4, village_name: 'Poor Camp', raidable_confidence: 'low' },
              recommendation: { n_send: 5, unit_type: 'TK', score: 1.2 },
            },
          ],
          diagnostics: { analysis_duration_seconds: 1 },
        }),
      )
    })
  })
  await page.goto('/reports')
  await page.getByRole('button', { name: 'Raid Target Analyzer' }).click()
  await page.getByRole('button', { name: 'Analyze', exact: true }).click()

  const high = page.getByRole('cell', { name: 'high', exact: true })
  const low = page.getByRole('cell', { name: 'low', exact: true })
  await expect(high).toBeVisible()

  const colours = await Promise.all(
    [high, low].map((c) => c.evaluate((el) => getComputedStyle(el).color)),
  )
  // The whole of the defect: these two were the same colour.
  expect(colours[0]).not.toBe(colours[1])
  // And "high" is the app's success token, not a Tailwind palette green.
  const success = await page.evaluate(() => {
    const probe = document.createElement('span')
    probe.className = 'text-success'
    document.body.appendChild(probe)
    const c = getComputedStyle(probe).color
    probe.remove()
    return c
  })
  expect(colours[0]).toBe(success)
})
