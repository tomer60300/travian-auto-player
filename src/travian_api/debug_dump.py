"""Shared HTML/response dump facility for exceptional server responses.

Any place in the codebase that hits an unexpected server response (HTTP
error, captcha trigger, parser-found-nothing, session redirect to login)
should call ``debug_dumper.dump(...)`` so the raw response lands on disk
for post-mortem inspection.

Files live under ``%TEMP%/travian_debug/<category>/`` with filenames
``<key>_<unix_ts>.<ext>`` plus a stable ``<key>_latest.<ext>`` for easy
tailing. A JSON sidecar with the same stem records context (url, status,
slot_id, user_id, etc.).

A background task prunes anything older than ``DEFAULT_TTL_S`` (24 h) so
disk usage stays bounded on long-running servers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path(tempfile.gettempdir()) / "travian_debug"
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
        hunting timestamps.

        Returns the path written, or None if the write failed. Swallows
        OSError so the hot path never dies because debug disk is full.
        """
        try:
            category_dir = self.root / _slug(category)
            category_dir.mkdir(parents=True, exist_ok=True)

            ts = int(time.time())
            stem_base = _slug(key) if key else "dump"
            stem_timestamped = f"{stem_base}_{ts}"
            stem_latest = f"{stem_base}_latest"

            data: bytes = (
                content if isinstance(content, bytes) else content.encode("utf-8", errors="replace")
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
        except OSError:
            logger.exception("Debug dump failed for category=%s key=%s", category, key)
            return None

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
            # Never prune `_latest` files — they're meant to always reflect
            # the most recent hit of each (category, key) even if old.
            if path.stem.endswith("_latest"):
                continue
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
