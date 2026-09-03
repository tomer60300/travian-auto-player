import { describe, it, expect } from 'vitest'
import {
  SEVERITY_LABEL,
  SEVERITY_ORDER,
  SEVERITY_TONE,
  findingSections,
  groupDetails,
  initialExpanded,
  lossChip,
  planStatus,
  relayLegIndex,
} from './plannerFindings'

/** One critical group and one warning group, in the order the backend ranks
 *  them: whatever destroys resources first. */
const group = (overrides = {}) => ({
  key: 'overflow_structural:clay',
  category: 'overflow_structural',
  severity: 'critical',
  resource: 'clay',
  headline: '377,808/day of clay lost at the store cap in 17 villages',
  action: 'Give the clay somewhere to go.',
  count: 17,
  loss_per_day: 377808,
  findings: Array.from({ length: 17 }, (_, i) => ({
    detail: `${String(i + 3).padStart(2, '0')} — 22,224/day`,
    village: String(i + 3).padStart(2, '0'),
    loss_per_day: 22224,
  })),
  ...overrides,
})

const latency = group({
  key: 'latency:',
  category: 'latency',
  severity: 'warning',
  resource: null,
  headline: '23 routes miss the latency target',
  action: 'Geometry or the merchant budget may forbid better.',
  count: 23,
  loss_per_day: 0,
  findings: [{ detail: '18 -> 02 — 5.9h', village: '18', loss_per_day: 0 }],
})

const note = group({
  key: 'store_filling:clay',
  category: 'store_filling',
  severity: 'note',
  headline: '2 stores of clay fill up within the day',
  count: 2,
  loss_per_day: 0,
  findings: [
    { detail: '19 — full in 3.6h', village: '19', loss_per_day: 0 },
    { detail: '20 — full in 5.1h', village: '20', loss_per_day: 0 },
  ],
})

describe('findingSections', () => {
  it('reads worst first and never invents an empty section', () => {
    const sections = findingSections([latency, group(), note])

    expect(sections.map((s) => s.severity)).toEqual(['critical', 'warning', 'note'])
    expect(findingSections([latency]).map((s) => s.severity)).toEqual(['warning'])
    expect(findingSections([])).toEqual([])
  })

  it('leaves the backend ranking inside a section alone', () => {
    // The backend ranks by what each group costs per day. Re-sorting here would
    // let the view and the headline disagree about which loss is the big one.
    const cheap = group({ key: 'a', loss_per_day: 1 })
    const dear = group({ key: 'b', loss_per_day: 9_000_000 })

    const [critical] = findingSections([dear, cheap])

    expect(critical.groups.map((g) => g.key)).toEqual(['b', 'a'])
  })

  it('carries a name for every severity, not just a colour', () => {
    // Colour alone is not a severity: an operator who cannot tell amber from
    // red would get nothing at all from the panel.
    for (const severity of SEVERITY_ORDER) {
      expect(SEVERITY_LABEL[severity]).toBeTruthy()
      expect(SEVERITY_TONE[severity]).toMatch(/^text-/)
    }
  })

  it('survives a response with no groups at all', () => {
    expect(findingSections(undefined)).toEqual([])
  })
})

describe('initialExpanded', () => {
  it('opens what is costing resources and leaves the rest one click away', () => {
    // Both halves matter. Hiding every action recreates the original problem
    // one level down; opening every group recreates the flat list.
    const open = initialExpanded([group(), latency, note])

    expect(open.has('overflow_structural:clay')).toBe(true)
    expect(open.has('latency:')).toBe(false)
    expect(open.has('store_filling:clay')).toBe(false)
  })

  it('opens a warning that still costs resources per day', () => {
    const expensive = group({ key: 'x', severity: 'warning', loss_per_day: 5000 })

    expect(initialExpanded([expensive]).has('x')).toBe(true)
  })

  it('handles a plan with nothing wrong', () => {
    expect(initialExpanded([]).size).toBe(0)
    expect(initialExpanded(undefined).size).toBe(0)
  })
})

describe('lossChip', () => {
  it('prices a group that destroys resources', () => {
    expect(lossChip(377808)).toBe('377,808/day')
  })

  it('says nothing for a finding measured in hours rather than resources', () => {
    // "0/day" would read as "we measured the cost and it is zero" rather than
    // "cost per day does not apply to a latency target".
    expect(lossChip(0)).toBeNull()
    expect(lossChip(undefined)).toBeNull()
    expect(lossChip(-5)).toBeNull()
  })
})

describe('groupDetails', () => {
  it('lists a village per row once there is more than one', () => {
    expect(groupDetails(group())).toHaveLength(17)
  })

  it('does not repeat a lone finding that is already the headline', () => {
    // The backend uses a single finding's own message as the group headline, so
    // listing it again below says the same thing twice.
    const lone = group({ count: 1, findings: [{ detail: '02 — 1,795,200/day', village: '02' }] })

    expect(groupDetails(lone)).toEqual([])
  })

  it('drops findings with nothing distinguishing to show', () => {
    const blank = group({ count: 2, findings: [{ detail: '', village: '' }, { detail: 'x' }] })

    expect(groupDetails(blank)).toHaveLength(1)
  })
})

describe('planStatus', () => {
  const verdict = (over) => ({
    executable: true,
    clean: true,
    blockers: [],
    covers: ['a'],
    unweighed: [],
    critical_findings: 0,
    ...over,
  })

  it('has nothing to say before a plan exists', () => {
    expect(planStatus(null)).toBeNull()
  })

  it('is green only when the plan is executable AND clean', () => {
    const status = planStatus({ feasible: true, verdict: verdict() })

    expect(status.label).toBe('Ready to run')
    expect(status.tone).toBe('text-success')
  })

  it('does not go green on a plan that destroys resources', () => {
    // The defect this exists for: feasible=true with 2.4M/day of overflow used
    // to render the same green badge as a clean plan.
    const status = planStatus({
      feasible: true,
      verdict: verdict({ clean: false, unweighed: ['overflow_structural'], critical_findings: 3 }),
    })

    expect(status.label).toBe('Runs, not clean')
    expect(status.tone).toBe('text-warning')
  })

  it('is red when the sheet cannot be carried out', () => {
    const status = planStatus({
      feasible: false,
      verdict: verdict({ executable: false, clean: false, blockers: ['Capital is over budget'] }),
    })

    expect(status.label).toBe('Cannot run')
    expect(status.tone).toBe('text-danger')
  })

  it('follows the verdict, not the legacy boolean, when they disagree', () => {
    const status = planStatus({ feasible: true, verdict: verdict({ executable: false }) })

    expect(status.label).toBe('Cannot run')
  })

  it('says safety is unknown against a backend with no verdict', () => {
    // A freshly built bundle can meet the older API on :80 for as long as that
    // process has not been restarted -- which happened for real during this
    // work. Inventing "Ready to run" would claim something that API never said,
    // and so would any green label: without a verdict the second question (is
    // anything critical outstanding?) went unanswered. Saying so is the only
    // honest option.
    expect(planStatus({ feasible: true }).label).toBe('Executable; safety unknown')
    expect(planStatus({ feasible: true }).tone).not.toContain('success')
    expect(planStatus({ feasible: false }).label).toBe('Cannot run')
    expect(planStatus({ feasible: true }).verdict).toBeNull()
  })

  it('uses the diagnostics an old backend does send, rather than ignoring them', () => {
    // The skew window is exactly when a starvation or overflow diagnostic can
    // exist with no verdict to weigh it. Reading green over the top of one is
    // the failure this guards.
    const withCritical = planStatus({
      feasible: true,
      diagnostics: { counts: { critical: 3 } },
    })

    expect(withCritical.label).toBe('Runs, not clean')
    expect(withCritical.tone).toContain('warning')
  })
})

describe('relayLegIndex', () => {
  const relay = { hub: 2, hub_name: 'V02', origins: [22], destinations: [17] }

  it('indexes both legs, because either alone misleads', () => {
    const legs = relayLegIndex([relay])

    expect(legs.get('22:2').leg).toBe(1)
    expect(legs.get('2:17').leg).toBe(2)
    expect(legs.get('22:2').relay).toBe(relay)
  })

  it('marks every leg of a hub with several feeders and receivers', () => {
    // One hub, not four deliveries -- but all four ROWS still need marking, or
    // the operator types an unmarked one and never learns it is half a delivery.
    const fanned = { hub: 2, hub_name: 'V02', origins: [22, 23], destinations: [17, 18] }
    const legs = relayLegIndex([fanned])

    expect(legs.size).toBe(4)
    expect(legs.get('23:2').leg).toBe(1)
    expect(legs.get('2:18').leg).toBe(2)
  })

  it('leaves ordinary rows unmarked', () => {
    expect(relayLegIndex([relay]).get('5:6')).toBeUndefined()
  })

  it('copes with a response that carries no relays', () => {
    expect(relayLegIndex(undefined).size).toBe(0)
    expect(relayLegIndex([]).size).toBe(0)
  })
})

describe('SEVERITY_LABEL', () => {
  // Pinned, because the labels are what the operator reads the sections BY and
  // the set of kinds under each one keeps growing. Section 6, 7 and 9 added
  // seven, and four of the warnings are not targets: a drift flag under
  // "Missed targets" sends someone looking for a target to fix.
  it('describes what is actually under each heading', () => {
    expect(SEVERITY_LABEL.critical).toBe('Costing you now')
    expect(SEVERITY_LABEL.warning).toBe('Missed targets and stale assumptions')
    expect(SEVERITY_LABEL.note).toBe('Notes')
  })

  it('has a label for every severity the backend ranks', () => {
    for (const severity of SEVERITY_ORDER) {
      expect(SEVERITY_LABEL[severity], severity).toBeTruthy()
    }
  })
})
