# Changelog

## [Unreleased] — 2026-09-05

A review day rather than a feature day: six independent audits — contract and
reachability, failure paths, accessibility, game mechanics, type checking and
mutation testing — were run against the distribution planner, and every finding
they raised was either fixed with a failing test first or argued equivalent in
writing. Sixty-odd commits, almost all of them corrections.

### Fixed — the night profile

- **No destination is promised a fraction of a trip.** `shed_limit` conserved
  merchant-hours at the demand-weighted mean hop, which lets merchant-time split
  fractionally across trips of different lengths — and a merchant cannot make
  0.6 of a trip. Two consumers needing 30,000/h each, one 2 fields away and one
  30, conserved exactly at 60,750/h while the far one needed 26.7 round trips of
  5h and eighteen merchants can make eighteen: 9,750/h of a hammer's deficit
  read as covered. Every destination now carries its own whole-trip bound and
  the limit is the smallest of them.
- **A material draw is priced at the receiver's hop, not the hub's.** Pricing
  every material sender against the hub bound it by a village its cargo never
  visits: a supplier 199 fields from the hub and **one** field from the receiver
  it feeds was told it could ship nothing over a ten-minute haul. The
  destination set for lumber, clay and iron is now the hub *and* every receiver,
  and the receiver branch is floored by what the receiver can actually ship —
  before which a village whose store left room for 44/h was booked to move
  19,956/h over 199 fields.
- **The crop draw is ordered by where the crop actually goes.** It ordered by
  the hub, or with a tribute by the tribute, and crop reaches neither: a
  supplier 2 fields from the hub and 18 from the hammer was drawn ahead of one
  19 from the hub and one from the hammer, so the plan built the long route at
  six merchants where the short one costs three, with the early firing still in
  the air at 09:00. Coverage is unchanged under every permutation; only the
  merchant bill moved.
- **Every foreign target is its own destination.** N obligations were summed
  into one rate pinned to the *first* target's coordinates, so a 500/h ally two
  fields out beside a 20,000/h artifact sixty out became 20,500/h at the
  two-field hop — and reordering the request body gave the opposite answer.
- **The night applies the tribute safety margin the day already applied.** The
  night freed the bare promise while the day booked promise-plus-margin, so the
  remainder village drained further than the profile predicted.
- **A tribute on one of your own villages is refused**, naming the village it
  collides with. A Travian tile holds one village, so it is a typo — and it
  surfaced as unmet crop with nothing connecting it to the coordinates that
  caused it. **An off-map coordinate is refused rather than folded**: the
  geometry took `span − raw` and then an absolute value, so (450|0) on a
  401-wide map read as 49 fields from the centre, a five-minute haul.

### Fixed — storage, schedule and the optimizer

- **The NPC conversion budget is capped at one day's allowance.** Accumulating
  without bound over the settling days the replay runs to reach steady state was
  the infinite reservoir again, wearing a rate.
- **Conversion no longer eats the feedstock floor**, and the debit is
  proportional to each store's share of the retention that sized the allowance,
  spilled onto whatever else has room where one store cannot cover its share.
- **`exact_arrival_minutes` no longer rounds**, a same-instant send is handled,
  a wrapping window's length is computed from both ends, and the arrival-gap
  default is pinned.
- **`WINDOW_PRUNED` has its own sentence.** It reused
  `WINDOW_NOT_ENFORCEABLE`'s message verbatim — which says "the destination
  receives about 3.0× what was modelled", the exact failure the prune exists to
  prevent. The note now says what is deleted and what is left; only the critical
  one reports an over-delivery.
- **The cadence cap is inclusive**, its empty-set fallback is pinned, and the
  sink-route weight is pinned against a merchant-neutral split.

### Fixed — the executor

- **A window prune that did not happen is reported.** The trim runs after the
  creates land and its failure was silent: the run reported the rows it meant to
  leave while the whole fan-out kept departing round the clock.
- **An off-schedule destination is switched off only when its rebuild is
  funded.** The disable happened first and the create cap or row budget then had
  nothing left, leaving a village receiving **nothing** — reachable on the first
  run at a cap of 1 against any village holding a previous plan's routes. The
  replacement is reserved out of both budgets before the disable, and where it
  cannot be, the refusal names the operator's own control and the real cause.
- **A rebuild the game refuses names the destination**, and so does a stop
  landing between the disable and the rebuild. One refusal is below the
  consecutive-failure limit, so the run reported an empty `problems` list over a
  village that had stopped being supplied.
- **A create whose answer died is settled by the marketplace read-back.** Where
  the read-back finds the route, the action is promoted, the "could not replace"
  line is withdrawn, the create leaves the consecutive-failure streak, and the
  stop that streak caused is lifted. Left standing, a flaky connection capped
  every run at two creates.
- **An unrecognised disable status is a failure, not a success.** Success is now
  the named branch: an unknown status read as "switched off" let creates stack
  on rows that may still be live, shipping both schedules at once.
- **The DELETE's own body is read**, through the same parser the toggles use —
  one call site from being the only guard on the one irreversible operation
  here. Its response shape is marked **UNVERIFIED for DELETE**: it is the game's
  bulk-*toggle* shape, and no DELETE reply has ever been observed on this
  account.
- **An unreadable cargo update is `unverified`, not `failed`**, which was the
  expected outcome of every cargo correction this app makes; and **protected
  rows are left out of the drift comparison entirely**, because a hand-made
  route always looks drifted and every rewrite also stamps `deliveries: 1`.
- **A live run with no trace is refused** before the first game request: the
  game returns no id on create, so the undo reconstructs from the trace's
  pre-write inventory, and without it the run is unrevertible.
- **`/routes/revert-plan` takes the lock `/execute` takes.** The one
  non-reversible action in the module had strictly less protection than the
  reversible one: a concurrent execution's fresh creates were attributed to the
  run being undone and deleted.
- **The sweep's re-pass condition is a fixed point.** It was gated on unvisited
  villages alone, so "swept" quietly meant "swept but only partly provisioned";
  gating on `deferred` alone would never terminate, because a route whose
  fan-out exceeds the row budget is deferred by every run alike.
- **A refused create makes a run need attention**, and observed rows are no
  longer clamped to the forecast.

### Fixed — the planner page

- **The page sends no `max_latency_hours` on any path.** It restated the
  backend's own policy on the client, and on a segmented request it was the
  wrong window entirely: selecting the Night tab before "run the whole day"
  planned the 16-hour day segment against an 8-hour target — shorter cycles,
  more routes, more merchants, more rows, on the endpoint that writes.
- **Nine boxes gained a bound the request will honour.** `min` and `max` on a
  number input bound the spinner and nothing else: typing 21 into a Trade Office
  box posted `trade_office_level: 21` with `aria-invalid` null. The gate is one
  predicate shared with the cell, in three lists for three audiences, and it
  reaches the full-day check, Preview, the live run and the night derivation —
  not only Build plan.
- **The reconciliation sweep is gated like every other write path.** It checked
  only that a plan existed and posted `dry_run: false`, so every marked cell
  Preview refuses went straight to a live, disabling run — and it is the one
  write button with no confirmation dialog.
- **`/day-check` is gated**, on the same list, and it carries the plan payload
  verbatim plus the crop alert levels typed below it.
- **The declared relay tier is in the gate, not only on the cell**, so a tier
  the store would refuse on `PUT` is refused before the document is written.
- **A blank merchant lever round-trips as blank**, a **fractional base capacity
  is refused** (it is a whole number of units on the cell and in the file), and
  the **row budget is sent as a figure** so a cleared box can still mean "no
  limit".
- **"Nothing typed yet" counts the whole document.** `buildSetup` writes twelve
  things and the guard counted three, so a page whose only content was a
  tribute, a window, the reserved NPC-burst window, an attendance answer, an
  overnight declaration or a deliberately unticked prune was told to fill
  something in — and the one owned answer it held went unsaved.
- **A typed foreign-target exclusion resolves into the document**, a role
  template's stored crop spend is stripped from it, half-typed state no longer
  blocks the whole setup, and profiles are capped at the segment ceiling.
- **The plan sheet says it is one profile once whole day is on**, and the
  result copy headlines the rows the run **leaves** rather than the rows it
  wrote.
- **Accessibility.** Twenty unlabelled controls across Auto-Scout, farm lists
  and the build queue were named and a duplicate embedded village selector
  removed; the nine bounded boxes set `aria-invalid` and `aria-describedby` from
  the same boolean; the unresolved-village warning is wired into
  `aria-describedby`; the Relays-for summary sets `aria-invalid` when refused;
  "Lift restriction", "Stop relaying" and the Relays-for summary carry the app
  focus ring; and a profile's Delete button names the profile it deletes.

### Added

- **A measured merchant model can clear its own warning.**
  `MERCHANT_MODEL_UNCALIBRATED` is an equality test against the shipped 0.20, so
  it cannot tell a measured 0.20 from an untouched one: an operator who read a
  Marketplace capacity at two Trade Office levels, found the default right and
  typed it back got the same warning for ever, asking them to do the thing they
  had just done. `PlanRequest.merchant_capacity_measured` is the operator saying
  they looked. It silences that one finding and **nothing else**; no bound, no
  budget and no other figure moves. It travels in the setup document as **v11**
  and is unticked automatically when either figure is edited.
- **The window prune travels in the setup (v10).** It decides whether `/execute`
  DELETES rows from the game — the only destructive answer the document carries
  — and neither persistence path held it.
- **`live_game_rows` is carried through the run summary and the run history**,
  so the footprint of a past run — what it left in the game, after the trim —
  survives the response that reported it.
- **`docs/26-first-live-run.md`** — the first live run, step by step, with the
  stop rules the review earned: what to settle before anything touches the game,
  what to read in the dry run, one route on a village you can watch, a rehearsed
  undo, and the four-step widening.
- **`docs/27-bug-map.md`** — the failure classes this review found in the
  planner and its execute path, each with the symbol it was found in, the defect
  as measured, the commit that closed it and the test that pins it; plus what is
  deliberately not covered (the four unverified game facts and the eight
  observations the canary must produce) and the open operator decisions.
- **`docs/28-production-readiness.md`** — the operational gate beside `docs/26`:
  the credential that is committed and how to find the others, the live database
  under `create_all` with no migrations, the two servers and why a frontend build
  is a deploy, the live-flag class and what a preview-only day needs, and what to
  read after a crash mid-run.

### Changed

- **`max_game_rows_per_run` now defaults to 24**, one day of hourly rows. An
  unbounded default on the one endpoint that writes was the opposite of what
  every other control here does, and the run already reported the number that
  nothing bounded. `0` is still unbounded, and the page always sends a figure,
  so the default governs a caller that omits the field rather than one that
  clears the box.
- **`/day-check` refuses segments without `prune_to_window`**, as `/execute`
  already did: without it the check the operator reviews is planned on the full
  cycle set while the run that writes is planned on divisor cycles with the
  out-of-window rows deleted.
- **Unknown keys are forbidden on the nested request models too**, not only on
  the top-level ones.
- **One list of Travian's repeat intervals**, not three; the three document
  writers are module-private; route handlers and plan parameters carry real
  return types.
- **The comments claiming live writes default off now say they default on**, and
  give the date (2026-08-27). Only the words changed: `Settings.trade_route_live`
  is `True`, `TradeRouteService.__init__` keeps `live_enabled: bool = False` as
  the library's own safe default, and `web/sessions.py` is the only caller that
  overrides it.
- **Constants the frontend copies are pinned as literals on both sides**
  (`tests/test_frontend_mirror_constants.py` ↔ `plannerSetup.test.js`), and the
  bounds are proved to bite rather than merely to be declared.

## [Previous] — 2026-09-04

### Added — Distribution planner: the requirements spec is operable from the UI

Sections 6, 7, 9 and 10 of the operator's spec had no implementation at all.
They do now, and the planner page was rebuilt around the order a plan is
actually assembled rather than around the endpoints it calls.

- **Night rules (spec §6).** Every night movement must be home before the
  profile switch: `night_overrun_minutes` prices each route's last in-window
  dispatch plus its round trip against the night's close, and `NIGHT_OVERRUN`
  (critical) names the route, the clock and the overrun. The beat is reshaped
  first and only refuses when no phase closes the night — it will not drop a
  firing, because the cargo was sized for the firings the plan counted.
- **The fill pair is 25% / 60%**, from 30% / 80%. One constant does both jobs:
  the room between baseline and target is what the night may ship, and the
  target *is* the morning floor. "Never overflow during the night, never arrive
  empty at morning" is one statement read from either side, and two constants
  could disagree.
- **`overnight` is declared, not derived.** A window that wraps midnight is
  still the default reading, but the operator's declaration wins. Deriving it
  alone was wrong in both directions: the half of a split night *after*
  midnight wraps in neither direction, so §6 applied to neither half and the
  60% floor was measured at midnight; and a near-24h day profile wraps, which
  silently suspended the latency target all day.
- **The 2-hour latency target does not bind at night.** `None` is exactly "no
  target", which also skips the pass that buys speed with merchants — the one
  thing a night that must end empty cannot afford.
- **NPC balancing (spec §7).** `services/distribution/npc.py`. The allowance is
  a rate built from rates — what a village retains of the resources it is not
  drawing on — so neither window length nor warehouse capacity can move it. The
  draw is a **cap consumed only against unmet demand**, never an addend, so a
  floor on a quiet village costs nothing. The reservoir is finite in both
  replays and the feedstock store is debited 1:1, which is what makes the
  700,000-crop trigger honest. `NPC_CAPACITY_SHORT` is critical and blocks
  execute.
- **Crop-profile drift (spec §9).** `RoleTemplate.assumed_crop_per_hour` — may
  be negative, `0` is the claim "breaks even", absent means "do not check me" —
  compared against the snapshot as `|actual − assumed| / |assumed|` past a 20%
  threshold. A warning, never a blocker: the profiles are hand-kept constants
  and drift is expected. A sign flip is reported as a gap *and* named in words.
- **Confirm-then-export (spec §10).** `/plan` returns a `plan_digest` over the
  response it showed; `/plan/yaml` re-plans, re-digests and **409s naming both
  digests** unless they agree, so the document either is the plan that was read
  or does not exist. The file is named for the plan, not the moment.
- **Server-side setup storage.** `GET`/`PUT`/`DELETE
  /api/distribution/setup?account_key=…`, one saved setup per user per account,
  stored verbatim and validated on `PUT` by reading the document *as* a
  `PlanRequest` so the rules are the planner's own rather than a second copy.
  `localStorage` is per origin, and the same app on `:80`, `:8001`, the LAN
  address and Tailscale kept four independent copies.
- **The document format went v6 → v9.** v7 carries per-profile
  `npc_attended`, v8 per-profile `overnight`, v9 the account-wide
  `reserved_window`. Each earned a version rather than riding along as an
  unknown key: all three are answers the planner refuses to guess, and a build
  that cannot read one drops it silently — after which the operator saves from
  that build and the answer is gone from the shared copy.
- **A reserved marketplace window.** Arrivals avoid the operator's manual NPC
  burst where any phase can manage it, ranked below "send at all" and "be home
  by the switch", and a warning names the route when geometry forces one in.
- **A four-stage planner page** — Account, Targets, Day & night, Plan — each
  answering one question, in the order a plan is assembled. Role templates were
  promoted out of a collapsed disclosure: a village's targets are resolved
  *from* its role, so the role's figures cannot be a footnote to the grid that
  shows their consequences.
- **An undo for a live run.** `/routes/revert-plan` was unreachable and the
  `trace_id` that keys it was discarded. The last-run panel now asks what
  undoing would take before offering to do it.

### Fixed — Distribution planner

- **`/execute` and `/day-check` planned the same profile against different
  latency targets** — 16h and 2h for a 07:00–23:00 day. The operator reviewed a
  sheet built from short cycles and small batches, and the writing endpoint
  recomputed with longer cycles and bigger batches into the same stores. Now
  `min(standing target, window length)`, derived once: a window may tighten the
  target, never loosen it.
- **The night shed limit measured to the nearest village, not the destination.**
  A neighbour one field away yielded 47 turnarounds in an 8h night, so the
  "limit" came out around six times fleet × capacity per hour — and because the
  operator's `max_busy_merchants` is clamped on and then multiplied by that
  count, the ceiling this function exists to honour was negated inside it. The
  `max(1, …)` trip floor is gone too: a village whose round trip does not fit
  the window now sheds nothing. Where the hub itself is the sender, the bound is
  the distance to the consumers or the tribute point — where its crop actually
  goes.
- **An unreadable bulk toggle was reported as total success.** A soft-block page
  or an HTML error body during a revert reported `disabled: 24 route(s)` while
  twenty-four rows kept shipping. `ToggleResponseUnreadable` now distinguishes
  "I could not check" from "the game refused nothing", as the read side already
  did, and the disable and enable messages differ because the consequences do.
- **Failed writes and reads did not bill the shared daily request ceiling** —
  including a Gold Club refusal, which returns "skipped" after the request went
  out. The ceiling is shared with the farm-list and oasis loops, so
  under-counting here licensed those to overspend.
- **The 429 backoff was switched off along with request pacing.** A server
  saying "slow down" is not a stealth preference; the penalty is now served
  whichever way the pacing flag is set.
- **The whole-day merchant boundary check ignored the reserve** and sat *after*
  the dry-run return, so the preview an operator authorises a live run from
  never showed it. Both endpoints now share it and measure against
  `merchant_budget(reserve)`.
- **`MERCHANT_MODEL_UNCALIBRATED` offered a village whose Trade Office nobody
  typed** as the place to read the merchant base from. Following that on a
  village that is really level 13 makes the base 3.6× too high, and every route
  is then sized to cargo the merchants cannot carry.
- **A round-the-clock route set was assumed attended.** It has all 24 hours,
  including the eight nobody is at the Marketplace. It now defaults to
  unattended, which under-delivers and says so rather than over-committing in
  silence.
- **Two adjacent functions computed a floored village's net rate differently**,
  so the continuous check read a drawn warehouse as draining forever and a
  feedstock granary as banking crop it was trading away.
- **The setup store accepted a document the page would then refuse**, and the
  two crop-drift comparison shapes that look right and are not are refuted in
  the code rather than left to be rediscovered.

### Fixed — Planner UI

- **Seven inputs had no styling at all.** `.input-sm` was named on seven
  controls and its only rule sat inside a mobile media query. Measured at 1440:
  16px tall, transparent, no border, no padding. Five of the seven are the
  live-run safety controls, and "Max rows this run" — empty by default — was
  invisible: there was nothing on screen to click.
- **The default live run bounded routes at 3 and game rows at infinity**, which
  is 72 rows on a 1-hour cycle.
- **A `Never disable` typo was undetectable by page and server.** `4688` for
  `46|88` is shape-valid as a village id, so the page now resolves entries
  against the village list — the server does not hold it.
- **Four `window.confirm` dialogs became `ConfirmDialog`**, including the live
  write. Chrome's "prevent this page from creating additional dialogs" makes
  every later `confirm()` return `false`, so the live button silently did
  nothing.
- **A stated NPC attendance answer was thrown away** when a profile had no
  window — which inverted the answer once absence began to mean "unattended".
- **`overnight` was unreachable from the app.** The backend learned the
  distinction and nothing sent it, so a split night still got the broken answer.
- **A disabled field did not look disabled, anywhere.** Chrome greys a disabled
  input by its own `color`, which an author declaration beats, so twenty value
  boxes that ignore typing looked identical to editable ones.
- **Five empty-vs-zero defects**, the worst being `Routes this run = 0` — which
  the backend documents as "reconcile only, create nothing" — falling through to
  three live route creations.
- **The plan survived edits it was computed from.** `relay_for` and
  `prune_to_window` were in the payload and in neither invalidation list, so the
  sheet went on describing a relay tier that had just been replaced.
- **Identity columns now pin wherever a table really overflows**, measured
  rather than gated on a breakpoint; nine controls gained accessible names; and
  the client log store no longer records request or response bodies, which had
  come to include the whole plan document and every village name and coordinate.

## [Previous] — 2026-09-03

### Added — Resource Distribution Planner: operator-declared village state

The planner gained the vocabulary to describe an account instead of inferring
it. Every field below is state the game does not report; each is refused rather
than silently ignored where it cannot mean anything, and each is validated once
so the rule reaches all four planning paths (`/plan`, `/day-check`, `/execute`,
`/night-profile`).

- **Roles** — `VillageConfig.role` plus a `Role` StrEnum (`capital`,
  `troops_off`, `full_off`, `def`, `feeder`). Decides who may relay
  (`default_may_relay`: `feeder` only, so not the capital) and how loud a
  designed crop deficit is. `PlanRequest.roles` carries one template per role,
  so profile section 2's four defensive figures are typed once, and
  `PlanResponse.role_deviations` names every cell where a village overrode its
  template. A role with no template is a 422, not a default.
- **Declared material relay tier** — `VillageConfig.relay_for`. Materials only,
  one hop. Crop relays are *searched*; material relays are *declared*, and the
  asymmetry is deliberate. The operator's own route is 02 → 18/14 → 11/17/19,
  and which relay serves which downstream is a fixture choice, not a solver
  output. Enforced: a relay may not feed a relay, a role village may not relay,
  `feeder` and a village with no role may.
- **Relay buffer check** — `RELAY_BUFFER` (critical) and `RELAY_BUFFER_TIGHT`
  (warning). A relay must hold `collect_rate × max(collect_cycle,
  forward_cycle)` — one cycle's pass-through — and the bound is **capacity, not
  free space**, so a 160,000 warehouse at 94% still holds a 33,488
  pass-through. Blamed on the tier only for overflow the pass-through can
  explain; a relay's own retention is reported by the ordinary store check.
- **`may_relay`** — per village, overriding its role template and then the
  role's own default, for the account whose one defensive village sits on the
  only road to a corner of the map.
- **`ship_only_to`** — a per-village whitelist of own destinations, binding
  every resource including crop. Resolves into the same origin-exclusion map the
  optimizer already reads. Foreign tributes are governed by their own
  `exclude_origins` and report `whitelist_vs_tribute` when supplied from a
  restricted village.
- **`stock_floor_fraction`** — the fraction of warehouse capacity a village
  keeps stocked by NPC trading, spread across the profile's window as extra
  supply (`fraction × capacity / window_hours`). Materials only — a granary is
  not NPC-fed. Also holds the store up as a floor in the day simulation.
- **`consumption_per_hour`** — what a village spends per hour, lumber/clay/iron
  only, so its store nets `target − consumption` instead of being modelled as
  stockpiling every unit (an army village read as losing 354,024/day at a cap it
  never reaches). Crop is refused by design: the snapshot's `crop_per_hour` is
  already net of troop upkeep, so declaring it again double-counts.
- **`max_busy_merchants`** — a per-village ceiling on merchants underway;
  `merchant_budget = min(spare_merchants, max_busy_merchants)`. A cap, not a
  reserve — a reserve costs every village the same merchants. A cap above the
  village's own fleet is refused rather than clamped.
- **`village_nets`** — per village, per resource: own production, supplement,
  target, cargo, spend and the resulting net, so the allocation grid reads the
  net from the plan rather than recomputing it in JavaScript. Own villages
  only; a foreign tribute has no store and appears in `shortfalls`.
- **Frontend controls** for all of the above on the planner's owned-state path,
  plus inputs for the two merchant-model levers.

Documented in `docs/25-resource-distribution-planner.md` Part IV.

### Fixed

#### Resource Distribution Planner

- **Merchant base capacity was 2,200, from a superseded reading** — the operator
  re-read the base as 2,500 (+20% per Trade Office level) on 2026-09-02.
  `EUROPE2_TEUTON` and the frontend's `DEFAULT_MERCHANT_MODEL` both carried
  2,200, derived from a live reading of 7,920 per merchant at TO 13 that fitted
  `2200 × 3.6` exactly. 2,500 predicts 9,000 there, so the two readings are
  **ordered, not reconciled** — both came off the game and the mechanism is not
  established. Capacity is the denominator of every merchant count, so this
  moved plan outcomes: three relabelling-audit seeds and one relay fixture were
  re-measured rather than relaxed. Settleable at zero request cost by reading
  capacity at a second Trade Office level.
- **The origin exclusion only ever bound the greedy seed** —
  `excluded_origins_by_destination` was handed to `_flows_for_resource` and
  nowhere else, so a 2×2 swap or a crop relay inside `_improve_flows` was free
  to name a forbidden sender. Found on the live account before its first run:
  village 02 was excluded from the foreign tribute, and the night plan shipped
  it 10,266 crop/h on 9 merchants regardless — an unrecoverable write. Every
  candidate pair is now vetted, both legs of a relay included.
- **A downstream named twice was shipped twice** — the relay tier sizes its
  collecting leg from the sum of its downstreams' gaps, so a duplicate inflated
  the leg and the forward loop handed that village its whole target once per
  mention: 16,744/h against an 8,372/h target, with the downstream it displaced
  reported unreachable and blaming the whitelist. Refused at the schema and
  conserved in the solver.
- **The relay tier's source was chosen blind to the merchant cap** — the tier's
  legs merge in after the improvement search, so nothing downstream re-prices
  them. On distance alone a capped village was committed 4 merchants against a
  cap of 2 while an affordable source of the same cargo stood one field further
  out. The sort key is now `(merchants over budget, distance, coordinates)`,
  priced with the plan's own route arithmetic.
- **`ABSOLUTE` allocation is a retention target, not a consumption figure** —
  `ship_per_hour = target − own production`, so the village retains the target
  and the figure already includes what it grows. Village 01's granary therefore
  genuinely gains 8,519/h, which makes its 204,456/day overflow finding real
  rather than an artefact; a negative absolute target is refused rather than
  planned.
- **The netting invariant is per resource** — a village may legitimately
  receive clay and send lumber, which village 02 does with 13 inbound rows and
  one outbound. The no-waterfall rule is per resource and now carries the
  declared-relay exception explicitly.

#### Frontend — accessibility and table pinning

- **Identity columns pin wherever a table actually overflows** — `.sticky-col`
  was gated on `@media (max-width: 640px)`, on the claim that desktop tables do
  not overflow. Measured, that is false at both other viewports: the Snapshot
  table's intrinsic width is 1408px in a 470px container at 768, and Role
  templates is 1839px with 717px of overflow even at 1440, so the operator
  scrolled with no village pinned and no hint that ten columns existed.
  Pinning and the scroll hint now both follow a `ScrollableTable` container's
  **measured** `scrollWidth > clientWidth` — a ResizeObserver whose first
  delivery lands before paint — instead of a breakpoint. "Swipe" became
  "Scroll" now that the hint reaches mouse users too.
- **Nine controls gained accessible names** — farm lists' `Status` filter, the
  auto-scout stealth-delay pair and its four bonus-resource selects, and both
  village selectors (the shell's two renders and Build queue's own), each
  repeating the visible words verbatim for WCAG 2.5.3 Label in Name. Four
  filter and loop boxes were named in the preceding round.
- **The Build queue panel stopped scrolling sideways** — two `flex-1 min-w-0`
  siblings of one flex row left the queue card 154px wide holding 389px of
  content at 375 and 250/397 at 768. Stacked below `lg`, with the bulk bar
  wrapping; 0px of overflow at all three viewports.

---

## [Previous] — 2026-04-28

### Added — Resumable Cross-Device Operations

Long-running ops (oasis raid, farm-list run, auto-scout, build queue, farm
builder) now survive Safari background, page reloads, bfcache restores, and
cross-device control — a stop pressed from a laptop terminates an op that
was started from an iPhone, and vice versa.

- **`OperationManager` + `ExecutionSession` registry** — every long op runs
  inside `OperationContext` whose messages are buffered in a 24h ring buffer
  and fanned out to any number of subscribers via per-subscriber asyncio
  queues. Sessions persist after the starter WS drops; subscribers reattach
  via `WS /ws/sessions/{id}/stream`.
- **`useResumableOperation` React hook** — single hook each page mounts
  with `(opType, {onMessage, onStatusChange})`. Internally manages the
  starter WS → session-stream WS handoff, persists `session_id` in
  `localStorage`, deduplicates history-replay frames by server-side `ts`,
  and exposes a `stop()` that works through whichever socket is open.
- **Cross-device stop** — `WS /ws/sessions/{id}/stream` accepts
  `{action:"stop"}` from any subscribed client; the `_listen_for_stop`
  task forwards it to `operation_manager.request_stop(session_id)` so
  `ctx.should_stop()` flips for the running coro.
- **Stop-while-reconnecting** — `pendingStopRef` queue + drain on
  `session_init` / `session_meta(running)`, plus active reattach via
  `subscribeToExisting()` when the user taps Stop with no live socket.
- **Auto-reconnect-on-terminal fix** — `closeWs()` now fires from every
  terminal frame (`session_ended`, `error`, `operation_complete`) so the
  captured WS path doesn't keep reopening after the server closes the
  session-stream. (This was producing a 1-second connect/close loop that
  silently swallowed Stop presses.)
- **Race hardening** — `mountedRef` so unmount-cleanup doesn't reattach;
  `inConstruction` flag for sync-onClose-during-construction races;
  handle-specific `closeIfStillCurrent()` so a terminal frame that
  triggers a sync reattach doesn't close the new socket.

See `docs/22-resumable-operations.md` for the full protocol contract and
client behavior.

### Added — Stealth Hardening Pass

The stealth stack got a layer-by-layer review focused on bot-tells that
wouldn't show up in fixed-cost benchmarks (timing patterns, identical
permutations, header-shape mismatches, request-ordering desync). Most
fixes are zero-cost or sub-second; the few that aren't are gated on
glaring tells.

- **TLS fail-closed** — stealth + missing `curl_cffi` now raises instead
  of degrading to `httpx` with Chrome headers (mismatch was a stronger
  tell than running stealth-off).
- **PRG redirect headers** — `post_form` now generates fresh page-load
  headers for the redirected GET instead of reusing the form POST's
  `Content-Type`/`Origin` (a real browser issues the GET as a fresh
  document navigation).
- **`request_type="xhr"` plumbing** — `post_json`/`delete_json` now
  accept `request_type="xhr"` so endpoints that the Travian frontend
  calls via fetch (map/position, tile-details, /api/v1/farm-list/*)
  send the XHR header shape (`X-Requested-With`, `Sec-Fetch-Mode: cors`)
  instead of generic JSON-client headers.
- **Persona scoping** — TTL bumped from 7d to 365d (rotating UA/TLS
  mid-cookie-jar was itself a tell); persona file now tracks the server
  URL and rotates on a server change.
- **Jittered `get_html` retry** — `wait_random_exponential` instead of
  the textbook 1s/2s/4s power-of-two cadence.
- **Captcha guard escalation** — short 403/503 pages with high-confidence
  block phrases (`access denied`, `bot-detection`, `automated access`,
  `your ip has been`) now hard-fire the captcha guard; soft-penalty
  reserved for transient signals (429, embedded-in-bundle false
  positives).
- **`zstd` Accept-Encoding** — Chromium personas advertise zstd to match
  current Chrome stable (mismatch is checked against UA in modern
  detectors).
- **Navigator helpers** — new `navigate_to_map`, `navigate_to_farm_list`,
  `pre_construct_flow` so feature code stops calling private `_visit`.

#### Per-feature stealth fixes

- **Oasis raider** — pre-tile-details navigation is now `navigate_to_map`
  (not random `idle_browse` which produced impossible Referer chains);
  troop deduction + burst counter only on `result.success` (soft failure
  applies a 60s throttle penalty and breaks the sweep — no more "ghost
  sends" with locally-deducted-but-server-untouched troops); recurring
  interval gets `micro_jitter(0.10)`; tile XHRs route through
  `request_type="xhr"`.
- **Auto-scout** — scan centers sorted by distance from player village
  then shuffled within 4-tile buckets (in-place, not a slice copy); same
  bucket-shuffle on `filter_targets` so target permutation varies across
  runs; `navigate_to_map` before tile loop; tile XHRs via XHR header
  shape; jittered scout-retry replaces fixed 3s.
- **Farm-list** — stealth floor of 60s on `interval` when stealth is on;
  `time.monotonic()` activity logging in try/finally so every exit path
  feeds the scheduler; `navigate_to_farm_list(owner_vid)` before send;
  `send_all_farm_lists` groups by owner village so cross-village handoffs
  trigger fresh navigation; per-cycle batch size jitter (4-7) instead of
  invariant 5; 0.25-0.9s pause between batches; troop-exhaustion now
  advances the cursor PAST the depleted batch (no more bot-like
  instant-retry on the same empty slots); send + add_slot + delete_slots
  use `request_type="xhr"`.
- **Build queue** — `_post_build_reaction()` heavy-tailed 20-300s wakeup
  window after every queue→empty transition (real players don't return
  to the browser exactly 3 seconds after a timer hits zero);
  `pre_construct_flow` for new construction (after queue/can-build
  guards so no wasted page loads on requests that will be rejected);
  client `poll_interval` clamped to [30s, 1h]; resource-short waits
  2-10min planner-style instead of polling every 30s; per-account
  build-action lock + 10-90s stagger when multiple village queues
  collide on the same account.
- **Farm builder** — `_pace_add(list_id)` before every add_slot
  (RAPID-class delay + per-8-slots heavy-tailed pause, gated on
  `stealth_enabled` AND `human_delay.enabled`); pacing inside retry loop
  and after overflow handoff; `navigate_to_farm_list()` once before bulk
  edit; per-bucket shuffle so target order varies between runs;
  jittered scout-send / report-fetch retries.

See `docs/23-stealth-decisions.md` for the trade-off analysis behind each
choice.

---

## [Previous] — 2026-04-13

### Added

#### Captcha/Bot Detection — Emergency Halt System
- **CaptchaGuard module** — New `asyncio.Event`-based per-user gate (`stealth/captcha_guard.py`) that blocks ALL outbound HTTP requests when bot detection fires, and resumes when the user resolves the captcha.
- **Structural validation** — Bot detection now uses HTML structural evidence (captcha divs, script tags, error page size, HTTP status codes) instead of naive substring matching to avoid false positives from the word "recaptcha" appearing in normal Travian JS bundles.
- **Rich diagnostic logging** — When detection fires, logs include: URL, HTTP status code, response length, and a ±200 char snippet around the matched pattern (HTML-stripped).
- **Full-screen captcha alert modal** — Non-dismissible modal (`CaptchaAlert.jsx`) that appears on any page when bot detection triggers, showing pattern, URL, status code, response snippet, and step-by-step resolution instructions.
- **"Dismiss (False Positive)" button** — Quick resolution option for false positives without requiring a browser visit.
- **REST endpoints** — `GET /api/captcha/status` and `POST /api/captcha/resolve` for checking and clearing captcha state.
- **Page-refresh recovery** — Frontend checks captcha status on WebSocket connect so the modal reappears after a page refresh.
- **Multi-tab broadcast** — `captcha_alert`/`captcha_resolved` messages broadcast to all user WS connections.

#### Auto Scout — Real Player Population from Profile Pages
- **Profile page population lookup** — New `get_player_population(player_id)` method fetches `/profile/<player_id>` and extracts real account-wide population from the React JSON data (`ranks.population`), not the misleading `<div class="population">` footer which shows the logged-in user's own pop.
- **Batch profile fetching** — `fetch_player_populations(player_ids)` fetches multiple profiles sequentially (respects throttler) with progress reporting.
- **Occupied oasis population inheritance** — Occupied oases (population=0) now inherit their owner's total population from their profile, so village-level and player-level filters both apply correctly.
- **Profile vs visible breakdown in UI** — Scan results show `PlayerName: 676 (profile) | visible: 228 = Village(16,93)=228` when profile data differs from visible village sums.

### Fixed

#### Auto Scout — Max Player Pop Filter (Critical Logic Error)
- **Incorrect population calculation** — The `max_player_pop` filter was summing populations only from villages found within the scan radius. Players with villages outside the radius had their total population undercounted, allowing large players to bypass the filter. Now fetches real total population from each player's profile page.

### Changed

#### Bot Detection — Reduced False Positives
- **`recaptcha` pattern** — No longer triggers on bare substring match in large responses. Requires structural HTML evidence (`class="g-recaptcha"`, `<script src="...recaptcha/api...">`), a short error page (<5000 chars), or an error HTTP status (403/429/503).
- **Other high-confidence patterns** — `bot-detection`, `suspicious activity`, `access denied`, etc. now also require short response or error status code context before triggering the guard.

---

## [Previous] — 2026-04-12

### Added

#### Auto Scout — Player Population Debug Logging
- **Player population breakdown in scan logs** — After enrichment, the scan WS now always sends a `player_pops` message showing each player's total population with per-village breakdown (e.g., `PlayerA: 350 = Village1(50,60)=200 + Village2(55,65)=150`). Previously this was only shown when `max_player_pop` was set.
- **Player population in auto-scout logs** — The auto-scout WS panel also displays per-player population breakdown before scouting begins, making it easy to verify the `max_player_pop` filter calculation.

#### Auto Scout — Farm List Integration
- **Farm list badges in scan results** — Each scan result row now shows which farm list(s) the target already belongs to, displayed as gold badges in a new "Farm Lists" column.
- **Quick-add to farm list** — Each scan result row has a "+Farm" button that opens a dialog to add the target to any farm list with configurable troop type and count.
- **`GET /api/farm/coord-map` endpoint** — New lightweight API that returns a coordinate-to-farm-list mapping for all farm list slots, enabling efficient lookup without N+1 queries.
- **Shared troop constants** — Extracted `TRIBE_TROOPS` and `DEFAULT_TROOPS` from `Military.jsx` into `constants/troops.js` for reuse by `AddToFarmDialog`.

#### Farm Lists — Defense Scan Combat Strength
- **Combat strength extraction** — Battle report parser now extracts attacker and defender combat strength from the `<table class="combatStatistic">` element, supporting both English ("Combat strength") and German ("Kampfkraft") labels.
- **Combat strength display** — Farm list defense column now shows the defender's combat strength value (red when defenders present, green "Empty" when 0), with troop breakdown in hover tooltip.

### Fixed

#### Auto Scout — Occupied Oasis Handling
- **Oasis player info preservation** — During tile enrichment, player/alliance info from the map scan is now preserved when the tile-details HTML parser doesn't extract it (e.g., occupied oases using "Occupied by" instead of "Owner").
- **Oasis owner detection** — The tile-details parser regex now matches both `Owner` and `Occupied by` labels for player extraction.
- **`max_player_pop` filter for oases** — With player info properly preserved, occupied oasis population is now correctly included in player totals and the `max_player_pop` filter applies to them.

#### Farm Lists — Defense Scan (3 critical bugs)
- **Dict-as-object access** — `scan_defense_strength` used `getattr(detail, 'data')` on a plain dict, which always returned None. Fixed to use `detail.get('data')`.
- **Nested coordinate extraction** — Defender coordinates were accessed as flat `x`/`y` keys but the parser returns `{'coordinates': {'x': ..., 'y': ...}}`. Fixed to access the nested structure. This was the root cause of defense data never populating.
- **Report age computation** — Report age was read from a non-existent `time` attribute. Fixed to parse `date_str` using `parse_report_date()`.

---

## [Previous] — 2026-04-09

### Added

#### Web UI (`travian-web`)
- **Full self-hosted web dashboard** — React + Vite + Tailwind CSS frontend served by FastAPI backend
- **Multi-user auth** — SQLite with bcrypt password hashing, JWT tokens, Fernet-encrypted Travian credential storage
- **Per-user session isolation** — Independent HTTP clients, cookies, and service instances per user
- **Auto-reconnect** — Saved credentials used to restore sessions automatically on 403
- **Dashboard** — Resource bar, construction queue with live countdown timers, quick actions, player info
- **Buildings page** — List all building slots, upgrade/construct with confirmation, live construction queue
- **Farm Lists page** — Full CRUD with sortable/filterable slot table, booty as taken/capacity (183/300), last raid datetime, active/inactive sync, multi-select copy/move between lists, background defense scan from reports
- **Auto Scout page** — Map scan with alliance/player exclusion (persisted in localStorage), population filters, max player pop, loop mode with configurable interval
- **Military page** — Tribe-aware scout dispatch, manual raid form with dynamic troop fields
- **Reports page** — Paginated report browser with collapsible raid analyzer panel
- **Build Queue page** — Visual drag-and-drop plan builder, YAML editor, template library (6 tribe-specific templates), validation, live WebSocket execution
- **Video Rewards page** — Claim individual or all production boosts
- **Activity Log** — Real-time server-side log streaming via WebSocket, client/server origin filter, level badges, search, export to JSONL
- **Server-side log broadcast** — `LogBroadcastHandler` captures Python logging records and streams to connected web clients
- **WebSocket channels** — Farm loop-send, auto-scout, build queue execution, log streaming, raid analyzer — all with live progress
- **Rate limiting** — Per-user sliding-window rate limiter on action routes (upgrade, construct, scout, raid)
- **Correlation IDs** — `X-Request-ID` header on all API responses for request tracing
- **Security headers** — CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy (relaxed in dev mode via `TRAVIAN_DEV=1`)
- **Light theme** — CSS variable-based theme with `[data-theme="light"]` support

#### CLI
- **`travian reports village X Y`** — Gather all reports (own + alliance) for any village from the map tile popup. Uses `/api/v1/map/tile-details` endpoint. Supports `--details` for full report parsing.
- **Build plan templates API** — `GET /api/queue/templates` returns 6 tribe-specific build plans (resource, military-roman, military-teuton, military-gaul, economy, settler)

#### Raid Analyzer v2 Pipeline
- **Scout-gated entry** — Only scout reports enter the pipeline (with battle report fallback when no scouts exist)
- **Coordinate deduplication** — Multiple scouts on the same target produce one fetch, not many
- **GQL pre-filter** — Radius, alliance, NAP alliances, population threshold — all applied before any HTML fetch
- **`fetch_village_reports` integration** — One API call per target returns all reports (own + alliance)
- **Cross-run TTL cache** — `VillageReportCache` with configurable TTL (default 30min). Repeated runs 90%+ faster
- **Short-circuit on depletion** — If most recent raid had `carry_full=false`, skip all computation, straight to re-scout queue
- **Binary search scoring** — O(log n) instead of O(5000) for defended targets. Direct O(1) calculation for undefended
- **Re-scout queue** — Depleted + stale targets output as actionable list with reason, distance, last report time
- **New settings** — `nap_alliances`, `max_population`, `stale_hours`, `cache_ttl_minutes`, `village_report_concurrency`

#### Scout Optimization
- **Navigate-once** — Rally point navigation only on first target, subsequent targets skip it
- **Stealth countdown** — `"waiting"` messages streamed during inter-target delays with seconds remaining
- **ETA computation** — After first target, estimated completion time shown in progress bar
- **Pre-send target list** — All targets sent to frontend immediately after scan
- **Loop mode** — Checkbox in Auto Scout panel, configurable interval, re-scouts same targets repeatedly

### Fixed

#### Backend (19 fixes across 3 rounds)
- `http_client.py` — Concurrent `follow_redirects` race condition (per-request param instead of client mutation)
- `http_client.py` — `get_html` 429 response not penalized (added 120s throttle penalty)
- `http_client.py` — Cookie load/save errors silently swallowed (now logged)
- `http_client.py` — `ConnectionResetError` from Travian rate limit (30s penalty + retry)
- `ws/manager.py` — WebSocket `close()` without `accept()` per ASGI spec
- `ws/logs_ws.py` — Log subscriber ID collision for multi-tab users (unique per connection)
- `ws/logs_ws.py` — `_stream_logs` crash on send after close (`RuntimeError` caught)
- `ws/queue_ws.py` — Anonymous `stop_event.wait()` task leaked (named variable)
- `ws/queue_ws.py` — `run_coroutine_threadsafe` race condition (replaced with `asyncio.Queue`)
- `ws/farm_ws.py` — `next_send_at` inverted logic + field names mismatched frontend
- `ws/farm_ws.py` — Per-target failure details only in verbose mode (now always sent)
- `routes/farm.py` — Defense scan `except: continue` without logging
- `routes/scout.py` — `max_player_pop` description misleading (clarified as scan-radius only)
- `raid_analyzer_service.py` — Coordinate (0,0) silently dropped (changed to `None` sentinel)
- `raid_analyzer_service.py` — `eff_R` double-penalizes raided targets (only decay for "scouted")
- `raid_analyzer_service.py` — Trap capacity formula wrong (`4+36*L` → `12*L` per Travian spec)
- `raid_analyzer_service.py` — `last_report_time` set to last-iterated, not newest
- `raid_analyzer_service.py` — `_resolve_source_village` silently returns (0,0) (now raises error)
- `raid_analyzer_service.py` — Missing `Callable` import
- `auto_scout_service.py` — `enrich_tiles` concurrent semaphore bypassed throttler burst limit (now sequential)
- `reports_service.py` — Page count log variable wrong after break
- `reports_service.py` — `fetch_village_reports` used `karte.php` HTML instead of `tile-details` API
- `rate_limit.py` — Unbounded key growth (prune empty keys when >100)
- `app.py` — SPA catch-all returned `None` for API 404s (now returns `JSONResponse(404)`)
- `auth.py` — Keys file in CWD (moved to `~/.travian/`), permissions warning on Unix
- `db.py` — Database path hardcoded (now `~/.travian/travian_web.db`, configurable via `TRAVIAN_DB_PATH`)

#### Frontend (14 fixes across 3 rounds)
- `Reports.jsx` — `useState` used instead of `useRef` for WebSocket (crash + memory leak)
- `Reports.jsx` — Re-clicking Analyze leaks previous WebSocket
- `authStore.js` — `require()` fails in Vite/ESM (replaced with top-level imports)
- `logStream.js` — `reconnectAttempts` never reset after disconnect (reconnect permanently disabled)
- `AutoScout.jsx` — Stale closure in loop mode (all loop-read values moved to refs)
- `BuildQueue.jsx` — Timer accumulation on re-execute (clear before new)
- `Buildings.jsx` / `BuildQueue.jsx` — Construction queue countdown never ticks (added interval timer)
- `Buildings.jsx` / `BuildQueue.jsx` — Missing `activeVillageId` dep (stale data after village switch)
- `Dashboard.jsx` — Timer tick applied uniformly to all queue items (snapshot-based)
- `Dashboard.jsx` — Missing `activeVillageId` dep in data-loading useEffect
- `FarmLists.jsx` — `stopLoop` doesn't close WebSocket (only sends stop message)
- `ErrorBoundary.jsx` — `require()` fails in Vite/ESM (changed to dynamic `import()`)
- `Military.jsx` — NaN stored for non-numeric troop input (`isNaN` guard)
- `Layout.jsx` — Redirect fires without checking current route + duplicate ToastContainer
- `VillageSelector.jsx` — No disable during async village switch (concurrent calls)

### Changed
- `pyproject.toml` — Added `[web]` optional dependencies (fastapi, uvicorn, sqlalchemy, etc.) and `travian-web` entry point
- `logging_config.py` — `SensitiveDataFilter` only redacts actual credential values (not messages about credentials)
- `app.py` — CSP headers relaxed in dev mode (`TRAVIAN_DEV=1`), correlation ID middleware added
- Farm WS `cycle_end` fields renamed `success`→`sent`, `fail`→`failed`, `cycles`→`total_cycles` to match frontend
- `SavedServerResponse` field `travian_username`→`username` to match frontend
