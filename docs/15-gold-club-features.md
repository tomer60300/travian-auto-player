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
- **`repeatEvery` is a cycle length in HOURS, and ONE create request fans out
  into `24 / repeatEvery` separate route rows.** The dialog renders it as
  `<select name="repeatEvery">` offering exactly `24, 12, 8, 6, 4, 3, 2, 1`
  hour(s) plus `0` = "Send only once" -- the divisors of 24, which is precisely
  `DAILY_BEAT_CYCLES` in `services/distribution/merchants.py`.

  The fan-out is measured, not assumed. On a real page, every destination's row
  count equals 24 divided by the spacing between its departures:

  | rows | departure spacing | 24 / spacing |
  |---|---|---|
  | 24 | 1 h | 24 |
  | 12 | 2 h | 12 |
  | 8 | 3 h | 8 |
  | 2 | 12 h | 2 |

  Each row is then **one daily departure at a fixed time**. That is why the Edit
  dialog for an existing route has no "Repeat every" field at all: an individual
  row has no period. "Repeat every 2 hours" is a creation-time instruction that
  produces twelve daily rows, not a property any row carries afterwards.

  Consequences for this app:

  * The planner emitting one create request per origin-destination pair is
    correct, and `repeatEvery: cycle_hours` means what it says: cargo arrives
    every `cycle_hours`, delivered as `24/cycle_hours` daily rows of that cargo.
  * `max_routes_per_run` bounds create REQUESTS, not rows. A cap of 3 with
    1-hour cycles authorises up to 72 rows in the game.
  * Disabling or deleting a "route" the operator thinks of as one thing means
    acting on all `24/N` of its rows. This is why the captured toggle carried 24
    route ids in a single PUT.

- **`deliveries` (1x / 2x / 3x) is read back as `repeat`, and it means N
  CONSECUTIVE round trips by the SAME merchant set** -- automatically resent the
  moment those merchants arrive home, without needing another route or a manual
  resend. It is not N parallel loads.

  For `2x` with 75,000 crop and 7 merchants:

  ```
  dispatch 75k  -> 7 merchants outbound, occupied
  75k lands     -> the same 7 on the return leg, still occupied
  they get home -> resent immediately, automatically
  dispatch 75k  -> the same 7 outbound again
  75k lands     -> returning
  they get home -> 2x complete
  ```

  So for `deliveries: N` against a route of *m* merchants:

  | quantity | value |
  |---|---|
  | delivered per firing | N x cargo |
  | PEAK merchants occupied | m -- unchanged by N |
  | merchants held for | N x round_trip |

  The trade-off is peak occupancy against latency: `2x` with 7 merchants moves
  the same total as one go with 14, holding half as many merchants for twice as
  long. Sending 14 at once is faster and occupies more of the fleet at once.

  Consistent with the captured page: 75,000 / 12,000 per merchant = 6.25 -> 7
  merchants for ONE load, and `repeat: 2` on that row. Every other route on the
  page reads `repeat: 1, merchants: 1`.

  The app sends `1`, which is why none of this is load-bearing yet. Were it ever
  raised, the planner would have to hold `N x round_trip <= cycle_hours` or
  account for the extra in-flight sets -- `deliveries` is a lever on peak
  merchant occupancy, which is precisely the budget the optimizer allocates.

- **No merchant count is sent.** The game derives it from the cargo, so a planner's merchant figures are for budgeting and warnings only, never wire data.
- **All four resources are always present**, zeros included.
- `mode` was `"send"`; `deliveries` was `1`; `useTradeShips` was `false` (no boats on this server).
- The response body is **empty**, so the new route's id is not returned — reconciliation has to re-read the marketplace page.

#### The complete API surface, read from the client's own bundle

Taken out of the marketplace page's own JavaScript (`main.js`), so these are the
requests the game's client makes rather than shapes we inferred:

| Operation | Verb + path | Body |
|---|---|---|
| create | `POST trade-routes` | `sourceVillageId, targetCoordinates, resources, mode, hour, minute, deliveries, repeatEvery, enabled, useTradeShips` |
| edit one | `PUT trade-routes/{id}` | same minus `sourceVillageId` and `repeatEvery` |
| edit/toggle many | `PUT trade-routes` | `routes: [{<changed fields>, id}, ...]` |
| **delete** | `DELETE trade-routes` | `routes: [id, id, ...]` |

Every one also carries `action: "traderoute"`.

**The shape trap.** The bulk PUT's `routes` holds OBJECTS (`{enabled, id}`) and
DELETE's `routes` holds BARE IDS. Same endpoint, same key, different element
type -- and an empty 200 could not tell you which the server wanted.

`repeatEvery` is absent from both edit forms, which is the same fact the Edit
dialog shows by having no "Repeat every" field: the fan-out happens once, at
creation, and an individual row has no period.

**The bulk PUT answers with a body**, and the client reads it: entries in
`response.routes` carrying an `error` are the routes the game refused. A request
can therefore succeed overall while rejecting individual routes, so the response
has to be inspected -- an overall 200 is not per-route success.

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

### The create dialog, read off the live UI

The Gold Club "Create trade route" dialog carries exactly these controls, which
pins the payload field by field:

| control | payload field | notes |
|---|---|---|
| Target village (dropdown, with all / only mine / others) | `targetCoordinates` | the filter confirms routes may target villages other than your own |
| four resource inputs | `resources` | dialog states the per-merchant capacity and shows `Total: n / <fleet capacity>` plus a live `Merchants: n / <fleet>` |
| **Send** / **Deliver** radio | `mode` | `"send"` is one of TWO directions; the other is a fetch/deliver mode this app never uses |
| time field (`HH:MM`) | `hour` + `minute` | a single time of day, matching the split payload fields |
| **Deliveries**: `1x` / `2x` / `3x` radio | `deliveries` | a three-way radio, NOT a free integer: 1, 2 or 3. Read back as `repeat`. The app sends 1 |
| **Repeat every** (dropdown) | `repeatEvery` | hours, a closed set (24/12/8/6/4/3/2/1, plus 0 = send once), and it FANS OUT into 24/N rows. Absent from the Edit dialog: a row has no period |
| **Deactivate trade route** checkbox | `enabled` | inverted: checked means `enabled: false` |

### Still open

Nothing blocking. The wire format, both field units, the fan-out behaviour, the
route cap and the delete path are all settled against real captures.

### Confirmed against the live game (2026-08-25)

A single controlled route was created, read back, disabled and deleted on a real
account, and the village returned to its exact prior state (36 rows, same ids).
What that settled:

- **create** works, and the payload lands verbatim: 1,440 crop, `hour: 0`,
  `repeatEvery: 12` produced departures at 00:00 and 12:00 server time.
- **the 24/N fan-out is real.** One create with `repeatEvery: 12` produced
  exactly 2 rows, spaced 12.00 hours apart. The same village independently
  showed 24 rows at 1h gaps and 12 rows at 2h gaps for its existing routes.
- **`deliveries` reads back as `repeat`**: sent `deliveries: 1`, read `repeat: 1`.
- **disable** works: both rows went to `enabled: false`, confirmed by re-reading.
- **delete** works with the bare-id body above: both rows vanished, 38 rows back
  to 36.
- **the read-back is what makes any of this trustworthy.** The create's response
  body is empty; every claim above came from re-reading the marketplace.

### Answered by the captured page

- **Routes per village is not capped anywhere near the planner's needs.** One
  village's marketplace holds **83** routes across 6 destinations, so the cap is
  at least that. No probing needed.
- **`repeatEvery`'s unit and its legal values** — hours, from the divisors of 24;
  see above.
- **`mode` has a second direction** — the dialog's Send / Deliver radio.
- **A route CAN be deleted.** Select the row(s), press *Edit selected*, then the
  trash icon in the Edit dialog. So a created route is reversible, not permanent
  -- which is what makes a controlled live test safe to run. Deleting a
  fanned-out route means selecting all `24/N` of its rows.
- **`deliveries` is N sequential round trips by the same merchant set**, read
  back as `repeat`; it trades peak merchant occupancy for latency.


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
