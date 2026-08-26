import { describe, it, expect } from 'vitest'
import {
  SETUP_FORMAT,
  SETUP_VERSION,
  SetupFileError,
  buildSetup,
  mergeSetup,
  parseSetup,
  setupFilename,
  setupMatchesAccount,
} from './plannerSetup'

const VILLAGES = [
  { village_id: 20030, name: 'Capital' },
  { village_id: 20031, name: 'V05' },
  { village_id: 20032, name: 'V16' },
]

const STAMP = '2026-08-20T14-32-00'

function roundTrip(doc) {
  return parseSetup(JSON.stringify(doc))
}

describe('buildSetup', () => {
  it('carries only villages that have something typed', () => {
    const setup = buildSetup({
      account: 'https://ts2.x1.europe.travian.com|Chieftain',
      villages: VILLAGES,
      tradeOffice: { 20031: 13 },
      cropCeilings: { 20030: 300000 },
      exportedAt: STAMP,
    })
    expect(setup.villages.map((v) => v.village_id)).toEqual([20030, 20031])
    expect(setup.format).toBe(SETUP_FORMAT)
    expect(setup.version).toBe(SETUP_VERSION)
    expect(setup.exported_at).toBe(STAMP)
  })

  it('keeps a level of 0, which is a real answer and not an absent one', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20032: 0 },
      cropCeilings: {},
      exportedAt: STAMP,
    })
    expect(setup.villages).toEqual([
      { village_id: 20032, name: 'V16', trade_office_level: 0 },
    ])
  })

  it('survives a round trip unchanged', () => {
    const tradeOffice = { 20030: 13, 20031: 7, 20032: 0 }
    const cropCeilings = { 20030: 300000 }
    const setup = roundTrip(
      buildSetup({ account: 'a|b', villages: VILLAGES, tradeOffice, cropCeilings, exportedAt: STAMP })
    )
    const merged = mergeSetup({ setup, villages: VILLAGES, tradeOffice: {}, cropCeilings: {} })
    expect(merged.tradeOffice).toEqual(tradeOffice)
    expect(merged.cropCeilings).toEqual(cropCeilings)
  })
})

describe('parseSetup rejects rather than half-loading', () => {
  it('refuses malformed JSON', () => {
    expect(() => parseSetup('{ not json')).toThrow(SetupFileError)
  })

  it('refuses a file that is not a planner setup', () => {
    expect(() => parseSetup(JSON.stringify({ hello: 'world' }))).toThrow(/Not a planner setup file/)
  })

  it('refuses a version this build does not read', () => {
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    expect(() => roundTrip({ ...doc, version: 99 })).toThrow(/version 99/)
  })

  it('refuses a level above the maximum instead of clamping it', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, trade_office_level: 25 }],
    }
    expect(() => roundTrip(doc)).toThrow(/0 to 20/)
  })

  it('refuses a negative level, and a fractional one', () => {
    for (const level of [-1, 3.5]) {
      const doc = {
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, trade_office_level: level }],
      }
      expect(() => roundTrip(doc)).toThrow(SetupFileError)
    }
  })

  it('refuses a row with no usable village id', () => {
    const doc = { format: SETUP_FORMAT, version: SETUP_VERSION, villages: [{ name: 'V05' }] }
    expect(() => roundTrip(doc)).toThrow(/village_id/)
  })

  it('refuses a negative crop ceiling', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, crop_ceiling: -5 }],
    }
    expect(() => roundTrip(doc)).toThrow(/crop ceiling/)
  })
})

describe('mergeSetup', () => {
  it('leaves a village the file does not mention as UNKNOWN, never 0', () => {
    // The planner floors an unknown level to 0 when it plans, but recording
    // that guess as a typed value hides an over-provisioned village.
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 13 }, exportedAt: STAMP })
    )
    const merged = mergeSetup({ setup, villages: VILLAGES, tradeOffice: {}, cropCeilings: {} })
    expect(merged.tradeOffice[20031]).toBeUndefined()
    expect(merged.tradeOffice[20032]).toBeUndefined()
    expect(merged.report.stillUnknown.map((v) => v.village_id)).toEqual([20031, 20032])
  })

  it('reports a village in the file that the account no longer has, and drops it', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [
        { village_id: 20030, name: 'Capital', trade_office_level: 13 },
        { village_id: 99999, name: 'Chiefed', trade_office_level: 9 },
      ],
    }
    const merged = mergeSetup({ setup: roundTrip(doc), villages: VILLAGES })
    expect(merged.tradeOffice[99999]).toBeUndefined()
    expect(merged.report.missingFromAccount).toEqual([{ village_id: 99999, name: 'Chiefed' }])
    expect(merged.report.loaded).toBe(1)
  })

  it('keeps existing values for villages the file does not mention', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 13 }, exportedAt: STAMP })
    )
    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      tradeOffice: { 20031: 4 },
      cropCeilings: { 20031: 120000 },
    })
    expect(merged.tradeOffice).toEqual({ 20030: 13, 20031: 4 })
    expect(merged.cropCeilings).toEqual({ 20031: 120000 })
    expect(merged.report.stillUnknown.map((v) => v.village_id)).toEqual([20032])
  })

  it('lets the file win over an existing value', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 13 }, exportedAt: STAMP })
    )
    const merged = mergeSetup({ setup, villages: VILLAGES, tradeOffice: { 20030: 2 } })
    expect(merged.tradeOffice[20030]).toBe(13)
  })
})

describe('setupMatchesAccount', () => {
  it('catches a file exported from a different account', () => {
    expect(setupMatchesAccount({ account: 'server|Alice' }, 'server|Bob')).toBe(false)
    expect(setupMatchesAccount({ account: 'server|Alice' }, 'server|Alice')).toBe(true)
  })

  it('does not object when either side is unknown', () => {
    expect(setupMatchesAccount({ account: null }, 'server|Bob')).toBe(true)
    expect(setupMatchesAccount({ account: 'server|Alice' }, null)).toBe(true)
  })
})

describe('setupFilename', () => {
  it('strips characters that a filesystem would refuse', () => {
    expect(setupFilename('Chief/tain 2', STAMP)).toBe(`travian-planner-Chief-tain-2-${STAMP}.json`)
  })

  it('falls back when there is no player name', () => {
    expect(setupFilename('', STAMP)).toBe(`travian-planner-account-${STAMP}.json`)
  })
})

// ── Profiles (format version 2) ────────────────────────────────────────
// The Day/Night pair is the largest body of typed state in the planner, and
// before v2 the setup file did not carry it: a cleared origin meant retyping a
// hundred numbers derived from store capacities. These pin the parts that make
// carrying them safe rather than merely possible.

const NIGHT = {
  crop: {
    20030: { mode: 'absolute', value: -8694 },
    20031: { mode: 'absolute', value: 11499 },
    20032: { mode: 'remainder', value: 0 },
  },
  clay: { 20031: { mode: 'absolute', value: -4000 } },
}

describe('profiles in the setup file', () => {
  it('carries profiles, windows and the calibrated merchant model', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      profiles: { Night: NIGHT },
      profileWindows: { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] },
      merchantModel: { base_capacity: 2500, bonus_per_to_level: 0.2 },
      exportedAt: STAMP,
    })
    expect(setup.version).toBe(2)
    expect(setup.profiles.Night.crop[20030].value).toBe(-8694)
    expect(setup.profile_windows.Night).toEqual(['23:00', '07:00'])
    expect(setup.merchant_model.base_capacity).toBe(2500)
  })

  it('omits the profile fields entirely when there are none', () => {
    // An empty object would import as "replace everything with nothing", which
    // is the opposite of what loading a file is for.
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      profiles: {},
      profileWindows: {},
      exportedAt: STAMP,
    })
    expect('profiles' in setup).toBe(false)
    expect('profile_windows' in setup).toBe(false)
  })

  it('survives the round trip with negative retentions intact', () => {
    // A negative absolute is how a store already past its ceiling gets drained
    // instead of merely frozen. Losing the sign would silently change the plan.
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, profiles: { Night: NIGHT }, exportedAt: STAMP })
    )
    expect(setup.profiles.Night.crop[20030]).toEqual({ mode: 'absolute', value: -8694 })
    expect(setup.profiles.Night.clay[20031].value).toBe(-4000)
  })

  it('still reads a version 1 file, which simply has no profiles', () => {
    const legacy = {
      format: SETUP_FORMAT,
      version: 1,
      account: null,
      villages: [{ village_id: 20030, name: 'Capital', trade_office_level: 13 }],
    }
    const setup = roundTrip(legacy)
    expect(setup.villages[0].trade_office_level).toBe(13)
    expect(setup.profiles).toEqual({})
    expect(setup.merchantModel).toBeNull()
  })

  it('rejects an allocation mode the backend does not have', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.profiles = { Night: { crop: { 20030: { mode: 'hoard', value: 1 } } } }
    expect(() => roundTrip(doc)).toThrow(SetupFileError)
  })

  it('rejects an unknown resource rather than dropping it', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.profiles = { Night: { gold: { 20030: { mode: 'absolute', value: 1 } } } }
    expect(() => roundTrip(doc)).toThrow(/unknown resource "gold"/)
  })

  it('rejects a window that is not HH:MM', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.profile_windows = { Night: ['23:00', '7pm'] }
    expect(() => roundTrip(doc)).toThrow(/not HH:MM/)
  })

  it('rejects a merchant model that would divide by nothing', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.merchant_model = { base_capacity: 0, bonus_per_to_level: 0.2 }
    expect(() => roundTrip(doc)).toThrow(/positive number/)
  })
})

describe('mergeSetup with profiles', () => {
  it('replaces a named profile wholesale and leaves the others alone', () => {
    // Half of an old Night profile merged into a new one is a distribution
    // nobody designed, so the file replaces rather than merges within a profile.
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, profiles: { Night: NIGHT }, exportedAt: STAMP })
    )
    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      profiles: {
        Day: { crop: { 20030: { mode: 'remainder', value: 0 } } },
        Night: { iron: { 20030: { mode: 'absolute', value: 999 } } },
      },
    })
    expect(merged.profiles.Day.crop[20030].mode).toBe('remainder')
    expect(merged.profiles.Night.iron).toBeUndefined()
    expect(merged.profiles.Night.crop[20030].value).toBe(-8694)
    expect(merged.report.profilesLoaded).toEqual(['Night'])
  })

  it('drops allocations for villages the account no longer has, and says so', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.profiles = {
      Night: {
        crop: {
          20030: { mode: 'absolute', value: 100 },
          99999: { mode: 'absolute', value: 200 },
        },
      },
    }
    const merged = mergeSetup({ setup: roundTrip(doc), villages: VILLAGES })
    expect(Object.keys(merged.profiles.Night.crop)).toEqual(['20030'])
    expect(merged.report.profileVillagesDropped).toEqual([99999])
  })

  it('drops a resource whose every village vanished, rather than keeping it empty', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.profiles = { Night: { crop: { 99999: { mode: 'absolute', value: 1 } } } }
    const merged = mergeSetup({ setup: roundTrip(doc), villages: VILLAGES })
    expect(merged.profiles.Night).toEqual({})
  })

  it('carries the windows and the merchant model through', () => {
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        profiles: { Night: NIGHT },
        profileWindows: { Night: ['23:00', '07:00'] },
        merchantModel: { base_capacity: 2500, bonus_per_to_level: 0.2 },
        exportedAt: STAMP,
      })
    )
    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      profileWindows: { Day: ['07:00', '23:00'] },
    })
    expect(merged.profileWindows).toEqual({ Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] })
    expect(merged.merchantModel).toEqual({ base_capacity: 2500, bonus_per_to_level: 0.2 })
  })
})

// ── Foreign targets (tributes) ─────────────────────────────────────────────
// The last piece of typed state the file did not carry. A tribute is entirely
// operator-supplied -- the game will not tell us an ally needs 25,700 crop an
// hour -- and it drives real routes, so losing it to a cleared origin means the
// obligation silently stops being planned for.

const TRIBUTE = [
  { name: '01Arb', x: 46, y: 133, crop_per_hour: 25700, safety_margin_pct: 0, route_eligible: true },
]

describe('foreign targets in the setup file', () => {
  it('carries them', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      foreignTargets: TRIBUTE,
      exportedAt: STAMP,
    })

    expect(setup.foreign_targets).toEqual(TRIBUTE)
  })

  it('omits the field when there are none', () => {
    const setup = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 5 }, exportedAt: STAMP })
    expect('foreign_targets' in setup).toBe(false)
  })

  it('survives the round trip with its rate intact', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, foreignTargets: TRIBUTE, exportedAt: STAMP })
    )
    expect(setup.foreignTargets[0].crop_per_hour).toBe(25700)
    expect(setup.foreignTargets[0].name).toBe('01Arb')
  })

  it('rejects a target with no name, rather than planning an anonymous obligation', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.foreign_targets = [{ name: '  ', x: 1, y: 2, crop_per_hour: 100 }]
    expect(() => roundTrip(doc)).toThrow(SetupFileError)
  })

  it('rejects a negative rate', () => {
    // A tribute is something owed. A negative one would plan as a source.
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.foreign_targets = [{ name: 'X', x: 1, y: 2, crop_per_hour: -5 }]
    expect(() => roundTrip(doc)).toThrow(/crop_per_hour/)
  })

  it('rejects coordinates that are not numbers', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.foreign_targets = [{ name: 'X', x: 'somewhere', y: 2, crop_per_hour: 100 }]
    expect(() => roundTrip(doc)).toThrow(SetupFileError)
  })

  it('lets the file replace the targets on screen wholesale', () => {
    // Merging two tribute lists would silently double an obligation, or leave a
    // target the operator deleted still being shipped to.
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, foreignTargets: TRIBUTE, exportedAt: STAMP })
    )
    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      foreignTargets: [{ name: 'Old', x: 9, y: 9, crop_per_hour: 999 }],
    })

    // Normalised, not echoed: the parser fills the cadence fields in as "unset"
    // so a consumer never has to distinguish absent from null.
    expect(merged.foreignTargets).toEqual([
      { ...TRIBUTE[0], max_cycle_hours: null, exclude_origins: [] },
    ])
  })

  it('leaves the targets alone when the file carries none', () => {
    const setup = roundTrip(buildSetup({ villages: VILLAGES, exportedAt: STAMP }))
    const existing = [{ name: 'Keep', x: 1, y: 1, crop_per_hour: 10 }]
    const merged = mergeSetup({ setup, villages: VILLAGES, foreignTargets: existing })

    expect(merged.foreignTargets).toEqual(existing)
  })
})

describe('foreign targets keep their cadence controls', () => {
  // Found by review: the parser rebuilt each target field by field and simply
  // omitted these two, so a file carrying "47,167 an hour, hourly, not from the
  // hub" imported as "47,167 an hour" -- the same volume with the constraint
  // silently gone, which is the plan the operator was trying to avoid.
  const CADENCED = [
    {
      name: '01Arb',
      x: 46,
      y: 133,
      crop_per_hour: 47167,
      safety_margin_pct: 0,
      route_eligible: true,
      max_cycle_hours: 1,
      exclude_origins: [53629],
    },
  ]

  it('survives the round trip with the cadence and the exclusions', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, foreignTargets: CADENCED, exportedAt: STAMP })
    )

    expect(setup.foreignTargets[0].max_cycle_hours).toBe(1)
    expect(setup.foreignTargets[0].exclude_origins).toEqual([53629])
  })

  it('leaves them absent when the target has none', () => {
    // Absent must not become a value: a target with no cadence must not import
    // as one capped at some default.
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        foreignTargets: [{ name: 'X', x: 1, y: 2, crop_per_hour: 10 }],
        exportedAt: STAMP,
      })
    )

    expect(setup.foreignTargets[0].max_cycle_hours).toBeNull()
    expect(setup.foreignTargets[0].exclude_origins).toEqual([])
  })

  it('rejects a cycle Travian cannot express', () => {
    // The repeat interval is a closed set. A 5 here would plan a cadence the
    // create payload cannot carry, and the route would come back on some other
    // interval entirely.
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.foreign_targets = [{ name: 'X', x: 1, y: 2, crop_per_hour: 10, max_cycle_hours: 5 }]
    expect(() => roundTrip(doc)).toThrow(/repeat interval/)
  })

  it('rejects an exclusion list that is not village ids', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.foreign_targets = [
      { name: 'X', x: 1, y: 2, crop_per_hour: 10, exclude_origins: ['the hub'] },
    ]
    expect(() => roundTrip(doc)).toThrow(SetupFileError)
  })
})
