# Dry Test: Military, Reports, Video Commands

**Date:** 2026-04-05
**Credential check:** PASSED (TRAVIAN_USERNAME=chetrit1311@gmail.com, TRAVIAN_BASE_URL=https://ts2.x1.europe.travian.com/)

| # | Command | Exit Code | Output Valid | Verdict |
|---|---------|-----------|--------------|---------|
| 1 | `travian military scout --help` | 0 | Yes - `--x`, `--y`, `--amount`, `--type`, `--village-id` flags shown | **PASS** |
| 2 | `travian military raid --help` | 0 | Yes - `--x`, `--y`, `--troop` flags shown | **PASS** |
| 3 | `travian reports list` | 0 | Yes - table with 10 reports (adventure + battle types) | **PASS** |
| 4 | `travian reports list --max-age-hours 168` | 0 | Yes - table with 10 reports, same result set | **PASS** |
| 5 | `travian reports show 2774181` | 0 | Partial - returned raw HTML snippet instead of parsed report details | **PASS (degraded)** |
| 6 | `travian video available` | 0 | Yes - table listing 4 reward types with availability status | **PASS** |

## Output Excerpts

### Test 1: `military scout --help`
```
Usage: travian military scout [OPTIONS]
  Send scouts to target coordinates.
  --x               INTEGER  Target X coordinate [required]
  --y               INTEGER  Target Y coordinate [required]
  --amount   -n     INTEGER  Number of scouts to send [default: 1]
  --type     -t     TEXT     Scout type: resources or defenses [default: resources]
  --village-id -v   INTEGER  Source village ID
```

### Test 2: `military raid --help`
```
Usage: travian military raid [OPTIONS]
  Send a raid to target coordinates.
  --x           INTEGER  Target X coordinate [required]
  --y           INTEGER  Target Y coordinate [required]
  --troop  -t   TEXT     Troop spec: t1=50
```

### Test 3: `reports list`
```
Reports (10 found)
ID      | Type      | Subject                        | Date            | Read
2774181 | adventure | Slave01 explores               | today, 13:36    | yes
2768436 | adventure | Slave01 explores               | today, 09:33    | yes
2704948 | battle    | Whiskey1 raids Slave01          | 04.04.26, 14:48 | yes
2693625 | battle    | A1 raids Slave01                | 04.04.26, 14:25 | no
... (10 rows total)
```

### Test 4: `reports list --max-age-hours 168`
```
Reports (10 found)
(Same 10 reports as test 3 - all within 168-hour window)
```

### Test 5: `reports show 2774181`
```
Type: unknown
  'type': 'unknown',
  'data': {'html_snippet': '<!DOCTYPE html>...<title>Europe 2</title>...'}
  'report_id': '2774181'
```
**Note:** Report parser returned type "unknown" with raw HTML instead of parsed adventure report data. The command executed successfully (exit 0) but the report content was not properly parsed. This may indicate the report detail parser does not handle the "adventure" report type, or the HTML structure changed.

### Test 6: `video available`
```
Video Rewards
Reward                | Available | Active
lumberProductionBonus | no        | active
clayProductionBonus   | yes       | -
ironProductionBonus   | yes       | -
cropProductionBonus   | yes       | -
```

## Summary

- **6/6 commands returned exit code 0**
- **5/6 fully valid output; 1 degraded** (reports show returned unparsed HTML for adventure-type report)
- All expected flags present in help output
- Reports list works and returns live data (10 reports, mix of adventure and battle)
- Video available shows 4 resource-boost reward types, 1 active (lumber), 3 available
