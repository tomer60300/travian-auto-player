import { useState, useRef, useCallback, useMemo, useEffect } from 'react'
import api from '../api'
import { createWebSocket } from '../ws'
import { useToast } from '../components/Toast'
import WebSocketPanel from '../components/WebSocketPanel'
import VillageSelector from '../components/VillageSelector'
import useGameStore from '../stores/gameStore'

// ── Scan Config Panel ─────────────────────────────────────────────────
function ScanConfigPanel({ onScanComplete, scanning, setScanning, onConfigChange }) {
  const [radius, setRadius] = useState(10)
  const [minPop, setMinPop] = useState(0)
  const [maxPop, setMaxPop] = useState(500)
  const [noPlayer, setNoPlayer] = useState(true)
  const [showOases, setShowOases] = useState(false)
  const [limit, setLimit] = useState(100)
  const toast = useToast()

  const handleScan = async () => {
    setScanning(true)
    onConfigChange?.({ radius, minPop, maxPop, noPlayer, showOases, limit })
    try {
      const res = await api.post('/scout/scan', {
        radius,
        min_pop: minPop,
        max_pop: maxPop,
        no_player: noPlayer,
        show_oases: showOases,
        limit,
      })
      onScanComplete(res.data)
      toast.success(`Scan complete: ${res.data.length} targets found`)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Scan failed')
    } finally {
      setScanning(false)
    }
  }

  return (
    <div className="card">
      <h3 className="heading-gold text-lg mb-4">
        Scan Configuration
      </h3>

      {/* Radius slider */}
      <div className="mb-4">
        <label className="field-label-lg">
          Radius: {radius}
        </label>
        <input
          type="range"
          min={5}
          max={50}
          value={radius}
          onChange={(e) => setRadius(Number(e.target.value))}
          className="w-full checkbox-gold"
        />
        <div className="flex justify-between text-xs text-secondary">
          <span>5</span>
          <span>50</span>
        </div>
      </div>

      {/* Population range */}
      <div className="flex gap-4 mb-4">
        <div className="flex-1">
          <label className="field-label-lg">
            Min Population
          </label>
          <input
            type="number"
            className="input-field"
            value={minPop}
            min={0}
            onChange={(e) => setMinPop(Number(e.target.value))}
          />
        </div>
        <div className="flex-1">
          <label className="field-label-lg">
            Max Population
          </label>
          <input
            type="number"
            className="input-field"
            value={maxPop}
            min={0}
            onChange={(e) => setMaxPop(Number(e.target.value))}
          />
        </div>
      </div>

      {/* Checkboxes */}
      <div className="flex gap-6 mb-4 flex-wrap">
        <label className="check-label">
          <input
            type="checkbox"
            checked={noPlayer}
            onChange={(e) => setNoPlayer(e.target.checked)}
            className="checkbox-gold"
          />
          Exclude player-owned villages
        </label>
        <label className="check-label">
          <input
            type="checkbox"
            checked={showOases}
            onChange={(e) => setShowOases(e.target.checked)}
            className="checkbox-gold"
          />
          Include oases
        </label>
      </div>

      {/* Result limit */}
      <div className="mb-5">
        <label className="field-label-lg">
          Result Limit
        </label>
        <input
          type="number"
          className="input-field max-w-[150px]"
          value={limit}
          min={1}
          max={500}
          onChange={(e) => setLimit(Number(e.target.value))}
        />
      </div>

      <button className="btn-primary" onClick={handleScan} disabled={scanning}>
        {scanning ? 'Scanning...' : 'Scan Map'}
      </button>
    </div>
  )
}

// ── Sort helpers ──────────────────────────────────────────────────────
function SortableHeader({ label, field, sortField, sortDir, onSort }) {
  const active = sortField === field
  const arrow = active ? (sortDir === 'asc' ? ' \u25B2' : ' \u25BC') : ''
  return (
    <th
      onClick={() => onSort(field)}
      className={`sortable ${active ? 'sort-active' : ''}`}
    >
      {label}{arrow}
    </th>
  )
}

// ── Scan Results Table ────────────────────────────────────────────────
function ScanResultsTable({ results, selected, setSelected }) {
  const [sortField, setSortField] = useState(null)
  const [sortDir, setSortDir] = useState('asc')

  const handleSort = (field) => {
    if (sortField === field) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortField(field)
      setSortDir('asc')
    }
  }

  const sorted = useMemo(() => {
    if (!sortField) return results
    const copy = [...results]
    copy.sort((a, b) => {
      const av = a[sortField] ?? 0
      const bv = b[sortField] ?? 0
      return sortDir === 'asc' ? av - bv : bv - av
    })
    return copy
  }, [results, sortField, sortDir])

  const allSelected = results.length > 0 && selected.size === results.length
  const toggleAll = () => {
    if (allSelected) {
      setSelected(new Set())
    } else {
      setSelected(new Set(results.map((_, i) => i)))
    }
  }

  const toggleRow = (idx) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) {
        next.delete(idx)
      } else {
        next.add(idx)
      }
      return next
    })
  }

  // Build a lookup from original index (in results array) for the sorted view
  const originalIndices = useMemo(() => {
    if (!sortField) return results.map((_, i) => i)
    const indexed = results.map((r, i) => ({ r, i }))
    indexed.sort((a, b) => {
      const av = a.r[sortField] ?? 0
      const bv = b.r[sortField] ?? 0
      return sortDir === 'asc' ? av - bv : bv - av
    })
    return indexed.map((x) => x.i)
  }, [results, sortField, sortDir])

  return (
    <div className="card">
      <div className="flex justify-between items-center mb-3 flex-wrap gap-2">
        <h3 className="heading-gold text-lg">
          Scan Results ({results.length} targets)
        </h3>
        <div className="flex gap-2 items-center">
          <button className="btn-secondary btn-xs" onClick={toggleAll}>
            {allSelected ? 'Deselect All' : 'Select All'}
          </button>
          <span className="text-xs text-secondary">
            {selected.size} selected
          </span>
        </div>
      </div>

      <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
        <table className="data-table">
          <thead className="sticky top-0 bg-card z-[1]">
            <tr>
              <th className="w-10">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                  className="checkbox-gold"
                />
              </th>
              <th>Coords</th>
              <th>Village Name</th>
              <SortableHeader label="Population" field="population" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortableHeader label="Distance" field="distance" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <th>Player</th>
              <th>Type</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, sortedIdx) => {
              const origIdx = originalIndices[sortedIdx]
              const isSelected = selected.has(origIdx)
              return (
                <tr
                  key={origIdx}
                  onClick={() => toggleRow(origIdx)}
                  className={`row-clickable ${isSelected ? 'row-selected' : ''}`}
                >
                  <td>
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => toggleRow(origIdx)}
                      onClick={(e) => e.stopPropagation()}
                      className="checkbox-gold"
                    />
                  </td>
                  <td className="font-mono text-gold">
                    ({row.x}, {row.y})
                  </td>
                  <td>
                    {row.name || '---'}
                  </td>
                  <td>
                    {row.population ?? '---'}
                  </td>
                  <td className="font-mono">
                    {row.distance != null ? row.distance.toFixed(1) : '---'}
                  </td>
                  <td className={row.player ? 'text-primary' : 'text-secondary italic'}>
                    {row.player || 'Unoccupied'}
                  </td>
                  <td>
                    {row.type || '---'}
                  </td>
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
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const toast = useToast()

  // Clean up WebSocket on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        try { wsRef.current.close() } catch {}
        wsRef.current = null
      }
    }
  }, [])

  const msgIdRef = useRef(0)
  const addMessage = useCallback((type, text) => {
    setMessages((prev) => [...prev, { id: ++msgIdRef.current, type, text, timestamp: Date.now() }])
  }, [])

  const handleClear = () => setMessages([])

  const handleStart = () => {
    if (selected.size === 0) {
      toast.warning('No targets selected')
      return
    }

    // Build exclude list: coords NOT selected
    const excludeCoords = scanResults
      .filter((_, i) => !selected.has(i))
      .map((r) => [r.x, r.y])

    setRunning(true)
    setWsStatus('connected')
    setMessages([])
    setProgress(null)
    addMessage('info', 'Connecting to auto-scout service...')

    const ws = createWebSocket(
      '/ws/scout/auto',
      (data) => {
        // Handle message types
        switch (data.type) {
          case 'scanning':
            addMessage('info', data.message || 'Scanning map...')
            break
          case 'scan_complete':
            addMessage('success', `Scan complete: ${data.targets} targets found`)
            break
          case 'scouting':
            setProgress({ index: data.index, total: data.total })
            addMessage(
              'info',
              `Scouting target ${data.index}/${data.total}: (${data.target.x}, ${data.target.y}) ${data.target.name || ''}`
            )
            break
          case 'scout_result':
            addMessage(
              data.success ? 'success' : 'warning',
              `(${data.target.x}, ${data.target.y}) - ${data.success ? 'Sent' : 'Failed'}${data.travel_time ? ` | Travel: ${data.travel_time}` : ''}`
            )
            break
          case 'complete':
            addMessage(
              'success',
              `Done! Sent ${data.successful}/${data.total_sent} scouts successfully`
            )
            setProgress(null)
            setRunning(false)
            setWsStatus('disconnected')
            toast.success(`Scouting complete: ${data.successful}/${data.total_sent} sent`)
            break
          case 'error':
            addMessage('error', data.message || 'Unknown error')
            break
          default:
            if (data.message) {
              addMessage('info', data.message)
            }
            break
        }
      },
      () => {
        addMessage('error', 'WebSocket connection error')
        setRunning(false)
        setWsStatus('disconnected')
      },
      () => {
        setRunning(false)
        setWsStatus('disconnected')
      }
    )

    if (!ws) {
      addMessage('error', 'No auth token — cannot connect')
      setRunning(false)
      setWsStatus('disconnected')
      return
    }
    wsRef.current = ws

    // Send config after connection opens
    ws.onopen = () => {
      setWsStatus('running')
      addMessage('info', 'Connected. Sending scout configuration...')
      ws.send(
        JSON.stringify({
          radius: scanConfig.radius || 10,
          amount,
          type: scoutType,
          delay,
          exclude_coords: excludeCoords,
          village_id: activeVillageId,
        })
      )
    }
  }

  const handleStop = () => {
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setRunning(false)
    setWsStatus('disconnected')
    addMessage('warning', 'Auto-scout stopped by user')
  }

  const progressPct = progress ? (progress.index / progress.total) * 100 : 0

  return (
    <div className="card">
      <h3 className="heading-gold text-lg mb-4">
        Auto-Scout
      </h3>

      <div className="flex gap-4 mb-4 flex-wrap">
        {/* Scout amount */}
        <div className="flex-1 min-w-[120px]">
          <label className="field-label-lg">
            Scouts per target
          </label>
          <input
            type="number"
            className="input-field"
            value={amount}
            min={1}
            max={20}
            onChange={(e) => setAmount(Number(e.target.value))}
            disabled={running}
          />
        </div>

        {/* Delay */}
        <div className="flex-1 min-w-[120px]">
          <label className="field-label-lg">
            Delay between sends (s)
          </label>
          <input
            type="number"
            className="input-field"
            value={delay}
            min={1}
            max={60}
            onChange={(e) => setDelay(Number(e.target.value))}
            disabled={running}
          />
        </div>
      </div>

      {/* Scout type radio */}
      <div className="mb-5">
        <label className="field-label-lg mb-2">
          Scout type
        </label>
        <div className="flex gap-6">
          <label className="check-label">
            <input
              type="radio"
              name="scoutType"
              value="resources"
              checked={scoutType === 'resources'}
              onChange={() => setScoutType('resources')}
              disabled={running}
              className="accent-radio"
            />
            Resources
          </label>
          <label className="check-label">
            <input
              type="radio"
              name="scoutType"
              value="defenses"
              checked={scoutType === 'defenses'}
              onChange={() => setScoutType('defenses')}
              disabled={running}
              className="accent-radio"
            />
            Defenses
          </label>
        </div>
      </div>

      {/* Buttons */}
      <div className="flex gap-3 mb-4">
        {!running ? (
          <button className="btn-primary" onClick={handleStart} disabled={selected.size === 0}>
            Start Auto-Scout ({selected.size} targets)
          </button>
        ) : (
          <button className="btn-danger" onClick={handleStop}>
            Stop
          </button>
        )}
      </div>

      {/* Progress bar */}
      {progress && (
        <div className="mb-4">
          <div className="flex justify-between text-xs text-secondary mb-1">
            <span>Scouting target {progress.index}/{progress.total}...</span>
            <span>{Math.round(progressPct)}%</span>
          </div>
          <div className="progress-track">
            <div
              className="progress-fill"
              style={{ width: `${progressPct}%` }}
            />
          </div>
        </div>
      )}

      {/* WebSocket log panel */}
      {messages.length > 0 && (
        <WebSocketPanel messages={messages} status={wsStatus} onClear={handleClear} />
      )}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────
export default function AutoScout() {
  const [scanResults, setScanResults] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [scanning, setScanning] = useState(false)
  const [scanConfig, setScanConfig] = useState({ radius: 10 })

  const handleScanComplete = (results) => {
    setScanResults(results)
    // Select all by default
    setSelected(new Set(results.map((_, i) => i)))
  }

  return (
    <div className="p-6 max-w-[1100px] mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Auto Scout</h2>
        <VillageSelector />
      </div>

      <div className="flex flex-col gap-4">
        <ScanConfigPanel
          onScanComplete={handleScanComplete}
          scanning={scanning}
          setScanning={setScanning}
          onConfigChange={setScanConfig}
        />

        {scanResults && scanResults.length > 0 && (
          <>
            <ScanResultsTable results={scanResults} selected={selected} setSelected={setSelected} />
            <AutoScoutPanel scanResults={scanResults} selected={selected} scanConfig={scanConfig} />
          </>
        )}

        {scanResults && scanResults.length === 0 && (
          <div className="card text-center p-8">
            <p className="text-secondary">
              No targets found matching your criteria. Try increasing the radius or adjusting filters.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
