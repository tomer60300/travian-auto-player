import { useState, useEffect, useRef } from 'react'
import api from '../api'
import useGameStore from '../stores/gameStore'
import { useToast } from './Toast'
import { TRIBE_TROOPS, DEFAULT_TROOPS } from '../constants/troops'

export default function AddToFarmDialog({ open, target, farmLists, onClose, onAdded }) {
  const toast = useToast()
  const tribeId = useGameStore((s) => s.tribeId)
  const troopNames = TRIBE_TROOPS[tribeId] || DEFAULT_TROOPS

  const [selectedListId, setSelectedListId] = useState('')
  const [troopType, setTroopType] = useState('t1')
  const [troopCount, setTroopCount] = useState(3)
  const [force, setForce] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  const dialogRef = useRef(null)

  // Reset form when dialog opens
  useEffect(() => {
    if (open && farmLists.length > 0 && !selectedListId) {
      setSelectedListId(farmLists[0].id)
    }
  }, [open, farmLists, selectedListId])

  // Focus trap + escape key
  useEffect(() => {
    if (!open) return

    const handleKeyDown = (e) => {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'Tab' && dialogRef.current) {
        const focusable = dialogRef.current.querySelectorAll(
          'button, input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus() }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus() }
        }
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open, onClose])

  if (!open || !target) return null

  const handleSubmit = async () => {
    if (!selectedListId) { toast.warning('Select a farm list'); return }
    if (troopCount < 0) { toast.warning('Invalid troop count'); return }

    const troops = {}
    for (let i = 1; i <= 10; i++) troops[`t${i}`] = 0
    troops[troopType] = troopCount

    try {
      setSubmitting(true)
      await api.post(`/farm/lists/${selectedListId}/targets`, {
        x: target.x,
        y: target.y,
        troops,
        force,
      })
      toast.success(`(${target.x},${target.y}) added to farm list`)
      onAdded?.(selectedListId, target.x, target.y)
      onClose()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to add target')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div ref={dialogRef} className="dialog-card" role="dialog" aria-modal="true" aria-labelledby="add-farm-title" style={{ maxWidth: 420 }}>
        <h3 id="add-farm-title" className="heading-gold text-lg mb-1">Add to Farm List</h3>
        <p className="text-secondary text-sm mb-4">({target.x}, {target.y}) — {target.village_name || target.name || '?'}</p>

        <div className="mb-3">
          <label className="field-label-lg">Farm List</label>
          <select className="input-field" value={selectedListId} onChange={(e) => setSelectedListId(Number(e.target.value))}>
            {farmLists.map((fl) => (
              <option key={fl.id} value={fl.id}>{fl.name} ({fl.slots_amount} slots)</option>
            ))}
          </select>
        </div>

        <div className="flex gap-3 mb-3">
          <div className="flex-1">
            <label className="field-label-lg">Troop Type</label>
            <select className="input-field" value={troopType} onChange={(e) => setTroopType(e.target.value)}>
              {troopNames.map((name, i) => (
                <option key={i} value={`t${i + 1}`}>{name} (t{i + 1})</option>
              ))}
            </select>
          </div>
          <div style={{ width: 100 }}>
            <label className="field-label-lg">Count</label>
            <input type="number" className="input-field" min={0} max={999} value={troopCount} onChange={(e) => setTroopCount(Number(e.target.value))} />
          </div>
        </div>

        <label className="check-label text-sm mb-4 block">
          <input type="checkbox" className="checkbox-gold" checked={force} onChange={(e) => setForce(e.target.checked)} />
          Force add (skip duplicate check)
        </label>

        <div className="flex justify-end gap-3">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={submitting}>Cancel</button>
          <button type="button" className="btn-primary" onClick={handleSubmit} disabled={submitting}>
            {submitting ? 'Adding...' : 'Add Target'}
          </button>
        </div>
      </div>
    </div>
  )
}
