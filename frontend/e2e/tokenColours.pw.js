/**
 * Colours that were not tokens, and what was wrong with each of them.
 *
 * `frontend/CLAUDE.md`'s rule is "components consume tokens only", and the
 * reason is stated there too: there is no `@theme` block in index.css, so
 * Tailwind's own palette is NOT wired to the token set. `text-gray-400` and
 * `var(--text-secondary)` are unrelated values and only one of them flips with
 * `[data-theme]`. A palette utility is therefore a theme bug that has not been
 * reported yet.
 *
 * Three separate failure modes are asserted here, because they fail
 * differently and a "renders some colour" check would pass all three:
 *
 *   1. **Does not flip.** Logs' five level badges were Tailwind palette
 *      classes; OasisRaider's log categories were four more. Asserted by
 *      measuring the same element in both themes and requiring the colour to
 *      CHANGE, then requiring AA in each.
 *   2. **Never resolved at all.** `var(--color-border, #333)` and
 *      `var(--color-danger, #e53e3e)` name properties this app has never
 *      declared, so the hardcoded fallback always fired -- a near-black
 *      hairline and a red that matches nothing else in the app. Asserted
 *      against the real token's value.
 *   3. **Rendered nothing.** `bg-danger` is declared neither in index.css nor
 *      by Tailwind (a shadeless palette name is not a utility), so the
 *      sidebar's unread-log badge was white text on no background at all.
 *      Asserted as a real, opaque fill behind it.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test tokenColours
 */

import { expect, test } from '@playwright/test'

import { isolateApp } from './appHarness'
import { forceTheme, measureContrast, parseColor } from './contrast'

/** The value a token resolves to right now, read the way the browser reads it. */
async function tokenValue(page, name) {
  return page.evaluate((prop) => {
    const probe = document.createElement('div')
    probe.style.color = `var(${prop})`
    document.body.appendChild(probe)
    const value = getComputedStyle(probe).color
    probe.remove()
    return value
  }, name)
}

const LEVELS = ['debug', 'info', 'success', 'warning', 'error']

/** The log store is client-only and has no persistence, so entries arrive the
 *  one way they ever arrive: over `/ws/logs`, which `Layout` opens as soon as
 *  the account is connected. Registered AFTER `isolateApp`, whose catch-all
 *  socket route would otherwise win. */
async function openLogsWithEntries(page) {
  await isolateApp(page)
  await page.routeWebSocket(/.*/, (ws) => {
    if (!new URL(ws.url()).pathname.endsWith('/ws/logs')) return ws.close()
    ws.send(
      JSON.stringify({
        type: 'history',
        entries: LEVELS.map((level) => ({
          level,
          source: 'server',
          message: `a ${level} line`,
        })),
      }),
    )
  })
  await page.goto('/logs')
  await expect(page.getByRole('heading', { name: 'Activity Log' })).toBeVisible()
  await expect(page.getByText('a error line')).toBeVisible()
}

test('Logs: every level badge flips with the theme and meets AA in both', async ({ page }) => {
  await openLogsWithEntries(page)

  // The five level badges by their exact classes. A `[class*="status-"]`
  // match also picks up `.status-dot` and the source chips, which are a
  // different mechanism and not what this asserts.
  const badges = page.locator(
    '.ws-panel span.status-idle, .ws-panel span.status-running, .ws-panel span.status-success, .ws-panel span.status-waiting, .ws-panel span.status-error',
  )
  await expect(badges.first()).toBeVisible()
  const count = await badges.count()
  // At least one per level. The stream can deliver its history more than once
  // (React StrictMode remounts the effect that opens it), and duplicate rows
  // are as worth measuring as the first ones.
  expect(count, 'at least one badge per seeded level').toBeGreaterThanOrEqual(LEVELS.length)
  const classes = new Set(
    await badges.evaluateAll((els) => els.map((el) => el.className.split(' ').find((c) => c.startsWith('status-')))),
  )
  expect([...classes].sort()).toEqual([
    'status-error', 'status-idle', 'status-running', 'status-success', 'status-waiting',
  ])

  const light = []
  for (let i = 0; i < count; i += 1) light.push(await measureContrast(badges.nth(i)))

  await forceTheme(page, 'dark')
  const dark = []
  for (let i = 0; i < count; i += 1) dark.push(await measureContrast(badges.nth(i)))

  for (let i = 0; i < count; i += 1) {
    const l = light[i]
    const d = dark[i]
    expect(l.ratio, `badge ${i} light`).toBeGreaterThanOrEqual(4.5)
    expect(d.ratio, `badge ${i} dark`).toBeGreaterThanOrEqual(4.5)
    // The point: a palette utility measures IDENTICALLY in both themes.
    expect(
      l.color.join(','),
      `badge ${i} did not flip: ${l.color} in both themes`,
    ).not.toBe(d.color.join(','))
  }
})

test('the dead `--color-*` references resolve to real tokens now', async ({ page }) => {
  await isolateApp(page, {
    '/sessions': [
      {
        id: 'sess-1',
        label: 'Build queue run',
        session_type: 'queue',
        status: 'disconnected',
        created_at: new Date().toISOString(),
        message_count: 3,
      },
    ],
  })
  await page.goto('/sessions')

  const card = page.locator('.bg-surface.border-default').first()
  await expect(card).toBeVisible()

  const border = await card.evaluate((el) => getComputedStyle(el).borderTopColor)
  const real = await tokenValue(page, '--border')
  expect(border).toBe(real)
  // The fallback that used to fire, spelled out so a regression is obvious.
  expect(parseColor(border).slice(0, 3)).not.toEqual([51, 51, 51])
})

test("Layout: the sidebar's unread-log badge has a background at all", async ({ page }) => {
  // Seeded over the same socket: `serverLogCount` only moves on a
  // server-origin entry, and the badge only renders when it is non-zero.
  // Opened on a page that is NOT /logs, because visiting /logs resets it.
  await isolateApp(page)
  await page.routeWebSocket(/.*/, (ws) => {
    if (!new URL(ws.url()).pathname.endsWith('/ws/logs')) return ws.close()
    ws.send(
      JSON.stringify({
        type: 'history',
        entries: LEVELS.map((level) => ({ level, source: 'server', message: `a ${level} line` })),
      }),
    )
  })
  await page.goto('/sessions')
  await expect(page.getByRole('heading', { name: 'Sessions' })).toBeVisible()

  const badge = page.locator('.sidebar .nav-link span').filter({ hasText: /^\d+\+?$/ }).first()
  await expect(badge).toBeVisible()

  const fill = parseColor(await badge.evaluate((el) => getComputedStyle(el).backgroundColor))
  // `bg-danger` is declared neither in index.css nor by Tailwind, so this used
  // to be `rgba(0, 0, 0, 0)` -- white text on nothing.
  expect(fill[3], 'the badge draws a real fill').toBe(1)

  const measured = await measureContrast(badge)
  expect(measured.ratio, 'unread count on its own fill').toBeGreaterThanOrEqual(4.5)
})
