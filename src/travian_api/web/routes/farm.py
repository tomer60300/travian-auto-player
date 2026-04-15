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
    max_pages: int = 5  # unused now, kept for backwards compat
    max_age_hours: int = 48  # unused now, kept for backwards compat


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


@router.post("/defense-scan", response_model=list[SlotDefenseInfo])
async def scan_defense_strength(
    body: DefenseInfoRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Fetch defender combat strength from each slot's last raid report.

    Optimized two-pass approach:
      Pass 1 (fast): Fetch report list pages → batch GraphQL for defender
              coordinates → match to farm slots → fetch HTML detail only
              for matched reports.
      Pass 2 (fallback): For any raided coordinates not resolved in pass 1,
              use tile-details API per coordinate.
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

    # Build coord → slots lookup (only raided slots)
    coord_to_slots: dict[tuple[int, int], list] = {}
    never_raided_results: list[SlotDefenseInfo] = []

    for slot in fl.slots:
        if not slot.last_raid or not slot.last_raid.time:
            never_raided_results.append(SlotDefenseInfo(
                slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                name=slot.target.name, never_raided=True,
            ))
        else:
            key = (slot.target.x, slot.target.y)
            coord_to_slots.setdefault(key, []).append(slot)

    if not coord_to_slots:
        return never_raided_results

    coord_defense: dict[tuple[int, int], SlotDefenseInfo | None] = {}

    # ── Pass 1: report list + batch metadata (fast) ───────────────────
    try:
        reports = await session.reports_service.fetch_reports(
            max_age_hours=body.max_age_hours,
            max_pages=body.max_pages,
        )
    except Exception as exc:
        logger.warning("Defense scan pass 1: failed to fetch report list: %s", exc)
        reports = []

    battle_report_ids = [
        r.report_id for r in reports
        if getattr(r, 'icon_type', 0) in range(1, 9)
        or 'battle' in (getattr(r, 'report_type', '') or '').lower()
    ]

    if battle_report_ids:
        try:
            metadata = await session.reports_service.fetch_report_batch_metadata(battle_report_ids)
        except Exception:
            metadata = {}

        # Match reports to farm slot coordinates (newest report per coord)
        coord_to_report: dict[tuple[int, int], str] = {}
        for rid, meta in metadata.items():
            defender = meta.get('defender') or {}
            village = defender.get('village') or {}
            vx, vy = village.get('x'), village.get('y')
            if vx is None or vy is None:
                continue
            coord_key = (int(vx), int(vy))
            if coord_key in coord_to_slots and coord_key not in coord_to_report:
                coord_to_report[coord_key] = rid

        logger.info(
            "Defense scan pass 1: matched %d/%d coords via batch metadata",
            len(coord_to_report), len(coord_to_slots),
        )

        # Fetch HTML detail for matched reports
        for coord_key, report_id in coord_to_report.items():
            try:
                detail = await session.reports_service.fetch_report_detail(report_id)
            except Exception:
                continue
            if not detail or detail.get('type') != 'battle':
                continue
            battle = detail.get('data')
            if not battle:
                continue

            defender_troops = dict(battle.defender_troops) if battle.defender_troops else {}
            defender_total = sum(defender_troops.values())
            defender_combat_strength = getattr(battle, 'defender_combat_strength', 0) or 0
            first_slot = coord_to_slots[coord_key][0]
            age_hours = round((now_ts - first_slot.last_raid.time) / 3600, 1) if first_slot.last_raid and first_slot.last_raid.time else None

            coord_defense[coord_key] = SlotDefenseInfo(
                slot_id=0, x=coord_key[0], y=coord_key[1], name="",
                defender_troops=defender_troops, defender_total=defender_total,
                defender_combat_strength=defender_combat_strength,
                report_age_hours=age_hours, report_id=report_id,
            )

    # ── Pass 2: tile-details fallback for unresolved coords ───────────
    unresolved = [c for c in coord_to_slots if c not in coord_defense]
    if unresolved:
        logger.info("Defense scan pass 2: %d unresolved coords, using tile-details", len(unresolved))

    for coord_key in unresolved:
        try:
            village_data = await session.reports_service.fetch_village_reports(
                x=coord_key[0], y=coord_key[1], fetch_details=False,
            )
        except Exception as exc:
            logger.debug("Defense scan: tile-details failed for (%s,%s): %s", coord_key[0], coord_key[1], exc)
            continue

        tile_reports = village_data.get('reports', [])
        battle_report = next(
            (r for r in tile_reports if 1 <= r.get('icon_type', 0) <= 8),
            None,
        )
        if not battle_report:
            continue

        report_id = battle_report.get('report_id', '')
        aid = battle_report.get('aid', '')
        try:
            detail = await session.reports_service.fetch_report_detail(
                f"{report_id}&aid={aid}" if aid else report_id
            )
        except Exception:
            continue
        if not detail or detail.get('type') != 'battle':
            continue
        battle = detail.get('data')
        if not battle:
            continue

        defender_troops = dict(battle.defender_troops) if battle.defender_troops else {}
        defender_total = sum(defender_troops.values())
        defender_combat_strength = getattr(battle, 'defender_combat_strength', 0) or 0
        first_slot = coord_to_slots[coord_key][0]
        age_hours = round((now_ts - first_slot.last_raid.time) / 3600, 1) if first_slot.last_raid and first_slot.last_raid.time else None

        coord_defense[coord_key] = SlotDefenseInfo(
            slot_id=0, x=coord_key[0], y=coord_key[1], name="",
            defender_troops=defender_troops, defender_total=defender_total,
            defender_combat_strength=defender_combat_strength,
            report_age_hours=age_hours, report_id=report_id,
        )

    # ── Build final results ───────────────────────────────────────────
    results = list(never_raided_results)
    for coord_key, slots in coord_to_slots.items():
        defense = coord_defense.get(coord_key)
        for slot in slots:
            age_hours = round((now_ts - slot.last_raid.time) / 3600, 1) if slot.last_raid and slot.last_raid.time else None
            if defense is not None:
                results.append(SlotDefenseInfo(
                    slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                    name=slot.target.name,
                    defender_troops=defense.defender_troops,
                    defender_total=defense.defender_total,
                    defender_combat_strength=defense.defender_combat_strength,
                    report_age_hours=age_hours, report_id=defense.report_id,
                ))
            else:
                results.append(SlotDefenseInfo(
                    slot_id=slot.id, x=slot.target.x, y=slot.target.y,
                    name=slot.target.name,
                ))

    logger.info(
        "Defense scan complete: %d slots, %d resolved via pass 1, %d via pass 2, %d unresolved",
        len(fl.slots), len(coord_defense) - len(unresolved) + len([c for c in unresolved if c in coord_defense]),
        len([c for c in unresolved if c in coord_defense]),
        len([c for c in coord_to_slots if c not in coord_defense]),
    )
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
