/** The Allocate stage's per-resource allocation meter.
 *
 * `slack` is the account's production for one resource MINUS everything the
 * targets explicitly assign, so its SIGN carries the whole story:
 *
 *   slack > 0   production nobody claimed. Settled if a Rest village absorbs
 *               it; only worth knowing if not, because unclaimed production
 *               simply stays where it was made.
 *   slack ~ 0   every unit is accounted for.
 *   slack < 0   the targets promise more than the account makes, so the Rest
 *               village would have to ship what it does not have. The
 *               optimizer cannot satisfy that and the backend says so — but
 *               only at the Plan stage, one stage after the data entry this
 *               meter exists to protect. So it has to read as an error HERE.
 *
 * A unit of tolerance, because targets are derived in floating point from
 * percentages and a sub-1/h residue is rounding, not an over-allocation.
 */
export const METER_TOLERANCE = 1

export function allocationMeterSeverity(slack, hasRemainder) {
  if (slack < -METER_TOLERANCE) return 'over'
  // Inclusive, so the tolerance band belongs to 'settled' from both sides: a
  // slack of exactly -1 must never come out as "1/h unassigned", which reads
  // as spare production when the sign says the opposite.
  if (Math.abs(slack) <= METER_TOLERANCE) return 'settled'
  return hasRemainder ? 'settled' : 'unassigned'
}

/** Severity to the theme's semantic colour, with the severity split Part III
 *  asks for: red "will break", amber "worth knowing", green settled. Theme
 *  tokens only — a raw Tailwind shade is unreadable in one of the two themes. */
export const METER_TONE = {
  over: 'text-danger',
  settled: 'text-success',
  unassigned: 'text-warning',
}
