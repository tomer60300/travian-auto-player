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

  // One name for both renders: the shell mounts this twice -- `compact` in the
  // mobile top bar and full-width in the sidebar -- and shows whichever the
  // breakpoint allows. Neither had a name, so the one control the width
  // census cannot see was reported to its reader as "select", which is
  // indistinguishable from a control the sweep genuinely missed.
  return (
    <select
      aria-label="Active village"
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
