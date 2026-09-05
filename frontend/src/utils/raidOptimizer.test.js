import { describe, expect, it } from 'vitest'

import {
  findCompositions,
  inventoryFromTroopsAPI,
  smithyFromAPI,
} from './raidOptimizer'

// Same inventory, smithy and defense constraints `func-raidoptimizer.pw.js`
// drives through the UI, and the same defaults `RaidOptimizer.jsx` starts
// with for defZero/defBudget/budget. Small enough that phase 1's naive
// per-unit caps (capRange()) already multiply out under PHASE1_BUDGET, so
// `fitCapsToBudget` is a no-op here -- this pins the search's answer, not
// just its speed.
const SMALL_INVENTORY = inventoryFromTroopsAPI({ t1: 40, t2: 8, t3: 25, t4: 90, t5: 4, t6: 12 })
const SMALL_SMITHY = smithyFromAPI({ found: true, research: { t1: 3, t2: 0, t3: 7, t5: 1, t6: 12 } })
const DEF_ZERO = 330
const DEF_BUDGET = 420
const BUDGET = 1500

describe('findCompositions', () => {
  it('picks the same best composition per strategy for a known small army', () => {
    const result = findCompositions(SMALL_INVENTORY, DEF_ZERO, DEF_BUDGET, BUDGET, SMALL_SMITHY)

    expect(result.balance.top.p).toEqual({ c: 11, sp: 5, a: 11, pa: 2, t: 12 })
    expect(result.raids.top.p).toEqual({ c: 10, sp: 0, a: 10, pa: 4, t: 11 })
    expect(result.zero.top.p).toEqual({ c: 11, sp: 5, a: 11, pa: 2, t: 12 })
    expect(result.death.top.p).toEqual({ c: 10, sp: 0, a: 10, pa: 4, t: 11 })
  })

  // Phase 1 (the 5-nested-loop exhaustive small-packet sweep) is the PRODUCT
  // of its five per-unit bounds. Before `fitCapsToBudget`, an ordinary
  // mid-game Teuton stockpile with all five slots non-zero pushed that
  // product past 2.5 billion, run synchronously -- measured on the machine
  // this was fixed on: still empty after 170 seconds, tab unresponsive
  // throughout. This is exactly that army; it must answer in well under a
  // second, not lock up whatever thread calls it.
  it('answers for a real, all-five-slots-full Teuton army in well under a second', () => {
    const inv = inventoryFromTroopsAPI({ t1: 1500, t2: 200, t3: 800, t4: 90, t5: 60, t6: 400 })
    const smithy = smithyFromAPI({ found: true, research: { t1: 3, t2: 0, t3: 7, t5: 1, t6: 12 } })

    const start = Date.now()
    const result = findCompositions(inv, DEF_ZERO, DEF_BUDGET, BUDGET, smithy)
    const elapsed = Date.now() - start

    expect(elapsed).toBeLessThan(2000)
    expect(result.balance).not.toBeNull()
  })
})
