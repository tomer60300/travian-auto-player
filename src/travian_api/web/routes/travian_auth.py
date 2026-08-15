"""Travian server connection and saved-credentials management routes."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from travian_api.web.auth import (
    decrypt_credential,
    encrypt_credential,
    get_current_user,
)
from travian_api.web.models.db import TravianCredential, User, get_db
from travian_api.web.sessions import TravianSession, session_manager, try_restore_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/travian", tags=["travian"])

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TravianConnectRequest(BaseModel):
    server_url: str
    username: str
    password: str


class TravianStatusResponse(BaseModel):
    connected: bool
    server_url: str | None = None
    player_name: str | None = None
    tribe_id: int | None = None
    active_village_id: int | None = None
    villages: list[dict] = []


class SavedServerRequest(BaseModel):
    server_url: str
    username: str
    password: str
    label: str | None = None


class SavedServerResponse(BaseModel):
    id: int
    server_url: str
    username: str
    label: str | None
    last_connected: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_to_status(session: TravianSession | None) -> TravianStatusResponse:
    """Build a ``TravianStatusResponse`` from a session (or ``None``)."""
    if session is None or session.auth_state is None:
        return TravianStatusResponse(connected=False)

    villages = [
        {
            "id": v.id,
            "name": v.name,
            "x": v.x,
            "y": v.y,
            "is_main_village": v.is_main_village,
        }
        for v in session.auth_state.villages
    ]

    return TravianStatusResponse(
        connected=True,
        server_url=session.server_url,
        player_name=session.player_name,
        tribe_id=session.tribe_id,
        active_village_id=session.active_village_id,
        villages=villages,
    )


def _credential_to_response(cred: TravianCredential) -> SavedServerResponse:
    return SavedServerResponse(
        id=cred.id,
        server_url=cred.server_url,
        username=cred.travian_username,
        label=cred.label,
        last_connected=cred.last_connected.isoformat() if cred.last_connected else None,
    )


async def _update_last_connected(
    db: AsyncSession,
    user_id: int,
    server_url: str,
    travian_username: str,
) -> None:
    """If a saved credential matches the server/username, update its timestamp.

    Best-effort: this runs AFTER a successful Travian login, so a database
    hiccup here must not make the whole connect report failure while a live
    session sits installed in the manager.
    """
    try:
        result = await db.execute(
            select(TravianCredential).where(
                TravianCredential.user_id == user_id,
                TravianCredential.server_url == server_url,
                TravianCredential.travian_username == travian_username,
            )
        )
        # first(), not scalar_one_or_none(): databases from before the upsert
        # in save_server may hold duplicate rows for the same account.
        cred = result.scalars().first()
        if cred is not None:
            cred.last_connected = datetime.now(UTC)
            await db.commit()
    except Exception:
        logger.warning("Connected user %s but could not stamp last_connected", user_id)


# ---------------------------------------------------------------------------
# Connection routes
# ---------------------------------------------------------------------------


@router.post("/connect", response_model=TravianStatusResponse)
async def connect(
    body: TravianConnectRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Authenticate to a Travian server and establish a session."""
    try:
        session = await session_manager.connect(
            user_id=user.id,
            server_url=body.server_url,
            username=body.username,
            password=body.password,
        )
    except HTTPException:
        # Control-flow errors from the session manager (e.g. 409 while
        # operations are running) must reach the client as themselves.
        raise
    except Exception as exc:
        logger.exception("Travian connect failed for user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to Travian server: {exc}",
        )

    # Update last_connected on any matching saved credential
    await _update_last_connected(db, user.id, body.server_url, body.username)

    return _session_to_status(session)


@router.delete("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(user: User = Depends(get_current_user)):
    """Tear down the current Travian session."""
    await session_manager.disconnect(user.id)


@router.get("/status", response_model=TravianStatusResponse)
async def get_status(user: User = Depends(get_current_user)):
    """Return the current Travian connection state and player info.

    Attempts a saved-credential restore when no live session exists: the
    frontend's initial mount and health poll key off this route, so without
    the attempt a backend restart sends users to /connect despite saved
    credentials. The restore is best-effort and returns None on failure.
    """
    session = session_manager.get(user.id)
    if session is None:
        session = await try_restore_session(user.id)
    return _session_to_status(session)


@router.post("/reconnect", response_model=TravianStatusResponse)
async def reconnect(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reconnect using the credentials from the current (or last) session.

    This re-authenticates without requiring the caller to resend credentials.
    """
    session = session_manager.get(user.id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active session to reconnect. Use POST /api/travian/connect instead.",
        )

    server_url = session.server_url
    username = session.settings.username
    password = session.settings.password

    try:
        new_session = await session_manager.connect(
            user_id=user.id,
            server_url=server_url,
            username=username,
            password=password,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Travian reconnect failed for user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to reconnect to Travian server: {exc}",
        )

    await _update_last_connected(db, user.id, server_url, username)

    return _session_to_status(new_session)


# ---------------------------------------------------------------------------
# Saved-server CRUD routes
# ---------------------------------------------------------------------------


@router.get("/servers", response_model=list[SavedServerResponse])
async def list_servers(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all saved Travian server credentials for the current user."""
    result = await db.execute(
        select(TravianCredential)
        .where(TravianCredential.user_id == user.id)
        .order_by(TravianCredential.created_at.desc())
    )
    credentials = result.scalars().all()
    return [_credential_to_response(c) for c in credentials]


@router.post("/servers", response_model=SavedServerResponse, status_code=status.HTTP_201_CREATED)
async def save_server(
    body: SavedServerRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save Travian server credentials (password is encrypted at rest).

    Upserts on (user, server, username): duplicates would break the
    last_connected stamping and let auto-restore pick an older row with an
    outdated password, so saving the same account again rotates it in place.
    """
    result = await db.execute(
        select(TravianCredential).where(
            TravianCredential.user_id == user.id,
            TravianCredential.server_url == body.server_url,
            TravianCredential.travian_username == body.username,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is not None:
        cred.encrypted_password = encrypt_credential(body.password)
        cred.label = body.label
    else:
        cred = TravianCredential(
            user_id=user.id,
            server_url=body.server_url,
            travian_username=body.username,
            encrypted_password=encrypt_credential(body.password),
            label=body.label,
        )
        db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return _credential_to_response(cred)


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a saved Travian server credential."""
    result = await db.execute(
        select(TravianCredential).where(
            TravianCredential.id == server_id,
            TravianCredential.user_id == user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Saved server not found",
        )

    await db.delete(cred)
    await db.commit()


@router.post("/servers/{server_id}/connect", response_model=TravianStatusResponse)
async def connect_saved_server(
    server_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Connect to a Travian server using saved credentials."""
    # 1. Look up the credential, ensuring it belongs to this user
    result = await db.execute(
        select(TravianCredential).where(
            TravianCredential.id == server_id,
            TravianCredential.user_id == user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Saved server not found",
        )

    # 2. Decrypt the password
    password = decrypt_credential(cred.encrypted_password)

    # 3. Connect via session_manager
    try:
        session = await session_manager.connect(
            user_id=user.id,
            server_url=cred.server_url,
            username=cred.travian_username,
            password=password,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Travian connect via saved server %s failed for user %s", server_id, user.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to Travian server: {exc}",
        )

    # 4. Update last_connected timestamp — best-effort bookkeeping: a DB
    # hiccup here must not report the successful login as a failure.
    try:
        cred.last_connected = datetime.now(UTC)
        await db.commit()
    except Exception:
        logger.warning("Connected user %s but could not stamp last_connected", user.id)

    return _session_to_status(session)
