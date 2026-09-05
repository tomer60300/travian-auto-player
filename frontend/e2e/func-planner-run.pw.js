/**
 * The distribution planner's run panel -- "Write it to the game" -- which is
 * the only surface in this app that creates trade routes on a real account.
 *
 * Everything here is about the gap between what the operator set and what
 * `POST /distribution/execute` receives, because a route created with the
 * wrong cap or the wrong mode is a real in-game write that has to be undone by
 * hand. So: does the preview carry the controls, does the result panel report
 * the server's counts rather than the click's optimism, and does a refusal
 * survive on the page.
 *
 * The canary tick gets one test here covering the three things it promises --
 * it presets and locks the controls, it names the condition holding the red
 * button, and a satisfied one sends the flag. `canaryRun.pw.js` owns the rest
 * of it (the undo list, the keyboard path, the 375px tap target); this is the
 * functional spine, kept alongside the two ordinary run flows it sits between.
 *
 * NO BACKEND AND NO GAME REQUEST: `plannerHarness.isolate` answers the shell
 * and ABORTS everything else, and the snapshot is seeded into localStorage
 * rather than fetched. There is a live Travian account on this machine.
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, DEF_A, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

const CANARY = 'Canary: one route, create only'

/** The harness plan ships ONE route, 02 -> 11, on a 4h cycle. 24 / 4 = 6 rows. */
const FAN_OUT = 6

function toast(page) {
  return page.locator('.toast').first()
}

/** Captures every `/distribution/execute` body and answers it by mode. */
async function arrive(page, { live = null, refusePreview = null, refuseLive = null } = {}) {
  const sent = []
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    const body = route.request().postDataJSON()
    sent.push(body)
    if (body.execution_mode !== 'live') {
      if (refusePreview) {
        await route.fulfill({ status: 502, json: { detail: refusePreview } })
        return 'handled'
      }
      await route.fulfill({ json: PREVIEW })
      return 'handled'
    }
    if (refuseLive) {
      await route.fulfill({ status: 422, json: { detail: refuseLive } })
      return 'handled'
    }
    await route.fulfill({ json: live })
    return 'handled'
  })
  await seed(page)
  await openPlan(page)
  return sent
}

test.describe('the run panel', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('a preview carries the controls that were set, and prints the server’s forecast', async ({
    page,
  }) => {
    const sent = await arrive(page)

    // ── SET THE CONTROLS ──────────────────────────────────────────────
    await page.getByRole('spinbutton', { name: 'Routes this run' }).fill('2')
    await page.getByRole('spinbutton', { name: 'Max rows this run' }).fill('18')
    await page.getByRole('spinbutton', { name: 'Only origin (village id)' }).fill(String(CAPITAL))
    // Both are moved off their defaults -- `disable_existing` starts TRUE and
    // `update_drifted` FALSE -- so each assertion below proves the control
    // travelled rather than that the default happened to match.
    await page
      .getByRole('checkbox', { name: /^Also disable routes the plan no longer wants/ })
      .uncheck()
    await page.getByRole('checkbox', { name: /^Correct cargo on routes that have drifted/ }).check()

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()

    // 1. THE REQUEST. Each of these bounds a real write: the route cap and the
    //    row cap bound how much of the game this run may touch, `only_origins`
    //    narrows it to one village, and `disable_existing` decides whether the
    //    origin's OLD routes get switched off before the new ones go in.
    await expect.poll(() => sent.length).toBe(1)
    const body = sent[0]
    expect(body.execution_mode).toBe('preview')
    expect(body.max_routes_per_run).toBe(2)
    expect(body.max_game_rows_per_run).toBe(18)
    expect(body.only_origins).toEqual([CAPITAL])
    expect(body.disable_existing).toBe(false)
    expect(body.update_drifted).toBe(true)
    // A preview writes nothing, so it must never carry the canary flag -- the
    // validator refuses it without a live mode.
    expect(body).not.toHaveProperty('canary')

    // 2. THE PANEL prints the server's own forecast, not a local guess: how
    //    many routes WOULD be created, and what the run would cost in requests.
    await expect(page.getByText(/1 route\(s\) would be created/)).toBeVisible()
    // The cost, in the server's own two figures (`estimated_total` and
    // `estimated_total_max`) rather than a number the page worked out.
    await expect(
      page.getByText(
        `Going live spends ~${PREVIEW.requests_forecast.estimated_total}–${PREVIEW.requests_forecast.estimated_total_max} requests`
      )
    ).toBeVisible()
  })

  test('a live run reports the server’s counts, and a failed create is not a clean success', async ({
    page,
  }) => {
    const LIVE = {
      ...PREVIEW,
      dry_run: false,
      created: 1,
      not_created: 1,
      remaining: 2,
      actions: [
        { ...PREVIEW.actions[0], status: 'created', detail: 'route 9001' },
        { ...PREVIEW.actions[0], status: 'failed', detail: 'the game rejected the cargo' },
      ],
      trace_id: 'e2e-live',
    }
    const sent = await arrive(page, { live: LIVE })

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)
    // `disableExisting` starts TRUE, so the red button carries the disabling
    // wording here; the canary test below is the one that turns it off.
    await page.getByRole('button', { name: /^Disable old routes & create 1/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect.poll(() => sent.length).toBe(2)
    expect(sent[1].execution_mode).toBe('live')

    // 2. THE PANEL states all three outcomes the server distinguishes.
    //    `created` counts only CONFIRMED creates, so on its own it read
    //    "Created 0 route(s)" over a problem list saying three had just been
    //    written -- a headline refuting its own detail.
    //    Asserted as the one whole sentence the panel prints, because the
    //    toast beside it opens with the same five words.
    await expect(
      page.getByText(
        'Created 1 route(s), 1 accepted by the game but never appeared, 2 deferred to a later run.'
      )
    ).toBeVisible()

    // 3. AND THE TONE IS THE FAILURE'S. One create failed, so the green count
    //    must not be the last word: an operator who reads "Created 1" and
    //    leaves has a plan that is one route short and does not know it.
    await expect(toast(page)).toHaveClass(/toast-error/)
    await expect(toast(page)).toContainText('a create failed')
  })

  test('a preview the server refused leaves its reason on the page', async ({ page }) => {
    const REASON = 'the marketplace read timed out; nothing was forecast'
    const sent = await arrive(page, { refusePreview: REASON })

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)

    // The refusal is received and shown -- for four seconds.
    await expect(toast(page)).toContainText(REASON)

    // `executePlan` (src/pages/ResourcePlanner.jsx) writes the refusal to the
    // page only for a LIVE canary run (`if (!dryRun && canary)
    // setCanaryRefusal(detail)`); a refused PREVIEW gets a toast and nothing
    // else, and `execResult` keeps whatever it held before. So the operator
    // who looks away for five seconds sees a run panel that never mentions the
    // attempt -- the exact defect `FetchError` was introduced across the other
    // pages to close, on the one page whose next click writes to a real
    // account.
    //
    // What it should do: keep the refusal on the run panel until the next
    // attempt clears it, the way the canary refusal already does.
    await expect(page.getByRole('alert').filter({ hasText: REASON })).toBeVisible({
      timeout: 2000,
    })
  })

  test('the canary presets the controls, names what holds it, and sends the flag', async ({
    page,
  }) => {
    const LIVE = {
      ...PREVIEW,
      dry_run: false,
      created: 1,
      actions: [{ ...PREVIEW.actions[0], status: 'created', detail: 'route 9001' }],
      canary_rows_created: [9001, 9002, 9003, 9004, 9005, 9006],
    }
    const sent = await arrive(page, { live: LIVE })
    const tick = page.getByRole('checkbox', { name: CANARY })

    // ── (a) IT PRESETS AND LOCKS ──────────────────────────────────────
    // Every value the server's validator refuses anything else for, set for
    // the operator rather than asked of them -- and locked, because a control
    // that can drift out from under a stated intent is the typo the flag
    // exists to remove.
    await tick.check()
    const routes = page.getByRole('spinbutton', { name: 'Routes this run' })
    await expect(routes).toHaveValue('1')
    await expect(routes).toBeDisabled()
    for (const name of [
      /^Also disable routes the plan no longer wants/,
      /^Correct cargo on routes that have drifted/,
    ]) {
      const box = page.getByRole('checkbox', { name })
      await expect(box).not.toBeChecked()
      await expect(box).toBeDisabled()
    }

    // ── (b) THE HELD BUTTON NAMES THE CONDITION ───────────────────────
    // Greying a button is not a reason. With no destination chosen the run is
    // not the smallest possible one, and the page must say so where the
    // operator is looking.
    await page.getByRole('spinbutton', { name: 'Only origin (village id)' }).fill(String(CAPITAL))
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => sent.length).toBe(1)
    const go = page.getByRole('button', { name: /^Create 1 route, disable nothing/ })
    await expect(go).toBeDisabled()
    await expect(
      page.getByText(/Held by Canary: one route, create only: Only destination/)
    ).toBeVisible()

    // ── (c) A SATISFIED CANARY SENDS THE FLAG ─────────────────────────
    await page
      .getByRole('spinbutton', { name: 'Only destination (village id)' })
      .fill(String(DEF_A))
    // The row budget is computed off the plan's own route -- 24 / 4h -- not off
    // a default. "One route" was never "one row".
    await expect(page.getByRole('spinbutton', { name: 'Max rows this run' })).toHaveValue(
      String(FAN_OUT)
    )
    await go.click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect.poll(() => sent.length).toBe(2)

    const body = sent[1]
    expect(body.canary).toBe(true)
    expect(body.execution_mode).toBe('live')
    expect(body.only_origins).toEqual([CAPITAL])
    expect(body.only_destinations).toEqual([DEF_A])
    expect(body.max_routes_per_run).toBe(1)
    expect(body.disable_existing).toBe(false)
    expect(body.max_game_rows_per_run).toBe(FAN_OUT)

    // And the undo list the flag exists to produce is on screen afterwards.
    await expect(page.getByText('Rows this canary put in the game (the undo list):')).toBeVisible()
  })
})
