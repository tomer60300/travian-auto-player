/** The distribution planner, staged.
 *
 * ─── The information architecture, and why it is this one ──────────────────
 *
 * The organising idea: **each stage answers exactly one question, and the
 * questions are in the order a plan is actually assembled.** Nothing is grouped
 * by which endpoint it talks to or by what kind of widget it is.
 *
 *   1. **Account** — *what the game says, plus what only you can say about each
 *      village.* The snapshot's facts, and the owned columns beside them: role,
 *      Trade Office, merchant cap, shipping whitelist, relay tier, stock floor,
 *      NPC feedstock, consumption. One row per village, because every one of
 *      those is a fact about THAT village. Setup storage lives here too — this
 *      is the stage whose contents are hand-typed and therefore the stage worth
 *      saving.
 *   2. **Roles** — *what each KIND of village is for.* Section 2.1 gives one
 *      consumption profile for four defensive villages, so the profile is typed
 *      once. Promoted out of a collapsed disclosure inside Allocate: a village's
 *      targets are resolved FROM its role, so the role's figures cannot be a
 *      footnote to the grid that displays their consequences.
 *   3. **Targets** — *what each village must end up holding per hour.* The
 *      per-resource editor, the by-village result, and the night derivation that
 *      fills them in.
 *   4. **Day & night** — *the whole day, window by window.* Which hours each
 *      profile owns, **who is at the marketplace during them** (section 7), the
 *      25%/60% state pair at the two switches (section 6), and the composite
 *      replay that answers what one profile leaves the next.
 *   5. **Plan** — *what it will do, and the record of it.* The sheet, the
 *      merchant budget, what section 7's balancing came to, the findings, then
 *      confirm-then-export (section 10) and execution.
 *
 * Three things had no home before, and each one has exactly one now, which is
 * the test the staging was designed against:
 *
 *   * **`npc_attended`** is a property of a WINDOW, not of a village and not of
 *     the account — so it belongs on stage 4 beside the hours, and nowhere else.
 *     It was previously unaskable, and the plan came back 422.
 *   * **The 25%/60% pair** describes the state of the stores at the two
 *     switches. It is stage 4 for the same reason.
 *   * **The confirmed plan's digest** is the identity of one plan, so the export
 *     that demands it back sits with the plan it identifies, on stage 5.
 *
 * What deliberately did NOT move: the profile selector and the active profile's
 * hours stay in the global bar above the stage nav, because they scope every
 * stage below it — the sheet on stage 5 is the sheet for one profile, and the
 * operator has to be able to see and change which one without leaving it. The
 * attendance ANSWER is edited on stage 4 only, and the bar carries a badge that
 * states it in words and jumps there; one editor, one indicator.
 */

import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import ConfirmDialog from '../components/ConfirmDialog'
import DayNightPanel from '../components/DayNightPanel'
import FullDayCheck from '../components/FullDayCheck'
import NightOverrunTable from '../components/NightOverrunTable'
import NpcBalancePanel from '../components/NpcBalancePanel'
import PlanDiagnostics from '../components/PlanDiagnostics'
import PlanExport from '../components/PlanExport'
import RevertRunPanel from '../components/RevertRunPanel'
import RoleTemplates from '../components/RoleTemplates'
import SetupStorage from '../components/SetupStorage'
import ScrollableTable from '../components/ScrollableTable'
import UnallocatedPanel from '../components/UnallocatedPanel'
import { useToast } from '../components/Toast'
import useGameStore from '../stores/gameStore'
import useLogStore from '../stores/logStore'
import api from '../api'
import {
  CONSUMABLE_RESOURCES,
  DEFAULT_BASELINE_FILL,
  DEFAULT_TARGET_FILL,
  MAX_MERCHANTS_PER_VILLAGE,
  SetupFileError,
  VILLAGE_ROLES,
  allocationsForRequest,
  buildSetup,
  declaresConsumption,
  describeConsumption,
  describeRelayFor,
  describeSpendSource,
  isConsumptionRate,
  isEmptyTemplate,
  isMaxBusyMerchants,
  isStockFloorFraction,
  merchantModelProblems,
  mergeSetup,
  parseSetup,
  relayFlagsOnly,
  relayTierProblemsByVillage,
  resolveRoleAllocation,
  resolveRoleSpend,
  resolvedSpend,
  roleDeviates,
  rolesForRequest,
  setupFilename,
  setupMatchesAccount,
  stripStoredCropSpends,
  stripUnknownRoles,
  unreachableCaps,
} from '../utils/plannerSetup'
import {
  METER_TONE,
  allocationMeterSeverity,
  npcDrawByVillage,
  planCellFigures,
  villageNetIndex,
  withEditedAllocation,
  withRemainder,
} from '../utils/plannerAllocation'
import {
  MINUTES_IN_DAY,
  dispatchWindowFor,
  overnightField,
  overnightMapOnly,
  windowDayShare,
} from '../utils/plannerClock'
import {
  NPC_FEEDSTOCK_RESOURCES,
  attendanceFor,
  attendanceMapOnly,
  attendanceRequired as npcAttendanceRequired,
  describeAttendance,
  describeFeedstock,
  isFeedstockList,
  npcAttendedField,
  npcFeedstockField,
  unansweredAttendance,
} from '../utils/plannerNpc'
import {
  filenameFromDisposition,
  isDigestConflict,
  planDigestShort,
  yamlFilename,
  yamlResponseTransform,
} from '../utils/plannerExport'
import {
  excludedOriginIds,
  namesForVillageIds,
  resolveVillageNames,
  unresolvedProtectedEntries,
} from '../utils/villageRefs'
import { describeBlockers, planBlockers } from '../utils/plannerBlockers'
import { planStatus, relayLegIndex, verdictSummary } from '../utils/plannerFindings'
import { routeSheetRow, routeSheetText } from '../utils/plannerSheet'
import { groupWarnings } from '../utils/warningGroups'
import {
  filterVillages,
  nextSort,
  sortVillages,
  summariseSnapshot,
} from '../utils/snapshotSummary'
import { copyToClipboard } from '../utils/clipboard'
import {
  MODES,
  RESOURCES,
  RESOURCE_LABEL,
  ROLE_LABEL,
} from '../constants/planner'

// Owned state the game will not tell us, kept per village. Trade Office level
// changes only when the operator builds one, so it is stored rather than fetched.
// Keys are namespaced per (server, player): the plan endpoint trusts the
// snapshot the client sends, so rehydrating another account's villages would
// generate a route sheet from the wrong account's data.
const LS_TRADE_OFFICE = 'planner_trade_office'
const LS_FOREIGN = 'planner_foreign_targets'
const LS_ALLOCATIONS = 'planner_allocations' // legacy single-plan key (migrated)
const LS_SNAPSHOT = 'planner_snapshot'
// When the snapshot was fetched (client receipt time, ms). A snapshot carries
// fast-changing production/stock/merchant state, so a restored one must show its
// age and go stale rather than pass silently as live.
const LS_SNAPSHOT_AT = 'planner_snapshot_at'
// Durable record of the last LIVE trade-route run. The in-page result panel is
// cleared whenever planner inputs change, but a run that mutated the game must
// stay auditable across input edits and reloads.
const LS_LAST_RUN = 'planner_last_live_run'
// Per-run route cap sent to /distribution/execute. Also bounds how many origin
// villages a single run VISITS (and therefore may disable stale routes on), so
// the confirmation copy derives its counts from the same number the request uses.
const MAX_ROUTES_PER_RUN = 3
/** What the box says, with blank falling back and 0 SURVIVING.
 *
 * The backend documents 0 as "reconcile only: read, disable what the plan no
 * longer wants, and create nothing — the safe first half of a profile switch",
 * and the box offers it (`min="0"`). `Number(routesPerRun) || MAX_ROUTES_PER_RUN`
 * turned that request into three live route creations, which is the opposite
 * instruction: the operator asks for the cautious half and gets the committing
 * one. Blank still falls back, because blank is unknown and unknown is not 0.
 */
const routeCap = (typed) =>
  String(typed).trim() === '' || !Number.isFinite(Number(typed))
    ? MAX_ROUTES_PER_RUN
    : Number(typed)
/** Route ROWS one run may put in the game, by default.
 *
 * The unit the operator actually authorises, in the backend's own words. A
 * ROUTE is one request; Travian turns each "repeat every N hours" into 24/N
 * separate daily rows and fires every one of them, so `MAX_ROUTES_PER_RUN` of 3
 * on one-hour cycles is 72 rows -- and removing them later means deleting each
 * row by hand.
 *
 * The row cap defaulted to blank, which the payload omits and the server reads
 * as 0 = unbounded. So a default live run bounded routes at 3 and rows at
 * infinity: "nothing bounded it, so what was agreed to and what was written
 * were different units", as the backend field says.
 *
 * 24 is one route at the shortest cycle the game offers -- the cautious reading
 * of "a few at a time", which is what the whole controlled run is for. Blank
 * still means no limit, and says so in the box, because a whole-day
 * provisioning pass is a legitimate thing to ask for and emptying the box is
 * how it is asked.
 */
const MAX_GAME_ROWS_PER_RUN = 24
// Villages a single reconciliation chunk visits. Two paced reads each, plus a
// disable and its verifying re-read where there is something stale, lands a
// chunk of five at roughly 40-70 seconds — comfortably inside one request, which
// is the whole reason the sweep is chunked at all.
const SWEEP_VILLAGES_PER_CHUNK = 5
// Travian's repeat interval is a closed set of the divisors of 24. Offering
// anything else would plan a cadence the create payload cannot express.
const TRAVIAN_REPEAT_INTERVALS = [1, 2, 3, 4, 6, 8, 12, 24]
// `planStatus()`'s tone, as the verdict banner's tone class. Mapped rather than
// interpolated, because the three tones are a closed set the CSS declares and a
// template string would silently produce a class that does not exist.
const VERDICT_TONE_CLASS = {
  'text-danger': 'plan-verdict-blocked',
  'text-warning': 'plan-verdict-dirty',
  'text-success': 'plan-verdict-clean',
  // `planStatus` returns this when an older backend sent no verdict at all, so
  // the second question -- is anything critical outstanding -- went unanswered.
  // The cautious tone, not the green one.
  'text-secondary': 'plan-verdict-dirty',
}

// The three profile actions that need a name typed, as one dialog. Titles and
// action words rather than a mode word in a heading, so each reads as the
// question it is.
const PROFILE_NAMING_TITLE = {
  add: 'New plan profile',
  duplicate: 'Duplicate this profile',
  rename: 'Rename this profile',
}
const PROFILE_NAMING_ACTION = {
  add: 'Create it',
  duplicate: 'Duplicate it',
  rename: 'Rename it',
}
const splitProtected = (text) =>
  text
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
// Two tiers, because "is this snapshot usable" has two different answers.
//
// PLANNING consumes production rates, store capacities and Trade Office levels
// -- figures that move over days. Gating that at 30 minutes cost ~15 game
// requests of pure re-fetching in one planning session on an account whose
// rates had not moved at all.
//
// GOING LIVE additionally consumes free merchants and stocks, which move minute
// to minute and decide whether the plan can be staffed at all. That keeps the
// strict gate.
const SNAPSHOT_PLAN_TTL_MS = 4 * 60 * 60 * 1000 // 4 hours
const SNAPSHOT_LIVE_TTL_MS = 30 * 60 * 1000 // 30 minutes
const LS_MERCHANT = 'planner_merchant_model'
// Named allocation profiles (e.g. Day / Night). Trade Office and the merchant
// model stay account-wide — only the allocations differ per profile, so
// switching a profile is free: it re-plans from the same snapshot with no
// extra Travian request.
const LS_PROFILES = 'planner_profiles'
const LS_ACTIVE_PROFILE = 'planner_active_profile'
const DEFAULT_PROFILE = 'Day'
const EMPTY_ALLOC = Object.freeze({}) // stable reference for a profile with no entries

// Drop allocation entries for villages no longer in the snapshot; a stale id
// would 400 every plan call. Applied to every profile on fetch.
function pruneAllocations(alloc, ids) {
  const pruned = {}
  for (const [resource, per] of Object.entries(alloc ?? {})) {
    const kept = Object.fromEntries(
      Object.entries(per).filter(([vid]) => ids.has(Number(vid)))
    )
    if (Object.keys(kept).length) pruned[resource] = kept
  }
  return pruned
}

// Load persisted profiles, migrating a legacy single-plan account into a
// "Day" profile so no existing work is lost.
function loadProfiles(accountKey) {
  const stored = loadJson(`${LS_PROFILES}::${accountKey}`, null)
  if (stored && typeof stored === 'object' && Object.keys(stored).length) return stored
  const legacy = loadJson(`${LS_ALLOCATIONS}::${accountKey}`, null)
  return { [DEFAULT_PROFILE]: legacy && typeof legacy === 'object' ? legacy : {} }
}

// Merchant capacity is server-calibrated (Europe 2 is not a stock server — see
// docs/25), so it cannot be derived from tribe and defaults to the operator's
// calibrated Europe 2 Teuton values. Travel SPEED, by contrast, is tribe-derived
// server-side and travels in the snapshot.
// The two merchant levers default to the planner's own values, so an account
// nobody has touched sends what the backend would have used anyway -- exposing
// them must not change a single plan by itself.
const DEFAULT_MERCHANT_MODEL = {
  base_capacity: 2500,
  bonus_per_to_level: 0.2,
  merchant_reserve: 2,
  merchant_headroom: 0.1,
}

const LS_WINDOWS = 'planner_profile_windows'
const LS_CROP_CEILING = 'planner_crop_ceiling'
const LS_SHIP_ONLY_TO = 'planner_ship_only_to'
const LS_RELAY_FOR = 'planner_relay_for'
const LS_STOCK_FLOOR = 'planner_stock_floor'
// Which of a village's stores NPC may convert FROM, per village:
// { [village_id]: Resource[] }. Absent is DERIVED -- everything the village is
// not drawing on -- which is the honest default and what section 7 describes
// for 02 (clay and crop into wood). Owned, like the stock floor it qualifies:
// nothing in the game states which store an operator would sell.
const LS_NPC_FEEDSTOCK = 'planner_npc_feedstock'
const LS_MAX_BUSY = 'planner_max_busy'
const LS_CONSUMPTION = 'planner_consumption'
const LS_VILLAGE_ROLES = 'planner_village_roles'
const LS_MAY_RELAY = 'planner_may_relay'
const LS_ROLE_TEMPLATES = 'planner_role_templates'
// Sensible defaults by convention; anything else starts unset until the
// operator gives it hours.
const DEFAULT_WINDOWS = { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] }
// Who is at the marketplace during each profile's hours: { [profile]: boolean }.
// Section 7's `npc_attended`, and the one answer this page could not give at
// all -- a stock floor plus a day/night profile came back 422 with no control
// on screen to fix it. Kept per PROFILE because it is a property of a window:
// this operator is awake for the day and asleep through the night, so the two
// profiles carry opposite answers and neither is a default.
//
// It travels in the setup document too (v7), so it follows the operator
// between origins the way the hours beside it do -- localStorage is scoped to
// an ORIGIN, and this app is served on :80, :8001, the LAN address and over
// Tailscale.
const LS_NPC_ATTENDED = 'planner_npc_attended'
// Which profile is the one the operator sleeps through: { [profile]: boolean }.
// Section 6's `overnight`, and the second answer the page could not give --
// the backend took the field on `PlanRequest` and on every day segment, and
// `grep -rn overnight frontend/src` found one prose mention, so the fix was
// API-only and unreachable from here.
//
// Absent is the third state and it is the RESTING one: it asks the backend to
// derive the night from a window that wraps past midnight, which is right for
// a night stated as one 23:00-07:00 pair. The declaration exists for the two
// cases the derivation gets wrong -- a night SPLIT at midnight, where
// 00:00-07:00 wraps in neither direction, and a near-24h day profile
// (`[420, 419]`) that wraps and is not the night. The operator can create a
// split night from this page (`addProfile`), so the exact configuration the
// backend fix exists for was reachable and got the broken answer: a 600-minute
// round trip inside a 420-minute night went unreported as NIGHT_OVERRUN, and
// with only the pre-midnight half recognised the 60% morning floor was
// measured at 00:00 instead of 07:00.
const LS_OVERNIGHT = 'planner_overnight'
// Minutes of the day to keep clear of ARRIVALS, so the operator's manual NPC
// burst is not competing with merchants landing. Account-wide, because it is
// one person at one marketplace -- unlike the attendance answer above, which
// is per profile because the operator is awake for some windows and not
// others. Owned: nothing in the game states when someone sits down to trade.
const LS_RESERVED_WINDOW = 'planner_reserved_window'

// Only complete rows go to the backend: a half-typed target would 422 the
// whole request, and the operator is mid-edit, not in error. Shared by the
// plan build and the full-day check so both see the same tributes.
/** A coordinate the operator actually typed. Blank is not 0.
 *
 * `Number('') || 0` turned a cleared box into (0|0) -- the middle of the map --
 * while the box on screen still read blank, so a half-typed tribute was planned
 * against a village that is not where it is, with the distance, the cycle and
 * the merchant count all computed from the wrong tile. A row missing one is a
 * draft, on exactly the rule its missing name or crop rate already followed.
 */
const hasCoord = (value) => String(value).trim() !== '' && Number.isFinite(Number(value))

const foreignTargetIsDraft = (t) =>
  !String(t.name).trim() || !(Number(t.crop_per_hour) > 0) || !hasCoord(t.x) || !hasCoord(t.y)

const usableForeignTargets = (targets, villages = []) =>
  targets
    .filter((t) => !foreignTargetIsDraft(t))
    .map((t) => ({
      name: t.name.trim(),
      x: Number(t.x),
      y: Number(t.y),
      crop_per_hour: Number(t.crop_per_hour),
      safety_margin_pct: Number(t.safety_margin_pct) || 0,
      route_eligible: Boolean(t.route_eligible),
      // Cadence, and who may meet it. Omitted when unset so an ordinary tribute
      // is byte-identical: the backend treats absent as "no constraint", and
      // sending 0 or [] would be a different statement.
      ...(Number(t.max_cycle_hours) > 0
        ? { max_cycle_hours: Number(t.max_cycle_hours) }
        : {}),
      // What was typed if anything was, else what a loaded file stored. Reading
      // only the typed text dropped a file's exclusions entirely -- see
      // excludedOriginIds.
      ...(excludedOriginIds(t, villages).length
        ? { exclude_origins: excludedOriginIds(t, villages) }
        : {}),
    }))

// The operator types a stock floor as a percent; the request and the file carry
// a fraction of warehouse capacity, so the two meet here. Printed through
// toPrecision because 0.3 * 100 is 30.000000000000004 in floating point, and
// that would land in the input box verbatim.
const fractionToPercent = (fraction) => Number((fraction * 100).toPrecision(12))

// The "ships only to" picker's one-line summary. Nothing stored is the
// unrestricted default and reads as such; an EMPTY list is a real answer --
// this village ships to nobody -- and must not look like the default. Names,
// not a count, while they fit.
const describeShipOnlyTo = (allowed, villages) => {
  if (allowed == null) return 'any village'
  if (allowed.length === 0) return 'nobody'
  if (allowed.length <= 2) return namesForVillageIds(allowed, villages)
  return `${allowed.length} villages`
}

// Trade Office building id, as the game reports it in a village's slot list.
const TRADE_OFFICE_GID = 28


// Minimal inline glyphs in the game's resource order, so cargo reads the way
// the marketplace shows it. Drawn here rather than shipped as assets: no
// external sprite, and they inherit currentColor for the strokes.
function ResourceIcon({ resource }) {
  const common = { width: 13, height: 13, viewBox: '0 0 16 16', 'aria-label': RESOURCE_LABEL[resource], role: 'img', className: 'inline-block align-[-2px]' }
  if (resource === 'lumber')
    return (
      <svg {...common}>
        <rect x="1" y="6" width="11" height="4" rx="2" fill="#a16207" />
        <rect x="4" y="10" width="11" height="4" rx="2" fill="#854d0e" />
        <circle cx="12" cy="8" r="1.6" fill="#fde68a" />
        <circle cx="15" cy="12" r="1.4" fill="#fde68a" />
      </svg>
    )
  if (resource === 'clay')
    return (
      <svg {...common}>
        <rect x="1" y="4" width="9" height="5" rx="1" fill="#ea580c" />
        <rect x="6" y="9" width="9" height="5" rx="1" fill="#c2410c" />
      </svg>
    )
  if (resource === 'iron')
    return (
      <svg {...common}>
        <path d="M3 12 L6 5 L10 5 L13 12 Z" fill="#94a3b8" />
        <path d="M6 5 L8 8 L10 5 Z" fill="#e2e8f0" />
      </svg>
    )
  return (
    <svg {...common}>
      <path d="M8 15 V6" stroke="#eab308" strokeWidth="1.4" />
      <path d="M8 7 C5 6 4 4 4 1 C7 2 8 4 8 7 Z" fill="#facc15" />
      <path d="M8 7 C11 6 12 4 12 1 C9 2 8 4 8 7 Z" fill="#eab308" />
      <path d="M8 11 C6 10 5 9 5 7 C7 8 8 9 8 11 Z" fill="#facc15" />
      <path d="M8 11 C10 10 11 9 11 7 C9 8 8 9 8 11 Z" fill="#eab308" />
    </svg>
  )
}
function loadJson(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback
  } catch {
    return fallback
  }
}

function saveJson(key, value) {
  if (!key) return // not connected yet — nothing to namespace the state under
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* storage full or disabled — the plan still works, it just will not persist */
  }
}

/** Hand the browser a file to save. Same shape as the other pages' exports. */
function downloadText(filename, text, mime) {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function downloadJson(filename, value) {
  downloadText(filename, JSON.stringify(value, null, 2), 'application/json')
}

/** FastAPI puts validation failures in `detail` as an ARRAY of error objects;
 *  rendering that raw crashes React. Reduce whatever came back to a string. */
function errorDetail(err, fallback) {
  const detail = err.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => d?.msg).filter(Boolean)
    if (msgs.length) return msgs.join(' · ')
  }
  return fallback
}

const fmt = (n) => (n == null ? '—' : Math.round(n).toLocaleString())
const signed = (n) => (n == null ? '—' : `${n > 0 ? '+' : ''}${Math.round(n).toLocaleString()}`)

/** Net crop, shown with a word as well as a colour.
 *  Design Guideline — Accessibility: never rely on colour alone to convey
 *  information, so "starving" is spelled out rather than implied by red. */
/** A column header that sorts, and says which way it is sorting.
 *
 *  The arrow is paired with aria-sort rather than carrying the state alone, so
 *  the ordering is announced rather than only seen. Cycling reaches unsorted on
 *  the third press: the account's own order is the default and has to stay
 *  reachable without a page reload.
 */
/** A field's reasoning, one click away.
 *
 * The controlled-run box's four checkbox labels measured 489, 408, 215 and 300
 * characters, and the two number inputs sat BETWEEN two of those paragraphs.
 * The bolded lead clause of each was already the label; the rest is prose that
 * carries real warnings, so it is disclosed rather than deleted -- the same
 * mechanism this page uses in nine other places.
 *
 * A `<button>` inside `<summary>` would be a control inside a control, so this
 * is the native disclosure and the SUMMARY carries the accessible name. It
 * still exposes `role="button"` to the accessibility tree, which is how a test
 * and a screen reader both reach it by name.
 *
 * `pointer-coarse:min-h-11`/`min-w-11` because a "?" glyph is about seven
 * pixels wide, and item 4 of the UI Definition of Done wants 44px on a coarse
 * pointer. The desktop layout is untouched: the constraint only applies there.
 */
function Why({ label, children }) {
  return (
    <details className="text-xs inline-block align-top">
      <summary
        className="why-toggle cursor-pointer list-none inline-flex items-center justify-center rounded-full border-default text-secondary hover:text-primary w-4 h-4 leading-none pointer-coarse:min-h-11 pointer-coarse:min-w-11"
        aria-label={`Why: ${label}`}
        title={`Why: ${label}`}
      >
        ?
      </summary>
      <div className="text-secondary mt-1 max-w-md">{children}</div>
    </details>
  )
}

function SortHeader({ label, col, sortKey, sortDir, onSort }) {
  const active = sortKey === col
  return (
    <button
      type="button"
      className={`inline-flex items-center gap-1 uppercase ${
        active ? 'text-primary' : 'hover:text-primary'
      }`}
      aria-sort={active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
      title={
        active
          ? sortDir === 'asc'
            ? `Sorted by ${label}, lowest first. Click for highest first.`
            : `Sorted by ${label}, highest first. Click to return to account order.`
          : `Sort by ${label}`
      }
      onClick={() => onSort(col)}
    >
      {label}
      <span aria-hidden="true" className="text-[10px]">
        {active ? (sortDir === 'asc' ? '\u25B2' : '\u25BC') : '\u2195'}
      </span>
    </button>
  )
}

function CropCell({ village }) {
  if (village.crop_per_hour == null) {
    return (
      <span className="text-warning" title="Rate could not be derived — not treated as zero">
        unknown
      </span>
    )
  }
  const starving = village.crop_per_hour < 0
  return (
    <span className={starving ? 'text-danger' : 'text-success'}>
      {signed(village.crop_per_hour)}/h{starving ? ' · starving' : ''}
    </span>
  )
}

/** Merchant budget as a bar plus the numbers.
 *  Design Guideline — Charts: a chart supplements the data, it does not replace
 *  it, so committed/spare stay readable as text for screen readers. */
function BudgetBar({ budget, cap }) {
  const spare = Math.max(budget.spare, 1)
  // "of 8 merchants" claims the village HAS 8. Where the operator's own cap is
  // what set the budget it is their figure, not the fleet's -- the same test
  // the backend's crowding warning applies, which also keeps this honest if
  // the Max busy cell has been edited since the plan was built.
  const allowed = cap != null && cap === budget.spare
  const usedPct = Math.min(100, Math.round((budget.committed / spare) * 100))
  // Over-budget villages open by default. "over by 2" says what happened but
  // not what to do about it, and the same excess means different things when
  // the trip is the cost than when the Trade Office is.
  const [open, setOpen] = useState(false)
  const legs = budget.legs ?? []
  return (
    <div className="flex-1">
      {/* `flex-wrap`, for the reason `BatchSet` above it already has it and
          measured on the Plan stage the first time that stage was swept: a
          112px bar, the committed/spare figure, "over by 1 · Trade Office +2
          would fit" and the "why?" toggle are all `shrink-0`, so at 375 the
          row ran to x=380 and took the DOCUMENT with it -- 5px of horizontal
          page scroll, which is item 1 of the UI Definition of Done. Nothing
          here may shrink (a truncated merchant count is a wrong merchant
          count), so wrapping is the only answer left. */}
      <div className="flex flex-wrap items-center gap-2">
        <div
          className="h-2 w-28 rounded bg-black/40 overflow-hidden shrink-0"
          role="img"
          aria-label={
            allowed
              ? `${budget.committed} committed of the ${budget.spare} merchants you allow`
              : `${budget.committed} of ${budget.spare} merchants committed`
          }
        >
          <div
            className={`h-full ${budget.over_budget ? 'bg-red-500' : 'bg-emerald-500'}`}
            style={{ width: `${usedPct}%` }}
          />
        </div>
        <span className="font-mono text-xs shrink-0">
          {budget.committed}/{budget.spare}
        </span>
        {budget.over_budget && (
          <span className="text-danger text-xs">
            over by {budget.committed - budget.spare}
            {budget.trade_office_levels_needed != null
              ? ` · Trade Office +${budget.trade_office_levels_needed} would fit`
              : ' · no upgrade fixes this'}
          </span>
        )}
        {legs.length > 0 && (
          <button
            type="button"
            className="text-secondary hover:text-primary text-xs underline shrink-0"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? 'hide' : budget.over_budget ? 'why?' : 'routes'}
          </button>
        )}
      </div>

      {open && (
        <div className="mt-1 mb-2 ml-2 pl-3 border-l border-gray-700">
          {budget.explanation && (
            <p className="text-xs text-warning mb-1">{budget.explanation}</p>
          )}
          <table className="text-xs w-full max-w-2xl">
            <thead className="text-secondary uppercase">
              <tr>
                <th className="text-left pr-3 font-normal">To</th>
                <th className="text-right pr-3 font-normal">Cargo/h</th>
                <th className="text-right pr-3 font-normal">Distance</th>
                <th className="text-right pr-3 font-normal">One way</th>
                <th className="text-right pr-3 font-normal">Cycle</th>
                <th className="text-right font-normal">Merchants</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {legs.map((leg, i) => (
                <tr key={i}>
                  <td className="pr-3">{leg.destination}</td>
                  <td className="text-right pr-3">{fmt(leg.per_hour)}</td>
                  <td className="text-right pr-3">{Math.round(leg.distance_fields)}f</td>
                  <td className="text-right pr-3">{leg.one_way_hours.toFixed(1)}h</td>
                  <td className="text-right pr-3">{leg.cycle_hours}h</td>
                  <td className="text-right">
                    {leg.merchants_per_send}×{leg.sets_in_flight} = {leg.merchants}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-secondary text-xs mt-1">
            A merchant is busy for the whole round trip, so a long haul keeps several sends in
            the air at once — that multiplier, not just the cargo, is what spends merchants.
          </p>
        </div>
      )}
    </div>
  )
}

/** Batch-set the mode/value of the CHECKED villages for one resource, so a
 *  common allocation does not have to be typed row by row. Select rows with
 *  the checkboxes (or the header select-all); the remainder village keeps its
 *  role. Disabled until at least one village is checked. */
/** `flex-wrap` for the same reason the two rows that hold this one already
 * have it: a `w-auto` mode select, a `w-20` value box and an "Apply to N
 * selected" button come to 344px, which is 10px past what a 375 viewport's
 * card can give, and without wrapping those 10px became DOCUMENT scroll --
 * on all four resource cards at once. */
/** The rule under one World & merchants box, said in the cell.
 *
 * `merchantModelProblems` decides WHETHER there is one, off the same predicates
 * the file parser and the plan request use; this only renders it. Two of these
 * six boxes had no message at all -- an even `map_span` and a `merchant_reserve`
 * past 20 are both refused by the backend, and the only sign of either was a
 * 422 on a Build click.
 */
function MerchantRule({ id, rule }) {
  if (!rule) return null
  return (
    <span id={id} className="text-warning">
      {rule}
    </span>
  )
}

function BatchSet({ count, onApply }) {
  const [mode, setMode] = useState('keep')
  const [value, setValue] = useState(0)
  return (
    <div className="flex flex-wrap items-center gap-1 text-xs">
      <span className="text-secondary">Set checked</span>
      <select
        aria-label="Batch mode"
        className="input-field w-auto text-xs py-0.5"
        value={mode}
        onChange={(e) => setMode(e.target.value)}
      >
        {MODES.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label}
          </option>
        ))}
      </select>
      <input
        type="number"
        aria-label="Batch value"
        className="input-field w-20 text-right text-xs py-0.5"
        disabled={mode === 'keep'}
        value={value}
        onChange={(e) => setValue(Number(e.target.value))}
      />
      <button
        className="btn-secondary btn-xs"
        disabled={!count}
        onClick={() => onApply(mode, value)}
      >
        Apply to {count} selected
      </button>
    </div>
  )
}

export default function ResourcePlanner() {
  const toast = useToast()
  const serverUrl = useGameStore((s) => s.serverUrl)
  const playerName = useGameStore((s) => s.playerName)
  // Normalize the server URL (strip trailing slashes) so the per-account cache
  // key is STABLE across reconnects — otherwise a slash difference would look
  // like a new account and silently drop cached Trade Office levels, profiles,
  // and the merchant model.
  const accountKey = serverUrl && playerName ? `${serverUrl.replace(/\/+$/, '')}|${playerName}` : null
  const [stage, setStage] = useState('snapshot')
  // Snapshot table view state. `sortKey` of null means the account's own order,
  // which is meaningful and stays the default.
  const [sortKey, setSortKey] = useState(null)
  const [sortDir, setSortDir] = useState('asc')
  const [rowFilter, setRowFilter] = useState(null)
  // Allocate stage has two views: the per-resource editor, and a read-only
  // result grid grouped by village showing what each ends up with. Grouping by
  // material is how the targets are EDITED; grouped by village is how the
  // operator actually thinks about the outcome.
  const [allocView, setAllocView] = useState('village')
  // Which hours of the day each profile actually runs, 'HH:MM' pairs. The
  // profiles are separate plans, but the account lives through all of them
  // every day -- the windows are what lets the full-day check line them up.
  const [profileWindows, setProfileWindows] = useState({})
  // Section 7's `npc_attended`, per profile. Three states, and the third is
  // load-bearing: absent means UNANSWERED, which is neither true nor false --
  // a guessed true funds night routes out of trading nobody is doing, and a
  // guessed false withdraws the day's allowance and reports shortfalls the
  // account does not have. The plan is refused until every profile with hours
  // says, and the Day & night stage is where it says it.
  const [profileAttendance, setProfileAttendance] = useState({})
  // Section 6's `overnight`, per profile, on exactly the shape above: absent
  // means "derive it from the window", which is the resting state and the
  // right answer for a night stated as one 23:00-07:00 pair. See LS_OVERNIGHT
  // for the two cases the derivation gets wrong and why the declaration wins.
  const [profileOvernight, setProfileOvernight] = useState({})
  // The NPC burst window, as an `['HH:MM', 'HH:MM']` pair or null for none.
  // Arrivals avoid it where an alternative exists, and the plan warns when the
  // geometry forces one into it -- so it is a preference the planner weighs,
  // never a refusal, which is why an unset one is silence rather than a
  // zero-width pair.
  const [reservedWindow, setReservedWindow] = useState(null)
  // Operator alert level for a village's crop stock (e.g. an NPC trigger),
  // below capacity. Cached per account like the Trade Office levels.
  const [cropCeilings, setCropCeilings] = useState({})
  // Where each village may ship: { [village_id]: number[] }. Absent means
  // unrestricted (the default); an empty list means nobody. Owned, like the
  // Trade Office level -- nothing in the game says which of your own villages a
  // merchant may be sent to.
  const [shipOnlyTo, setShipOnlyTo] = useState({})
  // Profile section 5's relay TIER: { [village_id]: number[] } -- the villages
  // this one forwards the capital's lumber, clay and iron on to. Absent means
  // "not a relay", which is every village until the operator says otherwise.
  //
  // Owned like the whitelist above, and for a sharper reason than most: 02 may
  // reach only its own neighbours, so without a tier the defensive villages
  // beyond them are unreachable and the plan comes back INFEASIBLE with a
  // shortfall each. Nothing in the game states a tier, and no role template
  // carries one -- a relay is a fact about a village's POSITION, not about the
  // kind of village it is.
  //
  // An INSTRUCTION, not a permission. `mayRelay` below answers whether the
  // route search may conscript a village as a CROP hub; this says which
  // villages a named relay forwards a material to. The two are different fields
  // because they are different questions.
  const [relayFor, setRelayFor] = useState({})
  // Share of warehouse capacity each village keeps stocked by NPC trading, as a
  // FRACTION (0.3, not 30) so state, file and request agree; the input shows it
  // as a percent. The planner may draw it down as lumber, clay or iron.
  const [stockFloors, setStockFloors] = useState({})
  // The feedstock override that goes with the floor above. Absent means
  // derived, which is an ANSWER and not a blank -- so the cell says "derived"
  // rather than sitting empty, and an empty list (the picker opened and not
  // yet ticked) is dropped from the request rather than sent as an override of
  // nothing, which the backend refuses.
  const [npcFeedstock, setNpcFeedstock] = useState({})
  // The most merchants each village may have underway or returning at once:
  // { [village_id]: number }. Profile section 5 gives the capital one such
  // number and the game states none, so it is owned exactly as the Trade Office
  // level is. A CAP, not a reserve -- `merchant_reserve` below holds merchants
  // back at every village, and off a full fleet the two are not even the same
  // figure (19 merchants less a reserve of 12 is 7, where the cap says 8).
  const [maxBusy, setMaxBusy] = useState({})
  // What each village SPENDS per hour: { [village_id]: { lumber, clay, iron,
  // crop } }, only the resources the operator has typed. Owned, like the
  // Trade Office level, and for a harder reason: the game's statistics page
  // reports materials GROSS, so a village burning 14,751 lumber an hour still
  // reads positive there and nothing in the snapshot can imply this.
  //
  // Kept apart from the allocation TARGET, which is what must arrive. Enter
  // only the target and the plan assumes the village stockpiles every unit --
  // which is how an army village came to be reported as losing target x 24 a
  // day at a cap it never reaches.
  const [consumption, setConsumption] = useState({})
  // Which of profile section 1's five kinds each village is: { [village_id]:
  // role }. Owned, like the Trade Office level and for the same reason --
  // nothing in the game says a village is the Hammer -- and it decides three
  // things the page cannot: which template supplies the village's targets and
  // spend, whether the optimizer may relay through it, and how loud a designed
  // crop deficit is reported.
  const [villageRoles, setVillageRoles] = useState({})
  // Whether ONE village may forward someone else's cargo, over its role
  // template's answer: { [village_id]: boolean }. Per village because the case
  // is singular -- the account this exists for has one defensive village on the
  // only road to a corner of the map, and putting the override on the template
  // would hand the permission to all four. No input for it yet; it arrives from
  // the setup file, which is why the page has to carry it rather than let the
  // parser's answer fall on the floor.
  const [mayRelay, setMayRelay] = useState({})
  // One profile per role: { [role]: { allocations, consumption, may_relay,
  // crop_negative_by_design } }. Section 2.1 gives ONE profile for FOUR
  // defensive villages, so this is where those four numbers are typed -- once.
  const [roleTemplates, setRoleTemplates] = useState({})
  // Which role's figures the operator has just asked to be taken to, as
  // `{ role, seq }`. `seq` rather than the role alone: the second press of the
  // same village's button has to land again, and the role it would compare is
  // already the role it wants. Never cleared -- it is a request, and the panel
  // acts on each one exactly once, keyed on the number.
  const [templateFocus, setTemplateFocus] = useState(null)
  // The cell Build plan's refusal has just sent the operator to, as
  // `{ label, seq }` -- the accessible name of the control, and the same `seq`
  // discipline as `templateFocus` above for the same reason: a second press
  // over the same unfixed cell has to land again.
  const [cellFocus, setCellFocus] = useState(null)
  // Result of the last setup-file load, kept on screen rather than only in a
  // toast: a file that is missing villages produces a quietly wrong plan, so
  // what it did and did not cover has to stay readable.
  const [setupReport, setSetupReport] = useState(null)
  // Villages whose stored crop spend was dropped on the way in, as village-id
  // keys. Stripping it is right -- the input no longer offers crop and the
  // backend 422s it -- but doing so in silence makes a CRITICAL the figure was
  // silencing reappear on the next plan with nothing to connect them, while
  // the file-import path raises a loud error for the same figure. Reported
  // once: the stripped map is what gets saved back, so the next hydration has
  // nothing to strip.
  const [cropSpendsDropped, setCropSpendsDropped] = useState([])
  // The same receipt for a role or a template key outside the five, dropped on
  // the way in for the same reason (the backend 422s an unknown role) and with
  // a larger consequence: a dropped role takes four villages' targets and their
  // spend, and the plan then reads them as keeping their own production without
  // saying so. Reported once -- the stripped maps are what get saved back.
  const [rolesDropped, setRolesDropped] = useState(null)
  // Whether a setup is saved on the SERVER for this account, and when.
  // Four states, and the two in the middle are the point: `none` is the
  // server's 404 -- nothing has ever been saved, which is an invitation to
  // import a file -- and it is not the same as a saved document that happens
  // to be empty, which is a decision to leave the account undescribed.
  const [serverSetup, setServerSetup] = useState({ state: 'unknown' })
  const [setupBusy, setSetupBusy] = useState(null)
  const [pasteOpen, setPasteOpen] = useState(false)
  const [pasteText, setPasteText] = useState('')
  const setupFileRef = useRef(null)
  // The five questions this page has to ask before it changes something, held
  // as state and answered by `components/ConfirmDialog` -- the one every other
  // page in the app already uses.
  //
  // They were four `window.confirm` calls and three `window.prompt` calls, and
  // the live-run manifest was one of them. A native dialog renders unstyled and
  // theme-blind, gives no emphasis to the counts that decide the answer, cannot
  // be re-read once dismissed, and -- the part that made it a defect rather than
  // a preference -- Chrome's "Prevent this page from creating additional
  // dialogs" makes every later `confirm()` return false and every later
  // `prompt()` return null, SILENTLY. The live button then does nothing, with
  // no explanation, on the one action that writes to a real account.
  //
  // `pendingSetup` holds a document from a different account until the operator
  // insists; `profileNaming` is `{ mode, value }` for the three actions that
  // need a name typed.
  const [pendingSetup, setPendingSetup] = useState(null)
  const [confirmForget, setConfirmForget] = useState(false)
  const [confirmDeleteProfile, setConfirmDeleteProfile] = useState(null)
  const [profileNaming, setProfileNaming] = useState(null)
  const [confirmLive, setConfirmLive] = useState(false)
  // What undoing one live run would take: `{ traceId, busy, result, error }`.
  // One at a time, keyed by the trace id, so opening the undo for a history row
  // replaces the answer rather than showing one run's steps under another's
  // heading.
  const [revert, setRevert] = useState(null)
  // Which history row's undo is open, if any. Separate from `revert` because a
  // row can be chosen before anything has been read for it.
  const [revertRun, setRevertRun] = useState(null)
  // `{ traceId, deleting }` while the operator is being asked. Disabling is
  // reversible and deleting is not, so they are separate opt-ins -- and the
  // endpoint disables before it deletes, so `deleting` implies both.
  const [confirmRevert, setConfirmRevert] = useState(null)
  const [dayCheck, setDayCheck] = useState(null)
  const [dayChecking, setDayChecking] = useState(false)
  // Invalidates an in-flight day-check when its inputs change, so a stale
  // response cannot resurrect a result computed from pre-edit inputs.
  const dayCheckInputRev = useRef(0)
  const [snapshot, setSnapshot] = useState(null)
  // Client receipt time of the current snapshot, and an explicit opt-in to plan
  // from a stale one (see SNAPSHOT_PLAN_TTL_MS / SNAPSHOT_LIVE_TTL_MS).
  const [snapshotFetchedAt, setSnapshotFetchedAt] = useState(null)
  const [useStaleSnapshot, setUseStaleSnapshot] = useState(false)
  // Staleness is derived from the clock, but time passing does not re-render
  // React — so `nowMs` is bumped by a timer that fires exactly when the current
  // snapshot crosses the TTL, engaging the gate even if the page just sits open.
  const [nowMs, setNowMs] = useState(() => Date.now())
  const [tradeOffice, setTradeOffice] = useState({})
  const [profiles, setProfiles] = useState({ [DEFAULT_PROFILE]: {} })
  const [activeProfile, setActiveProfile] = useState(DEFAULT_PROFILE)
  const [merchantModel, setMerchantModel] = useState(DEFAULT_MERCHANT_MODEL)
  // Villages outside the account that are owed crop. Hand-entered, because
  // nothing in the game tells us about them, and cached per account like the
  // Trade Office levels are.
  const [foreignTargets, setForeignTargets] = useState([])
  // Per-resource checkbox selection for batch edits: { [resource]: number[] }.
  // Transient (not persisted); reset on account change and on a fresh snapshot
  // so it never targets villages from a previous account/snapshot.
  const [selected, setSelected] = useState({})

  // The active profile's allocations, exposed with the same shape the rest of
  // the component already uses, so the Allocate grid and plan build unchanged.
  const allocations = profiles[activeProfile] ?? EMPTY_ALLOC
  const setAllocations = useCallback(
    (updater) => {
      setProfiles((prev) => {
        const current = prev[activeProfile] ?? {}
        const next = typeof updater === 'function' ? updater(current) : updater
        return { ...prev, [activeProfile]: next }
      })
    },
    [activeProfile]
  )
  // Persisting is enabled only after this account's stored state has been
  // loaded, so the initial defaults can never overwrite it.
  const [hydratedKey, setHydratedKey] = useState(null)
  const [plan, setPlan] = useState(null)
  // Section 10's export, and the refusal it can come back with. The conflict
  // is page state rather than a toast because it needs an ACTION -- re-read
  // the plan -- and a toast is gone before the operator has decided.
  const [exportingYaml, setExportingYaml] = useState(false)
  const [yamlConflict, setYamlConflict] = useState(null)
  const [fetching, setFetching] = useState(false)
  const [scanningTradeOffices, setScanningTradeOffices] = useState(false)
  const [planning, setPlanning] = useState(false)
  const [execResult, setExecResult] = useState(null)
  const [executing, setExecuting] = useState(false)
  // Controlled-run knobs. Deliberately not persisted with the rest of the setup:
  // narrowing a run is a decision about THIS run, and a saved filter silently
  // shrinking a later full run is exactly the surprise worth avoiding.
  const [routesPerRun, setRoutesPerRun] = useState(MAX_ROUTES_PER_RUN)
  const [onlyOrigin, setOnlyOrigin] = useState('')
  // Rows, not routes: the unit that actually lands in the game. A route is a
  // request; Travian turns it into 24/cycle daily rows, so three routes on
  // one-hour cycles is seventy-two rows. Defaulted rather than blank -- see
  // MAX_GAME_ROWS_PER_RUN -- because blank omits the field and the server then
  // reads 0, which is unbounded: the run capped the unit nobody authorises and
  // left the one they do uncapped. Blank still means no limit, and the box says
  // so.
  const [maxGameRows, setMaxGameRows] = useState(String(MAX_GAME_ROWS_PER_RUN))
  // Travian cannot confine a route to part of the day, but its fan-out can be
  // trimmed: repeat-every-N-hours is 24/N individually deletable rows, so the
  // ones departing outside the profile get removed after creation. On by
  // default because OFF is the broken case -- the window is a fiction the game
  // ignores, and the destination receives every firing.
  const [pruneToWindow, setPruneToWindow] = useState(true)
  // The two numbers the account cannot supply: how empty the stores actually
  // are at bedtime, and how full they may be at dawn. Everything else the
  // derivation needs it works out for itself.
  // Section 6's figures, and the same pair the server measures against: the
  // operator spends role villages down to 25% at the day->night switch, and
  // every one of them must be at 60% on both stores by 07:00. These were
  // 30/80, which made the derivation aim at one pair while the full-day check
  // graded against the other -- a night that does not exist. The disagreement
  // note below stays, because it still earns its place the moment either box
  // is edited.
  //
  // Imported rather than written as two literals here. They are a deliberate
  // second copy of `DEFAULT_BASELINE_FILL` / `DEFAULT_TARGET_FILL` in
  // `night_profile.py`, and they were the only such copy in this app with
  // neither a note nor a test -- so the constant and its pin live in
  // `plannerSetup.js` beside the three that already do. A percent on screen, a
  // fraction on the wire, and the rounding is deliberate: `0.6 * 100` is
  // 60.00000000000001.
  const [baselineFill, setBaselineFill] = useState(
    Math.round(DEFAULT_BASELINE_FILL * 100)
  )
  const [targetFill, setTargetFill] = useState(Math.round(DEFAULT_TARGET_FILL * 100))
  const [deriving, setDeriving] = useState(false)
  const [derived, setDerived] = useState(null)
  // Destinations the reconciler must leave alone. Its rule -- active,
  // identifiable, not wanted by the plan => stale -- is right for routes a
  // previous plan made and wrong for one made by hand.
  const [protectDestinations, setProtectDestinations] = useState('')
  const [onlyDestination, setOnlyDestination] = useState('')
  // Was hardcoded true, which made every run a create AND a disable. For a
  // first live test that is the wrong shape: turning it off makes the run
  // create-only, so the single thing it changes is the single thing being
  // tested. Defaults to on, which is the behaviour for ordinary runs.
  // A reconciliation sweep is its own operation, not a variation of the run
  // below: it visits every village, writes only disables, and takes minutes.
  const [sweeping, setSweeping] = useState(false)
  const [sweepProgress, setSweepProgress] = useState(null)
  const sweepCancel = useRef(false)
  const [disableExisting, setDisableExisting] = useState(true)
  // Off by default, and deliberately: correcting cargo overwrites a route that
  // may have been tuned in-game on purpose.
  const [updateDrifted, setUpdateDrifted] = useState(false)
  // Whole-day execution: every profile's routes in one pass, reconciled as a
  // union, so Day and Night rows coexist in the game (disjoint by departure
  // minute) and no daily profile switching is ever needed.
  const [wholeDay, setWholeDay] = useState(false)
  // Run history from the app's own execution traces (zero game requests).
  const [runHistory, setRunHistory] = useState(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  // Durable audit of the last LIVE run (see LS_LAST_RUN): survives the input
  // edits that clear execResult, and page reloads.
  const [lastRun, setLastRun] = useState(null)

  const storageKey = useCallback(
    (base) => (accountKey ? `${base}::${accountKey}` : null),
    [accountKey]
  )

  // The live account key, read from the store at call time — NOT the render
  // closure. An async response must compare against the account that is
  // current when it lands, or the guard sees its own stale captured value.
  const currentAccountKey = useCallback(() => {
    const s = useGameStore.getState()
    return s.serverUrl && s.playerName
      ? `${s.serverUrl.replace(/\/+$/, '')}|${s.playerName}`
      : null
  }, [])

  // Fire a re-render exactly when the current snapshot crosses the freshness TTL,
  // so the stale banner appears and Build plan gates without waiting for some
  // unrelated state change. Re-armed whenever the snapshot/receipt time changes;
  // cleared on unmount.
  useEffect(() => {
    if (!snapshot || snapshotFetchedAt == null) return undefined
    const remaining = snapshotFetchedAt + SNAPSHOT_PLAN_TTL_MS - Date.now()
    if (remaining <= 0) {
      setNowMs(Date.now())
      return undefined
    }
    const timer = setTimeout(() => setNowMs(Date.now()), remaining + 50)
    return () => clearTimeout(timer)
  }, [snapshot, snapshotFetchedAt])

  useEffect(() => {
    if (!accountKey) {
      // Disconnected: the page stays routable, so showing (or planning from)
      // the previous account's villages would act on stale data.
      setSnapshot(null)
      setSnapshotFetchedAt(null)
      setLastRun(null)
      setUseStaleSnapshot(false)
      setTradeOffice({})
      setShipOnlyTo({})
      setStockFloors({})
      setNpcFeedstock({})
      setVillageRoles({})
      setRoleTemplates({})
      setProfiles({ [DEFAULT_PROFILE]: {} })
      setProfileAttendance({})
      setProfileOvernight({})
      setReservedWindow(null)
      setForeignTargets([])
      setMayRelay({})
      setMaxBusy({})
      setActiveProfile(DEFAULT_PROFILE)
      setMerchantModel(DEFAULT_MERCHANT_MODEL)
      setSelected({})
      setPlan(null)
      // The load report names villages from the snapshot it was matched
      // against, so it cannot outlive that account.
      setSetupReport(null)
      setCropSpendsDropped([])
      setRolesDropped(null)
      setServerSetup({ state: 'unknown' })
      setHydratedKey(null)
      return
    }
    const loaded = loadProfiles(accountKey)
    const storedActive = loadJson(`${LS_ACTIVE_PROFILE}::${accountKey}`, DEFAULT_PROFILE)
    setSnapshot(loadJson(`${LS_SNAPSHOT}::${accountKey}`, null))
    setSnapshotFetchedAt(loadJson(`${LS_SNAPSHOT_AT}::${accountKey}`, null))
    setLastRun(loadJson(`${LS_LAST_RUN}::${accountKey}`, null))
    setUseStaleSnapshot(false)
    setTradeOffice(loadJson(`${LS_TRADE_OFFICE}::${accountKey}`, {}))
    setForeignTargets(loadJson(`${LS_FOREIGN}::${accountKey}`, []))
    setProfileWindows(loadJson(`${LS_WINDOWS}::${accountKey}`, {}))
    // Filtered to real booleans on the way in, the same discipline `mayRelay`
    // takes and for the same reason: this map goes straight into the request,
    // and the backend's lax `bool` would read a stored "yes" from a
    // hand-edited origin as an attendance nobody declared.
    setProfileAttendance(attendanceMapOnly(loadJson(`${LS_NPC_ATTENDED}::${accountKey}`, {})))
    // Same discipline, same reason: a stored "yes" from a hand-edited origin
    // would read as a declared night through the backend's lax `bool`, and
    // section 6's rules would then govern a profile nobody named.
    setProfileOvernight(overnightMapOnly(loadJson(`${LS_OVERNIGHT}::${accountKey}`, {})))
    setReservedWindow(loadJson(`${LS_RESERVED_WINDOW}::${accountKey}`, null))
    setCropCeilings(loadJson(`${LS_CROP_CEILING}::${accountKey}`, {}))
    setShipOnlyTo(loadJson(`${LS_SHIP_ONLY_TO}::${accountKey}`, {}))
    setRelayFor(loadJson(`${LS_RELAY_FOR}::${accountKey}`, {}))
    setStockFloors(loadJson(`${LS_STOCK_FLOOR}::${accountKey}`, {}))
    setNpcFeedstock(loadJson(`${LS_NPC_FEEDSTOCK}::${accountKey}`, {}))
    setMaxBusy(loadJson(`${LS_MAX_BUSY}::${accountKey}`, {}))
    // A role outside the five is dropped on the way in, the same way a stored
    // crop spend is: the backend answers an unknown role with a 422, so a
    // stale one from a future build would 422 every plan over a value the
    // selector cannot show.
    //
    // And said out loud, on the same standard and for a larger consequence: a
    // dropped role takes that village's targets AND its spend, so four
    // defensive villages silently revert to keeping their own production --
    // a tenth of what they need -- while the plan reads feasible.
    const roles = stripUnknownRoles(
      loadJson(`${LS_VILLAGE_ROLES}::${accountKey}`, {}),
      loadJson(`${LS_ROLE_TEMPLATES}::${accountKey}`, {})
    )
    // Filtered, not loaded raw: this map goes straight into the request, and
    // the backend's lax `bool` reads a stored "yes" as TRUE -- a village relaying
    // someone else's crop on the strength of a value with no box to clear it
    // from. See `relayFlagsOnly` for why it is dropped rather than coerced, and
    // silently rather than with a note.
    setMayRelay(relayFlagsOnly(loadJson(`${LS_MAY_RELAY}::${accountKey}`, {})))
    setVillageRoles(roles.villageRoles)
    setRoleTemplates(roles.templates)
    setRolesDropped(roles)
    // Crop is dropped on the way in. An earlier build let one be typed, and
    // the input no longer shows a crop box -- so a stored crop figure could be
    // neither seen nor cleared while still riding along on every request and
    // 422-ing the plan over a number the operator cannot find.
    //
    // And the drop is stated, not silent. Removing that figure makes a
    // previously-silenced CRITICAL reappear on the next plan, and the operator
    // had nothing on screen connecting the two -- while loading the very same
    // figure from a FILE raises a loud error. The note clears itself on the
    // next hydration, because the stripped map is what gets saved back.
    const stored = stripStoredCropSpends(loadJson(`${LS_CONSUMPTION}::${accountKey}`, {}))
    setConsumption(stored.consumption)
    setCropSpendsDropped(stored.droppedFrom)
    setDayCheck(null)
    setProfiles(loaded)
    setActiveProfile(loaded[storedActive] ? storedActive : Object.keys(loaded)[0])
    setMerchantModel({
      ...DEFAULT_MERCHANT_MODEL,
      ...loadJson(`${LS_MERCHANT}::${accountKey}`, DEFAULT_MERCHANT_MODEL),
    })
    setSelected({})
    setPlan(null)
    setSetupReport(null)
    setHydratedKey(accountKey)
  }, [accountKey])

  // Ask the server whether a setup is saved, on arriving at an account.
  //
  // Zero game requests, and the whole reason it is automatic: an operator
  // reaching this app on a fourth origin needs to know a saved copy EXISTS
  // without first guessing that it might. A 404 is the answer "nothing has
  // ever been saved here" and is recorded as such rather than as a failure --
  // it is the state that invites importing a file.
  useEffect(() => {
    if (!accountKey) return undefined
    let current = true
    setServerSetup({ state: 'checking' })
    api
      .get('/distribution/setup', { params: { account_key: accountKey } })
      .then((res) => {
        if (!current) return
        setServerSetup({ state: 'saved', savedAt: res.data.saved_at })
      })
      .catch((err) => {
        if (!current) return
        if (err.response?.status === 404) {
          setServerSetup({ state: 'none' })
          return
        }
        setServerSetup({
          state: 'error',
          message: errorDetail(err, 'the request failed'),
        })
      })
    // Cancelled on an account switch rather than compared afterwards: this
    // effect re-runs per account, so the flag is the account check.
    return () => {
      current = false
    }
  }, [accountKey])

  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_TRADE_OFFICE), tradeOffice)
  }, [tradeOffice, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_FOREIGN), foreignTargets)
  }, [foreignTargets, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_WINDOWS), profileWindows)
  }, [profileWindows, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_NPC_ATTENDED), profileAttendance)
  }, [profileAttendance, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_OVERNIGHT), profileOvernight)
  }, [profileOvernight, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_RESERVED_WINDOW), reservedWindow)
  }, [reservedWindow, hydratedKey, accountKey, storageKey])
  // A day-check result is a pure function of these inputs; the moment any of
  // them changes it describes a day that will never happen. Without this, the
  // green all-clear banner could sit on screen after the operator changed
  // everything it was computed from, mixing eras with the live Crop-now column.
  useEffect(() => {
    // Bump the revision so a day-check request already in flight cannot install
    // its stale result after these inputs changed (same discipline as the route
    // sheet below). Clearing the visible result alone does not stop an
    // in-progress request from resurrecting a pre-edit "all clear".
    dayCheckInputRev.current += 1
    setDayCheck(null)
    // Trade Office and the merchant model belong here too: the day check now
    // routes every profile through the same optimizer /plan uses, so its
    // request carries the whole plan payload. Before that it sent only the
    // snapshot and the allocations, and this list still described that older,
    // narrower request -- so editing a Trade Office level left the green
    // all-clear on screen describing a day computed from the old capacity.
    //
    // Section 6's overnight declaration belongs here for the sharper of the
    // two reasons: per segment it decides both which one is graded against the
    // closing deadline and when the morning threshold is read. A day check
    // computed with the post-midnight half of a split night undeclared read
    // that threshold at 00:00.
    //
    // Above the array rather than inside it, on the rule stated at the plan
    // effect below: `depOrder.js` text-scans the array's whole span, comments
    // included, and the word "floor" is a `const` further down this file.
  }, [
    profiles,
    profileWindows,
    cropCeilings,
    snapshot,
    foreignTargets,
    tradeOffice,
    shipOnlyTo,
    stockFloors,
    npcFeedstock,
    maxBusy,
    consumption,
    villageRoles,
    mayRelay,
    roleTemplates,
    merchantModel,
    // Section 7's attendance decides whether a segment's NPC allowance exists
    // at all, so a day computed with the night marked awake is a different day.
    // The reserved window moves arrival times, and the day check reads them.
    profileAttendance,
    profileOvernight,
    reservedWindow,
  ])
  // Same rule for the route sheet, with higher stakes: its rows are copied
  // field by field into the game's trade-route dialog. A sheet computed from
  // yesterday's allocations, Trade Office levels, merchant model, tributes or
  // snapshot prescribes routes for a world that no longer exists. The
  // revision counter also kills in-flight builds: a response that started
  // before the latest edit must not resurrect a stale sheet.
  const planInputRev = useRef(0)
  useEffect(() => {
    planInputRev.current += 1
    setPlan(null)
    // The conflict named a digest this plan no longer has, so it describes
    // nothing once the inputs move. Cleared with the plan it belonged to.
    setYamlConflict(null)
    // An execution preview/result belongs to the plan it was run against; the
    // moment any input changes it describes routes for a stale plan. (A live run
    // already committed in-game, and its success/failure is reported via toast;
    // the response table is intentionally allowed to reset with the plan.)
    setExecResult(null)
    // Depends on `snapshot`, not the derived `villages`: `villages` is declared
    // further down, and naming it here would evaluate this dependency array in
    // its temporal dead zone — a ReferenceError that drops the whole planner
    // into the error boundary. `villages` is `snapshot?.villages ?? []`, so
    // snapshot changing is exactly the signal we want.
    // profileWindows and activeProfile belong here for the same reason: the
    // payload now carries dispatch_window drawn from the active profile's
    // hours, so nudging those hours changes every route's send time. The
    // hours inputs sit in the global bar ABOVE the stage nav, editable while
    // the sheet is on screen -- so without these the operator reads "Send at
    // 08:20", moves the window, and the sheet still says 08:20 while a live
    // run would create the route at a different hour.
    //
    // profileAttendance is here because it MOVES CARGO: the active profile's
    // `npc_attended` rides in the payload, and false zeroes the conversion
    // allowance -- so a route set built while it said "awake" prescribes
    // deliveries nothing funds.
    //
    // profileOvernight is here for the same class of reason: `overnight`
    // decides whether section 6's rules govern this route set -- no latency
    // target, and every merchant home before the window closes -- so a sheet
    // built while the night was undeclared prescribes departures the closing
    // deadline would have refused.
    //
    // relayFor and pruneToWindow are here because they were MISSING, and both
    // are in the payload: `relay_for` decides which village forwards whose
    // cargo and `prune_to_window` decides what the run leaves behind. The
    // sheet is retyped into the game's trade-route dialog, so a route table
    // that outlived the tier it describes is not a stale display -- it is
    // wrong instructions for a live account. Nothing tests these arrays
    // against `buildPlanPayload`, which is how two fields drifted out of one.
    //
    // And every one of these sentences is above the array rather than inside
    // it, which is now a rule and not a habit: `depOrder.js` guards this hook
    // by TEXT-scanning everything between `}, [` and `]`, comments included, so
    // any prose in there naming a `const` declared further down the file reads
    // as a forward reference. A dumb scan cannot silently skip what a clever
    // regex fails to match, which is exactly why it is dumb.
  }, [
    allocations,
    tradeOffice,
    shipOnlyTo,
    stockFloors,
    npcFeedstock,
    maxBusy,
    consumption,
    villageRoles,
    mayRelay,
    relayFor,
    roleTemplates,
    merchantModel,
    foreignTargets,
    snapshot,
    profileWindows,
    activeProfile,
    profileAttendance,
    profileOvernight,
    reservedWindow,
    pruneToWindow,
  ])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_CROP_CEILING), cropCeilings)
  }, [cropCeilings, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_SHIP_ONLY_TO), shipOnlyTo)
  }, [shipOnlyTo, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_RELAY_FOR), relayFor)
  }, [relayFor, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_STOCK_FLOOR), stockFloors)
  }, [stockFloors, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_NPC_FEEDSTOCK), npcFeedstock)
  }, [npcFeedstock, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_MAX_BUSY), maxBusy)
  }, [maxBusy, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_CONSUMPTION), consumption)
  }, [consumption, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_VILLAGE_ROLES), villageRoles)
  }, [villageRoles, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_MAY_RELAY), mayRelay)
  }, [mayRelay, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_ROLE_TEMPLATES), roleTemplates)
  }, [roleTemplates, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_PROFILES), profiles)
  }, [profiles, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_ACTIVE_PROFILE), activeProfile)
  }, [activeProfile, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_MERCHANT), merchantModel)
  }, [merchantModel, hydratedKey, accountKey, storageKey])

  // Memoised so it does not become a fresh array on every render, which would
  // re-run the plan callback and the totals memo for no reason.
  const villages = useMemo(() => snapshot?.villages ?? [], [snapshot])

  // ── Section 7: who is trading, and whether anyone has to say ──────────
  //
  // Declared HERE, above every callback that reads them, for the reason the
  // dependency-array comments below give twice over: naming a value declared
  // further down the component evaluates it in its temporal dead zone, which
  // has crashed this page into the error boundary twice.
  //
  // The backend's own predicate for "an answer is required" is a
  // `stock_floor_fraction` above zero on any village, so it is imported rather
  // than restated -- one rule, checked on the cell that sets it and on the
  // button that sends it.
  const attendanceIsRequired = useMemo(() => npcAttendanceRequired(stockFloors), [stockFloors])
  const profilesWithHours = useMemo(
    () =>
      Object.keys(profiles).filter(
        (name) => dispatchWindowFor(profileWindows[name] ?? DEFAULT_WINDOWS[name] ?? null) != null
      ),
    [profiles, profileWindows]
  )
  // Every profile that owes an answer and has not given one. Empty unless a
  // floor is declared: an account with no NPC floor is asked nothing.
  const attendanceOwed = useMemo(
    () => (attendanceIsRequired ? unansweredAttendance(profilesWithHours, profileAttendance) : []),
    [attendanceIsRequired, profilesWithHours, profileAttendance]
  )
  // The one profile `/plan` and `/night-profile` actually send, which is the
  // gate a single-window build has to pass. The whole-day paths check the
  // list above instead, because they send every segment.
  const activeAttendanceOwed = attendanceOwed.includes(activeProfile)

  const fetchSnapshot = async () => {
    // The account can change mid-request (switch/disconnect); a response that
    // lands afterwards must not overwrite the new account's state with the old
    // account's villages.
    const requestedFor = accountKey
    setFetching(true)
    try {
      const res = await api.get('/distribution/snapshot', { timeout: 0 })
      if (requestedFor !== currentAccountKey()) return
      const fetchedAt = Date.now()
      setSnapshot(res.data)
      setSnapshotFetchedAt(fetchedAt)
      setUseStaleSnapshot(false)
      saveJson(`${LS_SNAPSHOT}::${requestedFor}`, res.data)
      saveJson(`${LS_SNAPSHOT_AT}::${requestedFor}`, fetchedAt)
      setPlan(null)
      // A new snapshot can have different village ids — drop stale selections,
      // and the setup-file report with them: it was computed against the
      // previous village list, so it can name villages that are now gone.
      setSelected({})
      setSetupReport(null)
      // Villages get lost, chiefed or renamed between fetches; allocations kept
      // for ids no longer in the snapshot would fail every future plan call.
      // Prune every profile, not just the active one.
      const ids = new Set(res.data.villages.map((v) => v.village_id))
      setProfiles((prev) =>
        Object.fromEntries(
          Object.entries(prev).map(([name, alloc]) => [name, pruneAllocations(alloc, ids)])
        )
      )
      const fresh = res.data.villages.filter((v) => tradeOffice[v.village_id] == null)
      toast.success(
        `Read ${res.data.villages.length} villages in ${res.data.requests_used} requests` +
          (fresh.length ? ` · ${fresh.length} need a Trade Office level` : '')
      )
    } catch (err) {
      toast.error(errorDetail(err, 'Could not read account state'))
    } finally {
      setFetching(false)
    }
  }

  // Read the Trade Office level of every village from the game instead of
  // asking the operator to type 23 of them. The levels were always available:
  // /status/export?include_buildings=true parses each village's dorf2, and the
  // Trade Office is gid 28. Without this the operator switches village in
  // Travian 23 times just to read a number the app can fetch.
  //
  // Deliberately its own action rather than part of Fetch state: it costs two
  // requests PER VILLAGE where the snapshot costs three or four in total, so it
  // must be priced separately and chosen, not paid by surprise.
  const scanTradeOffices = async () => {
    if (!villages.length) {
      toast.error('Fetch account state first')
      return
    }
    const requestedFor = accountKey
    setScanningTradeOffices(true)
    try {
      const res = await api.get('/status/export', {
        // Two pages per village through the stealth throttler runs minutes past
        // the client's default timeout, same reasoning as the Buildings page.
        params: { include_buildings: true },
        timeout: 0,
      })
      if (requestedFor !== currentAccountKey()) return

      const levels = {}
      let missing = 0
      for (const village of res.data?.villages ?? []) {
        const buildings = village.buildings
        if (!Array.isArray(buildings)) {
          missing += 1
          continue
        }
        // A village with no Trade Office has no gid-28 slot, and that is a
        // CONFIRMED zero, not an unknown -- which is the whole point of reading
        // it rather than guessing. Levels default to 0 only when the village's
        // buildings could not be read at all, and those are reported instead.
        const slot = buildings.find((b) => Number(b.gid) === TRADE_OFFICE_GID)
        levels[village.id] = slot ? Number(slot.level) || 0 : 0
      }

      const known = new Set(villages.map((v) => v.village_id))
      const applied = Object.fromEntries(
        Object.entries(levels).filter(([vid]) => known.has(Number(vid)))
      )
      setTradeOffice((prev) => ({ ...prev, ...applied }))
      const count = Object.keys(applied).length
      toast.success(
        `Read the Trade Office level of ${count} village(s) from the game` +
          (missing ? ` · ${missing} village(s) could not be read` : '')
      )
    } catch (err) {
      toast.error(errorDetail(err, 'Could not read Trade Office levels'))
    } finally {
      setScanningTradeOffices(false)
    }
  }

  // The shared request body for both /plan and /execute (execute recomputes
  // the same plan server-side, so it must send identical inputs). Only entries
  // the planner can act on go in: `keep` equals the backend default, and a
  // village whose rate is unknown for this resource is not plannable.
  // ── Setup file: the hand-typed columns, saved and reloaded ──────────
  // Trade Office and the crop alert are typed once per account, but they live
  // in localStorage, which is per ORIGIN — the same app on :80, on :8001, on
  // the LAN address and over Tailscale keeps four separate copies. Exporting
  // them makes the typing survive that, and every rebuild.
  // ONE document, for the file and for the server alike.
  //
  // Not two builders and not two formats: the server stores exactly what the
  // file export writes, validates it with the plan request's own rules, and
  // returns it byte for byte -- so a document can move between the two paths
  // and `parseSetup` reads either. A second shape here is how the two would
  // drift into disagreeing about what an empty relay list means.
  //
  // Returns null with a toast where there is nothing to write, because "you
  // have saved nothing" and "you saved a blank sheet" are different states the
  // server itself distinguishes, and writing the second by accident would
  // destroy the first.
  const setupDocument = useCallback(() => {
    if (!villages.length) {
      toast.error('Fetch account state first, so the setup records village names too')
      return null
    }
    const typed = villages.filter(
      (v) =>
        tradeOffice[v.village_id] != null ||
        cropCeilings[v.village_id] != null ||
        shipOnlyTo[v.village_id] != null ||
        relayFor[v.village_id]?.length > 0 ||
        stockFloors[v.village_id] != null ||
        isFeedstockList(npcFeedstock[v.village_id]) ||
        maxBusy[v.village_id] != null ||
        villageRoles[v.village_id] != null ||
        mayRelay[v.village_id] != null ||
        declaresConsumption(consumption[v.village_id])
    ).length
    const named = Object.entries(profiles).filter(([, a]) => Object.keys(a ?? {}).length)
    const templated = Object.keys(roleTemplates).length
    if (!typed && !named.length && !templated) {
      toast.error('Nothing typed yet — fill in a Trade Office level, crop alert or allocation first')
      return null
    }
    const parts = []
    if (typed) parts.push(`${typed} village(s)`)
    if (named.length) parts.push(`${named.length} profile(s)`)
    if (templated) parts.push(`${templated} role template(s)`)
    return {
      document: buildSetup({
        account: accountKey,
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
        roles: roleTemplates,
        profiles,
        profileWindows,
        // The answer that decides whether a night route set is funded at all.
        // It used to live only in localStorage -- which is per ORIGIN -- so it
        // did not follow the operator between :80, :8001, the LAN address and
        // Tailscale the way the hours beside it did, and a save-then-reload
        // lost it silently. Carrying it is what the v7 bump is for.
        npcAttended: profileAttendance,
        // Section 6's declaration, carried for the same reason and with the
        // same consequence for losing it: a split night whose post-midnight
        // half arrives undeclared has its 60% morning floor measured at 00:00
        // and its overruns unreported. That is what v8 is for.
        overnight: profileOvernight,
        // Section 7's reserved burst window, and v9. The last owned answer that
        // neither persistence path carried: it lived only in localStorage,
        // which is per browser origin, so it did not follow the operator
        // between :80, :8001, the LAN address and Tailscale -- exactly what the
        // storage panel two cards up warns about. Confirmed against a real
        // saved document, whose top level held every other owned field and not
        // this one.
        reservedWindow,
        merchantModel,
        foreignTargets,
        exportedAt: new Date().toISOString(),
      }),
      summary: parts.join(' and '),
    }
  }, [
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
    roleTemplates,
    profiles,
    profileWindows,
    profileAttendance,
    profileOvernight,
    reservedWindow,
    merchantModel,
    foreignTargets,
    accountKey,
    toast,
  ])

  const exportSetup = useCallback(() => {
    const built = setupDocument()
    if (!built) return
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
    downloadJson(setupFilename(playerName, stamp), built.document)
    toast.success(`Saved ${built.summary} — keep the file, load it after a rebuild`)
  }, [setupDocument, playerName, toast])

  // Apply an already-PARSED setup over what is on screen.
  //
  // Split out of the file path so the server path is the same path: a document
  // loaded from the store goes through `parseSetup` and this merge exactly as
  // a file does, so the load report, the account check, the crop-spend refusal
  // and the relay-tier rules all behave identically. The store keeps the
  // document verbatim, which means one written by a NEWER build can come back
  // out of it -- and `parseSetup` is the only thing that knows to refuse that
  // rather than half-load it.
  //
  // `where` names the source in the messages, because "this file was exported
  // from a different account" is the wrong sentence about a server document.
  const mergeParsedSetup = useCallback(
    (setup, where) => {
      const merged = mergeSetup({
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
        roles: roleTemplates,
        profiles,
        profileWindows,
        npcAttended: profileAttendance,
        overnight: profileOvernight,
        reservedWindow,
        foreignTargets,
      })
      setTradeOffice(merged.tradeOffice)
      setForeignTargets(merged.foreignTargets)
      setCropCeilings(merged.cropCeilings)
      setShipOnlyTo(merged.shipOnlyTo)
      setRelayFor(merged.relayFor)
      setStockFloors(merged.stockFloors)
      setNpcFeedstock(merged.npcFeedstock)
      setMaxBusy(merged.maxBusy)
      setConsumption(merged.consumption)
      setVillageRoles(merged.villageRoles)
      setMayRelay(merged.mayRelay)
      setRoleTemplates(merged.roles)
      setProfiles(merged.profiles)
      setProfileWindows(merged.profileWindows)
      setProfileAttendance(merged.npcAttended)
      setProfileOvernight(merged.overnight)
      // `mergeSetup` already decided this: the document wins where it has a
      // window and says nothing where it does not, so a v8 file cannot wipe the
      // one on screen.
      setReservedWindow(merged.reservedWindow)
      // Capacity is server-calibrated, so a file that carries a calibration is
      // more trustworthy than this build's default. Absent, the default stands.
      if (merged.merchantModel) {
        setMerchantModel({ ...DEFAULT_MERCHANT_MODEL, ...merged.merchantModel })
      }
      // Land on a profile the file actually brought, so its numbers are what the
      // operator sees rather than whichever profile happened to be selected.
      const [first] = merged.report.profilesLoaded
      if (first) setActiveProfile(first)
      // The source travels with the report: it is rendered as a sentence, and
      // "from the setup file" is the wrong sentence about a server document.
      setSetupReport({ ...merged.report, source: where })
      setPasteOpen(false)
      setPasteText('')
      // The plan was built from the old values, so it no longer describes the
      // inputs on screen.
      setPlan(null)
      const parts = []
      if (merged.report.loaded) parts.push(`${merged.report.loaded} village(s)`)
      if (merged.report.profilesLoaded.length) {
        parts.push(`profile(s) ${merged.report.profilesLoaded.join(', ')}`)
      }
      if (merged.report.rolesLoaded.length) {
        parts.push(`role(s) ${merged.report.rolesLoaded.join(', ')}`)
      }
      toast.success(`Loaded ${parts.join(' and ') || 'nothing'} from the ${where}`)
    },
    [
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
      roleTemplates,
      profiles,
      profileAttendance,
      profileOvernight,
      profileWindows,
      reservedWindow,
      foreignTargets,
      toast,
    ]
  )

  const applyParsedSetup = useCallback(
    (setup, where) => {
      if (!villages.length) {
        toast.error(`Fetch account state first — a ${where} cannot be matched to villages without it`)
        return
      }
      // A document from another account would apply its levels to whatever
      // village happens to share an id, which is a silently wrong plan rather
      // than a visible error. Refuse unless the operator insists.
      //
      // Held as pending state rather than asked with `window.confirm`, because
      // Chrome's "Prevent this page from creating additional dialogs" makes a
      // later `confirm()` return false with nothing on screen -- and a load that
      // silently does nothing is indistinguishable from a load that worked.
      if (!setupMatchesAccount(setup, accountKey)) {
        setPendingSetup({ setup, where })
        return
      }
      mergeParsedSetup(setup, where)
    },
    [villages, accountKey, mergeParsedSetup, toast]
  )

  const applySetupText = useCallback(
    (text) => {
      let setup
      try {
        setup = parseSetup(text)
      } catch (err) {
        toast.error(err instanceof SetupFileError ? err.message : 'Could not read that file')
        return
      }
      applyParsedSetup(setup, 'setup file')
    },
    [applyParsedSetup, toast]
  )

  // ── The setup, on the server ────────────────────────────────────────
  //
  // Three calls, and the interesting one is the 404: it means nothing has ever
  // been saved for this account, which is not an error and is not the same as
  // a saved document that happens to be empty. Reported as the state it is.
  const saveSetupToServer = useCallback(async () => {
    const built = setupDocument()
    if (!built) return
    setSetupBusy('saving')
    try {
      const res = await api.put('/distribution/setup', built.document, {
        params: { account_key: accountKey },
      })
      setServerSetup({ state: 'saved', savedAt: res.data.saved_at })
      toast.success(`Saved ${built.summary} to the server — every origin reads this one`)
    } catch (err) {
      // `errorDetail` already handles both shapes the store answers with: a
      // plain sentence for the cross-row refusals, and a Pydantic field-error
      // list for everything the schema catches.
      toast.error(errorDetail(err, 'The server refused this setup'))
    } finally {
      setSetupBusy(null)
    }
  }, [setupDocument, accountKey, toast])

  const loadSetupFromServer = useCallback(async () => {
    setSetupBusy('loading')
    try {
      const res = await api.get('/distribution/setup', {
        params: { account_key: accountKey },
      })
      setServerSetup({ state: 'saved', savedAt: res.data.saved_at })
      // Through `parseSetup` and the same merge a file takes. The document
      // came back verbatim, so this is where a newer build's version gets
      // refused rather than half-loaded.
      let setup
      try {
        setup = parseSetup(JSON.stringify(res.data.setup))
      } catch (err) {
        toast.error(
          err instanceof SetupFileError
            ? `The saved setup cannot be read: ${err.message}`
            : 'The saved setup could not be read'
        )
        return
      }
      applyParsedSetup(setup, 'saved setup')
    } catch (err) {
      if (err.response?.status === 404) {
        setServerSetup({ state: 'none' })
        toast.error('Nothing is saved on the server for this account yet')
        return
      }
      toast.error(errorDetail(err, 'Could not read the saved setup'))
    } finally {
      setSetupBusy(null)
    }
  }, [accountKey, applyParsedSetup, toast])

  const forgetServerSetup = useCallback(async () => {
    setSetupBusy('forgetting')
    try {
      await api.delete('/distribution/setup', { params: { account_key: accountKey } })
      setServerSetup({ state: 'none' })
      toast.success('The saved setup is gone from the server')
    } catch (err) {
      if (err.response?.status === 404) {
        // Already the state it wanted to reach, so it is recorded rather than
        // reported as a failure -- and for four rounds the next line said
        // `toast.error` two lines under a comment saying exactly that. A forget
        // that finds nothing forgot nothing AND left the operator where they
        // asked to be, which is a success with a caveat, not an error: red on
        // an outcome the operator wanted teaches them to distrust red.
        setServerSetup({ state: 'none' })
        toast.success('Nothing was saved on the server, so there was nothing to forget')
        return
      }
      toast.error(errorDetail(err, 'Could not delete the saved setup'))
    } finally {
      setSetupBusy(null)
    }
  }, [accountKey, toast])

  const onSetupFileChosen = useCallback(
    (event) => {
      const file = event.target.files?.[0]
      // Cleared straight away so choosing the same file twice fires again.
      event.target.value = ''
      if (!file) return
      const reader = new FileReader()
      reader.onload = () => applySetupText(String(reader.result ?? ''))
      reader.onerror = () => toast.error('Could not read that file')
      reader.readAsText(file)
    },
    [applySetupText, toast]
  )

  // Every cell this page has already outlined and named, gathered so the Build
  // plan button can refuse instead of posting a request it knows will come back
  // 422. Same rule and the same predicates as the marks themselves -- see
  // `plannerBlockers.js` for why it is computed from state rather than swept
  // out of the DOM.
  const blockers = useMemo(
    () =>
      planBlockers({
        villages,
        maxBusy,
        stockFloors,
        consumption,
        villageRoles,
        roleTemplates,
        foreignTargets,
        merchantModel,
      }),
    [
      villages,
      maxBusy,
      stockFloors,
      consumption,
      villageRoles,
      roleTemplates,
      foreignTargets,
      merchantModel,
    ]
  )

  // The six World & merchants boxes, checked against the bounds the file parser
  // and the plan request already enforce. Two of them -- an even map span and a
  // reserve past 20 -- were marked NOWHERE before this: the box accepted the
  // figure, the plan came back 422, and nothing on screen pointed at the cell.
  const merchantProblems = useMemo(() => merchantModelProblems(merchantModel), [merchantModel])

  const buildPlanPayload = useCallback(() => {
    // Both maps are built by tested utils rather than inline here: the seed and
    // the skip rules are the two places a wrong request came from, and neither
    // was reachable from a test while it lived in the page.
    const sendAllocations = allocationsForRequest(
      allocations,
      villageRoles,
      villages.map((v) => v.village_id)
    )
    // A role a village claims but nobody has given a template to is SKIPPED, so
    // the backend's 422 reaches the operator naming the villages and the role.
    const claimed = new Set(
      villages.map((v) => villageRoles[v.village_id]).filter((role) => role != null)
    )
    const sendRoles = rolesForRequest(roleTemplates, claimed)
    // The active profile's own hours. Without them the optimizer phases each
    // route's send time anywhere in its cycle, so a profile that runs only part
    // of the day gets sheet rows -- and, via /execute, REAL routes -- that fire
    // while a different profile is meant to be running. Read inline rather than
    // through windowFor(), which is declared further down: naming it in the
    // dependency array below would evaluate it in its temporal dead zone.
    const hours = profileWindows[activeProfile] ?? DEFAULT_WINDOWS[activeProfile] ?? null
    // A zero-width or unparseable window is sent as null, not as a broken pair:
    // the backend rejects start === end, and an all-day profile needs no phasing.
    // `dispatchWindowFor` collapses all three unusable shapes to null, which is
    // the reading the request already had -- it just had its own copy of it.
    const dispatchWindow = dispatchWindowFor(hours)

    return {
      snapshot: villages,
      dispatch_window: dispatchWindow,
      // Section 7, and the field that used to be unsendable: whether the
      // operator is at the marketplace during THESE hours. Sent whenever it has
      // been answered, WINDOW OR NOT: a route set with no window runs round the
      // clock, which is all 24 hours including the eight nobody is at the
      // Marketplace, and the backend reads a missing answer there as
      // unattended. It used to be dropped in that case on the reasoning that
      // round the clock "has no night hours to mis-fund" -- backwards, and it
      // threw away a `true` the operator could still see on screen.
      // Omitted only when nothing has been answered; `buildPlan` refuses to
      // send in that case, so the backend's 422 is a backstop, not the path.
      ...npcAttendedField(attendanceFor(profileAttendance, activeProfile)),
      // Section 6, and the second field that used to be unsendable: whether
      // THESE hours are the ones the operator sleeps through, so the closing
      // deadline and the suspended latency target apply to them. Omitted when
      // nothing has been declared, which is the resting state and asks the
      // backend to derive it from the window -- right for a night stated as
      // one 23:00-07:00 pair. Omitted with no window too, and that half is the
      // backend's own refusal rather than a convenience: a declaration with no
      // window has no closing minute to be measured against, so
      // `_overnight_needs_hours_to_be_overnight` raises on it.
      ...overnightField({
        declared: profileOvernight[activeProfile],
        hasWindow: dispatchWindow != null,
      }),
      // Kept clear of arrivals for the manual NPC burst. Omitted when unset,
      // and omitted for a zero-width pair: an empty reserved window reserves
      // nothing, so sending one would only make the request look like it
      // asked for something.
      ...(dispatchWindowFor(reservedWindow)
        ? { reserved_window: dispatchWindowFor(reservedWindow) }
        : {}),
      // Plan-time, not run-time: with pruning the window is genuinely enforced
      // and the escaping firings are a note about a dependency; without it they
      // are a critical over-delivery. /plan must see it to weigh them.
      ...(pruneToWindow && dispatchWindow ? { prune_to_window: true } : {}),
      // A route inside a profile has that profile's hours to deliver in, not the
      // two-hour default the backend falls back to. The default is right for a
      // round-the-clock set, where a late arrival really is late; inside an
      // 8-hour window it forces short cycles, and a short cycle against a long
      // haul keeps several shipments in the air at once and spends merchants on
      // speed nobody asked for. Measured: the same night plan came out 46
      // routes / 120 merchants against an 8h target and 48 / 135 against 2h.
      ...(dispatchWindow
        ? {
            max_latency_hours:
              (dispatchWindow[1] - dispatchWindow[0] + MINUTES_IN_DAY) % MINUTES_IN_DAY / 60,
          }
        : {}),
      config: villages
        .map((v) => ({
          village_id: v.village_id,
          // Omitted when nobody typed one, so "unknown" is distinguishable from
          // "checked in game, there is no Trade Office here". Both plan
          // identically -- the backend's field is `Field(default=0)` and its
          // sizing reads `.get(vid, 0)`, and flooring an unknown level to 0 is
          // the SAFE direction: understating capacity over-provisions merchants
          // while overstating it breaches the merchant budget invisibly. What
          // changes is what the request CLAIMS.
          //
          // It matters because of the merchant-calibration finding, which reads
          // a level-0 village as "read the base capacity off this one". Naming a
          // village whose level nobody has checked is account-killer #8 reached
          // through the mechanism meant to settle the model: if it is really
          // Trade Office 13 the dialog reads about 9,000, that becomes
          // `merchant_base_capacity`, and every route is sized to cargo the
          // merchants cannot carry. The backend already filters the sample to
          // villages with a config ROW -- which is why the row itself is dropped
          // below when a village declares nothing at all, and which is the half
          // of this that today's backend can act on. A village that declares
          // something else keeps its row and still says nothing about a Trade
          // Office; closing that residue needs the backend to read
          // `"trade_office_level" in cfg.model_fields_set`.
          ...(tradeOffice[v.village_id] != null
            ? { trade_office_level: Number(tradeOffice[v.village_id]) }
            : {}),
          // Omitted when unset, so an undeclared village's row is byte-identical
          // to before. A role sent WITHOUT its template is a 422, which is why
          // `sendRoles` above carries every role some village claims.
          ...(villageRoles[v.village_id] != null ? { role: villageRoles[v.village_id] } : {}),
          // Omitted when unset, so an undeclared village's row is unchanged:
          // absent means "take the role template's answer", and on a village
          // with no role at all leaves the crop-sign inference in place.
          ...(mayRelay[v.village_id] != null ? { may_relay: mayRelay[v.village_id] } : {}),
          // Both omitted when unset, so an ordinary village's row is byte-identical
          // to before: absent means "unrestricted" and "no floor" on the backend.
          // An EMPTY ship_only_to list is sent, because it means "nobody".
          ...(shipOnlyTo[v.village_id] != null ? { ship_only_to: shipOnlyTo[v.village_id] } : {}),
          // Profile section 5's relay tier, and the one field here whose EMPTY
          // list is dropped rather than sent. An empty `ship_only_to` means
          // "nobody", which is an answer; "forwards to nobody" says nothing that
          // omitting the field does not, so the backend refuses an empty list with
          // a 422 -- and the picker holds one for the moment between opening and
          // the first tick, which must not 422 the plan.
          ...(relayFor[v.village_id]?.length ? { relay_for: relayFor[v.village_id] } : {}),
          ...(stockFloors[v.village_id] != null
            ? { stock_floor_fraction: stockFloors[v.village_id] }
            : {}),
          // The feedstock override, dropped when there is none AND when the
          // picker holds an empty list: absent means "derive it", which is what
          // almost every village says, and an override of nothing is a statement
          // NPC cannot carry out -- the backend refuses one.
          ...npcFeedstockField(npcFeedstock[v.village_id]),
          // Omitted when unset, so a village with no ceiling is byte-identical to
          // before: absent means the fleet, less the account reserve, is the
          // budget. 0 IS sent -- it says every route from this village is a
          // budget breach, which is an answer.
          ...(maxBusy[v.village_id] != null
            ? { max_busy_merchants: Number(maxBusy[v.village_id]) }
            : {}),
          // An empty map is omitted too: the backend reads absent and {} the
          // same way, and sending {} would only make the request look like it
          // declares a spend. A village mid-edit with every box cleared is not
          // saying it spends nothing.
          ...(declaresConsumption(consumption[v.village_id])
            ? { consumption_per_hour: consumption[v.village_id] }
            : {}),
        }))
        // A row carrying nothing but its own id is dropped. It said only "this
        // village exists", which the snapshot above already says -- and while
        // it was sent, the backend's `declared = {c.village_id for c in
        // body.config}` filter on the merchant-calibration sample was a
        // tautology, because every village had a row. The same predicate
        // `buildSetup` applies to the exported document, for the same reason:
        // a village with nothing typed has nothing to declare.
        //
        // Behaviourally inert otherwise, and checked rather than assumed:
        // every consumer of `body.config` either iterates and skips rows
        // lacking its own field or reads `.get(vid, 0)`, so a village with no
        // row plans exactly as one whose row held only a floored level.
        .filter((row) => Object.keys(row).length > 1),
      allocations: sendAllocations,
      // Only the roles some village actually claims. A template nobody claims
      // is harmless on the backend, but sending it would put a half-typed
      // profile into every request made while one is being filled in.
      ...(Object.keys(sendRoles).length ? { roles: sendRoles } : {}),
      foreign_targets: usableForeignTargets(foreignTargets, villages),
      // Geometry defaults to the snapshot (map span + tribe-derived x1 merchant
      // speed) but the operator can override both for non-Europe 2 worlds.
      map_span: Number(merchantModel.map_span) || snapshot?.map_span,
      speed_fields_per_hour:
        Number(merchantModel.speed_fields_per_hour) || snapshot?.speed_fields_per_hour,
      merchant_base_capacity: Number(merchantModel.base_capacity) || undefined,
      // Both existed on the request and neither was ever sent, so the backend's
      // defaults were the only values reachable from this page. A reserve of 0
      // is valid (ship with everything) and a headroom of 0 restores the
      // pre-headroom packing exactly, so neither may be dropped by `|| default`.
      merchant_reserve: Number.isInteger(Number(merchantModel.merchant_reserve))
        ? Number(merchantModel.merchant_reserve)
        : undefined,
      merchant_headroom: Number.isFinite(Number(merchantModel.merchant_headroom))
        ? Number(merchantModel.merchant_headroom)
        : undefined,
      // A per-level bonus of 0 is valid (a world with no Trade Office scaling),
      // so preserve it — `|| undefined` would drop it and use the 0.2 default.
      trade_office_bonus_per_level: Number.isFinite(Number(merchantModel.bonus_per_to_level))
        ? Number(merchantModel.bonus_per_to_level)
        : undefined,
    }
  }, [
    villages,
    tradeOffice,
    shipOnlyTo,
    relayFor,
    stockFloors,
    npcFeedstock,
    maxBusy,
    consumption,
    villageRoles,
    mayRelay,
    roleTemplates,
    allocations,
    foreignTargets,
    merchantModel,
    snapshot,
    profileWindows,
    activeProfile,
    profileAttendance,
    profileOvernight,
    reservedWindow,
    pruneToWindow,
  ])

  const buildPlan = useCallback(async () => {
    if (!villages.length) {
      toast.error('Fetch account state first')
      return
    }
    // Re-check freshness from the LIVE clock, not the last render, so a stale
    // snapshot cannot be planned from by racing a not-yet-updated rendered value
    // (the button-disable is a UI hint; this is the authoritative guard).
    if (
      !useStaleSnapshot &&
      (snapshotFetchedAt == null || Date.now() - snapshotFetchedAt > SNAPSHOT_PLAN_TTL_MS)
    ) {
      toast.error(
        'Snapshot is stale — fetch fresh state, or tick “plan from this stale snapshot anyway”.'
      )
      return
    }
    // Every cell the page has already marked, refused rather than posted -- and
    // for four rounds the attendance answer below was the only guard here. A cap
    // of 99 on a 19-merchant village rendered "only 19 merchants here", set
    // `aria-invalid`, and this button posted it anyway; the refusal then arrived
    // as a 422 naming an internal village id, after a round trip, with nothing
    // on screen pointing back at the cell. Nothing is sent, the fields and the
    // villages are named, and the caret goes to the first of them.
    //
    // BEFORE the attendance guard, deliberately: a malformed figure is a typo
    // and a missing attendance answer is a question, and the question is often
    // asked BECAUSE of the typo -- a stock floor of 99% is still a stock floor
    // to `attendanceIsRequired`, so the operator was sent to Day & night to
    // answer for a figure the plan would have refused anyway.
    if (blockers.length) {
      toast.error(describeBlockers(blockers))
      setStage(blockers[0].stage)
      setCellFocus((prev) => ({
        label: blockers[0].focusLabel,
        seq: (prev?.seq ?? 0) + 1,
      }))
      return
    }
    // Section 7's attendance, refused here rather than by the backend. The 422
    // names village ids and a profile the operator cannot see from the button
    // they pressed; this names the profile and the stage that answers it.
    if (activeAttendanceOwed) {
      toast.error(
        `${activeProfile} runs a window and a village here keeps an NPC-backed stock ` +
          `floor, so the plan needs to know whether you are trading during those hours. ` +
          `Answer it under Day & night.`
      )
      setStage('day')
      return
    }
    // Guard against the account changing mid-request: a plan built from
    // account A's snapshot must not be presented under account B. The input
    // revision closes the same race for edits within one account.
    const requestedFor = accountKey
    const requestedRev = planInputRev.current
    setPlanning(true)
    try {
      const res = await api.post('/distribution/plan', buildPlanPayload())
      if (requestedFor !== currentAccountKey()) return
      if (requestedRev !== planInputRev.current) return
      setPlan(res.data)
      // A fresh plan is a fresh identity, so whatever the last export said
      // about a digest that has moved is now history.
      setYamlConflict(null)
      // AND THE STAGE DOES NOT MOVE. This used to end `setStage('plan')`, so
      // editing a 15-column table on Account and pressing Build plan threw the
      // operator onto another stage -- and navigating back remounted the table
      // with every `<details>` CLOSED, because `open` is DOM state React does
      // not restore. Every picker the operator had opened to read across the
      // row shut itself, on a press whose whole purpose is that re-planning
      // while tuning a target is free. The verdict chip beside the button is
      // the acknowledgement, and it is one click from the stage.
    } catch (err) {
      toast.error(errorDetail(err, 'Could not build a plan'))
    } finally {
      setPlanning(false)
    }
  }, [
    villages,
    toast,
    accountKey,
    currentAccountKey,
    buildPlanPayload,
    // Read by the live freshness guard above.
    snapshotFetchedAt,
    useStaleSnapshot,
    // Read by the attendance guard above.
    activeAttendanceOwed,
    activeProfile,
    // Read by the marked-cell guard above.
    blockers,
  ])

  // Section 10, in one call: the plan request the operator is looking at, plus
  // the digest of the plan they are looking AT.
  //
  // The digest is the confirmation step, in machine-readable form. Nothing on
  // the server holds a computed plan -- `/plan` is pure and stateless, which is
  // what makes tuning a target free -- so the export re-plans, and the digest
  // is what stops that being silent. A 409 means the plan MOVED, and it is
  // surfaced rather than retried: re-planning to make the download succeed
  // would hand over an authoritative-looking file describing a plan nobody
  // read, which is the one outcome this whole mechanism exists to prevent.
  const exportPlanYaml = useCallback(async () => {
    if (!plan?.plan_digest) {
      toast.error('Build a plan first — the export confirms a plan you have read')
      return
    }
    setExportingYaml(true)
    try {
      const res = await api.post(
        '/distribution/plan/yaml',
        { ...buildPlanPayload(), expected_plan_digest: plan.plan_digest },
        {
          // A YAML document on success and FastAPI's `{"detail": ...}` on a
          // refusal, so the transform is STATUS-DRIVEN. Returning the raw
          // string for both left the 409's own sentence unparsed -- and that
          // sentence is the only place both digests appear.
          responseType: 'text',
          transformResponse: [yamlResponseTransform],
        }
      )
      // The server names the file for the PLAN and not for the moment, so two
      // downloads of one plan are one file and a diff between two files is a
      // diff between two plans. Respected rather than reconstructed here,
      // which would be a second implementation of that convention.
      const filename = filenameFromDisposition(
        res.headers?.['content-disposition'],
        yamlFilename(plan.plan_digest)
      )
      downloadText(filename, res.data, 'application/yaml')
      setYamlConflict(null)
      toast.success(`Exported ${filename} — the plan you confirmed, as a document`)
    } catch (err) {
      if (isDigestConflict(err)) {
        // Kept on screen, with the server's own sentence: it names BOTH
        // digests, and "it moved" without saying from what to what is not
        // something anyone can check.
        setYamlConflict(errorDetail(err, 'This plan is not the one that was confirmed.'))
        return
      }
      toast.error(errorDetail(err, 'Could not render the plan as YAML'))
    } finally {
      setExportingYaml(false)
    }
  }, [plan, buildPlanPayload, toast])

  // Every profile with hours, as the segment list the backend plans one by
  // one. Shared by the day check AND whole-day execution, so the day the
  // operator simulated and the day they go live with are the same day.
  const buildSegments = useCallback(() => {
    // profileNames and windowFor are declared further DOWN the component, so
    // neither may appear here -- not in the body via closure alone nor in the
    // dependency array, which React evaluates at render and which has now
    // crashed this page twice with a temporal-dead-zone error. Everything is
    // derived from state declared above, and the deps are complete, so there
    // is no eslint-disable to hide the next mistake behind.
    const segments = []
    const skipped = []
    const unanswered = []
    for (const name of Object.keys(profiles)) {
      const w = profileWindows[name] ?? DEFAULT_WINDOWS[name] ?? null
      const pair = dispatchWindowFor(w)
      if (pair == null) {
        skipped.push(name)
        continue
      }
      const [start, end] = pair
      // Section 7: on a segmented request attendance is required on EVERY
      // segment as soon as any village keeps a stock floor, and it is a
      // property of the segment's hours -- so it is collected here rather
      // than taken from whichever profile happens to be selected. Named back
      // to the caller, which refuses to send: a 422 listing village ids does
      // not lead anyone to the profile that is silent.
      const attended = attendanceFor(profileAttendance, name)
      if (attended === null) unanswered.push(name)
      const per = profiles[name] ?? {}
      const sendAllocations = {}
      for (const resource of RESOURCES) {
        const usable = {}
        for (const [vid, a] of Object.entries(per[resource] ?? {})) {
          // Same rule as buildPlanPayload: a keep is silence unless the village
          // has a role, where it is the one way to say "not the template".
          if (a.mode !== 'keep' || villageRoles[Number(vid)] != null) usable[vid] = a
        }
        if (Object.keys(usable).length) sendAllocations[resource] = usable
      }
      segments.push({
        name,
        window: [start, end],
        allocations: sendAllocations,
        // A segment always carries its answer where there is one. `null` is
        // omitted rather than sent, because the field's own third state is
        // what the backend refuses on -- and it refuses naming the villages,
        // which is why the caller checks `unanswered` first.
        ...(attended === null ? {} : { npc_attended: attended }),
        // Section 6's declaration, on the same rule and in the place the
        // backend puts it: `DaySegmentInput.overnight`, because the hours live
        // here. `hasWindow` is unconditionally true on a segment -- `window` is
        // required and a zero-width one is refused above -- so the only thing
        // that omits it is the operator not having declared, which is what
        // asks the backend to derive.
        //
        // This is the field a SPLIT night needs. 23:00-00:00 wraps and derives
        // correctly; 00:00-07:00 wraps in neither direction, so undeclared it
        // was planned as a day segment: no NIGHT_OVERRUN for a 600-minute
        // round trip inside a 420-minute night, and the 60% morning floor
        // measured at 00:00 rather than 07:00.
        ...overnightField({ declared: profileOvernight[name], hasWindow: true }),
      })
    }
    return { segments, skipped, unanswered }
  }, [profiles, profileWindows, villageRoles, profileAttendance, profileOvernight])

  // Reads the local trace files the app wrote on previous live runs. Costs
  // nothing against the game, so it is safe to call whenever the operator opens
  // the panel rather than on a timer.
  const loadRunHistory = useCallback(async () => {
    setHistoryLoading(true)
    try {
      const res = await api.get('/distribution/run-history', { params: { limit: 20 } })
      setRunHistory(res.data)
    } catch (err) {
      toast.error(errorDetail(err, 'Could not read the run history'))
    } finally {
      setHistoryLoading(false)
    }
  }, [toast])

  /** Ask `/routes/revert-plan` what undoing one run would take, or do it.
   *
   * Read-only FIRST, always: `apply_disable` and `apply_delete` both default to
   * false on the wire and the panel offers them only once the read-only answer
   * has said what there is. That is the endpoint's own design -- "reverting is
   * deliberately not a single button" -- and it is also the only way the
   * operator can see `must_delete_by_hand` before deciding, which is the half
   * no button covers.
   *
   * Read-only is not free. Every origin the run touched costs two game requests
   * to re-read, and every step here is confirmed by reading the page back, so
   * the buttons carry their price the way every other action on this page does.
   */
  const requestRevert = useCallback(
    async (traceId, applyDisable, applyDelete) => {
      if (!traceId) return
      setRevert({ traceId, busy: true, result: null, error: null })
      try {
        const res = await api.post(
          '/distribution/routes/revert-plan',
          {
            trace_id: traceId,
            apply_disable: applyDisable,
            apply_delete: applyDelete,
            // The span the route ids were read against. Wrong, and every
            // destination resolves to the wrong tile.
            ...(Number(merchantModel.map_span) || snapshot?.map_span
              ? { map_span: Number(merchantModel.map_span) || snapshot?.map_span }
              : {}),
          },
          // Two paced reads per origin plus a write and its verifying re-read;
          // the same headroom the reconcile sweep takes.
          { timeout: 180000 }
        )
        setRevert({ traceId, busy: false, result: res.data, error: null })
        const wrote = applyDisable || applyDelete
        if (!wrote) return
        const outstanding = Object.keys(res.data.must_delete_by_hand ?? {}).length
        if (res.data.problems?.length) {
          toast.error(res.data.problems[0])
        } else if (outstanding) {
          toast.error(`Routes at ${outstanding} village(s) still need deleting by hand`)
        } else {
          toast.success('The run is undone — nothing left outstanding')
        }
      } catch (err) {
        setRevert({
          traceId,
          busy: false,
          result: null,
          error: errorDetail(err, 'Could not read what undoing this run would take'),
        })
      }
    },
    [merchantModel, snapshot, toast]
  )

  // Reads the same fields the rows render, so the strip can never disagree
  // with the table beneath it.
  const health = useMemo(
    () => summariseSnapshot(villages, tradeOffice),
    [villages, tradeOffice]
  )

  const visibleVillages = useMemo(
    () => sortVillages(filterVillages(villages, rowFilter, tradeOffice), sortKey, sortDir),
    [villages, rowFilter, tradeOffice, sortKey, sortDir]
  )

  // The bound a FILE cannot check: a cap against the village's own fleet, off
  // live state. Imported rather than recomputed on the cell -- the rule is
  // written and tested once, in `plannerSetup.js`, and the inline copy that
  // used to sit on the input was the one that actually ran.
  const unreachableFleets = useMemo(
    () => new Set(unreachableCaps(maxBusy, villages).map((c) => c.village_id)),
    [maxBusy, villages]
  )

  // The same four refusals the backend makes on a declared relay tier, computed
  // live and keyed by the relay whose list has to change. Same reasoning as
  // `unreachableFleets` above, the same shared helper, and the same reason for
  // it: the rule is written and tested once in `plannerSetup.js`, and a 422
  // arriving from a plan call names a village in a 26-row table with nothing on
  // screen pointing at it.
  const relayProblems = useMemo(
    () => relayTierProblemsByVillage(relayFor, villages, villageRoles),
    [relayFor, villages, villageRoles]
  )

  // The caret, sent to the cell a refusal named. Imperative and after the stage
  // has rendered, following the same three steps `RoleTemplates` uses for its
  // own jump: open whatever disclosure is hiding the control (React does not
  // track `<details open>`, so it will not write it back), focus without
  // scrolling, then scroll deliberately -- focus()'s own scrolling brings an
  // element barely into view at the bottom edge, and these are cells in a table
  // the operator has to read across.
  useEffect(() => {
    if (cellFocus == null) return
    const target = Array.from(document.querySelectorAll('[aria-label]')).find(
      (el) => el.getAttribute('aria-label') === cellFocus.label
    )
    if (target == null) return
    for (let node = target.parentElement; node != null; node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true
    }
    target.focus({ preventScroll: true })
    target.scrollIntoView({ block: 'center', inline: 'nearest' })
  }, [cellFocus])

  // Ascending, then descending, then the account's own order back -- the
  // ordering is never a one-way door. The cycle is computed by a pure helper
  // and both pieces of state are set from it, rather than deriving one inside
  // the other's updater: React may invoke an updater more than once, which
  // would advance the cycle twice from a single click.
  const toggleSort = useCallback(
    (key) => {
      const next = nextSort({ key: sortKey, direction: sortDir }, key)
      setSortKey(next.key)
      setSortDir(next.direction)
    },
    [sortKey, sortDir]
  )

  // The execute payload for the chosen mode. Whole-day: segments carry each
  // profile's allocations and hours, so the top-level pair is stripped -- the
  // backend rejects them rather than silently ignoring one -- and the prune is
  // forced on because disjoint row minutes are what make the union attributable.
  const buildExecutePayload = useCallback(() => {
    const base = buildPlanPayload()
    if (!wholeDay) return base
    const { segments, skipped, unanswered } = buildSegments()
    if (!segments.length) {
      throw new Error('No profile has hours set — give each profile its window first')
    }
    if (skipped.length) {
      throw new Error(
        `Whole-day execution needs hours on every profile — missing: ${skipped.join(', ')}`
      )
    }
    // Thrown rather than sent, and before anything is written: a whole-day run
    // creates real routes, and a segment with no attendance answer is refused
    // by the backend naming village ids the operator cannot act on from here.
    if (attendanceIsRequired && unanswered.length) {
      throw new Error(
        `Whole-day execution needs to know who is trading in every window — ` +
          `${unanswered.join(', ')} has not said. Answer it under Day & night.`
      )
    }
    // FOUR fields move to the segments, not three. `npc_attended` was the one
    // left behind once; `overnight` is the same shape and would be worse than
    // discarded -- the top-level `dispatch_window` is stripped here, and
    // `_overnight_needs_hours_to_be_overnight` REFUSES a declaration with no
    // window, so leaving it on would 422 the whole run. The segments each
    // carry their own, and the per-segment value is what the backend applies,
    // so a top-level one is a claim about hours this request no longer has.
    // That is precisely the shape `/execute` forbids unknown fields over: a
    // parameter that looks like it says something and is discarded.
    const {
      allocations: _a,
      dispatch_window: _w,
      npc_attended: _n,
      overnight: _o,
      ...rest
    } = base
    return { ...rest, segments, prune_to_window: true }
  }, [buildPlanPayload, buildSegments, wholeDay, attendanceIsRequired])

  // Execute the plan as trade routes. dryRun previews (zero game requests);
  // live requires an explicit confirm and only works once the backend's
  // trade-route payload is verified (execResult.live_enabled).
  const executePlan = useCallback(
    async (dryRun) => {
      // Re-check freshness from the LIVE clock at action time (issue #66). The
      // disabled button is only a hint computed at render; a snapshot can cross
      // the TTL between that render and this click, and a LIVE run must not
      // send Travian mutations computed from outdated stock/merchant/capacity
      // state. A preview is exempt: it mutates nothing, spends no requests, and
      // refusing it withholds the free diagnostic from the operator whose
      // snapshot just went stale — the one person who needs to look.
      if (
        !dryRun &&
        !useStaleSnapshot &&
        (snapshotFetchedAt == null || Date.now() - snapshotFetchedAt > SNAPSHOT_LIVE_TTL_MS)
      ) {
        toast.error(
          'Snapshot is too old to write from — free merchants and stocks decide ' +
            'whether this plan can be staffed, and they move by the minute. Fetch ' +
            'fresh state, or tick “plan from this stale snapshot anyway”.'
        )
        return
      }
      const requestedFor = accountKey
      // Staleness guard (same as buildPlan): if the operator edits an input or
      // switches account while a request is in flight, a stale response must
      // not overwrite the current view. This applies to the zero-effect PREVIEW
      // only — a live run mutates the game, so its result must ALWAYS be shown
      // (dropping it would hide real in-game writes the operator must see).
      const requestedRev = planInputRev.current
      setExecuting(true)
      try {
        const res = await api.post('/distribution/execute', {
          ...buildExecutePayload(),
          dry_run: dryRun,
          disable_existing: disableExisting,
          max_routes_per_run: routeCap(routesPerRun),
          // Targeting a single pair is how a first live run against a real
          // account becomes a controlled test rather than an uncontrolled one
          // with a small blast radius. Omitted entirely when unset, so an
          // ordinary run is byte-identical to what it was before.
          ...(onlyOrigin ? { only_origins: [Number(onlyOrigin)] } : {}),
          ...(onlyDestination ? { only_destinations: [Number(onlyDestination)] } : {}),
          ...(Number(maxGameRows) > 0 ? { max_game_rows_per_run: Number(maxGameRows) } : {}),
          ...(protectDestinations.trim()
            ? { protect_destinations: splitProtected(protectDestinations) }
            : {}),
          update_drifted: updateDrifted,
        })
        if (
          dryRun &&
          (requestedFor !== currentAccountKey() || requestedRev !== planInputRev.current)
        ) {
          return
        }
        setExecResult(res.data)
        if (!dryRun) {
          // A live run mutated the game, so record a human-readable audit entry
          // (Activity Log, source "planner") AND persist it, because the in-page
          // panel is cleared on the next input edit and lost on reload (#69).
          const counts = res.data.actions.reduce((acc, a) => {
            acc[a.status] = (acc[a.status] || 0) + 1
            return acc
          }, {})
          const record = {
            at: new Date().toISOString(),
            account: requestedFor,
            // The handle to this run's own undo. `/routes/revert-plan` needs
            // the trace id to read the pre-write inventory -- the only record
            // of what each village looked like before the run, because the game
            // returns no id when it creates a route. The app received it and
            // threw it away, so a run that wrote 72 game rows had no in-app
            // path back.
            traceId: res.data.trace_id ?? null,
            created: res.data.created,
            remaining: res.data.remaining,
            counts,
            disables: res.data.disables || [],
            // Separate from disables: re-enabling RESTARTS a route, so
            // reporting it as a disable told the operator the opposite.
            reEnables: res.data.re_enables || [],
            problems: res.data.problems || [],
            // Per-route outcomes, so an operator can reconstruct the run later.
            routes: res.data.actions.map((a) => ({
              from: a.origin_name,
              to: a.destination_name,
              at: `${a.dest_x}|${a.dest_y}`,
              status: a.status,
              detail: a.detail || '',
            })),
          }
          // Always PERSIST under the account the run actually targeted, so the
          // audit is never lost. Only adopt it as the *displayed* record if we
          // are still on that account — otherwise account A's routes/counts
          // would render as account B's "last run" (the dry-run staleness guard
          // above is deliberately skipped for live runs, so this needs its own
          // account check). Switching back to A re-hydrates A's record.
          saveJson(`${LS_LAST_RUN}::${requestedFor}`, record)
          if (requestedFor === currentAccountKey()) setLastRun(record)
          const summary =
            `Trade routes executed: ${res.data.created} created, ` +
            Object.entries(counts)
              .filter(([status]) => status !== 'created')
              .map(([status, n]) => `${n} ${status}`)
              .join(', ') +
            `${res.data.disables?.length ? `, ${res.data.disables.length} disable action(s)` : ''}` +
            `${res.data.re_enables?.length ? `, ${res.data.re_enables.length} re-enabled` : ''}` +
            `${res.data.problems?.length ? `, ${res.data.problems.length} problem(s)` : ''}`
          useLogStore
            .getState()
            .addLog(
              res.data.problems?.length ? 'warning' : 'success',
              'planner',
              summary,
              record
            )
          const left = res.data.remaining
            ? `, ${res.data.remaining} deferred to a later run`
            : ''
          // `problems` are real execution failures (failed disable, Gold Club);
          // `warnings` are benign planner notes and must NOT read as failure.
          const problem =
            res.data.problems?.[0] ||
            (res.data.actions.some((a) => a.status === 'failed')
              ? 'a create failed — see the result panel'
              : null)
          if (res.data.created > 0 && problem) {
            // Some routes went through, but a real failure occurred too — don't
            // let the green count hide it.
            toast.error(`Created ${res.data.created} route(s), but ${problem}`)
          } else if (res.data.created > 0) {
            toast.success(`Created ${res.data.created} route(s)${left}`)
          } else if (problem) {
            toast.error(problem)
          } else {
            toast.success(`No new routes needed${left}`)
          }
        }
      } catch (err) {
        toast.error(errorDetail(err, dryRun ? 'Preview failed' : 'Execution failed'))
      } finally {
        setExecuting(false)
      }
    },
    [
      accountKey,
      currentAccountKey,
      buildExecutePayload,
      toast,
      snapshotFetchedAt,
      useStaleSnapshot,
      routesPerRun,
      onlyOrigin,
      onlyDestination,
      maxGameRows,
      protectDestinations,
      disableExisting,
      updateDrifted,
    ],
  )

  // How much of the day the active profile owns. A profile covering most of it
  // is one the operator is awake for, which is exactly when "nothing is spent"
  // stops being true.
  const profileDayShare = useMemo(
    () =>
      windowDayShare(profileWindows[activeProfile] ?? DEFAULT_WINDOWS[activeProfile] ?? null) ?? 1,
    [profileWindows, activeProfile]
  )

  // ── Night profile ───────────────────────────────────────────────────────
  // At night nothing is spent, so everything that arrives stays and the store
  // becomes the binding constraint. The most a village may take per hour is
  //
  //     (target - baseline) x capacity / window hours
  //
  // measured from the baseline the operator RE-ESTABLISHES each night rather
  // than from whatever a snapshot caught -- which is what lets one profile hold
  // for weeks instead of going stale within the hour.
  //
  // Writes into the ACTIVE profile, so the operator picks Night first and sees
  // the numbers land in the table they are already looking at. Costs nothing, so
  // it can be redone freely while they settle on a baseline.
  const buildNightProfile = useCallback(async () => {
    // Same single-window rule as `buildPlan`: `/night-profile` carries the plan
    // request, so it carries the attendance requirement with it.
    if (activeAttendanceOwed) {
      toast.error(
        `${activeProfile} needs an answer for who is trading before its allowance can ` +
          `be sized. Answer it under Day & night.`
      )
      return
    }
    // `Number('') / 100` is 0, so an emptied box used to ask for a profile
    // that empties to 0% or fills to 0% -- and neither is what a blank box
    // says. It says nothing, and nothing is not a threshold. Refused here
    // rather than defaulted, because a derivation is only as good as the two
    // figures it is measured from and guessing one of them is how a profile
    // stops matching the account it was built for.
    if (String(baselineFill).trim() === '' || String(targetFill).trim() === '') {
      toast.error(
        'Type both fills before deriving — a blank box is not 0%, and a store ' +
          'emptied to nothing is a different night from one you did not answer for.'
      )
      return
    }
    setDeriving(true)
    try {
      const res = await api.post('/distribution/night-profile', {
        ...buildPlanPayload(),
        baseline_fill: Number(baselineFill) / 100,
        target_fill: Number(targetFill) / 100,
      })
      const incoming = res.data.allocations || {}
      setAllocations((prev) => {
        const next = { ...prev }
        for (const [resource, per] of Object.entries(incoming)) {
          next[resource] = { ...per }
        }
        return next
      })
      setDerived(res.data)
      // The plan on screen was built from the old numbers, so it no longer
      // describes the inputs.
      setPlan(null)
      const short = Object.keys(res.data.unmet || {}).length
      if (short) {
        toast.error(
          `Built, but ${short} resource(s) have demand no village can cover — see the notes`
        )
      } else {
        toast.success(`Night profile built for a ${baselineFill}% → ${targetFill}% night`)
      }
    } catch (err) {
      toast.error(errorDetail(err, 'Could not build the night profile'))
    } finally {
      setDeriving(false)
    }
  }, [
    buildPlanPayload,
    baselineFill,
    targetFill,
    setAllocations,
    toast,
    activeAttendanceOwed,
    activeProfile,
  ])

  // ── Reconciliation sweep ────────────────────────────────────────────────
  // Switching profiles drops some villages as origins entirely, and those are
  // exactly the ones still holding the other profile's routes — and exactly the
  // ones an ordinary run never visits, because it only reads the origins the
  // CURRENT plan uses. One surviving route breaks the whole plan: the plan is a
  // conservation system, so the receiver overflows AND the sender drains, and
  // the account ends up in neither profile.
  //
  // This sweep visits every village and writes nothing but disables. Doing it
  // with no create budget is what makes it safe to interrupt: afterwards the
  // game holds nothing the plan rejects, and every later capped run adds a safe
  // subset. The account is never in the conflicting state, only an incomplete one.
  //
  // It runs in chunks because it cannot fit in one request — fifty paced reads
  // alone outlast the client timeout before a single write delay or idle browse.
  // The gap between chunks is the session break a long operation needs, and the
  // server picks its length so the client is not returning on a metronome.
  const runReconcileSweep = useCallback(async () => {
    if (!plan) {
      toast.error('Build a plan first — the sweep needs to know what the plan wants')
      return
    }
    sweepCancel.current = false
    setSweeping(true)
    const sweptAll = []
    const problems = []
    let outstanding = null // null = first chunk, visit everything
    let chunk = 0
    let lastCreatesLeft = -1
    try {
      for (;;) {
        chunk += 1
        setSweepProgress({ chunk, swept: sweptAll.length, outstanding, waiting: 0, problems })
        const res = await api.post(
          '/distribution/execute',
          {
            ...buildExecutePayload(),
            dry_run: false,
            disable_existing: true,
            // Whole-day mode provisions as it sweeps -- one read per village
            // serves reconcile AND create, which is the entire point of the
            // single pass. Otherwise the sweep only takes routes away.
            max_routes_per_run: wholeDay ? routeCap(routesPerRun) : 0,
            ...(wholeDay && Number(maxGameRows) > 0
              ? { max_game_rows_per_run: Number(maxGameRows) }
              : {}),
            reconcile_all_origins: true,
            max_origins_per_run: SWEEP_VILLAGES_PER_CHUNK,
            // The sweep honours the exemption too, or it would switch off by
            // hand exactly what the ordinary run is told to leave alone.
            ...(protectDestinations.trim()
              ? { protect_destinations: splitProtected(protectDestinations) }
              : {}),
            // Narrow to the unswept villages while any remain. Once every
            // village is swept but whole-day creates are still deferred (the
            // per-chunk budget caps them), the run goes back UNfiltered so the
            // deferred creates on already-swept villages get their turn --
            // without this, "swept" quietly meant "swept but only partly
            // provisioned" and the loop ended with routes never created.
            ...(outstanding && outstanding.length ? { only_origins: outstanding } : {}),
          },
          // Generous but finite. A chunk of five villages is ~40-70s of paced
          // traffic; three minutes is headroom, not an invitation to hang.
          { timeout: 180000 }
        )
        sweptAll.push(...(res.data.swept_origins || []))
        problems.push(...(res.data.problems || []))
        outstanding = res.data.unswept_origins || []
        const wait = res.data.next_chunk_wait_seconds
        const createsLeft = wholeDay ? Number(res.data.remaining) || 0 : 0
        // Stall guard: a blocked account (Gold Club refused, repeated failures)
        // can leave `remaining` frozen -- looping on it would hammer the game
        // with identical chunks forever.
        if (createsLeft && createsLeft === lastCreatesLeft && !outstanding.length) {
          problems.push(
            `${createsLeft} route(s) stayed uncreated across two passes — ` +
              `stopping rather than repeating identical requests; see the problems above`
          )
          break
        }
        lastCreatesLeft = createsLeft
        if ((!outstanding.length && !createsLeft) || !wait) break
        if (sweepCancel.current) break
        // Counted down visibly: a progress bar that sits still for four minutes
        // reads as a hang, and the operator would reload and lose the loop.
        for (let left = Math.ceil(wait); left > 0; left -= 1) {
          if (sweepCancel.current) break
          setSweepProgress({
            chunk,
            swept: sweptAll.length,
            outstanding,
            waiting: left,
            problems,
          })
          await new Promise((r) => setTimeout(r, 1000))
        }
        if (sweepCancel.current) break
      }
      const done = !outstanding || outstanding.length === 0
      setSweepProgress({
        chunk,
        swept: sweptAll.length,
        outstanding: outstanding || [],
        waiting: 0,
        problems,
        done,
      })
      useLogStore
        .getState()
        .addLog(problems.length ? 'warning' : 'success', 'planner', done
          ? `Reconciliation sweep complete: ${sweptAll.length} village(s) swept`
          : `Reconciliation sweep stopped with ${outstanding.length} village(s) outstanding`,
        { swept: sweptAll, outstanding, problems })
      if (!done) {
        // Never let a partial sweep read as a finished one — that is the exact
        // false confidence this whole path exists to remove.
        toast.error(
          `Sweep incomplete: ${outstanding.length} village(s) not reached. ` +
            `Run it again — until it finishes, old routes may still be shipping.`
        )
      } else if (problems.length) {
        toast.error(`Swept ${sweptAll.length} village(s), but ${problems[0]}`)
      } else {
        toast.success(`Swept ${sweptAll.length} village(s) — nothing stale left`)
      }
    } catch (err) {
      toast.error(errorDetail(err, 'Reconciliation sweep failed'))
      setSweepProgress((p) => ({ ...(p || {}), failed: true, outstanding: outstanding || [] }))
    } finally {
      setSweeping(false)
    }
  }, [plan, buildExecutePayload, wholeDay, routesPerRun, maxGameRows, protectDestinations, toast])

  // Live unallocated counter, so slack is visible while typing rather than
  // discovered later (profile known issue #9).
  const totals = useMemo(() => {
    const out = {}
    for (const resource of RESOURCES) {
      const field = `${resource}_per_hour`
      let total = 0
      let known = true
      for (const v of villages) {
        if (v[field] == null) {
          known = false
          continue
        }
        total += v[field]
      }
      out[resource] = { total, known }
    }
    return out
  }, [villages])

  const remainderFor = (resource) => {
    const per = allocations[resource] ?? {}
    const found = Object.entries(per).find(([, a]) => a.mode === 'remainder')
    return found ? Number(found[0]) : null
  }

  // One cell of the Allocate grid. The merge lives in `withEditedAllocation`,
  // where it can be tested: seeded from a `keep` literal here, a value-only
  // patch on a TEMPLATED village (which has no own entry) flipped the mode to
  // Keep own and sent it, so the village retained its own 1,500/h while
  // spending its role's 8,372.
  const setAllocation = (resource, villageId, patch) => {
    setAllocations((prev) => ({
      ...prev,
      [resource]: withEditedAllocation({
        perVillage: prev[resource] ?? {},
        villageId,
        template: roleTemplates[villageRoles[villageId]],
        resource,
        patch,
      }),
    }))
  }

  // One resource of one village's spend. An emptied box is DELETED rather than
  // stored as 0: zero says "measured, and it spends none", which is a claim,
  // and a village left with no claims at all drops out of the map entirely so
  // the request and the file both say "nothing declared" rather than "{}".
  const setSpend = (villageId, resource, raw) => {
    setConsumption((prev) => {
      const per = { ...(prev[villageId] ?? {}) }
      if (raw === '') delete per[resource]
      else per[resource] = Number(raw)
      const next = { ...prev }
      if (Object.keys(per).length) next[villageId] = per
      else delete next[villageId]
      return next
    })
  }


  // ── Role templates: one profile per role, edited in one place ────────
  // Every setter writes through the role KEY, so the key exists the moment the
  // operator gives a role any figure at all -- which is what the village row's
  // "no template yet" warning and the payload's `roles` map both read. A
  // half-typed template is still a template; an absent one is a village with a
  // role the backend will refuse, and that refusal is the point.
  const setTemplateAllocation = (role, resource, patch) => {
    setRoleTemplates((prev) => {
      const template = prev[role] ?? {}
      const allocations = { ...(template.allocations ?? {}) }
      const next = { ...(allocations[resource] ?? { mode: 'keep', value: 0 }), ...patch }
      // Keep is the absence of a target, not a target of its own: a resource
      // the template says "keep" about has to fall through to whatever the
      // village itself says, which is exactly what an absent entry does.
      if (next.mode === 'keep') delete allocations[resource]
      else allocations[resource] = next
      return { ...prev, [role]: { ...template, allocations } }
    })
  }

  const setTemplateSpend = (role, resource, raw) => {
    setRoleTemplates((prev) => {
      const template = prev[role] ?? {}
      const spend = { ...(template.consumption ?? {}) }
      if (raw === '') delete spend[resource]
      else spend[resource] = Number(raw)
      return { ...prev, [role]: { ...template, consumption: spend } }
    })
  }

  const patchTemplate = (role, patch) => {
    setRoleTemplates((prev) => ({ ...prev, [role]: { ...(prev[role] ?? {}), ...patch } }))
  }

  const clearTemplate = (role) => {
    setRoleTemplates((prev) => {
      const next = { ...prev }
      delete next[role]
      return next
    })
  }

  // From the warning to the figures, in one press.
  //
  // The Snapshot row says "no DEF template yet" because the backend refuses a
  // claimed role whose template never arrived -- and for four rounds that was
  // all it said. The remedy is on ANOTHER stage, behind a collapsed
  // disclosure, in the widest table in the app, so acting on it meant knowing
  // the panel existed, finding it, opening it and picking one row out of five.
  //
  // Both state changes in one handler and therefore in one commit: the stage
  // switch MOUNTS the panel, which is what lets it render already open and be
  // measured open by `ScrollableTable`'s layout effect in the same frame. A
  // panel that opened afterwards would focus a skipped subtree, which is a
  // no-op -- measured in e2e/roleTemplates.pw.js, not assumed.
  const jumpToRoleTemplate = (role) => {
    setStage('allocate')
    setTemplateFocus((prev) => ({ role, seq: (prev?.seq ?? 0) + 1 }))
  }

  // How many villages each role is standing in for. The whole claim of a
  // template is "one profile, four villages", so the count is the number that
  // says whether it is doing that -- and a template nothing claims is dead
  // weight the operator should be able to see.
  const villagesInRole = (role) =>
    villages.filter((v) => villageRoles[v.village_id] === role).length

  // Handed to the panel as data rather than as a callback, so the panel stays a
  // pure function of its props and a test can render it with no page around it.
  const roleCounts = Object.fromEntries(VILLAGE_ROLES.map((role) => [role, villagesInRole(role)]))

  // Roles some village claims and nobody has given a profile to. The backend
  // refuses exactly these, so naming them here is the difference between a
  // 422 the operator can act on and one they have to decode.
  //
  // `isEmptyTemplate` rather than `== null`, and the SAME predicate
  // `rolesForRequest` drops a template on: the role key outlives the last
  // figure in it, so a template emptied box by box was still a template to a
  // null check -- the request carried `{"def": {"consumption": {}}}`, the
  // backend accepted it, and this warning stayed silent about the four villages
  // then planning at their own production.
  const rolesMissingTemplates = VILLAGE_ROLES.filter(
    (role) => villagesInRole(role) > 0 && isEmptyTemplate(roleTemplates[role])
  )

  // The allocation the plan will ACTUALLY use for one cell: the village's own
  // where it has one, otherwise its role's template, otherwise keep. The same
  // merge the backend does, per resource, and the page has to do it too --
  // without this the grid shows a defensive village as "Keep own" while the
  // plan ships it 8,372/h, and the unassigned meter counts its own 1,500
  // instead of the profile's figure, so the Rest village's displayed target is
  // wrong by the difference. One resolver, so the numbers on screen and the
  // numbers in the plan cannot come from two different rules.
  const effectiveAllocation = (resource, villageId) =>
    resolveRoleAllocation(
      roleTemplates[villageRoles[villageId]],
      resource,
      allocations[resource]?.[villageId]
    )

  // And the spend, on the same rule: a village's own figure per resource, else
  // its role's. Only for the LIVE preview before a plan exists -- once there is
  // one, the grid reads the spend off the plan's own `village_nets`.
  const effectiveSpend = (resource, villageId) =>
    resolveRoleSpend(
      roleTemplates[villageRoles[villageId]],
      resource,
      consumption[villageId]?.[resource]
    )

  // What a village ends up retaining per hour after the plan runs -- the same
  // resolution the backend applies, so the grid never disagrees with the plan.
  const targetFor = (resource, v) => {
    const own = v[`${resource}_per_hour`]
    const a = effectiveAllocation(resource, v.village_id)
    // Unreadable rate: the backend drops this village's allocation entirely
    // (with a warning) rather than plan blind, so showing any target here --
    // even an absolute one -- would promise something the plan will not do.
    if (own == null && a.mode !== 'remainder') return null
    if (a.mode === 'remainder') return explicitTotal(resource)
    if (a.mode === 'absolute') return Number(a.value) || 0
    if (a.mode === 'percentage') return (totals[resource].total * (Number(a.value) || 0)) / 100
    if (a.mode === 'sustain') return own < 0 ? (-own * (Number(a.value) || 0)) / 100 : own
    return own // keep
  }

  const isSelected = (resource, vid) => (selected[resource] ?? []).includes(vid)
  const someSelected = (resource) => (selected[resource] ?? []).length > 0
  const allSelected = (resource) =>
    villages.length > 0 && (selected[resource] ?? []).length === villages.length

  const toggleSelected = (resource, vid) => {
    setSelected((prev) => {
      const cur = new Set(prev[resource] ?? [])
      if (cur.has(vid)) cur.delete(vid)
      else cur.add(vid)
      return { ...prev, [resource]: [...cur] }
    })
  }

  const toggleSelectAll = (resource) => {
    setSelected((prev) => ({
      ...prev,
      [resource]: allSelected(resource) ? [] : villages.map((v) => v.village_id),
    }))
  }

  // Apply one mode/value to the CHECKED villages for a resource. The remainder
  // village keeps its role (set via the Rest radio, not a mode), so a batch
  // edit never silently clears the slack destination.
  const applyToSelected = (resource, mode, value) => {
    const ids = new Set(selected[resource] ?? [])
    if (!ids.size) {
      toast.error('Check the villages to set first')
      return
    }
    setAllocations((prev) => {
      const per = { ...(prev[resource] ?? {}) }
      for (const v of villages) {
        if (!ids.has(v.village_id)) continue
        if (per[v.village_id]?.mode === 'remainder') continue
        per[v.village_id] = { mode, value: Number(value) || 0 }
      }
      return { ...prev, [resource]: per }
    })
  }

  // Exactly one remainder per resource, or NONE -- `villageId == null` is the
  // initial state of every account and the one this widget could not get back
  // to. The merge lives in `withRemainder`, where it can be tested, and where
  // the displaced village's entry is deleted rather than overwritten with a
  // `keep` nobody chose.
  const setRemainder = (resource, villageId) => {
    setAllocations((prev) => ({
      ...prev,
      [resource]: withRemainder({ perVillage: prev[resource] ?? {}, villageId }),
    }))
  }

  const explicitTotal = (resource) => {
    const { total } = totals[resource]
    let assigned = 0
    for (const v of villages) {
      // A village with no entry of its own takes its ROLE's figure, and only
      // then keeps its own production — the same order the backend resolves in,
      // so an untouched account shows 0 unassigned rather than the whole
      // account total, and a templated one shows what its profiles claim.
      const a = effectiveAllocation(resource, v.village_id)
      if (a.mode === 'remainder') continue
      const own = v[`${resource}_per_hour`]
      // A village whose rate could not be read has its allocation DROPPED by
      // the backend (with a warning), so counting it here skews the unassigned
      // counter and, through it, the remainder village's displayed target.
      if (own == null) continue
      if (a.mode === 'absolute') assigned += Number(a.value) || 0
      else if (a.mode === 'percentage') assigned += (total * (Number(a.value) || 0)) / 100
      else if (a.mode === 'sustain') assigned += own < 0 ? (-own * (Number(a.value) || 0)) / 100 : own
      else assigned += own
    }
    // A route-eligible foreign target is a RECEIVER the backend plans for -- it
    // has no production and no stores, and its demand comes out of the same pool.
    // Leaving it out here overstated the remainder village's share by the whole
    // obligation: 47,167 crop/h on this account, which made a remainder of -813
    // read as +46,354 and a consuming village look like it was about to be buried.
    // The grid must never disagree with the plan about this.
    if (resource === 'crop') {
      for (const t of foreignTargets) {
        if (t.route_eligible) assigned += Number(t.crop_per_hour) || 0
      }
    }
    return total - assigned
  }

  // ── Profile management ─────────────────────────────────────────────
  // A built plan belongs to the profile it was built from, so switching or
  // editing the set clears it.
  const profileNames = Object.keys(profiles)

  const windowFor = (name) => profileWindows[name] ?? DEFAULT_WINDOWS[name] ?? null

  const runDayCheck = async () => {
    const requestedFor = accountKey
    const requestedRev = dayCheckInputRev.current
    const { segments, skipped, unanswered } = buildSegments()
    if (!segments.length) {
      toast.error('No profile has hours set — give each profile its window first')
      return
    }
    // Every segment, not just the selected profile: the backend requires an
    // answer on each one as soon as a floor is declared, and a day simulated
    // with the night marked awake is a different day.
    if (attendanceIsRequired && unanswered.length) {
      toast.error(
        `The full day needs to know who is trading in every window — ` +
          `${unanswered.join(', ')} has not said. Answer it in the table above.`
      )
      return
    }
    setDayChecking(true)
    try {
      // `!= null`, not `> 0`: a ceiling of 0 is the answer "tell me when this
      // store is empty", and the truthiness gate that used to be here read it
      // as "no ceiling at all" -- so the one village whose alert level is the
      // most urgent one there is was the one village never checked. Blank is
      // still absent, because blank is unknown and unknown is not zero.
      const ceilings = {}
      for (const [vid, value] of Object.entries(cropCeilings)) {
        if (value != null) ceilings[vid] = Number(value)
      }
      // The day check routes every profile through the SAME optimizer as
      // /plan, so it needs the same inputs: Trade Office levels, the merchant
      // model and the geometry. Reusing the plan payload is what keeps the two
      // from answering the same account differently. Allocations are dropped
      // because a profile carries its own -- the backend rejects them here.
      // Both allocations and dispatch_window are dropped: a profile carries
      // its own of each, so the backend rejects them at the top level rather
      // than silently applying one profile's hours to all of them.
      const {
        allocations: _perProfileAllocations,
        dispatch_window: _perProfileWindow,
        // And the attendance, for the same reason as the two above it: the
        // hours live on the segments, so the question about those hours does
        // too. The backend refuses a top-level window here outright and
        // ignores a top-level attendance, which is worse -- a field that
        // reads as an answer and is thrown away.
        npc_attended: _perProfileAttendance,
        // And section 6's declaration, which is the fourth. Not merely
        // ignored if left on: with the top-level window stripped, a top-level
        // `overnight` is exactly what
        // `_overnight_needs_hours_to_be_overnight` raises on, so the whole
        // day check would come back 422 over a field the segments already
        // carry.
        overnight: _perProfileOvernight,
        ...planInputs
      } = buildPlanPayload()
      const res = await api.post('/distribution/day-check', {
        ...planInputs,
        segments,
        crop_ceilings: ceilings,
      })
      // Drop the response if the account switched OR any day-check input changed
      // while it was in flight — otherwise a pre-edit "all clear" resurfaces.
      if (requestedFor !== currentAccountKey()) return
      if (requestedRev !== dayCheckInputRev.current) return
      setDayCheck({ ...res.data, skipped })
    } catch (err) {
      toast.error(errorDetail(err, 'Full-day check failed'))
    } finally {
      setDayChecking(false)
    }
  }

  const switchProfile = (name) => {
    if (!profiles[name] || name === activeProfile) return
    setActiveProfile(name)
    setPlan(null)
  }

  /** The three profile actions that need a name typed, asked in the app.
   *
   * `window.prompt` was suppressed by the same Chrome setting that suppresses
   * `confirm` -- and a suppressed prompt returns null, so "+ New" silently did
   * nothing at all. One descriptor drives one dialog, because these are one
   * interaction asked three times.
   */
  const askForProfileName = (mode) =>
    setProfileNaming({
      mode,
      value:
        mode === 'add' ? 'Night' : mode === 'duplicate' ? `${activeProfile} copy` : activeProfile,
    })

  const addProfile = (name) => {
    if (!name) return
    if (profiles[name]) {
      toast.error(`Profile "${name}" already exists`)
      return
    }
    setProfiles((prev) => ({ ...prev, [name]: {} }))
    setActiveProfile(name)
    setPlan(null)
  }

  const duplicateProfile = (name) => {
    if (!name) return
    if (profiles[name]) {
      toast.error(`Profile "${name}" already exists`)
      return
    }
    setProfiles((prev) => ({
      ...prev,
      [name]: JSON.parse(JSON.stringify(prev[activeProfile] ?? {})),
    }))
    setActiveProfile(name)
    setPlan(null)
  }

  const renameProfile = (name) => {
    if (!name || name === activeProfile) return
    if (profiles[name]) {
      toast.error(`Profile "${name}" already exists`)
      return
    }
    setProfiles((prev) => {
      const next = {}
      for (const [k, v] of Object.entries(prev)) next[k === activeProfile ? name : k] = v
      return next
    })
    // The hours belong to the profile, not to its old name. Left keyed by the
    // old name they orphan in localStorage -- and worse, resurrect: a FUTURE
    // profile that happens to reuse the name silently inherits hours the user
    // never set for it.
    setProfileWindows((prev) => {
      if (!(activeProfile in prev)) return prev
      const next = { ...prev }
      next[name] = next[activeProfile]
      delete next[activeProfile]
      return next
    })
    // The attendance answer belongs to the profile, exactly as its hours do,
    // and orphaning it has the sharper consequence: a future profile reusing
    // the name would inherit an "awake" nobody declared for it, which is the
    // one value the field exists to stop being guessed.
    setProfileAttendance((prev) => {
      if (!(activeProfile in prev)) return prev
      const next = { ...prev }
      next[name] = next[activeProfile]
      delete next[activeProfile]
      return next
    })
    // And the overnight declaration, on the same rule: it belongs to the
    // profile rather than to its old name, and an orphaned `true` inherited by
    // a future profile that reuses the name would put section 6's closing
    // deadline on hours nobody declared as the night.
    setProfileOvernight((prev) => {
      if (!(activeProfile in prev)) return prev
      const next = { ...prev }
      next[name] = next[activeProfile]
      delete next[activeProfile]
      return next
    })
    setActiveProfile(name)
  }

  /** Route the typed name to whichever of the three asked for it, then close. */
  const commitProfileName = () => {
    const asked = profileNaming
    if (!asked) return
    setProfileNaming(null)
    const name = String(asked.value ?? '').trim()
    if (asked.mode === 'add') addProfile(name)
    else if (asked.mode === 'duplicate') duplicateProfile(name)
    else renameProfile(name)
  }

  const deleteProfile = () => {
    const fallback = profileNames.find((n) => n !== activeProfile)
    setProfiles((prev) => {
      const next = { ...prev }
      delete next[activeProfile]
      return next
    })
    setProfileWindows((prev) => {
      if (!(activeProfile in prev)) return prev
      const next = { ...prev }
      delete next[activeProfile]
      return next
    })
    setProfileAttendance((prev) => {
      if (!(activeProfile in prev)) return prev
      const next = { ...prev }
      delete next[activeProfile]
      return next
    })
    setProfileOvernight((prev) => {
      if (!(activeProfile in prev)) return prev
      const next = { ...prev }
      delete next[activeProfile]
      return next
    })
    setActiveProfile(fallback)
    setPlan(null)
  }

  // One sheet row with its names resolved. Server-resolved names win: the
  // snapshot cannot know foreign tributes, whose negative ids rendered as "-1"
  // before the plan carried names itself. Used by the table AND the clipboard,
  // so a copied row can never name a different village than the one on screen.
  const sheetRow = (row) => ({
    from:
      row.origin_name ||
      (villages.find((v) => v.village_id === row.origin)?.name ?? String(row.origin)),
    to:
      row.destination_name ||
      (villages.find((v) => v.village_id === row.destination)?.name ?? String(row.destination)),
    cargo: row.cargo,
    cycleHours: row.cycle_hours,
    dispatch: row.dispatch,
    arrival: row.arrival,
    merchants: row.merchants,
  })

  // Live creation is opt-in and off by default, so this sheet is the working
  // output path: copying beats retyping four cargo numbers per row out of a
  // seven-column table. Success is reported only when the clipboard actually
  // took it (see copyToClipboard) — a false "copied" is worse than none.
  const copySheetText = async (text, what) => {
    if (await copyToClipboard(text)) toast.success(`${what} copied — tab-separated columns`)
    else toast.error('Could not reach the clipboard — select the text and copy it manually')
  }

  // One stage per question, in the order a plan is assembled. See the
  // information-architecture note at the top of this file for why these five
  // and not the three they replaced.
  const stages = [
    { id: 'snapshot', label: 'Account' },
    { id: 'allocate', label: 'Targets' },
    { id: 'day', label: 'Day & night' },
    { id: 'plan', label: 'Plan' },
  ]

  // A snapshot carries fast-changing production/stock/merchant state; once it is
  // older than the TTL (or its receipt time is unknown, e.g. an older cache),
  // treat it as stale so it is not silently planned from as if it were live.
  const snapshotAgeMs = snapshot
    ? snapshotFetchedAt
      ? nowMs - snapshotFetchedAt
      : null
    : null
  const snapshotStale =
    !!snapshot && (snapshotFetchedAt == null || snapshotAgeMs > SNAPSHOT_PLAN_TTL_MS)
  // Stricter, and only consulted before a WRITE: free merchants and stocks
  // decide whether the plan can be staffed, and those move minute to minute.
  const snapshotStaleForLive =
    !!snapshot && (snapshotFetchedAt == null || snapshotAgeMs > SNAPSHOT_LIVE_TTL_MS)
  const snapshotAgeLabel =
    snapshotAgeMs == null
      ? 'age unknown'
      : snapshotAgeMs < 60_000
        ? 'just now'
        : snapshotAgeMs < 3_600_000
          ? `${Math.round(snapshotAgeMs / 60_000)}m old`
          : `${Math.round(snapshotAgeMs / 3_600_000)}h old`

  // The report records one load, but "still has no Trade Office level" is a
  // LIVE fact: the operator's next keystroke can answer it. Re-checked against
  // the current levels, so the line shrinks as they type and disappears when
  // the last one is filled in, instead of naming villages that are now known.
  const setupStillUnknown = setupReport
    ? setupReport.stillUnknown.filter((v) => tradeOffice[v.village_id] == null)
    : []

  const planState = planStatus(plan)
  const verdict = planState?.verdict ?? null
  const relays = plan?.relays ?? []

  // The backend's own figures per resource per village, so the grid reads what
  // the plan used instead of recomputing any of it here. Two implementations of
  // one formula drift, and these two could: the planner drops a declared spend
  // whose rate it cannot read, and a stock floor makes a KEEP village's target
  // exceed its production, neither of which this page can derive. `plan` is set
  // to null by any input change (see the planInputRev effect), so a plan on
  // screen was computed from exactly these inputs -- there is no stale-figure
  // case to guard against.
  const planNet = useMemo(() => villageNetIndex(plan), [plan])
  // What the plan actually converted at each village, summed off the server's
  // own per-resource draws. Never the allowance: that is a ceiling on what the
  // village COULD convert, and a floor on a village that needs nothing draws
  // none of it.
  const planNpcDraw = useMemo(() => npcDrawByVillage(plan), [plan])
  const relayLegs = relayLegIndex(relays)

  // What going live will actually do, derived from the PREVIEW the operator is
  // looking at, so the confirmation states real numbers rather than a vague
  // "create these routes" (issue #67).
  const previewCreates = execResult
    ? execResult.actions.filter((a) => a.status === 'would_create')
    : []
  const plannedCreateCount = previewCreates.length
  // Disables are NOT limited to the origins shown as would_create. The live run
  // walks the plan's origins in randomized order and clears stale routes on each
  // origin it VISITS, up to the per-run cap — which can include origins whose own
  // routes the preview shows as `deferred`. So the honest figure is an UPPER
  // BOUND: distinct origins across every planned row, capped by the visit budget.
  const plannedOriginCount = execResult
    ? Math.min(
        MAX_ROUTES_PER_RUN,
        new Set(
          execResult.actions
            .filter((a) => a.status === 'would_create' || a.status === 'deferred')
            .map((a) => a.origin)
        ).size
      )
    : 0
  // Every other action on this page states its cost; the only irreversible one
  // did not. A live run reads each origin's marketplace, then disables, creates
  // and possibly re-enables on it -- so it is the visited origins plus the
  // creates, and it is an upper bound for the same reason plannedOriginCount is.
  const liveRequestEstimate = plannedOriginCount + plannedCreateCount

  // Which "Never disable" entries will protect nothing. The server validates
  // the shape and cannot validate the miss -- it does not hold this account's
  // village list -- so this is the only place the typo is knowable before the
  // run switches the route off.
  // Villages the plan commits more merchants at than they are allowed. Read off
  // `plan.budgets`, which is what the panel below renders, so the count in its
  // summary cannot disagree with the bars inside it.
  const overBudgetCount = plan?.budgets?.filter((b) => b.over_budget).length ?? 0

  const protectionMisses = useMemo(
    () => unresolvedProtectedEntries(protectDestinations, villages),
    [protectDestinations, villages]
  )

  /** The manifest, as a list rather than as a `\n`-joined string.
   *
   * Every word of it is the wording the native dialog carried, including the
   * honest last sentence -- that was the good half. What changes is that the
   * COUNTS are emphasised, because they are what decides the answer and a
   * native dialog rendered them as one more run of 8pt prose; that the
   * irreversible line takes `--danger`; and that it can be read twice.
   */
  const liveManifest = (
    <>
      <p className="text-primary">Execute this plan against Travian now?</p>
      <ul className="mt-3 space-y-1.5">
        {/* Only claimed when it is actually going to happen: the checkbox can
            make this a create-only run, and a dialog listing a disable that
            will not occur is worse than no dialog. */}
        <li>
          {disableExisting ? (
            <>
              Disable existing routes this plan no longer wants, on up to{' '}
              <strong className="text-primary text-xl font-mono">{plannedOriginCount}</strong>{' '}
              origin village{plannedOriginCount === 1 ? '' : 's'}
            </>
          ) : (
            <>
              <strong className="text-primary">Create ONLY</strong> — no existing route will be
              disabled
            </>
          )}
        </li>
        <li>
          Create up to{' '}
          <strong className="text-primary text-xl font-mono">{plannedCreateCount}</strong> new
          route{plannedCreateCount === 1 ? '' : 's'}
        </li>
        {execResult?.remaining ? (
          <li>
            Defer{' '}
            <strong className="text-primary text-xl font-mono">{execResult.remaining}</strong>{' '}
            route(s) to a later run
          </li>
        ) : null}
      </ul>
      <p className="mt-3">
        Already-active routes that the plan still wants are left untouched, and a route the plan
        wants that is currently DISABLED is switched back on.
      </p>
      <p className="mt-3 text-danger">
        This sends live requests to Travian. If a create fails after a disable, old routes can
        remain disabled without their replacements — re-run to reconcile.
      </p>
    </>
  )

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex justify-between items-center mb-4 gap-3 flex-wrap">
        <h2 className="heading-gold text-2xl">Resource Planner</h2>
        {/* `flex-wrap` on the INNER group too, which it did not have: the outer
            row wrapped the heading away from the controls and then the controls
            themselves overflowed. Measured at 375 the moment the verdict chip
            joined them -- the document went 76px past the viewport, and
            `liveRunGuards` read 82px of page scroll. */}
        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-secondary text-xs">
            {villages.length ? `${villages.length} villages` : 'no snapshot yet'}
          </span>
          {snapshot && (
            <span className={`text-xs ${snapshotStale ? 'text-warning' : 'text-secondary'}`}>
              {/* "stale" in words, so freshness is not signalled by colour alone. */}
              · {snapshotAgeLabel}
              {snapshotStale && ' · stale'}
            </span>
          )}
          {/* Every fetch is priced in the label — requests are the scarce
              resource, so the cost is stated before it is spent. */}
          <button className="btn-primary btn-sm" onClick={fetchSnapshot} disabled={fetching}>
            {fetching ? 'Reading…' : 'Fetch state (3–4 requests)'}
          </button>
          <button
            className="btn-secondary btn-sm"
            onClick={buildPlan}
            disabled={planning || (snapshotStale && !useStaleSnapshot)}
          >
            {planning ? 'Planning…' : 'Build plan (0 requests)'}
          </button>
          {/* The answer, where the question was asked. Pressing Build plan no
              longer moves the stage (see `buildPlan`), so this is what says the
              press landed -- and it is a button, because the operator who wants
              the detail should not have to find the tab. In words as well as in
              tone: `plan-verdict-chip` colours it, and "Cannot run" is what
              carries the meaning. */}
          {planState && (
            <button
              type="button"
              className={`plan-verdict-chip ${
                VERDICT_TONE_CLASS[planState.tone] ?? 'plan-verdict-dirty'
              }`}
              onClick={() => setStage('plan')}
            >
              {verdictSummary(planState)}
            </button>
          )}
        </div>
      </div>

      {snapshotStale && (
        <div className="card p-3 mb-4 border border-warning">
          <p className="text-warning text-xs">
            This snapshot is {snapshotAgeLabel}
            {snapshotFetchedAt == null && ' (restored from a previous session)'} — production,
            stocks and free merchants may have changed. Fetch fresh state before building, or
            acknowledge to plan from it anyway.
          </p>
          <div className="flex items-center gap-3 mt-1.5 flex-wrap">
            {/* The honest fix, in reach. Before this the only action inside the
                banner was the acknowledgement, while the button that actually
                resolves it sat at the top of the page. */}
            <button className="btn-secondary btn-xs" disabled={fetching} onClick={fetchSnapshot}>
              {fetching ? 'Fetching…' : 'Fetch fresh state (3–4 requests)'}
            </button>
            <label className="text-secondary text-xs flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={useStaleSnapshot}
                onChange={(e) => setUseStaleSnapshot(e.target.checked)}
              />
              Plan from this stale snapshot anyway
            </label>
          </div>
        </div>
      )}

      {/* Named allocation profiles (e.g. Day / Night). Only the allocations
          differ per profile; the snapshot and merchant model are shared, so
          switching re-plans from the same data with no extra request. */}
      <div className="flex items-center gap-2 mb-3 text-xs flex-wrap">
        <span className="text-secondary uppercase">Plan profile</span>
        <select
          aria-label="Allocation profile"
          className="input-field w-auto text-xs py-1"
          value={activeProfile}
          onChange={(e) => switchProfile(e.target.value)}
        >
          {profileNames.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
        <button className="btn-secondary btn-xs" onClick={() => askForProfileName('add')}>
          + New
        </button>
        <button className="btn-secondary btn-xs" onClick={() => askForProfileName('duplicate')}>
          Duplicate
        </button>
        <button className="btn-secondary btn-xs" onClick={() => askForProfileName('rename')}>
          Rename
        </button>
        <button
          className="btn-secondary btn-xs"
          onClick={() => setConfirmDeleteProfile(activeProfile)}
          disabled={profileNames.length <= 1}
        >
          Delete
        </button>
        {/* The hours this profile actually runs. Profiles are separate plans,
            but the account lives through all of them every day; without hours
            the full-day check cannot line them up.

            `w-auto` rather than `w-[74px]`, because the UA draws this control
            and knows what it needs: cloned at `width: auto` it asks for
            106.8px for the widget it is going to render, and 74px was 32.8px
            short of that -- enough for "07:00" (39.8px of glyphs in 42px of
            room) and not for the picker button beside it. Nothing in the DOM
            said so, which is why `scrollWidth` read 74 against a `clientWidth`
            of 74 and the width sweep called it fine. */}
        <span className="flex items-center gap-1 text-xs text-secondary ml-1">
          runs
          <input
            type="time"
            aria-label={`${activeProfile} window start`}
            className="input-field text-xs py-0.5 px-1 w-auto"
            value={(windowFor(activeProfile) ?? ['', ''])[0]}
            onChange={(e) =>
              setProfileWindows((prev) => ({
                ...prev,
                [activeProfile]: [e.target.value, (windowFor(activeProfile) ?? ['', ''])[1]],
              }))
            }
          />
          –
          <input
            type="time"
            aria-label={`${activeProfile} window end`}
            className="input-field text-xs py-0.5 px-1 w-auto"
            value={(windowFor(activeProfile) ?? ['', ''])[1]}
            onChange={(e) =>
              setProfileWindows((prev) => ({
                ...prev,
                [activeProfile]: [(windowFor(activeProfile) ?? ['', ''])[0], e.target.value],
              }))
            }
          />
        </span>
        {/* One editor, one indicator. The answer belongs to a window and is
            edited on the Day & night stage beside the hours it describes; here
            it is only stated -- in words, because a badge whose meaning is its
            colour tells the operator nothing about which half of the day they
            are looking at. Shown only where it is required, so an account with
            no NPC floor is not asked a question it does not have. */}
        {attendanceIsRequired && (
          <button
            type="button"
            className={`text-xs px-2 py-0.5 rounded border pointer-coarse:min-h-11 ${
              activeAttendanceOwed
                ? 'border-warning text-warning'
                : 'border-default text-secondary hover:text-primary'
            }`}
            onClick={() => setStage('day')}
          >
            NPC: {describeAttendance(attendanceFor(profileAttendance, activeProfile))}
            {activeAttendanceOwed ? ' — answer it' : ''}
          </button>
        )}
        <span className="text-secondary">
          each profile builds its own routes — switching is free (no request)
        </span>
      </div>

      <nav className="flex gap-1 mb-4 border-b border-gray-700" aria-label="Planner stages">
        {stages.map((s) => (
          <button
            key={s.id}
            onClick={() => setStage(s.id)}
            aria-current={stage === s.id ? 'page' : undefined}
            className={`px-4 py-2 text-sm rounded-t ${
              stage === s.id
                ? 'bg-card text-primary border-b-2 border-violet-400'
                : 'text-secondary hover:text-primary'
            }`}
          >
            {s.label}
          </button>
        ))}
      </nav>

      {!villages.length && (
        <div className="card p-6 text-center text-secondary">
          <p>No account state yet.</p>
          <p className="text-xs mt-1">
            Fetching reads production, stocks and granary countdowns for every village in three
            to four requests — net crop included, so army villages are not mistaken for healthy
            ones.
          </p>
        </div>
      )}

      {stage === 'snapshot' && villages.length > 0 && (
        <div className="card p-4 relative overflow-x-auto">
          {/* Trade Office and Crop alert below are typed by hand and stored per
              origin, so they do not follow you between :80, :8001, the LAN
              address or Tailscale. Save them once and reload them instead. */}
          {/* The account in one line. Every number here is a fact the operator
              otherwise derives by scanning the rows, and each problem count is
              a button that isolates exactly those rows. */}
          <div className="flex items-center gap-2 flex-wrap mb-3 pb-3 border-b border-gray-800">
            <span className="text-secondary text-xs uppercase">Account</span>
            <span className="text-xs font-mono">
              {health.total} villages
            </span>
            <span className="text-secondary text-xs">·</span>
            <span
              className={`text-xs font-mono ${
                health.netCropPerHour < 0 ? 'text-danger' : 'text-success'
              }`}
              title={
                health.netIsComplete
                  ? 'Sum of every village crop rate.'
                  : 'Sum of the readable rates only — villages whose rate could not be derived are left out rather than counted as zero.'
              }
            >
              net {signed(health.netCropPerHour)}/h{health.netIsComplete ? '' : ' (partial)'}
            </span>

            {health.starving.length > 0 && (
              <button
                className={`text-xs px-2 py-0.5 rounded border ${
                  rowFilter === 'starving'
                    ? 'border-danger text-danger bg-danger/10'
                    : 'border-gray-700 text-danger hover:bg-danger/10'
                }`}
                aria-pressed={rowFilter === 'starving'}
                onClick={() => setRowFilter((f) => (f === 'starving' ? null : 'starving'))}
                title="Show only the villages consuming more crop than they make."
              >
                {health.starving.length} starving ({signed(health.starvingCropPerHour)}/h)
              </button>
            )}

            {health.noFreeMerchants.length > 0 && (
              <button
                className={`text-xs px-2 py-0.5 rounded border ${
                  rowFilter === 'noMerchants'
                    ? 'border-warning text-warning bg-warning/10'
                    : 'border-gray-700 text-warning hover:bg-warning/10'
                }`}
                aria-pressed={rowFilter === 'noMerchants'}
                onClick={() => setRowFilter((f) => (f === 'noMerchants' ? null : 'noMerchants'))}
                title="Every merchant is already in flight, so these villages cannot start a route right now."
              >
                {health.noFreeMerchants.length} with no free merchants
              </button>
            )}

            {health.unknownCrop.length > 0 && (
              <button
                className={`text-xs px-2 py-0.5 rounded border ${
                  rowFilter === 'unknownCrop'
                    ? 'border-warning text-warning bg-warning/10'
                    : 'border-gray-700 text-warning hover:bg-warning/10'
                }`}
                aria-pressed={rowFilter === 'unknownCrop'}
                onClick={() => setRowFilter((f) => (f === 'unknownCrop' ? null : 'unknownCrop'))}
                title="Crop rate could not be derived for these villages. They are left out of the net above rather than counted as zero."
              >
                {health.unknownCrop.length} unknown crop
              </button>
            )}

            {health.missingTradeOffice.length > 0 && (
              <button
                className={`text-xs px-2 py-0.5 rounded border ${
                  rowFilter === 'missingTradeOffice'
                    ? 'border-warning text-warning bg-warning/10'
                    : 'border-gray-700 text-warning hover:bg-warning/10'
                }`}
                aria-pressed={rowFilter === 'missingTradeOffice'}
                onClick={() =>
                  setRowFilter((f) => (f === 'missingTradeOffice' ? null : 'missingTradeOffice'))
                }
                title="No Trade Office level typed. The planner floors these to 0, which over-provisions merchants rather than breaching the budget."
              >
                {health.missingTradeOffice.length} without Trade Office
              </button>
            )}

            {rowFilter && (
              <button
                className="text-xs text-secondary underline"
                onClick={() => setRowFilter(null)}
              >
                show all {health.total}
              </button>
            )}
          </div>

          <div className="flex items-center gap-2 flex-wrap mb-3">
            <span className="text-secondary text-xs">Typed columns:</span>
            <button
              className="btn-secondary btn-sm"
              onClick={scanTradeOffices}
              disabled={scanningTradeOffices || fetching}
              title="Reads each village's Trade Office level from the game instead of you typing it. Two page reads per village, paced by the stealth throttler, so it takes a few minutes."
            >
              {scanningTradeOffices
                ? 'Reading…'
                : `Read Trade Office from game (~${villages.length * 2} requests)`}
            </button>
            <input
              ref={setupFileRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={onSetupFileChosen}
            />
          </div>

          {/* Where the typed setup lives. The file buttons moved in here with
              the server ones rather than staying in the row above: they answer
              one question -- where does this survive -- and split across two
              places the operator would reasonably think the file WAS the
              server copy. */}
          <SetupStorage
            status={serverSetup}
            busy={setupBusy}
            onSave={saveSetupToServer}
            onLoad={loadSetupFromServer}
            onForget={() => setConfirmForget(true)}
            onExportFile={exportSetup}
            onImportFile={() => setupFileRef.current?.click()}
            onPaste={() => setPasteOpen((v) => !v)}
            pasteOpen={pasteOpen}
          />

          {pasteOpen && (
            <div className="mb-3">
              {/* A paste box as well as a file picker: this gets used from a
                  phone over Tailscale, where picking a file is awkward. */}
              <textarea
                className="input-field w-full h-28 text-xs font-mono"
                placeholder='Paste the contents of a setup file here, then press Load'
                aria-label="Setup file contents"
                value={pasteText}
                onChange={(e) => setPasteText(e.target.value)}
              />
              <button
                className="btn-primary btn-sm mt-2"
                disabled={!pasteText.trim()}
                onClick={() => applySetupText(pasteText)}
              >
                Load pasted setup
              </button>
            </div>
          )}

          {setupReport && (
            <div className="mb-3 text-xs space-y-1">
              <div className="text-success">
                Loaded {setupReport.loaded} village(s) from the{' '}
                {setupReport.source ?? 'setup file'}.
              </div>
              {setupReport.missingFromAccount.length > 0 && (
                <div className="text-warning">
                  {setupReport.missingFromAccount.length} village(s) in the file are not in this
                  account and were skipped:{' '}
                  {setupReport.missingFromAccount.map((v) => v.name || v.village_id).join(', ')}
                </div>
              )}
              {setupStillUnknown.length > 0 && (
                <div className="text-warning">
                  {setupStillUnknown.length} village(s) still have no Trade Office level and
                  are planned as 0, which over-provisions merchants rather than breaching the
                  budget:{' '}
                  {setupStillUnknown.map((v) => v.name || v.village_id).join(', ')}
                </div>
              )}
              {/* A relay tier that quietly lost one of the villages it was
                  feeding is a tier the operator believes is complete, with the
                  next plan reporting that village as unreachable and nothing
                  connecting the two. So the pruning is named. */}
              {(setupReport.relayTargetsDropped?.length ?? 0) > 0 && (
                <div className="text-warning">
                  A relay in the file forwards to village(s) this account no longer has, so
                  they were taken off its list:{' '}
                  {setupReport.relayTargetsDropped
                    .map(
                      (entry) =>
                        `${entry.name || entry.village_id} → ${entry.dropped.join(', ')}`
                    )
                    .join('; ')}
                </div>
              )}
            </div>
          )}

          {/* The stored crop spend that was dropped on the way in. Said out
              loud, because the figure was silencing a CRITICAL and removing it
              brings that finding back on the next plan -- and loading the same
              figure from a file raises a visible error, so silence here was the
              odd one out. `role="status"` rather than an alert: nothing is
              broken, and it is already history by the time it is read. */}
          {cropSpendsDropped.length > 0 && (
            <div className="mb-3 text-xs text-warning" role="status">
              A saved crop spend was dropped for{' '}
              {namesForVillageIds(cropSpendsDropped.map(Number), villages)} — crop is stated as a
              TARGET, not a spend, because the snapshot&rsquo;s crop rate is already net of troop
              upkeep. Set what{' '}
              {/* One branch per number rather than a plural verb followed by a
                  singular possessive: "what they should keep with its crop
                  allocation" reads as one village's allocation shared by
                  several, which is the opposite of what a per-village target
                  is. */}
              {cropSpendsDropped.length > 1
                ? 'they should keep with their crop allocations'
                : 'it should keep with its crop allocation'}{' '}
              instead; an overflow that figure was hiding will show up on the next plan.
            </div>
          )}

          {/* The same receipt for a stored role or template key outside the
              five. Two sentences rather than one, because the two drops leave
              the account in different states: a village that lost its role
              plans QUIETLY WRONG (it reverts to keeping its own production),
              while a village that kept its role and lost its template is
              REFUSED by the backend until one is typed. `role="status"`, like
              the crop note: nothing is broken, and it is history by the time
              it is read. */}
          {rolesDropped && rolesDropped.droppedFrom.length > 0 && (
            <div className="mb-3 text-xs text-warning" role="status">
              A saved role ({rolesDropped.droppedNames.join(', ')}) is not one of this
              build&rsquo;s five and was dropped for{' '}
              {namesForVillageIds(rolesDropped.droppedFrom.map(Number), villages)} — the backend
              refuses an unknown role, so it would have failed every plan. Set{' '}
              {rolesDropped.droppedFrom.length > 1 ? 'their roles' : 'its role'} again in the
              table below; until then{' '}
              {/* Same rule as the crop note above: the verb and the possessive
                  have to agree, or several villages are described as keeping
                  one village's production. */}
              {rolesDropped.droppedFrom.length > 1
                ? 'they keep only their own production'
                : 'it keeps only its own production'}
              , with no template targets and no spend.
            </div>
          )}
          {rolesDropped && rolesDropped.droppedTemplates.length > 0 && (
            <div className="mb-3 text-xs text-warning" role="status">
              A saved role template ({rolesDropped.droppedTemplates.join(', ')}) is not one of
              this build&rsquo;s five roles and was dropped. Any village still holding that role
              has no profile, and the plan will refuse it rather than read the village as keeping
              its own production.
            </div>
          )}

          {/* Same rule as the Allocate grid, and for higher stakes: the four
              hand-typed columns are the RIGHTMOST of fourteen, so the village
              name is off-screen exactly while a Trade Office level is being
              typed — and a level typed one row off breaches that village's
              merchant budget without a warning anywhere. Pin the identity
              column and say the rest are there.

              The hint's words come off this header row (see ScrollableTable),
              because the hand-typed one had gone stale in the way an
              enumeration always does: it named Merchants then Trade Office,
              with Role and Max busy between them on screen, so the reader
              counted across to Role and typed into it. */}
          <ScrollableTable>
            <table className="w-full text-sm">
              <thead className="text-secondary text-xs uppercase">
                <tr>
                  <th className="text-left py-2 px-2 sticky-col">
                    <SortHeader
                      label="Village"
                      col="name"
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSort={toggleSort}
                    />
                  </th>
                  <th className="text-right px-2">
                    <SortHeader
                      label="Lumber/h"
                      col="lumber"
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSort={toggleSort}
                    />
                  </th>
                  <th className="text-right px-2">
                    <SortHeader
                      label="Clay/h"
                      col="clay"
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSort={toggleSort}
                    />
                  </th>
                  <th className="text-right px-2">
                    <SortHeader
                      label="Iron/h"
                      col="iron"
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSort={toggleSort}
                    />
                  </th>
                  <th className="text-left px-2">
                    <SortHeader
                      label="Net crop"
                      col="crop"
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSort={toggleSort}
                    />
                  </th>
                  <th className="text-right px-2">
                    <SortHeader
                      label="Merchants"
                      col="merchants"
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSort={toggleSort}
                    />
                  </th>
                  <th
                    className="text-left px-2"
                    title="What this village is FOR (profile section 1). Its role's template supplies every target and spend the village does not state itself, decides whether the planner may relay through it, and whether a crop deficit here reads as designed rather than as an emergency. Nothing in the game says a village is the Hammer, so this is typed once. Give the role a template in Role templates, under Allocate."
                  >
                    Role
                  </th>
                  <th className="text-right px-2">Trade Office</th>
                  <th
                    className="text-right px-2"
                    title="The most merchants this village may have underway or RETURNING at once (profile section 5: “maximum 8 busy at 02”). A ceiling on the plan, not merchants held back: the account-wide reserve below holds some idle at EVERY village, and off a full fleet the two are not even the same figure — 19 merchants less a reserve of 12 is 7, where a cap of 8 is 8. Blank means no ceiling, so the budget is the fleet less the reserve. 0 does not stop the village shipping: its routes are still planned, and every one of them becomes a budget breach that refuses the sheet. To stop it shipping, restrict Ships only to instead."
                  >
                    Max busy
                  </th>
                  <th
                    className="text-right px-2"
                    title="Alert when this village's crop stock would cross this level (e.g. your NPC trigger). Used by the full-day check."
                  >
                    Crop alert
                  </th>
                  <th
                    className="text-left px-2"
                    title="Where this village may send. Unrestricted by default; once restricted it ships to the ticked villages only, and a restriction with nothing ticked ships to nobody. Tributes are governed by their own exclusions."
                  >
                    Ships only to
                  </th>
                  <th
                    className="text-left px-2"
                    title="Villages this one FORWARDS the capital's lumber, clay and iron on to (profile section 5's relay tier). Not a preference: 02 may only reach its own neighbours, so without a relay the defensive villages beyond them are unreachable and the plan comes back infeasible with a shortfall each. One hop only, and a role village may not relay — a feeder, or a village with no role, may. Materials only: crop already relays through a sub-hub wherever the route search finds it worth doing. The merchants for the COLLECTING leg are billed to whoever sends it, so at 02 they count inside its Max busy."
                  >
                    Relays for
                  </th>
                  <th
                    className="text-right px-2"
                    title="Share of warehouse capacity this village keeps stocked by NPC trading. The planner may draw it down over the profile window as extra lumber, clay or iron — never crop."
                  >
                    Stock floor %
                  </th>
                  <th
                    className="text-left px-2"
                    title="Which of this village's stores NPC may convert FROM. Derived by default: everything the village is not drawing on, which is what section 7 describes for the capital — clay and crop into wood. Override it only where you want the conversion to spare a store. Naming a resource the village is already shipping beyond its own production is refused, because NPC cannot convert a resource into itself. Only meaningful where there is a stock floor."
                  >
                    NPC converts from
                  </th>
                  <th
                    className="text-left px-2"
                    title="What this village SPENDS per hour — lumber, clay and iron only: the building queue and the troop upkeep. Not the allocation target: the target is the rate that must be HERE (own production plus whatever ships in), so the store nets target − consumption. Nothing in the game reports this, because the statistics page shows materials gross — a village burning lumber still reads positive there. Crop is refused, because the snapshot's crop rate is already net of upkeep; say what a village keeps of its crop with its crop target instead. A village with a role takes its role template's figures for every resource it does not state itself, and the cell says which those are."
                  >
                    Consumption /h
                  </th>
                </tr>
              </thead>
              <tbody>
                {visibleVillages.map((v) => (
                  <tr
                    key={v.village_id}
                    className="row-focus touch-target border-t-default hover:bg-white/5 transition-colors"
                  >
                    <td className="py-1.5 px-2 sticky-col row-focus-edge">
                      {v.name}{' '}
                      <span className="text-secondary text-xs">
                        ({v.x}|{v.y})
                      </span>
                    </td>
                    <td className="text-right px-2 font-mono">{fmt(v.lumber_per_hour)}</td>
                    <td className="text-right px-2 font-mono">{fmt(v.clay_per_hour)}</td>
                    <td className="text-right px-2 font-mono">{fmt(v.iron_per_hour)}</td>
                    <td className="px-2 font-mono">
                      <CropCell village={v} />
                    </td>
                    <td className="text-right px-2 font-mono">
                      {v.merchants_free}/{v.merchants_total}
                    </td>
                    <td className="px-2">
                      {/* Owned, like the Trade Office level. Section 1 assigns a
                          role to every village, and the role is what makes one
                          profile serve four defensive villages -- so this is the
                          field that removes the typing, not decoration. */}
                      {(() => {
                        const role = villageRoles[v.village_id]
                        // `isEmptyTemplate`, the same predicate `rolesForRequest`,
                        // `rolesMissingTemplates` and the panel's own count use. A
                        // null check made this the one surface that stayed silent
                        // about a template emptied box by box: the role key
                        // outlives its last figure, so `{"def": {"consumption": {}}}`
                        // read as present here while the panel said "0 typed" and
                        // the plan came back 422. This row is the only place that
                        // names the VILLAGE, so it is the one that most needed to
                        // agree.
                        const missing = role != null && isEmptyTemplate(roleTemplates[role])
                        const problemId = `role-problem-${v.village_id}`
                        return (
                          <>
                            <select
                              aria-label={`Role for ${v.name}`}
                              aria-invalid={missing || undefined}
                              aria-describedby={missing ? problemId : undefined}
                              className="input-field w-auto text-xs py-1"
                              value={role ?? ''}
                              onChange={(e) =>
                                setVillageRoles((prev) => {
                                  const next = { ...prev }
                                  if (e.target.value === '') delete next[v.village_id]
                                  else next[v.village_id] = e.target.value
                                  return next
                                })
                              }
                            >
                              <option value="">none</option>
                              {VILLAGE_ROLES.map((name) => (
                                <option key={name} value={name}>
                                  {ROLE_LABEL[name]}
                                </option>
                              ))}
                            </select>
                            {/* Named, not just coloured, and named HERE: the
                                backend refuses a role whose template was never
                                sent, because planning without it would read this
                                village as keeping its own production -- a tenth
                                of what a defensive village needs -- and call the
                                plan feasible. */}
                            {missing && (
                              <>
                                <span id={problemId} className="block text-warning text-xs mt-0.5">
                                  no {ROLE_LABEL[role]} template yet
                                </span>
                                {/* A SIBLING of the description, not a child of
                                    it. The select's `aria-describedby` points at
                                    the span above, and a screen reader flattens
                                    a description to its text -- so a button
                                    inside it would be read out as words and
                                    reached only by accident. Outside it, the
                                    description says exactly what it always said
                                    and this is the next tab stop after the
                                    select the operator just used.

                                    The role is in the VISIBLE label rather than
                                    an aria-label, so the accessible name
                                    contains the words on screen (WCAG 2.5.3) and
                                    four rows claiming the same missing role do
                                    not announce as four unlabelled buttons. */}
                                <button
                                  type="button"
                                  className="block underline text-warning hover:text-primary text-xs mt-0.5 pointer-coarse:min-h-11"
                                  onClick={() => jumpToRoleTemplate(role)}
                                >
                                  Type the {ROLE_LABEL[role]} figures
                                </button>
                              </>
                            )}
                          </>
                        )
                      })()}
                    </td>
                    <td className="text-right px-2">
                      {/* Owned, not fetched — editable, and blank means "unknown",
                          which the planner floors to 0 rather than guessing up. */}
                      <input
                        type="number"
                        min="0"
                        max="20"
                        aria-label={`Trade Office level for ${v.name}`}
                        placeholder="?"
                        className="input-field w-20 text-right text-xs py-1"
                        value={tradeOffice[v.village_id] ?? ''}
                        onChange={(e) =>
                          setTradeOffice((prev) => ({
                            ...prev,
                            [v.village_id]: e.target.value === '' ? undefined : Number(e.target.value),
                          }))
                        }
                      />
                    </td>
                    <td className="text-right px-2">
                      {(() => {
                        const cap = maxBusy[v.village_id]
                        const beyondFleet = unreachableFleets.has(v.village_id)
                        const invalid = (cap != null && !isMaxBusyMerchants(cap)) || beyondFleet
                        const problemId = `max-busy-problem-${v.village_id}`
                        return (
                          <>
                            <input
                              type="number"
                              min="0"
                              // 0 is the snapshot saying it could not read a
                              // count, so the bound falls back to the 20 a
                              // village can ever hold -- as `unreachableCaps`
                              // and the backend both do.
                              max={v.merchants_total || MAX_MERCHANTS_PER_VILLAGE}
                              aria-label={`Most merchants busy at once for ${v.name}`}
                              aria-invalid={invalid || undefined}
                              aria-describedby={invalid ? problemId : undefined}
                              placeholder="none"
                              className="input-field w-20 text-right text-xs py-1"
                              value={cap ?? ''}
                              onChange={(e) =>
                                setMaxBusy((prev) => ({
                                  ...prev,
                                  [v.village_id]:
                                    e.target.value === '' ? undefined : Number(e.target.value),
                                }))
                              }
                            />
                            {/* Named, not just outlined: the backend refuses the
                                whole plan over a ceiling the village cannot
                                reach, and "422" on a Build click is not a
                                sentence that leads anyone back to this cell. */}
                            {invalid && (
                              <span id={problemId} className="block text-warning text-xs mt-0.5">
                                {beyondFleet
                                  ? `only ${v.merchants_total} merchants here`
                                  : `0 to ${MAX_MERCHANTS_PER_VILLAGE}`}
                              </span>
                            )}
                          </>
                        )
                      })()}
                    </td>
                    <td className="text-right px-2">
                      <input
                        type="number"
                        min="0"
                        aria-label={`Crop stock alert level for ${v.name}`}
                        placeholder="none"
                        className="input-field w-28 text-right text-xs py-1"
                        value={cropCeilings[v.village_id] ?? ''}
                        onChange={(e) =>
                          setCropCeilings((prev) => ({
                            ...prev,
                            [v.village_id]:
                              e.target.value === '' ? undefined : Number(e.target.value),
                          }))
                        }
                      />
                    </td>
                    <td className="px-2">
                      {/* Owned, like the Trade Office level. Nothing stored is the
                          unrestricted default and reads muted like the other blanks;
                          a restriction reads in full-strength text. Ticking a village
                          while unrestricted starts the list; unticking the last one
                          leaves an EMPTY list, which is "nobody", not "anyone" --
                          only "Lift restriction" returns to the default. */}
                      {(() => {
                        const allowed = shipOnlyTo[v.village_id]
                        return (
                          <details className="text-xs">
                            <summary
                              className={`cursor-pointer whitespace-nowrap pointer-coarse:min-h-11 ${
                                allowed == null ? 'text-secondary' : 'text-primary'
                              }`}
                            >
                              <span className="sr-only">Ships only to, for {v.name}: </span>
                              {describeShipOnlyTo(allowed, villages)}
                            </summary>
                            <div
                              role="group"
                              aria-label={`Villages ${v.name} may ship to`}
                              className="mt-1 max-h-40 overflow-y-auto"
                            >
                              {villages
                                .filter((o) => o.village_id !== v.village_id)
                                .map((o) => (
                                  <label
                                    key={o.village_id}
                                    className="flex items-center gap-1 whitespace-nowrap"
                                  >
                                    <input
                                      type="checkbox"
                                      checked={allowed?.includes(o.village_id) ?? false}
                                      onChange={(e) =>
                                        setShipOnlyTo((prev) => {
                                          const current = prev[v.village_id] ?? []
                                          return {
                                            ...prev,
                                            [v.village_id]: e.target.checked
                                              ? [...current, o.village_id]
                                              : current.filter((id) => id !== o.village_id),
                                          }
                                        })
                                      }
                                    />
                                    {o.name}
                                  </label>
                                ))}
                              {allowed != null && (
                                <button
                                  type="button"
                                  className="underline mt-1"
                                  onClick={() =>
                                    setShipOnlyTo((prev) => {
                                      const next = { ...prev }
                                      delete next[v.village_id]
                                      return next
                                    })
                                  }
                                >
                                  Lift restriction
                                </button>
                              )}
                            </div>
                          </details>
                        )
                      })()}
                    </td>
                    <td className="px-2">
                      {/* Owned like Ships only to beside it, and the same picker
                          shape -- but the OPPOSITE rule about an empty list.
                          Nothing stored is "not a relay" and reads muted; an
                          empty list is the picker mid-edit rather than an
                          answer, so it says "nobody yet" and is dropped from the
                          request. "Stop relaying" removes the row entirely.
                          A problem shows on the cell that caused it: the backend
                          refuses the same four things with a 422, and a 422 from
                          a plan call names a village in a 26-row table with
                          nothing on screen pointing at it. */}
                      {(() => {
                        const forwards = relayFor[v.village_id]
                        const problems = relayProblems[v.village_id] ?? []
                        const problemId = `relay-problem-${v.village_id}`
                        return (
                          <details className="text-xs">
                            <summary
                              className={`cursor-pointer whitespace-nowrap pointer-coarse:min-h-11 ${
                                problems.length
                                  ? 'text-danger'
                                  : forwards?.length
                                    ? 'text-primary'
                                    : 'text-secondary'
                              }`}
                              aria-describedby={problems.length ? problemId : undefined}
                            >
                              <span className="sr-only">Relays for, for {v.name}: </span>
                              {describeRelayFor(forwards, villages)}
                            </summary>
                            <div
                              role="group"
                              aria-label={`Villages ${v.name} forwards material to`}
                              className="mt-1 max-h-40 overflow-y-auto"
                            >
                              {villages
                                .filter((o) => o.village_id !== v.village_id)
                                .map((o) => (
                                  <label
                                    key={o.village_id}
                                    className="flex items-center gap-1 whitespace-nowrap"
                                  >
                                    <input
                                      type="checkbox"
                                      checked={forwards?.includes(o.village_id) ?? false}
                                      onChange={(e) =>
                                        setRelayFor((prev) => {
                                          const current = prev[v.village_id] ?? []
                                          return {
                                            ...prev,
                                            [v.village_id]: e.target.checked
                                              ? [...current, o.village_id]
                                              : current.filter((id) => id !== o.village_id),
                                          }
                                        })
                                      }
                                    />
                                    {o.name}
                                  </label>
                                ))}
                              {forwards != null && (
                                <button
                                  type="button"
                                  className="underline mt-1"
                                  onClick={() =>
                                    setRelayFor((prev) => {
                                      const next = { ...prev }
                                      delete next[v.village_id]
                                      return next
                                    })
                                  }
                                >
                                  Stop relaying
                                </button>
                              )}
                            </div>
                            {problems.length > 0 && (
                              <p id={problemId} className="text-danger mt-1 max-w-xs">
                                {problems.join(' ')}
                              </p>
                            )}
                          </details>
                        )
                      })()}
                    </td>
                    <td className="text-right px-2">
                      {(() => {
                        const floor = stockFloors[v.village_id]
                        const invalid = floor != null && !isStockFloorFraction(floor)
                        const problemId = `stock-floor-problem-${v.village_id}`
                        return (
                          <>
                            <input
                              type="number"
                              min="0"
                              max="95"
                              step="0.1"
                              aria-label={`NPC-backed stock floor for ${v.name}, percent of warehouse`}
                              aria-invalid={invalid || undefined}
                              aria-describedby={invalid ? problemId : undefined}
                              placeholder="none"
                              className="input-field w-20 text-right text-xs py-1"
                              value={floor == null ? '' : fractionToPercent(floor)}
                              onChange={(e) =>
                                setStockFloors((prev) => ({
                                  ...prev,
                                  [v.village_id]:
                                    e.target.value === ''
                                      ? undefined
                                      : Number(e.target.value) / 100,
                                }))
                              }
                            />
                            {/* Named, not just coloured: the backend refuses a floor
                                outside 0-95%, and the operator should not need a
                                failed plan to learn that. */}
                            {invalid && (
                              <span id={problemId} className="block text-warning text-xs mt-0.5">
                                0–95%, whole or one decimal
                              </span>
                            )}
                          </>
                        )
                      })()}
                    </td>
                    <td className="px-2">
                      {/* The feedstock override, and the one control here whose
                          RESTING STATE is a word rather than a blank. "Derived"
                          is an answer -- everything the village is not drawing
                          on, which is what section 7 describes for the capital
                          -- so an empty-looking box would misrepresent it as
                          something nobody has decided yet. An empty LIST is a
                          third thing again: the picker opened and not yet
                          ticked, which is dropped from the request because NPC
                          cannot convert from nothing and the backend refuses
                          one. */}
                      {(() => {
                        const chosen = npcFeedstock[v.village_id]
                        const floor = stockFloors[v.village_id]
                        const override = isFeedstockList(chosen)
                        const halfTyped = Array.isArray(chosen) && chosen.length === 0
                        return (
                          <details className="text-xs">
                            <summary
                              className={`cursor-pointer whitespace-nowrap pointer-coarse:min-h-11 ${
                                halfTyped
                                  ? 'text-warning'
                                  : override
                                    ? 'text-primary'
                                    : 'text-secondary'
                              }`}
                            >
                              <span className="sr-only">NPC converts from, for {v.name}: </span>
                              {describeFeedstock(chosen ?? null)}
                            </summary>
                            <div
                              role="group"
                              aria-label={`Stores NPC may convert from at ${v.name}`}
                              className="mt-1"
                            >
                              {NPC_FEEDSTOCK_RESOURCES.map((resource) => (
                                <label
                                  key={resource}
                                  className="flex items-center gap-1 whitespace-nowrap"
                                >
                                  <input
                                    type="checkbox"
                                    aria-label={`NPC may convert ${RESOURCE_LABEL[resource]} at ${v.name}`}
                                    checked={chosen?.includes(resource) ?? false}
                                    onChange={(e) =>
                                      setNpcFeedstock((prev) => {
                                        const current = prev[v.village_id] ?? []
                                        return {
                                          ...prev,
                                          [v.village_id]: e.target.checked
                                            ? [...current, resource]
                                            : current.filter((r) => r !== resource),
                                        }
                                      })
                                    }
                                  />
                                  {RESOURCE_LABEL[resource]}
                                </label>
                              ))}
                              {/* Only meaningful alongside a floor, and said
                                  rather than enforced: the override is owned
                                  state and may be typed before the floor it
                                  qualifies, but a village with no floor
                                  converts nothing whatever this says. */}
                              {floor == null && (
                                <p className="text-secondary mt-1 max-w-56">
                                  No stock floor here, so nothing converts. Set{' '}
                                  <span className="text-primary">Stock floor %</span> first;
                                  this only says which stores would pay for it.
                                </p>
                              )}
                              {halfTyped && (
                                <p className="text-warning mt-1 max-w-56">
                                  Nothing ticked, so this is not an override yet — the
                                  feedstock is still derived. NPC exchanges one resource for
                                  another and cannot convert from nothing.
                                </p>
                              )}
                              {chosen != null && (
                                <button
                                  type="button"
                                  className="underline mt-1"
                                  onClick={() =>
                                    setNpcFeedstock((prev) => {
                                      const next = { ...prev }
                                      delete next[v.village_id]
                                      return next
                                    })
                                  }
                                >
                                  Back to derived
                                </button>
                              )}
                            </div>
                          </details>
                        )
                      })()}
                    </td>
                    <td className="px-2">
                      {/* Owned, like the Trade Office level, and the number the
                          overflow check was missing. A village told to LAND
                          14,751 lumber an hour because it BURNS 14,751 was read
                          as banking all of it, and reported as losing 354,024 a
                          day at a warehouse cap it never reaches. Blank means
                          nothing declared and reads muted like the other blanks;
                          clearing every box is the same as never typing one. */}
                      {(() => {
                        // The RESOLVED spend, role template included, and the
                        // same reader the Allocate view uses. Reading the own
                        // map alone printed "none" and three blank boxes for a
                        // defensive village the plan was spending 8,372/h of
                        // lumber at -- so the two surfaces disagreed about one
                        // village, which is the defect the targets already had.
                        const role = villageRoles[v.village_id]
                        const resolved = resolvedSpend(
                          roleTemplates[role],
                          consumption[v.village_id]
                        )
                        const spent = resolved.effective
                        // Named, because a role's figure and a hand-typed one
                        // are the same digits: without this the operator cannot
                        // tell which village they have actually said something
                        // about, and would read a whole profile as their own
                        // entry.
                        const source = describeSpendSource(resolved, role)
                        return (
                          <details className="text-xs">
                            <summary
                              className={`cursor-pointer whitespace-nowrap pointer-coarse:min-h-11 ${
                                declaresConsumption(spent) ? 'text-primary' : 'text-secondary'
                              }`}
                            >
                              <span className="sr-only">Spends per hour, for {v.name}: </span>
                              {describeConsumption(spent)}
                              {source && (
                                <span className="text-info ml-1">{'· '}{source}</span>
                              )}
                            </summary>
                            <div
                              role="group"
                              aria-label={`What ${v.name} spends per hour`}
                              className="mt-1"
                            >
                              {CONSUMABLE_RESOURCES.map((resource) => {
                                const rate = spent?.[resource]
                                const bad = rate != null && !isConsumptionRate(rate)
                                const problem = `spend-problem-${v.village_id}-${resource}`
                                return (
                                  <label
                                    key={resource}
                                    className="flex items-center justify-between gap-2 whitespace-nowrap mt-0.5"
                                  >
                                    <span className="text-secondary">
                                      {RESOURCE_LABEL[resource]}
                                    </span>
                                    <input
                                      type="number"
                                      min="0"
                                      step="1"
                                      aria-label={`${RESOURCE_LABEL[resource]} spent per hour by ${v.name}`}
                                      aria-invalid={bad || undefined}
                                      aria-describedby={bad ? problem : undefined}
                                      placeholder="none"
                                      className="input-field w-24 text-right text-xs py-1"
                                      value={rate ?? ''}
                                      onChange={(e) =>
                                        setSpend(v.village_id, resource, e.target.value)
                                      }
                                    />
                                    {/* Named, not just coloured: the backend
                                        refuses a negative spend outright, and a
                                        rate's sign cannot be read as
                                        consumption anyway. */}
                                    {bad && (
                                      <span id={problem} className="text-warning">
                                        0 or more
                                      </span>
                                    )}
                                  </label>
                                )
                              })}
                              {/* There is no Crop box, and the reason has to be
                                  here rather than only in a tooltip: the
                                  operator's own profile lists a crop figure per
                                  role village, so its absence reads as an
                                  oversight unless the alternative is named. */}
                              <p className="text-secondary mt-1 max-w-56">
                                No crop: the snapshot&apos;s crop rate is already net of upkeep, so
                                a declared crop spend would subtract the same troops twice. Set the
                                crop <span className="text-primary">target</span> instead — 0 holds
                                a crop-negative village level.
                              </p>
                              {/* Says where a figure the operator did not type
                                  came from, and what typing over it does. The
                                  boxes now hold the RESOLVED spend, so without
                                  this a whole profile reads as this village's
                                  own entry. */}
                              {resolved.fromRole.length > 0 && (
                                <p className="text-info mt-1 max-w-56">
                                  {resolved.fromRole
                                    .map((resource) => RESOURCE_LABEL[resource])
                                    .join(', ')}{' '}
                                  {resolved.fromRole.length === 1 ? 'comes' : 'come'} from the{' '}
                                  {ROLE_LABEL[role]} template. Typing here overrides this village
                                  only, per resource; clearing the box hands it back to the
                                  profile.
                                </p>
                              )}
                              {/* Gated on what this village ITSELF states, not
                                  on the resolved figures: a templated village
                                  with no own entry has nothing to clear, and
                                  offering the button there would read as a way
                                  to drop the role's profile. */}
                              {declaresConsumption(consumption[v.village_id]) && (
                                <button
                                  type="button"
                                  className="underline mt-1"
                                  onClick={() =>
                                    setConsumption((prev) => {
                                      const next = { ...prev }
                                      delete next[v.village_id]
                                      return next
                                    })
                                  }
                                >
                                  Clear this village&apos;s own figures
                                </button>
                              )}
                            </div>
                          </details>
                        )
                      })()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollableTable>
          {rowFilter && (
            <p className="text-xs text-secondary mt-2">
              Showing {visibleVillages.length} of {health.total} villages.{' '}
              <button className="underline" onClick={() => setRowFilter(null)}>
                Clear filter
              </button>
            </p>
          )}
          {snapshot?.warnings?.length > 0 && (
            <div className="mt-3">
              {/* Explicit label so "this is a warning" is not carried by colour
                  alone (and is announced by screen readers). */}
              <p className="text-xs text-warning font-semibold">
                ⚠ Warnings ({snapshot.warnings.length})
              </p>
              <ul className="text-xs text-warning list-disc list-inside">
                {snapshot.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}

          {/* World + merchant model. Capacity is server-calibrated and speed
              defaults to the tribe's x1 value; both — plus map span — are
              overridable for non-Europe 2 worlds without spending a request. */}
          <div className="mt-4 flex items-center gap-4 flex-wrap text-xs border-t border-gray-800 pt-3">
            <span className="text-secondary uppercase">World &amp; merchants</span>
            <label className="flex items-center gap-1">
              <span className="text-secondary">Base capacity</span>
              <input
                type="number"
                min="1"
                aria-label="Merchant base capacity"
                aria-invalid={merchantProblems.base_capacity ? true : undefined}
                aria-describedby={
                  merchantProblems.base_capacity ? 'merchant-problem-base_capacity' : undefined
                }
                className="input-field w-24 text-right py-1"
                value={merchantModel.base_capacity ?? ''}
                onChange={(e) =>
                  setMerchantModel((m) => ({
                    ...m,
                    // `Number('')` is 0, and a 0 here is not what an emptied box
                    // says. The four boxes beside this one already guard it; these
                    // two did not, so clearing either wrote a zero nobody typed --
                    // and then the two went different wrong ways. Base capacity was
                    // dropped by `|| undefined` at the payload, so the box read 0
                    // while the plan was built at the backend default; the bonus was
                    // kept, so an accidental clear silently stopped every Trade
                    // Office level adding capacity.
                    base_capacity: e.target.value === '' ? undefined : Number(e.target.value),
                  }))
                }
              />
              <MerchantRule
                id="merchant-problem-base_capacity"
                rule={merchantProblems.base_capacity}
              />
            </label>
            <label className="flex items-center gap-1">
              <span className="text-secondary">Bonus / TO level</span>
              <input
                type="number"
                min="0"
                step="0.05"
                aria-label="Trade Office bonus per level"
                aria-invalid={merchantProblems.bonus_per_to_level ? true : undefined}
                aria-describedby={
                  merchantProblems.bonus_per_to_level ? 'merchant-problem-bonus_per_to_level' : undefined
                }
                className="input-field w-20 text-right py-1"
                value={merchantModel.bonus_per_to_level ?? ''}
                onChange={(e) =>
                  setMerchantModel((m) => ({
                    ...m,
                    bonus_per_to_level:
                      e.target.value === '' ? undefined : Number(e.target.value),
                  }))
                }
              />
              <MerchantRule
                id="merchant-problem-bonus_per_to_level"
                rule={merchantProblems.bonus_per_to_level}
              />
            </label>
            <label className="flex items-center gap-1">
              <span className="text-secondary">Speed f/h</span>
              <input
                type="number"
                min="1"
                aria-label="Merchant speed fields per hour override"
                aria-invalid={merchantProblems.speed_fields_per_hour ? true : undefined}
                aria-describedby={
                  merchantProblems.speed_fields_per_hour ? 'merchant-problem-speed_fields_per_hour' : undefined
                }
                placeholder={String(snapshot?.speed_fields_per_hour ?? '')}
                className="input-field w-20 text-right py-1"
                value={merchantModel.speed_fields_per_hour ?? ''}
                onChange={(e) =>
                  setMerchantModel((m) => ({
                    ...m,
                    speed_fields_per_hour: e.target.value === '' ? undefined : Number(e.target.value),
                  }))
                }
              />
              <MerchantRule
                id="merchant-problem-speed_fields_per_hour"
                rule={merchantProblems.speed_fields_per_hour}
              />
            </label>
            <label className="flex items-center gap-1">
              <span
                className="text-secondary"
                title="Merchants to leave idle at EVERY village, so a shipment can be sent by hand without waiting for a route to come home. Account-wide: to hold ONE village down, cap it in the Max busy column instead."
              >
                Reserve / village
              </span>
              <input
                type="number"
                min="0"
                max={MAX_MERCHANTS_PER_VILLAGE}
                aria-label="Merchants held in reserve at every village"
                aria-invalid={merchantProblems.merchant_reserve ? true : undefined}
                aria-describedby={
                  merchantProblems.merchant_reserve ? 'merchant-problem-merchant_reserve' : undefined
                }
                placeholder={String(DEFAULT_MERCHANT_MODEL.merchant_reserve)}
                className="input-field w-20 text-right py-1"
                value={merchantModel.merchant_reserve ?? ''}
                onChange={(e) =>
                  setMerchantModel((m) => ({
                    ...m,
                    merchant_reserve: e.target.value === '' ? undefined : Number(e.target.value),
                  }))
                }
              />
              <MerchantRule
                id="merchant-problem-merchant_reserve"
                rule={merchantProblems.merchant_reserve}
              />
            </label>
            <label className="flex items-center gap-1">
              <span
                className="text-secondary"
                title="Share of each village's merchant budget the plan aims to leave uncommitted, so load spreads instead of piling onto whichever village is cheapest to ship from. Soft: exceeding it is reported, never fatal. 0 packs as tightly as it can."
              >
                Headroom %
              </span>
              <input
                type="number"
                min="0"
                max="99"
                aria-label="Merchant headroom, percent of each village's budget"
                aria-invalid={merchantProblems.merchant_headroom ? true : undefined}
                aria-describedby={
                  merchantProblems.merchant_headroom ? 'merchant-problem-merchant_headroom' : undefined
                }
                placeholder={String(DEFAULT_MERCHANT_MODEL.merchant_headroom * 100)}
                // w-24, matching the Base capacity box beside it: this box no
                // longer rounds its own value, so it has to fit two decimals.
                // Measured at 375/768/1440 -- "12.34" wants 87px and w-20 is
                // 80, which clipped it at all three.
                className="input-field w-24 text-right py-1"
                value={
                  // `fractionToPercent`, the same helper the stock-floor box
                  // uses, rather than a round to one decimal: the box writes
                  // back what it renders, so rounding the display silently
                  // rewrote a server-calibrated 0.1234 to 0.123 on the first
                  // keystroke. A display that changes the value it displays is
                  // not a display.
                  merchantModel.merchant_headroom == null
                    ? ''
                    : fractionToPercent(merchantModel.merchant_headroom)
                }
                onChange={(e) =>
                  setMerchantModel((m) => ({
                    ...m,
                    merchant_headroom:
                      e.target.value === '' ? undefined : Number(e.target.value) / 100,
                  }))
                }
              />
              <MerchantRule
                id="merchant-problem-merchant_headroom"
                rule={merchantProblems.merchant_headroom}
              />
            </label>
            <label className="flex items-center gap-1">
              <span className="text-secondary">Map span</span>
              <input
                type="number"
                min="1"
                aria-label="Map span override"
                aria-invalid={merchantProblems.map_span ? true : undefined}
                aria-describedby={
                  merchantProblems.map_span ? 'merchant-problem-map_span' : undefined
                }
                placeholder={String(snapshot?.map_span ?? '')}
                className="input-field w-20 text-right py-1"
                value={merchantModel.map_span ?? ''}
                onChange={(e) =>
                  setMerchantModel((m) => ({
                    ...m,
                    map_span: e.target.value === '' ? undefined : Number(e.target.value),
                  }))
                }
              />
              <MerchantRule
                id="merchant-problem-map_span"
                rule={merchantProblems.map_span}
              />
            </label>
          </div>

          {/* Villages outside the account that are owed crop. Kept in their own
              section rather than as rows in the village table: a tribute is not
              a village, and giving it a village row invites treating it as one.
              Saved as you type, like every other field here. */}
          <div className="mt-4 border-t border-gray-800 pt-3">
            <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
              <div>
                <span className="text-secondary text-xs uppercase">Crop owed to other players</span>
                <p className="text-secondary text-xs mt-0.5">
                  Travian only allows a Gold Club trade route to your own, Wonder, or
                  alliance/confederacy artifact villages. Tick “route-eligible” only for those —
                  the planner then ships it like any other demand. An ordinary ally/sitter village
                  is reported as a manual transfer and is left out of the route plan.
                </p>
              </div>
              <button
                type="button"
                className="btn-secondary text-xs py-1"
                onClick={() =>
                  setForeignTargets((prev) => [
                    ...prev,
                    {
                      name: '',
                      x: 0,
                      y: 0,
                      crop_per_hour: '',
                      safety_margin_pct: 5,
                      route_eligible: false,
                    },
                  ])
                }
              >
                + Add target
              </button>
            </div>

            {foreignTargets.length === 0 ? (
              <p className="text-secondary text-xs italic">
                None. Add one if you have promised crop to an ally or a sitter.
              </p>
            ) : (
              <>
                <ScrollableTable>
                  <table className="w-full text-xs">
                    <thead className="text-secondary uppercase">
                      <tr>
                        <th className="text-left py-1 px-2 sticky-col">Village</th>
                        <th className="text-right px-2">X</th>
                        <th className="text-right px-2">Y</th>
                        <th className="text-right px-2">Crop/h owed</th>
                        <th
                          className="text-right px-2"
                          title="Ship this much above the promise, so travel and rounding cannot leave it short"
                        >
                          Margin %
                        </th>
                        <th
                          className="text-right px-2"
                          title="How often it must be delivered. The planner satisfies a RATE and prefers the cheapest cycle that meets it, so an hourly obligation arrives as one lump every eight hours unless you say otherwise. Leave on 'any' when only the volume matters."
                        >
                          Every
                        </th>
                        <th
                          className="text-right px-2"
                          title="Villages that must NOT supply this target, by NAME, comma-separated (ids are accepted too). An hourly cycle commits one merchant per send in flight, so a distant village spends a fleet reaching here however little it carries — and the planner cannot know those merchants are wanted elsewhere."
                        >
                          Not from
                        </th>
                        <th className="text-right px-2">Ships/h</th>
                        <th
                          className="text-center px-2"
                          title="Tick only for your own, Wonder, or alliance/confederacy artifact villages — the only destinations Travian allows a Gold Club route to. Others are manual transfers."
                        >
                          Route?
                        </th>
                        <th className="px-2"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {foreignTargets.map((t, i) => {
                        const owed = Number(t.crop_per_hour) || 0
                        const ships = owed * (1 + (Number(t.safety_margin_pct) || 0) / 100)
                        // The same predicate `usableForeignTargets` filters on,
                        // so the badge and the payload can never disagree about
                        // which rows are being planned.
                        const incomplete = foreignTargetIsDraft(t)
                        const patch = (field, value) =>
                          setForeignTargets((prev) =>
                            prev.map((row, j) => (j === i ? { ...row, [field]: value } : row))
                          )
                        return (
                          <tr
                            key={i}
                            className="row-focus touch-target border-t-default hover:bg-white/5 transition-colors"
                          >
                            <td className="py-1 px-2 sticky-col row-focus-edge">
                              {/* w-36, and this column is PINNED (.sticky-col),
                                  so its width is taken off the visible strip
                                  permanently -- every other column has to fit
                                  in what is left, one at a time. w-56 was
                                  tried, to hold all 22 characters of
                                  "Rheinbund-Aussenposten", and it took the
                                  pinned column from 161px to 241px of the
                                  293px strip at 375: 82%, leaving 52px for
                                  nine columns of 96/96/112/96/96/128/63/60/56.
                                  Not one of them fitted. At 161px the 96px X
                                  column does.
                                  A name that scrolls is still legible -- the
                                  caret and the arrow keys reach the rest of
                                  it. A FIGURE is what may not scroll, and
                                  every figure in this row keeps its own
                                  width. */}
                              <input
                                type="text"
                                aria-label={`Foreign target ${i + 1} name`}
                                placeholder="Ally name"
                                className="input-field w-36 text-xs py-0.5"
                                value={t.name}
                                onChange={(e) => patch('name', e.target.value)}
                              />
                            </td>
                            <td className="text-right px-2">
                              <input
                                type="number"
                                aria-label={`Foreign target ${i + 1} x coordinate`}
                                className="input-field w-20 text-right text-xs py-0.5"
                                value={t.x}
                                onChange={(e) => patch('x', e.target.value)}
                              />
                            </td>
                            <td className="text-right px-2">
                              <input
                                type="number"
                                aria-label={`Foreign target ${i + 1} y coordinate`}
                                className="input-field w-20 text-right text-xs py-0.5"
                                value={t.y}
                                onChange={(e) => patch('y', e.target.value)}
                              />
                            </td>
                            <td className="text-right px-2">
                              <input
                                type="number"
                                min="0"
                                aria-label={`Foreign target ${i + 1} crop per hour`}
                                placeholder="0"
                                className="input-field w-24 text-right text-xs py-0.5"
                                value={t.crop_per_hour}
                                onChange={(e) => patch('crop_per_hour', e.target.value)}
                              />
                            </td>
                            <td className="text-right px-2">
                              <input
                                type="number"
                                min="0"
                                max="100"
                                aria-label={`Foreign target ${i + 1} safety margin`}
                                className="input-field w-20 text-right text-xs py-0.5"
                                value={t.safety_margin_pct}
                                onChange={(e) => patch('safety_margin_pct', e.target.value)}
                              />
                            </td>
                            {/* Cadence. The planner satisfies a RATE, and prefers
                                the cheapest cycle that meets it -- so an hourly
                                obligation arrives as one lump every eight hours
                                unless it is told otherwise. Blank means it may
                                choose. */}
                            <td className="text-right px-2">
                              <select
                                aria-label={`Foreign target ${i + 1} max cycle hours`}
                                className="input-field w-20 text-xs py-0.5"
                                value={t.max_cycle_hours ?? ''}
                                onChange={(e) => patch('max_cycle_hours', e.target.value)}
                              >
                                <option value="">any</option>
                                {TRAVIAN_REPEAT_INTERVALS.map((h) => (
                                  <option key={h} value={h}>
                                    {h}h
                                  </option>
                                ))}
                              </select>
                            </td>
                            {/* Who may NOT supply it. An hourly cycle commits one
                                merchant per send in flight, so a distant village
                                spends a fleet reaching this target however little
                                it carries -- and the planner cannot know those
                                merchants are wanted elsewhere. */}
                            <td className="text-right px-2">
                              {/* Named back, so a typo cannot pass for an exclusion.
                                  Silently dropping "2" for "02" would leave the
                                  operator believing a village is excluded while the
                                  next run draws on it.
                                  Computed ABOVE the input rather than under it so the
                                  box itself carries `aria-invalid`, like every other
                                  cell with a rule: `Build plan` gates on exactly the
                                  cells the page marks, and this one was marked in
                                  words only -- so the one field whose failure is
                                  SILENT on the backend was also the one the gate
                                  could not see. */}
                              {(() => {
                                const typed =
                                  t.exclude_origins_text ??
                                  namesForVillageIds(t.exclude_origins, villages)
                                const { unknown } = resolveVillageNames(typed, villages)
                                const problemId = `exclude-problem-${i}`
                                return (
                                  <>
                                    <input
                                      type="text"
                                      aria-label={`Foreign target ${i + 1} excluded origins`}
                                      aria-invalid={unknown.length ? true : undefined}
                                      aria-describedby={
                                        unknown.length ? problemId : undefined
                                      }
                                      placeholder="none"
                                      className="input-field w-28 text-right text-xs py-0.5"
                                      value={typed}
                                      onChange={(e) =>
                                        patch('exclude_origins_text', e.target.value)
                                      }
                                    />
                                    {unknown.length > 0 && (
                                      <span
                                        id={problemId}
                                        className="block text-warning text-xs mt-0.5"
                                      >
                                        no village named {unknown.join(', ')}
                                      </span>
                                    )}
                                  </>
                                )
                              })()}
                            </td>
                            <td className="text-right px-2 font-mono text-secondary">
                              {ships > 0 ? fmt(ships) : '—'}
                            </td>
                            <td className="text-center px-2">
                              <input
                                type="checkbox"
                                aria-label={`Foreign target ${i + 1} is eligible for a trade route`}
                                title="Own / Wonder / alliance-artifact village only. Unticked = manual transfer, left out of the route plan."
                                checked={Boolean(t.route_eligible)}
                                onChange={(e) => patch('route_eligible', e.target.checked)}
                              />
                            </td>
                            <td className="px-2 text-right whitespace-nowrap">
                              {incomplete && (
                                <span
                                  className="text-warning mr-2"
                                  title="Needs a name, a crop rate and a coordinate before the planner uses it"
                                >
                                  draft
                                </span>
                              )}
                              <button
                                type="button"
                                aria-label={`Remove foreign target ${i + 1}`}
                                className="text-danger hover:underline"
                                onClick={() =>
                                  setForeignTargets((prev) => prev.filter((_, j) => j !== i))
                                }
                              >
                                remove
                              </button>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </ScrollableTable>
              </>
            )}
          </div>
        </div>
      )}

      {stage === 'allocate' && villages.length > 0 && (
        <div className="space-y-4">
          {/* Build the night profile from the stores.

              Deliberately at the TOP of the allocation stage and not buried in a
              menu: it fills in the table below, so it belongs where the operator
              is about to start typing the hundred numbers it saves them. */}
          <div className="card p-4 border-l-2 border-l-indigo-400/60">
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div className="min-w-[18rem] flex-1">
                <h3 className="font-semibold">Derive an idle-window profile from your stores</h3>
                <p className="text-secondary text-xs mt-1">
                  At night nothing is spent, so everything that arrives stays and the
                  store becomes the limit — not the plan. The most a village may take
                  per hour is the room it has, divided by the hours it has to fill it:
                </p>
                <p className="text-xs mt-2 font-mono text-info">
                  (full% − empty%) × capacity ÷ hours
                </p>
                <p className="text-secondary text-xs mt-2">
                  Measured from the state <span className="text-primary">you leave behind</span>,
                  not from the snapshot. That is the difference between a profile that
                  holds for weeks and one that is stale within the hour.
                </p>
              </div>

              {/* flex-wrap: two number boxes, a separator and a
                  `whitespace-nowrap` button add up to 486px, and without
                  wrapping that pushed the whole DOCUMENT to 486 in a 375
                  viewport -- the page slid 111px sideways, which is item 1 of
                  the UI Definition of Done. */}
              <div className="flex flex-wrap items-end gap-3">
                <label className="text-xs">
                  <span className="text-secondary block mb-1">Emptied to</span>
                  <span className="flex items-baseline gap-1">
                    <input
                      type="number"
                      min="0"
                      max="90"
                      className="input-sm w-16 text-right"
                      value={baselineFill}
                      onChange={(e) => setBaselineFill(e.target.value)}
                    />
                    <span className="text-secondary">%</span>
                  </span>
                </label>
                <span className="text-secondary text-xs pb-2">→</span>
                <label className="text-xs">
                  <span className="text-secondary block mb-1">Full to</span>
                  <span className="flex items-baseline gap-1">
                    <input
                      type="number"
                      min="10"
                      max="100"
                      className="input-sm w-16 text-right"
                      value={targetFill}
                      onChange={(e) => setTargetFill(e.target.value)}
                    />
                    <span className="text-secondary">%</span>
                  </span>
                </label>
                <button
                  type="button"
                  className="btn-primary text-xs py-1.5 whitespace-nowrap"
                  disabled={deriving || !villages.length}
                  onClick={buildNightProfile}
                >
                  {deriving ? 'Deriving…' : 'Derive from stores · 0 requests'}
                </button>
              </div>
            </div>

            {/* These two figures and the full-day check's two thresholds are
                the same pair of quantities seen from either side -- "never
                overflow during the night, never arrive empty at morning" --
                so a plan derived against one pair and graded against the other
                describes a night that does not exist. They DID disagree: the
                boxes defaulted to 30% and 80% while the check measured against
                the server's own 25% and 60%. They agree now, and the boxes
                start from `DEFAULT_BASELINE_FILL` / `DEFAULT_TARGET_FILL`
                rather than from two literals, so the two cannot drift again
                unnoticed -- `plannerSetup.test.js` pins them.
                This note is still earned, because the operator may EDIT either
                box, and the pair they type is theirs. Shown only once a check
                has run, because until then there is no second pair to compare
                against -- which is also why the pin is not optional: a
                divergence in the DEFAULTS would be invisible here until
                someone asked for a full-day check. */}
            {dayCheck != null &&
              (Math.abs(Number(baselineFill) / 100 - dayCheck.pre_night_baseline) > 0.001 ||
                Math.abs(Number(targetFill) / 100 - dayCheck.morning_floor) > 0.001) && (
                <p className="text-warning text-xs mt-2">
                  These are your figures. The full-day check measures the same two switches
                  against{' '}
                  <span className="font-mono">
                    {Math.round(dayCheck.pre_night_baseline * 100)}%
                  </span>{' '}
                  and{' '}
                  <span className="font-mono">{Math.round(dayCheck.morning_floor * 100)}%</span>,
                  which is not the pair above — so a profile derived for{' '}
                  <span className="font-mono">
                    {baselineFill}% → {targetFill}%
                  </span>{' '}
                  is reported against a different night. Which pair the account should run on
                  is still open; nothing here decides it.
                </p>
              )}

            {/* The premise, stated where it cannot be missed. This is night
                arithmetic: it assumes nothing is spent, so everything that
                arrives stays. On a profile covering most of the day that is
                false -- the operator is awake and spending -- and it would cap
                inflows that the spending would have made room for. Judged on the
                WINDOW rather than the profile's name, which is theirs to rename. */}
            {profileDayShare > 0.6 ? (
              <p className="text-warning text-xs mt-2">
                This profile runs {Math.round(profileDayShare * 24)}h of the day, so you are
                awake and spending through most of it. This works out what a store can hold
                when <strong>nothing</strong> is spent — on a daytime profile it will hold
                back inflow your own spending would have made room for. Build a daytime
                profile by hand.
              </p>
            ) : null}

            {/* What it worked out for itself. A derivation whose inputs are
                invisible is one nobody can check, so the reasoning is shown
                rather than left to be trusted. */}
            {derived ? (
              <div className="mt-3 pt-3 border-t border-gray-800 text-xs space-y-1">
                <p className="text-secondary">
                  <span className="text-primary">It worked out:</span> window{' '}
                  <span className="font-mono text-info">{derived.window_hours}h</span>
                  {' · '}hub <span className="font-mono text-info">{derived.hub_name}</span>
                  {derived.consumers?.length ? (
                    <>
                      {' · '}fed{' '}
                      <span className="font-mono text-info">
                        {derived.consumers.join(', ')}
                      </span>
                    </>
                  ) : null}
                  {derived.tribute_per_hour > 0 ? (
                    <>
                      {' · '}tribute{' '}
                      <span className="font-mono text-info">
                        {fmt(derived.tribute_per_hour)}/h
                      </span>
                    </>
                  ) : null}
                </p>
                {Object.entries(derived.drawn_in || {}).some(([, v]) => v.length) ? (
                  <p className="text-secondary">
                    <span className="text-primary">Drawn on, nearest first:</span>{' '}
                    {Object.entries(derived.drawn_in)
                      .filter(([, v]) => v.length)
                      .map(([r, v]) => `${r} ${v.join(', ')}`)
                      .join('  ·  ')}
                  </p>
                ) : null}
                {Object.entries(derived.forced_senders || {}).some(([, v]) => v.length) ? (
                  <p className="text-secondary">
                    <span className="text-warning">Already past {targetFill}%, so they give:</span>{' '}
                    {Object.entries(derived.forced_senders)
                      .filter(([, v]) => v.length)
                      .map(([r, v]) => `${r} ${v.join(', ')}`)
                      .join('  ·  ')}
                  </p>
                ) : null}
                {derived.warnings?.map((w) => (
                  <p key={w} className="text-warning">
                    {w}
                  </p>
                ))}
              </div>
            ) : null}
          </div>

          <div className="flex items-center gap-2">
            {[
              ['village', 'Result by village'],
              ['edit', 'Edit by resource'],
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                aria-pressed={allocView === key}
                className={`text-xs px-3 py-1.5 rounded border transition-colors ${
                  allocView === key
                    ? 'border-violet-400/60 bg-violet-400/15 text-info'
                    : 'border-gray-700 text-secondary hover:text-primary hover:border-gray-500'
                }`}
                onClick={() => setAllocView(key)}
              >
                {label}
              </button>
            ))}
            {allocView === 'village' && (
              <span className="text-secondary text-xs ml-2">
                What each village keeps per hour once the routes run. Edit the targets in the
                other view.
              </span>
            )}
          </div>


          {/* One profile per kind of village. Its own component so a render
              test can reach it: the stage it sits in needs a snapshot, and
              renderToString runs no effects, so it never has one. */}
          <RoleTemplates
            templates={roleTemplates}
            roleCounts={roleCounts}
            missingTemplates={rolesMissingTemplates}
            focusRole={templateFocus?.role ?? null}
            focusSeq={templateFocus?.seq ?? 0}
            onAllocation={setTemplateAllocation}
            onSpend={setTemplateSpend}
            onPatch={patchTemplate}
            onClear={clearTemplate}
          />

          {allocView === 'village' && (
            <div className="card p-4">
              {/* The Allocate stage's DEFAULT view, and it overflowed its
                  container by 92px at 375 with nothing pinned and nothing
                  said -- it carried `overflow-x-auto` on the card and stopped
                  there. Read-only figures, so the stakes are a reader losing
                  their place rather than a figure landing in the wrong row,
                  but it is the same table shape and it takes the same
                  treatment. 0px at 768 and 1440, where the class is inert. */}
              <ScrollableTable>
                <table className="w-full text-xs">
                  <thead className="text-secondary uppercase">
                    <tr>
                      <th className="text-left py-1.5 px-2 sticky-col">Village</th>
                      {RESOURCES.map((resource) => (
                        <th key={resource} className="text-right px-3">
                          <span className="inline-flex items-center gap-1">
                            <ResourceIcon resource={resource} />
                            {RESOURCE_LABEL[resource]}
                          </span>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {villages.map((v) => (
                      <tr
                        key={v.village_id}
                        className="border-t border-gray-800 hover:bg-white/5 transition-colors"
                      >
                        <td className="py-1.5 px-2 whitespace-nowrap sticky-col">
                          {v.name}{' '}
                          <span className="text-secondary text-[11px]">
                            ({v.x}|{v.y})
                          </span>
                        </td>
                        {RESOURCES.map((resource) => {
                          const own = v[`${resource}_per_hour`]
                          const isRest = remainderFor(resource) === v.village_id
                          // All three lines from ONE source. The retention, the
                          // cargo, the spend and the net come off the plan once
                          // there is one, and off these inputs while there is not.
                          //
                          // Mixing the two put three contradictory numbers in one
                          // cell: the top line was this page's own derivation
                          // (KEEP → own production) while the net came from the
                          // plan, whose KEEP target INCLUDES the supplement a
                          // stock floor makes available -- so a floored village
                          // read "5,000/h ... −4,000 = 16,000 net", off by exactly
                          // the 15,000/h supplement. The cargo was derived as
                          // `target − own`, which the supplement funds, so it
                          // overstated the route as well.
                          //
                          // The spend line still prints what the PLAN used, not
                          // what was typed: the planner sets aside a declared
                          // spend whose rate it cannot read, and showing the typed
                          // figure claimed it had been applied.
                          const {
                            target: after,
                            ship,
                            spent,
                            net,
                            supplement,
                            allowance,
                          } = planCellFigures({
                            planned: planNet[resource]?.[v.village_id],
                            own,
                            localTarget: targetFor(resource, v),
                            declaredSpend: effectiveSpend(resource, v.village_id),
                          })
                          return (
                            <td key={resource} className="text-right px-3 py-1.5 align-top">
                              {/* A sign change is the story worth telling:
                                  -2,500/h own plus +4,000/h shipped IS +1,500/h,
                                  and a starving village turning surplus (or the
                                  reverse) must read as that transition, not as a
                                  bare final number. */}
                              <div
                                className={`font-mono ${
                                  after == null
                                    ? 'text-secondary'
                                    : after < 0
                                      ? 'text-danger'
                                      : ''
                                }`}
                                /* The supplement is named where there is one, or
                                   the reconciled cell still reads as a
                                   contradiction: a floored KEEP village shows a
                                   20,000/h retention against 5,000/h of own
                                   production and no cargo, and only the stock
                                   floor explains the other 15,000. */
                                /* The ceiling is named beside the draw where
                                   there is one, and never instead of it: the
                                   supplement is what the plan SPENT, and an
                                   operator reading "22,000/h" would otherwise
                                   have no way to tell a village that converted
                                   everything available from one that converted
                                   two thirds of it. */
                                title={
                                  own == null
                                    ? 'own production unknown'
                                    : supplement > 0
                                      ? `own ${fmt(own)}/h + ${fmt(supplement)}/h drawn` +
                                        (allowance > supplement
                                          ? ` of ${fmt(allowance)}/h the stock floor could convert`
                                          : ' from the stock floor')
                                      : allowance > 0
                                        ? `own ${fmt(own)}/h · the stock floor could have converted ${fmt(allowance)}/h and none was needed`
                                        : `own ${fmt(own)}/h`
                                }
                              >
                                {after == null ? (
                                  '?'
                                ) : own != null && own < 0 !== after < 0 && Math.abs(ship) >= 1 ? (
                                  <>
                                    <span className={own < 0 ? 'text-danger' : ''}>{fmt(own)}</span>
                                    <span
                                      className={
                                        after >= 0 ? 'text-success mx-0.5' : 'text-danger mx-0.5'
                                      }
                                    >
                                      {'\u2192'}
                                    </span>
                                    {`${fmt(after)}/h`}
                                  </>
                                ) : (
                                  `${fmt(after)}/h`
                                )}
                                {isRest && (
                                  <span className="ml-1 text-[10px] uppercase text-info font-sans">
                                    rest
                                  </span>
                                )}
                              </div>
                              {/* The delta is the cargo: what must arrive (+) or
                                  leave (−) to make the retention true. */}
                              <div
                                className={`text-[11px] font-mono ${
                                  ship == null || Math.abs(ship) < 1
                                    ? 'text-secondary/60'
                                    : ship > 0
                                      ? 'text-success'
                                      : 'text-warning'
                                }`}
                              >
                                {ship == null ? '—' : Math.abs(ship) < 1 ? '·' : signed(ship)}
                              </div>
                              {/* Landing − consumption = net. Level is the
                                  intended state for a role village, so it reads
                                  settled; draining is the one that kills troops,
                                  and a store that gains every hour is the one that
                                  overflows. Same three-way grammar the allocation
                                  meter uses. */}
                              {net != null && (
                                <div
                                  className={`text-[11px] font-mono ${
                                    net < -1
                                      ? 'text-danger'
                                      : net > 1
                                        ? 'text-warning'
                                        : 'text-success'
                                  }`}
                                >
                                  {`\u2212${fmt(spent)} = `}
                                  {Math.abs(net) < 1 ? '0' : signed(net)}
                                  <span className="ml-1 text-[10px] uppercase font-sans text-secondary">
                                    net
                                  </span>
                                </div>
                              )}
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr className="border-t border-gray-700 text-secondary">
                      <td className="py-1.5 px-2 uppercase text-[11px]">Account total</td>
                      {RESOURCES.map((resource) => (
                        <td key={resource} className="text-right px-3 font-mono">
                          {fmt(totals[resource].total)}/h
                          {!totals[resource].known && ' ?'}
                        </td>
                      ))}
                    </tr>
                  </tfoot>
                </table>
              </ScrollableTable>
              <p className="text-secondary text-[11px] mt-2">
                Top line: retention after distribution (red = still negative; a green arrow
                marks a village whose crop crosses from starving to surplus — e.g. −2,500/h own
                +4,000/h shipped → 1,500/h). Bottom line: what ships in (+) or out (−) to make it
                true. “rest” absorbs whatever the others leave unassigned. Third line, where a
                consumption profile is set: what the village spends, and the net its store is
                left moving at — zero means level, which is what a role village landing exactly
                what it burns should read as.
              </p>
            </div>
          )}

          {allocView === 'edit' &&
            RESOURCES.map((resource) => {
            const slack = explicitTotal(resource)
            const remainder = remainderFor(resource)
            // Sign convention: slack is production MINUS everything assigned,
            // so a NEGATIVE value means the targets promise more than the
            // account makes and the Rest village would have to ship what it
            // does not have. The backend warns about exactly that, but only at
            // the Plan stage — after the data-entry moment this widget exists
            // to protect. So over-allocation reads as an error here.
            const severity = allocationMeterSeverity(slack, remainder != null)
            const restName =
              remainder == null
                ? null
                : (villages.find((v) => v.village_id === remainder)?.name ??
                  `village ${remainder}`)
            return (
              <div key={resource} className="card p-4">
                <div className="flex justify-between items-baseline mb-2 flex-wrap gap-2">
                  <h3 className="font-semibold">{RESOURCE_LABEL[resource]}</h3>
                  <div className="flex items-center gap-3 flex-wrap">
                    <BatchSet
                      count={(selected[resource] ?? []).length}
                      onApply={(mode, value) => applyToSelected(resource, mode, value)}
                    />
                    <div className="text-xs">
                      <span className="text-secondary">
                        total {fmt(totals[resource].total)}/h
                        {!totals[resource].known && ' (some villages unknown)'} ·{' '}
                      </span>
                      <span className={METER_TONE[severity]}>
                        {/* Over-allocation is stated in words as well as in red:
                            severity is never carried by colour alone. */}
                        {severity === 'over' ? (
                          <>
                            ⚠ over-allocated by {fmt(-slack)}/h —{' '}
                            {restName
                              ? `${restName} cannot ship what the account does not produce`
                              : 'the targets ask for more than the account produces'}
                          </>
                        ) : (
                          <>
                            {fmt(slack)}/h unassigned
                            {restName ? ` → ${restName}` : ' · no remainder village set'}
                          </>
                        )}
                      </span>
                    </div>
                  </div>
                </div>
                {/* Wider than its container at 375 and 768, and not at 1440 —
                    `ScrollableTable` measures that rather than guessing. The
                    village column is pinned (see .table-overflowing
                    .sticky-col) so every field stays attributable to the right
                    village while the rest scrolls, and the hint tells the
                    operator the extra columns exist: clipping them silently is
                    how the wrong village gets edited. */}
                <ScrollableTable>
                  <table className="w-full text-xs">
                    <thead className="text-secondary uppercase">
                      <tr>
                        {/* The tick rides INSIDE the pinned identity column,
                            not in a column of its own beside it. As its own
                            column it was the one cell the pinned column
                            scrolled over: at the end of the scroll the row
                            checkbox sat at x -171..-158 in a container
                            starting at 41 (375) and at 214..227 in one
                            starting at 249 (768), so the operator could not
                            tick a row without scrolling back for it. It also
                            split the pair index.css relies on -- the edge that
                            marks the focused row was on the checkbox cell
                            while the pinning was on this one. */}
                        <th className="text-left py-1 px-2 sticky-col">
                          <span className="inline-flex items-center gap-2">
                            <input
                              type="checkbox"
                              aria-label="Select all villages"
                              checked={allSelected(resource)}
                              ref={(el) => {
                                if (el) el.indeterminate = someSelected(resource) && !allSelected(resource)
                              }}
                              onChange={() => toggleSelectAll(resource)}
                            />
                            Village
                          </span>
                        </th>
                        <th className="text-right px-2">Own/h</th>
                        <th className="text-left px-2">Mode</th>
                        <th className="text-right px-2">Value</th>
                        <th className="text-right px-2">Ship/h</th>
                        {/* The column heading AND the group's `none` option,
                            which is the state every account starts in: with no
                            `none` radio there was nothing to click, no clear,
                            and clicking the checked radio fires no change event
                            -- so the first tick was a one-way door out of a
                            state the Plan stage still reports on. In the header
                            because "no village" is not a row.

                            The resource is in the accessible name, so the four
                            tables do not offer four controls called "none", and
                            it is prefixed rather than replacing the visible word
                            so the name still CONTAINS the label on screen
                            (WCAG 2.5.3). */}
                        <th className="text-center px-2">
                          <span className="inline-flex flex-col items-center gap-0.5">
                            Rest
                            <label className="inline-flex items-center gap-1 font-normal normal-case cursor-pointer">
                              <input
                                type="radio"
                                name={`remainder-${resource}`}
                                checked={remainder == null}
                                onChange={() => setRemainder(resource, null)}
                              />
                              <span className="sr-only">
                                {`no village absorbs the remaining ${RESOURCE_LABEL[resource]}: `}
                              </span>
                              none
                            </label>
                          </span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {villages.map((v) => {
                        const own = v[`${resource}_per_hour`]
                        // Does this cell disagree with the village's role?
                        // Overriding a template is legitimate -- one of four
                        // defensive villages always has a wall going up -- but
                        // it must not be invisible: the operator reads the
                        // role's profile, the plan ships something else, and
                        // nothing says which cell. The predicate is the shared
                        // one, so the mark cannot land on a cell the backend
                        // did not report as a deviation.
                        const role = villageRoles[v.village_id]
                        const fromRole = roleTemplates[role]?.allocations?.[resource]
                        // What the row EDITS is the resolved figure, so a
                        // templated village shows its profile rather than the
                        // "Keep own" default it has no entry for. Typing over
                        // it writes the village's own entry, which is what
                        // makes the next line a deviation.
                        const deviates = roleDeviates(
                          roleTemplates[role],
                          resource,
                          allocations[resource]?.[v.village_id]
                        )
                        const deviationId = `deviates-${resource}-${v.village_id}`
                        const a = effectiveAllocation(resource, v.village_id)
                        let target = own ?? 0
                        if (a.mode === 'absolute') target = Number(a.value) || 0
                        else if (a.mode === 'percentage')
                          target = (totals[resource].total * (Number(a.value) || 0)) / 100
                        else if (a.mode === 'sustain')
                          target = own < 0 ? (-own * (Number(a.value) || 0)) / 100 : own
                        // An unreadable rate means the backend drops this
                        // allocation: promising a Ship/h figure here would be
                        // cargo the plan never routes.
                        const ship =
                          a.mode === 'remainder' || own == null ? null : target - own
                        return (
                          <tr
                            key={v.village_id}
                            className={`row-focus touch-target border-t-default hover:bg-white/5 transition-colors ${
                              isSelected(resource, v.village_id) ? 'bg-violet-400/10' : ''
                            }`}
                          >
                            <td className="py-1 px-2 sticky-col row-focus-edge">
                              <span className="inline-flex items-center gap-2">
                                <input
                                  type="checkbox"
                                  aria-label={`Select ${v.name} for batch edit`}
                                  checked={isSelected(resource, v.village_id)}
                                  onChange={() => toggleSelected(resource, v.village_id)}
                                />
                                {v.name}
                              </span>
                            </td>
                            <td className="text-right px-2 font-mono text-secondary">
                              {own == null ? '—' : signed(own)}
                            </td>
                            <td className="px-2">
                              {/* A Rest row says so, and cannot be told to say
                                  anything else. It used to read `Keep own` --
                                  `remainder` is deliberately absent from MODES,
                                  so the select fell back to the first legal
                                  value -- which is not what the plan does with
                                  the Rest village, and on a village with a role
                                  reads as a silent override of the role's own
                                  figure. Disabled rather than removed, because
                                  the column has to stay attributable to the row;
                                  `.input-field:disabled` is what makes it look
                                  disabled. The radio beside it is the control
                                  that moves it, which is where it always was. */}
                              <select
                                aria-label={`${RESOURCE_LABEL[resource]} mode for ${v.name}`}
                                className="input-field w-auto text-xs py-0.5"
                                disabled={a.mode === 'remainder'}
                                value={a.mode}
                                onChange={(e) =>
                                  setAllocation(resource, v.village_id, { mode: e.target.value })
                                }
                              >
                                {a.mode === 'remainder' ? (
                                  <option value="remainder">Rest &mdash; absorbs the slack</option>
                                ) : (
                                  MODES.map((m) => (
                                    <option key={m.value} value={m.value}>
                                      {m.label}
                                    </option>
                                  ))
                                )}
                              </select>
                            </td>
                            <td className="text-right px-2">
                              <input
                                type="number"
                                aria-label={`${RESOURCE_LABEL[resource]} value for ${v.name}`}
                                aria-describedby={deviates ? deviationId : undefined}
                                className={`input-field w-24 text-right text-xs py-0.5 ${
                                  deviates ? 'border-info' : ''
                                }`}
                                disabled={a.mode === 'keep' || a.mode === 'remainder'}
                                value={a.value ?? 0}
                                onChange={(e) =>
                                  setAllocation(resource, v.village_id, {
                                    value: Number(e.target.value),
                                  })
                                }
                              />
                              {/* Said in words and with the profile's own
                                  figure, never by the border alone: a coloured
                                  edge cannot tell the operator WHAT the role
                                  asked for, which is the only thing that makes
                                  the mark worth acting on. */}
                              {deviates && (
                                <span
                                  id={deviationId}
                                  className="block text-info text-xs mt-0.5 whitespace-nowrap"
                                >
                                  {'\u2260 '}
                                  {ROLE_LABEL[role]}:{' '}
                                  {MODES.find((m) => m.value === fromRole.mode)?.label ??
                                    fromRole.mode}{' '}
                                  {/* `?? 0` for the same reason `roleDeviates`
                                      reads it that way: an absent value IS
                                      zero to the backend, so naming it as an
                                      em-dash would print a figure the plan
                                      does not have. */}
                                  {fmt(fromRole.value ?? 0)}
                                </span>
                              )}
                            </td>
                            {/* Ship is the primary number: the cargo is the GAP,
                                not the target. Profile known issue #1. */}
                            <td
                              className={`text-right px-2 font-mono ${
                                ship == null
                                  ? 'text-secondary'
                                  : ship > 0
                                    ? 'text-success'
                                    : ship < 0
                                      ? 'text-warning'
                                      : 'text-secondary'
                              }`}
                            >
                              {ship == null ? 'rest' : signed(ship)}
                            </td>
                            <td className="text-center px-2">
                              <input
                                type="radio"
                                name={`remainder-${resource}`}
                                aria-label={`Send remaining ${RESOURCE_LABEL[resource]} to ${v.name}`}
                                checked={remainder === v.village_id}
                                onChange={() => setRemainder(resource, v.village_id)}
                              />
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </ScrollableTable>
              </div>
            )
            })}
        </div>
      )}

      {stage === 'day' && villages.length > 0 && (
        <div className="space-y-4">
          {/* Section 6 and section 7 together, because they are one question
              asked twice: which hours each profile owns, and what is true of
              the account during them. The attendance answer is EDITED here and
              nowhere else -- the bar above only states it and links back. */}
          <DayNightPanel
            profileNames={profileNames}
            activeProfile={activeProfile}
            profileWindows={profileWindows}
            profileAttendance={profileAttendance}
            profileOvernight={profileOvernight}
            attendanceRequired={attendanceIsRequired}
            reservedWindow={reservedWindow}
            onReservedWindow={setReservedWindow}
            onSelectProfile={switchProfile}
            onWindow={(name, pair) =>
              setProfileWindows((prev) => ({ ...prev, [name]: pair }))
            }
            onAttendance={(name, value) =>
              setProfileAttendance((prev) => {
                const next = { ...prev }
                // Unanswered is the ABSENCE of a key, not a stored null: the
                // map goes into the request, and a null would have to be
                // filtered out of it somewhere else instead.
                if (value == null) delete next[name]
                else next[name] = value
                return next
              })
            }
            // Same shape, and undeclared is the same absence: it is what asks
            // the backend to derive the night from the window.
            onOvernight={(name, value) =>
              setProfileOvernight((prev) => {
                const next = { ...prev }
                if (value == null) delete next[name]
                else next[name] = value
                return next
              })
            }
          />
          <FullDayCheck
            dayCheck={dayCheck}
            dayChecking={dayChecking}
            onRun={runDayCheck}
            cropCeilings={cropCeilings}
            villages={villages}
          />
        </div>
      )}

      {stage === 'plan' && (
        <div className="space-y-4">
          {!plan && (
            <div className="card p-6 text-center text-secondary">
              No plan yet — press <strong>Build plan</strong>. It costs no game requests, so
              re-planning while tuning targets is free.
            </div>
          )}

          {plan && (
            <>
              {/* ── The verdict, first and largest ──
                  The audit's answer to "is this eye-catching" was no: eleven
                  cards of identical weight, width and 11-14px type, with the
                  go/no-go answer as a 14px line in the third column of a
                  three-column strip, the largest number on the page a COST
                  three thousand pixels below the fold, and the biggest filled
                  button the YAML export -- a document that changes nothing.

                  `planStatus()` already returned the three-state label and the
                  tone; nothing here re-derives either. This is size and
                  placement. */}
              <div
                className={`plan-verdict ${
                  VERDICT_TONE_CLASS[planState.tone] ?? 'plan-verdict-dirty'
                }`}
              >
                <p className="plan-verdict-label">{planState.label}</p>
                {verdict && !verdict.clean && (
                  <p className="text-xs mt-1">
                    {verdict.executable
                      ? `${verdict.critical_findings} critical finding${
                          verdict.critical_findings === 1 ? '' : 's'
                        } this check does not weigh — read the findings below before going live`
                      : `${verdict.blockers.length} blocker${
                          verdict.blockers.length === 1 ? '' : 's'
                        }, and the server refuses a live run too`}
                  </p>
                )}
                {/* Printed ONCE, here, in the backend's own wording. The same
                    two sentences used to appear four times on one screen, and
                    every other copy is now a count that points back here. */}
                {verdict && verdict.blockers.length > 0 && (
                  <>
                    <ul className="text-xs mt-2 space-y-1">
                      {verdict.blockers.map((line) => (
                        <li key={line} className="flex items-start gap-2 flex-wrap">
                          <span className="flex-1 min-w-[16rem]">{line}</span>
                          {/* Targets, and not a destination sniffed out of the
                              sentence. Every one of the four blocker kinds --
                              a merchant budget breach, an unroutable receiver,
                              an over-claimed allocation, a conversion
                              shortfall -- is fixed by lowering a target; the
                              owned columns on Account are the other lever, and
                              the line below says so once rather than per row.
                              Matching keywords against a backend sentence to
                              choose between them is exactly the coupling this
                              codebase keeps warning about. */}
                          <button
                            type="button"
                            className="underline whitespace-nowrap pointer-coarse:min-h-11"
                            onClick={() => setStage('allocate')}
                          >
                            → fix
                          </button>
                        </li>
                      ))}
                    </ul>
                    <p className="text-xs mt-2">
                      Every one of these comes down to a target that asks for more than the
                      account can carry. Lower it on <strong>Targets</strong>, or change what the
                      village is allowed on <strong>Account</strong> — its merchant cap, its relay
                      tier, its stock floor.
                    </p>
                  </>
                )}
                {verdict && verdict.clean && (
                  <p className="text-xs mt-1">
                    Nothing critical outstanding. Preview it, then write it.
                  </p>
                )}
              </div>

              {/* ── The two figures the operator acts on after the verdict ──
                  The only other large numbers on the stage, because they are
                  the only other numbers that ask for a decision: what this plan
                  destroys per day, and how many NPC conversions it wants done
                  by hand. Both were computed and both were below the fold.
                  Neither is re-derived -- the loss is the diagnostics' own total
                  and the count is the trigger list's length -- and the panels
                  that break them down are still below. */}
              <div className="grid gap-4 md:grid-cols-2">
                <div className="card p-4">
                  <div className="text-secondary text-xs uppercase">Costs per day</div>
                  <div
                    className={`plan-headline-figure font-mono ${
                      plan.diagnostics?.total_loss_per_day > 0 ? 'text-danger' : 'text-success'
                    }`}
                  >
                    {plan.diagnostics?.total_loss_per_day > 0
                      ? fmt(plan.diagnostics.total_loss_per_day)
                      : 'nothing'}
                  </div>
                  <p className="text-secondary text-xs mt-0.5">
                    {plan.diagnostics?.total_loss_per_day > 0
                      ? 'resources destroyed per day — broken down under “What this plan costs”'
                      : 'no resource is destroyed by this plan'}
                  </p>
                </div>
                <div className="card p-4">
                  <div className="text-secondary text-xs uppercase">NPC by hand</div>
                  <div
                    className={`plan-headline-figure font-mono ${
                      plan.npc_triggers?.length ? 'text-warning' : 'text-secondary'
                    }`}
                  >
                    {plan.npc_triggers?.length ?? 0}
                  </div>
                  <p className="text-secondary text-xs mt-0.5">
                    {plan.npc_triggers?.length
                      ? 'conversion(s) the planner says are worth doing — it never presses the button'
                      : 'no conversion is worth doing right now'}
                  </p>
                </div>
              </div>

              <div className="card p-4 flex flex-wrap gap-6 items-center">
                <div>
                  <div className="text-secondary text-xs uppercase">Routes</div>
                  <div className="text-xl font-mono">{plan.rows.length}</div>
                </div>
                <div>
                  <div className="text-secondary text-xs uppercase">Merchants</div>
                  <div className="text-xl font-mono">{plan.total_merchants}</div>
                </div>
                {/* The Status column is gone: the banner above says it at 2rem,
                    and saying it twice at two sizes is what made eleven cards
                    read as one weight. */}
              </div>

              {verdict && (
                <details className="card p-4 text-xs">
                  <summary className="cursor-pointer font-semibold">
                    What “{planState.label}” checked
                  </summary>
                  <p className="text-secondary mt-2">
                    It asks one question — <em>can this sheet be carried out</em> — and it weighs
                    exactly three things:
                  </p>
                  <ul className="list-disc list-inside mt-1">
                    {verdict.covers.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                  {/* A COUNT, not the sentences. They are in the banner. */}
                  {verdict.blockers.length > 0 && (
                    <p className="text-danger mt-2">
                      {verdict.blockers.length} problem
                      {verdict.blockers.length === 1 ? '' : 's'} above, each one named in the
                      banner at the top of this stage.
                    </p>
                  )}
                  <p className="text-secondary mt-2">
                    It deliberately does <strong>not</strong> weigh what the plan leaves behind —
                    stores overflowing, a granary running dry, a tribute unpaid. Those are facts
                    about the account, and a plan that leaves them in place still runs perfectly
                    well; blocking on overflow would refuse a stockpile you meant to build.
                    {verdict.unweighed.length > 0 && (
                      <>
                        {' '}
                        Outstanding here: <strong>{verdict.unweighed.join(', ')}</strong> — read the
                        findings below before going live.
                      </>
                    )}
                  </p>
                </details>
              )}

              {/* ── What the plan LEFT BEHIND, folded ──
                  Read-only panels: they say what the plan already decided, not
                  what is still to be decided, and five of them at full weight
                  used to sit between the verdict and the write path. Each folds
                  with its count in the summary, and each opens on its own when
                  the plan is not clean -- the same rule `initialExpanded`
                  applies to the findings. */}
              <UnallocatedPanel
                rows={plan.unallocated ?? []}
                villages={villages}
                expanded={!verdict?.clean}
              />

              {/* Section 7, once the two-pass solve has sized it. Renders
                  nothing for an account that declares no floor. */}
              <NpcBalancePanel
                reserves={plan.npc_reserves ?? []}
                triggers={plan.npc_triggers ?? []}
                drawByVillage={planNpcDraw}
                expanded={!verdict?.clean}
              />

              <details className="plan-readonly card p-4" open={!verdict?.clean}>
                <summary className="cursor-pointer font-semibold">
                  Merchant budget ({overBudgetCount} village
                  {overBudgetCount === 1 ? '' : 's'} over)
                </summary>
                <div className="space-y-1 mt-3">
                  {plan.budgets
                    .filter((b) => b.committed > 0 || b.over_budget)
                    .map((b) => (
                      <div key={b.village_id} className="flex items-start gap-3 text-xs">
                        <span className="w-28 truncate shrink-0">
                          {villages.find((v) => v.village_id === b.village_id)?.name ??
                            b.village_id}
                        </span>
                        <BudgetBar budget={b} cap={maxBusy[b.village_id]} />
                      </div>
                    ))}
                </div>
              </details>

              <div className="card p-4 relative overflow-x-auto">
                <div className="flex items-baseline justify-between flex-wrap gap-2 mb-2">
                  <h3 className="font-semibold">
                    Setup sheet <span className="text-secondary">· {activeProfile}</span>
                  </h3>
                  <button
                    type="button"
                    className="btn-secondary btn-xs"
                    disabled={!plan.rows.length}
                    onClick={() =>
                      copySheetText(
                        routeSheetText(plan.rows.map(sheetRow)),
                        `${plan.rows.length} row${plan.rows.length === 1 ? '' : 's'}`
                      )
                    }
                  >
                    Copy all ({plan.rows.length}) · 0 requests
                  </button>
                </div>
                <table className="w-full text-xs">
                  <thead className="text-secondary uppercase">
                    <tr>
                      <th className="text-left py-1 px-2">From</th>
                      <th className="text-left px-2">To</th>
                      <th className="text-left px-2">Cargo per send</th>
                      <th className="text-right px-2">Cycle</th>
                      <th className="text-right px-2">Send at</th>
                      <th className="text-right px-2">Arrives</th>
                      <th className="text-right px-2">Merchants</th>
                      <th className="px-2"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.rows.map((row, i) => {
                      const sheet = sheetRow(row)
                      const relay = relayLegs.get(`${row.origin}:${row.destination}`)
                      return (
                        <tr key={i} className="border-t border-gray-800">
                          <td className="py-1 px-2">{sheet.from}</td>
                          <td className="px-2">
                            {sheet.to}
                            {relay && (
                              // Neither wording names a single partner village: one
                              // hub can collect from several origins and forward to
                              // several destinations, so "carries crop from V22"
                              // would be wrong on exactly the rows that matter most.
                              <div className="text-warning text-[10px]">
                                {relay.leg === 1
                                  ? `relay leg 1 — ${relay.relay.hub_name} forwards this on`
                                  : `relay leg 2 — forwards what arrives at ${relay.relay.hub_name}`}
                              </div>
                            )}
                          </td>
                          <td className="px-2 font-mono">
                            {/* Always all four, in the marketplace's order, zeros
                                included -- the sheet is copied into the game's
                                trade-route dialog field by field. */}
                            <span className="inline-flex items-center gap-2 whitespace-nowrap">
                              {RESOURCES.map((r) => (
                                <span key={r} className="inline-flex items-center gap-1">
                                  <ResourceIcon resource={r} />
                                  <span className={row.cargo[r] ? '' : 'text-secondary/50'}>
                                    {(row.cargo[r] ?? 0).toLocaleString()}
                                  </span>
                                </span>
                              ))}
                            </span>
                          </td>
                          <td className="text-right px-2 font-mono">{row.cycle_hours}h</td>
                          <td className="text-right px-2 font-mono">{row.dispatch}</td>
                          <td className="text-right px-2 font-mono">{row.arrival}</td>
                          <td className="text-right px-2 font-mono">{row.merchants}</td>
                          <td className="px-2 text-right">
                            <button
                              type="button"
                              className="text-info hover:underline"
                              aria-label={`Copy the route from ${sheet.from} to ${sheet.to}`}
                              onClick={() => copySheetText(routeSheetRow(sheet), 'Route')}
                            >
                              copy
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
                <p className="text-secondary text-xs mt-2">
                  Copy writes tab-separated columns with the cargo split into one column per
                  resource, unformatted — so it pastes one value per cell into a spreadsheet, and
                  the numbers still paste into the game's own fields. “Copy all” adds a header row.
                </p>
                <p className="text-secondary text-xs mt-2">
                  “Send at” is the route's scheduled send time — enter it in the trade route's
                  <span className="whitespace-nowrap"> Send at</span> field; the repeat interval is
                  set separately. It is not the wall-clock instant you must press create.
                </p>
                {relays.length > 0 && (
                  /* Folded on the same rule as the four panels above: read-only,
                     with the count in the summary. `border-gray-800` became
                     `border-t-default` while this line was being rewritten -- a
                     fixed dark grey that renders as a near-black hairline on a
                     light surface. */
                  <details
                    className="plan-readonly mt-3 border-t-default pt-3"
                    open={!verdict?.clean}
                  >
                    <summary className="cursor-pointer font-semibold text-xs">
                      Relayed crop · {relays.length} chain{relays.length === 1 ? '' : 's'}
                    </summary>
                    {/* Rewritten after the operator read it and had to ask what
                        it meant. The old copy opened with the mechanism -- how a
                        hub's granary refills -- and left the two things they
                        actually needed buried: that a chain is TWO rows and both
                        must exist, and that the big number is travel time and not
                        a gap between deliveries. It also called that number
                        "worst case", which reads as danger for a figure that is
                        simply how long the pipe takes to fill. Consequence and
                        action first; mechanism underneath for whoever wants it. */}
                    <p className="text-secondary text-xs mt-1">
                      These deliver through a middle village, because the source is too far
                      to reach the target directly.
                    </p>

                    <p className="text-xs mt-2 text-warning">
                      Each chain is <strong>two rows</strong> in the sheet above. Create both.
                    </p>
                    <p className="text-secondary text-xs">
                      The middle village ships from its own granary, and the other row is what
                      refills it. Create only the outbound one and it empties and stops.
                    </p>

                    <p className="text-secondary text-xs mt-2">
                      The times below are how long crop takes to travel the{' '}
                      <span className="text-primary">whole chain</span> — not the gap between
                      deliveries. Each route still fires on its own cycle, so expect{' '}
                      <span className="text-primary">partial deliveries until the chain fills</span>,
                      then the full rate.
                    </p>

                    <table className="w-full text-xs mt-2">
                      <thead className="text-secondary uppercase">
                        <tr>
                          <th className="text-left py-1 px-2">From</th>
                          <th
                            className="text-left px-2"
                            title="The middle village. Crop pools in its granary, so which sender's crop reaches the target is not something the plan decides — the granary is. The plan sets rates, not which grain goes where."
                          >
                            Via
                          </th>
                          <th className="text-left px-2">To</th>
                          <th className="text-right px-2" title="How long the middle village waits to be refilled">
                            Fills in
                          </th>
                          <th className="text-right px-2" title="How long the onward leg takes once it leaves the middle village">
                            Forwards in
                          </th>
                          <th
                            className="text-right px-2"
                            title="Source to target, end to end. The two legs added together, which is why it can exceed a latency target that each leg on its own meets."
                          >
                            Full journey
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {relays.map((relay) => (
                          <tr key={relay.hub} className="border-t border-gray-800">
                            <td className="py-1 px-2">{relay.origin_names.join(', ')}</td>
                            <td className="px-2">
                              <strong>{relay.hub_name}</strong>
                            </td>
                            <td className="px-2">{relay.destination_names.join(', ')}</td>
                            <td className="text-right px-2 font-mono">
                              {relay.collect_hours.toFixed(1)}h
                            </td>
                            <td className="text-right px-2 font-mono">
                              {relay.forward_hours.toFixed(1)}h
                            </td>
                            <td className="text-right px-2 font-mono font-semibold">
                              {relay.end_to_end_hours.toFixed(1)}h
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </details>
                )}
              </div>

              {/* Section 6's deadline, against THIS profile's routes. The
                  same rows the full-day check reports for the composite, so
                  one table renders both -- two would be two chances to
                  describe the same fact differently. Only ever populated for
                  an overnight profile: by day nothing says a merchant may not
                  be on the road at the switch. Folded, with the count in the
                  summary, on the same rule as every other read-only panel. */}
              {plan.night_overruns?.length > 0 && (
                <details className="plan-readonly card p-4" open={!verdict?.clean}>
                  <summary className="cursor-pointer font-semibold text-sm">
                    Still on the road at the switch ({plan.night_overruns.length} route
                    {plan.night_overruns.length === 1 ? '' : 's'})
                  </summary>
                  <div className="mt-3">
                    <NightOverrunTable
                      rows={plan.night_overruns}
                      emptyNote="Every movement closes before the window ends."
                    />
                  </div>
                </details>
              )}

              {/* ── The write path, and the only tinted card on the page ──
                  Eleven cards of identical weight, width and 11-14px type is
                  how the one action that changes a real account came to look
                  exactly like the ten that do not. `--md-error` at 1px over a
                  wash derived from `--md-error-container` -- see `.card-danger`
                  in index.css -- so the eye finds the write path first and
                  finds it once. */}
              <div className="card card-danger p-4">
                <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
                  <div>
                    <h3 className="font-semibold">Write it to the game</h3>
                    <p className="text-secondary text-xs mt-0.5">
                      Creates these routes in-game instead of retyping them. Preview costs no
                      requests. Going live disables each origin’s old routes first, then creates a
                      few at a time — a human sets routes up over days, not all at once, so the
                      rest wait for a later run.
                    </p>
                  </div>
                  <button
                    type="button"
                    className="btn-secondary text-xs py-1.5"
                    disabled={executing}
                    onClick={() => executePlan(true)}
                  >
                    {executing ? 'Working…' : 'Preview (0 requests)'}
                  </button>
                </div>

                {/* The safe first half of a profile switch. Kept above the
                    controlled run because it must happen FIRST: creating the new
                    routes while the old ones still ship is the one state worse
                    than either profile. */}
                <div className="mb-3 rounded border border-amber-700/60 bg-amber-500/5 p-2">
                  <div className="flex items-start justify-between gap-2 flex-wrap">
                    <p className="text-secondary text-xs flex-1 min-w-[16rem]">
                      <span className="text-primary font-medium">
                        Reconcile every village first.
                      </span>{' '}
                      An ordinary run only reads the villages this plan still ships
                      from, so a village the plan dropped keeps its old routes —
                      and switching profiles drops several. One surviving route
                      breaks the plan: its destination overflows while its origin
                      drains. This visits all {villages.length} villages and only
                      switches routes OFF, never on, so it is safe to stop and
                      resume. It runs in chunks of {SWEEP_VILLAGES_PER_CHUNK} with
                      a pause between them, and takes minutes, not seconds.
                    </p>
                    <div className="flex gap-2">
                      {sweeping ? (
                        <button
                          type="button"
                          className="btn-secondary text-xs py-1.5"
                          onClick={() => {
                            sweepCancel.current = true
                          }}
                        >
                          Stop after this chunk
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="btn-secondary text-xs py-1.5 whitespace-nowrap"
                          disabled={executing || !plan}
                          onClick={runReconcileSweep}
                        >
                          Reconcile all villages
                        </button>
                      )}
                    </div>
                  </div>
                  {sweepProgress ? (
                    <p className="text-xs mt-2 font-mono text-secondary">
                      chunk {sweepProgress.chunk} · {sweepProgress.swept} village(s) swept
                      {sweepProgress.outstanding?.length
                        ? ` · ${sweepProgress.outstanding.length} outstanding`
                        : ''}
                      {sweepProgress.waiting
                        ? ` · pausing ${sweepProgress.waiting}s before the next chunk`
                        : ''}
                      {sweepProgress.done ? ' · COMPLETE — nothing stale left' : ''}
                      {sweepProgress.failed ? ' · FAILED — routes may still be live' : ''}
                      {sweepProgress.problems?.length
                        ? ` · ${sweepProgress.problems.length} problem(s)`
                        : ''}
                    </p>
                  ) : null}
                </div>

                {/* Controlled run. A first live run against a real account should
                    be one chosen route, not whichever one the cap reached first.

                    Restructured, and the numbers are why. The four checkbox
                    labels measured 489, 408, 215 and 300 CHARACTERS, so the box
                    was a slab of 8pt prose with two number inputs wedged
                    BETWEEN paragraphs and three more inside a third: about 350
                    words to read before finding four checkboxes and five boxes,
                    with no label/field grid anywhere.

                    Every word is kept. The bolded lead clause of each label
                    already WAS the label, so it stays visible and the rest goes
                    behind a `?` disclosure -- the same mechanism this page uses
                    in nine other places -- because the prose carries real
                    warnings and deleting it would be the wrong fix. The five
                    inputs get a field grid above the checkboxes, which is the
                    order the questions are actually answered in: how much may
                    this run write, then which villages, then how it behaves. */}
                <div className="mb-3 rounded border-default p-2">
                  <p className="text-secondary mb-2 text-xs">
                    <strong>Controlled run.</strong> Narrow this run to specific villages — for a
                    first live test, or to retry one village after a failure. Leave the village
                    boxes empty to run the whole plan. A narrowed run is labelled as narrowed in
                    the result.
                  </p>
                  <div className="controlled-run-fields grid gap-3 md:grid-cols-2 lg:grid-cols-3 mb-3">
                    <label className="text-xs">
                      <span className="text-secondary flex items-center gap-1">
                        Routes this run
                        <Why label="Routes this run">
                          One route is one create REQUEST. 0 asks for reconcile only: read each
                          origin, disable what the plan no longer wants, and create nothing —
                          the safe first half of a profile switch. Blank falls back to{' '}
                          {MAX_ROUTES_PER_RUN}, because blank is unknown and unknown is not 0.
                        </Why>
                      </span>
                      {/* An explicit `aria-label`, because the `Why` disclosure
                          beside the words is INSIDE this label: without it the
                          computed accessible name came out "Routes this run
                          Why: Routes this run". aria-label wins over the label
                          element's contents, so the name is the field's name. */}
                      <input
                        type="number"
                        min="0"
                        aria-label="Routes this run"
                        className="input-sm w-24"
                        value={routesPerRun}
                        onChange={(e) => setRoutesPerRun(e.target.value)}
                      />
                    </label>
                    {/* The unit that actually lands in the game. A "route" is a
                        request; Travian turns it into 24/cycle daily rows, so a
                        cap of 3 routes on 1-hour cycles is 72 rows. */}
                    <label className="text-xs">
                      <span className="text-secondary flex items-center gap-1">
                        Max rows this run
                        <Why label="Max rows this run">
                          The unit you actually authorise. Travian turns one “repeat every N
                          hours” request into 24/N separate daily rows and fires every one, so
                          three routes on a one-hour cycle is seventy-two rows — and removing
                          them later means deleting each row. Blank or 0 is no limit, which is
                          what a whole-day provisioning pass wants.
                        </Why>
                      </span>
                      <input
                        type="number"
                        min="0"
                        aria-label="Max rows this run"
                        placeholder="no limit"
                        className="input-sm w-28"
                        value={maxGameRows}
                        onChange={(e) => setMaxGameRows(e.target.value)}
                      />
                    </label>
                    <label className="text-xs">
                      <span className="text-secondary flex items-center gap-1">
                        Never disable
                        <Why label="Never disable">
                          Destinations whose live routes are never switched off, however the plan
                          sees them. Village ids (<span className="font-mono">53629</span>) or
                          coordinates (<span className="font-mono">46|133</span>), comma
                          separated — coordinates because a hand-made route to a foreign target
                          has no usable village id. Without it the reconciler switches such a
                          route off, you switch it on, and the next run switches it off again.
                          Narrows only what is DISABLED, never what is created.
                        </Why>
                      </span>
                      <input
                        type="text"
                        aria-label="Never disable"
                        className="input-sm w-56"
                        placeholder="none"
                        value={protectDestinations}
                        onChange={(e) => setProtectDestinations(e.target.value)}
                      />
                      {/* Named back, exactly as the foreign-target exclusion
                          field does it. The server can only check the SHAPE
                          here -- it does not hold this account's village list --
                          so "4688" for "46|88" passes validation, protects
                          nothing, and the next run switches off the route the
                          operator was protecting. This page has the list. */}
                      {protectionMisses.length > 0 && (
                        <span className="block text-warning text-xs mt-0.5">
                          {protectionMisses.map((miss) => (
                            <span key={miss.entry} className="block">
                              no village named {miss.entry}
                              {miss.suggestion ? ` — did you mean ${miss.suggestion}?` : ''}
                            </span>
                          ))}
                        </span>
                      )}
                    </label>
                    <label className="text-xs">
                      <span className="text-secondary block">Only origin (village id)</span>
                      <input
                        type="number"
                        className="input-sm w-32"
                        placeholder="any"
                        value={onlyOrigin}
                        onChange={(e) => setOnlyOrigin(e.target.value)}
                      />
                    </label>
                    <label className="text-xs">
                      <span className="text-secondary block">Only destination (village id)</span>
                      <input
                        type="number"
                        className="input-sm w-32"
                        placeholder="any"
                        value={onlyDestination}
                        onChange={(e) => setOnlyDestination(e.target.value)}
                      />
                    </label>
                  </div>

                  {/* How the run behaves. One line each, with the paragraph the
                      line used to carry kept behind its `?`. */}
                  <div className="space-y-2 border-t-default pt-2">
                    <div className="flex items-start gap-2">
                      <label className="text-xs flex items-start gap-2 flex-1">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={wholeDay}
                          onChange={(e) => setWholeDay(e.target.checked)}
                        />
                        <span className="text-primary">Whole day — execute all profiles at once</span>
                      </label>
                      <Why label="Whole day">
                        Plans every profile in its own hours and creates both route sets in one
                        pass, reconciled together: a Night row is never &ldquo;stale&rdquo; to a
                        Day-eyed run. Both sets then coexist in the game — disjoint by departure
                        time — so the account runs around the clock with{' '}
                        <strong>no daily switching</strong>. The reconcile sweep also{' '}
                        <em>creates</em> as it goes in this mode, so one pass over the villages
                        provisions the whole day. Needs hours on every profile; the trim below is
                        forced on.
                      </Why>
                    </div>

                    <div className="flex items-start gap-2">
                      <label className="text-xs flex items-start gap-2 flex-1">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={pruneToWindow || wholeDay}
                          disabled={wholeDay}
                          onChange={(e) => setPruneToWindow(e.target.checked)}
                        />
                        <span className="text-primary">
                          Trim the fan-out to the profile hours
                        </span>
                      </label>
                      <Why label="Trim the fan-out to the profile hours">
                        Travian has no setting that confines a route to part of the day:
                        &ldquo;repeat every N hours&rdquo; becomes 24/N daily rows and every one
                        of them fires. This deletes the rows departing outside the profile hours
                        after the route is created, which is what makes the window real — and
                        cuts the row footprint to the share of the day it covers. Untick only for
                        a round-the-clock profile.
                      </Why>
                    </div>

                    <div className="flex items-start gap-2">
                      <label className="text-xs flex items-start gap-2 flex-1">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={disableExisting}
                          onChange={(e) => setDisableExisting(e.target.checked)}
                        />
                        <span className="text-primary">
                          Also disable routes the plan no longer wants
                        </span>
                      </label>
                      <Why label="Also disable routes the plan no longer wants">
                        Untick for a create-only run — then the only thing it changes in the game
                        is the route it creates. Leave ticked for normal use, or old routes pile
                        up alongside new ones.
                      </Why>
                    </div>

                    <div className="flex items-start gap-2">
                      <label className="text-xs flex items-start gap-2 flex-1">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={updateDrifted}
                          onChange={(e) => setUpdateDrifted(e.target.checked)}
                        />
                        <span className="text-primary">
                          Correct cargo on routes that have drifted
                        </span>
                      </label>
                      <Why label="Correct cargo on routes that have drifted">
                        A route is created once, but the plan moves every time production does.
                        Without this, live routes keep the amounts they were created with and
                        slowly stop matching the sheet. Off by default because it overwrites a
                        route you may have tuned in-game on purpose.
                      </Why>
                    </div>
                  </div>
                </div>

                {!plan.feasible && (
                  <div className="text-warning text-xs mb-2">
                    <p>
                      <strong>Going live is blocked</strong> until this is resolved, and the server
                      refuses it too. Preview is not blocked: it changes nothing, costs no
                      requests, and naming the routes that break is how you resolve it.
                    </p>
                    {/* The COUNT, and a pointer. The sentences are in the
                        verdict banner at the top of this stage -- printed
                        there once, in the backend's own wording, with a way to
                        fix each. They used to be printed here as well, and in
                        the "what this checked" disclosure, and restated twice
                        more: the same two sentences four times on one screen. */}
                    {verdict && verdict.blockers.length > 0 && (
                      <p className="mt-1">
                        {verdict.blockers.length} problem
                        {verdict.blockers.length === 1 ? '' : 's'} above, each one named in the
                        banner at the top of this stage.
                      </p>
                    )}
                  </div>
                )}

                {execResult && (
                  <>
                    <p className="text-xs mb-2">
                      {execResult.dry_run
                        ? `Preview: ${
                            execResult.actions.filter((a) => a.status === 'would_create').length
                          } route(s) would be created` +
                          (execResult.remaining
                            ? `, ${execResult.remaining} deferred to a later run.`
                            : '.')
                        : // `created` counts only CONFIRMED creates, so on its own
                          // it read "Created 0 route(s)" above a problem list
                          // saying three routes had just been written — a
                          // headline refuting its own detail. The other two
                          // outcomes the server distinguishes are stated here
                          // instead of left to be inferred from the prose.
                          `Created ${execResult.created} route(s)` +
                          (execResult.created_unverified
                            ? `, ${execResult.created_unverified} written but UNCONFIRMED`
                            : '') +
                          (execResult.not_created
                            ? `, ${execResult.not_created} accepted by the game but never appeared`
                            : '') +
                          (execResult.remaining
                            ? `, ${execResult.remaining} deferred to a later run.`
                            : '.')}
                    </p>
                    {execResult.updates?.length > 0 && (
                      <div className="text-xs mb-2">
                        <strong>Cargo corrected on {execResult.updates.length} route(s):</strong>
                        <ul className="list-disc ml-5 mt-1">
                          {execResult.updates.map((u) => (
                            <li key={u}>{u}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {execResult.filtered_to && (
                      <p className="text-warning text-xs mb-2">
                        <strong>Narrowed run:</strong> {execResult.filtered_to}
                      </p>
                    )}
                    {/* Also shown when the measured count is zero but routes were
                        written unconfirmed: the old `created_game_rows > 0` gate
                        hid this line entirely on exactly the run where the row
                        footprint is least certain. */}
                    {(execResult.created_game_rows > 0 ||
                      execResult.created_unverified > 0) && (
                      <p className="text-xs mb-2">
                        <strong>
                          {execResult.dry_run ? 'Would put' : 'Put'}{' '}
                          {execResult.created_game_rows} route row(s) in the game
                        </strong>{' '}
                        — Travian turns one “repeat every N hours” request into 24/N separate
                        daily rows, so a request is not a row. Removing them later means deleting
                        every row.{' '}
                        {execResult.dry_run
                          ? 'This number is a forecast — 24/N per request, before anything is written.'
                          : 'This number was measured: the marketplace was re-read and these are the rows that actually appeared.'}
                        {!execResult.dry_run && execResult.created_unverified > 0 && (
                          <>
                            {' '}
                            <strong className="text-warning">
                              {execResult.created_unverified} further route(s) were written but
                              could not be re-read, so their rows are unmeasured and are NOT
                              included above.
                            </strong>
                          </>
                        )}
                      </p>
                    )}
                    {execResult.dry_run &&
                      execResult.requests_forecast?.estimated_total > 0 && (
                        <p className="text-xs mb-2">
                          <strong>
                            Going live spends ~{execResult.requests_forecast.estimated_total}
                            {'–'}
                            {execResult.requests_forecast.estimated_total_max} requests
                          </strong>{' '}
                          — {execResult.requests_forecast.marketplace_reads} read(s),{' '}
                          {execResult.requests_forecast.creates} create(s),{' '}
                          {execResult.requests_forecast.verify_reads} verify read(s),{' '}
                          {execResult.requests_forecast.trim_deletes} trim(s) — plus up to one
                          batched disable per village, decided by what the marketplace holds.
                        </p>
                      )}
                    {execResult.dry_run && (
                      <p className="text-xs text-secondary mb-2">
                        Preview assumes an empty marketplace. The live run reads each village
                        first and only creates routes that are missing (disabling ones the plan no
                        longer wants), so it may create fewer than shown.
                      </p>
                    )}
                    {!execResult.dry_run &&
                      execResult.remaining > 0 &&
                      (!execResult.problems || execResult.problems.length === 0) &&
                      !execResult.actions.some((a) => a.status === 'failed') && (
                        <p className="text-xs text-secondary mb-2">
                          Deferred routes were not checked this run (a few villages are handled per
                          run). Run again to continue — already-active routes are skipped.
                        </p>
                      )}
                    {execResult.problems && execResult.problems.length > 0 && (
                      <ul className="text-xs text-danger list-disc list-inside mb-2">
                        {execResult.problems.map((p, i) => (
                          <li key={i}>{p}</li>
                        ))}
                      </ul>
                    )}
                    {execResult.disables.length > 0 && (
                      <ul className="text-xs text-secondary list-disc list-inside mb-2">
                        {execResult.disables.map((d, i) => (
                          <li key={i}>{d}</li>
                        ))}
                      </ul>
                    )}
                    {/* Grouped by sentence shape, not dumped: a whole-day
                        preview emits hundreds of per-route/per-store lines that
                        differ only in their numbers, and a wall that long stops
                        being read at all. Every original line stays reachable
                        inside its group. */}
                    {execResult.warnings.length > 0 && (
                      <div className="text-xs text-secondary mb-2 space-y-1">
                        {groupWarnings(execResult.warnings).map((g) =>
                          g.count === 1 ? (
                            <p key={g.signature} className="list-item list-disc list-inside">
                              {g.lines[0]}
                            </p>
                          ) : (
                            <details key={g.signature}>
                              <summary className="cursor-pointer">
                                <span className="font-mono">{g.count}×</span> {g.lines[0]}{' '}
                                <span className="text-primary">(show all {g.count})</span>
                              </summary>
                              <ul className="list-disc list-inside ml-4 mt-1">
                                {g.lines.map((w, i) => (
                                  <li key={i}>{w}</li>
                                ))}
                              </ul>
                            </details>
                          )
                        )}
                      </div>
                    )}
                    <table className="w-full text-xs">
                      <thead className="text-secondary uppercase">
                        <tr>
                          <th className="text-left py-1 px-2">From → To</th>
                          <th className="text-right px-2">Cycle</th>
                          <th className="text-right px-2">Merchants</th>
                          <th className="text-left px-2">Status</th>
                          {/* Detail is a real column, not a hover-only `title`:
                              tooltips are unreachable on touch, awkward for
                              keyboard users, and invisible to screen readers. */}
                          <th className="text-left px-2">Detail</th>
                        </tr>
                      </thead>
                      <tbody>
                        {/* Deferred rows dominate a capped preview -- 93 of
                            94 on the first whole-day chunk -- and they all say
                            the same thing. They collapse to one line below the
                            table; everything the run acted on stays a row. */}
                        {execResult.actions.filter((a) => a.status !== 'deferred').map((a, i) => (
                          <tr key={i} className="border-t border-gray-800">
                            <td className="py-1 px-2">
                              {a.origin_name} → {a.destination_name}{' '}
                              <span className="text-secondary">
                                ({a.dest_x}|{a.dest_y})
                              </span>
                            </td>
                            <td className="text-right px-2 font-mono">{a.cycle_hours}h</td>
                            <td className="text-right px-2 font-mono">{a.merchants}</td>
                            <td
                              className={`px-2 ${
                                a.status === 'created'
                                  ? 'text-success'
                                  : a.status === 'failed' || a.status === 'blocked'
                                    ? 'text-danger'
                                    : a.status === 'deferred' || a.status === 'skipped'
                                      ? 'text-secondary'
                                      : 'text-info'
                              }`}
                            >
                              {/* Glyph + word, so outcome is never colour-only. */}
                              {a.status === 'created'
                                ? '✓ '
                                : a.status === 'failed' || a.status === 'blocked'
                                  ? '✕ '
                                  : ''}
                              {a.status.replace('_', ' ')}
                            </td>
                            <td className="px-2 text-secondary">{a.detail || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {execResult.actions.some((a) => a.status === 'deferred') && (
                      <details className="text-xs text-secondary mt-1 mb-2">
                        <summary className="cursor-pointer">
                          {execResult.actions.filter((a) => a.status === 'deferred').length}{' '}
                          route(s) deferred to a later run — the per-run caps leave them for
                          the next chunk. <span className="text-primary">Show them</span>
                        </summary>
                        <ul className="list-disc list-inside ml-4 mt-1 font-mono">
                          {execResult.actions
                            .filter((a) => a.status === 'deferred')
                            .map((a, i) => (
                              <li key={i}>
                                {a.origin_name} → {a.destination_name} · {a.cycle_hours}h ·{' '}
                                {a.merchants} merchant(s){a.segment ? ` · ${a.segment}` : ''}
                              </li>
                            ))}
                        </ul>
                      </details>
                    )}

                    {execResult.dry_run &&
                      (execResult.live_enabled ? (
                        <>
                          {/* The confirmation must name every state-changing
                              effect, not just creation: the request carries
                              disable_existing, so stale routes are switched off
                              first and a partial failure can leave them off with
                              replacements missing (issue #67). */}
                          {/* The strict tier, surfaced exactly where a write is
                              about to happen. The BUILD gate is generous (rates
                              and capacities move over days); this one is not,
                              because free merchants and stocks decide whether
                              the plan can be staffed at all. */}
                          {snapshotStaleForLive && (
                            <p className="text-danger text-xs mt-3">
                              ⚠ This snapshot is {snapshotAgeLabel} — old enough that free
                              merchants and stocks have probably moved. The plan may commit
                              merchants that are no longer home.{' '}
                              <button
                                className="underline"
                                disabled={fetching}
                                onClick={fetchSnapshot}
                              >
                                {fetching ? 'Fetching…' : 'Fetch fresh state first'}
                              </button>
                            </p>
                          )}
                          <p className="text-warning text-xs mt-3">
                            ⚠ Going live will first <strong>disable existing routes this plan no
                            longer wants</strong> on up to {plannedOriginCount} origin village
                            {plannedOriginCount === 1 ? '' : 's'} it visits, then create up to{' '}
                            {plannedCreateCount} route{plannedCreateCount === 1 ? '' : 's'}
                            {execResult.remaining
                              ? `, leaving ${execResult.remaining} deferred to a later run`
                              : ''}
                            .{' '}
                            {disableExisting
                              ? 'If creation fails after a disable, old routes can stay off without their replacements — re-run to reconcile.'
                              : 'Create-only: nothing existing is switched off, so a failed create leaves the game exactly as it was.'}
                          </p>
                          {/* The gate Preview no longer carries lands here, where
                              it belongs: this is the irreversible branch, and the
                              backend refuses an infeasible live run anyway. */}
                          {!plan.feasible && (
                            <p className="text-danger text-xs mt-2">
                              ⚠ Blocked while this plan is not feasible. Fix the over-budget /
                              unroutable rows above, build the plan again, then preview.
                            </p>
                          )}
                          {/* `btn-danger btn-full`, and that is the point of
                              the whole rearrangement: the biggest filled button
                              on this page was the YAML export -- a document
                              that changes nothing -- while the button that
                              writes to a real account was a small one further
                              down. The `~N requests` estimate stays: every
                              action here states its cost before spending it. */}
                          <button
                            type="button"
                            className="btn-danger btn-full text-xs py-1.5 mt-2"
                            disabled={executing || !plan.feasible}
                            onClick={() => setConfirmLive(true)}
                          >
                            {executing
                              ? 'Working…'
                              : disableExisting
                                ? `Disable old routes & create ${plannedCreateCount} (~${liveRequestEstimate} requests)`
                                : `Create ${plannedCreateCount} route${plannedCreateCount === 1 ? '' : 's'}, disable nothing (~${liveRequestEstimate} requests)`}
                          </button>
                        </>
                      ) : (
                        <p className="text-warning text-xs mt-3">
                          Live creation is turned off on this server. The trade-route request
                          format is verified against a real captured request — this is a
                          deliberate opt-in, because creating routes changes your real account.
                          Set <code>TRAVIAN_TRADE_ROUTE_LIVE=true</code> on the server to enable
                          it. Preview works today and never touches the game.
                        </p>
                      ))}
                  </>
                )}
              </div>

              {plan.shortfalls.length > 0 && (
                <div className="card p-4">
                  <h3 className="font-semibold mb-2 text-danger">Unroutable demand</h3>
                  <ul className="text-xs list-disc list-inside space-y-0.5">
                    {plan.shortfalls.map((s, i) => (
                      <li key={i}>
                        {RESOURCE_LABEL[s.resource]} ·{' '}
                        {s.village_name ||
                          (villages.find((v) => v.village_id === s.village_id)?.name ??
                            `village ${s.village_id}`)}{' '}
                        short{' '}
                        {fmt(s.per_hour)}/h — {s.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {plan.diagnostics && plan.warnings.length > 0 && (
                <PlanDiagnostics diagnostics={plan.diagnostics} lineCount={plan.warnings.length} />
              )}

              {/* ── The document, demoted to the bottom ──
                  Section 10's order is unchanged and is still enforced by the
                  digest: read the plan, confirm it, get the file. What changed
                  is its WEIGHT. Its "Confirm this plan and export YAML" was the
                  biggest filled button on the page, for an artefact that
                  changes nothing in the game, sitting above the small button
                  that writes to a real account. So it folds, with the digest in
                  the summary -- which is where the digest is most useful
                  anyway: twelve characters that name which of three downloads
                  describes which plan. */}
              <details className="card p-4">
                <summary className="cursor-pointer font-semibold">
                  Export this plan as YAML{' '}
                  <span className="font-mono text-info font-normal" title={plan.plan_digest}>
                    plan {planDigestShort(plan.plan_digest)}
                  </span>{' '}
                  <span className="text-secondary text-xs font-normal">
                    — a document. It changes nothing in the game.
                  </span>
                </summary>
                <div className="mt-3">
                  <PlanExport
                    digest={plan.plan_digest}
                    exporting={exportingYaml}
                    conflict={yamlConflict}
                    onConfirm={exportPlanYaml}
                    onRePlan={buildPlan}
                  />
                </div>
              </details>
            </>
          )}
        </div>
      )}

      {/* The write history and the record of the last write, MOVED to the
          bottom. They sit outside the stage gates -- an operator opens them
          when they are not planning at all, just checking the thing still
          works -- but "outside the gates" used to mean ABOVE the Plan stage,
          and the Plan stage is the one that ends in a live write. Measured at
          1440 before this: "Run history -- what previous live runs wrote (0
          requests) / No live run has been recorded yet on this machine." was
          line 68 of the rendered text and "ROUTES / 3" was line 72, so the
          least useful panel on the page -- empty on a fresh machine -- held the
          most valuable slot on its most consequential stage. Below the four
          stages they are last on all four, which is where they already sat on
          the other three.

          Both, not just the history: they are one pair, a write history and
          the record of the last write, and B2 put the undo in the second.
          Leaving that above the go/no-go verdict would be the same defect with
          a different panel in it. */}
      {/* What the automation has WRITTEN, from the app's own traces. Sits
          outside the stage gates for the same reason the last-run card does:
          this is the panel an operator opens when they are not planning at all,
          just checking the thing still works. Deliberately a write history --
          traces cannot say what the game shipped afterwards. */}
      <div className="card p-3 mb-4">
        <details
          className="text-xs"
          onToggle={(e) => {
            if (e.currentTarget.open && !runHistory && !historyLoading) loadRunHistory()
          }}
        >
          <summary className="cursor-pointer text-secondary">
            Run history — what previous live runs wrote{' '}
            <span className="text-primary">(0 requests)</span>
          </summary>
          {historyLoading && <p className="text-secondary mt-2">Reading traces…</p>}
          {runHistory && runHistory.runs.length === 0 && (
            <p className="text-secondary mt-2">
              No live run has been recorded yet on this machine.
            </p>
          )}
          {runHistory && runHistory.runs.length > 0 && (
            <div className="mt-2 space-y-2">
              <p className="text-secondary">
                <strong className="text-primary">
                  {runHistory.rollup.runs} run(s) · {runHistory.rollup.total_created} route(s)
                  created
                </strong>
                {runHistory.rollup.total_problems > 0 &&
                  ` · ${runHistory.rollup.total_problems} problem(s)`}
                {runHistory.rollup.total_created_unverified > 0 && (
                  <span className="text-warning">
                    {' '}
                    · {runHistory.rollup.total_created_unverified} unverified
                  </span>
                )}
                {runHistory.rollup.failed_runs > 0 && (
                  <span className="text-danger"> · {runHistory.rollup.failed_runs} failed</span>
                )}
              </p>
              {runHistory.rollup.repeat_problem_villages.length > 0 && (
                <p className="text-warning">
                  Villages that hit a schedule mismatch in more than one run:{' '}
                  {runHistory.rollup.repeat_problem_villages
                    .map((v) => `${namesForVillageIds([v.village_id], villages) || v.village_id} (${v.runs}×)`)
                    .join(', ')}{' '}
                  — a destination whose live schedule keeps diverging is worth a look in the
                  game.
                </p>
              )}
              <table className="w-full">
                <thead className="text-secondary uppercase">
                  <tr>
                    <th className="text-left py-1 pr-2">When</th>
                    <th className="text-right px-2">Created</th>
                    <th className="text-right px-2">Rows</th>
                    <th className="text-right px-2">Disabled</th>
                    <th className="text-left px-2">Result</th>
                    <th className="text-left px-2">Undo</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {runHistory.runs.map((r) => (
                    <tr key={r.run_id} className="border-t-default">
                      <td className="py-1 pr-2">{new Date(r.started_at).toLocaleString()}</td>
                      <td className="text-right px-2">{r.created ?? '—'}</td>
                      <td className="text-right px-2">{r.created_game_rows ?? '—'}</td>
                      <td className="text-right px-2">{r.disabled ?? '—'}</td>
                      <td
                        className={`px-2 ${
                          r.failed || r.needs_attention ? 'text-warning' : 'text-secondary'
                        }`}
                      >
                        {r.failed
                          ? `failed — ${r.error ?? 'no reason recorded'}`
                          : !r.complete
                            ? 'incomplete — the run did not finish writing its trace'
                            : r.needs_attention
                              ? [
                                  r.created_unverified
                                    ? `${r.created_unverified} unverified`
                                    : null,
                                  r.verify_failures ? `${r.verify_failures} verify failure(s)` : null,
                                  r.problems ? `${r.problems} problem(s)` : null,
                                  r.gold_club_blocked ? 'Gold Club refused' : null,
                                  r.stopped_early ? 'stopped early' : null,
                                ]
                                  .filter(Boolean)
                                  .join(' · ')
                              : 'clean'}
                      </td>
                      {/* `run_id` IS the trace id `/routes/revert-plan` takes,
                          so the undo is not limited to whichever run this
                          browser happens to have recorded in localStorage. */}
                      <td className="px-2">
                        <button
                          type="button"
                          className="btn-secondary btn-xs"
                          onClick={() => setRevertRun(r.run_id)}
                        >
                          Undo this run
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {revertRun && (
                <div className="card-danger rounded-xl p-2">
                  <p className="text-xs font-semibold">
                    Undoing the run of{' '}
                    {new Date(
                      runHistory.runs.find((r) => r.run_id === revertRun)?.started_at ?? Date.now()
                    ).toLocaleString()}{' '}
                    <span className="font-mono text-secondary">({revertRun})</span>
                  </p>
                  <RevertRunPanel
                    traceId={revertRun}
                    state={revert}
                    villages={villages}
                    onCheck={(id) => requestRevert(id, false, false)}
                    onDisable={(id) => setConfirmRevert({ traceId: id, deleting: false })}
                    onDelete={(id) => setConfirmRevert({ traceId: id, deleting: true })}
                  />
                </div>
              )}
              <button className="btn-secondary btn-xs" onClick={loadRunHistory}>
                Refresh (0 requests)
              </button>
            </div>
          )}
        </details>
      </div>

      {/* Durable audit of the last LIVE run, rendered OUTSIDE the stage/plan
          gates on purpose: `plan` is cleared by any input edit and `stage`
          resets to 'snapshot' on reload, so nesting this inside them would hide
          the record in exactly the two situations it is persisted for. */}
      {lastRun && (
        <div className="card p-3 mb-4">
          <details className="text-xs">
            <summary className="cursor-pointer text-secondary">
              Last live trade-route run — {new Date(lastRun.at).toLocaleString()} ·{' '}
              {lastRun.created} created
              {lastRun.problems.length ? ` · ${lastRun.problems.length} problem(s)` : ''}
            </summary>
            <div className="mt-2 space-y-1">
              {lastRun.problems.length > 0 && (
                <ul className="text-danger list-disc list-inside">
                  {lastRun.problems.map((p, i) => (
                    <li key={i}>{p}</li>
                  ))}
                </ul>
              )}
              {lastRun.disables.length > 0 && (
                <ul className="text-secondary list-disc list-inside">
                  {lastRun.disables.map((d, i) => (
                    <li key={i}>{d}</li>
                  ))}
                </ul>
              )}
              <ul className="list-disc list-inside">
                {lastRun.routes.map((r, i) => (
                  <li key={i}>
                    {r.from} → {r.to} ({r.at}): <strong>{r.status}</strong>
                    {r.detail ? ` — ${r.detail}` : ''}
                  </li>
                ))}
              </ul>
              {/* The way back. `/routes/revert-plan` has existed all along and
                  was unreachable, because the one handle it accepts -- this
                  run's trace id -- was dropped on the floor. */}
              {lastRun.traceId ? (
                <details className="mt-3 border-t-default pt-2">
                  <summary className="cursor-pointer text-primary">
                    Undo the last live run
                  </summary>
                  <div className="mt-2">
                    <RevertRunPanel
                      traceId={lastRun.traceId}
                      state={revert}
                      villages={villages}
                      onCheck={(id) => requestRevert(id, false, false)}
                      onDisable={(id) => setConfirmRevert({ traceId: id, deleting: false })}
                      onDelete={(id) => setConfirmRevert({ traceId: id, deleting: true })}
                    />
                  </div>
                </details>
              ) : (
                <p className="text-secondary mt-3 border-t-default pt-2">
                  This run was recorded before this build kept the run&apos;s trace id, so there
                  is nothing to undo it with. The trace id is what
                  <span className="font-mono"> /routes/revert-plan </span>
                  reads the pre-run inventory by, and the game returns no id when it creates a
                  route — so without it there is no record of what each village held before.
                  Later runs carry it.
                </p>
              )}
            </div>
          </details>
        </div>
      )}

      {/* Every question this page asks before it changes something, in the app.
          Rendered outside the stage gates because two of them are asked from
          the global profile bar, which is itself outside them. */}
      <ConfirmDialog
        open={pendingSetup != null}
        title="This setup was saved from a different account"
        message={
          <>
            <p>
              <span className="font-mono">{pendingSetup?.where}</span>:{' '}
              <strong className="text-primary">{pendingSetup?.setup?.account}</strong>
            </p>
            <p>
              current: <strong className="text-primary">{accountKey}</strong>
            </p>
            <p className="mt-3 text-danger">
              Village ids are per-account, so loading it can attach the wrong Trade Office levels
              to the wrong villages.
            </p>
          </>
        }
        confirmText="Load it anyway"
        cancelText="Cancel"
        variant="danger"
        onConfirm={() => {
          const pending = pendingSetup
          setPendingSetup(null)
          if (pending) mergeParsedSetup(pending.setup, pending.where)
        }}
        onCancel={() => setPendingSetup(null)}
      />

      <ConfirmDialog
        open={confirmForget}
        title="Delete the setup saved on the server?"
        message="What is on screen stays, and so does any file you exported. Only the shared copy every origin reads is removed."
        confirmText="Delete it"
        cancelText="Keep it"
        variant="danger"
        onConfirm={() => {
          setConfirmForget(false)
          forgetServerSetup()
        }}
        onCancel={() => setConfirmForget(false)}
      />

      <ConfirmDialog
        open={confirmDeleteProfile != null}
        title={`Delete profile "${confirmDeleteProfile}"?`}
        message="Its allocations, hours, attendance answer and overnight declaration go with it. Nothing in the game changes — this is the plan, not the routes."
        confirmText="Delete"
        cancelText="Keep it"
        variant="danger"
        onConfirm={() => {
          setConfirmDeleteProfile(null)
          deleteProfile()
        }}
        onCancel={() => setConfirmDeleteProfile(null)}
      />

      <ConfirmDialog
        open={profileNaming != null}
        title={PROFILE_NAMING_TITLE[profileNaming?.mode] ?? 'Name this profile'}
        message={
          <label className="block">
            <span className="block mb-1">Profile name</span>
            <input
              type="text"
              className="input-field"
              aria-label="Profile name"
              value={profileNaming?.value ?? ''}
              onChange={(e) => setProfileNaming((p) => (p ? { ...p, value: e.target.value } : p))}
              onKeyDown={(e) => {
                // Enter is what a native prompt did, and the dialog is not a
                // <form>, so nothing else would submit it.
                if (e.key === 'Enter') {
                  e.preventDefault()
                  commitProfileName()
                }
              }}
            />
          </label>
        }
        confirmText={PROFILE_NAMING_ACTION[profileNaming?.mode] ?? 'Confirm'}
        cancelText="Cancel"
        onConfirm={commitProfileName}
        onCancel={() => setProfileNaming(null)}
      />

      <ConfirmDialog
        open={confirmRevert != null}
        title={
          confirmRevert?.deleting
            ? 'Delete the routes this run created'
            : 'Disable the routes this run created'
        }
        message={
          confirmRevert?.deleting ? (
            <>
              <p className="text-danger">
                This is the one irreversible action here. A disabled route can be switched back
                on; a deleted one cannot.
              </p>
              <p className="mt-3">
                They are disabled first regardless, so they stop shipping even if the removal then
                fails. Every step is confirmed by re-reading the page, and anything still standing
                afterwards is named.
              </p>
            </>
          ) : (
            <>
              <p>
                The routes this run created stop shipping. Reversible — a disabled route can be
                switched back on in the game, and this leaves the rows there to look at.
              </p>
              <p className="mt-3 text-secondary">
                They still need deleting afterwards, by you or by the delete button, and the
                outstanding rows are named once this finishes.
              </p>
            </>
          )
        }
        confirmText={confirmRevert?.deleting ? 'Delete them' : 'Disable them'}
        cancelText="Not yet"
        variant="danger"
        onConfirm={() => {
          const asked = confirmRevert
          setConfirmRevert(null)
          if (asked) requestRevert(asked.traceId, true, asked.deleting)
        }}
        onCancel={() => setConfirmRevert(null)}
      />

      <ConfirmDialog
        open={confirmLive}
        title="Write these routes to the game"
        message={liveManifest}
        confirmText={`Go live (~${liveRequestEstimate} requests)`}
        cancelText="Not yet"
        variant="danger"
        onConfirm={() => {
          setConfirmLive(false)
          executePlan(false)
        }}
        onCancel={() => setConfirmLive(false)}
      />
    </div>
  )
}
