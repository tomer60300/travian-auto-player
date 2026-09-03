# Review data — what to load, and in what order

This directory is the `travian-reviewer` agent's knowledge base. It was one 97KB
file; it is now split by review domain so a narrow review costs a narrow read.

**Never load the whole directory.** A distribution-planner review needs about half of
it; a farm-list review needs a quarter. Reading everything wastes the context you
need for the actual code.

## Always, before anything else — 3 files, ~7KB

| Order | File | Why it is mandatory |
|---|---|---|
| 1 | `00-how-to-use.md` | the three review layers and the confidence-tag vocabulary |
| 2 | `01-operator-rulings.md` | **stops you filing findings against correct code.** Two entries are exactly that trap |
| 3 | `10-part0-first-checks.md` | the three live gaps: non-stock server, foreign-tribute legality, the 20,000 cap |
| 4 | `18-part8-cross-cutting.md` | **the reads that lie, and the ranked account-killers.** Mandatory because almost every real finding on this codebase is a read that lies — a `0` that means `None`, an average that is really a lower bound, an order the code does not control — and because the severity rule tells you to rank an uncertain finding against §VIII.2's list, which you cannot do without having read it |

Add `02-constants-register.md` whenever a number is in play — it is a lookup table
so a constant check costs one read instead of the whole reference.

## Then, by what you are reviewing

| If the change touches | Load | Repo paths it covers |
|---|---|---|
| Distribution planner, allocation, optimizer, schedule, execute/revert, storage safety | `11-part1-planner.md` | `services/distribution/**`, `web/routes/distribution.py`, `services/trade_route_service.py` |
| Farm lists, raiding, oasis raider, raid analyzer, bounty scoring | `12-part2-raiding.md` | `services/raid_analyzer_service.py`, `services/farm_list_service.py`, `services/farm_builder_service.py`, `services/oasis_raider_service.py`, `services/rebalance_planner.py`, `services/target_aggregate_service.py`, `parsers/report_parser.py`, farm-list routes — **and `scripts/raid_optimizer_diff_v3.py`, which is where most of the raiding *decisions* actually live** (≈3,800 lines; the two `services/` planners are its extracted helpers and say so in their docstrings). A raiding review that skips `scripts/` misses the composition tables, the troop budget and the wave caps. |
| Map geometry, travel time, auto-scout | `13-part3-map-travel.md` | `services/distribution/geometry.py`, `services/*scout*` |
| Build queue, prerequisites, gold paths | `14-part4-buildings.md` | build-queue services and routes, `plans/*.yaml` |
| Anything about artifacts, World Wonder, village loss, culture points, 26-village topology | `15-part5-lategame.md` | planner inputs, account-level assumptions |
| A model that is *correct but too narrow* — cadence, windowing, deliver-at, per-row cargo, NPC as a mix transform | `16-part6-techniques.md` | the optimizer's decision space; read this when the tool says "infeasible" |
| Whether a decision a good player would not make got automated | `17-part7-player-behaviour.md` | any autonomous loop; scheduling; night profile |
| Final sweep before you report | `19-part9-checklist.md` | 66 numbered checks, grouped by subsystem |
| Any finding that rests on something nobody has measured | `20-open-questions.md` | caps your severity — see below |

## The load-order rule that matters

`01-operator-rulings.md` **before** `02-constants-register.md` **before** any part
file. The rulings override the register; the register points into the parts. Reading
them the other way round is how a reviewer talks itself into "fixing" a correct
constant.

## Severity is capped by confidence

The reference tags every mechanic: **VERIFIED** (official plus independent
corroboration) · **MEASURED** (confirmed against this account or this codebase's own
captures) · **PARTIAL** · **COMMUNITY** · **DISPUTED** · **UNVERIFIED**.

A finding inherits the tag of the mechanic it rests on, and:

- **VERIFIED / MEASURED** — may be P1.
- **PARTIAL / COMMUNITY** — P2 at most, and the tag must appear in the finding.
- **DISPUTED / UNVERIFIED** — **never a P1 or P2.** Report it as a question with the
  one in-game test that would settle it. `20-open-questions.md` lists six of these
  and the test for each.

A P1 that turns out to rest on a COMMUNITY figure costs more trust than the bug was
worth.

## What is deliberately not here

`_out-of-scope/` holds two bot-detection research documents. They are not part of
this agent's review logic and must not be loaded — see the README in that directory
for why. Nothing in the mechanics reference depends on them.

## Provenance

`10-` through `20-` are a verbatim, byte-for-byte split of
`travian-mechanics-reviewer-reference.md` (verified by rejoining and diffing), so
every section reference inside them — "§I.3.4", "§0.1", "§VII.7" — still resolves,
just possibly into a sibling file. The part-number prefix tells you which:
`§I.x` → `11-`, `§II.x` → `12-`, `§III.x` → `13-`, `§IV.x` → `14-`, `§V.x` → `15-`,
`§VI.x` → `16-`, `§VII.x` → `17-`, `§VIII.x` → `18-`, `§IX` → `19-`, `§0.x` → `10-`.

`00-`, `01-` and `02-` are new. `01-` and `02-` are the only files in here that
carry claims about **this codebase's current state**; if a pointer in them no longer
resolves, that is a finding worth reporting rather than a reason to guess.
