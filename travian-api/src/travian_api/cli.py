"""Command Line Interface for Travian API."""
from __future__ import annotations

import asyncio
import sys
import io
from typing import Optional, List

# Force UTF-8 output on Windows so non-ASCII village names display correctly
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import typer
from rich.console import Console
from rich.table import Table

from .config import Settings
from .clients.http_client import HttpClient
from .services.auth_service import AuthService
from .services.building_service import BuildingService
from .services.military_service import MilitaryService
from .services.reports_service import ReportsService
from .services.target_resolver import TargetResolver

app = typer.Typer(name="travian", help="Travian Legends API - Game automation library and CLI", add_completion=False)
console = Console(highlight=False)

# Global overrides (set by top-level options)
_server_override: str = ""
_username_override: str = ""
_password_override: str = ""


@app.callback()
def main_callback(
    server: Optional[str] = typer.Option(None, "--server", "-s", help="Game server URL", envvar="TRAVIAN_BASE_URL"),
    username: Optional[str] = typer.Option(None, "--username", "-u", help="Account username/email"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="Account password"),
):
    """Global options applied before any command."""
    global _server_override, _username_override, _password_override
    if server:
        _server_override = server
    if username:
        _username_override = username
    if password:
        _password_override = password


def _settings() -> Settings:
    """Build settings from .env + CLI overrides."""
    s = Settings()
    if _server_override:
        s.base_url = _server_override.rstrip("/")
    if _username_override:
        s.username = _username_override
    if _password_override:
        s.password = _password_override
    
    # Validate all three auth params are present
    missing = []
    if not s.base_url:
        missing.append("server (--server or TRAVIAN_BASE_URL)")
    if not s.username:
        missing.append("username (--username or TRAVIAN_USERNAME)")
    if not s.password:
        missing.append("password (--password or TRAVIAN_PASSWORD)")
    if missing:
        console.print(f"[red]Missing required auth config:[/red]")
        for m in missing:
            console.print(f"  - {m}")
        console.print("\nSet in .env file or pass as CLI options.")
        raise typer.Exit(1)
    
    return s


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# ── Auth ─────────────────────────────────────────────────────────────
auth_app = typer.Typer(name="auth", help="Authentication commands")
app.add_typer(auth_app)


@auth_app.command("login")
def auth_login():
    """Login to Travian server and show player info."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            result = await auth.login()
            console.print("[green]OK - Logged in![/green]")
            console.print(f"  Player: {result.player_name}")
            console.print(f"  Tribe:  {result.tribe_id} (1=Roman, 2=Teuton, 3=Gaul)")
            console.print(f"  Village: {result.village_id}")
    _run(_do())


@auth_app.command("token")
def auth_token():
    """Print current JWT token."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            jwt = await auth.get_jwt()
            if jwt:
                console.print(jwt)
            else:
                console.print("[red]No JWT available[/red]")
    _run(_do())


# ── Building ─────────────────────────────────────────────────────────
building_app = typer.Typer(name="building", help="Building management commands")
app.add_typer(building_app)


@building_app.command("list")
def building_list():
    """List all village buildings."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)
            buildings = await bs.get_village_buildings()

            table = Table(title="Village Buildings")
            table.add_column("Slot", style="cyan", justify="right")
            table.add_column("Name", style="green")
            table.add_column("GID", justify="right")
            table.add_column("Level", justify="right", style="yellow")

            for b in sorted(buildings, key=lambda x: x.slot_id):
                table.add_row(str(b.slot_id), b.name, str(b.gid), str(b.level))

            console.print(table)
    _run(_do())


@building_app.command("upgrade")
def building_upgrade(
    slot_id: int = typer.Option(..., "--slot-id", "-s", help="Building slot ID (1-40)"),
    allow_gold: bool = typer.Option(False, "--allow-gold", help="Allow spending gold (master builder). Default: REFUSE if queue occupied."),
):
    """Upgrade a building by slot ID. Refuses if queue occupied (would cost gold) unless --allow-gold."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)
            result = await bs.upgrade_building(slot_id, allow_gold=allow_gold)
            if result.success:
                console.print(f"[green]✓ Upgrade started![/green]")
                console.print(f"  {result.building_name}: Level {result.old_level} → {result.new_level}")
                console.print(f"  Construction time: {result.construction_time}")
            else:
                console.print(f"[red]✗ Upgrade failed[/red]")
                console.print(f"  {result.raw_response[:200] if result.raw_response else 'Unknown error'}")
    _run(_do())


@building_app.command("resources")
def building_resources():
    """Show current village resources."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)
            res = await bs.get_resources()
            console.print(f"  Lumber: [yellow]{res.lumber:>6}[/yellow] / {res.max_lumber}")
            console.print(f"  Clay:   [yellow]{res.clay:>6}[/yellow] / {res.max_clay}")
            console.print(f"  Iron:   [yellow]{res.iron:>6}[/yellow] / {res.max_iron}")
            console.print(f"  Crop:   [yellow]{res.crop:>6}[/yellow] / {res.max_crop}")
            console.print(f"  Free crop: {res.free_crop}")
    _run(_do())


@building_app.command("queue")
def building_queue():
    """Show construction queue."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)
            queue = await bs.get_construction_queue()
            if not queue:
                console.print("Queue is empty")
            else:
                for q in queue:
                    console.print(f"  {q.building_name} → Level {q.target_level}  ({q.remaining_seconds}s remaining)")
    _run(_do())


# ── Military ─────────────────────────────────────────────────────────
military_app = typer.Typer(name="military", help="Military operation commands")
app.add_typer(military_app)


@military_app.command("scout")
def military_scout(
    x: int = typer.Option(..., "--x", help="Target X coordinate"),
    y: int = typer.Option(..., "--y", help="Target Y coordinate"),
    amount: int = typer.Option(1, "--amount", "-n", help="Number of scouts to send"),
    scout_type: str = typer.Option("resources", "--type", "-t", help="Scout type: resources or defenses"),
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Source village ID"),
):
    """Send scouts to target coordinates."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            state = await auth.login()
            resolver = TargetResolver(client)
            ms = MilitaryService(client, resolver)
            result = await ms.send_scouts(x=x, y=y, amount=amount, scout_type=scout_type, village_id=village_id)
            if result.success:
                console.print(f"[green]✓ Scouts sent![/green]")
                console.print(f"  Target: ({result.target_x}, {result.target_y}) {result.target_name}")
                console.print(f"  Troops: {result.troops_sent}")
                if result.travel_time:
                    console.print(f"  Travel time: {result.travel_time}")
            else:
                console.print(f"[red]✗ Scout failed: {result.raw_response}[/red]")
    _run(_do())


@military_app.command("raid")
def military_raid(
    x: int = typer.Option(..., "--x", help="Target X coordinate"),
    y: int = typer.Option(..., "--y", help="Target Y coordinate"),
    troop: List[str] = typer.Option([], "--troop", "-t", help="Troop spec: t1=50"),
):
    """Send a raid to target coordinates."""
    # Parse troop specs
    troops = {}
    for spec in troop:
        parts = spec.split("=")
        if len(parts) == 2:
            troops[parts[0]] = int(parts[1])

    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            state = await auth.login()
            resolver = TargetResolver(client)
            ms = MilitaryService(client, resolver)
            result = await ms.send_raid(x=x, y=y, troops=troops)
            if result.success:
                console.print(f"[green]✓ Raid sent![/green]")
                console.print(f"  Target: ({result.target_x}, {result.target_y}) {result.target_name}")
                console.print(f"  Troops: {result.troops_sent}")
            else:
                console.print(f"[red]✗ Raid failed: {result.raw_response}[/red]")
    _run(_do())


# ── Reports ──────────────────────────────────────────────────────────
reports_app = typer.Typer(name="reports", help="Reports management commands")
app.add_typer(reports_app)


@reports_app.command("list")
def reports_list(
    max_age_hours: int = typer.Option(24, "--max-age-hours", help="Max report age in hours"),
    max_pages: int = typer.Option(5, "--max-pages", help="Max pages to fetch"),
):
    """List recent reports."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            rs = ReportsService(client)
            reports = await rs.fetch_reports(max_age_hours=max_age_hours, max_pages=max_pages)

            if not reports:
                console.print("No reports found")
                return

            table = Table(title=f"Reports ({len(reports)} found)")
            table.add_column("ID", style="cyan")
            table.add_column("Type", style="green")
            table.add_column("Subject")
            table.add_column("Date")
            table.add_column("Read")

            for r in reports:
                read = "yes" if r.is_read else "[red]no[/red]"
                safe_subject = r.subject[:50].encode('ascii', errors='replace').decode('ascii')
                table.add_row(r.report_id, r.report_type, safe_subject, r.date_str, read)

            console.print(table)
    _run(_do())


@reports_app.command("show")
def reports_show(
    report_id: str = typer.Argument(..., help="Report ID"),
):
    """Show detailed report content."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            rs = ReportsService(client)
            detail = await rs.fetch_report_detail(report_id)
            rtype = detail.get('type', 'unknown')
            data = detail.get('data')

            if rtype == 'scout' and data:
                d = data if isinstance(data, dict) else data.model_dump()
                target = d.get('target', {})
                res = d.get('resources', {})
                steal = d.get('stealable_resources', {})
                troops = {k: v for k, v in d.get('troops', {}).items() if v}
                console.print(f"[bold cyan]Scout Report[/bold cyan]  ID: {report_id}")
                console.print(f"  Target village: [yellow]{target.get('village_name','')}[/yellow]"
                              f"  (id={target.get('village_id',0)})")
                if target.get('coordinates', {}).get('x'):
                    c = target['coordinates']
                    console.print(f"  Coords: ({c['x']}|{c['y']})")
                console.print(f"\n  [green]Resources at target:[/green]")
                console.print(f"    Lumber: {res.get('lumber',0):>6}  Clay: {res.get('clay',0):>6}"
                              f"  Iron: {res.get('iron',0):>6}  Crop: {res.get('crop',0):>6}")
                raidable = steal.get('raidable', steal.get('lumber', 0) + steal.get('clay', 0) +
                                     steal.get('iron', 0) + steal.get('crop', 0))
                cranny = steal.get('cranny', 0)
                console.print(f"    Cranny: {cranny}  →  Raidable: [bold green]{raidable}[/bold green]")
                if troops:
                    console.print(f"\n  [red]Defender troops:[/red] {troops}")
                else:
                    console.print(f"\n  Defenders: none visible")

            elif rtype == 'battle' and data:
                d = data if isinstance(data, dict) else data.model_dump()
                atk = d.get('attacker', {})
                dfn = d.get('defender', {})
                atk_t = {k: v for k, v in d.get('attacker_troops', {}).items() if v}
                dfn_t = {k: v for k, v in d.get('defender_troops', {}).items() if v}
                bounty = d.get('bounty', {})
                console.print(f"[bold red]Battle Report[/bold red]  ID: {report_id}")
                console.print(f"  Attacker: [yellow]{atk.get('village_name','')}[/yellow]  Troops: {atk_t}")
                console.print(f"  Defender: [yellow]{dfn.get('village_name','')}[/yellow]  Troops: {dfn_t}")
                console.print(f"  Result: {d.get('battle_result','?')}")
                if any(bounty.values()):
                    console.print(f"  Bounty: L={bounty.get('lumber',0)} C={bounty.get('clay',0)}"
                                  f" I={bounty.get('iron',0)} Cr={bounty.get('crop',0)}")
            else:
                console.print(f"Type: {rtype}")
                console.print(detail)
    _run(_do())


def main():
    app()
