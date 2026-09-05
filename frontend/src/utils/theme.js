/**
 * Which theme the app is in, and how it got there.
 *
 * index.css has carried a complete `[data-theme="dark"]` token set for as long
 * as it has existed, and **nothing in the app ever set that attribute** -- the
 * wave-4 census grepped the whole of `src/` and found no `setAttribute`, no
 * `documentElement` write, no toggle. It was real, committed, tested CSS that
 * no user could ever reach, which is also why two AA failures could sit in it
 * unnoticed (`.btn-danger` at 1.70:1, RaidOptimizer's colour maps).
 *
 * Three preferences, not two:
 *
 *   'system'  follow `prefers-color-scheme`, and keep following it if the
 *             device flips at sunset. The default, and the only one that is
 *             not stored -- an absent key means "no opinion", which is exactly
 *             what it says.
 *   'light'   pin light, whatever the device says.
 *   'dark'    pin dark.
 *
 * The RESOLUTION is done here in JS rather than by duplicating the dark token
 * block under `@media (prefers-color-scheme: dark)`: the attribute is the
 * single switch either way, one source of truth for the tokens, and a spec can
 * assert on `data-theme` without having to know which mechanism set it.
 *
 * `applyTheme` is called from `main.jsx` at module scope, BEFORE React renders,
 * so a dark-preferring device does not see a frame of light first.
 *
 * Named exports, per the frontend convention for `src/utils/`.
 */

export const THEME_KEY = 'theme'

/** Preference order for the cycling control: system -> light -> dark -> ... */
export const THEME_ORDER = ['system', 'light', 'dark']

const LABELS = {
  system: 'matching your device',
  light: 'light',
  dark: 'dark',
}

/** What the device asks for right now. */
export function systemTheme() {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    // No `matchMedia` at all (a very old browser, or a test environment that
    // did not stub it). Light is the app's historical appearance.
    return 'light'
  }
}

/** The stored preference, or 'system' when there is none or it is unreadable
 *  (private browsing can make `localStorage` throw on read, not only on write). */
export function readThemePreference() {
  try {
    const stored = localStorage.getItem(THEME_KEY)
    return THEME_ORDER.includes(stored) ? stored : 'system'
  } catch {
    return 'system'
  }
}

export function storeThemePreference(preference) {
  try {
    if (preference === 'system') localStorage.removeItem(THEME_KEY)
    else localStorage.setItem(THEME_KEY, preference)
  } catch {
    // A pinned theme that cannot be remembered is still worth applying for
    // this session.
  }
}

/** The theme a preference resolves to. */
export function resolveTheme(preference) {
  return preference === 'system' ? systemTheme() : preference
}

/** Write the resolved theme onto `<html>`, which is the only thing index.css
 *  reads. Always an explicit value: leaving the attribute off would mean light,
 *  which is wrong for a dark-preferring device. */
export function applyTheme(preference = readThemePreference()) {
  const resolved = resolveTheme(preference)
  document.documentElement.setAttribute('data-theme', resolved)
  return resolved
}

/** The next preference in the cycle. */
export function nextTheme(preference) {
  return THEME_ORDER[(THEME_ORDER.indexOf(preference) + 1) % THEME_ORDER.length]
}

/** The control's accessible name: what the theme IS, then what activating it
 *  will do. Both halves matter -- a toggle that only says its destination
 *  leaves a screen-reader user unable to find out the current state. */
export function themeButtonLabel(preference) {
  return `Theme: ${LABELS[preference]}. Activate for ${LABELS[nextTheme(preference)]}.`
}
