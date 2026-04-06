# Dry Auth / Village / Building Tests

**Date:** 2026-04-05
**Venv:** `.venv/Scripts/travian.exe`
**Credential check:** PASSED (TRAVIAN_USERNAME=chetrit1311@gmail.com, TRAVIAN_BASE_URL=https://ts2.x1.europe.travian.com/)

## Results

| # | Command | Exit Code | Output Valid | Verdict |
|---|---------|-----------|--------------|---------|
| 1 | `travian auth login` | 0 | Yes - player info shown | PASS |
| 2 | `travian auth token` | 0 | Yes - JWT string printed | PASS |
| 3 | `travian village list` | 0 | Yes - table with village IDs | PASS |
| 4 | `travian building list` | 0 | Yes - table with slot/name/level | PASS |
| 5 | `travian building resources` | 0 | Yes - lumber/clay/iron/crop > 0 | PASS |
| 6 | `travian building queue` | 0 | Yes - queue info shown (empty) | PASS |
| 7 | `travian building upgrade --help` | 0 | Yes - --slot-id flag present | PASS |
| 8 | `travian building construct --help` | 0 | Yes - --slot-id and --building flags present | PASS |

## Output Excerpts

### 1. auth login
```
OK - Logged in!
  Player: Barca
  Tribe:  1 (1=Roman, 2=Teuton, 3=Gaul)
  Villages (1):
    41699  Slave01  (92|13)
```

### 2. auth token
```
eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJCWG9URDVYVG0z...
```
(Full JWT token printed, ~600 chars)

### 3. village list
```
              Villages              
┌───────┬─────────┬────┬────┬──────┐
│    ID │ Name    │  X │  Y │ Main │
├───────┼─────────┼────┼────┼──────┤
│ 41699 │ Slave01 │ 92 │ 13 │      │
└───────┴─────────┴────┴────┴──────┘
```

### 4. building list
```
          Village Buildings           
┌──────┬───────────────┬─────┬───────┐
│ Slot │ Name          │ GID │ Level │
├──────┼───────────────┼─────┼───────┤
│    1 │ Woodcutter    │   1 │     5 │
│    2 │ Cropland      │   4 │     4 │
│  ... │ ...           │ ... │   ... │
│   40 │ City Wall     │  31 │     1 │
└──────┴───────────────┴─────┴───────┘
```
(40 slots total, mix of resource fields and village buildings, several Empty slots)

### 5. building resources
```
  Lumber:   3620 / 5000
  Clay:     2772 / 5000
  Iron:     1972 / 5000
  Crop:     4049 / 5000
  Free crop: 58
```

### 6. building queue
```
Queue is empty
```

### 7. building upgrade --help
```
Usage: travian building upgrade [OPTIONS]
  --slot-id     -s  INTEGER  Building slot ID (1-40) [required]
  --allow-gold               Allow spending gold (master builder).
  --village-id  -v  INTEGER  Village ID (default: current village)
  --help                     Show this message and exit.
```

### 8. building construct --help
```
Usage: travian building construct [OPTIONS]
  --slot-id     -s  INTEGER  Empty building slot ID (19-40) [required]
  --building    -b  TEXT     Building name to construct [required]
  --allow-gold               Allow spending gold (master builder).
  --village-id  -v  INTEGER  Village ID (default: current village)
  --help                     Show this message and exit.
```

## Summary

**8/8 PASS** - All auth, village, and building commands working correctly.
