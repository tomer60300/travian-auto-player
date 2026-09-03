# Part VI — Emergent techniques: what players do that the settings don't offer

Everything in Parts I–V is a rule. This part is about **composition** — the techniques that fall out of combining rules, and which no manual describes because the game never advertised them. A reviewer who only knows the rules will read these as bugs or as impossible; a player treats several of them as routine.

**The governing principle, and it recurs everywhere:** Travian exposes *coarse* settings but stores *fine-grained* objects. Wherever the setting is coarser than the underlying data, players get fine control by **over-creating and then subtracting.** Any code that models a game feature as "the setting the UI shows" will underestimate the real decision space.

## VI.1 Cadence synthesis — arbitrary delivery intervals from whole-hour routes

The repeat interval is restricted to {1, 2, 3, 4, 6, 8, 12, 24} hours (§I.4.2). That looks like a hard floor of one hour and a hard set of eight options. It is neither.

Because **"repeat every N hours" fans out to 24/N independently deletable rows** (§I.4.1), and because **send-at has minute granularity**, the row grid's *resolution* is set by the phases of the routes you create, while the actual *cadence* is set by which rows you keep.

**Worked example — a 90-minute cadence, which is not an offered interval.** Create two 1-hour routes to the same destination, one with send-at `:00` and one with send-at `:30`. That produces a 48-row grid at 30-minute resolution. Then delete every row that isn't a multiple of 90 minutes past midnight, leaving 16:

```
00:00  01:30  03:00  04:30  06:00  07:30  09:00  10:30
12:00  13:30  15:00  16:30  18:00  19:30  21:00  22:30
```

Verified: every gap is exactly 90 minutes, and the wrap from 22:30 back to 00:00 is also 90, so the pattern closes the day cleanly. 32 of 48 rows deleted.

**The general rule.** Any cadence `C` that **divides 1440** is achievable. The number of phased 1-hour routes needed is `60 / gcd(C, 60)`; rows created is `24 ×` that; rows kept is `1440 / C`.

| Cadence | Fires/day | 1h routes needed | Rows created | Kept | Deleted |
|---|---|---|---|---|---|
| 10 min | 144 | 6 | 144 | 144 | **0** |
| 15 min | 96 | 4 | 96 | 96 | **0** |
| 20 min | 72 | 3 | 72 | 72 | **0** |
| 30 min | 48 | 2 | 48 | 48 | **0** |
| 45 min | 32 | 4 | 96 | 32 | 64 |
| 60 min | 24 | 1 | 24 | 24 | 0 *(native)* |
| 90 min | 16 | 2 | 48 | 16 | 32 |
| 120 min | 12 | 1 | 24 | 12 | 12 *(native)* |
| 180 min | 8 | 1 | 24 | 8 | 16 *(native)* |

Note the top rows: **sub-hour cadences need no deletions at all.** Six 1-hour routes phased at `:00, :10, :20, :30, :40, :50` deliver every 10 minutes. The "one hour minimum" is an artifact of thinking one route equals one cadence.

**What this is actually for — and it is not merchant savings.** Run the extended sweep on the V10→V02 example (9,323/h, rt 532 min, cap 5,720) and the cheapest options are cadence 36 min and cadence 180 min, both at pool 15 — but 36 min costs five routes and 40 rows against one route and eight rows. The native 3-hour cycle wins on both axes. Extended cadences rarely beat the best native cycle on merchants.

Their real value is **batch-size control**:

| Cadence | Batch per delivery | Merchant pool |
|---|---|---|
| 30 min | 4,662 | 18 |
| 60 min | 9,323 | 18 |
| 180 min | 27,969 | 15 |

Same hourly rate, wildly different arrival lumps. For a destination sitting near its warehouse cap, 27,969 landing at once overflows while 4,662 fits — and **overflow destroys resources outright** (§I.6.2). So the trick buys three merchants' worth of insurance against issue #12. It also lets a cadence be chosen to fit a window evenly, which matters for night profiles.

**What to look for:** the optimizer's decision variable is `cycle_h ∈ {1,2,3,4,6,8,12,24}` — eight options. The **real** decision space is `cadence ∈ divisors(1440)` — 36 options, reachable at a cost in routes and rows. The current model cannot express any of them, so it cannot trade merchants for batch size when a village is storage-constrained. It will instead declare a route storage-unsafe and escalate, when the answer was to split the same rate across a finer grid. Non-monotonicity is also far wilder over the larger space, so no directional heuristic survives there either.

## VI.2 Windowing by subtraction

Already in the codebase (`window_pruning.py`) but worth naming as a *technique* rather than a mechanic: **Travian offers no way to confine a route to part of the day.** There is no "only ship 22:00–06:00" setting.

Players get it by creating the route and deleting the rows that depart outside the window. What survives fires only inside those hours. The row footprint also drops to the fraction of the day covered, which matters if a per-village cap exists (§I.5.5).

This is why the night profile is enforceable at all. Without the fan-out it would be a fiction the plan believed and the game ignored.

## VI.3 Deliver-at as a free scheduler

Hub staggering (§I.4.4) looks like it needs distance math per sender: to have six feeders arrive 10 minutes apart you'd compute each one's travel time and back-solve its departure.

**You don't.** A route can be set by **Deliver-at** instead of Send-at, and the game solves the departure itself. Tell all six feeders to arrive at `:05, :15, :25, :35, :45, :55` and the staggering is done — distance-independent, and self-correcting if a Trade Office upgrade changes nothing but travel is re-derived.

**What to look for:** does the planner emit Send-at when Deliver-at would be simpler and more robust? Emitting Send-at means the tool owns the travel-time arithmetic, and any error in speed (§I.3.2, unmeasured on this server) shifts every arrival. Deliver-at pushes that arithmetic to the server, which cannot be wrong about it.

## VI.4 Per-row cargo asymmetry — a time-varying rate

The mental model "one route = one destination, one cargo, one interval" is what the create form suggests. But **rows are independently editable** (§I.4.1), which means the rows of a single logical route need not carry the same cargo.

So a player can make the 06:00 delivery large and the 18:00 delivery small, from what the UI presents as one route. That is a **time-varying hourly rate** — impossible under the one-route-one-cargo model, routine under the row model.

Combined with §VI.1 and §VI.2, the actual expressive power is: *an arbitrary 24-hour delivery schedule, with arbitrary per-delivery amounts, at any cadence dividing 1440.* That is a vastly larger space than "pick one of eight intervals and one cargo."

**Why it matters here:** the account's day and night profiles are genuinely different problems — nothing is spent while the operator sleeps, so accumulation ceilings replace consumption rates. Currently that's handled by separate profiles and separate optimizer runs. Per-row cargo means one route set could carry both, with the same rows shipping different amounts by hour. Whether that's worth the complexity is a design call, but a reviewer should know it's possible rather than assuming the game forbids it.

## VI.5 Deliveries as a row-cap dodge

`deliveries` (1–3) multiplies **trips per order** without creating more rows (§I.4.3). If a per-village route cap turns out to exist, three deliveries on one row moves three times the cargo for one row's footprint.

The code pins `deliveries: 1` deliberately, which is the right conservative default while the cap question is open — but it's the lever to reach for if the cap turns out to bind.

## VI.6 NPC as a production transform — the clay-to-wood trade

This is the most directly applicable technique to this account's actual state, and it reframes a "problem" as a solved one.

The account's constraint profile is: **wood near zero surplus, clay in large surplus, iron moderate, crop comfortable.** The planner treats these as four independent commodities to be routed. Players don't. **The NPC merchant converts any resource to any other at 1:1 for 3 gold** (§I.6.4).

So the clay surplus is not idle capital. It is **wood, priced at 3 gold per conversion**, and the exchange rate is 1:1 regardless of relative scarcity — which is enormously favourable when one resource is your binding constraint and another is overflowing. A player facing this profile doesn't agonise over routing clay more cleverly; they NPC it into wood at the village that needs wood, and move on.

Three constraints on the trick, all of which a tool must respect:
- **Total is conserved and capacity binds.** You cannot NPC into a store that's already full, and you cannot create resources.
- **Minimum 50 per exchange**, requires a Marketplace.
- **Not available in WW villages** — which is precisely why NPC-exchange villages exist next to a WW (§V.2). The conversion has to happen one hop before the destination.

**What to look for:** the optimizer minimises merchants subject to fixed per-resource demands. It has no notion that **the resource mix itself is a decision variable.** A plan that routes clay 90 fields to satisfy a clay target, while a wood target goes unmet nearby, is optimal under the model and wrong in the game — the clay should have been converted where it sat. This is arguably a bigger gap than the missing per-resource margins (§I.3.6), because it doesn't just re-weight the objective, it adds a transform the model doesn't have.

## VI.7 Crop tactics under pressure

The starvation rules (§I.6.3) have exploitable structure that players use routinely and that no tool currently models.

**Send troops out before the granary empties.** Death order is: foreign reinforcements → your own reinforcing troops → home garrison → **troops in transit, last.** So dispatching your army on a long raid immediately before a crop crisis moves it from group 2 or 3 to group 4. The raid also earns loot and the troops return to a village that has had time to recover. A player under crop pressure empties the barracks *outward*.

**Park armies at home, not as reinforcement.** The same ordering means a hammer stationed as reinforcement in a hub dies before that hub's own garrison. If crop is tight anywhere in the cluster, armies belong in their home village.

**The refund is a trap for monitoring.** Dying units refund their training crop cost, so a starving granary briefly refills and the countdown appears to recover. A tool watching the countdown will see improvement mid-catastrophe. And starvation only stops when **production** turns non-negative — an emergency shipment buys hours without ending the bleed.

**Diet Control changes everything at once.** A 25–50% cut in troop upkeep (§V.1) shifts every crop figure in the account the moment it activates or lapses.

## VI.8 Farming techniques

- **Overlapping raids on one target.** No cooldown and no per-target cap exist (§II.10) — only the 20,000 global cap and round-trip physics. So players time sends so a new party arrives as the previous one returns, keeping a productive farm continuously drained. The "1×/2×/3×" language in community guides describes *how many raids you keep simultaneously in the air against one farm*; it is a scheduling idea, not a game feature.
- **Size to the refill, not to the stock.** A target that regenerates is better hit twice with half the troops than once with all of them, because the second wave collects what regenerated during the first round trip. This is what the bag percentage is *for* — and why misreading it as "fraction of target taken" (§II.6) breaks the loop.
- **Sacrificial wave against Gaul Trappers.** Traps capture small parties. Send a throwaway wave to fill the traps, then the real force behind it. Below roughly 100 clubs, don't engage a Gaul at all.
- **Scout-only entries as cheap recon.** Teuton scouts are infantry, 1 crop upkeep, and an entry containing only scouts auto-converts to a spy mission (§II.3). Standing scout entries keep target intel fresh at almost no upkeep.
- **Hero on exactly one entry.** The 20% cranny dip requires the hero to travel (§II.5), and the hero is account-wide singular. Players send the hero manually with the single most valuable raid rather than trying to get the dip from a list.
- **Never let troops idle.** The core raider discipline: every minute troops sit at home is income permanently forgone, because production elsewhere didn't pause waiting for them.

## VI.9 Construction and bookkeeping techniques

- **Master Builder to bridge the sleep gap.** 1 gold per use, charged when the build starts, and it queues builds *without* having the resources (§IV.1). So a player queues the overnight sequence before bed and production fills it while they sleep, instead of the queue idling at 03:00 having spent everything at 23:00.
- **Keep Town Hall at level 1.** Upgrading changes neither the cost of a celebration nor the CP granted — only the cooldown (§V.4). Resources spent upgrading it early are better spent on the celebrations themselves.
- **Demolish the Smithy after upgrading.** Troops keep the bonuses they were trained with; the building's only ongoing value is training new troops at that level. Demolishing frees a building slot.
- **Capital designation is irreversible in effect.** Moving the capital destroys fields above L10 in the old capital and removes the Stonemason and Brewery. Players plan the cropper capital once and never revisit it.

## VI.10 What this part means for review

Three transferable lessons:

1. **Model the stored object, not the UI setting.** Rows, not routes. The gap between them is where most of this part lives.
2. **The decision space is larger than the game's forms suggest** — cadence beyond the eight intervals, cargo varying per row, resource mix as a variable via NPC, arrival time as a directly settable field.
3. **Where the tool's model is narrower than the game, it will escalate or declare infeasible in situations a player would simply solve.** That is the failure mode to hunt for: not a wrong number, but a correct refusal to a question the player would have answered differently.

---

