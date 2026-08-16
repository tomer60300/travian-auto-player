"""Farm list REST routes — CRUD, one-shot send, and send-all."""

from __future__ import annotations

import json as _json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from travian_api.exceptions import TravianError
from travian_api.web.auth import get_current_user
from travian_api.web.sessions import TravianSession, get_travian_session, require_village_id

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
        raided = getattr(slot.last_raid, "raided_resources", None)
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
        TargetResultResponse(id=t.id, status=t.status, error=t.error) for t in result.targets
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
            result.setdefault(key, []).append({"list_id": fl.id, "list_name": fl.name})
    return result


@router.post("/lists", status_code=status.HTTP_201_CREATED)
async def create_farm_list(
    body: CreateFarmListRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Create a new farm list. Returns the new list ID."""
    village_id = require_village_id(body.village_id)
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

from travian_api.services.defense_cache import defense_cache


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


async def _fetch_defense_for_coord(
    session,
    x: int,
    y: int,
) -> dict | None:
    """Fetch defense data for a single coordinate (tile-details + report HTML)."""
    try:
        village_data = await session.reports_service.fetch_village_reports(
            x=x,
            y=y,
            fetch_details=False,
        )
    except Exception as exc:
        logger.debug("Defense scan: tile-details failed for (%s,%s): %s", x, y, exc)
        return None

    tile_reports = village_data.get("reports", [])
    battle_report = next(
        (r for r in tile_reports if 1 <= r.get("icon_type", 0) <= 8),
        None,
    )
    if not battle_report:
        return None

    report_id = battle_report.get("report_id", "")
    aid = battle_report.get("aid", "")

    try:
        detail = await session.reports_service.fetch_report_detail(
            f"{report_id}&aid={aid}" if aid else report_id
        )
        if detail and detail.get("type") == "battle":
            battle = detail.get("data")
            if battle:
                defender_troops = dict(battle.defender_troops) if battle.defender_troops else {}
                return {
                    "defender_troops": defender_troops,
                    "defender_total": sum(defender_troops.values()),
                    "defender_combat_strength": getattr(battle, "defender_combat_strength", 0) or 0,
                    "report_id": report_id,
                }
    except Exception as exc:
        logger.debug("Defense scan: report fetch failed for %s: %s", report_id, exc)

    return None


@router.post("/defense-scan")
async def scan_defense_strength(
    body: DefenseInfoRequest,
    session: TravianSession = Depends(get_travian_session),
    user=Depends(get_current_user),
):
    """Stream defense scan results as NDJSON (newline-delimited JSON).

    Each line is one of:
      {"type":"progress","total":60,"cached":45,"to_fetch":15,"fetched":0}
      {"type":"result","slot_id":123,"x":12,"y":120,...}
      {"type":"log","message":"Fetching (12,120) BrightMaster..."}
      {"type":"complete","total":60,"fetched":15,"elapsed":45.2}
      {"type":"error","message":"..."}

    Cached results stream instantly. Fetched results stream one-by-one
    as each target completes. If interrupted, cached data persists on disk.
    """
    import time as _time

    # Defense data comes from this account's own raid reports: the same
    # coordinates mean different data per account, and per world they are
    # different villages outright. The scope keeps users out of each other's
    # cache entries (and in-flight futures).
    cache_scope = f"{session.server_url.rstrip('/')}|{session.player_name}"

    async def _generate():
        scan_start = _time.monotonic()

        def _line(obj: dict) -> str:
            return _json.dumps(obj, ensure_ascii=False) + "\n"

        try:
            fl = await session.farm_service.get_farm_list(body.list_id)
        except Exception as exc:
            yield _line({"type": "error", "message": f"Failed to fetch farm list: {exc}"})
            return

        if not fl.slots:
            yield _line({"type": "complete", "total": 0, "fetched": 0, "elapsed": 0})
            return

        now_ts = _time.time()
        needs_fetch: dict[tuple[int, int], list] = {}
        cached_count = 0

        # ── Phase 1: classify slots (cached vs needs-fetch) ──────────
        for slot in fl.slots:
            if not slot.last_raid or not slot.last_raid.time:
                continue  # will emit after classification
            coord_key = (slot.target.x, slot.target.y)
            if not body.force_refresh:
                cached = defense_cache.get(
                    cache_scope, slot.target.x, slot.target.y, slot.last_raid.time
                )
                if cached is not None:
                    cached_count += 1
                    continue
            if coord_key not in needs_fetch:
                needs_fetch[coord_key] = []
            needs_fetch[coord_key].append(slot)

        total = len(fl.slots)
        to_fetch = len(needs_fetch)

        yield _line(
            {
                "type": "progress",
                "total": total,
                "cached": cached_count,
                "to_fetch": to_fetch,
                "fetched": 0,
            }
        )

        yield _line(
            {
                "type": "log",
                "message": f"Scan started: {total} targets, {cached_count} cached, {to_fetch} to fetch",
            }
        )

        # ── Phase 2: emit cached results instantly ────────────────────
        for slot in fl.slots:
            if not slot.last_raid or not slot.last_raid.time:
                yield _line(
                    SlotDefenseInfo(
                        slot_id=slot.id,
                        x=slot.target.x,
                        y=slot.target.y,
                        name=slot.target.name,
                        never_raided=True,
                    ).model_dump()
                    | {"type": "result"}
                )
                continue

            coord_key = (slot.target.x, slot.target.y)
            age_hours = round((now_ts - slot.last_raid.time) / 3600, 1)

            if coord_key not in needs_fetch:
                # This is a cache hit
                cached = defense_cache.get(
                    cache_scope, slot.target.x, slot.target.y, slot.last_raid.time
                )
                if cached is not None:
                    yield _line(
                        SlotDefenseInfo(
                            slot_id=slot.id,
                            x=slot.target.x,
                            y=slot.target.y,
                            name=slot.target.name,
                            report_age_hours=age_hours,
                            defender_troops=cached.get("defender_troops", {}),
                            defender_total=cached.get("defender_total", 0),
                            defender_combat_strength=cached.get("defender_combat_strength", 0),
                            report_id=cached.get("report_id"),
                        ).model_dump()
                        | {"type": "result"}
                    )

        # ── Phase 3: fetch uncached coordinates one by one ────────────
        fetched_count = 0

        for coord_key, slots in needs_fetch.items():
            x, y = coord_key
            first_slot = slots[0]

            yield _line(
                {
                    "type": "log",
                    "message": f"Fetching ({x},{y}) {first_slot.target.name}... [{fetched_count + 1}/{to_fetch}]",
                }
            )

            # Request coalescing
            inflight = defense_cache.get_inflight(cache_scope, x, y)
            if inflight is not None:
                try:
                    defense_data = await inflight
                except Exception:
                    defense_data = None
            else:
                fut = defense_cache.set_inflight(cache_scope, x, y)
                try:
                    defense_data = await _fetch_defense_for_coord(session, x, y)
                    fut.set_result(defense_data)
                except Exception as exc:
                    fut.set_exception(exc)
                    defense_data = None
                finally:
                    defense_cache.clear_inflight(cache_scope, x, y)

            fetched_count += 1

            for slot in slots:
                age_hours = (
                    round((now_ts - slot.last_raid.time) / 3600, 1)
                    if slot.last_raid and slot.last_raid.time
                    else None
                )
                if defense_data:
                    yield _line(
                        SlotDefenseInfo(
                            slot_id=slot.id,
                            x=slot.target.x,
                            y=slot.target.y,
                            name=slot.target.name,
                            report_age_hours=age_hours,
                            **defense_data,
                        ).model_dump()
                        | {"type": "result"}
                    )
                    defense_cache.put(
                        cache_scope,
                        slot.target.x,
                        slot.target.y,
                        slot.last_raid.time,
                        defense_data,
                    )
                else:
                    yield _line(
                        SlotDefenseInfo(
                            slot_id=slot.id,
                            x=slot.target.x,
                            y=slot.target.y,
                            name=slot.target.name,
                            report_age_hours=age_hours,
                        ).model_dump()
                        | {"type": "result"}
                    )

            yield _line(
                {
                    "type": "progress",
                    "total": total,
                    "cached": cached_count,
                    "to_fetch": to_fetch,
                    "fetched": fetched_count,
                }
            )

        elapsed = round(_time.monotonic() - scan_start, 1)
        yield _line(
            {
                "type": "log",
                "message": f"Scan complete: {total} targets, {fetched_count} fetched in {elapsed}s",
            }
        )
        yield _line(
            {
                "type": "complete",
                "total": total,
                "fetched": fetched_count,
                "elapsed": elapsed,
            }
        )

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.get("/defense-cache/stats")
async def defense_cache_stats(user=Depends(get_current_user)):
    """Return defense scan cache statistics."""
    return defense_cache.get_stats()


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
        return SendResultResponse(list_id=list_id, success_count=0, fail_count=0, targets=[])

    if dry_run:
        # Return a preview — targets with status="dry_run"
        targets = [
            TargetResultResponse(id=s.id, status="dry_run", error="") for s in fl.active_slots
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
                TargetResultResponse(id=s.id, status="dry_run", error="") for s in fl.active_slots
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

    return [_send_result_response(lid, result) for lid, result in result_map.items()]
