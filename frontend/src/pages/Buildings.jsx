import { useEffect, useState, useRef, useCallback, memo } from 'react'
import useGameStore from '../stores/gameStore'
import { useToast } from '../components/Toast'
import ConfirmDialog from '../components/ConfirmDialog'
import FetchError from '../components/FetchError'
import api from '../api'

function formatTimeRemaining(seconds) {
  if (seconds == null || seconds <= 0) return '00:00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  return [h, m, s].map((v) => String(v).padStart(2, '0')).join(':')
}

function getBuildingCategory(slotId, building) {
  if (!building || !building.name || building.name === 'Empty' || building.level === 0) {
    return 'empty'
  }
  if (slotId >= 1 && slotId <= 18) return 'resource'
  const militaryBuildings = [
    'Barracks', 'Stable', 'Workshop', 'Academy', 'Smithy',
    'Rally Point', 'Wall', 'Earth Wall', 'Palisade', 'City Wall',
    'Great Barracks', 'Great Stable', 'Horse Drinking Trough',
    'Tournament Square', 'Trapper',
  ]
  const name = building.name || ''
  if (militaryBuildings.some((mb) => name.toLowerCase().includes(mb.toLowerCase()))) {
    return 'military'
  }
  return 'infrastructure'
}

const CATEGORY_TO_CLASS = {
  resource: 'btype-resource',
  military: 'btype-military',
  infrastructure: 'btype-infra',
  empty: 'btype-empty',
}

/* The queue half of this page. `error` is rendered by the SAME card as the
   queue itself, under the same heading, because this panel returns null for
   an empty queue and a failed read left the queue empty -- so the whole
   section vanished, heading and all, with nothing on screen saying the read
   had even been attempted. Worse than its own empty state, and the one half
   of this page the census found unhandled: the building LIST beside it has
   had `buildingsError` + Retry all along. */
const ConstructionQueuePanel = memo(function ConstructionQueuePanel({ queue, error, onRetry }) {
  const [snapTime, setSnapTime] = useState(Date.now)
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!queue || queue.length === 0) return
    setSnapTime(Date.now()); setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [queue])

  if (!error && (!queue || queue.length === 0)) return null
  const elapsed = Math.floor((now - snapTime) / 1000)

  return (
    <div className="card p-4 mb-4">
      <h3 className="heading-gold text-base mb-3 flex items-center gap-2">
        {'\uD83D\uDD28'} Construction Queue
      </h3>
      {error ? (
        <FetchError
          what="Could not read the construction queue"
          detail={error}
          onRetry={onRetry}
        />
      ) : (
        <div className="flex flex-col gap-2">
          {queue.map((item, idx) => {
            const baseRemaining = item.remaining_seconds ?? item.time_remaining ?? item.seconds_remaining
            const remaining = Math.max(0, (baseRemaining || 0) - elapsed)
            const doneAt = new Date(Date.now() + remaining * 1000)
            const doneStr = doneAt.toLocaleTimeString('en-US', { hour12: false })
            return (
              <div key={item.event_id ?? `${item.building_name}-${idx}`} className="surface-row">
                <span className="text-sm text-primary">
                  {item.building_name || item.name || 'Building'} {'\u2192'} Level{' '}
                  {item.target_level ?? item.level ?? '?'}
                </span>
                <span className="text-xs text-warning font-mono">
                  {formatTimeRemaining(remaining)}
                  <span className="text-xs text-secondary ml-2">Done at {doneStr}</span>
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
})

function BuildingDetailPanel({
  selectedSlot,
  detail,
  loading,
  onUpgrade,
  onConstruct,
  onClose,
  queueOccupied,
}) {
  const [selectedNewBuilding, setSelectedNewBuilding] = useState('')

  if (!selectedSlot) return null

  const isEmptySlot =
    detail &&
    (detail.available_buildings ||
      !detail.name ||
      detail.name === 'Empty' ||
      detail.level === 0)

  return (
    <div className="card p-4 mb-4 border-gold">
      <div className="flex justify-between items-center mb-3">
        <h3 className="heading-gold text-base">
          Slot {selectedSlot} {detail ? `- ${detail.name || 'Empty'}` : ''}
        </h3>
        <button className="btn-close" onClick={onClose} aria-label={`Close slot ${selectedSlot} details`}>
          {'\u2715'}
        </button>
      </div>

      {loading && (
        <p className="text-secondary text-sm">Loading details...</p>
      )}

      {!loading && detail && !isEmptySlot && (
        <div>
          <div className="mb-3">
            <span className="text-xs text-secondary">
              Current Level: {detail.level ?? '?'}
            </span>
          </div>
          {detail.upgrade_cost && (
            <div className="mb-3 p-2 bg-surface rounded-md text-xs text-secondary">
              <div className="mb-1 font-semibold">Upgrade Cost:</div>
              <div className="flex gap-3 flex-wrap">
                {detail.upgrade_cost.lumber != null && (
                  <span>{'\uD83E\uDEB5'} {detail.upgrade_cost.lumber}</span>
                )}
                {detail.upgrade_cost.clay != null && (
                  <span>{'\uD83E\uDDF1'} {detail.upgrade_cost.clay}</span>
                )}
                {detail.upgrade_cost.iron != null && (
                  <span>{'\u26CF\uFE0F'} {detail.upgrade_cost.iron}</span>
                )}
                {detail.upgrade_cost.crop != null && (
                  <span>{'\uD83C\uDF3E'} {detail.upgrade_cost.crop}</span>
                )}
              </div>
            </div>
          )}
          {queueOccupied && (
            <p className="text-xs text-warning mb-2">
              {'\u26A0'} Queue is occupied. Upgrading may use gold to start immediately.
            </p>
          )}
          <button className="btn-primary" onClick={() => onUpgrade(selectedSlot)}>
            Upgrade to Level {(detail.level ?? 0) + 1}
          </button>
        </div>
      )}

      {!loading && detail && isEmptySlot && (
        <div>
          <p className="text-xs text-secondary mb-3">
            This slot is empty. Choose a building to construct:
          </p>
          {detail.available_buildings && detail.available_buildings.length > 0 ? (
            <>
              <select
                className="input-field mb-3"
                value={selectedNewBuilding}
                onChange={(e) => setSelectedNewBuilding(e.target.value)}
              >
                <option value="">-- Select Building --</option>
                {detail.available_buildings.map((b) => {
                  const name = typeof b === 'string' ? b : b.name || b
                  return (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  )
                })}
              </select>
              {queueOccupied && (
                <p className="text-xs text-warning mb-2">
                  {'\u26A0'} Queue is occupied. Constructing may use gold.
                </p>
              )}
              <button
                className="btn-primary"
                disabled={!selectedNewBuilding}
                onClick={() => onConstruct(selectedSlot, selectedNewBuilding)}
              >
                Construct
              </button>
            </>
          ) : (
            <p className="text-xs text-secondary">
              No buildings available for this slot.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function formatMarkdown(data) {
  const tribeName = { 1: 'Romans', 2: 'Teutons', 3: 'Gauls' }[data.tribe_id] || 'Unknown'
  const lines = [
    `# Player Status: ${data.player_name}`,
    `**Tribe:** ${tribeName}  `,
    `**Exported:** ${new Date().toLocaleString()}`,
    '',
  ]

  for (const v of data.villages) {
    lines.push(`---`, '', `## ${v.name} (${v.x}|${v.y})`, '')

    if (v.error) {
      lines.push(`> Failed to fetch data: ${v.error}`, '')
      continue
    }

    // Buildings
    const built = (v.buildings || []).filter((b) => b.name && b.name !== 'Empty' && b.level > 0)
    if (built.length > 0) {
      lines.push('### Buildings', '', '| Slot | Building | Level |', '|------|----------|-------|')
      for (const b of built) {
        lines.push(`| ${b.slot_id} | ${b.name} | ${b.level} |`)
      }
      lines.push('')
    }

    // Troops
    const troops = v.troops || {}
    const troopEntries = Object.entries(troops)
    if (troopEntries.length > 0) {
      lines.push('### Troops', '', '| Unit | Count |', '|------|-------|')
      for (const [name, count] of troopEntries) {
        lines.push(`| ${name} | ${count.toLocaleString()} |`)
      }
      lines.push('')
    } else {
      lines.push('### Troops', '', '*No troops at rally point*', '')
    }

    // Crop balance first — a starving village is the most actionable fact here.
    // `crop.net_per_hour` comes from production.l4, the only true net rate.
    const crop = v.crop
    if (crop?.starving) {
      lines.push(
        `> ⚠️ **STARVING** — net crop ${crop.net_per_hour.toLocaleString()}/h` +
          (crop.hours_until_empty != null
            ? `, granary empty in ${crop.hours_until_empty}h`
            : ''),
        '',
      )
    }

    // Production
    const r = v.resources || {}
    lines.push(
      '### Production',
      '',
      '| Resource | Per Hour |',
      '|----------|----------|',
      `| Lumber | ${(r.lumber_per_hour || 0).toLocaleString()} |`,
      `| Clay | ${(r.clay_per_hour || 0).toLocaleString()} |`,
      `| Iron | ${(r.iron_per_hour || 0).toLocaleString()} |`,
      `| Crop (net of feeding) | ${(r.crop_per_hour || 0).toLocaleString()} |`,
      '',
    )

    // Resources
    lines.push(
      '### Resources',
      '',
      '| Resource | Current | Max |',
      '|----------|---------|-----|',
      `| Lumber | ${(r.lumber || 0).toLocaleString()} | ${(r.max_lumber || 0).toLocaleString()} |`,
      `| Clay | ${(r.clay || 0).toLocaleString()} | ${(r.max_clay || 0).toLocaleString()} |`,
      `| Iron | ${(r.iron || 0).toLocaleString()} | ${(r.max_iron || 0).toLocaleString()} |`,
      `| Crop | ${(r.crop || 0).toLocaleString()} | ${(r.max_crop || 0).toLocaleString()} |`,
      '',
    )
  }

  return lines.join('\n')
}

function downloadFile(filename, content) {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export default function Buildings() {
  const buildings = useGameStore((s) => s.buildings)
  const buildingsLoading = useGameStore((s) => s.buildingsLoading)
  const buildingsError = useGameStore((s) => s.buildingsError)
  const constructionQueue = useGameStore((s) => s.constructionQueue)
  const queueError = useGameStore((s) => s.queueError)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const fetchBuildings = useGameStore((s) => s.fetchBuildings)
  const fetchQueue = useGameStore((s) => s.fetchQueue)
  const toast = useToast()

  const [selectedSlot, setSelectedSlot] = useState(null)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pendingAction, setPendingAction] = useState(null)
  const [actionLoading, setActionLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [includeBuildings, setIncludeBuildings] = useState(false)
  const fetchingSlotRef = useRef(null)

  useEffect(() => {
    fetchBuildings()
    fetchQueue()
    setSelectedSlot(null)
    setDetail(null)
  }, [fetchBuildings, fetchQueue, activeVillageId])

  const fetchDetail = useCallback(async (slotId) => {
    // Key the in-flight request by slot AND village: the same slot id exists in
    // every village, so a slower response from the previous village must not
    // repopulate the sidebar after a switch.
    const token = `${slotId}::${activeVillageId ?? ''}`
    fetchingSlotRef.current = token
    setDetailLoading(true)
    try {
      // The backend's session default is forever the login village (switching
      // is client-side only), so the selected village must travel explicitly.
      const res = await api.get(`/buildings/${slotId}`, {
        params: activeVillageId != null ? { village_id: activeVillageId } : {},
      })
      if (fetchingSlotRef.current === token) {
        setDetail(res.data)
      }
    } catch {
      if (fetchingSlotRef.current === token) {
        toast.error('Failed to load building details')
        setDetail(null)
      }
    } finally {
      if (fetchingSlotRef.current === token) {
        setDetailLoading(false)
      }
    }
  }, [toast, activeVillageId])

  const handleSlotClick = (slotId) => {
    if (selectedSlot === slotId) {
      setSelectedSlot(null)
      setDetail(null)
      return
    }
    setSelectedSlot(slotId)
    fetchDetail(slotId)
  }

  const handleUpgrade = (slotId) => {
    setPendingAction({ type: 'upgrade', slotId })
    setConfirmOpen(true)
  }

  const handleConstruct = (slotId, buildingName) => {
    setPendingAction({ type: 'construct', slotId, buildingName })
    setConfirmOpen(true)
  }

  const handleConfirm = async () => {
    setConfirmOpen(false)
    if (!pendingAction) return

    setActionLoading(true)
    try {
      if (pendingAction.type === 'upgrade') {
        await api.post('/buildings/upgrade', {
          slot_id: pendingAction.slotId,
          allow_gold: false,
          village_id: activeVillageId ?? undefined,
        })
        toast.success('Upgrade started!')
      } else if (pendingAction.type === 'construct') {
        await api.post('/buildings/construct', {
          slot_id: pendingAction.slotId,
          building_name: pendingAction.buildingName,
          village_id: activeVillageId ?? undefined,
        })
        toast.success('Construction started!')
      }
      await useGameStore.getState().refreshVillageData()
      if (selectedSlot) {
        await fetchDetail(selectedSlot)
      }
    } catch (err) {
      const msg =
        err.response?.data?.detail || err.response?.data?.message || 'Action failed'
      toast.error(typeof msg === 'string' ? msg : 'Action failed')
    } finally {
      setActionLoading(false)
      setPendingAction(null)
    }
  }

  const handleCancelConfirm = () => {
    setConfirmOpen(false)
    setPendingAction(null)
  }

  const handleExportStatus = async () => {
    setExporting(true)
    try {
      // Building levels are the only per-village fetch (dorf1 + dorf2 each) and
      // run through the stealth throttler, so a large account takes minutes --
      // far past the client's default 120s. Opt out of the timeout only then.
      const res = await api.get('/status/export', {
        params: { include_buildings: includeBuildings },
        timeout: includeBuildings ? 0 : 120000,
      })
      const md = formatMarkdown(res.data)
      const name = (res.data.player_name || 'player').replace(/\s+/g, '_')
      downloadFile(`${name}_status.md`, md)
      toast.success('Player status downloaded')
    } catch (err) {
      const msg = err.response?.data?.detail || 'Failed to export status'
      toast.error(typeof msg === 'string' ? msg : 'Export failed')
    } finally {
      setExporting(false)
    }
  }

  const queueOccupied = constructionQueue && constructionQueue.length > 0

  const buildingList = Array.isArray(buildings) ? buildings : []

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Buildings</h2>
        <div className="flex items-center gap-3">
          <label
            className="flex items-center gap-1.5 text-xs text-secondary cursor-pointer"
            title="Building levels need two extra requests per village and take minutes on a large account"
          >
            <input
              type="checkbox"
              checked={includeBuildings}
              onChange={(e) => setIncludeBuildings(e.target.checked)}
              disabled={exporting}
            />
            Include building levels (slow)
          </label>
          <button
            className="btn-secondary btn-sm"
            onClick={handleExportStatus}
            disabled={exporting}
          >
            {exporting ? 'Exporting...' : 'Download Player Status'}
          </button>
          {/* No <VillageSelector/> here. The page used to embed its own, which
              duplicated the layout's -- same store action, same "Active village"
              name, so two comboboxes with that exact name were visible at once at
              every width. Same fix AutoScout took in 5a62dd1; the
              sidebar/mobile-top-bar selector already covers every breakpoint (see
              components/Layout.jsx). */}
        </div>
      </div>

      <ConstructionQueuePanel queue={constructionQueue} error={queueError} onRetry={fetchQueue} />

      <BuildingDetailPanel
        selectedSlot={selectedSlot}
        detail={detail}
        loading={detailLoading}
        onUpgrade={handleUpgrade}
        onConstruct={handleConstruct}
        onClose={() => {
          setSelectedSlot(null)
          setDetail(null)
        }}
        queueOccupied={queueOccupied}
      />

      <div className="card p-4">
        <h3 className="heading-gold text-base mb-3">
          Building Slots
        </h3>
        {buildingsLoading ? (
          <div className="flex items-center gap-3 py-4">
            <div className="spinner spinner-sm" />
            <p className="text-secondary text-sm">Loading buildings...</p>
          </div>
        ) : buildingsError ? (
          <div className="py-4">
            <p className="text-warning text-sm mb-2">{buildingsError}</p>
            <button className="btn-secondary btn-sm" onClick={fetchBuildings}>Retry</button>
          </div>
        ) : buildingList.length === 0 ? (
          <p className="text-secondary text-sm">
            No building data available. Make sure you are connected.
          </p>
        ) : (
          <div className="flex flex-col gap-1">
            {buildingList.map((b) => {
              const slotId = b.slot_id ?? b.slot ?? b.id
              const category = getBuildingCategory(slotId, b)
              const categoryClass = CATEGORY_TO_CLASS[category] || ''
              const isSelected = selectedSlot === slotId
              const isEmpty = category === 'empty'
              const isUpgrading = constructionQueue?.some(
                (q) => (q.slot_id ?? q.slotId) === slotId
              )

              return (
                <button
                  key={slotId}
                  onClick={() => handleSlotClick(slotId)}
                  className={`building-slot ${categoryClass}${isSelected ? ' row-selected' : ''}${isUpgrading ? ' border-gold' : ''}`}
                >
                  <span className="text-xs text-secondary min-w-8 text-right">
                    #{slotId}
                  </span>
                  <span
                    className={`flex-1 text-sm ${isEmpty ? 'italic text-secondary' : 'font-medium text-primary'}`}
                  >
                    {isEmpty ? 'Empty Slot' : b.name || 'Unknown'}
                  </span>
                  <span className="text-xs text-secondary min-w-14 text-right">
                    {isEmpty ? '---' : `Lv ${b.level ?? '?'}`}
                  </span>
                </button>
              )
            })}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title={
          pendingAction?.type === 'upgrade'
            ? 'Confirm Upgrade'
            : 'Confirm Construction'
        }
        message={
          pendingAction?.type === 'upgrade'
            ? `Upgrade building in slot ${pendingAction?.slotId} to the next level?`
            : `Construct ${pendingAction?.buildingName} in slot ${pendingAction?.slotId}?`
        }
        confirmText={pendingAction?.type === 'upgrade' ? 'Upgrade' : 'Construct'}
        onConfirm={handleConfirm}
        onCancel={handleCancelConfirm}
      />

      {actionLoading && (
        <div className="loading-overlay">
          <div className="card px-8 py-6 text-secondary text-sm">
            Processing...
          </div>
        </div>
      )}
    </div>
  )
}
