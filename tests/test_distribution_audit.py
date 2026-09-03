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
from fastapi import HTTPException

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
from travian_api.services.distribution import optimizer, schedule
from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.merchants import RouteCost
from travian_api.services.distribution.optimizer import VillageState
from travian_api.services.distribution.schedule import ScheduledRoute
from travian_api.services.distribution.storage import simulate_day, simulate_profile_cycle
from travian_api.web.routes import distribution as dist
from travian_api.web.routes.distribution import PlanResponse

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
    """The stocks, capacities, own rates and spend ``_storage_findings`` builds.

    Consumption comes back alongside them because the replays subtract it: a
    comparison that fed the oracle a spend the planner never saw (or the
    reverse) would fail for a reason that has nothing to do with the physics,
    and one that fed NEITHER would silently check only the zero case."""
    stocks: dict[int, dict[Resource, int]] = {}
    caps: dict[int, dict[Resource, int]] = {}
    own: dict[int, dict[Resource, float]] = {}
    spend = {
        cfg.village_id: dict(cfg.consumption_per_hour)
        for cfg in body.config
        if cfg.consumption_per_hour
    }
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
    return stocks, caps, own, spend


# `/plan` is "pure of game I/O" (see its docstring): same request in, same
# response out, always. Several audits below replan the exact same account --
# an unpermuted seed reused across test classes, mostly -- so memoise on the
# request's own JSON rather than repeat a multi-second solve for input this
# module already solved once.
#
# The key is the WHOLE request (`model_dump_json`), which is what makes a
# collision impossible rather than merely unlikely: two requests share an entry
# only if they are byte-identical, in which case sharing is correct. That
# argument is now the only one available. The comment here used to say a
# collision would "turn an xfail into an xpass and fail the suite", which was
# true while the relabelling tests asserted the plans DIFFER; they now assert
# the plans MATCH, so a collision would pass silently and the suite could not
# catch what the key has to guarantee.
_post_plan_cache: dict[str, PlanResponse] = {}


@contextlib.contextmanager
def _no_plan_cache():
    """Run a block with plan memoisation disabled, and leave nothing behind.

    Use this for ANY test that mutates global state -- a monkeypatched guard, a
    patched planner internal -- around a call that reaches the planner. Two
    distinct ways to get a FALSE PASS live here, and both were observed while
    the cache was being introduced:

    * a plan cached by an earlier, unmutated test satisfies the mutated call, so
      the mutation never runs and `pytest.raises` reports DID NOT RAISE;
    * the mutated call caches its own broken plan under that seed, which then
      leaks into a later unmutated reader and fails *that* test instead.

    Both failures are silent in the sense that matters: the suite still reports a
    result, just not one about the code under test. Hence a named contextmanager
    rather than a pair of bare clear() calls -- the next person writing a
    mutation test needs to be able to find this.
    """
    _post_plan_cache.clear()
    try:
        yield
    finally:
        _post_plan_cache.clear()


def _post_plan(request):
    key = request.model_dump_json()
    cached = _post_plan_cache.get(key)
    if cached is None:
        cached = asyncio.run(dist.post_plan(request, USER))
        _post_plan_cache[key] = cached
    return cached


class TestOracleAgreement:
    """Two independent implementations of the same physics must agree."""

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 7, 13])
    def test_simulate_day_matches_the_oracle(self, seed: int) -> None:
        account = random_account(seed, with_profiles=False)
        body = account.plan_request
        planned = asyncio.run(dist._plan_account(body))
        stocks, caps, own, spend = _storage_inputs(body)

        produced = simulate_day(
            planned.plan.beat, stocks, caps, own, step_minutes=1, consumption=spend
        )
        expected = oracle_day(planned.plan.beat, stocks, caps, own, consumption=spend)

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
    @pytest.mark.parametrize("seed", [0, 1, 2, 8])
    def test_simulate_profile_cycle_matches_the_oracle(self, seed: int) -> None:
        """The composite day: production always on, each profile's routes only
        inside its own hours, arrivals credited where they land.

        Seed 8 declares consumption where 0-2 do not, so the spend reaches this
        replay as well as ``simulate_day``'s. A parameter threaded into one
        simulation and not the other is how /plan and /day-check came to answer
        the same account differently once before, and the spy below reads what
        the endpoint actually passed rather than what this test assumes."""
        account = random_account(seed, with_profiles=True)
        captured: dict = {}
        real = dist.simulate_profile_cycle

        def spy(segments, own_rates, stocks, caps, ceilings=None, *args, **kwargs):
            captured["args"] = (segments, own_rates, stocks, caps, ceilings)
            captured["consumption"] = kwargs.get("consumption")
            return real(segments, own_rates, stocks, caps, ceilings, *args, **kwargs)

        dist.simulate_profile_cycle = spy
        try:
            asyncio.run(dist.post_day_check(account.day_request, USER))
        finally:
            dist.simulate_profile_cycle = real
        segments, own_rates, stocks, caps, ceilings = captured["args"]
        spend = captured["consumption"]
        declared = {c.village_id for c in account.day_request.config if c.consumption_per_hour}
        assert set(spend or {}) == declared, "the endpoint dropped a declared spend"

        trajectories, breaches = simulate_profile_cycle(
            segments,
            own_rates,
            stocks,
            caps,
            ceilings,
            step_minutes=1,
            max_days=ORACLE_DAYS,
            consumption=spend,
        )
        rows, oracle_breaches, settled = oracle_profile_cycle(
            segments, own_rates, stocks, caps, ceilings, max_days=ORACLE_DAYS, consumption=spend
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

        original = _post_plan(account.plan_request)
        relabelled = _post_plan(permute_ids(account.plan_request, mapping))

        assert plan_signature(relabelled, mapping) == plan_signature(original), (
            f"seed {seed}: the plan changed when only the village ids moved"
        )

    def test_relabelling_survives_a_tie_between_two_identical_suppliers(self) -> None:
        """Two suppliers identical in distance and surplus are broken apart by
        village id, so the CHOSEN supplier may swap under a relabelling -- but
        the shape of the plan must not."""
        account = next(a for a in adversarial_accounts() if a.name == "adv-tied-suppliers")
        mapping = id_permutation(account.plan_request, 77)

        original = _post_plan(account.plan_request)
        relabelled = _post_plan(permute_ids(account.plan_request, mapping))

        assert relabelled.total_merchants == original.total_merchants
        assert len(relabelled.rows) == len(original.rows)
        assert relabelled.feasible == original.feasible

    @pytest.mark.slow
    @pytest.mark.parametrize("index", [1, 3])
    def test_relabelling_permutes_a_mid_sized_account(self, index: int) -> None:
        account = case_account(index)
        mapping = id_permutation(account.plan_request, 500 + index)

        original = _post_plan(account.plan_request)
        relabelled = _post_plan(permute_ids(account.plan_request, mapping))

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
        result = _post_plan(account.plan_request)
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
        result = _post_plan(account.plan_request)

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

        result = _post_plan(account.plan_request)

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

        result = _post_plan(account.plan_request)

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

    def test_a_declared_material_relay_forwards_after_it_collects(self) -> None:
        """Collect-then-ship, for the resource profile section 5's tier moves.

        The beat's ordering machinery was crop-only in three places, and it
        could be: netting leaves every village a sender or a receiver of a
        material, so no material could produce a hub and no material forward leg
        could have an inbound to wait for. A DECLARED tier makes one real, and a
        forward leg placed with no regard for its inbound ships out of the
        relay's own warehouse while the collecting leg merely refills what just
        left -- the same defect the crop ordering exists to prevent, with a
        warehouse instead of a granary, and invisible at both ends of the tier.

        Measured on the real firing minutes rather than on the hub report,
        because the hub report is derived from the same beat and would agree with
        it whatever the beat did.

        Two assertions, because the established crop-side formulation is not
        sharp enough on its own here. "The worst wait is under one cycle" (which
        is how tests/test_distribution_planner.py states it, on 4h cycles) is
        satisfied by a beat that forwards 45 minutes into a 60-minute cycle --
        measured, that is exactly what this account's tier does with the
        ordering removed. So the second assertion is the physical one: a forward
        send must not leave while the batch it exists to carry is still IN THE
        AIR. Anything dispatched in that window is provably carrying the relay's
        own stock, which is the whole defect.
        """
        account = next(a for a in adversarial_accounts() if a.name == "adv-declared-material-relay")

        result = _post_plan(account.plan_request)

        rows = {(r.origin, r.destination): r for r in result.rows}
        relay = account.plan_request.snapshot[1].village_id
        source = account.plan_request.snapshot[0].village_id
        collect = rows.get((source, relay))
        assert collect is not None, f"no collecting leg was built: {sorted(rows)}"
        forwards = [row for (origin, _d), row in rows.items() if origin == relay]
        assert forwards, f"no forward leg was built: {sorted(rows)}"

        def firings(row, arrival):
            step = row.cycle_hours * 60
            clock = row.arrival if arrival else row.dispatch
            hours, minutes = clock.split(":")
            start = int(hours) * 60 + int(minutes)
            return [(start + offset) % 1440 for offset in range(0, 1440, step)]

        departures = firings(collect, arrival=False)
        arrivals = firings(collect, arrival=True)
        # The sheet states both clocks and not the trip, so the trip is their
        # difference -- which is also the only figure an operator reading the
        # sheet has, so it is the right one to hold the plan to.
        travel = (arrivals[0] - departures[0]) % 1440
        assert travel > 0, "the collecting leg reached the sheet with no travel time"
        for row in forwards:
            sends = firings(row, arrival=False)
            worst = max(min((d - a) % 1440 for a in arrivals) for d in sends)
            assert worst < row.cycle_hours * 60, (
                f"relay {relay} waits {worst} min after collecting before forwarding to "
                f"{row.destination}, which is a whole {row.cycle_hours}h cycle -- it is "
                f"shipping from its own warehouse"
            )
            in_flight = [
                send for send in sends if any((send - out) % 1440 < travel for out in departures)
            ]
            assert not in_flight, (
                f"relay {relay} forwards to {row.destination} at {sorted(in_flight)}, while "
                f"the batch it is meant to carry is still {travel} min in the air from "
                f"{sorted(departures)[:4]} -- so it is shipping its own warehouse and the "
                f"collecting leg only refills what just left"
            )

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
    """Bugs this audit found, each now a passing regression guard.

    They were `xfail(strict=True)` on the behaviour that is right, so that the
    day one was fixed the test said so instead of staying quiet. Every one of
    them has since been fixed and every marker is gone; the per-test docstring
    records what was wrong and when it was resolved, which is the part worth
    keeping -- a guard whose subject nobody remembers is the first one somebody
    deletes as redundant.
    """

    def test_a_crop_neutral_midway_village_can_act_as_a_relay_hub(self) -> None:
        """Resolved 2026-09-02. _relay_scan drew hub candidates from the crop flow
        graph alone, so a crop-neutral midway village -- the canonical hub of
        profile section 8.5 -- could never be chosen, while the same village given
        a 100/h flow was found at once (the test below). Candidates now come from
        every village the operator put in the crop plan -- an allocation, even one
        that leaves no flow, which is what this midway village has -- and the
        midway village is chosen on its geometry. (Between 2026-09-02 and
        2026-09-03 the set was every village of the account, which conscripted
        villages nobody had allocated crop; see `relay_hub_candidates`.)"""
        account = next(a for a in adversarial_accounts() if a.name == "adv-relay-shape")

        result = _post_plan(account.plan_request)

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

        result = _post_plan(account.plan_request.model_copy(update={"allocations": allocations}))

        senders = {r.origin for r in result.rows if Resource.CROP in r.cargo}
        receivers = {r.destination for r in result.rows if Resource.CROP in r.cargo}
        assert hub in senders & receivers

    def test_a_store_that_reaches_its_cap_after_the_settling_horizon_is_reported(self) -> None:
        """Resolved 2026-09-02. simulate_day gave up after MAX_SETTLING_DAYS=14
        and reported whatever the 14th day happened to waste, so a store that
        reaches its cap on day 15 was reported as losing nothing at all. It now
        records whether the horizon settled and, when it did not, reports a
        capped store still gaining at its net gain per day -- the loss it will
        shed once it sits at the cap -- as a projected overflow."""
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

    def test_the_reported_loss_is_the_recurring_one_not_a_partial_first_day(self) -> None:
        """Resolved 2026-09-02. wasted_per_day was read off the last simulated
        day even when the simulation never settled, so a store that first clamps
        part-way through day 14 reported half its recurring loss. On an
        unsettled horizon a store whose last-day clamping falls short of its net
        gain is now reported at the net gain, which is the recurring figure."""
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

    @pytest.mark.parametrize("seed", [55, 0, 3, 5, 6, 8, 9, 29])
    def test_relabelling_does_not_change_the_plan(self, seed: int) -> None:
        """RESOLVED 2026-09-02. Renumber the villages and the plan is the same.

        The plan used to depend on the integer ids: 16 of 30 seeds sampled gave a
        different route set under relabelling, and seed 7 spanned 127-141
        merchants (9.9%) for one account. Every tie-break that fell back to an id
        is now geographic -- the greedy seed's receiver order and supplier order,
        and the relay scan's hub, origin and destination keys -- and the relay
        scan commits the best (leg, hub) pair across the whole scan rather than
        the first leg in id order that had an improving hub. 29 of 30 seeds are
        now invariant; seed 29 joins the parametrisation as one that used to
        differ. For the thirtieth see the co-located test below.

        Seeds 0 and 8 were dropped from the list on 2026-09-02 and are back:
        both still hold (verified 2026-09-03, 25 and 15 villages), and a test
        whose whole subject is WHICH seeds hold must not quietly shrink its
        sample. Two more plans cost ~4s here, most of it shared with the seed-0
        account other tests in this module already plan.
        """
        account = random_account(seed, with_profiles=False)
        mapping = id_permutation(account.plan_request, seed + 1_000)

        original = _post_plan(account.plan_request)
        relabelled = _post_plan(permute_ids(account.plan_request, mapping))

        assert plan_signature(relabelled, mapping) == plan_signature(original)

    def test_two_villages_on_one_tile_are_separable_only_by_id(self) -> None:
        """The one limit of the geographic tie-breaks, stated rather than hidden.

        Seed 20 puts two villages on tile (-8, -40). Coordinates cannot order
        what shares them, so which of the two serves a given demand is decided by
        the id and relabelling can still swap them: 98 routes against 97, the
        same work split differently between the pair. The cost is invariant here
        -- 322 merchants either way -- ON THE SEEDS SAMPLED, and not by
        construction: two villages on one tile are interchangeable in DISTANCE
        and nothing else, so a different surplus, Trade Office or merchant count
        makes the pair a real choice. Seed 37 is the counter-example, in the test
        below.

        This cannot happen on a real account: Travian permits one village per
        tile, so coordinates are a total order there and the property above holds
        outright. Asserted as the boundary of the fix, so that a future key
        claiming to remove it has to face this case.
        """
        account = random_account(20, with_profiles=False)
        coords = [(v.x, v.y) for v in account.plan_request.snapshot]
        assert len(coords) != len(set(coords)), (
            "seed 20 no longer has two villages on one tile, so it no longer "
            "demonstrates the limit -- find another seed that does, or delete this"
        )

        mapping = id_permutation(account.plan_request, 1020)
        original = _post_plan(account.plan_request)
        relabelled = _post_plan(permute_ids(account.plan_request, mapping))

        assert original.total_merchants == relabelled.total_merchants, (
            "co-located villages are interchangeable, so the plan's COST must not "
            "depend on which of them was picked"
        )

    def test_co_located_villages_can_move_the_cost_as_well_as_the_split(self) -> None:
        """The other half of the limit above, which the seed-20 case understated.

        Seed 20's pair happens to be interchangeable in cost, and reading that
        as "co-located villages are always interchangeable" is wrong: sharing a
        tile makes two villages identical in DISTANCE and in nothing else.
        Seed 37 puts V25 and V37 on tile (1|4) with different shapes, and the
        merchant total moves 206/207 across labellings (measured 2026-09-03:
        207 on one of six permutations). Moving one of them off the tile
        restores invariance outright -- 211 on all seven labellings -- which is
        what pins the tile, and not the ids, as the cause.

        Asserted as a RANGE rather than as equality: this is the documented
        boundary of the geographic tie-breaks, so the test must fail if the
        spread grows, not if it exists.
        """
        account = random_account(37, with_profiles=False)
        coords = [(v.x, v.y) for v in account.plan_request.snapshot]
        assert len(coords) != len(set(coords)), (
            "seed 37 no longer has two villages on one tile, so it no longer "
            "demonstrates the limit -- find another seed that does, or delete this"
        )

        totals = {_post_plan(account.plan_request).total_merchants}
        for k in range(6):
            mapping = id_permutation(account.plan_request, 3_700 + k)
            totals.add(_post_plan(permute_ids(account.plan_request, mapping)).total_merchants)

        assert max(totals) - min(totals) <= 1, (
            f"the cost spread across labellings grew past the one merchant a "
            f"co-located pair explains: {sorted(totals)}"
        )

    def test_relabelling_does_not_change_what_the_plan_costs(self) -> None:
        """RESOLVED 2026-09-02 with the geographic tie-breaks. Seed 7 used to span
        127-141 merchants (9.9%) across labellings of one account; no seed
        sampled now moves at all."""
        account = random_account(7, with_profiles=False)
        original = _post_plan(account.plan_request)

        totals = {original.total_merchants}
        for k in range(3):
            mapping = id_permutation(account.plan_request, 70 + k)
            totals.add(_post_plan(permute_ids(account.plan_request, mapping)).total_merchants)

        assert len(totals) == 1, f"merchant total varies with the labelling: {sorted(totals)}"

    @pytest.mark.slow
    def test_relabelling_does_not_change_who_is_over_budget(self) -> None:
        """RESOLVED 2026-09-02. Case 1 at permutation 500 cost 459 merchants under
        one labelling and 457 under another, and a village flipped between over
        budget and within it -- the number the Trade Office upgrade advice is
        built from. Both halves are now invariant on every case account across
        seven permutations."""
        account = case_account(1)
        mapping = id_permutation(account.plan_request, 500)

        original = _post_plan(account.plan_request)
        relabelled = _post_plan(permute_ids(account.plan_request, mapping))

        back = {new: old for old, new in mapping.items()}
        assert {b.village_id for b in original.budgets if b.over_budget} == {
            back[b.village_id] for b in relabelled.budgets if b.over_budget
        }
        assert original.total_merchants == relabelled.total_merchants

    def test_a_negative_absolute_target_is_not_silently_planned(self) -> None:
        """An ABSOLUTE target of -4,000/h means 'retain less than nothing'. It
        used to be accepted: the sender was given a route it cannot fund,
        `unallocated` read +6,000/h on an account that makes 2,000/h, and the
        plan was reported feasible. Resolved 2026-09-02 by refusing a negative
        absolute retention in ``Allocation.__post_init__``, which /plan
        translates into a 400 that names the value."""
        account = next(
            a for a in adversarial_accounts() if a.name == "adv-negative-absolute-target"
        )

        with pytest.raises(HTTPException) as excinfo:
            _post_plan(account.plan_request)

        assert excinfo.value.status_code == 400
        assert "-4000" in excinfo.value.detail


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
    # exclusions the optimizer now honours and the names it labels a shortfall
    # with: a stub that lags behind it fails on arity and stops testing the
    # mutation it exists to test.
    def patched(plan, villages, geometry, *, names=None, excluded=None):
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
def _order_the_beat_for_crop_only():
    """Collect-then-ship goes back to being crop-only, as it was before the tier.

    The exact regression profile section 5's tier introduces the risk of: a
    declared material relay's forward leg is then phased with no regard for its
    inbound, so it ships out of the relay's own warehouse and the collecting leg
    refills what just left. Injected by narrowing the resource set the beat
    orders on, which is the one line that made it general.
    """
    real = schedule.Resource

    class _CropOnly:
        """Stands in for the Resource enum, offering only crop to iterate.

        The beat derives its hub sets by iterating `Resource`, so a stand-in
        that yields crop alone reproduces the pre-tier code exactly without
        touching anything else -- including the attribute access
        (`Resource.CROP`) the same function makes.
        """

        CROP = real.CROP

        def __iter__(self):
            return iter((real.CROP,))

    schedule.Resource = _CropOnly()
    try:
        yield
    finally:
        schedule.Resource = real


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
        "relay_order": lambda: (
            TestPlanArithmetic().test_a_declared_material_relay_forwards_after_it_collects()
        ),
    }

    MUTATIONS = {
        "surplus": _drop_the_sender_surplus_cap,
        "sets": _reserve_one_set_too_few,
        "budget": _inflate_the_merchant_budget,
        "overflow": _let_a_receiver_exceed_its_capacity,
        "arrival": _credit_an_arrival_at_its_dispatch_minute,
        "relay_order": _order_the_beat_for_crop_only,
    }

    @pytest.mark.slow
    @pytest.mark.parametrize("name", sorted(MUTATIONS))
    def test_the_matching_guard_catches_its_mutation(self, name: str) -> None:
        # A GUARD is a real test method, and several of those are memoised by
        # seed (see `_post_plan`). Clear before: a plan cached from an earlier,
        # unmutated call would make this pass without the mutation ever running.
        # Clear after: the mutated run may itself cache a broken plan under that
        # same seed, which would then leak into a later, unmutated reader.
        with _no_plan_cache(), self.MUTATIONS[name](), pytest.raises(AssertionError):
            self.GUARDS[name]()

    @pytest.mark.parametrize("name", sorted(GUARDS))
    def test_every_guard_passes_on_unmutated_code(self, name: str) -> None:
        self.GUARDS[name]()
