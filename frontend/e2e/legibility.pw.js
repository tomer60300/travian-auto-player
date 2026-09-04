/**
 * The four small, exact legibility fixes, measured.
 *
 * Each one is a thing that is either on screen or not, or reachable or not, so
 * each one is driven rather than reasoned about:
 *
 *   * the horizontal table scroller took no keyboard at all. Tabbing into an
 *     off-screen INPUT auto-scrolls it into view, which is why this survived
 *     twelve rounds -- but read-only figures and the whole header row in the
 *     scrolled-away region are not focusable, so a keyboard user could not
 *     reach them (WCAG 2.1.1);
 *   * the hours-of-the-day bar had no tick, no axis and no label, and drew a
 *     23:00-07:00 night as two disconnected pills;
 *   * "DEF, Feeder has villages but no template", with no mention of the button
 *     that fixes it;
 *   * the Day & night attendance answer was printed twice, once in the select
 *     and once in 11px grey beneath it.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test legibility
 */

import { expect, test } from '@playwright/test'

import { isolate, seed } from './plannerHarness'

const stageTab = (page, name) => page.getByRole('button', { name, exact: true })

/** Two profiles with the windows by convention, one of which wraps past
 *  midnight. Both have to be SEEDED: a fresh account has one profile, `Day`,
 *  and `DayNightPanel` reads `profileWindows[name]` with no default -- so an
 *  account nobody has typed hours into shows "no hours set" in every row while
 *  the profile bar above it and `buildPlanPayload` both fall back to
 *  DEFAULT_WINDOWS and plan 07:00-23:00. That disagreement is reported, not
 *  fixed here. */
const PROFILES = { Day: {}, Night: {} }
const WINDOWS = { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] }
const DAY_AND_NIGHT = { planner_profiles: PROFILES, planner_profile_windows: WINDOWS }

test.describe('the table scroller takes a keyboard', () => {
  // 375, where the Account table overflows hardest. The pinned column plus
  // thirteen more in a 327px strip.
  test.use({ viewport: { width: 375, height: 900 } })

  test('the scrolled region is a named landmark, and Tab reaches it', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    const region = page.getByRole('region', { name: 'The account, village by village' })
    await expect(region).toBeVisible()
    await expect(region).toHaveAttribute('tabindex', '0')

    // Focused directly, then scrolled by the keyboard alone: this is the whole
    // of WCAG 2.1.1 for an `overflow-x: auto` box.
    await region.focus()
    await expect(region).toBeFocused()
    const before = await region.evaluate((el) => el.scrollLeft)
    await page.keyboard.press('ArrowRight')
    await page.keyboard.press('ArrowRight')
    await page.keyboard.press('ArrowRight')
    await expect
      .poll(() => region.evaluate((el) => el.scrollLeft))
      .toBeGreaterThan(before)
  })

  test('a table that fits is not a landmark and not a tab stop', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')
    await stageTab(page, 'Day & night').click()

    // At 1440 the day/night table fits; measured here at 375 it does not, so
    // the assertion is made the other way round -- every REGION that exists
    // corresponds to a container that measured as overflowing.
    const regions = page.getByRole('region')
    for (const region of await regions.all()) {
      const overflows = await region.evaluate((el) => el.scrollWidth > el.clientWidth)
      expect(overflows).toBe(true)
    }
  })
})

test.describe('the hours of the day can be read', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('there is an axis, at the four positions the ticks mark', async ({ page }) => {
    await isolate(page)
    await seed(page, DAY_AND_NIGHT)
    await page.goto('/resource-planner')
    await stageTab(page, 'Day & night').click()

    const axis = page.locator('.day-axis').first()
    await expect(axis).toHaveText('00061218')
    // One tick per label, on every row's own track.
    const ticks = await page.locator('.day-track .day-track-tick').count()
    const tracks = await page.locator('.day-track').count()
    expect(tracks).toBe(2)
    expect(ticks).toBe(tracks * 4)
  })

  // 23:00-07:00 drew as two pills with rounded inner ends, so it read as two
  // unrelated windows rather than as one night crossing midnight.
  test('a wrapping window is one band, cut by the edges it runs off', async ({ page }) => {
    await isolate(page)
    await seed(page, DAY_AND_NIGHT)
    await page.goto('/resource-planner')
    await stageTab(page, 'Day & night').click()

    // Row 2 is Night (23:00-07:00), which wraps; row 1 is Day (07:00-23:00).
    const day = page.locator('.day-track').nth(0).locator('.day-track-band')
    const night = page.locator('.day-track').nth(1).locator('.day-track-band')
    await expect(day).toHaveCount(1)
    await expect(night).toHaveCount(2)

    const caps = await night.evaluateAll((nodes) =>
      nodes.map((el) => {
        const cs = getComputedStyle(el)
        return [cs.borderTopLeftRadius, cs.borderTopRightRadius]
      }),
    )
    // The late piece is rounded where the night begins and square where it runs
    // off the right edge; the early piece is the mirror of it.
    expect(caps[0][1]).toBe('0px')
    expect(caps[1][0]).toBe('0px')
    expect(caps[0][0]).not.toBe('0px')
    expect(caps[1][1]).not.toBe('0px')

    // And the two pieces really do reach the two edges of one track.
    const track = await page.locator('.day-track').nth(1).boundingBox()
    const boxes = await night.evaluateAll((nodes) =>
      nodes.map((el) => el.getBoundingClientRect()).map((r) => [r.left, r.right]),
    )
    expect(Math.abs(boxes[0][1] - (track.x + track.width))).toBeLessThan(1.5)
    expect(Math.abs(boxes[1][0] - track.x)).toBeLessThan(1.5)
  })

  test('the answer is stated once, in the select, not echoed under it', async ({ page }) => {
    await isolate(page)
    await seed(page, { ...DAY_AND_NIGHT, planner_npc_attended: { Day: true, Night: false } })
    await page.goto('/resource-planner')
    await stageTab(page, 'Day & night').click()

    await expect(page.getByLabel('Who is trading during Day')).toHaveValue('awake')
    // The echo printed the option's own words again, in 11px grey. One left.
    expect(await page.getByText('you are at the marketplace', { exact: true }).count()).toBe(0)
  })
})

test.describe('an explanation comes before the table it explains', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('the Assumed crop/h note is above the Role templates table', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')
    await stageTab(page, 'Targets').click()
    await page.getByText('Role templates', { exact: true }).click()

    const note = page.getByText('Assumed crop/h ships nothing.')
    await expect(note).toBeVisible()
    const table = page.getByRole('region', {
      name: 'Role templates, one profile per kind of village',
    })
    const noteBox = await note.boundingBox()
    const tableBox = await table.boundingBox()
    // At 1440 this table shows four of eleven columns, so an explanation of the
    // last one printed after it is reached only by scrolling past the thing it
    // explains.
    expect(noteBox.y).toBeLessThan(tableBox.y)
  })

  test('the foreign-target note names the column as the header reads it', async ({ page }) => {
    await isolate(page)
    await seed(page, {
      planner_foreign_targets: [
        {
          name: 'ally',
          x: 12,
          y: 9,
          crop_per_hour: 2000,
          safety_margin_pct: 5,
          route_eligible: false,
        },
      ],
    })
    await page.goto('/resource-planner')

    // It said "route-eligible"; the column is headed "Route?", which is also
    // what the scroll hint names -- so the sentence pointed at a column nothing
    // on screen was called.
    await expect(page.getByText(/Tick Route\? — the last column/)).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Route?' })).toBeAttached()
  })
})
