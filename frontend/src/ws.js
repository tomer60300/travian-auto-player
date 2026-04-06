export function createWebSocket(path, onMessage, onError, onClose) {
  const token = localStorage.getItem('token');
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  const url = `${protocol}//${host}${path}${path.includes('?') ? '&' : '?'}token=${token}`;

  const ws = new WebSocket(url);

  ws.onopen = () => {
    console.log(`WebSocket connected: ${path}`);
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      onMessage(data);
    } catch {
      onMessage(event.data);
    }
  };

  ws.onerror = (event) => {
    console.error(`WebSocket error: ${path}`, event);
    onError?.(event);
  };

  ws.onclose = (event) => {
    console.log(`WebSocket closed: ${path}`, event.code, event.reason);
    onClose?.(event);
  };

  return ws;
}
