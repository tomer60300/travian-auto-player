/**
 * What the API client is ALLOWED to write into the client log store.
 *
 * The store is not a private buffer: the Logs page renders every entry's
 * `detail` and its Export button writes the lot to a `.jsonl` file. So
 * whatever the interceptors put there is, in effect, published.
 *
 * The planner's bodies are the account itself. `/distribution/snapshot` and
 * `/distribution/plan` carry every village's name, coordinates and figures;
 * the `/distribution/plan/yaml` request carries that same payload; and
 * `/distribution/setup` carries the whole hand-typed configuration in both
 * directions. The backend deliberately logs none of it -- the setup store
 * module has no logger at all, and its `__repr__` prints a user id and an
 * account key and nothing else. The client must not be the leak the backend
 * refused to be.
 *
 * These tests drive the real instance through a fake adapter, so they exercise
 * the shipped request AND response interceptors rather than a copy of their
 * logic. `api.defaults.adapter` is axios's own extension point for exactly
 * this; no network, no backend, no mock library.
 */

import { beforeEach, describe, expect, it } from 'vitest'

// The interceptor reads a token from localStorage on every request and vitest
// runs in Node. Same shim, same smallest-possible surface, as
// `pages/pagesRender.test.jsx`.
if (typeof globalThis.localStorage === 'undefined') {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
}

const { default: api } = await import('./api')
const { default: useLogStore } = await import('./stores/logStore')

/** Answer with this body, through the real interceptors. */
function respondWith(status, data) {
  api.defaults.adapter = async (config) => {
    const response = { data, status, statusText: 'OK', headers: {}, config }
    if (status >= 200 && status < 300) return response
    return Promise.reject(
      Object.assign(new Error(`Request failed with status code ${status}`), {
        config,
        response,
        isAxiosError: true,
      })
    )
  }
}

/** Everything the store now holds, as one blob to search. */
function storeText() {
  return useLogStore
    .getState()
    .entries.map((e) => {
      const detail = typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail ?? null)
      return `${e.source} ${e.message} ${detail}`
    })
    .join('\n')
}

// One village, as the planner actually shapes it: a name the operator typed, a
// map coordinate, and a crop figure. None of the three may survive a request.
const VILLAGE = {
  ref: '01 Kayhut Capital',
  coords: { x: -32, y: 105 },
  crop_target: 12526,
  assumed_crop_per_hour: -5880,
}

describe('api log store hygiene', () => {
  beforeEach(() => {
    useLogStore.getState().clear()
    delete api.defaults.adapter
  })

  it('does not log the plan payload a /plan/yaml export sends', async () => {
    respondWith(200, 'routes: []\n')
    await api.post('/distribution/plan/yaml', {
      villages: [VILLAGE],
      expected_plan_digest: 'deadbeef',
    })

    const text = storeText()
    expect(text).not.toContain('Kayhut')
    expect(text).not.toContain('105')
    expect(text).not.toContain('12526')
    expect(text).not.toContain('5880')
  })

  it('does not log a snapshot response body', async () => {
    respondWith(200, { villages: [VILLAGE], account_key: 'acct' })
    await api.get('/distribution/snapshot')

    const text = storeText()
    expect(text).not.toContain('Kayhut')
    expect(text).not.toContain('12526')
    expect(text).not.toContain('acct')
  })

  it('does not log the setup document in either direction', async () => {
    respondWith(200, { version: 6, profiles: { night: { npc_feedstock: 'wood' } } })
    await api.put('/distribution/setup', {
      version: 6,
      villages: [VILLAGE],
      profiles: { night: { npc_feedstock: 'wood', npc_attended: true } },
    })

    const text = storeText()
    expect(text).not.toContain('Kayhut')
    expect(text).not.toContain('npc_feedstock')
    expect(text).not.toContain('npc_attended')
  })

  it('does not log an array response item by item', async () => {
    respondWith(200, [VILLAGE, VILLAGE])
    await api.get('/distribution/run-history')

    const text = storeText()
    expect(text).not.toContain('Kayhut')
    expect(text).toContain('2 items')
  })

  it('keeps a credential out of the store, as it has since the /recon/credentials leak', async () => {
    respondWith(200, { ok: true })
    await api.post('/travian/recon/credentials', {
      username: 'operator',
      password: 'hunter2',
    })

    expect(storeText()).not.toContain('hunter2')
  })

  it('still logs method, path, status and how long it took', async () => {
    respondWith(200, { villages: [] })
    await api.get('/distribution/snapshot')

    const messages = useLogStore.getState().entries.map((e) => e.message)
    expect(messages[0]).toBe('>> GET /distribution/snapshot')
    expect(messages[1]).toMatch(/^<< GET \/distribution\/snapshot 200 \d+ms$/)
    expect(useLogStore.getState().entries[1].source).toBe('planner')
  })

  // The error envelope was the exception the invariant did not admit to. A
  // pydantic entry's `type` is a fixed vocabulary -- `int_parsing`,
  // `value_error`, `missing` -- and names no value; `loc` and `msg` do. `loc`
  // carries dict KEYS, which on this API are profile names and village ids
  // (`body.allocations.lumber.30002.value`), and a model validator's own
  // sentence arrives as `msg`, prefixed "Value error, " -- which is how the
  // attendance refusal naming village ids gets there.
  it('keeps a 422 readable by its codes, without the paths or the sentences', async () => {
    respondWith(422, {
      detail: [
        {
          type: 'int_parsing',
          loc: ['body', 'villages', 0, 'crop_target'],
          msg: 'Input should be a valid integer, unable to parse string as an integer',
          input: '01 Kayhut Capital',
        },
        {
          type: 'value_error',
          loc: ['body', 'allocations', 'lumber', 30002, 'value'],
          msg: 'Value error, no role template was sent for 01 Kayhut Capital (role def)',
          input: {},
        },
      ],
    })
    await expect(api.post('/distribution/plan', { villages: [VILLAGE] })).rejects.toThrow()

    const text = storeText()
    // Enough to act on: how many fields the server refused, and why in the
    // machine's own words.
    expect(text).toContain('2 field errors')
    expect(text).toContain('int_parsing')
    expect(text).toContain('value_error')
    // And nothing the operator typed. Not the value, not the path that names a
    // village id, and not the validator's own sentence.
    expect(text).not.toContain('Kayhut')
    expect(text).not.toContain('30002')
    expect(text).not.toContain('crop_target')
    expect(text).not.toContain('Input should be a valid integer')
  })

  // Both measured examples of the leak were STRING details, and both are the
  // backend's own prose about the operator's own account: "no role template
  // was sent for 01 Kayhut Capital (role def)" from a setup save, and "this
  // setup was exported from account https://...|Kayhut, and would be saved
  // under https://...|Other".
  it('keeps a refusal countable without publishing the sentence', async () => {
    respondWith(409, { detail: 'The plan moved from deadbeef to feedface.' })
    await expect(api.post('/distribution/plan/yaml', { villages: [VILLAGE] })).rejects.toThrow()

    const text = storeText()
    expect(text).toContain('41-char refusal')
    expect(text).toContain('on screen only')
    expect(text).not.toContain('deadbeef')
    expect(text).not.toContain('Kayhut')
  })

  it('does not publish the account key a cross-account refusal names', async () => {
    respondWith(422, {
      detail:
        'this setup was exported from account https://ts2.travian.test|Kayhut, and ' +
        'would be saved under https://ts2.travian.test|Other',
    })
    await expect(api.put('/distribution/setup', { villages: [VILLAGE] })).rejects.toThrow()

    const text = storeText()
    expect(text).not.toContain('Kayhut')
    expect(text).not.toContain('travian.test')
  })

  it('falls back to the axios message when a failure body is not an error envelope', async () => {
    respondWith(502, '<html>502 Bad Gateway</html>')
    await expect(api.get('/distribution/snapshot')).rejects.toThrow()

    const text = storeText()
    expect(text).toContain('Request failed with status code 502')
    expect(text).not.toContain('Bad Gateway')
  })
})
