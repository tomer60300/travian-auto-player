"""The published entry points must fail usefully, not with a traceback.

Pins two Codex review findings: `travian-web` on a base install (no ``web``
extra) crashed with ModuleNotFoundError, and a custom TRAVIAN_DB_PATH pointing
into a directory that does not exist yet made the web app unable to boot.
"""

import os
import subprocess
import sys

import pytest


def test_travian_web_fails_helpfully_without_the_web_extra(monkeypatch):
    import travian_api.web as web

    # Simulate the base install: the app module (and its fastapi import chain)
    # is not importable.
    monkeypatch.setitem(sys.modules, "travian_api.web.app", None)

    with pytest.raises(SystemExit) as exc:
        web.main()

    assert "travian-api[web]" in str(exc.value)


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
