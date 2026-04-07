import { useState, useEffect, useRef, useMemo } from 'react'
import useLogStore from '../stores/logStore'

const LEVEL_CLASS = {
  info: 'text-primary',
  success: 'text-success',
  warning: 'text-warning',
  error: 'text-danger',
}

const SOURCE_LABELS = {
  api: 'API',
  auth: 'Auth',
  game: 'Game',
  military: 'Military',
  farm: 'Farm',
  scout: 'Scout',
  queue: 'Queue',
  video: 'Video',
  reports: 'Reports',
  ws: 'WS',
}

const ALL_SOURCES = Object.keys(SOURCE_LABELS)

function formatTime(ts) {
  const d = new Date(ts)
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
    + '.' + String(d.getMilliseconds()).padStart(3, '0')
}

export default function Logs() {
  const entries = useLogStore((s) => s.entries)
  const clear = useLogStore((s) => s.clear)

  const [autoScroll, setAutoScroll] = useState(true)
  const [filterSource, setFilterSource] = useState('all')
  const [filterLevel, setFilterLevel] = useState('all')
  const [search, setSearch] = useState('')
  const [expandedId, setExpandedId] = useState(null)
  const [showDetail, setShowDetail] = useState(true)
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [showAllLogs, setShowAllLogs] = useState(false)
  const scrollRef = useRef(null)

  // Debounce search input — 300ms delay prevents filtering 2000 entries on every keystroke
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300)
    return () => clearTimeout(timer)
  }, [search])

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [entries, autoScroll])

  const filtered = useMemo(() => entries.filter((e) => {
    if (filterSource !== 'all' && e.source !== filterSource) return false
    if (filterLevel !== 'all' && e.level !== filterLevel) return false
    if (debouncedSearch) {
      const q = debouncedSearch.toLowerCase()
      return (
        e.message.toLowerCase().includes(q) ||
        (e.detail && String(e.detail).toLowerCase().includes(q)) ||
        e.source.toLowerCase().includes(q)
      )
    }
    return true
  }), [entries, filterSource, filterLevel, debouncedSearch])

  const RENDER_CAP = 200
  const capped = (!showAllLogs && filtered.length > RENDER_CAP)
    ? filtered.slice(-RENDER_CAP)
    : filtered

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex justify-between items-center mb-4 flex-wrap gap-3">
        <h2 className="heading-gold text-2xl">Activity Log</h2>
        <div className="flex items-center gap-2">
          <span className="text-xs text-secondary">{filtered.length} / {entries.length} entries</span>
          <button onClick={clear} className="btn-danger btn-xs">Clear All</button>
        </div>
      </div>

      {/* Filters */}
      <div className="card p-3 mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <label className="text-xs text-secondary">Source:</label>
          <select
            value={filterSource}
            onChange={(e) => setFilterSource(e.target.value)}
            className="input-field text-xs py-1 px-2 w-auto"
          >
            <option value="all">All</option>
            {ALL_SOURCES.map((s) => (
              <option key={s} value={s}>{SOURCE_LABELS[s]}</option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label className="text-xs text-secondary">Level:</label>
          <select
            value={filterLevel}
            onChange={(e) => setFilterLevel(e.target.value)}
            className="input-field text-xs py-1 px-2 w-auto"
          >
            <option value="all">All</option>
            <option value="info">Info</option>
            <option value="success">Success</option>
            <option value="warning">Warning</option>
            <option value="error">Error</option>
          </select>
        </div>

        <div className="flex items-center gap-2 flex-1 min-w-[150px]">
          <label className="text-xs text-secondary">Search:</label>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input-field text-xs py-1 px-2"
            placeholder="Filter messages..."
          />
        </div>

        <label className="flex items-center gap-1 text-xs text-secondary cursor-pointer select-none">
          <input type="checkbox" checked={showDetail} onChange={(e) => setShowDetail(e.target.checked)} className="checkbox-gold" />
          Show details
        </label>
        <label className="flex items-center gap-1 text-xs text-secondary cursor-pointer select-none">
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} className="checkbox-gold" />
          Auto-scroll
        </label>
      </div>

      {/* Render cap notice */}
      {!showAllLogs && filtered.length > RENDER_CAP && (
        <div className="mb-2 text-center">
          <button
            onClick={() => setShowAllLogs(true)}
            className="btn-secondary btn-xs"
          >
            Showing last {RENDER_CAP} of {filtered.length} — Show all
          </button>
        </div>
      )}
      {showAllLogs && filtered.length > RENDER_CAP && (
        <div className="mb-2 text-center">
          <button
            onClick={() => setShowAllLogs(false)}
            className="btn-secondary btn-xs"
          >
            Showing all {filtered.length} — Cap to {RENDER_CAP}
          </button>
        </div>
      )}

      {/* Log entries */}
      <div
        ref={scrollRef}
        className="ws-panel"
        style={{ maxHeight: 'calc(100vh - 280px)', minHeight: '300px' }}
      >
        {capped.length === 0 ? (
          <div className="text-secondary italic py-4 text-center">
            {entries.length === 0 ? 'No activity yet. Navigate the app to see logs.' : 'No entries match your filters.'}
          </div>
        ) : (
          capped.map((entry) => {
            const expanded = showDetail || expandedId === entry.id
            const detailStr = entry.detail
              ? (typeof entry.detail === 'object' ? JSON.stringify(entry.detail, null, 2) : String(entry.detail))
              : null
            return (
            <div
              key={entry.id}
              className={`cursor-pointer ${LEVEL_CLASS[entry.level] || 'text-primary'}`}
              onClick={() => setExpandedId(expandedId === entry.id ? null : entry.id)}
              style={{ padding: '3px 0', borderBottom: '1px solid rgba(58,48,40,0.3)' }}
            >
              <div className="flex gap-2 items-start text-xs">
                <span className="ws-panel-time shrink-0">[{formatTime(entry.timestamp)}]</span>
                <span className="text-gold shrink-0 w-[55px] text-right">{SOURCE_LABELS[entry.source] || entry.source}</span>
                <span className="flex-1 break-all">{entry.message}</span>
              </div>
              {detailStr && expanded && (
                <pre className="mt-1 ml-[120px] text-xs text-secondary break-all whitespace-pre-wrap" style={{ maxHeight: '200px', overflowY: 'auto' }}>
                  {detailStr}
                </pre>
              )}
            </div>
            )
          })
        )}
      </div>
    </div>
  )
}
