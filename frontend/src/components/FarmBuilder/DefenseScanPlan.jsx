export default function DefenseScanPlan({ survivorCount, onConfirm, onCancel, disabled }) {
  // ETA estimate per .one-shot/insights.md: ~15s per existing-report check,
  // ~1 min per fresh scout send + wait for max travel (3-4h for r=30).
  // Practical heuristic: survivorCount * 15s = conservative floor for PRE phase.
  // Scout wave itself may add hours. We cannot predict without data — use
  // a broad estimate with 1.3x padding.
  const preSeconds = survivorCount * 15
  const scoutSeconds = survivorCount * 60  // assume up to 1 scout/min if all fresh
  const totalSeconds = Math.round((preSeconds + scoutSeconds) * 1.3)
  const hrs = Math.floor(totalSeconds / 3600)
  const mins = Math.round((totalSeconds % 3600) / 60)
  const etaText = hrs > 0 ? `~${hrs}h ${mins}m` : `~${mins}m`

  return (
    <div className="card mb-4 border-2 border-warning/50">
      <h3 className="heading-gold text-lg mb-4">Gate 2 — Defense-Scan Plan</h3>

      <div className="grid grid-cols-2 gap-4 mb-4">
        <div>
          <div className="text-secondary text-sm">Unique coords to scout</div>
          <div className="text-xl font-semibold">{survivorCount}</div>
        </div>
        <div>
          <div className="text-secondary text-sm">Estimated duration</div>
          <div className="text-xl font-semibold text-warning">{etaText}</div>
        </div>
      </div>

      <details className="mb-4">
        <summary className="cursor-pointer font-semibold text-warning">⚠️ Read before proceeding</summary>
        <div className="mt-2 p-3 bg-warning/10 rounded text-sm">
          <p className="mb-2">
            If the server restarts during this phase, the run is lost. ETA is ~{hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`}.
            Recommend running only when you don't plan a deploy.
          </p>
          <p className="text-secondary">
            Durability note: partial progress is held in a WebSocket session's in-memory ring buffer.
            Browser refresh/close is fine (see multi-device/reconnect). Server restart is not fine.
          </p>
        </div>
      </details>

      <div className="flex gap-3">
        <button className="btn-primary" onClick={onConfirm} disabled={disabled}>
          Create farm lists &amp; start defense-scan
        </button>
        <button className="btn-secondary" onClick={onCancel} disabled={disabled}>
          Cancel
        </button>
      </div>
    </div>
  )
}
