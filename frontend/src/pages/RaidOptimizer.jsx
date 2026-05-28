import { useEffect, useMemo, useRef, useState } from 'react'
import api from '../api'
import VillageSelector from '../components/VillageSelector'
import { useToast } from '../components/Toast'
import useGameStore from '../stores/gameStore'
import {
  UNITS,
  UNIT_BY_KEY,
  carryOf,
  casualtyResourceCost,
  findCompositions,
  inventoryFromTroopsAPI,
  lossPath,
  smithyAtk,
  smithyFromAPI,
} from '../utils/raidOptimizer'

const LS_KEY = 'raidopt_state_v2'

// Per-unit color tokens — functional indicators, not theme decoration.
// Same hues as the standalone optimizer so memorised colours carry over.
const UNIT_COLOR = {
  c:  '#8a9a4b',  // olive — Clubswinger
  sp: '#8a9aae',  // steel — Spearman
  a:  '#f5b454',  // amber — Axeman
  pa: '#b08acc',  // violet — Paladin
  t:  '#a83232',  // blood — Teutonic Knight
}

// Per-strategy palette — used by the four cards + their alternative sections.
const STRAT_COLOR = {
  balance: '#f5b454', // amber-bright
  raids:   '#d4892c', // amber
  zero:    '#8a9aae', // steel
  death:   '#8a9a4b', // olive
}

const STRATEGY_META = {
  balance: { label: 'BALANCE · TOP PICK', altLabel: 'BALANCE',   altRange: '#2 – #4' },
  raids:   { label: 'MAX RAIDS',          altLabel: 'RAIDS',     altRange: '#2 – #4' },
  zero:    { label: 'ZERO-CAS CEILING',   altLabel: 'ZERO-CAS',  altRange: '#2 – #3' },
  death:   { label: 'CHEAPEST LOSS',      altLabel: 'CHEAP LOSS', altRange: '#2 – #3' },
}

function loadState() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return null
    const obj = JSON.parse(raw)
    if (typeof obj?.defZero !== 'number') return null
    return obj
  } catch { return null }
}

function saveState(state) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(state)) } catch { /* disabled */ }
}

function strategyNote(strategy, v) {
  const raids = <strong>{v.raids}</strong>
  const loss  = <strong>{v.costAtBudget.toLocaleString()}r</strong>
  const zero  = <strong>{v.maxZero}</strong>
  switch (strategy) {
    case 'balance':
      return <>Composite pick · {raids} raids · {loss} loss/raid · 0-cas ceiling {zero}</>
    case 'raids':
      return <>Throws {raids} simultaneous raids · best when most targets sit at DEF ≤ {zero}</>
    case 'zero':
      return <>Highest zero-cas ceiling: {zero} · smallest army that holds it</>
    case 'death':
      return <>Cheapest loss profile: {loss}/raid at budget DEF · minimum deploy</>
    default:
      return null
  }
}

function deadAsEnglish(dead) {
  const labels = { c: 'club', sp: 'spear', a: 'axe', pa: 'paladin', t: 'TK' }
  const parts = []
  for (const u of UNITS) {
    const n = dead[u.key]
    if (n > 0) parts.push(`${n} ${labels[u.key]}${n > 1 ? 's' : ''}`)
  }
  return parts.length ? parts.join(' + ') : '0'
}

function CompositionPills({ p, size = 'lg' }) {
  const parts = UNITS
    .filter((u) => p[u.key] > 0)
    .map((u) => (
      <span key={u.key} className="font-bold" style={{ color: UNIT_COLOR[u.key] }}>
        {p[u.key]}
        <span className="ml-0.5 text-[0.6em] tracking-widest opacity-80">{u.short.toUpperCase()}</span>
      </span>
    ))
  if (parts.length === 0) parts.push(<span key="empty" className="text-secondary">(empty)</span>)
  const cls = {
    xl: 'text-3xl md:text-4xl font-bold tracking-tight leading-tight',
    lg: 'text-2xl font-bold leading-tight',
    md: 'text-xl font-bold leading-tight',
    sm: 'text-base font-bold leading-tight',
  }[size]
  return (
    <div className={cls}>
      {parts.flatMap((node, i) =>
        i === 0
          ? [node]
          : [<span key={`x${i}`} className="mx-1.5 text-secondary opacity-50 font-normal">+</span>, node]
      )}
    </div>
  )
}

function StatBlock({ label, value, sub, valueColor, large = false }) {
  return (
    <div className="bg-surface px-2.5 py-1.5 border-l-2 border-default">
      <div className="text-[0.6rem] tracking-[0.2em] uppercase text-secondary">{label}</div>
      <div
        className={`font-bold leading-tight ${large ? 'text-2xl md:text-3xl' : 'text-lg'}`}
        style={valueColor ? { color: valueColor } : undefined}
      >
        {value}
      </div>
      {sub && <div className="text-[0.6rem] text-secondary mt-0.5 tracking-wide">{sub}</div>}
    </div>
  )
}

// Loss path breakpoint description — rendered as JSX (not innerHTML).
function LossPathDescription({ v, budget, defBudget, atks }) {
  const path = useMemo(
    () => lossPath(v.p, budget, atks).filter((pt) => pt.cost > 0),
    [v.p, budget, atks],
  )

  const segments = [
    <span key="z">Zero losses up to DEF <strong>{v.maxZero}</strong></span>,
  ]

  let markerInserted = false

  for (let i = 0; i < path.length; i++) {
    const pt = path[i]
    const nextDef = i + 1 < path.length ? path[i + 1].def : null
    const isPastBudget = pt.def > defBudget
    const violatesBudget = pt.cost > budget && !isPastBudget

    segments.push(
      <span key={`b${i}`}>
        {isPastBudget && (
          <span className="font-bold uppercase text-[0.65rem] tracking-wider mr-1" style={{ color: '#5a5040', fontStyle: 'italic' }}>
            beyond scope
          </span>
        )}
        {violatesBudget && (
          <span className="font-bold uppercase text-[0.65rem] tracking-wider mr-1" style={{ color: '#a83232' }}>
            ⚠ over budget
          </span>
        )}
        DEF{' '}
        <strong>
          {nextDef ? `${pt.def}–${nextDef - 1}` : `${pt.def}+`}
        </strong>
        : {deadAsEnglish(pt.dead)} dies ({pt.cost.toLocaleString()}r per raid)
      </span>,
    )

    // Insert "at your budget DEF" marker after segment containing it.
    const segEnd = nextDef ? nextDef - 1 : Infinity
    if (!markerInserted && pt.def <= defBudget && segEnd >= defBudget) {
      const c = casualtyResourceCost(v.p, defBudget, atks)
      const ok = c <= budget
      segments.push(
        <span
          key="marker"
          className="inline-block px-1.5 py-0.5 ml-1 text-[0.65rem] font-bold uppercase tracking-wider rounded-sm border"
          style={{
            color: ok ? '#5a8c3a' : '#a83232',
            borderColor: ok ? '#5a8c3a' : '#a83232',
            backgroundColor: ok ? 'rgba(90,140,58,0.10)' : 'rgba(168,50,50,0.10)',
          }}
        >
          ⟵ at your budget DEF ({defBudget}): {c.toLocaleString()}r per raid, {ok ? 'within budget' : 'over budget'}
        </span>,
      )
      markerInserted = true
    }
  }

  if (path.length === 0) {
    return (
      <>
        Zero losses up to DEF <strong>{v.maxZero}</strong>. No further casualties in scanned DEF range.
      </>
    )
  }

  // Comp is overbuilt: no breakpoint within the budget-DEF range.
  if (!markerInserted) {
    const c = casualtyResourceCost(v.p, defBudget, atks)
    if (c === 0) {
      segments.splice(
        1, 0,
        <span
          key="marker-overbuilt"
          className="inline-block px-1.5 py-0.5 ml-1 text-[0.65rem] font-bold uppercase tracking-wider rounded-sm border"
          style={{
            color: '#5a8c3a',
            borderColor: '#5a8c3a',
            backgroundColor: 'rgba(90,140,58,0.10)',
          }}
        >
          ⟵ at your budget DEF ({defBudget}): 0r per raid, no casualties
        </span>,
      )
    }
  }

  return segments.flatMap((node, i) =>
    i === 0 ? [node] : [<span key={`sep${i}`} className="text-secondary opacity-60 mx-1.5">·</span>, node],
  )
}

function StrategyCard({ v, strategy, featured, budget, defBudget, atks }) {
  const accent = STRAT_COLOR[strategy]
  const meta = STRATEGY_META[strategy]
  const diesUnit = v?.dies ? UNIT_BY_KEY[v.dies] : null

  if (!v) {
    return (
      <div className="card text-center py-10 border-dashed">
        <div className="text-2xl mb-2 opacity-50">⚠</div>
        <div className="font-semibold uppercase tracking-wider text-sm">No composition</div>
        <div className="text-xs text-secondary mt-1">No valid pick for this strategy.</div>
      </div>
    )
  }

  const lossTotal = v.costAtBudget * v.raids
  const carryTotal = carryOf(v.p) * v.raids
  const ratio = lossTotal > 0 ? `${(carryTotal / lossTotal).toFixed(1)}×` : '∞'

  const deployStr = UNITS
    .filter((u) => v.p[u.key] > 0)
    .map((u) => `${v.p[u.key] * v.raids}${u.short.toUpperCase()}`)
    .join(' / ')

  const leftParts = UNITS
    .filter((u) => v.left[u.key] > 0)
    .map((u) => (
      <span key={u.key}>
        <span className="font-bold text-primary">{v.left[u.key]}</span>
        {u.short.toUpperCase()}
      </span>
    ))

  return (
    <div
      className="card relative"
      style={{
        borderColor: accent,
        background: featured
          ? `linear-gradient(135deg, ${accent}1A 0%, transparent 65%)`
          : `linear-gradient(135deg, ${accent}14 0%, transparent 60%)`,
        boxShadow: featured ? `0 0 24px ${accent}10` : undefined,
      }}
    >
      <div
        className="absolute -top-2.5 left-4 px-2.5 py-0.5 text-[0.6rem] font-bold tracking-[0.28em] rounded-sm whitespace-nowrap"
        style={{ background: accent, color: '#0c0a08' }}
      >
        {meta.label}
      </div>

      <div className="mb-3 mt-1">
        <CompositionPills p={v.p} size={featured ? 'xl' : 'lg'} />
      </div>

      <div className="text-[0.65rem] tracking-wider uppercase text-secondary mb-3 leading-relaxed">
        {strategyNote(strategy, v)}
      </div>

      <div className={`grid gap-2 mb-3 ${featured ? 'grid-cols-2 md:grid-cols-4' : 'grid-cols-2'}`}>
        <StatBlock label="Raids" value={v.raids} valueColor={accent} sub="simultaneous" large={featured} />
        <StatBlock label="0-Cas DEF" value={v.maxZero} sub="safe ceiling" />
        <StatBlock label="Budget DEF" value={v.maxBudget} sub="loss ≤ budget" />
        <StatBlock label="Cost / Raid" value={`${v.costAtBudget.toLocaleString()}r`} sub="at budget DEF" />
      </div>

      <div
        className="px-3 py-2 text-xs leading-relaxed border-l-2 bg-surface mb-2"
        style={{ borderColor: diesUnit ? UNIT_COLOR[v.dies] : '#5a5040' }}
      >
        <LossPathDescription v={v} budget={budget} defBudget={defBudget} atks={atks} />
      </div>

      <div className="px-3 py-1.5 text-[0.7rem] leading-relaxed text-secondary">
        Wave loss <strong className="text-primary">{lossTotal.toLocaleString()}r</strong>{' '}
        against carry <strong className="text-primary">{carryTotal.toLocaleString()}r</strong>{' '}
        ({ratio} return).
      </div>

      <div className="text-[0.65rem] tracking-wider text-secondary mt-2 leading-relaxed">
        DEPLOYS · {deployStr || '—'} &nbsp;·&nbsp; LEFTOVER ·{' '}
        {leftParts.length === 0
          ? <><span className="font-bold">0</span> all spent</>
          : leftParts.flatMap((node, i) => (i === 0 ? [node] : [<span key={`l${i}`}> · </span>, node]))}
      </div>
    </div>
  )
}

function AlternativeCard({ v, rank }) {
  const diesUnit = v.dies ? UNIT_BY_KEY[v.dies] : null
  const leftParts = UNITS
    .filter((u) => v.left[u.key] > 0)
    .map((u) => (
      <span key={u.key} className="mr-2">
        <span className="font-bold text-primary">{v.left[u.key]}</span>
        {u.short.toUpperCase()}
      </span>
    ))
  return (
    <div className="card relative">
      <div className="absolute top-2.5 right-3 font-bold text-base text-secondary opacity-60">#{rank}</div>
      <div className="mb-3">
        <CompositionPills p={v.p} size="md" />
      </div>
      <div className="grid grid-cols-4 gap-1.5 text-xs mb-2">
        <StatBlock label="Raids" value={v.raids} valueColor="#f5b454" />
        <StatBlock label="0-Cas" value={v.maxZero} />
        <StatBlock label="Budget" value={v.maxBudget} />
        <StatBlock label="Loss/r" value={v.costAtBudget.toLocaleString()} />
      </div>
      <div
        className="px-2 py-1 text-[0.7rem] border-l-2 bg-surface mb-1"
        style={{ borderColor: diesUnit ? UNIT_COLOR[v.dies] : '#5a5040' }}
      >
        First cas →{' '}
        <span
          className="font-bold uppercase tracking-wider"
          style={{ color: diesUnit ? UNIT_COLOR[v.dies] : 'inherit' }}
        >
          {diesUnit ? diesUnit.name : 'NONE'}
        </span>
      </div>
      <div className="text-[0.65rem] tracking-wider text-secondary">
        Leftover: {leftParts.length === 0 ? '—' : leftParts}
      </div>
    </div>
  )
}

function NumberField({ label, color, value, setValue, hint, min = 0, max }) {
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
        max={max}
        onChange={(e) => {
          const n = Number(e.target.value)
          const clamped = Math.max(min, Number.isFinite(n) ? n : min)
          setValue(max != null ? Math.min(max, clamped) : clamped)
        }}
      />
      {hint && <div className="text-[0.6rem] mt-1 tracking-wide opacity-60" style={{ color }}>{hint}</div>}
    </div>
  )
}

export default function RaidOptimizer() {
  const tribeId = useGameStore((s) => s.tribeId)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const toast = useToast()

  const persisted = useMemo(() => loadState(), [])

  // Inventory inputs (Teuton-only).
  const [c,  setC]  = useState(0)
  const [sp, setSp] = useState(0)
  const [a,  setA]  = useState(0)
  const [pa, setPa] = useState(0)
  const [t,  setT]  = useState(0)

  // Smithy levels per unit type (0–20).
  const [smC,  setSmC]  = useState(persisted?.smithy?.c  ?? 0)
  const [smSp, setSmSp] = useState(persisted?.smithy?.sp ?? 0)
  const [smA,  setSmA]  = useState(persisted?.smithy?.a  ?? 0)
  const [smPa, setSmPa] = useState(persisted?.smithy?.pa ?? 0)
  const [smT,  setSmT]  = useState(persisted?.smithy?.t  ?? 0)

  // Defense / budget constraints.
  const [defZero,   setDefZero]   = useState(persisted?.defZero   ?? 330)
  const [defBudget, setDefBudget] = useState(persisted?.defBudget ?? 420)
  const [budget,    setBudget]    = useState(persisted?.budget    ?? 1525)

  useEffect(() => {
    saveState({
      defZero, defBudget, budget,
      smithy: { c: smC, sp: smSp, a: smA, pa: smPa, t: smT },
    })
  }, [defZero, defBudget, budget, smC, smSp, smA, smPa, smT])

  const [fetching, setFetching] = useState(false)
  const autoFilledRef = useRef(false)

  const fetchTroops = async (showToast = true) => {
    if (!activeVillageId) return
    setFetching(true)
    try {
      // Troops + smithy fetched in parallel — both read paths, no shared state.
      const [troopsRes, smithyRes] = await Promise.all([
        api.get(`/military/troops?village_id=${activeVillageId}`),
        api.get(`/military/smithy?village_id=${activeVillageId}`).catch(() => null),
      ])
      const inv = inventoryFromTroopsAPI(troopsRes.data || {})
      setC(inv.c); setSp(inv.sp); setA(inv.a); setPa(inv.pa); setT(inv.t)

      let smithyMsg = ''
      if (smithyRes?.data?.found) {
        const sm = smithyFromAPI(smithyRes.data)
        setSmC(sm.c); setSmSp(sm.sp); setSmA(sm.a); setSmPa(sm.pa); setSmT(sm.t)
        smithyMsg = ' + smithy levels'
      } else if (smithyRes?.data && !smithyRes.data.found) {
        smithyMsg = ' (no smithy built)'
      }

      if (showToast) toast.success(`Loaded current troop counts${smithyMsg}`)
    } catch {
      if (showToast) toast.error('Could not load troops — enter manually')
    } finally {
      setFetching(false)
    }
  }

  useEffect(() => {
    if (autoFilledRef.current || !activeVillageId) return
    autoFilledRef.current = true
    fetchTroops(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVillageId])

  const inv      = useMemo(() => ({ c, sp, a, pa, t }), [c, sp, a, pa, t])
  const smithyLv = useMemo(
    () => ({ c: smC, sp: smSp, a: smA, pa: smPa, t: smT }),
    [smC, smSp, smA, smPa, smT],
  )
  const totalInv     = c + sp + a + pa + t
  const invalidDefs  = defBudget < defZero
  const invalidBudget = budget < 0

  const result = useMemo(() => {
    if (totalInv === 0 || invalidDefs || invalidBudget) {
      return { balance: null, raids: null, zero: null, death: null, atks: null }
    }
    return findCompositions(inv, defZero, defBudget, budget, smithyLv)
  }, [inv, defZero, defBudget, budget, smithyLv, totalInv, invalidDefs, invalidBudget])

  const tribeMismatch = tribeId != null && tribeId !== 2
  const hasResult = !!result.balance

  const totalAlts = hasResult
    ? (result.balance.alts.length + result.raids.alts.length
       + result.zero.alts.length + result.death.alts.length)
    : 0

  // Inventory summary for the verdict line.
  const invStr     = UNITS.filter((u) => inv[u.key] > 0).map((u) => `${inv[u.key]}${u.short.toUpperCase()}`).join(' · ')
  const smithyStr  = UNITS.filter((u) => smithyLv[u.key] > 0).map((u) => `${u.short}:L${smithyLv[u.key]}`).join(' ') || 'no smithy'

  return (
    <div className="p-6 max-w-[1280px] mx-auto">
      {/* Header */}
      <div className="flex justify-between items-end mb-5 flex-wrap gap-3 border-b border-default pb-3">
        <div>
          <h2 className="heading-gold text-2xl">Raid Composition Optimizer</h2>
          <div className="text-xs uppercase tracking-[0.25em] mt-1" style={{ color: STRAT_COLOR.raids }}>
            Teuton · Multi-Strategy Force Designer · K = 1.5 · Smithy-Aware
          </div>
        </div>
        <VillageSelector />
      </div>

      {tribeMismatch && (
        <div className="card mb-4 border-l-4" style={{ borderLeftColor: '#c97a2c' }}>
          <div className="text-sm">
            <strong>Heads up:</strong> the optimizer math is hardcoded for Teuton units
            (Clubswinger / Spearman / Axeman / Paladin / Teutonic Knight). Your active
            tribe is different — results are hypothetical for those troop counts.
          </div>
        </div>
      )}

      {/* Inputs */}
      <div className="card mb-5">
        <div className="flex justify-between items-baseline mb-3 flex-wrap gap-2">
          <h3 className="heading-gold text-base">Input Parameters</h3>
          <button
            className="btn-secondary btn-xs"
            onClick={() => fetchTroops(true)}
            disabled={fetching || !activeVillageId}
            title="Loads troop counts and per-unit smithy levels from the active village"
          >
            {fetching ? 'Loading…' : 'Auto-fill troops + smithy'}
          </button>
        </div>

        <div className="text-[0.6rem] tracking-[0.3em] uppercase text-secondary mb-2 border-b border-dashed border-default pb-1">
          Available Troops
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <NumberField label="Clubswingers" color={UNIT_COLOR.c}  value={c}  setValue={setC}  hint="ATK 40 · 250r · car 60" />
          <NumberField label="Axemen"       color={UNIT_COLOR.a}  value={a}  setValue={setA}  hint="ATK 60 · 490r · car 50" />
          <NumberField label="Spearmen"     color={UNIT_COLOR.sp} value={sp} setValue={setSp} hint="ATK 10 · 340r · car 40" />
          <NumberField label="Paladins"     color={UNIT_COLOR.pa} value={pa} setValue={setPa} hint="ATK 55 · 1005r · car 110" />
          <NumberField label="TKs"          color={UNIT_COLOR.t}  value={t}  setValue={setT}  hint="ATK 150 · 1525r · car 80" />
        </div>

        <div className="text-[0.6rem] tracking-[0.3em] uppercase text-secondary mt-5 mb-2 border-b border-dashed border-default pb-1">
          Smithy Levels <span className="opacity-60 italic tracking-normal normal-case ml-1">// per unit type (0–20)</span>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <NumberField label="Club Smithy"  color={UNIT_COLOR.c}  value={smC}  setValue={setSmC}  max={20}
            hint={`L${smC} → ATK ${smC === 0 ? UNIT_BY_KEY.c.atk : smithyAtk(UNIT_BY_KEY.c, smC).toFixed(1)}`} />
          <NumberField label="Axe Smithy"   color={UNIT_COLOR.a}  value={smA}  setValue={setSmA}  max={20}
            hint={`L${smA} → ATK ${smA === 0 ? UNIT_BY_KEY.a.atk : smithyAtk(UNIT_BY_KEY.a, smA).toFixed(1)}`} />
          <NumberField label="Spear Smithy" color={UNIT_COLOR.sp} value={smSp} setValue={setSmSp} max={20}
            hint={`L${smSp} → ATK ${smSp === 0 ? UNIT_BY_KEY.sp.atk : smithyAtk(UNIT_BY_KEY.sp, smSp).toFixed(1)}`} />
          <NumberField label="Pal Smithy"   color={UNIT_COLOR.pa} value={smPa} setValue={setSmPa} max={20}
            hint={`L${smPa} → ATK ${smPa === 0 ? UNIT_BY_KEY.pa.atk : smithyAtk(UNIT_BY_KEY.pa, smPa).toFixed(1)}`} />
          <NumberField label="TK Smithy"    color={UNIT_COLOR.t}  value={smT}  setValue={setSmT}  max={20}
            hint={`L${smT} → ATK ${smT === 0 ? UNIT_BY_KEY.t.atk : smithyAtk(UNIT_BY_KEY.t, smT).toFixed(1)}`} />
        </div>

        <div className="text-[0.6rem] tracking-[0.3em] uppercase text-secondary mt-5 mb-2 border-b border-dashed border-default pb-1">
          Defense Constraints
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 max-w-3xl">
          <NumberField label="Zero-Cas DEF" color="#5a8c3a" value={defZero}   setValue={setDefZero}   hint="No losses up to this DEF" />
          <NumberField label="Budget DEF"   color="#c97a2c" value={defBudget} setValue={setDefBudget} hint="Losses ≤ budget at this DEF" min={1} />
          <NumberField label="Budget (r)"   color="#c97a2c" value={budget}    setValue={setBudget}    hint="Max resource loss per raid" />
        </div>

        <div
          className="mt-4 px-3 py-2 text-xs border-l-2 bg-surface leading-relaxed"
          style={{ borderColor: STRAT_COLOR.raids }}
        >
          {totalInv === 0 ? (
            <><strong>Empty inventory.</strong> Enter at least one troop count or auto-fill.</>
          ) : invalidDefs ? (
            <><strong>Invalid:</strong> Budget DEF ({defBudget}) must be ≥ Zero-Cas DEF ({defZero}).</>
          ) : !hasResult ? (
            <>
              Inventory: <strong>{invStr}</strong> &nbsp;|&nbsp;
              Smithy: <strong>{smithyStr}</strong> &nbsp;|&nbsp;
              0-cas ≤ DEF <strong>{defZero}</strong>, ≤<strong>{budget.toLocaleString()}r</strong> loss at DEF <strong>{defBudget}</strong>{' '}
              &nbsp;|&nbsp; <span style={{ color: '#a83232' }}>No composition satisfies both constraints.</span>
            </>
          ) : (
            <>
              Inventory: <strong>{invStr}</strong> &nbsp;|&nbsp;
              Smithy: <strong>{smithyStr}</strong> &nbsp;|&nbsp;
              0-cas ≤ DEF <strong>{defZero}</strong>, ≤<strong>{budget.toLocaleString()}r</strong> loss at DEF <strong>{defBudget}</strong>{' '}
              &nbsp;|&nbsp; <strong style={{ color: STRAT_COLOR.balance }}>4 strategies</strong> + {totalAlts} alternatives.
            </>
          )}
        </div>
      </div>

      {/* Primary results */}
      <div className="flex justify-between items-baseline mb-4 border-b border-default pb-2">
        <h3 className="heading-gold text-lg" style={{ color: STRAT_COLOR.balance }}>OPTIMAL DEPLOYMENTS</h3>
        {hasResult && (
          <span className="text-[0.65rem] tracking-[0.15em] uppercase text-secondary font-mono">
            4 strategies{totalAlts > 0 ? ` · ${totalAlts} alts` : ''}
          </span>
        )}
      </div>

      {hasResult ? (
        <>
          <div className="mb-5">
            <StrategyCard v={result.balance.top} strategy="balance" featured
              budget={budget} defBudget={defBudget} atks={result.atks} />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
            <StrategyCard v={result.raids.top} strategy="raids"
              budget={budget} defBudget={defBudget} atks={result.atks} />
            <StrategyCard v={result.zero.top}  strategy="zero"
              budget={budget} defBudget={defBudget} atks={result.atks} />
            <StrategyCard v={result.death.top} strategy="death"
              budget={budget} defBudget={defBudget} atks={result.atks} />
          </div>
        </>
      ) : (
        <div className="card text-center py-12 border-dashed">
          <div className="text-3xl mb-2 opacity-50">⚠</div>
          <div className="font-semibold uppercase tracking-wider">No valid composition found</div>
          <div className="text-xs text-secondary mt-1">
            Try lowering Zero-Cas / Budget DEF, raising budget, or check inventory.
          </div>
        </div>
      )}

      {/* Alternatives */}
      {hasResult && totalAlts > 0 && (
        <>
          <div className="flex justify-between items-baseline mb-4 mt-2 border-b border-default pb-2">
            <h3 className="heading-gold text-lg" style={{ color: STRAT_COLOR.balance }}>ALTERNATIVES</h3>
            <span className="text-[0.65rem] tracking-[0.15em] uppercase text-secondary font-mono">
              Ranks 2 – 4 per strategy
            </span>
          </div>
          {['balance', 'raids', 'zero', 'death'].map((key) => {
            const section = result[key]
            if (!section?.alts?.length) return null
            const meta = STRATEGY_META[key]
            return (
              <div className="mb-5" key={key}>
                <div className="flex items-center gap-3 mb-2 border-b border-default pb-1">
                  <span
                    className="px-2 py-0.5 text-[0.6rem] font-bold tracking-[0.25em] rounded-sm"
                    style={{ background: STRAT_COLOR[key], color: '#0c0a08' }}
                  >
                    {meta.altLabel}
                  </span>
                  <span className="text-[0.6rem] tracking-[0.18em] uppercase text-secondary">
                    Ranks {meta.altRange}
                  </span>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                  {section.alts.map((alt, i) => (
                    <AlternativeCard key={`${key}-${i}`} v={alt} rank={i + 2} />
                  ))}
                </div>
              </div>
            )
          })}
        </>
      )}

      <div className="mt-8 pt-4 border-t border-default text-[0.65rem] tracking-wider text-secondary leading-relaxed font-mono">
        <div>// Combat model · OFF = Σ(N × ATK) · loss = x/(1+x) where x = (DEF/OFF)<sup>1.5</sup></div>
        <div>// Casualties per unit type = round(count × loss) · Lone attacker rule: solo unit with ATK&lt;83 always dies</div>
        <div>// Smithy formula · improved_atk = BASE + (BASE + 300×UPK/7) × (1.007<sup>L</sup> − 1) · L20 ≈ +26–31% depending on upkeep</div>
      </div>
    </div>
  )
}
