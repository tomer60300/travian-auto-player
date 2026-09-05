/**
 * Reports: a paged read of the game's report inbox, plus a per-report detail
 * read that only happens when a row is opened.
 *
 * The filters are the interesting part of the request. `max_age_hours` and
 * `max_pages` decide how many pages of the game's inbox get fetched, and every
 * page is a real request against a real account -- so a filter that does not
 * travel is a filter that costs requests the operator thought they had capped.
 *
 * The interesting part of the response is that this page has THREE states that
 * used to look identical: never fetched, fetched-and-empty, and failed. The
 * failed one used to render nothing at all, not even the page's own empty
 * sentence.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know; the Raid Target Analyzer on this page is
 * collapsed and opens no socket unless asked. There is a live Travian account
 * on this machine.
 */

import { expect, test } from '@playwright/test'

import { isolateApp } from './appHarness'

function report(n, extra = {}) {
  return {
    report_id: `r${n}`,
    report_type: 'battle_raid',
    subject: `Raid on (${n}|0)`,
    date_str: `today, 0${(n % 9) + 1}:15`,
    is_read: n !== 1,
    ...extra,
  }
}

const REPORTS = [
  report(1, { report_type: 'scout', subject: 'Scouting (-37|142)' }),
  ...Array.from({ length: 34 }, (_, i) => report(i + 2)),
]

const DETAIL = {
  resources: { lumber: 1250, clay: 980, iron: 1100, crop: 400 },
  attacker: { player: 'e2e-operator', losses: 0 },
}

async function record(page) {
  const seen = []
  await page.route('**/api/**', async (route) => {
    seen.push(new URL(route.request().url()))
    await route.fallback()
  })
  return seen
}

function toast(page) {
  return page.locator('.toast').first()
}

test('the filters travel, the list is the server’s, and a row opens its own detail', async ({
  page,
}) => {
  await isolateApp(page, { '/reports': REPORTS, '/reports/r1': DETAIL })
  const seen = await record(page)

  await page.goto('/reports')
  // Nothing has been fetched yet, so the page claims nothing about the inbox.
  await expect(page.getByText('No reports found for the given filters.')).toHaveCount(0)

  // Neither filter label carries `htmlFor` nor wraps its box, so `getByLabel`
  // finds nothing; the two spinboxes are addressed inside their own card
  // instead. (The Raid Target Analyzer above has four more, but it is
  // collapsed and renders none of them until opened.)
  const filters = page.locator('div.card').filter({ hasText: 'Max Age (hours)' })
  await filters.locator('input[type=number]').nth(0).fill('6')
  await filters.locator('input[type=number]').nth(1).fill('2')
  await page.getByRole('button', { name: 'Fetch Reports' }).click()

  // 1. THE REQUEST. Both filters, as numbers, on the query string -- each
  //    extra page is a real fetch against the account.
  const url = () => seen.find((u) => u.pathname.endsWith('/reports'))
  await expect.poll(() => !!url()).toBe(true)
  expect(url().searchParams.get('max_age_hours')).toBe('6')
  expect(url().searchParams.get('max_pages')).toBe('2')

  // 2. THE PAGE renders the server's rows, its read/unread flags and its
  //    types -- 35 reports, capped at 30 until asked for the rest.
  await expect(toast(page)).toContainText('Fetched 35 report(s)')
  await expect(page.getByText('Scouting (-37|142)')).toBeVisible()
  await expect(page.getByRole('button', { name: /Scouting/ })).toContainText('Unread')
  await expect(page.getByRole('button', { name: /Raid on \(2\|0\)/ })).toContainText('Read')
  await expect(page.getByText('Raid on (35|0)')).toHaveCount(0)
  await page.getByRole('button', { name: 'Show all 35 reports' }).click()
  await expect(page.getByText('Raid on (35|0)')).toBeVisible()

  // The detail read is lazy: it happens on expand, not on fetch.
  expect(seen.filter((u) => /\/reports\/r1$/.test(u.pathname))).toHaveLength(0)
  const row = page.getByRole('button', { name: /Scouting/ })
  await row.click()
  await expect(row).toHaveAttribute('aria-expanded', 'true')
  await expect.poll(() => seen.filter((u) => /\/reports\/r1$/.test(u.pathname)).length).toBe(1)

  // The detail renders as structured fields, not a JSON dump.
  await expect(page.getByText('lumber', { exact: true })).toBeVisible()
  await expect(page.getByText('1,250')).toBeVisible()
  await expect(page.getByText('Attacker')).toBeVisible()

  // Collapsing and reopening reuses the cached detail: a second game read for
  // a report that has not changed is a wasted request.
  await row.click()
  await expect(row).toHaveAttribute('aria-expanded', 'false')
  await row.click()
  await expect(page.getByText('1,250')).toBeVisible()
  expect(seen.filter((u) => /\/reports\/r1$/.test(u.pathname))).toHaveLength(1)
})

test('a detail that failed to load says so, in the row that asked for it', async ({ page }) => {
  await isolateApp(page, {
    '/reports': [report(1, { report_type: 'scout', subject: 'Scouting (-37|142)' })],
    '/reports/r1': { status: 502, json: { detail: 'the report page did not parse' } },
  })

  await page.goto('/reports')
  await page.getByRole('button', { name: 'Fetch Reports' }).click()
  await page.getByRole('button', { name: /Scouting/ }).click()

  // 3. THE FAILURE BRANCH. A failed detail is stored as `{ error }`, which
  //    carries none of the structured keys the renderer looks for -- so
  //    without its own branch it falls through to a raw `JSON.stringify` dump
  //    and the operator is shown `{"error": "..."}` in a <pre>.
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Could not load this report')
  await expect(alert).toContainText('the report page did not parse')
  await expect(page.locator('pre')).toHaveCount(0)
})

test('never fetched, fetched-and-empty, and failed are three different screens', async ({
  page,
}) => {
  const state = { mode: 'broken' }
  await isolateApp(page)
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.endsWith('/reports')) return route.fallback()
    if (state.mode === 'broken') {
      return route.fulfill({ status: 500, json: { detail: 'the inbox would not load' } })
    }
    return route.fulfill({ json: [] })
  })

  await page.goto('/reports')

  // ── NEVER FETCHED ─────────────────────────────────────────────────
  await expect(page.getByRole('button', { name: 'Fetch Reports' })).toBeVisible()
  await expect(page.getByText('No reports found for the given filters.')).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0)

  // ── FAILED ────────────────────────────────────────────────────────
  await page.getByRole('button', { name: 'Fetch Reports' }).click()
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Could not fetch reports')
  await expect(alert).toContainText('the inbox would not load')
  await expect(alert.getByRole('button', { name: 'Retry' })).toBeVisible()
  // "No reports found for the given filters" would blame the operator's
  // filters for a failure that had nothing to do with them.
  await expect(page.getByText('No reports found for the given filters.')).toHaveCount(0)

  // ── FETCHED AND EMPTY, through the alert's own Retry ───────────────
  state.mode = 'empty'
  await alert.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByText('No reports found for the given filters.')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})
