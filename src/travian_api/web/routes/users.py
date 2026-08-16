"""User registration, login, and profile routes."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from travian_api.web.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from travian_api.web.models.db import User, get_db
from travian_api.web.rate_limit import auth_limiter

router = APIRouter(prefix="/api/users", tags=["users"])

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6)

    @field_validator("password")
    @classmethod
    def _within_bcrypt_byte_limit(cls, value: str) -> str:
        # bcrypt only reads the first 72 BYTES and, from bcrypt 5, raises for
        # anything longer. Bytes, not characters: an emoji is four. Without
        # this check a long password passes validation and detonates inside
        # hash_password(), turning a user mistake into a 500.
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 bytes (UTF-8 encoded)")
        return value


class UserResponse(BaseModel):
    id: int
    username: str
    created_at: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _rate: None = Depends(auth_limiter),
):
    """Create a new user account and return a JWT."""
    # Check for duplicate username
    result = await db.execute(select(User).where(User.username == body.username))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )

    # bcrypt is intentionally slow; run it off the event loop so a burst of
    # registrations cannot stall unrelated API/WebSocket traffic.
    password_hash = await asyncio.to_thread(hash_password, body.password)
    user = User(
        username=body.username,
        password_hash=password_hash,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Two concurrent registrations can both pass the SELECT above; the
        # unique constraint decides the winner and the loser gets the same
        # 409 a sequential duplicate would.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )
    await db.refresh(user)

    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _rate: None = Depends(auth_limiter),
):
    """Authenticate an existing user and return a JWT."""
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    # Offload the bcrypt compare (same DoS/event-loop concern as register). A
    # missing user still returns 401 without hashing; the per-IP limiter above
    # is what bounds brute force against a known username.
    if user is None or not await asyncio.to_thread(
        verify_password, body.password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)):
    """Return the profile of the currently authenticated user."""
    return UserResponse(
        id=user.id,
        username=user.username,
        created_at=user.created_at.isoformat(),
    )
