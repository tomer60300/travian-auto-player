# CLI Command Log

Session started: 2026-03-28

---

## 1. Install travian module (editable)

**Command:**
```bash
pip install -e .
```

**Output:**
```
Obtaining file:///C:/Users/yuval/OneDrive/%D0%94%D0%BE%D0%BA%D1%83%D0%BC%D0%B5%D0%BD%D1%82%D1%8B/MyLoveT/YuvalPC/travian-auto-player/.claude/worktrees/competent-saha
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Checking if build backend supports build_editable: started
  Checking if build backend supports build_editable: finished with status 'done'
  Getting requirements to build editable: started
  Getting requirements to build editable: finished with status 'done'
  Preparing editable metadata (pyproject.toml): started
  Preparing editable metadata (pyproject.toml): finished with status 'done'
Requirement already satisfied: httpx>=0.25.0 in ...
Requirement already satisfied: pydantic>=2.0.0 in ...
Requirement already satisfied: pydantic-settings>=2.0.0 in ...
Requirement already satisfied: typer>=0.9.0 in ...
Requirement already satisfied: python-dotenv>=1.0.0 in ...
Requirement already satisfied: beautifulsoup4>=4.12.0 in ...
Requirement already satisfied: lxml>=4.9.0 in ...
Requirement already satisfied: tenacity>=8.2.0 in ...
Requirement already satisfied: rich>=13.0.0 in ...
Requirement already satisfied: pyyaml>=6.0 in ...
(+ transitive deps all satisfied)
Building wheels for collected packages: travian-api
  Building editable for travian-api (pyproject.toml): started
  Building editable for travian-api (pyproject.toml): finished with status 'done'
  Created wheel for travian-api: filename=travian_api-0.1.0-0.editable-py3-none-any.whl size=6248
Successfully built travian-api
Installing collected packages: travian-api
  Attempting uninstall: travian-api
    Found existing installation: travian-api 0.1.0
    Successfully uninstalled travian-api-0.1.0
Successfully installed travian-api-0.1.0
```

**Status:** SUCCESS

---

## 2. Verify CLI entry point

**Command:**
```bash
travian --help
```

**Output:**
```
Usage: travian [OPTIONS] COMMAND [ARGS]...

 Travian Legends API - Game automation library and CLI

┌─ Options ───────────────────────────────────────────────────────────────────┐
│ --server    -s      TEXT  Game server URL [env var: TRAVIAN_BASE_URL]       │
│ --username  -u      TEXT  Account username/email                            │
│ --password  -p      TEXT  Account password                                  │
│ --help                    Show this message and exit.                       │
└─────────────────────────────────────────────────────────────────────────────┘
┌─ Commands ──────────────────────────────────────────────────────────────────┐
│ auth      Authentication commands                                           │
│ village   Village management commands                                       │
│ building  Building management commands                                      │
│ military  Military operation commands                                       │
│ reports   Reports management commands                                       │
│ queue     Priority build queue commands                                     │
│ video     Video reward commands                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Status:** SUCCESS

---

## 3. Auth login

**Command:**
```bash
travian --server "https://ts2.x1.europe.travian.com" --username "chetrit1311@gmail.com" --password "12344456t" auth login
```

**Output:**
```
OK - Logged in!
  Player: Barca
  Tribe:  1 (1=Roman, 2=Teuton, 3=Gaul)
  Villages (1):
    41699  Barca`s village  (92|13)
```

**Status:** SUCCESS

---

## 4. Building list — village 41699

**Command:**
```bash
travian --server "https://ts2.x1.europe.travian.com" --username "chetrit1311@gmail.com" --password "12344456t" building list -v 41699
```

**Output:**
```
          Village Buildings
┌──────┬───────────────┬─────┬───────┐
│ Slot │ Name          │ GID │ Level │
├──────┼───────────────┼─────┼───────┤
│    1 │ Woodcutter    │   1 │     2 │
│    2 │ Cropland      │   4 │     2 │
│    3 │ Woodcutter    │   1 │     2 │
│    4 │ Iron Mine     │   3 │     1 │
│    5 │ Clay Pit      │   2 │     3 │
│    6 │ Clay Pit      │   2 │     2 │
│    7 │ Iron Mine     │   3 │     1 │
│    8 │ Cropland      │   4 │     2 │
│    9 │ Cropland      │   4 │     2 │
│   10 │ Iron Mine     │   3 │     1 │
│   11 │ Iron Mine     │   3 │     1 │
│   12 │ Cropland      │   4 │     2 │
│   13 │ Cropland      │   4 │     2 │
│   14 │ Woodcutter    │   1 │     2 │
│   15 │ Cropland      │   4 │     1 │
│   16 │ Clay Pit      │   2 │     3 │
│   17 │ Woodcutter    │   1 │     3 │
│   18 │ Clay Pit      │   2 │     1 │
│   19 │ Empty         │   0 │     0 │
│   20 │ Empty         │   0 │     0 │
│   21 │ Empty         │   0 │     0 │
│   22 │ Empty         │   0 │     0 │
│   23 │ Empty         │   0 │     0 │
│   24 │ Empty         │   0 │     0 │
│   25 │ Empty         │   0 │     0 │
│   26 │ Main Building │  15 │     7 │
│   27 │ Empty         │   0 │     0 │
│   28 │ Empty         │   0 │     0 │
│   29 │ Barracks      │  19 │     2 │
│   30 │ Warehouse     │  10 │     2 │
│   31 │ Granary       │  11 │     2 │
│   32 │ Marketplace   │  17 │     1 │
│   33 │ Empty         │   0 │     0 │
│   34 │ Empty         │   0 │     0 │
│   35 │ Embassy       │  18 │     1 │
│   36 │ Empty         │   0 │     0 │
│   37 │ Empty         │   0 │     0 │
│   38 │ Empty         │   0 │     0 │
│   39 │ Rally Point   │  16 │     1 │
│   40 │ Empty         │   0 │     0 │
└──────┴───────────────┴─────┴───────┘
```

**Status:** SUCCESS

---

## 5. Validate build plan

**Command:**
```bash
travian --server "https://ts2.x1.europe.travian.com" --username "chetrit1311@gmail.com" --password "12344456t" queue validate plans/barca-village.yaml
```

**Output:**
```
Village: 41699
Items: 12
  FOUND: Cropland at slot 15 (Lv1 -> 2)
  FOUND: Clay Pit at slot 18 (Lv1 -> 2)
  FOUND: Cropland at slot 2 (Lv2 -> 3)
  FOUND: Woodcutter at slot 1 (Lv2 -> 3)
  FOUND: Woodcutter at slot 3 (Lv2 -> 3)
  FOUND: Clay Pit at slot 6 (Lv2 -> 3)
  FOUND: Clay Pit at slot 18 (Lv1 -> 3)
  FOUND: Cropland at slot 8 (Lv2 -> 3)
  FOUND: Iron Mine at slot 4 (Lv1 -> 2)
  FOUND: Iron Mine at slot 7 (Lv1 -> 2)
  FOUND: Iron Mine at slot 10 (Lv1 -> 2)
  FOUND: Iron Mine at slot 11 (Lv1 -> 2)

Build plan (12 items):

  Priority 1:
    Cropland slot=15 Lv1->2
    Clay Pit slot=18 Lv1->2

  Priority 2:
    Cropland slot=2 Lv2->3
    Woodcutter slot=1 Lv2->3
    Woodcutter slot=3 Lv2->3
    Clay Pit slot=6 Lv2->3
    Clay Pit slot=18 Lv1->3

  Priority 3:
    Cropland slot=8 Lv2->3

  Priority 4:
    Iron Mine slot=4 Lv1->2
    Iron Mine slot=7 Lv1->2
    Iron Mine slot=10 Lv1->2
    Iron Mine slot=11 Lv1->2
```

**Status:** SUCCESS (old plan, replaced below)

---

## 6. Validate updated build plan (strict priority ordering 1→8)

**Command:**
```bash
travian --server "https://ts2.x1.europe.travian.com" --username "chetrit1311@gmail.com" --password "12344456t" queue validate plans/barca-village.yaml
```

**Output:**
```
Village: 41699
Items: 12
  FOUND: Cropland at slot 15 (Lv1 -> 2)
  FOUND: Clay Pit at slot 18 (Lv1 -> 2)
  FOUND: Cropland at slot 2 (Lv2 -> 3)
  FOUND: Woodcutter at slot 1 (Lv2 -> 3)
  FOUND: Woodcutter at slot 3 (Lv2 -> 3)
  FOUND: Clay Pit at slot 6 (Lv2 -> 3)
  FOUND: Clay Pit at slot 18 (Lv1 -> 3)
  FOUND: Cropland at slot 8 (Lv2 -> 3)
  FOUND: Iron Mine at slot 4 (Lv1 -> 2)
  FOUND: Iron Mine at slot 7 (Lv1 -> 2)
  FOUND: Iron Mine at slot 10 (Lv1 -> 2)
  FOUND: Iron Mine at slot 11 (Lv1 -> 2)

Build plan (12 items):

  Priority 1:
    Cropland slot=15 Lv1->2

  Priority 3:
    Clay Pit slot=18 Lv1->2

  Priority 4:
    Cropland slot=2 Lv2->3

  Priority 5:
    Woodcutter slot=1 Lv2->3
    Woodcutter slot=3 Lv2->3

  Priority 6:
    Clay Pit slot=6 Lv2->3
    Clay Pit slot=18 Lv1->3

  Priority 7:
    Cropland slot=8 Lv2->3

  Priority 8:
    Iron Mine slot=4 Lv1->2
    Iron Mine slot=7 Lv1->2
    Iron Mine slot=10 Lv1->2
    Iron Mine slot=11 Lv1->2
```

**Status:** SUCCESS

---

## 7. Queue run (first attempt — FAILED)

**Command:**
```bash
travian --server "https://ts2.x1.europe.travian.com" --username "chetrit1311@gmail.com" --password "12344456t" queue run plans/barca-village.yaml
```

**Output (truncated — was looping):**
```
=== Priority 1: 1 items ===
    Cropland Lv1 -> 2 (slot 15)
    UPGRADE FAILED: Cropland (slot 15) - <!DOCTYPE html>...
  Insufficient resources for priority 1 items. Waiting 30s...
  (repeated in infinite loop)
```

**Status:** FAILED — Bug found in `building_service.py`

**Root cause:** The `upgrade_building()` method unconditionally appended `&buildmaster` to the upgrade URL. On non-Plus accounts, this causes a silent failure (the server returns a normal HTML page instead of executing the build). The success detection patterns (`showCancelBuildingDialog`, `buildDuration`) weren't found in the response, so every upgrade was treated as failed.

**Fix applied:** Removed forced `&buildmaster` from the URL. Now it only adds `&buildmaster` when `allow_gold=True`.

---

## 8. Queue run (after fix) — SUCCESS

**Command:**
```bash
travian --server "https://ts2.x1.europe.travian.com" --username "chetrit1311@gmail.com" --password "12344456t" queue run plans/barca-village.yaml
```

**Output:**
```
Loaded plan: village 41699, 12 items
  P1: slot 15 -> Lv2
  P3: slot 18 -> Lv2
  P4: slot 2 -> Lv3
  P5: slot 1 -> Lv3
  P5: slot 3 -> Lv3
  P6: slot 6 -> Lv3
  P6: slot 18 -> Lv3
  P7: slot 8 -> Lv3
  P8: slot 4 -> Lv2
  P8: slot 7 -> Lv2
  P8: slot 10 -> Lv2
  P8: slot 11 -> Lv2
  FOUND: Cropland at slot 15 (Lv1 -> 2)
  FOUND: Clay Pit at slot 18 (Lv1 -> 2)
  FOUND: Cropland at slot 2 (Lv2 -> 3)
  FOUND: Woodcutter at slot 1 (Lv2 -> 3)
  FOUND: Woodcutter at slot 3 (Lv2 -> 3)
  FOUND: Clay Pit at slot 6 (Lv2 -> 3)
  FOUND: Clay Pit at slot 18 (Lv1 -> 3)
  FOUND: Cropland at slot 8 (Lv2 -> 3)
  FOUND: Iron Mine at slot 4 (Lv1 -> 2)
  FOUND: Iron Mine at slot 7 (Lv1 -> 2)
  FOUND: Iron Mine at slot 10 (Lv1 -> 2)
  FOUND: Iron Mine at slot 11 (Lv1 -> 2)

=== Priority 1: 1 items ===
    Cropland Lv1 -> 2 (slot 15)
  STARTED: Cropland Lv1->2 (20:07:58)

=== Priority 3: 1 items ===
    Clay Pit Lv1 -> 2 (slot 18)
  STARTED: Clay Pit Lv1->2 (20:07:59)

=== Priority 4: 1 items ===
    Cropland Lv2 -> 3 (slot 2)
  STARTED: Cropland Lv2->3 (20:08:01)

=== Priority 5: 2 items ===
  STARTED: Woodcutter Lv2->3 (20:08:03)
  STARTED: Woodcutter Lv2->3 (20:08:05)

=== Priority 6: 2 items ===
  STARTED: Clay Pit Lv2->3 (20:08:07)
  STARTED: Clay Pit Lv1->2 (20:08:08)  [multi-level: 1->2 first]
  STARTED: Clay Pit Lv2->3 (20:08:10)

=== Priority 7: 1 items ===
  STARTED: Cropland Lv2->3 (20:08:12)

=== Priority 8: 4 items ===
  STARTED: Iron Mine Lv1->2 (20:08:14)
  STARTED: Iron Mine Lv1->2 (20:08:15)
  STARTED: Iron Mine Lv1->2 (20:08:17)
  STARTED: Iron Mine Lv1->2 (20:08:19)

Build plan complete!

Results:
  Cropland 1->2 - started  20:07:58
  Clay Pit 1->2 - started  20:07:59
  Cropland 2->3 - started  20:08:01
  Woodcutter 2->3 - started  20:08:03
  Woodcutter 2->3 - started  20:08:05
  Clay Pit 2->3 - started  20:08:07
  Clay Pit 1->2 - started  20:08:08
  Clay Pit 2->3 - started  20:08:10
  Cropland 2->3 - started  20:08:12
  Iron Mine 1->2 - started  20:08:14
  Iron Mine 1->2 - started  20:08:15
  Iron Mine 1->2 - started  20:08:17
  Iron Mine 1->2 - started  20:08:19
```

**Status:** FAILED — Builds fired in 21 seconds total, only first one actually went through. Queue parser was broken.

---

## 9. Bug investigation & fixes

### Bug 1: `&buildmaster` forced on all upgrades (building_service.py)
- **Symptom:** Upgrade returned HTML page, success detection failed, infinite retry loop
- **Root cause:** `upgrade_building()` unconditionally added `&buildmaster` to URL. Non-Plus accounts silently ignore this.
- **Fix:** Only add `&buildmaster` when `allow_gold=True`

### Bug 2: Queue parser couldn't detect builds (html_parser.py — `parse_construction_queue`)
- **Symptom:** `is_queue_empty()` always returned `True`, so builds fired back-to-back without waiting
- **Root cause (3 issues):**
  1. Looked for `id="buildingList"` but HTML uses `class="buildingList"`
  2. Looked for `li.buildingItem` but `<li>` has no class
  3. Looked for timer by `id=timer\d+` but timer uses `class="timer"` with `value=` attribute (seconds)
- **Fix:** Updated selectors to match actual Travian HTML structure

### Bug 3: No post-upgrade wait (build_queue_service.py)
- **Symptom:** After upgrade, next build started immediately without waiting for current to finish
- **Fix:** Added post-upgrade wait loop: sleep 2s (grace), then poll `is_queue_empty()` until build completes

---

## 10. Queue re-run (with all fixes)

**Command:**
```bash
travian --server "https://ts2.x1.europe.travian.com" --username "chetrit1311@gmail.com" --password "12344456t" queue run plans/barca-village.yaml
```

**Output (in progress):**
```
  SKIP: Cropland already at level 2 (target 2)
  FOUND: 11 remaining items

=== Priority 3: 1 items ===
    Clay Pit Lv1 -> 2 (slot 18)
  Waiting for queue (220s)...
  (properly waiting for existing build to finish)
```

**Status:** RUNNING — properly waiting between builds now

---
