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
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable

from ..clients.http_client import HttpClient
from ..constants import BUILDING_NAMES
from ..exceptions import TravianError
from ..logging_config import get_logger
from .building_service import BuildingService

logger = get_logger(__name__)


@dataclass
class BuildPlanItem:
    """Single item in the build plan."""
    building: str       # Building name (partial match), empty if slot specified
    target: int         # Target level
    priority: int       # 1=highest, 5=lowest
    slot: int = 0       # Explicit slot ID from YAML (0 = resolve by name)
    expect: str = ""    # Optional: expected building name for slot (safety guard)
    slot_id: int = 0    # Resolved slot ID (filled at runtime)
    current_level: int = 0
    status: str = "pending"  # pending | building | done | skipped
    is_construction: bool = False  # True if building needs to be constructed (empty slot)
    construct_gid: int = 0        # GID of building to construct


@dataclass
class BuildPlan:
    """Full build plan for a village."""
    village_id: int
    items: List[BuildPlanItem]
    
    @classmethod
    def from_file(cls, path: str | Path) -> "BuildPlan":
        """Load build plan from YAML file. Tabs are converted to spaces automatically."""
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
        # YAML forbids tabs for indentation — silently convert to 2 spaces
        raw = raw.replace('\t', '  ')
        data = yaml.safe_load(raw)
        
        village_id = data.get('village', data.get('village_id', 0))
        items = []
        for entry in data.get('plan', []):
            items.append(BuildPlanItem(
                building=entry.get('building', ''),
                target=entry.get('target', entry.get('level', 1)),
                priority=entry.get('priority', 5),
                slot=entry.get('slot', 0),
                expect=entry.get('expect', ''),
            ))
        
        return cls(village_id=village_id, items=items)
    
    def pending_items(self) -> List[BuildPlanItem]:
        """Get items not yet done, sorted by priority."""
        return sorted(
            [i for i in self.items if i.status == "pending"],
            key=lambda x: x.priority
        )
    
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
    
    def on_status(self, callback: Callable[[str], None]):
        """Set status callback for progress reporting."""
        self._on_status = callback
    
    def _report(self, msg: str):
        """Report status."""
        logger.info(msg)
        if self._on_status:
            self._on_status(msg)
    
    async def resolve_slots(self, plan: BuildPlan):
        """Resolve building names to slot IDs."""
        buildings = await self.building_service.get_village_buildings(village_id=plan.village_id or None)
        
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
                            name_to_gid = {v.lower(): k for k, v in BUILDING_NAMES.items()}
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
                                self._report(f"WARNING: Unknown building '{item.building}' for empty slot {item.slot}")
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
                name_to_gid = {v.lower(): k for k, v in BUILDING_NAMES.items()}
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
                    self._report(f"WARNING: Could not find '{item.building}' in village or building list")
                    item.status = "skipped"
            elif item.current_level >= item.target:
                self._report(f"SKIP: {item.building} already at level {item.current_level} (target {item.target})")
                item.status = "done"
            else:
                self._report(f"FOUND: {item.building} at slot {item.slot_id} (Lv{item.current_level} -> {item.target})")
    
    async def check_resources(self, slot_id: int, village_id: Optional[int] = None,
                              construct_gid: int = 0) -> Dict[str, Any]:
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
            available = await self.building_service.get_available_buildings(slot_id, village_id=village_id)
            resources = await self.building_service.get_resources(village_id=village_id)
            target_building = None
            for b in available:
                if b['gid'] == construct_gid:
                    target_building = b
                    break
            costs = target_building['costs'] if target_building else {}
            can_construct = bool(target_building and target_building['can_build'])
            current = {
                'lumber': resources.lumber,
                'clay': resources.clay,
                'iron': resources.iron,
                'crop': resources.crop,
            }
            missing = {}
            for res, needed in costs.items():
                if res in current and current[res] < needed:
                    missing[res] = needed - current[res]
            return {
                'can_build': can_construct and len(missing) == 0,
                'has_checksum': can_construct,
                'missing': missing,
                'costs': costs,
                'current': current,
            }

        detail = await self.building_service.get_building_detail(slot_id, village_id=village_id)
        resources = await self.building_service.get_resources(village_id=village_id)

        costs = detail.costs or {}
        current = {
            'lumber': resources.lumber,
            'clay': resources.clay,
            'iron': resources.iron,
            'crop': resources.crop,
        }

        missing = {}
        for res, needed in costs.items():
            if res in current and current[res] < needed:
                missing[res] = needed - current[res]

        return {
            'can_build': len(missing) == 0 and detail.checksum is not None,
            'has_checksum': detail.checksum is not None,
            'missing': missing,
            'costs': costs,
            'current': current,
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
        return max(q.remaining_seconds for q in queue) if queue else 0
    
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
                        item.slot_id, village_id=plan.village_id or None,
                        construct_gid=item.construct_gid if item.is_construction else 0,
                    )
                    ready = "READY" if check['can_build'] else f"WAITING (missing: {check['missing']})"
                    action = "CONSTRUCT " if item.is_construction else ""
                    self._report(f"  {action}{item.building} Lv{item.current_level}->{item.target} "
                                f"(slot {item.slot_id}, cost: {check['costs']}) [{ready}]")
                    results.append({
                        'building': item.building,
                        'slot_id': item.slot_id,
                        'level': f"{item.current_level} -> {item.target}",
                        'status': 'dry_run',
                        'ready': check['can_build'],
                        'missing': check['missing'],
                    })
            return results
        
        while True:
            next_prio = plan.next_priority()
            if next_prio is None:
                self._report("All items completed!")
                break
            
            # Get all items at this priority level
            prio_items = [i for i in plan.items 
                         if i.priority == next_prio and i.status == "pending"]
            
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
                    await asyncio.sleep(wait_time)
                    waited += wait_time
                    continue
                
                # Queue is empty — check which item has resources
                for item in prio_items:
                    if item.status != "pending":
                        continue
                    
                    check = await self.check_resources(
                        item.slot_id, village_id=vid,
                        construct_gid=item.construct_gid if item.is_construction else 0,
                    )

                    if check['can_build']:
                        self._report(f"BUILDING: {item.building} Lv{item.current_level} -> {item.target}"
                                     f" (slot {item.slot_id}, costs: {check['costs']})")

                        if dry_run:
                            self._report("  (dry run - would build)")
                            item.status = "done"
                            results.append({
                                'building': item.building,
                                'slot_id': item.slot_id,
                                'level': f"{item.current_level} -> {item.target}",
                                'status': 'dry_run',
                            })
                            built_one = True
                            break

                        # Actually upgrade or construct
                        if item.is_construction:
                            result = await self.building_service.construct_building(
                                item.slot_id, item.construct_gid, allow_gold=False, village_id=vid,
                            )
                        else:
                            result = await self.building_service.upgrade_building(item.slot_id, allow_gold=False, village_id=vid)
                        
                        if result.success:
                            item.status = "done"
                            self._report(f"  Started! Time: {result.construction_time}")
                            results.append({
                                'building': item.building,
                                'slot_id': item.slot_id,
                                'level': f"{item.current_level} -> {item.target}",
                                'status': 'started',
                                'time': result.construction_time,
                            })
                            built_one = True
                        else:
                            self._report(f"  FAILED: {result.raw_response[:100]}")
                            # If blocked by gold guard, it means queue wasn't actually empty
                            if 'BLOCKED' in (result.raw_response or ''):
                                break  # Re-check queue
                            item.status = "skipped"
                            results.append({
                                'building': item.building,
                                'slot_id': item.slot_id,
                                'status': 'failed',
                                'error': result.raw_response[:200],
                            })
                        break
                    else:
                        missing_str = ', '.join(f"{k}: {v}" for k, v in check['missing'].items())
                        if not check['has_checksum']:
                            self._report(f"  {item.building}: no checksum (requirements not met?)")
                        # Don't spam — only log first time
                
                if not built_one:
                    self._report(f"No items ready at priority {next_prio}. "
                                f"Waiting {poll_interval_s}s for resources...")
                    await asyncio.sleep(poll_interval_s)
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
        """
        Execute entire plan continuously — waits for each build to finish
        before starting next.

        This is the main entry point for the auto-builder.

        Args:
            plan: Build plan to execute
            poll_interval_s: How often to check conditions (seconds)
            use_video: If True, claim buildingUpgrade video reward after each upgrade
            verbose: If True, show current resources and detailed cost breakdown
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
                self._report(f"  Queue not empty ({remaining}s remaining)...")
                wait = min(remaining + 5, 60)
                await asyncio.sleep(wait)

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
                self._report(f"  {pi.building} Lv{pi.current_level} -> {pi.target} (slot {pi.slot_id})")

            # Wait for queue to be empty
            while not await self.is_queue_empty(village_id=vid):
                remaining = await self.get_queue_remaining(village_id=vid)
                self._report(f"Waiting for queue ({remaining}s)...")
                wait = min(remaining + 5, 60)
                await asyncio.sleep(wait)

            # Show current resources if verbose
            if verbose:
                resources = await self.building_service.get_resources(village_id=vid)
                self._report(f"  Resources: L={resources.lumber} C={resources.clay} I={resources.iron} Cr={resources.crop} (free crop: {resources.free_crop})")

            # Try each item at this priority
            built = False
            for item in prio_items:
                try:
                    check = await self.check_resources(
                        item.slot_id, village_id=vid,
                        construct_gid=item.construct_gid if item.is_construction else 0,
                    )
                except Exception as e:
                    self._report(f"  {item.building} (slot {item.slot_id}): error checking - {e}")
                    continue
                if verbose:
                    costs = check.get('costs', {})
                    missing = check.get('missing', {})
                    can = check.get('can_build', False)
                    costs_str = ', '.join(f"{k}={v}" for k, v in costs.items()) if costs else 'n/a'
                    if can:
                        self._report(f"  {item.building} (slot {item.slot_id}): READY costs({costs_str})")
                    else:
                        missing_str = ', '.join(f"{k}={v}" for k, v in missing.items()) if missing else 'none'
                        self._report(f"  {item.building} (slot {item.slot_id}): costs({costs_str}) missing({missing_str})")
                if check['can_build']:
                    # Get building detail for gid (needed for video reward)
                    detail = await self.building_service.get_building_detail(item.slot_id, village_id=vid) if use_video and not item.is_construction else None

                    if item.is_construction:
                        result = await self.building_service.construct_building(
                            item.slot_id, item.construct_gid, allow_gold=False, village_id=vid,
                        )
                    else:
                        result = await self.building_service.upgrade_building(item.slot_id, allow_gold=False, village_id=vid)
                    if not result.success:
                        action = "CONSTRUCT" if item.is_construction else "UPGRADE"
                        self._report(f"  {action} FAILED: {item.building} (slot {item.slot_id}) - {result.raw_response[:200] if result.raw_response else 'unknown error'}")
                        continue
                    if result.success:
                        next_level = item.current_level + 1
                        self._report(f"STARTED: {item.building} Lv{item.current_level}->{next_level} ({result.construction_time})")
                        all_results.append({
                            'building': item.building,
                            'level': f"{item.current_level}->{next_level}",
                            'status': 'started',
                            'time': result.construction_time,
                        })

                        # Try video speedup
                        if use_video and detail:
                            try:
                                from .video_reward_service import VideoRewardService
                                vrs = VideoRewardService(self.http_client)
                                try:
                                    vr = await vrs.claim_reward(
                                        'buildingUpgrade',
                                        villageId=plan.village_id,
                                        slotId=item.slot_id,
                                        buildingId=detail.gid,
                                    )
                                    if vr.success:
                                        self._report(f"  VIDEO: Speed-up applied!")
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
                        # then wait for it to finish before starting the next one.
                        await asyncio.sleep(2)  # small grace period for server to register
                        while not await self.is_queue_empty(village_id=vid):
                            remaining = await self.get_queue_remaining(village_id=vid)
                            self._report(f"Waiting for build to finish ({remaining}s remaining)...")
                            wait = min(remaining + 5, 60)
                            await asyncio.sleep(wait)

                        # Re-read actual level from server (authoritative source of truth).
                        # This prevents drift when a plan is restarted mid-build or the
                        # server state diverges from the in-memory tracker.
                        actual_level = next_level  # fallback
                        try:
                            buildings = await self.building_service.get_village_buildings(village_id=vid)
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

                        item.current_level = actual_level
                        if item.current_level >= item.target:
                            item.status = "done"
                            self._report(f"  DONE: {item.building} reached Lv{item.current_level} (target was {item.target})")
                        else:
                            self._report(f"  PROGRESS: {item.building} now Lv{item.current_level}, continuing to Lv{item.target}")

                        break

            if not built:
                # No resources for any item at this priority — wait and retry
                self._report(f"Insufficient resources for priority {next_prio} items. Waiting {poll_interval_s}s...")
                await asyncio.sleep(poll_interval_s)

        return all_results
