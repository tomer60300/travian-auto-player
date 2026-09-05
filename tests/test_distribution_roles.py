"""Village roles and role templates: one profile, applied to every village of a kind.

``grep -c role`` over the fifteen distribution modules was 0, and roles are the
vocabulary the operator's profile is written in. Section 1 assigns one to every
village (capital / troops-off / full-off / DEF / feeder, exhaustively); section
2.1 gives ONE consumption profile for FOUR defensive villages; section 5.9 says
role villages may not relay; sections 9.1-9.2 say two of them are permanently
crop-negative *by design*. Without the vocabulary each of those had to be
restated per village, or inferred from whatever the snapshot happened to carry.

Three things follow, and they are why this lives in the backend rather than as a
label the page paints:

* a template is the ONE place a repeated profile is written, so the four DEF
  villages are entered once and cannot drift apart by a typo;
* ``may_relay`` supersedes the crop-sign inference inside the optimizer
  (tests/test_relay_hub_safety.py has that half);
* ``crop_negative_by_design`` moves a finding's severity, which is decided by
  its category in a pure module.

``role=None`` is the whole existing account and must plan exactly as it does
today -- pinned here by comparison and in tests/test_distribution_golden.py by
recorded output.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.findings import Category, Severity
from travian_api.services.distribution.roles import (
    CROP_DRIFT_THRESHOLD,
    Role,
    crop_drift_findings,
    default_may_relay,
    keeps_a_morning_floor,
)
from travian_api.web.routes.distribution import (
    DayCheckRequest,
    ExecuteRequest,
    NightProfileRequest,
    PlanRequest,
    RoleTemplate,
    _resolve_roles,
    post_day_check,
    post_execute,
    post_night_profile,
    post_plan,
)

USER = SimpleNamespace(id=1)

CAPITAL = 20002
DEF = (20011, 20013, 20017, 20019)
FEEDER = 20004

# Profile section 2's defensive profile, verbatim. One set of four numbers for
# four villages is the requirement in one line.
PROFILE_DEF = {"lumber": 8372.0, "clay": 5168.0, "iron": 5809.0, "crop": 2200.0}
MATERIAL_SPEND = {res: rate for res, rate in PROFILE_DEF.items() if res != "crop"}


def _village(vid, name, x, y, *, lumber, clay, iron, crop, merchants=20, cap=400_000):
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


def _snapshot():
    """A capital with real surplus, four DEF villages around it, one feeder.

    The capital's production is deliberately generous and its merchant count
    high: this suite is about which numbers reach which village, and a plan that
    goes infeasible on the merchant budget would answer that question with a
    shortfall instead.
    """
    return [
        _village(
            CAPITAL,
            "02",
            0,
            0,
            lumber=60_000,
            clay=40_000,
            iron=40_000,
            crop=60_000,
            merchants=200,
            cap=2_000_000,
        ),
        *(
            _village(vid, f"{vid - 20000:02d}", x, y, lumber=1500, clay=1400, iron=1300, crop=1200)
            for vid, (x, y) in zip(DEF, ((4, 0), (0, 4), (-4, 0), (0, -4)), strict=True)
        ),
        _village(FEEDER, "04", 2, 2, lumber=3000, clay=3000, iron=3000, crop=3000),
    ]


def _allocations(*, explicit_def=False):
    """The capital absorbs the remainder; the feeder ships everything away.

    With ``explicit_def`` the four defensive villages carry section 2's figures
    as four identical per-village entries -- the data entry roles exist to
    remove, kept here as the control the template must reproduce.
    """
    out = {}
    for resource, rate in PROFILE_DEF.items():
        per = {str(CAPITAL): {"mode": "remainder"}, str(FEEDER): {"mode": "absolute", "value": 0}}
        if explicit_def:
            for vid in DEF:
                per[str(vid)] = {"mode": "absolute", "value": rate}
        out[resource] = per
    return out


def _def_template(**kw):
    template = {
        "allocations": {
            resource: {"mode": "absolute", "value": rate} for resource, rate in PROFILE_DEF.items()
        },
        "consumption": dict(MATERIAL_SPEND),
    }
    template.update(kw)
    return template


def _payload(*, roles=None, explicit_def=False, config=None, **kw):
    payload = {
        "snapshot": _snapshot(),
        "config": config if config is not None else _config(),
        "allocations": _allocations(explicit_def=explicit_def),
        "foreign_targets": [],
    }
    if roles is not None:
        payload["roles"] = roles
    payload.update(kw)
    return payload


def _config(*, def_role="def", extra=None):
    rows = [{"village_id": CAPITAL, "trade_office_level": 19}]
    for vid in DEF:
        row = {"village_id": vid}
        if def_role is not None:
            row["role"] = def_role
        rows.append(row)
    rows.append({"village_id": FEEDER})
    for override in extra or []:
        for row in rows:
            if row["village_id"] == override["village_id"]:
                row.update(override)
                break
        else:  # pragma: no cover - a typo in a fixture, not a behaviour
            raise AssertionError(f"no config row for {override['village_id']}")
    return rows


def _relay_snapshot():
    """A stranded village that cannot afford its own haul, and a midpoint.

    The same shape as tests/test_relay_hub_safety.py's fixture, at the HTTP
    layer and mapped onto this suite's village ids, because ``_snapshot`` cannot
    show a relay at all: its villages sit four fields from the capital, so no
    relay ever prices better than the direct leg and every ``may_relay`` answer
    produces the same route set.

    * 02 is the capital and the sink -- no crop of its own, REMAINDER, and
      enough merchants that the plan never goes infeasible on its budget rather
      than on the thing under test.
    * 04 is stranded 120 fields out with crop to give away and 20 merchants,
      which is not enough to carry it direct.
    * 11 is halfway along that road, so on distance alone it is the hub the
      relay search wants.
    * 13 is solvent and off-axis at the same distance, so refusing 11 leaves the
      optimizer somewhere to go -- without it a passing test could mean no relay
      was possible at all rather than that 11 was correctly refused.
    * 17 and 19 carry no crop allocation, so they are not candidates: naming a
      village in the crop plan is how the operator offers it as infrastructure.
    """
    return [
        _village(
            CAPITAL,
            "02",
            0,
            0,
            lumber=1000,
            clay=1000,
            iron=1000,
            crop=0,
            merchants=200,
            cap=2_000_000,
        ),
        _village(DEF[0], "11", 60, 0, lumber=1000, clay=1000, iron=1000, crop=1200),
        _village(DEF[1], "13", 60, 40, lumber=1000, clay=1000, iron=1000, crop=1200),
        _village(DEF[2], "17", -60, 0, lumber=1000, clay=1000, iron=1000, crop=1200),
        _village(DEF[3], "19", 0, -60, lumber=1000, clay=1000, iron=1000, crop=1200),
        _village(FEEDER, "04", 120, 0, lumber=1000, clay=1000, iron=1000, crop=2400),
    ]


def _relay_payload(*, template_may_relay, village_may_relay=None):
    """A plan over :func:`_relay_snapshot` that turns on one ``may_relay``.

    Crop only, and the template carries no targets or spend: the question is
    which village may FORWARD someone else's crop, and a full defensive profile
    would move the demand as well as the permission.
    """
    extra = (
        []
        if village_may_relay is None
        else [{"village_id": DEF[0], "may_relay": village_may_relay}]
    )
    return PlanRequest.model_validate(
        {
            "snapshot": _relay_snapshot(),
            "config": _config(extra=extra),
            "allocations": {
                "crop": {
                    # The capital absorbs; the other three named villages give
                    # their crop away, which is what puts them in the graph.
                    str(CAPITAL): {"mode": "remainder"},
                    str(DEF[0]): {"mode": "absolute", "value": 0},
                    str(DEF[1]): {"mode": "absolute", "value": 0},
                    str(FEEDER): {"mode": "absolute", "value": 0},
                }
            },
            "foreign_targets": [],
            "roles": {
                "def": _def_template(allocations={}, consumption={}, may_relay=template_may_relay)
            },
            # The relay-safety world: a 401-field map at 12 fields/hour, so 120
            # fields is a long enough haul for the relay to be worth building.
            "map_span": 401,
            "speed_fields_per_hour": 12.0,
        }
    )


def _plan(**kw):
    return asyncio.run(post_plan(PlanRequest.model_validate(_payload(**kw)), USER))


def _target(res, vid, resource):
    return next(
        n.target_per_hour
        for n in res.village_nets
        if n.village_id == vid and n.resource is resource
    )


def _spend(res, vid, resource):
    return next(
        n.consumption_per_hour
        for n in res.village_nets
        if n.village_id == vid and n.resource is resource
    )


class TestOneProfileForFourVillages:
    """Section 2.1's requirement, stated as an equality.

    The point is not that a template is convenient. It is that four identical
    hand-typed profiles are four chances to disagree with each other, and the
    account they came from has four defensive villages the operator maintains as
    one thing. So the template must produce the SAME plan the four entries did,
    or it is a second way to say something rather than a better one.
    """

    def test_entering_the_profile_once_plans_what_entering_it_four_times_did(self):
        templated = _plan(roles={"def": _def_template(consumption={})})
        by_hand = _plan(explicit_def=True, config=_config(def_role=None))

        assert [r.model_dump() for r in templated.rows] == [r.model_dump() for r in by_hand.rows]
        assert templated.total_merchants == by_hand.total_merchants
        assert [n.model_dump() for n in templated.village_nets] == [
            n.model_dump() for n in by_hand.village_nets
        ]

    def test_every_one_of_the_four_gets_the_profile(self):
        res = _plan(roles={"def": _def_template()})

        for vid in DEF:
            for resource in Resource:
                assert _target(res, vid, resource) == pytest.approx(PROFILE_DEF[resource.value]), (
                    f"village {vid} did not take its role's {resource.value} target"
                )

    def test_the_templates_spend_reaches_every_village_of_the_role(self):
        """Consumption travels with the profile, because it IS the profile:
        section 2 calls its figures consumption targets, and a template that
        carried only the retention would leave the operator restating the same
        four numbers under a different heading."""
        res = _plan(roles={"def": _def_template()})

        for vid in DEF:
            assert _spend(res, vid, Resource.LUMBER) == pytest.approx(PROFILE_DEF["lumber"])
            assert _spend(res, vid, Resource.CROP) == 0.0, "crop can never be a declared spend"

    def test_a_village_without_the_role_is_untouched(self):
        res = _plan(roles={"def": _def_template()})

        assert _target(res, FEEDER, Resource.LUMBER) == 0.0
        assert _spend(res, FEEDER, Resource.LUMBER) == 0.0


class TestAnExplicitEntryOverridesTheTemplate:
    """A template is a default, not a cage.

    One of the four defensive villages will always be different -- a smaller
    granary, a building queue running, a wall going up -- and the answer cannot
    be "take it out of the role", because then it loses the relay rule and the
    fill floor along with the four numbers it only wanted to change one of.

    Per RESOURCE, for the same reason: overriding lumber must not silently
    revert clay and iron to whatever the village produces.
    """

    ODD_ONE = DEF[0]

    def _res(self, value=12_000.0):
        allocations = _allocations()
        allocations["lumber"][str(self.ODD_ONE)] = {"mode": "absolute", "value": value}
        return _plan(roles={"def": _def_template()}, allocations=allocations)

    def test_the_explicit_value_wins(self):
        assert _target(self._res(), self.ODD_ONE, Resource.LUMBER) == pytest.approx(12_000.0)

    def test_the_other_resources_still_come_from_the_template(self):
        res = self._res()

        assert _target(res, self.ODD_ONE, Resource.CLAY) == pytest.approx(PROFILE_DEF["clay"])
        assert _target(res, self.ODD_ONE, Resource.IRON) == pytest.approx(PROFILE_DEF["iron"])

    def test_the_other_villages_of_the_role_are_unaffected(self):
        res = self._res()

        for vid in DEF[1:]:
            assert _target(res, vid, Resource.LUMBER) == pytest.approx(PROFILE_DEF["lumber"])

    def test_the_deviation_is_reported_so_the_page_can_mark_it(self):
        """Silently overriding is how a plan comes to disagree with the profile
        the operator believes is running. The response names the cell, both
        figures and the role, so the grid can mark it without recomputing the
        resolution and reaching a different answer."""
        res = self._res()

        deviations = [d for d in res.role_deviations]
        assert len(deviations) == 1, deviations
        only = deviations[0]
        assert only.village_id == self.ODD_ONE
        assert only.village_name == "11"
        assert only.role is Role.DEF
        assert only.resource is Resource.LUMBER
        assert only.template_allocation.value == pytest.approx(PROFILE_DEF["lumber"])
        assert only.village_allocation.value == pytest.approx(12_000.0)

    def test_an_explicit_keep_overrides_the_template_and_is_reported(self):
        """KEEP is a statement, not an absence, and the page depends on it.

        Elsewhere in this module an explicit KEEP means exactly what an absent
        entry means, and the route layer drops it for that reason. A role
        changes that: the alternative to the template is not "nothing", it is
        "hold your own production" -- so a village whose operator picked Keep
        own must keep its own, and the deviation has to be reported or the grid
        shows Keep own while the plan ships the profile.
        """
        allocations = _allocations()
        allocations["lumber"][str(self.ODD_ONE)] = {"mode": "keep", "value": 0}

        res = _plan(roles={"def": _def_template()}, allocations=allocations)

        assert _target(res, self.ODD_ONE, Resource.LUMBER) == pytest.approx(1500.0), (
            "the template filled in over an explicit keep"
        )
        assert [(d.resource, d.village_id) for d in res.role_deviations] == [
            (Resource.LUMBER, self.ODD_ONE)
        ]
        for vid in DEF[1:]:
            assert _target(res, vid, Resource.LUMBER) == pytest.approx(PROFILE_DEF["lumber"])

    def test_an_explicit_value_equal_to_the_template_is_not_a_deviation(self):
        """Otherwise every account that spelled its profile out before templates
        existed would light up with deviations it does not have."""
        assert self._res(value=PROFILE_DEF["lumber"]).role_deviations == []

    def test_a_village_spend_of_its_own_overrides_the_templates(self):
        res = _plan(
            roles={"def": _def_template()},
            config=_config(
                extra=[{"village_id": self.ODD_ONE, "consumption_per_hour": {"lumber": 9_500}}]
            ),
        )

        assert _spend(res, self.ODD_ONE, Resource.LUMBER) == pytest.approx(9_500.0)
        assert _spend(res, self.ODD_ONE, Resource.CLAY) == pytest.approx(PROFILE_DEF["clay"]), (
            "overriding one resource reverted the rest of the template's spend"
        )
        for vid in DEF[1:]:
            assert _spend(res, vid, Resource.LUMBER) == pytest.approx(PROFILE_DEF["lumber"])


class TestTheDeclarationHasToBeComplete:
    """A role naming a template that is not there is refused, not ignored.

    Ignoring it plans the account as though nothing was said: four villages
    revert to keeping their own production, which for a defensive village is a
    tenth of what it needs, and the plan reads as feasible. Both halves of the
    declaration have to arrive or neither is usable.
    """

    def test_a_role_with_no_template_is_a_422_naming_the_village(self):
        with pytest.raises(HTTPException) as caught:
            _plan(roles={"feeder": {}})

        assert caught.value.status_code == 422
        assert "11" in str(caught.value.detail)
        assert "def" in str(caught.value.detail)

    def test_the_message_names_every_village_left_undeclared(self):
        with pytest.raises(HTTPException) as caught:
            _plan()

        detail = str(caught.value.detail)
        for name in ("11", "13", "17", "19"):
            assert name in detail, detail

    def test_an_unknown_role_name_is_a_validation_error(self):
        """Five roles, closed set. "hammer" is what the operator calls village
        01 in conversation and is not one of them, and a silently ignored key
        would be a village with no profile at all."""
        with pytest.raises(ValidationError):
            PlanRequest.model_validate(_payload(config=_config(def_role="hammer")))

    def test_a_template_nobody_uses_is_harmless(self):
        """The five roles are the operator's whole account; carrying a template
        for a role this snapshot has none of is a file being complete, not an
        error."""
        res = _plan(roles={"def": _def_template(), "capital": {"allocations": {}}})

        assert res.feasible


class TestACropSpendIsRefusedInATemplateToo:
    """The P1 ruling binds the template, and at the schema so it binds all four
    planning paths at once.

    Section 2 lists a crop figure per role village, so this is the field an
    operator will reach for -- and ``crop_per_hour`` in the snapshot is already
    net of upkeep, so declaring it here subtracts the same troops twice. The
    crop figure belongs in the template's ALLOCATION, which is a retention, and
    the message has to say so.
    """

    def test_the_schema_refuses_it(self):
        with pytest.raises(ValidationError, match="already net"):
            RoleTemplate.model_validate({"consumption": {"crop": 2200}})

    def test_zero_is_refused_as_well(self):
        with pytest.raises(ValidationError):
            RoleTemplate.model_validate({"consumption": {"crop": 0}})

    def test_the_materials_are_accepted(self):
        template = RoleTemplate.model_validate({"consumption": dict(MATERIAL_SPEND)})

        assert template.consumption == {
            Resource.LUMBER: PROFILE_DEF["lumber"],
            Resource.CLAY: PROFILE_DEF["clay"],
            Resource.IRON: PROFILE_DEF["iron"],
        }

    def test_it_is_refused_on_every_request_model(self):
        for model in (PlanRequest, DayCheckRequest, ExecuteRequest, NightProfileRequest):
            body = _payload(roles={"def": _def_template(consumption={"crop": 2200})})
            if model is not PlanRequest:
                body.pop("allocations")
            with pytest.raises(ValidationError, match="already net"):
                model.model_validate(body)


class TestARemainderIsRefusedInATemplate:
    """Manager ruling #4 at the schema: REMAINDER stays per village.

    Exactly one village per resource absorbs the slack -- a profile shared by
    four defensive villages cannot say which, so a template carrying remainder
    fans it out to every village of the role and the allocation layer then
    refuses the whole plan with a 400 that names VILLAGES ("got 02, 11, 13, 17,
    19"). The operator reads that as five bad cells and has to work back to the
    one template that wrote them.

    Refused at the schema for the reason the crop spend is: one rule for all
    four planning paths, and the error's own location names the role.
    """

    def test_the_schema_refuses_it(self):
        with pytest.raises(ValidationError, match="per village"):
            RoleTemplate.model_validate(
                {"allocations": {"lumber": {"mode": "remainder", "value": 0}}}
            )

    def test_the_message_names_the_resource_it_was_written_on(self):
        with pytest.raises(ValidationError) as caught:
            RoleTemplate.model_validate(
                {
                    "allocations": {
                        "clay": {"mode": "remainder"},
                        "iron": {"mode": "remainder"},
                    }
                }
            )

        detail = str(caught.value)
        assert "clay" in detail and "iron" in detail, detail
        # The Rest radio is where the answer belongs, so the message has to
        # point at it rather than only forbid the mode.
        assert "Rest" in detail, detail

    def test_the_other_four_modes_are_accepted(self):
        for mode, value in (("keep", 0), ("absolute", 8372), ("percentage", 12), ("sustain", 120)):
            template = RoleTemplate.model_validate(
                {"allocations": {"lumber": {"mode": mode, "value": value}}}
            )
            assert template.allocations[Resource.LUMBER].value == pytest.approx(value)

    def test_it_is_refused_on_every_request_model(self):
        for model in (PlanRequest, DayCheckRequest, ExecuteRequest, NightProfileRequest):
            body = _payload(
                roles={"def": _def_template(allocations={"lumber": {"mode": "remainder"}})}
            )
            if model is not PlanRequest:
                body.pop("allocations")
            with pytest.raises(ValidationError, match="per village"):
                model.model_validate(body)

    def test_a_per_village_remainder_is_untouched(self):
        """The template is the only place it is refused. The capital absorbs the
        slack in every fixture here, and must go on doing so."""
        res = _plan(roles={"def": _def_template()})

        assert res.feasible


class TestADesignedDeficitIsANoteNotACritical:
    """Sections 9.1-9.2 at the endpoint: 01 and 03 drain crop on purpose.

    Two of the twenty-seven CRITICAL findings on the operator's own account were
    this, and a red that says "troops are being destroyed" about an account
    running as designed is worse than no red. The hours of cover stay -- that
    figure is the whole of review R7 -- and only the severity moves.
    """

    HAMMER = DEF[0]

    def _res(self, *, by_design):
        """The Hammer's shape: -5,880/h of crop with 100,000 in the granary.

        No crop target at all, which is the state a crop-negative village is
        actually in before anyone has fed it: it keeps its own production, so
        the store moves at -5,880/h and empties in seventeen hours. Section 2's
        crop figure is deliberately NOT set here -- an absolute retention is
        refused below break-even, and the whole point of this class is the
        village that is genuinely draining.
        """
        snapshot = _snapshot()
        for village in snapshot:
            if village["village_id"] == self.HAMMER:
                village["crop_per_hour"] = -5_880.0
                village["crop_stock"] = 100_000
        return _plan(
            snapshot=snapshot,
            roles={
                "def": {
                    "allocations": {},
                    "consumption": {},
                    "crop_negative_by_design": by_design,
                }
            },
        )

    @staticmethod
    def _categories(res, village):
        return {
            f.category
            for group in res.diagnostics.groups
            for f in group.findings
            if f.village == village
        }

    def test_undeclared_it_is_still_critical(self):
        res = self._res(by_design=False)

        assert "starvation" in self._categories(res, "11")
        assert "starvation" in [
            g.category for g in res.diagnostics.groups if g.severity == "critical"
        ]

    def test_declared_it_becomes_a_note_carrying_the_hours(self):
        res = self._res(by_design=True)

        assert "starvation" not in self._categories(res, "11")
        assert "starvation_by_design" in self._categories(res, "11")
        note = next(
            f
            for group in res.diagnostics.groups
            for f in group.findings
            if f.category == "starvation_by_design"
        )
        assert "by design" in note.message
        assert "17.0h" in note.message, note.message
        assert "5,880" in note.message, note.message

    def test_the_severity_is_what_moved_and_nothing_else(self):
        loud = self._res(by_design=False)
        quiet = self._res(by_design=True)

        assert [r.model_dump() for r in loud.rows] == [r.model_dump() for r in quiet.rows]
        assert loud.total_merchants == quiet.total_merchants
        assert quiet.verdict.critical_findings == loud.verdict.critical_findings - 1


class TestTheTwoRoleDefaultsAreDifferentSets:
    """`default_may_relay` and `keeps_a_morning_floor` disagree on the capital,
    and that is the point rather than an inconsistency to tidy away.

    Neither had a case in this file, which is the module's own test file: the
    relay default was reached only through a plan and the morning floor only
    through the day-check endpoint, so the two sets could be quietly aligned
    with the suite green.
    """

    def test_only_a_feeder_relays_by_default(self):
        # The capital would win every relay search it entered -- it is the most
        # central village on the account by construction -- and the tier section
        # 5 asks for would never be built.
        assert default_may_relay(Role.FEEDER) is True
        assert [
            role
            for role in (Role.CAPITAL, Role.DEF, Role.TROOPS_OFF, Role.FULL_OFF)
            if default_may_relay(role)
        ] == []

    def test_the_morning_floor_is_the_narrower_set(self):
        # Section 6: DEF + OFF, capital excluded. A feeder holds nothing by
        # design, so 60% of its warehouse is cargo sitting where it is not
        # wanted.
        assert {role for role in Role if keeps_a_morning_floor(role)} == {
            Role.DEF,
            Role.TROOPS_OFF,
            Role.FULL_OFF,
        }

    def test_the_capital_is_where_the_two_defaults_part(self):
        assert default_may_relay(Role.CAPITAL) is False
        assert keeps_a_morning_floor(Role.CAPITAL) is False
        # ...and the feeder is where they part the other way.
        assert default_may_relay(Role.FEEDER) is True
        assert keeps_a_morning_floor(Role.FEEDER) is False


class TestAVillageMayOverrideItsRolesRelayPermission:
    """PLAN_REVIEW P2 item 2: the override is per VILLAGE, not only per role.

    ``default_may_relay``'s own docstring names the singular case -- "the
    account whose defensive village sits on the only road to a corner of the
    map". One village, not four: putting the override on the template hands the
    permission to every defensive village on the account, and taking that
    village out of the role to say it would cost it the profile's four targets
    and its spend as well.

    Merged village-over-template, per village, beside the consumption merge and
    on the same rule -- a village may state one thing itself and take the rest
    of its role's.
    """

    def _resolved(self, *, template_may_relay, village_may_relay, vid=DEF[0]):
        body = PlanRequest.model_validate(
            _payload(
                roles={"def": _def_template(may_relay=template_may_relay)},
                config=_config(extra=[{"village_id": vid, "may_relay": village_may_relay}]),
            )
        )
        return _resolve_roles(body).may_relay

    def test_a_village_may_relay_where_its_role_may_not(self):
        may_relay = self._resolved(template_may_relay=False, village_may_relay=True)

        assert may_relay[DEF[0]] is True
        # And only that village: the other three keep the profile's answer,
        # which is the whole reason the override is not on the template.
        for vid in DEF[1:]:
            assert may_relay[vid] is False, vid

    def test_a_village_may_be_refused_where_its_role_may(self):
        may_relay = self._resolved(template_may_relay=True, village_may_relay=False)

        assert may_relay[DEF[0]] is False
        for vid in DEF[1:]:
            assert may_relay[vid] is True, vid

    def test_silence_on_the_village_leaves_the_roles_answer(self):
        """Absent is not False. Every account today says nothing here, and the
        role -- or its own default -- has to go on deciding."""
        assert self._resolved(template_may_relay=True, village_may_relay=None)[DEF[0]] is True
        assert self._resolved(template_may_relay=None, village_may_relay=None)[DEF[0]] is None

    def test_a_village_with_no_role_can_still_be_told(self):
        """Otherwise the field would be accepted and silently ignored on the
        one village kind that has no template to fall back on."""
        body = PlanRequest.model_validate(
            _payload(
                roles={"def": _def_template()},
                config=_config(extra=[{"village_id": FEEDER, "may_relay": True}]),
            )
        )

        assert _resolve_roles(body).may_relay[FEEDER] is True

    def test_the_merged_answer_is_what_the_optimizer_is_given(self):
        """Resolved in one place, read in one place. A merge the plan does not
        see is a setting that does nothing.

        Asserted on ``plan.relays``, because that is the only place the answer
        becomes visible. This used to assert ``res.feasible`` alone, which is
        True with the merge reverted and True on the default snapshot -- where
        every village sits four fields from the capital, so no relay ever prices
        better than the direct leg and the whole route set is identical whatever
        ``may_relay`` says. The test could not have failed.

        So it plans the geometry from :func:`_relay_payload` instead, where a
        relay is the only affordable route, and asserts BOTH directions: with
        the village's override the plan relays through it and can be carried
        out; with the template's refusal standing alone nothing is permitted to
        relay and the plan cannot.
        """
        merged = asyncio.run(
            post_plan(_relay_payload(template_may_relay=False, village_may_relay=True), USER)
        )

        # The one village the operator overrode is the hub, and its three
        # siblings -- same role, same refusal from the template -- are not.
        assert {relay.hub for relay in merged.relays} == {DEF[0]}
        assert merged.feasible

        # And the control, which is what makes the assertion above bite: take
        # the override away and the template's `false` is the only answer, so no
        # village of the role may relay. 04 is then left carrying 2,400/h across
        # 120 fields on 20 merchants, which it cannot.
        refused = asyncio.run(post_plan(_relay_payload(template_may_relay=False), USER))

        assert refused.relays == []
        assert not refused.feasible


class TestRolesReachEveryPlanningPath:
    """The standing rule: `/plan`, `/day-check`, `/execute` and `/night-profile`.

    It became four because the night endpoint inherits every planner input from
    `PlanRequest` and SEEDS the other three -- the page writes its derived
    allocations straight into the active profile -- and it does NOT share
    `_plan_account`, which is exactly how it came to ignore a declared spend.
    """

    def test_the_day_check_resolves_the_template_per_segment(self):
        """A profile is a segment's allocations; the roles are the account's.
        So the template fills each segment's gaps, and a segment that states a
        figure itself still wins."""
        payload = _payload(roles={"def": _def_template()})
        allocations = payload.pop("allocations")
        day = asyncio.run(
            post_day_check(
                DayCheckRequest.model_validate(
                    payload
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

        # 8,372 landing against 8,372 spent is a level store, so the whole point
        # is a net of zero: without the template the village keeps its own 1,500
        # and gains it every hour.
        lumber = [
            row
            for row in day.villages
            if row.village_id == DEF[0] and row.resource is Resource.LUMBER
        ]
        assert lumber, "the defensive village has no lumber trajectory"
        # A UNIT of tolerance on a DAILY net, not the template's whole hourly
        # figure: `abs=PROFILE_DEF["lumber"]` was 8,372 against a number that
        # must be zero, so the assertion passed at about 65% template strength
        # -- it would have held while the day check applied a third of the
        # profile. A day's rounding is well under one unit.
        assert lumber[0].daily_net == pytest.approx(0.0, abs=1.0)

    def test_the_execute_dry_run_plans_from_the_same_templates(self):
        """Driven, not merely parsed.

        This used to assert only that `ExecuteRequest` had accepted the role
        and the template -- which is pydantic's own contract, tested above, and
        says nothing about whether /execute PLANS from them. `/execute`
        recomputes the plan server-side from the same inputs precisely because
        it must not trust client-sent rows, so the template has to reach the
        cargo of the routes a live run would create.

        A dry run issues ZERO game requests and resolves no session (it is
        auth-only, like /plan), so the whole path is exercised offline.
        """
        body = ExecuteRequest.model_validate(
            _payload(roles={"def": _def_template()}) | {"dry_run": True, "max_routes_per_run": 50}
        )

        assert {c.role for c in body.config if c.role} == {Role.DEF}
        assert body.roles[Role.DEF].consumption[Resource.LUMBER] == pytest.approx(
            PROFILE_DEF["lumber"]
        )

        res = asyncio.run(post_execute(body, USER))

        assert res.dry_run
        # The capital's leg into 11, whose cargo is the template's retention
        # MINUS what 11 makes: 8,372 - 1,500 lumber, 5,168 - 1,400 clay,
        # 5,809 - 1,300 iron. The cargo is the GAP, not the target, so these
        # figures are the template arriving at the game's own route dialog.
        into_11 = [
            a
            for a in res.actions
            if a.destination == DEF[0] and a.origin == CAPITAL and a.status == "would_create"
        ]
        assert into_11, [(a.origin, a.destination, a.status) for a in res.actions]
        assert dict(into_11[0].cargo) == {
            Resource.CLAY: 3768,
            Resource.IRON: 4509,
            Resource.LUMBER: 6872,
        }

    def test_the_night_derivation_reads_the_template(self):
        """Both halves: the template's day retention feeds the night ceiling,
        and its spend nets the production the night is sized from."""
        base = {
            "snapshot": [
                _village(
                    CAPITAL,
                    "02",
                    22,
                    88,
                    lumber=13_650,
                    clay=1750,
                    iron=1750,
                    crop=66_000,
                    cap=800_000,
                ),
                _village(DEF[0], "11", 13, 72, lumber=1750, clay=1750, iron=1750, crop=4_000),
                _village(DEF[1], "13", 16, 82, lumber=1750, clay=1750, iron=1750, crop=4_000),
            ],
            "allocations": {
                "lumber": {str(CAPITAL): {"mode": "remainder", "value": 0}},
                "crop": {str(CAPITAL): {"mode": "remainder", "value": 0}},
            },
            "dispatch_window": [23 * 60, 7 * 60],
            "foreign_targets": [],
        }
        config = [
            {"village_id": CAPITAL, "trade_office_level": 19},
            {"village_id": DEF[0], "role": "def"},
            {"village_id": DEF[1], "role": "def"},
        ]
        template = {
            "allocations": {"lumber": {"mode": "absolute", "value": 827}},
            "consumption": {"clay": 700},
        }

        def derive(**extra):
            return asyncio.run(
                post_night_profile(
                    NightProfileRequest.model_validate(base | {"config": config} | extra), USER
                )
            )

        bare = derive(config=[{"village_id": CAPITAL, "trade_office_level": 19}])
        templated = derive(roles={"def": template})
        by_hand = asyncio.run(
            post_night_profile(
                NightProfileRequest.model_validate(
                    base
                    | {
                        "config": [
                            {"village_id": CAPITAL, "trade_office_level": 19},
                            {"village_id": DEF[0], "consumption_per_hour": {"clay": 700}},
                            {"village_id": DEF[1], "consumption_per_hour": {"clay": 700}},
                        ],
                        "allocations": base["allocations"]
                        | {
                            "lumber": {
                                str(CAPITAL): {"mode": "remainder", "value": 0},
                                str(DEF[0]): {"mode": "absolute", "value": 827},
                                str(DEF[1]): {"mode": "absolute", "value": 827},
                            }
                        },
                    }
                ),
                USER,
            )
        )

        assert templated.model_dump() == by_hand.model_dump(), (
            "the template and the four hand-typed figures derived different nights"
        )
        assert templated.model_dump() != bare.model_dump(), (
            "the fixture cannot show anything: the template made no difference to "
            "the derivation even when spelled out by hand"
        )
        for vid in (DEF[0], DEF[1]):
            assert templated.allocations[Resource.CLAY][vid].value == pytest.approx(1050), (
                "1,750 produced less 700 spent is 1,050 to keep; the template's "
                "spend never reached the derivation"
            )

    def test_a_template_for_a_village_not_in_the_snapshot_is_refused(self):
        """The same 422 a hand-typed spend gets. A role attached to an id that
        is not being planned is a typo or a chiefed village, and either way the
        profile the operator is reading is not the one being planned."""
        config = _config()
        config.append({"village_id": 999, "role": "def"})

        with pytest.raises(HTTPException) as caught:
            _plan(roles={"def": _def_template()}, config=config)

        assert caught.value.status_code == 422
        assert "999" in str(caught.value.detail)


class TestNothingDeclaredPlansExactlyAsBefore:
    """`role=None` on every village is every account that exists today.

    Both sides of these comparisons run the same code, so they cannot prove the
    absence of a regression on their own -- that is what the frozen fixture in
    tests/test_distribution_golden.py is for. What they do pin is that the
    resolution is INERT when nothing names a role, which is the property the
    golden fixture would only catch after the fact.
    """

    def test_an_empty_roles_map_is_the_same_request(self):
        assert (
            _plan(config=_config(def_role=None)).model_dump()
            == _plan(roles={}, config=_config(def_role=None)).model_dump()
        )

    def test_a_template_nobody_claims_changes_nothing(self):
        assert (
            _plan(config=_config(def_role=None)).model_dump()
            == _plan(roles={"def": _def_template()}, config=_config(def_role=None)).model_dump()
        )

    def test_no_role_means_no_deviations_to_mark(self):
        assert _plan(explicit_def=True, config=_config(def_role=None)).role_deviations == []


class TestTheCropProfileDriftFlag:
    """Section 9's staleness detector on the operator's own flat constants.

    > Consumption profiles are flat constants. Drift is expected between manual
    > updates. Flag any village whose actual net crop deviates >20% from its
    > assumed profile.

    Read together with the paragraph beside it -- "01 and 03 are permanently
    crop-negative by design ... do not treat starvation as an error state" --
    this is a check on the FIGURES, not on the account. So the arithmetic has to
    survive an assumed figure of -5,880/h, and a village sitting exactly where
    its profile says it sits has to stay silent however negative that is.
    """

    @staticmethod
    def _drift(assumed, reads, *, role=Role.FULL_OFF):
        return crop_drift_findings({1: assumed}, {1: reads}, {1: role}, {1: "01"})

    def test_inside_the_threshold_is_silent(self):
        assert self._drift(-5_880.0, -6_000.0) == []

    def test_outside_it_fires_with_the_percentage(self):
        """53% is |-9,000 - (-5,880)| / |-5,880|, and the percentage is the point."""
        finding = self._drift(-5_880.0, -9_000.0)[0]

        assert finding.category is Category.CROP_PROFILE_DRIFT
        assert finding.severity is Severity.WARNING
        assert finding.village == "01"
        assert finding.resource is Resource.CROP
        assert finding.loss_per_day == 0.0
        assert "01" in finding.message
        assert "-5,880" in finding.message, finding.message
        assert "-9,000" in finding.message, finding.message
        assert "53%" in finding.message, finding.message
        assert "53%" in finding.detail, finding.detail

    def test_exactly_twenty_percent_is_inside(self):
        """The spec says "deviates >20%", so the boundary itself is not drift.

        -5,880 x 1.2 = -7,056, and 1,000 x 1.2 = 1,200: both directions of the
        boundary, because a percentage of a negative figure is where this check
        goes wrong.
        """
        assert self._drift(-5_880.0, -7_056.0) == []
        assert self._drift(1_000.0, 1_200.0) == []
        assert self._drift(1_000.0, 800.0) == []

    def test_an_assumed_zero_is_not_divided_by(self):
        """A profile stating 0/h claims the village breaks even, which is a claim.

        There is no percentage against zero, so the finding says so and gives
        the gap instead of printing an infinity or quietly dividing by an
        epsilon.
        """
        assert self._drift(0.0, 0.0) == []

        finding = self._drift(0.0, -5_880.0)[0]
        assert "0/h" in finding.message, finding.message
        assert "-5,880" in finding.message, finding.message
        assert "%" not in finding.message, finding.message
        assert "inf" not in finding.message.lower(), finding.message

    def test_a_sign_flip_is_named_and_not_only_measured(self):
        """-5,880 and +5,880 are the same magnitude and opposite facts.

        A check comparing magnitudes reports 0% here, which is the one drift
        that matters most: the village has stopped needing crop shipped to it.
        """
        finding = self._drift(-5_880.0, 5_880.0)[0]

        assert "200%" in finding.message, finding.message
        assert "sign" in finding.message.lower(), finding.message

    def test_negative_by_design_and_not_drifted_stays_silent(self):
        """01 reads -5,880/h by design. Sitting on its own figure is not drift."""
        assert self._drift(-5_880.0, -5_880.0) == []
        assert (
            crop_drift_findings(
                {1: -5_880.0, 3: -12_526.0},
                {1: -5_880.0, 3: -12_526.0},
                {1: Role.FULL_OFF, 3: Role.TROOPS_OFF},
                {1: "01", 3: "03"},
            )
            == []
        )

    def test_a_village_with_no_assumption_is_not_checked(self):
        """No figure is not a figure of zero, which would fire on the account."""
        assert crop_drift_findings({}, {1: -9_000.0}, {1: Role.FULL_OFF}, {1: "01"}) == []

    def test_an_unreadable_rate_is_not_drift(self):
        """`crop_per_hour=None` is the no-crop-balance snapshot; UNREADABLE_RATE
        already reports it, and a missing reading is not a stale figure."""
        assert self._drift(-5_880.0, None) == []

    def test_the_threshold_is_a_named_constant(self):
        assert CROP_DRIFT_THRESHOLD == 0.20

    def test_it_reaches_the_plan_as_a_warning_and_never_a_blocker(self):
        """The four DEF villages read 1,200/h; the profile claims 100/h."""
        drifted = _plan(roles={"def": _def_template(assumed_crop_per_hour=100.0)})
        agreed = _plan(roles={"def": _def_template(assumed_crop_per_hour=1_200.0)})

        fired = [
            f
            for group in drifted.diagnostics.groups
            for f in group.findings
            if f.category == "crop_profile_drift"
        ]
        assert len(fired) == 4, [f.message for f in fired]
        assert {f.severity for f in fired} == {"warning"}
        assert not any("crop" in b and "drift" in b for b in drifted.verdict.blockers)
        assert drifted.feasible == agreed.feasible
        assert drifted.verdict.critical_findings == agreed.verdict.critical_findings
        assert [r.model_dump() for r in drifted.rows] == [r.model_dump() for r in agreed.rows]

    def test_no_assumed_figure_in_the_template_leaves_the_plan_untouched(self):
        assert (
            _plan(roles={"def": _def_template()}).model_dump()
            == _plan(roles={"def": _def_template(assumed_crop_per_hour=None)}).model_dump()
        )
