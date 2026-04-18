export default function MatrixSpec({ villages, value, onChange, disabled, radius }) {
  const homes = villages.filter((v) => value.home_village_ids.includes(v.id))

  const updateShort = (vid, short) => {
    const map = { ...(value.home_shorts || {}) }
    map[vid] = short
    onChange({ ...value, home_shorts: map })
  }

  const movePriority = (vid, dir) => {
    const p = [...(value.home_priority || value.home_village_ids)]
    const idx = p.indexOf(vid)
    if (idx < 0) return
    const ni = idx + dir
    if (ni < 0 || ni >= p.length) return
    ;[p[idx], p[ni]] = [p[ni], p[idx]]
    onChange({ ...value, home_priority: p })
  }

  const addPopBucket = () => {
    const pb = [...(value.pop_buckets || [])]
    if (pb.length >= 4) return
    pb.push({ label: `B${pb.length + 1}`, max_pop: null })
    onChange({ ...value, pop_buckets: pb })
  }

  const removePopBucket = (i) => {
    const pb = [...(value.pop_buckets || [])]
    if (pb.length <= 1) return
    pb.splice(i, 1)
    onChange({ ...value, pop_buckets: pb })
  }

  const updatePopBucket = (i, field, v) => {
    const pb = [...(value.pop_buckets || [])]
    pb[i] = { ...pb[i], [field]: field === 'max_pop' ? (v === '' ? null : Number(v)) : v }
    onChange({ ...value, pop_buckets: pb })
  }

  // Live preview: compute bucket names from current state
  const renderPreview = () => {
    const pb = [...(value.pop_buckets || [])].sort((a, b) => {
      const ax = a.max_pop == null ? Infinity : a.max_pop
      const bx = b.max_pop == null ? Infinity : b.max_pop
      return ax - bx
    })
    const rows = []
    let prevMax = 0
    for (const b of pb) {
      const mx = b.max_pop == null ? '∞' : b.max_pop
      const mn = prevMax > 0 ? prevMax + 1 : 1
      const predicate = `pop in [${mn}, ${mx}]`
      for (const h of homes) {
        const short = (value.home_shorts || {})[h.id] || h.name || `V${h.id}`
        const name = (value.name_template || '{home_short}-{pop_label}-{radius}')
          .replace('{home_short}', short)
          .replace('{pop_label}', b.label)
          .replace('{radius}', radius)
          .replace('{home_id}', h.id)
        rows.push({
          name, home: short,
          predicate: `closest to ${short} AND ${predicate}`,
        })
      }
      prevMax = b.max_pop == null ? prevMax : b.max_pop
    }
    return rows
  }

  const preview = renderPreview()
  const priority = value.home_priority || value.home_village_ids

  if (homes.length === 0) {
    return (
      <div className="card mb-4 error-box">
        <p className="text-warning">Select at least one home village above to configure matrix.</p>
      </div>
    )
  }

  return (
    <div className="card mb-4">
      <h3 className="heading-gold text-lg mb-4">2. Farm List Spec — Matrix Mode</h3>

      <div className="mb-4">
        <label className="field-label-lg">Name template</label>
        <input
          type="text"
          className="input-field w-full"
          value={value.name_template || '{home_short}-{pop_label}-{radius}'}
          onChange={(e) => onChange({ ...value, name_template: e.target.value })}
          disabled={disabled}
        />
        <p className="text-xs text-secondary mt-1">
          Placeholders: <code>{'{home_short}'}</code> <code>{'{pop_label}'}</code> <code>{'{radius}'}</code> <code>{'{home_id}'}</code>
        </p>
      </div>

      <div className="mb-4">
        <label className="field-label-lg mb-2">Home village short names</label>
        <div className="grid grid-cols-2 gap-2">
          {homes.map((h) => (
            <div key={h.id} className="flex items-center gap-2">
              <span className="text-sm text-secondary w-32 truncate">{h.name} ({h.x}|{h.y})</span>
              <input
                type="text"
                className="input-field flex-1"
                value={(value.home_shorts || {})[h.id] || ''}
                placeholder={`Village${String(homes.indexOf(h) + 1).padStart(2, '0')}`}
                onChange={(e) => updateShort(h.id, e.target.value)}
                disabled={disabled}
              />
            </div>
          ))}
        </div>
      </div>

      <div className="mb-4">
        <label className="field-label-lg mb-2">Tie-break priority (highest first)</label>
        <div className="flex flex-col gap-1">
          {priority.map((vid) => {
            const h = villages.find((v) => v.id === vid)
            if (!h || !value.home_village_ids.includes(vid)) return null
            const short = (value.home_shorts || {})[vid] || h.name
            return (
              <div key={vid} className="flex items-center gap-2 p-2 border border-default rounded">
                <span className="flex-1">{short} ({h.x}|{h.y})</span>
                <button
                  className="btn-secondary btn-xs"
                  onClick={() => movePriority(vid, -1)}
                  disabled={disabled}
                >↑</button>
                <button
                  className="btn-secondary btn-xs"
                  onClick={() => movePriority(vid, 1)}
                  disabled={disabled}
                >↓</button>
              </div>
            )
          })}
        </div>
      </div>

      <div className="mb-4">
        <div className="flex items-center justify-between mb-2">
          <label className="field-label-lg">Population buckets (1–4)</label>
          <button className="btn-secondary btn-xs" onClick={addPopBucket} disabled={disabled || (value.pop_buckets || []).length >= 4}>
            + Add bucket
          </button>
        </div>
        {(value.pop_buckets || []).map((b, i) => (
          <div key={i} className="flex items-center gap-2 mb-2">
            <input
              type="text"
              className="input-field w-24"
              placeholder="Label"
              value={b.label}
              onChange={(e) => updatePopBucket(i, 'label', e.target.value)}
              disabled={disabled}
            />
            <span className="text-sm text-secondary">max pop:</span>
            <input
              type="number"
              className="input-field w-32"
              placeholder="∞ (leave empty)"
              value={b.max_pop == null ? '' : b.max_pop}
              onChange={(e) => updatePopBucket(i, 'max_pop', e.target.value)}
              disabled={disabled}
            />
            {(value.pop_buckets || []).length > 1 && (
              <button className="btn-danger btn-xs" onClick={() => removePopBucket(i)} disabled={disabled}>×</button>
            )}
          </div>
        ))}
      </div>

      <div className="mt-5 p-3 border border-default rounded bg-primary/5">
        <div className="font-semibold mb-2">Live preview — farm list names:</div>
        {preview.length === 0 ? (
          <div className="text-sm text-secondary">No buckets yet.</div>
        ) : (
          <div className="flex flex-col gap-1">
            {preview.map((r, i) => (
              <div key={i} className="text-sm">
                <strong className="text-primary">{r.name}</strong>
                <span className="text-secondary"> — {r.predicate}</span>
              </div>
            ))}
          </div>
        )}
        <div className="mt-2 text-xs text-secondary">Total: {preview.length} farm list(s)</div>
      </div>
    </div>
  )
}
