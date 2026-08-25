"""Relayed crop must be visible, and its latency measured end to end.

Two things were wrong at once, and they compound.

The optimizer may reroute a crop flow through an intermediate village, and the
sheet then shows ``V22 -> V02`` and ``V02 -> V17`` as two unrelated rows. An
audit of 66 synthetic accounts found 45 of them using a relay and not one of
8,784 warning lines mentioning that a relay existed. The operator types both
rows into the game with no way to know the second depends on the first.

And the latency target is checked per leg while the documented target is
end-to-end. Two legs each comfortably inside a 2h target composed to ~30h on one
audited account, and the plan reported no latency finding at all -- the one
number that would have exposed the relay was the number nobody computed.
"""

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.findings import Category, Severity
from travian_api.services.distribution.optimizer import Route, relay_chains, relay_findings

NAMES = {22: "V22", 2: "V02", 17: "V17"}


def _route(
    origin: int,
    destination: int,
    *,
    cycle: int = 1,
    one_way: float = 30.0,
    resource: Resource = Resource.CROP,
    amount: float = 500.0,
) -> Route:
    return Route(
        origin=origin,
        destination=destination,
        cargo_per_hour={resource: amount},
        cycle_hours=cycle,
        merchants_per_send=2,
        sets_in_flight=1,
        one_way_minutes=one_way,
    )


class TestFindingTheChains:
    def test_two_crop_legs_sharing_a_hub_are_one_chain(self):
        chains = relay_chains([_route(22, 2), _route(2, 17)])

        assert len(chains) == 1
        chain = chains[0]
        assert (chain.origin, chain.hub, chain.destination) == (22, 2, 17)

    def test_unrelated_legs_are_not_a_chain(self):
        assert relay_chains([_route(22, 2), _route(3, 17)]) == ()

    def test_a_direct_leg_alone_is_not_a_chain(self):
        assert relay_chains([_route(22, 2)]) == ()

    def test_material_legs_never_form_a_chain(self):
        """Relay is crop-only by design (profile section 3.5): materials must not
        chain A->B->C. A wood leg into a village that also ships wood out is two
        independent flows, not a relay, and calling it one would invent a
        dependency the plan does not have."""
        legs = [
            _route(22, 2, resource=Resource.LUMBER),
            _route(2, 17, resource=Resource.LUMBER),
        ]
        assert relay_chains(legs) == ()

    def test_a_mixed_cargo_leg_counts_for_its_crop(self):
        """Routes merge all four resources into one row, so a relay leg can
        arrive carrying lumber as well. What makes it a relay is the crop."""
        legs = [
            Route(
                origin=22,
                destination=2,
                cargo_per_hour={Resource.CROP: 400.0, Resource.LUMBER: 100.0},
                cycle_hours=1,
                merchants_per_send=2,
                sets_in_flight=1,
                one_way_minutes=30.0,
            ),
            _route(2, 17),
        ]
        assert len(relay_chains(legs)) == 1

    def test_a_hub_with_two_inbound_legs_yields_both_chains(self):
        chains = relay_chains([_route(22, 2), _route(23, 2), _route(2, 17)])

        assert {(c.origin, c.hub, c.destination) for c in chains} == {
            (22, 2, 17),
            (23, 2, 17),
        }

    def test_a_hub_forwarding_to_two_places_yields_both_chains(self):
        chains = relay_chains([_route(22, 2), _route(2, 17), _route(2, 18)])

        assert {(c.origin, c.hub, c.destination) for c in chains} == {
            (22, 2, 17),
            (22, 2, 18),
        }

    def test_the_hub_is_never_reported_as_its_own_origin(self):
        """A village shipping crop to itself cannot happen, but a self-loop in
        the input must not produce a chain that reads as a relay through
        nowhere."""
        assert relay_chains([_route(2, 2), _route(2, 17)]) == ()

    def test_worst_chain_reads_first(self):
        chains = relay_chains(
            [_route(22, 2, cycle=1), _route(23, 2, cycle=8), _route(2, 17, cycle=1)]
        )
        assert [c.origin for c in chains] == [23, 22]


class TestEndToEndLatency:
    def test_it_is_the_sum_of_the_two_legs(self):
        """Cargo waits a cycle at the origin, travels, then waits for the hub's
        next dispatch and travels again. Worst case is exactly both legs' own
        worst cases, which is what a per-leg check cannot see."""
        collect = _route(22, 2, cycle=4, one_way=30.0)
        forward = _route(2, 17, cycle=8, one_way=90.0)

        chain = relay_chains([collect, forward])[0]

        assert chain.collect_hours == pytest.approx(4.5)
        assert chain.forward_hours == pytest.approx(9.5)
        assert chain.end_to_end_hours == pytest.approx(14.0)

    def test_two_compliant_legs_can_still_miss_the_target(self):
        """The bug, stated as a number. Neither leg exceeds 2h; the delivery
        takes 3h."""
        collect = _route(22, 2, cycle=1, one_way=30.0)
        forward = _route(2, 17, cycle=1, one_way=30.0)

        chain = relay_chains([collect, forward])[0]

        assert collect.latency_hours <= 2.0
        assert forward.latency_hours <= 2.0
        assert chain.end_to_end_hours > 2.0


class TestThePlanReportsThem:
    def _findings(self, routes, *, target=2.0):
        return relay_findings(routes, names=NAMES, max_latency_hours=target)

    def test_a_relay_is_reported_even_when_it_meets_the_target(self):
        # Visibility is the point: a compliant relay is still a dependency
        # between two rows the operator is about to type in by hand.
        findings = self._findings(
            [_route(22, 2, cycle=1, one_way=6.0), _route(2, 17, cycle=1, one_way=6.0)],
            target=6.0,
        )

        assert len(findings) == 1
        assert findings[0].category is Category.RELAY
        assert findings[0].severity is Severity.NOTE
        assert "V22" in findings[0].message
        assert "V02" in findings[0].message
        assert "V17" in findings[0].message

    def test_a_relay_over_the_target_is_a_warning_naming_the_hours(self):
        findings = self._findings([_route(22, 2), _route(2, 17)])

        assert len(findings) == 1, "one finding per chain, not one per leg"
        finding = findings[0]
        assert finding.category is Category.RELAY_LATENCY
        assert finding.severity is Severity.WARNING
        assert "3.0h" in finding.message
        assert "V22 -> V02 -> V17" in finding.message

    def test_the_chain_is_not_also_reported_as_compliant(self):
        findings = self._findings([_route(22, 2), _route(2, 17)])
        assert [f.category for f in findings] == [Category.RELAY_LATENCY]

    def test_no_relay_means_no_findings(self):
        assert self._findings([_route(22, 2), _route(3, 17)]) == []

    def test_with_no_target_relays_are_still_announced(self):
        # A None target switches the latency pass off entirely; it must not also
        # hide the relay's existence.
        findings = self._findings([_route(22, 2), _route(2, 17)], target=None)

        assert [f.category for f in findings] == [Category.RELAY]

    def test_both_wordings_say_the_word_relay(self):
        """An operator scanning the warning list for "relay" has to find it.

        The over-target wording originally opened "crop V22 -> V02 -> V17 takes
        3.0h end-to-end", which never says what it is -- so on a plan where every
        relay missed the target, nothing in the output contained the word at all.
        """
        over = self._findings([_route(22, 2), _route(2, 17)])
        under = self._findings([_route(22, 2), _route(2, 17)], target=6.0)

        assert "relay" in over[0].message.lower()
        assert "relay" in under[0].message.lower()

    def test_the_finding_carries_the_end_to_end_hours_for_ranking(self):
        findings = self._findings([_route(22, 2, cycle=8), _route(2, 17, cycle=8)])
        assert "17.0h" in findings[0].detail


class TestTheEndpointShowsThem:
    """Through /plan, on a synthetic account that actually relays.

    The audit that motivated this found 45 of 66 accounts relaying and not one
    of 8,784 warning lines mentioning it, so the claim being pinned is end to
    end: the relay reaches the response, and it reaches the prose.
    """

    def _plan(self):
        import asyncio
        from types import SimpleNamespace

        from tests.distribution_synthetic import random_account
        from travian_api.web.routes import distribution as dist

        request = random_account(0, with_profiles=False).plan_request
        return asyncio.run(dist.post_plan(request, SimpleNamespace(id=1, username="t")))

    def test_a_relaying_account_reports_its_relays(self):
        result = self._plan()
        assert result.relays, "this seed relays; a response that says nothing is the bug"

    def test_both_legs_of_every_relay_are_real_sheet_rows(self):
        # Otherwise the panel names a hop the operator cannot find in the sheet.
        result = self._plan()
        pairs = {(row.origin, row.destination) for row in result.rows}

        for chain in result.relays:
            assert (chain.origin, chain.hub) in pairs
            assert (chain.hub, chain.destination) in pairs

    def test_every_relay_is_mentioned_in_the_prose(self):
        result = self._plan()
        hubs = {chain.hub_name for chain in result.relays}
        relay_lines = [w for w in result.warnings if "relay" in w.lower()]

        assert relay_lines
        for hub in hubs:
            assert any(hub in line for line in relay_lines), f"{hub} relays and nothing says so"

    def test_the_diagnostics_carry_a_relay_group(self):
        result = self._plan()
        categories = {group.category for group in result.diagnostics.groups}

        assert {"relay", "relay_latency"} & categories

    def test_end_to_end_exceeds_the_worst_single_leg(self):
        # The number the per-leg check could not see, on real planner output.
        result = self._plan()
        by_pair = {(r.origin, r.destination): r for r in result.rows}

        for chain in result.relays:
            legs = (by_pair[(chain.origin, chain.hub)], by_pair[(chain.hub, chain.destination)])
            assert chain.end_to_end_hours > max(leg.first_delivery_hours for leg in legs)
