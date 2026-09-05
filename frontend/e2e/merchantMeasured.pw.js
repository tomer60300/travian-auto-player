/**
 * The acknowledgement that the merchant model was read off the game.
 *
 * MERCHANT_MODEL_UNCALIBRATED fires whenever `trade_office_bonus_per_level`
 * still equals the shipped 0.20 and any village has a Trade Office. It is an
 * EQUALITY TEST against the default, so it cannot tell a measured 0.20 from an
 * untouched one -- and an operator who read a Marketplace capacity at two Trade
 * Office levels, found the default right and typed it back got the same warning
 * for ever, asking them to do the thing they had just done.
 *
 * The box is that operator saying they looked. It silences that one finding and
 * changes no number, which is why the `Why` says so out loud: a checkbox beside
 * six figures that quietly moved one of them would be the worse defect.
 *
 * Backend twins: `PlanRequest.merchant_model_measured` and
 * `SetupDocument.merchant_model_measured` in
 * `src/travian_api/web/routes/distribution.py` and
 * `src/travian_api/web/routes/planner_setup.py`.
 *
 * NO BACKEND AND NO GAME REQUEST.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test merchantMeasured
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, KEY, PLAN, PREVIEW, isolate, seed } from './plannerHarness'

/** The label is the accessible name, and it names both figures it covers --
 *  the base capacity on this row and the Trade Office bonus one disclosure in.
 *  A checkbox called "measured" would be an operator asserting they know not
 *  what. */
export const MEASURED = 'I read the base capacity and the bonus off the Marketplace send form'

const measured = (page) => page.getByRole('checkbox', { name: MEASURED })

/** A setup store that remembers what was PUT, so a save and the load after it
 *  are the same document rather than two fixtures that agree by hand. */
async function isolateStore(page) {
  const state = { saved: null, puts: [] }
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    if (!path.endsWith('/distribution/setup')) return undefined
    const method = route.request().method()
    if (method === 'PUT') {
      state.saved = route.request().postDataJSON()
      state.puts.push(state.saved)
      await route.fulfill({ json: { saved_at: '2026-09-05T10:00:00Z' } })
      return 'handled'
    }
    if (method === 'GET') {
      if (state.saved == null) {
        await route.fulfill({
          status: 404,
          json: { detail: 'No planner setup is saved for this account.' },
        })
        return 'handled'
      }
      await route.fulfill({ json: { setup: state.saved, saved_at: '2026-09-05T10:00:00Z' } })
      return 'handled'
    }
    return undefined
  })
  return state
}

/** The Account stage, which carries the World & merchants row. */
async function openAccount(page) {
  await page.goto('/resource-planner')
  await expect(page.getByLabel('Merchant base capacity')).toBeVisible()
}

/** What localStorage holds, which is the half a reload reads back. */
async function stored(page) {
  const raw = await page.evaluate(
    (key) => localStorage.getItem(`planner_merchant_measured::${key}`),
    KEY
  )
  return raw == null ? null : JSON.parse(raw)
}

test.describe('the measured-merchant-model acknowledgement', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('sits with the figures it is about, and says what ticking it does', async ({ page }) => {
    await isolate(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)

    // Unticked on a page nobody has touched: the finding's own default is
    // "never measured", and a box that arrived ticked would silence it for
    // every operator who has not looked.
    await expect(measured(page)).not.toBeChecked()

    // Its reasoning is one click away, like every other field on this page. A
    // native <summary>, so it is reached by its accessible name rather than by
    // a role: a <button> inside a <summary> would be a control inside a
    // control. Same locator `liveRunGuards.pw.js` uses for the run panel's four.
    const why = page.getByLabel(`Why: ${MEASURED}`)
    await expect(why).toHaveCount(1)
    await why.click()
    // The two sentences that matter: what the plan says without it, and that
    // ticking it moves no number.
    await expect(page.getByText(/never measured/i)).toBeVisible()
    await expect(page.getByText(/changes no number/i)).toBeVisible()
  })

  test('survives a reload on the same origin', async ({ page }) => {
    await isolate(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)

    await measured(page).check()
    await expect.poll(() => stored(page)).toBe(true)

    await page.reload()
    await openAccount(page)
    // Back to unticked after a reload is the defect: the operator's reading is
    // work done in the game, and the finding returns the moment it is lost.
    await expect(measured(page)).toBeChecked()
  })

  test('is written into the document, and the version rose for it', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)
    await measured(page).check()

    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()

    expect(store.puts[0].merchant_model_measured).toBe(true)
    // Both halves of the bump, or a fresh export answers 422 "NEWER build".
    expect(store.puts[0].version).toBe(11)
  })

  test('comes back out of the store as the answer that was saved', async ({ page }) => {
    const store = await isolateStore(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)
    await measured(page).check()
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()
    expect(store.saved).not.toBeNull()

    // A different origin is a fresh localStorage -- the failure the storage
    // panel two cards up warns about in words. Simulated by clearing the one
    // key, so the document is the only thing carrying the answer.
    await page.evaluate((key) => localStorage.removeItem(`planner_merchant_measured::${key}`), KEY)
    await page.reload()
    await page.getByRole('button', { name: 'Load setup from server' }).click()
    await expect(page.getByText(/from the saved setup/).first()).toBeVisible()

    await expect(measured(page)).toBeChecked()
  })

  test('a v10 document loads with the box unticked', async ({ page }) => {
    // A build that never wrote the field is not an operator who declined to
    // measure, but the two plan the same -- so the box has to be UNTICKED
    // rather than absent-and-forgotten, or the operator cannot see that the
    // finding is about to return.
    const DOC = {
      format: 'travian-planner-owned-state',
      version: 10,
      exported_at: '2026-09-05T04:00:00Z',
      account: KEY,
      villages: [{ village_id: CAPITAL, name: '02', trade_office_level: 13 }],
    }
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/setup') && route.request().method() === 'GET') {
        return { account_key: KEY, setup: DOC, saved_at: DOC.exported_at }
      }
      return undefined
    })
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Load setup from server' }).click()
    await expect(page.getByText(/from the saved setup/).first()).toBeVisible()

    await expect(measured(page)).not.toBeChecked()
  })

  test('a ticked box is content the empty-document guard counts', async ({ page }) => {
    // `setupDocument()` refuses to write a document with no content, because
    // "you have saved nothing" and "you saved a blank sheet" are different
    // states the server distinguishes. A reading taken in the game is content:
    // it is the one field in the document nothing could re-derive.
    const store = await isolateStore(page)
    await seed(page)
    await openAccount(page)

    // Nothing typed: the guard is reachable, which is what makes the assertion
    // after it mean something.
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/Nothing typed yet/)).toBeVisible()
    expect(store.puts).toHaveLength(0)

    await measured(page).check()
    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect(page.getByText(/A setup is saved on the server/)).toBeVisible()
    expect(store.puts).toHaveLength(1)
    expect(store.puts[0].merchant_model_measured).toBe(true)
  })
})

/** Every planner request, kept by endpoint so ONE page state can be read off
 *  all of them at once. Same shape `pruneCoherence.pw.js` uses, widened to the
 *  two paths that spec does not drive -- the night derivation and the sweep --
 *  because this field rides in `buildPlanPayload`, which all five share.
 */
async function recordBodies(page) {
  const sent = { plan: [], dayCheck: [], night: [], execute: [], revert: [] }
  await isolate(page, async (path, route) => {
    if (path.endsWith('/distribution/plan')) {
      sent.plan.push(route.request().postDataJSON())
      await route.fulfill({ json: PLAN })
      return 'handled'
    }
    if (path.endsWith('/distribution/day-check')) {
      sent.dayCheck.push(route.request().postDataJSON())
      await route.fulfill({
        json: {
          villages: [],
          morning_floor: 0.6,
          pre_night_baseline: 0.25,
          night_overruns: [],
          warnings: [],
        },
      })
      return 'handled'
    }
    if (path.endsWith('/distribution/night-profile')) {
      sent.night.push(route.request().postDataJSON())
      await route.fulfill({ json: { allocations: {}, unmet: {}, notes: [] } })
      return 'handled'
    }
    if (path.endsWith('/distribution/execute')) {
      sent.execute.push(route.request().postDataJSON())
      // `unswept_origins: []` and no `next_chunk_wait_seconds`, so the sweep's
      // loop takes one chunk and stops rather than paging for ever.
      await route.fulfill({ json: { ...PREVIEW, swept_origins: [CAPITAL], unswept_origins: [] } })
      return 'handled'
    }
    if (path.includes('/distribution/run-history')) {
      await route.fulfill({
        json: {
          runs: [
            {
              run_id: 'aaa111bbb222',
              started_at: '2026-09-05T09:00:00Z',
              created: 1,
              created_game_rows: 6,
              live_game_rows: 6,
              disabled: 0,
              complete: true,
              failed: false,
              needs_attention: false,
            },
          ],
          rollup: {
            runs: 1,
            total_created: 1,
            total_problems: 0,
            total_created_unverified: 0,
            failed_runs: 0,
            repeat_problem_villages: [],
          },
        },
      })
      return 'handled'
    }
    if (path.endsWith('/routes/revert-plan')) {
      sent.revert.push(route.request().postDataJSON())
      await route.fulfill({
        json: {
          trace_id: 'aaa111bbb222',
          steps: [],
          created: {},
          disabled_now: {},
          deleted_now: {},
          must_delete_by_hand: {},
          restore_state: {},
          clean: true,
          requests_used: 2,
          problems: [],
        },
      })
      return 'handled'
    }
    return undefined
  })
  return sent
}

/** Both profiles carry hours and an attendance answer, which is what the
 *  whole-day run and the full-day check both refuse to go without. */
const TWO_PROFILES = {
  planner_profiles: { Day: {}, Night: {} },
  planner_profile_windows: { Day: ['07:00', '23:00'], Night: ['23:00', '07:00'] },
  planner_npc_attended: { Day: true, Night: false },
  planner_trade_office: { [CAPITAL]: 13 },
}

const wholeDayBox = (page) =>
  page.getByRole('checkbox', { name: 'Whole day \u2014 execute all profiles at once' })

async function openPlanStage(page) {
  await page.getByRole('button', { name: /^Build plan/ }).click()
  await page.getByRole('button', { name: 'Plan', exact: true }).click()
  await expect(page.getByText(/^Routes$/)).toBeVisible()
}

test.describe('one page state reaches every request that carries the field', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  // `PlanRequest.merchant_model_measured`, and every sibling that INHERITS it:
  // `ExecuteRequest`, `DayCheckRequest`, `NightProfileRequest` and
  // `PlanYamlRequest` are all `class X(PlanRequest)`. So the field rides in
  // `buildPlanPayload` once rather than being spelled at five call sites --
  // which is how `npc_attended` and `overnight` each went missing from one.
  for (const measured of [true, false]) {
    test(`ticked=${measured} reaches plan, day-check, derive, run and sweep`, async ({ page }) => {
      const sent = await recordBodies(page)
      await seed(page, TWO_PROFILES)
      await page.goto('/resource-planner')
      if (measured) await page.getByRole('checkbox', { name: MEASURED }).check()

      await openPlanStage(page)
      await expect.poll(() => sent.plan.length).toBeGreaterThan(0)

      await page.getByRole('button', { name: 'Day & night' }).click()
      await page.getByRole('button', { name: /^Run \(0 requests\)/ }).click()
      await expect.poll(() => sent.dayCheck.length).toBe(1)
      await page.getByRole('button', { name: /^Derive from stores/ }).click()
      await expect.poll(() => sent.night.length).toBe(1)

      // Deriving CLEARS the plan -- it rewrote the allocations the sheet was
      // built from -- so the run panel has to be rebuilt before Preview exists.
      await page.getByRole('button', { name: 'Plan', exact: true }).click()
      await openPlanStage(page)
      await page.getByRole('button', { name: /^Preview/ }).click()
      await expect.poll(() => sent.execute.length).toBe(1)

      // The sweep, which is the one write path that reaches `/execute` without
      // going through Preview -- and posts `dry_run: false`.
      await page.getByRole('button', { name: 'Reconcile all villages' }).click()
      await expect.poll(() => sent.execute.length).toBe(2)

      // And the whole-day run, whose body is `buildExecutePayload`'s stripped
      // and segmented REST rather than the plan payload verbatim.
      await wholeDayBox(page).check()
      await page.getByRole('button', { name: /^Preview/ }).click()
      await expect.poll(() => sent.execute.length).toBe(3)

      const bodies = [
        ['plan', sent.plan.at(-1)],
        ['day-check', sent.dayCheck.at(-1)],
        ['night-profile', sent.night.at(-1)],
        ['preview', sent.execute[0]],
        ['sweep', sent.execute[1]],
        ['whole-day', sent.execute[2]],
      ]
      for (const [name, body] of bodies) {
        expect(body.merchant_model_measured, `${name} carries the acknowledgement`).toBe(measured)
      }
      // The whole-day body is the one that survives a destructuring, so it is
      // worth saying out loud that the field was not stripped along with the
      // four that DO move to the segments.
      expect(sent.execute[2].segments.length).toBe(2)
    })
  }

  test('the undo request does not carry it, because its model has no such field', async ({
    page,
  }) => {
    // `RevertPlanRequest(BaseModel)` -- not `(PlanRequest)`, alone among the
    // request models this page posts. Unknown keys are forbidden, so sending
    // the field there would 422 the one request an operator makes when a live
    // run has already gone wrong.
    const sent = await recordBodies(page)
    await seed(page, TWO_PROFILES)
    await page.goto('/resource-planner')
    await page.getByRole('checkbox', { name: MEASURED }).check()

    await page.getByText(/^Run history/).click()
    await page.getByRole('button', { name: /^Undo this run/ }).first().click()
    await page.getByRole('button', { name: /^Check what undoing this would take/ }).click()
    await expect.poll(() => sent.revert.length).toBe(1)

    expect('merchant_model_measured' in sent.revert[0]).toBe(false)
  })
})

/** The Trade Office bonus lives behind the "Non-Europe-2 world" disclosure,
 *  which is closed unless one of its two figures is refused. Opened directly,
 *  the way `focusRing.pw.js` and `cellPickers.pw.js` open theirs -- clicking
 *  the summary would move focus and is not what this is testing. */
async function openWorldDisclosure(page) {
  await page.evaluate(() => {
    for (const d of document.querySelectorAll('details')) {
      if (d.querySelector('summary')?.textContent?.includes('Non-Europe-2 world')) d.open = true
    }
  })
}

test.describe('editing a measured figure withdraws the acknowledgement', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  // A reading that is then typed over is not a reading. The box says the two
  // figures WERE READ OFF THE GAME; the moment one of them is a different
  // number, that sentence is false about what is on screen -- and it would go
  // on silencing MERCHANT_MODEL_UNCALIBRATED about a slope nobody measured.
  test('changing the base capacity unticks it, and the next plan says so', async ({ page }) => {
    const sent = await recordBodies(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)
    await measured(page).check()
    await expect(measured(page)).toBeChecked()

    await page.getByLabel('Merchant base capacity').fill('3200')
    await expect(measured(page)).not.toBeChecked()

    await openPlanStage(page)
    await expect.poll(() => sent.plan.length).toBeGreaterThan(0)
    expect(sent.plan.at(-1).merchant_model_measured).toBe(false)
    // And the figure the operator typed did go, so this is a withdrawal rather
    // than a rejected edit.
    expect(sent.plan.at(-1).merchant_base_capacity).toBe(3200)
  })

  test('changing the Trade Office bonus unticks it too', async ({ page }) => {
    // The bonus is the figure the finding actually tests -- "still equal to the
    // shipped 0.20" -- so an acknowledgement surviving an edit to THIS one
    // would silence the finding about a number nobody read.
    await isolate(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)
    await measured(page).check()
    await openWorldDisclosure(page)

    await page.getByLabel('Trade Office bonus per level').fill('0.25')
    await expect(measured(page)).not.toBeChecked()
  })

  test('the four levers it does not describe leave it alone', async ({ page }) => {
    // The reserve and the headroom are the operator's own POLICY, not readings;
    // the map span and the merchant speed are properties of the world and have
    // nothing to do with what one merchant carries. Unticking on those would
    // train the operator to re-tick a box that means nothing.
    await isolate(page)
    await seed(page, { planner_trade_office: { [CAPITAL]: 13 } })
    await openAccount(page)
    await measured(page).check()
    await openWorldDisclosure(page)

    await page.getByLabel('Merchants held in reserve at every village').fill('2')
    await page.getByLabel("Merchant headroom, percent of each village's budget").fill('15')
    await page.getByLabel('Merchant speed fields per hour override').fill('20')
    await page.getByLabel('Map span override').fill('801')

    await expect(measured(page)).toBeChecked()
  })

  test('a loaded document is not an operator typing, so it does not untick', async ({ page }) => {
    // `mergeParsedSetup` writes the merchant model with `setMerchantModel`
    // directly rather than through the boxes' own handlers, which is what keeps
    // a saved acknowledgement from being withdrawn by the very load that
    // restored it. A document carrying both must come back with both.
    const DOC = {
      format: 'travian-planner-owned-state',
      version: 11,
      exported_at: '2026-09-05T09:00:00Z',
      account: KEY,
      villages: [{ village_id: CAPITAL, name: '02', trade_office_level: 13 }],
      merchant_model_measured: true,
      merchant_model: { base_capacity: 3200, bonus_per_to_level: 0.25 },
    }
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/setup') && route.request().method() === 'GET') {
        return { account_key: KEY, setup: DOC, saved_at: DOC.exported_at }
      }
      return undefined
    })
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Load setup from server' }).click()
    await expect(page.getByText(/from the saved setup/).first()).toBeVisible()

    await expect(page.getByLabel('Merchant base capacity')).toHaveValue('3200')
    await expect(measured(page)).toBeChecked()
  })
})
