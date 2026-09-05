/**
 * The app's own navigation chrome, against items 2 and 4 of the UI Definition
 * of Done. Three separate defects, one surface.
 *
 * 1. NO FOCUS RING. `index.css` wires its purple ring
 *    (`outline: 2px solid var(--md-primary)`) to five classes -- `.btn-primary`,
 *    `.btn-secondary`, `.input-field`, `.input-sm`, `.tab-btn` -- plus
 *    `.link-action`. The PRIMARY NAVIGATION carries none of them: the 14
 *    sidebar `.nav-link`s, the sidebar-collapse `«`/`»`, the mobile hamburger
 *    `☰`, and all five bottom-tab controls. A live Tab traversal on every one of
 *    the 13 Layout-wrapped pages measured `outline-style: auto` -- Chromium's
 *    plain default -- on each. Same class of gap as "Lift restriction" (see
 *    `focusRing.pw.js`), over the whole of the app's navigation.
 *
 * 2. HAMBURGER 18x44. It picks up the generic `@media (max-width: 767px)
 *    { button { min-height: 44px } }` rule for its height and nothing at all
 *    for its width, so it measured 18 wide under touch emulation at 375.
 *
 * 3. ONE BACKWARD TAB JUMP, on all 13 pages at <=767px. `MobileNav` rendered
 *    BEFORE `<main>` in `Layout.jsx`'s DOM while being pinned to the BOTTOM of
 *    the viewport by CSS, so Tab left the top bar for the bottom bar and then
 *    jumped back up to the page's first control. Asserted here as the tab-stop
 *    ORDER (every bottom-bar stop after every stop inside `<main>`) rather than
 *    as a coordinate delta: the bar is `position: fixed`, so its document-space
 *    y is a function of the scroll position the walk happens to be in, and
 *    ordering is the property the fix actually establishes.
 *
 * Measured on Dashboard, which is one of the 13 and the page the census walked.
 * All three fixes are in `Layout.jsx`/`index.css`, so one page proves them.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test navChrome
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, isolateApp } from './appHarness'

const DASHBOARD = {
  '/buildings/resources': { wood: 1200, clay: 1100, iron: 900, crop: 800, warehouse: 8000, granary: 8000 },
  '/buildings/queue': { village_id: CAPITAL, queue: [] },
  '/buildings': { village_id: CAPITAL, buildings: [] },
}

/** The resolved `--md-primary`, read through a real computed style so it is
 *  directly comparable with a computed `outline-color`. Same probe as
 *  `focusRing.pw.js`. */
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
    return { style: cs.outlineStyle, color: cs.outlineColor, width: cs.outlineWidth }
  })
}

/** POLLED, not sampled once. `.nav-link` declares `transition: all 200ms`,
 *  which includes `outline-color`, so the ring fades in from whatever the
 *  previous value was: read immediately after `.focus()`, the first sidebar
 *  link measured `rgb(74,58,116)` -- a blend of Chromium's near-black default
 *  and the app's purple, neither of them. Same mid-transition trap the census
 *  documented for theme flips. */
async function expectAppRing(page, locator, primary) {
  await locator.focus()
  await expect(locator).toBeFocused()
  await expect
    .poll(() => ring(locator))
    .toEqual({ style: 'solid', color: primary, width: '2px' })
}

async function openDashboard(page) {
  await isolateApp(page, DASHBOARD)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 2 })).toBeVisible()
}

/** Where the caret goes, by pressing Tab, tagged with whether the stop is
 *  inside `<main>` or inside the pinned bottom bar. */
async function tabStops(page, limit = 120) {
  await page.evaluate(() => document.body.focus())
  await page.keyboard.press('Tab')
  const stops = []
  const seen = new Set()
  for (let i = 0; i < limit; i += 1) {
    const stop = await page.evaluate(() => {
      const el = document.activeElement
      if (!el || el === document.body) return null
      const path = []
      for (let n = el; n && n !== document.body; n = n.parentElement) path.push(n.tagName)
      const label = el.getAttribute('aria-label') || (el.textContent || '').trim()
      return {
        key: path.join('/') + '|' + label.slice(0, 40),
        name: label.slice(0, 40) || '<' + el.tagName.toLowerCase() + '>',
        inMain: !!el.closest('main'),
        inBottomBar: !!el.closest('.bottom-tab-bar'),
      }
    })
    if (stop == null) break
    if (seen.has(stop.key)) break
    seen.add(stop.key)
    stops.push(stop)
    await page.keyboard.press('Tab')
  }
  return stops
}

test.describe('desktop navigation chrome', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('every sidebar link and the collapse toggle carry the app ring', async ({ page }) => {
    await openDashboard(page)
    const primary = await primaryRingColor(page)

    const links = page.locator('.sidebar .nav-link')
    await expect(links).toHaveCount(14)
    for (const link of await links.all()) {
      await expectAppRing(page, link, primary)
    }

    // Located by `title` rather than by role+name: the button's text content is
    // `«`, which wins over `title` for the accessible name, so `«` IS its name.
    // Reported, not fixed here -- naming it is not one of this spec's three
    // defects and `.link-action` is what gives it the ring.
    await expectAppRing(page, page.locator('.sidebar button[title="Collapse sidebar"]'), primary)
  })
})

test.describe('mobile navigation chrome', () => {
  test.use({ viewport: { width: 375, height: 900 }, hasTouch: true, isMobile: true })

  test('the hamburger is at least 44x44 and carries the app ring', async ({ page }) => {
    await openDashboard(page)
    const primary = await primaryRingColor(page)

    const hamburger = page.locator('.sidebar-toggle')
    await expect(hamburger).toBeVisible()

    const box = await hamburger.boundingBox()
    expect(box.width, `hamburger width ${box.width}`).toBeGreaterThanOrEqual(44)
    expect(box.height, `hamburger height ${box.height}`).toBeGreaterThanOrEqual(44)

    await expectAppRing(page, hamburger, primary)
  })

  test('all five bottom-tab controls carry the app ring', async ({ page }) => {
    await openDashboard(page)
    const primary = await primaryRingColor(page)

    const tabs = page.locator('.bottom-tab-bar a, .bottom-tab-bar button')
    await expect(tabs).toHaveCount(5)
    for (const tab of await tabs.all()) {
      await expectAppRing(page, tab, primary)
    }
  })

  test('Tab reaches the pinned bottom bar after the page content, not before it', async ({
    page,
  }) => {
    await openDashboard(page)
    const stops = await tabStops(page)

    const bottomBar = stops.filter((s) => s.inBottomBar)
    const main = stops.filter((s) => s.inMain)
    expect(bottomBar.length, 'bottom-bar stops reached').toBe(5)
    expect(main.length, 'in-main stops reached').toBeGreaterThan(0)

    const firstBar = stops.findIndex((s) => s.inBottomBar)
    const lastMain = stops.map((s) => s.inMain).lastIndexOf(true)
    expect(
      firstBar,
      `order: ${stops.map((s, i) => `${i}:${s.name}${s.inBottomBar ? '[bar]' : s.inMain ? '[main]' : ''}`).join(' ')}`,
    ).toBeGreaterThan(lastMain)
  })
})
