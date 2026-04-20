"""User authentication, JWT tokens, and credential encryption for the Travian Web UI."""

import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from travian_api.web.models.db import User, get_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24
_logger = logging.getLogger(__name__)

# Store keys in ~/.travian/ (same directory as the DB) instead of CWD
_KEYS_DIR = Path.home() / ".travian"
_KEYS_DIR.mkdir(parents=True, exist_ok=True)
KEYS_FILE = _KEYS_DIR / ".web_keys"

# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def _warn_if_world_readable(path: Path) -> None:
    """Log a warning if the keys file is readable by others (Unix only)."""
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            _logger.warning(
                "Security: %s is readable by other users (mode %o). "
                "Run: chmod 600 %s",
                path, mode & 0o777, path,
            )
    except (OSError, AttributeError):
        pass  # Windows or inaccessible — skip


def get_or_create_keys() -> tuple[str, str]:
    """Return (jwt_secret, fernet_key), creating the keys file if needed.

    The keys file is a JSON object stored in ``~/.travian/.web_keys``:
        {"jwt_secret": "...", "fernet_key": "..."}
    """
    if KEYS_FILE.exists():
        _warn_if_world_readable(KEYS_FILE)
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
        return data["jwt_secret"], data["fernet_key"]

    jwt_secret = os.urandom(32).hex()
    fernet_key = Fernet.generate_key().decode()

    KEYS_FILE.write_text(
        json.dumps({"jwt_secret": jwt_secret, "fernet_key": fernet_key}, indent=2),
        encoding="utf-8",
    )

    # Try to restrict permissions on Unix
    try:
        KEYS_FILE.chmod(0o600)
    except OSError:
        pass

    return jwt_secret, fernet_key


# Eagerly load keys so the module-level helpers work immediately.
SECRET_KEY, FERNET_KEY = get_or_create_keys()

_fernet = Fernet(FERNET_KEY.encode())

# ---------------------------------------------------------------------------
# Password hashing  (bcrypt)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return a bcrypt hash of *password*."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Check *password* against a bcrypt *hashed* value."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------


def create_access_token(user_id: int, username: str) -> str:
    """Create a signed JWT containing the user's id and username."""
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and verify a JWT.  Returns ``{"user_id": int, "username": str}``.

    Raises ``jwt.ExpiredSignatureError`` or ``jwt.InvalidTokenError`` on failure.
    """
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    return {"user_id": payload["user_id"], "username": payload["username"]}


# ---------------------------------------------------------------------------
# Credential encryption  (Fernet)
# ---------------------------------------------------------------------------


def encrypt_credential(plaintext: str) -> str:
    """Fernet-encrypt *plaintext* and return the ciphertext as a string."""
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted *ciphertext* back to plaintext."""
    return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/users/login")


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Decode the Bearer token and return the corresponding `User` row.

    Also sets ``request.state.user_id`` so that downstream dependencies
    (e.g. the rate limiter) can key on the authenticated user rather than IP.

    Raises HTTP 401 if the token is invalid/expired or the user no longer exists.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_access_token(token)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise credentials_exception

    user_id: int = payload["user_id"]

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception

    # Expose user_id on request state for rate limiting and other middleware
    request.state.user_id = user_id

    return user
