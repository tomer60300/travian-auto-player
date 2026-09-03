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
import { resolveRoleAllocation } from './plannerSetup'

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

/** One resource's per-village map after the operator edits ONE cell.
 *
 * The seed is the allocation the cell is SHOWING -- `resolveRoleAllocation`'s
 * answer, its role's template included -- and never a `keep` literal. A
 * templated village has no own entry, so a literal seed turned a `{value: N}`
 * patch into `{mode: 'keep', value: N}`: typing 12,000 over a DEF village's
 * template-shown 8,372 flipped the mode to Keep own, disabled the box at
 * 12,000, marked the cell as a deviation, and sent a KEEP the backend resolves
 * to "hold your own production" -- so the village retained its own 1,500/h
 * while still spending the template's 8,372. Neither figure the operator saw.
 *
 * Resolved against `perVillage` rather than the render-time map, because two
 * edits batched into one update must both land: the second reads the first's
 * entry here, and would read the pre-edit one from a closure.
 */
export function withEditedAllocation({ perVillage, villageId, template, resource, patch }) {
  const shown = resolveRoleAllocation(template, resource, perVillage?.[villageId])
  return { ...perVillage, [villageId]: { ...shown, ...patch } }
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

/** How much NPC conversion this plan actually SPENDS at each village, /h.
 *
 * A sum of the server's own per-resource draws, never a re-derivation, and
 * never the allowance: `npc_allowance_per_hour` is a CEILING -- what the
 * village retains of the resources it is not shipping -- and nothing is obliged
 * to use any of it. A village whose floor funded no cargo appears here as 0,
 * which is the honest figure and the one the NPC panel leads with; showing its
 * ceiling in that column would claim it converted 22,000/h when it converted
 * nothing.
 *
 * A Map rather than an object so a numeric village id stays numeric: every
 * other index in this file is keyed off `village_nets` too, and the panel looks
 * these up by the id on an `npc_reserves` row.
 */
export function npcDrawByVillage(plan) {
  const out = new Map()
  for (const row of plan?.village_nets ?? []) {
    out.set(row.village_id, (out.get(row.village_id) ?? 0) + (row.npc_draw_per_hour ?? 0))
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
      // `npc_draw_per_hour` is what the plan actually SPENT against the floor,
      // not `npc_allowance_per_hour`, which is only the ceiling it could have
      // spent. Naming the ceiling here would claim a village drew 20,000/h
      // when it drew nothing. (Was `supplement_per_hour` until NPC balancing
      // split the two; reading the old name silently annotated every cell 0.)
      supplement: planned.npc_draw_per_hour ?? 0,
      // The ceiling beside the draw, so the cell can say "15,000/h of 22,000/h
      // available" rather than leaving the operator to wonder whether the
      // supplement was all there was. Carried as a SEPARATE field for the
      // reason the comment above gives: the two are different quantities, and
      // one field would eventually be read as the other.
      allowance: planned.npc_allowance_per_hour ?? 0,
    }
  }
  const ship = localTarget == null || own == null ? null : localTarget - own
  // No plan, no supplement AND no ceiling: both are rates only the planner can
  // work out (it reads a warehouse LEVEL against what the village retains), so
  // the live preview cannot show either and must not imply it has one.
  const preview = { target: localTarget, ship, supplement: 0, allowance: 0 }
  if (declaredSpend == null) return { ...preview, spent: null, net: null }
  return {
    ...preview,
    spent: declaredSpend,
    net: localTarget == null ? null : localTarget - declaredSpend,
  }
}
