/**
 * The focus ring on the three controls the app ring never reached.
 *
 * `index.css` wires its purple `:focus-visible` ring to five classes only --
 * `.btn-primary`, `.btn-secondary`, `.input-field`, `.input-sm`, `.tab-btn`.
 * "Lift restriction" and "Stop relaying" (both `className="underline mt-1"`)
 * carry none of them, and neither does the Relays-for `<summary>`, so all
 * three fell back to Chromium's plain default outline instead of the app's
 * own ring.
 *
 * `.focus()` here is never preceded by a `.click()`/`.check()` on this page --
 * the pickers are opened directly via `details.open = true`, not by clicking
 * the summary -- so Chromium's focus modality stays whatever a fresh
 * navigation leaves it at, and `:focus-visible` reports true for a
 * `<button>`/`<summary>` the same way it already does for a bare `.focus()`
 * on `.input-sm` (see `inputSmBox.pw.js`).
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test focusRing
 */

import { expect, test } from '@playwright/test'

import { DEF_A, isolate, seed } from './plannerHarness'

async function openAccount(page, extra = {}) {
  await isolate(page)
  await seed(page, extra)
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Allocation profile')).toBeVisible()
}

/** Every cell picker is a native `<details>`, and a closed one keeps its
 *  contents out of the accessibility tree -- opened directly, same approach
 *  as `plannerNames.pw.js` and `cellPickers.pw.js`. */
async function openEveryPicker(page) {
  await page.evaluate(() => {
    for (const d of document.querySelectorAll('tbody details')) d.open = true
  })
}

/** The resolved `--md-primary` token, read through a real computed style --
 *  the same way the browser resolves it for `outline-color` -- rather than
 *  the raw custom-property string, so the two are comparable directly. */
async function primaryRingColor(page) {
  return page.evaluate(() => {
    const probe = document.createElement('div')
    probe.style.color = 'var(--md-primary)'
    document.body.appendChild(probe)
    const color = getComputedStyle(probe).color
    probe.remove()
    return color
  })
}

async function ring(locator) {
  return locator.evaluate((el) => {
    const cs = getComputedStyle(el)
    return { style: cs.outlineStyle, color: cs.outlineColor }
  })
}

test.describe('the app ring reaches the three controls it used to miss', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('Lift restriction, Stop relaying, and the Relays-for summary all carry it', async ({
    page,
  }) => {
    await openAccount(page, {
      planner_ship_only_to: { [DEF_A]: [] },
      planner_relay_for: { [DEF_A]: [] },
    })
    await openEveryPicker(page)

    const primary = await primaryRingColor(page)

    const lift = page.getByRole('button', { name: 'Lift restriction for 11' })
    await lift.focus()
    await expect(lift).toBeFocused()
    expect(await ring(lift)).toEqual({ style: 'solid', color: primary })

    const stop = page.getByRole('button', { name: 'Stop relaying for 11' })
    await stop.focus()
    await expect(stop).toBeFocused()
    expect(await ring(stop)).toEqual({ style: 'solid', color: primary })

    const summary = page.locator('summary').filter({ hasText: 'Relays for, for 11:' })
    await summary.focus()
    await expect(summary).toBeFocused()
    expect(await ring(summary)).toEqual({ style: 'solid', color: primary })
  })
})
