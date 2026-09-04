/**
 * Section 10's order, end to end: readable plan first → operator confirms →
 * then the YAML is generated.
 *
 * The confirmation is not a dialog. It is the DIGEST: `/plan` returns
 * `plan_digest` over the response it displayed, `/plan/yaml` demands it back,
 * and a mismatch is a **409 naming both digests** with no document rendered.
 * This spec drives both halves, and the 409 half is the one worth having --
 * the failure mode it guards against is a download that quietly succeeds
 * against a re-planned account, handing the operator an authoritative-looking
 * file describing a plan nobody read.
 *
 * So the assertions are: the digest the request carries is the digest the plan
 * on screen showed; a 200 hands over a file named as the server asked; a 409
 * renders no file, says plainly that the plan moved, names both digests, and
 * makes EXACTLY ONE request -- no silent retry.
 *
 * NO BACKEND AND NO GAME REQUEST: every `/api` call is answered here or
 * ABORTED, and the snapshot is seeded into localStorage rather than fetched.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test planExport
 */

import { expect, test } from '@playwright/test'

const SERVER = 'https://ts2.x1.europe.travian.com'
const PLAYER = 'e2e-operator'
const KEY = `${SERVER}|${PLAYER}`

const CAPITAL = 60002
const DIGEST = 'ab12cd34ef56'.repeat(5) + 'abcd'
const MOVED = 'ff00'.repeat(16)

const SNAPSHOT = {
  villages: [
    {
      village_id: CAPITAL,
      name: '02',
      x: 0,
      y: 0,
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
    },
  ],
  map_span: 401,
  speed_fields_per_hour: 16,
  requests_used: 0,
  warnings: [],
}

const PLAN = {
  rows: [],
  budgets: [],
  shortfalls: [],
  unallocated: [],
  total_merchants: 0,
  feasible: true,
  verdict: {
    executable: true,
    clean: true,
    blockers: [],
    covers: ['every merchant budget'],
    unweighed: [],
    critical_findings: 0,
  },
  relays: [],
  role_deviations: [],
  village_nets: [],
  night_overruns: [],
  npc_reserves: [],
  npc_triggers: [],
  warnings: [],
  diagnostics: {
    headline: 'Nothing to report.',
    total_loss_per_day: 0,
    loss_by_resource: [],
    groups: [],
    counts: { critical: 0, warning: 0, note: 0 },
  },
  plan_digest: DIGEST,
}

const YAML = `# distribution plan\ndigest: ${DIGEST}\nroutes: []\n`

/**
 * @param {'ok'|'conflict'} yamlOutcome what `/plan/yaml` answers with
 */
async function isolate(page, yamlOutcome = 'ok') {
  const sent = { plan: [], yaml: [] }
  await page.routeWebSocket(/.*/, (ws) => ws.close())
  await page.route('**/api/**', (route) => {
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
          villages: [{ id: CAPITAL, name: '02' }],
        },
      })
    }
    if (path.endsWith('/distribution/plan/yaml')) {
      sent.yaml.push(route.request().postDataJSON())
      if (yamlOutcome === 'conflict') {
        return route.fulfill({
          status: 409,
          json: {
            detail:
              `this plan is not the one that was confirmed: the request re-plans to ${MOVED} ` +
              `and the confirmation names ${DIGEST}. Nothing was rendered -- a YAML file ` +
              `describing a plan nobody read is worse than no file. Re-read /plan and confirm ` +
              `the digest it returns.`,
          },
        })
      }
      return route.fulfill({
        status: 200,
        body: YAML,
        headers: {
          'content-type': 'application/yaml',
          // Named for the PLAN and not for the moment, so two downloads of one
          // plan are one file. The page must respect this rather than
          // reconstructing it.
          'content-disposition': `attachment; filename="distribution-plan-${DIGEST.slice(0, 12)}.yaml"`,
          'x-plan-digest': DIGEST,
        },
      })
    }
    if (path.endsWith('/distribution/plan')) {
      sent.plan.push(route.request().postDataJSON())
      return route.fulfill({ json: PLAN })
    }
    return route.abort('blockedbyclient')
  })
  return sent
}

async function seed(page) {
  await page.addInitScript(
    ([key, snap]) => {
      localStorage.setItem('token', 'e2e-not-a-real-token')
      localStorage.setItem(`planner_snapshot::${key}`, JSON.stringify(snap))
      localStorage.setItem(`planner_snapshot_at::${key}`, JSON.stringify(Date.now()))
      localStorage.setItem(`planner_profiles::${key}`, JSON.stringify({ Always: {} }))
      localStorage.setItem(`planner_profile_windows::${key}`, JSON.stringify({}))
      // A typed Trade Office level, so the request has a config ROW to be
      // asserted about below. A village with nothing typed no longer gets one:
      // the row said only "this village exists", which the snapshot already
      // says, and while it was sent the backend's calibration filter -- which
      // narrows its level-0 sample to villages with a config row -- was a
      // tautology. See `tradeOfficeUnknown.pw.js`.
      localStorage.setItem(
        `planner_trade_office::${key}`,
        JSON.stringify({ [snap.villages[0].village_id]: 13 })
      )
    },
    [KEY, SNAPSHOT]
  )
}

async function buildPlan(page) {
  await page.goto('/resource-planner')
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

/** Open the export disclosure.
 *
 * It is a disclosure now, at the bottom of the stage. Its "Confirm this plan
 * and export YAML" used to be the biggest filled button on the page -- for a
 * document that changes nothing in the game -- above the small button that
 * writes to a real account. Section 10's order is unchanged and is still
 * enforced by the digest; only the weight moved.
 */
async function openExport(page) {
  await page.getByText(/^Export this plan as YAML/).click()
  await expect(page.getByText(/Confirm this plan, then export it/)).toBeVisible()
}

test.describe('confirm, then export', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('the plan names its own identity, in the digest the file will carry', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await buildPlan(page)

    // The first twelve hex characters, which is exactly what the server's own
    // filename uses -- so the figure on screen and the figure in the file name
    // are the same string. In the disclosure's SUMMARY, which is where it is
    // most useful: twelve characters that say which of three downloads
    // describes which plan, readable without opening anything.
    await expect(page.getByText(DIGEST.slice(0, 12)).first()).toBeVisible()
  })

  test('confirming sends the digest back, and the file is named as asked', async ({ page }) => {
    const sent = await isolate(page)
    await seed(page)
    await buildPlan(page)
    await openExport(page)

    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: /Confirm this plan/ }).click()
    const file = await download

    expect(sent.yaml).toHaveLength(1)
    // The confirmation step itself, in machine-readable form.
    expect(sent.yaml[0].expected_plan_digest).toBe(DIGEST)
    // And the rest of the body is the plan request verbatim, so the file the
    // server renders describes the plan that was read.
    expect(sent.yaml[0].snapshot).toHaveLength(1)
    expect(sent.yaml[0].config[0].village_id).toBe(CAPITAL)
    expect(sent.yaml[0].config[0].trade_office_level).toBe(13)

    expect(file.suggestedFilename()).toBe(`distribution-plan-${DIGEST.slice(0, 12)}.yaml`)
  })

  test('a plan that moved is refused, named, and never retried', async ({ page }) => {
    const sent = await isolate(page, 'conflict')
    await seed(page)
    await buildPlan(page)
    await openExport(page)

    await page.getByRole('button', { name: /Confirm this plan and export/ }).click()

    // Said in the page, not only in a toast: this needs an action, and a toast
    // is gone before the operator has decided what to do about it.
    await expect(page.getByText(/The plan moved since you read it/i)).toBeVisible()
    // Both digests, because "it moved" without saying from what to what is not
    // something anyone can check.
    await expect(page.getByText(MOVED, { exact: false })).toBeVisible()

    // Exactly one attempt. Re-planning to make the download succeed would hand
    // over a file describing a plan nobody read.
    expect(sent.yaml).toHaveLength(1)
    expect(sent.plan).toHaveLength(1)
  })

  test('the way out of a conflict is re-reading the plan, and it clears', async ({ page }) => {
    const sent = await isolate(page, 'conflict')
    await seed(page)
    await buildPlan(page)
    await openExport(page)
    await page.getByRole('button', { name: /Confirm this plan and export/ }).click()
    await expect(page.getByText(/The plan moved since you read it/i)).toBeVisible()

    await page.getByRole('button', { name: /Re-read the plan/ }).click()

    await expect(page.getByText(/The plan moved since you read it/i)).toHaveCount(0)
    expect(sent.plan).toHaveLength(2)
    // Still one export attempt: the re-read is a plan call, not a second
    // confirmation.
    expect(sent.yaml).toHaveLength(1)
  })
})
