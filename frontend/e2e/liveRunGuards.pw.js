/**
 * The guards on a live write, DRIVEN — what the default run authorises, and
 * what the page says about what was typed.
 *
 * Each case here is a defect the audit reached by driving the real page. They
 * share one surface (the Plan stage's controlled-run bar) and one property: the
 * page has every fact it needs to stop the operator being surprised, and was
 * not using it.
 *
 * Asserted on REQUEST BODIES and on rendered warnings, never on a box's own
 * value: a box showing the right number confirms React re-rendered without
 * confirming what it sends.
 *
 * NO BACKEND AND NO GAME REQUEST: `plannerHarness.isolate` answers what the
 * shell asks for and aborts everything else fail-closed, and the only execute
 * path driven is the PREVIEW, which is `dry_run: true` and mocked besides.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test liveRunGuards
 */

import { expect, test } from '@playwright/test'

import { KEY, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** Drive a preview and hand back the body `/distribution/execute` was sent. */
async function previewBody(page) {
  let body = null
  await isolate(page, (path, route) => {
    if (path.endsWith('/distribution/execute')) {
      body = route.request().postDataJSON()
      return PREVIEW
    }
    return undefined
  })
  await seed(page)
  await openPlan(page)
  await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
  await expect(page.getByText(/route\(s\) would be created/)).toBeVisible()
  return body
}

test.describe('the row cap the page explains is on by default', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('a default run bounds ROWS, not only routes', async ({ page }) => {
    // The defect: `max_routes_per_run` defaulted to 3 and
    // `max_game_rows_per_run` was absent, so the server's own default of 0 --
    // unbounded -- applied. Travian turns one "repeat every N hours" request
    // into 24/N daily rows, so three routes on a one-hour cycle is
    // seventy-two rows, and the page's own copy calls a row "the unit the
    // operator actually authorises".
    const body = await previewBody(page)

    expect(body.max_routes_per_run).toBe(3)
    expect(
      body.max_game_rows_per_run,
      'the row cap travels rather than falling through to the server default of unbounded',
    ).toBeGreaterThan(0)
  })

  test('the box says what blank means, in the box', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    const box = page.getByLabel('Max rows this run')
    // A default the operator can see and change, not an empty box over a
    // sentence in a label.
    await expect(box).not.toHaveValue('')
    await expect(box).toHaveAttribute('placeholder', 'no limit')
  })

  test('clearing the box still means no limit', async ({ page }) => {
    // Blank has to stay unbounded. The cap is a default, not a new floor: an
    // operator who deliberately wants a whole-day provisioning pass must be
    // able to say so, and the way they say it is by emptying the box.
    let body = null
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/execute')) {
        body = route.request().postDataJSON()
        return PREVIEW
      }
      return undefined
    })
    await seed(page)
    await openPlan(page)
    await page.getByLabel('Max rows this run').fill('')
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect(page.getByText(/route\(s\) would be created/)).toBeVisible()

    expect(body).not.toBeNull()
    expect('max_game_rows_per_run' in body).toBe(false)
  })
})

test.describe('a protect_destinations typo is undetectable by the server', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  test('a bare integer no village has is named back, with the coordinate reading', async ({
    page,
  }) => {
    // Driven before the fix: typing 4688 sent `protect_destinations: ["4688"]`
    // with no inline warning at all. It is shape-valid as a village id, so the
    // server's `_protected_entries_are_parseable` passes it -- and the server
    // cannot do better, because it does not hold this account's village list.
    await isolate(page)
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable').fill('4688')
    await expect(page.getByText('no village named 4688 — did you mean 46|88?')).toBeVisible()
  })

  test('coordinates are never flagged, however foreign', async ({ page }) => {
    // The routes worth protecting are the hand-made ones to targets this
    // account does not own, so a pair matching no village is the normal case.
    await isolate(page)
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable').fill('46|133')
    await expect(page.getByText(/no village named/)).toHaveCount(0)
  })

  test('a real village id is left alone', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable').fill('20011')
    await expect(page.getByText(/no village named/)).toHaveCount(0)
  })

  test('the warning does not block the run, because the entry may be right', async ({ page }) => {
    // A foreign village id pasted from a Travian link is legitimate and is not
    // in the snapshot. So this is a warning and not a gate -- the same call the
    // foreign-target exclusion field makes.
    let body = null
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/execute')) {
        body = route.request().postDataJSON()
        return PREVIEW
      }
      return undefined
    })
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable').fill('4688')
    await expect(page.getByText(/no village named 4688/)).toBeVisible()
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect(page.getByText(/route\(s\) would be created/)).toBeVisible()

    expect(body.protect_destinations).toEqual(['4688'])
  })
})

test.describe('the gate on a live write is in the app, not the browser chrome', () => {
  test.use({ viewport: { width: 1440, height: 1200 } })

  /** Every native dialog the page opens, which must be none. */
  function watchNativeDialogs(page) {
    const seen = []
    page.on('dialog', (dialog) => {
      seen.push(`${dialog.type()}: ${dialog.message().slice(0, 60)}`)
      dialog.dismiss()
    })
    return seen
  }

  test('the live-run manifest is an in-app dialog', async ({ page }) => {
    // `window.confirm` renders unstyled and theme-blind, cannot be re-read
    // after dismissal, and -- the part that matters -- Chrome's "Prevent this
    // page from creating additional dialogs" makes every later `confirm()`
    // return false SILENTLY. The live button then does nothing, with no
    // explanation, on the one action that writes to a real account.
    const natives = watchNativeDialogs(page)
    await isolate(page, (path) => (path.endsWith('/distribution/execute') ? PREVIEW : undefined))
    await seed(page)
    await openPlan(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()

    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    expect(natives, 'no native dialog was opened').toEqual([])
    // The manifest's own words, which are good and are kept.
    await expect(dialog).toContainText('Execute this plan against Travian now?')
    await expect(dialog).toContainText(/Create up to 1 new route/)
    await expect(dialog).toContainText(
      /If a create fails after a disable, old routes can remain disabled/,
    )
  })

  test('the manifest can be re-read after it is dismissed', async ({ page }) => {
    // The other thing a native dialog cannot do. Once dismissed its text is
    // gone, so an operator who wanted to check a count had to press the
    // irreversible button again to see it.
    await isolate(page, (path) => (path.endsWith('/distribution/execute') ? PREVIEW : undefined))
    await seed(page)
    await openPlan(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)

    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await expect(page.getByRole('dialog')).toContainText('Execute this plan against Travian now?')
  })

  test('cancelling writes nothing', async ({ page }) => {
    let executes = 0
    await isolate(page, (path) => {
      if (path.endsWith('/distribution/execute')) {
        executes += 1
        return PREVIEW
      }
      return undefined
    })
    await seed(page)
    await openPlan(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    expect(executes).toBe(1)
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    expect(executes, 'cancelling sent no second execute').toBe(1)
  })

  test('deleting a profile asks in the app', async ({ page }) => {
    const natives = watchNativeDialogs(page)
    await isolate(page)
    await seed(page, { planner_profiles: { Day: {}, Night: {} } })
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Delete' }).click()
    await expect(page.getByRole('dialog')).toContainText('Day')
    expect(natives).toEqual([])
  })

  test('naming a new profile asks in the app', async ({ page }) => {
    // `window.prompt` is suppressed by the same Chrome setting, and a
    // suppressed prompt returns null -- so "+ New" silently does nothing.
    const natives = watchNativeDialogs(page)
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: '+ New' }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    expect(natives).toEqual([])

    await dialog.getByRole('textbox').fill('Night')
    await dialog.getByRole('button', { name: /^(Create|Add|Confirm)/ }).click()
    await expect(page.getByLabel('Allocation profile')).toHaveValue('Night')
  })
})

test.describe('the undo for a live run is reachable, and the app keeps its key', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  /** What a LIVE run answers with. `trace_id` is the handle the undo needs. */
  const LIVE = {
    ...PREVIEW,
    dry_run: false,
    created: 1,
    created_game_rows: 6,
    actions: [{ ...PREVIEW.actions[0], status: 'created', detail: 'route 9001' }],
  }

  /** The read-only answer: what undoing would take, having changed nothing. */
  const REVERT_READONLY = {
    trace_id: LIVE.trace_id,
    steps: [
      'village 20002: 1 route was created by this run',
      'village 20002: delete route 9001',
    ],
    created: { 20002: [9001] },
    disabled_now: {},
    deleted_now: {},
    must_delete_by_hand: { 20002: [9001] },
    restore_state: { 20002: ['route 8800 -> enabled'] },
    clean: false,
    requests_used: 2,
    problems: [],
  }

  /** Drive a live run to completion, so the last-run panel exists. */
  async function afterLiveRun(page) {
    const bodies = []
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/execute')) {
        return route.request().postDataJSON().dry_run ? PREVIEW : LIVE
      }
      if (path.endsWith('/routes/revert-plan')) {
        bodies.push(route.request().postDataJSON())
        return REVERT_READONLY
      }
      return undefined
    })
    await seed(page)
    await openPlan(page)
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect(page.getByText(/^Last live trade-route run/)).toBeVisible()
    return bodies
  }

  /** Open the last-run panel and then its undo disclosure. */
  async function openUndo(page) {
    await page.getByText(/^Last live trade-route run/).click()
    await page.getByText(/^Undo the last live run/).click()
  }

  test('the trace id is persisted with the run it identifies', async ({ page }) => {
    // `ExecuteResponse.trace_id` is real and `grep -n "trace_id" frontend/src`
    // returned zero hits: the app received the handle to its own undo and threw
    // it away, so a run that wrote 72 game rows had no in-app path back.
    await afterLiveRun(page)

    const stored = await page.evaluate(
      (key) => JSON.parse(localStorage.getItem(`planner_last_live_run::${key}`)),
      KEY,
    )
    expect(stored.traceId).toBe(LIVE.trace_id)
  })

  test('the read-only check goes first, and changes nothing', async ({ page }) => {
    const bodies = await afterLiveRun(page)

    await openUndo(page)
    await page.getByRole('button', { name: /^Check what undoing this would take/ }).click()
    await expect(page.getByText(/must be deleted by hand/i)).toBeVisible()

    expect(bodies).toHaveLength(1)
    expect(bodies[0].trace_id).toBe(LIVE.trace_id)
    expect(bodies[0].apply_disable).toBe(false)
    expect(bodies[0].apply_delete).toBe(false)
  })

  test('what a human has to remove is the prominent part', async ({ page }) => {
    // `must_delete_by_hand` is the half no button covers. Buried, it is the one
    // thing an operator would leave undone.
    await afterLiveRun(page)

    await openUndo(page)
    await page.getByRole('button', { name: /^Check what undoing this would take/ }).click()

    await expect(page.getByText(/9001/).first()).toBeVisible()
    await expect(page.getByText(/route 8800 -> enabled/)).toBeVisible()
    await expect(page.getByText(/2 game request/)).toBeVisible()
  })

  test('the two apply toggles are offered only after the check, and ask first', async ({
    page,
  }) => {
    const bodies = await afterLiveRun(page)

    await openUndo(page)
    // Nothing to apply until the read-only answer says what there is.
    await expect(page.getByRole('button', { name: /^Disable those routes now/ })).toHaveCount(0)

    await page.getByRole('button', { name: /^Check what undoing this would take/ }).click()
    await page.getByRole('button', { name: /^Disable those routes now/ }).click()
    await expect(page.getByRole('dialog')).toBeVisible()
    await page.getByRole('button', { name: /^Disable them/ }).click()

    expect(bodies).toHaveLength(2)
    expect(bodies[1].apply_disable).toBe(true)
    expect(bodies[1].apply_delete).toBe(false)
  })

  test('deleting is its own opt-in, because it cannot be undone', async ({ page }) => {
    const bodies = await afterLiveRun(page)

    await openUndo(page)
    await page.getByRole('button', { name: /^Check what undoing this would take/ }).click()
    await page.getByRole('button', { name: /^Delete those routes for good/ }).click()
    await page.getByRole('button', { name: /^Delete them/ }).click()

    expect(bodies).toHaveLength(2)
    // Disable comes with it: the endpoint disables before deleting, so the
    // routes stop shipping even if the removal then fails.
    expect(bodies[1].apply_disable).toBe(true)
    expect(bodies[1].apply_delete).toBe(true)
  })

  test('the must-delete box is measurably tinted and bordered', async ({ page }) => {
    // The claim in "prominent" is visual, so it is measured. `.card-danger` is
    // the only tinted, bordered surface on the page; if it computed to the same
    // background as the card behind it, the box would be a heading in a wall of
    // 8pt prose -- which is the defect the whole item is about.
    await afterLiveRun(page)

    await openUndo(page)
    await page.getByRole('button', { name: /^Check what undoing this would take/ }).click()
    await expect(page.getByText(/must be deleted by hand/i)).toBeVisible()

    const box = await page.locator('.card-danger').first().evaluate((el) => {
      const cs = getComputedStyle(el)
      const card = getComputedStyle(el.closest('.card'))
      return {
        background: cs.backgroundColor,
        borderWidth: cs.borderTopWidth,
        borderColor: cs.borderTopColor,
        radius: cs.borderTopLeftRadius,
        cardBackground: card.backgroundColor,
      }
    })
    if (globalThis.process?.env?.MEASURE) console.log('\n.card-danger:', JSON.stringify(box))

    expect(parseFloat(box.borderWidth)).toBe(1)
    // --md-error, light theme.
    expect(box.borderColor).toBe('rgb(186, 26, 26)')
    expect(box.radius).toBe('12px')
    // Derived from --md-error-container over --bg-card, so it is not the card.
    expect(box.background).not.toBe(box.cardBackground)
    expect(box.background).not.toMatch(/^(transparent|rgba\(0, 0, 0, 0\))$/)
  })

  test('a run recorded before the app kept trace ids says so', async ({ page }) => {
    // The honest state for an existing record, rather than a button that 404s.
    await isolate(page)
    await seed(page, {
      planner_last_live_run: {
        at: new Date().toISOString(),
        created: 1,
        problems: [],
        disables: [],
        routes: [{ from: '02', to: '11', at: '4|0', status: 'created', detail: '' }],
      },
    })
    await page.goto('/resource-planner')

    await page.getByText(/^Last live trade-route run/).click()
    await expect(page.getByText(/kept the run's trace id/)).toBeVisible()
    await expect(page.getByText(/^Undo the last live run/)).toHaveCount(0)
  })

  test('a past run in the history can reach its own undo', async ({ page }) => {
    // `run_id` on a /run-history row is the same identifier, so the undo is not
    // limited to whichever run this browser happens to have recorded.
    const bodies = []
    await isolate(page, (path, route) => {
      if (path.includes('/distribution/run-history')) {
        return {
          runs: [
            {
              run_id: 'aaa111bbb222',
              started_at: new Date().toISOString(),
              created: 2,
              created_game_rows: 12,
              disabled: 0,
              complete: true,
              failed: false,
              needs_attention: false,
            },
          ],
          rollup: {
            runs: 1,
            total_created: 2,
            total_problems: 0,
            total_created_unverified: 0,
            failed_runs: 0,
            repeat_problem_villages: [],
          },
        }
      }
      if (path.endsWith('/routes/revert-plan')) {
        bodies.push(route.request().postDataJSON())
        return { ...REVERT_READONLY, trace_id: 'aaa111bbb222' }
      }
      return undefined
    })
    await seed(page)
    await page.goto('/resource-planner')

    await page.getByText(/^Run history/).click()
    await page.getByRole('button', { name: /^Undo this run/ }).first().click()
    await page.getByRole('button', { name: /^Check what undoing this would take/ }).click()

    expect(bodies).toHaveLength(1)
    expect(bodies[0].trace_id).toBe('aaa111bbb222')
  })
})

test.describe('the Plan stage leads with the verdict, not with an empty history', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  /** Where each landmark sits in the page's own reading order. */
  async function order(page) {
    return page.evaluate(() => {
      const t = document.body.innerText
      return {
        history: t.indexOf('Run history'),
        routes: t.indexOf('ROUTES'),
        verdict: t.search(/Ready to run|Runs, not clean|Cannot run/),
      }
    })
  }

  test('Run history sits below the verdict on the Plan stage', async ({ page }) => {
    // Measured before: "Run history -- what previous live runs wrote (0
    // requests) / No live run has been recorded yet on this machine." was line
    // 68 of the rendered text and "ROUTES / 3" was line 72. The least useful
    // panel on the page held the most valuable slot on its most consequential
    // stage -- and it is the one panel that is empty on a fresh machine.
    await isolate(page)
    await seed(page)
    await openPlan(page)

    const at = await order(page)
    expect(at.routes).toBeGreaterThan(-1)
    expect(at.history).toBeGreaterThan(-1)
    expect(at.verdict).toBeGreaterThan(-1)
    expect(at.history, 'the verdict is read before the write history').toBeGreaterThan(at.verdict)
    expect(at.history).toBeGreaterThan(at.routes)
  })

  test('and stays where it was on the other stages', async ({ page }) => {
    // It already sat at the bottom of the other three. Moving it must not
    // change that, or the fix trades one misplacement for three.
    await isolate(page)
    await seed(page)
    await page.goto('/resource-planner')

    for (const stage of ['Account', 'Targets', 'Day & night']) {
      await page.getByRole('button', { name: stage, exact: true }).click()
      const at = await order(page)
      const tail = await page.evaluate(() => document.body.innerText.length)
      expect(at.history, `${stage} keeps its history at the bottom`).toBeGreaterThan(tail / 2)
    }
  })

  test('the last-run record travels with it', async ({ page }) => {
    // The two panels are one pair -- a write history and the record of the last
    // write -- and the undo lives in the second. Splitting them would leave the
    // post-run artefact above the go/no-go verdict, which is the same defect.
    await isolate(page)
    await seed(page, {
      planner_last_live_run: {
        at: new Date().toISOString(),
        traceId: 'abc123def456',
        created: 1,
        problems: [],
        disables: [],
        routes: [],
      },
    })
    await openPlan(page)

    const at = await page.evaluate(() => {
      const t = document.body.innerText
      return {
        lastRun: t.indexOf('Last live trade-route run'),
        verdict: t.search(/Ready to run|Runs, not clean|Cannot run/),
      }
    })
    expect(at.lastRun).toBeGreaterThan(at.verdict)
  })
})
