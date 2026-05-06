export function mapUrl(serverUrl, x, y) {
  if (!serverUrl || x == null || y == null) return null
  const base = String(serverUrl).replace(/\/+$/, '')
  return `${base}/karte.php?x=${encodeURIComponent(x)}&y=${encodeURIComponent(y)}`
}
