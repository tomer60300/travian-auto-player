/**
 * The planner's e2e fixture, shared by the specs that drive its write path.
 *
 * Not a `*.pw.js` file, deliberately: `playwright.config.js` sets
 * `testMatch: '**\/*.pw.js'`, so a spec importing from another SPEC would
 * register that spec's tests a second time. A plain module can be imported
 * freely.
 *
 * `inputWidths.pw.js` keeps its own, larger fixture and is left alone. That one
 * exists to fill every measured box with five-figure content; this one exists to
 * get the write path on screen with the least state, and merging them would make
 * each spec carry the other's reasons.
 *
 * NO BACKEND AND NO GAME REQUEST. `isolate` answers what the shell asks for and
 * ABORTS everything else fail-closed, and the snapshot is seeded into
 * localStorage rather than fetched, so nothing here can reach :8001 -- let alone
 * the game. There is a live Travian account on this machine.
 */

import { expect } from '@playwright/test'

export const SERVER = 'https://ts2.x1.europe.travian.com'
export const PLAYER = 'e2e-operator'
export const KEY = `${SERVER}|${PLAYER}`

export const CAPITAL = 20002
export const DEF_A = 20011

/** The three widths the UI Definition of Done names. */
export const VIEWPORTS = [
  { width: 375, height: 900 },
  { width: 768, height: 1000 },
  { width: 1440, height: 1200 },
]

export function village(id, name, x, y) {
  return {
    village_id: id,
    name,
    x,
    y,
    merchants_total: 20,
    merchants_free: 20,
    lumber_per_hour: 8372,
    clay_per_hour: 5168,
    iron_per_hour: 5809,
    crop_per_hour: 2200,
    crop_draining: false,
    lumber_stock: 100_000,
    clay_stock: 100_000,
    iron_stock: 100_000,
    crop_stock: 100_000,
    warehouse_capacity: 400_000,
    granary_capacity: 400_000,
  }
}

export const SNAPSHOT = {
  villages: [village(CAPITAL, '02', 0, 0), village(DEF_A, '11', 4, 0)],
  map_span: 401,
  speed_fields_per_hour: 16,
  requests_used: 0,
  warnings: [],
}

/** A clean, feasible one-route plan: the least the Plan stage needs to render. */
export const PLAN = {
  rows: [
    {
      origin: CAPITAL,
      origin_name: '02',
      destination: DEF_A,
      destination_name: '11',
      cargo: { lumber: 7920, clay: 0, iron: 0, crop: 0 },
      cycle_hours: 4,
      dispatch: '08:20',
      arrival: '09:48',
      merchants: 3,
    },
  ],
  budgets: [{ village_id: CAPITAL, committed: 3, spare: 14, over_budget: false, legs: [] }],
  shortfalls: [],
  unallocated: [],
  total_merchants: 3,
  feasible: true,
  verdict: {
    executable: true,
    clean: true,
    blockers: [],
    covers: ['every merchant budget', 'every receiver is routable'],
    unweighed: [],
    critical_findings: 0,
  },
  warnings: [],
  relays: [],
  role_deviations: [],
  village_nets: [],
  night_overruns: [],
  npc_reserves: [],
  npc_triggers: [],
  diagnostics: null,
  plan_digest: 'd'.repeat(64),
}

/** A preview response with the live opt-in ON, so the live branch renders. */
export const PREVIEW = {
  dry_run: true,
  live_enabled: true,
  created: 0,
  created_unverified: 0,
  not_created: 0,
  remaining: 0,
  created_game_rows: 6,
  actions: [
    {
      origin: CAPITAL,
      origin_name: '02',
      destination: DEF_A,
      destination_name: '11',
      dest_x: 4,
      dest_y: 0,
      cycle_hours: 4,
      merchants: 3,
      status: 'would_create',
      detail: '',
    },
  ],
  disables: [],
  re_enables: [],
  problems: [],
  warnings: [],
  updates: [],
  filtered_to: null,
  requests_forecast: {
    estimated_total: 4,
    estimated_total_max: 6,
    marketplace_reads: 1,
    creates: 1,
    verify_reads: 1,
    trim_deletes: 1,
  },
  trace_id: 'abc123def456',
}

/**
 * Everything the shell asks for, and a hard stop for anything else.
 *
 * `extra` answers the calls one spec needs on top of the shell's, as a function
 * of the request rather than a URL table: these endpoints carry query strings,
 * and a spec often wants to read the BODY it was sent. Return `undefined` for
 * anything it does not recognise so the abort stays the default.
 */
export async function isolate(page, extra = () => undefined) {
  await page.routeWebSocket(/.*/, (ws) => ws.close())
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/users/me')) {
      return route.fulfill({ json: { id: 1, username: PLAYER, is_active: true } })
    }
    if (path.endsWith('/travian/status')) {
      return route.fulfill({
        json: {
          connected: true,
          server_url: SERVER,
          player_name: PLAYER,
          tribe_id: 1,
          active_village_id: CAPITAL,
          villages: SNAPSHOT.villages.map((v) => ({
            id: v.village_id,
            name: v.name,
            x: v.x,
            y: v.y,
          })),
        },
      })
    }
    if (path.endsWith('/distribution/setup')) {
      return route.fulfill({
        status: 404,
        json: { detail: 'No planner setup is saved for this account.' },
      })
    }
    if (path.endsWith('/distribution/plan')) return route.fulfill({ json: PLAN })
    const answer = await extra(path, route)
    if (answer === 'handled') return undefined
    if (answer !== undefined) return route.fulfill({ json: answer })
    return route.abort('blockedbyclient')
  })
}

/** A connected account with a fresh snapshot and nothing else typed.
 *
 * `extra` is a plain `{ [localStorage suffix]: value }` map, applied under the
 * same account key the page scopes itself to.
 */
export async function seed(page, extra = {}) {
  await page.addInitScript(
    ([key, snapshot, more]) => {
      const set = (name, value) => localStorage.setItem(`${name}::${key}`, JSON.stringify(value))
      localStorage.setItem('token', 'e2e-not-a-real-token')
      set('planner_snapshot', snapshot)
      set('planner_snapshot_at', Date.now())
      for (const [name, value] of Object.entries(more)) set(name, value)
    },
    [KEY, SNAPSHOT, extra],
  )
}

/** The Targets stage, which carries the 25%/60% night pair. */
export async function openTargets(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: 'Targets' }).click()
  await expect(page.getByText('Derive an idle-window profile from your stores')).toBeVisible()
}

/** The Plan stage, which carries the whole write path. */
export async function openPlan(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}
