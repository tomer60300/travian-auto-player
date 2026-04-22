import { useState, useRef, useEffect } from 'react'
import api from '../api'
import useGameStore from '../stores/gameStore'
import { useToast } from '../components/Toast'

function getReportTypeLabel(type) {
  if (!type) return 'Other'
  const lower = type.toLowerCase()
  if (lower.includes('scout')) return 'Scout'
  if (lower.includes('battle') || lower.includes('attack') || lower.includes('raid')) return 'Battle'
  if (lower.includes('trade') || lower.includes('merchant')) return 'Trade'
  if (lower.includes('reinforce')) return 'Reinf.'
  if (lower.includes('adventure')) return 'Adventure'
  return type
}

function getReportTypeIcon(type) {
  if (!type) return '[?]'
  const lower = type.toLowerCase()
  if (lower.includes('scout')) return '[S]'
  if (lower.includes('battle') || lower.includes('attack') || lower.includes('raid')) return '[B]'
  if (lower.includes('trade') || lower.includes('merchant')) return '[T]'
  if (lower.includes('reinforce')) return '[R]'
  if (lower.includes('adventure')) return '[A]'
  return '[?]'
}

function formatDate(dateStr) {
  if (!dateStr) return '-'
  // Travian dates like "today, 14:57" or "05.04.26, 13:36" aren't ISO parseable
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr // return as-is if not parseable
  return d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

const RESOURCE_GRID_STYLE = {
  gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))',
}

const DETAIL_PRE_STYLE = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  maxHeight: '300px',
  overflowY: 'auto',
}

function ResourceTable({ resources }) {
  if (!resources || typeof resources !== 'object') return null
  const keys = Object.keys(resources)
  if (keys.length === 0) return null

  return (
    <div className="mt-2">
      <div className="text-xs font-semibold mb-1 text-gold">
        Resources
      </div>
      <div className="grid gap-2" style={RESOURCE_GRID_STYLE}>
        {keys.map((key) => (
          <div
            key={key}
            className="flex justify-between text-xs px-2 py-1 rounded bg-base"
          >
            <span className="text-secondary">{key}</span>
            <span className="text-primary font-semibold">
              {typeof resources[key] === 'number' ? resources[key].toLocaleString() : String(resources[key])}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function BattleInfo({ label, info }) {
  if (!info || typeof info !== 'object') return null

  return (
    <div className="mt-2">
      <div className="text-xs font-semibold mb-1 text-gold">
        {label}
      </div>
      <div className="text-xs text-secondary">
        {Object.entries(info).map(([key, value]) => (
          <div key={key} className="flex justify-between px-2 py-0.5">
            <span>{key}</span>
            <span className="text-primary">
              {typeof value === 'object' ? JSON.stringify(value) : String(value)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ReportDetail({ detail, loading }) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 py-3 px-4 text-secondary">
        <span className="spinner spinner-sm" /> Loading report details...
      </div>
    )
  }

  if (!detail) return null

  // Check for structured scout/battle data
  const hasResources = detail.resources || detail.scoutedResources || detail.scouted_resources
  const hasAttacker = detail.attacker || detail.attackerInfo || detail.attacker_info
  const hasDefender = detail.defender || detail.defenderInfo || detail.defender_info
  const hasStructured = hasResources || hasAttacker || hasDefender

  return (
    <div className="px-4 py-3 bg-base border-t-default">
      {hasStructured ? (
        <div className="flex flex-col gap-2">
          {hasResources && (
            <ResourceTable resources={hasResources} />
          )}
          {hasAttacker && (
            <BattleInfo label="Attacker" info={hasAttacker} />
          )}
          {hasDefender && (
            <BattleInfo label="Defender" info={hasDefender} />
          )}
        </div>
      ) : (
        <pre
          className="text-xs text-secondary overflow-x-auto"
          style={DETAIL_PRE_STYLE}
        >
          {JSON.stringify(detail, null, 2)}
        </pre>
      )}
    </div>
  )
}

function RaidTargetAnalyzer() {
  const toast = useToast()
  const [collapsed, setCollapsed] = useState(true)
  const [minResources, setMinResources] = useState(200)
  const [analyzerMaxAge, setAnalyzerMaxAge] = useState(24)
  const [analyzerMaxPages, setAnalyzerMaxPages] = useState(3)
  const [radius, setRadius] = useState('')
  const [excludeAlliances, setExcludeAlliances] = useState('')
  const [excludePlayers, setExcludePlayers] = useState('')
  const [analyzing, setAnalyzing] = useState(false)
  const [results, setResults] = useState(null)
  const [analyzeError, setAnalyzeError] = useState(null)
  const [progress, setProgress] = useState(null)
  const [liveTargets, setLiveTargets] = useState([])
  const wsRef = useRef(null)

  // Cleanup WS on unmount
  useEffect(() => {
    return () => { if (wsRef.current) { try { wsRef.current.close() } catch {} wsRef.current = null } }
  }, [])

  function handleAnalyze() {
    // Close any previous WS before starting a new analysis
    if (wsRef.current) { try { wsRef.current.close() } catch {} wsRef.current = null }
    setAnalyzing(true)
    setResults(null)
    setAnalyzeError(null)
    setProgress({ phase: 'start', message: 'Connecting...' })
    setLiveTargets([])

    const token = localStorage.getItem('token')
    if (!token) {
      setAnalyzeError('No auth token')
      setAnalyzing(false)
      return
    }

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/reports/analyze?token=${token}`)
    wsRef.current = ws

    ws.onopen = () => {
      ws.send(JSON.stringify({
        min_resources: minResources,
        max_report_age_hours: analyzerMaxAge,
        max_pages: analyzerMaxPages,
        radius: radius ? Number(radius) : null,
        exclude_alliances: excludeAlliances.split(',').map(s => s.trim()).filter(Boolean),
        exclude_players: excludePlayers.split(',').map(s => s.trim()).filter(Boolean),
      }))
    }

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'progress') {
        setProgress(data)
        // If a target was found, add it to the live table
        if (data.phase === 'target_found' && data.target) {
          setLiveTargets((prev) => [...prev, data.target])
        }
      } else if (data.type === 'complete') {
        setResults(data)
        setLiveTargets([])
        setProgress(null)
        setAnalyzing(false)
        toast.success(`Found ${data.total_targets ?? 0} raid target(s) in ${data.diagnostics?.analysis_duration_seconds ?? '?'}s`)
      } else if (data.type === 'error') {
        setAnalyzeError(data.message)
        setAnalyzing(false)
        setProgress(null)
        toast.error(data.message)
      }
    }

    ws.onerror = () => {
      setAnalyzeError('WebSocket connection error')
      setAnalyzing(false)
      setProgress(null)
    }

    ws.onclose = (event) => {
      setAnalyzing(false)
      if (event.code === 4009) {
        setAnalyzeError(event.reason || 'An analysis is already running')
        toast.error(event.reason || 'Analysis already running')
      }
    }
  }

  const confidenceClass = (c) => {
    if (!c) return 'text-secondary'
    if (c === 'high') return 'text-green'
    if (c === 'medium') return 'text-gold'
    return 'text-secondary'
  }

  const displayTargets = results?.targets ?? liveTargets

  return (
    <div className="card mb-6">
      <h3
        className="heading-gold text-base mb-0 flex items-center justify-between cursor-pointer select-none"
        onClick={() => setCollapsed((c) => !c)}
      >
        <span>Raid Target Analyzer</span>
        <span className="text-secondary text-sm font-normal">{collapsed ? '[ + ]' : '[ - ]'}</span>
      </h3>

      {collapsed ? null : <>
      <div className="mt-4" />
      <div className="flex items-end gap-4 flex-wrap mb-4">
        <div className="flex flex-col gap-1.5">
          <label className="text-sm font-semibold text-secondary">Min Resources</label>
          <input type="number" className="input-field w-30" min="0" value={minResources}
            onChange={(e) => setMinResources(Number(e.target.value))} disabled={analyzing} />
        </div>
        <div className="flex flex-col gap-1.5">
          <label className="text-sm font-semibold text-secondary">Max Report Age (hrs)</label>
          <input type="number" className="input-field w-30" min="1" value={analyzerMaxAge}
            onChange={(e) => setAnalyzerMaxAge(Number(e.target.value))} disabled={analyzing} />
        </div>
        <div className="flex flex-col gap-1.5">
          <label className="text-sm font-semibold text-secondary">Max Pages</label>
          <input type="number" className="input-field w-30" min="1" value={analyzerMaxPages}
            onChange={(e) => setAnalyzerMaxPages(Number(e.target.value))} disabled={analyzing} />
        </div>
        <div className="flex flex-col gap-1.5">
          <label className="text-sm font-semibold text-secondary">Radius (optional)</label>
          <input type="number" className="input-field w-30" min="1" value={radius}
            onChange={(e) => setRadius(e.target.value)} disabled={analyzing} />
        </div>
      </div>

      <div className="flex items-end gap-4 flex-wrap mb-4">
        <div className="flex flex-col gap-1.5 flex-1" style={{ minWidth: '200px' }}>
          <label className="text-sm font-semibold text-secondary">Exclude Alliances (comma-separated)</label>
          <input type="text" className="input-field" value={excludeAlliances}
            onChange={(e) => setExcludeAlliances(e.target.value)} disabled={analyzing}
            placeholder="e.g. NATO, ALLY2" />
        </div>
        <div className="flex flex-col gap-1.5 flex-1" style={{ minWidth: '200px' }}>
          <label className="text-sm font-semibold text-secondary">Exclude Players (comma-separated)</label>
          <input type="text" className="input-field" value={excludePlayers}
            onChange={(e) => setExcludePlayers(e.target.value)} disabled={analyzing}
            placeholder="e.g. player1, player2" />
        </div>
      </div>

      <div className="flex items-center gap-4">
        <button className="btn-primary flex items-center gap-2" onClick={handleAnalyze} disabled={analyzing}>
          {analyzing && <span className="spinner spinner-sm" />}
          {analyzing ? 'Analyzing...' : 'Analyze'}
        </button>
      </div>

      {/* Live progress bar */}
      {progress && (
        <div className="mt-3 p-3 bg-surface rounded-md border-default">
          <div className="flex items-center gap-3 mb-1.5">
            <span className="spinner spinner-sm" />
            <span className="text-sm text-primary">{progress.message}</span>
            {progress.targets_found > 0 && (
              <span className="text-xs text-gold ml-auto">{progress.targets_found} targets found</span>
            )}
          </div>
          {progress.total > 0 && progress.done != null && (
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${Math.min(100, (progress.done / progress.total) * 100)}%` }} />
            </div>
          )}
          {progress.phase_num && progress.total_phases && (
            <div className="text-xs text-secondary mt-1">Phase {progress.phase_num}/{progress.total_phases}</div>
          )}
        </div>
      )}

      {/* Error display */}
      {analyzeError && (
        <div className="error-box mt-4">
          {analyzeError}
        </div>
      )}

      {/* Live targets table (during analysis) or final results */}
      {displayTargets.length > 0 && (
        <div className="mt-4">
          {results && (
            <div className="text-sm text-secondary mb-2">
              Source: <span className="text-gold">{results.source_village}</span>{' '}
              {results.source_coords} — {results.total_targets} target(s)
              {results.diagnostics?.analysis_duration_seconds && (
                <span className="ml-2 text-xs">({results.diagnostics.analysis_duration_seconds}s)</span>
              )}
            </div>
          )}
          {!results && analyzing && (
            <div className="text-sm text-secondary mb-2">
              Targets found so far: <span className="text-gold">{liveTargets.length}</span>
              <span className="text-xs ml-2">(table updates live as targets are scored)</span>
            </div>
          )}

          <div className="overflow-x-auto">
            <table className="data-table w-full text-sm">
              <thead>
                <tr>
                  <th className="text-left">Village</th>
                  <th className="text-left">Player</th>
                  <th className="text-right">Pop</th>
                  <th className="text-right">Distance</th>
                  <th className="text-right">Est. Loot</th>
                  <th className="text-left">Troops</th>
                  <th className="text-right">Score</th>
                  <th className="text-left">Confidence</th>
                </tr>
              </thead>
              <tbody>
                {displayTargets.map((t, idx) => {
                  const st = t.state || {}
                  const rec = t.recommendation || {}
                  return (
                    <tr key={`${st.x}-${st.y}-${idx}`}>
                      <td className="text-primary">
                        {st.village_name || '-'}{' '}
                        <span className="text-secondary text-xs">({st.x}, {st.y})</span>
                      </td>
                      <td className="text-secondary">{st.player_name || '-'}</td>
                      <td className="text-right text-secondary">{st.population ?? '-'}</td>
                      <td className="text-right text-secondary">{st.distance != null ? Number(st.distance).toFixed(1) : '-'}</td>
                      <td className="text-right text-gold">{st.estimated_raidable ?? rec.est_loot ?? '-'}</td>
                      <td className="text-primary">
                        {rec.n_send ?? '-'} {rec.unit_type || ''}
                      </td>
                      <td className="text-right text-gold font-semibold">{rec.score != null ? Number(rec.score).toFixed(1) : '-'}</td>
                      <td className={confidenceClass(st.raidable_confidence)}>
                        {st.raidable_confidence || '-'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {!results && !analyzing && displayTargets.length === 0 && (
            <div className="text-secondary text-sm mt-2">
              <p className="mb-2">No raidable targets found.</p>
              <p className="text-xs">The analyzer needs <span className="text-gold">outgoing</span> scout and raid reports.</p>
            </div>
          )}
        </div>
      )}

      {results && displayTargets.length === 0 && (
        <div className="text-secondary text-sm mt-4">
          <p className="mb-2">No raidable targets found.</p>
          <p className="text-xs">The analyzer needs <span className="text-gold">outgoing</span> scout and raid reports — where YOUR troops attacked or scouted another village.</p>
        </div>
      )}

      {/* Diagnostics */}
      {results?.diagnostics && (
        <div className="mt-3 p-3 bg-surface rounded text-xs text-secondary">
          <p className="font-semibold text-primary mb-1">Diagnostics</p>
          <p>Reports scanned: {results.diagnostics.total_reports_listed ?? '-'} | Skipped (wrong type): {results.diagnostics.reports_skipped_type ?? '-'} | Fetched OK: {results.diagnostics.reports_fetched_ok ?? '-'} | Failed: {results.diagnostics.reports_fetched_fail ?? '-'}</p>
          <p>Pages: {results.diagnostics.pages_fetched ?? '-'} fetched, {results.diagnostics.pages_failed ?? '-'} failed | Duration: {results.diagnostics.analysis_duration_seconds ?? '-'}s</p>
          {results.diagnostics.warnings && results.diagnostics.warnings.length > 0 && (
            <div className="mt-1 text-warning">
              {results.diagnostics.warnings.map((w, i) => <p key={i}>{w}</p>)}
            </div>
          )}
        </div>
      )}
      </>}
    </div>
  )
}

export default function Reports() {
  const toast = useToast()
  const connected = useGameStore((s) => s.connected)

  // Filter state
  const [maxAgeHours, setMaxAgeHours] = useState('24')
  const [maxPages, setMaxPages] = useState('5')

  // Reports data
  const [reports, setReports] = useState([])
  const [loading, setLoading] = useState(false)
  const [fetched, setFetched] = useState(false)

  // Pagination
  const [showAllReports, setShowAllReports] = useState(false)

  // Expanded report
  const [expandedId, setExpandedId] = useState(null)
  const [reportDetails, setReportDetails] = useState({})
  const [loadingDetail, setLoadingDetail] = useState(null)

  async function fetchReports() {
    setLoading(true)
    setFetched(false)
    setExpandedId(null)
    setReportDetails({})
    setShowAllReports(false)
    try {
      const res = await api.get('/reports', {
        params: {
          max_age_hours: parseInt(maxAgeHours, 10) || 24,
          max_pages: parseInt(maxPages, 10) || 5,
        },
      })
      const data = Array.isArray(res.data) ? res.data : (res.data?.reports || [])
      setReports(data)
      setFetched(true)
      toast.success(`Fetched ${data.length} report(s)`)
    } catch (err) {
      const message = err.response?.data?.detail || err.response?.data?.message || 'Failed to fetch reports'
      toast.error(message)
    } finally {
      setLoading(false)
    }
  }

  async function toggleReport(reportId) {
    if (expandedId === reportId) {
      setExpandedId(null)
      return
    }

    setExpandedId(reportId)

    // Fetch detail if not cached
    if (!reportDetails[reportId]) {
      setLoadingDetail(reportId)
      try {
        const res = await api.get(`/reports/${reportId}`)
        setReportDetails((prev) => ({ ...prev, [reportId]: res.data }))
      } catch (err) {
        const message = err.response?.data?.detail || 'Failed to load report detail'
        toast.error(message)
        setReportDetails((prev) => ({ ...prev, [reportId]: { error: message } }))
      } finally {
        setLoadingDetail(null)
      }
    }
  }

  if (!connected) {
    return (
      <div className="p-6">
        <h2 className="heading-gold text-2xl mb-4">Reports</h2>
        <div className="card text-center p-8">
          <p className="text-secondary">Connect to a Travian server to view reports.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6">
      <h2 className="heading-gold text-2xl mb-6">Reports</h2>

      {/* Raid Target Analyzer */}
      <RaidTargetAnalyzer />

      {/* Filters */}
      <div className="card mb-6">
        <div className="flex items-end gap-4 flex-wrap">
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-semibold text-secondary">
              Max Age (hours)
            </label>
            <input
              type="number"
              className="input-field w-30"
              min="1"
              value={maxAgeHours}
              onChange={(e) => setMaxAgeHours(e.target.value)}
              disabled={loading}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-semibold text-secondary">
              Max Pages
            </label>
            <input
              type="number"
              className="input-field w-30"
              min="1"
              value={maxPages}
              onChange={(e) => setMaxPages(e.target.value)}
              disabled={loading}
            />
          </div>
          <button
            className="btn-primary flex items-center gap-2"
            onClick={fetchReports}
            disabled={loading}
          >
            {loading && <span className="spinner spinner-sm" />}
            {loading ? 'Fetching...' : 'Fetch Reports'}
          </button>
        </div>
      </div>

      {/* Report List */}
      {fetched && reports.length === 0 && (
        <div className="card text-center p-8">
          <p className="text-secondary">No reports found for the given filters.</p>
        </div>
      )}

      {reports.length > 0 && (() => {
        const REPORT_PAGE_SIZE = 30
        const visibleReports = showAllReports ? reports : reports.slice(0, REPORT_PAGE_SIZE)
        return (
        <div className="card p-0 overflow-hidden">
          {/* Header */}
          <div className="report-header text-xs font-semibold text-secondary">
            <span>Date</span>
            <span>Type</span>
            <span>Subject</span>
            <span className="text-right">Status</span>
          </div>

          {/* Rows */}
          {visibleReports.map((report) => {
            const id = report.report_id || report.id
            const rtype = report.report_type || report.type
            const isExpanded = expandedId === id

            return (
              <div key={id}>
                <div
                  className={`report-row text-sm${isExpanded ? ' row-selected' : ''}`}
                  onClick={() => toggleReport(id)}
                >
                  <span className="text-xs text-secondary">
                    {report.date_str || formatDate(report.date || report.time || report.timestamp)}
                  </span>
                  <span
                    className="text-xs font-semibold text-gold"
                    title={rtype}
                  >
                    {getReportTypeIcon(rtype)}{' '}
                    {getReportTypeLabel(rtype)}
                  </span>
                  <span
                    className="text-sm text-primary truncate"
                    title={report.subject || report.title || '-'}
                  >
                    {report.subject || report.title || '-'}
                  </span>
                  <span
                    className={`text-xs text-right ${
                      report.is_read === false
                        ? 'text-gold font-semibold'
                        : 'text-secondary'
                    }`}
                  >
                    {report.is_read === false ? 'Unread' : 'Read'}
                  </span>
                </div>

                {/* Expanded detail */}
                {isExpanded && (
                  <ReportDetail
                    detail={reportDetails[id]}
                    loading={loadingDetail === id}
                  />
                )}
              </div>
            )
          })}

          {!showAllReports && reports.length > REPORT_PAGE_SIZE && (
            <div className="py-2 text-center">
              <button
                className="btn-secondary btn-xs"
                onClick={() => setShowAllReports(true)}
              >
                Show all {reports.length} reports
              </button>
            </div>
          )}
        </div>
        )
      })()}
    </div>
  )
}
