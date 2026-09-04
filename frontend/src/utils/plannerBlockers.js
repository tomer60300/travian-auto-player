/** Cells the page has already marked invalid, gathered so Build plan can refuse.
 *
 * Every figure listed here is one the page ALREADY outlines and names in the
 * cell -- `aria-invalid`, plus a sentence under the box saying what the bound
 * is. Until this module the button beside them was enabled anyway: typing 99
 * into "Most merchants busy at once" on a 19-merchant village rendered
 * "only 19 merchants here", and `Build plan` then posted
 * `max_busy_merchants: 99` and reported the refusal as a server 422 -- a
 * response that names an internal village id, arrives after a round trip, and
 * leads nobody back to the cell that caused it.
 *
 * The page already had the right pattern one guard up: `activeAttendanceOwed`
 * refuses client-side, names the profile and jumps to the stage that answers
 * it. This is the same shape for every other marked cell.
 *
 * Computed from STATE rather than swept out of the DOM, and that is the
 * load-bearing decision. A `document.querySelectorAll('[aria-invalid]')` scan
 * is tempting -- it is by construction whatever the cells say -- but each stage
 * mounts only its own tables, so a scan run from the Targets stage cannot see
 * the Account table's cells, which is precisely the reported failure. What it
 * shares with the cells instead is the PREDICATE: `isMaxBusyMerchants`,
 * `unreachableCaps`, `isStockFloorFraction`, `isConsumptionRate`,
 * `isEmptyTemplate`, `isAssumedCropRate`, `merchantModelProblems` and
 * `resolveVillageNames` are the same functions the cells call, so a mark and a
 * blocker cannot come from two different rules.
 *
 * Pure, so the whole gate is testable without a browser -- which is the other
 * half of why it does not read the DOM.
 */

import { RESOURCE_LABEL, ROLE_LABEL } from '../constants/planner'
import {
  CONSUMABLE_RESOURCES,
  MAX_MERCHANTS_PER_VILLAGE,
  VILLAGE_ROLES,
  isAssumedCropRate,
  isConsumptionRate,
  isEmptyTemplate,
  isMaxBusyMerchants,
  isStockFloorFraction,
  merchantModelProblems,
  resolvedSpend,
  unreachableCaps,
} from './plannerSetup'
import { namesForVillageIds, resolveVillageNames } from './villageRefs'

/** Which merchant-model box carries which accessible name, so a blocker can
 *  send the caret to the one that is wrong. Second copy of a string the panel
 *  owns, and deliberately so: the alternative is the panel exporting six ids,
 *  which puts layout in the gate. Pinned by a test that renders the panel. */
export const MERCHANT_MODEL_LABELS = Object.freeze({
  base_capacity: 'Merchant base capacity',
  bonus_per_to_level: 'Trade Office bonus per level',
  merchant_reserve: 'Merchants held in reserve at every village',
  merchant_headroom: "Merchant headroom, percent of each village's budget",
  map_span: 'Map span override',
  speed_fields_per_hour: 'Merchant speed fields per hour override',
})

/** The column heading each merchant-model box sits under, for the refusal. */
const MERCHANT_MODEL_FIELDS = Object.freeze({
  base_capacity: 'Base capacity',
  bonus_per_to_level: 'Bonus / TO level',
  merchant_reserve: 'Reserve / village',
  merchant_headroom: 'Headroom %',
  map_span: 'Map span',
  speed_fields_per_hour: 'Speed f/h',
})

/**
 * Every marked cell, in reading order: Account first, then Targets.
 *
 * One entry per FIELD rather than per cell, with the villages it is wrong at
 * named — the operator fixes a column, and "Most merchants busy at once — 11,
 * 19" is one thing to go and do where two entries are two.
 *
 * Each entry carries:
 *   * `field`  — the column heading, as it reads on screen
 *   * `rule`   — what the box will accept, in the cell's own words
 *   * `stage`  — the stage that mounts it, so the refusal can jump there
 *   * `villages` — the names it is wrong at (empty for account-wide fields)
 *   * `focusLabel` — the accessible name of the FIRST offending control, so the
 *     caret can land in it. Matched against `aria-label` rather than an id
 *     because these controls are named for screen readers already and a second
 *     handle would be a second thing to keep in step.
 */
export function planBlockers({
  villages = [],
  maxBusy = {},
  stockFloors = {},
  consumption = {},
  villageRoles = {},
  roleTemplates = {},
  foreignTargets = [],
  merchantModel = {},
} = {}) {
  const out = []
  const add = (entry) => {
    if (entry.villages.length || entry.focusLabel) out.push(entry)
  }
  const named = (v) => v.name || String(v.village_id)

  // ── Account ───────────────────────────────────────────────────────────
  // Column order, so the refusal reads left to right across the table the
  // operator is looking at.

  const rolesMissing = villages.filter((v) => {
    const role = villageRoles[v.village_id]
    return role != null && isEmptyTemplate(roleTemplates[role])
  })
  add({
    field: 'Role',
    rule: 'a role needs its template typed — the plan is refused without it',
    stage: 'snapshot',
    villages: rolesMissing.map(named),
    focusLabel: rolesMissing.length ? `Role for ${named(rolesMissing[0])}` : null,
  })

  // Two rules on one column, and they are told apart because they lead to
  // different fixes: out of range is a typo in the box, past the fleet is a
  // ceiling for merchants the village does not have.
  const unreachable = new Set(unreachableCaps(maxBusy, villages).map((c) => c.village_id))
  const badCaps = villages.filter((v) => {
    const cap = maxBusy[v.village_id]
    return (cap != null && !isMaxBusyMerchants(cap)) || unreachable.has(v.village_id)
  })
  add({
    field: 'Most merchants busy at once',
    rule: `0 to ${MAX_MERCHANTS_PER_VILLAGE}, and no more than the village fields`,
    stage: 'snapshot',
    villages: badCaps.map(named),
    focusLabel: badCaps.length
      ? `Most merchants busy at once for ${named(badCaps[0])}`
      : null,
  })

  const badFloors = villages.filter((v) => {
    const floor = stockFloors[v.village_id]
    return floor != null && !isStockFloorFraction(floor)
  })
  add({
    field: 'Stock floor %',
    rule: '0–95%, whole or one decimal',
    stage: 'snapshot',
    villages: badFloors.map(named),
    focusLabel: badFloors.length
      ? `NPC-backed stock floor for ${named(badFloors[0])}, percent of warehouse`
      : null,
  })

  // The RESOLVED spend, which is what the cell shows and what the plan uses: a
  // negative figure inherited from a role template is outlined on every village
  // in that role, so it has to be refused on every one of them too.
  const badSpends = []
  for (const v of villages) {
    const spent = resolvedSpend(roleTemplates[villageRoles[v.village_id]], consumption[v.village_id])
      .effective
    for (const resource of CONSUMABLE_RESOURCES) {
      const rate = spent?.[resource]
      if (rate != null && !isConsumptionRate(rate)) {
        badSpends.push({ village: v, resource })
      }
    }
  }
  add({
    field: 'Consumption /h',
    rule: '0 or more',
    stage: 'snapshot',
    villages: [...new Set(badSpends.map(({ village }) => named(village)))],
    focusLabel: badSpends.length
      ? `${RESOURCE_LABEL[badSpends[0].resource]} spent per hour by ${named(badSpends[0].village)}`
      : null,
  })

  // A name nobody's village answers to does not 422 — it is silently DROPPED
  // from `exclude_origins`, which is worse: the operator believes a village is
  // excluded and the next run draws on it anyway. Refused for that reason
  // rather than for the server's.
  const badExclusions = []
  foreignTargets.forEach((target, index) => {
    // The cell's own expression, ids included: a stored id whose village has
    // left the account renders as its own number, which is what the operator
    // sees flagged.
    const typed =
      target?.exclude_origins_text ?? namesForVillageIds(target?.exclude_origins, villages)
    const { unknown } = resolveVillageNames(typed, villages)
    if (unknown.length) badExclusions.push({ index, unknown })
  })
  add({
    field: 'Not from',
    rule: 'the name of a village on this account, or nothing',
    stage: 'snapshot',
    villages: badExclusions.flatMap(({ unknown }) => unknown),
    focusLabel: badExclusions.length
      ? `Foreign target ${badExclusions[0].index + 1} excluded origins`
      : null,
  })

  for (const [field, rule] of Object.entries(merchantModelProblems(merchantModel))) {
    out.push({
      field: MERCHANT_MODEL_FIELDS[field],
      rule,
      stage: 'snapshot',
      villages: [],
      focusLabel: MERCHANT_MODEL_LABELS[field],
    })
  }

  // ── Targets ───────────────────────────────────────────────────────────
  // The role templates, which are edited here and read by every village that
  // claims the role.

  for (const role of VILLAGE_ROLES) {
    const template = roleTemplates[role]
    if (template == null) continue
    const badRates = CONSUMABLE_RESOURCES.filter((resource) => {
      const rate = template.consumption?.[resource]
      return rate != null && !isConsumptionRate(rate)
    })
    if (badRates.length) {
      out.push({
        field: `${ROLE_LABEL[role]} template spend`,
        rule: '0 or more',
        stage: 'allocate',
        villages: badRates.map((resource) => RESOURCE_LABEL[resource]),
        focusLabel: `${RESOURCE_LABEL[badRates[0]]} spent per hour by a ${ROLE_LABEL[role]} village`,
      })
    }
    const assumed = template.assumed_crop_per_hour
    if (assumed != null && !isAssumedCropRate(assumed)) {
      out.push({
        field: `${ROLE_LABEL[role]} assumed net crop`,
        rule: 'a number, negative included',
        stage: 'allocate',
        villages: [],
        focusLabel: `Assumed net crop per hour for a ${ROLE_LABEL[role]} village`,
      })
    }
  }

  return out
}

/** The refusal, in one sentence the operator can act on.
 *
 * Names the fields and the villages, because the 422 this replaces named
 * neither in words anyone running the account uses. The count is of FIELDS, not
 * of cells: it has to match the list that follows it.
 */
export function describeBlockers(blockers) {
  const parts = (blockers ?? []).map(
    (b) => `${b.field}${b.villages.length ? ` (${b.villages.join(', ')})` : ''} — ${b.rule}`
  )
  const lead =
    parts.length === 1
      ? 'One figure on this page is outside what the plan accepts'
      : `${parts.length} figures on this page are outside what the plan accepts`
  return `${lead}, so nothing was sent: ${parts.join('; ')}.`
}
