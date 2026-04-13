import useLogStore from './stores/logStore'
import useCaptchaStore from './stores/captchaStore'

let ws = null
let reconnectTimer = null
let reconnectAttempts = 0
const MAX_RECONNECT = 10
const BASE_DELAY = 1000

export function connectLogStream() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
  reconnectAttempts = 0 // reset so reconnect works after a disconnect/reconnect cycle

  const token = localStorage.getItem('token')
  if (!token) return

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  const url = `${protocol}//${host}/ws/logs?token=${token}&level=info`

  ws = new WebSocket(url)

  ws.onopen = () => {
    reconnectAttempts = 0
    // Check captcha status on connect (covers page-refresh case)
    fetch('/api/captcha/status', {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data && data.active) {
          useCaptchaStore.getState().trigger(
            data.pattern,
            data.triggered_at ? data.triggered_at * 1000 : Date.now(),
            {
              url: data.url,
              statusCode: data.status_code,
              responseSnippet: data.response_snippet,
            },
          )
        }
      })
      .catch(() => {})
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      const addLog = useLogStore.getState().addLog

      // Captcha guard messages — handle before normal log routing
      if (data.type === 'captcha_alert' && data.active) {
        useCaptchaStore.getState().trigger(
          data.pattern,
          data.triggered_at ? data.triggered_at * 1000 : Date.now(),
          {
            url: data.url,
            statusCode: data.status_code,
            responseSnippet: data.response_snippet,
          },
        )
        return
      }
      if (data.type === 'captcha_resolved') {
        useCaptchaStore.getState().resolve()
        return
      }

      if (data.type === 'history' && Array.isArray(data.entries)) {
        data.entries.forEach(e => {
          addLog(e.level || 'info', e.source || 'server', e.message, e.detail, 'server')
        })
      } else if (data.type === 'log') {
        addLog(data.level || 'info', data.source || 'server', data.message, data.detail, 'server')
      }
    } catch { /* ignore parse errors */ }
  }

  ws.onclose = () => {
    ws = null
    scheduleReconnect()
  }

  ws.onerror = () => {
    // onclose will fire after onerror
  }
}

function scheduleReconnect() {
  if (reconnectAttempts >= MAX_RECONNECT) return
  const delay = Math.min(BASE_DELAY * Math.pow(2, reconnectAttempts), 30000)
  reconnectAttempts++
  reconnectTimer = setTimeout(connectLogStream, delay)
}

export function disconnectLogStream() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  reconnectAttempts = MAX_RECONNECT // prevent reconnect
  if (ws) { try { ws.close() } catch {} ws = null }
}

export function setLogLevel(level) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'filter', level }))
  }
}
