import { useState, useRef, useCallback, useMemo, useEffect } from 'react'
import api from '../api'
import { createWebSocket } from '../ws'
import { useToast } from '../components/Toast'
import WebSocketPanel from '../components/WebSocketPanel'
import VillageSelector from '../components/VillageSelector'
import useGameStore from '../stores/gameStore'

// ── localStorage helpers ─────────────────────────────────────────────
const LS_KEY_ALLIANCES = 'autoscout_exclude_alliances'
const LS_KEY_PLAYERS = 'autoscout_exclude_players'

function loadJson(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback } catch { return fallback }
}

// ── Scan Config Panel ─────────────────────────────────────────────────
function ScanConfigPanel({ onScanComplete, scanning, setScanning, onConfigChange, activeVillageId }) {
  const [radius, setRadius] = useState(10)
  const [minPop, setMinPop] = useState(0)
  const [maxPop, setMaxPop] = useState(100)
  const [maxPlayerPop, setMaxPlayerPop] = useState('')
  const [showOases, setShowOases] = useState(false)
  const [limit, setLimit] = useState(100)

  // Alliance & player exclusion — persisted in localStorage
  const [excludeAlliances, setExcludeAlliances] = useState(() => loadJson(LS_KEY_ALLIANCES, []))
  const [excludePlayers, setExcludePlayers] = useState(() => loadJson(LS_KEY_PLAYERS, []))
  const [newAlliance, setNewAlliance] = useState('')
  const [newPlayer, setNewPlayer] = useState('')

  // Persist to localStorage on change
  useEffect(() => { localStorage.setItem(LS_KEY_ALLIANCES, JSON.stringify(excludeAlliances)) }, [excludeAlliances])
  useEffect(() => { localStorage.setItem(LS_KEY_PLAYERS, JSON.stringify(excludePlayers)) }, [excludePlayers])

  const toast = useToast()

  const addAlliance = () => {
    // Support comma-separated input: "HM2,HM,LR" → three entries
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

  const handleScan = async () => {
    setScanning(true)
    const config = { radius, minPop, maxPop, maxPlayerPop, showOases, limit, excludeAlliances, excludePlayers }
    onConfigChange?.(config)
    try {
      const body = {
        radius,
        min_pop: minPop,
        max_pop: maxPop,
        show_oases: showOases,
        limit,
        exclude_player_names: excludePlayers.flatMap((p) => p.split(',').map(s => s.trim())).filter(Boolean),
        village_id: activeVillageId || undefined,
      }
      if (maxPlayerPop !== '') body.max_player_pop = Number(maxPlayerPop)

      // Flatten: split any remaining comma-separated entries (safety for stale localStorage)
      const allAlliances = excludeAlliances.flatMap((a) => a.split(',').map(s => s.trim())).filter(Boolean)
      const allianceIds = allAlliances.filter((a) => /^\d+$/.test(a)).map(Number)
      const allianceNames = allAlliances.filter((a) => !/^\d+$/.test(a))
      if (allianceIds.length > 0) body.exclude_alliance_ids = allianceIds
      if (allianceNames.length > 0) body.exclude_alliance_names = allianceNames

      const res = await api.post('/scout/scan', body)
      const tiles = res.data.tiles ?? res.data

      onScanComplete(tiles)
      toast.success(`Scan complete: ${tiles.length} targets found`)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Scan failed')
    } finally {
      setScanning(false)
    }
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

      {/* Result limit */}
      <div className="mb-5">
        <label className="field-label-lg">Result Limit</label>
        <input type="number" className="input-field max-w-[150px]" value={limit} min={1} max={500} onChange={(e) => setLimit(Number(e.target.value))} />
      </div>

      <button className="btn-primary" onClick={handleScan} disabled={scanning}>
        {scanning ? 'Scanning...' : 'Scan Map'}
      </button>
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
  const [delay, setDelay] = useState(3)
  const [running, setRunning] = useState(false)
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [messages, setMessages] = useState([])
  const [progress, setProgress] = useState(null)
  const wsRef = useRef(null)
  const mountedRef = useRef(true)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const toast = useToast()

  useEffect(() => { return () => { mountedRef.current = false; if (wsRef.current) { try { wsRef.current.close() } catch {} wsRef.current = null } } }, [])

  const msgIdRef = useRef(0)
  const addMessage = useCallback((type, text) => {
    setMessages((prev) => [...prev, { id: ++msgIdRef.current, type, text, timestamp: Date.now() }])
  }, [])

  const handleStart = () => {
    if (selected.size === 0) { toast.warning('No targets selected'); return }
    const excludeCoords = scanResults.filter((_, i) => !selected.has(i)).map((r) => [r.x, r.y])
    setRunning(true); setWsStatus('connected'); setMessages([]); setProgress(null)
    addMessage('info', 'Connecting to auto-scout service...')

    const ws = createWebSocket('/ws/scout/auto',
      (data) => {
        if (!mountedRef.current) return
        switch (data.type) {
          case 'scanning': addMessage('info', data.message || 'Scanning map...'); break
          case 'scan_complete': addMessage('success', `Scan complete: ${data.targets} targets found`); break
          case 'scouting':
            setProgress({ index: data.index, total: data.total })
            addMessage('info', `Scouting ${data.index}/${data.total}: (${data.target.x}, ${data.target.y}) ${data.target.name || ''}`)
            break
          case 'scout_result':
            addMessage(data.success ? 'success' : 'warning', `(${data.target.x}, ${data.target.y}) - ${data.success ? 'Sent' : 'Failed'}${data.travel_time ? ` | ${data.travel_time}` : ''}`)
            break
          case 'complete':
            addMessage('success', `Done! ${data.successful}/${data.total_sent} scouts sent`)
            setProgress(null); setRunning(false); setWsStatus('disconnected')
            toast.success(`Scouting complete: ${data.successful}/${data.total_sent}`)
            break
          case 'error': addMessage('error', data.message || 'Error'); break
          default: if (data.message) addMessage('info', data.message); break
        }
      },
      () => { if (mountedRef.current) { addMessage('error', 'WS error'); setRunning(false); setWsStatus('disconnected') } },
      () => { if (mountedRef.current) { setRunning(false); setWsStatus('disconnected') } }
    )

    if (!ws) { addMessage('error', 'No auth token'); setRunning(false); setWsStatus('disconnected'); return }
    wsRef.current = ws
    ws.addEventListener('open', () => {
      setWsStatus('running')
      addMessage('info', 'Connected. Sending config...')
      ws.send(JSON.stringify({ radius: scanConfig.radius || 10, amount, type: scoutType, delay, exclude_coords: excludeCoords, village_id: activeVillageId }))
    })
  }

  const handleStop = () => {
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null }
    setRunning(false); setWsStatus('disconnected')
    addMessage('warning', 'Stopped by user')
  }

  const progressPct = progress ? (progress.index / progress.total) * 100 : 0

  return (
    <div className="card">
      <h3 className="heading-gold text-lg mb-4">Auto-Scout</h3>
      <div className="flex gap-4 mb-4 flex-wrap">
        <div className="flex-1 min-w-[120px]">
          <label className="field-label-lg">Scouts per target</label>
          <input type="number" className="input-field" value={amount} min={1} max={20} onChange={(e) => setAmount(Number(e.target.value))} disabled={running} />
        </div>
        <div className="flex-1 min-w-[120px]">
          <label className="field-label-lg">Delay between sends (s)</label>
          <input type="number" className="input-field" value={delay} min={1} max={60} onChange={(e) => setDelay(Number(e.target.value))} disabled={running} />
        </div>
      </div>
      <div className="mb-5">
        <label className="field-label-lg mb-2">Scout type</label>
        <div className="flex gap-6">
          <label className="check-label"><input type="radio" name="scoutType" value="resources" checked={scoutType === 'resources'} onChange={() => setScoutType('resources')} disabled={running} className="accent-radio" /> Resources</label>
          <label className="check-label"><input type="radio" name="scoutType" value="defenses" checked={scoutType === 'defenses'} onChange={() => setScoutType('defenses')} disabled={running} className="accent-radio" /> Defenses</label>
        </div>
      </div>
      <div className="flex gap-3 mb-4">
        {!running
          ? <button className="btn-primary" onClick={handleStart} disabled={selected.size === 0}>Start Auto-Scout ({selected.size} targets)</button>
          : <button className="btn-danger" onClick={handleStop}>Stop</button>}
      </div>
      {progress && (
        <div className="mb-4">
          <div className="flex justify-between text-xs text-secondary mb-1"><span>Target {progress.index}/{progress.total}...</span><span>{Math.round(progressPct)}%</span></div>
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
