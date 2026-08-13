# Resource Distribution Planner — Profiling, Review & UI Design

**Project:** `travian-auto-player` · **Server:** Europe 2, x1, 3 tribes · **Account:** `Chieftain` (Teutons)
**Status:** pre-development. Part I is the operator's profiling doc. Parts II and III are a technical review of it.

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
| Capacity | `Base_Merchant_Size × (1 + 0.2 × trade_office_lvl)` | `Base_Merchant_Size` configurable, default 2200 |
| Speed | 12 fields/hour | Teuton |
| Count per village | derived from Marketplace level | not always 20 — observed 13 and 19 |
| Reserve | 2 idle per village (config) | usable = count − reserve |

> ⚠️ **Both constants in this row are disputed — see Review R1. Do not build on them until settled.**

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

### 3.5 Topology rules (user-imposed)

- No waterfall for W/C/I — materials must not chain A→B→C.
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

Show TO alongside what upgrading buys, so the field doubles as "which Trade Office to build next":

| V | TO | Cap/merchant | Merchants used | At TO+1 | At TO+5 |
|---|---|---|---|---|---|
| 16 | 0 | 2,200 | 9 | 2,640 → 8 | 4,400 → 6 |
| 05 | 11 | 7,040 | 16 | 7,480 → 15 | 9,240 → 12 |

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
| Waterfall | forbidden for W/C/I; permitted for crop via sub-hub |
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

Multi-commodity min-cost flow with integer cycle and merchant variables plus per-node capacity — NP-hard in general. N=20 is small, so: cluster → assign hubs → per-route cycle sweep → local improvement. MILP (HiGHS/CBC via PuLP) later.

## 15. Known issues & guards

| # | Failure | Guard |
|---|---|---|
| 1 | Shipped the target instead of the gap | assert `shipped == target − own_production` |
| 2 | Two-way pair with opposite cargo | forbid duplicate (pair, resource); net before emitting |
| 3 | Gross crop treated as net | never read crop from the Production tab |
| 4 | Merchant base size wrong (1000 → 1600 → 2200) | config value; verify empirically |
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
| 4 | Map span for toroidal wrap on Europe 2 | distance correctness |

## 17. Build order

| Phase | Deliverable |
|---|---|
| 1 | §1 State & Fetch |
| 2 | §3 Allocation Input |
| 3 | §4 Flow Optimizer |
| 4 | §5 Schedule Solver |
| 5 | NPC, storage, apply, monitor |

## Appendix A — Regression fixture

The current 20-village plan is the optimizer's golden test case: net production, coords, TO levels, allocation targets in; route set, per-village merchant pool, total ~115 merchants, all villages ≤ 18 out. Exercises: an army village with large negative net crop, a `remainder` village, a stranded village, two sub-hubs, a priority village held at 3/20, a foreign crop tribute, sub-20-merchant villages, and unknown TO defaulting to 0.

---

# Part II — Technical review

Findings are ordered by how much damage they do if left unaddressed.

## R1 — BLOCKER: the merchant capacity constants look doubly wrong

§3.1 uses `Base_Merchant_Size = 2200` with `1 + 0.2 × TO`. Published Travian values for **Teutons** are **base 1000** and **+10% per Trade Office level** — the +20% figure is the **Roman** rate.

Both errors push the same way: they overstate capacity, which under-provisions merchants — the direction §5.2 itself labels **unsafe**.

Compare V05 (TO 11) from §5.3:

| Formula | Capacity/merchant |
|---|---|
| doc: `2200 × (1 + 0.2 × 11)` | **7,040** |
| stock Teuton: `1000 × (1 + 0.1 × 11)` | **2,100** |

A 3.35× overstatement. Every route sized against it needs ~3× the budgeted merchants — precisely known-issue #6.

The correction history in issue #4 (`1000 → 1600 → 2200`) is itself the tell. Under the Teuton +10% rule those are exactly `TO 0 → TO 6 → TO 12` on a base of 1000. That is the signature of **sampling different villages and mistaking a Trade Office bonus for the base** — after which the formula applies the same bonus a second time.

Other multipliers that could legitimately produce a non-stock number, and which are **not** constants:

- **Trade artifacts** multiply merchant capacity, can be lost or captured, and some are account-wide while others are single-village.
- Server speed multiplies capacity, but Europe 2 is x1.

**Decisive test — zero requests.** Open the Marketplace in two villages with *different* TO levels and read the capacity the game states. Two observations solve `cap = base × (1 + k × TO)` for both unknowns:

```
k    = (cap_b − cap_a) / (cap_a × b − cap_b × a)     # a, b = the two TO levels
base = cap_a / (1 + k × a)
```

Pick a TO-0 village for one sample and `base = cap` directly. **Nothing downstream should be built until this is pinned**, and the resolved values belong in a test fixture, not a config default.

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

At 22 villages that is a 22-request one-off, re-run only when a village is added. Treating TO as OWNED state (§5.2) is the right call; the scan just costs half what the doc budgets.

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

## R6 — Dispatch phase may not be a free variable

§9 assumes `dispatch_min` is chosen. In Gold Club, a trade route's phase is set by **when the route is created** — it repeats every N hours from that moment. If so, the planner cannot set dispatch minutes; it can only tell the operator **what time to create each route**, and the sheet needs a "create at HH:MM" column rather than a "dispatch" column.

This is unverified and sits directly under the schedule solver. It should join open question #1 as an empirical check before §5 is built — and it is cheap to answer by creating one route and observing.

## R7 — Storage safety only models the filling direction

§10's `fill_time_h = (capacity − stock) / net_inflow` returns a negative number for a village with negative net crop, which is the *starving* case and the one that actually kills troops. It needs the second branch — `empty_time_h = stock / −net` — and the warning threshold applies to both. This is the same l4/l5 trap resurfacing in a different module, which argues for a single shared `crop_status()` helper rather than per-module arithmetic. One now exists in `web/routes/status_export.py`.

## R8 — Routes persist; plans do not

§13 alerts on production drift, but an applied trade route keeps shipping at its configured cargo regardless. If a sender's production falls — troops rebuilt, an oasis lost, a village chiefed — the route drains the sender's stock and the tool only *notices*. Two guards worth speccing now, because they are cheap at design time and expensive to retrofit:

1. size routes against **sustainable surplus with margin**, not the instantaneous snapshot rate
2. make drift beyond X% **trigger a re-plan proposal** with a route diff, not just an alert

## R9 — Smaller notes

- **§3.1 speed 12 f/h is tribe-specific.** Correct for Teutons; if the tool ever handles another account it belongs with the base capacity in a per-tribe table, not in global CONFIG.
- **Open question #4 (map span) is answerable from data you already hold** — village coordinates and any map-scan output bound it; `docs/05-map-system.md` covers the map system.
- **Appendix A's fixture must be frozen, not live.** The account has already gone 20 → 22 villages; a golden test that reads current state stops being a regression test.
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
- Row action **"Scan buildings (1 request)"** for a single village; a header action for a full sweep with the cost stated (`Scan 22 villages (22 requests)`).
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

## Immediate next actions

| # | Action | Cost | Unblocks |
|---|---|---|---|
| 1 | Read merchant capacity in two villages with different TO | 0 requests | **R1 — everything** |
| 2 | Capture Resources + Capacity + one *filling* village `dorf1` together | 0 requests | R2 filling branch |
| 3 | One-off `dorf2` sweep for TO + Marketplace | N requests | §1 OWNED state |
| 4 | Create one trade route and observe its phase and any per-village cap | in-game | R6, open question #1 |
