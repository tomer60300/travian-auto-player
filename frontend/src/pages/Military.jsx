import { useState } from 'react'
import api from '../api'
import useGameStore from '../stores/gameStore'
import { useToast } from '../components/Toast'
import ConfirmDialog from '../components/ConfirmDialog'
import VillageSelector from '../components/VillageSelector'

const TRIBE_TROOPS = {
  1: [
    'Legionnaire', 'Praetorian', 'Imperian', 'Equites Legati',
    'Equites Imperatoris', 'Equites Caesaris', 'Battering Ram',
    'Fire Catapult', 'Senator', 'Settler',
  ],
  2: [
    'Clubswinger', 'Spearfighter', 'Axefighter', 'Scout',
    'Paladin', 'Teutonic Knight', 'Ram',
    'Catapult', 'Chief', 'Settler',
  ],
  3: [
    'Phalanx', 'Swordsman', 'Pathfinder', 'Theutates Thunder',
    'Druidrider', 'Haeduan', 'Ram',
    'Trebuchet', 'Chieftain', 'Settler',
  ],
}

const DEFAULT_TROOPS = [
  'Troop 1', 'Troop 2', 'Troop 3', 'Troop 4', 'Troop 5',
  'Troop 6', 'Troop 7', 'Troop 8', 'Troop 9', 'Troop 10',
]

function Spinner({ size = 4 }) {
  return (
    <span
      className={`inline-block w-${size} h-${size} rounded-full animate-spin`}
      style={{
        border: '2px solid var(--accent-gold)',
        borderTopColor: 'transparent',
        width: `${size * 0.25}rem`,
        height: `${size * 0.25}rem`,
      }}
    />
  )
}

export default function Military() {
  const toast = useToast()
  const tribeId = useGameStore((s) => s.tribeId)
  const connected = useGameStore((s) => s.connected)

  // Scout state
  const [scoutX, setScoutX] = useState('')
  const [scoutY, setScoutY] = useState('')
  const [scoutAmount, setScoutAmount] = useState('1')
  const [scoutType, setScoutType] = useState('resources')
  const [scoutLoading, setScoutLoading] = useState(false)
  const [scoutResult, setScoutResult] = useState(null)
  const [scoutConfirmOpen, setScoutConfirmOpen] = useState(false)

  // Raid state
  const [raidX, setRaidX] = useState('')
  const [raidY, setRaidY] = useState('')
  const [troops, setTroops] = useState({})
  const [raidLoading, setRaidLoading] = useState(false)
  const [raidResult, setRaidResult] = useState(null)
  const [raidConfirmOpen, setRaidConfirmOpen] = useState(false)

  const troopNames = TRIBE_TROOPS[tribeId] || DEFAULT_TROOPS

  function setTroopCount(index, value) {
    const key = `t${index + 1}`
    const num = parseInt(value, 10)
    setTroops((prev) => {
      const next = { ...prev }
      if (!value || num <= 0) {
        delete next[key]
      } else {
        next[key] = num
      }
      return next
    })
  }

  function getTroopCount(index) {
    const key = `t${index + 1}`
    return troops[key] || ''
  }

  // Scout handlers
  function handleScoutClick() {
    if (!scoutX || !scoutY) {
      toast.error('Please enter X and Y coordinates')
      return
    }
    setScoutConfirmOpen(true)
  }

  async function sendScout() {
    setScoutConfirmOpen(false)
    setScoutLoading(true)
    setScoutResult(null)
    try {
      const res = await api.post('/military/scout', {
        x: parseInt(scoutX, 10),
        y: parseInt(scoutY, 10),
        amount: parseInt(scoutAmount, 10) || 1,
        type: scoutType,
      })
      setScoutResult({ success: true, data: res.data })
      toast.success('Scouts sent successfully!')
    } catch (err) {
      const message = err.response?.data?.detail || err.response?.data?.message || 'Failed to send scouts'
      setScoutResult({ success: false, message })
      toast.error(message)
    } finally {
      setScoutLoading(false)
    }
  }

  // Raid handlers
  function handleRaidClick() {
    if (!raidX || !raidY) {
      toast.error('Please enter X and Y coordinates')
      return
    }
    if (Object.keys(troops).length === 0) {
      toast.error('Please enter at least one troop type')
      return
    }
    setRaidConfirmOpen(true)
  }

  async function sendRaid() {
    setRaidConfirmOpen(false)
    setRaidLoading(true)
    setRaidResult(null)
    try {
      const res = await api.post('/military/raid', {
        x: parseInt(raidX, 10),
        y: parseInt(raidY, 10),
        troops,
      })
      setRaidResult({ success: true, data: res.data })
      toast.success('Raid sent successfully!')
    } catch (err) {
      const message = err.response?.data?.detail || err.response?.data?.message || 'Failed to send raid'
      setRaidResult({ success: false, message })
      toast.error(message)
    } finally {
      setRaidLoading(false)
    }
  }

  function getTotalTroops() {
    return Object.values(troops).reduce((sum, v) => sum + (v || 0), 0)
  }

  if (!connected) {
    return (
      <div className="p-6">
        <h2 className="text-2xl mb-4" style={{ fontFamily: 'Cinzel', color: 'var(--accent-gold)' }}>Military</h2>
        <div className="card" style={{ textAlign: 'center', padding: '2rem' }}>
          <p style={{ color: 'var(--text-secondary)' }}>Connect to a Travian server to use military features.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6 flex-wrap gap-4">
        <h2 className="text-2xl" style={{ fontFamily: 'Cinzel', color: 'var(--accent-gold)' }}>Military</h2>
        <VillageSelector />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Scout Panel */}
        <div className="card">
          <h3
            className="text-lg mb-4"
            style={{ fontFamily: 'Cinzel', color: 'var(--text-primary)' }}
          >
            Scout
          </h3>

          <div className="flex flex-col gap-4">
            {/* Coordinates */}
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <label className="text-sm font-semibold" style={{ color: 'var(--text-secondary)' }}>
                  X Coordinate
                </label>
                <input
                  type="number"
                  className="input-field"
                  placeholder="X"
                  value={scoutX}
                  onChange={(e) => setScoutX(e.target.value)}
                  disabled={scoutLoading}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label className="text-sm font-semibold" style={{ color: 'var(--text-secondary)' }}>
                  Y Coordinate
                </label>
                <input
                  type="number"
                  className="input-field"
                  placeholder="Y"
                  value={scoutY}
                  onChange={(e) => setScoutY(e.target.value)}
                  disabled={scoutLoading}
                />
              </div>
            </div>

            {/* Amount */}
            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-semibold" style={{ color: 'var(--text-secondary)' }}>
                Amount
              </label>
              <input
                type="number"
                className="input-field"
                min="1"
                placeholder="1"
                value={scoutAmount}
                onChange={(e) => setScoutAmount(e.target.value)}
                disabled={scoutLoading}
              />
            </div>

            {/* Scout Type */}
            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-semibold" style={{ color: 'var(--text-secondary)' }}>
                Scout Type
              </label>
              <div className="flex gap-4">
                <label className="flex items-center gap-2 cursor-pointer" style={{ color: 'var(--text-primary)' }}>
                  <input
                    type="radio"
                    name="scoutType"
                    value="resources"
                    checked={scoutType === 'resources'}
                    onChange={(e) => setScoutType(e.target.value)}
                    disabled={scoutLoading}
                    style={{ accentColor: 'var(--accent-gold)' }}
                  />
                  <span className="text-sm">Resources</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer" style={{ color: 'var(--text-primary)' }}>
                  <input
                    type="radio"
                    name="scoutType"
                    value="defenses"
                    checked={scoutType === 'defenses'}
                    onChange={(e) => setScoutType(e.target.value)}
                    disabled={scoutLoading}
                    style={{ accentColor: 'var(--accent-gold)' }}
                  />
                  <span className="text-sm">Defenses</span>
                </label>
              </div>
            </div>

            {/* Send Button */}
            <button
              className="btn-primary w-full flex items-center justify-center gap-2"
              onClick={handleScoutClick}
              disabled={scoutLoading}
              style={{ padding: '0.625rem 1rem' }}
            >
              {scoutLoading && <Spinner />}
              {scoutLoading ? 'Sending Scouts...' : 'Send Scouts'}
            </button>

            {/* Result */}
            {scoutResult && (
              <div
                className="text-sm px-3 py-2 rounded"
                style={{
                  backgroundColor: scoutResult.success
                    ? 'rgba(74, 140, 74, 0.2)'
                    : 'rgba(179, 64, 64, 0.2)',
                  border: `1px solid ${scoutResult.success ? 'var(--success)' : 'var(--danger)'}`,
                  color: scoutResult.success ? '#8c8' : '#e88',
                }}
              >
                {scoutResult.success ? (
                  <div>
                    <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>Scouts dispatched!</div>
                    {scoutResult.data?.travel_time && (
                      <div>Travel time: {scoutResult.data.travel_time}</div>
                    )}
                    {scoutResult.data?.message && (
                      <div>{scoutResult.data.message}</div>
                    )}
                  </div>
                ) : (
                  <div>{scoutResult.message}</div>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Raid Panel */}
        <div className="card">
          <h3
            className="text-lg mb-4"
            style={{ fontFamily: 'Cinzel', color: 'var(--text-primary)' }}
          >
            Raid
          </h3>

          <div className="flex flex-col gap-4">
            {/* Coordinates */}
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <label className="text-sm font-semibold" style={{ color: 'var(--text-secondary)' }}>
                  X Coordinate
                </label>
                <input
                  type="number"
                  className="input-field"
                  placeholder="X"
                  value={raidX}
                  onChange={(e) => setRaidX(e.target.value)}
                  disabled={raidLoading}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label className="text-sm font-semibold" style={{ color: 'var(--text-secondary)' }}>
                  Y Coordinate
                </label>
                <input
                  type="number"
                  className="input-field"
                  placeholder="Y"
                  value={raidY}
                  onChange={(e) => setRaidY(e.target.value)}
                  disabled={raidLoading}
                />
              </div>
            </div>

            {/* Troop Grid */}
            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-semibold" style={{ color: 'var(--text-secondary)' }}>
                Troops {getTotalTroops() > 0 && (
                  <span style={{ color: 'var(--accent-gold)', fontWeight: 400 }}>
                    ({getTotalTroops()} total)
                  </span>
                )}
              </label>
              <div className="grid grid-cols-2 gap-2">
                {troopNames.map((name, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <label
                      className="text-xs flex-shrink-0"
                      style={{
                        color: 'var(--text-secondary)',
                        width: '120px',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                      title={name}
                    >
                      {name}
                    </label>
                    <input
                      type="number"
                      className="input-field"
                      min="0"
                      placeholder="0"
                      value={getTroopCount(i)}
                      onChange={(e) => setTroopCount(i, e.target.value)}
                      disabled={raidLoading}
                      style={{ padding: '0.25rem 0.5rem', fontSize: '0.8rem' }}
                    />
                  </div>
                ))}
              </div>
            </div>

            {/* Send Button */}
            <button
              className="btn-primary w-full flex items-center justify-center gap-2"
              onClick={handleRaidClick}
              disabled={raidLoading}
              style={{ padding: '0.625rem 1rem' }}
            >
              {raidLoading && <Spinner />}
              {raidLoading ? 'Sending Raid...' : 'Send Raid'}
            </button>

            {/* Result */}
            {raidResult && (
              <div
                className="text-sm px-3 py-2 rounded"
                style={{
                  backgroundColor: raidResult.success
                    ? 'rgba(74, 140, 74, 0.2)'
                    : 'rgba(179, 64, 64, 0.2)',
                  border: `1px solid ${raidResult.success ? 'var(--success)' : 'var(--danger)'}`,
                  color: raidResult.success ? '#8c8' : '#e88',
                }}
              >
                {raidResult.success ? (
                  <div>
                    <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>Raid dispatched!</div>
                    {raidResult.data?.travel_time && (
                      <div>Travel time: {raidResult.data.travel_time}</div>
                    )}
                    {raidResult.data?.message && (
                      <div>{raidResult.data.message}</div>
                    )}
                  </div>
                ) : (
                  <div>{raidResult.message}</div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Confirm Dialogs */}
      <ConfirmDialog
        open={scoutConfirmOpen}
        title="Send Scouts"
        message={`Send ${scoutAmount || 1} scout(s) to (${scoutX}, ${scoutY}) for ${scoutType}?`}
        confirmText="Send"
        onConfirm={sendScout}
        onCancel={() => setScoutConfirmOpen(false)}
      />
      <ConfirmDialog
        open={raidConfirmOpen}
        title="Send Raid"
        message={`Send ${getTotalTroops()} troops to raid (${raidX}, ${raidY})?`}
        confirmText="Send Raid"
        onConfirm={sendRaid}
        onCancel={() => setRaidConfirmOpen(false)}
        variant="danger"
      />
    </div>
  )
}
