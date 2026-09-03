"""Triage: does the finding list tell an operator what to do in five seconds?

The bug this pins is not a wrong number, it is 153 correct numbers nobody read.
A 25-village plan returned 153 warnings as one flat bulleted list -- 51 of them
the same systemic fact repeated per village with an identical figure, 51 more a
restatement of those same stores, the single 1.9M/day loss sitting in the middle
indistinguishable from a 22,224/day one, and no total anywhere. Every test here
asks whether the structure survives that shape: does one fact stay one item, does
the expensive thing come first, and is there a number at the top that decides
whether the operator cares at all.
"""

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.findings import (
    _SPECS,
    Category,
    Finding,
    Severity,
    summarise,
)


def _overflow(village: str, loss: float, resource: Resource = Resource.CLAY) -> Finding:
    return Finding(
        category=Category.OVERFLOW_STRUCTURAL,
        message=f"{village}: {resource.value} hits the cap and loses about {loss:,.0f}/day",
        detail=f"{village} — {loss:,.0f}/day",
        village=village,
        resource=resource,
        loss_per_day=loss,
    )


def _latency(origin: str, hours: float) -> Finding:
    return Finding(
        category=Category.LATENCY,
        message=(
            f"route {origin} -> hub has {hours:.1f}h latency against a 2h target; "
            f"geometry or the merchant budget may forbid better"
        ),
        detail=f"{origin} -> hub — {hours:.1f}h",
        village=origin,
    )


class TestTaxonomy:
    def test_every_category_has_a_severity_and_an_action(self):
        """A category with no spec would crash the summary at render time.

        More to the point: adding a warning must mean deciding how urgent it is
        and what to do about it. If a new category could be added without those,
        the flat undifferentiated list grows back one warning at a time.
        """
        for category in Category:
            assert category in _SPECS, f"{category} has no spec"
            spec = _SPECS[category]
            assert spec.action.strip(), f"{category} tells the operator nothing to do"
            assert spec.headline.strip()

    def test_severity_comes_from_the_category_not_the_call_site(self):
        """Two findings of the same kind cannot disagree about how urgent it is."""
        assert _latency("03", 5.9).severity is _latency("04", 2.1).severity
        assert _overflow("03", 10).severity is Severity.CRITICAL
        assert _latency("03", 5.9).severity is Severity.WARNING

    def test_editorial_order_is_unique_within_a_severity(self):
        """Ties would make the reading order depend on dict iteration order."""
        seen: dict[tuple[Severity, int], Category] = {}
        for category, spec in _SPECS.items():
            key = (spec.severity, spec.order)
            assert key not in seen, f"{category} and {seen[key]} share a rank"
            seen[key] = category


class TestAggregation:
    def test_seventeen_villages_losing_the_same_amount_are_one_finding(self):
        """The exact shape of the complaint: 17 lines, one idea.

        17 villages each losing 22,224/day of clay is a single systemic fact
        with a count and a total, not seventeen things to read.
        """
        findings = [_overflow(f"{i:02d}", 22_224) for i in range(3, 20)]

        result = summarise(findings)

        assert len(result.groups) == 1
        group = result.groups[0]
        assert group.count == 17
        assert group.loss_per_day == 22_224 * 17
        assert "17 villages" in group.headline
        assert group.findings[0].detail in group.headline, "the headline names one of them"

    def test_the_shared_reason_is_said_once_not_forty_five_times(self):
        """45 latency lines ended in the identical clause. The action is the
        clause, hoisted to the group, and it appears exactly once."""
        result = summarise([_latency(f"{i:02d}", 2.0 + i / 10) for i in range(1, 46)])

        assert len(result.groups) == 1
        group = result.groups[0]
        assert group.count == 45
        assert "merchant budget may forbid better" in group.action
        assert group.action.count("merchant budget") == 1

    def test_a_group_of_one_is_its_own_message(self):
        """Aggregating a lone finding into "1 village ..." would say less.

        Its own sentence already names the village and the number, which is
        exactly what the operator needs and what "1 village" throws away.
        """
        lone = _overflow("02", 1_795_200, Resource.CROP)

        group = summarise([lone]).groups[0]

        assert group.headline == lone.message
        assert "1 village" not in group.headline

    def test_two_restricted_hauls_to_one_tribute_do_not_read_as_two_tributes(self):
        """One finding is emitted per (origin, tribute) row, so the subject has
        to be the haul.

        With "tribute" as the subject, N restricted origins feeding the SAME
        target announced N tributes -- sending the operator to look through the
        foreign-target list for a second obligation that is not there. What
        there are two of is expensive hauls out of one restricted village each.
        """
        group = summarise(
            [
                Finding(
                    category=Category.WHITELIST_VS_TRIBUTE,
                    message=f"{origin} is restricted by ship_only_to, but ... Ally-Keep ...",
                    detail=f"{origin} -> Ally-Keep — 4 merchants",
                    village=origin,
                    resource=Resource.CROP,
                )
                for origin in ("03", "11")
            ]
        ).groups[0]

        assert group.count == 2
        assert "2 hauls" in group.headline, group.headline
        assert "tributes" not in group.headline, group.headline

    def test_different_resources_do_not_merge(self):
        """Clay and crop have different fixes and wildly different costs."""
        result = summarise(
            [_overflow("03", 22_224, Resource.CLAY), _overflow("02", 1_795_200, Resource.CROP)]
        )

        assert len(result.groups) == 2
        assert {g.resource for g in result.groups} == {Resource.CLAY, Resource.CROP}

    def test_the_group_key_is_stable_across_runs(self):
        """A UI that remembers which groups were expanded needs an identity that
        does not move when the counts do."""
        first = summarise([_overflow("03", 1)]).groups[0]
        second = summarise([_overflow("03", 1), _overflow("04", 2)]).groups[0]

        assert first.key == second.key == "overflow_structural:clay"


class TestRanking:
    def test_the_biggest_loss_leads(self):
        """A 1.9M/day loss forty times the next biggest must not be findable
        only by reading. It is the first group."""
        result = summarise(
            [
                *[_overflow(f"{i:02d}", 22_224) for i in range(3, 20)],
                _overflow("02", 1_795_200, Resource.CROP),
            ]
        )

        assert result.groups[0].resource is Resource.CROP
        assert result.groups[0].loss_per_day == 1_795_200

    def test_starvation_outranks_waste_however_expensive_the_waste(self):
        """Overflow wastes surplus; starvation destroys troops, and an army
        cannot be re-grown out of a warehouse."""
        starving = Finding(
            category=Category.STARVATION,
            message="04: crop runs out in 1.2h",
            detail="04 — 1.2h left",
            village="04",
            resource=Resource.CROP,
        )

        result = summarise([_overflow("02", 9_000_000, Resource.CROP), starving])

        assert result.groups[0].category is Category.STARVATION
        assert result.groups[1].loss_per_day == 9_000_000, "the waste is still reported"

    def test_notes_sink_below_warnings_and_criticals(self):
        note = Finding(
            category=Category.STORE_FILLING,
            message="19: clay fills its store in 3.6h at +4664/h",
            detail="19 — full in 3.6h",
            village="19",
            resource=Resource.CLAY,
        )

        result = summarise([note, _latency("03", 5.9), _overflow("02", 100)])

        assert [g.severity for g in result.groups] == [
            Severity.CRITICAL,
            Severity.WARNING,
            Severity.NOTE,
        ]

    def test_within_a_group_the_worst_village_is_first(self):
        result = summarise([_overflow("03", 100), _overflow("04", 9_000), _overflow("05", 500)])

        assert [f.village for f in result.groups[0].findings] == ["04", "05", "03"]


class TestTotals:
    def test_the_total_and_the_per_resource_split_are_both_reported(self):
        """The one number that decides whether the operator cares at all."""
        result = summarise(
            [
                _overflow("03", 22_224, Resource.CLAY),
                _overflow("04", 22_224, Resource.CLAY),
                _overflow("02", 1_795_200, Resource.CROP),
            ]
        )

        assert result.total_loss_per_day == 22_224 * 2 + 1_795_200
        assert [(loss.resource, loss.per_day) for loss in result.loss_by_resource] == [
            (Resource.CROP, 1_795_200),
            (Resource.CLAY, 44_448),
        ]

    def test_findings_that_cost_nothing_do_not_inflate_the_total(self):
        """Hours of latency are not resources per day. Totalling them together
        would produce a headline number that means nothing."""
        result = summarise([_latency("03", 5.9), _overflow("02", 1_000)])

        assert result.total_loss_per_day == 1_000

    def test_findings_are_counted_by_severity(self):
        result = summarise([_latency("03", 5.9), _latency("04", 2.1), _overflow("02", 1_000)])

        assert result.counts == {"warning": 2, "critical": 1}


class TestHeadline:
    def test_the_headline_names_the_dominant_loss(self):
        """Five seconds of reading has to land on the 1.9M, not on the 22,224."""
        result = summarise(
            [
                *[_overflow(f"{i:02d}", 22_224) for i in range(3, 20)],
                _overflow("02", 1_795_200, Resource.CROP),
            ]
        )

        assert "2,173,008" in result.headline
        assert "1,795,200" in result.headline
        assert "crop in 02" in result.headline

    def test_a_loss_spread_evenly_is_not_blamed_on_one_village(self):
        """Naming a village that holds a twentieth of the total would imply the
        other nineteen are noise."""
        result = summarise([_overflow(f"{i:02d}", 22_224) for i in range(3, 23)])

        assert "444,480" in result.headline
        assert "in 03" not in result.headline

    def test_a_clean_plan_says_so(self):
        assert summarise([]).headline == "No problems found."

    def test_a_plan_with_only_missed_targets_does_not_claim_waste(self):
        result = summarise([_latency("03", 5.9), _latency("04", 2.1)])

        assert result.total_loss_per_day == 0
        assert "destroys" not in result.headline
        assert "2 targets are missed" in result.headline


class TestTheHeadlineDoesNotBlameThePlanForTheAccount:
    """Every overflow figure comes from replaying production against capacity,
    not from what the plan ships. The old wording -- "This plan destroys N
    resources a day" -- was therefore measurably false: a one-village account,
    where no route is even possible, reported 480,000/day destroyed *by the
    plan*. The losses were real; the causation was invented.
    """

    def _overflow(self, loss: float = 480_000.0) -> Finding:
        return Finding(
            category=Category.OVERFLOW_STRUCTURAL,
            message="01: crop hits the cap",
            detail="01 — 480,000/day",
            village="01",
            resource=Resource.CROP,
            loss_per_day=loss,
        )

    def test_it_attributes_the_loss_to_the_account_not_the_plan(self):
        result = summarise([self._overflow()])
        assert "This account loses" in result.headline
        assert "plan destroys" not in result.headline

    def test_a_plan_that_ships_nothing_says_so_outright(self):
        # The decisive case. Without this the operator hunts for a planning
        # mistake that cannot exist, because there is no plan to be wrong.
        result = summarise([self._overflow()], routes_planned=0)
        assert "ships nothing" in result.headline
        assert "none of that is the plan's doing" in result.headline

    def test_a_plan_with_routes_does_not_claim_it_ships_nothing(self):
        result = summarise([self._overflow()], routes_planned=8)
        assert "ships nothing" not in result.headline
        assert "This account loses" in result.headline

    def test_an_unknown_route_count_stays_silent_about_the_plan(self):
        # Callers that do not know the route set must not have a claim invented
        # for them either way.
        result = summarise([self._overflow()])
        assert "ships nothing" not in result.headline

    def test_the_total_and_the_worst_offender_are_unchanged(self):
        # Only the attribution moved. The numbers were never the problem.
        result = summarise([self._overflow()], routes_planned=0)
        assert "480,000" in result.headline
        assert result.total_loss_per_day == 480_000.0
