/**
 * Dashboard is a read-only page, so "does it do what the user intends" reduces
 * to two things: it must ask for the RIGHT village, and it must never state
 * something about the account that it did not actually read.
 *
 * Both have teeth here. Village switching is client-side only on this app, so
 * a read that omits `village_id` is answered for the login village and the
 * numbers on screen belong to somewhere else. And the queue card's empty
 * sentence -- "No active operations" -- is the sentence a failed read used to
 * produce too, which is the difference between an idle account and an
 * unreachable one.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know. There is a live Travian account on this
 * machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, PLAYER, SERVER, isolateApp } from './appHarness'

const RESOURCES = {
  lumber: 123_456,
  clay: 40_000,
  iron: 40_000,
  crop: 9_000,
  max_lumber: 400_000,
  max_clay: 400_000,
  max_iron: 400_000,
  max_crop: 400_000,
  lumber_per_hour: 8_372,
  clay_per_hour: 5_168,
  iron_per_hour: 5_809,
  crop_per_hour: -1_200,
}

const QUEUE = [
  { event_id: 1, building_name: 'Main Building', target_level: 12, remaining_seconds: 3_723 },
]

async function record(page) {
  const seen = []
  await page.route('**/api/**', async (route) => {
    seen.push(new URL(route.request().url()))
    await route.fallback()
  })
  return seen
}

test('the dashboard reads the active village and prints what came back', async ({ page }) => {
  await isolateApp(page, {
    '/buildings/resources': RESOURCES,
    '/buildings/queue': { village_id: CAPITAL, queue: QUEUE },
    '/buildings': { village_id: CAPITAL, buildings: [] },
  })
  const seen = await record(page)

  await page.goto('/')

  // 1. THE REQUESTS carry the village. Switching villages never reaches the
  //    backend on this app, so a read without `village_id` is answered for the
  //    login village -- the numbers would be real, and from the wrong place.
  await expect
    .poll(() => seen.filter((u) => u.pathname.endsWith('/buildings/resources')).length)
    .toBeGreaterThan(0)
  for (const path of ['/buildings/resources', '/buildings/queue', '/buildings']) {
    const url = seen.find((u) => u.pathname.endsWith(path))
    expect(url, `${path} was requested`).toBeTruthy()
    expect(url.searchParams.get('village_id'), `${path} names the village`).toBe(String(CAPITAL))
  }

  // 2. THE PAGE reflects the answers. Player info comes from the status read
  //    -- scoped to its own card, because the sidebar prints the server and
  //    the player name too.
  const playerCard = page
    .locator('div.card')
    .filter({ has: page.getByRole('heading', { name: 'Player Info' }) })
  await expect(playerCard).toContainText(SERVER)
  await expect(playerCard).toContainText(PLAYER)
  await expect(playerCard).toContainText('Romans')
  await expect(playerCard).toContainText('(0|0)')

  // ...the bars from the resource read, including the sign of the net crop
  // rate, which is what separates a growing village from a starving one.
  await expect(page.getByRole('progressbar', { name: 'Lumber' })).toHaveAttribute(
    'aria-valuenow',
    '123456'
  )
  await expect(page.getByText('123,456 / 400,000')).toBeVisible()
  await expect(page.getByText('Net Crop: -1,200/h')).toBeVisible()
  await expect(page.getByText('— starving')).toBeVisible()

  // ...and the queue card from the queue read, counted down from the figure
  // the server gave (3,723s = 01:02:03).
  await expect(page.getByText('Main Building → Level 12')).toBeVisible()
  // The clock and the "Done at" stamp share one element, so this is a
  // contains-match on the row rather than an exact text lookup.
  await expect(page.locator('.surface-row')).toContainText(/01:0[12]:\d\d/)
  await expect(page.getByText('No active operations. Start something from the actions below.')).toHaveCount(0)

  // A quick action is a link in button's clothing; it must actually go there.
  // Scoped to its own card: the sidebar carries the same words as a nav link.
  await page
    .locator('div.card')
    .filter({ has: page.getByRole('heading', { name: 'Quick Actions' }) })
    .getByRole('button', { name: /Build Queue/ })
    .click()
  await expect(page).toHaveURL(/\/queue$/)
})

test('a read that failed and a village with nothing happening do not look alike', async ({
  page,
}) => {
  const state = { broken: true }
  await isolateApp(page, { '/buildings': { village_id: CAPITAL, buildings: [] } })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const isRead = path.endsWith('/buildings/resources') || path.endsWith('/buildings/queue')
    if (!isRead) return route.fallback()
    if (state.broken) return route.fulfill({ status: 500, json: { detail: 'the game timed out' } })
    if (path.endsWith('/buildings/queue')) return route.fulfill({ json: { queue: [] } })
    return route.fulfill({ json: RESOURCES })
  })

  await page.goto('/')

  // ── BOTH READS FAILED ─────────────────────────────────────────────
  // Two separate named failures, each with its own retry: `resources: null`
  // and `constructionQueue: []` are byte-for-byte what a healthy idle village
  // looks like, so silence here is indistinguishable from calm.
  const resourceAlert = page.getByRole('alert').filter({ hasText: "resources" })
  await expect(resourceAlert).toContainText("Could not read this village's resources")
  await expect(resourceAlert).toContainText('the game timed out')
  const queueAlert = page.getByRole('alert').filter({ hasText: 'construction queue' })
  await expect(queueAlert).toContainText('Could not read the construction queue')
  await expect(page.getByText('No active operations. Start something from the actions below.')).toHaveCount(0)
  // And no bar is drawn from numbers nobody supplied.
  await expect(page.getByRole('progressbar', { name: 'Lumber' })).toHaveCount(0)

  // ── THE IDLE VILLAGE, through the retry the alert offers ──────────
  state.broken = false
  await queueAlert.getByRole('button', { name: 'Retry' }).click()
  await resourceAlert.getByRole('button', { name: 'Retry' }).click()

  await expect(
    page.getByText('No active operations. Start something from the actions below.')
  ).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByRole('progressbar', { name: 'Lumber' })).toBeVisible()
})
