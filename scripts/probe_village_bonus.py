"""Manual check-up: resolve a map tile through the configured background
proxy account and report capital status + aggregated oasis bonus, exercising
the villages-by-oasis-bonus code path end to end.

ALL requests dispatch from the recon/proxy account configured in .env
(``TRAVIAN_RECON_USERNAME`` / ``TRAVIAN_RECON_PASSWORD``) — never from a
user's primary account. Read-only: tile-details + the owner's profile + one
tile-details per occupied oasis. No scouts sent, no writes.

Usage:
    python -m scripts.probe_village_bonus X Y [SERVER_URL]

SERVER_URL defaults to $TRAVIAN_BASE_URL, then to the europe ts2 world.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_SERVER = "https://ts2.x1.europe.travian.com"


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) < 3:
        print("usage: probe_village_bonus.py X Y [SERVER_URL]", file=sys.stderr)
        raise SystemExit(2)
    x, y = int(sys.argv[1]), int(sys.argv[2])

    from travian_api.clients.http_client import HttpClient
    from travian_api.config import Settings
    from travian_api.services.auth_service import AuthService
    from travian_api.services.auto_scout_service import (
        AutoScoutService,
        _format_bonus_breakdown,
    )

    root = Settings()
    if not (root.recon_username and root.recon_password):
        raise SystemExit(
            "No background proxy configured. Set TRAVIAN_RECON_USERNAME / "
            "TRAVIAN_RECON_PASSWORD in .env first."
        )
    server_url = (
        sys.argv[3] if len(sys.argv) > 3 else (root.base_url or os.getenv("TRAVIAN_BASE_URL") or DEFAULT_SERVER)
    ).rstrip("/")

    # Throwaway session dir so the probe never disturbs the live recon session.
    probe_dir = Path(tempfile.gettempdir()) / "travian_web_sessions" / "_probe_proxy"
    probe_dir.mkdir(parents=True, exist_ok=True)
    settings = root.model_copy(
        update={
            "base_url": server_url,
            "username": root.recon_username,
            "password": root.recon_password,
            "jwt_cache_file": str(probe_dir / "jwt_cache.json"),
            "jwt_cache_path": str(probe_dir / "jwt_cache.json"),
        }
    )
    client = HttpClient(settings, cookie_file=probe_dir / "cookies.json")
    auth = AuthService(client, settings)
    print(f"[probe] proxy = {root.recon_username} @ {server_url}", file=sys.stderr)
    print("[probe] logging in as proxy account…", file=sys.stderr)
    try:
        await auth.login()
    except Exception as exc:
        raise SystemExit(f"Proxy login failed: {exc!r}")

    svc = AutoScoutService(client)

    # ── 1. tile-details for the target coordinate ────────────────────
    print(f"[probe] fetching tile-details for ({x}, {y})…", file=sys.stderr)
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
        raise SystemExit(f"Empty tile HTML (session issue?). resp={resp}")
    info = svc._parse_tile_details(x, y, html)

    print(f"\n=== TILE {x}|{y} — parsed details ===")
    print(f"  coords:           ({info.x}, {info.y})")
    print(f"  village_id (did): {info.village_id}")
    print(f"  village_name:     {info.village_name!r}")
    print(f"  player_id:        {info.player_id}")
    print(f"  player_name:      {info.player_name!r}")
    print(f"  alliance:         {info.alliance_name!r} (id={info.alliance_id})")
    print(f"  tribe:            {info.tribe!r}")
    print(f"  population:       {info.population}")
    print(f"  is_oasis:         {info.is_oasis}")
    print(f"  is_abandoned:     {info.is_abandoned}")
    if info.is_oasis:
        print(f"  oasis bonus:      {info.bonus!r}  breakdown={info.bonus_breakdown}")
        print(f"  oasis owner xy:   ({info.oasis_owner_x}, {info.oasis_owner_y})")

    # ── 2. Capital + oasis-bonus via the owner's single profile fetch ─
    if not info.player_id:
        print("\n  >>> No owner — unoccupied/abandoned tile. Nothing more to resolve.")
        await client.close()
        return

    print(f"\n[probe] fetching profile for player_id={info.player_id} (one fetch)…", file=sys.stderr)
    profile = await svc.get_player_profile_info(info.player_id)
    capital_id = profile.get("capital_id")
    villages = profile.get("villages") or []
    print("\n=== OWNER PROFILE (single fetch -> capital + oases) ===")
    print(f"  player total pop:    {profile.get('pop')}")
    print(f"  capital_id:          {capital_id}")
    print(f"  villages on profile: {len(villages)}")

    is_capital = capital_id == info.village_id
    print(
        f"\n  >>> CAPITAL? this tile (village_id={info.village_id}) "
        f"{'IS' if is_capital else 'is NOT'} the player's capital "
        f"(capital_id={capital_id})."
    )

    this_village = next((v for v in villages if v.get("village_id") == info.village_id), None)
    if this_village is None:
        print("\n  >>> Village not in owner's profile array (layout/locale change?) — can't aggregate.")
        await client.close()
        return

    n_oases = len(set(this_village.get("oases", [])))
    print(f"\n[probe] aggregating oasis bonus across {n_oases} occupied oasis tile(s)…", file=sys.stderr)
    agg = await svc.aggregate_village_oasis_bonuses([this_village])
    breakdown = agg.get(info.village_id, {})
    total = sum(breakdown.values())
    print("\n=== AGGREGATED VILLAGE OASIS BONUS ===")
    print(f"  occupied oases:    {n_oases}")
    print(f"  per-resource:      {breakdown}")
    print(f"  human-readable:    {_format_bonus_breakdown(breakdown)!r}")
    print(f"  TOTAL bonus:       {total}%")
    print("\n=== VERDICT (combined non-capital + oasis-bonus filter) ===")
    print(f"  non-capital:       {not is_capital}")
    print(f"  total oasis bonus: {total}%")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
