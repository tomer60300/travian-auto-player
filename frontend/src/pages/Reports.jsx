import { useState } from 'react'
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
  return type
}

function getReportTypeIcon(type) {
  if (!type) return '[?]'
  const lower = type.toLowerCase()
  if (lower.includes('scout')) return '[S]'
  if (lower.includes('battle') || lower.includes('attack') || lower.includes('raid')) return '[B]'
  if (lower.includes('trade') || lower.includes('merchant')) return '[T]'
  if (lower.includes('reinforce')) return '[R]'
  return '[?]'
}

function formatDate(dateStr) {
  if (!dateStr) return '-'
  const d = new Date(dateStr)
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

  // Expanded report
  const [expandedId, setExpandedId] = useState(null)
  const [reportDetails, setReportDetails] = useState({})
  const [loadingDetail, setLoadingDetail] = useState(null)

  async function fetchReports() {
    setLoading(true)
    setFetched(false)
    setExpandedId(null)
    setReportDetails({})
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

      {reports.length > 0 && (
        <div className="card p-0 overflow-hidden">
          {/* Header */}
          <div className="report-header text-xs font-semibold text-secondary">
            <span>Date</span>
            <span>Type</span>
            <span>Subject</span>
            <span className="text-right">Status</span>
          </div>

          {/* Rows */}
          {reports.map((report) => {
            const id = report.id || report.report_id
            const isExpanded = expandedId === id

            return (
              <div key={id}>
                <div
                  className={`report-row text-sm${isExpanded ? ' row-selected' : ''}`}
                  onClick={() => toggleReport(id)}
                >
                  <span className="text-xs text-secondary">
                    {formatDate(report.date || report.time || report.timestamp)}
                  </span>
                  <span
                    className="text-xs font-semibold text-gold"
                    title={report.type}
                  >
                    {getReportTypeIcon(report.type)}{' '}
                    {getReportTypeLabel(report.type)}
                  </span>
                  <span
                    className="text-sm text-primary truncate"
                    title={report.subject || report.title || '-'}
                  >
                    {report.subject || report.title || '-'}
                  </span>
                  <span
                    className={`text-xs text-right ${
                      report.read === false || report.unread
                        ? 'text-gold font-semibold'
                        : 'text-secondary'
                    }`}
                  >
                    {report.read === false || report.unread ? 'Unread' : 'Read'}
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
        </div>
      )}
    </div>
  )
}
