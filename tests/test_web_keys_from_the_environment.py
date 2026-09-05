"""The Fernet key does not have to live in the directory it decrypts.

P1-7 from the config audit: ``KEYS_FILE = DB_DIR / ".web_keys"`` is plaintext
JSON holding both the JWT signing secret and the Fernet key, and ``DB_DIR`` is
the directory of the SQLite file whose credential rows that Fernet key
encrypts. Anything that reads ``~/.travian/`` -- a backup, a cloud-sync client,
another local user -- gets the ciphertext and its key together, so "encrypted at
rest" buys nothing against the threat that actually applies to a single-machine
deployment.

`src/travian_api/CLAUDE.md` already states the convention this violated:
"Fernet encryption keys from environment variables, never hardcoded."

The file stays, as the audit's fix says, as the fallback for every deployment
that has one. What is new is that an operator who wants the key somewhere else
has somewhere else to put it.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from travian_api.web import auth

FERNET_ENV = "TRAVIAN_FERNET_KEY"
JWT_ENV = "TRAVIAN_JWT_SECRET"


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    """Point the module at a keys file of its own, and start with none."""
    path = tmp_path / ".web_keys"
    monkeypatch.setattr(auth, "KEYS_FILE", path)
    monkeypatch.setattr(auth, "_LEGACY_KEYS_FILE", tmp_path / "legacy" / ".web_keys")
    monkeypatch.delenv(FERNET_ENV, raising=False)
    monkeypatch.delenv(JWT_ENV, raising=False)
    return path


class TestTheEnvironmentIsAskedFirst:
    def test_the_pair_comes_from_the_environment_when_it_is_set(self, keys_file, monkeypatch):
        fernet_key = Fernet.generate_key().decode()
        monkeypatch.setenv(JWT_ENV, "a-secret-from-somewhere-else")
        monkeypatch.setenv(FERNET_ENV, fernet_key)

        assert auth.get_or_create_keys() == ("a-secret-from-somewhere-else", fernet_key)

    def test_no_key_file_is_created_when_the_environment_supplies_them(
        self, keys_file, monkeypatch
    ):
        monkeypatch.setenv(JWT_ENV, "s")
        monkeypatch.setenv(FERNET_ENV, Fernet.generate_key().decode())

        auth.get_or_create_keys()

        assert not keys_file.exists()

    def test_an_existing_key_file_is_not_read_when_the_environment_wins(
        self, keys_file, monkeypatch
    ):
        keys_file.write_text(
            json.dumps({"jwt_secret": "from-the-file", "fernet_key": "unused"}),
            encoding="utf-8",
        )
        monkeypatch.setenv(JWT_ENV, "from-the-environment")
        monkeypatch.setenv(FERNET_ENV, Fernet.generate_key().decode())

        jwt_secret, _ = auth.get_or_create_keys()

        assert jwt_secret == "from-the-environment"


class TestHalfAnAnswerIsRefused:
    """Both or neither. One of the two would silently pair an environment key
    with a file key, and for the Fernet half that is every credential row."""

    def test_only_the_fernet_key_is_an_error_naming_the_other(self, keys_file, monkeypatch):
        monkeypatch.setenv(FERNET_ENV, Fernet.generate_key().decode())

        with pytest.raises(auth.WebKeysMisconfigured, match=JWT_ENV):
            auth.get_or_create_keys()

    def test_only_the_jwt_secret_is_an_error_naming_the_other(self, keys_file, monkeypatch):
        monkeypatch.setenv(JWT_ENV, "s")

        with pytest.raises(auth.WebKeysMisconfigured, match=FERNET_ENV):
            auth.get_or_create_keys()

    def test_a_blank_value_counts_as_unset(self, keys_file, monkeypatch):
        monkeypatch.setenv(JWT_ENV, "   ")
        monkeypatch.setenv(FERNET_ENV, "")

        auth.get_or_create_keys()

        assert keys_file.exists()  # fell through to the file, no error


class TestAMalformedKeyIsNamed:
    def test_the_error_names_the_variable_not_the_library(self, keys_file, monkeypatch):
        monkeypatch.setenv(JWT_ENV, "s")
        monkeypatch.setenv(FERNET_ENV, "not-a-fernet-key")

        with pytest.raises(auth.WebKeysMisconfigured, match=FERNET_ENV):
            auth.get_or_create_keys()


class TestTheFileStillWorks:
    def test_with_nothing_in_the_environment_the_file_is_created_and_reused(self, keys_file):
        first = auth.get_or_create_keys()

        assert keys_file.exists()
        assert auth.get_or_create_keys() == first
