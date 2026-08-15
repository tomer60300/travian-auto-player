"""Imperative build steps; package metadata lives in pyproject.toml.

Builds the React frontend into ``src/travian_api/web/static`` during packaging
so a non-editable ``pip install .[web]`` serves a real UI instead of the 503
"UI not built" fallback. Combined with the ``static/**`` package-data entry,
the built assets land in the wheel.

Degrades gracefully: if Node/npm is unavailable (or the build fails), packaging
still succeeds and the server falls back to the 503 page — the same behavior as
before. The one-click start.bat / start.sh path builds the frontend explicitly
and is unaffected.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

_FRONTEND = Path(__file__).parent / "frontend"


class BuildPyWithFrontend(build_py):
    def run(self) -> None:
        self._build_frontend()
        super().run()

    def _build_frontend(self) -> None:
        if not (_FRONTEND / "package.json").exists():
            return  # source layout without the frontend (e.g. an sdist)
        npm = shutil.which("npm")
        if npm is None:
            print(
                "warning: npm not found; packaging without the built web UI "
                "(the server will serve its 'UI not built' page).",
                file=sys.stderr,
            )
            return
        try:
            lock = _FRONTEND / "package-lock.json"
            subprocess.run(
                [npm, "ci" if lock.exists() else "install"], cwd=_FRONTEND, check=True
            )
            subprocess.run([npm, "run", "build"], cwd=_FRONTEND, check=True)
        except Exception as exc:  # noqa: BLE001 - build must not hard-fail packaging
            print(
                f"warning: frontend build failed ({exc}); packaging without the "
                f"built web UI.",
                file=sys.stderr,
            )


setup(cmdclass={"build_py": BuildPyWithFrontend})
