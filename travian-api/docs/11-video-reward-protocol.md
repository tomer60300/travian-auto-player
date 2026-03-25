# Video Reward Protocol — Complete Reverse Engineering

## Overview

Travian uses **AdScale (oadts.com)** as the ad provider for video rewards. The protocol uses cross-origin iframe postMessage communication between the AdScale video player and the Travian game client.

## Architecture

```
┌─────────────────────────────────────┐
│         Travian Game Client          │
│  (ts1.x1.europe.travian.com)        │
│                                      │
│  1. User clicks "Watch video"        │
│  2. POST /api/v1/videofeature/open/* │
│  3. Opens iframe with AdScale URL    │
│  4. Listens for postMessage events   │
│  5. On "videoEnds:{vrid}:{hash}"     │
│  6. POST /api/v1/videofeature/ends   │
│  7. Server validates & grants reward │
└────────────┬────────────────────────┘
             │ postMessage
             ▼
┌─────────────────────────────────────┐
│         AdScale Video Player         │
│  (media.oadts.com / js.adscale.de)  │
│                                      │
│  - Loads ad creative                 │
│  - Plays video                       │
│  - Sends "videoStart" on play        │
│  - Sends "videoEnds:{vrid}:{hash}"   │
│    on completion                     │
└─────────────────────────────────────┘
```

## Step-by-Step Protocol

### Phase 1: Trigger — User Clicks "Watch Video"

The video button is a React component with appearance `"purple"` that calls `wi(options)`:

```javascript
// Options passed to the video feature:
{
    type: "productionBoost",           // or "buildingUpgrade", "adventureDuration", etc.
    displayType: "productionBoost",
    data: { /* type-specific data */ },
    skipInfoScreen: false,
    videoStop: () => {},               // cleanup callback
    videoEndsSuccess: () => {}         // reward granted callback
}
```

### Phase 2: Info Dialog (Optional)

If user hasn't checked "Don't show again", an info dialog appears:
- Component: `hi` (videoFeatureInfoDialog)
- Shows ad-blocker warning
- Has "Watch video" button
- Has "Don't show again" checkbox
- Preference stored in: `videoFeatureVideoInfoScreen` (JSON in user prefs)

### Phase 3: Open Video — Server Request

**`POST /api/v1/videofeature/open/{type}`**

Where `{type}` is one of:
- `productionBoost` — +15% resource production for 8 hours
- `buildingUpgrade` — 25% shorter building time
- `adventureDuration` — reduced adventure travel time
- `smithyUpgrade` — reduced smithy research time
- `academyResearch` — reduced academy research time

**Request body:** Type-specific data (e.g., resource type for production boost)

**Response:** Contains:
```json
{
    "vrid": "unique-video-request-id",
    "videoIframeUrl": "https://media.oadts.com/...?params..."
}
```

The `vrid` (Video Request ID) is a server-generated unique identifier that ties this video session to a specific reward.

### Phase 4: Video Playback

The game creates an iframe:
```html
<iframe id="videoArea"
    frameBorder="0"
    scrolling="no"
    src="about:blank"
    class="cmplazyload"
    data-cmp-purpose="s2"
    data-cmp-src="{videoIframeUrl}"
    allowFullScreen="true"
    allow="autoplay; fullscreen">
</iframe>
```

Additionally, AdScale's map script is loaded:
```html
<script src="//js.adscale.de/map.js"
    class="cmplazyload"
    data-cmp-purpose="s2">
</script>
```

The `cmplazyload` class + `data-cmp-purpose="s2"` integrates with the CMP (Consent Management Platform) — the iframe src only activates after cookie consent is given.

### Phase 5: PostMessage Communication

The game listens for `message` events from `media.oadts.com`:

```javascript
window.addEventListener("message", function(event) {
    // Only accept from AdScale origin
    if (event.origin !== "http://media.oadts.com" &&
        event.origin !== "https://media.oadts.com") return;

    const data = event.data;

    if (data === "videoStart") {
        // Video started playing
        // Notify server:
        Travian.api("videofeature/start", { data: { vrid: vrid } });
    }
    else if (data === "noVideo") {
        // No ad available to show
    }
    else if (data === "videoEnds") {
        // Simple completion (no verification)
    }
    else if (typeof data === "string" && data.indexOf("videoEnds:") === 0) {
        // VERIFIED COMPLETION with hash
        const payload = data.replace("videoEnds:", "");
        const colonIndex = payload.indexOf(":");
        const vrid = payload.substring(0, colonIndex);
        const hash = payload.substring(colonIndex + 1);

        // Send completion to server
        Travian.api("videofeature/ends", {
            data: { vrid: vrid, hash: hash },
            success: function() { /* reward granted */ }
        });
    }
});
```

### Phase 6: Video Start Notification

**`POST /api/v1/videofeature/start`**

```json
{
    "vrid": "unique-video-request-id"
}
```

This tells the server the video has started playing. Likely starts a server-side timer.

### Phase 7: Video Completion — THE CRITICAL REQUEST

**`POST /api/v1/videofeature/ends`**

```json
{
    "vrid": "unique-video-request-id",
    "hash": "verification-hash-from-adscale"
}
```

**This is the reward-granting request.** The server:
1. Validates the `vrid` matches an open video session
2. Validates the `hash` provided by AdScale (anti-fraud)
3. Checks minimum watch time elapsed since `/videofeature/start`
4. Grants the reward (production boost, building time reduction, etc.)

**Response:** On success, the `videoEndsSuccess` callback fires, which:
- Closes all video dialogs
- Updates the game UI to show the active bonus
- Releases the autoreload suppression

### Phase 8: Stop/Abort Flow

If user tries to close the video dialog before completion:
1. The `onCloseDialog` handler fires
2. A "Stop the video?" confirmation dialog appears
3. If user confirms stop → video cancelled, no reward
4. If user clicks "Cancel" → returns to video

---

## API Endpoints Summary

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `POST /api/v1/videofeature/open/{type}` | POST | Initiate video session, get iframe URL + vrid |
| `POST /api/v1/videofeature/start` | POST | Notify server video playback started |
| `POST /api/v1/videofeature/ends` | POST | Submit completion with verification hash |

## Security / Anti-Fraud Mechanisms

### 1. Server-Generated VRID
The `vrid` is generated server-side when `/videofeature/open` is called. It ties the video session to the specific reward type and user.

### 2. AdScale Verification Hash
The `hash` in the `videoEnds:{vrid}:{hash}` message comes from AdScale's servers, NOT from the client. This is a **server-to-server verification** — AdScale generates the hash after confirming the video was fully watched.

### 3. Timing Validation
The server knows when `/videofeature/start` was called and when `/videofeature/ends` arrives. If the gap is too short (less than the actual video duration), the server can reject it.

### 4. Double-Click Prevention
```javascript
const Ni = new Travian.DoubleClickPreventer;
Ni.timeout = 1000;  // 1 second cooldown
```

### 5. Autoreload Suppression
During video playback, page auto-reload is suppressed:
```javascript
const t = Travian.Autoreload.suppress();
// ... after video ends or cancel:
t();  // release suppression
```

### 6. CMP Integration
The video iframe uses `cmplazyload` and `data-cmp-purpose="s2"` — it only loads after the user has given cookie consent for advertising purposes.

## Video Feature Types & Rewards

### Production Boost (from Shop)
- **Type:** `productionBoost`
- **Bonus:** +15% resource production
- **Duration:** 8 hours (28800 seconds, from `durationVideoFeature`)
- **Availability:** Per resource (lumber, clay, iron, crop)
- **Cooldown:** Can activate again after current boost expires
- **Stacks with:** Gold +25% bonus (additive)

### Building Upgrade
- **Type:** `buildingUpgrade`
- **Bonus:** 25% shorter construction time
- **Applies to:** Next building placed in queue

### Adventure Duration
- **Type:** `adventureDuration`
- **Bonus:** Reduced travel time
- **Applies to:** Next adventure sent

### Smithy Upgrade
- **Type:** `smithyUpgrade`
- **Bonus:** Reduced improvement duration

### Academy Research
- **Type:** `academyResearch`
- **Bonus:** Reduced research duration

## GraphQL Query for Production Boost State

```graphql
{
  ownPlayer {
    productionBoost {
      lumber { isActive expireAt type bonus videoFeatureAvailable durationVideoFeature }
      clay { isActive expireAt type bonus videoFeatureAvailable durationVideoFeature }
      iron { isActive expireAt type bonus videoFeatureAvailable durationVideoFeature }
      crop { isActive expireAt type bonus videoFeatureAvailable durationVideoFeature }
    }
  }
}
```

**Response fields:**
- `isActive` — boolean, is boost currently running
- `expireAt` — Unix timestamp when boost expires
- `type` — "videoFeature" or "quest" or "gold"
- `bonus` — percentage (15 for video, 25 for gold/quest)
- `videoFeatureAvailable` — boolean, can user watch a video for this resource
- `durationVideoFeature` — seconds the video boost lasts (28800 = 8 hours)

## Ad Provider Details

| Property | Value |
|----------|-------|
| Provider | AdScale (Deutsche Telekom subsidiary) |
| Script URL | `//js.adscale.de/map.js` |
| Origin | `http://media.oadts.com` or `https://media.oadts.com` |
| Communication | postMessage API |
| Messages | `videoStart`, `noVideo`, `videoEnds`, `videoEnds:{vrid}:{hash}` |

## Can This Be Automated?

**Short answer: Extremely difficult.**

The `hash` in `videoEnds:{vrid}:{hash}` is generated by AdScale's servers after confirming video completion. To fake this:

1. You'd need to actually load and play the AdScale video
2. AdScale verifies the video was watched (quartile tracking, focus detection)
3. Only then does AdScale generate the hash
4. The hash is likely HMAC-signed with a shared secret between AdScale and Travian
5. Travian's server validates the hash against the vrid

**You cannot simply call `/videofeature/ends` with a fake hash.** The hash must come from AdScale's servers, which require actual video playback to generate it.

The only realistic automation would be:
- Use a real browser (not headless)
- Actually load and play the video ad
- Wait for completion
- Capture the postMessage with the real hash
- Forward it to the Travian server

This is essentially what a human does, just automated — and would likely trigger AdScale's fraud detection.
