# Anti-Bot Feature Progress

## Branch: cli-anti-bot
## Started: 2026-04-02 18:27
## Last Updated: 2026-04-02 22:40

## Status: CORE COMPLETE ✅ — Pushed to GitHub

### Completed ✅

#### New Stealth Module (`src/travian_api/stealth/`)
- [x] `__init__.py` — module entry point
- [x] `user_agents.py` — 14 real browser UAs (Chrome/Firefox/Edge on Win/Mac)
- [x] `headers.py` — browser-accurate headers with Sec-Fetch-*, Referer chains
- [x] `throttler.py` — global rate limiter with burst detection + auto-penalty
- [x] `human_delay.py` — 10 action types, triangular distribution, micro-pauses
- [x] `navigator.py` — page navigation simulation before actions
- [x] `session_manager.py` — session lifetime tracking with break suggestions

#### Core Integration
- [x] `http_client.py` — stealth middleware on ALL requests
- [x] `config.py` — stealth settings (env vars: TRAVIAN_STEALTH, etc.)
- [x] `cli.py` — `--stealth/--no-stealth` global flag

#### Service Updates
- [x] `military_service.py` — delays between troop send steps
- [x] `build_queue_service.py` — pre-upgrade browsing, idle during waits
- [x] `auto_scout_service.py` — randomized jitter on delays
- [x] `video_reward_service.py` — jitter on ATG timing

### Testing
- [x] All stealth modules import cleanly
- [x] `--no-stealth auth login` works (fast mode)
- [x] `--stealth building resources` works (with delays)
- [x] Committed and pushed to `cli-anti-bot` branch

### Bug Fixes (Apr 3)
- [x] **FALSE POSITIVE FIX:** `_check_suspicious_response` was matching "blocked" in Travian's normal `upgradeBlocked` CSS class, triggering 60s throttle penalty on every upgrade. Replaced naive substring matching with context-aware detection (high-confidence patterns + structural checks for captcha/ban).
- [x] **Service attribute fix:** Services used `http_client.delay` instead of `http_client.human_delay` — would crash at runtime.
- [x] **ActionType fix:** Services referenced non-existent enums (`THINKING`, `BETWEEN_ACTIONS`, `FARM_SEND`, `PAGE_READ`) — fixed to use correct values.
- [x] **Navigator integration:** `building_service.upgrade()` now calls `navigator.pre_upgrade_flow()` for realistic page navigation before upgrades (was only doing a bare delay).
- [x] **User-Agent update:** Updated browser UA strings from Chrome 120-124 era to Chrome 132-135 / Firefox 135-137 (current for April 2026).

### Remaining / Future Enhancements
- [ ] Integration tests (mock server, verify timing patterns)
- [ ] Session manager integration into auto-builder loop
- [ ] Configurable "play schedule" (active hours, break patterns)
- [ ] Cookie persistence across sessions (save/load jar)
- [ ] Anti-fingerprint: randomize X-Version per session
- [ ] Captcha detection → Discord alert to human
