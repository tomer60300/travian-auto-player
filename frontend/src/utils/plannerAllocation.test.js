import { describe, it, expect } from 'vitest'
import {
  METER_TOLERANCE,
  METER_TONE,
  allocationMeterSeverity,
  planCellFigures,
  villageNetIndex,
  withEditedAllocation,
} from './plannerAllocation'

describe('allocationMeterSeverity', () => {
  it('reads an over-allocation as an error even with a Rest village set', () => {
    // The regression this exists for: a Rest village used to make ANY slack
    // "settled", so setting 130% of a resource read as green while the Rest
    // village was being asked to ship crop nobody produced.
    expect(allocationMeterSeverity(-42000, true)).toBe('over')
    expect(allocationMeterSeverity(-42000, false)).toBe('over')
  })

  it('never calls a negative slack settled, at any magnitude past tolerance', () => {
    for (const slack of [-1.5, -100, -9_999_999]) {
      expect(allocationMeterSeverity(slack, true)).toBe('over')
      expect(METER_TONE[allocationMeterSeverity(slack, true)]).not.toBe('text-success')
    }
  })

  it('is settled at exactly 100% allocated', () => {
    expect(allocationMeterSeverity(0, false)).toBe('settled')
    expect(allocationMeterSeverity(0, true)).toBe('settled')
  })

  it('treats sub-unit residue as rounding, not as over-allocation', () => {
    // Percentage targets are floating point; a fraction of a unit per hour is
    // not a real breach and must not turn the meter red.
    expect(allocationMeterSeverity(-0.4, false)).toBe('settled')
    expect(allocationMeterSeverity(0.4, false)).toBe('settled')
    expect(allocationMeterSeverity(-METER_TOLERANCE, false)).toBe('settled')
  })

  it('never labels a negative slack as merely unassigned', () => {
    // "unassigned" reads as spare production; the sign says the opposite.
    for (const slack of [-METER_TOLERANCE, -1.01, -3, -50000]) {
      for (const hasRemainder of [true, false]) {
        expect(allocationMeterSeverity(slack, hasRemainder)).not.toBe('unassigned')
      }
    }
  })

  it('is settled when a Rest village absorbs unassigned production', () => {
    expect(allocationMeterSeverity(4200, true)).toBe('settled')
  })

  it('is only worth knowing when slack has nowhere to go', () => {
    // Unclaimed production stays where it was made — a warning, not a breakage.
    expect(allocationMeterSeverity(4200, false)).toBe('unassigned')
  })
})

describe('METER_TONE', () => {
  it('maps every severity to a theme token, red for the breaking one', () => {
    expect(METER_TONE).toEqual({
      over: 'text-danger',
      settled: 'text-success',
      unassigned: 'text-warning',
    })
  })
})

describe('villageNetIndex', () => {
  const plan = {
    village_nets: [
      { village_id: 1, resource: 'lumber', target_per_hour: 100, net_per_hour: 40 },
      { village_id: 2, resource: 'lumber', target_per_hour: 200, net_per_hour: 0 },
      { village_id: 1, resource: 'crop', target_per_hour: 300, net_per_hour: -5 },
    ],
  }

  it('indexes the plan by resource and then village', () => {
    const index = villageNetIndex(plan)

    expect(index.lumber[1].target_per_hour).toBe(100)
    expect(index.lumber[2].net_per_hour).toBe(0)
    expect(index.crop[1].net_per_hour).toBe(-5)
  })

  it('is an empty index before there is a plan, not a crash', () => {
    // The grid renders on every keystroke of the Allocate stage, with no plan
    // for most of them.
    expect(villageNetIndex(null)).toEqual({})
    expect(villageNetIndex({})).toEqual({})
    expect(villageNetIndex({ village_nets: [] })).toEqual({})
  })

  it('has no row for a village the plan never mentioned', () => {
    expect(villageNetIndex(plan).lumber[99]).toBeUndefined()
    expect(villageNetIndex(plan).iron).toBeUndefined()
  })
})

describe('planCellFigures', () => {
  // The stock-floor case from R4-P2-1, in the operator's own numbers: 02 makes
  // 5,000/h of lumber, keeps its own production, and a 30% floor on a 1,200,000
  // warehouse over a 16h day is 22,500/h of supplement -- of which the plan
  // ships 15,000 out. KEEP's plan target INCLUDES the supplement; the page's
  // local derivation cannot know about it.
  const floored = {
    village_id: 2,
    resource: 'lumber',
    own_per_hour: 5_000,
    supplement_per_hour: 15_000,
    target_per_hour: 20_000,
    ship_per_hour: 0,
    consumption_per_hour: 4_000,
    net_per_hour: 16_000,
  }

  it('takes every figure off the plan, so the three lines cannot contradict', () => {
    const figures = planCellFigures({
      planned: floored,
      own: 5_000,
      localTarget: 5_000, // what `targetFor` derives for KEEP: own production
      declaredSpend: 4_000,
    })

    // The bug: the top line read 5,000/h (local) while the net line read
    // 16,000 (plan) -- "5,000/h ... -4,000 = 16,000 net" in one cell, off by
    // exactly the supplement.
    expect(figures).toEqual({
      target: 20_000,
      ship: 0,
      spent: 4_000,
      net: 16_000,
      supplement: 15_000,
    })
    expect(figures.target - figures.spent).toBe(figures.net)
  })

  it('never derives the cargo when the plan states it', () => {
    // `target - own` is 15,000 here and the real cargo is 0: the supplement
    // funds the whole difference, so deriving the delta invents a route.
    const figures = planCellFigures({
      planned: floored,
      own: 5_000,
      localTarget: 5_000,
      declaredSpend: null,
    })

    expect(figures.ship).toBe(0)
    expect(figures.target).toBe(20_000)
    // Named, so the cell can say what funds a retention four times the
    // village's own production with no cargo behind it.
    expect(figures.supplement).toBe(15_000)
  })

  it('leaves the spend and net lines unrendered where nothing is declared', () => {
    const figures = planCellFigures({
      planned: floored,
      own: 5_000,
      localTarget: 5_000,
      declaredSpend: null,
    })

    expect(figures.spent).toBeNull()
    expect(figures.net).toBeNull()
  })

  it('prints the spend the PLAN used, not the one the operator typed', () => {
    // The planner sets aside a declared spend whose rate it could not read.
    const figures = planCellFigures({
      planned: { ...floored, consumption_per_hour: 0, net_per_hour: 20_000 },
      own: 5_000,
      localTarget: 5_000,
      declaredSpend: 4_000,
    })

    expect(figures.spent).toBe(0)
    expect(figures.net).toBe(20_000)
  })

  it('previews the inputs on screen while there is no plan', () => {
    const figures = planCellFigures({
      planned: undefined,
      own: 5_000,
      localTarget: 12_000,
      declaredSpend: 4_000,
    })

    expect(figures).toEqual({
      target: 12_000,
      ship: 7_000,
      spent: 4_000,
      net: 8_000,
      // The floor is a warehouse LEVEL spread across the profile's hours,
      // which only the planner works out -- so the preview cannot show one.
      supplement: 0,
    })
  })

  it('says nothing about an unreadable rate rather than guessing', () => {
    // `own` is null when the snapshot could not read the rate, and `localTarget`
    // is null when the backend would drop the allocation with it.
    expect(planCellFigures({ planned: null, own: null, localTarget: 5_000, declaredSpend: 1 }).ship)
      .toBeNull()
    const unknown = planCellFigures({
      planned: null,
      own: 5_000,
      localTarget: null,
      declaredSpend: 1,
    })
    expect(unknown.ship).toBeNull()
    expect(unknown.net).toBeNull()
  })
})

describe('withEditedAllocation', () => {
  // Profile section 2.1's defensive template, and the village that showed the
  // defect: 11 produces 1,500 lumber an hour and its role says hold 8,372.
  const DEF_TEMPLATE = {
    allocations: {
      lumber: { mode: 'absolute', value: 8372 },
      clay: { mode: 'absolute', value: 5168 },
    },
  }

  it('seeds a templated cell from the figure the cell is showing', () => {
    // The trace: a templated village has NO own entry, so seeding the merge
    // from a `keep` literal turned a value-only patch into a KEEP. The mode
    // flipped to "Keep own", the box disabled at 12,000, the cell was marked a
    // deviation, and the request carried a KEEP -- which the backend resolves
    // to "hold your own production", so village 11 retained 1,500/h while
    // still spending the template's 8,372. Neither figure was ever on screen.
    const per = withEditedAllocation({
      perVillage: {},
      villageId: 20011,
      template: DEF_TEMPLATE,
      resource: 'lumber',
      patch: { value: 12_000 },
    })

    expect(per[20011]).toEqual({ mode: 'absolute', value: 12_000 })
  })

  it('keeps a mode-only patch on the template value it was showing', () => {
    // The mirror case: changing only the mode must not silently zero the
    // figure beside it, which a `value: 0` seed would.
    const per = withEditedAllocation({
      perVillage: {},
      villageId: 20011,
      template: DEF_TEMPLATE,
      resource: 'lumber',
      patch: { mode: 'percentage' },
    })

    expect(per[20011]).toEqual({ mode: 'percentage', value: 8372 })
  })

  it('takes the village entry over the template where it has one', () => {
    const per = withEditedAllocation({
      perVillage: { 20011: { mode: 'sustain', value: 120 } },
      villageId: 20011,
      template: DEF_TEMPLATE,
      resource: 'lumber',
      patch: { value: 130 },
    })

    expect(per[20011]).toEqual({ mode: 'sustain', value: 130 })
  })

  it('falls through to keep where neither the village nor a role says anything', () => {
    // The untemplated account, which is every account today: an empty cell
    // edited to absolute 4,000 must read exactly that.
    expect(
      withEditedAllocation({
        perVillage: {},
        villageId: 20011,
        template: undefined,
        resource: 'lumber',
        patch: { mode: 'absolute', value: 4_000 },
      })[20011]
    ).toEqual({ mode: 'absolute', value: 4_000 })
    expect(
      withEditedAllocation({
        perVillage: {},
        villageId: 20011,
        template: undefined,
        resource: 'lumber',
        patch: { value: 4_000 },
      })[20011]
    ).toEqual({ mode: 'keep', value: 4_000 })
  })

  it('leaves every other village of the resource alone', () => {
    const per = withEditedAllocation({
      perVillage: {
        20002: { mode: 'remainder', value: 0 },
        20013: { mode: 'absolute', value: 5168 },
      },
      villageId: 20011,
      template: DEF_TEMPLATE,
      resource: 'lumber',
      patch: { value: 12_000 },
    })

    expect(per[20002]).toEqual({ mode: 'remainder', value: 0 })
    expect(per[20013]).toEqual({ mode: 'absolute', value: 5168 })
  })

  it('reads a resource the template has no opinion about as keep', () => {
    // Per RESOURCE, like every other reader here: the DEF profile above says
    // nothing about iron, so an iron edit starts from the village's own
    // production rather than from its clay figure.
    expect(
      withEditedAllocation({
        perVillage: {},
        villageId: 20011,
        template: DEF_TEMPLATE,
        resource: 'iron',
        patch: { value: 7 },
      })[20011]
    ).toEqual({ mode: 'keep', value: 7 })
  })
})
