import { useEffect, useRef } from 'react'

const typeColors = {
  info: 'var(--text-primary)',
  success: 'var(--success)',
  error: 'var(--danger)',
  warning: 'var(--warning)',
}

const statusConfig = {
  connected: { label: 'Connected', color: 'var(--success)' },
  disconnected: { label: 'Disconnected', color: 'var(--danger)' },
  running: { label: 'Running', color: 'var(--warning)' },
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleTimeString('en-US', { hour12: false })
}

export default function WebSocketPanel({ messages = [], status = 'disconnected', onClear }) {
  const scrollRef = useRef(null)

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages])

  const st = statusConfig[status] || statusConfig.disconnected

  return (
    <div
      style={{
        backgroundColor: 'var(--bg-base)',
        border: '1px solid var(--border)',
        borderRadius: '0.5rem',
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* Header */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '0.5rem 0.75rem',
          backgroundColor: 'var(--bg-surface)',
          borderBottom: '1px solid var(--border)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.8rem' }}>
          <span
            style={{
              width: '8px',
              height: '8px',
              borderRadius: '50%',
              backgroundColor: st.color,
              display: 'inline-block',
            }}
          />
          <span style={{ color: 'var(--text-secondary)' }}>{st.label}</span>
        </div>
        {onClear && (
          <button
            onClick={onClear}
            style={{
              background: 'none',
              border: '1px solid var(--border)',
              color: 'var(--text-secondary)',
              fontSize: '0.75rem',
              padding: '0.2rem 0.5rem',
              borderRadius: '0.25rem',
              cursor: 'pointer',
            }}
          >
            Clear
          </button>
        )}
      </div>

      {/* Log area */}
      <div
        ref={scrollRef}
        style={{
          maxHeight: '300px',
          overflowY: 'auto',
          padding: '0.5rem 0.75rem',
          fontFamily: 'monospace',
          fontSize: '0.8rem',
          lineHeight: 1.6,
        }}
      >
        {messages.length === 0 ? (
          <div style={{ color: 'var(--text-secondary)', fontStyle: 'italic' }}>
            No messages yet...
          </div>
        ) : (
          messages.map((msg, i) => (
            <div key={i} style={{ color: typeColors[msg.type] || typeColors.info }}>
              <span style={{ color: 'var(--text-secondary)', marginRight: '0.5rem' }}>
                [{formatTime(msg.timestamp)}]
              </span>
              {msg.text}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
