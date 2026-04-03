"""Debug: find the current game version/gpack from live HTML."""
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
    await auth.login()
    html = await c.get_html("/dorf1.php")

    # Find gpack references
    for m in re.finditer(r"gpack/[^\"' ]+", html):
        print("gpack ref:", m.group())

    # Find version numbers
    for m in re.finditer(r"['\"]version['\"]:\s*['\"]([^'\"]+)", html):
        print("version:", m.group(1))

    # Find X-Version in meta/script
    for m in re.finditer(r"x.?version[^;]{0,80}", html, re.IGNORECASE):
        print("x-version:", m.group())

    # Find any build/version identifiers
    for m in re.finditer(r"build[_-]?(?:version|number|id)[^;]{0,60}", html, re.IGNORECASE):
        print("build:", m.group())

    # Check script src for version numbers
    for m in re.finditer(r'src="([^"]*\d+\.\d+[^"]*)"', html):
        print("script src:", m.group(1)[:100])

    await c.close()


asyncio.run(main())
