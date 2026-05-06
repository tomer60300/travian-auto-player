import useGameStore from '../stores/gameStore'
import { mapUrl } from '../utils/travianLinks'

export function MapCoord({ x, y, separator = ', ', className = '' }) {
  const serverUrl = useGameStore((s) => s.serverUrl)
  const xLabel = x ?? '?'
  const yLabel = y ?? '?'
  const text = `(${xLabel}${separator}${yLabel})`
  const href = mapUrl(serverUrl, x, y)
  if (!href) return <span className={className}>{text}</span>
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={`${className} hover:underline`.trim()}
      title={`Open (${xLabel}|${yLabel}) on Travian map`}
      onClick={(e) => e.stopPropagation()}
    >
      {text}
    </a>
  )
}
