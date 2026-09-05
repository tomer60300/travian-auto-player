/**
 * Activity Log is the only page here that talks to no endpoint of its own: it
 * renders what `src/api.js`'s interceptors put in the log store as the rest of
 * the app works. So a functional test of it has to CAUSE traffic and then read
 * the page -- which is why this file starts on Farm Lists with a broken
 * endpoint and walks to /logs through the sidebar (a full page load would
 * empty the in-memory store).
 *
 * Two things are being asked. First, does the log actually record what
 * happened on the wire, including the failure, at the right level and source.
 * Second -- and this is a contract `src/api.js` documents at length -- does it
 * record only the SHAPE of a request, never its content: the page renders
 * every entry's `detail` and its Export button writes the lot to a file, so a
 * refusal sentence naming villages or an account key must not reach it.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know. There is a live Travian account on this
 * machine.
 */

import { expect, test } from '@playwright/test'

import { isolateApp } from './appHarness'

// A refusal sentence of the shape this API really produces: it names a
// village. `api.js` replaces it with a length, deliberately.
const REFUSAL = 'no role template was sent for 01 Kayhut Capital (role def)'

async function arrive(page) {
  await isolateApp(page, {
    '/farm/lists': { status: 422, json: { detail: REFUSAL } },
  })
  await page.goto('/farm')
  // The failing read has happened; walk to the log INSIDE the SPA, because the
  // store is in-memory and a reload would clear it.
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('link', { name: /Activity Log/ }).click()
  await expect(page.getByRole('heading', { name: 'Activity Log' })).toBeVisible()
}

test('the log records the failed call at error level, by shape and not by content', async ({
  page,
}) => {
  await arrive(page)

  // 2. THE PAGE reflects what actually went over the wire: the request line,
  //    the failing response line, its `error` level badge, and the source
  //    bucket `logSource` derives from the path. Asserted on the panel as a
  //    whole because StrictMode double-invokes the mount effect, so there are
  //    two of each line and a per-line lookup would be a strict-mode error.
  const panel = page.locator('.ws-panel')
  await expect(panel).toContainText('>> GET /farm/lists')
  await expect(panel).toContainText('<< GET /farm/lists 422')
  await expect(panel).toContainText('error')
  await expect(panel).toContainText('Farm')

  // The privacy contract, which is a functional property and not a style one:
  // the operator still reads the server's sentence -- Farm Lists put it in the
  // alert on the previous page -- but the log, which Export writes to a file,
  // keeps only its LENGTH.
  await expect(panel).toContainText(`${REFUSAL.length}-char refusal, on screen only`)
  await expect(panel).not.toContainText('Kayhut')

  // The counter is the two numbers the filters move between.
  await expect(page.getByText(/^\d+ \/ \d+ entries$/)).toBeVisible()
})

test('filtered-to-nothing and genuinely empty are different sentences', async ({ page }) => {
  await arrive(page)

  // A filter that matches nothing must say it is the FILTER that emptied the
  // panel -- "No activity yet. Navigate the app to see logs." would send the
  // operator off to reproduce a problem the log is already holding.
  await page.locator('#log-filter-source').selectOption('video')
  await expect(page.getByText('No entries match your filters.')).toBeVisible()
  await expect(page.getByText('No activity yet. Navigate the app to see logs.')).toHaveCount(0)
  await expect(page.getByText(/^0 \/ [1-9]\d* entries$/)).toBeVisible()

  // Widening it again brings the same entries back -- nothing was destroyed.
  await page.locator('#log-filter-source').selectOption('all')
  const panel = page.locator('.ws-panel')
  await expect(panel).toContainText('>> GET /farm/lists')

  // Narrowing by LEVEL is the other axis, and the failing call is the only
  // entry at `error`.
  await page.locator('#log-filter-level').selectOption('error')
  await expect(panel).not.toContainText('>> GET /farm/lists')
  await expect(panel).toContainText('<< GET /farm/lists 422')

  // Clear All is the destructive one, and only then is the other sentence
  // true.
  await page.locator('#log-filter-level').selectOption('all')
  await page.getByRole('button', { name: 'Clear All' }).click()
  await expect(page.getByText('No activity yet. Navigate the app to see logs.')).toBeVisible()
  await expect(page.getByText('No entries match your filters.')).toHaveCount(0)
  await expect(page.getByText('0 / 0 entries')).toBeVisible()
})
