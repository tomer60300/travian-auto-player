/**
 * The control that makes the dark theme reachable at all.
 *
 * The wave-4 census's first finding, before any page: `index.css` has carried a
 * complete `[data-theme="dark"]` token set for as long as it has existed, and
 * **nothing in the app ever set that attribute**. A grep of the whole of `src/`
 * found no `setAttribute`, no `documentElement` write, no toggle. It was real,
 * committed, tested CSS that no user of this app could ever see -- which is
 * also how two AA failures came to sit in it unnoticed (`.btn-danger` at
 * 1.70:1, RaidOptimizer's colour maps at 2.17-4.31:1). The census could only
 * measure dark by forcing the attribute from the harness.
 *
 * So the last two tests here matter as much as the first ones: they re-measure
 * those two fixes THROUGH THE CONTROL a person actually has, rather than
 * through a harness poke. A fix that only holds when a test forces the
 * attribute is not a fix.
 *
 * `test.use({ colorScheme })` is Chromium's real `prefers-color-scheme`, so
 * the "follow the device" default is exercised rather than simulated.
 *
 * NO BACKEND AND NO GAME REQUEST: see `appHarness.js`.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test themeControl
 */

import { expect, test } from '@playwright/test'

import { isolateApp } from './appHarness'
import { measureContrast } from './contrast'

const toggle = (page) => page.getByRole('button', { name: /^Theme:/ })
const themeAttr = (page) => page.evaluate(() => document.documentElement.getAttribute('data-theme'))

async function openLogs(page) {
  await isolateApp(page)
  await page.goto('/logs')
  await expect(page.getByRole('heading', { name: 'Activity Log' })).toBeVisible()
}

test.describe('following the device', () => {
  test.use({ colorScheme: 'dark' })

  test('a dark-preferring device gets the dark theme with no stored choice', async ({ page }) => {
    await openLogs(page)
    expect(await themeAttr(page)).toBe('dark')
    await expect(toggle(page)).toHaveAccessibleName(
      'Theme: matching your device. Activate for light.',
    )
    // Nothing stored: "follow the device" is the absence of an opinion.
    expect(await page.evaluate(() => localStorage.getItem('theme'))).toBeNull()
  })
})

test.describe('overruling the device', () => {
  test.use({ colorScheme: 'light' })

  test('a light-preferring device gets light, and the toggle is named for it', async ({ page }) => {
    await openLogs(page)
    expect(await themeAttr(page)).toBe('light')
    await expect(toggle(page)).toHaveAccessibleName(
      'Theme: matching your device. Activate for light.',
    )
  })

  test('the control cycles system -> light -> dark -> system', async ({ page }) => {
    await openLogs(page)
    await toggle(page).click()
    expect(await themeAttr(page)).toBe('light')
    await expect(toggle(page)).toHaveAccessibleName('Theme: light. Activate for dark.')

    await toggle(page).click()
    expect(await themeAttr(page)).toBe('dark')
    await expect(toggle(page)).toHaveAccessibleName('Theme: dark. Activate for matching your device.')

    await toggle(page).click()
    expect(await themeAttr(page)).toBe('light') // the device's own answer
    await expect(toggle(page)).toHaveAccessibleName(
      'Theme: matching your device. Activate for light.',
    )
  })

  test('a pinned theme survives a reload', async ({ page }) => {
    await openLogs(page)
    await toggle(page).click()
    await toggle(page).click()
    expect(await themeAttr(page)).toBe('dark')

    await page.reload()
    await expect(page.getByRole('heading', { name: 'Activity Log' })).toBeVisible()
    // Applied BEFORE React renders, from `main.jsx`, so there is no light
    // frame first.
    expect(await themeAttr(page)).toBe('dark')
    await expect(toggle(page)).toHaveAccessibleName('Theme: dark. Activate for matching your device.')
  })

  test('the control is operable by keyboard, with the app ring', async ({ page }) => {
    await openLogs(page)
    const button = toggle(page)
    await button.focus()
    await expect(button).toBeFocused()

    const ring = await button.evaluate((el) => {
      const cs = getComputedStyle(el)
      return { style: cs.outlineStyle, width: cs.outlineWidth }
    })
    expect(ring).toEqual({ style: 'solid', width: '2px' })

    // Space and Enter both, because a `<button>` that has been rebuilt as a
    // div would answer to neither and this is the only control for the theme.
    await page.keyboard.press('Enter')
    expect(await themeAttr(page)).toBe('light')
    await page.keyboard.press(' ')
    expect(await themeAttr(page)).toBe('dark')
  })

  test('the control is 44x44', async ({ page }) => {
    await openLogs(page)
    const box = await toggle(page).boundingBox()
    expect(box.width).toBeGreaterThanOrEqual(44)
    expect(box.height).toBeGreaterThanOrEqual(44)
  })

  test('there is exactly one of it, at every width', async ({ page }) => {
    await openLogs(page)
    for (const width of [375, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(page.locator('button[aria-label^="Theme:"]:visible')).toHaveCount(1)
    }
  })
})

test.describe('what the dark theme actually looks like, reached the way a person reaches it', () => {
  test.use({ colorScheme: 'light' })

  async function pinDark(page) {
    await toggle(page).click()
    await toggle(page).click()
    expect(await themeAttr(page)).toBe('dark')
    // Past the longest declared transition (300ms), or the sample is a blend
    // of the two themes.
    await page.waitForTimeout(500)
  }

  test('.btn-danger still meets AA — the fix, through the control', async ({ page }) => {
    await openLogs(page)
    await pinDark(page)

    const clearAll = page.getByRole('button', { name: 'Clear All' })
    const measured = await measureContrast(clearAll)
    expect(
      measured.ratio,
      `rgb(${measured.color.slice(0, 3)}) on rgb(${measured.background.slice(0, 3)})`,
    ).toBeGreaterThanOrEqual(4.5)
  })

  test('RaidOptimizer still meets AA — the fix, through the control', async ({ page }) => {
    await isolateApp(page, {
      '/military/troops': { t1: 1000, t6: 1000 },
      '/military/smithy': { found: true, research: { t1: 5, t6: 14 } },
    })
    await page.goto('/raid-optimizer')
    await expect(page.getByRole('heading', { name: 'OPTIMAL DEPLOYMENTS' })).toBeVisible()
    await pinDark(page)

    const failures = await page.evaluate(() => {
      const parse = (v) => {
        const nums = String(v).match(/[\d.]+/g)
        const scale = String(v).startsWith('color(') ? 255 : 1
        return [
          ...nums.slice(0, 3).map((n) => Number(n) * scale),
          nums.length > 3 ? Number(nums[3]) : 1,
        ]
      }
      const lum = ([r, g, b]) =>
        [r, g, b]
          .map((c) => {
            const s = c / 255
            return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
          })
          .reduce((acc, v, i) => acc + v * [0.2126, 0.7152, 0.0722][i], 0)
      const over = (src, dst) => [0, 1, 2].map((i) => src[i] * src[3] + dst[i] * (1 - src[3]))
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
        if (!rect.width || !rect.height) continue
        const cs = getComputedStyle(el)
        if (cs.visibility === 'hidden' || cs.opacity === '0') continue
        let bg = [255, 255, 255]
        const stack = []
        for (let n = el; n; n = n.parentElement) {
          const p = parse(getComputedStyle(n).backgroundColor)
          if (p[3] === 1) { bg = p.slice(0, 3); break }
          stack.push(p)
        }
        for (const layer of stack.reverse()) bg = over(layer, bg)
        const fg = parse(cs.color)
        fg[3] *= Number(cs.opacity)
        const colour = over(fg, bg)
        const a = lum(colour)
        const b = lum(bg)
        const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
        const size = parseFloat(cs.fontSize)
        const need = size >= 24 || (size >= 18.66 && Number(cs.fontWeight) >= 700) ? 3 : 4.5
        if (ratio < need) out.push(`${text.slice(0, 30)} ${ratio.toFixed(2)} < ${need}`)
      }
      return out
    })

    expect(failures).toEqual([])
  })
})
