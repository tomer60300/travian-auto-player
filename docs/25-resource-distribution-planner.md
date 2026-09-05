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

*Not to be confused with the night's shed bound.* This one asks whether a
relay's **store** can hold what passes through it. `night_profile.shed_limit`
(§4.13) asks whether a village's **merchants** can carry what its retention
promises to give away, and is a partitioned-fleet bound over an
entirely different set of inputs. Neither is a refinement of the other.

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

Four rules, all of which used to be prose — and, under them, what the
derivation is allowed to promise.

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

**What a village may shed is a partitioned-fleet bound.**
`night_profile.shed_limit` measures the legs `_legs` builds and hands them to
`partitioned_fleet_limit`, which is the whole model — **one** model, where there
used to be two that had to be intersected.

A merchant serves **one** destination for the night. It cannot spend 0.6 of
itself on the near consumer and 0.4 on the far ally, because `merchants.py`
charges every route its own whole merchants (`ceil(batch / capacity) ×
sets_in_flight`, summed per destination) and the planner will not build anything
else. So the bound is the largest send whose per-destination merchant demand
fits the fleet:

```
mᵢ = ceil(S × wᵢ × window_hours / (capacity × tripsᵢ))
shippable  ⇔  Σ mᵢ ≤ fleet
```

`merchants_needed` is that predicate and `partitioned_fleet_limit` is the
largest `S` it accepts, solved **exactly**: every destination taking cargo needs
at least one merchant, so a fleet smaller than the destination count sheds
nothing at all; otherwise start at one merchant each and give each spare
merchant to whichever destination is currently holding the send down. Writing
`uᵢ = capacity × tripsᵢ / (wᵢ × window_hours)` for what one merchant dedicated
to `i` sustains, the send an allocation supports is `minᵢ mᵢ uᵢ`, and each `mᵢ`
was last raised while `i` was binding — so `(mᵢ − 1) uᵢ` was the send at that
moment and the send never falls. Anything above the final `min` therefore needs
at least `Σ mᵢ + 1` merchants. The answer is re-checked against
`merchants_needed` and stepped down while it fails, because the two reach the
same quantity by different arithmetic and a `ceil` can land one ULP high;
under-estimating is the safe direction.

This replaced `min(conserved, integral)` — merchant-hours conservation at the
demand-weighted mean hop, intersected with each destination's own whole-trip
bound. **The two no longer disagree, because there are no longer two.** Neither
factor saw the partition, so both passed sends no allocation can make:
conservation lets merchant-time split fractionally between a near and a far
destination, and `integral` asks each destination *alone* whether the whole
fleet could serve it. One merchant carrying 2,500 in an 8h night, a destination
an hour out claiming 3 and one three hours out claiming 1 — shares 0.75/0.25,
mean hop 1.5h, two turnarounds — conserved 625/h, 5,000 over the night.
Delivering that is 3,750 to the near one (two trips, 4h) and 1,250 to the far
one (one trip, 6h): **ten merchant-hours out of eight**. Under the partition the
two destinations need a merchant each and the answer is 0. Measured elsewhere in
the same direction: a hub with 8 merchants, a consumer 1 field away needing
100/h and one 40 fields away needing 40,000/h, shipped 2,500/h under the old
bound and 2,192.98/h under this one — the near consumer takes a whole merchant
for its 0.25% of the send, so seven and not eight serve the far one.

The demand weighting itself is unchanged and still the reason the bound is not
the *nearest* hop, which is the optimistic end of the range and so barely ever
bound: the same hub was credited 48 turnarounds against its neighbour and booked
53,000/h as shippable, 24× what actually reaches the destination needing it. Nor
the worst-case hop, which fails the other way: one unreachable destination then
zeroes the limit for every reachable one, so an ally 60 fields off — a 10h round
trip in an 8h night — left the hub unable to ship to the consumer 2 fields away.
With a single destination the whole fleet lands on one leg and the answer is the
single-destination formula, so nothing with one destination moved.

`_trips` never rounds up, and there is no `max(1, …)` anywhere in the bound: a
village whose round trip does not fit the window sheds **nothing**, because
crediting it one trip promises cargo still in the air at 07:00, which §6
forbids outright. The fleet is `merchants_total − merchant_reserve`, tightened
by `max_busy_merchants` where the operator declared one — the same rule
`VillageState.merchant_budget` applies on the plan side.

**Where the cargo goes is a different set per resource**, which is why
`shed_limit` cannot be asked without one. `_destinations` answers it. For crop
the set is the crop-negative villages in `consumer_ids` plus each tribute's own
tile; the hub is not in it, and on this account the hub is a crop *sender*
rather than a sink. For lumber, clay and iron it is `_material_receivers`: the
hub **and** every receiver — a village the day plan wants to hold more than it
makes, or one whose declared spend exceeds its production. Pricing every
material sender's hop to the hub alone bound it by a village its cargo never
visits: a supplier 199 fields from the hub and **one** field from the receiver
it feeds was told it could ship nothing over a ten-minute haul. A zero claim is
kept rather than dropped — `_legs` gives it no share, so it takes no cargo and
`shed_limit` charges it no merchant, but a hub that needs nothing must still be
the destination a forced sender is measured against. A village is never its own
destination: the tile is unique in Travian, so a zero hop is the village itself
and is dropped rather than allowed to read as a free delivery. A destination
**no** round trip reaches is dropped rather than allowed to zero the bound, and
`_anyone_reaches` keeps its claim out of the pooled demand so it lands in
`unmet` instead of reading as covered by a sender that cannot get there.

The receiver branch of the material pass is **floored by what the receiver can
ship**, exactly as `capped()` floors a forced sender:
`take = max(min(wanted, room), round(own − shed_limit(…)))`. Before that floor
the branch consulted no distance at all, and a village whose store left room
for 44/h was booked to ship 19,956/h over 199 fields, where no round trip fits
the night.

**The crop draw is ordered by where the crop actually goes.** The order key is
`_mean_hop(v, CROP)` — the demand-weighted mean over the same destination set
`shed_limit` bounds the village by, though no longer the same arithmetic since
the bound became a merchant partition — not the distance to the hub and not the
distance to the tribute, because crop reaches neither. A supplier 2 fields from the hub and 18 from the hammer (a 3h
round trip, two turnarounds) was drawn ahead of one 19 fields from the hub and
**one** from the hammer (ten minutes, forty-eight turnarounds), and the plan
then built the long route at six merchants where the short one costs three —
with the early firing still in the air at 09:00, which is a `NIGHT_OVERRUN`
besides. Coverage does not move either way: `give = min(own, demand,
shed_limit)` and `shed_limit` reads nothing the loop mutates, so greedy fill
yields `min(demand, Σ caps)` under every permutation. Only the merchant bill
does. The **material** draw deliberately keeps its hub ordering: there the hub
genuinely is the destination the draw ships to.

**One `TributeTarget` per foreign obligation.** A place and a rate kept
together, one built per `route_eligible` `ForeignTarget` on the
`/night-profile` path. They used to be a single `tribute_per_hour` beside a
single `tribute_at`, which cannot describe two allies: N obligations were
summed into one rate and pinned to the *first* one's coordinates, so a 500/h
ally two fields out beside a 20,000/h artifact sixty fields out became 20,500/h
priced at the two-field hop — about forty-eight turnarounds credited to a leg
that is a 10h round trip in an 8h night — and reordering the request body gave
the opposite answer.

The night path applies the **safety margin** the day path already applied:
`per_hour = crop_per_hour × (1 + safety_margin_pct / 100)`. The night freed the
bare promise while the day booked the promise plus the margin, so the remainder
village drained further than the profile predicted — or the plan read
`OVER_ALLOCATED`.

**A tribute on one of your own tiles is an input error.** `PlanRequest` refuses
it and names the village it collides with: a Travian tile holds one village, so
a foreign obligation sitting on one of the operator's own is a typo. It used to
surface as unmet crop with nothing connecting it to the coordinates that caused
it, because `_destinations` drops the zero hop as "the village itself". The
same validator bounds `ForeignTarget.x`/`y` against the request's own
`map_span`: a typed 450 on a 401-wide map is not a far target, it is a place
that is not there, and the geometry used to **fold** it — `span − raw` goes
negative and `hypot` takes the absolute value, so (450|0) read as 49 fields
from the centre, a five-minute haul. Each offending target is named, because
"x out of range" over a table of allies is one cell nobody can find.

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

**One reserve, apportioned across the materials it funds.** A village has a
single conversion budget, so `npc.draw_allowance` splits it over lumber, clay
and iron **proportional to need** — `_need(retention)`, the amount a village
must ship beyond its production — which is the only split no resource ordering
biases. Every floored village appears under every material *even at zero*,
because the allocation layer needs the **declaration** and not just the number:
that is what tells a village that relied on the exchange and came up short from
one that never asked. Crop is never in the map — a granary is not NPC-fed.

**The feedstock floor is kept inside the conversion, not outside it.**
`storage._npc_top_up` bounds each departure's funding three ways, and each is
load-bearing: by what is asked for, by the budget accrued so far (so the
reservoir can run out), and by what the feedstock stores actually **hold**, so
nothing is converted out of crop that has already been shipped away. A material
source keeps `reserve.floor_level`; crop keeps nothing, because a granary has
no floor. The debit is proportional to `NpcReserve.share_of` — the store that
funded most of the allowance is debited most — then spilled onto whatever else
has room where one store cannot cover its share, because the operator converts
from what is in the village and not from a ratio.

**The budget is capped at one day's allowance.** `storage._accrue` adds
`allowance_per_hour × hours` and takes `min(allowance_per_day, …)`. Without
the cap a budget accumulating over the settling days the replay runs to reach
steady state would be the infinite reservoir again, wearing a rate.

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

**Format versions.** `READABLE_VERSIONS` is `1–11`, pinned as a literal on
both sides (`planner_setup.READABLE_VERSIONS` ↔ `plannerSetup.SETUP_VERSION`).
v7 carries per-profile `npc_attended`, v8 per-profile `overnight`, v9 the
account-wide `reserved_window`, v10 `prune_to_window` and v11
`merchant_capacity_measured` (named `merchant_model_measured` until v11 shipped:
the model is capacity *and* speed, speed is still assumed, and the old name
claimed both). Each earned a version rather than riding along as an
unknown key — which all five mechanically could, since the body is stored
verbatim and `SetupDocument` ignores extras — because the harmful path is
identical: a build that cannot read one drops it silently, the operator saves
from that build, and the answer is gone from the shared copy. Older documents
load with the field absent; newer ones are **refused, never upgraded**, with a
message that says the server is the older half, and the version is stored as
given.

The last two earned it on exactly the criterion `reserved_window` did — neither
persistence path carried them — and each is worse than the general case in its
own way. `prune_to_window` decides whether `/execute` **deletes** rows from the
game, the only destructive answer the document holds; its resting state is
*on*, so only *off* is an answer somebody gave. `merchant_capacity_measured`
records work done **in** the game that the game does not record and nothing
here can re-derive: a measured +20%/level is indistinguishable from an
untouched one, which is the whole reason the acknowledgement exists (§4.20).
Both are `StrictBool | None`, and absent is "not answered" rather than "no".

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

**Every merchant lever is optional, and every one of them is declared.**
`MerchantModelIn` carries `base_capacity`, `bonus_per_to_level`,
`merchant_reserve`, `merchant_headroom`, `map_span` and
`speed_fields_per_hour`, all `| None`. Absent means "use the planner's own",
which is how the *plan* path already reads a cleared box — `buildPlanPayload`
omits the field and `PlanRequest`'s default decides. `base_capacity` and
`bonus_per_to_level` were required **here alone**, so clearing either made the
whole setup unsaveable: a 422 "Field required" over a figure the operator had
deliberately not supplied, with no cell marked to say which.

The last two were the sharper omission. `buildSetup` writes the whole merchant
model, `map_span` and `speed_fields_per_hour` included, and nothing on this
model declared them — so `_as_plan_request` never lifted them into the request
and `PlanRequest._span_is_odd` never saw them. An even span saved with a 200,
came back out of `GET` unchanged, and the page's own parser then refused the
document **forever**: "merchant_model.map_span is 400". A document the planner
would refuse is refused here. No bound is restated: `_as_plan_request` maps each
lever onto its `PlanRequest` field and lets that model's validators speak, so
there is one copy of each rule — including the `ForeignTarget` coordinate bound
and the own-tile clash of §4.13, both of which are cross-checks the *request*
makes against `map_span` and the snapshot rather than field bounds
`ForeignTarget` could carry alone.

**"Nothing typed yet" is counted off the document, not off the page.**
`buildSetup` writes twelve things and the guard counted three — village
columns, named profiles, role templates — so a page whose only content was a
tribute, a profile's hours, the reserved NPC-burst window, an attendance
answer, an overnight declaration or a deliberately unticked window prune was
told to "fill in a Trade Office level, crop alert or allocation first", and the
one owned answer it held went unsaved on an origin that will drop it. Four of
those earned a version bump precisely because losing them is expensive.
Counting the built document instead means the two cannot drift apart again: a
thirteenth field is counted on the day it is written. Two fields ride on every
document whatever the operator does and so are not content on their own —
`prune_to_window`, whose resting state is on, counts only when **false**; the
merchant model counts only when a lever differs from the planner's own seed
(`merchantModelIsCalibrated`). `merchant_capacity_measured` is content on its own,
because the document carries it only when it is true.

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
"no target", so a declared night is unaffected. **The clamped figure is now in
the response** rather than only rounded into finding prose: `/plan` carries it
as `latency_target_hours`, and `/day-check` carries one per profile as
`segments[].latency_target_hours` — `/execute` has no per-segment plan summary
to put it on. The field reports **what bound the routes, not what was asked
for**, so an overnight profile reports `null`: §6 suspends the target for the
hours nobody is waiting through, the latency pass never runs and the LATENCY
findings never fire, and naming the clamped figure there would read as a target
the night's longer cycles had *missed* rather than one it never had.

`null` there is a claim about the OUTPUT, and it is measured as one. Sweeping
the request's `max_latency_hours` across 2 h, 24 h and 0.5 h on an overnight
window leaves the plan byte-identical — the routes, the merchant assignments,
every derived figure — and leaves the derived night profile byte-identical too,
because `shed_limit` and everything it bounds (what a village may give away,
what a hub draws in, what goes unmet) is a function of stores and production and
never of scheduling. The same sweep on a 16 h day window moves the plan: the
reported target follows the clamp (2.0 / 16.0 / 0.5) and the LATENCY finding
fires at 0.5 h. That contrast is the evidence the suspension does not leak, and
it is what makes `null` an honest report rather than a label.

**There is now exactly one place the target comes from.** The page sends no
`max_latency_hours` on **any** path — `/plan`, `/day-check`, `/night-profile`,
preview, the sweep, the whole-day run — so the standing target is
`PlanRequest.max_latency_hours`, whose default is **2.0 h**. What the page used
to send was the backend's own policy restated on the client: it derived the
figure from the active profile's hours, which supplied no fact the request did
not already carry in `dispatch_window`, and it overrode the server's default
with a number the page had computed. That is the duplicated-default shape — a
change to the backend's target silently overridden by whatever the page last
derived.

On a **segmented** request it was worse than redundant, because it was the
wrong window entirely. The four other active-tab fields are stripped when the
whole-day body is built; this one was not, so selecting the Night tab before
"run the whole day" planned the 16-hour **day** segment against an 8-hour
target — shorter cycles, more routes, more merchants, more rows, on the
endpoint that writes.

**The standing target is an open operator decision**, and it is not free.
Measured on this repository against one night plan: an 8 h target planned **46
routes / 120 merchants**; a 2 h target planned **48 routes / 135 merchants** —
the same account, +12% merchants for a tighter delivery lag. Three options, all
one line: keep 2.0; send 24 on a segmented request so each segment's own window
is the only thing that binds; or expose a latency-target control. Decide it
before the first live run — `docs/26-first-live-run.md` §0.1 — because the plan
confirmed in its step 1 is the plan step 2 writes.

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

**A window prune that did not happen is reported.** The trim runs after the
creates land, and its own failure used to be silent: the run reported the rows
it meant to leave while the whole fan-out kept departing round the clock. Now a
failed or unmade delete raises a `problems` line naming the origin, the row
ids and what the service actually said, and a `stopped` delete stops the run
like any other stop. Either way a `window_pruned` trace event records the
origin, the ids and the status.

**An off-schedule destination is switched off only when its rebuild is funded.**
`disable_existing` used to disable a diverging destination and then discover
that the per-run create cap or the row budget had nothing left for the
replacement, leaving the village receiving **nothing** — reachable on the first
run at a cap of 1 against any village holding a previous plan's routes. The
replacement is now **reserved** out of both budgets before the disable, and a
destination whose rebuild cannot be reserved keeps its rows and is reported
diverging. The refusal names the operator's own control and the real cause,
because "raise the budget or re-run" was wrong in three of the four cases: a
destination this run's filters excluded is not a budget question at all, and a
destination needing more routes — or more rows — than the whole per-run limit
can never be reconciled by re-running however many times.

**A rebuild the game refuses names the destination.** One refusal is below the
consecutive-failure limit, so without this the run reported an empty `problems`
list over a village whose diverging rows had just been switched off and whose
replacement the game said no to. The same holds for a stop landing *between*
the disable and the rebuild: only destinations whose rows this run really
switched off are named, since a disable that failed left them shipping and has
its own report.

**A create whose answer died is settled by the marketplace, not by a verdict.**
An unanswered create is charged and reported `failed`; the read-back that
follows every create then looks for it. Where it finds it, the action is
promoted to `created` with the rows the read-back attributed to it, the
"switched off and could not replace" line is **withdrawn**, the create is taken
back out of the consecutive-failure streak, and — if that streak was what
stopped the run — the stop is lifted too. Left standing, a run that created
everything it attempted still reported the game as refusing the writes and kept
the stop that verdict caused, which skips every remaining origin: a flaky
connection capped every run at two creates.

**The DELETE's own body is read.** `delete_routes` used to discard the return,
so every 2xx was `deleted` — a body naming per-route errors, an HTML soft-block
or a non-object included — and the execute path only re-read the marketplace on
`deleted`, which it always was. One call site from being the only guard on the
one irreversible operation here. It now goes through `_rejected_routes` like
the toggles, and **the shape it applies is marked UNVERIFIED for DELETE**:
`{"routes": [{"id": .., "error": ..}]}` is the game's bulk-*toggle* shape from
its own bundle (`docs/15`), and nobody has observed a DELETE reply on this
account at all. It is a safe assumption — anything the parser cannot read
becomes `unverified` and is settled by re-reading the marketplace — but it is
an assumption, and the first live prune settles it.

**An unreadable cargo update is `unverified`, and protected rows are left
alone.** The same missing evidence `_toggle_routes` calls `unverified`, on the
same endpoint with the same request shape, was reported as `failed` — and
`docs/15` records the empty 200 as the *normal* body here, so `failed` was the
expected outcome of every cargo correction this app makes: the run said "0
corrected" and "the live route is still shipping the old amounts" over rows it
had just rewritten. Separately, a **protected** destination's rows are now
excluded from the drift comparison entirely. A protected route is a hand-made
one, so its amounts always look drifted — and every rewrite also stamps
`deliveries: 1`, so an ally route built at deliveries 3 silently dropped to a
third of its volume with nothing downstream detecting it. Where a protected row
does carry cargo the plan did not set, that is a **warning** saying it was left
as it is.

**The row budget has a default, and the footprint is reported after the trim.**
`max_game_rows_per_run` defaults to **24** — one day of hourly rows, a single
route at the shortest cycle. An unbounded default on the one endpoint that
writes was the opposite of what every other control here does, and the run
already reported the number that nothing bounded, so what was agreed to and
what was written were different units. `0` is still unbounded, and the page
always sends a figure (a blank box is sent as `0`), so the default governs a
caller that omits the field rather than one that clears the box. Two counts,
not one: `observed_game_rows` is what the read-back attributed to a create,
measured **before** the trim because that is when the read-back happens and it
is the question the 24/N fan-out model is checked against; `live_game_rows` is
what the run **left** in the game, and it is the unit the budget is charged in.
A prune that did not happen discounts nothing, so a silently failed trim shows
as the full fan-out rather than as the number the trim was supposed to produce.
`live_game_rows` is carried through `RunSummaryResponse` and the run history,
so the footprint of a past run survives the response that reported it.

**A live run with no trace is refused before the first game request.**
`ExecutionTrace.__init__` catches `OSError`, warns and carries on with tracing
off — right for observability, wrong here. The trace is the only record of what
a run put in a real account: the game returns no id on create, so
`/routes/revert-plan` reconstructs what to undo by diffing against the
pre-write inventory the trace holds. Without it the run is unrevertible and the
500 handler would hand the operator a `trace_id` that 404s. Dry runs never
reach it.

**The irreversible endpoint takes the lock the reversible one takes.**
`/routes/revert-plan` disables, and with `apply_delete` removes rows for good,
and it took none of the guards `/execute` takes. `plan_revert` attributes
everything new since the trace's inventory to the run being undone, so a
concurrent execution puts its own fresh creates into `plan.created` and
`apply_delete` deletes them irreversibly. A second run is now rejected with a
409, exactly as `/execute` rejects one, and the lock is held **across the reads
too** — the comparison is what the deletion is decided from.

**The sweep's "come back for another pass" condition is a fixed point.** It was
gated on unvisited villages alone, so a sweep that had visited everything while
still holding deferred creates reported itself finished: "swept" quietly meant
"swept but only partly provisioned". It is now also unfinished while the
per-chunk budget holds creates — and gated on **progress**, not merely on
`deferred`, because a route whose surviving fan-out exceeds
`max_game_rows_per_run` is deferred by every run alike, so `deferred` is never
empty and the contract had no termination guarantee at all. A pass that
attempted no create is a pass the next one would repeat exactly.

**`/day-check` refuses segments without `prune_to_window`**, the same rule
`ExecuteRequest._segments_are_coherent` makes. `segments` is `min_length=1`
there, so the endpoint is always segmented, and the only segmented `/execute`
is the whole-day run, which forces the prune on. The flag is not cosmetic on
the plan path: it narrows the allowed cycles to the divisors of the window
length and the replay simulates only the in-window firings. Without it the
full-day check the operator reviews is planned on the full cycle set with every
firing simulated, while the run that writes is planned on divisor cycles with
the out-of-window rows deleted — different cycles, different merchant counts,
different row counts.

**`WINDOW_PRUNED` says what is deleted; `WINDOW_NOT_ENFORCEABLE` says what
escapes.** The two are one branch on `prune_to_window` and the pruned half used
to reuse the other's message verbatim — which reads "the destination receives
about 3.0× what was modelled", the exact failure the prune exists to prevent.
Only the critical one reports an over-delivery; the note says how many rows are
removed after the route is created and how many the plan sized the cargo for.

**A measured merchant model can clear its own warning.**
`MERCHANT_MODEL_UNCALIBRATED` fires whenever `trade_office_bonus_per_level`
still equals the shipped 0.20 and any village has a Trade Office — the right
warning for an untouched account, and unanswerable for one whose operator read
a Marketplace capacity at two levels and found 0.20 to be correct, because
agreeing with the default is indistinguishable from never having looked.
`PlanRequest.merchant_capacity_measured` is the operator saying they looked. It
**silences that one finding and nothing else**: no bound, no budget and no
other finding moves, and every figure in the plan is what it was. It covers
CAPACITY only — the base and the Trade Office slope. Merchant **speed** has
never been measured on this server, which is why the field is not called
`merchant_model_measured`: a name that claimed the whole model would have let
the unmeasured half ride along on the operator's tick.

**Absence is never finalised from an unstable pair.** The stabilising read
already stopped an unsettled marketplace from being *deleted* from; it did not
stop the same page producing a *verdict*. With the two reads disagreeing, "the
later read classifies" still let a create the game was a moment slow to show be
recorded as **refused** — and a refusal releases its rows back to the budget,
drops the destination into the consecutive-failure streak and can stop the whole
run, all over a route that then appears. A create whose answer died and whose
rows are absent on a page that would not hold still is now
**`indeterminate`**: a fourth action status, distinct from `failed` (the game
refused it) and from `created_unverified` (the write was *accepted*). Its row
charge stands, the streak ignores it — neither counted nor breaking a genuine
streak — the destination is named in `problems`, and the next run settles it
against a freshly read marketplace, where an existing row already satisfies the
plan. `read_back_disagreed` now carries **both snapshots**, not just a reason
string: when nothing on a page is final, the recovery question is which rows
moved between the two reads, and only the rows answer it.

**A destination is settled as a whole, not one row at a time.** The read-back
runs BEFORE the trim, so every row a create made is still on the page —
including the ones the trim is about to remove. Attributing those rows per
create was guesswork on a shared destination, because an hourly fan-out covers
every minute a 4-hourly one has: a whole-day run whose *Day* create produced
nothing had it claim six of Night's rows, report itself `created` over nothing,
and leave Night reported eighteen rows short — and the pooled trim then kept and
DELETED on that same split. The verdict is now taken per destination against the
complete expected **pre-trim multiset** of (departure minute, cargo). Every
expected row present means every create landed; short by exactly the rows of one
create that no other create wants means that one produced nothing, by
elimination; anything else attributes nothing at all — the creates become
`indeterminate`, the destination is named in `problems` and **is not trimmed**,
because there is no telling which rows the trim would be removing.

The invariant that guarded the old matcher is narrowed to match. Two routes to
one destination no longer have to be separable row by row, so the plan is
refused only for the pair nothing can separate at any stage: identical cargo on
an **identical** full pre-trim fan-out with different windows that overlap. Two
cycles that merely overlap are now allowed — the destination's multiset says
which of them is missing — and the whole-day pair keeps its exclusion for a
sharper reason than before: its windows are *disjoint*, so every surviving
minute belongs to exactly one route and the trim's decision is determined.

**A wholly refused replacement is put back, under guard.** Disable was chosen
over delete on the off-schedule path *because* it is reversible, and the
reversibility was never used: a destination whose rebuild the game refused was
left dark — old rows off, no new ones — under a `problems` line telling a human
to go and switch them back on. The rows are still there and the write-ahead
record says exactly what they were, so the run now does it: **one** re-enable of
exactly the recorded row ids, verified by a read, traced
`restore_attempted` → `restored` / `restore_failed`, and reported under
`re_enables`. It is all-or-nothing and heavily guarded, because an automatic
write over a half-known state is worse than a dark village: every reserved
replacement create must have been written and have produced nothing, the page
must have read the same way twice, no replacement row may exist, and the
destination must still be exactly what the record describes. A part-written
rebuild, an `indeterminate` create, or a destination that changed underneath is
`replacement_abandoned` with the reason and left for a human — switching old
rows back on beside a new route that may exist would ship two schedules at once,
which is the state disable-and-recreate exists to avoid.

**The disable record says what happened to each row.** `rows_disabled` closed
the write-ahead chain with "whatever the game answered", which stops the record
dangling and does not make it recoverable: the answer is one status for a whole
batch, and the one that matters most — `unverified`, which a reset, a
session-expiry redirect or an unreadable body all produce — says nothing at all
about the rows. Reading it as a refusal is the same over-statement
`_toggle_routes` itself stopped making. The record now carries a verdict per
**row**: `confirmed` (the page shows it off), `failed` (it is not off), and
`unknown` (nobody can say). For an `unverified` answer the verdicts come from
the disable's **own** read-back — which the run already takes, at that point,
to decide whether it may create on top — so nothing extra is read, and a row
that read-back could not see is `unknown` rather than assumed.

**Consent to write is resolved once, at the boundary.**
`_execution_mode_is_unambiguous` decides from `model_fields_set`, which is
request-boundary provenance and nothing else — and the handler derives models
from the body: `model_copy(update=...)` builds a per-segment body for a
whole-day run and marks the fields it updates as explicitly set, while any
`model_dump()` → re-validate round trip emits every default and makes *all* of
them look explicit (a live body round-tripped that way now carries an explicit
`dry_run: true` and is refused as contradictory). Reading `execution_mode` off
a derived model could therefore reach a different answer than the operator
gave, on the one endpoint where the answer is "may this touch a real account".
The mode is now read off `body` exactly **once**, into a plain local, and
`dry_run` is not read in the handler at all — pinned by a structural test that
parses the handler rather than by behaviour, because the failure it guards
against is invisible until the day a derived model is passed where `body` is
expected.

**Going live for the first time is its own document.**
[`docs/26-first-live-run.md`](26-first-live-run.md) is the step-by-step
protocol these fixes earned — what to settle before anything touches the game,
what to read in the dry run, one route on a village you can watch, a rehearsed
undo, and the stop rules. It is written against this section: several of its
steps exist to make the first *observation* of a behaviour recorded here.

## 4.21 One predicate, three readers — the page's own refusal

`min` and `max` on a number input bound the **spinner** and nothing else. A
typed or pasted figure sails straight past both: `aria-invalid` stays null, the
cell says nothing, and the button beside it posts the figure to be refused as a
server 422 — a response that names a pydantic field path, arrives after a round
trip, and leads nobody back to the cell that caused it. Browser-confirmed
before any of this went in: typing 21 into a Trade Office box posted
`trade_office_level: 21` with `aria-invalid = null`.

`frontend/src/utils/plannerBlockers.js` is the gate, and it is **three lists,
not one, because the audiences differ**:

| list | what it holds | what it refuses |
|---|---|---|
| `planBlockers` | the document and the plan | Build plan, Save, Export, the full-day check, the night derivation, the run |
| `runBlockers` | the three controlled-run boxes | Preview and the live run alone |
| `nightBlockers` | the night fill pair | `/night-profile` alone |

The controlled-run boxes are neither plan inputs nor setup-document fields, and
the fill pair reaches one endpoint, so refusing `Build plan` or `Save setup`
over either would refuse a request the figure has no bearing on — the same
class of mistake as not refusing one it does. `describeBlockers` therefore says
"what **its field** accepts", not "what the plan accepts": an operator who
pasted 51 into "Routes this run" was told their figure was outside what the
plan accepts, about a box `Build plan` deliberately never reads.

**Computed from state, never swept out of the DOM.** A
`document.querySelectorAll('[aria-invalid]')` scan is tempting — it is by
construction whatever the cells say — but each stage mounts only its own
tables, so a scan run from the Targets stage cannot see the Account table's
cells, which is precisely the reported failure. What the gate shares with the
cells instead is the **predicate**: `isMaxBusyMerchants`, `unreachableCaps`,
`isStockFloorFraction`, `isConsumptionRate`, `isEmptyTemplate`,
`isAssumedCropRate`, `isTradeOfficeLevel`, `isCropCeiling`, `isSafetyMarginPct`,
`nightFillProblems`, `merchantModelProblems`, `relayTierProblemsByVillage` and
`resolveVillageNames` are the same functions in `plannerSetup.js` that the
cells call, so a mark and a blocker cannot come from two different rules. The
module is pure, which is the other half of why it does not read the DOM: the
whole gate is testable without a browser.

**Nine boxes, each on the button it actually reaches** — the list
`e2e/plannerBounds.pw.js` keeps in its own header:

* Trade Office level → Build plan
* Merchant base capacity → Build plan, and both Save writers
* Crop stock alert → the full-day check (`Run (0 requests)`)
* Foreign-target margin % → Build plan
* Emptied to % / Full to % → Derive from stores
* Routes this run → Preview / live run
* Max rows this run → Preview / live run
* Never disable → Preview / live run / the sweep

A refusal is not just a toast. `refuseBlockers` switches to the stage that
mounts the offending cell and drops the caret into it, so the fix is one
keystroke from the refusal — the pattern the page already had at
`activeAttendanceOwed` and nowhere else.

**The declared relay tier is in the gate, not only on the cell.**
`relayTierProblemsByVillage` feeds a `Relays for` entry into `planBlockers`, so
a tier the setup store would refuse on `PUT` — a relay feeding a relay, a role
village relaying, a downstream the snapshot does not contain — is refused
before the document is written rather than after.

**The reconciliation sweep is gated like every other write path.**
`runReconcileSweep` checked only that a plan existed and then posted
`dry_run: false`, so every marked cell Preview refuses — an unparseable "Never
disable" entry, an even `map_span` that decides what a marketplace row's map id
turns back into, an unresolved "Not from" exclusion — went straight to a live,
disabling run. It is also the one write button that carries no live-run
confirmation dialog, which is why the gate mattered most there.

**`/day-check` is gated on the same list**, and for a sharper reason than
`Build plan`: the request carries `buildPlanPayload()` verbatim **and** the
crop alert levels typed in the table below it, so a figure the plan would not
send was posted from here without the button ever hesitating.

**The constants both sides copy are pinned as literals on both sides.**
`tests/test_frontend_mirror_constants.py` ↔
`frontend/src/utils/plannerSetup.test.js`: `DEFAULT_MERCHANT_MODEL`'s four
levers, `TRAVIAN_REPEAT_INTERVALS` against `DAILY_BEAT_CYCLES`,
`MAX_DAY_SEGMENTS`, `MAX_STOCK_FLOOR_FRACTION`, `MAX_TRADE_OFFICE_LEVEL`,
`MAX_MERCHANTS_PER_VILLAGE`, the run-control ceilings (50 routes, 2,000 rows)
and the row budget's default of 24. A literal on each side deliberately:
asserting one side against the *other's* value would pass however far both had
drifted from what the game does. The Python half also proves the bounds
**bite** rather than merely being declared, since a bound asserted through
`model_fields` alone would survive the field being replaced by one that
validates nothing.

**The live-writes flag defaults ON, and the library's default is deliberately
the opposite.** `Settings.trade_route_live` defaults **True**, since
2026-08-27, at the operator's explicit instruction — the opt-in kept reverting
to preview-only on every server restart. `TradeRouteService.__init__` keeps
`live_enabled: bool = False`: that is the library's own safe default, which
every test and every direct construction relies on, and only
`web/sessions.py` overrides it with the settings value. The two disagreeing is
the design, not drift — do not "fix" the constructor. What this means in
practice is that **unset means live**: see `docs/26-first-live-run.md` §0.2 for
turning it off for the steps that must not write, and note that settings are
rebuilt per session, so a reconnect suffices and no server restart is needed.

## Immediate next actions

| # | Action | Cost | Unblocks | Status |
|---|---|---|---|---|
| 1 | Read merchant capacity at a *second* TO level — a **TO 0** village for preference, because its capacity *is* the base (`merchants.py` records 03 and 26 as TO 0) | 0 requests | pins base and slope together; today only the base is read | open — base 2,500 (2026-09-02), slope unmeasured, 7,920 @ TO 13 superseded. The reading now has somewhere to live: tick the merchant-model acknowledgement so `MERCHANT_MODEL_UNCALIBRATED` stops asking (§4.20), and follow `docs/26` §0.6 for the procedure |
| 2 | Capture Resources + Capacity + one *filling* village `dorf1` together | 0 requests | R2 filling branch | open |
| 3 | One-off `dorf2` sweep for TO + Marketplace | N requests | §1 OWNED state, and refreshes the per-village TO levels R1 found drifted | open |
| 4 | Create one trade route and observe its phase and any per-village cap | in-game | R6, open question #1 | open — `docs/26-first-live-run.md` §2 is the protocol for exactly this, and §5 carries the starved-route experiment that settles the partial-load assumption above |

Two decisions belong to the operator before any of the above, and neither has a
default the code can supply: the **standing latency target** (§4.19) and
whether live writes stay on for the first run (§4.21). Both are named in
`docs/26-first-live-run.md` §0.
