# Part III — Map, travel and scouting

## III.1 Map geometry

Covered fully in §I.3.3 — **span 401, ±200, wraps both axes, span must be odd.** The same span must be used by the distance function and by `map_id_to_coords`, or route reconciliation breaks.

## III.2 Troop travel time

Different from merchants: **speed bonuses apply only beyond the first 20 fields.**

```
dist ≤ 20:   time = dist / base_speed
dist > 20:   time = 20 / base_speed
                  + (dist − 20) / (base_speed × (1 + TS_bonus + boots_bonus))
```

then multiplied by artifact, pennant and standard factors. **Tournament Square and Boots add together; everything else multiplies.**

Tournament Square gives +10% base speed per level beyond 20 fields, requiring Rally Point 15. **The maximum is DISPUTED** — current documentation indicates +200% at L20, while some legacy sources cite up to +500%. **Verify via the in-game Rally Point travel-time simulator** before encoding it.

A hero moving with troops travels at the **slowest unit's** speed.

**None of this touches merchants** (§I.3.2).

## III.3 Auto-scout

The README notes auto-scout enrichment costs one API call per tile through the stealth throttler, so large-radius scans are slow **by design**. That is the correct trade given that requests are the scarce resource, and a reviewer should not treat the slowness as a performance defect to optimise away.

Scouting semantics: sending only scouting-capable units produces a scout mission regardless of whether raid or attack was selected. Scouts sent alongside combat units fight and die instead.

---

