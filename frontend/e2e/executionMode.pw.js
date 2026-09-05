/**
 * `execution_mode` is what starts a live run, and `dry_run` never does.
 *
 * Backend twins, by symbol in `src/travian_api/web/routes/distribution.py`:
 * `ExecuteRequest.execution_mode` (`Literal["preview", "live"]`, default
 * `"preview"`), `ExecuteRequest.dry_run` (kept for older callers, never read by
 * the handler) and the validator `ExecuteRequest._execution_mode_is_unambiguous`.
 * That validator turns two of this page's old bodies into a 422:
 *
 *   - `dry_run: false` with `execution_mode` absent or `"preview"` — refused
 *     with a detail beginning `dry_run: false does not start a live run`. The
 *     reconcile sweep sent exactly this, so the one write path that carries no
 *     confirmation dialog would have 422'd on every chunk.
 *   - `dry_run: true` alongside `execution_mode: "live"` — refused as
 *     "contradict each other".
 *
 * So the page must send the MODE and stop sending the boolean: a preview says
 * `execution_mode: "preview"` and no `dry_run`, a live run says
 * `execution_mode: "live"` and no `dry_run`. `ExecuteResponse.dry_run` still
 * reports the mode the server resolved, and the result panel still reads it —
 * that direction is unchanged and is asserted here too, so a later tidy cannot
 * take the response field out with the request one.
 *
 * NO BACKEND AND NO GAME REQUEST: `isolate` answers `/distribution/execute`
 * from this file and ABORTS everything it does not recognise, and the snapshot
 * is seeded into localStorage rather than fetched.
 *
 * Running it:
 *   cd frontend
 *   npx playwright test executionMode
 */

import { expect, test } from '@playwright/test'

import { CAPITAL, PREVIEW, isolate, openPlan, seed } from './plannerHarness'

/** What a live run answers with: `dry_run` RESOLVED, which the panel reads. */
const LIVE = {
  ...PREVIEW,
  dry_run: false,
  created: 1,
  actions: [{ ...PREVIEW.actions[0], status: 'created', detail: 'route 9001' }],
}

/** One chunk of sweep, finished on the first pass so the loop does not wait. */
const SWEPT = {
  ...LIVE,
  swept_origins: [CAPITAL],
  unswept_origins: [],
  next_chunk_wait_seconds: 0,
  remaining: 0,
}

/**
 * Every `/execute` body this page sends, in order.
 *
 * The answer is chosen off `execution_mode` and NOT off `dry_run`, for the
 * reason the page itself now sends the mode: a preview no longer carries the
 * boolean at all, so a discriminator reading `dry_run` would read `undefined`
 * and hand the preview a live answer.
 */
async function captureExecutes(page, live = LIVE) {
  const bodies = []
  await isolate(page, async (path, route) => {
    if (!path.endsWith('/distribution/execute')) return undefined
    const body = route.request().postDataJSON()
    bodies.push(body)
    await route.fulfill({ json: body.execution_mode === 'live' ? live : PREVIEW })
    return 'handled'
  })
  await seed(page)
  await openPlan(page)
  return bodies
}

test.describe('the mode is stated, and the superseded boolean is not sent', () => {
  test.use({ viewport: { width: 1440, height: 1400 } })

  test('Preview sends execution_mode preview and no dry_run', async ({ page }) => {
    const bodies = await captureExecutes(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => bodies.length).toBe(1)

    // Preview-or-absent, per `ExecuteRequest.execution_mode`'s own default.
    expect(['preview', undefined]).toContain(bodies[0].execution_mode)
    // The half that was a 422 waiting to happen: `dry_run: false` on a body
    // that means a preview. Absent is the only shape that cannot become one.
    expect('dry_run' in bodies[0]).toBe(false)
  })

  test('the ordinary live run sends execution_mode live and no dry_run', async ({ page }) => {
    const bodies = await captureExecutes(page)

    await page.getByRole('button', { name: /^Preview \(0 requests\)/ }).click()
    await expect.poll(() => bodies.length).toBe(1)
    await page.getByRole('button', { name: /^Disable old routes & create/ }).click()
    await page.getByRole('button', { name: /^Go live/ }).click()
    await expect.poll(() => bodies.length).toBe(2)

    expect(bodies[1].execution_mode).toBe('live')
    // `dry_run: true` beside a live mode is the OTHER 422 the validator
    // raises, and the default of the field is `true` — so an omitted key is
    // the only one that stays a live run.
    expect('dry_run' in bodies[1]).toBe(false)

    // The response direction is untouched: the panel still reads the resolved
    // mode off `ExecuteResponse.dry_run`.
    await expect(page.getByText(/^Last live trade-route run/)).toBeVisible()
  })

  test('the reconcile sweep is live by construction and says so', async ({ page }) => {
    const bodies = await captureExecutes(page, SWEPT)

    // The one write button with no confirmation dialog, and the one that sent
    // a bare `dry_run: false` — the exact body the validator now refuses.
    await page.getByRole('button', { name: 'Reconcile all villages' }).click()
    await expect.poll(() => bodies.length).toBe(1)

    expect(bodies[0].execution_mode).toBe('live')
    expect('dry_run' in bodies[0]).toBe(false)
  })
})
