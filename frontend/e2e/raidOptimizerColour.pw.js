/**
 * RaidOptimizer's functional colours, measured.
 *
 * The worst contrast page in the wave-4 census by a wide margin: 27 hex
 * literals inline in the page, chiefly the `UNIT_COLOR` and `STRAT_COLOR`
 * maps, applied as `style={{ color: ... }}` on troop and strategy labels drawn
 * straight onto `--bg-card`/`--bg-base` with no contrast check ever run against
 * them. **24 of 84 sampled text nodes failed AA in light theme** (1.58-3.48:1)
 * and **10 failed in dark** (2.17-4.31:1) -- the only place in the whole census
 * where the same literal failed in BOTH themes, which is what a colour chosen
 * for a dark surface and then shipped on a light one looks like.
 *
 * This spec SWEEPS every rendered text node on the page in both themes rather
 * than naming the failing ones, the same method the census used: computed
 * colour pairs, every translucent background layer composited down, WCAG
 * relative-luminance ratio. 4.5:1 for normal text, 3:1 where the text is >=24px
 * or >=18.66px and bold.
 *
 * The thirteen spinbuttons are checked here too: their `<label>` is a SIBLING
 * of the input, so every one of them had an empty accessible name.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test raidOptimizerColour
 */

import { expect, test } from '@playwright/test'

import { isolateApp } from './appHarness'
import { forceTheme, parseColor, composite, ratio } from './contrast'

// TWO unit types, not five. `findCompositions` enumerates compositions across
// every non-zero unit type, and a five-type inventory of this size does not
// finish inside a Playwright timeout -- the page simply hangs. Reported as a
// finding; not this spec's subject. 1000 clubs + 1000 TKs is the page's own
// default pair and renders the full four-strategy table, which is where most
// of the coloured text lives.
const FIXTURES = {
  '/military/troops': { t1: 1000, t6: 1000 },
  '/military/smithy': { found: true, research: { t1: 5, t6: 14 } },
}

async function openOptimizer(page) {
  await isolateApp(page, FIXTURES)
  await page.goto('/raid-optimizer')
  await expect(page.getByRole('heading', { name: 'Raid Composition Optimizer' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'OPTIMAL DEPLOYMENTS' })).toBeVisible()
}

/** Every visible text node with its own colour and the layers behind it. */
async function textSamples(page) {
  return page.evaluate(() => {
    const out = []
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    const seen = new Set()
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = (node.textContent || '').trim()
      if (!text) continue
      const el = node.parentElement
      if (!el || seen.has(el)) continue
      seen.add(el)
      const rect = el.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) continue
      const cs = getComputedStyle(el)
      if (cs.visibility === 'hidden' || cs.opacity === '0') continue
      const layers = []
      for (let n = el; n; n = n.parentElement) layers.push(getComputedStyle(n).backgroundColor)
      out.push({
        text: text.slice(0, 40),
        color: cs.color,
        opacity: Number(cs.opacity),
        fontSize: parseFloat(cs.fontSize),
        fontWeight: Number(cs.fontWeight),
        layers,
      })
    }
    return out
  })
}

function measure(sample) {
  let base = [255, 255, 255, 1]
  const stack = []
  for (const layer of sample.layers) {
    const parsed = parseColor(layer)
    if (parsed[3] === 1) {
      base = parsed
      break
    }
    stack.push(parsed)
  }
  let background = base
  for (const layer of stack.reverse()) background = composite(layer, background)
  // `opacity` on the element multiplies the text against what is behind it,
  // which is how a "muted" label can measure worse than its declared colour.
  const fg = parseColor(sample.color)
  fg[3] = fg[3] * sample.opacity
  const color = composite(fg, background)
  const large = sample.fontSize >= 24 || (sample.fontSize >= 18.66 && sample.fontWeight >= 700)
  return { ratio: ratio(color, background), required: large ? 3 : 4.5 }
}

for (const theme of [null, 'dark']) {
  const label = theme ?? 'light'

  test(`every text node on RaidOptimizer meets AA — ${label} theme`, async ({ page }) => {
    await openOptimizer(page)
    await forceTheme(page, theme)

    const samples = await textSamples(page)
    expect(samples.length, 'text nodes sampled').toBeGreaterThan(60)

    const failures = samples
      .map((s) => ({ ...s, ...measure(s) }))
      .filter((s) => s.ratio < s.required)
      .map((s) => `${JSON.stringify(s.text)} ${s.ratio.toFixed(2)} < ${s.required}`)

    expect(failures, `${label}: ${samples.length} sampled`).toEqual([])
  })
}

test('every spinbutton on RaidOptimizer has an accessible name', async ({ page }) => {
  await openOptimizer(page)

  const boxes = page.getByRole('spinbutton')
  const count = await boxes.count()
  // Five troop counts, five smithy levels, three defence fields.
  expect(count).toBe(13)

  const unnamed = []
  for (let i = 0; i < count; i += 1) {
    // The name as the browser computes it, through the label ASSOCIATION --
    // not the placeholder and not the value. `el.labels` is empty for a
    // `<label>` that neither wraps the control nor carries `htmlFor`, which is
    // exactly the defect.
    const name = await boxes.nth(i).evaluate((el) => {
      const labelled = el.labels && el.labels.length ? el.labels[0].textContent : ''
      return (el.getAttribute('aria-label') || labelled || '').trim()
    })
    if (!name) unnamed.push(i)
  }
  expect(unnamed, 'spinbuttons with no accessible name').toEqual([])
})
