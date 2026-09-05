/**
 * `.btn-danger` has to be readable in BOTH themes.
 *
 * The rule (`index.css`, `.btn-danger`) painted hardcoded `#FFFFFF` text on
 * `var(--md-error)`. `--md-error` is `#BA1A1A` in light theme -- a dark red,
 * fine under white -- but `#FFB4AB` in dark, MD3's on-dark-surface TEXT tone,
 * which is a pale salmon. White on pale salmon measures 1.7:1 against a 4.5:1
 * requirement, and the census confirmed it identically on every `.btn-danger`
 * in the app: Connect's "Delete", FarmLists' "Delete", FarmBuilder's
 * "Remove"/"×", OasisRaider's "x", Logs' "Clear All", Sessions' "Stop All".
 * One rule, six-plus unreadable buttons.
 *
 * The `:hover` background was the same defect one step along: a literal
 * `#D32F2F` that does not flip either, so it stayed a light red under whatever
 * the resting colour became.
 *
 * Measured on Logs, which is the cheapest page carrying one -- a pure client
 * store, so "Clear All" renders with no fetch at all. The fix is a CSS rule,
 * not a page, so one page proves it for all of them.
 *
 * NO BACKEND AND NO GAME REQUEST: `/users/me` and `/travian/status` are
 * fulfilled from literals and every other `/api/**` call is aborted. There is
 * a live Travian account on this machine.
 */

import { expect, test } from '@playwright/test'

import { forceTheme, measureContrast } from './contrast'

const PLAYER = 'e2e-operator'
const CAPITAL = 20002

async function isolateApi(page) {
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/users/me')) {
      return route.fulfill({ json: { id: 1, username: PLAYER, is_active: true } })
    }
    if (path.endsWith('/travian/status')) {
      return route.fulfill({
        json: {
          connected: true,
          server_url: 'https://ts2.x1.europe.travian.com',
          player_name: PLAYER,
          tribe_id: 1,
          active_village_id: CAPITAL,
          villages: [{ id: CAPITAL, name: '02', x: 0, y: 0 }],
        },
      })
    }
    return route.abort('blockedbyclient')
  })
}

async function openLogs(page) {
  await isolateApi(page)
  await page.routeWebSocket(/.*/, (ws) => ws.close())
  await page.addInitScript(() => localStorage.setItem('token', 'e2e-not-a-real-token'))
  await page.goto('/logs')
  const clearAll = page.getByRole('button', { name: 'Clear All' })
  await expect(clearAll).toBeVisible()
  return clearAll
}

for (const theme of [null, 'dark']) {
  const label = theme ?? 'light'

  test(`.btn-danger label meets AA on its own fill — ${label} theme`, async ({ page }) => {
    const clearAll = await openLogs(page)
    await forceTheme(page, theme)

    const measured = await measureContrast(clearAll)
    expect(
      measured.ratio,
      `${label}: rgb(${measured.color.slice(0, 3)}) on rgb(${measured.background.slice(0, 3)})`,
    ).toBeGreaterThanOrEqual(4.5)
  })

  test(`.btn-danger label meets AA while hovered — ${label} theme`, async ({ page }) => {
    const clearAll = await openLogs(page)
    await forceTheme(page, theme)
    await clearAll.hover()
    // The hover background is itself a 300ms transition.
    await page.waitForTimeout(500)

    const measured = await measureContrast(clearAll)
    expect(
      measured.ratio,
      `${label} hover: rgb(${measured.color.slice(0, 3)}) on rgb(${measured.background.slice(0, 3)})`,
    ).toBeGreaterThanOrEqual(4.5)
  })
}
