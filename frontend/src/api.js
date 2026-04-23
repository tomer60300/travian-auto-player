import axios from 'axios'
import useLogStore from './stores/logStore'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

function logSource(url) {
  if (!url) return 'api'
  if (url.includes('/travian')) return 'auth'
  if (url.includes('/buildings') || url.includes('/villages')) return 'game'
  if (url.includes('/military')) return 'military'
  if (url.includes('/farm')) return 'farm'
  if (url.includes('/scout')) return 'scout'
  if (url.includes('/queue')) return 'queue'
  if (url.includes('/video')) return 'video'
  if (url.includes('/reports')) return 'reports'
  if (url.includes('/users')) return 'auth'
  return 'api'
}

const SENSITIVE_URLS = ['/users/login', '/users/register', '/travian/connect', '/travian/servers']
const SENSITIVE_KEYS = ['password', 'access_token', 'token', 'jwt']

function redactSensitive(data) {
  if (!data || typeof data !== 'object') return data
  const copy = { ...data }
  for (const key of SENSITIVE_KEYS) {
    if (key in copy) copy[key] = '[REDACTED]'
  }
  return copy
}

function summarizeData(data, maxLen) {
  if (data === undefined || data === null) return null
  try {
    const str = typeof data === 'string' ? data : JSON.stringify(data)
    if (str.length <= maxLen) return str
    return str.slice(0, maxLen) + '...'
  } catch { return '[unserializable]' }
}

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  const method = (config.method || 'get').toUpperCase()
  const url = config.url || ''
  const isSensitive = SENSITIVE_URLS.some(s => url.includes(s))
  const body = config.data
    ? summarizeData(isSensitive ? redactSensitive(config.data) : config.data, 500)
    : null
  useLogStore.getState().addLog('info', logSource(url), `>> ${method} ${url}`, body)
  return config
})

let logoutTriggered = false

api.interceptors.response.use(
  (response) => {
    const method = (response.config.method || 'get').toUpperCase()
    const url = response.config.url || ''
    const status = response.status
    const isSensitive = SENSITIVE_URLS.some(s => url.includes(s))
    const data = isSensitive ? redactSensitive(response.data) : response.data

    // Build detail summary
    let detail = null
    if (Array.isArray(data)) {
      detail = `[${data.length} items]`
      if (data.length > 0 && data.length <= 3) detail = summarizeData(data, 800)
    } else if (data && typeof data === 'object') {
      detail = summarizeData(data, 800)
    }

    useLogStore.getState().addLog('success', logSource(url), `<< ${method} ${url} ${status}`, detail)
    return response
  },
  (error) => {
    const url = error.config?.url || ''
    const method = (error.config?.method || 'get').toUpperCase()
    const status = error.response?.status
    const detail = error.response?.data
      ? summarizeData(error.response.data, 500)
      : error.message
    useLogStore.getState().addLog('error', logSource(url), `<< ${method} ${url} ${status || 'ERR'}`, detail)

    if (status === 401 && !logoutTriggered) {
      logoutTriggered = true
      Promise.resolve().then(() => {
        const hadToken = !!localStorage.getItem('token')
        if (hadToken) {
          localStorage.removeItem('token')
          import('./stores/authStore')
            .then(({ default: useAuthStore }) => {
              const state = useAuthStore.getState()
              if (state.isAuthenticated) state.logout()
            })
            .catch(() => {})
            .finally(() => { logoutTriggered = false })
        } else {
          logoutTriggered = false
        }
      })
    }
    return Promise.reject(error)
  }
)

export default api
