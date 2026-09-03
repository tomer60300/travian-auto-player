/** The planner's clock arithmetic, in one place.
 *
 * `hhmmToMinutes` and the 1440 beside it were module-level constants inside
 * `pages/ResourcePlanner.jsx`, which made them unreachable from a test and
 * unreachable from the components the page delegates to. Both are now needed by
 * `plannerNpc.js` -- whether a profile is one the operator sleeps through is a
 * question about its hours -- so they live here rather than being copied.
 *
 * A window may WRAP past midnight and that is the normal case, not an edge one:
 * the account's night profile is 23:00-07:00. So every length here is computed
 * modulo the day, and a pair whose two halves are equal is refused rather than
 * read as either zero or the whole day -- the backend refuses a zero-width
 * dispatch window outright, and guessing which of the two the operator meant is
 * how a profile comes to plan for hours it does not own.
 */

export const MINUTES_IN_DAY = 1440

/** `'23:00'` to 1380, or null for anything that is not a clock time. */
export function hhmmToMinutes(text) {
  const [h, m] = String(text).split(':').map(Number)
  if (!Number.isInteger(h) || !Number.isInteger(m)) return null
  if (h < 0 || h > 23 || m < 0 || m > 59) return null
  return h * 60 + m
}

/** Minutes past midnight back to `'HH:MM'`, or `'—'` when there is no minute. */
export function minutesToHhmm(minute) {
  if (!Number.isFinite(minute)) return '—'
  const wrapped = ((Math.round(minute) % MINUTES_IN_DAY) + MINUTES_IN_DAY) % MINUTES_IN_DAY
  return `${String(Math.floor(wrapped / 60)).padStart(2, '0')}:${String(wrapped % 60).padStart(2, '0')}`
}

/** An `['HH:MM', 'HH:MM']` pair as the backend's minute pair, or null.
 *
 * Null covers all three unusable shapes -- missing, unparseable, zero-width --
 * because the request treats them identically: the field is omitted and the
 * route set runs round the clock.
 */
export function dispatchWindowFor(hours) {
  if (!Array.isArray(hours) || hours.length !== 2) return null
  const from = hhmmToMinutes(hours[0])
  const to = hhmmToMinutes(hours[1])
  if (from == null || to == null || from === to) return null
  return [from, to]
}

/** How many minutes of the day a window covers, wrapping past midnight. */
export function windowMinutes(hours) {
  const pair = dispatchWindowFor(hours)
  if (pair == null) return null
  return ((pair[1] - pair[0]) % MINUTES_IN_DAY + MINUTES_IN_DAY) % MINUTES_IN_DAY
}

/** The share of the day a window owns, 0..1. Null when it owns no hours. */
export function windowDayShare(hours) {
  const minutes = windowMinutes(hours)
  return minutes == null ? null : minutes / MINUTES_IN_DAY
}

/** The small hours: 01:00 to 05:00, when nobody is at a marketplace.
 *
 * Deliberately narrow. A window ending at 23:30 or starting at 06:00 tells us
 * nothing about whether the operator is awake -- plenty of people are -- so the
 * clock is only allowed to speak where it actually has something to say. Used
 * to SUGGEST an attendance answer and label the day/night asymmetry, never to
 * supply one: "the operator is asleep" is a fact about the operator, and
 * `npc_attended` exists precisely because it cannot be read off a clock.
 */
export function coversSmallHours(hours) {
  const pair = dispatchWindowFor(hours)
  if (pair == null) return false
  const [from, to] = pair
  const inside = (minute) =>
    from < to ? minute >= from && minute < to : minute >= from || minute < to
  // Sampled on the hour across 01:00-04:00, which is enough: a window is a
  // contiguous arc, so if it holds any of the small hours it holds one of these.
  return [60, 120, 180, 240].some(inside)
}
