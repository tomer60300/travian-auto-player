import { useState, useRef, useCallback, useMemo, useEffect } from 'react'
import { useResumableOperation } from '../hooks/useResumableOperation'
import { useToast } from '../components/Toast'
import WebSocketPanel from '../components/WebSocketPanel'
import VillageSelector from '../components/VillageSelector'
import AddToFarmDialog from '../components/AddToFarmDialog'
import { MapCoord } from '../components/MapCoord'
import useGameStore from '../stores/gameStore'
import api from '../api'

// ── localStorage helpers ─────────────────────────────────────────────
const LS_KEY_ALLIANCES = 'autoscout_exclude_alliances'
const LS_KEY_PLAYERS = 'autoscout_exclude_players'
const LS_KEY_BONUS_MINS = 'autoscout_bonus_resource_mins'
const LS_KEY_BONUS_LEVELS = 'autoscout_bonus_total_levels'
const LS_KEY_USE_RECON = 'autoscout_use_recon'
const LS_KEY_RECON_STRICT = 'autoscout_recon_strict'
const LS_KEY_EXCLUDE_CAPITALS = 'autoscout_exclude_capitals'

function loadJson(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback } catch { return fallback }
}

// ── Background (recon) account credentials ─────────────────────────────
// Credentials used to live only in the server's .env, so "rotate the recon
// credentials, then retry" was impossible without editing a file and
// restarting. Saving here applies to the running server immediately.
function BackgroundAccountPanel({ disabled }) {
  const toast = useToast()
  const [status, setStatus] = useState(null)
  const [editing, setEditing] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const res = await api.get('/recon/status')
      setStatus(res.data)
    } catch {
      setStatus(null)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const save = async () => {
    if (!username.trim() || !password) {
      toast.error('Username and password are both required')
      return
    }
    setBusy(true)
    try {
      const res = await api.put('/recon/credentials', { username: username.trim(), password })
      setStatus(res.data)
      setPassword('')
      setEditing(false)
      toast.success('Background account saved')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Could not save credentials')
    } finally {
      setBusy(false)
    }
  }

  const test = async () => {
    setBusy(true)
    try {
      const res = await api.post('/recon/test', {}, { timeout: 0 })
      if (res.data.ok) toast.success(`Authenticated as ${res.data.username}`)
      else toast.error(res.data.detail || 'Authentication failed')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Test failed')
    } finally {
      setBusy(false)
    }
  }

  const clear = async () => {
    setBusy(true)
    try {
      const res = await api.delete('/recon/credentials')
      setStatus(res.data)
      toast.success(res.data.configured ? 'Reverted to .env credentials' : 'Credentials cleared')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Could not clear credentials')
    } finally {
      setBusy(false)
    }
  }

  const locked = disabled || busy
  // The recon account is shared, process-global state; only the instance
  // operator may see its username or manage it, so hide controls that would 403.
  const manageable = status?.manageable !== false

  return (
    <div className="ml-6 mt-2 p-3 rounded border border-gray-700 bg-black/20 max-w-xl">
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="text-secondary">Background account:</span>
        {status?.configured ? (
          <>
            <span className="font-mono">{status.username ?? 'configured'}</span>
            <span className="text-secondary">
              ({status.source === 'stored' ? 'saved here' : 'from .env'})
            </span>
          </>
        ) : (
          <span className="text-amber-400">not configured</span>
        )}
        <span className="flex-1" />
        {manageable ? (
          <>
            <button className="btn-secondary btn-sm" onClick={() => setEditing(!editing)} disabled={locked}>
              {status?.configured ? 'Change' : 'Set'}
            </button>
            <button className="btn-secondary btn-sm" onClick={test} disabled={locked || !status?.configured}>
              Test
            </button>
            {status?.source === 'stored' && (
              <button className="btn-secondary btn-sm" onClick={clear} disabled={locked}>
                Clear
              </button>
            )}
          </>
        ) : (
          <span className="text-secondary">managed by the instance operator</span>
        )}
      </div>

      {editing && manageable && (
        <div className="mt-3 flex flex-col gap-2">
          <input
            className="input-field text-xs"
            placeholder="Background account username / email"
            value={username}
            autoComplete="off"
            onChange={(e) => setUsername(e.target.value)}
            disabled={locked}
          />
          <input
            className="input-field text-xs"
            type="password"
            placeholder="Password"
            value={password}
            autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)}
            disabled={locked}
          />
          <div className="flex gap-2">
            <button className="btn-primary btn-sm" onClick={save} disabled={locked}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button
              className="btn-secondary btn-sm"
              onClick={() => { setEditing(false); setPassword('') }}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
          <span className="text-secondary text-xs">
            Stored encrypted on the server. Saving replaces the credentials the
            running server uses and drops any cached background session, so a
            rotation takes effect without a restart.
          </span>
        </div>
      )}
    </div>
  )
}

// ── Scan Progress Panel (shown during WS scan) ─────────────────────────
function ScanProgressPanel({ phase, messages, enrichProgress, stats }) {
  const scrollRef = useRef(null)
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [messages])

  const pct = enrichProgress ? Math.round((enrichProgress.index / enrichProgress.total) * 100) : 0

  return (
    <div className="mt-4">
      {phase && (
        <div className="flex items-center gap-2 mb-3">
          <div className="spinner spinner-sm" />
          <span className="text-sm text-gold font-semibold">{phase}</span>
        </div>
      )}
      {enrichProgress && (
        <div className="mb-3">
          <div className="flex justify-between text-xs text-secondary mb-1">
            <span>Enriching tile {enrichProgress.index}/{enrichProgress.total}{enrichProgress.name ? ` — ${enrichProgress.name}` : ''}</span>
            <span>{pct}%{enrichProgress.eta ? ` | ETA: ${enrichProgress.eta}` : ''}</span>
          </div>
          <div className="progress-track"><div className="progress-fill" style={{ width: `${pct}%` }} /></div>
        </div>
      )}
      <div ref={scrollRef} className="ws-panel" style={{ maxHeight: 200 }}>
        {messages.map((msg, i) => (
          <div key={i} className={`ws-panel-line ${msg.type === 'success' ? 'text-success' : msg.type === 'error' ? 'text-danger' : msg.type === 'detail' ? 'text-secondary' : 'text-primary'}`}>
            <span className="ws-panel-time">[{new Date(msg.ts).toLocaleTimeString('en-US', { hour12: false })}]</span>
            {msg.text}
          </div>
        ))}
      </div>
      {stats && (
        <div className="mt-2 flex gap-4 text-xs text-secondary flex-wrap">
          <span>Raw tiles: {stats.raw_tiles}</span>
          <span>After pre-filter: {stats.after_prefilter}</span>
          <span>Final: {stats.final}</span>
          <span>Enrich time: {stats.enrich_time_seconds}s (avg {stats.avg_enrich_time}s/tile)</span>
          <span>Total: {stats.time_seconds}s</span>
        </div>
      )}
    </div>
  )
}

// ── Scan Config Panel ─────────────────────────────────────────────────
function ScanConfigPanel({ onScanComplete, scanning, setScanning, onConfigChange, activeVillageId }) {
  const [radius, setRadius] = useState(10)
  const [minPop, setMinPop] = useState(0)
  const [maxPop, setMaxPop] = useState(100)
  const [maxPlayerPop, setMaxPlayerPop] = useState('')
  // Single-pick target type. Each mode dictates what reaches the
  // results table and (crucially) which expensive recon work the
  // server does — capital info is only fetched in "non-capitals"
  // mode because that's the only mode that needs it to filter.
  //   "villages"     — player villages only (default)
  //   "non-capitals" — player villages MINUS capitals (requires
  //                    profile-fetch per unique player)
  //   "with-oases"   — player villages + unoccupied oases
  //   "oasis-only"   — only oases (occupied + unoccupied)
  const [filterMode, setFilterMode] = useState('villages')

  // Alliance & player exclusion — persisted in localStorage
  const [excludeAlliances, setExcludeAlliances] = useState(() => loadJson(LS_KEY_ALLIANCES, []))
  const [excludePlayers, setExcludePlayers] = useState(() => loadJson(LS_KEY_PLAYERS, []))

  // Bonus filter state.
  //   bonusResourceMins: per-resource minimum % constraint. 0 = no
  //   constraint. The valid non-zero values are 25/50/75/100 (matching
  //   Travian's only emitted bonus buckets). Unknown keys/values that
  //   sneak in via localStorage are coerced back to defaults.
  //   bonusTotalLevels: array of selected bucket values (subset of
  //   [25, 50, 75, 100]). Empty = no total filter.
  const _BONUS_MIN_DEFAULT = { wood: 0, clay: 0, iron: 0, crop: 0 }
  const _BONUS_ALLOWED_VALUES = new Set([0, 25, 50, 75, 100])
  const _sanitizeMins = (raw) => {
    if (!raw || typeof raw !== 'object') return { ..._BONUS_MIN_DEFAULT }
    const out = { ..._BONUS_MIN_DEFAULT }
    for (const k of Object.keys(_BONUS_MIN_DEFAULT)) {
      const v = Number(raw[k])
      if (_BONUS_ALLOWED_VALUES.has(v)) out[k] = v
    }
    return out
  }
  const _sanitizeLevels = (raw) => {
    if (!Array.isArray(raw)) return []
    const allowed = new Set([25, 50, 75, 100])
    return Array.from(new Set(raw.map(Number).filter((v) => allowed.has(v))))
  }
  const [bonusResourceMins, setBonusResourceMins] = useState(
    () => _sanitizeMins(loadJson(LS_KEY_BONUS_MINS, _BONUS_MIN_DEFAULT)),
  )
  const [bonusTotalLevels, setBonusTotalLevels] = useState(
    () => _sanitizeLevels(loadJson(LS_KEY_BONUS_LEVELS, [])),
  )

  // Background recon account — routes the read-only sweep work
  // (map_position, tile-details, profile pages) through a disposable
  // Travian login configured server-side. Defaults to ON because the
  // whole point of having it configured is to use it; users can
  // disable per-scan for debugging / direct-account scans.
  const [useRecon, setUseRecon] = useState(() => {
    const raw = loadJson(LS_KEY_USE_RECON, true)
    return raw === false ? false : true
  })
  // Strict-recon mode — if recon is required AND can't authenticate,
  // abort the scan rather than silently fall back to the primary
  // account. Off by default: existing users keep the visible-warning
  // + fallback path. On = power-users who'd rather see no results
  // than have any scout request leak onto their main account.
  const [reconStrict, setReconStrict] = useState(() => {
    return loadJson(LS_KEY_RECON_STRICT, false) === true
  })
  // Modifier for the "villages by oasis bonus" mode: also drop each
  // player's capital village. Costs NOTHING extra — the capital id is
  // already parsed from the very same profile fetch the oasis aggregation
  // uses, so this just enables the existing non-capitals post-filter.
  const [excludeCapitals, setExcludeCapitals] = useState(() => {
    return loadJson(LS_KEY_EXCLUDE_CAPITALS, false) === true
  })
  const [newAlliance, setNewAlliance] = useState('')
  const [newPlayer, setNewPlayer] = useState('')

  // Scan progress state
  const [scanPhase, setScanPhase] = useState(null)
  const [scanMessages, setScanMessages] = useState([])
  const [enrichProgress, setEnrichProgress] = useState(null)
  const [scanStats, setScanStats] = useState(null)
  const mountedRef = useRef(true)
  // True only for scans started in THIS mount. Suppresses the "Scan
  // complete" toast and selection reset that would otherwise fire on
  // history-replay when the user navigates back to /scout after a run
  // completed in the background.
  const startedHereRef = useRef(false)
  const lastScanIdRef = useRef(null)

  useEffect(() => { return () => { mountedRef.current = false } }, [])

  // Persist to localStorage on change
  useEffect(() => { localStorage.setItem(LS_KEY_ALLIANCES, JSON.stringify(excludeAlliances)) }, [excludeAlliances])
  useEffect(() => { localStorage.setItem(LS_KEY_PLAYERS, JSON.stringify(excludePlayers)) }, [excludePlayers])
  useEffect(() => { localStorage.setItem(LS_KEY_BONUS_MINS, JSON.stringify(bonusResourceMins)) }, [bonusResourceMins])
  useEffect(() => { localStorage.setItem(LS_KEY_BONUS_LEVELS, JSON.stringify(bonusTotalLevels)) }, [bonusTotalLevels])
  useEffect(() => { localStorage.setItem(LS_KEY_USE_RECON, JSON.stringify(useRecon)) }, [useRecon])
  useEffect(() => { localStorage.setItem(LS_KEY_RECON_STRICT, JSON.stringify(reconStrict)) }, [reconStrict])
  useEffect(() => { localStorage.setItem(LS_KEY_EXCLUDE_CAPITALS, JSON.stringify(excludeCapitals)) }, [excludeCapitals])

  const toast = useToast()

  const addScanMsg = useCallback((type, text) => {
    setScanMessages((prev) => [...prev, { type, text, ts: Date.now() }])
  }, [])

  const addAlliance = () => {
    const parts = newAlliance.split(',').map(s => s.trim()).filter(Boolean)
    if (parts.length === 0) return
    const newList = [...excludeAlliances]
    for (const v of parts) {
      if (!newList.includes(v)) newList.push(v)
    }
    setExcludeAlliances(newList)
    setNewAlliance('')
  }

  const addPlayer = () => {
    const parts = newPlayer.split(',').map(s => s.trim()).filter(Boolean)
    if (parts.length === 0) return
    const newList = [...excludePlayers]
    for (const v of parts) {
      if (!newList.includes(v)) newList.push(v)
    }
    setExcludePlayers(newList)
    setNewPlayer('')
  }

  const PHASE_LABELS = useMemo(() => ({
    recon_active: 'Background account active',
    recon_unavailable: 'Background account unavailable — using primary',
    map_scan: 'Scanning map regions...',
    map_scan_done: 'Map scan complete',
    pre_filter: 'Filtering tiles...',
    enriching: 'Enriching tile details...',
    enrich_done: 'Enrichment complete',
    player_pop: 'Querying player populations...',
    player_pop_done: 'Player populations loaded',
    player_profiles: 'Fetching player profiles...',
    profile_progress: 'Fetching player profiles...',
    post_filter: 'Applying filters...',
    stopped: 'Scan stopped',
    capital_parse_warning: 'Capital parser produced empty results',
  }), [])

  const handleScanMessage = useCallback((data) => {
    if (!mountedRef.current || !data) return
    switch (data.type) {
      case 'heartbeat':
        // Keepalive frame from the server — keeps the WS warm during long
        // silent phases. Nothing to render.
        break
      case 'session_init':
        addScanMsg('info', `Session: ${data.session_id} (viewable from /sessions)`)
        lastScanIdRef.current = data.session_id
        break
      case 'phase': {
        // profile_progress: {index, total, message: "Profile fetch: N/M"}
        // — show a phase line instead of letting the raw key leak through.
        if (data.phase === 'profile_progress' && data.total) {
          setScanPhase(`Fetching player profiles ${data.index}/${data.total}…`)
        } else {
          setScanPhase(PHASE_LABELS[data.phase] || data.phase)
        }
        addScanMsg(
          data.phase?.includes('done') || data.phase?.includes('complete') ? 'success' : 'info',
          data.message
        )
        if (data.detail) addScanMsg('detail', data.detail)
        break
      }
      case 'scan_region':
        addScanMsg('detail', `  Fetching map region ${data.index}/${data.total} at (${data.center.x},${data.center.y})`)
        break
      case 'enrich_progress':
        setEnrichProgress({ index: data.index, total: data.total, eta: data.eta, name: data.tile?.name })
        break
      case 'enrich_detail': {
        const t = data.tile
        if (t.error) {
          addScanMsg('error', `  [${data.index}/${data.total}] (${t.x},${t.y}) Failed: ${t.error}`)
        } else if (t.is_oasis) {
          // Oasis row: show bonus instead of pop/player/alliance — those
          // are usually empty for oases anyway. "Unoccupied" vs owner is
          // surfaced via the dedicated Type/Player columns in the table.
          const bonusStr = t.bonus ? ` — ${t.bonus}` : ''
          const ownerStr = t.player ? ` (owner: ${t.player})` : ''
          addScanMsg('detail', `  [${data.index}/${data.total}] (${t.x},${t.y}) Oasis${bonusStr}${ownerStr}`)
        } else {
          addScanMsg('detail', `  [${data.index}/${data.total}] (${t.x},${t.y}) ${t.name || '?'} — pop:${t.pop ?? '?'} player:${t.player || '-'} ally:${t.alliance || '-'}`)
        }
        break
      }
      case 'player_pops': {
        const players = data.players || []
        const source = data.source || 'visible'
        if (players.length > 0) {
          addScanMsg('info', source === 'profile'
            ? 'Player populations (from profile pages):'
            : 'Player populations (sum of visible villages):')
          for (const p of players) {
            const parts = p.villages.map((v) => `${v.name}(${v.x},${v.y})=${v.pop}`).join(' + ')
            const visibleSum = p.visible_total ?? p.total
            if (p.source === 'profile' && p.total !== visibleSum) {
              addScanMsg('info', `  ${p.name}: ${p.total} (profile) | visible: ${visibleSum} = ${parts}`)
            } else {
              addScanMsg('info', `  ${p.name}: ${p.total} = ${parts}`)
            }
          }
        }
        break
      }
      case 'complete': {
        const tiles = data.tiles || []
        setScanPhase(null)
        setEnrichProgress(null)
        setScanStats(data.stats || null)
        addScanMsg('success', `Scan complete: ${tiles.length} targets found in ${data.stats?.time_seconds || '?'}s`)
        // History-replay: don't reset selections or pop a stale toast for
        // a scan the user already saw. startedHereRef is only true when
        // the user clicked Scan in THIS mount.
        const fresh = startedHereRef.current
        onScanComplete(tiles, { preserveSelection: !fresh })
        setScanning(false)
        if (fresh) {
          toast.success(`Scan complete: ${tiles.length} targets found`)
          startedHereRef.current = false
        }
        break
      }
      case 'error':
        addScanMsg('error', data.message || 'Error')
        setScanPhase(null)
        setScanning(false)
        if (startedHereRef.current) {
          toast.error(data.message || 'Scan failed')
          startedHereRef.current = false
        }
        break
      case 'already_running':
        addScanMsg('warning', 'A scan is already running on the server — reattaching')
        break
      case 'operation_complete': {
        // Multi-tab / cross-device stop, or a stop initiated via the
        // /sessions page. Tab A clicked Stop; tab B was just viewing
        // — without this case, tab B's progress UI sat there silently.
        // Note: 'completed' status is normal end — `complete` was
        // already emitted with the results table, so we don't need to
        // do anything here for that. Only stopped/failed need a UI
        // hint since `complete` is never emitted in those paths.
        if (data.status === 'stopped') {
          setScanPhase(null)
          setEnrichProgress(null)
          setScanning(false)
          addScanMsg('warning', 'Scan stopped (from another tab or via /sessions)')
          startedHereRef.current = false
        } else if (data.status === 'failed') {
          setScanPhase(null)
          setEnrichProgress(null)
          setScanning(false)
          addScanMsg('error', 'Scan failed on the server')
          startedHereRef.current = false
        }
        break
      }
      default:
        if (data.message) addScanMsg('info', data.message)
    }
  }, [PHASE_LABELS, addScanMsg, onScanComplete, setScanning, toast])

  const scanOp = useResumableOperation('scout-scan', {
    onMessage: handleScanMessage,
    onStatusChange: (next) => {
      if (!mountedRef.current) return
      if (next === 'reconnecting') setScanPhase('Reconnecting...')
      // Keep the parent's `scanning` flag in sync with whichever
      // direction the hook is moving — without this, returning to /scout
      // mid-scan leaves the Scan button enabled (and a duplicate start
      // becomes possible).
      if (next === 'connecting' || next === 'running' || next === 'reconnecting') {
        setScanning(true)
      } else if (next === 'completed' || next === 'failed' || next === 'stopped' || next === 'idle') {
        setScanning(false)
      }
    },
  })

  // Mount-time reattach indicator — when the hook restored an existing
  // session_id from localStorage we're tailing a scan that started in a
  // previous mount; surface that in the UI so the user understands why
  // the Cancel button is showing without them having clicked anything.
  useEffect(() => {
    if (scanOp.sessionId && scanOp.status === 'reconnecting') {
      setScanPhase('Reconnecting to running scan...')
      setScanning(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleScan = () => {
    setScanning(true)
    setScanMessages([])
    setScanPhase('Connecting...')
    setEnrichProgress(null)
    setScanStats(null)
    startedHereRef.current = true

    const config = {
      radius, minPop, maxPop, maxPlayerPop, filterMode,
      excludeAlliances, excludePlayers,
    }
    onConfigChange?.(config)

    const body = {
      radius,
      min_pop: minPop,
      max_pop: maxPop,
      // Oases participate in results when the target type explicitly
      // includes them. The non-capitals mode is village-only territory,
      // so it stays oasis-free just like the plain "villages" mode.
      show_oases: filterMode === 'with-oases' || filterMode === 'oasis-only',
      oasis_only: filterMode === 'oasis-only',
      // Triggers the server's capital-identification path (profile
      // fetches per unique player) AND the post-filter that drops
      // is_capital tiles. Fires for the dedicated non-capitals target
      // type, OR as a modifier on the oasis-bonus mode (Exclude capital
      // villages) — the latter reuses the SAME per-player profile fetch
      // the oasis aggregation already needs, so it adds zero requests.
      non_capitals:
        filterMode === 'non-capitals' ||
        (filterMode === 'villages-by-oasis-bonus' && excludeCapitals),
      // "Villages by oasis bonus" mode — server fetches each in-radius
      // player's profile (reusing the population/capital fetch), reads each
      // village's occupied-oasis coords, fetches+caches the per-oasis bonus,
      // and filters villages on the aggregated breakdown via the shared
      // bonus_resource_mins / bonus_total_levels fields below.
      villages_by_oasis_bonus: filterMode === 'villages-by-oasis-bonus',
      // Default true even when the user hasn't explicitly toggled —
      // server side decides whether recon is actually used (it needs
      // creds configured + a successful login). Sending false here
      // forces fallback to the primary account.
      use_recon: useRecon,
      // Strict mode — if true AND use_recon is true AND recon
      // can't authenticate, the server aborts the scan rather
      // than silently falling back to the primary account.
      recon_strict: reconStrict,
      exclude_player_names: excludePlayers.flatMap((p) => p.split(',').map(s => s.trim())).filter(Boolean),
      village_id: activeVillageId || undefined,
    }
    if (maxPlayerPop !== '') body.max_player_pop = Number(maxPlayerPop)

    const allAlliances = excludeAlliances.flatMap((a) => a.split(',').map(s => s.trim())).filter(Boolean)
    const allianceIds = allAlliances.filter((a) => /^\d+$/.test(a)).map(Number)
    const allianceNames = allAlliances.filter((a) => !/^\d+$/.test(a))
    if (allianceIds.length > 0) body.exclude_alliance_ids = allianceIds
    if (allianceNames.length > 0) body.exclude_alliance_names = allianceNames

    // Oasis bonus filters. Send only the non-zero per-resource entries
    // (server treats absence as "no constraint"). Send the levels array
    // only when at least one bucket is selected — empty array would be
    // ambiguous between "no filter" and "explicitly nothing matches".
    const resourceMinsToSend = Object.fromEntries(
      Object.entries(bonusResourceMins).filter(([, v]) => v > 0)
    )
    if (Object.keys(resourceMinsToSend).length > 0) {
      body.bonus_resource_mins = resourceMinsToSend
    }
    if (bonusTotalLevels.length > 0) {
      body.bonus_total_levels = bonusTotalLevels.slice().sort((a, b) => a - b)
    }

    setScanPhase('Scanning map...')
    addScanMsg('info', `Starting scan: radius=${radius}, pop=${minPop}–${maxPop}`)
    scanOp.start('/ws/scout/scan', body)
  }

  const handleCancel = () => {
    // If the hook is already in a terminal state (op finished server-side
    // during a reconnect), the cancel intent is moot — suppress the
    // misleading "Scan cancelled by user" toast. Status from the hook is
    // authoritative; the cancel only fires the WS action when status is
    // actually running/reconnecting.
    const liveStatus = scanOp.status
    const isTerminal = (
      liveStatus === 'completed'
      || liveStatus === 'failed'
      || liveStatus === 'stopped'
      || liveStatus === 'idle'
    )
    scanOp.stop()
    setScanPhase(null)
    setScanning(false)
    if (!isTerminal) {
      addScanMsg('warning', 'Scan cancelled by user')
      toast.warning('Scan cancelled')
    }
  }

  return (
    <div className="card">
      <h3 className="heading-gold text-lg mb-4">Scan Configuration</h3>

      {/* Radius slider */}
      <div className="mb-4">
        <label className="field-label-lg">Radius: {radius}</label>
        <input type="range" min={5} max={100} value={radius} onChange={(e) => setRadius(Number(e.target.value))} className="w-full checkbox-gold" />
        <div className="flex justify-between text-xs text-secondary"><span>5</span><span>100</span></div>
      </div>

      {/* Population range — village */}
      <div className="flex gap-4 mb-4">
        <div className="flex-1">
          <label className="field-label-lg">Min Village Pop</label>
          <input type="number" className="input-field" value={minPop} min={0} onChange={(e) => setMinPop(Number(e.target.value))} />
        </div>
        <div className="flex-1">
          <label className="field-label-lg">Max Village Pop</label>
          <input type="number" className="input-field" value={maxPop} min={0} onChange={(e) => setMaxPop(Number(e.target.value))} />
        </div>
        <div className="flex-1">
          <label className="field-label-lg">Max Player Pop (all villages)</label>
          <input type="number" className="input-field" value={maxPlayerPop} placeholder="no limit" onChange={(e) => setMaxPlayerPop(e.target.value)} />
        </div>
      </div>

      {/* Target type — single-pick radio replaces the prior pair of
          checkboxes (showOases + oasisOnly with disable cross-talk). */}
      <div className="mb-4">
        <label className="field-label-lg mb-2">Target type</label>
        <div className="flex flex-col gap-1.5">
          <label className="check-label">
            <input
              type="radio"
              name="filterMode"
              value="villages"
              checked={filterMode === 'villages'}
              onChange={() => setFilterMode('villages')}
              className="accent-radio"
              disabled={scanning}
            />
            Player villages only
          </label>
          <label className="check-label">
            <input
              type="radio"
              name="filterMode"
              value="non-capitals"
              checked={filterMode === 'non-capitals'}
              onChange={() => setFilterMode('non-capitals')}
              className="accent-radio"
              disabled={scanning}
            />
            Non-capital villages only
            <span className="text-xs text-secondary ml-1">
              (costs ~1 profile fetch per unique player to identify
              capitals)
            </span>
          </label>
          <label className="check-label">
            <input
              type="radio"
              name="filterMode"
              value="with-oases"
              checked={filterMode === 'with-oases'}
              onChange={() => setFilterMode('with-oases')}
              className="accent-radio"
              disabled={scanning}
            />
            Villages + unoccupied oases
          </label>
          <label className="check-label">
            <input
              type="radio"
              name="filterMode"
              value="oasis-only"
              checked={filterMode === 'oasis-only'}
              onChange={() => setFilterMode('oasis-only')}
              className="accent-radio"
              disabled={scanning}
            />
            Oases only (occupied + unoccupied; ignore villages)
          </label>
          <label className="check-label">
            <input
              type="radio"
              name="filterMode"
              value="villages-by-oasis-bonus"
              checked={filterMode === 'villages-by-oasis-bonus'}
              onChange={() => setFilterMode('villages-by-oasis-bonus')}
              className="accent-radio"
              disabled={scanning}
            />
            Villages by oasis bonus
            <span className="text-xs text-secondary ml-1">
              (1 profile fetch per player — oasis bonus is read straight from
              the profile, no extra requests)
            </span>
          </label>
          {filterMode === 'villages-by-oasis-bonus' && (
            <label className="check-label ml-6">
              <input
                type="checkbox"
                checked={excludeCapitals}
                onChange={(e) => setExcludeCapitals(e.target.checked)}
                disabled={scanning}
                className="checkbox-gold"
              />
              Exclude capital villages —{' '}
              <span className="text-secondary text-xs">
                also drop each player&apos;s capital (same profile fetch — no
                extra requests)
              </span>
            </label>
          )}
        </div>
      </div>

      {/* Background recon account — when configured server-side, the
          read sweep (map_position, tile-details, profile pages) routes
          through a disposable Travian login. The user's primary
          account does no scout traffic at all, keeping bot-detection
          pressure on the throwaway. Falls back gracefully when the
          server-side recon credentials aren't set. */}
      <div className="mb-4">
        <label className="check-label">
          <input
            type="checkbox"
            checked={useRecon}
            onChange={(e) => setUseRecon(e.target.checked)}
            disabled={scanning}
            className="checkbox-gold"
          />
          Use background account for read ops (recommended) —{' '}
          <span className="text-secondary text-xs">
            keeps scout-request fingerprint off your main account
          </span>
        </label>
        {useRecon && (
          <label className="check-label ml-6 mt-1">
            <input
              type="checkbox"
              checked={reconStrict}
              onChange={(e) => setReconStrict(e.target.checked)}
              disabled={scanning}
              className="checkbox-gold"
            />
            Require background account —{' '}
            <span className="text-secondary text-xs">
              abort scan if it can&apos;t authenticate (no silent
              fallback to your active account)
            </span>
          </label>
        )}
        {useRecon && <BackgroundAccountPanel disabled={scanning} />}
      </div>

      {/* Oasis bonus filter — only meaningful when oases are in scope.
          Two axes that AND together: per-resource minimum % (must have
          ≥N% of every set resource) and total-bucket multi-select
          (sum must equal any selected bucket). Both axes empty = no
          filter, all oases pass. */}
      {(() => {
        const RESOURCES = [
          { id: 'wood', label: 'Wood' },
          { id: 'clay', label: 'Clay' },
          { id: 'iron', label: 'Iron' },
          { id: 'crop', label: 'Crop' },
        ]
        const LEVELS = [25, 50, 75, 100]
        const anyMinSet = Object.values(bonusResourceMins).some((v) => v > 0)
        const anyLevelSet = bonusTotalLevels.length > 0
        const filterActive = anyMinSet || anyLevelSet
        const villageMode = filterMode === 'villages-by-oasis-bonus'
        // The filter has no effect only when oases are entirely out of
        // scope — i.e. the plain villages mode. The village-by-bonus mode
        // applies the filter to each village's AGGREGATED oasis bonus.
        const noOasesInScope = filterMode === 'villages'
        const toggleLevel = (lv) => {
          setBonusTotalLevels((prev) =>
            prev.includes(lv) ? prev.filter((x) => x !== lv) : [...prev, lv]
          )
        }
        const resetBonusFilter = () => {
          setBonusResourceMins({ wood: 0, clay: 0, iron: 0, crop: 0 })
          setBonusTotalLevels([])
        }
        return (
          <div className="mb-4 p-3 rounded border-default bg-surface">
            <div className="flex items-center justify-between mb-2">
              <label className="field-label-lg">
                {villageMode ? 'Village oasis bonus filter' : 'Oasis bonus filter'}
              </label>
              {filterActive && (
                <button
                  type="button"
                  className="text-xs text-secondary underline hover:text-primary"
                  onClick={resetBonusFilter}
                  disabled={scanning}
                >Clear</button>
              )}
            </div>
            <div className="mb-3">
              <label className="field-label mb-1">Minimum % per resource</label>
              <div className="grid grid-cols-4 gap-2">
                {RESOURCES.map(({ id, label }) => (
                  <label key={id} className="flex flex-col items-center gap-1">
                    <span className="text-xs text-secondary">{label}</span>
                    <select
                      className="input-field text-center"
                      value={bonusResourceMins[id] || 0}
                      onChange={(e) =>
                        setBonusResourceMins({
                          ...bonusResourceMins,
                          [id]: Number(e.target.value),
                        })
                      }
                      disabled={scanning}
                    >
                      <option value={0}>—</option>
                      <option value={25}>25%</option>
                      <option value={50}>50%</option>
                      <option value={75}>75%</option>
                      <option value={100}>100%</option>
                    </select>
                  </label>
                ))}
              </div>
            </div>
            <div>
              <label className="field-label mb-1">
                {villageMode ? 'Minimum total bonus' : 'Total bonus level'}
              </label>
              <div className="flex flex-wrap gap-2">
                {LEVELS.map((lv) => {
                  const selected = bonusTotalLevels.includes(lv)
                  return (
                    <button
                      type="button"
                      key={lv}
                      className={
                        'px-3 py-1 rounded text-sm transition ' +
                        (selected
                          ? 'border-gold text-gold bg-card'
                          : 'border-default text-secondary hover:text-primary')
                      }
                      onClick={() => toggleLevel(lv)}
                      disabled={scanning}
                    >{lv}%</button>
                  )
                })}
              </div>
              <p className="text-xs text-secondary mt-1">
                {villageMode
                  ? 'A village passes if its TOTAL oasis bonus (summed across all its oases) is at least the lowest selected level. Empty = no total filter.'
                  : 'Multi-select. An oasis passes if its TOTAL bonus equals any selected bucket. Empty = no total filter.'}
              </p>
            </div>
            {filterActive && noOasesInScope && (
              <p className="text-xs text-warning mt-2">
                Filter set, but Target type is <span className="font-mono">villages</span>.
                Switch to <span className="font-mono">with oases</span> or <span className="font-mono">oasis only</span> to apply.
              </p>
            )}
          </div>
        )
      })()}

      <p className="text-xs text-secondary mb-4">Wilderness, abandoned valleys, and empty tiles are always skipped.</p>

      {/* Alliance exclusion */}
      <div className="mb-4">
        <label className="field-label-lg mb-1">Exclude Alliances (name or ID — persisted)</label>
        <div className="flex gap-2 items-center mb-2">
          <input className="input-field flex-1" placeholder="Alliance name or ID" value={newAlliance} onChange={(e) => setNewAlliance(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && addAlliance()} />
          <button className="btn-secondary btn-xs" onClick={addAlliance}>Add</button>
        </div>
        {excludeAlliances.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {excludeAlliances.map((a) => (
              <span key={a} className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-surface border-default text-secondary">
                {a}
                <button className="text-danger hover:text-primary ml-0.5" onClick={() => setExcludeAlliances(excludeAlliances.filter((x) => x !== a))}>x</button>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Player exclusion */}
      <div className="mb-4">
        <label className="field-label-lg mb-1">Exclude Players (persisted)</label>
        <div className="flex gap-2 items-center mb-2">
          <input className="input-field flex-1" placeholder="Player name" value={newPlayer} onChange={(e) => setNewPlayer(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && addPlayer()} />
          <button className="btn-secondary btn-xs" onClick={addPlayer}>Add</button>
        </div>
        {excludePlayers.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {excludePlayers.map((p) => (
              <span key={p} className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-surface border-default text-secondary">
                {p}
                <button className="text-danger hover:text-primary ml-0.5" onClick={() => setExcludePlayers(excludePlayers.filter((x) => x !== p))}>x</button>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="flex gap-3 items-center">
        <button className="btn-primary" onClick={handleScan} disabled={scanning}>
          {scanning ? 'Scanning...' : 'Scan Map'}
        </button>
        {scanning && <button className="btn-danger" onClick={handleCancel}>Cancel</button>}
      </div>

      {/* Live scan progress */}
      {(scanMessages.length > 0 || scanning) && (
        <ScanProgressPanel phase={scanPhase} messages={scanMessages} enrichProgress={enrichProgress} stats={scanStats} />
      )}
    </div>
  )
}

// ── Sort helpers ──────────────────────────────────────────────────────
function SortableHeader({ label, field, sortField, sortDir, onSort, className = '' }) {
  const active = sortField === field
  const arrow = active ? (sortDir === 'asc' ? ' \u25B2' : ' \u25BC') : ''
  return (
    <th onClick={() => onSort(field)} className={`sortable ${active ? 'sort-active' : ''} ${className}`}>
      {label}{arrow}
    </th>
  )
}

// ── Scan Results Table ────────────────────────────────────────────────
function ScanResultsTable({ results, selected, setSelected, farmLists, coordMap, onFarmAdded }) {
  const [sortField, setSortField] = useState(null)
  const [sortDir, setSortDir] = useState('asc')
  const [addFarmTarget, setAddFarmTarget] = useState(null)
  // Pagination — 10 rows per page so the table doesn't push the rest
  // of the page below the fold on common viewports. Resets to page 1
  // whenever the results list changes (new scan) or the sort changes,
  // since page indexes would otherwise point at unintended rows.
  const PAGE_SIZE = 10
  const [page, setPage] = useState(1)
  useEffect(() => { setPage(1) }, [results, sortField, sortDir])

  const handleSort = (field) => {
    if (sortField === field) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortField(field); setSortDir('asc') }
  }

  const originalIndices = useMemo(() => {
    if (!sortField) return results.map((_, i) => i)
    const indexed = results.map((r, i) => ({ r, i }))
    // bonus_total is a derived sort key: sum the canonical breakdown.
    // Non-oasis rows compute as -1 so they always sink (descending) or
    // surface (ascending) but never interleave with oases.
    const computeSortValue = (r) => {
      if (sortField !== 'bonus_total') return r[sortField] ?? 0
      // Prefer the village-aggregated breakdown (villages-by-oasis-bonus
      // mode); fall back to a per-oasis breakdown. Rows with neither sink
      // to -1 so they never interleave with rows that have a bonus.
      const bd = r.village_oasis_count > 0
        ? r.village_oasis_breakdown
        : (r.is_oasis ? r.bonus_breakdown : null)
      if (!bd || typeof bd !== 'object') return -1
      let total = 0
      for (const v of Object.values(bd)) {
        if (typeof v === 'number') total += v
      }
      return total
    }
    indexed.sort((a, b) => {
      let av = computeSortValue(a.r)
      let bv = computeSortValue(b.r)
      if (typeof av === 'string') { av = av.toLowerCase(); bv = (bv || '').toLowerCase() }
      if (av < bv) return sortDir === 'asc' ? -1 : 1
      if (av > bv) return sortDir === 'asc' ? 1 : -1
      return 0
    })
    return indexed.map((x) => x.i)
  }, [results, sortField, sortDir])

  const sorted = useMemo(() => originalIndices.map((i) => results[i]), [results, originalIndices])

  // Page math, defensively clamped so a stale page index from a
  // sort change can't overshoot when the results array shrinks.
  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE))
  const safePage = Math.min(Math.max(1, page), totalPages)
  const pageStart = (safePage - 1) * PAGE_SIZE
  const pageEnd = pageStart + PAGE_SIZE
  const pageSlice = useMemo(
    () => sorted.slice(pageStart, pageEnd),
    [sorted, pageStart, pageEnd],
  )
  const pageOriginalIndices = useMemo(
    () => originalIndices.slice(pageStart, pageEnd),
    [originalIndices, pageStart, pageEnd],
  )

  const allSelected = results.length > 0 && selected.size === results.length
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(results.map((_, i) => i)))
  const toggleRow = (idx) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx); else next.add(idx)
      return next
    })
  }

  const downloadMarkdown = () => {
    // Render the same columns the table shows. Pipe characters in
    // village or player names would break the table row — escape
    // them. Newlines in the same fields would as well; replace with
    // a literal space.
    const esc = (v) => String(v ?? '')
      .replace(/\|/g, '\\|')
      .replace(/[\r\n]+/g, ' ')
    const headers = [
      'Coords', 'Village', 'V.Pop', 'Player Pop',
      'Distance', 'Bonus', 'Player', 'Alliance', 'Type',
    ]
    const rows = sorted.map((r) => [
      `(${r.x},${r.y})`,
      esc(r.village_name || r.name || '---'),
      r.population ?? 0,
      r.owner_population ?? 0,
      r.distance != null ? r.distance.toFixed(1) : '---',
      esc(r.bonus || ''),
      esc(r.player_name || 'Unoccupied'),
      esc(r.alliance_name || '---'),
      r.is_oasis ? 'Oasis' : r.is_abandoned ? 'Abandoned' : 'Village',
    ])
    const now = new Date()
    const stamp = now.toISOString().replace(/[:.]/g, '-').slice(0, 19)
    const lines = [
      `# Auto-Scout Results`,
      ``,
      `- Captured at: ${now.toISOString()}`,
      `- Targets: ${results.length}`,
      ``,
      `| ${headers.join(' | ')} |`,
      `|${headers.map(() => '---').join('|')}|`,
      ...rows.map((row) => `| ${row.join(' | ')} |`),
      ``,
    ]
    const md = lines.join('\n')
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `scout-results-${stamp}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="card">
      <div className="flex justify-between items-center mb-3 flex-wrap gap-2">
        <h3 className="heading-gold text-lg">Scan Results ({results.length} targets)</h3>
        <div className="flex gap-2 items-center">
          <button
            className="btn-secondary btn-xs"
            onClick={downloadMarkdown}
            title="Export the current results table to a Markdown file (all visible columns)."
          >
            Download .md
          </button>
          <button className="btn-secondary btn-xs" onClick={toggleAll}>{allSelected ? 'Deselect All' : 'Select All'}</button>
          <span className="text-xs text-secondary">{selected.size} selected</span>
        </div>
      </div>

      {/* Drop the fixed max-height on the scroll container — with
          pagination we never render more than PAGE_SIZE rows at a
          time, so the table fits naturally and doesn't push the
          page below the fold. Horizontal-scroll wrapper preserved
          for narrow viewports. */}
      <div className="overflow-x-auto">
        <table className="data-table">
          <thead className="sticky top-0 bg-card z-[1]">
            <tr>
              <th className="w-10">
                <input type="checkbox" checked={allSelected} onChange={toggleAll} className="checkbox-gold" />
              </th>
              <th>Coords</th>
              <SortableHeader label="Village" field="village_name" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <SortableHeader label="V.Pop" field="population" sortField={sortField} sortDir={sortDir} onSort={handleSort} className="text-center" />
              <SortableHeader label="Player Pop" field="owner_population" sortField={sortField} sortDir={sortDir} onSort={handleSort} className="text-center" />
              <SortableHeader label="Distance" field="distance" sortField={sortField} sortDir={sortDir} onSort={handleSort} className="text-center" />
              <SortableHeader label="Bonus" field="bonus_total" sortField={sortField} sortDir={sortDir} onSort={handleSort} className="text-center" />
              <SortableHeader label="Player" field="player_name" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <th>Alliance</th>
              <SortableHeader label="Type" field="is_oasis" sortField={sortField} sortDir={sortDir} onSort={handleSort} />
              <th>Farm Lists</th>
              <th className="w-10"></th>
            </tr>
          </thead>
          <tbody>
            {pageSlice.map((row, sliceIdx) => {
              const origIdx = pageOriginalIndices[sliceIdx]
              const isSelected = selected.has(origIdx)
              return (
                <tr key={origIdx} onClick={() => toggleRow(origIdx)} className={`row-clickable ${isSelected ? 'row-selected' : ''}`}>
                  <td><input type="checkbox" checked={isSelected} onChange={() => toggleRow(origIdx)} onClick={(e) => e.stopPropagation()} className="checkbox-gold" /></td>
                  <td className="font-mono text-gold"><MapCoord x={row.x} y={row.y} /></td>
                  <td>{row.village_name || row.name || '---'}</td>
                  <td
                    className="text-center font-mono"
                    title={row.is_oasis && !row.player_id ? 'Unoccupied oasis — no owning village' : ''}
                  >
                    {(row.population ?? 0) === 0
                      ? <span className="text-secondary opacity-50">0</span>
                      : row.population}
                  </td>
                  <td className="text-center font-mono">
                    {row.player_id
                      ? (row.owner_population || <span className="text-secondary opacity-50">?</span>)
                      : <span className="text-secondary opacity-50">0</span>}
                  </td>
                  <td className="text-center font-mono">{row.distance != null ? row.distance.toFixed(1) : '---'}</td>
                  <td className="text-center font-mono whitespace-nowrap">
                    {row.village_oasis_count > 0
                      ? (row.village_oasis_bonus
                          ? <span className="text-gold" title={`${row.village_oasis_count} occupied oasis/oases`}>{row.village_oasis_bonus}</span>
                          : <span className="text-secondary opacity-30" title="Village oasis bonus could not be parsed">—</span>)
                      : row.is_oasis
                        ? (row.bonus
                            ? <span className="text-gold">{row.bonus}</span>
                            : <span className="text-secondary opacity-30" title="Oasis bonus could not be parsed">—</span>)
                        : <span className="text-secondary opacity-15">·</span>}
                  </td>
                  <td className={row.player_name ? 'text-primary' : 'text-secondary italic'}>{row.player_name || 'Unoccupied'}</td>
                  <td className="text-secondary text-xs">{row.alliance_name || '---'}</td>
                  <td>
                    {row.is_oasis ? 'Oasis' : row.is_abandoned ? 'Abandoned' : 'Village'}
                  </td>
                  <td>
                    {(coordMap?.[`${row.x},${row.y}`] || []).map((entry) => (
                      <span key={entry.list_id} className="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-surface border-default text-gold mr-1 mb-0.5">
                        {entry.list_name}
                      </span>
                    ))}
                  </td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <button className="btn-secondary btn-xs" title="Add to farm list" onClick={() => setAddFarmTarget(row)}>+Farm</button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {/* Pagination controls — show only when there's more than one
          page. Compact: First / Prev / "Page N of M (rows A–B of T)"
          / Next / Last. Disabled buttons are visually muted so users
          can see they're at a boundary. */}
      {totalPages > 1 && (
        <div className="flex justify-between items-center mt-3 flex-wrap gap-2 text-xs">
          <div className="text-secondary">
            Showing rows {pageStart + 1}–{Math.min(pageEnd, sorted.length)} of {sorted.length}
          </div>
          <div className="flex gap-1 items-center">
            <button
              type="button"
              className="btn-secondary btn-xs"
              disabled={safePage === 1}
              onClick={() => setPage(1)}
              title="First page"
            >« First</button>
            <button
              type="button"
              className="btn-secondary btn-xs"
              disabled={safePage === 1}
              onClick={() => setPage(p => Math.max(1, p - 1))}
            >‹ Prev</button>
            <span className="px-2">
              Page <span className="text-gold font-mono">{safePage}</span> of {totalPages}
            </span>
            <button
              type="button"
              className="btn-secondary btn-xs"
              disabled={safePage === totalPages}
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
            >Next ›</button>
            <button
              type="button"
              className="btn-secondary btn-xs"
              disabled={safePage === totalPages}
              onClick={() => setPage(totalPages)}
              title="Last page"
            >Last »</button>
          </div>
        </div>
      )}
      {farmLists && farmLists.length > 0 && (
        <AddToFarmDialog
          open={!!addFarmTarget}
          target={addFarmTarget}
          farmLists={farmLists}
          onClose={() => setAddFarmTarget(null)}
          onAdded={onFarmAdded}
        />
      )}
    </div>
  )
}

// ── Auto-Scout Panel ──────────────────────────────────────────────────
function AutoScoutPanel({ scanResults, selected, scanConfig }) {
  const [amount, setAmount] = useState(1)
  const [scoutType, setScoutType] = useState('resources')
  const [delayMin, setDelayMin] = useState(2)
  const [delayMax, setDelayMax] = useState(5)
  const [running, setRunning] = useState(false)
  const [wsStatus, setWsStatus] = useState('disconnected')
  const [messages, setMessages] = useState([])
  const [progress, setProgress] = useState(null)
  const mountedRef = useRef(true)
  // The auto-scout op runs server-side via OperationManager. We use the
  // resumable hook so iOS Safari background / page reload / network drop
  // doesn't kill the sweep — the next pageshow reattaches.
  const passResolverRef = useRef(null)
  const activeVillageId = useGameStore((s) => s.activeVillageId)
  const toast = useToast()

  // Idle scout count
  const [idleScouts, setIdleScouts] = useState(null)
  const [checkingScouts, setCheckingScouts] = useState(false)

  const checkIdleScouts = async () => {
    if (!activeVillageId) return
    setCheckingScouts(true)
    try {
      const res = await api.get(`/military/troops?village_id=${activeVillageId}`)
      const troops = res.data
      // Scout unit depends on tribe: t3 for Gauls, t4 for Romans/Teutons
      const tribeId = useGameStore.getState().tribeId
      const scoutKey = tribeId === 3 ? 't3' : 't4'
      const count = troops[scoutKey] || 0
      setIdleScouts(count)
    } catch {
      setIdleScouts('API not available')
    } finally {
      setCheckingScouts(false)
    }
  }

  // Loop mode state
  const [loopEnabled, setLoopEnabled] = useState(false)
  const [loopInterval, setLoopInterval] = useState(300) // seconds between cycles
  const [loopDuration, setLoopDuration] = useState(0) // 0 = infinite
  const [loopCycle, setLoopCycle] = useState(0)
  const loopStoppedRef = useRef(false)
  const loopTimerRef = useRef(null)
  const loopStartRef = useRef(null)

  // Round-robin resume position
  const [resumeIndex, setResumeIndex] = useState(0)

  useEffect(() => { return () => {
    mountedRef.current = false
    loopStoppedRef.current = true
    if (loopTimerRef.current) clearTimeout(loopTimerRef.current)
  } }, [])

  const msgIdRef = useRef(0)
  const addMessage = useCallback((type, text) => {
    setMessages((prev) => [...prev, { id: ++msgIdRef.current, type, text, timestamp: Date.now() }])
  }, [])

  // Refs for values that the loop needs at execution time (avoids stale closures)
  const scanResultsRef = useRef(scanResults)
  const selectedRef = useRef(selected)
  const scanConfigRef = useRef(scanConfig)
  const amountRef = useRef(amount)
  const scoutTypeRef = useRef(scoutType)
  const delayMinRef = useRef(delayMin)
  const delayMaxRef = useRef(delayMax)
  const villageIdRef = useRef(activeVillageId)
  const resumeIndexRef = useRef(resumeIndex)
  const loopDurationRef = useRef(loopDuration)
  useEffect(() => { scanResultsRef.current = scanResults }, [scanResults])
  useEffect(() => { selectedRef.current = selected }, [selected])
  useEffect(() => { scanConfigRef.current = scanConfig }, [scanConfig])
  useEffect(() => { amountRef.current = amount }, [amount])
  useEffect(() => { scoutTypeRef.current = scoutType }, [scoutType])
  useEffect(() => { delayMinRef.current = delayMin }, [delayMin])
  useEffect(() => { delayMaxRef.current = delayMax }, [delayMax])
  useEffect(() => { villageIdRef.current = activeVillageId }, [activeVillageId])
  useEffect(() => { resumeIndexRef.current = resumeIndex }, [resumeIndex])
  useEffect(() => { loopDurationRef.current = loopDuration }, [loopDuration])

  // Hook handler: this runs for EVERY message, including history replay
  // after a page reload / Safari resume reattaches via localStorage. The
  // earlier per-pass router design dropped resumed messages because
  // runOnePass hadn't yet installed it. Inlining the UI updates here
  // means a backgrounded sweep that ends while you were away still
  // updates progress + appends results when you come back.
  // Pass-completion still resolves only on `operation_complete` so the
  // next loop cycle waits for OperationManager cleanup (the inner
  // `complete` frame fires before the per-user `scout` label is
  // released; resolving on it would race the next start).
  const handleAutoMessage = useCallback((data) => {
    if (!data || !mountedRef.current) return
    switch (data.type) {
      case 'session_init': addMessage('info', `Session: ${data.session_id} (viewable from /sessions)`); break
      case 'trigger_info': addMessage('warning', `$ ${data.command}`); break
      case 'scanning': addMessage('info', data.message || 'Scanning map...'); break
      case 'scan_complete': addMessage('success', `Scan: ${data.targets} targets`); break
      case 'target_list':
        addMessage('info', `Targets queued: ${(data.targets || []).length} villages`)
        break
      case 'player_pops': {
        const players = data.players || []
        const popSource = data.source || 'visible'
        if (players.length > 0) {
          addMessage('info', popSource === 'profile'
            ? 'Player populations (from profile pages):'
            : 'Player max population (visible villages sum):')
          for (const p of players) {
            const parts = p.villages.map((v) => `${v.name}(${v.x},${v.y})=${v.pop}`).join(' + ')
            const visibleSum = p.visible_total ?? p.total
            if (p.source === 'profile' && p.total !== visibleSum) {
              addMessage('info', `  ${p.name}: ${p.total} (profile) | visible: ${visibleSum} = ${parts}`)
            } else {
              addMessage('info', `  ${p.name}: ${p.total} = ${parts}`)
            }
          }
        }
        break
      }
      case 'scouting':
        setProgress({ index: data.index, total: data.total, eta: data.eta })
        addMessage('info', `[${data.index}/${data.total}] Scouting (${data.target.x},${data.target.y}) ${data.target.name || ''}${data.eta ? ' | ' + data.eta : ''}`)
        break
      case 'scout_result': {
        const ok = data.success
        const errStr = !ok && data.error ? `: ${data.error}` : ''
        const ttStr = data.travel_time ? ` | ${data.travel_time}` : ''
        const t = data.target || {}
        addMessage(ok ? 'success' : 'warning', `[${data.index || '?'}/${data.total || '?'}] (${t.x ?? '?'},${t.y ?? '?'}) ${ok ? 'Sent' : 'Failed'}${errStr}${ttStr}`)
        break
      }
      case 'waiting':
        setProgress((prev) => prev ? { ...prev, waitRemaining: data.remaining } : prev)
        break
      case 'complete': {
        const timeStr = data.total_time_seconds ? ` in ${data.total_time_seconds}s` : ''
        const avgStr = data.avg_time_per_target ? ` (avg ${data.avg_time_per_target}s/target)` : ''
        addMessage('success', `Pass done: ${data.successful}/${data.total_sent} sent${timeStr}${avgStr}`)
        setProgress(null); setWsStatus('disconnected')
        if (data.next_start_index != null) setResumeIndex(data.next_start_index)
        else setResumeIndex((prev) => (prev + (data.total_sent || 0)) % (scanResultsRef.current?.length || 1))
        break
      }
      case 'scout_preflight':
        addMessage('info', `Scouts available: ${data.available}${data.needed_per_target > 1 ? ` (${data.needed_per_target} per target)` : ''} — can send to ${data.can_send_to}/${data.total_targets} targets`)
        break
      case 'scouts_capped':
        addMessage('warning', data.message || `Capped to ${data.can_send_to} targets (${data.available} scouts idle)`)
        break
      case 'scouts_exhausted':
        addMessage('warning', data.message || `Scouts ran out after ${data.sent_so_far} sends`)
        setProgress(null)
        break
      case 'scouts_low':
        addMessage('warning', `Scouts running low: ${data.remaining} remaining`)
        break
      case 'noise_action':
        addMessage('info', data.message || 'Stealth: idle browsing...')
        break
      case 're_navigate':
        addMessage('info', data.message || 'Stealth: breaking request pattern...')
        break
      case 'error': addMessage('error', data.message || 'Error'); break
      case 'already_running':
        addMessage('warning', data.message || 'Auto-scout already running on the server — reattaching')
        if (!running) setRunning(true)
        break
      case 'operation_complete': {
        const resolver = passResolverRef.current
        passResolverRef.current = null
        resolver?.()
        break
      }
      default: if (data.message) addMessage('info', data.message); break
    }
  }, [addMessage, running])

  const handleAutoStatus = useCallback((next) => {
    if (next === 'running') setWsStatus('running')
    if (next === 'reconnecting') setWsStatus('reconnecting')
    if (next === 'failed' || next === 'stopped') {
      setWsStatus('disconnected')
      const resolver = passResolverRef.current
      passResolverRef.current = null
      resolver?.()
    }
  }, [])

  const scoutAutoOp = useResumableOperation('scout-auto', {
    onMessage: handleAutoMessage,
    onStatusChange: handleAutoStatus,
  })

  // If we mount with a stored session_id, the op is running server-side.
  // Reflect that in `running` so the Stop button is visible and Start
  // is hidden — otherwise the page would offer to start a duplicate.
  useEffect(() => {
    if (scoutAutoOp.sessionId && !running) {
      setRunning(true)
      addMessage('info', `Resumed running auto-scout (session ${scoutAutoOp.sessionId})`)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scoutAutoOp.sessionId])

  // Core: run one scout pass via the resumable hook, returns a promise that
  // resolves on the terminal `complete`/`operation_complete` frame OR a 5-min
  // safety timeout.
  const runOnePass = useCallback((cycleNum) => {
    return new Promise((resolve) => {
      if (!mountedRef.current || loopStoppedRef.current) { resolve(); return }
      let resolved = false
      const safeResolve = () => { if (!resolved) { resolved = true; resolve() } }
      // Pass safety timeout: 5 minutes was too aggressive — a real auto-
      // scout pass with stealth delays + 50+ targets routinely takes
      // 10-30 minutes. The op runs detached server-side anyway; this is
      // just a watchdog so loop mode doesn't deadlock if the WS drops
      // and we never see operation_complete. Bumping to 60 minutes
      // covers practical sweep sizes; loop mode handles longer sessions
      // by restarting passes.
      const safetyTimer = setTimeout(() => {
        addMessage('warning', 'Pass timed out after 60 minutes (server may still be running — check /sessions)')
        setWsStatus('disconnected')
        safeResolve()
      }, 60 * 60 * 1000)
      passResolverRef.current = () => { clearTimeout(safetyTimer); safeResolve() }

      const curResults = scanResultsRef.current
      const curSelected = selectedRef.current
      const targets = curResults
        .filter((_, i) => curSelected.has(i))
        .map((r) => ({ x: r.x, y: r.y, name: r.village_name || r.name || '', pop: r.population || 0, player: r.player_name || '' }))
      setWsStatus('connecting')
      if (cycleNum > 0) addMessage('info', `--- Loop cycle ${cycleNum + 1} ---`)

      // Spawn the detached op via the resumable hook. Message handling
      // is shared by the always-running handleAutoMessage so resumes
      // restored via localStorage still update the UI even if no pass
      // is locally in flight.
      scoutAutoOp.start('/ws/scout/auto', {
        radius: scanConfigRef.current.radius || 10,
        amount: amountRef.current,
        type: scoutTypeRef.current,
        delay_min: delayMinRef.current,
        delay_max: delayMaxRef.current,
        targets: targets,
        village_id: villageIdRef.current,
        start_index: resumeIndexRef.current,
      })
    })
  }, [addMessage])  // eslint-disable-line react-hooks/exhaustive-deps

  const handleStart = async () => {
    if (selected.size === 0) { toast.warning('No targets selected'); return }
    if (loopTimerRef.current) { clearTimeout(loopTimerRef.current); loopTimerRef.current = null }
    setRunning(true); setMessages([]); setProgress(null)
    loopStoppedRef.current = false
    setLoopCycle(0)

    if (!loopEnabled) {
      // Single pass — reset resume index for fresh run
      setResumeIndex(0)
      resumeIndexRef.current = 0
      await runOnePass(0)
      if (mountedRef.current) { setRunning(false); toast.success('Scouting complete') }
    } else {
      // Loop mode
      loopStartRef.current = Date.now()
      let cycle = 0
      const loop = async () => {
        if (loopStoppedRef.current || !mountedRef.current) { setRunning(false); return }
        // Check duration limit
        if (loopDurationRef.current > 0 && loopStartRef.current) {
          const elapsedMin = (Date.now() - loopStartRef.current) / 60000
          if (elapsedMin >= loopDurationRef.current) {
            addMessage('info', `Duration limit reached (${loopDurationRef.current} min). Stopping.`)
            setRunning(false)
            return
          }
        }
        setLoopCycle(cycle)
        await runOnePass(cycle)
        cycle++
        if (loopStoppedRef.current || !mountedRef.current) { setRunning(false); return }
        const safeInterval = Math.max(loopInterval, 30)
        addMessage('info', `Waiting ${safeInterval}s before next cycle...`)
        loopTimerRef.current = setTimeout(loop, safeInterval * 1000)
      }
      await loop()
    }
  }

  const handleStop = () => {
    loopStoppedRef.current = true
    if (loopTimerRef.current) { clearTimeout(loopTimerRef.current); loopTimerRef.current = null }
    // Op is detached server-side; send stop via the hook (which routes
    // through the live WS — starter or session-stream — depending on
    // whether we're tailing a fresh op or a resumed one).
    scoutAutoOp.stop()
    setRunning(false); setWsStatus('disconnected')
    addMessage('warning', 'Stopped by user')
    toast.warning('Auto-scout stopped')
  }

  const progressPct = progress ? (progress.index / progress.total) * 100 : 0
  const waitPct = progress?.waitRemaining != null ? progress.waitRemaining : null

  return (
    <div className="card">
      <h3 className="heading-gold text-lg mb-4">Auto-Scout</h3>

      {/* Scouts per target + idle scout check */}
      <div className="flex gap-4 mb-4 flex-wrap">
        <div className="flex-1 min-w-[120px]">
          <label className="field-label-lg">Scouts per target</label>
          <input type="number" className="input-field" value={amount} min={1} max={20} onChange={(e) => setAmount(Number(e.target.value))} disabled={running} />
          <div className="flex items-center gap-2 mt-1">
            <button className="btn-secondary btn-xs" onClick={checkIdleScouts} disabled={checkingScouts || running}>
              {checkingScouts ? '...' : 'Check'}
            </button>
            {idleScouts !== null && (
              <span className="text-xs text-secondary">
                {typeof idleScouts === 'number' ? `${idleScouts} scouts idle in village` : idleScouts}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Stealth delay range */}
      <div className="mb-4">
        <label className="field-label-lg">Stealth delay</label>
        <div className="flex items-center gap-2">
          <input type="number" className="input-field w-20" value={delayMin} min={0} max={60} onChange={(e) => setDelayMin(Number(e.target.value))} disabled={running} />
          <span className="text-secondary">s</span>
          <span className="text-secondary">&mdash;</span>
          <input type="number" className="input-field w-20" value={delayMax} min={0} max={120} onChange={(e) => setDelayMax(Number(e.target.value))} disabled={running} />
          <span className="text-secondary">s</span>
        </div>
        <p className="text-xs text-secondary mt-1">Human-like delay — heavy-tailed distribution (most delays shorter, occasional longer pauses)</p>
      </div>

      {/* Scout type */}
      <div className="mb-4">
        <label className="field-label-lg mb-2">Scout type</label>
        <div className="flex gap-6">
          <label className="check-label">
            <input type="radio" name="scoutType" value="resources" checked={scoutType === 'resources'} onChange={() => setScoutType('resources')} disabled={running} className="accent-radio" /> Resources
          </label>
          <label className="check-label">
            <input type="radio" name="scoutType" value="defenses" checked={scoutType === 'defenses'} onChange={() => setScoutType('defenses')} disabled={running} className="accent-radio" /> Defenses
          </label>
          <label className="check-label">
            <input type="radio" name="scoutType" value="both" checked={scoutType === 'both'} onChange={() => setScoutType('both')} disabled={running} className="accent-radio" /> Both
          </label>
        </div>
      </div>

      {/* Resume position indicator */}
      {resumeIndex > 0 && !running && (
        <div className="mb-4">
          <span className="text-xs text-secondary">Resuming from target #{resumeIndex + 1}</span>
        </div>
      )}

      {/* Loop mode */}
      <div className="mb-5 p-3 bg-surface rounded-md border-default">
        <div className="flex gap-4 items-center flex-wrap">
          <label className="check-label">
            <input type="checkbox" className="checkbox-gold" checked={loopEnabled} onChange={(e) => setLoopEnabled(e.target.checked)} disabled={running} />
            Loop mode
          </label>
          {loopEnabled && running && (
            <span className="text-xs text-gold font-semibold">Cycle #{loopCycle + 1}</span>
          )}
        </div>
        {loopEnabled && (
          <div className="flex items-center gap-4 flex-wrap mt-3">
            <div className="flex items-center gap-2">
              <label className="text-xs text-secondary">Interval (s):</label>
              <input type="number" className="input-field text-xs py-1 px-2 w-20" min={30} max={3600} value={loopInterval} onChange={(e) => setLoopInterval(Number(e.target.value) || 300)} disabled={running} />
            </div>
            <div className="flex items-center gap-2">
              <label className="text-xs text-secondary">Duration (min):</label>
              <input type="number" className="input-field text-xs py-1 px-2 w-20" min={0} max={1440} value={loopDuration} onChange={(e) => setLoopDuration(Number(e.target.value) || 0)} disabled={running} />
              <span className="text-xs text-secondary opacity-70">0 = infinite</span>
            </div>
          </div>
        )}
        {loopEnabled && (
          <p className="text-xs text-secondary mt-2">Scan once, then re-scout the same targets every {loopInterval}s. Scouts return home and get re-sent.</p>
        )}
      </div>

      {/* Buttons */}
      <div className="flex gap-3 mb-4">
        {!running
          ? <button className="btn-primary" onClick={handleStart} disabled={selected.size === 0}>
              {loopEnabled ? `Start Scout Loop (${selected.size} targets)` : `Start Auto-Scout (${selected.size} targets)`}
            </button>
          : <button className="btn-danger" onClick={handleStop}>Stop</button>}
      </div>
      {progress && (
        <div className="mb-4">
          <div className="flex justify-between text-xs text-secondary mb-1">
            <span>Target {progress.index}/{progress.total}{progress.eta ? ` | ${progress.eta}` : ''}</span>
            <span>{Math.round(progressPct)}%{waitPct != null ? ` | cooldown ${Math.round(waitPct)}s` : ''}</span>
          </div>
          <div className="progress-track"><div className="progress-fill" style={{ width: `${progressPct}%` }} /></div>
        </div>
      )}
      {messages.length > 0 && <WebSocketPanel messages={messages} status={wsStatus} onClear={() => setMessages([])} />}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────
export default function AutoScout() {
  const [scanResults, setScanResults] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [scanning, setScanning] = useState(false)
  const [scanConfig, setScanConfig] = useState({ radius: 10 })
  const activeVillageId = useGameStore((s) => s.activeVillageId)

  // Farm list integration
  const [farmLists, setFarmLists] = useState([])
  const [coordMap, setCoordMap] = useState({})

  const fetchFarmData = useCallback(async () => {
    try {
      const [listsRes, mapRes] = await Promise.all([
        api.get('/farm/lists'),
        api.get('/farm/coord-map'),
      ])
      setFarmLists(Array.isArray(listsRes.data) ? listsRes.data : [])
      setCoordMap(mapRes.data && typeof mapRes.data === 'object' ? mapRes.data : {})
    } catch { /* farm integration is optional — silently degrade */ }
  }, [])

  useEffect(() => { fetchFarmData() }, [fetchFarmData])

  const handleFarmAdded = useCallback((listId, x, y) => {
    const list = farmLists.find((l) => l.id === listId)
    const key = `${x},${y}`
    setCoordMap((prev) => ({
      ...prev,
      [key]: [...(prev[key] || []), { list_id: listId, list_name: list?.name || '?' }],
    }))
  }, [farmLists])

  const handleScanComplete = (results, opts = {}) => {
    setScanResults(results)
    // History-replay (user navigated back to /scout after a scan
    // completed in the background): keep prior selections so the user
    // doesn't lose their deselect work. Fresh scans select-all by default.
    if (!opts.preserveSelection) {
      setSelected(new Set(results.map((_, i) => i)))
    }
    fetchFarmData() // refresh farm data after scan
  }

  return (
    <div className="p-6 max-w-[1100px] mx-auto">
      <div className="flex justify-between items-center mb-5">
        <h2 className="heading-gold text-2xl">Auto Scout</h2>
        <div className="flex items-center gap-3">
          <span
            className="text-[10px] text-secondary opacity-50 font-mono"
            title="Bundle build marker. wd12 = wd11 + Scan Results pagination (10 rows/page) — removes the fixed-height scroll container so the table doesn't push the rest of the page below the fold."
          >
            build: wd12
          </span>
          <VillageSelector />
        </div>
      </div>
      <div className="flex flex-col gap-4">
        <ScanConfigPanel onScanComplete={handleScanComplete} scanning={scanning} setScanning={setScanning} onConfigChange={setScanConfig} activeVillageId={activeVillageId} />
        {scanResults && scanResults.length > 0 && (
          <>
            <ScanResultsTable results={scanResults} selected={selected} setSelected={setSelected} farmLists={farmLists} coordMap={coordMap} onFarmAdded={handleFarmAdded} />
            <AutoScoutPanel scanResults={scanResults} selected={selected} scanConfig={scanConfig} />
          </>
        )}
        {scanResults && scanResults.length === 0 && (
          <div className="card text-center p-8"><p className="text-secondary">No targets found. Try increasing the radius or adjusting filters.</p></div>
        )}
      </div>
    </div>
  )
}
