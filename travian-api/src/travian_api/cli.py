"""Command Line Interface for Travian API."""
from __future__ import annotations

import asyncio
import sys
import io
from typing import Any, Dict, Optional, List

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
from .services.build_queue_service import BuildQueueService, BuildPlan
from .services.military_service import MilitaryService
from .services.reports_service import ReportsService
from .services.target_resolver import TargetResolver
from .services.video_reward_service import VideoRewardService, REWARD_TYPES

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


def _settings(interactive: bool = True) -> Settings:
    """Build settings from .env + CLI overrides. Prompts for missing values if interactive."""
    s = Settings()
    if _server_override:
        s.base_url = _server_override.rstrip("/")
    if _username_override:
        s.username = _username_override
    if _password_override:
        s.password = _password_override
    
    # Check what's missing
    missing_server = not s.base_url
    missing_username = not s.username
    missing_password = not s.password
    
    if not any([missing_server, missing_username, missing_password]):
        return s
    
    # If interactive (TTY), prompt for missing values
    if interactive and sys.stdin.isatty():
        if missing_server:
            s.base_url = typer.prompt("Server URL (e.g. https://ts1.x1.europe.travian.com)").rstrip("/")
        if missing_username:
            s.username = typer.prompt("Username/email")
        if missing_password:
            s.password = typer.prompt("Password", hide_input=True)
        
        # Offer to save to .env
        if any([missing_server, missing_username, missing_password]):
            save = typer.confirm("Save credentials to .env?", default=True)
            if save:
                _save_env(s)
    else:
        # Non-interactive: fail with guidance
        missing = []
        if missing_server:
            missing.append("server (--server or TRAVIAN_BASE_URL)")
        if missing_username:
            missing.append("username (--username or TRAVIAN_USERNAME)")
        if missing_password:
            missing.append("password (--password or TRAVIAN_PASSWORD)")
        console.print(f"[red]Missing required auth config:[/red]")
        for m in missing:
            console.print(f"  - {m}")
        console.print("\nSet in .env file or pass as CLI options.")
        raise typer.Exit(1)
    
    return s


def _save_env(s: Settings) -> None:
    """Save settings to .env file."""
    from pathlib import Path
    env_path = Path(".env")
    
    # Read existing .env if it exists (preserve other settings)
    existing = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                existing[key.strip()] = val.strip()
    
    # Update with new values
    existing["TRAVIAN_BASE_URL"] = s.base_url
    existing["TRAVIAN_USERNAME"] = s.username
    existing["TRAVIAN_PASSWORD"] = s.password
    
    # Write back
    lines = [f"{k}={v}" for k, v in existing.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]Saved to {env_path.resolve()}[/green]")


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
            if result.villages:
                console.print(f"  Villages ({len(result.villages)}):")
                for v in result.villages:
                    main_tag = " [cyan](main)[/cyan]" if v.is_main_village else ""
                    console.print(f"    {v.id}  {v.name}  ({v.x}|{v.y}){main_tag}")
            else:
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


# ── Village ──────────────────────────────────────────────────────────
village_app = typer.Typer(name="village", help="Village management commands")
app.add_typer(village_app)


@village_app.command("list")
def village_list():
    """List all player villages."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            result = await auth.login()

            if not result.villages:
                console.print("No village data available")
                return

            table = Table(title="Villages")
            table.add_column("ID", style="cyan", justify="right")
            table.add_column("Name", style="green")
            table.add_column("X", justify="right")
            table.add_column("Y", justify="right")
            table.add_column("Main", justify="center")

            for v in result.villages:
                main = "[cyan]yes[/cyan]" if v.is_main_village else ""
                table.add_row(str(v.id), v.name, str(v.x), str(v.y), main)

            console.print(table)
    _run(_do())


@village_app.command("switch")
def village_switch(
    village_id: int = typer.Argument(..., help="Village ID to switch to"),
):
    """Switch active village context (via newdid)."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            await client.get_html(f"/dorf1.php?newdid={village_id}")
            console.print(f"[green]Switched to village {village_id}[/green]")
    _run(_do())


# ── Building ─────────────────────────────────────────────────────────
building_app = typer.Typer(name="building", help="Building management commands")
app.add_typer(building_app)


@building_app.command("list")
def building_list(
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Village ID (default: current village)"),
):
    """List all village buildings."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)
            buildings = await bs.get_village_buildings(village_id=village_id)

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
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Village ID (default: current village)"),
):
    """Upgrade a building by slot ID. Refuses if queue occupied (would cost gold) unless --allow-gold."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)
            result = await bs.upgrade_building(slot_id, allow_gold=allow_gold, village_id=village_id)
            if result.success:
                console.print(f"[green]✓ Upgrade started![/green]")
                console.print(f"  {result.building_name}: Level {result.old_level} → {result.new_level}")
                console.print(f"  Construction time: {result.construction_time}")
            else:
                console.print(f"[red]✗ Upgrade failed[/red]")
                console.print(f"  {result.raw_response[:200] if result.raw_response else 'Unknown error'}")
    _run(_do())


@building_app.command("resources")
def building_resources(
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Village ID (default: current village)"),
):
    """Show current village resources."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)
            res = await bs.get_resources(village_id=village_id)
            console.print(f"  Lumber: [yellow]{res.lumber:>6}[/yellow] / {res.max_lumber}")
            console.print(f"  Clay:   [yellow]{res.clay:>6}[/yellow] / {res.max_clay}")
            console.print(f"  Iron:   [yellow]{res.iron:>6}[/yellow] / {res.max_iron}")
            console.print(f"  Crop:   [yellow]{res.crop:>6}[/yellow] / {res.max_crop}")
            console.print(f"  Free crop: {res.free_crop}")
    _run(_do())


@building_app.command("queue")
def building_queue(
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Village ID (default: current village)"),
):
    """Show construction queue."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)
            queue = await bs.get_construction_queue(village_id=village_id)
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


# -- Build Queue ---------------------------------------------------------------
queue_app = typer.Typer(name="queue", help="Priority build queue commands")
app.add_typer(queue_app)


@queue_app.command("run")
def queue_run(
    plan_file: str = typer.Argument(..., help="Path to YAML build plan file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show what would be built"),
    poll: int = typer.Option(30, "--poll", help="Poll interval in seconds"),
    use_video: bool = typer.Option(False, "--use-video", help="Claim buildingUpgrade video reward after each upgrade (~33s extra)"),
):
    """Execute a build plan in priority order. Waits for resources and empty queue."""
    async def _do():
        s = _settings()
        plan = BuildPlan.from_file(plan_file)
        console.print(f"Loaded plan: village {plan.village_id}, {len(plan.items)} items")
        for item in sorted(plan.items, key=lambda x: x.priority):
            label = f"slot {item.slot}" if item.slot else item.building
            console.print(f"  P{item.priority}: {label} -> Lv{item.target}")

        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bqs = BuildQueueService(client)
            bqs.on_status(lambda msg: console.print(f"  {msg}"))

            if dry_run:
                results = await bqs.execute_plan(plan, poll_interval_s=poll, dry_run=True)
            else:
                results = await bqs.execute_plan_continuous(plan, poll_interval_s=poll, use_video=use_video)

            console.print(f"\n[bold]Results:[/bold]")
            for r in results:
                status = r.get('status', '?')
                color = 'green' if status == 'started' else 'yellow' if status == 'dry_run' else 'red'
                console.print(f"  [{color}]{r['building']} {r.get('level','')} - {status}[/{color}]"
                              f"  {r.get('time','')}")
    _run(_do())


@queue_app.command("validate")
def queue_validate(
    plan_file: str = typer.Argument(..., help="Path to YAML build plan file"),
):
    """Validate a build plan file and check current building levels."""
    async def _do():
        s = _settings()
        plan = BuildPlan.from_file(plan_file)
        console.print(f"Village: {plan.village_id}")
        console.print(f"Items: {len(plan.items)}")

        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bqs = BuildQueueService(client)
            bqs.on_status(lambda msg: console.print(f"  {msg}"))
            await bqs.resolve_slots(plan)

            # Show skipped items separately
            skipped = [i for i in plan.items if i.status == "skipped"]
            active = [i for i in plan.items if i.status != "skipped"]

            if skipped:
                console.print(f"\n[red]Skipped ({len(skipped)}):[/red]")
                for i in skipped:
                    reason = f"expect '{i.expect}' mismatch" if i.expect else "not found"
                    console.print(f"  [red]X[/red] slot={i.slot_id} target={i.target} ({reason})")

            console.print(f"\nBuild plan ({len(active)} items):")
            for p in sorted(set(i.priority for i in active)):
                items = [i for i in active if i.priority == p]
                if not items:
                    continue
                console.print(f"\n  Priority {p}:")
                for i in items:
                    status_color = 'green' if i.status == 'done' else 'yellow'
                    console.print(f"    [{status_color}]{i.building}[/{status_color}]"
                                  f" slot={i.slot_id} Lv{i.current_level}->{i.target} [{i.status}]")
    _run(_do())


# ── Video Rewards ─────────────────────────────────────────────────────
video_app = typer.Typer(name="video", help="Video reward commands")
app.add_typer(video_app)

PRODUCTION_REWARDS = [
    "lumberProductionBonus",
    "clayProductionBonus",
    "ironProductionBonus",
    "cropProductionBonus",
]


@video_app.command("available")
def video_available():
    """Check which video rewards are currently available."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            vrs = VideoRewardService(client)
            try:
                rewards = await vrs.get_available_rewards()
            finally:
                await vrs.close()

            if not rewards:
                console.print("[yellow]No reward data returned[/yellow]")
                return

            table = Table(title="Video Rewards")
            table.add_column("Reward", style="cyan")
            table.add_column("Available", justify="center")
            table.add_column("Active", justify="center")

            for key in PRODUCTION_REWARDS:
                available = rewards.get(key, False)
                active = rewards.get(key.replace("ProductionBonus", "_active"), False)
                avail_str = "[green]yes[/green]" if available else "[red]no[/red]"
                active_str = "[yellow]active[/yellow]" if active else "-"
                table.add_row(key, avail_str, active_str)

            console.print(table)
    _run(_do())


@video_app.command("claim")
def video_claim(
    reward_type: str = typer.Argument(..., help=f"Reward type: {', '.join(REWARD_TYPES.keys())}"),
    village_id: Optional[int] = typer.Option(None, "--village-id", help="Village ID (for buildingUpgrade)"),
    slot_id: Optional[int] = typer.Option(None, "--slot-id", help="Slot ID (for buildingUpgrade)"),
    building_id: Optional[int] = typer.Option(None, "--building-id", help="Building ID (for buildingUpgrade)"),
):
    """Claim a video reward. Takes ~33 seconds due to ATG timing requirements."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            vrs = VideoRewardService(client)
            try:
                extra: Dict[str, Any] = {}
                if reward_type == "buildingUpgrade":
                    if not all([village_id, slot_id, building_id]):
                        console.print("[red]buildingUpgrade requires --village-id, --slot-id, --building-id[/red]")
                        raise typer.Exit(1)
                    extra = {"villageId": village_id, "slotId": slot_id, "buildingId": building_id}

                console.print(f"Claiming [cyan]{reward_type}[/cyan] — this takes ~33 seconds...")
                from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("{task.completed}/{task.total}s"),
                    console=console,
                ) as progress:
                    task = progress.add_task("Simulating video", total=33)
                    # Run claim in background, update progress in parallel
                    claim_task = asyncio.ensure_future(vrs.claim_reward(reward_type, **extra))
                    for i in range(33):
                        if claim_task.done():
                            progress.update(task, completed=33)
                            break
                        await asyncio.sleep(1)
                        progress.update(task, advance=1)
                    result = await claim_task

                if result.success:
                    console.print(f"[green]✓ {result.message}[/green]")
                else:
                    console.print(f"[red]✗ {result.message}[/red]")
                    if result.raw:
                        console.print(f"  Raw: {result.raw[:200]}")
            finally:
                await vrs.close()
    _run(_do())


@video_app.command("claim-all")
def video_claim_all():
    """Claim all available production boost rewards in sequence."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            vrs = VideoRewardService(client)
            try:
                rewards = await vrs.get_available_rewards()
                available = [r for r in PRODUCTION_REWARDS if rewards.get(r, False)]

                if not available:
                    console.print("[yellow]No production rewards available[/yellow]")
                    return

                console.print(f"Found {len(available)} available reward(s): {', '.join(available)}")
                for i, rtype in enumerate(available, 1):
                    console.print(f"\n[{i}/{len(available)}] Claiming [cyan]{rtype}[/cyan] (~33s)...")
                    result = await vrs.claim_reward(rtype)
                    if result.success:
                        console.print(f"  [green]✓ {result.message}[/green]")
                    else:
                        console.print(f"  [red]✗ {result.message}[/red]")

                console.print(f"\n[bold]Done — processed {len(available)} rewards[/bold]")
            finally:
                await vrs.close()
    _run(_do())


def main():
    app()
