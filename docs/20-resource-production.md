# Resource Production, Capacity & Balance

This document covers all methods to fetch resource data: current amounts, production rates, storage capacity, crop balance, and detailed per-building breakdowns.

---

## Quick Read — Inline Script (Every Page)

Every page includes real-time resource data:

```javascript
var resources = {
    production: {"l1": 654, "l2": 570, "l3": 583, "l4": 518, "l5": 1084},
    storage:    {"l1": 5650, "l2": 5307, "l3": 4944, "l4": 10962},
    maxStorage: {"l1": 9600, "l2": 9600, "l3": 9600, "l4": 17600}
};
```

| Key | l1 | l2 | l3 | l4 | l5 |
|-----|----|----|----|----|-----|
| **Resource** | Lumber | Clay | Iron | Crop | see below |
| `production` | Per-hour rate | Per-hour rate | Per-hour rate | **NET crop per hour** | not net crop — do not use |
| `storage` | Current amount | Current amount | Current amount | Current amount | — |
| `maxStorage` | Warehouse cap | Warehouse cap | Warehouse cap | Granary cap | — |

> ### ⚠️ `l4` is the crop balance, NOT `l5`
>
> **`production.l4` is the true net crop rate** — production minus all consumption
> (population, constructions, and every troop actually feeding from this
> village). It is what the granary drains or fills at, and it goes negative when
> the village is starving. **This is the only field to use for starvation.**
>
> **`production.l5` is NOT net crop.** An earlier version of this document said
> it was; that was wrong, and the mistake propagated into `Resources.free_crop`
> and the Dashboard resource bar.
>
> Measured on a live starving village (`newdid=20003`), captured 2026-08:
>
> ```json
> production: {"l1": 2875, "l2": 3750, "l3": 2175, "l4": -5556, "l5": 1481}
> storage:    {"l1": 88652, "l2": 85167, "l3": 93880, "l4": 67397}
> maxStorage: {"l1": 160000, "l2": 160000, "l3": 160000, "l4": 240000}
> ```
>
> `l5` is **positive (+1481)** while the village is draining at **−5,556/h**.
> Anything treating `l5` as the starvation signal reports this village as
> healthy. Verified independently against the warehouse overview, which marked
> this village `crit` with a 43,899 s countdown to an empty granary:
> `67,397 / 5,556 = 12.13 h = 43,670 s` — a 0.5% match, the residual being the
> few minutes between the two captures.
>
> What `l5` actually means is **unresolved**. From one sample,
> `l5 - l4 = 7037` looks like it could be gross production or total consumption,
> but a single data point cannot distinguish those. Treat it as unknown until
> reconciled against `/production.php?t=balance`, which decomposes every term.

### Python — Quick Resource Fetch

```python
import re, json

html = session.get(f"{BASE}/dorf1.php").text
match = re.search(r'var resources = (\{.*?\});', html, re.DOTALL)
res = json.loads(match.group(1))

print(f"Lumber: {res['storage']['l1']}/{res['maxStorage']['l1']} (+{res['production']['l1']}/h)")
print(f"Clay:   {res['storage']['l2']}/{res['maxStorage']['l2']} (+{res['production']['l2']}/h)")
print(f"Iron:   {res['storage']['l3']}/{res['maxStorage']['l3']} (+{res['production']['l3']}/h)")
print(f"Crop:   {res['storage']['l4']}/{res['maxStorage']['l4']} (+{res['production']['l4']}/h)")
print(f"Free Crop: {res['production']['l5']}/h")
```

---

## Account-Wide Net Crop — Warehouse Overview (2 requests, all villages)

`var resources` costs one request **per village**. The Central Village Overview
carries the same net-crop information for every village at once.

```
/village/statistics/resources            -> absolute stocks per village
/village/statistics/resources/warehouse  -> fill/empty countdown per village
/village/statistics/resources/capacity   -> warehouse + granary caps (changes rarely; cache it)
```

`#warehouse` gives percentages, not absolutes, so pair it with the stocks table.
The countdown is the server's own computation, which is why this needs no upkeep
model:

```
crit present (draining):  net_crop = -stock / seconds * 3600
crit absent  (filling):   net_crop = (granary_cap - stock) / seconds * 3600
```

For a **starving** village only stocks + warehouse are needed — capacity does not
enter the formula. That makes "which villages are starving, and how fast" a
**two-request** question for the whole account.

### `#warehouse` markup

```html
<table id="warehouse">
  <thead><tr>
    <td>Village</td><td><i class="r1"></i></td><td><i class="r2"></i></td><td><i class="r3"></i></td>
    <td><img class="clock"></td>            <!-- warehouse: first of lumber/clay/iron -->
    <td><i class="r4"></i></td>
    <td><img class="clock"></td>            <!-- granary: crop-specific -->
  </tr></thead>
  <tbody>
    <!-- filling -->
    <tr class="hover"><td class="vil fc"><a href="/dorf1.php?newdid=20011">11</a></td>
      <td class="lum">18%</td><td class="clay">35%</td><td class="iron">0%</td>
      <td class="max123"><span class="timer" counting="down" value="88017" data-value="88017">24:26:57</span></td>
      <td class="crop">56%</td>
      <td class="max4 lc"><span class="timer" counting="down" value="211328" data-value="211328">58:42:08</span></td>
    </tr>
    <!-- draining (starving) -->
    <tr class="hover"><td class="vil fc"><a href="/dorf1.php?newdid=20003">03</a></td>
      ...
      <td class="max4 lc"><span class="crit">−</span>&nbsp;<span class="timer crit" value="43899" data-value="43899">12:11:39</span></td>
    </tr>
  </tbody>
</table>
```

| Detail | Value |
|--------|-------|
| Raw seconds | `value` **and** `data-value` on `span.timer` — never parse the rendered `H:MM:SS` |
| Direction | `class="crit"` (plus a `−` U+2212 prefix) = draining; absent = filling |
| Crop column | the **second** clock (`td.max4`); the first (`td.max123`) is whichever of lumber/clay/iron fills first |
| Village id | `newdid=` in the row's first-cell link |

**Three parsing traps, all present in real markup:**

1. Rows are **not** in village-id order — the header has `onclick="sortByColumnOrder(...)"` and the server honours the saved sort. Key on `newdid`, never on row index.
2. The rows are malformed: `<tr class="hover" "="">`. `html.parser` tolerates it; do not switch parsers without re-testing.
3. Percentages are integers, so `pct x capacity` carries up to ±0.5% error. Prefer absolute stocks from `/village/statistics/resources`.

### Central Village Overview — full tab map

Reached from the village-list header, legacy alias `dorf3.php`. **Travian Plus
feature** — code must tolerate its absence and fall back to per-village `dorf1`.

| Tab | Sub-tab | Path | Per-village data |
|-----|---------|------|------------------|
| Overview | — | `/village/statistics/overview` | attacks, buildings queued, troops training, merchants |
| Resources | Resources | `/village/statistics/resources` | absolute stocks + merchants |
| Resources | Warehouse | `/village/statistics/resources/warehouse` | **fill/empty countdowns** (net crop) |
| Resources | Production | `/village/statistics/resources/production` | **GROSS** production — no troop feeding |
| Resources | Capacity | `/village/statistics/resources/capacity` | warehouse + granary caps |
| Culture points | — | `/village/statistics/culturepoints` | CP/day, celebrations, settlement slots |
| Troops | Own troops | `/village/statistics/troops` | troop counts **by owning village** |
| Troops | Troops in village | *(unconfirmed)* | troops **by station** + their crop upkeep |
| Troops | Smithy / Hospital / Training | *(unconfirmed)* | research levels, wounded, training queues |

> **Own troops vs Troops in village.** Consumption is charged where troops
> *stand*, not where they were built. "Own troops" therefore cannot be used to
> compute crop upkeep: a village's own army may be reinforcing elsewhere (eating
> the host's crop), while foreign reinforcements eat this village's. "Troops in
> village" is the station-keyed view and reports upkeep directly.

## Top Bar — Stock Bar (HTML)

The top bar shows resources with tooltips containing production info:

```html
<div id="stockBar">
    <div class="warehouse">
        <div class="capacity" title="Warehouse">
            <i class="warehouse_medium"></i>
            <div class="value">9,600</div>
        </div>
    </div>

    <a class="stockBarButton resource1" href="/production.php?t=lumber"
       title="Lumber||Production: 654<br />Full in: 6:02:23<br />Click for more information">
        <i class="lumber_small"></i>
        <div id="l1" class="value">5,650</div>
        <div class="barBox"><!-- progress bar --></div>
    </a>

    <!-- Same for clay (resource2), iron (resource3), crop (resource4) -->

    <div class="granary">
        <div class="capacity" title="Granary">
            <i class="granary_medium"></i>
            <div class="value">17,600</div>
        </div>
    </div>

    <div id="stockBarFreeCrop" class="value">1,084</div>
</div>
```

**Tooltip data per resource:**
- `Production: {rate}` — hourly production
- `Full in: {HH:MM:SS}` — time until storage is full
- Links to `/production.php?t={type}` for details

---

## Detailed Production Overview (Server-Rendered viewData)

The production overview page (`/production.php?t={type}`) contains **comprehensive per-building breakdown** as embedded JSON in a React render call.

### URL

```
/production.php?t=lumber    → Lumber production breakdown
/production.php?t=clay      → Clay production breakdown
/production.php?t=iron      → Iron production breakdown
/production.php?t=crop      → Crop production breakdown
/production.php?t=balance   → FREE CROP BALANCE (the most useful one)
```

### Extraction

```python
html = session.get(f"{BASE}/production.php?t=balance").text
match = re.search(r'ProductionOverview\.render\((\{.*?\})\s*,\s*\[', html, re.DOTALL)
data = json.loads(match.group(1))
view = data['viewData']
```

### Per-Resource Data Structure (Verified)

Each resource (`lumber`, `clay`, `iron`, `crop`) contains:

```json
{
    "resourceTypeId": 1,
    "resourceTypeName": "lumber",
    "productionPerHour": [
        {"title": "Woodcutter", "gid": 1, "production": 70, "stufe": 6, "bonus": 0},
        {"title": "Woodcutter", "gid": 1, "production": 70, "stufe": 6, "bonus": 0},
        {"title": "Woodcutter", "gid": 1, "production": 140, "stufe": 8, "bonus": 0},
        {"title": "Woodcutter", "gid": 1, "production": 140, "stufe": 8, "bonus": 0}
    ],
    "productionPerHourTotal": {"production": 420, "bonus": 0},
    "productionOfHero": 234,
    "hasPlayerBoost": false,
    "premiumFeatureActive": false,
    "premiumFeatureCosts": 5,
    "premiumFeatureFactor": 25,
    "premiumFeatureDuration": 604800,
    "premiumFeatureProductionBoost": 164,
    "interimBalanceSheet": 654,
    "productionBoostFactor": 25,
    "productionBoost": 164,
    "balanceSheet": 654,
    "buildings": [],
    "waterworks": {"level": 0, "bonus": 0},
    "productionBonusOases": {"amount": 0, "bonus": 0},
    "productionBonusPercentage": 0,
    "egyptians": false,
    "hasCompensationBoost": false,
    "compensationBoostFactor": null,
    "compensationBoostValue": 0
}
```

| Field | Description |
|-------|-------------|
| `productionPerHour` | Array of each building's contribution (title, gid, production, level, bonus) |
| `productionPerHourTotal` | Sum of all buildings `{production, bonus}` |
| `productionOfHero` | Hero's resource production contribution |
| `balanceSheet` | **Final production rate** (total + hero + boosts) |
| `interimBalanceSheet` | Production before premium boost |
| `premiumFeatureActive` | Whether +25% boost is active |
| `premiumFeatureProductionBoost` | How much extra the +25% boost gives |
| `productionBoost` | Current active boost value |
| `productionBonusOases` | Bonus from occupied oases `{amount, bonus}` |
| `productionBonusPercentage` | Total percentage bonus (oases + buildings) |
| `buildings` | Bonus buildings (e.g., Grain Mill, Bakery for crop) |
| `waterworks` | Waterworks bonus (Egyptians only) |
| `stufe` | Building level (German for "level") |

### Crop-Specific Fields

Crop has additional `buildings` showing bonus production buildings:

```json
"buildings": [
    {"title": "Bakery", "gid": 9, "stufe": 2, "bonus": 0.1},
    {"title": "Grain Mill", "gid": 8, "stufe": 5, "bonus": 0.25}
]
```

---

## Crop Balance — The Critical Data

The `balance` tab contains the **complete crop economy breakdown**:

```json
{
    "balance": {
        "hasPlayerBoost": false,
        "interimBalanceSheet": 1690,
        "productionBoostFactor": 25,
        "productionBoost": 423,
        "premiumFeatureActive": false,
        "premiumFeatureProductionBoost": 423,
        "productionOfBuildingsAndOasis": 1450,
        "consumptionOfVillagersAndConstructions": -366,
        "freeCrop": 1084,
        "notFinishedConstructionCommand": 0,
        "productionOfHero": 240,
        "interimBalance": 1324,
        "ownTroops": {
            "inVillage": 801,
            "reinforcements": 0,
            "onTheWay": 5,
            "forwarded": null,
            "inOasis": 0,
            "caught": 0,
            "horseDrinkingTrough": null,
            "artefacts": 0,
            "ownTroops": true,
            "sum": 806
        },
        "foreignTroops": {
            "inVillage": 0,
            "reinforcements": 0,
            "onTheWay": 0,
            "inOasis": 0,
            "caught": 0,
            "ownTroops": false,
            "sum": 0
        },
        "totalSum": 518
    }
}
```

| Field | Description |
|-------|-------------|
| `productionOfBuildingsAndOasis` | Raw crop production from fields + oases |
| `consumptionOfVillagersAndConstructions` | Population consumption (negative) |
| `freeCrop` | **Net free crop** (production - all consumption) |
| `productionOfHero` | Hero's crop contribution |
| `interimBalance` | Balance before troop consumption |
| `ownTroops.inVillage` | Crop consumed by own troops in village |
| `ownTroops.reinforcements` | Crop consumed by reinforcements you sent |
| `ownTroops.onTheWay` | Crop consumed by troops in transit |
| `ownTroops.inOasis` | Crop consumed by troops in oases |
| `ownTroops.caught` | Crop consumed by trapped troops |
| `ownTroops.sum` | Total own troop consumption |
| `foreignTroops.sum` | Total foreign troop consumption (reinforcements from allies) |
| `totalSum` | **Final crop balance** (can be negative = starving) |
| `notFinishedConstructionCommand` | Extra consumption from buildings under construction |

### The Crop Balance Formula

```
totalSum = productionOfBuildingsAndOasis
         + productionOfHero
         + productionBoost (if premium active)
         - consumptionOfVillagersAndConstructions
         - ownTroops.sum
         - foreignTroops.sum
```

If `totalSum < 0`, your village is starving — troops will start dying.

---

## GraphQL — What's Available (and What's Not)

GraphQL does **NOT** expose production/storage data:

```graphql
# These return empty - production/storage fields don't exist in GQL schema:
{ ownPlayer { village { id production { lumber } } } }     → empty
{ ownPlayer { village { id storage { lumber } } } }        → empty
{ ownPlayer { village { id resourceFields { id } } } }     → empty
{ ownPlayer { village { id warehouse granary } } }          → empty
```

**What GraphQL DOES provide:**
```graphql
{ ownPlayer { village { id name population loyalty x y } } }
```

Population and loyalty are available, but resource production/storage must come from HTML.

---

## Per-Village Resource Data

Switch village with `?newdid={villageId}` then read:

```python
def get_all_resource_data(session, village_id):
    """Get complete resource data for a village"""
    # Quick data (from any page)
    html = session.get(f"{BASE}/dorf1.php?newdid={village_id}").text
    res_match = re.search(r'var resources = (\{.*?\});', html, re.DOTALL)
    quick = json.loads(res_match.group(1)) if res_match else None
    
    # Detailed breakdown (from production page)
    html2 = session.get(f"{BASE}/production.php?t=balance").text
    detail_match = re.search(
        r'ProductionOverview\.render\((\{.*?\})\s*,\s*\[', html2, re.DOTALL)
    detail = json.loads(detail_match.group(1))['viewData'] if detail_match else None
    
    return {
        'quick': quick,
        'detail': detail
    }

# Example: Get data for all villages
for vid in [20030, 20031]:
    data = get_all_resource_data(session, vid)
    q = data['quick']
    d = data['detail']
    
    print(f"\n=== Village {vid} ===")
    print(f"Lumber: {q['storage']['l1']}/{q['maxStorage']['l1']} (+{q['production']['l1']}/h)")
    print(f"Clay:   {q['storage']['l2']}/{q['maxStorage']['l2']} (+{q['production']['l2']}/h)")
    print(f"Iron:   {q['storage']['l3']}/{q['maxStorage']['l3']} (+{q['production']['l3']}/h)")
    print(f"Crop:   {q['storage']['l4']}/{q['maxStorage']['l4']} (+{q['production']['l4']}/h)")
    print(f"Free Crop: {q['production']['l5']}/h")
    
    if d and 'balance' in d:
        b = d['balance']
        print(f"Crop Balance: {b['totalSum']}/h")
        print(f"  Production: {b['productionOfBuildingsAndOasis']}")
        print(f"  Population: {b['consumptionOfVillagersAndConstructions']}")
        print(f"  Own Troops: {b['ownTroops']['sum']}")
        print(f"  Hero:       {b['productionOfHero']}")
```

---

## Summary — Where to Get What

| Data | Source | Method |
|------|--------|--------|
| Current resource amounts | `var resources` on any page | Parse `storage.l1`–`l4` |
| Production rates | `var resources` on any page | Parse `production.l1`–`l4` |
| **Net crop / starvation (one village)** | `var resources` on any page | Parse `production.l4` — **not `l5`** |
| **Net crop for ALL villages** | `/village/statistics/resources` + `.../warehouse` | Stocks ÷ countdown, sign from `crit` — 2 requests |
| Gross crop production (no feeding) | `/village/statistics/resources/production` | Per-village table |
| Warehouse capacity | `var resources` on any page | Parse `maxStorage.l1`–`l3` |
| Granary capacity | `var resources` on any page | Parse `maxStorage.l4` |
| Per-building breakdown | `/production.php?t={type}` | Parse `ProductionOverview.render()` viewData |
| Crop balance with troop consumption | `/production.php?t=balance` | Parse `viewData.balance` |
| Oasis bonuses | `/production.php?t={type}` | Parse `productionBonusOases` |
| Bonus buildings (Mill, Bakery) | `/production.php?t=crop` | Parse `buildings` array |
| Hero production contribution | `/production.php?t={type}` | Parse `productionOfHero` |
| Premium boost status | `/production.php?t={type}` | Parse `premiumFeatureActive` |
| Time until full | Stock bar tooltip | Parse `title` attribute on `.stockBarButton` |
