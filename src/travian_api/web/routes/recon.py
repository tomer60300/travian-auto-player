"""Background ("recon") account credential management.

The recon account masks account-independent gathering reads. Its credentials
previously came only from ``.env``, which made the "rotate the recon
credentials, then retry" advice impossible to follow without editing a file and
restarting the process. These routes store them encrypted at rest and apply a
rotation to the live runtime.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from travian_api.services.recon_account import recon_account_manager
from travian_api.web.auth import decrypt_credential, encrypt_credential, get_current_user
from travian_api.web.models.db import ReconCredential, User, get_db
from travian_api.web.sessions import TravianSession, get_travian_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recon", tags=["recon"])


class ReconCredentialRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    # The world this recon account exists on. None = any (single-world
    # deployments); when set, the account only masks reads on that server.
    server_url: str | None = None


class ReconStatusResponse(BaseModel):
    configured: bool
    username: str | None = None
    # "stored" (set here), "env" (.env fallback), or None when unconfigured.
    source: str | None = None
    # False for everyone but the instance operator, so the UI can hide the
    # management controls instead of offering buttons that will 403.
    manageable: bool = True


class ReconTestResponse(BaseModel):
    ok: bool
    username: str | None = None
    detail: str | None = None


def _status(manageable: bool) -> ReconStatusResponse:
    return ReconStatusResponse(
        configured=recon_account_manager.is_configured(),
        # The recon username is the operator's business only.
        username=recon_account_manager.get_proxy_username() if manageable else None,
        source=recon_account_manager.credentials_source(),
        manageable=manageable,
    )


async def _first_user_id(db: AsyncSession) -> int | None:
    result = await db.execute(select(func.min(User.id)))
    return result.scalar()


async def get_instance_operator(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """The earliest-registered user, the only one who manages shared state.

    ReconAccountManager is a process-global singleton backed by a single
    credential row: letting any authenticated user rotate or clear it would let
    one web user silently break every other user's recon setup.
    """
    first_id = await _first_user_id(db)
    if first_id is not None and user.id != first_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the instance operator (the first registered user) can "
                "manage the shared background account."
            ),
        )
    return user


async def load_stored_credentials(db: AsyncSession) -> bool:
    """Push stored credentials into the manager. Returns True if any existed.

    Called at startup so a rotation survives a restart, and after every write.
    """
    result = await db.execute(select(ReconCredential).order_by(ReconCredential.id).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        recon_account_manager.clear_credentials()
        return False
    recon_account_manager.set_credentials(
        row.travian_username,
        decrypt_credential(row.encrypted_password),
        server_url=getattr(row, "server_url", None),
    )
    return True


@router.get("/status", response_model=ReconStatusResponse)
async def get_recon_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Whether a background account is available, and where it came from."""
    first_id = await _first_user_id(db)
    return _status(manageable=first_id is None or user.id == first_id)


@router.put("/credentials", response_model=ReconStatusResponse)
async def set_recon_credentials(
    body: ReconCredentialRequest,
    _operator: User = Depends(get_instance_operator),
    db: AsyncSession = Depends(get_db),
):
    """Store rotated credentials and apply them to the live runtime."""
    result = await db.execute(select(ReconCredential).order_by(ReconCredential.id).limit(1))
    row = result.scalar_one_or_none()

    server_url = body.server_url.rstrip("/") if body.server_url else None
    if row is None:
        row = ReconCredential(
            travian_username=body.username,
            encrypted_password=encrypt_credential(body.password),
            server_url=server_url,
        )
        db.add(row)
    else:
        row.travian_username = body.username
        row.encrypted_password = encrypt_credential(body.password)
        row.server_url = server_url

    await db.commit()

    recon_account_manager.set_credentials(body.username, body.password, server_url=server_url)
    # A cached ReconAccount holds the old password and a sticky failure window;
    # without dropping it the rotation could not take effect.
    await recon_account_manager.invalidate()
    logger.info("Recon credentials rotated; cached recon sessions dropped")

    return _status(manageable=True)


@router.delete("/credentials", response_model=ReconStatusResponse)
async def clear_recon_credentials(
    _operator: User = Depends(get_instance_operator),
    db: AsyncSession = Depends(get_db),
):
    """Forget stored credentials and fall back to the .env values, if any."""
    result = await db.execute(select(ReconCredential))
    for row in result.scalars().all():
        await db.delete(row)
    await db.commit()

    recon_account_manager.clear_credentials()
    await recon_account_manager.invalidate()
    logger.info("Stored recon credentials cleared; falling back to environment")

    return _status(manageable=True)


@router.post("/test", response_model=ReconTestResponse)
async def test_recon_credentials(
    # Authorization first: dependencies resolve in signature order, and the
    # session dependency may spend a real auto-reconnect login.
    _operator: User = Depends(get_instance_operator),
    session: TravianSession = Depends(get_travian_session),
):
    """Attempt a real login with the active recon credentials.

    Uses the connected server so the operator does not have to retype it. This
    authenticates the background account only — it never touches the primary
    session.
    """
    if not recon_account_manager.is_configured():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No background account configured.",
        )

    username = recon_account_manager.get_proxy_username()
    try:
        client = await recon_account_manager.get_or_create_client(session.settings.base_url)
    except Exception as exc:  # get_or_create_client is documented never to raise
        logger.exception("Recon test failed unexpectedly")
        return ReconTestResponse(ok=False, username=username, detail=str(exc))

    if client is None:
        return ReconTestResponse(
            ok=False,
            username=username,
            detail=(
                "Authentication failed. Check the username and password, and "
                "that the account is not banned or sitting-locked."
            ),
        )
    return ReconTestResponse(ok=True, username=username)
