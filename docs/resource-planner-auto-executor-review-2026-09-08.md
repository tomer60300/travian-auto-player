# Resource planner auto-executor safety review — 2026-09-08

## Verdict

**Not ready for unattended production auto-execution.** The executor has substantial safeguards, but the automatic multi-chunk workflow can lose unfinished work, continue after a server stop, and declare completion with stale cargo. Initial marketplace inventory is also not sufficiently validated before writes.

This is a review, not a fix. No production/game actions, restarts, deployments, or source changes were made during this review. Findings describe the inspected checkout, not proof that these failures have already occurred on the production account.

Reviewed checkout: `perf/test-suite-speed`, commit `e207ac589b7199c2cd8ad70cd4068942adc7f4e8`. Scope: browser automatic reconciliation, execute API, marketplace reads/writes, trace persistence, locking, stop handling, and verification/recovery boundaries. Optimizer quality was outside this review.

## Execution map

`ResourcePlanner.runReconcileSweep` → chunk request → server recomputes plan → live gates and service lock → initial marketplace inventory → classify existing schedules → disable/re-enable/update/create → readback and reconciliation → response → browser selects next origins or reports completion.

The critical boundaries are **inventory before the first write**, **durable evidence before destructive changes**, and **account-wide unfinished work across requests**. Per-request correctness alone does not establish sweep correctness.

## Findings

P1 = release blocker for unattended execution; P2 = important safety/recovery gap. These priorities reflect impact and reproducibility, not measured production frequency.

| ID | Priority | Finding | Evidence |
| --- | --- | --- | --- |
| AE-01 | P1 | A later filtered chunk hides earlier deferred creates; UI reports COMPLETE | Backend and browser reproduction |
| AE-02 | P1 | Stop fields expected by browser are absent from execute response | Backend response and browser reproduction |
| AE-03 | P1 | Malformed initial marketplace model becomes empty/partial inventory | Four parser/service cases |
| AE-04 | P1 | Initial inventory accepts another village's model | Service reproduction |
| AE-05 | P1 | Capped cargo updates disappear from outstanding work | Stateful executor reproduction |
| AE-06 | P2 | Trace write failure does not prevent subsequent live writes | Disk-full injection into real executor |
| AE-07 | P2, conditional | Execute lock is service-instance-local, not account-wide | Independent locks acquired simultaneously |

### AE-01 — Deferred creates are lost across filtered chunks

Locations: `frontend/src/pages/ResourcePlanner.jsx:3460–3504`; `src/travian_api/web/routes/distribution.py:6680–6685, 9946`.

The browser sends `only_origins = unswept_origins` on the next request and replaces its outstanding-create count with that request's `remaining`. The server counts deferred work in the current filtered request; it does not carry the previous request's deferred work. Thus a village can be visited but only partly provisioned, disappear from the next request, and never return.

Reproduction using the existing stateful fake service:

1. Plan two routes from origin A and one from B.
2. Cap each chunk at one create and one origin.
3. First request creates one A route: `remaining=2`, `unswept_origins=[B]`.
4. Second request filters to B and creates its route: `remaining=0`, `unswept_origins=[]`.
5. Only two of three routes exist. Browser nevertheless renders `COMPLETE — nothing stale left`.

Required fix: retain unfinished work by origin/action across the entire sweep, or perform an authoritative unfiltered convergence pass before completion. A zero count from a filtered request cannot certify the whole account. Test at least three chunks, mixed route/row/origin caps, and reordered origins.

### AE-02 — Automatic continuation ignores server terminal stops

Locations: `frontend/src/pages/ResourcePlanner.jsx:3478–3506`; `src/travian_api/web/routes/distribution.py:2624–2720, 9868, 9920–9990`; `src/travian_api/web/operation_gate.py`.

The browser checks `stopped_early`, `gold_club_blocked`, and `outstanding`. None is defined on `ExecuteResponse` or supplied by the final execute response. The local values written to the trace are not response fields. The browser therefore cannot receive these stop signals through this API contract.

Reproduction: exhaust the activity budget during the first marketplace read, with a second origin still unvisited. The actual response reports an early-stop problem, `unswept_origins=[20011]`, and a positive next-chunk delay (117 seconds in this run), but all three expected fields are absent. A browser test using this response shape sends a second automatic request after the wait.

The browser only special-cases one marketplace-unsettled message. Its unchanged-count stall guard does not apply while unswept origins remain. Consequently some persistent failures can keep requesting chunks; this is a source-derived risk, not an observed infinite production loop. Budget preflight can still reject the next request: this finding does not mean budget enforcement itself is bypassed.

Captcha stop semantics amplify the problem: signals concern an in-flight operation, fresh operations do not inherit older signals, and unregistering the last operation clears the signal. Automatic continuation must not accidentally become the fresh user-authorized restart.

Required fix: serialize a typed terminal status and stop reason consistently on all exit paths. Stop automatic continuation on terminal or unresolved outcomes; require explicit operator resume. Test budget, captcha, Gold Club rejection, read failures, and uncertain writes through the real HTTP response model, not only frontend mocks with invented fields.

### AE-03 — Invalid marketplace data is interpreted as absence of routes

Locations: `src/travian_api/parsers/html_parser.py:716–796, 820–838`; `src/travian_api/services/trade_route_service.py:477–525`.

An entirely missing React model fails closed, but a recognized wrapper containing malformed nested data does not. `_routes_from_view` returns an empty list for missing/non-list trade routes and skips malformed collections/entries. `list_existing_routes` rejects only `None`, accepting these empty or partial results as known inventory.

Four accepted-as-empty cases were reproduced inside the real `TradeRoutes.render` wrapper: `{}`, a null marketplace, null `tradeRoutes`, and a collection with an unaddressable destination containing a route ID. All returned `[]` instead of raising `MarketplaceUnreadable`.

Impact: an unknown existing schedule can be treated as missing, allowing duplicate creation or incorrect replacement decisions. The probes establish the unsafe read boundary; they do not establish how often the game returns malformed models.

Required fix: distinguish a validated empty marketplace from malformed or partially parsed inventory. Reject an inventory with missing required structure, invalid identities, or unparseable rows before any mutation. Test mixed valid/invalid collections as well as entirely malformed data.

### AE-04 — Wrong-village inventory is accepted before mutations

Locations: `src/travian_api/services/trade_route_service.py:339–378, 418, 477–525, 562`; execute initial read around `distribution.py:7302`.

The initial HTML read pins the requested village in URLs but does not validate the returned model's current village. The later GraphQL refresh does check `ownPlayer.currentVillageId`; that check occurs too late to validate the inventory used for initial write decisions.

Reproduction: request village `20003`, return a well-formed model with `currentVillageId=99999` and route `98765`. `list_existing_routes(20003)` returns route `98765` without complaint.

Impact: reconciliation can classify another village's routes as this origin's existing schedule. Toggle payloads identify route IDs without a source-village field, so the initial read boundary matters even when create payloads contain a source ID. Actual game acceptance of a cross-context toggle was not tested.

Required fix: verify requested village and available account identity before accepting initial inventory. Refuse redirects/context mismatches before any write. Share strict validation between initial read and readback.

### AE-05 — Update cap leaves stale cargo but reports no unfinished work

Locations: `src/travian_api/web/routes/distribution.py:8181–8204, 9946`; browser completion around `ResourcePlanner.jsx:3533`.

When the update cap is spent, the executor records a deferred trace decision but emits a `skipped` action and does not add the update to outstanding/deferred work. Existing route timing is therefore considered satisfied even though requested cargo remains wrong.

Reproduction: two origins each have a complete active six-hour schedule, carrying crop 999 where the plan requests 100. Set `update_drifted=true`, cap updates at one, and visit both origins. One schedule is corrected; the other still carries 999. Response: `remaining=0`, no unswept origins, no problems, and an action detail `route active, cargo stale (update cap reached)`.

The detail is truthful, but the aggregate completion contract is not. With this response the browser's completion predicate can report success. This is not the intentional behavior of leaving cargo alone when `update_drifted=false`.

Required fix: track pending updates separately or include every pending mutation in the completion contract and continuation logic. Verify final cargo, not just route existence. Add update-only, mixed-create/update, and multi-chunk cargo convergence tests.

### AE-06 — Trace persistence fails open

Locations: `src/travian_api/services/distribution/execution_trace.py:130–160`; `src/travian_api/web/routes/distribution.py:7055` and pre-mutation trace calls.

The executor checks whether the trace initially opened. Later write/flush failures are logged and swallowed; they do not make the trace unavailable to the executor. This undermines the write-ahead evidence used for diagnosis and recovery.

Reproduction: open an isolated trace normally, then replace its handle with one raising `OSError('simulated disk full')` on every write/flush. The real executor with a fake game service still creates one route, returns no problems, and leaves a zero-byte trace.

Required fix: expose persistence health and require successfully persisted intent before each new mutation. If a failure occurs after a game write, report that outcome honestly and stop further writes; do not claim the write rolled back merely because logging failed. Non-throwing post-action logging can be intentional, but permitting the next mutation without durable evidence is a separate decision.

### AE-07 — Lock scope does not exclude another service/process

Location: `src/travian_api/services/trade_route_service.py:330`; `src/travian_api/web/routes/distribution.py:7203`.

Each service constructs its own `asyncio.Lock`. Two service instances, even sharing the same fake HTTP client, can hold their execute locks simultaneously. A production process and a debug process also cannot share this in-memory lock.

Impact is conditional: if two instances can execute against the same game account concurrently, both may read old inventory before either creates or reconciles it. This review demonstrated lock independence, not a full concurrent duplicate-write reproduction or an actual production collision.

Required mitigation: enforce one live executor per game server/account, including debug instances. For multi-process support, use an account-scoped durable lease with ownership/fencing and crash recovery. Test two sessions/workers against shared marketplace state.

## Safeguards present and intentional limitations

- Explicit live intent, global/service live gating, feasibility and reconciler-verification gates, per-service mutual exclusion, bounded writes, pacing/budget checks, canary constraints, and post-write readback are present. They remain useful; the findings concern gaps around them.
- Existing tests cover many partial-write, failed-disable, readback, replacement, and restoration scenarios. Refusing to blindly restore an uncertain or partially successful replacement is prudent: restoration could duplicate shipments.
- “Stop after this chunk” is not an immediate rollback or cancellation guarantee. An accepted in-flight mutation may finish. Treat timeout/disconnection as an unknown outcome until history and fresh inventory establish what happened.
- Destination protection is not proof of account-wide conservation. Preserving hand-managed routes can leave extra resource consumption outside the generated schedule. The API describes protection primarily as preventing disabling; this review does not classify every re-enable of a protected destination as a confirmed contract violation.
- Plan feasibility does not certify storage survival, account freshness, or absence of concurrent manual changes. Preview/canary and later readback reduce risk but cannot guarantee zero loss.
- No claim is made that automation is permitted by the game or free of account-enforcement risk. This review concerns application correctness, not permission to automate.

## Additional verification still needed

These are open coverage questions, not additional reproduced bugs:

- Browser navigation/unmount while a sweep is waiting; whether hidden work continues without reachable stop controls.
- Editing execution controls during a sweep, especially destination protection and write caps; verify either freezing or explicit invalidation of every safety-relevant setting.
- Account/session replacement during an active request and server-side binding of submitted snapshot to current account/freshness.
- Process termination between disable, create, readback, and trace flush; explicit operator recovery from each durable state.
- HTTP timeout followed by retry while the original request is still active; recovery by stable run identity rather than assuming no write occurred.
- Realistic partial/malformed game responses, delayed visibility beyond stabilization attempts, and concurrent manual edits. These require controlled fixtures or an explicitly authorized isolated canary, not broad production execution.

## Verification evidence

Existing focused suite:

```text
uv run --extra dev --extra web pytest tests/test_distribution_execute.py tests/test_trade_route_footprint.py tests/test_execution_trace.py tests/test_trade_route_payload.py tests/test_trade_route_reconciler_gate.py tests/test_distribution_http_contract.py -q --tb=short
442 passed, 1 warning in 6.00s
```

The warning is a Starlette/httpx test-client deprecation, not an executor failure.

Temporary diagnostic probes:

```text
uv run --extra dev --extra web pytest tests/test_executor_review_probe.py -q -s --tb=short
10 passed in 1.05s

cd frontend
npx playwright test executorReviewProbe --workers=1
2 passed (11.7s)
```

**These 12 probes assert the observed defects/lock limitation, not desired safe behavior. Passing them confirms the findings, not production readiness.** They used existing isolated test fixtures and fake game services; browser requests were intercepted. Early probe iterations required correcting a fixture marker/import and moving budget exhaustion from preflight into the origin read to exercise the intended stop path.

Temporary Python and Playwright probe files were removed after the review, leaving documentation only. The reproduction recipes above describe their setup and observed results. A fix should introduce permanent tests asserting the opposite, safe outcomes.

## Release gate recommendation

Do not enable unattended automatic reconciliation yet. Fix AE-01 through AE-05, make trace loss visible and fail closed before further mutations, and enforce a single live executor per account. Then require:

1. Regression tests for every reproduced case through the real response schema.
2. Multi-chunk end-to-end convergence: every desired schedule and cargo matches, no unintended active rows, no pending/unverified work, and no false COMPLETE status.
3. Fault injection at every write/readback/trace boundary with no automatic continuation after terminal stops.
4. Fresh preview reviewed against current marketplace inventory, followed only by an explicitly authorized, tightly scoped canary and verified recovery.

Even after these gates pass, describe the feature as bounded and verified under tested conditions—not universally safe. A running, healthy web process and a large green test suite do not certify this automatic execution path.
