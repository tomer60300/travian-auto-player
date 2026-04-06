export function createWebSocket(path, onMessage, onError, onClose) {
  const token = localStorage.getItem('token')
  if (!token) {
    onError?.(new Event('error'))
    onClose?.(new CloseEvent('close', { code: 4001, reason: 'No auth token' }))
    return null
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  const url = `${protocol}//${host}${path}${path.includes('?') ? '&' : '?'}token=${token}`

  const ws = new WebSocket(url)

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      onMessage(data)
    } catch {
      onMessage(event.data)
    }
  }

  ws.onerror = (event) => {
    onError?.(event)
  }

  ws.onclose = (event) => {
    onClose?.(event)
  }

  return ws
}
