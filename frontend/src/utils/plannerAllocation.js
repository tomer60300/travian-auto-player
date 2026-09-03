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

/** The plan's own per-village figures, indexed the way the grid reads them:
 *  `resource -> village id -> row`.
 *
 * Own villages only, because that is what the backend sends: a route-eligible
 * foreign tribute used to arrive here as a pseudo-village with a negative id
 * and no store. Indexing by real village id hid it, which is why it survived
 * this long -- so this is a read of what the plan says, not a filter.
 */
export function villageNetIndex(plan) {
  const out = {}
  for (const row of plan?.village_nets ?? []) {
    if (!out[row.resource]) out[row.resource] = {}
    out[row.resource][row.village_id] = row
  }
  return out
}

/** The three lines one Allocate cell prints, from ONE source.
 *
 * `planned` is the plan's row for this village and resource, from
 * `villageNetIndex`. When there is one, all four figures come off it. That is
 * the whole point: the top line used to be the page's own `targetFor(...)`
 * while the spend and net lines came from the plan, so a village with a stock
 * FLOOR read as three contradictory numbers -- KEEP's plan target includes the
 * supplement the local derivation does not, so "5,000/h ... -4,000 = 16,000
 * net" appeared in one cell, off by exactly the 15,000/h supplement. `ship`
 * comes off the plan too, and must: with a supplement the cargo is
 * `target - own - supplement`, so deriving it as `target - own` overstates it.
 *
 * With no plan every figure is the LIVE preview of the inputs on screen, which
 * is what the operator edits against before pressing Plan. Not a fallback for a
 * missing field: every input change clears the plan, so a plan on screen was
 * computed from exactly these inputs.
 *
 * The spend and net lines render only where a spend is DECLARED (`null`
 * otherwise), and the declared figure is deliberately not the one printed once
 * a plan exists -- the planner sets aside a spend whose rate it could not read,
 * and printing what the operator typed would claim it had been applied.
 */
export function planCellFigures({ planned, own, localTarget, declaredSpend }) {
  if (planned) {
    return {
      target: planned.target_per_hour,
      ship: planned.ship_per_hour,
      spent: declaredSpend == null ? null : planned.consumption_per_hour,
      net: declaredSpend == null ? null : planned.net_per_hour,
      // Carried so the cell can NAME it. Reconciling the three lines is not
      // enough on its own: a floored KEEP village reads 20,000/h retention
      // against 5,000/h of own production with no cargo, and nothing but the
      // stock floor accounts for the other 15,000.
      supplement: planned.supplement_per_hour ?? 0,
    }
  }
  const ship = localTarget == null || own == null ? null : localTarget - own
  // No plan, no supplement: the floor is a rate only the planner can work out
  // (it spreads a warehouse LEVEL across the window the profile runs), so the
  // live preview cannot show one and must not imply it has.
  const preview = { target: localTarget, ship, supplement: 0 }
  if (declaredSpend == null) return { ...preview, spent: null, net: null }
  return {
    ...preview,
    spent: declaredSpend,
    net: localTarget == null ? null : localTarget - declaredSpend,
  }
}
