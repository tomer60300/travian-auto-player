/**
 * OperationCard — Reusable card showing real-time progress of a long-running operation.
 *
 * Props:
 *   title          (string)         — operation name, e.g. "Building Queue"
 *   subtitle       (string)         — secondary label, e.g. "Village 1"
 *   status         (string)         — 'running' | 'waiting' | 'success' | 'error' | 'idle'
 *   statusLabel    (string?)        — override for badge text (default: capitalised status)
 *   stepText       (string?)        — current step description
 *   progress       (number?)        — 0-100 percentage (null = indeterminate)
 *   startedAt      (number?)        — timestamp when operation started (elapsed timer)
 *   lastUpdate     (number?)        — timestamp of last WS message
 *   estimatedRemaining (string?)    — e.g. "~2m"
 *   errorMessage   (string?)        — shown when status is 'error'
 *   onRetry        (function?)      — retry callback (shown on error)
 *   children       (ReactNode?)     — extra content rendered below the card
 */
import { useState, useEffect } from 'react'

/**
 * Format elapsed time from milliseconds into a human-readable string.
 * e.g. 83000 -> "1m 23s", 45000 -> "45s", 3661000 -> "1h 1m 1s"
 */
function formatElapsed(ms) {
  if (ms < 0) ms = 0
  const totalSec = Math.floor(ms / 1000)
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

/**
 * Format "time ago" from a timestamp into a human-readable string.
 * e.g. 3000 -> "3s ago", 65000 -> "1m ago"
 */
function formatAgo(ms) {
  if (ms < 0) ms = 0
  const totalSec = Math.floor(ms / 1000)
  if (totalSec < 60) return `${totalSec}s ago`
  const m = Math.floor(totalSec / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  return `${h}h ago`
}

/**
 * Status dot color mapped to CSS custom properties.
 */
const STATUS_DOT_COLORS = {
  running: 'var(--status-running)',
  waiting: 'var(--status-waiting)',
  success: 'var(--status-success)',
  error: 'var(--status-error)',
  idle: 'var(--status-idle)',
}

/**
 * OperationCard — shows real-time status of a long-running operation.
 *
 * Props:
 * @param {string} title - e.g. "Building Queue", "Farm Lists"
 * @param {string} subtitle - e.g. "Village 1"
 * @param {string} status - 'running' | 'waiting' | 'success' | 'error' | 'idle'
 * @param {string} [statusLabel] - override for badge text (default: capitalize status)
 * @param {string} [stepText] - current step description, e.g. "Step 3/7: Upgrading Barracks to Lv5"
 * @param {number} [progress] - 0-100 percentage (null = indeterminate)
 * @param {number} [startedAt] - timestamp when operation started (for elapsed timer)
 * @param {number} [lastUpdate] - timestamp of last WS message (for "last update" display)
 * @param {string} [estimatedRemaining] - e.g. "~2m" or null
 * @param {string} [errorMessage] - shown when status is 'error'
 * @param {function} [onRetry] - callback for retry button (shown when status is 'error')
 * @param {React.ReactNode} [children] - optional extra content below the card
 */
export default function OperationCard({
  title,
  subtitle,
  status = 'idle',
  statusLabel,
  stepText,
  progress,
  startedAt,
  lastUpdate,
  estimatedRemaining,
  errorMessage,
  onRetry,
  children,
}) {
  const [now, setNow] = useState(Date.now())

  // Tick every second to update elapsed time and "last update" display
  useEffect(() => {
    const isActive = status === 'running' || status === 'waiting'
    if (!isActive && !lastUpdate) return

    const interval = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(interval)
  }, [status, lastUpdate])

  const badgeText = statusLabel || status.charAt(0).toUpperCase() + status.slice(1)
  const isPulsing = status === 'running' || status === 'waiting'
  const dotColor = STATUS_DOT_COLORS[status] || STATUS_DOT_COLORS.idle

  const elapsed = startedAt ? now - startedAt : null
  const lastUpdateAgo = lastUpdate ? now - lastUpdate : null

  return (
    <div className="card">
      {/* Header row: dot + title + subtitle ... status badge */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: stepText || progress != null ? '0.75rem' : 0 }}>
        {/* Status dot */}
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            backgroundColor: dotColor,
            flexShrink: 0,
          }}
        />

        {/* Title and subtitle */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <span style={{ fontWeight: 600, fontSize: '0.95rem' }}>
            {title}
            {subtitle && (
              <span className="text-secondary" style={{ fontWeight: 400, fontSize: '0.85rem' }}>
                {' '}&mdash; {subtitle}
              </span>
            )}
          </span>
        </div>

        {/* Status badge */}
        <span
          className={`status-badge status-${status}${isPulsing ? ' status-pulse' : ''}`}
          style={{ whiteSpace: 'nowrap' }}
        >
          {badgeText}
        </span>
      </div>

      {/* Step text */}
      {stepText && (
        <div className="text-secondary" style={{ fontSize: '0.85rem', marginBottom: '0.5rem' }}>
          {stepText}
        </div>
      )}

      {/* Progress bar */}
      {(status === 'running' || status === 'waiting' || progress != null) && (
        <div className="progress-track" style={{ marginBottom: '0.5rem' }}>
          {progress != null ? (
            /* Determinate progress */
            <div
              className="progress-fill"
              style={{ width: `${Math.min(100, Math.max(0, progress))}%` }}
            />
          ) : (
            /* Indeterminate shimmer bar */
            <div
              className="progress-fill"
              style={{
                width: '30%',
                animation: 'shimmer 1.5s ease-in-out infinite',
                background: 'linear-gradient(90deg, var(--accent-gold) 0%, var(--accent-gold-hover) 50%, var(--accent-gold) 100%)',
                backgroundSize: '200% 100%',
              }}
            />
          )}
        </div>
      )}

      {/* Progress percentage text (only for determinate) */}
      {progress != null && (status === 'running' || status === 'waiting') && (
        <div className="text-secondary" style={{ fontSize: '0.75rem', marginBottom: '0.5rem', textAlign: 'right' }}>
          {Math.round(progress)}%
        </div>
      )}

      {/* Metadata row: last update | elapsed | estimated */}
      {(lastUpdateAgo != null || elapsed != null || estimatedRemaining) && (
        <div
          className="text-secondary"
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: '0.5rem',
            fontSize: '0.75rem',
            borderTop: '1px solid var(--border)',
            paddingTop: '0.5rem',
            marginTop: '0.25rem',
          }}
        >
          {lastUpdateAgo != null && (
            <span>Last update: {formatAgo(lastUpdateAgo)}</span>
          )}
          {lastUpdateAgo != null && elapsed != null && (
            <span style={{ opacity: 0.4 }}>|</span>
          )}
          {elapsed != null && (
            <span>Elapsed: {formatElapsed(elapsed)}</span>
          )}
          {(lastUpdateAgo != null || elapsed != null) && estimatedRemaining && (
            <span style={{ opacity: 0.4 }}>|</span>
          )}
          {estimatedRemaining && (
            <span>Est: {estimatedRemaining}</span>
          )}
        </div>
      )}

      {/* Error state */}
      {status === 'error' && errorMessage && (
        <div className="error-box" style={{ marginTop: '0.75rem' }}>
          {errorMessage}
        </div>
      )}
      {status === 'error' && onRetry && (
        <div style={{ marginTop: '0.5rem' }}>
          <button className="btn-secondary btn-sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}

      {/* Optional extra content */}
      {children && (
        <div style={{ marginTop: '0.75rem' }}>
          {children}
        </div>
      )}
    </div>
  )
}
