# Page Routes

All pages are PHP-rendered with React hydration. No SPA routing — each is a full page load.

## Main Navigation

| Route | Page | Description |
|-------|------|-------------|
| `/dorf1.php` | Resources View | Village resource fields (lumber, clay, iron, crop tiles) |
| `/dorf2.php` | Buildings View | Village building slots, construction queue |
| `/karte.php` | Map | Interactive world map (PixiJS) |
| `/statistics` | Statistics | Player, alliance, and village rankings |
| `/report` | Reports | Battle reports, trade reports, scout reports |
| `/messages` | Messages | In-game messaging system |

### Map URL Parameters
```
/karte.php?zoom=1&x=50&y=50          — Map centered at (50, 50), zoom 1
/karte.php?d={mapId}                   — Map centered on village by mapId
/karte.php?newdid={villageId}          — Switch active village context
```

## Hero

| Route | Page |
|-------|------|
| `/hero` | Hero overview (attributes, equipment, status) |
| `/hero/adventures` | Adventure list |

## Village & Buildings

| Route | Page |
|-------|------|
| `/build.php?id={slot}` | Building slot detail |
| `/build.php?gid={buildingType}` | Building type page |
| `/build.php?gid=16&tt=1` | Rally Point — troop overview |
| `/build.php?gid=16&tt=2` | Rally Point — send troops |
| `/build.php?gid=17` | Marketplace |
| `/build.php?gid=18` | Embassy/Alliance building |
| `/build.php?newdid={villageId}&id={slot}` | Building in specific village |
| `/village/statistics` | Village statistics |

### Rally Point Tabs (gid=16)
| tt | Tab |
|----|-----|
| 1 | Troop overview |
| 2 | Send troops |
| 3 | (Troop movements) |

## Production

| Route | Page |
|-------|------|
| `/production.php?t=lumber` | Lumber production overview |
| `/production.php?t=clay` | Clay production overview |
| `/production.php?t=iron` | Iron production overview |
| `/production.php?t=crop` | Crop production overview |
| `/production.php?t=balance` | Free crop balance |

## Player & Alliance

| Route | Page |
|-------|------|
| `/profile` | Own profile |
| `/profile/{playerId}` | Other player's profile |
| `/options` | Account settings |
| `/alliance` | Alliance overview |
| `/alliance/forum` | Alliance forum |
| `/alliance/{allianceId}` | Alliance profile |

## Economy

| Route | Page |
|-------|------|
| `/auctions` | Auction house |
| `/referAFriend` | Referral program |

## Other

| Route | Page |
|-------|------|
| `/tasks` | Progressive tasks / quest overview |
| `/help.php?page=support` | Support/help |
| `/logout` | Logout (redirects to lobby) |
| `/position_details.php?mapId={id}` | Tile detail view (click from map) |

## External Links

| URL | Purpose |
|-----|---------|
| `https://www.travian.com/international` | Travian Lobby |
| `https://www.travian.com/international/news` | Game news |
| `https://www.travian.com/international/gamerules` | Game rules |
| `https://discord.gg/travianlegends` | Official Discord |
| `https://support.travian.com` | Knowledge base |
| `https://agb.traviangames.com/terms-en.pdf` | Terms of service |

## Navigation Flow

```
Lobby (travian.com) → Login → Select Server → Game Server
                                                    │
                    ┌───────────────────────────────┤
                    │                               │
                 /dorf1.php ←→ /dorf2.php ←→ /karte.php
                    │               │              │
              /production.php  /build.php     /position_details.php
                              /build.php?gid=16 (Rally Point)
                              /build.php?gid=17 (Market)
                    │
              /hero ←→ /hero/adventures
                    │
              /statistics ←→ /report ←→ /messages
                    │
              /alliance ←→ /alliance/forum
                    │
              /profile ←→ /options
```

## Resource Inline Data (All Pages)

Every page includes resource data in an inline script:
```javascript
var resources = {
    production: {"l1": 466, "l2": 442, "l3": 553, "l4": 62, "l5": 654},
    storage: {"l1": 2809, "l2": 4006, "l3": 5902, "l4": 7714},
    maxStorage: {"l1": 9600, "l2": 9600, "l3": 9600, "l4": 9600}
};
```
- `l1`-`l4`: Lumber, Clay, Iron, Crop
- `l5`: Free crop (production balance)
- `production`: Per-hour rates
- `storage`: Current amounts
- `maxStorage`: Warehouse/granary capacity
