"""The operator's own merchant budget: a cap on how many may be BUSY, per village.

Profile section 5 gives the capital one number -- "maximum 8 busy merchants at
any instant, underway and returning both, and the relay leg counts inside the
8". Section 8 gives the arithmetic that number is measured in: a route's
merchant pool is ``merchants_per_send x sets_in_flight``, which is exactly
``Route.merchants_committed``. So the cap needs no new maths; it needs somewhere
to be said.

The lever that existed was account-wide. ``merchant_reserve`` holds N merchants
idle at EVERY village, so getting the capital down to 8 meant a reserve of 12 --
which caps every other village at 8 as well and takes the plan apart. And a
reserve is not a cap: village 26 fields 19 merchants, so a reserve of 12 leaves
it 7 where the cap says 8. The two numbers only coincide on a full 20.

``max_busy_merchants=None`` is every account that exists today and must plan
byte-for-byte as it does now -- pinned here by comparison and in
tests/test_distribution_golden.py by recorded output.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.optimizer import VillageState, merchant_ceiling_clause
from travian_api.web.auth import get_current_user
from travian_api.web.routes.distribution import (
    DayCheckRequest,
    ExecuteRequest,
    NightProfileRequest,
    PlanRequest,
    VillageConfig,
    _explain_over_budget,
    post_day_check,
    post_execute,
    post_night_profile,
    post_plan,
)

USER = SimpleNamespace(id=1)


@pytest.fixture(scope="module")
def client():
    """The real ASGI app with only the auth dependency replaced.

    A refusal is only worth having if it reaches the page, and a 422's SHAPE
    only exists on the wire -- in-process the validator raises instead.
    """
    from travian_api.web.app import app

    app.dependency_overrides[get_current_user] = lambda: USER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


HUB = 20002
NEAR = 20003
FAR = 20005


def _village(vid, name, x, y, *, lumber=0, clay=0, iron=0, crop=0, merchants=20, cap=400_000):
    return {
        "village_id": vid,
        "name": name,
        "x": x,
        "y": y,
        "merchants_total": merchants,
        "merchants_free": merchants,
        "lumber_per_hour": lumber,
        "clay_per_hour": clay,
        "iron_per_hour": iron,
        "crop_per_hour": crop,
        "lumber_stock": cap // 4,
        "clay_stock": cap // 4,
        "iron_stock": cap // 4,
        "crop_stock": cap // 4,
        "warehouse_capacity": cap,
        "granary_capacity": cap,
    }


def _payload(*, caps=None, snapshot=None, allocations=None, **kw):
    """One hub straining its merchants, one neighbour on an easy haul.

    The hub ships 20,000 lumber/h ten fields, which section 8's arithmetic
    prices at 8 per send x 2 sets in flight = 16 -- inside a full fleet's 18 and
    twice the operator's 8. The neighbour ships clay a shorter distance for 4,
    and exists so "the cap moved ONE village's budget" is a statement about the
    other villages too.
    """
    caps = caps or {}
    payload = {
        "snapshot": snapshot
        if snapshot is not None
        else [
            _village(HUB, "02", 0, 0, lumber=20_000),
            _village(NEAR, "03", 2, 0, clay=3_000),
            _village(FAR, "05", 10, 0),
        ],
        "config": [
            {"village_id": vid, **({"max_busy_merchants": caps[vid]} if vid in caps else {})}
            for vid in (HUB, NEAR, FAR)
        ],
        "allocations": allocations
        if allocations is not None
        else {
            "lumber": {
                str(HUB): {"mode": "absolute", "value": 0},
                str(FAR): {"mode": "remainder"},
            },
            "clay": {
                str(NEAR): {"mode": "absolute", "value": 0},
                str(FAR): {"mode": "remainder"},
            },
        },
        "foreign_targets": [],
        "speed_fields_per_hour": 12.0,
    }
    payload.update(kw)
    return payload


def _plan(**kw):
    return asyncio.run(post_plan(PlanRequest.model_validate(_payload(**kw)), USER))


def _budget(res, vid):
    return next(b for b in res.budgets if b.village_id == vid)


# A long haul the latency pass wants to buy speed on. The hub ships 3,000
# lumber/h thirty fields, which is cheapest as a 3h cycle on 8 merchants; left
# to itself the plan spends idle merchants down to a 1h cycle on 10. That makes
# the cap's effect visible as the plan CHANGING rather than as a refusal, which
# is what the other three planning paths can be read for.
def _long_haul(*, cap=None):
    return _payload(
        caps={HUB: cap} if cap is not None else {},
        snapshot=[
            _village(HUB, "02", 0, 0, lumber=3_000),
            _village(FAR, "05", 30, 0),
        ],
        allocations={
            "lumber": {
                str(HUB): {"mode": "absolute", "value": 0},
                str(FAR): {"mode": "remainder"},
            }
        },
        config=[
            {"village_id": HUB, **({"max_busy_merchants": cap} if cap is not None else {})},
            {"village_id": FAR},
        ],
    )


class TestTheBudgetIsTheTighterOfTheFleetAndTheCap:
    """``VillageState`` decides it, so every reader gets the same answer.

    Threaded onto the village rather than handed round as a side map, the way
    ``trade_office_level``, ``role`` and ``may_relay`` travel: a second source
    for one village's budget is a second answer waiting to disagree.
    """

    def test_no_cap_is_the_fleet_less_the_reserve(self):
        village = VillageState(village_id=1, x=0, y=0, merchant_count=20)

        assert village.max_busy_merchants is None
        assert village.merchant_budget(reserve=2) == 18
        assert village.merchant_budget(reserve=0) == 20

    def test_a_cap_below_the_fleet_binds(self):
        village = VillageState(village_id=1, x=0, y=0, merchant_count=20, max_busy_merchants=8)

        assert village.merchant_budget(reserve=2) == 8

    def test_a_cap_above_the_fleet_does_not_loosen_it(self):
        """A cap is a ceiling on the plan, never a promise of merchants."""
        village = VillageState(village_id=1, x=0, y=0, merchant_count=20, max_busy_merchants=20)

        assert village.merchant_budget(reserve=2) == 18

    def test_a_cap_of_zero_is_a_budget_of_zero(self):
        """Which is NOT the same as grounding the village -- see below."""
        village = VillageState(village_id=1, x=0, y=0, merchant_count=20, max_busy_merchants=0)

        assert village.merchant_budget(reserve=2) == 0

    def test_a_cap_is_not_a_reserve(self):
        """The whole reason this is a new field and not a per-village reserve.

        Village 26 fields 19 merchants. "8 busy" and "hold 12 back" are the same
        sentence only at a full 20, and the account this was written for has one
        village where they are not.
        """
        nineteen = VillageState(village_id=26, x=0, y=0, merchant_count=19, max_busy_merchants=8)

        assert nineteen.merchant_budget(reserve=0) == 8
        # The reserve that produces 8 at a full fleet leaves this village 7.
        assert (
            VillageState(village_id=26, x=0, y=0, merchant_count=19).merchant_budget(reserve=12)
            == 7
        )


class TestTheCapBindsThePlan:
    def test_without_it_the_hub_is_inside_its_fleets_budget(self):
        """The control. 8 per send x 2 sets in flight is section 8's own sum."""
        res = _plan()

        hub = _budget(res, HUB)
        assert (hub.committed, hub.spare, hub.over_budget) == (16, 18, False)
        assert [(leg.merchants_per_send, leg.sets_in_flight) for leg in hub.legs] == [(8, 2)]
        assert res.feasible

    def test_the_capped_village_goes_over_by_the_predicted_amount(self):
        res = _plan(caps={HUB: 8})

        hub = _budget(res, HUB)
        assert hub.committed == 16, "the haul itself did not change"
        assert hub.spare == 8, "the budget is the operator's cap, not the fleet's 18"
        assert hub.over_budget
        assert hub.free == -8, "over by 16 - 8"
        assert not res.feasible

    def test_the_over_budget_record_reports_the_cap_as_what_was_available(self):
        res = _plan(caps={HUB: 8})

        assert [(o.village_id, o.committed, o.spare) for o in res.budgets if o.over_budget] == [
            (HUB, 16, 8)
        ]

    def test_no_other_villages_budget_moves(self):
        """The whole point against `merchant_reserve`, which moves all of them."""
        loose = _plan()
        capped = _plan(caps={HUB: 8})

        for vid in (NEAR, FAR):
            before = _budget(loose, vid)
            after = _budget(capped, vid)
            assert (after.committed, after.spare, after.over_budget) == (
                before.committed,
                before.spare,
                before.over_budget,
            ), vid

    def test_the_other_villages_rows_do_not_move_either(self):
        loose = _plan()
        capped = _plan(caps={HUB: 8})

        def rows_from(res, vid):
            return sorted(
                (r.destination, tuple(sorted(r.cargo.items())), r.cycle_hours, r.merchants)
                for r in res.rows
                if r.origin == vid
            )

        assert rows_from(capped, NEAR) == rows_from(loose, NEAR)

    def test_a_reserve_that_reaches_the_same_cap_takes_every_village_with_it(self):
        """The negative control, and the measurement that made this field exist."""
        reserved = _plan(merchant_reserve=12)

        assert [b.spare for b in reserved.budgets] == [8, 8, 8], (
            "an account-wide reserve is the only lever there was"
        )
        capped = _plan(caps={HUB: 8})
        assert [(b.village_id, b.spare) for b in capped.budgets] == [
            (HUB, 8),
            (NEAR, 18),
            (FAR, 18),
        ]

    def test_a_cap_can_cost_cadence_instead_of_feasibility(self):
        """On a long haul the cap does not refuse the plan, it slows it.

        Left alone the latency pass spends idle merchants on a shorter cycle --
        10 merchants for a 1h beat. Told 8 is the ceiling it takes the 3h beat
        that fits. Both plans are feasible, and only one of them obeys the
        operator.
        """
        loose = asyncio.run(post_plan(PlanRequest.model_validate(_long_haul()), USER))
        capped = asyncio.run(post_plan(PlanRequest.model_validate(_long_haul(cap=8)), USER))

        assert [(r.cycle_hours, r.merchants) for r in loose.rows] == [(1, 2)]
        assert _budget(loose, HUB).committed == 10
        assert [(r.cycle_hours, r.merchants) for r in capped.rows] == [(3, 4)]
        assert _budget(capped, HUB).committed == 8
        assert capped.feasible and loose.feasible

    def test_the_cap_reaches_the_crowding_report_as_the_budget_it_is(self):
        """Crowding is measured against the budget, so it follows the cap.

        Not a separate code path -- ``_crowding_findings`` already reads the
        budgets map -- but worth pinning, because a crowding warning quoting the
        fleet's 18 while the plan was held to 8 would be arithmetic nobody can
        follow.
        """
        res = _plan(caps={HUB: 12})

        hub = _budget(res, HUB)
        assert (hub.committed, hub.spare) == (16, 12)
        crowded = [w for w in res.warnings if "of its 12" in w]
        assert crowded, [w for w in res.warnings if "merchants" in w]


class TestNoCapPlansExactlyAsBefore:
    def test_the_field_absent_and_the_field_null_are_the_same_request(self):
        absent = _plan()
        explicit = asyncio.run(
            post_plan(
                PlanRequest.model_validate(
                    _payload()
                    | {
                        "config": [
                            {"village_id": vid, "max_busy_merchants": None}
                            for vid in (HUB, NEAR, FAR)
                        ]
                    }
                ),
                USER,
            )
        )

        assert explicit.model_dump() == absent.model_dump()

    def test_the_default_is_none_on_the_schema(self):
        assert VillageConfig(village_id=HUB).max_busy_merchants is None


class TestTheSchemaGuardsTheCap:
    def test_a_negative_cap_is_refused(self):
        with pytest.raises(ValidationError):
            VillageConfig(village_id=HUB, max_busy_merchants=-1)

    def test_zero_is_accepted_because_it_means_something(self):
        """ "Every route from here is a budget breach" is an answer, not a typo."""
        assert VillageConfig(village_id=HUB, max_busy_merchants=0).max_busy_merchants == 0

    def test_a_cap_above_the_villages_own_merchants_is_refused(self):
        """A ceiling the village cannot reach is a typo, not a plan.

        Checked against the snapshot rather than clamped: clamping would accept
        "02 may run 30 busy" and plan 18, so the operator's file and the plan
        would disagree about the account with nothing saying so.
        """
        with pytest.raises(ValidationError, match="21"):
            PlanRequest.model_validate(_payload(caps={HUB: 21}))

    def test_the_refusal_names_the_village_the_operator_named_it(self):
        with pytest.raises(ValidationError) as caught:
            PlanRequest.model_validate(_payload(caps={HUB: 25}))

        detail = str(caught.value)
        assert "02" in detail, detail
        assert "20" in detail, detail

    def test_a_cap_equal_to_the_fleet_is_accepted(self):
        res = _plan(caps={HUB: 20})

        assert _budget(res, HUB).spare == 18, "the reserve still applies under it"

    def test_a_cap_for_a_village_the_snapshot_does_not_contain_is_refused(self):
        with pytest.raises(ValidationError, match="99999"):
            PlanRequest.model_validate(
                _payload() | {"config": [{"village_id": 99999, "max_busy_merchants": 4}]}
            )

    def test_an_unread_merchant_count_is_not_a_fleet_of_zero(self):
        """`/snapshot` encodes a count it could not read as 0, and says so.

        It warns about those villages separately -- "no merchant count read
        for ...; they cannot send until it is known". Read here as a FLEET, one
        failed parse refused every cap on that village: a 422 from all four
        endpoints blaming merchant training, over a plan that runs identically
        without the cap. Unknown is not zero, which is the principle the page's
        own `unreachableCaps` states.
        """
        unread = [
            _village(HUB, "02", 0, 0, lumber=20_000, merchants=0),
            _village(NEAR, "03", 2, 0, clay=3_000),
            _village(FAR, "05", 10, 0),
        ]

        res = _plan(caps={HUB: 8}, snapshot=unread)

        # It plans, and what bounds the village is the count nobody could read
        # -- the same answer as with no cap at all, which is why refusing the
        # request bought nothing.
        assert _budget(res, HUB).spare == _budget(_plan(snapshot=unread), HUB).spare

    def test_the_sentinel_is_skipped_on_every_request_model(self):
        """Because it was refused by all four, being one model-level rule."""
        unread = [
            _village(HUB, "02", 0, 0, lumber=20_000, merchants=0),
            _village(NEAR, "03", 2, 0, clay=3_000),
            _village(FAR, "05", 10, 0),
        ]
        for model in (PlanRequest, DayCheckRequest, ExecuteRequest, NightProfileRequest):
            body = _payload(caps={HUB: 8}, snapshot=unread)
            if model is DayCheckRequest:
                body["segments"] = [
                    {"name": "All day", "window": [0, 1439], "allocations": body.pop("allocations")}
                ]
            request = model.model_validate(body)

            assert [c.max_busy_merchants for c in request.config if c.village_id == HUB] == [8], (
                model.__name__
            )

    def test_it_is_refused_on_every_request_model(self):
        """One rule for all four planning paths, at the schema.

        Put in the handler it would have to be repeated four times, which is
        precisely how `/night-profile` came to ignore a declared spend.
        """
        for model in (PlanRequest, DayCheckRequest, ExecuteRequest, NightProfileRequest):
            body = _payload(caps={HUB: 21})
            if model is DayCheckRequest:
                # A day check IS its segments, and a model-level validator only
                # runs once the fields are all valid -- so a body missing them
                # would report that instead and prove nothing about the cap.
                body["segments"] = [
                    {"name": "All day", "window": [0, 1439], "allocations": body.pop("allocations")}
                ]
            with pytest.raises(ValidationError, match="21") as caught:
                model.model_validate(body)
            assert "02" in str(caught.value), model.__name__


class TestTheReserveIsBoundedLikeTheCap:
    """The account-wide twin of the cap, and it had no ceiling at all.

    A reserve of 50 holds back merchants no village has: every budget goes to 0,
    every village lands over budget, and the request is still ACCEPTED -- while
    a CAP of 50 is refused by name. The same 20 bounds both, because it is
    Travian's own ceiling on merchants in one village; a reserve past it cannot
    describe any account that exists.

    Exposed by P3 rather than introduced by it: the field was always on
    `PlanRequest` and the page had never sent it.
    """

    def test_a_reserve_past_what_a_village_can_hold_is_refused(self):
        with pytest.raises(ValidationError, match="20"):
            PlanRequest.model_validate(_payload(merchant_reserve=21))

    def test_the_bound_itself_is_accepted(self):
        """Holding all 20 back is a legible answer: nothing ships tonight."""
        request = PlanRequest.model_validate(_payload(merchant_reserve=20))

        assert request.merchant_reserve == 20

    def test_the_reserve_that_took_the_whole_account_over_budget_is_gone(self):
        """What the missing bound actually did, on the fixture that showed it."""
        with pytest.raises(ValidationError):
            PlanRequest.model_validate(_payload(merchant_reserve=50))


class TestACapOfZeroDoesNotGroundTheVillage:
    """It was documented as doing exactly that, in four places. It does not.

    The merchant budget is SOFT everywhere in this optimizer: exceeding it is
    costed, recorded as `over_budget` and refused at `/execute`, never routed
    around. A cap is one more budget, so 0 does not withdraw the village from
    the plan -- its routes are built and every one of them becomes a breach.

    Saying otherwise sent the operator looking for a village that had quietly
    stopped shipping, and it is the wording that was wrong rather than the
    mechanism: a hard exclusion is a different lever (`ship_only_to` is the one
    that exists) and inventing a second one here would make 0 the only figure
    in the field that changes what the planner IS rather than what it may spend.
    """

    def test_the_routes_survive_and_every_one_of_them_is_a_breach(self):
        res = _plan(caps={HUB: 0})

        assert [r.origin for r in res.rows].count(HUB) == 1, "the route is still planned"
        hub = _budget(res, HUB)
        assert (hub.committed, hub.spare, hub.free, hub.over_budget) == (16, 0, -16, True)

    def test_the_sheet_is_refused_rather_than_replanned_without_the_village(self):
        res = _plan(caps={HUB: 0})

        assert res.feasible is False
        assert res.verdict.blockers, "refused with nothing to tell the operator"

    def test_the_schema_says_what_zero_does_rather_than_what_it_does_not(self):
        """The falsehood was in prose, so prose is what has to be pinned.

        Four surfaces said "0 grounds the village" -- this one, the Max busy
        column's tooltip, the setup file's own notes and a Playwright title.
        """
        description = VillageConfig.model_fields["max_busy_merchants"].description

        assert "grounds" not in description, description
        assert "breach" in description, description


class TestTheExplanationNamesWhatIsActuallyBinding:
    """'02 needs 16 merchants but has 8' reads as a fact about the fleet.

    It is not: the village has 20 and could spare 18. Saying "has 8" of an
    operator ceiling sends them to the Trade Office and the map to fix a number
    they typed themselves.
    """

    def test_the_cap_is_named_when_the_cap_is_what_binds(self):
        res = _plan(caps={HUB: 8})

        explanation = _budget(res, HUB).explanation
        assert explanation is not None
        assert "capped" in explanation, explanation
        assert "8 busy" in explanation, explanation
        # And the fleet it is measured against, so the operator can see the room
        # the cap is holding back rather than only the ceiling.
        assert "18" in explanation, explanation

    def test_raising_the_cap_is_offered_as_the_fix_it_is(self):
        res = _plan(caps={HUB: 8})

        assert "16" in _budget(res, HUB).explanation

    def test_the_fleet_is_named_when_the_fleet_is_what_binds(self):
        """No cap: the message is what it always was, word for word."""
        capped_out = _plan(merchant_reserve=18)

        explanation = _budget(capped_out, HUB).explanation
        assert explanation is not None
        assert "needs 16 merchants but has 2" in explanation, explanation
        assert "capped" not in explanation, explanation

    def test_a_cap_above_the_fleet_does_not_claim_to_be_binding(self):
        """The cap is set but the reserve is tighter, so the fleet is the story."""
        res = _plan(caps={HUB: 20}, merchant_reserve=18)

        explanation = _budget(res, HUB).explanation
        assert explanation is not None
        assert "capped" not in explanation, explanation

    def test_a_cap_level_with_the_fleets_own_spare_is_not_what_binds(self):
        """20 merchants, a reserve of 8 and a cap of 12 are one ceiling, not two.

        On equality the two figures coincide, so "you capped it at 12 busy at
        once; its fleet could otherwise spare 12" blamed the operator for a
        limit the RESERVE imposed -- and then offered raising the cap to 16,
        which under that reserve buys nothing at all. The case that existed put
        a cap of 20 against a spare of 2, strict inequality, so it never came
        near this.
        """
        res = _plan(caps={HUB: 12}, merchant_reserve=8)

        explanation = _budget(res, HUB).explanation
        assert explanation is not None
        assert "capped" not in explanation, explanation
        assert "needs 16 merchants but has 12" in explanation, explanation

    def test_no_cap_is_offered_that_the_request_layer_would_refuse(self):
        """ "Raising 02's cap to 48" -- and 02 fields 20 merchants.

        A cap above `merchants_total` is a data-entry error refused by name at
        the schema, so advising one sends the operator round a loop: follow the
        advice, get a 422. Where no reachable cap fits, the haul is what has to
        move, and that is worth saying instead.
        """
        big = [
            _village(HUB, "02", 0, 0, lumber=60_000),
            _village(NEAR, "03", 2, 0, clay=3_000),
            _village(FAR, "05", 10, 0),
        ]

        explanation = _budget(_plan(caps={HUB: 8}, snapshot=big), HUB).explanation

        assert "Raising" not in explanation, explanation
        assert "48" in explanation, "what the plan wants is still worth saying"
        assert "20" in explanation, "and the fleet that cannot reach it"

    def test_a_reachable_cap_is_still_offered_as_the_fix_it_is(self):
        assert "Raising 02's cap to 16" in _budget(_plan(caps={HUB: 8}), HUB).explanation

    def test_the_trade_office_advice_still_applies_under_a_cap(self):
        """A cap is on merchants, and a Trade Office level is what one carries.

        So the upgrade genuinely fixes an over-cap village -- fewer merchants
        carry the same haul -- and the advice has to be costed against the cap
        rather than the fleet, or it recommends a level that still does not fit.
        """
        res = _plan(caps={HUB: 8})

        hub = _budget(res, HUB)
        assert hub.trade_office_levels_needed == 5, (
            "20,000/h on 4 merchants per send needs 5,000 each"
        )
        assert f"+{hub.trade_office_levels_needed}" in hub.explanation

    def test_the_refusal_names_the_ceiling_too_not_only_the_budget(self):
        """The verdict and /execute's 422 body are the same tuple: `blockers`.

        It said "its budget allows 8" with no mention of whose 8 it was, on the
        one surface whose whole job is refusing to write to the account.
        """
        nineteen = [
            _village(HUB, "02", 0, 0, lumber=20_000, merchants=19),
            _village(NEAR, "03", 2, 0, clay=3_000),
            _village(FAR, "05", 10, 0),
        ]

        res = _plan(caps={HUB: 8}, snapshot=nineteen)

        assert res.feasible is False
        blocker = next(b for b in res.verdict.blockers if "02" in b)
        assert merchant_ceiling_clause(8, 17) in blocker, blocker
        # And the budget explanation says it the same way, off one helper.
        assert merchant_ceiling_clause(8, 17) in _budget(res, HUB).explanation

    def test_the_wording_is_decided_in_one_place(self):
        """Driven directly, so the two branches are pinned without a fixture."""
        legs = _budget(_plan(caps={HUB: 8}), HUB).legs

        capped = _explain_over_budget(
            "02", 16, 8, legs, 0, 2500, None, max_busy=8, merchants_total=20, fleet_spare=18
        )
        fleet = _explain_over_budget(
            "02", 16, 2, legs, 0, 2500, None, max_busy=None, merchants_total=20, fleet_spare=2
        )

        assert "capped it at 8 busy" in capped, capped
        assert "needs 16 merchants but has 2" in fleet, fleet


class TestTheCapReachesEveryPlanningPath:
    """`/plan`, `/day-check`, `/execute segments` and `/night-profile`.

    Four, not three: the night endpoint inherits every planner input from
    ``PlanRequest``, does NOT share ``_plan_account``, and SEEDS the other three
    -- the page writes its derived allocations straight into the active profile.
    A budget it ignores is a budget missing from every plan built on that night.
    """

    def test_plan_holds_the_hub_to_the_cap(self):
        capped = asyncio.run(post_plan(PlanRequest.model_validate(_long_haul(cap=8)), USER))

        assert _budget(capped, HUB).committed == 8

    def test_the_day_check_plans_each_segment_under_the_cap(self):
        def run(cap):
            body = _long_haul(cap=cap) if cap is not None else _long_haul()
            allocations = body.pop("allocations")
            return asyncio.run(
                post_day_check(
                    DayCheckRequest.model_validate(
                        body
                        | {
                            "prune_to_window": False,
                            "segments": [
                                {"name": "All day", "window": [0, 1439], "allocations": allocations}
                            ],
                        }
                    ),
                    USER,
                )
            )

        # The cap changes the cycle, so it changes the route the day check is
        # built from -- and the check reports that route's own latency. A 1h beat
        # over a 2.5h haul lands 3.5h after production; the 3h beat the cap can
        # afford lands 5.5h after. The composite has to be planning the capped
        # route to be able to say the second number.
        loose = run(None)
        capped = run(8)

        def latency(day):
            return [w for w in day.warnings if "latency" in w]

        assert any("3.5h latency" in w for w in latency(loose)), latency(loose)
        assert any("5.5h latency" in w for w in latency(capped)), latency(capped)

    def test_the_execute_dry_run_plans_from_the_cap(self):
        """Driven, not merely parsed. /execute recomputes the plan server-side
        precisely because it must not trust client rows, so the cap has to reach
        the routes a live run would create. A dry run issues zero game requests.
        """
        body = _long_haul(cap=8)
        allocations = body.pop("allocations")
        request = ExecuteRequest.model_validate(
            body
            | {
                "dry_run": True,
                "max_routes_per_run": 50,
                "prune_to_window": True,
                "segments": [{"name": "All day", "window": [0, 1439], "allocations": allocations}],
            }
        )

        assert [c.max_busy_merchants for c in request.config if c.village_id == HUB] == [8]

        res = asyncio.run(post_execute(request, USER))

        assert res.dry_run
        creating = [a for a in res.actions if a.origin == HUB and a.status == "would_create"]
        assert creating, [(a.origin, a.destination, a.status) for a in res.actions]
        # 4 per send on a 3h cycle: the cap's plan, not the 2-on-1h the latency
        # pass reaches for when nothing stops it.
        assert {(a.merchants, a.cycle_hours) for a in creating} == {(4, 3)}

    def test_the_night_derivation_ships_only_what_the_cap_can_carry(self):
        """A retention is only real if the merchants exist to move the rest.

        The derivation already refuses to plan an export a village's fleet
        cannot ship in the hours it has. Under a cap the fleet that matters is
        the cap, so the village keeps what it cannot send instead of being given
        a retention it has no way to honour.
        """
        # A small warehouse, so the night's own ceiling is 1,000/h and the shed
        # limit is what actually decides the retention. With the fleet free the
        # village can move 5,625/h and keeps the ceiling's 1,000; held to 4
        # merchants it can move 1,250/h and has to keep the other 3,750.
        snapshot = [
            _village(HUB, "02", 0, 0, lumber=5_000, crop=1_000, cap=16_000),
            _village(FAR, "05", 30, 0, lumber=100, crop=1_000, cap=400_000),
        ]
        base = {
            "snapshot": snapshot,
            "allocations": {
                "lumber": {str(FAR): {"mode": "remainder", "value": 0}},
                "crop": {str(FAR): {"mode": "remainder", "value": 0}},
            },
            "dispatch_window": [23 * 60, 7 * 60],
            "foreign_targets": [],
            "speed_fields_per_hour": 12.0,
        }

        def derive(cap):
            config = [
                {"village_id": HUB, **({"max_busy_merchants": cap} if cap is not None else {})},
                {"village_id": FAR},
            ]
            return asyncio.run(
                post_night_profile(
                    NightProfileRequest.model_validate(base | {"config": config}), USER
                )
            )

        loose = derive(None)
        capped = derive(4)

        kept_loose = loose.allocations[Resource.LUMBER][HUB].value
        kept_capped = capped.allocations[Resource.LUMBER][HUB].value
        assert kept_capped > kept_loose, (
            f"the cap did not reach the shed limit: {kept_loose} -> {kept_capped}"
        )


class TestTheRefusalIsA422OnTheWire:
    """In-process a validator raises; the operator gets a JSON body to read.

    The refusal is only useful if it reaches the page naming the village, so it
    is checked where that is decided rather than as a Python exception.
    """

    def test_an_unreachable_cap_answers_422_naming_the_village(self, client):
        response = client.post("/api/distribution/plan", json=_payload(caps={HUB: 25}))

        assert response.status_code == 422, response.text
        assert "02" in response.text
        assert "25" in response.text

    def test_a_cap_the_village_can_reach_is_planned(self, client):
        response = client.post("/api/distribution/plan", json=_payload(caps={HUB: 8}))

        assert response.status_code == 200, response.text
        body = response.json()
        hub = next(b for b in body["budgets"] if b["village_id"] == HUB)
        assert (hub["committed"], hub["spare"], hub["over_budget"]) == (16, 8, True)
        assert "capped" in hub["explanation"]
