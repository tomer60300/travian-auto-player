import {
  CONSUMABLE_RESOURCES,
  VILLAGE_ROLES,
  isConsumptionRate,
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
 * A pure function of its props, deliberately. It belongs to the planner's
 * Allocate stage, which only renders once a snapshot has arrived, and
 * `renderToString` never gets there: it runs no effects, so there is no
 * snapshot and no villages. As inline JSX none of this had render coverage, in
 * a page whose own render test exists because three white-screen crashes
 * escaped exactly that way. `roleCounts` is data rather than a callback into
 * the page for the same reason.
 */
export default function RoleTemplates({
  templates,
  roleCounts,
  missingTemplates,
  onAllocation,
  onSpend,
  onPatch,
  onClear,
}) {
  return (
    <div className="card p-3">
      <details className="text-xs">
        <summary className="cursor-pointer pointer-coarse:min-h-11 flex items-center flex-wrap gap-x-2">
          <span className="text-primary font-semibold">Role templates</span>
          <span className="text-secondary">
            one profile per kind of village &mdash; {Object.keys(templates).length}{' '}
            typed, covering{' '}
            {VILLAGE_ROLES.reduce(
              (n, role) => n + (templates[role] ? (roleCounts[role] ?? 0) : 0),
              0
            )}{' '}
            village(s)
          </span>
        </summary>
        <p className="text-secondary mt-2 max-w-3xl">
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
        <div className="overflow-x-auto mt-2">
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
                    {RESOURCES.map((resource) => {
                      const alloc = template?.allocations?.[resource] ?? {
                        mode: 'keep',
                        value: 0,
                      }
                      return (
                        <td key={resource} className="px-2">
                          <div className="flex items-center gap-1">
                            <select
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
        </div>
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
