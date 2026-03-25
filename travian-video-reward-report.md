# Travian Legends — Video Reward Protocol: Full Reverse-Engineering Report

**Date:** 2026-03-22  
**Server:** ts1.x1.europe.travian.com (Europe 1)  
**Feature:** Free +15% production bonus for 8 hours via video ad watch  
**Game bundle version:** gpack/389  
**Ad network:** OpenX (media.oadts.com) + Google IMA SDK (imasdk.googleapis.com)

---

## Executive Summary

The Travian video reward flow uses a **3-phase server handshake** paired with a **postMessage protocol** between the ad iframe and the parent page. Video completion is verified via a **server-generated `vrid` (Video Request ID)** that is signed by the ad server with an MD5 hash. The Travian backend validates the hash before granting the bonus. There is no real-time video streaming verification — only a signed completion token checked at the end.

**Reward confirmed:** Clay production +15% bonus activated for 07:59:43 (≈8 hours) immediately after the `videofeature/ends` call returned `{"token":"LH7lEA1Y5DsxJhl6"}`.

---

## Attempt 1: Ad Blocker Active (Lumber, 2026-03-22 ~17:48)

**Resource:** Lumber  
**Button ref:** `.bonusVideo button.textButtonV2` (first)  
**Outcome:** Video dialog opened but iframe was blocked — no `videoStart`, no completion. Ad blocker or third-party cookie blocking prevented the ad from loading.

**Key capture:**
- `POST /api/v1/videofeature/open/productionBoost` → returned `vrid` + iframe URL
- DOM: `<script src="//js.adscale.de/map.js" class="cmplazyload" data-cmp-purpose="s2">` injected
- Modal message shown: *"Are you using an ad blocker or declining third-party cookies?"*
- No `videoStart` XHR, no `videoEnds:` postMessage

---

## Attempt 2: Ad Blocker Disabled (Clay, 2026-03-22 ~17:53)

**Resource:** Clay  
**Outcome:** Full success — video played to completion, bonus activated.

---

## 1. Full Event Timeline

| Time (ms) | Δ from click | Event | Direction |
|-----------|-------------|-------|-----------|
| 1774194774875 | baseline | Page load: `POST /api/v1/payment-wizard` | → Server |
| 1774194774990 | +115ms | `POST /api/v1/graphql` — bootstrap + productionBoost state query | → Server |
| 1774194793599 | click | User clicks "Activate" button (clay +15% free) | User |
| 1774194793602 | +3ms | `POST /api/v1/graphql` — gold amount check | → Server |
| 1774194793605 | +6ms | `POST /api/v1/videofeature/open/productionBoost` — `{"resource":"clay"}` | → Server |
| 1774194793724 | +125ms | Response: `{vrid, videoIframeUrl}` | ← Server |
| 1774194793724 | +125ms | DOM: `<script src="//js.adscale.de/map.js">` injected (ad consent layer) | DOM |
| 1774194794215 | +616ms | postMessage from `media.oadts.com`: `__tcfapiCall getTCData` (consent check) | ← iframe |
| 1774194794215 | +616ms | postMessage: `atg://{"cmd":"fire","fire":"create","id":"oadts"}` | ← iframe |
| 1774194794215 | +616ms | postMessage: `"created"` | ← iframe |
| — | ~60s | User clicks play button in video area | User |
| 1774194895273 | +101674ms | postMessage: `addEventListener` TCF v2, USP, GPP (consent negotiation) | ← iframe |
| 1774194896599 | +103000ms | postMessage: `removeEventListener` GPP | ← iframe |
| 1774194896869 | +103270ms | postMessage: `"loaded"` | ← iframe |
| 1774194896869 | +103270ms | postMessage: `"ready"` | ← iframe |
| 1774194896967 | +103368ms | postMessage: **`"videoStart"`** → triggers `/api/v1/videofeature/start` | ← iframe |
| 1774194896969 | +103370ms | `POST /api/v1/videofeature/start` — `{"vrid":"DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V"}` | → Server |
| 1774194896970 | +103371ms | postMessage: `ad.start` | ← iframe |
| 1774194896970 | +103371ms | postMessage: `ad.impression` | ← iframe |
| 1774194897115 | +103516ms | Response: `[]` (start acknowledged) | ← Server |
| 1774194905432 | +111833ms | postMessage: `ad.firstQuartile` (25% complete) | ← iframe |
| 1774194912900 | +119301ms | postMessage: `ad.midpoint` (50% complete) | ← iframe |
| 1774194920350 | +126751ms | postMessage: `ad.thirdQuartile` (75% complete) | ← iframe |
| 1774194927749 | +134150ms | postMessage: `ad.complete` (100%) | ← iframe |
| 1774194927855 | +134256ms | postMessage: `"content"` | ← iframe |
| 1774194927958 | +134359ms | postMessage: `"videoComplete"` | ← iframe |
| 1774194928331 | +134732ms | postMessage: **`"videoEnds:DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V:b195edbc07b2a81ecc71698035df3bfe"`** | ← iframe |
| 1774194928332 | +134733ms | `POST /api/v1/videofeature/ends` — `{"vrid":"DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V","hash":"b195edbc07b2a81ecc71698035df3bfe"}` | → Server |
| 1774194928333 | +134734ms | postMessage: `atg://{"cmd":"sign","vrid":"DVxrO5...","sign":"b195edbc..."}` | ← iframe |
| 1774194928587 | +134988ms | Response: **`{"token":"LH7lEA1Y5DsxJhl6"}`** — REWARD GRANTED | ← Server |
| 1774194928581 | +134982ms | `POST /api/v1/payment-wizard` — shop UI refresh | → Server |
| 1774194928690 | +135091ms | `POST /api/v1/graphql` — full state refresh | → Server |
| 1774194928809 | +135210ms | Response: `clay.isActive=true, expireAt=1774223728, type="videoFeature", bonus=15` | ← Server |

**Total video duration:** ~31 seconds (firstQuartile at T+8866ms, complete at T+30782ms from `videoStart`)

---

## 2. Complete Network Log

### 2.1 XHR Request 1 — Page Bootstrap
```
POST /api/v1/payment-wizard
Content-Type: application/json
X-Version: 389

{"action":"paymentWizard","goldProductId":"","goldProductLocation":"","activeTab":"pros"}

Response 200: HTML for advantages shop (includes React render call)
```

### 2.2 XHR Request 2 — Bootstrap State Query
```
POST /api/v1/graphql
Content-Type: application/json
X-Version: 389

{"query":"{bootstrapData{timestamp lastResetTimestamp goldActionPrices{resourceBonusLumber resourceBonusClay resourceBonusIron resourceBonusCrop plus goldClub}}ownPlayer{wallet{goldAmount}accessRights{buySpendGold}goldFeatures{lumberProductionBonus{...subscriptionFeatureFields}clayProductionBonus{...subscriptionFeatureFields}ironProductionBonus{...subscriptionFeatureFields}cropProductionBonus{...subscriptionFeatureFields}travianPlus{...subscriptionFeatureFields}goldClub}productionBoost{lumber{...productionBoostFields}clay{...productionBoostFields}iron{...productionBoostFields}crop{...productionBoostFields}}}}fragment subscriptionFeatureFields on SubscriptionFeature{isAutoProlonged isActive expiresAt duration}fragment productionBoostFields on ProductionBoost{isActive expireAt type bonus videoFeatureAvailable durationVideoFeature}"}

Response 200:
{
  "data": {
    "bootstrapData": {
      "timestamp": 1774194775,
      "lastResetTimestamp": 1774191600,
      "goldActionPrices": {"resourceBonusLumber":5,"resourceBonusClay":5,"resourceBonusIron":5,"resourceBonusCrop":5,"plus":10,"goldClub":200}
    },
    "ownPlayer": {
      "wallet": {"goldAmount": 130},
      "accessRights": {"buySpendGold": true},
      "productionBoost": {
        "lumber":  {"isActive":false,"expireAt":null,"type":null,"bonus":null,"videoFeatureAvailable":true,"durationVideoFeature":28800},
        "clay":    {"isActive":false,"expireAt":null,"type":null,"bonus":null,"videoFeatureAvailable":true,"durationVideoFeature":28800},
        "iron":    {"isActive":false,"expireAt":null,"type":null,"bonus":null,"videoFeatureAvailable":true,"durationVideoFeature":28800},
        "crop":    {"isActive":false,"expireAt":null,"type":null,"bonus":null,"videoFeatureAvailable":true,"durationVideoFeature":28800}
      }
    }
  }
}
```

**Key field:** `videoFeatureAvailable: true` — this flag controls whether the free button is shown. `durationVideoFeature: 28800` = 8 hours in seconds.

### 2.3 XHR Request 3 — Gold Amount Check (fires simultaneously with open)
```
POST /api/v1/graphql
{"query":"query{ownPlayer{wallet{goldAmount}}}"}

Response 200: {"data":{"ownPlayer":{"wallet":{"goldAmount":130}}}}
```

### 2.4 XHR Request 4 — **OPEN VIDEO SESSION** ⭐
```
POST /api/v1/videofeature/open/productionBoost
Content-Type: application/json; charset=UTF-8
X-Version: 389
cache-control: no-store

{"resource":"clay"}

Response 200:
{
  "vrid": "DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V",
  "videoIframeUrl": "//media.oadts.com/www/delivery/afv.php?zoneid=3716&vrid=DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V&cb=1774194793&loc=https://travian.com"
}
```

**Notes:**
- `vrid` = 32-character alphanumeric Video Request ID (server-generated, unique per session)
- `zoneid=3716` = OpenX ad zone for Travian video rewards
- `cb=1774194793` = Unix timestamp used as cache-buster
- `loc=https://travian.com` = location hint passed to ad server

**Attempt 1 (lumber):** `vrid = Sk8B75zVx2fWzugDVzl8X4nkmnf5WgZ7`  
**Attempt 2 (clay):** `vrid = DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V`

### 2.5 XHR Request 5 — **VIDEO START NOTIFICATION** ⭐
```
POST /api/v1/videofeature/start
{"vrid":"DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V"}

Response 200: []
```

Triggered immediately when the `"videoStart"` postMessage is received from the ad iframe.

### 2.6 XHR Request 6 — **VIDEO COMPLETION CLAIM** ⭐
```
POST /api/v1/videofeature/ends
{"vrid":"DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V","hash":"b195edbc07b2a81ecc71698035df3bfe"}

Response 200:
{"token":"LH7lEA1Y5DsxJhl6"}
```

- `hash` = MD5 signature provided by the ad server in the `videoEnds:` postMessage
- `token` = server-issued reward token (16-char alphanumeric) — confirms bonus was granted

### 2.7 XHR Request 7 — Shop UI Refresh (post-completion)
```
POST /api/v1/payment-wizard
{"action":"paymentWizard","goldProductId":"","goldProductLocation":"","activeTab":"pros"}

Response 200: Updated HTML showing clay +15% active countdown
```

### 2.8 XHR Request 8 — State Refresh (post-completion)
```
POST /api/v1/graphql
[same full bootstrap query as 2.2]

Response 200 — critical change:
"clay": {
  "isActive": true,
  "expireAt": 1774223728,   ← ~8 hours from now
  "type": "videoFeature",
  "bonus": 15,
  "videoFeatureAvailable": false,   ← no longer available (cooldown active)
  "durationVideoFeature": 28800
}
```

---

## 3. WebSocket Messages

**None observed.** The entire protocol is stateless HTTP + postMessage. No WebSocket connections were opened at any point during the video reward flow.

---

## 4. DOM Changes

| Time | Tag | Class/ID | src |
|------|-----|----------|-----|
| T+125ms | `<script>` | `class="cmplazyload" data-cmp-purpose="s2"` | `//js.adscale.de/map.js` |
| T+134.98s | `<script>` | inline | React PaymentWizard re-render |
| T+134.98s | `<div>` | `advantagesBonusBox goldClub` | — |
| T+134.98s | `<div>` | `advantagesBonusBox travianPlus` | — |
| T+134.98s | `<div>` | `advantagesBonusBox lumberProductionBonus` | — |
| T+134.98s | `<div>` | `advantagesBonusBox clayProductionBonus active` | — |
| T+134.98s | `<div>` | `advantagesBonusBox ironProductionBonus` | — |
| T+134.98s | `<div>` | `advantagesBonusBox cropProductionBonus` | — |

The `active` CSS class on `clayProductionBonus` visually marks the bonus as live. The iframe itself is not tracked by the MutationObserver (it was injected as part of the dialog HTML, not appended to body separately).

**Iframe element (in dialog HTML):**
```html
<iframe id="videoArea" frameborder="0" scrolling="no"
  src="//media.oadts.com/www/delivery/afv.php?zoneid=3716&vrid=DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V&cb=1774194793&loc=https://travian.com"
  class="cmplazyload"
  data-cmp-purpose="s2"
  data-cmp-src="//media.oadts.com/www/delivery/afv.php?zoneid=3716&..."
  allowfullscreen="" allow="autoplay; fullscreen"
  data-cmp-done="1" data-cmp-ab="1" data-cmp-activated="1">
</iframe>
```

---

## 5. PostMessage Events (Complete Sequence)

All messages from `https://media.oadts.com` or `https://imasdk.googleapis.com`. The parent page listens only for messages matching specific string patterns.

### Phase A: Consent/Init (pre-play)
| Message | Purpose |
|---------|---------|
| `{"__tcfapiCall":{"command":"getTCData","version":2,"callId":"..."}}` | GDPR consent check |
| `"atg://{"cmd":"fire","fire":"create","id":"oadts"}"` | Ad container created |
| `"created"` | **Travian-consumed**: ad player initialized |
| `{"__tcfapiCall":{"command":"addEventListener",...}}` | Watch for consent changes |
| `{"__uspapiCall":{"command":"getUSPData",...}}` | CCPA data request |
| `{"__gppCall":{"command":"addEventListener",...}}` | GPP consent watch |
| `"get"` | Internal ad player state request |
| `"ima://{"name":"gpt","type":"isGptPresent",...}"` | Google IMA/GPT presence check |
| `"atg://{"cmd":"fire","fire":"loaded","id":"oadts"}"` | Ad assets loaded |
| `"loaded"` | **Travian-consumed** |
| `"atg://{"cmd":"fire","fire":"ready","id":"oadts"}"` | Ad ready to play |
| `"ready"` | **Travian-consumed** |

### Phase B: Playback
| Message | Purpose |
|---------|---------|
| `"atg://{"cmd":"fire","fire":"start","id":"oadts"}"` | Ad started playing |
| **`"videoStart"`** | **TRIGGER**: Travian calls `POST /api/v1/videofeature/start` |
| `"atg://{"cmd":"tell","tell":"ad.start","plItem":0,...}"` | VAST tracking: ad start |
| `"atg://{"cmd":"tell","tell":"ad.impression","plItem":0,...}"` | VAST tracking: impression |
| `"atg://{"cmd":"tell","tell":"ad.firstQuartile","plItem":0,...}"` | 25% complete |
| `"atg://{"cmd":"tell","tell":"ad.midpoint","plItem":0,...}"` | 50% complete |
| `"atg://{"cmd":"tell","tell":"ad.thirdQuartile","plItem":0,...}"` | 75% complete |
| `"atg://{"cmd":"tell","tell":"ad.complete","plItem":0,...}"` | 100% complete |
| `"atg://{"cmd":"tell","tell":"ad.complete","plItem":0,...}"` | (duplicate emission) |

### Phase C: Completion
| Message | Purpose |
|---------|---------|
| `"content"` | Transitioning to content frame |
| `"atg://{"cmd":"fire","fire":"end","id":"oadts"}"` | Ad session ended |
| `"videoComplete"` | **Travian-consumed** (no server call made for this one) |
| `"atg://{"cmd":"tell","tell":"content","plItem":0,"at":0.268006,"duration":0.5005,...}"` | Content position info |
| **`"videoEnds:DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V:b195edbc07b2a81ecc71698035df3bfe"`** | **TRIGGER**: Travian calls `POST /api/v1/videofeature/ends` |
| `"atg://{"cmd":"sign","vrid":"DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V","sign":"b195edbc07b2a81ecc71698035df3bfe",...}"` | Ad server signs completion |

---

## 6. Completion Protocol — How "Video Watched" Is Verified

The verification is a **challenge-response between the ad server and Travian backend**:

### Step-by-step:
1. **Client clicks Activate** → Travian backend creates a video session:
   - Generates `vrid` (32-char random token, e.g. `DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V`)
   - Passes `vrid` to the OpenX ad server embedded in the iframe URL
   - Stores `vrid` server-side associated with the player's session and resource type

2. **Ad plays** → When video completes, the OpenX ad server (running inside the iframe):
   - Signs the completion with an **MD5 hash**: `hash = MD5(vrid + some_secret_or_data)`
   - Emits postMessage: `"videoEnds:{vrid}:{hash}"`
   - Separately also emits `atg://{"cmd":"sign","vrid":"...","sign":"..."}` (same data, different format)

3. **Client receives `videoEnds:`** → JavaScript parses `vrid` and `hash`, sends:
   ```
   POST /api/v1/videofeature/ends
   {"vrid":"DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V","hash":"b195edbc07b2a81ecc71698035df3bfe"}
   ```

4. **Travian backend validates**:
   - Looks up `vrid` → confirms it belongs to this session and resource
   - Re-computes expected hash (knowing the secret used to generate it)
   - If valid: grants bonus, sets `expireAt = now + 28800`, returns `{"token":"..."}` 
   - If invalid/replayed: rejects (presumably 4xx)

5. **Client closes dialog** → React refreshes shop state via GraphQL

### The `videofeature/start` call:
- Called when `"videoStart"` postMessage is received
- Returns `[]` (empty array)
- Likely records the start timestamp server-side and prevents the video session from being re-used if never started
- Does NOT affect the reward — only `ends` grants the bonus

### What the client JS does (from decompiled `xi` function in main.js):
```javascript
const xi = function(e) {
  const t = (t) => {
    if ("http://media.oadts.com" === t.origin || "https://media.oadts.com" === t.origin) {
      const a = t.data;
      if ("videoStart" === a)
        e.vrid && Travian.api("videofeature/start", {data: {vrid: e.vrid}});
      else if ("noVideo" === a)
        ; // no-op
      else if ("videoEnds" === a)
        ; // no-op (bare "videoEnds" without hash is ignored)
      else if (typeof a === "string" && a.indexOf("videoEnds:") === 0) {
        const t = a.replace("videoEnds:", "");
        const s = t.indexOf(":");
        Travian.api("videofeature/ends", {
          data: {
            vrid: t.substring(0, s),
            hash: t.substring(s + 1)
          },
          success: e.videoEndsSuccess   // closes dialog + re-renders shop
        });
      }
    }
  };
  // ... registers event listener, injects adscale script
};
```

---

## 7. Security Analysis

### Tokens and identifiers observed:
| Token | Value (example) | Length | Notes |
|-------|----------------|--------|-------|
| `vrid` | `DVxrO5MNxIEPDkGaaLbo54lsY9efCX2V` | 32 chars | Base62-like, server-generated per session |
| `hash` | `b195edbc07b2a81ecc71698035df3bfe` | 32 chars | MD5 hex digest, generated by ad server |
| `token` (reward) | `LH7lEA1Y5DsxJhl6` | 16 chars | Issued on successful `ends` call |
| `cb` | `1774194793` | 10 digits | Unix timestamp, cache-buster only |

### Anti-replay mechanisms:
- **`vrid` is single-use**: After a successful `videofeature/ends`, the `videoFeatureAvailable` field flips to `false` for that resource, preventing re-use
- **Session binding**: `vrid` is tied to the authenticated player session (standard cookie auth) — cannot be used cross-account
- **Hash validation**: The `hash` can only be computed by the ad server (server knows a shared secret with OpenX). The client never sees the secret — it just relays the hash from the iframe postMessage
- **Timestamp binding**: The `cb` field in the iframe URL is a Unix timestamp — the server can reject stale session tokens
- **Rate limiting**: After activation, `videoFeatureAvailable` becomes `false` and the bonus runs for 8 hours (`durationVideoFeature: 28800`). The cooldown is enforced server-side

### Potential attack surface:
- The `videoEnds` message format `"videoEnds:{vrid}:{hash}"` is sent over postMessage cross-origin from `media.oadts.com` to the Travian parent page. Any script running in the context of `media.oadts.com` (e.g. if the ad server itself were compromised) could emit this message.
- The hash is MD5 — if the shared secret between OpenX and Travian were known, hashes could be forged. MD5 is not cryptographically strong, but the secret is never exposed client-side.
- The `vrid` must be valid (server-side lookup) so even with a known hash algorithm, you'd need a valid `vrid` first which requires a real `open` call.
- **No client-side video completion verification**: Travian's JS does NOT verify that the video actually played — it only waits for the `videoEnds:` postMessage from the iframe origin. If `media.oadts.com` sends this message without a real video play (e.g. broken ad), the reward would still be granted if the hash is correct.

### Response headers (security-relevant):
```
cache-control: no-store
content-security-policy: frame-ancestors 'self'
x-frame-options: SAMEORIGIN
access-control-allow-origin: https://ts1.x1.europe.travian.com
```

---

## 8. Architecture Flow Diagram

```
USER BROWSER                    TRAVIAN SERVER              OPENX AD SERVER
     │                               │                           │
     │  [Click "Activate" button]    │                           │
     ├──POST /api/v1/graphql────────►│ (gold amount check)       │
     ├──POST /videofeature/open/─────►│                           │
     │       productionBoost         │                           │
     │   body: {"resource":"clay"}   │                           │
     │                               ├──create vrid session──────┤
     │                               │   vrid=DVxrO5MN...        │
     │◄──{vrid, videoIframeUrl}──────┤                           │
     │                               │                           │
     │  [Render iframe]              │                           │
     │──────────────────────────────────────────────────────────►│
     │   GET media.oadts.com/www/delivery/afv.php                │
     │        ?zoneid=3716&vrid=DVxrO5MN...&cb=177419...         │
     │                               │                           │
     │  [Consent negotiation]        │                           │
     │◄──postMessage: getTCData──────────────────────────────────┤
     │◄──postMessage: "created"──────────────────────────────────┤
     │                               │                           │
     │  [User clicks Play]           │                           │
     │                               │                           │
     │◄──postMessage: "loaded"───────────────────────────────────┤
     │◄──postMessage: "ready"────────────────────────────────────┤
     │◄──postMessage: "videoStart"───────────────────────────────┤
     │                               │                           │
     ├──POST /videofeature/start─────►│                           │
     │   body: {"vrid":"DVxrO5MN..."}│                           │
     │◄──[]──────────────────────────┤                           │
     │                               │                           │
     │  [Video plays ~30 seconds]    │                           │
     │◄──postMessage: ad.firstQuartile────────────────────────────┤
     │◄──postMessage: ad.midpoint────────────────────────────────┤
     │◄──postMessage: ad.thirdQuartile────────────────────────────┤
     │◄──postMessage: ad.complete────────────────────────────────┤
     │◄──postMessage: "videoComplete"────────────────────────────┤
     │                               │                           │
     │  [Ad server signs completion] │                           │
     │◄──postMessage: "videoEnds:    │     hash = MD5(vrid+secret)
     │    DVxrO5MN...:b195edbc..."───────────────────────────────┤
     │                               │                           │
     ├──POST /videofeature/ends──────►│                           │
     │   body: {vrid, hash}          │                           │
     │                               ├──validate hash────────────┤
     │                               │   (shared secret w/ OpenX)│
     │                               ├──grant bonus              │
     │                               │   clay.isActive=true      │
     │                               │   expireAt=now+28800      │
     │◄──{"token":"LH7lEA1Y5DsxJhl6"}┤                           │
     │                               │                           │
     ├──POST /api/v1/graphql─────────►│ (full state refresh)      │
     │◄──{clay.isActive:true,        │                           │
     │    expireAt:1774223728,       │                           │
     │    type:"videoFeature",       │                           │
     │    bonus:15,                  │                           │
     │    videoFeatureAvailable:false│                           │
     │   }───────────────────────────┤                           │
     │                               │                           │
     │  [UI shows "+15% active for   │                           │
     │   07:59:43"]                  │                           │
```

---

## 9. API Endpoint Reference

| Endpoint | Method | Auth | Request Body | Response |
|----------|--------|------|-------------|---------|
| `/api/v1/videofeature/open/{type}` | POST | Session cookie | `{"resource":"clay"}` | `{"vrid":"...","videoIframeUrl":"..."}` |
| `/api/v1/videofeature/start` | POST | Session cookie | `{"vrid":"..."}` | `[]` |
| `/api/v1/videofeature/ends` | POST | Session cookie | `{"vrid":"...","hash":"..."}` | `{"token":"..."}` |
| `/api/v1/graphql` | POST | Session cookie | GraphQL query | State data |
| `/api/v1/payment-wizard` | POST | Session cookie | `{"action":"paymentWizard",...}` | Shop HTML |

**`{type}` values observed:** `productionBoost`  
**Other `{type}` values (from source code):** `buildingUpgrade`, `adventureDuration`, `smithyUpgrade`, `adventureDifficulty`, `dailyQuest`

**Common request header:**  
`X-Version: 389` (game bundle version, required)

---

## 10. GraphQL Schema (productionBoost fields)

```graphql
type ProductionBoost {
  isActive: Boolean
  expireAt: Int          # Unix timestamp
  type: String           # "videoFeature" or "gold"
  bonus: Int             # 15 (video) or 25 (gold)
  videoFeatureAvailable: Boolean
  durationVideoFeature: Int   # 28800 = 8 hours
}
```

Available resources: `lumber`, `clay`, `iron`, `crop`

---

## 11. Ad Network Details

- **Ad server:** OpenX — `media.oadts.com`  
- **Ad zone:** `zoneid=3716` (Travian-specific video reward zone)
- **Ad SDK:** Google IMA (Interactive Media Ads) — `imasdk.googleapis.com`
- **Consent:** IAB TCF v2.0, USP (CCPA), GPP via `consentmanager.net`
- **Consent manager:** `cdn.consentmanager.net` (cdid=40dcf06677fd)
- **Ad blocker detection:** `js.adscale.de/map.js` (Adscale — German ad tech)
- **Video content observed:** "Arkham: Realms at War" game ad (~30 seconds)
- **atg:// protocol:** OpenX's "Advertising Tag Gateway" internal event bus

---

## Appendix A: Decompiled Client-Side Video Handler

From `https://cdn.legends.travian.com/gpack/389/js/bundle/main.js`:

```javascript
// xi = React component managing the video iframe
const xi = function(e) {
  const t = (t) => {
    if ("http://media.oadts.com" === t.origin || "https://media.oadts.com" === t.origin) {
      const a = t.data;
      if ("videoStart" === a)
        e.vrid && Travian.api("videofeature/start", {data: {vrid: e.vrid}});
      else if ("noVideo" === a)
        ; // ad failed to load
      else if ("videoEnds" === a)
        ; // bare signal without hash — ignored
      else if (typeof a === "string" && 0 === a.indexOf("videoEnds:")) {
        const t = a.replace("videoEnds:", "");
        const s = t.indexOf(":");
        Travian.api("videofeature/ends", {
          data: {
            vrid: t.substring(0, s),
            hash: t.substring(s + 1)
          },
          success: e.videoEndsSuccess  // triggers dialog close + shop refresh
        });
      }
    }
  };
  useEffect(() => {
    window.__cmp?.("checkBlocking", void 0, void 0, true);
    injectScript("//js.adscale.de/map.js", [["class","cmplazyload"],["data-cmp-purpose","s2"]]);
    window.addEventListener("message", t);
    return () => window.removeEventListener("message", t);
  }, []);
  // renders iframe with data-cmp-src (CMP lazy-loads it after consent)
};

// wi = openVideo() entry point  
const wi = (e) => {
  if (!Ni.check()) return;  // double-click prevention
  const suppress = Travian.Autoreload.suppress();
  const openVideoDialog = () => {
    Travian.api(`videofeature/open/${e.type}`, {
      data: e.data,
      success: (response) => {
        Travian.React.Dialog.open({
          component: xi,
          componentProps: {...e, ...response, videoEndsSuccess: () => {
            e.videoEndsSuccess?.();
            closeDialogs();
            suppress();
          }},
          cssClass: "videoFeature videoFeatureVideoDialog",
          buttonCancel: true
        }, "videoFeatureVideoDialog", ["videoFeature"]);
      }
    });
  };
  // Show info screen first (unless skipInfoScreen=true), then openVideoDialog()
};
```

---

## Appendix B: Production Boost Button HTML

```html
<div class="bonusVideo">
  <span class="separator">or</span>
  <span class="bonusText">
    <!-- SVG arrow icon -->
    <span><strong>+15%</strong> for <strong>8 hours</strong></span>
  </span>
  <button class="textButtonV2 buttonFramed withTextAndIcon rectangle withText purple" type="button">
    <div>
      <span>Activate</span>
      <i class="videoIcon"></i>
    </div>
  </button>
</div>
```

CSS class `purple` distinguishes video reward buttons from gold buttons (which are `gold`).

---

*Report generated by automated browser interception research — 2026-03-22*
