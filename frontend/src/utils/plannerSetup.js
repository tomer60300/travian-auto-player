/** Import/export of the planner's hand-typed per-village state.
 *
 * Trade Office level and the crop alert ceiling are OWNED state: the game will
 * not tell us either, so the operator types them once per account. They live in
 * localStorage, which is scoped to an ORIGIN — so the same app served on :80,
 * on :8001, on the LAN address and over Tailscale keeps four independent copies.
 * Retyping them per origin is the problem this file exists to remove.
 *
 * Everything here is pure, including the timestamp, which is passed in rather
 * than read. That keeps the round trip testable without a browser.
 */

export const SETUP_FORMAT = 'travian-planner-owned-state'
export const SETUP_VERSION = 1

/** Matches the Trade Office input's own bounds, and the backend's `le=20`. */
export const MAX_TRADE_OFFICE_LEVEL = 20

export class SetupFileError extends Error {}

/** Build the exportable document. Villages with nothing typed are left out. */
export function buildSetup({ account, villages, tradeOffice, cropCeilings, exportedAt }) {
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
  return {
    format: SETUP_FORMAT,
    version: SETUP_VERSION,
    exported_at: exportedAt,
    // Carried so a file cannot be loaded into the wrong account unnoticed:
    // village ids are per-account, so A's levels under B are silently wrong.
    account: account ?? null,
    villages: rows,
  }
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
  if (raw.version !== SETUP_VERSION) {
    throw new SetupFileError(
      `This file is version ${raw.version}, and this build reads version ${SETUP_VERSION}. ` +
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

  return { ...raw, villages }
}

/** Apply a parsed setup over the current maps, and say exactly what happened.
 *
 * The file wins where it has a value; villages it does not mention keep what
 * they already had. A village with no value from either source is reported as
 * still unknown and is deliberately NOT set to 0 — the planner floors unknown
 * levels to 0 when it plans, but recording that guess as if the operator had
 * confirmed it is how an over-provisioned village becomes invisible.
 */
export function mergeSetup({ setup, villages, tradeOffice, cropCeilings }) {
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

  return {
    tradeOffice: nextTradeOffice,
    cropCeilings: nextCropCeilings,
    report: { loaded, missingFromAccount, stillUnknown },
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
