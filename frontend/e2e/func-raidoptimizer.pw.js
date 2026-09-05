/**
 * Raid Composition Optimizer: a calculator whose inputs it fills in for the
 * operator from the game, and whose output four ranked deployments an operator
 * then sends real troops on.
 *
 * That auto-fill is why this page is worth a functional test rather than a
 * label check. The five troop counts START at fabricated defaults (1,000
 * clubs, 1,000 TKs). When the read succeeds the numbers on screen are the
 * account's; when it fails, they are the page's own invention -- and the
 * optimizer must NOT rank four confident strategies off them. So: does the
 * read name the village, do the returned unit ids land in the right boxes,
 * does a failed read stop the computation and say so, and does typing a real
 * count start it again.
 *
 * NO BACKEND AND NO GAME REQUEST: `appHarness.isolateApp` answers the shell and
 * ABORTS every path it does not know. There is a live Travian account on this
 * machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

// Travian unit ids as `/military/troops` returns them. The optimizer's five
// Teuton slots are t1 club, t2 spear, t3 axe, t5 paladin, t6 TK -- note the
// GAP at t4 (the scout), which a naive positional mapping would get wrong.
//
// The counts are deliberately tiny. `findCompositions`
// (src/utils/raidOptimizer.js) runs five nested loops bounded by `capRange`,
// so its cost is the PRODUCT of the five bounds: 40/8/25/4/12 is ~600k
// iterations and lands in about a second. A realistic stockpile is billions --
// see the last test in this file.
const TROOPS = { t1: 40, t2: 8, t3: 25, t4: 90, t5: 4, t6: 12 }
const SMITHY = { found: true, research: { t1: 3, t2: 0, t3: 7, t5: 1, t6: 12 } }

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

test('the auto-fill lands each unit id in its own box, and the verdict follows the inputs', async ({
  page,
}) => {
  await isolateApp(page, { '/military/troops': TROOPS, '/military/smithy': SMITHY })
  const seen = await record(page)

  await page.goto('/raid-optimizer')

  // 1. THE REQUESTS name the village. Troop counts are per-village, so an
  //    unqualified read designs a force out of another village's army.
  await expect.poll(() => seen.some((u) => u.pathname.endsWith('/military/troops'))).toBe(true)
  for (const path of ['/military/troops', '/military/smithy']) {
    const url = seen.find((u) => u.pathname.endsWith(path))
    expect(url, `${path} was requested`).toBeTruthy()
    expect(url.searchParams.get('village_id')).toBe(String(CAPITAL))
  }

  // 2. THE PAGE puts each id in the right box. t4 is the scout and belongs in
  //    none of them; t5 and t6 must not slide up into its place.
  await expect(page.getByLabel('Clubswingers')).toHaveValue('40')
  await expect(page.getByLabel('Spearmen')).toHaveValue('8')
  await expect(page.getByLabel('Axemen')).toHaveValue('25')
  await expect(page.getByLabel('Paladins')).toHaveValue('4')
  await expect(page.getByLabel('TKs')).toHaveValue('12')
  // t4 is the scout -- 90 of them -- and no box may have taken it.
  for (const label of ['Clubswingers', 'Spearmen', 'Axemen', 'Paladins', 'TKs']) {
    await expect(page.getByLabel(label)).not.toHaveValue('90')
  }
  await expect(page.getByLabel('Club Smithy')).toHaveValue('3')
  await expect(page.getByLabel('Axe Smithy')).toHaveValue('7')
  await expect(page.getByLabel('TK Smithy')).toHaveValue('12')

  // The strategies are computed off them, and the page says so.
  await expect(page.getByText('4 strategies', { exact: false }).first()).toBeVisible()
  await expect(page.getByText('Nothing to optimise yet')).toHaveCount(0)

  // The account is Roman, and this page's arithmetic is Teuton-only. That is a
  // warning about the ANSWER, not about the form, so it must be on screen.
  await expect(page.getByText(/hardcoded for Teuton units/)).toBeVisible()

  // A re-read is the operator asking again, so it speaks up -- and says which
  // half it got, because "no smithy built" changes every attack figure below.
  await page.getByRole('button', { name: 'Auto-fill troops + smithy' }).click()
  await expect(toast(page)).toHaveClass(/toast-success/)
  await expect(toast(page)).toContainText('Loaded current troop counts + smithy levels')

  // 3. A CONTRADICTORY CONSTRAINT is named rather than silently producing an
  //    empty table: budget DEF below zero-casualty DEF cannot be satisfied by
  //    any composition, and the page must say which two numbers disagree.
  await page.getByLabel('Budget DEF').fill('100')
  await expect(page.getByText(/Budget DEF \(100\) must be ≥ Zero-Cas DEF \(330\)/)).toBeVisible()
  await expect(page.getByText('No valid composition found')).toBeVisible()
})

test('a failed troop read stops the optimizer instead of ranking its own defaults', async ({
  page,
}) => {
  await isolateApp(page, {
    '/military/troops': { status: 502, json: { detail: 'the rally point would not parse' } },
    '/military/smithy': SMITHY,
  })

  await page.goto('/raid-optimizer')

  // 3. THE FAILURE BRANCH, and the sharpest one in the app: the boxes are NOT
  //    blank on this page, they hold 1,000 clubs and 1,000 TKs that nobody
  //    supplied. Ranking four deployments off them is the page inventing an
  //    army.
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Troop counts unread')
  await expect(alert).toContainText('the numbers below are placeholders, not your army')
  await expect(alert).toContainText('the rally point would not parse')
  await expect(page.getByText('Nothing to optimise yet')).toBeVisible()
  await expect(page.getByText(/^No strategies computed\./)).toBeVisible()

  // The defaults are visible but explicitly disowned -- and nothing was ranked
  // from them.
  await expect(page.getByLabel('Clubswingers')).toHaveValue('1000')
  await expect(page.getByText('4 strategies')).toHaveCount(0)

  // Typing a count is the operator vouching for it, so the refusal lifts and
  // the optimizer runs on numbers a human actually stands behind.
  await page.getByLabel('Clubswingers').fill('640')
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByText('Nothing to optimise yet')).toHaveCount(0)
  await expect(page.getByText('4 strategies', { exact: false }).first()).toBeVisible()
})

test('a real Teuton army does not lock the page up', async ({ page }) => {
  // Bounded on purpose: measured on this machine the page never recovers, so
  // without a cap this one test would sit for the whole suite's budget.
  test.setTimeout(25_000)

  // An ordinary mid-game Teuton stockpile, and the shape this page is FOR.
  // Every one of the five slots is non-zero, which is what makes it expensive:
  // `findCompositions` phase 1 is five nested loops whose bounds come from
  // `capRange`, and the work is their product --
  //   c 1500 -> 150,  sp 200 -> 20,  a 800 -> 150,  pa 60 -> 40,  t 400 -> 130
  // = about 2.5 BILLION iterations, run synchronously inside a `useMemo`
  // during render. The default inventory (1,000 clubs and 1,000 TKs, three
  // slots at zero) is 150 x 1 x 1 x 1 x 130 = ~20k, which is why the page
  // feels instant until the auto-fill lands a real army in it.
  const BIG = { t1: 1500, t2: 200, t3: 800, t4: 90, t5: 60, t6: 400 }
  await isolateApp(page, { '/military/troops': BIG, '/military/smithy': SMITHY })

  await page.goto('/raid-optimizer')

  // The auto-fill happens on mount with no button pressed, so this is what an
  // operator gets by NAVIGATING to the page. Measured 2026-09-05 on this
  // machine: 160/30/120/12/60 -- a small early-game force -- took 38 SECONDS
  // to paint, and 1500/200/800/60/400 was still empty after 170 seconds with
  // the tab unresponsive throughout. Ten seconds is already far past "slow".
  await expect(page.getByLabel('Clubswingers')).toHaveValue('1500', { timeout: 8_000 })
})
