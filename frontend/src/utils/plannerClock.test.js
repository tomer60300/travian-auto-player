import { describe, expect, it } from 'vitest'

import {
  MINUTES_IN_DAY,
  coversSmallHours,
  dispatchWindowFor,
  hhmmToMinutes,
  minutesToHhmm,
  windowDayShare,
  windowMinutes,
} from './plannerClock'

describe('hhmmToMinutes', () => {
  it('reads a clock time', () => {
    expect(hhmmToMinutes('00:00')).toBe(0)
    expect(hhmmToMinutes('07:00')).toBe(420)
    expect(hhmmToMinutes('23:59')).toBe(1439)
  })

  it('refuses anything that is not one, rather than returning NaN', () => {
    expect(hhmmToMinutes('')).toBeNull()
    expect(hhmmToMinutes('7pm')).toBeNull()
    expect(hhmmToMinutes('24:00')).toBeNull()
    expect(hhmmToMinutes('12:60')).toBeNull()
    expect(hhmmToMinutes(undefined)).toBeNull()
  })
})

describe('minutesToHhmm', () => {
  it('round trips a clock time', () => {
    expect(minutesToHhmm(0)).toBe('00:00')
    expect(minutesToHhmm(420)).toBe('07:00')
    expect(minutesToHhmm(1439)).toBe('23:59')
  })

  it('wraps a minute past the end of the day rather than printing 25:00', () => {
    expect(minutesToHhmm(MINUTES_IN_DAY + 30)).toBe('00:30')
  })

  it('says so when there is no minute', () => {
    expect(minutesToHhmm(null)).toBe('—')
  })
})

describe('dispatchWindowFor', () => {
  it('gives the backend its minute pair', () => {
    expect(dispatchWindowFor(['07:00', '23:00'])).toEqual([420, 1380])
  })

  it('keeps a window that wraps past midnight in the order it was typed', () => {
    expect(dispatchWindowFor(['23:00', '07:00'])).toEqual([1380, 420])
  })

  // All three unusable shapes collapse to null, because the request treats them
  // identically: the field is omitted and the route set runs round the clock.
  it('is null for a missing, unparseable or zero-width pair', () => {
    expect(dispatchWindowFor(null)).toBeNull()
    expect(dispatchWindowFor(['', ''])).toBeNull()
    expect(dispatchWindowFor(['07:00'])).toBeNull()
    expect(dispatchWindowFor(['07:00', '07:00'])).toBeNull()
  })
})

describe('windowMinutes and windowDayShare', () => {
  it('measures a daytime window', () => {
    expect(windowMinutes(['07:00', '23:00'])).toBe(960)
    expect(windowDayShare(['07:00', '23:00'])).toBeCloseTo(960 / 1440)
  })

  it('measures a window that wraps past midnight, which is the night', () => {
    expect(windowMinutes(['23:00', '07:00'])).toBe(480)
    expect(windowDayShare(['23:00', '07:00'])).toBeCloseTo(1 / 3)
  })

  it('is null when the pair owns no hours', () => {
    expect(windowMinutes(['07:00', '07:00'])).toBeNull()
    expect(windowDayShare(null)).toBeNull()
  })
})

describe('coversSmallHours', () => {
  it('is true for the night window this account runs', () => {
    expect(coversSmallHours(['23:00', '07:00'])).toBe(true)
  })

  it('is true for a window entirely inside the small hours', () => {
    expect(coversSmallHours(['01:30', '03:30'])).toBe(true)
  })

  it('is false for a daytime window', () => {
    expect(coversSmallHours(['07:00', '23:00'])).toBe(false)
  })

  // Deliberately narrow: plenty of people are awake at 23:30 and at 06:00, so
  // the clock is only allowed to speak where it has something to say.
  it('is false for a late evening window that stops before 01:00', () => {
    expect(coversSmallHours(['21:00', '00:30'])).toBe(false)
  })

  it('is false when there is no window at all', () => {
    expect(coversSmallHours(null)).toBe(false)
  })
})
