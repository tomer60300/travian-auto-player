# Multi-Village Management

This document covers identifying, switching, and controlling multiple villages programmatically.

---

## Identifying Villages

### GraphQL — Complete Village List

```graphql
{
    ownPlayer {
        name
        tribeId
        village {
            id tribeId name sortIndex population loyalty x y hasHarbour
        }
        villageList {
            ... on VillageListGroup {
                id name color
                villages {
                    id name distance
                    incomingAttacksAmount
                    incomingAttacksSymbols { gray green red yellow }
                    x y
                }
            }
            ... on VillageListVillage {
                id name distance
                incomingAttacksAmount
                incomingAttacksSymbols { gray green red yellow }
                x y
            }
        }
        culturalPointsOverview {
            usedSlots
            maxControllableVillages
            cpProducedForNextSlot
            cpNeededForNextSlot
        }
    }
}
```

**Verified Response (2 villages):**
```json
{
    "ownPlayer": {
        "name": "Chieftain",
        "tribeId": 2,
        "village": {
            "id": 20030,
            "tribeId": 2,
            "name": "Chieftain`s village",
            "sortIndex": 1,
            "population": 366,
            "loyalty": 100,
            "x": -161,
            "y": 166,
            "hasHarbour": false
        },
        "villageList": [
            {
                "id": 20030,
                "name": "Chieftain`s village",
                "distance": 0,
                "incomingAttacksAmount": 0,
                "incomingAttacksSymbols": {"gray": 0, "green": 0, "red": 0, "yellow": 0},
                "x": -161, "y": 166
            },
            {
                "id": 20031,
                "name": "New village",
                "distance": 14.14214,
                "incomingAttacksAmount": 0,
                "incomingAttacksSymbols": {"gray": 0, "green": 0, "red": 0, "yellow": 0},
                "x": -163, "y": 180
            }
        ],
        "culturalPointsOverview": {
            "usedSlots": 2,
            "maxControllableVillages": 2,
            "cpProducedForNextSlot": 1671,
            "cpNeededForNextSlot": 6000
        }
    }
}
```

**Key Fields:**

| Field | Description |
|-------|-------------|
| `village` | **Currently active** village (the one the server session is pointing to) |
| `villageList` | All player villages (flat list or grouped) |
| `village.id` | Village ID — the primary identifier for all operations |
| `village.sortIndex` | Display order in sidebar |
| `culturalPointsOverview.usedSlots` | Number of villages owned |
| `culturalPointsOverview.maxControllableVillages` | Maximum villages allowed |
| `incomingAttacksAmount` | Number of incoming attacks to this village |
| `incomingAttacksSymbols` | Attack indicator colors (gray=returning, green=reinforcement, red=attack, yellow=scout) |

### Village ID vs Map ID

- **Village ID** (`did`/`id`): Internal database ID (e.g., `20030`). Used in API calls and `newdid` parameter.
- **Map ID** (`mapId`): Tile ID on the map (e.g., `13674`). Used in map links like `/karte.php?d=13674`.

These are different numbers. Use village ID for all programmatic operations.

---

## Switching Active Village

### The `newdid` Parameter

The active village is switched by adding `?newdid={villageId}` to any page request:

```
/dorf1.php?newdid=20031          → Resource view for village 20031
/dorf2.php?newdid=20031          → Building view for village 20031
/build.php?newdid=20031&id=1     → Building slot 1 in village 20031
/build.php?newdid=20031&gid=16&tt=2  → Rally Point send troops for village 20031
```

**How it works:**
1. The `newdid` parameter tells the server to switch the active village for the session
2. After switching, all subsequent requests (even without `newdid`) stay on the new village
3. The server stores the active village in the session — it persists across requests
4. The browser URL is cleaned up client-side (removes `newdid` from URL bar via `history.replaceState`)

**Verified behavior:**
```
GET /dorf1.php?newdid=20031  → resources for village 20031
GET /dorf1.php               → still village 20031 (session persisted)
GET /dorf1.php?newdid=20030  → back to village 20030
```

### After Switching — Active Village Changes Everywhere

After `?newdid=20031`:
- `ownPlayer.village` in GraphQL returns village 20031
- Resource data (`var resources = {...}`) shows village 20031's resources
- Building pages show village 20031's buildings
- Checksums change (each village has its own CSRF token)
- Troop counts in rally point show village 20031's troops

### Sidebar Village List (HTML)

The sidebar shows all villages with switch links:

```html
<div id="sidebarBoxVillageList">
    <div class="listEntry active" data-did="20030">
        <a href="?newdid=20030&amp;">
            <span class="name" data-did="20030">Chieftain`s village</span>
            <span class="coordinates">(−161|166)</span>
        </a>
    </div>
    <div class="listEntry" data-did="20031">
        <a href="?newdid=20031&amp;">
            <span class="name" data-did="20031">New village</span>
            <span class="coordinates">(−163|180)</span>
        </a>
    </div>
</div>
```

- `class="active"` marks the currently selected village
- `data-did="{villageId}"` on each entry

---

## Controlling Each Village

### Reading State — Per Village

To read any village's state, switch to it first:

```python
import requests, re, json

BASE = "https://ts1.x1.europe.travian.com"
s = requests.Session()
s.cookies.set("JWT", token)
s.headers.update({"Content-Type": "application/json", "X-Version": "389"})

def get_village_state(village_id):
    """Get complete state for a specific village"""
    state = {}
    
    # Switch to village and get resource fields
    r = s.get(f"{BASE}/dorf1.php?newdid={village_id}")
    
    # Parse resources
    res_match = re.search(r'var resources = (\{.*?\});', r.text, re.DOTALL)
    if res_match:
        state['resources'] = json.loads(res_match.group(1).replace("'", '"'))
    
    # Parse resource field levels
    fields = re.findall(r'data-aid="(\d+)"\s+data-gid="(\d+)"', r.text)
    state['resourceFields'] = [{'slot': a, 'gid': g} for a, g in fields]
    
    # Parse construction queue
    queue = re.findall(
        r'showCancelBuildingDialog\(\s*(\d+).*?class="name">\s*([\w\s]+)',
        r.text, re.DOTALL)
    state['queue'] = [{'eventId': e, 'name': n.strip()} for e, n in queue]
    
    # Get buildings
    r2 = s.get(f"{BASE}/dorf2.php")  # Already on this village
    buildings = re.findall(r'data-aid="(\d+)"\s+data-gid="(\d+)"', r2.text)
    state['buildings'] = [{'slot': a, 'gid': g} for a, g in buildings]
    
    return state

# Get state for all villages
villages = [20030, 20031]
for vid in villages:
    state = get_village_state(vid)
    print(f"Village {vid}: {json.dumps(state, indent=2)}")
```

### Building Upgrades — Village Context

Upgrades use the currently active village. The checksum is village-specific:

```python
# Switch to village 2 and upgrade
r = s.get(f"{BASE}/build.php?newdid=20031&id=1")
checksum = re.search(r'checksum=([a-f0-9]+)', r.text).group(1)

# Upgrade — note: dorf1.php (resources) or dorf2.php (buildings) 
# The target URL must match where the building is
r2 = s.get(f"{BASE}/dorf1.php?id=1&gid=3&action=build&checksum={checksum}")
```

> **⚠️ Checksum is per-village:** Each village generates its own CSRF checksum. You must fetch the page for the target village first to get the correct checksum.

### Sending Troops — Village Context

Troops are sent from the currently active village:

```python
# Send troops from village 20031
r = s.get(f"{BASE}/build.php?newdid=20031&gid=16&tt=2")
# ... fill form and submit (see docs/13-troop-sending.md)
```

The form includes `troops[0][villageId]` which confirms the source village.

### Farm Lists — Village Binding

Farm lists are bound to a specific village via `ownerVillage.id`:

```graphql
{
    ownPlayer {
        farmLists {
            id
            name
            ownerVillage { id }
        }
    }
}
```

When creating a farm list, the `villageId` parameter determines which village's troops are used:
```json
{
    "villageId": 20031,
    "name": "Village 2 Raids",
    "defaultUnits": {"t1": 5, ...}
}
```

---

## Village Management APIs

### Rename Village

```
PUT /api/v1/village/change-names
```

**Request:**
```json
{
    "data": [
        {"villageId": 20030, "name": "Main Village"},
        {"villageId": 20031, "name": "Farm Village"}
    ]
}
```

Can rename multiple villages in one call.

### Reorder Villages (Sort Index)

```
POST /api/v1/village/{villageId}/update-sort-index
```

**Request:**
```json
{
    "to": 2
}
```

Changes the village's position in the sidebar list.

### Get Village Names

```
GET /api/v1/villageList/getVillageNames
```

**Response:**
```json
{
    "villageNames": {
        "20030": "Chieftain`s village",
        "20031": "New village"
    }
}
```

---

## Village Groups

Village groups organize villages in the sidebar with colors.

### Create Group

```
POST /api/v1/village-group
```

**Request:**
```json
{
    "name": "My Group",
    "color": "grey"
}
```

**Colors:** `grey` (and other predefined colors from `xo.GREY` etc.)

### Update Group

```
PUT /api/v1/village-group/{groupId}
```

**Request:**
```json
{
    "name": "Renamed Group",
    "color": "blue"
}
```

### Delete Group

```
DELETE /api/v1/village-group/{groupId}
```

---

## Complete Multi-Village Automation Pattern

### Iterate All Villages

```javascript
// Get all villages
Travian.graphQL({
    query: '{ ownPlayer { villageList { ... on VillageListVillage { id name x y } } } }'
}, function(data) {
    var villages = data.data.ownPlayer.villageList;
    villages.forEach(function(v) {
        console.log('Village: ' + v.name + ' (ID: ' + v.id + ') at (' + v.x + '|' + v.y + ')');
    });
});
```

### Per-Village Operations (Python)

```python
def for_each_village(session, village_ids, action_fn):
    """Execute an action for each village"""
    results = {}
    for vid in village_ids:
        # Switch to village
        session.get(f"{BASE}/dorf1.php?newdid={vid}")
        # Execute action
        results[vid] = action_fn(session, vid)
    return results

# Example: Get resources for all villages
def get_resources(session, vid):
    r = session.get(f"{BASE}/dorf1.php")
    match = re.search(r'var resources = (\{.*?\});', r.text, re.DOTALL)
    return json.loads(match.group(1)) if match else None

village_ids = [20030, 20031]
all_resources = for_each_village(s, village_ids, get_resources)
for vid, res in all_resources.items():
    print(f"Village {vid}: Lumber={res['storage']['l1']}, "
          f"Clay={res['storage']['l2']}, Iron={res['storage']['l3']}, "
          f"Crop={res['storage']['l4']}")
```

### Per-Village Building Upgrade

```python
def upgrade_building(session, vid, slot_id, gid, page='dorf1'):
    """Upgrade a building in a specific village"""
    # Switch and get checksum
    r = session.get(f"{BASE}/build.php?newdid={vid}&id={slot_id}")
    checksum_match = re.search(r'checksum=([a-f0-9]+)', r.text)
    if not checksum_match:
        return {"error": "no checksum found - cannot upgrade"}
    checksum = checksum_match.group(1)
    
    # Execute upgrade
    r2 = session.get(f"{BASE}/{page}.php?id={slot_id}&gid={gid}&action=build&checksum={checksum}")
    return {"status": r2.status_code, "url": r2.url}
```

### Per-Village Troop Training Status

```python
def get_troop_training(session, vid, building_gid):
    """Check troop training queue for a barracks/stable/workshop"""
    # building_gid: 19=barracks, 20=stable, 21=workshop
    r = session.get(f"{BASE}/build.php?newdid={vid}&gid={building_gid}")
    # Parse training queue from HTML
    queue = re.findall(
        r'class="timer" counting="down" value="(\d+)"',
        r.text)
    return {"village": vid, "building": building_gid, "timers": queue}
```

---

## Key Concepts Summary

| Concept | How It Works |
|---------|-------------|
| **Village count** | `ownPlayer.villageList.length` or `culturalPointsOverview.usedSlots` |
| **Active village** | `ownPlayer.village.id` — the one the session is pointing to |
| **Switch village** | Add `?newdid={villageId}` to any page URL |
| **Village-specific data** | All page content (resources, buildings, queue, troops) reflects the active village |
| **Checksum scope** | Each village has its own CSRF checksum — fetch the page after switching |
| **API calls** | GraphQL and REST calls use the active village from the session |
| **Farm list binding** | Each farm list is bound to a `villageId` (source of troops) |
| **Troop sending source** | `troops[0][villageId]` in confirmation form confirms the source |
| **Persistence** | Village switch persists in session — subsequent requests stay on that village |

### Village ID Quick Reference

```python
# Get village IDs from GraphQL (no page switching needed)
r = s.post(f"{BASE}/api/v1/graphql", json={
    "query": "{ ownPlayer { villageList { ... on VillageListVillage { id name x y population } } } }"
})
villages = r.json()["data"]["ownPlayer"]["villageList"]
village_ids = [v["id"] for v in villages]
```

### Session-Free Village Data

Some data is available for ALL villages without switching:

| Data | Method | Needs Switch? |
|------|--------|:---:|
| Village list (names, coords, population) | GraphQL `villageList` | ❌ |
| Incoming attacks count | GraphQL `incomingAttacksAmount` | ❌ |
| Village count / CP progress | GraphQL `culturalPointsOverview` | ❌ |
| Resource levels | Parse `dorf1.php?newdid=X` | ✅ |
| Building levels | Parse `dorf2.php?newdid=X` | ✅ |
| Construction queue | Parse `dorf1.php?newdid=X` | ✅ |
| Current resources (exact) | Parse `var resources` from any page | ✅ |
| Available troops | Parse rally point or farm list GQL | ✅ |
| Checksum for actions | Parse from target page | ✅ |
