# Authentication

## Flow

1. User visits `https://www.travian.com/international` (Travian Lobby)
2. Authenticates via Google OAuth / email+password
3. Selects a game server (e.g., `ts1.x1.europe`)
4. Lobby sets a JWT cookie on the game server domain
5. All subsequent requests include this JWT cookie

## JWT Token

**Cookie name:** `JWT`
**Algorithm:** RS256

### Payload Structure
```json
{
  "sub": "6XS2L2gqX7lWTY1oliedg4vnqpnaB1YV",  // user ID (lobby)
  "aud": "7d945800-1191-11f1-6501-0100000005e4c",  // server audience ID
  "exp": 1773940034,  // expiration (Unix timestamp)
  "properties": {
    "hash": "c4e5c4e5c4e52koeABWrwnbWJrJb",  // session hash
    "mobileOptimizations": true,
    "lobby": true,
    "did": 69130,  // default village ID
    "language": "en-US",
    "villagePerspective": "perspectiveBuildings"  // or "perspectiveResources"
  }
}
```

### JWT Lifetime
- Tokens expire periodically (observed ~2-3 hour window)
- When expired, server redirects to `https://www.travian.com/international#loginLobby`
- No refresh token mechanism visible — requires re-authentication through lobby

## Other Cookies

| Cookie | Purpose |
|--------|---------|
| `JWT` | **Authentication** — the only required cookie |
| `session-cl*` | Gleap analytics session (not required for API) |
| `__cmpconsentx*` | Cookie consent (GDPR) |
| `__cmpcccx*` | Consent manager config |
| `active_rallypoint_sub_filters_*` | UI state persistence |

## Required Headers

All API calls require:
```
Content-Type: application/json; charset=UTF-8
X-Version: 389
```

The `X-Version` header must match the current game version (gpack version). Requests without it may fail or be rejected.

## Login API

### `POST /api/v1/auth/login`
Used by the lobby login form (not typically called directly).

### `POST /api/v1/auth/logout`
Invalidates the current session.
```javascript
Travian.api("auth/logout", {})
```

## Session Detection

The game detects stale sessions via:
- `Travian.Autoreload.autoreload()` — forces page reload
- Server returns `{reload: true}` in API responses when session needs refresh
- Server returns `{redirectTo: "/url"}` to force navigation
