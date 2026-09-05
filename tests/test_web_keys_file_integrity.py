"""The keys file is written atomically, and a corrupt one says what it is.

`.web_keys` holds the only copy of the Fernet key that decrypts every
``travian_credentials.encrypted_password`` row. It was written with a bare
``write_text`` -- not atomic -- and read back with a bare ``json.loads``, no
handler, at module import. Power loss during that one-time write left a
truncated file, and the next start raised ``JSONDecodeError`` before FastAPI
existed, with a traceback pointing at a JSON parse rather than at the file that
matters. Losing the file makes every stored password permanently undecryptable,
so "which file, and do not delete it" is the whole content of a useful error.

The repository already had the correct write: ``stealth/scheduler.py`` writes
through ``tempfile.mkstemp`` + ``os.replace``, unlinking the temp file if
anything raises. That is the pattern used here, so a half-written file is never
what a reader finds.

``WebKeysCorrupt`` is a named error rather than a bare parse failure, and it
carries the path and the "do not delete this file" instruction.
"""

import json

import pytest

from travian_api.web import auth as auth_module
from travian_api.web.auth import WebKeysCorrupt


@pytest.fixture
def keys_at(tmp_path, monkeypatch):
    """Point the module's key file at a temp path, leaving the real one alone."""
    path = tmp_path / ".web_keys"
    monkeypatch.setattr(auth_module, "KEYS_FILE", path)
    monkeypatch.setattr(auth_module, "_LEGACY_KEYS_FILE", tmp_path / "legacy" / ".web_keys")
    return path


def test_a_fresh_file_is_created_and_read_back(keys_at):
    jwt_secret, fernet_key = auth_module.get_or_create_keys()

    assert keys_at.exists()
    assert json.loads(keys_at.read_text(encoding="utf-8")) == {
        "jwt_secret": jwt_secret,
        "fernet_key": fernet_key,
    }
    assert auth_module.get_or_create_keys() == (jwt_secret, fernet_key), "keys must be stable"


def test_no_temp_file_is_left_behind(keys_at):
    auth_module.get_or_create_keys()
    assert [p.name for p in keys_at.parent.iterdir()] == [".web_keys"]


@pytest.mark.parametrize(
    "content",
    ['{"jwt_secret": "abc", "ferne', "", "   ", "not json at all"],
    ids=["truncated", "empty", "blank", "garbage"],
)
def test_a_corrupt_file_raises_a_named_error(keys_at, content):
    keys_at.write_text(content, encoding="utf-8")

    with pytest.raises(WebKeysCorrupt) as exc:
        auth_module.get_or_create_keys()

    message = str(exc.value)
    assert str(keys_at) in message, "the error must name the file"
    assert "delete" in message.lower(), "and must say not to delete it"


def test_a_file_missing_a_key_is_corrupt_too(keys_at):
    # Valid JSON, wrong shape: regenerating from here would orphan every
    # encrypted credential row just as surely as a truncated file.
    keys_at.write_text(json.dumps({"jwt_secret": "abc"}), encoding="utf-8")

    with pytest.raises(WebKeysCorrupt):
        auth_module.get_or_create_keys()


def test_a_failed_write_leaves_no_file_at_all(keys_at, monkeypatch):
    """Atomicity: readers see the old file or the new one, never a fragment."""

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(auth_module.json, "dump", boom)

    with pytest.raises(OSError):
        auth_module.get_or_create_keys()

    assert not keys_at.exists()
    assert list(keys_at.parent.iterdir()) == [], "the temp file must be cleaned up"
