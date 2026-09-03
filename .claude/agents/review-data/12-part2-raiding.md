# Part II — Raiding and farm lists

## II.1 The outgoing cap

Covered in §0.3 because it is a live gap. Restated for completeness: **20,000 outgoing troops per avatar, account-wide, including returning legs, reinforcements between your own villages, and hero adventures.** VERIFIED. Not implemented.

## II.2 How a strong raider actually operates

COMMUNITY but well-established. A big Teuton raider runs **many lists grouped by launching village and by direction/ring**, each up to the 100-entry cap. Targets are inactives, cleared or low-defence villages, and unoccupied oases.

Troop counts per target are sized so **carry capacity ≈ expected loot.** Over-sending wastes troop-time that could be raiding elsewhere; under-sending leaves resources on the table. The October 2023 rework surfaces per-entry loot (last raid and today), carry/efficiency stats, and sort filters precisely so this resizing loop can be run from the list view.

Lists are sent whenever troops return — in practice every 20–90 minutes while the player is active, and not at all while asleep. "Start all" issues the whole list at once.

**Why this matters for review:** the tool's raid-analyzer scoring is a model of this resizing loop. If the model's carry assumptions are wrong, it will systematically over- or under-send across hundreds of targets.

## II.3 Send semantics

- **Per-entry atomicity** — an entry sends its full defined troop composition or is skipped entirely. No partial sends. PARTIAL/COMMUNITY, consistently observed. **Plan rally-point inventory per entry, not aggregated.**
- **100 entries per list**, hard server-side; the UI deactivates "add new entry" once reached. Older guides cite 99 — an off-by-one artifact from a 0–99 display. Retire the 99 figure.
- **Per-village or per-account list count** — officially silent. Community practice treats it as unbounded.
- **Sort modes**: combat result, last attack, last bounty, total bounty, next attack. VERIFIED as the canonical set. Send order follows displayed order top-down under every reasonable assumption, but **no Travian source guarantees it** — so pin the sort explicitly and read the rendered order rather than relying on internal ids or on inherited session state.
- **Scout-only entries** auto-send a spy mission instead of a raid: if only scouting-capable units are selected, the system converts it. **Mixed scouts plus combat units is a normal raid** — the scouts fight, die as combat units, and produce no scouting report. VERIFIED.
- **Entries fail rather than disappear.** The common claim that entries "auto-deactivate" on chiefing or oasis-ownership change is stronger than Travian documents. Entries remain in place and their sends fail with specific reasons (target deleted, target protected, pushing limit). Monitor per-entry send-fail status; do not assume the server hid them.

## II.4 Raid versus attack, and why siege is wasted

**All farm-list sends are raids, never full attacks.** VERIFIED and consistent across every source; there is no per-entry type toggle.

In a raid: **catapults do not fire and rams do not damage the wall.** Including siege in a farm-list entry therefore just adds nothing but exposes expensive, slow units to losses and contributes no carry. **The tool should never put siege in farm-list entries.**

A **normal attack** is required to chief, to capture artifacts, or to destroy buildings.

## II.5 Cranny mechanics and the Teuton hero dip

Crannies hide resources from raids. Roman/Teuton L10 hides **2,000 per resource**; **Gaul crannies are ×1.5** (3,000 at L10). Crannies cannot be destroyed by catapults.

**The Teuton hero cranny dip: enemy cranny protection is multiplied by 0.8** — the top 20% becomes raidable. VERIFIED (support "Hiding Resources & Cranny": if a Teuton hero accompanies raiding troops, the cranny protects only 80% of its usual capacity). Three critical qualifications:

1. **It is multiplicative on the target's protection capacity**, not an additive bonus to loot and not a buff to attacker carry. A L10 cranny hides 1,600 instead of 2,000; the 400 difference becomes available.
2. **The hero must physically travel with that raid.** This is the T4 rule — the older automatic-by-tribe version is gone. So it applies only to whichever single send actually contains the hero, and **the hero can only be in one place at a time.** Using farm lists to apply the dip across many targets simultaneously is impossible.
3. **Irrelevant for unoccupied oases** — there is no cranny on an oasis tile to dip.

Thief hero items (Pouch/Bag/Sack) stack additively on top of the tribe dip for Teutons.

**What to look for in the raid analyzer:** the codebase has **no cranny model.** It uses `WAREHOUSE_RATIO = 0.67` as a fallback stealable fraction when the carry icon isn't parsed, and otherwise prefers the parsed value. That is pragmatic and arguably better than modelling crannies from scratch. But two risks follow:

- If the scoring was calibrated on hero-accompanied raids, it will **over-estimate** hero-less ones by up to 20% of cranny capacity.
- Nothing gates the dip on hero presence, so there is no place where the distinction could be expressed even if wanted.

**Direction of danger: quiet over-estimation** → repeated under-full bags on targets the model rates as profitable.

## II.6 Bounty arithmetic

Community-consensus formula; Travian does not print it.

```
raidable_pool[r]  = max(0, stock[r] − cranny_protection[r])
available_carry   = Σ (surviving_units × per_unit_carry)
haul[r]           = distributed proportionally across resources up to available_carry,
                    never exceeding raidable_pool[r]
```

**Only surviving troops carry.** Surplus carry beyond the total raidable pool simply goes unused — 100 clubswingers (6,000 carry) against a 2,400 pool return with 2,400 and 3,600 of waste.

Resources are taken from warehouses, granaries, **and anything offered for sale on the marketplace** (VERIFIED) — a detail that makes trade-active targets richer than their stock suggests.

Teuton carry capacities: Clubswinger 60, Axeman 50, Spearfighter 40, Paladin 110, Teutonic Knight 80, rams/catapults 0. The code's `CLUB_CARRY = 60` and `AXE_CARRY = 50` match stock — but per §0.1, stock is not guaranteed on this server, and carry is measurable from any full-bag report.

**The bag/sack percentage is carry-utilisation, not target-emptied.** `haul / total_carry_of_survivors`. So a 100% bag means a full carry, *not* that the target was cleaned out — if the target held more than you could carry you still show 100%. Conversely, bringing far more carry than the target had gives a low percentage even though you emptied it completely. **Any resizing logic that reads the bag percentage as "how much of the target we got" has the semantics backwards**, and the error is self-reinforcing: it will keep growing sends against targets that are already fully drained.

## II.7 Pushing protection and zero-bounty raids

Raiding **unrelated players (Connection Type 1) has no restriction.** VERIFIED.

For Connection Type 2 (recent shared IP, past sitters) and Type 3 (active sitter, shared device), the automatic anti-pushing system caps raidable bounty, and a raid that would exceed the limit **returns zero bounty** with an explanation in the battle report. There is also a specific rule: you cannot raid villages that were sent resources in the last 6 hours by a player you have a Type 3 connection with.

Zero-bounty raids record correctly in the farm-list metrics — combat result registers, last/total bounty show 0. Nothing is silently suppressed; the explanation just lives in the battle report rather than the list view.

**What to look for:** the tool should **not** flag zero-bounty raids as bugs or errors when targets are on shared IPs or sitter relationships. The codebase does model this (`pushing_protection_suspect`, `any_ct2_ct3_flag`) — correctly, and correctly only for raid targets, never for own-village trade.

## II.8 Oases

Killing nature units yields resources: Rats 40 of each, Boars 80, Bears/Crocodiles/Tigers 120, Elephants 200 of each. VERIFIED (support "Early Game Oasis Farming"). An unoccupied oasis holds roughly 1,000–2,000 per resource type depending on type, regenerating over time; nature troops respawn once beginner protection ends.

Annexed oases give production instead of loot. No cranny on an oasis tile, so no hero dip applies.

## II.9 Reports and no-loss suppression

The October 2024 QoL update added an option to **suppress farm-list raid reports with no losses**. VERIFIED — it removes only zero-attacker-loss reports; anything with a loss still generates. It is an **account-level** preference, not per-list.

**If the tool parses reports to update entry metrics, do not enable this option on the avatar** — it silently removes exactly the reports that describe successful raids, and the tool's metrics will degrade with no visible cause.

## II.10 Cooldowns and simultaneous raids

**There is no artificial minimum interval between raids on the same target**, and no per-target cap on in-transit raids. The only constraints are the 20,000 global cap and physics — troops must complete the round trip before being re-sent. VERIFIED as no documented limit; the Game Rules FAQ even acknowledges that players may attack a target 8–10 times in one second without tooling.

The "1×/2×/3× interval" lore circulating in community guides comes from third-party tool scheduling conveniences, **not** from the game. Do not encode it as a rule.

## II.11 Edge cases

- **Rally point destroyed (level 0): no farm-list send can be initiated.** Returning troops still arrive; outgoing is blocked until rebuilt.
- **Sender village under attack:** Send All still works, sends are not blocked.
- **Sender village conquered mid-batch:** in-transit raids continue and return — to a village that is no longer yours, so **the troops are lost on arrival.** The lists move to the "Abandoned farm list" category, a real state the data model should support: lists survive village loss, sit in a hidden category that appears only when populated, and can be revived by moving them to a surviving village. VERIFIED.
- **One sender village per list**, immutable except via the explicit "Move list" operation. Moving preserves troop values. Moving to a **different-tribe** village is officially silent, but unit types won't exist there, so per-entry troop values become unusable until re-edited. Raiding one target from two villages requires duplicating the entry into a second list.

---

