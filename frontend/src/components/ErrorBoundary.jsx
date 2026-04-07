import { Component } from 'react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, errorInfo) {
    console.error('ErrorBoundary caught:', error, errorInfo?.componentStack)
    try {
      const { addLog } = require('../stores/logStore').default.getState()
      addLog('error', 'ui', `React error: ${error.message}`, errorInfo?.componentStack || error.stack)
    } catch {}
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null })
  }

  handleGoHome = () => {
    this.setState({ hasError: false, error: null })
    window.location.href = '/'
  }

  handleClearAndLogin = () => {
    localStorage.removeItem('token')
    this.setState({ hasError: false, error: null })
    window.location.href = '/login'
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-base p-6" role="alert">
          <div className="card max-w-md w-full text-center">
            <h2 className="heading-gold text-xl mb-3">Something went wrong</h2>
            <p className="text-secondary text-sm mb-1">
              An unexpected error occurred in this page.
            </p>
            {this.state.error?.message && (
              <p className="text-danger text-xs mb-4 font-mono break-all">
                {this.state.error.message}
              </p>
            )}
            <div className="flex flex-col gap-2">
              <button onClick={this.handleReset} className="btn-primary btn-full">
                Try Again
              </button>
              <button onClick={this.handleGoHome} className="btn-secondary btn-full">
                Go to Dashboard
              </button>
              <button onClick={this.handleClearAndLogin} className="btn-danger btn-full btn-sm">
                Clear Session &amp; Login
              </button>
            </div>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}
