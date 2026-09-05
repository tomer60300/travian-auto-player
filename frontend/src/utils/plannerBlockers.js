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
 * `isEmptyTemplate`, `isAssumedCropRate`, `isTradeOfficeLevel`,
 * `isCropCeiling`, `isSafetyMarginPct`, `nightFillProblems`,
 * `merchantModelProblems`, `relayTierProblemsByVillage` and
 * `resolveVillageNames` are the same functions the cells call, so a mark and a
 * blocker cannot come from two different rules.
 *
 * Pure, so the whole gate is testable without a browser -- which is the other
 * half of why it does not read the DOM.
 *
 * Three lists, not one, because the AUDIENCES differ. `planBlockers` is the
 * document and the plan, so it refuses Build plan, Save, Export, the full-day
 * check, the night derivation and the run. `runBlockers` is the three
 * controlled-run boxes, which are neither plan inputs nor document fields, so
 * they refuse Preview and the live run alone. `nightBlockers` is the fill pair,
 * which reaches `/night-profile` and nothing else. Refusing a request a figure
 * has no bearing on would be the same class of mistake as not refusing one it
 * does.
 */

import { RESOURCE_LABEL, ROLE_LABEL } from '../constants/planner'
import {
  CONSUMABLE_RESOURCES,
  MAX_GAME_ROWS_PER_RUN_CEILING,
  MAX_MERCHANTS_PER_VILLAGE,
  MAX_ROUTES_PER_RUN_CEILING,
  MAX_TRADE_OFFICE_LEVEL,
  VILLAGE_ROLES,
  isAssumedCropRate,
  isConsumptionRate,
  isCropCeiling,
  isEmptyTemplate,
  isMaxBusyMerchants,
  isSafetyMarginPct,
  isStockFloorFraction,
  isTradeOfficeLevel,
  merchantModelProblems,
  nightFillProblems,
  relayTierProblemsByVillage,
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
 * Every marked cell, in reading order: Account, then Targets, then Day & night.
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
  tradeOffice = {},
  cropCeilings = {},
  maxBusy = {},
  stockFloors = {},
  consumption = {},
  villageRoles = {},
  roleTemplates = {},
  foreignTargets = [],
  merchantModel = {},
  relayFor = {},
} = {}) {
  const out = []
  const add = (entry) => {
    if (entry.villages.length || entry.focusLabel) out.push(entry)
  }
  const named = (v) => v.name || String(v.village_id)
  const namedId = (id) => {
    const found = villages.find((v) => v.village_id === id)
    return found ? named(found) : String(id)
  }

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

  const badLevels = villages.filter((v) => {
    const level = tradeOffice[v.village_id]
    return level != null && !isTradeOfficeLevel(level)
  })
  add({
    field: 'Trade Office',
    rule: `a whole level from 0 to ${MAX_TRADE_OFFICE_LEVEL}`,
    stage: 'snapshot',
    villages: badLevels.map(named),
    focusLabel: badLevels.length ? `Trade Office level for ${named(badLevels[0])}` : null,
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

  // The declared relay tier, which the cell has outlined in `text-danger` and
  // named with `aria-describedby` since it was built -- and which this list did
  // not carry at all, so it was the one figure the page marked and then agreed
  // to send. A `def` village declared as the relay for 02 is backend rule 3
  // (profile section 5.9): `/plan` 422s it, `Save setup` PUTs a document that
  // 422s, and `Save setup to file` writes one `parseSetup` refuses on the way
  // back in -- the exact "saves with a 200 and can never be loaded again" shape
  // the save gate exists to stop, reached through a column the gate never read.
  //
  // Same helper the cell calls, so a mark and a refusal cannot come from two
  // rules. Keyed to the RELAY's own cell, because that is the list that has to
  // change: the downstream village named in the message is not where the edit
  // happens.
  const relayProblems = relayTierProblemsByVillage(relayFor, villages, villageRoles)
  const badRelays = Object.keys(relayProblems)
    .map(Number)
    .sort((a, b) => a - b)
  add({
    field: 'Relays for',
    // The cell's own sentences, not a bound: a relay tier has no range to
    // state, only rules it breaks, and each one already names its villages.
    rule: badRelays.flatMap((id) => relayProblems[id]).join(' '),
    stage: 'snapshot',
    villages: badRelays.map(namedId),
    focusLabel: badRelays.length ? `Villages ${namedId(badRelays[0])} forwards material to` : null,
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

  // The margin rides on the same row as the exclusions above, and is refused
  // for the server's reason rather than the exclusions': a tribute shipped at
  // 150% is a 422 on `foreign_targets.N.safety_margin_pct`, which names an
  // index into a list the operator sees as a table of ally names.
  const badMargins = []
  foreignTargets.forEach((target, index) => {
    const raw = target?.safety_margin_pct
    // Blank is the 0 the backend defaults to, not a figure to check -- the same
    // rule `merchantModelProblems` follows for an empty override box.
    if (raw == null || raw === '') return
    if (!isSafetyMarginPct(Number(raw))) {
      badMargins.push({ index, name: target?.name || `target ${index + 1}` })
    }
  })
  add({
    field: 'Margin %',
    rule: '0 to 100',
    stage: 'snapshot',
    villages: badMargins.map(({ name }) => name),
    focusLabel: badMargins.length
      ? `Foreign target ${badMargins[0].index + 1} safety margin`
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

  // ── Day & night ───────────────────────────────────────────────────────
  // The crop alert is typed on the stage that reads it, two stages from the
  // Account table it used to live in -- so its refusal has to send the caret
  // there rather than to `snapshot`.
  const badCeilings = villages.filter((v) => {
    const ceiling = cropCeilings[v.village_id]
    return ceiling != null && !isCropCeiling(ceiling)
  })
  add({
    field: 'Crop stock alert',
    rule: '0 or more',
    stage: 'day',
    villages: badCeilings.map(named),
    focusLabel: badCeilings.length
      ? `Crop stock alert level for ${named(badCeilings[0])}`
      : null,
  })

  return out
}

/** Is this "Never disable" entry a shape the reconciler can match against?
 *
 * Backend twin: `ExecuteRequest._protected_entries_are_parseable` in
 * `src/travian_api/web/routes/distribution.py`, transcribed rule for rule --
 * a village id, or coordinates either side of a `|`, with a leading minus
 * allowed on each half because a world is centred on 0|0. The server REFUSES a
 * malformed entry rather than dropping it, and the reason is the same one the
 * cell's own "no village named ..." note gives: an entry that protects nothing
 * while looking like it does means the operator switches a hand-made route back
 * on and the next run switches it off again.
 *
 * `unresolvedProtectedEntries` in `villageRefs.js` answers the OTHER half --
 * whether a shape-valid entry names anything on this account -- and skips
 * everything containing a `|`. So "46|abc" was flagged by neither.
 */
function isProtectedEntry(raw) {
  const entry = String(raw ?? '').trim()
  const bar = entry.indexOf('|')
  if (bar === -1) return /^\d+$/.test(entry) && Number(entry) > 0
  // `-*` rather than `-?`: Python's `lstrip("-")` strips every leading hyphen,
  // so the server accepts "--5" and a stricter regex here would refuse a figure
  // the request would have taken.
  const coordinate = /^-*\d+$/
  return coordinate.test(entry.slice(0, bar).trim()) && coordinate.test(entry.slice(bar + 1).trim())
}

/** What the box is allowed to hold, as a whole count with a ceiling.
 *
 * Blank is skipped, never refused: every one of these boxes documents a
 * fallback for an empty box in its own copy, and `routeCap` / the payload
 * builders implement it. Blank is unknown, and unknown is not out of range.
 */
function wholeCountProblem(raw, ceiling) {
  if (String(raw ?? '').trim() === '') return null
  const value = Number(raw)
  if (!Number.isInteger(value) || value < 0 || value > ceiling) return `0 to ${ceiling}`
  return null
}

/** The controlled-run boxes, which no other gate on this page could see.
 *
 * Separate from `planBlockers` because the audience is different, not because
 * the mechanism is: these three figures are not plan inputs and not setup
 * document fields, so refusing `Build plan` or `Save setup` over one would
 * refuse a request they have no bearing on. They ride on `/execute` alone, so
 * this is what Preview and the live run consult -- the same predicates the
 * cells mark themselves with, the same entry shape, and `describeBlockers`
 * renders both.
 */
export function runBlockers({ routesPerRun, maxGameRows, protectDestinations } = {}) {
  const out = []
  const routes = wholeCountProblem(routesPerRun, MAX_ROUTES_PER_RUN_CEILING)
  if (routes) {
    out.push({
      field: 'Routes this run',
      rule: routes,
      stage: 'plan',
      villages: [],
      focusLabel: 'Routes this run',
    })
  }
  const rows = wholeCountProblem(maxGameRows, MAX_GAME_ROWS_PER_RUN_CEILING)
  if (rows) {
    out.push({
      field: 'Max rows this run',
      rule: rows,
      stage: 'plan',
      villages: [],
      focusLabel: 'Max rows this run',
    })
  }
  const malformed = String(protectDestinations ?? '')
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .filter((entry) => !isProtectedEntry(entry))
  if (malformed.length) {
    out.push({
      field: 'Never disable',
      rule: 'a village id, or coordinates like 46|133',
      stage: 'plan',
      // The ENTRIES rather than village names: nothing resolved them to a
      // village, which is the whole finding.
      villages: malformed,
      focusLabel: 'Never disable',
    })
  }
  return out
}

/** The two night-fill boxes, which ride on `/night-profile` alone.
 *
 * Same reasoning as `runBlockers`: `Derive from stores` is the only button
 * these figures reach, so they are the only thing they may refuse. The pair
 * rule is here rather than on either box because it is a statement about both,
 * and it is reported on the target -- the figure the operator moves to fix it.
 */
export function nightBlockers({ baselineFill, targetFill } = {}) {
  const FIELDS = { baseline_fill: 'Emptied to %', target_fill: 'Full to %' }
  return Object.entries(nightFillProblems({ baselineFill, targetFill })).map(([field, rule]) => ({
    field: FIELDS[field],
    rule,
    stage: 'day',
    villages: [],
    focusLabel: FIELDS[field],
  }))
}

/** The refusal, in one sentence the operator can act on.
 *
 * Names the fields and the villages, because the 422 this replaces named
 * neither in words anyone running the account uses. The count is of FIELDS, not
 * of cells: it has to match the list that follows it.
 *
 * "What ITS FIELD accepts", not "what the plan accepts". This renders all three
 * lists above, and only one of them is the plan: the controlled-run boxes ride
 * on `/execute` and the fill pair on `/night-profile`, so an operator who
 * pasted 51 into "Routes this run" was told their figure was outside what the
 * plan accepts -- about a box `Build plan` deliberately never reads. Every
 * entry carries a `field` and the rule that field enforces, which is what makes
 * the new wording true of each audience rather than merely vague about them.
 */
export function describeBlockers(blockers) {
  const parts = (blockers ?? []).map(
    (b) => `${b.field}${b.villages.length ? ` (${b.villages.join(', ')})` : ''} — ${b.rule}`
  )
  const lead =
    parts.length === 1
      ? 'One figure on this page is outside what its field accepts'
      : `${parts.length} figures on this page are outside what their fields accept`
  return `${lead}, so nothing was sent: ${parts.join('; ')}.`
}
