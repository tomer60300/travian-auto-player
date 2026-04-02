# Anti-Bot Feature Progress

## Branch: cli-anti-bot
## Started: 2026-04-02 18:27

## Files to Create/Modify

### New Files
- [ ] `src/travian_api/stealth/__init__.py` — stealth module
- [ ] `src/travian_api/stealth/user_agents.py` — realistic UA rotation
- [ ] `src/travian_api/stealth/headers.py` — browser-like headers with Referer chain
- [ ] `src/travian_api/stealth/throttler.py` — global request rate limiter
- [ ] `src/travian_api/stealth/human_delay.py` — random human-like delays
- [ ] `src/travian_api/stealth/navigator.py` — page navigation simulation
- [ ] `src/travian_api/stealth/session_manager.py` — session lifetime, breaks, idle browsing

### Modify Existing
- [ ] `src/travian_api/clients/http_client.py` — integrate stealth middleware
- [ ] `src/travian_api/config.py` — add stealth config options
- [ ] `src/travian_api/services/military_service.py` — delays between troop send steps
- [ ] `src/travian_api/services/build_queue_service.py` — delays in auto-builder
- [ ] `src/travian_api/services/auto_scout_service.py` — randomized delays
- [ ] `src/travian_api/services/video_reward_service.py` — jitter on timing
- [ ] `src/travian_api/services/farm_list_service.py` — delays between farm sends
- [ ] `src/travian_api/cli.py` — add --stealth flag

## Implementation Order
1. Core stealth module (UA, headers, throttler, delays)
2. Integrate into http_client
3. Config options
4. Update all services
5. CLI flag
6. Test

## Status: IN PROGRESS — starting core stealth module
