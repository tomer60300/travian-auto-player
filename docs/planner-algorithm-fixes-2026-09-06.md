# Planner algorithm fixes — 2026-09-06

Branch: `fix/resource-planner-algorithm`.

This implements the reproducible routing and cycle-selection defects from the
algorithm investigation. It is not a production-safety certification and does
not resolve every finding in the earlier feature-wide review.

## Implemented

- **False shortages under exclusions:** residual augmenting paths reassign
  previously served flexible demand before reporting a shortage. The direct
  transportation stage maximizes served cargo within its permitted supplier
  graph (subject to the existing floating-point tolerance). Merchant feasibility
  is still a later constraint. The four-village counterexample now delivers the
  previously missing 80 lumber/hour without violating the exclusion.
- **Pairwise local minima:** after full and breakpoint pair swaps and crop
  relay moves, search both coordinated three-route rotations. The hourly
  counterexample drops from 10 merchants to 8; the daily-cycle example drops
  from 8 to 6. Every accepted move improves the existing lexicographic cost;
  exclusions and crop-relay shape checks still apply.
- **Latency spending:** replace greedy benefit-per-merchant choices with a
  per-origin multiple-choice dynamic program. Among original and eligible
  shorter cycles, minimize target violations, then total latency, then merchant
  commitments. Original cycles remain eligible; shorter cycles must meet fill
  and cadence constraints. Soft budgets remain binding. The reported example
  selects cycles `(1, 4, 1)`, costing 10 merchants with summed latency 10.75h,
  instead of `(3, 3, 1)`, costing 9 with 11.75h. This explicitly prefers lower
  latency within budget; it is not an improvement on every objective at once.
- **Declared-relay accounting:** price immutable tier cargo in the local search,
  including bundling on existing pairs, without letting search rewrite the tier.
  Merge that fixed cargo into the returned route set only once.
- **Tie-breaking:** use geography followed by fleet size, Trade Office level,
  and name to distinguish otherwise co-located synthetic villages without
  relying on IDs. Real villages on distinct coordinates retain geographic order.
  This is not a graph-isomorphism guarantee for identical synthetic records.
- **Bounded search:** cap the cubic rotation neighborhood at 200,000 triples
  across a search. Exhaustion produces `SEARCH_TRUNCATED`; it never claims all
  improvements were exhausted. Existing improvement-pass limits remain.

## Verification

The three initial regression tests were observed failing before implementation.
The dedicated regression suite also covers the daily-cycle counterexample,
fixed-tier budget pressure, and rotation-limit reporting.

Independent checks:

- All **512** three-supplier/three-receiver allowed-edge graphs, with uneven
  supply and demand, match a separately calculated minimum-cut bound. Checks
  also assert conservation, supply/demand ceilings, and excluded-edge avoidance.
- **75** randomized three-route latency problems match exhaustive enumeration
  of eligible cycle combinations under their merchant budgets.
- Re-running the original transportation audit over **3,000 hourly-only** and
  **3,000 daily-cycle** cases found **zero** cheaper merchant-count or route-count
  witnesses among the 21 enumerated transportation matrices per case. Before
  these changes, the same families had 16 and 15 cheaper merchant-count witnesses.
  The daily-cycle audit is a restricted flow-grid comparison, not a proof over
  all fractional rates, resource mixes, or schedules. The original audit JSON
  remains historical evidence and was not overwritten.

Full backend run: **3,244 passed, 6 skipped** (200 seconds; only existing
Starlette/httpx deprecation warnings). The final dedicated regression file has
**8 passing tests**, including two cases added after that full run. Repository
Ruff lint/format checks and a focused mypy check of the optimizer also pass.
After the final edits, the regression, optimizer, and declared-relay suites
passed again: **202 passed**.

The existing relay-own-production regression now explicitly excludes its
upstream source from the separate destination. Without that constraint the new
residual repair legitimately reroutes the relay's own production downstream,
so the old fixture no longer forces the scenario its assertion is testing.

## Still open — do not claim all optimization problems are solved

- **Pooled merchants:** accounting still reserves each route's peak independently.
  A phased pair of short trips may physically share a merchant while this model
  reserves two. No execution gate was relaxed. Replacing reservations requires
  exact simultaneous occupancy checks over integer dispatches, return times,
  midnight wrap, and day/night profile boundaries, plus corresponding changes
  to execution and budget reporting. This limitation remains unresolved here.
- **Joint scheduling/storage:** route choice, beat placement, integer cargo, and
  storage replay are still staged. There is no search jointly optimizing all
  of them, and storage warnings do not trigger a global reroute/rephase solve.
- **Global optimality:** local search still has bounded neighborhoods; larger
  coordinated moves, fractional three-route transfers, and different unused
  supplier choices can escape those neighborhoods. A converged result means
  only that its implemented, untruncated neighborhood had no improvement.
- **Feature-wide production risks:** the original undo attribution/account
  isolation and asynchronous UI/reconciliation findings are outside these
  algorithm changes and remain listed in the original review. These fixes must
  not be read as approval to run the feature unattended on a production account.

No frontend build, service restart, game request, or live route mutation was
performed as part of this implementation.

The repository's `/codex:review --wait` delivery gate is not exposed in this
session, and no corresponding repository command was found. It has not been
run; the passing tests and local inspection are not a substitute for that gate.
