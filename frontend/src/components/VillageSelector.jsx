import useGameStore from '../stores/gameStore'

export default function VillageSelector() {
  const villages = useGameStore((s) => s.villages)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const switchVillage = useGameStore((s) => s.switchVillage)

  if (!villages || villages.length === 0) return null

  return (
    <select
      value={activeVillageId || ''}
      onChange={(e) => { const id = Number(e.target.value); if (id) switchVillage(id) }}
      className="input-field max-w-[260px] cursor-pointer bg-surface text-primary"
    >
      {villages.map((v) => (
        <option key={v.id} value={v.id}>
          {v.name} ({v.x}|{v.y})
        </option>
      ))}
    </select>
  )
}
