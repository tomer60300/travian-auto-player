import { useState } from 'react'
import api from '../api'
import useGameStore from '../stores/gameStore'
import { useToast } from '../components/Toast'
import ConfirmDialog from '../components/ConfirmDialog'
import { TRIBE_TROOPS, DEFAULT_TROOPS } from '../constants/troops'

export default function Military() {
  const toast = useToast()
  const tribeId = useGameStore((s) => s.tribeId)
  const connected = useGameStore((s) => s.connected)
  const activeVillageId = useGameStore((s) => s.activeVillageId)

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
  const [showAllTroops, setShowAllTroops] = useState(false)
  const [raidLoading, setRaidLoading] = useState(false)
  const [raidResult, setRaidResult] = useState(null)
  const [raidConfirmOpen, setRaidConfirmOpen] = useState(false)

  const troopNames = TRIBE_TROOPS[tribeId] || DEFAULT_TROOPS

  function setTroopCount(index, value) {
    const key = `t${index + 1}`
    const num = parseInt(value, 10)
    setTroops((prev) => {
      const next = { ...prev }
      if (!value || isNaN(num) || num <= 0) {
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
    const x = parseInt(scoutX, 10)
    const y = parseInt(scoutY, 10)
    if (isNaN(x) || isNaN(y)) {
      toast.error('Coordinates must be valid numbers')
      return
    }
    if (x < -400 || x > 400 || y < -400 || y > 400) {
      toast.error('Coordinates must be between -400 and 400')
      return
    }
    const amt = parseInt(scoutAmount, 10) || 1
    if (amt < 1 || amt > 1000) {
      toast.error('Scout amount must be between 1 and 1000')
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
        // The backend default is forever the login village (switching is
        // client-side only), so the selected village must travel explicitly.
        village_id: activeVillageId ?? undefined,
      })
      setScoutResult({ success: true, data: res.data })
      toast.success('Scouts sent successfully!')
      useGameStore.getState().fetchResources()
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
    const x = parseInt(raidX, 10)
    const y = parseInt(raidY, 10)
    if (isNaN(x) || isNaN(y)) {
      toast.error('Coordinates must be valid numbers')
      return
    }
    if (x < -400 || x > 400 || y < -400 || y > 400) {
      toast.error('Coordinates must be between -400 and 400')
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
        village_id: activeVillageId ?? undefined,
      })
      setRaidResult({ success: true, data: res.data })
      toast.success('Raid sent successfully!')
      useGameStore.getState().fetchResources()
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
        <h2 className="heading-gold text-2xl mb-4">Military</h2>
        <div className="card text-center p-8">
          <p className="text-secondary">Connect to a Travian server to use military features.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6 flex-wrap gap-4">
        <h2 className="heading-gold text-2xl">Military</h2>
        {/* No <VillageSelector/> here. The page used to embed its own, which
            duplicated the layout's -- same store action, same "Active village"
            name, so two comboboxes with that exact name were visible at once at
            every width. Same fix AutoScout took in 5a62dd1; the
            sidebar/mobile-top-bar selector already covers every breakpoint (see
            components/Layout.jsx). */}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Scout Panel */}
        <div className="card">
          <h3 className="text-lg mb-4 text-primary">Scout</h3>

          <div className="flex flex-col gap-4">
            {/* Coordinates */}
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <label className="text-sm font-semibold text-secondary">
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
                <label className="text-sm font-semibold text-secondary">
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
              <label className="text-sm font-semibold text-secondary">
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
              <label className="text-sm font-semibold text-secondary">
                Scout Type
              </label>
              <div className="flex gap-4">
                <label className="flex items-center gap-2 cursor-pointer text-primary">
                  <input
                    type="radio"
                    name="scoutType"
                    value="resources"
                    checked={scoutType === 'resources'}
                    onChange={(e) => setScoutType(e.target.value)}
                    disabled={scoutLoading}
                    className="accent-radio"
                  />
                  <span className="text-sm">Resources</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer text-primary">
                  <input
                    type="radio"
                    name="scoutType"
                    value="defenses"
                    checked={scoutType === 'defenses'}
                    onChange={(e) => setScoutType(e.target.value)}
                    disabled={scoutLoading}
                    className="accent-radio"
                  />
                  <span className="text-sm">Defenses</span>
                </label>
              </div>
            </div>

            {/* Send Button */}
            <button
              className="btn-primary btn-full flex items-center justify-center gap-2"
              onClick={handleScoutClick}
              disabled={scoutLoading}
            >
              {scoutLoading && <span className="spinner spinner-sm" />}
              {scoutLoading ? 'Sending Scouts...' : 'Send Scouts'}
            </button>

            {/* Result */}
            {scoutResult && (
              <div
                className={`result-box text-sm ${scoutResult.success ? 'result-box-success' : 'result-box-danger'}`}
              >
                {scoutResult.success ? (
                  <div>
                    <div className="font-semibold mb-1">Scouts dispatched!</div>
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
          <h3 className="text-lg mb-4 text-primary">Raid</h3>

          <div className="flex flex-col gap-4">
            {/* Coordinates */}
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <label className="text-sm font-semibold text-secondary">
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
                <label className="text-sm font-semibold text-secondary">
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
              <div className="flex items-center justify-between">
                <label className="text-sm font-semibold text-secondary">
                  Troops {getTotalTroops() > 0 && (
                    <span className="text-gold font-normal">
                      ({getTotalTroops()} total)
                    </span>
                  )}
                </label>
                <label className="check-label-secondary text-xs cursor-pointer">
                  <input
                    type="checkbox"
                    checked={showAllTroops}
                    onChange={(e) => setShowAllTroops(e.target.checked)}
                  />
                  Show all
                </label>
              </div>
              <div className="grid grid-cols-2 gap-2">
                {troopNames.map((name, i) => {
                  const hasValue = getTroopCount(i) !== '' && Number(getTroopCount(i)) > 0
                  if (!showAllTroops && !hasValue) return null
                  return (
                    <div key={i} className="flex items-center gap-2">
                      <label
                        className="text-xs text-secondary shrink-0 w-30 truncate"
                        title={name}
                      >
                        {name}
                      </label>
                      <input
                        type="number"
                        className="input-troop"
                        min="0"
                        placeholder="0"
                        value={getTroopCount(i)}
                        onChange={(e) => setTroopCount(i, e.target.value)}
                        disabled={raidLoading}
                      />
                    </div>
                  )
                })}
                {!showAllTroops && getTotalTroops() === 0 && (
                  <p className="text-xs text-secondary col-span-2 italic">
                    Enable "Show all" to add troops
                  </p>
                )}
              </div>
            </div>

            {/* Send Button */}
            <button
              className="btn-primary btn-full flex items-center justify-center gap-2"
              onClick={handleRaidClick}
              disabled={raidLoading}
            >
              {raidLoading && <span className="spinner spinner-sm" />}
              {raidLoading ? 'Sending Raid...' : 'Send Raid'}
            </button>

            {/* Result */}
            {raidResult && (
              <div
                className={`result-box text-sm ${raidResult.success ? 'result-box-success' : 'result-box-danger'}`}
              >
                {raidResult.success ? (
                  <div>
                    <div className="font-semibold mb-1">Raid dispatched!</div>
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
