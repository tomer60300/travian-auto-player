import { useState, useEffect, useRef } from 'react'

/**
 * Connection progress overlay.
 *
 * The backend connect is a SINGLE blocking HTTP call that does:
 *   1. DNS + TLS to Travian server
 *   2. Login (username/password via stealth-throttled HTTP)
 *   3. Parse response → extract player, tribe, villages
 *
 * We can't get real mid-flight progress, so this component shows an
 * honest indeterminate progress with a pulsing status indicator.
 * The steps advance on a timer but the overall feel is "working…"
 * rather than pretending we know exact progress.
 */

export default function ConnectionProgress({ serverName, isActive }) {
  const [visible, setVisible] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const startRef = useRef(null)

  // Fade in/out
  useEffect(() => {
    if (isActive) {
      startRef.current = Date.now()
      setElapsed(0)
      requestAnimationFrame(() => setVisible(true))
    } else {
      setVisible(false)
      const t = setTimeout(() => { setElapsed(0); startRef.current = null }, 300)
      return () => clearTimeout(t)
    }
  }, [isActive])

  // Elapsed timer
  useEffect(() => {
    if (!isActive) return
    const id = setInterval(() => {
      if (startRef.current) setElapsed(Math.floor((Date.now() - startRef.current) / 1000))
    }, 1000)
    return () => clearInterval(id)
  }, [isActive])

  if (!isActive && !visible) return null

  const statusText = elapsed < 3
    ? 'Establishing connection…'
    : elapsed < 8
      ? 'Authenticating with server…'
      : elapsed < 15
        ? 'Loading player data…'
        : 'Still working — stealth delays active…'

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 50,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: 'var(--bg-base)',
        opacity: visible ? 1 : 0,
        transition: 'opacity 300ms cubic-bezier(0.2, 0, 0, 1)',
      }}
    >
      {/* Atmospheric blurs */}
      <div style={{ position: 'absolute', inset: 0, overflow: 'hidden', pointerEvents: 'none' }} aria-hidden="true">
        <div style={{
          position: 'absolute', width: 400, height: 400, top: '20%', left: '50%',
          transform: 'translateX(-50%)', borderRadius: '50%',
          background: 'var(--md-primary)', opacity: 0.06, filter: 'blur(80px)',
        }} />
        <div style={{
          position: 'absolute', width: 300, height: 300, bottom: '10%', right: '20%',
          borderRadius: '50%', background: 'var(--md-tertiary)', opacity: 0.05, filter: 'blur(60px)',
        }} />
      </div>

      <div style={{ width: '100%', maxWidth: 380, padding: '0 1.5rem', position: 'relative', zIndex: 1, textAlign: 'center' }}>
        {/* Pulsing connection icon */}
        <div style={{
          width: 80, height: 80, margin: '0 auto 24px', borderRadius: 24,
          background: 'var(--md-primary-container)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          animation: 'status-pulse 2s ease-in-out infinite',
        }}>
          <span className="spinner" style={{ width: 32, height: 32, borderWidth: 3 }} />
        </div>

        {/* Title */}
        <h2 style={{
          fontFamily: "'Roboto', system-ui, sans-serif", fontWeight: 500,
          fontSize: '1.35rem', color: 'var(--text-primary)', margin: '0 0 6px',
        }}>
          Connecting
        </h2>

        {/* Server name */}
        {serverName && (
          <p style={{
            fontSize: '0.85rem', color: 'var(--md-primary)', margin: '0 0 20px',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 500,
          }}>
            {serverName}
          </p>
        )}

        {/* Status card */}
        <div style={{
          background: 'var(--bg-card)', borderRadius: 20, padding: '20px 24px',
          boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
        }}>
          {/* Current activity */}
          <p style={{
            fontSize: '0.875rem', color: 'var(--text-secondary)', margin: '0 0 16px',
            minHeight: 20, transition: 'opacity 200ms',
          }}>
            {statusText}
          </p>

          {/* Indeterminate progress bar */}
          <div style={{
            height: 4, borderRadius: 9999, backgroundColor: 'var(--md-surface-container-high)',
            overflow: 'hidden', position: 'relative',
          }}>
            <div style={{
              position: 'absolute', top: 0, left: 0, height: '100%', width: '40%',
              borderRadius: 9999, backgroundColor: 'var(--md-primary)',
              animation: 'indeterminate-bar 1.8s cubic-bezier(0.65, 0, 0.35, 1) infinite',
            }} />
          </div>

          {/* Elapsed time */}
          <p style={{
            fontSize: '0.75rem', color: 'var(--text-secondary)', margin: '12px 0 0',
            opacity: 0.7, fontVariantNumeric: 'tabular-nums',
          }}>
            {elapsed > 0 ? `${elapsed}s elapsed` : ''}
          </p>
        </div>

        {/* Hint for long waits */}
        {elapsed >= 10 && (
          <p style={{
            fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: 16, opacity: 0.6,
            animation: 'fade-in 300ms ease',
          }}>
            Stealth mode adds delays between requests to avoid detection
          </p>
        )}
      </div>

      {/* Indeterminate bar animation */}
      <style>{`
        @keyframes indeterminate-bar {
          0% { left: -40%; }
          100% { left: 100%; }
        }
      `}</style>
    </div>
  )
}

ConnectionProgress.STEP_COUNT = 4
