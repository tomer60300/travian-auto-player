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
 * Version 4 adds `may_relay` to the village row: whether THIS village may
 * forward someone else's cargo, over its role template's answer. Per village
 * because the case is singular -- the account whose one defensive village sits
 * on the only road to a corner of the map wants that village relaying, not all
 * four of them. `false` is written rather than dropped, because keeping one
 * village out of a tier its role permits is the asymmetric half worth carrying.
 * The version rises for it: a v3 build would drop the field in silence, and a
 * relay permission lost that way is a route set the operator never sees change.
 *
 * Version 5 adds `max_busy_merchants` to the village row -- the most merchants
 * that village may have underway or returning at once (profile section 5:
 * "maximum 8 busy at 02") -- and the account-wide `merchant_reserve` /
 * `merchant_headroom` to the merchant model. The cap is the field whose loss is
 * least visible of any here: a Trade Office level dropped on a cleared origin
 * makes the plan over-provision, which is safe, while a dropped CAP makes the
 * plan commit sixteen merchants at a village the operator holds to eight and
 * report the sheet as feasible. So the version rises for it, and a build that
 * cannot read one refuses the file rather than half-loading it. A cap of 0 is
 * carried like any other figure: it makes every route from that village a
 * budget breach, and does not withdraw the village from the plan.
 *
 * Version 6 adds `relay_for` to the village row: the villages this one FORWARDS
 * the capital's lumber, clay and iron on to (profile section 5's relay tier).
 * It is not a preference and losing it is not a cosmetic loss -- 02 may reach
 * only its own neighbours, so without the tier the defensive villages beyond
 * them are simply unreachable and the plan comes back INFEASIBLE with a
 * shortfall each. A v5 build would drop the field in silence and report those
 * shortfalls as though the operator had never answered, which is the worst of
 * the available outcomes: the answer is on screen and the plan says it is
 * missing. So the version rises, and a build that cannot read one refuses the
 * file rather than half-loading it.
 *
 * An empty list is REFUSED rather than carried, which is the one place this
 * field differs from `ship_only_to`: an empty whitelist is a real answer
 * ("ships to nobody"), while "forwards to nobody" says nothing that leaving the
 * field off does not already say.
 *
 * Version 7 adds `npc_attended`, one answer per profile, as a sibling map
 * beside `profile_windows` rather than a field on the window pair -- so a
 * profile can carry an answer before it has hours and the two stay
 * independently absent.
 *
 * Version 8 adds `overnight`, the third map in that family, travelling the same
 * way: which profile is the one the operator sleeps through, over the
 * derivation from a window that wraps past midnight.
 *
 * Version 9 adds `reserved_window`, the minutes of the day to keep clear of
 * ARRIVALS so the operator's manual NPC burst is not competing with merchants
 * landing. A PAIR and not a fourth map beside the three above, and that is the
 * whole reason it sits at the top level: it is one person at one marketplace.
 * The attendance answer is per profile because the operator is awake for some
 * windows and not others; when they sit down to trade is not a property of a
 * window at all.
 *
 * It earns a version on the same rule as the three before it and for the same
 * consequence: an older build loads the document, drops the window, and the
 * operator saves from there -- at which point the burst competes with arrivals
 * again with nothing on screen saying the answer was lost. Until this it was
 * carried by NEITHER persistence path: it lived only in localStorage, which is
 * per browser origin, so it did not follow the operator between :80, :8001, the
 * LAN address and Tailscale -- the exact failure the page's own copy warns
 * about two panels earlier.
 *
 * Everything here is pure, including the timestamp, which is passed in rather
 * than read. That keeps the round trip testable without a browser.
 */

import { RESOURCE_LABEL, ROLE_LABEL } from '../constants/planner'
// `npc_feedstock`'s own rule, imported rather than restated. The module note
// above says contract constants live here beside the parser that enforces them,
// and this is the one exception with a reason: `plannerNpc.js` is the module
// for that field -- it also builds the request row and the picker's summary
// from the same list -- so a copy here would be the second definition of a
// wire format, which is exactly what that note exists to prevent. The
// dependency runs one way only; nothing in `plannerNpc.js` reads this file.
import { NPC_FEEDSTOCK_RESOURCES, isFeedstockList } from './plannerNpc'
import { excludedOriginIds, namesForVillageIds } from './villageRefs'

export const SETUP_FORMAT = 'travian-planner-owned-state'
export const SETUP_VERSION = 9
/** Versions this build can read. A v1 file simply carries no profiles, a v2 one
 * no roles, a v3 one no per-village relay answer, a v4 one no merchant cap, a
 * v5 one no relay tier and a v6 one no per-profile NPC attendance, so refusing
 * any of them would strand every export written before those travelled. The
 * version still has to rise when a field is added, and in the other direction:
 * a build that cannot read roles must REFUSE a file that has them, or it loads
 * the villages, drops their profiles and plans a different account without
 * saying so.
 *
 * Every version here is coupled to the server, which validates the version on
 * write: the `READABLE_VERSIONS` tuple in
 * `src/travian_api/web/routes/planner_setup.py` has to gain the same number in
 * the same breath, or every fresh export comes back 422 "NEWER build". That
 * module's own comment states the coupling from its side, and
 * `tests/test_planner_setup_store.py` pins both directions -- the parametrised
 * readable list AND two refusals that have to be asked for with a version
 * beyond this build. Bumping one side alone is the failure this note exists to
 * prevent. */
export const READABLE_VERSIONS = Object.freeze([1, 2, 3, 4, 5, 6, 7, 8, 9])

/** Matches the Trade Office input's own bounds, and the backend's `le=20`. */
export const MAX_TRADE_OFFICE_LEVEL = 20

/** Travian's hard ceiling on merchants in one village (profile section 8).
 *
 * The only bound on a merchant cap a FILE can check. The real bound is that
 * village's own `merchants_total`, which lives in the snapshot and not in the
 * file -- see `unreachableCaps`, which checks it against live state. */
export const MAX_MERCHANTS_PER_VILLAGE = 20

/** Section 6's two switches, as FRACTIONS, mirroring
 * `src/travian_api/services/distribution/night_profile.py`:
 * `DEFAULT_BASELINE_FILL = 0.25` and `DEFAULT_TARGET_FILL = 0.60`.
 *
 * The fourth deliberate second copy of a Python value in this file, and it had
 * neither a note nor a test -- unlike `SETUP_VERSION`,
 * `MAX_TRADE_OFFICE_LEVEL` and `MAX_MERCHANTS_PER_VILLAGE`, all three of which
 * are pinned by a literal on both sides. The pair lived as `useState(25)` /
 * `useState(60)` inside the page, so nothing named either and a grep for the
 * Python constant found one side of it.
 *
 * They are ONE pair of quantities seen from either side -- "never overflow
 * during the night, never arrive empty at morning" -- so the derivation and the
 * full-day check have to grade against the same two numbers. They have already
 * disagreed once: the boxes defaulted to 30/80 while the server measured
 * against 25/60, which made the derivation aim at one night and the report
 * describe another. The on-screen disagreement note is not a substitute for
 * this pin, because it compares the boxes against a day check's own response
 * and is therefore invisible until a check has RUN.
 *
 * Fractions rather than percents, because that is the unit
 * `/distribution/night-profile` and `/distribution/day-check` carry. The boxes
 * show a percent, ROUNDED off these: `0.6 * 100` is 60.00000000000001 in IEEE,
 * and an input reading that is its own defect.
 *
 * The operator owns both figures and may edit either. These are only where the
 * boxes start, and section 6 settled them at 0.25 and 0.60 on 2026-09-03. */
export const DEFAULT_BASELINE_FILL = 0.25
export const DEFAULT_TARGET_FILL = 0.6

/** Is this a usable ceiling on busy merchants?
 *
 * A whole count of merchants, from 0 to the 20 a village can ever hold. Zero is
 * accepted because it says something -- "every route from this village is a
 * budget breach" -- the same way a Trade Office level of 0 is an answer rather
 * than a blank. It does NOT say the village sends nothing: the budget is soft,
 * so the routes are still planned and the breaches are what refuse the sheet.
 * Shared by the file parser and the planner's input so the two cannot disagree
 * about what a valid cap is.
 */
export function isMaxBusyMerchants(value) {
  if (typeof value !== 'number' || !Number.isInteger(value)) return false
  return value >= 0 && value <= MAX_MERCHANTS_PER_VILLAGE
}

/** Caps that name more merchants than their village actually fields.
 *
 * The bound the file cannot enforce: a village's merchant count comes from the
 * snapshot, so 20 is a legal figure in a file and still wrong for the village
 * with 19. The backend refuses such a plan with a 422 naming the village, and
 * this is the same fact computed from live state -- so the operator sees it on
 * the cell they typed instead of on their next plan.
 *
 * A village whose merchant count was never read has no bound to fail and is
 * left alone. Unknown is not zero, and inventing a bound would flag a cap that
 * may be perfectly correct. `/snapshot` writes that unknown as 0 -- it warns
 * "no merchant count read for ..." beside it rather than claiming the village
 * has no merchants -- so 0 is skipped here, as the backend's own reachability
 * check skips it. An absent field is skipped too, though no snapshot emits one.
 */
export function unreachableCaps(maxBusy, villages) {
  const out = []
  for (const village of villages ?? []) {
    const cap = maxBusy?.[village.village_id]
    const fleet = village.merchants_total
    if (cap == null || typeof fleet !== 'number' || fleet === 0) continue
    if (cap > fleet) {
      out.push({
        village_id: village.village_id,
        name: village.name ?? '',
        cap: Number(cap),
        merchants_total: fleet,
      })
    }
  }
  return out
}

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

/* ── The merchant model's own bounds ──────────────────────────────────────
 *
 * Six boxes on the World & merchants row, and every bound below is the plan
 * request's: `merchant_base_capacity` gt 0, `trade_office_bonus_per_level`
 * ge 0, `merchant_reserve` 0-20, `merchant_headroom` under 1, `map_span` odd
 * and gt 0, `speed_fields_per_hour` gt 0.
 *
 * One predicate each, because three readers have to agree about them and two of
 * them already did not: `parseSetup` REFUSED a `merchant_reserve` of 25 from a
 * file while the box beside it accepted the same 25 from a keystroke and sent
 * it, so one number was invalid on import and valid on typing. `min`/`max` on a
 * number input bound the spinner and nothing else -- a typed or pasted figure
 * sails past both -- so the bound has to be a predicate, and it has to be THIS
 * predicate rather than a second copy of it.
 */

/** A merchant's cargo, in units. Positive: a capacity of 0 carries nothing. */
export function isMerchantBaseCapacity(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

/** Extra cargo per Trade Office level, as a fraction. 0 is a real answer -- a
 *  world with no Trade Office scaling -- so only negative is refused. */
export function isTradeOfficeBonus(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

/** Merchants held idle at every village. Whole merchants, and bounded above by
 *  the 20 a village can ever hold: a reserve past it holds back merchants no
 *  village has, which takes every budget to 0 while the request still reads as
 *  valid. */
export function isMerchantReserve(value) {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= 0 &&
    value <= MAX_MERCHANTS_PER_VILLAGE
  )
}

/** The share of each village's budget left uncommitted, as a FRACTION (the box
 *  types a percent). Below 1: at 1 the whole budget is held clear and every
 *  route is billed as crowding, which is not a plan. */
export function isMerchantHeadroom(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value < 1
}

/** The world's width in fields. ODD, because a Travian world is centred on
 *  0|0 -- an even span shifts every tile index by half a field and silently
 *  skews every distance the geometry computes from it. */
export function isMapSpan(value) {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 && value % 2 === 1
}

/** A merchant's travel speed in fields per hour. Positive: at 0 nothing ever
 *  arrives, and the backend refuses it rather than dividing by it. */
export function isMerchantSpeed(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

/** What each merchant-model box is allowed to hold, said in the words the cell
 *  under it prints.
 *
 * Keyed by field so one call answers the whole row, and skipping `null` on the
 * way: every one of these is an override, and an empty box means "use the
 * planner's own value" rather than a figure to check. Consumed by the boxes'
 * own inline messages AND by the gate on Build plan, so the page cannot mark a
 * cell it then agrees to send.
 */
export function merchantModelProblems(model) {
  const out = {}
  const check = (field, ok, rule) => {
    const raw = model?.[field]
    if (raw == null || raw === '') return
    if (!ok(Number(raw))) out[field] = rule
  }
  check('base_capacity', isMerchantBaseCapacity, 'more than 0')
  check('bonus_per_to_level', isTradeOfficeBonus, '0 or more')
  check(
    'merchant_reserve',
    isMerchantReserve,
    `0 to ${MAX_MERCHANTS_PER_VILLAGE} whole merchants`
  )
  check('merchant_headroom', isMerchantHeadroom, 'under 100%')
  check('map_span', isMapSpan, 'odd — a world is centred on 0|0')
  check('speed_fields_per_hour', isMerchantSpeed, 'more than 0')
  return out
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

/** Roles that may NOT be a relay, and the backend's `Role` names for them.
 *
 * Profile section 5.9: role villages may not relay. A feeder may, and so may a
 * village with no role declared, which is most of an account nobody has
 * described yet. Derived from `VILLAGE_ROLES` rather than typed out, so a sixth
 * role cannot be added on one side only.
 */
export const ROLES_THAT_MAY_NOT_RELAY = Object.freeze(
  VILLAGE_ROLES.filter((role) => role !== 'feeder')
)

/** What a village's `may_relay` resolves to when the village says nothing.
 *
 * The resting state of this field is not a blank, it is an ANSWER, and which
 * answer depends on three things the operator cannot see from the cell: the
 * village's role, whether that role's template states a permission, and -- for
 * a village with no role at all -- the sign of its own crop rate. The
 * backend's order, mirrored: the village's own answer, then its role
 * template's, then `default_may_relay(role)`, then `crop_per_hour >= 0`.
 *
 * So the "unset" option names the answer rather than saying "default", which
 * is the same rule the feedstock picker follows for "derived": an option that
 * hides which of three things it means is a control the operator cannot use.
 */
export function describeRelayPermission({ role, template, cropPerHour }) {
  if (role != null) {
    if (template?.may_relay != null) {
      return `${ROLE_LABEL[role] ?? role} template (${template.may_relay ? 'may' : 'may not'})`
    }
    return `Role default (${ROLES_THAT_MAY_NOT_RELAY.includes(role) ? 'may not' : 'may'})`
  }
  // A village with no role falls to the crop sign, which the snapshot states --
  // so it can be named here rather than left as "derived from something".
  if (typeof cropPerHour !== 'number' || !Number.isFinite(cropPerHour)) {
    return 'From the crop sign'
  }
  return `From the crop sign (${cropPerHour >= 0 ? 'may' : 'may not'})`
}

/** Everything wrong with a declared relay tier, BY THE RELAY that has to change.
 *
 * The same six refusals the backend makes at the schema, computed from live
 * state so the operator reads them on the cells they typed instead of on their
 * next plan -- exactly what `unreachableCaps` above does for the merchant cap,
 * and for the same reason: a 422 arriving from a plan call names a village in a
 * 26-row table and nothing on screen points at it.
 *
 * Keyed by the relay rather than returned flat, because a relay's own list is
 * the cell that has to change in every one of the six cases -- including the
 * chain, whose message names two villages but whose fix is to move a downstream
 * off one of their lists, and the downstream two relays both claim, where
 * either list will do and the message names both. A flat list would have to be
 * attributed back to a cell by matching its prose, which is the kind of thing
 * that works until someone rewords a sentence.
 *
 * `relayFor` is `{ [village_id]: number[] }`, `villageRoles` is
 * `{ [village_id]: role }`. A village with an EMPTY list is deliberately not a
 * problem: the picker holds one for the moment between opening and the first
 * tick, and flagging that would make the control shout at its own resting
 * state. `buildPlanPayload` drops an empty list and the file parser refuses
 * one, which are the two places it could do harm.
 */
export function relayTierProblemsByVillage(relayFor, villages, villageRoles) {
  const known = new Set((villages ?? []).map((v) => v.village_id))
  const named = (id) => {
    const found = (villages ?? []).find((v) => v.village_id === id)
    return found?.name || String(id)
  }
  const relays = new Set(
    Object.entries(relayFor ?? {})
      .filter(([, list]) => list?.length)
      .map(([vid]) => Number(vid))
  )
  const out = {}
  for (const relay of [...relays].sort((a, b) => a - b)) {
    const downstream = relayFor[relay] ?? []
    const problems = []
    const missing = downstream.filter((vid) => !known.has(vid))
    if (missing.length) {
      problems.push(
        `${named(relay)} relays for ${missing.map(named).join(', ')}, which this account ` +
          `does not have.`
      )
    }
    if (downstream.includes(relay)) {
      problems.push(`${named(relay)} is its own relay, which is not a leg.`)
    }
    const role = villageRoles?.[relay]
    if (role && ROLES_THAT_MAY_NOT_RELAY.includes(role)) {
      problems.push(
        `${named(relay)} is a relay but its role is ${ROLE_LABEL[role] ?? role}. Profile ` +
          `section 5.9 says role villages may not relay — only a feeder, or a village ` +
          `with no role.`
      )
    }
    const chained = downstream.filter((vid) => vid !== relay && relays.has(vid))
    if (chained.length) {
      problems.push(
        `${named(relay)} relays for ${chained.map(named).join(', ')}, which is itself a ` +
          `relay. One hop only — a relay may not feed a relay.`
      )
    }
    const twice = [...new Set(downstream.filter((vid) => countOf(downstream, vid) > 1))]
    if (twice.length) {
      problems.push(
        `${named(relay)} names ${twice.map(named).join(', ')} more than once. A duplicate ` +
          `is one downstream, and the tier sizes its legs from the gaps it forwards — so ` +
          `that village would be shipped its whole target once per mention and another ` +
          `downstream would go without.`
      )
    }
    if (problems.length) out[relay] = problems
  }
  // Across the lists rather than inside one. Two relays each naming the same
  // village is the same over-ship as naming it twice in one list, and neither
  // list is wrong on its own -- so the message names BOTH relays. Keyed to one
  // of them, not both: either cell can be edited, and keying it twice would
  // make the count beside the table read one problem as two.
  const claimedBy = {}
  for (const relay of [...relays].sort((a, b) => a - b)) {
    for (const vid of new Set(relayFor[relay] ?? [])) {
      if (vid !== relay) (claimedBy[vid] ??= []).push(relay)
    }
  }
  for (const [vid, owners] of Object.entries(claimedBy).sort((a, b) => a[0] - b[0])) {
    if (owners.length < 2) continue
    const [first] = owners
    ;(out[first] ??= []).push(
      `${named(Number(vid))} is a downstream of ${owners.map(named).join(' and ')}, and each ` +
        `relay sizes its legs from the whole of that village's gap — so it would be shipped ` +
        `its target twice while their other downstreams go without. One relay per downstream.`
    )
  }
  return out
}

/** How many times *value* appears in *list*. */
function countOf(list, value) {
  return list.reduce((total, entry) => (entry === value ? total + 1 : total), 0)
}

/** The same problems as one flat list, relay order, for a summary line. */
export function relayTierProblems(relayFor, villages, villageRoles) {
  return Object.values(relayTierProblemsByVillage(relayFor, villages, villageRoles)).flat()
}

/** The relay picker's one-line summary.
 *
 * Nothing stored is the default and reads as such. An EMPTY list is the picker
 * mid-edit rather than an answer -- unlike `ship_only_to`, where empty means
 * "ships to nobody" -- so it says so plainly and does not pretend a tier
 * exists. Names while they fit, then a count, matching the ships-only-to
 * summary beside it.
 */
export function describeRelayFor(downstream, villages) {
  if (downstream == null) return 'not a relay'
  if (downstream.length === 0) return 'nobody yet'
  if (downstream.length <= 2) return namesForVillageIds(downstream, villages)
  return `${downstream.length} villages`
}

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

/** Is this cell showing its ROLE's figure, untouched?
 *
 * The other half of `roleDeviates`, and the half that was missing. An override
 * was marked -- the cell gained "≠ DEF: Absolute /h 8,372" -- while the cell it
 * had been overridden FROM carried nothing at all: "Lumber value for 11" read
 * `absolute / 8372` off the DEF template with nothing saying where the figure
 * came from. So the operator could not tell, BEFORE touching a cell, that
 * touching it creates an override; and a whole role profile read as this
 * village's own entry.
 *
 * The Account stage already solved this one stage over, per `describeSpendSource`
 * -- "from DEF" on a spend the village never typed. This is the same answer for
 * an allocation.
 *
 * `allocation == null` is the whole test on the village's side: an own entry is
 * an override whether or not it happens to agree with the template, because it
 * is what the next template edit will no longer reach.
 */
export function roleInherits(template, resource, allocation) {
  return allocation == null && template?.allocations?.[resource] != null
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
  // `?? 0` on BOTH sides, because `Number(undefined)` is NaN and NaN !== NaN:
  // an allocation missing `value` read as a deviation on every render, and the
  // backend would never report it -- `AllocationInput.value` defaults to 0.0,
  // so `{mode: 'keep'}` and `{mode: 'keep', value: 0}` are one allocation
  // there. A cell marked "different from DEF" over a figure the plan agrees
  // with is the confidently-wrong mark this predicate exists to avoid.
  return (
    stated.mode !== allocation.mode ||
    Number(stated.value ?? 0) !== Number(allocation.value ?? 0)
  )
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

/** Does this template say NOTHING -- so that the role having one is a fiction?
 *
 * The role key outlives the last figure in it. Every setter in the panel writes
 * through `{...prev, [role]: {...}}` and not one of them deletes the role when
 * its last box is cleared, so an emptied template survives as
 * `{"def": {"consumption": {}}}` -- and that is a template as far as any
 * `!= null` check can tell. The backend agreed, and planned four defensive
 * villages at their own 1,500/h with spend 0 and an empty `role_deviations`,
 * feasible; the page's warning agreed too, and said nothing, because it read
 * the same key. `Clear` was the only door that reached the 422.
 *
 * So the question both readers have to ask is not "is there a key" but "is
 * there a figure", and they have to ask it the same way -- two predicates that
 * disagree is the defect, not the mechanism. Answering it here means the plan
 * is refused where the operator can see why, and the panel names the role.
 *
 * Two things count as figures that do not look like ones:
 *
 * * `may_relay: false` -- unset means "take the role's own default", so false
 *   is the operator overriding it. It is the whole template on the account whose
 *   one defensive village sits on the only road to a corner of the map.
 * * `consumption: {lumber: 0}` -- zero is a measured claim about a village that
 *   spends none, which is why `setTemplateSpend` deletes an emptied box rather
 *   than storing a zero.
 *
 * And one that does: `crop_negative_by_design: false`, which is what unticking
 * the checkbox stores. Absent and false are the same answer there -- the finding
 * keeps its severity either way -- so the pair cannot be distinguished and false
 * has to read as silence.
 */
export function isEmptyTemplate(template) {
  if (template == null) return true
  if (Object.keys(template.allocations ?? {}).length > 0) return false
  if (Object.keys(template.consumption ?? {}).length > 0) return false
  if (template.may_relay != null) return false
  // An assumption alone IS a template. It moves no target, no cargo and no
  // merchant -- but it is the figure section 9's drift check compares reality
  // against, and an "empty" template is SKIPPED from the request entirely, so
  // reading it as empty would silently drop the only thing it can do. 0.0 is a
  // real claim ("this village breaks even") and is checked as one; null means
  // no assumption, which is not an assumption of zero.
  if (template.assumed_crop_per_hour != null) return false
  return !template.crop_negative_by_design
}

/** Is this a usable crop assumption?
 *
 * Any finite number, sign included. NEGATIVE IS THE NORMAL CASE on the roles
 * that matter -- 01 reads -5,880/h and is crop-negative by design -- so a
 * non-negative rule here would refuse the account's own figures. `null` is not
 * a rate and is handled by the caller: it means no assumption, and the village
 * is then not drift-checked at all.
 */
export function isAssumedCropRate(value) {
  return typeof value === 'number' && Number.isFinite(value)
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
 * An EMPTIED template is skipped on the same rule and for the same outcome, per
 * `isEmptyTemplate`: it would arrive as `{}` and be accepted, which is the same
 * silent plan by another door.
 *
 * A template that IS present and half typed is sent whole, with its four
 * halves spelled out rather than spread: a template is a template from the
 * moment the operator gives a role any figure at all, and the page's own
 * "no template yet" warning reads the same predicate. Its crop spend is dropped
 * on the way out, because the backend refuses one and a template stored by an
 * older build could still carry it -- which would 422 every plan over a figure
 * the editor no longer shows.
 */
export function rolesForRequest(roleTemplates, claimedRoles) {
  const out = {}
  for (const role of claimedRoles ?? []) {
    const template = roleTemplates?.[role]
    if (isEmptyTemplate(template)) continue
    out[role] = {
      allocations: template.allocations ?? {},
      consumption: materialSpendOnly(template.consumption) ?? {},
      may_relay: template.may_relay ?? null,
      crop_negative_by_design: Boolean(template.crop_negative_by_design),
      // Omitted where there is none, and NEVER coerced to 0: the backend reads
      // a missing figure as "no assumption, do not drift-check this village",
      // and reads 0 as the claim that it breaks even. Sending 0 for silence
      // would flag every village on every account that has never typed one.
      ...(isAssumedCropRate(template.assumed_crop_per_hour)
        ? { assumed_crop_per_hour: template.assumed_crop_per_hour }
        : {}),
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

/** Rehydrate the stored per-village relay flags, keeping only real booleans.
 *
 * This was the one hydrated map loaded raw. Its three neighbours in the same
 * effect are all filtered on the way in -- `stripUnknownRoles`,
 * `stripStoredCropSpends` and the merchant model's defaults merge -- and the
 * FILE path refuses a non-boolean `may_relay` outright with a `SetupFileError`.
 * localStorage was the door left open, and it leads straight into the request:
 * `may_relay: mayRelay[vid]` is sent whenever the entry is not null.
 *
 * What a bad value costs is asymmetric, which is why it is dropped rather than
 * coerced. The backend's `bool` is lax, so `"yes"` and `1` arrive as TRUE and
 * the plan may then route someone else's crop through a village on the strength
 * of a string nobody can see -- profile section 5.9's whole point is that only a
 * feeder relays. Anything the lax read rejects 422s the entire plan instead,
 * over a field the planner has no box for: nothing here writes this map except
 * a setup-file import, so there is no control to clear the value from.
 *
 * `false` is kept, because it is an answer: unset means "take the role's own
 * default", so false is the operator overriding that default. `null` is not,
 * for the same reason the request reads `!= null` as absent -- a village with
 * nothing to declare leaves the map rather than holding a key that says nothing.
 *
 * Dropped SILENTLY, unlike its two neighbours, and the difference is that they
 * have something on screen to reconcile: a stripped crop spend un-silences a
 * CRITICAL the operator typed, and a stripped role empties boxes they can see.
 * A relay flag has no control in the planner at all, so a receipt would name a
 * village and point at nothing.
 */
export function relayFlagsOnly(stored) {
  const out = {}
  if (!stored || typeof stored !== 'object') return out
  for (const [villageId, flag] of Object.entries(stored)) {
    if (typeof flag === 'boolean') out[villageId] = flag
  }
  return out
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

/** The role templates as a DOCUMENT carries them: materials only.
 *
 * `stripStoredCropSpends`' twin, one level up. Crop cannot be declared as a
 * spend -- the snapshot's crop rate is already net of troop upkeep, so a
 * declared crop spend subtracts the same troops twice -- and the input stopped
 * offering it, which leaves a figure an older build stored with no box to clear
 * it from.
 *
 * The REQUEST path has stripped it since `rolesForRequest`, and the document
 * path did not. So the plan ran, the save came back 422 on
 * `roles.def.consumption`, and the export wrote a file `parseRoleTemplate`
 * refuses -- the operator's backup unreadable by their own build, over a figure
 * they cannot see.
 *
 * A template whose ONLY spend was crop is left declaring nothing rather than
 * dropped: its allocations are the reason it exists, and section 2.1's one
 * profile stands in for four defensive villages. A template with no
 * `consumption` key at all keeps none, because writing `{}` in would make a
 * template that says nothing look like one declaring a spend of nothing.
 *
 * No receipt, unlike the village-level twin, and the difference is that there
 * is nothing on screen to reconcile: the request has been dropping this figure
 * all along, so the document catching up changes no plan.
 */
export function storedRoleTemplates(templates) {
  const out = {}
  for (const [role, template] of Object.entries(templates ?? {})) {
    if (!template || typeof template !== 'object' || template.consumption == null) {
      out[role] = template
      continue
    }
    out[role] = { ...template, consumption: materialSpendOnly(template.consumption) ?? {} }
  }
  return out
}

/** One `HH:MM` box, filled in. The single copy of this shape in the file --
 *  `parseClockPair` reads it too, because two copies of a clock-time regex is
 *  how one of them comes to accept "6am". */
const isClockTime = (value) => /^([01]\d|2[0-3]):[0-5]\d$/.test(String(value))

/** The profile windows a DOCUMENT can carry: both boxes typed.
 *
 * A window is two `HH:MM` inputs, so there is a moment where only one of them
 * is filled in. The REQUEST has always handled it -- `dispatchWindowFor`
 * collapses the unusable shapes to null and `buildSegments` skips the profile --
 * while the writer stored the pair raw, and `["07:00", ""]` is refused by this
 * file's own `parseClockPair` and by the server's `_ClockTime`. So "Save setup
 * to server" failed for as long as one box was mid-edit.
 *
 * NOT `dispatchWindowFor`, deliberately: that also collapses a ZERO-WIDTH pair,
 * because a request has nothing to do with one. A document is what the operator
 * typed, both boxes are filled, and the parser and the server both take it --
 * dropping it would lose typed state over a rule that belongs to the request.
 */
export function storedProfileWindows(windows) {
  const out = {}
  for (const [name, pair] of Object.entries(windows ?? {})) {
    if (!Array.isArray(pair) || pair.length !== 2) continue
    if (!pair.every(isClockTime)) continue
    out[name] = [String(pair[0]), String(pair[1])]
  }
  return out
}

/** A coordinate the operator actually typed. Blank is not 0.
 *
 * `Number('') || 0` turned a cleared box into (0|0) -- the middle of the map --
 * while the box on screen still read blank, so a half-typed tribute was planned
 * against a village that is not where it is, with the distance, the cycle and
 * the merchant count all computed from the wrong tile.
 */
const hasCoord = (value) => String(value).trim() !== '' && Number.isFinite(Number(value))

/** Is this row the operator mid-edit rather than an obligation?
 *
 * `+ Add target` seeds a row with no name, no rate and 0|0, and it is filled in
 * from there. Shared by the plan request, the table's own "incomplete" badge and
 * the document writer, so all three agree about which rows exist -- it lived in
 * the page, where the document writer could not reach it, which is how a fresh
 * draft came to make the whole setup unsaveable.
 */
export function foreignTargetIsDraft(target) {
  return (
    !String(target?.name ?? '').trim() ||
    !(Number(target?.crop_per_hour) > 0) ||
    !hasCoord(target?.x) ||
    !hasCoord(target?.y)
  )
}

/** The foreign targets as a DOCUMENT carries them: exclusions RESOLVED to ids.
 *
 * The page holds each exclusion twice -- `exclude_origins` as ids, and
 * `exclude_origins_text` as whatever is in the input box, so it can hold a
 * half-finished name. Only the ids are a wire field. Writing the page's targets
 * verbatim therefore put a typed exclusion into the document as text alone, and
 * `parseForeignTargets` reads only the ids, so it came back empty: the operator
 * excluded a hub from a tribute, saved, reloaded, and the hub supplied it again
 * with nothing on screen saying the answer had gone.
 *
 * `excludedOriginIds` is the same resolution the plan request uses, imported
 * rather than restated -- its rule is that the typed text wins where it EXISTS,
 * even empty, because clearing the box is a deliberate act, while a target
 * loaded from a file has ids and no text at all.
 *
 * The text itself is NOT written. A document carries answers, not the state of
 * an input box, and a name the account does not have resolves to nothing --
 * which the cell already marks and `planBlockers` already refuses the plan over.
 * Omitted when it resolves to nothing at all, on the same rule the rest of this
 * file follows: the parser reads absent as "no exclusion", so writing an empty
 * list would only make the document look like it had an answer.
 */
export function storedForeignTargets(targets, villages) {
  return (targets ?? [])
    // A draft is the operator mid-edit, not in error, so it is left out rather
    // than refused -- the same rule and the same predicate the plan request
    // uses. Written raw, pressing "+ Add target" made the whole setup
    // unsaveable: the PUT 422'd on `name` and `crop_per_hour`, and the export
    // wrote a file `parseForeignTargets` refuses with "has no name".
    .filter((target) => !foreignTargetIsDraft(target))
    .map((target) => {
      const excluded = excludedOriginIds(target, villages)
      // Rebuilt field by field, as the request row is, so what the page happens
      // to be holding cannot leak into the document. The boxes hand back
      // STRINGS, and a string is what broke the cadence: the select writes
      // `e.target.value`, so going back to "any" stored `''`, `Number('')` is 0,
      // and 0 is not one of Travian's repeat intervals -- refused by the parser
      // and by the server over a control reading "any" on screen.
      return {
        name: String(target.name).trim(),
        x: Number(target.x),
        y: Number(target.y),
        crop_per_hour: Number(target.crop_per_hour),
        safety_margin_pct: Number(target.safety_margin_pct) || 0,
        route_eligible: Boolean(target.route_eligible),
        ...(Number(target.max_cycle_hours) > 0
          ? { max_cycle_hours: Number(target.max_cycle_hours) }
          : {}),
        ...(excluded.length ? { exclude_origins: excluded } : {}),
      }
    })
}

export class SetupFileError extends Error {}

/** Build the exportable document. Villages with nothing typed are left out. */
export function buildSetup({
  account,
  villages,
  tradeOffice,
  maxBusy,
  cropCeilings,
  shipOnlyTo,
  relayFor,
  stockFloors,
  npcFeedstock,
  consumption,
  villageRoles,
  mayRelay,
  roles,
  profiles,
  profileWindows,
  npcAttended,
  overnight,
  reservedWindow,
  merchantModel,
  foreignTargets,
  exportedAt,
}) {
  const rows = []
  for (const village of villages ?? []) {
    const level = tradeOffice?.[village.village_id]
    const cap = maxBusy?.[village.village_id]
    const ceiling = cropCeilings?.[village.village_id]
    const allowed = shipOnlyTo?.[village.village_id]
    const forwards = relayFor?.[village.village_id]
    const floor = stockFloors?.[village.village_id]
    // Only a usable override travels. An empty list is the picker mid-edit,
    // and "derived" -- the resting state -- is said by leaving the field off.
    const feedstock = isFeedstockList(npcFeedstock?.[village.village_id])
      ? npcFeedstock[village.village_id]
      : null
    const role = villageRoles?.[village.village_id]
    const relay = mayRelay?.[village.village_id]
    // Materials only, so an export can never write a file this same parser
    // refuses to read back -- a crop figure saved by an older build would do
    // exactly that.
    const spends = materialSpendOnly(consumption?.[village.village_id])
    if (
      level == null &&
      cap == null &&
      ceiling == null &&
      allowed == null &&
      !forwards?.length &&
      floor == null &&
      feedstock == null &&
      role == null &&
      relay == null &&
      !spends
    ) {
      continue
    }
    const row = { village_id: village.village_id, name: village.name ?? '' }
    if (role != null) row.role = String(role)
    if (level != null) row.trade_office_level = Number(level)
    // 0 is written, not dropped: it says every route from this village is a
    // budget breach, which is an answer, and dropping it puts the village's
    // whole fleet back to work.
    if (cap != null) row.max_busy_merchants = Number(cap)
    if (ceiling != null) row.crop_ceiling = Number(ceiling)
    // An empty list is written, not dropped: it says "ships to nobody", which
    // is a different answer from the unrestricted default an absent field means.
    if (allowed != null) row.ship_only_to = allowed.map(Number)
    // The opposite rule, and the one place this field differs from the
    // whitelist above: an EMPTY relay list is dropped rather than written,
    // because "forwards to nobody" says nothing that leaving the field off does
    // not already say -- and the file parser and the backend both refuse one, so
    // writing it would produce a file this same parser will not read.
    if (forwards?.length) row.relay_for = forwards.map(Number)
    if (floor != null) row.stock_floor_fraction = Number(floor)
    // Written in the game's own resource order rather than the click order, so
    // two operators who ticked the same two stores export the same document --
    // the same reason the request row orders it.
    if (feedstock != null) {
      row.npc_feedstock = NPC_FEEDSTOCK_RESOURCES.filter((r) => feedstock.includes(r))
    }
    // false is written, not dropped: it is the answer that keeps ONE village
    // out of the relay tier its role otherwise permits, which is exactly the
    // asymmetric half of the setting worth carrying.
    if (relay != null) row.may_relay = Boolean(relay)
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
  //
  // Through `storedRoleTemplates`, so a stored crop spend cannot travel: the
  // request path has stripped it since `rolesForRequest`, while this one wrote
  // it raw -- the plan ran, the save 422'd on `roles.<role>.consumption`, and
  // the export wrote a file this file's own parser refuses.
  if (roles && Object.keys(roles).length) doc.roles = storedRoleTemplates(roles)
  // Only the pairs with both boxes filled in. A half-typed window is the
  // operator mid-edit, and written raw it made the whole document unsaveable:
  // `["07:00", ""]` is refused by `parseClockPair` here and by `_ClockTime` on
  // the server.
  const windows = storedProfileWindows(profileWindows)
  if (Object.keys(windows).length) doc.profile_windows = windows
  // Beside the hours, because it is a question ABOUT the hours: "who is
  // trading during this window". A third sibling map keyed by profile name
  // rather than a field on the window pair, so a profile can carry an answer
  // before it has hours and the two stay independently absent.
  //
  // Omitted when empty on the same rule as the two above, and here the rule is
  // load-bearing rather than tidy: absent is the THIRD state -- "not answered
  // yet" -- and it is the state that refuses a plan instead of funding a night
  // nobody is awake for. An empty map written into the file would import as
  // every profile having answered nothing, which reads identically on screen.
  if (npcAttended && Object.keys(npcAttended).length) {
    doc.npc_attended = npcAttended
  }
  // Section 6's own per-profile answer, a third sibling map beside the hours
  // and the attendance, and omitted-when-empty on the same rule: absent is
  // "derive it from the window", which is right for a night stated as one
  // 23:00-07:00 pair and wrong for the half of a split night that wraps in
  // neither direction. An empty map would import as every profile having
  // declared nothing, which reads identically and is a different document.
  //
  // v8, by the rule this file states above: a build that cannot read it loads
  // the document, drops the declaration, and the operator saves from there --
  // at which point the split night silently measures its 60% morning floor at
  // 00:00 again.
  if (overnight && Object.keys(overnight).length) {
    doc.overnight = overnight
  }
  // v9, and the only owned answer that used to be carried by NEITHER
  // persistence path: it lived in localStorage alone, which is per browser
  // origin, so it did not follow the operator between :80, :8001, the LAN
  // address and Tailscale -- the failure the page's own copy warns about two
  // panels earlier. Confirmed against a real saved document, whose top level
  // held every other owned field and not this one.
  //
  // A PAIR at the top level, not a fourth map beside the three above: the
  // window is account-wide because it is one person at one marketplace, where
  // the attendance answer is per profile because the operator is awake for some
  // windows and not others.
  //
  // Omitted when unset on the same rule as the maps above, and here the rule is
  // simply honest: absent means "reserve nothing", which is what a half-typed
  // pair would mean too -- so writing one would only make the document look
  // like it had an answer. What is stored is what was TYPED, because a document
  // is what the operator sees in the boxes; `dispatchWindowFor` is what the
  // plan REQUEST uses to decide whether the pair reserves anything.
  if (Array.isArray(reservedWindow) && reservedWindow.length === 2) {
    doc.reserved_window = [String(reservedWindow[0]), String(reservedWindow[1])]
  }
  // The levers the operator actually TYPED. A blank box means "use the
  // planner's own", which the plan path has always read correctly -- the field
  // is omitted from the request and the backend's default stands -- while this
  // writer stored the model wholesale, so an emptied box travelled as an
  // `undefined` key the server answered with 422 "Field required" and this
  // file's own reader refused. Nothing is filled in: writing 2,500 into an
  // emptied box would make it look like a calibration the operator asserted,
  // and that figure sizes every cargo the account ships.
  //
  // Omitted entirely when nothing is typed, on the rule the rest of this
  // document follows: `{}` would import as a model asserting nothing, which
  // reads identically to having none.
  const levers = {}
  for (const [field, value] of Object.entries(merchantModel ?? {})) {
    if (value != null && value !== '') levers[field] = value
  }
  if (Object.keys(levers).length) doc.merchant_model = levers
  // A tribute is entirely operator-supplied -- the game will not say that an ally
  // needs 25,700 crop an hour -- and it drives real routes. Losing it to a cleared
  // origin means the obligation silently stops being planned for.
  //
  // Through `storedForeignTargets` rather than verbatim, because the page holds
  // each exclusion as ids AND as the text in its input box, and only the ids are
  // a wire field. Written raw, a typed exclusion reached the document as text
  // alone and came back out of the parser as nothing.
  const targets = storedForeignTargets(foreignTargets, villages)
  if (targets.length) doc.foreign_targets = targets
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
  // Refused rather than read as absent, and refused for the sign as much as
  // the type: a template whose assumption failed to parse would be silently
  // undrifted, so the one check the figure exists for stops happening while
  // the file still loads.
  if (raw.assumed_crop_per_hour != null && !isAssumedCropRate(raw.assumed_crop_per_hour)) {
    throw new SetupFileError(
      `${where}.assumed_crop_per_hour is ${JSON.stringify(raw.assumed_crop_per_hour)}; ` +
        `it must be a number -- negative is normal, since a hammer eats more crop than ` +
        `it grows -- or absent to make no assumption at all.`
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
    // null, on the same rule and for a sharper reason: 0.0 is a real claim and
    // no claim at all is not zero, so the two cannot share a representation.
    assumed_crop_per_hour: raw.assumed_crop_per_hour ?? null,
  }
}

/** Validate one `['HH:MM', 'HH:MM']` pair.
 *
 * Split out of `parseWindows` rather than copied: the reserved window is the
 * same shape asked of a different thing, and two copies of a clock-time regex
 * is how one of them comes to accept "6am".
 */
function parseClockPair(pair, where) {
  if (!Array.isArray(pair) || pair.length !== 2) {
    throw new SetupFileError(`${where} must be a [start, end] pair.`)
  }
  for (const t of pair) {
    if (!isClockTime(t)) {
      throw new SetupFileError(`${where} has "${t}", which is not HH:MM.`)
    }
  }
  return [String(pair[0]), String(pair[1])]
}

/** Validate a `{ profile: ['HH:MM', 'HH:MM'] }` window map. */
function parseWindows(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not a window map.`)
  }
  const out = {}
  for (const [name, pair] of Object.entries(raw)) {
    out[name] = parseClockPair(pair, `${where}["${name}"]`)
  }
  return out
}

/** One attendance answer per profile: a boolean, or the key is not there.
 *
 * `attendanceMapOnly` drops a non-boolean silently on the way out of
 * localStorage, and rightly: an unanswered profile is already named on screen
 * and refuses the plan. A FILE is not that. It is the operator asserting an
 * answer, and the backend's `bool` is lax enough to read a stored `"yes"` as an
 * attendance nobody declared -- which funds a night's conversion off a string.
 * Refused here, on the same discipline `npc_feedstock` follows two fields up.
 */
function parseAttendance(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not a map of answers.`)
  }
  const out = {}
  for (const [name, value] of Object.entries(raw)) {
    if (typeof value !== 'boolean') {
      throw new SetupFileError(
        `${where}["${name}"] is ${JSON.stringify(value)}, which is not an answer. ` +
          `It must be true (you are at the marketplace), false (nobody is trading), ` +
          `or absent to leave the profile unanswered.`
      )
    }
    out[name] = value
  }
  return out
}

/** One boolean per profile saying which one the operator sleeps through.
 *
 * Refused rather than coerced, on `parseAttendance`'s reasoning and for a
 * consequence of the same size: a document is the operator ASSERTING an
 * answer, and section 6's rules -- no latency target, every merchant home
 * before the window closes -- are applied to whichever profile this names.
 */
function parseOvernight(raw, where) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SetupFileError(`${where} is not a map of declarations.`)
  }
  const out = {}
  for (const [name, value] of Object.entries(raw)) {
    if (typeof value !== 'boolean') {
      throw new SetupFileError(
        `${where}["${name}"] is ${JSON.stringify(value)}, which is not a declaration. ` +
          `It must be true (this is the night you sleep through), false (this is not ` +
          `the night), or absent to let the window decide.`
      )
    }
    out[name] = value
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
    if (row.max_busy_merchants != null) {
      // Not clamped, for the reason the backend answers 422 rather than
      // trimming: a ceiling nobody can reach plans as the fleet, so the figure
      // in the file and the figure in force would differ with nothing saying
      // which is being obeyed.
      const cap = row.max_busy_merchants
      if (!isMaxBusyMerchants(cap)) {
        throw new SetupFileError(
          `${where} may have ${JSON.stringify(cap)} merchants busy at once; it must be ` +
            `a whole number from 0 to ${MAX_MERCHANTS_PER_VILLAGE}, the most a village ` +
            `can ever hold.`
        )
      }
      parsed.max_busy_merchants = cap
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
    if (row.relay_for != null) {
      if (!Array.isArray(row.relay_for)) {
        throw new SetupFileError(`${where} has a relay_for that is not a list of village ids.`)
      }
      // Refused, not read as "no tier". Unlike `ship_only_to`, where an empty
      // list is the real answer "ships to nobody", there is no reading of
      // "forwards to nobody" that differs from leaving the field off -- so
      // accepting it would let a half-typed row look like a decision, and the
      // backend answers one with a 422 naming the village.
      if (row.relay_for.length === 0) {
        throw new SetupFileError(
          `${where} has an empty relay_for. A relay for nobody is not a tier — remove the ` +
            `field, or name the villages this one forwards to.`
        )
      }
      parsed.relay_for = row.relay_for.map((raw) => {
        const id = Number(raw)
        if (!Number.isInteger(id) || id <= 0) {
          throw new SetupFileError(
            `${where} has ${JSON.stringify(raw)} in relay_for; it must be a village id.`
          )
        }
        if (id === parsed.village_id) {
          throw new SetupFileError(
            `${where} names itself in relay_for, which is not a leg. A relay forwards to ` +
              `OTHER villages.`
          )
        }
        return id
      })
    }
    if (row.may_relay != null) {
      if (typeof row.may_relay !== 'boolean') {
        throw new SetupFileError(
          `${where} has may_relay ${JSON.stringify(row.may_relay)}; it must be true, ` +
            `false, or absent to take the role template's answer.`
        )
      }
      parsed.may_relay = row.may_relay
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
    if (row.npc_feedstock != null) {
      // Refused, not trimmed, and the two ways it can be wrong get different
      // sentences: an unknown store is a typo, while an EMPTY list is a picker
      // that was opened and never ticked. Neither is an override, and the
      // backend refuses both -- so a document carrying one would load cleanly
      // here and 422 on the next plan.
      if (!Array.isArray(row.npc_feedstock)) {
        throw new SetupFileError(`${where} has an npc_feedstock that is not a list of stores.`)
      }
      for (const resource of row.npc_feedstock) {
        if (!NPC_FEEDSTOCK_RESOURCES.includes(resource)) {
          throw new SetupFileError(
            `${where}.npc_feedstock names "${resource}"; it must be one of ` +
              `${NPC_FEEDSTOCK_RESOURCES.join(', ')}.`
          )
        }
      }
      if (!isFeedstockList(row.npc_feedstock)) {
        throw new SetupFileError(
          `${where} has npc_feedstock ${JSON.stringify(row.npc_feedstock)}. It must name at ` +
            `least one store, each at most once: NPC exchanges one resource for another, so it ` +
            `can convert from neither nothing nor the same store twice. Remove the field to ` +
            `let the feedstock be derived, which is the honest default.`
        )
      }
      parsed.npc_feedstock = NPC_FEEDSTOCK_RESOURCES.filter((r) =>
        row.npc_feedstock.includes(r)
      )
    }
    if (row.consumption_per_hour != null) {
      parsed.consumption_per_hour = parseConsumption(
        row.consumption_per_hour,
        `${where}.consumption_per_hour`
      )
    }
    return parsed
  })

  // The two relay rules that need MORE THAN ONE ROW to check, so they cannot
  // live in the loop above. Both are the backend's own refusals, made here so a
  // file that would 422 on the next plan call is refused at import instead --
  // where the message can still say which row, and where the operator has not
  // yet lost the state on screen to a half-applied load.
  //
  // The village ids are NOT checked against an account here, because the file
  // does not know one: a downstream village with nothing else typed has no row
  // at all, so demanding one would refuse a perfectly good file. `mergeSetup`
  // prunes against live villages and reports what it dropped, and
  // `relayTierProblems` shows the same facts on the cells as they are typed.
  const declaredRelays = new Set(
    villages.filter((row) => row.relay_for?.length).map((row) => row.village_id)
  )
  for (const row of villages) {
    if (!row.relay_for?.length) continue
    const label = row.name || String(row.village_id)
    if (row.role && ROLES_THAT_MAY_NOT_RELAY.includes(row.role)) {
      throw new SetupFileError(
        `${label} is a relay in this file but its role is ${row.role}. Profile section 5.9 ` +
          `says role villages may not relay — only a feeder, or a village with no role. ` +
          `Refused rather than dropped: the backend answers it with a 422, and a relay ` +
          `dropped here would leave those villages unreachable with nothing saying why.`
      )
    }
    const chained = row.relay_for.filter((id) => declaredRelays.has(id))
    if (chained.length) {
      const named = chained
        .map((id) => villages.find((v) => v.village_id === id)?.name || String(id))
        .join(', ')
      throw new SetupFileError(
        `${label} relays for ${named}, which is itself a relay in this file. One hop only — ` +
          `a relay may not feed a relay, because a chain puts one hub's forward leg behind ` +
          `another's and no daily beat can order both.`
      )
    }
  }

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
  const npcAttended =
    raw.npc_attended == null ? {} : parseAttendance(raw.npc_attended, 'npc_attended')
  const overnight = raw.overnight == null ? {} : parseOvernight(raw.overnight, 'overnight')
  // null rather than an empty pair: absent means "reserve nothing", and a pair
  // is what the boxes hold. Refused rather than coerced on the same discipline
  // the window map follows -- the backend's `_ClockTime` refuses it too, so
  // accepting it here would write a file the server will not take.
  const reservedWindow =
    raw.reserved_window == null ? null : parseClockPair(raw.reserved_window, 'reserved_window')

  let merchantModel = null
  if (raw.merchant_model != null) {
    const m = raw.merchant_model
    // ABSENT is blank, not a refusal, and blank is not 0. Both boxes may be
    // emptied on the page, where it means "use the planner's own" -- the plan
    // request omits the field and the backend's default stands -- and this
    // reader demanded both, so an operator who cleared either could not load
    // their own document back. A key that IS there is still held to the same
    // bound: 0 is a claim, and it is one the backend's `gt=0` refuses.
    //
    // The predicates rather than the conditions they replaced, so the box on
    // the page and this parser cannot disagree about one number -- the defect
    // this shares with `merchant_reserve` below: 25 was refused from a file and
    // accepted from a keystroke.
    //
    // The keys are set to `undefined` rather than left off, so a document that
    // omits one loads as an EMPTY box: the page merges what it reads over its
    // own defaults, and a missing key would take the default back while an
    // explicit `undefined` carries the blank through.
    let base
    let bonus
    if (m?.base_capacity != null) {
      base = Number(m.base_capacity)
      if (!isMerchantBaseCapacity(base)) {
        throw new SetupFileError('merchant_model.base_capacity must be a positive number.')
      }
    }
    if (m?.bonus_per_to_level != null) {
      bonus = Number(m.bonus_per_to_level)
      if (!isTradeOfficeBonus(bonus)) {
        throw new SetupFileError('merchant_model.bonus_per_to_level must be zero or more.')
      }
    }
    merchantModel = { base_capacity: base, bonus_per_to_level: bonus }
    // The two account-wide merchant levers, carried only when the file has
    // them: they are the planner's own defaults otherwise, and writing a
    // default in would make an old file look like a decision.
    if (m.merchant_reserve != null) {
      const reserve = Number(m.merchant_reserve)
      // Bounded above by the 20 a village can ever hold, matching the
      // backend's `le=20` and the cap's own ceiling: a reserve past it holds
      // back merchants no village has, taking every budget to 0.
      if (!isMerchantReserve(reserve)) {
        throw new SetupFileError(
          `merchant_model.merchant_reserve is ${JSON.stringify(m.merchant_reserve)}; ` +
            `it must be a whole number of merchants, from 0 to ${MAX_MERCHANTS_PER_VILLAGE}.`
        )
      }
      merchantModel.merchant_reserve = reserve
    }
    if (m.merchant_headroom != null) {
      const headroom = Number(m.merchant_headroom)
      // Below 1, matching the backend's `lt=1.0`: at 1 the entire budget is
      // held clear and every route is billed as crowding, which is not a plan.
      if (!isMerchantHeadroom(headroom)) {
        throw new SetupFileError(
          `merchant_model.merchant_headroom is ${JSON.stringify(m.merchant_headroom)}; ` +
            `it must be a fraction from 0 up to but not including 1.`
        )
      }
      merchantModel.merchant_headroom = headroom
    }
    // The two WORLD overrides, and the pair this parser used to drop in
    // silence. `buildSetup` stores `merchant_model` WHOLESALE, so a typed
    // `map_span` or `speed_fields_per_hour` has always reached the file -- while
    // this reader rebuilt the model field by field and simply did not name
    // them. A round trip therefore lost both with no message, which is the
    // worst shape the loss can take: the span scales every distance the
    // geometry computes and the speed divides into every travel time, so an
    // operator on a non-Europe-2 world reloaded their own file and planned
    // another world's journeys.
    //
    // NO VERSION BUMP. The rule this file states above is that the version
    // rises when a FIELD IS ADDED to the document -- because an older build
    // would then load it, drop the field, and let the operator save from
    // there. Nothing is added here: the writer already emitted these keys
    // inside v9, and the server's `MerchantModelIn` ignores extras while the
    // store keeps the body verbatim, so v9 documents carrying them are already
    // saved and already readable. Only the reader changes, so this is the one
    // case where the two halves do not have to move together.
    //
    // Refused rather than coerced, on the same discipline as the four above and
    // with the same predicates the boxes use: the plan request's `map_span`
    // validator refuses an even span outright, so accepting one here would load
    // a figure every subsequent plan is refused over.
    if (m.map_span != null) {
      const span = Number(m.map_span)
      if (!isMapSpan(span)) {
        throw new SetupFileError(
          `merchant_model.map_span is ${JSON.stringify(m.map_span)}; ` +
            `it must be an odd whole number of fields -- a world is centred on 0|0, ` +
            `so its width is always odd.`
        )
      }
      merchantModel.map_span = span
    }
    if (m.speed_fields_per_hour != null) {
      const speed = Number(m.speed_fields_per_hour)
      if (!isMerchantSpeed(speed)) {
        throw new SetupFileError(
          `merchant_model.speed_fields_per_hour is ${JSON.stringify(m.speed_fields_per_hour)}; ` +
            `it must be more than 0 -- the plan divides every distance by it.`
        )
      }
      merchantModel.speed_fields_per_hour = speed
    }
  }

  const foreignTargets =
    raw.foreign_targets == null
      ? null
      : parseForeignTargets(raw.foreign_targets, "foreign_targets")

  return {
    ...raw,
    villages,
    roles,
    profiles,
    profileWindows,
    npcAttended,
    overnight,
    reservedWindow,
    merchantModel,
    foreignTargets,
  }
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
  maxBusy,
  cropCeilings,
  shipOnlyTo,
  relayFor,
  stockFloors,
  npcFeedstock,
  consumption,
  villageRoles,
  mayRelay,
  roles,
  profiles,
  profileWindows,
  npcAttended,
  overnight,
  reservedWindow,
  foreignTargets,
}) {
  const known = new Map((villages ?? []).map((v) => [v.village_id, v]))
  const nextTradeOffice = { ...(tradeOffice ?? {}) }
  const nextMaxBusy = { ...(maxBusy ?? {}) }
  const nextCropCeilings = { ...(cropCeilings ?? {}) }
  const nextShipOnlyTo = { ...(shipOnlyTo ?? {}) }
  const nextRelayFor = { ...(relayFor ?? {}) }
  const nextStockFloors = { ...(stockFloors ?? {}) }
  const nextNpcFeedstock = { ...(npcFeedstock ?? {}) }
  const nextConsumption = { ...(consumption ?? {}) }
  const nextVillageRoles = { ...(villageRoles ?? {}) }
  const nextMayRelay = { ...(mayRelay ?? {}) }

  const missingFromAccount = []
  // Relay downstreams the account no longer has, per relay. Reported rather
  // than silently pruned: a tier that has quietly lost one of the villages it
  // was feeding is a tier the operator believes is complete, and the plan will
  // report that village as unreachable with nothing connecting the two.
  const relayTargetsDropped = []
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
    if (row.may_relay != null) nextMayRelay[row.village_id] = row.may_relay
    if (row.trade_office_level != null) nextTradeOffice[row.village_id] = row.trade_office_level
    if (row.max_busy_merchants != null) nextMaxBusy[row.village_id] = row.max_busy_merchants
    if (row.crop_ceiling != null) nextCropCeilings[row.village_id] = row.crop_ceiling
    if (row.ship_only_to != null) nextShipOnlyTo[row.village_id] = row.ship_only_to
    if (row.relay_for?.length) {
      const kept = row.relay_for.filter((id) => known.has(id))
      const gone = row.relay_for.filter((id) => !known.has(id))
      if (gone.length) {
        relayTargetsDropped.push({ village_id: row.village_id, name: row.name, dropped: gone })
      }
      // A tier left with nobody to feed is removed outright rather than kept as
      // an empty list, which neither the file nor the backend accepts.
      if (kept.length) nextRelayFor[row.village_id] = kept
      else delete nextRelayFor[row.village_id]
    }
    if (row.stock_floor_fraction != null) nextStockFloors[row.village_id] = row.stock_floor_fraction
    // Silence leaves the override on screen alone, the same rule every other
    // column here follows -- a document that says nothing about the feedstock
    // is not a document saying "derive it".
    if (row.npc_feedstock != null) nextNpcFeedstock[row.village_id] = row.npc_feedstock
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
  const nextAttendance = { ...(npcAttended ?? {}) }
  const nextOvernight = { ...(overnight ?? {}) }
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
  // Per profile, and merged rather than replaced wholesale: the file wins about
  // the profiles it names and says nothing about the rest, exactly as the hours
  // beside it do. False overwrites, because false is an answer.
  for (const [name, answer] of Object.entries(setup.npcAttended ?? {})) {
    nextAttendance[name] = answer
  }
  // Per profile and merged, on exactly the rule above. False overwrites here
  // too, and it has to: false is what keeps a near-24h day profile out of
  // section 6's rules when its window happens to wrap.
  for (const [name, declared] of Object.entries(setup.overnight ?? {})) {
    nextOvernight[name] = declared
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
    maxBusy: nextMaxBusy,
    cropCeilings: nextCropCeilings,
    shipOnlyTo: nextShipOnlyTo,
    relayFor: nextRelayFor,
    stockFloors: nextStockFloors,
    npcFeedstock: nextNpcFeedstock,
    consumption: nextConsumption,
    villageRoles: nextVillageRoles,
    mayRelay: nextMayRelay,
    roles: nextRoles,
    profiles: nextProfiles,
    profileWindows: nextWindows,
    npcAttended: nextAttendance,
    overnight: nextOvernight,
    merchantModel: setup.merchantModel ?? null,
    // The file wins where it HAS one and says nothing where it does not -- the
    // same rule the merchant model above follows. Absent is not a clear: a v8
    // document knows nothing about this field, so loading one must not wipe the
    // window the operator has on screen.
    reservedWindow: setup.reservedWindow ?? reservedWindow ?? null,
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
      relayTargetsDropped,
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
