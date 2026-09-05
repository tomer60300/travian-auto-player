/**
 * `indeterminate` is attention. It is not success and it is not failure.
 *
 * `RouteActionResponse.status` (`src/travian_api/web/routes/distribution.py`)
 * gained a fourth create outcome in 15475f4, and its own comment says what it
 * means: the create's answer died AND the marketplace could not be read twice
 * the same way, so absence proves nothing. It keeps its row charge, it is not
 * counted as a refusal in the failure streak, and the next run settles it.
 *
 * The page rendered it generically -- `a.status.replace('_', ' ')` in the
 * `text-info` fallback with no glyph -- which loses nothing by itself. What
 * lost something was everything around the table: the result toast and the
 * "Deferred routes were not checked this run" note both branched on
 * `status === 'failed'` alone, so a run whose every create came back
 * indeterminate produced a GREEN toast and a paragraph reassuring the operator
 * that deferrals are normal. Nothing on the page said a write may or may not
 * have landed.
 *
 * Note what the fixture leaves empty: `problems`. An indeterminate action sets
 * `marketplace_state_uncertain`, and that only becomes a `problems` line when a
 * LATER origin is refused because of it -- so a one-origin run really does
 * report an indeterminate create with an empty problem list, which is exactly
 * the run that read as clean.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test indeterminateAction
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, DEF_A, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

const action = (over) => ({
  origin: CAPITAL,
  origin_name: '02',
  destination: DEF_A,
  destination_name: '11',
  dest_x: 4,
  dest_y: 0,
  cycle_hours: 4,
  merchants: 3,
  detail: '',
  ...over,
})

/** The backend's own detail for the unattributable case, verbatim. */
const UNSETTLED_DETAIL =
  "the create's answer died and this destination's rows could not be attributed, " +
  'so whether it landed is unknown; the next run settles it'

/** One indeterminate create and nothing else: no problems, nothing failed, and
 *  routes left for a later run -- the shape that used to read as a clean run. */
const ONLY_INDETERMINATE = {
  ...PREVIEW,
  dry_run: false,
  created: 0,
  remaining: 4,
  actions: [action({ status: 'indeterminate', detail: UNSETTLED_DETAIL })],
  problems: [],
}

/** All three outcomes at once, so "distinct from success and danger" is asked
 *  as a comparison rather than as a guess about which token was used. */
const ALL_THREE = {
  ...ONLY_INDETERMINATE,
  created: 1,
  actions: [
    action({ status: 'created', detail: 'route 9001' }),
    action({ status: 'failed', detail: 'the Gold Club refused this route' }),
    action({ status: 'indeterminate', detail: UNSETTLED_DETAIL }),
  ],
}

async function goLive(page, live) {
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    const body = route.request().postDataJSON()
    await route.fulfill({ json: body.execution_mode === 'live' ? live : PREVIEW })
    return 'handled'
  })
  await seed(page)
  await openPlan(page)
  await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
  await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
  await page.getByRole('button', { name: /^Go live/ }).click()
}

/** The status cell of the row whose status word is `word`. */
function statusCell(page, word) {
  return page.getByRole('cell', { name: new RegExp(`\\b${word}$`) })
}

test.describe('an indeterminate create is not reported as a clean run', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('its cell takes a tone and a glyph of its own', async ({ page }) => {
    await goLive(page, ALL_THREE)

    const cells = {}
    for (const word of ['created', 'failed', 'indeterminate']) {
      const cell = statusCell(page, word)
      await expect(cell).toBeVisible()
      cells[word] = await cell.evaluate((el) => ({
        text: el.textContent.trim(),
        colour: getComputedStyle(el).color,
      }))
    }

    // A glyph as well as a word: outcome is never colour-only (item 3 of the UI
    // Definition of Done reads on contrast, but the rule the table already
    // follows for the other two is a glyph beside the word).
    expect(cells.created.text).toMatch(/^✓ /)
    expect(cells.failed.text).toMatch(/^✕ /)
    expect(cells.indeterminate.text).toMatch(/^⚠ /)
    expect(cells.indeterminate.text).toContain('indeterminate')

    // Distinct from BOTH, which is the whole claim: it is neither the outcome
    // that worked nor the outcome that was refused.
    expect(cells.indeterminate.colour).not.toBe(cells.created.colour)
    expect(cells.indeterminate.colour).not.toBe(cells.failed.colour)
  })

  test('the toast is not a success when a create could not be settled', async ({ page }) => {
    await goLive(page, ONLY_INDETERMINATE)

    // `created: 0`, `problems: []`, nothing `failed` -- so the toast used to be
    // the green "No new routes needed, 4 deferred to a later run".
    await expect(page.locator('.toast-success')).toHaveCount(0)
    await expect(page.locator('.toast-warning')).toHaveCount(1)
    await expect(page.locator('.toast-warning')).toContainText(/could not be settled/)
  })

  test('the reassuring deferred note does not cover an unsettled run', async ({ page }) => {
    await goLive(page, ONLY_INDETERMINATE)

    await expect(statusCell(page, 'indeterminate')).toBeVisible()
    // "Run again to continue — already-active routes are skipped" is true of a
    // run that merely ran out of budget. It is not true of one that does not
    // know what it wrote.
    await expect(page.getByText(/Deferred routes were not checked this run/)).toHaveCount(0)
  })

  test('a run that only deferred still gets the note', async ({ page }) => {
    await goLive(page, {
      ...ONLY_INDETERMINATE,
      created: 1,
      actions: [action({ status: 'created', detail: 'route 9001' })],
    })

    await expect(page.getByText(/Deferred routes were not checked this run/)).toBeVisible()
  })
})
