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
 * path driven is the PREVIEW, which writes nothing and is mocked besides.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test liveRunGuards
 */

import { expect, test } from '@playwright/test'

import {
  BLOCKER_BUDGET,
  BLOCKER_SHORT,
  KEY,
  PLAN_BLOCKED,
  PREVIEW,
  VIEWPORTS,
  isolate,
  openPlan,
  seed,
} from './plannerHarness'

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

    const box = page.getByLabel('Max rows this run', { exact: true })
    // A default the operator can see and change, not an empty box over a
    // sentence in a label.
    await expect(box).not.toHaveValue('')
    await expect(box).toHaveAttribute('placeholder', 'no limit')
  })

  test('clearing the box still means no limit', async ({ page }) => {
    // Blank has to stay unbounded. The cap is a default, not a new floor: an
    // operator who deliberately wants a whole-day provisioning pass must be
    // able to say so, and the way they say it is by emptying the box.
    //
    // Said on the wire as an explicit 0, not as an omission. Omitting it meant
    // unbounded only while `ExecuteRequest.max_game_rows_per_run` defaulted to
    // 0; `456bf02` moved that default to 24, so from then on an emptied box
    // asked the server for the very cap it had just cleared and this sentence
    // was true of the page and false of the run.
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
    await page.getByLabel('Max rows this run', { exact: true }).fill('')
    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect(page.getByText(/route\(s\) would be created/)).toBeVisible()

    expect(body).not.toBeNull()
    expect(body.max_game_rows_per_run).toBe(0)
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

    await page.getByLabel('Never disable', { exact: true }).fill('4688')
    await expect(page.getByText('no village named 4688 — did you mean 46|88?')).toBeVisible()
  })

  test('coordinates are never flagged, however foreign', async ({ page }) => {
    // The routes worth protecting are the hand-made ones to targets this
    // account does not own, so a pair matching no village is the normal case.
    await isolate(page)
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable', { exact: true }).fill('46|133')
    await expect(page.getByText(/no village named/)).toHaveCount(0)
  })

  test('a real village id is left alone', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    await page.getByLabel('Never disable', { exact: true }).fill('20011')
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

    await page.getByLabel('Never disable', { exact: true }).fill('4688')
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

    await page.getByRole('button', { name: 'Delete profile Day' }).click()
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
    live_game_rows: 6,
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
        return route.request().postDataJSON().execution_mode === 'live' ? LIVE : PREVIEW
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

    // Scoped by its own text: the write card is `.card card-danger` too, and it
    // is earlier in the DOM. That is deliberate -- it is the only tinted CARD --
    // and this box is a nested one inside a panel.
    const box = await page
      .locator('.card-danger')
      .filter({ hasText: 'must be deleted by hand' })
      .evaluate((el) => {
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
              live_game_rows: 12,
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

test.describe('the controlled run is a form, not a slab of prose', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  // The four labels, measured: 489 characters (Whole day), 408 (Trim the
  // fan-out), 215 (Also disable), 300 (Correct cargo). Their bolded lead clause
  // already IS the label; the rest is prose, and it carries real warnings that
  // must not be deleted.
  const ROWS = [
    { lead: 'Whole day', prose: /Plans every profile in its own hours/ },
    { lead: 'Trim the fan-out to the profile hours', prose: /24\/N daily rows/ },
    { lead: 'Also disable routes the plan no longer wants', prose: /create-only run/ },
    { lead: 'Correct cargo on routes that have drifted', prose: /tuned in-game on purpose/ },
  ]

  test('each checkbox reads as one label, with its prose behind a disclosure', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    for (const row of ROWS) {
      const label = page.getByText(row.lead, { exact: false }).first()
      await expect(label).toBeVisible()
      const visible = (await label.innerText()).trim()
      // The whole point: what is on screen before anything is clicked is the
      // label, not four paragraphs of it.
      expect(visible.length, `"${row.lead}" reads as a label, not an essay`).toBeLessThan(80)
      // And the prose is still there, one click away, not deleted: in the DOM
      // inside a closed <details>, so present but not on screen.
      await expect(page.getByText(row.prose).first()).toHaveCount(1)
      await expect(page.getByText(row.prose).first()).not.toBeVisible()
    }
  })

  test('the prose is kept, and opening the disclosure shows it', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    for (const row of ROWS) {
      // A native <summary>, so it is reached by its accessible name rather
      // than by a role: a <button> inside a <summary> would be a control inside
      // a control.
      await page.getByLabel(`Why: ${row.lead}`).click()
      await expect(page.getByText(row.prose).first()).toBeVisible()
    }
  })

  test('the five boxes are a field grid above the checkboxes', async ({ page }) => {
    await isolate(page)
    await seed(page)
    await openPlan(page)

    const layout = await page.locator('.controlled-run-fields').evaluate((el) => ({
      display: getComputedStyle(el).display,
      inputs: el.querySelectorAll('input').length,
      checkboxes: el.querySelectorAll('input[type="checkbox"]').length,
    }))
    expect(layout.display).toBe('grid')
    expect(layout.inputs).toBe(5)
    // The two number inputs were wedged BETWEEN paragraphs and three more sat
    // inside a third; the grid is fields only.
    expect(layout.checkboxes).toBe(0)

    // And the grid comes first in reading order.
    const at = await page.evaluate(() => {
      const t = document.body.innerText
      return { rows: t.indexOf('Max rows this run'), whole: t.indexOf('Whole day') }
    })
    expect(at.rows).toBeLessThan(at.whole)
  })

  test('every disclosure toggle is a 44px target on a coarse pointer', async ({ browser }) => {
    // Item 4 of the UI Definition of Done. A "?" glyph is about seven pixels
    // wide, so this is the one that a new affordance most easily gets wrong.
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    })
    const page = await context.newPage()
    await isolate(page)
    await seed(page)
    await openPlan(page)

    const sizes = await page.locator('.why-toggle').evaluateAll((nodes) =>
      nodes.map((el) => {
        const r = el.getBoundingClientRect()
        return { w: Math.round(r.width), h: Math.round(r.height) }
      }),
    )
    if (globalThis.process?.env?.MEASURE) console.log('\nwhy toggles:', JSON.stringify(sizes))
    expect(sizes.length).toBeGreaterThanOrEqual(4)
    for (const size of sizes) {
      expect(size.w).toBeGreaterThanOrEqual(44)
      expect(size.h).toBeGreaterThanOrEqual(44)
    }
    await context.close()
  })
})

test.describe('the reserved NPC-burst window survives being saved', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('it travels in the document the server is sent', async ({ page }) => {
    // `grep -rn "reserved" frontend/src/utils/` found nothing: the window lived
    // only in localStorage, which is per browser origin -- so it did not follow
    // the operator between :80, :8001, the LAN address and Tailscale, the exact
    // failure the storage panel two cards up warns about. Confirmed against a
    // real saved document, whose top level held every other owned field and not
    // this one.
    let saved = null
    await isolate(page, (path, route) => {
      if (path.endsWith('/distribution/setup') && route.request().method() === 'PUT') {
        saved = route.request().postDataJSON()
        return { account_key: 'k', setup: saved, saved_at: new Date().toISOString() }
      }
      return undefined
    })
    await seed(page, {
      planner_reserved_window: ['20:00', '21:00'],
      planner_trade_office: { 20002: 13 },
    })
    await page.goto('/resource-planner')

    await page.getByRole('button', { name: 'Save setup to server' }).click()
    await expect.poll(() => saved).not.toBeNull()

    expect(saved.reserved_window).toEqual(['20:00', '21:00'])
    // Both halves of the bump, or a fresh export answers 422 "NEWER build".
    // 11 since the measured merchant model landed; the reserved window this
    // test is about is what took it to 9, and the window prune to 10.
    expect(saved.version).toBe(11)
  })

  test('and comes back out of it', async ({ page }) => {
    const DOC = {
      format: 'travian-planner-owned-state',
      version: 9,
      exported_at: new Date().toISOString(),
      account: KEY,
      villages: [{ village_id: 20002, name: '02', trade_office_level: 13 }],
      reserved_window: ['19:30', '20:30'],
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
    await page.getByRole('button', { name: 'Day & night' }).click()

    // The boxes on screen, which is where the answer is typed.
    await expect(page.getByLabel('NPC burst window start')).toHaveValue('19:30')
  })
})

test.describe('the Plan stage puts the verdict and the write path first', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  /** The Plan stage with a plan that CANNOT run. */
  async function blocked(page) {
    await isolate(page, (path) =>
      path.endsWith('/distribution/plan') ? PLAN_BLOCKED : undefined,
    )
    await seed(page)
    await openPlan(page)
  }

  /** The Plan stage with a clean plan. */
  async function clean(page) {
    await isolate(page)
    await seed(page)
    await openPlan(page)
  }

  test('the verdict is the largest thing on the stage, and it leads', async ({ page }) => {
    // The audit's verdict on "eye-catching" was no: eleven cards of identical
    // weight, width and 11-14px type, with the go/no-go answer as a small line
    // in a three-column strip and the largest number on the page a COST three
    // thousand pixels below the fold.
    await blocked(page)

    const banner = page.locator('.plan-verdict')
    await expect(banner).toBeVisible()
    const size = await banner
      .locator('.plan-verdict-label')
      .evaluate((el) => parseFloat(getComputedStyle(el).fontSize))
    expect(size).toBeGreaterThanOrEqual(32)

    // First in the stage's own reading order.
    const at = await page.evaluate(() => {
      const t = document.body.innerText
      return { verdict: t.indexOf('Cannot run'), sheet: t.indexOf('Setup sheet') }
    })
    expect(at.verdict).toBeGreaterThan(-1)
    expect(at.verdict).toBeLessThan(at.sheet)
  })

  test('the verdict changes tone rather than only wording', async ({ page }) => {
    await blocked(page)
    await expect(page.locator('.plan-verdict.plan-verdict-blocked')).toHaveCount(1)

    await clean(page)
    await expect(page.locator('.plan-verdict.plan-verdict-clean')).toHaveCount(1)
  })

  test('each blocker is printed exactly once, with a way to fix it', async ({ page }) => {
    // Measured before: the same two sentences appeared four times on one
    // screen -- in the "what this checked" disclosure, in the execute panel,
    // and twice more as restatements.
    await blocked(page)

    const text = await page.evaluate(() => document.body.innerText)
    for (const blocker of [BLOCKER_BUDGET, BLOCKER_SHORT]) {
      const count = text.split(blocker).length - 1
      expect(count, `"${blocker}" is printed once`).toBe(1)
    }
    // And each one is actionable from where it is printed.
    await expect(page.locator('.plan-verdict').getByRole('button', { name: /fix/ })).toHaveCount(2)
  })

  test('the other copies become one line each', async ({ page }) => {
    await blocked(page)

    // The count, not the sentences. Both other copies live inside panels the
    // operator has to open, which is itself the point: neither competes with
    // the banner for the same screen.
    await expect(page.getByText(/2 problems? above/)).toHaveCount(2)
    await page.getByText(/^What “Cannot run” checked/).click()
    await expect(page.getByText(/2 problems? above/).first()).toBeVisible()
  })

  test('cost and NPC triggers are the only other large numbers', async ({ page }) => {
    await blocked(page)

    const tiles = page.locator('.plan-headline-figure')
    await expect(tiles).toHaveCount(2)
    for (const size of await tiles.evaluateAll((nodes) =>
      nodes.map((el) => parseFloat(getComputedStyle(el).fontSize)),
    )) {
      expect(size).toBeGreaterThanOrEqual(24)
    }
    await expect(page.getByText('96,000').first()).toBeVisible()

    // Above the sheet, which is where the operator acts on them.
    const at = await page.evaluate(() => {
      const t = document.body.innerText
      return { cost: t.indexOf('96,000'), sheet: t.indexOf('Setup sheet') }
    })
    expect(at.cost).toBeLessThan(at.sheet)
  })

  test('every read-only panel folds, with its count in the summary', async ({ page }) => {
    await blocked(page)

    for (const summary of [
      /^Merchant budget/,
      /^Relayed crop/,
      /^What the account had to give/,
      /^NPC balancing/,
      /^Still on the road at the switch/,
    ]) {
      const found = page.getByText(summary).first()
      await expect(found, `${summary} is a disclosure summary`).toBeVisible()
      const tag = await found.evaluate((el) => el.closest('details, summary')?.tagName ?? el.tagName)
      expect(tag).toMatch(/DETAILS|SUMMARY/)
    }
    // The counts, so a closed panel still says whether it is worth opening.
    await expect(page.getByText(/Merchant budget \(1 village over\)/)).toBeVisible()
  })

  test('a non-clean plan opens them, a clean one leaves them shut', async ({ page }) => {
    await blocked(page)
    expect(
      await page.locator('details.plan-readonly[open]').count(),
      'a refused plan opens what it left behind',
    ).toBeGreaterThan(0)

    await clean(page)
    expect(await page.locator('details.plan-readonly[open]').count()).toBe(0)
  })

  test('the write card is the only tinted card, and the live button is the danger one', async ({
    page,
  }) => {
    await isolate(page, (path) => (path.endsWith('/distribution/execute') ? PREVIEW : undefined))
    await seed(page)
    await openPlan(page)

    // Exactly one tinted, bordered card on the stage, and it is the one that
    // writes. Eleven cards of identical weight is how the write path came to be
    // indistinguishable from the YAML export.
    await expect(page.locator('.card-danger')).toHaveCount(1)
    await expect(page.locator('.card-danger')).toContainText(/Write it to the game/i)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    const live = page.getByRole('button', { name: /^Disable old routes & create/ })
    const shape = await live.evaluate((el) => ({
      classes: el.className,
      width: Math.round(el.getBoundingClientRect().width),
      parent: Math.round(el.parentElement.getBoundingClientRect().width),
    }))
    expect(shape.classes).toContain('btn-danger')
    // Full width, so the thing that writes to the account is not a small
    // outline button beside a large filled one that writes a document.
    expect(shape.width).toBeGreaterThan(shape.parent * 0.9)
    // The estimate stays: every action on this page states its cost.
    await expect(live).toContainText(/~\d+ requests/)
  })

  test('the YAML export is a bottom disclosure with its digest', async ({ page }) => {
    // It was the biggest filled button on the page -- for a document that
    // changes nothing -- while the button that writes to the account was a
    // small one further down.
    await clean(page)

    const summary = page.getByText(/^Export this plan as YAML/)
    await expect(summary).toBeVisible()
    await expect(summary).toContainText('dddddddddddd')

    const at = await page.evaluate(() => {
      const t = document.body.innerText
      return { yaml: t.indexOf('Export this plan as YAML'), write: t.indexOf('Write it to the game') }
    })
    expect(at.yaml).toBeGreaterThan(at.write)

    // And it is no longer the loudest button.
    await summary.click()
    const cls = await page
      .getByRole('button', { name: /Confirm this plan and export YAML/ })
      .evaluate((el) => el.className)
    expect(cls).not.toContain('btn-primary')
  })
})

test.describe('the redesigned Plan stage against the UI Definition of Done', () => {
  for (const viewport of VIEWPORTS) {
    test.describe(`at ${viewport.width}px`, () => {
      test.use({ viewport })

      test('nothing scrolls the page sideways, with every disclosure open', async ({ page }) => {
        // Item 1. Asked with the disclosures DRIVEN OPEN, because "not on
        // screen" is otherwise a loophole -- the redesign moved five panels
        // behind <details>, so a closed one cannot be the reason this passes.
        await isolate(page, (path) =>
          path.endsWith('/distribution/plan') ? PLAN_BLOCKED : undefined,
        )
        await seed(page)
        await openPlan(page)

        const opened = await page.evaluate(() => {
          const all = [...document.querySelectorAll('details')]
          for (const d of all) d.open = true
          return all.length
        })
        expect(opened, 'there are disclosures to open').toBeGreaterThan(5)
        expect(
          await page.evaluate(() => [...document.querySelectorAll('details:not([open])')].length),
          'every disclosure is open',
        ).toBe(0)

        const scroll = await page.evaluate(() => ({
          x: document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth,
          overflowing: [...document.querySelectorAll('*')]
            .filter((el) => {
              const cs = getComputedStyle(el)
              if (cs.overflowX !== 'auto' && cs.overflowX !== 'scroll') return false
              return el.getBoundingClientRect().width > document.documentElement.clientWidth + 1
            })
            .map((el) => el.className),
        }))
        if (globalThis.process?.env?.MEASURE) console.log(`\n${viewport.width}px page scroll:`, JSON.stringify(scroll))
        expect(scroll.x, 'no horizontal page scroll').toBe(0)
        expect(scroll.overflowing, 'no scrolling container is wider than the viewport').toEqual([])
      })

      test('no control is clipped, with every disclosure open', async ({ page }) => {
        // Item 1's other half, and the number the audit asks be held at zero: a
        // control whose own content does not fit inside it.
        await isolate(page, (path) =>
          path.endsWith('/distribution/plan') ? PLAN_BLOCKED : undefined,
        )
        await seed(page)
        await openPlan(page)
        await page.evaluate(() => {
          for (const d of document.querySelectorAll('details')) d.open = true
        })

        const clipped = await page.$$eval('input, select, button', (nodes) =>
          nodes
            .filter((el) => el.getBoundingClientRect().width > 0)
            .filter((el) => el.scrollWidth > el.clientWidth + 1)
            .map((el) => ({
              tag: el.tagName,
              label:
                el.getAttribute('aria-label') || el.textContent?.trim().slice(0, 40) || el.type,
              client: Math.round(el.clientWidth),
              content: Math.round(el.scrollWidth),
            })),
        )
        if (globalThis.process?.env?.MEASURE) console.log(`\n${viewport.width}px clipped:`, JSON.stringify(clipped))
        expect(clipped).toEqual([])
      })
    })
  }

  test('the sheet keeps its identity column pinned when it scrolls', async ({ page }) => {
    // The one table the redesign touched around: the relay chain moved inside a
    // disclosure in the same card as the sheet. Pinning is what makes a wide
    // table readable, and it is asserted structurally rather than by screenshot.
    await page.setViewportSize({ width: 375, height: 900 })
    await isolate(page, (path) => (path.endsWith('/distribution/plan') ? PLAN_BLOCKED : undefined))
    await seed(page)
    await openPlan(page)
    await page.evaluate(() => {
      for (const d of document.querySelectorAll('details')) d.open = true
    })

    const pinned = await page.$$eval('.table-overflowing', (wrappers) =>
      wrappers.map((w) => ({
        scrollable: w.scrollWidth > w.clientWidth,
        pinnedCells: w.querySelectorAll('.sticky-col').length,
      })),
    )
    if (globalThis.process?.env?.MEASURE) console.log('\npinned:', JSON.stringify(pinned))
    for (const wrapper of pinned) {
      if (wrapper.scrollable) expect(wrapper.pinnedCells).toBeGreaterThan(0)
    }
  })
})

test.describe('the new surfaces meet WCAG AA in both themes', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  /**
   * The contrast ratio of one element's own text against its own background.
   *
   * The channel parser handles `color(srgb r g b)` as well as `rgb(r, g, b)`,
   * and that is not defensive coding -- `color-mix()` computes to the FORMER,
   * with components in 0..1. A first version of this measurement read those as
   * 0..255, which turned every washed background into near-black and reported
   * the clean banner at 3.23:1. The banner was fine; the ruler was not.
   */
  async function contrast(page, selector) {
    return page.evaluate((sel) => {
      const channels = (value) => {
        const probe = document.createElement('span')
        probe.style.color = value
        document.body.appendChild(probe)
        const computed = getComputedStyle(probe).color
        probe.remove()
        const numbers = (computed.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number)
        // `color(srgb ...)` is 0..1 per channel; `rgb()` is 0..255.
        return computed.startsWith('color(') ? numbers.map((n) => n * 255) : numbers
      }
      const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4))
      const lum = ([r, g, b]) => 0.2126 * lin(r / 255) + 0.7152 * lin(g / 255) + 0.0722 * lin(b / 255)
      const el = document.querySelector(sel)
      if (!el) return null
      const cs = getComputedStyle(el)
      const fg = lum(channels(cs.color))
      const bg = lum(channels(cs.backgroundColor))
      const ratio = (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05)
      return Math.round(ratio * 100) / 100
    }, selector)
  }

  for (const theme of ['light', 'dark']) {
    for (const tone of [
      { name: 'blocked', selector: '.plan-verdict-blocked', plan: PLAN_BLOCKED },
      { name: 'clean', selector: '.plan-verdict-clean', plan: null },
      {
        // Executable, with a critical finding the gate does not weigh: the
        // third state, and the one that is neither red nor green.
        name: 'dirty',
        selector: '.plan-verdict-dirty',
        plan: {
          ...PLAN_BLOCKED,
          feasible: true,
          verdict: {
            executable: true,
            clean: false,
            blockers: [],
            covers: ['every merchant budget'],
            unweighed: ['overflow'],
            critical_findings: 1,
          },
        },
      },
    ]) {
      test(`the ${tone.name} verdict banner in ${theme}`, async ({ page }) => {
        // Item 3 of the UI Definition of Done: "a token pair that passes in
        // light can fail in dark". The app sets no `data-theme` today -- the
        // dark tokens exist and nothing selects them -- so the attribute is set
        // here, which is the only way to ask the question at all.
        await isolate(page, (path) =>
          tone.plan && path.endsWith('/distribution/plan') ? tone.plan : undefined,
        )
        await seed(page)
        await openPlan(page)
        await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)

        const ratio = await contrast(page, tone.selector)
        if (globalThis.process?.env?.MEASURE) {
          console.log(`\n${theme} ${tone.name} banner: ${ratio}:1`)
        }
        // 4.5:1, the small-text threshold. The banner's own label is 2rem and
        // would only need 3, but its blocker lines and its sub-line are 12px,
        // and they inherit this colour.
        expect(ratio).toBeGreaterThanOrEqual(4.5)
      })
    }

    test(`the write card's danger wash in ${theme}`, async ({ page }) => {
      await isolate(page)
      await seed(page)
      await openPlan(page)
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)

      const ratio = await contrast(page, '.card-danger')
      if (globalThis.process?.env?.MEASURE) console.log(`\n${theme} write card: ${ratio}:1`)
      expect(ratio).toBeGreaterThanOrEqual(4.5)
    })
  }
})
