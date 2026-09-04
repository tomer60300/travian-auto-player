import ScrollableTable from './ScrollableTable'
import { RESOURCE_LABEL } from '../constants/planner'

const fmt = (n) => (n == null ? '—' : Math.round(n).toLocaleString())

/** What section 7's NPC balancing came to, and when to press the button.
 *
 * Everything here is DERIVED by the planner, not declared: the operator states
 * a `stock_floor_fraction` and an attendance, and these are what those became
 * once the first pass said what each village retains. All of it was computed
 * and none of it was rendered.
 *
 * The one rule this panel exists to obey: **the allowance is a ceiling, not a
 * supply.** It is what a village retains of the resources it is not shipping,
 * and nothing is obliged to use any of it -- a floor on a village that needs
 * nothing draws zero. So the allowance and the draw are separate columns with
 * separate words, the draw is what the row leads with when there is one, and a
 * village that converted nothing says "nothing drawn" rather than showing its
 * allowance as though it had been spent. `plannerAllocation.js` reads the draw
 * for exactly this reason, and this panel must not contradict it.
 *
 * The triggers are ADVICE and are labelled as such: the planner does not press
 * the NPC button, it says when the operator should. `projected` is never
 * blurred into the present tense -- "true now" and "true after a day of this
 * plan" are different claims, and only one of them is something to act on
 * before the plan has run.
 */
export default function NpcBalancePanel({ reserves, triggers, drawByVillage, expanded }) {
  if (reserves.length === 0 && triggers.length === 0) return null

  return (
    /* Folded with its two counts in the summary, on `UnallocatedPanel`'s
       reasoning: read-only, and a closed panel still has to say whether it is
       worth opening. The trigger count is also one of the two figures the stage
       now leads with, so a closed panel here does not hide it. */
    <details className="plan-readonly card p-4" open={expanded}>
      <summary className="cursor-pointer font-semibold mb-1">
        NPC balancing ({reserves.length} buffered, {triggers.length} to do by hand)
      </summary>
      <p className="text-secondary text-xs mb-3 mt-2">
        The buffer each village keeps by hand, what its own retention could convert, and
        which stores pay for it. The planner never presses the button — it sizes what the
        button could do and says when it is worth doing.
      </p>

      {reserves.length > 0 && (
        <ScrollableTable>
          <table className="w-full text-xs">
            <thead className="text-secondary uppercase">
              <tr>
                <th className="text-left py-1 px-2 sticky-col">Village</th>
                <th
                  className="text-right px-2"
                  title="The buffer in resources: your stock floor times the warehouse capacity. Applies to lumber, clay and iron; a granary has no floor because it is not NPC-fed. Also this account's reading of section 7's 'wood is low' trigger."
                >
                  Buffer
                </th>
                <th
                  className="text-right px-2"
                  title="A CEILING, not a supply. What this village retains per hour of the resources it is not shipping, which is all NPC has to exchange — it converts 1:1 inside one village and cannot create resources. Nothing is obliged to use any of it."
                >
                  Could convert
                </th>
                <th
                  className="text-right px-2"
                  title="How much of that ceiling this plan actually spends. Consumed only against unmet demand, so a floor on a village that needs nothing reads zero here."
                >
                  This plan spends
                </th>
                <th
                  className="text-left px-2"
                  title="Which stores pay for the conversion, 1:1, and each one's share of it — proportional to the retention that sized the allowance, so the store funding most of it is debited most."
                >
                  Paid for by
                </th>
                <th
                  className="text-left px-2"
                  title="Materials this village must ship beyond its own production, so is converting INTO. The complement of the feedstock."
                >
                  Converted into
                </th>
              </tr>
            </thead>
            <tbody>
              {reserves.map((reserve) => {
                const drew = drawByVillage.get(reserve.village_id) ?? 0
                return (
                  <tr
                    key={reserve.village_id}
                    className="row-focus touch-target border-t-default"
                  >
                    <td className="py-1.5 px-2 sticky-col row-focus-edge whitespace-nowrap">
                      {reserve.village_name || reserve.village_id}
                    </td>
                    <td className="text-right px-2 font-mono">{fmt(reserve.floor_level)}</td>
                    <td className="text-right px-2 font-mono text-secondary">
                      {fmt(reserve.allowance_per_hour)}/h
                      <span className="block text-[10px]">{fmt(reserve.allowance_per_day)}/day</span>
                    </td>
                    <td className="text-right px-2 font-mono">
                      {/* Zero is said in words, not printed as "0/h" beside a
                          large allowance: the two figures are different
                          quantities, and a bare zero next to a ceiling invites
                          reading the ceiling as the real number. */}
                      {drew >= 1 ? (
                        <span className="text-info">{fmt(drew)}/h</span>
                      ) : (
                        <span className="text-secondary">nothing drawn</span>
                      )}
                    </td>
                    <td className="px-2">
                      {reserve.feedstock.length === 0 ? (
                        <span className="text-secondary">—</span>
                      ) : (
                        <span className="whitespace-nowrap">
                          {reserve.feedstock
                            .map((resource, i) => {
                              const share = reserve.feedstock_shares?.[i]
                              const label = RESOURCE_LABEL[resource] ?? resource
                              return share == null
                                ? label
                                : `${label} ${Math.round(share * 100)}%`
                            })
                            .join(' · ')}
                        </span>
                      )}
                    </td>
                    <td className="px-2">
                      {reserve.drawn.length === 0 ? (
                        // Not a defect and not a waste: a floor that funded
                        // nothing cost the account nothing.
                        <span className="text-secondary">nothing — the floor funded no cargo</span>
                      ) : (
                        reserve.drawn.map((r) => RESOURCE_LABEL[r] ?? r).join(', ')
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </ScrollableTable>
      )}

      {triggers.length > 0 && (
        <div className="mt-3 border-t-default pt-3">
          <p className="text-xs font-semibold">
            When to trade{' '}
            <span className="text-secondary font-normal">
              · {triggers.length} trigger{triggers.length === 1 ? '' : 's'} fired
            </span>
          </p>
          <ul className="mt-1 space-y-1">
            {triggers.map((trigger, i) => (
              <li key={`${trigger.village_id}-${trigger.kind}-${i}`} className="text-xs">
                <span className="font-medium">
                  {trigger.village_name || trigger.village_id}
                </span>{' '}
                <span className={trigger.kind === 'wood_low' ? 'text-warning' : 'text-info'}>
                  {trigger.kind === 'wood_low'
                    ? `${RESOURCE_LABEL[trigger.resource] ?? trigger.resource} is at or below your floor`
                    : `${RESOURCE_LABEL[trigger.resource] ?? trigger.resource} is banked past the trigger`}
                </span>
                <span className="text-secondary font-mono">
                  {' '}
                  — {fmt(trigger.level)} against {fmt(trigger.threshold)}
                </span>
                {/* Never blurred into the present tense. "True now" is
                    something to act on before the plan runs; "true after a day
                    of this plan" is a consequence of running it. */}
                <span
                  className={`ml-1 text-[10px] uppercase ${
                    trigger.projected ? 'text-secondary' : 'text-warning'
                  }`}
                >
                  {trigger.projected ? 'after a day of this plan' : 'true now'}
                </span>
              </li>
            ))}
          </ul>
          <p className="text-secondary text-[11px] mt-1">
            Advice, not an action: nothing here presses the NPC button. A wood trigger says
            the buffer these routes ship out of is gone; a crop one says there is feedstock
            standing idle.
          </p>
        </div>
      )}
    </details>
  )
}
