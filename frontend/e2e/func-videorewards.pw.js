/**
 * Video Rewards: six cards that each spend a real ad view on a real village,
 * plus a "Claim All" that fans out over them.
 *
 * The interesting question here is not whether the buttons exist -- it is
 * whether the page's report of a claim matches the server's. Each card keeps
 * its own result box, and "Claim All" folds a per-type result list into one
 * summary line; a summary that reads green over six failures is the page
 * lying about the account, which is the failure mode this file is pointed at.
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

const card = (page, label) => page.locator('div.card').filter({ hasText: label })

test('a single claim names the reward and the village, and reports the game’s answer', async ({
  page,
}) => {
  await isolateApp(page, {
    '/buildings/resources': RESOURCES,
    '/video/claim': { message: '+750 lumber credited' },
  })
  const sent = await record(page)

  await page.goto('/video')
  await card(page, 'Lumber Bonus').getByRole('button', { name: 'Claim', exact: true }).click()

  // 1. THE REQUEST. `type` is which of the six ads this is, and `village_id`
  //    is where the resources land -- the backend's session default is forever
  //    the login village, so an omitted id credits the wrong one.
  const post = () => sent.find((s) => s.path.endsWith('/video/claim'))
  await expect.poll(() => !!post()).toBe(true)
  expect(post().method).toBe('POST')
  expect(post().body).toEqual({ type: 'lumberProductionBonus', village_id: CAPITAL })

  // 2. THE PAGE AFTERWARDS carries the server's sentence, on that card only.
  const result = card(page, 'Lumber Bonus').locator('.result-box')
  await expect(result).toHaveClass(/result-box-success/)
  await expect(result).toContainText('+750 lumber credited')
  await expect(card(page, 'Clay Bonus').locator('.result-box')).toHaveCount(0)
  await expect(toast(page)).toHaveClass(/toast-success/)
})

test('a refused claim is red on its own card and green nowhere', async ({ page }) => {
  const REASON = 'no video reward is available right now'
  await isolateApp(page, {
    '/buildings/resources': RESOURCES,
    '/video/claim': { status: 409, json: { detail: REASON } },
  })

  await page.goto('/video')
  await card(page, 'Iron Bonus').getByRole('button', { name: 'Claim', exact: true }).click()

  const result = card(page, 'Iron Bonus').locator('.result-box')
  await expect(result).toHaveClass(/result-box-danger/)
  await expect(result).toContainText(REASON)
  await expect(toast(page)).toHaveClass(/toast-error/)
})

test('Claim All reports each type as the server reported it', async ({ page }) => {
  await isolateApp(page, {
    '/buildings/resources': RESOURCES,
    '/video/claim-all': {
      results: [
        { reward_type: 'lumberProductionBonus', success: true, message: '+750 lumber' },
        { reward_type: 'clayProductionBonus', success: false, error: 'already claimed today' },
      ],
    },
  })
  const sent = await record(page)

  await page.goto('/video')
  await page.getByRole('button', { name: 'Claim All' }).click()

  const post = () => sent.find((s) => s.path.endsWith('/video/claim-all'))
  await expect.poll(() => !!post()).toBe(true)
  expect(post().body).toEqual({ village_id: CAPITAL })

  // Per-type results land on the matching cards, each in its own tone -- one
  // request, two different truths, and the page must not flatten them.
  const lumber = card(page, 'Lumber Bonus').locator('.result-box')
  await expect(lumber).toHaveClass(/result-box-success/)
  await expect(lumber).toContainText('+750 lumber')
  const clay = card(page, 'Clay Bonus').locator('.result-box')
  await expect(clay).toHaveClass(/result-box-danger/)
  await expect(clay).toContainText('already claimed today')

  await expect(page.getByText('Completed: 1 succeeded, 1 failed')).toBeVisible()
})

test('a Claim All where every claim failed is not a success', async ({ page }) => {
  const TYPES = [
    'buildingUpgrade',
    'productionBoost',
    'lumberProductionBonus',
    'clayProductionBonus',
    'ironProductionBonus',
    'cropProductionBonus',
  ]
  await isolateApp(page, {
    '/buildings/resources': RESOURCES,
    '/video/claim-all': {
      results: TYPES.map((t) => ({ reward_type: t, success: false, error: 'no ad available' })),
    },
  })

  await page.goto('/video')
  await page.getByRole('button', { name: 'Claim All' }).click()

  // Every card says the truth...
  await expect(card(page, 'Crop Bonus').locator('.result-box')).toHaveClass(/result-box-danger/)

  // ...and the headline above them contradicts it. `handleClaimAll`
  // (src/pages/VideoRewards.jsx) sets `claimAllResult = { success: true, ... }`
  // for ANY response carrying a `results` object, so the summary box renders
  // `result-box-success` and the toast is `toast.success('Claim all
  // completed!')` even when `successCount` is 0. "Completed" is being used to
  // mean "the request returned", which is not what a green box says to a
  // reader.
  //
  // What it should do: derive the tone from `successCount` -- danger at zero,
  // warning on a partial, success only when nothing failed.
  // Filtered by its own sentence: while the request is in flight the same card
  // also holds an `result-box-info` progress line, and two matches would be a
  // strict-mode error rather than an answer.
  const summary = page
    .locator('div.card')
    .filter({ hasText: 'Claim All Production Boosts' })
    .locator('.result-box')
    .filter({ hasText: 'Completed:' })
  await expect(summary).toContainText('Completed: 0 succeeded, 6 failed')
  await expect(summary).not.toHaveClass(/result-box-success/, { timeout: 2000 })
  await expect(toast(page)).not.toHaveClass(/toast-success/, { timeout: 2000 })
})
