/**
 * A failed read must not read like an empty one.
 *
 * The wave-4 census's most common defect, live-confirmed on 7 of 15 pages: the
 * error variant and the empty variant of the same page rendered byte-for-byte
 * the same text. Two were worse than that -- Buildings' queue section vanished
 * entirely on failure (no heading, no message), and Reports rendered nothing at
 * all, not even its own empty sentence.
 *
 * Each test here drives the SAME page twice against two fixtures, an empty one
 * and a failing one, and asserts three things:
 *
 *   1. the empty variant still says what it always said (no regression on the
 *      empty state, which several of these pages get right);
 *   2. the error variant says something the empty variant does not;
 *   3. the error variant does NOT say what the empty variant says -- the actual
 *      defect. A page that renders both is still telling the user the game is
 *      idle when what happened is that we could not ask it.
 *
 * A toast does not satisfy (2): the census confirmed live that 4.5s later the
 * page reads exactly like the empty state again. The assertions run after the
 * toast's own lifetime where a toast is the only thing on screen.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`. `{ status: 500 }` in a
 * fixture is how the failing variant is driven -- still a fulfilled route,
 * never a real request.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test maskedErrors
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const RESOURCES = { wood: 1200, clay: 1100, iron: 900, crop: 800, warehouse: 8000, granary: 8000 }
const EMPTY_QUEUE = { village_id: CAPITAL, queue: [] }
const NO_BUILDINGS = { village_id: CAPITAL, buildings: [] }
const BOOM = { status: 500, json: { detail: 'Travian returned 503' } }

test.describe('Dashboard', () => {
  test('a resource read that failed is not a village with no resources', async ({ page }) => {
    await isolateApp(page, {
      '/buildings/resources': RESOURCES,
      '/buildings/queue': EMPTY_QUEUE,
      '/buildings': NO_BUILDINGS,
    })
    await page.goto('/')
    await expect(page.getByText('No active operations.', { exact: false })).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('both failed reads are named, and the idle sentence is withdrawn', async ({ page }) => {
    await isolateApp(page, {
      '/buildings/resources': BOOM,
      '/buildings/queue': BOOM,
      '/buildings': NO_BUILDINGS,
    })
    await page.goto('/')

    await expect(
      page.getByRole('alert').filter({ hasText: "Could not read this village's resources" })
    ).toBeVisible()
    await expect(
      page.getByRole('alert').filter({ hasText: 'Could not read the construction queue' })
    ).toBeVisible()
    // The server's own words, not only ours.
    await expect(page.getByText('Travian returned 503').first()).toBeVisible()
    // Two Retry buttons, one per failed read.
    await expect(page.getByRole('button', { name: 'Retry' })).toHaveCount(2)

    // The defect: "No active operations" is a claim about the game, and we
    // never got to ask it.
    await expect(page.getByText('No active operations.', { exact: false })).toHaveCount(0)
  })
})

test.describe('Buildings', () => {
  test('an empty queue renders no queue section and no alert', async ({ page }) => {
    await isolateApp(page, {
      '/buildings/queue': EMPTY_QUEUE,
      '/buildings': { village_id: CAPITAL, buildings: [{ slot_id: 1, name: 'Woodcutter', level: 3 }] },
    })
    await page.goto('/buildings')
    await expect(page.getByRole('heading', { name: 'Building Slots' })).toBeVisible()
    await expect(page.getByRole('heading', { name: /Construction Queue/ })).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('the queue section stays on screen, named, when its own read fails', async ({ page }) => {
    await isolateApp(page, {
      '/buildings/queue': BOOM,
      '/buildings': { village_id: CAPITAL, buildings: [{ slot_id: 1, name: 'Woodcutter', level: 3 }] },
    })
    await page.goto('/buildings')

    // The heading is the half that used to disappear with the section.
    await expect(page.getByRole('heading', { name: /Construction Queue/ })).toBeVisible()
    await expect(
      page.getByRole('alert').filter({ hasText: 'Could not read the construction queue' })
    ).toBeVisible()
    await expect(page.getByText('Travian returned 503')).toBeVisible()
    // The building LIST beside it is unaffected -- it already had its own
    // error path and this fixture answers it successfully.
    await expect(page.getByRole('button', { name: /Woodcutter/ })).toBeVisible()
  })
})

test.describe('BuildQueue', () => {
  test('an empty village says so, with no alert', async ({ page }) => {
    await isolateApp(page, { '/buildings': NO_BUILDINGS, '/buildings/queue': EMPTY_QUEUE })
    await page.goto('/queue')
    await expect(page.getByText('No buildings available. Connect to a server first.')).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('a failed read is named instead of claiming the village is empty', async ({ page }) => {
    await isolateApp(page, { '/buildings': BOOM, '/buildings/queue': BOOM })
    await page.goto('/queue')

    await expect(
      page.getByRole('alert').filter({ hasText: "Could not read this village's buildings" })
    ).toBeVisible()
    await expect(page.getByText('Travian returned 503')).toBeVisible()
    // The old text was both identical to the empty state AND wrong: we are
    // connected, which is why the request was made at all.
    await expect(page.getByText('No buildings available. Connect to a server first.')).toHaveCount(0)
  })
})

test.describe('Sessions', () => {
  test('an empty session list says nothing is running', async ({ page }) => {
    await isolateApp(page, { '/sessions': [] })
    await page.goto('/sessions')
    await expect(page.getByText('No active or recent sessions')).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('a failed poll does not claim nothing is running', async ({ page }) => {
    await isolateApp(page, { '/sessions': BOOM })
    await page.goto('/sessions')

    await expect(
      page.getByRole('alert').filter({ hasText: 'Could not read the session list' })
    ).toBeVisible()
    await expect(page.getByText('Travian returned 503')).toBeVisible()
    // This page is the one place an operator checks whether something IS
    // running, so a wrong "no" here is the worst version of the defect.
    await expect(page.getByText('No active or recent sessions')).toHaveCount(0)
  })
})

test.describe("AutoScout's background account", () => {
  test('a genuinely unconfigured account says "not configured"', async ({ page }) => {
    await isolateApp(page, { '/recon/status': { configured: false, manageable: true } })
    await page.goto('/scout')
    await expect(page.getByText('not configured')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Set' })).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('a failed status read is not the same as no account', async ({ page }) => {
    await isolateApp(page, { '/recon/status': BOOM })
    await page.goto('/scout')

    await expect(page.getByRole('alert').filter({ hasText: 'Status unknown' })).toBeVisible()
    await expect(page.getByText('Travian returned 503')).toBeVisible()
    // The defect: it offered to SET an account that may already exist.
    await expect(page.getByText('not configured')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Set' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
  })
})

test.describe('RaidOptimizer', () => {
  // FLAT `t1..t10`, and the smithy under `research` -- the shapes
  // `inventoryFromTroopsAPI` / `smithyFromAPI` actually read. The counts are
  // the page's own defaults on purpose: the census's point is that these exact
  // numbers produce a confident four-strategy table, so a fixture that
  // supplies them proves the success path renders what the failure path used
  // to render for free.
  const TROOPS = { t1: 1000, t6: 1000 }
  const SMITHY = { found: true, research: { t1: 5, t6: 14 } }

  test('a successful read designs a force', async ({ page }) => {
    await isolateApp(page, { '/military/troops': TROOPS, '/military/smithy': SMITHY })
    await page.goto('/raid-optimizer')
    await expect(page.getByRole('heading', { name: 'OPTIMAL DEPLOYMENTS' })).toBeVisible()
    await expect(page.getByText('strategies', { exact: false }).first()).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('a failed read refuses to compute, and says the numbers are not the army', async ({
    page,
  }) => {
    await isolateApp(page, { '/military/troops': BOOM, '/military/smithy': BOOM })
    await page.goto('/raid-optimizer')

    // The mount auto-fill passes `showToast = false`, so before this fix there
    // was NO signal of any kind -- not even an expiring toast.
    await expect(
      page.getByRole('alert').filter({ hasText: 'Troop counts unread' })
    ).toBeVisible()
    await expect(page.getByText('Travian returned 503')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Read troops again' })).toBeVisible()

    // The defect: a full four-strategy table computed from 1000 clubs and
    // 1000 TKs that nobody supplied.
    await expect(page.getByText('4 strategies')).toHaveCount(0)
    await expect(page.getByText('No strategies computed.')).toBeVisible()
    await expect(page.getByText('Nothing to optimise yet')).toBeVisible()
    // And not the OTHER wrong answer either: "no valid composition" is a claim
    // about an army we never read.
    await expect(page.getByText('No valid composition found')).toHaveCount(0)
  })

  test('typing a real count over the placeholder lifts the refusal', async ({ page }) => {
    await isolateApp(page, { '/military/troops': BOOM, '/military/smithy': BOOM })
    await page.goto('/raid-optimizer')
    await expect(page.getByText('No strategies computed.')).toBeVisible()

    await page.getByRole('spinbutton').first().fill('850')

    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText('No strategies computed.')).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'OPTIMAL DEPLOYMENTS' })).toBeVisible()
  })
})
