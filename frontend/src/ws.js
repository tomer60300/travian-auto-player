import useLogStore from './stores/logStore'

function wsSource(path) {
  if (path.includes('/farm')) return 'farm'
  if (path.includes('/scout')) return 'scout'
  if (path.includes('/queue')) return 'queue'
  return 'ws'
}

export function createWebSocket(path, onMessage, onError, onClose) {
  const token = localStorage.getItem('token')
  const log = useLogStore.getState().addLog
  const source = wsSource(path)

  if (!token) {
    log('error', source, `WS ${path} — no auth token`)
    onError?.(new Event('error'))
    onClose?.(new CloseEvent('close', { code: 4001, reason: 'No auth token' }))
    return null
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  const url = `${protocol}//${host}${path}${path.includes('?') ? '&' : '?'}token=${token}`

  log('info', source, `WS connecting: ${path}`)
  const ws = new WebSocket(url)

  ws.onopen = () => {
    log('success', source, `WS connected: ${path}`)
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      log('info', source, `WS msg: ${data.type || 'data'}`, typeof data.message === 'string' ? data.message : undefined)
      onMessage(data)
    } catch {
      onMessage(event.data)
    }
  }

  ws.onerror = (event) => {
    log('error', source, `WS error: ${path}`)
    onError?.(event)
  }

  ws.onclose = (event) => {
    log('warning', source, `WS closed: ${path} (code ${event.code})`, event.reason || undefined)
    onClose?.(event)
  }

  return ws
}
