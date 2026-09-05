/**
 * Military: the two buttons on this page each send real troops out of a real
 * village, so what matters is (a) that the request carries the numbers the
 * operator typed, (b) that the result box says what the game said, and (c)
 * that a refusal is not dressed as a dispatch.
 *
 * The client-side guards are driven in the same pass as the dispatches, before
 * them, because they are the cheap half of the same promise: a coordinate
 * outside the map or a raid with no troops must cost ZERO requests, not one the
 * server then refuses. "Fewer requests beats saving the operator's typing" is
 * this repo's standing rule for anything aimed at the game.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know. There is a live Travian account on this
 * machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const RESOURCES = { lumber: 1000, clay: 1000, iron: 1000, crop: 1000 }

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
    sent.push({ method: req.method(), path: new URL(req.url()).pathname, body })
    await route.fallback()
  })
  return sent
}

function toast(page) {
  return page.locator('.toast').first()
}

const panel = (page, name) =>
  page.locator('div.card').filter({ has: page.getByRole('heading', { name, exact: true }) })

test('scouts and a raid leave carrying exactly what was typed, and the result is the game’s', async ({
  page,
}) => {
  await isolateApp(page, {
    '/buildings/resources': RESOURCES,
    '/military/scout': { travel_time: '00:42:11', message: '3 scouts on their way' },
    '/military/raid': { travel_time: '01:07:00', message: 'raid dispatched' },
  })
  const sent = await record(page)

  await page.goto('/military')

  // ── WHAT THE PAGE REFUSES ITSELF ─────────────────────────
  // Off-map coordinates: the map is 401 tiles across, so ±400 is the outer
  // edge. This must cost NO request -- the form alone decides it, and a
  // request that can only be refused still spends the account's budget.
  const scout = panel(page, 'Scout')
  await scout.getByPlaceholder('X').fill('900')
  await scout.getByPlaceholder('Y').fill('0')
  await page.getByRole('button', { name: 'Send Scouts' }).click()
  await expect(toast(page)).toHaveClass(/toast-error/)
  await expect(toast(page)).toContainText('Coordinates must be between -400 and 400')
  // No dialog either, so there is nothing for a habitual Enter to confirm.
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(sent.filter((s) => s.method === 'POST')).toHaveLength(0)

  // ── SCOUT ─────────────────────────────────────────────────────────
  await scout.getByPlaceholder('X').fill('-37')
  await scout.getByPlaceholder('Y').fill('142')
  await scout.getByPlaceholder('1').fill('3')
  await scout.getByRole('radio', { name: 'Defenses' }).check()
  await page.getByRole('button', { name: 'Send Scouts' }).click()

  // The dialog restates the dispatch in the operator's own numbers -- the last
  // point at which a typo is free.
  await expect(page.getByRole('dialog')).toContainText('Send 3 scout(s) to (-37, 142) for defenses?')
  expect(sent.filter((s) => s.method === 'POST')).toHaveLength(0)
  await page.getByRole('dialog').getByRole('button', { name: 'Send', exact: true }).click()

  // 1. THE REQUEST. `village_id` matters as much as the coordinates: the
  //    backend's session default is forever the login village, so a scout that
  //    omits it leaves from a village the operator is not looking at.
  const scoutPost = () => sent.find((s) => s.path.endsWith('/military/scout'))
  await expect.poll(() => !!scoutPost()).toBe(true)
  expect(scoutPost().method).toBe('POST')
  expect(scoutPost().body).toEqual({
    x: -37,
    y: 142,
    amount: 3,
    type: 'defenses',
    village_id: CAPITAL,
  })

  // 2. THE PAGE AFTERWARDS repeats the game's own travel time and message,
  //    which is the only evidence the troops are actually moving.
  const scoutResult = scout.locator('.result-box')
  await expect(scoutResult).toHaveClass(/result-box-success/)
  await expect(scoutResult).toContainText('Scouts dispatched!')
  await expect(scoutResult).toContainText('00:42:11')
  await expect(scoutResult).toContainText('3 scouts on their way')

  // ── RAID ──────────────────────────────────────────────────────────
  const raid = panel(page, 'Raid')
  await raid.getByPlaceholder('X').fill('12')
  await raid.getByPlaceholder('Y').fill('-8')
  // A raid with coordinates but no troops is this page's other own-refusal,
  // and it too must cost nothing.
  await page.getByRole('button', { name: 'Send Raid' }).click()
  // Addressed by its own words rather than as "the first toast": the scout
  // half above left one of its own, and toasts live for four seconds.
  await expect(
    page.locator('.toast', { hasText: 'Please enter at least one troop type' })
  ).toBeVisible()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await raid.getByRole('checkbox', { name: 'Show all' }).check()
  // Romans (tribe 1): the first box is Legionnaire = t1, the third Imperian = t3.
  await raid.locator('input.input-troop').nth(0).fill('120')
  await raid.locator('input.input-troop').nth(2).fill('40')
  await expect(raid.getByText('(160 total)')).toBeVisible()
  await page.getByRole('button', { name: 'Send Raid' }).click()

  await expect(page.getByRole('dialog')).toContainText('Send 160 troops to raid (12, -8)?')
  await page.getByRole('dialog').getByRole('button', { name: 'Send Raid' }).click()

  const raidPost = () => sent.find((s) => s.path.endsWith('/military/raid'))
  await expect.poll(() => !!raidPost()).toBe(true)
  // Only the units that were given a count, keyed the way the API keys them.
  // A zero that travels as `t2: 0` is a different request from one that does
  // not travel at all.
  expect(raidPost().body).toEqual({
    x: 12,
    y: -8,
    troops: { t1: 120, t3: 40 },
    village_id: CAPITAL,
  })

  const raidResult = raid.locator('.result-box')
  await expect(raidResult).toHaveClass(/result-box-success/)
  await expect(raidResult).toContainText('Raid dispatched!')
  await expect(raidResult).toContainText('01:07:00')
})

test('a refused dispatch is shown as a refusal, never as troops on their way', async ({ page }) => {
  const REASON = 'Not enough troops in this village'
  await isolateApp(page, {
    '/buildings/resources': RESOURCES,
    '/military/raid': { status: 400, json: { detail: REASON } },
  })

  await page.goto('/military')
  const raid = panel(page, 'Raid')
  await raid.getByPlaceholder('X').fill('12')
  await raid.getByPlaceholder('Y').fill('-8')
  await raid.getByRole('checkbox', { name: 'Show all' }).check()
  await raid.locator('input.input-troop').nth(0).fill('5000')
  await page.getByRole('button', { name: 'Send Raid' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Send Raid' }).click()

  // 3. THE FAILURE BRANCH. The danger surface, the server's sentence, and no
  //    "Raid dispatched!" anywhere -- an operator who reads that will not
  //    resend, and the troops are still sitting at home.
  const result = raid.locator('.result-box')
  await expect(result).toHaveClass(/result-box-danger/)
  await expect(result).toContainText(REASON)
  await expect(page.getByText('Raid dispatched!')).toHaveCount(0)
  await expect(toast(page)).toHaveClass(/toast-error/)
})
