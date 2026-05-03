import { useEffect, useMemo, useRef, useState } from 'react'
import api from '../api'
import VillageSelector from '../components/VillageSelector'
import { useToast } from '../components/Toast'
import useGameStore from '../stores/gameStore'
import {
  UNITS,
  UNIT_BY_KEY,
  findCompositions,
  inventoryFromTroopsAPI,
} from '../utils/raidOptimizer'

const LS_DEFS = 'raidopt_defs'

// Per-unit color tokens — kept on this page (not in the global theme)
// because they're functional indicators, not decoration. Same hues as
// the standalone optimizer so users who already memorized the colors
// don't have to relearn.
const UNIT_COLOR = {
  c:  '#8a9a4b',  // olive — Clubswinger
  sp: '#8a9aae',  // steel — Spearman
  a:  '#f5b454',  // amber — Axeman
  pa: '#b08acc',  // violet — Paladin
  t:  '#a83232',  // blood — Teutonic Knight
}

function loadDefs() {
  try {
    const raw = localStorage.getItem(LS_DEFS)
    if (!raw) return null
    const obj = JSON.parse(raw)
    if (typeof obj?.defZero === 'number' && typeof obj?.defOne === 'number') return obj
  } catch { /* fall through */ }
  return null
}

function saveDefs(defZero, defOne) {
  try {
    localStorage.setItem(LS_DEFS, JSON.stringify({ defZero, defOne }))
  } catch { /* localStorage may be disabled */ }
}

function CompositionDisplay({ p, large = false }) {
  const parts = UNITS
    .filter((u) => p[u.key] > 0)
    .map((u) => (
      <span key={u.key} style={{ color: UNIT_COLOR[u.key] }} className="font-bold">
        {p[u.key]}
        <span className="ml-0.5 text-[0.6em] tracking-widest opacity-90">
          {u.key.toUpperCase()}
        </span>
      </span>
    ))
  if (parts.length === 0) parts.push(<span key="empty" className="text-secondary">(empty)</span>)
  const cls = large
    ? 'text-3xl md:text-4xl font-bold tracking-tight leading-tight'
    : 'text-xl font-bold leading-tight'
  return (
    <div className={cls}>
      {parts.flatMap((node, i) =>
        i === 0
          ? [node]
          : [<span key={`x${i}`} className="mx-1 text-secondary opacity-60 font-normal">+</span>, node]
      )}
    </div>
  )
}

function StatBlock({ label, value, sub, valueColor }) {
  return (
    <div>
      <div className="text-[0.65rem] tracking-[0.2em] uppercase text-secondary mb-1">{label}</div>
      <div className="text-2xl font-bold" style={valueColor ? { color: valueColor } : undefined}>
        {value}
      </div>
      {sub && <div className="text-xs text-secondary mt-0.5">{sub}</div>}
    </div>
  )
}

function PrimaryResult({ v }) {
  if (!v) {
    return (
      <div className="card text-center py-12 border-dashed">
        <div className="text-3xl mb-2 opacity-50">⚠</div>
        <div className="font-semibold">No valid composition found</div>
        <div className="text-xs text-secondary mt-1">Try lower DEF targets or check inventory.</div>
      </div>
    )
  }

  const diesUnit = v.dies ? UNIT_BY_KEY[v.dies] : null
  const lossUnitCost = diesUnit ? diesUnit.cost : 0
  const deployed = v.n * v.raids
  const leftoverTotal = v.left.c + v.left.sp + v.left.a + v.left.pa + v.left.t
  const showSurplus = leftoverTotal > deployed * 1.5 && leftoverTotal > 50

  const deployStr = UNITS
    .filter((u) => v.p[u.key] > 0)
    .map((u) => `${v.p[u.key] * v.raids}${u.key.toUpperCase()}`)
    .join(' / ')

  const leftStr = UNITS
    .filter((u) => v.left[u.key] > 0 || v.p[u.key] > 0)
    .map((u) => (
      <span key={u.key}>
        <span className="font-bold text-primary">{v.left[u.key]}</span>
        {u.key.toUpperCase()}
      </span>
    ))

  return (
    <div className="card relative" style={{ borderColor: '#d4892c' }}>
      <div
        className="absolute -top-2.5 left-4 px-2.5 py-0.5 text-[0.6rem] font-bold tracking-[0.3em] rounded-sm"
        style={{ background: '#d4892c', color: '#0c0a08' }}
      >
        OPTIMAL
      </div>

      <div className="mb-4">
        <CompositionDisplay p={v.p} large />
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 border-t border-default border-dashed pt-4">
        <StatBlock
          label="Parallel Raids"
          value={v.raids}
          valueColor="#f5b454"
          sub="simultaneous deployments"
        />
        <StatBlock
          label="Max DEF · 0 Cas"
          value={v.maxZero}
          sub="zero losses ceiling"
        />
        <StatBlock
          label="Max DEF · 1 Cas"
          value={v.maxOne}
          sub="single loss ceiling"
        />
        <StatBlock
          label="Per-Raid OFF"
          value={v.off.toLocaleString()}
          sub={`${v.cost.toLocaleString()}r force`}
        />
      </div>

      <div
        className="mt-4 px-3 py-2 text-xs leading-relaxed border-l-2 bg-surface"
        style={{ borderColor: '#c97a2c' }}
      >
        Above DEF <strong>{v.maxZero}</strong>, the first to die is{' '}
        <span
          className="font-bold uppercase tracking-wider"
          style={{ color: diesUnit ? UNIT_COLOR[v.dies] : 'inherit' }}
        >
          {diesUnit ? diesUnit.name : 'NONE'}
        </span>
        {' '}({lossUnitCost.toLocaleString()}r per loss). Up to DEF{' '}
        <strong>{v.maxOne}</strong> only 1 unit dies; beyond, additional types start falling.
      </div>

      <div className="text-[0.65rem] tracking-wider text-secondary mt-3">
        DEPLOYS · {deployStr} &nbsp;·&nbsp; LEFTOVER ·{' '}
        {leftStr.length === 0 ? '0' : leftStr.flatMap((node, i) => i === 0 ? [node] : [<span key={`s${i}`}> · </span>, node])}
      </div>

      {showSurplus && (
        <div
          className="mt-3 px-3 py-2 text-xs leading-relaxed border-l-2 bg-surface"
          style={{ borderColor: '#8a9a4b' }}
        >
          Large surplus remains.
          {' '}Consider a second wave with a different composition — see{' '}
          <em className="not-italic uppercase tracking-wider text-primary">Alternatives</em>{' '}
          for swarm options.
        </div>
      )}
    </div>
  )
}

function AlternativeCard({ v }) {
  const diesUnit = v.dies ? UNIT_BY_KEY[v.dies] : null
  const leftStr = UNITS
    .filter((u) => v.left[u.key] > 0)
    .map((u) => (
      <span key={u.key} className="mr-2">
        <span className="font-bold text-primary">{v.left[u.key]}</span>
        {u.key.toUpperCase()}
      </span>
    ))
  return (
    <div className="card hover:border-default transition-colors">
      <div className="mb-3">
        <CompositionDisplay p={v.p} />
      </div>
      <div className="grid grid-cols-4 gap-2 text-xs border-t border-default border-dashed pt-2.5">
        <StatBlock label="Raids" value={v.raids} valueColor="#f5b454" />
        <StatBlock label="0-Cas DEF" value={v.maxZero} />
        <StatBlock label="1-Cas DEF" value={v.maxOne} />
        <StatBlock label="OFF" value={v.off} />
      </div>
      <div className="mt-3 text-[0.65rem] tracking-wider uppercase text-secondary">
        First Cas →{' '}
        <span
          className="font-bold border px-1.5 py-0.5"
          style={{
            color: diesUnit ? UNIT_COLOR[v.dies] : 'inherit',
            borderColor: diesUnit ? UNIT_COLOR[v.dies] : 'currentColor',
          }}
        >
          {diesUnit ? diesUnit.name.toUpperCase() : 'NONE'}
        </span>
      </div>
      <div className="text-[0.65rem] tracking-wider text-secondary mt-2">
        Leftover: {leftStr.length === 0 ? '—' : leftStr}
      </div>
    </div>
  )
}

function NumberField({ label, color, value, setValue, hint, min = 0 }) {
  return (
    <div>
      <label className="block text-[0.6rem] tracking-[0.18em] uppercase mb-1.5" style={{ color }}>
        {label}
      </label>
      <input
        type="number"
        className="input-field font-bold text-base"
        style={{ color }}
        value={value}
        min={min}
        onChange={(e) => setValue(Math.max(min, Number(e.target.value) || min))}
      />
      {hint && <div className="text-[0.6rem] mt-1 tracking-wide opacity-60" style={{ color }}>{hint}</div>}
    </div>
  )
}

export default function RaidOptimizer() {
  const tribeId = useGameStore((s) => s.tribeId)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const toast = useToast()

  // Inventory inputs (Teuton-only for now)
  const [c, setC]   = useState(0)
  const [sp, setSp] = useState(0)
  const [a, setA]   = useState(0)
  const [pa, setPa] = useState(0)
  const [t, setT]   = useState(0)

  // Defense thresholds — persisted in localStorage so the user doesn't
  // re-enter their target's typical DEF on every visit.
  const persistedDefs = useMemo(() => loadDefs(), [])
  const [defZero, setDefZero] = useState(persistedDefs?.defZero ?? 330)
  const [defOne, setDefOne]   = useState(persistedDefs?.defOne ?? 420)
  useEffect(() => { saveDefs(defZero, defOne) }, [defZero, defOne])

  const [fetching, setFetching] = useState(false)
  const autoFilledRef = useRef(false)

  const fetchTroops = async (showToast = true) => {
    if (!activeVillageId) return
    setFetching(true)
    try {
      const res = await api.get(`/military/troops?village_id=${activeVillageId}`)
      const inv = inventoryFromTroopsAPI(res.data || {})
      setC(inv.c); setSp(inv.sp); setA(inv.a); setPa(inv.pa); setT(inv.t)
      if (showToast) toast.success('Loaded current troop counts')
    } catch {
      if (showToast) toast.error('Could not load troops — enter manually')
    } finally {
      setFetching(false)
    }
  }

  // Auto-fetch once on first mount when we have an active village.
  useEffect(() => {
    if (autoFilledRef.current || !activeVillageId) return
    autoFilledRef.current = true
    fetchTroops(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVillageId])

  const inv = useMemo(() => ({ c, sp, a, pa, t }), [c, sp, a, pa, t])
  const totalInv = c + sp + a + pa + t
  const invalidDefs = defOne < defZero

  const results = useMemo(() => {
    if (totalInv === 0 || invalidDefs) return []
    return findCompositions(inv, defZero, defOne)
  }, [inv, defZero, defOne, totalInv, invalidDefs])

  const tribeMismatch = tribeId != null && tribeId !== 2

  return (
    <div className="p-6 max-w-[1180px] mx-auto">
      <div className="flex justify-between items-center mb-5 flex-wrap gap-3">
        <div>
          <h2 className="heading-gold text-2xl">Raid Composition Optimizer</h2>
          <div className="text-xs text-secondary uppercase tracking-[0.25em] mt-0.5">
            Teuton · Casualty-Bounded Force Designer · K = 1.5
          </div>
        </div>
        <VillageSelector />
      </div>

      {tribeMismatch && (
        <div className="card mb-4 border-l-4" style={{ borderLeftColor: '#c97a2c' }}>
          <div className="text-sm">
            <strong>Heads up:</strong> the optimizer math is hardcoded for Teuton units
            (Clubswinger / Spearman / Axeman / Paladin / Teutonic Knight). Your active
            tribe is different — the result is purely hypothetical for those troop counts.
          </div>
        </div>
      )}

      <div className="card mb-5">
        <div className="flex justify-between items-baseline mb-3 flex-wrap gap-2">
          <h3 className="heading-gold text-base">Available Troops</h3>
          <button
            className="btn-secondary btn-xs"
            onClick={() => fetchTroops(true)}
            disabled={fetching || !activeVillageId}
          >
            {fetching ? 'Loading…' : 'Auto-fill from current village'}
          </button>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <NumberField label="Clubswingers" color={UNIT_COLOR.c}  value={c}  setValue={setC}  hint="ATK 40 · 250r · car 60" />
          <NumberField label="Axemen"       color={UNIT_COLOR.a}  value={a}  setValue={setA}  hint="ATK 60 · 490r · car 50" />
          <NumberField label="Spearmen"     color={UNIT_COLOR.sp} value={sp} setValue={setSp} hint="ATK 10 · 340r · car 40" />
          <NumberField label="Paladins"     color={UNIT_COLOR.pa} value={pa} setValue={setPa} hint="ATK 55 · 1005r · car 110" />
          <NumberField label="TKs"          color={UNIT_COLOR.t}  value={t}  setValue={setT}  hint="ATK 150 · 1525r · car 80" />
        </div>

        <h3 className="heading-gold text-base mt-5 mb-3">Defense Constraints</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 max-w-xl">
          <NumberField label="Zero-Cas DEF" color="#5a8c3a" value={defZero} setValue={setDefZero} hint="No losses up to this DEF" />
          <NumberField label="1-Cas DEF"    color="#c97a2c" value={defOne}  setValue={setDefOne}  hint="Up to 1 loss at this DEF" min={1} />
        </div>

        <div
          className="mt-4 px-3 py-2 text-xs border-l-2 bg-surface"
          style={{ borderColor: '#d4892c' }}
        >
          {totalInv === 0 ? (
            <><strong>Empty inventory.</strong> Enter at least one troop count or auto-fill.</>
          ) : invalidDefs ? (
            <><strong>Invalid:</strong> 1-Cas DEF ({defOne}) must be ≥ Zero-Cas DEF ({defZero}).</>
          ) : results.length === 0 ? (
            <>Constraints: 0 cas ≤ DEF <strong>{defZero}</strong>, ≤1 cas ≤ DEF <strong>{defOne}</strong>{' '}
            · <span style={{ color: '#a83232' }}>No composition satisfies both constraints.</span></>
          ) : (
            <>Constraints: 0 cas ≤ DEF <strong>{defZero}</strong>, ≤1 cas ≤ DEF <strong>{defOne}</strong>{' '}
            · <strong style={{ color: '#f5b454' }}>{results.length}</strong> viable compositions found.</>
          )}
        </div>
      </div>

      <div className="mb-3 flex justify-between items-baseline">
        <h3 className="heading-gold text-lg">Optimal Deployment</h3>
        {results.length > 1 && (
          <span className="text-[0.65rem] tracking-[0.15em] uppercase text-secondary">
            {results.length - 1} alternatives
          </span>
        )}
      </div>

      <PrimaryResult v={results[0] || null} />

      {results.length > 1 && (
        <>
          <div className="mt-6 mb-3 flex justify-between items-baseline">
            <h3 className="heading-gold text-lg">Alternative Compositions</h3>
            <span className="text-[0.65rem] tracking-[0.15em] uppercase text-secondary">
              ranked by raid count, then casualty cost
            </span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {results.slice(1).map((v, i) => <AlternativeCard key={i} v={v} />)}
          </div>
        </>
      )}

      <div className="mt-8 pt-4 border-t border-default text-[0.65rem] tracking-wider text-secondary leading-relaxed">
        <div>// Combat model · OFF = Σ(N × ATK) · loss = x/(1+x) where x = (DEF/OFF)<sup>K</sup></div>
        <div>// Casualties per unit type = round(count × loss) · Lone-attacker rule: solo unit with ATK&lt;83 always dies (only solo TKs survive)</div>
        <div>// Stats are <code>BASE</code> (no smithy). Real OFF scales by ~0.7%/level on attacking unit.</div>
      </div>
    </div>
  )
}
