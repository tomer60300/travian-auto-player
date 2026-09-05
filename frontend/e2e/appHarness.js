/**
 * Fail-closed page harness for the NON-planner routes.
 *
 * `plannerHarness.js` does this job for `/resource-planner`, which has its own
 * large fixture surface. Every other route needs the same three things and
 * nothing more: a token in `localStorage`, `/users/me` + `/travian/status`
 * answered so `Layout` renders instead of redirecting to `/connect`, and
 * EVERYTHING ELSE REFUSED. There is a live Travian account on this machine, so
 * the default for an unrecognised `/api/**` path is `route.abort`, never a pass
 * through -- a spec that forgets a fixture fails, it does not reach a server.
 *
 * Not a spec itself (no `.pw.js` suffix), the same way `plannerHarness.js` and
 * `contrast.js` are not.
 *
 * `extra` maps a path SUFFIX to a body:
 *   - a plain value is fulfilled as JSON;
 *   - `{ status, json }` fulfils with that status, which is how a spec drives
 *     the ERROR variant of a page rather than its empty one.
 */

export const PLAYER = 'e2e-operator'
export const SERVER = 'https://ts2.x1.europe.travian.com'
export const CAPITAL = 20002
export const SECOND_VILLAGE = 20011

export const VILLAGES = [
  { id: CAPITAL, name: '02', x: 0, y: 0 },
  { id: SECOND_VILLAGE, name: '11', x: 4, y: 0 },
]

export const STATUS = {
  connected: true,
  server_url: SERVER,
  player_name: PLAYER,
  tribe_id: 1,
  active_village_id: CAPITAL,
  villages: VILLAGES,
}

export async function isolateApp(page, extra = {}) {
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    for (const [suffix, body] of Object.entries(extra)) {
      if (!path.endsWith(suffix)) continue
      if (body && typeof body === 'object' && 'status' in body && !Array.isArray(body)) {
        return route.fulfill({ status: body.status, json: body.json ?? { detail: 'e2e failure' } })
      }
      return route.fulfill({ json: body })
    }
    if (path.endsWith('/users/me')) {
      return route.fulfill({ json: { id: 1, username: PLAYER, is_active: true } })
    }
    if (path.endsWith('/travian/status')) return route.fulfill({ json: STATUS })
    return route.abort('blockedbyclient')
  })
  // Nothing here plays a socket back; a page that opens one gets it closed.
  await page.routeWebSocket(/.*/, (ws) => ws.close())
  await page.addInitScript(() => localStorage.setItem('token', 'e2e-not-a-real-token'))
}
