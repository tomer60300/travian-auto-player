# Constants register — every number, where it lives, and which way it hurts

One file so a constant check costs one read instead of the whole reference. Every
code location here was verified against the tree on 2026-09-03; a pointer that no
longer resolves is itself a finding worth reporting.

**The reference sections are authority on the game. This register is a lookup
table.** Where the two disagree, the reference part file wins — and say so, because
a drifted register is a defect in the review data.

**Read `01-operator-rulings.md` before filing any finding against a constant.**
Three of the numbers below have been ruled on by the operator, and two published
figures the reference calls wrong are in fact right in the code.

---

## Merchant physics — `services/distribution/merchants.py`

| Constant | Value in code | Location | Confidence | Wrong-direction danger |
|---|---|---|---|---|
| Merchant base capacity | **2,500** | `merchants.py:102` `EUROPE2_TEUTON` | operator ruling 2026-09-02, **contradicts** the 7,920@TO13 reading | **Over-estimate is the dangerous one** — plans cargo the merchants cannot carry, routes silently under-deliver, a feeder or hammer starves. Under-estimate merely wastes merchants. |
| Trade Office slope | **+0.20 / level**, additive on base | `merchants.py:102` | MEASURED as a factorisation, not independently pinned | see above; additive-vs-multiplicative confusion flips it |
| Alliance Commerce | **not modelled** | — | VERIFIED to exist, multiplicative with TO | Absence is safe (over-provisions). Implementing it *additively* flips it dangerous. Worked example to test against: Gaul `750 × 2.2 × 5 = 8,250` (§I.3.1). |
| Allowed repeat cycles | `(1,2,3,4,6,8,12,24)` | `merchants.py:41` `DAILY_BEAT_CYCLES` | VERIFIED (`&every=N`) | Accepting a non-divisor destroys the readable daily beat; the code keeps `ALL_CYCLES` only to *quantify* the restriction's cost, which is correct and should be defended |
| Merchant speed (Teuton) | **12 fields/h** | `web/routes/distribution.py:171` `_TRIBE_MERCHANT_SPEED` | stock, **never measured on this server** | Wrong speed inflates travel → `sets_in_flight` → merchant counts, and can flip `over_budget`. One timed send settles it. |
| Trade Office affects speed | **no** | — | VERIFIED | — |
| Tournament Square affects merchants | **no** | — | VERIFIED | Any TS/artifact/hero term in the merchant geometry path is a defect |
| Merchant reserve (flat) | **2** | `optimizer.py:96` `DEFAULT_MERCHANT_RESERVE` | design choice | Permanently-committed merchants must be subtracted **before** the reserve, or a tribute village ends up with zero idle merchants while the reserve looks honoured — quiet |
| Merchant headroom (proportional) | **0.10** | `optimizer.py:119` `DEFAULT_MERCHANT_HEADROOM` | design choice | Must **compose** with the flat reserve, not double-count it |
| `deliveries` | pinned **1** | create payload | VERIFIED it is a trip count, not a merchant count | Reading it as merchant count triple-counts the budget |

**Note on the reference's §I.3.5:** it names `DEFAULT_MERCHANT_RESERVE` and
`DEFAULT_MERCHANT_HEADROOM` as the whole story. The reserve is now also a
per-request parameter (`merchant_budget(reserve)`, `optimizer.py:225`), so a review
must check the *passed* value, not only the default.

## Cost model — `services/distribution/optimizer.py`

```
batch          = hourly_cargo × cycle_h
m_send         = ceil(batch / merchant_cap[sender])
sets_in_flight = ceil(rt_minutes / (cycle_h × 60))
pool           = m_send × sets_in_flight
```

**`pool` is non-monotonic in cycle length** — the double ceiling. The worked
counter-example (§I.3.4) is 1h→18, 2h→20, **3h→15**, 4h→21 merchants. Any code that
assumes longer cycles are cheaper, *or* that shorter ones are, is wrong. Sweep the
whole allowed set per route and never infer a direction.

## Geometry — `services/distribution/geometry.py`

| Constant | Value | Confidence | Wrong-direction danger |
|---|---|---|---|
| Map span | **401** (−200…+200) | **MEASURED, with proof in the codebase** | See `01-operator-rulings.md` §2 — this is the one where the *spec* is wrong and the code is right. Do not "fix" it to 801. |
| Wrap | both axes | VERIFIED | Distance failure is **quiet** (every real village sits inside ±200, so `min(raw, span−raw)` picks `raw` anyway and wrap protection silently disappears). `map_id_to_coords` failure is **loud and total** — it breaks route reconciliation, which is what revert depends on. |
| Span parity | must be **odd** | derived (world centred on 0\|0) | `map_id_to_coords` returns `None` on an even span rather than shifting every tile by half a field — correct, keep it |
| `useTradeShips` | pinned **false** | VERIFIED (no boats on standard Legends) | Under-uses a ship world; accepted limitation, not a defect |

`mapId = (S − y) × span + (x + S) + 1`, `S = (span − 1) / 2`.

## Raiding — `services/raid_analyzer_service.py`

| Constant | Value in code | Location | Confidence | Wrong-direction danger |
|---|---|---|---|---|
| Outgoing troop cap | **not implemented** | — | VERIFIED: 20,000 per **avatar**, account-wide | **Quiet.** Includes returning legs, own-village reinforcements and hero adventures. The tool keeps issuing sends the server refuses, and every downstream metric (last bounty, carry ratio, re-scout queue) degrades because the raids it thinks it sent never left. The only `20_000` in the tree is `MAX_EVENTS` in `execution_trace.py` — unrelated. |
| Clubswinger carry | **60** | `raid_analyzer_service.py:95` `CLUB_CARRY` | stock; measurable from any full-bag report | over-estimate → under-full bags on targets the model rates profitable |
| Axeman carry | **50** | `raid_analyzer_service.py:102` `AXE_CARRY` | stock | as above |
| Stealable fallback | **0.67** | `raid_analyzer_service.py:392` `WAREHOUSE_RATIO` | pragmatic fallback when the carry icon is not parsed | **No cranny model exists.** If scoring was calibrated on hero-accompanied raids it over-estimates hero-less ones by up to 20% of cranny capacity — quiet |
| Farm list entry cap | **100** | — | VERIFIED, hard server-side | The 99 figure circulating in older guides is an off-by-one from a 0–99 display. Retire it. |
| Siege in farm lists | must be **excluded** | — | VERIFIED | In a raid catapults do not fire and rams do not damage the wall — siege adds no carry and just exposes slow expensive units |
| Bag / sack percentage | carry-**utilisation** | — | VERIFIED semantics | **Backwards-reading is self-reinforcing**: read as "fraction of target taken", resizing logic keeps growing sends against targets that are already fully drained |

Teuton carry, stock: Clubswinger 60, Axeman 50, Spearfighter 40, Paladin 110,
Teutonic Knight 80, rams/catapults 0.

Cranny: Roman/Teuton L10 hides **2,000 per resource**, Gaul **×1.5** (3,000).
Teuton hero dip is **×0.8 on the target's protection**, requires the hero to
physically travel with that raid, and is irrelevant on oases (no cranny on the tile).

## Troop travel — different from merchants

```
dist ≤ 20:  time = dist / base_speed
dist > 20:  time = 20 / base_speed + (dist − 20) / (base_speed × (1 + TS + boots))
```
then × artifact, pennant, standard. **Tournament Square and Boots add together;
everything else multiplies.** TS maximum is **DISPUTED** (+200% current docs vs
+500% legacy) — verify with the in-game Rally Point simulator before encoding.
A hero moving with troops travels at the **slowest unit's** speed.

## Buildings

| Rule | Value | Confidence |
|---|---|---|
| Simultaneous build slots | Gaul/Teuton **1**, Roman **2** | VERIFIED |
| Travian Plus | +1 queued waiting-loop slot; resources required at queue time | VERIFIED |
| Master Builder | **1 gold per use**, charged when the build starts | VERIFIED |
| Cancel refund | L1 build **full**; L≥2 upgrade **partial** | VERIFIED |
| "90 seconds for a full refund" | **DISPUTED for T4** — the 90s window in the Oct 2024 notes is *troop training*, a different mechanic | do not encode either without an in-game check |
| Instant complete | free under 5 min remaining; **not available in WW villages** | VERIFIED |
| Trade Office prereq | Marketplace 20 + Stable 10 | VERIFIED |
| Brewery | Teuton, capital only, Granary 20 + Rally Point 10 | VERIFIED |

**Brewery interaction worth a finding:** while a Mead Festival runs, chief
persuasion is halved and catapults hit random targets only. Any tool that automates
chiefing or targeted catapult strikes must not run the Brewery through it.

**The gold guard:** the tool never spends gold unless explicitly opted in. Each
gold-touching path is a **separate** opt-in surface — Master Builder, instant
complete, NPC, production bonus — and a missed one spends real money.

## Reads that lie

| Read | The trap |
|---|---|
| Net crop | must be `l4`, **never** `l5` / free crop. No path may return `0` instead of `None`. |
| `merchants_total` | the **second** number of the free/total cell |
| In-transit resources | not in the destination warehouse until arrival. Read stock once, reconcile in-transit **exactly once** — a planner that reads stock *and* counts incoming routes double-counts. |
| The resource bar | animates client-side off a JS timer; the authoritative amount is server-side. The bar leads or lags. |
| Capacity tab | contaminated by trade in flight |
| Aggregate overviews | lag per-village state |
| Mobile vs browser | presentation and rounding differ, parity is undocumented. Target the browser DOM. The 20,000 cap, raid-vs-attack semantics and entry independence are server-side and identical — only the UI differs. |
| Hand-maintained fields | drift. The design doc's own Trade Office table had two villages listed at TO 0 and TO 11 that are both actually TO 13, so every merchant figure derived from it was wrong independently of the formula. |

## The rule that generates most constant findings

**§0.1: this server is not stock, and it is proven.** Every published capacity or
production figure must be read live from the game or be a configurable parameter —
**never a code literal**. Capacity already lives behind one injectable
`MerchantModel` with `calibrate()`; that seam is correct and nothing else in the
planner may hardcode a capacity. When you find a stock value hardcoded elsewhere,
that is the finding, and the fix is the seam, not a better literal.

### How to apply that rule without producing sixty findings

Read literally, "never a code literal" would have you file against every unit carry,
speed, cost, attack and defence value in the tree — roughly sixty findings, all with
currently-correct values. That is not the intent and it collides with "do not
manufacture findings". Apply it this way instead:

- **One finding per missing seam, not per literal.** Name the constants that share a
  seam, name every file that copies them, and name the shape the fix should take.
  The merchant path is the worked example of the right shape: one injectable model
  with `calibrate()`, everything else reading from it.
- **Duplication is the defect, not the value.** N independent copies of a game
  constant means the first measured reading reaches one of them and silently leaves
  N−1 stale. Say how many copies and where.
- **A literal is only a finding on its own** if it is wrong, or if it is in the
  dangerous direction (over-estimating capacity or carry), or if a live reading of it
  already exists in the code and is being ignored in favour of the literal.

A correct literal behind a correct seam is not a finding at all.

---

## Known gaps in this register — ask, do not fill them in from memory

These are values the reference does not carry. **Do not supply them from general
knowledge of Travian** — this server is provably non-stock (§0.1), so a remembered
figure is worse than an absent one. Where a review needs one, say which gap you hit
and where the operator can read it, and cap your severity accordingly.

| Gap | What it blocks | Where to read it in-game |
|---|---|---|
| **Unit defence and attack values** (the register stops at *carry*) | Whether a defender whose unit id is missing from a defence table contributes zero — and therefore whether a "zero losses" fast path is safe. This is the highest-value gap: it turns findings from "please check" into decided. | Rally Point / Barracks unit info states attack and both defence values per unit |
| **Trapper mechanics** — traps per level on **T4**, whether raid-vs-attack matters, whether trapped troops are recoverable | Any judgement about sending small parties at Gaul targets. §VI.8 gives only a rule of thumb ("below roughly 100 clubs, don't engage a Gaul at all"). Note that any figure sourced from Travian **Kingdoms** is discarded per `00-how-to-use.md`. | The Trapper building info states traps per level. Gaul-only, so it is only reachable against Gaul targets. |
| **Direct scout-send mechanics** — whether one scout suffices, what a "defences" scout returns versus a "resources" scout, whether a scout that dies still produces a report | The whole defence-scan phase of the farm builder. §II.3 covers scout-only *farm-list entries* only. | Send one scout at a known target and compare the report to a resources scout of the same target |
| **Oasis stock after clearing** — is an animal-guarded oasis's ~1,000–2,000 per resource lootable once the animals are dead, or is only the animal yield (160–800 per clear) available? | Whether skipping every animal-guarded oasis forfeits ~4,000–8,000 per tile or a few hundred. Changes the oasis raider's income model by an order of magnitude. | Clear one known animal oasis and compare the battle report's bounty against §II.8's animal-yield table |

## A possible defect in this review data itself

**§II.9 says the no-loss report suppression is "an account-level preference, not
per-list". The game's own GraphQL `FarmList` type exposes `onlyLosses` per list**, and
the codebase's `create_farm_list` posts it per list. Those cannot both describe one
setting. The code's behaviour is safe either way — it never enables suppression — but
one of the two positions is wrong, and if it is the reference then §II.9 needs
correcting. **UNRESOLVED**: one look at the farm-list settings UI settles it. Do not
pick a winner.
