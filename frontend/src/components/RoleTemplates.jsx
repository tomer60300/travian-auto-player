import { useEffect, useRef, useState } from 'react'

import ScrollableTable from './ScrollableTable'
import {
  CONSUMABLE_RESOURCES,
  VILLAGE_ROLES,
  isAssumedCropRate,
  isConsumptionRate,
  isEmptyTemplate,
} from '../utils/plannerSetup'
import {
  MODES,
  RESOURCES,
  RESOURCE_LABEL,
  ROLE_LABEL,
  roleRelaysByDefault,
} from '../constants/planner'

/** One allocation profile per kind of village, edited in one place.
 *
 * Profile section 2.1 gives ONE consumption profile for FOUR defensive
 * villages. Typed per village that is four copies of the same four numbers and
 * four chances for them to drift apart, and the operator maintains those
 * villages as one thing -- so this is where they are typed once.
 *
 * Data in, callbacks out, and one effect. It belongs to the planner's
 * Allocate stage, which only renders once a snapshot has arrived, and
 * `renderToString` never gets there: it runs no effects, so there is no
 * snapshot and no villages. As inline JSX none of this had render coverage, in
 * a page whose own render test exists because three white-screen crashes
 * escaped exactly that way. `roleCounts` is data rather than a callback into
 * the page for the same reason.
 *
 * `focusRole` / `focusSeq` are the panel's OTHER door. The Snapshot row's "no
 * DEF template yet" named a problem whose only remedy was on another stage,
 * behind this disclosure, in a 1839px table -- so the page sends the operator
 * here by role, and the row takes the caret. `focusSeq` rises on every press
 * of that control rather than the role alone changing, because pressing it
 * twice for the same role must land twice: the state it would compare is
 * already what it wants.
 */
export default function RoleTemplates({
  templates,
  roleCounts,
  missingTemplates,
  focusRole,
  focusSeq,
  onAllocation,
  onSpend,
  onPatch,
  onClear,
}) {
  const detailsRef = useRef(null)
  const focusRef = useRef(null)
  // OPEN FROM THE FIRST RENDER when the page has sent the operator to a role,
  // not from the effect below, and this is the load-bearing half of the whole
  // feature. A closed `<details>` skips its subtree: measured at 1440 in
  // e2e/roleTemplates.pw.js, the DEF Lumber mode select has a 135x47 box --
  // so a zero-rect check would call it reachable -- while
  // `checkVisibility()` is false and `el.focus()` leaves
  // `document.activeElement` untouched. Opening in an effect would therefore
  // focus nothing on the frame it ran, and `ScrollableTable`'s own layout
  // effect would have measured a skipped table (client == scroll == 0), losing
  // the pinned Role column and the scroll hint until a ResizeObserver
  // delivery a frame later shifted the table down to make room for the hint.
  //
  // Frozen at mount on purpose: the jump always crosses a stage, which mounts
  // this component, so the arrival state is known on the first render. React
  // never diffs a prop whose value it has not changed, so the operator can
  // still close the panel from its own summary and it stays closed.
  const [openOnArrival] = useState(focusRole != null)

  useEffect(() => {
    if (focusRole == null) return
    const details = detailsRef.current
    const target = focusRef.current
    if (details == null || target == null) return
    // The disclosure FIRST, and imperatively, so this holds even on the path
    // `openOnArrival` cannot cover -- a jump arriving at an already-mounted
    // panel the operator had closed. React is not tracking `open` (the prop
    // never changes), so it will not write it back.
    details.open = true
    // `preventScroll` and then an explicit scroll: focus()'s own scrolling
    // brings an element barely into view at the bottom edge, and this row is
    // one of five in a table the operator has to read across.
    target.focus({ preventScroll: true })
    target.scrollIntoView({ block: 'center', inline: 'nearest' })
  }, [focusRole, focusSeq])

  return (
    <div className="card p-3">
      <details ref={detailsRef} open={openOnArrival} className="text-xs">
        <summary className="cursor-pointer pointer-coarse:min-h-11 flex items-center flex-wrap gap-x-2">
          <span className="text-primary font-semibold">Role templates</span>
          {/* Counted on `isEmptyTemplate`, the same predicate the request and
              the warning below use. On `Object.keys` alone a role emptied box
              by box read "1 typed" while the warning called it missing, so the
              panel contradicted itself in two lines. */}
          <span className="text-secondary">
            one profile per kind of village &mdash;{' '}
            {VILLAGE_ROLES.filter((role) => !isEmptyTemplate(templates[role])).length}{' '}
            typed, covering{' '}
            {VILLAGE_ROLES.reduce(
              (n, role) => n + (isEmptyTemplate(templates[role]) ? 0 : (roleCounts[role] ?? 0)),
              0
            )}{' '}
            village(s)
          </span>
        </summary>
        <p className="text-secondary mt-2 mb-2 max-w-3xl">
          A village takes its role&apos;s figures for everything it does not state itself,
          per resource &mdash; so overriding one village&apos;s lumber leaves its clay and
          iron on the profile. An override is marked in the{' '}
          <span className="text-primary">Edit by resource</span> grid rather than applied in
          silence. Crop has no spend box here for the same reason it has none on a village:
          the snapshot&apos;s crop rate is already net of upkeep, so say what the role
          should KEEP with its crop target. There is no Rest here either &mdash; exactly
          one village per resource absorbs the slack, which a profile shared by four
          villages cannot say, so it stays the Rest radio in{' '}
          <span className="text-primary">Edit by resource</span>.
        </p>
        {/* 1839px of columns — the widest table in the app, and it overflows
            its container by 717px even at 1440. The role name is pinned so a
            spend figure typed into row four is attributable to row four, and
            the hint names what is off to the right; it had neither before,
            because `.sticky-col` was gated on `max-width: 640px` and this
            panel never carried a hint at all.

            The gap above is the intro paragraph's `mb-2` rather than a `mt-2`
            on the scroll container, because the hint renders BETWEEN them:
            on the container it left the hint flush under the intro text with
            the whole gap below, reading as a caption for the block above it
            instead of a label for the table below. */}
        <ScrollableTable>
          <table className="w-full text-xs">
            <thead className="text-secondary uppercase">
              <tr>
                <th className="text-left py-1 px-2 sticky-col">Role</th>
                {RESOURCES.map((resource) => (
                  <th key={resource} className="text-left px-2">
                    {RESOURCE_LABEL[resource]} target
                  </th>
                ))}
                {CONSUMABLE_RESOURCES.map((resource) => (
                  <th key={resource} className="text-right px-2">
                    {RESOURCE_LABEL[resource]} spend/h
                  </th>
                ))}
                <th
                  className="text-right px-2"
                  title="What you BELIEVE a village of this role nets in crop per hour — your own reading, kept as a flat constant. It moves no target, no cargo and no merchant: the only thing it can do is raise a warning when reality has drifted more than 20% away from it, which is how a hand-maintained profile is caught going stale. Negative is normal on the roles that matter: the Hammer eats more crop than it grows by design. 0 is a real claim (this village breaks even) and is checked as one. Leave it blank to make no assumption, which is not an assumption of zero — the village is simply not checked."
                >
                  Assumed crop/h
                </th>
                <th
                  className="text-left px-2"
                  title="Whether the planner may route someone else's cargo through these villages. Profile section 5.9: only a feeder relays, the capital included -- it is the hub every feeder ships to and hands off to a relay tier rather than carrying a leg in transit. Override it only for the account whose defensive village sits on the only road to a corner of the map."
                >
                  Relay
                </th>
                <th
                  className="text-left px-2"
                  title="These villages eat more crop than they grow, on purpose (profile sections 9.1-9.2: the Hammer and the troops-only village). Their granary countdown is then reported as a note rather than a critical -- the same rate and the same hours of cover, without the claim that something has gone wrong."
                >
                  Crop-negative by design
                </th>
                <th className="px-2" />
              </tr>
            </thead>
            <tbody>
              {VILLAGE_ROLES.map((role) => {
                const template = templates[role]
                const claiming = (roleCounts[role] ?? 0)
                return (
                  <tr
                    key={role}
                    className="row-focus touch-target border-t-default transition-colors"
                  >
                    <td className="py-1 px-2 sticky-col whitespace-nowrap row-focus-edge">
                      {ROLE_LABEL[role]}
                      <span className="block text-secondary">
                        {claiming === 0
                          ? 'no villages'
                          : claiming + (claiming === 1 ? ' village' : ' villages')}
                      </span>
                    </td>
                    {RESOURCES.map((resource, column) => {
                      const alloc = template?.allocations?.[resource] ?? {
                        mode: 'keep',
                        value: 0,
                      }
                      return (
                        <td key={resource} className="px-2">
                          <div className="flex items-center gap-1">
                            <select
                              // Where a jump lands: the first control of the
                              // row, which is also the first thing the
                              // operator has to answer -- the value box beside
                              // it is disabled until a mode is chosen, because
                              // keep is the absence of a target. Its own
                              // accessible name names the role, so a screen
                              // reader announces which row the caret arrived
                              // in without the row needing to say it again.
                              ref={role === focusRole && column === 0 ? focusRef : null}
                              aria-label={ROLE_LABEL[role] + ' ' + RESOURCE_LABEL[resource] + ' mode'}
                              className="input-field w-auto text-xs py-0.5"
                              value={alloc.mode}
                              onChange={(e) =>
                                onAllocation(role, resource, {
                                  mode: e.target.value,
                                })
                              }
                            >
                              {MODES.map((m) => (
                                <option key={m.value} value={m.value}>
                                  {m.label}
                                </option>
                              ))}
                            </select>
                            <input
                              type="number"
                              aria-label={ROLE_LABEL[role] + ' ' + RESOURCE_LABEL[resource] + ' value'}
                              className="input-field w-24 text-right text-xs py-0.5"
                              disabled={alloc.mode === 'keep'}
                              value={alloc.value ?? 0}
                              onChange={(e) =>
                                onAllocation(role, resource, {
                                  value: Number(e.target.value),
                                })
                              }
                            />
                          </div>
                        </td>
                      )
                    })}
                    {CONSUMABLE_RESOURCES.map((resource) => {
                      const rate = template?.consumption?.[resource]
                      const bad = rate != null && !isConsumptionRate(rate)
                      const problem = 'template-spend-' + role + '-' + resource
                      return (
                        <td key={resource} className="text-right px-2">
                          <input
                            type="number"
                            min="0"
                            step="1"
                            aria-label={RESOURCE_LABEL[resource] + ' spent per hour by a ' + ROLE_LABEL[role] + ' village'}
                            aria-invalid={bad || undefined}
                            aria-describedby={bad ? problem : undefined}
                            placeholder="none"
                            className="input-field w-24 text-right text-xs py-0.5"
                            value={rate ?? ''}
                            onChange={(e) => onSpend(role, resource, e.target.value)}
                          />
                          {bad && (
                            <span id={problem} className="block text-warning">
                              0 or more
                            </span>
                          )}
                        </td>
                      )
                    })}
                    <td className="text-right px-2">
                      {/* Section 9's staleness detector, and the one figure in
                          this table that CHANGES NOTHING. The row says so, in
                          the cell rather than only in the header tooltip: every
                          other box here moves cargo, so an operator reading
                          across would reasonably assume this one does too and
                          type a target into it.

                          A number input rather than a positive-only one, and
                          with no `min`: -5,880 is the right value for the
                          Hammer, so a non-negative bound would refuse the
                          account's own figure. An emptied box DELETES the
                          assumption rather than storing 0 -- zero is the claim
                          "this village breaks even" and is checked as one,
                          while nothing at all means the village is not checked. */}
                      {(() => {
                        const assumed = template?.assumed_crop_per_hour
                        const bad = assumed != null && !isAssumedCropRate(assumed)
                        const problem = 'template-assumed-crop-' + role
                        return (
                          <>
                            <input
                              type="number"
                              step="1"
                              aria-label={'Assumed net crop per hour for a ' + ROLE_LABEL[role] + ' village'}
                              aria-invalid={bad || undefined}
                              aria-describedby={bad ? problem : undefined}
                              placeholder="no assumption"
                              className="input-field w-28 text-right text-xs py-0.5"
                              value={assumed ?? ''}
                              onChange={(e) =>
                                onPatch(role, {
                                  assumed_crop_per_hour:
                                    e.target.value === '' ? null : Number(e.target.value),
                                })
                              }
                            />
                            {bad ? (
                              <span id={problem} className="block text-warning">
                                a number, or blank for no assumption
                              </span>
                            ) : (
                              <span className="block text-secondary">
                                {assumed == null ? 'not checked' : 'checked, ships nothing'}
                              </span>
                            )}
                          </>
                        )
                      })()}
                    </td>
                    <td className="px-2">
                      <select
                        aria-label={'Whether a ' + ROLE_LABEL[role] + ' village may relay'}
                        className="input-field w-auto text-xs py-0.5"
                        value={
                          template?.may_relay == null
                            ? ''
                            : template.may_relay
                              ? 'yes'
                              : 'no'
                        }
                        onChange={(e) =>
                          onPatch(role, {
                            may_relay:
                              e.target.value === '' ? null : e.target.value === 'yes',
                          })
                        }
                      >
                        <option value="">
                          Role default ({roleRelaysByDefault(role) ? 'may' : 'may not'})
                        </option>
                        <option value="yes">May relay</option>
                        <option value="no">May not relay</option>
                      </select>
                    </td>
                    <td className="px-2">
                      <label className="flex items-center gap-2 whitespace-nowrap">
                        <input
                          type="checkbox"
                          aria-label={'A ' + ROLE_LABEL[role] + ' village is crop-negative by design'}
                          checked={Boolean(template?.crop_negative_by_design)}
                          onChange={(e) =>
                            onPatch(role, {
                              crop_negative_by_design: e.target.checked,
                            })
                          }
                        />
                        <span className="text-secondary">by design</span>
                      </label>
                    </td>
                    <td className="px-2 text-right">
                      {template != null && (
                        <button
                          type="button"
                          className="underline text-secondary hover:text-primary pointer-coarse:min-h-11"
                          onClick={() => onClear(role)}
                        >
                          Clear
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </ScrollableTable>
        <p className="text-secondary mt-2 max-w-3xl">
          <span className="text-primary">Assumed crop/h ships nothing.</span> Section 9 calls
          these profiles flat constants and expects drift between manual updates, so this is
          the figure the planner compares the snapshot&apos;s own net crop against — over 20%
          apart and it raises a warning. What a role should KEEP of its crop is its crop
          target above; what it spends is refused outright, because the snapshot&apos;s crop
          rate is already net of upkeep.
        </p>
        {/* Named in words as well as flagged in the row: the backend
            refuses a role whose template never arrived, and an operator
            who has collapsed this panel needs to know which role it is
            waiting on. */}
        {missingTemplates.length > 0 && (
          <p className="text-warning mt-2">
            {'\u26A0 '}
            {missingTemplates.map((role) => ROLE_LABEL[role]).join(', ')} has villages
            but no template, so the plan will refuse it rather than read those villages as
            keeping their own production.
          </p>
        )}
      </details>
    </div>
  )
}
