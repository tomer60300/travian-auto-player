import useLogStore from './stores/logStore'

function wsSource(path) {
  if (path.includes('/farm')) return 'farm'
  if (path.includes('/scout')) return 'scout'
  if (path.includes('/queue')) return 'queue'
  if (path.includes('/logs')) return 'logs'
  return 'ws'
}

function summarize(data, maxLen) {
  try {
    const str = typeof data === 'string' ? data : JSON.stringify(data)
    return str.length <= maxLen ? str : str.slice(0, maxLen) + '...'
  } catch { return '[unserializable]' }
}

/**
 * Create a WebSocket connection with optional auto-reconnect support.
 *
 * @param {string} path - WS endpoint path (e.g., '/ws/farm/run/1')
 * @param {function} onMessage - Called with parsed JSON data for each message
 * @param {function} [onError] - Called on WS error
 * @param {function} [onClose] - Called on WS close (only fires on final close when reconnect is enabled)
 * @param {object} [options] - { reconnect, maxRetries, onReconnecting, onReconnected }
 *   - reconnect: boolean (default false) — auto-reconnect on disconnect
 *   - maxRetries: number (default 10)
 *   - onReconnecting: () => void — called when a reconnect attempt starts
 *   - onReconnected: (ws) => void — called after a successful reconnect; use to re-send config
 * @returns {WebSocket | { ws: WebSocket, close: () => void } | null}
 *   - null when there is no auth token
 *   - raw WebSocket when reconnect is disabled (backward compatible)
 *   - { ws, close() } object when reconnect is enabled
 */
export function createWebSocket(path, onMessage, onError, onClose, options = {}) {
  const { reconnect = false, maxRetries = 10, onReconnecting, onReconnected } = options
  const token = localStorage.getItem('token')
  const log = useLogStore.getState().addLog
  const source = wsSource(path)

  if (!token) {
    log('error', source, `WS ${path} — no auth token`)
    onError?.(new Event('error'))
    onClose?.(new CloseEvent('close', { code: 4001, reason: 'No auth token' }))
    return null
  }

  let attempts = 0
  let stopped = false
  let reconnectTimer = null
  let currentWs = null
  let isFirstConnect = true

  function connect() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    // The bearer JWT rides in a WebSocket subprotocol, NOT the query string:
    // uvicorn logs request paths (query included) at INFO, so a `?token=<jwt>`
    // writes a reusable 24h token into server logs (and proxy logs, browser
    // history, Referer). Subprotocol values are part of the handshake headers,
    // which uvicorn does not log. The server reads the token from here.
    const url = `${protocol}//${host}${path}`

    const isReconnect = !isFirstConnect
    if (isReconnect) onReconnecting?.()
    log('info', source, `WS >> connect: ${path}${attempts > 0 ? ` (retry ${attempts})` : ''}`)
    const ws = new WebSocket(url, ['travian-jwt', token])
    currentWs = ws

    ws.onopen = () => {
      log('success', source, `WS << connected: ${path}`)
      attempts = 0
      if (isReconnect) onReconnected?.(ws)
      isFirstConnect = false
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        const type = data.type || 'data'
        const msg = data.message || ''
        const detail = summarize(data, 1000)
        log('info', source, `WS << ${type}${msg ? ': ' + msg : ''}`, detail)
        onMessage(data)
      } catch {
        log('info', source, `WS << raw: ${summarize(event.data, 200)}`)
        onMessage(event.data)
      }
    }

    ws.onerror = (event) => {
      log('error', source, `WS error: ${path}`)
      // Only forward error to caller when not auto-reconnecting
      if (!reconnect || stopped) onError?.(event)
    }

    ws.onclose = (event) => {
      log('warning', source, `WS closed: ${path} code=${event.code}`, event.reason || undefined)
      currentWs = null

      // Don't reconnect on: 4009 (already running), 4003 (no session),
      // 4004 (session not found / expired — 24h TTL, server restart,
      // or a different user's id), 1000 (normal close).
      const noRetry =
        event.code === 4009
        || event.code === 4003
        || event.code === 4004
        || event.code === 1000

      // Auto-reconnect if enabled and not manually stopped
      if (reconnect && !stopped && !noRetry && attempts < maxRetries) {
        const delay = Math.min(1000 * Math.pow(2, attempts), 30000)
        attempts++
        log('info', source, `WS reconnecting in ${delay / 1000}s (attempt ${attempts}/${maxRetries})`)
        reconnectTimer = setTimeout(connect, delay)
      } else {
        // Only fire onClose when we're truly done (manual stop or max retries)
        onClose?.(event)
        if (reconnect && !stopped && attempts >= maxRetries) {
          log('error', source, `WS max retries reached for ${path}`)
        }
      }
    }

    // Log outgoing messages
    const origSend = ws.send.bind(ws)
    ws.send = (data) => {
      log('info', source, `WS >> send`, summarize(data, 500))
      return origSend(data)
    }
  }

  connect()

  // When reconnect is disabled, return raw WS for backward compatibility
  if (!reconnect) {
    return currentWs
  }

  return {
    get ws() { return currentWs },
    close() {
      stopped = true
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
      if (currentWs) { try { currentWs.close() } catch { /* empty */ } }
    }
  }
}
