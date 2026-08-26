import { describe, it, expect } from 'vitest'

import { namesForVillageIds, resolveVillageNames } from './villageRefs'

const VILLAGES = [
  { village_id: 53629, name: '02' },
  { village_id: 41212, name: '18' },
  { village_id: 81449, name: '21' },
  { village_id: 41568, name: '01 Hammer' },
]

describe('resolveVillageNames', () => {
  it('resolves the names the operator actually uses', () => {
    expect(resolveVillageNames('02, 18', VILLAGES).ids).toEqual([53629, 41212])
  })

  it('accepts an id too, so a file or a warning can be pasted straight in', () => {
    expect(resolveVillageNames('53629', VILLAGES).ids).toEqual([53629])
  })

  it('handles a name with a space in it', () => {
    expect(resolveVillageNames('01 Hammer', VILLAGES).ids).toEqual([41568])
  })

  it('is not case sensitive', () => {
    expect(resolveVillageNames('01 hammer', VILLAGES).ids).toEqual([41568])
  })

  it('reports what it could not resolve instead of dropping it', () => {
    // The failure this exists to prevent: "2" is not "02", and a silently
    // discarded entry leaves the operator believing a village is excluded while
    // the next run draws on it.
    const { ids, unknown } = resolveVillageNames('02, 2', VILLAGES)

    expect(ids).toEqual([53629])
    expect(unknown).toEqual(['2'])
  })

  it('ignores blank entries from trailing commas and stray spaces', () => {
    // Mid-typing states must not read as errors, or the field fights the typist.
    const { ids, unknown } = resolveVillageNames(' 02 , , 18 ,', VILLAGES)

    expect(ids).toEqual([53629, 41212])
    expect(unknown).toEqual([])
  })

  it('does not repeat a village named twice', () => {
    expect(resolveVillageNames('02, 02, 53629', VILLAGES).ids).toEqual([53629])
  })

  it('is empty for empty input rather than throwing', () => {
    expect(resolveVillageNames('', VILLAGES)).toEqual({ ids: [], unknown: [] })
    expect(resolveVillageNames(null, VILLAGES)).toEqual({ ids: [], unknown: [] })
  })

  it('reports everything as unknown when there are no villages yet', () => {
    // Before a snapshot is fetched nothing can be resolved, and claiming an
    // exclusion worked would be worse than saying it did not.
    expect(resolveVillageNames('02', []).unknown).toEqual(['02'])
  })
})

describe('namesForVillageIds', () => {
  it('shows stored ids as the names the operator recognises', () => {
    expect(namesForVillageIds([53629, 41212], VILLAGES)).toBe('02, 18')
  })

  it('falls back to the id when the village is gone', () => {
    // A chiefed village must still be visible in the field, or the operator
    // cannot tell why the exclusion no longer matches anything.
    expect(namesForVillageIds([53629, 999999], VILLAGES)).toBe('02, 999999')
  })

  it('is empty for nothing stored', () => {
    expect(namesForVillageIds([], VILLAGES)).toBe('')
    expect(namesForVillageIds(undefined, VILLAGES)).toBe('')
  })
})
