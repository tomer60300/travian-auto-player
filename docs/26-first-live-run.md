# 26 — The first live run of the distribution planner

Written 2026-09-05, against the execute path as it stands after the wave-1
review (`4b5203b` and later), corrected after the game-mechanics review of its
first draft, and re-checked the same day against the two further rounds of
backend fixes and the four rounds of page fixes that landed after it — the
behaviours those changed are recorded in `docs/25-resource-distribution-planner.md`
§4.20 and §4.21, and this protocol is what first observes them against a real
marketplace. Every control named here is quoted as the page labels it.
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
2. **Live writes are already on. Turn them OFF for steps 0 and 1.**
   `TRAVIAN_TRADE_ROUTE_LIVE` defaults to **true** (`config.py`, changed
   2026-08-27 at the operator's instruction because the opt-in kept reverting
   to preview-only on every server restart). Unset, the switch is *on*:
   the red button at the foot of "Write it to the game" — labelled
   **Disable old routes & create N (~M requests)**, or **Create N route(s),
   disable nothing (~M requests)** with the disable box unticked — writes to
   the real account at any point, and the page seeds **Routes this run** to 3,
   **Max rows this run** to 24 and **Also disable routes the plan no longer
   wants** ticked. The page asks for confirmation before a live execute, but
   a dialog is not a safety. Set `TRAVIAN_TRADE_ROUTE_LIVE=false` in `.env` —
   not in the shell, because a running process's environment cannot be
   changed from outside — and reconnect the session before step 0, so steps
   0 and 1 cannot write even by mis-click; set it back to true at step 2.
   Settings are rebuilt per session, so a reconnect is enough: no server
   restart, and never restart :80 without asking. Reconnect is refused with a
   409 while any operation is running, so stop the farm and queue loops
   first; the refusal names them.

   The response tells you which state you are in: every response carries
   `live_enabled`, and a dry run reports it truthfully. Read it before you
   trust anything else. Dry runs never depend on it — `dry_run: true`
   returns before the session is touched — and a live execute with the flag
   false is refused with a 409 naming it. The three comments that used to say
   the flag defaults off — the module docstring and the constructor comment in
   `trade_route_service.py`, and the construction site in `web/sessions.py` —
   now say it defaults **on** and give the date; only the words were wrong, and
   only the words changed. The constructor default `live_enabled: bool = False`
   in `TradeRouteService.__init__` is **not** one of them and is **not** stale:
   it is the library's own safe default, which every test and every direct
   construction depends on, and `web/sessions.py` is the only caller that
   overrides it, with the settings value. Do not "correct" it to match
   `config.py`.
3. **A run cannot be undone without its trace.** A live run whose trace file
   cannot be opened is refused before the first game request (`4b5203b`),
   because the game returns no id on create and the undo reconstructs from
   the trace's pre-write inventory. If the refusal fires, fix the trace
   directory; do not work around it. Dry runs are unaffected.
4. **Save the setup to the server** (v11 — v10 added the window prune, v11
   the merchant-model acknowledgement of step 0.6) and **export it** to a
   file, then load the file back once to prove it parses. The review found three ways a
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
   base capacity. Read it again in any village with a Trade Office; with a
   level-0 sample in hand there is no minimum separation, because the base is
   read directly and nothing has to be inverted. (The three-level minimum
   exists only when no level-0 reading is available and the slope has to be
   solved from two levelled villages; the game floors the figure and a close
   pair then overstates capacity.) Slope = (second reading ÷ base − 1) ÷ that
   village's Trade Office level. Type base and slope into the merchant-model
   fields, not the two readings. Do not build the plan against the defaults.

   Then **record that you did it**: tick **I read the base capacity and the
   bonus off the Marketplace send form**, the one control in the World &
   merchants row that is not a figure. It rides in the plan request as
   `merchant_capacity_measured` and in the saved document as the field v11 added,
   and it exists because `MERCHANT_MODEL_UNCALIBRATED` is an equality test
   against the shipped 0.20 — so an operator who measured the slope, found the
   default right and typed it back got the same warning for ever, asking them
   to do the thing they had just done. Ticking it **changes no number**: it
   silences that one finding and nothing else. Editing either figure afterwards
   unticks it, because the reading no longer describes what is in the box — so
   tick it last, after both figures are typed.
7. **Time one leg.** Merchant speed has never been measured on this server;
   it drives sets in flight, the merchant budget and the night profile's
   turnaround counts, and a wrong speed invalidates step 1's merchant column.
   If the send form states a duration for the destination step 2 will use,
   compare it with `distance ÷ 12 × 60` minutes and you are done. If it does
   not, do not block step 1 on this: time the first real firing in step 5
   instead, departure minute from the route row, arrival from the
   destination's incoming transports. Either way, record the answer. This
   check cannot validate the map wrap: every village on this account sits
   well inside the half-span, so the wrap never engages on a real pair.
8. **Confirm the snapshot carries capacities.** The fetch reads warehouse and
   granary capacity for every village; a village whose capacity was not read
   is refused by the night derivation, by name. If 01 or 03 is named, fetch
   again before deriving the night; do not type a figure in.

## 1. Dry run of the whole day, read in full

Turn on **Whole day — execute all profiles at once**. **Trim the fan-out to
the profile hours** is forced on and shown disabled; the page and the request
agree. Press **Preview (0 requests)**. Nothing reaches the game.

A dry run carries no `problems` — that field is populated only on live runs.
Its refusal signal is the plan verdict from step 0.5 (`executable` true,
`blockers` empty) and that same verdict's `unweighed`, where overflow,
starvation and `NIGHT_OVERRUN` appear: all three are critical and all three
deliberately do **not** block execution. The live call's own 422 is the last
gate. Then read the execute response, in this order, and stop on anything
unexpected:

- `warnings` — read each. `MERCHANT_MODEL_UNCALIBRATED` does **not** mean a
  Trade Office level is missing; it fires *because* levels were typed, on any
  plan still carrying the default +20 % slope. The fix is step 0.6 and only
  step 0.6. The check compares against the default value, so a reading that
  *confirms* +20 % is indistinguishable from never having looked — which is
  what the acknowledgement in step 0.6 is for: with it ticked the request
  carries `merchant_capacity_measured: true` and this one finding is suppressed,
  and nothing else about the plan moves. If the warning is still here, either
  the box is unticked or an edit to base or bonus unticked it. Record the two
  readings in your run notes either way. A merchant-boundary warning means the
  two profiles together commit more merchants than the fleet around the window
  edge; acceptable, but know which village.
- `requests_forecast` — the reads, disables, creates, verifies and trims the
  live run would spend. This is the price. On an ordinary run
  `marketplace_reads` is one per origin that creates; on a sweep it is one per
  village; either way a marketplace read is two page loads. Expect roughly
  one create per route and a handful of batched writes on top. If the
  forecast is far above that, something is planning more than you asked. One
  exception: a sweep preview forecasts every village in the snapshot, and
  the page then runs the sweep five villages at a time, so expect the
  preview's read count to be about five times any one pass. Compare per
  pass, not against the preview total.
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
- `created_game_rows` and `live_game_rows` — the first is the rows the
  creates would make, counted before the trim; the second is what would
  survive it. On a dry run both are forecasts over the first **Routes this
  run** routes and **neither is capped by Max rows this run**, so a forecast
  above the budget is expected and means the live run will defer, not that
  anything is wrong. The budget binds only live.

Paste the whole response somewhere you can compare against later.

## 2. The create-only canary: one route, live, on a village you can watch

The first write is one create and nothing else. Not because a larger run would
probably be fine, but because every other thing this endpoint does — switching
rows off, deleting them, rewriting their cargo — fails in a different way, and
a run that does two of them at once cannot tell you which one went wrong.

That shape is a control now, not a checklist. Send `canary: true` and the
server refuses the request unless **every** one of these holds, naming the one
that failed:

| condition | value | why |
|---|---|---|
| `execution_mode` | `"live"` | a preview writes nothing, so there is nothing for the flag to make safe |
| **Only origin (village id)** | exactly one | one marketplace to read afterwards |
| **Only destination (village id)** | exactly one | the run's whole footprint is one destination's rows |
| **Routes this run** | `1` | one create, one verify |
| **Also disable routes the plan no longer wants** | unticked | nothing is switched off that you have not seen |
| **Whole day** off, so **Trim the fan-out to the profile hours** is off | unticked | the trim DELETEs; a first live run must have no delete path at all |
| **Correct cargo on routes that have drifted** | unticked | it writes to rows this run did not make |
| **Max rows this run** | the route's own fan-out: 24 ÷ its cycle hours | "1 route" is not "1 row" |

The last one is checked against the plan rather than against the request: the
server computes 24/N for the single route the filters selected, refuses any
other figure, and refuses the run outright if the filters select no route or
more than one. `max_routes_per_run: 1` never meant one row — Travian turns one
create into 24/N daily rows — and that arithmetic is the footprint you would
have to delete by hand if this goes wrong. A 1 h cycle is 24 rows from one
request.

Note what the flag costs: **Whole day** must be off, so the route this writes
is planned for one profile's hours rather than the union step 1 confirmed.
Preview that single profile on its own first and check the one route the
filters leave against step 1's `actions` for the same pair. They should agree
on destination and cargo; the repeat interval may differ, because a profile
planned alone is planned against its own window.

The page has no canary tick yet — the field rides in the `/routes/execute`
request — so set the controls above by hand and send `canary: true` with them.
The refusals are how you find out you missed one, and they arrive before the
first game request: nothing is read and nothing is written by a refused canary.

Pick the origin as a village whose marketplace you will open in the game
immediately after the run, and prefer one with **no existing trade routes** —
not because the run would touch them (it cannot; the disable box is unticked
and the flag enforces it) but because it makes the after-check unambiguous.

Set `TRAVIAN_TRADE_ROUTE_LIVE=true` in `.env` (or unset it; true is the
default), stop any running loops, and reconnect the session. Press
**Preview (0 requests)** once more and confirm the response
shows `live_enabled: true` with `dry_run: true`. That pair is the only proof
you have of which mode you are in. Then press the red run button beneath it,
which on a canary reads **Create 1 route, disable nothing (~N requests)**
because the disable box is unticked, and confirm the dialog, whose own button
reads **Go live (~N requests)**.

Read the response:

- `created` = 1, `created_unverified` = 0, `not_created` = 0. If
  `created_unverified` is 1, the create's answer was unreadable and the
  read-back could not settle it: go to the game **before** doing anything
  else. If `not_created` is 1, the game accepted the write and made no route;
  that is worse, and a second create on top is how duplicates accumulate.
- `problems` empty, and `disables` empty — not "as predicted": a canary
  disables nothing, so a single line here means the flag did not do its job
  and is itself a stop.
- `trace_id` present. Write it down; it is the handle for **Undo this run**.
- The spend, against the forecast you kept from step 1: expect the live run
  to be higher on reads, because each marketplace read is two page loads and
  every write is confirmed by its own read-back.

**Now open the game.** Marketplace of the origin village, trade-routes tab:

1. The route exists, to the planned destination, with the planned cargo per
   resource and the planned repeat interval.
2. Count the rows **to the planned destination**. On the canary this is an
   equality and nothing else: nothing was trimmed, so `created_game_rows`,
   `live_game_rows`, **Max rows this run** and the game's own count are all
   the same number — 24 ÷ the cycle. Any disagreement is a stop, in either
   direction. (From step 4 on, once the trim is in play, the rule relaxes to
   "the game's count is at most `live_game_rows`": a response higher than the
   game means the trim worked and its confirming read did not, which is worth
   a note rather than a stop.)
3. Nothing else on this village changed at all — no row switched off, none
   deleted, no cargo rewritten. That is the whole claim a create-only run
   makes, and this is where it is checked.
4. Merchants are **not** readable now: the route has not departed, and the
   free/total cell shows only who is idle at this instant. What the plan
   promised is in its own per-village merchant table, committed against the
   fleet minus the reserve. The game-side check belongs in step 5.

Then open the trace by `trace_id`. `run_start` records `canary: true` beside
`execution_mode_requested`, `execution_mode_resolved` and `env_brake_open`, so
the record answers "was this run asked for, and was the server allowed to
honour it" without reference to anything outside the file. `origin_read`
carries the pre-write inventory in full — every row's id, destination, cargo,
departure minute, enabled and visible flags — and the `verified` event carries
the page as it stood after the write in the same shape. Those two are the
before and after; keep the trace.

This first route is also the **canary for the game's read-after-write
consistency**: every run now reads a marketplace a second time before it
deletes anything, and `read_back_disagreed` events in the trace are the
measurement — none across the widening steps means the page settles before the
read-back, and even one means it does not and the trim is deferring itself.
One such event now also stops the run before it reaches the next village, and
says so in `problems`: once a marketplace has stopped agreeing with itself,
every later verdict this run would reach rests on a page that would not hold
still.

**Restoration gets its own canary, later.** A destination whose rows diverge
from the plan is emptied and rebuilt in two writes, and when the second is
refused the run switches the old rows back on itself. That is a write over
rows that already existed and that this run did not create, which is a
different reversibility from a create's: getting it wrong leaves a destination
running a schedule nobody chose rather than one route too many. So rehearse it
alone, after step 3 and before step 4.3, on a destination you are willing to
lose for an hour: `canary` **off** (the flag forbids disabling, which is the
mechanism under test), one **Only origin** and one **Only destination**, that
destination chosen because step 4.2's `problems` named it as diverging,
**Routes this run** `1`, **Also disable routes the plan no longer wants**
ticked, **Trim the fan-out** still off, and **Max rows this run** set to the
rebuild's own fan-out. Read `re_enables` for the "restored N disabled row(s)"
line, then the trace's `restore_attempted` and `restored` events:
`already_enabled_ids` are rows that were already back on and that the request
therefore changed nothing about, `enabled_by_request_ids` are the ones this run
switched, and `restoration_completed` is whether the confirming read found
every old row on afterwards. The counts in the line are the second set only.

**Deletion gets its own canary, later still.** The window trim is the one thing
here that removes a row for good, and no delete has ever been seen against this
account. Rehearse it alone, after the restoration canary: `canary` **off**,
one **Only origin**, one **Only destination**, **Routes this run** `1`, **Also
disable routes the plan no longer wants** unticked, **Whole day** on (which
forces **Trim the fan-out to the profile hours**) with a profile whose window
excludes part of that route's fan-out — otherwise there is nothing to delete —
and **Max rows this run** set to the count that SURVIVES the trim, not the
whole fan-out. Then open the trace and find the `window_pruned` event. Its
`status` is the first real observation of what a trade-route DELETE answers
with: the delete's response shape was taken from the game's bulk-toggle
handler and has never been seen here. `deleted` means the shape applies;
`unverified` means it does not and the code settled the delete purely by
read-back. Either is safe. Record which. The event exists only if rows were
actually due for removal, so its absence means nothing needed trimming rather
than that the trim failed — which is exactly why the window has to be chosen
to exclude some of the fan-out before this proves anything.

If any game check is false, stop. Undo as §3 describes — Check, then Disable,
then Delete — confirm in the game that the route is gone, put back by hand
every row the panel lists under **Routes this run switched, to put back** to
the state its arrow names, and bring the response and a screenshot to the
review.

The by-hand put-back is now for what the run could **not** reverse itself. A
destination the run emptied to rebuild, whose every replacement create came to
nothing, is switched back on by the run: one re-enable of exactly the row ids
the write-ahead record named, verified by a read, reported under `re_enables`
as "restored N disabled row(s)". What stops it is evidence that the
replacement may exist after all — a part-written rebuild, an `indeterminate`
create, an unstable read-back, or a destination whose rows no longer match the
record. Those are left off and named in `problems` instead, because switching
old rows back on beside a new route that may exist would ship two schedules at
once. A row a dual switched back on in the meantime is **not** one of those: the
enabled flag is the field the restore exists to change, and the bulk PUT states
each row's target state rather than flipping it, so the already-on row is a
no-op in the same request. The line then reads "restored N disabled row(s); M
were already back on", and the trace's `already_enabled_ids` says which.

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

**The undo does not change any enabled flag on a pre-existing row.** Every row
whose flag this run moved is listed by the panel under **Routes this run
switched, to put back**, and each line names the state to put it *back to*:
`route 627318 -> enabled` means switch it on, `route 627319 -> disabled` means
switch it off. Both happen — the run disables stale rows, and it re-enables
dormant rows the plan still wants instead of creating them — so read the
arrow, do not assume the direction. Do it by hand in the marketplace: select
the rows, edit selected, set enabled or not. The undo is complete only when a
fresh **Check** returns that list empty.

As written, step 2 unticks the disable option and prefers a village with no
routes, so this list will come back empty and the one part of the undo that
has no code path is the one part never rehearsed. Before undoing, disable one
disposable row on the origin by hand, so the list has one line and the
put-back is exercised once with nothing at stake. Note what the rehearsal
also demonstrates: the panel attributes your manual disable to the run — the
line reads "the run changed it", and it did not. The diff compares the
pre-run inventory against now and cannot tell your edit from the app's. On
an account with a dual, read that list as "rows whose state differs from
the pre-run inventory", not as a record of what the run did.

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

1. **Routes this run** = 3, still filtered to the one origin. Drop `canary`
   here: the flag permits exactly one route and will refuse this run by name.
   Confirm all three in the game.
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
   rows is a stop. Where a funded rebuild is refused outright, the run puts
   the destination's old rows back itself and says so in `re_enables` — read
   that line as "this destination is where it started", not as a new route.
   Where it could not (part-written, indeterminate, or the destination changed
   underneath), `problems` names it and the destination really is dark until
   you act.
4. **Reconcile all villages**, with creates on. The page runs the sweep five
   villages per pass with human pauses; `swept_origins` / `unswept_origins`
   and `next_chunk_wait_seconds` tell you where it is. Never drive the sweep
   from the API without `max_origins_per_run`: the server's own default is
   unbounded, and fifty paced reads run past a client timeout with writes
   already committed. One marketplace read (two page loads) per village
   serves both the reconcile and the create. Let it finish; a deferred create
   gets its own pass at the end. If a sweep needs undoing, narrow it: the
   revert's `only_origins` takes village ids, and without it the Check
   re-reads every origin the run touched at two page loads each — about
   fifty reads for this account before any write. Undo the villages named in
   `problems` first, one at a time, in the Check → Disable → Delete order §3
   rehearsed.

After the sweep, the run-history panel's `needs_attention` must be false for
every run, and a second sweep must report zero creates and zero disables: the
account has converged on the plan.

## 5. The first night

Do not let the first live run be at 23:00. The Night profile's rows were
created in step 4 alongside the Day profile's, so the night runs itself with
no request from us.

Before sleeping, one deliberate experiment, because it is load-bearing on
every rate the tool reports and has never been observed: what a route does
when its origin holds less than the batch. The tool assumes a short sender
sends a partial load rather than nothing. Pick one low-value route and, in
the twenty minutes before its scheduled departure, spend the origin's stock
by hand down below the batch the plan sized — into a building or a troop
queue, not into a merchant send, because a send ties up merchants and would
confound "short of resources" with "short of merchants", the two halves of
the same unanswered question. Then watch: at the send minute
the marketplace's merchants-in-transit list shows what actually left, and the
destination's stock at the arrival minute shows what landed. This is the only
window — a transfer between your own villages generates no report and nothing
records it afterwards. A short load confirms the assumption; nothing leaving
refutes it, and the code states it at two points that would both need
changing.

The next morning, read:

- **Stores against the two figures the night asserts.** 60 % is the ceiling
  every store was sized to fill: a store above it shed production. For the
  role villages (01, 03) 60 % is also the **morning floor**: either store below
  it at 07:00 means the night under-delivered. 25 % is the baseline you were
  meant to be at by 23:00, not somewhere to return to: a store above 25 % at
  the switch means the pre-night spend-down did not happen.
- **The crop-negative villages**: is anyone's granary below where the plan
  promised at 07:00?
- **Merchants**, now meaningful. A merchant is busy for the whole round trip,
  so a village with short cycles has no quiet minute; do not wait for one.
  Sample the free/total cell three or four times across the morning: `free`
  should never fall below the total minus the plan's committed count for that
  village, and never below the reserve you set (default 2). One reading below
  the reserve is the signal: the plan's peak commitment is understated, and
  step 0.6 or 0.7 is where the ruler is wrong.
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
| an action with status `indeterminate` | the run could not tell whether the route exists: the create's answer died and the page would not read the same way twice, or a shared destination's rows could not be attributed | read that destination in the marketplace; the next run reconciles it either way, and nothing there was trimmed or restored |
| `stopped_early` | a captcha, the activity budget, a failed read, a prune that stopped, or a marketplace that stopped agreeing with itself | stop; for a captcha, solve it in a browser first, then retry; for the others, read the trace |
| a `problems` line saying the marketplace reads disagreed at a village | the page would not read the same way twice, so the run wrote to no village after that one | read that village in the game, then re-run once the marketplace is quiet; the villages it skipped are in `actions` as `deferred` |
| rows in the game > `live_game_rows` | the trim did not hold; checkable as a total only while the run is one origin and one destination — from step 4.2 on, compare per action at a village you have opened, not the run total | stop; the response names the rows still departing |
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
