"""The declared material relay tier: profile section 5's one hop, said out loud.

Profile section 5 does not ask the planner to DISCOVER a relay tier. It states
that one exists, and constrains where it is drawn from: 02 holds the reserved
wood, may only reach its five neighbours (03, 18, 14, 13, 01), and hands off to
a relay that forwards to 11 / 17 / 19. Role villages may not relay; feeders may.

So the tier is declared, not searched. ``VillageConfig.relay_for`` names the
villages one village forwards the capital's material to, and the planner builds
the two legs BY CONSTRUCTION -- ``source -> relay`` sized to the sum of that
relay's downstreams' unmet material demand, and ``relay -> each downstream``
sized to that downstream's own gap. Nothing in ``_improve_flows`` searches for a
material hub and the crop relay mover is untouched.

**This amends a documented structural invariant.** The old rule was "no material
village both sends and receives"; the new one is "no material village both sends
and receives EXCEPT a declared relay, and no relay feeds a relay". The four
assertion sites that pinned the old rule are amended, not deleted:
``test_distribution_optimizer.py::TestStructuralInvariants::test_no_village_relays_a_material``,
``test_distribution_golden.py::test_golden_plan_respects_the_structural_invariants``
and ``test_distribution_invariants.py``'s MATERIALS senders-and-receivers block
all now exempt a DECLARED relay and assert the no-second-hop rule in its place.

What made this a new mechanism rather than a flag: the existing crop relay mover
only ever REROUTES an existing direct flow, and when the direct edge is banned
there is no flow to reroute. Measured on the operator's own fixture with 02 the
only wood source and its whitelist on, the plan came back infeasible with three
named shortfalls -- 11, 17 and 19 each short 8,372 lumber/h -- and no relay was
attempted at all.

``relay_for`` unset anywhere must reproduce today's behaviour byte for byte;
that is pinned here by comparison and in test_distribution_golden.py by recorded
output.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.findings import Category, Severity
from travian_api.services.distribution.schedule import MINUTES_PER_DAY
from travian_api.web.routes.distribution import (
    DayCheckRequest,
    ExecuteRequest,
    NightProfileRequest,
    PlanRequest,
    post_plan,
)

USER = SimpleNamespace(id=1)

# The account, laid out so the whitelist genuinely bites: 02 can reach the two
# relays and 13, and cannot reach 11, 17 or 19 at all.
CAPITAL = 2
RELAY_A = 18
RELAY_B = 14
D1 = 11
D2 = 17
D3 = 19
DIRECT = 13
REMAINDER = 1

COORDS = {
    CAPITAL: (0, 0),
    RELAY_A: (3, 0),
    RELAY_B: (0, 3),
    D1: (6, 0),
    D2: (3, 4),
    D3: (0, 6),
    DIRECT: (2, 0),
    REMAINDER: (1, 1),
}

# The profile's own defensive lumber figure, which is what makes the shortfalls
# in the docstring above 8,372 rather than a number invented here.
DEF_LUMBER = 8372.0
WHITELIST = [RELAY_A, RELAY_B, DIRECT, REMAINDER]


# A forward haul long enough that its cheapest cycle is LONGER than the
# collecting leg's, which is the regime the operator's own geometry is in (02 ->
# 18 is one field, 18 -> 11 is seventeen) and the one that makes the relay's
# buffer bound more than a single batch.
#
# Ten fields from the relay at (3|0) is a 50-minute trip: a 1h cycle keeps two
# sets in the air where a 2h cycle keeps one, and `cheapest_cycle` takes the 2h.
# Measured, with the latency pass off (see LATENCY_OFF): collect 1h, forward 2h,
# and the relay commits 14 of its 18 -- so the case is feasible and the buffer
# is the only thing under test.
FAR_DOWNSTREAM = {D1: (13, 0), D2: (3, 10)}

# The latency pass spends a village's IDLE merchants on shorter cycles, and on a
# haul this short it spends them all the way back to a 1h forward cycle -- which
# is the right thing for the plan and would leave the case above measuring the
# equal-cycle regime again (measured: it did, and the case's own guard caught
# it). Switched off here so the cycles are the merchant-minimal ones, which is
# what `max_latency_hours=None` is documented to give.
LATENCY_OFF = {"max_latency_hours": None}


def _village(vid, *, lumber=0.0, merchants=20, warehouse=400_000, lumber_stock=None, coords=None):
    x, y = (coords or COORDS)[vid]
    return {
        "village_id": vid,
        "name": f"{vid:02d}",
        "x": x,
        "y": y,
        "merchants_total": merchants,
        "merchants_free": merchants,
        "lumber_per_hour": lumber,
        "clay_per_hour": 0.0,
        "iron_per_hour": 0.0,
        "crop_per_hour": 500.0,
        "lumber_stock": int(warehouse * 0.25) if lumber_stock is None else lumber_stock,
        "clay_stock": 0,
        "iron_stock": 0,
        "crop_stock": int(warehouse * 0.25),
        "warehouse_capacity": warehouse,
        "granary_capacity": warehouse,
    }


def _payload(
    *,
    relays=None,
    whitelist=True,
    roles=None,
    village_roles=None,
    caps=None,
    warehouses=None,
    lumber_stocks=None,
    coords=None,
    **kw,
):
    """02 the only wood source, and 11/17/19 out of its reach.

    ``relays`` is ``{village_id: [downstream, ...]}`` -- exactly what the
    operator types. Absent, this is the r09-B shape the plan cannot serve.
    """
    relays = relays or {}
    caps = caps or {}
    warehouses = warehouses or {}
    lumber_stocks = lumber_stocks or {}
    village_roles = village_roles or {}
    where = {**COORDS, **(coords or {})}
    snapshot = [
        _village(
            vid,
            lumber=40_000.0 if vid == CAPITAL else 0.0,
            warehouse=warehouses.get(vid, 400_000),
            lumber_stock=lumber_stocks.get(vid),
            coords=where,
        )
        for vid in sorted(COORDS)
    ]
    config = []
    for vid in sorted(COORDS):
        entry = {"village_id": vid}
        if vid == CAPITAL and whitelist:
            entry["ship_only_to"] = list(WHITELIST)
        if vid in relays:
            entry["relay_for"] = list(relays[vid])
        if vid in caps:
            entry["max_busy_merchants"] = caps[vid]
        if vid in village_roles:
            entry["role"] = village_roles[vid]
        config.append(entry)
    payload = {
        "snapshot": snapshot,
        "config": config,
        "allocations": {
            "lumber": {
                str(CAPITAL): {"mode": "absolute", "value": 0},
                str(D1): {"mode": "absolute", "value": DEF_LUMBER},
                str(D2): {"mode": "absolute", "value": DEF_LUMBER},
                str(D3): {"mode": "absolute", "value": DEF_LUMBER},
                str(DIRECT): {"mode": "absolute", "value": DEF_LUMBER},
                str(REMAINDER): {"mode": "remainder"},
            }
        },
        "foreign_targets": [],
        "speed_fields_per_hour": 12.0,
    }
    if roles:
        payload["roles"] = roles
    payload.update(kw)
    return payload


def _request(**kw) -> PlanRequest:
    return PlanRequest.model_validate(_payload(**kw))


def _plan(**kw):
    return asyncio.run(post_plan(_request(**kw), USER))


def _lumber_legs(res):
    return sorted(
        (row.origin, row.destination, round(row.cargo.get(Resource.LUMBER, 0) / row.cycle_hours))
        for row in res.rows
        if row.cargo.get(Resource.LUMBER, 0)
    )


def _budget(res, vid):
    return next(b for b in res.budgets if b.village_id == vid)


# The tier the operator chose (FINDINGS.md, operator answer 2: "02 -> 18/14 ->
# 11/17/19, two hops"). Which relay serves which defensive village is a solver
# output, so the assignment below is this fixture's, not his instruction.
TIER = {RELAY_A: [D1, D2], RELAY_B: [D3]}


class TestTheFieldIsRefusedWhenItCannotMeanAnything:
    """Every refusal names what is wrong, at the schema, for all four paths.

    On ``PlanRequest``, so ``/plan``, ``/day-check``, ``/execute segments`` and
    ``/night-profile`` all get it from one place -- which is precisely how
    ``/night-profile`` came to ignore ``consumption_per_hour`` altogether.
    """

    def test_a_downstream_the_snapshot_does_not_contain_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            _request(relays={RELAY_A: [D1, 4242]})

        detail = str(exc.value)
        assert "4242" in detail
        assert "18" in detail

    def test_a_relay_cannot_relay_for_itself(self):
        with pytest.raises(ValidationError) as exc:
            _request(relays={RELAY_A: [D1, RELAY_A]})

        assert "18" in str(exc.value)

    def test_an_empty_list_is_refused_rather_than_read_as_no_tier(self):
        """A relay for nobody is a half-typed row, not a declaration.

        Unlike ``ship_only_to``, where an empty list IS an answer ("ships to
        nobody"), there is no reading of "forwards to nobody" that differs from
        leaving the field off -- so accepting it would let a typo look like a
        decision.
        """
        with pytest.raises(ValidationError) as exc:
            _request(relays={RELAY_A: []})

        assert "18" in str(exc.value)

    @pytest.mark.parametrize("role", ["capital", "troops_off", "full_off", "def"])
    def test_a_role_village_may_not_be_a_relay(self, role):
        """Profile section 5.9, and the refusal names the role.

        Not silently dropped: a relay declared and ignored plans the defensive
        villages as unreachable while the operator reads a tier that is not
        there.
        """
        with pytest.raises(ValidationError) as exc:
            _request(
                relays={RELAY_A: [D1]},
                village_roles={RELAY_A: role},
                roles={role: {"allocations": {}}},
            )

        detail = str(exc.value)
        assert role in detail
        assert "18" in detail

    @pytest.mark.parametrize("role", ["feeder"])
    def test_a_feeder_may_relay(self, role):
        """The other half of 5.9, and the case the operator actually has."""
        request = _request(
            relays=TIER,
            village_roles={RELAY_A: role, RELAY_B: role},
            roles={role: {"allocations": {}}},
        )

        assert request.config[0].village_id == REMAINDER

    def test_a_village_with_no_role_at_all_may_relay(self):
        """Most accounts. Nothing declared is not a declaration of a role."""
        assert _request(relays=TIER) is not None

    def test_a_relay_may_not_feed_a_relay(self):
        """No second hop, and the 422 names BOTH villages.

        One hop is the whole amendment. A chain would put a hub's forward leg
        behind another hub's forward leg, which the beat's collect-then-ship
        ordering was never designed for -- and which the crop side forbids for
        exactly the same reason (``_crop_shape_ok``).
        """
        with pytest.raises(ValidationError) as exc:
            _request(relays={RELAY_A: [RELAY_B], RELAY_B: [D3]})

        detail = str(exc.value)
        assert "18" in detail
        assert "14" in detail

    def test_a_downstream_named_twice_in_one_list_is_refused(self):
        """A duplicate is one downstream, and sizing it twice ships it twice.

        Measured before this refusal existed, on this fixture: ``{18: [11, 11]}``
        built ``02 -> 18`` at 16,744/h and ``18 -> 11`` at 16,744/h against
        11's 8,372/h target, 17 got nothing, and the shortfall it was handed
        blamed 02's ``ship_only_to``.
        """
        with pytest.raises(ValidationError) as exc:
            _request(relays={RELAY_A: [D1, D1], RELAY_B: [D3]})

        detail = str(exc.value)
        assert "18" in detail
        assert "11" in detail

    def test_two_relays_may_not_claim_the_same_downstream(self):
        """The other half of it, and the refusal names the village and BOTH relays.

        Neither list is wrong on its own, so neither relay identifies the fix --
        exactly the reason the no-second-hop refusal names both villages too.
        Measured before this existed: 11 landed 16,744/h against an 8,372/h
        target while 17 and 19 were reported unreachable.
        """
        with pytest.raises(ValidationError) as exc:
            _request(relays={RELAY_A: [D1], RELAY_B: [D1]})

        detail = str(exc.value)
        assert "11" in detail
        assert "18" in detail
        assert "14" in detail

    @pytest.mark.parametrize(
        "request_type", [PlanRequest, ExecuteRequest, DayCheckRequest, NightProfileRequest]
    )
    @pytest.mark.parametrize(
        "relays",
        [{RELAY_A: [D1, D1], RELAY_B: [D3]}, {RELAY_A: [D1], RELAY_B: [D1]}],
        ids=["named-twice", "two-relays-one-downstream"],
    )
    def test_both_duplicate_rules_hold_on_all_four_planning_paths(self, request_type, relays):
        """One validator, four endpoints -- asserted rather than assumed.

        ``/night-profile`` is in this list because it is the endpoint that once
        ignored ``consumption_per_hour`` altogether while every other path
        honoured it. A rule that holds on ``/plan`` alone is a rule the operator
        can walk around by pressing a different button.
        """
        payload = _payload(relays=relays)
        if request_type is DayCheckRequest:
            # A day-check carries its allocations per segment and refuses them at
            # the top level, so the payload has to move them across -- otherwise
            # this case passes on THAT refusal and says nothing about relay_for.
            payload["segments"] = [
                {"name": "day", "window": (0, 1439), "allocations": payload.pop("allocations")}
            ]
        with pytest.raises(ValidationError) as exc:
            request_type.model_validate(payload)

        assert "relay" in str(exc.value), (
            f"{request_type.__name__} refused the payload for some other reason: {exc.value}"
        )


class TestUnsetIsTodaysBehaviour:
    def test_no_relay_anywhere_is_still_three_shortfalls(self):
        """The measurement that made this a feature. Do not "fix" it here.

        02 holds the only wood and its whitelist keeps it away from 11, 17 and
        19; the crop relay mover cannot help because there is no direct flow to
        reroute.
        """
        res = _plan()

        assert res.feasible is False
        short = sorted((s.village_name, round(s.per_hour)) for s in res.shortfalls)
        assert short == [("11", 8372), ("17", 8372), ("19", 8372)]
        assert all("ship_only_to" in s.reason for s in res.shortfalls)

    def test_an_explicit_none_plans_identically_to_saying_nothing(self):
        silent = _plan()
        explicit = asyncio.run(
            post_plan(
                PlanRequest.model_validate(
                    {
                        **_payload(),
                        "config": [{**entry, "relay_for": None} for entry in _payload()["config"]],
                    }
                ),
                USER,
            )
        )

        assert explicit.model_dump() == silent.model_dump()

    def test_no_material_relay_means_no_material_relay_reported(self):
        res = _plan()

        assert [r for r in res.relays if r.resource != Resource.CROP] == []


class TestTheTierIsBuiltByConstruction:
    def test_the_two_legs_appear_and_the_plan_becomes_feasible(self):
        res = _plan(relays=TIER)

        assert res.shortfalls == []
        assert res.feasible is True
        legs = _lumber_legs(res)
        # 02 -> 18 carries both of 18's downstreams; 02 -> 14 carries 19's.
        assert (CAPITAL, RELAY_A, round(2 * DEF_LUMBER)) in legs
        assert (CAPITAL, RELAY_B, round(DEF_LUMBER)) in legs
        assert (RELAY_A, D1, round(DEF_LUMBER)) in legs
        assert (RELAY_A, D2, round(DEF_LUMBER)) in legs
        assert (RELAY_B, D3, round(DEF_LUMBER)) in legs

    def test_the_direct_leg_the_whitelist_allows_is_untouched(self):
        res = _plan(relays=TIER)

        assert (CAPITAL, DIRECT, round(DEF_LUMBER)) in _lumber_legs(res)

    def test_every_downstream_lands_its_whole_target(self):
        res = _plan(relays=TIER)

        for vid in (D1, D2, D3, DIRECT):
            landed = sum(
                row.cargo.get(Resource.LUMBER, 0) / row.cycle_hours
                for row in res.rows
                if row.destination == vid
            )
            assert landed == pytest.approx(DEF_LUMBER, rel=1e-3)

    def test_the_relay_keeps_nothing_it_forwards(self):
        """Conservation at the hub: everything in goes out again.

        A relay that banked a share of the pass-through would be a village the
        operator never gave a lumber target, quietly accumulating.
        """
        res = _plan(relays=TIER)

        for relay in TIER:
            inbound = sum(
                row.cargo.get(Resource.LUMBER, 0) / row.cycle_hours
                for row in res.rows
                if row.destination == relay
            )
            outbound = sum(
                row.cargo.get(Resource.LUMBER, 0) / row.cycle_hours
                for row in res.rows
                if row.origin == relay
            )
            assert inbound == pytest.approx(outbound, rel=1e-3)

    def test_the_collect_leg_is_reported_as_a_relay_of_its_own_resource(self):
        res = _plan(relays=TIER)

        material = {r.hub_name: r for r in res.relays if r.resource == Resource.LUMBER}
        assert set(material) == {"18", "14"}
        assert material["18"].origin_names == ["02"]
        assert material["18"].destination_names == ["11", "17"]

    def test_the_collect_legs_merchants_land_on_the_sources_budget(self):
        """Profile section 5: "the relay leg counts inside the 8 at 02".

        Not merely present on the budget line -- the whole of 02's commitment
        must be the sum of its own legs, collect legs included, or a cap of 8 is
        measured against a figure that leaves the tier out.
        """
        res = _plan(relays=TIER)

        # `BudgetLegResponse.destination` is the village as the operator names
        # it, never an id -- so the whole of this test speaks in labels.
        def label(vid):
            return f"{vid:02d}"

        budget = _budget(res, CAPITAL)
        legs = {leg.destination: leg.merchants for leg in budget.legs}
        assert label(RELAY_A) in legs, "the collect leg is missing from 02's budget"
        assert label(RELAY_B) in legs
        assert budget.committed == sum(legs.values())
        # And the forward legs are billed to the RELAYS, never to 02.
        for relay, downstreams in TIER.items():
            relay_legs = {leg.destination for leg in _budget(res, relay).legs}
            assert {label(vid) for vid in downstreams} <= relay_legs
            assert not {label(vid) for vid in downstreams} & set(legs)

    def test_a_declared_relay_is_not_reachable_from_the_search(self):
        """The tier is by construction, so the crop relay mover is untouched.

        With no crop flow to relay there must be no crop hub, whatever the
        material tier does.
        """
        res = _plan(relays=TIER)

        assert [r for r in res.relays if r.resource == Resource.CROP] == []


class TestTheBeatShipsAfterItCollects:
    """Measured on the real firing minutes, exactly as the crop case is.

    ``schedule.build_beat``'s collect-then-ship machinery was crop-only in three
    places -- the hub set, the topological placement pass and the arrival
    bookkeeping -- so a material relay would have forwarded from its own
    warehouse while the inbound leg merely refilled it.
    """

    def test_each_forward_leg_dispatches_after_its_collect_leg_lands(self):
        res = _plan(relays=TIER)

        rows = {(r.origin, r.destination): r for r in res.rows}
        for relay, downstreams in TIER.items():
            collect = rows[(CAPITAL, relay)]
            arrivals = _firings(collect, arrival=True)
            for downstream in downstreams:
                forward = rows[(relay, downstream)]
                dispatches = _firings(forward)
                worst = max(min((d - a) % MINUTES_PER_DAY for a in arrivals) for d in dispatches)
                assert worst < forward.cycle_hours * 60, (
                    f"{relay} waits {worst} min after collecting before forwarding to "
                    f"{downstream}, which is a whole {forward.cycle_hours}h cycle -- it "
                    f"is shipping from its own warehouse"
                )


def _firings(row, *, arrival=False):
    """Every minute this sheet row fires (or lands) across the day."""
    step = row.cycle_hours * 60
    base = row.arrival if arrival else row.dispatch
    start = _minutes(base)
    return [(start + offset) % MINUTES_PER_DAY for offset in range(0, MINUTES_PER_DAY, step)]


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


class TestTheRelaysWarehouseMustHoldThePassThrough:
    """The reason material relay was deferred once before -- and what it really is.

    The concern on record: 02's warehouse is 1,200,000 and a neighbour's is
    160,000, and that neighbour "would fill on 02's flow in under 7 hours of an
    8-hour night". That reasoning assumed the relay does not forward while it
    collects, which is precisely the defect the collect-then-ship generalisation
    in ``schedule.build_beat`` fixed for materials. With the forward legs phased
    after the collecting arrival, the law is sharper:

        **the relay's warehouse must hold what lands between two FORWARD
        sends** -- the collecting rate times the FORWARDING cycle.

    Two things about that are worth stating, because the first reading of it is
    wrong in both directions.

    It is not "one collecting batch". That is only the same number when the two
    legs share a cycle, and on the operator's own geometry they do not: 02 -> 18
    is one field and costs least on a 1h cycle, while 18 -> 11 is seventeen and
    costs least on 2h -- so TWO batches land between forward sends and the relay
    holds both. Measured on his fixture, the finding is silent at a 33,488
    warehouse (16,744/h x 2h) and fires at 33,000, while one batch is 16,744.

    And it is not free SPACE. The steady state settles the trough at
    ``cap - peak`` wherever that is positive, so a relay that starts nearly full
    sheds once and then never sheds again -- a real cost, and the one the
    existing filling-store check already reports. Only a capacity below the peak
    sheds something on every cycle, which is the recurring defect this names.

    So 02's neighbour at 160,000 holds this tier with room to spare, and what
    does not hold is a relay whose warehouse is smaller than the pass-through --
    a freshly settled feeder near the capital, which is exactly the shape of
    village a tier gets drawn from.
    """

    def _relay_buffer(self, res):
        """Every relay-buffer finding, in either severity.

        Reached through `diagnostics`, which is where the endpoint puts the
        structured findings -- `warnings` is the same content as prose.
        """
        return [
            finding
            for group in res.diagnostics.groups
            for finding in group.findings
            if finding.category.startswith("relay_buffer")
        ]

    def _pass_through(self, res):
        """What lands at RELAY_A between two of its forward sends.

        Read off the plan rather than written down, so this states the LAW and
        not a number that happens to hold for one set of cycles. A fixture whose
        cycles moved would otherwise leave every threshold below measuring
        something else, and all of them would still pass.
        """
        rows = {(r.origin, r.destination): r for r in res.rows}
        collect = rows[(CAPITAL, RELAY_A)]
        collect_rate = collect.cargo[Resource.LUMBER] / collect.cycle_hours
        forward_cycle = max(
            row.cycle_hours for (origin, _d), row in rows.items() if origin == RELAY_A
        )
        return collect_rate * forward_cycle

    def test_the_bound_is_the_pass_through_between_forward_sends(self):
        """The law itself, found by sweeping and compared with the arithmetic.

        This is the test that would have caught the first version of this
        class, which asserted a one-batch bound: on a fixture with equal cycles
        one batch IS the pass-through, so the narrower claim passed and read as
        though it had been established.
        """
        reference = _plan(relays=TIER)
        bound = self._pass_through(reference)

        # Coarse either side of the bound, then the two capacities that bracket
        # it: the claim is about where the finding APPEARS, so the pair astride
        # it is the whole measurement and the rest is context.
        holds = [int(bound), int(bound) + 5_000, int(bound) * 4]
        sheds = [int(bound) - 500, int(bound) // 2, int(bound) // 4]
        for warehouse in holds:
            res = _plan(relays=TIER, warehouses={RELAY_A: warehouse})
            assert self._relay_buffer(res) == [], (
                f"{warehouse:,} holds the {bound:,.0f} pass-through and was flagged anyway"
            )
        for warehouse in sheds:
            res = _plan(relays=TIER, warehouses={RELAY_A: warehouse})
            assert self._relay_buffer(res), (
                f"{warehouse:,} cannot hold the {bound:,.0f} pass-through and said nothing"
            )

    def test_a_slower_forward_leg_raises_the_bound_above_one_batch(self):
        """The regime the corrected law exists for, and the one that caught it.

        With the downstreams moved far enough out that their cheapest cycle is
        2h against the collecting leg's 1h, TWO collecting batches land between
        forward sends and the relay has to hold both. A warehouse that holds one
        batch comfortably is then not enough -- which is exactly what the first
        version of this class asserted was fine, and what the operator's own
        geometry does.
        """
        reference = _plan(relays=TIER, coords=FAR_DOWNSTREAM, **LATENCY_OFF)
        rows = {(r.origin, r.destination): r for r in reference.rows}
        collect = rows[(CAPITAL, RELAY_A)]
        forward_cycle = max(
            row.cycle_hours for (origin, _d), row in rows.items() if origin == RELAY_A
        )
        assert forward_cycle > collect.cycle_hours, (
            f"this case needs a slower forward leg to mean anything, and the plan "
            f"gave collect {collect.cycle_hours}h / forward {forward_cycle}h"
        )
        one_batch = collect.cargo[Resource.LUMBER]
        bound = self._pass_through(reference)
        assert bound > one_batch

        # A warehouse between the two: it holds a batch and not the
        # pass-through, so it is the capacity the one-batch reading called safe.
        between = (one_batch + int(bound)) // 2
        flagged = _plan(
            relays=TIER, coords=FAR_DOWNSTREAM, warehouses={RELAY_A: between}, **LATENCY_OFF
        )
        assert self._relay_buffer(flagged), (
            f"{between:,} holds one {one_batch:,} batch but not the {bound:,.0f} that "
            f"lands between forward sends, and nothing was reported"
        )

        held = _plan(
            relays=TIER, coords=FAR_DOWNSTREAM, warehouses={RELAY_A: int(bound)}, **LATENCY_OFF
        )
        assert self._relay_buffer(held) == []

    def test_it_fires_when_the_relay_cannot_hold_the_pass_through(self):
        res = _plan(relays=TIER, warehouses={RELAY_A: 12_000})

        found = self._relay_buffer(res)
        assert found, "a 12,000 warehouse taking a 16,744 batch said nothing"
        assert [f.village for f in found] == ["18"]
        assert "12,000" in found[0].message
        assert "relay" in found[0].message.lower()
        assert found[0].loss_per_day > 0

    def test_it_is_critical_because_nothing_was_forwarded_first(self):
        """The cargo is destroyed at the relay, not merely delayed.

        02's reserved wood leaves 02, never reaches 11 or 17, and neither end's
        own store says anything is wrong -- which is why this is its own finding
        rather than an overflow line at village 18.
        """
        found = self._relay_buffer(_plan(relays=TIER, warehouses={RELAY_A: 12_000}))

        assert [f.severity for f in found] == ["critical"]
        assert [f.category for f in found] == [Category.RELAY_BUFFER.value]

    def test_the_capitals_own_warehouse_size_is_not_close_to_the_bound(self):
        res = _plan(relays=TIER, warehouses={RELAY_A: 1_200_000, RELAY_B: 1_200_000})

        assert self._relay_buffer(res) == []

    def test_a_neighbours_160k_warehouse_holds_this_tier_comfortably(self):
        """The figure section 5's deferral was argued from, measured.

        Asserted because it is worth knowing rather than assuming: with the
        forward legs phased after the collecting arrival, 160,000 is many times
        the pass-through and never troubles it.
        """
        res = _plan(relays=TIER, warehouses={RELAY_A: 160_000})

        assert self._relay_buffer(res) == []

    def test_a_relay_that_starts_nearly_full_is_not_this_finding(self):
        """Free space is a transient; capacity below the peak is not.

        A relay holding 94% of a warehouse that CAN take the pass-through sheds
        one load and then settles, which the filling-store check reports. Firing
        here as well would teach the operator to ignore the one that matters.
        """
        res = _plan(
            relays=TIER,
            warehouses={RELAY_A: 160_000},
            lumber_stocks={RELAY_A: 150_000},
        )

        assert self._relay_buffer(res) == []

    def test_it_says_nothing_about_a_relay_nobody_declared(self):
        """No tier, no pass-through, no finding -- however small the warehouse."""
        res = _plan(warehouses={RELAY_A: 12_000})

        assert self._relay_buffer(res) == []


class TestTheBufferSeverityTurnsOnWhetherAnythingLeftFirst:
    """A pure check over an already-replayed day, so both branches are reachable.

    Through the endpoint only the CRITICAL branch is naturally produced -- a
    well-sized tier ships out what it takes in, so a hub either overflows on its
    very first batch or never overflows at all. The WARNING branch is the hub
    that fills only after it has already forwarded something, which is a
    scheduling cost rather than destroyed cargo; both are asserted here on the
    function that decides, rather than on a fixture bent into producing one.
    """

    HUB = 18
    SOURCE = 2
    DOWNSTREAM = 11

    def _case(self, fill_minute: int, forward_dispatch: int):
        from travian_api.services.distribution.optimizer import RelayHub, Route
        from travian_api.services.distribution.schedule import Beat, ScheduledRoute
        from travian_api.services.distribution.storage import (
            OverflowEvent,
            relay_buffer_findings,
        )

        def route(origin, destination):
            return Route(
                origin=origin,
                destination=destination,
                cargo_per_hour={Resource.LUMBER: 8372.0},
                cycle_hours=8,
                merchants_per_send=4,
                sets_in_flight=1,
                one_way_minutes=30.0,
            )

        beat = Beat(
            routes=(
                ScheduledRoute(route=route(self.SOURCE, self.HUB), dispatch_minute=0),
                ScheduledRoute(
                    route=route(self.HUB, self.DOWNSTREAM), dispatch_minute=forward_dispatch
                ),
            )
        )
        hubs = (
            RelayHub(
                hub=self.HUB,
                origins=(self.SOURCE,),
                destinations=(self.DOWNSTREAM,),
                collect_hours=1.0,
                forward_hours=1.0,
                resource=Resource.LUMBER,
            ),
        )
        overflows = (
            OverflowEvent(
                village_id=self.HUB,
                resource=Resource.LUMBER,
                minute=fill_minute,
                wasted_per_day=100_000.0,
            ),
        )
        return relay_buffer_findings(
            hubs, overflows, beat, {self.HUB: {Resource.LUMBER: 160_000}}, names={self.HUB: "18"}
        )

    def test_filling_before_the_first_forward_send_is_critical(self):
        # Cargo lands at 00:30 and the hub does not forward until 09:00, so
        # everything past the cap between the two is destroyed.
        found = self._case(fill_minute=30, forward_dispatch=540)

        assert len(found) == 1
        assert found[0].severity is Severity.CRITICAL
        assert found[0].category == Category.RELAY_BUFFER

    def test_filling_after_something_has_already_left_is_a_warning(self):
        found = self._case(fill_minute=600, forward_dispatch=60)

        assert len(found) == 1
        assert found[0].severity is Severity.WARNING

    def test_a_hub_with_no_overflow_says_nothing(self):
        from travian_api.services.distribution.optimizer import RelayHub
        from travian_api.services.distribution.schedule import Beat
        from travian_api.services.distribution.storage import relay_buffer_findings

        hubs = (
            RelayHub(
                hub=self.HUB,
                origins=(self.SOURCE,),
                destinations=(self.DOWNSTREAM,),
                collect_hours=1.0,
                forward_hours=1.0,
                resource=Resource.LUMBER,
            ),
        )

        assert relay_buffer_findings(hubs, (), Beat(), {}, names={}) == []

    def test_a_crop_hub_is_not_this_findings_business(self):
        """Crop relay has its own report and its own hub-solvency guard.

        A granary filling on a relay is the ordinary overflow the storage
        findings already name; RELAY_BUFFER is about the material tier this
        feature introduced.
        """
        from travian_api.services.distribution.optimizer import RelayHub, Route
        from travian_api.services.distribution.schedule import Beat, ScheduledRoute
        from travian_api.services.distribution.storage import (
            OverflowEvent,
            relay_buffer_findings,
        )

        def route(origin, destination):
            return Route(
                origin=origin,
                destination=destination,
                cargo_per_hour={Resource.CROP: 500.0},
                cycle_hours=8,
                merchants_per_send=1,
                sets_in_flight=1,
                one_way_minutes=30.0,
            )

        beat = Beat(
            routes=(
                ScheduledRoute(route=route(self.SOURCE, self.HUB), dispatch_minute=0),
                ScheduledRoute(route=route(self.HUB, self.DOWNSTREAM), dispatch_minute=540),
            )
        )
        hubs = (
            RelayHub(
                hub=self.HUB,
                origins=(self.SOURCE,),
                destinations=(self.DOWNSTREAM,),
                collect_hours=1.0,
                forward_hours=1.0,
            ),
        )
        overflows = (
            OverflowEvent(
                village_id=self.HUB, resource=Resource.CROP, minute=30, wasted_per_day=100_000.0
            ),
        )

        assert relay_buffer_findings(hubs, overflows, beat, {}, names={}) == []
