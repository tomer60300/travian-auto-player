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
//
// The ERROR path was the one place that claim was not true. It returned the
// server's `detail` sentence verbatim, and this API's refusal sentences name
// villages and the account key -- "no role template was sent for 01 Kayhut
// Capital (role def)", "this setup was exported from account
// https://...|Kayhut". The reasoning for excluding FastAPI's `input` was about
// echoed VALUES and never reached the prose itself. So `errorDetail` now keeps
// an ALLOWLIST -- pydantic's `type` codes, a field count, a sentence LENGTH --
// and the invariant above holds without an exception. The sentence still
// reaches the operator, through the toast or panel the calling page renders
// from `response.data.detail`; none of those is written to a file.
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

/** The SHAPE of a failure, which is all the invariant above allows.
 *
 * This used to return the server's `detail` verbatim, and that was the one
 * hole in "nothing a request or a response carries reaches the log store". The
 * reasoning behind it -- exclude `input`, because FastAPI echoes the rejected
 * value back in it -- was about echoed VALUES, and the refusal sentences
 * themselves are the account. Two measured examples, both real:
 *
 *   "no role template was sent for 01 Kayhut Capital (role def)..."  (422 from
 *   saving a setup)
 *   "this setup was exported from account https://...|Kayhut, and would be
 *   saved under https://...|Other"
 *
 * A per-field allowlist cannot help there, because the leak is in the prose. So
 * an ALLOWLIST of what survives, not a filter of what does not:
 *
 *   * an entry's `type` -- `int_parsing`, `value_error`, `missing`,
 *     `greater_than` -- is pydantic's own fixed vocabulary and names no value;
 *   * a count of how many fields the server refused;
 *   * the length of a refusal sentence, so a truncated response is
 *     distinguishable from a terse one.
 *
 * `loc` and `msg` are dropped, and both had to be. `loc` carries dict KEYS,
 * which on this API are profile names and village ids
 * (`body.allocations.lumber.30002.value`); and a model validator's own
 * sentence arrives as `msg` prefixed "Value error, ", which is how the
 * attendance refusal naming village ids reaches this function at all.
 *
 * The operator does not lose the sentence. Every page that makes a call
 * surfaces `response.data.detail` itself -- `errorDetail` in
 * `pages/ResourcePlanner.jsx` puts it in a toast, and the YAML digest conflict
 * into a panel that stays until it is acted on -- and none of those is written
 * to a file. This log's job is correlation: which call, what status, how long,
 * how many fields and which kind. The sentence's job is being read now.
 *
 * A body that is not an error envelope at all (an HTML gateway page, say) has
 * nothing quotable in it, so axios's own message is the honest answer.
 */
function errorDetail(error) {
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') {
    return `${detail.length}-char refusal, on screen only`
  }
  if (Array.isArray(detail)) {
    const kinds = [...new Set(detail.map((e) => String(e?.type ?? 'unknown')))]
    return summarizeData(
      `${detail.length} field errors: ${kinds.join(', ')} (text on screen only)`,
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
