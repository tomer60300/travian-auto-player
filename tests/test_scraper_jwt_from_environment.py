"""The root map scraper takes its session token from the environment.

`scraper.py` used to carry a live Travian session JWT as a string literal, in a
tracked file of a public repository, wired straight into `COOKIES` at import.
The literal was redacted in c60df60, which left the module holding a redaction
notice where a token belongs -- so it would have authenticated as nobody and
failed with "JWT may be expired", which is not what went wrong.

It reads `TRAVIAN_SCRAPER_JWT` now, and refuses to run when that is unset rather
than sending a request it knows cannot work.

There is no test harness for root scripts, so this is a smoke check on the two
properties that matter: importing the module neither needs the variable nor
touches the network, and the refusal names the variable it wants. The suite's
own egress guard (`tests/conftest.py`) turns any request the import made into a
`RealNetworkBlocked` error, so "no request at import" is enforced rather than
asserted -- and `_scrub_travian_credentials` has already removed
`TRAVIAN_SCRAPER_JWT` from the environment by the time this runs, which is
exactly the unset case.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRAPER = Path(__file__).resolve().parent.parent / "scraper.py"


def _load():
    """Import scraper.py by path -- it is a root script, not part of the package."""
    spec = importlib.util.spec_from_file_location("_scraper_under_test", SCRAPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_importing_it_needs_no_token_and_makes_no_request():
    module = _load()
    assert module.SESSION.cookies.get("JWT") is None


def test_no_token_literal_survives_in_the_file():
    text = SCRAPER.read_text(encoding="utf-8")
    assert "eyJ" not in text, "a JWT literal is back in a tracked file"


def test_it_refuses_to_run_without_the_variable(monkeypatch):
    module = _load()
    monkeypatch.delenv("TRAVIAN_SCRAPER_JWT", raising=False)

    with pytest.raises(SystemExit) as exc:
        module.authenticate()

    assert "TRAVIAN_SCRAPER_JWT" in str(exc.value)
    assert module.SESSION.cookies.get("JWT") is None


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_variable_is_not_a_token(value, monkeypatch):
    module = _load()
    monkeypatch.setenv("TRAVIAN_SCRAPER_JWT", value)

    with pytest.raises(SystemExit):
        module.authenticate()


def test_the_token_reaches_the_session_when_it_is_supplied(monkeypatch):
    module = _load()
    monkeypatch.setenv("TRAVIAN_SCRAPER_JWT", "  a.stand-in.value  ")

    module.authenticate()

    assert module.SESSION.cookies.get("JWT") == "a.stand-in.value"
