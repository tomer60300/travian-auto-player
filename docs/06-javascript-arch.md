# JavaScript Architecture

## Loading Order

Scripts are loaded in this exact order:

### 1. Third-Party Libraries (CDN)
```
jquery-3.5.1.min.js       — DOM manipulation, AJAX
jquery.md5.min.js          — MD5 hashing
d3.min.js + d3pie.min.js   — Data visualization, pie charts
Chart.min.js               — Chart.js for graphs
TweenMax.min.js            — GSAP animation
MorphSVGPlugin.min.js      — SVG morphing animations
simplebar.min.js           — Custom scrollbars
popper.min.js              — Tooltip positioning
tippy.min.js               — Tooltip library
pixi.min.js                — PixiJS WebGL renderer (map)
deepmerge.js               — Deep object merging
```

### 2. Game Core (Server-Specific)
```
/js/Constants.js            — Game constants (tribes, buildings, etc.)
/js/Variables.js?389        — Server variables (speed, map size, features)
/js/{lang}/Strings.js?389   — Translation strings
```

### 3. Webpack Bundles (CDN, deferred)
```
bundle/vendor.js            — React, Luxon, other npm deps
bundle/runtime.js           — Webpack runtime
bundle/main.js              — Main application code (~500KB minified)
bundle/crypt.js             — Encryption utilities
```

### 4. Consent Manager
```
consentmanager.net/delivery/autocdn/cmp-web.*.js
consentmanager.net/delivery/js/cmp_en.min.js
```

---

## Global Namespace: `Travian`

### Top-Level Structure
```
Travian
├── Constants          — Static game constants
├── Variables          — Server-specific configuration
├── Strings            — Translation keys (partially loaded)
├── Templates          — HTML templates (EventJam, ButtonV1/V2, DialogV1/V2)
├── Game               — Main game state & UI controllers
├── React              — React component renderers
├── api()              — REST API wrapper (jQuery.ajax)
├── graphQL()          — GraphQL API wrapper
├── Dialog             — Modal dialog system
├── WindowManager      — Window/panel management
├── TimersAndCounters  — Countdown timers, resource counters
├── Translation        — i18n translation system
├── Storage            — localStorage wrapper with TTL
├── Browser            — Browser detection utilities
├── Tip                — Tooltip management (tippy.js wrapper)
├── Formatter          — Number/date formatting
├── Seasons            — Visual season system
├── i18n               — RTL/LTR, number formatting
├── SVGHandler         — SVG asset loading
├── Autoreload         — Auto page refresh on stale state
├── Helpers            — String utilities
├── Draggable          — Drag & drop
├── Moveable           — Moveable panels
├── TabManager         — Tab navigation
├── AttackSymbol       — Attack indicator icons
├── Login              — Login form handler
├── Form / FormV2      — Form handling
├── DoubleClickPreventer — Prevents duplicate submissions
└── emptyFunction      — No-op function
```

### `Travian.Game` — Game Controllers
```
Travian.Game
├── language             — "en-US"
├── timestamp            — Server time (Unix)
├── timeZone             — "Asia/Jerusalem"
├── timeFormat           — 0 (EU format)
├── timezoneOffsetToUTC  — -7200
├── Preferences          — User preference storage
├── Layout               — Top bar, resource display, UI state
├── Hero                 — Hero HUD (health/XP bars)
├── Map                  — Map system (see 05-map-system.md)
├── Village              — Village management
├── ActiveVillage        — Current village
├── VillageList          — Village list sidebar
├── RallyPoint           — Troop sending UI
├── TrainingTroops       — Barracks/stable UI
├── Messages             — Message/mail system
├── AddressBook          — Contact management
├── Reports              — Report viewer
├── Profile              — Player profile
├── ExchangeResources    — NPC resource exchange
├── AllianceMembers      — Alliance member list
├── AllianceBonus        — Alliance bonus system
├── PaymentWizard        — Gold purchase flow
├── PremiumFeature       — Premium activation
├── Manual               — In-game help/manual
├── Europe               — Region minimap
├── ContextualHelp       — Tutorial/help overlay
└── Vacation             — Vacation mode
```

### `Travian.React` — React Components
```
Travian.React
├── AllianceBanner       — Alliance banner editor
├── Auctions             — Auction house
├── Buy                  — Gold purchase
├── FarmList             — Farm list manager
├── Hero                 — Hero page
├── HeroAdventure        — Adventure list
├── HeroAdventureStarted — Active adventure
├── HeroAuction          — Auction bidding
├── Login                — Login form
├── Offer                — Marketplace offer
├── PlayerProfile        — Profile viewer
├── ProductionOverview   — Resource production
├── RallyPointSimulators — Combat simulator
├── ReferAFriend         — Referral system
├── SendResources        — Resource sending dialog
├── StandaloneItem       — Item display
├── Statistics           — Game statistics
├── StyleguideReact      — (Dev) component guide
├── Tasks                — Progressive tasks
├── TradeRoutes          — Trade route manager
├── ToastStack           — Toast notifications
├── VillageBoxes         — Village sidebar boxes
├── VideoFeature         — Video ad player
├── PaymentWizard        — Payment flow
├── Dialog               — React dialog wrapper
├── openHelpDialog()     — Open help
├── openDailyQuestsDialog() — Open daily quests
├── openExchangeOffice() — Open exchange
└── openQuickLinksDialog() — Quick links editor
```

---

## Translation System

### Loading
Translations are loaded as JSON from `/js/{lang}/{group}.json`:
```javascript
fetch(`/js/en-US/layout.json`)
fetch(`/js/en-US/karte.json`)
fetch(`/js/en-US/allgemein.json`)
```

### Usage
```javascript
// In code:
Travian.Translation.get("karte.freie_oase")  // "Free oasis"
Travian.Translation.get("allgemein.ok")       // "OK"

// In templates:
"{k.spieler}"  // resolved to "Player" (from karte.json)
"{a.v3}"       // resolved to "Gauls" (from allgemein.json)
```

### Known Translation Groups
- `layout` — Header, navigation, sidebar
- `karte` — Map-related strings
- `allgemein` — General/common strings
- `hero` — Hero-related strings
- `crafting` — Item crafting strings

---

## Timer System

```javascript
Travian.TimersAndCounters = {
    timers: {},              // Active countdown timers
    resourceCounters: {},    // Resource production counters
    init(),                  // Initialize all timers on page
    initTimers(),            // Find and start all timer elements
    addTimer(id, config),    // Add a new timer
    updateTimer(id),         // Tick a timer
    initResourcesCounters()  // Start resource increment
}
```

Timers are React components (`<Me counting="down" value={seconds} />`) that tick every second client-side.

---

## Dialog System

```javascript
// V1 (jQuery-based)
Travian.Dialog.Dialog(options)
Travian.Dialog.Confirmation(options)  // Yes/No dialog
Travian.Dialog.Ajax(url, options)     // Load content via AJAX
Travian.Dialog.Api(endpoint, options) // Load from API

// V2 (React-based)
Travian.React.Dialog.open({
    component: ComponentName,
    componentProps: {...},
    cssClass: "...",
    buttonOk: true/false,
    buttonCancel: true/false,
    enableBackground: true/false
})
```

### Close Contexts
```javascript
Travian.Dialog.CLOSE_CONTEXT_FORMSUBMIT
Travian.Dialog.CLOSE_CONTEXT_OVERLAYBACKGROUND
Travian.Dialog.CLOSE_CONTEXT_CANCELBUTTON
Travian.Dialog.CLOSE_CONTEXT_CLOSEONCLICKOK
Travian.Dialog.CLOSE_CONTEXT_CLOSEONESCKEY
```

---

## Window Manager

Manages floating panels/windows:
```javascript
Travian.WindowManager.register(window)
Travian.WindowManager.closeWindow(id)
Travian.WindowManager.hideWindow(id)
Travian.WindowManager.showWindow(id)
Travian.WindowManager.closeAllWindows()
Travian.WindowManager.getWindows()
```

---

## Storage

```javascript
Travian.Storage.set(key, value)     // Save to localStorage
Travian.Storage.get(key)            // Retrieve
Travian.Storage.clear()             // Clear all

// Map-specific:
// DataStore uses localStorage for tile caching (when enabled)
// Currently disabled: persistentStorage = false
```
