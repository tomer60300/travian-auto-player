# Map System

## Map Configuration

- **Size:** 401×401 tiles (coordinates -200 to +200 on both axes)
- **Wrapping:** enabled (`travelOverTheWorldEdge: true`)
- **Renderer:** PixiJS canvas
- **Block size:** 600×600 pixels
- **Zoom levels:** 1-4

### Zoom Level Grid Sizes
| Zoom | Grid (tiles) | Area Per Block |
|------|-------------|----------------|
| 1 | 10×10 | Small area, full detail |
| 2 | 20×20 | Medium area |
| 3 | 100×100 | Large area |
| 4 | 100×100 | World view |

### Position Area Around Center (per zoom)
```json
{
  "1": {"left": -5, "bottom": -4, "right": 5, "top": 4},
  "2": {"left": -10, "bottom": -8, "right": 10, "top": 8},
  "3": {"left": -15, "bottom": -15, "right": 15, "top": 15},
  "4": {"left": -15, "bottom": -15, "right": 15, "top": 15}
}
```

## Data Loading

### How the Map Loads Tiles

1. **Page load** → `new Travian.Game.Map.Container(options)` initializes the map
2. **Initial data** is embedded in the page HTML as inline JSON
3. **On pan/zoom** → `Travian.Game.Map.Updater.request()` fires
4. **Block requests** → `POST /api/v1/map/info` fetches symbols for visible blocks
5. **Position requests** → `POST /api/v1/map/position` fetches tile tooltips
6. **Caching** → DataStore caches results with configurable TTLs

### Cache TTLs
```json
{
  "blocks": 1800000,    // 30 minutes
  "symbol": 600000,     // 10 minutes
  "tile": 600000,       // 10 minutes
  "tooltip": 300000     // 5 minutes
}
```

### Request Throttling
- Max concurrent requests: 5
- Delay between multi-block requests: 100ms
- Delay for position requests: 300ms

## Coordinate System

- **Origin:** (0, 0) is center of map
- **X axis:** -200 (left/west) to +200 (right/east)
- **Y axis:** -200 (bottom/south) to +200 (top/north)
- **Tile ID:** computed via `Travian.Game.Map.xy2id(x, y)`

### URL Format
```
/karte.php?zoom=1&x=50&y=50
```

## Tile Types

### Village (occupied)
Response includes `uid`, `aid`, `did`:
```json
{
  "position": {"x": -2, "y": 3},
  "uid": 316,        // player ID
  "aid": 61,         // alliance ID
  "did": 57498,      // village ID
  "title": "{k.dt} Village Name",
  "text": "{k.spieler} PlayerName<br />{k.einwohner} 645<br />{k.allianz} AllianceName<br />{k.volk} {a.v3}"
}
```

### Unoccupied Land
No `uid`/`aid`/`did`. Title shows resource distribution:
```json
{
  "position": {"x": 0, "y": 0},
  "title": "{k.vt} {k.f12}",   // Unoccupied, field type 12 (4-4-4-6)
  "text": "(0|0)"
}
```

### Oasis
Title is a terrain type:
```json
{
  "position": {"x": 5, "y": 4},
  "title": "Forest",
  "text": "(5|4)"
}
```

## Template Variables

### Title Templates
| Variable | Meaning |
|----------|---------|
| `{k.dt}` | Village (occupied) |
| `{k.vt}` | Unoccupied land |
| `{k.f1}` - `{k.f12}` | Field type (resource distribution) |

### Text Templates
| Variable | Meaning |
|----------|---------|
| `{k.spieler}` | Player |
| `{k.einwohner}` | Population |
| `{k.allianz}` | Alliance |
| `{k.volk}` | Tribe |

### Tribe Templates
| Variable | Tribe |
|----------|-------|
| `{a.v1}` | Romans |
| `{a.v2}` | Teutons |
| `{a.v3}` | Gauls |
| `{a.v4}` | Nature |
| `{a.v5}` | Natars |
| `{a.v6}` | Egyptians |
| `{a.v7}` | Huns |
| `{a.v8}` | Spartans |
| `{a.v9}` | Vikings |

### Field Types (Resource Distribution: Lumber-Clay-Iron-Crop)
| Type | Distribution |
|------|-------------|
| f1 | 1-1-1-15 (cropper) |
| f3 | 3-3-3-9 (9-cropper) |
| f4 | 4-4-4-6 (balanced) |
| f5 | 4-5-3-6 |
| f6 | 5-3-4-6 |
| f7 | 3-4-5-6 |
| f8 | 4-4-3-7 |
| f9 | 3-4-4-7 |
| f10 | 4-3-4-7 |
| f11 | 3-3-3-9 |
| f12 | 4-4-4-6 |

## Map Symbols

Symbols are overlays drawn on the map (adventures, special events):

```json
{
  "dataId": "adventure2272480",
  "x": -165, "y": 158,
  "type": "adventure",
  "parameters": {"difficulty": 0},
  "title": "Adventure",
  "text": "Adventure 32"
}
```

### Symbol Types
- `adventure` — hero adventure location
- (others TBD — war markers, alliance territory, etc.)

## Map Marks (Flags/Colors)

Players can set custom map marks:

### Layers
- **Player marks:** alliance, player, flag
- **Alliance marks:** alliance, player, flag (shared with alliance members)

### Operations
Marks are managed via the map UI dialogs, stored server-side.

## Minimap

- **Image:** `/map/minimap.jpg` (pre-rendered server-side)
- **Tooltip:** Separate tooltip HTML template
- **Zoom:** Independent from main map zoom

## Map Features

### Filter Toolbar
- Filter by player (show/hide player names)
- Filter by alliance (show/hide alliance names)
- Crop finder (Gold Club feature)
- Fullscreen mode (Travian Plus feature)

### Context Menu (Right-Click)
- Send troops
- Send merchants
- Mark alliance
- Mark player
- Mark field

### Navigation
```javascript
Travian.Game.Map._map.moveTo({x: 100, y: 100})  // programmatic pan
Travian.Game.Map._map.zoomIn()
Travian.Game.Map._map.zoomOut()
```
