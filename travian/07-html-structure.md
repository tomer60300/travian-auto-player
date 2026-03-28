# HTML Structure

## Page Layout

Every page follows the same layout structure:

```html
<!DOCTYPE html>
<html id="mainLayout" lang="en-US" style="--readingDirection: ltr;">
<head>
    <title>Europe 1</title>
    <meta charset="" />
    <meta name="viewport" content="width=device-width"/>
    <meta name="theme-color" content="#F4EFE4" />
    <link rel="manifest" href="/manifest.webmanifest" />
    <!-- Single CSS bundle -->
    <link href="cdn.../css_ltr/imports_compressed.css" rel="stylesheet" />
    <!-- Scripts (see 06-javascript-arch.md) -->
</head>
<body>
    <!-- Layout structure below -->
</body>
</html>
```

## DOM Structure

```
#mainLayout (html)
├── #cmpwrapper                    — Cookie consent overlay
├── #reactPortalRoot               — React portal mount point
├── #reactDialogWrapper            — React dialog container
├── #background                    — Background image
├── #topBar                        — Top navigation bar
│   ├── #logo                      — Game logo (link to lobby)
│   ├── #header                    — Header area
│   ├── #navigation                — Main nav (Resources, Buildings, Map, Stats)
│   │   ├── a → /dorf1.php        — Resources view
│   │   ├── a → /dorf2.php        — Buildings view
│   │   ├── a → /karte.php        — Map view
│   │   ├── a → /statistics       — Statistics
│   │   ├── a → /report           — Reports (with badge count)
│   │   └── a → /messages         — Messages
│   ├── #stockBar                  — Resource display
│   │   ├── #l1 / #lbar1          — Lumber (amount + progress bar)
│   │   ├── #l2 / #lbar2          — Clay
│   │   ├── #l3 / #lbar3          — Iron
│   │   ├── #l4 / #lbar4          — Crop
│   │   └── #stockBarFreeCrop     — Free crop production
│   ├── #outOfGame                 — Profile, Options, Logout
│   └── #topBarHero               — Hero portrait + health/XP bars
│       ├── #healthMask            — SVG clip for health bar
│       ├── #experienceMask        — SVG clip for XP bar
│       └── #heroImageButton       — Hero image (links to /hero)
├── #center                        — Main content area
│   ├── #sidebarBeforeContent      — Left sidebar
│   │   ├── #servertime            — Server clock
│   │   ├── #sidebarBoxAlliance    — Alliance box
│   │   ├── #sidebarBoxInfobox     — Info/notification box
│   │   └── (village list)         — Village switcher
│   └── #content / .contentContainer — Page-specific content
│       └── (varies per page)
├── footer                         — Links, copyright
└── (inline scripts)               — Page-specific initialization
```

## Key IDs

| ID | Element | Purpose |
|----|---------|---------|
| `mainLayout` | `<html>` | Root element, CSS variable host |
| `topBar` | `<div>` | Top navigation + resource bar |
| `navigation` | `<div>` | Main nav links |
| `stockBar` | `<div>` | Resource amounts |
| `l1` - `l4` | `<div>` | Resource values (lumber, clay, iron, crop) |
| `lbar1` - `lbar4` | `<div>` | Storage progress bars |
| `stockBarFreeCrop` | `<div>` | Net crop production |
| `topBarHero` | `<div>` | Hero portrait area |
| `center` | `<div>` | Main content wrapper |
| `sidebarBeforeContent` | `<div>` | Left sidebar |
| `servertime` | `<div>` | Server clock display |
| `sidebarBoxAlliance` | `<div>` | Alliance info sidebar |
| `sidebarBoxInfobox` | `<div>` | Notifications/info sidebar |
| `reactPortalRoot` | `<div>` | React component mount |
| `reactDialogWrapper` | `<div>` | Modal dialogs mount |

## Forms

| Form ID | Action | Method | Purpose |
|---------|--------|--------|---------|
| `mapCoordEnter` | `/karte.php` | GET | Enter X/Y coordinates on map |

## PWA Support

The game has basic PWA support:
```json
// /manifest.webmanifest
{
    "name": "Europe 1",
    "icons": [{"src": "/img/pwa/icon-512.png"}]
}
```

## CSS

Single compressed CSS bundle:
```
cdn.../gpack/389/css_ltr/imports_compressed.css
```

Direction-aware: `css_ltr` for LTR languages, `css_rtl` for RTL (Arabic, Hebrew).

CSS custom properties on `<html>`:
```css
--readingDirection: ltr;
--readingDirectionFrom: left;
--readingDirectionTo: right;
--scaleXFactor: 1;
```

## Inline Data Injection

Each page injects server data via inline `<script>` blocks:

```javascript
// Game state
Travian.Game.language = "en-US";
Travian.Game.timestamp = 1773923741;
Travian.Game.timeZone = "Asia/Jerusalem";

// User preferences
Travian.Game.Preferences.initialize({...});

// Resource counters
var resources = {
    production: {"l1": 466, "l2": 442, "l3": 553, "l4": 62, "l5": 654},
    storage: {"l1": 2809, "l2": 4006, "l3": 5902, "l4": 7714},
    maxStorage: {"l1": 9600, "l2": 9600, "l3": 9600, "l4": 9600}
};

// React hydration with GraphQL data
Travian.React.VillageBoxes.render({
    gqlQuery: "query{...}",
    viewData: {...}  // Pre-fetched GraphQL results
});
```
