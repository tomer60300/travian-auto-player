"""A session directory this process cannot harden is refused, not used anyway.

The per-session cookie and JWT caches live under
``<tempdir>/travian_web_sessions/<user>/<identity>``. The tree is created with
``exist_ok=True`` and then ``os.chmod(dir, 0o700)`` -- whose ``OSError`` was
swallowed, with the comment "Windows ACL may not support chmod".

That comment is true on Windows and only there, and swallowing it everywhere
turns the one signal that matters on a multi-user POSIX host into silence. The
identity segment is ``sha256(f"{server}|{username}")[:16]``, computable by
anyone who knows the Travian username, so an attacker can pre-create the leaf
directory owned by themselves: ``exist_ok=True`` succeeds, the chmod raises
EPERM, it is ignored, and the server proceeds to write ``jwt_cache.json`` and
``cookies.json`` into a directory somebody else owns. The per-file ``chmod 600``
that follows is applied AFTER ``write_text`` has created the file, so it is a
race, not a guard.

So on POSIX the failure is fatal -- a directory we cannot make private is not a
directory to put a session token in -- and on Windows it stays best-effort with
a debug line, because there ``stat``/``chmod`` do not describe the NTFS ACL at
all (the same reason ``web/auth.py::_warn_if_world_readable`` returns early
there).

The helper lives in its own module: ``web/sessions.py`` and
``services/recon_account.py`` both need it, and a service importing a web
module for one function would invert the layering. Only the hardening step is
exercised here -- building a whole ``TravianSession`` would need a Settings and
an HttpClient, and the property under test is one function.
"""

import os

import pytest

from travian_api.session_dirs import SessionDirectoryUnsafe, harden_session_dir


def test_a_directory_it_can_harden_is_accepted(tmp_path):
    target = tmp_path / "identity"
    target.mkdir()

    harden_session_dir(target)

    if os.name != "nt":
        assert (target.stat().st_mode & 0o777) == 0o700


def test_a_chmod_refusal_is_fatal_on_posix(tmp_path, monkeypatch):
    target = tmp_path / "identity"
    target.mkdir()
    monkeypatch.setattr(os, "name", "posix")

    def refuse(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", refuse)

    with pytest.raises(SessionDirectoryUnsafe) as exc:
        harden_session_dir(target)

    assert str(target) in str(exc.value)


def test_a_chmod_refusal_is_tolerated_on_windows(tmp_path, monkeypatch):
    # There chmod says nothing about the ACL, so a failure is not evidence of
    # anything -- see web/auth.py::_warn_if_world_readable.
    target = tmp_path / "identity"
    target.mkdir()
    monkeypatch.setattr(os, "name", "nt")

    def refuse(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", refuse)

    harden_session_dir(target)  # must not raise
