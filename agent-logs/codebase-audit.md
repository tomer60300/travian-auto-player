# Codebase Audit — cli-anti-bot Branch
Date: 2026-04-05

## CLI Command Groups & Subcommands

### auth
- `auth login` — Login, show player info
- `auth token` — Print JWT

### village
- `village list` — List villages (table)
- `village switch <id>` — Switch active village

### building
- `building list [-v <vid>]` — List all buildings
- `building upgrade --slot-id X [-v <vid>] [--allow-gold]` — Upgrade building
- `building construct --slot-id X --building NAME [-v <vid>] [--allow-gold]` — Build new
- `building resources [-v <vid>]` — Show current resources
- `building queue [-v <vid>]` — Show construction queue

### military
- `military scout --x X --y Y [--amount N] [--type resources|defenses] [-v <vid>]` — Send scouts
- `military raid --x X --y Y [--troop t1=50]` — Send raid

### reports
- `reports list [--max-age-hours N] [--max-pages N]` — List recent reports
- `reports show <id>` — Show report detail

### queue (build queue)
- `queue run <plan.yaml> [--dry-run] [--poll N] [--use-video] [--verbose] [--log-file]` — Execute build plan
- `queue validate <plan.yaml>` — Validate plan against current state

### video
- `video available` — Check available video rewards
- `video claim <type> [--village-id] [--slot-id] [--building-id]` — Claim a reward (~33s)
- `video claim-all` — Claim all available production boosts

### farm
- `farm list` — List all farm lists
- `farm show <id>` — Show farm list targets
- `farm send <id> [--yes]` — Send all active targets
- `farm create --name NAME [-v <vid>]` — Create new farm list
- `farm add-target <id> --x X --y Y [--troop t1=5]` — Add target
- `farm delete <id> [--yes]` — Delete farm list

### scout
- `scout scan --radius R [-v <vid>] [--max-pop] [--min-pop] [--no-player] [--limit]` — Scan map
- `scout auto --radius R [-v <vid>] [--type] [--amount] [--limit] [--dry-run] [--yes]` — Auto-scout

## Anti-Bot Mechanisms Already Implemented (stealth/ package)

1. **UserAgentRotator** — Pool of 12 real 2026 browser UAs (Chrome/Firefox/Edge). One per session, consistent.
2. **BrowserHeaders** — Generates correct Sec-Fetch-*, Accept-Language, Referer chains per request type.
3. **RequestThrottler** — Min/max gap between requests (1.5-3.0s default), burst detection (20 req/60s), adaptive penalties.
4. **HumanDelay** — Triangular distribution delays per action type (page load, click, decision, etc). Micro-pauses (5%), periodic think pauses.
5. **PageNavigator** — Simulates dorf1→build.php navigation chains before actions. Idle browsing.
6. **SessionManager** — Session duration limits (10-45min), action counts (max 50), break recommendations.
7. **Bot detection checker** — Scans responses for captcha, rate limiting, ban indicators. Adds throttle penalties.

## HTTP Client Config
- httpx AsyncClient with cookie jar
- Base headers: User-Agent (from rotator) + X-Version
- Retry: 3 attempts, exponential backoff
- Stealth pre-request: throttle + headers per type
- Post-request: update last page (for Referer chain)
- Session expiry detection with re-auth callback

## Config (Settings)
- env_prefix = "TRAVIAN_" 
- stealth defaults: enabled, speed=1.0, min_gap=1.5s, max_gap=3.0s, navigate=True, burst_max=20, burst_cooldown=15s

## Dry-run Support
- `queue run --dry-run` — Yes, previews build plan
- `scout auto --dry-run` — Yes, shows targets without sending
- Other commands: No explicit --dry-run flag; use --help for verification

