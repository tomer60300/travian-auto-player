# Part V — Late-game context at day ≈ 161

## V.1 Artifacts, from a logistics angle

Artifacts release mid-server. Three tiers: **Small** affects one village (Treasury L10), **Large** and **Unique** affect the whole account (Treasury L20). At most three owned active — one account-scope plus up to two village-scope; a village artifact overrides an account-scope one within its village; same-type effects don't stack. Activation takes 24h after capture.

Capture requires a **normal attack** (not a raid) with a surviving hero, sent from a village whose Treasury is of adequate level and empty. Natar artifact villages have a L20 Treasury regardless of tier and build walls only to level 1, so rams aren't needed — roughly 55 non-upgraded catapults with one target set to Treasury suffices. The artifact appears in the **sending** village.

**The one that matters most for this tool: Diet Control.** Reduces troop crop consumption by 25% to 50% depending on tier (support: small/village halves it, large/account three-quarters, unique/account halves it).

**What to look for:** a Diet Control artifact **changes every crop figure in the planner.** Because net crop is derived from the live granary countdown rather than computed from unit counts, the planner picks this up automatically — another payoff from that design choice. But any code path that *computes* crop from troop counts would need the artifact factor, and any cached crop rate becomes stale the moment one activates or is lost. Also relevant: **storage** artifacts change capacity, and **speed** artifacts (Titan's Boots) affect troops only, never merchants.

## V.2 The World Wonder pipeline

14 Natar WW villages exist. Once a player holds one and construction plans release, the race to level 100 begins; reaching 100 wins the server.

**WW village special rules** (VERIFIED, support "World Wonder"), all of which matter to a logistics tool:

- **No gold features** — no NPC merchant, no instant build. So resource *ratios* cannot be fixed on site.
- **No Treasury, no culture points.**
- **Build times 50% shorter**; troops consume **50% crop** once plans appear.
- **Great Warehouse and Great Granary are allowed** — 3× normal storage.
- **Cannot be conquered once the WW reaches level 1.**
- **The hourly pushing limit is waived** — unlimited resources may be sent in.

**This is why NPC-exchange villages exist.** The WW consumes enormous quantities in specific ratios but has no NPC to rebalance and finite storage even when tripled. So nearby villages receive raw resources, NPC-balance them to the ratio the next WW level needs, and forward. A planner supporting a WW push needs to model those villages as **ratio converters with reserved merchant headroom for the forwarding burst**, not as ordinary feeders.

Note also: the pushing-limit waiver means a WW is the one cross-player destination where an unlimited trade route is legal — directly relevant to §0.2.

## V.3 Village loss

Villages are taken by repeatedly sending a Chief/Chieftain/Senator to drop **loyalty from 100% to 0.** Each successful chief reduces loyalty by a percentage, lessened if the attacker already owns oases via the Hero's Mansion. The Teuton Brewery halves persuasion while active. **The capital cannot be chiefed.**

**Why a reviewer must care:** a persisted plan holds village ids. A chiefed sub-hub or feeder breaks the route topology and leaves the plan referencing a village that is no longer yours. Verify the tool **fails safe** — a route to a lost village should not crash, and should not keep being planned against.

## V.4 Culture points and expansion

CP accumulates as a running total, never spent. Thresholds at x1 (VERIFIED): 2nd village 2,000 · 3rd 8,000 · 5th 39,000 · 10th 251,000 · 15th 692,000 · 20th 1,397,000 · 25th 2,391,000 · **26th 2,627,000** · 27th 2,874,000.

Celebrations inject CP: a **Small celebration** grants the village's daily CP capped at 500; a **Great celebration** (Town Hall 10+) grants total daily CP across all villages capped at 2,000. **Town Hall level does not change cost or CP granted — only the cooldown.**

Relevant to the tool mainly as a source of **village-count churn**: every new village invalidates the fixture, needs a Trade Office scan, and enters the plan with no allocation. §I.8 applies.

## V.5 Account topology at 26 villages

COMMUNITY, but this is what the planner is actually planning for:

- **Capital / cropper** — often a 15c with crop oasis bonus, fields past L10, large granary. Usually *not* the hammer.
- **Hammer village** — non-capital, adjacent to the capital, Barracks + Stable + Workshop + Great Barracks + Great Stable running continuously. Deeply crop-negative. Receives crop from the capital and feeders.
- **Feeders** — fields to L10, Marketplace 20, Trade Office, no military beyond a wall. Overproduce raw resources; ship surplus onward.
- **Sub-hubs** — consolidate several feeders' output so the long haul runs with full merchants rather than many part-loaded ones. **The single biggest available merchant saving, and the piece the optimizer doesn't yet do.**
- **Defensive villages** — Spearmen and Paladins, moderate crop draw.
- **NPC-exchange villages** — hold stock, NPC-balance to a downstream ratio, forward.

Flow is hub-and-spoke: feeders → sub-hub → hammer/capital, plus any WW pipeline.

**The player-level insight a reviewer needs:** the objective is not "move resources," it is **"keep every training queue fed without a single hour of idle production."** A plan that balances perfectly on average but lets the hammer's granary hit zero at 04:00 has failed. A plan that leaves clay accumulating at a feeder that could have been sending wood has also failed — just less visibly.

---

