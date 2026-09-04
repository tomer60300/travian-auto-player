import { describe, expect, it } from 'vitest'

import { describeBlockers, planBlockers, MERCHANT_MODEL_LABELS } from './plannerBlockers'

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
      'One figure on this page is outside what the plan accepts, so nothing was sent: ' +
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
      '2 figures on this page are outside what the plan accepts, so nothing was sent: ' +
        'Stock floor % (02) — 0–95%; Map span — odd.'
    )
  })
})
