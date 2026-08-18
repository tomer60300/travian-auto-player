"""Client-hint coherence for the browser persona.

Two review findings are pinned here:
  * #59 — the sec-ch-ua brand list must match the selected impersonation target
    per Chrome version (the transport advertises a different GREASE token,
    version and brand order per release), not one synthesized value for all;
  * #57 — a persisted persona from an earlier revision must have its DERIVED
    client hints normalized to the current policy on load, without rotating the
    identity (UA / salt / created_at preserved), so the in-place upgrade
    population actually receives the hardening instead of keeping a stale
    fingerprint for the persona's 365-day TTL.
"""

import json
from datetime import UTC, datetime, timedelta

from travian_api.stealth.persona import (
    _SEC_CH_UA_BY_MAJOR,
    _sec_ch_ua_for,
    build_persona,
    load_persona,
)

_UA_146 = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
_SERVER = "https://ts1.x1.europe.travian.com"


class TestPerVersionClientHints:
    def test_each_pinned_target_carries_its_own_brand_list(self):
        values = [_sec_ch_ua_for(m) for m in (142, 145, 146)]
        assert len(set(values)) == 3, "one synthesized value for all targets is the bug"
        for major in (142, 145, 146):
            assert _sec_ch_ua_for(major) == _SEC_CH_UA_BY_MAJOR[major]
            assert f'v="{major}"' in _sec_ch_ua_for(major)

    def test_unknown_major_uses_current_era_grease_not_the_legacy_marker(self):
        value = _sec_ch_ua_for(999)
        assert '"Not-A.Brand";v="24"' in value
        assert 'v="99"' not in value

    def test_build_persona_carries_the_per_version_hint(self):
        ua = _UA_146.replace("146", "145")
        persona = build_persona(ua=ua)
        assert persona.sec_ch_ua == _SEC_CH_UA_BY_MAJOR[145]
        assert persona.impersonate == "chrome145"


class TestPersistedPersonaNormalization:
    def _write(self, path, ua, sec_ch_ua, *, created_at=None, salt="abcdef0123456789"):
        path.write_text(
            json.dumps(
                {
                    "user_agent": ua,
                    "impersonate": "chrome146",
                    "sec_ch_ua": sec_ch_ua,
                    "sec_ch_ua_platform": '"Windows"',
                    "sec_ch_ua_mobile": "?0",
                    "accept_language": "en-US,en;q=0.9",
                    "is_chromium": True,
                    "salt": salt,
                    "created_at": created_at or datetime.now(UTC).isoformat(),
                    "server_url": _SERVER,
                }
            )
        )

    def test_legacy_client_hint_is_normalized_on_load(self, tmp_path):
        path = tmp_path / ".travian_persona.json"
        legacy = '"Chromium";v="146", "Google Chrome";v="146", "Not-A.Brand";v="99"'
        self._write(path, _UA_146, legacy)

        persona = load_persona(path, server_url=_SERVER)

        assert persona is not None
        assert persona.sec_ch_ua == _sec_ch_ua_for(146), "the stale GREASE must be normalized"
        assert persona.sec_ch_ua != legacy
        # The normalized value is persisted: a second load returns it unchanged.
        again = load_persona(path, server_url=_SERVER)
        assert again.sec_ch_ua == persona.sec_ch_ua

    def test_normalization_preserves_identity_and_ttl(self, tmp_path):
        path = tmp_path / ".travian_persona.json"
        created = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        legacy = '"Chromium";v="146", "Google Chrome";v="146", "Not-A.Brand";v="99"'
        self._write(path, _UA_146, legacy, created_at=created, salt="stablesalt123456")

        persona = load_persona(path, server_url=_SERVER)

        assert persona.user_agent == _UA_146, "UA must not change (no identity rotation)"
        assert persona.salt == "stablesalt123456", "salt must be preserved"
        # created_at is untouched so the 365-day TTL is not reset.
        assert json.loads(path.read_text())["created_at"] == created
