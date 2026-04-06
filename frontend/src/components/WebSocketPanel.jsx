import { useEffect, useRef } from 'react'

const typeClassMap = {
  info: 'text-primary',
  success: 'text-success',
  error: 'text-danger',
  warning: 'text-warning',
}

const statusConfig = {
  connected: { label: 'Connected', dotClass: 'status-dot-success' },
  disconnected: { label: 'Disconnected', dotClass: 'status-dot-danger' },
  running: { label: 'Running', dotClass: 'status-dot-warning' },
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
    <div className="bg-base border-default rounded-lg overflow-hidden flex flex-col">
      {/* Header */}
      <div className="flex justify-between items-center px-3 py-2 bg-surface border-b-default">
        <div className="flex items-center gap-2 text-xs">
          <span className={`status-dot ${st.dotClass}`} />
          <span className="text-secondary">{st.label}</span>
        </div>
        {onClear && (
          <button
            onClick={onClear}
            className="btn-secondary btn-xs"
          >
            Clear
          </button>
        )}
      </div>

      {/* Log area */}
      <div
        ref={scrollRef}
        className="ws-panel"
      >
        {messages.length === 0 ? (
          <div className="text-secondary italic">
            No messages yet...
          </div>
        ) : (
          messages.map((msg, i) => (
            <div key={i} className={`ws-panel-line ${typeClassMap[msg.type] || 'text-primary'}`}>
              <span className="ws-panel-time">
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
