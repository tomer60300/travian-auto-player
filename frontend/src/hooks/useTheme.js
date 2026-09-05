import { useCallback, useEffect, useState } from 'react'

import { applyTheme, nextTheme, readThemePreference, storeThemePreference } from '../utils/theme'

/**
 * The theme preference, as React state.
 *
 * The effect only writes to an external system (`<html>`'s `data-theme`) and
 * subscribes to one (`prefers-color-scheme`), which is what an effect is for.
 * There is deliberately no `resolved` state: nothing renders differently per
 * theme -- the CSS does all of that from the attribute -- so mirroring the
 * resolved value into React would be a second source of truth and a cascading
 * render for nothing.
 *
 * The media subscription exists only while the preference is 'system', so a
 * device that flips at sunset flips the app with it. A pinned preference is
 * the user overruling the device, and stays pinned.
 *
 * Named export, per the frontend convention for `src/hooks/`.
 */
export function useTheme() {
  const [preference, setPreference] = useState(readThemePreference)

  useEffect(() => {
    applyTheme(preference)
    if (preference !== 'system') return
    let media
    try {
      media = window.matchMedia('(prefers-color-scheme: dark)')
    } catch {
      return
    }
    const onChange = () => applyTheme('system')
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [preference])

  const cycle = useCallback(() => {
    setPreference((current) => {
      const next = nextTheme(current)
      storeThemePreference(next)
      return next
    })
  }, [])

  return { preference, cycle }
}
