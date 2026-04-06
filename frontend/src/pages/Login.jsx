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
    <div className="min-h-screen flex items-center justify-center px-4 bg-base">
      <div className="w-full max-w-md">
        {/* Title */}
        <h1 className="logo-title text-3xl text-center mb-8">
          Travian Auto Player
        </h1>

        {/* Card */}
        <div className="card">
          {/* Tabs */}
          <div className="flex mb-6 border-b-default">
            <button
              type="button"
              onClick={() => switchMode('login')}
              className={`tab-btn${mode === 'login' ? ' tab-btn-active' : ''}`}
            >
              Login
            </button>
            <button
              type="button"
              onClick={() => switchMode('register')}
              className={`tab-btn${mode === 'register' ? ' tab-btn-active' : ''}`}
            >
              Register
            </button>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="username"
                className="text-sm font-semibold text-secondary"
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
                className="text-sm font-semibold text-secondary"
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
                  className="text-sm font-semibold text-secondary"
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
              <div className="error-box">{error}</div>
            )}

            {/* Submit */}
            <button
              type="submit"
              className="btn-primary btn-lg w-full mt-2 flex items-center justify-center gap-2"
              disabled={loading}
            >
              {loading && (
                <span className="spinner spinner-sm" />
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
