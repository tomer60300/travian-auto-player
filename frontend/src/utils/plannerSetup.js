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

/** The backend's AllocationMode, which the file must not outrun. */
export const ALLOCATION_MODES = Object.freeze([
  'keep',
  'absolute',
  'percentage',
  'sustain',
  'remainder',
])
export const SETUP_RESOURCES = Object.freeze(['lumber', 'clay', 'iron', 'crop'])

export class SetupFileError extends Error {}

/** Build the exportable document. Villages with nothing typed are left out. */
export function buildSetup({
  account,
  villages,
  tradeOffice,
  cropCeilings,
  profiles,
  profileWindows,
  merchantModel,
  exportedAt,
}) {
  const rows = []
  for (const village of villages ?? []) {
    const level = tradeOffice?.[village.village_id]
    const ceiling = cropCeilings?.[village.village_id]
    if (level == null && ceiling == null) continue
    const row = { village_id: village.village_id, name: village.name ?? '' }
    if (level != null) row.trade_office_level = Number(level)
    if (ceiling != null) row.crop_ceiling = Number(ceiling)
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
      kept[id] = { mode: alloc.mode, value }
    }
    out[resource] = kept
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

  return { ...raw, villages, profiles, profileWindows, merchantModel }
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
  profiles,
  profileWindows,
}) {
  const known = new Map((villages ?? []).map((v) => [v.village_id, v]))
  const nextTradeOffice = { ...(tradeOffice ?? {}) }
  const nextCropCeilings = { ...(cropCeilings ?? {}) }

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
    profiles: nextProfiles,
    profileWindows: nextWindows,
    merchantModel: setup.merchantModel ?? null,
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
