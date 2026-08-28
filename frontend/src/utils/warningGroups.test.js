import { describe, it, expect } from 'vitest'

import { groupWarnings, warningSignature } from './warningGroups'

// Verbatim lines from the operator's own pasted preview — the 200-line wall
// this utility exists to fold.
const PRUNE_A =
  "Day: route 18 -> 19 repeats every 4h, so the game fires it 6 times a day; only 4 land in this profile's 960 min, and the other 2 ship the same cargo outside it. The plan sized the cargo for the firings inside, so the destination receives about 1.5x what was modelled"
const PRUNE_B =
  "Day: route 25 -> 19 repeats every 1h, so the game fires it 24 times a day; only 16 land in this profile's 960 min, and the other 8 ship the same cargo outside it. The plan sized the cargo for the firings inside, so the destination receives about 1.5x what was modelled"
const PRUNE_NIGHT =
  "Night: route 02 -> 17 repeats every 1h, so the game fires it 24 times a day; only 8 land in this profile's 480 min, and the other 16 ship the same cargo outside it. The plan sized the cargo for the firings inside, so the destination receives about 3.0x what was modelled"
const CAP_A =
  'Day: 02: crop hits the cap and loses about 1,088,988/day — the granary never leaves its cap, because 1,088,988/day more arrives than leaves'
const CAP_B =
  'Day: 15: crop hits the cap and loses about 86,395/day — the granary never leaves its cap, because 86,395/day more arrives than leaves'
const UNIQUE =
  'Day: 01Arb (46|133): the first crop can take up to 4.2h to land (a full cycle plus travel if the route is created just after its scheduled send time), and the full tribute up to 5.5h, so cover it by hand until the first scheduled send lands'

describe('warningSignature', () => {
  it('folds two prune warnings about different routes into one shape', () => {
    expect(warningSignature(PRUNE_A)).toBe(warningSignature(PRUNE_B))
  })

  it('keeps the profile prefix apart — Day noise must not bury Night noise', () => {
    expect(warningSignature(PRUNE_A)).not.toBe(warningSignature(PRUNE_NIGHT))
  })

  it('folds cap-loss warnings about different villages', () => {
    expect(warningSignature(CAP_A)).toBe(warningSignature(CAP_B))
  })

  it('does not fold sentences with different shapes', () => {
    expect(warningSignature(CAP_A)).not.toBe(warningSignature(PRUNE_A))
    expect(warningSignature(UNIQUE)).not.toBe(warningSignature(CAP_A))
  })
})

describe('groupWarnings', () => {
  it('turns the wall into a handful of counted groups, largest first', () => {
    const groups = groupWarnings([PRUNE_A, CAP_A, PRUNE_B, CAP_B, PRUNE_NIGHT, UNIQUE])

    expect(groups[0].count).toBe(2)
    expect(groups.map((g) => g.count)).toEqual([2, 2, 1, 1])
    expect(groups.flatMap((g) => g.lines)).toHaveLength(6)
  })

  it('keeps every original line reachable — grouping must never destroy detail', () => {
    const lines = [PRUNE_A, PRUNE_B, CAP_A]
    const groups = groupWarnings(lines)

    expect(groups.flatMap((g) => g.lines).sort()).toEqual([...lines].sort())
  })

  it('is calm about empty input', () => {
    expect(groupWarnings([])).toEqual([])
    expect(groupWarnings(undefined)).toEqual([])
  })
})
