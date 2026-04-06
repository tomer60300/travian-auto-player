import { create } from 'zustand'
import { useEffect } from 'react'

let toastId = 0

export const useToastStore = create((set) => ({
  toasts: [],

  addToast: (type, message) => {
    const id = ++toastId
    set((state) => ({
      toasts: [...state.toasts, { id, type, message, timestamp: Date.now() }],
    }))
    setTimeout(() => {
      set((state) => ({
        toasts: state.toasts.filter((t) => t.id !== id),
      }))
    }, 4000)
  },

  removeToast: (id) =>
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    })),
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

const typeStyles = {
  success: {
    bg: 'var(--success)',
    border: '#5aa65a',
    icon: '✓',
  },
  error: {
    bg: 'var(--danger)',
    border: '#c44a4a',
    icon: '✕',
  },
  warning: {
    bg: 'var(--warning)',
    border: '#d4913f',
    icon: '⚠',
  },
  info: {
    bg: 'var(--info)',
    border: '#5a8c9c',
    icon: 'ℹ',
  },
}

function ToastItem({ toast, onClose }) {
  const style = typeStyles[toast.type] || typeStyles.info

  return (
    <div
      style={{
        background: style.bg,
        borderLeft: `4px solid ${style.border}`,
        color: 'var(--text-primary)',
        padding: '0.75rem 1rem',
        borderRadius: '0.375rem',
        marginBottom: '0.5rem',
        display: 'flex',
        alignItems: 'center',
        gap: '0.75rem',
        minWidth: '280px',
        maxWidth: '400px',
        boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
        animation: 'slideIn 0.3s ease-out',
      }}
    >
      <span style={{ fontSize: '1.1rem', flexShrink: 0 }}>{style.icon}</span>
      <span style={{ flex: 1, fontSize: '0.875rem' }}>{toast.message}</span>
      <button
        onClick={() => onClose(toast.id)}
        style={{
          background: 'none',
          border: 'none',
          color: 'var(--text-primary)',
          cursor: 'pointer',
          padding: '0 0.25rem',
          fontSize: '1rem',
          opacity: 0.7,
          flexShrink: 0,
        }}
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
    <>
      <style>{`
        @keyframes slideIn {
          from { transform: translateX(100%); opacity: 0; }
          to { transform: translateX(0); opacity: 1; }
        }
      `}</style>
      <div
        style={{
          position: 'fixed',
          top: '1rem',
          right: '1rem',
          zIndex: 9999,
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {toasts.map((toast) => (
          <ToastItem key={toast.id} toast={toast} onClose={removeToast} />
        ))}
      </div>
    </>
  )
}
