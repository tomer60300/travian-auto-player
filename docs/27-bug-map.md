# 27 — Failure-class map: the distribution planner and its execute path

Written 2026-09-05 against `3f4d121`, the tip of `fix/planner-review-hardening`.
It is a map of **failure classes**, not a bug list: each row is a *shape* of
defect that six independent audits found, the place it was found, what it did
when it was measured, the commit that closed it, and the test that will fail if
it comes back.

**How every claim here was checked.** A previous doc-truth audit of this work
found nine statements written from commit subject lines that the code
contradicted. So the rule for this document is: every symbol named below was
read in the tree at `3f4d121`, and every test named below was located by
`grep -rn "def <name>"` (Python) or by its title string (Playwright/vitest) in
that same tree. Where a *measurement* comes from an audit report rather than
from code I read, it is marked **(audit measurement)** — those numbers were
produced by driving the code at an older commit and are not re-derivable from
today's source. Nothing is cited by line number: backend and frontend writers
moved every line in these files repeatedly during the review, sometimes within
the hour.

**Where the endpoint actually lives.** `distribution.py` declares
`router = APIRouter(prefix="/api/distribution")`, and the execute handler is
`@router.post("/execute")` — so the write path is
`POST /api/distribution/execute`, which is also the path the page posts to in
`ResourcePlanner.jsx`. `docs/26-first-live-run.md` calls it `/routes/execute`
throughout; that path does not exist. Only `revert-plan` sits under
`/routes/` (`POST /api/distribution/routes/revert-plan`).

Companion documents:

- `docs/25-resource-distribution-planner.md` Part IV — the shipped behaviour,
  and §4.20 in particular is the narrative version of sections A–C below.
- `docs/26-first-live-run.md` — the protocol that turns section H's unknowns
  into observations.
- `docs/28-production-readiness.md` — the operational half: secrets, database,
  servers, live flags.
- `docs/22-resumable-operations.md` — the resume model the trace-based undo
  sits inside.

---

## The classes, in one table

The detail sections that follow are keyed to these names.

| # | Failure class | Reachable today? | Section |
|---|---|---|---|
| 1 | Silent partial outcome — a step that fails and reports nothing | closed | A |
| 2 | Landed write reported as refused | closed | B |
| 3 | Budget that is not a bound | closed | C |
| 4 | Save-then-cannot-load | closed | D |
| 5 | Duplicated constant drift | closed | E |
| 6 | Tab-dependent request | closed | F |
| 7 | Latency clamp inert | closed, target now reported | F |
| 8 | Over-promising shed bound | closed | G |
| 9 | Unstable read-back treated as an answer | closed | B |
| 10 | Ambiguous attribution | closed | B |
| 11 | Unfunded rebuild | closed | C |
| 12 | Stale default or stale comment | closed | E |
| 13 | Accessible-name collisions | closed | I |
| 14 | Optional field read as present (crash) | closed | H |
| 15 | Destructive path with weaker guards than the reversible one | closed | A |
| 16 | Unverified game fact stated as measured | **open by design** | J |

---

## A. Silence — a step that ended without saying so

The wave-1 failure-path audit's verdict, verbatim: *"The create half: yes. The
disable half: not as shipped."* Every row here is a step that could fail and
leave a green response.

| what was silent | module:symbol | the defect as measured | fix | the test that pins it |
|---|---|---|---|---|
| A window prune that did not happen | `web/routes/distribution.py` — the prune branch that consumed `TradeRouteService.delete_routes` | Only a `deleted` status reported anything. `delete_routes` returns `failed` on a `NetworkError` and `stopped` on the captcha/budget check, so a prune that removed nothing produced a **byte-identical** response to one that worked: `created_game_rows: 42`, `problems: []`, 26 night rows departing round the clock under a green toast **(audit measurement)** | `d4aa17d` | `tests/test_distribution_execute.py::test_a_refused_prune_names_the_rows_still_departing`, `::test_a_stopped_prune_stops_the_run`, `::test_a_refused_prune_makes_the_run_need_attention` |
| An unrecognised disable status | `web/routes/distribution.py`, disable-status handling | Anything the code did not recognise fell through the success arm, so creates stacked on rows that were still shipping | `57804c6` | `tests/test_distribution_execute.py::test_an_unrecognised_disable_status_is_not_read_as_success` |
| A run that stopped between the disable and the rebuild | `web/routes/distribution.py`, the replacement sequence | The destination was emptied and never named | `8ae66b1` | `tests/test_distribution_execute.py::test_a_stop_between_the_disable_and_the_rebuild_names_the_destination` |
| One refused create, below the streak limit | `services/distribution/run_history.py` — `needs_attention` | `_CONSECUTIVE_FAILURE_LIMIT` is `2` (read in `distribution.py`), so a single refusal produced no `problems` entry at all and the audit report read clean | `3c06bbc` | `tests/test_run_history.py::test_a_single_refused_create_is_not_reported_as_a_clean_run` |
| A destination the reconciler could never fund | `web/routes/distribution.py`, the sweep's re-pass condition | The reason lived only in the trace's `decision` events; the sweep asked to be called back for ever | `49691ea` | `tests/test_distribution_execute.py::test_a_sweep_that_can_never_fund_the_rest_stops_asking` |
| A run with no trace file — unrevertible, and silent about it | `services/distribution/execution_trace.py` — `ExecutionTrace.path` returns `None` when the file could not be opened | `TRACE_DIR = Path.home()/".travian"/"traces"`; the undo reconstructs from the trace's pre-write inventory, because the game returns no id on create. No trace meant no undo, announced by nothing | `4b5203b` | `tests/test_distribution_execute.py::test_a_live_run_refuses_when_the_trace_cannot_be_written`, `::test_nothing_is_written_to_the_game`, `::test_a_dry_run_is_unaffected` |
| **Class 15** — the one irreversible endpoint took no lock | `web/routes/distribution.py` — `post_revert_plan` vs `post_execute` | `post_execute` holds a 409 on concurrency, an execute lock, a per-village origin lock and an `active_ops` entry; `post_revert_plan` held none. `plan_revert` attributes everything new since the trace inventory to the reverted run, so a concurrent execute's fresh creates were deleted irreversibly | `949224c` | `tests/test_revert_endpoint.py::test_a_run_in_flight_is_a_409`, `::test_nothing_is_read_or_written_while_it_is_refused`, `::test_the_lock_is_held_for_the_whole_revert` |
| A marketplace that stopped agreeing with itself, then more villages written | `web/routes/distribution.py` — `marketplace_state_uncertain` | Before the breaker, a village whose two reads disagreed did not stop the run; every later verdict rested on a page that would not hold still | `241d664` | `tests/test_distribution_execute.py::test_the_village_after_an_indeterminate_one_is_never_visited`, `::test_the_response_says_why_it_stopped`, `::test_a_steady_account_visits_every_village` |
| The trim proceeding on a page the run could not attribute | `web/routes/distribution.py`, the same-origin pre-mutation gate | The trim is the only DELETE in the system; it now refuses for the **whole page** an origin could not read, and the restore keeps only the stable half | `11a8a21` | `tests/test_distribution_execute.py::test_the_trim_deletes_nothing_anywhere_on_that_page`, `::test_the_response_says_the_trim_was_skipped_and_why`, `::test_the_rows_left_departing_round_the_clock_are_named` |

**Known non-guardable point, recorded rather than fixed.** Backend round 8 found
that the third write — the cargo/drift `PUT` via `TradeRouteService.update_cargo`
— is issued from the destination loop, which runs *before* the read-back, so it
cannot be made to follow settlement within an origin. A guard there would be
unreachable code; it is pinned by test instead
(`tests/test_distribution_execute.py::test_no_cargo_is_rewritten_on_that_page_either`,
added in `11a8a21`).

---

## B. Attribution — who wrote what, and did it land

| what was wrong | module:symbol | the defect as measured | fix | the test that pins it |
|---|---|---|---|---|
| **Class 2** — a create that landed but whose answer died was reported as refused | `services/trade_route_service.py` — `create_route` maps `NetworkError` to `failed`; the read-back gate required a claimed success | 30 active rows in the game, `created: 0`, and a `problems` line asserting that the game refused the creates **(audit measurement)** — the verdict `distribution.py` still emits, and now only emits, for a genuine refusal: *"the game refused N create(s) in a row"*. `post_json` produces exactly that error for the session-expiry redirect on a non-retryable write, a connection reset and a curl failure | `60daa64` | `tests/test_distribution_execute.py::test_a_landed_create_is_reported_as_created`, `::test_a_create_that_really_was_refused_stays_failed`, `::test_the_marketplace_is_actually_re_read` |
| The refusal verdict survived the promotion | `web/routes/distribution.py`, the refusal verdict and the early-stop | A run whose creates *all* landed still reported them refused, and still stopped | `c273a4c` | `tests/test_distribution_execute.py::test_a_run_whose_creates_all_landed_is_not_reported_as_refused`, `::test_the_stop_it_caused_is_lifted_so_the_next_origin_still_runs`, `::test_a_genuine_refusal_still_stops_the_run` |
| The streak was a decremented counter, not a ledger | `web/routes/distribution.py`, the consecutive-failure accounting | A promotion arriving before a success lifted a *later* pair of failures | `f11f2ff` | `tests/test_distribution_execute.py::test_a_promotion_before_a_success_does_not_lift_a_later_pair`, `::test_a_refusal_ending_one_origin_and_one_starting_the_next_still_count_two` |
| An unreadable cargo-update answer read as a refusal | `services/trade_route_service.py` — `update_cargo` returned `failed` where `_toggle_routes` returns `unverified` on the same `ToggleResponseUnreadable` | On this account every 200 body is empty, so `failed` was the **normal** outcome: the run said "0 corrected" while N rows had been reset, and printed a line saying the live route was still shipping the old amounts — a claim that was unknown when made and whose likeliest truth was the opposite. That sentence now fires only from an *observation*: the surviving wording in `distribution.py` is "the page still shows the old amounts", emitted from the post-write re-read | `2f74c5c` | `tests/test_trade_route_payload.py::test_an_unreadable_body_is_unverified_not_failed`; `tests/test_distribution_execute.py::test_rows_still_carrying_the_old_amounts_are_reported`, `::test_an_update_only_run_still_reads_back` |
| The DELETE's status code was trusted over its body | `services/trade_route_service.py` — `delete_routes` discarded the parsed body; `_rejected_routes` was never applied to it | Every 2xx became `deleted`, including a body naming a rejected route, an HTML soft-block and a non-object. The prune's read-back only ran on `deleted`, which this always was | `f47083f` | `tests/test_trade_route_payload.py::test_a_clean_response_is_a_clean_delete`, `::test_a_rejected_route_makes_the_whole_delete_a_failure`, `::test_a_body_we_cannot_read_is_unverified_not_deleted`; `tests/test_revert_endpoint.py::test_an_unverifiable_delete_is_settled_by_the_read_back` |
| **Class 9** — an absence finalised from a pair of reads that disagreed | `web/routes/distribution.py` — the `indeterminate` action status | Before this, "I could not see it" and "it is not there" were the same answer. `indeterminate` is now a fourth status and the row charge is retained rather than refunded | `15475f4` | `tests/test_distribution_execute.py::test_an_absent_create_on_an_unstable_page_is_indeterminate`, `::test_the_row_charge_of_an_indeterminate_create_is_retained`, `::test_an_indeterminate_create_is_not_a_refusal_in_the_streak`, `::test_the_indeterminate_destination_is_named_in_problems` |
| One stabilising read was not taken before a delete | `web/routes/distribution.py`, the pre-delete stabilising read; trace kind `read_back_disagreed` | A page that lagged the create was read once and deleted from | `53ab710` | `tests/test_distribution_execute.py::test_a_page_that_lagged_the_create_is_settled_by_one_more_read`, `::test_a_page_that_never_settles_is_never_deleted_from`, `::test_a_trim_always_costs_the_stabilising_read_first`, `::test_the_disagreement_is_in_the_trace` |
| **Class 10** — two rows the matcher could not tell apart | `web/routes/distribution.py`, the read-back matcher invariant (narrowed to surviving minutes) | Two routes to one destination sharing a surviving departure minute were attributed arbitrarily; the invariant is now checked before the write and refuses, naming both route ids | `f730069`, narrowed by `ef6f38c` | `tests/test_distribution_execute.py::test_an_ambiguous_pair_is_refused_and_both_routes_are_named`, `::test_an_identical_fanout_under_overlapping_windows_is_refused`, `::test_two_cycles_that_merely_overlap_are_settled_by_the_destination`, `::test_the_invariant_reads_the_same_minutes_the_matcher_claims_by`, `::test_two_identical_rows_are_the_same_route_not_an_ambiguous_pair` |
| A destination settled one row at a time | `web/routes/distribution.py`, destination-level pre-trim settlement | Two indistinguishable creates could each steal the other's row; a destination is now settled as a whole, by elimination, and an unattributable one is never trimmed | `ef6f38c` | `tests/test_whole_day_execute.py::test_a_barren_create_is_found_by_elimination_not_by_stealing_rows`, `::test_two_indistinguishable_creates_where_one_landed_are_both_indeterminate`, `::test_an_unattributable_destination_is_never_trimmed` |
| The disable reported one status for a whole batch | `web/routes/distribution.py`, per-row disable outcomes | A single verdict hid rows that went off, rows that did not, and rows the read-back could not see | `8e65ece` | `tests/test_distribution_execute.py::test_a_clean_disable_confirms_every_row`, `::test_an_unreadable_answer_over_rows_still_shipping_is_a_failure`, `::test_rows_the_read_back_could_not_see_are_unknown` |
| A canary claiming reversibility it could not prove | `web/routes/distribution.py` — `ExecuteResponse.canary_rows_created: list[int] \| None`; trace kind `canary_settled` | The undo list is the whole claim a canary makes. `[]` is a measurement, `null` is "could not settle"; a failed read-back or a run that never posted claims nothing | `4c199b9` | `tests/test_distribution_execute.py::test_the_response_names_every_row_the_create_made`, `::test_a_failed_read_back_claims_no_reversibility`, `::test_a_canary_that_never_posted_claims_no_reversibility_either`, `::test_the_trace_carries_the_same_id_set` |
| A restore blocked by a row a dual had already switched on | `web/routes/distribution.py` — `already_enabled_ids` / `enabled_by_request_ids` / `restoration_completed` in the trace | The bulk `PUT` states each row's target state rather than flipping it, so an already-on row is a no-op in the same request; before this, one such row abandoned the other seven | `fe84298` | `tests/test_distribution_execute.py::test_one_row_already_back_on_does_not_abandon_the_other_seven`, `::test_the_operator_line_counts_only_the_rows_this_run_switched_on`; `tests/test_trade_route_payload.py::test_a_bulk_enable_states_the_target_state_it_does_not_flip` |

---

## C. Budgets and bounds — a control that authorised more than it said

| what was wrong | module:symbol | the defect as measured | fix | the test that pins it |
|---|---|---|---|---|
| **Class 3** — `max_game_rows_per_run` was not an upper bound | `web/routes/distribution.py` — `ExecuteRequest.max_game_rows_per_run`, and `_game_rows(cycle_hours)` which turns one create into `24 / N` rows | `created_game_rows` was measured **pre-prune**, so the "measured" field described a state the same run destroyed; the server default was `0` (unbounded). Budget 8 gave 18 live rows, budget 16 gave 42 — 2.3–2.6× the authorised footprint **(audit measurement)** | `456bf02` | `tests/test_distribution_execute.py::test_the_live_count_is_what_survived_the_trim`, `::test_a_prune_that_did_not_happen_reports_the_whole_footprint`, `::test_the_server_default_is_twenty_four_rows`, `::test_zero_still_means_unbounded_when_asked_for`; `tests/test_frontend_mirror_constants.py::test_the_row_budget_a_run_gets_by_default_is_twenty_four` |
| The corrected count never reached the operator | `services/distribution/run_history.py` — `RunSummary.live_game_rows`; `ResourcePlanner.jsx` result copy | The API gained `live_game_rows` and the page still headlined `created_game_rows`; the run summary dropped it entirely | `d143f08` (API) + `fdb4a22` (page) | `tests/test_run_history.py::test_the_rows_the_run_left_live_are_reported_too`; `frontend/e2e/rowFootprint.pw.js` — *"a live run headlines the held count, not the pre-trim count"*, *"a preview above the row budget reads as deferral, not as a refusal"* |
| A landed dead answer charged the budget in the wrong unit | `web/routes/distribution.py`, the row charge on a promoted create | It was charged the fan-out where it had refunded post-prune rows, silently deferring the next origin | `de49b8c`, then `ec9faf0` holds the charge until the read-back settles it | `tests/test_distribution_execute.py::test_a_landed_dead_answer_is_charged_the_same_rows_it_refunded`, `::test_a_dead_answer_holds_its_charge_until_the_read_back_settles_it`, `::test_a_create_that_really_was_refused_never_overspends_either` |
| Observed rows clamped to the forecast | `web/routes/distribution.py`, the observed-row count | A game that fanned out more than predicted could not report it, so `live_game_rows` undercounted | `33c33e6` | `tests/test_distribution_execute.py::test_an_overshoot_against_the_forecast_is_reported_too` |
| **Class 11** — an off-schedule destination was emptied with no budget to rebuild it | `web/routes/distribution.py` — `_off_schedule(...)`, against the create cap and the row budget | The code's own comment states the invariant ("we never disable a destination we are about to create") and `_off_schedule` was exactly the case that broke it. One origin, two mismatched destinations, `cap=1`: `created=1`, `problems=[]`, and a village that had three shipments a day got none — under a green toast **(audit measurement)**. Reachable on the *first* live run at `cap=1` against any village holding routes from a previous plan | `add3978` | `tests/test_distribution_execute.py::test_the_unfundable_destination_keeps_its_rows`, `::test_it_says_which_destination_was_left_diverging`, `::test_a_cap_that_covers_both_replaces_both`, `::test_a_row_budget_too_small_to_rebuild_also_holds_the_rows` |
| A *reserved* rebuild the game then refused still left the destination empty | `web/routes/distribution.py`, the refused-rebuild path | The reservation made the run *able* to rebuild; it did not make the game agree. `problems` was empty and the toast was green | `4a90884`, then `313bbf9` makes the run put the old rows back itself | `tests/test_distribution_execute.py::test_a_refused_rebuild_puts_the_destination_it_emptied_back`, `::test_a_wholly_refused_replacement_restores_the_old_rows`, `::test_a_destination_that_changed_underneath_is_not_restored`, `::test_an_indeterminate_replacement_is_never_restored`; `tests/test_whole_day_execute.py::test_a_half_written_rebuild_is_abandoned_not_restored` |
| The replacement had no write-ahead record | `web/routes/distribution.py`, the write-ahead replacement record and its closing chain | Without a record on disk before the disable is sent, a run that died mid-replacement could not say what the destination used to look like | `b404e85` | `tests/test_distribution_execute.py::test_the_record_is_on_disk_before_the_disable_is_sent`, `::test_the_record_carries_the_whole_old_configuration`, `::test_a_run_that_stops_before_the_rebuild_closes_the_record` |
| At `cap=1` a two-route destination could never converge, and the remedy text was wrong | `web/routes/distribution.py`, the reserve refusal message | The refusal blamed the row budget for a filter exclusion | `e34f7af` | `tests/test_whole_day_execute.py::test_the_refusal_names_the_cap_that_would_work`, `::test_the_cap_it_names_actually_reconciles_the_destination`; `tests/test_distribution_execute.py::test_a_filter_excluded_mismatch_is_not_blamed_on_the_budget` |
| A protected destination's cargo was rewritten anyway | `web/routes/distribution.py`, the cargo-drift update vs `protect_destinations` | `protect_destinations` removed the destination from the mismatched set and said "left untouched" — then the drift pass collected the same rows and rewrote them with `deliveries: 1`. A hand-built ally route at `deliveries: 3` would ship one third of its volume, undetected | `1cbfb53` | `tests/test_distribution_execute.py::test_a_protected_routes_cargo_is_not_rewritten_either`, `::test_the_same_cargo_is_rewritten_without_the_exemption` |
| Nine page boxes had bounds only as HTML attributes | `frontend/src/utils/plannerBlockers.js` — `planBlockers` / `runBlockers` / `nightBlockers`, consumed through `describeBlockers` | Attribute-only bounds let a typed figure the request refuses reach every write path, with nothing marked on the cell — and the Export writer produced a file its own parser refuses | `ab735a0`, gates widened to the sweep in `f69806c` | `frontend/e2e/plannerBounds.pw.js` — *"Trade Office level past 20"*, *"a foreign-target safety margin past 100"*, *"more than 2000 game rows in one run"*, *"the sweep refuses a marked plan cell too"*; `frontend/src/utils/plannerBlockers.test.js` |

---

## D. Save-then-cannot-load — a document the writer accepts and the reader refuses

The class: the setup document is stored **verbatim** server-side, and
`SetupDocument` ignores unknown keys, so a field the page writes and the
schema does not declare round-trips through the store and is then refused by
`parseSetup` on the way back in. Four instances, all closed.

| instance | module:symbol | the defect as measured | fix | the test that pins it |
|---|---|---|---|---|
| An even `map_span` / a zero `speed_fields_per_hour` | `frontend/src/utils/plannerSetup.js` — `buildSetup` wrote the whole merchant model; `web/routes/planner_setup.py` — `MerchantModelIn` declared neither, so `PlanRequest._span_is_odd` never saw it | Real HTTP: `PUT` an even span returns 200, `GET` returns it, `parseSetup` then refuses the file. Save/Export were gated on `busy` only, never on blockers | `5632d3d` (schema) + `cd023b3` (gate) | `tests/test_planner_setup_store.py::test_an_even_map_span`, `::test_an_odd_map_span_is_accepted_and_round_trips`, `::test_a_merchant_speed_of_zero`; `frontend/e2e/setupSaveGate.pw.js` — *"an even map span holds both writers back"*, *"loading is left alone, because a load is the way out"* |
| A blank base capacity or Trade Office bonus | `MerchantModelIn.base_capacity` / `bonus_per_to_level` were required; blank means "use the planner's own" on the plan path | `PUT` returned 422 "Field required" with no cell marked | `5632d3d` (both optional) + `61f810a` (blank round-trips as blank) | `tests/test_planner_setup_store.py::test_a_model_with_neither_figure_saves`, `::test_the_absent_figures_are_not_written_back`, `::test_a_figure_the_planner_refuses_is_still_refused`; `frontend/e2e/blankMerchantModel.pw.js` — *"saves, instead of coming back 422 over a box that reads blank"*, *"comes back out of the store still blank, not filled in"* |
| A role template's crop spend | `plannerSetup.js` — the request builder strips it, `buildSetup` wrote it raw | Plan worked; save 422'd on `roles.def.consumption`; Export wrote a file `parseSetup` refuses — over a figure with no box to clear it from | `90f79e3` | `frontend/src/utils/plannerSetup.test.js` — *"strips a stored crop spend out of a template on the way into the document"*, *"leaves a template whose only spend was crop declaring nothing"* |
| Half-typed state — a draft target row, a window with only its start | `plannerSetup.js` — `buildSetup` | A fresh "+ Add target" row and a `["07:00",""]` window blocked the **whole** document while any of three boxes was mid-edit | `2f1c6b0` | `frontend/e2e/halfTypedSetup.pw.js` — *"a fresh \"+ Add target\" row does not travel, and does not stop the save"*, *"a window with only its start typed does not stop the save"*; `frontend/src/utils/plannerSetup.test.js` |
| A typed foreign-target exclusion, lost on the round trip | `plannerSetup.js` — `buildSetup` wrote `foreignTargets` raw, so the exclusion lived only as `exclude_origins_text`; `parseForeignTargets` rebuilt field by field and emitted `exclude_origins: []` | Exclude the hub from a tribute, save, reload — the hub supplies it again, with no message. The same denylist `94892ed` had just fixed at the other end | `80fd7bb` | `frontend/src/utils/plannerSetup.test.js` — *"resolves the typed names to ids on the way into the document"*, *"keeps a stored exclusion the operator never retyped"*, *"leaves an unresolvable name out rather than inventing an id for it"* |

**The version discipline this class produced.** `READABLE_VERSIONS` is
`(1, …, 11)` in `web/routes/planner_setup.py` and `SETUP_VERSION` is `11` in
`plannerSetup.js`. The rule the file's own docstring states: an answer the
planner refuses to guess earns a version rather than riding along as an unknown
key, because the harmful path is identical for each — a new build writes it, an
older build silently drops it, the operator saves from the older build, and the
answer is gone from the *shared* copy, which is the only reason the store
exists. Two answers earned versions during this review: `prune_to_window`
(v10 — it decides whether the run DELETEs rows) and the merchant-model
acknowledgement (v11).

Both directions of the coercion trap are closed the same way, with
`StrictBool`: `SetupDocument.npc_attended` / `overnight` / `prune_to_window`
refuse the string `"yes"`, which pydantic's lax bool would have accepted and
`parseSetup` would then have thrown on. Pinned by
`tests/test_planner_setup_store.py::test_the_prune_answer_is_not_coerced_from_a_string`
and `::test_the_measured_answer_is_not_coerced_from_a_string`.

---

## E. Words and duplicates — statements that were false rather than code that was wrong

| what was wrong | module:symbol | the defect | fix | the test that pins it |
|---|---|---|---|---|
| **Class 5** — the merchant model was duplicated with nothing pinning it | `plannerSetup.js` — `DEFAULT_MERCHANT_MODEL`; `services/distribution/merchants.py` — `EUROPE2_TEUTON = MerchantModel(base_capacity=2500, bonus_per_trade_office_level=0.20)`, which `PlanRequest.merchant_base_capacity` defaults from | The page seeds from its own frozen constant and **sends all four levers explicitly**, so a backend default change is silently overridden with the old value. This is the numbers that size every cargo — and 2,500 only recently stopped being 2,200 | `a552199` (frontend literals) + `b1c51de`, `0b2c385` (backend literals) | `tests/test_frontend_mirror_constants.py::test_the_base_capacity_is_two_thousand_five_hundred`, `::test_the_trade_office_bonus_is_a_fifth_per_level`, `::test_the_merchant_reserve_is_two`, `::test_the_merchant_headroom_is_a_tenth`; `frontend/src/utils/plannerSetup.test.js` — *"DEFAULT_MERCHANT_MODEL matches the four levers the planner defaults to"* |
| A third shadow copy of the cycle list | `ResourcePlanner.jsx` carried its own array beside `plannerSetup.js` — `TRAVIAN_REPEAT_INTERVALS` (now the one list, `[1,2,3,4,6,8,12,24]`) and the backend's `DAILY_BEAT_CYCLES` | Three copies of Travian's eight legal repeat intervals | `17c7e83` (dedup) + `a552199` (pin) | `tests/test_frontend_mirror_constants.py::test_the_cycles_are_the_divisors_of_a_day`; `frontend/src/utils/plannerSetup.test.js` — *"TRAVIAN_REPEAT_INTERVALS matches DAILY_BEAT_CYCLES"* |
| Constants the pin file *claimed* were pinned | `MAX_TRADE_OFFICE_LEVEL`, `MAX_MERCHANTS_PER_VILLAGE` | The mirror test's header named them; no assertion existed | `0b2c385` | `tests/test_frontend_mirror_constants.py::test_a_trade_office_reaches_level_twenty`, `::test_a_village_reserves_at_most_twenty_merchants` |
| `MAX_DAY_SEGMENTS` had no frontend twin and no cap | `plannerSetup.js` — `MAX_DAY_SEGMENTS = 12`; the page's `addProfile` | A thirteenth profile was a pydantic list-length 422 pointing at nothing | `a552199` | `frontend/e2e/profileCap.pw.js` — *"a thirteenth is refused here, not by a 422 that names nothing"*, *"duplicate is capped too, because it makes a profile as well"*; `tests/test_frontend_mirror_constants.py::test_a_day_holds_twelve_profiles` |
| **Class 12** — three comments said live writes default OFF | `services/trade_route_service.py` module docstring and the constructor comment; `web/sessions.py` at the construction site | `Settings.trade_route_live` in `config.py` has `default=True` and has since 2026-08-27. **Only the words changed.** `TradeRouteService.__init__` keeps `live_enabled: bool = False` — that is the library's own safe default, which every test and every direct construction depends on, and `web/sessions.py` is the only caller that overrides it with the settings value. Do not "correct" it to match `config.py` | `983a295` | No test: this commit changed comment text only. The behaviour it describes is pinned by `tests/test_distribution_http_contract.py::test_dry_run_false_alone_is_refused_and_names_the_field` and the `execution_mode` suite below |
| A field description that described the old page | `ExecuteRequest.max_game_rows_per_run` description said the UI omits the field when the box is blank | The page sends `0` explicitly now (`bb193d2`), so the default governs an *omitted* field rather than a cleared box | `8d967f0` | `frontend/e2e/rowBudget.pw.js` — *"a blank box asks for no limit, and the wire says 0"*, *"a typed 0 is the same 0, not the same omission"*; `frontend/src/utils/plannerSetup.test.js` — *"MAX_GAME_ROWS_PER_RUN matches the row budget the server defaults to"* |
| A finding that named a control the page no longer has | `services/distribution/findings.py` — `Category.LATENCY` | Its action text said "raise the latency target" after the page stopped sending `max_latency_hours` on every path | `eaf34cb` | `tests/test_distribution_findings.py::test_it_no_longer_points_at_a_control_the_page_does_not_have`, `::test_it_names_the_one_way_the_target_can_still_be_set`, `::test_the_standing_figure_it_names_is_the_request_default` |
| `WINDOW_PRUNED` reused another finding's sentence verbatim | `services/distribution/schedule.py`, the pruned-window note | It described the breach that would have happened rather than the prune that prevented it — false whenever the prune is on | `22f066a` | `tests/test_window_execution_truth.py::test_the_note_describes_the_prune_rather_than_the_breach`, `::test_the_breach_keeps_its_own_words`, `::test_both_findings_quote_the_windows_real_length` |
| A field name that claimed more than it covered | `PlanRequest.merchant_capacity_measured` (was `merchant_model_measured`) | "Model" is capacity **and** speed; only capacity was measurable from the send form. Renamed with no alias | `b89e0f4` | `tests/test_planner_setup_store.py::test_the_old_field_name_is_gone_and_not_kept_as_an_alias`, `::test_the_measured_merchant_capacity_round_trips` |

**A stale statement still standing at `3f4d121`.** `CHANGELOG.md` under
`[Unreleased]` referred to `PlanRequest.merchant_model_measured`; the field is
`merchant_capacity_measured`, and `tests/test_distribution_routes.py` asserts
the old name is **absent** from `PlanRequest.model_fields`. Corrected in the
same commit as this document. Every other surviving mention of the old name in
the tree is a deliberate "it was called X until v11" note, in
`docs/25`, `planner_setup.py`, `distribution.py` and the two tests above.

---

## F. Requests that depended on the wrong thing

| what was wrong | module:symbol | the defect as measured | fix | the test that pins it |
|---|---|---|---|---|
| **Class 6** — the request carried the ACTIVE TAB's window onto every other profile | `ResourcePlanner.jsx` derived `max_latency_hours` from the selected profile's dispatch window; the segmented request builders stripped `allocations`, `dispatch_window`, `npc_attended` and `overnight` — **not** this | Driven in a browser, whole-day preview, identical state, only the selected tab differing: active Day gave 16, active Night gave **8**, with byte-identical segments. So clicking the Night tab before "run the whole day" planned the *Day* segment against 8 h — shorter cycles, more routes, more merchants, more rows — on the endpoint that writes **(audit measurement)** | `036ea19` — the page now sends it on **no** path | `frontend/e2e/segmentedLatency.pw.js` — *"/day-check is byte-identical whichever profile is selected"*, *"a whole-day /execute preview is byte-identical too"*, *"a single-profile plan does not send one either"* |
| **Class 7** — the latency clamp was inert | `_plan_account` in `distribution.py` applies `min(max_latency_hours, window_minutes/60)`; the page had already been sending the window length as the target | `min(16, 16)`. The consequence was certain: `RELAY_LATENCY` could never fire inside a profile. The repo's own test asserted 2.0 on a payload that **omits** the field — which the app never sent **(audit measurement)** | `036ea19` makes the omission real, so `PlanRequest.max_latency_hours = 2.0` is what applies | `tests/test_distribution_routes.py::test_a_sixteen_hour_window_reports_the_standing_target`, `::test_an_hour_long_profile_reports_the_window_that_tightened_it` |
| The number that bound the routes was not reported at all | `PlanResponse.latency_target_hours`, `DayCheckResponse` per-segment; `craft_plan` in `services/distribution/planner.py` sets `latency_target = None if overnight else config.max_latency_hours` | The clamped figure reached the client only inside finding prose, rounded to whole hours and unsafe to parse | `4d17ae9`, corrected by `b40e242` to report what bound the routes rather than what was asked for | `tests/test_distribution_day_check.py::test_each_segment_is_named_with_the_target_its_plan_used`, `::test_a_caller_supplied_target_is_reported_as_each_window_clamped_it`; `tests/test_distribution_routes.py::test_an_overnight_profile_reports_no_target_at_all`; `frontend/e2e/latencyTarget.pw.js` (5 cases) |
| An overnight profile's `null` target might have been an omission | measured directly: sweeping the target over 2 / 24 / 0.5 | The whole night plan **and** the derived night profile were byte-identical across all three, and the day control did move — so `null` is honest, not a gap | `52b9c1b` (test-only) | `tests/test_distribution_routes.py::test_the_whole_night_plan_is_byte_identical_across_three_targets`, `::test_the_same_sweep_does_move_a_day_plan`; `tests/test_night_profile_endpoint.py::test_the_derived_profile_is_identical_across_three_targets` |
| `prune_to_window` had one answer per builder | `ResourcePlanner.jsx` — execute forced `true` under whole-day, plan and day-check sent the operator's (false) state, and the checkbox displayed ticked | The check the operator reviews was planned on the full cycle set while the run that writes was planned on divisor cycles with the out-of-window rows deleted | `fb70094` + `85ab028` (page), `f352981` (server refuses segments without it) | `frontend/e2e/pruneCoherence.pw.js` — *"whole day forces the prune on every request, not only the one that writes"*, *"the full-day check prunes whatever the box says, because it always segments"*; `tests/test_distribution_day_check.py::test_a_segmented_check_without_the_prune_is_refused`, `::test_saying_so_explicitly_is_refused_the_same_way` |
| `dry_run` was the only statement of consent | `ExecuteRequest.execution_mode: Literal["preview","live"]`, resolved once at the request boundary | Consent to write was inferred from an absent boolean. It is now a named field: `dry_run: false` alone is a 422 that names the field, `execution_mode: "live"` with `dry_run: true` is refused as contradictory, and a null mode is refused rather than read as omitted | `26205b8`, boundary-resolved in `d4d6fcd` | `tests/test_distribution_http_contract.py::test_a_body_with_neither_field_previews`, `::test_dry_run_false_alone_is_refused_and_names_the_field`, `::test_live_with_dry_run_true_is_refused_as_contradictory`, `::test_an_unknown_mode_is_refused`, `::test_dry_run_null_is_refused_rather_than_read_as_omitted`, `::test_the_handler_reads_the_mode_off_the_body_exactly_once`; `frontend/e2e/executionMode.pw.js` |
| `extra: "forbid"` was top-level only | the nested request models | A typo inside a nested object was accepted and ignored | `48ce08d` | `tests/test_distribution_execute.py::test_a_nested_model_refuses_an_unknown_key_too` |

---

## G. Over-promising — a bound that was necessary but not sufficient

| what was wrong | module:symbol | the defect as measured | fix | the test that pins it |
|---|---|---|---|---|
| **Class 8** — the shed bound conserved merchant-hours and ignored whole trips | `services/distribution/night_profile.py` — `shed_limit`, and `_destinations(v, resource)` which supplies its `(hop, weight)` pairs | The weighted-mean bound lets merchant-time split fractionally across trips of different lengths, and a merchant cannot make 0.6 of a trip. H=8, fleet 18, capacity 9,000, two consumers needing 30,000/h at 2 and 30 fields: conserved exactly at 60,750/h with `unmet: 0`, while the far one needed 26.7 round trips of 5 h and eighteen merchants make eighteen — **9,750/h of a hammer's deficit read as covered**. Merchant-hours balanced exactly and the answer was still infeasible **(audit measurement)** | `1b1fc65` adds the per-destination whole-trip bound; `7b33f9a` replaces both with a **partitioned-fleet** bound | `tests/test_night_profile_derivation.py::test_every_destination_gets_a_whole_number_of_trips`, `::test_an_unreachable_destination_does_not_zero_the_reachable_ones`, `::test_the_counterexample_sheds_nothing_on_one_merchant`, `::test_it_matches_an_exhaustive_search_over_merchant_allocations`, `::test_a_single_destination_is_the_whole_fleet_on_one_leg` |
| The crop draw was ordered by somewhere the crop does not go | `night_profile.py` — the `_cost` ordering, now the demand-weighted mean of `_destinations(v, CROP)` | Ordered by the hub, or by `tribute_at` when set — neither is where crop goes. A supplier 2 fields from the hub and 18 from the hammer was drawn ahead of one 19 from the hub and 1 from the hammer: six merchants spent where three would do, plus a 120-minute `NIGHT_OVERRUN`. Coverage is provably order-invariant (`give = min(own, demand, shed_limit)` and `shed_limit` reads nothing the loop mutates) — only the bill moved | `4cb5fc9` | `tests/test_night_profile_derivation.py::test_the_supplier_next_to_the_hammer_is_drawn_first`, `::test_coverage_is_the_same_either_way`, `::test_a_small_tribute_does_not_order_the_whole_account` |
| Material draws were still priced at the hub hop | `night_profile.py` — the material branch of `_destinations`, and the receiver branch | Right for a forced sender shedding into the remainder village, wrong for the draw: a village drawn to cover a receiver ships to the *receiver*. Supplier one field from its receiver and 199 from the hub was told it could ship nothing. Second site: the receiver branch booked `own − room` to shed without consulting `shed_limit` at all | `bcbdd30` | `tests/test_night_profile_derivation.py::test_a_ten_minute_haul_to_a_material_receiver_stays_shippable`, `::test_a_material_receiver_out_of_reach_is_still_reported_unmet`, `::test_a_day_plan_receiver_never_promises_more_shed_than_it_can_carry` |
| N foreign targets collapsed onto one coordinate | `web/routes/distribution.py`, the night path's tribute assembly; `ForeignTarget.route_eligible` selects them | `tribute` summed every route-eligible target's rate while `tribute_at` took the **first one's** coordinates: a 500/h ally two fields out beside a 20,000/h artifact sixty out became 20,500/h at the two-field hop. **Reordering the request body gave the opposite answer** | `25b3fe5` | `tests/test_night_profile_derivation.py::test_each_target_carries_its_own_hop`, `::test_the_order_they_are_given_in_does_not_matter`; `tests/test_night_profile_endpoint.py::test_the_far_obligation_is_not_priced_at_the_near_hop`, `::test_the_answer_does_not_depend_on_the_order_they_were_typed_in` |
| The night omitted the tribute safety margin the day applied | `web/routes/distribution.py`, the night path's `TributeTarget` | The plan path and the manual path both use `crop_per_hour × (1 + margin/100)`; the night used the bare rate, so the remainder village drained further than the profile predicted. Latent at the default margin of 0 | `fce7617` | `tests/test_night_profile_endpoint.py::test_the_safety_margin_raises_the_night_obligation_too` |
| An off-map coordinate was folded instead of refused | `services/distribution/geometry.py` — `MapGeometry._axis_delta` | It returned a negative for a raw coordinate beyond the span and `hypot` took the absolute value, so (450\|0) on a 401-wide map read as 49 fields from the centre — a five-minute haul. Load-bearing once every target carried its own hop | `a03e6bb` | `tests/test_distribution_geometry.py::test_a_coordinate_off_the_map_is_refused_not_folded`, `::test_the_widest_legal_separation_is_still_measured`; `tests/test_distribution_routes.py::test_a_target_off_the_map_is_refused_rather_than_wrapped` |
| A tribute on your own tile surfaced as unmet crop | `web/routes/distribution.py`, foreign-target validation | A Travian tile holds one village, so it is a typo — and nothing connected the unmet figure to the coordinates that caused it | `9c4c3f7` | `tests/test_distribution_routes.py::test_a_target_on_one_of_your_own_tiles_is_an_input_error` |
| A boundary breach of exactly one merchant was silent | `web/routes/distribution.py`, the whole-day merchant-boundary check | The two profiles together committed one more merchant than the fleet around the window edge, and said nothing | `804a3f1` (test-only; the check was correct) | `tests/test_whole_day_execute.py::test_a_breach_of_exactly_one_merchant_is_reported`, `::test_a_fleet_with_exactly_enough_says_nothing` |

### The mutation-audit tail

Two mutation sweeps (batch 1, and batch 2 over 57 mutants of `optimizer.py`,
`schedule.py`, `storage.py` and `web/routes/distribution.py` at 72 % kill)
produced fifteen confirmed survivors — behaviour no test would have noticed
changing. All fifteen were killed red-first in backend round 3, plus one
narrowed case from §F of that report. The ones worth naming as *classes*:

| survivor | module:symbol | what could change unnoticed | fix | the test that pins it |
|---|---|---|---|---|
| Unbounded accrual | `services/distribution/storage.py`, the NPC reservoir | The daily allowance accumulated without bound over the settling days the replay runs — the infinite reservoir again, wearing a rate | `ffc5ba6` | `tests/test_distribution_storage.py::test_a_budget_nobody_spends_stops_at_one_days_allowance`, `::test_a_part_day_accrues_at_the_rate` |
| A floor that funded itself | `storage.py`, the conversion feedstock floor | Conversion ate the buffer it exists to keep. Every fixture used `sources=(CROP,)`, so no test saw it | `ffc5ba6` | `tests/test_distribution_storage.py::test_a_store_sitting_exactly_on_its_floor_funds_nothing`, `::test_only_what_stands_above_the_floor_is_spendable` |
| A rate boundary flipped | `storage.py` — `NEGLIGIBLE_RATE = 1.0` | 1.0 → 500.0 changed which villages count as level, unnoticed | `ffc5ba6` | `tests/test_distribution_storage.py::test_a_rate_that_empties_a_granary_in_a_day_is_not_level`, `::test_exactly_the_warn_horizon_of_cover_is_not_yet_urgent` |
| Rounded travel time | `services/distribution/schedule.py` — `Beat.exact_arrival_minutes` | No fixture had a fractional travel time, so rounding was invisible | `81b41d9` | `tests/test_distribution_relay.py::test_the_exact_arrival_keeps_the_seconds`, `::test_the_displayed_arrival_is_the_rounded_one`, `::test_a_send_in_the_same_instant_has_not_caught_the_cargo` |
| A window that did not wrap | `schedule.py` — `_window_length`, `MINUTES_PER_DAY` | A midnight-wrapping window measured as its complement | `81b41d9` | `tests/test_distribution_planner.py::test_a_window_that_wraps_midnight_is_as_long_as_it_looks`, `::test_the_default_arrival_gap_is_the_one_a_caller_gets` |
| An off-by-one cadence cap laundered by a fallback | `services/distribution/optimizer.py`, the cadence cap and its empty-set fallback | An exclusive cap produced an empty set, which the fallback then filled with every cycle | `1d72f0f` | `tests/test_delivery_cadence.py::test_a_cap_inside_the_range_is_the_cycle_that_comes_back`, `::test_a_cap_that_admits_something_admits_only_that` |
| A weight with no observable effect | `optimizer.py` — `SINK_ROUTE_WEIGHT = 4` | Dropping it to 1 split a tribute across suppliers at the same merchant cost, so nothing failed | `968b021` | `tests/test_distribution_optimizer.py::test_the_tribute_is_served_by_one_supplier`, `::test_without_the_weight_the_same_account_splits_it` |
| An unentered validation branch | `ExecuteRequest._segments_are_coherent`, the attendance-required arm | The whole-day fixture never set `stock_floor_fraction`, so the branch was never entered | `804a3f1` | `tests/test_whole_day_execute.py::test_a_stock_floor_with_no_attendance_is_refused`, `::test_a_stock_floor_with_attendance_on_every_segment_is_accepted` |
| A crop draw counted as converted material | `web/routes/distribution.py`, the NPC store deltas | The granary was credited and a feedstock store paid for it | `804a3f1` | `tests/test_distribution_npc.py::test_the_granary_is_credited_the_draw_in_full`, `::test_no_feedstock_store_pays_for_it` |
| Trim multiplicity and half-open hours | `web/routes/distribution.py` (trim) and the window predicate | Both were already correct; the mutants proved nothing pinned them | `64f6481` (test-only) | `tests/test_distribution_execute.py::test_a_second_row_at_a_wanted_minute_is_still_deleted`, `::test_the_row_the_plan_wants_is_not_deleted_with_it`; `tests/test_reconciliation_matching.py::test_a_window_owns_its_start_minute_and_not_its_end_minute`, `::test_the_night_window_wraps_past_midnight` |

**Two mutants were argued EQUIVALENT in writing rather than killed**, and that
argument is part of the map:

- `MAX_RELAY_HOPS` in `optimizer.py` is `1`, and its only read is a `< 1`
  comparison; relay depth is enforced elsewhere (the crop-shape check and the
  hub-id guard), already pinned by `TestRelayGraphStaysShallow` in
  `tests/test_distribution_relay.py`. Raising the constant to 2 changes nothing
  reachable. The mutation report's claim that two-hop relays became possible is
  **wrong**.
- The whole-day opening count in `distribution.py`: `!= 1` versus `< 1` over the
  same set. No test was forced.

---

## H. One real crash

| module:symbol | the defect as measured | fix | the test that pins it |
|---|---|---|---|
| `POST /api/distribution/night-profile` built a `NightVillage` per snapshot village with `warehouse_capacity=v.warehouse_capacity`, where `VillageSnapshot`'s field is `int \| None` — documented as "None when the capacity page was not needed by the crop read" | `NightVillage` is an unvalidated dataclass declaring `int`, so `None` flowed into `capacity_for()` and into the night derivation: `TypeError: unsupported operand type(s) for *: 'float' and 'NoneType'`. A 500 aborting derivation for the **whole account** because one village's capacity was not fetched. Reproduced by the type audit **(audit measurement)** | already fixed at that HEAD by `14fb201`; the missing warehouse half of the refusal was added in `a053bc0` | `tests/test_baseline_reality_check.py::test_an_unreadable_warehouse_capacity_is_refused_too` |

The pattern is worth naming because it recurs: an **optional field read as
present**. `VillageSnapshot.warehouse_capacity` is legitimately `None`, and the
fix is to refuse by name rather than to substitute a figure — the same rule
`distribution.py` already applied to an unreadable crop rate.

---

## I. Accessible names

The class: two controls resolving to the **same** accessible name, or to none,
so that neither a screen-reader user nor a `getByRole` locator can tell them
apart. Wave 1 censused the real Chromium accessibility tree at 375/768/1440 and
found 17 empty names on the non-planner pages and several name collisions that
scale with the village count.

| what was wrong | module:symbol | fix | the test that pins it |
|---|---|---|---|
| Ships-to / Relays-for checkboxes were bare village names, four of each — a collision that scales 2×(N−1) | `frontend/src/pages/ResourcePlanner.jsx`, the per-village checkboxes | `a48b324` | `frontend/e2e/plannerNames.pw.js` — *"a ships-to checkbox names the row it belongs to"*, *"a relays-for checkbox does the same"* |
| N buttons all called "Clear this village's own figures"; two window editors sharing one name | `ResourcePlanner.jsx` and `frontend/src/components/DayNightPanel.jsx` | `a48b324` | `frontend/e2e/plannerNames.pw.js` — *"\"clear this village’s own figures\" names the village"*, *"the two window editors do not share a name"* |
| "Lift restriction" / "Stop relaying" — one per village, all identically named | `ResourcePlanner.jsx`, now `aria-label={\`Lift restriction for ${v.name}\`}` with the visible words kept inside the fuller name, so speech input still works | `8789c24` | `frontend/e2e/plannerNames.pw.js` — *"\"Lift restriction\" names the village whose restriction it lifts"*, *"the visible words survive inside the fuller name"* |
| 17 empty names across BuildQueue, FarmLists and AutoScout: unlinked labels, icon-only buttons, inputs named `"0"` or `"no limit"` from a placeholder | `frontend/src/pages/BuildQueue.jsx`, `FarmLists.jsx`, `AutoScout.jsx` | `3ae2471`, `d1ea15d`, `5a62dd1` | `frontend/e2e/accessibleNames.pw.js` — *"BuildQueue: add/select/remove controls each resolve to one element"*, and one case each for FarmLists and AutoScout |
| A second "Active village" control — AutoScout embedded its own `<VillageSelector/>` in the page header beside the layout's | `AutoScout.jsx` — one `VillageSelector` reference remains at `3f4d121`, and it is the comment recording the removal | `5a62dd1` | the AutoScout case in `frontend/e2e/accessibleNames.pw.js` asserts *a single* Active village |
| Nine bounded boxes marked invalid without saying why | `ResourcePlanner.jsx` — `aria-invalid` and `aria-describedby` driven off the same boolean | `ab735a0` | `frontend/e2e/plannerBounds.pw.js` — *"the refusal sends the caret to the cell that caused it"* |

Two items the wave-3 census raised as consistency gaps, both **closed at
`3f4d121`** and verified by reading the source rather than the report:

- The "Never disable" account-invalid warning is now `<span
  id="protect-miss-problem">` and is named in the input's `aria-describedby`
  beside `protect-shape-problem` (`ResourcePlanner.jsx`).
- The three plain-text controls that fell back to the browser outline now carry
  `link-action`, and `frontend/src/index.css` defines
  `.link-action:focus-visible { outline: 2px solid var(--md-primary) }` with a
  comment naming those exact three controls.

**A tab-order dispute, settled.** Wave 1 reported three backward focus jumps on
the write path. The fixer re-measured with scroll-corrected element rectangles
and found none; wave 2 re-ran it and agreed the fixer was right — the three
jumps were a measurement artefact of reading rectangles across a scroll. The
measurement now lives in `frontend/e2e/focusOrder.pw.js` (*"the write path reads
downwards at 375/768/1440"*). One **pre-existing, out-of-scope** generic name
survives: a plain `<button>Delete</button>` in the profile toolbar
(`ResourcePlanner.jsx`, `onClick={() => setConfirmDeleteProfile(activeProfile)}`),
which none of the reviewed commits touched.

---

## J. Deliberately NOT covered

Everything above is a defect in *our* code, found by reading or driving *our*
code. This section is the other kind: things the code assumes about the game
that no one has ever observed on this account. They are not bugs, they are not
fixed, and no test can pin them — a test would only re-assert the assumption.

### The four unverified game facts

| fact | where the assumption lives | why it is safe to run with | what settles it |
|---|---|---|---|
| **The trade-route DELETE response shape** | `services/trade_route_service.py` — `delete_routes`, whose docstring says **RESPONSE SHAPE, UNVERIFIED** in as many words. `_rejected_routes` reads `{"routes":[{"id":..,"error":..}]}`, which is the shape the game's own bulk-**toggle** handler uses; nobody has observed a DELETE reply on this account at all | Anything the parser cannot read becomes `unverified` and is settled by re-reading the marketplace | the `window_pruned` trace event's `status` on the first live prune. `deleted` means the shape applies; `unverified` means it does not and the delete was settled purely by read-back. Either is safe — **record which** |
| **Merchant speed** | `distribution.py` — `DEFAULT_SPEED_FIELDS_PER_HOUR = 12.0`, fed into `MapGeometry.speed_fields_per_hour`, whose own docstring marks the figure tribe-specific | Every trip figure scales linearly with it, so an error is systematic rather than erratic | one timed leg: `distance ÷ 12 × 60` minutes against the send form's stated duration, or the first real firing in `docs/26` step 5. **This account cannot test the map wrap** — every village sits well inside the half-span |
| **The Trade Office bonus slope** | `services/distribution/merchants.py` — `EUROPE2_TEUTON = MerchantModel(base_capacity=2500, bonus_per_trade_office_level=0.20)`. The base was re-read on 2026-09-02; the slope never was | Over-estimating plans cargo the merchants cannot carry — the unsafe direction | a send-form reading at Trade Office 0 (which *is* the base, directly) plus one levelled village. `docs/26` step 0.6 has the arithmetic and the reason a level-0 sample removes the three-level minimum |
| **What a short sender does** | `services/distribution/storage.py` — stated at exactly two points, `simulate_day` and `simulate_profile_cycle`, each with **ASSUMPTION** and **UNVERIFIED** in the docstring and again at the point of use. The assumption: a dispatch takes what the origin actually has and the matching arrival delivers exactly that, so cargo is conserved | Crediting a batch the origin could not fund would invent resources, and the invention resurfaces as overflow at the far end. Both replays make the *same* assumption deliberately — one modelling a skipping game and the other a partial one is how `/plan` and `/day-check` would answer one account differently | one deliberately resource-starved route, watched at the send minute and the arrival minute (`docs/26` step 5). A transfer between your own villages generates no report, so that window is the only evidence there will ever be |

Also unverifiable from here, recorded by the mechanics review: whether the
integrality relaxation bites on the real geometry (it needs each crop feeder's
distance to 01 and 03, which only the operator's snapshot holds), and whether
`PUT /api/v1/trade-routes` ever carries a `routes[]` array.

### The eight live observations the canary must produce

`docs/26-first-live-run.md` §2 lists these as the evidence the write path needs
before it is trusted. They are reproduced here only as an index — the protocol
is the authority, and each is checkable from the response, the trace and the
marketplace page:

1. `run_start` records `canary: true`, requested and resolved mode `live`, env brake open.
2. `origin_read` shows no pre-existing row from this origin to the chosen destination.
3. Exactly one create attempt and exactly one marketplace `POST` in the trace.
4. No disable, delete, restore or cargo-update request anywhere in the run.
5. Two consistent post-write snapshots — a `verified` event and no `read_back_disagreed` — holding the expected `(minute, cargo)` multiset.
6. `canary_rows_created` holds precisely the new row ids: not `null`, not unexpectedly empty.
7. The visible marketplace matches those ids on destination, cargo, dispatch minutes, cycle, fan-out and enabled state.
8. No uncertainty, attribution, refusal or early-stop line in `problems`, and the recorded row charge equals the observed fan-out.

The canary shape is a server-enforced flag, not a checklist: `ExecuteRequest.canary`
refuses the request unless every precondition holds, naming the one that failed
(`tests/test_distribution_execute.py::test_the_smallest_run_validates`,
`::test_the_flag_is_off_by_default`, `::test_a_preview_canary_is_refused`,
`::test_an_unfiltered_origin_is_refused`, `::test_a_larger_route_cap_is_refused`,
`::test_disabling_existing_routes_is_refused`, `::test_a_window_trim_is_refused`,
`::test_rows_that_already_satisfy_the_plan_are_refused`,
`::test_no_write_of_any_kind_went_out`). The page presets it
(`frontend/e2e/canaryRun.pw.js`, 15 cases).

### Not in scope for this document at all

- **Every write path other than the planner.** Farm-list sending, the build
  queue, auto-scout, the raid optimizer and reports have had the accessible-name
  pass (section I) and nothing else — no failure-class audit, no mutation sweep,
  no contract audit. Their silence, activity billing, error handling and
  convergence are unmeasured.
- **Auth, session, rate limiting and CORS.** `web/app.py` binds `0.0.0.0` in
  both server configurations, adds a `SecurityHeadersMiddleware` and a
  `CORSMiddleware` allowlisting three localhost origins. Not reviewed here.
- **Schema drift.** `init_db` in `web/models/db.py` calls
  `Base.metadata.create_all` plus a hand-maintained `_COLUMN_BACKFILLS` dict.
  See `docs/28-production-readiness.md` §2 — the risk is operational, not a
  planner defect.
- **Performance, LCP/CLS, and the remaining raw-hex/palette violations** in the
  frontend. `frontend/CLAUDE.md` owns the UI Definition of Done.

---

## Open operator decisions

Each of these is a question the code cannot answer, and each one changes what a
run does. They are listed with what is true today, so that "do nothing" is a
visible choice rather than a default.

### 1. The latency target — decide before the first live run

**Today:** the page sends `max_latency_hours` on **no** path (`036ea19`), so
`PlanRequest.max_latency_hours = 2.0` applies, clamped per profile window in
`_plan_account` and never loosened. The figure that actually bound each plan is
now reported as `latency_target_hours`, and is `null` on an overnight profile.

**The measurement,** same night plan, from the repo's own sweep: an 8 h target
planned 46 routes / 120 merchants; a 2 h target planned 48 / 135 — about 12 %
more merchants.

**The options,** each a one-line change:

1. Keep the standing 2.0 h.
2. Send 24 on segmented requests, so each segment is clamped to its own window
   and the clamp never binds. This is the tautology the reachability audit
   called inert; choosing it deliberately is different from arriving at it by
   accident, but the effect is the same.
3. Expose a latency-target control on the page.

### 2. What `route_eligible` should mean

`ForeignTarget.route_eligible` is one boolean today, read in three places in
`distribution.py`. It currently conflates cases the game treats differently:
your own Wonder-of-the-World village, an alliance Wonder, and an alliance
artifact village. Whether these need separate handling — and therefore separate
inputs — is an operator question about how this account plays, not a code
question.

### 3. Phased-row cadence

Unresolved. Whether the fan-out within a cycle should be phased across the
window rather than departing on the cycle boundary, and what the operator wants
to type to say so.

### 4. Reinforcements versus garrison

The planner has no input distinguishing troops stationed at a village from
troops reinforcing it, and the two consume crop identically while belonging to
different owners. What the operator wants to enter is undecided.

### 5. pyright or mypy — pick one

**Today, neither runs.** `.github/workflows/ci.yml` runs `ruff check`,
`ruff format --check`, `pytest -x -v` and a blocking `pip-audit`, and no type
checker at all. Meanwhile `pyproject.toml` declares `mypy>=1.5.0` as a dev
dependency **and** a strict `[tool.mypy]` block — `disallow_untyped_defs`,
`disallow_incomplete_defs`, `check_untyped_defs`, `warn_return_any` — that
nothing executes. The type audit measured mypy at 480 errors (234 of them
`no-untyped-def`) against pyright 1.1.411 in basic mode at 290 diagnostics, of
which 149 were BeautifulSoup stub noise in three parser modules.

The gate the review ran with, and recommends, is **pyright basic** scoped to
`src/travian_api/`, non-blocking on `parsers/` and `defense_cache.py` until the
bs4 usage is tightened, with `reportIncompatibleVariableOverride` suppressed
project-wide (five hits, all the idiomatic Pydantic subclass-narrows-Optional
pattern). The backend rounds tracked a baseline of 42 errors, then 23 after
`00de74d` narrowed a `TradeRouteService | None`. Neither checker is wired into
CI, so the baseline is enforced by hand.

**The decision is which one, not whether**: the `[tool.mypy]` block is dead
configuration as long as nothing runs it, and dead configuration is how a
future contributor comes to believe a gate exists.

### 6. Two lesser ones, for completeness

- One comment-only commit on this branch lacks the `Co-Authored-By` and
  `Claude-Session` trailers. It is not HEAD; rewriting it means a rebase after
  every writer has stopped.
- The xdist shared-database race: `tests/conftest.py` repoints
  `TRAVIAN_DB_PATH` before anything imports, and its isolation helper returns
  early when the variable is **already** set — which it is, in every xdist
  worker, inherited from the controller. So all eight workers share one SQLite
  file, and two modules starting an app lifespan concurrently can race
  `create_all` and fail with `table users already exists`. Observed once. The
  fix — keying the temporary path on `PYTEST_XDIST_WORKER` — is untaken, so
  treat an `already exists` failure under `-n 8` as this and not as your change.
