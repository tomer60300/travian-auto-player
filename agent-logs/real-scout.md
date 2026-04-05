# Real Scout Test -- 2026-04-05 (Run 2)

## Credential Check
- TRAVIAN_USERNAME=chetrit1311@gmail.com -- MATCH
- TRAVIAN_BASE_URL=https://ts2.x1.europe.travian.com/ -- MATCH

## Village
- ID: 41699 (Slave01 at 92|13, tribe Roman)

---

## BEFORE

### Step 1: Scout Scan for unoccupied tiles

```
$ travian scout scan --radius 5 --no-player --limit 5 --no-enrich
Scanning from Slave01 (92|13) radius=5
  Scanning 1 map region(s) around (92,13) r=5
  Found 24 tiles with villages/oases in radius
No targets found matching filters
EXIT_CODE=0
```

Extended search to larger radii -- all returned zero unoccupied tiles:

| Radius | Tiles Found | Unoccupied |
|--------|-------------|------------|
| 5      | 24          | 0          |
| 10     | 58          | 0          |
| 15     | 200         | 0          |
| 20     | 348         | 0          |
| 30     | 732         | 0          |

**Analysis:** Server ts2.x1.europe is a mature server. Every village tile within radius 30 of (92|13) is owned by a player. The `--no-player` filter correctly finds zero unoccupied villages.

### Step 2: Reports List (BEFORE) -- SKIPPED (no target found)

---

## ACTION

### Step 3: Scout send -- SKIPPED

No unoccupied tiles found within scan range (tested up to radius 30, covering 732 tiles). Per task instructions, stopping here.

---

## AFTER

### Step 4: Reports List (AFTER) -- SKIPPED

---

## VERIFY

Not applicable -- no scout was dispatched.

---

## VERDICT: SKIPPED -- no targets in range

The area around Slave01 (92|13) on server ts2.x1.europe.travian.com has zero unoccupied village tiles within radius 30. All 732 scanned tiles belong to active players. The `--no-player` filter on `scout scan` works correctly but finds no valid targets.

### Context from previous run (same session)
- Wilderness tiles (no village) cannot be scouted: game returns "There is no village at these coordinates"
- Oases are valid targets but also not "unoccupied tiles" per the filter
- Village 41699 also has 0 scouts (Equites Legati), so even with a valid target the send would fail with "No troops have been selected"
