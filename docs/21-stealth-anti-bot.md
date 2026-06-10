# Stealth & Anti-Bot System

The stealth system makes the CLI's and web UI's network traffic and behavior
look like a real player using a browser. It operates across ten layers -- from
TLS fingerprinting to per-account build-action staggering -- and is enabled by
default on every command.

This document describes **what** each layer does. For the **why** (trade-off
analysis behind each design choice), see `docs/23-stealth-decisions.md`. For
the **resumable cross-device session** layer that interacts with activity
scheduling, see `docs/22-resumable-operations.md`.

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

**Solution:** The HTTP client uses `curl_cffi` with a persona-matched
`impersonate=` target (e.g. `chrome131`, `chrome133a`, `chrome136`) so the
TLS ClientHello, HTTP/2 SETTINGS frames, and cipher suite ordering match
the UA the persona claims.

```
# Hard dependency in pyproject.toml
curl_cffi>=0.7.0
```

**Fail closed:** if `stealth=true` and `curl_cffi` is not importable, the
HttpClient now **raises RuntimeError on construction** rather than degrading
silently to `httpx`. Sending Chrome-shaped headers over a Python TLS handshake
is a stronger tell than running stealth-off, so we refuse the misconfiguration.

**Files:** `clients/http_client.py`, `stealth/persona.py`

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

**`Accept-Encoding`:** Chromium personas advertise `gzip, deflate, br, zstd`
to match Chrome 124+ stable; non-Chromium personas advertise `gzip, deflate, br`.
The encoding string is derived from the persona's browser type so it can't
drift from the UA.

**`request_type="xhr"`:** services calling endpoints that the Travian frontend
JavaScript invokes via `fetch`/`XMLHttpRequest` — `/api/v1/map/position`,
`/api/v1/map/tile-details`, `/api/v1/farm-list/send`, `/api/v1/farm-list/slot`
(POST and DELETE) — pass `request_type="xhr"` to `post_json`/`delete_json`.
This selects `BrowserHeaders.for_xhr()` (XHR shape with `X-Requested-With` +
`Sec-Fetch-Mode: cors`) and merges in `Content-Type: application/json` for
the body. Generic JSON-client traffic still uses `for_json_post()`.

**PRG-redirected GET:** when `post_form` follows a `302` to a GET, the GET now
uses fresh page-load headers (not the form POST's `Content-Type`/`Origin`/etc).
Real browsers issue redirected GETs as fresh document navigations.

**Referer tracking:** The last visited `.php` page is tracked and sent as the
`Referer` header on subsequent requests, simulating natural browsing flow. API
and XHR responses do not advance page context (only document-like requests do).

**Firefox awareness:** Sec-Fetch-* and sec-ch-ua headers are omitted when the
selected User-Agent is Firefox, matching real Firefox behavior.

---

### Layer 3: Persona — Coherent Identity (stealth/persona.py)

A `Persona` ties together User-Agent, `curl_cffi` impersonate target,
`sec-ch-ua` headers, platform, and `accept-language` so every layer of the
stealth stack presents the same browser identity.

Pool: Chrome on Windows with versions matching `curl_cffi`'s exact impersonate
support (Chrome 131 / 133 / 136 currently). UA, `sec-ch-ua`, and TLS
fingerprint always match — no version skew.

**Pinned to cookie lifetime:** persona is persisted to
`.travian_persona.json` alongside cookies, with a 365-day TTL (was 7d). A
real browser profile keeps its UA/TLS for the install lifetime; rotating
mid-cookie-jar is itself a tell — same auth cookie keeps showing up with
different Chrome versions and different TLS fingerprints.

**Server-URL scoped:** the persisted persona file records the server URL
it was created against. If the current `settings.base_url` differs (e.g.
switching from `.de` to `.com`, or between game worlds), the persona
rotates automatically since a player who switched servers would
realistically be on a different machine or browser profile.

**Explicit identity reset:** delete `.travian_persona.json` (and the
matching cookie file) to force a fresh persona.

---

### Layer 4: Request Throttling (stealth/throttler.py)

Prevents machine-gun request patterns:

- **Minimum gap:** drawn from a right-skewed (shifted log-normal)
  distribution whose body sits in the configured `[min_gap, max_gap]` band
  (default 1.5-3.0s) with an occasional longer tail, soft-capped at 3x
  `max_gap`. This replaces a flat `uniform(min, max)` draw, whose flat gap
  histogram a KS test against real human traffic flags as automation. The
  distribution's shape (median fraction + sigma) is bound to the persona via
  `seed_gap_shape()`, so one account keeps a stable timing fingerprint across
  restarts while differing from other accounts.
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
`(min, mode, max)`. Delays are drawn from a **shifted log-normal** whose shape
is auto-derived from that profile — `min` is a soft floor, the peak sits
exactly at `mode`, and `max` is the ~95th percentile (not a hard cutoff; a
soft right tail continues past it, capped at 4x the span). A bounded
*triangular* was used before, but its hard min/max cutoffs and linear ramps
are rejectable by a KS / Anderson-Darling test against human action-time data.
The tuned central tendency (mode) is preserved exactly, so this is a shape
fix, not a re-tune. A per-account `seed_delays()` multiplier (`[0.92, 1.12]`
on sigma, from the salt-bearing identity) gives each account a distinct spread
without moving the mode. `VIDEO_TICK` stays triangular — its ~3s cadence is a
functional ATG-signature requirement, not a stealth knob.

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

#### SessionTempo -- shared drifting tempo (stealth/session_tempo.py)

The action-delay and request-gap samplers were independent within their class
— i.e. ~iid. A real player's pace drifts over a session (warming up, tiring,
getting distracted), so consecutive delays and gaps are positively
**autocorrelated**. A detector separates iid synthetic timing from a human
session tempo with a Ljung-Box / runs / lag-1 autocorrelation test (or an HMM
likelihood-ratio over a latent tempo).

`SessionTempo` is one bounded AR(1) random walk in log-space, **shared** by
both `HumanDelay` and `RequestThrottler` (one instance per account, seeded
from the salt-bearing identity). Each multiplies its sampled delay by the same
current tempo, so consecutive delays and gaps drift together — positive
short-lag correlation, without flattening either marginal (the walk is
centered at multiplier 1.0). The latent is mapped to `[0.7, 1.5]` via a smooth
`tanh` squash (no hard-clamp boundary mass), and the walk advances at most once
per 30s of wall-clock so a `HumanDelay.wait()` and a `throttler.wait()` within
one action read the same tempo instead of double-stepping. `phi` (persistence)
and volatility are persona-stable, giving each account a steady-or-erratic
tempo personality. The throttler re-floors after scaling (the hard min-gap is
never violated) and `VIDEO_TICK` is excluded (functional ~3s cadence).

The same tempo also drives the **macro loop intervals** via
`HttpClient.tempo_scale(seconds)`, which scales the *mean* fed to
`HumanTiming.delay(...)` (preserving its heavy-tailed shape and clamps): the
build-queue slot-free reaction window and "come back later" polling sleep, and
the inter-scout delay. So short request gaps, per-action delays, and the
minutes-scale loop cadence all drift with one coherent session pace — closing
the cross-timescale gap an HMM (short gaps drift, loop cadence doesn't) or a
periodogram / Lomb-Scargle (fixed polling cadence) would otherwise find.
`tempo_scale` is a no-op when stealth is off and is applied ONLY to
human-controlled waits — never to server-deadline countdowns, retry backoffs,
or the ATG/video tick cadence.

---

### Layer 6: Page Navigation Simulation (stealth/navigator.py)

Before performing an action, the navigator loads the pages a real player would
visit. This creates realistic traffic patterns instead of direct API-only access.

**Post-login warm-up (persona-stable first-order Markov walk):**
```
Login -> dorf1.php (always; prevents "login -> immediate API blast")
  -> Markov walk over {dorf1, dorf2, statistiken, spieler, karte}:
     each step draws the next page (or STOP) from this account's stable
     transition matrix; bounded to a per-account length cap
```
The warm-up used to be a fixed skeleton (`dorf1 -> dorf2 -> [20%] -> [10%]
-> dorf1`) with the same transitions every session — trivially clustered by
an n-gram / Markov-transition chi-square or edit-distance test. An interim
version randomized the visited *set* per account (independent Bernoulli),
but the transition *structure* and route lengths were still shared across
accounts.

Now the walk is a **per-persona first-order Markov chain**. Each account
draws stable behavioral motifs from its (salt-bearing) identity:
- a wide per-page **bias** (`uniform(0.2, 2.5)`) — a coherent browsing
  personality (one account favors the map, another profiles), so the
  population is a broad mixture, not one shared transition profile;
- a small **self-loop** tendency (reloading the current page is possible —
  a hard-zero diagonal is itself a regularity humans lack);
- a **stop bias** and a per-account **length cap** (`randint(4, 7)`), so
  route lengths form a broad family rather than one shared distribution.

The base page affinity (overviews > profile/stats) is kept realistic on
purpose: a *human* population also favors overviews, so the aggregate is not
a bot discriminator — per-account spread is what defeats transition-count
chi-square / likelihood-ratio, route-length KS, and edit-distance clustering.
The per-call realization still varies (sampled from the global RNG); only
the matrix is persona-stable. All pages are coherent navigation targets, so
the Referer chain stays truthful. (Deferred: same Markov treatment for
mid-session `idle_browse` / noise navigation.)

**Behavioral salt:** the persona carries a persisted per-account random
`salt` (`secrets.token_hex(8)`, never sent to the server). It is folded into
the identity that seeds both the request-gap shape and the warm-up routes,
because UA/language/server alone are low-entropy on a single world (a few
UAs, one server-derived language) — without the salt, accounts would collide
into a handful of latent behavioral buckets a detector could cluster.

**Pre-upgrade flow:**
```
dorf1.php or dorf2.php -> build.php?id=X -> (read costs) -> upgrade
```

**Pre-construct flow:**
```
dorf2.php -> build.php?id=X (empty slot picker) -> (read options) -> construct
```
Mirrors the upgrade flow for new building construction. Fires AFTER the
queue/can-build guards in `_construct_building_unlocked` so we don't
waste page loads on requests that will be rejected.

**Map navigation (`navigate_to_map`):**
```
karte.php (with optional newdid)
```
Called before any `tile-details` or `map/position` XHR. Without this,
those XHRs carry whatever Referer was set last (e.g. `/profile.php`),
which is impossible from a real browser since tile popups are opened
from the map page.

**Farm-list navigation (`navigate_to_farm_list`):**
```
dorf2.php -> rally point -> build.php?gid=16&tt=99
```
Establishes Referer/Origin context before any farm-list mutation API
(`/api/v1/farm-list/slot` add/remove, `/api/v1/farm-list/send`). Run
once per village handoff.

**Idle browsing** (between automation actions):
Random visits to dorf1, dorf2, statistics, or player profile pages.

---

### Layer 7: Noise Injection (stealth/noise.py)

Automation loops are detected by the *absence* of normal browsing. The noise
injector adds random non-functional browsing between bot operations:

- **Default rate:** 15% chance per action cycle
- Between each farm send, build check, or scout operation, there is a chance
  the bot will visit a page. The page is **persona-weighted**, not a flat
  `random.choice`: `idle_browse` draws from the same per-account page
  personality (`_route_page_bias` × base affinity) that `warm_up` uses, so each
  account's idle-page distribution is distinct and internally consistent. A
  flat pick is identical across the fleet and clusterable by a visit-frequency
  chi-square / G-test; the realistic base affinity (overviews > profile/stats)
  keeps the aggregate human-like.
- **Session breaks:** Simulates AFK periods (2-10 minutes of silence, then a
  mini warm-up when resuming)

> Known limitation (deferred): the per-cycle noise *trigger* is still an
> independent Bernoulli draw, so inter-noise gaps are geometric (testable by
> KS/chi-square vs geometric, runs, Ljung-Box on the event indicator). A
> persona-stable renewal/hazard process with a refractory period is the next
> step.

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

The daily-hours and continuous-session limits above are **hard safety
ceilings** that are never exceeded. The scheduler does not stop at exactly
that number, though: it gates on a jittered *effective* cap drawn at-or-below
the ceiling (continuous: `0.80–1.0x`, daily: `0.85–1.0x`, triangular so the
density tapers to zero at both band edges). Without this, every limit-hitting
session is exactly the configured length and every capped day exactly the
configured total — a sharp spike in the session-length / daily-total histogram
that a KDE or chi-square density test flags. The continuous cap re-jitters per
session (after each break and on idle auto-reset); the daily cap re-jitters
once per local day (resampling it every short session would let it drift
upward as an order statistic). Both are persisted (`.scheduler_state.json`)
for same-day restart consistency, validated on load (non-finite or out-of-band
values are rejected, never disabling the gate).

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

The HTTP client inspects every response for signs that the server has
detected bot activity. Detection is tiered: high-confidence block-page
signals on a short error response trigger the **captcha guard** (a hard
stop on all outbound traffic until the user resolves it); transient or
embedded-in-bundle signals trigger a **throttler penalty** (cooldown +
continue).

| Pattern | Context | Action |
|---|---|---|
| structural `g-recaptcha` HTML | always | Hard stop (captcha guard) |
| `recaptcha` text + short page (<5KB) or 403/429/503 | | Soft penalty 120s |
| `bot-detection` / `suspicious activity` / `automated access` / `your ip has been` / `access denied` | short page (<5KB) AND status in (403, 503) | Hard stop (captcha guard) |
| Same patterns | short page OR error status (not the AND) | Soft penalty 90s |
| `<form\|div\|iframe ...captcha` HTML | always | Hard stop |
| `too many requests` text + short page | | Soft penalty 120s |
| `your account has been banned` | always | Hard stop |
| HTTP 429 | any body | Throttler penalty 120s |
| HTTP 403/503 + non-game body | short page | Soft penalty 90s |

The hard-stop path (captcha guard) blocks every subsequent outbound
request — including those from concurrent operations — until the user
acknowledges the captcha modal in the web UI (or calls
`/api/captcha/resolve` from the CLI). This prevents the "bot keeps
probing through a real block" pattern.

The soft-penalty path adds a one-off cooldown to the next request via
`throttler.add_penalty(seconds)` and continues normally.

---

### Layer 11: Per-Account Action Coordination (build_queue_service.py)

A single Travian account can have multiple build queues running (one
per village). Without coordination, two queues whose builds finish at
the same instant would fire upgrade POSTs on the same second — a
pattern impossible for a single human operator to produce.

`build_queue_service` keeps a module-level `_account_build_locks` dict
keyed by `id(http_client)` (which is per-account). Before every
`upgrade_building` / `construct_building` call:

1. Acquire the per-account `asyncio.Lock` (serializes build POSTs).
2. Check `_last_account_build_action_ts[account]` — if another queue
   acted within the last 60 seconds, sleep a heavy-tailed 10-90s
   stagger.
3. Update the timestamp BEFORE sleeping so concurrent waiters stack
   behind this stagger rather than stacking on top of it.

This adds latency only when multiple village queues collide; isolated
single-village runs see no overhead.

### Layer 12: Operation Identity & Activity Logging

The cross-device session-control feature (see
`docs/22-resumable-operations.md`) feeds the activity scheduler with
real elapsed time:

- `farm_list_service._send_farm_list_unlocked` measures
  `time.monotonic()` around the work in a `try/finally` and calls
  `activity_scheduler.log_activity(elapsed)` on every exit path.
- `build_queue_service.execute_plan_continuous` logs activity per
  cycle.
- Cross-device subscribe/unsubscribe events do NOT trigger fresh
  activity entries — only actual API work does.

This keeps the activity scheduler's daily/session caps honest even when
ops run unattended for hours.

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
- Per-account build-action stagger is a no-op
- Stealth floors on user-configurable intervals (e.g. farm-list 60s
  floor) drop to the legacy minimums

**Note:** when stealth is OFF, `request_type="xhr"` callers still get a
JSON `Content-Type` (the empty-string-safe guard in the http_client
ensures this), so feature code that assumes XHR shape doesn't break.

---

## Architecture

```
src/travian_api/stealth/
  __init__.py          # Public API exports
  timing.py            # HumanTiming: heavy-tailed delay engine
  human_delay.py       # HumanDelay: action-specific timing profiles
  headers.py           # BrowserHeaders: Chrome-accurate header sets
  persona.py           # Persona: coherent identity (UA + TLS + sec-ch-ua)
  user_agents.py       # UserAgentRotator: persona-driven UA selection
  throttler.py         # RequestThrottler: rate limiting + burst detection
  navigator.py         # PageNavigator: page simulation + warm-up + map/farm-list
  noise.py             # NoiseInjector: random browsing between actions
  scheduler.py         # ActivityScheduler: daily/session hour limits
  session_manager.py   # SessionManager: session lifetime + break timing
  captcha_guard.py     # CaptchaGuard: hard-stop gate on detection
```

Plus per-account coordination at the service layer:

```
src/travian_api/services/
  build_queue_service.py
    # Module-level: _account_build_locks, _last_account_build_action_ts
    # Helpers: _account_build_lock_for(), _stagger_account_build()
```

**Dependency rule:** The stealth module imports only from stdlib. Services
import from stealth, never the reverse. No circular dependencies.

---

## Troubleshooting

**RuntimeError: stealth mode requires curl_cffi:**
Install with `pip install curl_cffi>=0.7.0`. Stealth mode now refuses to
start without it (the previous fallback to `httpx` was a stronger tell
than running stealth-off). If you need to run without TLS impersonation,
explicitly disable stealth: `TRAVIAN_STEALTH=false` or `--no-stealth`.

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
