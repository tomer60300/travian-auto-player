import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import api from '../api'
import { useResumableOperation } from '../hooks/useResumableOperation'
import WebSocketPanel from '../components/WebSocketPanel'
import { useToast } from '../components/Toast'
import ConfirmDialog from '../components/ConfirmDialog'
import useGameStore from '../stores/gameStore'

// ── Helpers ──────────────────────────────────────────────────────────
function formatTimeRemaining(seconds) {
  if (seconds == null || seconds <= 0) return '00:00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  return [h, m, s].map((v) => String(v).padStart(2, '0')).join(':')
}

function getBuildingCategory(slotId, b) {
  if (!b || !b.name || b.name === 'Empty' || (b.level === 0 && slotId > 18)) return 'empty'
  if (slotId >= 1 && slotId <= 18) return 'resource'
  const military = ['Barracks','Stable','Workshop','Academy','Smithy','Rally Point','Wall','Earth Wall','Palisade','City Wall','Great Barracks','Great Stable','Horse Drinking Trough','Tournament Square','Trapper']
  if (military.some((m) => (b.name || '').toLowerCase().includes(m.toLowerCase()))) return 'military'
  return 'infrastructure'
}

const CAT_CLASS = { resource: 'btype-resource', military: 'btype-military', infrastructure: 'btype-infra', empty: 'btype-empty' }
const CAT_LABEL = { resource: 'Resources', military: 'Military', infrastructure: 'Infrastructure', empty: 'Empty Slots' }

let _queueId = 0
function nextId() { return ++_queueId }

function queueToYaml(items, villageId) {
  let yaml = `village_id: ${villageId || 0}\nplan:\n`
  for (const item of items) {
    yaml += `  - building: "${item.name}"\n`
    yaml += `    target: ${item.targetLevel}\n`
    yaml += `    priority: ${item.priority}\n`
    if (item.slotId) yaml += `    slot: ${item.slotId}\n`
  }
  return yaml
}

// ── Construction Queue (in-progress builds) ──────────────────────────
function ConstructionQueue({ queue }) {
  const [snapTime, setSnapTime] = useState(Date.now)
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!queue || queue.length === 0) return
    setSnapTime(Date.now()); setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [queue])

  if (!queue || queue.length === 0) return null
  const elapsed = Math.floor((now - snapTime) / 1000)

  return (
    <div className="mb-4 p-3 bg-surface rounded-lg border-default">
      <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">In Progress</h4>
      <div className="flex flex-col gap-1.5">
        {queue.map((item, idx) => {
          const baseRemaining = item.remaining_seconds ?? item.time_remaining ?? 0
          const remaining = Math.max(0, (baseRemaining || 0) - elapsed)
          const doneAt = new Date(Date.now() + remaining * 1000)
          return (
            <div key={item.event_id ?? idx} className="flex justify-between items-center text-sm">
              <span className="text-primary">{item.building_name || 'Building'} &rarr; Lv {item.target_level ?? '?'}</span>
              <span className="text-warning font-mono text-xs">{formatTimeRemaining(remaining)} <span className="text-secondary">done {doneAt.toLocaleTimeString('en-US', { hour12: false })}</span></span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Building List (left panel) ───────────────────────────────────────
function BuildingList({ buildings, onAdd, queueItems }) {
  // Count how many times each slot is already in queue
  const queueCountBySlot = useMemo(() => {
    const counts = {}
    for (const item of (queueItems || [])) {
      counts[item.slotId] = (counts[item.slotId] || 0) + 1
    }
    return counts
  }, [queueItems])

  const grouped = useMemo(() => {
    const groups = { resource: [], military: [], infrastructure: [], empty: [] }
    for (const b of buildings) {
      const sid = b.slot_id ?? b.id
      const cat = getBuildingCategory(sid, b)
      groups[cat].push({ ...b, slotId: sid, category: cat })
    }
    return groups
  }, [buildings])

  return (
    <div className="flex flex-col gap-3">
      {['resource', 'military', 'infrastructure', 'empty'].map((cat) => {
        const items = grouped[cat]
        if (items.length === 0) return null
        return (
          <div key={cat}>
            <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-1.5">{CAT_LABEL[cat]}</h4>
            <div className="flex flex-col gap-0.5">
              {items.map((b) => {
                const isEmpty = cat === 'empty'
                const inQueueCount = queueCountBySlot[b.slotId] || 0
                return (
                  <div
                    key={b.slotId}
                    className={`flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer hover:bg-surface transition-colors ${CAT_CLASS[cat] || ''}`}
                    onClick={() => !isEmpty && onAdd(b)}
                    title={isEmpty ? 'Empty slot' : `Click to add ${b.name} to queue`}
                    style={{ opacity: isEmpty ? 0.5 : 1 }}
                  >
                    <span className="text-xs text-secondary min-w-6 text-right">#{b.slotId}</span>
                    <span className={`flex-1 text-sm ${isEmpty ? 'italic text-secondary' : 'text-primary'}`}>
                      {isEmpty ? 'Empty' : b.name}
                    </span>
                    {inQueueCount > 0 && (
                      <span className="text-xs bg-gold text-base rounded-full px-1.5 py-0.5 font-bold leading-none" title={`In queue ${inQueueCount}x`}>
                        {inQueueCount}
                      </span>
                    )}
                    <span className="text-xs text-secondary min-w-10 text-right">
                      {isEmpty ? '' : `Lv ${b.level ?? 0}`}
                    </span>
                    {!isEmpty && (
                      <button
                        className="text-gold hover:text-primary text-lg leading-none px-1"
                        onClick={(e) => { e.stopPropagation(); onAdd(b) }}
                        title="Add to queue"
                      >+</button>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ── Queue Item ───────────────────────────────────────────────────────
function QueueItem({ item, onRemove, onChange, onMoveUp, onMoveDown, isFirst, isLast, selected, onToggleSelect }) {
  return (
    <div className={`bg-surface rounded-md border-default group px-2.5 py-1.5${selected ? ' ring-1 ring-gold/50' : ''}`}>
      {/* Row 1: checkbox + name + remove */}
      <div className="flex items-center justify-between gap-1 mb-1">
        <span className="text-sm text-primary font-medium flex items-center gap-1.5">
          {onToggleSelect && (
            <input type="checkbox" checked={selected} onChange={onToggleSelect} className="checkbox-gold" />
          )}
          <span className="text-gold font-mono">#{item.slotId}</span>
          {item.name || '???'}
        </span>
        <button
          className="text-secondary hover:text-danger text-base leading-none px-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
          onClick={onRemove}
          title="Remove"
        >&times;</button>
      </div>
      {/* Row 2: level, target, priority, reorder */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-secondary whitespace-nowrap">
          Lv <span className="text-primary font-semibold">{item.currentLevel}</span>
        </span>
        <span className="text-secondary text-xs">&rarr;</span>
        <input
          type="number"
          min={item.currentLevel + 1}
          max={30}
          value={item.targetLevel}
          onChange={(e) => onChange({ targetLevel: Math.max(item.currentLevel + 1, Number(e.target.value) || item.currentLevel + 1) })}
          className="input-field w-20 text-center text-xs py-0.5 px-1"
          title="Target level"
        />
        <select
          value={item.priority}
          onChange={(e) => onChange({ priority: Number(e.target.value) })}
          className="input-field w-auto text-center text-xs py-0.5 px-0.5"
          title="Priority (1=highest)"
        >
          {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((p) => (
            <option key={p} value={p}>P{p}</option>
          ))}
        </select>
        <div className="flex gap-0.5 ml-auto">
          <button className="text-xs text-secondary hover:text-primary disabled:opacity-30 px-0.5" disabled={isFirst} onClick={onMoveUp} title="Move up">&uarr;</button>
          <button className="text-xs text-secondary hover:text-primary disabled:opacity-30 px-0.5" disabled={isLast} onClick={onMoveDown} title="Move down">&darr;</button>
        </div>
      </div>
    </div>
  )
}

// ── Queue Panel (right side) ─────────────────────────────────────────
function QueuePanel({ items, setItems }) {
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [bulkPriority, setBulkPriority] = useState(1)

  const toggleSelect = (id) => setSelectedIds((prev) => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const selectNone = () => setSelectedIds(new Set())
  const selectGroupIds = (groupItems, allSelected) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      for (const item of groupItems) {
        allSelected ? next.delete(item.id) : next.add(item.id)
      }
      return next
    })
  }
  const applyBulkPriority = () => {
    setItems((prev) => prev.map((i) => selectedIds.has(i.id) ? { ...i, priority: bulkPriority } : i))
    setSelectedIds(new Set())
  }
  const removeSelected = () => {
    setItems((prev) => prev.filter((i) => !selectedIds.has(i.id)))
    setSelectedIds(new Set())
  }

  const handleRemove = (id) => {
    setItems((prev) => prev.filter((i) => i.id !== id))
    setSelectedIds((prev) => { const next = new Set(prev); next.delete(id); return next })
  }
  const handleChange = (id, changes) => setItems((prev) => prev.map((i) => i.id === id ? { ...i, ...changes } : i))
  const handleMoveUp = (idx) => {
    if (idx <= 0) return
    setItems((prev) => {
      const next = [...prev]
      ;[next[idx - 1], next[idx]] = [next[idx], next[idx - 1]]
      return next
    })
  }
  const handleMoveDown = (idx) => {
    setItems((prev) => {
      if (idx >= prev.length - 1) return prev
      const next = [...prev]
      ;[next[idx], next[idx + 1]] = [next[idx + 1], next[idx]]
      return next
    })
  }

  // Group by priority for display
  const grouped = useMemo(() => {
    const groups = {}
    items.forEach((item, idx) => {
      const p = item.priority
      if (!groups[p]) groups[p] = []
      groups[p].push({ ...item, _idx: idx })
    })
    return groups
  }, [items])

  const priorities = Object.keys(grouped).sort((a, b) => a - b)

  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-secondary">
        <div className="text-3xl mb-2 opacity-50">&larr;</div>
        <p className="text-sm">Click a building to add it to the queue</p>
        <p className="text-xs mt-1 opacity-70">Set target levels and priorities, then execute</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Bulk action bar */}
      {selectedIds.size > 0 && (
        <div className="flex items-center gap-2 p-2 bg-surface rounded-lg border-default sticky top-0 z-10">
          <span className="text-xs text-secondary font-medium">{selectedIds.size} selected</span>
          <select
            value={bulkPriority}
            onChange={(e) => setBulkPriority(Number(e.target.value))}
            className="input-field text-xs w-auto py-0.5 px-1"
          >
            {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((p) => (
              <option key={p} value={p}>P{p}</option>
            ))}
          </select>
          <button className="btn-primary btn-xs" onClick={applyBulkPriority}>Set Priority</button>
          <button className="btn-danger btn-xs" onClick={removeSelected}>Remove</button>
          <button className="btn-secondary btn-xs ml-auto" onClick={selectNone}>Deselect</button>
        </div>
      )}

      {priorities.map((p) => {
        const groupItems = grouped[p]
        const allSelected = groupItems.every((item) => selectedIds.has(item.id))
        return (
          <div key={p}>
            <div className="flex items-center gap-1.5 mb-1.5">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={() => selectGroupIds(groupItems, allSelected)}
                className="checkbox-gold"
                title={`Select all Priority ${p}`}
              />
              <h4 className="text-xs font-semibold text-gold uppercase tracking-wider">Priority {p}</h4>
              <span className="text-xs text-secondary">({groupItems.length})</span>
            </div>
            <div className="flex flex-col gap-1">
              {groupItems.map((item) => (
                <QueueItem
                  key={item.id}
                  item={item}
                  onRemove={() => handleRemove(item.id)}
                  onChange={(changes) => handleChange(item.id, changes)}
                  onMoveUp={() => handleMoveUp(item._idx)}
                  onMoveDown={() => handleMoveDown(item._idx)}
                  isFirst={item._idx === 0}
                  isLast={item._idx === items.length - 1}
                  selected={selectedIds.has(item.id)}
                  onToggleSelect={() => toggleSelect(item.id)}
                />
              ))}
            </div>
          </div>
        )
      })}
      <div className="flex justify-end pt-1">
        <button className="btn-secondary btn-xs" onClick={() => { setItems([]); setSelectedIds(new Set()) }}>Clear All</button>
      </div>
    </div>
  )
}

// ── Quick Templates ──────────────────────────────────────────────────
function TemplateButtons({ buildings, onApply }) {
  const templates = [
    {
      label: 'All Resources to Lv 5',
      build: () => buildings.filter((b) => (b.slot_id ?? b.id) <= 18 && b.name && b.name !== 'Empty' && (b.level ?? 0) < 5)
        .map((b) => ({ name: b.name, slotId: b.slot_id ?? b.id, currentLevel: b.level ?? 0, targetLevel: 5, priority: 1, id: nextId() }))
    },
    {
      label: 'All Resources to Lv 10',
      build: () => buildings.filter((b) => (b.slot_id ?? b.id) <= 18 && b.name && b.name !== 'Empty' && (b.level ?? 0) < 10)
        .map((b) => ({ name: b.name, slotId: b.slot_id ?? b.id, currentLevel: b.level ?? 0, targetLevel: 10, priority: 1, id: nextId() }))
    },
  ]

  return (
    <div className="flex gap-1.5 flex-wrap">
      {templates.map((t) => (
        <button key={t.label} className="btn-secondary btn-xs" onClick={() => { const items = t.build(); if (items.length > 0) onApply(items) }}>
          {t.label}
        </button>
      ))}
    </div>
  )
}

// ── YAML Preview (collapsible) ───────────────────────────────────────
function YamlPreview({ yaml }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button className="text-xs text-secondary hover:text-primary underline" onClick={() => setOpen(!open)}>
        {open ? 'Hide' : 'Show'} generated YAML
      </button>
      {open && (
        <pre className="mt-2 p-3 bg-surface rounded-md border-default text-xs text-secondary font-mono overflow-x-auto whitespace-pre max-h-60 overflow-y-auto">
          {yaml}
        </pre>
      )}
    </div>
  )
}

// ── Main Page ────────────────────────────────────────────────────────
export default function BuildQueue() {
  const villages = useGameStore((s) => s.villages)
  const globalActiveVillageId = useGameStore((s) => s.activeVillageId)
  const toast = useToast()

  // Local village selection — independent of the global active village.
  // This lets two tabs target different villages without interfering.
  const [localVillageId, setLocalVillageId] = useState(null)
  const villageId = localVillageId || globalActiveVillageId

  // Reset the tab-local selection whenever the connected ACCOUNT changes, not
  // just when the id is absent: a different world can hold the same numeric
  // village id, so keeping it would silently target a colliding village.
  const serverUrl = useGameStore((s) => s.serverUrl)
  const playerName = useGameStore((s) => s.playerName)
  const accountKey = serverUrl && playerName ? `${serverUrl}|${playerName}` : null
  const accountKeyRef = useRef(accountKey)
  useEffect(() => {
    if (accountKeyRef.current !== accountKey) {
      accountKeyRef.current = accountKey
      setLocalVillageId(null)
    } else if (localVillageId && !villages.some((v) => v.id === localVillageId)) {
      setLocalVillageId(null)
    }
  }, [accountKey, villages, localVillageId])

  // Local building/queue state (fetched per-village, not from global store)
  const [buildings, setBuildings] = useState([])
  const [buildingsLoading, setBuildingsLoading] = useState(false)
  const [constructionQueue, setConstructionQueue] = useState([])

  // Queue items
  const [queueItems, setQueueItems] = useState([])

  // Execution options
  const [pollInterval, setPollInterval] = useState(30)
  const [useVideo, setUseVideo] = useState(true)
  const [verbose, setVerbose] = useState(false)

  // Execution state
  const [wsMessages, setWsMessages] = useState([])
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [running, setRunning] = useState(false)
  const timersRef = useRef([])
  const mountedRef = useRef(true)

  // Validation
  const [validationResult, setValidationResult] = useState(null)
  const [showConfirm, setShowConfirm] = useState(false)

  // Only the latest fetch may commit: a slower response from the previous
  // village/account must not overwrite the current one after a switch.
  const fetchTokenRef = useRef('')

  // Fetch buildings + queue for the selected village (no global switch)
  const fetchLocalData = useCallback(async (vid) => {
    if (!vid) return
    const token = `${vid}::${accountKeyRef.current}`
    fetchTokenRef.current = token
    setBuildingsLoading(true)
    try {
      const [bRes, qRes] = await Promise.all([
        api.get(`/buildings?village_id=${vid}`),
        api.get(`/buildings/queue?village_id=${vid}`),
      ])
      if (fetchTokenRef.current !== token) return
      const arr = Array.isArray(bRes.data) ? bRes.data
        : Array.isArray(bRes.data?.buildings) ? bRes.data.buildings : []
      setBuildings(arr)
      const qarr = Array.isArray(qRes.data) ? qRes.data
        : Array.isArray(qRes.data?.queue) ? qRes.data.queue : []
      setConstructionQueue(qarr)
    } catch (e) {
      console.warn('Failed to fetch village data:', e)
    } finally {
      setBuildingsLoading(false)
    }
  }, [])

  // Re-fetch on account change too: a different world can share the numeric
  // village id, so villageId alone would leave the old account's buildings
  // and queue on screen.
  useEffect(() => { fetchLocalData(villageId) }, [villageId, accountKey, fetchLocalData])

  const handleVillageSwitch = (id) => {
    setLocalVillageId(id)
    setQueueItems([])     // Clear queue when switching village
    setValidationResult(null)
  }

  useEffect(() => {
    return () => {
      mountedRef.current = false
      timersRef.current.forEach(({ type, id }) => type === 'interval' ? clearInterval(id) : clearTimeout(id))
      timersRef.current = []
    }
  }, [])

  const buildingList = Array.isArray(buildings) ? buildings : []

  // Add building to queue
  const handleAddBuilding = useCallback((b) => {
    const slotId = b.slotId ?? b.slot_id ?? b.id
    const currentLevel = b.level ?? 0
    setQueueItems((prev) => [...prev, {
      id: nextId(),
      name: b.name,
      slotId,
      currentLevel,
      targetLevel: currentLevel + 1,
      priority: 1,
    }])
    toast.success(`Added ${b.name} to queue`)
  }, [toast])

  // Apply template
  const handleApplyTemplate = useCallback((items) => {
    setQueueItems((prev) => [...prev, ...items])
    toast.success(`Added ${items.length} items to queue`)
  }, [toast])

  // Generate YAML — uses the local village ID, not the global active
  const generatedYaml = useMemo(() => queueToYaml(queueItems, villageId), [queueItems, villageId])

  // Validate
  const handleValidate = async () => {
    if (queueItems.length === 0) { toast.warning('Queue is empty'); return }
    if (!villageId) { toast.error('Select a village first'); return }
    try {
      const res = await api.post('/queue/validate', { yaml_content: generatedYaml })
      setValidationResult(res.data)
      toast.success('Plan validated')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Validation failed')
      setValidationResult(null)
    }
  }

  // ── Resumable hook handler ──────────────────────────────────────
  const msgIdRef = useRef(Date.now())
  const addMessage = useCallback((type, text, extra) => {
    setWsMessages((prev) => [
      ...prev,
      { id: ++msgIdRef.current, type, text, timestamp: new Date().toISOString(), ...extra },
    ])
  }, [])

  const handleQueueMessage = useCallback((data) => {
    if (!mountedRef.current || !data) return
    if (data.type === 'session_init') {
      addMessage('info', `Session: ${data.session_id} (viewable from /sessions)`)
    } else if (data.type === 'trigger_info') {
      addMessage('warning', `$ ${data.command}`, data.plan_yaml ? { detail: data.plan_yaml, detailLabel: 'Show plan.yaml' } : undefined)
    } else if (data.type === 'status') {
      addMessage('info', data.message)
    } else if (data.type === 'step_complete') {
      addMessage(data.success ? 'success' : 'error', `${data.building} -> Level ${data.level}: ${data.success ? 'Done' : 'Failed'}`)
    } else if (data.type === 'complete') {
      addMessage('success', 'Build queue completed!')
      setRunning(false); setWsStatus('disconnected')
    } else if (data.type === 'error') {
      addMessage('error', data.message)
    } else if (data.type === 'already_running') {
      addMessage('warning', data.message || 'A queue is already running for this village')
    } else if (data.message) {
      addMessage('info', data.message)
    }
  }, [addMessage])

  const handleQueueStatusChange = useCallback((next) => {
    if (next === 'connecting' || next === 'reconnecting' || next === 'running') {
      // On page reload / Safari resume, the hook reattaches to a still-
      // running session and only flips status. Mirror that into `running`
      // so the Stop button is visible (otherwise the UI shows the Execute
      // button as if nothing were live).
      setRunning(true)
      setWsStatus(next === 'running' ? 'running' : next)
    } else if (next === 'completed' || next === 'stopped' || next === 'failed') {
      setRunning(false); setWsStatus('disconnected')
    }
  }, [])

  const queueOp = useResumableOperation('queue', {
    onMessage: handleQueueMessage,
    onStatusChange: handleQueueStatusChange,
  })

  // Execute
  const startExecution = useCallback(() => {
    if (!villageId) { toast.error('Select a village first'); return }
    setShowConfirm(false)
    timersRef.current.forEach(({ type, id }) => type === 'interval' ? clearInterval(id) : clearTimeout(id))
    timersRef.current = []

    setWsMessages([])
    setWsStatus('connecting')
    setRunning(true)

    const itemCount = queueItems.length
    addMessage('info', `Executing ${itemCount} items${useVideo ? ' with video speed-up' : ''}...`)

    queueOp.start('/ws/queue/run', {
      yaml_content: generatedYaml,
      poll_interval: pollInterval,
      use_video: useVideo,
      verbose,
    })
  }, [villageId, generatedYaml, pollInterval, useVideo, verbose, queueItems.length, queueOp, addMessage, toast])

  const handleStop = () => {
    queueOp.stop()
    toast.warning('Stop signal sent')
  }

  return (
    <div className="p-6 max-w-[1200px] mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Build Queue</h2>
        {/* Local village selector — does NOT change the global active village */}
        {villages && villages.length > 0 && (
          <select
            value={villageId || ''}
            onChange={(e) => { const id = Number(e.target.value); if (id) handleVillageSwitch(id) }}
            disabled={running}
            className="input-field max-w-[260px] cursor-pointer bg-surface text-primary"
          >
            {villages.map((v) => (
              <option key={v.id} value={v.id}>{v.name} ({v.x}|{v.y})</option>
            ))}
          </select>
        )}
      </div>

      {/* Construction queue (in-progress) */}
      <ConstructionQueue queue={constructionQueue} />

      {/* Main layout: buildings list + queue builder */}
      <div className="flex gap-4 mb-4" style={{ minHeight: 400 }}>
        {/* Left: Building list */}
        <div className="card flex-1 min-w-0 overflow-y-auto" style={{ maxHeight: 600 }}>
          <div className="flex justify-between items-center mb-3">
            <h3 className="heading-gold text-base">Village Buildings</h3>
            <button className="btn-secondary btn-xs" onClick={() => fetchLocalData(villageId)} disabled={buildingsLoading}>
              {buildingsLoading ? '...' : 'Refresh'}
            </button>
          </div>
          {buildingsLoading ? (
            <div className="flex items-center gap-2 py-8 justify-center">
              <div className="spinner spinner-sm" /><span className="text-secondary text-sm">Loading...</span>
            </div>
          ) : buildingList.length === 0 ? (
            <p className="text-secondary text-sm">No buildings available. Connect to a server first.</p>
          ) : (
            <BuildingList buildings={buildingList} onAdd={handleAddBuilding} queueItems={queueItems} />
          )}
        </div>

        {/* Right: Queue builder */}
        <div className="card flex-1 min-w-0 overflow-y-auto" style={{ maxHeight: 600 }}>
          <div className="flex justify-between items-center mb-3 flex-wrap gap-2">
            <h3 className="heading-gold text-base">Queue ({queueItems.length} items)</h3>
            <TemplateButtons buildings={buildingList} onApply={handleApplyTemplate} />
          </div>
          <QueuePanel items={queueItems} setItems={setQueueItems} />
        </div>
      </div>

      {/* Execution options */}
      <div className="card mb-4">
        <h3 className="heading-gold text-base mb-3">Execution Options</h3>
        <div className="flex gap-6 items-center flex-wrap">
          <label className="check-label-secondary gap-2">
            Poll interval (s):
            <input type="number" min={5} max={600} value={pollInterval} onChange={(e) => setPollInterval(Number(e.target.value) || 30)} className="input-field w-20 text-center" />
          </label>
          <label className="check-label-secondary">
            <input type="checkbox" checked={useVideo} onChange={(e) => setUseVideo(e.target.checked)} className="checkbox-gold" />
            Use video speed-up (25% faster)
          </label>
          <label className="check-label-secondary">
            <input type="checkbox" checked={verbose} onChange={(e) => setVerbose(e.target.checked)} className="checkbox-gold" />
            Verbose
          </label>
        </div>
      </div>

      {/* Actions */}
      <div className="flex gap-3 items-center flex-wrap mb-4">
        <button className="btn-secondary min-w-[120px]" onClick={handleValidate} disabled={queueItems.length === 0 || running}>
          Validate
        </button>
        <button className="btn-primary min-w-[140px]" onClick={() => setShowConfirm(true)} disabled={queueItems.length === 0 || running}>
          Execute Queue
        </button>
        {running && <button className="btn-danger min-w-[100px]" onClick={handleStop}>Stop</button>}
        <YamlPreview yaml={generatedYaml} />
      </div>

      {/* Validation results */}
      {validationResult && validationResult.items && validationResult.items.length > 0 && (
        <div className="card mb-4">
          <h3 className="heading-gold text-base mb-3">Validation Results</h3>
          {validationResult.messages?.length > 0 && (
            <div className="mb-2">{validationResult.messages.map((m, i) => <div key={i} className="text-xs text-secondary font-mono">{m}</div>)}</div>
          )}
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead><tr><th>Building</th><th className="text-center">Slot</th><th className="text-center">Current</th><th className="text-center">Target</th><th className="text-center">Status</th></tr></thead>
              <tbody>
                {validationResult.items.map((item, i) => (
                  <tr key={i}>
                    <td>{item.building}{item.is_construction && <span className="ml-1 text-xs text-warning">(new)</span>}</td>
                    <td className="text-center font-mono text-secondary">{item.slot_id ?? '---'}</td>
                    <td className="text-center font-mono">{item.current_level ?? '---'}</td>
                    <td className="text-center font-mono text-gold">{item.target}</td>
                    <td className="text-center"><span className={`status-badge status-badge-${item.status === 'done' ? 'done' : item.status === 'skipped' ? 'skipped' : 'pending'}`}>{item.status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Execution log */}
      {(wsMessages.length > 0 || running) && (
        <div>
          <h3 className="heading-gold text-base mb-2">Execution Log</h3>
          <WebSocketPanel messages={wsMessages} status={wsStatus} onClear={() => setWsMessages([])} />
        </div>
      )}

      {/* Confirm dialog */}
      <ConfirmDialog
        open={showConfirm}
        title="Execute Build Queue"
        message={`Start building ${queueItems.length} items${useVideo ? ' with video speed-up' : ''}? The process runs in the background.`}
        confirmText="Execute"
        cancelText="Cancel"
        onConfirm={startExecution}
        onCancel={() => setShowConfirm(false)}
      />
    </div>
  )
}
