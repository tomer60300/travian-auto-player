import { useState, useEffect, useRef, useCallback } from 'react'
import { createWebSocket } from '../ws'
import api from '../api'
import WebSocketPanel from '../components/WebSocketPanel'

const typeIcons = {
  'queue': '[Q]',
  'farm-run': '[F]',
  'farm-run-all': '[F*]',
  'scout-auto': '[S]',
  'scout-scan': '[S~]',
}

const typeLabels = {
  'queue': 'Build Queue',
  'farm-run': 'Farm Run',
  'farm-run-all': 'Farm Run All',
  'scout-auto': 'Auto Scout',
  'scout-scan': 'Map Scan',
}

function timeAgo(ts) {
  if (!ts) return ''
  const diff = Math.floor((Date.now() / 1000) - ts)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function formatMessage(data) {
  if (!data) return ''
  if (data.message) return data.message
  if (data.type === 'cycle_start') return `Cycle ${data.cycle} started`
  if (data.type === 'cycle_end') return `Cycle ${data.cycle} done - sent: ${data.sent}, failed: ${data.failed}`
  if (data.type === 'step_complete') return `${data.building} Lv${data.level} - ${data.success ? 'OK' : 'Failed'}`
  if (data.type === 'complete') return `Completed${data.reason ? ` (${data.reason})` : ''}`
  if (data.type === 'result') return `Result: ${data.success !== undefined ? (data.success ? 'OK' : 'Failed') : JSON.stringify(data).slice(0, 80)}`
  if (data.type === 'session_init') return `Session: ${data.session_id}`
  if (data.type === 'scanning') return 'Scanning map...'
  if (data.type === 'scan_complete') return `Scan complete: ${data.targets} targets`
  if (data.type === 'scouting') return data.message || 'Scouting...'
  if (data.type === 'scout_result') return data.message || 'Scout sent'
  return data.type || JSON.stringify(data).slice(0, 100)
}

function typeToLevel(type) {
  if (type === 'error') return 'error'
  if (type === 'complete' || type === 'step_complete' || type === 'scout_result') return 'success'
  if (type === 'warning') return 'warning'
  return 'info'
}

export default function Sessions() {
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [messages, setMessages] = useState([])
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [sessionMeta, setSessionMeta] = useState(null)
  const [ended, setEnded] = useState(false)
  const wsRef = useRef(null)
  const msgIdRef = useRef(0)

  // Fetch session list
  const fetchSessions = useCallback(async () => {
    try {
      const res = await api.get('/api/sessions')
      setSessions(res.data)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchSessions()
    const id = setInterval(fetchSessions, 10000)
    return () => clearInterval(id)
  }, [fetchSessions])

  // Connect to a session
  const connectToSession = useCallback((sessionId) => {
    // Disconnect previous
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }

    setSelected(sessionId)
    setMessages([])
    setSessionMeta(null)
    setEnded(false)
    setWsStatus('connected')

    const handle = createWebSocket(
      `/ws/sessions/${sessionId}/stream`,
      (data) => {
        if (data.type === 'session_meta') {
          setSessionMeta(data)
          return
        }
        if (data.type === 'history') {
          const hist = (data.messages || []).map((m) => ({
            id: ++msgIdRef.current,
            type: typeToLevel(m.type),
            text: formatMessage(m),
            timestamp: m.ts ? m.ts * 1000 : Date.now(),
          }))
          setMessages(hist)
          return
        }
        if (data.type === 'session_ended') {
          setEnded(true)
          setWsStatus('disconnected')
          setMessages((prev) => [
            ...prev,
            {
              id: ++msgIdRef.current,
              type: 'warning',
              text: 'Session has ended',
              timestamp: Date.now(),
            },
          ])
          return
        }
        if (data.type === 'message' && data.data) {
          setMessages((prev) => [
            ...prev,
            {
              id: ++msgIdRef.current,
              type: typeToLevel(data.data.type),
              text: formatMessage(data.data),
              timestamp: data.data.ts ? data.data.ts * 1000 : Date.now(),
            },
          ])
          return
        }
        if (data.type === 'error') {
          setMessages((prev) => [
            ...prev,
            {
              id: ++msgIdRef.current,
              type: 'error',
              text: data.message || 'Error',
              timestamp: Date.now(),
            },
          ])
        }
      },
      () => setWsStatus('disconnected'),
      () => {
        setWsStatus('disconnected')
        wsRef.current = null
      },
      { reconnect: true, maxRetries: 5 },
    )

    wsRef.current = handle
  }, [])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [])

  const goBack = () => {
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setSelected(null)
    setMessages([])
    setSessionMeta(null)
    setEnded(false)
    setWsStatus('disconnected')
    fetchSessions()
  }

  const running = sessions.filter((s) => s.status === 'running')
  const disconnected = sessions.filter((s) => s.status === 'disconnected')

  // Session viewer
  if (selected) {
    return (
      <div className="p-4 space-y-4">
        <div className="flex items-center gap-3">
          <button onClick={goBack} className="btn-secondary btn-sm">
            Back
          </button>
          <h2 className="heading-gold text-lg">
            {sessionMeta ? sessionMeta.label : `Session ${selected}`}
          </h2>
          {sessionMeta && (
            <span
              className={`text-xs px-2 py-0.5 rounded-full font-semibold ${
                ended || sessionMeta.status === 'disconnected'
                  ? 'bg-[var(--surface)] text-secondary'
                  : 'bg-[var(--success)]/20 text-[var(--success)]'
              }`}
            >
              {ended || sessionMeta.status === 'disconnected' ? 'Ended' : 'Live'}
            </span>
          )}
        </div>

        {sessionMeta && (
          <div className="flex gap-4 text-xs text-secondary">
            <span>Type: {typeLabels[sessionMeta.session_type] || sessionMeta.session_type}</span>
            <span>ID: <code className="text-primary">{sessionMeta.id}</code></span>
            <span>Started: {timeAgo(sessionMeta.created_at)}</span>
          </div>
        )}

        <WebSocketPanel
          messages={messages}
          status={wsStatus}
          onClear={() => setMessages([])}
        />
      </div>
    )
  }

  // Session list
  return (
    <div className="p-4 space-y-6">
      <h1 className="heading-gold text-xl">Sessions</h1>
      <p className="text-secondary text-sm">
        View live execution logs from any device. Start a task (Build Queue, Farm Loop, Auto Scout)
        and connect here to monitor it remotely.
      </p>

      {loading ? (
        <div className="flex justify-center py-8">
          <div className="spinner spinner-md" />
        </div>
      ) : sessions.length === 0 ? (
        <div className="text-center py-12 text-secondary">
          <p className="text-lg mb-2">No active or recent sessions</p>
          <p className="text-sm">
            Start a task (Build Queue, Farm Loop, Auto Scout) on any device to see it here.
          </p>
        </div>
      ) : (
        <>
          {running.length > 0 && (
            <div>
              <h2 className="text-sm font-semibold text-[var(--success)] mb-2">
                Running ({running.length})
              </h2>
              <div className="space-y-2">
                {running.map((s) => (
                  <SessionCard key={s.id} session={s} onClick={() => connectToSession(s.id)} />
                ))}
              </div>
            </div>
          )}

          {disconnected.length > 0 && (
            <div>
              <h2 className="text-sm font-semibold text-secondary mb-2">
                Disconnected ({disconnected.length})
              </h2>
              <div className="space-y-2">
                {disconnected.map((s) => (
                  <SessionCard key={s.id} session={s} onClick={() => connectToSession(s.id)} />
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function SessionCard({ session, onClick }) {
  const isRunning = session.status === 'running'

  return (
    <button
      onClick={onClick}
      className="w-full text-left bg-surface border-default rounded-lg p-3 hover:border-[var(--primary)] transition-colors cursor-pointer"
      style={{ border: '1px solid var(--color-border, #333)' }}
    >
      <div className="flex items-center gap-3">
        <span className="text-lg font-mono text-secondary" style={{ minWidth: 36, textAlign: 'center' }}>
          {typeIcons[session.session_type] || '[?]'}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-primary font-medium truncate">
              {session.label}
            </span>
            <span className={`status-dot ${isRunning ? 'status-dot-success' : 'status-dot-danger'}`} />
          </div>
          <div className="flex items-center gap-3 text-xs text-secondary mt-0.5">
            <code>{session.id}</code>
            <span>{timeAgo(session.created_at)}</span>
            <span>{session.message_count} msgs</span>
          </div>
        </div>
        <span className="text-secondary text-sm">{'>'}</span>
      </div>
    </button>
  )
}
