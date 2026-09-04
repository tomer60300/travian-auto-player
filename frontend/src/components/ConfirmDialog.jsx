import { useEffect, useRef } from 'react'

export default function ConfirmDialog({
  open,
  title = 'Confirm',
  message = 'Are you sure?',
  confirmText = 'Confirm',
  cancelText = 'Cancel',
  onConfirm,
  onCancel,
  variant = 'default',
}) {
  const dialogRef = useRef(null)
  const confirmBtnRef = useRef(null)

  useEffect(() => {
    if (!open) return

    // Focus what the operator has to answer: a form control if the message
    // carries one, otherwise the action. A dialog that asks for a profile name
    // and lands focus on its Confirm button makes the typist Tab backwards to
    // reach the only field in it -- and every dialog in the app that carries no
    // field behaves exactly as it did.
    const timer = setTimeout(() => {
      const field = dialogRef.current?.querySelector('input, select, textarea')
      if (field) field.focus()
      else confirmBtnRef.current?.focus()
    }, 50)

    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        onCancel?.()
        return
      }

      // Focus trap
      if (e.key === 'Tab' && dialogRef.current) {
        const focusable = dialogRef.current.querySelectorAll(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]

        if (e.shiftKey) {
          if (document.activeElement === first) {
            e.preventDefault()
            last.focus()
          }
        } else {
          if (document.activeElement === last) {
            e.preventDefault()
            first.focus()
          }
        }
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, onCancel])

  if (!open) return null

  return (
    <div
      className="dialog-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel?.()
      }}
    >
      <div
        ref={dialogRef}
        className="dialog-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
      >
        <h3
          id="confirm-title"
          className="heading-gold text-lg mb-3"
        >
          {title}
        </h3>
        {/* A <div>, not a <p>. `message` is a node rather than a string at the
            planner's live-run manifest, which is a LIST of the state-changing
            effects a run will have -- and a <ul> inside a <p> is invalid HTML
            that the parser silently reshapes, so the list escaped the styled
            wrapper. Every caller that passes a plain string renders identically. */}
        <div className="text-secondary text-sm mb-6 leading-relaxed">
          {message}
        </div>
        <div className="flex justify-end gap-3">
          <button type="button" className="btn-secondary" onClick={onCancel}>
            {cancelText}
          </button>
          <button type="button"
            ref={confirmBtnRef}
            className={variant === 'danger' ? 'btn-danger' : 'btn-primary'}
            onClick={onConfirm}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  )
}
