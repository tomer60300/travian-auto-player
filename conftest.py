"""Repo-root conftest: the egress guard has to cover every pytest invocation.

The guard itself lives in ``tests/conftest.py``; this module only re-exports its
hooks so they also run for collection outside ``tests/``. That matters because
``test_stealth.py``, ``test_stealth_integration.py`` and
``test_stealth_upgrade.py`` sit at the repo root and call ``asyncio.run(main())``
at module scope — importing any of them performs a real login with the
developer's ``.env`` credentials, and one of them upgrades a real building.
``testpaths = ["tests"]`` keeps them out of a plain ``pytest`` run, but
``pytest .`` or ``pytest test_stealth_upgrade.py`` collects them, and a conftest
inside ``tests/`` is never loaded for a root-level target.
"""

from tests.conftest import pytest_configure, pytest_unconfigure  # noqa: F401
