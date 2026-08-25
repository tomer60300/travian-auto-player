import { useMemo, useState } from 'react'
import {
  findingSections,
  groupDetails,
  initialExpanded,
  lossChip,
} from '../utils/plannerFindings'

const RESOURCE_LABEL = { lumber: 'Lumber', clay: 'Clay', iron: 'Iron', crop: 'Crop' }

const fmt = (n) => Math.round(n).toLocaleString()

/** What the plan found, in the order a person should read it.
 *
 * This replaced a flat bulleted list of every warning. On a real 25-village
 * account that list was 153 lines: 51 of them the same systemic fact repeated
 * per village with an identical number, 51 more a second description of those
 * same stores, and a 1.9M/day crop loss — forty times the next biggest —
 * sitting in the middle, visually identical to a 22,224/day one. There was no
 * total anywhere. The operator's verdict was "I won't read all", and they were
 * right: an unread warning is worth exactly nothing.
 *
 * So the hierarchy is deliberate and the levels are not interchangeable:
 *
 *   1. one sentence, and the total, for someone who reads nothing else;
 *   2. the groups that cost resources, each with the one action that fixes it;
 *   3. per-village detail and the merely-informational notes, one click away.
 *
 * The backend does the ranking, grouping and totalling (see
 * `services/distribution/findings.py`) — this decides only what is on screen
 * before anything is clicked.
 */
function PlanDiagnostics({ diagnostics, lineCount }) {
  const sections = useMemo(() => findingSections(diagnostics.groups), [diagnostics.groups])
  const defaults = useMemo(() => initialExpanded(diagnostics.groups), [diagnostics.groups])
  // Only the operator's own toggles are stored, so a group that appears in a
  // later re-plan still gets its default rather than inheriting a stale one.
  const [toggled, setToggled] = useState(() => new Set())
  const isOpen = (key) => (toggled.has(key) ? !defaults.has(key) : defaults.has(key))
  const toggle = (key) =>
    setToggled((previous) => {
      const next = new Set(previous)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const [showNotes, setShowNotes] = useState(false)
  const noteCount = sections
    .filter((s) => s.severity === 'note')
    .reduce((total, s) => total + s.groups.length, 0)
  const visible = sections.filter((s) => s.severity !== 'note' || showNotes)
  const groupCount = diagnostics.groups.length

  return (
    <div className="card p-4">
      <h3 className="font-semibold mb-1">What this plan costs</h3>
      <p className="text-sm">{diagnostics.headline}</p>

      {diagnostics.total_loss_per_day > 0 && (
        <div className="mt-3 flex items-baseline gap-2 flex-wrap">
          <span className="text-2xl font-bold text-danger">
            {fmt(diagnostics.total_loss_per_day)}
          </span>
          <span className="text-xs text-secondary">resources destroyed per day</span>
        </div>
      )}

      {diagnostics.loss_by_resource.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {diagnostics.loss_by_resource.map((loss) => (
            <span key={loss.resource} className="status-badge status-badge-error">
              {RESOURCE_LABEL[loss.resource] ?? loss.resource} {fmt(loss.per_day)}/day
            </span>
          ))}
        </div>
      )}

      <p className="text-xs text-secondary mt-3">
        {groupCount} {groupCount === 1 ? 'thing' : 'things'} to look at, from {lineCount}{' '}
        individual {lineCount === 1 ? 'finding' : 'findings'}
      </p>

      {visible.map((section) => (
        <div key={section.severity} className="mt-3">
          <p className={`text-xs font-semibold uppercase tracking-wide ${section.tone}`}>
            {section.label} ({section.groups.length})
          </p>
          {section.groups.map((group) => {
            const open = isOpen(group.key)
            const chip = lossChip(group.loss_per_day)
            const details = groupDetails(group)
            return (
              <div key={group.key} className="border-t-default pt-2 mt-2">
                <button
                  type="button"
                  onClick={() => toggle(group.key)}
                  aria-expanded={open}
                  className="w-full text-left flex items-start gap-2 touch-target"
                >
                  <span className="text-xs text-secondary mt-0.5" aria-hidden="true">
                    {open ? '▾' : '▸'}
                  </span>
                  <span className="flex-1 text-xs">{group.headline}</span>
                  {group.count > 1 && (
                    <span className="text-xs text-secondary shrink-0">x{group.count}</span>
                  )}
                  {chip && (
                    <span className={`text-xs font-semibold shrink-0 ${section.tone}`}>{chip}</span>
                  )}
                </button>
                {open && (
                  <div className="pl-5 mt-1.5">
                    {/* The action, once for the whole group. 45 latency lines
                        used to carry this same clause 45 times. */}
                    <p className="text-xs text-secondary">→ {group.action}</p>
                    {details.length > 0 && (
                      <ul className="text-xs mt-1.5 space-y-0.5 max-h-48 overflow-y-auto">
                        {details.map((finding, index) => (
                          <li key={`${finding.village}-${index}`} className="text-secondary">
                            {finding.detail}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ))}

      {noteCount > 0 && (
        <button
          type="button"
          onClick={() => setShowNotes((previous) => !previous)}
          className="btn-secondary btn-xs mt-3"
        >
          {showNotes ? 'Hide' : 'Show'} {noteCount} {noteCount === 1 ? 'note' : 'notes'}
        </button>
      )}
    </div>
  )
}

export default PlanDiagnostics
