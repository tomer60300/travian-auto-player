import axios from 'axios'
import useLogStore from './stores/logStore'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

function logSource(url) {
  if (!url) return 'api'
  // Planner traffic (snapshot / plan / day-check / execute) gets its own source
  // so a live trade-route run is filterable in the Activity Log instead of
  // vanishing into the generic `API` bucket.
  if (url.includes('/distribution')) return 'planner'
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

// Nothing a request or a response CARRIES reaches the log store — only its
// shape. The store is not a private buffer: the Logs page renders every
// entry's `detail` and its Export button writes the lot to a .jsonl file, so
// what lands here is in effect published.
//
// The planner's bodies are the account itself. /distribution/snapshot and
// /distribution/plan carry every village's name, coordinates and figures; the
// /distribution/plan/yaml request carries that same payload; /distribution/setup
// carries the whole hand-typed configuration in both directions. The backend
// deliberately logs none of it — the setup-store module has no logger at all
// and its `__repr__` prints a user id and an account key — so the client must
// not be the leak the backend refused to be.
//
// This SUPERSEDES the per-key redaction that used to run over every body: a
// password cannot be redacted out of a body that is never written down, and
// neither can a village name, which no key list would ever have caught.
// `api.test.js` pins both halves.
function describePayload(data) {
  if (data === undefined || data === null) return null
  if (typeof data === 'string') return `${data.length} chars`
  if (Array.isArray(data)) return `${data.length} items`
  if (typeof data === 'object') return `${Object.keys(data).length} fields`
  return typeof data
}

// Bodies are gone, so the timing is what is left to tell a slow snapshot from
// a hung one.
function elapsed(config) {
  const startedAt = config?.logStartedAt
  return typeof startedAt === 'number' ? ` ${Date.now() - startedAt}ms` : ''
}

function summarizeData(data, maxLen) {
  if (data === undefined || data === null) return null
  try {
    const str = typeof data === 'string' ? data : JSON.stringify(data)
    if (str.length <= maxLen) return str
    return str.slice(0, maxLen) + '...'
  } catch { return '[unserializable]' }
}

/** The one thing worth keeping from a failure: what the server SAID.
 *
 * Read out of the error envelope only, never off the body as a whole.
 * FastAPI's validation errors echo the rejected value back in `input`, so a
 * 422 on a plan payload would re-leak the entire payload through the error
 * path; `loc`, `msg` and `type` name the field and the reason without
 * repeating the value. A body that is not an error envelope at all (an HTML
 * gateway page, say) has nothing quotable in it, so axios's own message is
 * the honest answer.
 */
function errorDetail(error) {
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return summarizeData(detail, 500)
  if (Array.isArray(detail)) {
    return summarizeData(
      detail.map((e) => `${(e?.loc || []).join('.')}: ${e?.msg} (${e?.type})`),
      500
    )
  }
  return error.message
}

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  const method = (config.method || 'get').toUpperCase()
  const url = config.url || ''
  config.logStartedAt = Date.now()
  useLogStore
    .getState()
    .addLog('info', logSource(url), `>> ${method} ${url}`, describePayload(config.data))
  return config
})

let logoutTriggered = false

api.interceptors.response.use(
  (response) => {
    const method = (response.config.method || 'get').toUpperCase()
    const url = response.config.url || ''
    useLogStore
      .getState()
      .addLog(
        'success',
        logSource(url),
        `<< ${method} ${url} ${response.status}${elapsed(response.config)}`,
        describePayload(response.data)
      )
    return response
  },
  (error) => {
    const url = error.config?.url || ''
    const method = (error.config?.method || 'get').toUpperCase()
    const status = error.response?.status
    useLogStore
      .getState()
      .addLog(
        'error',
        logSource(url),
        `<< ${method} ${url} ${status || 'ERR'}${elapsed(error.config)}`,
        errorDetail(error)
      )

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
