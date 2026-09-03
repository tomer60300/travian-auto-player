# Part 0 — The three things to check before anything else

## 0.1 This server is not stock, and it is proven

**MEASURED:** a Trade Office level 13 village on this account carries **7,920 resources per merchant**. That is exactly `2200 × (1 + 0.2 × 13)`. Stock Teuton values (base 1000, +10% per Trade Office level) would predict **2,300**. So Europe 2 runs both a raised merchant base and a Roman-slope Trade Office on Teutons.

The consequence is general, not local: **every published constant must be treated as possibly modified.** Any capacity or production figure in the codebase must be read live from the game or be a configurable parameter — never a code literal. Where a stock value is hardcoded, under-estimating wastes merchants; over-estimating plans cargo the merchants cannot carry, so routes silently under-deliver and a feeder or hammer starves.

*Verify in-game:* the Marketplace send dialog states resources-per-merchant directly.

## 0.2 A possible correctness bug in the planner's foreign-tribute path

**Gold Club trade routes cannot target an arbitrary other player's village.** Valid destinations are **your own villages, Wonder-of-the-World villages, and alliance/confederacy artifact villages** — VERIFIED (support.travian.com, "Trade Routes").

The planner models a `ForeignTarget`: a fixed hourly crop obligation to an external destination, with a `paying_village` and permanently-committed merchants (profile §7.3, exercised by `test_tribute_supplier_choice.py`). If that destination is an ordinary village belonging to another player, **no route can be created to it at all.** The planner will size a merchant budget and emit a route the execute path physically cannot write.

Two follow-ons:

1. **Pushing protection applies to cross-player sends.** The automatic resource-sharing limits (Connection Types 1/2/3, rolling 7-day balance) cap how much can move between different players. A nominal 50,000 crop/hour obligation could be silently capped regardless of available merchants — so even the manual-send fallback is constrained. **The WW village is the explicit exception: its hourly limit is waived** (VERIFIED, support "World Wonder"). Alliance artifact villages are not obviously waived.
2. **If the destination is a WW village, everything is fine** — routes are legal and unlimited.

**What to look for:** does anything between allocation and execute classify the target's *type*? If `ForeignTarget` is just a coordinate pair plus an hourly rate, the planner cannot tell the legal case from the impossible one.

**Direction of danger: severe and quiet.** The plan reads as feasible, the merchant budget is committed on paper, and either the write fails or the obligation is under-delivered by pushing limits while the tool reports it satisfied.

## 0.3 The 20,000 outgoing-troop cap is not implemented

**VERIFIED** — support.travian.com "Battle Mechanics": there is a limit of 20,000 outgoing troops **per avatar**, and it includes attacks, raids, adventures, reinforcements, and any other movement. New raids cannot be sent once the limit is reached.

Critical details a reviewer must hold:

- **Account-wide, not per village.** "Per avatar" means the whole account shares one counter.
- **Returning troops still count** while in transit. A raid that has hit and is walking home occupies the pool until it arrives.
- **Your own reinforcements moving between your own villages count.** So do hero adventures.
- When the cap is hit, the farm list displays an error rather than dispatching.

**The codebase does not track this.** The only `20_000` anywhere is `MAX_EVENTS` in `execution_trace.py`. Nothing in farm-list send-all, the oasis raider, or auto-scout maintains an account-wide in-transit count. On a 26-village account running send-all across many lists, this is the gap most likely to produce dispatch failures the tool misreads as something else — a skipped entry attributed to insufficient troops when the real cause is the global cap.

**Direction of danger: quiet.** The tool keeps issuing sends that the server refuses, and every downstream metric (last bounty, carry ratio, re-scout queue) silently degrades because the raids it thinks it sent never left.

---

