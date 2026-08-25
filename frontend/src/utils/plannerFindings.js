/** Presentation rules for the plan's diagnostics panel.
 *
 * The backend already ranks, groups and totals the findings, so nothing here
 * re-derives any of that — it decides only what is on screen before the
 * operator clicks anything. That question is the whole bug: a 25-village plan
 * returned 153 correct warnings as one flat bulleted list and the operator
 * refused to read it, which wasted the two that mattered along with the 151
 * that did not.
 *
 * So the panel is a hierarchy, not a list: one sentence, then the groups that
 * cost something, then everything else behind a click.
 */

/** Reading order. Matches the backend's own ranking, restated so the sections
 *  cannot silently disagree with it. */
export const SEVERITY_ORDER = ['critical', 'warning', 'note']

/** Theme tokens only — a raw Tailwind shade is unreadable in one of the two
 *  themes, and severity is exactly the thing that must survive a theme swap. */
export const SEVERITY_TONE = {
  critical: 'text-danger',
  warning: 'text-warning',
  note: 'text-secondary',
}

/** Named, because colour alone is not a severity: an operator who cannot tell
 *  amber from red gets no information from the panel at all. */
export const SEVERITY_LABEL = {
  critical: 'Costing you now',
  warning: 'Missed targets',
  note: 'Notes',
}

/** Sections in reading order, dropping any severity with nothing in it.
 *
 * Group order inside a section is the backend's, untouched: it ranks by what
 * each group costs per day, which is the ordering the operator asked for and
 * not something the view gets to re-decide.
 */
export function findingSections(groups) {
  return SEVERITY_ORDER.map((severity) => ({
    severity,
    label: SEVERITY_LABEL[severity],
    tone: SEVERITY_TONE[severity],
    groups: (groups ?? []).filter((group) => group.severity === severity),
  })).filter((section) => section.groups.length > 0)
}

/** Which groups are open before the operator touches anything.
 *
 * The expensive ones, and only those. A group's action is the sentence that
 * turns a warning into something to do, so hiding every action behind a click
 * recreates the original problem one level down — but expanding all of them
 * recreates the flat list. Anything that destroys resources is open; a missed
 * latency target is one click away.
 */
export function initialExpanded(groups) {
  return new Set(
    (groups ?? [])
      .filter((group) => group.severity === 'critical' || group.loss_per_day > 0)
      .map((group) => group.key)
  )
}

/** A per-day loss as a chip, or null when the finding costs no resources.
 *
 * Hours of latency and counts of merchants are not resources per day, and
 * printing "0/day" next to them implies the planner measured a cost and found
 * none rather than that the number does not apply.
 */
export function lossChip(perDay) {
  if (!perDay || perDay <= 0) return null
  return `${Math.round(perDay).toLocaleString()}/day`
}

/** Details worth listing under a group.
 *
 * A group of one already IS its own message — the backend uses that message as
 * the headline — so repeating it as the only detail row says nothing twice.
 */
export function groupDetails(group) {
  if (!group || group.count <= 1) return []
  return group.findings.filter((finding) => finding.detail)
}

/** The plan's one-line status, and what colour it earns.
 *
 * "Feasible" was one word answering two questions — can this sheet be carried
 * out, and is it worth carrying out — and it rendered green either way, so a
 * plan losing 2.4M resources a day looked approved. That badge is the only
 * thing between a bad plan and 53 real routes, so it now distinguishes:
 *
 * * `Cannot run` — a merchant budget, an unroutable receiver or an
 *   over-claimed allocation makes the sheet impossible. Going live is blocked.
 * * `Runs, not clean` — executable, with critical findings the check does not
 *   weigh (overflow, starvation, an unpaid tribute). Going live is allowed:
 *   those are facts about the account, and a stockpile is deliberate.
 * * `Ready to run` — executable, nothing critical outstanding.
 *
 * `verdict` is absent when an older backend answered: the app on :80 is a
 * long-running process only restarted with permission, so a freshly built
 * bundle can briefly meet an API that predates it. Then this degrades to the
 * bare boolean that API does send rather than inventing the second answer.
 */
export function planStatus(plan) {
  if (!plan) return null
  const verdict = plan.verdict ?? null
  const executable = verdict ? verdict.executable : Boolean(plan.feasible)
  if (!executable) return { label: 'Cannot run', tone: 'text-danger', verdict }
  if (verdict && !verdict.clean) return { label: 'Runs, not clean', tone: 'text-warning', verdict }
  return { label: verdict ? 'Ready to run' : 'Feasible', tone: 'text-success', verdict }
}

/** Sheet rows that are legs of a relayed crop delivery, keyed `origin:destination`.
 *
 * A relay is two ordinary rows, so the sheet cannot show it: the operator
 * typing them into the game has no way to know the second is carrying what the
 * first delivered. Both legs are indexed, because either one alone is the
 * misleading half.
 *
 * Keyed per leg rather than per path: crop pools in the hub granary, so a hub
 * with two feeders and two destinations is ONE relay, not four deliveries.
 */
export function relayLegIndex(relays) {
  const legs = new Map()
  for (const relay of relays ?? []) {
    for (const origin of relay.origins) legs.set(`${origin}:${relay.hub}`, { leg: 1, relay })
    for (const dest of relay.destinations) legs.set(`${relay.hub}:${dest}`, { leg: 2, relay })
  }
  return legs
}
