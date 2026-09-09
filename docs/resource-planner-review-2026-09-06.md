# Resource planner review — 2026-09-06

The planner has a substantial pure calculation layer and extensive regression coverage, but I found eight issues across route allocation, live reconciliation, recovery, and asynchronous UI state. The highest priorities are undo attribution and the scope of live writes.

This is a review, not an implementation. No application code was changed and no game requests were made. Code-path findings below are distinguished from locally reproduced behavior. Passing tests establish the existing contracts; they do not cover the counterexamples identified here.

## Findings

### 1. P1 — Undo can delete routes created by a later run

Sources: `src/travian_api/services/distribution/route_revert.py:124`, `src/travian_api/web/routes/distribution.py:5691` and the delete branch in that handler.

`plan_revert` treats every currently existing route ID absent from the selected run's opening inventory as a route created by that run. It has no closing inventory or attribution filter. The execution lock prevents overlapping execution and undo; it does not protect against a run that finished after the selected run.

Locally reproduced with the pure function:

```text
Before run A:       [10]
Created by run A:   [20]
Created later by B: [30]
Undo A selects:    [20, 30]
```

With `apply_delete`, the handler passes both IDs to deletion. Manual additions have the same problem. The UI describes this operation as undoing one particular run, so silently including later changes violates its scope.

Recommended fix: record and verify the rows attributable to each run, with account identity and expected post-run state. Refuse ambiguous or subsequently modified state before proposing mutations. A current-minus-opening diff alone cannot implement run-specific undo.

Regression: execute A, execute B at the same origin, undo A, and assert that B's rows remain untouched. Repeat with a manually added row and changed enabled state.

### 2. P1 — Run history and undo lack user/account isolation

Sources: `src/travian_api/web/routes/distribution.py:9983`, `src/travian_api/web/routes/distribution.py:5612`, `src/travian_api/services/distribution/execution_trace.py:246`, `src/travian_api/services/distribution/run_history.py` (`summarise_runs`).

The history endpoint authenticates `_user` but never uses it: it summarizes the shared trace directory. The undo endpoint reads a supplied trace ID without checking its owner or game account and compares its inventory against the caller's current session. `read_inventories` discards all events except `origin_read`, including the `run_start` user field. The trace creation shown in the execution handler does not bind the run to a server/player identity.

Consequences:

- History from different users or game accounts is mixed into one account rollup.
- An authenticated user can obtain other runs' IDs through history.
- Selecting a trace after switching worlds can compare unrelated inventories. Where origin IDs coincide, the resulting disable/delete selection can affect the wrong account's routes.

This is a verified code-path defect, not an attempted cross-account game operation. Its security impact depends on whether the installation has multiple users; account switching also exposes the correctness problem for one user.

Recommended fix: store user ID plus a stable game-account/server identity on each trace. Filter history before applying the limit, and reject undo if either identity differs from the connected session. Treat legacy traces without identity as requiring explicit offline inspection, not automatically executable undo input.

### 3. P1 — “Only switches routes OFF” can provision routes

Sources: `frontend/src/pages/ResourcePlanner.jsx:3345` and the reconciliation instructions around `:7496`.

The reconciliation panel says it only switches routes off, “never on.” However, in whole-day mode the handler sends a positive `max_routes_per_run`, derived from the normal run controls, together with `execution_mode: 'live'`. The button calls the sweep directly and does not use the ordinary live-run confirmation dialog.

Thus an operator can choose an action explicitly described as disabling old routes and instead also create the whole-day schedule across successive chunks. Provisioning may be intentional for this mode; the defect is that the action's displayed scope does not describe what it sends.

Recommended fix: either keep this action disable-only in every mode, or give whole-day provisioning an explicit label, explanation, and review of its create/disable/delete scope before it starts.

Regression: assert both the rendered instructions and outgoing request for single-profile and whole-day modes. A test of the request alone would preserve this mismatch.

### 4. P2 — Reconciliation bypasses the stale-snapshot live-write guard

Sources: `frontend/src/pages/ResourcePlanner.jsx:2955`, `:3302`, and `:7526`.

`executePlan(false)` refuses a snapshot older than 30 minutes unless the user explicitly accepts stale state. `runReconcileSweep` checks field blockers and the presence of a plan, but never checks snapshot age or that override. Its button also omits the freshness condition.

Reproduction by code path: build a plan, leave the page open past the live TTL, and use Reconcile all villages. Normal execution refuses the stale state; reconciliation still submits a live request using it. In whole-day mode that request can also create routes. Snapshot age is not carried as an enforceable condition in `ExecuteRequest`.

Recommended fix: share the live-write gate between both paths and recheck freshness before each chunk. A long-running sweep needs an explicit policy for expiration during its pauses, not just at the first click.

Regression: test a stale initial snapshot with the override off, and a snapshot that expires between two chunks.

### 5. P2 — Greedy allocation can report a shortage despite a feasible routing

Sources: `src/travian_api/services/distribution/optimizer.py:696`, `:2224`, `:2266`.

The initial flow builder serves the largest receiver first and selects its nearest suppliers. It does not reserve a constrained supplier for a receiver that has no alternative. Its shortfalls are recorded before improvement; the later search preserves destination inflows, so it cannot repair the unmet demand.

Locally reproduced through `resolve_resource` and `build_plan`, with abundant merchant capacity:

| Village | Coordinates | Supply or demand per hour | Restriction |
|---|---|---:|---|
| A / 1 | (0, 0) | supplies 100 lumber | none |
| B / 2 | (10, 0) | supplies 100 lumber | cannot supply D |
| C / 3 | (1, 0) | needs 120 lumber | can use A or B |
| D / 4 | (-1, 0) | needs 80 lumber | can only use A |

Actual result: A sends 100 to C; B sends 20 to C; D is reported short by 80 and the plan is infeasible.

A feasible result exists: B sends 100 to C; A sends 20 to C and 80 to D. No additional production, merchants, relay, or relaxed exclusion is needed.

Recommended fix: establish a feasible transportation flow with the exclusions first, using residual reassignment/augmenting paths or a flow solver, then optimize cost. Sorting more constrained destinations first helps but does not establish general feasibility.

Regression: the four-village case above, plus permutations of village IDs and asymmetric exclusions. Assert that all demand is met, not just that the chosen routes conserve what they carry.

### 6. P2 — A late night-profile response can overwrite newer setup state

Sources: `frontend/src/pages/ResourcePlanner.jsx:1005`, `:3239`.

Plan building and the full-day check capture account and input revisions before awaiting their requests. `buildNightProfile` does not. It applies returned allocations unconditionally through the captured `setAllocations` callback.

If allocations or fill settings are edited while derivation is in flight, the older result overwrites newer edits. If the account changes before the response returns, the callback merges the old account's allocation IDs into the current `profiles` state under the originally captured profile name. A renamed or deleted profile can similarly be recreated. Merely switching profiles does not redirect the callback to the new active profile: its captured name is the relevant detail.

Recommended fix: capture the account, profile identity, and input revision. Apply the response only if all still match; otherwise discard it and explain that the inputs changed. Also guard result/error display against a different account.

Regression: delay the mocked night-profile response, then edit an allocation, rename/delete the source profile, or switch account before resolving it. Assert that no stale state is persisted.

### 7. P2 — Undo reports success while previously disabled routes still need restoring

Sources: `frontend/src/pages/ResourcePlanner.jsx:2815`, `src/travian_api/web/routes/distribution.py:5697`.

The backend reports changed pre-existing enabled states in `restore_state`; it does not automatically restore those routes in this endpoint. The frontend's success decision checks only `problems` and `must_delete_by_hand`, then displays “The run is undone — nothing left outstanding.” It ignores `restore_state` and `clean`.

Locally reproduced the underlying state comparison with one pre-existing active route that the run disabled:

```text
clean: false
must_delete: []
restore: [(10, true)]
```

That response shape reaches the success toast even though route 10 still needs to be enabled. The panel can simultaneously list restoration instructions and announce successful undo.

Recommended fix: derive completion from every outstanding category. If restoration remains manual, say so explicitly. Do not blindly use the current `clean` value as the only post-write signal either: the handler computes it from the initial comparison, before applying mutations.

Regression: a disable-only original run, followed by undo, must continue to report required restoration until it is verified complete.

### 8. P2 — Sweep completion ignores deferred creates and unresolved outcomes

Sources: `frontend/src/pages/ResourcePlanner.jsx:3380`, `:3411`, and the completion label around `:7564`.

The loop tracks both unvisited origins and `createsLeft`, but the final `done` expression checks only whether origins remain. If every origin has been visited and the user stops during the pause before a provisioning retry, `remaining > 0` is discarded from the final progress state. A zero wait or a stall can reach the same completion calculation. The panel then says “COMPLETE — nothing stale left.”

A nonempty `problems` list does not suppress that label either. Visiting every marketplace establishes neither that every planned create succeeded nor that every stale row was successfully disabled.

Recommended fix: keep distinct counts for origins visited, creates deferred, and unresolved reconciliation actions. Only describe the requested whole-day operation as complete when all required work is finished; otherwise preserve the stop reason and remaining work. Count unique origins so repeated passes do not inflate the village total.

Regression: return `unswept_origins: []` with `remaining > 0`, cancel during the next-chunk pause, and assert an incomplete result. Also cover a visited origin whose disable failed.

## Architecture and calculation review

The feature spans much more than its URL. The reviewed paths include snapshot acquisition; setup persistence and role templates; allocation; merchant and toroidal geometry models; greedy routing and improvement; scheduling, crop relays and declared material relays; NPC budgets; storage replay and day/night profiles; YAML export; live route matching, window pruning, execution traces, history, and undo.

Several design choices are sound and should survive fixes:

- Snapshot fetching is separate from planning; tuning targets does not require new game traffic.
- Plan, preview, and execution share the account-planning path. CPU-heavy planning and storage work are dispatched through `asyncio.to_thread`.
- Allocation distinguishes production, retention, consumption, and NPC conversion instead of treating conversion as extra production.
- Merchant capacity is configurable; routing budgets account for round-trip occupancy, reserves, and per-village caps.
- Window enforcement is explicit, and whole-day execution uses per-segment schedules and pruning.
- Execution distinguishes refused, unverified, and indeterminate outcomes and contains substantial read-back and replacement recovery logic.
- Setup storage reuses planner validation, while YAML export checks the digest of the reviewed plan.

The main maintainability concern is concentration: the distribution route module is approximately 10,000 lines, and the React page exceeds 7,500. Similar action flows implement their own freshness, identity, completion, and result-persistence rules. Findings 3, 4, 6, 7, and 8 are concrete examples of those rules diverging. Extracting common guards and an execution-state model would address a demonstrated problem; splitting files purely by line count would not.

## Verification and limits

- Python feature suite: **1,802 passed, 2 skipped**, covering distribution, setup storage, trade-route services, execution convergence/traces, night profiles, reconciliation matching, revert, and window behavior.
- Additional cargo-rounding, frontend/backend constant, and run-history tests: **42 passed**.
- Frontend unit suite: **667 passed across 20 files**.
- Two extra local pure-function reproductions confirmed findings 1 and 5; the state comparison used for finding 7 was also reproduced.
- Selected Playwright suites were attempted, but could not launch because the expected `chromium_headless_shell-1234` executable was absent. The run was stopped after the repeated launch failures. Those are environment failures, not evidence of failing UI assertions. The frontend findings are source-level findings awaiting browser regression coverage.
- Python test collection initially failed on sandbox access to the existing application key file; the suites passed after approved execution outside the sandbox. No credential contents were printed.
- No production build/deployment, live account mutation, or in-game mechanic experiment was performed. This review does not certify every branch or actual game behavior.

The repository explicitly records remaining assumptions about merchant calibration, partial shipments when resources are insufficient, and supported world geometry. I did not treat those as newly discovered bugs or independently verified game facts. They remain limits on how confidently simulated delivery/storage outcomes can predict a real account.

Recommended implementation order: fix run attribution and account binding first; make reconciliation's write scope and freshness consistent; repair feasible flow allocation; then address stale derivation responses and completion reporting. Add the small adversarial regressions above alongside the existing broad suite.
