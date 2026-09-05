/**
 * The one column the page outlined and then agreed to send.
 *
 * `relayTierProblemsByVillage` is the backend's six refusals on a declared
 * relay tier, computed live and rendered on the relay's own cell in
 * `text-danger` with `aria-describedby`. `planBlockers` did not take `relayFor`
 * at all, so none of them reached the gate — while the module's own docstring
 * says "Every figure listed here is one the page ALREADY outlines and names in
 * the cell". This was the one that was outlined and not listed.
 *
 * Measured before the fix, with 11 given role `def` and declared as the relay
 * for 02 — backend rule 3, profile section 5.9:
 *
 *   a relay problem is rendered on the cell: true
 *   Save disabled = false
 *   Build plan   -> /plan requests sent = 1   (422: role villages may not relay)
 *   Save setup   -> PUTs sent = 1             (422, same sentence)
 *
 * `Save setup to file` wrote the same document, and `parseSetup` refuses it on
 * the way back in ("11 is a relay in this file but its role is def") — the exact
 * "saves with a 200 and can never be loaded again" failure `setupSaveGate` was
 * built to stop, reached through a column the gate did not read.
 *
 * NO BACKEND AND NO GAME REQUEST: every call is counted and aborted, and the
 * point of each case is that the count stays at zero.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test relayTierGate
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, DEF_A, PLAN, isolate, seed } from './plannerHarness'

/** 11 is DEF and is declared as the relay for 02: a role village forwarding
 *  someone else's cargo, which profile section 5.9 forbids. */
const DEF_RELAY = {
  planner_village_roles: { [DEF_A]: 'def' },
  planner_role_templates: { def: { allocations: {}, consumption: { lumber: 8372 } } },
  planner_relay_for: { [DEF_A]: [CAPITAL] },
}

/** Everything this page could write, counted. */
async function countWrites(page) {
  const calls = { plan: 0, puts: 0 }
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      calls.plan += 1
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    if (path.endsWith('/distribution/setup') && route.request().method() === 'PUT') {
      calls.puts += 1
      await route.fulfill({ json: { saved_at: new Date().toISOString() } })
      return 'handled'
    }
    return undefined
  })
  return calls
}

async function openAccount(page, extra) {
  await seed(page, extra)
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Allocation profile')).toBeVisible()
}

test.describe('a relay tier the backend refuses is refused here first', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the cell says so, and Build plan sends nothing', async ({ page }) => {
    const calls = await countWrites(page)
    await openAccount(page, DEF_RELAY)

    // The mark that was already there, and was already ignored.
    await expect(page.getByText(/Profile section 5\.9 says role villages may not relay/)).toBeVisible()

    await page.getByRole('button', { name: /^Build plan/ }).click()
    // Named by the column heading and by the village whose list has to change.
    await expect(page.getByText(/Relays for \(11\)/)).toBeVisible()
    expect(calls.plan).toBe(0)
  })

  test('neither writer will save it', async ({ page }) => {
    const calls = await countWrites(page)
    await openAccount(page, DEF_RELAY)

    await expect(page.getByRole('button', { name: 'Save setup to server' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Save setup to file' })).toBeDisabled()
    // And the panel says which cell, rather than only greying out.
    await expect(page.getByRole('status').filter({ hasText: /Relays for/ })).toBeVisible()
    expect(calls.puts).toBe(0)
  })

  test('the refusal drops the caret in the relay’s own picker', async ({ page }) => {
    await countWrites(page)
    await openAccount(page, DEF_RELAY)

    await page.getByRole('button', { name: 'Day & night' }).click()
    await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()

    // Back on the stage that mounts the cell, with its picker open: the group
    // is inside a `<details>` the refusal has to unfold before anything in it
    // can be reached.
    await expect(
      page.getByRole('group', { name: `Villages 11 forwards material to` })
    ).toBeVisible()
  })

  test('a legal tier is planned and saved exactly as before', async ({ page }) => {
    const calls = await countWrites(page)
    // 02 has no role, so it may relay: the tier is one hop, to a village this
    // account has, and nothing else claims 11.
    await openAccount(page, { planner_relay_for: { [CAPITAL]: [DEF_A] } })

    await expect(page.getByRole('button', { name: 'Save setup to server' })).toBeEnabled()
    await page.getByRole('button', { name: /^Build plan/ }).click()
    await expect.poll(() => calls.plan).toBe(1)
  })
})
