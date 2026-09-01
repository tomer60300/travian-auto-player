/**
 * Reading a 26-village snapshot at a glance.
 *
 * The Snapshot table answers "what is each village doing" one row at a time,
 * which is the wrong shape for the question the operator actually opens the page
 * with: where is crop going wrong, and can the villages that need to fix it
 * still ship. Finding that today means eye-scanning every row for a red number,
 * and the account only grows.
 *
 * These are pure functions over the snapshot so the counting is testable and the
 * page stays a view. The definitions deliberately match what the table already
 * renders -- `starving` is the same `crop_per_hour < 0` that CropCell colours
 * red -- so the summary can never disagree with the rows beneath it.
 */

/** A village is starving when it consumes more crop than it makes. */
const isStarving = (v) => v.crop_per_hour != null && v.crop_per_hour < 0

/** Rate could not be derived. NOT the same as zero, and never counted as it. */
const isUnknownCrop = (v) => v.crop_per_hour == null

/** Every merchant is already in flight, so this village cannot start a route now. */
const hasNoFreeMerchants = (v) => v.merchants_free === 0

/**
 * Summarise the account the way the operator reads it.
 *
 * `netCropPerHour` sums only the villages whose rate is KNOWN, and
 * `unknownCrop` reports how many were left out. Folding an unknown in as zero
 * would make the headline number quietly wrong in the one direction that
 * matters -- it would look like the account is coping.
 */
export function summariseSnapshot(villages = [], tradeOffice = {}) {
  const starving = villages.filter(isStarving)
  const unknownCrop = villages.filter(isUnknownCrop)
  const noFreeMerchants = villages.filter(hasNoFreeMerchants)
  const missingTradeOffice = villages.filter((v) => {
    const level = tradeOffice[v.village_id]
    return level === undefined || level === null || level === ''
  })

  const netCropPerHour = villages.reduce(
    (sum, v) => (isUnknownCrop(v) ? sum : sum + v.crop_per_hour),
    0
  )
  const starvingCropPerHour = starving.reduce((sum, v) => sum + v.crop_per_hour, 0)

  return {
    total: villages.length,
    netCropPerHour,
    // Complete only when every village's rate was readable; the UI says so.
    netIsComplete: unknownCrop.length === 0,
    starving,
    starvingCropPerHour,
    unknownCrop,
    noFreeMerchants,
    missingTradeOffice,
  }
}

/** The columns worth ordering by, and how to read a value out of a village. */
const SORT_VALUES = {
  name: (v) => (v.name ?? '').toLowerCase(),
  lumber: (v) => v.lumber_per_hour ?? 0,
  clay: (v) => v.clay_per_hour ?? 0,
  iron: (v) => v.iron_per_hour ?? 0,
  crop: (v) => v.crop_per_hour,
  merchants: (v) => v.merchants_free ?? 0,
}

export const SORTABLE_COLUMNS = Object.keys(SORT_VALUES)

/**
 * Sort a copy of the villages. `key` of null returns the original order.
 *
 * The incoming order is the account's own and is meaningful, so it stays the
 * default and remains reachable by cycling a header back to unsorted.
 *
 * Unknown crop sorts LAST in both directions rather than to an end that implies
 * a magnitude: a village whose rate could not be read is not the worst offender
 * and it is not the healthiest, and putting it at either extreme would say one
 * of those things.
 */
export function sortVillages(villages = [], key, direction = 'asc') {
  if (!key || !SORT_VALUES[key]) return villages
  const read = SORT_VALUES[key]
  const sign = direction === 'desc' ? -1 : 1

  return [...villages].sort((a, b) => {
    const av = read(a)
    const bv = read(b)
    const aUnknown = av === null || av === undefined
    const bUnknown = bv === null || bv === undefined
    if (aUnknown && bUnknown) return 0
    if (aUnknown) return 1
    if (bUnknown) return -1
    if (av === bv) return 0
    return av < bv ? -sign : sign
  })
}

/**
 * The next sort state when a header is clicked: ascending, then descending,
 * then back to the account's own order.
 *
 * Pure, and returned as one value, because the caller sets `key` and
 * `direction` together. Deriving one inside the other's state updater would
 * make the updater impure -- React is free to invoke it more than once, and a
 * double invocation would advance the cycle twice from a single click.
 */
export function nextSort(current, key) {
  if (current.key !== key) return { key, direction: 'asc' }
  if (current.direction === 'asc') return { key, direction: 'desc' }
  return { key: null, direction: 'asc' }
}


/** The problem sets the summary chips can isolate. */
export const FILTERS = {
  starving: { label: 'Starving', match: isStarving },
  noMerchants: { label: 'No free merchants', match: hasNoFreeMerchants },
  unknownCrop: { label: 'Unknown crop', match: isUnknownCrop },
}

/**
 * Narrow the table to one problem set. An unknown filter returns everything
 * rather than nothing -- a filter that silently empties the table would read as
 * "no villages" instead of "bad filter".
 */
export function filterVillages(villages = [], filter, tradeOffice = {}) {
  if (!filter) return villages
  if (filter === 'missingTradeOffice') {
    return villages.filter((v) => {
      const level = tradeOffice[v.village_id]
      return level === undefined || level === null || level === ''
    })
  }
  const spec = FILTERS[filter]
  if (!spec) return villages
  return villages.filter(spec.match)
}
