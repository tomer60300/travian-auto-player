# Stealth vs Performance — Design Decisions

This document captures the hard choices made in the stealth stack: where
performance was traded for stealth, where it wasn't, and the reasoning
behind each call.

The stealth pipeline is described in `docs/21-stealth-anti-bot.md`. This
file is the "why".

## Guiding principles

1. **Don't pay for headers what you can pay for in clock time.** A
   request that looks wrong (mismatched header set, wrong Referer chain,
   missing `X-Requested-With`) is detectable on the first packet.
   Sending the right headers costs nothing and doesn't show up on a
   throughput benchmark.
2. **Heavy-tailed jitter > big mean delays.** Anti-bot systems flag
   uniform-random or perfectly periodic timing. A log-normal jitter
   added to existing intervals defeats the pattern at zero cost. A
   30-second flat sleep added to every action defeats the pattern at
   30s/action — an unacceptable trade.
3. **Pay perf only for glaring tells.** "Wakes up exactly 3s after the
   build timer hits zero, every single time" is a glaring tell. A
   30s-5min reaction window is worth the wall-clock cost. "Sends 3
   batches of farm-list slots in 1.2s" is fine.
4. **A failed request that the bot continues through is more visible
   than the bot that pauses.** Real players who see a soft block stop
   and look at the page. They don't fire 30 more requests in the next
   minute. Soft-failure handling now mirrors that.
5. **Identity stays still.** Real browser profiles don't rotate UA
   between sessions. Persona is pinned to cookie lifetime, not to a 7d
   timer.

## Capability and dependency map

| Capability | Hard dependency | Soft dependency | Stealth impact |
|---|---|---|---|
| TLS impersonation | `curl_cffi>=0.7.0` | — | **Critical**. Without it Travian sees `python-httpx/...` JA3 alongside Chrome UA. We now fail closed when stealth=on and curl_cffi is missing. |
| Browser headers | — | persona | High. Different Sec-Fetch-* and sec-ch-ua per request type. |
| Persona stability | `.travian_persona.json` | server URL | Medium. UA/TLS change mid-cookie-jar is suspicious. |
| Referer chain | navigator + last_page tracking | — | High. Tile XHRs with `/profile.php` Referer are impossible from a real browser. |
| Heavy-tailed timing | `stealth/timing.py` | `random` | High. Uniform/triangular timing is a stripe pattern. |
| Action-class delays | `stealth/human_delay.py` | `speed_factor` | Medium. Per-action timing profile (CLICK vs PAGE_LOAD vs DECISION). |
| Burst protection | throttler | captcha guard | Critical. Triggers on accidental concurrent requests. |
| Activity scheduling | `stealth/scheduler.py` | persona file dir | Medium. Daily/session caps, forced breaks. |
| Captcha guard | `stealth/captcha_guard.py` | — | Critical. Hard-stops all outbound traffic on detection. |
| Cross-device session | `operation_manager` + 24h TTL | localStorage | Stealth-neutral (it's a UX feature) but interacts with activity logging. |

## Layer-by-layer trade-off table

### TLS fingerprint

**Decision:** fail closed when `curl_cffi` is missing.

Before: a warning log + fallback to `httpx`. Travian saw Chrome-shaped
headers over a Python-shaped TLS handshake — a stronger tell than just
running with stealth off.

Cost: 0 (curl_cffi is already a hard dep in pyproject.toml).
Benefit: removes the worst possible tell.

### Persona TTL

**Decision:** 7 days → 365 days, plus server-URL scoping.

Before: persona rotated every 7 days. Same auth cookie kept showing up
with a different Chrome version, different sec-ch-ua, different TLS
fingerprint — a clear "automation refreshing identity" pattern. Real
browser profiles don't do that.

Cost: 0 wall-clock; the only scenario this hurts is detection bypass
when an account is flagged on a specific UA. We have explicit identity
reset (delete the persona file) for that case.
Benefit: removes a recurring weekly tell.

The server-URL scope means switching from `.de` to `.com` (different
`Accept-Language`, possibly different timezone implication) rotates the
persona automatically, since a real player who switched servers would be
on a different machine or at minimum a different browser profile.

### Header shape per endpoint

**Decision:** add `request_type="xhr"` to `post_json`/`delete_json` and
route map/farm-list/tile-detail traffic through it.

Before: every JSON POST got the generic JSON-client header set
(`Accept: application/json`, no `X-Requested-With`). Travian's frontend
JS calls these endpoints via fetch — they should carry
`X-Requested-With: XMLHttpRequest` and `Sec-Fetch-Mode: cors`. Generic
JSON shape was a fingerprint mismatch.

Cost: 0 (header strings are free).
Benefit: removes a structural tell on the most-frequent endpoints.

### `Accept-Encoding` zstd

**Decision:** Chromium personas advertise `gzip, deflate, br, zstd`.

Before: hardcoded `gzip, deflate, br` regardless of UA major version.
Modern Chrome (124+) sends zstd. Mismatch is a sub-tell that can be
checked against UA in modern detectors.

Cost: 0 (Travian doesn't actually send zstd back, but advertising it
still matters for fingerprinting).
Benefit: closes a small but free gap.

### PRG-redirected GET

**Decision:** the GET that follows a 302 PRG response now carries fresh
page-load headers, not the original form POST headers.

Before: the redirected GET reused the form POST headers including
`Content-Type: application/x-www-form-urlencoded`, `Origin`,
`Sec-Fetch-Dest: document`, etc. A real browser issues the redirected
GET as a fresh document navigation — different header set entirely.

Cost: 0.
Benefit: removes a per-action tell on every state-mutating endpoint
(every troop send, every build, every farm-list slot mutation produces
a 302 PRG).

### Retry backoff

**Decision:** `wait_random_exponential` instead of `wait_exponential`.

Before: `get_html` retried at 1s, 2s, 4s — perfect powers of two.
Browsers retry on network blips with timing driven by network events,
not power-of-two intervals.

Cost: 0 expected throughput change.
Benefit: removes a retry-stripe in request timing that's trivially
binable.

### Captcha guard escalation

**Decision:** short 403/503 pages with high-confidence block phrases
hard-fire the captcha guard; soft-penalty reserved for 429 and
embedded-in-large-bundle false positives.

Before: every match (recaptcha, bot-detection, access denied, etc.)
applied a 90-120s soft cooldown. A real block page was treated the same
as a transient rate limit. The bot kept probing through what was
effectively a hard block.

Cost: false positives now hard-stop the user; they have to dismiss the
captcha modal. Mitigated by structural-evidence checks (the guard fires
only when the page is short AND the status is 403/503).
Benefit: real blocks no longer get probed through.

### Activity-scheduler effective caps

**Decision:** `can_continue()` gates on a *jittered effective cap* drawn
at-or-below the configured `max_continuous_hours` / `max_daily_hours`,
instead of the exact configured value.

Before: the scheduler stopped at exactly `max_continuous_hours` (e.g. 6.0h)
and `max_daily_hours` (e.g. 16.0h). Every session that hit the cap was
therefore exactly the same length and every capped day exactly the same
total — a point mass that shows up as a sharp spike in the session-length /
daily-total histogram. Session length is a data-rich signal (many short
sessions per account), so a one-sample KS / chi-square density test against
human session-length data flags it readily.

Now: `continuous = triangular(0.80x, 1.0x, mid)`, `daily =
triangular(0.85x, 1.0x, mid)`. Triangular (not uniform) so the density
tapers to zero at the band edges — a uniform draw would leave a rectangular
support step a KDE edge check can still see. Key invariant: the effective
cap is always **≤ the configured hard ceiling**, so we never work *longer*
than the safety limit (over-activity is exactly what the cap protects
against).

Cadence matters:
- The **continuous** cap re-jitters per session (each `start_session()`,
  and on idle auto-reset / idle-restart) — the session counter resets to 0
  at those boundaries, so there's no oscillation near the limit.
- The **daily** cap re-jitters only across a **local-day** boundary
  (tracked via a persisted `daily_cap_day`). Resampling it every short
  session would make the capped daily total the max of several draws — an
  order statistic that drifts back toward the ceiling. `can_continue()` and
  state-load both refresh it on a day change so a long-running or resumed
  process can't gate on a stale cap.

Persistence: both caps live in `.scheduler_state.json` for same-day restart
consistency. On load they're validated — non-finite (nan/inf) or out-of-band
values are rejected and a fresh in-band value is used, because accepting a
nan would make every `usage >= cap` comparison false and silently disable
the safety gate.

**Deferred (next step):** persona-bind the cap *centers* so each account has
a stable per-account mean (via a persona-seeded RNG, like
`throttler.seed_gap_shape`) with a truncated log-normal/beta around it.
Triangular removes the per-account point mass but all accounts still share
the same support and midpoint; a fleet-wide density comparison could still
cluster them.

### Warm-up route randomization + behavioral salt

**Decision:** `PageNavigator.warm_up()` draws a persona-weighted variable
route instead of a fixed skeleton, and the persona gained a persisted
per-account `salt` that seeds all local behavioral RNGs.

Before: warm-up was `dorf1 -> dorf2 -> [20% stats] -> [10% profile] ->
dorf1` — the same transitions every session. Request sequences are
higher-volume and more server-visible than the session-length signal from
the scheduler caps, and this one was deterministic: every session began
`dorf1 -> dorf2` and ended `-> dorf1`. An n-gram / first-order
Markov-transition chi-square or edit-distance clustering over warm-up routes
separates these accounts from humans (and groups the bot's accounts
together) immediately.

Now: dorf1 is always first (still prevents the "login -> immediate API
blast" tell), then each of `{dorf2, statistiken, spieler, karte}` is included
with this account's stable probability, the chosen subset is visited in
randomized order, and the bot settles on a persona-weighted home page. The
inclusion probabilities are seeded once from the persona (`seed_routes`), so
they are stable for one account across restarts but differ between accounts;
the per-call realization still varies. All candidates are top-level pages so
the Referer chain stays coherent.

**Why not pure per-call randomness?** Then every account would share the
*same* route distribution — a different but equally clusterable fleet-wide
signature. Persona-stable preferences give each account a distinct,
internally consistent browsing personality, the way a real player has
habits.

**Behavioral salt.** The seed identity was `UA|language|server`, which is
low-entropy on a single world (a few UAs, one server-derived language) — so
accounts collided into a handful of latent buckets for both the request-gap
shape (cycle 1) and the warm-up routes. The persona now carries a persisted
`salt` (`secrets.token_hex(8)`, never sent to the server); the identity is
`UA|language|server|salt`, used to seed both. Legacy persona files without a
salt get one backfilled on load (preserving `created_at` so the 365-day TTL
isn't reset).

Cost: 0 — same number of warm-up page loads in expectation; the salt is a
local-only field.

**Deferred (next step):** replace the independent-Bernoulli subset sampling
with a persona-stable first-order Markov transition matrix (bounded path
length), so the *transition structure* varies per account too, not just the
visited set. Validate against transition-count chi-square / likelihood-ratio,
route-length KS, and edit-distance clustering.

### Heavy-tailed timing

**Decision:** kept `HumanDelay` (action-class triangular) but use
`HumanTiming.delay()` (heavy-tailed) for loop intervals and reaction
windows.

The two engines target different problems:

- `HumanDelay` answers "how long does a user pause between clicking
  Buildings and clicking Upgrade?" — the answer is short and tight, a
  triangular distribution captures it well.
- `HumanTiming.delay` answers "how long until the user reacts to a
  build slot freeing?" — the answer is bursty: most reactions are quick
  but occasional ones are minutes long. Heavy-tailed is correct.

Cost: 0 mean-time change for either; the distributions just match real
data better.
Benefit: defeats simple variance-based timing fingerprints.

### Throttler gap distribution

**Decision:** the global request throttler draws its inter-request gap from
a *shifted log-normal* instead of `random.uniform(min_gap, max_gap)`, and
binds the distribution's shape to the persona.

Before: `target_gap = random.uniform(min_gap_s, max_gap_s)`. This produced a
flat gap histogram over `[min, max]` — the exact "uniform-random timing"
pattern guiding principle #2 warns against. A detector fitting a
distribution to inter-request gaps separates a flat band from human traffic
(which is right-skewed) with a one-sample KS test on a few hundred requests.
This also contradicted this very doc, which previously claimed the throttler
used "random-exponential gaps" — it did not.

Now: `min_gap_s + lognormvariate(log(span * frac), sigma)`, floored at
`min_gap_s` (the shift means no spike piles up at the floor), body inside
`[min_gap, max_gap]`, tail soft-capped at `3x max_gap`. The shape parameters
(`frac` in [0.30, 0.48], `sigma` in [0.45, 0.85]) are drawn per instance by
default and bound to a persona-stable identity via `seed_gap_shape()` when a
persona exists, so:

- two accounts on the same config don't emit an identical *normalized* gap
  shape a cross-account likelihood-ratio test could fingerprint, and
- one account doesn't *drift* to a new shape on every restart, which a
  two-sample KS / Cramer-von Mises test across that account's sessions could
  otherwise catch.

Intra-session non-stationarity (rapid clusters, reading pauses, fatigue) is
deliberately left to the upper layers (`NoiseInjector` breaks, `HumanDelay`
think-pauses, `session_manager` caps) — the throttler is the global lower
envelope, not the macro-behavior engine. Stacking regimes here would
double-count.

Cost: 0 expected mean-time change (the body is centered in the same band);
the distribution just stops being flat.
Benefit: removes a primary statistical timing tell at zero throughput cost.

The reviewer flagged the triangular distributions as still detectable in
principle. We have not switched HumanDelay to log-normal because:

- The action-class profiles are tuned (mode/min/max) to match observed
  user data and re-tuning to log-normal parameters is risky.
- Anti-bot systems that distinguish triangular from log-normal would
  need much larger sample sizes than they have for individual users.
- Adding log-normal everywhere is a much bigger refactor than the
  proportional benefit. (Marked as deferred.)

## Per-feature decisions

### Oasis raider

**Tile-detail Referer.** Before: `_simulate_map_browsing` called
`navigator.idle_browse` which could visit `/profile.php`,
`/statistiken.php`, etc. The next `tile-details` AJAX then carried a
Referer from a non-map page — impossible from a real browser. Now:
`navigate_to_map` so the popup looks like it was opened from the map
page.

**Cost:** 0 (replaces an existing visit, doesn't add one).

**Soft-failure handling.** Before: troops were locally deducted and
`raids_in_burst` incremented regardless of `result.success`. A failure
mode like "no confirmation form returned" produced phantom sends in the
local accounting while the bot pressed on with depleted-on-paper troops.
Now: deduct + increment ONLY on `result.success`; soft-failure pauses
the sweep with a 60s throttle penalty.

**Cost:** sweeps may stop early if Travian soft-blocks. This is a
feature, not a bug.

**Recurring interval jitter.** Before: exact `repeat_interval_seconds`
between sweeps. Now: `micro_jitter(0.10)` + `max(1, round())` floor.

**Cost:** ±10% wall-clock between sweeps. Negligible.

**Deferred:** tile-detail caching to deduplicate the enrich+JIT call
pair. Worth a future pass; would reduce request volume by ~40% on
recurring sweeps but requires careful staleness handling.

### Farm-list

**Stealth floor on `interval`.** Before: client could configure
`interval=10s` even with stealth on, producing impossible-to-mistake
human cadence. Now: 60s floor when stealth is on, 10s when off.

**Cost:** users explicitly choosing stealth+10s now get 60s. The 10s
configuration was always a tell so this isn't a real loss.

**Per-cycle batch size jitter.** Before: invariant 5,5,5,...
payload-shape signature across runs. Now: `random.randint(4, 7)` per
cycle.

**Cost:** 0 (batch math is unchanged in expectation).

**Cross-village navigation.** Before: `send_all_farm_lists` fired API
calls for village B while the browser/Referer context still said
village A. Now: lists grouped by `owner_village.id` and a fresh
`navigate_to_farm_list(owner_vid)` between groups.

**Cost:** +2-6s per cross-village handoff. Acceptable — this is a real
human action a player would take.

**Troop-exhaustion cursor advance.** Before: cursor stayed at 0
relative-to-batch-start when troops ran out, so the next cycle retried
the same depleted batch first — instant-retry on the same empty slots
is a clear automation pattern. Now: advance past the depleted batch.

**Cost:** 0; arguably faster since we stop wasting requests on slots
that just returned "not enough troops."

### Build queue

**Wakeup precision.** Before: queue slept until `remaining + 3` seconds
with 5% jitter. Real users take 30s-5min to notice a free slot. Now:
`_post_build_reaction()` heavy-tailed delay with mean ~45s clamped to
[20s, 300s], fired once per queue→empty transition.

**Cost:** +30s-5min per build. This is the biggest perf cost in the
stealth pass. Justified because the wakeup-at-zero pattern is the most
specific build-queue tell.

**Resource-short pacing.** Before: when resources were short, retried
every `poll_interval_s` (default 30s). Real players who lack resources
plan and come back later — they don't poll every 30s. Now: 2-10min
heavy-tailed wait when stealth is on, falls back to `poll_interval_s`
when off.

**Cost:** the bot is slower to react to resource availability changes.
For a player whose builds take 20+ minutes, this is invisible. For a
heavy-trade flow it's noticeable; mitigation is to keep stealth off for
those short-loop scenarios.

**Per-account stagger.** Before: two village queues on the same account
could fire build POSTs at synchronized timestamps. Real players can
only click in one village at a time. Now: a per-account `asyncio.Lock`
serializes build actions and inserts a 10-90s stagger when another
queue acted within the last 60s.

**Cost:** +10-90s when multiple queues collide. Only fires under the
collision condition.

**Poll interval clamp.** Before: client could send `poll_interval=1`
and force tight polling. Now: clamped to `[30, 3600]`.

**Cost:** 0 for default clients.

### Auto-scout / map-scan

**Scan-center order.** Before: deterministic nested-coordinate raster.
A real player pans around clusters, not left-to-right top-to-bottom.
Now: sorted by squared distance from center, then in-place
shuffle within 4-tile buckets.

**Cost:** 0 (still O(n) and same total tile count).

**Filter-target permutation.** Before: pure distance sort produced an
identical permutation across runs with the same scan input. Now:
distance sort, then in-place bucket-shuffle by integer distance bucket.

**Cost:** 0.

**`navigate_to_map` before tile loop.** Same Referer-chain reasoning as
oasis raider tile-details.

### Farm builder

**`_pace_add` between slot adds.** Before: back-to-back JSON POST
mutations with no per-slot pause. A player editing the farm list clicks
through fields and pauses occasionally. Now: RAPID-class delay before
every add_slot + heavy-tailed 2s pause every 8 slots on a list.

**Cost:** +0.4-2s per slot, ~1-3s per 8-slot block. For a 200-target
build that's +90-150s. Acceptable; this is a one-shot operation, not a
hot loop.

**Per-bucket shuffle.** Before: bucket iteration produced an identical
cross-list insertion order across runs. Now: shuffled per-run.

**Cost:** 0.

**Failure-recovery jitter.** Before: fixed `asyncio.sleep(5)` /
`asyncio.sleep(10)` retries. Now: `micro_jitter(0.35)` on the same
means.

**Cost:** ±35% on a failure path, negligible since failures are rare.

## What we explicitly didn't do

- **Globally switch to log-normal delay distributions.** Big refactor,
  marginal benefit at our scale. Triangular profiles are tuned and
  work.
- **Endpoint-aware throttler with separate gap profiles.** The global
  throttler with persona-bound log-normal gaps (see "Throttler gap
  distribution" below) is good enough; per-endpoint buckets would add
  complexity without removing a clear tell.
- **Cookie jar with full attribute preservation** (domain, path,
  expires, secure, httpOnly, sameSite, ordering). The current flat
  `{name: value}` storage works for Travian's cookie set; structured
  preservation matters more for cross-origin scenarios than for
  same-origin Travian traffic.
- **Workflow-aware noise injection** (reports/messages after raids,
  map/player pages near scouting). Current independent-Bernoulli noise
  is sufficient; correlation-with-current-action would add complexity
  for marginal stealth benefit.
- **Captcha post-resolution warmup.** Currently the captcha guard
  releases traffic immediately on resolve. Adding a 2s warmup window
  would be more realistic but the current behavior is conservative
  enough.

These are documented as deferred in the per-feature Codex reviews; they
can be re-litigated if Travian changes their detection.

## Speed-factor recommendations

| Profile | `STEALTH_SPEED` | When to use |
|---|---|---|
| **Aggressive** | 0.5 | Short attended sessions, you're watching the screen and accept some risk. |
| **Normal** | 1.0 | Default. Balanced for unattended multi-hour ops. |
| **Cautious** | 1.5 | Long unattended sessions, new accounts, fresh servers. |
| **Paranoid** | 2.0 | Recently-flagged accounts, post-warning servers. |

`STEALTH_SPEED` only multiplies `HumanDelay` (action-class) waits. It
does NOT multiply `HumanTiming.delay()` (heavy-tailed loop intervals)
because those are sized for the loop's purpose (e.g. build wakeup
reaction is sized in the [20s, 300s] window directly), and amplifying a
heavy-tailed mean would produce uselessly long pauses on the tail.

## Performance-cost summary

Roughly, on a typical session:

| Change | Per-event cost | Frequency | Per-hour total |
|---|---|---|---|
| Header/Accept-Encoding/zstd | 0 | every request | 0 |
| Persona stability | 0 | per session start | 0 |
| `request_type=xhr` plumbing | 0 | many | 0 |
| PRG redirect headers | 0 | every state-mutating request | 0 |
| Random-exponential retry | 0 expected | retries (rare) | 0 |
| Captcha guard escalation | 0 expected | block events (rare) | 0 |
| Oasis tile-details Referer | 0 | per oasis | 0 |
| Oasis soft-failure pause | 60s | failure events | 0 typical |
| Oasis interval jitter | ±10% of repeat_interval | per recurring sweep | 0 net |
| Auto-scout shuffle | 0 | per scan | 0 |
| Farm-list batch jitter | ~0.5s/batch | per cycle | a few seconds |
| Farm-list cross-village nav | 2-6s | per village handoff | seconds |
| Build queue wakeup | 30s-5min | per slot-free | minutes (1-5/hour) |
| Build queue resource-short | 2-10min | per resource gate | minutes |
| Build queue village stagger | 10-90s | per collision | rare |
| Farm builder pacing | 0.4-2s/slot | bulk-edit ops only | once-per-run |

Net: a single farm loop adds a few seconds per cycle. A build queue
adds 1-5 minutes per build. The biggest cost is the build wakeup
reaction; it's also the highest-stealth-value change.
