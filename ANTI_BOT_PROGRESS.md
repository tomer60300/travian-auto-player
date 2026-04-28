# Anti-Bot Feature Progress

## Status: PRODUCTION ✅ — Multiple hardening passes complete

**Last major pass:** 2026-04-28 (the "stealth-vs-performance" review,
covering 49 findings across 6 features). Most P1/P2 items addressed in
that pass; remaining deferred items are documented in
`docs/23-stealth-decisions.md`.

For the current architecture see `docs/21-stealth-anti-bot.md`. For the
trade-off analysis behind each design choice see
`docs/23-stealth-decisions.md`. For the cross-device session control
that integrates with activity scheduling see
`docs/22-resumable-operations.md`.

## Completed milestones

### Foundation (2026-04-02 / `cli-anti-bot` branch)
- Stealth module created with timing, headers, throttler, human_delay,
  navigator, session_manager, user_agents.
- HTTP client middleware: stealth applied to all requests.
- CLI integration: `--stealth/--no-stealth`, env vars.
- Service updates: military, build queue, auto-scout, video reward.

### Captcha guard (2026-04-13)
- `CaptchaGuard` async-event gate blocking all outbound traffic.
- Structural HTML evidence required for high-confidence patterns to
  avoid false positives from `upgradeBlocked` and similar.
- Frontend modal + REST endpoints (`/api/captcha/{status,resolve}`).

### Persona / TLS coherence (later passes)
- `Persona` dataclass tying UA + curl_cffi impersonate target +
  sec-ch-ua + platform + accept-language together.
- Persistent persona file with creation timestamp.

### Stealth pass — 2026-04-28
- TLS: fail-closed without curl_cffi.
- Headers: zstd Accept-Encoding for Chromium personas; XHR shape on
  endpoints called by Travian frontend JS (map, farm-list, tile
  details); page-load headers on PRG-redirected GET.
- Persona: TTL 7d → 365d; server-URL scoped.
- Retry: jittered (`wait_random_exponential`).
- Captcha: short 403/503 with high-confidence phrases now hard-fire
  the guard (was soft-only).
- Per-feature stealth fixes for oasis raider, farm-list, auto-scout,
  build queue, farm builder. Detailed in `docs/23-stealth-decisions.md`.

## Currently deferred (lower-priority; documented in stealth-decisions)

- Globally switching `HumanDelay` from triangular to log-normal
  distributions.
- Endpoint-aware throttler with separate gap profiles per
  endpoint class.
- Cookie jar with full attribute preservation (domain/path/expires/
  secure/httpOnly/sameSite/ordering).
- Workflow-aware noise injection (correlated with current action
  rather than independent Bernoulli).
- Captcha post-resolution warm-up window.
- Tile-detail caching to deduplicate enrich+JIT call pairs.

These are tracked in the per-feature Codex review threads and can be
revisited if Travian changes their detection.
