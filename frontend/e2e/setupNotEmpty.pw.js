/**
 * "Nothing typed yet" said about a page with something typed on it.
 *
 * `setupDocument()` refuses to write a document with no content, which is
 * right: "you have saved nothing" and "you saved a blank sheet" are different
 * states the server itself distinguishes, and writing the second by accident
 * destroys the first.
 *
 * But it counted three things -- village columns, named profiles and role
 * templates -- while `buildSetup` writes eight more. So a page whose only
 * content was a foreign tribute, a profile's hours, the reserved NPC-burst
 * window, an attendance answer, an overnight declaration, a calibrated
 * merchant model or a deliberately unticked window prune was told to
 * "fill in a Trade Office level, crop alert or allocation first" -- and the one
 * answer it holds went unsaved, on an origin that will drop it.
 *
 * Every one of those is an owned answer nothing in the game states, and four of
 * them earned a document version bump precisely because losing them is
 * expensive.
 *
 * NO BACKEND AND NO GAME REQUEST: the store's PUT is answered from a fixture
 * and its body is what gets asserted.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test setupNotEmpty
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolate, seed } from './plannerHarness'

const REFUSAL = /Nothing typed yet/

async function isolateStore(page) {
  const state = { puts: [] }
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/setup')) return undefined
    const method = route.request().method()
    if (method === 'PUT') {
      state.puts.push(route.request().postDataJSON())
      await route.fulfill({ json: { saved_at: '2026-09-05T04:00:00Z' } })
      return 'handled'
    }
    await route.fulfill({
      status: 404,
      json: { detail: 'No planner setup is saved for this account.' },
    })
    return 'handled'
  })
  return state
}

async function save(page) {
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Allocation profile')).toBeVisible()
  await page.getByRole('button', { name: 'Save setup to server' }).click()
}

/** One case per thing the document carries and the guard did not count. */
const CARRIED = [
  {
    what: 'a foreign tribute',
    seed: {
      planner_foreign_targets: [
        {
          name: 'Ally',
          x: 12,
          y: -34,
          crop_per_hour: 25700,
          safety_margin_pct: 5,
          route_eligible: false,
        },
      ],
    },
    field: 'foreign_targets',
  },
  {
    what: "a profile's hours",
    seed: { planner_profile_windows: { Day: ['07:00', '23:00'] } },
    field: 'profile_windows',
  },
  {
    what: 'the reserved NPC-burst window',
    seed: { planner_reserved_window: ['21:00', '22:00'] },
    field: 'reserved_window',
  },
  {
    what: 'who is trading during a window',
    seed: { planner_npc_attended: { Day: true } },
    field: 'npc_attended',
  },
  {
    what: 'which profile is the one slept through',
    seed: { planner_overnight: { Day: true } },
    field: 'overnight',
  },
  {
    what: 'a calibrated merchant model',
    seed: { planner_merchant_model: { base_capacity: 1000 } },
    field: 'merchant_model',
  },
  // The v10 field, and the one with the sharpest consequence of any here: it
  // decides whether `/execute` DELETES game rows. Its resting state is ON, so
  // OFF is the deliberate answer and the only polarity that is content.
  {
    what: 'a deliberately unticked window prune',
    seed: { planner_prune_to_window: false },
    field: 'prune_to_window',
  },
]

test.describe('the empty-document guard counts what the document carries', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  for (const { what, seed: extra, field } of CARRIED) {
    test(`${what} is content`, async ({ page }) => {
      const store = await isolateStore(page)
      await seed(page, extra)
      await save(page)

      await expect(page.getByText(REFUSAL)).toHaveCount(0)
      await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()
      expect(store.puts).toHaveLength(1)
      expect(store.puts[0]).toHaveProperty(field)
    })
  }

  // The guard still has to fire, or it is not a guard. A page with a fresh
  // snapshot and nothing else is the state it exists for: saving a blank sheet
  // over a real one is what it prevents.
  test('a page with nothing on it is still refused', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page)
    await save(page)

    await expect(page.getByText(REFUSAL)).toBeVisible()
    expect(store.puts).toHaveLength(0)
  })

  // The resting state of the prune is ON, and nobody typed it. Counting it
  // would have made the guard unreachable.
  test('the prune left at its resting ON is not content on its own', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, { planner_prune_to_window: true })
    await save(page)

    await expect(page.getByText(REFUSAL)).toBeVisible()
    expect(store.puts).toHaveLength(0)
  })

  // Same rule for the merchant model, whose boxes are SEEDED from the
  // planner's own defaults: they are filled in on a page nobody has touched.
  test('the merchant model left at the planner’s own figures is not content', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, {
      planner_merchant_model: {
        base_capacity: 2500,
        bonus_per_to_level: 0.2,
        merchant_reserve: 2,
        merchant_headroom: 0.1,
      },
    })
    await save(page)

    await expect(page.getByText(REFUSAL)).toBeVisible()
    expect(store.puts).toHaveLength(0)
  })

  // And the summary the toast prints has to name what was actually written,
  // or a save that carried one answer reads as a save that carried none.
  test('the summary names what was saved', async ({ page }) => {
    await isolateStore(page)
    await seed(page, {
      planner_trade_office: { [CAPITAL]: 13 },
      planner_reserved_window: ['21:00', '22:00'],
    })
    await save(page)

    await expect(page.getByText(/1 village\(s\).*reserved window/)).toBeVisible()
  })
})
