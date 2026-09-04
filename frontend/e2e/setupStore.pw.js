/**
 * The setup, on the server: save, load, forget — and the distinction the whole
 * thing turns on.
 *
 * `localStorage` is per ORIGIN, and this app is reached on four of them, so it
 * kept four independent copies of every hand-typed figure and a cleared origin
 * lost the lot. Three endpoints existed and were tested; the UI called none.
 *
 * **404 is not "empty".** The store answers 404 when nothing has ever been
 * saved, deliberately: "you have never saved" invites importing a file, while
 * "you saved a blank sheet" is a decision to leave the account undescribed.
 * Collapsing them would turn the second into the first and quietly suggest the
 * operator undo it. So this spec asserts the two render DIFFERENTLY, which is
 * the one assertion a naive implementation fails.
 *
 * It also pins that a document goes out and comes back through the SAME path a
 * file does -- `buildSetup` writes it, `parseSetup` reads it -- because the
 * store keeps the body verbatim, and a document written by a newer build has
 * to be refused rather than half-loaded.
 *
 * NO BACKEND AND NO GAME REQUEST: every `/api` call is answered here or
 * ABORTED. The store is a fixture in this file, so the assertions are about
 * what the page sends and how it reads the answer.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test setupStore
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 70002
const DEF_A = 70011

function village(id, name, x, y) {
  return {
    village_id: id,
    name,
    x,
    y,
    merchants_total: 20,
    merchants_free: 20,
    lumber_per_hour: 6000,
    clay_per_hour: 6000,
    iron_per_hour: 6000,
    crop_per_hour: 1200,
    crop_draining: false,
    lumber_stock: 100_000,
    clay_stock: 100_000,
    iron_stock: 100_000,
    crop_stock: 100_000,
    warehouse_capacity: 400_000,
    granary_capacity: 400_000,
  }
}

const SNAPSHOT = {
  villages: [village(CAPITAL, '02', 0, 0), village(DEF_A, '11', 4, 0)],
  map_span: 401,
  speed_fields_per_hour: 16,
  requests_used: 0,
  warnings: [],
}

const SAVED_AT = '2026-09-03T09:15:00Z'

/** A stored document, in the format `buildSetup` writes. */
const STORED = {
  format: 'travian-planner-owned-state',
  version: 6,
  exported_at: SAVED_AT,
  account: KEY,
  villages: [
    { village_id: CAPITAL, name: '02', trade_office_level: 13, max_busy_merchants: 8 },
    { village_id: DEF_A, name: '11', role: 'def' },
  ],
  roles: { def: { allocations: {}, consumption: { lumber: 8372 } } },
}

/** The same document at v9, carrying the two WORLD overrides.
 *
 * `buildSetup` has always written these -- it stores `merchant_model` wholesale
 * -- and `parseSetup` rebuilt the model field by field and dropped them, so a
 * saved setup came back with the operator's own span and speed missing and
 * nothing said. Same version: the writer's half never changed. */
const STORED_WORLD = {
  ...STORED,
  version: 9,
  merchant_model: {
    base_capacity: 2500,
    bonus_per_to_level: 0.2,
    map_span: 801,
    speed_fields_per_hour: 24,
  },
}

/**
 * @param {object} store the store's behaviour: `get` is 'found' | 'missing' |
 *   'empty' | 'error' | 'world', and `put`/`del` are 'ok' | 'refused' |
 *   'missing'.
 */
async function isolate(page, store = {}) {
  const behaviour = { get: 'missing', put: 'ok', del: 'ok', ...store }
  const sent = { get: [], put: [], del: [] }
  await page.routeWebSocket(/.*/, (ws) => ws.close())
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const method = route.request().method()
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
          villages: SNAPSHOT.villages.map((v) => ({ id: v.village_id, name: v.name })),
        },
      })
    }
    if (path.endsWith('/distribution/setup')) {
      const accountKey = url.searchParams.get('account_key')
      if (method === 'GET') {
        sent.get.push(accountKey)
        if (behaviour.get === 'missing') {
          return route.fulfill({
            status: 404,
            json: { detail: 'No planner setup is saved for this account.' },
          })
        }
        if (behaviour.get === 'error') {
          return route.fulfill({ status: 500, json: { detail: 'the database is unavailable' } })
        }
        // 'empty' is a SAVED document that describes nothing — a decision, and
        // not the same state as 404.
        const setup =
          behaviour.get === 'empty'
            ? { ...STORED, villages: [], roles: {} }
            : behaviour.get === 'world'
              ? STORED_WORLD
              : STORED
        return route.fulfill({
          json: { account_key: accountKey, setup, saved_at: SAVED_AT },
        })
      }
      if (method === 'PUT') {
        sent.put.push({ accountKey, body: route.request().postDataJSON() })
        if (behaviour.put === 'refused') {
          // The store's second detail shape: a Pydantic field-error list.
          return route.fulfill({
            status: 422,
            json: {
              detail: [
                { loc: ['villages', 0, 'trade_office_level'], msg: 'it must be 0 to 20' },
                { loc: [], msg: 'a relay may not feed a relay' },
              ],
            },
          })
        }
        return route.fulfill({
          json: { account_key: accountKey, setup: route.request().postDataJSON(), saved_at: SAVED_AT },
        })
      }
      if (method === 'DELETE') {
        sent.del.push(accountKey)
        if (behaviour.del === 'missing') {
          return route.fulfill({
            status: 404,
            json: { detail: 'No planner setup is saved for this account.' },
          })
        }
        return route.fulfill({ status: 204, body: '' })
      }
    }
    return route.abort('blockedbyclient')
  })
  return sent
}

async function seed(page, { tradeOffice = null } = {}) {
  await page.addInitScript(
    ([key, snap, levels]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      if (levels != null) {
        localStorage.setItem(`planner_trade_office::${key}`, JSON.stringify(levels))
      }
    },
    [KEY, SNAPSHOT, tradeOffice]
  )
  // No native dialog handler any more, and that is the assertion rather than an
  // omission: the account-mismatch confirm and the forget confirm are both
  // `components/ConfirmDialog` now, so a `page.on('dialog')` here would be
  // waiting for something that can no longer happen -- and Playwright's default
  // is to DISMISS, which would silently turn every one of these into a no-op.
  // The tests below click the in-app button instead.
}

test.describe('the setup on the server', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('nothing saved is an invitation, not an error', async ({ page }) => {
    const sent = await isolate(page, { get: 'missing' })
    await seed(page)
    await page.goto('/resource-planner')

    // Probed on arrival: an operator reaching a fourth origin needs to know a
    // saved copy exists without first guessing that it might.
    await expect(page.getByText(/Nothing is saved on the server for this account yet/)).toBeVisible()
    expect(sent.get).toContain(KEY)

    // And the two actions that need something saved are not offered.
    await expect(page.getByRole('button', { name: 'Load setup from server' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Forget the saved setup' })).toBeDisabled()
  })

  test('a saved but EMPTY document is a different state from nothing saved', async ({ page }) => {
    await isolate(page, { get: 'empty' })
    await seed(page)
    await page.goto('/resource-planner')

    // The distinction the store exists to preserve: this says a setup IS
    // saved, with when, and offers to load and to forget it.
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Load setup from server' })).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Forget the saved setup' })).toBeEnabled()
    await expect(
      page.getByText(/Nothing is saved on the server for this account yet/)
    ).toHaveCount(0)
  })

  test('loading applies the document the same way a file does', async ({ page }) => {
    await isolate(page, { get: 'found' })
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Load setup from server' }).click()

    // The same load report the file path renders -- naming THIS source, since
    // "from the setup file" is the wrong sentence about a server document --
    // and the same cells filled in.
    await expect(page.getByText('Loaded 2 village(s) from the saved setup.')).toBeVisible()
    await expect(page.getByLabel('Trade Office level for 02')).toHaveValue('13')
    await expect(page.getByLabel('Most merchants busy at once for 02')).toHaveValue('8')
    await expect(page.getByLabel('Role for 11')).toHaveValue('def')
    // The role template came with it, so the row no longer says the role has
    // no profile -- which is what the backend would refuse the plan over.
    await expect(page.getByText('no DEF template yet')).toHaveCount(0)
  })

  test('the two world overrides survive the load, instead of vanishing', async ({ page }) => {
    await isolate(page, { get: 'world' })
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Load setup from server' }).click()
    await expect(page.getByText('Loaded 2 village(s) from the saved setup.')).toBeVisible()

    // Both were dropped in silence before. The span scales every distance the
    // geometry computes and the speed divides into every travel time, so a
    // non-Europe-2 operator reloaded their own document and planned another
    // world's journeys.
    await expect(page.getByLabel('Map span override')).toHaveValue('801')
    await expect(page.getByLabel('Merchant speed fields per hour override')).toHaveValue('24')
  })

  test('saving sends the same document the file export writes', async ({ page }) => {
    const sent = await isolate(page, { get: 'missing' })
    await seed(page, { tradeOffice: { [CAPITAL]: 13 } })
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(sent.put).toHaveLength(1)
    expect(sent.put[0].accountKey).toBe(KEY)
    const body = sent.put[0].body
    // `buildSetup`'s own document, not a second format.
    expect(body.format).toBe('travian-planner-owned-state')
    expect(body.account).toBe(KEY)
    expect(body.villages).toEqual([
      { village_id: CAPITAL, name: '02', trade_office_level: 13 },
    ])
  })

  test('a refusal is readable whichever shape the detail arrives in', async ({ page }) => {
    await isolate(page, { get: 'missing', put: 'refused' })
    await seed(page, { tradeOffice: { [CAPITAL]: 13 } })
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Save setup to server' }).click()

    // A Pydantic field-error LIST, rendered as sentences rather than as
    // "[object Object]" -- the shape `errorDetail` exists for.
    await expect(page.getByText(/it must be 0 to 20/)).toBeVisible()
    await expect(page.getByText(/a relay may not feed a relay/)).toBeVisible()
    // And the state is unchanged: a refused save saved nothing.
    await expect(page.getByText(/Nothing is saved on the server for this account yet/)).toBeVisible()
  })

  test('forgetting asks first, then says the shared copy is gone', async ({ page }) => {
    const sent = await isolate(page, { get: 'found' })
    await seed(page)
    await page.goto('/resource-planner')
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    await page.getByRole('button', { name: 'Forget the saved setup' }).click()
    // Asks in the app, not in the browser chrome: a native `confirm` cannot be
    // re-read, and Chrome's dialog suppression makes a later one return false
    // with nothing on screen.
    await expect(page.getByRole('dialog')).toContainText(
      /Only the shared copy every origin reads is removed/
    )
    await page.getByRole('button', { name: 'Delete it' }).click()

    expect(sent.del).toEqual([KEY])
    await expect(page.getByText(/Nothing is saved on the server for this account yet/)).toBeVisible()
  })

  test('a forget that finds nothing lands on the state it wanted', async ({ page }) => {
    await isolate(page, { get: 'found', del: 'missing' })
    await seed(page)
    await page.goto('/resource-planner')
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    await page.getByRole('button', { name: 'Forget the saved setup' }).click()
    await page.getByRole('button', { name: 'Delete it' }).click()

    // A 404 on delete is not a failure to report as one: the state it wanted
    // to reach is the state it is in. Asserted on the TONE and not only on the
    // words, because the words were already right and sat under
    // `toast.error` -- the page's own comment said "recorded rather than
    // reported as a failure" two lines above the red toast that reported it.
    await expect(page.locator('.toast-success')).toContainText(
      /Nothing was saved on the server, so there was nothing to forget/
    )
    await expect(page.locator('.toast-error')).toHaveCount(0)
    await expect(page.getByText(/Nothing is saved on the server for this account yet/)).toBeVisible()
  })

  test('a check that fails says so, and does not disable the buttons', async ({ page }) => {
    await isolate(page, { get: 'error' })
    await seed(page)
    await page.goto('/resource-planner')

    await expect(page.getByText(/could not say whether a setup is saved/)).toBeVisible()
    // The probe failing is not evidence that nothing is saved, so nothing is
    // taken away: the reason may have gone away since, and refusing the button
    // would leave no way to find out.
    await expect(page.getByRole('button', { name: 'Load setup from server' })).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Forget the saved setup' })).toBeEnabled()
  })
})
