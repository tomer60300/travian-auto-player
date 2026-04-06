"""Build queue routes -- validate a YAML build plan against current village state."""

from __future__ import annotations

import logging

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from travian_api.exceptions import TravianError
from travian_api.services.build_queue_service import BuildPlan, BuildPlanItem
from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/queue", tags=["queue"])

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ValidateRequest(BaseModel):
    yaml_content: str = Field(..., description="YAML build plan content")


class ValidatedItem(BaseModel):
    building: str
    target: int
    priority: int
    slot: int
    slot_id: int
    current_level: int
    status: str
    is_construction: bool
    construct_gid: int


class ValidateResponse(BaseModel):
    village_id: int
    item_count: int
    items: list[ValidatedItem]
    messages: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_yaml_to_plan(yaml_content: str) -> BuildPlan:
    """Parse a YAML string into a BuildPlan, mirroring BuildPlan.from_file()."""
    # YAML forbids tabs for indentation -- silently convert to 2 spaces
    raw = yaml_content.replace("\t", "  ")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid YAML: {exc}",
        )

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="YAML must be a mapping with 'village' and 'plan' keys.",
        )

    raw_vid = data.get("village", data.get("village_id", 0))
    try:
        village_id = int(raw_vid)
    except (TypeError, ValueError):
        village_id = 0
    plan_entries = data.get("plan", [])

    if not isinstance(plan_entries, list) or not plan_entries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="YAML must contain a non-empty 'plan' list.",
        )

    items: list[BuildPlanItem] = []
    for entry in plan_entries:
        items.append(
            BuildPlanItem(
                building=entry.get("building", ""),
                target=entry.get("target", entry.get("level", 1)),
                priority=entry.get("priority", 5),
                slot=entry.get("slot", 0),
                expect=entry.get("expect", ""),
            )
        )

    return BuildPlan(village_id=village_id, items=items)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/validate", response_model=ValidateResponse)
async def validate_build_plan(
    body: ValidateRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Validate a YAML build plan: parse it, resolve slot references, and
    return each item with its resolved slot_id, current level, and status.

    Nothing is executed -- this is a read-only operation.
    """
    plan = _parse_yaml_to_plan(body.yaml_content)

    # Collect status messages emitted during resolve_slots
    messages: list[str] = []
    service = session.build_queue_service
    prev_callback = service._on_status  # preserve existing callback
    service.on_status(lambda msg: messages.append(msg))

    try:
        await service.resolve_slots(plan)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to resolve slots: {exc.message}",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error resolving build plan slots")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to resolve slots: {exc}",
        ) from exc
    finally:
        # Restore previous callback
        service._on_status = prev_callback

    items = [
        ValidatedItem(
            building=item.building,
            target=item.target,
            priority=item.priority,
            slot=item.slot,
            slot_id=item.slot_id,
            current_level=item.current_level,
            status=item.status,
            is_construction=item.is_construction,
            construct_gid=item.construct_gid,
        )
        for item in plan.items
    ]

    return ValidateResponse(
        village_id=plan.village_id,
        item_count=len(items),
        items=items,
        messages=messages,
    )
