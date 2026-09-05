/**
 * The profile ceiling, which only the backend knew about.
 *
 * A whole-day request carries one segment per profile, and
 * `DayCheckRequest.segments` / `ExecuteRequest.segments` are
 * `max_length=MAX_DAY_SEGMENTS` -- 12, because each segment is its own
 * optimizer run and the real ceiling is cost, not arithmetic. `addProfile` had
 * no cap at all, so a thirteenth profile was created happily and the next
 * full-day check came back as a pydantic list-length 422: a message about
 * `body.segments` that names no profile, points at no control, and arrives
 * after the operator has typed a whole allocation set into it.
 *
 * So the twelfth is the last one the page will make, and the control says so
 * BEFORE it is pressed rather than after.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test profileCap
 */

import { expect, test } from '@playwright/test'

import { isolate, seed } from './plannerHarness'

/** Twelve, which is the cap: `MAX_DAY_SEGMENTS` on both sides. */
const FULL = Object.fromEntries(
  Array.from({ length: 12 }, (_, i) => [`P${String(i + 1).padStart(2, '0')}`, {}])
)
const ELEVEN = Object.fromEntries(Object.entries(FULL).slice(0, 11))

const addProfile = (page) => page.getByRole('button', { name: '+ New' })

async function openPlanner(page, profiles) {
  await isolate(page)
  await seed(page, { planner_profiles: profiles, planner_active_profile: 'P01' })
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Allocation profile')).toBeVisible()
}

test.describe('the day holds twelve profiles', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the eleventh account can still add one', async ({ page }) => {
    await openPlanner(page, ELEVEN)

    await expect(addProfile(page)).toBeEnabled()
    await addProfile(page).click()
    await page.getByRole('textbox', { name: /profile/i }).fill('P12')
    await page.getByRole('button', { name: 'Create' }).click()

    await expect(page.getByLabel('Allocation profile')).toHaveValue('P12')
  })

  test('a thirteenth is refused here, not by a 422 that names nothing', async ({ page }) => {
    await openPlanner(page, FULL)

    await expect(addProfile(page)).toBeDisabled()
    // And it says why, at the control, before it is pressed. A greyed-out
    // button with no sentence beside it is the same dead end as the 422.
    await expect(page.getByText(/12 profiles/)).toBeVisible()
  })

  test('duplicate is capped too, because it makes a profile as well', async ({ page }) => {
    await openPlanner(page, FULL)

    await expect(page.getByRole('button', { name: 'Duplicate' })).toBeDisabled()
    // Renaming and deleting are untouched: neither changes the count upward,
    // and DELETE is the way back under the cap.
    await expect(page.getByRole('button', { name: 'Rename' })).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Delete' })).toBeEnabled()
  })

  test('deleting one opens the door again', async ({ page }) => {
    await openPlanner(page, FULL)

    await page.getByRole('button', { name: 'Delete' }).click()
    await page.getByRole('button', { name: /Delete/ }).last().click()

    await expect(addProfile(page)).toBeEnabled()
  })
})
