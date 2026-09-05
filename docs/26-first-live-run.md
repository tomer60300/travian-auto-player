# 26 — The first live run of the distribution planner

Written 2026-09-05, against the execute path as it stands after the wave-1
review (`4b5203b` and later), and corrected after the game-mechanics review of
its first draft. Every control named here is quoted as the page labels it.
Every response field is quoted as `/routes/execute` returns it. The protocol
exists because the review found that the create half of the executor was
honest and the disable half was not, and that both are now honest only along
paths that have been driven with fakes, never against the game. The first live
run is the first time the game answers.

The one rule above all others: **nothing in this document may be skipped
because the previous step looked fine.** Every step exists because a specific
silence was found and closed; the step is how we learn whether it stays closed
against a real marketplace.

## 0. Before anything touches the game

Settle these first. Each one changes what the run will do, and three of them
are measurements the plan's own numbers rest on.

1. **The latency target.** The page no longer sends `max_latency_hours`; the
   backend's standing target of 2.0 h applies, clamped to each profile's
   window, never loosened. Measured on the same night plan: an 8 h target
   planned 46 routes / 120 merchants; a 2 h target planned 48 / 135. Decide
   2 h, window length, or an exposed control **before** step 1, because the
   plan you confirm in step 1 is the plan step 2 writes.
2. **Live writes are refused, not faked, until the switch is on.** With
   `TRAVIAN_TRADE_ROUTE_LIVE` unset, a live execute is refused with a 409 that
   names the flag. Dry runs need no flag. There is no silent no-op path: if a
   live execute returns a response at all, writes were enabled. Set the flag
   only at step 2.
3. **A run cannot be undone without its trace.** A live run whose trace file
   cannot be opened is refused before the first game request (`4b5203b`),
   because the game returns no id on create and the undo reconstructs from
   the trace's pre-write inventory. If the refusal fires, fix the trace
   directory; do not work around it. Dry runs are unaffected.
4. **Save the setup to the server** (v10) and **export it** to a file, then
   load the file back once to prove it parses. The review found three ways a
   saved document could never be loaded again; all are closed, and this is
   the check that they stay closed for your account's actual figures.
5. **Read the plan's blockers to zero.** The plan verdict's `executable` must
   be true and `blockers` empty; the marked cell is named beside the Save and
   Export buttons. A plan with a blocker is not a plan.
6. **Pin the merchant model before the plan is built.** The formula the plan
   uses — 2,500 × (1 + 0.2 × Trade Office level) per merchant — is pinned at
   one end only: the base was re-read on 2026-09-02, the slope was not, and
   over-estimating plans cargo the merchants cannot carry. Open the
   Marketplace send form in a village at **Trade Office 0** (03 and 26 are)
   and read the resources-per-merchant figure it states; that number *is* the
   base capacity. Read it again in a village at least three levels higher;
   the pair fixes the slope, and closer levels are refused by the calibration
   because the game floors the figure and the fit then overstates capacity.
   Type both into the merchant-model fields. Do not build the plan against
   the defaults.
7. **Time one leg on the same form.** With the send form open on the
   destination step 2 will use, note the travel time the game states and
   compare it with `distance ÷ 12 × 60` minutes. Merchant speed has never
   been measured on this server; it drives sets in flight, the merchant
   budget and the night profile's turnaround counts. A disagreement here
   invalidates step 1's merchant column, so it is settled before step 1.
8. **Confirm the snapshot carries capacities.** The fetch reads warehouse and
   granary capacity for every village; a village whose capacity was not read
   is refused by the night derivation, by name. If 01 or 03 is named, fetch
   again before deriving the night; do not type a figure in.

## 1. Dry run of the whole day, read in full

Turn on **Whole day — execute all profiles at once**. **Trim the fan-out to
the profile hours** is forced on and shown disabled; the page and the request
agree. Press **Preview**. Nothing reaches the game.

A dry run carries no `problems` — that field is populated only on live runs.
Its refusal signal is the plan verdict from step 0.5, and the live call's own
422. Read, in this order, and stop on anything unexpected:

- `warnings` — read each. `MERCHANT_MODEL_UNCALIBRATED` on a village with a
  real Trade Office means the level was not typed; fix it. A merchant-boundary
  warning means the two profiles together commit more merchants than the
  fleet around the window edge; acceptable, but know which village. Read
  `unweighed` too: overflow, starvation and `NIGHT_OVERRUN` are critical and
  deliberately do **not** block execution.
- `requests_forecast` — the reads, disables, creates, verifies and trims the
  live run would spend. This is the price. On an ordinary run
  `marketplace_reads` is one per origin that creates; on a sweep it is one per
  village; either way a marketplace read is two page loads. Expect roughly
  one create per route and a handful of batched writes on top. If the
  forecast is far above that, something is planning more than you asked.
  Keep this response: the live run returns `requests_forecast: {}`, so this
  is the only forecast you will have to compare against.
- `actions` — every planned row: origin, destination, cargo, repeat interval.
  Check three at random against what you would do by hand: is the destination
  the consumer or hub you expect, is the interval one of the eight the game
  offers, is the cargo within the capacity you pinned in step 0.6?
- `disables` — in a dry run these are the rows the live run **would** switch
  off. Read every one. If a destination you want fed appears here, either it
  is off-schedule for the profile (the plan moved it) or it is not in the plan
  at all. Protect it with **Never disable** (`x|y`) or fix the plan. Do not
  proceed with a disable you do not understand.
- `created_game_rows` vs `live_game_rows` and **Max rows this run** — the
  reported count is measured after the trim; the budget is an upper bound on
  live rows. Confirm the numbers are consistent with each other.

Paste the whole response somewhere you can compare against later.

## 2. One route, live, on a village you can watch

Pick one origin whose marketplace you will open in the game immediately after
the run. Prefer a village with **no existing trade routes**: **Also disable
routes the plan no longer wants** is ticked by default and the run will switch
off every stale row on the origin it visits. If no such village exists, untick
it for this run, or list every existing destination under **Never disable**
first. Leave **Correct cargo on routes that have drifted** unticked.

Set:

| control | value | why |
|---|---|---|
| **Only origin (village id)** | the one village | one marketplace to read afterwards |
| **Routes this run** | `1` | one create, one verify |
| **Max rows this run** | leave the default (24) | the largest fan-out one create can produce, so one route cannot exceed it |
| **Never disable** | every destination you are not ready to lose | the disable half is the one that was silent |
| **Also disable routes the plan no longer wants** | unticked, unless the village has no routes | nothing is switched off that you have not seen |
| **Whole day** | on | the same plan you confirmed in step 1 |

Set `TRAVIAN_TRADE_ROUTE_LIVE`. Press **Preview** once more and confirm the
response shows `live_enabled: true` with `dry_run: true`. Then execute.

Read the response:

- `created` = 1, `created_unverified` = 0, `not_created` = 0. If
  `created_unverified` is 1, the create's answer was unreadable and the
  read-back could not settle it: go to the game **before** doing anything
  else. If `not_created` is 1, the game accepted the write and made no route;
  that is worse, and a second create on top is how duplicates accumulate.
- `problems` empty. `disables` empty, or exactly what step 1 predicted for
  this origin.
- `trace_id` present. Write it down; it is the handle for **Undo this run**.
- The spend, against the forecast you kept from step 1: expect the live run
  to be higher on reads, because each marketplace read is two page loads and
  every write is confirmed by its own read-back.

**Now open the game.** Marketplace of the origin village, trade-routes tab:

1. The route exists, to the planned destination, with the planned cargo per
   resource and the planned repeat interval.
2. Count the rows **to the planned destination**. The game's count must be at
   most `live_game_rows`; equal is the normal answer. Response higher than the
   game means the trim worked and its confirming read did not — note it, it
   is not a failure. Game higher than the response is a stop.
3. Nothing else on this village changed except the `disables` the response
   listed.
4. Merchants are **not** readable now: the route has not departed, and the
   free/total cell shows only who is idle at this instant. What the plan
   promised is in its own per-village merchant table, committed against the
   fleet minus the reserve. The game-side check belongs in step 5.

Then open the trace by `trace_id` and find the `window_pruned` event. Its
`status` is the first real observation of what a trade-route DELETE answers
with: the delete's response shape was taken from the game's bulk-toggle
handler and has never been seen on this account. `deleted` means the shape
applies; `unverified` means it does not and the code settled the delete purely
by read-back. Either is safe. Record which.

If any game check is false, stop. Undo as §3 describes — Check, then Disable,
then Delete — confirm in the game that the route is gone, re-enable by hand
every row the panel lists under **Routes this run switched, to put back**, and
bring the response and a screenshot to the review.

## 3. Prove the undo once, on purpose

With the single route confirmed correct, undo it anyway. The undo is **three
actions, not one**, and only the first is free of consequence.

1. **Undo this run**, then **Check what undoing this would take**. Read-only:
   it re-reads the origin and returns what the run created and what it
   switched. Nothing has changed in the game yet.
2. **Disable those routes now (reversible)**. The created rows stop shipping;
   they are still there and still re-enableable. Confirm in the game that they
   read as off.
3. **Delete those routes for good (disables first)**. Irreversible. Confirm in
   the game that the rows are gone; the panel names anything the app could not
   remove and you delete that by hand.

**The undo does not re-enable anything.** Rows this run switched off are listed
by the panel under **Routes this run switched, to put back**, and you switch
them back on by hand in the marketplace: select them, edit selected, enable.
The undo is complete only when that list is empty or every row on it is back
on. An empty `disables` in step 2 is what makes the list empty, which is why
step 2 prefers a village with no existing routes.

Budget roughly ten game requests for the three calls: each re-reads the origin
it touches, and each write is confirmed by its own read-back. It is the only
time the undo is exercised against the game with nothing at stake; after this,
the undo is trusted because it was seen to work, not because its tests passed.
The revert takes the same lock the execute does, and its own read-back reports
rows still there as still there rather than deleted.

Re-run step 2 to recreate the route. `disables` must be empty this time
(nothing is stale) and `created` = 1 again with the same destination and
cargo. Convergence in two passes with no duplicate rows is the property every
interrupted shape was driven to with fakes; this is its first live instance.
If the second pass is not a no-op, look for the `problems` line that says the
game made a different number of rows than the cycle predicts before you call
it a duplicate-creation bug: convergence is defined by planned departure
minutes matching what the game made.

## 4. Widen, in four steps, reading every response

Each step is the previous one plus more; each response is read as in step 2
before the next step is taken.

1. **Routes this run** = 3, still filtered to the one origin. Confirm all three
   in the game.
2. Clear **Only origin**, **Routes this run** = 3, and **Also disable routes
   the plan no longer wants** **unticked**. The run now visits villages in
   plan order and creates at most 3, and switches nothing off. Read the
   `problems` lines: they name every destination the plan disagrees with on
   the villages it visited. That is the inventory you decide over before
   anything is disabled.
3. The same run with **Also disable routes the plan no longer wants** ticked.
   Now `disables` lists what it switched off. It is **not** the full inventory
   from 4.2: a diverging destination is disabled only when the same run can
   fund its rebuild in both creates and rows (`add3978`); the rest are left
   running and reported. A `problems` line ending an origin with zero active
   rows is a stop.
4. **Reconcile all villages**, with creates on. The page runs the sweep five
   villages per pass with human pauses; `swept_origins` / `unswept_origins`
   and `next_chunk_wait_seconds` tell you where it is. Never drive the sweep
   from the API without `max_origins_per_run`: the server's own default is
   unbounded, and fifty paced reads run past a client timeout with writes
   already committed. One read per village serves both the reconcile and the
   create. Let it finish; a deferred create gets its own pass at the end.

After the sweep, the run-history panel's `needs_attention` must be false for
every run, and a second sweep must report zero creates and zero disables: the
account has converged on the plan.

## 5. The first night

Do not let the first live run be at 23:00. The Night profile's rows were
created in step 4 alongside the Day profile's, so the night runs itself with
no request from us.

Before sleeping, one deliberate experiment, because it is load-bearing on
every rate the tool reports and has never been observed: pick one low-value
route and size its cargo above what the origin will hold at its send time. The
tool assumes a short sender sends a partial load rather than nothing. In the
morning, read the origin's own movement history: did it send a short load, or
skip? Record the answer; if it skipped, the assumption is wrong at both points
the code states it.

The next morning, read:

- **Stores against the two figures the night asserts.** 60 % is the ceiling
  every store was sized to fill: a store above it shed production. For the
  role villages (01, 03) 60 % is also the **morning floor**: either store below
  it at 07:00 means the night under-delivered. 25 % is the baseline you were
  meant to be at by 23:00, not somewhere to return to: a store above 25 % at
  the switch means the pre-night spend-down did not happen.
- **The crop-negative villages**: is anyone's granary below where the plan
  promised at 07:00?
- **Merchants**, now readable: at a minute when no route of an origin is
  scheduled to depart, its free count should be at least the reserve you set
  (default 2). Fewer means the plan's merchant arithmetic is wrong for that
  village, and step 0.6 or 0.7 is where to look.
- **NPC**: if the plan relied on the exchange (attended or unattended, the
  wood-low or crop > 700,000 trigger), the plan modelled it; **no run
  performs it**. The NPC merchant exchanges inside one village and the
  executor writes only trade routes. If the plan relied on an NPC exchange at
  02, you did it, or it did not happen.

## Stop rules, at any step

| signal | meaning | action |
|---|---|---|
| any `problems` line | a step ended without proof it did what it said | stop; read the trace by `trace_id` |
| a `problems` line saying the game made N rows, not the M the cycle predicts | the 24/N row model every merchant figure rests on does not hold here | stop and re-plan; this invalidates merchants committed, shipments per day and the row budget, not just this route |
| `not_created` > 0 | the game accepted the create and made no route; the plan believes cargo is moving that is not | stop; read the marketplace before any re-create |
| `created_unverified` > 0 | a create's answer was unreadable and the read-back did not settle it | read the marketplace before any other write |
| `stopped_early` | a captcha, the activity budget, a failed read, or a prune that stopped | stop; for a captcha, solve it in a browser first, then retry; for the others, read the trace |
| rows in the game > `live_game_rows` | the trim did not hold | stop; the response names the rows still departing |
| a `disables` entry you did not predict | the plan moved or dropped a destination | undo, protect or re-plan |
| the game shows a row the response does not | reconciliation is wrong about the account | stop; this is a review finding, not an operator error |
| an incoming attack on any origin or destination in the plan | the executor does not consult this and will act identically under threat | stop the protocol; a run that commits merchants and cargo into a village under attack is a decision no player would make |

## What this protocol does not cover

- Gold Club: `docs/15-gold-club-features.md`. A Gold Club account's route
  editor behaves differently and the executor has an early return for it.
- The night profile bounds each village's shed by a fleet that is shared
  across destinations over time; the optimizer partitions merchants per
  route. If the optimizer reports over budget on a night profile that derived
  cleanly, that is the two models disagreeing, not a defect in either. Read
  the optimizer's answer as the binding one.
