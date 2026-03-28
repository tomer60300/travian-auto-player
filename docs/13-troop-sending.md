# Troop Sending System (Attack / Raid / Reinforcement / Scouting)

Troop sending in Travian Legends uses a **two-step HTML form POST** — there is no single REST/GraphQL API endpoint that directly dispatches troops. The process is:

1. **Step 1 (Form Submit):** POST form data to `/build.php?gid=16&tt=2` → Returns a confirmation page
2. **Step 2 (Confirm):** POST confirmation data from the confirmation page → Troops are dispatched

There are also supporting REST API endpoints for travel time calculation and destination validation.

---

## Rally Point Page

**URL:** `/build.php?gid=16&tt=2` (or `/build.php?id={slotId}&gid=16&tt=2`)

### Rally Point Tabs

| Tab | URL Parameter | Description |
|-----|--------------|-------------|
| Management | `tt=0` | Rally point building management |
| Overview | `tt=1` | Current troop movements |
| Send troops | `tt=2` | Send troops form |
| Simulators | `tt=3` | Combat simulator |
| Farm List | `tt=4` | Farm list management (Gold Club) |

---

## Step 1: Send Troops Form

### Form Structure

```html
<div class="a2b">
    <form method="post" action="/build.php?gid=16&amp;tt=2">
        <!-- Troop inputs table -->
        <table id="troops">...</table>

        <!-- Destination -->
        <div class="destination">...</div>

        <!-- Attack type -->
        <div class="option">...</div>

        <!-- Submit button -->
        <button type="submit" name="ok" id="ok">Send</button>
    </form>
</div>
```

### Troop Input Fields

Troops are input as `troop[t{N}]` where N is the troop slot number (1-10) within the tribe:

```html
<input type="text" inputmode="numeric" name="troop[t1]" value="" maxlength="6" />
<input type="text" inputmode="numeric" name="troop[t2]" value="" maxlength="6" disabled="" />
<!-- ... up to troop[t10] -->
```

**Teuton troop slot mapping:**

| Input Name | Unit Class | Troop Name |
|-----------|------------|------------|
| `troop[t1]` | `u11` | Clubswinger |
| `troop[t2]` | `u12` | Spearman |
| `troop[t3]` | `u13` | Axeman |
| `troop[t4]` | `u14` | Scout |
| `troop[t5]` | `u15` | Paladin |
| `troop[t6]` | `u16` | Teutonic Knight |
| `troop[t7]` | `u17` | Ram |
| `troop[t8]` | `u18` | Catapult |
| `troop[t9]` | `u19` | Chief |
| `troop[t10]` | `u20` | Settler |

**Roman troop slot mapping:**

| Input Name | Unit Class | Troop Name |
|-----------|------------|------------|
| `troop[t1]` | `u1` | Legionnaire |
| `troop[t2]` | `u2` | Praetorian |
| `troop[t3]` | `u3` | Imperian |
| `troop[t4]` | `u4` | Equites Legati (Scout) |
| `troop[t5]` | `u5` | Equites Imperatoris |
| `troop[t6]` | `u6` | Equites Caesaris |
| `troop[t7]` | `u7` | Ram |
| `troop[t8]` | `u8` | Fire Catapult |
| `troop[t9]` | `u9` | Senator |
| `troop[t10]` | `u10` | Settler |

**Gaul troop slot mapping:**

| Input Name | Unit Class | Troop Name |
|-----------|------------|------------|
| `troop[t1]` | `u21` | Phalanx |
| `troop[t2]` | `u22` | Swordsman |
| `troop[t3]` | `u23` | Pathfinder (Scout) |
| `troop[t4]` | `u24` | Theutates Thunder |
| `troop[t5]` | `u25` | Druidrider |
| `troop[t6]` | `u26` | Haeduan |
| `troop[t7]` | `u27` | Ram |
| `troop[t8]` | `u28` | Trebuchet |
| `troop[t9]` | `u29` | Chieftain |
| `troop[t10]` | `u30` | Settler |

> **Note:** The unit class (`uNN`) is a global ID. The troop slot (`t1`-`t10`) is relative to the tribe. `t1` is always the first troop of that tribe. The form always uses `t1`-`t10` regardless of tribe.

> **Disabled fields:** Inputs for troop types with 0 available have `disabled=""` attribute and CSS class `disabled`. They cannot be submitted unless re-enabled.

### Destination Fields

**By village name (autocomplete):**
```html
<input type="text" id="enterVillageName" name="villagename" value="" maxlength="25" autocomplete="off" />
```

**By coordinates:**
```html
<input type="text" id="xCoordInput" name="x" value="" maxlength="4"
       onkeyup="Travian.Formatter.Filter.aNumber(this)"
       onpaste="var cih = new Travian.Game.RallyPoint.CoordinatesInputHelper({...}); cih.insertCoordinates(event);" />
<input type="text" id="yCoordInput" name="y" value="" maxlength="4"
       onkeyup="Travian.Formatter.Filter.aNumber(this)"
       onpaste="..." />
```

> **Coordinate paste helper:** The `CoordinatesInputHelper` class parses pasted coordinates in formats like `123|456`, `123/456`, or `123,456` and splits them into X and Y fields.

### Event Type (Attack Mode)

```html
<input type="radio" name="eventType" value="5" checked="" /> Reinforcement
<input type="radio" name="eventType" value="3" />            Attack: Normal
<input type="radio" name="eventType" value="4" />            Attack: Raid
```

| Value | Event Type | Description |
|-------|-----------|-------------|
| `2` | Scout | Send scouts (used when only scouts are selected) |
| `3` | Attack: Normal | Full attack — kills defending troops, can conquer |
| `4` | Attack: Raid | Raid — steals resources, survivors return |
| `5` | Reinforcement | Send troops to defend another village |

> **Note:** Scouting (eventType=2) may not appear as a radio option; it is automatically selected by the server when only scout units are sent. The visible options are Reinforcement (5), Attack: Normal (3), and Attack: Raid (4).

### Complete POST Body (Step 1)

When the form is submitted, the POST body is URL-encoded:

```
troop[t1]=10&troop[t2]=0&troop[t3]=5&troop[t4]=0&troop[t5]=0&troop[t6]=0&troop[t7]=0&troop[t8]=0&troop[t9]=0&troop[t10]=0&villagename=&x=-164&y=165&eventType=4&ok=ok
```

**Fields:**
- `troop[t1]` through `troop[t10]` — Number of each troop type to send (0 if none)
- `villagename` — Target village name (empty if using coordinates)
- `x` — Target X coordinate
- `y` — Target Y coordinate
- `eventType` — Attack mode (2/3/4/5)
- `ok` — Submit button value

**POST target:** `/build.php?gid=16&tt=2`
**Method:** POST
**Content-Type:** `application/x-www-form-urlencoded`

### Pre-filled from Report

When coming from a battle report, the URL includes `reportId`:
```
/build.php?id=39&tt=2&reportId=72738472&readReport=1
```
This pre-fills the X/Y coordinates from the report's target.

---

## Step 2: Confirmation Page

After submitting Step 1, the server returns a **confirmation page** with a new form (`id="troopSendForm"`) containing all data as hidden fields plus a server-generated checksum.

### Confirmation Page Form Structure

```html
<form method="post" action="/build.php?gid=16&amp;tt=2" id="troopSendForm">
    <!-- Action token: "troopsSend/{targetVillageId}/{timestamp}" -->
    <input type="hidden" name="action" value="troopsSend/75483/1774353611" />
    <input type="hidden" name="eventType" value="4" />
    <input type="hidden" name="villagename" value="KAK Köyü" />
    <input type="hidden" name="x" value="-162" />
    <input type="hidden" name="y" value="167" />
    <input type="hidden" name="redeployHero" value="" />
    <input type="hidden" name="checksum" value="" />  <!-- Filled by JS on confirm -->

    <!-- Troop data (note: troops[0] array format, not troop[]) -->
    <input type="hidden" name="troops[0][t1]" value="1" />
    <input type="hidden" name="troops[0][t2]" value="0" />
    <input type="hidden" name="troops[0][t3]" value="0" />
    <input type="hidden" name="troops[0][t4]" value="0" />
    <input type="hidden" name="troops[0][t5]" value="0" />
    <input type="hidden" name="troops[0][t6]" value="0" />
    <input type="hidden" name="troops[0][t7]" value="0" />
    <input type="hidden" name="troops[0][t8]" value="0" />
    <input type="hidden" name="troops[0][t9]" value="0" />
    <input type="hidden" name="troops[0][t10]" value="0" />
    <input type="hidden" name="troops[0][t11]" value="0" />  <!-- Hero slot -->

    <!-- Targeting options -->
    <input type="hidden" name="troops[0][scoutTarget]" value="" />
    <input type="hidden" name="troops[0][catapultTarget1]" value="" />
    <input type="hidden" name="troops[0][catapultTarget2]" value="" />
    <input type="hidden" name="troops[0][villageId]" value="75483" />  <!-- Source village ID -->

    <!-- Troop summary table -->
    <table class="troop_details">...</table>

    <!-- Arrival time -->
    <div id="in" class="in">In 1:51:45 hours</div>
    <div class="at">at <span id="at" class="timer" counting="up" value="{arrivalTimestamp}">15:52:07</span></div>

    <!-- Buttons -->
    <button type="submit" name="back" id="back">Edit</button>
    <button type="button" id="waveBuilder" onclick="openWaveBuilderDialog()">Wave Builder</button>
    <button type="button" name="confirmSendTroops" id="confirmSendTroops"
            onclick="this.disabled = true;
                     document.querySelector('#troopSendForm input[name=checksum]').value = 'a2834f';
                     document.getElementById('troopSendForm').submit();
                     return false;">
        Confirm
    </button>
</form>
```

### Key Hidden Fields

| Field | Example Value | Description |
|-------|--------------|-------------|
| `action` | `troopsSend/75483/1774353611` | Token: `troopsSend/{targetVillageId}/{serverTimestamp}` |
| `eventType` | `4` | Attack mode (2/3/4/5) |
| `villagename` | `KAK Köyü` | Resolved target village name |
| `x`, `y` | `-162`, `167` | Target coordinates |
| `redeployHero` | (empty) | Whether to move hero with troops |
| `checksum` | `a2834f` | **CSRF checksum** — injected by JS on confirm click |
| `troops[0][t1]`–`troops[0][t10]` | troop counts | Troops per slot |
| `troops[0][t11]` | `0` | **Hero** (1 = send hero, 0 = don't) |
| `troops[0][scoutTarget]` | (empty) | Scout target preference (resources/troops) |
| `troops[0][catapultTarget1]` | (empty) | Primary catapult target building |
| `troops[0][catapultTarget2]` | (empty) | Secondary catapult target building |
| `troops[0][villageId]` | `75483` | **Source** village ID (your village sending troops) |

> **⚠️ CRITICAL — Checksum:** The `checksum` field starts empty. When the user clicks "Confirm", the onclick handler sets it to a server-generated value (e.g., `'a2834f'`) before submitting. This acts as a CSRF token. The checksum value is embedded in the button's onclick attribute and is unique per confirmation page load.

### Differences from Step 1

| Field | Step 1 (Form) | Step 2 (Confirmation) |
|-------|--------------|----------------------|
| Troop names | `troop[t1]` | `troops[0][t1]` (array with wave index) |
| Hero | Not present | `troops[0][t11]` |
| Checksum | Not present | `checksum` (CSRF) |
| Action token | Not present | `action` = `troopsSend/{villageId}/{timestamp}` |
| Scout target | Not present | `troops[0][scoutTarget]` |
| Catapult targets | Not present | `troops[0][catapultTarget1]`, `troops[0][catapultTarget2]` |
| Village ID | Not present | `troops[0][villageId]` (source) |

### Confirm Flow

1. User clicks "Confirm" button
2. Button onclick disables itself (prevent double-click)
3. Injects checksum value into hidden field: `document.querySelector('#troopSendForm input[name=checksum]').value = '{hash}'`
4. Submits the form: `document.getElementById('troopSendForm').submit()`
5. Server processes and **redirects to Rally Point Overview** (`/build.php?gid=16&tt=1`)

### Wave Builder

The confirmation page also has a "Wave Builder" button (Gold Club feature) that calls:
```javascript
Travian.api('waveBuilderDialog/{targetVillageId}', {
    success: function(response) { /* shows dialog */ }
}, 'GET', 'HTML');
```

### Step 2 POST Body

```
action=troopsSend%2F75483%2F1774353611&eventType=4&villagename=KAK+K%C3%B6y%C3%BC&x=-162&y=167&redeployHero=&checksum=a2834f&troops%5B0%5D%5Bt1%5D=1&troops%5B0%5D%5Bt2%5D=0&troops%5B0%5D%5Bt3%5D=0&troops%5B0%5D%5Bt4%5D=0&troops%5B0%5D%5Bt5%5D=0&troops%5B0%5D%5Bt6%5D=0&troops%5B0%5D%5Bt7%5D=0&troops%5B0%5D%5Bt8%5D=0&troops%5B0%5D%5Bt9%5D=0&troops%5B0%5D%5Bt10%5D=0&troops%5B0%5D%5Bt11%5D=0&troops%5B0%5D%5BscoutTarget%5D=&troops%5B0%5D%5BcatapultTarget1%5D=&troops%5B0%5D%5BcatapultTarget2%5D=&troops%5B0%5D%5BvillageId%5D=75483
```

> **⚠️ This is the destructive action.** Step 1 is reversible (just shows confirmation). Step 2 actually dispatches the troops. After confirmation, the server redirects to `/build.php?gid=16&tt=1` (Rally Point Overview) where the troop movement is visible.

---

## Supporting REST API Endpoints

### `POST /api/v1/troop/distanceSpeedAndDuration`

Calculate travel time for a troop movement. Called by `Travian.Game.RallyPoint.updateTravelTime()`.

**Request:**
```json
{
    "origin": 69130,
    "from": 69130,
    "to": 14072,
    "troops": {
        "t1": 10,
        "t2": 0,
        "t3": 5,
        "t4": 0,
        "t5": 0,
        "t6": 0,
        "t7": 0,
        "t8": 0,
        "t9": 0,
        "t10": 0
    },
    "useShip": false,
    "eventType": 4
}
```

**Parameters:**
- `origin` — Source village ID (your village)
- `from` — Departure village ID (usually same as origin)
- `to` — Target village/map ID
- `troops` — Object with troop counts per slot (`t1`-`t10`)
- `useShip` — Whether to use a ship (harbor feature)
- `eventType` — Attack type (2/3/4/5)

**Response:**
```json
{
    "data": {
        "duration": 3600,
        "durationString": "1:00:00",
        "distance": 10.63,
        "speed": 7
    }
}
```

### `POST /api/v1/troop/travelInfo`

Get detailed travel information for troop movements.

### `POST /api/v1/validate-destination`

Validate a target coordinate before sending.

**Request:**
```json
{
    "x": -164,
    "y": 165
}
```

### `POST /api/v1/combatSimulator`

Run a combat simulation.

---

## Farm List Sending (Bulk Raids)

Farm lists provide a **one-click bulk raid** mechanism via a proper REST API:

### `POST /api/v1/farm-list/send`

Send all targets in a farm list.

> **Note:** This is the easiest programmatic way to send raids — farm lists bypass the two-step form process. Create farm list targets via the farm list API, then trigger them with a single API call.

### Farm List Management

| Endpoint | Description |
|----------|-------------|
| `POST /api/v1/farm-list/` | CRUD operations on farm lists |
| `POST /api/v1/farm-list/slot` | Manage farm list slots (target villages) |
| `POST /api/v1/farm-list/slot?force` | Force add a slot (ignore warnings) |
| `POST /api/v1/farm-list/send` | Send all targets in farm list |
| `POST /api/v1/farm-list/change-expanded-state` | Toggle UI expanded state |
| `POST /api/v1/farm-list/close-inbox` | Close farm list inbox |

---

## JavaScript API

### `Travian.Game.RallyPoint` Methods

| Method | Description |
|--------|-------------|
| `initialize(jqTroopTable)` | Initialize the troop input form — sets up numeric validation, keyboard shortcuts |
| `updateTravelTime(origin, from, to, element, troopIdx, eventType)` | Calculate and display travel time via `troop/distanceSpeedAndDuration` API |
| `updateShips(event, troopIdx)` | Toggle ship usage for troop movement |
| `getShipForMovement(movement)` | Check if a ship is available for a movement |
| `freeShipMovements(troopIdx)` | Release ship allocation for a movement |
| `updateShipMovements()` | Update all ship movement UI |
| `settleVillageTribeSelection()` | Handle tribe selection for settling new village |
| `storeNewVillageSelectedTribe()` | Save tribe selection to localStorage |
| `handleRemoveVillageFromFarmLists(villageId)` | Remove a village from all farm lists |
| `processRemoveVillageFromFarmLists(villageId)` | Execute farm list removal via `village/{id}/remove-from-farm-lists` |
| `CoordinatesInputHelper(options)` | Helper class for parsing pasted coordinates (`x|y`, `x/y` formats) |

### Keyboard Shortcuts

- **Enter key** (when body is focused): Triggers the Send button click
  ```javascript
  document.addEventListener('keydown', e => {
      if ((e.code === 'Enter' || e.code === 'NumpadEnter') && e.target === document.body) {
          e.preventDefault();
          jQuery('.a2b #ok').click();
      }
  });
  ```

### Autocomplete

Village name input uses autocomplete:
```javascript
Travian.Game.AutoCompleter.VillageName(jQuery('#enterVillageName'));
```
This calls `POST /api/v1/autocomplete/villagename` as the user types.

---

## Programmatic Troop Sending

### Method 1: Farm List API (Recommended for Raids)

The simplest way to send raids programmatically — no form parsing needed:

```python
import requests

BASE = "https://ts1.x1.europe.travian.com"
s = requests.Session()
s.cookies.set("JWT", token)
s.headers.update({"Content-Type": "application/json", "X-Version": "389"})

# Send all targets in a farm list
r = s.post(f"{BASE}/api/v1/farm-list/send", json={
    "listId": 123,    # Farm list ID
    "villageId": 69130  # Source village ID
})
```

### Method 2: Two-Step Form POST (For Custom Attacks)

```python
# Step 1: Submit the form
form_data = {
    "troop[t1]": "10",   # 10 Clubswingers
    "troop[t2]": "0",
    "troop[t3]": "5",    # 5 Axemen
    "troop[t4]": "0",
    "troop[t5]": "0",
    "troop[t6]": "0",
    "troop[t7]": "0",
    "troop[t8]": "0",
    "troop[t9]": "0",
    "troop[t10]": "0",
    "x": "-164",
    "y": "165",
    "eventType": "4",     # Raid
    "ok": "ok"
}
r1 = s.post(f"{BASE}/build.php?gid=16&tt=2", data=form_data)

# Step 2: Parse confirmation page for hidden fields, then confirm
# The confirmation page contains hidden inputs with timing tokens
# Parse them and POST again to confirm the dispatch
```

### Method 3: JavaScript Execution (In Browser)

```javascript
// Calculate travel time
Travian.api("troop/distanceSpeedAndDuration", {
    data: {
        origin: 69130,      // Your village ID
        from: 69130,
        to: 14072,           // Target map ID
        troops: { t1: 10, t2: 0, t3: 5, t4: 0, t5: 0, t6: 0, t7: 0, t8: 0, t9: 0, t10: 0 },
        useShip: false,
        eventType: 4         // Raid
    },
    success: function(data) {
        console.log("Duration:", data.data.durationString);
        console.log("Distance:", data.data.distance);
    }
});
```

---

## Event Type Summary

| eventType | Name | Behavior |
|-----------|------|----------|
| 2 | Scout | Send scouts only; attempt to spy on target |
| 3 | Attack: Normal | Full attack; all surviving defenders die; can use siege/chiefs |
| 4 | Attack: Raid | Raid; steal resources; surviving attackers return; no village damage |
| 5 | Reinforcement | Send troops to defend another village |

---

## Related Village Endpoints

### `POST /api/v1/village/{villageId}/remove-from-farm-lists`

Remove a village from all farm lists. Called by `Travian.Game.RallyPoint.processRemoveVillageFromFarmLists()`.

### Autocomplete Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /api/v1/autocomplete/villagename` | Search villages by name |
| `POST /api/v1/autocomplete/playername` | Search players by name |
