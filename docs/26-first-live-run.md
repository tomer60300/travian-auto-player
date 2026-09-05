# 26 — The first live run of the distribution planner

Written 2026-09-05, against the execute path as it stands after the wave-1
review (`4b5203b` and later). Every control named here is quoted as the page
labels it. Every response field is quoted as `/routes/execute` returns it.
The protocol exists because the review found that the create half of the
executor was honest and the disable half was not, and that both are now
honest only along paths that have been driven with fakes, never against the
game. The first live run is the first time the game answers.

The one rule above all others: **nothing in this document may be skipped
because the previous step looked fine.** Every step exists because a specific
silence was found and closed today; the step is how we learn whether it stays
closed against a real marketplace.

## 0. Before anything touches the game

Settle these first. Each one changes what the run will do.

1. **The latency target.** The page no longer sends `max_latency_hours`; the
   backend's standing target of 2.0 h applies, clamped to each profile's window.
   Measured on the same night plan: an 8 h target planned 46 routes / 120
   merchants; a 2 h target planned 48 / 135. Decide 2 h, window length, or an
   exposed control **before** step 2, because the plan you confirm in step 1 is
   the plan step 2 writes.
2. **Live writes are off by default.** The response carries `live_enabled`.
   Until the live-writes switch is on, every "execute" is a dry run whatever
   the page says, and the response says so. Turn it on only at step 2.
3. **A run cannot be undone without its trace.** A live run whose trace file
   cannot be opened is refused before the first write (`4b5203b`). If the
   refusal fires, fix the trace directory; do not work around it.
4. **Save the setup to the server** (v10) and **export it** to a file, and
   load the file back once to prove it parses. The review found three ways a
   saved document could never be loaded again; all are closed, and this is the
   check that they stay closed for your account's actual figures.
5. **Read the plan's blockers to zero.** "Build plan" refuses while any cell is
   marked; the marked cell is named beside the Save and Export buttons. A
   plan with a blocker is not a plan.

## 1. Dry run of the whole day, read in full

Turn on **Whole day — execute all profiles at once**. Leave **Trim the fan-out
to the profile hours** as it shows (whole day forces it on; the page and the
request now agree). Press **Preview**. Nothing reaches the game.

Read, in this order, and stop on anything unexpected:

- `problems` — must be empty. A non-empty list is a refusal to proceed, not a
  note.
- `warnings` — read each. `MERCHANT_MODEL_UNCALIBRATED` on a village with a
  real Trade Office means the level was not typed; fix it. A merchant-boundary
  warning means the two profiles together commit more merchants than the
  fleet around the window edge; acceptable, but know which village.
- `requests_forecast` — the reads, disables, creates, verifies and trims the
  live run would spend. This is the price. If it is not roughly 1 read per
  village plus 1 create per route plus a handful of batched writes, something
  is planning more than you asked.
- `actions` — every planned row: origin, destination, cargo, repeat interval.
  Check three at random against what you would do by hand: is the destination
  the consumer or hub you expect, is the interval one of the eight the game
  offers, is the cargo within the origin's merchant capacity
  (2,500 × (1 + 0.2 × Trade Office level) per merchant)?
- `disables` — in a dry run these are the rows the live run **would** switch
  off. Read every one. If a destination you want fed appears here, either it
  is off-schedule for the profile (the plan moved it) or it is not in the plan
  at all. Protect it with **Never disable** (`x|y`) or fix the plan. Do not
  proceed with a disable you do not understand.
- `created_game_rows` vs `live_game_rows` and **Max rows this run** — the
  reported count is now measured after the trim; the budget is now an upper
  bound on live rows. Confirm the numbers are consistent with each other.

Paste the whole response somewhere you can compare against later.

## 2. One route, live, on a village you can watch

Pick one origin whose marketplace you will open in the game immediately after
the run. Prefer a village with **no existing trade routes**, because
`disable_existing` is on by default and the run will switch off every stale
row on the origin it visits — the review measured 6 disables from a run that
created 1 route. If no such village exists, either turn the disable-existing
option off for this run or list every existing destination under **Never
disable** first.

Set:

| control | value | why |
|---|---|---|
| origin filter | the one village | one marketplace to read afterwards |
| **Routes this run** | `1` | one create, one verify |
| **Max rows this run** | leave the default (24) | one route cannot exceed it |
| **Never disable** | every destination you are not ready to lose | the disable half is the one that was silent |
| **Whole day** | on | the same plan you confirmed in step 1 |

Turn the live-writes switch on. Press **Preview** once more and confirm
`live_enabled: true` and `dry_run: true`. Then execute.

Read the response:

- `created` = 1, `created_unverified` = 0, `not_created` = 0. If
  `created_unverified` is 1, the create's answer was unreadable and the
  read-back could not settle it; go to the game **before** doing anything
  else.
- `problems` empty. `disables` exactly what step 1 predicted for this origin,
  or empty.
- `trace_id` present. Write it down; it is the handle for **Undo this run**.
- `requests_forecast` spent versus forecast: 1 read, 1 create, 1 verify, plus
  a trim and its verify if the route's interval put rows outside the profile.

**Now open the game.** Marketplace of the origin village, trade-routes tab:

1. The route exists, to the planned destination, with the planned cargo per
   resource and the planned repeat interval.
2. Rows outside the profile's hours are gone — the trim ran and its own
   read-back confirmed it. Count the rows: they must equal the response's
   `live_game_rows` for this origin.
3. Nothing else on this village changed except the `disables` the response
   listed.
4. Merchants: the village still has the reserve you set (`merchant_reserve`,
   default 2) free. If merchants are already all committed, the cargo or the
   interval is wrong; do not widen.

If any of the four is false, stop. Use **Undo this run** with the `trace_id`,
confirm in the game that the route is gone and every disabled row is back on,
and bring the response and a screenshot to the review. The revert is the one
irreversible action; it now takes the same lock the execute does, and its
own read-back reports rows still there as "STILL THERE" rather than deleted.

## 3. Prove the undo once, on purpose

With the single route confirmed correct, undo it anyway: **Undo this run**,
then re-read the marketplace. The route must be gone and any row the run
disabled must be enabled again. This costs one delete, one re-enable and two
reads. It is the only time the undo is exercised against the game with
nothing at stake; after this, the undo is trusted because it was seen to work,
not because its tests passed.

Re-run step 2 to recreate the route. The response's `disables` must be empty
this time (nothing is stale) and `created` = 1 again with the same
destination and cargo. Convergence in two passes with no duplicate rows is the
property every interrupted shape was driven to; this is its first live
instance.

## 4. Widen, in three steps, reading every response

Each step is the previous one plus more; each response is read as in step 2
before the next step is taken.

1. **Routes this run** = 3, still filtered to the one origin. Confirm all three
   in the game.
2. Remove the origin filter, **Routes this run** = 3. The run now visits
   villages in plan order and creates at most 3. Read `disables` with care:
   this is the first run that can switch rows off on villages you have not
   watched. Any `problems` line ending an origin with zero active rows is a
   stop.
3. **Reconcile all villages**, with creates on. The sweep visits five villages
   per chunk with human pauses; `swept_origins` / `unswept_origins` and
   `next_chunk_wait_seconds` tell you where it is. One read per village now
   serves both the reconcile and the create. Let it finish; a deferred create
   gets its own pass at the end (`4b5203b`).

After the sweep, the run-history panel's `needs_attention` must be false for
every run, and a second sweep must report zero creates and zero disables:
the account has converged on the plan.

## 5. The first night

Do not let the first live run be at 23:00. The Night profile's rows were
created in step 4 alongside the Day profile's, so the night runs itself with
no request from us. The next morning, read:

- The night-profile stores: did any feeder's warehouse or granary hit the
  60 % target or fall to the 25 % baseline outside what the plan said?
- The crop-negative villages: is anyone's granary below where the plan
  promised at 07:00?
- If NPC was planned (attended or unattended, wood-low or crop > 700k
  trigger): the plan modelled the exchange; **no run performs it**. The NPC
  merchant exchanges inside one village and the executor writes only trade
  routes. If the plan relied on an NPC exchange at 02, you did it, or it did
  not happen.

## Stop rules, at any step

| signal | meaning | action |
|---|---|---|
| any `problems` line | a step ended without proof it did what it said | stop; read the trace by `trace_id` |
| `stopped_early` | captcha or activity budget | stop; do not retry within the hour |
| `created_unverified` > 0 | a create's answer was unreadable and the read-back did not settle it | read the marketplace before any other write |
| `live_game_rows` > **Max rows this run** | the trim did not hold | stop; the response names the rows still departing |
| a `disables` entry you did not predict | the plan moved or dropped a destination | undo, protect or re-plan |
| the game shows a row the response does not | reconciliation is wrong about the account | stop; this is a review finding, not an operator error |

## What this protocol does not cover

- The merchant model's calibration against your real Trade Office levels:
  `MERCHANT_MODEL_UNCALIBRATED` says when it is missing, and the capacity
  formula is one place in the code, but only the game's own "merchants
  needed" figure on the send form proves the constant for your server.
  Read it once on the first route and compare.
- Gold Club: `docs/15-gold-club-features.md`. A Gold Club account's route
  editor behaves differently and the executor has an early return for it.
