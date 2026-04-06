"""Farm list service for Travian API — CRUD, querying, and sending raids."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ..clients.http_client import HttpClient
from ..models.farm_list import (
    FarmList,
    FarmListSlot,
    FarmListSendResult,
    FarmListSendTargetResult,
    SlotTroop,
)

logger = logging.getLogger(__name__)

# GraphQL fragment used for every farm-list fetch
_FARM_LIST_FRAGMENT = """
fragment farmListFragment on FarmList {
    id
    name
    runningRaidsAmount
    isExpanded
    sortIndex
    lastStartedTime
    sortField
    sortDirection
    useShip
    onlyLosses
    ownerVillage {
        id
        troops {
            ownTroopsAtTown {
                units { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
            }
        }
    }
    defaultTroop { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
    slots(onlyExpanded: $onlyExpanded) {
        id
        target { id mapId x y name type population }
        troop { t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 }
        distance
        isActive
        isRunning
        isSpying
        runningAttacks
        nextAttackAt
        lastRaid {
            reportObjectId
            authKey
            time
            raidedResources { lumber clay iron crop }
            bootyMax
            icon
        }
        totalBooty { booty raids }
    }
    slotsAmount
}
"""


class FarmListService:
    """Service for managing farm lists (CRUD + send)."""

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client

    # ── Queries ──────────────────────────────────────────────────────

    async def get_all_farm_lists(self) -> List[FarmList]:
        """Fetch all farm lists via GraphQL."""
        query = (
            _FARM_LIST_FRAGMENT
            + """
            query($onlyExpanded: Boolean!) {
                ownPlayer {
                    farmLists { ...farmListFragment }
                    abandonedFarmLists { ...farmListFragment }
                }
            }
            """
        )
        resp = await self.http_client.post_json(
            "/api/v1/graphql",
            {"query": query, "variables": {"onlyExpanded": False}},
        )
        data = resp.get("data", {}).get("ownPlayer", {})
        lists = data.get("farmLists", []) + data.get("abandonedFarmLists", [])
        return [FarmList.model_validate(fl) for fl in lists]

    async def get_farm_list(self, list_id: int) -> FarmList:
        """Fetch a single farm list by ID."""
        query = (
            _FARM_LIST_FRAGMENT
            + """
            query($id: Int!, $onlyExpanded: Boolean!) {
                farmList(id: $id) { ...farmListFragment }
            }
            """
        )
        resp = await self.http_client.post_json(
            "/api/v1/graphql",
            {"query": query, "variables": {"id": list_id, "onlyExpanded": False}},
        )
        data = resp.get("data", {}).get("farmList", {})
        return FarmList.model_validate(data)

    # ── CRUD ─────────────────────────────────────────────────────────

    async def create_farm_list(
        self,
        village_id: int,
        name: str,
        default_units: Optional[Dict[str, int]] = None,
    ) -> int:
        """Create a new farm list. Returns the new list ID."""
        units = default_units or {f"t{i}": 0 for i in range(1, 11)}
        resp = await self.http_client.post_json(
            "/api/v1/farm-list",
            {
                "villageId": village_id,
                "name": name,
                "defaultUnits": units,
                "useShip": False,
                "onlyLosses": False,
            },
        )
        list_id = resp.get("id", 0)
        logger.info(f"Created farm list '{name}' (id={list_id}) for village {village_id}")
        return list_id

    async def delete_farm_list(self, list_id: int) -> None:
        """Delete a farm list."""
        await self.http_client.delete_json(
            f"/api/v1/farm-list/{list_id}",
        )
        logger.info(f"Deleted farm list {list_id}")

    async def add_slot(
        self,
        list_id: int,
        x: int,
        y: int,
        units: Optional[Dict[str, int]] = None,
        active: bool = True,
        force: bool = False,
    ) -> None:
        """Add a target slot to a farm list."""
        slot_units = units or {f"t{i}": 0 for i in range(1, 11)}
        url = "/api/v1/farm-list/slot"
        if force:
            url += "?force"
        await self.http_client.post_json(
            url,
            {
                "slots": [
                    {
                        "listId": list_id,
                        "x": x,
                        "y": y,
                        "units": slot_units,
                        "active": active,
                        "abandoned": False,
                    }
                ]
            },
        )
        logger.info(f"Added slot ({x},{y}) to list {list_id}")

    async def delete_slots(self, slot_ids: List[int]) -> None:
        """Delete slots by IDs."""
        # The DELETE method needs special handling — use post_json but with method override
        # Actually the http_client only has post_json. We need a delete method.
        # For now, use the raw httpx client.
        import httpx
        from urllib.parse import urljoin

        url = urljoin(self.http_client.base_url + "/", "api/v1/farm-list/slot")
        headers = {
            "Content-Type": "application/json",
            "X-Version": self.http_client.settings.x_version,
        }
        resp = await self.http_client.client.request(
            "DELETE", url, json={"slots": slot_ids, "abandoned": False}, headers=headers
        )
        resp.raise_for_status()
        logger.info(f"Deleted slots: {slot_ids}")

    # ── Send ─────────────────────────────────────────────────────────

    async def send_farm_list(
        self,
        list_id: int,
        target_slot_ids: Optional[List[int]] = None,
    ) -> FarmListSendResult:
        """
        Send raids for a farm list.

        Args:
            list_id: Farm list ID
            target_slot_ids: Specific slot IDs to send. If None, sends all active.
        """
        if target_slot_ids is None:
            # Fetch the list to get all active slot IDs
            farm_list = await self.get_farm_list(list_id)
            target_slot_ids = [s.id for s in farm_list.active_slots]

        if not target_slot_ids:
            return FarmListSendResult(targets=[])

        try:
            resp = await self.http_client.post_json(
                "/api/v1/farm-list/send",
                {
                    "action": "farmList",
                    "lists": [{"id": list_id, "targets": target_slot_ids}],
                },
            )
        except Exception as e:
            error_str = str(e)
            # Gold Club error comes as HTTP 400
            if "goldclub" in error_str.lower() or "gold club" in error_str.lower():
                logger.warning("Gold Club not active — cannot send farm lists via API")
                return FarmListSendResult(
                    targets=[
                        FarmListSendTargetResult(
                            id=sid, status="error", error="plus.error_goldclub"
                        )
                        for sid in target_slot_ids
                    ]
                )
            raise

        # Parse response
        error = resp.get("error", "")
        if error:
            logger.warning(f"Farm list send error: {error} — {resp.get('message', '')}")
            return FarmListSendResult(
                targets=[
                    FarmListSendTargetResult(id=sid, status="error", error=error)
                    for sid in target_slot_ids
                ]
            )

        result_lists = resp.get("lists", [])
        targets = []
        if result_lists:
            for t in result_lists[0].get("targets", []):
                targets.append(
                    FarmListSendTargetResult(
                        id=t.get("id", 0),
                        status=t.get("status", "unknown"),
                        error=t.get("error") or "",
                    )
                )
        return FarmListSendResult(targets=targets)

    async def send_all_farm_lists(
        self, list_ids: Optional[List[int]] = None
    ) -> Dict[int, FarmListSendResult]:
        """Send all (or specified) farm lists. Returns dict of list_id -> result."""
        if list_ids is None:
            all_lists = await self.get_all_farm_lists()
            list_ids = [fl.id for fl in all_lists]

        results = {}
        for i, lid in enumerate(list_ids):
            # Stealth: check activity scheduler before each send
            try:
                scheduler = self.http_client.activity_scheduler
                if not scheduler.can_continue():
                    logger.info("Activity limit reached during farm sends. Stopping.")
                    break
                scheduler.log_activity(5.0)
            except Exception:
                pass

            results[lid] = await self.send_farm_list(lid)

            # Stealth: noise injection between farm list sends
            try:
                await self.http_client.noise_injector.maybe_inject_noise()
            except Exception:
                pass

            # Stealth: delay between farm list sends
            if i < len(list_ids) - 1:
                from ..stealth.human_delay import ActionType
                await self.http_client.human_delay.wait(
                    ActionType.BETWEEN_RAIDS, f"pause between farm list sends ({i+1}/{len(list_ids)})"
                )
        return results

