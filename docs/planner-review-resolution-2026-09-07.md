# Resource planner review resolution

Branch: `fix/resource-planner-algorithm`.

Scope: resolve the review findings unless the behavior is an intentional,
documented product rule. No production deployment or game mutation was performed.

## Unintended behavior fixed

| Finding | Resolution |
| --- | --- |
| Undo claims later runs' routes | Live undo uses a recorded post-run inventory and exact create matches from the selected run. Later additions are excluded. Subsequently edited created routes are refused, not deleted. |
| History/undo cross account boundaries | New traces carry a web-user and opaque server/login binding. History filters that binding before its limit. Undo refuses foreign or legacy/unidentified traces before reading the game. Switching accounts clears cached history, undo instructions and confirmations; late history/undo responses are discarded. |
| Whole-day reconciliation claims it only switches routes off | The panel now discloses provisioning, re-enabling, cargo updates, and pruning. Whole-day reconciliation requires an explicit in-app confirmation. The existing whole-day provisioning behavior is retained. |
| Reconciliation bypasses snapshot freshness | The same 30-minute freshness rule used by normal live execution is checked before every chunk; the explicit stale-state override is retained and can be revoked. Account/input changes stop further chunks. |
| Greedy false shortage | Residual reassignment repairs constrained demand; implemented in the previous commit, with independent min-cut checks. |
| Late night derivation overwrites newer input | Account, planning-input, and fill-setting revisions are checked before applying the response. Stale failures are also discarded. |
| Undo reports success despite restoration remaining | Both the response and the UI account for outstanding restoration. Successful confirmed deletions can now produce a genuinely clean response; cargo changes to old rows require manual restoration too. |
| Sweep reports completion with work remaining | Deferred creates, unresolved outcomes, failures and cancellation no longer become a complete result merely because every origin was visited. Origins are counted uniquely; deferred creates remain visible. |

Deleting also implies a confirmed disable first. A failed disable prevents the
subsequent delete. Route content is checked again against the recorded inventory
after the disable read-back, before deletion.

## Intentional rules retained, not silently replaced

### Partitioned merchant fleets

`docs/25-resource-distribution-planner.md`, “What a village may shed is a
partitioned-fleet bound,” explicitly dedicates merchants per destination and
uses the sum of per-route whole-merchant commitments. This is also the model
behind `night_profile.shed_limit` and `partitioned_fleet_limit`.

The example where two staggered short trips could share one merchant demonstrates
conservatism relative to a pooled-fleet model, not a violation of this specified
model. The branch does **not** lower live merchant requirements based on an
unimplemented pooled scheduler. Pooling would be a separate product/model change.

### Local search rather than a global optimization certificate

Section 14 explicitly specifies route-cycle sweeps and local improvement, with a
MILP solver deferred. The earlier commit fixes the known pairwise traps by adding
three-route rotations, and fixes cycle-budget choice with an exact per-origin
dynamic program over its eligible cycles. Search exhaustion is reported.

These changes do not make the whole multi-resource, temporal problem globally
optimal. That guarantee was never the implemented contract; it must not be claimed.

### Storage warnings rather than automatic rewrites

Section 13 says to alert on divergence rather than silently replan.
`DistributionPlan.is_feasible` deliberately excludes storage outcomes because an
operator may intentionally stockpile; `assess` separately exposes critical
findings and distinguishes executable from clean. This separation is retained.
No automatic reroute/rephase loop is introduced on the strength of a warning.

## Recovery compatibility and residual limitations

- Old trace files are preserved, but traces without account binding and verified
  attribution are not eligible for automatic undo. Inspect those runs manually;
  do not retrofit ownership or infer it from overlapping village IDs.
- History reports the currently connected account's bound runs. A disconnected
  or unidentified session receives no history, without reconnecting to the game.
- Restoring changed pre-existing rows remains manual. Undo reports their original
  values instead of silently replaying an old schedule over later operator edits.
- Matching a game's read-back is not a transaction identifier from the game.
  Concurrent manual changes that are identical to an app create cannot be
  distinguished from that create. Avoid concurrent route editing during execution
  or undo. The application execution lock does not lock the Travian website.
- Existing game-mechanics assumptions and snapshot freshness still limit real
  outcomes. Passing local tests is not a guarantee against production account risk.
- The repository's `/codex:review --wait` gate is unavailable in this session;
  an independent review remains outstanding before merge/deployment.

## Verification

- Final full backend suite: **3,264 passed, 6 skipped**, with eight existing
  Starlette/httpx deprecation warnings (183 seconds in an isolated worktree).
- Frontend unit suite: **678 passed** across 21 files.
- Final affected browser run: **58 passed**, including account-switch history,
  night derivation, freshness, confirmation, undo, responsive and contrast checks.
- Earlier full functional browser run: 524 passed and seven failed. Three stale
  expectations were corrected (existing setup schema version and incomplete-sweep
  reporting); four timeouts passed when rerun without heavy contention. All
  affected files then passed together: **34 passed**. This was not a single green
  full-suite run; the separate login screenshot project was not run.
- Ruff lint and format checks pass. Frontend lint has no errors and 16 existing
  warnings outside the modified planner. Git whitespace checks pass.
- Regression evidence includes a reproduced late-account history response,
  unsafe unstable-inventory undo, missing manual cargo restoration, and a route
  disappearing during disable confirmation; their tests pass after the fixes.

Tests use isolated backend state and mocked browser endpoints. No production
frontend build, service restart, deployment, or live Travian action was performed.

The shared checkout switched to `perf/test-suite-speed` during verification.
Only the planner commit was copied to the dedicated bugfix branch in an isolated
worktree; the unrelated performance changes were excluded. Backend checks were
repeated there. Application and frontend files match those verified previously.
The shared checkout and its commits were preserved.
