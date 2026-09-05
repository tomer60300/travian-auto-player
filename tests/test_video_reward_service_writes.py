"""`VideoRewardService.claim_reward` spends the operator's ad-view sessions and,
at the end, claims a real production/build reward from the live game. Four
`http_client.post_json` calls are its write surface: the session open
(`/api/v1/videofeature/open/{type}`), the mid-flow start notify
(`/api/v1/videofeature/start`), the reward claim (`/api/v1/videofeature/ends`
-- the one call that actually grants something), and the availability read
(`/api/v1/graphql`) `get_available_rewards` uses to decide which types are
worth attempting.

The ad-network (ATG) leg -- the iframe config, the 3s tick loop, the xs.php
signature -- is faked out entirely: `_extract_atg_config` and `_get_atg_client`
are monkeypatched, and `asyncio.sleep`/`HumanTiming.micro_jitter`/
`HumanTiming.reaction_time` are all patched to be instant, so these tests
exercise the same open -> start -> ends sequence real playback does without
any real HTTP, the real ~33s of ticking, or the reaction-time wait.
`tests/test_action_url_integrity.py` already pins the ad client's own
header/TLS identity; this file does not repeat that.

**Finding**: `/ends`'s handler read `ends_data.get("error")` straight off the
POST's answer. `post_json` hands back ``{"response_text": ...}`` for a body
that was not JSON (an HTML soft-block, a maintenance page) -- that dict has no
``"error"`` key, so the claim fell through to the success return and reported
"Reward claimed successfully!" for an answer that named no such thing. A
non-dict answer (e.g. a bare JSON array) was worse: ``.get`` raised
``AttributeError``, caught only by the outer catch-all, which reported failure
with a raw exception string rather than the deliberate "unverified" classification
this codebase gives every other write's unreadable answer
(`farm_list_service._check_mutation`, `trade_route_service.ToggleResponseUnreadable`).
Both shared one root cause -- using the answer's shape without checking it --
and are fixed together in `video_reward_service.py`.
"""

import asyncio

import pytest

from travian_api.exceptions import NetworkError
from travian_api.services.video_reward_service import VideoRewardService

from .activity_billing import billing

FC_URL = "https://atg.example/fc.php"
XS_URL = "https://atg.example/xs.php"


def _atg_config() -> dict:
    return {
        "xsign": {"fc": FC_URL, "xs": XS_URL, "xc": {"ts": 0}},
        "waterfall": [{"bid": "17606"}],
        "zone_id": "3716",
    }


class _AtgResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _AtgClient:
    """Stands in for the ad-network httpx session: tick posts + the xs.php
    signature POST. Never touched by billing -- the ad network is not the
    game, and the transport only bills `self.http_client`'s own methods."""

    def __init__(self, *, xs_signature: str = "deadbeef"):
        self.posts: list[str] = []
        self._xs_body = f"<xml><sign>{xs_signature}</sign></xml>" if xs_signature else ""

    async def post(self, url, content=b"", headers=None):
        self.posts.append(url)
        if url == XS_URL:
            return _AtgResponse(200, self._xs_body)
        return _AtgResponse(200, "")  # fc.php tick: empty body, xc left unchanged


class _Http:
    """Records every post_json/get_html call and replays a scripted answer per
    endpoint, matched by substring against the URL. An answer that is an
    ``Exception`` instance is raised instead of returned, modelling a lost
    write. Bills like the real transport via the shared `billing` helper --
    never by replacing the wrapped method, so a defect that skips billing
    cannot go unnoticed.
    """

    def __init__(self, bodies: dict):
        self._bodies = bodies
        self.calls: list[tuple[str, dict]] = []
        self.bills: list[float] = []
        self.post_json = billing(self.bills)(self._post_json)
        self.get_html = billing(self.bills)(self._get_html)

    def _answer(self, url: str):
        for key, answer in self._bodies.items():
            if key in url:
                return answer
        raise AssertionError(f"no scripted answer for {url}")

    async def _post_json(self, url, data=None, **kwargs):
        self.calls.append((url, dict(data or {})))
        answer = self._answer(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def _get_html(self, url, **kwargs):
        self.calls.append((url, {}))
        return ""


def _service(bodies: dict, *, atg: _AtgClient | None = None):
    http = _Http(bodies)
    svc = VideoRewardService(http_client=http)
    svc._test_atg_client = atg if atg is not None else _AtgClient()
    return svc, http


@pytest.fixture(autouse=True)
def _fake_atg_and_no_real_pauses(monkeypatch):
    async def _fake_extract(self, iframe_url):
        return _atg_config()

    async def _fake_get_client(self):
        return self._test_atg_client

    async def _instant_sleep(_seconds):
        return None

    monkeypatch.setattr(VideoRewardService, "_extract_atg_config", _fake_extract)
    monkeypatch.setattr(VideoRewardService, "_get_atg_client", _fake_get_client)
    monkeypatch.setattr("travian_api.services.video_reward_service.asyncio.sleep", _instant_sleep)
    for name in ("micro_jitter", "reaction_time"):
        monkeypatch.setattr(
            f"travian_api.stealth.timing.HumanTiming.{name}", staticmethod(lambda *a, **k: 0)
        )


# ── Call site 1: POST /api/v1/videofeature/open/{type} ────────────────────


def test_a_production_boost_claim_opens_with_its_resource_param():
    svc, http = _service(
        {
            "videofeature/open": {"vrid": "v1", "videoIframeUrl": "//x.ad/iframe"},
            "videofeature/start": {},
            "videofeature/ends": {},
        }
    )

    result = asyncio.run(svc.claim_reward("ironProductionBonus"))

    assert http.calls[0] == ("/api/v1/videofeature/open/productionBoost", {"resource": "iron"})
    assert result.success is True


def test_a_building_upgrade_claim_opens_with_village_slot_and_building():
    svc, http = _service(
        {
            "videofeature/open": {"vrid": "v2", "videoIframeUrl": "//x.ad/iframe"},
            "videofeature/start": {},
            "videofeature/ends": {},
        }
    )

    asyncio.run(svc.claim_reward("buildingUpgrade", villageId=1, slotId=2, buildingId=3))

    assert http.calls[0] == (
        "/api/v1/videofeature/open/buildingUpgrade",
        {"villageId": 1, "slotId": 2, "buildingId": 3},
    )


def test_an_unknown_reward_type_is_refused_before_any_request():
    svc, http = _service({})

    result = asyncio.run(svc.claim_reward("not_a_real_type"))

    assert result.success is False
    assert http.calls == []


def test_a_building_upgrade_missing_its_params_is_refused_before_any_request():
    svc, http = _service({})

    result = asyncio.run(svc.claim_reward("buildingUpgrade"))

    assert result.success is False
    assert "requires villageId" in result.message
    assert http.calls == []


def test_open_is_a_refusal_when_the_game_names_an_error():
    svc, http = _service({"videofeature/open": {"error": "errorAlreadyClaimed"}})

    result = asyncio.run(svc.claim_reward("ironProductionBonus"))

    assert result.success is False
    assert "errorAlreadyClaimed" in result.message
    assert [c[0] for c in http.calls] == ["/api/v1/videofeature/open/productionBoost"]


def test_an_unreadable_open_answer_is_reported_as_a_failure_not_a_crash():
    """Open grants nothing by itself, so a soft-block here is honestly just a
    failure -- unlike `/ends`, there is no reward to falsely claim."""
    svc, http = _service({"videofeature/open": {"response_text": "<html>blocked</html>"}})

    result = asyncio.run(svc.claim_reward("ironProductionBonus"))

    assert result.success is False
    assert "Open failed" in result.message
    assert len(http.calls) == 1, "an unreadable open must not fall through to start/ends"


def test_a_lost_open_answer_is_reported_as_ambiguous_with_no_further_calls():
    svc, http = _service(
        {"videofeature/open": NetworkError("Connection reset (non-retryable): peer closed")}
    )

    result = asyncio.run(svc.claim_reward("ironProductionBonus"))

    assert result.success is False
    assert "non-retryable" in result.message
    assert len(http.calls) == 1
    assert len(http.bills) == 1, "the failed request must still be billed"


# ── Call site 2: POST /api/v1/videofeature/start ──────────────────────────


def test_the_start_notify_carries_only_the_vrid():
    svc, http = _service(
        {
            "videofeature/open": {"vrid": "vrid-42", "videoIframeUrl": "//x.ad/iframe"},
            "videofeature/start": {},
            "videofeature/ends": {},
        }
    )

    asyncio.run(svc.claim_reward("ironProductionBonus"))

    assert http.calls[1] == ("/api/v1/videofeature/start", {"vrid": "vrid-42"})


def test_a_refusal_shaped_start_answer_does_not_stop_the_flow():
    """Documents current behaviour: `/start`'s answer is never inspected, so
    the flow still proceeds to `/ends` even if `/start` names an error."""
    svc, http = _service(
        {
            "videofeature/open": {"vrid": "v1", "videoIframeUrl": "//x.ad/iframe"},
            "videofeature/start": {"error": "sessionExpired"},
            "videofeature/ends": {},
        }
    )

    result = asyncio.run(svc.claim_reward("ironProductionBonus"))

    assert result.success is True
    assert [c[0] for c in http.calls] == [
        "/api/v1/videofeature/open/productionBoost",
        "/api/v1/videofeature/start",
        "/api/v1/videofeature/ends",
    ]


def test_a_lost_start_answer_is_reported_as_ambiguous_and_ends_is_never_called():
    svc, http = _service(
        {
            "videofeature/open": {"vrid": "v1", "videoIframeUrl": "//x.ad/iframe"},
            "videofeature/start": NetworkError("Connection reset (non-retryable): peer closed"),
        }
    )

    result = asyncio.run(svc.claim_reward("ironProductionBonus"))

    assert result.success is False
    assert "non-retryable" in result.message
    assert [c[0] for c in http.calls] == [
        "/api/v1/videofeature/open/productionBoost",
        "/api/v1/videofeature/start",
    ]
    assert len(http.bills) == 2


# ── Call site 3: POST /api/v1/videofeature/ends (the actual claim) ────────


def _through_ends(ends_body, *, reward_type="ironProductionBonus", **extra):
    svc, http = _service(
        {
            "videofeature/open": {"vrid": "v1", "videoIframeUrl": "//x.ad/iframe"},
            "videofeature/start": {},
            "videofeature/ends": ends_body,
        }
    )
    return asyncio.run(svc.claim_reward(reward_type, **extra)), http


def test_the_claim_post_carries_the_vrid_and_the_parsed_signature():
    result, http = _through_ends({})

    assert http.calls[2] == ("/api/v1/videofeature/ends", {"vrid": "v1", "hash": "deadbeef"})
    assert result.success is True
    assert "Reward claimed" in result.message


def test_a_building_upgrade_claim_follows_its_redirect_to_start_the_build():
    result, http = _through_ends(
        {"redirectTo": "/build.php?id=1&a=1"},
        reward_type="buildingUpgrade",
        villageId=1,
        slotId=2,
        buildingId=3,
    )

    assert result.success is True
    assert http.calls[-1][0] == "/build.php?id=1&a=1"


def test_a_refused_claim_is_reported_as_a_failure_with_the_games_reason():
    result, _http = _through_ends({"error": "errorAlreadyClaimed", "message": "already used"})

    assert result.success is False
    assert "errorAlreadyClaimed" in result.message
    assert "already used" in result.message


def test_an_html_soft_block_after_the_claim_is_not_read_as_a_silent_success():
    """FINDING: this used to report 'Reward claimed successfully!'"""
    result, _http = _through_ends({"response_text": "<html>we are down for maintenance</html>"})

    assert result.success is False, "an unreadable claim answer must not read as granted"
    assert "may already have taken effect" in result.message


def test_a_genuinely_empty_claim_answer_is_not_read_as_a_silent_success():
    """FINDING: same defect, the empty-body variant of the response_text wrapper."""
    result, _http = _through_ends({"response_text": ""})

    assert result.success is False
    assert "may already have taken effect" in result.message


def test_a_non_object_claim_answer_is_reported_as_unverified_not_a_crash():
    """A bare JSON array used to raise AttributeError inside the handler,
    caught only by the outer catch-all -- reported failure, but via a raw
    exception string instead of the deliberate unverified classification."""
    result, _http = _through_ends([])

    assert result.success is False
    assert "may already have taken effect" in result.message


def test_a_lost_claim_answer_is_not_re_sent_and_stays_ambiguous():
    svc, http = _service(
        {
            "videofeature/open": {"vrid": "v1", "videoIframeUrl": "//x.ad/iframe"},
            "videofeature/start": {},
            "videofeature/ends": NetworkError("Connection reset (non-retryable): peer closed"),
        }
    )

    result = asyncio.run(svc.claim_reward("ironProductionBonus"))

    assert result.success is False
    assert "non-retryable" in result.message
    assert [c[0] for c in http.calls] == [
        "/api/v1/videofeature/open/productionBoost",
        "/api/v1/videofeature/start",
        "/api/v1/videofeature/ends",
    ], "the claim must be attempted exactly once, never re-sent"
    assert len(http.bills) == 3


def test_a_dispatched_claim_bills_once_per_post_json_call():
    _result, http = _through_ends({})

    assert len(http.calls) == 3
    assert len(http.bills) == 3


# ── Call site 4: POST /api/v1/graphql (get_available_rewards) ─────────────


def test_get_available_rewards_parses_the_graphql_response_per_resource():
    resp = {
        "data": {
            "ownPlayer": {
                "productionBoost": {
                    "lumber": {"videoFeatureAvailable": True, "isActive": False},
                    "clay": {"videoFeatureAvailable": False, "isActive": False},
                    "iron": {"videoFeatureAvailable": True, "isActive": True},
                    "crop": {"videoFeatureAvailable": False, "isActive": False},
                }
            }
        }
    }
    svc, http = _service({"graphql": resp})

    result = asyncio.run(svc.get_available_rewards())

    assert result["lumberProductionBonus"] is True
    assert result["clayProductionBonus"] is False
    assert result["ironProductionBonus"] is True
    assert result.get("iron_active") is True
    assert "clay_active" not in result, "isActive is only reported when true"
    assert http.calls[0][0] == "/api/v1/graphql"
    assert "productionBoost" in http.calls[0][1]["query"]
    assert len(http.bills) == 1


def test_a_failed_availability_read_reports_nothing_available_not_unlimited():
    """The one guard this read backs (which types are worth claiming) must
    never default to "everything is available" just because the read failed
    -- the same defect class the scout preflight guard was fixed for."""
    svc, http = _service({"graphql": NetworkError("Connection reset (non-retryable): peer closed")})

    result = asyncio.run(svc.get_available_rewards())

    assert result == {}
    assert len(http.bills) == 1, "the failed read must still be billed"
