import { useState, useRef, useCallback, useMemo, useEffect } from 'react'
import { createWebSocket } from '../ws'
import { useToast } from '../components/Toast'
import WebSocketPanel from '../components/WebSocketPanel'
import VillageSelector from '../components/VillageSelector'
import useGameStore from '../stores/gameStore'
import api from '../api'

// ── localStorage helpers ─────────────────────────────────────────────
const LS_KEY_ALLIANCES = 'autoscout_exclude_alliances'
const LS_KEY_PLAYERS = 'autoscout_exclude_players'

function loadJson(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback } catch { return fallback }
}

// ── Scan Progress Panel (shown during WS scan) ─────────────────────────
function ScanProgressPanel({ phase, messages, enrichProgress, stats }) {
  const scrollRef = useRef(null)
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [messages])

  const pct = enrichProgress ? Math.round((enrichProgress.index / enrichProgress.total) * 100) : 0

  return (
    <div className="mt-4">
      {phase && (
        <div className="flex items-center gap-2 mb-3">
          <div className="spinner spinner-sm" />
          <span className="text-sm text-gold font-semibold">{phase}</span>
        </div>
      )}
      {enrichProgress && (
        <div className="mb-3">
          <div className="flex justify-between text-xs text-secondary mb-1">
            <span>Enriching tile {enrichProgress.index}/{enrichProgress.total}{enrichProgress.name ? ` — ${enrichProgress.name}` : ''}</span>
            <span>{pct}%{enrichProgress.eta ? ` | ETA: ${enrichProgress.eta}` : ''}</span>
          </div>
          <div className="progress-track"><div className="progress-fill" style={{ width: `${pct}%` }} /></div>
        </div>
      )}
      <div ref={scrollRef} className="ws-panel" style={{ maxHeight: 200 }}>
        {messages.map((msg, i) => (
          <div key={i} className={`ws-panel-line ${msg.type === 'success' ? 'text-success' : msg.type === 'error' ? 'text-danger' : msg.type === 'detail' ? 'text-secondary' : 'text-primary'}`}>
            <span className="ws-panel-time">[{new Date(msg.ts).toLocaleTimeString('en-US', { hour12: false })}]</span>
            {msg.text}
          </div>
        ))}
      </div>
      {stats && (
        <div className="mt-2 flex gap-4 text-xs text-secondary flex-wrap">
          <span>Raw tiles: {stats.raw_tiles}</span>
          <span>After pre-filter: {stats.after_prefilter}</span>
          <span>Final: {stats.final}</span>
          <span>Enrich time: {stats.enrich_time_seconds}s (avg {stats.avg_enrich_time}s/tile)</span>
          <span>Total: {stats.time_seconds}s</span>
        </div>
      )}
    </div>
  )
}

// ── Scan Config Panel ─────────────────────────────────────────────────
function ScanConfigPanel({ onScanComplete, scanning, setScanning, onConfigChange, activeVillageId }) {
  const [radius, setRadius] = useState(10)
  const [minPop, setMinPop] = useState(0)
  const [maxPop, setMaxPop] = useState(100)
  const [maxPlayerPop, setMaxPlayerPop] = useState('')
  const [showOases, setShowOases] = useState(false)

  // Alliance & player exclusion — persisted in localStorage
  const [excludeAlliances, setExcludeAlliances] = useState(() => loadJson(LS_KEY_ALLIANCES, []))
  const [excludePlayers, setExcludePlayers] = useState(() => loadJson(LS_KEY_PLAYERS, []))
  const [newAlliance, setNewAlliance] = useState('')
  const [newPlayer, setNewPlayer] = useState('')

  // Scan progress state
  const [scanPhase, setScanPhase] = useState(null)
  const [scanMessages, setScanMessages] = useState([])
  const [enrichProgress, setEnrichProgress] = useState(null)
  const [scanStats, setScanStats] = useState(null)
  const wsRef = useRef(null)
  const mountedRef = useRef(true)

  useEffect(() => { return () => { mountedRef.current = false; if (wsRef.current) { try { wsRef.current.close() } catch {} } } }, [])

  // Persist to localStorage on change
  useEffect(() => { localStorage.setItem(LS_KEY_ALLIANCES, JSON.stringify(excludeAlliances)) }, [excludeAlliances])
  useEffect(() => { localStorage.setItem(LS_KEY_PLAYERS, JSON.stringify(excludePlayers)) }, [excludePlayers])

  const toast = useToast()

  const addScanMsg = useCallback((type, text) => {
    setScanMessages((prev) => [...prev, { type, text, ts: Date.now() }])
  }, [])

  const addAlliance = () => {
    const parts = newAlliance.split(',').map(s => s.trim()).filter(Boolean)
    if (parts.length === 0) return
    const newList = [...excludeAlliances]
    for (const v of parts) {
      if (!newList.includes(v)) newList.push(v)
    }
    setExcludeAlliances(newList)
    setNewAlliance('')
  }

  const addPlayer = () => {
    const parts = newPlayer.split(',').map(s => s.trim()).filter(Boolean)
    if (parts.length === 0) return
    const newList = [...excludePlayers]
    for (const v of parts) {
      if (!newList.includes(v)) newList.push(v)
    }
    setExcludePlayers(newList)
    setNewPlayer('')
  }

  const handleScan = () => {
    setScanning(true)
    setScanMessages([])
    setScanPhase('Connecting...')
    setEnrichProgress(null)
    setScanStats(null)

    const config = { radius, minPop, maxPop, maxPlayerPop, showOases, excludeAlliances, excludePlayers }
    onConfigChange?.(config)

    const body = {
      radius,
      min_pop: minPop,
      max_pop: maxPop,
      show_oases: showOases,
      exclude_player_names: excludePlayers.flatMap((p) => p.split(',').map(s => s.trim())).filter(Boolean),
      village_id: activeVillageId || undefined,
    }
    if (maxPlayerPop !== '') body.max_player_pop = Number(maxPlayerPop)

    const allAlliances = excludeAlliances.flatMap((a) => a.split(',').map(s => s.trim())).filter(Boolean)
    const allianceIds = allAlliances.filter((a) => /^\d+$/.test(a)).map(Number)
    const allianceNames = allAlliances.filter((a) => !/^\d+$/.test(a))
    if (allianceIds.length > 0) body.exclude_alliance_ids = allianceIds
    if (allianceNames.length > 0) body.exclude_alliance_names = allianceNames

    const PHASE_LABELS = {
      map_scan: 'Scanning map regions...',
      map_scan_done: 'Map scan complete',
      pre_filter: 'Filtering tiles...',
      enriching: 'Enriching tile details...',
      enrich_done: 'Enrichment complete',
      player_pop: 'Querying player populations...',
      player_pop_done: 'Player populations loaded',
      post_filter: 'Applying filters...',
    }

    const ws = createWebSocket('/ws/scout/scan',
      (data) => {
        if (!mountedRef.current) return
        switch (data.type) {
          case 'phase':
            setScanPhase(PHASE_LABELS[data.phase] || data.phase)
            addScanMsg(data.phase?.includes('done') || data.phase?.includes('complete') ? 'success' : 'info', data.message)
            if (data.detail) addScanMsg('detail', data.detail)
            break
          case 'scan_region':
            addScanMsg('detail', `  Fetching map region ${data.index}/${data.total} at (${data.center.x},${data.center.y})`)
            break
          case 'enrich_progress':
            setEnrichProgress({ index: data.index, total: data.total, eta: data.eta, name: data.tile?.name })
            break
          case 'enrich_detail': {
            const t = data.tile
            if (t.error) {
              addScanMsg('error', `  [${data.index}/${data.total}] (${t.x},${t.y}) Failed: ${t.error}`)
            } else {
              addScanMsg('detail', `  [${data.index}/${data.total}] (${t.x},${t.y}) ${t.name || '?'} — pop:${t.pop ?? '?'} player:${t.player || '-'} ally:${t.alliance || '-'}`)
            }
            break
          }
          case 'complete': {
            const tiles = data.tiles || []
            setScanPhase(null)
            setEnrichProgress(null)
            setScanStats(data.stats || null)
            addScanMsg('success', `Scan complete: ${tiles.length} targets found in ${data.stats?.time_seconds || '?'}s`)
            onScanComplete(tiles)
            setScanning(false)
            toast.success(`Scan complete: ${tiles.length} targets found`)
            break
          }
          case 'error':
            addScanMsg('error', data.message || 'Error')
            setScanPhase(null)
            setScanning(false)
            toast.error(data.message || 'Scan failed')
            break
          default:
            if (data.message) addScanMsg('info', data.message)
        }
      },
      () => { if (mountedRef.current) { addScanMsg('error', 'WebSocket error'); setScanPhase(null); setScanning(false) } },
      () => { if (mountedRef.current) { setScanPhase(null); setScanning(false) } }
    )

    if (!ws) { addScanMsg('error', 'No auth token'); setScanPhase(null); setScanning(false); return }
    wsRef.current = ws

    ws.addEventListener('open', () => {
      setScanPhase('Scanning map...')
      addScanMsg('info', `Starting scan: radius=${radius}, pop=${minPop}–${maxPop}`)
      ws.send(JSON.stringify(body))
    })
  }

  const handleCancel = () => {
    if (wsRef.current) { try { wsRef.current.close() } catch {} wsRef.current = null }
    setScanPhase(null)
    setScanning(false)
    addScanMsg('warning', 'Scan cancelled by user')
    toast.warning('Scan cancelled')
  }

  return (
    <div className="card">
      <h3 className="heading-gold text-lg mb-4">Scan Configuration</h3>

      {/* Radius slider */}
      <div className="mb-4">
        <label className="field-label-lg">Radius: {radius}</label>
        <input type="range" min={5} max={50} value={radius} onChange={(e) => setRadius(Number(e.target.value))} className="w-full checkbox-gold" />
        <div className="flex justify-between text-xs text-secondary"><span>5</span><span>50</span></div>
      </div>

      {/* Population range — village */}
      <div className="flex gap-4 mb-4">
        <div className="flex-1">
          <label className="field-label-lg">Min Village Pop</label>
          <input type="number" className="input-field" value={minPop} min={0} onChange={(e) => setMinPop(Number(e.target.value))} />
        </div>
        <div className="flex-1">
          <label className="field-label-lg">Max Village Pop</label>
          <input type="number" className="input-field" value={maxPop} min={0} onChange={(e) => setMaxPop(Number(e.target.value))} />
        </div>
        <div className="flex-1">
          <label className="field-label-lg">Max Player Pop (all villages)</label>
          <input type="number" className="input-field" value={maxPlayerPop} placeholder="no limit" onChange={(e) => setMaxPlayerPop(e.target.value)} />
        </div>
      </div>

      {/* Options */}
      <div className="flex gap-6 mb-4 flex-wrap">
        <label className="check-label">
          <input type="checkbox" checked={showOases} onChange={(e) => setShowOases(e.target.checked)} className="checkbox-gold" />
          Include unoccupied oases
        </label>
      </div>
      <p className="text-xs text-secondary mb-4">Scans only player-owned villages (and oases if checked). Wilderness, abandoned valleys, and empty tiles are automatically skipped.</p>

      {/* Alliance exclusion */}
      <div className="mb-4">
        <label className="field-label-lg mb-1">Exclude Alliances (name or ID — persisted)</label>
        <div className="flex gap-2 items-center mb-2">
          <input className="input-field flex-1" placeholder="Alliance name or ID" value={newAlliance} onChange={(e) => setNewAlliance(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && addAlliance()} />
          <button className="btn-secondary btn-xs" onClick={addAlliance}>Add</button>
        </div>
        {excludeAlliances.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {excludeAlliances.map((a) => (
              <span key={a} className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-surface border-default text-secondary">
                {a}
                <button className="text-danger hover:text-primary ml-0.5" onClick={() => setExcludeAlliances(excludeAlliances.filter((x) => x !== a))}>x</button>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Player exclusion */}
      <div className="mb-4">
        <label className="field-label-lg mb-1">Exclude Players (persisted)</label>
        <div className="flex gap-2 items-center mb-2">
          <input className="input-field flex-1" placeholder="Player name" value={newPlayer} onChange={(e) => setNewPlayer(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && addPlayer()} />
          <button className="btn-secondary btn-xs" onClick={addPlayer}>Add</button>
        </div>
        {excludePlayers.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {excludePlayers.map((p) => (
              <span key={p} className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-surface border-default text-secondary">
                {p}
                <button className="text-danger hover:text-primary ml-0.5" onClick={() => setExcludePlayers(excludePlayers.filter((x) => x !== p))}>x</button>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="flex gap-3 items-center">
        <button className="btn-primary" onClick={handleScan} disabled={scanning}>
          {scanning ? 'Scanning...' : 'Scan Map'}
        </button>
        {scanning && <button className="btn-danger" onClick={handleCancel}>Cancel</button>}
      </div>

      {/* Live scan progress */}
      {(scanMessages.length > 0 || scanning) && (
        <ScanProgressPanel phase={scanPhase} messages={scanMessages} enrichProgress={enrichProgress} stats={scanStats} />
      )}
    </div>
  )
}

// ── Sort helpers ──────────────────────────────────────────────────────
function SortableHeader({ label, field, sortField, sortDir, onSort, className = '' }) {
  const active = sortField === field
  const arrow = active ? (sortDir === 'asc' ? ' \u25B2' : ' \u25BC') : ''
  return (
    <th onClick={() => onSort(field)} className={`sortable ${active ? 'sort-active' : ''} ${className}`}>
      {label}{arrow}
    </th>
  )
}

// ── Scan Results Table ────────────────────────────────────────────────
function ScanResultsTable({ results, selected, setSelected }) {
  const [sortField, setSortField] = useState(null)
  const [sortDir, setSortDir] = useState('asc')

  const handleSort = (field) => {
    if (sortField === field) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortField(field); setSortDir('asc') }
  }

  const originalIndices = useMemo(() => {
    if (!sortField) return results.map((_, i) => i)
    const indexed = results.map((r, i) => ({ r, i }))
    indexed.sort((a, b) => {
      let av = a.r[sortField] ?? 0
      let bv = b.r[sortField] ?? 0
      if (typeof av === 'string') { av = av.toLowerCase(); bv = (bv || '').toLowerCase() }
      if (av < bv) return sortDir === 'asc' ? -1 : 1
      if (av > bv) return sortDir === 'asc' ? 1 : -1
      return 0
    })
    return indexed.map((x) => x.i)
  }, [results, sortField, sortDir])

  const sorted = useMemo(() => originalIndices.map((i) => results[i]), [results, originalIndices])

  const allSelected = results.length > 0 && selected.size === results.length
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(results.map((_, i) => i)))
  const toggleRow = (idx) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx); else next.add(idx)
      return next
    })
  }

  return (
    <div className="card">
      <div className="flex justify-between items-center mb-3 flex-wrap gap-2">
        <h3 className="heading-gold text-lg">Scan Results ({results.length} targets)</h3>
        <div className="flex gap-2 items-center">
          <button className="btn-secondary btn-xs" onClick={toggleAll}>{allSelected ? 'Deselect All' : 'Select All'}</button>
          <span className="text-xs text-secondary">{selected.size} selected</span>
        </div>
      </div>

      <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
        <table className="data-table">
          <thead className="sticky top-0 bg-card z-[1]">
            <tr>
              <th className="w-10">
                <input type="checkbox" checked={allSelected} onChange={toggleAll} className="checkbox-gold" />
              </th>
              <th>Coords</th>
              <SortableHeader label="Village" field="village_name" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortableHeader label="V.Pop" field="population" sortField={sortField} sortDir={sortDir} onSort={handleSort} className="text-center" />
              <SortableHeader label="Distance" field="distance" sortField={sortField} sortDir={sortDir} onSort={handleSort} className="text-center" />
              <SortableHeader label="Player" field="player_name" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <th>Alliance</th>
              <th>Type</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, sortedIdx) => {
              const origIdx = originalIndices[sortedIdx]
              const isSelected = selected.has(origIdx)
              return (
                <tr key={origIdx} onClick={() => toggleRow(origIdx)} className={`row-clickable ${isSelected ? 'row-selected' : ''}`}>
                  <td><input type="checkbox" checked={isSelected} onChange={() => toggleRow(origIdx)} onClick={(e) => e.stopPropagation()} className="checkbox-gold" /></td>
                  <td className="font-mono text-gold">({row.x}, {row.y})</td>
                  <td>{row.village_name || row.name || '---'}</td>
                  <td className="text-center font-mono">{row.population ?? '---'}</td>
                  <td className="text-center font-mono">{row.distance != null ? row.distance.toFixed(1) : '---'}</td>
                  <td className={row.player_name ? 'text-primary' : 'text-secondary italic'}>{row.player_name || 'Unoccupied'}</td>
                  <td className="text-secondary text-xs">{row.alliance_name || '---'}</td>
                  <td>{row.is_oasis ? 'Oasis' : row.is_abandoned ? 'Abandoned' : 'Village'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Auto-Scout Panel ──────────────────────────────────────────────────
function AutoScoutPanel({ scanResults, selected, scanConfig }) {
  const [amount, setAmount] = useState(1)
  const [scoutType, setScoutType] = useState('resources')
  const [delayMin, setDelayMin] = useState(2)
  const [delayMax, setDelayMax] = useState(5)
  const [running, setRunning] = useState(false)
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [messages, setMessages] = useState([])
  const [progress, setProgress] = useState(null)
  const wsRef = useRef(null)
  const mountedRef = useRef(true)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const toast = useToast()

  // Idle scout count
  const [idleScouts, setIdleScouts] = useState(null)
  const [checkingScouts, setCheckingScouts] = useState(false)

  const checkIdleScouts = async () => {
    if (!activeVillageId) return
    setCheckingScouts(true)
    try {
      const res = await api.get(`/military/troops?village_id=${activeVillageId}`)
      const troops = res.data
      // Scout unit depends on tribe: t3 for Gauls, t4 for Romans/Teutons
      const tribeId = useGameStore.getState().tribeId
      const scoutKey = tribeId === 3 ? 't3' : 't4'
      const count = troops[scoutKey] || 0
      setIdleScouts(count)
    } catch {
      setIdleScouts('API not available')
    } finally {
      setCheckingScouts(false)
    }
  }

  // Loop mode state
  const [loopEnabled, setLoopEnabled] = useState(false)
  const [loopInterval, setLoopInterval] = useState(300) // seconds between cycles
  const [loopDuration, setLoopDuration] = useState(0) // 0 = infinite
  const [loopCycle, setLoopCycle] = useState(0)
  const loopStoppedRef = useRef(false)
  const loopTimerRef = useRef(null)
  const loopStartRef = useRef(null)

  // Round-robin resume position
  const [resumeIndex, setResumeIndex] = useState(0)

  useEffect(() => { return () => {
    mountedRef.current = false
    loopStoppedRef.current = true
    if (loopTimerRef.current) clearTimeout(loopTimerRef.current)
    if (wsRef.current) { try { wsRef.current.close() } catch {} wsRef.current = null }
  } }, [])

  const msgIdRef = useRef(0)
  const addMessage = useCallback((type, text) => {
    setMessages((prev) => [...prev, { id: ++msgIdRef.current, type, text, timestamp: Date.now() }])
  }, [])

  // Refs for values that the loop needs at execution time (avoids stale closures)
  const scanResultsRef = useRef(scanResults)
  const selectedRef = useRef(selected)
  const scanConfigRef = useRef(scanConfig)
  const amountRef = useRef(amount)
  const scoutTypeRef = useRef(scoutType)
  const delayMinRef = useRef(delayMin)
  const delayMaxRef = useRef(delayMax)
  const villageIdRef = useRef(activeVillageId)
  const resumeIndexRef = useRef(resumeIndex)
  const loopDurationRef = useRef(loopDuration)
  useEffect(() => { scanResultsRef.current = scanResults }, [scanResults])
  useEffect(() => { selectedRef.current = selected }, [selected])
  useEffect(() => { scanConfigRef.current = scanConfig }, [scanConfig])
  useEffect(() => { amountRef.current = amount }, [amount])
  useEffect(() => { scoutTypeRef.current = scoutType }, [scoutType])
  useEffect(() => { delayMinRef.current = delayMin }, [delayMin])
  useEffect(() => { delayMaxRef.current = delayMax }, [delayMax])
  useEffect(() => { villageIdRef.current = activeVillageId }, [activeVillageId])
  useEffect(() => { resumeIndexRef.current = resumeIndex }, [resumeIndex])
  useEffect(() => { loopDurationRef.current = loopDuration }, [loopDuration])

  // Core: run one scout pass via WS, returns a promise that resolves when complete
  const runOnePass = useCallback((cycleNum) => {
    return new Promise((resolve) => {
      if (!mountedRef.current || loopStoppedRef.current) { resolve(); return }
      // Safety timeout — resolve if WS never completes (5 min max per pass)
      let resolved = false
      const safeResolve = () => { if (!resolved) { resolved = true; resolve() } }
      const safetyTimer = setTimeout(() => {
        addMessage('warning', 'Pass timed out after 5 minutes')
        setWsStatus('disconnected')
        safeResolve()
      }, 300000)

      // Send selected targets with enriched data — don't let the backend re-scan
      const curResults = scanResultsRef.current
      const curSelected = selectedRef.current
      const targets = curResults
        .filter((_, i) => curSelected.has(i))
        .map((r) => ({ x: r.x, y: r.y, name: r.village_name || r.name || '', pop: r.population || 0, player: r.player_name || '' }))
      setWsStatus('connected')
      if (cycleNum > 0) addMessage('info', `--- Loop cycle ${cycleNum + 1} ---`)

      const ws = createWebSocket('/ws/scout/auto',
        (data) => {
          if (!mountedRef.current) return
          switch (data.type) {
            case 'scanning': addMessage('info', data.message || 'Scanning map...'); break
            case 'scan_complete': addMessage('success', `Scan: ${data.targets} targets`); break
            case 'target_list':
              addMessage('info', `Targets queued: ${(data.targets || []).length} villages`)
              break
            case 'scouting':
              setProgress({ index: data.index, total: data.total, eta: data.eta })
              addMessage('info', `[${data.index}/${data.total}] Scouting (${data.target.x},${data.target.y}) ${data.target.name || ''}${data.eta ? ' | ' + data.eta : ''}`)
              break
            case 'scout_result': {
              const ok = data.success
              const errStr = !ok && data.error ? `: ${data.error}` : ''
              const ttStr = data.travel_time ? ` | ${data.travel_time}` : ''
              const t = data.target || {}
              addMessage(ok ? 'success' : 'warning', `[${data.index || '?'}/${data.total || '?'}] (${t.x ?? '?'},${t.y ?? '?'}) ${ok ? 'Sent' : 'Failed'}${errStr}${ttStr}`)
              break
            }
            case 'waiting':
              // Update progress bar remaining but don't spam the log
              setProgress((prev) => prev ? { ...prev, waitRemaining: data.remaining } : prev)
              break
            case 'complete': {
              const timeStr = data.total_time_seconds ? ` in ${data.total_time_seconds}s` : ''
              const avgStr = data.avg_time_per_target ? ` (avg ${data.avg_time_per_target}s/target)` : ''
              addMessage('success', `Pass done: ${data.successful}/${data.total_sent} sent${timeStr}${avgStr}`)
              setProgress(null); setWsStatus('disconnected')
              // Update round-robin resume index (prefer backend-computed value)
              if (data.next_start_index != null) setResumeIndex(data.next_start_index)
              else setResumeIndex((prev) => (prev + (data.total_sent || 0)) % (scanResultsRef.current?.length || 1))
              clearTimeout(safetyTimer); safeResolve()
              break
            }
            case 'scout_preflight':
              addMessage('info', `Scouts available: ${data.available}${data.needed_per_target > 1 ? ` (${data.needed_per_target} per target)` : ''} — can send to ${data.can_send_to}/${data.total_targets} targets`)
              break
            case 'scouts_capped':
              addMessage('warning', data.message || `Capped to ${data.can_send_to} targets (${data.available} scouts idle)`)
              break
            case 'scouts_exhausted':
              addMessage('warning', data.message || `Scouts ran out after ${data.sent_so_far} sends`)
              setProgress(null)
              break
            case 'scouts_low':
              addMessage('warning', `Scouts running low: ${data.remaining} remaining`)
              break
            case 'noise_action':
              addMessage('info', data.message || 'Stealth: idle browsing...')
              break
            case 're_navigate':
              addMessage('info', data.message || 'Stealth: breaking request pattern...')
              break
            case 'error': addMessage('error', data.message || 'Error'); break
            default: if (data.message) addMessage('info', data.message); break
          }
        },
        () => { if (mountedRef.current) { addMessage('error', 'WS error'); setWsStatus('disconnected') }; clearTimeout(safetyTimer); safeResolve() },
        () => { if (mountedRef.current) { setWsStatus('disconnected') }; clearTimeout(safetyTimer); safeResolve() }
      )

      if (!ws) { addMessage('error', 'No auth token'); setWsStatus('disconnected'); clearTimeout(safetyTimer); safeResolve(); return }
      // Close any previous WS before assigning new one
      if (wsRef.current) { try { wsRef.current.close() } catch {} }
      wsRef.current = ws
      ws.addEventListener('open', () => {
        if (!mountedRef.current) { try { ws.close() } catch {} clearTimeout(safetyTimer); safeResolve(); return }
        setWsStatus('running')
        ws.send(JSON.stringify({
          radius: scanConfigRef.current.radius || 10,
          amount: amountRef.current,
          type: scoutTypeRef.current,
          delay_min: delayMinRef.current,
          delay_max: delayMaxRef.current,
          targets: targets,
          village_id: villageIdRef.current,
          start_index: resumeIndexRef.current,
        }))
      })
    })
  }, [addMessage]) // refs are stable — no deps needed for values read from refs

  const handleStart = async () => {
    if (selected.size === 0) { toast.warning('No targets selected'); return }
    if (loopTimerRef.current) { clearTimeout(loopTimerRef.current); loopTimerRef.current = null }
    setRunning(true); setMessages([]); setProgress(null)
    loopStoppedRef.current = false
    setLoopCycle(0)

    if (!loopEnabled) {
      // Single pass — reset resume index for fresh run
      setResumeIndex(0)
      resumeIndexRef.current = 0
      await runOnePass(0)
      if (mountedRef.current) { setRunning(false); toast.success('Scouting complete') }
    } else {
      // Loop mode
      loopStartRef.current = Date.now()
      let cycle = 0
      const loop = async () => {
        if (loopStoppedRef.current || !mountedRef.current) { setRunning(false); return }
        // Check duration limit
        if (loopDurationRef.current > 0 && loopStartRef.current) {
          const elapsedMin = (Date.now() - loopStartRef.current) / 60000
          if (elapsedMin >= loopDurationRef.current) {
            addMessage('info', `Duration limit reached (${loopDurationRef.current} min). Stopping.`)
            setRunning(false)
            return
          }
        }
        setLoopCycle(cycle)
        await runOnePass(cycle)
        cycle++
        if (loopStoppedRef.current || !mountedRef.current) { setRunning(false); return }
        const safeInterval = Math.max(loopInterval, 30)
        addMessage('info', `Waiting ${safeInterval}s before next cycle...`)
        loopTimerRef.current = setTimeout(loop, safeInterval * 1000)
      }
      await loop()
    }
  }

  const handleStop = () => {
    loopStoppedRef.current = true
    if (loopTimerRef.current) { clearTimeout(loopTimerRef.current); loopTimerRef.current = null }
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null }
    setRunning(false); setWsStatus('disconnected')
    addMessage('warning', 'Stopped by user')
    toast.warning('Auto-scout stopped')
  }

  const progressPct = progress ? (progress.index / progress.total) * 100 : 0
  const waitPct = progress?.waitRemaining != null ? progress.waitRemaining : null

  return (
    <div className="card">
      <h3 className="heading-gold text-lg mb-4">Auto-Scout</h3>

      {/* Scouts per target + idle scout check */}
      <div className="flex gap-4 mb-4 flex-wrap">
        <div className="flex-1 min-w-[120px]">
          <label className="field-label-lg">Scouts per target</label>
          <input type="number" className="input-field" value={amount} min={1} max={20} onChange={(e) => setAmount(Number(e.target.value))} disabled={running} />
          <div className="flex items-center gap-2 mt-1">
            <button className="btn-secondary btn-xs" onClick={checkIdleScouts} disabled={checkingScouts || running}>
              {checkingScouts ? '...' : 'Check'}
            </button>
            {idleScouts !== null && (
              <span className="text-xs text-secondary">
                {typeof idleScouts === 'number' ? `${idleScouts} scouts idle in village` : idleScouts}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Stealth delay range */}
      <div className="mb-4">
        <label className="field-label-lg">Stealth delay</label>
        <div className="flex items-center gap-2">
          <input type="number" className="input-field w-20" value={delayMin} min={0} max={60} onChange={(e) => setDelayMin(Number(e.target.value))} disabled={running} />
          <span className="text-secondary">s</span>
          <span className="text-secondary">&mdash;</span>
          <input type="number" className="input-field w-20" value={delayMax} min={0} max={120} onChange={(e) => setDelayMax(Number(e.target.value))} disabled={running} />
          <span className="text-secondary">s</span>
        </div>
        <p className="text-xs text-secondary mt-1">Human-like delay — heavy-tailed distribution (most delays shorter, occasional longer pauses)</p>
      </div>

      {/* Scout type */}
      <div className="mb-4">
        <label className="field-label-lg mb-2">Scout type</label>
        <div className="flex gap-6">
          <label className="check-label">
            <input type="radio" name="scoutType" value="resources" checked={scoutType === 'resources'} onChange={() => setScoutType('resources')} disabled={running} className="accent-radio" /> Resources
          </label>
          <label className="check-label">
            <input type="radio" name="scoutType" value="defenses" checked={scoutType === 'defenses'} onChange={() => setScoutType('defenses')} disabled={running} className="accent-radio" /> Defenses
          </label>
          <label className="check-label">
            <input type="radio" name="scoutType" value="both" checked={scoutType === 'both'} onChange={() => setScoutType('both')} disabled={running} className="accent-radio" /> Both
          </label>
        </div>
      </div>

      {/* Resume position indicator */}
      {resumeIndex > 0 && !running && (
        <div className="mb-4">
          <span className="text-xs text-secondary">Resuming from target #{resumeIndex + 1}</span>
        </div>
      )}

      {/* Loop mode */}
      <div className="mb-5 p-3 bg-surface rounded-md border-default">
        <div className="flex gap-4 items-center flex-wrap">
          <label className="check-label">
            <input type="checkbox" className="checkbox-gold" checked={loopEnabled} onChange={(e) => setLoopEnabled(e.target.checked)} disabled={running} />
            Loop mode
          </label>
          {loopEnabled && running && (
            <span className="text-xs text-gold font-semibold">Cycle #{loopCycle + 1}</span>
          )}
        </div>
        {loopEnabled && (
          <div className="flex items-center gap-4 flex-wrap mt-3">
            <div className="flex items-center gap-2">
              <label className="text-xs text-secondary">Interval (s):</label>
              <input type="number" className="input-field text-xs py-1 px-2 w-20" min={30} max={3600} value={loopInterval} onChange={(e) => setLoopInterval(Number(e.target.value) || 300)} disabled={running} />
            </div>
            <div className="flex items-center gap-2">
              <label className="text-xs text-secondary">Duration (min):</label>
              <input type="number" className="input-field text-xs py-1 px-2 w-20" min={0} max={1440} value={loopDuration} onChange={(e) => setLoopDuration(Number(e.target.value) || 0)} disabled={running} />
              <span className="text-xs text-secondary opacity-70">0 = infinite</span>
            </div>
          </div>
        )}
        {loopEnabled && (
          <p className="text-xs text-secondary mt-2">Scan once, then re-scout the same targets every {loopInterval}s. Scouts return home and get re-sent.</p>
        )}
      </div>

      {/* Buttons */}
      <div className="flex gap-3 mb-4">
        {!running
          ? <button className="btn-primary" onClick={handleStart} disabled={selected.size === 0}>
              {loopEnabled ? `Start Scout Loop (${selected.size} targets)` : `Start Auto-Scout (${selected.size} targets)`}
            </button>
          : <button className="btn-danger" onClick={handleStop}>Stop</button>}
      </div>
      {progress && (
        <div className="mb-4">
          <div className="flex justify-between text-xs text-secondary mb-1">
            <span>Target {progress.index}/{progress.total}{progress.eta ? ` | ${progress.eta}` : ''}</span>
            <span>{Math.round(progressPct)}%{waitPct != null ? ` | cooldown ${Math.round(waitPct)}s` : ''}</span>
          </div>
          <div className="progress-track"><div className="progress-fill" style={{ width: `${progressPct}%` }} /></div>
        </div>
      )}
      {messages.length > 0 && <WebSocketPanel messages={messages} status={wsStatus} onClear={() => setMessages([])} />}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────
export default function AutoScout() {
  const [scanResults, setScanResults] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [scanning, setScanning] = useState(false)
  const [scanConfig, setScanConfig] = useState({ radius: 10 })
  const activeVillageId = useGameStore((s) => s.activeVillageId)

  const handleScanComplete = (results) => {
    setScanResults(results)
    setSelected(new Set(results.map((_, i) => i)))
  }

  return (
    <div className="p-6 max-w-[1100px] mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Auto Scout</h2>
        <VillageSelector />
      </div>
      <div className="flex flex-col gap-4">
        <ScanConfigPanel onScanComplete={handleScanComplete} scanning={scanning} setScanning={setScanning} onConfigChange={setScanConfig} activeVillageId={activeVillageId} />
        {scanResults && scanResults.length > 0 && (
          <>
            <ScanResultsTable results={scanResults} selected={selected} setSelected={setSelected} />
            <AutoScoutPanel scanResults={scanResults} selected={selected} scanConfig={scanConfig} />
          </>
        )}
        {scanResults && scanResults.length === 0 && (
          <div className="card text-center p-8"><p className="text-secondary">No targets found. Try increasing the radius or adjusting filters.</p></div>
        )}
      </div>
    </div>
  )
}
