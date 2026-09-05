"""Tests for the shared debug dump facility."""

from __future__ import annotations

import json
import time

import pytest

from travian_api.debug_dump import DebugDumper


def test_dump_writes_file_and_latest(tmp_path):
    d = DebugDumper(root=tmp_path)
    path = d.dump("no_checksum", "<html>body</html>", key="slot39_v20002")
    assert path is not None and path.exists()
    # Timestamped file and _latest file both exist under the category dir.
    cat = tmp_path / "no_checksum"
    assert any(p.name.startswith("slot39_v20002_") and p.suffix == ".html" for p in cat.iterdir())
    latest = cat / "slot39_v20002_latest.html"
    assert latest.exists()
    assert latest.read_bytes() == b"<html>body</html>"


def test_dump_context_sidecar(tmp_path):
    d = DebugDumper(root=tmp_path)
    d.dump(
        "http_error",
        "<html>500 oops</html>",
        key="get_dorf1_500",
        context={"method": "GET", "status": 500, "url": "/dorf1.php"},
    )
    sidecars = list((tmp_path / "http_error").glob("*.json"))
    assert len(sidecars) == 1
    data = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert data["category"] == "http_error"
    assert data["status"] == 500


def test_dump_handles_ugly_keys(tmp_path):
    d = DebugDumper(root=tmp_path)
    path = d.dump("cat/with slash", "x", key="key with space & specials!")
    assert path is not None
    # Category and key are slugged — no slashes or special chars end up on disk.
    assert "/" not in path.parent.name
    assert "&" not in path.name and " " not in path.name


def test_dump_accepts_bytes(tmp_path):
    d = DebugDumper(root=tmp_path)
    path = d.dump("raw", b"\x00\x01\x02", key="binary", file_ext="bin")
    assert path is not None
    assert path.read_bytes() == b"\x00\x01\x02"


def test_prune_removes_every_file_older_than_the_ttl(tmp_path):
    # This test used to assert the opposite of its last two lines: `_latest`
    # was exempt from the TTL. That exemption kept one permanent, unredacted
    # copy of an authenticated game page per (category, key) on disk forever,
    # which is the retention half of the config audit's P1-6. `_latest` is a
    # tailing convenience nothing in this codebase reads back, so it now
    # expires with everything else.
    d = DebugDumper(root=tmp_path, ttl_s=3600)
    # Write a fresh dump.
    d.dump("no_checksum", "fresh", key="slot1")
    # Also drop an old timestamped + old sidecar file.
    old_ts = time.time() - 2 * 3600
    old_file = tmp_path / "no_checksum" / "slot1_111.html"
    old_file.write_text("stale")
    old_sidecar = tmp_path / "no_checksum" / "slot1_111.json"
    old_sidecar.write_text("{}")
    import os

    os.utime(old_file, (old_ts, old_ts))
    os.utime(old_sidecar, (old_ts, old_ts))

    # And backdate _latest — it is stale too, so it goes.
    latest = tmp_path / "no_checksum" / "slot1_latest.html"
    os.utime(latest, (old_ts, old_ts))

    removed = d.prune_once()

    assert removed == 3  # old_file + old_sidecar + the stale _latest
    assert not old_file.exists()
    assert not old_sidecar.exists()
    assert not latest.exists()


def test_dump_never_raises_on_write_failure(tmp_path, monkeypatch):
    d = DebugDumper(root=tmp_path)

    from pathlib import Path

    def boom(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom, raising=True)
    # Must not raise — callers are on hot paths.
    result = d.dump("no_checksum", "x", key="k")
    assert result is None


@pytest.mark.asyncio
async def test_start_cleanup_is_idempotent(tmp_path):
    d = DebugDumper(root=tmp_path, cleanup_interval_s=3600)
    d.start_cleanup()
    first = d._cleanup_task
    d.start_cleanup()  # second call must not replace the running task
    assert d._cleanup_task is first
    d.stop_cleanup()
