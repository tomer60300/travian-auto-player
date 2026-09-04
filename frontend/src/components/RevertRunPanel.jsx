/** What undoing one live run would take, and the two ways to do it.
 *
 * `POST /api/distribution/routes/revert-plan` has existed and been unreachable:
 * `grep -rn "revert" frontend/src` found only unrelated prose, and
 * `grep -n "trace_id" frontend/src` found nothing at all -- while
 * `ExecuteResponse.trace_id` is real and is the only handle the endpoint
 * accepts. So a run that wrote seventy-two game rows had no in-app path back,
 * even though the app had been handed the key to it.
 *
 * Three rules, all of them the endpoint's own and none of them this panel's
 * invention:
 *
 *   1. **Read-only first.** `apply_disable` and `apply_delete` both default to
 *      false, and the panel offers neither until the read-only answer has said
 *      what there is. "Reverting is deliberately not a single button."
 *   2. **`must_delete_by_hand` is the prominent part.** It is the half no
 *      button covers -- rows a person has to remove -- so it leads, and it
 *      leads even when the automated half succeeded, because the automated half
 *      succeeding is exactly when it would be forgotten.
 *   3. **Disabling and deleting are separate opt-ins.** A disabled route can be
 *      switched back on; a deleted one cannot. Disabling happens first
 *      regardless, so the resources stop moving even if the removal then fails
 *      -- which is why the delete button says it disables too rather than
 *      pretending to be the narrower action.
 *
 * The read-only check is NOT free, and says so. Every origin the run touched
 * costs two game requests to re-read, and every step is confirmed by reading
 * the page back: a revert that claimed to have undone a run while leaving live
 * routes behind would be worse than one that names the rows still outstanding.
 */

/** `{ [origin]: number[] }` as "village 20002: 9001, 9002" lines. */
function byOrigin(map, villages) {
  return Object.entries(map ?? {}).map(([origin, ids]) => ({
    origin,
    label:
      (villages ?? []).find((v) => v.village_id === Number(origin))?.name ?? `village ${origin}`,
    ids,
  }))
}

export default function RevertRunPanel({
  traceId,
  state,
  villages,
  onCheck,
  onDisable,
  onDelete,
}) {
  // One answer at a time, keyed by trace id: a history row's steps must never
  // render under another run's heading.
  const mine = state && state.traceId === traceId ? state : null
  const result = mine?.result ?? null
  const outstanding = byOrigin(result?.must_delete_by_hand, villages)
  const restore = Object.entries(result?.restore_state ?? {})
  const disabled = byOrigin(result?.disabled_now, villages)
  const deleted = byOrigin(result?.deleted_now, villages)

  return (
    <div className="text-xs">
      <p className="text-secondary">
        The game returns no id when it creates a route, so the only record of
        what each village looked like before this run is the inventory the run
        itself wrote down. That is what this reads, and it is why the undo is
        tied to one run rather than offered as a general &ldquo;remove my
        routes&rdquo;.
      </p>
      <button
        type="button"
        className="btn-secondary btn-xs mt-2"
        disabled={Boolean(mine?.busy)}
        onClick={() => onCheck(traceId)}
      >
        {mine?.busy && !result
          ? 'Reading each village…'
          : 'Check what undoing this would take (~2 requests per village the run touched)'}
      </button>

      {mine?.error && (
        <p className="text-danger mt-2" role="alert">
          {mine.error}
        </p>
      )}

      {result && (
        <div className="mt-3 space-y-2">
          {/* The half no button covers, first. */}
          {outstanding.length > 0 && (
            <div className="card-danger p-2">
              <p className="text-danger font-semibold">
                {outstanding.reduce((n, row) => n + row.ids.length, 0)} route(s) must be deleted
                by hand
              </p>
              <ul className="list-disc list-inside mt-1">
                {outstanding.map((row) => (
                  <li key={row.origin}>
                    {row.label}: {row.ids.join(', ')}
                  </li>
                ))}
              </ul>
              <p className="text-secondary mt-1">
                Deleting is the one irreversible action here, so it is not done for you unless you
                ask below. Until these rows are gone they are only switched off, and a switched-off
                route can be switched back on.
              </p>
            </div>
          )}

          {result.clean && (
            <p className="text-success">
              Nothing to undo — every village reads as it did before this run.
            </p>
          )}

          {disabled.length > 0 && (
            <p className="text-secondary">
              Disabled just now, confirmed inert:{' '}
              {disabled.map((row) => `${row.label} (${row.ids.join(', ')})`).join(' · ')}
            </p>
          )}
          {deleted.length > 0 && (
            <p className="text-secondary">
              Deleted just now, confirmed gone:{' '}
              {deleted.map((row) => `${row.label} (${row.ids.join(', ')})`).join(' · ')}
            </p>
          )}

          {restore.length > 0 && (
            <div>
              <p className="font-semibold">Routes this run switched, to put back:</p>
              <ul className="list-disc list-inside mt-1">
                {restore.map(([origin, lines]) => (
                  <li key={origin}>
                    {(villages ?? []).find((v) => v.village_id === Number(origin))?.name ??
                      `village ${origin}`}
                    : {lines.join(', ')}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.problems?.length > 0 && (
            <ul className="text-danger list-disc list-inside" role="alert">
              {result.problems.map((problem, i) => (
                <li key={i}>{problem}</li>
              ))}
            </ul>
          )}

          {/* The endpoint's own ordering, stated in its own words. */}
          {result.steps?.length > 0 && (
            <details>
              <summary className="cursor-pointer text-secondary">
                Every step, in order ({result.steps.length})
              </summary>
              <ol className="list-decimal list-inside mt-1 font-mono">
                {result.steps.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
            </details>
          )}

          <p className="text-secondary font-mono">
            {result.requests_used} game request(s) spent reading and confirming.
          </p>

          {!result.clean && (
            <div className="flex flex-wrap gap-2 pt-1">
              <button
                type="button"
                className="btn-secondary btn-xs"
                disabled={Boolean(mine?.busy)}
                onClick={() => onDisable(traceId)}
              >
                Disable those routes now (reversible)
              </button>
              <button
                type="button"
                className="btn-danger btn-xs"
                disabled={Boolean(mine?.busy)}
                onClick={() => onDelete(traceId)}
              >
                Delete those routes for good (disables first)
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
