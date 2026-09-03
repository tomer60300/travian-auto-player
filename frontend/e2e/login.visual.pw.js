/**
 * Visual regression on the login page, three viewports.
 *
 * Why /login: it is the only page that renders without auth, so this spec needs no fixture, no
 * token and no backend. It is also the shape to copy for a second route -- an authed one needs
 * a storageState fixture, which is the piece this file does not carry.
 *
 * (The header said TEMPLATE and "Playwright is NOT installed in this project" in the same commit
 * that installed Playwright, committed four baselines, and rewrote ../playwright.config.js to
 * say the opposite. The dependency is real; see that config's header for the commands.)
 *
 * What a visual regression test is for: catching the layout you did not mean to change. It is not
 * a substitute for the accessibility and Core Web Vitals checks in the UI Definition of Done —
 * run `/ux-audit http://localhost:5173/login` for those. A screenshot diff is blind to contrast,
 * focus order and accessible names; it is very good at noticing that a card grew 40px.
 *
 * Baselines live in e2e/login.visual.pw.js-snapshots/ and are committed. A diff is a failure
 * until a human agrees the new layout is the intended one and reruns with --update-snapshots.
 */

import { expect, test } from '@playwright/test'

const ROUTE = '/login'

// Every viewport in the UI Definition of Done. 375px is iPhone SE/12 mini width — the narrowest
// device the operator actually uses; 768px is the tablet breakpoint boundary in index.css;
// 1440px is the desktop layout with the 220px sidebar.
const VIEWPORTS = [
  { name: 'mobile-375', width: 375, height: 812 },
  { name: 'tablet-768', width: 768, height: 1024 },
  { name: 'desktop-1440', width: 1440, height: 900 },
]

/**
 * Two sources of pixel noise have to go before a screenshot is comparable run to run: the caret
 * blink in a focused input, and any in-flight CSS animation. Playwright's `animations: 'disabled'`
 * handles declarative CSS; the theme pin is here because index.css swaps every colour token on
 * `[data-theme="dark"]`, so an operator's remembered theme would rewrite the whole baseline.
 */
async function settle(page) {
  await page.addStyleTag({
    content: `*, *::before, *::after {
      animation: none !important;
      transition: none !important;
      caret-color: transparent !important;
    }`,
  })
  await page.evaluate(() => {
    document.documentElement.removeAttribute('data-theme')
  })
  await page.waitForLoadState('networkidle')
}

for (const vp of VIEWPORTS) {
  test.describe(`login @ ${vp.name}`, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } })

    test('default state matches baseline', async ({ page }) => {
      await page.goto(ROUTE)
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(
        'Travian Auto Player',
      )
      await settle(page)

      await expect(page).toHaveScreenshot(`login-${vp.name}.png`, {
        fullPage: true,
      })
    })

    test('page does not scroll horizontally', async ({ page }) => {
      await page.goto(ROUTE)
      await settle(page)

      // UI Definition of Done #1. Cheap, deterministic, and the failure a screenshot diff is
      // worst at surfacing — an overflowing child can leave the visible crop looking correct.
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      expect(
        overflow.scrollWidth,
        `page overflows by ${overflow.scrollWidth - overflow.clientWidth}px at ${vp.width}px`,
      ).toBeLessThanOrEqual(overflow.clientWidth)
    })
  })
}

test.describe('login error state @ mobile-375', () => {
  test.use({ viewport: { width: 375, height: 812 } })

  test('validation error matches baseline', async ({ page }) => {
    await page.goto(ROUTE)

    // Client-side validation only — `validate()` in Login.jsx rejects a username under 3 chars
    // before submit reaches the network. No backend, no credentials, no request that could ever
    // leave this machine. That last part is not optional in this repo.
    await page.getByLabel('Username').fill('ab')
    await page.getByLabel('Password').fill('short')
    await page.getByRole('button', { name: 'Sign In' }).click()

    await expect(
      page.getByText('Username must be between 3 and 32 characters'),
    ).toBeVisible()
    await settle(page)

    await expect(page).toHaveScreenshot('login-error-mobile-375.png', {
      fullPage: true,
    })
  })
})
