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
from .services.farm_list_service import FarmListService
from .services.auto_scout_service import AutoScoutService
from .constants import BUILDING_NAMES

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
    """Run an async coroutine synchronously.

    Installs signal handlers so that SIGTERM (from TaskStop / kill) is
    converted into a KeyboardInterrupt, which propagates through the
    async stack and triggers the 'finally' cleanup in queue_run.
    Without this, killing the bash wrapper leaves the Python child
    process running as a zombie.

    On Windows, also starts a background thread that monitors the parent
    process — if the parent (bash wrapper) dies, we self-terminate.
    This covers the case where SIGTERM is not delivered to the child.
    """
    import signal
    import sys
    import os
    import threading

    def _sigterm_handler(signum, frame):
        """Convert SIGTERM into KeyboardInterrupt so cleanup runs."""
        raise KeyboardInterrupt(f"Received signal {signum}")

    # SIGTERM on Unix/Windows-Git-Bash, SIGBREAK on native Windows
    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        signal.signal(signal.SIGBREAK, _sigterm_handler)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass  # SIGBREAK only exists on Windows

    # Windows orphan prevention: monitor parent process.
    # If bash wrapper is killed, Python child won't get SIGTERM on Windows.
    # This daemon thread polls the parent PID and exits if it disappears.
    if sys.platform == "win32":
        _parent_pid = os.getppid()

        def _watch_parent():
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            SYNCHRONIZE = 0x00100000
            WAIT_OBJECT_0 = 0x00000000
            INFINITE = 0xFFFFFFFF

            # Open a handle to the parent process
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, _parent_pid)
            if not handle:
                # Can't open parent — fall back to polling
                import time
                while True:
                    time.sleep(2)
                    try:
                        os.kill(_parent_pid, 0)  # check if alive
                    except OSError:
                        os._exit(1)
                return

            # Block until parent exits (efficient, no polling)
            kernel32.WaitForSingleObject(handle, INFINITE)
            kernel32.CloseHandle(handle)
            os._exit(1)

        t = threading.Thread(target=_watch_parent, daemon=True)
        t.start()

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


@building_app.command("construct")
def building_construct(
    slot_id: int = typer.Option(..., "--slot-id", "-s", help="Empty building slot ID (19-40)"),
    building: str = typer.Option(..., "--building", "-b", help="Building name to construct (e.g. 'Cranny', 'Embassy')"),
    allow_gold: bool = typer.Option(False, "--allow-gold", help="Allow spending gold (master builder)."),
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Village ID (default: current village)"),
):
    """Construct a new building on an empty slot."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            bs = BuildingService(client)

            # Resolve building name to GID
            name_to_gid = {v.lower(): k for k, v in BUILDING_NAMES.items()}
            search = building.lower()
            gid = name_to_gid.get(search, 0)
            if not gid:
                for bname, bgid in name_to_gid.items():
                    if search in bname:
                        gid = bgid
                        break
            if not gid:
                console.print(f"[red]Unknown building: {building}[/red]")
                raise typer.Exit(1)

            result = await bs.construct_building(slot_id, gid, allow_gold=allow_gold, village_id=village_id)
            if result.success:
                console.print(f"[green]Construction started![/green]")
                console.print(f"  {result.building_name} on slot {slot_id}")
            else:
                console.print(f"[red]Construction failed[/red]")
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
    verbose: bool = typer.Option(False, "--verbose", help="Show current resources and detailed cost breakdown"),
    log_file: Optional[str] = typer.Option(None, "--log-file", help="Path to a log file. All status messages are appended in real-time."),
):
    """Execute a build plan in priority order. Waits for resources and empty queue."""
    async def _do():
        s = _settings()
        plan = BuildPlan.from_file(plan_file)

        # Open log file handle (stays open for the entire run)
        _log_fh = None
        if log_file:
            import os, pathlib
            pathlib.Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            _log_fh = open(log_file, "a", encoding="utf-8")

        def _log(msg: str):
            """Print to console AND append to log file with timestamp.
            Every piece of information flows through this single function —
            nothing can happen unlogged."""
            console.print(f"  {msg}")
            if _log_fh:
                from datetime import datetime
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _log_fh.write(f"[{ts}] {msg}\n")
                _log_fh.flush()  # flush immediately so nothing is lost on crash/kill

        # Install a Python logging handler that mirrors everything the
        # travian_api loggers emit (HTTP errors, retries, warnings) into
        # the same log file.  This catches low-level network issues that
        # never reach _report().
        if _log_fh:
            import logging as _logging
            class _FileHandler(_logging.Handler):
                def emit(self, record):
                    from datetime import datetime
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _log_fh.write(f"[{ts}] [LOG/{record.levelname}] {record.getMessage()}\n")
                    _log_fh.flush()
            _fh = _FileHandler()
            _fh.setLevel(_logging.DEBUG)
            _logging.getLogger("travian_api").addHandler(_fh)

        _log(f"Loaded plan: village {plan.village_id}, {len(plan.items)} items")
        _log(f"Plan file: {plan_file}")
        _log(f"Options: dry_run={dry_run}, poll={poll}s, use_video={use_video}, verbose={verbose}")
        for item in sorted(plan.items, key=lambda x: x.priority):
            label = f"slot {item.slot}" if item.slot else item.building
            _log(f"  P{item.priority}: {label} -> Lv{item.target}")

        try:
            async with HttpClient(s) as client:
                _log("Connecting to Travian server...")
                auth = AuthService(client, s)
                await auth.login()
                _log("Login successful.")

                bqs = BuildQueueService(client)
                bqs.on_status(_log)

                if dry_run:
                    _log("Mode: DRY RUN")
                    results = await bqs.execute_plan(plan, poll_interval_s=poll, dry_run=True)
                else:
                    _log("Mode: LIVE EXECUTION")
                    results = await bqs.execute_plan_continuous(plan, poll_interval_s=poll, use_video=use_video, verbose=verbose)

                _log("=== RESULTS ===")
                for r in results:
                    status = r.get('status', '?')
                    color = 'green' if status == 'started' else 'yellow' if status == 'dry_run' else 'red'
                    console.print(f"  [{color}]{r['building']} {r.get('level','')} - {status}[/{color}]  {r.get('time','')}")
                    _log(f"RESULT: {r['building']} {r.get('level','')} - {status} {r.get('time','')}")

        except KeyboardInterrupt:
            _log("INTERRUPTED: Received Ctrl+C / SIGINT. Shutting down gracefully.")
            raise
        except Exception as e:
            _log(f"FATAL ERROR: {type(e).__name__}: {e}")
            raise
        finally:
            _log("Session ended.")
            if _log_fh:
                _log_fh.close()
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


# ── Farm List ────────────────────────────────────────────────────────
farm_app = typer.Typer(name="farm", help="Farm list management and raiding commands")
app.add_typer(farm_app)


def _raid_icon(icon: int) -> str:
    """Map lastRaid.icon to a coloured label."""
    return {
        1: "[green]no loss[/green]",
        2: "[yellow]some loss[/yellow]",
        3: "[red]all dead[/red]",
    }.get(icon, "[dim]—[/dim]")


def _capacity_bar(raided: int, max_cap: int) -> str:
    """Show raided/capacity with colour."""
    if max_cap == 0:
        return "[dim]—[/dim]"
    pct = raided / max_cap
    colour = "green" if pct > 0.7 else "yellow" if pct > 0.3 else "red"
    return f"[{colour}]{raided}/{max_cap}[/{colour}]"


def _time_ago(unix_ts: int | None) -> str:
    """Convert unix timestamp to human-readable time-ago string."""
    if not unix_ts:
        return "[dim]never[/dim]"
    import time
    diff = int(time.time()) - unix_ts
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h {(diff % 3600) // 60}m ago"
    return f"{diff // 86400}d ago"


@farm_app.command("list")
def farm_list_cmd():
    """List all farm lists with summary info."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            fls = FarmListService(client)
            lists = await fls.get_all_farm_lists()

            if not lists:
                console.print("[yellow]No farm lists found[/yellow]")
                return

            table = Table(title="Farm Lists")
            table.add_column("ID", style="cyan", justify="right")
            table.add_column("Name", style="green")
            table.add_column("Slots", justify="right")
            table.add_column("Running", justify="center")
            table.add_column("Last Started", justify="right")
            table.add_column("Village ID", justify="right")

            for fl in lists:
                running = (
                    f"[yellow]{fl.running_raids_amount}[/yellow]"
                    if fl.running_raids_amount
                    else "[dim]0[/dim]"
                )
                table.add_row(
                    str(fl.id),
                    fl.name,
                    str(fl.slots_amount),
                    running,
                    _time_ago(fl.last_started_time),
                    str(fl.owner_village.id),
                )
            console.print(table)
    _run(_do())


@farm_app.command("show")
def farm_show(
    list_id: int = typer.Argument(..., help="Farm list ID"),
):
    """Show a farm list with smart raid intelligence per target."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            fls = FarmListService(client)
            fl = await fls.get_farm_list(list_id)

            # Header
            troops = fl.owner_village.get_available_troops()
            console.print(f"\n[bold]{fl.name}[/bold]  (id={fl.id})")
            console.print(
                f"  Village: {fl.owner_village.id}  |  "
                f"Running raids: {fl.running_raids_amount}  |  "
                f"Slots: {fl.slots_amount}"
            )
            console.print(
                f"  Available troops: t1={troops.t1} t2={troops.t2} t3={troops.t3} "
                f"t4={troops.t4} t5={troops.t5} t6={troops.t6}"
            )

            if not fl.slots:
                console.print("[yellow]  No targets in this list[/yellow]")
                return

            # Smart table
            table = Table(title="Targets", show_lines=True)
            table.add_column("#", style="dim", justify="right")
            table.add_column("Target", style="cyan")
            table.add_column("Pop", justify="right")
            table.add_column("Dist", justify="right")
            table.add_column("Troops", justify="right")
            table.add_column("Last Raid", justify="right")
            table.add_column("Raided/Cap", justify="right")
            table.add_column("Result")
            table.add_column("Status")
            table.add_column("Total", justify="right")

            for i, slot in enumerate(fl.slots, 1):
                t = slot.target
                name = f"{t.name}\n({t.x}|{t.y})" if t.name else f"({t.x}|{t.y})"

                # Troop composition (non-zero only)
                troop_parts = []
                for ti in range(1, 11):
                    val = getattr(slot.troop, f"t{ti}", 0)
                    if val:
                        troop_parts.append(f"t{ti}={val}")
                troop_str = " ".join(troop_parts) if troop_parts else "[dim]—[/dim]"

                # Last raid info
                lr = slot.last_raid
                last_raid_time = _time_ago(lr.time if lr else None)
                capacity = _capacity_bar(
                    lr.raided_resources.total if lr else 0,
                    lr.booty_max if lr else 0,
                )
                result = _raid_icon(lr.icon if lr else 0)

                # Status: running / spying / active / inactive
                if slot.is_running:
                    status = "[yellow]raiding...[/yellow]"
                elif slot.is_spying:
                    status = "[blue]scouting...[/blue]"
                elif not slot.is_active:
                    status = "[red]inactive[/red]"
                else:
                    status = "[green]ready[/green]"

                # Total booty
                tb = slot.total_booty
                total_str = f"{tb.booty:,}\n({tb.raids} raids)" if tb.raids else "[dim]—[/dim]"

                table.add_row(
                    str(i),
                    name,
                    str(t.population),
                    f"{slot.distance:.1f}",
                    troop_str,
                    last_raid_time,
                    capacity,
                    result,
                    status,
                    total_str,
                )

            console.print(table)
    _run(_do())


@farm_app.command("send")
def farm_send(
    list_id: int = typer.Argument(..., help="Farm list ID to send"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Send all active targets in a farm list."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            fls = FarmListService(client)

            fl = await fls.get_farm_list(list_id)
            active = fl.active_slots
            if not active:
                console.print("[yellow]No active targets to send[/yellow]")
                return

            console.print(
                f"Farm list: [bold]{fl.name}[/bold]  —  "
                f"{len(active)} active targets"
            )
            if not confirm:
                if not typer.confirm("Send raids?"):
                    console.print("Cancelled.")
                    return

            result = await fls.send_farm_list(list_id)
            if result.targets and result.targets[0].error == "plus.error_goldclub":
                console.print(
                    "[red]Gold Club is not active — sending via farm list API is blocked.[/red]\n"
                    "Manage lists (create/add targets) still works without Gold Club."
                )
                return

            console.print(
                f"[green]Sent![/green]  "
                f"Success: {result.success_count}  Failed: {result.fail_count}"
            )
            for t in result.targets:
                icon = "[green]ok[/green]" if t.status == "success" else f"[red]{t.error}[/red]"
                console.print(f"  Slot {t.id}: {icon}")
    _run(_do())


@farm_app.command("create")
def farm_create(
    name: str = typer.Option(..., "--name", "-n", help="Farm list name"),
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Source village ID"),
):
    """Create a new farm list."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            state = await auth.login()
            fls = FarmListService(client)
            vid = village_id or state.village_id
            list_id = await fls.create_farm_list(vid, name)
            console.print(f"[green]Created farm list '{name}' (id={list_id})[/green]")
    _run(_do())


@farm_app.command("add-target")
def farm_add_target(
    list_id: int = typer.Argument(..., help="Farm list ID"),
    x: int = typer.Option(..., "--x", help="Target X coordinate"),
    y: int = typer.Option(..., "--y", help="Target Y coordinate"),
    troop: List[str] = typer.Option([], "--troop", "-t", help="Troop spec: t1=5"),
    force: bool = typer.Option(False, "--force", help="Force add even if duplicate"),
):
    """Add a target to a farm list."""
    troops = {f"t{i}": 0 for i in range(1, 11)}
    for spec in troop:
        parts = spec.split("=")
        if len(parts) == 2:
            troops[parts[0]] = int(parts[1])

    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            fls = FarmListService(client)
            await fls.add_slot(list_id, x=x, y=y, units=troops, force=force)
            console.print(f"[green]Added target ({x},{y}) to list {list_id}[/green]")
    _run(_do())


@farm_app.command("delete")
def farm_delete(
    list_id: int = typer.Argument(..., help="Farm list ID to delete"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a farm list."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            await auth.login()
            fls = FarmListService(client)

            if not confirm:
                fl = await fls.get_farm_list(list_id)
                if not typer.confirm(f"Delete '{fl.name}' ({fl.slots_amount} slots)?"):
                    console.print("Cancelled.")
                    return

            await fls.delete_farm_list(list_id)
            console.print(f"[green]Deleted farm list {list_id}[/green]")
    _run(_do())


# ── Auto-Scout ───────────────────────────────────────────────────────
scout_app = typer.Typer(name="scout", help="Auto-scout commands — scan map and send scouts")
app.add_typer(scout_app)


@scout_app.command("scan")
def scout_scan(
    radius: int = typer.Option(10, "--radius", "-r", help="Scan radius from village"),
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Source village ID (default: main)"),
    max_pop: Optional[int] = typer.Option(None, "--max-pop", help="Max population filter"),
    min_pop: Optional[int] = typer.Option(None, "--min-pop", help="Min population filter"),
    no_player: bool = typer.Option(False, "--no-player", help="Only show unoccupied villages"),
    show_oases: bool = typer.Option(False, "--show-oases", help="Include oases in results"),
    enrich: bool = typer.Option(True, "--enrich/--no-enrich", help="Fetch population details (slower)"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results to show"),
):
    """Scan the map around your village and show potential targets."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            state = await auth.login()
            svc = AutoScoutService(client)
            svc.on_status(lambda msg: console.print(f"  {msg}"))

            # Determine center
            vid = village_id or state.village_id
            center_village = next(
                (v for v in state.villages if v.id == vid), None
            )
            if not center_village:
                console.print(f"[red]Village {vid} not found[/red]")
                return

            cx, cy = center_village.x, center_village.y
            console.print(
                f"Scanning from [cyan]{center_village.name}[/cyan] "
                f"({cx}|{cy}) radius={radius}"
            )

            # Scan
            tiles = await svc.scan_map(cx, cy, radius)

            # Filter out own villages
            own_village_ids = {v.id for v in state.villages}
            tiles = [t for t in tiles if t.village_id not in own_village_ids]

            # Filter non-oasis villages with actual village IDs
            if not show_oases:
                tiles = [t for t in tiles if not t.is_oasis]
            tiles = [t for t in tiles if t.village_id > 0]

            # Enrich with population data
            if enrich and tiles:
                console.print(f"  Enriching {len(tiles)} tiles with details...")
                tiles = await svc.enrich_tiles(tiles)

            # Apply filters
            from travian_api.services.auto_scout_service import AutoScoutService as _AS
            tiles = svc.filter_targets(
                tiles,
                max_population=max_pop,
                min_population=min_pop,
                only_no_player=no_player,
                exclude_oases=not show_oases,
            )

            if not tiles:
                console.print("[yellow]No targets found matching filters[/yellow]")
                return

            tiles = tiles[:limit]
            console.print(f"\n[bold]Found {len(tiles)} targets:[/bold]")

            table = Table(title="Scan Results")
            table.add_column("#", style="dim", justify="right")
            table.add_column("Coords", style="cyan")
            table.add_column("Name")
            table.add_column("Pop", justify="right")
            table.add_column("Dist", justify="right")
            table.add_column("Player")
            table.add_column("Tribe")

            for i, t in enumerate(tiles, 1):
                table.add_row(
                    str(i),
                    f"({t.x}|{t.y})",
                    t.village_name or "[dim]—[/dim]",
                    str(t.population) if t.population else "[dim]?[/dim]",
                    f"{t.distance:.1f}",
                    t.player_name or "[dim]—[/dim]",
                    t.tribe or "[dim]—[/dim]",
                )
            console.print(table)
    _run(_do())


@scout_app.command("auto")
def scout_auto(
    radius: int = typer.Option(10, "--radius", "-r", help="Scan radius"),
    village_id: Optional[int] = typer.Option(None, "--village-id", "-v", help="Source village ID"),
    max_pop: Optional[int] = typer.Option(None, "--max-pop", help="Max population filter"),
    min_pop: Optional[int] = typer.Option(None, "--min-pop", help="Min population filter"),
    scout_type: str = typer.Option("resources", "--type", "-t", help="Scout type: resources or defenses"),
    amount: int = typer.Option(1, "--amount", "-n", help="Number of scouts per target"),
    exclude: Optional[str] = typer.Option(None, "--exclude", "-e", help="Exclude file (one coord per line: x,y)"),
    no_player: bool = typer.Option(False, "--no-player", help="Only scout unoccupied villages"),
    show_oases: bool = typer.Option(False, "--show-oases", help="Include oases"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max targets to scout"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be scouted without sending"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    delay: float = typer.Option(1.0, "--delay", help="Seconds between scout sends"),
):
    """Scan the map, filter targets, and send scouts automatically."""
    async def _do():
        s = _settings()
        async with HttpClient(s) as client:
            auth = AuthService(client, s)
            state = await auth.login()
            svc = AutoScoutService(client)
            svc.on_status(lambda msg: console.print(f"  {msg}"))

            vid = village_id or state.village_id
            center_village = next(
                (v for v in state.villages if v.id == vid), None
            )
            if not center_village:
                console.print(f"[red]Village {vid} not found[/red]")
                return

            cx, cy = center_village.x, center_village.y
            console.print(
                f"Auto-Scout from [cyan]{center_village.name}[/cyan] "
                f"({cx}|{cy}) r={radius} type={scout_type} amount={amount}"
            )

            # Parse exclude list
            exclude_coords: set = set()
            if exclude:
                from pathlib import Path
                p = Path(exclude)
                if p.exists():
                    for line in p.read_text().splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            parts = line.replace("|", ",").split(",")
                            if len(parts) == 2:
                                try:
                                    exclude_coords.add((int(parts[0]), int(parts[1])))
                                except ValueError:
                                    pass
                    console.print(f"  Loaded {len(exclude_coords)} excluded coordinates")
                else:
                    console.print(f"[yellow]Exclude file not found: {exclude}[/yellow]")

            # Scan
            tiles = await svc.scan_map(cx, cy, radius)

            # Remove own villages
            own_ids = {v.id for v in state.villages}
            tiles = [t for t in tiles if t.village_id not in own_ids]
            if not show_oases:
                tiles = [t for t in tiles if not t.is_oasis]
            tiles = [t for t in tiles if t.village_id > 0]

            # Enrich
            if tiles:
                console.print(f"  Enriching {len(tiles)} tiles...")
                tiles = await svc.enrich_tiles(tiles)

            # Filter
            tiles = svc.filter_targets(
                tiles,
                max_population=max_pop,
                min_population=min_pop,
                exclude_coords=exclude_coords,
                only_no_player=no_player,
                exclude_oases=not show_oases,
            )

            if not tiles:
                console.print("[yellow]No targets found matching filters[/yellow]")
                return

            tiles = tiles[:limit]
            console.print(f"\n[bold]{len(tiles)} targets to scout:[/bold]")

            # Show targets table
            table = Table()
            table.add_column("#", style="dim", justify="right")
            table.add_column("Coords", style="cyan")
            table.add_column("Name")
            table.add_column("Pop", justify="right")
            table.add_column("Dist", justify="right")
            table.add_column("Player")

            for i, t in enumerate(tiles, 1):
                table.add_row(
                    str(i),
                    f"({t.x}|{t.y})",
                    t.village_name or "[dim]—[/dim]",
                    str(t.population) if t.population else "[dim]?[/dim]",
                    f"{t.distance:.1f}",
                    t.player_name or "[dim]—[/dim]",
                )
            console.print(table)

            if dry_run:
                console.print("[yellow]DRY RUN — no scouts sent[/yellow]")
                return

            if not confirm:
                if not typer.confirm(f"Send {amount} scout(s) to {len(tiles)} targets?"):
                    console.print("Cancelled.")
                    return

            # Send scouts
            results = await svc.send_scouts_to_targets(
                targets=tiles,
                scout_amount=amount,
                scout_type=scout_type,
                village_id=vid,
                tribe_id=state.tribe_id,
                delay_between=delay,
            )

            # Summary
            sent = sum(1 for r in results if r["success"])
            console.print(
                f"\n[bold]Results: {sent}/{len(results)} scouts sent[/bold]"
            )
    _run(_do())


def main():
    app()
