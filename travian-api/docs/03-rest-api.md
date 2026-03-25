# REST API Endpoints

**Base URL:** `/api/v1/`
**Method:** POST (all endpoints)
**Content-Type:** `application/json; charset=UTF-8`
**Required Header:** `X-Version: 389`
**Auth:** JWT cookie

## API Client (JavaScript)

```javascript
// Internal API wrapper used by the game:
Travian.api = function(endpoint, options, method="POST", dataType="json") {
    return jQuery.ajax({
        url: "/api/v1/" + endpoint,
        method: method,
        data: JSON.stringify(options.data),
        processData: false,
        dataType: dataType,
        contentType: "application/json; charset=UTF-8",
        headers: { "X-Version": "389" },
        success: options.success,
        error: options.error,
        complete: options.complete
    });
}
```

## Error Response Format
```json
{
  "error": "karte.ajaxMissingInputParameters",
  "errorId": "KQrCLLx1pshqYUg8",
  "message": "Not all of the necessary data has been submitted."
}
```

## Special Response Fields
- `{reload: true}` → triggers `Travian.Autoreload.autoreload()` (page refresh)
- `{redirectTo: "/url"}` → triggers `Travian.redirectTo(url)` (navigation)

---

## Map Endpoints

### `POST /api/v1/map/info`
Fetch map symbols/overlays (adventures, special markers) for rectangular areas.

**Request:**
```json
{
  "data": [
    {"position": {"x0": -10, "y0": -10, "x1": -1, "y1": -1}},
    {"position": {"x0": 0, "y0": 0, "x1": 9, "y1": 9}}
  ],
  "zoomLevel": 1
}
```

**Response:**
```json
{
  "elements": [
    {
      "position": {"x": -165, "y": 158},
      "symbols": [{
        "dataId": "adventure2272480",
        "x": -165, "y": 158,
        "type": "adventure",
        "parameters": {"difficulty": 0},
        "title": "Adventure",
        "text": "Adventure 32<br />{a.ad} {a.ad0}"
      }]
    }
  ],
  "blocks": []
}
```

### `POST /api/v1/map/position`
Fetch tile tooltip data for an area around a position.

**Request:**
```json
{
  "data": {
    "x": 0,
    "y": 0,
    "zoomLevel": 1,
    "ignorePositions": []
  }
}
```

**Response:**
```json
{
  "tiles": [
    {
      "position": {"x": -2, "y": 3},
      "uid": 316,
      "aid": 61,
      "did": 57498,
      "title": "{k.dt} 02 Chaleco Prosti",
      "text": "{k.spieler} Pingu Satisfyer<br />{k.einwohner} 645<br />{k.allianz} P.A<br />{k.volk} {a.v3}"
    }
  ]
}
```

---

## Hero Endpoints

### `POST /api/v1/hero/v2/attributes`
Set hero attribute points.
```json
{"power": 0, "offBonus": 0, "defBonus": 0, "productionPoints": 5}
```

### `POST /api/v1/hero/v2/revive`
Revive a dead hero.

### `POST /api/v1/hero/v2/appearance/save`
Save hero appearance customization.

### `POST /api/v1/hero/v2/screen/{subpage}`
Load hero screen data for a specific tab.

### `POST /api/v1/hero/v2/inventory/move-item`
Move items in hero inventory.

---

## Auction House

### `POST /api/v1/hero/auction`
List current auctions.

### `POST /api/v1/hero/auction/data`
Fetch auction details. Also: `hero/auction/data?{params}`

### `POST /api/v1/hero/auction/bid`
Place a bid on an auction item.

### `POST /api/v1/hero/auction/bid-help`
Get bid help/suggestion info.

---

## Farm Lists

### `POST /api/v1/farm-list/`
CRUD operations on farm lists.

### `POST /api/v1/farm-list/slot`
Manage farm list slots (target villages).

### `POST /api/v1/farm-list/slot?force`
Force add a slot even if warnings exist.

### `POST /api/v1/farm-list/send`
**Send farm list raids** — dispatches attacks to all targets in the list.

### `POST /api/v1/farm-list/change-expanded-state`
Toggle expanded/collapsed state of farm list UI.

### `POST /api/v1/farm-list/close-inbox`
Close farm list inbox notifications.

---

## Marketplace

### `POST /api/v1/marketplace/exchange-resources`
Exchange resources between villages or with NPC.

### `POST /api/v1/marketplace/offers`
View marketplace trade offers.

### `POST /api/v1/marketplace/merchants/{id}`
Get merchant details for a specific trade.

---

## Reports

### `POST /api/v1/report/{reportId}/{direction}`
Navigate to previous/next report.

**Parameters:**
- `{reportId}` — Current report ID (integer)
- `{direction}` — `prev` or `next`

**Request:**
```json
{
    "category": "",
    "filter": [0,1,2,3,4,5,6,7,8,11,12,13,14,15,16,17,18,19,20,21,22,23],
    "orderBy": false
}
```

**Response:** Redirect data to navigate to prev/next report.

> See `docs/12-reports-system.md` for comprehensive report system documentation including HTML parsing, scout/raid report structure, and programmatic access patterns.

---

## Troops & Combat

### `POST /api/v1/troop/distanceSpeedAndDuration`
Calculate distance, speed, and travel duration for troops.

### `POST /api/v1/troop/travelInfo`
Get travel information for troop movements.

### `POST /api/v1/combatSimulator`
Run combat simulation.

### `POST /api/v1/rallyPointSimulators/screen/{id}`
Rally point simulator screens.

---

## Village Management

### `POST /api/v1/village/target/{id}`
Get target village information.

### `POST /api/v1/village/change-names`
Rename a village.

### `POST /api/v1/village-group/`
CRUD for village groups.

### `POST /api/v1/village-group`
Village groups management.

### `POST /api/v1/village-list`
Village list operations.

### `POST /api/v1/validate-destination`
Validate a destination coordinate (for sending troops/resources).

---

## Alliance

### `POST /api/v1/alliance/invite`
Send an alliance invitation.

### `POST /api/v1/alliance/banner/my`
Get your alliance's banner.

### `POST /api/v1/alliance/banner/edit`
Edit alliance banner.

### `POST /api/v1/alliance/banner/screen/{id}`
Banner screen data.

---

## Adventures

### `POST /api/v1/adventures/calculate-travelingdurations`
Calculate hero travel times to adventure locations.

---

## Player & Profile

### `POST /api/v1/profile`
Profile data operations.

### `POST /api/v1/player/update-note`
Update a player note (visible on their profile).

### `POST /api/v1/player/ignore`
Add/remove player from ignore list.

### `POST /api/v1/accusePlayer/{id}`
Report a player for rule violation.

---

## Premium & Economy

### `POST /api/v1/premium`
Activate/manage premium features.

### `POST /api/v1/silver-to-gold`
Exchange silver for gold (or vice versa).

### `POST /api/v1/videofeature/start`
Start watching a video ad for rewards.

### `POST /api/v1/videofeature/ends`
Notify server that video ad has completed.

---

## Trade Routes

### `POST /api/v1/trade-routes`
CRUD for trade routes.

### `POST /api/v1/trade-routes/toggle-group`
Enable/disable a trade route group.

---

## Daily Quests & Tasks

### `POST /api/v1/daily-quest/award`
Claim a daily quest reward.

### `POST /api/v1/daily-quest/close-reminder`
Dismiss quest reminder.

### `POST /api/v1/progressive-tasks/collectReward`
Collect a progressive task reward.

### `POST /api/v1/progressive-tasks/reload`
Reload progressive tasks state.

---

## Items & Crafting

### `POST /api/v1/item/crafting/smelt/take`
Take a smelted item from the forge.

### `POST /api/v1/item/crafting/forge/take`
Take a forged item.

---

## Miscellaneous

### `POST /api/v1/favourite-tab`
Set a favourite tab for a page.
```json
{"name": "tabName", "key": "tabKey"}
```

### `POST /api/v1/autocomplete/{type}`
Autocomplete for player/village names.
Types: `villagename`, `playername`

### `POST /api/v1/quick-links-set`
Configure sidebar quick links.

### `POST /api/v1/referAFriend/invite`
Send a referral invitation.

### `POST /api/v1/referAFriend/collectReward`
Collect referral rewards.

### `POST /api/v1/referAFriend/removeNotifications`
Clear referral notifications.
