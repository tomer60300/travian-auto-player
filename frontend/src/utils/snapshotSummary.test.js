import { describe, expect, it } from 'vitest'

import {
  FILTERS,
  filterVillages,
  nextSort,
  sortVillages,
  summariseSnapshot,
} from './snapshotSummary'

const v = (id, over = {}) => ({
  village_id: id,
  name: `v${id}`,
  x: 0,
  y: 0,
  lumber_per_hour: 1000,
  clay_per_hour: 1000,
  iron_per_hour: 1000,
  crop_per_hour: 1000,
  merchants_free: 5,
  merchants_total: 20,
  ...over,
})

describe('summarising the account', () => {
  it('counts the villages that consume more crop than they make', () => {
    const summary = summariseSnapshot([
      v(1, { crop_per_hour: -45617 }),
      v(2, { crop_per_hour: 68131 }),
      v(3, { crop_per_hour: -5326 }),
    ])

    expect(summary.starving.map((x) => x.village_id)).toEqual([1, 3])
    expect(summary.starvingCropPerHour).toBe(-50943)
  })

  it('reports the net rate across the account', () => {
    const summary = summariseSnapshot([
      v(1, { crop_per_hour: -45617 }),
      v(2, { crop_per_hour: 68131 }),
    ])

    expect(summary.netCropPerHour).toBe(22514)
    expect(summary.netIsComplete).toBe(true)
  })

  it('leaves an unreadable rate OUT of the net and says the net is incomplete', () => {
    // The dangerous alternative is folding unknown in as zero, which biases the
    // headline in exactly one direction: the account looks like it is coping.
    const summary = summariseSnapshot([
      v(1, { crop_per_hour: 5000 }),
      v(2, { crop_per_hour: null }),
    ])

    expect(summary.netCropPerHour).toBe(5000)
    expect(summary.netIsComplete).toBe(false)
    expect(summary.unknownCrop).toHaveLength(1)
  })

  it('does not call an unreadable rate starving', () => {
    const summary = summariseSnapshot([v(1, { crop_per_hour: null })])

    expect(summary.starving).toHaveLength(0)
    expect(summary.unknownCrop).toHaveLength(1)
  })

  it('counts villages that cannot start a route because no merchant is home', () => {
    const summary = summariseSnapshot([
      v(1, { merchants_free: 0 }),
      v(2, { merchants_free: 1 }),
      v(3, { merchants_free: 0 }),
    ])

    expect(summary.noFreeMerchants.map((x) => x.village_id)).toEqual([1, 3])
  })

  it('treats a blank Trade Office as missing but a typed 0 as answered', () => {
    // Blank means "nobody has said"; 0 is an operator's answer. The planner
    // floors both to 0, but only one of them is a question still open.
    const summary = summariseSnapshot([v(1), v(2), v(3)], { 1: 0, 2: undefined, 3: 7 })

    expect(summary.missingTradeOffice.map((x) => x.village_id)).toEqual([2])
  })

  it('summarises an empty account without throwing', () => {
    const summary = summariseSnapshot()

    expect(summary.total).toBe(0)
    expect(summary.netCropPerHour).toBe(0)
    expect(summary.netIsComplete).toBe(true)
  })
})

describe('ordering the table', () => {
  it('keeps the account order when nothing is chosen', () => {
    const villages = [v(3), v(1), v(2)]

    expect(sortVillages(villages, null).map((x) => x.village_id)).toEqual([3, 1, 2])
  })

  it('does not mutate the array it was given', () => {
    const villages = [v(3), v(1)]

    sortVillages(villages, 'name')

    expect(villages.map((x) => x.village_id)).toEqual([3, 1])
  })

  it('puts the worst crop deficit first when sorting ascending', () => {
    const villages = [
      v(1, { crop_per_hour: 68131 }),
      v(2, { crop_per_hour: -45617 }),
      v(3, { crop_per_hour: 1123 }),
    ]

    expect(sortVillages(villages, 'crop', 'asc').map((x) => x.village_id)).toEqual([2, 3, 1])
  })

  it('sorts an unreadable rate to the end in BOTH directions', () => {
    // Either extreme would assert a magnitude the snapshot does not have.
    const villages = [v(1, { crop_per_hour: null }), v(2, { crop_per_hour: -5 }), v(3, { crop_per_hour: 5 })]

    expect(sortVillages(villages, 'crop', 'asc').map((x) => x.village_id)).toEqual([2, 3, 1])
    expect(sortVillages(villages, 'crop', 'desc').map((x) => x.village_id)).toEqual([3, 2, 1])
  })

  it('sorts by name case-insensitively', () => {
    const villages = [v(1, { name: 'beta' }), v(2, { name: 'Alpha' })]

    expect(sortVillages(villages, 'name', 'asc').map((x) => x.village_id)).toEqual([2, 1])
  })

  it('sorts by free merchants so the villages that cannot ship surface', () => {
    const villages = [v(1, { merchants_free: 5 }), v(2, { merchants_free: 0 })]

    expect(sortVillages(villages, 'merchants', 'asc').map((x) => x.village_id)).toEqual([2, 1])
  })
})

describe('isolating a problem set', () => {
  const villages = [
    v(1, { crop_per_hour: -100 }),
    v(2, { crop_per_hour: 100, merchants_free: 0 }),
    v(3, { crop_per_hour: null }),
  ]

  it('returns everything when no filter is chosen', () => {
    expect(filterVillages(villages, null)).toHaveLength(3)
  })

  it('narrows to the starving villages', () => {
    expect(filterVillages(villages, 'starving').map((x) => x.village_id)).toEqual([1])
  })

  it('narrows to villages with no merchant at home', () => {
    expect(filterVillages(villages, 'noMerchants').map((x) => x.village_id)).toEqual([2])
  })

  it('narrows to villages whose crop rate could not be read', () => {
    expect(filterVillages(villages, 'unknownCrop').map((x) => x.village_id)).toEqual([3])
  })

  it('narrows to villages with no Trade Office level typed', () => {
    expect(filterVillages(villages, 'missingTradeOffice', { 1: 7 }).map((x) => x.village_id)).toEqual([
      2, 3,
    ])
  })

  it('returns everything for a filter it does not know', () => {
    // An empty table would read as "no villages", which is a different and
    // alarming claim from "that filter does not exist".
    expect(filterVillages(villages, 'nonsense')).toHaveLength(3)
  })

  it('every advertised filter has a label the UI can render', () => {
    for (const [key, spec] of Object.entries(FILTERS)) {
      expect(spec.label, `${key} has no label`).toBeTruthy()
      expect(spec.match).toBeTypeOf('function')
    }
  })
})


describe('cycling a column header', () => {
  it('sorts ascending on a column that was not sorted', () => {
    expect(nextSort({ key: null, direction: 'asc' }, 'crop')).toEqual({
      key: 'crop',
      direction: 'asc',
    })
  })

  it('flips to descending on the second click', () => {
    expect(nextSort({ key: 'crop', direction: 'asc' }, 'crop')).toEqual({
      key: 'crop',
      direction: 'desc',
    })
  })

  it('returns to the account order on the third click', () => {
    // The incoming order is meaningful, so it has to be reachable again without
    // a page reload.
    expect(nextSort({ key: 'crop', direction: 'desc' }, 'crop')).toEqual({
      key: null,
      direction: 'asc',
    })
  })

  it('starts a different column fresh at ascending', () => {
    expect(nextSort({ key: 'crop', direction: 'desc' }, 'name')).toEqual({
      key: 'name',
      direction: 'asc',
    })
  })

  it('is pure, so calling it twice with the same input gives the same answer', () => {
    // The property that makes it safe inside a React state updater.
    const state = { key: 'crop', direction: 'asc' }

    expect(nextSort(state, 'crop')).toEqual(nextSort(state, 'crop'))
    expect(state).toEqual({ key: 'crop', direction: 'asc' })
  })
})
