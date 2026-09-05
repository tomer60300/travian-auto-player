import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { createWebSocket } from '../ws'
import { useToast } from '../components/Toast'
import api from '../api'
import FetchError from '../components/FetchError'
import { readErrorDetail } from '../utils/fetchError'
import WebSocketPanel from '../components/WebSocketPanel'

const typeIcons = {
  'queue': '[Q]',
  'farm-run': '[F]',
  'farm-run-all': '[F*]',
  'scout-auto': '[S]',
  'scout-scan': '[S~]',
  'oasis-raider': '[O]',
  'farm-builder': '[FB]',
}

const typeLabels = {
  'queue': 'Build Queue',
  'farm-run': 'Farm Run',
  'farm-run-all': 'Farm Run All',
  'scout-auto': 'Auto Scout',
  'scout-scan': 'Map Scan',
  'oasis-raider': 'Oasis Raider',
  'farm-builder': 'Farm Builder',
}

const typeRoutes = {
  'queue': '/queue',
  'farm-run': '/farm',
  'farm-run-all': '/farm',
  'scout-auto': '/scout',
  'scout-scan': '/scout',
  'oasis-raider': '/oasis-raider',
  'farm-builder': '/farm-builder',
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
  if (data.type === 'trigger_info') return `$ ${data.command}`
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
  if (data.type === 'log') {
    const d = data.data || {}
    return `${d.emoji || ''} ${d.category || ''} — ${d.message || ''}`
  }
  return data.type || JSON.stringify(data).slice(0, 100)
}

function typeToLevel(type) {
  if (type === 'error') return 'error'
  if (type === 'complete' || type === 'step_complete' || type === 'scout_result') return 'success'
  if (type === 'trigger_info') return 'warning'
  if (type === 'warning') return 'warning'
  if (type === 'log') return 'info'
  return 'info'
}

export default function Sessions() {
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [listError, setListError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [messages, setMessages] = useState([])
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [sessionMeta, setSessionMeta] = useState(null)
  const [ended, setEnded] = useState(false)
  const [stopping, setStopping] = useState(false)
  const wsRef = useRef(null)
  const msgIdRef = useRef(0)
  const navigate = useNavigate()
  const toast = useToast()

  // Fetch session list. The catch used to be a bare `catch {}`, which left
  // `sessions` at its previous value -- `[]` on the first poll -- so a
  // backend that was down rendered "No active or recent sessions", the exact
  // words for a machine with nothing running. This page is the one place an
  // operator checks whether something IS running, so that answer being wrong
  // silently is the worst version of the defect.
  const fetchSessions = useCallback(async () => {
    try {
      const res = await api.get('/sessions')
      setSessions(res.data)
      setListError(null)
    } catch (e) {
      setListError(readErrorDetail(e, 'Could not reach the server'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchSessions()
    const id = setInterval(fetchSessions, 5000)
    return () => clearInterval(id)
  }, [fetchSessions])

  // Stop ALL active operations (destructive global action). Guarded by a
  // confirm() so a misclick from the per-session detail view (where the
  // single-session "Stop" used to incorrectly invoke this) and from the
  // session-list top-bar don't fan out a kill to every running op.
  const handleStopAll = async () => {
    const n = running.length
    if (n > 1) {
      const ok = window.confirm(
        `Stop ALL ${n} active operations? This signals every running op to halt; reruns must be initiated manually.`
      )
      if (!ok) return
    }
    setStopping(true)
    try {
      await api.post('/sessions/stop-all')
      toast.success(`Stop signal sent to ${n} operation${n === 1 ? '' : 's'}`)
      setTimeout(fetchSessions, 1000)
    } catch {
      toast.error('Failed to send stop signal')
    } finally {
      setStopping(false)
    }
  }

  // Stop ONE specific operation by session_id. Used by the per-session
  // detail-view "Stop" button so the operator can halt one op without
  // collateral. Falls back to the WS-channel {"action":"stop"} if the
  // REST endpoint is unreachable (older server build), since both paths
  // ultimately call operation_manager.request_stop(session_id).
  const handleStopOne = async (sessionId) => {
    if (!sessionId) return
    setStopping(true)
    try {
      await api.post(`/sessions/${sessionId}/stop`)
      toast.success('Stop signal sent')
      setTimeout(fetchSessions, 1000)
    } catch {
      // REST path failed — try the WS action channel.
      const handle = wsRef.current
      const ws = handle?.ws ?? handle
      if (ws && ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ action: 'stop' }))
          toast.success('Stop signal sent (via WebSocket)')
          setTimeout(fetchSessions, 1000)
        } catch {
          toast.error('Failed to send stop signal')
        }
      } else {
        toast.error('Failed to send stop signal')
      }
    } finally {
      setStopping(false)
    }
  }

  // Navigate to feature page to rerun
  const handleRerun = (sessionType) => {
    const route = typeRoutes[sessionType]
    if (route) {
      navigate(route)
    } else {
      toast.warning(`No page found for ${sessionType}`)
    }
  }

  // Connect to a session
  const connectToSession = useCallback((sessionId) => {
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
            ...(m.plan_yaml ? { detail: m.plan_yaml, detailLabel: 'Show plan.yaml' } : {}),
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
              ...(data.data.plan_yaml ? { detail: data.data.plan_yaml, detailLabel: 'Show plan.yaml' } : {}),
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
    const isLive = sessionMeta && sessionMeta.status !== 'disconnected' && !ended
    return (
      <div className="p-4 space-y-4">
        <div className="flex items-center gap-3">
          <button onClick={goBack} className="btn-secondary btn-sm">
            Back
          </button>
          <h2 className="heading-gold text-lg flex-1">
            {sessionMeta ? sessionMeta.label : `Session ${selected}`}
          </h2>
          {sessionMeta && (
            <>
              <span
                className={`text-xs px-2 py-0.5 rounded-full font-semibold ${
                  !isLive
                    ? 'bg-[var(--surface)] text-secondary'
                    : 'bg-[var(--success)]/20 text-[var(--success)]'
                }`}
              >
                {isLive ? 'Live' : 'Ended'}
              </span>
              {isLive && (
                <button
                  onClick={() => handleStopOne(selected)}
                  className="btn-danger btn-sm"
                  disabled={stopping}
                  title="Stop this operation only (does not affect other running operations)"
                >
                  {stopping ? 'Stopping...' : 'Stop'}
                </button>
              )}
              {!isLive && (
                <button
                  onClick={() => handleRerun(sessionMeta.session_type)}
                  className="btn-primary btn-sm"
                >
                  Rerun
                </button>
              )}
            </>
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
      <div className="flex justify-between items-center">
        <h1 className="heading-gold text-xl">Sessions</h1>
        {running.length > 0 && (
          <button
            onClick={handleStopAll}
            className="btn-danger btn-sm"
            disabled={stopping}
          >
            {stopping ? 'Stopping...' : `Stop All (${running.length})`}
          </button>
        )}
      </div>
      <p className="text-secondary text-sm">
        Control panel for all running and recent operations. Monitor logs, stop active tasks, or rerun completed ones.
      </p>

      {listError && (
        <FetchError
          what="Could not read the session list"
          detail={listError}
          onRetry={fetchSessions}
        />
      )}

      {loading ? (
        <div className="flex justify-center py-8">
          <div className="spinner spinner-md" />
        </div>
      ) : sessions.length === 0 ? (
        // Suppressed while `listError` is set: the banner above already says
        // what happened, and "nothing is running" is a claim about the machine
        // that the failed poll did not establish.
        listError ? null : (
          <div className="text-center py-12 text-secondary">
            <p className="text-lg mb-2">No active or recent sessions</p>
            <p className="text-sm">
              Start a task (Build Queue, Farm Loop, Auto Scout, Oasis Raider) on any device to see it here.
            </p>
          </div>
        )
      ) : (
        <>
          {running.length > 0 && (
            <div>
              <h2 className="text-sm font-semibold text-[var(--success)] mb-2">
                Running ({running.length})
              </h2>
              <div className="space-y-2">
                {running.map((s) => (
                  <SessionCard
                    key={s.id}
                    session={s}
                    onClick={() => connectToSession(s.id)}
                    onRerun={() => handleRerun(s.session_type)}
                  />
                ))}
              </div>
            </div>
          )}

          {disconnected.length > 0 && (
            <div>
              <h2 className="text-sm font-semibold text-secondary mb-2">
                Completed ({disconnected.length})
              </h2>
              <div className="space-y-2">
                {disconnected.map((s) => (
                  <SessionCard
                    key={s.id}
                    session={s}
                    onClick={() => connectToSession(s.id)}
                    onRerun={() => handleRerun(s.session_type)}
                  />
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function SessionCard({ session, onClick, onRerun }) {
  const isRunning = session.status === 'running'

  return (
    <div
      className="w-full text-left bg-surface border-default rounded-lg p-3 hover:border-[var(--primary)] transition-colors"
      style={{ border: '1px solid var(--color-border, #333)' }}
    >
      <div className="flex items-center gap-3">
        <span className="text-lg font-mono text-secondary" style={{ minWidth: 36, textAlign: 'center' }}>
          {typeIcons[session.session_type] || '[?]'}
        </span>
        <div
          className="flex-1 min-w-0 cursor-pointer link-action"
          role="button"
          tabIndex={0}
          onClick={onClick}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return
            e.preventDefault()
            onClick()
          }}
        >
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
        <div className="flex items-center gap-2">
          {!isRunning && (
            <button
              onClick={(e) => { e.stopPropagation(); onRerun() }}
              className="btn-secondary btn-xs"
              title="Open the feature page to run again"
            >
              Rerun
            </button>
          )}
          <button
            onClick={onClick}
            className="btn-secondary btn-xs"
            title="View session logs"
          >
            Logs
          </button>
        </div>
      </div>
    </div>
  )
}
