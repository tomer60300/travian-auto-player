# Real Test: Video Reward Claim

**Date:** 2026-04-05
**Credential check:** PASS (username=chetrit1311@gmail.com, base_url=https://ts2.x1.europe.travian.com/)

---

## Test 1: Claim lumberProductionBonus (earlier session)

**Command:** `travian video claim lumberProductionBonus`

### BEFORE

```
                Video Rewards                 
+------------------------+-----------+--------+
| Reward                 | Available | Active |
+------------------------+-----------+--------+
| lumberProductionBonus  |    yes    |   -    |
| clayProductionBonus    |    yes    |   -    |
| ironProductionBonus    |    yes    |   -    |
| cropProductionBonus    |    yes    |   -    |
+------------------------+-----------+--------+
```

Exit code: 0. All four reward types available, none active.

### ACTION

```
$ travian video claim lumberProductionBonus
Claiming lumberProductionBonus -- this takes ~33 seconds...
  Simulating video ---------------------------------------- 33/33s
  Reward claimed: +15% lumber production (8h)
```

Exit code: 0.

### AFTER

```
                Video Rewards                 
+------------------------+-----------+--------+
| Reward                 | Available | Active |
+------------------------+-----------+--------+
| lumberProductionBonus  |    no     | active |
| clayProductionBonus    |    yes    |   -    |
| ironProductionBonus    |    yes    |   -    |
| cropProductionBonus    |    yes    |   -    |
+------------------------+-----------+--------+
```

Exit code: 0. lumberProductionBonus: available=yes -> available=no/active.

### Verdict: PASS

---

## Test 2: Claim clayProductionBonus

**Date:** 2026-04-05
**Command:** `travian video claim clayProductionBonus`

### BEFORE

```
                Video Rewards                 
+-----------------------+-----------+--------+
| Reward                | Available | Active |
+-----------------------+-----------+--------+
| lumberProductionBonus |    no     | active |
| clayProductionBonus   |    yes    |   -    |
| ironProductionBonus   |    yes    |   -    |
| cropProductionBonus   |    yes    |   -    |
+-----------------------+-----------+--------+
```

Exit code: 0.
Lumber already active from Test 1. Clay, iron, crop available.

### ACTION

```
$ travian video claim clayProductionBonus
Claiming clayProductionBonus -- this takes ~33 seconds...
  Simulating video ---------------------------------------- 33/33s
✓ Reward claimed: +15% clay production (8h)
```

Exit code: 0. CLI reported success: +15% clay production (8h).

### AFTER

```
                Video Rewards                 
+-----------------------+-----------+--------+
| Reward                | Available | Active |
+-----------------------+-----------+--------+
| lumberProductionBonus |    no     | active |
| clayProductionBonus   |    no     | active |
| ironProductionBonus   |    yes    |   -    |
| cropProductionBonus   |    yes    |   -    |
+-----------------------+-----------+--------+
```

Exit code: 0. clayProductionBonus changed from available=yes/active=- to available=no/active=active.
Iron and crop unchanged (still available, not active).

### Verification

| Check | Result |
|---|---|
| BEFORE: clayProductionBonus available? | yes |
| CLI claim exit code | 0 (success) |
| CLI claim message | "+15% clay production (8h)" |
| AFTER: clayProductionBonus available? | no |
| AFTER: clayProductionBonus active? | active |
| Other types unchanged? | yes (iron/crop still available; lumber still active from Test 1) |
| State transition consistent? | yes (available -> claimed -> active) |

### Verdict: PASS (CONFIRMED)

---

## Summary

Two video rewards have been successfully claimed across two sessions:

1. **lumberProductionBonus** -- PASS (earlier session)
2. **clayProductionBonus** -- PASS (CONFIRMED) (this session)

Both showed the correct state transition: available=yes/active=- before claim, available=no/active=active after claim. The 33-second ATG simulation timer completed correctly in both cases. No false positives observed -- server-side state genuinely changed each time.
