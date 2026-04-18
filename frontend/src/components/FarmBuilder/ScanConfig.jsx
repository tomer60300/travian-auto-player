import { useState } from 'react'

export default function ScanConfig({ villages, value, onChange, disabled }) {
  const [tagInput, setTagInput] = useState('')
  const [playerInput, setPlayerInput] = useState('')

  const toggleVillage = (vid) => {
    const s = new Set(value.home_village_ids)
    if (s.has(vid)) s.delete(vid)
    else s.add(vid)
    onChange({ ...value, home_village_ids: [...s] })
  }

  const addTag = (e) => {
    e.preventDefault()
    const t = tagInput.trim()
    if (!t) return
    if (!value.exclude_alliance_tags.map(x => x.toLowerCase()).includes(t.toLowerCase())) {
      onChange({ ...value, exclude_alliance_tags: [...value.exclude_alliance_tags, t] })
    }
    setTagInput('')
  }

  const removeTag = (t) => {
    onChange({ ...value, exclude_alliance_tags: value.exclude_alliance_tags.filter(x => x !== t) })
  }

  const addPlayer = (e) => {
    e.preventDefault()
    const t = playerInput.trim()
    if (!t) return
    if (!value.exclude_player_names.map(x => x.toLowerCase()).includes(t.toLowerCase())) {
      onChange({ ...value, exclude_player_names: [...value.exclude_player_names, t] })
    }
    setPlayerInput('')
  }

  const removePlayer = (t) => {
    onChange({ ...value, exclude_player_names: value.exclude_player_names.filter(x => x !== t) })
  }

  return (
    <div className="card mb-4">
      <h3 className="heading-gold text-lg mb-4">1. Scan Parameters</h3>

      <div className="grid grid-cols-2 gap-4 mb-4">
        <div>
          <label className="field-label-lg">Radius (tiles)</label>
          <input
            type="number" min={1} max={50}
            className="input-field w-32"
            value={value.radius}
            onChange={(e) => onChange({ ...value, radius: Number(e.target.value) })}
            disabled={disabled}
          />
          <p className="text-xs text-secondary mt-1">Chebyshev, 1–50</p>
        </div>
        <div>
          <label className="field-label-lg">Player total population max</label>
          <input
            type="number" min={1} max={10000}
            className="input-field w-32"
            value={value.max_player_total_pop}
            onChange={(e) => onChange({ ...value, max_player_total_pop: Number(e.target.value) })}
            disabled={disabled}
          />
          <p className="text-xs text-secondary mt-1">Across all of that player's villages</p>
        </div>
      </div>

      <div className="mb-4">
        <label className="field-label-lg mb-2">Home Villages</label>
        <div className="flex gap-2 flex-wrap">
          {villages.map((v) => (
            <label key={v.id} className="check-label">
              <input
                type="checkbox"
                className="checkbox-gold"
                checked={value.home_village_ids.includes(v.id)}
                onChange={() => toggleVillage(v.id)}
                disabled={disabled}
              />
              {v.name} ({v.x}|{v.y})
            </label>
          ))}
        </div>
      </div>

      <div className="mb-4">
        <label className="field-label-lg mb-2">Exclude Alliances (tags)</label>
        <form onSubmit={addTag} className="flex gap-2 mb-2">
          <input
            type="text"
            className="input-field flex-1"
            placeholder="Type tag then Enter (e.g. LR)"
            value={tagInput}
            onChange={(e) => setTagInput(e.target.value)}
            disabled={disabled}
          />
          <button className="btn-secondary" type="submit" disabled={disabled}>Add</button>
        </form>
        <div className="flex gap-2 flex-wrap">
          {value.exclude_alliance_tags.map((t) => (
            <span key={t} className="loop-chip">
              {t}
              {!disabled && (
                <button onClick={() => removeTag(t)} className="ml-1 text-danger">×</button>
              )}
            </span>
          ))}
          {value.exclude_alliance_tags.length === 0 && (
            <span className="text-xs text-secondary">(none)</span>
          )}
        </div>
      </div>

      <div className="mb-2">
        <label className="field-label-lg mb-2">Exclude Players (names)</label>
        <form onSubmit={addPlayer} className="flex gap-2 mb-2">
          <input
            type="text"
            className="input-field flex-1"
            placeholder="Type player name then Enter"
            value={playerInput}
            onChange={(e) => setPlayerInput(e.target.value)}
            disabled={disabled}
          />
          <button className="btn-secondary" type="submit" disabled={disabled}>Add</button>
        </form>
        <div className="flex gap-2 flex-wrap">
          {value.exclude_player_names.map((t) => (
            <span key={t} className="loop-chip">
              {t}
              {!disabled && (
                <button onClick={() => removePlayer(t)} className="ml-1 text-danger">×</button>
              )}
            </span>
          ))}
          {value.exclude_player_names.length === 0 && (
            <span className="text-xs text-secondary">(none)</span>
          )}
        </div>
      </div>
    </div>
  )
}
