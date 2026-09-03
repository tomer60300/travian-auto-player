import { describe, expect, it } from 'vitest'

import {
  NPC_FEEDSTOCK_RESOURCES,
  attendanceMapOnly,
  attendanceRequired,
  attendanceFor,
  describeAttendance,
  describeFeedstock,
  isFeedstockList,
  npcAttendedField,
  npcFeedstockField,
  suggestedAttendance,
  unansweredAttendance,
} from './plannerNpc'

describe('attendanceRequired', () => {
  it('is false for an account that declares no stock floor', () => {
    expect(attendanceRequired({})).toBe(false)
    expect(attendanceRequired(undefined)).toBe(false)
  })

  // 0.0 is the same as None at every layer of the backend, so a village whose
  // fraction is zero declares nothing and asks nothing of the operator.
  it('is false for a floor of zero, which is not a floor', () => {
    expect(attendanceRequired({ 101: 0 })).toBe(false)
  })

  it('is true as soon as one village keeps a floor', () => {
    expect(attendanceRequired({ 101: 0, 102: 0.3 })).toBe(true)
  })

  it('ignores a cleared box, which stores undefined rather than a number', () => {
    expect(attendanceRequired({ 101: undefined })).toBe(false)
  })
})

describe('attendanceFor', () => {
  it('reads a stored answer of either polarity', () => {
    expect(attendanceFor({ Day: true }, 'Day')).toBe(true)
    expect(attendanceFor({ Night: false }, 'Night')).toBe(false)
  })

  // The whole point of the field: unanswered is a third state, and reading it
  // as false would fund nothing while reading it as true would fund night
  // routes from trading nobody is doing.
  it('is null when nothing has been said, never false', () => {
    expect(attendanceFor({}, 'Night')).toBeNull()
    expect(attendanceFor(undefined, 'Night')).toBeNull()
    expect(attendanceFor({ Night: 'yes' }, 'Night')).toBeNull()
  })
})

describe('attendanceMapOnly', () => {
  it('keeps both booleans', () => {
    expect(attendanceMapOnly({ Day: true, Night: false })).toEqual({ Day: true, Night: false })
  })

  it('drops anything that is not one, so a stored string cannot pass for an answer', () => {
    expect(attendanceMapOnly({ Day: 'yes', Night: 1, Dusk: null })).toEqual({})
    expect(attendanceMapOnly(null)).toEqual({})
  })
})

describe('unansweredAttendance', () => {
  it('names every profile with hours that has no answer', () => {
    expect(unansweredAttendance(['Day', 'Night'], { Day: true })).toEqual(['Night'])
  })

  it('is empty once both are answered, false included', () => {
    expect(unansweredAttendance(['Day', 'Night'], { Day: true, Night: false })).toEqual([])
  })
})

describe('suggestedAttendance', () => {
  // A suggestion, and only ever a suggestion: it is offered as a chip the
  // operator presses, because the clock cannot know who is awake.
  it('proposes asleep for a window holding the small hours', () => {
    expect(suggestedAttendance(['23:00', '07:00'])).toBe(false)
  })

  it('proposes awake for a daytime window', () => {
    expect(suggestedAttendance(['07:00', '23:00'])).toBe(true)
  })

  it('proposes awake for a round-the-clock profile, which has no night to miss', () => {
    expect(suggestedAttendance(null)).toBe(true)
  })
})

describe('npcAttendedField', () => {
  // Omitted, not guessed. With no window the route set runs round the clock and
  // has no night hours to mis-fund, so the backend reads absent as available.
  it('omits the field when the profile has no hours', () => {
    expect(npcAttendedField({ attended: true, hasWindow: false })).toEqual({})
  })

  it('omits the field when nothing has been answered', () => {
    expect(npcAttendedField({ attended: null, hasWindow: true })).toEqual({})
  })

  it('sends true and false alike, because false is an answer', () => {
    expect(npcAttendedField({ attended: true, hasWindow: true })).toEqual({ npc_attended: true })
    expect(npcAttendedField({ attended: false, hasWindow: true })).toEqual({ npc_attended: false })
  })
})

describe('describeAttendance', () => {
  it('says which side of the day it is in words, not by colour', () => {
    expect(describeAttendance(true)).toMatch(/at the marketplace/i)
    expect(describeAttendance(false)).toMatch(/nobody/i)
    expect(describeAttendance(null)).toMatch(/not answered/i)
  })
})

describe('npc feedstock', () => {
  it('offers all four stores, in the game order', () => {
    expect(NPC_FEEDSTOCK_RESOURCES).toEqual(['lumber', 'clay', 'iron', 'crop'])
  })

  it('reads absent as derived, which is the resting state', () => {
    expect(describeFeedstock(null)).toBe('derived')
    expect(describeFeedstock(undefined)).toBe('derived')
  })

  // An empty list is the picker mid-edit, not an answer: NPC converting from
  // nothing is not a statement the operator can have meant.
  it('says so when the picker is open with nothing ticked', () => {
    expect(describeFeedstock([])).toMatch(/nothing/i)
    expect(isFeedstockList([])).toBe(false)
  })

  it('names the stores it was given', () => {
    expect(describeFeedstock(['clay', 'crop'])).toBe('Clay, Crop')
    expect(isFeedstockList(['clay', 'crop'])).toBe(true)
  })

  it('refuses anything that is not one of the four', () => {
    expect(isFeedstockList(['gold'])).toBe(false)
    expect(isFeedstockList('clay')).toBe(false)
  })

  it('drops an empty override from the request, and sends a real one', () => {
    expect(npcFeedstockField([])).toEqual({})
    expect(npcFeedstockField(null)).toEqual({})
    expect(npcFeedstockField(['clay', 'crop'])).toEqual({ npc_feedstock: ['clay', 'crop'] })
  })

  // The request order is the game's, not the click order: two operators who
  // ticked the same two boxes must produce the same plan digest.
  it('sends the stores in the game order however they were ticked', () => {
    expect(npcFeedstockField(['crop', 'clay'])).toEqual({ npc_feedstock: ['clay', 'crop'] })
  })
})
