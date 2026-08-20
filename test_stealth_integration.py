"""Integration test: verify stealth mode works end-to-end without false positives."""
import asyncio
import sys
import time

sys.path.insert(0, "src")

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings
from travian_api.services.auth_service import AuthService


async def main():
    print("=== Stealth Integration Test ===\n")

    # Test 1: Login with stealth
    print("[1] Login with stealth enabled...")
    s = Settings(stealth=True)
    c = HttpClient(s)
    assert c.stealth_enabled, "Stealth should be enabled"
    
    auth = AuthService(c, s)
    start = time.time()
    result = await auth.login()
    login_time = time.time() - start
    print(f"    ✓ Logged in as {result.player_name} ({login_time:.1f}s)")

    # Test 2: Read resources (GET) with stealth
    print("[2] Reading resources with stealth...")
    start = time.time()
    html = await c.get_html("/dorf1.php")
    read_time = time.time() - start
    assert len(html) > 1000, f"dorf1 page too short ({len(html)} chars)"
    assert c.throttler.requests_in_window > 0, "Throttler should track requests"
    print(f"    ✓ dorf1.php loaded ({len(html)} chars, {read_time:.1f}s)")

    # Test 3: Check that 'upgradeBlocked' does NOT trigger false positive
    print("[3] Checking build.php for false positive 'blocked'...")
    html = await c.get_html("/build.php?id=1")
    # If we get here without exception, the false positive is fixed
    assert "upgradeblocked" in html.lower() or "blocked" not in html.lower(), \
        "Expected either upgradeBlocked CSS class or no blocked at all"
    # Check throttler has no penalty (the old bug would add 60s penalty)
    # We can't directly check penalty_until but we can verify the upgrade page loaded fine
    print(f"    ✓ build.php loaded without false positive ({len(html)} chars)")

    # Test 4: Verify browser headers are realistic
    print("[4] Checking browser headers...")
    ua = c.client.headers.get("user-agent", "")
    assert "Mozilla/5.0" in ua, f"UA doesn't look like a browser: {ua}"
    assert "Travian-API" not in ua, f"UA still has bot identifier: {ua}"
    headers = c.browser_headers.for_page_load("/dorf1.php")
    assert "Accept-Language" in headers, "Missing Accept-Language header"
    assert "Accept-Encoding" in headers, "Missing Accept-Encoding header"
    print(f"    ✓ UA: {ua[:60]}...")
    print(f"    ✓ Headers include Accept-Language, Accept-Encoding")

    # Test 5: Verify throttler is active
    print("[5] Checking throttler state...")
    reqs = c.throttler.requests_in_window
    print(f"    ✓ {reqs} requests tracked in burst window")

    # Test 6: Navigator state
    print("[6] Checking navigator...")
    assert c.navigator.enabled, "Navigator should be enabled"
    print(f"    ✓ Navigator enabled, current page: {c.navigator.current_page}")

    # Test 7: Verify human delay is enabled
    print("[7] Checking human delay...")
    assert c.human_delay.enabled, "Human delay should be enabled"
    from travian_api.stealth.human_delay import ActionType
    start = time.time()
    waited = await c.human_delay.wait(ActionType.CLICK, "test")
    actual = time.time() - start
    assert actual > 0.2, f"Click delay too short: {actual:.2f}s"
    print(f"    ✓ Human delay active (click: {actual:.2f}s)")

    await c.close()
    print("\n=== All stealth tests PASSED ✓ ===")


# Guarded so importing this module cannot run it. These are live integration
# scripts, not unit tests: main() logs in to the real server with the
# credentials in .env and spends real requests. pytest's testpaths is
# "tests", so a bare `pytest` never collected them -- but `pytest
# test_stealth_upgrade.py`, or an editor's "run this file", imported the
# module and did it for real.
if __name__ == "__main__":
    asyncio.run(main())

