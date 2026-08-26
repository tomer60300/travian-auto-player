"""Properties and regressions from the synthetic-account audit of the planner.

Two of these are properties rather than examples, and they are the reason the
file exists:

* **Oracle agreement.** ``simulate_day`` and ``simulate_profile_cycle`` are
  compared against ``tests/distribution_oracle.py`` -- a deliberately dumb
  minute-by-minute replay written from the specification rather than from the
  code. Every overflow warning the planner emits rests on those two functions,
  and the class of bug they hide (an average that fits while a batch does not)
  has been live before. Two independent implementations agreeing is the only
  evidence worth having.
* **Village-id permutation invariance.** Relabelling the villages must permute
  the plan and change nothing else. A plan that moves under a relabelling is
  reading dict iteration order or an id tie-break as if it were data, which no
  single-account test can reveal.

Everything else here is a regression: one named account per bug, the smallest
one that reproduces it, seed pinned.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from types import SimpleNamespace

import pytest

from tests.distribution_oracle import oracle_day, oracle_profile_cycle
from tests.distribution_synthetic import (
    adversarial_accounts,
    case_account,
    id_permutation,
    permute_ids,
    plan_signature,
    profile_windows,
    random_account,
)
from travian_api.services.distribution import optimizer
from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.merchants import RouteCost
from travian_api.services.distribution.optimizer import VillageState
from travian_api.services.distribution.schedule import ScheduledRoute
from travian_api.services.distribution.storage import simulate_day, simulate_profile_cycle
from travian_api.web.routes import distribution as dist

USER = SimpleNamespace(id=1)

# The oracle replays every minute of every day with no settling shortcut, so
# both sides run a shortened horizon. The settling RULE is identical on each
# side; only the number of days it may use is cut, which is what makes a
# minute-granularity cross-check affordable in a test suite.
ORACLE_DAYS = 6

_RATE = {
    Resource.LUMBER: "lumber_per_hour",
    Resource.CLAY: "clay_per_hour",
    Resource.IRON: "iron_per_hour",
    Resource.CROP: "crop_per_hour",
}
_STOCK = {
    Resource.LUMBER: "lumber_stock",
    Resource.CLAY: "clay_stock",
    Resource.IRON: "iron_stock",
    Resource.CROP: "crop_stock",
}


def _storage_inputs(body):
    """The stocks, capacities and own rates ``_storage_findings`` would build."""
    stocks: dict[int, dict[Resource, int]] = {}
    caps: dict[int, dict[Resource, int]] = {}
    own: dict[int, dict[Resource, float]] = {}
    for village in body.snapshot:
        for resource in Resource:
            rate = getattr(village, _RATE[resource])
            if rate is None:
                continue
            vid = village.village_id
            stocks.setdefault(vid, {})[resource] = getattr(village, _STOCK[resource])
            own.setdefault(vid, {})[resource] = float(rate)
            cap = (
                village.granary_capacity
                if resource is Resource.CROP
                else village.warehouse_capacity
            )
            if cap is not None:
                caps.setdefault(vid, {})[resource] = cap
    return stocks, caps, own


class TestOracleAgreement:
    """Two independent implementations of the same physics must agree."""

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 7, 13])
    def test_simulate_day_matches_the_oracle(self, seed: int) -> None:
        account = random_account(seed, with_profiles=False)
        body = account.plan_request
        planned = asyncio.run(dist._plan_account(body))
        stocks, caps, own = _storage_inputs(body)

        produced = simulate_day(planned.plan.beat, stocks, caps, own, step_minutes=1)
        expected = oracle_day(planned.plan.beat, stocks, caps, own)

        got = {(e.village_id, e.resource): e for e in produced}
        reportable = {key for key, value in expected.items() if value["wasted"] >= 1.0}
        assert set(got) == reportable, f"seed {seed}: overflow sets differ"
        for key, event in got.items():
            mine = expected[key]
            assert event.wasted_per_day == pytest.approx(mine["wasted"], abs=1.0), key
            assert event.net_gain_per_day == pytest.approx(mine["net_gain"], abs=1.0), key
            if not event.structural:
                assert event.minute == int(mine["first_full"]), key

    @pytest.mark.slow
    @pytest.mark.parametrize("seed", [0, 1, 2])
    def test_simulate_profile_cycle_matches_the_oracle(self, seed: int) -> None:
        """The composite day: production always on, each profile's routes only
        inside its own hours, arrivals credited where they land."""
        account = random_account(seed, with_profiles=True)
        captured: dict = {}
        real = dist.simulate_profile_cycle

        def spy(segments, own_rates, stocks, caps, ceilings=None, *args, **kwargs):
            captured["args"] = (segments, own_rates, stocks, caps, ceilings)
            return real(segments, own_rates, stocks, caps, ceilings, *args, **kwargs)

        dist.simulate_profile_cycle = spy
        try:
            asyncio.run(dist.post_day_check(account.day_request, USER))
        finally:
            dist.simulate_profile_cycle = real
        segments, own_rates, stocks, caps, ceilings = captured["args"]

        trajectories, breaches = simulate_profile_cycle(
            segments, own_rates, stocks, caps, ceilings, step_minutes=1, max_days=ORACLE_DAYS
        )
        rows, oracle_breaches, settled = oracle_profile_cycle(
            segments, own_rates, stocks, caps, ceilings, max_days=ORACLE_DAYS
        )

        assert {(t.village_id, t.resource) for t in trajectories} == set(rows)
        for t in trajectories:
            mine = rows[(t.village_id, t.resource)]
            assert t.low == pytest.approx(mine["low"], abs=1.0), (t.village_id, t.resource)
            assert t.high == pytest.approx(mine["high"], abs=1.0), (t.village_id, t.resource)
            assert t.daily_net == pytest.approx(mine["daily_net"], abs=1.0), (
                t.village_id,
                t.resource,
            )
        assert {(b.village_id, b.resource, b.kind) for b in breaches} == set(oracle_breaches)
        assert all(t.settled for t in trajectories) == settled


class TestVillageIdPermutation:
    """Relabelling the villages must permute the plan and change nothing else.

    A village id is an internal handle. Two accounts that differ only in which
    integer names which village are the same account, so they must get the same
    plan. Where they do not, the id is being read as data -- through dict
    iteration order or through an id tie-break -- and the plan the operator gets
    depends on an accident.

    The seeds below are the ones where the property currently holds; the ones
    where it does not are pinned as known defects in
    :class:`TestKnownDefects`, with the measured cost.
    """

    @pytest.mark.parametrize("seed", [1, 2, 4, 13, 14, 18, 21, 22, 28, 31, 32, 43, 46])
    def test_relabelling_permutes_the_plan(self, seed: int) -> None:
        account = random_account(seed, with_profiles=False)
        mapping = id_permutation(account.plan_request, seed + 1_000)

        original = asyncio.run(dist.post_plan(account.plan_request, USER))
        relabelled = asyncio.run(dist.post_plan(permute_ids(account.plan_request, mapping), USER))

        assert plan_signature(relabelled, mapping) == plan_signature(original), (
            f"seed {seed}: the plan changed when only the village ids moved"
        )

    def test_relabelling_survives_a_tie_between_two_identical_suppliers(self) -> None:
        """Two suppliers identical in distance and surplus are broken apart by
        village id, so the CHOSEN supplier may swap under a relabelling -- but
        the shape of the plan must not."""
        account = next(a for a in adversarial_accounts() if a.name == "adv-tied-suppliers")
        mapping = id_permutation(account.plan_request, 77)

        original = asyncio.run(dist.post_plan(account.plan_request, USER))
        relabelled = asyncio.run(dist.post_plan(permute_ids(account.plan_request, mapping), USER))

        assert relabelled.total_merchants == original.total_merchants
        assert len(relabelled.rows) == len(original.rows)
        assert relabelled.feasible == original.feasible

    @pytest.mark.slow
    @pytest.mark.parametrize("index", [1, 3])
    def test_relabelling_permutes_a_mid_sized_account(self, index: int) -> None:
        account = case_account(index)
        mapping = id_permutation(account.plan_request, 500 + index)

        original = asyncio.run(dist.post_plan(account.plan_request, USER))
        relabelled = asyncio.run(dist.post_plan(permute_ids(account.plan_request, mapping), USER))

        assert plan_signature(relabelled, mapping) == plan_signature(original)


class TestGeneratedProfilesTileTheDay:
    """The request model only rejects OVERLAPS, so the no-gap half is ours."""

    @pytest.mark.parametrize("shape", ["night8", "night7_start2", "night7_start2_end1"])
    def test_every_minute_is_covered_exactly_once(self, shape: str) -> None:
        seen: dict[int, int] = {}
        for _name, (start, end) in profile_windows(shape):
            span = range(start, end) if start < end else [*range(start, 1440), *range(end)]
            for minute in span:
                seen[minute] = seen.get(minute, 0) + 1

        assert sorted(seen) == list(range(1440))
        assert set(seen.values()) == {1}


# ---------------------------------------------------------------------------
# Invariants the audit's planted faults each broke. Each one holds on the
# current code and is the assertion that would have caught its fault.
# ---------------------------------------------------------------------------


class TestPlanArithmetic:
    @pytest.mark.parametrize("seed", [0, 2, 3, 5, 8, 13])
    def test_no_village_ships_more_than_it_makes_plus_what_arrives(self, seed: int) -> None:
        """Conservation at every node: outbound <= own positive production +
        inbound. A relay hub forwards cargo it never grew, so the ceiling has to
        be production PLUS arrivals rather than production alone -- but nothing
        may ship resources that were neither produced nor delivered.

        Skipped where the allocation itself over-claims the account: a negative
        remainder target does ask for more than exists, and the plan reports that
        as over-allocation rather than as a routing error.
        """
        account = random_account(seed, with_profiles=False)
        result = asyncio.run(dist.post_plan(account.plan_request, USER))
        if any(u.unallocated < -1e-6 for u in result.unallocated):
            pytest.skip("this seed over-allocates on purpose; the ceiling does not apply")

        produced = {
            v.village_id: sum(max(0.0, float(getattr(v, _RATE[r]) or 0.0)) for r in Resource)
            for v in account.plan_request.snapshot
        }
        out: dict[int, float] = {}
        into: dict[int, float] = {}
        for row in result.rows:
            hourly = row.total_cargo / row.cycle_hours
            out[row.origin] = out.get(row.origin, 0.0) + hourly
            into[row.destination] = into.get(row.destination, 0.0) + hourly

        for vid, shipped in out.items():
            # One unit of integer-rounding slack per route on each side.
            slack = 2.0 * (len(result.rows) + 1)
            assert shipped <= produced.get(vid, 0.0) + into.get(vid, 0.0) + slack, (
                f"seed {seed}: village {vid} ships {shipped:,.0f}/h but only "
                f"{produced.get(vid, 0.0):,.0f}/h is produced there and "
                f"{into.get(vid, 0.0):,.0f}/h arrives"
            )

    @pytest.mark.parametrize("seed", [0, 1, 3, 8])
    def test_every_leg_reserves_one_set_per_round_trip(self, seed: int) -> None:
        """A merchant is busy for the whole round trip, so the number of sets in
        flight is ceil(round trip / cycle). One set too few is a budget that
        cannot be staffed; one too many is waste."""
        account = random_account(seed, with_profiles=False)
        result = asyncio.run(dist.post_plan(account.plan_request, USER))

        for budget in result.budgets:
            for leg in budget.legs:
                expected = math.ceil(2 * leg.one_way_hours / leg.cycle_hours - 1e-9)
                assert leg.sets_in_flight == expected, (
                    f"seed {seed}: {budget.village_id} -> {leg.destination}, one way "
                    f"{leg.one_way_hours:.3f}h on a {leg.cycle_hours}h cycle needs "
                    f"{expected} sets, plan says {leg.sets_in_flight}"
                )
                assert leg.merchants == leg.merchants_per_send * leg.sets_in_flight

    @pytest.mark.parametrize("seed", [0, 1, 3, 8])
    def test_the_merchant_budget_is_the_villages_own_merchants(self, seed: int) -> None:
        """`spare` is what the village can staff: its merchants less the reserve.
        Nothing may inflate it -- an over-budget village that reads as within
        budget is known issue #6 in the profile."""
        account = random_account(seed, with_profiles=False)
        reserve = account.plan_request.merchant_reserve
        totals = {v.village_id: v.merchants_total for v in account.plan_request.snapshot}

        result = asyncio.run(dist.post_plan(account.plan_request, USER))

        for budget in result.budgets:
            if budget.village_id < 0:
                continue  # a foreign tribute sink: no village, no merchants
            expected = max(0, totals[budget.village_id] - reserve)
            assert budget.spare == expected
            assert budget.over_budget is (budget.committed > budget.spare)

    def test_a_store_that_fills_within_the_day_is_reported(self) -> None:
        """The discrete replay must report a cap that is actually reached. A
        silent overflow is resources thrown away with nothing said about it."""
        account = next(a for a in adversarial_accounts() if a.name == "adv-ship-into-a-full-store")

        result = asyncio.run(dist.post_plan(account.plan_request, USER))

        overflow = [w for w in result.warnings if "hits the cap" in w]
        assert overflow, "a route into a store already at its cap reported no overflow"
        assert result.diagnostics.total_loss_per_day > 0

    def test_an_arrival_lands_after_the_trip_not_when_it_was_sent(self) -> None:
        """Cargo is credited at the minute it ARRIVES, in whatever profile owns
        that minute. Crediting it at dispatch attributes the delivery to the
        sending profile and hides an overnight overflow behind the day."""
        from travian_api.services.distribution.optimizer import Route
        from travian_api.services.distribution.storage import ProfileSegment

        # One 24h route leaving at 23:30 under "night", travelling 120 minutes,
        # so it lands at 01:30 -- still night. Push the travel to 8 hours and it
        # lands at 07:30, under "day". The breach must follow the cargo.
        def breach_segment(travel_minutes: float) -> str:
            route = Route(
                origin=1,
                destination=2,
                cargo_per_hour={Resource.CROP: 1000.0},
                cycle_hours=24,
                merchants_per_send=1,
                sets_in_flight=1,
                one_way_minutes=travel_minutes,
            )
            night = ProfileSegment(
                name="night",
                start_minute=22 * 60,
                end_minute=6 * 60,
                routes=(ScheduledRoute(route=route, dispatch_minute=23 * 60 + 30),),
            )
            day = ProfileSegment(name="day", start_minute=6 * 60, end_minute=22 * 60)
            _trajectories, breaches = simulate_profile_cycle(
                [night, day],
                {1: {Resource.CROP: 2000.0}},
                {1: {Resource.CROP: 500_000}, 2: {Resource.CROP: 0}},
                {2: {Resource.CROP: 10_000}},
                step_minutes=5,
                max_days=2,
            )
            landing = [b for b in breaches if b.village_id == 2 and b.kind == "capacity"]
            assert landing, f"travel {travel_minutes}: nothing overflowed at the destination"
            return landing[0].segment

        assert breach_segment(120.0) == "night"
        assert breach_segment(8 * 60.0) == "day"

    def test_the_day_check_hands_the_simulation_the_real_travel_times(self) -> None:
        """The seam between the beat and the storage replay.

        The audit's hardest planted fault lived exactly here: the routes were
        marshalled into the simulation with their travel time zeroed, so every
        delivery landed the minute it was sent and was attributed to the sending
        profile. Both the artefact and the minute-by-minute oracle missed it --
        the artefact because a breach never names the delivery that caused it,
        the oracle because it is handed the same already-marshalled segments and
        faithfully simulates them. Only an assertion ON the marshalling catches
        it, so that is what this is.
        """
        account = random_account(0, with_profiles=True)
        captured: list = []
        real = dist.simulate_profile_cycle

        def spy(segments, own_rates, stocks, caps, ceilings=None, *args, **kwargs):
            captured.append(segments)
            return real(segments, own_rates, stocks, caps, ceilings, *args, **kwargs)

        dist.simulate_profile_cycle = spy
        try:
            asyncio.run(dist.post_day_check(account.day_request, USER))
        finally:
            dist.simulate_profile_cycle = real

        assert captured, "the day check never reached the storage simulation"
        checked = 0
        for segments in captured:
            for segment in segments:
                for scheduled in segment.routes:
                    route = scheduled.route
                    travel = round(route.one_way_minutes)
                    assert route.origin != route.destination
                    assert route.one_way_minutes > 0.0, (
                        f"route {route.origin} -> {route.destination} reached the "
                        f"simulation with no travel time at all"
                    )
                    for out_minute, in_minute in zip(
                        scheduled.dispatch_minutes, scheduled.arrival_minutes, strict=True
                    ):
                        assert in_minute == (out_minute + travel) % 1440
                        checked += 1
        assert checked > 0


class TestKnownDefects:
    """Bugs this audit found. Each is an xfail on the behaviour that is right,
    so the day one is fixed the test says so instead of staying quiet."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "AUDIT: _relay_scan draws hub candidates only from villages already "
            "inside the crop flow graph, so a crop-neutral midway village -- the "
            "canonical hub of profile section 8.5 -- can never be chosen. Give "
            "the same village a 100/h crop flow and the relay is found."
        ),
    )
    def test_a_crop_neutral_midway_village_can_act_as_a_relay_hub(self) -> None:
        account = next(a for a in adversarial_accounts() if a.name == "adv-relay-shape")

        result = asyncio.run(dist.post_plan(account.plan_request, USER))

        senders = {r.origin for r in result.rows if Resource.CROP in r.cargo}
        receivers = {r.destination for r in result.rows if Resource.CROP in r.cargo}
        assert senders & receivers, (
            "three feeders that cannot staff their own haul, and a midway village "
            "with 18 spare merchants, produced no relay at all"
        )

    def test_the_relay_is_found_once_the_hub_carries_any_crop_at_all(self) -> None:
        """The other half of the finding: nothing about the geometry or the
        merchant budget changes, only whether the hub already has a crop flow."""
        from travian_api.services.distribution.allocation import AllocationMode
        from travian_api.web.routes.distribution import AllocationInput

        account = next(a for a in adversarial_accounts() if a.name == "adv-relay-shape")
        hub = account.plan_request.snapshot[3].village_id
        allocations = {r: dict(per) for r, per in account.plan_request.allocations.items()}
        # The hub keeps 400/h of the 500/h it grows, so it ships 100/h.
        allocations[Resource.CROP][hub] = AllocationInput(mode=AllocationMode.ABSOLUTE, value=400.0)

        result = asyncio.run(
            dist.post_plan(
                account.plan_request.model_copy(update={"allocations": allocations}), USER
            )
        )

        senders = {r.origin for r in result.rows if Resource.CROP in r.cargo}
        receivers = {r.destination for r in result.rows if Resource.CROP in r.cargo}
        assert hub in senders & receivers

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "AUDIT: simulate_day gives up after MAX_SETTLING_DAYS=14 and reports "
            "whatever the 14th day happened to waste. A store that reaches its cap "
            "on day 15 is reported as losing nothing at all, though its net gain "
            "per day says it will sit at the cap for ever."
        ),
    )
    def test_a_store_that_reaches_its_cap_after_the_settling_horizon_is_reported(self) -> None:
        from travian_api.services.distribution.schedule import Beat

        # +1,000/h into a 400,000 store holding 40,000: full in exactly 15 days,
        # and losing 24,000/day every day after that.
        events = simulate_day(
            Beat(),
            {1: {Resource.CROP: 40_000}},
            {1: {Resource.CROP: 400_000}},
            {1: {Resource.CROP: 1000.0}},
        )

        assert events, "a store certain to sit at its cap reported no overflow"
        assert events[0].wasted_per_day == pytest.approx(24_000.0, abs=1.0)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "AUDIT: wasted_per_day is read off the last simulated day even when "
            "the simulation never settled, so a store that first clamps part-way "
            "through day 14 reports half its recurring loss."
        ),
    )
    def test_the_reported_loss_is_the_recurring_one_not_a_partial_first_day(self) -> None:
        from travian_api.services.distribution.schedule import Beat

        # Full on day 13.5, so day 14 clamps for only half of it.
        events = simulate_day(
            Beat(),
            {1: {Resource.CROP: 76_000}},
            {1: {Resource.CROP: 400_000}},
            {1: {Resource.CROP: 1000.0}},
        )

        assert events[0].net_gain_per_day == pytest.approx(24_000.0, abs=1.0)
        assert events[0].wasted_per_day == pytest.approx(24_000.0, abs=1.0)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "AUDIT: the plan depends on the integer village ids. Relabelling the "
            "villages of seed 49 -- five villages, nothing else changed -- gives a "
            "different route set. Measured over 16 accounts x 3 relabellings, 8 "
            "changed their merchant total, route count or feasibility, and seed 7 "
            "spanned 127-141 merchants (9.9%) for the same account. The objective's "
            "last key is an integer, so candidate swaps tie often and the tie is "
            "broken by the id-sorted scan order."
        ),
    )
    @pytest.mark.parametrize("seed", [49, 0, 3, 5, 8])
    def test_relabelling_does_not_change_the_plan(self, seed: int) -> None:
        account = random_account(seed, with_profiles=False)
        mapping = id_permutation(account.plan_request, seed + 1_000)

        original = asyncio.run(dist.post_plan(account.plan_request, USER))
        relabelled = asyncio.run(dist.post_plan(permute_ids(account.plan_request, mapping), USER))

        assert plan_signature(relabelled, mapping) == plan_signature(original)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "AUDIT: the same account relabelled costs a different number of "
            "merchants, so one labelling finds a cheaper plan than another and "
            "which one you get is an accident of the village ids."
        ),
    )
    def test_relabelling_does_not_change_what_the_plan_costs(self) -> None:
        account = random_account(7, with_profiles=False)
        original = asyncio.run(dist.post_plan(account.plan_request, USER))

        totals = {original.total_merchants}
        for k in range(3):
            mapping = id_permutation(account.plan_request, 70 + k)
            totals.add(
                asyncio.run(
                    dist.post_plan(permute_ids(account.plan_request, mapping), USER)
                ).total_merchants
            )

        assert len(totals) == 1, f"merchant total varies with the labelling: {sorted(totals)}"

    @pytest.mark.slow
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "AUDIT: the same 17-village account relabelled costs 687 merchants "
            "under one labelling and 709 under another, and one village flips "
            "between over budget and within it -- which is the number the Trade "
            "Office upgrade advice is built from."
        ),
    )
    def test_relabelling_does_not_change_who_is_over_budget(self) -> None:
        account = case_account(2)
        mapping = id_permutation(account.plan_request, 502)

        original = asyncio.run(dist.post_plan(account.plan_request, USER))
        relabelled = asyncio.run(dist.post_plan(permute_ids(account.plan_request, mapping), USER))

        back = {new: old for old, new in mapping.items()}
        assert {b.village_id for b in original.budgets if b.over_budget} == {
            back[b.village_id] for b in relabelled.budgets if b.over_budget
        }
        assert original.total_merchants == relabelled.total_merchants

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "AUDIT: AllocationInput.value is unbounded, so an ABSOLUTE target of "
            "-4,000/h is accepted. It means 'retain less than nothing', the sender "
            "is given a route it cannot fund, `unallocated` reads +6,000/h on an "
            "account that makes 2,000/h, and the plan is reported feasible."
        ),
    )
    def test_a_negative_absolute_target_is_not_silently_planned(self) -> None:
        account = next(
            a for a in adversarial_accounts() if a.name == "adv-negative-absolute-target"
        )

        result = asyncio.run(dist.post_plan(account.plan_request, USER))

        lumber = next(u for u in result.unallocated if u.resource is Resource.LUMBER)
        assert lumber.unallocated <= lumber.total_production
        assert not result.feasible


# ---------------------------------------------------------------------------
# Mutation matrix: break the planner on purpose and prove the guard above
# notices. A regression test nobody has ever seen fail is a regression test
# nobody knows anything about.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _drop_the_sender_surplus_cap():
    """A sender may ship the whole of a receiver's demand, surplus or not."""
    real = optimizer._flows_for_resource

    # Mirrors the real signature, including the per-destination supplier
    # exclusions the optimizer now honours: a stub that lags behind it fails
    # on arity and stops testing the mutation it exists to test.
    def patched(plan, villages, geometry, excluded=None):
        surplus = {v.village_id: -v.ship_per_hour for v in plan.senders if v.village_id in villages}
        demand = sorted(
            (v for v in plan.receivers if v.village_id in villages),
            key=lambda v: (-v.ship_per_hour, v.village_id),
        )
        flows: dict[tuple[int, int], float] = {}
        for receiver in demand:
            remaining = receiver.ship_per_hour
            for origin in sorted(
                (vid for vid in surplus if vid != receiver.village_id),
                key=lambda vid: (
                    geometry.distance(villages[vid].coords, villages[receiver.village_id].coords),
                    vid,
                ),
            ):
                if remaining <= 1e-6:
                    break
                key = (origin, receiver.village_id)
                flows[key] = flows.get(key, 0.0) + remaining
                surplus[origin] -= remaining
                remaining = 0.0
        return flows, []

    optimizer._flows_for_resource = patched
    try:
        yield
    finally:
        optimizer._flows_for_resource = real


@contextlib.contextmanager
def _inflate_the_merchant_budget():
    """Every village is credited with three times the merchants it has."""
    real = VillageState.spare_merchants
    VillageState.spare_merchants = lambda self, reserve=2: max(0, self.merchant_count * 3 - reserve)
    try:
        yield
    finally:
        VillageState.spare_merchants = real


@contextlib.contextmanager
def _reserve_one_set_too_few():
    """One fewer set of merchants than the round trip needs."""
    real_cheapest = optimizer.cheapest_cycle
    real_sweep = optimizer.cycle_sweep

    def shave(cost):
        return RouteCost(
            cycle_hours=cost.cycle_hours,
            batch=cost.batch,
            merchants_per_send=cost.merchants_per_send,
            sets_in_flight=max(1, cost.sets_in_flight - 1),
        )

    optimizer.cheapest_cycle = lambda *a, **k: shave(real_cheapest(*a, **k))
    optimizer.cycle_sweep = lambda *a, **k: [shave(c) for c in real_sweep(*a, **k)]
    try:
        yield
    finally:
        optimizer.cheapest_cycle = real_cheapest
        optimizer.cycle_sweep = real_sweep


@contextlib.contextmanager
def _let_a_receiver_exceed_its_capacity():
    """The discrete overflow replay reports nothing, whatever the cap."""
    real = dist.simulate_day
    dist.simulate_day = lambda *a, **k: ()
    try:
        yield
    finally:
        dist.simulate_day = real


@contextlib.contextmanager
def _credit_an_arrival_at_its_dispatch_minute():
    """Cargo lands the minute it leaves, so it is attributed to the wrong profile.

    Injected inside the production property rather than as a wrapper around the
    simulation call: a wrapper intercepts the very call any observer watches, so
    nothing outside can see it -- which is exactly how this fault survived the
    audit's blind review.
    """
    real = ScheduledRoute.arrival_minutes
    ScheduledRoute.arrival_minutes = property(lambda self: self.dispatch_minutes)
    try:
        yield
    finally:
        ScheduledRoute.arrival_minutes = real


class TestTheGuardsActuallyGuard:
    """Each guard must fail on its own mutation and on nothing else."""

    GUARDS = {
        "surplus": lambda: (
            TestPlanArithmetic().test_no_village_ships_more_than_it_makes_plus_what_arrives(0)
        ),
        "sets": lambda: TestPlanArithmetic().test_every_leg_reserves_one_set_per_round_trip(0),
        "budget": lambda: (
            TestPlanArithmetic().test_the_merchant_budget_is_the_villages_own_merchants(0)
        ),
        "overflow": lambda: (
            TestPlanArithmetic().test_a_store_that_fills_within_the_day_is_reported()
        ),
        "arrival": lambda: (
            TestPlanArithmetic().test_an_arrival_lands_after_the_trip_not_when_it_was_sent()
        ),
    }

    MUTATIONS = {
        "surplus": _drop_the_sender_surplus_cap,
        "sets": _reserve_one_set_too_few,
        "budget": _inflate_the_merchant_budget,
        "overflow": _let_a_receiver_exceed_its_capacity,
        "arrival": _credit_an_arrival_at_its_dispatch_minute,
    }

    @pytest.mark.slow
    @pytest.mark.parametrize("name", sorted(MUTATIONS))
    def test_the_matching_guard_catches_its_mutation(self, name: str) -> None:
        with self.MUTATIONS[name](), pytest.raises(AssertionError):
            self.GUARDS[name]()

    @pytest.mark.parametrize("name", sorted(GUARDS))
    def test_every_guard_passes_on_unmutated_code(self, name: str) -> None:
        self.GUARDS[name]()
