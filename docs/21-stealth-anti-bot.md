# Stealth & Anti-Bot System

The stealth system makes the CLI's network traffic and behavior look like a real
player using a browser. It operates across seven layers -- from TLS fingerprinting
to daily activity scheduling -- and is enabled by default on every command.

## Quick Start

```bash
# Stealth is ON by default -- just use the CLI normally
travian building list
travian farm send 123 --yes

# Disable stealth for speed (testing, development)
travian --no-stealth building list

# Tune stealth parameters
travian --noise-rate 0.3 --max-hours 8 farm send 123 --yes
```

## How It Works

When stealth is enabled, every HTTP request passes through this pipeline:

```
Your command
  |
  +-- 1. Request Throttler    -- enforces minimum gap between requests
  +-- 2. Browser Headers      -- adds Chrome-realistic headers + Referer chain
  +-- 3. Chrome TLS           -- curl_cffi impersonates Chrome TLS fingerprint
  +-- 4. Bot Detection Check  -- inspects responses for captcha/ban indicators
  +-- 5. Cookie Persistence   -- saves/loads cookies across CLI invocations
  |
  +-- (for automation loops)
       +-- 6. Human Timing    -- heavy-tailed delays between actions
       +-- 7. Noise Injection -- random idle browsing between operations
       +-- 8. Activity Limits -- daily hour caps + forced breaks
```

---

## Configuration

All settings are configurable via environment variables (`.env` file) or CLI flags.
Env vars use the `TRAVIAN_` prefix.

### .env File Settings

```bash
# Master switch (default: true)
TRAVIAN_STEALTH=true

# Delay speed multiplier: 0.5 = 2x faster, 1.0 = normal, 2.0 = 2x slower
TRAVIAN_STEALTH_SPEED=1.0

# Minimum/maximum seconds between any two HTTP requests
TRAVIAN_STEALTH_MIN_GAP=1.5
TRAVIAN_STEALTH_MAX_GAP=3.0

# Simulate page navigation before actions (visit dorf1/dorf2 before upgrading)
TRAVIAN_STEALTH_NAVIGATE=true

# Burst protection: max requests per 60-second window
TRAVIAN_STEALTH_BURST_MAX=20

# Cooldown seconds when burst limit is hit
TRAVIAN_STEALTH_BURST_COOLDOWN=15.0

# Probability of injecting a random browsing action between operations (0.0-1.0)
TRAVIAN_STEALTH_NOISE_RATE=0.15

# Maximum active hours per calendar day (resets at midnight)
TRAVIAN_STEALTH_MAX_DAILY_HOURS=10.0

# Maximum continuous hours before a forced break
TRAVIAN_STEALTH_MAX_CONTINUOUS_HOURS=4.0

# Minimum break duration in minutes
TRAVIAN_STEALTH_MIN_BREAK_MINUTES=30.0

# Delay variance factor: 0 = tight/predictable, 1 = very spread out
TRAVIAN_STEALTH_TIMING_VARIANCE=0.4
```

### CLI Flags

These override `.env` values for the current session only:

```bash
# Disable all stealth behavior
travian --no-stealth <command>

# Adjust noise injection rate (default: 0.15 = 15%)
travian --noise-rate 0.25 <command>

# Limit daily active hours (default: 10)
travian --max-hours 6 <command>
```

---

## Layer-by-Layer Reference

### Layer 1: TLS Fingerprinting (curl_cffi)

**Problem:** Python's default TLS libraries produce a JA3 fingerprint that
identifies the connection as Python/httpx, not a real browser.

**Solution:** The HTTP client uses `curl_cffi` with `impersonate="chrome"` to
produce a Chrome-identical TLS ClientHello, HTTP/2 SETTINGS frames, and cipher
suite ordering.

```
# Dependency in pyproject.toml
curl_cffi>=0.7.0
```

If `curl_cffi` is not installed, the client falls back to `httpx` with a
warning. This fallback is detectable -- keep `curl_cffi` installed.

**Files:** `clients/http_client.py`

---

### Layer 2: Browser Headers (stealth/headers.py)

Every request gets a complete set of Chrome-realistic headers, automatically
selected by request type:

| Request Type | Key Headers |
|---|---|
| **Page load** (GET .php) | `Accept: text/html`, `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, `Upgrade-Insecure-Requests: 1` |
| **API / GraphQL** (POST JSON) | `Accept: application/json`, `Sec-Fetch-Mode: cors`, `Sec-Fetch-Dest: empty`, `Origin` |
| **Form submit** (POST form) | `Content-Type: application/x-www-form-urlencoded`, `Sec-Fetch-Mode: navigate` |
| **XHR / AJAX** | `X-Requested-With: XMLHttpRequest`, `Sec-Fetch-Mode: cors` |

All requests include `Accept-Language`, `Accept-Encoding`, and the `X-Version`
game client header.

**Referer tracking:** The last visited `.php` page is tracked and sent as the
`Referer` header on subsequent requests, simulating natural browsing flow.

**Firefox awareness:** Sec-Fetch-* and sec-ch-ua headers are omitted when the
selected User-Agent is Firefox, matching real Firefox behavior.

---

### Layer 3: User-Agent Rotation (stealth/user_agents.py)

A pool of 11 real browser User-Agent strings:

- Chrome 132-135 on Windows (4 variants)
- Chrome 134-135 on macOS (2 variants)
- Firefox 135-137 on Windows (3 variants)
- Firefox 137 on macOS (1 variant)
- Edge 133-134 on Windows (2 variants)

One UA is randomly selected at session start and used for the entire session.
This matches real browser behavior -- browsers don't change their UA mid-session.

---

### Layer 4: Request Throttling (stealth/throttler.py)

Prevents machine-gun request patterns:

- **Minimum gap:** 1.5-3.0 seconds between any two requests (randomized)
- **Burst detection:** If 20+ requests happen within 60 seconds, adds a 15-23
  second cooldown
- **Adaptive penalties:** When bot detection is suspected (captcha, 429 status),
  adds 60-300 second cooldowns automatically

The throttler is global -- all HTTP methods go through it.

---

### Layer 5: Human-Like Timing (stealth/timing.py, stealth/human_delay.py)

Two complementary timing engines replace every fixed `sleep()` in the codebase:

#### HumanTiming -- Heavy-tailed distributions

Used for automation loop intervals. Produces the "bursty" pattern that matches
real human behavior: many short delays with occasional long pauses.

```
HumanTiming.delay(30)         ->  typical: 15s, occasional: 45s, rare: 150s
HumanTiming.reaction_time()   ->  0.15-3.0s (ex-Gaussian distribution)
HumanTiming.micro_jitter(3.0) ->  2.55-3.45s (+-15%)
```

**Distribution:** 70% short delays, 25% medium, 5% long pauses.

#### HumanDelay -- Action-specific timing

Used for in-game action delays. Each action type has a tuned timing profile
using triangular distributions (min, mode, max):

| Action | Typical Delay | Use Case |
|---|---|---|
| PAGE_LOAD | 1.5-8.0s (mode 3s) | Reading a loaded page |
| CLICK | 0.3-2.5s (mode 0.8s) | Clicking a button/link |
| DECISION | 2-15s (mode 5s) | Reviewing upgrade costs |
| PRE_UPGRADE | 1.5-8.0s (mode 3s) | Before starting an upgrade |
| BETWEEN_RAIDS | 1.5-6.0s (mode 3s) | Between farm list sends |
| BETWEEN_SCOUTS | 1.5-7.0s (mode 3.5s) | Between scout sends |
| VIDEO_TICK | 2.5-3.8s (mode 3s) | ATG ad tick (tight range) |

**Bonus behaviors:**
- **5% micro-pause chance:** 5-20 second "distraction" (phone rang, looked away)
- **Periodic think pause:** Every 15-25 actions, 10-30 second idle
- **Speed factor:** Multiply all delays by `TRAVIAN_STEALTH_SPEED`

---

### Layer 6: Page Navigation Simulation (stealth/navigator.py)

Before performing an action, the navigator loads the pages a real player would
visit. This creates realistic traffic patterns instead of direct API-only access.

**Post-login warm-up:**
```
Login -> dorf1.php (resource overview) -> dorf2.php (village center)
  -> [20% chance: statistics page] -> [10% chance: profile page]
  -> back to dorf1.php
```

**Pre-upgrade flow:**
```
dorf1.php or dorf2.php -> build.php?id=X -> (read costs) -> upgrade
```

**Idle browsing** (between automation actions):
Random visits to dorf1, dorf2, statistics, or player profile pages.

---

### Layer 7: Noise Injection (stealth/noise.py)

Automation loops are detected by the *absence* of normal browsing. The noise
injector adds random non-functional browsing between bot operations:

- **Default rate:** 15% chance per action cycle
- Between each farm send, build check, or scout operation, there is a chance
  the bot will visit a random page (map, messages, statistics, profile)
- **Session breaks:** Simulates AFK periods (2-10 minutes of silence, then a
  mini warm-up when resuming)

The noise injector is automatically called between iterations in:
- Build queue polling (build_queue_service.py)
- Farm list batch sends (farm_list_service.py)
- Scout send sequences (auto_scout_service.py)

---

### Layer 8: Activity Scheduling (stealth/scheduler.py)

Prevents 24/7 activity patterns that trigger Multihunter investigations:

| Limit | Default | Purpose |
|---|---|---|
| **Daily hours** | 10h | Max active hours per calendar day |
| **Continuous session** | 4h | Max hours before forced break |
| **Minimum break** | 30 min | Shortest allowed break |
| **Night break** | 6-9h | Automatic long break 23:00-06:00 |
| **Long break** | 2-6h | When 80%+ of daily budget used |

The scheduler is checked before each automation cycle in the build queue and
farm list services. When limits are hit, the bot takes a break automatically.

---

### Layer 9: Cookie Persistence (clients/http_client.py)

Cookies are saved to `.travian_cookies.json` and restored on the next CLI
invocation. This makes sequential commands look like the same browser session
resuming, rather than a fresh login each time.

```bash
travian auth login          # Cookies saved
travian building list       # Reuses saved cookies -- no re-login needed
travian farm send 123 --yes # Same session continues
```

---

### Layer 10: Bot Detection Response (clients/http_client.py)

The HTTP client inspects every response for signs that the server has detected
bot activity:

| Pattern | Severity | Response |
|---|---|---|
| recaptcha, bot-detection | HIGH | 120s cooldown |
| suspicious activity, automated access | HIGH | 120s cooldown |
| your ip has been, access denied | HIGH | 120s cooldown |
| Captcha form/div/iframe | HIGH | 120s cooldown |
| too many requests (short page) | MEDIUM | 60s cooldown |
| your account has been banned | CRITICAL | 300s cooldown |
| HTTP 429 | HIGH | 120s cooldown |

When detection is suspected, the throttler automatically adds a penalty delay
before the next request.

---

## Speed Profiles

Adjust `TRAVIAN_STEALTH_SPEED` for different risk tolerances:

| Profile | Speed | Min Gap | Noise Rate | Use Case |
|---|---|---|---|---|
| **Aggressive** | 0.5 | 1.5s | 0.05 | Short sessions, active monitoring |
| **Normal** | 1.0 | 1.5s | 0.15 | Default, balanced |
| **Cautious** | 1.5 | 2.0s | 0.25 | Long unattended sessions |
| **Paranoid** | 2.0 | 3.0s | 0.40 | New accounts, suspicious servers |

Example cautious `.env` setup:

```bash
TRAVIAN_STEALTH_SPEED=1.5
TRAVIAN_STEALTH_MIN_GAP=2.0
TRAVIAN_STEALTH_MAX_GAP=4.0
TRAVIAN_STEALTH_NOISE_RATE=0.25
TRAVIAN_STEALTH_MAX_DAILY_HOURS=8.0
TRAVIAN_STEALTH_MAX_CONTINUOUS_HOURS=3.0
```

---

## Disabling Stealth

For development, testing, or when you need raw speed:

```bash
# Via CLI flag (single command)
travian --no-stealth building list

# Via .env (all commands)
TRAVIAN_STEALTH=false
```

When disabled:
- No request throttling or delays
- Minimal headers (just User-Agent + X-Version)
- No page navigation simulation
- No noise injection or activity limits
- No cookie persistence
- Uses httpx directly (no curl_cffi TLS impersonation)

---

## Architecture

```
src/travian_api/stealth/
  __init__.py          # Public API exports
  timing.py            # HumanTiming: heavy-tailed delay engine
  human_delay.py       # HumanDelay: action-specific timing profiles
  headers.py           # BrowserHeaders: Chrome-accurate header sets
  user_agents.py       # UserAgentRotator: 11 real browser UA strings
  throttler.py         # RequestThrottler: rate limiting + burst detection
  navigator.py         # PageNavigator: page simulation + warm-up
  noise.py             # NoiseInjector: random browsing between actions
  scheduler.py         # ActivityScheduler: daily/session hour limits
  session_manager.py   # SessionManager: session lifetime + break timing
```

**Dependency rule:** The stealth module imports only from stdlib. Services
import from stealth, never the reverse. No circular dependencies.

---

## Troubleshooting

**"curl_cffi not found" warning:**
Install with `pip install curl_cffi>=0.7.0`. Without it, TLS fingerprinting
falls back to Python's default (detectable).

**Commands are very slow:**
Lower the speed factor: `TRAVIAN_STEALTH_SPEED=0.5`. Or disable stealth for
one-off reads: `travian --no-stealth building list`.

**"BOT DETECTION" in logs:**
The server flagged something. The throttler will automatically add a cooldown.
If it keeps happening, increase `TRAVIAN_STEALTH_SPEED` and
`TRAVIAN_STEALTH_MIN_GAP`.

**Bot stopped for hours (break):**
The activity scheduler enforced a break. Reduce limits if needed:
`TRAVIAN_STEALTH_MAX_DAILY_HOURS=12` or `TRAVIAN_STEALTH_MAX_CONTINUOUS_HOURS=6`.

**Session expired frequently:**
Cookie persistence may not be saving. Check that `.travian_cookies.json` exists
in the working directory and is writable.
