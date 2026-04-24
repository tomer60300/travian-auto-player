"""Farm list service for Travian API — CRUD, querying, and sending raids."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ..clients.http_client import HttpClient
from ..concurrency import KeyedLock
from ..models.farm_list import (
    FarmList,
    FarmListSendResult,
    FarmListSendTargetResult,
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
        # Round-robin cursors: {list_id: cursor_index}
        # Persisted across cycles so each send starts where the last one left off.
        self._cursors: Dict[int, int] = {}
        # Serializes sends for the same list_id (two loops on the same list
        # won't double-dispatch the same slots). Disjoint lists stay parallel.
        self._list_lock = KeyedLock()

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
        """Delete slots by IDs via Travian REST API (DELETE with JSON body)."""
        await self.http_client.delete_json(
            "/api/v1/farm-list/slot",
            data={"slots": slot_ids, "abandoned": False},
        )
        logger.info(f"Deleted slots: {slot_ids}")

    # ── Send ─────────────────────────────────────────────────────────

    BATCH_SIZE = 5  # targets per API call for round-robin batching

    async def _send_batch(
        self, list_id: int, slot_ids: List[int]
    ) -> List[FarmListSendTargetResult]:
        """Send a single batch of slot IDs. Returns per-target results."""
        resp = await self.http_client.post_json(
            "/api/v1/farm-list/send",
            {
                "action": "farmList",
                "lists": [{"id": list_id, "targets": slot_ids}],
            },
            safe_to_retry=False,
        )

        error = resp.get("error", "")
        if error:
            return [
                FarmListSendTargetResult(id=sid, status="error", error=error) for sid in slot_ids
            ]

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
        return targets

    async def send_farm_list(
        self,
        list_id: int,
        target_slot_ids: Optional[List[int]] = None,
    ) -> FarmListSendResult:
        """
        Send raids for a farm list using round-robin batched ordering.

        Travian's bulk send API ignores the order of the targets array and
        always processes in its own internal order (top → bottom).  To
        distribute raids fairly when troops are limited, we send targets
        in **small batches** starting from a persistent cursor position.

        Each batch is a separate API call containing only a few targets,
        so Travian is forced to process exactly those.  When a batch
        returns all "not enough troops", we stop — troops are exhausted.
        The cursor advances to where we stopped, so the next cycle
        picks up right there.

        Args:
            list_id: Farm list ID
            target_slot_ids: Specific slot IDs to send (skips round-robin).
                If None, fetches all active slots and applies batched rotation.
        """
        async with self._list_lock(list_id):
            return await self._send_farm_list_unlocked(list_id, target_slot_ids)

    async def _send_farm_list_unlocked(
        self,
        list_id: int,
        target_slot_ids: Optional[List[int]],
    ) -> FarmListSendResult:
        use_round_robin = target_slot_ids is None

        if target_slot_ids is None:
            farm_list = await self.get_farm_list(list_id)
            target_slot_ids = [s.id for s in farm_list.active_slots]

        if not target_slot_ids:
            return FarmListSendResult(targets=[])

        # ── Non-round-robin: single bulk call (explicit slot IDs) ───
        if not use_round_robin or len(target_slot_ids) <= 1:
            try:
                results = await self._send_batch(list_id, target_slot_ids)
            except Exception as e:
                if "goldclub" in str(e).lower():
                    return FarmListSendResult(
                        targets=[
                            FarmListSendTargetResult(
                                id=s, status="error", error="plus.error_goldclub"
                            )
                            for s in target_slot_ids
                        ]
                    )
                raise
            return FarmListSendResult(targets=results)

        # ── Round-robin: rotate + send in small batches ─────────────
        total = len(target_slot_ids)
        cursor = self._cursors.get(list_id, 0) % total
        rotated = target_slot_ids[cursor:] + target_slot_ids[:cursor]

        logger.info(
            "Farm list %d: round-robin cursor=%d/%d, sending in batches of %d",
            list_id,
            cursor,
            total,
            self.BATCH_SIZE,
        )

        all_results: List[FarmListSendTargetResult] = []
        troops_exhausted = False

        for batch_start in range(0, total, self.BATCH_SIZE):
            batch = rotated[batch_start : batch_start + self.BATCH_SIZE]

            try:
                batch_results = await self._send_batch(list_id, batch)
            except Exception as e:
                if "goldclub" in str(e).lower():
                    all_results.extend(
                        [
                            FarmListSendTargetResult(
                                id=s, status="error", error="plus.error_goldclub"
                            )
                            for s in batch
                        ]
                    )
                    troops_exhausted = True
                    break
                raise

            all_results.extend(batch_results)

            # Check if this entire batch failed with troop errors → stop
            batch_sent = sum(1 for t in batch_results if not t.error)
            batch_troop_errors = sum(
                1 for t in batch_results if t.error and "troops" in t.error.lower()
            )
            if batch_sent == 0 and batch_troop_errors == len(batch_results):
                troops_exhausted = True
                logger.info(
                    "Farm list %d: troops exhausted at batch offset %d",
                    list_id,
                    batch_start,
                )
                break

        # ── Advance cursor ──────────────────────────────────────────
        sent_ok = sum(1 for t in all_results if not t.error)
        new_cursor = (cursor + sent_ok) % total
        self._cursors[list_id] = new_cursor
        logger.info(
            "Farm list %d: %d/%d sent, cursor %d → %d%s",
            list_id,
            sent_ok,
            len(all_results),
            cursor,
            new_cursor,
            " (troops exhausted)" if troops_exhausted else "",
        )

        return FarmListSendResult(targets=all_results)

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
                    ActionType.BETWEEN_RAIDS,
                    f"pause between farm list sends ({i + 1}/{len(list_ids)})",
                )
        return results
