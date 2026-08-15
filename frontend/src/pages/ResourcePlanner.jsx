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
const LS_ALLOCATIONS = 'planner_allocations'
const LS_SNAPSHOT = 'planner_snapshot'
const LS_MERCHANT = 'planner_merchant_model'

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
  return (
    <div className="flex items-center gap-2">
      <div
        className="h-2 w-28 rounded bg-black/40 overflow-hidden"
        role="img"
        aria-label={`${budget.committed} of ${budget.spare} merchants committed`}
      >
        <div
          className={`h-full ${budget.over_budget ? 'bg-red-500' : 'bg-emerald-500'}`}
          style={{ width: `${usedPct}%` }}
        />
      </div>
      <span className="font-mono text-xs">
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
    </div>
  )
}

export default function ResourcePlanner() {
  const toast = useToast()
  const serverUrl = useGameStore((s) => s.serverUrl)
  const playerName = useGameStore((s) => s.playerName)
  const accountKey = serverUrl && playerName ? `${serverUrl}|${playerName}` : null
  const [stage, setStage] = useState('snapshot')
  const [snapshot, setSnapshot] = useState(null)
  const [tradeOffice, setTradeOffice] = useState({})
  const [allocations, setAllocations] = useState({})
  const [merchantModel, setMerchantModel] = useState(DEFAULT_MERCHANT_MODEL)
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
    return s.serverUrl && s.playerName ? `${s.serverUrl}|${s.playerName}` : null
  }, [])

  useEffect(() => {
    if (!accountKey) {
      // Disconnected: the page stays routable, so showing (or planning from)
      // the previous account's villages would act on stale data.
      setSnapshot(null)
      setTradeOffice({})
      setAllocations({})
      setMerchantModel(DEFAULT_MERCHANT_MODEL)
      setPlan(null)
      setHydratedKey(null)
      return
    }
    setSnapshot(loadJson(`${LS_SNAPSHOT}::${accountKey}`, null))
    setTradeOffice(loadJson(`${LS_TRADE_OFFICE}::${accountKey}`, {}))
    setAllocations(loadJson(`${LS_ALLOCATIONS}::${accountKey}`, {}))
    setMerchantModel(loadJson(`${LS_MERCHANT}::${accountKey}`, DEFAULT_MERCHANT_MODEL))
    setPlan(null)
    setHydratedKey(accountKey)
  }, [accountKey])

  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_TRADE_OFFICE), tradeOffice)
  }, [tradeOffice, hydratedKey, accountKey, storageKey])
  useEffect(() => {
    if (hydratedKey && hydratedKey === accountKey) saveJson(storageKey(LS_ALLOCATIONS), allocations)
  }, [allocations, hydratedKey, accountKey, storageKey])
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
      // Villages get lost, chiefed or renamed between fetches; allocations kept
      // for ids no longer in the snapshot would fail every future plan call.
      const ids = new Set(res.data.villages.map((v) => v.village_id))
      setAllocations((prev) => {
        const pruned = {}
        for (const [resource, per] of Object.entries(prev)) {
          const kept = Object.fromEntries(
            Object.entries(per).filter(([vid]) => ids.has(Number(vid)))
          )
          if (Object.keys(kept).length) pruned[resource] = kept
        }
        return pruned
      })
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
        // Geometry defaults to the snapshot (map span + tribe-derived x1
        // merchant speed) but the operator can override both for non-Europe 2
        // worlds (x2/x3 speed, larger maps) — no extra Travian requests.
        map_span: Number(merchantModel.map_span) || snapshot?.map_span,
        speed_fields_per_hour:
          Number(merchantModel.speed_fields_per_hour) || snapshot?.speed_fields_per_hour,
        merchant_base_capacity: Number(merchantModel.base_capacity) || undefined,
        trade_office_bonus_per_level: Number(merchantModel.bonus_per_to_level) || undefined,
      })
      if (requestedFor !== currentAccountKey()) return
      setPlan(res.data)
      setStage('plan')
    } catch (err) {
      toast.error(errorDetail(err, 'Could not build a plan'))
    } finally {
      setPlanning(false)
    }
  }, [villages, tradeOffice, allocations, toast, accountKey, currentAccountKey, snapshot, merchantModel])

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
                <tr key={v.village_id} className="border-t border-gray-800">
                  <td className="py-1.5 px-2">
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
        </div>
      )}

      {stage === 'allocate' && villages.length > 0 && (
        <div className="space-y-4">
          {RESOURCES.map((resource) => {
            const slack = explicitTotal(resource)
            const remainder = remainderFor(resource)
            const settled = Math.abs(slack) < 1 || remainder != null
            return (
              <div key={resource} className="card p-4">
                <div className="flex justify-between items-baseline mb-2 flex-wrap gap-2">
                  <h3 className="font-semibold">{RESOURCE_LABEL[resource]}</h3>
                  <div className="text-xs">
                    <span className="text-secondary">
                      total {fmt(totals[resource].total)}/h
                      {!totals[resource].known && ' (some villages unknown)'} ·{' '}
                    </span>
                    <span className={settled ? 'text-success' : 'text-danger'}>
                      {fmt(slack)}/h unassigned
                      {remainder != null
                        ? ` → village ${remainder}`
                        : ' · no remainder village set'}
                    </span>
                  </div>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="text-secondary uppercase">
                      <tr>
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
                          <tr key={v.village_id} className="border-t border-gray-800">
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
                      <div key={b.village_id} className="flex items-center gap-3 text-xs">
                        <span className="w-28 truncate">
                          {villages.find((v) => v.village_id === b.village_id)?.name ??
                            b.village_id}
                        </span>
                        <BudgetBar budget={b} />
                      </div>
                    ))}
                </div>
              </div>

              <div className="card p-4 overflow-x-auto">
                <h3 className="font-semibold mb-2">Setup sheet</h3>
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
                          {villages.find((v) => v.village_id === row.origin)?.name ?? row.origin}
                        </td>
                        <td className="px-2">
                          {villages.find((v) => v.village_id === row.destination)?.name ??
                            row.destination}
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
                        {RESOURCE_LABEL[s.resource]} · village {s.village_id} short{' '}
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
