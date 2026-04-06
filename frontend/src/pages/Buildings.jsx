import { useEffect, useState, useRef, useCallback } from 'react'
import useGameStore from '../stores/gameStore'
import { useToast } from '../components/Toast'
import ConfirmDialog from '../components/ConfirmDialog'
import VillageSelector from '../components/VillageSelector'
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

function ConstructionQueuePanel({ queue }) {
  if (!queue || queue.length === 0) return null

  return (
    <div className="card p-4 mb-4">
      <h3 className="heading-gold text-base mb-3 flex items-center gap-2">
        {'\uD83D\uDD28'} Construction Queue
      </h3>
      <div className="flex flex-col gap-2">
        {queue.map((item, idx) => (
          <div key={item.event_id ?? `${item.building_name}-${idx}`} className="surface-row">
            <span className="text-sm text-primary">
              {item.building_name || item.name || 'Building'} {'\u2192'} Level{' '}
              {item.target_level ?? item.level ?? '?'}
            </span>
            <span className="text-xs text-warning font-mono">
              {formatTimeRemaining(item.time_remaining ?? item.seconds_remaining)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

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
        <button className="btn-close" onClick={onClose}>
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

export default function Buildings() {
  const buildings = useGameStore((s) => s.buildings)
  const constructionQueue = useGameStore((s) => s.constructionQueue)
  const fetchBuildings = useGameStore((s) => s.fetchBuildings)
  const fetchQueue = useGameStore((s) => s.fetchQueue)
  const toast = useToast()

  const [selectedSlot, setSelectedSlot] = useState(null)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pendingAction, setPendingAction] = useState(null)
  const [actionLoading, setActionLoading] = useState(false)

  useEffect(() => {
    fetchBuildings()
    fetchQueue()
  }, [fetchBuildings, fetchQueue])

  const fetchDetail = useCallback(async (slotId) => {
    setDetailLoading(true)
    try {
      const res = await api.get(`/buildings/${slotId}`)
      setDetail(res.data)
    } catch (err) {
      toast.error('Failed to load building details')
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

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
        })
        toast.success('Upgrade started!')
      } else if (pendingAction.type === 'construct') {
        await api.post('/buildings/construct', {
          slot_id: pendingAction.slotId,
          building_name: pendingAction.buildingName,
        })
        toast.success('Construction started!')
      }
      await Promise.all([fetchBuildings(), fetchQueue()])
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

  const queueOccupied = constructionQueue && constructionQueue.length > 0

  const buildingList = Array.isArray(buildings) ? buildings : []

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Buildings</h2>
        <VillageSelector />
      </div>

      <ConstructionQueuePanel queue={constructionQueue} />

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
        {buildingList.length === 0 ? (
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

              return (
                <button
                  key={slotId}
                  onClick={() => handleSlotClick(slotId)}
                  className={`building-slot ${categoryClass}${isSelected ? ' row-selected' : ''}`}
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
                    {isEmpty ? '---' : `Lvl ${b.level ?? '?'}`}
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
