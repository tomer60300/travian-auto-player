"""One-shot tile-details probe for diagnosing why a specific tile
isn't appearing in scan results.

Uses the recon account's credentials from .env (TRAVIAN_RECON_*)
to log in fresh — the cached JWT alone isn't enough because
Travian also needs the session cookies set during the full login
handshake. The fresh login creates a parallel session to the
live :8002 server's recon; Travian will invalidate whichever
session does its next action last, so run this BEFORE you start
another scan via the UI to be safe.

Usage::

    python -m scripts.probe_tile 17 96

Prints the parsed tile attributes plus the player's profile
capital_id for cross-checking against the non-capitals filter.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


SERVER_URL = "https://ts2.x1.europe.travian.com"


async def main() -> None:
    if len(sys.argv) < 3:
        print("usage: probe_tile.py X Y", file=sys.stderr)
        raise SystemExit(2)
    x = int(sys.argv[1])
    y = int(sys.argv[2])

    from travian_api.clients.http_client import HttpClient
    from travian_api.config import Settings
    from travian_api.services.auth_service import AuthService
    from travian_api.services.auto_scout_service import AutoScoutService

    settings_root = Settings()
    if not (settings_root.recon_username and settings_root.recon_password):
        raise SystemExit(
            "TRAVIAN_RECON_USERNAME / TRAVIAN_RECON_PASSWORD missing in "
            ".env — can't probe without recon credentials."
        )

    # Throwaway data dir so we don't disturb the live recon session.
    probe_dir = (
        Path(tempfile.gettempdir())
        / "travian_web_sessions" / "_probe"
    )
    probe_dir.mkdir(parents=True, exist_ok=True)
    settings = settings_root.model_copy(
        update={
            "base_url": SERVER_URL,
            "username": settings_root.recon_username,
            "password": settings_root.recon_password,
            "jwt_cache_file": str(probe_dir / "jwt_cache.json"),
            "jwt_cache_path": str(probe_dir / "jwt_cache.json"),
        }
    )

    client = HttpClient(settings, cookie_file=probe_dir / "cookies.json")
    auth = AuthService(client, settings)
    print(f"[probe] logging in as recon user '{settings_root.recon_username}'...",
          file=sys.stderr)
    try:
        await auth.login()
    except Exception as exc:
        raise SystemExit(f"Recon login failed: {exc!r}")

    print(f"[probe] fetching tile-details for ({x}, {y})...", file=sys.stderr)
    try:
        resp: dict[str, Any] = await client.post_json(
            "/api/v1/map/tile-details", {"x": x, "y": y}, request_type="xhr"
        )
    except Exception as exc:
        await client.close()
        raise SystemExit(f"Tile fetch failed: {exc!r}")

    html = resp.get("html", "")
    if not html:
        await client.close()
        print(resp)
        raise SystemExit("Empty HTML — session likely expired.")

    svc = AutoScoutService(client)
    info = svc._parse_tile_details(x, y, html)

    print("\n=== parsed tile info ===")
    print(f"  coords:           ({info.x}, {info.y})")
    print(f"  village_id (did): {info.village_id}")
    print(f"  player_id:        {info.player_id}")
    print(f"  player_name:      {info.player_name!r}")
    print(f"  alliance_id:      {info.alliance_id}")
    print(f"  alliance_name:    {info.alliance_name!r}")
    print(f"  village_name:     {info.village_name!r}")
    print(f"  tribe:            {info.tribe!r}")
    print(f"  population:       {info.population}")
    print(f"  is_oasis:         {info.is_oasis}")
    print(f"  is_abandoned:     {info.is_abandoned}")
    print(f"  bonus:            {info.bonus!r}")
    print(f"  bonus_breakdown:  {info.bonus_breakdown}")
    print(f"  oasis_owner_x/y:  ({info.oasis_owner_x}, {info.oasis_owner_y})")

    if info.player_id:
        print(f"\n[probe] fetching profile for player_id={info.player_id}...",
              file=sys.stderr)
        profile_info = await svc.get_player_profile_info(info.player_id)
        print(f"  profile pop:        {profile_info.get('pop')}")
        print(f"  profile capital_id: {profile_info.get('capital_id')}")
        if profile_info.get("capital_id") == info.village_id:
            print(
                f"\n  >>> CONCLUSION: village_id {info.village_id} IS the "
                f"player's capital. Non-capitals filter drops this tile."
            )
        else:
            print(
                f"\n  >>> CONCLUSION: capital is village_id "
                f"{profile_info.get('capital_id')}, this tile is "
                f"village_id {info.village_id} — should NOT be dropped "
                f"by the non-capitals filter."
            )

    try:
        await client.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
