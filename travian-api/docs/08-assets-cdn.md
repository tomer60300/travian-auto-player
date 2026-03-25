# Assets & CDN

## CDN Structure

**Base URL:** `https://cdn.legends.travian.com/gpack/389/`

```
gpack/389/
├── css_ltr/
│   └── imports_compressed.css      — All CSS (LTR)
├── css_rtl/
│   └── imports_compressed.css      — All CSS (RTL)
├── js/
│   ├── jquery-3.5.1.min.js
│   ├── jquery.md5.min.js
│   ├── d3/d3.min.js
│   ├── d3/d3pie.min.js
│   ├── ChartJs/Chart.min.js
│   ├── gsap/TweenMax.min.js
│   ├── gsap/plugins/MorphSVGPlugin.min.js
│   ├── simplebar.min.js
│   ├── popper.min.js
│   ├── tippy.min.js
│   ├── PixiJS/pixi.min.js
│   ├── deepmerge.js
│   └── bundle/
│       ├── vendor.js               — React + npm dependencies
│       ├── runtime.js              — Webpack runtime
│       ├── main.js                 — Application code (~500KB)
│       └── crypt.js                — Encryption utilities
├── img_ltr/
│   ├── hud/topBar/                 — Header/navigation sprites
│   ├── themes/default/background/  — Background images
│   └── ...
├── img_rtl/                        — Mirrored images for RTL
└── logo.png                        — Game logo
```

## Server-Hosted Assets

**Base URL:** `https://ts1.x1.europe.travian.com/`

```
/js/
├── Constants.js                    — Game constants (tribes, buildings, etc.)
├── Variables.js?389                — Server config (speed, map size, features)
└── {lang}/
    ├── Strings.js?389              — Core translation strings
    ├── layout.json                 — Layout translations
    ├── karte.json                  — Map translations
    ├── allgemein.json              — General translations
    ├── hero.json                   — Hero translations
    └── crafting.json               — Crafting translations

/map/
└── minimap.jpg                     — Pre-rendered minimap image

/img/
├── x.gif                           — 1px transparent spacer
└── pwa/icon-512.png                — PWA icon

/heroV2/head/
└── {hash}.{did}.{tribeId}.png     — Hero portrait images
```

## Image Naming Conventions

### Hero Portraits
```
/heroV2/head/{hash}.{villageId}.{tribeId}.png
Example: /heroV2/head/24cf443d79223d88c65692f199cdb78a21d8c679.24140.1.png
```

### Map Tiles
Map tiles are rendered client-side by PixiJS using sprite sheets from the CDN:
```
gpack/389/img_ltr/themes/default/background/bgBuildings.jpg
```

### CSS Sprites
Navigation and UI elements use CSS sprites:
```
gpack/389/img_ltr/hud/topBar/navigation/navigation.png
gpack/389/img_ltr/hud/topBar/headerBackground_referAFriend.png
gpack/389/img_ltr/hud/topBar/hero/frame/health.png
gpack/389/img_ltr/hud/topBar/hero/frame/experience.png
```

## Resource Loading Summary

Typical page load resources:
| Type | Count | Source |
|------|-------|--------|
| CSS | 1 | CDN (compressed bundle) |
| Scripts | ~21 | CDN + Server |
| Images | ~50 | CDN + Server |
| CSS background images | ~80 | CDN (sprites) |
| Fetch (translations) | ~5 | Server (/js/{lang}/*.json) |
| XHR (API) | ~3 | Server (/api/v1/*) |
| iframes | 1 | consentmanager.net |

## Versioning

The gpack version (`389`) is used for cache busting:
- CDN assets: embedded in URL path (`/gpack/389/...`)
- Server assets: query string (`/js/Variables.js?389`)
- API calls: `X-Version: 389` header

When the game updates, the version number increments and all caches are invalidated.
