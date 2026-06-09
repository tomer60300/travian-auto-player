"""Regression tests for HttpClient construction, op tracking, and persona."""

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


class TestActiveOpRegistry:
    """Active-op tracking is visibility-only — never rejects."""

    def test_register_allows_concurrent_same_label(self):
        from travian_api.web.operation_gate import ActiveOpRegistry

        reg = ActiveOpRegistry()
        reg.register(1, "farm-all")
        reg.register(1, "farm-all")  # second instance — no longer blocked
        assert reg.get_active(1) == ["farm-all"]
        reg.unregister(1, "farm-all")
        assert reg.get_active(1) == ["farm-all"]  # still one left
        reg.unregister(1, "farm-all")
        assert reg.get_active(1) == []

    def test_distinct_labels_coexist(self):
        from travian_api.web.operation_gate import ActiveOpRegistry

        reg = ActiveOpRegistry()
        reg.register(1, "farm")
        reg.register(1, "scout")
        assert sorted(reg.get_active(1)) == ["farm", "scout"]

    def test_users_isolated(self):
        from travian_api.web.operation_gate import ActiveOpRegistry

        reg = ActiveOpRegistry()
        reg.register(1, "farm")
        reg.register(2, "scout")
        assert reg.get_active(1) == ["farm"]
        assert reg.get_active(2) == ["scout"]

    def test_unregister_last_op_clears_captcha_stop(self):
        """When a user's last op ends, any lingering stop signal is cleared."""
        from travian_api.web.operation_gate import active_ops, captcha_stop

        user_id = 9999
        active_ops.register(user_id, "farm")
        captcha_stop.signal(user_id)
        # Stop timestamp is recorded.
        assert user_id in captcha_stop._ts

        active_ops.unregister(user_id, "farm")
        # Last op gone → stop signal dropped, no leak.
        assert user_id not in captcha_stop._ts


class TestCaptchaStopSignal:
    """Captcha stop signals halt in-flight ops but not fresh ones."""

    def test_signal_seen_by_ops_started_before(self):
        import time as _time

        from travian_api.web.operation_gate import CaptchaStopSignal

        sig = CaptchaStopSignal()
        started = _time.monotonic()
        _time.sleep(0.02)  # Windows monotonic resolution is ~15ms
        sig.signal(1)
        assert sig.should_stop(1, started_after=started) is True
        # Non-destructive: re-reading returns the same answer.
        assert sig.should_stop(1, started_after=started) is True

    def test_signal_not_seen_by_ops_started_after(self):
        import time as _time

        from travian_api.web.operation_gate import CaptchaStopSignal

        sig = CaptchaStopSignal()
        sig.signal(1)
        _time.sleep(0.02)
        new_start = _time.monotonic()
        assert sig.should_stop(1, started_after=new_start) is False

    def test_clear_removes_signal(self):
        import time as _time

        from travian_api.web.operation_gate import CaptchaStopSignal

        sig = CaptchaStopSignal()
        started = _time.monotonic()
        _time.sleep(0.02)
        sig.signal(1)
        sig.clear(1)
        assert sig.should_stop(1, started_after=started) is False

    def test_users_independent(self):
        import time as _time

        from travian_api.web.operation_gate import CaptchaStopSignal

        sig = CaptchaStopSignal()
        started = _time.monotonic()
        _time.sleep(0.02)
        sig.signal(1)
        assert sig.should_stop(1, started_after=started) is True
        assert sig.should_stop(2, started_after=started) is False


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

    def test_persona_has_distinct_behavioral_salt(self):
        """Each fresh persona gets a non-empty, high-entropy, distinct salt.

        The salt is the entropy source that keeps behavioral seeds (gap shape,
        warm-up routes) distinct between accounts on the same world, where
        UA/lang/server collide into a few buckets.
        """
        from travian_api.stealth.persona import build_persona

        server = "https://ts2.x1.europe.travian.com"
        salts = {build_persona(server_url=server).salt for _ in range(20)}
        assert "" not in salts
        assert len(salts) == 20  # all distinct
        assert all(len(s) >= 12 for s in salts)

    def test_persona_salt_round_trips(self, tmp_path):
        """The salt persists and reloads unchanged."""
        from travian_api.stealth.persona import build_persona, load_persona, save_persona

        server = "https://ts2.x1.europe.travian.com"
        path = tmp_path / ".travian_persona.json"
        persona = build_persona(server_url=server)
        save_persona(persona, path, server_url=server)

        loaded = load_persona(path, server_url=server)
        assert loaded is not None
        assert loaded.salt == persona.salt

    def test_persona_salt_backfilled_for_legacy_file(self, tmp_path):
        """A pre-salt persona file gets a stable salt backfilled and persisted."""
        import json
        from datetime import UTC, datetime

        from travian_api.stealth.persona import load_persona

        server = "https://ts2.x1.europe.travian.com"
        path = tmp_path / ".travian_persona.json"
        created_at = datetime.now(UTC).isoformat()
        # Legacy file: no "salt" key.
        path.write_text(
            json.dumps(
                {
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "impersonate": "chrome133a",
                    "sec_ch_ua": '"Chromium";v="133"',
                    "sec_ch_ua_platform": '"Windows"',
                    "sec_ch_ua_mobile": "?0",
                    "accept_language": "en-US,en;q=0.9",
                    "is_chromium": True,
                    "created_at": created_at,
                    "server_url": server,
                }
            )
        )

        first = load_persona(path, server_url=server)
        assert first is not None and first.salt  # backfilled
        # Persisted: a second load returns the same salt (stable across runs).
        second = load_persona(path, server_url=server)
        assert second is not None and second.salt == first.salt
        # TTL not reset: created_at preserved through the backfill re-save.
        assert json.loads(path.read_text())["created_at"] == created_at
