"""The published entry points must fail usefully, not with a traceback.

Pins two Codex review findings: `travian-web` on a base install (no ``web``
extra) crashed with ModuleNotFoundError, and a custom TRAVIAN_DB_PATH pointing
into a directory that does not exist yet made the web app unable to boot.
"""

import asyncio
import os
import subprocess
import sys

import pytest


def test_travian_web_fails_helpfully_without_the_web_extra(monkeypatch):
    import travian_api.web as web

    # Simulate the base install: fastapi itself is not importable, so loading
    # the app module halts on its fastapi import.
    monkeypatch.delitem(sys.modules, "travian_api.web.app", raising=False)
    monkeypatch.setitem(sys.modules, "fastapi", None)

    with pytest.raises(SystemExit) as exc:
        web.main()

    assert "travian-api[web]" in str(exc.value)


def test_travian_web_does_not_mask_app_bugs_as_missing_extras(monkeypatch):
    """An import failure inside the app itself must surface as itself; blaming
    the packaging would hide a broken release behind install instructions."""
    import travian_api.web as web

    monkeypatch.setitem(sys.modules, "travian_api.web.app", None)

    with pytest.raises(ImportError):
        web.main()


def test_a_missing_frontend_build_explains_itself():
    """`pip install .[web]` alone ships no static assets; the server must say
    how to get a UI instead of serving a blank 404."""
    from travian_api.web.app import serve_ui_not_built

    res = asyncio.run(serve_ui_not_built(None, ""))

    assert res.status_code == 503
    assert b"npm run build" in res.body


def test_the_unbuilt_ui_handler_still_404s_api_paths():
    from travian_api.web.app import serve_ui_not_built

    res = asyncio.run(serve_ui_not_built(None, "api/travian/status"))

    assert res.status_code == 404


def test_loose_static_files_do_not_count_as_a_frontend_build(tmp_path):
    """favicon.svg alone must not trip the SPA mount: StaticFiles raises on a
    missing assets/ dir, crashing the server before the 503 fallback exists."""
    from travian_api.web.app import ui_build_exists

    (tmp_path / "favicon.svg").write_text("<svg/>")
    assert ui_build_exists(tmp_path) is False

    # index.html referencing assets that were never written = interrupted build.
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        "<html><head>"
        '<link rel="stylesheet" href="/assets/index-abc123.css">'
        "</head><body>"
        '<script type="module" src="/assets/index-abc123.js"></script>'
        "</body></html>"
    )
    assert ui_build_exists(tmp_path) is False

    # The script exists but the linked stylesheet is still missing → not ready.
    (tmp_path / "assets" / "index-abc123.js").write_text("// built")
    assert ui_build_exists(tmp_path) is False

    # Every referenced asset exists → a complete build.
    (tmp_path / "assets" / "index-abc123.css").write_text("/* built */")
    assert ui_build_exists(tmp_path) is True


def test_spa_file_serving_rejects_path_traversal():
    """`/../../.env` must not escape the build directory: only api/ and ws/
    prefixes were filtered, so any readable file on disk could be downloaded."""
    import asyncio

    from travian_api.web.app import STATIC_DIR, serve_spa

    escape = "../routes/distribution.py"
    assert (STATIC_DIR / escape).resolve().is_file(), "traversal target must exist for the test"

    response = asyncio.run(serve_spa(None, escape))

    served = getattr(response, "path", "")
    assert not str(served).endswith("distribution.py"), "traversal escaped the build directory"


def test_custom_db_path_parent_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "state" / "app.db"
    assert not target.parent.exists()

    subprocess.run(
        [sys.executable, "-c", "import travian_api.web.models.db"],
        env={**os.environ, "TRAVIAN_DB_PATH": str(target)},
        capture_output=True,
        text=True,
        check=True,
    )

    assert target.parent.is_dir()


def test_web_keys_follow_a_custom_db_path(tmp_path):
    """The Fernet/JWT keys must live next to the database they encrypt for:
    with keys pinned to ~/.travian, moving or reusing a custom-path DB on
    another setup cannot decrypt its own credential rows."""
    db_file = tmp_path / "data" / "app.db"
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    subprocess.run(
        [sys.executable, "-c", "import travian_api.web.auth"],
        env={
            **os.environ,
            "TRAVIAN_DB_PATH": str(db_file),
            "USERPROFILE": str(fake_home),
            "HOME": str(fake_home),
        },
        capture_output=True,
        text=True,
        check=True,
    )

    assert (db_file.parent / ".web_keys").is_file()


def test_legacy_keys_are_migrated_next_to_a_custom_db(tmp_path):
    """Deployments that already ran with a custom TRAVIAN_DB_PATH have their
    keys in ~/.travian; those must move with the DB, not be regenerated —
    regeneration would orphan every encrypted credential row."""
    db_file = tmp_path / "data" / "app.db"
    fake_home = tmp_path / "home"
    legacy_dir = fake_home / ".travian"
    legacy_dir.mkdir(parents=True)
    import json

    from cryptography.fernet import Fernet

    legacy_keys = legacy_dir / ".web_keys"
    legacy_keys.write_text(
        json.dumps({"jwt_secret": "legacy-jwt", "fernet_key": Fernet.generate_key().decode()})
    )

    subprocess.run(
        [sys.executable, "-c", "import travian_api.web.auth"],
        env={
            **os.environ,
            "TRAVIAN_DB_PATH": str(db_file),
            "USERPROFILE": str(fake_home),
            "HOME": str(fake_home),
        },
        capture_output=True,
        text=True,
        check=True,
    )

    migrated = db_file.parent / ".web_keys"
    assert migrated.is_file()
    assert "legacy-jwt" in migrated.read_text()


def test_init_db_backfills_columns_added_after_first_release(tmp_path):
    """create_all() never ALTERs an existing table, so upgrading a live
    travian_web.db from before label/last_connected left reconnect queries
    failing with 'no such column' until the DB was rebuilt by hand."""
    db_file = tmp_path / "travian_web.db"

    import sqlite3

    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "CREATE TABLE travian_credentials ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL, "
            "server_url VARCHAR(256) NOT NULL, "
            "travian_username VARCHAR(128) NOT NULL, "
            "encrypted_password VARCHAR(512) NOT NULL, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE recon_credentials ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "travian_username VARCHAR(128) NOT NULL, "
            "encrypted_password VARCHAR(512) NOT NULL, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from travian_api.web.models.db import init_db; asyncio.run(init_db())",
        ],
        env={**os.environ, "TRAVIAN_DB_PATH": str(db_file)},
        capture_output=True,
        text=True,
        check=True,
    )

    with sqlite3.connect(db_file) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(travian_credentials)")}
        recon_columns = {row[1] for row in conn.execute("PRAGMA table_info(recon_credentials)")}
    assert {"label", "last_connected"} <= columns
    assert "server_url" in recon_columns
