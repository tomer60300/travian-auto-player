import { describe, it, expect } from 'vitest'
import {
  DEFAULT_BASELINE_FILL,
  DEFAULT_MERCHANT_MODEL,
  DEFAULT_TARGET_FILL,
  MAX_DAY_SEGMENTS,
  MAX_GAME_ROWS_PER_RUN,
  MAX_MERCHANTS_PER_VILLAGE,
  MAX_STOCK_FLOOR_FRACTION,
  MAX_TRADE_OFFICE_LEVEL,
  TRAVIAN_REPEAT_INTERVALS,
  SETUP_FORMAT,
  SETUP_VERSION,
  SetupFileError,
  KEEP_ALLOCATION,
  allocationsForRequest,
  buildSetup,
  declaresConsumption,
  describeConsumption,
  describeRelayPermission,
  merchantModelIsCalibrated,
  describeSpendSource,
  isConsumptionRate,
  isEmptyTemplate,
  isMaxBusyMerchants,
  isMerchantBaseCapacity,
  isStockFloorFraction,
  materialSpendOnly,
  mergeSetup,
  parseSetup,
  relayFlagsOnly,
  resolveRoleAllocation,
  resolveRoleSpend,
  resolvedSpend,
  roleDeviates,
  roleInherits,
  rolesForRequest,
  setupFilename,
  setupMatchesAccount,
  stripStoredCropSpends,
  stripUnknownRoles,
  unreachableCaps,
} from './plannerSetup'
import { withEditedAllocation } from './plannerAllocation'

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
    // 11 since the measured merchant capacity landed (10 was the window prune, 9
    // the reserved NPC-burst window, 8 the overnight declaration, 7 the
    // per-profile NPC attendance, 6 profile section 5's declared relay tier, 5
    // the per-village merchant cap, 4 `may_relay`, 3 the role templates).
    // Pinned to a literal on purpose: the version has to rise
    // whenever a field is added, so that an older build refuses a file it would
    // otherwise half-load, and a literal is what makes forgetting the bump a
    // failing test rather than a tautology.
    expect(setup.version).toBe(11)
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
    expect(() => roundTrip(doc)).toThrow(/whole number of units/)
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

  it('carries the per-profile NPC attendance through, including false', () => {
    // The field that decides whether a night route set is funded at all, and
    // the setup document had no place for it -- so saving and reloading lost
    // it silently, on the one answer this codebase refuses to guess. False is
    // the case that matters: it is an ANSWER ("nobody is trading"), and any
    // carrier that treats it as absence loses exactly the profile whose
    // conversion allowance has to be zeroed.
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        profiles: { Night: NIGHT },
        profileWindows: { Night: ['23:00', '07:00'] },
        npcAttended: { Day: true, Night: false },
        exportedAt: STAMP,
      })
    )
    expect(setup.npcAttended).toEqual({ Day: true, Night: false })

    const merged = mergeSetup({ setup, villages: VILLAGES, npcAttended: {} })
    expect(merged.npcAttended).toEqual({ Day: true, Night: false })
  })

  it('omits the attendance field entirely when no profile has answered', () => {
    // Absent is the third state, and it has to survive as absence: an empty
    // map written into the file would import as "every profile answered
    // nothing", which reads the same on screen and is a different document.
    const setup = buildSetup({ villages: VILLAGES, npcAttended: {}, exportedAt: STAMP })
    expect('npc_attended' in setup).toBe(false)
  })

  it('leaves a profile the file is silent about exactly as it was', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, npcAttended: { Night: false }, exportedAt: STAMP })
    )
    const merged = mergeSetup({ setup, villages: VILLAGES, npcAttended: { Day: true } })
    expect(merged.npcAttended).toEqual({ Day: true, Night: false })
  })

  it('rejects an attendance that is not a boolean rather than guessing one', () => {
    // `attendanceMapOnly` drops a non-boolean silently on the way out of
    // localStorage, because an unanswered profile is already named on screen.
    // A FILE is different: it is the operator asserting an answer, and "yes"
    // read as an attendance nobody declared is the one outcome worth stopping
    // for -- the backend's lax `bool` would accept it.
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.npc_attended = { Night: 'yes' }
    expect(() => roundTrip(doc)).toThrow(/not an answer/)
  })

  it('carries the per-profile overnight declaration through, including false', () => {
    // Section 6's rules are the overnight profile's, and until now the ONLY
    // way the backend could know which profile that was came off the clock:
    // a window that wraps past midnight. That is wrong for a night SPLIT at
    // midnight -- 00:00-07:00 wraps in neither direction -- so the operator's
    // own declaration has to travel, and false has to travel with it: a
    // near-24h day profile (`[420, 419]`) wraps and is not the night.
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        profiles: { Night: NIGHT },
        profileWindows: { 'Night early': ['00:00', '07:00'] },
        overnight: { 'Night early': true, Day: false },
        exportedAt: STAMP,
      })
    )
    expect(setup.overnight).toEqual({ 'Night early': true, Day: false })

    const merged = mergeSetup({ setup, villages: VILLAGES, overnight: {} })
    expect(merged.overnight).toEqual({ 'Night early': true, Day: false })
  })

  it('omits the overnight field entirely when no profile has declared', () => {
    // Absent is the third state here too, and it is the state that asks the
    // backend to DERIVE. An empty map would import as every profile having
    // declared nothing, which reads identically on screen but is a different
    // document.
    const setup = buildSetup({ villages: VILLAGES, overnight: {}, exportedAt: STAMP })
    expect('overnight' in setup).toBe(false)
  })

  it('leaves a profile the file says nothing about overnight for alone', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, overnight: { Night: true }, exportedAt: STAMP })
    )
    const merged = mergeSetup({ setup, villages: VILLAGES, overnight: { Day: false } })
    expect(merged.overnight).toEqual({ Day: false, Night: true })
  })

  it('rejects an overnight that is not a boolean rather than guessing one', () => {
    const doc = buildSetup({ villages: VILLAGES, exportedAt: STAMP })
    doc.overnight = { Night: 'yes' }
    expect(() => roundTrip(doc)).toThrow(/not a declaration/)
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

// A TYPED exclusion is the operator excluding a hub from a tribute, and it lived
// only as `exclude_origins_text` -- the half of the pair the input box holds so
// it can carry a half-finished name. `buildSetup` wrote the page's targets
// verbatim, so the document carried the text and no ids; `parseForeignTargets`
// rebuilds field by field and reads only `exclude_origins`, so it came back
// empty. The operator excluded the hub, saved, reloaded, and the hub supplied
// the tribute again with nothing on screen saying the answer had gone.
//
// The same denylist `94892ed` fixed on the backend ("the origin exclusion only
// ever bound the greedy seed"), failing from the other end.
describe('a typed exclusion survives the document round trip', () => {
  const TYPED = [
    {
      name: '01Arb',
      x: 46,
      y: 133,
      crop_per_hour: 47167,
      safety_margin_pct: 0,
      route_eligible: true,
      exclude_origins: [],
      exclude_origins_text: 'V05, V16',
    },
  ]

  it('resolves the typed names to ids on the way into the document', () => {
    const setup = buildSetup({ villages: VILLAGES, foreignTargets: TYPED, exportedAt: STAMP })

    expect(setup.foreign_targets[0].exclude_origins).toEqual([20031, 20032])
    // The text is a draft the input box owns, not an answer: the document
    // carries what was RESOLVED, which is what the plan request carries too.
    expect('exclude_origins_text' in setup.foreign_targets[0]).toBe(false)
  })

  it('comes back out of the parser as the same two villages', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, foreignTargets: TYPED, exportedAt: STAMP })
    )

    expect(setup.foreignTargets[0].exclude_origins).toEqual([20031, 20032])
  })

  it('keeps a stored exclusion the operator never retyped', () => {
    // `excludedOriginIds`' own rule: the text wins where it EXISTS, even empty,
    // because clearing the box is deliberate — but a target loaded from a file
    // has ids and no text at all, and those must not be dropped.
    const stored = [{ ...TYPED[0], exclude_origins: [20031], exclude_origins_text: undefined }]
    delete stored[0].exclude_origins_text
    const setup = buildSetup({ villages: VILLAGES, foreignTargets: stored, exportedAt: STAMP })

    expect(setup.foreign_targets[0].exclude_origins).toEqual([20031])
  })

  it('drops a cleared box rather than falling back to what the file said', () => {
    const cleared = [{ ...TYPED[0], exclude_origins: [20031], exclude_origins_text: '' }]
    const setup = buildSetup({ villages: VILLAGES, foreignTargets: cleared, exportedAt: STAMP })

    expect('exclude_origins' in setup.foreign_targets[0]).toBe(false)
  })

  it('leaves an unresolvable name out rather than inventing an id for it', () => {
    // The cell already marks this and `planBlockers` already refuses the plan
    // over it. What must not happen is a document claiming an exclusion that
    // resolves to nothing.
    const typo = [{ ...TYPED[0], exclude_origins_text: 'V05, no-such-village' }]
    const setup = buildSetup({ villages: VILLAGES, foreignTargets: typo, exportedAt: STAMP })

    expect(setup.foreign_targets[0].exclude_origins).toEqual([20031])
  })
})

// `+ Add target` seeds a row with no name, no rate and 0|0 for coordinates, and
// the operator fills it in from there. The REQUEST has always dropped such a row
// -- `usableForeignTargets` filters on `foreignTargetIsDraft`, and the badge in
// the table reads off the same predicate -- while the document wrote it raw. So
// pressing "+ Add target" made the whole setup unsaveable until the row was
// finished or deleted: the PUT 422'd on `name` and `crop_per_hour`, and the
// export wrote a file `parseForeignTargets` refuses ("has no name").
//
// A draft is the operator mid-edit, not in error, so it is left out rather than
// refused -- which is what every other half-typed cell in this document does.
// The window prune decides whether `/execute` DELETES game rows: Travian fans
// "repeat every N hours" into 24/N individually deletable rows, and the prune is
// what removes the ones departing outside the profile. It lived as
// `useState(true)` alone -- carried by NEITHER persistence path, which is the
// exact criterion that earned `reserved_window` its v9 bump -- so an operator
// who turned it off saw it back ON after a reload, and the next run left every
// out-of-window firing in place.
describe('the window prune travels in the setup (v10)', () => {
  it('carries a deliberate OFF, which is the answer that changes a run', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      pruneToWindow: false,
      exportedAt: STAMP,
    })

    expect(setup.prune_to_window).toBe(false)
    expect(roundTrip(setup).pruneToWindow).toBe(false)
  })

  it('carries an explicit ON too, because both are answers', () => {
    // Unlike every other optional field here, `false` is not "absent". The
    // resting state of this switch is ON, so a document that omits it must not
    // be read as OFF -- and one that says ON must survive the trip.
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      pruneToWindow: true,
      exportedAt: STAMP,
    })

    expect(setup.prune_to_window).toBe(true)
    expect(roundTrip(setup).pruneToWindow).toBe(true)
  })

  it('reads a v9 document as saying nothing about it', () => {
    // A build that never wrote the field is not a build that turned it off.
    const doc = {
      format: SETUP_FORMAT,
      version: 9,
      villages: [],
    }

    expect(roundTrip(doc).pruneToWindow).toBeNull()
  })

  it('refuses anything that is not a boolean, rather than coercing it', () => {
    // The same discipline `npc_attended` and `overnight` follow, and for a
    // consequence of the same size: the backend's `bool` is lax enough to read
    // `"no"` as TRUE, so a hand-edited document could switch the prune back on
    // and delete rows over a string nobody can see.
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.prune_to_window = 'no'

    expect(() => roundTrip(doc)).toThrow(SetupFileError)
    expect(() => roundTrip(doc)).toThrow(/prune_to_window/)
  })

  it('lets the document win where it has one, and says nothing where it does not', () => {
    const carried = roundTrip(
      buildSetup({
        villages: VILLAGES,
        tradeOffice: { 20030: 1 },
        pruneToWindow: false,
        exportedAt: STAMP,
      })
    )
    expect(mergeSetup({ setup: carried, villages: VILLAGES, pruneToWindow: true }).pruneToWindow)
      .toBe(false)

    // A v9 document knows nothing about the switch, so loading one must not
    // move it -- the same rule the reserved window follows.
    const silent = roundTrip({ format: SETUP_FORMAT, version: 9, villages: [] })
    expect(mergeSetup({ setup: silent, villages: VILLAGES, pruneToWindow: false }).pruneToWindow)
      .toBe(false)
  })
})

// The acknowledgement that the two CAPACITY figures were read off the game, and the
// one thing in this document that records work done IN THE GAME that the game
// does not record. MERCHANT_MODEL_UNCALIBRATED fires whenever
// `trade_office_bonus_per_level` still equals the shipped 0.20 and any village
// has a Trade Office -- a test a MEASURED 0.20 also satisfies, so an operator
// who read a Marketplace capacity at two levels, found the default right and
// typed it back got the same warning for ever. Nothing here could re-derive it,
// and dropped it brings the finding back on every plan.
describe('the measured merchant capacity travels in the setup (v11)', () => {
  it('carries the acknowledgement', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      merchantCapacityMeasured: true,
      exportedAt: STAMP,
    })

    // The literal, pinned against `SetupDocument.merchant_capacity_measured`
    // in `planner_setup.py` and `PlanRequest.merchant_capacity_measured` in
    // `distribution.py` -- both were `merchant_model_measured` until b89e0f4.
    // The server IGNORES an unknown key on the document and lifts the absent
    // field with `bool(None)`, so the old name would have gone on saving,
    // loading and planning while the acknowledgement silently did nothing.
    expect(setup.merchant_capacity_measured).toBe(true)
    expect(roundTrip(setup).merchantCapacityMeasured).toBe(true)
  })

  it('leaves it out when nobody has said so, because absent IS unmeasured', () => {
    // The opposite of `prune_to_window` beside it, and for the opposite reason:
    // that switch rests ON, so its `false` is an answer worth carrying. This one
    // rests OFF -- the finding's own default -- so a written `false` would say
    // nothing an absent field does not, and the backend lifts absent with
    // `bool(None)`.
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      merchantCapacityMeasured: false,
      exportedAt: STAMP,
    })

    expect('merchant_capacity_measured' in setup).toBe(false)
    expect(roundTrip(setup).merchantCapacityMeasured).toBeNull()
  })

  it('reads a v10 document as saying nothing about it', () => {
    // A build that never wrote the field is not an operator who declined to
    // measure -- and the two plan the same, which is precisely why the box has
    // to be UNTICKED rather than absent-and-forgotten when one loads.
    const doc = { format: SETUP_FORMAT, version: 10, villages: [] }

    expect(roundTrip(doc).merchantCapacityMeasured).toBeNull()
  })

  it('refuses anything that is not a boolean, rather than coercing it', () => {
    // `StrictBool` on the server, and the same discipline every other owned
    // answer here follows: pydantic's lax `bool` reads "yes" as True, and a
    // value nobody typed as a boolean must not silence a finding about the
    // figure that sizes every cargo.
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.merchant_capacity_measured = 'yes'

    expect(() => roundTrip(doc)).toThrow(SetupFileError)
    expect(() => roundTrip(doc)).toThrow(/merchant_capacity_measured/)
  })

  it('lets the document win where it has one, and says nothing where it does not', () => {
    const carried = roundTrip(
      buildSetup({
        villages: VILLAGES,
        tradeOffice: { 20030: 1 },
        merchantCapacityMeasured: true,
        exportedAt: STAMP,
      })
    )
    expect(
      mergeSetup({ setup: carried, villages: VILLAGES, merchantCapacityMeasured: false })
        .merchantCapacityMeasured
    ).toBe(true)

    // A v10 document knows nothing about it, so loading one must not clear an
    // acknowledgement the operator has on screen.
    const silentDoc = roundTrip({ format: SETUP_FORMAT, version: 10, villages: [] })
    expect(
      mergeSetup({ setup: silentDoc, villages: VILLAGES, merchantCapacityMeasured: true })
        .merchantCapacityMeasured
    ).toBe(true)
    // And with nothing on either side it is unmeasured, never null: the page
    // renders a checkbox, and a checkbox has two states.
    expect(mergeSetup({ setup: silentDoc, villages: VILLAGES }).merchantCapacityMeasured).toBe(false)
  })
})

describe('a half-typed foreign target does not block the document', () => {
  const REAL = {
    name: '01Arb',
    x: 46,
    y: 133,
    crop_per_hour: 25700,
    safety_margin_pct: 0,
    route_eligible: true,
  }
  const DRAFT = { name: '', x: 0, y: 0, crop_per_hour: '', safety_margin_pct: 5, route_eligible: false }

  it('writes the finished target and leaves the draft out', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      foreignTargets: [REAL, DRAFT],
      exportedAt: STAMP,
    })

    expect(setup.foreign_targets).toEqual([REAL])
    expect(() => roundTrip(setup)).not.toThrow()
  })

  it('omits the field entirely when every row is a draft', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 5 },
      foreignTargets: [DRAFT],
      exportedAt: STAMP,
    })

    expect('foreign_targets' in setup).toBe(false)
  })

  it('drops a cadence the operator selected and then cleared', () => {
    // The select writes `e.target.value`, so going back to "any" stores `''`.
    // `Number('')` is 0, which is not one of Travian's repeat intervals, so the
    // parser refused the file and the backend refused the save over a control
    // that reads "any" on screen.
    const cleared = [{ ...REAL, max_cycle_hours: '' }]
    const setup = buildSetup({ villages: VILLAGES, foreignTargets: cleared, exportedAt: STAMP })

    expect('max_cycle_hours' in setup.foreign_targets[0]).toBe(false)
    expect(roundTrip(setup).foreignTargets[0].max_cycle_hours).toBeNull()
  })

  it('keeps a cadence that was actually chosen, as a number', () => {
    const chosen = [{ ...REAL, max_cycle_hours: '8' }]
    const setup = buildSetup({ villages: VILLAGES, foreignTargets: chosen, exportedAt: STAMP })

    expect(setup.foreign_targets[0].max_cycle_hours).toBe(8)
  })
})

// A profile window is two `HH:MM` boxes, so there is a moment where only one is
// typed. The REQUEST has always handled it -- `dispatchWindowFor` collapses the
// unusable shapes to null and `buildSegments` skips the profile -- while the
// document wrote the pair raw, and `["07:00", ""]` is refused by this file's own
// `parseClockPair` and by the server's `_ClockTime`. "Save setup to server"
// therefore failed for as long as one box was mid-edit.
describe('a half-typed profile window does not block the document', () => {
  it('writes the finished windows and leaves the half-typed one out', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      profileWindows: { Day: ['07:00', '23:00'], Night: ['23:00', ''] },
      exportedAt: STAMP,
    })

    expect(setup.profile_windows).toEqual({ Day: ['07:00', '23:00'] })
    expect(() => roundTrip(setup)).not.toThrow()
  })

  it('omits the map entirely when no profile has a usable pair', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 5 },
      profileWindows: { Day: ['', ''] },
      exportedAt: STAMP,
    })

    expect('profile_windows' in setup).toBe(false)
  })

  // NOT `dispatchWindowFor`, deliberately. That helper also collapses a
  // ZERO-WIDTH pair, because a request has nothing to do with one -- but a
  // document is what the operator typed, both boxes are filled, and the parser
  // and the server both accept it. Dropping it would lose typed state.
  it('keeps a zero-width pair, which is typed rather than half-typed', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      profileWindows: { Day: ['07:00', '07:00'] },
      exportedAt: STAMP,
    })

    expect(setup.profile_windows).toEqual({ Day: ['07:00', '07:00'] })
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

describe('roles and role templates in the setup file', () => {
  // Section 1 assigns a role to every village and section 2 gives ONE profile
  // for FOUR defensive villages, so a role template is the largest saving in
  // hand-typed state this file has ever carried: five profiles instead of
  // twenty-six allocations plus six spend maps. It is also the state that hurts
  // most to lose -- a cleared origin takes the roles with it, and the plan then
  // reads four defensive villages as keeping their own production, which is a
  // tenth of what they need, without saying anything is wrong.
  const TEMPLATES = {
    def: {
      allocations: {
        lumber: { mode: 'absolute', value: 8372 },
        clay: { mode: 'absolute', value: 5168 },
        iron: { mode: 'absolute', value: 5809 },
        crop: { mode: 'absolute', value: 2200 },
      },
      consumption: { lumber: 8372, clay: 5168, iron: 5809 },
      may_relay: null,
      crop_negative_by_design: false,
      assumed_crop_per_hour: null,
    },
    full_off: {
      allocations: { crop: { mode: 'absolute', value: 8519 } },
      consumption: { lumber: 14751 },
      may_relay: null,
      crop_negative_by_design: true,
      assumed_crop_per_hour: null,
    },
  }

  it('writes the per-village role and the templates', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      villageRoles: { 20031: 'def', 20032: 'feeder' },
      roles: TEMPLATES,
      exportedAt: STAMP,
    })

    expect(setup.villages).toEqual([
      { village_id: 20031, name: 'V05', role: 'def' },
      { village_id: 20032, name: 'V16', role: 'feeder' },
    ])
    expect(setup.roles).toEqual(TEMPLATES)
  })

  it('omits the roles field entirely when there are none', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 5 },
      exportedAt: STAMP,
    })

    expect('roles' in setup).toBe(false)
    expect(setup.villages[0].role).toBeUndefined()
  })

  it('survives the round trip unchanged', () => {
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        villageRoles: { 20030: 'capital', 20031: 'def', 20032: 'def' },
        roles: TEMPLATES,
        exportedAt: STAMP,
      })
    )
    const merged = mergeSetup({ setup, villages: VILLAGES })

    expect(merged.villageRoles).toEqual({ 20030: 'capital', 20031: 'def', 20032: 'def' })
    expect(merged.roles).toEqual(TEMPLATES)
  })

  it('is the current version, and every older file simply carries less', () => {
    // Rewritten twice now -- from "is a version 3 file" when the per-village
    // `may_relay` landed, and again for the merchant cap. The contract is the
    // RULE, not the number: the version rises whenever a field is added, so
    // that a build which cannot read the new one refuses a file it would
    // otherwise half-load rather than dropping the field and planning a
    // different account in silence. The number itself is pinned in the merchant
    // cap's own suite, which is where the field that last moved it lives.
    const older = {
      format: SETUP_FORMAT,
      version: 2,
      villages: [{ village_id: 20030, trade_office_level: 3 }],
    }
    const parsed = roundTrip(older)
    expect(parsed.roles).toEqual({})
    expect(parsed.villages[0].role).toBeUndefined()
    expect(parsed.villages[0].may_relay).toBeUndefined()
  })

  it('rejects a role name the backend does not have', () => {
    // "hammer" is what the operator calls village 01 in conversation and is not
    // one of the five. Dropped rather than refused, it would be a village with
    // no profile at all -- and the backend answers an unknown role with a 422,
    // so accepting it here would only move the failure somewhere less
    // explicable.
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, role: 'hammer' }],
    }

    expect(() => roundTrip(doc)).toThrow(/hammer/)
    expect(() => roundTrip(doc)).toThrow(SetupFileError)
  })

  it('rejects a template that is not an object at all', () => {
    // Every other branch of `parseRoleTemplate` has a test; this one did not,
    // and it is the branch a hand-edited file hits first. `null` and a bare
    // number would both read `raw.allocations` as undefined and import as an
    // empty template -- a role with a profile of nothing, which plans its
    // villages as keeping their own production while the file looked loaded.
    for (const bad of [null, 42, 'def', ['def'], true]) {
      const doc = {
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [],
        roles: { def: bad },
      }
      expect(() => roundTrip(doc), JSON.stringify(bad)).toThrow(SetupFileError)
      expect(() => roundTrip(doc), JSON.stringify(bad)).toThrow(/is not a role template/)
      // Named by its key, so the operator knows which of five to look at.
      expect(() => roundTrip(doc), JSON.stringify(bad)).toThrow(/def/)
    }
  })

  it('rejects a template keyed by a role name that is not one of the five', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { hammer: { allocations: {} } },
    }

    expect(() => roundTrip(doc)).toThrow(/hammer/)
  })

  it('rejects a crop spend in a template, and says where the figure belongs', () => {
    // The backend's 422, mirrored: the snapshot's crop rate is already net of
    // upkeep, so a declared crop spend subtracts the same troops twice. Section
    // 2 lists a crop figure per role village, which is exactly why this is the
    // field an operator reaches for -- so the message has to name the
    // alternative rather than only refuse.
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { def: { consumption: { crop: 2200 } } },
    }

    expect(() => roundTrip(doc)).toThrow(SetupFileError)
    expect(() => roundTrip(doc)).toThrow(/already net of troop upkeep/)
    expect(() => roundTrip(doc)).toThrow(/crop allocation target/)
  })

  // The village-level twin of this has been stripped since `materialSpendOnly`:
  // the input no longer offers crop, so a figure an older build stored can be
  // neither seen nor cleared. A ROLE TEMPLATE's was stripped for the REQUEST
  // (`rolesForRequest`) and written to the document raw, so the plan ran, the
  // save came back 422 on `roles.def.consumption`, and the export wrote a file
  // this same parser refuses -- over a figure with no box to clear it from.
  it('strips a stored crop spend out of a template on the way into the document', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      roles: { def: { allocations: {}, consumption: { lumber: 8372, crop: 2200 } } },
      exportedAt: STAMP,
    })

    expect(setup.roles.def.consumption).toEqual({ lumber: 8372 })
    expect(() => roundTrip(setup)).not.toThrow()
  })

  it('leaves a template whose only spend was crop declaring nothing', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      roles: { def: { allocations: { lumber: { mode: 'keep', value: 0 } }, consumption: { crop: 2200 } } },
      exportedAt: STAMP,
    })

    // The template itself survives -- its allocations are the reason it exists.
    expect(setup.roles.def.allocations).toEqual({ lumber: { mode: 'keep', value: 0 } })
    expect(setup.roles.def.consumption).toEqual({})
    expect(() => roundTrip(setup)).not.toThrow()
  })

  it('leaves a template that declares no spend at all untouched', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      roles: { feeder: { allocations: {}, may_relay: true } },
      exportedAt: STAMP,
    })

    // Absent stays absent: writing `consumption: {}` in would make a template
    // that says nothing look like one that declares a spend of nothing.
    expect('consumption' in setup.roles.feeder).toBe(false)
  })

  it('rejects a negative spend in a template', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { def: { consumption: { lumber: -500 } } },
    }

    expect(() => roundTrip(doc)).toThrow(/cannot be negative/)
  })

  it('rejects an allocation mode a template cannot have', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { def: { allocations: { lumber: { mode: 'hoard', value: 1 } } } },
    }

    expect(() => roundTrip(doc)).toThrow(/hoard/)
  })

  it('rejects remainder in a template, and points at the Rest radio', () => {
    // Ruling #4: exactly one village per resource absorbs the slack, so a
    // profile shared by four defensive villages cannot say which. Left to the
    // backend it fanned out to every village of the role and the plan came
    // back as a 400 naming VILLAGES ("got 02, 11, 13, 17, 19") for one mistyped
    // template -- five bad cells to work back from.
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { def: { allocations: { lumber: { mode: 'remainder', value: 0 } } } },
    }

    expect(() => roundTrip(doc)).toThrow(SetupFileError)
    expect(() => roundTrip(doc)).toThrow(/remainder/)
    expect(() => roundTrip(doc)).toThrow(/per village/)
    expect(() => roundTrip(doc)).toThrow(/Rest/)
    // And the refusal must not enumerate remainder as a valid correction, the
    // way the unknown-resource message once offered crop.
    expect(() => roundTrip(doc)).toThrow(/keep, absolute, percentage, sustain/)
  })

  it('still accepts remainder on a per-village allocation', () => {
    // The template is the only place it is refused; a profile's own map is
    // where the slack destination is actually recorded.
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      profiles: { Day: { lumber: { 20030: { mode: 'remainder', value: 0 } } } },
    }

    expect(roundTrip(doc).profiles.Day.lumber['20030']).toEqual({
      mode: 'remainder',
      value: 0,
    })
  })

  it('rejects an unknown resource in a template allocation', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { def: { allocations: { gold: { mode: 'absolute', value: 1 } } } },
    }

    expect(() => roundTrip(doc)).toThrow(/gold/)
  })

  it('rounds a template allocation to whole units, as a per-village one is', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { def: { allocations: { lumber: { mode: 'absolute', value: 8371.6009 } } } },
    }

    expect(roundTrip(doc).roles.def.allocations.lumber).toEqual({
      mode: 'absolute',
      value: 8372,
    })
  })

  it('defaults the optional halves of a template rather than leaving them undefined', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { feeder: {} },
    }

    expect(roundTrip(doc).roles.feeder).toEqual({
      allocations: {},
      consumption: {},
      may_relay: null,
      crop_negative_by_design: false,
      // Explicitly null, not absent: no assumption is not an assumption of
      // zero, and reading a missing figure as 0/h would drift-flag every
      // village on every account that has never typed one.
      assumed_crop_per_hour: null,
    })
  })

  it('keeps a may_relay override of false, which is not the same as unset', () => {
    // Unset means "take the role's own answer" and is what almost every
    // template says; an explicit false on a FEEDER is the one account that
    // wants its feeders left out of the relay tier, and coercing the two
    // together would lose that.
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { feeder: { may_relay: false }, def: { may_relay: true } },
    }
    const parsed = roundTrip(doc)

    expect(parsed.roles.feeder.may_relay).toBe(false)
    expect(parsed.roles.def.may_relay).toBe(true)
  })

  it('rejects a may_relay that is not a boolean or unset', () => {
    for (const bad of ['yes', 1, 0]) {
      const doc = {
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [],
        roles: { def: { may_relay: bad } },
      }
      expect(() => roundTrip(doc)).toThrow(SetupFileError)
    }
  })
})

describe('mergeSetup with roles', () => {
  it('replaces a role the file names and leaves the others alone', () => {
    // The same rule the profiles follow, for the same reason: half of an old
    // defensive profile merged into a new one is a distribution nobody
    // designed.
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { def: { consumption: { lumber: 8372 } } },
    }
    const merged = mergeSetup({
      setup: roundTrip(doc),
      villages: VILLAGES,
      roles: {
        def: { consumption: { lumber: 1, clay: 2 }, crop_negative_by_design: true },
        feeder: { allocations: { lumber: { mode: 'absolute', value: 0 } } },
      },
    })

    expect(merged.roles.def).toEqual({
      allocations: {},
      consumption: { lumber: 8372 },
      may_relay: null,
      crop_negative_by_design: false,
      assumed_crop_per_hour: null,
    })
    expect(merged.roles.feeder).toEqual({
      allocations: { lumber: { mode: 'absolute', value: 0 } },
    })
  })

  it('drops a role for a village the account no longer has', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 99999, role: 'def' }],
    }
    const merged = mergeSetup({ setup: roundTrip(doc), villages: VILLAGES })

    expect(merged.villageRoles).toEqual({})
    expect(merged.report.missingFromAccount.map((v) => v.village_id)).toEqual([99999])
  })

  it('does not clear a role the file row is silent about', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, trade_office_level: 4 }],
    }
    const merged = mergeSetup({
      setup: roundTrip(doc),
      villages: VILLAGES,
      villageRoles: { 20030: 'capital' },
    })

    expect(merged.villageRoles).toEqual({ 20030: 'capital' })
  })

  it('reports the roles it loaded, so a partial file is visible', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [],
      roles: { def: {}, capital: {} },
    }
    const merged = mergeSetup({ setup: roundTrip(doc), villages: VILLAGES })

    expect(merged.report.rolesLoaded).toEqual(['capital', 'def'])
  })
})

describe('roleDeviates', () => {
  // The allocation grid marks a cell where an explicit value differs from the
  // role's template. It must be the same predicate the backend reports the
  // deviation with, or the grid marks a cell the plan did not -- worse than
  // marking none, because it is confidently wrong.
  const TEMPLATE = { allocations: { lumber: { mode: 'absolute', value: 8372 } } }

  it('is false when the village says nothing', () => {
    expect(roleDeviates(TEMPLATE, 'lumber', undefined)).toBe(false)
  })

  it('is false when the village says the same thing', () => {
    expect(roleDeviates(TEMPLATE, 'lumber', { mode: 'absolute', value: 8372 })).toBe(false)
  })

  it('is true on a different value, and on a different mode', () => {
    expect(roleDeviates(TEMPLATE, 'lumber', { mode: 'absolute', value: 12000 })).toBe(true)
    expect(roleDeviates(TEMPLATE, 'lumber', { mode: 'percentage', value: 8372 })).toBe(true)
  })

  it('is true for an explicit keep, which is a different answer from the template', () => {
    // In the planner KEEP means "hold your own production", so it overrides the
    // template rather than falling through to it -- and the backend reports it
    // as a deviation.
    expect(roleDeviates(TEMPLATE, 'lumber', { mode: 'keep', value: 0 })).toBe(true)
  })

  it('is false for a resource the template has no opinion about', () => {
    expect(roleDeviates(TEMPLATE, 'clay', { mode: 'absolute', value: 5168 })).toBe(false)
  })

  it('is false with no template at all, which is a village with no role', () => {
    expect(roleDeviates(undefined, 'lumber', { mode: 'absolute', value: 1 })).toBe(false)
  })

  it('reads an absent value as the zero the backend reads it as', () => {
    // `Number(undefined)` is NaN and NaN !== NaN, so an allocation missing
    // `value` on either side read as a deviation the backend would never
    // report: `AllocationInput.value` defaults to 0.0, so `{mode: 'keep'}` and
    // `{mode: 'keep', value: 0}` are the same allocation there. The grid would
    // have marked the cell and named a figure the plan agrees with.
    expect(roleDeviates({ allocations: { lumber: { mode: 'keep' } } }, 'lumber', {
      mode: 'keep',
      value: 0,
    })).toBe(false)
    expect(roleDeviates(TEMPLATE, 'lumber', { mode: 'absolute' })).toBe(true)
    expect(
      roleDeviates({ allocations: { lumber: { mode: 'absolute' } } }, 'lumber', {
        mode: 'absolute',
        value: 0,
      })
    ).toBe(false)
  })
})

describe('resolveRoleAllocation and resolveRoleSpend', () => {
  // The page has to resolve the templates itself, because the operator edits
  // before any plan exists. Two implementations of one merge rule drift, so the
  // rule lives here, beside `roleDeviates` which asks the other half of the
  // same question, and the component is a thin wrapper over both. What this
  // pins is the ORDER -- village, then role, then keep -- because getting it
  // wrong is not a visible bug: the grid would show a defensive village as
  // "Keep own" while the plan shipped it 8,372/h, and the unassigned meter
  // would count its own 1,500 instead, so the Rest village's displayed target
  // would be wrong by the difference.
  const TEMPLATE = {
    allocations: { lumber: { mode: 'absolute', value: 8372 } },
    consumption: { lumber: 8372, clay: 5168 },
  }

  it('takes the village own entry first', () => {
    expect(
      resolveRoleAllocation(TEMPLATE, 'lumber', { mode: 'percentage', value: 10 })
    ).toEqual({ mode: 'percentage', value: 10 })
  })

  it('falls through to the role template', () => {
    expect(resolveRoleAllocation(TEMPLATE, 'lumber', undefined)).toEqual({
      mode: 'absolute',
      value: 8372,
    })
  })

  it('falls through per resource, not per village', () => {
    // Overriding lumber must not revert clay: the whole reason the merge is per
    // resource is that one of four defensive villages always has a wall going
    // up and wants ONE figure changed.
    expect(resolveRoleAllocation(TEMPLATE, 'clay', undefined)).toEqual(KEEP_ALLOCATION)
  })

  it('is keep when there is no role and no entry', () => {
    expect(resolveRoleAllocation(undefined, 'lumber', undefined)).toEqual(KEEP_ALLOCATION)
    expect(KEEP_ALLOCATION).toEqual({ mode: 'keep', value: 0 })
  })

  it('keeps an explicit keep, which a role makes meaningful', () => {
    // With a role the alternative to the template is not "nothing", it is
    // "hold your own production" -- so the entry has to survive resolution,
    // and `roleDeviates` reports it.
    const kept = resolveRoleAllocation(TEMPLATE, 'lumber', { mode: 'keep', value: 0 })
    expect(kept).toEqual({ mode: 'keep', value: 0 })
    expect(roleDeviates(TEMPLATE, 'lumber', kept)).toBe(true)
  })

  it('resolves a spend the same way', () => {
    expect(resolveRoleSpend(TEMPLATE, 'lumber', 9500)).toBe(9500)
    expect(resolveRoleSpend(TEMPLATE, 'clay', undefined)).toBe(5168)
    expect(resolveRoleSpend(TEMPLATE, 'iron', undefined)).toBeUndefined()
    expect(resolveRoleSpend(undefined, 'lumber', undefined)).toBeUndefined()
  })

  it('keeps a declared spend of zero, which is a claim and not silence', () => {
    expect(resolveRoleSpend(TEMPLATE, 'lumber', 0)).toBe(0)
  })
})

describe('the unknown-resource message names only what may be declared', () => {
  const declare = (spend) =>
    roundTrip({
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20030, consumption_per_hour: spend }],
    })

  it('offers lumber, clay and iron', () => {
    expect(() => declare({ gold: 100 })).toThrow(/unknown resource "gold"/)
    expect(() => declare({ gold: 100 })).toThrow(/must be one of lumber, clay, iron\./)
  })

  it('does not offer crop, which it refuses one branch later', () => {
    // The operator took the message's advice, corrected "gold" to "crop", and
    // got a second error for it.
    let message = ''
    try {
      declare({ gold: 100 })
    } catch (err) {
      message = err.message
    }
    expect(message).not.toMatch(/crop/)
  })
})

describe('relayFlagsOnly', () => {
  // `may_relay` was the one hydrated map loaded raw. Its three neighbours in the
  // same effect are filtered on the way in -- `stripUnknownRoles`,
  // `stripStoredCropSpends`, and the merchant model's defaults merge -- and the
  // FILE path refuses a non-boolean outright with a `SetupFileError`. So a
  // stored value the select cannot produce rode into the request unexamined,
  // where the backend's `bool` either coerces it to something nobody typed
  // ("yes" is True) or 422s the whole plan over a figure with no box to clear it
  // from: nothing in the planner writes this map except a setup-file import.

  it('keeps both booleans, because both are answers', () => {
    // False is not absence here. Unset means "take the role's answer", so a
    // stored false is the operator refusing a relay this village's role allows.
    expect(relayFlagsOnly({ 20011: true, 20013: false })).toEqual({ 20011: true, 20013: false })
  })

  it('drops anything that is not a boolean', () => {
    // "yes" is the shape that matters: the backend's lax bool would read it as
    // true, so the plan would relay through a village on the strength of a
    // string nobody can see.
    expect(
      relayFlagsOnly({
        20011: 'yes',
        20013: 'true',
        20017: 1,
        20019: 0,
        20021: {},
        20023: [],
        20026: 'false',
      })
    ).toEqual({})
  })

  it('drops an explicit null, because absent is how "no answer" is stored', () => {
    // The request already reads `!= null` as absent, and a village with no
    // claims at all leaves the map entirely -- so keeping the key would be a
    // second way to say nothing.
    expect(relayFlagsOnly({ 20011: null, 20013: undefined, 20017: true })).toEqual({
      20017: true,
    })
  })

  it('keeps the readable entries and only drops the rest', () => {
    // One corrupt entry must not cost the account the flags it did type.
    expect(relayFlagsOnly({ 20011: true, 20013: 'yes', 20017: false })).toEqual({
      20011: true,
      20017: false,
    })
  })

  it('answers an absent or unusable map with an empty one', () => {
    expect(relayFlagsOnly(undefined)).toEqual({})
    expect(relayFlagsOnly(null)).toEqual({})
    expect(relayFlagsOnly('nonsense')).toEqual({})
    expect(relayFlagsOnly(7)).toEqual({})
    expect(relayFlagsOnly({})).toEqual({})
  })
})

describe('stripStoredCropSpends', () => {
  it('keeps the materials and drops the crop figure', () => {
    const { consumption } = stripStoredCropSpends({
      20030: { lumber: 14751, crop: 8519 },
      20032: { clay: 5168 },
    })

    expect(consumption).toEqual({ 20030: { lumber: 14751 }, 20032: { clay: 5168 } })
  })

  it('says which villages lost one, so the strip is not silent', () => {
    // R4-P3-2. Stripping is right; silence is not -- the removal makes a
    // previously-silenced CRITICAL reappear on the next plan with nothing
    // connecting cause to effect, while the file-import path raises a loud
    // SetupFileError for exactly the same figure.
    const { droppedFrom } = stripStoredCropSpends({
      20030: { lumber: 14751, crop: 8519 },
      20032: { clay: 5168 },
    })

    expect(droppedFrom).toEqual(['20030'])
  })

  it('reports a village whose only stored figure was crop, and drops the row', () => {
    // The loudest case: nothing about that village survives, so without the
    // receipt there is nothing on screen to notice.
    const { consumption, droppedFrom } = stripStoredCropSpends({ 20031: { crop: 8519 } })

    expect(consumption).toEqual({})
    expect(droppedFrom).toEqual(['20031'])
  })

  it('reports nothing when nothing was stripped', () => {
    const { consumption, droppedFrom } = stripStoredCropSpends({ 20030: { lumber: 1 } })

    expect(consumption).toEqual({ 20030: { lumber: 1 } })
    expect(droppedFrom).toEqual([])
  })

  it('drops a village whose map is empty, as "declared nothing" is one state', () => {
    const { consumption, droppedFrom } = stripStoredCropSpends({ 20030: {}, 20031: null })

    expect(consumption).toEqual({})
    expect(droppedFrom).toEqual([])
  })

  it('survives an empty or missing store', () => {
    // Hydration runs against whatever localStorage holds, including nothing.
    expect(stripStoredCropSpends(undefined)).toEqual({ consumption: {}, droppedFrom: [] })
    expect(stripStoredCropSpends(null)).toEqual({ consumption: {}, droppedFrom: [] })
    expect(stripStoredCropSpends({})).toEqual({ consumption: {}, droppedFrom: [] })
  })

  it('reports a crop figure of zero, which is a declaration like any other', () => {
    const { droppedFrom } = stripStoredCropSpends({ 20030: { lumber: 1, crop: 0 } })

    expect(droppedFrom).toEqual(['20030'])
  })
})

describe('describeConsumption', () => {
  it('says nothing is declared when nothing is', () => {
    expect(describeConsumption(undefined)).toBe('none')
    expect(describeConsumption({})).toBe('none')
  })

  it('sums the declared materials and names them', () => {
    expect(describeConsumption({ lumber: 14751, clay: 5168 })).toBe(
      `${(19919).toLocaleString()}/h · Lumber, Clay`
    )
  })

  it('collapses to "all three" once every material is declared', () => {
    expect(describeConsumption({ lumber: 1000, clay: 2000, iron: 3000 })).toBe(
      `${(6000).toLocaleString()}/h, all three`
    )
  })

  it('never summarises a crop figure an older build stored', () => {
    // Crop is refused everywhere else, so summarising it here would print a
    // spend that is not being applied -- and `stripStoredCropSpends` has
    // already thrown it away by the time the cell renders.
    expect(describeConsumption({ crop: 8519 })).toBe('none')
    expect(describeConsumption({ lumber: 1000, crop: 8519 })).toBe(
      `${(1000).toLocaleString()}/h · Lumber`
    )
  })

  it('reads a declared zero as a declaration', () => {
    expect(describeConsumption({ iron: 0 })).toBe('0/h · Iron')
  })
})

describe('allocationsForRequest', () => {
  const OWN = [20002, 20011, 20013]

  it('carries the edited figure a templated cell wrote, not a keep', () => {
    // The payload half of the data-loss trace. `withEditedAllocation` is the
    // store half; composing the two is the whole path from a keystroke to the
    // request, and it is the composition that went wrong: the store held
    // `{keep, 12000}`, this map sent it (the village HAS a role, so a keep is a
    // statement here and is not dropped), and the backend resolved KEEP to
    // "hold your own production" -- 1,500/h at village 11, while the role's
    // 8,372/h spend went on being applied.
    const edited = withEditedAllocation({
      perVillage: {},
      villageId: 20011,
      template: { allocations: { lumber: { mode: 'absolute', value: 8372 } } },
      resource: 'lumber',
      patch: { value: 12_000 },
    })

    const sent = allocationsForRequest({ lumber: edited }, { 20011: 'def' }, OWN)

    expect(sent.lumber['20011']).toEqual({ mode: 'absolute', value: 12_000 })
  })

  it('drops a keep on a village with no role, and sends one on a village with', () => {
    const sent = allocationsForRequest(
      {
        lumber: {
          20002: { mode: 'keep', value: 0 },
          20011: { mode: 'keep', value: 0 },
        },
      },
      { 20011: 'def' },
      OWN
    )

    // Silence for the ordinary village; a statement for the templated one --
    // dropped, the template would fill straight back in.
    expect(sent.lumber).toEqual({ 20011: { mode: 'keep', value: 0 } })
  })

  it('drops a village the snapshot no longer has', () => {
    // A chiefed or renamed village would 400 the whole request.
    const sent = allocationsForRequest(
      { lumber: { 20011: { mode: 'absolute', value: 1 }, 29999: { mode: 'absolute', value: 2 } } },
      {},
      OWN
    )

    expect(Object.keys(sent.lumber)).toEqual(['20011'])
  })

  it('omits a resource left with nothing usable rather than sending it empty', () => {
    // So an untouched account's request is byte-identical to a pre-roles one.
    expect(allocationsForRequest({ lumber: { 20002: { mode: 'keep' } } }, {}, OWN)).toEqual({})
    expect(allocationsForRequest({}, {}, OWN)).toEqual({})
  })

  it('sends an allocation whose rate the snapshot could not read', () => {
    // Filtering those hid them from the backend's UNREADABLE_RATE critical, so
    // the plan read "Ready to run" while planning without an allocation the
    // operator wrote. Nothing here looks at production at all -- which is the
    // point, pinned so a future filter cannot creep back in.
    const sent = allocationsForRequest(
      { iron: { 20013: { mode: 'absolute', value: 5809 } } },
      {},
      OWN
    )

    expect(sent.iron['20013']).toEqual({ mode: 'absolute', value: 5809 })
  })
})

describe('isEmptyTemplate', () => {
  // One predicate, because two readers of "is there a template here" that
  // disagree is exactly the defect: `rolesForRequest` decides whether the
  // backend gets the chance to refuse, and the panel's warning decides whether
  // the operator is told. They have to answer the same question the same way.

  it('calls a missing template empty', () => {
    expect(isEmptyTemplate(undefined)).toBe(true)
    expect(isEmptyTemplate(null)).toBe(true)
    expect(isEmptyTemplate({})).toBe(true)
  })

  it('calls a template with every box cleared empty', () => {
    // The shapes the panel's own setters leave behind: a deleted allocation
    // entry, a deleted spend entry, a `null` written by the relay select's
    // unset option, and a `false` written by unticking the checkbox.
    expect(isEmptyTemplate({ allocations: {} })).toBe(true)
    expect(isEmptyTemplate({ consumption: {} })).toBe(true)
    expect(isEmptyTemplate({ crop_negative_by_design: false })).toBe(true)
    expect(isEmptyTemplate({ may_relay: null })).toBe(true)
    expect(
      isEmptyTemplate({
        allocations: {},
        consumption: {},
        may_relay: null,
        crop_negative_by_design: false,
      })
    ).toBe(true)
  })

  it('calls any single surviving figure a template', () => {
    expect(isEmptyTemplate({ allocations: { lumber: KEEP_ALLOCATION } })).toBe(false)
    expect(isEmptyTemplate({ consumption: { lumber: 8372 } })).toBe(false)
    // Zero is a measured claim about a village that spends none, not silence --
    // the same reason `setTemplateSpend` deletes on an empty string rather than
    // storing a zero.
    expect(isEmptyTemplate({ consumption: { lumber: 0 } })).toBe(false)
    expect(isEmptyTemplate({ crop_negative_by_design: true })).toBe(false)
    expect(isEmptyTemplate({ may_relay: true })).toBe(false)
    // And false, which is a refusal rather than an absence.
    expect(isEmptyTemplate({ may_relay: false })).toBe(false)
  })

  it('ignores a key the request would not carry anyway', () => {
    // `rolesForRequest` spells out four halves, so a stray key is dropped on
    // the way out. A template whose ONLY content is such a key would arrive at
    // the backend as `{}` -- the state this predicate exists to catch.
    expect(isEmptyTemplate({ nonsense: 1 })).toBe(true)
  })
})

describe('rolesForRequest', () => {
  const DEF = {
    allocations: { lumber: { mode: 'absolute', value: 8372 } },
    consumption: { lumber: 8372, clay: 5168, iron: 5809 },
    may_relay: null,
    crop_negative_by_design: false,
  }

  it('skips a claimed role that has no template, so the plan is refused', () => {
    // Sending `{}` for it made the backend's "no role template was sent for
    // ..." 422 unreachable from the page: four villages set to DEF before the
    // panel was filled planned at HTTP 200 with target 1,500 and spend 0 --
    // a tenth of what those villages need, reported as feasible with an empty
    // `role_deviations` to say nothing was overridden.
    expect(rolesForRequest({}, new Set(['def']))).toEqual({})
    expect(rolesForRequest({ def: null }, new Set(['def']))).toEqual({})
  })

  it('still sends a template that is only half typed', () => {
    // A template is a template from the moment the operator gives a role any
    // figure at all. Refusing a half-typed one would refuse an account
    // mid-edit, so ONE figure is enough -- here a lumber target and nothing
    // else, no spend and no relay answer. It used to be asserted with
    // `{allocations: {}}`, which is not half typed but EMPTY, and pinned the
    // defect below as if it were the rule.
    expect(
      rolesForRequest({ def: { allocations: { lumber: KEEP_ALLOCATION } } }, new Set(['def']))
    ).toEqual({
      def: {
        allocations: { lumber: KEEP_ALLOCATION },
        consumption: {},
        may_relay: null,
        crop_negative_by_design: false,
      },
    })
  })

  it('treats an EMPTIED template as absent, whichever box was emptied last', () => {
    // The key outlives the last figure: every setter writes through
    // `{...prev, [role]: {...}}` and none deletes the role when the last box is
    // cleared, so `{"def": {"consumption": {}}}` reached the backend, which
    // accepted it -- village 11 planned at target 1,500 / spend 0 with an empty
    // `role_deviations`, and the page's own missing-template warning stayed
    // silent because it read the same key. An empty template has to be the same
    // state as no template: refused by the backend's 422, and named on screen.
    //
    // One case per way of emptying one, because they leave different shapes
    // behind. A spend typed and then cleared:
    expect(rolesForRequest({ def: { consumption: {} } }, new Set(['def']))).toEqual({})
    // A mode set to absolute and then back to keep, which DELETES the entry:
    expect(rolesForRequest({ def: { allocations: {} } }, new Set(['def']))).toEqual({})
    // By-design ticked and then unticked, which stores `false` rather than
    // removing the key:
    expect(
      rolesForRequest({ def: { crop_negative_by_design: false } }, new Set(['def']))
    ).toEqual({})
    // And all of them at once, which is what the panel holds after a full edit
    // has been undone box by box:
    expect(
      rolesForRequest(
        {
          def: {
            allocations: {},
            consumption: {},
            may_relay: null,
            crop_negative_by_design: false,
          },
        },
        new Set(['def'])
      )
    ).toEqual({})
  })

  it('sends a template whose only answer is a relay refusal', () => {
    // `may_relay: false` is not emptiness. Unset means "take the role's own
    // default", so false is the operator overriding that default -- and for the
    // account whose defensive village sits on the only road to a corner of the
    // map, that field is the whole template.
    const sent = rolesForRequest({ def: { may_relay: false } }, new Set(['def']))

    expect(sent.def.may_relay).toBe(false)
  })

  it('sends only the roles some village actually claims', () => {
    const sent = rolesForRequest({ def: DEF, feeder: DEF }, new Set(['def']))

    expect(Object.keys(sent)).toEqual(['def'])
  })

  it('drops a crop spend an older build could have stored', () => {
    // The backend 422s a template's crop spend, and the editor no longer shows
    // one -- so a stored figure would refuse every plan over a number the
    // operator cannot find.
    const sent = rolesForRequest(
      { def: { ...DEF, consumption: { ...DEF.consumption, crop: 2200 } } },
      new Set(['def'])
    )

    expect(sent.def.consumption).toEqual({ lumber: 8372, clay: 5168, iron: 5809 })
  })

  it('spells out all four halves of a template rather than spreading it', () => {
    // So a template carrying an unknown key cannot smuggle it into a request,
    // and one carrying none still arrives complete.
    const sent = rolesForRequest(
      { feeder: { allocations: DEF.allocations, may_relay: true, nonsense: 1 } },
      new Set(['feeder'])
    )

    expect(Object.keys(sent.feeder).sort()).toEqual([
      'allocations',
      'consumption',
      'crop_negative_by_design',
      'may_relay',
    ])
    expect(sent.feeder.may_relay).toBe(true)
  })
})

describe('resolvedSpend', () => {
  const DEF = { consumption: { lumber: 8372, clay: 5168, iron: 5809 } }

  it('reads the role figures a templated village will actually spend', () => {
    // The defect on the setup table: it read the own map alone, so a
    // defensive village showed "none" and three blank boxes while the plan
    // spent 8,372/h of its lumber. The Allocate view resolved the same
    // question correctly, so the two surfaces disagreed about one village.
    const { effective, fromRole, overridden } = resolvedSpend(DEF, undefined)

    expect(effective).toEqual({ lumber: 8372, clay: 5168, iron: 5809 })
    expect(fromRole).toEqual(['lumber', 'clay', 'iron'])
    expect(overridden).toEqual([])
    expect(describeConsumption(effective)).toBe(`${(19349).toLocaleString()}/h, all three`)
  })

  it('takes an own figure over the role, per resource', () => {
    // Overriding one village's lumber must leave its clay and iron on the
    // profile -- the same per-resource rule the targets follow.
    const { effective, fromRole, overridden } = resolvedSpend(DEF, { lumber: 14751 })

    expect(effective).toEqual({ lumber: 14751, clay: 5168, iron: 5809 })
    expect(fromRole).toEqual(['clay', 'iron'])
    expect(overridden).toEqual(['lumber'])
  })

  it('reads a declared zero as an override, not as silence', () => {
    // Zero says "measured, and it spends none", which is a claim. Falling
    // through to the role here would spend 8,372/h at a village the operator
    // has explicitly said spends nothing.
    const { effective, overridden } = resolvedSpend(DEF, { lumber: 0 })

    expect(effective.lumber).toBe(0)
    expect(overridden).toEqual(['lumber'])
  })

  it('resolves to nothing for a village with no role and no figures', () => {
    const { effective, fromRole } = resolvedSpend(undefined, undefined)

    expect(effective).toEqual({})
    expect(fromRole).toEqual([])
    expect(describeConsumption(effective)).toBe('none')
  })

  it('never carries a crop figure through', () => {
    // Crop cannot be declared on either side; a template stored by an older
    // build could still hold one, and printing it would claim a spend the
    // planner refuses to apply.
    const { effective } = resolvedSpend({ consumption: { crop: 2200 } }, { crop: 8519 })

    expect(effective).toEqual({})
  })
})

describe('describeSpendSource', () => {
  it('credits the role where every figure on screen is the role s', () => {
    expect(describeSpendSource({ fromRole: ['lumber'], overridden: [] }, 'def')).toBe('from DEF')
  })

  it('names the resources the village states itself', () => {
    expect(
      describeSpendSource({ fromRole: ['clay', 'iron'], overridden: ['lumber'] }, 'def')
    ).toBe('DEF, own Lumber')
  })

  it('says nothing where the role supplied none of the figures', () => {
    // An untemplated village, and a village that overrode every figure the
    // role has: crediting the profile there would name one the plan is not
    // using.
    expect(describeSpendSource({ fromRole: [], overridden: [] }, undefined)).toBeNull()
    expect(describeSpendSource({ fromRole: [], overridden: ['lumber'] }, 'def')).toBeNull()
  })
})

describe('stripUnknownRoles', () => {
  it('drops a role outside the five and says which villages lost one', () => {
    // The receipt `stripStoredCropSpends` already gives, for a larger
    // consequence: a dropped role takes that village's targets AND its spend,
    // so four defensive villages silently revert to keeping their own
    // production -- a tenth of what they need -- and the plan reads feasible.
    const stripped = stripUnknownRoles(
      { 20011: 'def', 20013: 'hammer', 20017: 'hammer' },
      { def: { allocations: {} } }
    )

    expect(stripped.villageRoles).toEqual({ 20011: 'def' })
    expect(stripped.droppedFrom).toEqual(['20013', '20017'])
    expect(stripped.droppedNames).toEqual(['hammer'])
  })

  it('drops a template keyed by a name that is not one of the five', () => {
    const stripped = stripUnknownRoles({}, { def: { allocations: {} }, hammer: {} })

    expect(Object.keys(stripped.templates)).toEqual(['def'])
    expect(stripped.droppedTemplates).toEqual(['hammer'])
  })

  it('keeps a template no village claims', () => {
    // A file being complete, not an error -- the backend accepts it.
    const stripped = stripUnknownRoles({}, { capital: { allocations: {} } })

    expect(stripped.templates).toEqual({ capital: { allocations: {} } })
    expect(stripped.droppedTemplates).toEqual([])
  })

  it('reports each unknown name once however many villages carried it', () => {
    const stripped = stripUnknownRoles({ 1: 'hammer', 2: 'hammer', 3: 'sitter' }, {})

    expect(stripped.droppedNames).toEqual(['hammer', 'sitter'])
    expect(stripped.droppedFrom).toHaveLength(3)
  })

  it('reports nothing for state this build wrote', () => {
    const stripped = stripUnknownRoles({ 20011: 'def' }, { def: {} })

    expect(stripped.droppedFrom).toEqual([])
    expect(stripped.droppedNames).toEqual([])
    expect(stripped.droppedTemplates).toEqual([])
  })

  it('survives absent or malformed storage', () => {
    for (const bad of [null, undefined, 'nonsense', 42]) {
      const stripped = stripUnknownRoles(bad, bad)
      expect(stripped.villageRoles).toEqual({})
      expect(stripped.templates).toEqual({})
      expect(stripped.droppedFrom).toEqual([])
    }
  })
})

describe('may_relay in the setup file', () => {
  it('carries a per-village relay answer both ways', () => {
    // Per village, not per role: the account this exists for has ONE defensive
    // village on the only road to a corner of the map, and putting the
    // override on the template would hand the permission to all four.
    const setup = buildSetup({
      villages: VILLAGES,
      mayRelay: { 20031: true, 20032: false },
      exportedAt: STAMP,
    })

    expect(setup.villages).toHaveLength(2)
    expect(setup.villages.find((v) => v.village_id === 20031).may_relay).toBe(true)
    // false is WRITTEN, not dropped: keeping one village out of a tier its
    // role permits is the asymmetric half of the setting worth carrying, and
    // an absent field means "take the template's answer" instead.
    expect(setup.villages.find((v) => v.village_id === 20032).may_relay).toBe(false)
  })

  it('leaves a village out where it says nothing about relaying', () => {
    expect(buildSetup({ villages: VILLAGES, mayRelay: {}, exportedAt: STAMP }).villages).toEqual([])
  })

  it('reads it back over the map on screen, per village', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [
        { village_id: 20031, may_relay: true },
        { village_id: 20032, may_relay: false },
      ],
    }
    const merged = mergeSetup({
      setup: parseSetup(JSON.stringify(doc)),
      villages: VILLAGES,
      mayRelay: { 20030: true },
    })

    // Silence is not a clear: 20030 said nothing in the file and keeps what it
    // had, the rule every other column here follows.
    expect(merged.mayRelay).toEqual({ 20030: true, 20031: true, 20032: false })
  })

  it('rejects a relay answer that is not a boolean', () => {
    // "yes" and 1 are the two shapes a hand-edited file arrives in, and both
    // would coerce to true -- so a village told "no" in words would relay.
    for (const bad of ['yes', 1, {}]) {
      const doc = {
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20031, may_relay: bad }],
      }
      expect(() => parseSetup(JSON.stringify(doc))).toThrow(SetupFileError)
      expect(() => parseSetup(JSON.stringify(doc))).toThrow(/may_relay/)
    }
  })

  it('survives a round trip', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      villageRoles: { 20031: 'def' },
      mayRelay: { 20031: true },
      exportedAt: STAMP,
    })
    const back = parseSetup(JSON.stringify(setup))

    expect(back.villages[0]).toEqual({
      village_id: 20031,
      name: 'V05',
      role: 'def',
      may_relay: true,
    })
  })
})

// ── The per-village merchant cap (format version 5) ────────────────────────
// Profile section 5 gives the capital one number -- "maximum 8 busy merchants
// at any instant" -- and it is owned state: nothing in the game states a
// ceiling. It is also the field whose loss is least visible. A Trade Office
// level dropped on a cleared origin makes the plan over-provision, which is
// safe; a CAP dropped makes the plan quietly commit sixteen merchants at a
// village the operator holds to eight, and the sheet reads feasible.
describe('the merchant cap in the setup file', () => {
  it('carries a cap, and exports a village whose only typed field is one', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      maxBusy: { 20031: 8 },
      exportedAt: STAMP,
    })

    expect(setup.villages.map((v) => v.village_id)).toEqual([20031])
    expect(setup.villages[0].max_busy_merchants).toBe(8)
  })

  it('keeps a cap of 0, which says "every route from here is a breach"', () => {
    // The same rule a Trade Office level of 0 follows: an answer, not a blank.
    // Dropped, the village goes back to shipping with its whole fleet.
    const setup = buildSetup({ villages: VILLAGES, maxBusy: { 20032: 0 }, exportedAt: STAMP })

    expect(setup.villages[0].max_busy_merchants).toBe(0)
  })

  it('survives a round trip beside the other owned columns', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20031: 13 },
      maxBusy: { 20031: 8 },
      exportedAt: STAMP,
    })

    expect(parseSetup(JSON.stringify(setup)).villages[0]).toEqual({
      village_id: 20031,
      name: 'V05',
      trade_office_level: 13,
      max_busy_merchants: 8,
    })
  })

  it('refuses a cap that is not a whole number of merchants', () => {
    for (const bad of [8.5, '8', 'nine', true]) {
      const doc = {
        format: SETUP_FORMAT,
        version: SETUP_VERSION,
        villages: [{ village_id: 20031, max_busy_merchants: bad }],
      }
      expect(() => roundTrip(doc), JSON.stringify(bad)).toThrow(SetupFileError)
      expect(() => roundTrip(doc), JSON.stringify(bad)).toThrow(/merchant/)
    }
  })

  it('refuses a negative cap', () => {
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20031, max_busy_merchants: -1 }],
    }

    expect(() => roundTrip(doc)).toThrow(SetupFileError)
    expect(() => roundTrip(doc)).toThrow(/-1/)
  })

  it('refuses a cap above the 20 merchants a village can ever hold', () => {
    // Section 8's hard cap. Above it the figure cannot be a merchant count at
    // all, whatever the account -- and clamping it would leave the operator's
    // file and the plan describing different accounts, which is the same
    // reason the backend answers 422 rather than trimming.
    const doc = {
      format: SETUP_FORMAT,
      version: SETUP_VERSION,
      villages: [{ village_id: 20031, max_busy_merchants: 21 }],
    }

    expect(() => roundTrip(doc)).toThrow(SetupFileError)
    expect(() => roundTrip(doc)).toThrow(/20/)
  })

  it('applies the file over the caps on screen, leaving silent rows alone', () => {
    const setup = roundTrip(
      buildSetup({ villages: VILLAGES, maxBusy: { 20031: 8 }, exportedAt: STAMP })
    )

    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      maxBusy: { 20030: 12, 20031: 20 },
    })

    expect(merged.maxBusy).toEqual({ 20030: 12, 20031: 8 })
  })

  // The fourth deliberate second copy of a Python value in this file, and
  // until now the only one with neither a note nor a test. `SETUP_VERSION`,
  // `MAX_TRADE_OFFICE_LEVEL` and `MAX_MERCHANTS_PER_VILLAGE` are all pinned by
  // a literal on both sides; this pair was `useState(25)` / `useState(60)`
  // inside `pages/ResourcePlanner.jsx` against `DEFAULT_BASELINE_FILL = 0.25`
  // and `DEFAULT_TARGET_FILL = 0.60` in `night_profile.py`, with nothing in
  // `frontend/src/**/*.test.js*` or `frontend/e2e` naming either.
  //
  // The only guard was the on-screen disagreement note, and that needs a day
  // check to have RUN first: the note compares the boxes against
  // `dayCheck.pre_night_baseline` / `dayCheck.morning_floor`, so a divergence
  // is invisible until the operator asks for a full-day check. This pair
  // already drifted once -- the boxes defaulted to 30/80 while the check
  // graded against 25/60, so the derivation aimed at one night and the report
  // described another.
  it('mirrors the server night thresholds as a literal, on both sides', () => {
    expect(DEFAULT_BASELINE_FILL).toBe(0.25)
    expect(DEFAULT_TARGET_FILL).toBe(0.6)
    // Fractions, matching Python exactly, because that is the unit the
    // `/night-profile` and `/day-check` bodies carry. The percent the boxes
    // show is rounded off them -- `0.6 * 100` is 60.00000000000001 in IEEE, and
    // an input reading that is a different defect.
    expect(Math.round(DEFAULT_BASELINE_FILL * 100)).toBe(25)
    expect(Math.round(DEFAULT_TARGET_FILL * 100)).toBe(60)
  })

  it('is a version 11 file, and every older one simply carries less', () => {
    // Retitled at every bump -- v4 (the per-village merchant cap), v5, v6
    // (section 5's declared relay tier), v7, v8, v9 for the reserved NPC-burst
    // window, v10 for the window prune, and v11 for the measured merchant
    // model. The contract is the RULE, not the
    // number: the version rises whenever a field is added, so a build that
    // cannot read the new one REFUSES a file it would otherwise half-load. A v4
    // build silently dropping a cap plans sixteen merchants where the operator
    // allowed eight; a v5 build dropping a relay tier reports the villages
    // beyond it as unreachable while the answer sits on screen; a v8 build
    // dropping the reserved window puts the manual NPC burst back into
    // competition with arrivals; a v9 build dropping the prune leaves every
    // out-of-window firing live in the game; a v10 build dropping the
    // acknowledgement asks the operator for a reading they have already taken,
    // on every plan.
    expect(SETUP_VERSION).toBe(11)

    const older = {
      format: SETUP_FORMAT,
      version: 4,
      villages: [{ village_id: 20030, trade_office_level: 3, may_relay: true }],
    }
    const parsed = roundTrip(older)

    expect(parsed.villages[0].trade_office_level).toBe(3)
    expect(parsed.villages[0].may_relay).toBe(true)
    expect(parsed.villages[0].max_busy_merchants).toBeUndefined()

    const v5 = {
      format: SETUP_FORMAT,
      version: 5,
      villages: [{ village_id: 20030, trade_office_level: 3, max_busy_merchants: 8 }],
    }
    const fromV5 = roundTrip(v5)

    expect(fromV5.villages[0].max_busy_merchants).toBe(8)
    expect(fromV5.villages[0].relay_for).toBeUndefined()
  })
})

// Every other integer-typed lever's predicate says `Number.isInteger` and this
// one did not, on the single figure that sizes every cargo the account ships.
// Backend twin: `PlanRequest.merchant_base_capacity` (`int`, `gt=0`) and
// `MerchantModelIn.base_capacity` (`int | None`) in
// src/travian_api/web/routes/distribution.py.
describe('isMerchantBaseCapacity', () => {
  it('takes a whole number of units, more than 0', () => {
    for (const value of [1, 750, 2500, 3200]) {
      expect(isMerchantBaseCapacity(value), String(value)).toBe(true)
    }
  })

  it('refuses a fraction, which the request would 422 over', () => {
    for (const value of [2500.5, 0.5, -0.5]) {
      expect(isMerchantBaseCapacity(value), String(value)).toBe(false)
    }
  })

  it('refuses 0, a negative, and anything that is not a number', () => {
    for (const value of [0, -1, NaN, Infinity, '2500', null, undefined]) {
      expect(isMerchantBaseCapacity(value), String(value)).toBe(false)
    }
  })
})

describe('isMaxBusyMerchants', () => {
  it('accepts every whole count a village can actually field', () => {
    for (const value of [0, 1, 8, 19, 20]) expect(isMaxBusyMerchants(value)).toBe(true)
  })

  it('refuses a fraction, a negative, a string and anything past 20', () => {
    for (const value of [8.5, -1, '8', 21, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(isMaxBusyMerchants(value), String(value)).toBe(false)
    }
  })
})

describe('unreachableCaps', () => {
  // The bound the FILE cannot check: a village's own merchant count comes from
  // the snapshot, so 20 is legal in a file and still wrong for the village
  // with 19. The backend answers that with a 422 naming the village; this is
  // the same fact, computed live, so the operator sees it on the cell they
  // typed rather than on their next plan.
  const FLEETS = [
    { village_id: 20030, name: 'Capital', merchants_total: 20 },
    { village_id: 20031, name: 'V05', merchants_total: 19 },
    { village_id: 20032, name: 'V16', merchants_total: 20 },
    // The shape `/snapshot` emits for a count it could not read: 0, with a
    // warning beside it saying so.
    { village_id: 20034, name: 'V31', merchants_total: 0 },
  ]

  it('is empty when every cap is inside its village fleet', () => {
    expect(unreachableCaps({ 20030: 8, 20031: 19 }, FLEETS)).toEqual([])
  })

  it('names the village, its cap and the fleet it cannot reach', () => {
    expect(unreachableCaps({ 20031: 20 }, FLEETS)).toEqual([
      { village_id: 20031, name: 'V05', cap: 20, merchants_total: 19 },
    ])
  })

  it('says nothing about a village whose merchant count was never read', () => {
    // Unknown is not zero. A village the snapshot could not read a merchant
    // count for has no bound to fail, and inventing one would flag a cap that
    // may be perfectly correct.
    //
    // 0 is the shape `/snapshot` actually emits for that, and the backend's own
    // reachability check skips it for this same reason -- so a cap of 20 on
    // V31 is not a mistake anyone can prove yet.
    expect(unreachableCaps({ 20034: 20 }, FLEETS)).toEqual([])
    // A cap for a village the snapshot does not carry at all is not this
    // function's question either; the backend refuses that one by name.
    expect(unreachableCaps({ 20033: 20 }, FLEETS)).toEqual([])
    // An ABSENT count is a shape no snapshot writes. Kept because a cap must
    // not be flagged off a field that is not there, but it is the defensive
    // case, not the real one.
    expect(unreachableCaps({ 20030: 20 }, [{ village_id: 20030, name: 'Capital' }])).toEqual([])
  })
})

describe('the account-wide merchant levers in the setup file', () => {
  // `merchant_reserve` and `merchant_headroom` are on PlanRequest and the page
  // never sent either, so the backend's defaults were the only values an
  // operator could have. They are account-wide, so they ride in the merchant
  // model rather than the village rows.
  it('round-trips the reserve and the headroom', () => {
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        tradeOffice: { 20030: 19 },
        merchantModel: {
          base_capacity: 2500,
          bonus_per_to_level: 0.2,
          merchant_reserve: 4,
          merchant_headroom: 0.25,
        },
        exportedAt: STAMP,
      })
    )

    expect(setup.merchantModel).toEqual({
      base_capacity: 2500,
      bonus_per_to_level: 0.2,
      merchant_reserve: 4,
      merchant_headroom: 0.25,
    })
  })

  it('leaves them out when the file does not carry them', () => {
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        tradeOffice: { 20030: 19 },
        merchantModel: { base_capacity: 2500, bonus_per_to_level: 0.2 },
        exportedAt: STAMP,
      })
    )

    expect(setup.merchantModel).toEqual({ base_capacity: 2500, bonus_per_to_level: 0.2 })
  })

  it('refuses a reserve that is not a whole number of merchants', () => {
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.merchant_model = { base_capacity: 2500, bonus_per_to_level: 0.2, merchant_reserve: -1 }

    expect(() => roundTrip(doc)).toThrow(/merchant_reserve/)
  })

  it('refuses a reserve past the 20 merchants a village can hold', () => {
    // The backend's `le=20`, and the same MAX_MERCHANTS_PER_VILLAGE the cap is
    // bounded by: a reserve of 50 holds back merchants no village has, taking
    // every budget to 0 while the request still reads as valid.
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.merchant_model = { base_capacity: 2500, bonus_per_to_level: 0.2, merchant_reserve: 21 }

    expect(() => roundTrip(doc)).toThrow(/merchant_reserve/)
  })

  // The two WORLD overrides, which `buildSetup` has always written -- it stores
  // `merchant_model` wholesale -- and which `parseSetup` rebuilt field by field
  // and therefore dropped. A round trip lost both in silence, which is the worst
  // shape a loss can take here: `map_span` scales every distance the geometry
  // computes and the speed divides into every travel time, so an operator on a
  // non-Europe-2 world reloaded their own file and planned another world's
  // journeys with no message at all.
  //
  // NO VERSION BUMP, and that is checked below: the document byte for byte is
  // what it always was, because the writer already emitted these keys inside v9
  // and the store keeps the body verbatim (`MerchantModelIn` ignores extras).
  // Only the reader changes, so the two halves do not have to move together.
  it('round-trips the two world overrides the writer has always written', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      merchantModel: {
        base_capacity: 2500,
        bonus_per_to_level: 0.2,
        map_span: 801,
        speed_fields_per_hour: 24,
      },
      exportedAt: STAMP,
    })

    // The writer's half, unchanged.
    expect(doc.merchant_model.map_span).toBe(801)
    expect(doc.merchant_model.speed_fields_per_hour).toBe(24)
    expect(doc.version).toBe(SETUP_VERSION)

    expect(roundTrip(doc).merchantModel).toEqual({
      base_capacity: 2500,
      bonus_per_to_level: 0.2,
      map_span: 801,
      speed_fields_per_hour: 24,
    })
  })

  it('leaves the world overrides out when the file does not carry them', () => {
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        tradeOffice: { 20030: 19 },
        merchantModel: { base_capacity: 2500, bonus_per_to_level: 0.2 },
        exportedAt: STAMP,
      })
    )
    expect(setup.merchantModel).not.toHaveProperty('map_span')
    expect(setup.merchantModel).not.toHaveProperty('speed_fields_per_hour')
  })

  it('refuses an even map span, which a world centred on 0|0 cannot have', () => {
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.merchant_model = { base_capacity: 2500, bonus_per_to_level: 0.2, map_span: 800 }

    expect(() => roundTrip(doc)).toThrow(/map_span/)
  })

  it('refuses a merchant speed of zero, which the backend divides by', () => {
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.merchant_model = {
      base_capacity: 2500,
      bonus_per_to_level: 0.2,
      speed_fields_per_hour: 0,
    }

    expect(() => roundTrip(doc)).toThrow(/speed_fields_per_hour/)
  })

  it('refuses a headroom of 1, which would hold every merchant back', () => {
    // The backend's `lt=1.0`: at 1 the whole budget is held clear and every
    // route is billed as crowding, which is not a plan.
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.merchant_model = { base_capacity: 2500, bonus_per_to_level: 0.2, merchant_headroom: 1 }

    expect(() => roundTrip(doc)).toThrow(/merchant_headroom/)
  })

  // Blank is a state these two boxes can be in, and it means "use the planner's
  // own". The PLAN path has always read it that way -- the field is omitted from
  // the request and the backend's default stands -- while this writer stored the
  // model wholesale and the reader demanded both, so an operator who cleared
  // either could not save at all: the PUT came back 422 "Field required" and the
  // export wrote a file this parser refused, with no cell marked on the way.
  //
  // Blank is NOT 0 and is not filled in with a number here. Writing 2,500 in
  // would make an emptied box look like a calibration the operator asserted --
  // and that figure sizes every cargo the account ever ships.
  it('leaves a blank base capacity out of the document rather than inventing one', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      merchantModel: {
        base_capacity: undefined,
        bonus_per_to_level: 0.2,
        merchant_reserve: 2,
        merchant_headroom: 0.1,
      },
      exportedAt: STAMP,
    })

    expect('base_capacity' in doc.merchant_model).toBe(false)
    expect(doc.merchant_model.bonus_per_to_level).toBe(0.2)
  })

  it('reads an absent base capacity back as blank, not as a refusal', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      merchantModel: { base_capacity: undefined, bonus_per_to_level: 0.2 },
      exportedAt: STAMP,
    })

    const setup = roundTrip(doc)
    expect(setup.merchantModel.base_capacity).toBeUndefined()
    expect(setup.merchantModel.bonus_per_to_level).toBe(0.2)
  })

  it('does the same for a blank Trade Office bonus', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      merchantModel: { base_capacity: 2500, bonus_per_to_level: undefined },
      exportedAt: STAMP,
    })

    expect('bonus_per_to_level' in doc.merchant_model).toBe(false)
    const setup = roundTrip(doc)
    expect(setup.merchantModel.base_capacity).toBe(2500)
    expect(setup.merchantModel.bonus_per_to_level).toBeUndefined()
  })

  it('omits the model entirely when every lever is blank', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      merchantModel: { base_capacity: undefined, bonus_per_to_level: undefined },
      exportedAt: STAMP,
    })

    // Omitted rather than written empty, the rule every other optional field in
    // this document follows: `{}` would import as a model that asserts nothing,
    // which reads identically to having none.
    expect('merchant_model' in doc).toBe(false)
    expect(roundTrip(doc).merchantModel).toBeNull()
  })

  it('still refuses a base capacity that is present and wrong', () => {
    // Absent is blank; 0 is a claim, and it is one the backend's `gt=0` refuses.
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.merchant_model = { base_capacity: 0, bonus_per_to_level: 0.2 }

    expect(() => roundTrip(doc)).toThrow(/base_capacity/)
  })

  // The one the reader ACCEPTED. `merchant_base_capacity` is an `int` on the
  // request, so a fractional capacity is a 422 -- and this parser let the file
  // in, which is the worst place to be lenient: the document loads, no cell is
  // marked, and every plan and every save from then on is refused over a figure
  // nothing on screen names.
  it('refuses a fractional base capacity, which the request is an int', () => {
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 1 }, exportedAt: STAMP })
    doc.merchant_model = { base_capacity: 2500.5, bonus_per_to_level: 0.2 }

    expect(() => roundTrip(doc)).toThrow(/base_capacity/)
  })
})

// ─── Section 7's feedstock override, and section 9's crop assumption ────────
//
// Both fields existed on the backend's models and NEITHER was reachable from
// this app: `npc_feedstock` on a village config, `assumed_crop_per_hour` on a
// role template. They are owned state -- the game states neither -- so they
// travel in the setup document with everything else the operator types.

describe('npc_feedstock, per village', () => {
  it('writes an override, and reads it back', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      stockFloors: { 20030: 0.3 },
      npcFeedstock: { 20030: ['clay', 'crop'] },
      exportedAt: STAMP,
    })

    expect(doc.villages).toEqual([
      {
        village_id: 20030,
        name: 'Capital',
        stock_floor_fraction: 0.3,
        npc_feedstock: ['clay', 'crop'],
      },
    ])
    expect(roundTrip(doc).villages[0].npc_feedstock).toEqual(['clay', 'crop'])
  })

  // A village the operator has said nothing else about still gets a row when it
  // carries a feedstock: dropping it would lose the answer entirely.
  it('is enough on its own to give a village a row', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      npcFeedstock: { 20031: ['crop'] },
      exportedAt: STAMP,
    })

    expect(doc.villages).toEqual([
      { village_id: 20031, name: 'V05', npc_feedstock: ['crop'] },
    ])
  })

  // Derived is the resting state, and an absent field is how a document says
  // so. An empty list would import as an override of nothing.
  it('drops an empty override on the way out rather than writing one', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 5 },
      npcFeedstock: { 20030: [] },
      exportedAt: STAMP,
    })

    expect(doc.villages[0]).not.toHaveProperty('npc_feedstock')
  })

  it('refuses an empty list on the way in, as the backend does', () => {
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 5 }, exportedAt: STAMP })
    doc.villages[0].npc_feedstock = []

    expect(() => roundTrip(doc)).toThrow(/npc_feedstock/)
  })

  it('refuses anything that is not one of the four stores', () => {
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 5 }, exportedAt: STAMP })
    doc.villages[0].npc_feedstock = ['gold']

    expect(() => roundTrip(doc)).toThrow(/gold/)
  })

  it('refuses the same store twice, which is not two feedstocks', () => {
    const doc = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 5 }, exportedAt: STAMP })
    doc.villages[0].npc_feedstock = ['clay', 'clay']

    expect(() => roundTrip(doc)).toThrow(/npc_feedstock/)
  })

  it('merges onto the account, and leaves an unmentioned village alone', () => {
    const setup = roundTrip(
      buildSetup({
        villages: VILLAGES,
        npcFeedstock: { 20030: ['clay', 'crop'] },
        exportedAt: STAMP,
      })
    )

    const merged = mergeSetup({
      setup,
      villages: VILLAGES,
      npcFeedstock: { 20032: ['iron'] },
    })

    expect(merged.npcFeedstock).toEqual({ 20030: ['clay', 'crop'], 20032: ['iron'] })
  })
})

describe('assumed_crop_per_hour, per role', () => {
  it('is a template all by itself, because it raises a real finding', () => {
    // Not empty: it moves no target and no cargo, but it is the figure the
    // drift check compares reality against, and a role whose template is
    // "empty" is SKIPPED from the request entirely.
    expect(isEmptyTemplate({ assumed_crop_per_hour: -5880 })).toBe(false)
    // 0.0 is a real claim -- "this village breaks even" -- and is checked as one.
    expect(isEmptyTemplate({ assumed_crop_per_hour: 0 })).toBe(false)
    // No assumption is not an assumption of zero.
    expect(isEmptyTemplate({ assumed_crop_per_hour: null })).toBe(true)
  })

  it('rides the request, negative figures included', () => {
    // 01 reads -5,880/h and is crop-negative BY DESIGN, so -5,880 is the right
    // value to record for it.
    const sent = rolesForRequest(
      { full_off: { assumed_crop_per_hour: -5880 }, def: { assumed_crop_per_hour: 0 } },
      ['full_off', 'def']
    )

    expect(sent.full_off.assumed_crop_per_hour).toBe(-5880)
    expect(sent.def.assumed_crop_per_hour).toBe(0)
  })

  it('is omitted where there is no assumption, never sent as zero', () => {
    const sent = rolesForRequest({ def: { consumption: { lumber: 8372 } } }, ['def'])

    expect(sent.def).not.toHaveProperty('assumed_crop_per_hour')
  })

  it('survives the file round trip with its sign', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      roles: { full_off: { allocations: {}, consumption: {}, assumed_crop_per_hour: -5880 } },
      exportedAt: STAMP,
    })

    expect(roundTrip(doc).roles.full_off.assumed_crop_per_hour).toBe(-5880)
  })

  it('reads a document that has no assumption as having none', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      roles: { def: { allocations: {}, consumption: { lumber: 8372 } } },
      exportedAt: STAMP,
    })

    expect(roundTrip(doc).roles.def.assumed_crop_per_hour).toBeNull()
  })

  it('refuses a non-numeric assumption rather than reading it as none', () => {
    const doc = buildSetup({
      villages: VILLAGES,
      roles: { def: { allocations: {}, consumption: {} } },
      exportedAt: STAMP,
    })
    doc.roles.def.assumed_crop_per_hour = 'lots'

    expect(() => roundTrip(doc)).toThrow(/assumed_crop_per_hour/)
  })
})

// ── The reserved NPC-burst window (format version 9) ─────────────────
//
// The one owned answer that neither persistence path carried.
// `grep -rn "reserved" frontend/src/utils/` found nothing at all: the window
// lived only in localStorage under `LS_RESERVED_WINDOW`, which is exactly the
// failure the page's own copy warns about two panels earlier -- "stored per
// browser origin, so they do not follow you between :80, :8001, the LAN address
// and Tailscale". Confirmed against a real saved document: its top level was
// `format, version, exported_at, account, villages, profiles, roles,
// profile_windows, npc_attended, merchant_model, foreign_targets`, with no
// `reserved_window` anywhere.
//
// Account-wide rather than per profile, and that is the reason it is a pair and
// not a map beside `profile_windows`: it is one person at one marketplace. The
// attendance answer next to it is per profile because the operator is awake for
// some windows and not others; when they sit down to trade is not a property of
// a window at all.

describe('the reserved NPC-burst window in the setup file', () => {
  it('rides the round trip, and the version rose for it', () => {
    const setup = buildSetup({
      villages: VILLAGES,
      tradeOffice: { 20030: 19 },
      reservedWindow: ['20:00', '21:00'],
      exportedAt: STAMP,
    })

    // 11 since the measured merchant capacity landed (10 was the window prune, 9
    // the reserved window this block is about, 8 the overnight declaration, 7
    // the per-profile NPC attendance, 6 the relay tier). Pinned to a literal on
    // purpose: the version has to rise
    // whenever a field is added, so that an older build refuses a file it would
    // otherwise half-load, and a literal is what makes forgetting the bump a
    // failing test rather than a tautology.
    expect(setup.version).toBe(11)
    expect(setup.reserved_window).toEqual(['20:00', '21:00'])
    expect(roundTrip(setup).reservedWindow).toEqual(['20:00', '21:00'])
  })

  it('is omitted when nothing is reserved, rather than written empty', () => {
    // Absent is the resting state and means "reserve nothing". An empty pair
    // reserves nothing either, so writing one would only make the document look
    // like it had an answer.
    const setup = buildSetup({ villages: VILLAGES, tradeOffice: { 20030: 19 }, exportedAt: STAMP })

    expect('reserved_window' in setup).toBe(false)
    expect(roundTrip(setup).reservedWindow).toBe(null)
  })

  it('refuses a pair that is not two clock times', () => {
    // A document is the operator ASSERTING an answer, so a malformed pair is
    // refused rather than coerced -- the same discipline `profile_windows`
    // follows, and the backend's `_ClockTime` pattern refuses it too.
    for (const bad of [['20:00'], ['20:00', '21:00', '22:00'], ['8pm', '21:00'], '20:00-21:00']) {
      expect(() =>
        roundTrip({ format: SETUP_FORMAT, version: SETUP_VERSION, villages: [], reserved_window: bad })
      ).toThrow(/reserved_window/)
    }
  })

  it('still reads a version 8 file, which simply has no reserved window', () => {
    const parsed = roundTrip({
      format: SETUP_FORMAT,
      version: 8,
      villages: [{ village_id: 20030, name: '02', trade_office_level: 19 }],
    })

    expect(parsed.reservedWindow).toBe(null)
    expect(parsed.villages[0].trade_office_level).toBe(19)
  })

  it('lets the file win over what is on screen, and says nothing when absent', () => {
    // The same rule the merchant model follows: a document that carries one
    // replaces it, and a document that does not leaves the screen alone.
    const withOne = mergeSetup({
      setup: roundTrip(
        buildSetup({ villages: VILLAGES, reservedWindow: ['20:00', '21:00'], exportedAt: STAMP })
      ),
      villages: VILLAGES,
      reservedWindow: ['09:00', '10:00'],
    })
    expect(withOne.reservedWindow).toEqual(['20:00', '21:00'])

    const withNone = mergeSetup({
      setup: roundTrip(buildSetup({ villages: VILLAGES, exportedAt: STAMP })),
      villages: VILLAGES,
      reservedWindow: ['09:00', '10:00'],
    })
    expect(withNone.reservedWindow).toEqual(['09:00', '10:00'])
  })
})

// ── The other half of the deviation mark ────────────────────────────────
//
// An override was marked ("≠ DEF: Absolute /h 8,372") while the cell it would
// be overridden FROM carried nothing, so the operator could not tell -- before
// touching a cell -- that touching it creates an override.
describe('roleInherits', () => {
  const DEF = { allocations: { lumber: { mode: 'absolute', value: 8372 } } }

  it('is true for a templated resource the village has not touched', () => {
    expect(roleInherits(DEF, 'lumber', undefined)).toBe(true)
  })

  it('is false once the village states its own, agreeing or not', () => {
    // The agreeing case matters: an own entry is what the NEXT template edit
    // will no longer reach, so it is an override whatever it currently says.
    expect(roleInherits(DEF, 'lumber', { mode: 'absolute', value: 8372 })).toBe(false)
    expect(roleInherits(DEF, 'lumber', { mode: 'absolute', value: 9000 })).toBe(false)
  })

  it('is false for a resource the template says nothing about', () => {
    expect(roleInherits(DEF, 'clay', undefined)).toBe(false)
  })

  it('is false with no template and no role at all', () => {
    expect(roleInherits(undefined, 'lumber', undefined)).toBe(false)
    expect(roleInherits({}, 'lumber', undefined)).toBe(false)
  })

  // The two marks are mutually exclusive, which is what lets the cell show one
  // provenance note rather than two that contradict each other.
  it('never holds at the same time as roleDeviates', () => {
    for (const allocation of [undefined, { mode: 'absolute', value: 8372 }, { mode: 'keep' }]) {
      expect(
        roleInherits(DEF, 'lumber', allocation) && roleDeviates(DEF, 'lumber', allocation)
      ).toBe(false)
    }
  })
})

// ── The resting state of a village's relay permission ───────────────────
//
// `may_relay` had no input at all for four rounds, so nothing had to name what
// "unset" meant -- and it means three different things depending on the role,
// on the role's template, and on the sign of the village's own crop. The
// backend's order, mirrored: the village's own answer, then the template's,
// then `default_may_relay(role)`, then `crop_per_hour >= 0`.
describe('describeRelayPermission', () => {
  // Every case below carries a crop rate, because the cell does: it is fed
  // `cropPerHour: v.crop_per_hour` straight off the snapshot row. A rate that
  // could not be read is its own answer and has its own case at the foot of
  // this block.
  it('names the role default where the template is silent', () => {
    expect(describeRelayPermission({ role: 'def', template: undefined, cropPerHour: 2200 })).toBe(
      'Role default (may not)'
    )
    expect(describeRelayPermission({ role: 'feeder', template: {}, cropPerHour: 2200 })).toBe(
      'Role default (may)'
    )
  })

  // The capital is `may not` on purpose -- it is the hub every feeder ships to
  // and hands off from -- and it is the one everybody gets wrong.
  it('says the capital may not, which is the answer that surprises people', () => {
    expect(describeRelayPermission({ role: 'capital', cropPerHour: 2200 })).toBe(
      'Role default (may not)'
    )
  })

  it('names the TEMPLATE where the template has an opinion', () => {
    expect(
      describeRelayPermission({ role: 'def', template: { may_relay: true }, cropPerHour: 2200 })
    ).toBe('DEF template (may)')
    expect(
      describeRelayPermission({ role: 'feeder', template: { may_relay: false }, cropPerHour: 2200 })
    ).toBe('Feeder template (may not)')
  })

  it('falls to the crop sign for a village with no role, and says which way', () => {
    expect(describeRelayPermission({ role: null, cropPerHour: 1200 })).toBe(
      'From the crop sign (may)'
    )
    expect(describeRelayPermission({ role: null, cropPerHour: -5880 })).toBe(
      'From the crop sign (may not)'
    )
    // 0 is crop-neutral and the backend reads `>= 0`.
    expect(describeRelayPermission({ role: null, cropPerHour: 0 })).toBe(
      'From the crop sign (may)'
    )
  })

  it('does not guess a direction from a crop rate it could not read', () => {
    expect(describeRelayPermission({ role: null, cropPerHour: null })).toBe(
      'No crop rate read (may not)'
    )
    expect(describeRelayPermission({})).toBe('No crop rate read (may not)')
  })

  // The backend's FIRST rule, and the one this cell used to omit entirely:
  // `_may_relay_through` returns False on `crop_per_hour is None` BEFORE it
  // consults the village's own answer, its template or its role. So a village
  // whose rate could not be read was told "Role default (may)" while the
  // planner would never relay through it -- the optimistic reading of an
  // unparsed rate, in the one place this codebase has never taken it.
  it('refuses an unreadable rate before the role, the template or anything else', () => {
    expect(describeRelayPermission({ role: 'feeder', cropPerHour: null })).toBe(
      'No crop rate read (may not)'
    )
    expect(
      describeRelayPermission({ role: 'def', template: { may_relay: true }, cropPerHour: null })
    ).toBe('No crop rate read (may not)')
    expect(describeRelayPermission({ role: null, cropPerHour: undefined })).toBe(
      'No crop rate read (may not)'
    )
    // NaN is what `Number(unreadable)` produces, and it is not a rate either.
    expect(describeRelayPermission({ role: 'feeder', cropPerHour: NaN })).toBe(
      'No crop rate read (may not)'
    )
  })
})


// ─── Values this file keeps a second copy of ────────────────────────────────
//
// Each one is a Python constant restated in JavaScript because the wire cannot
// carry it: the page has to seed a box, bound an input or cap a control before
// any request is made. That is legitimate, and it is also how 25/60 came to sit
// on screen while the server measured against 30/80 -- a copy nobody asserted.
//
// So every copy is pinned to a LITERAL here, with its twin named by file and
// line. A literal is the only assertion that fails when the number moves;
// comparing the constant to itself is a tautology, and comparing it to an
// import of the same module is the same tautology with more steps. The backend
// agent adds the mirror pytest, so a change on either side breaks a test on
// that side.
//
// Cited by SYMBOL and never by line number, which `frontend/CLAUDE.md` already
// requires of the design-token references for the same reason: the first draft
// of this block gave lines, and the branch's own concurrent backend edits moved
// every one of them within the hour. A symbol is greppable and a line is not.
describe('the backend values this build keeps a second copy of', () => {
  // THE SHARPEST OF THEM. The page seeds its four merchant boxes from this and
  // then sends all four EXPLICITLY on every request, so a change to a backend
  // default is silently overridden by whatever this last said -- the other
  // copies at least fall back to the server's own value.
  it('DEFAULT_MERCHANT_MODEL matches the four levers the planner defaults to', () => {
    // base_capacity / bonus_per_to_level: `EUROPE2_TEUTON` in
    // src/travian_api/services/distribution/merchants.py, which is what
    // `PlanRequest.merchant_base_capacity` and
    // `PlanRequest.trade_office_bonus_per_level` default to.
    expect(DEFAULT_MERCHANT_MODEL.base_capacity).toBe(2500)
    expect(DEFAULT_MERCHANT_MODEL.bonus_per_to_level).toBe(0.2)
    // `DEFAULT_MERCHANT_RESERVE` in
    // src/travian_api/services/distribution/optimizer.py
    expect(DEFAULT_MERCHANT_MODEL.merchant_reserve).toBe(2)
    // `DEFAULT_MERCHANT_HEADROOM`, same file
    expect(DEFAULT_MERCHANT_MODEL.merchant_headroom).toBe(0.1)
    // Four levers, no more: merchant SPEED is tribe-derived server-side and
    // arrives in the snapshot, so a default for it here would be a guess about
    // a fact the account already states.
    expect(Object.keys(DEFAULT_MERCHANT_MODEL).sort()).toEqual([
      'base_capacity',
      'bonus_per_to_level',
      'merchant_headroom',
      'merchant_reserve',
    ])
  })

  // Twin: `DAILY_BEAT_CYCLES` in
  // src/travian_api/services/distribution/merchants.py.
  //
  // A closed set, not a range: Travian's "repeat every N hours" fans out into
  // 24/N daily rows, so only the divisors of 24 are expressible at all. A cycle
  // the create payload cannot express comes back from the game on some other
  // interval entirely.
  it('TRAVIAN_REPEAT_INTERVALS matches DAILY_BEAT_CYCLES', () => {
    expect([...TRAVIAN_REPEAT_INTERVALS]).toEqual([1, 2, 3, 4, 6, 8, 12, 24])
  })

  // Twin: the `le=0.95` on `stock_floor_fraction`, declared twice in
  // src/travian_api/web/routes/distribution.py -- on `VillageConfig` and on
  // the day check's own village model.
  //
  // Below 1 by a real margin, because a floor at the warehouse cap leaves NPC
  // nothing to convert out of and the village simply stops trading.
  it('MAX_STOCK_FLOOR_FRACTION matches the request bound', () => {
    expect(MAX_STOCK_FLOOR_FRACTION).toBe(0.95)
  })

  // Twin: `MAX_DAY_SEGMENTS` in
  // src/travian_api/web/routes/distribution.py, the `max_length` on
  // `DayCheckRequest.segments` and `ExecuteRequest.segments`.
  it('MAX_DAY_SEGMENTS matches the segment ceiling', () => {
    expect(MAX_DAY_SEGMENTS).toBe(12)
  })

  // Twin: the `default=24` on `ExecuteRequest.max_game_rows_per_run` in
  // src/travian_api/web/routes/distribution.py.
  //
  // A DEFAULT rather than a ceiling, and the sharpest kind: the box seeds
  // itself from this copy and the request sends the box, so the two halves
  // agreeing is the only thing that makes a blank box mean what it says. When
  // that default moved from 0 to 24 and this copy did not, the page went on
  // omitting the field for a blank box -- which used to mean "no limit" and now
  // means "24, the cap you just cleared".
  it('MAX_GAME_ROWS_PER_RUN matches the row budget the server defaults to', () => {
    expect(MAX_GAME_ROWS_PER_RUN).toBe(24)
  })

  // The two already pinned when this block was written, kept here so the whole
  // set reads in one place.
  it('MAX_TRADE_OFFICE_LEVEL and MAX_MERCHANTS_PER_VILLAGE match their bounds', () => {
    // `le=20` on `trade_office_level`, and Travian's own ceiling on merchants
    // in one village.
    expect(MAX_TRADE_OFFICE_LEVEL).toBe(20)
    expect(MAX_MERCHANTS_PER_VILLAGE).toBe(20)
  })

  // The fourth copy, which had neither a note nor a test until the night
  // profile landed: the boxes defaulted to 30/80 while the server measured
  // against 25/60, so the derivation aimed at one night and the report
  // described another.
  it('the fill pair matches night_profile.py', () => {
    expect(DEFAULT_BASELINE_FILL).toBe(0.25)
    expect(DEFAULT_TARGET_FILL).toBe(0.6)
  })
})


// The four merchant boxes are FILLED IN on a page nobody has touched -- the
// planner seeds them from its own defaults -- so `buildSetup` writes a
// `merchant_model` into every document and "the document carries one" cannot be
// read as "the operator typed one". The empty-document guard needs the
// difference, or it becomes unreachable.
describe('merchantModelIsCalibrated', () => {
  it('says no to the planner’s own figures', () => {
    expect(merchantModelIsCalibrated(DEFAULT_MERCHANT_MODEL)).toBe(false)
    expect(merchantModelIsCalibrated({ ...DEFAULT_MERCHANT_MODEL })).toBe(false)
    expect(merchantModelIsCalibrated({})).toBe(false)
    expect(merchantModelIsCalibrated(null)).toBe(false)
  })

  it('says no to a box the operator emptied', () => {
    expect(merchantModelIsCalibrated({ ...DEFAULT_MERCHANT_MODEL, base_capacity: '' })).toBe(false)
    expect(merchantModelIsCalibrated({ base_capacity: null })).toBe(false)
  })

  it('says yes to a lever that differs from the planner’s', () => {
    expect(merchantModelIsCalibrated({ ...DEFAULT_MERCHANT_MODEL, base_capacity: 2200 })).toBe(true)
    expect(merchantModelIsCalibrated({ merchant_reserve: 0 })).toBe(true)
  })

  // Neither exists in `DEFAULT_MERCHANT_MODEL`: travel speed is tribe-derived
  // and rides in the snapshot, and the span comes from the world. They are
  // overrides for a world that is not Europe 2, so any value is an assertion.
  it('says yes to a geometry override, which has no planner default at all', () => {
    expect(merchantModelIsCalibrated({ map_span: 801 })).toBe(true)
    expect(merchantModelIsCalibrated({ speed_fields_per_hour: 24 })).toBe(true)
  })

  // The string the page actually holds: every box is a text input.
  it('compares by value, not by type', () => {
    expect(merchantModelIsCalibrated({ base_capacity: '2500' })).toBe(false)
    expect(merchantModelIsCalibrated({ base_capacity: '2501' })).toBe(true)
  })
})
