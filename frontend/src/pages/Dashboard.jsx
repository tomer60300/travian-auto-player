import { useEffect, useRef, useState, memo } from 'react'
import { useNavigate } from 'react-router-dom'
import useGameStore from '../stores/gameStore'
import VillageSelector from '../components/VillageSelector'
import ResourceBar from '../components/ResourceBar'

const TRIBE_NAMES = {
  1: 'Romans',
  2: 'Teutons',
  3: 'Gauls',
  4: 'Nature',
  5: 'Natars',
  6: 'Egyptians',
  7: 'Huns',
}

const quickActions = [
  {
    icon: '\uD83C\uDFAC',
    label: 'Claim Video Rewards',
    description: 'Watch ads for free resources',
    path: '/video',
  },
  {
    icon: '\uD83D\uDDE1\uFE0F',
    label: 'Send Farm Lists',
    description: 'Launch farm raids',
    path: '/farm',
  },
  {
    icon: '\uD83D\uDCDC',
    label: 'View Reports',
    description: 'Battle and trade reports',
    path: '/reports',
  },
  {
    icon: '\uD83D\uDDFA\uFE0F',
    label: 'Scan Map',
    description: 'Auto-scout surroundings',
    path: '/scout',
  },
]

function formatTimeRemaining(seconds) {
  if (seconds == null || seconds <= 0) return '00:00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  return [h, m, s].map((v) => String(v).padStart(2, '0')).join(':')
}

const ConstructionQueueSummary = memo(function ConstructionQueueSummary({ queue }) {
  if (!queue || queue.length === 0) return null

  return (
    <div className="card p-4">
      <h3 className="heading-gold text-base flex items-center gap-2 mb-3">
        {'\uD83D\uDD28'} Construction Queue
      </h3>
      <div className="flex flex-col gap-2">
        {queue.map((item, idx) => {
          const remaining = item.remaining_seconds ?? item.time_remaining ?? item.seconds_remaining
          const doneAt = new Date(Date.now() + (remaining || 0) * 1000)
          const doneStr = doneAt.toLocaleTimeString('en-US', { hour12: false })
          return (
            <div key={item.event_id ?? `${item.building_name}-${item.target_level}-${idx}`} className="surface-row">
              <span className="text-sm text-primary">
                {item.building_name || item.name || 'Building'} {'\u2192'} Level{' '}
                {item.target_level ?? item.level ?? '?'}
              </span>
              <span className="text-sm text-warning font-mono">
                {formatTimeRemaining(remaining)}
                <span className="text-xs text-secondary ml-2">Done at {doneStr}</span>
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
})

function QuickActions() {
  const navigate = useNavigate()

  return (
    <div className="card p-4">
      <h3 className="heading-gold text-base mb-3">
        Quick Actions
      </h3>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-3">
        {quickActions.map((action) => (
          <button
            key={action.path}
            onClick={() => navigate(action.path)}
            className="quick-action"
          >
            <span className="text-2xl">{action.icon}</span>
            <span className="text-sm font-semibold">{action.label}</span>
            <span className="text-xs text-secondary">{action.description}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function PlayerInfoCard() {
  const serverUrl = useGameStore((s) => s.serverUrl)
  const playerName = useGameStore((s) => s.playerName)
  const tribeId = useGameStore((s) => s.tribeId)
  const villages = useGameStore((s) => s.villages)

  return (
    <div className="card p-4">
      <h3 className="heading-gold text-base mb-3">
        Player Info
      </h3>
      <div className="flex flex-col gap-1">
        <InfoRow label="Server" value={serverUrl || '---'} />
        <InfoRow label="Player" value={playerName || '---'} />
        <InfoRow label="Tribe" value={TRIBE_NAMES[tribeId] || `Tribe ${tribeId ?? '?'}`} />
        <InfoRow label="Villages" value={villages?.length ?? 0} />
      </div>
    </div>
  )
}

function InfoRow({ label, value }) {
  return (
    <div className="info-row">
      <span className="text-sm text-secondary">{label}</span>
      <span className="text-sm text-primary break-all text-right max-w-[60%]">
        {value}
      </span>
    </div>
  )
}

export default function Dashboard() {
  const resources = useGameStore((s) => s.resources)
  const constructionQueue = useGameStore((s) => s.constructionQueue)
  const fetchResources = useGameStore((s) => s.fetchResources)
  const fetchBuildings = useGameStore((s) => s.fetchBuildings)
  const fetchQueue = useGameStore((s) => s.fetchQueue)

  const [loading, setLoading] = useState(true)
  const intervalRef = useRef(null)
  const fetchResourcesRef = useRef(fetchResources)
  useEffect(() => { fetchResourcesRef.current = fetchResources }, [fetchResources])

  useEffect(() => {
    let cancelled = false
    async function loadData() {
      await fetchResources()
      if (cancelled) return
      await fetchQueue()
      if (cancelled) return
      setLoading(false)
    }
    loadData()

    intervalRef.current = setInterval(() => {
      if (document.visibilityState === 'visible') {
        fetchResourcesRef.current()
      }
    }, 60000)

    return () => {
      cancelled = true
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [])

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Dashboard</h2>
        <VillageSelector />
      </div>

      {loading ? (
        <div className="card p-12 text-center">
          <div className="spinner spinner-md mx-auto mb-4" />
          <span className="text-secondary">Loading village data...</span>
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <ResourceBar resources={resources} />
          <ConstructionQueueSummary queue={constructionQueue} />
          <QuickActions />
          <PlayerInfoCard />
        </div>
      )}
    </div>
  )
}
