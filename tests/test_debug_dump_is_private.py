"""The debug dumper writes fully authenticated game pages. Where, and how long.

Four separate defects from the config audit (P1-6), one file:

1. the root was ``tempfile.gettempdir()/travian_debug`` -- a fixed, world-
   readable path on a multi-user OS, holding the operator's authenticated
   ``/build.php`` markup;
2. nothing in the module ever chmod'd anything, unlike every other file this
   app writes;
3. bodies went to disk verbatim, while the logging filter one module over
   redacts the same credential shapes out of log records;
4. ``_latest`` was deliberately exempt from the 24 h TTL, so one permanent
   copy per (category, key) survived forever.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from travian_api.debug_dump import DEFAULT_ROOT, DebugDumper
from travian_api.session_dirs import SessionDirectoryUnsafe


class TestTheRootIsTheApplicationsOwnDirectory:
    def test_the_default_root_is_not_shared_temp(self):
        shared = Path(tempfile.gettempdir()).resolve()
        assert shared not in DEFAULT_ROOT.resolve().parents

    def test_the_default_root_sits_with_the_database_and_the_traces(self):
        assert DEFAULT_ROOT.resolve() == (Path.home() / ".travian" / "debug").resolve()


class TestTheDirectoryIsPrivateOrUnused:
    def test_a_directory_that_cannot_be_made_private_is_not_written_to(self, tmp_path, monkeypatch):
        def refuse(path: Path) -> None:
            raise SessionDirectoryUnsafe(f"cannot harden {path}")

        monkeypatch.setattr("travian_api.debug_dump.harden_session_dir", refuse)
        dumper = DebugDumper(root=tmp_path / "root")

        assert dumper.dump("no_checksum", "<html>authenticated</html>", key="slot1") is None
        assert not list((tmp_path / "root").rglob("*.html"))

    @pytest.mark.skipif(os.name == "nt", reason="chmod does not describe an NTFS ACL")
    def test_the_root_and_the_category_are_owner_only(self, tmp_path):
        dumper = DebugDumper(root=tmp_path / "root")
        path = dumper.dump("no_checksum", "<html>x</html>", key="slot1")

        assert path is not None
        assert (tmp_path / "root").stat().st_mode & 0o777 == 0o700
        assert path.parent.stat().st_mode & 0o777 == 0o700


class TestTheBodyIsRedacted:
    def test_a_credential_in_the_body_does_not_reach_the_disk(self, tmp_path):
        dumper = DebugDumper(root=tmp_path)
        body = "<html>token=abc123def <b>Authorization: Bearer eyJhbGciOi</b></html>"

        path = dumper.dump("session_expired", body, key="login_redirect")

        assert path is not None
        written = path.read_text(encoding="utf-8")
        assert "abc123def" not in written
        assert "eyJhbGciOi" not in written
        assert "token=[REDACTED]" in written
        assert "Authorization: [REDACTED]" in written

    def test_the_latest_copy_is_redacted_too(self, tmp_path):
        dumper = DebugDumper(root=tmp_path)
        dumper.dump("session_expired", "<html>password=hunter2</html>", key="k")

        latest = tmp_path / "session_expired" / "k_latest.html"
        assert "hunter2" not in latest.read_text(encoding="utf-8")

    def test_the_surrounding_markup_survives(self, tmp_path):
        dumper = DebugDumper(root=tmp_path)
        path = dumper.dump("no_checksum", "<html><body>no checksum here</body></html>", key="k")

        assert path is not None
        assert path.read_text(encoding="utf-8") == "<html><body>no checksum here</body></html>"


class TestNothingSurvivesTheTtl:
    def test_a_stale_latest_is_pruned_like_everything_else(self, tmp_path):
        dumper = DebugDumper(root=tmp_path, ttl_s=3600)
        dumper.dump("no_checksum", "authenticated markup", key="slot1")

        latest = tmp_path / "no_checksum" / "slot1_latest.html"
        old = time.time() - 2 * 3600
        for path in (tmp_path / "no_checksum").iterdir():
            os.utime(path, (old, old))

        assert dumper.prune_once() == 2  # the timestamped copy AND _latest
        assert not latest.exists()

    def test_a_fresh_latest_is_kept(self, tmp_path):
        dumper = DebugDumper(root=tmp_path, ttl_s=3600)
        dumper.dump("no_checksum", "authenticated markup", key="slot1")

        assert dumper.prune_once() == 0
        assert (tmp_path / "no_checksum" / "slot1_latest.html").exists()
