import { useState, useEffect } from 'react'

const STEPS = [
  { key: 'connect', label: 'Connecting to server', detail: 'Establishing secure connection...' },
  { key: 'auth', label: 'Authenticating', detail: 'Logging in to Travian...' },
  { key: 'villages', label: 'Loading villages', detail: 'Fetching player data...' },
  { key: 'ready', label: 'Ready', detail: 'Redirecting to dashboard...' },
]

/**
 * Animated connection progress overlay.
 * Advances through steps on a timer to give the impression of progress,
 * since the backend connect call is a single blocking request.
 */
export default function ConnectionProgress({ serverName, isActive }) {
  const [step, setStep] = useState(0)

  useEffect(() => {
    if (!isActive) { setStep(0); return }
    // Advance through "visual" steps on a timer.
    // Step 0 immediately, step 1 after 1.2s, step 2 after 3s
    const timers = [
      setTimeout(() => setStep(1), 1200),
      setTimeout(() => setStep(2), 3000),
    ]
    return () => timers.forEach(clearTimeout)
  }, [isActive])

  // Called externally when connect actually succeeds
  useEffect(() => {
    if (!isActive) return
    // The parent will unmount us on navigate, so step 3 is just visual
  }, [isActive])

  if (!isActive) return null

  return (
    <div className="fixed inset-0 z-50 bg-base/95 flex items-center justify-center">
      <div className="w-full max-w-md px-6">
        {/* Title */}
        <div className="text-center mb-8">
          <h2 className="logo-text-lg text-xl mb-2">Connecting</h2>
          {serverName && <p className="text-sm text-secondary truncate">{serverName}</p>}
        </div>

        {/* Steps */}
        <div className="flex flex-col gap-1">
          {STEPS.map((s, i) => {
            const isDone = i < step
            const isCurrent = i === step
            const isPending = i > step

            return (
              <div
                key={s.key}
                className={`flex items-center gap-3 px-4 py-2.5 rounded-lg transition-all duration-300 ${
                  isCurrent ? 'bg-surface border-default' : ''
                } ${isPending ? 'opacity-30' : ''}`}
              >
                {/* Icon */}
                <div className="w-6 h-6 flex items-center justify-center shrink-0">
                  {isDone ? (
                    <span className="text-success text-lg">&#10003;</span>
                  ) : isCurrent ? (
                    <div className="spinner spinner-sm" />
                  ) : (
                    <span className="w-2 h-2 rounded-full bg-secondary/30 block" />
                  )}
                </div>

                {/* Text */}
                <div className="flex-1 min-w-0">
                  <div className={`text-sm font-medium ${isDone ? 'text-success' : isCurrent ? 'text-primary' : 'text-secondary'}`}>
                    {s.label}
                  </div>
                  {isCurrent && (
                    <div className="text-xs text-secondary mt-0.5 animate-pulse">{s.detail}</div>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {/* Subtle progress bar */}
        <div className="mt-6 h-1 bg-surface rounded-full overflow-hidden">
          <div
            className="h-full bg-gold rounded-full transition-all duration-1000 ease-out"
            style={{ width: `${((step + 1) / STEPS.length) * 100}%` }}
          />
        </div>
      </div>
    </div>
  )
}

/** Trigger the "Ready" step from outside */
ConnectionProgress.STEP_COUNT = STEPS.length
