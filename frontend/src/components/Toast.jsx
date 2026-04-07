import { create } from 'zustand'

let toastId = 0
const timers = new Map()

export const useToastStore = create((set) => ({
  toasts: [],

  addToast: (type, message) => {
    const id = ++toastId
    set((state) => ({
      toasts: [...state.toasts, { id, type, message, timestamp: Date.now() }],
    }))
    const timer = setTimeout(() => {
      timers.delete(id)
      set((state) => ({
        toasts: state.toasts.filter((t) => t.id !== id),
      }))
    }, 4000)
    timers.set(id, timer)
  },

  removeToast: (id) => {
    const timer = timers.get(id)
    if (timer) { clearTimeout(timer); timers.delete(id) }
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    }))
  },
}))

export function useToast() {
  const addToast = useToastStore((s) => s.addToast)
  return {
    success: (msg) => addToast('success', msg),
    error: (msg) => addToast('error', msg),
    warning: (msg) => addToast('warning', msg),
    info: (msg) => addToast('info', msg),
  }
}

const typeIcons = {
  success: '✓',
  error: '✕',
  warning: '⚠',
  info: 'ℹ',
}

function ToastItem({ toast, onClose }) {
  const icon = typeIcons[toast.type] || typeIcons.info
  const validTypes = ['success', 'error', 'warning', 'info']
  const typeClass = validTypes.includes(toast.type) ? `toast-${toast.type}` : 'toast-info'

  return (
    <div className={`toast ${typeClass}`}>
      <span className="text-lg shrink-0">{icon}</span>
      <span className="flex-1">{toast.message}</span>
      <button
        onClick={() => onClose(toast.id)}
        className="toast-close"
        aria-label="Dismiss"
      >
        ✕
      </button>
    </div>
  )
}

export default function ToastContainer() {
  const toasts = useToastStore((s) => s.toasts)
  const removeToast = useToastStore((s) => s.removeToast)

  if (toasts.length === 0) return null

  return (
    <div className="toast-container" role="region" aria-live="polite" aria-label="Notifications">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onClose={removeToast} />
      ))}
    </div>
  )
}
