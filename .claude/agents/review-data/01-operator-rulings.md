# Operator rulings and live contradictions — READ THIS BEFORE FILING ANY FINDING

The mechanics reference is authority on **the game**. It is not authority on **what
this account has decided**. Where the two differ, the operator's ruling wins, and
this file is where those rulings live.

A reviewer that files a P1 against correct code because the reference says otherwise
is worse than useless — it burns the operator's attention and invites a "fix" that
breaks working behaviour. Two of the items below are exactly that trap.

Each ruling states **what not to do** as well as what is true.

---

## 1. Merchant base capacity is 2,500 — and the reference's 2,200 is superseded, not reconciled

**Ruling (operator, 2026-09-02): base 2,500, slope +20% per Trade Office level.**
In code at `merchants.py:102`.

The reference (§0.1, §I.3.1) says **2,200**, derived from a live reading of **7,920
per merchant at Trade Office 13** — which fits `2200 × (1 + 0.2 × 13)` exactly.

**Both numbers came off the game, and they cannot both describe one model:**
`2500 × (1 + 0.2 × 13)` predicts **9,000**, not 7,920. Stock Teuton would predict
2,300. So something changed between the two readings and the mechanism is not
established — a Trade artifact is one thing that could do it. `merchants.py:1-23`
records this openly rather than pretending it reconciles.

**Do not:** "correct" the code back to 2,200 to match the reference. Do not file a
finding that the constant is wrong. Do not treat 7,920@TO13 as the current truth.

**Do:** treat the *factorisation* as unpinned — any `base × (1 + 13k) = 7,920` fitted
the old observation, and `2500 / 0.20` fits neither reading cleanly. The seam is what
matters: capacity lives behind one injectable `MerchantModel` with `calibrate()`, and
**nothing else in the planner may hardcode a capacity.** A hardcoded capacity
anywhere else *is* a finding.

**How it gets settled, at zero request cost:** read capacity at a *second* Trade
Office level. The levels are already held in the snapshot, so this needs one
Marketplace send dialog and no extra fetch. Until then, over-estimating is the
dangerous direction — it plans cargo the merchants cannot carry.

## 2. Map span 401 is CORRECT. The requirements spec's 801 is the error.

The requirements spec describes the world as −400…+400, span **801**. The reference
(§I.3.3) says **401**, and proves it: a captured create request targeting (23|88)
resolved to `mapId 45136`, and

```
mapId = (S − y) × span + (x + S) + 1,   S = (span − 1) / 2
span 401, S = 200:  (200 − 88) × 401 + (23 + 200) + 1 = 45,136   ← matches the capture
span 801, S = 400:  (400 − 88) × 801 + (23 + 400) + 1 = 250,336  ← does not
```

The marketplace page then listed exactly that destination for the route, so the
capture is corroborated end to end.

**Do not** change `span` to 801, and do not file a finding that it should be. A wrong
span breaks `map_id_to_coords`, which is how a route read off the marketplace page is
matched back to a plan — that page names destinations by village id and map id and
carries **no coordinates at all**. So reconciliation breaks, and revert depends on
reconciliation. The reference calls this failure "loud and total" and it is right.

*(Minor: the reference states the span-801 result as 250,136; the arithmetic gives
250,336. A 200-off transcription slip that does not touch the conclusion — logged
here only because catching it is the kind of thing this register exists for.)*

**Consequence for the review queue:** an item was queued in the project ledger as
"P7 — map span 401 vs the spec's 801, a latent bug in `map_id_to_coords`". That
framing is backwards. P7 should be **closed as not-a-bug**, with the spec corrected
instead of the code.

## 3. `ABSOLUTE` allocation is a retention target, not a consumption figure

`ship_per_hour = target − own_production`. The village ends up **retaining** the
target rate; the figure already includes its own production.

Operator's own words on village 01: *"It means that it gets absolute of 8,519, if
loses 5,880 meaning 5,880 + 8,519 = ~14k crop land."* So with 01 at −5,880/h net,
≈14,399/h lands: the drain is covered **and** 8,519/h is retained.

**Consequence the operator accepted:** 01's granary genuinely gains 8,519/h, so the
204,456/day overflow finding is **real**. Never silence it.

## 4. Crop consumption is refused by design; consumption is materials-only

`crop_per_hour` in the snapshot is **already net of troop upkeep**. Declaring a crop
consumption figure on top would double-count it. Consumption applies to
lumber/clay/iron only, and `net_per_hour = target − consumption`.

**Do not** file a finding that the crop path is missing a consumption term.

## 5. Current crop targets include a deliberate two-week overhead

| Village | Role | Base crop target | **Current target** | Overhead |
|---|---|---|---|---|
| 03 | troops_off | 9,526 | **12,526** | +3,000 (operator: "+10000, total 12.5k") |
| 01 | full_off | 8,519 | **11,019** | +2,500 (operator: "+8000, total 11k") |

Rationale, in his words: *"I added crop overhead to that, as troop size will keep
grow and the plan won't be run daily but once of 2 weeks."*

**Open and unresolved:** at the fixture's 400,000 granaries the extra 132,000/day
this banks is **shed at the cap, not held** — 03 banks 300,000 crop/day and 01
264,000/day, which is 4.2M and 3.7M over two weeks and no granary holds that. The
operator's real granary capacities have been requested and not yet supplied. Until
they are, do not report the overhead as either sufficient or wasteful.

## 6. The material relay tier is DECLARED, not searched

The wood route is the operator's own: **02 → 18/14 → 11/17/19**. The tool takes it as
a declaration (`relay_for` per village) and does not search for relay shapes on
materials. Crop relays *are* searched — that asymmetry is deliberate.

**Which relay serves which downstream village is a fixture choice, not a solver
output.** Do not file a finding that the assignment is arbitrary.

Rules the tier enforces: a relay may not feed a relay; a role village
(capital / troops_off / full_off / def) may not relay; `feeder` and no-role may.

**The relay buffer bound is one cycle's pass-through, not a night's accumulation** —
`collect_rate × max(collect_cycle, forward_cycle)`, and it is **capacity, not free
space**. A 160,000 warehouse already 94% full holds a 33,488 pass-through. Do not
reason about a relay's buffer as "how much arrives over the window".

## 7. Roles

01 = **full_off** (the hammer). 03 = **troops_off**. Role villages may not relay.

## 8. The netting invariant is PER RESOURCE

A village may legitimately receive clay and send lumber. Village 02 does exactly
that — 13 inbound rows and one outbound. Any finding of the form "this village both
sends and receives, which the waterfall rule forbids" must name the **resource**, or
it is wrong. The limiter on 02 is its **remainder role**, not a no-waterfall rule.

---

# Still open — ask, do not assume

## The reference's own list (each settleable with one controlled in-game test)

| # | Question | What it blocks |
|---|---|---|
| 1 | Trade-route behaviour when resources or merchants are short — skip, partial, or top up? | the correctness of **every** hourly rate |
| 2 | Does Gold Club cap trade routes per village or per account? | the apply layer |
| 3 | Is the filling branch of the net-crop derivation correct? | storage safety on filling villages |
| 4 | Is `2200 / 0.20` the right factorisation? (see §1 — now `2500 / 0.20`) | all route sizing |
| 5 | Tournament Square maximum — +200% or +500%? | troop travel time |
| 6 | Current cancel-refund rule for construction | build-queue economics |

The code's stated default for #1 is **skip**, and that assumption is load-bearing.
A reviewer should check the assumption is *stated where it is used*, not that it is
right — nobody knows yet.

## Open with the operator

- **Granary capacities for 01 and 03** — blocks sizing the crop overhead (§5). No
  fixture in the repo has them.
- **25%/60% vs 30%/80%** for pre-night and morning fill.
- **Whether to adopt non-divisor trade cycles** (which break the readable daily beat)
  or merely report what they would save. Current code reports — see the register.
- **What consumes the YAML export.**

---

## How to use this file when you disagree with it

If the code, the reference and this file form a genuine three-way contradiction that
none of the above resolves, **say so and stop** — do not pick a winner. Name the
three positions, name the one in-game observation that would settle it, and mark the
finding `UNRESOLVED — needs an operator decision`. That is a useful review result.
Guessing is not.
