/**
 * The Role-templates panel, rendered.
 *
 * It is the surface where profile section 2.1's "one profile, four defensive
 * villages" is actually typed, and it lives in a stage that only exists once a
 * snapshot has arrived -- so `pagesRender.test.jsx`, which renders every page
 * with no effects and therefore no snapshot, cannot reach a line of it. That is
 * the whole reason it is a component rather than inline JSX.
 *
 * `renderToString` again: no browser, no new dependency, and it evaluates
 * exactly the code that turns a page white. What it cannot see is a click or a
 * change event, so what is asserted here is what the panel SAYS -- every role
 * present, the figures shown, crop offered as a target and never as a spend,
 * the relay default spelled out per role, and the warning that names a role
 * whose villages have no profile. The merge rule itself is pure and lives in
 * utils/plannerSetup.js with its own tests.
 */

import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import RoleTemplates from './RoleTemplates'
import { VILLAGE_ROLES } from '../utils/plannerSetup'
import { ROLE_LABEL } from '../constants/planner'

const DEF_TEMPLATE = {
  allocations: {
    lumber: { mode: 'absolute', value: 8372 },
    clay: { mode: 'absolute', value: 5168 },
    iron: { mode: 'absolute', value: 5809 },
    crop: { mode: 'absolute', value: 2200 },
  },
  consumption: { lumber: 8372, clay: 5168, iron: 5809 },
  may_relay: null,
  crop_negative_by_design: false,
}

const NO_COUNTS = Object.fromEntries(VILLAGE_ROLES.map((role) => [role, 0]))

// react-dom/server separates adjacent text nodes with an empty HTML comment,
// so `{label} target` arrives as "Crop<!-- --> target". Those markers are an
// artifact of hydration, not of the copy, and asserting around them would pin
// React's rendering strategy instead of what the panel says.
function render(props = {}) {
  return renderToString(
    <RoleTemplates
      templates={{}}
      roleCounts={NO_COUNTS}
      missingTemplates={[]}
      focusRole={null}
      focusSeq={0}
      onAllocation={() => {}}
      onSpend={() => {}}
      onPatch={() => {}}
      onClear={() => {}}
      {...props}
    />
  ).replaceAll('<!-- -->', '')
}

describe('RoleTemplates', () => {
  it('renders a row for every one of the five roles', () => {
    // Assignment is exhaustive in the profile, so the panel has to offer all
    // five whether the account uses them or not -- a role with no row is a
    // village that cannot be described.
    const html = render()
    for (const role of VILLAGE_ROLES) {
      expect(html, `no row for ${role}`).toContain(ROLE_LABEL[role])
    }
  })

  it('renders with nothing typed at all', () => {
    // The empty state is the one every account starts in, and the one a page
    // render test would otherwise be the first to exercise -- in production.
    expect(render()).toContain('no villages')
  })

  it('shows how many villages a typed template is standing in for', () => {
    // The claim of a template is "one profile, four villages", so the count is
    // the number that says whether it is doing that.
    const html = render({
      templates: { def: DEF_TEMPLATE },
      roleCounts: { ...NO_COUNTS, def: 4 },
    })
    expect(html).toContain('4 villages')
    expect(html).toContain('covering')
  })

  it('shows each figure of a typed profile', () => {
    const html = render({
      templates: { def: DEF_TEMPLATE },
      roleCounts: { ...NO_COUNTS, def: 4 },
    })
    for (const figure of ['8372', '5168', '5809', '2200']) {
      expect(html, `${figure} is not on screen`).toContain(figure)
    }
  })

  it('offers crop as a target and never as a spend', () => {
    // The P1 ruling, as a countable fact rather than a comment: four target
    // columns, three spend columns. A crop spend would 422 the plan, and
    // section 2 lists a crop figure per role village -- so the box has to be
    // absent, not merely warned about.
    const html = render({ templates: { def: DEF_TEMPLATE } })
    expect(html).toContain('Crop target')
    expect(html).not.toContain('Crop spend')
    expect(html.match(/spend\/h/g)).toHaveLength(3)
  })

  it('spells out what each role default resolves to', () => {
    // Section 5.9 in the one place the operator can act on it: a feeder may,
    // and nothing else may -- the capital included, which is the half people
    // get wrong because it is the hub everything ships to.
    const html = render()
    expect(html.match(/Role default \(may\)/g)).toHaveLength(1)
    expect(html.match(/Role default \(may not\)/g)).toHaveLength(VILLAGE_ROLES.length - 1)
  })

  it('names a role whose villages have no template', () => {
    // The backend refuses exactly this, so naming it here is the difference
    // between a 422 the operator can act on and one they have to decode.
    const html = render({ roleCounts: { ...NO_COUNTS, def: 4 }, missingTemplates: ['def'] })
    expect(html).toContain('has villages')
    expect(html).toContain('will refuse it')
    expect(html).toContain(ROLE_LABEL.def)
  })

  // The subject is a LIST, and it read "DEF, Feeder has villages". Counted
  // rather than pinned to one form, because one missing role is the commonest
  // case and "DEF have villages" is the same defect the other way round.
  it('agrees with the number of roles it is naming', () => {
    expect(
      render({ roleCounts: { ...NO_COUNTS, def: 4 }, missingTemplates: ['def'] })
    ).toContain('has villages')
    const two = render({
      roleCounts: { ...NO_COUNTS, def: 4, feeder: 2 },
      missingTemplates: ['def', 'feeder'],
    })
    expect(two).toContain('have villages')
    expect(two).not.toContain('has villages')
  })

  // The remedy exists and works -- every village claiming an untyped role has a
  // "Type the … figures" button that jumps into this panel with the caret in
  // the row -- and the warning did not mention it, so the operator was told
  // about a problem and left to find the fix.
  it('names the button that fixes it, and where that button is', () => {
    const html = render({ roleCounts: { ...NO_COUNTS, def: 4 }, missingTemplates: ['def'] })
    expect(html).toContain('Type the … figures')
    expect(html).toContain('Account')
  })

  it('says nothing about missing templates when none are missing', () => {
    expect(render({ templates: { def: DEF_TEMPLATE } })).not.toContain('will refuse it')
  })

  it('arrives already OPEN when the page has sent the operator to a role', () => {
    // The one thing about the jump that `renderToString` CAN see, and it is the
    // half that has to be true on the FIRST render rather than in an effect: a
    // closed <details> skips its subtree, so `focus()` into it is a no-op and
    // `ScrollableTable` would measure a table of zero width -- losing the
    // pinned Role column and the scroll hint. Both are measured for real in
    // e2e/roleTemplates.pw.js; this pins the render that makes them possible.
    expect(render()).not.toMatch(/<details[^>]*\bopen\b/)
    expect(render({ focusRole: 'troops_off', focusSeq: 1 })).toMatch(/<details[^>]*\bopen\b/)
  })

  it('offers Clear only for a role that has a template to clear', () => {
    expect(render()).not.toContain('Clear')
    expect(render({ templates: { def: DEF_TEMPLATE } })).toContain('Clear')
  })

  it('shows a crop-negative-by-design template as checked', () => {
    const html = render({
      templates: { full_off: { ...DEF_TEMPLATE, crop_negative_by_design: true } },
    })
    expect(html).toContain('checked')
  })

  it('offers the override in both directions on every role', () => {
    // Three choices per role: take the role's answer, may, may not. Both
    // overrides have to be reachable -- the account that wants a defensive
    // village relaying and the account that wants its feeders left out of the
    // tier are the same feature read from either end.
    //
    // Which one is CURRENTLY chosen is deliberately not asserted: a select's
    // selection is a DOM property, and `renderToString` emits the same option
    // markup whatever the value, so a test claiming to see it would be reading
    // React's rendering strategy rather than the panel.
    const html = render({ templates: { def: { ...DEF_TEMPLATE, may_relay: true } } })
    expect(html.match(/May relay/g)).toHaveLength(VILLAGE_ROLES.length)
    expect(html.match(/May not relay/g)).toHaveLength(VILLAGE_ROLES.length)
  })
})
