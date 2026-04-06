import { useState } from 'react'
import useAuthStore from '../stores/authStore'
import { useToast } from '../components/Toast'

export default function Login() {
  const [mode, setMode] = useState('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const login = useAuthStore((s) => s.login)
  const register = useAuthStore((s) => s.register)
  const toast = useToast()

  const isRegister = mode === 'register'

  function validate() {
    if (username.length < 3 || username.length > 32) {
      return 'Username must be between 3 and 32 characters'
    }
    if (password.length < 6) {
      return 'Password must be at least 6 characters'
    }
    if (isRegister && password !== confirmPassword) {
      return 'Passwords do not match'
    }
    return null
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')

    const validationError = validate()
    if (validationError) {
      setError(validationError)
      return
    }

    setLoading(true)
    try {
      if (isRegister) {
        await register(username, password)
        toast.success('Account created successfully!')
      } else {
        await login(username, password)
        toast.success('Welcome back!')
      }
    } catch (err) {
      const message =
        err.response?.data?.detail ||
        err.response?.data?.message ||
        (isRegister ? 'Registration failed' : 'Invalid credentials')
      setError(message)
    } finally {
      setLoading(false)
    }
  }

  function switchMode(newMode) {
    setMode(newMode)
    setError('')
    setConfirmPassword('')
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center px-4"
      style={{ backgroundColor: 'var(--bg-base)' }}
    >
      <div className="w-full max-w-md">
        {/* Title */}
        <h1
          className="text-3xl text-center mb-8"
          style={{ fontFamily: "'Cinzel Decorative', serif", color: 'var(--accent-gold)' }}
        >
          Travian Auto Player
        </h1>

        {/* Card */}
        <div className="card">
          {/* Tabs */}
          <div className="flex mb-6" style={{ borderBottom: '1px solid var(--border)' }}>
            <button
              type="button"
              onClick={() => switchMode('login')}
              className="flex-1 pb-3 text-center font-semibold transition-colors"
              style={{
                color: mode === 'login' ? 'var(--accent-gold)' : 'var(--text-secondary)',
                borderBottom: mode === 'login' ? '2px solid var(--accent-gold)' : '2px solid transparent',
                background: 'none',
                border: 'none',
                borderBottom: mode === 'login' ? '2px solid var(--accent-gold)' : '2px solid transparent',
                cursor: 'pointer',
                fontSize: '1rem',
                paddingBottom: '0.75rem',
              }}
            >
              Login
            </button>
            <button
              type="button"
              onClick={() => switchMode('register')}
              className="flex-1 pb-3 text-center font-semibold transition-colors"
              style={{
                color: mode === 'register' ? 'var(--accent-gold)' : 'var(--text-secondary)',
                background: 'none',
                border: 'none',
                borderBottom: mode === 'register' ? '2px solid var(--accent-gold)' : '2px solid transparent',
                cursor: 'pointer',
                fontSize: '1rem',
                paddingBottom: '0.75rem',
              }}
            >
              Register
            </button>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="username"
                className="text-sm font-semibold"
                style={{ color: 'var(--text-secondary)' }}
              >
                Username
              </label>
              <input
                id="username"
                type="text"
                className="input-field"
                placeholder="Enter username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                disabled={loading}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="password"
                className="text-sm font-semibold"
                style={{ color: 'var(--text-secondary)' }}
              >
                Password
              </label>
              <input
                id="password"
                type="password"
                className="input-field"
                placeholder="Enter password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={isRegister ? 'new-password' : 'current-password'}
                disabled={loading}
              />
            </div>

            {isRegister && (
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="confirm-password"
                  className="text-sm font-semibold"
                  style={{ color: 'var(--text-secondary)' }}
                >
                  Confirm Password
                </label>
                <input
                  id="confirm-password"
                  type="password"
                  className="input-field"
                  placeholder="Confirm password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  autoComplete="new-password"
                  disabled={loading}
                />
              </div>
            )}

            {/* Error message */}
            {error && (
              <div
                className="text-sm px-3 py-2 rounded"
                style={{
                  backgroundColor: 'rgba(179, 64, 64, 0.2)',
                  border: '1px solid var(--danger)',
                  color: '#e88',
                }}
              >
                {error}
              </div>
            )}

            {/* Submit */}
            <button
              type="submit"
              className="btn-primary w-full mt-2 flex items-center justify-center gap-2"
              disabled={loading}
              style={{ padding: '0.625rem 1rem', fontSize: '1rem' }}
            >
              {loading && (
                <span
                  className="inline-block w-4 h-4 rounded-full animate-spin"
                  style={{
                    border: '2px solid var(--bg-base)',
                    borderTopColor: 'transparent',
                  }}
                />
              )}
              {loading
                ? isRegister
                  ? 'Creating Account...'
                  : 'Signing In...'
                : isRegister
                  ? 'Create Account'
                  : 'Sign In'}
            </button>
          </form>
        </div>
      </div>
    </div>
  )
}
