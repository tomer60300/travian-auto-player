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
 *
 * `overnight` lives here for the same reason `coversSmallHours` does: it is a
 * question ABOUT a profile's hours -- which window is the one the operator
 * sleeps through, so section 6's rules govern it -- and the derivation is pure
 * clock arithmetic. Its request-shape builder sits beside it rather than in
 * `plannerNpc.js`, which is section 7's module; putting a section 6 field
 * there would be the second place a wire format could be got wrong.
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

/** Does this window run past midnight into the next day? */
export function windowWrapsMidnight(hours) {
  const pair = dispatchWindowFor(hours)
  return pair != null && pair[0] > pair[1]
}

/** Is this profile the OVERNIGHT one, which section 6's rules govern?
 *
 * `declared` is the operator's own answer and it wins; the clock only derives.
 * The same asymmetry `npc_attended` has, and the backend's own rule --
 * `is_night_window(window, overnight=...)` in
 * `services/distribution/night_profile.py` returns the declaration first and
 * falls back to the wrap.
 *
 * The derivation is right for a night stated as one 23:00-07:00 window and
 * wrong twice over, which is why the declaration exists:
 *
 *   * a night SPLIT at midnight. 23:00-00:00 is `[1380, 0]` and does wrap, but
 *     00:00-07:00 is `[0, 420]` -- the half that runs up to the morning switch,
 *     wrapping in neither direction. Undeclared, a 600-minute round trip inside
 *     a 420-minute night is not reported as NIGHT_OVERRUN, and with only the
 *     pre-midnight half recognised the 60% morning floor is measured at 00:00
 *     instead of 07:00.
 *   * a near-24h day profile. `[420, 419]` wraps and is not the night.
 */
export function isOvernightProfile(hours, declared) {
  if (typeof declared === 'boolean') return declared
  return windowWrapsMidnight(hours)
}

/** Only the real booleans in a stored overnight map.
 *
 * Its own function rather than a shared boolean filter, on the reasoning
 * `parseOvernight` and `parseAttendance` are two near-identical parsers for:
 * each field owns its own rule, so a change to one cannot silently move the
 * other. This map goes straight into the request, and the backend's lax `bool`
 * would read a stored `"yes"` -- from a hand-edited origin, or a build that
 * stored it differently -- as a night nobody declared, which puts section 6's
 * closing deadline on the wrong profile. Dropped silently, because absent is
 * the resting state and the panel says which way it then derives.
 */
export function overnightMapOnly(stored) {
  const out = {}
  for (const [name, value] of Object.entries(stored ?? {})) {
    if (typeof value === 'boolean') out[name] = value
  }
  return out
}

/** `overnight` for a request body or a segment, or nothing at all.
 *
 * Omitted when nothing was declared, because absent is what asks the backend
 * to derive it -- and the derivation is right for the common case, so sending
 * a computed copy of it would only make the request look like a decision.
 *
 * Omitted with no window too, and here the rule is the backend's own refusal
 * rather than this module's taste: `_overnight_needs_hours_to_be_overnight`
 * raises on `overnight` without `dispatch_window`, because section 6's
 * deadline is measured against the window's END and a declaration with no
 * window decides nothing. (This is NOT the rule `npcAttendedField` used to
 * have and lost -- that one had no backend behind it and inverted a stated
 * answer. This one is a 422 if ignored.)
 */
export function overnightField({ declared, hasWindow }) {
  if (!hasWindow) return {}
  if (typeof declared !== 'boolean') return {}
  return { overnight: declared }
}

/** The overnight answer in words, so it is never carried by a widget position.
 *
 * Says WHERE the answer came from as well as what it is, because a derived
 * answer and a declared one are the same boolean with different authority --
 * and the whole reason the field exists is that the derivation is wrong for a
 * split night.
 */
export function describeOvernight(declared, hours) {
  if (typeof declared === 'boolean') {
    return declared ? 'you said this is the night' : 'you said this is not the night'
  }
  if (dispatchWindowFor(hours) == null) return 'no hours, so nothing to read it from'
  return windowWrapsMidnight(hours)
    ? 'from the hours: this is the night'
    : 'from the hours: not the night'
}
