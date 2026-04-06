const resourceConfig = [
  { key: 'lumber', label: 'Lumber', color: '#8B7355', icon: '🪵', maxKey: 'max_lumber', prodKey: 'lumber_per_hour' },
  { key: 'clay', label: 'Clay', color: '#C4A882', icon: '🧱', maxKey: 'max_clay', prodKey: 'clay_per_hour' },
  { key: 'iron', label: 'Iron', color: '#808080', icon: '⛏️', maxKey: 'max_iron', prodKey: 'iron_per_hour' },
  { key: 'crop', label: 'Crop', color: '#6B8E23', icon: '🌾', maxKey: 'max_crop', prodKey: 'crop_per_hour' },
]

function formatNumber(n) {
  if (n == null) return '0'
  return Number(n).toLocaleString()
}

function SingleBar({ config, resources }) {
  const current = resources[config.key] ?? 0
  const max = resources[config.maxKey] ?? 1
  const production = resources[config.prodKey] ?? 0
  const pct = max > 0 ? Math.min((current / max) * 100, 100) : 0

  return (
    <div style={{ marginBottom: '0.5rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.25rem' }}>
        <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: '0.35rem' }}>
          <span>{config.icon}</span>
          <span>{config.label}</span>
        </span>
        <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          {formatNumber(current)} / {formatNumber(max)}
          <span style={{ marginLeft: '0.5rem', color: production >= 0 ? 'var(--success)' : 'var(--danger)' }}>
            {production >= 0 ? '+' : ''}{formatNumber(production)}/hr
          </span>
        </span>
      </div>
      <div
        style={{
          height: '6px',
          backgroundColor: 'var(--bg-base)',
          borderRadius: '3px',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: '100%',
            backgroundColor: config.color,
            borderRadius: '3px',
            transition: 'width 0.5s ease',
          }}
        />
      </div>
    </div>
  )
}

export default function ResourceBar({ resources }) {
  if (!resources) return null

  return (
    <div className="card" style={{ padding: '1rem' }}>
      {resourceConfig.map((cfg) => (
        <SingleBar key={cfg.key} config={cfg} resources={resources} />
      ))}
      {resources.free_crop != null && (
        <div
          style={{
            marginTop: '0.5rem',
            paddingTop: '0.5rem',
            borderTop: '1px solid var(--border)',
            fontSize: '0.8rem',
            color: resources.free_crop > 0 ? 'var(--success)' : 'var(--danger)',
            display: 'flex',
            alignItems: 'center',
            gap: '0.35rem',
          }}
        >
          <span>🌿</span>
          <span>Free Crop: {formatNumber(resources.free_crop)}</span>
        </div>
      )}
    </div>
  )
}
