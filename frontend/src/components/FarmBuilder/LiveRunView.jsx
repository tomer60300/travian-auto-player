import { useEffect, useRef, useState } from 'react'
import { createWebSocket } from '../../ws'
import { useToast } from '../Toast'

const LEVEL_COLORS = {
  error: 'text-danger',
  success: 'text-success',
  warning: 'text-warning',
  info: 'text-primary',
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleTimeString('en-US', { hour12: false })
}

export default function LiveRunView({ sessionId, onClose }) {
  const [status, setStatus] = useState('connecting')
  const [logs, setLogs] = useState([])
  const [phaseCounts, setPhaseCounts] = useState({}) // {phase: {done, total}}
  const [added, setAdded] = useState([])
  const [skipped, setSkipped] = useState([])
  const [report, setReport] = useState(null)
  const [tab, setTab] = useState('completed')
  const wsRef = useRef(null)
  const mountedRef = useRef(true)
  const msgId = useRef(0)
  const toast = useToast()

  useEffect(() => {
    return () => { mountedRef.current = false }
  }, [])

  const addLog = (level, category, emoji, message) => {
    if (!mountedRef.current) return
    setLogs((prev) => [...prev, {
      id: ++msgId.current,
      level, category, emoji, message,
      timestamp: Date.now(),
    }])
  }

  useEffect(() => {
    if (!sessionId) return

    const handle = createWebSocket(
      `/ws/sessions/${sessionId}/stream`,
      (data) => {
        if (!mountedRef.current) return
        switch (data.type) {
          case 'session_meta':
            setStatus(data.status || 'running')
            break
          case 'history':
            // Replay buffer
            (data.messages || []).forEach((m) => processMessage(m))
            break
          case 'message':
            processMessage(data.data)
            break
          case 'session_ended':
            setStatus('completed')
            break
          case 'error':
            toast.error(data.message || 'WS error')
            addLog('error', 'SYSTEM', '❌', data.message || 'error')
            break
          default:
            break
        }
      },
      () => addLog('error', 'SYSTEM', '❌', 'WebSocket error'),
      () => { if (mountedRef.current && status === 'connecting') setStatus('disconnected') },
      {
        reconnect: true,
        maxRetries: 10,
        onReconnecting: () => addLog('warning', 'SYSTEM', '🔄', 'Reconnecting...'),
        onReconnected: () => addLog('success', 'SYSTEM', '✅', 'Reconnected'),
      }
    )
    wsRef.current = handle
    return () => {
      if (handle?.close) handle.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  const processMessage = (m) => {
    if (!m || !m.type) return
    if (m.type === 'log' && m.data) {
      const { level, category, emoji, message } = m.data
      addLog(level || 'info', category || '', emoji || '', message || '')
    } else if (m.type === 'phase_start') {
      setPhaseCounts((prev) => ({ ...prev, [m.phase]: { done: 0, total: m.total || 0 } }))
    } else if (m.type === 'phase_progress') {
      setPhaseCounts((prev) => ({ ...prev, [m.phase]: { done: m.completed || 0, total: m.total || 0 } }))
    } else if (m.type === 'target_result') {
      if (m.phase === 'assign' && m.status !== 'failed') {
        setAdded((prev) => [...prev, m])
      } else if (m.matched_row?.startsWith?.('SKIP')) {
        setSkipped((prev) => [...prev, m])
      }
    } else if (m.type === 'complete') {
      setReport(m.report)
      setStatus('completed')
    } else if (m.type === 'status') {
      setStatus(m.data?.state || status)
    } else if (m.type === 'error') {
      addLog('error', 'ERROR', '❌', m.message || '')
      toast.error(m.message || 'Error')
    }
  }

  const handleStop = () => {
    const ws = wsRef.current?.ws || wsRef.current
    if (ws) {
      try { ws.send(JSON.stringify({ action: 'stop' })) } catch { /* */ }
    }
    addLog('warning', 'STOP', '⛔', 'Stop requested')
  }

  const running = status === 'running' || status === 'connecting'

  const PhaseBar = ({ phase, label }) => {
    const c = phaseCounts[phase]
    if (!c) return null
    const pct = c.total > 0 ? Math.round((c.done / c.total) * 100) : 0
    return (
      <div className="mb-2">
        <div className="flex justify-between text-xs text-secondary mb-1">
          <span>{label}</span>
          <span>{c.done}/{c.total} ({pct}%)</span>
        </div>
        <div className="progress-track">
          <div className="progress-fill" style={{ width: `${pct}%` }} />
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-[1200px] mx-auto">
      <div className="flex justify-between items-center mb-4">
        <h2 className="heading-gold text-2xl">Farm Builder — Live Run</h2>
        <div className="flex items-center gap-3">
          <span className={`status-badge status-badge-${status === 'running' ? 'running' : status === 'completed' ? 'success' : status === 'stopped' ? 'error' : 'waiting'}`}>
            {status}
          </span>
          {running && <button className="btn-danger" onClick={handleStop}>Stop</button>}
          {!running && <button className="btn-secondary" onClick={onClose}>Close</button>}
        </div>
      </div>

      <div className="card mb-4">
        <h3 className="heading-gold text-lg mb-3">Progress</h3>
        <PhaseBar phase="scan" label="Scan" />
        <PhaseBar phase="filter" label="Filter" />
        <PhaseBar phase="create_lists" label="Create lists" />
        <PhaseBar phase="defense_scan" label="Defense-scan" />
        <PhaseBar phase="assign" label="Assign" />
      </div>

      <div className="card mb-4">
        <div className="flex justify-between items-center mb-2">
          <h3 className="heading-gold text-lg">Live log</h3>
          <button className="btn-secondary btn-xs" onClick={() => setLogs([])}>Clear</button>
        </div>
        <div className="ws-panel" style={{ maxHeight: 400 }}>
          {logs.map((l) => (
            <div key={l.id} className={`ws-panel-line ${LEVEL_COLORS[l.level] || 'text-primary'}`}>
              <span className="text-secondary">{formatTime(l.timestamp)}</span>
              {' '}{l.emoji}{' '}
              <span className="text-xs text-secondary">[{l.category}]</span>
              {' '}{l.message}
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="flex gap-2 mb-3">
          <button
            className={tab === 'completed' ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
            onClick={() => setTab('completed')}
          >Completed ({added.length})</button>
          <button
            className={tab === 'skipped' ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
            onClick={() => setTab('skipped')}
          >Skipped ({skipped.length})</button>
        </div>
        <div className="overflow-auto" style={{ maxHeight: 300 }}>
          {tab === 'completed' ? (
            <table className="data-table w-full text-sm">
              <thead><tr><th>coord</th><th>list</th><th>def</th><th>troops</th><th>status</th></tr></thead>
              <tbody>
                {added.map((r, i) => (
                  <tr key={i}>
                    <td>({r.x}|{r.y})</td>
                    <td>{r.list_id}</td>
                    <td>{r.def_total ?? r.def ?? '-'}</td>
                    <td>{JSON.stringify(r.troops || {})}</td>
                    <td>{r.status || 'ok'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <table className="data-table w-full text-sm">
              <thead><tr><th>coord</th><th>reason</th></tr></thead>
              <tbody>
                {skipped.map((r, i) => (
                  <tr key={i}>
                    <td>({r.x}|{r.y})</td>
                    <td>{r.matched_row || r.reason || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {report && (
        <div className="card mt-4">
          <h3 className="heading-gold text-lg mb-3">Report</h3>
          <pre className="text-xs overflow-auto" style={{ maxHeight: 300 }}>
            {JSON.stringify(report, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
