# Automatic material relay discovery — profile and design

**Status:** design only. Nothing here is implemented.
**Question asked:** how hard is it to let the optimizer *find* material relays, keeping
the declared ones working?
**Short answer:** much less hard than the module docstring implies, because roughly
70% of it already exists and runs in production for crop. The difficulty is not the
search. It is determinism, and the interaction between a discovered hub and a declared
one.

## 1. What already exists

Do not rebuild any of this.

| Piece | Where | State |
|---|---|---|
| Relay **move** inside the local search | `_improve_flows`, the crop relay branch | works, crop only |
| Candidate hubs from operator **consent** | `relay_hub_candidates` | works, derived from the crop plan |
| Eligibility predicate | `_may_relay_through` | works; only its last inference is crop-specific |
| Single-hop **shape check** | `_crop_shape_ok` | works, and is **not crop-specific** — pure graph logic over an edge set |
| Adoption on a **budget breach** | objective `(over_budget_excess, …)` | works — excess is the first lexicographic key |
| Route selection for relief | `_budget_relief_withdrawals` | works, declared relays only |
| Two-leg construction | `_relay_tier_flows` | works, declared relays only |
| **collect-then-ship** beat ordering | `schedule.build_beat` | works, **already generalised to materials** |
| Warehouse pass-through bound | `TestTheRelaysWarehouseMustHoldThePassThrough` | works, and supersedes the old objection |

Two of those deserve emphasis because they are the reasons this was deferred before.

`_crop_shape_ok` reads an edge set and rejects a two-way pair and a hub feeding a hub.
There is nothing about crop in it. Called with material edges it enforces the material
waterfall rule as written.

The old objection to material relay was storage: a neighbour with a 160,000 warehouse
would fill on the capital's flow inside a night. That assumed the relay does not forward
while it collects, which `schedule.build_beat` fixed for materials. The real law is
**pass-through rate × the longer of the two cycles**, and it is already tested.

## 2. Why materials were excluded, and how much of that reason survives

The invariant (`optimizer.py:14-29`): no material village both sends and receives the
same resource, so `A -> B -> C` cannot form. It comes from the data model — `allocation`
nets each village to one figure per resource — and is asserted in tests rather than
defended at runtime.

**It is already relaxed.** The declared tier states: no material village both sends and
receives *except a village the operator declared as a relay, and no relay feeds a relay*.
So the shape is legal today; what is missing is permission to reach it without a
declaration.

That moves the question from "is this shape sound?" — answered, in production — to
"who consents to a village becoming infrastructure?"

## 3. What makes a good relay, mapped onto what exists

Your criteria, and where each already lives:

| Criterion | Status |
|---|---|
| Not a role village | **exists.** `_may_relay_through` reads `may_relay`, then the role; feeder forwards, every other role does not, capital is explicitly no |
| Has merchants to staff it | **partly.** The crop path checks `merchant_count > 0`. Materials should test *idle* merchants against the village's own budget, not mere existence |
| Warehouse can hold the pass-through | **exists**, as the rate × longer-cycle law |
| Saves merchants / time | **exists.** A move is adopted only when the lexicographic objective strictly falls |
| Operator can still pin one by hand | **exists** and is untouched — the declared tier runs outside the search |

So the eligibility predicate is mostly assembled. The new part is idle-merchant headroom,
which is the criterion that actually distinguishes a *good* relay from a legal one.

## 4. The design

Generalise the crop relay move to a resource parameter, and gate materials on the same
guards crop already passes.

**4.1 Candidates.** Mirror the crop derivation: villages named in a *material* plan with
`mode is not KEEP`. This is the consent model and it is not negotiable — known issue #10
records what happened when the candidate set was widened to the whole account: a village
nobody had mentioned was conscripted the moment a relay through it priced marginally
cheaper, putting 27,000/h of someone else's crop into a granary nobody had sized. KEEP is
not a naming; `resolve_resource` defaults every readable village to KEEP.

**4.2 Eligibility.** A materials variant of `_may_relay_through`:

- unreadable rate → refuse first, before any declaration is consulted;
- `may_relay` per village, then the role default (unchanged);
- **new:** idle merchants — `budget(v) - committed(v)` must cover the trunk's cost;
- **new:** warehouse headroom against the pass-through law.

The crop-solvency inference is dropped; the material equivalent is warehouse headroom.

**4.3 Shape.** Call the existing shape check per material resource, from **both** the
relay move and the 2×2 swap. Guarding only the move is not enough — the crop code learned
this: once a hub exists an ordinary swap can rewire its legs into a chain, and swaps know
nothing about relay.

**4.4 Precedence with the declared tier.** The declared tier is built by construction
*outside* the search and merged afterwards. A discovered hub must therefore never be a
declared relay, a declared relay's downstream, or an origin the declared tier will serve.
Simplest correct rule: **declared wins** — remove every village touched by `relay_for`
from the discovered candidate set before the search starts.

**4.5 Reporting.** `relay_hubs(routes, material_relays=relay_for)` already computes hubs
from routes. Discovered hubs need to be distinguishable from declared ones in the plan
output, so the operator can see what the planner decided on their behalf.

## 5. Profiling

Measured 2026-09-03 on this machine, crop relay scan alone, median of five warmed runs:

```
random_account(5), 40 villages:  4.5s over 33 candidates (crop plan)
                                 5.8s over 40 candidates (whole account)
random_account(7), 21 villages:  0.09s over 19,  0.09s over 21
```

The important reading is counter-intuitive and is recorded in the source: **narrowing the
candidate set was ~20% faster at 40 villages and a wash at 21**, because runtime is
dominated by which relays the search *finds* and how far it then chases them — not by how
many candidates it scanned.

That is the profiling result that matters here. Adding three material resources does not
multiply cost by three on candidate count; it multiplies it by how many *additional
improving relays exist*. On a tight account that is few. On the operator's account, where
several villages are far out and capped, it could be many — and the scan is already 4.5s
at 40 villages, against a full-suite budget where `test_distribution_audit.py` alone is
123.5s of CPU.

**Mitigation to design in from the start, not bolt on:** cap discovered material relays
per plan (one per over-budget origin is enough to answer the question that motivates
this), and reuse the existing "one best relay per scan" structure rather than adopting
every improving hub.

## 6. Effort

Assuming someone who knows this module.

| Component | Size | Notes |
|---|---|---|
| Resource-parameterise the relay move | **M** | mechanical; the move already takes a flows map |
| Material candidate set | **S** | mirrors the crop derivation |
| Materials eligibility predicate | **M** | idle-merchant and warehouse headroom are new |
| Shape check on the swap path too | **S** | function exists; call it per material resource |
| Declared/discovered precedence | **M–L** | the fiddly part; two mechanisms answering one question |
| Plan output distinguishes the two | **S** | |
| Search-cost cap | **S** | |
| **Tests** | **L** | the declared tier alone is ~1,440 lines |

Realistically **3–5 focused days**, of which more than half is tests. Not a rewrite of the
optimizer — a generalisation of one existing move plus one new guard.

## 7. Risks, worst first

**Determinism is a correctness requirement, not a nicety.** `/execute` acts on the DIFF
against live configuration: it deletes and recreates real Gold Club rows. A discovery that
flips between two equally-good hubs on unchanged input is *destructive*, not merely
surprising. Every tie-break must be coordinates, as the crop path already does, and a
re-plan on unchanged input must be byte-identical. This wants a dedicated test, not a
line in an existing one.

**Conscription.** Known issue #10 in material form. The consent rule in 4.1 is what
prevents it, and it is the rule most likely to be "simplified" by a later change.

**Two mechanisms, one question.** Declared and discovered relays can disagree. The
precedence rule keeps it decidable; without it, both could serve the same downstream.

**Documented boundary moves.** `optimizer.py:84-86` states the planner does not discover
a material relay. That sentence, profile section 3.5, and the invariant statements in the
tests all become wrong on the day this ships — and a stale comment in this area has
already cost one incorrect issue document (see
`planner-merchant-cap-not-constraining.md`).

## 8. Suggested staging

1. **Materials eligibility predicate + candidate set**, with no move wired up. Testable
   alone, and it is the part carrying the operator-consent semantics.
2. **The move**, behind the existing per-plan relay cap, discovered hubs reported
   distinctly.
3. **Determinism gate** — re-plan identical on unchanged input, across the audit fixtures.
4. **Precedence with the declared tier.**

Stage 1 answers whether the criteria pick sensible villages on the real account, before
any routing changes. If the answer is no, the rest is not worth building.
