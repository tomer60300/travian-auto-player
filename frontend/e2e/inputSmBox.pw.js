/**
 * Every `.input-sm` in the planner, measured as a BOX.
 *
 * `.input-sm` was named on seven controls and had no base rule anywhere in
 * `index.css`. Its single mention sat inside `@media (max-width: 640px)` and
 * only pinned `font-size: 16px`, so at every wider viewport the class
 * contributed nothing at all and the browser drew a bare `<input>`: measured at
 * 1440 before the fix, `height 16px`, `background rgba(0, 0, 0, 0)`,
 * `border-width 0px`, `border-radius 0px`, `padding 0px 0px`.
 *
 * That is not a cosmetic defect. Five of the seven are the live-run safety
 * controls -- the route cap, the row cap, the never-disable list and the two
 * single-village narrowing boxes -- and "Max rows this run" is empty by
 * default, so there was nothing on screen to click at all. The other two are
 * the 25%/60% night pair.
 *
 * So the assertion is the thing that was wrong, at the three viewports the UI
 * Definition of Done names: a control the operator is expected to find must
 * have a filled background, a radius, padding and a height. Measured rather
 * than screenshotted for the same reason `inputWidths.pw.js` is -- a baseline
 * PNG of an invisible control is a baseline of the defect, and it would pass
 * for as long as the defect stood.
 *
 * Item 4 of the UI Definition of Done is asked separately, with a COARSE
 * pointer, because that is the only condition under which it applies. `375` on
 * a desktop pointer is a narrow window, not a phone, and the app's own
 * `@media (pointer: coarse)` block is orthogonal to width by design.
 *
 * The fixture is `plannerHarness.js`, a plain module rather than a spec:
 * Playwright collects every `*.pw.js` under `e2e/`, so importing from another
 * SPEC would register that spec's tests a second time.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test inputSmBox
 *   MEASURE=1 npx playwright test inputSmBox   # print the numbers table
 */

import { expect, test } from '@playwright/test'

import { VIEWPORTS, isolate, openPlan, openTargets, seed } from './plannerHarness'

const SURFACES = [
  { name: 'Targets stage (the 25%/60% night pair)', open: openTargets, expected: 2 },
  { name: 'Plan stage (the five controlled-run boxes)', open: openPlan, expected: 5 },
]

/** Every `.input-sm` on screen, with the box properties that make it findable. */
async function measure(page) {
  return page.$$eval('.input-sm', (nodes) =>
    nodes.map((el) => {
      const cs = getComputedStyle(el)
      const rect = el.getBoundingClientRect()
      const label =
        el.getAttribute('aria-label') ||
        el.closest('label')?.textContent?.trim().slice(0, 40) ||
        el.getAttribute('placeholder') ||
        el.tagName
      return {
        label,
        height: Math.round(rect.height * 10) / 10,
        background: cs.backgroundColor,
        borderRadius: cs.borderTopLeftRadius,
        paddingY: cs.paddingTop,
        paddingX: cs.paddingLeft,
        fontSize: cs.fontSize,
      }
    }),
  )
}

const TRANSPARENT = /^(transparent|rgba\(0, 0, 0, 0\))$/

for (const viewport of VIEWPORTS) {
  test.describe(`every .input-sm is drawn as a box at ${viewport.width}px`, () => {
    test.use({ viewport })

    for (const surface of SURFACES) {
      test(surface.name, async ({ page }) => {
        await isolate(page)
        await seed(page)
        await surface.open(page)

        const boxes = await measure(page)
        if (globalThis.process?.env?.MEASURE) {
          console.log(`\n${viewport.width}px — ${surface.name}`)
          for (const b of boxes) {
            console.log(
              `  ${b.label.padEnd(42)} h=${String(b.height).padEnd(6)} ` +
                `bg=${b.background.padEnd(20)} radius=${b.borderRadius.padEnd(5)} ` +
                `pad=${b.paddingY}/${b.paddingX} font=${b.fontSize}`,
            )
          }
        }

        expect(boxes.length, 'every .input-sm on this surface is on screen').toBe(
          surface.expected,
        )
        for (const box of boxes) {
          expect(box.background, `${box.label} has a filled background`).not.toMatch(TRANSPARENT)
          expect(
            parseFloat(box.borderRadius),
            `${box.label} has a corner radius`,
          ).toBeGreaterThan(0)
          expect(parseFloat(box.paddingY), `${box.label} has vertical padding`).toBeGreaterThan(0)
          expect(
            parseFloat(box.paddingX),
            `${box.label} has horizontal padding`,
          ).toBeGreaterThan(0)
          // The old bare input measured 16px tall. Anything padded clears this
          // by a wide margin; the number is a floor, not a target.
          expect(box.height, `${box.label} is taller than a bare text run`).toBeGreaterThan(20)
        }
      })
    }
  })
}

test.describe('the focus ring, which is the other half of finding a control', () => {
  test.use({ viewport: VIEWPORTS[2] })

  test('an .input-sm shows the app-wide focus outline', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    const box = page.locator('.input-sm').first()
    await box.focus()
    const ring = await box.evaluate((el) => {
      const cs = getComputedStyle(el)
      return { width: cs.outlineWidth, style: cs.outlineStyle, offset: cs.outlineOffset }
    })
    if (globalThis.process?.env?.MEASURE) console.log('\nfocus ring:', JSON.stringify(ring))
    // The same ring `.input-field:focus-visible` and `.btn-primary:focus-visible`
    // declare, which is what item 2 of the UI Definition of Done asks for.
    expect(ring.style).toBe('solid')
    expect(parseFloat(ring.width)).toBe(2)
    expect(parseFloat(ring.offset)).toBe(2)
  })
})

test.describe('item 4 of the UI Definition of Done, on a coarse pointer', () => {
  // Width and pointer type are orthogonal in this app's CSS by design, so the
  // 44px rule is asked of a touch device rather than of a narrow window.
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('every .input-sm clears 44px', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    expect(
      await page.evaluate(() => matchMedia('(pointer: coarse)').matches),
      'the emulated device really reports a coarse pointer',
    ).toBe(true)

    const boxes = await measure(page)
    if (globalThis.process?.env?.MEASURE) {
      console.log('\n390px coarse pointer — Plan stage')
      for (const b of boxes) console.log(`  ${b.label.padEnd(42)} h=${b.height}`)
    }
    expect(boxes.length).toBe(5)
    for (const box of boxes) {
      expect(box.height, `${box.label} is a 44px tap target`).toBeGreaterThanOrEqual(44)
    }
  })
})
