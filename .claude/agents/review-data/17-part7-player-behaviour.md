# Part VII — How players actually play

A reviewer can verify every formula in this document and still miss the point, because most of what makes an account good is *decision quality between sessions*, not arithmetic. This part is what a mechanics-literate player knows about behaviour — the shape of a day, what motivates each action, and which decisions a naive script gets wrong even with perfect mechanics.

## VII.1 The game is about idle time, not resources

The single organising fact: **production is continuous and storage is finite.** Every hour, four resources accumulate whether or not you are present. Nothing else in the game is continuous — troops train only if queued, buildings rise only if started, raids fly only if sent.

So the skill is not acquiring resources. It is **never letting anything idle**:

- Resources sitting in a warehouse are a mistake (they should be troops, or a building, or in transit).
- A full warehouse is worse than a mistake — production above cap is destroyed.
- Troops sitting at home are a mistake (they should be raiding).
- An empty build queue is a mistake.
- An empty training queue is the worst mistake, because army strength compounds.

Every habit below exists to shrink the window in which something idles. A player's mental accounting is in **resources-per-hour forgone**, not in resources held — which is exactly why the planner works in hourly rates.

## VII.2 A day in the life

Roughly, on an account this size:

| Session | Length | What happens |
|---|---|---|
| **Wake-up** | 20–40 min | Reports first — was I attacked overnight, did anything die? Then spend down whatever filled up, relaunch farm lists (troops sent before bed are returning now), refill build and training queues, check incoming attacks |
| **Midday check-ins** | 5–10 min, opportunistic | Usually from a phone. Send farm lists, top up queues, nothing strategic |
| **Evening** | 1–2 h | The real session. Planning, trade-route adjustment, alliance coordination, launching operations, the analysis work |
| **Pre-sleep** | 15–30 min | The highest-skill moment of the day — see below |
| **Overnight** | — | 6–9 hours where the account runs on whatever was set up |

Note the asymmetry: the *shortest* session (pre-sleep) carries the most consequence, and the longest (evening) is mostly discretionary. A tool that optimises the evening session and ignores the pre-sleep ritual is optimising the wrong window.

## VII.3 The pre-sleep ritual

This is the routine that separates competent accounts from good ones, and it's worth spelling out because the repo already encodes part of it numerically without necessarily naming it.

Before logging off, a player:

1. **Spends down every warehouse** so nothing overflows during the sleep gap. Not to zero — to a level that will be near-full but not over at wake-up.
2. **Queues the longest builds**, so construction spans the gap rather than finishing at 02:00 and idling.
3. **Sends troops out on long raids** timed to return near wake-up. This does three things at once: earns loot, keeps troops out of a village that might be attacked, and moves them to group 4 in the starvation order.
4. **Checks crop will last the night** in every village, especially wherever armies are stationed.
5. **Sets the hero and troops to evade** if an attack is expected.
6. **Confirms trade routes will not overflow a destination** while nobody is consuming.

**This is what `night_profile.py` is actually modelling**, and why its docstring insists on measuring from "the BASELINE the operator leaves behind before sleeping" rather than from whatever a snapshot catches. That is not a technical preference — it's the ritual made numeric. A mid-afternoon reading is mid-accumulation, so a profile built from one goes stale within the hour; the observed figure was 34 of 100 allocations moving in a single hour, three changing sign. **Emptying before sleep is what makes the baseline reproducible**, and reproducible is what lets one profile hold for weeks.

A reviewer should understand that the "empty before sleeping" instruction is not housekeeping advice bolted onto the tool. It is the precondition that makes the night plan *stable*.

## VII.4 Duals and sitters

Serious accounts are run by two or three people in different timezones, precisely to eliminate the sleep gap. This is legitimate and built into the game (sitters, duals). It's the reason "24/7 training queues" is achievable by human players at all, and it's the standard against which a large account's activity is judged.

Relevant consequence for the tool: **the account may be touched by someone other than the operator, at any time.** A plan that assumes it is the only actor will be wrong — routes get edited by hand, resources get spent, troops get moved. This is exactly why `run_history.py` is careful that a trace never learns "whether an operator changed something by hand in-game after the run ended" (§I.5.3), and why re-planning must be a diff rather than an assertion of intended state.

## VII.5 Players satisfice; they do not optimise

This matters for how the tool's output is received, not just what it computes.

No player runs a min-cost flow. They use **round numbers, rules of thumb, and good-enough ratios**: "roughly 70/30 axes to Teutonic Knights by crop," "keep two merchants free," "fields to level 10 then stop," "don't bother below 100 clubs." They eyeball a village's stock and decide. They re-derive nothing.

Two implications:

**A globally optimal plan the operator cannot intuit will be distrusted and overridden.** This is why showing the cycle sweep next to each route matters — the operator did this by hand, and seeing the non-monotonic curve is what makes a surprising answer believable. Explainability here is not decoration; it's the difference between a plan that gets applied and one that gets second-guessed into a manual edit.

**The operator's own annotations will be ambiguous, and guessing is worse than asking.** A number written in a spreadsheet may be net or gross, target or gap, per-hour or per-send. The right response to an ambiguous figure is to flag and exclude it, not to assume — because the assumption propagates silently through every downstream calculation while the flag costs one question.

## VII.6 What day ≈ 161 looks like behaviourally

The phase changes what players spend their attention on, and the tool should be read against the current phase rather than a generic one.

At day 161 — artifacts released, World Wonder phase approaching or begun:

- **Farming is background income maintained by habit.** It still matters, but it's no longer where the day's thinking goes. Farm lists get sent reflexively; they get *re-optimised* rarely.
- **Attention is on alliance coordination**: defensive calls, artifact holding, WW planning, hammer readiness. These arrive at unpredictable times and override personal plans.
- **Logistics is the dominant personal workload.** Feeding continuous training across a large village count is the daily grind, and it's the part that scales worst with village count — which is precisely why this tool's largest subsystem is the resource planner.
- **Defensive calls come at random hours** and consume merchants and troops with no notice. This is a real argument for the merchant reserve being generous rather than tight (§I.3.5): a village with zero free merchants cannot answer a call.
- **Operational security matters.** Players don't announce targets, and hammer locations are concealed. Anything the tool logs or exposes should be considered in that light.

## VII.7 Decisions a player makes that a naive script gets wrong

This is the most useful list in this part for a code reviewer. Each is a case where the mechanics permit an action and a good player still declines, or where the player acts on information the script doesn't consult.

- **Won't send a short farm list right before bed.** Troops would return in 40 minutes and then idle for eight hours. A player sends the *longest-distance* list instead, so the return lands near wake-up. A script that just fires every list maximises sends and minimises value.
- **Won't fill a warehouse to 100% before logging off**, even though it's "efficient" storage-wise.
- **Prunes a farm that grew.** Population went up, defenders appeared, a report came back yellow — the player removes it without being prompted. A script keeps sending until losses accumulate.
- **Stops everything when a serious attack is incoming** and reorganises: troops out, resources spent down, defence called. No routine continues.
- **Won't raid a Gaul with a small party** regardless of what the profitability model says, because trappers eat small parties.
- **Holds merchants back** because "something might come up" — which is a correct read of a game where alliance calls are unpredictable, not conservatism.
- **Checks the alliance forum before launching anything**, because coordination overrides local optimisation.
- **Won't run the Brewery during a conquest window** (§IV.2) — persuasion halved, catapults random. A script that keeps the attack bonus on permanently would silently sabotage a chiefing operation.
- **Accepts a worse plan that is easier to verify.** Given a 15-merchant plan they don't understand and a 17-merchant plan they do, many players take the 17.

**What to look for:** does any of the tool's automation consult *time of day*, *incoming attacks*, or *recent report outcomes* before acting? If a loop's only inputs are "troops available" and "target list," it will make every one of the above mistakes while reporting success.

## VII.8 What normal activity looks like — descriptive only

**Scope note.** This exists so a reviewer can judge whether the tool's modelled pacing is *plausible as gameplay* and whether its decisions resemble a competent player's. It is not, and must not be used as, guidance for evading anti-cheat or defeating detection heuristics. Travian does not publish detection heuristics, automation violates the Terms of Service, and account suspension remains possible at the operator's risk regardless of access patterns.

Real activity is **bursty and diurnal**, as Part VII.2 describes: clusters of actions during waking sessions, a multi-hour gap with no actions, farm-list sends clustered when troops return (roughly every 20–90 minutes while active, never while asleep), and build activity spiking when timers finish. Session lengths run tens of minutes rather than hours of unbroken uniform action. Duals shift the pattern to two or three overlapping diurnal cycles rather than removing the structure.

Perfectly uniform, round-the-clock, to-the-second-periodic behaviour with identical intervals and no gap does not resemble human play. The legitimate review question is whether the scheduler produces a plausible session-and-sleep structure, and whether its per-action decisions match §VII.7.

---

