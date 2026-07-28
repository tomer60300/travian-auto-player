"""Building service for Travian API."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..clients.http_client import HttpClient
from ..concurrency import KeyedLock
from ..constants import BUILDING_NAMES
from ..debug_dump import debug_dumper
from ..exceptions import TravianError
from ..models.buildings import Building, BuildingDetail, QueueItem, Resources, UpgradeResult
from ..parsers.html_parser import (
    parse_build_page,
    parse_construction_queue,
    parse_dorf1,
    parse_dorf2,
    parse_empty_slot_buildings,
    parse_resources,
    parse_village_stats_capacity,
    parse_village_stats_production,
    parse_village_stats_resources,
)

logger = logging.getLogger(__name__)


class BuildingService:
    """Service for managing buildings and resources."""

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client
        # Serializes upgrade/construct calls for the same (village_id, slot_id).
        # Prevents double-upgrade races when two clients hit the same slot.
        self._slot_lock = KeyedLock()

    async def get_village_snapshot(
        self, village_id: Optional[int] = None
    ) -> tuple[List[Building], Resources]:
        """
        Get all buildings AND current resources from one dorf1/dorf2 pair.

        Resources live in an inline ``var resources`` blob carried by every
        page, so they parse out of the dorf1 HTML the building list already
        needs. Callers wanting both must use this rather than pairing
        get_village_buildings() with get_resources(), which fetches dorf1.php
        a second time -- an entire extra round trip through the throttler per
        village.

        Args:
            village_id: Village ID (switches village if set)

        Returns:
            Tuple of (buildings, resources)

        Raises:
            TravianError: If request fails
        """
        buildings = []
        newdid = f"?newdid={village_id}" if village_id else ""

        try:
            # Get resource fields from dorf1.php
            dorf1_html = await self.http_client.get_html(f"/dorf1.php{newdid}")
            resource_fields = parse_dorf1(dorf1_html)

            for field in resource_fields:
                buildings.append(
                    Building(
                        slot_id=field["slot_id"],
                        gid=field["gid"],
                        name=field["name"],
                        level=field["level"],
                    )
                )

            # Get village buildings from dorf2.php
            dorf2_html = await self.http_client.get_html(f"/dorf2.php{newdid}")
            village_buildings = parse_dorf2(dorf2_html)

            for building in village_buildings:
                buildings.append(
                    Building(
                        slot_id=building["slot_id"],
                        gid=building["gid"],
                        name=building["name"],
                        level=building["level"],
                    )
                )

            # Same dorf1-then-dorf2 fallback get_resources() uses, but both
            # pages are already in hand -- no additional request.
            resources = parse_resources(dorf1_html)
            if not any([resources.lumber, resources.clay, resources.iron, resources.crop]):
                resources = parse_resources(dorf2_html)

            return buildings, resources

        except Exception as e:
            raise TravianError(f"Failed to get village buildings: {e}") from e

    async def get_village_buildings(self, village_id: Optional[int] = None) -> List[Building]:
        """
        Get all buildings in the village.

        Args:
            village_id: Village ID (unused for now, uses current village)

        Returns:
            List of Building objects

        Raises:
            TravianError: If request fails
        """
        buildings, _ = await self.get_village_snapshot(village_id)
        return buildings

    async def get_all_villages_resources(self) -> Dict[int, Resources]:
        """
        Get stocks, production and capacity for EVERY village in three requests.

        The /village/statistics tables are account-wide: each renders one row
        per village regardless of which village is active. Three fetches cover
        the whole account, versus one dorf1 fetch per village.

        Note: ``free_crop`` is not exposed by these tables (it lives only in the
        per-page ``var resources`` blob) and is left at 0.

        Returns:
            Dict of village_id -> Resources

        Raises:
            TravianError: If request fails
        """
        try:
            stocks = parse_village_stats_resources(
                await self.http_client.get_html("/village/statistics/resources")
            )
            production = parse_village_stats_production(
                await self.http_client.get_html("/village/statistics/resources/production")
            )
            capacity = parse_village_stats_capacity(
                await self.http_client.get_html("/village/statistics/resources/capacity")
            )
        except Exception as e:
            raise TravianError(f"Failed to get village statistics: {e}") from e

        resources: Dict[int, Resources] = {}
        for village_id, stock in stocks.items():
            rates = production.get(village_id, {})
            caps = capacity.get(village_id, {})
            warehouse = caps.get("warehouse", 0)
            resources[village_id] = Resources(
                lumber=stock["lumber"],
                clay=stock["clay"],
                iron=stock["iron"],
                crop=stock["crop"],
                max_lumber=warehouse,
                max_clay=warehouse,
                max_iron=warehouse,
                max_crop=caps.get("granary", 0),
                lumber_per_hour=rates.get("lumber", 0),
                clay_per_hour=rates.get("clay", 0),
                iron_per_hour=rates.get("iron", 0),
                crop_per_hour=rates.get("crop", 0),
            )
        return resources

    async def get_building_detail(
        self, slot_id: int, village_id: Optional[int] = None
    ) -> BuildingDetail:
        """
        Get detailed information about a specific building.

        Args:
            slot_id: Building slot ID (1-40)
            village_id: Village ID (switches village if set)

        Returns:
            BuildingDetail object

        Raises:
            TravianError: If request fails
        """
        try:
            # Buildings with tabs (Rally Point, Marketplace, Embassy, Residence,
            # Palace, etc.) remember the user's favourite tab server-side, so a
            # bare `build.php?id=N` can land on a tab that has no upgrade
            # button. Rally Point with the Farm-List tab favourited is the
            # canonical case. `tt=0` pins the request to the Management tab
            # where the upgrade URL/checksum always lives. Single-tab
            # buildings and resource fields ignore the parameter.
            url = f"/build.php?id={slot_id}&tt=0"
            if village_id:
                url = f"/build.php?newdid={village_id}&id={slot_id}&tt=0"
            build_html = await self.http_client.get_html(url)
            detail = parse_build_page(build_html, slot_id=slot_id)
            if not detail.checksum:
                debug_dumper.dump(
                    "no_checksum",
                    build_html,
                    key=f"slot{slot_id}_v{village_id or 'NA'}",
                    context={
                        "slot_id": slot_id,
                        "village_id": village_id,
                        "url": url,
                        "parsed_name": detail.name,
                        "parsed_level": detail.level,
                        "parsed_gid": detail.gid,
                    },
                )
            return detail

        except Exception as e:
            raise TravianError(f"Failed to get building detail for slot {slot_id}: {e}") from e

    async def find_building(
        self, name_or_gid: str | int, village_id: Optional[int] = None
    ) -> Optional[Building]:
        """
        Find a building by name or GID.

        Args:
            name_or_gid: Building name (partial match) or GID number
            village_id: Village ID (switches village if set)

        Returns:
            First matching Building or None
        """
        buildings = await self.get_village_buildings(village_id=village_id)

        if isinstance(name_or_gid, int):
            # Search by GID
            for building in buildings:
                if building.gid == name_or_gid:
                    return building
        else:
            # Search by name (case-insensitive partial match)
            search_term = str(name_or_gid).lower()
            for building in buildings:
                if search_term in building.name.lower():
                    return building

        return None

    async def upgrade_building(
        self, slot_id: int, allow_gold: bool = False, village_id: Optional[int] = None
    ) -> UpgradeResult:
        """
        Upgrade a building.

        SAFETY: Will refuse to upgrade if construction queue is occupied
        unless allow_gold=True (master builder costs 1 gold per extra slot).

        Args:
            slot_id: Building slot ID to upgrade
            allow_gold: If True, allow spending gold on master builder.
                        Default False — NEVER spend gold without explicit consent.

        Returns:
            UpgradeResult object

        Raises:
            TravianError: If upgrade fails or would cost gold
        """
        async with self._slot_lock((village_id, slot_id)):
            return await self._upgrade_building_unlocked(slot_id, allow_gold, village_id)

    async def _upgrade_building_unlocked(
        self, slot_id: int, allow_gold: bool, village_id: Optional[int]
    ) -> UpgradeResult:
        try:
            # SAFETY CHECK: Check construction queue BEFORE upgrading
            # If queue is occupied and allow_gold is False, REFUSE.
            # EXCEPTION: Romans have dual build queue — they can build one resource
            # field (slot 1-18) and one building (slot 19-40) simultaneously without gold.
            queue = await self.get_construction_queue(village_id=village_id)
            if queue and not allow_gold:
                # Check if Roman dual-queue allows this build
                is_resource_field = slot_id <= 18
                queue_has_resource = any(
                    getattr(q, "slot_id", 0) <= 18 for q in queue if hasattr(q, "slot_id")
                )
                queue_has_building = any(
                    getattr(q, "slot_id", 0) > 18 for q in queue if hasattr(q, "slot_id")
                )

                # If we can't determine slot_ids from queue, block by default.
                # Without slot data we can't verify Roman dual-queue eligibility,
                # so the safe choice is to prevent accidental gold usage.
                if not any(hasattr(q, "slot_id") for q in queue):
                    roman_dual_ok = False
                else:
                    # We know slot types — Roman can add if different category
                    roman_dual_ok = (is_resource_field and not queue_has_resource) or (
                        not is_resource_field and not queue_has_building
                    )

                if not roman_dual_ok:
                    queue_names = ", ".join(f"{q.building_name} Lv{q.target_level}" for q in queue)
                    return UpgradeResult(
                        success=False,
                        village_id=0,
                        building_id=slot_id,
                        building_name="Unknown",
                        old_level=0,
                        new_level=0,
                        construction_time="",
                        reward_used=False,
                        raw_response=f"BLOCKED: Construction queue already has [{queue_names}]. "
                        f"Upgrading now would cost gold (master builder). "
                        f"Use allow_gold=True to override.",
                    )

            # Stealth: simulate realistic navigation flow before upgrading
            if getattr(self.http_client, "stealth_enabled", False):
                await self.http_client.navigator.pre_upgrade_flow(slot_id, village_id)

            # Get building details to extract checksum and upgrade URL
            building_detail = await self.get_building_detail(slot_id, village_id=village_id)

            if not building_detail.checksum:
                raise TravianError(f"No upgrade available for building in slot {slot_id}")

            # Construct upgrade URL — use the section1 (normal) upgrade button URL.
            # Do NOT add &buildmaster unless allow_gold is True (master builder costs gold
            # when queue is occupied, and may silently fail on non-Plus accounts).
            upgrade_url = building_detail.upgrade_url
            if not upgrade_url:
                dorf_page = "dorf1" if slot_id <= 18 else "dorf2"
                upgrade_url = f"/{dorf_page}.php?id={slot_id}&gid={building_detail.gid}&action=build&checksum={building_detail.checksum}"

            # Only add &buildmaster when explicitly allowed (master builder / gold usage)
            if allow_gold and "&buildmaster" not in upgrade_url:
                upgrade_url += "&buildmaster"

            # Add village context if needed
            if village_id and f"newdid={village_id}" not in upgrade_url:
                sep = "&" if "?" in upgrade_url else "?"
                upgrade_url += f"{sep}newdid={village_id}"

            # Perform upgrade by GET request to the URL
            response_html = await self.http_client.get_html(
                upgrade_url, skip_reauth=True, safe_to_retry=False
            )

            # Success detection: after upgrade, server redirects to dorf1/dorf2 page
            # which shows the construction queue with the new item.
            import re

            has_queue_item = bool(re.search(r"showCancelBuildingDialog", response_html))
            has_error_dialog = bool(re.search(r'class="errorMessage"', response_html))
            not_enough = "notEnough" in response_html or "not_enough" in response_html
            # Also check for buildDuration (queue timer) as alternative success indicator
            has_build_duration = bool(re.search(r"buildDuration|underConstruction", response_html))

            success = (
                (has_queue_item or has_build_duration) and not has_error_dialog and not not_enough
            )

            return UpgradeResult(
                success=success,
                village_id=0,
                building_id=slot_id,
                building_name=building_detail.name,
                old_level=building_detail.level,
                new_level=building_detail.level + 1 if success else building_detail.level,
                construction_time=building_detail.construction_time,
                reward_used=False,
                raw_response="" if success else response_html[:500],
            )

        except Exception as e:
            return UpgradeResult(
                success=False,
                village_id=0,
                building_id=slot_id,
                building_name="Unknown",
                old_level=0,
                new_level=0,
                construction_time="",
                reward_used=False,
                raw_response=str(e),
            )

    async def get_available_buildings(
        self, slot_id: int, village_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get available buildings for an empty slot.

        Args:
            slot_id: Empty building slot ID (19-40)
            village_id: Village ID (switches village if set)

        Returns:
            List of available building dicts with gid, name, checksum, build_url, costs, can_build
        """
        url = f"/build.php?id={slot_id}"
        if village_id:
            url = f"/build.php?newdid={village_id}&id={slot_id}"
        html = await self.http_client.get_html(url)
        return parse_empty_slot_buildings(html, slot_id=slot_id)

    async def construct_building(
        self,
        slot_id: int,
        building_gid: int,
        allow_gold: bool = False,
        village_id: Optional[int] = None,
    ) -> UpgradeResult:
        """
        Construct a new building on an empty slot.

        Args:
            slot_id: Empty building slot ID (19-40)
            building_gid: GID of the building to construct
            allow_gold: If True, allow spending gold on master builder
            village_id: Village ID (switches village if set)

        Returns:
            UpgradeResult object
        """
        async with self._slot_lock((village_id, slot_id)):
            return await self._construct_building_unlocked(
                slot_id, building_gid, allow_gold, village_id
            )

    async def _construct_building_unlocked(
        self,
        slot_id: int,
        building_gid: int,
        allow_gold: bool,
        village_id: Optional[int],
    ) -> UpgradeResult:
        try:
            # Gold guard — same as upgrade_building
            queue = await self.get_construction_queue(village_id=village_id)
            if queue and not allow_gold:
                queue_names = ", ".join(f"{q.building_name} Lv{q.target_level}" for q in queue)
                return UpgradeResult(
                    success=False,
                    village_id=0,
                    building_id=slot_id,
                    building_name="Unknown",
                    old_level=0,
                    new_level=0,
                    construction_time="",
                    reward_used=False,
                    raw_response=f"BLOCKED: Construction queue already has [{queue_names}]. "
                    f"Use allow_gold=True to override.",
                )

            # Fetch available buildings for this empty slot
            available = await self.get_available_buildings(slot_id, village_id=village_id)
            target = None
            for b in available:
                if b["gid"] == building_gid:
                    target = b
                    break

            if not target:
                available_names = ", ".join(
                    f"{b['name']} (gid={b['gid']})" for b in available if b["can_build"]
                )
                return UpgradeResult(
                    success=False,
                    village_id=0,
                    building_id=slot_id,
                    building_name=BUILDING_NAMES.get(building_gid, f"gid={building_gid}"),
                    old_level=0,
                    new_level=0,
                    construction_time="",
                    reward_used=False,
                    raw_response=f"Building gid={building_gid} not available on slot {slot_id}. "
                    f"Available: {available_names}",
                )

            if not target["can_build"]:
                return UpgradeResult(
                    success=False,
                    village_id=0,
                    building_id=slot_id,
                    building_name=target["name"],
                    old_level=0,
                    new_level=0,
                    construction_time="",
                    reward_used=False,
                    raw_response=f"{target['name']} requirements not met (no construct button).",
                )

            # Stealth: a real player constructing a NEW building opens the
            # village center, clicks the empty slot, reviews the available
            # building list, then clicks Build. upgrade_building uses
            # pre_upgrade_flow for the same purpose; mirror it here so the
            # construct path doesn't deep-link into build.php with a stale
            # Referer chain. Run AFTER the queue/gold and "is target
            # available" guards so the pre-flow doesn't waste page loads
            # on requests that are about to be rejected anyway.
            navigator = getattr(self.http_client, "navigator", None)
            if navigator is not None and navigator.enabled:
                try:
                    await navigator.pre_construct_flow(slot_id, village_id=village_id)
                except Exception as exc:
                    logger.debug("pre_construct_flow noise failed (non-critical): %s", exc)

            build_url = target["build_url"]
            if village_id and f"newdid={village_id}" not in build_url:
                sep = "&" if "?" in build_url else "?"
                build_url += f"{sep}newdid={village_id}"

            response_html = await self.http_client.get_html(
                build_url, skip_reauth=True, safe_to_retry=False
            )

            import re

            has_queue_item = bool(re.search(r"showCancelBuildingDialog", response_html))
            has_build_duration = bool(re.search(r"buildDuration|underConstruction", response_html))
            has_error = bool(re.search(r'class="errorMessage"', response_html))
            not_enough = "notEnough" in response_html or "not_enough" in response_html

            success = (has_queue_item or has_build_duration) and not has_error and not not_enough

            return UpgradeResult(
                success=success,
                village_id=0,
                building_id=slot_id,
                building_name=target["name"],
                old_level=0,
                new_level=1 if success else 0,
                construction_time="",
                reward_used=False,
                raw_response="" if success else response_html[:500],
            )

        except Exception as e:
            return UpgradeResult(
                success=False,
                village_id=0,
                building_id=slot_id,
                building_name=BUILDING_NAMES.get(building_gid, "Unknown"),
                old_level=0,
                new_level=0,
                construction_time="",
                reward_used=False,
                raw_response=str(e),
            )

    async def get_construction_queue(self, village_id: Optional[int] = None) -> List[QueueItem]:
        """
        Get current construction queue.

        Args:
            village_id: Village ID (switches village if set)

        Returns:
            List of QueueItem objects

        Raises:
            TravianError: If request fails
        """
        try:
            newdid = f"?newdid={village_id}" if village_id else ""
            # Construction queue is typically shown on dorf1 or dorf2
            # Try dorf2 first as it usually has the buildingList
            dorf2_html = await self.http_client.get_html(f"/dorf2.php{newdid}")
            queue = parse_construction_queue(dorf2_html)

            if not queue:
                # Try dorf1 as fallback
                dorf1_html = await self.http_client.get_html(f"/dorf1.php{newdid}")
                queue = parse_construction_queue(dorf1_html)

            return queue

        except Exception as e:
            raise TravianError(f"Failed to get construction queue: {e}") from e

    async def use_construction_reward(self) -> bool:
        """
        Use video reward to speed up construction.

        Returns:
            True if successful, False otherwise

        Note:
            This is a TODO - video reward flow needs implementation
        """
        # TODO: Implement video reward flow
        # POST /api/v1/videofeature/open/buildingUpgrade -> {vrid, videoIframeUrl}
        # POST /api/v1/videofeature/start -> {vrid}
        # POST /api/v1/videofeature/ends -> {vrid, hash}

        raise NotImplementedError("Video reward flow not yet implemented")

    async def get_resources(self, village_id: Optional[int] = None) -> Resources:
        """
        Get current village resources.

        Args:
            village_id: Village ID (switches village if set)

        Returns:
            Resources object

        Raises:
            TravianError: If request fails
        """
        try:
            newdid = f"?newdid={village_id}" if village_id else ""
            # Resources are typically shown on dorf1 and dorf2
            dorf1_html = await self.http_client.get_html(f"/dorf1.php{newdid}")
            resources = parse_resources(dorf1_html)

            # If not found, try dorf2
            if not any([resources.lumber, resources.clay, resources.iron, resources.crop]):
                dorf2_html = await self.http_client.get_html(f"/dorf2.php{newdid}")
                resources = parse_resources(dorf2_html)

            return resources

        except Exception as e:
            raise TravianError(f"Failed to get resources: {e}") from e
