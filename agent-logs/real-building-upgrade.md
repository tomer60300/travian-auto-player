# Real Test: Building Upgrade (Cropland Slot 2)

**Date:** 2026-04-05
**Village ID:** 41699
**Target:** Slot 2, Cropland, Level 4 -> 5
**Gold flag:** NOT passed (--allow-gold never used)

---

## BEFORE

### Building List (slot 2)

```
Slot 2 | Cropland | GID 4 | Level 4
```

Confirmed: Cropland at level 4 in resource-field slot (1-18), safe to upgrade.

### Resources

```
Lumber:    3639 / 5000
Clay:      2788 / 5000
Iron:      1990 / 5000
Crop:      4060 / 5000
Free crop: 58
```

### Queue

```
Queue is empty
```

Queue empty -- safe to proceed (no gold cost risk).

---

## ACTION

**Command:** `travian building upgrade --slot-id 2 -v 41699`

### CLI Output

```
✓ Upgrade started!
  Cropland: Level 4 → 5
  Construction time: 17:27:56
```

**Exit code:** 0

---

## AFTER (5-second wait applied)

### Queue (post-upgrade)

```
Cropland → Level 5  (2141s remaining)
```

Queue now shows Cropland upgrading to level 5 with ~35 minutes remaining.

### Building List (post-upgrade)

```
Slot 2 | Cropland | GID 4 | Level 4
```

Slot 2 still shows level 4 in the building list. This is expected behavior -- the level increments only after the construction timer completes. The queue entry is the authoritative indicator that the upgrade is in progress.

---

## VERIFY

| Check                                      | Result  |
|--------------------------------------------|---------|
| CLI reported success (exit code 0)         | YES     |
| CLI output shows Cropland 4 -> 5           | YES     |
| Queue now contains Cropland -> Level 5     | YES     |
| Building list still shows level 4          | YES (expected -- under construction) |
| --allow-gold was NOT used                  | CONFIRMED |
| Slot is resource field (1-18)              | YES (slot 2) |
| Original level was 4 (low-level)           | YES     |

### Verdict Matrix

- Queue contains entry for Cropland at level 5: **YES**
- CLI reported success: **YES**

This matches the **PASS (CONFIRMED)** scenario:
> Queue now contains Cropland slot 2 at level 5 = PASS (CONFIRMED)

---

## VERDICT: PASS (CONFIRMED)

The `travian building upgrade` command successfully queued a Cropland upgrade from level 4 to level 5 in village 41699 (slot 2). The upgrade was confirmed server-side via the queue check. No gold was spent. Construction time is approximately 17 hours 28 minutes.
