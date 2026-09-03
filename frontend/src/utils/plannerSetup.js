/** Import/export of the planner's hand-typed per-village state.
 *
 * Trade Office level and the crop alert ceiling are OWNED state: the game will
 * not tell us either, so the operator types them once per account. They live in
 * localStorage, which is scoped to an ORIGIN — so the same app served on :80,
 * on :8001, on the LAN address and over Tailscale keeps four independent copies.
 * Retyping them per origin is the problem this file exists to remove.
 *
 * Version 2 carries the allocation PROFILES too. They were the largest body of
 * typed state the file did not cover: a Day/Night pair is a hundred numbers
 * derived from store capacities, and losing them to a cleared origin meant
 * retyping the whole distribution. Profiles ride the same account guard as the
 * per-village rows, because an allocation keyed by village id is just as wrong
 * under the wrong account.
 *
 * The village row also carries three more owned facts, all optional: where a
 * village may ship (`ship_only_to`, a list of own village ids; absent means
 * unrestricted and an EMPTY list means no OWN village, for every resource
 * including crop -- foreign tributes keep their own exclusions and a whitelist
 * cannot stop one), the share of its warehouse it keeps stocked by NPC
 * trading (`stock_floor_fraction`, 0 to 0.95), which the planner may draw down
 * as extra lumber, clay or iron, and what it SPENDS per hour
 * (`consumption_per_hour`, by resource -- LUMBER, CLAY and IRON only).
 *
 * That last one is worth carrying for the same reason the profiles are: it is
 * the second-largest body of hand-typed numbers in the planner -- a spend
 * figure per resource per role village -- and it is not derivable from
 * anything. The game reports materials GROSS, so a village consuming lumber
 * still reads positive on the statistics page. Losing it to a cleared origin
 * means every consuming village silently reads as stockpiling its whole
 * allocation again.
 *
 * Crop is the exception and is REFUSED, matching the backend's 422: the
 * snapshot's crop rate is already net of upkeep, so a declared crop spend
 * double-counts the same troops. What a village should keep of its crop is
 * said with its crop allocation TARGET instead.
 *
 * Everything here is pure, including the timestamp, which is passed in rather
 * than read. That keeps the round trip testable without a browser.
 */

export const SETUP_FORMAT = 'travian-planner-owned-state'
export const SETUP_VERSION = 2
/** Versions this build can read. A v1 file simply carries no profiles, so
 * refusing it would strand every export written before profiles travelled. */
export const READABLE_VERSIONS = Object.freeze([1, 2])

/** Matches the Trade Office input's own bounds, and the backend's `le=20`. */
export const MAX_TRADE_OFFICE_LEVEL = 20

/** The backend's ceiling for an NPC-backed stock floor. Above it a village
 * keeps nothing worth drawing down. */
export const MAX_STOCK_FLOOR_FRACTION = 0.95

/** Is this a usable stock floor? The operator types a percent, whole or to one
 * decimal, so the fraction must sit on a 0.001 grid between 0 and the ceiling.
 * Shared by the file parser and the planner's input so the two cannot disagree
 * about what a valid floor is. */
export function isStockFloorFraction(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return false
  if (value < 0 || value > MAX_STOCK_FLOOR_FRACTION) return false
  const permille = value * 1000
  return Math.abs(permille - Math.round(permille)) < 1e-6
}

/** Is this a usable consumption rate? What a village spends per hour, so zero
 * or more and finite. Negative is refused rather than clamped for the reason
 * the backend refuses it: a rate's sign cannot be read as consumption, because
 * the game's statistics page reports materials GROSS and a village burning
 * lumber still shows a positive figure. There is nothing to invert, so a
 * negative here is a typo and worth stopping for. Shared by the file parser and
 * the planner's input so the two cannot disagree. */
export function isConsumptionRate(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

/** Has this village had any spend typed at all?
 *
 * A map with every box cleared is the same as no map: the backend reads absent
 * and `{}` identically, so the file, the request and the input must all treat
 * them the same or one of them starts claiming something the others do not.
 * Unlike `ship_only_to`, where an empty list IS a distinct answer ("nobody"),
 * there is no third state to express here.
 */
export function declaresConsumption(spent) {
  return spent != null && Object.keys(spent).length > 0
}

/** The backend's AllocationMode, which the file must not outrun. */
export const ALLOCATION_MODES = Object.freeze([
  'keep',
  'absolute',
  'percentage',
  'sustain',
  'remainder',
])
export const SETUP_RESOURCES = Object.freeze(['lumber', 'clay', 'iron', 'crop'])

/** The resources a village may DECLARE a spend for. Crop is absent, and that
 * absence is the whole rule: the snapshot's `crop_per_hour` is already net of
 * troop upkeep (it comes from the village's crop balance, not the gross
 * statistics column), so a declared crop spend subtracts the same troops twice.
 * Materials are the opposite case -- reported GROSS, so a village burning
 * lumber still reads positive and nothing in the game states the spend.
 * The backend refuses a crop key with a 422; this is the same list, so the
 * file, the input and the request cannot disagree. */
export const CONSUMABLE_RESOURCES = Object.freeze(['lumber', 'clay', 'iron'])

/** A spend map with everything undeclarable dropped, or null when nothing
 * declarable is left.
 *
 * Only crop is ever dropped, and only because an older build could store one:
 * the input no longer offers it, so such a figure can be neither seen nor
 * cleared, while still being POSTed on every plan and 422'd on a number the
 * operator cannot find. Returning null rather than `{}` keeps "declared
 * nothing" one state, as `declaresConsumption` requires. */
export function materialSpendOnly(spent) {
  if (!declaresConsumption(spent)) return null
  const out = {}
  for (const resource of CONSUMABLE_RESOURCES) {
    if (spent[resource] != null) out[resource] = Number(spent[resource])
  }
  return Object.keys(out).length ? out : null
}
/** Travian's repeat interval, which is a closed set of the divisors of 24. */
export const TRAVIAN_REPEAT_INTERVALS = Object.freeze([1, 2, 3, 4, 6, 8, 12, 24])

export class SetupFileError extends Error {}

/** Build the exportable document. Villages with nothing typed are left out. */
export function buildSetup({
  account,
  villages,
  tradeOffice,
  cropCeilings,
  shipOnlyTo,
  stockFloors,
  consumption,
  profiles,
  profileWindows,
  merchantModel,
  foreignTargets,
  exportedAt,
}) {
  const rows = []
  for (const village of villages ?? []) {
    const level = tradeOffice?.[village.village_id]
    const ceiling = cropCeilings?.[village.village_id]
    const allowed = shipOnlyTo?.[village.village_id]
    const floor = stockFloors?.[village.village_id]
    // Materials only, so an export can never write a file this same parser
    // refuses to read back -- a crop figure saved by an older build would do
    // exactly that.
    const spends = materialSpendOnly(consumption?.[village.village_id])
    if (level == null && ceiling == null && allowed == null && floor == null && !spends) continue
    const row = { village_id: village.village_id, name: village.name ?? '' }
    if (level != null) row.trade_office_level = Number(level)
    if (ceiling != null) row.crop_ceiling = Number(ceiling)
    // An empty list is written, not dropped: it says "ships to nobody", which
    // is a different answer from the unrestricted default an absent field means.
    if (allowed != null) row.ship_only_to = allowed.map(Number)
    if (floor != null) row.stock_floor_fraction = Number(floor)
    if (spends) row.consumption_per_hour = spends
    rows.push(row)
  }
  const doc = {
    format: SETUP_FORMAT,
    version: SETUP_VERSION,
    exported_at: exportedAt,
    // Carried so a file cannot be loaded into the wrong account unnoticed:
    // village ids are per-account, so A's levels under B are silently wrong.
    account: account ?? null,
    villages: rows,
  }
  // Omitted rather than written empty, so a file says plainly whether it has
  // profiles to give. An empty object would import as "replace everything with
  // nothing", which is the opposite of what an operator loading a file wants.
  if (profiles && Object.keys(profiles).length) doc.profiles = profiles
  if (profileWindows && Object.keys(profileWindows).length) {
    doc.profile_windows = profileWindows
  }
  if (merchantModel) doc.merchant_model = merchantModel
  // A tribute is entirely operator-supplied -- the game will not say that an ally
  // needs 25,700 crop an hour -- and it drives real routes. Losing it to a cleared
  // origin means the obligation silently stops being planned for.
  if (foreignTargets && foreignTargets.length) doc.foreign_targets = foreignTargets
  return doc
}

/** Validate one profile's allocation map, resource by resource.
 *
 * Negative values are deliberately allowed. An `absolute` retention below a
 * village's own production is how a store that is already past its ceiling gets
 * drained rather than merely frozen, and the night profile is built on exactly
 * that — so rejecting the sign here would reject the plan it exists to carry.
 */
function parseProfile(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not an allocation map.`)
  }
  const out = {}
  for (const [resource, per] of Object.entries(raw)) {
    if (!SETUP_RESOURCES.includes(resource)) {
      throw new SetupFileError(`${where} has unknown resource "${resource}".`)
    }
    if (!per || typeof per !== 'object' || Array.isArray(per)) {
      throw new SetupFileError(`${where}.${resource} is not an object.`)
    }
    const kept = {}
    for (const [vid, alloc] of Object.entries(per)) {
      const id = Number(vid)
      if (!Number.isInteger(id) || id <= 0) {
        throw new SetupFileError(`${where}.${resource} has no usable village id "${vid}".`)
      }
      if (!alloc || typeof alloc !== 'object') {
        throw new SetupFileError(`${where}.${resource}[${vid}] is not an allocation.`)
      }
      if (!ALLOCATION_MODES.includes(alloc.mode)) {
        throw new SetupFileError(
          `${where}.${resource}[${vid}] has mode "${alloc.mode ?? 'nothing'}"; ` +
            `it must be one of ${ALLOCATION_MODES.join(', ')}.`
        )
      }
      const value = alloc.value == null ? 0 : Number(alloc.value)
      if (!Number.isFinite(value)) {
        throw new SetupFileError(`${where}.${resource}[${vid}] has a non-numeric value.`)
      }
      // Whole units. A file written from a raw computation can carry values
      // like 43726.200918351606, and that lands verbatim in the operator's
      // input box -- unreadable, uneditable, and pretending to a precision no
      // /h rate has. Sub-unit differences are noise to the planner.
      kept[id] = { mode: alloc.mode, value: Math.round(value) }
    }
    out[resource] = kept
  }
  return out
}

/** Validate the foreign-target (tribute) list.
 *
 * Rejects rather than repairs. An anonymous target cannot be reported to the
 * operator, and a negative rate would plan as a SOURCE -- the opposite of an
 * obligation. Both are more useful as an error than as a silently altered plan.
 */
function parseForeignTargets(raw, where) {
  if (!Array.isArray(raw)) throw new SetupFileError(`${where} is not a list.`)
  return raw.map((entry, i) => {
    const at = `${where}[${i}]`
    if (!entry || typeof entry !== 'object') throw new SetupFileError(`${at} is not an object.`)
    const name = String(entry.name ?? '').trim()
    if (!name) throw new SetupFileError(`${at} has no name.`)
    const x = Number(entry.x)
    const y = Number(entry.y)
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      throw new SetupFileError(`${at} ("${name}") has coordinates that are not numbers.`)
    }
    const rate = Number(entry.crop_per_hour)
    if (!Number.isFinite(rate) || rate < 0) {
      throw new SetupFileError(
        `${at} ("${name}") has crop_per_hour ${entry.crop_per_hour}; a tribute is owed, ` +
          `so it cannot be negative.`
      )
    }
    const margin = entry.safety_margin_pct == null ? 0 : Number(entry.safety_margin_pct)
    if (!Number.isFinite(margin) || margin < 0 || margin > 100) {
      throw new SetupFileError(`${at} ("${name}") has a safety margin outside 0-100.`)
    }
    // Rebuilt field by field, so anything not named here is DROPPED. That is how
    // the cadence controls went missing: a file saying "47,167 an hour, hourly,
    // not from the hub" imported as "47,167 an hour" -- the same volume with the
    // constraint silently gone, which is the plan the operator was avoiding.
    let maxCycle = null
    if (entry.max_cycle_hours != null) {
      const cycle = Number(entry.max_cycle_hours)
      if (!TRAVIAN_REPEAT_INTERVALS.includes(cycle)) {
        throw new SetupFileError(
          `${at} ("${name}") has max_cycle_hours ${entry.max_cycle_hours}; Travian's ` +
            `repeat interval is a closed set, so choose one of ` +
            `${TRAVIAN_REPEAT_INTERVALS.join(', ')}.`
        )
      }
      maxCycle = cycle
    }
    const excluded = []
    for (const raw of entry.exclude_origins ?? []) {
      const id = Number(raw)
      if (!Number.isInteger(id) || id <= 0) {
        throw new SetupFileError(
          `${at} ("${name}") has ${JSON.stringify(raw)} in exclude_origins; ` +
            `it must be a village id.`
        )
      }
      excluded.push(id)
    }
    return {
      name,
      x,
      y,
      crop_per_hour: rate,
      safety_margin_pct: margin,
      route_eligible: Boolean(entry.route_eligible),
      max_cycle_hours: maxCycle,
      exclude_origins: excluded,
    }
  })
}

/** Validate one village's `{ resource: rate }` consumption map.
 *
 * Rejects rather than repairs, the same discipline the rest of this file
 * follows: a spend under an unknown resource name would import as silence, and
 * silence here means the village reads as stockpiling its whole allocation --
 * which is the exact defect consumption exists to remove.
 */
function parseConsumption(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not a map of resource to rate per hour.`)
  }
  const out = {}
  for (const [resource, rate] of Object.entries(raw)) {
    if (!SETUP_RESOURCES.includes(resource)) {
      throw new SetupFileError(
        `${where} has unknown resource "${resource}"; ` +
          `it must be one of ${SETUP_RESOURCES.join(', ')}.`
      )
    }
    // A separate message from the unknown-resource one, because "unknown
    // resource crop" would be a lie: crop is a resource the planner knows
    // well, and the operator's own spec lists a crop figure per role village.
    // What they need is where to put it instead.
    if (!CONSUMABLE_RESOURCES.includes(resource)) {
      throw new SetupFileError(
        `${where} declares ${resource}, which the planner refuses: the ` +
          `snapshot's crop rate is already net of troop upkeep, so a declared ` +
          `crop spend subtracts the same troops twice. Declare ` +
          `${CONSUMABLE_RESOURCES.join(', ')} only, and say what the village ` +
          `should keep of its crop with its crop allocation target instead.`
      )
    }
    // The RAW value, before any coercion. `Number(null)` is 0, `Number([1])`
    // is 1 and `Number('')` is 0, so coercing first defeated the
    // `typeof === 'number'` half of the shared predicate and imported three
    // malformed shapes as confident declarations -- while the planner's input
    // tests the raw value, so parser and input disagreed about the same file.
    if (!isConsumptionRate(rate)) {
      throw new SetupFileError(
        typeof rate === 'number' && Number.isFinite(rate)
          ? `${where}.${resource} is ${rate}; consumption is what a village ` +
            `SPENDS per hour, so it cannot be negative.`
          : `${where}.${resource} is ${JSON.stringify(rate)}, which is not a ` +
            `number; consumption is a flat rate per hour.`
      )
    }
    // Whole units, as the allocation values are: a /h rate has no sub-unit
    // precision, and a figure like 14750.600918351606 from a raw computation
    // lands verbatim in the operator's input box. Rounded only after the raw
    // value has been accepted.
    out[resource] = Math.round(rate)
  }
  return out
}

/** Validate a `{ profile: ['HH:MM', 'HH:MM'] }` window map. */
function parseWindows(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not a window map.`)
  }
  const out = {}
  for (const [name, pair] of Object.entries(raw)) {
    if (!Array.isArray(pair) || pair.length !== 2) {
      throw new SetupFileError(`${where}["${name}"] must be a [start, end] pair.`)
    }
    for (const t of pair) {
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(String(t))) {
        throw new SetupFileError(`${where}["${name}"] has "${t}", which is not HH:MM.`)
      }
    }
    out[name] = [String(pair[0]), String(pair[1])]
  }
  return out
}

/** Parse and validate a setup document. Throws rather than half-loading.
 *
 * Out-of-range levels are rejected, not clamped. Clamping down would be the
 * safe direction for merchant budgets, but it would silently rewrite what the
 * operator typed, and a level above the maximum means the file is wrong — that
 * is worth stopping for.
 */
export function parseSetup(text) {
  let raw
  try {
    raw = JSON.parse(text)
  } catch {
    throw new SetupFileError('Not valid JSON — check the file was saved whole.')
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError('Not a planner setup file.')
  }
  if (raw.format !== SETUP_FORMAT) {
    throw new SetupFileError(
      `Not a planner setup file (expected format "${SETUP_FORMAT}", got "${raw.format ?? 'nothing'}").`
    )
  }
  if (!READABLE_VERSIONS.includes(raw.version)) {
    throw new SetupFileError(
      `This file is version ${raw.version}, and this build reads ` +
        `version ${READABLE_VERSIONS.join(' or ')}. ` +
        `Re-export it from the build that wrote it.`
    )
  }
  if (!Array.isArray(raw.villages)) {
    throw new SetupFileError('The file has no villages list.')
  }

  const villages = raw.villages.map((row, i) => {
    const where = `villages[${i}]`
    if (!row || typeof row !== 'object') throw new SetupFileError(`${where} is not an object.`)
    const id = Number(row.village_id)
    if (!Number.isInteger(id) || id <= 0) {
      throw new SetupFileError(`${where} has no usable village_id.`)
    }
    const parsed = { village_id: id, name: typeof row.name === 'string' ? row.name : '' }
    if (row.trade_office_level != null) {
      const level = Number(row.trade_office_level)
      if (!Number.isInteger(level) || level < 0 || level > MAX_TRADE_OFFICE_LEVEL) {
        throw new SetupFileError(
          `${where} has Trade Office level ${row.trade_office_level}; ` +
            `it must be a whole number from 0 to ${MAX_TRADE_OFFICE_LEVEL}.`
        )
      }
      parsed.trade_office_level = level
    }
    if (row.crop_ceiling != null) {
      const ceiling = Number(row.crop_ceiling)
      if (!Number.isFinite(ceiling) || ceiling < 0) {
        throw new SetupFileError(`${where} has a negative or non-numeric crop ceiling.`)
      }
      parsed.crop_ceiling = ceiling
    }
    if (row.ship_only_to != null) {
      if (!Array.isArray(row.ship_only_to)) {
        throw new SetupFileError(`${where} has a ship_only_to that is not a list of village ids.`)
      }
      parsed.ship_only_to = row.ship_only_to.map((raw) => {
        const id = Number(raw)
        if (!Number.isInteger(id) || id <= 0) {
          throw new SetupFileError(
            `${where} has ${JSON.stringify(raw)} in ship_only_to; it must be a village id.`
          )
        }
        return id
      })
    }
    if (row.stock_floor_fraction != null) {
      const floor = Number(row.stock_floor_fraction)
      if (!isStockFloorFraction(floor)) {
        throw new SetupFileError(
          `${where} has stock floor ${row.stock_floor_fraction}; it must be a fraction ` +
            `from 0 to ${MAX_STOCK_FLOOR_FRACTION} in steps of 0.001 ` +
            `(a whole percent or one decimal).`
        )
      }
      parsed.stock_floor_fraction = floor
    }
    if (row.consumption_per_hour != null) {
      parsed.consumption_per_hour = parseConsumption(
        row.consumption_per_hour,
        `${where}.consumption_per_hour`
      )
    }
    return parsed
  })

  const profiles = {}
  if (raw.profiles != null) {
    if (typeof raw.profiles !== 'object' || Array.isArray(raw.profiles)) {
      throw new SetupFileError('The file has a profiles field that is not a map of profiles.')
    }
    for (const [name, alloc] of Object.entries(raw.profiles)) {
      if (!name.trim()) throw new SetupFileError('A profile in the file has an empty name.')
      profiles[name] = parseProfile(alloc, `profiles["${name}"]`)
    }
  }
  const profileWindows =
    raw.profile_windows == null ? {} : parseWindows(raw.profile_windows, 'profile_windows')

  let merchantModel = null
  if (raw.merchant_model != null) {
    const m = raw.merchant_model
    const base = Number(m?.base_capacity)
    const bonus = Number(m?.bonus_per_to_level)
    if (!Number.isFinite(base) || base <= 0) {
      throw new SetupFileError('merchant_model.base_capacity must be a positive number.')
    }
    if (!Number.isFinite(bonus) || bonus < 0) {
      throw new SetupFileError('merchant_model.bonus_per_to_level must be zero or more.')
    }
    merchantModel = { base_capacity: base, bonus_per_to_level: bonus }
  }

  const foreignTargets =
    raw.foreign_targets == null
      ? null
      : parseForeignTargets(raw.foreign_targets, "foreign_targets")

  return { ...raw, villages, profiles, profileWindows, merchantModel, foreignTargets }
}

/** Apply a parsed setup over the current maps, and say exactly what happened.
 *
 * The file wins where it has a value; villages it does not mention keep what
 * they already had. A village with no value from either source is reported as
 * still unknown and is deliberately NOT set to 0 — the planner floors unknown
 * levels to 0 when it plans, but recording that guess as if the operator had
 * confirmed it is how an over-provisioned village becomes invisible.
 */
export function mergeSetup({
  setup,
  villages,
  tradeOffice,
  cropCeilings,
  shipOnlyTo,
  stockFloors,
  consumption,
  profiles,
  profileWindows,
  foreignTargets,
}) {
  const known = new Map((villages ?? []).map((v) => [v.village_id, v]))
  const nextTradeOffice = { ...(tradeOffice ?? {}) }
  const nextCropCeilings = { ...(cropCeilings ?? {}) }
  const nextShipOnlyTo = { ...(shipOnlyTo ?? {}) }
  const nextStockFloors = { ...(stockFloors ?? {}) }
  const nextConsumption = { ...(consumption ?? {}) }

  const missingFromAccount = []
  let loaded = 0

  for (const row of setup.villages) {
    if (!known.has(row.village_id)) {
      // Kept out of the maps entirely: a chiefed or renamed village must not
      // linger as state for an id the account no longer has.
      missingFromAccount.push({ village_id: row.village_id, name: row.name })
      continue
    }
    loaded += 1
    if (row.trade_office_level != null) nextTradeOffice[row.village_id] = row.trade_office_level
    if (row.crop_ceiling != null) nextCropCeilings[row.village_id] = row.crop_ceiling
    if (row.ship_only_to != null) nextShipOnlyTo[row.village_id] = row.ship_only_to
    if (row.stock_floor_fraction != null) nextStockFloors[row.village_id] = row.stock_floor_fraction
    // Replaced wholesale where the file has one, not merged per resource: half
    // of an old spend under a new profile is a figure nobody entered, and a
    // stale leftover resource silences a real overflow.
    if (row.consumption_per_hour != null) {
      nextConsumption[row.village_id] = row.consumption_per_hour
    }
  }

  const stillUnknown = []
  for (const village of villages ?? []) {
    if (nextTradeOffice[village.village_id] == null) {
      stillUnknown.push({ village_id: village.village_id, name: village.name ?? '' })
    }
  }

  // A profile the file names replaces the one on screen wholesale, because half
  // of an old Day profile merged into a new one is a distribution nobody
  // designed. Profiles the file does not mention are left exactly as they are.
  const nextProfiles = { ...(profiles ?? {}) }
  const nextWindows = { ...(profileWindows ?? {}) }
  const profilesLoaded = []
  const droppedVillages = new Set()
  for (const [name, alloc] of Object.entries(setup.profiles ?? {})) {
    const pruned = {}
    for (const [resource, per] of Object.entries(alloc)) {
      const kept = {}
      for (const [vid, a] of Object.entries(per)) {
        // Same rule as the per-village rows: an id the account no longer has
        // would 400 every plan call, so it is dropped and counted, not kept.
        if (known.has(Number(vid))) kept[vid] = a
        else droppedVillages.add(Number(vid))
      }
      if (Object.keys(kept).length) pruned[resource] = kept
    }
    nextProfiles[name] = pruned
    profilesLoaded.push(name)
  }
  for (const [name, pair] of Object.entries(setup.profileWindows ?? {})) {
    nextWindows[name] = pair
  }

  return {
    tradeOffice: nextTradeOffice,
    cropCeilings: nextCropCeilings,
    shipOnlyTo: nextShipOnlyTo,
    stockFloors: nextStockFloors,
    consumption: nextConsumption,
    profiles: nextProfiles,
    profileWindows: nextWindows,
    merchantModel: setup.merchantModel ?? null,
    // Replaced wholesale, not merged. Merging two tribute lists would either
    // double an obligation or leave a target the operator deleted still being
    // shipped to. A file with no targets leaves what is on screen alone.
    foreignTargets: setup.foreignTargets ?? foreignTargets ?? [],
    report: {
      loaded,
      missingFromAccount,
      stillUnknown,
      profilesLoaded,
      profileVillagesDropped: [...droppedVillages].sort((a, b) => a - b),
    },
  }
}

/** Does this file belong to the account currently connected? */
export function setupMatchesAccount(setup, accountKey) {
  if (!setup.account || !accountKey) return true // nothing to contradict
  return setup.account === accountKey
}

/** Filename for an export, stamped so successive saves do not overwrite. */
export function setupFilename(playerName, stamp) {
  const who = (playerName || 'account').replace(/[^A-Za-z0-9_-]+/g, '-')
  return `travian-planner-${who}-${stamp}.json`
}
