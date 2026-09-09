"""One live executor per game account, across PROCESSES.

`TradeRouteService.execute_lock` serialises runs inside one interpreter, and
that is all it can do: it is an `asyncio.Lock` in memory. The production server
on :80 and a debug server on :8001 run the same code against the same account
and share nothing, so both could read the marketplace before either wrote to it
and each would then create what the other had just made.

This is the durable half. A lease is a file whose creation is atomic --
``O_CREAT | O_EXCL`` -- so exactly one process can hold one at a time, whatever
interpreter it is in. The holder writes down who it is; a second process is
refused with that information rather than left guessing.

**Staleness is by AGE, not by asking whether the holder is alive.** Checking
liveness would mean `os.kill(pid, 0)`, and on Windows that is not a probe: it
calls `TerminateProcess`, so the check would kill the very run it was asking
about. A generous TTL is the honest trade. The lease is released in a `finally`,
so it only outlives its holder after a hard kill or a power cut, and then the
wait is bounded rather than forever.

Not a distributed lock. It assumes one filesystem, which is what "the operator's
machine" means here. Two machines against one account remain the operator's
problem to avoid.
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
"""Where leases live. Repointed by the test suite's conftest, like the trace
directory, so a run never touches the operator's real one."""

LEASE_TTL_SECONDS = 15 * 60
"""How long a lease may sit untouched before another process may take it.

Longer than any single execute request can legitimately run: the browser's own
chunk timeout is 180s, and a chunk is a handful of villages of paced traffic. A
lease older than this was left behind by a process that died, because a live one
would have released it in its `finally`.
"""


class AccountBusy(TravianError):
    """Another process is already executing against this account.

    Refused rather than queued. Waiting would mean holding an HTTP request open
    for however long the other run takes, and the honest answer to "someone else
    is writing to your account right now" is to say so.
    """


def _lease_path(account_key: str) -> Path:
    # Hashed, because the key is a server URL plus a login and a lease file name
    # is world-readable on a shared machine. The digest identifies the account
    # without naming it.
    digest = hashlib.sha256(account_key.encode("utf-8")).hexdigest()[:16]
    return LEASE_DIR / f"execute-{digest}.lease"


def _describe(path: Path) -> str:
    """Who holds this lease, for the refusal message."""
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "another process (its lease file could not be read)"
    age = max(0, int(time.time() - float(held.get("started", 0))))
    return (
        f"pid {held.get('pid', '?')} on {held.get('host', '?')} "
        f"({held.get('purpose', 'execute')}, started {age}s ago)"
    )


@contextlib.contextmanager
def account_execute_lease(account_key: str, *, purpose: str = "execute") -> Iterator[Path]:
    """Hold the account's write lease for the duration of the block.

    Raises :class:`AccountBusy` if another process holds it. Releases on the way
    out, including on an exception -- a run that fails still has to let the next
    one in.
    """
    path = _lease_path(account_key)
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 2:
                raise AccountBusy(
                    f"Another process is writing to this account: {_describe(path)}. "
                    f"Run one live executor per account -- the server on :80 and a "
                    f"debug server share the game, not this lock. If that process is "
                    f"gone, the lease is taken over automatically after "
                    f"{LEASE_TTL_SECONDS // 60} minutes, or delete {path}."
                ) from None
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                # It went away between the open and the stat: the holder
                # finished. Go round again and take it.
                continue
            if age <= LEASE_TTL_SECONDS:
                raise AccountBusy(
                    f"Another process is writing to this account: {_describe(path)}. "
                    f"Run one live executor per account -- the server on :80 and a "
                    f"debug server share the game, not this lock. If that process is "
                    f"gone, the lease is taken over automatically after "
                    f"{LEASE_TTL_SECONDS // 60} minutes, or delete {path}."
                ) from None
            # Stale: the holder died without releasing. Say so loudly -- a run
            # that ended this way may have left rows behind in the game.
            logger.warning(
                "taking over a stale execute lease at %s (%.0fs old, held by %s); "
                "the run that left it may not have finished",
                path,
                age,
                _describe(path),
            )
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        else:
            try:
                os.write(
                    handle,
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "host": socket.gethostname(),
                            "purpose": purpose,
                            "started": time.time(),
                        }
                    ).encode("utf-8"),
                )
            finally:
                os.close(handle)
            try:
                yield path
            finally:
                # Best effort: a lease we cannot delete becomes a stale one and
                # is taken over on age. Failing the run over it would be worse
                # than the wait.
                with contextlib.suppress(OSError):
                    path.unlink()
            return
    raise AssertionError("unreachable: the loop either yields or raises")
