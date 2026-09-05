# Part I — The resource distribution planner

This is the tool's largest and most consequential subsystem. Sections follow its pipeline: **Snapshot → Allocate → Optimize → Beat → Execute → Storage safety.**

## I.1 Snapshot — what can be read, what it costs, and which numbers lie

### I.1.1 The crop trap (the single most important read in the tool)

Three different "crop" numbers exist and two of them will get armies killed:

| Number | Definition | On a starving village |
|---|---|---|
| **Gross crop** | cropland output × bonuses | Always positive |
| **Free crop** | crop production − *population only* | **Positive.** Troop upkeep is not subtracted |
| **Net crop** | gross − population − *troops* | Negative, shown red |

VERIFIED — support: *"Free crop is your crop production minus your total population."*

In the page data these sit in adjacent fields: `production.l4` is net, `production.l5` is free crop. A tool reading `l5` believes a starving hammer village is healthy.

**Current code is correct** — `crop_per_hour = production["l4"]`, `free_crop = production["l5"]`, both parsed and distinctly named. It goes further and sidesteps the question entirely by inverting the granary countdown:

```
draining:  net = −stock / hours
filling:   net = (capacity − stock) / hours
```

Inverting the server's own arithmetic means no troop table, no population figure, and no ambiguity about which village feeds troops standing somewhere else. This is a better design than any upkeep model, and it is why the repo has no unit-upkeep table at all — correctly so.

**Still open:** the **draining branch is verified against a live village to 0.5%; the filling branch is not validated** and reportedly disagrees with gross production on at least one village. No public documentation resolves this. It needs one live capture of Resources + Capacity + a *filling* village's `dorf1` together.

**What to look for:** any path that falls back to `0` when net crop cannot be derived.

**Direction of danger: a silent zero is the worst possible value here** — worse than an exception, worse than a wrong number, because zero reads as healthy and suppresses every downstream warning. The current model returns `None` and skips the village. That is right and should be defended in review.

### I.1.2 Request cost of a full snapshot

| Datum | Source | Cost |
|---|---|---|
| Stocks + merchants free/total, all villages | `/village/statistics/resources` | 1 |
| Granary countdown | `/village/statistics/resources/warehouse` | 1 |
| Warehouse / granary capacity | Capacity tab | 1 (cacheable) |
| Trade Office + Marketplace level | `dorf2` per village (`gid 28` / `gid 17`) | **N**, one-off |

The account-wide statistics path is **Travian Plus gated**; without Plus it degrades to one `dorf1` per village. PARTIAL.

At 26 villages the Trade Office sweep is a 26-request one-off, re-run only when a village is added. Both Trade Office *and* Marketplace level come out of the same `dorf2` parse, so no second mechanism is needed. Warehouse and granary levels (`gid 10`/`11`) come free from the same sweep if capacity is ever wanted from levels rather than the Capacity tab.

There is **no account-wide source for building levels** — the Central Village Overview exposes Overview / Resources / Culture points / Troops only, and GraphQL carries no building fields. One request per village is the floor. Checked and ruled out, so a reviewer should not expect a cheaper path to exist.

**Merchant count is read directly, not derived.** The stock rule is one merchant per Marketplace level, L20 → 20, linear (VERIFIED), and there is **no Great Marketplace in Legends** — that is a different game, do not model it. The code nonetheless reads `merchants_total` off the statistics page rather than deriving it from Marketplace level, and the parser notes 2 and 19 observed alongside the usual 20. Both are consistent with the linear rule, but reading directly is strictly safer.

**Watch the `free/total` cell parse: the second number is the count, the first is how many are idle right now.** Reading the first understates capacity on any village whose merchants are currently out — which, in a working plan, is most of them. Quiet failure: the optimizer would think the account is far more constrained than it is and strand villages needlessly.

### I.1.3 Validation every fetch needs

- Reject all-zero payloads (observed in practice).
- Flag villages missing from the response.
- Flag villages present in the response but absent from owned state → new village, prompt a Trade Office scan.
- Warn when snapshot age exceeds a threshold.
- Assert per-village figures sum to the reported account total, per resource.

## I.2 Allocate — the gap, not the target

### I.2.1 The error made twice by hand

If village B needs 10,000 crop/hour and produces 4,000 itself, the cargo is **6,000** — not 10,000. Shipping the target instead of the gap over-delivers to B (overflow) *and* over-drains the senders (starvation) simultaneously. Both failure modes from one arithmetic slip.

```
shipped = max(0, target − own_production)
```

**Current code:** `VillageAllocation` computes `ship_per_hour` internally, so there is no way to obtain a target without the ship figure beside it. The invariant lives in the data model rather than in a check. That is the right shape — validation messages don't prevent data-entry errors, input types that cannot express the error do.

**What to look for:** anywhere a target rate reaches cargo sizing without subtraction — especially `sustain + X%` mode, where the derived figure is a deficit *plus* headroom and the signs are easy to invert.

### I.2.2 Sign convention

`ship_per_hour` positive means **into** the village; negative means the village sends that much away. Crop production is itself legitimately negative on army villages, which is exactly why sustain mode exists. Two independent sign conventions meeting in one expression is where `l4`/`l5`-class bugs breed.

**What to look for:** any comparison against zero where the convention isn't locally obvious. Every number should carry its unit *and* its sign convention — rates are `/h`, stocks absolute, cargo per-send. Mixing those three is how the target-vs-gap error happened.

### I.2.3 Netting, and what it structurally forbids

Netting each village to one figure per resource makes two known issues impossible rather than merely checked:

- **Two-way pairs** — a village cannot be both sender and receiver of iron, so `A→B` and `B→A` for iron cannot coexist.
- **W/C/I waterfall** — a village that nets as a lumber receiver cannot also send lumber, so `A→B→C` cannot form.

Both are properties of the data model, not of vigilance, and are asserted in tests rather than defended at runtime. Good.

But netting also makes **crop relay through a sub-hub inexpressible**, which the profile explicitly permits (§3.5). A relay needs multi-leg flows in the optimizer, not a scheduling change. **This is the highest-value unbuilt planner feature for a 26-village account**, because relay is precisely how a real player gets crop to a distant hammer without paying full round-trip merchants from every feeder.

**What to look for:** don't accept a runtime check where the model already guarantees the property — and don't accept any claim that relay works, because netting forbids it.

### I.2.4 Slack

Percentage allocations summing to 96% leave 4% unassigned. Exactly one `remainder` village per resource must absorb it: zero is a hard error, two is ambiguous. The UI encodes this as a radio group per resource column, so "exactly one" is structural rather than validated.

### I.2.5 Modes

Per resource, per village: `percentage` (share of account production of that resource), `absolute` (fixed net/hour), `remainder` (receives everything unallocated), `sustain + X%` (cover a negative-net-crop village's deficit plus headroom), and named role templates applied across many villages. Mixed modes within one village are normal, not an edge case.

## I.3 Optimize — merchant physics

### I.3.1 Capacity

```
capacity = base × (1 + trade_office_rate × TO_level)
```

Trade Office is **additive on the base**. Stock values, VERIFIED (Travian Fandom "Merchant" states plainly that Gauls and Teutons gain 10% of base per level while Romans gain 20%, and that the Trade Office has no effect on merchant speed):

| Tribe | Base | TO rate/level | Speed (fields/h) |
|---|---|---|---|
| Roman | 500 | +20% | 16 |
| Gaul | 750 | +10% | 24 |
| Teuton | 1,000 | +10% | 12 |

**This account is modified** — see §0.1. Measured `2500 / +20%` (base re-read 2026-09-02; 2,200 was the superseded reading — see 01-operator-rulings §1).

**Current code:** `EUROPE2_TEUTON = MerchantModel(base_capacity=2500, bonus_per_trade_office_level=0.20)`, held in one injectable model with `calibrate()` to derive base and slope from two observations, and `STOCK_TEUTON` retained for contrast. Nothing else in the planner hardcodes a capacity. Correct, and the seam is the right one.

**Still pinned by a single data point.** Any `base × (1 + 13k) = 7,920` fits the observation — `2200/0.20` is the natural reading but not the only one. A second village at a *different* Trade Office level nails it at zero request cost, since the levels are already held. A Trade artifact can also change capacity mid-server, which is another reason the constant must stay derivable rather than fixed.

**Alliance Commerce stacks MULTIPLICATIVELY with Trade Office.** VERIFIED — support "Alliance Bonuses" worked example: Gaul `750 × 2.2 (Commerce L4) × 5 (TO20) = 8,250`, explicitly stating the Commerce bonus is multiplied with the Trade Office. So:

```
effective_capacity = base × (1 + TO_bonus) × (1 + commerce_bonus)
```

`MerchantModel` has **no Commerce term.** If the alliance runs Commerce, real capacity exceeds modelled and the planner over-provisions merchants.

**Direction of danger: safe but wasteful.** Adding Commerce is a cost optimisation, not a correctness fix. But adding it *wrongly* — additively, or applied to the wrong base — flips it to over-estimating capacity, which is the dangerous direction. If it gets implemented, it needs the worked example above as a test.

### I.3.2 Speed, and what does not affect it

Merchant speed is fixed per tribe. **Trade Office does not affect it** (VERIFIED, as above). **Tournament Square does not affect merchants** — VERIFIED, Fandom Tournament Square states merchant speed is not affected, and independent travel calculators agree that merchants gain nothing from it. Speed artifacts, the hero's horse, pennants and standards are all troop-only.

**Current code:** `_TRIBE_MERCHANT_SPEED = {1: 16.0 Roman, 2: 12.0 Teuton, 3: 24.0 Gaul, …}` matches stock exactly, and `MapGeometry` accepts only `speed_fields_per_hour` — no Tournament Square or artifact term exists anywhere in the geometry path. Correct.

One caveat worth raising in review: merchant speed is a constant a modified server could also change, and **unlike capacity it has not been measured on this account.** Wrong speed inflates travel time → `sets_in_flight` → merchant counts, and can flip `over_budget`. One timed real send settles it.

*(A 2024 annual-special Commerce bonus does add merchant speed on some special worlds. Not standard 3-tribe Europe. Ignore it here, but note it as a reason the constant belongs in a per-world table rather than global config.)*

### I.3.3 Distance — toroidal wrap

Europe 2 runs **−200 … +200, span 401**, wrapping on **both** axes. The shortest path between two villages may cross the map edge rather than run through the middle: a village at x = −190 and one at x = 190 are near neighbours, not 380 fields apart.

```
dx = min(|x1 − x2|, span − |x1 − x2|)
dy = min(|y1 − y2|, span − |y1 − y2|)
dist = sqrt(dx² + dy²)
ow_minutes = dist / speed_fields_per_hour × 60
rt_minutes = 2 × ow_minutes
```

**MEASURED, with the proof sitting in the codebase:** a captured create request targeting (23|88) resolved to `mapId 45136`, against

```
mapId = (S − y) × span + (x + S) + 1        where S = (span − 1) / 2
```

With span 401, `S = 200`: `(200 − 88) × 401 + (23 + 200) + 1 = 112 × 401 + 224 = 45,136` — exact, and matching the destination the marketplace page then listed for that route. Span 801 would give 250,136. **Any source claiming ±400 / span 801 for this world is wrong.**

The span must be **odd**, because a world is centred on 0|0. The code validates this and `map_id_to_coords` returns `None` on an even span rather than silently shifting every tile index by half a field.

**A wrong span causes two failures that differ in loudness — worth separating in review:**

- **Distance: quiet.** Every real village sits inside ±200, so with an over-large span `min(raw, span − raw)` always picks `raw`. Distances stay correct while wrap protection silently disappears. Nothing visibly breaks.
- **`map_id_to_coords`: loud and total.** This is how a route read off the marketplace page is matched back to a plan — the page names destinations by village id and map id and carries **no coordinates at all**. A wrong span breaks reconciliation, and reconciliation is what revert depends on.

Some special worlds have **hard map edges.** There is no flag exposing it, and the planner always wraps, so distances near the seam would be wrong there — hard-edge worlds are unsupported, deliberately. Standard Europe wraps (VERIFIED, support "Guide: The Map": the map wraps around like a globe, so the shortest path may run through the edge).

**Trade ships** do not exist on standard Legends worlds. The create payload pins `useTradeShips: false`, correctly. On a ship world the planner would under-use what the account could move — a known, accepted limitation rather than a defect.

### I.3.4 The cost model, and why it is non-monotonic

A merchant is committed for the **full round trip** and is unavailable until it returns home. Therefore:

```
batch           = hourly_cargo × cycle_h
m_send          = ceil(batch / merchant_cap[sender])
sets_in_flight  = ceil(rt_minutes / (cycle_h × 60))
pool            = m_send × sets_in_flight
```

**The double ceiling makes `pool` non-monotonic in cycle length.** Worked example — V10 → V02, 9,323/h, round trip 532 min, capacity 5,720 (independently re-derived; reproduces exactly):

| cycle | batch | `m_send` | `sets` | pool |
|---|---|---|---|---|
| 1h | 9,323 | ⌈1.63⌉ = 2 | ⌈8.87⌉ = 9 | 18 |
| 2h | 18,646 | ⌈3.26⌉ = 4 | ⌈4.43⌉ = 5 | 20 |
| **3h** | 27,969 | ⌈4.89⌉ = 5 | ⌈2.96⌉ = 3 | **15** |
| 4h | 37,292 | ⌈6.52⌉ = 7 | ⌈2.22⌉ = 3 | 21 |

`sets = ceil(rt / cycle)` is also correct at the boundary: at `rt = 360, cycle = 180` it yields 2, and the first set returns exactly as the third dispatch falls due, so 2 genuinely suffices.

**What to look for:** any code that assumes longer cycles are cheaper, or that shorter ones are. Both are wrong. Sweep the whole allowed set per route and never infer a direction.

### I.3.5 Per-village merchant budget

```
Σ pool ≤ merchants_total − reserve
```

The reserve exists because a village with zero idle merchants cannot respond to anything by hand — an NPC burst, an emergency crop shipment, a tribute top-up.

**Ordering matters:** permanently-committed merchants (a foreign tribute) must be subtracted **before** the reserve. Otherwise a tribute-paying village ends up with no idle merchants while the reserve appears to have been honoured — quiet failure, and precisely the village where manual intervention is most likely to be needed.

The code carries both a flat `DEFAULT_MERCHANT_RESERVE` and a proportional `DEFAULT_MERCHANT_HEADROOM = 0.10`. These answer different questions — the reserve says "never plan with the last two," the headroom says "leave a fraction free proportional to the village's size." Check they compose rather than double-count.

### I.3.6 Objective and escalation ladder

Objective, in priority order: minimise total merchants committed → minimise maximum end-to-end latency → maximise free merchants at priority-flagged villages → minimise route count.

Escalation when a village doesn't fit: sweep other cycles → reroute via a nearer hub → split cargo across paths → recommend a Trade Office upgrade showing Δmerchants → declare infeasible and strand the village.

**Steps 2 and 3 — hub consolidation and cargo splitting — are unbuilt.** The optimizer sweeps cycles, recommends a Trade Office upgrade, then declares infeasibility rather than quietly trimming a route to fit.

**That refusal is correct behaviour and should be defended in review.** A route trimmed to fit the budget under-delivers silently, which is the exact failure the tool exists to prevent. Verify the infeasible path really refuses rather than degrades.

Concrete illustration of why: a village at 105 fields needing roughly 55 merchants cannot be served at all. The right answer is local consumption, not a heroic route.

**The gap in the objective worth raising.** Nothing in that ordering knows that **wood is this account's binding constraint** (near-zero surplus) while clay sits in large surplus. A wood route and a clay route at identical merchant cost are not equally valuable, and a wood route sized against an instantaneous snapshot is far riskier than a clay one. Per-resource margins — tight on wood, loose on clay — would encode the account's actual shape. This is the "size against sustainable surplus with margin" guard applied per resource rather than uniformly.

## I.4 Beat — what the game will actually accept

### I.4.1 A trade route is not a cron job

This is the structural fact most likely to mislead someone reading the code as software. **"Repeat every N hours" creates 24/N independent scheduled rows**, each with its own id, its own departure time, individually editable, deletable, and enable/disable-toggled.

VERIFIED — support "Trade Routes" worked example: start at 04:00, 2 deliveries, repeat every 4 hours, and the game creates routes at **04:00, 08:00, 12:00, 16:00, 20:00 and 00:00**, with each adjustable separately via edit at any time. **MEASURED against this account:** a 1-hour route produced 24 rows, and deleting one left 23.

Two consequences the planner leans on:

- **A time window is enforced by subtraction, not by a setting.** Travian offers no way to confine a route to part of the day — and does not need to. You create the route and delete the rows departing outside the window. `window_pruning.py` is built entirely on this, keyed on `departure_at % 86400`, which needs no timezone because it is the same minutes-past-midnight the create payload's `hour`/`minute` use. Confirmed against the game, which returned 1410 for a route asked to leave at 23:30.
- **Row footprint is real.** A 1-hour route across 20 origins is 480 rows. If a per-village cap exists, short cycles hit it first.

### I.4.2 Allowed repeat intervals

**Whole hours from a fixed set: 1, 2, 3, 4, 6, 8, 12, 24.** VERIFIED — the in-game URL parameter is `&every=N`, documented with exactly that set. Not arbitrary minutes; not every integer from 1 to 24.

Those are the divisors of 24, which is what makes a readable daily beat possible at all: every cycle divides the day, so the schedule repeats every 24 hours and can be written as one table. With an arbitrary cycle set the pattern's period is the lowest common multiple of every cycle, and the output stops being a schedule anyone can read or verify.

**Current code:** `DAILY_BEAT_CYCLES = (1,2,3,4,6,8,12,24)`, validated at the API boundary — `max_cycle_hours` rejects non-members and names the allowed set in the error. `ALL_CYCLES = 1..24` is retained so the optimizer can *quantify* what the restriction costs ("a 5h cycle would save 2 merchants but breaks the daily beat") rather than hiding it. That is the right call: because `pool` is non-monotonic, the restriction sometimes genuinely costs merchants, and the operator should see the number instead of being silently constrained.

**What to look for:** a 60-minute beat table cannot represent a multi-hour cycle at all. A 3-hour route fires 8 times a day; a minute-of-hour table either shows it three times (wrong) or once without saying which hour (ambiguous). The schedule must be a 1440-minute timeline placing *every* firing. The code does this; earlier revisions of the design did not, and it was the single largest structural finding in the review.

### I.4.3 Phase is a route field, not the submit moment

A route carries an explicit **"Send at" or "Deliver at"** scheduled time plus a separate repeat interval. VERIFIED. So `dispatch_minute` maps directly onto the route's Send-at field — the operator does **not** have to press create at the dispatch clock. Deliver-at mode makes arrival distance-independent, which is the natural mode for hub staggering.

**Deliveries (1–3)** is the "merchants run N times" trip count, *not* the merchant count. Confusing "3 deliveries × 1 merchant" with "3 merchants" triple-counts the budget. Current code pins `deliveries: 1` deliberately. Note also that Gold Club's "merchants run three times" and Plus's "merchant run twice" multiply *trips per order*, never the merchant count itself.

**Cold start:** `first_delivery_hours` is a **worst-case upper bound** — a full cycle plus travel, if the route is created just after its send time. It exists only to bound the manual-coverage window, not as a fixed startup cost. An earlier revision wrongly instructed the operator to press create at the dispatch clock; that has been corrected, and a reviewer may still find stale traces of the old framing.

### I.4.4 Hub staggering

Stagger arrivals at each hub by at least N minutes and reserve a clear slot for the manual NPC burst. Collect-then-ship ordering at hubs is currently moot, because netting leaves each village either a sender or a receiver of a given resource — nothing relays, so no outbound waits on an inbound. **That changes the moment crop relay is built**, and the schedule solver will need real ordering constraints then.

## I.5 Execute — what the game does and doesn't tell you

### I.5.1 Route creation returns nothing useful

`POST /api/v1/trade-routes` returns an **empty body.** The game never reveals the id of the route it just created. A run can therefore only say "I asked for a route to village N," never "I created route 627318."

The only way to identify what a run actually added is to **diff a fresh read of the marketplace against exactly what was there before the run started** — which is why the execution trace records the full pre-write inventory per origin. This is a game limitation driving an architectural requirement, and a reviewer who doesn't know it will read that inventory as redundant defensive coding and suggest removing it.

The read-back is **GraphQL**, not a document GET, and marketplace reads pin their own Referer.

### I.5.2 Revert has two halves that differ in kind

**Disable** — reversible, the route can be switched back on. **Delete** — not reversible. Disabling must always run first, so resources stop moving even if the delete then fails.

Reporting the halves separately is essential rather than pedantic: a revert that claims to have undone a run while leaving live routes shipping is far worse than one that plainly names the exact rows a human still has to remove. Both operations are verified against the game's own client code and covered by tests, but kept as separate opt-ins precisely because they differ in reversibility.

### I.5.3 What a run can never learn

A trace records what the app decided and put on the wire, plus — only where a run verified it — what the marketplace read-back showed immediately afterwards. It **never** learns whether a shipment fired on schedule, whether it arrived, or whether the operator changed something by hand in-game afterwards.

"Created" means *written, and where verified, seen to appear.* It never means "N sends fired" or "M deliveries landed in-window." Those are facts about the game that only reading the game would give.

**What to look for:** any report that presents write-history as delivery-history. `run_history.py` is explicit about this scope in its own docstring; check the UI doesn't quietly promote an audit into a delivery report.

### I.5.4 Shortfall behaviour — UNVERIFIED, and the assumption matters

What happens when a row fires and the sender lacks resources, or lacks free merchants? The understood behaviour is that it sends what it can and does **not** later top up the missed slot; the next row fires on its own schedule. Third-party observation notes routes may not fire at the same second, implying tick-based processing where an unsatisfiable row is effectively skipped for that cycle. PARTIAL at best — the mechanism is not documented anywhere official.

**Assume the conservative side: rows skip rather than retry or top up.** A planner that assumes top-up will believe an hourly rate is being delivered when it is not. One deliberately resource-starved test route settles this empirically.

### I.5.5 Also unverified: per-village route cap

Whether Gold Club caps trade routes per village or per account is **not documented.** Given the 24/N fan-out, a cap would bind on short cycles first. Community practice treats it as effectively unbounded, but that is absence of evidence rather than evidence of absence. Blocking the apply layer on this question was the right call.

### I.5.6 Gold Club access

Gold Club membership costs **200 gold** and lasts the whole game round; gold spent on it is non-refundable. VERIFIED (support "Gold Club Overview"). Trade routes require Gold Club plus a Plus account and appear once the account has more than one village. Legacy community sources citing 100 gold are outdated.

## I.6 Storage safety — both directions

### I.6.1 The direction that kills armies

`fill_time = (capacity − stock) / net` returns a **negative** number for a village with negative net crop. Negative reads as "no problem" while the granary empties and troops are about to die. It is the same `l4`/`l5` trap surfacing in a different module — which argues for one shared `crop_status()` helper rather than per-module arithmetic.

Both branches are needed:

```
filling:   fill_time  = (capacity − stock) / net       net > 0
draining:  empty_time = stock / −net                   net < 0
```

and the warning threshold applies to both. A near-zero rate is "no trouble either way," not a division.

`storage.py` returns *which way the village is heading and how long it has* rather than a single "time to trouble" number, and treats a zero-ish rate as safe rather than dividing by it. Correct.

### I.6.2 Averages hide bursts

A route delivering 20,000 every 4 hours averages 5,000/h — which passes a continuous-rate check while the 20,000 spike overflows a warehouse already 70% full. Rates are averages; cargo lands in discrete batches. `simulate_day` replays the actual beat against actual capacity instead of trusting the average.

That replay finds **two different failures** which were long reported in the words of the rarer one: a **burst** overflow (the batch overflows even though the average fits) versus a **sustained** overflow (the average itself doesn't fit). Telling them apart matters because the fixes differ — retime the beat versus resize the cargo.

**Overflow loses resources outright.** Production above capacity is discarded. VERIFIED.

### I.6.3 Starvation order — the gap with no code counterpart

When a granary hits zero, troops die in four groups, **in this order** (VERIFIED, support "Starvation Mechanics"):

1. **Reinforcements from other players**
2. **Your own troops reinforcing this village** (sent from your other villages)
3. **Troops in their home village**
4. **Troops in transit, forwarded, or held in traps**

Within a group, the army with the **most units** starves first; within an army, the **most numerous unit type** dies first; ties break **left-to-right** in the unit list. Dying units **refund their training crop cost** to the granary, briefly feeding the survivors. **Starvation stops only when crop *production* returns to non-negative** — refilling the granary is not enough, which is a subtlety worth noting since it means an emergency shipment buys time without ending the bleed.

**This is unmodelled anywhere in the codebase.** `storage.py` correctly gives direction and time-to-empty, but nothing encodes that a hammer parked as reinforcement in a hub dies **before** that hub's own garrison. On a 26-village account that stations armies across villages, a hub reading "12h to empty" is considerably more dangerous than the number suggests, and the loss lands somewhere other than where the troops nominally live.

**What to look for:** whether any warning distinguishes a village's own garrison from armies reinforcing it. Nothing currently does.

**This is the highest-value unimplemented safety check in the planner.**

### I.6.4 NPC Merchant

**3 gold per use**, 1:1 exchange between resource types, **minimum 50 resources**, requires a Marketplace, no cooldown. VERIFIED (support "NPC Merchant" — a premium gold feature exchanging any resource type for another at a 1:1 ratio; the UI blocks amounts under 50).

It **redistributes within one village, conserves the total, cannot create resources, and cannot exceed warehouse or granary capacity.** Any "fix the deficit with NPC" logic must check both that total available ≥ the target sum *and* that each target ≤ that store's cap. Assuming NPC can conjure crop is dangerous.

NPC is unbuilt; the schedule reserves a slot for a manual burst. The problem it exists to solve is worth stating, because it bounds how good any route plan can be: roughly 93k/h produced against 48k/h absorbed means surplus accumulates no matter how well routes are balanced. Perfect routing does not fix a sink-capacity deficit.

*(Sources citing 4–5 gold are describing Kingdoms. Discard.)*

## I.7 Production — what a rate actually is

### I.7.1 Bonuses are additive on base, not compounding

Oasis annexation applies to the village's **base** production, not its current total — VERIFIED (the increase applies to base production and not the actual one). Grain Mill, Bakery, Sawmill, Brickyard and Iron Foundry behave the same way. So oasis and building bonuses are **additive percentages of base**.

**A tool that multiplies these overstates production**, which plans over-sends. Dangerous direction.

Hero's Mansion L10/15/20 → 1/2/3 annexed oases, each within a 3-tile radius. Oasis types: +25% one resource; +25% one resource and +25% crop, or +50% crop; +50% one resource (grey/Natar area only).

### I.7.2 Transient bonuses and the steady-rate fiction

- **Gold production bonus:** +25% to one resource's total, 5 gold, lasting 7 days (1×) or 3 days (3×), auto-extendable. VERIFIED.
- **Travian Plus:** +25%.
- **Video reward:** the repo records **+15% resource production for 8h** per resource, plus 25% faster construction, keyed by the game's own reward ids (`lumberProductionBonus`, `cropProductionBonus`, …). These are better sourced than any public documentation, which publishes only the *gold* figures. **Treat the codebase as authoritative here** and do not substitute the +25%/7d gold numbers — a research pass that only sees official sources will wrongly mark this unconfirmed and may propose the gold values as a fix.

**The planner is immune to all of this by construction, and that deserves naming in review.** Because net crop is derived by inverting the live granary countdown, an active bonus is already baked into the snapshot. No bonus modelling is needed at all. A planner that instead *computed* production from field levels and bonus flags would drift every time a bonus expired — this design sidesteps an entire class of staleness bug.

**What to look for:** any production figure computed rather than read. **If a bonus must be modelled anywhere, model it as off** — assuming it is on overestimates production.

### I.7.3 Field caps and storage

Capital fields exceed L10 (to L20); non-capital resource fields cap at **L10**. Capital-only buildings: Stonemason's Lodge and, for Teutons, the Brewery. Conversely **Great Barracks and Great Stable cannot be built in the capital** — which is why the hammer village is non-capital and adjacent to the capital, and therefore why the capital produces crop while the hammer consumes it. **That asymmetry is the reason the entire route topology exists**, and it is the single most useful fact for understanding why the planner's shape is what it is.

Warehouse and granary: L0 = 800, L1 = 1,200, L20 = **80,000** each, stacking additively across multiple buildings. VERIFIED. **Great Warehouse and Great Granary are WW-village-only** (3× a normal one) — WW-gated, not artifact-gated.

The code reads capacity live (`int | None`, skipped rather than guessed when not fetched) instead of holding a level table. Better than a table, and consistent with §0.1.

## I.8 Monitoring and re-planning

Routes persist; plans do not. An applied route keeps shipping its configured cargo regardless of what happens to the sender. If a sender's production falls — troops rebuilt, an oasis lost, a village chiefed — the route drains the sender's stock and the tool only *notices*.

Two guards, cheap at design time and expensive to retrofit:

1. Size routes against **sustainable surplus with margin**, not the instantaneous snapshot rate.
2. Make drift beyond X% **trigger a re-plan proposal with a route diff**, not just an alert.

Also: village-count churn is constant on this account (20 → 22 → 25 → 26). A re-plan must be a **diff** against the currently configured route set — Create / Edit / Delete, with unchanged routes collapsed — not a re-entry. And the golden regression fixture must be **frozen** at a snapshot, never read live: a test that reads current state stops being a regression test. The existing Appendix A fixture is 20 villages and has already drifted.

---

