import { describe, it, expect } from 'vitest'
import { METER_TOLERANCE, METER_TONE, allocationMeterSeverity } from './plannerAllocation'

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
