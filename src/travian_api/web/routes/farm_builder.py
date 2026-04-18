"""Farm Builder REST routes — presets, preview, history."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from travian_api.web.auth import get_current_user
from travian_api.web.models.db import User, get_db
from travian_api.web.models.farm_builder import (
    FarmBuilderPreset,
    FarmBuilderRunHistory,
    FarmBuilderScanCache,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/farm-builder", tags=["farm-builder"])


# ─── Request / response models ────────────────────────────────────────────


class PresetIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    config: dict[str, Any]


class PresetOut(BaseModel):
    id: int
    name: str
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class RunHistoryOut(BaseModel):
    id: int
    session_id: str
    status: str
    total_targets: int
    added: int
    skipped: int
    failed: int
    error_message: str | None = None
    started_at: datetime
    ended_at: datetime | None = None


class ScanCacheOut(BaseModel):
    has_cache: bool
    updated_at: datetime | None = None
    scan: dict[str, Any] | None = None


class ScanCacheIn(BaseModel):
    scan: dict[str, Any]


# ─── Presets ──────────────────────────────────────────────────────────────


@router.get("/presets", response_model=list[PresetOut])
async def list_presets(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FarmBuilderPreset).where(FarmBuilderPreset.user_id == user.id)
    )
    presets = result.scalars().all()
    return [
        PresetOut(
            id=p.id, name=p.name, config=json.loads(p.config_json),
            created_at=p.created_at, updated_at=p.updated_at,
        )
        for p in presets
    ]


@router.post("/presets", response_model=PresetOut, status_code=status.HTTP_201_CREATED)
async def save_preset(
    body: PresetIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Upsert by (user_id, name)
    existing = await db.execute(
        select(FarmBuilderPreset).where(
            FarmBuilderPreset.user_id == user.id,
            FarmBuilderPreset.name == body.name,
        )
    )
    preset = existing.scalar_one_or_none()
    if preset is None:
        preset = FarmBuilderPreset(
            user_id=user.id,
            name=body.name,
            config_json=json.dumps(body.config),
        )
        db.add(preset)
    else:
        preset.config_json = json.dumps(body.config)
    await db.commit()
    await db.refresh(preset)
    return PresetOut(
        id=preset.id, name=preset.name, config=json.loads(preset.config_json),
        created_at=preset.created_at, updated_at=preset.updated_at,
    )


@router.delete("/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FarmBuilderPreset).where(
            FarmBuilderPreset.id == preset_id,
            FarmBuilderPreset.user_id == user.id,
        )
    )
    preset = result.scalar_one_or_none()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")
    await db.delete(preset)
    await db.commit()


# ─── Run history ──────────────────────────────────────────────────────────


@router.get("/history", response_model=list[RunHistoryOut])
async def list_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FarmBuilderRunHistory)
        .where(FarmBuilderRunHistory.user_id == user.id)
        .order_by(FarmBuilderRunHistory.started_at.desc())
        .limit(50)
    )
    rows = result.scalars().all()
    return [
        RunHistoryOut(
            id=r.id, session_id=r.session_id, status=r.status,
            total_targets=r.total_targets, added=r.added,
            skipped=r.skipped, failed=r.failed,
            error_message=r.error_message,
            started_at=r.started_at, ended_at=r.ended_at,
        )
        for r in rows
    ]


# ─── Scan cache (view-layer convenience, NOT job resume) ──────────────────


SCAN_CACHE_MAX_AGE_SECONDS = 3600


@router.get("/scan-cache", response_model=ScanCacheOut)
async def get_scan_cache(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FarmBuilderScanCache).where(FarmBuilderScanCache.user_id == user.id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return ScanCacheOut(has_cache=False)
    # Check age
    age = datetime.now(timezone.utc) - row.updated_at
    if age > timedelta(seconds=SCAN_CACHE_MAX_AGE_SECONDS):
        return ScanCacheOut(has_cache=False)
    return ScanCacheOut(
        has_cache=True,
        updated_at=row.updated_at,
        scan=json.loads(row.scan_json),
    )


@router.post("/scan-cache", status_code=status.HTTP_204_NO_CONTENT)
async def save_scan_cache(
    body: ScanCacheIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FarmBuilderScanCache).where(FarmBuilderScanCache.user_id == user.id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = FarmBuilderScanCache(user_id=user.id, scan_json=json.dumps(body.scan))
        db.add(row)
    else:
        row.scan_json = json.dumps(body.scan)
    await db.commit()


@router.delete("/scan-cache", status_code=status.HTTP_204_NO_CONTENT)
async def clear_scan_cache(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FarmBuilderScanCache).where(FarmBuilderScanCache.user_id == user.id)
    )
    row = result.scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
