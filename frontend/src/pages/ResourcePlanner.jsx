import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import PlanDiagnostics from '../components/PlanDiagnostics'
import { useToast } from '../components/Toast'
import useGameStore from '../stores/gameStore'
import useLogStore from '../stores/logStore'
import api from '../api'
import {
  SetupFileError,
  buildSetup,
  isStockFloorFraction,
  mergeSetup,
  parseSetup,
  setupFilename,
  setupMatchesAccount,
} from '../utils/plannerSetup'
import { METER_TONE, allocationMeterSeverity } from '../utils/plannerAllocation'
import { excludedOriginIds, namesForVillageIds, resolveVillageNames } from '../utils/villageRefs'
import { planStatus, relayLegIndex } from '../utils/plannerFindings'
import { routeSheetRow, routeSheetText } from '../utils/plannerSheet'
import { groupWarnings } from '../utils/warningGroups'
import {
  filterVillages,
  nextSort,
  sortVillages,
  summariseSnapshot,
} from '../utils/snapshotSummary'
import { copyToClipboard } from '../utils/clipboard'

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
// Villages a single reconciliation chunk visits. Two paced reads each, plus a
// disable and its verifying re-read where there is something stale, lands a
// chunk of five at roughly 40-70 seconds — comfortably inside one request, which
// is the whole reason the sweep is chunked at all.
const SWEEP_VILLAGES_PER_CHUNK = 5
const MINUTES_IN_DAY = 1440
// Travian's repeat interval is a closed set of the divisors of 24. Offering
// anything else would plan a cadence the create payload cannot express.
const TRAVIAN_REPEAT_INTERVALS = [1, 2, 3, 4, 6, 8, 12, 24]
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
const DEFAULT_MERCHANT_MODEL = { base_capacity: 2500, bonus_per_to_level: 0.2 }

const LS_WINDOWS = 'planner_profile_windows'
const LS_CROP_CEILING = 'planner_crop_ceiling'
const LS_SHIP_ONLY_TO = 'planner_ship_only_to'
const LS_STOCK_FLOOR = 'planner_stock_floor'
// Sensible defaults by convention; anything else starts unset until the
// operator gives it hours.
const DEFAULT_WINDOWS = { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] }
const hhmmToMinutes = (t) => {
  const [h, m] = String(t).split(':').map(Number)
  return Number.isFinite(h) && Number.isFinite(m) ? h * 60 + m : null
}

// Only complete rows go to the backend: a half-typed target would 422 the
// whole request, and the operator is mid-edit, not in error. Shared by the
// plan build and the full-day check so both see the same tributes.
const usableForeignTargets = (targets, villages = []) =>
  targets
    .filter((t) => t.name.trim() && Number(t.crop_per_hour) > 0)
    .map((t) => ({
      name: t.name.trim(),
      x: Number(t.x) || 0,
      y: Number(t.y) || 0,
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

const RESOURCES = ['lumber', 'clay', 'iron', 'crop']
const RESOURCE_LABEL = { lumber: 'Lumber', clay: 'Clay', iron: 'Iron', crop: 'Crop' }

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
const MODES = [
  { value: 'keep', label: 'Keep own' },
  { value: 'absolute', label: 'Absolute /h' },
  { value: 'percentage', label: '% of total' },
  { value: 'sustain', label: 'Sustain +%' },
]

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
function downloadJson(filename, value) {
  const blob = new Blob([JSON.stringify(value, null, 2)], {
    type: 'application/json;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
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
function BudgetBar({ budget }) {
  const spare = Math.max(budget.spare, 1)
  const usedPct = Math.min(100, Math.round((budget.committed / spare) * 100))
  // Over-budget villages open by default. "over by 2" says what happened but
  // not what to do about it, and the same excess means different things when
  // the trip is the cost than when the Trade Office is.
  const [open, setOpen] = useState(false)
  const legs = budget.legs ?? []
  return (
    <div className="flex-1">
      <div className="flex items-center gap-2">
        <div
          className="h-2 w-28 rounded bg-black/40 overflow-hidden shrink-0"
          role="img"
          aria-label={`${budget.committed} of ${budget.spare} merchants committed`}
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
function BatchSet({ count, onApply }) {
  const [mode, setMode] = useState('keep')
  const [value, setValue] = useState(0)
  return (
    <div className="flex items-center gap-1 text-xs">
      <span className="text-secondary">Set checked</span>
      <select
        aria-label="Batch mode"
        className="input-field text-xs py-0.5"
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
  // Operator alert level for a village's crop stock (e.g. an NPC trigger),
  // below capacity. Cached per account like the Trade Office levels.
  const [cropCeilings, setCropCeilings] = useState({})
  // Where each village may ship: { [village_id]: number[] }. Absent means
  // unrestricted (the default); an empty list means nobody. Owned, like the
  // Trade Office level -- nothing in the game says which of your own villages a
  // merchant may be sent to.
  const [shipOnlyTo, setShipOnlyTo] = useState({})
  // Share of warehouse capacity each village keeps stocked by NPC trading, as a
  // FRACTION (0.3, not 30) so state, file and request agree; the input shows it
  // as a percent. The planner may draw it down as lumber, clay or iron.
  const [stockFloors, setStockFloors] = useState({})
  // Result of the last setup-file load, kept on screen rather than only in a
  // toast: a file that is missing villages produces a quietly wrong plan, so
  // what it did and did not cover has to stay readable.
  const [setupReport, setSetupReport] = useState(null)
  const [pasteOpen, setPasteOpen] = useState(false)
  const [pasteText, setPasteText] = useState('')
  const setupFileRef = useRef(null)
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
  // one-hour cycles is seventy-two rows. Blank = no limit, so an existing run
  // is unchanged until the operator sets one.
  const [maxGameRows, setMaxGameRows] = useState('')
  // Travian cannot confine a route to part of the day, but its fan-out can be
  // trimmed: repeat-every-N-hours is 24/N individually deletable rows, so the
  // ones departing outside the profile get removed after creation. On by
  // default because OFF is the broken case -- the window is a fiction the game
  // ignores, and the destination receives every firing.
  const [pruneToWindow, setPruneToWindow] = useState(true)
  // The two numbers the account cannot supply: how empty the stores actually
  // are at bedtime, and how full they may be at dawn. Everything else the
  // derivation needs it works out for itself.
  const [baselineFill, setBaselineFill] = useState(30)
  const [targetFill, setTargetFill] = useState(80)
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
      setProfiles({ [DEFAULT_PROFILE]: {} })
      setForeignTargets([])
      setActiveProfile(DEFAULT_PROFILE)
      setMerchantModel(DEFAULT_MERCHANT_MODEL)
      setSelected({})
      setPlan(null)
      // The load report names villages from the snapshot it was matched
      // against, so it cannot outlive that account.
      setSetupReport(null)
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
    setCropCeilings(loadJson(`${LS_CROP_CEILING}::${accountKey}`, {}))
    setShipOnlyTo(loadJson(`${LS_SHIP_ONLY_TO}::${accountKey}`, {}))
    setStockFloors(loadJson(`${LS_STOCK_FLOOR}::${accountKey}`, {}))
    setDayCheck(null)
    setProfiles(loaded)
    setActiveProfile(loaded[storedActive] ? storedActive : Object.keys(loaded)[0])
    setMerchantModel(loadJson(`${LS_MERCHANT}::${accountKey}`, DEFAULT_MERCHANT_MODEL))
    setSelected({})
    setPlan(null)
    setSetupReport(null)
    setHydratedKey(accountKey)
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
  }, [
    profiles,
    profileWindows,
    cropCeilings,
    snapshot,
    foreignTargets,
    tradeOffice,
    shipOnlyTo,
    stockFloors,
    merchantModel,
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
  }, [
    allocations,
    tradeOffice,
    shipOnlyTo,
    stockFloors,
    merchantModel,
    foreignTargets,
    snapshot,
    profileWindows,
    activeProfile,
  ])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey)
      saveJson(storageKey(LS_CROP_CEILING), cropCeilings)
  }, [cropCeilings, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_SHIP_ONLY_TO), shipOnlyTo)
  }, [shipOnlyTo, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_STOCK_FLOOR), stockFloors)
  }, [stockFloors, hydratedKey, accountKey, storageKey])
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
  const exportSetup = useCallback(() => {
    if (!villages.length) {
      toast.error('Fetch account state first, so the file records village names too')
      return
    }
    const typed = villages.filter(
      (v) =>
        tradeOffice[v.village_id] != null ||
        cropCeilings[v.village_id] != null ||
        shipOnlyTo[v.village_id] != null ||
        stockFloors[v.village_id] != null
    ).length
    const named = Object.entries(profiles).filter(([, a]) => Object.keys(a ?? {}).length)
    if (!typed && !named.length) {
      toast.error('Nothing typed yet — fill in a Trade Office level, crop alert or allocation first')
      return
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
    downloadJson(
      setupFilename(playerName, stamp),
      buildSetup({
        account: accountKey,
        villages,
        tradeOffice,
        cropCeilings,
        shipOnlyTo,
        stockFloors,
        profiles,
        profileWindows,
        merchantModel,
        foreignTargets,
        exportedAt: new Date().toISOString(),
      })
    )
    const parts = []
    if (typed) parts.push(`${typed} village(s)`)
    if (named.length) parts.push(`${named.length} profile(s)`)
    toast.success(`Saved ${parts.join(' and ')} — keep the file, load it after a rebuild`)
  }, [
    villages,
    tradeOffice,
    cropCeilings,
    shipOnlyTo,
    stockFloors,
    profiles,
    profileWindows,
    merchantModel,
    foreignTargets,
    accountKey,
    playerName,
    toast,
  ])

  const applySetupText = useCallback(
    (text) => {
      if (!villages.length) {
        toast.error('Fetch account state first — a file cannot be matched to villages without it')
        return
      }
      let setup
      try {
        setup = parseSetup(text)
      } catch (err) {
        toast.error(err instanceof SetupFileError ? err.message : 'Could not read that file')
        return
      }
      // A file from another account would apply its levels to whatever village
      // happens to share an id, which is a silently wrong plan rather than a
      // visible error. Refuse unless the operator insists.
      if (!setupMatchesAccount(setup, accountKey)) {
        const proceed = window.confirm(
          `This file was exported from a different account:\n\n` +
            `  file:    ${setup.account}\n  current: ${accountKey}\n\n` +
            `Village ids are per-account, so loading it can attach the wrong ` +
            `Trade Office levels to the wrong villages. Load it anyway?`
        )
        if (!proceed) return
      }
      const merged = mergeSetup({
        setup,
        villages,
        tradeOffice,
        cropCeilings,
        shipOnlyTo,
        stockFloors,
        profiles,
        profileWindows,
        foreignTargets,
      })
      setTradeOffice(merged.tradeOffice)
      setForeignTargets(merged.foreignTargets)
      setCropCeilings(merged.cropCeilings)
      setShipOnlyTo(merged.shipOnlyTo)
      setStockFloors(merged.stockFloors)
      setProfiles(merged.profiles)
      setProfileWindows(merged.profileWindows)
      // Capacity is server-calibrated, so a file that carries a calibration is
      // more trustworthy than this build's default. Absent, the default stands.
      if (merged.merchantModel) setMerchantModel(merged.merchantModel)
      // Land on a profile the file actually brought, so its numbers are what the
      // operator sees rather than whichever profile happened to be selected.
      const [first] = merged.report.profilesLoaded
      if (first) setActiveProfile(first)
      setSetupReport(merged.report)
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
      toast.success(`Loaded ${parts.join(' and ') || 'nothing'} from the setup file`)
    },
    [
      villages,
      tradeOffice,
      cropCeilings,
      shipOnlyTo,
      stockFloors,
      profiles,
      profileWindows,
      foreignTargets,
      accountKey,
      toast,
    ]
  )

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

  const buildPlanPayload = useCallback(() => {
    // Every explicit allocation is sent, readable rate or not. Filtering the
    // unreadable ones here hid them from the backend's UNREADABLE_RATE critical
    // finding -- the plan showed "Ready to run" while silently planning without
    // an allocation the operator wrote. The backend drops what it cannot use,
    // says so as a CRITICAL finding, and refuses a live run over it.
    const sendAllocations = {}
    for (const [resource, per] of Object.entries(allocations)) {
      const usable = {}
      for (const [vid, a] of Object.entries(per)) {
        if (a.mode === 'keep') continue
        if (!villages.some((x) => x.village_id === Number(vid))) continue
        usable[vid] = a
      }
      if (Object.keys(usable).length) sendAllocations[resource] = usable
    }
    // The active profile's own hours. Without them the optimizer phases each
    // route's send time anywhere in its cycle, so a profile that runs only part
    // of the day gets sheet rows -- and, via /execute, REAL routes -- that fire
    // while a different profile is meant to be running. Read inline rather than
    // through windowFor(), which is declared further down: naming it in the
    // dependency array below would evaluate it in its temporal dead zone.
    const hours = profileWindows[activeProfile] ?? DEFAULT_WINDOWS[activeProfile] ?? null
    const from = hours && hhmmToMinutes(hours[0])
    const to = hours && hhmmToMinutes(hours[1])
    // A zero-width or unparseable window is sent as null, not as a broken pair:
    // the backend rejects start === end, and an all-day profile needs no phasing.
    const dispatchWindow = from == null || to == null || from === to ? null : [from, to]

    return {
      snapshot: villages,
      dispatch_window: dispatchWindow,
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
      config: villages.map((v) => ({
        village_id: v.village_id,
        trade_office_level: Number(tradeOffice[v.village_id] ?? 0),
        // Both omitted when unset, so an ordinary village's row is byte-identical
        // to before: absent means "unrestricted" and "no floor" on the backend.
        // An EMPTY ship_only_to list is sent, because it means "nobody".
        ...(shipOnlyTo[v.village_id] != null ? { ship_only_to: shipOnlyTo[v.village_id] } : {}),
        ...(stockFloors[v.village_id] != null
          ? { stock_floor_fraction: stockFloors[v.village_id] }
          : {}),
      })),
      allocations: sendAllocations,
      foreign_targets: usableForeignTargets(foreignTargets, villages),
      // Geometry defaults to the snapshot (map span + tribe-derived x1 merchant
      // speed) but the operator can override both for non-Europe 2 worlds.
      map_span: Number(merchantModel.map_span) || snapshot?.map_span,
      speed_fields_per_hour:
        Number(merchantModel.speed_fields_per_hour) || snapshot?.speed_fields_per_hour,
      merchant_base_capacity: Number(merchantModel.base_capacity) || undefined,
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
    stockFloors,
    allocations,
    foreignTargets,
    merchantModel,
    snapshot,
    profileWindows,
    activeProfile,
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
      setStage('plan')
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
  ])

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
    for (const name of Object.keys(profiles)) {
      const w = profileWindows[name] ?? DEFAULT_WINDOWS[name] ?? null
      const start = w && hhmmToMinutes(w[0])
      const end = w && hhmmToMinutes(w[1])
      if (start == null || end == null || start === end) {
        skipped.push(name)
        continue
      }
      const per = profiles[name] ?? {}
      const sendAllocations = {}
      for (const resource of RESOURCES) {
        const usable = {}
        for (const [vid, a] of Object.entries(per[resource] ?? {})) {
          if (a.mode !== 'keep') usable[vid] = a
        }
        if (Object.keys(usable).length) sendAllocations[resource] = usable
      }
      segments.push({ name, window: [start, end], allocations: sendAllocations })
    }
    return { segments, skipped }
  }, [profiles, profileWindows])

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
    const { segments, skipped } = buildSegments()
    if (!segments.length) {
      throw new Error('No profile has hours set — give each profile its window first')
    }
    if (skipped.length) {
      throw new Error(
        `Whole-day execution needs hours on every profile — missing: ${skipped.join(', ')}`
      )
    }
    const { allocations: _a, dispatch_window: _w, ...rest } = base
    return { ...rest, segments, prune_to_window: true }
  }, [buildPlanPayload, buildSegments, wholeDay])

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
          max_routes_per_run: Number(routesPerRun) || MAX_ROUTES_PER_RUN,
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
  const profileDayShare = useMemo(() => {
    const hours = profileWindows[activeProfile] ?? DEFAULT_WINDOWS[activeProfile] ?? null
    if (!hours) return 1
    const from = hhmmToMinutes(hours[0])
    const to = hhmmToMinutes(hours[1])
    if (from == null || to == null || from === to) return 1
    return (((to - from) % MINUTES_IN_DAY) + MINUTES_IN_DAY) % MINUTES_IN_DAY / MINUTES_IN_DAY
  }, [profileWindows, activeProfile])

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
  }, [buildPlanPayload, baselineFill, targetFill, setAllocations, toast])

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
            max_routes_per_run: wholeDay ? Number(routesPerRun) || MAX_ROUTES_PER_RUN : 0,
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

  const setAllocation = (resource, villageId, patch) => {
    setAllocations((prev) => {
      const per = { ...(prev[resource] ?? {}) }
      per[villageId] = { mode: 'keep', value: 0, ...(per[villageId] ?? {}), ...patch }
      return { ...prev, [resource]: per }
    })
  }

  // What a village ends up retaining per hour after the plan runs -- the same
  // resolution the backend applies, so the grid never disagrees with the plan.
  const targetFor = (resource, v) => {
    const own = v[`${resource}_per_hour`]
    const a = allocations[resource]?.[v.village_id] ?? { mode: 'keep', value: 0 }
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

  const setRemainder = (resource, villageId) => {
    setAllocations((prev) => {
      const per = { ...(prev[resource] ?? {}) }
      // Exactly one remainder per resource, enforced by the widget itself.
      for (const [vid, a] of Object.entries(per)) {
        if (a.mode === 'remainder') per[vid] = { mode: 'keep', value: 0 }
      }
      per[villageId] = { mode: 'remainder', value: 0 }
      return { ...prev, [resource]: per }
    })
  }

  const explicitTotal = (resource) => {
    const per = allocations[resource] ?? {}
    const { total } = totals[resource]
    let assigned = 0
    for (const v of villages) {
      // A village with no entry keeps its own production — the same default the
      // backend resolves — so an untouched account shows 0 unassigned, not the
      // whole account total.
      const a = per[v.village_id] ?? { mode: 'keep', value: 0 }
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
    const { segments, skipped } = buildSegments()
    if (!segments.length) {
      toast.error('No profile has hours set — give each profile its window first')
      return
    }
    setDayChecking(true)
    try {
      const ceilings = {}
      for (const [vid, value] of Object.entries(cropCeilings)) {
        if (Number(value) > 0) ceilings[vid] = Number(value)
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

  const addProfile = () => {
    const name = (window.prompt('New profile name', 'Night') || '').trim()
    if (!name) return
    if (profiles[name]) {
      toast.error(`Profile "${name}" already exists`)
      return
    }
    setProfiles((prev) => ({ ...prev, [name]: {} }))
    setActiveProfile(name)
    setPlan(null)
  }

  const duplicateProfile = () => {
    const name = (window.prompt(`Duplicate "${activeProfile}" as`, `${activeProfile} copy`) || '').trim()
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

  const renameProfile = () => {
    const name = (window.prompt('Rename profile', activeProfile) || '').trim()
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
    setActiveProfile(name)
  }

  const deleteProfile = () => {
    if (profileNames.length <= 1) {
      toast.error('Keep at least one profile')
      return
    }
    if (!window.confirm(`Delete profile "${activeProfile}"?`)) return
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

  const stages = [
    { id: 'snapshot', label: 'Snapshot' },
    { id: 'allocate', label: 'Allocate' },
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

  const liveConfirmMessage = [
    'Execute this plan against Travian now?',
    '',
    // Only claimed when it is actually going to happen: the checkbox can make
    // this a create-only run, and a dialog listing a disable that will not
    // occur is worse than no dialog.
    disableExisting
      ? `• Disable existing routes this plan no longer wants, on up to ${plannedOriginCount} origin village(s)`
      : '• Create ONLY — no existing route will be disabled',
    `• Create up to ${plannedCreateCount} new route(s)`,
    execResult?.remaining ? `• Defer ${execResult.remaining} route(s) to a later run` : null,
    '',
    'Already-active routes that the plan still wants are left untouched, and a',
    'route the plan wants that is currently DISABLED is switched back on.',
    '',
    'This sends live requests to Travian. If a create fails after a disable, old',
    'routes can remain disabled without their replacements — re-run to reconcile.',
  ]
    .filter((line) => line !== null)
    .join('\n')

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex justify-between items-center mb-4 gap-3 flex-wrap">
        <h2 className="heading-gold text-2xl">Resource Planner</h2>
        <div className="flex items-center gap-3">
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
          className="input-field text-xs py-1"
          value={activeProfile}
          onChange={(e) => switchProfile(e.target.value)}
        >
          {profileNames.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
        <button className="btn-secondary btn-xs" onClick={addProfile}>
          + New
        </button>
        <button className="btn-secondary btn-xs" onClick={duplicateProfile}>
          Duplicate
        </button>
        <button className="btn-secondary btn-xs" onClick={renameProfile}>
          Rename
        </button>
        <button
          className="btn-secondary btn-xs"
          onClick={deleteProfile}
          disabled={profileNames.length <= 1}
        >
          Delete
        </button>
        {/* The hours this profile actually runs. Profiles are separate plans,
            but the account lives through all of them every day; without hours
            the full-day check cannot line them up. */}
        <span className="flex items-center gap-1 text-xs text-secondary ml-1">
          runs
          <input
            type="time"
            aria-label={`${activeProfile} window start`}
            className="input-field text-xs py-0.5 px-1 w-[74px]"
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
            className="input-field text-xs py-0.5 px-1 w-[74px]"
            value={(windowFor(activeProfile) ?? ['', ''])[1]}
            onChange={(e) =>
              setProfileWindows((prev) => ({
                ...prev,
                [activeProfile]: [(windowFor(activeProfile) ?? ['', ''])[0], e.target.value],
              }))
            }
          />
        </span>
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
        <div className="card p-4 overflow-x-auto">
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
            <button className="btn-secondary btn-sm" onClick={exportSetup}>
              Save setup to file
            </button>
            <button className="btn-secondary btn-sm" onClick={() => setupFileRef.current?.click()}>
              Load setup from file
            </button>
            <button
              className="btn-secondary btn-sm"
              onClick={() => setPasteOpen((v) => !v)}
              aria-expanded={pasteOpen}
            >
              {pasteOpen ? 'Cancel paste' : 'Paste setup'}
            </button>
            <input
              ref={setupFileRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={onSetupFileChosen}
            />
            <span className="text-secondary text-xs">0 Travian requests</span>
          </div>

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
                Loaded {setupReport.loaded} village(s) from the setup file.
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
            </div>
          )}

          {/* Same rule as the Allocate grid, and for higher stakes: the four
              hand-typed columns are the RIGHTMOST of ten, so on a phone the
              village name is off-screen exactly while a Trade Office level is
              being typed — and a level typed one row off breaches that
              village's merchant budget without a warning anywhere. Pin the
              identity column and say the rest are there. */}
          <p className="text-secondary text-xs mb-1 sm:hidden">
            Swipe the table sideways for Merchants, Trade Office, Crop alert, Ships only to and
            Stock floor — the village column stays pinned.
          </p>
          <div className="overflow-x-auto">
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
                  <th className="text-right px-2">Trade Office</th>
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
                    className="text-right px-2"
                    title="Share of warehouse capacity this village keeps stocked by NPC trading. The planner may draw it down over the profile window as extra lumber, clay or iron — never crop."
                  >
                    Stock floor %
                  </th>
                </tr>
              </thead>
              <tbody>
                {visibleVillages.map((v) => (
                  <tr
                    key={v.village_id}
                    className="group touch-target border-t border-gray-800 hover:bg-white/5 focus-within:bg-violet-400/15 transition-colors"
                  >
                    <td className="py-1.5 px-2 sticky-col border-l-2 border-l-transparent group-focus-within:border-l-violet-400">
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
                    <td className="text-right px-2">
                      {/* Owned, not fetched — editable, and blank means "unknown",
                          which the planner floors to 0 rather than guessing up. */}
                      <input
                        type="number"
                        min="0"
                        max="20"
                        aria-label={`Trade Office level for ${v.name}`}
                        placeholder="?"
                        className="input-field w-16 text-right text-xs py-1"
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
                      <input
                        type="number"
                        min="0"
                        aria-label={`Crop stock alert level for ${v.name}`}
                        placeholder="none"
                        className="input-field w-24 text-right text-xs py-1"
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
                              className="input-field w-16 text-right text-xs py-1"
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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
                className="input-field w-24 text-right py-1"
                value={merchantModel.base_capacity}
                onChange={(e) =>
                  setMerchantModel((m) => ({ ...m, base_capacity: Number(e.target.value) }))
                }
              />
            </label>
            <label className="flex items-center gap-1">
              <span className="text-secondary">Bonus / TO level</span>
              <input
                type="number"
                min="0"
                step="0.05"
                aria-label="Trade Office bonus per level"
                className="input-field w-20 text-right py-1"
                value={merchantModel.bonus_per_to_level}
                onChange={(e) =>
                  setMerchantModel((m) => ({ ...m, bonus_per_to_level: Number(e.target.value) }))
                }
              />
            </label>
            <label className="flex items-center gap-1">
              <span className="text-secondary">Speed f/h</span>
              <input
                type="number"
                min="1"
                aria-label="Merchant speed fields per hour override"
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
            </label>
            <label className="flex items-center gap-1">
              <span className="text-secondary">Map span</span>
              <input
                type="number"
                min="1"
                aria-label="Map span override"
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
                <p className="text-secondary text-xs mb-1 sm:hidden">
                  Swipe sideways for the coordinates, crop owed, margin and route flag — the name
                  column stays pinned.
                </p>
                <div className="overflow-x-auto">
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
                        const incomplete = !String(t.name).trim() || owed <= 0
                        const patch = (field, value) =>
                          setForeignTargets((prev) =>
                            prev.map((row, j) => (j === i ? { ...row, [field]: value } : row))
                          )
                        return (
                          <tr
                            key={i}
                            className="group touch-target border-t border-gray-800 hover:bg-white/5 focus-within:bg-violet-400/15 transition-colors"
                          >
                            <td className="py-1 px-2 sticky-col border-l-2 border-l-transparent group-focus-within:border-l-violet-400">
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
                                className="input-field w-16 text-right text-xs py-0.5"
                                value={t.x}
                                onChange={(e) => patch('x', e.target.value)}
                              />
                            </td>
                            <td className="text-right px-2">
                              <input
                                type="number"
                                aria-label={`Foreign target ${i + 1} y coordinate`}
                                className="input-field w-16 text-right text-xs py-0.5"
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
                                className="input-field w-16 text-right text-xs py-0.5"
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
                              <input
                                type="text"
                                aria-label={`Foreign target ${i + 1} excluded origins`}
                                placeholder="none"
                                className="input-field w-28 text-right text-xs py-0.5"
                                value={
                                  t.exclude_origins_text ??
                                  namesForVillageIds(t.exclude_origins, villages)
                                }
                                onChange={(e) => patch('exclude_origins_text', e.target.value)}
                              />
                              {/* Named back, so a typo cannot pass for an exclusion.
                                  Silently dropping "2" for "02" would leave the
                                  operator believing a village is excluded while the
                                  next run draws on it. */}
                              {(() => {
                                const { unknown } = resolveVillageNames(
                                  t.exclude_origins_text ??
                                    namesForVillageIds(t.exclude_origins, villages),
                                  villages
                                )
                                return unknown.length ? (
                                  <span className="block text-warning text-xs mt-0.5">
                                    no village named {unknown.join(', ')}
                                  </span>
                                ) : null
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
                                  title="Needs a name and a crop rate before the planner uses it"
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
                </div>
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

              <div className="flex items-end gap-3">
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
              ['fullday', 'Full day (all profiles)'],
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

          {allocView === 'village' && (
            <div className="card p-4 overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-secondary uppercase">
                  <tr>
                    <th className="text-left py-1.5 px-2">Village</th>
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
                      <td className="py-1.5 px-2 whitespace-nowrap">
                        {v.name}{' '}
                        <span className="text-secondary text-[11px]">
                          ({v.x}|{v.y})
                        </span>
                      </td>
                      {RESOURCES.map((resource) => {
                        const own = v[`${resource}_per_hour`]
                        const after = targetFor(resource, v)
                        const ship = after == null || own == null ? null : after - own
                        const isRest = remainderFor(resource) === v.village_id
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
                              title={own == null ? 'own production unknown' : `own ${fmt(own)}/h`}
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
              <p className="text-secondary text-[11px] mt-2">
                Top line: retention after distribution (red = still negative; a green arrow
                marks a village whose crop crosses from starving to surplus — e.g. −2,500/h own
                +4,000/h shipped → 1,500/h). Bottom line: what ships in (+) or out (−) to make it
                true. “rest” absorbs whatever the others leave unassigned.
              </p>
            </div>
          )}

          {allocView === 'fullday' && (
            <div className="card p-4">
              <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
                <div>
                  <h3 className="font-semibold">The whole day, every profile in its hours</h3>
                  <p className="text-secondary text-xs mt-0.5">
                    Profiles are planned separately, but the account lives through all of them:
                    what Day ships decides the stock Night starts from. This simulates the
                    composite — net rates per window, production always on — and answers
                    questions like “does 02 cross its crop alert at night?” with an hour on it.
                  </p>
                </div>
                <button
                  type="button"
                  className="btn-primary text-xs py-1.5"
                  disabled={dayChecking}
                  onClick={runDayCheck}
                >
                  {dayChecking ? 'Simulating…' : dayCheck ? 'Re-run (0 requests)' : 'Run (0 requests)'}
                </button>
              </div>

              {dayCheck?.skipped?.length > 0 && (
                <p className="text-warning text-xs mb-2">
                  Skipped {dayCheck.skipped.join(', ')} — no hours set. Give each profile its
                  window in the bar above.
                </p>
              )}

              {dayCheck?.warnings?.length > 0 && (
                <div className="mb-3">
                  <p className="text-xs text-warning font-semibold">
                    ⚠ Warnings ({dayCheck.warnings.length})
                  </p>
                  <ul className="text-xs text-warning list-disc list-inside space-y-0.5">
                    {dayCheck.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}
              {dayCheck && dayCheck.warnings.length === 0 && (
                <p className="text-success text-xs mb-3">
                  No store crosses its cap, alert level or zero across the full day.
                </p>
              )}

              {dayCheck && (
                <table className="w-full text-xs">
                  <thead className="text-secondary uppercase">
                    <tr>
                      <th className="text-left py-1 px-2">Village</th>
                      <th className="text-right px-2">
                        <span className="inline-flex items-center gap-1">
                          <ResourceIcon resource="crop" />
                          Crop now
                        </span>
                      </th>
                      <th className="text-right px-2">Day swing (low → high)</th>
                      <th className="text-right px-2">Drift/day</th>
                      <th className="text-right px-2" title="Your alert level from the Snapshot tab">
                        Alert at
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {dayCheck.villages
                      .filter((t) => t.resource === 'crop')
                      .map((t) => {
                        const ceiling = Number(cropCeilings[t.village_id]) || null
                        const nearAlert = ceiling != null && t.high >= ceiling
                        return (
                          <tr
                            key={t.village_id}
                            className={`border-t border-gray-800 ${nearAlert ? 'bg-red-500/10' : ''}`}
                          >
                            <td className="py-1 px-2">{t.village_name}</td>
                            <td className="text-right px-2 font-mono text-secondary">
                              {fmt(
                                villages.find((v) => v.village_id === t.village_id)?.crop_stock ?? 0
                              )}
                            </td>
                            <td className="text-right px-2 font-mono">
                              {fmt(t.low)} → {fmt(t.high)}
                              {!t.settled && (
                                <span
                                  className="text-warning ml-1"
                                  title="Still drifting at the simulation horizon — the drift column is the story"
                                >
                                  ↗
                                </span>
                              )}
                            </td>
                            <td
                              className={`text-right px-2 font-mono ${
                                // Either direction of drift needs attention:
                                // up walks into the cap or the alert, down
                                // walks toward an empty granary. Green would
                                // read as "fine" about a village slowly
                                // starving.
                                Math.abs(t.daily_net) < 1
                                  ? 'text-secondary/60'
                                  : 'text-warning'
                              }`}
                            >
                              {signed(t.daily_net)}
                            </td>
                            <td className="text-right px-2 font-mono text-secondary">
                              {ceiling != null ? fmt(ceiling) : '—'}
                            </td>
                          </tr>
                        )
                      })}
                  </tbody>
                </table>
              )}
              {!dayCheck && !dayChecking && (
                <p className="text-secondary text-xs italic">
                  Not run yet. It costs no game requests — everything comes from the snapshot you
                  already hold.
                </p>
              )}
              <p className="text-secondary text-[11px] mt-2">
                Cargo is counted when it <em>lands</em>, not when it leaves: a batch a day-profile
                route dispatches at 22:00 is credited to whichever profile owns the hour it
                actually arrives in. Each profile’s routes fire on their own schedule inside its
                hours, so an overflow caused by a hand-off between profiles shows up here.
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
                {/* On a phone this editor is wider than the viewport. The village
                    column is pinned (see .sticky-col) so every field stays
                    attributable to the right village while the rest scrolls, and
                    the hint below tells the operator the extra columns exist —
                    clipping them silently is how the wrong village gets edited. */}
                <p className="text-secondary text-xs mb-1 sm:hidden">
                  Swipe the table sideways for Mode, Value, Ship/h and Rest — the village column
                  stays pinned.
                </p>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="text-secondary uppercase">
                      <tr>
                        <th className="text-center px-2">
                          <input
                            type="checkbox"
                            aria-label="Select all villages"
                            checked={allSelected(resource)}
                            ref={(el) => {
                              if (el) el.indeterminate = someSelected(resource) && !allSelected(resource)
                            }}
                            onChange={() => toggleSelectAll(resource)}
                          />
                        </th>
                        <th className="text-left py-1 px-2 sticky-col">Village</th>
                        <th className="text-right px-2">Own/h</th>
                        <th className="text-left px-2">Mode</th>
                        <th className="text-right px-2">Value</th>
                        <th className="text-right px-2">Ship/h</th>
                        <th className="text-center px-2">Rest</th>
                      </tr>
                    </thead>
                    <tbody>
                      {villages.map((v) => {
                        const own = v[`${resource}_per_hour`]
                        const a = allocations[resource]?.[v.village_id] ?? { mode: 'keep', value: 0 }
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
                            className={`group touch-target border-t border-gray-800 hover:bg-white/5 focus-within:bg-violet-400/15 transition-colors ${
                              isSelected(resource, v.village_id) ? 'bg-violet-400/10' : ''
                            }`}
                          >
                            <td className="text-center px-2 border-l-2 border-l-transparent group-focus-within:border-l-violet-400">
                              <input
                                type="checkbox"
                                aria-label={`Select ${v.name} for batch edit`}
                                checked={isSelected(resource, v.village_id)}
                                onChange={() => toggleSelected(resource, v.village_id)}
                              />
                            </td>
                            <td className="py-1 px-2 sticky-col">{v.name}</td>
                            <td className="text-right px-2 font-mono text-secondary">
                              {own == null ? '—' : signed(own)}
                            </td>
                            <td className="px-2">
                              <select
                                aria-label={`${RESOURCE_LABEL[resource]} mode for ${v.name}`}
                                className="input-field text-xs py-0.5"
                                value={a.mode === 'remainder' ? 'keep' : a.mode}
                                onChange={(e) =>
                                  setAllocation(resource, v.village_id, { mode: e.target.value })
                                }
                              >
                                {MODES.map((m) => (
                                  <option key={m.value} value={m.value}>
                                    {m.label}
                                  </option>
                                ))}
                              </select>
                            </td>
                            <td className="text-right px-2">
                              <input
                                type="number"
                                aria-label={`${RESOURCE_LABEL[resource]} value for ${v.name}`}
                                className="input-field w-24 text-right text-xs py-0.5"
                                disabled={a.mode === 'keep' || a.mode === 'remainder'}
                                value={a.value ?? 0}
                                onChange={(e) =>
                                  setAllocation(resource, v.village_id, {
                                    value: Number(e.target.value),
                                  })
                                }
                              />
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
                </div>
              </div>
            )
            })}
        </div>
      )}

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
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {runHistory.runs.map((r) => (
                    <tr key={r.run_id} className="border-t border-gray-800">
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
                    </tr>
                  ))}
                </tbody>
              </table>
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
            </div>
          </details>
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
              <div className="card p-4 flex flex-wrap gap-6 items-center">
                <div>
                  <div className="text-secondary text-xs uppercase">Routes</div>
                  <div className="text-xl font-mono">{plan.rows.length}</div>
                </div>
                <div>
                  <div className="text-secondary text-xs uppercase">Merchants</div>
                  <div className="text-xl font-mono">{plan.total_merchants}</div>
                </div>
                <div>
                  <div className="text-secondary text-xs uppercase">Status</div>
                  <div className={planState.tone}>{planState.label}</div>
                  {verdict && !verdict.clean && (
                    <div className="text-secondary text-xs">
                      {verdict.executable
                        ? `${verdict.critical_findings} critical finding${
                            verdict.critical_findings === 1 ? '' : 's'
                          } this check does not weigh`
                        : `${verdict.blockers.length} blocker${
                            verdict.blockers.length === 1 ? '' : 's'
                          }`}
                    </div>
                  )}
                </div>
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
                  {verdict.blockers.length > 0 && (
                    <>
                      <p className="text-danger mt-2 font-semibold">
                        Why it cannot run, in full:
                      </p>
                      <ul className="list-disc list-inside text-danger">
                        {verdict.blockers.map((line) => (
                          <li key={line}>{line}</li>
                        ))}
                      </ul>
                    </>
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

              <div className="card p-4">
                <h3 className="font-semibold mb-2">Merchant budget</h3>
                <div className="space-y-1">
                  {plan.budgets
                    .filter((b) => b.committed > 0 || b.over_budget)
                    .map((b) => (
                      <div key={b.village_id} className="flex items-start gap-3 text-xs">
                        <span className="w-28 truncate shrink-0">
                          {villages.find((v) => v.village_id === b.village_id)?.name ??
                            b.village_id}
                        </span>
                        <BudgetBar budget={b} />
                      </div>
                    ))}
                </div>
              </div>

              <div className="card p-4 overflow-x-auto">
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
                  <div className="mt-3 border-t border-gray-800 pt-3">
                    {/* Rewritten after the operator read it and had to ask what
                        it meant. The old copy opened with the mechanism -- how a
                        hub's granary refills -- and left the two things they
                        actually needed buried: that a chain is TWO rows and both
                        must exist, and that the big number is travel time and not
                        a gap between deliveries. It also called that number
                        "worst case", which reads as danger for a figure that is
                        simply how long the pipe takes to fill. Consequence and
                        action first; mechanism underneath for whoever wants it. */}
                    <h4 className="font-semibold text-xs">
                      Relayed crop · {relays.length} chain{relays.length === 1 ? '' : 's'}
                    </h4>
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
                  </div>
                )}
              </div>

              <div className="card p-4">
                <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
                  <div>
                    <h3 className="font-semibold">Execute as trade routes</h3>
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
                    be one chosen route, not whichever one the cap reached first. */}
                <div className="mb-3 rounded border border-gray-800 p-2">
                  <p className="text-secondary mb-2 text-[11px]">
                    <strong>Controlled run.</strong> Narrow this run to specific villages — for a
                    first live test, or to retry one village after a failure. Leave the village
                    boxes empty to run the whole plan. A narrowed run is labelled as narrowed in
                    the result.
                  </p>
                  <div className="flex flex-wrap items-end gap-2">
                    <label className="text-xs">
                      <span className="text-secondary block">Routes this run</span>
                      <input
                        type="number"
                        min="0"
                        className="input-sm w-24"
                        value={routesPerRun}
                        onChange={(e) => setRoutesPerRun(e.target.value)}
                      />
                    </label>
                    {/* The unit that actually lands in the game. A "route" is a
                        request; Travian turns it into 24/cycle daily rows, so a
                        cap of 3 routes on 1-hour cycles is 72 rows. */}
                    <label className="text-xs">
                      <span className="text-secondary block">
                        Max rows this run <span className="text-secondary">(0 = no limit)</span>
                      </span>
                      <input
                        type="number"
                        min="0"
                        className="input-sm w-28"
                        value={maxGameRows}
                        onChange={(e) => setMaxGameRows(e.target.value)}
                      />
                    </label>
                    <label className="text-xs flex items-start gap-2 max-w-md">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={wholeDay}
                        onChange={(e) => setWholeDay(e.target.checked)}
                      />
                      <span className="text-secondary">
                        <span className="text-primary">
                          Whole day — execute all profiles at once.
                        </span>{' '}
                        Plans every profile in its own hours and creates both route
                        sets in one pass, reconciled together: a Night row is never
                        &ldquo;stale&rdquo; to a Day-eyed run. Both sets then coexist
                        in the game — disjoint by departure time — so the account
                        runs around the clock with <strong>no daily switching</strong>.
                        The reconcile sweep also <em>creates</em> as it goes in this
                        mode, so one pass over the villages provisions the whole day.
                        Needs hours on every profile; the trim below is forced on.
                      </span>
                    </label>
                    <label className="text-xs flex items-start gap-2 max-w-md">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={pruneToWindow || wholeDay}
                        disabled={wholeDay}
                        onChange={(e) => setPruneToWindow(e.target.checked)}
                      />
                      <span className="text-secondary">
                        <span className="text-primary">Trim the fan-out to the profile hours.</span>{' '}
                        Travian has no setting that confines a route to part of the
                        day: &ldquo;repeat every N hours&rdquo; becomes 24/N daily rows
                        and every one of them fires. This deletes the rows departing
                        outside the profile hours after
                        the route is created, which is what makes the window real —
                        and cuts the row footprint to the share of the day it covers.
                        Untick only for a round-the-clock profile.
                      </span>
                    </label>
                    <label className="text-xs">
                      <span className="text-secondary block">
                        Never disable (ids or x|y, comma-separated)
                      </span>
                      <input
                        type="text"
                        className="input-sm w-56"
                        placeholder="none"
                        value={protectDestinations}
                        onChange={(e) => setProtectDestinations(e.target.value)}
                      />
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
                  <label className="mt-2 flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={disableExisting}
                      onChange={(e) => setDisableExisting(e.target.checked)}
                    />
                    <span>
                      Also disable routes the plan no longer wants.{' '}
                      <span className="text-secondary">
                        Untick for a create-only run — then the only thing it changes in the game
                        is the route it creates. Leave ticked for normal use, or old routes pile
                        up alongside new ones.
                      </span>
                    </span>
                  </label>
                  <label className="mt-2 flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={updateDrifted}
                      onChange={(e) => setUpdateDrifted(e.target.checked)}
                    />
                    <span>
                      Correct cargo on routes that have drifted.{' '}
                      <span className="text-secondary">
                        A route is created once, but the plan moves every time production does.
                        Without this, live routes keep the amounts they were created with and
                        slowly stop matching the sheet. Off by default because it overwrites a
                        route you may have tuned in-game on purpose.
                      </span>
                    </span>
                  </label>
                </div>

                {!plan.feasible && (
                  <div className="text-warning text-xs mb-2">
                    <p>
                      <strong>Going live is blocked</strong> until this is resolved, and the server
                      refuses it too. Preview is not blocked: it changes nothing, costs no
                      requests, and naming the routes that break is how you resolve it.
                    </p>
                    {/* The reasons, not a category. "Over budget / unroutable" told the
                        operator which of two shapes the problem had and nothing about
                        which village caused it. */}
                    {verdict && verdict.blockers.length > 0 && (
                      <ul className="list-disc list-inside mt-1">
                        {verdict.blockers.map((line) => (
                          <li key={line}>{line}</li>
                        ))}
                      </ul>
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
                          <button
                            type="button"
                            className="btn-primary text-xs py-1.5 mt-2"
                            disabled={executing || !plan.feasible}
                            onClick={() => {
                              if (window.confirm(liveConfirmMessage)) executePlan(false)
                            }}
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
            </>
          )}
        </div>
      )}
    </div>
  )
}
