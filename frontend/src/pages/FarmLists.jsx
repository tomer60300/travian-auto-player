import { useState, useEffect, useRef, useCallback } from 'react'
import api from '../api'
import { createWebSocket } from '../ws'
import { useToast } from '../components/Toast'
import WebSocketPanel from '../components/WebSocketPanel'
import ConfirmDialog from '../components/ConfirmDialog'
import useGameStore from '../stores/gameStore'

// ---------------------------------------------------------------------------
//  Raid result color helpers
// ---------------------------------------------------------------------------
function raidResultClass(result) {
  if (!result) return 'text-secondary'
  const icon = (result.icon ?? result.status ?? '').toLowerCase()
  if (icon.includes('green') || icon === 'ok' || icon === 'success' || result.losses === 0)
    return 'text-success'
  if (icon.includes('yellow') || icon === 'partial' || icon === 'some')
    return 'text-warning'
  if (icon.includes('red') || icon === 'dead' || icon === 'fail' || icon === 'total')
    return 'text-danger'
  return 'text-secondary'
}

function raidResultLabel(result) {
  if (!result) return '---'
  if (result.message) return result.message
  const icon = (result.icon ?? result.status ?? '').toLowerCase()
  if (icon.includes('green') || icon === 'ok' || icon === 'success') return 'No losses'
  if (icon.includes('yellow') || icon === 'partial' || icon === 'some') return 'Some losses'
  if (icon.includes('red') || icon === 'dead' || icon === 'fail' || icon === 'total')
    return 'All dead'
  return result.icon || result.status || '---'
}

// ---------------------------------------------------------------------------
//  Transform WS messages for WebSocketPanel
// ---------------------------------------------------------------------------
let _farmMsgId = 0
function transformWsMessage(data) {
  const id = ++_farmMsgId
  const ts = new Date()
  const base = { id, timestamp: ts }
  switch (data.type) {
    case 'info':
      return { ...base, type: 'info', text: data.list_names
        ? `Connected: ${data.list_count} list(s) - ${data.list_names.join(', ')}`
        : data.message || 'Info' }
    case 'cycle_start':
      return { ...base, type: 'info', text: `Cycle ${data.cycle} started` }
    case 'result':
      return { ...base, type: data.success ? 'success' : 'error', text: data.success
        ? `Sent to ${data.slot || '???'}: ${data.message || 'OK'}`
        : `Failed ${data.slot || '???'}: ${data.message || 'error'}` }
    case 'cycle_end':
      return { ...base, type: 'info', text: `Cycle ${data.cycle} finished - sent: ${data.sent ?? 0}, failed: ${data.failed ?? 0}` }
    case 'error':
      return { ...base, type: 'error', text: data.message || 'Unknown error' }
    case 'complete':
      return { ...base, type: 'success', text: `Completed after ${data.total_cycles ?? '?'} cycle(s)` }
    default:
      return { ...base, type: 'info', text: JSON.stringify(data) }
  }
}

// ===========================================================================
//  MAIN COMPONENT
// ===========================================================================
export default function FarmLists() {
  const toast = useToast()
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const villages = useGameStore((s) => s.villages)

  // ---- Farm list overview ----
  const [lists, setLists] = useState([])
  const [loading, setLoading] = useState(true)
  const [selectedListId, setSelectedListId] = useState(null)

  // ---- New list form ----
  const [newListName, setNewListName] = useState('')
  const [newListVillage, setNewListVillage] = useState('')
  const [creating, setCreating] = useState(false)

  // ---- Detail ----
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)

  // ---- Add target ----
  const [targetX, setTargetX] = useState('')
  const [targetY, setTargetY] = useState('')
  const [targetForce, setTargetForce] = useState(false)
  const [addingTarget, setAddingTarget] = useState(false)

  // ---- Sending ----
  const [sendingListId, setSendingListId] = useState(null)
  const [sendingAll, setSendingAll] = useState(false)

  // ---- Delete confirmation ----
  const [deleteConfirm, setDeleteConfirm] = useState(null)

  // ---- Loop mode ----
  const [loopListIds, setLoopListIds] = useState([])
  const [loopInterval, setLoopInterval] = useState(300)
  const [loopDuration, setLoopDuration] = useState(0)
  const [loopRunning, setLoopRunning] = useState(false)
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [wsMessages, setWsMessages] = useState([])
  const wsRef = useRef(null)

  // -----------------------------------------------------------------
  //  Fetch all lists
  // -----------------------------------------------------------------
  const fetchLists = useCallback(async () => {
    try {
      setLoading(true)
      const res = await api.get('/farm/lists')
      setLists(Array.isArray(res.data) ? res.data : [])
    } catch (err) {
      if (err.response?.status !== 403) {
        toast.error('Failed to load farm lists')
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchLists()
  }, [fetchLists])

  // Default new-list village to active
  useEffect(() => {
    if (activeVillageId && !newListVillage) {
      setNewListVillage(String(activeVillageId))
    }
  }, [activeVillageId, newListVillage])

  // -----------------------------------------------------------------
  //  Fetch detail when a list is selected
  // -----------------------------------------------------------------
  useEffect(() => {
    if (!selectedListId) {
      setDetail(null)
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        setDetailLoading(true)
        const res = await api.get(`/farm/lists/${selectedListId}`)
        if (!cancelled) setDetail(res.data)
      } catch {
        if (!cancelled) {
          toast.error('Failed to load list detail')
          setDetail(null)
        }
      } finally {
        if (!cancelled) setDetailLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [selectedListId, toast])

  // -----------------------------------------------------------------
  //  Create list
  // -----------------------------------------------------------------
  const handleCreate = async () => {
    if (!newListName.trim()) return
    try {
      setCreating(true)
      await api.post('/farm/lists', {
        name: newListName.trim(),
        village_id: newListVillage ? Number(newListVillage) : undefined,
      })
      toast.success(`List "${newListName.trim()}" created`)
      setNewListName('')
      await fetchLists()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to create list')
    } finally {
      setCreating(false)
    }
  }

  // -----------------------------------------------------------------
  //  Delete list
  // -----------------------------------------------------------------
  const handleDelete = async (id) => {
    try {
      await api.delete(`/farm/lists/${id}`)
      toast.success('List deleted')
      if (selectedListId === id) {
        setSelectedListId(null)
        setDetail(null)
      }
      await fetchLists()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to delete list')
    } finally {
      setDeleteConfirm(null)
    }
  }

  // -----------------------------------------------------------------
  //  Add target
  // -----------------------------------------------------------------
  const handleAddTarget = async () => {
    if (targetX === '' || targetY === '') return
    try {
      setAddingTarget(true)
      await api.post(`/farm/lists/${selectedListId}/targets`, {
        x: Number(targetX),
        y: Number(targetY),
        force: targetForce,
      })
      toast.success(`Target (${targetX}, ${targetY}) added`)
      setTargetX('')
      setTargetY('')
      setTargetForce(false)
      // Refresh detail
      const res = await api.get(`/farm/lists/${selectedListId}`)
      setDetail(res.data)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to add target')
    } finally {
      setAddingTarget(false)
    }
  }

  // -----------------------------------------------------------------
  //  Send list (one-shot)
  // -----------------------------------------------------------------
  const handleSendList = async (id) => {
    try {
      setSendingListId(id)
      const res = await api.post(`/farm/lists/${id}/send`)
      const msg = res.data?.message || res.data?.detail || 'List sent'
      toast.success(msg)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to send list')
    } finally {
      setSendingListId(null)
    }
  }

  // -----------------------------------------------------------------
  //  Send all lists
  // -----------------------------------------------------------------
  const handleSendAll = async () => {
    try {
      setSendingAll(true)
      const ids = lists.map((l) => l.id)
      const res = await api.post('/farm/send-all', { list_ids: ids })
      const msg = res.data?.message || res.data?.detail || 'All lists sent'
      toast.success(msg)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to send all')
    } finally {
      setSendingAll(false)
    }
  }

  // -----------------------------------------------------------------
  //  Loop mode toggle
  // -----------------------------------------------------------------
  const toggleLoopList = (id) => {
    setLoopListIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    )
  }

  const startLoop = () => {
    if (loopListIds.length === 0) {
      toast.warning('Select at least one list')
      return
    }
    setWsMessages([])
    setLoopRunning(true)
    setWsStatus('connected')

    const qs = `interval=${loopInterval}&duration=${loopDuration}&list_ids=${loopListIds.join(',')}`
    const ws = createWebSocket(
      `/ws/farm/run-all?${qs}`,
      (data) => {
        setWsMessages((prev) => [...prev, transformWsMessage(data)])
        if (data.type === 'complete') {
          setLoopRunning(false)
          setWsStatus('disconnected')
          toast.success('Loop completed')
        }
      },
      () => {
        setWsStatus('disconnected')
        setLoopRunning(false)
        toast.error('WebSocket error')
      },
      () => {
        setWsStatus('disconnected')
        setLoopRunning(false)
      }
    )

    if (!ws) {
      toast.error('No auth token — cannot connect')
      setLoopRunning(false)
      setWsStatus('disconnected')
      return
    }

    // Once open, send start action (addEventListener to not overwrite ws.js log handler)
    ws.addEventListener('open', () => {
      setWsStatus('running')
      ws.send(JSON.stringify({ action: 'start' }))
    })

    wsRef.current = ws
  }

  const stopLoop = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'stop' }))
    }
    setLoopRunning(false)
    setWsStatus('disconnected')
  }

  // Clean up WS on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [])

  // ===========================================================================
  //  RENDER
  // ===========================================================================
  const slots = detail?.slots ?? detail?.targets ?? []

  return (
    <div className="p-6 max-w-[1100px] mx-auto">
      {/* Page title */}
      <h2 className="heading-gold text-2xl mb-5">
        Farm Lists
      </h2>

      <div className="flex flex-col gap-4">
        {/* =============================================================== */}
        {/*  SECTION 1 - Farm List Overview                                 */}
        {/* =============================================================== */}
        <div className="card">
          <h3 className="heading-gold text-base mb-3 flex items-center gap-2">Farm List Overview</h3>

          {/* Create new list form */}
          <div className="flex gap-2 items-end flex-wrap mb-4">
            <div className="flex-1 min-w-[180px]">
              <label className="field-label">List Name</label>
              <input
                className="input-field"
                placeholder="New farm list"
                value={newListName}
                onChange={(e) => setNewListName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              />
            </div>
            <div className="w-[180px] shrink-0">
              <label className="field-label">Village</label>
              <select
                className="input-field cursor-pointer"
                value={newListVillage}
                onChange={(e) => setNewListVillage(e.target.value)}
              >
                <option value="">-- Select --</option>
                {(villages || []).map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name} ({v.x}|{v.y})
                  </option>
                ))}
              </select>
            </div>
            <button
              className="btn-primary h-[38px]"
              disabled={creating || !newListName.trim()}
              onClick={handleCreate}
            >
              {creating ? 'Creating...' : 'Create List'}
            </button>
          </div>

          {/* Table of lists */}
          {loading ? (
            <p className="text-secondary italic py-2">
              Loading...
            </p>
          ) : lists.length === 0 ? (
            <p className="text-secondary italic py-2">
              No farm lists found. Create one above.
            </p>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Village</th>
                      <th className="text-center">Slots</th>
                      <th className="text-center">Running</th>
                      <th className="text-right">Total Booty</th>
                      <th className="text-center">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {lists.map((list) => {
                      const isSelected = selectedListId === list.id
                      return (
                        <tr
                          key={list.id}
                          className={`row-clickable ${isSelected ? 'row-selected' : ''}`}
                          onClick={() => setSelectedListId(isSelected ? null : list.id)}
                        >
                          <td className="text-primary font-semibold">
                            {list.name}
                          </td>
                          <td className="text-secondary">
                            {list.village_name || list.village || '---'}
                          </td>
                          <td className="text-center">
                            {list.slot_count ?? list.slots ?? '---'}
                          </td>
                          <td className="text-center">
                            {list.running_raids ?? list.raids ?? 0}
                          </td>
                          <td className="text-right text-gold">
                            {list.total_booty != null ? list.total_booty.toLocaleString() : '---'}
                          </td>
                          <td
                            className="text-center"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <div className="flex gap-1.5 justify-center">
                              <button
                                className="btn-primary btn-xs"
                                disabled={sendingListId === list.id}
                                onClick={() => handleSendList(list.id)}
                              >
                                {sendingListId === list.id ? 'Sending...' : 'Send'}
                              </button>
                              <button
                                className="btn-danger btn-xs"
                                onClick={() => setDeleteConfirm(list)}
                              >
                                Delete
                              </button>
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              {/* Send All button */}
              <div className="mt-3 text-right">
                <button
                  className="btn-secondary"
                  disabled={sendingAll || lists.length === 0}
                  onClick={handleSendAll}
                >
                  {sendingAll ? 'Sending All...' : 'Send All Lists'}
                </button>
              </div>
            </>
          )}
        </div>

        {/* =============================================================== */}
        {/*  SECTION 2 - Farm List Detail                                   */}
        {/* =============================================================== */}
        {selectedListId && (
          <div className="card">
            <h3 className="heading-gold text-base mb-3 flex items-center gap-2">
              {detail?.name || 'List Detail'}
              {detailLoading && (
                <span className="text-sm text-secondary font-normal">
                  {' '}loading...
                </span>
              )}
            </h3>

            {/* Add Target form */}
            <div className="flex gap-2 items-end flex-wrap mb-4 p-3 bg-surface rounded-md border-default">
              <div className="w-20 shrink-0">
                <label className="field-label">X</label>
                <input
                  className="input-field"
                  type="number"
                  value={targetX}
                  onChange={(e) => setTargetX(e.target.value)}
                  placeholder="0"
                />
              </div>
              <div className="w-20 shrink-0">
                <label className="field-label">Y</label>
                <input
                  className="input-field"
                  type="number"
                  value={targetY}
                  onChange={(e) => setTargetY(e.target.value)}
                  placeholder="0"
                />
              </div>
              <label className="check-label-secondary pb-1">
                <input
                  type="checkbox"
                  checked={targetForce}
                  onChange={(e) => setTargetForce(e.target.checked)}
                />
                Force
              </label>
              <button
                className="btn-primary h-[38px]"
                disabled={addingTarget || targetX === '' || targetY === ''}
                onClick={handleAddTarget}
              >
                {addingTarget ? 'Adding...' : 'Add Target'}
              </button>
            </div>

            {/* Slot table */}
            {slots.length === 0 ? (
              <p className="text-secondary italic py-1">
                No targets in this list.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Coords</th>
                      <th>Target</th>
                      <th className="text-center">Pop</th>
                      <th className="text-center">Distance</th>
                      <th>Troops</th>
                      <th className="text-center">Active</th>
                      <th>Last Raid</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slots.map((slot, idx) => {
                      const lastRaid = slot.lastRaid ?? slot.last_raid ?? null
                      return (
                        <tr key={slot.id ?? idx}>
                          <td className="font-mono text-primary">
                            ({slot.x ?? '?'}, {slot.y ?? '?'})
                          </td>
                          <td className="text-primary">
                            {slot.target_name ?? slot.name ?? '---'}
                          </td>
                          <td className="text-center">
                            {slot.population ?? '---'}
                          </td>
                          <td className="text-center">
                            {slot.distance != null ? slot.distance.toFixed(1) : '---'}
                          </td>
                          <td className="text-secondary text-xs">
                            {formatTroops(slot.troops)}
                          </td>
                          <td className="text-center">
                            <span
                              className={`status-dot ${
                                slot.active ?? slot.enabled
                                  ? 'status-dot-success'
                                  : ''
                              }`}
                            />
                          </td>
                          <td className={raidResultClass(lastRaid)}>
                            {raidResultLabel(lastRaid)}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* =============================================================== */}
        {/*  SECTION 3 - Loop Mode                                          */}
        {/* =============================================================== */}
        <div className="card">
          <h3 className="heading-gold text-base mb-3 flex items-center gap-2">Loop Send Mode</h3>

          {/* List picker */}
          <div className="mb-3">
            <label className="field-label-lg">
              Select lists to include:
            </label>
            {lists.length === 0 ? (
              <p className="text-secondary italic text-sm">
                No lists available.
              </p>
            ) : (
              <div className="flex flex-wrap gap-2">
                {lists.map((list) => (
                  <label
                    key={list.id}
                    className={`loop-chip ${
                      loopListIds.includes(list.id) ? 'loop-chip-active' : ''
                    } ${loopRunning ? 'loop-chip-disabled' : ''}`}
                  >
                    <input
                      type="checkbox"
                      checked={loopListIds.includes(list.id)}
                      onChange={() => toggleLoopList(list.id)}
                      disabled={loopRunning}
                    />
                    {list.name}
                  </label>
                ))}
              </div>
            )}
          </div>

          {/* Interval & duration */}
          <div className="flex gap-3 items-end flex-wrap mb-3">
            <div className="w-[140px] shrink-0">
              <label className="field-label">Interval (seconds)</label>
              <input
                className="input-field"
                type="number"
                min="10"
                value={loopInterval}
                onChange={(e) => setLoopInterval(Number(e.target.value) || 10)}
                disabled={loopRunning}
              />
            </div>
            <div className="w-[140px] shrink-0">
              <label className="field-label">Duration (min, 0=forever)</label>
              <input
                className="input-field"
                type="number"
                min="0"
                value={loopDuration}
                onChange={(e) => setLoopDuration(Number(e.target.value) || 0)}
                disabled={loopRunning}
              />
            </div>
            {!loopRunning ? (
              <button
                className="btn-primary h-[38px]"
                onClick={startLoop}
                disabled={loopListIds.length === 0}
              >
                Start Loop
              </button>
            ) : (
              <button
                className="btn-danger h-[38px]"
                onClick={stopLoop}
              >
                Stop Loop
              </button>
            )}
          </div>

          {/* WebSocket log panel */}
          <WebSocketPanel
            messages={wsMessages}
            status={wsStatus}
            onClear={() => setWsMessages([])}
          />
        </div>
      </div>

      {/* Delete confirm dialog */}
      <ConfirmDialog
        open={!!deleteConfirm}
        title="Delete Farm List"
        message={`Are you sure you want to delete "${deleteConfirm?.name}"? This cannot be undone.`}
        confirmText="Delete"
        variant="danger"
        onConfirm={() => { if (deleteConfirm?.id) handleDelete(deleteConfirm.id) }}
        onCancel={() => setDeleteConfirm(null)}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
//  Helpers
// ---------------------------------------------------------------------------
function formatTroops(troops) {
  if (!troops) return '---'
  if (typeof troops === 'string') return troops
  if (Array.isArray(troops)) {
    const parts = troops
      .filter((t) => t && (t.count > 0 || t.amount > 0))
      .map((t) => `${t.name ?? t.type ?? '?'}: ${t.count ?? t.amount ?? 0}`)
    return parts.length > 0 ? parts.join(', ') : '---'
  }
  if (typeof troops === 'object') {
    const parts = Object.entries(troops)
      .filter(([, v]) => v > 0)
      .map(([k, v]) => `${k}: ${v}`)
    return parts.length > 0 ? parts.join(', ') : '---'
  }
  return String(troops)
}
