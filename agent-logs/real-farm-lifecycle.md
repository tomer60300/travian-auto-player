# Real Farm List CRUD Lifecycle Test

**Date:** 2026-04-05 (re-run)
**Server:** https://ts2.x1.europe.travian.com/
**Account:** chetrit1311@gmail.com
**Village ID:** 41699 (Slave01, coords 92|13)

---

## Credential Check

- TRAVIAN_USERNAME: chetrit1311@gmail.com -- MATCH
- TRAVIAN_BASE_URL: https://ts2.x1.europe.travian.com/ -- MATCH
- Result: **PASS**

---

## STEP 1 -- CREATE

### BEFORE
```
travian farm list
-> "No farm lists found"
-> Count: 0
```

### ACTION
```
travian farm create --name "TestVerify-001" -v 41699
-> Created farm list 'TestVerify-001' (id=2575)
```

### AFTER
```
travian farm list
-> Farm Lists table shows:
   ID=2575, Name=TestVerify-001, Slots=0, Running=0, Last Started=never, Village ID=41699
-> Count: 1
```

### VERIFY
Farm list "TestVerify-001" exists with valid ID 2575.

### Verdict: **PASS**

---

## STEP 2 -- ADD TARGET

### BEFORE
```
travian farm show 2575
-> TestVerify-001 (id=2575)
   Village: 41699 | Running raids: 0 | Slots: 0
   Available troops: t1=0 t2=21 t3=0 t4=0 t5=0 t6=0
   No targets in this list
-> Slot count: 0
```

### ACTION (attempts)

**Attempt 1:** `travian farm add-target 2575 --x 0 --y 0 -t t1=1`
- Result: FAILED -- HTTP 400: "raidList.errorWorldWonderVillage" (0,0 is a Wonder of the World)

**Attempt 2:** `travian farm add-target 2575 --x -1 --y -1 -t t1=1`
- Result: FAILED -- HTTP 400: "api.unexpectedError" (no village at those coordinates)

**Attempt 3:** `travian farm add-target 2575 --x 10 --y 10 -t t1=1`
- Result: FAILED -- HTTP 400: "api.unexpectedError" (no village at those coordinates)

**Attempt 4:** `travian farm add-target 2575 --x 91 --y 13 -t t1=1`
- Result: FAILED -- HTTP 400: "api.unexpectedError" (no village at those coordinates)

**Discovery:** Used `travian scout scan -v 41699 --radius 5` to find valid target villages:
- Found 11 targets in radius, closest: (93|12) "Vesnice: Pripyat" pop=20, dist=1.4, player=Pripyat, Romans

**Attempt 5 (valid target):** `travian farm add-target 2575 --x 93 --y 12 -t t1=1`
- Result: SUCCESS -- "Added target (93,12) to list 2575"

### AFTER
```
travian farm show 2575
-> TestVerify-001 (id=2575)
   Village: 41699 | Running raids: 0 | Slots: 1
   Targets table:
   #1 | Vesnice: Pripyat (93|12) | Pop=20 | Dist=1.4 | Troops=t1=1 | Last Raid=never | Status=ready
-> Slot count: 1
```

### VERIFY
Target at (93,12) visible in slot list with t1=1 troops assigned.

### Verdict: **PASS**
Initial failures were due to invalid coordinates (WoW village and empty tiles), not CLI bugs. The CLI correctly propagated server-side error messages. Once a valid village coordinate was used, add-target succeeded.

---

## STEP 3 -- SEND

### ACTION
```
travian farm send 2575 --yes
-> Farm list: TestVerify-001 -- 1 active targets
   Gold Club not active -- cannot send farm lists via API
   Gold Club is not active -- sending via farm list API is blocked.
   Manage lists (create/add targets) still works without Gold Club.
```

### VERIFY
**SKIPPED: Gold Club required**

Gold Club is a premium feature required for farm list sending via API. The CLI correctly detected this limitation, provided a clear error message, and exited gracefully without crashing.

### Verdict: **SKIP (expected -- Gold Club not active)**

---

## STEP 4 -- CLEANUP (DELETE)

### BEFORE
```
travian farm list
-> ID=2575, Name=TestVerify-001, Slots=1, Running=0, Last Started=never, Village ID=41699
-> "TestVerify-001" present: YES
```

### ACTION
```
travian farm delete 2575 --yes
-> "Deleted farm list 2575"
```

### AFTER
```
travian farm list
-> "No farm lists found"
-> Count: 0
```

### VERIFY
"TestVerify-001" is GONE. List count back to 0. Cleanup complete.

### Verdict: **PASS**

---

## Overall Summary

| Step | Operation   | Verdict |
|------|-------------|---------|
| 1    | CREATE      | **PASS** |
| 2    | ADD TARGET  | **PASS** |
| 3    | SEND        | **SKIP** (Gold Club required -- expected) |
| 4    | DELETE      | **PASS** |

**Overall Verdict: PASS**

The full CRUD lifecycle works correctly. Create, add-target, and delete all function as expected. The send operation is blocked by the Gold Club premium requirement, which is a server-side restriction, not a CLI bug. The CLI handles this gracefully with a clear message. Cleanup completed successfully -- no test artifacts remain on the server.

### Notes
- Coordinates for add-target must point to an actual village on the map. Empty tiles and Wonder of the World villages are rejected by the server API with HTTP 400 errors.
- The `scout scan` command proved useful to discover valid nearby targets before adding them to a farm list.
- The delete bug from the previous run (wrong HTTP method) has already been fixed and worked correctly this time.
