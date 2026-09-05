# Resource Distribution Planner — Profiling, Review & UI Design

**Project:** `travian-auto-player` · **Server:** Europe 2, x1, 3 tribes · **Account:** `Chieftain` (Teutons)
**Status:** built and running. Part I is the operator's profiling doc, Parts II and III are a technical review of it, and **Part IV records what the code actually does** wherever it has moved past them.

---

# Part I — Profiling *(operator)*

## 1. Purpose

Replace manual resource-distribution planning with a tool that:

1. reads current per-village production and state,
2. takes role-based allocation targets from the user,
3. computes a minimal-merchant, minimal-latency set of trade routes,
4. assigns dispatch/arrival timing so no hub overflows,
5. outputs a copy-paste setup sheet (later: applies routes in-game).

Everything is expressed in **hourly rates**. All planning is read-only against the game at first; an "apply" path is a later phase.

## 2. Domain model

| Entity | Fields |
|---|---|
| **Village** | id, name, x, y, role, net production (W/C/I/Cr), current stock, warehouse_cap, granary_cap, marketplace_lvl, merchant_count, trade_office_lvl, priority_flag, allocation targets |
| **ForeignTarget** | x, y, hourly crop owed, safety margin, paying village |
| **Role** | name, allocation template (per-resource mode + value) |
| **Route** | from, to, cargo (W/C/I/Cr per send), cycle_h, dispatch_min, arrival_min, merchants |
| **Plan** | snapshot timestamp, route set, merchant pool per village, warnings, infeasibilities |

**Village roles observed in practice:** feeder (base template), capital/NPC hub, army/hammer village (negative net crop), crop hub, sub-hub, stranded/isolated.

## 3. Verified game mechanics — ground truth

### 3.1 Merchants

| Property | Value | Notes |
|---|---|---|
| Capacity | `Base_Merchant_Size × (1 + 0.2 × trade_office_lvl)` | `Base_Merchant_Size` configurable, default **2500** (`merchants.EUROPE2_TEUTON`) |
| Speed | 12 fields/hour | Teuton |
| Count per village | derived from Marketplace level | not always 20 — observed 13 and 19 |
| Reserve | 2 idle per village (config) | usable = count − reserve |

> ⚠️ **The base was re-read as 2,500 (operator, 2026-09-02) and it *supersedes* the earlier 7,920-at-TO-13 reading rather than reconciling with it. The `+20%` slope has never been re-measured against the new base, so the model is pinned at one end only. Read Review R1 before touching either number.**

### 3.2 Gold Club trade routes

- Cycle length must be a whole number of hours (1–24). No 30-minute or 80-minute cycles.
- One sender village per route.
- **UNVERIFIED:** whether Gold Club caps the number of trade routes per village.

### 3.3 Production data

- The account-wide Production tab reports **GROSS** crop. Using gross as net is a silent, catastrophic error — it inverts the sign on army villages.
- Net production for all villages is obtainable cheaply — see Review R2 for the actual cost and its caveats.
- Trade-in-transit contaminates Capacity-tab readings.

### 3.4 Distance

Toroidal Euclidean over the map wrap:

```
dx = min(|x1-x2|, MAP_SPAN - |x1-x2|)
dy = min(|y1-y2|, MAP_SPAN - |y1-y2|)
dist_fields = sqrt(dx² + dy²)
ow_min      = dist_fields / speed_fields_per_h × 60
rt_min      = 2 × ow_min
```

`MAP_SPAN` is **401** on Europe 2 (−200…+200), proved in Part IV §4.12. The
requirements spec's 801 is the error — do not "correct" the code to it.

### 3.5 Topology rules (user-imposed)

- No waterfall for W/C/I — materials must not chain A→B→C. **Amended:** one hop
  is permitted where the operator has *declared* a relay, and a relay may not
  feed a relay. Nothing searches for a material hop. See Part IV §4.1.
- Crop relay through a sub-hub is permitted.
- Max end-to-end latency target: 2h (configurable); far villages may exceed it when geometry forbids compliance.

## 4. System decomposition

| # | Subsystem | Depends on |
|---|---|---|
| 1 | State & Fetch | — |
| 2 | Allocation Input | 1 |
| 3 | Flow Optimizer | 1, 2 |
| 4 | Schedule Solver | 3 |
| 5 | NPC / Storage / Apply / Monitor | 1, 3, 4 |

Subsystems 1–3 alone replace all manual planning.

## 5. §1 — State & Fetch

```
FETCH-A   per village: net_W, net_C, net_I, net_Cr
FETCH-B   per village: x, y, marketplace_lvl, merchant_count, warehouse_cap, granary_cap
OWNED     trade_office_lvl — seeded by a one-off scan, user-editable, defaults to 0, never rounds up
CONFIG    base_merchant_size, speed_fields_per_h, merchant_reserve, max_latency_h, map_span
DERIVED   merchant_cap, dist, ow_min, rt_min
```

### 5.2 Trade Office handling — rounding rule

| Cached TO | Consequence |
|---|---|
| too low | over-provisions merchants. Wasteful, every route still fits. **Safe.** |
| too high | under-provisions. Cargo exceeds assigned merchants, budget breached invisibly. **Unsafe.** |

Unknown TO = 0; all inference rounds **down**.

### 5.3 TO as a decision surface

Show TO alongside what upgrading buys, so the field doubles as "which Trade Office to build next".

Capacity is `2500 × (1 + 0.2 × TO)`, floored — the current model (R1), not the
superseded 2,200. The merchants column sizes §8.1's own worked batch (9,323/h on
a 3h cycle = 27,969 per send) so every figure here can be checked with a
calculator:

| TO | Cap/merchant | `m_send` for a 27,969 batch |
|---|---|---|
| 0 | 2,500 | ⌈11.188⌉ = 12 |
| 1 | 3,000 | ⌈9.323⌉ = 10 |
| 5 | 5,000 | ⌈5.594⌉ = 6 |
| 11 | 8,000 | ⌈3.496⌉ = 4 |
| 13 | 9,000 | ⌈3.108⌉ = 4 |
| 14 | 9,500 | ⌈2.944⌉ = 3 |
| 18 | 11,500 | ⌈2.432⌉ = 3 |

A ladder rather than a per-village snapshot, for two reasons. **A Trade Office
moves `m_send` and never `sets`** — `sets = ceil(rt_min / (cycle_h × 60))` has
no capacity term at all (§8.1) — so one column says everything an upgrade buys,
and the pool follows by multiplying. And the earlier version of this table
carried two hand-typed village rows whose TO levels had *both* already drifted
(R1), which is exactly the drift the nag below exists to catch; a ladder cannot
drift.

**Staleness nag (no request):** if a village's TO is unchanged for N days *and* its routes are over-provisioned relative to neighbours, flag "verify TO?".

### 5.4 Validation on every fetch

- reject all-zero payloads (observed in practice)
- flag villages missing from the response
- flag villages present but absent from OWNED state → new village, prompt for a TO scan
- warn if snapshot age exceeds N minutes
- assert `sum(per-village) == reported account total` per resource

## 6. §2 — Configuration

Open: is `Base_Merchant_Size` per-tribe or per-server? Prefer reading `merchant_count` directly over deriving it from `marketplace_lvl`.

## 7. §3 — Allocation Input

### 7.1 Modes — per resource type per village

| Mode | Meaning |
|---|---|
| `percentage` | share of total account production of that resource |
| `absolute` | fixed net/hour |
| `remainder` | receives everything unallocated. Exactly one village per resource. |
| `sustain + X%` | for negative-net-crop villages: cover the deficit plus X% headroom |
| `role template` | named bundle of the above, applied to many villages |

Mixed modes within one village are normal.

### 7.2 Requirements

- Live unallocated counter per resource.
- Role assignment with per-village override.
- **Display shipped alongside target.** `shipped = target − own_production`. Showing only the target caused the same error twice.
- Slack routed to the `remainder` village; hard warning if none set.
- Layout toggle: current production vs post-distribution retention.
- Per-village `priority_flag` + acceptable-occupied-merchants ceiling.

### 7.3 Foreign targets

`x, y, hourly_crop, safety_margin_pct, paying_village`. Crop only. Payment must run continuously; first payment lands after full one-way travel (cold-start warning). Apply a safety margin. These merchants are permanently committed at the paying village.

## 8. §4 — Flow Optimizer

### 8.1 Cost model

```
ow_min  = dist_fields / speed_fields_per_h × 60
rt_min  = 2 × ow_min
batch   = hourly_cargo × cycle_h
m_send  = ceil(batch / merchant_cap[sender])
sets    = ceil(rt_min / (cycle_h × 60))
pool    = m_send × sets
```

The double ceiling makes `pool` non-monotonic in `cycle_h`. Sweep 1..24 per route; never assume a direction.

Worked example (V10 → V02, 9,323/h, rt 532 min, cap 5,720) — *arithmetic independently re-verified, see R4*:

| cycle | m_send | sets | pool |
|---|---|---|---|
| 1h | 2 | 9 | 18 |
| 2h | 4 | 5 | 20 |
| **3h** | **5** | **3** | **15** |
| 4h | 7 | 3 | 21 |

### 8.2 Hard constraints

| Constraint | Rule |
|---|---|
| Cycle length | integer hours 1–24 |
| Merchants per village | `Σ pool ≤ merchant_count − reserve` |
| Waterfall | forbidden for W/C/I **except one hop through a village the operator declared a relay** (Part IV §4.1); permitted for crop via sub-hub |
| Two-way pairs | never ship the same resource both directions between one pair — net it out |
| Latency | prefer ≤ `max_latency_h`; flag geometry-forced violations |

### 8.3 Objective

1. minimise total merchants committed
2. minimise maximum end-to-end latency
3. maximise free merchants at `priority_flag` villages
4. minimise route count

### 8.4 Escalation ladder

sweep other cycles → reroute via nearer hub → split cargo → recommend TO upgrade (show Δmerchants) → declare infeasible, strand the village.

### 8.5 Hub logic

Neighbours consolidate at a hub; the hub makes the long haul with full merchants. Hub assignment is discrete and interacts with the per-village cap.

## 9. §5 — Schedule Solver

- derive `dispatch_min = (desired_arrival_min − ow_min) mod 60`
- stagger arrivals at each hub ≥ N minutes apart
- enforce collect-then-ship ordering at hubs
- reserve a clear slot for the manual NPC burst
- emit the "hourly beat" table per hub

> ⚠️ **The 60-minute beat cannot represent multi-hour cycles — see Review R5.**

## 10. §6 — Storage Safety

`fill_time_h = (capacity − current_stock) / net_inflow_per_h`; warn under 18h. Special case: a lumpy NPC batch overflows while continuous-rate checks pass — model batches as discrete events.

## 11. §7 — NPC Subsystem

| Feature | Detail |
|---|---|
| Trigger | crop stock ≥ threshold (currently 300k at the capital) |
| Split ratio | configurable, bias mode (e.g. iron-heavy 30/20/50) |
| Post-NPC ship | large manual burst; may need several back-to-back sends |
| Merchant headroom | reserve capacity at the NPC village for the burst |
| Frequency estimate | retention → NPCs/day → gold cost/day |
| Sink-capacity warning | ~93k/h produced vs 48k/h absorbed → surplus accumulates |

## 12. §8 — Output & Apply

**Phase 1:** setup sheet, one row per route: `from | to | cargo | cycle | dispatch | arrival | merchants`, plus merchant pool per village, warnings, infeasibilities. Integer cargo, sum-preserving rounding. Diff against the currently configured route set. Idempotent re-plan.

**Phase 2 (apply):** blocked on the Gold Club route cap. All HTTP through the existing stealth chain; no parallel requests; captcha = immediate clean stop.

## 13. §9 — Validation & Monitoring

Expected vs actual stock trajectory; detect silently failing routes, new/chiefed villages, production drift; alert on divergence rather than silently re-planning.

## 14. Hardness

Multi-commodity min-cost flow with integer cycle and merchant variables plus per-node capacity — NP-hard in general. N=26 is small, so: cluster → assign hubs → per-route cycle sweep → local improvement. MILP (HiGHS/CBC via PuLP) later.

## 15. Known issues & guards

| # | Failure | Guard |
|---|---|---|
| 1 | Shipped the target instead of the gap | assert `shipped == target − own_production` |
| 2 | Two-way pair with opposite cargo | forbid duplicate (pair, resource); net before emitting |
| 3 | Gross crop treated as net | never read crop from the Production tab |
| 4 | Merchant base size wrong (1000 → 1600 → 2200 → 2500) | one injectable `MerchantModel` with `calibrate()`; **nothing else in the planner may hardcode a capacity** |
| 5 | Non-integer cycles proposed | clamp to integer hours at generation |
| 6 | Exceeded the 20-merchant cap | hard constraint + reserve, per village |
| 7 | All-zero export accepted | payload validation |
| 8 | Unroutable village (105 fields, ~55 merchants) | feasibility check → strand |
| 9 | Percentages summed to 96%, slack unassigned | require a `remainder` village per resource |
| 10 | Village count churn 5 → 20 | idempotent re-plan, route diffing |
| 11 | TO unknown for new villages | default 0, flag, never round up |
| 12 | Continuous-rate check passed while a batch overflowed | model NPC batches as discrete events |

## 16. Open questions

| # | Question | Blocks |
|---|---|---|
| 1 | Does Gold Club cap trade routes per village? | apply layer |
| 2 | `merchant_count` — read directly or derive? | §1 |
| 3 | Is `Base_Merchant_Size` per-tribe or per-server? | see R1 — blocks everything |
| 4 | Map span for toroidal wrap on Europe 2 — **answered: 401** (Part IV §4.12) | settled; do not re-open it as 801 |

## 17. Build order

| Phase | Deliverable |
|---|---|
| 1 | §1 State & Fetch |
| 2 | §3 Allocation Input |
| 3 | §4 Flow Optimizer |
| 4 | §5 Schedule Solver |
| 5 | NPC, storage, apply, monitor |

## Appendix A — Regression fixture

The golden fixture (`tests/fixtures/distribution_account.json`, `tests/test_distribution_golden.py`) is a **23-village account that is invented, not captured** — this repository is public, so no real account state belongs in it — pinned at `SEED_TOTAL_MERCHANTS = 233`, `MAX_TOTAL_MERCHANTS = 211`, `MAX_OVER_BUDGET_EXCESS = 52`. It is frozen deliberately (see the note below) and is not the live 26-village account. An earlier draft of this paragraph quoted a 20-village plan at ~115 merchants that no longer exists in the repo. Exercises: an army village with large negative net crop, a `remainder` village, a stranded village, two sub-hubs, a priority village held at 3/20, a foreign crop tribute, sub-20-merchant villages, and unknown TO defaulting to 0.

---

# Part II — Technical review

Findings are ordered by how much damage they do if left unaddressed.

## R1 — The base is 2,500, and the 7,920 reading is *superseded, not reconciled*

The review's original argument — that the profile's `base 2200` / `+20%` had to be mistaken, because published Teuton values are `base 1000` / `+10%` and `+20%` is the Roman rate — was sound reasoning on a false premise: **Europe 2 is not a stock server.** Stock Teuton predicts 2,300 per merchant at TO 13, and the game reported several times that. So the review was wrong to want the number pushed down. It does not follow that the number it was pushed to is right.

Two live readings, and they cannot both describe one model:

| Reading | Fits | Status |
|---|---|---|
| **7,920 per merchant at Trade Office 13** | `2200 × (1 + 0.2 × 13) = 7,920` exactly | **superseded** |
| **base 2,500** (operator, 2026-09-02) | predicts `2500 × 3.6 = 9,000` at TO 13, **not** 7,920 | in code, `merchants.py:102` `EUROPE2_TEUTON` |

Both came off the game, so **something changed between them and the mechanism is not established** — a Trade artifact is one thing that could do it, and one can be captured or lost mid-server. `merchants.py:1-23` records the pair openly rather than pretending it reconciles, and so does this section: 7,920@TO13 is ordered behind 2,500, not explained by it.

What follows from that, and what does not:

**The factorisation is unpinned, and always was.** Any `base × (1 + 13k) = 7,920` fitted the old reading; `2200/0.20` was the natural one and matched the profile's own empirical history, but it was never the only one. The new base is a *base* reading, and the `+20%` slope has not been re-measured against it — so the model is now pinned at one end and extrapolated at the other.

**It is settled at zero request cost.** Read capacity at a **second** Trade Office level. The levels are already held in the snapshot, so this needs one Marketplace send dialog and no extra fetch. `calibrate()` (`merchants.py`) solves both unknowns from two readings, and **prefers a TO 0 village** — its capacity *is* the base, so there is no inversion and the residual error becomes one-sided in the safe direction. Without such a sample it refuses levels closer than `MIN_CALIBRATION_SEPARATION = 3` apart, because the game floors the capacity it reports and close readings make the solve ill-conditioned. A third observation that does not fit is **raised, not averaged in**: a mismatch means capacity is not one account-wide function of Trade Office level, which is exactly the error this finding is about.

**Over-estimating is the dangerous direction.** Too high a capacity plans cargo the merchants cannot carry, routes silently under-deliver, and a feeder or the hammer starves. Too low merely wastes merchants. `MerchantModel.capacity()` therefore floors, and unknown Trade Office levels default to 0 (§5.2).

**The seam is the point.** Capacity lives in one injectable `MerchantModel` with `calibrate()`, and **nothing else in the planner may hardcode a capacity** — a hardcoded capacity anywhere else is a defect, not a shortcut. The frontend's `DEFAULT_MERCHANT_MODEL` (`ResourcePlanner.jsx`) carries the same 2,500 and a loaded setup file overrides it, so the constant only ever applies to an account nobody has described yet.

**§5.3's Trade Office table was stale twice over** — its capacities came off the superseded base, and its two hand-typed village rows listed V16 at TO 0 and V05 at TO 11 when both actually read **TO 13**. That is exactly the drift the §5.3 staleness nag is designed to catch, and the hand-maintained field had already diverged before the tool existed. §5.3 is now a ladder derived from the model rather than a snapshot of two villages.

## R2 — §3.3's "single request" for net production is optimistic

The claim "net production for all villages is obtainable in a single request (solved)" overstates a result that is real but narrower:

- It is **two** requests, not one: `/village/statistics/resources` (stocks) + `/village/statistics/resources/warehouse` (countdown). Net crop is derived, not read.
- Only the **draining** branch is verified. `net = −stock / t` was confirmed on village 20003 to 0.5%. The **filling** branch `net = (capacity − stock) / t` is **unvalidated** and currently disagrees with gross production on village 02 by a wide margin.
- It needs the **Capacity** tab too for filling villages, so realistically **three** requests until capacity is cached.
- The whole path is **Travian Plus gated**. Without Plus it collapses to one `dorf1` per village.

FETCH-A should therefore be specced as *2–3 requests, Plus-dependent, with a per-village `dorf1` fallback*, and the filling branch marked unproven. See `docs/20-resource-production.md`.

Related trap, already burned once: **net crop is `production.l4`, never `l5`.** `l5` reads positive on a starving village.

## R3 — Trade Office fetch research *(the question asked)*

**Answer: there is no account-wide source. The floor is one request per village, and it is a one-off.**

Checked and ruled out:

| Candidate | Verdict |
|---|---|
| Central Village Overview tabs | Overview / Resources / Culture points / Troops only — no building levels |
| "Buildings and Resource Fields Statistics" support page | a static reference calculator of costs and effects, not a view of your account |
| GraphQL | no building fields; `docs/04` and `docs/20` both confirm production/building data is HTML-only |
| Troops → Smithy sub-tab | per-village *research* levels, not building levels |

**But the scan is cheaper than §5.1 implies, and needs no new code.** `parse_dorf2` already returns every village-centre building with its `gid` and level, and Trade Office is `gid 28`, Marketplace `gid 17`. So:

- one `dorf2` per village — **N requests, not 2N** (no `dorf1` needed; both buildings are in the village centre)
- the same sweep yields **Marketplace level for free**, settling open question #2 without a second mechanism
- and warehouse/granary levels (`gid 10`/`11`) if capacity is ever wanted from levels rather than the Capacity tab

At 26 villages that is a 26-request one-off, re-run only when a village is added. Treating TO as OWNED state (§5.2) is the right call; the scan just costs half what the doc budgets.

**Cheaper long-term option, phase 2 only:** once routes are applied, capacity is *observable* — Travian reports how many merchants a given cargo consumed, so `cap = cargo / merchants` recovers it with zero extra requests and self-heals when a TO is upgraded. That also removes the staleness nag in §5.3 entirely. Worth designing toward.

## R4 — The optimizer cost model is correct (verified)

I re-derived the §8.1 worked example independently and it reproduces exactly, including the non-monotonicity:

| cycle | batch | `m_send` | `sets` | pool |
|---|---|---|---|---|
| 1h | 9,323 | ⌈1.63⌉=2 | ⌈8.87⌉=9 | 18 |
| 2h | 18,646 | ⌈3.26⌉=4 | ⌈4.43⌉=5 | 20 |
| 3h | 27,969 | ⌈4.89⌉=5 | ⌈2.96⌉=3 | **15** |
| 4h | 37,292 | ⌈6.52⌉=7 | ⌈2.22⌉=3 | 21 |

`sets = ceil(rt / cycle)` is also right at the boundary: at `rt = 360, cycle = 180` it gives 2, and the first set returns exactly as the third dispatch is due, so 2 suffices. The sweep-don't-assume conclusion stands.

## R5 — MAJOR: the "hourly beat" cannot express the schedule it is scheduling

§9 emits a minute-of-hour table and computes `dispatch_min = (arrival − ow) mod 60`. That is only well-defined when every cycle is 1 hour. A 3-hour route fires once per three hours; a 60-minute table either shows it three times (wrong) or once without saying which hour (ambiguous). The optimizer's own best answer in §8.1 is a 3h cycle, so this is not a corner case.

**Fix — constrain `cycle_h` to divisors of 24: {1, 2, 3, 4, 6, 8, 12, 24}.** Then every cycle divides the day, the beat is a single repeating **24-hour** table, and the schedule is exactly representable. Over the free sweep this costs only cycles 5, 7, 9–11, 13–23; since `pool` is non-monotonic the optimizer can still evaluate them and *report* "a 5h cycle would save 2 merchants but breaks the daily beat", leaving it as an explicit user choice rather than a silent constraint.

Without this restriction the beat's LCM is unbounded and the output table is not a schedule.

## R6 — Dispatch phase IS a free variable (resolved)

§9 assumes `dispatch_min` is chosen. This is correct: per current official documentation ([Trade Routes](https://support.travian.com/en/articles/60-trade-routes)), a Gold Club route has an explicit **Send at / Deliver at** scheduled time plus a separate repeat interval — the phase is a route field, **not** the wall-clock instant the form is submitted. So the planner's `dispatch_minute` maps directly to the route's "Send at" field, and the sheet's column is labelled **Send at**, not "create at".

Consequence for cold-start: `first_delivery_hours` is a WORST-CASE upper bound (a full cycle plus travel, if the route is created just after its send time), used only to bound the manual-coverage window — not a fixed startup. An earlier revision wrongly told the operator to press create at the dispatch clock; that has been corrected.

## R7 — Storage safety only models the filling direction

§10's `fill_time_h = (capacity − stock) / net_inflow` returns a negative number for a village with negative net crop, which is the *starving* case and the one that actually kills troops. It needs the second branch — `empty_time_h = stock / −net` — and the warning threshold applies to both. This is the same l4/l5 trap resurfacing in a different module, which argues for a single shared `crop_status()` helper rather than per-module arithmetic. One now exists in `web/routes/status_export.py`.

## R8 — Routes persist; plans do not

§13 alerts on production drift, but an applied trade route keeps shipping at its configured cargo regardless. If a sender's production falls — troops rebuilt, an oasis lost, a village chiefed — the route drains the sender's stock and the tool only *notices*. Two guards worth speccing now, because they are cheap at design time and expensive to retrofit:

1. size routes against **sustainable surplus with margin**, not the instantaneous snapshot rate
2. make drift beyond X% **trigger a re-plan proposal** with a route diff, not just an alert

## R9 — Smaller notes

- **§3.1 speed 12 f/h is tribe-specific.** Correct for Teutons; if the tool ever handles another account it belongs with the base capacity in a per-tribe table, not in global CONFIG.
- **Open question #4 (map span) is answered: 401.** It was answerable from data already held, and it was answered from a captured create request rather than from coordinates — see Part IV §4.12 for the proof and for why nobody should "fix" it to 801. `docs/05-map-system.md` covers the map system.
- **Appendix A's fixture must be frozen, not live.** The account has already gone 20 → 22 → 26 villages; a golden test that reads current state stops being a regression test.
- **Merchant reserve interacts with foreign tribute.** §7.3's permanently committed merchants must be subtracted *before* the reserve, or a tribute-paying village silently ends up with no idle merchants.
- **§7.2's "shipped vs target" is the highest-value guard in the document** (it caused the same error twice). It should be enforced in the data model — store the gap and derive the target — not left to a UI label.

---

# Part III — UI design

## Principle: encode the invariants in the widgets

Six of the twelve known issues are data-entry or bookkeeping errors. Validation messages do not prevent those; **input types that cannot express the error** do. Three examples that drive the whole design:

| Issue | Structural fix |
|---|---|
| #9 slack unassigned | `remainder` is a **radio group per resource column** — cannot pick two, warns loudly at zero |
| #1 shipped vs target | the grid's **primary number is the gap**; the target is the dimmed secondary |
| #11 unknown TO | TO cells with no scan render as a distinct *unknown* state, not as `0` |

## Layout: five stages, one persistent state

Left rail with five stages; every stage is re-enterable and shares one persisted plan document, so a re-plan never means re-entry (#10).

```
Snapshot → Allocate → Optimize → Beat → Sheet
```

A persistent header carries: snapshot age (amber past N minutes), **request cost of the last refresh**, and a single Refresh button labelled with what it will cost — `Refresh (2 requests)`. Making the cost visible in the button is the whole ethos of this tool.

### Stage 1 — Snapshot

One table, one row per village. Columns: name · coords · role · net W/C/I/Cr · stock/cap with fill-or-empty time · merchants free/total · TO · distance to assigned hub.

- **Net crop is signed and coloured**, negative in red with the empty-time beside it. Never show an unsigned crop rate.
- **Owned vs fetched is visible.** Fetched cells are plain; owned/editable cells (TO, role, priority flag) carry a subtle edit affordance. That makes §5.2's safety model legible instead of documentation.
- **TO cell has three states:** a number, `0 (assumed)`, and `unknown — scan`. The third is not silently treated as 0 in the UI even though the optimizer floors it, so the user can see what the plan is guessing.
- Row action **"Scan buildings (1 request)"** for a single village; a header action for a full sweep with the cost stated (`Scan 26 villages (26 requests)`).
- Validation from §5.4 appears **on the offending row**, not in a list underneath.

### Stage 2 — Allocate

A spreadsheet grid, because that is what it replaces and the muscle memory is worth keeping. Rows = villages, columns = W/C/I/Cr.

Each cell shows two lines:

```
  +827/h        ← SHIP (large) — the number that becomes cargo
  target 1,014  ← dimmed secondary
```

- **Column header is the live allocation meter**: `Iron — 96% allocated · 4% unassigned → V02`. Green at exactly 100% with a remainder set; amber with slack; red over 100%.
- **`remainder` is a radio in the column header**, so exactly-one is structural.
- **Role dropdown per row**; cells overridden away from the template get a small marker so deviations are visible at a glance.
- `sustain + X%` cells render the derived figure inline: `sustain −5,556/h + 13% → ship 6,278/h`.
- **Foreign targets are a separate section below the grid**, never rows in it — they are not villages and giving them village rows invites treating them as such.

### Stage 3 — Optimize

Two panes.

**Left — merchant budget per village.** A horizontal stacked bar per village: committed / tribute / reserve / free, with the cap as a hard line. Over-cap bars turn red and jump to the top. This is the constraint the user actually reasons about, so it deserves the primary visual.

**Right — routes**, grouped by hub. Each route row expands to show **the cycle sweep that produced it**:

```
V10 → V02   9,323/h   cycle 3h   5 merchants × 3 sets = 15
  1h 18 │ 2h 20 │ ▸3h 15◂ │ 4h 21 │ 6h 18 │ 8h 24 …
```

Explainability is not decoration here. The operator did this by hand and will not trust a solver that shows only its answer; seeing the non-monotonic curve is what makes the choice believable.

**Infeasible villages get their own panel**, each with the escalation ladder's next step spelled out — *"needs ~55 merchants at 105 fields; recommend local consumption"* or *"Trade Office +2 → fits in 17"*.

### Stage 4 — Beat

A **24-hour** timeline per hub (per R5, not 60 minutes), arrivals as markers, the hub's own outbound as a distinct mark, and the reserved NPC slot as a shaded band. Collect-then-ship violations draw as a red overlap. If R6 confirms that phase is fixed at creation, this stage's output column is **"create route at HH:MM"**.

### Stage 5 — Sheet

The diff, not the plan: **Create / Edit / Delete** against the currently configured route set, so the user only touches what changed. Per-row copy buttons, and one "copy all creates" action. Unchanged routes collapse into a single line: `14 routes unchanged`.

## Cross-cutting

- **Every number carries its unit and sign convention.** Rates are `/h`, stocks are absolute, cargo is per-send. Mixing these is how #1 happened.
- **Nothing auto-refreshes.** Requests are the scarce resource; every fetch is a deliberate, priced click.
- **Warnings live where the problem is**, with a severity split between "will break" (red, blocks Sheet) and "worth knowing" (amber).
- **The plan document is local and versioned**, so re-planning after a village count change is a diff rather than a re-entry.

---

# Part IV — Shipped behaviour

Parts I–III are the profile and the review of it. This part records what the
code actually does where it has moved past them, so nobody plans against a
version of the tool that no longer exists. Every claim names the symbol it came
from; where something is unverified it says so.

Where the code and an older section of this document disagree about **what this
account has decided**, `.claude/agents/review-data/01-operator-rulings.md` is
the authority. Two of its entries have already invited wrong "fixes" — the
merchant base (R1) and the map span (§4.12).

A reference of the form **profile §N** below is to the operator's own
requirements profile, which the code's docstrings cite as "profile section N".
Bare `§N` is a section of *this* document, as elsewhere.

One assumption qualifies every rate below: **the code assumes a trade route
whose resources are short ships a PARTIAL load — whatever the origin holds at
that minute — and never tops the missed amount up later.** That is what both
replays do (`shipped = min(batch, available)` in `storage.simulate_day`, and
the same line in `storage.simulate_profile_cycle`), and both now say so at the
point of use rather than presenting it as a property of the simulation.

*This paragraph used to state the opposite — that the code assumes the send is
simply SKIPPED. The code never did; the sentence was the error, and it is
recorded here rather than quietly replaced because it is exactly the kind of
drift the `UNVERIFIED` tag exists to keep visible.*

Whether the game skips the send, ships a partial load, or tops up from the next
cycle is **UNVERIFIED** (§I.5.4 of the mechanics reference), and it is
load-bearing — every hourly figure in a plan depends on which of the three it
is. **Which way it cuts:** under SKIP the destination gets nothing that cycle
and the whole load stays at the origin; under PARTIAL it gets whatever the
origin held. So against a skipping game the tool reports more arriving than
really does, and a receiver it calls fed may not be — the optimistic direction.

**The one test that settles it:** a single deliberately resource-starved route.
Size a row above what its origin produces, leave the origin short, and read the
destination back after one cycle. Nothing else in the plan need change, and no
extra fetch is needed beyond that read-back.

## 4.1 The declared material relay tier

`VillageConfig.relay_for` names the villages one village **forwards materials
on to**. Lumber, clay and iron only. The operator's own route is
**02 → 18/14 → 11/17/19**.

**Crop relays are searched; material relays are declared** — and the asymmetry
is deliberate, not an omission. Crop already relays through a sub-hub wherever
the route search finds it worth doing (`optimizer.MAX_RELAY_HOPS = 1`), so a
second, *declared* mechanism for the same resource would be two answers to one
question. Nothing searches for a material hop: profile §5 does not ask the
planner to find a tier, it says one exists and says where it may be drawn from.
Consequently **which relay serves which downstream is a fixture choice, not a
solver output** — the assignment is the operator's sentence, and its arbitrary
look is not a defect to file.

**What it buys.** 02 holds the reserved wood and may only reach its own
neighbours, so with a `ship_only_to` on 02 the villages beyond it are simply
unreachable and the plan comes back infeasible with a shortfall each. Naming a
neighbour as their relay supplies the two legs the planner cannot invent.

**Sized by construction, never searched** (`optimizer._relay_tier_flows`): the
forward leg to each downstream carries that downstream's own unmet gap, and the
collecting leg into the relay carries the sum of its downstreams' gaps. A relay
keeps nothing it forwards — the function asserts that and raises if a later
change breaks it, because cargo banked at a relay is an allocation nobody gave
it and shows up days later as a full warehouse rather than in the route list.
The collecting leg's merchants are billed to the village that **sends** it, so
they count inside that village's own `max_busy_merchants` — profile §5's "the
relay leg counts inside the 8".

Rules the tier enforces, refused at the schema
(`PlanRequest._relay_tier_is_one_hop_of_non_role_villages`) so **one** rule
covers `/plan`, `/day-check`, `/execute` and `/night-profile`:

| Refused | Why |
|---|---|
| a downstream the snapshot does not contain | a typo, or a chiefed village |
| a village declared as its own relay | there is no leg to build |
| a **role** village as the relay | profile §5.9: capital / troops_off / full_off / def may not relay. `feeder` and a village with **no** role declared may |
| a relay feeding a relay | one hop only — a chain puts one hub's forward leg behind another's, and no daily beat can order both |
| a downstream named twice in one list | a duplicate is one downstream; sized twice it is shipped twice |
| one downstream claimed by two relays | the same over-ship from the other side |
| an empty `relay_for` list | unlike `ship_only_to`, "forwards to nobody" has no reading that differs from leaving the field off |

Each refusal names the villages, and the role refusal names the role — "18 may
not relay" is unanswerable where "18 is your Hammer, so it may not relay" says
what to change. `may_relay` is deliberately **not** consulted here: it is a
permission about the *crop* search, and profile §5.9's rule is about the role.

Two defects in the tier were found in review and fixed:

- **A downstream named twice was shipped twice.** The collecting leg was drawn
  from an inflated total and the forward loop handed the repeated village its
  whole target once per mention. Measured on the relay-tier fixture: 16,744/h
  landed against an 8,372/h target, while the downstream the duplicate
  displaced was reported unreachable — and reported with the *whitelist* as the
  reason, so nothing on the sheet pointed at the duplicate. Guarded in both
  layers now, doing different jobs: the solver treats the downstream set as a
  set and decrements each gap as it forwards, so a duplicate cannot destroy
  resources, and the schema refusal keeps the operator from believing a tier
  they typed twice is a tier twice the size.
- **The tier's source was chosen blind to the merchant cap.** A relay is an
  ordinary receiver and the greedy rule picks its nearest sender with surplus
  left, but the tier's legs are merged in *after* `_improve_flows`, so this is
  the only place a cap can be consulted at all. On distance alone a capped
  village was put over its budget while an affordable source of the same cargo
  stood one field further out — 4 merchants committed against a cap of 2, plan
  infeasible. The sort key is now `(merchants over budget, distance,
  coordinates)`, priced with the plan's own `_route_for_pair` arithmetic.
  Distance still decides between two sources that can both afford the leg, so
  the operator's tier builds the same legs it did. It is a **preference, not a
  guarantee**: the excess is an under-estimate on a multi-resource account
  (pair-merging has not happened yet inside the per-resource loop), so a breach
  that survives it is still reported by `over_budget`, which remains the
  authority.

## 4.2 The relay buffer bound — capacity, not free space

A relay holds cargo it never grew, between collecting and forwarding, and a
store that tops out in between destroys the difference. This is the check that
had material relay deferred once before, and it is not a refinement of the
generic overflow report.

The bound (`storage._pass_through_bound`) is one cycle's **pass-through**:

```
pass_through = rate × max(collect_cycle, forward_cycle)
```

Both tempting readings of it are wrong in one of the two regimes.
`rate × collect_cycle` — "one collecting batch" — is the answer only when the
forward leg is no slower. `rate × forward_cycle` — "what accumulates between two
forward sends" — is the answer only when the collecting leg is no slower. Both
regimes are real. On the operator's own geometry the **forward** leg is the
slower one — 02 → 18 is one field and costs least at 1h, 18 → 11 is seventeen
and costs least at 2h — so two batches land between forward sends and the relay
has to hold both: silent at a capacity of 33,488
(16,744/h × 2h) and reporting at 32,988, where one batch is only 16,744. Move a
relay far from its source and the other regime appears: at collect 1,000/h on a
2h cycle against a 1h forward cycle, silent at a 2,000 warehouse and critical at
1,800, where `rate × forward_cycle` is 500. The rate is read off the **forward**
legs, which is the same figure for a pure relay and excludes the relay's own
retention where they differ.

**It is capacity, not free space.** A 160,000 warehouse already 94% full still
holds a 33,488 pass-through, and the finding stays silent. That is a
**steady-state** claim and the honest reading of it: in steady state the trough
settles at `cap − peak` wherever that is positive, so only a capacity below the
peak sheds something every cycle. A relay that *starts* nearly full sheds once
and then settles — measured, a 160,000 warehouse holding 150,000 sheds 6,744 on
day one and 16,743 at 159,999 — and that one-off is deliberately **not** this
finding. It is bounded by a single pass-through, it is fixed by draining the
village once rather than by changing the tier, and the continuous filling-store
check is what reports it.

Two categories rather than one with two severities, because severity belongs to
the category here (`findings.py`):

| Finding | Severity | Meaning |
|---|---|---|
| `RELAY_BUFFER` | critical | the store sits at its cap all day, or fills before the forward leg's first send — cargo destroyed **at the relay** |
| `RELAY_BUFFER_TIGHT` | warning | the tier *does* forward first, then tops out and sheds what lands afterwards |

Read off `simulate_day`'s replay rather than re-simulated, because a second
simulation with its own assumptions is how `/plan` and `/day-check` came to
answer the same account differently once before. And the tier is blamed **only
for overflow the pass-through can explain**: a relay forwards everything it
collects, so where the replay reports a store that never leaves its cap *and*
the warehouse is big enough for the pass-through, what fills it is the relay's
own retention or production, and `storage_findings` already names that with the
right cause. Before that gate, a relay whose 100,000 warehouse held its 10,000
pass-through ten times over drew a critical blaming the tier for 240,000/day
that was entirely its own target, and prescribed three fixes none of which was
the one.

Neither finding carries a `loss_per_day`: `storage_findings` has already billed
the same event to the account and `Diagnostics.total_loss_per_day` is a plain
sum, so carrying it twice put 113,856/day of lumber into one account's total
twice. The figure is in every message instead.

## 4.3 Roles

`VillageConfig.role` says **what a village is FOR**, which nothing in the game
states. `Role` is a `StrEnum` in `services/distribution/roles.py` with five
members: `capital`, `troops_off`, `full_off`, `def`, `feeder` — `feeder` being
what the profile calls "all other villages". `None` means nothing declared, and
plans exactly as before.

Roles are backend state rather than a label the page paints, because two
decisions are made *from* them in `roles.py` and neither is expressible any
other way (short of the per-village `may_relay` override below):

- **Who may relay.** `default_may_relay(role)` returns true for `feeder`
  **only**, which means the capital may not relay either. Profile §5 makes 02
  the hub every feeder ships to *and* draws the onward relays from 02's own
  neighbour set, and those two only fit together if the capital hands off. Read
  the other way the capital would win every relay search it entered, being the
  most central village on the account by construction, and the tier profile §5
  asks for would never be built.
- **How loud a designed crop deficit is.** A role whose template sets
  `crop_negative_by_design` has its granary countdown reported as a note rather
  than a critical — a downgrade and never a suppression, because the hours of
  cover are the one figure worth acting on either way.

The crop-relay permission (`optimizer._may_relay_through`) resolves in this
order: an **unreadable** crop rate is refused before anything else is consulted
(a declaration says what a village is *for*, not what its granary is doing, and
an unparsed rate is never given the optimistic reading); then
`VillageConfig.may_relay`, merged per village over the role template's — so it
can arrive with no role at all; then `default_may_relay(role)`; and only where
nothing has been declared does the old crop-sign inference apply. That inference
is right on an undescribed account and wrong the moment something is declared —
01 reads −5,880/h **by design**, so its sign says nothing about whether crop may
pass through it. Getting the first two the other way round let a declared role
bypass the unreadable-rate refusal entirely.

A role named on a village with no template in `PlanRequest.roles` is **refused
rather than ignored**: ignoring it plans four defensive villages as keeping
their own production, which is a tenth of what they need, and calls it feasible.

`PlanResponse.role_deviations` reports every cell where a village was given a
target its role's template did not, with both figures. Overriding a template is
legitimate — one of four defensive villages always has a wall going up — but
overriding it *invisibly* is not. The server resolves it rather than the page,
because two implementations of one merge rule drift and a grid that marked the
wrong cell would be worse than one that marked none.

## 4.4 `ship_only_to` — a per-village destination whitelist

If set, the village may send to those **own** villages only, for **every**
resource, crop included — so a village left off the list is not fed from here
even when its granary is emptying. Exempting crop was considered and rejected:
the operator's own spec whitelists the army village, which says they expect the
list to bind crop, and an exemption would silently overrule a declared
restriction with nothing anywhere to explain the extra route. A starved receiver
surfaces as a shortfall naming the whitelist as the cause instead.

Mechanically it is the same denylist seen from the other side: for every own
destination the list does not name, the whitelisted village is added to
`excluded_origins_by_destination` (§4.9), so the optimizer sees one structure
whichever side the operator wrote it from. A village naming itself is harmless
and ignored. An id the snapshot does not contain is a 422, not a silent drop.
An **empty** list is a real answer — "ships to nobody".

Foreign targets are governed separately by their own `exclude_origins`: a
whitelist cannot stop a tribute, and the plan raises `WHITELIST_VS_TRIBUTE` when
one is supplied from a restricted village, so the exemption is visible on the
plan that used it rather than only in a tooltip.

## 4.5 `stock_floor_fraction` — a buffer level the NPC allowance draws against

The fraction of its warehouse capacity a village keeps stocked by NPC trading
(0…0.95). **Materials only** — a granary is not NPC-fed — and `0.0 ≡ None` at
every layer: a zero fraction declares nothing.

It is a **level**, and only a level. What a floored village may ship beyond its
production is not derived from the floor at all; it is the NPC allowance of
§4.14 — a rate built from what the village retains of the resources it is *not*
drawing on — consumed as a cap against unmet demand, from a reservoir that is
finite in both replays and debited 1:1. The floor is the buffer that reservoir
refills toward, and `NPC_CAPACITY_SHORT` is what fires when the allowance
cannot cover the gap.

*This section used to describe a different model,* and its worked example is
worth keeping as the thing not to rebuild: `stock_floor_fraction × capacity /
window_hours` turned the level into a rate, so 30% of a 1,200,000 warehouse read
as 22,500/h over a 16-hour day and 45,000/h over an 8-hour night — a **shorter
window raised the claim**, the supply was compulsory (an addend every non-KEEP
mode shipped away), and the replay's reservoir was infinite. Three independent
reviewers converged on those three defects and the model was replaced, not
patched. The field's own Pydantic description records the same history.

## 4.6 `consumption_per_hour` — materials only, and crop is refused by design

What a village **spends** per hour: the building queue and the troop upkeep,
entered as flat constants and kept current by hand. Three figures, all
different:

| Figure | Meaning |
|---|---|
| **target** | the rate that must be *here* — own production plus whatever is shipped in |
| **cargo** (`ship_per_hour`) | target − available: the gap a route carries. Consumption never changes it |
| **consumption** | what leaves again |

so the store nets `net_per_hour = target − consumption`. Zero is level. Enter
only the target and the plan assumes the village stockpiles every unit, which is
how an army village told to hold 14,751 lumber/h came to be reported as losing
354,024/day at a warehouse cap it never reaches.

**Crop is refused, and this is a ruling rather than an oversight.**
`crop_per_hour` in the snapshot is **already net of troop upkeep** — it is
derived from the village's own crop balance, not from the gross statistics
column — so a declared crop spend subtracts the same troops a second time. On
the account that prompted the rule it deleted a real 204,456/day overflow at
village 01. Materials are the opposite case: the statistics page reports them
**gross**, so a village burning lumber still reads positive and nothing in the
game states the spend.

To say what a village should *keep* of its crop, set its crop **allocation**
target: an absolute target is retention above break-even, so 0 holds a
crop-negative village level and a positive figure lets it accumulate.

The refusal lives on the Pydantic field (and on the role template's `consumption`,
which is a second door) rather than in a handler, so **one** rule covers all four
planning paths. A check further in would have to be repeated in each, which is
exactly how `/night-profile` came to ignore the per-village field altogether. It
is not clamped and not trimmed: a crop figure in a profile means the operator
believes it is being applied.

## 4.7 `max_busy_merchants` — a per-village ceiling

The most merchants a village may have underway or returning at any instant
(profile §5: "maximum 8 busy at 02", with the relay leg counting inside the 8).
Measured in the unit the plan already commits merchants in — §8.1's
`merchants_per_send × sets_in_flight` — so it needs no arithmetic of its own.

```
merchant_budget = min(spare_merchants, max_busy_merchants)
spare_merchants = merchants_total − merchant_reserve
```

**A cap, not a reserve**, and the distinction is the point.
`merchant_reserve` holds N merchants back at *every* village, so reaching 8 busy
at one village that way costs every other village the same 12 — and off a fleet
of 19 the two are not even the same number: 19 − 12 = 7 where the cap says 8.
The fleet still applies underneath, so a cap at or above the fleet changes
nothing and is not a promise of merchants the village does not have.

A cap **above** `merchants_total` is refused rather than clamped, cross-checked
against the snapshot in a model validator so one rule covers all four planning
paths. Clamped, "02 may run 30 busy" is accepted and planned as 18, and the
operator's file and the plan then describe different accounts with nothing
saying which is being obeyed. The refusal names the village, because the figure
is one cell in a 26-row table. Two deliberate exemptions: an empty snapshot
(there is nothing to check against), and a village whose merchant count read as
0, which is what `/snapshot` writes when it could not **read** one — unknown is
not zero, and reading it as a fleet refused every cap on that village from all
four endpoints over a plan that runs identically without it.

`merchant_headroom` applies to the cap as to any merchant budget, so a cap of 8
is a soft target of 7 and a cap set to exactly what the plan wants comes back
feasible *and* reported as crowded. Feasibility is decided against the cap
itself, which is why the over-budget advice names the figure that clears the cap
rather than one that also clears the headroom.

**A cap of 0 does not withdraw the village from the plan.** This budget is soft,
as every merchant budget here is: its routes are still built and costed, and
every one of them becomes a budget breach — which marks the village
`over_budget`, refuses the sheet and blocks `/execute`. Use `ship_only_to` to
stop a village shipping.

## 4.8 `village_nets` in the plan response

Per village, per resource: `own_per_hour`, `npc_allowance_per_hour` (the
ceiling the floor could fund), `npc_draw_per_hour` (what the plan actually
spent against it — the two are different quantities and the UI must never show
the first as the second), `target_per_hour`, `ship_per_hour`,
`consumption_per_hour` and the resulting `net_per_hour`, all read straight off
`VillageAllocation`. `supplement_per_hour` was retired with the model in §4.5.

It exists because the net had no reader at all and the allocation grid
recomputed `target − consumption` in JavaScript instead. Two implementations of
one formula drift, and these two already could: the planner drops a declared
spend whose rate it cannot read, while the page still holds the figure the
operator typed — so the page would show a net the plan never used. A UI that
reads the net from here cannot disagree with the plan it is displaying.

**Own villages only.** A foreign tribute is a sink with no store, so it has no
net to report and appears in `shortfalls` instead.

## 4.9 The origin exclusion now binds the whole search *(fixed defect)*

`excluded_origins_by_destination` — the map both `exclude_origins` on a foreign
target and `ship_only_to` on a village resolve into — was handed to the greedy
seed (`_flows_for_resource`) and **nowhere else**. Everything the improvement
search did afterwards was free to name a forbidden sender: a 2×2 swap lands a
flow on `(o1, d2)` and a crop relay invents `(hub, destination)`, and neither
pair was ever vetted against the ban.

Found on the live account before its first run: village 02 was excluded from the
foreign tribute, the seed duly avoided it, and the night plan emitted a direct
02 → tribute route carrying 10,266 crop/h on 9 merchants anyway. Relay breached
it more quietly still — the banned village became the forwarding hub, so the
final leg into the tribute was the exact pair forbidden. **The exclusion was
worse than useless:** it pushed the seed into a poorer basin (six origins rather
than four) and was then violated regardless. And a route into a foreign village
is an **unrecoverable write** — there is no taking the crop back.

The map is now threaded into `_improve_flows`, which vets every candidate
through one predicate (`_may_send`) before costing it: a 2×2 swap is rejected
unless both new pairs are permitted, and a relay unless both of its legs are.
The declared material tier honours the same map when it picks a collecting
source (§4.1). The pre-existing tests could not have caught either path — their
fixture has one destination, and a 2×2 swap needs two.

## 4.10 `ABSOLUTE` is a retention target, not a consumption figure

```
ship_per_hour = target_per_hour − own_per_hour − npc_draw_per_hour
```

The draw is subtracted, never added: a village drawing on its floor ships *less*
from production, not more (§4.14). There is no `available_per_hour`. The village
ends up **retaining** the target; the figure already
includes its own production, so a target is not a delivery quota.

The operator's own words on village 01: *"It means that it gets absolute of
8,519, if loses 5,880 meaning 5,880 + 8,519 = ~14k crop land."* With 01 at
−5,880/h net, ≈14,399/h lands: the drain is covered **and** 8,519/h is retained.

**The consequence the operator accepted:** 01's granary genuinely gains 8,519/h,
so its 204,456/day overflow finding is **real** and must never be silenced. A
negative absolute target is refused rather than planned.

## 4.11 The netting invariant is per resource

A village may legitimately **receive one resource and send another**. Village 02
does exactly that — 13 inbound rows and one outbound. Any statement of the form
"this village both sends and receives, which the waterfall rule forbids" must
name the **resource**, or it is wrong.

The invariant `allocation` actually establishes is per resource: a village nets
to a single figure for each resource, so it cannot be both sender and receiver
of *the same* resource, which is what makes §8.2's two-way-pair rule and the
material no-waterfall rule hold **by construction** rather than by vigilance.
Both are asserted as invariants in the tests rather than defended with runtime
checks, because the property comes from the data model. The declared relay tier
is the one exception, and it is the amended rule in full: *no material village
both sends and receives except one the operator declared a relay, and no relay
feeds a relay* — enforced at the schema, where the operator can be told which
village and which role made their declaration impossible (§4.1).

The limiter on 02 is its **remainder** role, not a no-waterfall rule.

## 4.12 Map span is 401, and the requirements spec's 801 is the error

Europe 2 runs −200…+200, so `span = 401` (`MapGeometry.span`,
`html_parser.DEFAULT_MAP_SPAN`). The requirements spec describes the world as
−400…+400, span 801. **The spec is wrong**, and the proof is recorded here so
nobody "fixes" the code to it. A captured create request targeting (23|88)
resolved to `mapId 45136`, and with

```
mapId = (S − y) × span + (x + S) + 1,   S = (span − 1) / 2

span 401, S = 200:  (200 − 88) × 401 + (23 + 200) + 1 = 45,136   ← matches the capture
span 801, S = 400:  (400 − 88) × 801 + (23 + 400) + 1 = 250,336  ← does not
```

The marketplace page then listed exactly that destination for the route, so the
capture is corroborated end to end.

**Why a wrong span is loud and total rather than a rounding error.** It breaks
`map_id_to_coords`, which is how a route read off the marketplace page is
matched back to a plan — that page names a destination by village id and map id
and carries **no coordinates at all**. So reconciliation breaks, and revert
depends on reconciliation. It also skews every distance `MapGeometry` computes,
which is why the request validator refuses an **even** span outright: a world is
centred on 0|0, so its width is always odd, and an even span shifts every tile
index by half a field silently.

---

## 4.13 The night rules (profile §6)

Four rules, all of which used to be prose.

**Everything home before the switch.** `schedule.night_overrun_minutes(scheduled,
window, night_end)` prices a route's last *in-window* dispatch plus its full
round trip against the night's close, in minutes; zero or below is a clear road.
`Category.NIGHT_OVERRUN` (CRITICAL) names the route, the clock it last leaves
at, the round trip and the overrun. `build_beat` reshapes first — its placement
score gained a `home` term ranked below "send at all" and above the reserved
window — and only reports when no phase closes the night. The one fix it will
not apply is dropping a firing: the cargo was sized for the firings the plan
counted, so trimming under-delivers in silence.

**The fill pair is 0.25 and 0.60.** `night_profile.DEFAULT_BASELINE_FILL` and
`DEFAULT_TARGET_FILL`, from 0.30 / 0.80. One constant does both jobs on the
target side, deliberately: the room between baseline and target bounds what the
night may ship *into* a store, and the same figure is the floor every role
village must have reached by 07:00. "Never overflow during the night, never
arrive empty at morning" is one statement seen from either side, and two
constants could disagree. The ceiling is
`(target − baseline) × capacity / window_hours`.

Who holds a floor: `roles.keeps_a_morning_floor(role)` → DEF, TROOPS_OFF,
FULL_OFF. Deliberately narrower than `default_may_relay`, which counts the
capital in — relay is about a job a leg in transit disturbs, the floor is about
waking able to build.

**The 2-hour latency target does not bind at night.** `craft_plan` passes
`latency_target = None` for a declared night, and `None` is exactly "no target":
it skips both the `Category.LATENCY` findings and
`_spend_idle_merchants_on_latency`, the pass that buys speed by spending
merchants. That pass is the one a night which must end empty cannot afford.

**`overnight` is declared, not derived.**
`night_profile.is_night_window(window, *, overnight=None)` — a window that wraps
past midnight is still the fallback reading, but the declaration wins, and a
`None` window (round the clock) is never the night whatever is declared. Fields:
`PlanRequest.overnight`, `DaySegmentInput.overnight`, `PlannerConfig.overnight`,
all `bool | None`. Declaring it with no hours is a 422.

*What to look for:* deriving it from the wrap alone was wrong in **both**
directions, and both shapes are legal input. The half of a split night *after*
midnight — `(0, 420)` — wraps in neither direction, so §6 applied to neither
half; and because the detected half was the pre-midnight `(1380, 0)`, the 60%
floor was measured **at midnight**. Conversely a near-24h day profile
`(420, 419)` wraps, which silently suspended the latency target all day.

**A split night is read from both ends.** `_one_night_run(night_segments)`
requires the declared halves to chain end-to-start: the opening is the half
whose start is no other half's end (exactly one, or it gives up), and the
closing is reached by chaining forward. Picking the closing by "the half no
other starts at" looks order-free and is not — a gap between halves, or a second
window declared overnight, makes every candidate qualify as both ends and then
list position decides. When the halves do not form one run, `/day-check` says so
and skips the two state rules rather than answering against an arbitrary piece.

The 25% baseline is measured at the opening half; the 60% floor is measured
against the profile that **starts at the closing half's end** — usually the
morning profile — not against the closing night half itself.

*Direction of danger: quiet.* The derivation is what the operator writes into
the active profile, while `NIGHT_OVERRUN` is raised by the planner on routes
this profile has already called shippable.

## 4.14 NPC balancing (profile §7)

`services/distribution/npc.py`. Two mechanisms kept apart, because conflating
them is what produced the first build's defects.

```
allowance_per_hour(v) = Σ_{r ∉ drawn(v)} max(0, target₁_per_hour(v, r))
ship = target − own − draw          # the draw is a CAP, never an addend
```

**The allowance is a rate built from rates** — what the village retains of the
resources it is *not* drawing on — so neither window length nor warehouse
capacity appears in it. **The draw is consumed only against unmet demand**, so a
floor on a quiet village costs nothing. **The reservoir is finite in both
replays**, refilled at the allowance, zero while unattended, booked as an inflow
so `net_gain_per_day` stays true, and **the feedstock store is debited 1:1** —
that debit is what makes the 700,000-crop trigger at the capital honest.

`stock_floor_fraction` keeps its name and wire contract and means the **buffer
level** only (`fraction × warehouse_capacity`, materials, warehouse). `0.0 ≡
None` at every layer. A granary is not NPC-fed.

Three of the first build's mistakes are pinned so they cannot return: a **level
modelled as a rate** (`capacity × fraction / window_hours`, so a shorter window
*raised* the claim), supply that was **compulsory** (an addend every non-KEEP
mode shipped away), and an **infinite reservoir** in the replay.

**Triggers.** `NPC_CROP_BANKED` (note) at `CROP_FEEDSTOCK_TRIGGER = 700_000`,
strict `>`. `NPC_WOOD_LOW` (warning) reads the village's **own declared floor**
rather than an invented threshold — profile §5's "warehouse assumed ≥25% stocked
on wood" *is* that floor — so a village with no floor gets no reading at all.
Both are reporting triggers: the planner does not press the NPC button.

**`npc_attended` is required, never guessed.** Per segment on `/day-check` and a
whole-day `/execute`; required on `/plan`, `/execute` and `/night-profile`
whenever a floor meets a `dispatch_window`. A round-the-clock set (no window) is
**not** exempt and defaults to **unattended** — it has all 24 hours, including
the eight nobody is at the Marketplace, and Travian offers nothing to confine a
repeat interval to part of the day. It is not a 422 there only because a setup
*document* is validated through the same model and carries no window.

*Direction of danger: quiet and over-committing.* Nothing downstream catches an
optimistic reading — `simulate_day` tops the store up at every departure minute
including 03:00, and `NPC_CAPACITY_SHORT` is measured against that same cap. So
the direction of the default *is* the guard. `NPC_CAPACITY_SHORT` is CRITICAL,
weighed, and blocks `/execute`.

## 4.15 Crop-profile drift (profile §9)

`roles.crop_drift_findings`, threshold `CROP_DRIFT_THRESHOLD = 0.20`, quoted
from §9 at the constant. Compares the snapshot's `crop_per_hour` against
`RoleTemplate.assumed_crop_per_hour` — the operator's own reading of what a
village of that role nets. May be **negative** (01 reads −5,880/h); `0.0` is the
real claim "breaks even"; absent means no assumption and the village is not
checked.

Deviation is `|actual − assumed| / |assumed|`, and the two tempting alternatives
are refuted in the docstring. Comparing *magnitudes* scores "assumed −5,880,
actual +5,880" as 0% drift, when that is the single most important change the
figure can undergo. A *signed ratio* inverts against a negative denominator, so
a village that got worse reads as improving. The predicate is written as
`abs(gap) <= threshold * abs(claim)`, so an assumed 0/h needs no special case
and is never divided by; the wording changes instead.

A WARNING, never a blocker: these are hand-kept constants and drift is expected.
`crop_negative_by_design` deliberately does **not** silence it — a profile
behind a designed deficit is the likeliest figure on the account to have moved.

## 4.16 Confirm, then export (profile §10)

§10 asks for "readable plan first → operator confirms → then generate YAML".
Nothing on this server holds a computed plan — `/plan` is pure and stateless,
which is what makes tuning a target free — so there is no plan to fetch by id,
and trusting a plan posted back is what `/execute` already refuses.

So `/plan` returns `plan_digest`: sha256 over the whole response with that field
excluded, canonicalised as `json.dumps(sort_keys=True, separators=(",", ":"))`.
`POST /plan/yaml` takes the `PlanRequest` plus a required
`expected_plan_digest`, re-plans, re-digests, and **409s naming both digests**
unless they agree. The document therefore either *is* the plan that was read or
does not exist. A malformed digest is a 422 rather than a 409, so a mistyped
token does not send the operator re-reading a plan that never moved.

The document is deterministic — no timestamp, hostname or run id anywhere — and
the file is named for the plan (`distribution-plan-<digest[:12]>.yaml`), not the
moment, so two downloads of one plan are one file. Twelve sections, `inputs`
among them, so a file is self-describing a month later and re-plans to the same
digest.

## 4.17 The setup document, stored server-side

`GET`/`PUT`/`DELETE /api/distribution/setup?account_key=…`. One saved setup per
user per account — a composite primary key on `planner_setups`, not a preset
library. Stored and returned **verbatim**: `buildSetup` omits every field it has
no answer for, and a store that re-serialised a validated model would write
`may_relay: null` and `trade_office_level: 0` onto every row, turning "nothing
declared" into a declaration.

**A document the planner would refuse is refused on `PUT`**, by building a
`PlanRequest` out of it and letting its own validators speak — not by restating
the rules. The half that is about the *account* rather than the document ("is
this village real", "does it field that many merchants") has no answer without a
snapshot and is deliberately left to the next `/plan`.

Why it exists: `localStorage` is scoped to an **origin**, so the same app on
`:80`, `:8001`, the LAN address and over Tailscale kept four independent copies.

**Format versions.** v7 carries per-profile `npc_attended`, v8 per-profile
`overnight`, v9 the account-wide `reserved_window`. Each earned a version rather
than riding along as an unknown key — which all three mechanically could, since
the body is stored verbatim and `SetupDocument` ignores extras — because the
harmful path is identical: a build that cannot read one drops it silently, the
operator saves from that build, and the answer is gone from the shared copy.
Older documents load with the field absent; newer ones are **refused, never
upgraded**, and the version is stored as given.

**Every boolean answer is typed at the door.** `npc_attended` and `overnight`
are declared `dict[str, StrictBool]` on `SetupDocument`, and `may_relay` is
`StrictBool | None` on both doors that carry it — the village row
(`VillageConfig`) and the role template (`RoleTemplate`). `StrictBool` rather
than `bool` because pydantic's lax bool accepts the **string** `"yes"`, and
`"no"`, `"on"`, `"off"`, `"1"` and `"0"` besides: the body is stored verbatim
and `parseSetup` **throws** on a non-boolean in all three places, so a coerced
`"yes"` saved with a 200 and the page then refused, permanently, to load the
document it had just written. All three are now a **422**.

`crop_negative_by_design` and `route_eligible` stay lax on purpose:
`parseSetup` coerces both with `Boolean(...)` rather than refusing them, so the
document still loads. (Not a perfect agreement — pydantic reads `"off"` as
false where JS `Boolean` reads it as true — but that is a value divergence, not
a document that cannot be opened.) The numeric fields are safe the same way:
`Number(...)` on each.

## 4.18 The reserved marketplace window

`reserved_window`, minutes past midnight on the wire and `HH:MM` in the
document, threaded to `build_beat`. It is a **soft scheduling preference ranked
third** — behind "send at all" and "be home by the switch" — not a refusal: each
route's dispatch minute is chosen so its arrivals miss the window wherever any
phase can manage it, and `Category.RESERVED_WINDOW` (WARNING) names the route
when geometry forces one in. Wrapping past midnight is supported; a zero-width
window is legal and simply omitted from the request.

It exists because the operator's NPC burst is a manual action at a Marketplace,
and merchants landing during it compete for the same attention.

## 4.19 One latency target per profile *(fixed defect)*

`/execute` used to override `max_latency_hours` per segment with the window's
own length while `/day-check` kept the standing top-level value. A 07:00–23:00
day was therefore planned against **16h** by the endpoint that writes and **2h**
by the endpoint the operator reviews with — and the comment above the override
claimed the opposite.

The consequence is the §I.6.2 burst: the operator reads a clean sheet built from
short cycles and small batches, `/execute` recomputes on the cheapest longer
cycles, and bigger batches land in the same stores. What gets written is a route
set whose burst behaviour nothing simulated. A 60-minute profile inverts it —
1.0h in `/execute` against 2.0h in `/day-check`, so `/execute` could buy
merchants and report `over_budget` on a plan `/day-check` passed.

Now derived once inside `_plan_account` as
`min(standing target, window length)`: a window may **tighten** the standing
target, never loosen it. Taking a 16h window *as* the target disables the
objective outright, since no route can miss 16h; ignoring the window leaves a
60-minute profile aiming at a lag it has no hours to absorb. `None` still means
"no target", so a declared night is unaffected.

## 4.20 Writes and reads that were not what they claimed *(fixed defects)*

**An unreadable bulk toggle was reported as total success.**
`_rejected_routes` returned "nothing rejected" for any body it could not parse,
and the caller reads that as "all N went through". A soft-block page, a gpack
revision or an HTML error body during a **revert** therefore reported
`disabled: 24 route(s)` while twenty-four rows kept shipping — a revert complete
on paper with the account still draining a sender. `ToggleResponseUnreadable`
now draws the same distinction the read side already drew with
`MarketplaceUnreadable`: "I could not check" and "the game refused nothing" are
different answers. `routes: []` is still a clean success. The disable and enable
messages differ because the consequences do — an unreadable disable sends the
operator to look, an unreadable enable says a later run can repair it.

*Worth knowing before the first live run:* `docs/15-gold-club-features.md`
records that only the **disable** direction of the bulk toggle was ever
observed, and that the `routes[].error` shape was read off the game's own
JavaScript bundle rather than an observed response. So the exception's trigger
rests on a shape that has not been seen in a real reply.

**Failed writes and reads did not bill the shared request ceiling.** Billing ran
only on the success path, so a `NetworkError` — and a Gold Club refusal, which
returns "skipped" *after* the request went out — cost the account a real request
and a real throttler gap and were counted as nothing. The ceiling is shared with
the farm-list and oasis loops, so under-reporting here licensed those to
overspend. Billing now sits in a `finally` inside each request's own `try`, so
"exactly once" holds by construction rather than by inspection.

**The 429 backoff was switched off along with request pacing.** The pacing
switch is the operator's and its default is safe, but the same flag guarded
every penalty, so with pacing off a 429 produced no backoff at all. A server
saying "slow down" is not a stealth preference. The penalty is now served
whichever way the flag is set; the inter-request gap and burst rules stay off,
which is the deliberate half.

## Immediate next actions

| # | Action | Cost | Unblocks | Status |
|---|---|---|---|---|
| 1 | Read merchant capacity at a *second* TO level — a **TO 0** village for preference, because its capacity *is* the base (`merchants.py` records 03 and 26 as TO 0) | 0 requests | pins base and slope together; today only the base is read | open — base 2,500 (2026-09-02), slope unmeasured, 7,920 @ TO 13 superseded |
| 2 | Capture Resources + Capacity + one *filling* village `dorf1` together | 0 requests | R2 filling branch | open |
| 3 | One-off `dorf2` sweep for TO + Marketplace | N requests | §1 OWNED state, and refreshes the per-village TO levels R1 found drifted | open |
| 4 | Create one trade route and observe its phase and any per-village cap | in-game | R6, open question #1 | open |
