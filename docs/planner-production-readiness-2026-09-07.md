# Planner production-readiness verification

## Decision

Do not enable unattended execution yet. The merged code has passed offline
verification and the production backend was restarted on September 8 to load it.
The actual account's fresh plan and a supervised canary remain unverified.

## September 8 restart update

- Restart explicitly authorized by the operator. The first Ctrl+C signal was
  ineffective; a Ctrl+Break signal to the verified production-only console
  produced a graceful shutdown. No forced termination was needed.
- Old production log confirms `All sessions disconnected` and Uvicorn confirms
  `Application shutdown complete`.
- New production PID: 39932, started September 8 at 10:07:57 local time. Startup
  completed at 10:08:01. The source matches the previously verified merged main.
- The frontend was not rebuilt and no route configuration was changed. Debug
  port 8001 was left alone (it had already been restarted separately).
- New logs: `C:/Users/tomerc/.travian/uvicorn-80-20260908-100757.out.log` and
  `C:/Users/tomerc/.travian/uvicorn-80-20260908-100757.err.log`.
- Old logs show repeated unauthenticated status polling (401), not a confirmed
  server crash. Missing, expired, or invalid app credentials can explain those
  responses; the exact cause of the operator's instability report is unproven.
- All 36 checks over approximately one minute returned their expected status:
  200 for the app and planner page, 401 for unauthenticated `/api/users/me`.
  Average response time was 24.2 ms; maximum was 215 ms. This is a short-term
  check, not proof of long-term stability. Authenticated game-session behavior
  still requires the operator to sign in.

## Pre-restart deployment evidence (September 7)

- Remote main is `09b2bfa1e8e229e9b12f40dc04b83cd9a5e1cea7`, the merge of PR #73.
- GitHub reports the PR merged and the merge commit's CI successful:
  https://github.com/tomer60300/travian-auto-player/actions/runs/34136696027
- The shared checkout remains on `perf/test-suite-speed`. Its application code,
  frontend code, and tests match merged main; only a review-document difference
  exists among tracked files. No checkout switch was performed.
- Production port 80 is owned by Python PID 30196, started September 6 at
  14:35:39 local time. Debug port 8001 is owned by PID 23188 with the same start
  time. Neither command uses `--reload`.
- The distribution route module was last modified September 7 at 11:14:50.
  The app imports that module at startup. This is strong evidence that the
  running backend has not loaded the new execution/undo changes; it is not a
  live in-memory code fingerprint.
- Production returned HTTP 200 for the index and planner JavaScript asset;
  both match the files on disk. The planner asset contains the new whole-day
  confirmation and incomplete-undo warning. Frontend freshness does not prove
  backend freshness.
- An unauthenticated history request returned HTTP 401.

## Offline verification

- Exact merged main tested in an isolated detached worktree: **3,840 passed,
  6 skipped**, with eight existing Starlette/httpx deprecation warnings.
- Frontend unit tests: **678 passed** across 21 files.
- Ruff lint and formatting pass. Frontend lint: zero errors, 16 existing warnings.
- Full functional browser suite: **533 passed in one run** (9 minutes, four
  workers). The separate login screenshot project was not run.

Relevant coverage includes explicit preview/live consent, zero-network preview,
server-enforced canary limits, merchant budgets and dropped allocations,
unverified-markup refusal, repeated-run convergence and idempotence, protected
routes, read-back disagreement, failed writes, partial replacement, deferred
creates, freshness expiry between chunks, account switching, and attributed undo.
Algorithm tests include all 512 three-by-three exclusion graphs and exhaustive
eligible-cycle combinations for randomized small scenarios. These do not certify
global optimality or reproduce all game behavior.

Backend tests isolate databases, credentials, traces, and external network access.
Browser tests use mocked endpoints. They are not live-account acceptance tests.

The live API deliberately gates on `DistributionPlan.is_feasible`, not every
storage outcome. Overflow and crop depletion can remain warnings even when a plan
is executable. The 30-minute freshness guard is client-side in the planner UI;
an external scheduler calling the live API does not inherit it automatically.
Such a scheduler needs separate preflight and stop-condition validation before
it should be treated as unattended-safe.

## Live-account checks that remain

- No browser connection was available through the browser skill. No cookies,
  session credentials, or browser storage were extracted as a workaround.
- The five most recent local execution traces inspected are from August 25–26.
  None carries the new account-bound, verified undo attribution. They do not
  demonstrate that the merged execution path has worked on the production account.
- Fresh account snapshot, saved profile, NPC assumptions, day/night stock
  trajectory, current routes, and free merchants have not been validated here.
- No canary was sent, so game read-back and an actual delivery are unverified.

## Required before widening execution

1. Completed September 8: authorized production restart loaded the tested source.
2. Make the signed-in planner available, or provide its exported setup and fresh
   snapshot. Review the exact intended profile and a dry preview/day check.
3. Leave stale-state overrides off. Resolve critical storage/merchant findings
   and confirm whether the schedule relies on attended NPC operations.
4. Separately authorize a supervised built-in canary: one origin/destination,
   bounded rows, no disabling, cargo rewriting, or pruning of existing routes.
5. Verify route contents, merchant occupancy, one actual delivery, and the new
   account-bound trace. Expand gradually only after those checks pass.

Undo is not transactional rollback: changed pre-existing routes may require manual
restoration, and concurrent manual route editing cannot be locked by the app.
Storage warnings and local-search limits are intentional. A clean test run or
successful merge is not a promise of production account safety.

No source edits, configuration changes, deployments, service restarts, or live
Travian requests/writes were performed in the September 7 verification pass. Read-only local
HTTP, process metadata, trace metadata, and GitHub checks were performed.
The isolated test worktree and its temporary dependencies were removed after
verification; the original checkout and pre-existing review artifacts were kept.
