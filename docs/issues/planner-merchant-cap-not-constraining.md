# `max_busy_merchants` is reported against, never planned within (known issue #6)

## 1. Summary

A declared `max_busy_merchants` cap does not constrain route selection. The optimizer
builds a plan, and only afterwards does a separate layer notice the village is over its
budget and emit a blocker — so the cap behaves as an assertion about a finished plan
rather than as a constraint on building one. On a real account this produces a plan that
is `feasible: false` / `executable: false`, which `/execute` refuses, with no route the
operator can take from inside the planner: no cycle choice fits the cap, the optimizer is
forbidden from discovering a material relay, and the one declared mechanism that *could*
satisfy the cap (`_relay_tier_flows`) only activates against shortfalls, of which this
account has none. The operator is left to hand-craft a workaround (`ship_only_to` plus a
`relay_for` declaration) to express something they already declared once, as a cap.

## 2. Current behaviour

Planning the night profile on the account below returns:

```
feasible:   false
executable: false
blockers:   ["02 commits 12 merchants but you capped it at 8 busy at once;
              its fleet could otherwise spare 18"]
```

The committed figure is `merchants_committed = merchants_per_send × sets_in_flight`
(`merchants.py:224-226`, `optimizer.py:263-264`) — merchants tied up simultaneously, not
merchants per dispatch. A route that fires hourly with a round trip longer than an hour
has a second load in the air before the first returns, so its committed cost is a
multiple of its per-send cost.

Because the verdict is `executable: false`, `/execute` refuses the plan. The cap is
enforced — just one stage too late to influence the plan it rejects.

## 3. Worked example (real account data)

Village **02**, id `53629`, Trade Office 20 → merchant capacity **12,500**, 20 merchants,
`max_busy_merchants: 8`. Its five night routes, recomputed directly from the plan
response and matching it exactly:

| Route | batch / send | cycle | cargo/h | one-way | round trip | send | sets | **committed** |
|---|---|---|---|---|---|---|---|---|
| 02 → 13 | 24,582 | 1h | 24,582 | 42 min | 84 min | 2 | 2 | **4** |
| 02 → 01 Hammer | 24,902 | 1h | 24,902 | 38 min | 76 min | 2 | 2 | **4** |
| 02 → 03 | 21,874 | 1h | 21,874 | 5 min | 10 min | 2 | 1 | **2** |
| 02 → 19 | 11,172 | 4h | 2,793 | 95 min | 190 min | 1 | 1 | **1** |
| 02 → 11 | 9,520 | 4h | 2,380 | 92 min | 184 min | 1 | 1 | **1** |
| | | | **≈ 86,531/h** | | | | | **12** |

Two routes (13, 01 Hammer) account for 8 of the 12 purely because a ~40-minute one-way
leg cannot complete inside an hourly cycle. Village **18** — the natural relay — sits
**3 fields / 15 minutes** from 02, a 30-minute round trip.

## 4. No cycle choice fixes it

`cheapest_cycle` already returns the minimum-merchant cycle per route
(`merchants.py:290-338`), and its docstring is right that no other cycle is cheaper *for
that route*. Sweeping every cycle in `DAILY_BEAT_CYCLES` confirms the per-route minimum
is already selected:

| Route | 1h | 2h | 3h | 4h | 6h | 8h | 12h | 24h | min |
|---|---|---|---|---|---|---|---|---|---|
| 02 → 13 | **4** | **4** | 6 | 8 | 12 | 16 | 24 | 48 | 4 |
| 02 → 01 Hammer | **4** | **4** | 6 | 8 | 12 | 16 | 24 | 48 | 4 |
| 02 → 03 | **2** | 4 | 6 | 7 | 11 | 14 | 21 | 42 | 2 |
| 02 → 19 | 4 | 2 | 2 | **1** | 2 | 2 | 3 | 6 | 1 |
| 02 → 11 | 4 | 2 | 2 | **1** | 2 | 2 | 3 | 5 | 1 |

Stretching a cycle does not help the expensive routes: a 2h cycle halves the loads in the
air but doubles the batch, which doubles merchants per send. `4 = 2×2 = 4×1`. Beyond 2h
the batch grows faster than the sets shrink and the cost climbs.

**12 is the true minimum for the direct shape.** The defect is not a bad cycle choice —
it is that nothing reconsiders the *shape* when the per-village total exceeds the budget.

## 5. The hand-crafted optimal solution

Pool everything bound for 13, 01 Hammer, 11 and 19 onto one short trunk into 18, and let
18 — 20 merchants, almost idle — run the long legs.

**Trunk**, 02 → 18, capacity 12,500 (02's Trade Office 20), round trip 30 min:

| | cargo/h | cycle | batch | send | sets | committed |
|---|---|---|---|---|---|---|
| 02 → 18 | 54,657 | 1h | 54,657 | 5 | 1 | **5** |
| 02 → 03 (unchanged, direct) | 21,874 | 1h | 21,874 | 2 | 1 | **2** |
| | | | | | | **7 at 02** |

**7 ≤ 8.** The cap is satisfied.

**Forward legs out of 18**, capacity **8,500** (18's Trade Office is 12, not 20):

| Leg | round trip | best cycle | send | sets | committed |
|---|---|---|---|---|---|
| 18 → 13 | 108 min | 1h | 3 | 2 | 6 |
| 18 → 01 Hammer | 104 min | 1h | 3 | 2 | 6 |
| 18 → 11 | 200 min | 2h | 1 | 2 | 2 |
| 18 → 19 | 160 min | 3h | 1 | 1 | 1 |
| | | | | | **15 at 18** |

18's budget is 18 (20 merchants − 2 reserve), so 15 fits.

**Honest caveat, and it should shape the fix.** This shape uses **22 merchants
account-wide (7 + 15) against 12 direct** — it is not globally cheaper, it *relocates*
load off a village that cannot staff its own haul onto one that can. The module says as
much at `optimizer.py:64-65`: relay's "main use is *moving* commitment off a village that
cannot staff its own haul". Any fix that optimizes purely for "fit the cap" will increase
total merchant usage, and that trade-off should be explicit rather than incidental.

## 6. Why the optimizer cannot reach this today

Three separate mechanisms each decline, and together they leave no path.

**(a) The budget comparison is skipped.** `merchants.py:301-309`:

> There is deliberately no budget parameter. Because this already returns
> the minimum-merchant cycle, that cycle is also the most affordable one:
> if it does not fit a village's spare merchants, no cycle does. Filtering
> by a budget could therefore never change the answer. Feasibility is the
> caller's decision -- compare :attr:`RouteCost.merchants_committed`
> against the budget and escalate per the optimizer's ladder. **Known issue
> #6 is that comparison being skipped**, and no signature here can make it
> for you.

The reasoning is sound *per route* and does not generalise: the breach here is the **sum
across five routes at one origin**, which no per-route cycle choice can see.

**(b) A material relay is never discovered.** `optimizer.py:84-86`:

> What it still does *not* do is claim global optimality (the problem is NP-hard,
> section 14) or DISCOVER a material relay -- a material hop has to be declared.
> A village over its merchant budget is reported, never hidden.

**(c) The declared tier only fires against shortfalls.** `optimizer.py:864-869`:

> Build section 5's two legs for whatever the direct pass could not reach.
>
> Returns ``(relay_flows, shortfalls)`` -- the tier's own edges, and the
> shortfall list with everything the tier now covers removed. **Both are empty
> of change when nothing was declared or nothing was short**, which is what
> keeps an undeclared account byte-identical.

This account has `shortfalls: 0` — every downstream is directly reachable — so declaring
18 as a relay changes nothing. Verified empirically: setting `relay_for` on 18 to
`[11, 19]`, and again to `[11, 19, 13, 01 Hammer]`, produced **byte-identical plans** with
`relays: []` both times.

**The waterfall invariant is a legitimate reason for (b) and (c), and this issue does not
dispute it.** `optimizer.py:15-29` requires that no material village both sends and
receives, so that the plan stays acyclic and orderable into one daily beat; the declared
relay is the single sanctioned exception, one hop deep, because "a chain puts one hub's
forward leg behind another's, which no daily beat can order". Requiring a *declaration*
for a structure that breaks an invariant is defensible design.

What is **not** defensible is (a): a cap the operator declared is silently ignored while
the plan is built. That is a separate decision from the relay policy, and it is the core
of this issue.

## 7. Proposed fixes

### (a) Make the budget comparison real — *smallest, highest value*

Compare accumulated `merchants_committed` per origin against `merchant_budget()` during
assignment, and escalate through the existing ladder (`_relief` at `optimizer.py:637-643`
already tries longer cycles against a budget). Where no assignment fits, refuse with a
named reason instead of emitting an over-committed plan.

- **Pro:** honours the declared cap; turns a silent over-commit into an honest refusal;
  no new invariants.
- **Con:** does not by itself *solve* this account — it would refuse rather than find the
  relay. Some accounts that currently get a usable-looking plan would start getting a
  refusal, which is more correct but is a behaviour change.

### (b) Let the relay tier activate on a budget breach, not only a shortfall

Extend `_relay_tier_flows` so a declared relay is also used when the source village is
over `merchant_budget()`, sizing the collecting leg from the over-committed flows rather
than from unmet demand.

- **Pro:** makes an existing, already-declared mechanism reach the case it was designed
  for; the operator's `relay_for` finally does something on a reachable account; no
  search-space growth.
- **Con:** still requires a declaration; needs a rule for *which* flows move to the relay
  (largest committed first is the obvious candidate, but it is a policy choice); must
  preserve one-hop and the no-relay-feeds-a-relay rule; total merchant usage rises (§5).

### (c) Full material relay discovery

Let the improvement search propose material relays the way it already does for crop.

- **Pro:** solves it with no declaration at all, which is what the operator expects from a
  declared cap.
- **Con:** materially expands an already NP-hard search; must preserve the waterfall and
  one-hop ordering invariants that currently come free from the data model
  (`optimizer.py:31-34` notes they are asserted in tests, not defended at runtime), so the
  invariant would have to become an explicit constraint. Highest risk by far.

**Recommendation:** (a) then (b). (a) makes the cap honest; (b) makes it satisfiable
without hand-crafting `ship_only_to`. (c) only if the declaration requirement itself
proves unacceptable.

## 8. Acceptance criteria

1. Planning an account where one origin's routes sum above its `merchant_budget()` never
   returns a plan whose per-origin `merchants_committed` exceeds that budget.
2. Where no assignment fits the budget, the plan is refused with a reason naming the
   village and the budget — not returned with `feasible: false` and an over-committed
   route set.
3. A regression test reproduces the table in §3: five routes from one origin, per-route
   minimum cycles already chosen, summing to 12 against a cap of 8.
4. With fix (b): declaring `relay_for` on a village whose source is over budget produces
   the two legs, and the source's committed count drops to at or below its cap, on an
   account with **zero shortfalls**.
5. Accounts with no cap declared, and accounts already within budget, plan
   byte-identically to before — the same guarantee `_relay_tier_flows` already keeps for
   undeclared accounts.
6. Any fix that relocates load records the account-wide merchant total, so the trade-off
   in §5 is visible in the plan rather than discovered later.

## Appendix — reproduction

- Account: `ts2.x1.europe.travian.com` / ToChe, 27 villages, night profile 23:00–07:00.
- Constants from the request: `merchant_base_capacity: 2500`,
  `trade_office_bonus_per_level: 0.2`, `merchant_reserve: 2`,
  `speed_fields_per_hour: 12`.
- Trade Office levels: 02 → 20 (cap 12,500), 18 → 12 (cap 8,500), 13 → 8, 01 Hammer → 7,
  11 → 13, 19 → 9, 03 → 0.
- `POST /distribution/night-profile` then `POST /distribution/plan` with
  `queues_running: false`, `overnight: true`, `dispatch_window: [1380, 420]`.
