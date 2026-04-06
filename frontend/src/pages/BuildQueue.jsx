import { useState, useRef, useCallback, useEffect } from 'react'
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

function statusBadgeClass(status) {
  const map = {
    pending: 'status-badge-pending',
    done: 'status-badge-done',
    error: 'status-badge-error',
    skipped: 'status-badge-skipped',
  }
  return map[status] || 'status-badge-pending'
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
    <div className="yaml-editor">
      {/* Line numbers */}
      <div className="yaml-line-numbers">
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
        className="yaml-textarea"
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
    <div className="card">
      <div className="flex justify-between items-center mb-3">
        <h3 className="heading-gold text-base">
          Building Reference
        </h3>
        <button
          className="btn-secondary btn-xs"
          onClick={fetchBuildings}
          disabled={loading}
        >
          {loading ? 'Loading...' : 'Show Current Buildings'}
        </button>
      </div>

      {error && (
        <div className="error-box">
          {error}
        </div>
      )}

      {buildings && Array.isArray(buildings) && buildings.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Slot</th>
                <th>Building</th>
                <th className="text-right">Level</th>
              </tr>
            </thead>
            <tbody>
              {buildings.map((b, i) => (
                <tr key={b.slot_id ?? i}>
                  <td className="text-secondary font-mono">
                    {b.slot_id ?? b.id ?? i}
                  </td>
                  <td>
                    {b.name ?? b.building_name ?? '---'}
                  </td>
                  <td className="text-right text-gold font-mono">
                    {b.level ?? b.current_level ?? 0}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {buildings && Array.isArray(buildings) && buildings.length === 0 && (
        <div className="text-secondary text-sm italic">
          No buildings found.
        </div>
      )}
    </div>
  )
}

function ValidationResults({ data }) {
  if (!data) return null

  return (
    <div className="card">
      <h3 className="heading-gold text-base mb-3">
        Validation Results
      </h3>

      {data.messages && data.messages.length > 0 && (
        <div className="mb-3">
          {data.messages.map((msg, i) => (
            <div
              key={i}
              className="text-xs text-secondary py-0.5 font-mono"
            >
              {msg}
            </div>
          ))}
        </div>
      )}

      {data.items && data.items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Building</th>
                <th className="text-center">Slot</th>
                <th className="text-center">Current</th>
                <th className="text-center">Target</th>
                <th className="text-center">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item, i) => (
                <tr key={i}>
                  <td>
                    {item.building}
                    {item.is_construction && (
                      <span className="ml-1.5 text-xs text-warning">
                        (new)
                      </span>
                    )}
                  </td>
                  <td className="text-center font-mono text-secondary">
                    {item.slot_id ?? '---'}
                  </td>
                  <td className="text-center font-mono">
                    {item.current_level ?? '---'}
                  </td>
                  <td className="text-center font-mono text-gold">
                    {item.target}
                  </td>
                  <td className="text-center">
                    <span className={`status-badge ${statusBadgeClass(item.status)}`}>{item.status}</span>
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
  const timersRef = useRef([])

  // Cleanup WS + timers on unmount
  useEffect(() => {
    return () => {
      timersRef.current.forEach(({ type, id }) =>
        type === 'interval' ? clearInterval(id) : clearTimeout(id)
      )
      timersRef.current = []
      if (wsRef.current) {
        try { wsRef.current.close() } catch {}
        wsRef.current = null
      }
    }
  }, [])

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

    let msgId = Date.now()
    const addMessage = (type, text) => {
      setWsMessages((prev) => [
        ...prev,
        { id: ++msgId, type, text, timestamp: new Date().toISOString() },
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

    // Send config once connected, with tracked cleanup
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
    timersRef.current.push({ type: 'interval', id: waitForOpen })

    const safetyTimeout = setTimeout(() => clearInterval(waitForOpen), 10000)
    timersRef.current.push({ type: 'timeout', id: safetyTimeout })
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
    <div className="p-6 max-w-[960px] mx-auto">
      {/* Header */}
      <h2 className="heading-gold text-2xl mb-5">
        Build Queue
      </h2>

      <div className="flex flex-col gap-4">
        {/* YAML Editor Panel */}
        <div className="card">
          <div className="flex justify-between items-center mb-3 flex-wrap gap-2">
            <h3 className="heading-gold text-base">
              Build Plan (YAML)
            </h3>
            <div className="flex gap-1.5 flex-wrap">
              {Object.entries(TEMPLATES).map(([key, tpl]) => (
                <button
                  key={key}
                  className="btn-secondary btn-xs"
                  onClick={() => handleTemplateInsert(key)}
                >
                  {tpl.label}
                </button>
              ))}
            </div>
          </div>
          <YamlEditor value={yamlContent} onChange={handleYamlChange} />
          {activeVillageId && (
            <div className="mt-2 text-xs text-secondary">
              Active village ID: {activeVillageId} (used when village_id is "auto")
            </div>
          )}
        </div>

        {/* Building Reference Panel */}
        <BuildingReferencePanel />

        {/* Execution Options */}
        <div className="card">
          <h3 className="heading-gold text-base mb-3">
            Execution Options
          </h3>
          <div className="flex gap-6 items-center flex-wrap">
            {/* Poll Interval */}
            <label className="check-label-secondary gap-2">
              Poll interval (s):
              <input
                type="number"
                min={5}
                max={600}
                value={pollInterval}
                onChange={(e) => setPollInterval(Number(e.target.value) || 30)}
                className="input-field w-20 text-center"
              />
            </label>

            {/* Use Video Rewards */}
            <label className="check-label-secondary">
              <input
                type="checkbox"
                checked={useVideo}
                onChange={(e) => setUseVideo(e.target.checked)}
                className="checkbox-gold"
              />
              Use video rewards
            </label>

            {/* Verbose Mode */}
            <label className="check-label-secondary">
              <input
                type="checkbox"
                checked={verbose}
                onChange={(e) => setVerbose(e.target.checked)}
                className="checkbox-gold"
              />
              Verbose mode
            </label>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex gap-3 flex-wrap">
          <button
            className="btn-primary min-w-[130px]"
            onClick={handleValidate}
            disabled={validating || !yamlContent.trim() || running}
          >
            {validating ? 'Validating...' : 'Validate Plan'}
          </button>
          <button
            className={`btn-primary min-w-[130px] ${validated && !running ? 'bg-success' : ''}`}
            onClick={handleExecute}
            disabled={!validated || running}
          >
            Execute Plan
          </button>
          {running && (
            <button
              className="btn-danger min-w-[100px]"
              onClick={handleStop}
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
            <h3 className="heading-gold text-base mb-2">
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
