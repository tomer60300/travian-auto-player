"""End-to-end test: full stealth upgrade flow with all anti-bot features active."""
import asyncio
import sys
import time

sys.path.insert(0, "src")

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings
from travian_api.services.auth_service import AuthService
from travian_api.services.building_service import BuildingService


async def main():
    print("=== Stealth Upgrade E2E Test ===\n")
    s = Settings(stealth=True)
    c = HttpClient(s)
    auth = AuthService(c, s)
    building_svc = BuildingService(c)

    # Step 1: Login
    print("[1] Logging in with stealth...")
    t0 = time.time()
    login = await auth.login()
    print(f"    OK: {login.player_name} ({time.time() - t0:.1f}s)")

    # Step 2: Check resources
    print("[2] Checking resources...")
    res = await building_svc.get_resources(village_id=41699)
    print(f"    Resources: {res}")

    # Step 3: Check queue
    print("[3] Checking build queue...")
    queue = await building_svc.get_construction_queue(village_id=41699)
    if queue:
        for item in queue:
            print(f"    Building: {item}")
    else:
        print("    Queue empty")

    # Step 4: Visit build pages that contain 'upgradeBlocked' CSS class
    print("[4] Visiting build pages (false-positive stress test)...")
    for slot in [1, 3, 26, 30]:
        html = await c.get_html(f"/build.php?id={slot}")
        has_blocked = "upgradeblocked" in html.lower()
        print(f"    Slot {slot}: {'has upgradeBlocked CSS' if has_blocked else 'clean'} ({len(html)} chars)")

    # Step 5: Verify no throttle penalty was triggered
    print("[5] Checking throttle state...")
    penalty_active = time.monotonic() < c.throttler._penalty_until
    reqs = c.throttler.requests_in_window
    print(f"    Requests in window: {reqs}")
    print(f"    Penalty active: {penalty_active}")
    if penalty_active:
        print("    *** FALSE POSITIVE DETECTED! Penalty was triggered incorrectly ***")
        await c.close()
        sys.exit(1)

    print(f"\n=== ALL CHECKS PASSED - Stealth mode working correctly ===")
    print(f"Total requests: {reqs}")
    print(f"Total time: {time.time() - t0:.1f}s")

    await c.close()


asyncio.run(main())
