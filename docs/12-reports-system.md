# Reports System

Reports in Travian Legends are primarily **server-rendered HTML** — there is no REST/GraphQL API for listing or fetching report data as structured JSON. Reports are accessed by navigating to page routes, and the content is embedded in HTML tables.

**Key constraint:** The GraphQL `resources` field on reports always returns `null` for all report types. Resource data **must** be parsed from HTML.

---

## Report List Pages

### Routes

| Route | Tab | Description |
|-------|-----|-------------|
| `/report/all` | All (complete) | **All report types in one chronological stream — the only complete source** |
| `/report/overview` | All (default) | All report types (same as /report/all) |
| `/report/offensive` | Offensive | Attacks you sent |
| `/report/defensive` | Defensive | Attacks you received |
| `/report/scouting` | Scouting | Scout reports |
| `/report/other` | Other | Trade, adventure, settlement reports |
| `/report/surrounding` | Surrounding | Activity near your villages |
| `#` (goldclub) | Archive | Archived reports (requires Gold Club) |

> **⚠️ IMPORTANT:** Category pages (`/report/offensive`, `/report/scouting`, etc.) return only a **tiny fraction** of reports. `/report/all` is the **only complete source** for all report types in one chronological stream. Report type classification (scout vs raid) should be done from the title field, not by using category-specific pages.

### Pagination

Reports are paginated server-side via query parameter:
```
/report/all?page=2
/report/overview?page=2
```

- **30 reports per page** (configurable via `Travian.Game.Preferences` → `entriesPerPage`)
- Pages are 1-indexed

### Report List Row HTML Structure

Each report in the list is a `<tr>` inside a table:

```html
<tr>
    <td class="sel">
        <!-- Checkbox — THIS is the reliable report ID source -->
        <input class="check report" type="checkbox" name="ids[]" value="{reportId}"
               onclick="Travian.Game.Reports.updateSelected()" />
    </td>
    <td class="sub">
        <!-- Read/Unread toggle -->
        <a href="#" onclick="Travian.Game.Reports.toggleReadStatus('{reportId}', {0|1}); return false;">
            <img class="messageStatus messageStatusRead|messageStatusUnread" alt="read|unread" />
        </a>
        <!-- Report type icon -->
        <img class="iReport iReport{N}" alt="{description}" title="{description}" />
        <!-- Carry indicator (battle reports only) -->
        <a class="reportInfoIcon" href="/build.php?id=39&amp;tt=2&amp;reportId={reportId}&amp;readReport=1">
            <img class="reportInfo carry full|partial|empty" title="{carried}/{capacity}" />
        </a>
        <!-- Subject line (links to detail) -->
        <div>
            <a href="?id={reportId}%7C{hash}&s=1">{subject}</a>
        </div>
    </td>
    <td class="dat">
        {date}
    </td>
</tr>
```

> **⚠️ Report ID extraction:** Always use `row.querySelector('input[type="checkbox"]').value` for the report ID. Do NOT use `a[href*="id="]` — that selector also matches building upgrade links on the page.

### Report Detail URL Patterns

**With sharing hash (from list page links):**
```
/report?id={reportId}|{hash}&s=1
```
URL-encoded: `?id={reportId}%7C{hash}&s=1`

**By ID alone (for programmatic fetch):**
```
/report?id={reportId}
```
Both work. The hash is an 8-character hex sharing token. The `s=1` parameter is a session flag.

### Date/Time Formats in Report Lists

The `<td class="dat">` column uses these formats (browser local timezone):

| Format | Example |
|--------|---------|
| `today, HH:MM` | `today, 11:15` |
| `yesterday, HH:MM` | `yesterday, 14:30` |
| `DD.MM.YY, HH:MM` | `22.03.26, 09:45` |

> **⚠️ Parsing:** Timestamps use a **comma** between date and time parts. Regex must use `[,\s]+` not `\s+` to match correctly.

---

## Report Type Icons

| CSS Class | Icon ID | Description |
|-----------|---------|-------------|
| `iReport1` | 1 | Won as attacker without losses |
| `iReport2` | 2 | Won as attacker with losses |
| `iReport3` | 3 | Lost as attacker |
| `iReport4` | 4 | Won as defender without losses |
| `iReport5` | 5 | Won as defender with losses |
| `iReport6` | 6 | Lost as defender |
| `iReport7` | 7 | Defender was raided (no/some defense) |
| `iReport8` | 8 | Defender lost completely |
| `iReport11` | 11 | Trade — merchants delivered mostly lumber |
| `iReport12` | 12 | Trade — merchants delivered mostly clay |
| `iReport13` | 13 | Trade — merchants delivered mostly iron |
| `iReport14` | 14 | Trade — merchants delivered mostly crop |
| `iReport15` | 15 | Scouting — successful and not detected |
| `iReport16` | 16 | Scouting — successful but detected |
| `iReport17` | 17 | Scouting — partially successful |
| `iReport18` | 18 | Scouting — failed (scouts killed) |
| `iReport19` | 19 | Scouting — defended against |
| `iReport20` | 20 | Reinforcement report |
| `iReport21` | 21 | Adventure report |
| `iReport22` | 22 | Settlers used to create a new village |
| `iReport23` | 23 | Miscellaneous |

### Filter Icons by Tab

| Tab | Filter Icons |
|-----|-------------|
| All (overview) | `[0,1,2,3,4,5,6,7,8,11,12,13,14,15,16,17,18,19,20,21,22,23]` |
| Offensive | `[2,3]` |
| Defensive | `[4,5,6,7,8]` |
| Scouting | `[16,17,18,19]` |
| Other | `[0,11,12,13,14,20,21,22,23]` |

### Report Type Classification from Title

Report type can be determined from the GQL `title` field (more reliable than icon parsing):

| Title pattern | Type |
|---------------|------|
| `{village} scouts {target}` | Scout report |
| `{village} raids {target}` | Raid report |
| `{village} attacks {target}` | Attack report |
| `{village} explores` | Adventure report |

---

## Individual Report Detail Page

### Wrapper Structure

```html
<div id="reportWrapper">
    <div class="header">
        <!-- Navigation (prev/next) -->
        <div class="headline withQuickNavigation">
            <a class="reportQuickNavigation" data-direction="prev">...</a>
            <div class="subject" title="{full subject}">{subject}</div>
            <a class="reportQuickNavigation" data-direction="next">...</a>
        </div>
        <!-- Timestamp -->
        <div class="time">
            <div class="text">24.03.26, 11:15:28</div>
        </div>
        <!-- Action buttons -->
        <div class="toolList">
            <button onclick="Travian.Game.Reports.processDeleteIds(['{reportId}'])">Delete</button>
            <button onclick="Travian.Game.Reports.updateStatus(['{reportId}'], 'unread')">Mark unread</button>
            <button onclick="Travian.Game.Reports.editRights(this, {reportId})">Access permissions</button>
            <button id="archiveGoldclub">Archive (Gold Club)</button>
        </div>
    </div>
    <div class="body">
        <!-- Report-type-specific content -->
    </div>
</div>
```

### Victory Banner (Battle/Scout Reports)

```html
<div class="victory">
    <img class="reportImage outcome wonLost|wonNone|lost" />
    <img class="tribe tribe{N} attacker won|lost" />
    <img class="tribe tribe{N} defender won|lost" />
</div>
```

Outcome CSS classes:
- `wonNone` — Attacker won without losses
- `wonLost` — Attacker won with losses
- `lost` — Attacker lost

---

## Battle / Raid Report Structure

### Attacker Section

```html
<div class="role attacker">
    <div class="header">
        <div class="avatar">
            <i class="tribeIcon bigTribe{N}"></i>
            <!-- SVG role icon -->
        </div>
        <h2>Attacker</h2>
        <div class="outcome">
            <!-- SVG icons for losses + loot status -->
            <svg class="losses attack|attackLost">...</svg>
            <svg class="loot lootFull|lootPartial|lootEmpty">...</svg>
        </div>
    </div>
    <div class="troopHeadline">
        <div>
            [<a href="/alliance/{allianceId}">{allianceTag}</a>]
            <a class="player" href="/profile/{playerId}">{playerName}</a>
            from village <a class="village" href="/karte.php?d={mapId}">{villageName}</a>
        </div>
    </div>
    <table>
        <!-- See Troop Table Layout below -->
    </table>
</div>
```

### Defender Section

Same structure as attacker but with `class="role defender"`.

### Troop Table Layout — `<tbody class="units">`

> **⚠️ CRITICAL:** Troop icons use `<img>` tags, NOT `<i>` tags. Resource icons use `<i class="lumber">`, but troop icons use `<img src="/img/x.gif" class="unit u21" title="Phalanx">`. Use `querySelector('img')` for troops, read `.title` for the troop name and `.className` for the unit ID.

The troop table contains multiple `<tbody class="units">` sections:

```
tbody[0] = attacker troop icons   → <img class="unit uNN" title="TroopName">
tbody[1] = attacker troop counts  → <td class="unit">{count}</td>
tbody[2] = attacker casualties    → <td class="unit">{dead}</td>
tbody[3] = defender troop icons   → <img class="unit uNN" title="TroopName">
tbody[4] = defender troop counts  → sum non-zero values for defender total
tbody[5] = defender casualties
tbody[6+] = trapped troops (if any) → <img> with className/title containing "trap"
```

**Example troop icons row:**
```html
<tbody class="units">
    <tr>
        <th class="coords"></th>
        <td class="uniticon"><img src="/img/x.gif" class="unit u11" title="Clubswinger" alt="Clubswinger" /></td>
        <td class="uniticon"><img src="/img/x.gif" class="unit u12" title="Spearman" alt="Spearman" /></td>
        <td class="uniticon"><img src="/img/x.gif" class="unit u13" title="Axeman" alt="Axeman" /></td>
        <!-- ... up to 10 units + hero -->
        <td class="uniticon last"><img src="/img/x.gif" class="unit uhero" title="Hero" alt=" Hero" /></td>
    </tr>
</tbody>
```

**Example troop counts row:**
```html
<tbody class="units">
    <tr>
        <th><i class="troopCount_small" title="Troops"></i></th>
        <td class="unit">161</td>
        <td class="unit">0</td>
        <td class="unit">302</td>
        <!-- ... -->
    </tr>
</tbody>
```

### Unit CSS Classes by Tribe

| Tribe | Unit Range | Examples |
|-------|-----------|---------|
| Romans | `u1` – `u10` | `u1` = Legionnaire, `u2` = Praetorian, `u3` = Imperian |
| Teutons | `u11` – `u20` | `u11` = Clubswinger, `u12` = Spearman, `u13` = Axeman, `u14` = Scout, `u15` = Paladin, `u16` = Teutonic Knight, `u17` = Ram, `u18` = Catapult, `u19` = Chief, `u20` = Settler |
| Gauls | `u21` – `u30` | `u21` = Phalanx, `u22` = Swordsman |
| Nature | `u31` – `u40` | `u31` = Rat, `u32` = Spider, `u33` = Snake, `u34` = Bat, `u35` = Wild Boar, `u36` = Wolf, `u37` = Bear, `u38` = Crocodile, `u39` = Tiger, `u40` = Elephant |
| Natars | `u41` – `u50` | |
| Hero | `uhero` | Hero unit |

### Combat Statistics Table

```html
<table class="combatStatistic">
    <thead>
        <tr>
            <th></th>
            <th>Attacker</th>
            <th>Defender</th>
        </tr>
    </thead>
    <tbody>
        <tr title="This figure includes all positive and negative factors">
            <th>Combat strength</th>
            <td><i class="offence_medium"></i><span class="value">{attackerStrength}</span></td>
            <td><i class="defence_medium"></i><span class="value">{defenderStrength}</span></td>
        </tr>
        <tr>
            <th>Supply before</th>
            <td><i class="r5Big"></i><span class="value">{attackerSupply}</span></td>
            <td><i class="r5Big"></i><span class="value">{defenderSupply}</span></td>
        </tr>
        <tr>
            <th>Supply lost</th>
            <td><i class="r5Big"></i><span class="value">{attackerSupplyLost}</span></td>
            <td><i class="r5Big"></i><span class="value">{defenderSupplyLost}</span></td>
        </tr>
        <tr>
            <th>Resources lost</th>
            <td><i class="resources_medium"></i><span class="value">{attackerResLost}</span></td>
            <td><i class="resources_medium"></i><span class="value">{defenderResLost}</span></td>
        </tr>
    </tbody>
</table>
```

> **Note:** Numeric values in `<span class="value">` contain Unicode directional markers (U+202D `‭` and U+202C `‬`). Clean with `.replace(/[^\d]/g, '')` or `.replace(/[\u202d\u202c]/g, '')` before parsing.

### Bounty / Additional Information Table

```html
<table class="additionalInformation">
    <tbody class="infos">
        <tr>
            <th>Bounty</th>
            <td>
                <!-- Resource amounts (use <i> tags for resource type) -->
                <div class="res">
                    <div class="inlineIconList resourceWrapper">
                        <div class="inlineIcon resources" title="Lumber">
                            <i class="lumber"></i><span class="value">{amount}</span>
                        </div>
                        <div class="inlineIcon resources" title="Clay">
                            <i class="clay"></i><span class="value">{amount}</span>
                        </div>
                        <div class="inlineIcon resources" title="Iron">
                            <i class="iron"></i><span class="value">{amount}</span>
                        </div>
                        <div class="inlineIcon resources" title="Crop">
                            <i class="crop"></i><span class="value">{amount}</span>
                        </div>
                    </div>
                </div>
                <!-- Carry fraction -->
                <div class="inlineIcon carry" title="carry">
                    <i class="carry full|half"></i>
                    <span class="value">{carried}/{capacity}</span>
                </div>
            </td>
        </tr>
    </tbody>
</table>
```

### Carry Indicator — Understanding "Still Left"

The carry indicator shows how much loot was carried vs capacity:

| `<i>` class | Meaning |
|-------------|---------|
| `carry full` | Troops fully loaded — village had **more** resources than troops could carry. Resources remain. |
| `carry half` | Troops partially loaded — village was **emptied** by this raid. |
| `carry empty` | Nothing carried (e.g., oasis with no resources) |

> **⚠️ Carry text contains Unicode:** The carry fraction text (e.g., "400/400") contains invisible Unicode directional markers U+202D and U+202C. Clean with `.replace(/[^\d\/]/g, '')` then `split('/')` to get `[carryUsed, carryMax]`.

**"stillLeft" logic for programmatic use:**
```
carryUsed >= carryMax → stillLeft = true  (troops full, village had MORE loot)
carryUsed < carryMax  → stillLeft = false (village was emptied)
```

---

## Scout Report Structure (Detailed)

Scout report HTML has a specific layout for scouted resources and cranny data, inside the `.additionalInformation` table:

### Resource Wrappers

```
.inlineIconList.resourceWrapper[0] → Resources (4 items: lumber, clay, iron, crop)
    Each: .inlineIcon <i class="lumber|clay|iron|crop"> → resource values

.inlineIconList.resourceWrapper[1] → Cranny / Stealable Info (2 items)
    .inlineIcon <i class="building_small tribe* type23"> → cranny capacity (informational)
    .inlineIcon <i class="carry full|half"> → STEALABLE AMOUNT
```

### Cranny / Carry Semantics

> **⚠️ CRITICAL — This is counterintuitive:**
>
> The carry icon value in scout reports IS the **stealable (raidable) amount** directly. It is NOT the cranny protection amount. Do NOT subtract it from total.

| Carry class | Meaning | Formula |
|-------------|---------|---------|
| `carry full` | Stealable = total (cranny irrelevant or zero) | `stealable = carry_value` |
| `carry half` | Stealable < total (rest is cranny-protected) | `stealable = carry_value` |
| No carry icon | No cranny data | Fallback: `stealable = total × 0.67` |

**Examples:**
- Total resources = 6,050, carry half = 3,457 → Stealable is **3,457** (NOT 6,050 - 3,457)
- Total resources = 2,877, carry full = 2,877 → Stealable is **2,877** (NOT 0)

### Espionage-Only Scouts

Some scout reports are espionage-only (troop information only, no resources):
- They have only **1 resourceWrapper** (wrapper[1] is missing)
- Resource total = 0 or ≤ 10
- No carry icon
- These reports show defender troop counts but NOT resources

When parsing, if `total ≤ 10`, treat the scout as espionage-only and look for an older scout report for resource data.

### Defender Troops in Scout Reports

Scout reports contain defender troop data in the same `<tbody class="units">` layout as battle reports:
```
tbody[3] = defender troop icons
tbody[4] = defender troop counts
```
Parse defenders from scouts — many targets have scout data but no raid reports.

---

## Battle / Raid Report — Bounty Parsing

### Resource Bounty

Located in `.inlineIconList.resourceWrapper[0]` inside the report body:

```html
<div class="inlineIconList resourceWrapper">
    <div class="inlineIcon resources" title="Lumber">
        <i class="lumber"></i><span class="value">1234</span>
    </div>
    <!-- clay, iron, crop same pattern -->
</div>
```

Parse each `.inlineIcon` where the `<i>` has class `lumber|clay|iron|crop`. Sum all 4 for total bounty.

### Carry Fraction

```html
<div class="inlineIcon carry" title="carry">
    <i class="carry full"></i>
    <span class="value">‭‭400‬/‭400‬‬</span>
</div>
```

The carry icon `<i>` class tells you `full` or `half`. The text value needs Unicode cleaning:
```javascript
var parts = carryEl.textContent.replace(/[^\d\/]/g, '').split('/');
var carryUsed = parseInt(parts[0]);
var carryMax = parseInt(parts[1]);
var stillLeft = carryMax > 0 ? (carryUsed >= carryMax) : false;
```

---

## Adventure Report Structure

```html
<img class="reportImage adventureVictory" title="Adventure {N}" />
<div class="adventureReport">
    <div class="adventureHeader">
        <h2>Adventure</h2>
    </div>
    <table class="additionalInformation">
        <tbody class="infos">
            <tr>
                <th>Information</th>
                <td class="dropItems">
                    <div><img class="iExperience" title="Experience" />{xp}</div>
                    <div><img class="injury" title="Health" />{healthLoss}%</div>
                </td>
            </tr>
        </tbody>
        <tbody class="goods">
            <!-- Items/resources found -->
        </tbody>
    </table>
</div>
```

---

## Surrounding Reports Table

```html
<table class="row_table_data reportSurround">
    <thead>
        <tr>
            <th>Subject:</th>
            <th class="sent">location</th>
            <th class="sent">distance</th>
            <th class="sent">Received</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td class="sub">
                <div class="reportIcon {eventType}"></div>
                <div class="reportText">{description}</div>
            </td>
            <td class="coords">
                <a href="/karte.php?x={x}&y={y}">({x}|{y})</a>
            </td>
            <td class="dist">{distance}</td>
            <td class="dat">{date}</td>
        </tr>
    </tbody>
</table>
```

### Surrounding Report Event Types

| CSS Class | Event |
|-----------|-------|
| `oasisOccupy` | An oasis has been plundered |
| `villageFound` | A player founded a new village |
| `villageConquered` | A village was conquered |
| `troopMovement` | Troops spotted moving |

---

## GraphQL API for Reports

### Report Metadata (Batched)

The GraphQL API can fetch **metadata** for reports (but NOT resource data):

```graphql
{
    r0: report(objectId: "72738472") {
        time
        title
        defender {
            playerName
            village { id name x y }
        }
    }
    r1: report(objectId: "72638225") { ... }
    # Up to ~250 aliases per query
}
```

**Fields:**
- `time` — Unix timestamp (seconds)
- `title` — Report subject line (e.g., "Chieftain`s village raids Unoccupied oasis (−108|142)")
- `defender.playerName` — Defender player name
- `defender.village` — Defender village `{id, name, x, y}` (null for oasis targets)
- `resources` — **⚠️ Always returns `null`** — use HTML parsing instead

### Oasis Reports

When the defender is an oasis, `defender.village` is `null` in GraphQL. Extract coordinates from the title field:

```javascript
// Decode HTML entities in title, then regex match
var decoded = title.replace(/&#x202[dc];/g, '').replace(/&minus;/g, '-');
var match = decoded.match(/\((-?\d+)\|(-?\d+)\)/);
if (match) { var x = parseInt(match[1]); var y = parseInt(match[2]); }
// Set player = 'Nature' for oasis targets
```

### Village Alliance Lookup (Batched)

```graphql
{
    a0: village(id: 12345) { player { alliance { tag } } }
    a1: village(id: 67890) { player { alliance { tag } } }
}
```

### Listing Reports via GraphQL

The `reports` (plural) root query **returns empty `[]`** — it cannot list reports. The `ownPlayer { reports { ... } }` field also returns empty `{}`. Report listing must be done via HTML page scraping.

---

## JavaScript API

### `Travian.Game.Reports` Methods

| Method | Description |
|--------|-------------|
| `toggleReadStatus(reportId, status)` | Toggle read/unread (0=unread, 1=read) |
| `updateSelected()` | Update bulk selection state |
| `handleMarkAsRead()` | Open mark-as-read confirmation dialog |
| `processMarkAsRead()` | Execute mark-as-read on selected reports |
| `handleDelete(force)` | Open delete confirmation dialog |
| `processDelete(force)` | Execute delete on selected reports |
| `processDeleteIds([ids])` | Delete specific report IDs |
| `updateStatus([ids], 'unread'|'read')` | Set read status for specific reports |
| `editRights(element, reportId)` | Open access permissions dialog |
| `closeDialog()` | Close the current dialog |
| `selectAll(checkbox)` | Select/deselect all report checkboxes |
| `filter.icons` | Array of currently active filter icon IDs |
| `indexUrl` | Base URL for report index (e.g., "/report/overview") |

### Report Navigation API (REST)

**Navigate between reports** (prev/next):

```
POST /api/v1/report/{reportId}/{direction}
```

**Parameters:**
- `{reportId}` — Current report ID (integer)
- `{direction}` — `prev` or `next`

**Request Body:**
```json
{
    "category": "",
    "filter": [0,1,2,3,4,5,6,7,8,11,12,13,14,15,16,17,18,19,20,21,22,23],
    "orderBy": false
}
```

- `category` — Report category filter (empty string = all categories)
- `filter` — Array of report type icon IDs to include
- `orderBy` — Sort order (false = default/newest first)

**Response:** Redirect URL to the next/previous report, or error if at boundary.

### Favourite Tab API

```
POST /api/v1/favourite-tab
```

```json
{
    "name": "report",
    "key": "overview|offensive|defensive|scouting|other|surrounding"
}
```

---

## Translation Keys

```javascript
Travian.Translation.add({
    "berichte.anonymOpponent": "make opponent anonymous",
    "berichte.anonymMyself": "make myself anonymous",
    "berichte.hiddenOwnTroops": "hide own troops",
    "berichte.hiddenOtherTroops": "hide opposing troops",
    "berichte.description": "Description:",
    "berichte.save": "Save",
    "berichte.accessAuth": "Access permissions"
});
```

---

## Programmatic Access

### Listing Reports (HTML Scraping)

```python
import requests
import re

BASE = "https://ts1.x1.europe.travian.com"
s = requests.Session()
s.cookies.set("JWT", token)
s.headers.update({"Content-Type": "application/json", "X-Version": "389"})

# List ALL reports (page 1)
r = s.get(f"{BASE}/report/all")

# Extract report IDs from checkboxes (most reliable method)
report_ids = re.findall(r'name="ids\[\]" value="(\d+)"', r.text)

# Extract rows with metadata
rows = re.findall(
    r'value="(\d+)".*?iReport(\d+).*?href="\?id=([^"]+)".*?class="dat">\s*(.+?)\s*</td',
    r.text, re.DOTALL)
for report_id, icon_type, detail_link, date in rows:
    print(f"ID={report_id}, type=iReport{icon_type}, date={date.strip()}")
```

### Fetching Report Metadata (GraphQL Batch)

```python
# Batch up to 250 report IDs per query
ids = ["72738472", "72638225", "72637342"]
aliases = " ".join([
    f'r{i}:report(objectId:"{rid}"){{time title defender{{playerName village{{id name x y}}}}}}'
    for i, rid in enumerate(ids)
])
r = s.post(f"{BASE}/api/v1/graphql",
    json={"query": "{" + aliases + "}"},
    headers={"Content-Type": "application/json", "X-Version": "389"})
data = r.json()["data"]
for i, rid in enumerate(ids):
    report = data[f"r{i}"]
    print(f"ID={rid}, time={report['time']}, title={report['title']}")
```

### Fetching Individual Report HTML

```python
r = s.get(f"{BASE}/report?id=72738472")
# Parse with BeautifulSoup or DOMParser (in JS)
```

### Parsing Scout Report Resources (JavaScript)

```javascript
function parseScoutHTML(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var wrappers = doc.querySelectorAll('.inlineIconList.resourceWrapper');
    var total = 0, stealable = -1, carryType = '';

    // Wrapper[0]: Resource totals
    if (wrappers[0]) {
        wrappers[0].querySelectorAll('.inlineIcon').forEach(function(el) {
            var ic = el.querySelector('i');
            if (ic && /lumber|clay|iron|crop/.test(ic.className)) {
                total += parseInt(el.textContent.replace(/[^\d]/g, '') || '0');
            }
        });
    }

    // Wrapper[1]: Cranny info + stealable amount
    if (wrappers.length > 1 && wrappers[1]) {
        wrappers[1].querySelectorAll('.inlineIcon').forEach(function(el) {
            var ic = el.querySelector('i');
            if (ic && ic.className.includes('carry')) {
                stealable = parseInt(el.textContent.replace(/[^\d]/g, '') || '0');
                carryType = ic.className.includes('full') ? 'full' : 'half';
            }
        });
    }

    // If no carry icon, fallback
    if (stealable < 0) stealable = Math.round(total * 0.67);

    return { total: total, stealable: stealable, carryType: carryType };
}
```

### Parsing Raid Report Bounty (JavaScript)

```javascript
function parseRaidHTML(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var wrapper = doc.querySelector('.inlineIconList.resourceWrapper');
    var bounty = 0;
    if (wrapper) {
        wrapper.querySelectorAll('.inlineIcon').forEach(function(el) {
            var ic = el.querySelector('i');
            if (ic && /lumber|clay|iron|crop/.test(ic.className)) {
                bounty += parseInt(el.textContent.replace(/[^\d]/g, '') || '0');
            }
        });
    }

    // Carry fraction (with Unicode cleaning)
    var carryEl = doc.querySelector('.inlineIcon.carry');
    var carryUsed = 0, carryMax = 0;
    if (carryEl) {
        var parts = carryEl.textContent.replace(/[^\d\/]/g, '').split('/');
        carryUsed = parseInt(parts[0]) || 0;
        carryMax = parseInt(parts[1]) || 0;
    }
    var stillLeft = carryMax > 0 ? (carryUsed >= carryMax) : false;

    return { bounty: bounty, carryUsed: carryUsed, carryMax: carryMax, stillLeft: stillLeft };
}
```

### Rally Point Link from Report

Battle reports include a link to re-send troops to the same target:
```
/build.php?id=39&tt=2&reportId={reportId}&readReport=1
```
This opens the Rally Point (gid=16, tab 2) pre-filled with target coordinates.

---

## Performance Notes

Based on verified testing against the live server:

- The server handles **200+ concurrent fetch requests** without rate limiting (no 429/503 errors)
- No artificial batching or delays are needed for HTML report fetches
- All parallel fetches can use `Promise.all()` safely
- If HTTP 429/503 occurs: wait 2s, retry once; on second failure, skip and log
- Typical pipeline for ~500 reports across 20 pages completes in < 10 seconds
- GraphQL batching: up to 250 aliases per query works reliably

---

## User Preferences (Report-Related)

Stored via `Travian.Game.Preferences`:

| Key | Description | Example |
|-----|-------------|---------|
| `entriesPerPage` | Reports per page | `"30"` |
| `reports_tab_offensive` | Offensive tab filter (base64) | `"AAACAAMA"` |
| `reports_tab_scouting` | Scouting tab filter (base64) | `"AAAQABEAEgATAA=="` |
| `allianceReportsFilter` | Alliance report filter (JSON) | `{"attackTypes":["4","5","6","1"],"noOwnAllianceAttacks":false}` |
