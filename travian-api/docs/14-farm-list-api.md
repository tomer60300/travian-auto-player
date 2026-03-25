# Farm List API

Farm lists are the primary automation mechanism for raids in Travian Legends. They provide **one-click bulk raiding** through proper REST API endpoints, bypassing the two-step troop sending form.

> **⚠️ Gold Club Lock — Partial:** The UI blocks farm list access without Gold Club, but the **API enforcement is inconsistent**:
>
> | Operation | Without Gold Club | Notes |
> |-----------|:-:|-------|
> | **Create farm list** | ✅ Works | `POST /api/v1/farm-list` |
> | **Delete farm list** | ✅ Works | `DELETE /api/v1/farm-list/{id}` |
> | **Add/edit/delete slots** | ✅ Works | `POST/PUT/DELETE /api/v1/farm-list/slot` |
> | **Query via GraphQL** | ✅ Works | `farmList(id:$id)`, `ownPlayer { farmLists }` |
> | **Send raids** | ❌ Blocked | Returns `{"error":"plus.error_goldclub","message":"Your Gold club is not activated."}` |
>
> **In short:** You can manage farm lists (create, add targets, edit, delete) without Gold Club. Only the **send** action is server-enforced. The UI lock is purely cosmetic for CRUD operations.

---

## REST API Endpoints

All endpoints are under `/api/v1/farm-list/`. Method defaults to POST unless specified.

### Create Farm List

```
POST /api/v1/farm-list
```

**Request:**
```json
{
    "villageId": 69130,
    "name": "My Raid List",
    "defaultUnits": {
        "t1": 5,
        "t2": 0,
        "t3": 3,
        "t4": 0,
        "t5": 0,
        "t6": 0,
        "t7": 0,
        "t8": 0,
        "t9": 0,
        "t10": 0
    },
    "useShip": false,
    "onlyLosses": false
}
```

**Fields:**
- `villageId` — Source village ID (troops depart from here)
- `name` — Farm list display name
- `defaultUnits` — Default troop counts for new slots (`t1`–`t10`)
- `useShip` — Use ship for harbor travel
- `onlyLosses` — Only send to targets where last raid had losses

**Response:** `{ "id": 123 }` — the new farm list ID

### Update Farm List

```
PUT /api/v1/farm-list/{listId}
```

**Request:**
```json
{
    "villageId": 69130,
    "name": "Updated Name",
    "defaultUnits": { "t1": 10, "t2": 0, ... },
    "useShip": false,
    "onlyLosses": false,
    "abandoned": false
}
```

**Fields:** Same as create, plus:
- `abandoned` — Whether this is an abandoned/inactive list

### Delete Farm List

```
DELETE /api/v1/farm-list/{listId}
```

**Request:**
```json
{
    "abandoned": false
}
```

### Send Farm List (Execute Raids)

```
POST /api/v1/farm-list/send
```

**Request:**
```json
{
    "action": "farmList",
    "lists": [
        {
            "id": 123,
            "targets": [456, 789, 1011]
        }
    ]
}
```

**Fields:**
- `action` — Always `"farmList"` (matches `Travian.Constants.ACTION.farmList`)
- `lists` — Array of farm lists to send
  - `id` — Farm list ID
  - `targets` — Array of **slot IDs** to raid (active slots only). To send all, include all active slot IDs.

**Optional fields (for "Send All" across multiple lists):**
- `triggeredBySendAll` — Boolean, true when sending all lists at once
- `startedAll` — Boolean, true when the "Send All" button was used

**Response:**
```json
{
    "lists": [
        {
            "targets": [
                { "id": 456, "status": "success" },
                { "id": 789, "status": "success" },
                { "id": 1011, "error": "raidList.notEnoughTroops" }
            ]
        }
    ]
}
```

### Add/Create Slot (Target)

```
POST /api/v1/farm-list/slot
```

**Request:**
```json
{
    "slots": [
        {
            "listId": 123,
            "x": -162,
            "y": 167,
            "units": {
                "t1": 5,
                "t2": 0,
                "t3": 3,
                "t4": 0,
                "t5": 0,
                "t6": 0,
                "t7": 0,
                "t8": 0,
                "t9": 0,
                "t10": 0
            },
            "active": true,
            "abandoned": false
        }
    ]
}
```

**Fields per slot:**
- `listId` — Farm list ID to add this slot to
- `x`, `y` — Target coordinates
- `units` — Troop counts to send to this target (`t1`–`t10`)
- `active` — Whether slot is active (will be included in sends)
- `abandoned` — Whether this is an abandoned target

**Error Handling:** If target already exists, server returns `"raidList.targetExists"`. The UI then prompts and can retry with `?force`:

### Force Add Slot

```
POST /api/v1/farm-list/slot?force
```

Same request body as regular slot creation. Forces adding even if target already exists in another farm list.

### Update Slot(s)

```
PUT /api/v1/farm-list/slot
```

Same body structure as create, but each slot includes an `id` field:

```json
{
    "slots": [
        {
            "id": 456,
            "listId": 123,
            "x": -162,
            "y": 167,
            "units": { "t1": 10, ... },
            "active": true
        }
    ]
}
```

### Force Update Slot(s)

```
PUT /api/v1/farm-list/slot?force
```

Same as update but forces through warnings (e.g., duplicate targets).

### Delete Slot(s)

```
DELETE /api/v1/farm-list/slot
```

**Request:**
```json
{
    "slots": [456, 789],
    "abandoned": false
}
```

- `slots` — Array of slot IDs to delete
- `abandoned` — Whether these are abandoned targets

### Change Expanded State

```
POST /api/v1/farm-list/change-expanded-state
```

**Request:**
```json
{
    "action": "farmList",
    "farmLists": [123, 456],
    "isExpanded": true
}
```

Toggles the expanded/collapsed UI state for farm lists.

### Change Sort Order

```
PUT /api/v1/farm-list/{listId}/change-sorting
```

**Request:**
```json
{
    "sortField": "distance",
    "sortDirection": "asc",
    "abandoned": false
}
```

### Change Sort Index (Drag & Drop Reorder)

```
POST /api/v1/farm-list/{listId}/change-sort-index
```

**Request:**
```json
{
    "newIndex": 2
}
```

### Close Inbox (Dismiss Deactivated Targets Notice)

```
POST /api/v1/farm-list/close-inbox
```

**Request:**
```json
{
    "action": "farmList"
}
```

---

## GraphQL API

### Farm List Fragment

The game uses this fragment for all farm list queries:

```graphql
fragment farmListFragment on FarmList {
    id
    name
    runningRaidsAmount
    isExpanded
    sortIndex
    lastStartedTime
    sortField
    sortDirection
    useShip
    onlyLosses
    ownerVillage {
        id
        troops {
            ownTroopsAtTown {
                units { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
            }
        }
    }
    defaultTroop { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
    slots(onlyExpanded: $onlyExpanded) {
        id
        target { id mapId x y name type population }
        troop { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
        distance
        isActive
        isRunning
        isSpying
        runningAttacks
        nextAttackAt
        lastRaid {
            reportObjectId
            authKey
            time
            raidedResources { lumber clay iron crop }
            bootyMax
            icon
        }
        totalBooty { booty raids }
    }
    slotsAmount
    slotsStates: slots { id isActive }
}
```

### Query: Get Single Farm List

```graphql
query($id: Int!, $onlyExpanded: Boolean!) {
    bootstrapData { timestamp }
    weekendWarrior { isNightTruce }
    farmList(id: $id) {
        ...farmListFragment
    }
}
```

### Query: Get Abandoned Farm List

```graphql
query($id: Int!, $onlyExpanded: Boolean!) {
    abandonedFarmList(id: $id) {
        ...farmListFragment
    }
}
```

### Query: Get All Farm Lists (Page Load)

The initial page load query fetches all farm lists via `ownPlayer.farmLists`:

```graphql
{
    bootstrapData { timestamp }
    weekendWarrior { isNightTruce }
    ownPlayer {
        farmLists { ...farmListFragment }
        abandonedFarmLists { ...farmListFragment }
        deactivatedFarmListTargets
    }
}
```

### GraphQL Response Fields

**FarmList:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | Int | Farm list ID |
| `name` | String | Farm list name |
| `runningRaidsAmount` | Int | Number of currently active raids |
| `isExpanded` | Boolean | UI expanded state |
| `sortIndex` | Int | Display order |
| `lastStartedTime` | Int | Unix timestamp of last send |
| `sortField` | String | Sort field (e.g., "distance") |
| `sortDirection` | String | Sort direction ("asc"/"desc") |
| `useShip` | Boolean | Use ship travel |
| `onlyLosses` | Boolean | Only send to loss targets |
| `ownerVillage` | Object | Source village info + available troops |
| `defaultTroop` | Object | Default troop counts `{t1..t10}` |
| `slots` | Array | Target list entries |
| `slotsAmount` | Int | Total number of slots |
| `slotsStates` | Array | Slot IDs with active state |

**Slot:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | Int | Slot ID |
| `target.id` | Int | Target village ID |
| `target.mapId` | Int | Target map tile ID |
| `target.x`, `target.y` | Int | Target coordinates |
| `target.name` | String | Target village name |
| `target.type` | Int | Village type (0=normal, 3=oasis, etc.) |
| `target.population` | Int | Target population |
| `troop` | Object | Troop counts `{t1..t10}` for this slot |
| `distance` | Float | Distance from source village |
| `isActive` | Boolean | Whether slot is active |
| `isRunning` | Boolean | Whether raid is currently in progress |
| `isSpying` | Boolean | Whether a scout mission is active |
| `runningAttacks` | Int | Number of active attacks to this target |
| `nextAttackAt` | Int | Unix timestamp of next returning attack |
| `lastRaid.reportObjectId` | String | Last raid report ID |
| `lastRaid.authKey` | String | Report auth key (for viewing) |
| `lastRaid.time` | Int | Unix timestamp of last raid |
| `lastRaid.raidedResources` | Object | `{lumber, clay, iron, crop}` from last raid |
| `lastRaid.bootyMax` | Int | Maximum carry capacity |
| `lastRaid.icon` | Int | Report icon type (iReport code) |
| `totalBooty.booty` | Int | Total resources raided from this target |
| `totalBooty.raids` | Int | Total number of raids to this target |

---

## JavaScript API

### `Travian.React.FarmList`

| Method/Property | Description |
|----------------|-------------|
| `render(props, toasts)` | Render the farm list React component |
| `openSlotDialog(props)` | Open the slot create/edit dialog |
| `SLOT_DIALOG_TYPE_CREATE` | `"create"` |
| `SLOT_DIALOG_TYPE_EDIT` | `"edit"` |

### Related Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /api/v1/village/{villageId}/remove-from-farm-lists` | Remove a village from all farm lists |
| `GET /api/v1/waveBuilderDialog/{targetVillageId}` | Get wave builder dialog HTML (Gold Club) |

---

## Programmatic Usage

### List All Farm Lists

```javascript
Travian.graphQL({
    query: `query($onlyExpanded: Boolean!) {
        ownPlayer {
            farmLists {
                id name slotsAmount lastStartedTime
                slots(onlyExpanded: $onlyExpanded) {
                    id target { x y name } distance isActive
                    lastRaid { time raidedResources { lumber clay iron crop } }
                }
            }
        }
    }`,
    variables: { onlyExpanded: false }
}, function(data) {
    console.log(data.data.ownPlayer.farmLists);
});
```

### Send All Active Targets in a Farm List

```javascript
// Get active slot IDs first
var activeSlotIds = farmList.slots.filter(s => s.isActive).map(s => s.id);

Travian.api("farm-list/send", {
    data: {
        action: "farmList",
        lists: [{
            id: farmListId,
            targets: activeSlotIds
        }]
    },
    success: function(response) {
        console.log("Raids sent:", response);
    }
});
```

### Add a Target to a Farm List

```javascript
Travian.api("farm-list/slot", {
    data: {
        slots: [{
            listId: 123,
            x: -162,
            y: 167,
            units: { t1: 5, t2: 0, t3: 3, t4: 0, t5: 0, t6: 0, t7: 0, t8: 0, t9: 0, t10: 0 },
            active: true,
            abandoned: false
        }]
    },
    success: function() { console.log("Slot added"); },
    error: function(err) {
        if (err.error === "raidList.targetExists") {
            // Target already in a list — use ?force to override
            Travian.api("farm-list/slot?force", { data: { slots: [/*same*/] } });
        }
    }
});
```

### Create a New Farm List

```javascript
Travian.api("farm-list", {
    data: {
        villageId: 69130,
        name: "New Raid List",
        defaultUnits: { t1: 5, t2: 0, t3: 3, t4: 0, t5: 0, t6: 0, t7: 0, t8: 0, t9: 0, t10: 0 },
        useShip: false,
        onlyLosses: false
    },
    success: function(data) { console.log("Created list ID:", data.id); }
});
```

---

## Error Codes

| Error | Description |
|-------|-------------|
| `plus.error_goldclub` | Gold Club not activated (blocks `farm-list/send` only) |
| `raidList.targetExists` | Target village already exists in a farm list |
| `raidList.notEnoughTroops` | Not enough troops to send |
| `raidList.error_delete` | Failed to delete farm list |

---

## Verified Live API Responses

### Create Farm List (without Gold Club) ✅

**Request:** `POST /api/v1/farm-list`
```json
{"villageId":69130,"name":"API Test","defaultUnits":{"t1":1,"t2":0,"t3":0,"t4":0,"t5":0,"t6":0,"t7":0,"t8":0,"t9":0,"t10":0},"useShip":false,"onlyLosses":false}
```
**Response:** `{"id": 8839}`

### Add Slot (without Gold Club) ✅

**Request:** `POST /api/v1/farm-list/slot`
```json
{"slots":[{"listId":8839,"x":-162,"y":167,"units":{"t1":1,"t2":0,"t3":0,"t4":0,"t5":0,"t6":0,"t7":0,"t8":0,"t9":0,"t10":0},"active":true,"abandoned":false}]}
```
**Response:** `{}`

### Query Farm List via GraphQL (without Gold Club) ✅

**Query:**
```graphql
query($id: Int!, $onlyExpanded: Boolean!) {
    farmList(id: $id) {
        id name runningRaidsAmount isExpanded sortIndex lastStartedTime
        ownerVillage { id }
        defaultTroop { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
        slots(onlyExpanded: $onlyExpanded) {
            id target { id mapId x y name type population }
            troop { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
            distance isActive isRunning isSpying
            lastRaid { reportObjectId time raidedResources { lumber clay iron crop } bootyMax icon }
            totalBooty { booty raids }
        }
        slotsAmount
    }
}
```
**Response:**
```json
{
    "data": {
        "farmList": {
            "id": 8839,
            "name": "API Test",
            "runningRaidsAmount": 0,
            "isExpanded": true,
            "sortIndex": 1,
            "lastStartedTime": null,
            "ownerVillage": {"id": 69130},
            "defaultTroop": {"t1":1,"t2":0,"t3":0,"t4":0,"t5":0,"t6":0,"t7":0,"t8":0,"t9":0,"t10":0},
            "slots": [{
                "id": 372120,
                "target": {"id":69344,"mapId":13272,"x":-162,"y":167,"name":"KAK Köyü","type":1,"population":12},
                "troop": {"t1":1,"t2":0,"t3":0,"t4":0,"t5":0,"t6":0,"t7":0,"t8":0,"t9":0,"t10":0},
                "distance": 1.41421,
                "isActive": true,
                "isRunning": false,
                "isSpying": false,
                "lastRaid": null,
                "totalBooty": {"booty":0,"raids":0}
            }],
            "slotsAmount": 1
        }
    }
}
```

### Send Raid (without Gold Club) ❌

**Request:** `POST /api/v1/farm-list/send`
```json
{"action":"farmList","lists":[{"id":8839,"targets":[372120]}]}
```
**Response:**
```json
{"error":"plus.error_goldclub","errorId":"haDh214Ufu64XWZV","message":"Your Gold club is not activated."}
```

### Delete Slot ✅

**Request:** `DELETE /api/v1/farm-list/slot`
```json
{"slots":[372120],"abandoned":false}
```
**Response:** `{}`

### Delete Farm List ✅

**Request:** `DELETE /api/v1/farm-list/8839`
```json
{"abandoned":false}
```
**Response:** `{}`
