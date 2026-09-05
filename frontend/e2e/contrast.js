/**
 * WCAG contrast measurement for the Playwright specs, and the one way to reach
 * the dark theme from a spec.
 *
 * Not a spec itself (no `.pw.js` suffix, so neither runner collects it) -- a
 * helper, the same way `plannerHarness.js` is.
 *
 * Why measured and not eyeballed: item 3 of the UI Definition of Done is a
 * NUMBER (4.5:1 for normal text, 3:1 for large text and non-text boundaries),
 * and a token pair that passes in light can fail in dark. `measureContrast`
 * takes the two colours the browser actually computed, composites every
 * translucent background layer down to the first opaque one, and returns the
 * ratio -- so a `color-mix()`, an `rgba()` wash or an inherited background is
 * measured as rendered rather than as written.
 *
 * `forceTheme` exists because of a trap the wave-4 census fell into and
 * documented: setting `data-theme` from `page.addInitScript` does not survive
 * to the settled page under Vite dev, and several rules (`.nav-link`,
 * `.quick-action`, `.btn-*`) declare `transition: all 200ms`/`300ms`, so
 * sampling a colour immediately after flipping the attribute captures a
 * MID-TRANSITION blend that is neither theme's value. It therefore sets the
 * attribute on the settled page and waits past the longest declared
 * transition. `null` restores the light default.
 */

/**
 * Parse a computed colour into 0-255 sRGB plus alpha.
 *
 * Two serialisations, not one: `rgb()`/`rgba()` with 0-255 channels for
 * ordinary declarations, and `color(srgb r g b / a)` with 0-1 channels, which
 * is what Chromium computes a `color-mix(in srgb, ...)` to. Reading the second
 * as if it were the first yields a near-black for every colour -- the trap that
 * made a passing 5.87:1 measure as 1.60:1.
 */
export function parseColor(value) {
  const text = String(value)
  const nums = text.match(/[\d.]+/g)
  if (!nums || nums.length < 3) throw new Error(`not an rgb colour: ${value}`)
  const scale = text.startsWith('color(') ? 255 : 1
  const [r, g, b] = nums.slice(0, 3).map((n) => Number(n) * scale)
  const a = nums.length > 3 ? Number(nums[3]) : 1
  return [r, g, b, a]
}

/** `src` (with alpha) painted over opaque `dst`. */
export function composite(src, dst) {
  const a = src[3]
  return [0, 1, 2].map((i) => src[i] * a + dst[i] * (1 - a)).concat(1)
}

/** WCAG 2.1 relative luminance of an opaque sRGB triple. */
export function luminance([r, g, b]) {
  const lin = [r, g, b].map((c) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
}

/** WCAG 2.1 contrast ratio between two opaque sRGB triples. */
export function ratio(fg, bg) {
  const a = luminance(fg)
  const b = luminance(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

/**
 * The rendered foreground/background pair for one element, and their ratio.
 *
 * `state` is passed through to `getComputedStyle`'s pseudo-element argument
 * slot only for real pseudo-elements; a STATE such as hover is driven by the
 * caller (`locator.hover()`) before calling this.
 */
export async function measureContrast(locator) {
  const sample = await locator.evaluate((el) => {
    const own = getComputedStyle(el)
    const layers = []
    for (let node = el; node; node = node.parentElement) {
      layers.push(getComputedStyle(node).backgroundColor)
    }
    return { color: own.color, fontSize: own.fontSize, fontWeight: own.fontWeight, layers }
  })

  // Walk outwards to the first opaque layer, then paint the translucent ones
  // back over it. Nothing opaque anywhere means the canvas, which is white.
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

  const color = composite(parseColor(sample.color), background)
  return {
    color,
    background,
    fontSize: parseFloat(sample.fontSize),
    fontWeight: Number(sample.fontWeight),
    ratio: ratio(color, background),
  }
}

/** Flip the app to `dark` (or back, with `null`) and let every transition land. */
export async function forceTheme(page, theme) {
  await page.evaluate((next) => {
    if (next) document.documentElement.setAttribute('data-theme', next)
    else document.documentElement.removeAttribute('data-theme')
  }, theme)
  // 300ms is the longest transition declared in index.css (`.btn-*`,
  // `transition: all 300ms`); 500 clears it with room for a slow frame.
  await page.waitForTimeout(500)
}
