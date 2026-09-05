"""Shared HTML/response dump facility for exceptional server responses.

Any place in the codebase that hits an unexpected server response (HTTP
error, captcha trigger, parser-found-nothing, session redirect to login)
should call ``debug_dumper.dump(...)`` so the raw response lands on disk
for post-mortem inspection.

Files live under ``~/.travian/debug/<category>/`` with filenames
``<key>_<unix_ts>.<ext>`` plus a ``<key>_latest.<ext>`` for easy tailing.
A JSON sidecar with the same stem records context (url, status, slot_id,
user_id, etc.).

What lands here is a **fully authenticated game page** -- the account name and
the per-page action checksum the app parses out of it -- so three things are
not optional and are exercised by ``tests/test_debug_dump_is_private.py``:

* the root is the application's own directory, alongside the database and the
  traces, and made private to this user. It used to be
  ``tempfile.gettempdir()/travian_debug``: a fixed path under a world-readable
  ``/tmp`` on any multi-user host, with no ``chmod`` anywhere in the module;
* every body is run through :func:`~travian_api.logging_config.redact_sensitive`
  before it is written, the same redaction the log records get;
* everything expires. ``_latest`` used to be exempt from the TTL, which left
  one permanent unredacted copy per (category, key) on disk forever. Nothing in
  this codebase reads ``_latest`` -- it is a convenience for a human tailing the
  most recent miss -- and a convenience does not need to outlive the evidence.

A background task prunes anything older than ``DEFAULT_TTL_S`` (24 h) so
disk usage stays bounded on long-running servers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .logging_config import redact_sensitive
from .session_dirs import SessionDirectoryUnsafe, harden_session_dir

logger = logging.getLogger(__name__)

# Alongside the database, the traces and the cookie jar, not in shared temp.
DEFAULT_ROOT = Path.home() / ".travian" / "debug"
DEFAULT_TTL_S = 24 * 60 * 60  # 24 hours
DEFAULT_CLEANUP_INTERVAL_S = 60 * 60  # hourly

# Filenames must not contain path separators, nulls, or characters that
# trip Windows filesystems. Everything outside this set is squashed to '_'.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str, max_len: int = 80) -> str:
    """Turn an arbitrary string into a filesystem-safe slug."""
    cleaned = _SAFE_CHARS.sub("_", value).strip("_")
    return cleaned[:max_len] if cleaned else "unknown"


class DebugDumper:
    """Writes raw server responses to disk with time-bounded retention."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        ttl_s: int = DEFAULT_TTL_S,
        cleanup_interval_s: int = DEFAULT_CLEANUP_INTERVAL_S,
    ) -> None:
        self.root = root or DEFAULT_ROOT
        self.ttl_s = ttl_s
        self.cleanup_interval_s = cleanup_interval_s
        self._cleanup_task: asyncio.Task | None = None

    def dump(
        self,
        category: str,
        content: str | bytes,
        *,
        key: str = "",
        context: dict[str, Any] | None = None,
        file_ext: str = "html",
    ) -> Path | None:
        """Write *content* to ``<root>/<category>/<key>_<ts>.<ext>``.

        A JSON sidecar with the same stem holds *context* (url, status,
        slot_id, etc.) and a ``<key>_latest.<ext>`` copy is always
        overwritten so you can tail the most recent miss without
        hunting timestamps. Text content is redacted first; ``bytes``
        content is written as given, because the only callers that pass
        bytes pass something that is not text to begin with.

        Returns the path written, or None if the write failed or the
        directory could not be made private. Swallows OSError so the hot
        path never dies because debug disk is full.
        """
        try:
            category_dir = self.root / _slug(category)
            self._private_dir(self.root)
            self._private_dir(category_dir)

            ts = int(time.time())
            stem_base = _slug(key) if key else "dump"
            stem_timestamped = f"{stem_base}_{ts}"
            stem_latest = f"{stem_base}_latest"

            data: bytes = (
                content
                if isinstance(content, bytes)
                else redact_sensitive(content).encode("utf-8", errors="replace")
            )

            path = category_dir / f"{stem_timestamped}.{file_ext}"
            path.write_bytes(data)

            latest = category_dir / f"{stem_latest}.{file_ext}"
            latest.write_bytes(data)

            if context:
                sidecar = {
                    "ts": ts,
                    "category": category,
                    "key": key,
                    **context,
                }
                try:
                    (category_dir / f"{stem_timestamped}.json").write_text(
                        json.dumps(sidecar, default=str, indent=2),
                        encoding="utf-8",
                    )
                except (OSError, TypeError):
                    logger.debug("Failed to write sidecar for %s", stem_timestamped, exc_info=True)

            logger.warning(
                "Debug dump [%s/%s]: %s (%d bytes)",
                category,
                stem_base,
                path,
                len(data),
            )
            return path
        except SessionDirectoryUnsafe as exc:
            logger.warning("Debug dump skipped, directory is not private: %s", exc)
            return None
        except OSError:
            logger.exception("Debug dump failed for category=%s key=%s", category, key)
            return None

    @staticmethod
    def _private_dir(directory: Path) -> None:
        """Create *directory* and make it readable only by this user.

        Both levels, not just the leaf: ``mkdir(parents=True)`` creates the
        parents with the umask default and takes ``mode`` into account for the
        leaf alone, which is the same "only the leaf is hardened" hole the
        session store had. The hardening rule itself is
        :func:`~travian_api.session_dirs.harden_session_dir` -- best-effort on
        Windows, where chmod does not describe the NTFS ACL, and fatal on
        POSIX, where a directory we cannot make private is not a directory to
        put an authenticated game page in.
        """
        directory.mkdir(parents=True, exist_ok=True)
        harden_session_dir(directory)

    def start_cleanup(self) -> None:
        """Spawn the periodic pruning task. Idempotent."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "Debug dump cleanup started (ttl=%ds, interval=%ds, root=%s)",
            self.ttl_s,
            self.cleanup_interval_s,
            self.root,
        )

    def stop_cleanup(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()

    def prune_once(self, now: float | None = None) -> int:
        """Delete everything older than ``ttl_s``. Returns files removed.

        Exposed for tests. Also used by the background loop.
        """
        cutoff = (now if now is not None else time.time()) - self.ttl_s
        removed = 0
        if not self.root.exists():
            return 0
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            # `_latest` used to be exempt here, which turned a 24 h retention
            # policy into one permanent copy of an authenticated page per
            # (category, key). It is a tailing convenience nothing reads back;
            # it expires with everything else.
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                logger.debug("Failed to prune %s", path, exc_info=True)
        if removed:
            logger.info("Debug dump cleanup: removed %d expired files", removed)
        return removed

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval_s)
                try:
                    self.prune_once()
                except Exception:
                    logger.exception("Debug dump cleanup pass failed")
        except asyncio.CancelledError:
            pass


# Module-level singleton.
debug_dumper = DebugDumper()
