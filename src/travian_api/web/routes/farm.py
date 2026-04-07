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
    troop_total: int
    last_raid_icon: str | None = None
    last_raid_resources: int | None = None
    total_booty: int
    total_raids: int


class FarmListSummaryResponse(BaseModel):
    id: int
    name: str
    slots_amount: int
    active_slots: int
    running_raids: int
    owner_village_id: int


class FarmListDetailResponse(FarmListSummaryResponse):
    slots: list[SlotResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _farm_list_to_summary(fl) -> FarmListSummaryResponse:
    return FarmListSummaryResponse(
        id=fl.id,
        name=fl.name,
        slots_amount=fl.slots_amount,
        active_slots=len(fl.active_slots),
        running_raids=fl.running_raids_amount,
        owner_village_id=fl.owner_village.id,
    )


def _slot_to_response(slot) -> SlotResponse:
    last_icon = None
    last_res = None
    if slot.last_raid:
        last_icon = slot.last_raid.icon_label
        raided = getattr(slot.last_raid, 'raided_resources', None)
        last_res = raided.total if raided else None

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
        troop_total=slot.troop.total,
        last_raid_icon=last_icon,
        last_raid_resources=last_res,
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
