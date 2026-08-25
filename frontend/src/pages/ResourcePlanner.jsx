import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import PlanDiagnostics from '../components/PlanDiagnostics'
import { useToast } from '../components/Toast'
import useGameStore from '../stores/gameStore'
import useLogStore from '../stores/logStore'
import api from '../api'
import {
  SetupFileError,
  buildSetup,
  mergeSetup,
  parseSetup,
  setupFilename,
  setupMatchesAccount,
} from '../utils/plannerSetup'
import { METER_TONE, allocationMeterSeverity } from '../utils/plannerAllocation'
import { planStatus, relayLegIndex } from '../utils/plannerFindings'
import { routeSheetRow, routeSheetText } from '../utils/plannerSheet'
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
const SNAPSHOT_TTL_MS = 30 * 60 * 1000 // 30 minutes
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
const DEFAULT_MERCHANT_MODEL = { base_capacity: 2200, bonus_per_to_level: 0.2 }

const LS_WINDOWS = 'planner_profile_windows'
const LS_CROP_CEILING = 'planner_crop_ceiling'
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
const usableForeignTargets = (targets) =>
  targets
    .filter((t) => t.name.trim() && Number(t.crop_per_hour) > 0)
    .map((t) => ({
      name: t.name.trim(),
      x: Number(t.x) || 0,
      y: Number(t.y) || 0,
      crop_per_hour: Number(t.crop_per_hour),
      safety_margin_pct: Number(t.safety_margin_pct) || 0,
      route_eligible: Boolean(t.route_eligible),
    }))

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
  // from a stale one (see SNAPSHOT_TTL_MS).
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
  const [onlyDestination, setOnlyDestination] = useState('')
  // Was hardcoded true, which made every run a create AND a disable. For a
  // first live test that is the wrong shape: turning it off makes the run
  // create-only, so the single thing it changes is the single thing being
  // tested. Defaults to on, which is the behaviour for ordinary runs.
  const [disableExisting, setDisableExisting] = useState(true)
  // Off by default, and deliberately: correcting cargo overwrites a route that
  // may have been tuned in-game on purpose.
  const [updateDrifted, setUpdateDrifted] = useState(false)
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
    const remaining = snapshotFetchedAt + SNAPSHOT_TTL_MS - Date.now()
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
  }, [profiles, profileWindows, cropCeilings, snapshot, foreignTargets, tradeOffice, merchantModel])
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
      (v) => tradeOffice[v.village_id] != null || cropCeilings[v.village_id] != null
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
        profiles,
        profileWindows,
        merchantModel,
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
    profiles,
    profileWindows,
    merchantModel,
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
        profiles,
        profileWindows,
      })
      setTradeOffice(merged.tradeOffice)
      setCropCeilings(merged.cropCeilings)
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
    [villages, tradeOffice, cropCeilings, profiles, profileWindows, accountKey, toast]
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
    const sendAllocations = {}
    for (const [resource, per] of Object.entries(allocations)) {
      const usable = {}
      for (const [vid, a] of Object.entries(per)) {
        if (a.mode === 'keep') continue
        const v = villages.find((x) => x.village_id === Number(vid))
        if (!v || v[`${resource}_per_hour`] == null) continue
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
      config: villages.map((v) => ({
        village_id: v.village_id,
        trade_office_level: Number(tradeOffice[v.village_id] ?? 0),
      })),
      allocations: sendAllocations,
      foreign_targets: usableForeignTargets(foreignTargets),
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
  }, [villages, tradeOffice, allocations, foreignTargets, merchantModel, snapshot, profileWindows, activeProfile])

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
      (snapshotFetchedAt == null || Date.now() - snapshotFetchedAt > SNAPSHOT_TTL_MS)
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
        (snapshotFetchedAt == null || Date.now() - snapshotFetchedAt > SNAPSHOT_TTL_MS)
      ) {
        toast.error(
          'Snapshot is stale — fetch fresh state, or tick “plan from this stale snapshot anyway”.'
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
          ...buildPlanPayload(),
          dry_run: dryRun,
          disable_existing: disableExisting,
          max_routes_per_run: Number(routesPerRun) || MAX_ROUTES_PER_RUN,
          // Targeting a single pair is how a first live run against a real
          // account becomes a controlled test rather than an uncontrolled one
          // with a small blast radius. Omitted entirely when unset, so an
          // ordinary run is byte-identical to what it was before.
          ...(onlyOrigin ? { only_origins: [Number(onlyOrigin)] } : {}),
          ...(onlyDestination ? { only_destinations: [Number(onlyDestination)] } : {}),
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
      buildPlanPayload,
      toast,
      snapshotFetchedAt,
      useStaleSnapshot,
      routesPerRun,
      onlyOrigin,
      onlyDestination,
      disableExisting,
      updateDrifted,
    ],
  )

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
    const segments = []
    const skipped = []
    for (const name of profileNames) {
      const w = windowFor(name)
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
  const snapshotStale = !!snapshot && (snapshotFetchedAt == null || snapshotAgeMs > SNAPSHOT_TTL_MS)
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
          <label className="text-secondary text-xs flex items-center gap-1.5 mt-1.5">
            <input
              type="checkbox"
              checked={useStaleSnapshot}
              onChange={(e) => setUseStaleSnapshot(e.target.checked)}
            />
            Plan from this stale snapshot anyway
          </label>
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

          {/* Same rule as the Allocate grid, and for higher stakes: the two
              hand-typed columns are the RIGHTMOST of eight, so on a phone the
              village name is off-screen exactly while a Trade Office level is
              being typed — and a level typed one row off breaches that
              village's merchant budget without a warning anywhere. Pin the
              identity column and say the rest are there. */}
          <p className="text-secondary text-xs mb-1 sm:hidden">
            Swipe the table sideways for Merchants, Trade Office and Crop alert — the village
            column stays pinned.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-secondary text-xs uppercase">
                <tr>
                  <th className="text-left py-2 px-2 sticky-col">Village</th>
                  <th className="text-right px-2">Lumber/h</th>
                  <th className="text-right px-2">Clay/h</th>
                  <th className="text-right px-2">Iron/h</th>
                  <th className="text-left px-2">Net crop</th>
                  <th className="text-right px-2">Merchants</th>
                  <th className="text-right px-2">Trade Office</th>
                  <th
                    className="text-right px-2"
                    title="Alert when this village's crop stock would cross this level (e.g. your NPC trigger). Used by the full-day check."
                  >
                    Crop alert
                  </th>
                </tr>
              </thead>
              <tbody>
                {villages.map((v) => (
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
                        placeholder="—"
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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
                                  ? `relay leg 1 — ${relay.chain.hub_name} forwards this on`
                                  : `relay leg 2 — forwards what arrives at ${relay.chain.hub_name}`}
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
                    <h4 className="font-semibold text-xs">
                      Relayed crop — {relays.length} deliver
                      {relays.length === 1 ? 'y' : 'ies'} arriving in two hops
                    </h4>
                    <p className="text-secondary text-xs mt-1">
                      Two rows above, one delivery. The hub ships from its own granary and the
                      first leg refills it, so creating the second row without the first ships
                      nothing useful. “Total” is what you actually wait for: each leg's own worst
                      case in turn, which is why it can exceed a target both legs meet.
                    </p>
                    <table className="w-full text-xs mt-2">
                      <thead className="text-secondary uppercase">
                        <tr>
                          <th className="text-left py-1 px-2">Path</th>
                          <th className="text-right px-2">To hub</th>
                          <th className="text-right px-2">Hub on</th>
                          <th className="text-right px-2">Total</th>
                        </tr>
                      </thead>
                      <tbody>
                        {relays.map((chain) => (
                          <tr
                            key={`${chain.origin}:${chain.hub}:${chain.destination}`}
                            className="border-t border-gray-800"
                          >
                            <td className="py-1 px-2">
                              {chain.origin_name} → <strong>{chain.hub_name}</strong> →{' '}
                              {chain.destination_name}
                            </td>
                            <td className="text-right px-2 font-mono">
                              {chain.collect_hours.toFixed(1)}h
                            </td>
                            <td className="text-right px-2 font-mono">
                              {chain.forward_hours.toFixed(1)}h
                            </td>
                            <td className="text-right px-2 font-mono font-semibold">
                              {chain.end_to_end_hours.toFixed(1)}h
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
                        min="1"
                        className="input-sm w-24"
                        value={routesPerRun}
                        onChange={(e) => setRoutesPerRun(e.target.value)}
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
                    {execResult.warnings.length > 0 && (
                      <ul className="text-xs text-secondary list-disc list-inside mb-2">
                        {execResult.warnings.map((w, i) => (
                          <li key={i}>{w}</li>
                        ))}
                      </ul>
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
                        {execResult.actions.map((a, i) => (
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

                    {execResult.dry_run &&
                      (execResult.live_enabled ? (
                        <>
                          {/* The confirmation must name every state-changing
                              effect, not just creation: the request carries
                              disable_existing, so stale routes are switched off
                              first and a partial failure can leave them off with
                              replacements missing (issue #67). */}
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
