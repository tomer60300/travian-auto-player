# Buildings & Resources System

This document covers reading village state (buildings, resources, construction queue), upgrading buildings/resources, cancelling construction, and using video rewards to speed up upgrades.

---

## Reading Village State

### Current Resources (Inline Script)

Every page includes real-time resource data in an inline `<script>`:

```javascript
var resources = {
    production: {"l1": 654, "l2": 570, "l3": 583, "l4": 520, "l5": 1084},
    storage:    {"l1": 3940, "l2": 3816, "l3": 3420, "l4": 9607},
    maxStorage: {"l1": 9600, "l2": 9600, "l3": 9600, "l4": 14400}
};
```

| Key | Description |
|-----|-------------|
| `l1` | Lumber |
| `l2` | Clay |
| `l3` | Iron |
| `l4` | Crop |
| `l5` | Free crop (net crop production after troop consumption) |
| `production` | Per-hour production rates |
| `storage` | Current resource amounts |
| `maxStorage` | Warehouse/granary capacity |

> **Note:** `storage` values tick up in real-time client-side. To get a precise server value, use the timestamp from `Travian.Game.timestamp` and calculate: `current = storage + production/3600 * (now - timestamp)`.

### Resource Fields (dorf1.php)

Resource fields are `<a>` tags in `#resourceFieldContainer`:

```html
<a href="/build.php?id=1"
   class="notNow level colorLayer resourceField gid1 buildingSlot1 level6"
   data-aid="1" data-gid="1"
   title="Woodcutter Level 6||Cost for upgrading...">
    <div class="labelLayer">6</div>
</a>
```

**Attributes:**
| Attribute | Description |
|-----------|-------------|
| `data-aid` | Slot ID (1-18 for resource fields) |
| `data-gid` | Building type ID (1=Woodcutter, 2=Clay Pit, 3=Iron Mine, 4=Cropland) |
| `class` contains `levelN` | Current level |
| `class` contains `gidN` | Building type |
| `class` contains `notNow` | Cannot upgrade now (insufficient resources or queue full) |

### Village Buildings (dorf2.php)

Building slots are `<a>` tags in `#village_map`:

```html
<a href="/build.php?id=19"
   class="level colorLayer buildingSlot19 gid46 level1"
   data-aid="19" data-gid="46"
   title="Hospital Level 1||...">
    <div class="labelLayer">1</div>
</a>
```

**Attributes:** Same as resource fields.
- `data-aid` — Slot ID (19-40 for village buildings, plus special slots)
- `data-gid` — Building type ID (see `docs/09-game-constants.md`)

### Empty Building Slots

Empty slots have `data-gid="0"`:
```html
<a href="/build.php?id=25"
   class="level colorLayer buildingSlot25 gid0 level0"
   data-aid="25" data-gid="0"
   title="Empty slot">
</a>
```

---

## GraphQL: Village Data

### Village with Buildings

```graphql
{
    ownPlayer {
        village {
            id
            tribeId
            name
            sortIndex
            population
            loyalty
            x
            y
        }
        villageList {
            ... on VillageListVillage {
                id name x y distance
                incomingAttacksAmount
                incomingAttacksSymbols { gray green red yellow }
            }
        }
    }
}
```

### Available Troops at Village (from Farm List fragment)

```graphql
{
    farmList(id: $id) {
        ownerVillage {
            id
            troops {
                ownTroopsAtTown {
                    units { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
                }
            }
        }
    }
}
```

---

## Construction Queue

### Reading the Queue

The construction queue is displayed in the `<div class="buildingList">` section on both dorf1.php and dorf2.php:

```html
<div class="buildingList">
    <h5>Building:</h5>
    <div class="finishNow">
        <button class="textButtonV1 gold">Complete construction immediately</button>
    </div>
    <ul>
        <li>
            <a onclick="showCancelBuildingDialog(3802250, ...)">
                <img class="del" title="cancel" />
            </a>
            <div class="name">
                Granary <span class="lvl">Level 12</span>
            </div>
            <div class="buildDuration">
                <span class="timer" counting="down" value="1516">0:25:16</span>
                hrs. done at 16:27
            </div>
        </li>
        <!-- Additional queue items (with Travian Plus) -->
    </ul>
</div>
```

**Parsing the queue:**
- Each `<li>` is one queued construction
- `showCancelBuildingDialog(eventId, ...)` — the `eventId` is the construction event ID
- `.name` — Building name + target level
- `.timer[counting="down"]` — Time remaining (seconds in `value` attribute)
- Queue length: 1 item without Travian Plus, 2 with Travian Plus

### Queue Programmatic Access

The queue is **server-rendered HTML** — no REST/GraphQL endpoint lists active constructions. Parse it from dorf1.php or dorf2.php HTML.

```python
import re
r = s.get(f"{BASE}/dorf1.php")
queue_items = re.findall(
    r'showCancelBuildingDialog\(\s*(\d+).*?class="name">\s*(\w[\w\s]*?)\s*<span class="lvl">Level (\d+)',
    r.text, re.DOTALL)
for event_id, building, level in queue_items:
    print(f"Building: {building}, Target Level: {level}, Event ID: {event_id}")

timer_match = re.search(r'class="timer" counting="down" value="(\d+)"', r.text)
if timer_match:
    print(f"Time remaining: {int(timer_match.group(1))} seconds")
```

---

## Upgrading Buildings

### Mechanism: URL Redirect with Checksum

Building upgrades use a **GET redirect** (not a POST API call). The upgrade button's onclick navigates to:

```
/dorf1.php?id={slotId}&gid={buildingTypeId}&action=build&checksum={hash}
```

Or for village buildings:
```
/dorf2.php?id={slotId}&gid={buildingTypeId}&action=build&checksum={hash}
```

**Parameters:**
| Parameter | Description |
|-----------|-------------|
| `id` | Building slot ID (1-40) |
| `gid` | Building type ID |
| `action` | Always `build` |
| `checksum` | CSRF token (6-char hex, server-generated per page load) |

### Upgrade Button HTML

```html
<button type="button" class="textButtonV1 gold builder"
        onclick="this.disabled = true;
                 window.location.href = '/dorf1.php?id=1&amp;gid=1&amp;action=build&amp;checksum=bd2feb&amp;buildmaster';
                 return false;"
        coins="1">
    Construct with master builder
    <i class="goldIcon"></i><span class="goldValue">1</span>
</button>
```

### Upgrade Variants

| URL Suffix | Type | Cost |
|------------|------|------|
| `&action=build&checksum={hash}` | Normal upgrade | Resources only (free queue slot required) |
| `&action=build&checksum={hash}&buildmaster` | Master Builder upgrade | Resources + 1 Gold (uses second queue slot) |

### Free Upgrade Button (when no queue conflict)

When the queue is empty, the standard upgrade button is:
```
/dorf1.php?id={slotId}&gid={gid}&action=build&checksum={hash}
```

This button appears as a green "Upgrade to level N" button.

### Building Upgrade Details Page

Each building slot has a detail page at `/build.php?id={slotId}`:

```html
<div class="upgradeBuilding">
    <!-- Cost breakdown -->
    <div id="contract" class="contractWrapper">
        <div class="inlineIconList resourceWrapper">
            <div class="inlineIcon resource" title="Lumber">
                <i class="r1Big"></i><span class="value">870</span>
            </div>
            <div class="inlineIcon resource" title="Clay">
                <i class="r2Big"></i><span class="value">2170</span>
            </div>
            <div class="inlineIcon resource" title="Iron">
                <i class="r3Big"></i><span class="value">1085</span>
            </div>
            <div class="inlineIcon resource" title="Crop">
                <i class="r4Big"></i><span class="value">1300</span>
            </div>
            <div class="inlineIcon resource" title="Free crop">
                <i class="cropConsumptionBig"></i><span class="value">2</span>
            </div>
        </div>
    </div>

    <!-- Benefits -->
    <div class="buildingBenefits">
        <div class="unit" title="Total culture points granted by this building">
            <i class="culturePoints_medium"></i>
            <span class="value">3</span>
            <span class="delta">(+1)</span>
        </div>
        <div class="unit" title="Total population granted by this building">
            <i class="population_medium"></i>
            <span class="value">8</span>
            <span class="delta">(+2)</span>
        </div>
    </div>

    <!-- Duration -->
    <div class="inlineIcon duration">
        <i class="clock_medium"></i>
        <span class="value">1:43:20</span>
    </div>

    <!-- Production info (resource fields only) -->
    <table id="build_value">
        <tr class="currentLevel">
            <th>Current production:</th>
            <td><span class="number">70</span> per hour</td>
        </tr>
        <tr class="nextPossible">
            <th>Production at level 7:</th>
            <td><span class="number">98</span> per hour</td>
        </tr>
    </table>
</div>
```

### Constructing a New Building (Empty Slot)

For empty slots (`gid=0`), `/build.php?id={slotId}` shows a list of available buildings. Each has an upgrade link:
```
/dorf2.php?id={slotId}&gid={newBuildingGid}&action=build&checksum={hash}
```

### Programmatic Upgrade

```python
# 1. Fetch the building page to get the checksum
r = s.get(f"{BASE}/build.php?id=1")
checksum = re.search(r'checksum=([a-f0-9]+)', r.text).group(1)

# 2. Navigate to the upgrade URL
r2 = s.get(f"{BASE}/dorf1.php?id=1&gid=1&action=build&checksum={checksum}")
# If successful, redirects back to dorf1.php with the building queued
```

---

## Cancelling Construction

### Mechanism: URL Redirect with Event ID

```
/dorf1.php?cancel={eventId}&action=build&checksum={hash}
```

Or for village buildings:
```
/dorf2.php?cancel={eventId}&action=build&checksum={hash}
```

### Cancel Dialog JS Function

```javascript
function showCancelBuildingDialog(eventId, title, text) {
    new Travian.Dialog.Confirmation({
        title: title,
        message: text,
        confirmText: 'Abort construction',
        cancelText: 'Continue construction',
        confirmClass: 'grey negativeAction',
        cancelClass: 'grey',
        buttonCancel: true,
        context: 'cancelBuilding',
        onCancel: function() {
            Travian.WindowManager.closeByContext('cancelBuilding');
        },
        onConfirm: function() {
            window.location.href = '?cancel=' + eventId + '&action=build&checksum=bd2feb';
        }
    }).show();
}
```

**Parameters:**
| Parameter | Description |
|-----------|-------------|
| `cancel` | Construction event ID (e.g., `3802250`) |
| `action` | Always `build` |
| `checksum` | Same CSRF token as upgrade |

**Refund:** Cancelling refunds **78%** of the construction costs (verified from cancel dialog text).

### Programmatic Cancel

```python
# Parse event ID from queue
r = s.get(f"{BASE}/dorf1.php")
event_id = re.search(r'showCancelBuildingDialog\(\s*(\d+)', r.text).group(1)
checksum = re.search(r'checksum=([a-f0-9]+)', r.text).group(1)

# Cancel
r2 = s.get(f"{BASE}/dorf1.php?cancel={event_id}&action=build&checksum={checksum}")
```

---

## Instant Completion (Gold)

### "Complete construction immediately" Button

```html
<button class="textButtonV1 gold" 
        title="More information about completing construction immediately">
    Complete construction immediately
</button>
```

This button triggers a dialog via:
```json
{
    "cmd": "building/instant-completion-popup",
    "data": [],
    "context": "finishNow"
}
```

The popup shows the Gold cost and calls the instant completion API. This is a Gold-only feature (costs Gold per completion).

---

## Video Reward — Building Upgrade Speed-Up

### Video Reward Types

| Type | Description |
|------|-------------|
| `buildingUpgrade` | Speed up building construction time |
| `productionBoost` | +25% resource production boost |
| `adventureDuration` | Reduce adventure travel time |
| `smithyUpgrade` | Speed up smithy research |
| `adventureDifficulty` | Reduce adventure difficulty |

### Video Feature Protocol (Building Upgrade)

1. **Start video:** `POST /api/v1/videofeature/start`
   ```json
   {"vrid": "{videoRewardId}"}
   ```
   The `vrid` is provided by the AdScale ad provider.

2. **Complete video:** `POST /api/v1/videofeature/ends`
   ```json
   {"vrid": "{vrid}", "hash": "{completionHash}"}
   ```
   The `vrid:hash` pair comes from the ad provider's `postMessage` event: `"videoEnds:{vrid}:{hash}"`.

3. **Server validates** the hash, grants the reward (time reduction or production boost).

### Video Feature User Preferences

Stored in `Travian.Game.Preferences`:
```json
{
    "videoFeatureVideoInfoScreen": {
        "buildingUpgrade": false,
        "productionBoost": false,
        "adventureDuration": false,
        "smithyUpgrade": false,
        "adventureDifficulty": false
    }
}
```

When `false`, the info dialog is shown before playing. When `true`, video plays directly.

### Production Boost Button

The +25% production boost button appears on dorf1.php:
```html
<button class="textButtonV1 gold productionBoostButton"
        title="More information about the production bonus.">
    +25%
</button>
```

This triggers the `productionBoost` video reward flow or shows a purchase dialog.

> See `docs/11-video-reward-protocol.md` for the complete video reward protocol (iframe postMessage, AdScale integration, timing).

---

## Building Types Reference

### Resource Field Types (dorf1.php, slots 1-18)

| gid | Building | Resource |
|-----|----------|----------|
| 1 | Woodcutter | Lumber |
| 2 | Clay Pit | Clay |
| 3 | Iron Mine | Iron |
| 4 | Cropland | Crop |

### Village Building Types (dorf2.php, slots 19-40)

| gid | Building | Description |
|-----|----------|-------------|
| 8 | Barracks | Train infantry |
| 9 | Grain Mill | +production bonus |
| 10 | Warehouse | Increase resource storage |
| 11 | Granary | Increase crop storage |
| 13 | Smithy | Research troop upgrades |
| 14 | Tournament Square | Increase troop speed |
| 15 | Main Building | Reduce construction time |
| 16 | Rally Point | Send troops |
| 17 | Marketplace | Trade resources |
| 18 | Embassy | Alliance functions |
| 19 | Barracks | Train infantry |
| 20 | Stable | Train cavalry |
| 21 | Workshop | Train siege |
| 22 | Academy | Research buildings |
| 23 | Cranny | Protect resources from raids |
| 24 | Town Hall | Celebrations |
| 25 | Residence | Train settlers/chiefs, expansion |
| 26 | Palace | Alternative to Residence |
| 27 | Treasury | Alliance treasures |
| 28 | Trade Office | Increase merchant capacity |
| 29 | Great Barracks | Train faster infantry |
| 30 | Great Stable | Train faster cavalry |
| 31 | City Wall (Romans) | Defense bonus |
| 32 | Earth Wall (Teutons) | Defense bonus |
| 33 | Palisade (Gauls) | Defense bonus |
| 34 | Stonemason | Increase building durability |
| 35 | Brewery (Teutons) | Attack bonus |
| 36 | Trapper (Gauls) | Trap enemy troops |
| 37 | Hero's Mansion | Hero management |
| 38 | Great Warehouse | Large resource storage |
| 39 | Great Granary | Large crop storage |
| 46 | Hospital | Heal wounded troops |

---

## JavaScript API

### `Travian.Game.BuildingUpgradeView`

```javascript
Travian.Game.BuildingUpgradeView.initialize();
```

Initializes the building detail page with upgrade buttons and collapsible description.

### Resource Counters

```javascript
Travian.TimersAndCounters.initResourcesCounters();
```

Starts the client-side resource counting (increments storage values based on production rates).

---

## Programmatic Building Management — Complete Flow

### 1. Read All Building Levels

```python
import re, requests

# Resource fields (dorf1.php)
r = s.get(f"{BASE}/dorf1.php")
fields = re.findall(r'data-aid="(\d+)" data-gid="(\d+)".*?level(\d+)', r.text)
for slot, gid, level in fields:
    print(f"Slot {slot}: gid={gid}, level={level}")

# Village buildings (dorf2.php)  
r = s.get(f"{BASE}/dorf2.php")
buildings = re.findall(r'data-aid="(\d+)" data-gid="(\d+)".*?level(\d+)', r.text)
for slot, gid, level in buildings:
    print(f"Slot {slot}: gid={gid}, level={level}")
```

### 2. Read Construction Queue

```python
r = s.get(f"{BASE}/dorf1.php")
# Active constructions
queue = re.findall(
    r'showCancelBuildingDialog\(\s*(\d+).*?class="name">\s*([\w\s]+?)\s*<span class="lvl">Level (\d+).*?value="(\d+)"',
    r.text, re.DOTALL)
for event_id, name, level, seconds in queue:
    print(f"Building: {name.strip()}, Level: {level}, Remaining: {seconds}s, EventID: {event_id}")
```

### 3. Check Resources & Decide

```python
res_match = re.search(r'var resources = (\{.*?\});', r.text, re.DOTALL)
if res_match:
    import json
    resources = json.loads(res_match.group(1).replace("'", '"'))
    print(f"Lumber: {resources['storage']['l1']}/{resources['maxStorage']['l1']}")
```

### 4. Upgrade a Building

```python
# Get checksum from any page
checksum = re.search(r'checksum=([a-f0-9]+)', r.text).group(1)

# Upgrade Woodcutter (slot 1, gid 1) 
r = s.get(f"{BASE}/dorf1.php?id=1&gid=1&action=build&checksum={checksum}")
```

### 5. Cancel Construction

```python
r = s.get(f"{BASE}/dorf1.php?cancel={event_id}&action=build&checksum={checksum}")
```
