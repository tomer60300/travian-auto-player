import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import api from '../api'
import { useResumableOperation } from '../hooks/useResumableOperation'
import { useToast } from '../components/Toast'
import WebSocketPanel from '../components/WebSocketPanel'
import ConfirmDialog from '../components/ConfirmDialog'
import { MapCoord } from '../components/MapCoord'
import useGameStore from '../stores/gameStore'
import FetchError from '../components/FetchError'
import SkeletonRows from '../components/SkeletonRows'
import { readErrorDetail } from '../utils/fetchError'

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
    case 'result': {
      // Single-list result: { slot_id, success, status, error }
      // Multi-list result: { list_id, success, fail, targets: [{slot_id, success, status, error}] }
      const messages = []
      if (data.targets && Array.isArray(data.targets)) {
        // run-all: per-list with target details
        const listLabel = data.list_id ? `[List #${data.list_id}] ` : ''
        for (const t of data.targets) {
          const ok = t.success
          messages.push({
            ...base, id: ++_farmMsgId,
            type: ok ? 'success' : 'error',
            text: `${listLabel}Slot #${t.slot_id}: ${ok ? 'Sent' : t.error || t.status || 'Failed'}`,
          })
        }
        if (messages.length === 0) {
          messages.push({ ...base, type: 'info', text: `${listLabel}sent: ${data.success ?? 0}, failed: ${data.fail ?? 0}` })
        }
      } else {
        // single-list: per-target
        const ok = data.success
        messages.push({
          ...base,
          type: ok ? 'success' : 'error',
          text: `Slot #${data.slot_id ?? '?'}: ${ok ? (data.status || 'Sent') : (data.error || data.status || 'Failed')}`,
        })
      }
      return messages
    }
    case 'cycle_end':
      return { ...base, type: 'info', text: `Cycle ${data.cycle} done - sent: ${data.sent ?? 0}, failed: ${data.failed ?? 0}${data.next_send_at ? ' | next: ' + data.next_send_at : ''}` }
    case 'session_init':
      return { ...base, type: 'info', text: `Session: ${data.session_id} (viewable from /sessions)` }
    case 'error':
      return { ...base, type: 'error', text: data.message || 'Unknown error' }
    case 'complete':
      return { ...base, type: 'success', text: `Completed after ${data.total_cycles ?? '?'} cycle(s) - total sent: ${data.total_success ?? '?'}, failed: ${data.total_fail ?? '?'}` }
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
  const [listsError, setListsError] = useState(null)
  const [selectedListId, setSelectedListId] = useState(null)

  // ---- New list form ----
  const [newListName, setNewListName] = useState('')
  const [newListVillage, setNewListVillage] = useState('')
  const [creating, setCreating] = useState(false)

  // ---- Detail ----
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(null)

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

  // ---- Defense scan (streaming) ----
  const [defenseData, setDefenseData] = useState({})
  const [defenseScanning, setDefenseScanning] = useState(false)
  const [defenseScanProgress, setDefenseScanProgress] = useState(null) // {total, cached, to_fetch, fetched}
  const [defenseScanLogs, setDefenseScanLogs] = useState([])

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
  const mountedRef = useRef(true)
  // Set on mount as well as cleared on unmount. React re-runs an effect's
  // cleanup and body once on mount in development (StrictMode), and a
  // cleanup-only version left this ref stuck at `false` from the first
  // teardown onwards -- so the loop-mode message handler's
  // `if (!mountedRef.current)` guard dropped EVERY frame the operation sent.
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // -----------------------------------------------------------------
  //  Fetch all lists
  // -----------------------------------------------------------------
  const fetchLists = useCallback(async () => {
    try {
      setLoading(true)
      const res = await api.get('/farm/lists')
      setLists(Array.isArray(res.data) ? res.data : [])
      setListsError(null)
    } catch (err) {
      // The toast was the ONLY signal, and 4.5s later the page read exactly
      // like "No farm lists found. Create one above." -- which invites the
      // operator to build a list they may already have. A 403 stays quiet
      // because the layout is already redirecting to /connect for it.
      if (err.response?.status !== 403) {
        const message = readErrorDetail(err, 'Failed to load farm lists')
        setListsError(message)
        toast.error(message)
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
      setDetailError(null)
      setShowAllSlots(false)
      return
    }
    setShowAllSlots(false)
    let cancelled = false
    ;(async () => {
      try {
        setDetailLoading(true)
        const res = await api.get(`/farm/lists/${selectedListId}`)
        if (!cancelled) { setDetail(res.data); setDetailError(null) }
      } catch (err) {
        if (!cancelled) {
          const message = readErrorDetail(err, 'Failed to load list detail')
          setDetailError(message)
          toast.error(message)
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
    const addedSlots = []
    let addFail = 0
    let addFailReason = ''

    // 1. Add each selected target to destination list
    for (const slot of slotsToTransfer) {
      try {
        await api.post(`/farm/lists/${destListId}/targets`, {
          x: slot.x,
          y: slot.y,
          force: true,
        })
        addedSlots.push(slot)
      } catch (err) {
        addFail++
        addFailReason = err.response?.data?.detail || addFailReason || 'unknown error'
      }
    }

    // 2. If "move", delete from source only the slots whose add the
    // destination confirmed. A slot whose add failed (e.g. the destination
    // is at its slot cap) must stay in the source -- deleting it here would
    // destroy the target with no copy left anywhere.
    let delOk = 0
    if (transferMode === 'move' && addedSlots.length > 0) {
      for (const slot of addedSlots) {
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
    } catch { /* empty */ }
    await fetchLists()

    setSelectedSlotIds(new Set())
    setTransferring(false)

    const destName = lists.find((l) => l.id === destListId)?.name || `#${destListId}`
    if (transferMode === 'move') {
      if (addFail > 0) {
        const sourceName = lists.find((l) => l.id === selectedListId)?.name || `#${selectedListId}`
        toast.warning(`${delOk} moved; ${addFail} left in "${sourceName}" because "${destName}" refused them: ${addFailReason}`)
      } else {
        toast.success(`Moved ${delOk} target(s) to "${destName}"`)
      }
    } else if (addFail > 0) {
      toast.warning(`Copied ${addedSlots.length} target(s) to "${destName}"; ${addFail} refused: ${addFailReason}`)
    } else {
      toast.success(`Copied ${addedSlots.length} target(s) to "${destName}"`)
    }
  }

  // -----------------------------------------------------------------
  //  Background defense scan
  // -----------------------------------------------------------------
  const handleDefenseScan = async () => {
    if (!selectedListId) return
    setDefenseScanning(true)
    setDefenseScanProgress(null)
    setDefenseScanLogs([])

    const token = localStorage.getItem('token')
    try {
      const resp = await fetch('/api/farm/defense-scan', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
        },
        body: JSON.stringify({ list_id: selectedListId }),
      })

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}))
        throw new Error(err.detail || `HTTP ${resp.status}`)
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        const lines = buffer.split('\n')
        buffer = lines.pop() // keep incomplete line in buffer

        for (const line of lines) {
          if (!line.trim()) continue
          try {
            const msg = JSON.parse(line)
            if (msg.type === 'result') {
              setDefenseData((prev) => ({ ...prev, [msg.slot_id]: msg }))
            } else if (msg.type === 'progress') {
              setDefenseScanProgress(msg)
            } else if (msg.type === 'log') {
              setDefenseScanLogs((prev) => [...prev.slice(-19), msg.message])
            } else if (msg.type === 'complete') {
              toast.success(`Defense scan: ${msg.total} targets, ${msg.fetched} fetched in ${msg.elapsed}s`)
            } else if (msg.type === 'error') {
              toast.error(msg.message)
            }
          } catch { /* skip malformed lines */ }
        }
      }
    } catch (err) {
      toast.error(err.message || 'Defense scan failed')
    } finally {
      setDefenseScanning(false)
      setDefenseScanProgress(null)
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

  // ── Resumable hook: server-side op survives Safari background, page
  // reload, bfcache. Status comes from the hook + per-message updates.
  const handleOpMessage = useCallback((data) => {
    if (!mountedRef.current || !data) return
    const msg = transformWsMessage(data)
    setWsMessages((prev) => [...prev, ...(Array.isArray(msg) ? msg : [msg])])
    if (data.type === 'complete') {
      setLoopRunning(false)
      setWsStatus('disconnected')
      toast.success('Loop completed')
    }
    if (data.type === 'already_running') {
      setLoopRunning(true)
    }
  }, [toast])

  const handleStatusChange = useCallback((next) => {
    if (next === 'connecting') setWsStatus('connecting')
    else if (next === 'reconnecting') setWsStatus('reconnecting')
    else if (next === 'running') setWsStatus('running')
    else if (next === 'completed' || next === 'stopped' || next === 'failed') {
      setLoopRunning(false)
      setWsStatus('disconnected')
    }
  }, [])

  const farmAllOp = useResumableOperation('farm-all', {
    onMessage: handleOpMessage,
    onStatusChange: handleStatusChange,
  })

  // If there's a stored session_id at mount, surface that we're rejoining.
  useEffect(() => {
    if (farmAllOp.sessionId && wsMessages.length === 0) {
      setLoopRunning(true)
      setWsStatus('reconnecting')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [farmAllOp.sessionId])

  const startLoop = () => {
    if (loopListIds.length === 0) {
      toast.warning('Select at least one list')
      return
    }
    setWsMessages([])
    setLoopRunning(true)
    setWsStatus('connecting')
    const qs = `interval=${loopInterval}&duration=${loopDuration}&list_ids=${loopListIds.join(',')}`
    farmAllOp.start(`/ws/farm/run-all?${qs}`, {})
  }

  const stopLoop = () => {
    farmAllOp.stop()
    // Server emits operation_complete with status="stopped"; status flips
    // when that arrives, but flip locally too for snappier feedback.
    setLoopRunning(false)
    setWsStatus('disconnected')
  }

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
              <label className="field-label" htmlFor="new-list-village">Village</label>
              <select
                id="new-list-village"
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
            // A header row plus six list rows at `.data-table`'s own row
            // height. The one-line "Loading..." it replaces reserved nothing,
            // so the detail panel and the loop controls under it jumped by
            // ~180px at 768 when the lists landed.
            <SkeletonRows rows={7} height={38} gap={2} label="Loading farm lists" />
          ) : listsError ? (
            <FetchError
              what="Could not read your farm lists"
              detail={listsError}
              onRetry={fetchLists}
            />
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
                          <td className="text-right text-gold font-mono">
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
        {selectedListId && detailError && (
          <div className="card">
            <FetchError
              what="Could not read this farm list"
              detail={detailError}
              onRetry={() => { setDetailError(null); setSelectedListId(selectedListId) }}
            />
          </div>
        )}

        {selectedListId && !detailError && (
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
              <div className="flex items-center gap-2">
                <button
                  className="btn-secondary btn-xs flex items-center gap-1.5"
                  disabled={defenseScanning || !detail}
                  onClick={handleDefenseScan}
                  title="Scan recent reports for defender troop info on targets"
                >
                  {defenseScanning && <span className="spinner spinner-sm" />}
                  {defenseScanning ? 'Scanning...' : 'Scan Defense'}
                </button>
                {defenseScanProgress && (
                  <span className="text-xs text-secondary">
                    {defenseScanProgress.fetched}/{defenseScanProgress.to_fetch} fetched
                    {defenseScanProgress.cached > 0 && ` (${defenseScanProgress.cached} cached)`}
                  </span>
                )}
              </div>
            </div>

            {/* Add Target form */}
            <div className="flex gap-2 items-end flex-wrap mb-4 p-3 bg-surface rounded-md border-default">
              <div className="w-20 shrink-0">
                <label className="field-label" htmlFor="target-x">X</label>
                <input
                  id="target-x"
                  className="input-field"
                  type="number"
                  value={targetX}
                  onChange={(e) => setTargetX(e.target.value)}
                  placeholder="0"
                />
              </div>
              <div className="w-20 shrink-0">
                <label className="field-label" htmlFor="target-y">Y</label>
                <input
                  id="target-y"
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
            {filteredSortedSlots.length > 0 && (
              <div className="flex gap-2 items-center flex-wrap mb-3 p-2.5 bg-surface rounded-md border-default">
                <button className="btn-secondary btn-xs" onClick={selectAllSlots}>
                  Select All ({filteredSortedSlots.length})
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

            {/* Defense scan log */}
            {defenseScanLogs.length > 0 && (
              <div className="mb-3 p-2.5 bg-base rounded-md border-default text-xs font-mono max-h-32 overflow-y-auto">
                {defenseScanLogs.map((msg, i) => (
                  <div key={i} className="text-secondary leading-relaxed">{msg}</div>
                ))}
              </div>
            )}

            {/* Filter bar */}
            {rawSlots.length > 0 && (
              <div className="flex gap-3 items-center flex-wrap mb-3 p-2.5 bg-surface rounded-md border-default text-xs">
                <div className="flex items-center gap-1.5">
                  <span className="text-secondary">Status:</span>
                  {/* Same `<span>`-names-nothing markup as the two boxes
                      below, and named the same way: the visible word verbatim,
                      so WCAG 2.5.3 Label in Name holds. */}
                  <select aria-label="Status" className="input-field text-xs py-0.5 px-1.5 w-auto" value={filterActive} onChange={(e) => setFilterActive(e.target.value)}>
                    <option value="all">All</option>
                    <option value="active">Active only</option>
                    <option value="inactive">Inactive only</option>
                  </select>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-secondary">Max dist:</span>
                  {/* The visible text is a `<span>`, so it names nothing: no
                      `htmlFor`, no wrapping `<label>`. WCAG 4.1.2 -- and the
                      accessible name repeats the visible one rather than
                      spelling it out ("Maximum distance"), because 2.5.3
                      Label in Name wants the on-screen text inside the
                      accessible name. */}
                  <input className="input-field text-xs py-0.5 px-1.5 w-20" type="number" aria-label="Max dist" placeholder="any" value={filterMaxDist} onChange={(e) => setFilterMaxDist(e.target.value)} />
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-secondary">Min pop:</span>
                  <input className="input-field text-xs py-0.5 px-1.5 w-20" type="number" aria-label="Min pop" placeholder="any" value={filterMinPop} onChange={(e) => setFilterMinPop(e.target.value)} />
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
                          aria-label="Select all targets"
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
                            <input
                              type="checkbox"
                              className="checkbox-gold"
                              aria-label={`Select target (${slot.x}, ${slot.y})`}
                              checked={selectedSlotIds.has(slotId)}
                              onChange={() => toggleSlotSelection(slotId)}
                            />
                          </td>
                          <td className="font-mono text-primary whitespace-nowrap"><MapCoord x={slot.x} y={slot.y} separator="," /></td>
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
                          <td className="text-center font-mono text-secondary">{(slot.total_raids || 0) > 0 ? slot.total_raids : slot.last_raid ? '1+' : 0}</td>
                          <td className="text-right text-xs whitespace-nowrap">
                            {(() => {
                              const def = defenseData[slotId]
                              if (defenseScanning) return <span className="text-secondary">...</span>
                              if (!def) return <span className="text-secondary">---</span>
                              if (def.never_raided) return <span className="text-secondary">N/A</span>
                              if (def.defender_combat_strength === 0 && def.defender_total === 0)
                                return <span className="text-success">Empty{def.report_age_hours != null && <span className="text-secondary ml-1">({def.report_age_hours}h)</span>}</span>
                              const strength = def.defender_combat_strength || def.defender_total
                              const troopTip = Object.entries(def.defender_troops || {}).filter(([,v]) => v > 0).map(([k,v]) => `${k}:${v}`).join(' ')
                              const tip = troopTip
                                ? `Troops: ${troopTip}\nCombat Str: ${def.defender_combat_strength}`
                                : `Combat Str: ${def.defender_combat_strength}`
                              return (
                                <span className="text-danger" title={tip}>
                                  {strength.toLocaleString()}
                                  {def.report_age_hours != null && <span className="text-secondary ml-1">({def.report_age_hours}h)</span>}
                                </span>
                              )
                            })()}
                          </td>
                          <td className="text-center">
                            <button
                              className="btn-danger btn-xs"
                              disabled={deletingTargetId === slotId}
                              onClick={() => setDeleteTargetConfirm(slotId)}
                              aria-label={`Delete target (${slot.x}, ${slot.y})`}
                            >
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
              <label className="field-label" htmlFor="loop-interval">Interval (seconds)</label>
              <input
                id="loop-interval"
                className="input-field"
                type="number"
                min="10"
                value={loopInterval}
                onChange={(e) => setLoopInterval(Number(e.target.value) || 10)}
                disabled={loopRunning}
              />
            </div>
            <div className="w-[140px] shrink-0">
              <label className="field-label" htmlFor="loop-duration">Duration (min, 0=forever)</label>
              <input
                id="loop-duration"
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
