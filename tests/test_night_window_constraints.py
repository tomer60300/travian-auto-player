"""Section 6: the night is a closed window, and the morning starts full.

Four rules the operator settled, and each of them was previously a note in a
docstring rather than something the planner could fail:

* The pre-night baseline is **25%** and the morning target **60%** -- not the
  30/80 pair the derivation defaulted to while the question was open.
* **All night movements complete before 07:00.** No merchant underway or
  returning at the switch, so the morning profile starts with a full pool
  everywhere. A route that cannot manage it is refused by name with its
  overrun, never quietly trimmed to fit.
* Because of that, the standing **2-hour latency target does not apply inside
  the night window.** Night cycle length is bounded by getting home and by not
  overflowing, not by how fresh a delivery is while nobody is spending it.
* At 07:00 every role village -- DEF and both OFF roles, the capital excluded
  -- must be at **60% on both stores**. A plan that lands one below it says so
  with the measured percentage.

The pre-night 25% is the one rule that is NOT enforced: the operator spends the
stores down by hand at the switch, so the planner treats it as a trusted
starting condition and reports a snapshot that disagrees rather than refusing.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
)
from travian_api.services.distribution.findings import Category, Severity
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import EUROPE2_TEUTON
from travian_api.services.distribution.night_profile import (
    DEFAULT_BASELINE_FILL,
    DEFAULT_TARGET_FILL,
    MORNING_MINUTE,
    NIGHT_WINDOW,
    NightVillage,
    derive_night_profile,
    is_night_window,
)
from travian_api.services.distribution.optimizer import Route, VillageState
from travian_api.services.distribution.planner import PlannerConfig, craft_plan
from travian_api.services.distribution.roles import Role, keeps_a_morning_floor
from travian_api.services.distribution.schedule import (
    ScheduledRoute,
    build_beat,
    night_overrun_minutes,
)
from travian_api.services.distribution.storage import (
    ProfileSegment,
    morning_floor_shortfalls,
    night_state_findings,
    pre_night_overfills,
    simulate_profile_cycle,
)
from travian_api.web.routes.distribution import DayCheckRequest, post_day_check

USER = SimpleNamespace(id=1)
DAY_WINDOW = (7 * 60, 23 * 60)
HUB, ARMY, FAR = 1, 2, 3


# ── The fill fractions ───────────────────────────────────────────────────────


class TestTheFillFractionsAreTwentyFiveAndSixty:
    """Settled 2026-09-03. The register carried 25/60 vs 30/80 as open for days
    and the code kept the pair that was never chosen."""

    def test_the_pre_night_baseline_is_a_quarter(self):
        assert DEFAULT_BASELINE_FILL == 0.25

    def test_the_morning_target_is_three_fifths(self):
        assert DEFAULT_TARGET_FILL == 0.60

    def test_the_ceiling_is_the_room_between_them(self):
        """A receiver is given what it can still hold, which IS the ceiling:
        (0.60 - 0.25) x 160,000 over 8 hours is 7,000/h. Under 30/80 the same
        village was handed 10,000/h, and the extra 3,000 an hour is 24,000 the
        night ships into a store with no room for it."""
        village = NightVillage(
            village_id=ARMY,
            name="03",
            x=1,
            y=0,
            merchants_total=20,
            trade_office_level=0,
            warehouse_capacity=160_000,
            granary_capacity=160_000,
            production={Resource.LUMBER: 0.0, Resource.CROP: 0.0},
        )
        hub = NightVillage(
            village_id=HUB,
            name="02",
            x=0,
            y=0,
            merchants_total=20,
            trade_office_level=0,
            warehouse_capacity=1_200_000,
            granary_capacity=800_000,
            production={Resource.LUMBER: 40_000.0, Resource.CROP: 0.0},
        )

        profile = derive_night_profile(
            [hub, village],
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            # Far above anything the store can hold, so the answer is the
            # ceiling and nothing else.
            day_retention={Resource.LUMBER: {ARMY: 999_999.0}},
            hub_id=HUB,
        )

        assert profile.allocations[Resource.LUMBER][ARMY].value == 7_000.0


# ── The night window, and what identifies it ─────────────────────────────────


class TestWhichProfileIsTheNight:
    def test_the_nights_hours_are_23_to_07(self):
        assert NIGHT_WINDOW == (23 * 60, 7 * 60)
        assert MORNING_MINUTE == 7 * 60

    def test_a_window_that_wraps_past_midnight_is_the_night(self):
        assert is_night_window(NIGHT_WINDOW)
        assert is_night_window((22 * 60, 6 * 60))

    def test_the_day_and_the_round_the_clock_set_are_not(self):
        assert not is_night_window(DAY_WINDOW)
        assert not is_night_window(None)


# ── The wrap is a default; the profile's own declaration wins ────────────────

# The night typed as two profiles either side of midnight. Minute 1440 does not
# exist (`DaySegmentInput._window_in_day` requires 0-1439), so "23:00 to
# midnight" is `(1380, 0)` -- which DOES wrap -- and it is the SECOND half, the
# one that actually runs up to the 07:00 switch, that wraps in neither
# direction and so reads as a day profile.
NIGHT_BEFORE_MIDNIGHT = (23 * 60, 0)
NIGHT_AFTER_MIDNIGHT = (0, 7 * 60)
# A day profile that runs almost the whole day. It wraps, and nothing about it
# is overnight.
NEARLY_ALL_DAY = (7 * 60, 7 * 60 - 1)


class TestTheDeclarationBeatsTheWrap:
    """`window[0] > window[1]` is neither necessary nor sufficient.

    Which profile the operator sleeps through is a fact about the operator, not
    about the clock -- the same reason `npc_attended` is declared per profile
    rather than inferred from its hours. Both misreadings below are legal
    input, so the declaration has to be able to overrule the derivation in both
    directions.
    """

    def test_the_half_of_a_split_night_after_midnight_wraps_in_neither_direction(self):
        assert is_night_window(NIGHT_BEFORE_MIDNIGHT), "23:00-00:00 is typed (1380, 0), which wraps"
        assert not is_night_window(NIGHT_AFTER_MIDNIGHT), (
            "and 00:00-07:00 does not, though it is the half with the deadline"
        )
        assert is_night_window(NIGHT_AFTER_MIDNIGHT, overnight=True)

    def test_a_near_24h_day_profile_wraps_and_is_not_the_night(self):
        assert is_night_window(NEARLY_ALL_DAY), "07:00-06:59 wraps"
        assert not is_night_window(NEARLY_ALL_DAY, overnight=False)

    def test_a_declaration_overrides_the_derivation_in_both_directions(self):
        assert not is_night_window(NIGHT_WINDOW, overnight=False)
        assert is_night_window(DAY_WINDOW, overnight=True)

    def test_a_round_the_clock_set_has_no_switch_to_declare_one_for(self):
        """Section 6's deadline is measured against a window's END, and a set
        with no window has none. The declaration cannot invent one."""
        assert not is_night_window(None, overnight=True)


# ── Everything home before 07:00 ─────────────────────────────────────────────


def _leg(one_way: float, cycle: int = 24, origin: int = 2, destination: int = 1) -> Route:
    return Route(
        origin=origin,
        destination=destination,
        cargo_per_hour={Resource.CROP: 1_000.0},
        cycle_hours=cycle,
        merchants_per_send=1,
        sets_in_flight=1,
        one_way_minutes=one_way,
    )


def _overruns(beat):
    return [f for f in beat.findings if f.category is Category.NIGHT_OVERRUN]


class TestEveryNightMovementFinishesBeforeDawn:
    """`last_dispatch + round_trip <= 07:00`, derived from the beat.

    The night window is 480 minutes, so a 24h route -- one firing -- has room
    for a round trip of exactly 480 minutes and not a minute more.
    """

    def test_a_round_trip_that_lands_exactly_at_dawn_is_accepted(self):
        # 240 min out, 240 min back: leaves at 23:00, home at 07:00 sharp.
        beat = build_beat((_leg(240.0),), dispatch_window=NIGHT_WINDOW)

        (placed,) = beat.routes
        assert placed.dispatch_minute == 23 * 60, (
            "the only phase that fits is the window's first minute, and the beat "
            "must choose it rather than merely tolerate it"
        )
        assert night_overrun_minutes(placed, NIGHT_WINDOW) == pytest.approx(0.0)
        assert not _overruns(beat), (
            f"a round trip that just fits is not an overrun: {beat.warnings}"
        )

    def test_a_round_trip_one_minute_too_long_is_refused(self):
        beat = build_beat((_leg(240.5),), dispatch_window=NIGHT_WINDOW)

        assert _overruns(beat), f"481 min of round trip does not fit 480: {beat.warnings}"

    def test_the_refusal_names_the_route_and_the_overrun_in_minutes(self):
        beat = build_beat((_leg(245.0),), dispatch_window=NIGHT_WINDOW)

        (finding,) = _overruns(beat)
        assert finding.severity is Severity.CRITICAL
        assert "10 min" in finding.message, finding.message
        assert "2" in finding.detail and "1" in finding.detail
        # And what would fix it, so the operator is not left holding a number.
        assert "cycle" in finding.action
        assert "nearer" in finding.action

    def test_nothing_is_trimmed_to_make_it_fit(self):
        """Refusing beats under-delivering. A 4h cycle fires twice in the night
        and the plan sized the cargo for two firings, so dropping one to get the
        merchants home would silently halve the delivery."""
        beat = build_beat((_leg(200.0, cycle=4),), dispatch_window=NIGHT_WINDOW)

        (placed,) = beat.routes
        inside = [m for m in placed.dispatch_minutes if m >= 23 * 60 or m < 7 * 60]
        assert len(inside) == 2, "both in-window firings must survive"
        assert _overruns(beat), "and the overrun they cause must be reported instead"

    def test_the_beat_phases_the_route_to_get_home_where_it_can(self):
        """Reshaped, not merely reported. A reserved window covering every
        arrival that fits pulls the placement late unless getting home outranks
        keeping the NPC slot clear -- measured as a route phased to 06:00 and
        home at 08:00, for the sake of an arrival slot."""
        beat = build_beat(
            (_leg(60.0),),
            dispatch_window=NIGHT_WINDOW,
            # Covers 00:00-06:00, i.e. every arrival a fitting phase produces.
            reserved_window=(0, 6 * 60),
        )

        (placed,) = beat.routes
        assert not _overruns(beat), (
            f"the route is placed at {placed.dispatch_minute} and does not get home: "
            f"{beat.warnings}"
        )

    def test_the_day_profile_keeps_no_such_deadline(self):
        """Nothing says a merchant may not be underway at 23:00. The rule is the
        night's, and applying it to the day would refuse most of the sheet."""
        beat = build_beat((_leg(245.0),), dispatch_window=DAY_WINDOW)

        assert not _overruns(beat), f"the day has no completion rule: {beat.warnings}"

    def test_a_round_the_clock_set_keeps_no_such_deadline(self):
        beat = build_beat((_leg(245.0),))

        assert not _overruns(beat)


class TestTheDeclarationDecidesWhereTheDeadlineBinds:
    """A 490-minute round trip cannot fit the 420 minutes between midnight and
    07:00, so the merchants are still on the road when the morning profile
    starts believing the pool is whole."""

    def test_the_half_of_a_split_night_after_midnight_must_still_get_home(self):
        derived = build_beat((_leg(245.0),), dispatch_window=NIGHT_AFTER_MIDNIGHT)
        assert not _overruns(derived), (
            "the wrap alone reads 00:00-07:00 as a day profile, which has no deadline"
        )

        declared = build_beat((_leg(245.0),), dispatch_window=NIGHT_AFTER_MIDNIGHT, overnight=True)
        assert _overruns(declared), f"490 min of round trip does not fit 420: {declared.warnings}"

    def test_a_declared_day_profile_carries_no_deadline_however_it_is_typed(self):
        beat = build_beat((_leg(245.0),), dispatch_window=NIGHT_WINDOW, overnight=False)

        assert not _overruns(beat), f"the day has no completion rule: {beat.warnings}"


# ── The latency target is a day rule ─────────────────────────────────────────


def _two_villages():
    villages = {
        1: VillageState(village_id=1, x=0, y=0, merchant_count=20, trade_office_level=0),
        2: VillageState(village_id=2, x=60, y=0, merchant_count=20, trade_office_level=0),
    }
    productions = {Resource.LUMBER: {1: 4_000.0, 2: 0.0}}
    allocations = {
        Resource.LUMBER: {
            1: Allocation(AllocationMode.ABSOLUTE, 0.0),
            2: Allocation(AllocationMode.REMAINDER),
        }
    }
    return villages, productions, allocations


def _config(window, overnight=None):
    return PlannerConfig(
        geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
        merchant_model=EUROPE2_TEUTON,
        dispatch_window=window,
        overnight=overnight,
    )


class TestTheLatencyTargetDoesNotBindAtNight:
    """60 fields at 12 fields/h is five hours one way, so every cycle misses a
    2h target. By day that is worth saying; overnight nothing is being spent,
    so freshness costs nothing and the target is simply the wrong rule."""

    def test_it_still_binds_in_the_day_window(self):
        villages, productions, allocations = _two_villages()

        plan = craft_plan(villages, productions, allocations, _config(DAY_WINDOW))

        assert [f for f in plan.findings if f.category is Category.LATENCY], (
            f"a 5h haul misses a 2h target and the day must report it: {plan.warnings}"
        )

    def test_it_does_not_bind_in_the_night_window(self):
        villages, productions, allocations = _two_villages()

        plan = craft_plan(villages, productions, allocations, _config(NIGHT_WINDOW))

        assert not [f for f in plan.findings if f.category is Category.LATENCY], (
            f"section 6 suspends the latency rule overnight: {plan.warnings}"
        )

    def test_it_still_binds_round_the_clock(self):
        villages, productions, allocations = _two_villages()

        plan = craft_plan(villages, productions, allocations, _config(None))

        assert [f for f in plan.findings if f.category is Category.LATENCY]


def _latency(config):
    villages, productions, allocations = _two_villages()
    plan = craft_plan(villages, productions, allocations, config)
    return [f for f in plan.findings if f.category is Category.LATENCY], plan.warnings


class TestTheDeclarationDecidesWhereTheLatencyTargetBinds:
    """Both misreadings put the target on the wrong profile, and each costs.

    Suspended where it should bind, a 23h59 day profile ships six-hour-old
    deliveries into villages that are spending; left binding where it should
    not, the night buys merchants for freshness nobody is waiting for -- and
    those merchants are exactly the ones section 6 needs home by 07:00.
    """

    def test_a_near_24h_day_profile_keeps_the_target_when_it_says_it_is_the_day(self):
        suspended, _ = _latency(_config(NEARLY_ALL_DAY))
        assert not suspended, "the wrap alone reads 07:00-06:59 as the night"

        binding, warnings = _latency(_config(NEARLY_ALL_DAY, overnight=False))
        assert binding, f"a declared day profile keeps the 2h target: {warnings}"

    def test_the_half_of_a_split_night_after_midnight_suspends_it(self):
        binding, _ = _latency(_config(NIGHT_AFTER_MIDNIGHT))
        assert binding, "the wrap alone reads 00:00-07:00 as a day profile"

        suspended, warnings = _latency(_config(NIGHT_AFTER_MIDNIGHT, overnight=True))
        assert not suspended, f"a declared night profile suspends it: {warnings}"


# ── The morning floor, on both stores, for the role villages only ────────────


def _still_life(stocks, capacities, floor_villages=(HUB, ARMY, FAR)):
    """A day where nothing moves, so 07:00 holds exactly what was seeded.

    Zero production and no routes: the point under test is which stores are
    measured at the switch and against what, not the simulation's arithmetic.
    """
    night = ProfileSegment(name="Night", start_minute=23 * 60, end_minute=7 * 60)
    day = ProfileSegment(name="Day", start_minute=7 * 60, end_minute=23 * 60)
    trajectories, _ = simulate_profile_cycle(
        [night, day],
        own_rates={vid: {resource: 0.0 for resource in Resource} for vid in stocks},
        stocks=stocks,
        capacities=capacities,
    )
    return trajectories, morning_floor_shortfalls(
        trajectories, capacities, floor_villages, morning_profile="Day"
    )


def _stores(*, crop, material, capacity=100_000):
    return (
        {
            Resource.CROP: int(crop * capacity),
            Resource.LUMBER: int(material * capacity),
            Resource.CLAY: int(material * capacity),
            Resource.IRON: int(material * capacity),
        },
        {resource: capacity for resource in Resource},
    )


class TestTheMorningFloorIsOnBothStores:
    def test_a_granary_below_the_floor_is_reported(self):
        stocks, caps = _stores(crop=0.10, material=0.90)
        _, short = _still_life({ARMY: stocks}, {ARMY: caps})

        assert {s.resource for s in short} == {Resource.CROP}
        (crop,) = short
        assert crop.fraction == pytest.approx(0.10)

    def test_a_warehouse_below_the_floor_is_reported(self):
        stocks, caps = _stores(crop=0.90, material=0.10)
        _, short = _still_life({ARMY: stocks}, {ARMY: caps})

        assert {s.resource for s in short} == {Resource.LUMBER, Resource.CLAY, Resource.IRON}

    def test_both_stores_below_the_floor_report_both(self):
        stocks, caps = _stores(crop=0.10, material=0.10)
        _, short = _still_life({ARMY: stocks}, {ARMY: caps})

        assert {s.resource for s in short} == set(Resource)

    def test_a_village_at_the_floor_exactly_is_not_reported(self):
        stocks, caps = _stores(crop=0.60, material=0.60)
        _, short = _still_life({ARMY: stocks}, {ARMY: caps})

        assert not short, "60% IS the floor, and a floor is met by standing on it"

    def test_the_finding_carries_the_measured_percentage(self):
        stocks, caps = _stores(crop=0.10, material=0.90)
        _, short = _still_life({ARMY: stocks}, {ARMY: caps})

        findings = night_state_findings(short, (), names={ARMY: "03"})

        (finding,) = findings
        assert finding.category is Category.MORNING_FLOOR
        assert "10%" in finding.message, finding.message
        assert "60%" in finding.message
        assert "granary" in finding.message
        assert finding.village == "03"


class TestOnlyRoleVillagesHoldTheFloor:
    def test_the_def_and_off_roles_hold_it(self):
        assert keeps_a_morning_floor(Role.DEF)
        assert keeps_a_morning_floor(Role.TROOPS_OFF)
        assert keeps_a_morning_floor(Role.FULL_OFF)

    def test_the_capital_is_exempt(self):
        """Section 6 names DEF and OFF and excludes the capital by name: it is
        the storage and NPC hub, so its stores are drawn down deliberately."""
        assert not keeps_a_morning_floor(Role.CAPITAL)

    def test_a_feeder_is_exempt(self):
        assert not keeps_a_morning_floor(Role.FEEDER)

    def test_an_exempt_village_below_the_floor_produces_nothing(self):
        stocks, caps = _stores(crop=0.05, material=0.05)
        _, short = _still_life({HUB: stocks}, {HUB: caps}, floor_villages=())

        assert not short


# ── No overflow, either direction, at any point in the night ─────────────────


class TestNoOverflowEitherDirectionDuringTheNight:
    """The rule needs no new simulator: `simulate_profile_cycle` already
    replays the beat against real capacity and names the profile that was
    running, in both directions -- a store hitting its cap and a store running
    dry. These pin that the composite really answers the night's question, so
    nobody writes a second replay to ask it again.

    Worth knowing where this does NOT hold: `simulate_day`, which `/plan` uses,
    reports only waste that survives to a SETTLED day, by its own documented
    contract. A batch that overflows on the first night and never again is
    invisible there while the composite reports it at 01:00 on day 0. That gap
    is real and is queued separately (P17); nothing here changes that contract.
    """

    @staticmethod
    def _landing(crop_per_hour, dispatch_minute):
        return ScheduledRoute(
            route=Route(
                origin=FAR,
                destination=ARMY,
                cargo_per_hour={Resource.CROP: crop_per_hour},
                cycle_hours=24,
                merchants_per_send=1,
                sets_in_flight=1,
                one_way_minutes=0.0,
            ),
            dispatch_minute=dispatch_minute,
        )

    def test_a_batch_that_overflows_at_01_00_is_reported_against_the_night(self):
        # 60,000 lands at 01:00 on a granary holding 90,000 of 100,000. The day
        # profile spends it back down, so this overflows once and never again --
        # a transient, and the night is exactly when it happens.
        night = ProfileSegment(
            name="Night",
            start_minute=23 * 60,
            end_minute=7 * 60,
            routes=(self._landing(60_000 / 24, 60),),
        )
        day = ProfileSegment(name="Day", start_minute=7 * 60, end_minute=23 * 60)

        _, breaches = simulate_profile_cycle(
            [night, day],
            own_rates={ARMY: {Resource.CROP: -2_500.0}, FAR: {Resource.CROP: 0.0}},
            stocks={ARMY: {Resource.CROP: 90_000}, FAR: {Resource.CROP: 1_000_000}},
            capacities={ARMY: {Resource.CROP: 100_000}, FAR: {Resource.CROP: 2_000_000}},
        )

        overflow = [b for b in breaches if b.kind == "capacity" and b.village_id == ARMY]
        assert overflow, f"10,000 crop is destroyed at 01:00: {breaches}"
        assert overflow[0].segment == "Night"
        assert overflow[0].minute == 60

    def test_a_granary_emptying_overnight_is_reported_against_the_night(self):
        # The other direction, which the operator named in the same breath.
        # Nothing arrives and the army eats 5,000/h into a 20,000 stock, so it
        # is dry four hours into the night.
        night = ProfileSegment(name="Night", start_minute=23 * 60, end_minute=7 * 60)
        day = ProfileSegment(name="Day", start_minute=7 * 60, end_minute=23 * 60)

        _, breaches = simulate_profile_cycle(
            [night, day],
            own_rates={ARMY: {Resource.CROP: -5_000.0}},
            stocks={ARMY: {Resource.CROP: 20_000}},
            capacities={ARMY: {Resource.CROP: 100_000}},
        )

        dry = [b for b in breaches if b.kind == "empty"]
        assert dry, f"a granary at 20,000 losing 5,000/h does not last the night: {breaches}"
        assert dry[0].segment == "Night"


# ── The pre-night 25% is an assumption, not a gate ───────────────────────────


class TestThePreNightBaselineIsTrusted:
    def test_a_role_village_above_the_baseline_at_the_switch_is_reported(self):
        stocks, caps = _stores(crop=0.40, material=0.10)
        night = ProfileSegment(name="Night", start_minute=23 * 60, end_minute=7 * 60)
        day = ProfileSegment(name="Day", start_minute=7 * 60, end_minute=23 * 60)
        trajectories, _ = simulate_profile_cycle(
            [night, day],
            own_rates={ARMY: {resource: 0.0 for resource in Resource}},
            stocks={ARMY: stocks},
            capacities={ARMY: caps},
        )

        over = pre_night_overfills(trajectories, {ARMY: caps}, (ARMY,), night_profile="Night")

        assert {o.resource for o in over} == {Resource.CROP}
        findings = night_state_findings((), over, names={ARMY: "03"})
        (finding,) = findings
        assert finding.category is Category.PRE_NIGHT_BASELINE
        assert finding.severity is Severity.WARNING, (
            "the operator spends the stores down by hand; a snapshot that "
            "disagrees is a finding, never a refusal"
        )
        assert "40%" in finding.message
        assert "25%" in finding.message

    def test_a_village_at_or_below_the_baseline_is_silent(self):
        stocks, caps = _stores(crop=0.25, material=0.10)
        night = ProfileSegment(name="Night", start_minute=23 * 60, end_minute=7 * 60)
        trajectories, _ = simulate_profile_cycle(
            [night],
            own_rates={ARMY: {resource: 0.0 for resource in Resource}},
            stocks={ARMY: stocks},
            capacities={ARMY: caps},
        )

        assert not pre_night_overfills(trajectories, {ARMY: caps}, (ARMY,), night_profile="Night")


# ── Through the endpoint the page calls ──────────────────────────────────────


def _day_check_body(*, crop_stock, material_stock):
    def village(vid, name, x, y):
        return {
            "village_id": vid,
            "name": name,
            "x": x,
            "y": y,
            "merchants_total": 20,
            "merchants_free": 20,
            "lumber_per_hour": 0,
            "clay_per_hour": 0,
            "iron_per_hour": 0,
            "crop_per_hour": 0,
            "crop_stock": crop_stock,
            "lumber_stock": material_stock,
            "clay_stock": material_stock,
            "iron_stock": material_stock,
            "granary_capacity": 100_000,
            "warehouse_capacity": 100_000,
        }

    return DayCheckRequest.model_validate(
        {
            "snapshot": [village(HUB, "02", 0, 0), village(ARMY, "03", 1, 0)],
            "config": [
                {"village_id": HUB, "role": "capital"},
                {"village_id": ARMY, "role": "troops_off"},
            ],
            "roles": {"capital": {}, "troops_off": {}},
            "segments": [
                {"name": "Day", "window": [7 * 60, 23 * 60], "allocations": {}},
                {"name": "Night", "window": [23 * 60, 7 * 60], "allocations": {}},
            ],
        }
    )


class TestTheDayCheckReportsTheNightState:
    def test_a_role_village_short_at_dawn_is_reported_with_its_percentage(self):
        body = _day_check_body(crop_stock=10_000, material_stock=90_000)

        res = asyncio.run(post_day_check(body, USER))

        assert res.morning_floor == 0.60
        assert res.pre_night_baseline == 0.25
        rows = [r for r in res.morning_shortfalls if r.village_id == ARMY]
        assert [r.resource for r in rows] == [Resource.CROP]
        assert rows[0].fill == pytest.approx(0.10)
        assert rows[0].store == "granary"
        assert any("10%" in w and "03" in w for w in res.warnings), res.warnings

    def test_the_capital_is_not_reported_however_empty_it_is(self):
        body = _day_check_body(crop_stock=1_000, material_stock=1_000)

        res = asyncio.run(post_day_check(body, USER))

        assert not [r for r in res.morning_shortfalls if r.village_id == HUB], (
            "the capital is the storage hub and is excluded by name"
        )

    def test_a_role_village_over_the_baseline_at_the_switch_is_a_finding_not_a_refusal(self):
        # 40% crop against the 25% baseline; the materials are under it, so the
        # granary is the only store that has more in it than the night reserved
        # room for -- and the endpoint answers rather than refusing.
        body = _day_check_body(crop_stock=40_000, material_stock=10_000)

        res = asyncio.run(post_day_check(body, USER))

        rows = [r for r in res.pre_night_over_baseline if r.village_id == ARMY]
        assert [r.resource for r in rows] == [Resource.CROP]
        assert rows[0].fill == pytest.approx(0.40)
        assert rows[0].store == "granary"
        assert any("25%" in w and "40%" in w for w in res.warnings), res.warnings


def _split_night_body(*, declared: bool):
    """The night typed as two profiles either side of midnight.

    02 sits 60 fields from 03, which is five hours each way at 12 fields/h --
    a 600-minute round trip that cannot fit the 420 minutes between midnight
    and 07:00 however it is phased. Only the second half ships, so the whole
    of section 6's completion rule rests on that half being recognised.
    """

    def village(vid, name, x, crop_rate):
        return {
            "village_id": vid,
            "name": name,
            "x": x,
            "y": 0,
            "merchants_total": 20,
            "merchants_free": 20,
            "lumber_per_hour": 0,
            "clay_per_hour": 0,
            "iron_per_hour": 0,
            "crop_per_hour": crop_rate,
            "crop_stock": 50_000,
            "lumber_stock": 0,
            "clay_stock": 0,
            "iron_stock": 0,
            "granary_capacity": 400_000,
            "warehouse_capacity": 400_000,
        }

    def segment(name, window, allocations):
        entry = {"name": name, "window": list(window), "allocations": allocations}
        if declared:
            entry["overnight"] = name.startswith("Night")
        return entry

    return DayCheckRequest.model_validate(
        {
            "snapshot": [village(HUB, "02", 0, 1_000), village(ARMY, "03", 60, 0)],
            "config": [{"village_id": HUB}, {"village_id": ARMY}],
            "segments": [
                segment("Day", DAY_WINDOW, {}),
                segment("Night before midnight", NIGHT_BEFORE_MIDNIGHT, {}),
                segment(
                    "Night after midnight",
                    NIGHT_AFTER_MIDNIGHT,
                    {
                        "crop": {
                            str(HUB): {"mode": "absolute", "value": 0},
                            str(ARMY): {"mode": "remainder"},
                        }
                    },
                ),
            ],
        }
    )


class TestASplitNightIsStillTheNight:
    """Through the endpoint the page calls.

    The half of the night after midnight is where the 07:00 deadline actually
    bites, and it is the half the wrapping derivation cannot see.
    """

    def test_the_derivation_alone_checks_nothing_after_midnight(self):
        res = asyncio.run(post_day_check(_split_night_body(declared=False), USER))

        assert not res.night_overruns, (
            "00:00-07:00 does not wrap, so section 6's completion rule is never "
            f"applied to the only profile that ships: {res.night_overruns}"
        )

    def test_the_declaration_reports_the_merchants_still_on_the_road_at_07_00(self):
        res = asyncio.run(post_day_check(_split_night_body(declared=True), USER))

        rows = [r for r in res.night_overruns if r.origin == HUB and r.destination == ARMY]
        assert rows, f"a 600-minute round trip cannot close a 420-minute night: {res.warnings}"
        assert rows[0].overrun_minutes > 0
        assert rows[0].round_trip_minutes == pytest.approx(600.0)


# -- The night has to be ONE night, whatever order it is typed in -------------


def _night_state(res):
    """Everything section 6's two state rules produce, as one comparable value."""
    return (
        [(r.village_id, r.resource, r.store, r.fill) for r in res.morning_shortfalls],
        [(r.village_id, r.resource, r.store, r.fill) for r in res.pre_night_over_baseline],
        sorted(w for w in res.warnings if "07:00" in w or "60%" in w or "25%" in w),
    )


def _pieces_body(pieces):
    """A day check over exactly the profiles given, in exactly that order.

    Nothing moves: zero rates and no allocations anywhere, so 07:00 holds what
    was seeded and the only thing under test is WHICH minute each rule reads.
    03 is a troops_off village at 10% of its granary, well under the 60% floor.
    """

    def village(vid, name, x, crop_stock):
        return {
            "village_id": vid,
            "name": name,
            "x": x,
            "y": 0,
            "merchants_total": 20,
            "merchants_free": 20,
            "lumber_per_hour": 0,
            "clay_per_hour": 0,
            "iron_per_hour": 0,
            "crop_per_hour": 0,
            "crop_stock": crop_stock,
            # Materials well over the floor, so the granary is the only store
            # with anything to report and the rows stay about the crop.
            "lumber_stock": 90_000,
            "clay_stock": 90_000,
            "iron_stock": 90_000,
            "granary_capacity": 100_000,
            "warehouse_capacity": 100_000,
        }

    return DayCheckRequest.model_validate(
        {
            "snapshot": [village(HUB, "02", 0, 90_000), village(ARMY, "03", 1, 10_000)],
            "config": [
                {"village_id": HUB, "role": "capital"},
                {"village_id": ARMY, "role": "troops_off"},
            ],
            "roles": {"capital": {}, "troops_off": {}},
            "segments": [
                {"name": name, "window": list(window), "allocations": {}, "overnight": overnight}
                for name, window, overnight in pieces
            ],
        }
    )


DAY_TO_23 = ("Day", (7 * 60, 23 * 60), False)
NIGHT_A = ("Night before midnight", (23 * 60, 0), True)
NIGHT_B_GAPPED = ("Night after midnight", (30, 7 * 60), True)
NAP = ("Nap", (13 * 60, 14 * 60), True)


class TestTheNightsEndsDoNotDependOnListOrder:
    """Section 6 reads the night from both ends of it, and `next()` over the
    request's list order is not a way to find either.

    The comment claimed order-independence, and that holds only when the halves
    chain end-to-start. Both counter-cases are legal -- the overlap check
    refuses overlaps, not gaps -- and in both of them every declared-overnight
    profile qualifies as the opening AND as the closing, so the answer came
    down to which one happened to be listed first.
    """

    def test_a_gap_between_the_halves_reads_the_same_either_way_round(self):
        """23:00-00:00 and 00:30-07:00, with the half hour between them running
        on production alone. Listed one way the closing half ended at 00:00 and
        the morning floor was measured against nothing; listed the other way it
        ended at 07:00 and the floor was measured against the day."""
        forward = asyncio.run(
            post_day_check(_pieces_body([DAY_TO_23, NIGHT_A, NIGHT_B_GAPPED]), USER)
        )
        backward = asyncio.run(
            post_day_check(_pieces_body([DAY_TO_23, NIGHT_B_GAPPED, NIGHT_A]), USER)
        )

        assert _night_state(forward) == _night_state(backward)

    def test_a_gap_says_so_rather_than_measuring_one_end_of_two_nights(self):
        res = asyncio.run(post_day_check(_pieces_body([DAY_TO_23, NIGHT_A, NIGHT_B_GAPPED]), USER))

        assert any("one continuous night" in w for w in res.warnings), res.warnings

    def test_a_second_declared_overnight_profile_reads_the_same_either_way_round(self):
        """An afternoon nap declared overnight is legal and is not the night.
        Both it and the real night qualified as opening and as closing, so both
        of section 6's state rules could end up measured against the nap."""
        pieces = [
            ("Morning", (7 * 60, 13 * 60), False),
            ("Afternoon", (14 * 60, 23 * 60), False),
            ("Night", (23 * 60, 7 * 60), True),
        ]
        forward = asyncio.run(post_day_check(_pieces_body([*pieces, NAP]), USER))
        backward = asyncio.run(post_day_check(_pieces_body([NAP, *pieces]), USER))

        assert _night_state(forward) == _night_state(backward)
        # And it is the ambiguity that has to be said, not one arbitrary end of
        # it: listed first, the nap became both ends of "the night".
        assert any("one continuous night" in w for w in forward.warnings), forward.warnings

    def test_one_contiguous_night_still_reads_both_of_its_ends(self):
        """The shape this branch exists to support must keep working: the 25%
        baseline at 23:00 and the 60% floor against the profile that takes over
        at 07:00, from halves given in either order."""
        night_b = ("Night after midnight", (0, 7 * 60), True)
        forward = asyncio.run(post_day_check(_pieces_body([DAY_TO_23, NIGHT_A, night_b]), USER))
        backward = asyncio.run(post_day_check(_pieces_body([DAY_TO_23, night_b, NIGHT_A]), USER))

        assert _night_state(forward) == _night_state(backward)
        rows = [r for r in forward.morning_shortfalls if r.village_id == ARMY]
        assert [r.store for r in rows] == ["granary"], forward.morning_shortfalls
        assert rows[0].fill == pytest.approx(0.10)
        assert not any("one continuous night" in w for w in forward.warnings), forward.warnings


# -- The deadline is 07:00, not whenever this half of the night ends ----------


def _split_night_shipping_body(one_way_fields):
    """A split night where the PRE-midnight half is the one that ships.

    The existing split-night fixture ships only after midnight, which is why
    nothing caught this: for the (1380, 0) half `_window_length` is 60, so any
    round trip over an hour was reported as missing section 6's deadline -- at
    12 fields/h, every route beyond 6 fields.
    """

    def village(vid, name, x, crop_rate):
        return {
            "village_id": vid,
            "name": name,
            "x": x,
            "y": 0,
            "merchants_total": 20,
            "merchants_free": 20,
            "lumber_per_hour": 0,
            "clay_per_hour": 0,
            "iron_per_hour": 0,
            "crop_per_hour": crop_rate,
            "crop_stock": 50_000,
            "lumber_stock": 0,
            "clay_stock": 0,
            "iron_stock": 0,
            "granary_capacity": 400_000,
            "warehouse_capacity": 400_000,
        }

    ships = {
        "crop": {
            str(HUB): {"mode": "absolute", "value": 0},
            str(ARMY): {"mode": "remainder"},
        }
    }
    return DayCheckRequest.model_validate(
        {
            "snapshot": [
                village(HUB, "02", 0, 1_000),
                village(ARMY, "03", one_way_fields, 0),
            ],
            "config": [{"village_id": HUB}, {"village_id": ARMY}],
            "segments": [
                {"name": "Day", "window": list(DAY_WINDOW), "allocations": {}, "overnight": False},
                {
                    "name": "Night before midnight",
                    "window": list(NIGHT_BEFORE_MIDNIGHT),
                    "allocations": ships,
                    "overnight": True,
                },
                {
                    "name": "Night after midnight",
                    "window": list(NIGHT_AFTER_MIDNIGHT),
                    "allocations": {},
                    "overnight": True,
                },
            ],
        }
    )


class TestTheClosingDeadlineIsTheNightsEnd:
    """Section 6's rule is "all night movements complete before 07:00", and the
    consequence it states is that the morning starts with a full pool.

    Each half of a split night was measured against its OWN window's end, so a
    merchant dispatched at 23:00 and home at 02:00 was reported as a CRITICAL
    reading "the morning starts with merchants still on the road" -- five hours
    before the morning, on the profile shape this branch was built to support.

    A merchant still out at midnight does overlap the other half's fleet, which
    is real; that is the whole-day merchant boundary, reported as its own
    warning against `merchant_budget`, and it is not section 6's 07:00 rule.
    """

    def test_a_round_trip_that_ends_inside_the_night_is_not_an_overrun(self):
        # 90 min each way: leaves 23:00, home at 02:00, five hours clear of the
        # switch. Against the half's own 60-minute window that was 120 min late.
        beat = build_beat(
            (_leg(90.0, cycle=1),),
            dispatch_window=NIGHT_BEFORE_MIDNIGHT,
            overnight=True,
            night_end=MORNING_MINUTE,
        )

        assert not _overruns(beat), beat.warnings

    def test_a_round_trip_that_really_misses_07_00_still_is_one(self):
        # 260 min each way from 23:00 is home at 07:40: 40 minutes late, and the
        # only phase available is the window's single minute.
        beat = build_beat(
            (_leg(260.0, cycle=1),),
            dispatch_window=NIGHT_BEFORE_MIDNIGHT,
            overnight=True,
            night_end=MORNING_MINUTE,
        )

        (finding,) = _overruns(beat)
        assert finding.severity is Severity.CRITICAL
        assert "40 min" in finding.message, finding.message

    def test_a_night_stated_as_one_window_is_unchanged(self):
        """The degenerate case: the night's end IS the window's end, so passing
        it decides nothing and a 480-minute round trip still just fits."""
        stated = build_beat((_leg(240.0),), dispatch_window=NIGHT_WINDOW)
        with_end = build_beat(
            (_leg(240.0),), dispatch_window=NIGHT_WINDOW, night_end=MORNING_MINUTE
        )

        assert not _overruns(stated) and not _overruns(with_end)
        assert [p.dispatch_minute for p in stated.routes] == [
            p.dispatch_minute for p in with_end.routes
        ]

    def test_the_endpoint_measures_the_pre_midnight_half_against_07_00(self):
        """03 is 30 fields out: a 300-minute round trip, home at 04:00. Against
        the half's own window that is a CRITICAL four hours early."""
        res = asyncio.run(post_day_check(_split_night_shipping_body(30), USER))

        assert not res.night_overruns, res.night_overruns
        assert not [w for w in res.warnings if "still on the road" in w], res.warnings

    def test_the_endpoint_still_reports_a_half_that_misses_the_morning(self):
        """55 fields out is a 550-minute round trip from 23:00 -- home at 08:10,
        70 minutes into the morning profile."""
        res = asyncio.run(post_day_check(_split_night_shipping_body(55), USER))

        rows = [r for r in res.night_overruns if r.origin == HUB]
        assert rows, res.warnings
        assert rows[0].overrun_minutes == pytest.approx(70.0)
