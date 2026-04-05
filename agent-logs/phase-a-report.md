# Phase A: Dry Testing Report
Date: 2026-04-05
Server: ts2.x1.europe.travian.com
User: chetrit1311@gmail.com (Player: Barca)

## Summary
Total commands tested: 24
Passed: 23 | Failed: 1 | Skipped: 0

## Results by Agent

### dry-auth (2/2 PASS)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| auth login | 0 | Yes - player Barca, tribe Roman, village Slave01 | PASS |
| auth token | 0 | Yes - valid JWT string (eyJ0eXAi...) | PASS |

### dry-village (2/2 PASS)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| village list | 0 | Yes - table with ID 41699, Slave01 (92,13) | PASS |
| village switch 41699 | 0 | Yes - "Switched to village 41699" | PASS |

### dry-building (5/5 PASS)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| building list | 0 | Yes - 40 rows, Slot/Name/GID/Level columns | PASS |
| building resources | 0 | Yes - L=3895 C=2779 I=2247 Cr=4231, cap=5000 | PASS |
| building queue | 0 | Yes - "Queue is empty" | PASS |
| building upgrade --help | 0 | Yes - --slot-id, --allow-gold flags shown | PASS |
| building construct --help | 0 | Yes - --slot-id, --building flags shown | PASS |

### dry-military (2/2 PASS)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| military scout --help | 0 | Yes - --x, --y, --amount, --type flags | PASS |
| military raid --help | 0 | Yes - --x, --y, --troop flags | PASS |

### dry-reports (2 PASS, 1 FAIL)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| reports list | 0 | Yes - 10 reports (2 adventure, 8 battle) | PASS |
| reports list --max-age-hours 168 | 0 | Yes - same 10 reports, flag accepted | PASS |
| reports show 2774181 | 0 | No - type=unknown, raw HTML not parsed | FAIL |

**Note:** `reports show` fails to parse adventure-type reports (returns raw HTML). Battle/scout reports may parse correctly. Non-core failure.

### dry-video (1/1 PASS)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| video available | 0 | Yes - 4 production rewards all "yes" | PASS |

### dry-farm (5/5 PASS)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| farm list | 0 | Yes - "No farm lists found" (valid empty) | PASS |
| farm send --help | 0 | Yes - LIST_ID arg, --yes flag | PASS |
| farm create --help | 0 | Yes - --name, --village-id flags | PASS |
| farm add-target --help | 0 | Yes - LIST_ID, --x, --y, --troop flags | PASS |
| farm delete --help | 0 | Yes - LIST_ID, --yes flags | PASS |

### dry-scout (3/3 PASS)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| scout scan --radius 5 --limit 5 | 0 | Yes - 5-row table with coords/pop/dist | PASS |
| scout auto --help | 0 | Yes - all flags: --radius, --amount, --type, --dry-run, --exclude | PASS |
| scout auto --radius 5 --limit 3 --dry-run --yes | 0 | Yes - 3 targets shown, "DRY RUN" message | PASS |

### dry-queue (2/2 PASS)
| Command | Exit Code | Output Valid | Verdict |
|---|---|---|---|
| queue validate dry-test-plan.yaml | 0 | Yes - Cropland slot 2 Lv4→5, costs shown | PASS |
| queue run dry-test-plan.yaml --dry-run | 0 | Yes - dry_run status, resource costs shown | PASS |

## Gate Check
- [x] auth login: PASS (CORE)
- [x] village list: PASS (CORE)
- [x] building list: PASS (CORE)
- [x] building resources: PASS (CORE)

**All core commands passed. Phase B may proceed.**
