"""Farm list REST routes — CRUD, one-shot send, and send-all."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from travian_api.exceptions import TravianError
from travian_api.web.auth import get_current_user
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


class CoordMapEntry(BaseModel):
    list_id: int
    list_name: str


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


@router.get("/coord-map")
async def get_coord_map(
    session: TravianSession = Depends(get_travian_session),
):
    """Return a lightweight coord -> farm list(s) mapping for all lists."""
    try:
        lists = await session.farm_service.get_all_farm_lists()
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc

    result: dict[str, list[dict]] = {}
    for fl in lists:
        for slot in fl.slots:
            key = f"{slot.target.x},{slot.target.y}"
            result.setdefault(key, []).append(
                {"list_id": fl.id, "list_name": fl.name}
            )
    return result


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
    force_refresh: bool = False


class SlotDefenseInfo(BaseModel):
    slot_id: int
    x: int
    y: int
    name: str
    defender_troops: dict[str, int] = {}
    defender_total: int = 0
    defender_combat_strength: int = 0
    report_age_hours: float | None = None
    report_id: str | None = None
    never_raided: bool = False


# ── Module-level defense cache (per user_id → per coordinate) ─────────
_DEFENSE_CACHE_TTL = 1800  # 30 minutes

# {user_id: {(x,y): (monotonic_ts, last_raid_time, defense_dict)}}
_defense_cache: dict[int, dict[tuple[int, int], tuple[float, int, dict]]] = {}


def _cache_get(user_id: int, x: int, y: int, last_raid_time: int) -> dict | None:
    """Return cached defense data if fresh and last_raid_time matches."""
    import time
    user_store = _defense_cache.get(user_id)
    if not user_store:
        return None
    entry = user_store.get((x, y))
    if not entry:
        return None
    ts, cached_lrt, data = entry
    if time.monotonic() - ts > _DEFENSE_CACHE_TTL:
        del user_store[(x, y)]
        return None
    if cached_lrt != last_raid_time:
        del user_store[(x, y)]
        return None
    return data


def _cache_put(user_id: int, x: int, y: int, last_raid_time: int, data: dict) -> None:
    import time
    if user_id not in _defense_cache:
        _defense_cache[user_id] = {}
    _defense_cache[user_id][(x, y)] = (time.monotonic(), last_raid_time, data)


@router.post("/defense-scan", response_model=list[SlotDefenseInfo])
async def scan_defense_strength(
    body: DefenseInfoRequest,
    session: TravianSession = Depends(get_travian_session),
    user=Depends(get_current_user),
):
    """Fetch defender combat strength with 30-min cache.

    Flow:
      1. Get farm list (1 GraphQL call)
      2. Check cache for each coordinate (0 calls)
      3. For uncached: tile-details + report HTML (2 calls each)
      4. Cache all fetched results
    """
    import time as _time

    # ── 1. Get the farm list slots ────────────────────────────────────
    try:
        fl = await session.farm_service.get_farm_list(body.list_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch farm list: {exc}",
        ) from exc

    if not fl.slots:
        return []

    now_ts = _time.time()
    user_id = user.id

    results: list[SlotDefenseInfo] = []
    needs_fetch: dict[tuple[int, int], list] = {}

    # ── 2. Cache check for each slot ──────────────────────────────────
    for slot in fl.slots:
        if not slot.last_raid or not slot.last_raid.time:
            results.append(SlotDefenseInfo(
                slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                name=slot.target.name, never_raided=True,
            ))
            continue

        coord_key = (slot.target.x, slot.target.y)
        age_hours = round((now_ts - slot.last_raid.time) / 3600, 1)

        if not body.force_refresh:
            cached = _cache_get(user_id, slot.target.x, slot.target.y, slot.last_raid.time)
            if cached is not None:
                results.append(SlotDefenseInfo(
                    slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                    name=slot.target.name, report_age_hours=age_hours, **cached,
                ))
                continue

        if coord_key not in needs_fetch:
            needs_fetch[coord_key] = []
        needs_fetch[coord_key].append(slot)

    logger.info(
        "Defense scan: %d slots, %d cached, %d need fetch",
        len(fl.slots), len(results), len(needs_fetch),
    )

    # ── 4. Tile-details + report HTML for ambiguous coords ────────────
    for coord_key, slots in needs_fetch.items():
        try:
            village_data = await session.reports_service.fetch_village_reports(
                x=coord_key[0], y=coord_key[1], fetch_details=False,
            )
        except Exception as exc:
            logger.debug("Defense scan: tile-details failed for (%s,%s): %s", coord_key[0], coord_key[1], exc)
            for slot in slots:
                age_hours = round((now_ts - slot.last_raid.time) / 3600, 1) if slot.last_raid and slot.last_raid.time else None
                results.append(SlotDefenseInfo(
                    slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                    name=slot.target.name, report_age_hours=age_hours,
                ))
            continue

        tile_reports = village_data.get('reports', [])
        battle_report = next(
            (r for r in tile_reports if 1 <= r.get('icon_type', 0) <= 8),
            None,
        )

        if not battle_report:
            for slot in slots:
                age_hours = round((now_ts - slot.last_raid.time) / 3600, 1) if slot.last_raid and slot.last_raid.time else None
                results.append(SlotDefenseInfo(
                    slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                    name=slot.target.name, report_age_hours=age_hours,
                ))
            continue

        report_id = battle_report.get('report_id', '')
        aid = battle_report.get('aid', '')

        defense_data = None
        try:
            detail = await session.reports_service.fetch_report_detail(
                f"{report_id}&aid={aid}" if aid else report_id
            )
            if detail and detail.get('type') == 'battle':
                battle = detail.get('data')
                if battle:
                    defender_troops = dict(battle.defender_troops) if battle.defender_troops else {}
                    defense_data = {
                        "defender_troops": defender_troops,
                        "defender_total": sum(defender_troops.values()),
                        "defender_combat_strength": getattr(battle, 'defender_combat_strength', 0) or 0,
                        "report_id": report_id,
                    }
        except Exception as exc:
            logger.debug("Defense scan: report fetch failed for %s: %s", report_id, exc)

        for slot in slots:
            age_hours = round((now_ts - slot.last_raid.time) / 3600, 1) if slot.last_raid and slot.last_raid.time else None
            if defense_data:
                results.append(SlotDefenseInfo(
                    slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                    name=slot.target.name, report_age_hours=age_hours,
                    **defense_data,
                ))
                # Cache the fetched result
                _cache_put(user_id, slot.target.x, slot.target.y, slot.last_raid.time, defense_data)
            else:
                results.append(SlotDefenseInfo(
                    slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                    name=slot.target.name, report_age_hours=age_hours,
                ))

    logger.info("Defense scan complete: %d results (%d fetched)", len(results), len(needs_fetch))
    return results


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
