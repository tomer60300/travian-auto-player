import { useState, useEffect } from 'react'

/**
 * StaleChip — shows when data was fetched more than `threshold` seconds ago.
 *
 * Props:
 * @param {number} fetchedAt - timestamp of last fetch
 * @param {number} [threshold=60] - seconds before showing stale indicator
 * @param {function} onRefresh - callback to re-fetch
 */
export default function StaleChip({ fetchedAt, threshold = 60, onRefresh }) {
  const [isStale, setIsStale] = useState(false)

  useEffect(() => {
    if (!fetchedAt) {
      setIsStale(false)
      return
    }

    // Check immediately
    setIsStale(Date.now() - fetchedAt > threshold * 1000)

    // Re-check every 10 seconds
    const interval = setInterval(() => {
      setIsStale(Date.now() - fetchedAt > threshold * 1000)
    }, 10000)

    return () => clearInterval(interval)
  }, [fetchedAt, threshold])

  if (!isStale) return null

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '0.35rem',
        padding: '0.2rem 0.6rem',
        fontSize: '0.75rem',
        borderRadius: '0.375rem',
        backgroundColor: 'rgba(196, 129, 47, 0.12)',
        border: '1px solid var(--warning)',
        color: 'var(--warning)',
      }}
    >
      <span>Data may be stale</span>
      <span style={{ opacity: 0.4 }}>&middot;</span>
      <button
        onClick={onRefresh}
        style={{
          background: 'none',
          border: 'none',
          padding: 0,
          color: 'var(--warning)',
          cursor: 'pointer',
          fontWeight: 600,
          fontSize: 'inherit',
          textDecoration: 'underline',
          textUnderlineOffset: '2px',
        }}
      >
        Refresh
      </button>
    </span>
  )
}
