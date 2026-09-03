/** Import/export of the planner's hand-typed per-village state.
 *
 * Trade Office level and the crop alert ceiling are OWNED state: the game will
 * not tell us either, so the operator types them once per account. They live in
 * localStorage, which is scoped to an ORIGIN — so the same app served on :80,
 * on :8001, on the LAN address and over Tailscale keeps four independent copies.
 * Retyping them per origin is the problem this file exists to remove.
 *
 * Version 2 carries the allocation PROFILES too. They were the largest body of
 * typed state the file did not cover: a Day/Night pair is a hundred numbers
 * derived from store capacities, and losing them to a cleared origin meant
 * retyping the whole distribution. Profiles ride the same account guard as the
 * per-village rows, because an allocation keyed by village id is just as wrong
 * under the wrong account.
 *
 * The village row also carries three more owned facts, all optional: where a
 * village may ship (`ship_only_to`, a list of own village ids; absent means
 * unrestricted and an EMPTY list means no OWN village, for every resource
 * including crop -- foreign tributes keep their own exclusions and a whitelist
 * cannot stop one), the share of its warehouse it keeps stocked by NPC
 * trading (`stock_floor_fraction`, 0 to 0.95), which the planner may draw down
 * as extra lumber, clay or iron, and what it SPENDS per hour
 * (`consumption_per_hour`, by resource -- LUMBER, CLAY and IRON only).
 *
 * That last one is worth carrying for the same reason the profiles are: it is
 * the second-largest body of hand-typed numbers in the planner -- a spend
 * figure per resource per role village -- and it is not derivable from
 * anything. The game reports materials GROSS, so a village consuming lumber
 * still reads positive on the statistics page. Losing it to a cleared origin
 * means every consuming village silently reads as stockpiling its whole
 * allocation again.
 *
 * Crop is the exception and is REFUSED, matching the backend's 422: the
 * snapshot's crop rate is already net of upkeep, so a declared crop spend
 * double-counts the same troops. What a village should keep of its crop is
 * said with its crop allocation TARGET instead.
 *
 * Version 3 carries the ROLES: which of profile section 1's five kinds each
 * village is, and one template per role. That is the largest saving in typed
 * state the file has ever held and the worst to lose -- section 2.1 gives ONE
 * profile for FOUR defensive villages, so five templates stand in for
 * twenty-six allocations and six spend maps, and a cleared origin leaves the
 * plan reading those four villages as keeping their own production, a tenth of
 * what they need, with nothing on screen to say so. A role name outside the
 * five is refused rather than dropped, because the backend answers an unknown
 * role with a 422 and a role dropped here would leave the village with no
 * profile at all.
 *
 * Everything here is pure, including the timestamp, which is passed in rather
 * than read. That keeps the round trip testable without a browser.
 */

import { RESOURCE_LABEL, ROLE_LABEL } from '../constants/planner'

export const SETUP_FORMAT = 'travian-planner-owned-state'
export const SETUP_VERSION = 3
/** Versions this build can read. A v1 file simply carries no profiles and a v2
 * one no roles, so refusing either would strand every export written before
 * those travelled. The version still has to rise when a field is added, and in
 * the other direction: a build that cannot read roles must REFUSE a file that
 * has them, or it loads the villages, drops their profiles and plans a
 * different account without saying so. */
export const READABLE_VERSIONS = Object.freeze([1, 2, 3])

/** Matches the Trade Office input's own bounds, and the backend's `le=20`. */
export const MAX_TRADE_OFFICE_LEVEL = 20

/** The backend's ceiling for an NPC-backed stock floor. Above it a village
 * keeps nothing worth drawing down. */
export const MAX_STOCK_FLOOR_FRACTION = 0.95

/** Is this a usable stock floor? The operator types a percent, whole or to one
 * decimal, so the fraction must sit on a 0.001 grid between 0 and the ceiling.
 * Shared by the file parser and the planner's input so the two cannot disagree
 * about what a valid floor is. */
export function isStockFloorFraction(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return false
  if (value < 0 || value > MAX_STOCK_FLOOR_FRACTION) return false
  const permille = value * 1000
  return Math.abs(permille - Math.round(permille)) < 1e-6
}

/** Is this a usable consumption rate? What a village spends per hour, so zero
 * or more and finite. Negative is refused rather than clamped for the reason
 * the backend refuses it: a rate's sign cannot be read as consumption, because
 * the game's statistics page reports materials GROSS and a village burning
 * lumber still shows a positive figure. There is nothing to invert, so a
 * negative here is a typo and worth stopping for. Shared by the file parser and
 * the planner's input so the two cannot disagree. */
export function isConsumptionRate(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

/** Has this village had any spend typed at all?
 *
 * A map with every box cleared is the same as no map: the backend reads absent
 * and `{}` identically, so the file, the request and the input must all treat
 * them the same or one of them starts claiming something the others do not.
 * Unlike `ship_only_to`, where an empty list IS a distinct answer ("nobody"),
 * there is no third state to express here.
 */
export function declaresConsumption(spent) {
  return spent != null && Object.keys(spent).length > 0
}

/** The backend's AllocationMode, which the file must not outrun. */
export const ALLOCATION_MODES = Object.freeze([
  'keep',
  'absolute',
  'percentage',
  'sustain',
  'remainder',
])
/** The modes a ROLE TEMPLATE may carry: every one except remainder.
 *
 * Exactly one village per resource absorbs the slack, and a profile shared by
 * four defensive villages cannot say which -- so remainder stays per village,
 * set by the Rest radio. It is refused rather than dropped: left to the backend
 * a template's remainder fanned out to every village of the role, and the plan
 * came back as a 400 naming VILLAGES ("got 02, 11, 13, 17, 19") for one
 * mistyped template. Five bad cells to work back from, when the file says which
 * one wrote them.
 *
 * The backend's `RoleTemplate.allocations` validator refuses the same mode, so
 * the file and the request cannot disagree. Every other mode is a figure each
 * village of the role can hold independently, which is what makes a profile
 * shareable at all.
 */
export const TEMPLATE_ALLOCATION_MODES = Object.freeze(
  ALLOCATION_MODES.filter((mode) => mode !== 'remainder')
)
export const SETUP_RESOURCES = Object.freeze(['lumber', 'clay', 'iron', 'crop'])

/** The resources a village may DECLARE a spend for. Crop is absent, and that
 * absence is the whole rule: the snapshot's `crop_per_hour` is already net of
 * troop upkeep (it comes from the village's crop balance, not the gross
 * statistics column), so a declared crop spend subtracts the same troops twice.
 * Materials are the opposite case -- reported GROSS, so a village burning
 * lumber still reads positive and nothing in the game states the spend.
 * The backend refuses a crop key with a 422; this is the same list, so the
 * file, the input and the request cannot disagree. */
export const CONSUMABLE_RESOURCES = Object.freeze(['lumber', 'clay', 'iron'])

/** The five kinds of village in profile section 1, and the backend's `Role`.
 *
 * A closed set, so a name that is not one of them is an error rather than a
 * dropped key: "hammer" is what the operator calls village 01 in conversation,
 * and importing it as silence would leave that village with no profile at all
 * while the file looked like it had loaded. */
export const VILLAGE_ROLES = Object.freeze([
  'capital',
  'troops_off',
  'full_off',
  'def',
  'feeder',
])

/** Neither send nor receive: the default for a village nothing has said about.
 *
 * Frozen and shared so every caller compares against one object rather than
 * minting its own literal, which is how two "defaults" come to differ. */
export const KEEP_ALLOCATION = Object.freeze({ mode: 'keep', value: 0 })

/** The allocation a plan will actually use for one village and one resource.
 *
 * The village's own entry where it has one, then its role's template, then
 * keep -- the same order, and the same per-resource granularity, the backend
 * resolves in. The page has to do this itself because the operator edits before
 * any plan exists, and getting the order wrong is not a visible bug: the grid
 * would show a defensive village as "Keep own" while the plan shipped it
 * 8,372/h, and the unassigned meter would count its own 1,500 instead, so the
 * Rest village's displayed target would be wrong by the difference.
 *
 * An explicit `keep` SURVIVES rather than falling through. On a village with a
 * role the alternative to the template is not "nothing", it is "hold your own
 * production" -- so it is an answer, `roleDeviates` reports it, and the request
 * carries it.
 */
export function resolveRoleAllocation(template, resource, allocation) {
  return allocation ?? template?.allocations?.[resource] ?? KEEP_ALLOCATION
}

/** The same resolution for what a village spends. Zero is a claim, not silence,
 * so only an absent figure falls through to the role's. */
export function resolveRoleSpend(template, resource, spend) {
  return spend ?? template?.consumption?.[resource]
}

/** Does this village's own allocation differ from what its role's template says?
 *
 * The same question the backend answers with `role_deviations`, asked here so
 * the allocation grid can mark the cell while the operator is still editing and
 * no plan exists yet. Kept as one exported predicate rather than inlined,
 * because a grid that marks a cell the plan did not is worse than one that
 * marks none: it is confidently wrong.
 *
 * A resource the template has no opinion about cannot deviate. An explicit
 * `keep` DOES deviate, and deliberately: in the planner keep means "hold your
 * own production", which is a different answer from the template's rather than
 * an absence of one, so it overrides and the backend reports it.
 */
export function roleDeviates(template, resource, allocation) {
  const stated = template?.allocations?.[resource]
  if (stated == null || allocation == null) return false
  return stated.mode !== allocation.mode || Number(stated.value) !== Number(allocation.value)
}

/** One village's spend as the plan will APPLY it, and where each figure came
 *  from.
 *
 * `resolveRoleSpend` answers this for one resource; this is the whole map,
 * because two surfaces show a village's spend collapsed and both were reading
 * the own map alone. The setup table therefore printed "none" and three blank
 * boxes for a defensive village the plan was spending 8,372/h of lumber at --
 * the same defect the Allocate grid had for its targets, and the reason the two
 * views must resolve through one reader rather than two.
 *
 * `fromRole` and `overridden` are the provenance the collapsed cell needs:
 * without them a role's figure and a hand-typed one are the same digits, and
 * the operator cannot tell which village they have actually said something
 * about. Materials only, like every other spend reader -- crop cannot be
 * declared.
 */
export function resolvedSpend(template, spent) {
  const effective = {}
  const fromRole = []
  const overridden = []
  for (const resource of CONSUMABLE_RESOURCES) {
    const own = spent?.[resource]
    const role = template?.consumption?.[resource]
    if (own != null) {
      effective[resource] = Number(own)
      if (role != null) overridden.push(resource)
    } else if (role != null) {
      effective[resource] = Number(role)
      fromRole.push(resource)
    }
  }
  return { effective, fromRole, overridden }
}

/** Where the figures a spend cell is showing came from, in one phrase.
 *
 * `null` where the role supplied none of them: every figure on screen was then
 * typed against this village, which is what an untemplated account looks like
 * and needs no attribution. Naming the role only where it actually supplied
 * something is the whole point -- an attribution on a village that overrode
 * every figure would credit a profile the plan is not using.
 */
export function describeSpendSource({ fromRole, overridden }, role) {
  if (!fromRole.length) return null
  const label = ROLE_LABEL[role] ?? role
  if (!overridden.length) return `from ${label}`
  return `${label}, own ${overridden.map((resource) => RESOURCE_LABEL[resource]).join(', ')}`
}

/** A spend map with everything undeclarable dropped, or null when nothing
 * declarable is left.
 *
 * Only crop is ever dropped, and only because an older build could store one:
 * the input no longer offers it, so such a figure can be neither seen nor
 * cleared, while still being POSTed on every plan and 422'd on a number the
 * operator cannot find. Returning null rather than `{}` keeps "declared
 * nothing" one state, as `declaresConsumption` requires. */
export function materialSpendOnly(spent) {
  if (!declaresConsumption(spent)) return null
  const out = {}
  for (const resource of CONSUMABLE_RESOURCES) {
    if (spent[resource] != null) out[resource] = Number(spent[resource])
  }
  return Object.keys(out).length ? out : null
}

/** The `allocations` a plan request carries, from the profile on screen.
 *
 * Every explicit allocation is sent, readable rate or not: filtering the
 * unreadable ones hid them from the backend's UNREADABLE_RATE critical, so the
 * plan read "Ready to run" while silently planning without an allocation the
 * operator wrote.
 *
 * Two things are dropped. A village the snapshot no longer has, because the
 * backend 400s an unknown id. And a KEEP on a village with NO role, because
 * there an absent entry resolves to exactly the same thing -- but a KEEP on a
 * village WITH a role is a statement: the alternative to the template is not
 * "nothing", it is "hold your own production". Dropped, the template would fill
 * straight back in and the grid would show Keep own while the plan shipped the
 * profile.
 *
 * A resource left with nothing usable is omitted rather than sent empty, so an
 * untouched account's request is byte-identical to one from before roles.
 */
export function allocationsForRequest(allocations, villageRoles, villageIds) {
  const own = new Set((villageIds ?? []).map(Number))
  const out = {}
  for (const [resource, per] of Object.entries(allocations ?? {})) {
    const usable = {}
    for (const [vid, a] of Object.entries(per)) {
      if (a.mode === 'keep' && villageRoles?.[Number(vid)] == null) continue
      if (!own.has(Number(vid))) continue
      usable[vid] = a
    }
    if (Object.keys(usable).length) out[resource] = usable
  }
  return out
}

/** The `roles` a plan request carries: a template for every role some village
 *  claims, and nothing else.
 *
 * A role whose template is ABSENT is skipped, which is the whole point. Sending
 * `{}` for it made the backend's "no role template was sent for ..." 422
 * unreachable from the page: four villages set to DEF before the panel was
 * filled in planned at HTTP 200 with target 1,500 and spend 0 -- a tenth of
 * what those villages need, reported as feasible. Skipping it puts the refusal
 * back in front of the operator, naming the villages and the role.
 *
 * A template that IS present but half typed is sent whole, with its four
 * halves spelled out rather than spread: a template is a template from the
 * moment the operator gives a role any figure at all, and the page's own
 * "no template yet" warning reads the same key. Its crop spend is dropped on
 * the way out, because the backend refuses one and a template stored by an
 * older build could still carry it -- which would 422 every plan over a figure
 * the editor no longer shows.
 */
export function rolesForRequest(roleTemplates, claimedRoles) {
  const out = {}
  for (const role of claimedRoles ?? []) {
    const template = roleTemplates?.[role]
    if (template == null) continue
    out[role] = {
      allocations: template.allocations ?? {},
      consumption: materialSpendOnly(template.consumption) ?? {},
      may_relay: template.may_relay ?? null,
      crop_negative_by_design: Boolean(template.crop_negative_by_design),
    }
  }
  return out
}

/** Rehydrate a stored consumption map, and say whose crop figure was dropped.
 *
 * The strip itself is `materialSpendOnly` applied village by village; what this
 * adds is the RECEIPT. Stripping silently made a previously-silenced CRITICAL
 * reappear on the next plan with nothing on screen connecting cause to effect,
 * while the file-import path raises a loud `SetupFileError` for the very same
 * figure. The page turns `droppedFrom` into a note beside the setup table.
 *
 * A village whose ONLY stored figure was crop is reported too, and it is the
 * loudest case: it leaves the map entirely, so nothing about it is visible
 * anywhere afterwards.
 *
 * Lifted out of the page's hydration effect because `renderToString` runs no
 * effects, so the strip could not be reached by a test at all where it lived.
 */
export function stripStoredCropSpends(stored) {
  const consumption = {}
  const droppedFrom = []
  if (!stored || typeof stored !== 'object') return { consumption, droppedFrom }
  for (const [villageId, spent] of Object.entries(stored)) {
    const kept = materialSpendOnly(spent)
    if (kept) consumption[villageId] = kept
    if (spent && typeof spent === 'object' && spent.crop != null) droppedFrom.push(villageId)
  }
  return { consumption, droppedFrom }
}

/** Rehydrate the stored roles and templates, and say what was dropped.
 *
 * The same shape and the same discipline as `stripStoredCropSpends`: a role
 * outside the five HAS to go, because the backend answers an unknown one with a
 * 422 and a stale name from a future build would refuse every plan over a value
 * the selector cannot show. What was missing was the RECEIPT.
 *
 * The consequence is larger than the crop spend's. A dropped role takes that
 * village's targets AND its spend with it -- section 2.1's one profile stands
 * in for four defensive villages, so the plan silently reads them as keeping
 * their own production, a tenth of what they need, and calls it feasible. The
 * file-import path raises a loud `SetupFileError` for the very same name.
 *
 * A template for a role no village claims is NOT dropped: that is a file being
 * complete, and the backend accepts it. Only a key outside the five goes, and
 * it is reported under its own heading -- a template lost while its villages
 * keep their role is the case where the next plan is refused rather than
 * quietly wrong, which is a different sentence for the operator to read.
 *
 * The note clears itself on the next hydration, because the stripped maps are
 * what get saved back.
 */
export function stripUnknownRoles(storedRoles, storedTemplates) {
  const villageRoles = {}
  const templates = {}
  const droppedFrom = []
  const droppedNames = new Set()
  const droppedTemplates = new Set()
  if (storedRoles && typeof storedRoles === 'object') {
    for (const [villageId, role] of Object.entries(storedRoles)) {
      if (VILLAGE_ROLES.includes(role)) villageRoles[villageId] = role
      else {
        droppedFrom.push(villageId)
        droppedNames.add(String(role))
      }
    }
  }
  if (storedTemplates && typeof storedTemplates === 'object') {
    for (const [role, template] of Object.entries(storedTemplates)) {
      if (VILLAGE_ROLES.includes(role)) templates[role] = template
      else droppedTemplates.add(role)
    }
  }
  return {
    villageRoles,
    templates,
    droppedFrom,
    droppedNames: [...droppedNames].sort(),
    droppedTemplates: [...droppedTemplates].sort(),
  }
}

/** The collapsed summary of one village's spend, for the setup table's cell.
 *
 * What it says, not how many fields it has. Materials only: crop cannot be
 * declared, because the snapshot's crop rate is already net of upkeep, so
 * reading every resource here would summarise a figure the planner refuses to
 * accept -- and a stored one, which `stripStoredCropSpends` has already thrown
 * away, would print as a spend that is not being applied.
 */
export function describeConsumption(spent) {
  if (!declaresConsumption(spent)) return 'none'
  const declared = CONSUMABLE_RESOURCES.filter((resource) => spent[resource] != null)
  if (!declared.length) return 'none'
  const total = declared.reduce((sum, resource) => sum + (Number(spent[resource]) || 0), 0)
  const rate = `${Math.round(total).toLocaleString()}/h`
  if (declared.length === CONSUMABLE_RESOURCES.length) return `${rate}, all three`
  return `${rate} · ${declared.map((resource) => RESOURCE_LABEL[resource]).join(', ')}`
}

/** Travian's repeat interval, which is a closed set of the divisors of 24. */
export const TRAVIAN_REPEAT_INTERVALS = Object.freeze([1, 2, 3, 4, 6, 8, 12, 24])

export class SetupFileError extends Error {}

/** Build the exportable document. Villages with nothing typed are left out. */
export function buildSetup({
  account,
  villages,
  tradeOffice,
  cropCeilings,
  shipOnlyTo,
  stockFloors,
  consumption,
  villageRoles,
  roles,
  profiles,
  profileWindows,
  merchantModel,
  foreignTargets,
  exportedAt,
}) {
  const rows = []
  for (const village of villages ?? []) {
    const level = tradeOffice?.[village.village_id]
    const ceiling = cropCeilings?.[village.village_id]
    const allowed = shipOnlyTo?.[village.village_id]
    const floor = stockFloors?.[village.village_id]
    const role = villageRoles?.[village.village_id]
    // Materials only, so an export can never write a file this same parser
    // refuses to read back -- a crop figure saved by an older build would do
    // exactly that.
    const spends = materialSpendOnly(consumption?.[village.village_id])
    if (
      level == null &&
      ceiling == null &&
      allowed == null &&
      floor == null &&
      role == null &&
      !spends
    ) {
      continue
    }
    const row = { village_id: village.village_id, name: village.name ?? '' }
    if (role != null) row.role = String(role)
    if (level != null) row.trade_office_level = Number(level)
    if (ceiling != null) row.crop_ceiling = Number(ceiling)
    // An empty list is written, not dropped: it says "ships to nobody", which
    // is a different answer from the unrestricted default an absent field means.
    if (allowed != null) row.ship_only_to = allowed.map(Number)
    if (floor != null) row.stock_floor_fraction = Number(floor)
    if (spends) row.consumption_per_hour = spends
    rows.push(row)
  }
  const doc = {
    format: SETUP_FORMAT,
    version: SETUP_VERSION,
    exported_at: exportedAt,
    // Carried so a file cannot be loaded into the wrong account unnoticed:
    // village ids are per-account, so A's levels under B are silently wrong.
    account: account ?? null,
    villages: rows,
  }
  // Omitted rather than written empty, so a file says plainly whether it has
  // profiles to give. An empty object would import as "replace everything with
  // nothing", which is the opposite of what an operator loading a file wants.
  if (profiles && Object.keys(profiles).length) doc.profiles = profiles
  // The role templates, on the same rule and for a sharper reason: five
  // templates stand in for twenty-six allocations and six spend maps (section
  // 2.1's one profile for four defensive villages), so losing them to a cleared
  // origin makes the plan read four defensive villages as keeping their own
  // production -- a tenth of what they need -- with nothing on screen to say so.
  if (roles && Object.keys(roles).length) doc.roles = roles
  if (profileWindows && Object.keys(profileWindows).length) {
    doc.profile_windows = profileWindows
  }
  if (merchantModel) doc.merchant_model = merchantModel
  // A tribute is entirely operator-supplied -- the game will not say that an ally
  // needs 25,700 crop an hour -- and it drives real routes. Losing it to a cleared
  // origin means the obligation silently stops being planned for.
  if (foreignTargets && foreignTargets.length) doc.foreign_targets = foreignTargets
  return doc
}

/** Validate one profile's allocation map, resource by resource.
 *
 * Negative values are deliberately allowed. An `absolute` retention below a
 * village's own production is how a store that is already past its ceiling gets
 * drained rather than merely frozen, and the night profile is built on exactly
 * that — so rejecting the sign here would reject the plan it exists to carry.
 */
function parseProfile(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not an allocation map.`)
  }
  const out = {}
  for (const [resource, per] of Object.entries(raw)) {
    if (!SETUP_RESOURCES.includes(resource)) {
      throw new SetupFileError(`${where} has unknown resource "${resource}".`)
    }
    if (!per || typeof per !== 'object' || Array.isArray(per)) {
      throw new SetupFileError(`${where}.${resource} is not an object.`)
    }
    const kept = {}
    for (const [vid, alloc] of Object.entries(per)) {
      const id = Number(vid)
      if (!Number.isInteger(id) || id <= 0) {
        throw new SetupFileError(`${where}.${resource} has no usable village id "${vid}".`)
      }
      if (!alloc || typeof alloc !== 'object') {
        throw new SetupFileError(`${where}.${resource}[${vid}] is not an allocation.`)
      }
      if (!ALLOCATION_MODES.includes(alloc.mode)) {
        throw new SetupFileError(
          `${where}.${resource}[${vid}] has mode "${alloc.mode ?? 'nothing'}"; ` +
            `it must be one of ${ALLOCATION_MODES.join(', ')}.`
        )
      }
      const value = alloc.value == null ? 0 : Number(alloc.value)
      if (!Number.isFinite(value)) {
        throw new SetupFileError(`${where}.${resource}[${vid}] has a non-numeric value.`)
      }
      // Whole units. A file written from a raw computation can carry values
      // like 43726.200918351606, and that lands verbatim in the operator's
      // input box -- unreadable, uneditable, and pretending to a precision no
      // /h rate has. Sub-unit differences are noise to the planner.
      kept[id] = { mode: alloc.mode, value: Math.round(value) }
    }
    out[resource] = kept
  }
  return out
}

/** Validate the foreign-target (tribute) list.
 *
 * Rejects rather than repairs. An anonymous target cannot be reported to the
 * operator, and a negative rate would plan as a SOURCE -- the opposite of an
 * obligation. Both are more useful as an error than as a silently altered plan.
 */
function parseForeignTargets(raw, where) {
  if (!Array.isArray(raw)) throw new SetupFileError(`${where} is not a list.`)
  return raw.map((entry, i) => {
    const at = `${where}[${i}]`
    if (!entry || typeof entry !== 'object') throw new SetupFileError(`${at} is not an object.`)
    const name = String(entry.name ?? '').trim()
    if (!name) throw new SetupFileError(`${at} has no name.`)
    const x = Number(entry.x)
    const y = Number(entry.y)
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      throw new SetupFileError(`${at} ("${name}") has coordinates that are not numbers.`)
    }
    const rate = Number(entry.crop_per_hour)
    if (!Number.isFinite(rate) || rate < 0) {
      throw new SetupFileError(
        `${at} ("${name}") has crop_per_hour ${entry.crop_per_hour}; a tribute is owed, ` +
          `so it cannot be negative.`
      )
    }
    const margin = entry.safety_margin_pct == null ? 0 : Number(entry.safety_margin_pct)
    if (!Number.isFinite(margin) || margin < 0 || margin > 100) {
      throw new SetupFileError(`${at} ("${name}") has a safety margin outside 0-100.`)
    }
    // Rebuilt field by field, so anything not named here is DROPPED. That is how
    // the cadence controls went missing: a file saying "47,167 an hour, hourly,
    // not from the hub" imported as "47,167 an hour" -- the same volume with the
    // constraint silently gone, which is the plan the operator was avoiding.
    let maxCycle = null
    if (entry.max_cycle_hours != null) {
      const cycle = Number(entry.max_cycle_hours)
      if (!TRAVIAN_REPEAT_INTERVALS.includes(cycle)) {
        throw new SetupFileError(
          `${at} ("${name}") has max_cycle_hours ${entry.max_cycle_hours}; Travian's ` +
            `repeat interval is a closed set, so choose one of ` +
            `${TRAVIAN_REPEAT_INTERVALS.join(', ')}.`
        )
      }
      maxCycle = cycle
    }
    const excluded = []
    for (const raw of entry.exclude_origins ?? []) {
      const id = Number(raw)
      if (!Number.isInteger(id) || id <= 0) {
        throw new SetupFileError(
          `${at} ("${name}") has ${JSON.stringify(raw)} in exclude_origins; ` +
            `it must be a village id.`
        )
      }
      excluded.push(id)
    }
    return {
      name,
      x,
      y,
      crop_per_hour: rate,
      safety_margin_pct: margin,
      route_eligible: Boolean(entry.route_eligible),
      max_cycle_hours: maxCycle,
      exclude_origins: excluded,
    }
  })
}

/** Validate one village's `{ resource: rate }` consumption map.
 *
 * Rejects rather than repairs, the same discipline the rest of this file
 * follows: a spend under an unknown resource name would import as silence, and
 * silence here means the village reads as stockpiling its whole allocation --
 * which is the exact defect consumption exists to remove.
 */
function parseConsumption(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not a map of resource to rate per hour.`)
  }
  const out = {}
  for (const [resource, rate] of Object.entries(raw)) {
    if (!SETUP_RESOURCES.includes(resource)) {
      // The DECLARABLE list, not every resource the planner knows. Enumerating
      // `SETUP_RESOURCES` here offered crop as a correction and then refused it
      // one branch below, so the operator fixed "gold" to "crop" on this
      // message's own advice and got a second error for their trouble.
      throw new SetupFileError(
        `${where} has unknown resource "${resource}"; ` +
          `it must be one of ${CONSUMABLE_RESOURCES.join(', ')}.`
      )
    }
    // A separate message from the unknown-resource one, because "unknown
    // resource crop" would be a lie: crop is a resource the planner knows
    // well, and the operator's own spec lists a crop figure per role village.
    // What they need is where to put it instead.
    if (!CONSUMABLE_RESOURCES.includes(resource)) {
      throw new SetupFileError(
        `${where} declares ${resource}, which the planner refuses: the ` +
          `snapshot's crop rate is already net of troop upkeep, so a declared ` +
          `crop spend subtracts the same troops twice. Declare ` +
          `${CONSUMABLE_RESOURCES.join(', ')} only, and say what the village ` +
          `should keep of its crop with its crop allocation target instead.`
      )
    }
    // The RAW value, before any coercion. `Number(null)` is 0, `Number([1])`
    // is 1 and `Number('')` is 0, so coercing first defeated the
    // `typeof === 'number'` half of the shared predicate and imported three
    // malformed shapes as confident declarations -- while the planner's input
    // tests the raw value, so parser and input disagreed about the same file.
    if (!isConsumptionRate(rate)) {
      throw new SetupFileError(
        typeof rate === 'number' && Number.isFinite(rate)
          ? `${where}.${resource} is ${rate}; consumption is what a village ` +
            `SPENDS per hour, so it cannot be negative.`
          : `${where}.${resource} is ${JSON.stringify(rate)}, which is not a ` +
            `number; consumption is a flat rate per hour.`
      )
    }
    // Whole units, as the allocation values are: a /h rate has no sub-unit
    // precision, and a figure like 14750.600918351606 from a raw computation
    // lands verbatim in the operator's input box. Rounded only after the raw
    // value has been accepted.
    out[resource] = Math.round(rate)
  }
  return out
}

/** Validate one role's template: its profile, its spend and its two flags.
 *
 * Allocations are keyed by RESOURCE alone, not by resource and village: a
 * template is one profile applied to every village of the role, which is the
 * whole point of it. Otherwise the same discipline as `parseProfile` -- the
 * mode must be one the backend has, the value is rounded to whole units
 * because a `/h` rate has no sub-unit precision and a raw computation's
 * 8371.6009 lands verbatim in the operator's input box.
 *
 * The optional halves are DEFAULTED rather than left undefined, so the shape
 * the page holds is the shape the file gives it and a template can be rendered
 * without every reader guarding for absence.
 */
function parseRoleTemplate(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not a role template.`)
  }
  const allocations = {}
  for (const [resource, alloc] of Object.entries(raw.allocations ?? {})) {
    if (!SETUP_RESOURCES.includes(resource)) {
      throw new SetupFileError(
        `${where}.allocations has unknown resource "${resource}"; ` +
          `it must be one of ${SETUP_RESOURCES.join(', ')}.`
      )
    }
    if (!alloc || typeof alloc !== 'object') {
      throw new SetupFileError(`${where}.allocations.${resource} is not an allocation.`)
    }
    // A separate message from the unknown-mode one, because "unknown mode
    // remainder" would be a lie: remainder is a mode the planner uses on every
    // account. What the operator needs is where to say it instead.
    if (alloc.mode === 'remainder') {
      throw new SetupFileError(
        `${where}.allocations.${resource} uses remainder, which a role template ` +
          `refuses: remainder stays per village. Exactly one village per ` +
          `resource absorbs the slack, and a profile shared by four villages ` +
          `cannot say which one -- set it on the village itself with the Rest ` +
          `radio, and give the template one of ` +
          `${TEMPLATE_ALLOCATION_MODES.join(', ')}.`
      )
    }
    if (!TEMPLATE_ALLOCATION_MODES.includes(alloc.mode)) {
      throw new SetupFileError(
        `${where}.allocations.${resource} has mode "${alloc.mode ?? 'nothing'}"; ` +
          `it must be one of ${TEMPLATE_ALLOCATION_MODES.join(', ')}.`
      )
    }
    const value = alloc.value == null ? 0 : Number(alloc.value)
    if (!Number.isFinite(value)) {
      throw new SetupFileError(`${where}.allocations.${resource} has a non-numeric value.`)
    }
    allocations[resource] = { mode: alloc.mode, value: Math.round(value) }
  }
  // Reuses the per-village spend parser, so the crop refusal and the negative
  // refusal are stated once. A template is the second door onto the same
  // mistake -- section 2 lists a crop figure per role village, so it is the
  // field an operator reaches for -- and two doors with one rule behind them is
  // the only arrangement that cannot drift.
  const consumption =
    raw.consumption == null ? {} : parseConsumption(raw.consumption, `${where}.consumption`)
  if (raw.may_relay != null && typeof raw.may_relay !== 'boolean') {
    throw new SetupFileError(
      `${where}.may_relay is ${JSON.stringify(raw.may_relay)}; it must be true, ` +
        `false, or absent to take the role's own answer.`
    )
  }
  return {
    allocations,
    consumption,
    // null rather than undefined, and distinct from false: unset means "take
    // the role's own answer", which is what almost every template says, while
    // an explicit false on a feeder is the one account that wants its feeders
    // kept out of the relay tier.
    may_relay: raw.may_relay ?? null,
    crop_negative_by_design: Boolean(raw.crop_negative_by_design),
  }
}

/** Validate a `{ profile: ['HH:MM', 'HH:MM'] }` window map. */
function parseWindows(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not a window map.`)
  }
  const out = {}
  for (const [name, pair] of Object.entries(raw)) {
    if (!Array.isArray(pair) || pair.length !== 2) {
      throw new SetupFileError(`${where}["${name}"] must be a [start, end] pair.`)
    }
    for (const t of pair) {
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(String(t))) {
        throw new SetupFileError(`${where}["${name}"] has "${t}", which is not HH:MM.`)
      }
    }
    out[name] = [String(pair[0]), String(pair[1])]
  }
  return out
}

/** Parse and validate a setup document. Throws rather than half-loading.
 *
 * Out-of-range levels are rejected, not clamped. Clamping down would be the
 * safe direction for merchant budgets, but it would silently rewrite what the
 * operator typed, and a level above the maximum means the file is wrong — that
 * is worth stopping for.
 */
export function parseSetup(text) {
  let raw
  try {
    raw = JSON.parse(text)
  } catch {
    throw new SetupFileError('Not valid JSON — check the file was saved whole.')
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError('Not a planner setup file.')
  }
  if (raw.format !== SETUP_FORMAT) {
    throw new SetupFileError(
      `Not a planner setup file (expected format "${SETUP_FORMAT}", got "${raw.format ?? 'nothing'}").`
    )
  }
  if (!READABLE_VERSIONS.includes(raw.version)) {
    throw new SetupFileError(
      `This file is version ${raw.version}, and this build reads ` +
        `version ${READABLE_VERSIONS.join(' or ')}. ` +
        `Re-export it from the build that wrote it.`
    )
  }
  if (!Array.isArray(raw.villages)) {
    throw new SetupFileError('The file has no villages list.')
  }

  const villages = raw.villages.map((row, i) => {
    const where = `villages[${i}]`
    if (!row || typeof row !== 'object') throw new SetupFileError(`${where} is not an object.`)
    const id = Number(row.village_id)
    if (!Number.isInteger(id) || id <= 0) {
      throw new SetupFileError(`${where} has no usable village_id.`)
    }
    const parsed = { village_id: id, name: typeof row.name === 'string' ? row.name : '' }
    if (row.role != null) {
      if (!VILLAGE_ROLES.includes(row.role)) {
        throw new SetupFileError(
          `${where} has role "${row.role}"; it must be one of ` +
            `${VILLAGE_ROLES.join(', ')}. The backend answers an unknown role with ` +
            `a 422, and a role dropped here would leave the village with no ` +
            `profile at all.`
        )
      }
      parsed.role = row.role
    }
    if (row.trade_office_level != null) {
      const level = Number(row.trade_office_level)
      if (!Number.isInteger(level) || level < 0 || level > MAX_TRADE_OFFICE_LEVEL) {
        throw new SetupFileError(
          `${where} has Trade Office level ${row.trade_office_level}; ` +
            `it must be a whole number from 0 to ${MAX_TRADE_OFFICE_LEVEL}.`
        )
      }
      parsed.trade_office_level = level
    }
    if (row.crop_ceiling != null) {
      const ceiling = Number(row.crop_ceiling)
      if (!Number.isFinite(ceiling) || ceiling < 0) {
        throw new SetupFileError(`${where} has a negative or non-numeric crop ceiling.`)
      }
      parsed.crop_ceiling = ceiling
    }
    if (row.ship_only_to != null) {
      if (!Array.isArray(row.ship_only_to)) {
        throw new SetupFileError(`${where} has a ship_only_to that is not a list of village ids.`)
      }
      parsed.ship_only_to = row.ship_only_to.map((raw) => {
        const id = Number(raw)
        if (!Number.isInteger(id) || id <= 0) {
          throw new SetupFileError(
            `${where} has ${JSON.stringify(raw)} in ship_only_to; it must be a village id.`
          )
        }
        return id
      })
    }
    if (row.stock_floor_fraction != null) {
      const floor = Number(row.stock_floor_fraction)
      if (!isStockFloorFraction(floor)) {
        throw new SetupFileError(
          `${where} has stock floor ${row.stock_floor_fraction}; it must be a fraction ` +
            `from 0 to ${MAX_STOCK_FLOOR_FRACTION} in steps of 0.001 ` +
            `(a whole percent or one decimal).`
        )
      }
      parsed.stock_floor_fraction = floor
    }
    if (row.consumption_per_hour != null) {
      parsed.consumption_per_hour = parseConsumption(
        row.consumption_per_hour,
        `${where}.consumption_per_hour`
      )
    }
    return parsed
  })

  const profiles = {}
  if (raw.profiles != null) {
    if (typeof raw.profiles !== 'object' || Array.isArray(raw.profiles)) {
      throw new SetupFileError('The file has a profiles field that is not a map of profiles.')
    }
    for (const [name, alloc] of Object.entries(raw.profiles)) {
      if (!name.trim()) throw new SetupFileError('A profile in the file has an empty name.')
      profiles[name] = parseProfile(alloc, `profiles["${name}"]`)
    }
  }
  const roles = {}
  if (raw.roles != null) {
    if (typeof raw.roles !== 'object' || Array.isArray(raw.roles)) {
      throw new SetupFileError('The file has a roles field that is not a map of templates.')
    }
    for (const [name, template] of Object.entries(raw.roles)) {
      if (!VILLAGE_ROLES.includes(name)) {
        throw new SetupFileError(
          `The file has a template for role "${name}", which is not one of ` +
            `${VILLAGE_ROLES.join(', ')}.`
        )
      }
      roles[name] = parseRoleTemplate(template, `roles["${name}"]`)
    }
  }
  const profileWindows =
    raw.profile_windows == null ? {} : parseWindows(raw.profile_windows, 'profile_windows')

  let merchantModel = null
  if (raw.merchant_model != null) {
    const m = raw.merchant_model
    const base = Number(m?.base_capacity)
    const bonus = Number(m?.bonus_per_to_level)
    if (!Number.isFinite(base) || base <= 0) {
      throw new SetupFileError('merchant_model.base_capacity must be a positive number.')
    }
    if (!Number.isFinite(bonus) || bonus < 0) {
      throw new SetupFileError('merchant_model.bonus_per_to_level must be zero or more.')
    }
    merchantModel = { base_capacity: base, bonus_per_to_level: bonus }
  }

  const foreignTargets =
    raw.foreign_targets == null
      ? null
      : parseForeignTargets(raw.foreign_targets, "foreign_targets")

  return { ...raw, villages, roles, profiles, profileWindows, merchantModel, foreignTargets }
}

/** Apply a parsed setup over the current maps, and say exactly what happened.
 *
 * The file wins where it has a value; villages it does not mention keep what
 * they already had. A village with no value from either source is reported as
 * still unknown and is deliberately NOT set to 0 — the planner floors unknown
 * levels to 0 when it plans, but recording that guess as if the operator had
 * confirmed it is how an over-provisioned village becomes invisible.
 */
export function mergeSetup({
  setup,
  villages,
  tradeOffice,
  cropCeilings,
  shipOnlyTo,
  stockFloors,
  consumption,
  villageRoles,
  roles,
  profiles,
  profileWindows,
  foreignTargets,
}) {
  const known = new Map((villages ?? []).map((v) => [v.village_id, v]))
  const nextTradeOffice = { ...(tradeOffice ?? {}) }
  const nextCropCeilings = { ...(cropCeilings ?? {}) }
  const nextShipOnlyTo = { ...(shipOnlyTo ?? {}) }
  const nextStockFloors = { ...(stockFloors ?? {}) }
  const nextConsumption = { ...(consumption ?? {}) }
  const nextVillageRoles = { ...(villageRoles ?? {}) }

  const missingFromAccount = []
  let loaded = 0

  for (const row of setup.villages) {
    if (!known.has(row.village_id)) {
      // Kept out of the maps entirely: a chiefed or renamed village must not
      // linger as state for an id the account no longer has.
      missingFromAccount.push({ village_id: row.village_id, name: row.name })
      continue
    }
    loaded += 1
    // Silence is not a clear: a row that says nothing about the role leaves the
    // one on screen alone, the same rule every other column here follows.
    if (row.role != null) nextVillageRoles[row.village_id] = row.role
    if (row.trade_office_level != null) nextTradeOffice[row.village_id] = row.trade_office_level
    if (row.crop_ceiling != null) nextCropCeilings[row.village_id] = row.crop_ceiling
    if (row.ship_only_to != null) nextShipOnlyTo[row.village_id] = row.ship_only_to
    if (row.stock_floor_fraction != null) nextStockFloors[row.village_id] = row.stock_floor_fraction
    // Replaced wholesale where the file has one, not merged per resource: half
    // of an old spend under a new profile is a figure nobody entered, and a
    // stale leftover resource silences a real overflow.
    if (row.consumption_per_hour != null) {
      nextConsumption[row.village_id] = row.consumption_per_hour
    }
  }

  const stillUnknown = []
  for (const village of villages ?? []) {
    if (nextTradeOffice[village.village_id] == null) {
      stillUnknown.push({ village_id: village.village_id, name: village.name ?? '' })
    }
  }

  // A profile the file names replaces the one on screen wholesale, because half
  // of an old Day profile merged into a new one is a distribution nobody
  // designed. Profiles the file does not mention are left exactly as they are.
  const nextProfiles = { ...(profiles ?? {}) }
  const nextWindows = { ...(profileWindows ?? {}) }
  const profilesLoaded = []
  const droppedVillages = new Set()
  for (const [name, alloc] of Object.entries(setup.profiles ?? {})) {
    const pruned = {}
    for (const [resource, per] of Object.entries(alloc)) {
      const kept = {}
      for (const [vid, a] of Object.entries(per)) {
        // Same rule as the per-village rows: an id the account no longer has
        // would 400 every plan call, so it is dropped and counted, not kept.
        if (known.has(Number(vid))) kept[vid] = a
        else droppedVillages.add(Number(vid))
      }
      if (Object.keys(kept).length) pruned[resource] = kept
    }
    nextProfiles[name] = pruned
    profilesLoaded.push(name)
  }
  for (const [name, pair] of Object.entries(setup.profileWindows ?? {})) {
    nextWindows[name] = pair
  }

  // A role the file names replaces the template on screen wholesale, on the
  // same rule the profiles follow and for the same reason: half of an old
  // defensive profile merged into a new one is a distribution nobody designed.
  // Roles the file does not mention are left exactly as they are.
  const nextRoles = { ...(roles ?? {}) }
  const rolesLoaded = []
  for (const [name, template] of Object.entries(setup.roles ?? {})) {
    nextRoles[name] = template
    rolesLoaded.push(name)
  }

  return {
    tradeOffice: nextTradeOffice,
    cropCeilings: nextCropCeilings,
    shipOnlyTo: nextShipOnlyTo,
    stockFloors: nextStockFloors,
    consumption: nextConsumption,
    villageRoles: nextVillageRoles,
    roles: nextRoles,
    profiles: nextProfiles,
    profileWindows: nextWindows,
    merchantModel: setup.merchantModel ?? null,
    // Replaced wholesale, not merged. Merging two tribute lists would either
    // double an obligation or leave a target the operator deleted still being
    // shipped to. A file with no targets leaves what is on screen alone.
    foreignTargets: setup.foreignTargets ?? foreignTargets ?? [],
    report: {
      loaded,
      missingFromAccount,
      stillUnknown,
      profilesLoaded,
      rolesLoaded: rolesLoaded.sort(),
      profileVillagesDropped: [...droppedVillages].sort((a, b) => a - b),
    },
  }
}

/** Does this file belong to the account currently connected? */
export function setupMatchesAccount(setup, accountKey) {
  if (!setup.account || !accountKey) return true // nothing to contradict
  return setup.account === accountKey
}

/** Filename for an export, stamped so successive saves do not overwrite. */
export function setupFilename(playerName, stamp) {
  const who = (playerName || 'account').replace(/[^A-Za-z0-9_-]+/g, '-')
  return `travian-planner-${who}-${stamp}.json`
}
