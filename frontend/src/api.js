import axios from 'axios'
import useLogStore from './stores/logStore'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
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

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  const method = (config.method || 'get').toUpperCase()
  const url = config.url || ''
  useLogStore.getState().addLog('info', logSource(url), `${method} ${url}`)
  return config
})

let logoutTriggered = false

api.interceptors.response.use(
  (response) => {
    const method = (response.config.method || 'get').toUpperCase()
    const url = response.config.url || ''
    const status = response.status
    useLogStore.getState().addLog('success', logSource(url), `${method} ${url} -> ${status}`)
    return response
  },
  (error) => {
    const url = error.config?.url || ''
    const status = error.response?.status
    const detail = error.response?.data?.detail || error.message
    useLogStore.getState().addLog('error', logSource(url), `${(error.config?.method || 'get').toUpperCase()} ${url} -> ${status || 'ERR'}`, detail)

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
