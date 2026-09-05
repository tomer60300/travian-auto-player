import { useEffect, useRef, useState, memo } from 'react'
import { useNavigate } from 'react-router-dom'
import useGameStore from '../stores/gameStore'
import VillageSelector from '../components/VillageSelector'
import ResourceBar from '../components/ResourceBar'
import FetchError from '../components/FetchError'

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
  { icon: '\uD83D\uDCCB', label: 'Build Queue', description: 'Automated building upgrades', path: '/queue' },
  { icon: '\uD83C\uDF3E', label: 'Send Farms', description: 'Launch farm raids', path: '/farm' },
  { icon: '\uD83D\uDD2D', label: 'Scout Map', description: 'Auto-scout surroundings', path: '/scout' },
  { icon: '\uD83C\uDFAC', label: 'Video Rewards', description: 'Watch ads for free resources', path: '/video' },
  { icon: '\uD83D\uDCDC', label: 'Reports', description: 'Battle and trade reports', path: '/reports' },
]

function formatTimeRemaining(seconds) {
  if (seconds == null || seconds <= 0) return '00:00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  return [h, m, s].map((v) => String(v).padStart(2, '0')).join(':')
}

const ConstructionQueueSummary = memo(function ConstructionQueueSummary({ queue }) {
  // Snapshot the time when queue data arrived so each item's countdown is relative
  const [snapshotTime, setSnapshotTime] = useState(Date.now)
  const [now, setNow] = useState(Date.now)

  useEffect(() => {
    if (!queue || queue.length === 0) return
    setSnapshotTime(Date.now())
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [queue])

  if (!queue || queue.length === 0) return null

  const elapsedSecs = Math.floor((now - snapshotTime) / 1000)

  return (
    <div className="card p-4">
      <h3 className="heading-gold text-base flex items-center gap-2 mb-3">
        {'\uD83D\uDD28'} Construction Queue
      </h3>
      <div className="flex flex-col gap-2">
        {queue.map((item, idx) => {
          const baseRemaining = item.remaining_seconds ?? item.time_remaining ?? item.seconds_remaining
          const remaining = Math.max(0, (baseRemaining || 0) - elapsedSecs)
          const doneAt = new Date(Date.now() + remaining * 1000)
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
      <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-3">
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
  const activeVillageId = useGameStore((s) => s.activeVillageId)

  const activeVillage = villages?.find((v) => (v.id ?? v.villageId) === activeVillageId)
  const coordsStr = activeVillage
    ? `(${activeVillage.x ?? '?'}|${activeVillage.y ?? '?'})`
    : '---'

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
        <InfoRow label="Active Village" value={coordsStr} />
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

function SkeletonCard({ height = 'h-24' }) {
  return <div className={`card ${height} animate-pulse bg-surface/50`} />
}

export default function Dashboard() {
  const resources = useGameStore((s) => s.resources)
  const resourcesError = useGameStore((s) => s.resourcesError)
  const constructionQueue = useGameStore((s) => s.constructionQueue)
  const queueError = useGameStore((s) => s.queueError)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const fetchResources = useGameStore((s) => s.fetchResources)
  const fetchBuildings = useGameStore((s) => s.fetchBuildings)
  const fetchQueue = useGameStore((s) => s.fetchQueue)

  const [resourcesReady, setResourcesReady] = useState(false)
  const [queueReady, setQueueReady] = useState(false)
  const intervalRef = useRef(null)
  const fetchResourcesRef = useRef(fetchResources)
  useEffect(() => { fetchResourcesRef.current = fetchResources }, [fetchResources])

  // Fetch all data in PARALLEL, reveal each section as it arrives
  useEffect(() => {
    let cancelled = false
    setResourcesReady(false)
    setQueueReady(false)

    // Fire all three in parallel — no sequential blocking
    fetchResources().then(() => { if (!cancelled) setResourcesReady(true) })
    fetchQueue().then(() => { if (!cancelled) setQueueReady(true) })
    fetchBuildings() // buildings aren't displayed on dashboard, but pre-fetched

    intervalRef.current = setInterval(() => {
      if (document.visibilityState === 'visible') {
        fetchResourcesRef.current()
      }
    }, 60000)

    return () => {
      cancelled = true
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [activeVillageId, fetchResources, fetchQueue, fetchBuildings])

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Dashboard</h2>
        <VillageSelector />
      </div>

      <div className="flex flex-col gap-4">
        {/* Player info is always available immediately from connect response */}
        <PlayerInfoCard />

        {/* Resources — skeleton until loaded, and a named failure if the read
            did not come back. A failed read used to leave `resources` null,
            which renders the same blank bar as a village with no data. */}
        <div>
          <h3 className="heading-gold text-base mb-2">Resources</h3>
          {resourcesError ? (
            <FetchError what="Could not read this village's resources" detail={resourcesError} onRetry={fetchResources} />
          ) : resourcesReady ? (
            <ResourceBar resources={resources} />
          ) : (
            <SkeletonCard height="h-16" />
          )}
        </div>

        {/* Construction queue — skeleton until loaded. The empty sentence is
            reachable only when the read actually SUCCEEDED and came back
            empty; a failure said "No active operations" too, which is a claim
            about the game we had not been able to make. */}
        {queueError ? (
          <FetchError what="Could not read the construction queue" detail={queueError} onRetry={fetchQueue} />
        ) : queueReady ? (
          <>
            <ConstructionQueueSummary queue={constructionQueue} />
            {(!constructionQueue || constructionQueue.length === 0) && resourcesReady && !resourcesError && (
              <div className="card p-6 text-center">
                <p className="text-secondary text-sm">No active operations. Start something from the actions below.</p>
              </div>
            )}
          </>
        ) : (
          <SkeletonCard height="h-20" />
        )}

        {/* Quick actions are always available */}
        <QuickActions />
      </div>
    </div>
  )
}
