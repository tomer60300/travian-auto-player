"""One live executor per game account, across PROCESSES.

`TradeRouteService.execute_lock` serialises runs inside one interpreter, and
that is all it can do: it is an `asyncio.Lock` in memory. The production server
on :80 and a debug server on :8001 run the same code against the same account
and share nothing, so both could read the marketplace before either wrote to it
and each would then create what the other had just made.

This is the durable half, and it is held **by the operating system**: an
exclusive byte-range lock on a file, taken non-blocking. The kernel owns it, so
it is released when the handle closes -- including when the process is killed,
crashes, or is powered off. There is nothing to expire and nothing to clean up.

## Why not a lease with a timeout

The first version of this file was one, and it was wrong twice over. A lease
that another process may take after N minutes assumes no legitimate run lasts
longer than N; the browser's 180-second request timeout does not stop the
backend operation, so that assumption does not hold, and a second executor could
start while the first was still writing. Worse, release was an unconditional
`unlink`: the original holder, finishing late, would delete the REPLACEMENT'S
lease and let a third process in. Both were reproduced.

An OS lock has neither failure mode. It cannot be stolen while the holder lives,
and it cannot be released by anyone but the holder.

## What it does not cover

One filesystem. Two machines against one account is outside its reach, and so is
a filesystem whose locking is advisory-only across hosts (NFS without a lock
daemon). "The operator's machine" is the scope.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path

from travian_api.exceptions import TravianError

logger = logging.getLogger(__name__)

LEASE_DIR = Path.home() / ".travian" / "locks"
"""Where the lock files live. Repointed by the test suite's conftest, like the
trace directory, so a run never touches the operator's real one."""


class AccountBusy(TravianError):
    """Another process is already executing against this account.

    Refused rather than queued. Waiting would mean holding an HTTP request open
    for however long the other run takes, and the honest answer to "someone else
    is writing to your account right now" is to say so.
    """


def _lock_path(account_key: str) -> Path:
    # Hashed, because the key is a server URL plus a login and these files sit
    # in a directory anyone on the machine can list. The digest identifies the
    # account without naming it.
    digest = hashlib.sha256(account_key.encode("utf-8")).hexdigest()[:16]
    return LEASE_DIR / f"execute-{digest}.lock"


def _holder_path(lock_path: Path) -> Path:
    """Diagnostics, kept OUT of the locked file.

    On Windows a byte-range lock is mandatory: a second process reading the
    locked byte gets an error rather than the bytes. Writing who-holds-it
    alongside instead of inside keeps the refusal message readable.
    """
    return lock_path.with_suffix(".holder")


def _acquire(fd: int) -> None:
    """Take the exclusive lock, or raise OSError if another handle holds it."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fd: int) -> None:
    """Drop the lock. Closing the handle would do it too; this is explicit."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:  # pragma: no cover - the close below still releases it
        logger.warning("could not explicitly release the execute lock: %s", exc)


def _describe(lock_path: Path) -> str:
    """Who holds this lock, for the refusal message."""
    try:
        held = json.loads(_holder_path(lock_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "another process (it left no readable record of itself)"
    age = max(0, int(time.time() - float(held.get("started", 0))))
    return (
        f"pid {held.get('pid', '?')} on {held.get('host', '?')} "
        f"({held.get('purpose', 'execute')}, started {age}s ago)"
    )


@contextlib.contextmanager
def account_execute_lease(account_key: str, *, purpose: str = "execute") -> Iterator[Path]:
    """Hold the account's write lock for the duration of the block.

    Raises :class:`AccountBusy` if another process holds it. Released on the way
    out, including on an exception, and by the kernel if this process never gets
    that far.
    """
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(account_key)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            _acquire(fd)
        except OSError:
            raise AccountBusy(
                f"Another process is writing to this account: {_describe(lock_path)}. "
                f"Run one live executor per account -- the server on :80 and a debug "
                f"server share the game, not this lock. The lock is held by the "
                f"operating system and is released the moment that process ends, so "
                f"there is nothing to clean up by hand."
            ) from None
        # Written only once the lock is ours, so the record can never describe a
        # process that failed to take it. Best effort: losing the diagnostics
        # must not lose the lock.
        with contextlib.suppress(OSError):
            _holder_path(lock_path).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "purpose": purpose,
                        "started": time.time(),
                    }
                ),
                encoding="utf-8",
            )
        try:
            yield lock_path
        finally:
            _release(fd)
    finally:
        # Closing releases the lock even if `_release` could not, which is why
        # the file is never unlinked: deleting it is what let a late finisher
        # remove somebody else's lock. An empty lock file per account is a
        # cheaper thing to leave behind than that bug.
        os.close(fd)
