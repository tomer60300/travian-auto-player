/**
 * LogDrawer — Floating, resizable log drawer with virtual-scrolled entries.
 *
 * Collapses to a single-line ticker bar; expands to a filterable log panel.
 * Currently not mounted in the app layout.
 *
 * Props: none (reads all state from logStore).
 */
import React, { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import useLogStore from '../stores/logStore'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ITEM_HEIGHT = 28
const EXPANDED_ITEM_HEIGHT = 120
const BUFFER = 20
const MIN_DRAWER_HEIGHT = 150
const MAX_DRAWER_VH = 0.7
const DEBOUNCE_MS = 300

const LEVELS = ['info', 'success', 'warning', 'error']

const SOURCE_LABELS = {
  api: 'API',
  ws: 'WS',
  auth: 'Auth',
  game: 'Game',
  scout: 'Scout',
  farm: 'Farm',
  queue: 'Queue',
  military: 'Military',
  video: 'Video',
  reports: 'Reports',
  server: 'Server',
}

const SOURCE_COLORS = {
  api: '#6b8aad',
  auth: '#8b7cc8',
  game: '#7cab7c',
  military: '#c87c7c',
  farm: '#5ba65b',
  scout: '#5a9aad',
  queue: '#d4a03c',
  video: '#c88a5a',
  reports: '#8a8a5a',
  ws: '#7a7068',
  server: '#4a9eda',
}

const LEVEL_STYLES = {
  info: 'text-primary',
  success: 'text-success',
  warning: 'text-warning',
  error: '',
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(ts) {
  const diff = Math.floor((Date.now() - ts) / 1000)
  if (diff < 5) return 'now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return new Date(ts).toLocaleDateString()
}

function formatISO(ts) {
  return new Date(ts).toISOString()
}


// ---------------------------------------------------------------------------
// Debounce hook
// ---------------------------------------------------------------------------

function useDebouncedValue(value, delay) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return debounced
}

// ---------------------------------------------------------------------------
// LogEntryRow
// ---------------------------------------------------------------------------

const LogEntryRow = React.memo(function LogEntryRow({ entry, expanded, onToggle }) {
  const levelClass = LEVEL_STYLES[entry.level]
  const isError = entry.level === 'error'
  const srcColor = SOURCE_COLORS[entry.source] || '#888'

  return (
    <div
      style={{ minHeight: ITEM_HEIGHT }}
      className={`flex flex-col cursor-pointer select-none ${
        isError ? 'bg-red-900/30' : 'hover:bg-white/5'
      }`}
      onClick={() => entry.detail && onToggle(entry.id)}
    >
      {/* Main row */}
      <div
        className="flex items-center gap-2 px-3 text-xs font-mono"
        style={{ height: ITEM_HEIGHT }}
      >
        {/* Relative timestamp */}
        <span
          className="text-secondary shrink-0 w-[52px] text-right"
          title={formatISO(entry.timestamp)}
        >
          {relativeTime(entry.timestamp)}
        </span>

        {/* Level badge */}
        <span
          className={`shrink-0 w-[52px] text-center font-semibold uppercase text-[10px] ${levelClass}`}
          style={isError ? { color: '#f87171' } : undefined}
        >
          [{entry.level}]
        </span>

        {/* Source badge */}
        <span
          className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full font-medium"
          style={{ backgroundColor: srcColor + '30', color: srcColor }}
        >
          {SOURCE_LABELS[entry.source] || entry.source}
        </span>

        {/* Message */}
        <span className="truncate text-gray-200">{entry.message}</span>

        {/* Detail indicator */}
        {entry.detail && (
          <span className="shrink-0 text-secondary text-[10px] ml-auto">
            {expanded ? '\u25BC' : '\u25B6'}
          </span>
        )}
      </div>

      {/* Expanded detail */}
      {expanded && entry.detail && (
        <div className="px-3 pb-2 pl-[130px] text-[11px] text-secondary font-mono whitespace-pre-wrap break-all">
          {typeof entry.detail === 'string'
            ? entry.detail
            : JSON.stringify(entry.detail, null, 2)}
        </div>
      )}
    </div>
  )
})

// ---------------------------------------------------------------------------
// VirtualLogList
// ---------------------------------------------------------------------------

function VirtualLogList({ entries, expandedIds, onToggleExpand }) {
  const containerRef = useRef(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [containerHeight, setContainerHeight] = useState(0)
  const autoScrollRef = useRef(true)
  const prevLengthRef = useRef(entries.length)

  // Observe container size
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => {
      setContainerHeight(e.contentRect.height)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Determine if user scrolled away from bottom
  const handleScroll = useCallback((e) => {
    const el = e.target
    setScrollTop(el.scrollTop)
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < ITEM_HEIGHT * 2
    autoScrollRef.current = atBottom
  }, [])

  // Auto-scroll to bottom on new entries
  useEffect(() => {
    if (entries.length > prevLengthRef.current && autoScrollRef.current) {
      const el = containerRef.current
      if (el) {
        el.scrollTop = el.scrollHeight
      }
    }
    prevLengthRef.current = entries.length
  }, [entries.length])

  const totalHeight = entries.length * ITEM_HEIGHT
  const startIdx = Math.max(0, Math.floor(scrollTop / ITEM_HEIGHT) - BUFFER)
  const endIdx = Math.min(
    entries.length,
    Math.ceil((scrollTop + containerHeight) / ITEM_HEIGHT) + BUFFER
  )
  const visibleEntries = entries.slice(startIdx, endIdx)

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      style={{ height: '100%', overflow: 'auto' }}
      className="scrollbar-thin"
    >
      <div style={{ height: totalHeight, position: 'relative' }}>
        <div
          style={{
            position: 'absolute',
            top: startIdx * ITEM_HEIGHT,
            width: '100%',
          }}
        >
          {visibleEntries.map((entry) => (
            <LogEntryRow
              key={entry.id}
              entry={entry}
              expanded={expandedIds.has(entry.id)}
              onToggle={onToggleExpand}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Collapsed Ticker Bar
// ---------------------------------------------------------------------------

function CollapsedBar() {
  const toggleDrawer = useLogStore((s) => s.toggleDrawer)
  const entryCount = useLogStore((s) => s.entries.length)
  const serverLogCount = useLogStore((s) => s.serverLogCount)
  const lastEntry = useLogStore((s) => (s.entries.length > 0 ? s.entries[s.entries.length - 1] : null))
  const [, setTick] = useState(0)

  // Tick every second for relative time updates
  useEffect(() => {
    const iv = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(iv)
  }, [])

  return (
    <div
      className="flex items-center gap-3 px-4 cursor-pointer select-none"
      style={{ height: 36 }}
      onClick={toggleDrawer}
    >
      {/* Title and count */}
      <span className="text-xs font-semibold text-gray-300 flex items-center gap-1.5">
        <span style={{ fontSize: 14 }}>&#128202;</span>
        Logs
        <span className="text-secondary">({entryCount})</span>
        {serverLogCount > 0 && (
          <span className="status-pulse bg-blue-500 text-white text-[10px] px-1.5 py-0.5 rounded-full font-bold ml-1">
            {serverLogCount}
          </span>
        )}
      </span>

      {/* Last log preview */}
      {lastEntry && (
        <span className="text-[11px] text-secondary truncate flex-1 font-mono">
          last: [{lastEntry.level}] {SOURCE_LABELS[lastEntry.source] || lastEntry.source}: {lastEntry.message}
        </span>
      )}

      {/* Toggle button */}
      <button
        className="text-secondary hover:text-gray-200 text-sm shrink-0"
        onClick={(e) => {
          e.stopPropagation()
          toggleDrawer()
        }}
      >
        &#9650;
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Expanded Drawer Content
// ---------------------------------------------------------------------------

function ExpandedDrawerContent() {
  const entries = useLogStore((s) => s.entries)
  const clearLogs = useLogStore((s) => s.clear)
  const toggleDrawer = useLogStore((s) => s.toggleDrawer)

  // Local filter state
  const [activeLevels, setActiveLevels] = useState(new Set(LEVELS))
  const [sourceFilter, setSourceFilter] = useState('all')
  const [searchRaw, setSearchRaw] = useState('')
  const searchTerm = useDebouncedValue(searchRaw, DEBOUNCE_MS)
  const [expandedIds, setExpandedIds] = useState(new Set())
  const [, setTick] = useState(0)

  // Tick every second for relative timestamps
  useEffect(() => {
    const iv = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(iv)
  }, [])

  const toggleLevel = useCallback((level) => {
    setActiveLevels((prev) => {
      const next = new Set(prev)
      if (next.has(level)) {
        // Don't allow deselecting all
        if (next.size > 1) next.delete(level)
      } else {
        next.add(level)
      }
      return next
    })
  }, [])

  const onToggleExpand = useCallback((id) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const filtered = useMemo(() => {
    const lowerSearch = searchTerm.toLowerCase()
    return entries.filter((e) => {
      if (!activeLevels.has(e.level)) return false
      if (sourceFilter !== 'all' && e.source !== sourceFilter) return false
      if (lowerSearch && !e.message.toLowerCase().includes(lowerSearch) && !(e.source || '').toLowerCase().includes(lowerSearch)) return false
      return true
    })
  }, [entries, activeLevels, sourceFilter, searchTerm])

  return (
    <div className="flex flex-col h-full">
      {/* Filter bar */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-default flex-wrap shrink-0">
        {/* Level toggles */}
        {LEVELS.map((level) => {
          const active = activeLevels.has(level)
          return (
            <button
              key={level}
              className={`btn-xs text-[10px] px-2 py-0.5 rounded font-semibold capitalize transition-colors ${
                active
                  ? level === 'info'
                    ? 'bg-blue-600/30 text-blue-300 border border-blue-500/40'
                    : level === 'success'
                    ? 'bg-green-600/30 text-green-300 border border-green-500/40'
                    : level === 'warning'
                    ? 'bg-yellow-600/30 text-yellow-300 border border-yellow-500/40'
                    : 'bg-red-600/30 text-red-300 border border-red-500/40'
                  : 'bg-white/5 text-secondary border border-transparent'
              }`}
              onClick={() => toggleLevel(level)}
            >
              {level}
            </button>
          )
        })}

        {/* Separator */}
        <span className="text-secondary text-xs mx-1">|</span>

        {/* Source dropdown */}
        <select
          className="bg-surface text-xs text-gray-300 border border-default rounded px-1.5 py-0.5 outline-none"
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
        >
          <option value="all">All Sources</option>
          {Object.entries(SOURCE_LABELS).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>

        {/* Search */}
        <div className="flex items-center gap-1 ml-auto">
          <span className="text-secondary text-xs">&#128269;</span>
          <input
            type="text"
            placeholder="Search logs..."
            className="bg-surface text-xs text-gray-300 border border-default rounded px-2 py-0.5 outline-none w-36 focus:border-blue-500/50"
            value={searchRaw}
            onChange={(e) => setSearchRaw(e.target.value)}
          />
        </div>

        {/* Count */}
        <span className="text-[10px] text-secondary shrink-0">
          {filtered.length}/{entries.length}
        </span>

        {/* Clear */}
        <button
          className="btn-xs text-[10px] px-2 py-0.5 rounded text-red-400 hover:bg-red-900/30 border border-transparent hover:border-red-500/30"
          onClick={clearLogs}
          title="Clear all logs"
        >
          Clear
        </button>

        {/* Collapse */}
        <button
          className="text-secondary hover:text-gray-200 text-sm shrink-0"
          onClick={toggleDrawer}
          title="Collapse drawer"
        >
          &#9660;
        </button>
      </div>

      {/* Log entries */}
      <div className="flex-1 min-h-0">
        {filtered.length === 0 ? (
          <div className="flex items-center justify-center h-full text-secondary text-xs">
            No log entries match the current filters
          </div>
        ) : (
          <VirtualLogList
            entries={filtered}
            expandedIds={expandedIds}
            onToggleExpand={onToggleExpand}
          />
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// LogDrawer (main export)
// ---------------------------------------------------------------------------

export default function LogDrawer() {
  const drawerOpen = useLogStore((s) => s.drawerOpen)
  const drawerHeight = useLogStore((s) => s.drawerHeight)
  const setDrawerHeight = useLogStore((s) => s.setDrawerHeight)

  const draggingRef = useRef(false)
  const startYRef = useRef(0)
  const startHeightRef = useRef(0)

  // Drag-to-resize handler
  const onMouseDown = useCallback(
    (e) => {
      e.preventDefault()
      draggingRef.current = true
      startYRef.current = e.clientY
      startHeightRef.current = drawerHeight

      const onMouseMove = (ev) => {
        if (!draggingRef.current) return
        const delta = startYRef.current - ev.clientY
        const maxH = window.innerHeight * MAX_DRAWER_VH
        const newH = Math.min(maxH, Math.max(MIN_DRAWER_HEIGHT, startHeightRef.current + delta))
        setDrawerHeight(newH)
      }

      const onMouseUp = () => {
        draggingRef.current = false
        document.removeEventListener('mousemove', onMouseMove)
        document.removeEventListener('mouseup', onMouseUp)
      }

      document.addEventListener('mousemove', onMouseMove)
      document.addEventListener('mouseup', onMouseUp)
    },
    [drawerHeight, setDrawerHeight]
  )

  // Compute heights
  const collapsedHeight = 60 // handle (24) + ticker bar (36)
  const totalHeight = drawerOpen ? drawerHeight : collapsedHeight

  return (
    <div
      className="log-drawer bg-card"
      style={{
        height: totalHeight,
        // eslint-disable-next-line react-hooks/rules-of-hooks -- ref read for conditional style is intentional
        transition: draggingRef.current ? 'none' : undefined,
      }}
    >
      {/* Drag handle */}
      <div
        className="log-drawer-handle"
        onMouseDown={drawerOpen ? onMouseDown : undefined}
        style={drawerOpen ? undefined : { cursor: 'pointer' }}
        onClick={drawerOpen ? undefined : () => useLogStore.getState().toggleDrawer()}
      />

      {/* Content */}
      {drawerOpen ? <ExpandedDrawerContent /> : <CollapsedBar />}
    </div>
  )
}
