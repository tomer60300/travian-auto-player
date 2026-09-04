/** Section 7's NPC balancing, from the operator's side of it.
 *
 * Two owned answers live here, and neither could be given from this app at all
 * until now -- so a stock floor plus a day/night profile came back **422** and
 * the page had no control to fix it with.
 *
 * **`npc_attended`, per profile.** Whether the operator is at the marketplace
 * during a profile's hours. The backend requires it -- 422 without it -- as
 * soon as any village keeps a `stock_floor_fraction` above 0 and the request
 * carries a `dispatch_window`, and it is deliberately not defaulted: this
 * account is ASLEEP through the night window, so a guessed `true` would fund
 * night routes out of trading nobody is doing, which is the plan promising
 * cargo that does not exist. A guessed `false` is no better in the other
 * direction -- it withdraws the day's conversion allowance and reports
 * shortfalls the account does not have.
 *
 * So unanswered is a THIRD STATE here, everywhere, and it is never collapsed
 * into either boolean. `attendanceFor` returns null for it, `npcAttendedField`
 * omits the field rather than inventing one, and `unansweredAttendance` is what
 * the page gates the Build button on -- a named question on screen beats a 422
 * naming village ids.
 *
 * A WINDOWLESS profile is not exempt from any of that, which is the correction
 * this module carries. The 422 does not fire there -- the request would be
 * accepted -- but a missing answer is now read as UNATTENDED, so a profile with
 * no hours needs the answer just as much and gets a zero allowance without it.
 * That is why `npcAttendedField` sends a boolean whether or not there is a
 * window, and why `suggestedAttendance` guesses asleep for one: round the clock
 * is all 24 hours, not none of them.
 *
 * The clock may SUGGEST (`suggestedAttendance`) and may never decide. That
 * asymmetry is the whole reason the field exists.
 *
 * **`npc_feedstock`, per village.** Which of a village's stores NPC may convert
 * FROM, overriding the derivation. Left unset the feedstock is everything the
 * village is not drawing on, which is the honest default and what section 7
 * describes for 02 (clay and crop into wood) -- so "derived" is the resting
 * state and reads as an answer rather than as an empty box. An EMPTY list is
 * the picker mid-edit and not a statement: NPC exchanges one resource for
 * another and cannot convert from nothing, so it is dropped from the request
 * exactly as an empty `relay_for` is.
 */

import { RESOURCE_LABEL, RESOURCES } from '../constants/planner'
import { coversSmallHours, dispatchWindowFor } from './plannerClock'

/** The four stores, in the game's own order. */
export const NPC_FEEDSTOCK_RESOURCES = Object.freeze([...RESOURCES])

/** Does this account's setup make attendance a question the operator must answer?
 *
 * The backend's own predicate: a `stock_floor_fraction` above zero on any
 * village. 0.0 is not a floor (`0.0 is None` at every layer), so a village
 * whose fraction is zero declares nothing and asks nothing.
 */
export function attendanceRequired(stockFloors) {
  return Object.values(stockFloors ?? {}).some((floor) => Number(floor) > 0)
}

/** One profile's stored answer, or null when nothing has been said.
 *
 * Strictly a boolean or null. A stored string -- which is what a hand-edited
 * localStorage value or an older build would leave behind -- reads as
 * unanswered rather than as truthy, for the same reason `relayFlagsOnly` drops
 * one: the backend's lax `bool` would read "yes" as an attendance nobody
 * declared.
 */
export function attendanceFor(profileAttendance, name) {
  const value = profileAttendance?.[name]
  return typeof value === 'boolean' ? value : null
}

/** Only the real booleans in a stored attendance map.
 *
 * The same discipline `relayFlagsOnly` applies to the relay permissions, and
 * for the same reason: this map goes straight into the request, and the
 * backend's lax `bool` would read a stored `"yes"` -- from a hand-edited
 * origin, or from a build that stored it differently -- as an attendance
 * nobody declared. Dropped silently, because an unanswered profile is already
 * named on screen and refuses the plan.
 */
export function attendanceMapOnly(stored) {
  const out = {}
  for (const [name, value] of Object.entries(stored ?? {})) {
    if (typeof value === 'boolean') out[name] = value
  }
  return out
}

/** Profiles that have hours and no attendance answer, in the order given. */
export function unansweredAttendance(profilesWithHours, profileAttendance) {
  return (profilesWithHours ?? []).filter(
    (name) => attendanceFor(profileAttendance, name) === null
  )
}

/** What the CLOCK would guess, offered as a chip and never applied by itself.
 *
 * False for a window holding the small hours, true otherwise -- and false for
 * NO window at all, which is the case this used to get backwards. "A
 * round-the-clock profile has no night hours to mis-fund" reads as if the
 * absent window narrowed the set; it widens it. Such a profile owns ALL 24
 * hours, the eight nobody is at the Marketplace included, and Travian offers
 * nothing that would confine a repeat interval to part of the day. So the
 * clock's guess is asleep, which is also the direction the backend's own
 * default takes (`attended=... else False`, beside its note in
 * `web/routes/distribution.py`): unattended under-delivers and reports it
 * through NPC_CAPACITY_SHORT, attended over-commits in silence.
 */
export function suggestedAttendance(hours) {
  if (dispatchWindowFor(hours) == null) return false
  return !coversSmallHours(hours)
}

/** Attendance in words, so the state is never carried by a toggle position. */
export function describeAttendance(value) {
  if (value === true) return 'you are at the marketplace'
  if (value === false) return 'nobody is trading'
  return 'not answered yet'
}

/** `npc_attended` for a request body, or nothing at all.
 *
 * Omitted for ONE case only: an unanswered profile, which must produce the 422
 * that names the villages instead of a fabricated boolean. The page refuses to
 * build in that case, so the 422 is a backstop and not the path.
 *
 * It used to be omitted for a windowless profile too, and that was the defect.
 * The reasoning was "round the clock has no night hours to mis-fund", which is
 * backwards -- see `suggestedAttendance` -- and it stopped being merely lossy
 * when the backend changed its reading of an absent answer from ATTENDED to
 * unattended. Measured on the repo's NPC fixture with `dispatch_window: None`:
 * omitted gives an allowance of 0 and a draw of 0, `attended=true` gives
 * 20,000/h and 12,000/h. So the operator answered "you are at the
 * marketplace", saw their answer on screen, and got a plan 12,000/h short of
 * the account they had described. A boolean travels now, window or not; the
 * validator that could refuse it (`_npc_attendance_is_stated`) returns early
 * when there is no window, so a stated answer is always accepted there.
 */
export function npcAttendedField(attended) {
  if (typeof attended !== 'boolean') return {}
  return { npc_attended: attended }
}

/** Is this a usable feedstock override?
 *
 * A non-empty list of distinct stores from the four. Empty is refused for the
 * reason an empty `relay_for` is: it says nothing that leaving the field off
 * does not already say, and the backend refuses it.
 */
export function isFeedstockList(list) {
  if (!Array.isArray(list) || list.length === 0) return false
  if (list.some((resource) => !NPC_FEEDSTOCK_RESOURCES.includes(resource))) return false
  return new Set(list).size === list.length
}

/** The picker's one-line summary. "derived" is the resting state, in a word. */
export function describeFeedstock(list) {
  if (list == null) return 'derived'
  if (list.length === 0) return 'nothing ticked yet'
  return NPC_FEEDSTOCK_RESOURCES.filter((resource) => list.includes(resource))
    .map((resource) => RESOURCE_LABEL[resource])
    .join(', ')
}

/** `npc_feedstock` for a village's config row, or nothing at all.
 *
 * Ordered by the game's own resource order rather than by the order the boxes
 * were ticked: `/plan` digests its own response and `/plan/yaml` demands that
 * digest back, so two operators who ticked the same two stores must produce the
 * same plan.
 */
export function npcFeedstockField(list) {
  if (!isFeedstockList(list)) return {}
  return {
    npc_feedstock: NPC_FEEDSTOCK_RESOURCES.filter((resource) => list.includes(resource)),
  }
}
