import { useState, useEffect, useMemo, useCallback } from 'react'
import { useToast } from '../components/Toast'
import useGameStore from '../stores/gameStore'
import api from '../api'

// Owned state the game will not tell us, kept per village. Trade Office level
// changes only when the operator builds one, so it is stored rather than fetched.
// Keys are namespaced per (server, player): the plan endpoint trusts the
// snapshot the client sends, so rehydrating another account's villages would
// generate a route sheet from the wrong account's data.
const LS_TRADE_OFFICE = 'planner_trade_office'
const LS_FOREIGN = 'planner_foreign_targets'
const LS_ALLOCATIONS = 'planner_allocations' // legacy single-plan key (migrated)
const LS_SNAPSHOT = 'planner_snapshot'
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

const RESOURCES = ['lumber', 'clay', 'iron', 'crop']
const RESOURCE_LABEL = { lumber: 'Lumber', clay: 'Clay', iron: 'Iron', crop: 'Crop' }
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
      <span className="text-amber-300" title="Rate could not be derived — not treated as zero">
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
            className="text-secondary hover:text-white text-xs underline shrink-0"
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
            <p className="text-xs text-amber-200 mb-1">{budget.explanation}</p>
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
  const [snapshot, setSnapshot] = useState(null)
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
  const [planning, setPlanning] = useState(false)

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

  useEffect(() => {
    if (!accountKey) {
      // Disconnected: the page stays routable, so showing (or planning from)
      // the previous account's villages would act on stale data.
      setSnapshot(null)
      setTradeOffice({})
      setProfiles({ [DEFAULT_PROFILE]: {} })
      setForeignTargets([])
      setActiveProfile(DEFAULT_PROFILE)
      setMerchantModel(DEFAULT_MERCHANT_MODEL)
      setSelected({})
      setPlan(null)
      setHydratedKey(null)
      return
    }
    const loaded = loadProfiles(accountKey)
    const storedActive = loadJson(`${LS_ACTIVE_PROFILE}::${accountKey}`, DEFAULT_PROFILE)
    setSnapshot(loadJson(`${LS_SNAPSHOT}::${accountKey}`, null))
    setTradeOffice(loadJson(`${LS_TRADE_OFFICE}::${accountKey}`, {}))
    setForeignTargets(loadJson(`${LS_FOREIGN}::${accountKey}`, []))
    setProfiles(loaded)
    setActiveProfile(loaded[storedActive] ? storedActive : Object.keys(loaded)[0])
    setMerchantModel(loadJson(`${LS_MERCHANT}::${accountKey}`, DEFAULT_MERCHANT_MODEL))
    setSelected({})
    setPlan(null)
    setHydratedKey(accountKey)
  }, [accountKey])

  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_TRADE_OFFICE), tradeOffice)
  }, [tradeOffice, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_FOREIGN), foreignTargets)
  }, [foreignTargets, hydratedKey, accountKey, storageKey])
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
      setSnapshot(res.data)
      saveJson(`${LS_SNAPSHOT}::${requestedFor}`, res.data)
      setPlan(null)
      // A new snapshot can have different village ids — drop stale selections.
      setSelected({})
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

  const buildPlan = useCallback(async () => {
    if (!villages.length) {
      toast.error('Fetch account state first')
      return
    }
    // Guard against the account changing mid-request: a plan built from
    // account A's snapshot must not be presented under account B.
    const requestedFor = accountKey
    setPlanning(true)
    try {
      // Send only entries the planner can act on: `keep` equals the backend
      // default, and a village whose rate is unknown for this resource is not
      // plannable — a stale or inert entry must not fail the whole plan.
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
      const res = await api.post('/distribution/plan', {
        snapshot: villages,
        config: villages.map((v) => ({
          village_id: v.village_id,
          trade_office_level: Number(tradeOffice[v.village_id] ?? 0),
        })),
        allocations: sendAllocations,
        // Only complete rows go to the planner: a half-typed target would
        // 422 the whole plan, and the operator is mid-edit, not in error.
        foreign_targets: foreignTargets
          .filter((t) => t.name.trim() && Number(t.crop_per_hour) > 0)
          .map((t) => ({
            name: t.name.trim(),
            x: Number(t.x) || 0,
            y: Number(t.y) || 0,
            crop_per_hour: Number(t.crop_per_hour),
            safety_margin_pct: Number(t.safety_margin_pct) || 0,
          })),
        // Geometry defaults to the snapshot (map span + tribe-derived x1
        // merchant speed) but the operator can override both for non-Europe 2
        // worlds (x2/x3 speed, larger maps) — no extra Travian requests.
        map_span: Number(merchantModel.map_span) || snapshot?.map_span,
        speed_fields_per_hour:
          Number(merchantModel.speed_fields_per_hour) || snapshot?.speed_fields_per_hour,
        merchant_base_capacity: Number(merchantModel.base_capacity) || undefined,
        // A per-level bonus of 0 is valid (a world with no Trade Office scaling),
        // so preserve it — `|| undefined` would drop it and use the 0.2 default.
        trade_office_bonus_per_level: Number.isFinite(Number(merchantModel.bonus_per_to_level))
          ? Number(merchantModel.bonus_per_to_level)
          : undefined,
      })
      if (requestedFor !== currentAccountKey()) return
      setPlan(res.data)
      setStage('plan')
    } catch (err) {
      toast.error(errorDetail(err, 'Could not build a plan'))
    } finally {
      setPlanning(false)
    }
  }, [
    villages,
    tradeOffice,
    allocations,
    toast,
    accountKey,
    currentAccountKey,
    snapshot,
    merchantModel,
    foreignTargets,
  ])

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
      const own = v[`${resource}_per_hour`] ?? 0
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
    setActiveProfile(fallback)
    setPlan(null)
  }

  const stages = [
    { id: 'snapshot', label: 'Snapshot' },
    { id: 'allocate', label: 'Allocate' },
    { id: 'plan', label: 'Plan' },
  ]

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex justify-between items-center mb-4 gap-3 flex-wrap">
        <h2 className="heading-gold text-2xl">Resource Planner</h2>
        <div className="flex items-center gap-3">
          <span className="text-secondary text-xs">
            {villages.length ? `${villages.length} villages` : 'no snapshot yet'}
          </span>
          {/* Every fetch is priced in the label — requests are the scarce
              resource, so the cost is stated before it is spent. */}
          <button className="btn-primary btn-sm" onClick={fetchSnapshot} disabled={fetching}>
            {fetching ? 'Reading…' : 'Fetch state (3–4 requests)'}
          </button>
          <button className="btn-secondary btn-sm" onClick={buildPlan} disabled={planning}>
            {planning ? 'Planning…' : 'Build plan (0 requests)'}
          </button>
        </div>
      </div>

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
                ? 'bg-black/40 text-white border-b-2 border-amber-400'
                : 'text-secondary hover:text-white'
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
          <table className="w-full text-sm">
            <thead className="text-secondary text-xs uppercase">
              <tr>
                <th className="text-left py-2 px-2">Village</th>
                <th className="text-right px-2">Lumber/h</th>
                <th className="text-right px-2">Clay/h</th>
                <th className="text-right px-2">Iron/h</th>
                <th className="text-left px-2">Net crop</th>
                <th className="text-right px-2">Merchants</th>
                <th className="text-right px-2">Trade Office</th>
              </tr>
            </thead>
            <tbody>
              {villages.map((v) => (
                <tr
                  key={v.village_id}
                  className="group border-t border-gray-800 hover:bg-white/5 focus-within:bg-amber-400/15 transition-colors"
                >
                  <td className="py-1.5 px-2 border-l-2 border-l-transparent group-focus-within:border-l-amber-400">
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
                </tr>
              ))}
            </tbody>
          </table>
          {snapshot?.warnings?.length > 0 && (
            <ul className="mt-3 text-xs text-amber-300 list-disc list-inside">
              {snapshot.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
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
                  Shipped like any other demand and taken out of the account crop pool. The
                  planner picks the supplier, and prefers a single one.
                </p>
              </div>
              <button
                type="button"
                className="btn-secondary text-xs py-1"
                onClick={() =>
                  setForeignTargets((prev) => [
                    ...prev,
                    { name: '', x: 0, y: 0, crop_per_hour: '', safety_margin_pct: 5 },
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
              <table className="w-full text-xs">
                <thead className="text-secondary uppercase">
                  <tr>
                    <th className="text-left py-1 px-2">Village</th>
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
                        className="group border-t border-gray-800 hover:bg-white/5 focus-within:bg-amber-400/15 transition-colors"
                      >
                        <td className="py-1 px-2 border-l-2 border-l-transparent group-focus-within:border-l-amber-400">
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
                        <td className="px-2 text-right whitespace-nowrap">
                          {incomplete && (
                            <span
                              className="text-amber-300 mr-2"
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
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                aria-pressed={allocView === key}
                className={`text-xs px-3 py-1.5 rounded border transition-colors ${
                  allocView === key
                    ? 'border-amber-400/60 bg-amber-400/15 text-amber-200'
                    : 'border-gray-700 text-secondary hover:text-white hover:border-gray-500'
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
                        {RESOURCE_LABEL[resource]}
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
                                <span className="ml-1 text-[10px] uppercase text-amber-300/80 font-sans">
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
                                    : 'text-amber-300'
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

          {allocView === 'edit' &&
            RESOURCES.map((resource) => {
            const slack = explicitTotal(resource)
            const remainder = remainderFor(resource)
            const settled = Math.abs(slack) < 1 || remainder != null
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
                      <span className={settled ? 'text-success' : 'text-danger'}>
                        {fmt(slack)}/h unassigned
                        {remainder != null
                          ? ` → ${villages.find((v) => v.village_id === remainder)?.name ?? `village ${remainder}`}`
                          : ' · no remainder village set'}
                      </span>
                    </div>
                  </div>
                </div>
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
                        <th className="text-left py-1 px-2">Village</th>
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
                        const ship = a.mode === 'remainder' ? null : target - (own ?? 0)
                        return (
                          <tr
                            key={v.village_id}
                            className={`group border-t border-gray-800 hover:bg-white/5 focus-within:bg-amber-400/15 transition-colors ${
                              isSelected(resource, v.village_id) ? 'bg-amber-400/5' : ''
                            }`}
                          >
                            <td className="text-center px-2 border-l-2 border-l-transparent group-focus-within:border-l-amber-400">
                              <input
                                type="checkbox"
                                aria-label={`Select ${v.name} for batch edit`}
                                checked={isSelected(resource, v.village_id)}
                                onChange={() => toggleSelected(resource, v.village_id)}
                              />
                            </td>
                            <td className="py-1 px-2">{v.name}</td>
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
                                      ? 'text-amber-300'
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
                  <div className={plan.feasible ? 'text-success' : 'text-danger'}>
                    {plan.feasible ? 'Feasible' : 'Not feasible'}
                  </div>
                </div>
              </div>

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
                <h3 className="font-semibold mb-2">
                  Setup sheet <span className="text-secondary">· {activeProfile}</span>
                </h3>
                <table className="w-full text-xs">
                  <thead className="text-secondary uppercase">
                    <tr>
                      <th className="text-left py-1 px-2">From</th>
                      <th className="text-left px-2">To</th>
                      <th className="text-left px-2">Cargo per send</th>
                      <th className="text-right px-2">Cycle</th>
                      <th className="text-right px-2">Create at</th>
                      <th className="text-right px-2">Arrives</th>
                      <th className="text-right px-2">Merchants</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.rows.map((row, i) => (
                      <tr key={i} className="border-t border-gray-800">
                        <td className="py-1 px-2">
                          {/* Server-resolved names: the snapshot cannot know
                              foreign tributes, whose negative ids rendered as
                              "-1" here before the plan carried names itself. */}
                          {row.origin_name ||
                            (villages.find((v) => v.village_id === row.origin)?.name ?? row.origin)}
                        </td>
                        <td className="px-2">
                          {row.destination_name ||
                            (villages.find((v) => v.village_id === row.destination)?.name ??
                              row.destination)}
                        </td>
                        <td className="px-2 font-mono">
                          {Object.entries(row.cargo)
                            .map(([r, v]) => `${RESOURCE_LABEL[r]} ${v.toLocaleString()}`)
                            .join(' · ')}
                        </td>
                        <td className="text-right px-2 font-mono">{row.cycle_hours}h</td>
                        <td className="text-right px-2 font-mono">{row.dispatch}</td>
                        <td className="text-right px-2 font-mono">{row.arrival}</td>
                        <td className="text-right px-2 font-mono">{row.merchants}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="text-secondary text-xs mt-2">
                  A Gold Club route repeats from the moment it is created, so “create at” is the
                  clock time to press create — not a field you can set afterwards.
                </p>
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

              {plan.warnings.length > 0 && (
                <div className="card p-4">
                  <h3 className="font-semibold mb-2 text-amber-300">
                    Warnings ({plan.warnings.length})
                  </h3>
                  <ul className="text-xs list-disc list-inside space-y-0.5 max-h-64 overflow-y-auto">
                    {plan.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
