"""User authentication, JWT tokens, and credential encryption for the Travian Web UI."""

import json
import logging
import os
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from travian_api.web.models.db import DB_DIR, User, get_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24
_logger = logging.getLogger(__name__)

# Keys live NEXT TO the database they encrypt for: with a custom
# TRAVIAN_DB_PATH, keys pinned to ~/.travian mean moving or reusing that DB
# elsewhere cannot decrypt its own credential rows. The default DB dir is
# ~/.travian, so default deployments keep their existing key file.
#
# That adjacency is also the weakness (config audit P1-7): anything that reads
# the directory -- a backup, a cloud-sync client, another local user -- gets the
# ciphertext and its key in one copy. These two variables are the way out, and
# `src/travian_api/CLAUDE.md` already asks for them ("Fernet encryption keys
# from environment variables, never hardcoded"); the file remains the fallback
# for every deployment that has one.
KEYS_FILE = DB_DIR / ".web_keys"
_LEGACY_KEYS_FILE = Path.home() / ".travian" / ".web_keys"
_JWT_ENV = "TRAVIAN_JWT_SECRET"
_FERNET_ENV = "TRAVIAN_FERNET_KEY"

# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


class WebKeysCorrupt(RuntimeError):
    """The keys file exists but cannot be read as a key pair.

    Raised instead of letting a bare ``JSONDecodeError`` or ``KeyError`` out of
    :func:`get_or_create_keys`, which runs at module import: the traceback then
    pointed at a JSON parse rather than at the one file that holds the only
    copy of the Fernet key for every stored credential. Regenerating past this
    is not a recovery -- it orphans every encrypted password row -- so the
    message says which file and says not to delete it.
    """


class WebKeysMisconfigured(RuntimeError):
    """The environment says something about the keys that cannot be honoured.

    Half the pair, or a Fernet key the library will not accept. Both are
    refused loudly at import rather than absorbed: a server that quietly fell
    back to the file for the missing half would sign tokens with one key and
    decrypt credentials with another, and the operator would learn about it
    from a decryption failure hours later.
    """


def _keys_from_environment() -> tuple[str, str] | None:
    """Return the (jwt_secret, fernet_key) pair the environment supplies.

    None when it supplies neither, which is the ordinary case and sends the
    caller to the file. Both or nothing -- see :class:`WebKeysMisconfigured`.
    """
    jwt_secret = os.environ.get(_JWT_ENV, "").strip()
    fernet_key = os.environ.get(_FERNET_ENV, "").strip()

    if not jwt_secret and not fernet_key:
        return None
    if not jwt_secret or not fernet_key:
        missing, present = (_JWT_ENV, _FERNET_ENV) if not jwt_secret else (_FERNET_ENV, _JWT_ENV)
        raise WebKeysMisconfigured(
            f"{present} is set but {missing} is not. The two are read together: "
            f"set both to move the keys out of {KEYS_FILE}, or unset both to keep "
            "using that file."
        )

    try:
        Fernet(fernet_key.encode())
    except (ValueError, TypeError) as exc:
        raise WebKeysMisconfigured(
            f"{_FERNET_ENV} is not a valid Fernet key "
            f"({type(exc).__name__}: {exc}). It must be the SAME key the stored "
            f"credentials were encrypted with -- the one in {KEYS_FILE} if you "
            "are moving it out of there -- because a new one leaves every "
            "credential row permanently undecryptable."
        ) from exc

    return jwt_secret, fernet_key


def _read_keys(path: Path) -> tuple[str, str]:
    """Read (jwt_secret, fernet_key) from *path*, or say why it cannot be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["jwt_secret"], data["fernet_key"]
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise WebKeysCorrupt(
            f"{path} is not a readable key file ({type(exc).__name__}: {exc}). "
            "It holds the only copy of the Fernet key that decrypts every stored "
            "Travian password, so DO NOT delete it and do not let the server "
            "regenerate one -- every credential row would become permanently "
            "undecryptable. Restore the file from a backup, or delete the "
            "credential rows deliberately and re-enter the passwords."
        ) from exc


def _write_keys(path: Path, data: dict[str, str]) -> None:
    """Write the keys file atomically, so a reader never finds a fragment.

    The bare ``write_text`` this replaces could be interrupted mid-write --
    power loss during the one-time creation -- leaving a truncated file that
    bricks the next start. Same mkstemp/os.replace pattern as
    ``stealth/scheduler.py``, which is where this repository already had it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            # Windows: stat/chmod do not describe the NTFS ACL. Harden with
            # icacls instead; see _warn_if_world_readable.
            pass
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _warn_if_world_readable(path: Path) -> None:
    """Log a warning if the keys file is readable by others (Unix only).

    Skipped on Windows: ``stat()`` there reports a synthetic 0o666 that has no
    relationship to the actual NTFS ACL, so this fired unconditionally and told
    operators to run a chmod that cannot change anything. Use ``icacls`` to
    restrict the file instead.
    """
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            _logger.warning(
                "Security: %s is readable by other users (mode %o). Run: chmod 600 %s",
                path,
                mode & 0o777,
                path,
            )
    except (OSError, AttributeError):
        pass  # Windows or inaccessible — skip


def get_or_create_keys() -> tuple[str, str]:
    """Return (jwt_secret, fernet_key), creating the keys file if needed.

    ``TRAVIAN_JWT_SECRET`` + ``TRAVIAN_FERNET_KEY`` win when both are set, and
    the file is then neither read nor written -- that is the whole point, since
    the file's problem is the directory it is in (P1-7).

    Otherwise the keys file, a JSON object stored in ``~/.travian/.web_keys``:
        {"jwt_secret": "...", "fernet_key": "..."}
    """
    from_environment = _keys_from_environment()
    if from_environment is not None:
        return from_environment

    if KEYS_FILE.exists():
        _warn_if_world_readable(KEYS_FILE)
        return _read_keys(KEYS_FILE)

    # One-time migration: deployments that ran a custom TRAVIAN_DB_PATH before
    # keys followed the DB have their keys in ~/.travian. Regenerating instead
    # of migrating would orphan every credential row that DB already holds.
    if KEYS_FILE != _LEGACY_KEYS_FILE and _LEGACY_KEYS_FILE.exists():
        jwt_secret, fernet_key = _read_keys(_LEGACY_KEYS_FILE)
        _write_keys(KEYS_FILE, {"jwt_secret": jwt_secret, "fernet_key": fernet_key})
        _logger.info("Migrated web keys from %s to %s", _LEGACY_KEYS_FILE, KEYS_FILE)
        return jwt_secret, fernet_key

    jwt_secret = os.urandom(32).hex()
    fernet_key = Fernet.generate_key().decode()
    _write_keys(KEYS_FILE, {"jwt_secret": jwt_secret, "fernet_key": fernet_key})
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
    """Check *password* against a bcrypt *hashed* value.

    A password over bcrypt's 72-byte limit cannot be the right one -- nothing
    that long was ever hashed -- but bcrypt 5 raises for it rather than
    returning False. Login must answer "invalid credentials", not a 500, so
    the impossible length is the answer, not an error.
    """
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        return False
    return bcrypt.checkpw(encoded, hashed.encode("utf-8"))


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------


def create_access_token(user_id: int, username: str) -> str:
    """Create a signed JWT containing the user's id and username."""
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.now(UTC) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
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
