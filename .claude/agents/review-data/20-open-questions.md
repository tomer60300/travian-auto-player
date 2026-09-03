# Caveats and open questions

**Confirmed modified on this server:** Teuton merchant base and Trade Office slope only (measured 7,920 at TO 13). Every other constant may also be modified and none has been independently measured. **Merchant speed in particular is assumed from stock** and is worth one timed send to confirm.

**Genuinely unresolved, each settleable with one controlled in-game test:**

| # | Question | Blocks | Test |
|---|---|---|---|
| 1 | Trade-route behaviour when resources or merchants are short — skip, partial, or top up? | correctness of every hourly rate | one deliberately resource-starved route |
| 2 | Does Gold Club cap trade routes per village or account? | the apply layer | create routes until refused |
| 3 | Is the filling branch of the net-crop derivation correct? | storage safety on filling villages | capture Resources + Capacity + one filling `dorf1` together |
| 4 | Merchant capacity model — is `2200 / 0.20` the right factorisation? | all route sizing | read capacity at a second Trade Office level |
| 5 | Tournament Square maximum — +200% or +500%? | troop travel time | Rally Point travel simulator |
| 6 | Current cancel-refund rule for construction | build queue economics | cancel one L2+ upgrade and observe |

**Documented but unmodelled:** the alliance Commerce bonus. Its absence is the safe direction (over-provisioning merchants), so this is a cost issue rather than a correctness one — but implementing it wrongly flips it dangerous.

**Better in the code than in any public source:** the video reward figures (+15% / 8h). Do not "correct" these toward the gold-bonus numbers.

**Wrong in circulating sources, right in the code:** map span. Any document claiming ±400 / span 801 for Europe 2 is mistaken; the codebase's captured `mapId 45136` proves 401.

**Community-sourced tables here reflect stock values.** Per §0.1, stock ≠ this server. Where a number matters, measure it.

**Discarded as Travian Kingdoms (T5), not Legends:** NPC merchant at 4–5 gold, different starvation classes, the "+100×100 farm list" Plus benefit, relocation crop rules, oasis influence ranking. Several of these surface high in search results for Legends queries.

**Third-party automation-tool documentation** was used only as descriptive evidence of observed game behaviour, never as authority on rules. Where this document says OFFICIALLY SILENT or PARTIAL, treat the behaviour as observed-but-not-contracted and re-validate empirically before relying on it.
