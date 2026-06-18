"""Priority Build Queue Service.

Reads a build plan file and executes upgrades in priority order,
waiting for resources and empty construction queue.

Plan file format (YAML):
```yaml
village: 20031          # village ID
plan:
  # By slot (preferred for resource fields with duplicates)
  - slot: 3
    target: 5
    priority: 1

  # By name (still works for unique buildings)
  - building: Cranny
    target: 3
    priority: 2
  - building: Residence
    target: 5
    priority: 2
  - building: Clay Pit
    target: 3
    priority: 3
```

Execution rules:
- Process priorities 1..5 in order
- Within same priority: first building whose conditions are met goes first
- Conditions: queue empty + enough resources
- After starting a build, wait for it to finish before next
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from ..clients.http_client import HttpClient
from ..constants import BUILDING_GID_BY_NAME_LOWER, BUILDING_NAMES
from ..logging_config import get_logger
from ..stealth.timing import HumanTiming
from .building_service import BuildingService

# ── Per-account build-action coordination (stealth) ────────────────────
#
# Two queues on different villages of the same account would otherwise
# fire build POSTs at the same instant — a tell that no human can
# produce. _account_build_locks holds one asyncio.Lock per http_client
# (id-keyed) so build actions serialize within an account, and
# _last_account_build_action_ts records when the last action fired so
# back-to-back actions on the same account can insert a randomized
# 10–90s stagger. State is process-local; cross-process coordination
# isn't needed because a single process owns each account session.
_account_build_locks: Dict[int, asyncio.Lock] = {}
_account_build_locks_meta_lock = asyncio.Lock()
_last_account_build_action_ts: Dict[int, float] = {}


async def _account_build_lock_for(http_client: HttpClient) -> asyncio.Lock:
    """Return (creating if needed) the per-account build-action lock."""
    key = id(http_client)
    async with _account_build_locks_meta_lock:
        lock = _account_build_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _account_build_locks[key] = lock
        return lock


async def _stagger_account_build(http_client: HttpClient) -> None:
    """Insert a 10-90s stagger when another queue on this account acted recently.

    No-op when stealth is disabled or there's no prior action. Updates the
    timestamp BEFORE sleeping so concurrent waiters see the latest commit
    time and stack their staggers correctly.
    """
    if not getattr(http_client, "stealth_enabled", False):
        _last_account_build_action_ts[id(http_client)] = time.monotonic()
        return
    key = id(http_client)
    last_ts = _last_account_build_action_ts.get(key, 0.0)
    now = time.monotonic()
    gap = now - last_ts
    stagger = 0.0
    if last_ts > 0.0 and gap < 60.0:
        stagger = HumanTiming.delay(45.0, variance_factor=0.6)
        stagger = max(10.0, min(stagger, 90.0))
    # Reserve our slot first so other waiters stack after this stagger,
    # not on top of it.
    _last_account_build_action_ts[key] = now + stagger
    if stagger > 0.0:
        await asyncio.sleep(stagger)
    _last_account_build_action_ts[key] = time.monotonic()


def _resolve_idle_wait(
    *,
    any_no_checksum: bool,
    any_resource_short: bool,
    next_prio: int,
    poll_interval_s: float,
    stealth_enabled: bool,
) -> tuple[str, float]:
    """Decide the post-cycle wait when nothing was built — returns (reason, wait_s).

    Each branch produces the FINAL wait (a single heavy-tail where wanted); the
    caller applies ONLY ``tempo_scale`` to it, never another
    ``HumanTiming.delay()`` — that double-application once compounded the
    resource-short pause into 45+ minute frozen waits. Resource-short is the
    economic "come back later" branch (clamped 2–10 min); the no-checksum and
    no-items branches are transient and just re-poll on a human-paced interval.
    """
    if any_no_checksum and not any_resource_short:
        reason = (
            f"Priority {next_prio}: build page returned no upgrade URL "
            "(rally-point tab default, maxed building, or stale session)"
        )
        return reason, HumanTiming.delay(float(poll_interval_s))
    if any_resource_short and not any_no_checksum:
        reason = f"Insufficient resources for priority {next_prio} items"
        if stealth_enabled:
            return reason, max(120.0, min(HumanTiming.delay(180.0, variance_factor=0.7), 600.0))
        return reason, float(poll_interval_s)
    return f"No items completed for priority {next_prio}", HumanTiming.delay(float(poll_interval_s))


logger = get_logger(__name__)


@dataclass
class BuildPlanItem:
    """Single item in the build plan."""

    building: str  # Building name (partial match), empty if slot specified
    target: int  # Target level
    priority: int  # 1=highest, 5=lowest
    slot: int = 0  # Explicit slot ID from YAML (0 = resolve by name)
    expect: str = ""  # Optional: expected building name for slot (safety guard)
    slot_id: int = 0  # Resolved slot ID (filled at runtime)
    current_level: int = 0
    status: str = "pending"  # pending | building | done | skipped
    is_construction: bool = False  # True if building needs to be constructed (empty slot)
    construct_gid: int = 0  # GID of building to construct


@dataclass
class BuildPlan:
    """Full build plan for a village."""

    village_id: int
    items: List[BuildPlanItem]

    @classmethod
    def from_file(cls, path: str | Path) -> BuildPlan:
        """Load build plan from YAML file. Tabs are converted to spaces automatically."""
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        # YAML forbids tabs for indentation — silently convert to 2 spaces
        raw = raw.replace("\t", "  ")
        data = yaml.safe_load(raw)

        village_id = data.get("village", data.get("village_id", 0))
        items = []
        for entry in data.get("plan", []):
            items.append(
                BuildPlanItem(
                    building=entry.get("building", ""),
                    target=entry.get("target", entry.get("level", 1)),
                    priority=entry.get("priority", 5),
                    slot=entry.get("slot", 0),
                    expect=entry.get("expect", ""),
                )
            )

        return cls(village_id=village_id, items=items)

    def pending_items(self) -> List[BuildPlanItem]:
        """Get items not yet done, sorted by priority."""
        return sorted([i for i in self.items if i.status == "pending"], key=lambda x: x.priority)

    def next_priority(self) -> Optional[int]:
        """Get the next priority level to process."""
        pending = self.pending_items()
        return pending[0].priority if pending else None


class BuildQueueService:
    """Executes a build plan with priority ordering."""

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client
        self.building_service = BuildingService(http_client)
        self._on_status: Optional[Callable[[str], None]] = None
        self._status_callbacks: list[Callable[[str], None]] = []

    def on_status(self, callback: Callable[[str], None]):
        """Set status callback for progress reporting (legacy single-callback API)."""
        self._on_status = callback

    def add_status_callback(self, callback: Callable[[str], None]) -> None:
        """Register an additional status callback (safe for concurrent use)."""
        self._status_callbacks.append(callback)

    def remove_status_callback(self, callback: Callable[[str], None]) -> None:
        """Remove a previously registered status callback."""
        try:
            self._status_callbacks.remove(callback)
        except ValueError:
            pass

    def _report(self, msg: str):
        """Report status."""
        logger.info(msg)
        if self._on_status:
            self._on_status(msg)
        for cb in self._status_callbacks:
            cb(msg)

    async def _post_build_reaction(self) -> None:
        """Sleep a randomized reaction window after a build slot frees.

        Travian's anti-bot scopes for the 'wakes up exactly 3s after the
        timer hits zero' fingerprint — a human takes 30s-5min to notice a
        push notification, glance at the tab, decide to queue the next
        build. Heavy-tailed: most reactions are quick, occasional ones are
        long (player walked away). Cheap to implement, expensive in pure
        wall-clock — but only fires once per slot-free transition, not
        per polling cycle.
        """
        if not getattr(self.http_client, "stealth_enabled", False):
            return
        try:
            # Heavy-tailed: 70% short (~30s), 25% medium (~90s), 5% long (~300s).
            # Mean scaled by the shared session tempo so the reaction window
            # drifts with the rest of the session's pace.
            reaction = HumanTiming.delay(self.http_client.tempo_scale(45.0), variance_factor=0.8)
            reaction = max(20.0, min(reaction, 300.0))
            self._report(f"  Noticing slot freed... ({reaction:.0f}s reaction window)")
            await asyncio.sleep(reaction)
        except Exception:
            pass

    async def resolve_slots(self, plan: BuildPlan):
        """Resolve building names to slot IDs."""
        buildings = await self.building_service.get_village_buildings(
            village_id=plan.village_id or None
        )

        for item in plan.items:
            if item.slot:
                # Slot explicitly specified — just look up current level
                item.slot_id = item.slot
                for b in buildings:
                    if b.slot_id == item.slot:
                        item.current_level = b.level
                        if not item.building:
                            item.building = b.name
                        # If slot is empty (gid=0) and a building name is provided, mark as construction
                        if b.gid == 0 and item.building:
                            name_to_gid = BUILDING_GID_BY_NAME_LOWER
                            search_lower = item.building.lower()
                            gid = name_to_gid.get(search_lower, 0)
                            if not gid:
                                for bname, bgid in name_to_gid.items():
                                    if search_lower in bname:
                                        gid = bgid
                                        break
                            if gid:
                                item.is_construction = True
                                item.construct_gid = gid
                                item.building = BUILDING_NAMES.get(gid, item.building)
                                self._report(
                                    f"CONSTRUCT: {item.building} (gid={gid}) on slot {item.slot_id} (target Lv{item.target})"
                                )
                            else:
                                self._report(
                                    f"WARNING: Unknown building '{item.building}' for empty slot {item.slot}"
                                )
                                item.status = "skipped"
                        # Safety guard: if 'expect' is set, verify the building name matches
                        elif item.expect and item.expect.lower() not in b.name.lower():
                            self._report(
                                f"MISMATCH: slot {item.slot} is '{b.name}' but expected '{item.expect}'. Skipping!"
                            )
                            item.status = "skipped"
                        break
            else:
                # Resolve by name — find matches, pick lowest level below target
                search = item.building.lower()
                matches = [b for b in buildings if search in b.name.lower()]
                below_target = [b for b in matches if b.level < item.target]

                if len(matches) > 1:
                    logger.warning(
                        f"Multiple matches for '{item.building}': "
                        f"{', '.join(f'slot {b.slot_id} Lv{b.level}' for b in matches)}. "
                        f"Consider using 'slot:' instead of 'building:' in your plan."
                    )

                if below_target:
                    # Pick the one with the lowest level (most work to do)
                    best = min(below_target, key=lambda b: b.level)
                    item.slot_id = best.slot_id
                    item.current_level = best.level
                elif matches:
                    # All at or above target
                    item.slot_id = matches[0].slot_id
                    item.current_level = matches[0].level

            # Skip further checks if already marked (e.g. mismatch)
            if item.status == "skipped":
                continue

            if item.slot_id == 0:
                # Building not found — try to resolve as new construction on empty slot
                name_to_gid = BUILDING_GID_BY_NAME_LOWER
                search_lower = item.building.lower()
                gid = name_to_gid.get(search_lower, 0)
                if not gid:
                    # Partial match
                    for bname, bgid in name_to_gid.items():
                        if search_lower in bname:
                            gid = bgid
                            break

                if gid:
                    # Find first empty slot (gid=0) in village buildings (19-40)
                    empty_slots = [b for b in buildings if b.gid == 0 and b.slot_id >= 19]
                    # Exclude slots already claimed by other construction items in this plan
                    claimed = {i.slot_id for i in plan.items if i.is_construction and i.slot_id > 0}
                    empty_slots = [b for b in empty_slots if b.slot_id not in claimed]

                    if empty_slots:
                        chosen = empty_slots[0]
                        item.slot_id = chosen.slot_id
                        item.is_construction = True
                        item.construct_gid = gid
                        item.current_level = 0
                        item.building = BUILDING_NAMES.get(gid, item.building)
                        self._report(
                            f"CONSTRUCT: {item.building} (gid={gid}) on empty slot {item.slot_id} (target Lv{item.target})"
                        )
                    else:
                        self._report(f"WARNING: No empty slots available for '{item.building}'")
                        item.status = "skipped"
                else:
                    self._report(
                        f"WARNING: Could not find '{item.building}' in village or building list"
                    )
                    item.status = "skipped"
            elif item.current_level >= item.target:
                self._report(
                    f"SKIP: {item.building} already at level {item.current_level} (target {item.target})"
                )
                item.status = "done"
            else:
                self._report(
                    f"FOUND: {item.building} at slot {item.slot_id} (Lv{item.current_level} -> {item.target})"
                )

    async def check_resources(
        self, slot_id: int, village_id: Optional[int] = None, construct_gid: int = 0
    ) -> Dict[str, Any]:
        """Check if resources are sufficient for upgrade or construction.

        Args:
            slot_id: Building slot ID
            village_id: Village ID
            construct_gid: If > 0, check resources for constructing this building GID on an empty slot

        Returns dict with:
            can_build: bool
            missing: dict of resource shortages
            costs: dict of upgrade costs
        """
        if construct_gid:
            # New construction — fetch available buildings for the empty slot
            available = await self.building_service.get_available_buildings(
                slot_id, village_id=village_id
            )
            resources = await self.building_service.get_resources(village_id=village_id)
            target_building = None
            for b in available:
                if b["gid"] == construct_gid:
                    target_building = b
                    break
            costs = target_building["costs"] if target_building else {}
            can_construct = bool(target_building and target_building["can_build"])
            current = {
                "lumber": resources.lumber,
                "clay": resources.clay,
                "iron": resources.iron,
                "crop": resources.crop,
            }
            missing = {}
            for res, needed in costs.items():
                if res in current and current[res] < needed:
                    missing[res] = needed - current[res]
            return {
                "can_build": can_construct and len(missing) == 0,
                "has_checksum": can_construct,
                "missing": missing,
                "costs": costs,
                "current": current,
            }

        detail = await self.building_service.get_building_detail(slot_id, village_id=village_id)
        resources = await self.building_service.get_resources(village_id=village_id)

        costs = detail.costs or {}
        current = {
            "lumber": resources.lumber,
            "clay": resources.clay,
            "iron": resources.iron,
            "crop": resources.crop,
        }

        missing = {}
        for res, needed in costs.items():
            if res in current and current[res] < needed:
                missing[res] = needed - current[res]

        # `detail.checksum` defaults to "" (not None) when the build page
        # returned no upgrade URL — must match the truthy check in
        # BuildingService.upgrade_building, otherwise the queue falsely
        # reports READY and the subsequent upgrade_building() raises
        # "No upgrade available".
        return {
            "can_build": len(missing) == 0 and bool(detail.checksum),
            "has_checksum": bool(detail.checksum),
            "missing": missing,
            "costs": costs,
            "current": current,
        }

    async def is_queue_empty(self, village_id: Optional[int] = None) -> bool:
        """Check if construction queue is empty."""
        queue = await self.building_service.get_construction_queue(village_id=village_id)
        return len(queue) == 0

    async def get_queue_remaining(self, village_id: Optional[int] = None) -> int:
        """Get seconds remaining on current construction. 0 if empty."""
        queue = await self.building_service.get_construction_queue(village_id=village_id)
        if not queue:
            return 0
        return max(q.remaining_seconds for q in queue)

    async def execute_plan(
        self,
        plan: BuildPlan,
        poll_interval_s: int = 30,
        max_wait_s: int = 7200,  # 2 hours max
        dry_run: bool = False,
        use_video: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Execute build plan in priority order.

        Args:
            plan: Build plan to execute
            poll_interval_s: How often to check conditions (seconds)
            max_wait_s: Max time to wait for conditions to be met
            dry_run: If True, only report what would happen

        Returns:
            List of results per build item
        """
        results = []

        # Resolve building slots
        await self.resolve_slots(plan)

        # Dry run: just show the plan in order, check resources
        if dry_run:
            for prio in sorted(set(i.priority for i in plan.items)):
                items = [i for i in plan.items if i.priority == prio and i.status == "pending"]
                if not items:
                    continue
                self._report(f"\n--- Priority {prio} ({len(items)} items) ---")
                for item in items:
                    check = await self.check_resources(
                        item.slot_id,
                        village_id=plan.village_id or None,
                        construct_gid=item.construct_gid if item.is_construction else 0,
                    )
                    ready = (
                        "READY" if check["can_build"] else f"WAITING (missing: {check['missing']})"
                    )
                    action = "CONSTRUCT " if item.is_construction else ""
                    self._report(
                        f"  {action}{item.building} Lv{item.current_level}->{item.target} "
                        f"(slot {item.slot_id}, cost: {check['costs']}) [{ready}]"
                    )
                    results.append(
                        {
                            "building": item.building,
                            "slot_id": item.slot_id,
                            "level": f"{item.current_level} -> {item.target}",
                            "status": "dry_run",
                            "ready": check["can_build"],
                            "missing": check["missing"],
                        }
                    )
            return results

        while True:
            next_prio = plan.next_priority()
            if next_prio is None:
                self._report("All items completed!")
                break

            # Get all items at this priority level
            prio_items = [
                i for i in plan.items if i.priority == next_prio and i.status == "pending"
            ]

            if not prio_items:
                break

            self._report(f"\n--- Processing priority {next_prio} ({len(prio_items)} items) ---")

            # Find first item whose conditions are met
            built_one = False
            waited = 0

            vid = plan.village_id or None
            while not built_one and waited < max_wait_s:
                # First check: is queue empty?
                queue_empty = await self.is_queue_empty(village_id=vid)

                if not queue_empty:
                    remaining = await self.get_queue_remaining(village_id=vid)
                    self._report(f"Queue busy ({remaining}s remaining). Waiting...")
                    wait_time = min(remaining + 5, poll_interval_s)
                    await asyncio.sleep(HumanTiming.micro_jitter(wait_time, jitter_pct=0.15))
                    waited += wait_time
                    continue

                # Queue is empty — check which item has resources
                for item in prio_items:
                    if item.status != "pending":
                        continue

                    check = await self.check_resources(
                        item.slot_id,
                        village_id=vid,
                        construct_gid=item.construct_gid if item.is_construction else 0,
                    )

                    if check["can_build"]:
                        self._report(
                            f"BUILDING: {item.building} Lv{item.current_level} -> {item.target}"
                            f" (slot {item.slot_id}, costs: {check['costs']})"
                        )

                        if dry_run:
                            self._report("  (dry run - would build)")
                            item.status = "done"
                            results.append(
                                {
                                    "building": item.building,
                                    "slot_id": item.slot_id,
                                    "level": f"{item.current_level} -> {item.target}",
                                    "status": "dry_run",
                                }
                            )
                            built_one = True
                            break

                        # Actually upgrade or construct (per-account stagger
                        # so concurrent queues on the same account don't
                        # fire build POSTs at the same instant).
                        account_lock = await _account_build_lock_for(self.http_client)
                        async with account_lock:
                            await _stagger_account_build(self.http_client)
                            if item.is_construction:
                                result = await self.building_service.construct_building(
                                    item.slot_id,
                                    item.construct_gid,
                                    allow_gold=False,
                                    village_id=vid,
                                )
                            else:
                                result = await self.building_service.upgrade_building(
                                    item.slot_id, allow_gold=False, village_id=vid
                                )

                        if result.success:
                            item.status = "done"
                            self._report(f"  Started! Time: {result.construction_time}")
                            results.append(
                                {
                                    "building": item.building,
                                    "slot_id": item.slot_id,
                                    "level": f"{item.current_level} -> {item.target}",
                                    "status": "started",
                                    "time": result.construction_time,
                                }
                            )
                            built_one = True
                        else:
                            self._report(f"  FAILED: {result.raw_response[:100]}")
                            # If blocked by gold guard, it means queue wasn't actually empty
                            if "BLOCKED" in (result.raw_response or ""):
                                break  # Re-check queue
                            item.status = "skipped"
                            results.append(
                                {
                                    "building": item.building,
                                    "slot_id": item.slot_id,
                                    "status": "failed",
                                    "error": result.raw_response[:200],
                                }
                            )
                        break
                    _missing_str = ", ".join(f"{k}: {v}" for k, v in check["missing"].items())
                    if not check["has_checksum"]:
                        self._report(f"  {item.building}: no checksum (requirements not met?)")
                        # Don't spam — only log first time

                if not built_one:
                    self._report(
                        f"No items ready at priority {next_prio}. "
                        f"Waiting {poll_interval_s}s for resources..."
                    )
                    await asyncio.sleep(HumanTiming.micro_jitter(poll_interval_s, jitter_pct=0.2))
                    waited += poll_interval_s

            if not built_one:
                self._report(f"Timeout waiting for priority {next_prio} items")
                for item in prio_items:
                    if item.status == "pending":
                        item.status = "skipped"
                break

        return results

    async def execute_plan_continuous(
        self,
        plan: BuildPlan,
        poll_interval_s: int = 30,
        use_video: bool = False,
        verbose: bool = False,
    ) -> List[Dict[str, Any]]:
        """Execute entire plan continuously, waiting for each build to finish.

        This is the main entry point for the auto-builder.  Three guard
        mechanisms prevent duplicate or wasted upgrades:

        1. **Pre-build queue guard** -- before issuing an upgrade, re-check
           the live construction queue to catch races with other tabs.
        2. **Level-sync guard** -- after a build finishes, re-read the
           building level from the server instead of trusting the tracker.
        3. **Post-build queue guard** -- cross-check the queue's target
           level against the plan target to handle server-side lag.

        Args:
            plan: Build plan to execute.
            poll_interval_s: How often to check conditions (seconds).
            use_video: If True, claim buildingUpgrade video reward after each upgrade.
            verbose: If True, show current resources and detailed cost breakdown.
        """
        all_results = []
        vid = plan.village_id or None

        # Wait for any in-progress build to finish before resolving levels.
        # If we read levels while a build is in the queue, we'd get the
        # pre-upgrade level, causing an off-by-one drift for the entire run.
        if not await self.is_queue_empty(village_id=vid):
            self._report("Waiting for in-progress build to finish before resolving plan...")
            while not await self.is_queue_empty(village_id=vid):
                remaining = await self.get_queue_remaining(village_id=vid)
                self._report(f"  Queue not empty ({remaining}s remaining). Sleeping until done...")
                # Sleep for the actual remaining time + small buffer instead of polling
                wait = max(remaining + 3, 5)
                await asyncio.sleep(HumanTiming.micro_jitter(wait, jitter_pct=0.05))
            # Stealth: a real player doesn't return to the browser exactly
            # 3 seconds after a build timer ends. Add a human reaction
            # window before continuing — short for fast follow-ups, longer
            # otherwise so the wakeup doesn't sit on perfect completion
            # boundaries every single time.
            await self._post_build_reaction()

        await self.resolve_slots(plan)

        while True:
            pending = plan.pending_items()
            if not pending:
                self._report("Build plan complete!")
                break

            next_prio = pending[0].priority
            prio_items = [i for i in pending if i.priority == next_prio]

            self._report(f"\n=== Priority {next_prio}: {len(prio_items)} items ===")
            for pi in prio_items:
                self._report(
                    f"  {pi.building} Lv{pi.current_level} -> {pi.target} (slot {pi.slot_id})"
                )

            # Wait for queue to be empty — sleep for actual remaining time
            queue_was_busy = False
            while not await self.is_queue_empty(village_id=vid):
                queue_was_busy = True
                remaining = await self.get_queue_remaining(village_id=vid)
                self._report(f"Queue busy ({remaining}s remaining). Sleeping until done...")
                wait = max(remaining + 3, 5)
                await asyncio.sleep(HumanTiming.micro_jitter(wait, jitter_pct=0.05))
            if queue_was_busy:
                # Wakeup reaction window — see _post_build_reaction docstring.
                await self._post_build_reaction()

            # Show current resources if verbose
            if verbose:
                resources = await self.building_service.get_resources(village_id=vid)
                self._report(
                    f"  Resources: L={resources.lumber} C={resources.clay} I={resources.iron} Cr={resources.crop} (free crop: {resources.free_crop})"
                )

            # Stealth: activity scheduling — check if we need a break
            try:
                scheduler = self.http_client.activity_scheduler
                if not scheduler.can_continue():
                    break_s = scheduler.next_break_duration()
                    self._report(
                        f"Activity limit reached. Taking a break for {break_s / 60:.0f} minutes..."
                    )
                    await asyncio.sleep(break_s)
                    scheduler.start_session()
            except Exception:
                pass

            # Stealth: noise injection between build cycles
            try:
                await self.http_client.noise_injector.maybe_inject_noise(village_id=vid)
            except Exception:
                pass

            # Stealth: human delay before checking what to build next
            try:
                from ..stealth.human_delay import ActionType

                await self.http_client.human_delay.wait(
                    ActionType.DECISION, "reviewing build options"
                )
            except Exception:
                pass

            # Try each item at this priority
            built = False
            any_resource_short = False
            any_no_checksum = False
            for item in prio_items:
                try:
                    check = await self.check_resources(
                        item.slot_id,
                        village_id=vid,
                        construct_gid=item.construct_gid if item.is_construction else 0,
                    )
                except Exception as e:
                    self._report(f"  {item.building} (slot {item.slot_id}): error checking - {e}")
                    continue
                if not check.get("can_build"):
                    if not check.get("has_checksum"):
                        any_no_checksum = True
                    elif check.get("missing"):
                        any_resource_short = True
                if verbose:
                    costs = check.get("costs", {})
                    missing = check.get("missing", {})
                    can = check.get("can_build", False)
                    has_checksum = check.get("has_checksum", False)
                    costs_str = ", ".join(f"{k}={v}" for k, v in costs.items()) if costs else "n/a"
                    if can:
                        self._report(
                            f"  {item.building} (slot {item.slot_id}): READY costs({costs_str})"
                        )
                    elif not has_checksum:
                        # The build page returned no upgrade URL — common when
                        # the building is maxed, another tab is selected
                        # (rally-point defaults to troops), or the session
                        # landed on an error page. Not a resource problem.
                        self._report(
                            f"  {item.building} (slot {item.slot_id}): "
                            "no upgrade URL on build page — skipping "
                            "(building may be maxed, wrong tab, or session stale)"
                        )
                    else:
                        missing_str = (
                            ", ".join(f"{k}={v}" for k, v in missing.items()) if missing else "none"
                        )
                        self._report(
                            f"  {item.building} (slot {item.slot_id}): costs({costs_str}) missing({missing_str})"
                        )
                if check["can_build"]:
                    # ── Queue guard: prevent duplicate upgrades ──────────────
                    # Between the is_queue_empty() check above and this point,
                    # another tab or a previous cycle may have already queued
                    # the same building.  Re-check the live construction queue
                    # and, if the building is already upgrading, wait for it
                    # to finish rather than issuing a duplicate request.
                    try:
                        current_queue = await self.building_service.get_construction_queue(
                            village_id=vid
                        )
                        already_upgrading = any(
                            q.building_name
                            and item.building
                            and q.building_name.lower() == item.building.lower()
                            for q in current_queue
                        )
                        if already_upgrading:
                            self._report(
                                f"  SKIP: {item.building} (slot {item.slot_id}) already in construction queue — waiting for it to finish"
                            )
                            # Block until the in-flight upgrade completes
                            while not await self.is_queue_empty(village_id=vid):
                                remaining = await self.get_queue_remaining(village_id=vid)
                                wait = max(remaining + 3, 5)
                                await asyncio.sleep(HumanTiming.micro_jitter(wait, jitter_pct=0.05))
                            # Sync level from server to avoid off-by-one on next iteration
                            try:
                                buildings = await self.building_service.get_village_buildings(
                                    village_id=vid
                                )
                                for b in buildings:
                                    if b.slot_id == item.slot_id:
                                        item.current_level = b.level
                                        break
                            except Exception:
                                pass
                            if item.current_level >= item.target:
                                item.status = "done"
                                self._report(
                                    f"  DONE: {item.building} reached Lv{item.current_level} (target was {item.target})"
                                )
                            else:
                                self._report(
                                    f"  PROGRESS: {item.building} now Lv{item.current_level}, continuing to Lv{item.target}"
                                )
                            built = True
                            break
                    except Exception as e:
                        self._report(
                            f"  WARNING: Queue guard check failed: {e} — proceeding with upgrade"
                        )

                    # Stealth: simulate human browsing to the building before upgrading
                    delay = self.http_client.human_delay
                    await delay.wait(
                        ActionType.PRE_UPGRADE, f"preparing to upgrade {item.building}"
                    )

                    # Get building detail for gid (needed for video reward)
                    detail = (
                        await self.building_service.get_building_detail(
                            item.slot_id, village_id=vid
                        )
                        if use_video and not item.is_construction
                        else None
                    )

                    # Stealth: per-account stagger so two village queues
                    # on the same account don't fire build POSTs at the
                    # same instant. Serializes build actions per http_client
                    # and inserts a 10-90s pause when another queue acted
                    # within the last 60s.
                    account_lock = await _account_build_lock_for(self.http_client)
                    async with account_lock:
                        await _stagger_account_build(self.http_client)
                        if item.is_construction:
                            result = await self.building_service.construct_building(
                                item.slot_id,
                                item.construct_gid,
                                allow_gold=False,
                                village_id=vid,
                            )
                        else:
                            result = await self.building_service.upgrade_building(
                                item.slot_id, allow_gold=False, village_id=vid
                            )
                    if not result.success:
                        action = "CONSTRUCT" if item.is_construction else "UPGRADE"
                        self._report(
                            f"  {action} FAILED: {item.building} (slot {item.slot_id}) - {result.raw_response[:200] if result.raw_response else 'unknown error'}"
                        )
                        continue
                    if result.success:
                        next_level = item.current_level + 1
                        self._report(
                            f"STARTED: {item.building} Lv{item.current_level}->{next_level} ({result.construction_time})"
                        )
                        all_results.append(
                            {
                                "building": item.building,
                                "level": f"{item.current_level}->{next_level}",
                                "status": "started",
                                "time": result.construction_time,
                            }
                        )

                        # Try video speedup
                        if use_video and detail:
                            try:
                                from .video_reward_service import VideoRewardService

                                vrs = VideoRewardService(self.http_client)
                                try:
                                    vr = await vrs.claim_reward(
                                        "buildingUpgrade",
                                        villageId=plan.village_id,
                                        slotId=item.slot_id,
                                        buildingId=detail.gid,
                                    )
                                    if vr.success:
                                        self._report("  VIDEO: Speed-up applied!")
                                    else:
                                        self._report(f"  VIDEO: Failed - {vr.message}")
                                finally:
                                    await vrs.close()
                            except Exception as e:
                                self._report(f"  VIDEO: Error - {e}")

                        if item.is_construction:
                            # After construction, building exists — switch to upgrade mode
                            item.is_construction = False
                            item.construct_gid = 0

                        built = True

                        # Wait for the build to actually register in the queue,
                        # then sleep until it finishes (no wasteful polling).
                        await asyncio.sleep(
                            HumanTiming.reaction_time()
                        )  # grace period for server to register
                        while not await self.is_queue_empty(village_id=vid):
                            remaining = await self.get_queue_remaining(village_id=vid)
                            self._report(
                                f"Build in progress ({remaining}s remaining). Sleeping until done..."
                            )
                            wait = max(remaining + 3, 5)
                            await asyncio.sleep(HumanTiming.micro_jitter(wait, jitter_pct=0.05))
                            # Stealth: occasional idle browsing during long waits
                            try:
                                noise = getattr(self.http_client, "noise_injector", None)
                                if noise:
                                    await noise.maybe_inject_noise(village_id=vid)
                            except Exception:
                                pass  # stealth failures shouldn't break the builder

                        # Level-sync guard: re-read the actual level from the server
                        # rather than trusting the in-memory tracker.  This prevents
                        # drift when a plan is restarted mid-build or the server
                        # state diverges from our bookkeeping.
                        actual_level = next_level  # fallback
                        try:
                            buildings = await self.building_service.get_village_buildings(
                                village_id=vid
                            )
                            for b in buildings:
                                if b.slot_id == item.slot_id:
                                    actual_level = b.level
                                    break
                        except Exception as e:
                            self._report(f"  WARNING: Could not re-read level from server: {e}")

                        if actual_level != next_level:
                            self._report(
                                f"  LEVEL SYNC: Server reports Lv{actual_level} "
                                f"(tracker expected Lv{next_level}). Using server value."
                            )

                        # Post-build guard: the server-reported level sometimes
                        # lags behind (caching).  Cross-check against the live
                        # construction queue — if the building's target level in
                        # the queue already meets our plan target, mark it done
                        # immediately rather than issuing a redundant upgrade.
                        queued_target = None
                        try:
                            post_queue = await self.building_service.get_construction_queue(
                                village_id=vid
                            )
                            for q in post_queue:
                                if (
                                    q.building_name
                                    and item.building
                                    and q.building_name.lower() == item.building.lower()
                                ):
                                    queued_target = q.target_level
                                    break
                        except Exception:
                            pass

                        if queued_target is not None and queued_target >= item.target:
                            item.current_level = queued_target
                            item.status = "done"
                            self._report(
                                f"  DONE: {item.building} upgrade to Lv{queued_target} in progress/complete (target was {item.target})"
                            )
                        else:
                            item.current_level = actual_level
                            if item.current_level >= item.target:
                                item.status = "done"
                                self._report(
                                    f"  DONE: {item.building} reached Lv{item.current_level} (target was {item.target})"
                                )
                            else:
                                self._report(
                                    f"  PROGRESS: {item.building} now Lv{item.current_level}, continuing to Lv{item.target}"
                                )

                        break

            # Log activity for scheduler tracking
            try:
                self.http_client.activity_scheduler.log_activity(poll_interval_s)
            except Exception:
                pass

            if not built:
                # Distinguish "no money" from "no upgrade URL on build page" —
                # the second is a transient Travian-state issue, the first is
                # an economic wait. A real player who can't afford a build
                # plans and returns later; they don't poll every 30s.
                # Wait selection is extracted to _resolve_idle_wait (pure +
                # tested). The sleep applies ONLY tempo_scale — never another
                # HumanTiming.delay() (that double-application once compounded
                # the resource-short pause into 45+ min frozen waits).
                reason, wait_s = _resolve_idle_wait(
                    any_no_checksum=any_no_checksum,
                    any_resource_short=any_resource_short,
                    next_prio=next_prio,
                    poll_interval_s=poll_interval_s,
                    stealth_enabled=getattr(self.http_client, "stealth_enabled", False),
                )
                self._report(f"{reason}. Waiting {wait_s:.0f}s...")
                await asyncio.sleep(self.http_client.tempo_scale(wait_s))

        return all_results
