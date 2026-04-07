import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import api from '../api'
import { createWebSocket } from '../ws'
import { useToast } from '../components/Toast'
import WebSocketPanel from '../components/WebSocketPanel'
import ConfirmDialog from '../components/ConfirmDialog'
import useGameStore from '../stores/gameStore'

// ---------------------------------------------------------------------------
//  Raid icon helpers (backend sends: "no_loss", "some_loss", "all_dead", "unknown")
// ---------------------------------------------------------------------------
function raidIconClass(icon) {
  if (!icon) return 'text-secondary'
  if (icon === 'no_loss') return 'text-success'
  if (icon === 'some_loss') return 'text-warning'
  if (icon === 'all_dead') return 'text-danger'
  return 'text-secondary'
}

function raidIconLabel(icon) {
  if (!icon) return '---'
  if (icon === 'no_loss') return 'No losses'
  if (icon === 'some_loss') return 'Some losses'
  if (icon === 'all_dead') return 'All dead'
  return icon
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
  const [deleteTargetConfirm, setDeleteTargetConfirm] = useState(null)
  const [deletingTargetId, setDeletingTargetId] = useState(null)

  // ---- Slot pagination ----
  const [showAllSlots, setShowAllSlots] = useState(false)

  // ---- Multi-select copy/move ----
  const [selectedSlotIds, setSelectedSlotIds] = useState(new Set())
  const [transferTarget, setTransferTarget] = useState('')
  const [transferMode, setTransferMode] = useState('copy') // 'copy' | 'move'
  const [transferring, setTransferring] = useState(false)

  // ---- Defense scan (background) ----
  const [defenseData, setDefenseData] = useState({})
  const [defenseScanning, setDefenseScanning] = useState(false)

  // ---- Sorting & Filtering ----
  const [sortField, setSortField] = useState('distance') // distance|population|total_booty|total_raids|booty_ratio
  const [sortDir, setSortDir] = useState('asc') // asc|desc
  const [filterActive, setFilterActive] = useState('all') // all|active|inactive
  const [filterFullBooty, setFilterFullBooty] = useState(false)
  const [filterMaxDist, setFilterMaxDist] = useState('')
  const [filterMinPop, setFilterMinPop] = useState('')

  // ---- Loop mode ----
  const [loopListIds, setLoopListIds] = useState([])
  const [loopInterval, setLoopInterval] = useState(300)
  const [loopDuration, setLoopDuration] = useState(0)
  const [loopRunning, setLoopRunning] = useState(false)
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [wsMessages, setWsMessages] = useState([])
  const wsRef = useRef(null)
  const mountedRef = useRef(true)
  useEffect(() => { return () => { mountedRef.current = false } }, [])

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
      setShowAllSlots(false)
      return
    }
    setShowAllSlots(false)
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
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedListId])

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
  //  Delete target
  // -----------------------------------------------------------------
  const handleDeleteTarget = async (slotId) => {
    try {
      setDeletingTargetId(slotId)
      await api.delete(`/farm/lists/${selectedListId}/targets/${slotId}`)
      toast.success('Target deleted')
      // Refresh detail
      const res = await api.get(`/farm/lists/${selectedListId}`)
      setDetail(res.data)
      await fetchLists()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to delete target')
    } finally {
      setDeletingTargetId(null)
      setDeleteTargetConfirm(null)
    }
  }

  // -----------------------------------------------------------------
  //  Multi-select helpers
  // -----------------------------------------------------------------
  const toggleSlotSelection = (slotId) => {
    setSelectedSlotIds((prev) => {
      const next = new Set(prev)
      if (next.has(slotId)) next.delete(slotId)
      else next.add(slotId)
      return next
    })
  }

  const selectAllSlots = () => {
    const all = filteredSortedSlots.map((s) => s.id)
    setSelectedSlotIds(new Set(all))
  }

  const deselectAllSlots = () => setSelectedSlotIds(new Set())

  // Clear selection when switching lists
  useEffect(() => {
    setSelectedSlotIds(new Set())
  }, [selectedListId])

  // -----------------------------------------------------------------
  //  Copy / Move targets to another list
  // -----------------------------------------------------------------
  const handleTransfer = async () => {
    if (!transferTarget || selectedSlotIds.size === 0) return
    const destListId = Number(transferTarget)
    if (destListId === selectedListId) {
      toast.warning('Source and destination are the same')
      return
    }

    const allSlotsList = detail?.slots ?? []
    const slotsToTransfer = allSlotsList.filter((s) => selectedSlotIds.has(s.id))
    if (slotsToTransfer.length === 0) return

    setTransferring(true)
    let addOk = 0
    let addFail = 0

    // 1. Add each selected target to destination list
    for (const slot of slotsToTransfer) {
      try {
        await api.post(`/farm/lists/${destListId}/targets`, {
          x: slot.x,
          y: slot.y,
          force: true,
        })
        addOk++
      } catch {
        addFail++
      }
    }

    // 2. If "move", delete from source
    let delOk = 0
    if (transferMode === 'move' && addOk > 0) {
      for (const slot of slotsToTransfer) {
        try {
          await api.delete(`/farm/lists/${selectedListId}/targets/${slot.id}`)
          delOk++
        } catch { /* ignore individual failures */ }
      }
    }

    // 3. Refresh data
    try {
      const res = await api.get(`/farm/lists/${selectedListId}`)
      setDetail(res.data)
    } catch {}
    await fetchLists()

    setSelectedSlotIds(new Set())
    setTransferring(false)

    const destName = lists.find((l) => l.id === destListId)?.name || `#${destListId}`
    if (transferMode === 'move') {
      toast.success(`Moved ${delOk} target(s) to "${destName}" (${addOk} added, ${addFail} failed)`)
    } else {
      toast.success(`Copied ${addOk} target(s) to "${destName}"${addFail ? ` (${addFail} failed)` : ''}`)
    }
  }

  // -----------------------------------------------------------------
  //  Background defense scan
  // -----------------------------------------------------------------
  const handleDefenseScan = async () => {
    if (!selectedListId) return
    setDefenseScanning(true)
    try {
      const res = await api.post('/farm/defense-scan', {
        list_id: selectedListId,
        max_pages: 5,
        max_age_hours: 48,
      })
      const map = {}
      for (const item of (res.data || [])) {
        map[item.slot_id] = item
      }
      setDefenseData(map)
      const count = Object.keys(map).length
      toast.success(`Defense scan complete: ${count} target(s) with report data`)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Defense scan failed')
    } finally {
      setDefenseScanning(false)
    }
  }

  // Clear defense data when switching lists
  useEffect(() => { setDefenseData({}) }, [selectedListId])

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
        if (!mountedRef.current) return
        setWsMessages((prev) => [...prev, transformWsMessage(data)])
        if (data.type === 'complete') {
          setLoopRunning(false)
          setWsStatus('disconnected')
          toast.success('Loop completed')
        }
      },
      () => {
        if (!mountedRef.current) return
        setWsStatus('disconnected')
        setLoopRunning(false)
        toast.error('WebSocket error')
      },
      () => {
        if (!mountedRef.current) return
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
  //  FILTERING + SORTING
  // ===========================================================================
  const rawSlots = detail?.slots ?? detail?.targets ?? []

  const filteredSortedSlots = useMemo(() => {
    let arr = [...rawSlots]

    // Filter: active status
    if (filterActive === 'active') arr = arr.filter((s) => s.is_active)
    else if (filterActive === 'inactive') arr = arr.filter((s) => !s.is_active)

    // Filter: full booty (resources/capacity >= 1)
    if (filterFullBooty) {
      arr = arr.filter((s) => {
        const lr = s.last_raid
        if (!lr || lr.resources == null || !lr.capacity) return false
        return lr.resources >= lr.capacity
      })
    }

    // Filter: max distance
    if (filterMaxDist !== '') {
      const maxD = Number(filterMaxDist)
      if (!isNaN(maxD)) arr = arr.filter((s) => (s.distance ?? 999) <= maxD)
    }

    // Filter: min population
    if (filterMinPop !== '') {
      const minP = Number(filterMinPop)
      if (!isNaN(minP)) arr = arr.filter((s) => (s.population ?? 0) >= minP)
    }

    // Sort
    arr.sort((a, b) => {
      let va, vb
      switch (sortField) {
        case 'distance': va = a.distance ?? 0; vb = b.distance ?? 0; break
        case 'population': va = a.population ?? 0; vb = b.population ?? 0; break
        case 'total_booty': va = a.total_booty ?? 0; vb = b.total_booty ?? 0; break
        case 'total_raids': va = a.total_raids ?? 0; vb = b.total_raids ?? 0; break
        case 'booty_ratio': {
          const ra = a.last_raid, rb = b.last_raid
          va = (ra && ra.capacity) ? (ra.resources ?? 0) / ra.capacity : -1
          vb = (rb && rb.capacity) ? (rb.resources ?? 0) / rb.capacity : -1
          break
        }
        case 'name': va = (a.name ?? '').toLowerCase(); vb = (b.name ?? '').toLowerCase(); break
        default: va = 0; vb = 0
      }
      if (va < vb) return sortDir === 'asc' ? -1 : 1
      if (va > vb) return sortDir === 'asc' ? 1 : -1
      return 0
    })

    return arr
  }, [rawSlots, filterActive, filterFullBooty, filterMaxDist, filterMinPop, sortField, sortDir])

  const SLOT_PAGE_SIZE = 50
  const slots = showAllSlots ? filteredSortedSlots : filteredSortedSlots.slice(0, SLOT_PAGE_SIZE)

  const handleSort = (field) => {
    if (sortField === field) setSortDir((d) => d === 'asc' ? 'desc' : 'asc')
    else { setSortField(field); setSortDir('asc') }
  }

  const sortArrow = (field) => sortField === field ? (sortDir === 'asc' ? ' \u25B2' : ' \u25BC') : ''

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
                      <th className="text-center">Slots</th>
                      <th className="text-center">Active</th>
                      <th className="text-center">Running</th>
                      <th className="text-right">Total Booty</th>
                      <th className="text-center">Raids</th>
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
                            {list.owner_village_name && (
                              <span className="text-xs text-secondary font-normal ml-1.5">
                                ({list.owner_village_name})
                              </span>
                            )}
                          </td>
                          <td className="text-center font-mono">
                            {list.slots_amount ?? '---'}
                          </td>
                          <td className="text-center font-mono text-success">
                            {list.active_slots ?? '---'}
                          </td>
                          <td className="text-center font-mono">
                            {list.running_raids ?? 0}
                          </td>
                          <td className="text-right text-gold font-mono">
                            {list.total_booty != null ? list.total_booty.toLocaleString() : '---'}
                          </td>
                          <td className="text-center font-mono text-secondary">
                            {list.total_raids ?? 0}
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
            <div className="flex justify-between items-center mb-3">
              <h3 className="heading-gold text-base flex items-center gap-2">
                {detail?.name || 'List Detail'}
                {detailLoading && (
                  <span className="text-sm text-secondary font-normal">
                    {' '}loading...
                  </span>
                )}
              </h3>
              <button
                className="btn-secondary btn-xs flex items-center gap-1.5"
                disabled={defenseScanning || !detail}
                onClick={handleDefenseScan}
                title="Scan recent reports for defender troop info on targets"
              >
                {defenseScanning && <span className="spinner spinner-sm" />}
                {defenseScanning ? 'Scanning Reports...' : 'Scan Defense'}
              </button>
            </div>

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

            {/* Multi-select toolbar */}
            {allSlots.length > 0 && (
              <div className="flex gap-2 items-center flex-wrap mb-3 p-2.5 bg-surface rounded-md border-default">
                <button className="btn-secondary btn-xs" onClick={selectAllSlots}>
                  Select All ({allSlots.length})
                </button>
                {selectedSlotIds.size > 0 && (
                  <button className="btn-secondary btn-xs" onClick={deselectAllSlots}>
                    Deselect
                  </button>
                )}
                {selectedSlotIds.size > 0 && (
                  <>
                    <span className="text-xs text-gold font-semibold">
                      {selectedSlotIds.size} selected
                    </span>
                    <span className="text-secondary text-xs">|</span>
                    <select
                      className="input-field text-xs py-1 px-2 w-auto min-w-[140px]"
                      value={transferTarget}
                      onChange={(e) => setTransferTarget(e.target.value)}
                    >
                      <option value="">-- Destination --</option>
                      {lists
                        .filter((l) => l.id !== selectedListId)
                        .map((l) => (
                          <option key={l.id} value={l.id}>{l.name}</option>
                        ))}
                    </select>
                    <select
                      className="input-field text-xs py-1 px-2 w-auto"
                      value={transferMode}
                      onChange={(e) => setTransferMode(e.target.value)}
                    >
                      <option value="copy">Copy</option>
                      <option value="move">Move</option>
                    </select>
                    <button
                      className="btn-primary btn-xs"
                      disabled={!transferTarget || transferring}
                      onClick={handleTransfer}
                    >
                      {transferring
                        ? 'Transferring...'
                        : transferMode === 'move'
                          ? `Move ${selectedSlotIds.size}`
                          : `Copy ${selectedSlotIds.size}`}
                    </button>
                  </>
                )}
              </div>
            )}

            {/* Filter bar */}
            {rawSlots.length > 0 && (
              <div className="flex gap-3 items-center flex-wrap mb-3 p-2.5 bg-surface rounded-md border-default text-xs">
                <div className="flex items-center gap-1.5">
                  <span className="text-secondary">Status:</span>
                  <select className="input-field text-xs py-0.5 px-1.5 w-auto" value={filterActive} onChange={(e) => setFilterActive(e.target.value)}>
                    <option value="all">All</option>
                    <option value="active">Active only</option>
                    <option value="inactive">Inactive only</option>
                  </select>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-secondary">Max dist:</span>
                  <input className="input-field text-xs py-0.5 px-1.5 w-16" type="number" placeholder="any" value={filterMaxDist} onChange={(e) => setFilterMaxDist(e.target.value)} />
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-secondary">Min pop:</span>
                  <input className="input-field text-xs py-0.5 px-1.5 w-16" type="number" placeholder="any" value={filterMinPop} onChange={(e) => setFilterMinPop(e.target.value)} />
                </div>
                <label className="flex items-center gap-1 cursor-pointer text-secondary select-none">
                  <input type="checkbox" className="checkbox-gold" checked={filterFullBooty} onChange={(e) => setFilterFullBooty(e.target.checked)} />
                  Full booty only
                </label>
                <span className="text-secondary ml-auto">{filteredSortedSlots.length}/{rawSlots.length} shown</span>
              </div>
            )}

            {/* Slot table */}
            {slots.length === 0 ? (
              <p className="text-secondary italic py-1">
                {rawSlots.length === 0 ? 'No targets in this list.' : 'No targets match your filters.'}
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th className="w-8">
                        <input
                          type="checkbox"
                          className="checkbox-gold"
                          checked={selectedSlotIds.size > 0 && selectedSlotIds.size === filteredSortedSlots.length}
                          onChange={() => selectedSlotIds.size === filteredSortedSlots.length ? deselectAllSlots() : selectAllSlots()}
                        />
                      </th>
                      <th>Coords</th>
                      <th className="cursor-pointer select-none" onClick={() => handleSort('name')}>Target{sortArrow('name')}</th>
                      <th className="text-center cursor-pointer select-none" onClick={() => handleSort('population')}>Pop{sortArrow('population')}</th>
                      <th className="text-center cursor-pointer select-none" onClick={() => handleSort('distance')}>Dist{sortArrow('distance')}</th>
                      <th>Troops</th>
                      <th className="text-center">Active</th>
                      <th>Last Raid</th>
                      <th className="text-right cursor-pointer select-none" onClick={() => handleSort('booty_ratio')}>Booty{sortArrow('booty_ratio')}</th>
                      <th className="text-right cursor-pointer select-none" onClick={() => handleSort('total_booty')}>Total{sortArrow('total_booty')}</th>
                      <th className="text-center cursor-pointer select-none" onClick={() => handleSort('total_raids')}>Raids{sortArrow('total_raids')}</th>
                      <th className="text-right">Defense</th>
                      <th className="text-center">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slots.map((slot, idx) => {
                      const lr = slot.last_raid ?? null
                      const lrIcon = lr?.icon ?? ''
                      const lrRes = lr?.resources
                      const lrCap = lr?.capacity
                      const lrTime = lr?.time
                      const slotId = slot.id ?? idx
                      const inactive = !slot.is_active
                      const bootyStr = lrRes != null && lrCap
                        ? `${lrRes}/${lrCap}`
                        : lrRes != null ? String(lrRes) : '---'
                      const isFull = lrRes != null && lrCap && lrRes >= lrCap
                      return (
                        <tr key={slotId} className={`${selectedSlotIds.has(slotId) ? 'row-selected' : ''} ${inactive ? 'opacity-50' : ''}`}>
                          <td onClick={(e) => e.stopPropagation()}>
                            <input type="checkbox" className="checkbox-gold" checked={selectedSlotIds.has(slotId)} onChange={() => toggleSlotSelection(slotId)} />
                          </td>
                          <td className="font-mono text-primary whitespace-nowrap">({slot.x ?? '?'},{slot.y ?? '?'})</td>
                          <td className="text-primary">{slot.name ?? '---'}</td>
                          <td className="text-center font-mono">{slot.population ?? '---'}</td>
                          <td className="text-center font-mono">{slot.distance != null ? slot.distance.toFixed(1) : '---'}</td>
                          <td className="text-secondary text-xs whitespace-nowrap">{formatTroops(slot.troops, slot.troop_total)}</td>
                          <td className="text-center">
                            {inactive
                              ? <span className="text-danger text-xs font-semibold">OFF</span>
                              : <span className="status-dot status-dot-success" />}
                          </td>
                          <td className="whitespace-nowrap">
                            <span className={raidIconClass(lrIcon)}>{raidIconLabel(lrIcon)}</span>
                            {lrTime != null && (
                              <div className="text-[10px] text-secondary leading-tight">{formatRaidTime(lrTime)}</div>
                            )}
                          </td>
                          <td className={`text-right font-mono text-xs ${isFull ? 'text-success font-bold' : 'text-gold'}`}>{bootyStr}</td>
                          <td className="text-right text-gold font-mono">{slot.total_booty != null ? slot.total_booty.toLocaleString() : '---'}</td>
                          <td className="text-center font-mono text-secondary">{slot.total_raids ?? 0}</td>
                          <td className="text-right text-xs whitespace-nowrap">
                            {(() => {
                              const def = defenseData[slotId]
                              if (defenseScanning) return <span className="text-secondary">...</span>
                              if (!def) return <span className="text-secondary">---</span>
                              if (def.defender_total === 0) return <span className="text-success">Empty</span>
                              return (
                                <span className="text-danger" title={Object.entries(def.defender_troops).filter(([,v]) => v > 0).map(([k,v]) => `${k}:${v}`).join(' ')}>
                                  {def.defender_total.toLocaleString()}
                                  {def.report_age_hours != null && <span className="text-secondary ml-1">({def.report_age_hours}h)</span>}
                                </span>
                              )
                            })()}
                          </td>
                          <td className="text-center">
                            <button className="btn-danger btn-xs" disabled={deletingTargetId === slotId} onClick={() => setDeleteTargetConfirm(slotId)}>
                              {deletingTargetId === slotId ? '...' : 'Del'}
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {!showAllSlots && filteredSortedSlots.length > SLOT_PAGE_SIZE && (
              <div className="mt-2 text-center">
                <button className="btn-secondary btn-xs" onClick={() => setShowAllSlots(true)}>
                  Show all {filteredSortedSlots.length} slots
                </button>
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

      {/* Delete target confirm dialog */}
      <ConfirmDialog
        open={deleteTargetConfirm != null}
        title="Delete Target"
        message="Are you sure you want to remove this target from the farm list?"
        confirmText="Delete"
        variant="danger"
        onConfirm={() => { if (deleteTargetConfirm != null) handleDeleteTarget(deleteTargetConfirm) }}
        onCancel={() => setDeleteTargetConfirm(null)}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
//  Helpers
// ---------------------------------------------------------------------------
function formatRaidTime(unixTs) {
  if (!unixTs) return ''
  const d = new Date(unixTs * 1000)
  const now = new Date()
  const diffH = Math.round((now - d) / 3600000)
  const time = d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
  if (diffH < 24) return `${time} (${diffH}h ago)`
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  return `${date} ${time}`
}

function formatTroops(troops, total) {
  if (!troops || typeof troops !== 'object') {
    return total != null && total > 0 ? `${total} total` : '---'
  }
  // troops is {"t1": 0, "t2": 50, ...}
  const parts = Object.entries(troops)
    .filter(([, v]) => v > 0)
    .map(([k, v]) => `${k}:${v}`)
  if (parts.length === 0) {
    return total != null && total > 0 ? `${total} total` : '---'
  }
  return parts.join(' ')
}
