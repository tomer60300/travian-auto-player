import ScrollableTable from './ScrollableTable'
import { RESOURCE_LABEL } from '../constants/planner'

const fmt = (n) => (n == null ? '—' : Math.round(n).toLocaleString())
const signed = (n) => (n == null ? '—' : `${n > 0 ? '+' : ''}${Math.round(n).toLocaleString()}`)

/** What the account had to give, per resource, and what is left over.
 *
 * `PlanResponse.unallocated` was computed on every plan and rendered nowhere,
 * so the only slack figure on screen was the Targets grid's own live
 * derivation -- which is the right thing while typing and is not what the plan
 * used. This is the plan's own answer, and where the two disagree this one is
 * the one that ships.
 *
 * The three quantities in the middle are kept in three columns on purpose:
 *
 *   * **production** is what the villages make;
 *   * **the NPC ceiling** is what could be converted -- and the account does
 *     NOT produce what its operator converts by hand, so adding it to
 *     production would inflate the total by a figure nothing may spend;
 *   * **the NPC draw** is what this plan actually converts.
 *
 * A resource whose ceiling is large and whose draw is zero is the normal case,
 * not a waste: conversion is consumed only against unmet demand.
 */
export default function UnallocatedPanel({ rows, villages, expanded }) {
  if (!rows.length) return null
  const anyNpc = rows.some((row) => (row.total_npc_allowance ?? 0) > 0)

  return (
    /* Folded, with the count in the summary. This is read-only -- it says what
       the plan already decided, not what is still to be decided -- and five
       panels of that kind used to sit at full weight between the verdict and
       the write path. A closed panel still has to say whether it is worth
       opening, which is what the count is for.

       `expanded` is the INITIAL state and nothing more: `<details>` toggles
       itself natively afterwards. The caller opens it when the plan is not
       clean, which is the rule `initialExpanded` already applies to the
       findings. */
    <details className="plan-readonly card p-4" open={expanded}>
      <summary className="cursor-pointer font-semibold mb-1">
        What the account had to give ({rows.length} resource{rows.length === 1 ? '' : 's'})
      </summary>
      <p className="text-secondary text-xs mb-3 mt-2">
        Per resource: what the villages produce, what the plan left unassigned, and which
        village absorbs it. Unassigned is production minus every target the plan committed —
        a negative figure means the targets ask for more than the account makes.
      </p>
      <ScrollableTable>
        <table className="w-full text-xs">
          <thead className="text-secondary uppercase">
            <tr>
              <th className="text-left py-1 px-2 sticky-col">Resource</th>
              <th className="text-right px-2">Production /h</th>
              {anyNpc && (
                <>
                  <th
                    className="text-right px-2"
                    title="A CEILING on NPC conversion into this resource, account-wide. Never spent by itself, and never production: the account does not produce what its operator converts by hand, so this is not added to the figure on its left."
                  >
                    NPC ceiling /h
                  </th>
                  <th
                    className="text-right px-2"
                    title="How much of that ceiling this plan actually converts into this resource. Consumed only against unmet demand, so zero against a large ceiling is the normal case."
                  >
                    NPC spent /h
                  </th>
                </>
              )}
              <th className="text-right px-2">Unassigned /h</th>
              <th className="text-left px-2" title="The one village per resource that absorbs the slack.">
                Rest
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const rest =
                row.remainder_village_id == null
                  ? null
                  : (villages.find((v) => v.village_id === row.remainder_village_id)?.name ??
                    `village ${row.remainder_village_id}`)
              const over = row.unallocated < -1
              return (
                <tr key={row.resource} className="row-focus touch-target border-t-default">
                  <td className="py-1.5 px-2 sticky-col row-focus-edge whitespace-nowrap">
                    {RESOURCE_LABEL[row.resource] ?? row.resource}
                  </td>
                  <td className="text-right px-2 font-mono">{fmt(row.total_production)}</td>
                  {anyNpc && (
                    <>
                      <td className="text-right px-2 font-mono text-secondary">
                        {fmt(row.total_npc_allowance)}
                      </td>
                      <td className="text-right px-2 font-mono">
                        {/* Said in words when it is nothing, for the reason the
                            NPC panel gives: a bare 0 beside a large ceiling
                            invites reading the ceiling as the real figure. */}
                        {(row.total_npc_draw ?? 0) >= 1 ? (
                          <span className="text-info">{fmt(row.total_npc_draw)}</span>
                        ) : (
                          <span className="text-secondary">none</span>
                        )}
                      </td>
                    </>
                  )}
                  <td
                    className={`text-right px-2 font-mono ${over ? 'text-danger' : 'text-secondary'}`}
                  >
                    {signed(row.unallocated)}
                    {/* Over-allocation named in words as well as coloured:
                        severity is never carried by colour alone. */}
                    {over && <span className="block text-[10px] uppercase">over-allocated</span>}
                  </td>
                  <td className="px-2">
                    {rest ?? <span className="text-secondary">no remainder village</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </ScrollableTable>
    </details>
  )
}
