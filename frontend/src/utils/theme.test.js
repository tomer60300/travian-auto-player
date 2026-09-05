/**
 * The theme preference, unit-tested.
 *
 * This suite runs in vitest's default NODE environment -- there is no jsdom in
 * this repo, which is why every component test uses `renderToString` -- so
 * `localStorage`, `window` and `document` are installed on `globalThis` here
 * rather than assumed. That is not only scaffolding: `theme.js` has to survive
 * a bare `localStorage` that THROWS (private browsing does exactly that, on
 * read as well as write), and the only way to check that is to control it.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'

import {
  THEME_KEY,
  applyTheme,
  nextTheme,
  readThemePreference,
  resolveTheme,
  storeThemePreference,
  systemTheme,
  themeButtonLabel,
} from './theme'

function fakeStorage() {
  const map = new Map()
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
  }
}

function stubMatchMedia(prefersDark) {
  globalThis.window = {
    matchMedia: () => ({ matches: prefersDark, addEventListener() {}, removeEventListener() {} }),
  }
}

const saved = {}

beforeEach(() => {
  saved.window = globalThis.window
  saved.localStorage = globalThis.localStorage
  saved.document = globalThis.document
  globalThis.localStorage = fakeStorage()
  stubMatchMedia(false)
})

afterEach(() => {
  globalThis.window = saved.window
  globalThis.localStorage = saved.localStorage
  globalThis.document = saved.document
})

describe('theme preference', () => {
  it('defaults to system when nothing is stored', () => {
    expect(readThemePreference()).toBe('system')
  })

  it('ignores a stored value that is not a preference', () => {
    globalThis.localStorage.setItem(THEME_KEY, 'sepia')
    expect(readThemePreference()).toBe('system')
  })

  // "system" is the absence of an opinion, so it is the absence of a key --
  // not a third stored string that a later version would have to migrate.
  it('stores a pin and clears the key for system', () => {
    storeThemePreference('dark')
    expect(globalThis.localStorage.getItem(THEME_KEY)).toBe('dark')
    expect(readThemePreference()).toBe('dark')
    storeThemePreference('system')
    expect(globalThis.localStorage.getItem(THEME_KEY)).toBeNull()
    expect(readThemePreference()).toBe('system')
  })

  it('resolves system through the device and a pin through itself', () => {
    stubMatchMedia(true)
    expect(systemTheme()).toBe('dark')
    expect(resolveTheme('system')).toBe('dark')
    expect(resolveTheme('light')).toBe('light')
    stubMatchMedia(false)
    expect(resolveTheme('system')).toBe('light')
    expect(resolveTheme('dark')).toBe('dark')
  })

  it('survives a storage that throws, in both directions', () => {
    globalThis.localStorage = {
      getItem() { throw new Error('denied') },
      setItem() { throw new Error('denied') },
      removeItem() { throw new Error('denied') },
    }
    expect(readThemePreference()).toBe('system')
    expect(() => storeThemePreference('dark')).not.toThrow()
  })

  it('falls back to light where matchMedia throws', () => {
    globalThis.window = { matchMedia() { throw new Error('no matchMedia') } }
    expect(systemTheme()).toBe('light')
  })

  it('cycles system -> light -> dark -> system', () => {
    expect(nextTheme('system')).toBe('light')
    expect(nextTheme('light')).toBe('dark')
    expect(nextTheme('dark')).toBe('system')
  })

  // Both halves: what it IS and what activating it does. A label that only
  // names the destination leaves a screen-reader user unable to learn the
  // current state at all.
  it('names the current state and the next one', () => {
    expect(themeButtonLabel('system')).toBe('Theme: matching your device. Activate for light.')
    expect(themeButtonLabel('light')).toBe('Theme: light. Activate for dark.')
    expect(themeButtonLabel('dark')).toBe('Theme: dark. Activate for matching your device.')
  })

  // ALWAYS an explicit value, never the absence of the attribute: index.css's
  // dark tokens live under `[data-theme="dark"]` only, so leaving it off would
  // mean light on a dark-preferring device.
  it('writes an explicit resolved theme onto the document element', () => {
    const attrs = {}
    globalThis.document = {
      documentElement: { setAttribute: (k, v) => { attrs[k] = v } },
    }
    stubMatchMedia(true)
    expect(applyTheme('system')).toBe('dark')
    expect(attrs['data-theme']).toBe('dark')
    expect(applyTheme('light')).toBe('light')
    expect(attrs['data-theme']).toBe('light')
  })
})
