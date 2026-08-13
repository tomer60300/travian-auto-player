const RESOURCE_COLORS = {
  lumber: '#8B7355',
  clay: '#C4A882',
  iron: '#808080',
  crop: '#6B8E23',
}

const resourceConfig = [
  { key: 'lumber', label: 'Lumber', icon: '🪵', maxKey: 'max_lumber', prodKey: 'lumber_per_hour' },
  { key: 'clay', label: 'Clay', icon: '🧱', maxKey: 'max_clay', prodKey: 'clay_per_hour' },
  { key: 'iron', label: 'Iron', icon: '⛏️', maxKey: 'max_iron', prodKey: 'iron_per_hour' },
  { key: 'crop', label: 'Crop', icon: '🌾', maxKey: 'max_crop', prodKey: 'crop_per_hour' },
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
  const barColor = RESOURCE_COLORS[config.key]

  return (
    <div className="mb-2">
      <div className="flex justify-between items-center mb-1">
        <span className="text-xs text-secondary flex items-center gap-1">
          <span>{config.icon}</span>
          <span>{config.label}</span>
        </span>
        <span className="text-xs text-secondary">
          {formatNumber(current)} / {formatNumber(max)}
          <span className={`ml-2 ${production >= 0 ? 'text-success' : 'text-danger'}`}>
            {production >= 0 ? '+' : ''}{formatNumber(production)}/hr
          </span>
        </span>
      </div>
      <div className="h-1.5 bg-base rounded-sm overflow-hidden" role="progressbar" aria-label={config.label} aria-valuenow={current} aria-valuemin={0} aria-valuemax={max}>
        <div
          className="h-full rounded-sm transition-[width] duration-500 ease-in-out"
          style={{ width: `${pct}%`, backgroundColor: barColor }}
        />
      </div>
    </div>
  )
}

export default function ResourceBar({ resources }) {
  if (!resources) return null

  return (
    <div className="card p-4">
      {resourceConfig.map((cfg) => (
        <SingleBar key={cfg.key} config={cfg} resources={resources} />
      ))}
      {/* crop_per_hour (production.l4) is the true net rate. free_crop (l5) was
          used here and is not net — it reads positive on a starving village, so
          this bar showed green while the granary drained. */}
      {resources.crop_per_hour != null && (
        <div className={`mt-2 pt-2 border-t-default text-xs flex items-center gap-1 ${resources.crop_per_hour >= 0 ? 'text-success' : 'text-danger'}`}>
          <span>🌿</span>
          <span>Net Crop: {formatNumber(resources.crop_per_hour)}/h</span>
          {resources.crop_per_hour < 0 && <span>— starving</span>}
        </div>
      )}
    </div>
  )
}
