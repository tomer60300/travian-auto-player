"""The credential redactor runs in the configuration the servers actually use.

Two defects, one filter.

``setup_logging`` attached ``SensitiveDataFilter`` inside ``if settings.debug:``
and ``Settings.debug`` defaults False -- so in the default configuration, the one
both servers run, the filter that exists to redact credential values was not
attached at all. Its sibling ``QueryStringRedactionFilter`` is attached
unconditionally and its docstring says so in as many words ("runs
UNCONDITIONALLY (not only in debug) so production logs are covered"), which is
the author making exactly this distinction and applying it to one of the two.

And when it was attached it inspected ``record.msg`` alone. A logger called as
``logger.warning("Auto-reconnect failed for user %s: %s", user.id, exc)`` -- the
shape at ``sessions.py:653``, ``sessions.py:519``, ``travian_auth.py:184`` and
``travian_auth.py:259`` -- has ``record.msg == "...%s: %s"``, which matches no
pattern, while ``record.args`` holds whatever the exception's ``__str__``
carried. The correct implementation sits twenty lines above it in the same file.

Redaction is surgical now rather than replacing the whole record, which is what
the class docstring always claimed ("preserving operational messages") and what
makes a redacted log still worth reading.
"""

import logging

import pytest

from travian_api.logging_config import SensitiveDataFilter, setup_logging


def _record(msg, *args):
    return logging.LogRecord("t", logging.WARNING, __file__, 1, msg, args or None, None)


def _rendered(record):
    SensitiveDataFilter().filter(record)
    return record.getMessage()


# ── The filter itself ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pattern",
    ["password=", "jwt=", "token=", "secret=", "Authorization:"],
    ids=lambda p: p.strip("=:"),
)
def test_a_credential_in_the_format_string_is_redacted(pattern):
    out = _rendered(_record(f"login failed ({pattern}hunter2swordfish)"))
    assert "hunter2swordfish" not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize(
    "pattern",
    ["password=", "jwt=", "token=", "secret=", "Authorization:"],
    ids=lambda p: p.strip("=:"),
)
def test_a_credential_in_the_interpolated_args_is_redacted(pattern):
    """The shape every real call site uses, and the one that leaked."""
    out = _rendered(_record("Auto-reconnect failed for user %s: %s", 7, f"{pattern}hunter2"))
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_a_dict_style_arg_is_redacted_too():
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "%(detail)s", None, None)
    record.args = {"detail": "token=hunter2"}
    assert "hunter2" not in _rendered(record)


def test_the_operational_message_survives_the_redaction():
    # A record reduced to "[REDACTED - Sensitive data filtered]" tells the
    # operator nothing about what failed; the class docstring always promised
    # otherwise.
    out = _rendered(_record("Auto-reconnect failed for user %s: %s", 7, "jwt=hunter2 expired"))
    assert out.startswith("Auto-reconnect failed for user 7: ")
    assert "expired" in out


def test_a_record_with_no_credential_is_left_exactly_alone():
    out = _rendered(_record("village %s has %s merchants", 20003, 4))
    assert out == "village 20003 has 4 merchants"


def test_non_string_args_are_not_disturbed():
    record = _record("%s %s", 20003, None)
    SensitiveDataFilter().filter(record)
    assert record.args == (20003, None)


# ── Where it is attached ───────────────────────────────────────────────────


def test_it_is_attached_with_debug_off(monkeypatch):
    """The default configuration -- and the one both servers run under."""
    import travian_api.logging_config as lc

    monkeypatch.setattr(lc.settings, "debug", False)
    setup_logging("INFO")

    attached = [
        f
        for handler in logging.getLogger().handlers
        for f in handler.filters
        if isinstance(f, SensitiveDataFilter)
    ]
    assert attached, "the credential redactor is not attached when debug is off"
