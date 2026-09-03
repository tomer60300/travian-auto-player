import { describe, it, expect } from 'vitest'
import {
  SETUP_FORMAT,
  SETUP_VERSION,
  SetupFileError,
  buildSetup,
  declaresConsumption,
  isConsumptionRate,
  isStockFloorFraction,
  materialSpendOnly,
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

describe('allocation values are whole units', () => {
  it('rounds a raw-computation float from a file to something an input box can hold', () => {
    // Seen live: a stored 43726.200918351606 rendered verbatim in the Value
    // spinbutton. A /h rate has no sub-unit precision worth keeping.
    const parsed = roundTrip({
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      account: null,
      villages: [],
      profiles: { Night: { crop: { 101: { mode: 'absolute', value: 43726.200918351606 } } } },
    })

    expect(parsed.profiles.Night.crop[101].value).toBe(43726)
  })
})

// ── Per-village shipping restriction and NPC-backed stock floor ────────────
// Two more pieces of owned state on the same village row as the Trade Office
// level, and with the same stakes: "ships only to" narrows where a village's
// merchants may go, and the stock floor is supply the planner draws down over
// the window. Losing either to a cleared origin silently changes the plan.

describe('ship_only_to and stock_floor_fraction in the setup file', () => {
  it('writes both onto the village row', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 13 },
      shipOnlyTo: { 20030: [20031, 20032] },
      stockFloors: { 20030: 0.3 },
      exportedAt: STAMP,
    })
    expect(setup.villages).toEqual([
      {
        village_id: 20030,
        name: 'Capital',
        trade_office_level: 13,
        ship_only_to: [20031, 20032],
        stock_floor_fraction: 0.3,
      },
    ])
  })

  it('keeps an empty list, which means "ships to nobody" and not "unrestricted"', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      shipOnlyTo: { 20031: [] },
      exportedAt: STAMP,
    })
    expect(setup.villages).toEqual([{ village_id: 20031, name: 'V05', ship_only_to: [] }])
  })

  it('carries a village that has only a restriction or only a floor typed', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      shipOnlyTo: { 20031: [20030] },
      stockFloors: { 20032: 0.5 },
      exportedAt: STAMP,
    })
    expect(setup.villages.map((v) => v.village_id)).toEqual([20031, 20032])
  })

  it('survives the round trip unchanged', () => {
    const shipOnlyTo = { 20030: [20031], 20031: [] }
    const stockFloors = { 20030: 0.3, 20032: 0.125 }
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, shipOnlyTo, stockFloors, exportedAt: STAMP })
    )
    const merged = mergeSetup({ setup, villages: VILLAGES })
    expect(merged.shipOnlyTo).toEqual(shipOnlyTo)
    expect(merged.stockFloors).toEqual(stockFloors)
  })

  it('leaves both absent when the row carries neither', () => {
    // Absent must stay absent: a row with only a Trade Office level must not
    // import as "ships to nobody" or as a floor of 0.
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 13 }, exportedAt: STAMP })
    )
    expect('ship_only_to' in setup.villages[0]).toBe(false)
    expect('stock_floor_fraction' in setup.villages[0]).toBe(false)
  })

  it('does not overwrite an existing value when the file row has none', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 13 }, exportedAt: STAMP })
    )
    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      shipOnlyTo: { 20030: [20032], 20031: [] },
      stockFloors: { 20030: 0.2 },
    })
    expect(merged.shipOnlyTo).toEqual({ 20030: [20032], 20031: [] })
    expect(merged.stockFloors).toEqual({ 20030: 0.2 })
  })

  it('lets the file win over an existing value', () => {
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        shipOnlyTo: { 20030: [20031] },
        stockFloors: { 20030: 0.4 },
        exportedAt: STAMP,
      })
    )
    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      shipOnlyTo: { 20030: [20032] },
      stockFloors: { 20030: 0.1 },
    })
    expect(merged.shipOnlyTo[20030]).toEqual([20031])
    expect(merged.stockFloors[20030]).toBe(0.4)
  })

  it('drops both for a village the account no longer has', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 99999, ship_only_to: [20030], stock_floor_fraction: 0.5 }],
    }
    const merged = mergeSetup({ setup: roundTrip(doc), villages: VILLAGES })
    expect(merged.shipOnlyTo).toEqual({})
    expect(merged.stockFloors).toEqual({})
    expect(merged.report.missingFromAccount.map((v) => v.village_id)).toEqual([99999])
  })

  it('rejects a ship_only_to that is not a list', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, ship_only_to: 20031 }],
    }
    expect(() => roundTrip(doc)).toThrow(/ship_only_to/)
  })

  it('rejects destination ids that are not village ids', () => {
    for (const bad of ['the hub', 1.5, 0, -3]) {
      const doc = {
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, ship_only_to: [20031, bad] }],
      }
      expect(() => roundTrip(doc)).toThrow(SetupFileError)
    }
  })

  it('rejects a stock floor above 0.95 instead of clamping it', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, stock_floor_fraction: 0.96 }],
    }
    expect(() => roundTrip(doc)).toThrow(/0 to 0\.95/)
  })

  it('rejects a negative, non-numeric, or over-precise stock floor', () => {
    // The input takes a whole percent or one decimal, so 0.1234 (12.34%) is a
    // value the operator could never have typed and the file is wrong.
    for (const floor of [-0.1, 'lots', 0.1234]) {
      const doc = {
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, stock_floor_fraction: floor }],
      }
      expect(() => roundTrip(doc)).toThrow(SetupFileError)
    }
  })

  it('accepts the bounds and a one-decimal percent', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [
        { village_id: 20030, stock_floor_fraction: 0 },
        { village_id: 20031, stock_floor_fraction: 0.95 },
        { village_id: 20032, stock_floor_fraction: 0.125 },
      ],
    }
    expect(roundTrip(doc).villages.map((v) => v.stock_floor_fraction)).toEqual([0, 0.95, 0.125])
  })
})

describe('isStockFloorFraction', () => {
  // Shared by the file parser and the planner's input, so the two cannot
  // disagree about what a usable floor is.
  it('accepts 0 to 0.95 on a one-decimal-percent grid', () => {
    for (const ok of [0, 0.3, 0.95, 0.125, 0.001]) expect(isStockFloorFraction(ok)).toBe(true)
  })

  it('refuses out-of-range, over-precise and non-numeric values', () => {
    for (const bad of [0.96, -0.1, 0.1234, NaN, Infinity, undefined, null, '0.3']) {
      expect(isStockFloorFraction(bad)).toBe(false)
    }
  })
})

describe('consumption_per_hour in the setup file', () => {
  it('writes what a village spends onto its row', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 13 },
      consumption: { 20030: { lumber: 14751, clay: 10222, iron: 11458 } },
      exportedAt: STAMP,
    })
    expect(setup.villages).toEqual([
      {
        village_id: 20030,
        name: 'Capital',
        trade_office_level: 13,
        consumption_per_hour: { lumber: 14751, clay: 10222, iron: 11458 },
      },
    ])
  })

  it('carries a village that has only a consumption profile typed', () => {
    // The whole point of exporting it: a Day/Night pair of consumption profiles
    // is a hundred hand-typed numbers, and a village with nothing else on it
    // must not be dropped from the file.
    const setup = buildSetup({
      villages: VILLAGES,
      consumption: { 20031: { iron: 9526 } },
      exportedAt: STAMP,
    })
    expect(setup.villages).toEqual([
      { village_id: 20031, name: 'V05', consumption_per_hour: { iron: 9526 } },
    ])
  })

  it('drops an empty profile rather than writing "spends nothing"', () => {
    // Unlike ship_only_to, an empty map is not a distinct answer: the backend
    // reads absent and {} identically, so writing {} would only make a file
    // that looks like it says something.
    const setup = buildSetup({
      villages: VILLAGES,
      consumption: { 20031: {} },
      exportedAt: STAMP,
    })
    expect(setup.villages).toEqual([])
  })

  it('survives the round trip unchanged', () => {
    const consumption = {
      20030: { lumber: 14751, iron: 8519 },
      20032: { clay: 5168, iron: 5809 },
    }
    const setup = roundTrip(buildSetup({ villages: VILLAGES, consumption, exportedAt: STAMP }))
    const merged = mergeSetup({ setup, villages: VILLAGES })
    expect(merged.consumption).toEqual(consumption)
  })

  it('leaves it absent when the row carries none', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 13 }, exportedAt: STAMP })
    )
    expect('consumption_per_hour' in setup.villages[0]).toBe(false)
  })

  it('does not overwrite an existing profile when the file row has none', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 13 }, exportedAt: STAMP })
    )
    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      consumption: { 20030: { lumber: 8372 } },
    })
    expect(merged.consumption).toEqual({ 20030: { lumber: 8372 } })
  })

  it('lets the file win over an existing profile, wholesale', () => {
    // Wholesale for the same reason a named allocation profile is: half of an
    // old spend merged into a new one is a figure nobody entered.
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        consumption: { 20030: { lumber: 14751 } },
        exportedAt: STAMP,
      })
    )
    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      consumption: { 20030: { lumber: 100, iron: 200 } },
    })
    expect(merged.consumption[20030]).toEqual({ lumber: 14751 })
  })

  it('drops it for a village the account no longer has', () => {
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        consumption: { 20030: { lumber: 1 }, 20031: { iron: 2 } },
        exportedAt: STAMP,
      })
    )
    const merged = mergeSetup({ setup, villages: [VILLAGES[0]] })
    expect(merged.consumption).toEqual({ 20030: { lumber: 1 } })
    expect(merged.report.missingFromAccount.map((v) => v.village_id)).toEqual([20031])
  })

  it('rejects a consumption that is not a resource map', () => {
    for (const bad of [5, 'lots', [8372]]) {
      expect(() =>
        roundTrip({
          format: SETUP_FORMAT,
          version: SETUP_VERSION,
          villages: [{ village_id: 20030, consumption_per_hour: bad }],
        })
      ).toThrow(SetupFileError)
    }
  })

  it('rejects a resource the backend does not have', () => {
    expect(() =>
      roundTrip({
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, consumption_per_hour: { gold: 500 } }],
      })
    ).toThrow(/gold/)
  })

  it('refuses a crop spend, because the backend refuses it too', () => {
    // The snapshot's `crop_per_hour` is already NET of upkeep, so a declared
    // crop spend subtracts the same troops twice. The file format must not
    // accept what the backend 422s, or an import succeeds and every plan from
    // it fails.
    for (const bad of [{ crop: 8519 }, { crop: 0 }, { lumber: 100, crop: 8519 }]) {
      expect(() =>
        roundTrip({
          format: SETUP_FORMAT,
          version: SETUP_VERSION,
          villages: [{ village_id: 20030, consumption_per_hour: bad }],
        })
      ).toThrow(SetupFileError)
    }
  })

  it('tells the operator why crop is refused and what to do instead', () => {
    // "unknown resource crop" would be a lie -- crop is a resource the planner
    // knows well. The message has to name the target as the answer.
    expect(() =>
      roundTrip({
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, consumption_per_hour: { crop: 8519 } }],
      })
    ).toThrow(/already net/)
    expect(() =>
      roundTrip({
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, consumption_per_hour: { crop: 8519 } }],
      })
    ).toThrow(/target/)
  })

  it('does not export a crop spend an older build left in local state', () => {
    // The input no longer offers crop, so a figure saved by an earlier build
    // can be neither seen nor cleared -- and writing it would produce a file
    // this same parser refuses to read back.
    const setup = buildSetup({
      villages: VILLAGES,
      consumption: { 20030: { lumber: 100, crop: 200 } },
      exportedAt: STAMP,
    })
    expect(setup.villages[0].consumption_per_hour).toEqual({ lumber: 100 })
  })

  it('drops a row whose only declared spend was crop', () => {
    // Not written as `{}`: an empty profile is the same as no profile, and a
    // row carrying nothing else has no reason to be in the file at all.
    const setup = buildSetup({
      villages: VILLAGES,
      consumption: { 20030: { crop: 200 } },
      exportedAt: STAMP,
    })
    expect(setup.villages).toEqual([])
  })

  it('keeps only the materials of a stale spend map', () => {
    expect(materialSpendOnly({ lumber: 1, clay: 2, iron: 3, crop: 4 })).toEqual({
      lumber: 1,
      clay: 2,
      iron: 3,
    })
    expect(materialSpendOnly({ crop: 4 })).toBeNull()
    expect(materialSpendOnly({})).toBeNull()
    expect(materialSpendOnly(undefined)).toBeNull()
  })

  it('rejects a negative spend, which the backend refuses too', () => {
    // Not read as extra production: the statistics page reports materials
    // gross, so a consuming material village reads positive and there is no
    // sign to invert. A file that says -500 is wrong, not clampable.
    expect(() =>
      roundTrip({
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, consumption_per_hour: { lumber: -500 } }],
      })
    ).toThrow(/-500/)
  })

  it('rejects a non-numeric spend', () => {
    expect(() =>
      roundTrip({
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, consumption_per_hour: { lumber: 'some' } }],
      })
    ).toThrow(SetupFileError)
  })

  it('rejects a malformed spend instead of coercing it into a claim', () => {
    // `Number(null)` is 0, `Number([1])` is 1 and `Number('')` is 0, so
    // coercing before validating turned three malformed files into confident
    // declarations -- and a declared 0 is not silence, it is the claim
    // "measured, and it spends none". Each of these must reach the operator as
    // a file error, not as a number the file never contained.
    for (const bad of [null, [1], '']) {
      expect(() =>
        roundTrip({
          format: SETUP_FORMAT,
          version: SETUP_VERSION,
          villages: [{ village_id: 20030, consumption_per_hour: { lumber: bad } }],
        })
      ).toThrow(SetupFileError)
    }
  })

  it('says which thing is wrong: a type is not a sign', () => {
    // One message for two faults told the operator that `"some"` "cannot be
    // negative", which is both untrue and unactionable.
    expect(() =>
      roundTrip({
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, consumption_per_hour: { lumber: 'some' } }],
      })
    ).toThrow(/is not a number/)
    expect(() =>
      roundTrip({
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20030, consumption_per_hour: { lumber: -500 } }],
      })
    ).toThrow(/cannot be negative/)
  })

  it('rounds a raw-computation float to something an input box can hold', () => {
    // The same rule the allocation values follow: a /h rate has no sub-unit
    // precision, and 14750.600918 lands verbatim in the operator's input.
    const setup = roundTrip({
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, consumption_per_hour: { lumber: 43726.200918351606 } }],
    })
    expect(setup.villages[0].consumption_per_hour).toEqual({ lumber: 43726 })
  })

  it('keeps a declared zero, which says "measured, and it spends nothing"', () => {
    const setup = roundTrip({
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, consumption_per_hour: { lumber: 0 } }],
    })
    expect(setup.villages[0].consumption_per_hour).toEqual({ lumber: 0 })
  })
})

describe('isConsumptionRate', () => {
  it('accepts zero and any positive rate', () => {
    expect(isConsumptionRate(0)).toBe(true)
    expect(isConsumptionRate(14751)).toBe(true)
  })

  it('refuses a negative, a non-number and an infinity', () => {
    expect(isConsumptionRate(-1)).toBe(false)
    expect(isConsumptionRate('14751')).toBe(false)
    expect(isConsumptionRate(Number.POSITIVE_INFINITY)).toBe(false)
    expect(isConsumptionRate(Number.NaN)).toBe(false)
  })
})

describe('declaresConsumption', () => {
  it('is false for nothing, and for a map with every box cleared', () => {
    // The rule the file, the request and the input all have to share: the
    // backend reads absent and {} identically, so a village mid-edit with
    // every field emptied must not send a declaration.
    expect(declaresConsumption(undefined)).toBe(false)
    expect(declaresConsumption(null)).toBe(false)
    expect(declaresConsumption({})).toBe(false)
  })

  it('is true for a single resource, including a declared zero', () => {
    expect(declaresConsumption({ iron: 9526 })).toBe(true)
    expect(declaresConsumption({ iron: 0 })).toBe(true)
  })
})
