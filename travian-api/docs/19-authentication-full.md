# Authentication — Complete Guide for Programmatic Access

This document covers every way to authenticate to Travian Legends as a Python script (or any HTTP client), from simplest to most robust.

---

## Authentication Overview

Travian has **two authentication paths**:

```
PATH 1: Direct Game Server Login (simplest)
┌──────────────┐    POST /api/v1/auth/login     ┌──────────────┐
│ Python Script │ ─────────────────────────────→ │ Game Server  │
│               │ ←───────────────────────────── │ ts1.x1...    │
│               │    Set-Cookie: JWT=...         │              │
└──────────────┘    + {redirectTo: "/dorf1.php"} └──────────────┘

PATH 2: Lobby → Game Server (browser flow)
┌──────────────┐    Login via SPA      ┌──────────────┐    Redirect + JWT    ┌──────────────┐
│   Browser    │ ──────────────────→  │ Travian Lobby │ ──────────────────→ │ Game Server  │
│              │                       │ travian.com   │                     │ ts1.x1...    │
└──────────────┘                       └──────────────┘                     └──────────────┘
```

---

## Method 1: Direct Game Server Login (Recommended for Scripts)

The game server has its own `auth/login` endpoint that accepts username + password directly.

### Endpoint

```
POST /api/v1/auth/login
```

### Request

```json
{
    "name": "YourUsername",
    "password": "YourPassword",
    "w": "1920:1080",
    "mobileOptimizations": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | String | Your in-game username |
| `password` | String | Your account password |
| `w` | String | Screen resolution (`width:height`) — can be any value |
| `mobileOptimizations` | Boolean | Enable mobile layout optimizations |
| `captcha` | String | reCAPTCHA token (may be required after multiple failed attempts) |

### Response — Success (Step 1)

```
HTTP 200
```

```json
{
    "redirectTo": "/api/v1/auth?code=taTZ3rEOOKULafqQXu7A2l3ECLw...&response_type=redirect",
    "code": "taTZ3rEOOKULafqQXu7A2l3ECLw..."
}
```

> **⚠️ Two-step flow!** The login does NOT directly set the JWT cookie. It returns an **auth code** that must be exchanged.

### Step 2: Exchange Code for JWT

```
GET /api/v1/auth?code={code}&response_type=redirect
```

Follow the `redirectTo` URL from Step 1. This sets the JWT cookie and redirects to `/dorf1.php`:

```
HTTP 302
Set-Cookie: JWT=eyJ0eXAi...; Path=/; HttpOnly; Secure; SameSite=Lax
Location: /dorf1.php
```

The `JWT` cookie is now set as **HttpOnly** (not accessible via JavaScript).

### Response — Errors

| HTTP | Error Code | Message |
|------|-----------|---------|
| 400 | `login.pw_error2` | The password is wrong |
| 400 | `login.captchaRequired` | reCAPTCHA verification required |
| 400 | `login.banned` | Account is banned |

### Complete Python Login (Verified)

```python
import requests

BASE = "https://ts1.x1.europe.travian.com"

def login(username, password):
    """Authenticate and return a session with valid JWT cookie.
    
    Two-step flow:
    1. POST credentials → get auth code
    2. GET auth code exchange → get JWT cookie
    """
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "X-Version": "389"
    })
    
    # Step 1: Submit credentials
    r1 = session.post(f"{BASE}/api/v1/auth/login", json={
        "name": username,
        "password": password,
        "w": "1920:1080",
        "mobileOptimizations": False
    })
    
    if r1.status_code != 200:
        error = r1.json()
        raise Exception(f"Login failed: {error.get('message', 'Unknown error')}")
    
    data = r1.json()
    redirect_url = data.get("redirectTo")
    if not redirect_url:
        raise Exception("No redirect URL in login response")
    
    # Step 2: Exchange auth code for JWT
    r2 = session.get(f"{BASE}{redirect_url}", allow_redirects=False)
    
    jwt = session.cookies.get("JWT")
    if not jwt:
        raise Exception("No JWT cookie received after code exchange")
    
    print(f"Logged in successfully.")
    return session

# Usage
session = login("your_email@example.com", "your_password")

# Now use the session for all API calls
r = session.post(f"{BASE}/api/v1/graphql", json={
    "query": "{ ownPlayer { name village { id name x y } } }"
})
print(r.json())
```

### reCAPTCHA Handling

The login page uses Google reCAPTCHA v2:
```
Site Key: 6LfNjW0sAAAAAIZWD2v1AzTyFPrR3fXWysghexK8
```

reCAPTCHA is **not always required** — the server only demands it after suspicious activity (multiple failed logins, bot detection). When required:

1. Solve the reCAPTCHA (using a solving service or manual intervention)
2. Pass the token in the `captcha` field

For scripts that login infrequently (once per session), reCAPTCHA is typically not triggered.

---

## Method 2: Reuse Existing JWT Cookie

If you have a valid JWT (e.g., from your browser), you can use it directly:

```python
import requests

BASE = "https://ts1.x1.europe.travian.com"
session = requests.Session()
session.cookies.set("JWT", "eyJ0eXAiOiJKV1Qi...")
session.headers.update({
    "Content-Type": "application/json",
    "X-Version": "389"
})

# Test if JWT is valid
r = session.post(f"{BASE}/api/v1/graphql", json={
    "query": "{ ownPlayer { name } }"
})

if "redirectTo" in r.text or "Unauthorized" in r.text:
    print("JWT expired — need to re-login")
else:
    print(f"JWT valid: {r.json()}")
```

### Getting JWT from Browser

1. Open Chrome DevTools (F12)
2. Go to **Application** → **Cookies** → `https://ts1.x1.europe.travian.com`
3. Copy the `JWT` cookie value

> **⚠️ JWT expiry:** Tokens expire every ~2-5 hours. The exact lifetime varies. When expired, API calls return `{"redirectTo": "https://lobby.legends.travian.com/", "message": "Unauthorized"}`.

---

## Method 3: Lobby Flow (Browser-Based)

The normal browser login flow goes through the Travian lobby:

### Flow

1. User visits `https://www.travian.com/international#loginLobby`
2. Lobby SPA loads at `https://www.travian.com/international`
3. User authenticates (email + password, or Google OAuth)
4. Lobby redirects to game server with authentication token
5. Game server sets JWT cookie

### Lobby URLs

| URL | Purpose |
|-----|---------|
| `https://www.travian.com/international` | Main lobby SPA |
| `https://www.travian.com/international#loginLobby` | Login page |
| `https://www.travian.com/international#registration` | Registration |
| `https://www.travian.com/international#passwordRecovery` | Password recovery |
| `https://lobby.legends.travian.com/` | Lobby API (redirects to travian.com if not authenticated) |

### Lobby Authentication

The lobby uses its own session cookies. After authenticating:
- `GET https://lobby.legends.travian.com/api/gameworlds` → lists available servers
- `GET https://lobby.legends.travian.com/api/user/info` → user profile

Without lobby auth, these redirect to `https://www.travian.com/#loginLobby`.

> **For scripts:** The lobby flow is complex (SPA + reCAPTCHA + OAuth). Use **Method 1** (direct game server login) instead — it's a single POST request.

---

## JWT Token Structure

### Header

```json
{
    "typ": "JWT",
    "alg": "RS256"
}
```

### Payload

```json
{
    "sub": "R2LpSiv6DjAQGO8evagtxfE0rC126y93",
    "aud": "7d945800-1191-11f1-6501-01000000005e4c",
    "exp": 1774352395,
    "properties": {
        "hash": "c4e5c4e5c4e5cHhMV1t5XiupuS5a",
        "mobileOptimizations": true,
        "lobby": true,
        "did": 20030,
        "language": "en-US",
        "villagePerspective": "perspectiveBuildings"
    }
}
```

| Field | Description |
|-------|-------------|
| `sub` | User identifier (opaque string) |
| `aud` | Game server audience ID |
| `exp` | Expiration timestamp (Unix seconds) |
| `properties.hash` | Session hash |
| `properties.did` | Default village ID |
| `properties.language` | UI language |
| `properties.villagePerspective` | Default view (`perspectiveBuildings` or `perspectiveResources`) |

### Checking Expiry

```python
import json, base64, time

def check_jwt_expiry(jwt_token):
    """Check if JWT is expired"""
    payload = jwt_token.split('.')[1]
    # Add padding
    payload += '=' * (4 - len(payload) % 4)
    data = json.loads(base64.b64decode(payload))
    
    exp = data.get('exp', 0)
    now = int(time.time())
    remaining = exp - now
    
    if remaining <= 0:
        print(f"JWT EXPIRED {abs(remaining)} seconds ago")
    else:
        print(f"JWT valid for {remaining} seconds ({remaining // 60} minutes)")
    
    return remaining > 0
```

---

## Session Management

### Detecting Expired Sessions

When JWT expires, the server responds differently depending on the request type:

| Request Type | Expired JWT Response |
|-------------|---------------------|
| Page request (GET) | HTTP 302 redirect to `/` |
| REST API (POST) | `{"redirectTo": "https://lobby.legends.travian.com/", "message": "Unauthorized"}` |
| GraphQL | Same as REST |

### Auto-Refresh Pattern

```python
import requests
import time

class TravianSession:
    def __init__(self, base_url, username, password):
        self.base = base_url
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-Version": "389"
        })
        self._login()
    
    def _login(self):
        """Authenticate and get fresh JWT (two-step)"""
        r1 = self.session.post(f"{self.base}/api/v1/auth/login", json={
            "name": self.username,
            "password": self.password,
            "w": "1920:1080",
            "mobileOptimizations": False
        })
        if r1.status_code != 200:
            raise Exception(f"Login failed: {r1.json().get('message')}")
        
        # Step 2: Exchange code for JWT
        data = r1.json()
        self.session.get(f"{self.base}{data['redirectTo']}", allow_redirects=False)
        
        if not self.session.cookies.get("JWT"):
            raise Exception("No JWT cookie after code exchange")
        print(f"[{time.strftime('%H:%M:%S')}] Logged in")
    
    def _is_expired(self, response):
        """Check if response indicates expired session"""
        try:
            data = response.json()
            return "redirectTo" in data or "Unauthorized" in data.get("message", "")
        except:
            return response.status_code == 302
    
    def api(self, endpoint, data=None, method="POST"):
        """Make an API call with auto-refresh on expiry"""
        r = self.session.post(f"{self.base}/api/v1/{endpoint}", json=data or {})
        
        if self._is_expired(r):
            print("Session expired, re-authenticating...")
            self._login()
            r = self.session.post(f"{self.base}/api/v1/{endpoint}", json=data or {})
        
        return r.json()
    
    def graphql(self, query, variables=None):
        """Execute a GraphQL query with auto-refresh"""
        return self.api("graphql", {"query": query, "variables": variables or {}})
    
    def get_page(self, path):
        """Fetch a page with auto-refresh"""
        r = self.session.get(f"{self.base}{path}")
        if r.url.endswith('/') and path != '/':
            # Redirected to login page
            self._login()
            r = self.session.get(f"{self.base}{path}")
        return r.text

# Usage
ts = TravianSession(
    "https://ts1.x1.europe.travian.com",
    "Chieftain",
    "your_password"
)

# These auto-refresh on expiry
player = ts.graphql("{ ownPlayer { name village { id name } } }")
resources_html = ts.get_page("/dorf1.php")
```

---

## Security Notes

- **JWT is HttpOnly** — cannot be stolen via XSS
- **JWT uses RS256** — signed with server's RSA private key, cannot be forged
- **reCAPTCHA** — protects against brute force (triggered after suspicious patterns)
- **No refresh tokens** — when JWT expires, full re-authentication required
- **CSRF checksums** — building/troop actions require page-specific checksums (see `docs/16-buildings-resources.md`, `docs/13-troop-sending.md`)

### Rate Limiting

- The game server handles 200+ concurrent requests without rate limiting (verified for report fetching)
- Login endpoint may rate-limit after multiple failed attempts (reCAPTCHA required)
- Space login attempts reasonably (don't retry in a tight loop)

---

## Required Headers Reference

| Header | Value | Required For |
|--------|-------|-------------|
| `Cookie: JWT=...` | JWT token | All requests (set automatically by login) |
| `Content-Type` | `application/json; charset=UTF-8` | All POST requests |
| `X-Version` | `389` (current gpack version) | All API requests |

> **Note:** The `X-Version` value may change with game updates. Check the current version from any page's script tags: `<script src=".../gpack/{VERSION}/...">`

---

## Quick Start — Minimal Working Script

```python
import requests

BASE = "https://ts1.x1.europe.travian.com"

# Login (two-step)
s = requests.Session()
s.headers.update({"Content-Type": "application/json", "X-Version": "389"})
r = s.post(f"{BASE}/api/v1/auth/login", json={
    "name": "YOUR_EMAIL",
    "password": "YOUR_PASSWORD",
    "w": "1920:1080",
    "mobileOptimizations": False
})
s.get(f"{BASE}{r.json()['redirectTo']}", allow_redirects=False)  # Exchange code for JWT

# You're in. Do anything:
BASE = "https://ts1.x1.europe.travian.com"

# Get player info
player = s.post(f"{BASE}/api/v1/graphql", json={
    "query": "{ ownPlayer { name village { id name x y population } villageList { ... on VillageListVillage { id name x y } } } }"
}).json()
print(player)

# Get resources (parse from HTML)
import re
html = s.get(f"{BASE}/dorf1.php").text
resources = re.search(r'var resources = (\{.*?\});', html, re.DOTALL)
print(resources.group(1) if resources else "Not found")
```
