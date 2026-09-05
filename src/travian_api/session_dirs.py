"""Hardening for the on-disk session data directories.

Its own module because two layers need it -- ``web/sessions.py`` for a user's
cookie and JWT caches, ``services/recon_account.py`` for the recon proxy's --
and a service importing a web module to get one function would invert the
layering for no reason.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionDirectoryUnsafe(RuntimeError):
    """The session data directory could not be made private to this user."""


def harden_session_dir(path: Path) -> None:
    """Make *path* private (0o700), and refuse to use it if that fails.

    The failure this catches is not hypothetical. The identity segment of the
    session path is ``sha256(f"{server}|{username}")[:16]`` -- computable by
    anyone who knows the Travian username -- and the tree is created with
    ``exist_ok=True``, so another local user can pre-create the leaf owned by
    themselves. The mkdir then succeeds, the chmod raises EPERM, and the server
    writes ``jwt_cache.json`` and ``cookies.json`` into their directory. The
    per-file ``chmod 600`` that follows runs AFTER ``write_text`` created the
    file, so it is a race rather than a guard.

    Swallowing the ``OSError`` everywhere -- which is what the code did, under
    the comment "Windows ACL may not support chmod" -- silenced exactly that.
    The comment is true, and true only on Windows, where ``stat``/``chmod`` do
    not describe the NTFS ACL at all (``web/auth.py::_warn_if_world_readable``
    returns early there for the same reason). So on Windows this stays
    best-effort; on POSIX a directory we cannot make private is not a directory
    to put a session token in.
    """
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        if os.name == "nt":
            logger.debug("Could not chmod %s (%s); harden with icacls instead", path, exc)
            return
        raise SessionDirectoryUnsafe(
            f"Refusing to use {path}: it could not be made private to this user "
            f"({exc}). Another local user may own it -- the path is derivable "
            "from the Travian username -- and session cookies written there "
            "would be readable by them. Remove or take ownership of the "
            "directory and reconnect."
        ) from exc
