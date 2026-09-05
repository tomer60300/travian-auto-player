/**
 * Buildings: the page that spends a village's resources. An upgrade or a
 * construction started here is irreversible in the game, so the three things
 * worth testing are that the request names the slot and the village the
 * operator picked, that the page's "started!" is the server's word and not the
 * click's, and that a failed read of the village never reads as an empty one.
 *
 * `allow_gold: false` is part of the intent, not boilerplate: the page warns
 * that a busy queue means an upgrade "may use gold", and the flag is what
 * stops the app from spending it.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know. There is a live Travian account on this
 * machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const BUILDINGS = [
  { slot_id: 1, name: 'Woodcutter', level: 7 },
  { slot_id: 19, name: 'Main Building', level: 5 },
  { slot_id: 25, name: 'Empty', level: 0 },
]

const RESOURCES = { lumber: 50_000, clay: 50_000, iron: 50_000, crop: 50_000 }

const MAIN_BUILDING_DETAIL = {
  slot_id: 19,
  name: 'Main Building',
  level: 5,
  upgrade_cost: { lumber: 240, clay: 135, iron: 205, crop: 70 },
}

const EMPTY_SLOT_DETAIL = {
  slot_id: 25,
  name: 'Empty',
  level: 0,
  available_buildings: ['Marketplace', 'Granary'],
}

async function record(page) {
  const sent = []
  await page.route('**/api/**', async (route) => {
    const req = route.request()
    let body = null
    try {
      body = req.postDataJSON()
    } catch {
      body = req.postData() ?? null
    }
    sent.push({ method: req.method(), url: new URL(req.url()), body })
    await route.fallback()
  })
  return sent
}

function toast(page) {
  return page.locator('.toast').first()
}

/** Serves the two per-slot details this file uses, and lets a test refuse the
 *  write endpoints. */
async function serveSlots(page, hooks = {}) {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/buildings/19')) return route.fulfill({ json: MAIN_BUILDING_DETAIL })
    if (path.endsWith('/buildings/25')) return route.fulfill({ json: EMPTY_SLOT_DETAIL })
    if (path.endsWith('/buildings/upgrade') || path.endsWith('/buildings/construct')) {
      if (hooks.refuse) return route.fulfill({ status: 400, json: { detail: hooks.refuse } })
      return route.fulfill({ json: { ok: true } })
    }
    return route.fallback()
  })
}

test('an upgrade and a construction each name their slot, their village, and no gold', async ({
  page,
}) => {
  await isolateApp(page, {
    '/buildings': { village_id: CAPITAL, buildings: BUILDINGS },
    '/buildings/queue': { village_id: CAPITAL, queue: [] },
    '/buildings/resources': RESOURCES,
  })
  // Read on every call, so the refusal at the end of this test can be turned
  // on without a second page load.
  const hooks = {}
  await serveSlots(page, hooks)
  const sent = await record(page)

  await page.goto('/buildings')
  await expect(page.getByRole('button', { name: /#19/ })).toBeVisible()

  // ── UPGRADE ───────────────────────────────────────────────────────
  await page.getByRole('button', { name: /#19/ }).click()

  // The detail read must name the village: the same slot number exists in
  // every village, so an unqualified read describes the login village.
  const detail = () => sent.find((s) => s.url.pathname.endsWith('/buildings/19'))
  await expect.poll(() => !!detail()).toBe(true)
  expect(detail().url.searchParams.get('village_id')).toBe(String(CAPITAL))

  // 2. The panel shows what came back -- the level and the cost the operator
  //    is about to spend.
  await expect(page.getByRole('heading', { name: 'Slot 19 - Main Building' })).toBeVisible()
  await expect(page.getByText('Current Level: 5')).toBeVisible()
  // The cost card, read as a whole: each figure sits beside an emoji, so a
  // bare text lookup would match the row and its wrappers alike.
  await expect(page.locator('div.border-gold')).toContainText('240')

  await page.getByRole('button', { name: 'Upgrade to Level 6' }).click()
  await expect(page.getByRole('dialog')).toContainText(
    'Upgrade building in slot 19 to the next level?'
  )
  expect(sent.filter((s) => s.method === 'POST')).toHaveLength(0)
  await page.getByRole('dialog').getByRole('button', { name: 'Upgrade' }).click()

  // 1. THE REQUEST. `allow_gold: false` is the difference between a free
  //    upgrade and one that quietly spends the account's gold to jump the
  //    queue -- the page itself warns about it, so it must never be true here.
  const upgrade = () => sent.find((s) => s.url.pathname.endsWith('/buildings/upgrade'))
  await expect.poll(() => !!upgrade()).toBe(true)
  expect(upgrade().body).toEqual({ slot_id: 19, allow_gold: false, village_id: CAPITAL })
  await expect(toast(page)).toHaveClass(/toast-success/)
  await expect(toast(page)).toContainText('Upgrade started!')

  // ── CONSTRUCT ─────────────────────────────────────────────────────
  await page.getByRole('button', { name: /#25/ }).click()
  await expect(page.getByText('This slot is empty. Choose a building to construct:')).toBeVisible()
  // The choices are the server's, not a hardcoded list: what may be built in a
  // slot depends on the village.
  await expect(page.getByRole('option', { name: 'Marketplace' })).toBeAttached()
  await page.locator('div.border-gold').getByRole('combobox').selectOption('Granary')
  await page.getByRole('button', { name: 'Construct' }).click()
  await expect(page.getByRole('dialog')).toContainText('Construct Granary in slot 25?')
  await page.getByRole('dialog').getByRole('button', { name: 'Construct' }).click()

  const construct = () => sent.find((s) => s.url.pathname.endsWith('/buildings/construct'))
  await expect.poll(() => !!construct()).toBe(true)
  expect(construct().body).toEqual({
    slot_id: 25,
    building_name: 'Granary',
    village_id: CAPITAL,
  })
  await expect(toast(page)).toContainText('Construction started!')

  // -- AND THE SAME UPGRADE, REFUSED --------------------------------
  // 3. THE FAILURE BRANCH: the game's own sentence, in the error tone, and no
  //    "Upgrade started!" -- an operator who reads that walks away from a
  //    village that is still doing nothing.
  const REASON = 'not enough resources: 240 lumber short'
  hooks.refuse = REASON
  await page.getByRole('button', { name: /#19/ }).click()
  await page.getByRole('button', { name: 'Upgrade to Level 6' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Upgrade' }).click()
  await expect(toast(page)).toHaveClass(/toast-error/)
  await expect(toast(page)).toContainText(REASON)
  await expect(page.getByText('Upgrade started!')).toHaveCount(0)
})

test('a village that could not be read is named as such, in both halves of the page', async ({
  page,
}) => {
  const state = { broken: true }
  await isolateApp(page, { '/buildings/resources': RESOURCES })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const isRead = path.endsWith('/buildings') || path.endsWith('/buildings/queue')
    if (!isRead) return route.fallback()
    if (state.broken) return route.fulfill({ status: 503, json: { detail: 'the game is in maintenance' } })
    if (path.endsWith('/buildings/queue')) return route.fulfill({ json: { queue: [] } })
    return route.fulfill({ json: { village_id: CAPITAL, buildings: [] } })
  })

  await page.goto('/buildings')

  // ── BOTH READS FAILED ─────────────────────────────────────────────
  // The queue panel returns null for an empty queue, so a failed read used to
  // take the whole section -- heading and all -- off the page with nothing
  // saying it had been attempted.
  const queueAlert = page.getByRole('alert')
  await expect(queueAlert).toContainText('Could not read the construction queue')
  await expect(queueAlert).toContainText('the game is in maintenance')
  await expect(page.getByRole('heading', { name: /Construction Queue/ })).toBeVisible()

  // The slot list states its own failure with its own retry, and does not fall
  // through to "make sure you are connected" -- we are connected.
  await expect(page.getByText('the game is in maintenance').last()).toBeVisible()
  await expect(page.getByText('No building data available. Make sure you are connected.')).toHaveCount(0)

  // ── A VILLAGE WITH NOTHING IN IT ──────────────────────────────────
  // Two independent reads, so two independent retries: the queue panel's own
  // button first, then the slot list's, which is all that is left once the
  // queue comes back empty and the panel returns null again.
  state.broken = false
  await page.getByRole('alert').getByRole('button', { name: 'Retry' }).click()
  await page.getByRole('button', { name: 'Retry' }).click()
  await expect(
    page.getByText('No building data available. Make sure you are connected.')
  ).toBeVisible()
  // The queue panel is back to returning nothing at all, which is its empty
  // state, and no alert is left over.
  await expect(page.getByRole('alert')).toHaveCount(0)
})
