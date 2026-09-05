import { useTheme } from '../hooks/useTheme'
import { themeButtonLabel } from '../utils/theme'

const GLYPH = {
  system: '◐', // half-filled circle — neither one nor the other
  light: '☀',  // sun
  dark: '☽',   // crescent moon
}

/**
 * The control that reaches the dark theme.
 *
 * One button, cycling system -> light -> dark, rather than a two-state switch:
 * "follow my device" is a real third answer and the one most people want, and
 * a two-state control cannot express it. It is also the DEFAULT, so the button
 * exists mainly to overrule it.
 *
 * A button and not a `<select>` because it lives in the top bar beside the
 * hamburger, and at 375 that row already holds a hamburger, the app name, the
 * village selector and Logout -- a third box does not fit. The cost is that
 * the next state is not visible until you read the label, which is why the
 * label says both halves ("Theme: light. Activate for dark.").
 *
 * 44x44 and `.link-action` for the app focus ring, per items 2 and 4 of the UI
 * Definition of Done. The glyph is `aria-hidden`: the button's name comes from
 * `aria-label`, and a screen reader announcing "half-filled circle" would be
 * noise on top of it.
 */
export default function ThemeToggle() {
  const { preference, cycle } = useTheme()
  const label = themeButtonLabel(preference)

  return (
    <button
      type="button"
      onClick={cycle}
      className="link-action theme-toggle bg-transparent border-none text-primary cursor-pointer"
      aria-label={label}
      title={label}
    >
      <span aria-hidden="true">{GLYPH[preference]}</span>
    </button>
  )
}
