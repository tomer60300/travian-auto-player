# Dry Test Report: Farm / Scout / Queue Commands

**Date:** 2026-04-05
**Credential check:** PASSED (TRAVIAN_USERNAME=chetrit1311@gmail.com, TRAVIAN_BASE_URL=https://ts2.x1.europe.travian.com/)

## Results

| # | Command | Exit Code | Output Valid | Verdict |
|---|---------|-----------|-------------|---------|
| 1 | `travian farm list` | 0 | Yes - "No farm lists found" | PASS |
| 2 | `travian farm send --help` | 0 | Yes - flags: LIST_ID, --yes/-y | PASS |
| 3 | `travian farm create --help` | 0 | Yes - flags: --name/-n, --village-id/-v | PASS |
| 4 | `travian farm delete --help` | 0 | Yes - flags: LIST_ID, --yes/-y | PASS |
| 5 | `travian scout scan --radius 3 --limit 5 --no-enrich` | 0 | Yes - table with 2 targets from Slave01 (92\|13) | PASS |
| 6 | `travian scout auto --help` | 0 | Yes - flags: --radius, --amount, --type, --dry-run, --limit, --yes, --delay, --no-player, --exclude | PASS |
| 7 | `travian scout auto --radius 3 --limit 3 --dry-run --yes` | 0 | Yes - 2 targets listed, "DRY RUN -- no scouts sent" | PASS |
| 8 | Create dry-test-plan.yaml | — | File created | PASS |
| 9 | `travian queue validate dry-test-plan.yaml` | 0 | Yes - "FOUND: Woodcutter at slot 1 (Lv5 -> 6)" | PASS |
| 10 | `travian queue run dry-test-plan.yaml --dry-run` | 0 | Yes - dry run preview with cost breakdown | PASS |
| 11 | Delete dry-test-plan.yaml | — | File deleted | PASS |

## Output Excerpts

### Test 1: farm list
```
No farm lists found
```

### Test 2: farm send --help
```
Usage: travian farm send [OPTIONS] LIST_ID
  Send all active targets in a farm list.
  Arguments: *  list_id  INTEGER  Farm list ID to send [required]
  Options:   --yes/-y  Skip confirmation
```

### Test 3: farm create --help
```
Usage: travian farm create [OPTIONS]
  Create a new farm list.
  Options: *  --name/-n  TEXT  Farm list name [required]
              --village-id/-v  INTEGER  Source village ID
```

### Test 4: farm delete --help
```
Usage: travian farm delete [OPTIONS] LIST_ID
  Delete a farm list.
  Arguments: *  list_id  INTEGER  Farm list ID to delete [required]
  Options:   --yes/-y  Skip confirmation
```

### Test 5: scout scan
```
Scanning from Slave01 (92|13) radius=3
  Found 10 tiles with villages/oases in radius
Found 2 targets:
  (93|12) Vesnice: Pripyat  dist=1.4
  (92|15) A1                dist=2.0
```

### Test 6: scout auto --help
```
Usage: travian scout auto [OPTIONS]
  Scan the map, filter targets, and send scouts automatically.
  Options: --radius/-r, --village-id/-v, --max-pop, --min-pop, --type/-t,
           --amount/-n, --exclude/-e, --no-player, --show-oases,
           --limit/-l, --dry-run, --yes/-y, --delay
```

### Test 7: scout auto --dry-run
```
Auto-Scout from Slave01 (92|13) r=3 type=resources amount=1
  Enriching 2 tiles...
2 targets to scout:
  (93|12) Vesnice: Pripyat  pop=20  dist=1.4  player=Pripyat
  (92|15) A1                pop=331 dist=2.0  player=Lion_roar
DRY RUN -- no scouts sent
```

**Note on Test 7:** The original command included `--no-enrich` which is not a valid flag for `scout auto` (it is valid for `scout scan`). The test was re-run without `--no-enrich` and passed. The `--no-enrich` flag is only available on `scout scan`.

### Test 9: queue validate
```
Village: 41699
Items: 1
  FOUND: Woodcutter at slot 1 (Lv5 -> 6)
Build plan (1 items):
  Priority 1: Woodcutter slot=1 Lv5->6
```

### Test 10: queue run --dry-run
```
Loaded plan: village 41699, 1 items
Mode: DRY RUN
FOUND: Woodcutter at slot 1 (Lv5 -> 6)
--- Priority 1 (1 items) ---
  Woodcutter Lv5->6 (slot 1, cost: lumber=520, clay=1300, iron=650, crop=780) [READY]
RESULT: Woodcutter 5 -> 6 - dry_run
```

## Summary

**11/11 PASS** -- All farm, scout, and queue dry tests completed successfully. One minor note: `--no-enrich` is only available on `scout scan`, not `scout auto`.
