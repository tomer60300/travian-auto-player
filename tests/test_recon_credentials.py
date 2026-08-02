"""Background-account credential precedence, rotation and storage.

Credentials rotated through the UI must beat the .env fallback, must survive
into the live runtime (a cached ReconAccount pins the old password and a
30-minute sticky-failure window), and must never be persisted in plaintext.
"""

import asyncio
from unittest.mock import patch

from travian_api.services.recon_account import ReconAccountManager
from travian_api.web.auth import decrypt_credential, encrypt_credential


class _FakeSettings:
    def __init__(self, username: str = "", password: str = "") -> None:
        self.recon_username = username
        self.recon_password = password


class _FakeAccount:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_env_credentials_are_used_when_nothing_is_stored():
    mgr = ReconAccountManager()
    with patch(
        "travian_api.services.recon_account._get_settings",
        return_value=_FakeSettings("env@example.com", "envpass"),
    ):
        assert mgr.credentials() == ("env@example.com", "envpass")
        assert mgr.credentials_source() == "env"
        assert mgr.is_configured() is True
        assert mgr.get_proxy_username() == "env@example.com"


def test_stored_credentials_take_precedence_over_env():
    mgr = ReconAccountManager()
    mgr.set_credentials("rotated@example.com", "newpass")
    with patch(
        "travian_api.services.recon_account._get_settings",
        return_value=_FakeSettings("env@example.com", "envpass"),
    ):
        assert mgr.credentials() == ("rotated@example.com", "newpass")
        assert mgr.credentials_source() == "stored"
        assert mgr.get_proxy_username() == "rotated@example.com"


def test_clearing_stored_credentials_falls_back_to_env():
    mgr = ReconAccountManager()
    mgr.set_credentials("rotated@example.com", "newpass")
    mgr.clear_credentials()
    with patch(
        "travian_api.services.recon_account._get_settings",
        return_value=_FakeSettings("env@example.com", "envpass"),
    ):
        assert mgr.credentials() == ("env@example.com", "envpass")
        assert mgr.credentials_source() == "env"


def test_unconfigured_when_neither_source_has_credentials():
    mgr = ReconAccountManager()
    with patch(
        "travian_api.services.recon_account._get_settings",
        return_value=_FakeSettings(),
    ):
        assert mgr.credentials() == (None, None)
        assert mgr.credentials_source() is None
        assert mgr.is_configured() is False
        assert mgr.get_proxy_username() is None


def test_invalidate_closes_and_drops_cached_accounts():
    """Without this a rotation cannot apply: the cached account holds the old
    password and its sticky-failure window keeps suppressing retries."""
    mgr = ReconAccountManager()
    account = _FakeAccount()
    mgr._accounts["https://ts2.example.com"] = account

    asyncio.run(mgr.invalidate())

    assert account.closed is True
    assert mgr._accounts == {}


def test_credentials_are_encrypted_at_rest():
    secret = "recon-password-123"
    ciphertext = encrypt_credential(secret)

    assert secret not in ciphertext
    assert decrypt_credential(ciphertext) == secret
