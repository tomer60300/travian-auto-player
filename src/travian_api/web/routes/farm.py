"""Farm list REST routes — CRUD, one-shot send, and send-all."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from travian_api.exceptions import TravianError
from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/farm", tags=["farm"])

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateFarmListRequest(BaseModel):
    name: str
    village_id: int | None = None


class AddTargetRequest(BaseModel):
    x: int
    y: int
    troops: dict[str, int] | None = None
    force: bool = False


class SendFarmListRequest(BaseModel):
    dry_run: bool = False


class SendAllRequest(BaseModel):
    list_ids: list[int] | None = None
    dry_run: bool = False


class TargetResultResponse(BaseModel):
    id: int
    status: str
    error: str


class SendResultResponse(BaseModel):
    list_id: int
    success_count: int
    fail_count: int
    targets: list[TargetResultResponse]


class LastRaidResponse(BaseModel):
    icon: str  # "no_loss", "some_loss", "all_dead", "unknown"
    resources: int | None = None
    capacity: int | None = None  # booty_max — total carry capacity of the raid
    time: int | None = None  # unix timestamp


class SlotResponse(BaseModel):
    id: int
    x: int
    y: int
    name: str
    population: int
    distance: float
    is_active: bool
    is_running: bool
    running_attacks: int
    troops: dict[str, int]  # {"t1": 0, "t2": 50, ...} — full breakdown
    troop_total: int
    last_raid: LastRaidResponse | None = None
    total_booty: int
    total_raids: int


class FarmListSummaryResponse(BaseModel):
    id: int
    name: str
    slots_amount: int
    active_slots: int
    running_raids: int
    owner_village_id: int
    owner_village_name: str | None = None
    total_booty: int = 0
    total_raids: int = 0


class FarmListDetailResponse(FarmListSummaryResponse):
    slots: list[SlotResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _farm_list_to_summary(fl) -> FarmListSummaryResponse:
    total_booty = sum(s.total_booty.booty for s in fl.slots)
    total_raids = sum(s.total_booty.raids for s in fl.slots)
    return FarmListSummaryResponse(
        id=fl.id,
        name=fl.name,
        slots_amount=fl.slots_amount,
        active_slots=len(fl.active_slots),
        running_raids=fl.running_raids_amount,
        owner_village_id=fl.owner_village.id,
        total_booty=total_booty,
        total_raids=total_raids,
    )


def _slot_to_response(slot) -> SlotResponse:
    last_raid = None
    if slot.last_raid:
        raided = getattr(slot.last_raid, 'raided_resources', None)
        last_raid = LastRaidResponse(
            icon=slot.last_raid.icon_label,
            resources=raided.total if raided else None,
            capacity=slot.last_raid.booty_max or None,
            time=slot.last_raid.time,
        )

    # Full troop breakdown (only include non-zero for cleaner output)
    troops = slot.troop.to_dict()

    return SlotResponse(
        id=slot.id,
        x=slot.target.x,
        y=slot.target.y,
        name=slot.target.name,
        population=slot.target.population,
        distance=slot.distance,
        is_active=slot.is_active,
        is_running=slot.is_running,
        running_attacks=slot.running_attacks,
        troops=troops,
        troop_total=slot.troop.total,
        last_raid=last_raid,
        total_booty=slot.total_booty.booty,
        total_raids=slot.total_booty.raids,
    )


def _send_result_response(list_id: int, result) -> SendResultResponse:
    targets = [
        TargetResultResponse(id=t.id, status=t.status, error=t.error)
        for t in result.targets
    ]
    return SendResultResponse(
        list_id=list_id,
        success_count=result.success_count,
        fail_count=result.fail_count,
        targets=targets,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/lists", response_model=list[FarmListSummaryResponse])
async def get_all_farm_lists(
    session: TravianSession = Depends(get_travian_session),
):
    """Return all farm lists with summary stats."""
    try:
        lists = await session.farm_service.get_all_farm_lists()
        return [_farm_list_to_summary(fl) for fl in lists]
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.get("/lists/{list_id}", response_model=FarmListDetailResponse)
async def get_farm_list(
    list_id: int,
    session: TravianSession = Depends(get_travian_session),
):
    """Return a detailed farm list with all slots."""
    try:
        fl = await session.farm_service.get_farm_list(list_id)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc

    return FarmListDetailResponse(
        id=fl.id,
        name=fl.name,
        slots_amount=fl.slots_amount,
        active_slots=len(fl.active_slots),
        running_raids=fl.running_raids_amount,
        owner_village_id=fl.owner_village.id,
        slots=[_slot_to_response(s) for s in fl.slots],
    )


@router.post("/lists", status_code=status.HTTP_201_CREATED)
async def create_farm_list(
    body: CreateFarmListRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Create a new farm list. Returns the new list ID."""
    village_id = body.village_id or session.active_village_id
    if village_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No village_id provided and no active village set.",
        )
    try:
        list_id = await session.farm_service.create_farm_list(village_id, body.name)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc
    return {"id": list_id, "name": body.name}


@router.delete("/lists/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_farm_list(
    list_id: int,
    session: TravianSession = Depends(get_travian_session),
):
    """Delete a farm list."""
    try:
        await session.farm_service.delete_farm_list(list_id)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post("/lists/{list_id}/targets", status_code=status.HTTP_201_CREATED)
async def add_target(
    list_id: int,
    body: AddTargetRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Add a target slot to a farm list."""
    try:
        await session.farm_service.add_slot(
            list_id,
            x=body.x,
            y=body.y,
            units=body.troops,
            force=body.force,
        )
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc
    return {"list_id": list_id, "x": body.x, "y": body.y}


@router.delete("/lists/{list_id}/targets/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    list_id: int,
    slot_id: int,
    session: TravianSession = Depends(get_travian_session),
):
    """Remove a target slot from a farm list."""
    try:
        await session.farm_service.delete_slots([slot_id])
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


# ---------------------------------------------------------------------------
# Defender strength lookup (background-friendly)
# ---------------------------------------------------------------------------


class DefenseInfoRequest(BaseModel):
    list_id: int
    max_pages: int = 5
    max_age_hours: int = 48


class SlotDefenseInfo(BaseModel):
    slot_id: int
    x: int
    y: int
    name: str
    defender_troops: dict[str, int] = {}
    defender_total: int = 0
    report_age_hours: float | None = None
    report_id: str | None = None


@router.post("/defense-scan", response_model=list[SlotDefenseInfo])
async def scan_defense_strength(
    body: DefenseInfoRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Scan recent battle/scout reports to extract defender troop info for farm list targets.

    This can be slow (fetches reports) — call it in the background from the frontend.
    """
    import time as _time
    from datetime import datetime, timezone

    # 1. Get the farm list slots
    try:
        fl = await session.farm_service.get_farm_list(body.list_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch farm list: {exc}",
        ) from exc

    # Build a coord→slot lookup
    coord_to_slots: dict[tuple[int, int], list] = {}
    for slot in fl.slots:
        key = (slot.target.x, slot.target.y)
        coord_to_slots.setdefault(key, []).append(slot)

    if not coord_to_slots:
        return []

    # 2. Fetch recent reports
    try:
        reports = await session.reports_service.fetch_reports(
            max_age_hours=body.max_age_hours,
            max_pages=body.max_pages,
        )
    except Exception as exc:
        logger.warning("Failed to fetch reports for defense scan: %s", exc)
        reports = []

    # 3. For each report, check if it's a battle targeting one of our farm slots
    now_ts = _time.time()
    results: dict[int, SlotDefenseInfo] = {}

    for report in reports:
        # Only process battle reports
        report_type = getattr(report, 'report_type', '') or ''
        if 'battle' not in report_type.lower() and 'raid' not in report_type.lower():
            continue

        # Try to get detailed report data
        try:
            detail = await session.reports_service.fetch_report_detail(report.report_id)
        except Exception:
            continue

        if not detail:
            continue

        # Extract defender coords — look for target village coords
        battle = getattr(detail, 'battle_data', None) or getattr(detail, 'data', None)
        if not battle:
            continue

        defender_info = {}
        if hasattr(battle, 'defender') and isinstance(battle.defender, dict):
            defender_info = battle.defender
        elif isinstance(battle, dict):
            defender_info = battle.get('defender', {})

        # Get defender coords
        dx = defender_info.get('x') or defender_info.get('coordinateX')
        dy = defender_info.get('y') or defender_info.get('coordinateY')

        if dx is None or dy is None:
            continue

        coord_key = (int(dx), int(dy))
        if coord_key not in coord_to_slots:
            continue

        # Extract defender troops
        defender_troops = {}
        if hasattr(battle, 'defender_troops'):
            defender_troops = dict(battle.defender_troops) if battle.defender_troops else {}
        elif isinstance(battle, dict):
            defender_troops = dict(battle.get('defender_troops', {}))

        defender_total = sum(defender_troops.values())

        # Report age
        report_time = getattr(report, 'time', None) or getattr(report, 'timestamp', None)
        age_hours = None
        if report_time:
            if isinstance(report_time, (int, float)):
                age_hours = round((now_ts - report_time) / 3600, 1)
            elif isinstance(report_time, datetime):
                age_hours = round((datetime.now(timezone.utc) - report_time).total_seconds() / 3600, 1)

        report_id = getattr(report, 'report_id', None) or getattr(report, 'id', None)

        for slot in coord_to_slots[coord_key]:
            # Only keep the most recent (first encountered) report per slot
            if slot.id not in results:
                results[slot.id] = SlotDefenseInfo(
                    slot_id=slot.id,
                    x=slot.target.x,
                    y=slot.target.y,
                    name=slot.target.name,
                    defender_troops=defender_troops,
                    defender_total=defender_total,
                    report_age_hours=age_hours,
                    report_id=str(report_id) if report_id else None,
                )

    return list(results.values())


@router.post("/lists/{list_id}/send", response_model=SendResultResponse)
async def send_farm_list(
    list_id: int,
    body: SendFarmListRequest | None = None,
    session: TravianSession = Depends(get_travian_session),
):
    """One-shot send of a single farm list."""
    dry_run = body.dry_run if body else False

    try:
        fl = await session.farm_service.get_farm_list(list_id)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc

    if not fl.active_slots:
        return SendResultResponse(
            list_id=list_id, success_count=0, fail_count=0, targets=[]
        )

    if dry_run:
        # Return a preview — targets with status="dry_run"
        targets = [
            TargetResultResponse(id=s.id, status="dry_run", error="")
            for s in fl.active_slots
        ]
        return SendResultResponse(
            list_id=list_id,
            success_count=len(targets),
            fail_count=0,
            targets=targets,
        )

    try:
        result = await session.farm_service.send_farm_list(list_id)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc

    return _send_result_response(list_id, result)


@router.post("/send-all", response_model=list[SendResultResponse])
async def send_all_farm_lists(
    body: SendAllRequest | None = None,
    session: TravianSession = Depends(get_travian_session),
):
    """Send all (or specified) farm lists in one shot."""
    list_ids = body.list_ids if body else None
    dry_run = body.dry_run if body else False

    try:
        all_lists = await session.farm_service.get_all_farm_lists()
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc

    if list_ids:
        all_lists = [fl for fl in all_lists if fl.id in list_ids]

    if not all_lists:
        return []

    if dry_run:
        results = []
        for fl in all_lists:
            targets = [
                TargetResultResponse(id=s.id, status="dry_run", error="")
                for s in fl.active_slots
            ]
            results.append(
                SendResultResponse(
                    list_id=fl.id,
                    success_count=len(targets),
                    fail_count=0,
                    targets=targets,
                )
            )
        return results

    send_ids = [fl.id for fl in all_lists]
    try:
        result_map = await session.farm_service.send_all_farm_lists(send_ids)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc

    return [
        _send_result_response(lid, result)
        for lid, result in result_map.items()
    ]
