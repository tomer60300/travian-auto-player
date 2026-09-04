import { describe, it, expect } from 'vitest'

import {
  excludedOriginIds,
  namesForVillageIds,
  resolveVillageNames,
  unresolvedProtectedEntries,
} from './villageRefs'

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

describe('excludedOriginIds', () => {
  const VS = [
    { village_id: 53629, name: '02' },
    { village_id: 41212, name: '18' },
  ]

  it('uses the ids a loaded setup file stored', () => {
    // The regression this exists for. A file carries ids and no typed text, so
    // reading only the text dropped the exclusion -- and the operator had every
    // reason to believe it was still in force.
    expect(excludedOriginIds({ exclude_origins: [53629] }, VS)).toEqual([53629])
  })

  it('prefers what the operator typed', () => {
    expect(
      excludedOriginIds({ exclude_origins: [53629], exclude_origins_text: '18' }, VS)
    ).toEqual([41212])
  })

  it('treats a cleared field as cleared, not as "fall back to the file"', () => {
    // Emptying the box is a deliberate act. Reverting to the stored ids would
    // make the exclusion impossible to remove.
    expect(
      excludedOriginIds({ exclude_origins: [53629], exclude_origins_text: '' }, VS)
    ).toEqual([])
  })

  it('is empty for a target with neither', () => {
    expect(excludedOriginIds({}, VS)).toEqual([])
    expect(excludedOriginIds(undefined, VS)).toEqual([])
  })

  it('drops a stored value that is not a village id', () => {
    expect(excludedOriginIds({ exclude_origins: [0, -5, 53629] }, VS)).toEqual([53629])
  })
})

describe('unresolvedProtectedEntries', () => {
  // Coordinates, because a hand-made route to a foreign target has no usable
  // village id -- which is why this field accepts both shapes at all.
  const WITH_COORDS = [
    { village_id: 53629, name: '02', x: 12, y: -34 },
    { village_id: 41212, name: '18', x: -5, y: 7 },
  ]

  it('says nothing about an entry that names a real village id', () => {
    expect(unresolvedProtectedEntries('53629', WITH_COORDS)).toEqual([])
  })

  it('says nothing about coordinates, which need not be a village at all', () => {
    // The whole reason the field takes coordinates: the routes worth protecting
    // are the hand-made ones to targets this account does not own, so a pair
    // pointing at nothing in the snapshot is the NORMAL case, not an error.
    expect(unresolvedProtectedEntries('46|133, -5|7', WITH_COORDS)).toEqual([])
  })

  it('flags a bare integer no village has, and offers the coordinate reading', () => {
    // The exact case the backend's own docstring names: "A typo ('4688' for
    // '46|88') that is silently ignored leaves the operator believing a route
    // is protected when it is not, and the very next run switches it off."
    // `4688` is shape-valid as a village id, so the server accepts it and
    // protects nothing.
    expect(unresolvedProtectedEntries('4688', WITH_COORDS)).toEqual([
      { entry: '4688', suggestion: '46|88' },
    ])
  })

  it('splits six digits into two three-digit coordinates', () => {
    expect(unresolvedProtectedEntries('461330', WITH_COORDS)).toEqual([
      { entry: '461330', suggestion: '461|330' },
    ])
  })

  it('offers the ID when the operator typed a village NAME', () => {
    // The account names its villages "02" and "18", which are bare integers --
    // so `int("02")` is 2, the server protects village 2, and the village the
    // operator meant keeps being switched off. Higher confidence than a digit
    // split, so it wins.
    expect(unresolvedProtectedEntries('02', WITH_COORDS)).toEqual([
      { entry: '02', suggestion: '53629' },
    ])
  })

  it('offers nothing it cannot ground, rather than guessing', () => {
    // Five digits do not split evenly and no village is named this, so the
    // honest answer is that nothing matches.
    expect(unresolvedProtectedEntries('53628', WITH_COORDS)).toEqual([
      { entry: '53628', suggestion: null },
    ])
  })

  it('does not split two digits, which would be noise', () => {
    expect(unresolvedProtectedEntries('99', WITH_COORDS)).toEqual([
      { entry: '99', suggestion: null },
    ])
  })

  it('flags an entry that is neither an id nor coordinates', () => {
    // The server 422s this, so the page saying it first is a faster no.
    expect(unresolvedProtectedEntries('Sommerwind', WITH_COORDS)).toEqual([
      { entry: 'Sommerwind', suggestion: null },
    ])
  })

  it('ignores blanks from trailing commas and stray spaces', () => {
    // Mid-typing states must not read as errors, or the field fights the typist.
    expect(unresolvedProtectedEntries(' 53629 , , 46|133 ,', WITH_COORDS)).toEqual([])
  })

  it('is empty for empty input rather than throwing', () => {
    expect(unresolvedProtectedEntries('', WITH_COORDS)).toEqual([])
    expect(unresolvedProtectedEntries(null, WITH_COORDS)).toEqual([])
  })

  it('flags everything when there is no snapshot yet', () => {
    // Nothing can be resolved before a snapshot, and claiming a protection
    // holds would be worse than saying it does not.
    expect(unresolvedProtectedEntries('53629', [])).toEqual([
      { entry: '53629', suggestion: null },
    ])
  })
})
