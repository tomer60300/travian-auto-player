# 29 — The raiding model: what is deferred, and what it costs to leave it

Not a priority. This file exists so the next person to open the raiding code —
including a future me — starts from what was already established rather than
rediscovering it. Everything here was found by review on 2026-09-05/06 and
deliberately left; nothing in it is a bug report against code that shipped.

The one thing already done is the vocabulary: `models/unknown_reason.py` gives
every reason a figure could not be established its own named code, a guard that
raises rather than computing with one, and a serialiser that puts the name on
the wire. The items below are the places that vocabulary has not reached yet.

## The decision everything else waits on

**May three loss-free raids stand in for a defence scout?**

The live decision script answers a qualified yes:

```python
DEFENSE_PROXY = {"no_loss": 10, "some_loss": 200, "all_dead": 1000, "unknown": 0}
```

Note what zero means there: *unknown*, the never-scouted sentinel. A target you
have raided without losses is worth **10** — a small number, not an empty one —
and the script requires **ten** raids plus a no-loss icon before it treats a
target as comfortably farmable.

An abandoned draft (preserved locally as the tag
`archive/recovered-stash-2026-09-06`, never merged) answered an unqualified yes:
three battle reports whose attacker losses summed to zero set
`defense_value = 0.0` and `defenders_known = True`.

Three things made that unsafe, and any future version has to solve all three:

1. **The input cannot support the claim.** `attacker_losses` is `{}` both when
   the block was read and nobody died, and when the block could not be read at
   all. An empty dict sums to zero. Three unreadable reports were therefore
   indistinguishable from three flawless raids. *Half-solved*: the parser now
   returns `None` for an unread losses row and carries
   `attacker_losses_unknown`, so the distinction exists — nothing consumes it
   for this purpose yet.
2. **The claim could never be withdrawn.** It was timestamped with
   `last_raid_time`, which is only assigned inside the branch that runs when a
   scout report exists. For a never-scouted village — exactly this population —
   it stays `None`, and the staleness check requires a real value. The zero was
   permanent.
3. **Zero is not a small number here.** `if defense_value <= 10.0: return 0`
   collapses the troop requirement to carrying capacity alone. The register
   records a 40-Tiger target at 534 clubswingers; the draft emitted it as
   roughly ten.

**If the answer is yes**, the honest implementation needs: a parse that
separates "no losses" from "no data" (done), a floor that never asserts zero
(use the proxy's 10, or a named code), and an expiry that reaches never-scouted
targets. Build it on `UnknownReason`, and give it its own reviewed change.

## Deferred items, each with a TODO at the code site

| # | Where | What | Why it was left |
|---|---|---|---|
| 1 | `raid_analyzer_service.py`, `reconstruct_state` | An unread **defender**-loss row reads as "nobody died" and overstates the surviving garrison | Same defect as the attacker side, but it errs **safe**: it makes a target look better defended than it is, so the tool sends more troops, not fewer |
| 2 | `raid_analyzer_service.py`, both scoring paths | `C_scout = 0.5` fabricates a confidence when the scout age is unknown | The last fabricated figure in the family the reason codes replaced. Changing it moves every score, so it needs a ruling, not a patch |
| 3 | `models/unknown_reason.py` | `STALE_BEYOND_HORIZON` has no producer | The only site that fits (`_phase_4_score`'s staleness branch) currently files a re-scout **and still scores the target** — "stale data is better than none". Making the reason explicit there means refusing, which is an outcome change. `test_stale_beyond_horizon_has_no_producer_yet` keeps the gap deliberate rather than letting it be filled quietly |
| 4 | `raid_analyzer_service.py`, `cli.py`, `scripts/raid_optimizer_diff_v3.py`, `frontend/src/utils/raidOptimizer.js` | **Four unsynchronised copies of the unit table, already drifted twice** | Measured: Teutonic Knight cost **1395** vs **1525**; Paladin attack **50** vs **55**. Unifying them is a real change with real blast radius across three languages of caller |
| 5 | `raid_analyzer_service.py`, `UNIT_DEF_TABLE` | Eleven unvouched defence values (`u13` plus the whole `u31`–`u40` nature block) are **withdrawn**, not fixed | Per the operator's standing ruling: don't support troops whose values we cannot vouch for. `UNVOUCHED_DEFENDER_IDS` refuses them. Confirming the real figures needs one look at the Barracks page |
| 6 | `raid_analyzer_service.py`, `req_no_cas` | The defence table is **infantry-only** and is applied to cavalry; the hero's strength is added at **every** target at once | Both were found in the abandoned draft's version of this function. The shipped version predates them, so this is a latent modelling gap rather than a regression — but it bites the moment the hero inputs are filled |

## What was already fixed, so nobody re-opens it

- Absence is no longer zero for the **stealable amount, cranny, wall and
  traps**: the analyzer computes `max(0, total − cranny)` and *refuses* when it
  has neither figure, rather than falling back to a fraction of stock. The
  `WAREHOUSE_RATIO = 0.67` fallback is deleted; do not reinstate it.
- **A resources-only scout carries no building row**, so those targets are now
  refused with "scout defences before committing troops" rather than scored off
  assumed zeros. This narrows what the analyzer recommends, on purpose.
- Run logs (`*.jsonl`) are gitignored. They carry real village ids, coordinates
  and login events, and this remote is public.
