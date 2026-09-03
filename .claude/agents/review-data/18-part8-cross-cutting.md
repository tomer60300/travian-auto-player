# Part VIII — Cross-cutting

## VIII.1 Where the game's own data misleads

- **In-transit resources** are not in the destination's warehouse until arrival. A planner that reads current stock *and* separately counts incoming routes will double-count. Read stock once, reconcile in-transit exactly once.
- **The resource bar animates client-side.** A JS timer increments it between page loads; the authoritative amount is server-side and reconciled on action. The bar leads or lags.
- **Trade in flight contaminates Capacity-tab readings.**
- **Mobile app versus browser** differ in presentation and rounding, and feature parity is officially undocumented. **Target the browser DOM; do not assume parity.** The 20,000 cap, raid-vs-attack semantics and entry independence are server-side and identical across clients — only the UI differs.
- **Aggregate overviews lag** per-village state.
- **Hand-maintained fields drift.** The design doc's own Trade Office table listed two villages at TO 0 and TO 11 that are both actually TO 13 — every merchant figure derived from it was wrong independently of the formula. This is exactly what a staleness check is for.

**What to look for:** any read trusted for arithmetic without a server response confirming it.

## VIII.2 Account-killers, ranked by what they cost

1. **Shipping the target instead of the gap** — overflow at the destination and starvation at the senders, simultaneously. Structurally prevented now; verify it stays that way.
2. **Gross crop read as net** — inverts the sign on every army village. Prevented by the countdown derivation; still verify no fallback-to-zero path exists.
3. **A route that keeps shipping after the sender's production falls.** Routes are dumb fixed-amount rows. A raided or chiefed feeder keeps being drained to zero. §I.8's two guards.
4. **Starvation with armies stationed as reinforcement** — the death order means your own reinforcing troops die *before* the host village's garrison, and no warning distinguishes them. §I.6.3.
5. **A hammer caught at home** during an enemy attack instead of being out.
6. **Overflow while the average check passes** — the lumpy-batch case. Handled by `simulate_day`.
7. **Losing a hub village** — breaks the topology, leaves a plan referencing a dead id.
8. **A merchant constant wrong in the over-estimating direction** — plans cargo the merchants cannot carry; routes silently under-deliver.
9. **The 20,000 cap unmodelled** — sends silently refused, every downstream raid metric degrades. §0.3.
10. **Village-count churn** — a re-plan that is a re-entry rather than a diff, and a golden fixture that reads live state.

---

