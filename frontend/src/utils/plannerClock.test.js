import { describe, expect, it } from 'vitest'

import {
  MINUTES_IN_DAY,
  coversSmallHours,
  describeOvernight,
  dispatchWindowFor,
  hhmmToMinutes,
  isOvernightProfile,
  minutesToHhmm,
  overnightField,
  overnightMapOnly,
  windowDayShare,
  windowMinutes,
  windowWrapsMidnight,
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

describe('windowWrapsMidnight', () => {
  it('is true for the night this account states as one window', () => {
    expect(windowWrapsMidnight(['23:00', '07:00'])).toBe(true)
  })

  it('is false for a window inside one day', () => {
    expect(windowWrapsMidnight(['07:00', '23:00'])).toBe(false)
  })

  // The half of a SPLIT night that runs up to the morning switch. It wraps in
  // neither direction, which is why the derivation alone gets it wrong.
  it('is false for the post-midnight half of a split night', () => {
    expect(windowWrapsMidnight(['00:00', '07:00'])).toBe(false)
  })

  it('is false when there is no usable window', () => {
    expect(windowWrapsMidnight(null)).toBe(false)
    expect(windowWrapsMidnight(['07:00', '07:00'])).toBe(false)
  })
})

describe('isOvernightProfile', () => {
  // The clock derives, and the operator's declaration WINS -- the same
  // asymmetry `npc_attended` has, and the backend's own rule
  // (`is_night_window(window, overnight=...)` returns `overnight` first).
  it('derives the night from a window that wraps past midnight', () => {
    expect(isOvernightProfile(['23:00', '07:00'], null)).toBe(true)
    expect(isOvernightProfile(['07:00', '23:00'], null)).toBe(false)
  })

  it('lets a declaration overrule the clock in both directions', () => {
    // The half of a split night the derivation calls a day profile.
    expect(isOvernightProfile(['00:00', '07:00'], true)).toBe(true)
    // A near-24h day profile: [420, 419] wraps and is not the night.
    expect(isOvernightProfile(['07:00', '06:59'], false)).toBe(false)
  })

  it('ignores anything that is not a boolean declaration', () => {
    expect(isOvernightProfile(['23:00', '07:00'], 'yes')).toBe(true)
    expect(isOvernightProfile(['00:00', '07:00'], undefined)).toBe(false)
  })
})

describe('overnightField', () => {
  it('sends a declaration of either polarity', () => {
    expect(overnightField({ declared: true, hasWindow: true })).toEqual({ overnight: true })
    expect(overnightField({ declared: false, hasWindow: true })).toEqual({ overnight: false })
  })

  it('omits it when nothing was declared, so the backend derives', () => {
    expect(overnightField({ declared: null, hasWindow: true })).toEqual({})
    expect(overnightField({ declared: 'yes', hasWindow: true })).toEqual({})
  })

  // NOT the same rule `npcAttendedField` had and lost. This one is the
  // backend's own refusal: `_overnight_needs_hours_to_be_overnight` raises on
  // `overnight` with no `dispatch_window`, because section 6's deadline is
  // measured against the window's END and a declaration with no window
  // decides nothing.
  it('omits it with no window, which the backend refuses outright', () => {
    expect(overnightField({ declared: true, hasWindow: false })).toEqual({})
    expect(overnightField({ declared: false, hasWindow: false })).toEqual({})
  })
})

describe('overnightMapOnly', () => {
  it('keeps both polarities', () => {
    expect(overnightMapOnly({ Night: true, Day: false })).toEqual({ Night: true, Day: false })
  })

  it('drops anything that is not a boolean, so a stored string is not a night', () => {
    expect(overnightMapOnly({ Night: 'yes', Day: 1, Dusk: null })).toEqual({})
    expect(overnightMapOnly(null)).toEqual({})
  })
})

describe('describeOvernight', () => {
  it('names a declaration as a declaration', () => {
    expect(describeOvernight(true, ['00:00', '07:00'])).toMatch(/you said/i)
    expect(describeOvernight(true, ['00:00', '07:00'])).toMatch(/night/i)
    expect(describeOvernight(false, ['23:00', '07:00'])).toMatch(/you said/i)
  })

  it('names a derived answer as derived, and says which way', () => {
    expect(describeOvernight(null, ['23:00', '07:00'])).toMatch(/from the hours/i)
    expect(describeOvernight(null, ['23:00', '07:00'])).toMatch(/night/i)
    expect(describeOvernight(null, ['07:00', '23:00'])).toMatch(/from the hours/i)
  })

  it('says there is nothing to derive from when the profile has no hours', () => {
    expect(describeOvernight(null, null)).toMatch(/no hours/i)
  })
})
