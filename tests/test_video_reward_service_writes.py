"""`VideoRewardService.claim_reward` spends the operator's daily ad views and,
at the end, claims a real production/build reward from the live game.

Every request it makes is pinned here against a capture: two ads watched end to
end on 2026-09-15, request and response bodies included. The flow is five calls
and they are all on Travian's own host::

    GET  /api/v1/videofeature/open/<type>    -> vrid, identifier, gameId
    GET  /fallback/v1/request-ad?game_id=    -> token
    … the video …
    POST /fallback/v1/reward                 -> conversionId + signature
    POST /api/v1/videofeature/ends           -> the reward

This file used to test a different protocol entirely: a POSTed open carrying
villageId/slotId/buildingId, a `/videofeature/start` notify, an ATG iframe, a
3-second tick loop against `fc.php`, and a signature parsed out of `xs.php`'s
XML. None of that is in the game any more, and a test suite that faked the ad
network with `_AtgClient` was green the whole time it was describing a provider
Travian had stopped using -- which is the specific way a mocked integration
test fails: confidently, and about nothing.

So the fake here answers by URL and the assertions quote the capture. What it
cannot check is the one thing no capture gave us -- how a *production boost*
names its resource -- and that is marked rather than invented.

**Findings this file also carries**, from the version before the rewrite, all
still live because the `/ends` handling survived it:

`/ends`'s handler read `ends_data.get("error")` straight off the POST's answer.
`post_json` hands back ``{"response_text": ...}`` for a body that was not JSON
(an HTML soft-block, a maintenance page) -- that dict has no ``"error"`` key, so
the claim fell through to the success return and reported "Reward claimed
successfully!" for an answer that named no such thing. A non-dict answer (e.g. a
bare JSON array) was worse: ``.get`` raised ``AttributeError``, caught only by
the outer catch-all, which reported failure with a raw exception string rather
than the deliberate "unverified" classification this codebase gives every other
write's unreadable answer (`farm_list_service._check_mutation`,
`trade_route_service.ToggleResponseUnreadable`).
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.exceptions import NetworkError
from travian_api.services.video_reward_service import VideoRewardService

from .activity_billing import billing

BASE = "https://example.invalid"

# The `open` answer, field for field as the live game sent it.
OPENED = {
    "vrid": "ipqmKd3wFEdw2uh4tKYOx5Vqlb6v1cG0",
    "hash": None,
    "identifier": "75f8d800-2924-11f1-6502-010000000f1a",
    "gameId": "09eed1e1-3feb-4cc8-a734-e8c315d311e4",
    "suffix": "",
}

CONVERSION_ID = "724355b8-d589-45d3-9f63-7ae575f6c8db"
SIGNATURE = "f15843e619164f612971fad8799c5d9671388a7a"

AD = {
    "status": "success",
    "data": {
        "video_src_url": "https://cdn.traviangames.com/Hyperdrome_EN.mp4",
        "tracking_url": "https://games.traviangames.com/115271234591123/38",
        "token": "d471db71449dd56507476cb4d45cb11eecb75ad75b7e2a3e92eead867764b83e",
    },
}

GRANTED = {
    "status": "success",
    "data": {
        "status": "success",
        "rewarded": True,
        "externalIdentifier": OPENED["identifier"],
        "currency": 0,
        "conversionId": CONVERSION_ID,
        "signature": SIGNATURE,
        "custom_1": OPENED["vrid"],
    },
}


def _happy(**overrides) -> dict:
    """The scripted answers for a claim that goes all the way through."""
    bodies = {
        "videofeature/open": OPENED,
        "request-ad": AD,
        "fallback/v1/reward": GRANTED,
        "videofeature/ends": {"token": "s60Vn0D4R7PLoZlr"},
    }
    bodies.update(overrides)
    return bodies


class _Http:
    """Records every call and replays a scripted answer per endpoint, matched by
    substring against the URL. An answer that is an ``Exception`` instance is
    raised instead of returned, modelling a lost write. Bills like the real
    transport via the shared `billing` helper -- never by replacing the wrapped
    method, so a defect that skips billing cannot go unnoticed.
    """

    def __init__(self, bodies: dict):
        self._bodies = bodies
        self.calls: list[tuple[str, dict]] = []
        self.referers: list[tuple[str, str | None]] = []
        self.bills: list[float] = []
        self.base_url = BASE
        self.browser_headers = SimpleNamespace(last_page_path="/dorf1.php")
        self.navigator = SimpleNamespace(enabled=False)
        self.post_json = billing(self.bills)(self._post_json)
        self.get_json = billing(self.bills)(self._get_json)
        self.get_html = billing(self.bills)(self._get_html)

    def _answer(self, url: str):
        for key, answer in self._bodies.items():
            if key in url:
                return answer
        raise AssertionError(f"no scripted answer for {url}")

    def _record(self, url, data, kwargs):
        self.calls.append((url, dict(data or {})))
        self.referers.append((url, kwargs.get("referer")))
        answer = self._answer(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def _post_json(self, url, data=None, **kwargs):
        return self._record(url, data, kwargs)

    async def _get_json(self, url, **kwargs):
        return self._record(url, None, kwargs)

    async def _get_html(self, url, **kwargs):
        self.calls.append((url, {}))
        self.referers.append((url, kwargs.get("referer")))
        return ""


def _service(bodies: dict):
    http = _Http(bodies)
    return VideoRewardService(http_client=http), http


@pytest.fixture(autouse=True)
def _no_real_pauses(monkeypatch):
    """The claim waits out a real ~31-second ad. Not in a unit test it doesn't."""

    async def _instant_sleep(_seconds):
        return None

    monkeypatch.setattr("travian_api.services.video_reward_service.asyncio.sleep", _instant_sleep)


def _paths(http) -> list[str]:
    return [c[0] for c in http.calls]


def _body(http, needle: str) -> dict:
    return next(body for url, body in http.calls if needle in url)


# ── The shape of a whole claim ────────────────────────────────────────────


class TestTheClaimIsTheFlowTheGameUses:
    def test_it_is_five_requests_in_the_recorded_order(self):
        svc, http = _service(_happy())

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is True
        assert _paths(http) == [
            "/hero/adventures",
            "/api/v1/videofeature/open/adventureDuration",
            f"/fallback/v1/request-ad?game_id={OPENED['gameId']}",
            "/fallback/v1/reward",
            "/api/v1/videofeature/ends",
        ]

    def test_the_open_carries_no_body(self):
        """The real GET has no body and no query. The target comes from the
        session and the Referer, which is why standing on the right page is a
        correctness step and not decoration."""
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))

        assert _body(http, "videofeature/open") == {}

    def test_there_is_no_start_notify(self):
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))

        assert not any("videofeature/start" in p for p in _paths(http))

    def test_nothing_leaves_travians_host(self):
        """The ad network used to be a second HTTP client with its own cookie
        jar and its own impersonation target to keep coherent. It is Travian's
        own /fallback/v1/ now, so there is one client and one identity."""
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))

        assert all(p.startswith("/") for p in _paths(http))

    def test_every_request_is_billed(self):
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))

        assert len(http.bills) == len(http.calls) == 5


class TestTheAdReceiptBecomesTheClaimHash:
    """conversionId + signature, concatenated, no separator. Both watches."""

    def test_the_reward_post_carries_the_token_identifier_and_vrid(self):
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))

        assert _body(http, "fallback/v1/reward") == {
            "token": AD["data"]["token"],
            "external_identifier": OPENED["identifier"],
            "custom_1": OPENED["vrid"],
        }

    def test_the_hash_is_the_two_halves_of_the_receipt(self):
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))

        assert _body(http, "videofeature/ends") == {
            "vrid": OPENED["vrid"],
            "hash": CONVERSION_ID + SIGNATURE,
        }

    def test_the_hash_is_not_transformed(self):
        """It is not hashed, signed, or re-encoded -- the previous
        implementation went looking for a <sign> element to parse."""
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))
        sent = _body(http, "videofeature/ends")["hash"]

        assert sent.startswith(CONVERSION_ID) and sent.endswith(SIGNATURE)
        assert len(sent) == len(CONVERSION_ID) + len(SIGNATURE)

    def test_an_ad_the_network_did_not_reward_stops_before_the_claim(self):
        ungranted = dict(GRANTED["data"], rewarded=False)
        svc, http = _service(_happy(**{"fallback/v1/reward": {"data": ungranted}}))

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert not any("videofeature/ends" in p for p in _paths(http))

    def test_a_receipt_missing_its_signature_stops_before_the_claim(self):
        half = {k: v for k, v in GRANTED["data"].items() if k != "signature"}
        svc, http = _service(_happy(**{"fallback/v1/reward": {"data": half}}))

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert not any("videofeature/ends" in p for p in _paths(http))


class TestTheRequestComesFromThePageThatOffersIt:
    def test_an_adventure_claim_stands_on_the_adventure_page(self):
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))

        assert _paths(http)[0] == "/hero/adventures"
        assert dict(http.referers)["/api/v1/videofeature/open/adventureDuration"] == (
            f"{BASE}/hero/adventures"
        )

    def test_a_building_claim_stands_on_that_buildings_page(self):
        """`/build.php?id=22&gid=22` in the capture. A claim made from the wrong
        page is not a failed claim -- it is a claim against whatever the session
        was last looking at, and the daily video is spent either way.

        With the navigator off (this fake's default) the page is loaded
        directly; with it on, the navigator walks dorf2 -> the same URL, and the
        gid is handed to it so there is no second load afterwards.
        """
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("buildingUpgrade", villageId=7, slotId=22, buildingId=22))

        assert _paths(http)[0] == "/build.php?id=22&gid=22&newdid=7"

    def test_the_navigator_is_given_the_gid_so_it_lands_in_one_hop(self):
        seen = {}

        async def _nav(slot, village=None, gid=None):
            seen.update(slot=slot, village=village, gid=gid)

        svc, http = _service(_happy())
        http.navigator = SimpleNamespace(enabled=True, navigate_to_building=_nav)

        asyncio.run(svc.claim_reward("buildingUpgrade", villageId=7, slotId=22, buildingId=22))

        assert seen == {"slot": 22, "village": 7, "gid": 22}
        assert not any(p.startswith("/build.php") for p in _paths(http)), (
            "the navigator landed on the page; loading it again would be a hop "
            "the capture does not contain"
        )

    def test_the_village_selector_is_still_last(self):
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("buildingUpgrade", villageId=7, slotId=22, buildingId=22))

        assert _paths(http)[0].endswith("&newdid=7")

    def test_the_ad_requests_are_referred_from_the_site_root(self):
        """They are the ad frame asking, and the frame's document is the root."""
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))
        referers = dict(http.referers)

        assert referers[f"/fallback/v1/request-ad?game_id={OPENED['gameId']}"] == f"{BASE}/"
        assert referers["/fallback/v1/reward"] == f"{BASE}/"

    def test_the_claim_is_referred_from_the_offering_page(self):
        svc, http = _service(_happy())
        asyncio.run(svc.claim_reward("adventureDuration"))

        assert dict(http.referers)["/api/v1/videofeature/ends"] == f"{BASE}/hero/adventures"

    def test_a_building_claim_without_its_slot_is_refused_before_any_request(self):
        svc, http = _service(_happy())

        result = asyncio.run(svc.claim_reward("buildingUpgrade"))

        assert result.success is False
        assert http.calls == []


class TestTheWaitIsTheVideo:
    """Timed from the ad request to the reward POST: 33.6s and 23.2s."""

    def test_it_is_never_shorter_than_the_shorter_observation(self):
        assert min(VideoRewardService._watch_seconds() for _ in range(2000)) >= 24.0

    def test_it_is_not_a_constant(self):
        draws = {round(VideoRewardService._watch_seconds(), 3) for _ in range(200)}

        assert len(draws) > 100, "a flow that always takes exactly N seconds is its own signal"

    def test_its_body_sits_around_the_observations(self):
        import statistics

        draws = [VideoRewardService._watch_seconds() for _ in range(4000)]

        assert 28.0 < statistics.median(draws) < 36.0
        assert max(draws) <= 75.0


# ── Failures along the way ────────────────────────────────────────────────


class TestAFailedStepStopsTheFlow:
    def test_an_unknown_reward_type_is_refused_before_any_request(self):
        svc, http = _service({})

        result = asyncio.run(svc.claim_reward("not_a_real_type"))

        assert result.success is False
        assert http.calls == []

    def test_an_open_that_hands_back_no_session_stops_there(self):
        svc, http = _service(_happy(**{"videofeature/open": {"error": "errorAlreadyClaimed"}}))

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert "errorAlreadyClaimed" in result.message
        assert not any("request-ad" in p for p in _paths(http))

    def test_an_unreadable_open_answer_is_a_failure_not_a_crash(self):
        svc, http = _service(
            _happy(**{"videofeature/open": {"response_text": "<html>blocked</html>"}})
        )

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert not any("request-ad" in p for p in _paths(http))

    def test_a_lost_open_is_reported_as_ambiguous_and_still_billed(self):
        svc, http = _service(
            _happy(
                **{
                    "videofeature/open": NetworkError(
                        "Connection reset (non-retryable): peer closed"
                    )
                }
            )
        )

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert "non-retryable" in result.message
        assert len(http.bills) == 2, "the page load and the failed open both went out"

    def test_no_ad_on_offer_stops_before_the_wait(self):
        svc, http = _service(_happy(**{"request-ad": {"status": "success", "data": {}}}))

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert not any("reward" in p for p in _paths(http))


class TestTheClaimsAnswerIsNeverReadOptimistically:
    def test_a_refused_claim_reports_the_games_reason(self):
        svc, _ = _service(
            _happy(**{"videofeature/ends": {"error": "errorNoVideoAvailable", "message": "nope"}})
        )

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert "errorNoVideoAvailable" in result.message

    def test_an_html_soft_block_is_not_read_as_a_silent_success(self):
        svc, _ = _service(
            _happy(**{"videofeature/ends": {"response_text": "<html>maintenance</html>"}})
        )

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert "cannot be read" in result.message

    def test_a_non_object_answer_is_unverified_not_a_crash(self):
        svc, _ = _service(_happy(**{"videofeature/ends": ["unexpected"]}))

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert "cannot be read" in result.message

    def test_a_lost_claim_is_not_re_sent(self):
        svc, http = _service(
            _happy(
                **{
                    "videofeature/ends": NetworkError(
                        "Connection reset (non-retryable): peer closed"
                    )
                }
            )
        )

        result = asyncio.run(svc.claim_reward("adventureDuration"))

        assert result.success is False
        assert "non-retryable" in result.message
        assert _paths(http).count("/api/v1/videofeature/ends") == 1

    def test_a_building_claim_follows_a_redirect_when_the_game_sends_one(self):
        svc, http = _service(_happy(**{"videofeature/ends": {"redirectTo": "/dorf2.php?id=1&a=1"}}))
        http._bodies["/dorf2.php"] = ""

        result = asyncio.run(svc.claim_reward("buildingUpgrade", slotId=22, buildingId=22))

        assert result.success is True
        assert _paths(http)[-1] == "/dorf2.php?id=1&a=1"


# ── Availability ──────────────────────────────────────────────────────────


def test_get_available_rewards_parses_the_graphql_response_per_resource():
    svc, http = _service(
        {
            "graphql": {
                "data": {
                    "ownPlayer": {
                        "productionBoost": {
                            "lumber": {"videoFeatureAvailable": True, "isActive": False},
                            "clay": {"videoFeatureAvailable": False, "isActive": True},
                            "iron": {"videoFeatureAvailable": True, "isActive": False},
                            "crop": {"videoFeatureAvailable": False, "isActive": False},
                        }
                    }
                }
            }
        }
    )

    result = asyncio.run(svc.get_available_rewards())

    assert result["lumberProductionBonus"] is True
    assert result["clayProductionBonus"] is False
    assert result["clay_active"] is True
    assert http.calls[0][0] == "/api/v1/graphql"


def test_a_failed_availability_read_reports_nothing_available_not_unlimited():
    svc, _ = _service({"graphql": NetworkError("boom")})

    assert asyncio.run(svc.get_available_rewards()) == {}


def test_the_production_boost_resource_parameter_is_the_unverified_piece():
    """Marked rather than invented.

    Both recorded opens were `buildingUpgrade` and `adventureDuration`, neither
    of which names a resource. How a production boost says which resource it
    wants has not been observed, so the query-string form carried over from the
    previous implementation is a guess -- the only one left in this flow, and
    labelled as one in the service.
    """
    svc, http = _service(_happy())

    asyncio.run(svc.claim_reward("ironProductionBonus"))

    # Positionally first, not second: a production boost has no known offer
    # page, so nothing is loaded before the open -- which is the other half of
    # what is unverified here. Where a player clicks this from was not captured
    # either, so the flow declines to invent a page rather than guess one.
    assert _paths(http)[0] == "/api/v1/videofeature/open/productionBoost?resource=iron"
