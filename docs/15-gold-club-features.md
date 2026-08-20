# Gold Club Features & API Enforcement

Gold Club costs 200 Gold (one-time per game round) and unlocks premium features. This document maps each feature to its API enforcement status.

**Key finding: The Gold Club lock is inconsistent.** Some features are only locked in the UI (cosmetic), while others have server-side enforcement. The pattern is: **read/manage operations tend to work, but action/execution operations are blocked.**

---

## Feature Enforcement Matrix

| Feature | UI Locked | API CRUD | API Execute | Enforcement |
|---------|:-:|:-:|:-:|-------------|
| **Farm Lists** | ✅ | ✅ Works | ❌ Send blocked | Server blocks `farm-list/send` only |
| **Trade Routes** | ✅ | ✅ Works | ✅ Works | Verified 2026-08-20 with Gold Club: create `201`, toggle `200` |
| **Archive (Reports/Messages)** | ✅ | ❓ Untested | ❓ Untested | Needs Gold Club to verify |
| **Map Crop Finder** | ✅ | ❓ Untested | ❓ Untested | Needs Gold Club to verify |
| **Troop Evasion** | ✅ | ❓ Untested | ❓ Untested | Needs Gold Club to verify |
| **3x Merchant Trips** | ✅ | N/A | N/A | Likely server-enforced (game mechanic) |

---

## Farm Lists (Verified)

**Gold Club cost:** 200 Gold (included in Gold Club)
**What it provides:** Manage raid target lists, send bulk raids from Rally Point

### API Enforcement Detail

| Operation | Endpoint | Method | Without Gold Club |
|-----------|----------|--------|:-:|
| Create list | `POST /api/v1/farm-list` | POST | ✅ Works |
| Update list | `PUT /api/v1/farm-list/{id}` | PUT | ✅ Works |
| Delete list | `DELETE /api/v1/farm-list/{id}` | DELETE | ✅ Works |
| Add slot | `POST /api/v1/farm-list/slot` | POST | ✅ Works |
| Update slot | `PUT /api/v1/farm-list/slot` | PUT | ✅ Works |
| Force add slot | `POST /api/v1/farm-list/slot?force` | POST | ✅ Works |
| Delete slot | `DELETE /api/v1/farm-list/slot` | DELETE | ✅ Works |
| Query (GQL) | `farmList(id:$id){...}` | GQL | ✅ Works |
| List all (GQL) | `ownPlayer{farmLists{...}}` | GQL | ✅ Works |
| Change expanded | `POST /api/v1/farm-list/change-expanded-state` | POST | ✅ Works |
| Change sorting | `PUT /api/v1/farm-list/{id}/change-sorting` | PUT | ✅ Works |
| Change sort index | `POST /api/v1/farm-list/{id}/change-sort-index` | POST | ✅ Works |
| Close inbox | `POST /api/v1/farm-list/close-inbox` | POST | ✅ Works |
| **Send raids** | `POST /api/v1/farm-list/send` | POST | ❌ `plus.error_goldclub` |

**Error response when blocked:**
```json
{
    "error": "plus.error_goldclub",
    "errorId": "haDh214Ufu64XWZV",
    "message": "Your Gold club is not activated."
}
```

### Practical Implication

You can programmatically:
1. Create farm lists with target analysis results
2. Add targets with calculated troop counts
3. Query all farm list data (including last raid results, booty totals)
4. Manage and organize targets

But to **actually send** the raids, you need either:
- Gold Club activated (200 Gold) → use `farm-list/send`
- No Gold Club → use the two-step form POST (documented in `13-troop-sending.md`)

> See `14-farm-list-api.md` for complete API documentation with verified request/response bodies.

---

## Trade Routes

**Gold Club cost:** Included in Gold Club
**What it provides:** Automatic scheduled resource deliveries between own villages

### Verified Endpoints

Captured from a real client on 2026-08-20 (gpack 597.6, Europe 2, Gold Club active). Both are `application/json; charset=UTF-8`, sent as `fetch` with `sec-fetch-mode: cors` from the marketplace page.

| Endpoint | Method | Description | Response |
|----------|--------|-------------|----------|
| `/api/v1/trade-routes` | **POST** | Create one route | `201`, empty body |
| `/api/v1/trade-routes` | **PUT** | Enable/disable routes in bulk | `200`, updated routes |

There is **no** `/api/v1/trade-routes/toggle-group`; an earlier note here guessed one. Enable/disable is a PUT to the same collection endpoint.

#### Create — `POST /api/v1/trade-routes`

```json
{
  "action": "traderoute",
  "sourceVillageId": 20031,
  "targetCoordinates": { "x": 23, "y": 88 },
  "resources": { "lumber": 1, "clay": 2, "iron": 3, "crop": 4 },
  "mode": "send",
  "hour": 15,
  "minute": 27,
  "deliveries": 1,
  "repeatEvery": 1,
  "enabled": true,
  "useTradeShips": false
}
```

Notes, all of which corrected an earlier assumption:

- **`hour` + `minute` set the send time**, so a route's phase is chosen at creation rather than being fixed to the moment of the click. This resolves review R6 in `docs/25-resource-distribution-planner.md`: a planned beat is realisable exactly as scheduled.
- **The destination is nested** under `targetCoordinates`, not flat `x`/`y`.
- **`repeatEvery`** is the cycle length in hours.
- **No merchant count is sent.** The game derives it from the cargo, so a planner's merchant figures are for budgeting and warnings only, never wire data.
- **All four resources are always present**, zeros included.
- `mode` was `"send"`; `deliveries` was `1`; `useTradeShips` was `false` (no boats on this server).
- The response body is **empty**, so the new route's id is not returned — reconciliation has to re-read the marketplace page.

#### Enable / disable — `PUT /api/v1/trade-routes`

```json
{
  "action": "traderoute",
  "routes": [
    { "enabled": false, "id": 647196 },
    { "enabled": false, "id": 647197 }
  ]
}
```

One request carries every route being switched — the capture toggled 24 in a single call. Only the disable direction was observed; enabling is the identical body with `"enabled": true`.

> Village ids, coordinates and route ids above are stand-ins. The real capture identifies a live account and this repository is public.

### UI Access

Trade routes are managed from the Marketplace building (`/build.php?gid=17`), tab `t=3`.

### API Enforcement

Without Gold Club, `POST /api/v1/trade-routes` returns `api.unexpectedError`. With Gold Club active the same request returns `201`, which confirms the error was the Gold Club lock rather than a malformed payload.

### Still open

- Whether Gold Club caps the number of routes per village.
- What `deliveries` controls beyond the observed value of `1`.
- Whether `mode` accepts a fetch/receive direction as well as `"send"`.

---

## Archive (Reports & Messages)

**Gold Club cost:** Included in Gold Club
**What it provides:** Archive reports and messages for permanent storage

### UI Access

The "Archive" tab appears in both Reports (`/report`) and Messages (`/messages`) pages. Without Gold Club, clicking it shows a Gold Club purchase prompt.

### Known Behavior

The archive tab in reports has `goldclubDialog` configuration in its button data, suggesting UI-only lock for viewing. The actual archive/unarchive API endpoints are not yet verified.

---

## Map Crop Finder

**Gold Club cost:** Included in Gold Club
**What it provides:** Search for villages/tiles with high crop field counts on the map

### UI Access

Available as an overlay on the map page (`/karte.php`).

### Potential API

The crop finder likely uses the existing map API endpoints (`/api/v1/map/position`, `/api/v1/map/info`) with additional filtering parameters. The data for crop field distribution is already available in tile data (field type templates like `{k.f3}`, `{k.f4}`, etc.), so a crop finder could be built programmatically using existing documented endpoints without Gold Club.

---

## Troop Evasion

**Gold Club cost:** Included in Gold Club
**What it provides:** Automatically send troops away from capital before incoming attacks

### UI Access

Configured in village settings or Rally Point.

### Notes

This is likely a server-side automation feature — the server detects incoming attacks and automatically dispatches a pre-configured evasion. Unclear if any API endpoints exist beyond configuration.

---

## 3x Merchant Trips

**Gold Club cost:** Included in Gold Club
**What it provides:** Each merchant completes up to 3 trips per trade order

### Notes

This is a pure game mechanic change (server-side multiplier). No separate API endpoint — it modifies the behavior of existing marketplace trade requests. Without Gold Club, each merchant makes 1 trip.

---

## Gold Club Status Check

### GraphQL

```graphql
{
    ownPlayer {
        goldFeatures {
            goldClub
            travianPlus { isActive }
        }
    }
}
```

**Response (not activated):**
```json
{
    "data": {
        "ownPlayer": {
            "goldFeatures": {
                "goldClub": false,
                "travianPlus": {
                    "isActive": false
                }
            }
        }
    }
}
```

### Error Pattern

When a Gold Club feature is server-enforced, the error follows this pattern:
```json
{
    "error": "plus.error_goldclub",
    "errorId": "{randomId}",
    "message": "Your Gold club is not activated."
}
```

### UI Lock Pattern

In the page HTML, Gold Club-locked tabs use a `goldclubDialog` configuration:
```json
{
    "featureKey": "raidList|messageArchive|...",
    "infoIcon": "https://support.travian.com/.../gold-club",
    "cssClass": "premiumFeaturePackage premiumFeatureGoldclub paymentShopV4",
    "premiumFeatureDialogVersion": 2,
    "version": 2,
    "paymentShopVersion": 4
}
```

This triggers a purchase dialog instead of navigating to the feature page. The API endpoints behind these pages may still be accessible directly.
