import { useState, useRef, useCallback, useMemo } from 'react'
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
      <h3
        style={{
          fontFamily: 'Cinzel, serif',
          fontSize: '1.1rem',
          marginBottom: '1rem',
          color: 'var(--accent-gold)',
        }}
      >
        Scan Configuration
      </h3>

      {/* Radius slider */}
      <div style={{ marginBottom: '1rem' }}>
        <label style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', display: 'block', marginBottom: '0.35rem' }}>
          Radius: {radius}
        </label>
        <input
          type="range"
          min={5}
          max={50}
          value={radius}
          onChange={(e) => setRadius(Number(e.target.value))}
          style={{ width: '100%', accentColor: 'var(--accent-gold)' }}
        />
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span>5</span>
          <span>50</span>
        </div>
      </div>

      {/* Population range */}
      <div style={{ display: 'flex', gap: '1rem', marginBottom: '1rem' }}>
        <div style={{ flex: 1 }}>
          <label style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', display: 'block', marginBottom: '0.35rem' }}>
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
        <div style={{ flex: 1 }}>
          <label style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', display: 'block', marginBottom: '0.35rem' }}>
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
      <div style={{ display: 'flex', gap: '1.5rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.85rem', color: 'var(--text-primary)', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={noPlayer}
            onChange={(e) => setNoPlayer(e.target.checked)}
            style={{ accentColor: 'var(--accent-gold)' }}
          />
          Exclude player-owned villages
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.85rem', color: 'var(--text-primary)', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={showOases}
            onChange={(e) => setShowOases(e.target.checked)}
            style={{ accentColor: 'var(--accent-gold)' }}
          />
          Include oases
        </label>
      </div>

      {/* Result limit */}
      <div style={{ marginBottom: '1.25rem' }}>
        <label style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', display: 'block', marginBottom: '0.35rem' }}>
          Result Limit
        </label>
        <input
          type="number"
          className="input-field"
          value={limit}
          min={1}
          max={500}
          onChange={(e) => setLimit(Number(e.target.value))}
          style={{ maxWidth: '150px' }}
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
      style={{
        padding: '0.6rem 0.75rem',
        textAlign: 'left',
        fontSize: '0.8rem',
        color: active ? 'var(--accent-gold)' : 'var(--text-secondary)',
        cursor: 'pointer',
        userSelect: 'none',
        whiteSpace: 'nowrap',
        borderBottom: '1px solid var(--border)',
        fontWeight: 600,
      }}
    >
      {label}{arrow}
    </th>
  )
}

function PlainHeader({ label }) {
  return (
    <th
      style={{
        padding: '0.6rem 0.75rem',
        textAlign: 'left',
        fontSize: '0.8rem',
        color: 'var(--text-secondary)',
        whiteSpace: 'nowrap',
        borderBottom: '1px solid var(--border)',
        fontWeight: 600,
      }}
    >
      {label}
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
    <div className="card" style={{ padding: '1rem' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '0.75rem',
          flexWrap: 'wrap',
          gap: '0.5rem',
        }}
      >
        <h3
          style={{
            fontFamily: 'Cinzel, serif',
            fontSize: '1.1rem',
            margin: 0,
            color: 'var(--accent-gold)',
          }}
        >
          Scan Results ({results.length} targets)
        </h3>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button className="btn-secondary" style={{ fontSize: '0.8rem', padding: '0.3rem 0.75rem' }} onClick={toggleAll}>
            {allSelected ? 'Deselect All' : 'Select All'}
          </button>
          <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', display: 'flex', alignItems: 'center' }}>
            {selected.size} selected
          </span>
        </div>
      </div>

      <div style={{ overflowX: 'auto', maxHeight: '400px', overflowY: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead style={{ position: 'sticky', top: 0, backgroundColor: 'var(--bg-card)', zIndex: 1 }}>
            <tr>
              <th style={{ padding: '0.6rem 0.75rem', borderBottom: '1px solid var(--border)', width: '40px' }}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                  style={{ accentColor: 'var(--accent-gold)' }}
                />
              </th>
              <PlainHeader label="Coords" />
              <PlainHeader label="Village Name" />
              <SortableHeader label="Population" field="population" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortableHeader label="Distance" field="distance" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <PlainHeader label="Player" />
              <PlainHeader label="Type" />
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
                  style={{
                    cursor: 'pointer',
                    backgroundColor: isSelected ? 'rgba(201, 168, 76, 0.08)' : 'transparent',
                    transition: 'background-color 0.15s',
                  }}
                  onMouseEnter={(e) => {
                    if (!isSelected) e.currentTarget.style.backgroundColor = 'rgba(255,255,255,0.03)'
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.backgroundColor = isSelected ? 'rgba(201, 168, 76, 0.08)' : 'transparent'
                  }}
                >
                  <td style={{ padding: '0.5rem 0.75rem', borderBottom: '1px solid var(--border)' }}>
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => toggleRow(origIdx)}
                      onClick={(e) => e.stopPropagation()}
                      style={{ accentColor: 'var(--accent-gold)' }}
                    />
                  </td>
                  <td style={{ padding: '0.5rem 0.75rem', borderBottom: '1px solid var(--border)', fontSize: '0.85rem', fontFamily: 'monospace', color: 'var(--accent-gold)' }}>
                    ({row.x}, {row.y})
                  </td>
                  <td style={{ padding: '0.5rem 0.75rem', borderBottom: '1px solid var(--border)', fontSize: '0.85rem' }}>
                    {row.name || '---'}
                  </td>
                  <td style={{ padding: '0.5rem 0.75rem', borderBottom: '1px solid var(--border)', fontSize: '0.85rem' }}>
                    {row.population ?? '---'}
                  </td>
                  <td style={{ padding: '0.5rem 0.75rem', borderBottom: '1px solid var(--border)', fontSize: '0.85rem', fontFamily: 'monospace' }}>
                    {row.distance != null ? row.distance.toFixed(1) : '---'}
                  </td>
                  <td style={{ padding: '0.5rem 0.75rem', borderBottom: '1px solid var(--border)', fontSize: '0.85rem', color: row.player ? 'var(--text-primary)' : 'var(--text-secondary)', fontStyle: row.player ? 'normal' : 'italic' }}>
                    {row.player || 'Unoccupied'}
                  </td>
                  <td style={{ padding: '0.5rem 0.75rem', borderBottom: '1px solid var(--border)', fontSize: '0.85rem' }}>
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

  const addMessage = useCallback((type, text) => {
    setMessages((prev) => [...prev, { type, text, timestamp: Date.now() }])
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

    wsRef.current = ws

    // Send config after connection opens
    const origOnOpen = ws.onopen
    ws.onopen = () => {
      origOnOpen?.()
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

  return (
    <div className="card">
      <h3
        style={{
          fontFamily: 'Cinzel, serif',
          fontSize: '1.1rem',
          marginBottom: '1rem',
          color: 'var(--accent-gold)',
        }}
      >
        Auto-Scout
      </h3>

      <div style={{ display: 'flex', gap: '1rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        {/* Scout amount */}
        <div style={{ flex: '1 1 120px', minWidth: '120px' }}>
          <label style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', display: 'block', marginBottom: '0.35rem' }}>
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
        <div style={{ flex: '1 1 120px', minWidth: '120px' }}>
          <label style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', display: 'block', marginBottom: '0.35rem' }}>
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
      <div style={{ marginBottom: '1.25rem' }}>
        <label style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', display: 'block', marginBottom: '0.5rem' }}>
          Scout type
        </label>
        <div style={{ display: 'flex', gap: '1.5rem' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85rem', color: 'var(--text-primary)', cursor: 'pointer' }}>
            <input
              type="radio"
              name="scoutType"
              value="resources"
              checked={scoutType === 'resources'}
              onChange={() => setScoutType('resources')}
              disabled={running}
              style={{ accentColor: 'var(--accent-gold)' }}
            />
            Resources
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85rem', color: 'var(--text-primary)', cursor: 'pointer' }}>
            <input
              type="radio"
              name="scoutType"
              value="defenses"
              checked={scoutType === 'defenses'}
              onChange={() => setScoutType('defenses')}
              disabled={running}
              style={{ accentColor: 'var(--accent-gold)' }}
            />
            Defenses
          </label>
        </div>
      </div>

      {/* Buttons */}
      <div style={{ display: 'flex', gap: '0.75rem', marginBottom: '1rem' }}>
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
        <div style={{ marginBottom: '1rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '0.35rem' }}>
            <span>Scouting target {progress.index}/{progress.total}...</span>
            <span>{Math.round((progress.index / progress.total) * 100)}%</span>
          </div>
          <div
            style={{
              height: '6px',
              backgroundColor: 'var(--bg-surface)',
              borderRadius: '3px',
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                height: '100%',
                width: `${(progress.index / progress.total) * 100}%`,
                backgroundColor: 'var(--accent-gold)',
                borderRadius: '3px',
                transition: 'width 0.3s ease',
              }}
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
    <div style={{ padding: '1.5rem', maxWidth: '1100px', margin: '0 auto' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '1.25rem',
        }}
      >
        <h2 style={{ fontFamily: 'Cinzel, serif', fontSize: '1.5rem', margin: 0 }}>Auto Scout</h2>
        <VillageSelector />
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
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
          <div className="card" style={{ textAlign: 'center', padding: '2rem' }}>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
              No targets found matching your criteria. Try increasing the radius or adjusting filters.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
