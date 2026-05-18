import { useCallback, useEffect, useRef, useState } from 'react'
import { createWebSocket } from '../ws'

// ─── Diagnostic instrumentation ──────────────────────────────────────────
// Always-on telemetry. Every notable transition (WS open/close, frame,
// watchdog arm/fire, status change, visibility event, subscribeToExisting
// call, terminal) is both `console.log`'d with a `[ws:resumable]` prefix
// AND appended to a fixed-size ring buffer the user can inspect.
//
// To inspect after a hang:
//   1. F12 → Console tab.
//   2. Type `__resumableDebug.history()` — last 300 events with timestamps.
//   3. Type `__resumableDebug.snapshot()` — current state of every active
//      hook instance (status, sessionId, lastInbound age, WS readyState,
//      watchdog timer active, last-seen ts).
//
// The ring buffer survives across React StrictMode double-invokes etc.
// Module-level so all hook instances share it.
const _DBG_RING_SIZE = 300
const _dbgRing = []
const _dbgInstances = []

function _dbg(opType, event, data) {
  const t = Date.now()
  const entry = { t, opType, event, ...(data || {}) }
  const hms = new Date(t).toISOString().slice(11, 23) // HH:MM:SS.mmm
  // eslint-disable-next-line no-console
  console.log(`[ws:resumable ${hms}] ${opType}:${event}`, data ?? '')
  _dbgRing.push(entry)
  if (_dbgRing.length > _DBG_RING_SIZE) _dbgRing.shift()
}

if (typeof window !== 'undefined' && !window.__resumableDebug) {
  window.__resumableDebug = {
    /** Return a copy of the recent event ring (oldest first). */
    history: () => _dbgRing.slice(),
    /** Return a fresh snapshot of every active hook instance. */
    snapshot: () => _dbgInstances.map((i) => i.snap()),
    /** Force a re-subscribe across all instances (debug only). */
    forceReconnect: () => _dbgInstances.forEach((i) => i.forceReconnect?.()),
    /** Clear the ring buffer. */
    clear: () => { _dbgRing.length = 0 },
    /** Toggle console.log noise (ring buffer still captures). */
    silent: false,
  }
}

/**
 * Manages a long-running server-side operation whose lifetime is independent
 * of any single WebSocket connection. Survives page hides (iOS Safari
 * background), reloads, and bfcache restores by stashing the operation's
 * `session_id` in `localStorage` and reconnecting to
 * `/ws/sessions/<id>/stream` on every visibility/pageshow event.
 *
 * Storage key: `resumableOp:<opType>` → `session_id`. Cleared when the op
 * emits a terminal `operation_complete` message.
 *
 * Server protocol contract (mirrors operation_manager.py):
 *   - The op's `ExecutionSession` receives every message; the stream WS
 *     wraps each payload as `{type: "message", data: <original>}` with an
 *     initial `{type: "history", messages: [...]}` and final
 *     `{type: "session_ended"}`.
 *   - The starter WS (e.g. /ws/oasis-raider) emits `session_init` and the
 *     operation's own status messages directly.
 *   - Either WS accepts `{action: "stop"}`.
 *
 * @param {string} opType - identifier for this operation type (e.g.
 *   "oasis-raider", "farm-run-all"). Used as the localStorage key and to
 *   namespace listeners — distinct opTypes can be used in parallel by the
 *   same page.
 * @param {object} handlers
 * @param {(data: object) => void} handlers.onMessage - receives each op
 *   message (history replay items, live messages, terminals).
 * @param {(status: 'idle'|'connecting'|'running'|'reconnecting'|'completed'|'stopped'|'failed') => void} [handlers.onStatusChange]
 * @returns {{
 *   start: (path: string, config: object) => void,
 *   stop: () => void,
 *   sessionId: string | null,
 *   status: string,
 * }}
 */
export function useResumableOperation(opType, { onMessage, onStatusChange } = {}) {
  const storageKey = `resumableOp:${opType}`
  const [sessionId, setSessionId] = useState(() => {
    try { return localStorage.getItem(storageKey) } catch { return null }
  })
  const [status, setStatus] = useState(sessionId ? 'reconnecting' : 'idle')

  // Refs for handlers so we don't re-create WS on every parent re-render.
  const onMessageRef = useRef(onMessage)
  const onStatusChangeRef = useRef(onStatusChange)
  useEffect(() => { onMessageRef.current = onMessage }, [onMessage])
  useEffect(() => { onStatusChangeRef.current = onStatusChange }, [onStatusChange])

  const wsHandleRef = useRef(null)
  const sessionIdRef = useRef(sessionId)
  useEffect(() => { sessionIdRef.current = sessionId }, [sessionId])

  // Mount guard so the starter close handler doesn't re-open a session
  // stream after the component has unmounted.
  const mountedRef = useRef(true)

  // Stop requested while the current socket is not OPEN (e.g. user tapped
  // Stop during reconnect after Safari background return). Drained on
  // starter session_init and on session-stream session_meta when its
  // status === 'running'.
  const pendingStopRef = useRef(false)

  // Highest server-side timestamp delivered to the page so far. The
  // session-stream endpoint always replays the full ring buffer on
  // (re)connect; without this guard, every visibilitychange/pageshow
  // reconnect would re-append every prior log/result and re-fire
  // completion toasts, scan-complete handlers, etc. Each ExecutionSession
  // message carries a server-generated `ts` (added in push()), so we
  // skip anything we've already seen.
  const lastSeenTsRef = useRef(0)

  // Mirror status into a ref so closures (subscribeToExisting, in particular
  // the session_ended branch) can read the *current* value rather than a
  // stale capture from when the subscription was opened. Without this, a
  // history replay that pushes operation_complete with status=failed/stopped
  // gets clobbered to "completed" by the subsequent session_ended frame.
  const statusRef = useRef(status)
  useEffect(() => { statusRef.current = status }, [status])

  // Application-layer watchdog. The server pushes a heartbeat every 10s
  // (scout_ws.py:731 et al), and per-phase pushes interleave at sub-second
  // intervals during active work, so any 25s gap means the WS has gone
  // silent. The browser keeps WebSockets in OPEN state through TCP
  // half-open zombies (network drops without FIN/RST — NAT/firewall idle
  // timeout, VPN flap, laptop sleep/wake), so we can't rely on `onclose`
  // alone. When the watchdog fires we re-open the session-stream; that
  // socket is reconnect=true,maxRetries=30 and replays history on connect
  // so any messages we missed (incl. operation_complete) get delivered.
  const watchdogRef = useRef(null)
  const WATCHDOG_MS = 25_000
  // Belt-and-braces periodic poll. The primary detection is the
  // setTimeout-based watchdog above (armed on every inbound frame), but
  // Chrome can silently delay or coalesce setTimeout under memory
  // pressure / DevTools open / GPU stalls — even on a foreground tab.
  // setInterval has a different scheduling path; running it as a third
  // defense (in addition to the setTimeout watchdog + visibilitychange
  // handler) means recovery still fires under conditions where any one
  // of them stalls. 5 s tick × 25 s threshold = at most 5 s extra
  // detection latency in the worst case.
  const watchdogIntervalRef = useRef(null)
  const WATCHDOG_POLL_MS = 5_000

  // Monotonic timestamp (performance.now()) of the last inbound WS frame.
  // Backup signal for detecting a half-open zombie connection: in Chrome
  // under memory pressure or with DevTools open, `setTimeout` can be
  // silently delayed or coalesced even on a foreground tab, so the
  // watchdog's setTimeout-based check is not 100% reliable. The
  // visibilitychange/pageshow handlers use this timestamp as a second
  // defense — if `readyState === OPEN` but no frame has arrived for
  // >WATCHDOG_MS, treat the socket as dead and reconnect.
  //
  // performance.now() not Date.now(): wall-clock can step backwards
  // (NTP correction, laptop sleep/wake, manual time change) and a
  // negative elapsed value would falsely pass the `elapsed < WATCHDOG_MS`
  // check, leaving the page stranded on a zombie socket exactly when
  // the user was most likely to need recovery.
  //
  // Initialized to 0 deliberately so the first armWatchdog call records
  // a real timestamp; refreshed at every WS open/subscribe so the
  // recovery guard has a sane starting point.
  const lastInboundAtRef = useRef(0)

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current !== null) {
      clearTimeout(watchdogRef.current)
      watchdogRef.current = null
    }
    if (watchdogIntervalRef.current !== null) {
      clearInterval(watchdogIntervalRef.current)
      watchdogIntervalRef.current = null
    }
  }, [])

  // Third-defense periodic poll. Runs continuously while a session is
  // alive. Detects the same "no inbound frame for >WATCHDOG_MS while WS
  // claims OPEN" condition as the visibility handlers, but doesn't require
  // a tab transition to trigger. Started on the first armWatchdog (= first
  // inbound frame, when we actually have a real-clock baseline) and torn
  // down on terminal / unmount via clearWatchdog().
  const startWatchdogInterval = useCallback(() => {
    if (watchdogIntervalRef.current !== null) return
    if (!mountedRef.current) return
    watchdogIntervalRef.current = setInterval(() => {
      if (!mountedRef.current) return
      const live = statusRef.current
      if (live !== 'running' && live !== 'reconnecting' && live !== 'connecting') return
      if (!sessionIdRef.current) return
      if (lastInboundAtRef.current === 0) return
      const elapsed = performance.now() - lastInboundAtRef.current
      if (elapsed <= WATCHDOG_MS) return
      const handle = wsHandleRef.current
      const ws = handle?.ws ?? handle
      // If readyState is already not OPEN, the setTimeout watchdog or
      // onClose handler will (or has) handled it. We specifically
      // target the zombie case: claims OPEN, no frames for too long.
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      _dbg(opType, 'watchdog.intervalFire', {
        lastInboundAgeMs: elapsed,
        readyState: ws.readyState,
      })
      subscribeToExistingRef.current?.()
    }, WATCHDOG_POLL_MS)
  }, [opType])

  const setStatusBoth = useCallback((next) => {
    const prev = statusRef.current
    statusRef.current = next
    _dbg(opType, 'status', { from: prev, to: next })
    if (!mountedRef.current) return
    setStatus(next)
    onStatusChangeRef.current?.(next)
  }, [opType])

  const persistSessionId = useCallback((id) => {
    // Persist regardless of mount state — terminal handlers must be able
    // to clear the stored session_id even if the component unmounted
    // before the queued terminal frame was processed; otherwise the next
    // mount would resume a session that is already gone.
    try {
      if (id) localStorage.setItem(storageKey, id)
      else localStorage.removeItem(storageKey)
    } catch { /* localStorage may be disabled */ }
    sessionIdRef.current = id
    if (!mountedRef.current) return
    setSessionId(id)
  }, [storageKey])

  const closeWs = useCallback(() => {
    const handle = wsHandleRef.current
    wsHandleRef.current = null
    if (handle?.close) {
      try { handle.close() } catch { /* empty */ }
    }
  }, [])

  const drainPendingStop = useCallback(() => {
    if (!pendingStopRef.current) return
    const handle = wsHandleRef.current
    const ws = handle?.ws ?? handle
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ action: 'stop' })) } catch { /* empty */ }
      pendingStopRef.current = false
    }
  }, [])

  // Forward-declared (mutable ref) so armWatchdog can call into
  // subscribeToExisting without a circular useCallback dependency.
  const subscribeToExistingRef = useRef(null)

  const armWatchdog = useCallback(() => {
    // Note: clearWatchdog clears BOTH setTimeout AND setInterval. We
    // re-create the setTimeout below; the interval gets re-started via
    // startWatchdogInterval (which no-ops if already running, but
    // clearWatchdog cleared it). Net effect: timeout resets on every
    // frame; interval starts on first arm and stays running until
    // terminal.
    clearWatchdog()
    if (!mountedRef.current) return
    // Record wall-clock time of this arm. The visibilitychange/pageshow
    // recovery handlers compare against this to bypass a falsely-OPEN
    // zombie socket even when setTimeout fires reliably (cf. Chrome
    // memory-pressure throttling).
    lastInboundAtRef.current = performance.now()
    startWatchdogInterval()
    watchdogRef.current = setTimeout(() => {
      watchdogRef.current = null
      _dbg(opType, 'watchdog.fire', {
        status: statusRef.current,
        hasSessionId: !!sessionIdRef.current,
        lastInboundAgeMs: performance.now() - lastInboundAtRef.current,
      })
      const live = statusRef.current
      // Only act mid-operation. Terminal states are absorbing; idle never
      // had a socket to begin with. 'connecting' is included because the
      // starter WS can connect but get stuck before session_init arrives
      // (server hang, mid-handshake zombie), and without this we'd leave
      // the page stuck on "Connecting..." forever.
      if (
        live !== 'running'
        && live !== 'reconnecting'
        && live !== 'connecting'
      ) return
      // Without a session_id there's nothing to resume to. Mark the
      // socket dead so the page exits its spinner instead of pretending
      // the op is still alive.
      if (!sessionIdRef.current) {
        pendingStopRef.current = false
        setStatusBoth('failed')
        closeWs()
        return
      }
      // Force a clean reattach via the session-stream. Its own onMessage
      // handler will re-arm the watchdog as soon as data flows again.
      subscribeToExistingRef.current?.()
    }, WATCHDOG_MS)
  }, [clearWatchdog, closeWs, setStatusBoth, startWatchdogInterval])

  /**
   * Open the live stream for an existing session_id (resume flow).
   * Returns silently if there's no stored session_id.
   */
  const subscribeToExisting = useCallback(() => {
    const id = sessionIdRef.current
    if (!id) {
      _dbg(opType, 'subscribeToExisting.skip', { reason: 'no_session_id' })
      return
    }
    _dbg(opType, 'subscribeToExisting', { sessionId: id, prevStatus: statusRef.current })
    closeWs()
    setStatusBoth('reconnecting')
    // INTENTIONALLY do NOT armWatchdog() here.
    //
    // History: an earlier "safety net" did `armWatchdog()` right here on
    // the assumption it would catch a dead replacement WS. In practice
    // it created a recursive loop: when the watchdog's own callback
    // calls subscribeToExisting() (because no inbound frame in 25s),
    // arming here re-starts the timer for the new attempt — but if the
    // WS handshake + first frame takes >25s (large history payload on a
    // slow network), the timer fires while the in-flight WS is still
    // CONNECTING. The watchdog kills it, opens another, and loops.
    //
    // Arm-on-inbound-frame is sufficient: a TCP half-open zombie WS
    // (the only failure mode the watchdog was created to detect) never
    // delivers a frame, so the stale armWatchdog from BEFORE this call
    // is still ticking and will fire correctly. A live-but-slow WS gets
    // unlimited time to deliver its first frame, then re-arms the timer
    // normally.
    // Captured by the message handler so terminal closeWs() only fires
    // when wsHandleRef still points at THIS handle. Otherwise a sync
    // reattach triggered earlier in the same handler (via setStatusBoth
    // → onStatusChange or onMessageRef) could replace wsHandleRef and
    // we'd close the brand-new socket instead of the terminal one.
    let myHandle = null
    let inConstruction = true
    const closeIfStillCurrent = () => {
      // During synchronous construction (e.g. no-auth path) myHandle is
      // still null and wsHandleRef may point at a stale handle. Skip;
      // the post-construction failure handler below sets terminal state.
      if (inConstruction) return
      if (wsHandleRef.current === myHandle) closeWs()
    }
    let handle
    try {
      handle = createWebSocket(
      `/ws/sessions/${id}/stream`,
      (data) => {
        // Any frame from the session-stream is a sign-of-life; reset the
        // watchdog. We arm again here even when the inbound is the
        // wrapper meta — receiving it proves the WS is alive.
        _dbg(opType, 'stream.frame', {
          type: data?.type,
          msgType: data?.data?.type,
          historyLen: Array.isArray(data?.messages) ? data.messages.length : undefined,
        })
        armWatchdog()
        // Wrapper format: {type:'session_meta'|'history'|'message'|'session_ended', ...}
        if (data.type === 'session_meta') {
          if (data.status === 'running') {
            // Live → flip to running and flush any stop queued during reconnect.
            setStatusBoth('running')
            drainPendingStop()
          } else {
            // Server-side session is already terminal — never send a stop
            // for a finished op.
            pendingStopRef.current = false
          }
          return
        }
        if (data.type === 'history') {
          for (const msg of data.messages || []) handleOpMessage(msg)
          return
        }
        if (data.type === 'message') {
          handleOpMessage(data.data)
          return
        }
        if (data.type === 'session_ended') {
          // Op already finished — clear the stored id; the terminal message
          // (if any) was just replayed via history above. Read the LATEST
          // status from the ref, not the closure capture, so a previous
          // operation_complete with status=failed/stopped isn't clobbered.
          persistSessionId(null)
          pendingStopRef.current = false
          clearWatchdog()
          const live = statusRef.current
          if (live === 'reconnecting' || live === 'running' || live === 'connecting') {
            setStatusBoth('completed')
          }
          // Server will close right after this; without an explicit close
          // here, createWebSocket's auto-reconnect would re-open the same
          // session-stream path forever (the path is captured in the
          // closure even though sessionIdRef is now null), each reconnect
          // re-receiving session_ended → close → reconnect.
          closeIfStillCurrent()
          return
        }
        // Plain error envelope from the stream endpoint itself —
        // typically "Session not found or expired" or "Access denied"
        // when the session_id has aged out (24h TTL), the server has
        // restarted, or another user logged in. Forward the error AND
        // drop the persisted id so we stop trying to reconnect to a
        // session that will never come back.
        if (data.type === 'error') {
          if (mountedRef.current) onMessageRef.current?.(data)
          persistSessionId(null)
          pendingStopRef.current = false
          clearWatchdog()
          setStatusBoth('failed')
          // Server closes after the error envelope; stop auto-reconnect.
          closeIfStillCurrent()
        }
      },
      undefined,
      () => {
        // Final close (max retries reached or manual close). Do NOT delete
        // the stored session_id — the op may still be running server-side;
        // we want the next visibilitychange/pageshow to retry.
      },
      {
        reconnect: true,
        maxRetries: 30,
        onReconnecting: () => setStatusBoth('reconnecting'),
        // Intentionally a no-op: leave status as 'reconnecting' until the
        // server's session_meta confirms the session is still live. That
        // branch (running-gated) is the sole transition back to 'running'
        // and the sole drain point for pendingStopRef on the session-stream.
        // Flipping to 'running' here would let stop() pass its immediate-
        // send gate before the session has been confirmed alive, racing
        // with a session that completed during the disconnect window.
      },
    )
    } catch {
      // createWebSocket itself threw synchronously. Drop the queued stop
      // and reflect terminal so the page exits its reconnecting UI.
      pendingStopRef.current = false
      setStatusBoth('failed')
      return
    }
    if (!handle) {
      // No auth token / synchronous onClose with null return — same
      // terminal handling as the starter path.
      pendingStopRef.current = false
      setStatusBoth('failed')
      return
    }
    // Wire identity first, then enable async callbacks. Reverse order
    // would expose a window where closeIfStillCurrent compares against
    // a still-null myHandle. The finally guarantees inConstruction is
    // released even if a ref assignment unexpectedly throws — without
    // it, the guard would block all future terminal cleanup.
    try {
      myHandle = handle
      wsHandleRef.current = handle
    } finally {
      inConstruction = false
    }

    function handleOpMessage(msg) {
      if (!msg) return
      // Drop already-seen messages on reconnect. ts is the server-side
      // timestamp added by ExecutionSessionManager.push.
      const ts = typeof msg.ts === 'number' ? msg.ts : null
      if (ts !== null) {
        if (ts <= lastSeenTsRef.current) return
        lastSeenTsRef.current = ts
      }
      if (mountedRef.current) onMessageRef.current?.(msg)
      // Terminal markers from operation_manager.py
      if (msg.type === 'operation_complete') {
        const s = msg.status === 'failed' ? 'failed'
          : msg.status === 'stopped' ? 'stopped'
          : 'completed'
        setStatusBoth(s)
        persistSessionId(null)
        pendingStopRef.current = false
        clearWatchdog()
        // Mirror session_ended: stop the auto-reconnect so the captured
        // path doesn't re-open after server closes.
        closeIfStillCurrent()
      }
    }
  }, [armWatchdog, clearWatchdog, closeWs, drainPendingStop, persistSessionId, setStatusBoth])

  // Wire subscribeToExisting into the watchdog ref now that the binding
  // exists; the ref-of-fn pattern avoids a circular useCallback dep.
  useEffect(() => {
    subscribeToExistingRef.current = subscribeToExisting
  }, [subscribeToExisting])

  /**
   * Start a brand new op. `path` is the per-op WS endpoint (e.g.
   * `/ws/oasis-raider`); `config` is the JSON config sent inside
   * `{action:'start', config}`.
   */
  const start = useCallback((path, config) => {
    _dbg(opType, 'start', { path, configKeys: Object.keys(config || {}) })
    closeWs()
    persistSessionId(null)
    // Fresh op — reset the de-dup cursor so legitimate first-time
    // messages aren't suppressed, and the pending-stop queue so a stop
    // queued for a prior op doesn't fire on this one's session_init.
    lastSeenTsRef.current = 0
    pendingStopRef.current = false
    setStatusBoth('connecting')
    // Arm the watchdog now. session_init should arrive within seconds;
    // if 25s pass without ANY frame, the WS is a TCP half-open zombie
    // and we should fail over to the session-stream (or, if no
    // sessionId persisted, fail the op so the page exits its spinner).
    armWatchdog()
    // Captured by the starter onClose so it can detect intentional
    // handoffs (e.g. already_running → subscribeToExisting replaced
    // wsHandleRef) and skip reattaching, since reattaching would
    // double-open the session-stream against the live one.
    let starterHandle = null
    let inConstruction = true
    let handle
    try {
      handle = createWebSocket(
      path,
      (data) => {
        // Any frame from the starter WS proves the wire is alive. Reset
        // the watchdog — heartbeats keep it armed during long silent
        // phases (e.g., scout enrichment).
        _dbg(opType, 'starter.frame', { type: data?.type, hasSid: !!data?.session_id })
        armWatchdog()
        // The starter WS emits session_init early — capture and persist.
        if (data.type === 'session_init' && data.session_id) {
          persistSessionId(data.session_id)
          setStatusBoth('running')
          // Stop queued before session_init arrived → flush via the open
          // starter WS.
          drainPendingStop()
        }
        if (data.type === 'already_running' && data.session_id) {
          // Reattach to the existing op instead of starting a duplicate.
          persistSessionId(data.session_id)
          subscribeToExisting()
          return
        }
        // Track the highest server-side ts here too, so a later
        // session-stream reconnect (Safari resume) doesn't replay
        // starter-WS-delivered messages back to the page handlers.
        const ts = typeof data.ts === 'number' ? data.ts : null
        if (ts !== null) {
          if (ts <= lastSeenTsRef.current) return
          lastSeenTsRef.current = ts
        }
        if (mountedRef.current) onMessageRef.current?.(data)
        if (data.type === 'operation_complete') {
          const s = data.status === 'failed' ? 'failed'
            : data.status === 'stopped' ? 'stopped'
            : 'completed'
          setStatusBoth(s)
          persistSessionId(null)
          pendingStopRef.current = false
          clearWatchdog()
        }
      },
      undefined,
      () => {
        _dbg(opType, 'starter.close', {
          mounted: mountedRef.current,
          inConstruction,
          hasSid: !!sessionIdRef.current,
          status: statusRef.current,
        })
        // Starter WS closed. If the component has unmounted, do nothing —
        // otherwise the cleanup-driven close would silently re-open a
        // session-stream after unmount.
        if (!mountedRef.current) return
        // Synchronous onClose fired during construction (e.g. no auth
        // token → ws.js calls onClose then returns null). The post-
        // construction failure path below handles terminal state; don't
        // reattach or fail mid-construction.
        if (inConstruction) return
        // If wsHandleRef has already been replaced by another connection
        // (e.g. already_running handoff → subscribeToExisting attached
        // the session-stream and overwrote wsHandleRef), this close is
        // for a stale starter handle. Don't reattach — the live stream
        // is already running.
        if (wsHandleRef.current !== starterHandle) return
        //   * If session_init never arrived → no op to resume.
        //     Transition to a terminal state so the page exits its
        //     connecting UI rather than spinning forever.
        //   * If we DID get a session_id, the op is alive server-side.
        //     Don't wait for a visibility/pageshow event to reattach —
        //     the page may stay visible across a transient network
        //     blip; the user expects live updates and the Stop button
        //     to keep working. Switch immediately to the session-stream
        //     tail.
        if (sessionIdRef.current) {
          subscribeToExisting()
          return
        }
        if (statusRef.current !== 'completed'
          && statusRef.current !== 'failed'
          && statusRef.current !== 'stopped') {
          pendingStopRef.current = false
          setStatusBoth('failed')
        }
      },
      {
        reconnect: false,
      },
    )
    } catch {
      // createWebSocket itself threw synchronously (e.g. WebSocket
      // constructor rejected an invalid URL). Reflect terminal state
      // and clear any queued stop so the next start() is clean.
      pendingStopRef.current = false
      setStatusBoth('failed')
      return
    }
    if (!handle) {
      // createWebSocket already invoked onClose synchronously with no
      // auth token; reflect that as a terminal state.
      pendingStopRef.current = false
      setStatusBoth('failed')
      return
    }
    // Wire identity first, then enable async callbacks (same pattern
    // as subscribeToExisting). finally guarantees inConstruction flips
    // off so the close handler can do its job even on a throw.
    try {
      starterHandle = handle
      wsHandleRef.current = handle
    } finally {
      inConstruction = false
    }
    // Older createWebSocket variants returned a raw WS; normalize.
    const ws = handle.ws ?? handle
    ws.addEventListener('open', () => {
      try { ws.send(JSON.stringify({ action: 'start', config })) } catch { /* empty */ }
    })
  }, [armWatchdog, clearWatchdog, closeWs, drainPendingStop, persistSessionId, setStatusBoth, subscribeToExisting])

  /**
   * Ask the running op to stop. Works whether we're connected via the
   * starter WS or the session-stream WS. If neither is OPEN (e.g. the
   * stream is reconnecting after Safari background return), the request
   * is queued and flushed once a socket opens — via session_init on the
   * starter, or session_meta (when status === 'running') on the
   * session-stream.
   */
  const stop = useCallback(() => {
    // Already terminal — nothing to stop. A late tap shouldn't transmit a
    // spurious stop to a finished session (which would surface as a stop
    // for whatever runs next under the same socket reuse).
    const live = statusRef.current
    if (live === 'completed' || live === 'failed' || live === 'stopped' || live === 'idle') {
      pendingStopRef.current = false
      return
    }
    const handle = wsHandleRef.current
    const ws = handle?.ws ?? handle
    // Only transmit immediately when we've confirmed the server-side
    // session is live (status === 'running'). A session-stream socket can
    // be OPEN before session_meta arrives, and that meta could classify
    // the session as terminal — in which case sending now would race
    // (drop a legit stop or stop something that already ended). Queueing
    // lets the session_meta running-status gate drain it correctly.
    if (ws && ws.readyState === WebSocket.OPEN && live === 'running') {
      try { ws.send(JSON.stringify({ action: 'stop' })) } catch { /* empty */ }
      pendingStopRef.current = false
      return
    }
    pendingStopRef.current = true
    // No live or in-flight socket but we know a server-side session is
    // running → actively reach it via the session-stream.
    if (sessionIdRef.current && (!ws || ws.readyState >= WebSocket.CLOSING)) {
      subscribeToExisting()
    }
  }, [subscribeToExisting])

  // On mount: register snapshot accessor on the global debug bus and (if
  // a stored session_id exists) resume that session.
  useEffect(() => {
    mountedRef.current = true
    const inst = {
      opType,
      forceReconnect: () => {
        _dbg(opType, 'forceReconnect.manual')
        subscribeToExisting()
      },
      snap: () => {
        const handle = wsHandleRef.current
        const ws = handle?.ws ?? handle
        return {
          opType,
          status: statusRef.current,
          sessionId: sessionIdRef.current,
          mounted: mountedRef.current,
          wsReadyState: ws?.readyState ?? null,
          wsReadyStateName: ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'][ws?.readyState] ?? 'NONE',
          // performance.now()-based, monotonic from page load
          lastInboundPerfMs: lastInboundAtRef.current,
          lastInboundAgeMs: lastInboundAtRef.current
            ? performance.now() - lastInboundAtRef.current
            : null,
          watchdogActive: watchdogRef.current !== null,
          watchdogIntervalActive: watchdogIntervalRef.current !== null,
          lastSeenTs: lastSeenTsRef.current,
          pendingStop: pendingStopRef.current,
        }
      },
    }
    _dbgInstances.push(inst)
    _dbg(opType, 'mount', { hasStoredSession: !!sessionIdRef.current })
    if (sessionIdRef.current) subscribeToExisting()
    return () => {
      mountedRef.current = false
      _dbg(opType, 'unmount')
      const idx = _dbgInstances.indexOf(inst)
      if (idx >= 0) _dbgInstances.splice(idx, 1)
      clearWatchdog()
      closeWs()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // visibilitychange + pageshow → reconnect when the tab returns. iOS Safari
  // is the headline case: it tears WS down on hide and may restore from
  // bfcache without firing visibilitychange first.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      if (!sessionIdRef.current) return
      const handle = wsHandleRef.current
      const ws = handle?.ws ?? handle
      // Two failure modes to detect:
      //   1. Socket isn't OPEN/CONNECTING → clearly broken → reconnect.
      //   2. Socket IS OPEN but no inbound frame for >WATCHDOG_MS — this
      //      is the TCP half-open zombie case. The OS-level socket lies
      //      about being OPEN because no FIN/RST has arrived. Without
      //      this second check, the visibility recovery accepts the lie
      //      and leaves the page stranded.
      const open = ws && ws.readyState === WebSocket.OPEN
      const connecting = ws && ws.readyState === WebSocket.CONNECTING
      const ageMs = lastInboundAtRef.current > 0
        ? performance.now() - lastInboundAtRef.current
        : null
      const staleSinceLastInbound = ageMs !== null && ageMs > WATCHDOG_MS
      _dbg(opType, 'onVisible', {
        readyState: ws?.readyState,
        open, connecting, staleSinceLastInbound,
        lastInboundAgeMs: ageMs,
      })
      if (!open && !connecting) {
        subscribeToExisting()
      } else if (open && staleSinceLastInbound) {
        subscribeToExisting()
      }
    }
    const onPageShow = (e) => {
      // bfcache restores fire pageshow with persisted=true and no
      // visibilitychange. Same logic as onVisible: reconnect if socket
      // is broken, OR if it claims OPEN but no inbound frame for
      // >WATCHDOG_MS (zombie). bfcache restoring a zombie socket is
      // exactly the case where this matters.
      if (!e.persisted || !sessionIdRef.current) return
      const handle = wsHandleRef.current
      const ws = handle?.ws ?? handle
      const open = ws && ws.readyState === WebSocket.OPEN
      const connecting = ws && ws.readyState === WebSocket.CONNECTING
      const ageMs = lastInboundAtRef.current > 0
        ? performance.now() - lastInboundAtRef.current
        : null
      const staleSinceLastInbound = ageMs !== null && ageMs > WATCHDOG_MS
      if (!open && !connecting) {
        subscribeToExisting()
      } else if (open && staleSinceLastInbound) {
        subscribeToExisting()
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('pageshow', onPageShow)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('pageshow', onPageShow)
    }
  }, [subscribeToExisting])

  return { start, stop, sessionId, status }
}
