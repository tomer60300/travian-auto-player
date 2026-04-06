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

function getCategoryStyle(category) {
  switch (category) {
    case 'resource':
      return {
        borderLeft: '3px solid var(--success)',
        backgroundColor: 'rgba(74, 140, 74, 0.06)',
      }
    case 'military':
      return {
        borderLeft: '3px solid var(--danger)',
        backgroundColor: 'rgba(179, 64, 64, 0.06)',
      }
    case 'infrastructure':
      return {
        borderLeft: '3px solid var(--info)',
        backgroundColor: 'rgba(74, 124, 140, 0.06)',
      }
    case 'empty':
      return {
        borderLeft: '3px dashed var(--text-secondary)',
        backgroundColor: 'transparent',
        opacity: 0.6,
      }
    default:
      return {}
  }
}

function ConstructionQueuePanel({ queue }) {
  if (!queue || queue.length === 0) return null

  return (
    <div className="card" style={{ padding: '1rem', marginBottom: '1rem' }}>
      <h3
        style={{
          fontFamily: 'Cinzel, serif',
          fontSize: '1rem',
          marginBottom: '0.75rem',
          color: 'var(--accent-gold)',
          display: 'flex',
          alignItems: 'center',
          gap: '0.5rem',
        }}
      >
        {'\uD83D\uDD28'} Construction Queue
      </h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
        {queue.map((item, idx) => (
          <div
            key={idx}
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '0.5rem 0.75rem',
              backgroundColor: 'var(--bg-surface)',
              borderRadius: '0.375rem',
              border: '1px solid var(--border)',
            }}
          >
            <span style={{ fontSize: '0.9rem', color: 'var(--text-primary)' }}>
              {item.building_name || item.name || 'Building'} {'\u2192'} Level{' '}
              {item.target_level ?? item.level ?? '?'}
            </span>
            <span
              style={{
                fontSize: '0.85rem',
                color: 'var(--warning)',
                fontFamily: 'monospace',
              }}
            >
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
    <div
      className="card"
      style={{
        padding: '1rem',
        marginBottom: '1rem',
        border: '1px solid var(--accent-gold)',
      }}
    >
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
          Slot {selectedSlot} {detail ? `- ${detail.name || 'Empty'}` : ''}
        </h3>
        <button
          onClick={onClose}
          style={{
            background: 'none',
            border: 'none',
            color: 'var(--text-secondary)',
            cursor: 'pointer',
            fontSize: '1.1rem',
            padding: '0 0.25rem',
          }}
        >
          {'\u2715'}
        </button>
      </div>

      {loading && (
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
          Loading details...
        </p>
      )}

      {!loading && detail && !isEmptySlot && (
        <div>
          <div style={{ marginBottom: '0.75rem' }}>
            <span style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              Current Level: {detail.level ?? '?'}
            </span>
          </div>
          {detail.upgrade_cost && (
            <div
              style={{
                marginBottom: '0.75rem',
                padding: '0.5rem',
                backgroundColor: 'var(--bg-surface)',
                borderRadius: '0.375rem',
                fontSize: '0.8rem',
                color: 'var(--text-secondary)',
              }}
            >
              <div style={{ marginBottom: '0.25rem', fontWeight: 600 }}>
                Upgrade Cost:
              </div>
              <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
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
            <p
              style={{
                fontSize: '0.8rem',
                color: 'var(--warning)',
                marginBottom: '0.5rem',
              }}
            >
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
          <p
            style={{
              fontSize: '0.85rem',
              color: 'var(--text-secondary)',
              marginBottom: '0.75rem',
            }}
          >
            This slot is empty. Choose a building to construct:
          </p>
          {detail.available_buildings && detail.available_buildings.length > 0 ? (
            <>
              <select
                className="input-field"
                value={selectedNewBuilding}
                onChange={(e) => setSelectedNewBuilding(e.target.value)}
                style={{ marginBottom: '0.75rem' }}
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
                <p
                  style={{
                    fontSize: '0.8rem',
                    color: 'var(--warning)',
                    marginBottom: '0.5rem',
                  }}
                >
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
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
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
  }, [toast])

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
    <div style={{ padding: '1.5rem', maxWidth: '960px', margin: '0 auto' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '1.25rem',
        }}
      >
        <h2 style={{ fontFamily: 'Cinzel, serif', fontSize: '1.5rem', margin: 0 }}>
          Buildings
        </h2>
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

      <div className="card" style={{ padding: '1rem' }}>
        <h3
          style={{
            fontFamily: 'Cinzel, serif',
            fontSize: '1rem',
            marginBottom: '0.75rem',
            color: 'var(--accent-gold)',
          }}
        >
          Building Slots
        </h3>
        {buildingList.length === 0 ? (
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
            No building data available. Make sure you are connected.
          </p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
            {buildingList.map((b) => {
              const slotId = b.slot_id ?? b.slot ?? b.id
              const category = getBuildingCategory(slotId, b)
              const categoryStyle = getCategoryStyle(category)
              const isSelected = selectedSlot === slotId
              const isEmpty = category === 'empty'

              return (
                <button
                  key={slotId}
                  onClick={() => handleSlotClick(slotId)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.75rem',
                    padding: '0.6rem 0.75rem',
                    background: isSelected
                      ? 'rgba(201, 168, 76, 0.12)'
                      : 'var(--bg-surface)',
                    border: isSelected
                      ? '1px solid var(--accent-gold)'
                      : '1px solid var(--border)',
                    borderRadius: '0.375rem',
                    cursor: 'pointer',
                    width: '100%',
                    textAlign: 'left',
                    color: 'var(--text-primary)',
                    transition: 'background-color 0.15s, border-color 0.15s',
                    ...categoryStyle,
                  }}
                  onMouseEnter={(e) => {
                    if (!isSelected) {
                      e.currentTarget.style.backgroundColor = 'rgba(201, 168, 76, 0.06)'
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (!isSelected) {
                      e.currentTarget.style.backgroundColor =
                        categoryStyle.backgroundColor || 'var(--bg-surface)'
                    }
                  }}
                >
                  <span
                    style={{
                      fontSize: '0.75rem',
                      color: 'var(--text-secondary)',
                      minWidth: '2rem',
                      textAlign: 'right',
                    }}
                  >
                    #{slotId}
                  </span>
                  <span
                    style={{
                      flex: 1,
                      fontSize: '0.9rem',
                      fontWeight: isEmpty ? 400 : 500,
                      fontStyle: isEmpty ? 'italic' : 'normal',
                      color: isEmpty ? 'var(--text-secondary)' : 'var(--text-primary)',
                    }}
                  >
                    {isEmpty ? 'Empty Slot' : b.name || 'Unknown'}
                  </span>
                  <span
                    style={{
                      fontSize: '0.8rem',
                      color: 'var(--text-secondary)',
                      minWidth: '3.5rem',
                      textAlign: 'right',
                    }}
                  >
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
        <div
          style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: 'rgba(0, 0, 0, 0.3)',
            zIndex: 8000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div
            className="card"
            style={{
              padding: '1.5rem 2rem',
              color: 'var(--text-secondary)',
              fontSize: '0.9rem',
            }}
          >
            Processing...
          </div>
        </div>
      )}
    </div>
  )
}
