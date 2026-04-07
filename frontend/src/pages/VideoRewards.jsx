import { useState } from 'react'
import api from '../api'
import useGameStore from '../stores/gameStore'
import { useToast } from '../components/Toast'

const REWARD_TYPES = [
  { key: 'buildingUpgrade', label: 'Building Upgrade', description: 'Speed up your current building upgrade' },
  { key: 'productionBoost', label: 'Production Boost', description: 'Boost all resource production' },
  { key: 'lumberProductionBonus', label: 'Lumber Bonus', description: 'Increase lumber production' },
  { key: 'clayProductionBonus', label: 'Clay Bonus', description: 'Increase clay production' },
  { key: 'ironProductionBonus', label: 'Iron Bonus', description: 'Increase iron production' },
  { key: 'cropProductionBonus', label: 'Crop Bonus', description: 'Increase crop production' },
]

function RewardCard({ reward, onClaim, claiming, result }) {
  return (
    <div className="card flex flex-col justify-between min-h-[160px]">
      <div>
        <h4 className="heading-gold text-sm font-semibold mb-1">
          {reward.label}
        </h4>
        <p className="text-xs text-secondary mb-3">
          {reward.description}
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <button
          className="btn-primary btn-sm w-full flex items-center justify-center gap-2 text-sm"
          onClick={() => onClaim(reward.key)}
          disabled={claiming}
        >
          {claiming && <span className="spinner spinner-sm" />}
          {claiming ? 'Claiming...' : 'Claim'}
        </button>

        {result && (
          <div
            className={`text-xs px-2 py-1.5 rounded result-box ${
              result.success ? 'result-box-success' : 'result-box-danger'
            }`}
          >
            {result.success ? (result.message || 'Claimed!') : (result.message || 'Failed')}
          </div>
        )}
      </div>
    </div>
  )
}

export default function VideoRewards() {
  const toast = useToast()
  const connected = useGameStore((s) => s.connected)

  // Per-reward state
  const [claimingType, setClaimingType] = useState(null)
  const [results, setResults] = useState({})

  // Claim All state
  const [claimingAll, setClaimingAll] = useState(false)
  const [claimAllProgress, setClaimAllProgress] = useState('')
  const [claimAllResult, setClaimAllResult] = useState(null)

  async function handleClaim(type) {
    setClaimingType(type)
    setResults((prev) => {
      const next = { ...prev }
      delete next[type]
      return next
    })

    try {
      const res = await api.post('/video/claim', { type })
      const message = res.data?.message || res.data?.detail || 'Reward claimed!'
      setResults((prev) => ({ ...prev, [type]: { success: true, message } }))
      toast.success(`${REWARD_TYPES.find((r) => r.key === type)?.label || type}: ${message}`)
      useGameStore.getState().fetchResources()
    } catch (err) {
      const message = err.response?.data?.detail || err.response?.data?.message || 'Failed to claim reward'
      setResults((prev) => ({ ...prev, [type]: { success: false, message } }))
      toast.error(message)
    } finally {
      setClaimingType(null)
    }
  }

  async function handleClaimAll() {
    setClaimingAll(true)
    setClaimAllResult(null)
    setClaimAllProgress('Starting claim all...')

    try {
      const res = await api.post('/video/claim-all')
      const data = res.data

      // Handle different response shapes
      if (data?.results && typeof data.results === 'object') {
        // Backend returned per-type results
        const entries = Array.isArray(data.results) ? data.results : Object.entries(data.results)
        let successCount = 0
        let failCount = 0

        if (Array.isArray(entries) && entries.length > 0) {
          if (typeof entries[0] === 'object' && !Array.isArray(entries[0])) {
            // Array of result objects
            entries.forEach((item) => {
              const type = item.type || item.key
              const success = item.success !== false && !item.error
              if (success) successCount++
              else failCount++
              if (type) {
                setResults((prev) => ({
                  ...prev,
                  [type]: {
                    success,
                    message: item.message || item.error || (success ? 'Claimed!' : 'Failed'),
                  },
                }))
              }
            })
          } else {
            // Array of [key, value] pairs
            entries.forEach(([key, value]) => {
              const success = value?.success !== false && !value?.error
              if (success) successCount++
              else failCount++
              setResults((prev) => ({
                ...prev,
                [key]: {
                  success,
                  message: value?.message || value?.error || (success ? 'Claimed!' : 'Failed'),
                },
              }))
            })
          }
        }

        setClaimAllResult({
          success: true,
          message: `Completed: ${successCount} succeeded, ${failCount} failed`,
        })
      } else {
        // Simple response
        const message = data?.message || 'All production boosts claimed!'
        setClaimAllResult({ success: true, message })
      }

      toast.success('Claim all completed!')
      useGameStore.getState().fetchResources()
    } catch (err) {
      const message = err.response?.data?.detail || err.response?.data?.message || 'Failed to claim all rewards'
      setClaimAllResult({ success: false, message })
      toast.error(message)
    } finally {
      setClaimingAll(false)
      setClaimAllProgress('')
    }
  }

  if (!connected) {
    return (
      <div className="p-6">
        <h2 className="heading-gold text-2xl mb-4">Video Rewards</h2>
        <div className="card text-center p-8">
          <p className="text-secondary">Connect to a Travian server to claim video rewards.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6 flex-wrap gap-4">
        <h2 className="heading-gold text-2xl">Video Rewards</h2>
      </div>

      {/* Claim All Section */}
      <div className="card mb-6">
        <div className="flex items-center justify-between flex-wrap gap-4">
          <div>
            <h3 className="text-base font-semibold text-primary">
              Claim All Production Boosts
            </h3>
            <p className="text-xs mt-1 text-secondary">
              Attempt to claim all available production boost rewards at once.
            </p>
          </div>
          <button
            className="btn-primary flex items-center gap-2 whitespace-nowrap"
            onClick={handleClaimAll}
            disabled={claimingAll || claimingType !== null}
          >
            {claimingAll && <span className="spinner spinner-sm" />}
            {claimingAll ? 'Claiming All...' : 'Claim All'}
          </button>
        </div>

        {/* Progress */}
        {claimingAll && claimAllProgress && (
          <div className="text-xs mt-3 px-3 py-2 rounded result-box result-box-info">
            {claimAllProgress}
          </div>
        )}

        {/* Result */}
        {claimAllResult && (
          <div
            className={`text-sm mt-3 px-3 py-2 rounded result-box ${
              claimAllResult.success ? 'result-box-success' : 'result-box-danger'
            }`}
          >
            {claimAllResult.message}
          </div>
        )}
      </div>

      {/* Reward Cards Grid */}
      <div className="reward-grid">
        {REWARD_TYPES.map((reward) => (
          <RewardCard
            key={reward.key}
            reward={reward}
            onClaim={handleClaim}
            claiming={claimingType === reward.key || claimingAll}
            result={results[reward.key]}
          />
        ))}
      </div>
    </div>
  )
}
