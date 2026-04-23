import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import useGameStore from '../stores/gameStore'
import { createWebSocket } from '../ws'
import { useToast } from '../components/Toast'
import ConfirmDialog from '../components/ConfirmDialog'
import ScanConfig from '../components/FarmBuilder/ScanConfig'
import MatrixSpec from '../components/FarmBuilder/MatrixSpec'
import AdvancedSpec from '../components/FarmBuilder/AdvancedSpec'
import ScanPreviewTable from '../components/FarmBuilder/ScanPreviewTable'
import DefenseScanPlan from '../components/FarmBuilder/DefenseScanPlan'
import LiveRunView from '../components/FarmBuilder/LiveRunView'

const DEFAULT_CONFIG = {
  radius: 30,
  max_player_total_pop: 220,
  home_village_ids: [],
  exclude_alliance_tags: [],
  exclude_player_names: [],
  spec_mode: 'advanced',
  name_template: '{home_short}-{pop_label}-{radius}',
  home_shorts: {},
  home_priority: [],
  pop_buckets: [
    { label: 'S', max_pop: 120 },
    { label: 'M', max_pop: null },
  ],
  advanced_rows: [],
}

export default function FarmBuilder() {
  const villages = useGameStore((s) => s.villages) || []
  const tribeId = useGameStore((s) => s.tribeId)
  const navigate = useNavigate()
  const toast = useToast()

  const [config, setConfig] = useState(() => {
    try {
      const saved = localStorage.getItem('farm_builder_config')
      if (saved) return { ...DEFAULT_CONFIG, ...JSON.parse(saved) }
    } catch { /* */ }
    return { ...DEFAULT_CONFIG, home_village_ids: villages.map((v) => v.id) }
  })

  const [step, setStep] = useState('config')  // config | preview | plan | live
  const [preview, setPreview] = useState(null)
  const [sessionId, setSessionId] = useState(null)
  const [loading, setLoading] = useState(false)
  const [wsStatus, setWsStatus] = useState('idle')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const wsRef = useRef(null)
  const mountedRef = useRef(true)

  useEffect(() => () => { mountedRef.current = false }, [])

  useEffect(() => {
    try { localStorage.setItem('farm_builder_config', JSON.stringify(config)) } catch { /* */ }
  }, [config])

  // On mount: look for active farm-builder session for this user
  useEffect(() => {
    (async () => {
      try {
        const resp = await fetch('/api/sessions', {
          headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
        })
        const list = await resp.json()
        const active = (list || []).find((s) =>
          s.session_type === 'farm-builder' && s.status === 'running'
        )
        if (active) {
          setSessionId(active.id)
          setStep('live')
        }
      } catch { /* */ }
    })()
  }, [])

  const tribeName = tribeId === 2 ? 'Teutons' : tribeId === 1 ? 'Romans' : tribeId === 3 ? 'Gauls' : 'Unknown'
  const isTeuton = tribeId === 2

  if (!villages || villages.length === 0) {
    return (
      <div className="p-6 max-w-[900px] mx-auto">
        <div className="card">
          <h2 className="heading-gold text-xl mb-2">Farm Builder</h2>
          <p className="text-secondary">No villages connected. Go to /connect first.</p>
          <button className="btn-primary mt-3" onClick={() => navigate('/connect')}>Connect</button>
        </div>
      </div>
    )
  }

  if (step === 'live' && sessionId) {
    return <LiveRunView sessionId={sessionId} onClose={() => { setSessionId(null); setStep('config') }} />
  }

  const invalid = !config.home_village_ids?.length
  const canRun = isTeuton && !invalid && !loading

  const buildConfigForBackend = () => {
    const homes = villages
      .filter((v) => config.home_village_ids.includes(v.id))
      .map((v, i) => ({
        id: v.id, x: v.x, y: v.y, name: v.name,
        short: (config.home_shorts || {})[v.id] || `Village${String(i + 1).padStart(2, '0')}`,
      }))
    const home_priority = (config.home_priority || []).filter((vid) => config.home_village_ids.includes(vid))
    for (const vid of config.home_village_ids) {
      if (!home_priority.includes(vid)) home_priority.push(vid)
    }
    return {
      radius: config.radius,
      max_player_total_pop: config.max_player_total_pop,
      home_villages: homes,
      home_priority,
      exclude_alliance_tags: config.exclude_alliance_tags,
      exclude_player_names: config.exclude_player_names,
      per_home_lists: config.per_home_lists || {},
    }
  }

  const doPreview = () => {
    if (!canRun) return
    setLoading(true)
    setWsStatus('connecting')
    setPreview(null)

    const handle = createWebSocket(
      '/ws/farm-builder/run',
      (data) => {
        if (!mountedRef.current) return
        switch (data.type) {
          case 'session_init':
            setSessionId(data.session_id)
            break
          case 'preview_result':
            setPreview(data.data)
            setStep('preview')
            setLoading(false)
            setWsStatus('done')
            // Save to scan cache
            fetch('/api/farm-builder/scan-cache', {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                Authorization: `Bearer ${localStorage.getItem('token')}`,
              },
              body: JSON.stringify({ scan: { preview: data.data, config } }),
            }).catch(() => {})
            break
          case 'status':
            setWsStatus(data.data?.state || 'running')
            if (data.data?.state === 'stopped' || data.data?.state === 'completed') {
              setLoading(false)
            }
            break
          case 'log':
            // Live logs during scan surface via logStore automatically via WS; skip here
            break
          case 'error':
            toast.error(data.message || 'Error')
            setLoading(false)
            setWsStatus('error')
            break
          default:
            break
        }
      },
      () => { toast.error('WS error'); setLoading(false) },
      () => { if (mountedRef.current && wsStatus === 'connecting') setWsStatus('disconnected') },
      { reconnect: false }
    )

    if (!handle) {
      toast.error('No auth token')
      setLoading(false)
      return
    }
    wsRef.current = handle
    const ws = handle.ws || handle
    ws.addEventListener('open', () => {
      try {
        ws.send(JSON.stringify({
          action: 'preview',
          config: buildConfigForBackend(),
        }))
      } catch {
        toast.error('Failed to send preview request')
        setLoading(false)
      }
    })
  }

  const confirmRun = () => setConfirmOpen(true)

  const doRun = () => {
    setConfirmOpen(false)
    if (!canRun || !preview) return
    setLoading(true)
    setWsStatus('connecting')

    const handle = createWebSocket(
      '/ws/farm-builder/run',
      (data) => {
        if (!mountedRef.current) return
        switch (data.type) {
          case 'session_init':
            setSessionId(data.session_id)
            setStep('live')
            break
          default:
            break
        }
      },
      () => toast.error('WS error'),
      () => { /* */ },
      { reconnect: false }
    )

    if (!handle) {
      toast.error('No auth token')
      setLoading(false)
      return
    }
    wsRef.current = handle
    const ws = handle.ws || handle
    ws.addEventListener('open', () => {
      try {
        ws.send(JSON.stringify({
          action: 'run',
          config: buildConfigForBackend(),
          survivors: preview.records,
        }))
      } catch {
        toast.error('Failed to send run request')
        setLoading(false)
      }
    })
  }

  // ── Render ──────────────────────────────────────────────────────────

  return (
    <div className="p-6 max-w-[1100px] mx-auto">
      <div className="flex items-center justify-between mb-5">
        <h2 className="heading-gold text-2xl">Farm Builder</h2>
        <div className="flex items-center gap-2">
          <span className={`loop-chip ${isTeuton ? 'loop-chip-active' : ''}`}
                title={isTeuton ? 'DEF→troop mapping enabled for Teutons' : 'DEF→troop tables for other tribes coming soon'}>
            {tribeName} only
          </span>
          {step !== 'config' && (
            <button className="btn-secondary btn-sm" onClick={() => setStep('config')}>
              ← Reconfigure
            </button>
          )}
        </div>
      </div>

      {!isTeuton && (
        <div className="error-box mb-4">
          Your account is {tribeName}. Only Teutons have a DEF→troop composition table configured.
          Runs are disabled until tables for other tribes are added.
        </div>
      )}

      {step === 'config' && (
        <>
          <ScanConfig
            villages={villages}
            value={config}
            onChange={setConfig}
            disabled={loading}
          />

          <AdvancedSpec
            villages={villages}
            value={config}
            onChange={setConfig}
            disabled={loading}
            radius={config.radius}
          />

          <div className="card mb-4">
            <h3 className="heading-gold text-lg mb-3">3. Defense → troop composition</h3>
            <p className="text-sm text-secondary mb-3">
              Hardcoded Teuton mapping from DEF (defender_combat_strength) to troops.
              Villages outside [10, 219] are skipped.
            </p>
            <table className="data-table text-sm w-full">
              <thead><tr><th>DEF range</th><th>Troops</th></tr></thead>
              <tbody>
                <tr><td>10 – 38</td><td>2 Clubs (t1)</td></tr>
                <tr><td>39 – 41</td><td>3 Clubs</td></tr>
                <tr><td>42 – 43</td><td>4 Clubs</td></tr>
                <tr><td>44 – 46</td><td>5 Clubs</td></tr>
                <tr><td>47 – 48</td><td>6 Clubs</td></tr>
                <tr><td>49 – 149</td><td>1 TK (t6)</td></tr>
                <tr><td>150 – 189</td><td>1 Club + 1 TK</td></tr>
                <tr><td>190 – 194</td><td>3 Clubs + 3 TK</td></tr>
                <tr><td>195 – 196</td><td>3 Clubs + 4 TK</td></tr>
                <tr><td>197 – 207</td><td>4 Clubs + 4 TK</td></tr>
                <tr><td>208 – 210</td><td>4 Clubs + 5 TK</td></tr>
                <tr><td>211 – 219</td><td>5 Clubs + 5 TK</td></tr>
              </tbody>
            </table>
          </div>

          <div className="card mb-4">
            <h3 className="heading-gold text-lg mb-3">4. Run</h3>
            <button
              className="btn-primary"
              onClick={doPreview}
              disabled={!canRun}
              title={!canRun ? (!isTeuton ? 'Teuton only' : invalid ? 'Select home villages' : 'Loading...') : ''}
            >
              {loading ? 'Scanning...' : 'Scan & preview'}
            </button>
          </div>
        </>
      )}

      {step === 'preview' && preview && (
        <>
          <ScanPreviewTable data={preview} />

          <div className="card mb-4">
            <div className="flex gap-3">
              <button className="btn-primary" onClick={confirmRun} disabled={!canRun || preview.survivors === 0}>
                Proceed to defense-scan
              </button>
              <button className="btn-secondary" onClick={() => setStep('config')}>
                Reconfigure
              </button>
            </div>
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirmOpen}
        title="Create farm lists and start defense-scan?"
        message={`This will create ${Object.keys(preview?.bucket_counts || {}).length} farm list(s) and scout ${preview?.survivors || 0} targets. This cannot be undone automatically.`}
        confirmText="Start"
        cancelText="Cancel"
        variant="default"
        onConfirm={doRun}
        onCancel={() => setConfirmOpen(false)}
      >
        {preview && (
          <DefenseScanPlan
            survivorCount={preview.survivors}
            onConfirm={doRun}
            onCancel={() => setConfirmOpen(false)}
            disabled={loading}
          />
        )}
      </ConfirmDialog>

      {/* Inline plan panel when confirm dialog not sufficient (show before confirm) */}
      {step === 'preview' && preview && !confirmOpen && (
        <DefenseScanPlan
          survivorCount={preview.survivors}
          onConfirm={confirmRun}
          onCancel={() => setStep('config')}
          disabled={loading}
        />
      )}
    </div>
  )
}
