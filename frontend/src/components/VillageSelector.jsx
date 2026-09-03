import { useState } from 'react'
import useGameStore from '../stores/gameStore'

export default function VillageSelector({ compact = false }) {
  const villages = useGameStore((s) => s.villages)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const switchVillage = useGameStore((s) => s.switchVillage)
  const [switching, setSwitching] = useState(false)

  if (!villages || villages.length === 0) return null

  const handleSwitch = async (id) => {
    setSwitching(true)
    try { await switchVillage(id) } finally { setSwitching(false) }
  }

  return (
    <select
      value={activeVillageId || ''}
      onChange={(e) => { const id = Number(e.target.value); if (id) handleSwitch(id) }}
      disabled={switching}
      className={`${compact ? 'input-field w-auto text-xs py-1 px-2 max-w-[160px] cursor-pointer bg-surface text-primary' : 'input-field max-w-[260px] cursor-pointer bg-surface text-primary'} ${switching ? 'opacity-50' : ''}`}
    >
      {villages.map((v) => (
        <option key={v.id} value={v.id}>
          {v.name} ({v.x}|{v.y})
        </option>
      ))}
    </select>
  )
}
