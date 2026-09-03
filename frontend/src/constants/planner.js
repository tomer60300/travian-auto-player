/** Planner labels shared by the page and the components it delegates to.
 *
 * These lived in `pages/ResourcePlanner.jsx` while it was the only reader. The
 * Role-templates panel is the second, and a duplicated label map is how two
 * parts of one screen come to call the same thing by different names -- the
 * resource labels were already duplicated once, in `PlanDiagnostics.jsx`.
 *
 * Labels and reading order only. The contract constants -- which resources may
 * carry a declared spend, which allocation modes the backend has, which roles
 * exist -- stay in `utils/plannerSetup.js` beside the parser that enforces them,
 * so there is exactly one place a wire-format list can be got wrong.
 */

export const RESOURCES = ['lumber', 'clay', 'iron', 'crop']

export const RESOURCE_LABEL = { lumber: 'Lumber', clay: 'Clay', iron: 'Iron', crop: 'Crop' }

/** The allocation modes as the operator picks them.
 *
 * `remainder` is deliberately absent: exactly one village per resource absorbs
 * the slack, which is set by the Rest radio rather than chosen from a list --
 * offering it here would let two villages claim it.
 */
export const MODES = [
  { value: 'keep', label: 'Keep own' },
  { value: 'absolute', label: 'Absolute /h' },
  { value: 'percentage', label: '% of total' },
  { value: 'sustain', label: 'Sustain +%' },
]

/** Profile section 1's five kinds of village, in the operator's own words.
 *
 * The keys are the backend's `Role`; the labels are what the profile calls
 * them, so the selector reads like the document the roles come from.
 */
export const ROLE_LABEL = {
  capital: 'Capital / storage / NPC',
  troops_off: 'Troops off',
  full_off: 'Full off (Hammer)',
  def: 'DEF',
  feeder: 'Feeder',
}

/** Whether a role's villages may forward someone else's cargo when its
 * template says nothing -- the backend's `default_may_relay`, mirrored so the
 * operator can see what "role default" resolves to before overriding it.
 *
 * Profile section 5.9: only a feeder relays. The capital included, which is
 * section 5's own resolution -- it is the hub every feeder ships to and hands
 * off to a relay tier rather than carrying a leg in transit.
 */
export const roleRelaysByDefault = (role) => role === 'feeder'
