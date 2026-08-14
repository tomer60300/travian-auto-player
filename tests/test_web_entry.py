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

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html></html>")
    assert ui_build_exists(tmp_path) is True


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
