/** Profile section 5's declared relay tier, on the page side.
 *
 * The backend refuses a malformed tier with a 422 naming the villages. That is
 * the right place for the rule to LIVE, and the wrong place for the operator to
 * meet it: a 422 arriving from a plan call names a village in a 26-row table and
 * nothing on screen points at the cell that caused it. So the same four
 * refusals are computed from live state as the tier is typed --
 * `relayTierProblems`, the same shape `unreachableCaps` already has for the
 * merchant cap -- and the file parser refuses them at import, where the message
 * can still say which row.
 *
 * Kept in its own spec rather than appended to plannerSetup.test.js because the
 * subject is one feature across three surfaces (the live check, the round trip,
 * and the merge against a changed account), and reading them together is how the
 * three are kept saying the same thing.
 */

import { describe, expect, it } from 'vitest'

import {
  ROLES_THAT_MAY_NOT_RELAY,
  SETUP_FORMAT,
  SETUP_VERSION,
  SetupFileError,
  buildSetup,
  describeRelayFor,
  mergeSetup,
  parseSetup,
  relayTierProblems,
  relayTierProblemsByVillage,
} from './plannerSetup'

// 02 the capital, 18 and 14 its neighbours, 11 / 17 / 19 the defensive villages
// beyond them -- the account profile section 5 is written about.
const VILLAGES = [
  { village_id: 2, name: '02', merchants_total: 20 },
  { village_id: 14, name: '14', merchants_total: 20 },
  { village_id: 18, name: '18', merchants_total: 20 },
  { village_id: 11, name: '11', merchants_total: 20 },
  { village_id: 17, name: '17', merchants_total: 20 },
  { village_id: 19, name: '19', merchants_total: 20 },
]

const TIER = { 18: [11, 17], 14: [19] }
const STAMP = '2026-09-03T12-00-00'

function roundTrip(doc) {
  return parseSetup(JSON.stringify(doc))
}

describe('relayTierProblems', () => {
  it('says nothing about the tier the operator actually chose', () => {
    expect(relayTierProblems(TIER, VILLAGES, {})).toEqual([])
  })

  it('says nothing when no tier is declared at all', () => {
    expect(relayTierProblems({}, VILLAGES, {})).toEqual([])
    expect(relayTierProblems(undefined, VILLAGES, {})).toEqual([])
  })

  it('names a downstream village the account does not have', () => {
    const problems = relayTierProblems({ 18: [11, 4242] }, VILLAGES, {})

    expect(problems).toHaveLength(1)
    expect(problems[0]).toContain('18')
    expect(problems[0]).toContain('4242')
  })

  it('names a relay that relays for itself', () => {
    const problems = relayTierProblems({ 18: [11, 18] }, VILLAGES, {})

    expect(problems.some((p) => p.includes('its own relay'))).toBe(true)
  })

  it.each(ROLES_THAT_MAY_NOT_RELAY)('refuses %s as a relay, naming the role', (role) => {
    const problems = relayTierProblems({ 18: [11] }, VILLAGES, { 18: role })

    expect(problems).toHaveLength(1)
    expect(problems[0]).toContain('18')
    expect(problems[0]).toMatch(/5\.9/)
  })

  it('accepts a feeder, which is the case section 5.9 permits', () => {
    expect(relayTierProblems({ 18: [11] }, VILLAGES, { 18: 'feeder' })).toEqual([])
  })

  it('accepts a village with no role, which is most of an account', () => {
    expect(relayTierProblems({ 18: [11] }, VILLAGES, { 11: 'def' })).toEqual([])
  })

  it('names BOTH villages when a relay feeds a relay', () => {
    // Both, because the fix is to move a downstream and neither half of the
    // pair identifies which one on its own.
    const problems = relayTierProblems({ 18: [14], 14: [19] }, VILLAGES, {})

    expect(problems).toHaveLength(1)
    expect(problems[0]).toContain('18')
    expect(problems[0]).toContain('14')
    expect(problems[0]).toContain('One hop only')
  })

  it('names a downstream typed twice in one list', () => {
    // A duplicate is one downstream. The tier draws its collecting leg from the
    // sum of the gaps it forwards, so a village named twice is sized twice --
    // measured on the backend fixture at 16,744/h against an 8,372/h target,
    // with the downstream it displaced reported unreachable.
    const problems = relayTierProblems({ 18: [11, 11, 17] }, VILLAGES, {})

    expect(problems).toHaveLength(1)
    expect(problems[0]).toContain('18')
    expect(problems[0]).toContain('11')
    expect(problems[0]).toContain('once')
  })

  it('names BOTH relays when two of them claim the same downstream', () => {
    // The same over-ship from the other direction, and neither list is wrong on
    // its own -- so neither relay identifies which one to edit.
    const problems = relayTierProblems({ 18: [11], 14: [11] }, VILLAGES, {})

    expect(problems).toHaveLength(1)
    expect(problems[0]).toContain('11')
    expect(problems[0]).toContain('18')
    expect(problems[0]).toContain('14')
  })

  it('says nothing when two relays serve different downstreams', () => {
    expect(relayTierProblems({ 18: [11, 17], 14: [19] }, VILLAGES, {})).toEqual([])
  })

  it('does not treat a half-typed empty list as a problem', () => {
    // The picker starts one that way between opening and the first tick.
    // `buildPlanPayload` drops it and the file parser refuses it, which are the
    // two places it could do harm.
    expect(relayTierProblems({ 18: [] }, VILLAGES, {})).toEqual([])
  })

  it('reports every problem, not just the first', () => {
    const problems = relayTierProblems({ 18: [18, 4242], 14: [19] }, VILLAGES, { 18: 'def' })

    expect(problems.length).toBeGreaterThanOrEqual(3)
  })
})

describe('relayTierProblemsByVillage', () => {
  // What the page actually renders from: each problem has to land on the cell
  // that has to change, and the flat list above is the same content joined.
  it('keys every problem to the relay whose list has to change', () => {
    const byVillage = relayTierProblemsByVillage(
      { 18: [18, 4242], 14: [19] },
      VILLAGES,
      { 18: 'def' }
    )

    expect(Object.keys(byVillage)).toEqual(['18'])
    expect(byVillage[18]).toHaveLength(3)
    expect(relayTierProblems({ 18: [18, 4242], 14: [19] }, VILLAGES, { 18: 'def' })).toEqual(
      byVillage[18]
    )
  })

  it('puts a chain on the relay that declared the second hop', () => {
    // 18 relays for 14, which relays for 19. The fix is to take 14 off 18's
    // list, so the message belongs on 18 -- and 14's own list is fine.
    const byVillage = relayTierProblemsByVillage({ 18: [14], 14: [19] }, VILLAGES, {})

    expect(Object.keys(byVillage)).toEqual(['18'])
    expect(byVillage[18][0]).toContain('One hop only')
  })

  it('is empty for a tier with nothing wrong with it', () => {
    expect(relayTierProblemsByVillage(TIER, VILLAGES, {})).toEqual({})
  })

  it('keys a downstream claimed twice to ONE relay, and names both', () => {
    // Either list can be edited, so the message names both relays -- but it is
    // keyed once, or the summary line beside the table would count one problem
    // as two.
    const byVillage = relayTierProblemsByVillage({ 18: [11], 14: [11] }, VILLAGES, {})

    expect(Object.keys(byVillage)).toEqual(['14'])
    expect(byVillage[14][0]).toContain('18')
  })
})

describe('describeRelayFor', () => {
  it('reads as the default when nothing is stored', () => {
    expect(describeRelayFor(undefined, VILLAGES)).toBe('not a relay')
    expect(describeRelayFor(null, VILLAGES)).toBe('not a relay')
  })

  it('says an empty list is mid-edit rather than pretending a tier exists', () => {
    // The opposite of `ship_only_to`, where an empty list IS the answer
    // "ships to nobody".
    expect(describeRelayFor([], VILLAGES)).toBe('nobody yet')
  })

  it('names up to two villages, then counts', () => {
    expect(describeRelayFor([11], VILLAGES)).toBe('11')
    expect(describeRelayFor([11, 17], VILLAGES)).toBe('11, 17')
    expect(describeRelayFor([11, 17, 19], VILLAGES)).toBe('3 villages')
  })
})

describe('the relay tier in the setup file', () => {
  it('rides the round trip, and the version rose for it', () => {
    const setup = buildSetup({ villages: VILLAGES, relayFor: TIER, exportedAt: STAMP })

    // 7 since the per-profile NPC attendance landed (6 was the relay tier this
    // file is about, 5 the merchant cap, 4 `may_relay`, 3 the role templates).
    // Pinned to a literal on purpose: the version has to rise whenever a field
    // is added, so that an older build refuses a file it would otherwise
    // half-load, and a literal is what makes forgetting the bump a failing test
    // rather than a tautology.
    expect(setup.version).toBe(7)
    expect(setup.villages.find((v) => v.village_id === 18).relay_for).toEqual([11, 17])
    expect(setup.villages.find((v) => v.village_id === 14).relay_for).toEqual([19])
    expect(roundTrip(setup).villages.find((v) => v.village_id === 18).relay_for).toEqual([11, 17])
  })

  it('does not carry a village whose only relay list is empty', () => {
    // An empty list is not a decision, so a village with nothing else typed has
    // nothing to save -- and writing one would produce a file this same parser
    // refuses to read.
    const setup = buildSetup({ villages: VILLAGES, relayFor: { 18: [] }, exportedAt: STAMP })

    expect(setup.villages).toEqual([])
  })

  it('refuses an empty relay_for in a file, naming the row', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 18, name: '18', relay_for: [] }],
    }

    expect(() => roundTrip(doc)).toThrow(SetupFileError)
    expect(() => roundTrip(doc)).toThrow(/empty relay_for/)
  })

  it('refuses a relay_for that is not a list', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 18, name: '18', relay_for: 11 }],
    }

    expect(() => roundTrip(doc)).toThrow(/not a list of village ids/)
  })

  it('refuses an entry that is not a village id', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 18, name: '18', relay_for: ['eighteen'] }],
    }

    expect(() => roundTrip(doc)).toThrow(/relay_for/)
  })

  it('refuses a village that names itself', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 18, name: '18', relay_for: [18] }],
    }

    expect(() => roundTrip(doc)).toThrow(/names itself/)
  })

  it('refuses a role village as a relay, naming the role', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 18, name: '18', role: 'def', relay_for: [11] }],
    }

    expect(() => roundTrip(doc)).toThrow(/5\.9/)
    expect(() => roundTrip(doc)).toThrow(/def/)
  })

  it('accepts a feeder as a relay', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 18, name: '18', role: 'feeder', relay_for: [11] }],
    }

    expect(roundTrip(doc).villages[0].relay_for).toEqual([11])
  })

  it('refuses a relay that feeds a relay, naming both', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [
        { village_id: 18, name: '18', relay_for: [14] },
        { village_id: 14, name: '14', relay_for: [19] },
      ],
    }

    expect(() => roundTrip(doc)).toThrow(/One hop only/)
    expect(() => roundTrip(doc)).toThrow(/18/)
  })

  it('does not check downstream ids against an account, because a file has none', () => {
    // A downstream village with nothing else typed has no row in the file at
    // all, so demanding one would refuse a perfectly good export. `mergeSetup`
    // prunes against live villages and says what it dropped.
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 18, name: '18', relay_for: [11, 17] }],
    }

    expect(roundTrip(doc).villages[0].relay_for).toEqual([11, 17])
  })

  it('reads a v5 file, which simply has no tier', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: 5,
      villages: [{ village_id: 18, name: '18', trade_office_level: 10 }],
    }

    expect(roundTrip(doc).villages[0].relay_for).toBeUndefined()
  })
})

describe('mergeSetup and the relay tier', () => {
  const setupWith = (relayFor) =>
    parseSetup(JSON.stringify(buildSetup({ villages: VILLAGES, relayFor, exportedAt: STAMP })))

  it('loads the tier onto the maps the page holds', () => {
    const merged = mergeSetup({ setup: setupWith(TIER), villages: VILLAGES })

    expect(merged.relayFor).toEqual({ 18: [11, 17], 14: [19] })
  })

  it('leaves a tier the file says nothing about alone', () => {
    // Silence is not a clear, the rule every other column here follows.
    const merged = mergeSetup({
      setup: setupWith({ 18: [11] }),
      villages: VILLAGES,
      relayFor: { 14: [19] },
    })

    expect(merged.relayFor).toEqual({ 18: [11], 14: [19] })
  })

  it('prunes a downstream the account no longer has, and SAYS so', () => {
    // A chiefed or renamed village. Reported rather than quietly dropped: a
    // tier the operator believes is complete, with the plan calling that
    // village unreachable and nothing connecting the two, is the worse outcome.
    const merged = mergeSetup({ setup: setupWith({ 18: [11, 17] }), villages: VILLAGES.slice(0, 4) })

    expect(merged.relayFor).toEqual({ 18: [11] })
    expect(merged.report.relayTargetsDropped).toEqual([
      { village_id: 18, name: '18', dropped: [17] },
    ])
  })

  it('removes a relay left with nobody to feed rather than keeping an empty list', () => {
    // Neither the file nor the backend accepts an empty list, so a merge must
    // not produce one.
    const merged = mergeSetup({
      setup: setupWith({ 18: [11, 17] }),
      villages: VILLAGES.filter((v) => ![11, 17].includes(v.village_id)),
    })

    expect(merged.relayFor).toEqual({})
    expect(merged.report.relayTargetsDropped).toEqual([
      { village_id: 18, name: '18', dropped: [11, 17] },
    ])
  })

  it('reports nothing dropped when the whole tier survives', () => {
    const merged = mergeSetup({ setup: setupWith(TIER), villages: VILLAGES })

    expect(merged.report.relayTargetsDropped).toEqual([])
  })
})
