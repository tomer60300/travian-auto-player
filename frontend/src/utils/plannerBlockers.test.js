import { describe, expect, it } from 'vitest'

import {
  describeBlockers,
  nightBlockers,
  planBlockers,
  runBlockers,
  MERCHANT_MODEL_LABELS,
} from './plannerBlockers'

const CAPITAL = 20002
const DEF_A = 20011

const village = (id, name, merchants = 19) => ({
  village_id: id,
  name,
  merchants_total: merchants,
  merchants_free: merchants,
  lumber_per_hour: 8372,
  clay_per_hour: 5168,
  iron_per_hour: 5809,
  crop_per_hour: 2200,
})

const VILLAGES = [village(CAPITAL, '02'), village(DEF_A, '11')]

describe('planBlockers', () => {
  it('says nothing about an account with nothing typed', () => {
    expect(planBlockers({ villages: VILLAGES })).toEqual([])
  })

  it('is empty for no arguments at all, so the gate cannot refuse a bare page', () => {
    expect(planBlockers()).toEqual([])
  })

  // The reported case: 99 into a 19-merchant village. The cell says "only 19
  // merchants here" and the button beside it posted `max_busy_merchants: 99`.
  it('refuses a merchant ceiling past the village fleet, naming the village', () => {
    const blockers = planBlockers({ villages: VILLAGES, maxBusy: { [DEF_A]: 99 } })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Most merchants busy at once',
      stage: 'snapshot',
      villages: ['11'],
      focusLabel: 'Most merchants busy at once for 11',
    })
  })

  it('refuses a ceiling outside 0-20 as well as one past the fleet', () => {
    const blockers = planBlockers({
      villages: VILLAGES,
      maxBusy: { [CAPITAL]: 25, [DEF_A]: 4.5 },
    })
    expect(blockers).toHaveLength(1)
    expect(blockers[0].villages).toEqual(['02', '11'])
  })

  it('accepts a ceiling the village can actually reach', () => {
    expect(planBlockers({ villages: VILLAGES, maxBusy: { [DEF_A]: 19 } })).toEqual([])
  })

  it('refuses a stock floor past the 95% ceiling', () => {
    const blockers = planBlockers({ villages: VILLAGES, stockFloors: { [CAPITAL]: 0.99 } })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Stock floor %',
      villages: ['02'],
      focusLabel: 'NPC-backed stock floor for 02, percent of warehouse',
    })
  })

  it('refuses a negative spend, naming the resource in the caret target', () => {
    const blockers = planBlockers({
      villages: VILLAGES,
      consumption: { [DEF_A]: { lumber: -5 } },
    })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Consumption /h',
      villages: ['11'],
      focusLabel: 'Lumber spent per hour by 11',
    })
  })

  // The figure is typed once, on the Targets stage, and outlined on every
  // village that claims the role -- so it has to be refused on every one.
  it('refuses a template spend on the villages that inherit it', () => {
    const blockers = planBlockers({
      villages: VILLAGES,
      villageRoles: { [CAPITAL]: 'def', [DEF_A]: 'def' },
      roleTemplates: { def: { consumption: { clay: -1 } } },
    })
    const account = blockers.find((b) => b.field === 'Consumption /h')
    expect(account.villages).toEqual(['02', '11'])
    const targets = blockers.find((b) => b.stage === 'allocate')
    expect(targets).toMatchObject({
      rule: '0 or more',
      focusLabel: 'Clay spent per hour by a DEF village',
    })
  })

  it('refuses a role whose template was never typed', () => {
    const blockers = planBlockers({
      villages: VILLAGES,
      villageRoles: { [DEF_A]: 'def' },
    })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Role',
      villages: ['11'],
      focusLabel: 'Role for 11',
    })
  })

  it('accepts a role with a figure in its template', () => {
    expect(
      planBlockers({
        villages: VILLAGES,
        villageRoles: { [DEF_A]: 'def' },
        roleTemplates: { def: { consumption: { lumber: 8372 } } },
      })
    ).toEqual([])
  })

  // Not a 422 on the backend: the unknown name is silently DROPPED, so the
  // operator believes a village is excluded while the next run draws on it.
  it('refuses an excluded origin no village answers to', () => {
    const blockers = planBlockers({
      villages: VILLAGES,
      foreignTargets: [{ name: 'ally', exclude_origins_text: '2, 11' }],
    })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Not from',
      villages: ['2'],
      focusLabel: 'Foreign target 1 excluded origins',
    })
  })

  it('accepts an exclusion that resolves, and a target with none typed', () => {
    expect(
      planBlockers({
        villages: VILLAGES,
        foreignTargets: [{ exclude_origins_text: '02' }, { exclude_origins: [DEF_A] }],
      })
    ).toEqual([])
  })

  // The two cases the page marked nowhere at all before this: the backend
  // refuses an even span and a reserve past 20, and neither had a message.
  it('refuses an even map span, which the backend will not take', () => {
    const blockers = planBlockers({ villages: VILLAGES, merchantModel: { map_span: 400 } })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Map span',
      rule: 'odd — a world is centred on 0|0',
      focusLabel: MERCHANT_MODEL_LABELS.map_span,
    })
  })

  it('accepts the odd span every real world has', () => {
    expect(planBlockers({ villages: VILLAGES, merchantModel: { map_span: 401 } })).toEqual([])
  })

  it('refuses a merchant reserve the file parser would refuse, at the same bound', () => {
    const blockers = planBlockers({
      villages: VILLAGES,
      merchantModel: { merchant_reserve: 25 },
    })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Reserve / village',
      rule: '0 to 20 whole merchants',
      focusLabel: MERCHANT_MODEL_LABELS.merchant_reserve,
    })
  })

  it('refuses headroom of a whole budget, and accepts the fraction the box types', () => {
    expect(
      planBlockers({ villages: VILLAGES, merchantModel: { merchant_headroom: 1.5 } })
    ).toHaveLength(1)
    expect(
      planBlockers({ villages: VILLAGES, merchantModel: { merchant_headroom: 0.1 } })
    ).toEqual([])
  })

  it('leaves an emptied override alone: absent is the planner default, not a figure', () => {
    expect(
      planBlockers({
        villages: VILLAGES,
        merchantModel: {
          base_capacity: undefined,
          merchant_reserve: undefined,
          map_span: undefined,
        },
      })
    ).toEqual([])
  })

  it('reads the Account table left to right, then the Targets stage', () => {
    const blockers = planBlockers({
      villages: VILLAGES,
      villageRoles: { [CAPITAL]: 'def' },
      roleTemplates: { def: { assumed_crop_per_hour: Number.NaN } },
      maxBusy: { [DEF_A]: 99 },
      stockFloors: { [CAPITAL]: 0.99 },
      merchantModel: { map_span: 400 },
    })
    expect(blockers.map((b) => b.field)).toEqual([
      'Most merchants busy at once',
      'Stock floor %',
      'Map span',
      'DEF assumed net crop',
    ])
  })
})

describe('describeBlockers', () => {
  it('names one field, its villages and its rule', () => {
    expect(
      describeBlockers([
        {
          field: 'Most merchants busy at once',
          rule: '0 to 20, and no more than the village fields',
          villages: ['11'],
        },
      ])
    ).toBe(
      'One figure on this page is outside what its field accepts, so nothing was sent: ' +
        'Most merchants busy at once (11) — 0 to 20, and no more than the village fields.'
    )
  })

  it('counts fields, and drops the bracket for an account-wide one', () => {
    expect(
      describeBlockers([
        { field: 'Stock floor %', rule: '0–95%', villages: ['02'] },
        { field: 'Map span', rule: 'odd', villages: [] },
      ])
    ).toBe(
      '2 figures on this page are outside what their fields accept, so nothing was sent: ' +
        'Stock floor % (02) — 0–95%; Map span — odd.'
    )
  })

  // The lead used to say "outside what THE PLAN accepts", which was true of the
  // only list that existed when it was written. It now renders three, and two of
  // them are not plan inputs at all: the controlled-run boxes ride on `/execute`
  // and the fill pair rides on `/night-profile`. An operator who pasted 51 into
  // "Routes this run" was told their figure was outside what the plan accepts —
  // about a box `Build plan` deliberately never reads.
  it('is true of the run-control boxes, which are not plan inputs', () => {
    expect(describeBlockers(runBlockers({ routesPerRun: '51' }))).toBe(
      'One figure on this page is outside what its field accepts, so nothing was sent: ' +
        'Routes this run — 0 to 50.'
    )
  })

  it('is true of the night-fill pair, which reaches /night-profile alone', () => {
    expect(describeBlockers(nightBlockers({ baselineFill: '99', targetFill: '101' }))).toBe(
      '2 figures on this page are outside what their fields accept, so nothing was sent: ' +
        'Emptied to % — 0 to 95%; Full to % — above 0% and up to 100%.'
    )
  })

  // The one shape that is neither a bound nor a range: an entry the server
  // cannot parse. "Outside what its field accepts" has to cover that too, since
  // `Never disable` reports the ENTRIES rather than village names.
  it('is true of a malformed Never disable entry', () => {
    expect(describeBlockers(runBlockers({ protectDestinations: '46|abc' }))).toBe(
      'One figure on this page is outside what its field accepts, so nothing was sent: ' +
        'Never disable (46|abc) — a village id, or coordinates like 46|133.'
    )
  })
})


// ─── Boxes whose only bound was the spinner ─────────────────────────────────
//
// `min`/`max` on a number input bound the SPINNER and nothing else: a typed or
// pasted figure sails past both, `aria-invalid` stays null, and the request
// goes out to be refused as a server 422 -- which names a pydantic field path
// and arrives after a round trip. Browser-confirmed for the Trade Office box:
// typing 21 posted `trade_office_level: 21`.
describe('the boxes that only had a spinner bound', () => {
  it('refuses a Trade Office level past 20, naming the village', () => {
    const blockers = planBlockers({ villages: VILLAGES, tradeOffice: { [DEF_A]: 21 } })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Trade Office',
      stage: 'snapshot',
      villages: ['11'],
      focusLabel: 'Trade Office level for 11',
    })
  })

  it('refuses a negative or fractional Trade Office level too', () => {
    expect(
      planBlockers({ villages: VILLAGES, tradeOffice: { [CAPITAL]: -1, [DEF_A]: 3.5 } })[0]
        .villages
    ).toEqual(['02', '11'])
  })

  it('accepts 0 and 20, which are both real answers', () => {
    expect(
      planBlockers({ villages: VILLAGES, tradeOffice: { [CAPITAL]: 0, [DEF_A]: 20 } })
    ).toEqual([])
  })

  // The same asymmetry the merchant-model comment says was fixed: `parseSetup`
  // has refused a negative ceiling since it was written, and the box did not.
  it('refuses a negative crop stock alert, on the stage that mounts it', () => {
    const blockers = planBlockers({ villages: VILLAGES, cropCeilings: { [CAPITAL]: -5 } })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Crop stock alert',
      stage: 'day',
      villages: ['02'],
      focusLabel: 'Crop stock alert level for 02',
    })
  })

  // 0 is the answer "tell me when this store is empty", which is exactly the
  // village whose alert matters most.
  it('accepts a crop alert of 0', () => {
    expect(planBlockers({ villages: VILLAGES, cropCeilings: { [CAPITAL]: 0 } })).toEqual([])
  })

  it('refuses a foreign-target safety margin outside 0-100', () => {
    const blockers = planBlockers({
      villages: VILLAGES,
      foreignTargets: [
        { name: 'Ally', x: 1, y: 2, crop_per_hour: 100, safety_margin_pct: 150 },
      ],
    })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Margin %',
      stage: 'snapshot',
      villages: ['Ally'],
      focusLabel: 'Foreign target 1 safety margin',
    })
  })

  it('leaves a blank margin alone, because blank is the 0 the backend defaults to', () => {
    expect(
      planBlockers({
        villages: VILLAGES,
        foreignTargets: [{ name: 'Ally', x: 1, y: 2, crop_per_hour: 100, safety_margin_pct: '' }],
      })
    ).toEqual([])
  })
})

describe('runBlockers', () => {
  it('says nothing about an untouched run panel', () => {
    expect(runBlockers()).toEqual([])
    expect(runBlockers({ routesPerRun: '3', maxGameRows: '24', protectDestinations: '' })).toEqual(
      []
    )
  })

  it('refuses more than 50 routes in one run', () => {
    const blockers = runBlockers({ routesPerRun: '51' })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Routes this run',
      stage: 'plan',
      villages: [],
      focusLabel: 'Routes this run',
    })
  })

  // 0 is "reconcile only", documented by the backend and offered by the box.
  it('keeps 0 meaning 0', () => {
    expect(runBlockers({ routesPerRun: '0' })).toEqual([])
    expect(runBlockers({ routesPerRun: '50' })).toEqual([])
  })

  // Blank falls back to the page default, which is what the box's own copy says.
  it('leaves a blank route cap alone', () => {
    expect(runBlockers({ routesPerRun: '' })).toEqual([])
  })

  it('refuses more than 2000 game rows in one run', () => {
    const blockers = runBlockers({ maxGameRows: '2001' })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Max rows this run',
      stage: 'plan',
      focusLabel: 'Max rows this run',
    })
  })

  it('keeps blank and 0 meaning no limit', () => {
    expect(runBlockers({ maxGameRows: '' })).toEqual([])
    expect(runBlockers({ maxGameRows: '0' })).toEqual([])
    expect(runBlockers({ maxGameRows: '2000' })).toEqual([])
  })

  // The SHAPE, which is all the server can check -- and it 422s rather than
  // dropping, because an entry that protects nothing while looking like it does
  // is how a hand-made route gets switched off on the very next run.
  it('refuses a protected entry that is neither a village id nor coordinates', () => {
    const blockers = runBlockers({ protectDestinations: '53629, ally hub, 46|133' })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Never disable',
      stage: 'plan',
      villages: ['ally hub'],
      focusLabel: 'Never disable',
    })
  })

  it('accepts ids, negative coordinates and stray whitespace', () => {
    expect(runBlockers({ protectDestinations: ' 53629 , -46|133 , 12|-8 ,, ' })).toEqual([])
  })

  it('refuses 0 as an id and a half-typed coordinate', () => {
    expect(runBlockers({ protectDestinations: '0' })[0].villages).toEqual(['0'])
    expect(runBlockers({ protectDestinations: '46|' })[0].villages).toEqual(['46|'])
  })
})

describe('nightBlockers', () => {
  it('says nothing about the pair the boxes start on', () => {
    expect(nightBlockers({ baselineFill: 25, targetFill: 60 })).toEqual([])
  })

  // Blank is refused by `buildNightProfile` in its own words -- "a blank box is
  // not 0%" -- so it is not a bound violation here.
  it('leaves a blank box to the blank-box refusal', () => {
    expect(nightBlockers({ baselineFill: '', targetFill: '' })).toEqual([])
  })

  it('refuses a baseline past 95%', () => {
    const blockers = nightBlockers({ baselineFill: '96', targetFill: '99' })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({
      field: 'Emptied to %',
      stage: 'day',
      focusLabel: 'Emptied to %',
    })
  })

  it('refuses a target of 0 or past 100%', () => {
    expect(nightBlockers({ baselineFill: '0', targetFill: '0' })[0].field).toBe('Full to %')
    expect(nightBlockers({ baselineFill: '25', targetFill: '101' })[0].field).toBe('Full to %')
  })

  // The backend's own `_target_is_above_baseline`: equal leaves no room for
  // anything to arrive in.
  it('refuses a target that is not above the baseline', () => {
    const blockers = nightBlockers({ baselineFill: '60', targetFill: '60' })
    expect(blockers).toHaveLength(1)
    expect(blockers[0]).toMatchObject({ field: 'Full to %', focusLabel: 'Full to %' })
    expect(nightBlockers({ baselineFill: '60', targetFill: '25' })).toHaveLength(1)
  })
})
