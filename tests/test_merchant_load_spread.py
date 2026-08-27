"""Two related things: what the search is allowed to believe, and how it spreads.

**Cadence blindness.** ``_improve_flows`` priced every pair with the full cycle
list while the route builder restricts a capped destination through
``_cycles_for``. So for any destination carrying ``max_cycle_hours`` the search
optimised against a price nobody could pay -- measured on a real payload, it
believed a long haul to a 1h-capped tribute cost 5 merchants by choosing a 2h
cycle it was not allowed to use, where the builder had to pay 9. The visible
consequence was a village committing 19 merchants against a budget of 18, in a
plan the search thought was *cheaper*. Hard feasibility is the FIRST key of the
objective, so that is precisely what cannot be allowed to happen.

**Load spreading.** The first key is ``max(0, committed - budget)``, so a village
at exactly its budget costs nothing and nothing pushes work off it; the second is
``total_merchants``, which actively concentrates, because a further origin means a
longer round trip means more sets in flight. A soft cap priced into the merchant
term at :data:`SOFT_BUDGET_PRICE` is what makes spreading worth something --
bounded, and never able to buy an infeasible plan.

Every fixture here was found by searching for one that actually reproduces
(scratchpad/find_cadence_repro.py). The first versions of both tests passed with
the bugs reintroduced, which is worth stating plainly: an invariant nobody has
watched fail is a guess about what the code does.
"""

import asyncio

import pytest

from travian_api.services.distribution import optimizer
from travian_api.services.distribution import planner as planner_mod
from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
    resolve_resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import DAILY_BEAT_CYCLES, EUROPE2_TEUTON
from travian_api.services.distribution.optimizer import VillageState, build_plan
from travian_api.web.routes import distribution as dist

from .distribution_synthetic import random_account
from .test_distribution_audit import USER

GEOMETRY = MapGeometry(span=401, speed_fields_per_hour=12.0)
MODEL = EUROPE2_TEUTON


def _excess(plan) -> int:
    return sum(over.excess for over in plan.over_budget)


def _peak_utilisation(plan, villages, reserve: int = 2) -> float:
    peak = 0.0
    for vid, used in plan.merchants_committed.items():
        budget = villages[vid].spare_merchants(reserve)
        if budget > 0:
            peak = max(peak, used / budget)
    return peak


def _fan_in(*, villages_count: int, spread: int, budget: int, surplus: int):
    """Several senders strung out along a line, all shipping to village 1.

    The parameters are the ones that separate a search which knows the cadence
    cap from one which does not: distance sets sets-in-flight, the budget sets how
    little slack there is to get it wrong with, and the surplus sets how much
    cargo must move under a cap that forbids the cheap long cycles.
    """
    villages = {
        1: VillageState(
            1, 0, 0, merchant_count=budget + 2, trade_office_level=10, crop_per_hour=0.0
        )
    }
    production = {1: 0.0}
    allocations = {1: Allocation(AllocationMode.REMAINDER)}
    for i in range(2, villages_count + 1):
        villages[i] = VillageState(
            i,
            spread * (i - 1),
            spread // 2 * (i % 3),
            merchant_count=budget + 2,
            trade_office_level=10,
            crop_per_hour=float(surplus),
        )
        production[i] = float(surplus)
        allocations[i] = Allocation(AllocationMode.ABSOLUTE, 0.0)
    plans = {Resource.CROP: resolve_resource(Resource.CROP, production, allocations)}
    return villages, plans


# Fixtures confirmed to separate the fixed search from the blind one, recorded
# with the numbers they were chosen for so a later change that stops reproducing
# is visible rather than silently vacuous.
CADENCE_REPRODUCTIONS = [
    pytest.param(8, 25, 16, 4000, 1, id="8v_tight_budget_1h_cap"),  # fixed 4, blind 38
    pytest.param(6, 40, 10, 4000, 1, id="6v_far_1h_cap"),  # blind: seed 57 -> full 85
    pytest.param(8, 25, 6, 4000, 1, id="8v_starved_budget_1h_cap"),  # fixed 51, blind 96
    pytest.param(6, 40, 6, 9000, 2, id="6v_far_2h_cap"),
    pytest.param(4, 12, 6, 4000, 1, id="4v_near_1h_cap"),
]


class TestTheSearchPricesWhatTheBuilderWillPay:
    @pytest.mark.parametrize("villages_count,spread,budget,surplus,cap", CADENCE_REPRODUCTIONS)
    def test_improving_never_raises_over_budget_excess(
        self, villages_count: int, spread: int, budget: int, surplus: int, cap: int
    ) -> None:
        """The seed is the plan with the search switched off.

        Every move the search makes is supposed to lower a lexicographic
        objective whose first key is over-budget excess, so excess can only fall.
        Priced with cycles the builder cannot use, it rose instead -- on the
        second fixture here, from 57 to 85.
        """
        villages, plans = _fan_in(
            villages_count=villages_count, spread=spread, budget=budget, surplus=surplus
        )
        common = dict(
            max_latency_hours=None,
            cycles=DAILY_BEAT_CYCLES,
            max_cycle_by_destination={1: cap},
        )

        seeded = build_plan(villages, plans, GEOMETRY, MODEL, max_improve_passes=0, **common)
        improved = build_plan(villages, plans, GEOMETRY, MODEL, **common)

        assert _excess(improved) <= _excess(seeded), (
            f"the improvement pass raised over-budget excess from {_excess(seeded)} to "
            f"{_excess(improved)} under a {cap}h cadence cap. It is optimising against "
            f"merchant costs the route builder will not honour, so hard feasibility -- "
            f"the first key of the objective -- is not actually protected."
        )

    @pytest.mark.parametrize("cap", [1, 2])
    def test_every_route_obeys_the_cap_it_was_priced_under(self, cap: int) -> None:
        """The other half: a cap the search respects must reach the routes too,
        or search and builder would merely be consistently wrong together."""
        villages, plans = _fan_in(villages_count=8, spread=25, budget=16, surplus=4000)

        plan = build_plan(
            villages,
            plans,
            GEOMETRY,
            MODEL,
            max_latency_hours=None,
            max_cycle_by_destination={1: cap},
        )

        offenders = [
            (r.origin, r.destination, r.cycle_hours)
            for r in plan.routes
            if r.destination == 1 and r.cycle_hours > cap
        ]
        assert not offenders, f"routes exceeded the {cap}h cap: {offenders}"


class TestTheSoftCapSpreadsLoad:
    def test_zero_headroom_is_a_true_no_op(self) -> None:
        """Switching the feature off must leave the objective inert.

        What this pins: at zero headroom the plan does not depend on
        SOFT_BUDGET_PRICE. That is the property an operator relies on when they
        set headroom to 0 to get the old behaviour back, and it is worth a test.

        What it does NOT pin, stated because the distinction was learned the hard
        way: it does not detect an unclamped crowding term. Unclamped, crowding
        equals the hard excess at zero headroom, and since hard excess is the
        FIRST key an extra copy of it inside the second cannot reorder anything --
        so clamped and unclamped agree on 12 of 12 real payload cases and this
        test passes either way. The clamp is bookkeeping. An earlier version of
        this docstring claimed the test caught it; it did not.

        The fixture is deliberately OVER budget so the price has a non-zero
        quantity to scale if the ordering ever changes.
        """
        villages, plans = _fan_in(villages_count=8, spread=25, budget=6, surplus=9000)

        reference = build_plan(
            villages, plans, GEOMETRY, MODEL, max_latency_hours=None, merchant_headroom=0.0
        )
        assert _excess(reference) > 0, (
            "fixture is within budget, so a soft term duplicating the hard excess "
            "would scale zero and this test could not detect it"
        )

        original = optimizer.SOFT_BUDGET_PRICE
        try:
            for price in (0, 1, 5, 50):
                optimizer.SOFT_BUDGET_PRICE = price
                produced = build_plan(
                    villages,
                    plans,
                    GEOMETRY,
                    MODEL,
                    max_latency_hours=None,
                    merchant_headroom=0.0,
                )
                assert produced.routes == reference.routes, (
                    f"at zero headroom, SOFT_BUDGET_PRICE={price} changed the route "
                    f"set. Crowding is not identically zero with the feature off, so "
                    f"switching it off does not restore the old objective."
                )
                assert produced.merchants_committed == reference.merchants_committed
        finally:
            optimizer.SOFT_BUDGET_PRICE = original

    def test_headroom_lowers_peak_utilisation(self) -> None:
        villages, plans = _fan_in(villages_count=9, spread=10, budget=18, surplus=5000)

        tight = build_plan(
            villages, plans, GEOMETRY, MODEL, max_latency_hours=None, merchant_headroom=0.0
        )
        spread = build_plan(
            villages, plans, GEOMETRY, MODEL, max_latency_hours=None, merchant_headroom=0.10
        )

        tight_peak = _peak_utilisation(tight, villages)
        spread_peak = _peak_utilisation(spread, villages)
        assert tight_peak > 0.5, (
            f"fixture does not concentrate load ({tight_peak:.0%} peak), so it cannot "
            f"show that headroom spreads it"
        )
        assert spread_peak <= tight_peak, (
            f"headroom raised peak utilisation from {tight_peak:.0%} to {spread_peak:.0%}"
        )

    @pytest.mark.parametrize("target_hours", [1.0, 2.0, 4.0])
    def test_the_latency_pass_does_not_spend_the_headroom(self, target_hours: float) -> None:
        """The latency pass runs AFTER the improvement search, spending idle
        merchants on shorter cycles. Spending them up to the HARD budget undoes
        the exact thing the search just paid merchants for: a village the search
        left under its soft cap was refilled to 100%, the API description
        ("aims to leave this fraction uncommitted") became false whenever a
        latency target was set, and MERCHANTS_CROWDED then reported villages the
        planner's own pass had pushed over. Measured before the fix: peak 78% ->
        100% and 0 -> 3 over-cap villages on one real payload, purely from the
        target being present.

        The property: a village under its soft cap without a latency target must
        still be under it with one. The only thing the target changes is the
        latency pass, so any breach is that pass spending past the cap.
        Fixture found by search (scratchpad/find_erosion_fixture.py): 102
        parameter combinations reproduced; this is the smallest.
        """
        villages, plans = _fan_in(villages_count=5, spread=15, budget=10, surplus=7000)

        quiet = build_plan(
            villages, plans, GEOMETRY, MODEL, max_latency_hours=None, merchant_headroom=0.10
        )
        fast = build_plan(
            villages,
            plans,
            GEOMETRY,
            MODEL,
            max_latency_hours=target_hours,
            merchant_headroom=0.10,
        )

        breaches = []
        for vid, village in villages.items():
            budget = village.spare_merchants(2)
            soft = budget - int(budget * 0.10 + 0.5)
            was = quiet.merchants_committed.get(vid, 0)
            now = fast.merchants_committed.get(vid, 0)
            if was <= soft < now:
                breaches.append((vid, was, now, soft))
        assert not breaches, (
            f"a {target_hours}h latency target lifted villages past the soft cap the "
            f"improvement search had just protected: {breaches} (village, before, after, cap)"
        )

    @pytest.mark.parametrize("headroom", [0.10, 0.20, 0.35])
    @pytest.mark.parametrize("seed", [0, 1, 3, 8])
    def test_headroom_never_makes_a_feasible_plan_infeasible(
        self, seed: int, headroom: float
    ) -> None:
        """Hard feasibility outranks crowding, so headroom must never be bought
        with an over-budget village. Asserted through the endpoint, so the request
        field and its threading are covered as well."""
        account = random_account(seed, with_profiles=False)

        tight = asyncio.run(
            dist.post_plan(account.plan_request.model_copy(update={"merchant_headroom": 0.0}), USER)
        )
        spread = asyncio.run(
            dist.post_plan(
                account.plan_request.model_copy(update={"merchant_headroom": headroom}), USER
            )
        )

        if tight.feasible:
            over = [(b.village_id, b.committed, b.spare) for b in spread.budgets if b.over_budget]
            assert spread.feasible, f"headroom {headroom} made a feasible plan infeasible: {over}"


def test_the_planner_tells_the_optimizer_what_each_village_grows() -> None:
    """Relay refuses a hub whose crop rate it cannot read, so a construction site
    that forgets ``crop_per_hour`` silently disables relay account-wide. That is
    how five tests broke when the guard landed; nothing but this stops it
    recurring.
    """
    account = random_account(3, with_profiles=False)
    snapshot_rates = {v.village_id: v.crop_per_hour for v in account.plan_request.snapshot}

    seen: dict[int, float | None] = {}
    real_build = planner_mod.build_plan

    def spy(villages, *args, **kwargs):
        seen.update({vid: v.crop_per_hour for vid, v in villages.items()})
        return real_build(villages, *args, **kwargs)

    planner_mod.build_plan = spy
    try:
        asyncio.run(dist.post_plan(account.plan_request, USER))
    finally:
        planner_mod.build_plan = real_build

    assert seen, "build_plan was never reached, so this test proves nothing"
    for vid, rate in snapshot_rates.items():
        assert vid in seen, f"village {vid} never reached the optimizer"
        assert seen[vid] == rate, (
            f"village {vid} reached the optimizer with crop_per_hour={seen[vid]!r} "
            f"but the snapshot said {rate!r} -- relay silently refuses hubs whose rate "
            f"is None, so dropping it disables relay without a word"
        )
