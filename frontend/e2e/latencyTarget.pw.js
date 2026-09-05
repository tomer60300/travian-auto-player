/**
 * What actually bound the cycles, reported instead of inferred.
 *
 * `PlanResponse.latency_target_hours` and `SegmentPlanResponse` /
 * `DayCheckResponse.segments` (`src/travian_api/web/routes/distribution.py`)
 * landed in 4d17ae9 and were corrected in b40e242 to carry what BOUND the
 * routes rather than what was asked for. Before them the number reached the
 * client only inside finding PROSE -- "... against a 2h target", rounded to
 * whole hours -- and the page had no entitlement to parse that.
 *
 * It matters because the page stopped sending `max_latency_hours` on every path
 * (see `segmentedLatency.pw.js`), so the figure is now entirely the server's:
 * the standing 2 h default, clamped by whatever window the request or segment
 * carries. A window may only TIGHTEN it. So a Night profile planned with
 * shorter cycles than the Day is not necessarily a different decision -- it may
 * be the same target against eight hours instead of sixteen -- and nothing on
 * the page said which.
 *
 * `null` is the case with a wrong answer available. It is not zero and it is
 * not the 2.0 default: section 6 SUSPENDS the target for the hours nobody is
 * waiting through, so the latency pass never runs and no cycle was shortened to
 * meet one. Rendered as "0 h" it would read as the tightest target there is,
 * and defaulted to 2.0 it would be a number the plan never used.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test latencyTarget
 */

import { expect, test } from '@playwright/test'

import { PLAN, isolate, seed } from './plannerHarness'

async function openPlanWith(page, hours) {
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/plan')) return undefined
    await route.fulfill({ json: { ...PLAN, latency_target_hours: hours } })
    return 'handled'
  })
  await seed(page)
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

/** The stat card the figure lives in, so "2 h" is asked of the figure rather
 *  than of any sentence on the page that happens to contain those characters. */
function statCard(page) {
  return page.locator('.card').filter({ hasText: 'Latency target used' })
}

test.describe('the plan sheet says what bound its routes', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the standing 2 h default', async ({ page }) => {
    await openPlanWith(page, 2.0)

    await expect(page.getByText('Latency target used', { exact: true })).toBeVisible()
    // "2 h", not "2.0 h": the wire carries a float and the operator reads hours.
    await expect(statCard(page).getByText('2 h', { exact: true })).toBeVisible()
  })

  test('a window that tightened it', async ({ page }) => {
    // `min(the standing target, the window's own length)`. A one-hour window
    // clamps the 2 h default to 1 h, and the routes were planned against that.
    await openPlanWith(page, 1.0)

    await expect(statCard(page).getByText('1 h', { exact: true })).toBeVisible()
  })

  test('null is the night, and is neither 0 h nor the default', async ({ page }) => {
    await openPlanWith(page, null)

    await expect(statCard(page).getByText('0 h', { exact: true })).toHaveCount(0)
    await expect(statCard(page).getByText('2 h', { exact: true })).toHaveCount(0)
    await expect(page.getByText(/suspended overnight by the night rules/i)).toBeVisible()
  })

  test('the Why says it is the server’s and not a control', async ({ page }) => {
    await openPlanWith(page, 2.0)

    const why = page.getByLabel('Why: Latency target used')
    await expect(why).toHaveCount(1)
    await why.click()
    await expect(page.getByText(/never loosen/i)).toBeVisible()
    await expect(page.getByText(/not an operator control/i)).toBeVisible()
  })

  test.describe('at 375px on a coarse pointer', () => {
    test.use({ viewport: { width: 375, height: 900 }, hasTouch: true, isMobile: true })

    test('its Why is a 44px target and the card does not scroll sideways', async ({ page }) => {
      await openPlanWith(page, null)

      const why = page.getByLabel('Why: Latency target used')
      const box = await why.boundingBox()
      expect(Math.round(box.width)).toBeGreaterThanOrEqual(44)
      expect(Math.round(box.height)).toBeGreaterThanOrEqual(44)

      await why.click()
      const scrollX = await page.evaluate(
        () => document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth
      )
      expect(scrollX, 'no horizontal page scroll').toBe(0)
    })
  })
})
