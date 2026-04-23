"""Regression tests for HttpClient construction, OperationGate, and persona."""

from pathlib import Path


class TestHttpClientConstruction:
    """Ensure HttpClient can be constructed without crashing."""

    def test_default_construction(self):
        """HttpClient(Settings(...)) must not crash."""
        from travian_api.clients.http_client import HttpClient
        from travian_api.config import Settings

        s = Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
        )
        client = HttpClient(s)
        assert client is not None
        assert client.base_url == "https://ts2.x1.europe.travian.com"

    def test_stealth_disabled(self):
        """HttpClient with stealth=False should construct without stealth components."""
        from travian_api.clients.http_client import HttpClient
        from travian_api.config import Settings

        s = Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
            stealth=False,
        )
        client = HttpClient(s)
        assert client is not None
        assert not client._stealth_enabled

    def test_cookie_file_set_before_stealth_init(self):
        """_cookie_file must be set before _init_stealth runs."""
        from travian_api.clients.http_client import HttpClient
        from travian_api.config import Settings

        s = Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
        )
        client = HttpClient(s)
        assert hasattr(client, "_cookie_file")
        assert isinstance(client._cookie_file, Path)

    def test_x_version_defaults_to_config(self):
        """X-Version should default to config value (no network fetch at construction)."""
        import asyncio

        from travian_api.clients.http_client import HttpClient
        from travian_api.config import Settings

        s = Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
            x_version="999",
        )
        client = HttpClient(s)
        version = asyncio.get_event_loop().run_until_complete(client._fetch_x_version())
        assert version == "999"


class TestOperationGate:
    """Test OperationGate concurrency and stop signal semantics."""

    def test_acquire_release(self):
        """Acquire grants first request, rejects duplicate op_type for same user."""
        from travian_api.web.operation_gate import OperationGate

        gate = OperationGate()
        assert gate.acquire(1, "farm") is True
        assert gate.acquire(1, "farm") is False  # duplicate rejected (mutual exclusion)
        assert gate.acquire(1, "scout") is True  # different op_type allowed
        assert sorted(gate.get_active(1)) == ["farm", "scout"]
        gate.release(1, "farm")
        assert gate.get_active(1) == ["scout"]
        assert gate.acquire(1, "farm") is True  # can re-acquire after release

    def test_stop_signal_seen_by_all_operations(self):
        """should_stop flag visible to all ops started before the signal."""
        import time as _time

        from travian_api.web.operation_gate import OperationGate

        gate = OperationGate()
        started = _time.monotonic()
        gate.acquire(1, "farm")
        gate.acquire(1, "scout")
        gate.set_should_stop(1)

        # Both operations started before the signal see it
        assert gate.check_should_stop(1, started_after=started) is True
        assert gate.check_should_stop(1, started_after=started) is True  # non-destructive

    def test_stop_signal_not_seen_by_new_operation(self):
        """Operations started AFTER the stop signal don't see it."""
        import time as _time

        from travian_api.web.operation_gate import OperationGate

        gate = OperationGate()
        gate.acquire(1, "farm")
        gate.set_should_stop(1)
        new_start = _time.monotonic()  # after the signal
        gate.acquire(1, "scout")

        assert gate.check_should_stop(1, started_after=new_start) is False

    def test_stop_signal_cleared_on_last_release(self):
        """should_stop flag clears when last operation releases."""
        import time as _time

        from travian_api.web.operation_gate import OperationGate

        gate = OperationGate()
        started = _time.monotonic()
        gate.acquire(1, "farm")
        gate.acquire(1, "scout")
        gate.set_should_stop(1)

        gate.release(1, "farm")
        assert gate.check_should_stop(1, started_after=started) is True

        gate.release(1, "scout")  # last op released
        assert gate.check_should_stop(1, started_after=started) is False

    def test_different_users_independent(self):
        """Different users don't interfere."""
        import time as _time

        from travian_api.web.operation_gate import OperationGate

        gate = OperationGate()
        started = _time.monotonic()
        gate.acquire(1, "farm")
        gate.acquire(2, "farm")
        gate.set_should_stop(1)

        assert gate.check_should_stop(1, started_after=started) is True
        assert gate.check_should_stop(2, started_after=started) is False


class TestPersona:
    """Test persona creation and coherence."""

    def test_build_persona_coherent(self):
        """Persona UA, impersonate, and headers must be internally consistent."""
        from travian_api.stealth.persona import build_persona

        persona = build_persona(server_url="https://ts2.x1.europe.travian.com")
        assert "Chrome" in persona.user_agent
        assert "Windows" in persona.user_agent
        assert persona.sec_ch_ua_platform == '"Windows"'
        assert "chrome" in persona.impersonate
        assert persona.is_chromium is True

    def test_persona_accept_language_europe(self):
        """European server should get en-GB locale."""
        from travian_api.stealth.persona import build_persona

        persona = build_persona(server_url="https://ts2.x1.europe.travian.com")
        assert "en-GB" in persona.accept_language

    def test_persona_accept_language_de(self):
        """German server should get de-DE locale."""
        from travian_api.stealth.persona import build_persona

        persona = build_persona(server_url="https://ts2.x1.de.travian.com")
        assert "de-DE" in persona.accept_language
