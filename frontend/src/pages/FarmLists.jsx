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
function raidResultColor(result) {
  if (!result) return 'var(--text-secondary)'
  const icon = (result.icon ?? result.status ?? '').toLowerCase()
  if (icon.includes('green') || icon === 'ok' || icon === 'success' || result.losses === 0)
    return 'var(--success)'
  if (icon.includes('yellow') || icon === 'partial' || icon === 'some')
    return 'var(--warning)'
  if (icon.includes('red') || icon === 'dead' || icon === 'fail' || icon === 'total')
    return 'var(--danger)'
  return 'var(--text-secondary)'
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
function transformWsMessage(data) {
  const ts = new Date()
  switch (data.type) {
    case 'info':
      return {
        type: 'info',
        text: data.list_names
          ? `Connected: ${data.list_count} list(s) - ${data.list_names.join(', ')}`
          : data.message || 'Info',
        timestamp: ts,
      }
    case 'cycle_start':
      return {
        type: 'info',
        text: `Cycle ${data.cycle} started`,
        timestamp: ts,
      }
    case 'result':
      return {
        type: data.success ? 'success' : 'error',
        text: data.success
          ? `Sent to ${data.slot || '???'}: ${data.message || 'OK'}`
          : `Failed ${data.slot || '???'}: ${data.message || 'error'}`,
        timestamp: ts,
      }
    case 'cycle_end':
      return {
        type: 'info',
        text: `Cycle ${data.cycle} finished - sent: ${data.sent ?? 0}, failed: ${data.failed ?? 0}`,
        timestamp: ts,
      }
    case 'error':
      return { type: 'error', text: data.message || 'Unknown error', timestamp: ts }
    case 'complete':
      return {
        type: 'success',
        text: `Completed after ${data.total_cycles ?? '?'} cycle(s)`,
        timestamp: ts,
      }
    default:
      return { type: 'info', text: JSON.stringify(data), timestamp: ts }
  }
}

// ---------------------------------------------------------------------------
//  Shared style constants
// ---------------------------------------------------------------------------
const sectionTitle = {
  fontFamily: 'Cinzel, serif',
  fontSize: '1rem',
  marginBottom: '0.75rem',
  color: 'var(--accent-gold)',
  display: 'flex',
  alignItems: 'center',
  gap: '0.5rem',
}

const tableCell = {
  padding: '0.5rem 0.75rem',
  fontSize: '0.85rem',
  borderBottom: '1px solid var(--border)',
}

const tableHeader = {
  ...tableCell,
  color: 'var(--text-secondary)',
  fontWeight: 600,
  whiteSpace: 'nowrap',
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
      toast.error('Failed to load farm lists')
    } finally {
      setLoading(false)
    }
  }, [toast])

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

    // Once open, send start action
    const origOnOpen = ws.onopen
    ws.onopen = (e) => {
      origOnOpen?.(e)
      setWsStatus('running')
      ws.send(JSON.stringify({ action: 'start' }))
    }

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
    <div style={{ padding: '1.5rem', maxWidth: '1100px', margin: '0 auto' }}>
      {/* Page title */}
      <h2 style={{ fontFamily: 'Cinzel, serif', fontSize: '1.5rem', marginBottom: '1.25rem' }}>
        Farm Lists
      </h2>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
        {/* =============================================================== */}
        {/*  SECTION 1 - Farm List Overview                                 */}
        {/* =============================================================== */}
        <div className="card" style={{ padding: '1rem' }}>
          <h3 style={sectionTitle}>Farm List Overview</h3>

          {/* Create new list form */}
          <div
            style={{
              display: 'flex',
              gap: '0.5rem',
              alignItems: 'flex-end',
              flexWrap: 'wrap',
              marginBottom: '1rem',
            }}
          >
            <div style={{ flex: '1 1 180px' }}>
              <label
                style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}
              >
                List Name
              </label>
              <input
                className="input-field"
                placeholder="New farm list"
                value={newListName}
                onChange={(e) => setNewListName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              />
            </div>
            <div style={{ flex: '0 0 180px' }}>
              <label
                style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}
              >
                Village
              </label>
              <select
                className="input-field"
                value={newListVillage}
                onChange={(e) => setNewListVillage(e.target.value)}
                style={{ cursor: 'pointer' }}
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
              className="btn-primary"
              disabled={creating || !newListName.trim()}
              onClick={handleCreate}
              style={{ height: '38px' }}
            >
              {creating ? 'Creating...' : 'Create List'}
            </button>
          </div>

          {/* Table of lists */}
          {loading ? (
            <p style={{ color: 'var(--text-secondary)', fontStyle: 'italic', padding: '0.5rem 0' }}>
              Loading...
            </p>
          ) : lists.length === 0 ? (
            <p style={{ color: 'var(--text-secondary)', fontStyle: 'italic', padding: '0.5rem 0' }}>
              No farm lists found. Create one above.
            </p>
          ) : (
            <>
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr style={{ borderBottom: '2px solid var(--border)' }}>
                      <th style={tableHeader}>Name</th>
                      <th style={tableHeader}>Village</th>
                      <th style={{ ...tableHeader, textAlign: 'center' }}>Slots</th>
                      <th style={{ ...tableHeader, textAlign: 'center' }}>Running</th>
                      <th style={{ ...tableHeader, textAlign: 'right' }}>Total Booty</th>
                      <th style={{ ...tableHeader, textAlign: 'center' }}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {lists.map((list) => {
                      const isSelected = selectedListId === list.id
                      return (
                        <tr
                          key={list.id}
                          onClick={() => setSelectedListId(isSelected ? null : list.id)}
                          style={{
                            cursor: 'pointer',
                            backgroundColor: isSelected
                              ? 'rgba(201, 168, 76, 0.08)'
                              : 'transparent',
                            transition: 'background-color 0.15s',
                          }}
                          onMouseEnter={(e) => {
                            if (!isSelected)
                              e.currentTarget.style.backgroundColor = 'rgba(201, 168, 76, 0.04)'
                          }}
                          onMouseLeave={(e) => {
                            if (!isSelected)
                              e.currentTarget.style.backgroundColor = 'transparent'
                          }}
                        >
                          <td style={{ ...tableCell, color: 'var(--text-primary)', fontWeight: 600 }}>
                            {list.name}
                          </td>
                          <td style={{ ...tableCell, color: 'var(--text-secondary)' }}>
                            {list.village_name || list.village || '---'}
                          </td>
                          <td style={{ ...tableCell, textAlign: 'center' }}>
                            {list.slot_count ?? list.slots ?? '---'}
                          </td>
                          <td style={{ ...tableCell, textAlign: 'center' }}>
                            {list.running_raids ?? list.raids ?? 0}
                          </td>
                          <td style={{ ...tableCell, textAlign: 'right', color: 'var(--accent-gold)' }}>
                            {list.total_booty != null ? list.total_booty.toLocaleString() : '---'}
                          </td>
                          <td
                            style={{ ...tableCell, textAlign: 'center' }}
                            onClick={(e) => e.stopPropagation()}
                          >
                            <div style={{ display: 'flex', gap: '0.35rem', justifyContent: 'center' }}>
                              <button
                                className="btn-primary"
                                style={{ padding: '0.25rem 0.6rem', fontSize: '0.78rem' }}
                                disabled={sendingListId === list.id}
                                onClick={() => handleSendList(list.id)}
                              >
                                {sendingListId === list.id ? 'Sending...' : 'Send'}
                              </button>
                              <button
                                className="btn-danger"
                                style={{ padding: '0.25rem 0.6rem', fontSize: '0.78rem' }}
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
              <div style={{ marginTop: '0.75rem', textAlign: 'right' }}>
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
          <div className="card" style={{ padding: '1rem' }}>
            <h3 style={sectionTitle}>
              {detail?.name || 'List Detail'}
              {detailLoading && (
                <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', fontWeight: 400 }}>
                  {' '}loading...
                </span>
              )}
            </h3>

            {/* Add Target form */}
            <div
              style={{
                display: 'flex',
                gap: '0.5rem',
                alignItems: 'flex-end',
                flexWrap: 'wrap',
                marginBottom: '1rem',
                padding: '0.75rem',
                backgroundColor: 'var(--bg-surface)',
                borderRadius: '0.375rem',
                border: '1px solid var(--border)',
              }}
            >
              <div style={{ flex: '0 0 80px' }}>
                <label
                  style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}
                >
                  X
                </label>
                <input
                  className="input-field"
                  type="number"
                  value={targetX}
                  onChange={(e) => setTargetX(e.target.value)}
                  placeholder="0"
                />
              </div>
              <div style={{ flex: '0 0 80px' }}>
                <label
                  style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}
                >
                  Y
                </label>
                <input
                  className="input-field"
                  type="number"
                  value={targetY}
                  onChange={(e) => setTargetY(e.target.value)}
                  placeholder="0"
                />
              </div>
              <label
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.35rem',
                  fontSize: '0.85rem',
                  color: 'var(--text-secondary)',
                  cursor: 'pointer',
                  paddingBottom: '0.3rem',
                }}
              >
                <input
                  type="checkbox"
                  checked={targetForce}
                  onChange={(e) => setTargetForce(e.target.checked)}
                />
                Force
              </label>
              <button
                className="btn-primary"
                disabled={addingTarget || targetX === '' || targetY === ''}
                onClick={handleAddTarget}
                style={{ height: '38px' }}
              >
                {addingTarget ? 'Adding...' : 'Add Target'}
              </button>
            </div>

            {/* Slot table */}
            {slots.length === 0 ? (
              <p style={{ color: 'var(--text-secondary)', fontStyle: 'italic', padding: '0.25rem 0' }}>
                No targets in this list.
              </p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr style={{ borderBottom: '2px solid var(--border)' }}>
                      <th style={tableHeader}>Coords</th>
                      <th style={tableHeader}>Target</th>
                      <th style={{ ...tableHeader, textAlign: 'center' }}>Pop</th>
                      <th style={{ ...tableHeader, textAlign: 'center' }}>Distance</th>
                      <th style={tableHeader}>Troops</th>
                      <th style={{ ...tableHeader, textAlign: 'center' }}>Active</th>
                      <th style={tableHeader}>Last Raid</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slots.map((slot, idx) => {
                      const lastRaid = slot.lastRaid ?? slot.last_raid ?? null
                      return (
                        <tr key={slot.id ?? idx}>
                          <td style={{ ...tableCell, fontFamily: 'monospace', color: 'var(--text-primary)' }}>
                            ({slot.x ?? '?'}, {slot.y ?? '?'})
                          </td>
                          <td style={{ ...tableCell, color: 'var(--text-primary)' }}>
                            {slot.target_name ?? slot.name ?? '---'}
                          </td>
                          <td style={{ ...tableCell, textAlign: 'center' }}>
                            {slot.population ?? '---'}
                          </td>
                          <td style={{ ...tableCell, textAlign: 'center' }}>
                            {slot.distance != null ? slot.distance.toFixed(1) : '---'}
                          </td>
                          <td style={{ ...tableCell, color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
                            {formatTroops(slot.troops)}
                          </td>
                          <td style={{ ...tableCell, textAlign: 'center' }}>
                            <span
                              style={{
                                display: 'inline-block',
                                width: '8px',
                                height: '8px',
                                borderRadius: '50%',
                                backgroundColor:
                                  slot.active ?? slot.enabled
                                    ? 'var(--success)'
                                    : 'var(--text-secondary)',
                              }}
                            />
                          </td>
                          <td style={{ ...tableCell, color: raidResultColor(lastRaid) }}>
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
        <div className="card" style={{ padding: '1rem' }}>
          <h3 style={sectionTitle}>Loop Send Mode</h3>

          {/* List picker */}
          <div style={{ marginBottom: '0.75rem' }}>
            <label
              style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '0.35rem' }}
            >
              Select lists to include:
            </label>
            {lists.length === 0 ? (
              <p style={{ color: 'var(--text-secondary)', fontStyle: 'italic', fontSize: '0.85rem' }}>
                No lists available.
              </p>
            ) : (
              <div
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  gap: '0.5rem',
                }}
              >
                {lists.map((list) => (
                  <label
                    key={list.id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '0.35rem',
                      padding: '0.35rem 0.7rem',
                      backgroundColor: loopListIds.includes(list.id)
                        ? 'rgba(201, 168, 76, 0.12)'
                        : 'var(--bg-surface)',
                      border: `1px solid ${loopListIds.includes(list.id) ? 'var(--accent-gold)' : 'var(--border)'}`,
                      borderRadius: '0.375rem',
                      cursor: loopRunning ? 'not-allowed' : 'pointer',
                      fontSize: '0.85rem',
                      color: 'var(--text-primary)',
                      transition: 'all 0.15s',
                    }}
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
          <div
            style={{
              display: 'flex',
              gap: '0.75rem',
              alignItems: 'flex-end',
              flexWrap: 'wrap',
              marginBottom: '0.75rem',
            }}
          >
            <div style={{ flex: '0 0 140px' }}>
              <label
                style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}
              >
                Interval (seconds)
              </label>
              <input
                className="input-field"
                type="number"
                min="10"
                value={loopInterval}
                onChange={(e) => setLoopInterval(Number(e.target.value) || 10)}
                disabled={loopRunning}
              />
            </div>
            <div style={{ flex: '0 0 140px' }}>
              <label
                style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}
              >
                Duration (min, 0=forever)
              </label>
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
                className="btn-primary"
                onClick={startLoop}
                disabled={loopListIds.length === 0}
                style={{ height: '38px' }}
              >
                Start Loop
              </button>
            ) : (
              <button
                className="btn-danger"
                onClick={stopLoop}
                style={{ height: '38px' }}
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
        onConfirm={() => handleDelete(deleteConfirm.id)}
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
