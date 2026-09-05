"""`claim-all` must check the activity ceiling between claims.

`POST /api/video/claim-all` iterates five reward types, each ~8 requests plus
30 s of real 3-second ticks plus a `wait_before_claim_s`, and nothing checked
`check_activity_budget()` between them. A run that started just under the
ceiling spent five full video sessions past it.
"""

import asyncio
from types import SimpleNamespace

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.web.routes.video import (
    _PRODUCTION_BOOST_TYPES,
    VideoClaimAllRequest,
    claim_all_production_boosts,
)


def _session(*, allow_claims):
    """`allow_claims` claims are permitted; the ceiling refuses after that."""
    claimed: list[str] = []

    def check_activity_budget():
        if len(claimed) >= allow_claims:
            raise ActivityBudgetExhausted("Activity budget exhausted: rolling 24h limit reached")
        return True

    async def claim_reward(reward_type, **kwargs):
        claimed.append(reward_type)
        return SimpleNamespace(success=True, reward_type=reward_type, message="claimed")

    session = SimpleNamespace(
        http_client=SimpleNamespace(check_activity_budget=check_activity_budget),
        video_service=SimpleNamespace(claim_reward=claim_reward),
    )
    return session, claimed


def _run(session):
    return asyncio.run(claim_all_production_boosts(VideoClaimAllRequest(village_id=1), session))


def test_a_run_inside_the_budget_claims_every_type():
    session, claimed = _session(allow_claims=len(_PRODUCTION_BOOST_TYPES))
    body = _run(session)
    assert claimed == _PRODUCTION_BOOST_TYPES
    assert body["succeeded"] == len(_PRODUCTION_BOOST_TYPES)
    assert body["stopped_reason"] is None


def test_the_ceiling_stops_the_run_part_way():
    session, claimed = _session(allow_claims=2)
    body = _run(session)

    assert len(claimed) == 2, "the third claim must not run"
    assert body["stopped_reason"], "the stream must say why the rest did not run"
    assert "budget" in body["stopped_reason"].lower()


def test_the_types_that_never_ran_are_reported():
    session, _claimed = _session(allow_claims=2)
    body = _run(session)

    assert len(body["results"]) == len(_PRODUCTION_BOOST_TYPES), "every type is accounted for"
    not_attempted = [r for r in body["results"] if r.get("status") == "not_attempted"]
    assert [r["reward_type"] for r in not_attempted] == _PRODUCTION_BOOST_TYPES[2:]
    assert body["succeeded"] == 2
