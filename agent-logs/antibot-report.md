# Anti-Bot Detection Audit Report
Branch: cli-anti-bot
Date: 2026-04-05
Commit: d5faa8f

## Overall Score: 30/50
## Risk Level: MODERATE

## Executive Summary
The codebase implements a solid anti-bot foundation with Chrome TLS fingerprinting (curl_cffi),
heavy-tailed timing distributions, request throttling, session warm-up, and behavioral noise
injection. However, critical gaps in sec-ch-ua headers, header ordering (httpx limitation),
and protocol fingerprinting (API-only traffic, no HTML page loads) create detectable patterns.
The timing layer is excellent (8/10) but undermined by 2 hardcoded sleeps in the CLI.

## Scores by Vector
| Vector | Score | Worst Vulnerability |
|---|---|---|
| HTTP Fingerprinting | 5/10 | Missing sec-ch-ua headers + httpx header ordering |
| Request Timing | 8/10 | 2 hardcoded sleeps in cli.py (time.sleep(2), asyncio.sleep(1)) |
| Session Behavior | 6/10 | Deterministic warm-up sequence (always dorf1→dorf2→dorf1) |
| Action Patterns | 5/10 | Sequential farm sends, instant resource-triggered upgrades |
| Protocol Fingerprinting | 6/10 | API-only traffic (no HTML page loads), static x-version |

## Top 10 Vulnerabilities (ranked by detection risk)

1. **Missing sec-ch-ua headers** — Chrome requires sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-full-version. Only sec-ch-ua-platform is set. Instant bot flag for modern anti-bot. — File: stealth/headers.py:46-52

2. **Header ordering not preserved** — httpx does not guarantee Chrome header order. Anti-bot systems check ordering. — File: clients/http_client.py (httpx limitation). Mitigation: curl_cffi handles this when active.

3. **Hardcoded time.sleep(2) in CLI parent monitor** — Fixed 2-second intervals are a bot signature. — File: cli.py:202

4. **Hardcoded asyncio.sleep(1) in video progress** — 33 evenly-spaced 1s intervals during video claims. — File: cli.py:827

5. **API-only traffic pattern** — 99% API endpoints, 0% HTML page loads. Real clients load HTML/CSS/JS. — File: all services (GraphQL-heavy, no page navigation during operations)

6. **Deterministic warm-up sequence** — Always visits dorf1→dorf2→[maybe stats]→dorf1 after login. — File: stealth/navigator.py:72-85

7. **Sequential farm sends with fixed order** — Sends all farms one-by-one with BETWEEN_RAIDS delay. Real players batch or skip. — File: services/farm_list_service.py:271-295

8. **Static x-version header** — Never changes across sessions. Real clients update with game patches. — File: config.py, http_client.py:265

9. **curl_cffi optional fallback** — If not installed, falls back to httpx with detectable TLS fingerprint. — File: http_client.py:58-65

10. **Cookie persistence lacks attributes** — Saves name=value only, no domain/path/expires/secure flags. — File: http_client.py:81-105

## Hardening Roadmap

### Phase 1: Quick Wins (< 1 day each, high impact)

**1. Add sec-ch-ua headers (stealth/headers.py)**
Current: Only sec-ch-ua-platform set
Fix: Add sec-ch-ua, sec-ch-ua-mobile matching User-Agent version
```python
# In for_page_load(), for_json_post(), etc:
headers["sec-ch-ua"] = '"Chromium";v="135", "Google Chrome";v="135", "Not-A.Brand";v="8"'
headers["sec-ch-ua-mobile"] = "?0"
headers["sec-ch-ua-platform"] = '"Windows"'
```
Impact: +2 HTTP Fingerprinting score

**2. Fix hardcoded sleeps in cli.py**
Current: time.sleep(2) at line 202, asyncio.sleep(1) at line 827
Fix:
```python
# Line 202: import random; time.sleep(random.uniform(1.8, 2.3))
# Line 827: await asyncio.sleep(random.uniform(0.8, 1.2))
```
Impact: +1 Timing score, removes 2 trivially detectable patterns

**3. Randomize warm-up sequence (stealth/navigator.py:warm_up)**
Current: Always dorf1 → dorf2 → [maybe stats] → dorf1
Fix: Randomize page order, skip pages 30% of the time, vary count 2-5 pages
Impact: +1 Session Behavior score

### Phase 2: Core Hardening (1-3 days each)

**4. Add HTML page loads between API calls**
Inject occasional GET dorf1.php/dorf2.php between GraphQL sequences.
Every 5-10 API calls, load one HTML page to create realistic traffic mix.
Impact: +2 Protocol Fingerprinting score

**5. Randomize farm send order and add skips**
Shuffle target list before sending. Skip 10-20% of targets randomly per cycle.
Impact: +1 Action Patterns score

**6. Add realistic pre-upgrade browsing flow**
Before upgrade: load dorf page → load build.php?id=X → wait 5-15s → upgrade
Impact: +1 Action Patterns, +1 Session Behavior

### Phase 3: Advanced Evasion (3+ days)

**7. Dynamic x-version tracking**
Periodically check game client version and update header.
Impact: +1 Protocol Fingerprinting

**8. Full cookie jar with Set-Cookie parsing**
Parse response Set-Cookie headers, preserve domain/path/expires attributes.
Impact: +1 HTTP Fingerprinting

**9. GraphQL field noise**
Add unused fields to GraphQL queries to match real client traffic patterns.
Impact: +1 Protocol Fingerprinting

## Before/After: Typical HTTP Request

### CURRENT (detectable):
```
POST /api/v1/graphql HTTP/2
User-Agent: Mozilla/5.0 ... Chrome/135.0.0.0 ...
Accept: application/json, text/plain, */*
Accept-Language: en-US,en;q=0.9
Content-Type: application/json
sec-ch-ua-platform: "Windows"          ← MISSING sec-ch-ua, sec-ch-ua-mobile
Sec-Fetch-Site: same-origin
Sec-Fetch-Mode: cors
Sec-Fetch-Dest: empty
                                       ← NO preceding HTML page load
                                       ← headers may be out of order (httpx)
```

### HARDENED (recommended):
```
POST /api/v1/graphql HTTP/2
sec-ch-ua: "Chromium";v="135", "Google Chrome";v="135", "Not-A.Brand";v="8"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "Windows"
User-Agent: Mozilla/5.0 ... Chrome/135.0.0.0 ...
Accept: application/json, text/plain, */*
Accept-Language: en-US,en;q=0.9
Accept-Encoding: gzip, deflate, br
Content-Type: application/json
Origin: https://ts2.x1.europe.travian.com
Referer: https://ts2.x1.europe.travian.com/dorf1.php
Sec-Fetch-Site: same-origin
Sec-Fetch-Mode: cors
Sec-Fetch-Dest: empty
X-Version: 389
```

## Before/After: 1-Hour Session Timeline

### CURRENT PATTERN (periodic):
```
00:00 LOGIN → warm-up (dorf1→dorf2→dorf1, always same)
00:10 API: check resources
00:10 API: check queue
00:10 API: upgrade (instant reaction)
00:40 API: check resources (exactly 30s later)
01:10 API: check resources (exactly 30s later)
... [metronome pattern with ±15% jitter]
```

### RECOMMENDED PATTERN (irregular human):
```
00:00 LOGIN → warm-up (dorf1→map→dorf2→profile, varied)
00:08 GET dorf1.php (browsing)
00:12 API: check resources
00:15 GET dorf2.php (browsing to buildings)
00:22 API: check queue
00:28 GET build.php?id=2 (reading upgrade page)
00:36 API: upgrade (delayed reaction after reading)
01:05 GET dorf1.php (idle browsing)
01:22 GET karte.php (checking map, noise)
01:35 API: check resources (42s gap, not 30)
... [bursty pattern with long pauses]
```
