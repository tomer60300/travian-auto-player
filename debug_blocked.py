"""Debug: find where 'blocked' appears in Travian responses."""
import asyncio
import re
import sys

sys.path.insert(0, "src")

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings
from travian_api.services.auth_service import AuthService


async def main():
    s = Settings(stealth=False)
    c = HttpClient(s)
    auth = AuthService(c, s)
    result = await auth.login()
    print(f"Logged in as: {result}")

    pages = [
        "/dorf1.php",
        "/dorf2.php",
        "/build.php?id=1",   # woodcutter (upgrading)
        "/build.php?id=3",   # woodcutter
        "/build.php?id=26",  # main building
        "/build.php?id=30",  # warehouse (upgrading)
    ]

    suspicious_words = ["blocked", "captcha", "recaptcha", "bot-detection", "banned", "suspicious"]

    for page in pages:
        html = await c.get_html(page)
        low = html.lower()
        found_any = False
        for word in suspicious_words:
            if word in low:
                found_any = True
                for m in re.finditer(rf".{{0,80}}{word}.{{0,80}}", low):
                    print(f"\n[{page}] '{word}' found:")
                    print(f"  ...{m.group()}...")
        if not found_any:
            print(f"[{page}] CLEAN ({len(html)} chars)")

    # Also check the upgrade POST response format
    # The upgrade goes through post_form or post_json - let's check what build.php returns
    # when you submit an upgrade
    html = await c.get_html("/build.php?id=3")
    # Check for the upgrade form/button
    if "build.php" in html and "action" in html.lower():
        print(f"\n[build.php?id=3] Has form/action elements")

    await c.close()


asyncio.run(main())
