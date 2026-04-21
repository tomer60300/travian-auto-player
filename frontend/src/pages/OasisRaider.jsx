import { useState, useRef, useCallback, useEffect } from 'react'
import { createWebSocket } from '../ws'
import { useToast } from '../components/Toast'
import VillageSelector from '../components/VillageSelector'
import useGameStore from '../stores/gameStore'
import { TRIBE_TROOPS } from '../constants/troops'

const BONUS_TYPES = ['Wood', 'Clay', 'Iron', 'Crop']

const LEVEL_COLORS = {
  error: 'text-danger',
  success: 'text-success',
  warning: 'text-warning',
}

const CATEGORY_COLORS = {
  TROOPS: 'text-blue-400',
  SCAN: 'text-purple-400',
  FILTER: 'text-secondary',
  ENRICH: 'text-cyan-400',
  CLASSIFY: 'text-secondary',
  RAID: 'text-primary',
  'DRY RUN': 'text-cyan-400',
  SLEEP: 'text-yellow-400',
  DONE: 'text-success',
  SUMMARY: 'text-gold',
  SORT: 'text-secondary',
  NEXT: 'text-secondary',
  STOP: 'text-warning',
  SYSTEM: 'text-secondary',
  ERROR: 'text-danger',
  HUMANIZE: 'text-purple-400',
  BROWSE: 'text-cyan-400',
  THINK: 'text-blue-400',
  BREAK: 'text-yellow-400',
  SKIP: 'text-warning',
}

const STATUS_CONFIG = {
  idle: { label: 'Idle', dot: 'status-dot-secondary' },
  running: { label: 'Running', dot: 'status-dot-warning' },
  sleeping: { label: 'Sleeping', dot: 'status-dot-warning' },
  reconnecting: { label: 'Reconnecting...', dot: 'status-dot-warning' },
  completed: { label: 'Completed', dot: 'status-dot-success' },
  stopped: { label: 'Stopped', dot: 'status-dot-danger' },
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleTimeString('en-US', { hour12: false })
}

export default function OasisRaider() {
  // Config state
  const [radius, setRadius] = useState(15)
  const [troopRows, setTroopRows] = useState([
    { type: 't1', amount: 20 },
    { type: 't6', amount: 1 },
  ])
  const [maxTargets, setMaxTargets] = useState(0)
  const [bonusFilter, setBonusFilter] = useState(['Wood', 'Clay', 'Iron', 'Crop'])
  const [sleepInterval, setSleepInterval] = useState(60)
  const [repeatIntervalSeconds, setRepeatIntervalSeconds] = useState(0)

  // Operation state
  const [status, setStatus] = useState('idle')
  const [logs, setLogs] = useState([])
  const [summary, setSummary] = useState(null)

  const wsRef = useRef(null)
  const logEndRef = useRef(null)
  const mountedRef = useRef(true)
  const msgIdRef = useRef(0)

  const tribeId = useGameStore((s) => s.tribeId) || 2
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const troopNames = TRIBE_TROOPS[tribeId] || TRIBE_TROOPS[2]
  const toast = useToast()

  useEffect(() => {
    return () => {
      mountedRef.current = false
      if (wsRef.current) {
        // Handle both reconnect wrapper { ws, close() } and raw WebSocket
        try {
          if (typeof wsRef.current.close === 'function') wsRef.current.close()
        } catch { /* ignore */ }
      }
    }
  }, [])

  // Auto-scroll logs
  useEffect(() => {
    if (logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [logs])

  const addLog = useCallback((emoji, category, message, level) => {
    setLogs((prev) => [
      ...prev,
      {
        id: ++msgIdRef.current,
        timestamp: Date.now(),
        emoji,
        category,
        message,
        level,
      },
    ])
  }, [])

  const buildTroopsDict = () => {
    const troops = {}
    for (const row of troopRows) {
      if (row.amount > 0) {
        troops[row.type] = (troops[row.type] || 0) + row.amount
      }
    }
    return troops
  }

  // Store the last config so onReconnected can re-send it
  const lastConfigRef = useRef(null)

  const handleStart = (dryRun = false) => {
    const troops = buildTroopsDict()
    if (Object.keys(troops).length === 0) {
      toast.warning('No troops configured')
      return
    }

    setLogs([])
    setSummary(null)
    setStatus('running')

    const startPayload = {
      action: 'start',
      config: {
        radius,
        troops,
        max_targets: maxTargets,
        bonus_filter:
          bonusFilter.length === BONUS_TYPES.length
            ? []
            : bonusFilter.map((b) => b.toLowerCase()),
        sleep_interval: sleepInterval,
        dry_run: dryRun,
        village_id: activeVillageId || undefined,
        repeat_interval_seconds: repeatIntervalSeconds,
      },
    }
    lastConfigRef.current = startPayload

    const handle = createWebSocket(
      '/ws/oasis-raider',
      (data) => {
        if (!mountedRef.current) return
        switch (data.type) {
          case 'session_init':
            addLog('📡', 'SYSTEM', `Session: ${data.session_id}`, 'info')
            // Send config as soon as backend session is ready (first connect + reconnect)
            if (lastConfigRef.current) {
              const rawWs = wsRef.current?.ws || wsRef.current
              if (rawWs && rawWs.readyState === WebSocket.OPEN) {
                rawWs.send(JSON.stringify(lastConfigRef.current))
              }
            }
            break
          case 'status': {
            const state = data.data?.state || 'idle'
            setStatus(state)
            // Sweep finished — stop reconnecting so we don't restart it
            if (state === 'completed' || state === 'stopped') {
              lastConfigRef.current = null
              // Cleanly close the reconnect handle (no more retries)
              if (wsRef.current?.close) {
                setTimeout(() => { try { wsRef.current?.close() } catch {} }, 500)
              }
            }
            break
          }
          case 'log': {
            const d = data.data || {}
            addLog(d.emoji || '', d.category || '', d.message || '', d.level || 'info')
            if (d.category === 'TROOPS' && d.message?.includes('entering sleep')) setStatus('sleeping')
            if (d.category === 'SLEEP' && d.message?.includes('SUFFICIENT')) setStatus('running')
            break
          }
          case 'summary':
            setSummary(data.data)
            break
          case 'error':
            addLog('❌', 'ERROR', data.message || 'Unknown error', 'error')
            toast.error(data.message || 'Error')
            break
          default:
            break
        }
      },
      (event) => {
        if (mountedRef.current) {
          addLog('❌', 'SYSTEM', 'WebSocket error', 'error')
        }
      },
      () => {
        // Only fires on final close (after all retries exhausted)
        if (mountedRef.current) {
          setStatus((prev) => (prev === 'completed' ? prev : 'stopped'))
        }
      },
      {
        reconnect: true,
        maxRetries: 5,
        onReconnecting: () => {
          if (mountedRef.current) {
            addLog('🔄', 'SYSTEM', 'Connection lost — reconnecting...', 'warning')
            setStatus('reconnecting')
          }
        },
        onReconnected: () => {
          if (mountedRef.current) {
            addLog('✅', 'SYSTEM', 'Reconnected — resuming operation', 'success')
            setStatus('running')
            // Config is re-sent when session_init arrives (see case above)
          }
        },
      },
    )

    if (!handle) {
      toast.error('No auth token')
      setStatus('idle')
      return
    }
    wsRef.current = handle
  }

  const handleStop = () => {
    lastConfigRef.current = null
    if (wsRef.current) {
      // Send stop action through the current live socket
      const ws = wsRef.current.ws || wsRef.current
      try {
        ws.send(JSON.stringify({ action: 'stop' }))
      } catch { /* ignore */ }
      // Close the reconnect handle (prevents auto-reconnect)
      setTimeout(() => {
        if (wsRef.current) {
          try {
            wsRef.current.close()
          } catch { /* ignore */ }
        }
      }, 1000)
    }
    setStatus('stopped')
    addLog('⛔', 'STOP', 'Stop requested by user', 'warning')
  }

  // Troop row management
  const addTroopRow = () => setTroopRows([...troopRows, { type: 't1', amount: 0 }])
  const removeTroopRow = (index) => setTroopRows(troopRows.filter((_, i) => i !== index))
  const updateTroopRow = (index, field, value) =>
    setTroopRows(troopRows.map((row, i) => (i === index ? { ...row, [field]: value } : row)))

  const toggleBonusFilter = (bonus) =>
    setBonusFilter((prev) =>
      prev.includes(bonus) ? prev.filter((b) => b !== bonus) : [...prev, bonus],
    )

  const isRunning = status === 'running' || status === 'sleeping' || status === 'reconnecting'
  const st = STATUS_CONFIG[status] || STATUS_CONFIG.idle

  return (
    <div className="p-6 max-w-[1100px] mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Oasis Raider</h2>
        <div className="flex items-center gap-3">
          <VillageSelector />
          <span className={`status-dot ${st.dot}`} />
          <span className="text-sm text-secondary">{st.label}</span>
        </div>
      </div>

      {/* ── Config Panel ─────────────────────────────────────────── */}
      <div className="card mb-4">
        <h3 className="heading-gold text-lg mb-4">Configuration</h3>

        {/* Scan Radius */}
        <div className="mb-4">
          <label className="field-label-lg">Scan Radius</label>
          <input
            type="number"
            className="input-field w-32"
            value={radius}
            min={1}
            max={50}
            onChange={(e) => setRadius(Number(e.target.value))}
            disabled={isRunning}
            placeholder="15"
          />
        </div>

        {/* Troop Composition */}
        <div className="mb-4">
          <label className="field-label-lg mb-2">Troop Composition</label>
          {troopRows.map((row, i) => {
            const troopIdx = parseInt(row.type.slice(1), 10) - 1
            const selectedName = troopNames[troopIdx] || row.type
            return (
              <div key={i} className="flex gap-2 items-center mb-2">
                <div className="flex-1 relative">
                  <select
                    className="absolute inset-0 opacity-0 cursor-pointer"
                    value={row.type}
                    onChange={(e) => updateTroopRow(i, 'type', e.target.value)}
                    disabled={isRunning}
                  >
                    {troopNames.map((name, idx) => (
                      <option key={idx} value={`t${idx + 1}`}>
                        {name} (t{idx + 1})
                      </option>
                    ))}
                  </select>
                  <div className="input-field pointer-events-none flex items-center justify-between">
                    <span>{selectedName}</span>
                    <span className="text-secondary text-xs ml-2">&#9662;</span>
                  </div>
                </div>
                <input
                  type="number"
                  className="input-field w-24"
                  value={row.amount}
                  min={0}
                  onChange={(e) => updateTroopRow(i, 'amount', Number(e.target.value))}
                  disabled={isRunning}
                  placeholder="0"
                />
                {troopRows.length > 1 && (
                  <button
                    className="btn-danger btn-xs"
                    onClick={() => removeTroopRow(i)}
                    disabled={isRunning}
                  >
                    x
                  </button>
                )}
              </div>
            )
          })}
          <button className="btn-secondary btn-xs" onClick={addTroopRow} disabled={isRunning}>
            + Add Troop
          </button>
        </div>

        {/* Max Targets + Sleep Interval */}
        <div className="flex gap-4 mb-4 flex-wrap">
          <div>
            <label className="field-label-lg">Max Targets</label>
            <input
              type="number"
              className="input-field w-24"
              value={maxTargets}
              min={0}
              onChange={(e) => setMaxTargets(Number(e.target.value))}
              disabled={isRunning}
              placeholder="0"
            />
            <p className="text-xs text-secondary mt-1">0 = unlimited</p>
          </div>
          <div>
            <label className="field-label-lg">Sleep Interval (s)</label>
            <input
              type="number"
              className="input-field w-24"
              value={sleepInterval}
              min={10}
              onChange={(e) => setSleepInterval(Number(e.target.value))}
              disabled={isRunning}
              placeholder="60"
            />
            <p className="text-xs text-secondary mt-1">Between troop re-checks</p>
          </div>
          <div>
            <label className="field-label-lg">Repeat Interval (s)</label>
            <input
              type="number"
              className="input-field w-24"
              value={repeatIntervalSeconds}
              min={0}
              onChange={(e) => setRepeatIntervalSeconds(Number(e.target.value))}
              disabled={isRunning}
              placeholder="0"
            />
            <p className="text-xs text-secondary mt-1">
              0 = single run. &gt;0 = re-run after sweep completes (e.g. 3600 = every hour)
            </p>
          </div>
        </div>

        {/* Bonus Filter */}
        <div className="mb-5">
          <label className="field-label-lg mb-2">Bonus Filter</label>
          <div className="flex gap-4 flex-wrap">
            {BONUS_TYPES.map((bonus) => (
              <label key={bonus} className="check-label">
                <input
                  type="checkbox"
                  className="checkbox-gold"
                  checked={bonusFilter.includes(bonus)}
                  onChange={() => toggleBonusFilter(bonus)}
                  disabled={isRunning}
                />
                {bonus}
              </label>
            ))}
          </div>
          <p className="text-xs text-secondary mt-1">
            Only raid oases matching selected bonuses. All checked = no filter.
          </p>
        </div>

        {/* Buttons */}
        <div className="flex gap-3">
          {!isRunning ? (
            <>
              <button className="btn-secondary" onClick={() => handleStart(true)}>
                Dry Run
              </button>
              <button className="btn-primary" onClick={() => handleStart(false)}>
                Start Raiding
              </button>
            </>
          ) : (
            <button className="btn-danger" onClick={handleStop}>
              Stop
            </button>
          )}
        </div>
      </div>

      {/* ── Live Log Stream ──────────────────────────────────────── */}
      {logs.length > 0 && (
        <div className="card">
          <div className="flex justify-between items-center mb-3">
            <h3 className="heading-gold text-lg">Live Log</h3>
            <div className="flex gap-2 items-center">
              <span className={`status-dot ${st.dot}`} />
              <span className="text-xs text-secondary">{st.label}</span>
              <button className="btn-secondary btn-xs" onClick={() => setLogs([])}>
                Clear
              </button>
            </div>
          </div>

          <div className="ws-panel" style={{ maxHeight: 500 }}>
            {logs.map((log) => {
              const color =
                LEVEL_COLORS[log.level] || CATEGORY_COLORS[log.category] || 'text-primary'
              const isSummary = log.category === 'SUMMARY'
              return (
                <div
                  key={log.id}
                  className={`ws-panel-line ${color} ${isSummary ? 'font-semibold' : ''}`}
                >
                  <span className="ws-panel-time">[{formatTime(log.timestamp)}]</span>
                  {log.emoji} {log.category} — {log.message}
                </div>
              )
            })}
            <div ref={logEndRef} />
          </div>

          {/* Summary card */}
          {summary && (
            <div className="mt-3 p-3 bg-surface rounded border-default">
              <h4 className="text-sm font-semibold text-gold mb-2">Summary</h4>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-secondary">
                <span>Total targets: {summary.total}</span>
                <span>Raids sent: {summary.sent}</span>
                {summary.skipped_animals?.length > 0 && (
                  <span>Skipped (animals): {summary.skipped_animals.length}</span>
                )}
                {summary.skipped_random > 0 && (
                  <span>Skipped (human-skip): {summary.skipped_random}</span>
                )}
                {summary.skipped_troops > 0 && (
                  <span>Skipped (no troops): {summary.skipped_troops}</span>
                )}
                {summary.browse_pauses > 0 && <span>Map browses: {summary.browse_pauses}</span>}
                {summary.breaks_taken > 0 && (
                  <span>Micro-breaks: {summary.breaks_taken} ({Math.round(summary.break_time)}s)</span>
                )}
                {summary.think_delays?.length > 0 && (
                  <span>Avg think: {(summary.think_delays.reduce((a, b) => a + b, 0) / summary.think_delays.length).toFixed(1)}s</span>
                )}
                {summary.order_entropy != null && (
                  <span>Order entropy: {summary.order_entropy.toFixed(2)}/1.00</span>
                )}
                {summary.duration > 0 && <span>Duration: {Math.round(summary.duration)}s</span>}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
