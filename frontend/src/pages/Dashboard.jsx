import { useEffect, useRef, useState } from 'react'
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

function ConstructionQueueSummary({ queue }) {
  if (!queue || queue.length === 0) return null

  return (
    <div className="card" style={{ padding: '1rem' }}>
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

function QuickActions() {
  const navigate = useNavigate()

  return (
    <div className="card" style={{ padding: '1rem' }}>
      <h3
        style={{
          fontFamily: 'Cinzel, serif',
          fontSize: '1rem',
          marginBottom: '0.75rem',
          color: 'var(--accent-gold)',
        }}
      >
        Quick Actions
      </h3>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
          gap: '0.75rem',
        }}
      >
        {quickActions.map((action) => (
          <button
            key={action.path}
            onClick={() => navigate(action.path)}
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: '0.35rem',
              padding: '1rem 0.75rem',
              backgroundColor: 'var(--bg-surface)',
              border: '1px solid var(--border)',
              borderRadius: '0.5rem',
              cursor: 'pointer',
              transition: 'border-color 0.2s, background-color 0.2s',
              color: 'var(--text-primary)',
              textAlign: 'center',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.borderColor = 'var(--accent-gold)'
              e.currentTarget.style.backgroundColor = 'rgba(201, 168, 76, 0.05)'
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = 'var(--border)'
              e.currentTarget.style.backgroundColor = 'var(--bg-surface)'
            }}
          >
            <span style={{ fontSize: '1.5rem' }}>{action.icon}</span>
            <span style={{ fontSize: '0.9rem', fontWeight: 600 }}>{action.label}</span>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
              {action.description}
            </span>
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
    <div className="card" style={{ padding: '1rem' }}>
      <h3
        style={{
          fontFamily: 'Cinzel, serif',
          fontSize: '1rem',
          marginBottom: '0.75rem',
          color: 'var(--accent-gold)',
        }}
      >
        Player Info
      </h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
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
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        padding: '0.35rem 0',
        borderBottom: '1px solid var(--border)',
      }}
    >
      <span style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{label}</span>
      <span
        style={{
          fontSize: '0.85rem',
          color: 'var(--text-primary)',
          wordBreak: 'break-all',
          textAlign: 'right',
          maxWidth: '60%',
        }}
      >
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

  useEffect(() => {
    let cancelled = false
    async function loadData() {
      // Fetch sequentially to avoid overwhelming the stealth-limited backend
      await fetchResources()
      if (cancelled) return
      await fetchQueue()
      if (cancelled) return
      setLoading(false)
    }
    loadData()

    intervalRef.current = setInterval(() => {
      fetchResources()
    }, 60000) // 60s refresh — gentler on stealth backend

    return () => {
      cancelled = true
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [])

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
        <h2 style={{ fontFamily: 'Cinzel, serif', fontSize: '1.5rem', margin: 0 }}>Dashboard</h2>
        <VillageSelector />
      </div>

      {loading ? (
        <div className="card" style={{ padding: '3rem', textAlign: 'center' }}>
          <div
            style={{
              width: '28px',
              height: '28px',
              border: '3px solid var(--border)',
              borderTopColor: 'var(--accent-gold)',
              borderRadius: '50%',
              animation: 'spin 0.8s linear infinite',
              margin: '0 auto 1rem',
            }}
          />
          <span style={{ color: 'var(--text-secondary)' }}>Loading village data...</span>
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          <ResourceBar resources={resources} />
          <ConstructionQueueSummary queue={constructionQueue} />
          <QuickActions />
          <PlayerInfoCard />
        </div>
      )}
    </div>
  )
}
