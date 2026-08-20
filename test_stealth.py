"""Quick test of stealth integration."""
import asyncio
from travian_api.clients.http_client import HttpClient
from travian_api.config import get_settings

async def main():
    s = get_settings()
    print(f"stealth={s.stealth}")
    print(f"stealth_speed={s.stealth_speed}")
    print(f"stealth_navigate={s.stealth_navigate}")
    
    c = HttpClient(s)
    
    # Check UA is realistic (not 'Travian-API/1.0')
    ua = c.client.headers.get("user-agent", "MISSING")
    assert "Travian-API" not in ua, f"UA still shows bot string: {ua}"
    print(f"UA: {ua[:70]}...")
    
    # Check stealth components
    print(f"navigator enabled: {c.navigator.enabled}")
    print(f"delay enabled: {c.delay.enabled}")
    print(f"throttler enabled: {c.throttler.enabled}")
    
    # Test that delay actually works
    from travian_api.stealth.human_delay import ActionType
    waited = await c.delay.wait(ActionType.CLICK, "test click")
    print(f"Test click delay: {waited:.2f}s")
    
    # Test login still works with stealth
    from travian_api.services.auth_service import AuthService
    auth = AuthService(c)
    result = await auth.login()
    print(f"Login OK: player={result.get('name', '?')}")
    
    await c.close()
    print("\nAll stealth integration tests passed!")

# Guarded so importing this module cannot run it. These are live integration
# scripts, not unit tests: main() logs in to the real server with the
# credentials in .env and spends real requests. pytest's testpaths is
# "tests", so a bare `pytest` never collected them -- but `pytest
# test_stealth_upgrade.py`, or an editor's "run this file", imported the
# module and did it for real.
if __name__ == "__main__":
    asyncio.run(main())

