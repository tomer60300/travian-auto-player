/**
 * The five panels this round added, rendered.
 *
 * All five live inside a planner stage that only exists once a snapshot has
 * arrived, so `pagesRender.test.jsx` -- which renders every page with no
 * effects and therefore no snapshot -- cannot reach a line of any of them.
 * That is the same reason `RoleTemplates.test.jsx` exists, and the same reason
 * these are components rather than inline JSX: three white-screen crashes have
 * escaped through exactly that gap.
 *
 * `renderToString` again: no browser, no new dependency, and it evaluates
 * exactly the code that turns a page white. What it cannot see is a click or a
 * change event -- those are driven in `e2e/` -- so what is asserted here is
 * what each panel SAYS. And what they say is the point, because every one of
 * them exists to keep two quantities from being read as each other:
 *
 *   * an NPC allowance is a CEILING and a draw is what was spent;
 *   * a `fill` is a fraction on the wire and a percentage on screen, shown
 *     against the threshold it missed;
 *   * `projected` is not the present tense;
 *   * "derived" is an ANSWER, and 404 is not an empty document.
 */

import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import DayNightPanel from './DayNightPanel'
import FullDayCheck from './FullDayCheck'
import NightOverrunTable from './NightOverrunTable'
import NpcBalancePanel from './NpcBalancePanel'
import PlanExport from './PlanExport'
import RevertRunPanel from './RevertRunPanel'
import SetupStorage from './SetupStorage'
import UnallocatedPanel from './UnallocatedPanel'

// react-dom/server separates adjacent text nodes with an empty HTML comment,
// so a rendered sentence can carry `<!-- -->` inside it. Stripped, the same way
// `RoleTemplates.test.jsx` does, so an assertion is about the words on screen.
const text = (element) =>
  renderToString(element)
    .replace(/<!--\s*-->/g, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;|&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/&mdash;|&#x2014;/g, '—')
    .replace(/\s+/g, ' ')

const OVERRUN = {
  origin: 2,
  origin_name: '02',
  destination: 11,
  destination_name: '11',
  cycle_hours: 4,
  last_dispatch_minute: 360,
  last_dispatch_clock: '06:00',
  round_trip_minutes: 108,
  overrun_minutes: 48,
}

describe('DayNightPanel', () => {
  const props = {
    profileNames: ['Day', 'Night'],
    activeProfile: 'Day',
    profileWindows: { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] },
    profileAttendance: { Day: true, Night: false },
    attendanceRequired: true,
    reservedWindow: null,
    onWindow: () => {},
    onAttendance: () => {},
    onReservedWindow: () => {},
    onSelectProfile: () => {},
  }

  it('shows both profiles with their opposite answers, which is the whole point', () => {
    const out = text(<DayNightPanel {...props} />)
    expect(out).toContain('you are at the marketplace')
    expect(out).toContain('nobody is trading')
    // Each window's share of the day, so the pair is comparable at a glance.
    expect(out).toContain('16h')
    expect(out).toContain('8h')
  })

  it('names every profile that owes an answer', () => {
    const out = text(<DayNightPanel {...props} profileAttendance={{ Day: true }} />)
    expect(out).toContain('Night')
    expect(out).toContain('the plan is refused until this says')
  })

  // A night shift is a real thing, so this is a warning and not a refusal --
  // but it is the shape of a forgotten answer, and the consequence is a night
  // route funded by trading nobody did.
  it('questions a profile that claims to be awake through the small hours', () => {
    const out = text(
      <DayNightPanel {...props} profileAttendance={{ Day: true, Night: true }} />
    )
    expect(out).toContain('runs through the small hours and says you are trading')
  })

  it('asks nothing of an account with no stock floor', () => {
    const out = text(<DayNightPanel {...props} attendanceRequired={false} />)
    expect(out).toContain('No village keeps an NPC-backed stock floor')
  })

  it('says whether the marketplace hours are reserved', () => {
    expect(text(<DayNightPanel {...props} />)).toContain('Nothing reserved')
    expect(
      text(<DayNightPanel {...props} reservedWindow={['20:00', '21:00']} />)
    ).toContain('Arrivals avoid 20:00–21:00')
  })
})

describe('FullDayCheck', () => {
  const dayCheck = {
    villages: [],
    warnings: [],
    morning_floor: 0.6,
    pre_night_baseline: 0.25,
    morning_shortfalls: [
      {
        village_id: 11,
        village_name: '11',
        resource: 'clay',
        store: 'warehouse',
        stock: 168_000,
        capacity: 400_000,
        fill: 0.42,
      },
    ],
    pre_night_over_baseline: [],
    night_overruns: [],
  }

  it('reads a fill as a percentage, against the threshold it missed', () => {
    const out = text(
      <FullDayCheck
        dayCheck={dayCheck}
        dayChecking={false}
        onRun={() => {}}
        cropCeilings={{}}
        villages={[]}
      />
    )
    // 0.42 on the wire, 42% on screen. A renderer printing it raw would show
    // 0% for every row.
    expect(out).toContain('42%')
    expect(out).toContain('threshold 60%')
    expect(out).toContain('threshold 25%')
    expect(out).toContain('72,000 short of the floor')
  })

  it('says a threshold was met rather than showing an empty section', () => {
    const out = text(
      <FullDayCheck
        dayCheck={{ ...dayCheck, morning_shortfalls: [] }}
        dayChecking={false}
        onRun={() => {}}
        cropCeilings={{}}
        villages={[]}
      />
    )
    expect(out).toContain('Every role village reaches the morning floor')
  })

  it('says nothing has been run yet, rather than rendering a blank card', () => {
    const out = text(
      <FullDayCheck
        dayCheck={null}
        dayChecking={false}
        onRun={() => {}}
        cropCeilings={{}}
        villages={[]}
      />
    )
    expect(out).toContain('Not run yet')
  })
})

describe('NightOverrunTable', () => {
  it('shows the arithmetic rather than the verdict', () => {
    const out = text(<NightOverrunTable rows={[OVERRUN]} emptyNote="clear" />)
    expect(out).toContain('02 → 11')
    expect(out).toContain('06:00')
    expect(out).toContain('1.8h')
    expect(out).toContain('48m late')
  })

  it("says the road is clear in the caller's own words", () => {
    const out = text(<NightOverrunTable rows={[]} emptyNote="Every movement closes." />)
    expect(out).toContain('Every movement closes.')
  })
})

describe('NpcBalancePanel', () => {
  const reserves = [
    {
      village_id: 2,
      village_name: '02',
      floor_level: 120_000,
      allowance_per_day: 528_000,
      allowance_per_hour: 22_000,
      feedstock: ['clay', 'crop'],
      feedstock_shares: [0.6, 0.4],
      drawn: ['lumber'],
    },
    {
      village_id: 11,
      village_name: '11',
      floor_level: 40_000,
      allowance_per_day: 216_000,
      allowance_per_hour: 9000,
      feedstock: ['crop'],
      feedstock_shares: [1],
      drawn: [],
    },
  ]

  it('renders nothing for an account that declares no floor', () => {
    expect(renderToString(
      <NpcBalancePanel reserves={[]} triggers={[]} drawByVillage={new Map()} />
    )).toBe('')
  })

  it('keeps the ceiling and the draw apart, in separate words', () => {
    const out = text(
      <NpcBalancePanel
        reserves={reserves}
        triggers={[]}
        drawByVillage={new Map([[2, 15_000]])}
      />
    )
    expect(out).toContain('22,000/h')
    expect(out).toContain('528,000/day')
    expect(out).toContain('15,000/h')
    expect(out).toContain('Clay 60%')
    // The village that drew nothing says so, rather than showing its 9,000/h
    // ceiling in the spent column.
    expect(out).toContain('nothing drawn')
    expect(out).toContain('the floor funded no cargo')
  })

  it('distinguishes a trigger that is true now from one a day of this plan causes', () => {
    const out = text(
      <NpcBalancePanel
        reserves={[]}
        drawByVillage={new Map()}
        triggers={[
          {
            village_id: 2,
            village_name: '02',
            kind: 'wood_low',
            resource: 'lumber',
            level: 95_000,
            threshold: 120_000,
            projected: false,
          },
          {
            village_id: 2,
            village_name: '02',
            kind: 'crop_banked',
            resource: 'crop',
            level: 742_000,
            threshold: 700_000,
            projected: true,
          },
        ]}
      />
    )
    expect(out).toContain('true now')
    expect(out).toContain('after a day of this plan')
    expect(out).toContain('nothing here presses the NPC button')
  })
})

describe('UnallocatedPanel', () => {
  const rows = [
    {
      resource: 'lumber',
      total_production: 121_000,
      total_npc_allowance: 22_000,
      total_npc_draw: 15_000,
      unallocated: 3000,
      remainder_village_id: 11,
    },
    {
      resource: 'clay',
      total_production: 12_000,
      total_npc_allowance: 9000,
      total_npc_draw: 0,
      unallocated: -4200,
      remainder_village_id: null,
    },
  ]

  it('renders nothing without a plan to report on', () => {
    expect(renderToString(<UnallocatedPanel rows={[]} villages={[]} />)).toBe('')
  })

  it('keeps production, the ceiling and the draw in three columns', () => {
    const out = text(
      <UnallocatedPanel rows={rows} villages={[{ village_id: 11, name: '11' }]} />
    )
    expect(out).toContain('NPC ceiling /h')
    expect(out).toContain('NPC spent /h')
    expect(out).toContain('121,000')
    expect(out).toContain('22,000')
    expect(out).toContain('15,000')
    // A ceiling with nothing spent reads as a word, not as a 0 beside it.
    expect(out).toContain('none')
  })

  it('names an over-allocation in words as well as in colour', () => {
    const out = text(<UnallocatedPanel rows={rows} villages={[]} />)
    expect(out).toContain('-4,200')
    expect(out).toContain('over-allocated')
    expect(out).toContain('no remainder village')
  })

  // Section 7 applies to some accounts and not others; an account with no
  // floor should not be shown two empty columns about it.
  it('drops the two NPC columns for an account that converts nothing', () => {
    const out = text(
      <UnallocatedPanel
        rows={[{ ...rows[0], total_npc_allowance: 0, total_npc_draw: 0 }]}
        villages={[]}
      />
    )
    expect(out).not.toContain('NPC ceiling')
  })
})

describe('PlanExport', () => {
  const digest = 'ab12cd34ef56'.repeat(5) + 'abcd'

  it('shows the twelve characters the file will be named after', () => {
    const out = text(
      <PlanExport
        digest={digest}
        exporting={false}
        conflict={null}
        onConfirm={() => {}}
        onRePlan={() => {}}
      />
    )
    expect(out).toContain('ab12cd34ef56')
    expect(out).toContain('the same twelve the file is named after')
  })

  it('says the plan moved, keeps the server sentence, and offers the way out', () => {
    const out = text(
      <PlanExport
        digest={digest}
        exporting={false}
        conflict={'re-plans to ff00 and the confirmation names ab12'}
        onConfirm={() => {}}
        onRePlan={() => {}}
      />
    )
    expect(out).toContain('The plan moved since you read it')
    // Both digests, because "it moved" without saying from what to what is not
    // something anyone can check.
    expect(out).toContain('re-plans to ff00 and the confirmation names ab12')
    expect(out).toContain('Re-read the plan')
  })
})

describe('SetupStorage', () => {
  const props = {
    busy: null,
    onSave: () => {},
    onLoad: () => {},
    onForget: () => {},
    onExportFile: () => {},
    onImportFile: () => {},
    onPaste: () => {},
    pasteOpen: false,
  }

  // The distinction the store exists to preserve, and the one a naive panel
  // collapses: "you have never saved" invites importing a file, while "you
  // saved a blank sheet" is a decision to leave the account undescribed.
  it('reads nothing-saved as an invitation', () => {
    const out = text(<SetupStorage {...props} status={{ state: 'none' }} />)
    expect(out).toContain('Nothing is saved on the server for this account yet')
  })

  it('reads a saved document as saved, with when', () => {
    const out = text(
      <SetupStorage {...props} status={{ state: 'saved', savedAt: '2026-09-03T09:15:00Z' }} />
    )
    expect(out).toContain('A setup is saved on the server')
  })

  it('says a failed check is only the check', () => {
    const out = text(
      <SetupStorage {...props} status={{ state: 'error', message: 'the request failed' }} />
    )
    expect(out).toContain('could not say whether a setup is saved')
    expect(out).toContain('The buttons still work')
  })

  it('offers the file path beside the server one', () => {
    const out = text(<SetupStorage {...props} status={{ state: 'none' }} />)
    expect(out).toContain('Save setup to file')
    expect(out).toContain('Load setup from file')
    expect(out).toContain('0 Travian requests')
  })
})

// ── RevertRunPanel ───────────────────────────────────────────────────
//
// Behind two gates `pagesRender.test.jsx` cannot pass -- a snapshot AND a
// recorded live run -- and it is the panel whose whole job is to say what a
// button did NOT do. `must_delete_by_hand` is the half no button covers, so
// what is asserted here is that it is said, and said first.

const REVERT = {
  trace_id: 'abc123def456',
  steps: ['village 53629: delete route 9001'],
  created: { 53629: [9001] },
  disabled_now: { 53629: [9001] },
  deleted_now: {},
  must_delete_by_hand: { 53629: [9001] },
  restore_state: { 53629: ['route 8800 -> enabled'] },
  clean: false,
  requests_used: 4,
  problems: [],
}

describe('RevertRunPanel', () => {
  const VS = [{ village_id: 53629, name: '02' }]
  const noop = () => {}
  const panel = (props) =>
    text(
      <RevertRunPanel
        traceId="abc123def456"
        state={null}
        villages={VS}
        onCheck={noop}
        onDisable={noop}
        onDelete={noop}
        {...props}
      />
    )
  const answered = (result) => ({
    state: { traceId: 'abc123def456', busy: false, result, error: null },
  })

  it('offers only the read-only check before anything has been read', () => {
    const out = panel({})

    expect(out).toContain('Check what undoing this would take')
    // Reverting is deliberately not a single button: neither write is offered
    // until the read-only answer has said what there is to undo.
    expect(out).not.toContain('Disable those routes now')
    expect(out).not.toContain('Delete those routes for good')
  })

  it('prices the check, because it is not free', () => {
    // Every origin the run touched costs two game requests to re-read, and
    // every other action on this page states its cost before spending it.
    expect(panel({})).toMatch(/2 requests per village the run touched/)
  })

  it('leads with what a human has to remove, even after a successful disable', () => {
    // A successful disable is exactly when the outstanding rows get forgotten:
    // the routes have stopped shipping, so nothing on screen feels urgent.
    const out = panel(answered(REVERT))

    expect(out).toMatch(/1 route\(s\) must be deleted by hand/)
    expect(out).toContain('02: 9001')
    expect(out.indexOf('must be deleted by hand')).toBeLessThan(out.indexOf('Disabled just now'))
    // Named by name, not by id: nobody running the account knows which village
    // 53629 is.
    expect(out).toContain('02: route 8800 -&gt; enabled')
    expect(out).toMatch(/4 game request\(s\) spent/)
  })

  it('says the delete disables too, rather than pretending to be narrower', () => {
    const out = panel(answered(REVERT))

    expect(out).toContain('Disable those routes now (reversible)')
    expect(out).toContain('Delete those routes for good (disables first)')
  })

  it('offers no writes when there is nothing to undo', () => {
    const out = panel(
      answered({ ...REVERT, clean: true, must_delete_by_hand: {}, disabled_now: {} })
    )

    expect(out).toContain('every village reads as it did before this run')
    expect(out).not.toContain('Delete those routes for good')
  })

  it('renders one answer under one heading only', () => {
    // Keyed by trace id. A history row's steps under another run's heading
    // would be the worst possible wrong reading in this panel.
    const out = text(
      <RevertRunPanel
        traceId="zzz999zzz999"
        state={{ traceId: 'abc123def456', busy: false, result: REVERT, error: null }}
        villages={VS}
        onCheck={noop}
        onDisable={noop}
        onDelete={noop}
      />
    )

    expect(out).not.toContain('must be deleted by hand')
  })

  it('shows a refusal where the operator asked, not as a toast', () => {
    const out = panel({
      state: {
        traceId: 'abc123def456',
        busy: false,
        result: null,
        error: 'No trace for run abc123def456.',
      },
    })

    expect(out).toContain('No trace for run abc123def456.')
  })
})
