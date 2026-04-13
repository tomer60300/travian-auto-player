import { useState } from 'react'
import useCaptchaStore from '../stores/captchaStore'
import api from '../api'

export default function CaptchaAlert() {
  const active = useCaptchaStore((s) => s.active)
  const pattern = useCaptchaStore((s) => s.pattern)
  const triggeredAt = useCaptchaStore((s) => s.triggeredAt)
  const url = useCaptchaStore((s) => s.url)
  const statusCode = useCaptchaStore((s) => s.statusCode)
  const responseSnippet = useCaptchaStore((s) => s.responseSnippet)
  const resolve = useCaptchaStore((s) => s.resolve)
  const [resolving, setResolving] = useState(false)

  if (!active) return null

  const handleResolve = async () => {
    setResolving(true)
    try {
      await api.post('/captcha/resolve')
      resolve()
    } catch (err) {
      console.error('Captcha resolve failed:', err)
      resolve()
    } finally {
      setResolving(false)
    }
  }

  const handleDismiss = async () => {
    // False positive — resolve without requiring browser visit
    setResolving(true)
    try {
      await api.post('/captcha/resolve')
      resolve()
    } catch {
      resolve()
    } finally {
      setResolving(false)
    }
  }

  const timeStr = triggeredAt
    ? new Date(triggeredAt).toLocaleTimeString()
    : null

  return (
    <div className="captcha-overlay" role="alertdialog" aria-modal="true" aria-labelledby="captcha-title">
      <div className="captcha-card">
        <div className="captcha-icon">!</div>
        <h2 id="captcha-title" className="captcha-title">
          Bot Detection Triggered
        </h2>
        <p className="captcha-message">
          Travian has detected automated activity and is showing a captcha
          or blocking page. <strong>All automated operations have been paused.</strong>
        </p>

        {/* Diagnostic context */}
        <div className="captcha-context">
          <div className="captcha-context-row">
            <span className="captcha-context-label">Pattern:</span>
            <code>{pattern}</code>
          </div>
          {timeStr && (
            <div className="captcha-context-row">
              <span className="captcha-context-label">Time:</span>
              <span>{timeStr}</span>
            </div>
          )}
          {url && (
            <div className="captcha-context-row">
              <span className="captcha-context-label">URL:</span>
              <span className="captcha-context-url">{url}</span>
            </div>
          )}
          {statusCode > 0 && (
            <div className="captcha-context-row">
              <span className="captcha-context-label">HTTP Status:</span>
              <span>{statusCode}</span>
            </div>
          )}
          {responseSnippet && (
            <div className="captcha-context-snippet">
              <span className="captcha-context-label">Response snippet:</span>
              <pre>{responseSnippet}</pre>
            </div>
          )}
        </div>

        <div className="captcha-instructions">
          <p><strong>To resolve:</strong></p>
          <ol>
            <li>Open your Travian server in a regular browser tab</li>
            <li>Log in to your account if needed</li>
            <li>Complete any captcha challenge that appears</li>
            <li>Navigate around a few pages to confirm access is restored</li>
            <li>Return here and click the button below</li>
          </ol>
        </div>
        <div className="captcha-actions">
          <button
            onClick={handleResolve}
            disabled={resolving}
            className="btn-primary captcha-resolve-btn"
          >
            {resolving ? 'Resuming...' : "I've Resolved the Captcha"}
          </button>
          <button
            onClick={handleDismiss}
            disabled={resolving}
            className="btn-secondary captcha-dismiss-btn"
          >
            Dismiss (False Positive)
          </button>
        </div>
      </div>
    </div>
  )
}
