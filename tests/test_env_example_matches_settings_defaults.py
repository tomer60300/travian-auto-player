"""`.env.example` must not contradict the code it documents.

The config audit (P2-1) found `.env.example` pinning
``TRAVIAN_TRADE_ROUTE_LIVE=false`` while ``config.py`` defaulted it **True** --
and the pin is not a comment: ``model_config["env_file"] = ".env"``, so an
``.env`` copied from the example *overrides* the code default. The default was
flipped on 2026-08-27 precisely because "the opt-in kept silently reverting to
preview-only on every server restart", which is exactly what a copied line does.
The remediation pass that fixed the comments claiming live writes default off
(CHANGELOG 198-203) updated three source files and missed this one.

P2-2 is the structural half: exactly one of 25 ``Settings`` fields had its
default pinned by a test, and that pin (`test_trade_route_live_flag.py`) passes
``_env_file=None`` -- deliberately, for test hygiene -- so **no test observed
the configuration the repository actually ships**. This file is that test.

The rule it enforces: a value in `.env.example` either agrees with the
corresponding ``Settings`` default, or it is one of the three per-install
identity fields the file's own header calls "required for authentication",
whose defaults are empty because there is nothing sensible to default them to.
Anything else is the example and the code disagreeing about what the app does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from travian_api.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

# Per-install identity, not configuration: `Settings` defaults these to "" so
# the app refuses to run rather than guessing, and the example must show the
# shape of a real value. These are the only three lines allowed to differ.
IDENTITY_FIELDS = frozenset({"base_url", "username", "password"})

_ASSIGNMENT = re.compile(r"^\s*(TRAVIAN_[A-Z0-9_]+)\s*=(.*)$")


def _assignments() -> dict[str, str]:
    """Every uncommented ``TRAVIAN_<NAME>=<value>`` line in the example."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return {
        match.group(1): match.group(2).strip()
        for match in (_ASSIGNMENT.match(line) for line in text.splitlines())
        if match
    }


def _field_name(env_key: str) -> str:
    return env_key.removeprefix("TRAVIAN_").lower()


def test_the_example_is_readable_and_sets_something():
    assert ENV_EXAMPLE.exists()
    assert _assignments(), "no TRAVIAN_* assignments found — the parser is wrong"


def test_every_key_in_the_example_names_a_real_settings_field():
    fields = set(Settings.model_fields)
    unknown = {key for key in _assignments() if _field_name(key) not in fields}
    assert not unknown, f"{sorted(unknown)} are not Settings fields; renamed or removed"


@pytest.mark.parametrize("env_key", sorted(_assignments()))
def test_the_example_agrees_with_the_code_default(env_key: str, monkeypatch):
    """A pinned value that disagrees with the default is a silent override."""
    field = _field_name(env_key)
    if field in IDENTITY_FIELDS:
        pytest.skip(f"{env_key} is per-install identity, not a default")

    # The suite scrubs TRAVIAN_* from the environment, but be explicit: an
    # environment variable beats the env file, and this test is about the file.
    monkeypatch.delenv(env_key, raising=False)

    from_example = getattr(Settings(_env_file=ENV_EXAMPLE), field)
    from_code = getattr(Settings(_env_file=None), field)

    assert from_example == from_code, (
        f"{ENV_EXAMPLE.name} sets {env_key}={_assignments()[env_key]!r}, which "
        f"loads as {from_example!r}, but Settings defaults {field} to "
        f"{from_code!r}. An .env copied from the example would override the "
        f"code. Either comment the line out or change the default."
    )


def test_the_identity_fields_are_the_only_ones_with_no_usable_default():
    """The exemption above is a fact about the code, not a licence to grow.

    Each exempt field must actually have an empty default — the moment one
    gets a real default, it stops being identity and rejoins the check.
    """
    defaults = Settings(_env_file=None)
    for field in IDENTITY_FIELDS:
        assert getattr(defaults, field) == "", (
            f"{field} now has a default; drop it from IDENTITY_FIELDS so the "
            "example is checked against it"
        )
