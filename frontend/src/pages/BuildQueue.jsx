import { useState, useRef, useCallback } from 'react'
import api from '../api'
import { createWebSocket } from '../ws'
import WebSocketPanel from '../components/WebSocketPanel'
import { useToast } from '../components/Toast'
import ConfirmDialog from '../components/ConfirmDialog'
import useGameStore from '../stores/gameStore'

const TEMPLATES = {
  resource: {
    label: 'Resource Focus',
    yaml: `village_id: auto
plan:
  - building: Woodcutter
    target: 5
    priority: 1
  - building: Clay Pit
    target: 5
    priority: 1
  - building: Iron Mine
    target: 5
    priority: 1
  - building: Cropland
    target: 5
    priority: 1`,
  },
  military: {
    label: 'Military Focus',
    yaml: `village_id: auto
plan:
  - building: Barracks
    target: 10
    priority: 1
  - building: Academy
    target: 10
    priority: 2
  - building: Smithy
    target: 10
    priority: 2`,
  },
  economy: {
    label: 'Economy Starter',
    yaml: `village_id: auto
plan:
  - building: Main Building
    target: 10
    priority: 1
  - building: Warehouse
    target: 10
    priority: 1
  - building: Granary
    target: 10
    priority: 1
  - building: Marketplace
    target: 5
    priority: 2`,
  },
}

const statusBadgeStyle = (status) => {
  const colors = {
    pending: { bg: 'rgba(196, 129, 47, 0.15)', color: 'var(--warning)' },
    done: { bg: 'rgba(74, 140, 74, 0.15)', color: 'var(--success)' },
    error: { bg: 'rgba(179, 64, 64, 0.15)', color: 'var(--danger)' },
    skipped: { bg: 'rgba(74, 124, 140, 0.15)', color: 'var(--info)' },
  }
  const c = colors[status] || colors.pending
  return {
    display: 'inline-block',
    padding: '0.15rem 0.5rem',
    borderRadius: '0.25rem',
    fontSize: '0.75rem',
    fontWeight: 600,
    backgroundColor: c.bg,
    color: c.color,
  }
}

function YamlEditor({ value, onChange }) {
  const textareaRef = useRef(null)
  const lineCount = value.split('\n').length

  const handleKeyDown = (e) => {
    if (e.key === 'Tab') {
      e.preventDefault()
      const ta = textareaRef.current
      const start = ta.selectionStart
      const end = ta.selectionEnd
      const newValue = value.substring(0, start) + '  ' + value.substring(end)
      onChange(newValue)
      requestAnimationFrame(() => {
        ta.selectionStart = ta.selectionEnd = start + 2
      })
    }
  }

  return (
    <div
      style={{
        display: 'flex',
        border: '1px solid var(--border)',
        borderRadius: '0.375rem',
        overflow: 'hidden',
        backgroundColor: '#0d0b09',
        fontFamily: "'Courier New', Consolas, monospace",
        fontSize: '0.85rem',
        lineHeight: '1.6',
      }}
    >
      {/* Line numbers */}
      <div
        style={{
          padding: '0.75rem 0.5rem',
          textAlign: 'right',
          color: 'var(--text-secondary)',
          backgroundColor: '#161310',
          userSelect: 'none',
          minWidth: '2.5rem',
          borderRight: '1px solid var(--border)',
        }}
      >
        {Array.from({ length: lineCount }, (_, i) => (
          <div key={i}>{i + 1}</div>
        ))}
      </div>
      {/* Text area */}
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        spellCheck={false}
        style={{
          flex: 1,
          padding: '0.75rem',
          background: 'transparent',
          color: 'var(--text-primary)',
          border: 'none',
          outline: 'none',
          resize: 'vertical',
          minHeight: '280px',
          fontFamily: 'inherit',
          fontSize: 'inherit',
          lineHeight: 'inherit',
          tabSize: 2,
          whiteSpace: 'pre',
          overflowWrap: 'normal',
          overflowX: 'auto',
        }}
      />
    </div>
  )
}

function BuildingReferencePanel() {
  const [buildings, setBuildings] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const fetchBuildings = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.get('/buildings')
      setBuildings(res.data)
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to fetch buildings')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="card" style={{ padding: '1rem' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '0.75rem',
        }}
      >
        <h3
          style={{
            fontFamily: 'Cinzel, serif',
            fontSize: '1rem',
            color: 'var(--accent-gold)',
            margin: 0,
          }}
        >
          Building Reference
        </h3>
        <button
          className="btn-secondary"
          onClick={fetchBuildings}
          disabled={loading}
          style={{ fontSize: '0.8rem', padding: '0.3rem 0.75rem' }}
        >
          {loading ? 'Loading...' : 'Show Current Buildings'}
        </button>
      </div>

      {error && (
        <div
          style={{
            color: 'var(--danger)',
            fontSize: '0.85rem',
            padding: '0.5rem',
            backgroundColor: 'rgba(179, 64, 64, 0.1)',
            borderRadius: '0.25rem',
          }}
        >
          {error}
        </div>
      )}

      {buildings && Array.isArray(buildings) && buildings.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table
            style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontSize: '0.8rem',
            }}
          >
            <thead>
              <tr
                style={{
                  borderBottom: '1px solid var(--border)',
                  color: 'var(--text-secondary)',
                  textAlign: 'left',
                }}
              >
                <th style={{ padding: '0.4rem 0.5rem' }}>Slot</th>
                <th style={{ padding: '0.4rem 0.5rem' }}>Building</th>
                <th style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>Level</th>
              </tr>
            </thead>
            <tbody>
              {buildings.map((b, i) => (
                <tr
                  key={b.slot_id ?? i}
                  style={{
                    borderBottom: '1px solid var(--border)',
                  }}
                >
                  <td
                    style={{
                      padding: '0.35rem 0.5rem',
                      color: 'var(--text-secondary)',
                      fontFamily: 'monospace',
                    }}
                  >
                    {b.slot_id ?? b.id ?? i}
                  </td>
                  <td style={{ padding: '0.35rem 0.5rem' }}>
                    {b.name ?? b.building_name ?? '---'}
                  </td>
                  <td
                    style={{
                      padding: '0.35rem 0.5rem',
                      textAlign: 'right',
                      color: 'var(--accent-gold)',
                      fontFamily: 'monospace',
                    }}
                  >
                    {b.level ?? b.current_level ?? 0}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {buildings && Array.isArray(buildings) && buildings.length === 0 && (
        <div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', fontStyle: 'italic' }}>
          No buildings found.
        </div>
      )}
    </div>
  )
}

function ValidationResults({ data }) {
  if (!data) return null

  return (
    <div className="card" style={{ padding: '1rem' }}>
      <h3
        style={{
          fontFamily: 'Cinzel, serif',
          fontSize: '1rem',
          color: 'var(--accent-gold)',
          marginBottom: '0.75rem',
        }}
      >
        Validation Results
      </h3>

      {data.messages && data.messages.length > 0 && (
        <div style={{ marginBottom: '0.75rem' }}>
          {data.messages.map((msg, i) => (
            <div
              key={i}
              style={{
                fontSize: '0.8rem',
                color: 'var(--text-secondary)',
                padding: '0.2rem 0',
                fontFamily: 'monospace',
              }}
            >
              {msg}
            </div>
          ))}
        </div>
      )}

      {data.items && data.items.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table
            style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontSize: '0.8rem',
            }}
          >
            <thead>
              <tr
                style={{
                  borderBottom: '1px solid var(--border)',
                  color: 'var(--text-secondary)',
                  textAlign: 'left',
                }}
              >
                <th style={{ padding: '0.4rem 0.5rem' }}>Building</th>
                <th style={{ padding: '0.4rem 0.5rem', textAlign: 'center' }}>Slot</th>
                <th style={{ padding: '0.4rem 0.5rem', textAlign: 'center' }}>Current</th>
                <th style={{ padding: '0.4rem 0.5rem', textAlign: 'center' }}>Target</th>
                <th style={{ padding: '0.4rem 0.5rem', textAlign: 'center' }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item, i) => (
                <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '0.35rem 0.5rem' }}>
                    {item.building}
                    {item.is_construction && (
                      <span
                        style={{
                          marginLeft: '0.4rem',
                          fontSize: '0.7rem',
                          color: 'var(--warning)',
                        }}
                      >
                        (new)
                      </span>
                    )}
                  </td>
                  <td
                    style={{
                      padding: '0.35rem 0.5rem',
                      textAlign: 'center',
                      fontFamily: 'monospace',
                      color: 'var(--text-secondary)',
                    }}
                  >
                    {item.slot_id ?? '---'}
                  </td>
                  <td
                    style={{
                      padding: '0.35rem 0.5rem',
                      textAlign: 'center',
                      fontFamily: 'monospace',
                    }}
                  >
                    {item.current_level ?? '---'}
                  </td>
                  <td
                    style={{
                      padding: '0.35rem 0.5rem',
                      textAlign: 'center',
                      fontFamily: 'monospace',
                      color: 'var(--accent-gold)',
                    }}
                  >
                    {item.target}
                  </td>
                  <td style={{ padding: '0.35rem 0.5rem', textAlign: 'center' }}>
                    <span style={statusBadgeStyle(item.status)}>{item.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function BuildQueue() {
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const toast = useToast()

  // YAML editor state
  const [yamlContent, setYamlContent] = useState(TEMPLATES.resource.yaml)

  // Validation state
  const [validationResult, setValidationResult] = useState(null)
  const [validating, setValidating] = useState(false)
  const [validated, setValidated] = useState(false)

  // Execution options
  const [pollInterval, setPollInterval] = useState(30)
  const [useVideo, setUseVideo] = useState(false)
  const [verbose, setVerbose] = useState(false)

  // Execution state
  const [wsMessages, setWsMessages] = useState([])
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [running, setRunning] = useState(false)
  const wsRef = useRef(null)

  // Confirm dialog
  const [showConfirm, setShowConfirm] = useState(false)

  const handleTemplateInsert = (key) => {
    setYamlContent(TEMPLATES[key].yaml)
    setValidated(false)
    setValidationResult(null)
  }

  const handleYamlChange = (val) => {
    setYamlContent(val)
    setValidated(false)
    setValidationResult(null)
  }

  const handleValidate = async () => {
    setValidating(true)
    setValidationResult(null)
    setValidated(false)
    try {
      const res = await api.post('/queue/validate', { yaml_content: yamlContent })
      setValidationResult(res.data)
      setValidated(true)
      toast.success('Plan validated successfully')
    } catch (err) {
      const detail = err.response?.data?.detail || err.response?.data?.message || 'Validation failed'
      setValidationResult({ items: [], messages: [typeof detail === 'string' ? detail : JSON.stringify(detail)] })
      toast.error('Validation failed')
    } finally {
      setValidating(false)
    }
  }

  const handleExecute = () => {
    setShowConfirm(true)
  }

  const startExecution = useCallback(() => {
    setShowConfirm(false)
    setWsMessages([])
    setWsStatus('connected')
    setRunning(true)

    const addMessage = (type, text) => {
      setWsMessages((prev) => [
        ...prev,
        { type, text, timestamp: new Date().toISOString() },
      ])
    }

    addMessage('info', 'Connecting to build queue executor...')

    const ws = createWebSocket(
      '/ws/queue/run',
      // onMessage
      (data) => {
        if (data.type === 'status') {
          addMessage('info', data.message)
        } else if (data.type === 'step_complete') {
          const successText = data.success ? 'Success' : 'Failed'
          const msgType = data.success ? 'success' : 'error'
          addMessage(msgType, `${data.building} -> Level ${data.level}: ${successText}`)
        } else if (data.type === 'complete') {
          addMessage('success', 'Build queue execution completed!')
          setRunning(false)
          setWsStatus('disconnected')
        } else if (data.type === 'error') {
          addMessage('error', data.message)
        } else if (typeof data === 'string') {
          addMessage('info', data)
        } else if (data.message) {
          addMessage('info', data.message)
        }
      },
      // onError
      () => {
        addMessage('error', 'WebSocket connection error')
        setRunning(false)
        setWsStatus('disconnected')
      },
      // onClose
      () => {
        setRunning(false)
        setWsStatus('disconnected')
      }
    )

    wsRef.current = ws

    // Send config once connected
    const waitForOpen = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        clearInterval(waitForOpen)
        setWsStatus('running')
        ws.send(
          JSON.stringify({
            yaml_content: yamlContent,
            poll_interval: pollInterval,
            use_video: useVideo,
            verbose,
          })
        )
        addMessage('info', 'Configuration sent. Execution starting...')
      } else if (ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        clearInterval(waitForOpen)
      }
    }, 100)

    // Safety cleanup for the interval
    setTimeout(() => clearInterval(waitForOpen), 10000)
  }, [yamlContent, pollInterval, useVideo, verbose])

  const handleStop = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'stop' }))
      toast.warning('Stop signal sent')
    }
  }

  const handleClearLog = () => {
    setWsMessages([])
  }

  return (
    <div style={{ padding: '1.5rem', maxWidth: '960px', margin: '0 auto' }}>
      {/* Header */}
      <h2
        style={{
          fontFamily: 'Cinzel, serif',
          fontSize: '1.5rem',
          marginBottom: '1.25rem',
        }}
      >
        Build Queue
      </h2>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
        {/* YAML Editor Panel */}
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
                fontSize: '1rem',
                color: 'var(--accent-gold)',
                margin: 0,
              }}
            >
              Build Plan (YAML)
            </h3>
            <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
              {Object.entries(TEMPLATES).map(([key, tpl]) => (
                <button
                  key={key}
                  className="btn-secondary"
                  onClick={() => handleTemplateInsert(key)}
                  style={{ fontSize: '0.75rem', padding: '0.25rem 0.6rem' }}
                >
                  {tpl.label}
                </button>
              ))}
            </div>
          </div>
          <YamlEditor value={yamlContent} onChange={handleYamlChange} />
          {activeVillageId && (
            <div
              style={{
                marginTop: '0.5rem',
                fontSize: '0.75rem',
                color: 'var(--text-secondary)',
              }}
            >
              Active village ID: {activeVillageId} (used when village_id is "auto")
            </div>
          )}
        </div>

        {/* Building Reference Panel */}
        <BuildingReferencePanel />

        {/* Execution Options */}
        <div className="card" style={{ padding: '1rem' }}>
          <h3
            style={{
              fontFamily: 'Cinzel, serif',
              fontSize: '1rem',
              color: 'var(--accent-gold)',
              marginBottom: '0.75rem',
            }}
          >
            Execution Options
          </h3>
          <div
            style={{
              display: 'flex',
              gap: '1.5rem',
              alignItems: 'center',
              flexWrap: 'wrap',
            }}
          >
            {/* Poll Interval */}
            <label
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.5rem',
                fontSize: '0.85rem',
                color: 'var(--text-secondary)',
              }}
            >
              Poll interval (s):
              <input
                type="number"
                min={5}
                max={600}
                value={pollInterval}
                onChange={(e) => setPollInterval(Number(e.target.value) || 30)}
                className="input-field"
                style={{ width: '5rem', textAlign: 'center' }}
              />
            </label>

            {/* Use Video Rewards */}
            <label
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.4rem',
                fontSize: '0.85rem',
                color: 'var(--text-secondary)',
                cursor: 'pointer',
              }}
            >
              <input
                type="checkbox"
                checked={useVideo}
                onChange={(e) => setUseVideo(e.target.checked)}
                style={{ accentColor: 'var(--accent-gold)' }}
              />
              Use video rewards
            </label>

            {/* Verbose Mode */}
            <label
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.4rem',
                fontSize: '0.85rem',
                color: 'var(--text-secondary)',
                cursor: 'pointer',
              }}
            >
              <input
                type="checkbox"
                checked={verbose}
                onChange={(e) => setVerbose(e.target.checked)}
                style={{ accentColor: 'var(--accent-gold)' }}
              />
              Verbose mode
            </label>
          </div>
        </div>

        {/* Action Buttons */}
        <div
          style={{
            display: 'flex',
            gap: '0.75rem',
            flexWrap: 'wrap',
          }}
        >
          <button
            className="btn-primary"
            onClick={handleValidate}
            disabled={validating || !yamlContent.trim() || running}
            style={{ minWidth: '130px' }}
          >
            {validating ? 'Validating...' : 'Validate Plan'}
          </button>
          <button
            className="btn-primary"
            onClick={handleExecute}
            disabled={!validated || running}
            style={{
              minWidth: '130px',
              backgroundColor: validated && !running ? 'var(--success)' : undefined,
            }}
          >
            Execute Plan
          </button>
          {running && (
            <button
              className="btn-danger"
              onClick={handleStop}
              style={{ minWidth: '100px' }}
            >
              Stop
            </button>
          )}
        </div>

        {/* Validation Results */}
        <ValidationResults data={validationResult} />

        {/* Live Execution Panel */}
        {(wsMessages.length > 0 || running) && (
          <div>
            <h3
              style={{
                fontFamily: 'Cinzel, serif',
                fontSize: '1rem',
                color: 'var(--accent-gold)',
                marginBottom: '0.5rem',
              }}
            >
              Execution Log
            </h3>
            <WebSocketPanel
              messages={wsMessages}
              status={wsStatus}
              onClear={handleClearLog}
            />
          </div>
        )}
      </div>

      {/* Confirmation Dialog */}
      <ConfirmDialog
        open={showConfirm}
        title="Execute Build Plan"
        message="This will start building according to your plan. The process will run in the background. Continue?"
        confirmText="Execute"
        cancelText="Cancel"
        onConfirm={startExecution}
        onCancel={() => setShowConfirm(false)}
      />
    </div>
  )
}
