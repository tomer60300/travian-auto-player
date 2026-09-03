"""The optimizer's stated invariants, checked on the plan the endpoint returns.

The module docstrings promise these; the unit tests check them one mechanism at
a time. Nothing checked them end-to-end on a corpus, so a promise that holds in
a fixture but breaks in the assembled plan had nowhere to show up. Written from
the 2026-09-02 review, where a sweep of 42 synthetic accounts found zero
violations -- this pins that result so it stays true.

A plan the endpoint REFUSES (an AllocationError, or a 4xx) is a legitimate
outcome for the account that is BUILT to be refused, and only for that one: the
invariants are about any plan that is produced, but "produced no plan" must not
be a way to satisfy them. Which accounts may be refused is named in
``REFUSED_BY_DESIGN`` and asserted per account, so a regression that 400s every
request fails this file instead of passing it by vacuity.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest
from fastapi import HTTPException

from tests.distribution_synthetic import (
    adversarial_accounts,
    case_account,
    random_account,
)
from travian_api.services.distribution.allocation import AllocationError, Resource
from travian_api.services.distribution.night_profile import MATERIALS
from travian_api.web.routes import distribution as dist


def _plan_uncached(request):
    """The DistributionPlan for *request*, or None when the endpoint refused."""
    try:
        return asyncio.run(dist._plan_account(request)).plan
    except AllocationError:
        return None
    except HTTPException as exc:
        assert exc.status_code < 500, f"a refusal must be a 4xx, got {exc.status_code}"
        return None


# Accounts the endpoint is EXPECTED to refuse. Everything else in the corpus
# must produce a plan, and that is asserted per account rather than left to a
# bare `if plan is None: return` -- with only the early return, a regression
# that refused every request left this whole file green.
REFUSED_BY_DESIGN = frozenset({"adv-negative-absolute-target"})


# Sized deliberately. Planning a large synthetic account costs seconds, so this
# corpus is the smallest that still spans the shapes that matter: a dozen random
# accounts, every adversarial one (each exists to provoke a specific edge), and
# the six hand-built cases. The 2026-09-02 review swept 42 accounts with the
# same result; running all of them here roughly doubled the whole suite.
RANDOM_SEEDS = range(12)
DETERMINISM_SEEDS = range(5)


def _corpus():
    for seed in RANDOM_SEEDS:
        yield pytest.param(random_account(seed, with_profiles=False), id=f"random{seed}")
    for account in adversarial_accounts():
        # The fifty-village account exists to test SCALE, which the slow
        # 40-village planner cases already cover; here it cost 34s of a 44s run
        # while adding no shape the others lack.
        if account.name == "adv-fifty-villages":
            continue
        yield pytest.param(account, id=account.name)
    for index in range(6):
        yield pytest.param(case_account(index), id=f"case{index}")


def _determinism_corpus():
    for seed in DETERMINISM_SEEDS:
        yield pytest.param(random_account(seed, with_profiles=False), id=f"random{seed}")


@pytest.mark.slow
@pytest.mark.parametrize("account", list(_corpus()))
class TestStatedInvariantsHoldOnTheAssembledPlan:
    def test_every_stated_invariant_holds(self, account):
        """One plan, every invariant, ALL violations reported together.

        Collected rather than asserted one by one so a single failing account
        shows everything wrong with it at once, and so the account is planned
        once instead of once per invariant -- planning is the expensive part.
        """
        plan = _plan_uncached(account.plan_request)
        expected_refusal = account.name in REFUSED_BY_DESIGN
        assert (plan is None) == expected_refusal, f"{account.name}: " + (
            "the endpoint refused an account it is supposed to plan -- the "
            "invariants below then pass by vacuity"
            if plan is None
            else "planned an account listed as refused by design; if that is now "
            "correct, take it out of REFUSED_BY_DESIGN"
        )
        if plan is None:
            return  # refused by design; nothing to check
        bad: list[str] = []

        # The netting invariant from allocation.py, AMENDED for profile section
        # 5's declared relay tier: a village is a sender OR a receiver of
        # lumber, clay or iron -- EXCEPT a village the operator named with
        # `relay_for`, which forwards the capital's material on, and which may
        # neither be fed by nor feed another relay. Crop is exempt entirely by
        # design (relay hubs receive and forward it wherever the search finds it
        # worth doing), so it is deliberately not checked.
        #
        # Read off the REQUEST, so the exemption is exactly the set the operator
        # declared and not "whatever the plan happens to have forwarded".
        #
        # Exactly one account in this corpus declares one --
        # `adv-declared-material-relay`, where 9002 relays for 9003 and 9004 --
        # and it exists so the exemption is exercised rather than merely
        # written down. Measured: the plan builds all three lumber legs
        # (9001->9002, 9002->9003, 9002->9004) and reports no violation, while
        # the same plan read with the declaration stripped reports
        # "lumber: [9002] both send and receive without being declared relays".
        # So the exemption is not a silencer here: it turns off for the one
        # account it is about and the block below is the unchanged rule for
        # every other account in the corpus. That mutation is pinned as a test
        # in test_distribution_optimizer.py
        # (`test_the_amended_rule_still_catches_an_undeclared_material_hub`),
        # over the same statement of the rule.
        relays = {
            cfg.village_id: set(cfg.relay_for)
            for cfg in account.plan_request.config
            if cfg.relay_for
        }
        for res in MATERIALS:
            edges = {(r.origin, r.destination) for r in plan.rows if r.cargo.get(res, 0) > 0}
            senders = {origin for origin, _ in edges}
            receivers = {destination for _, destination in edges}
            forwarding = senders & receivers
            undeclared = sorted(forwarding - set(relays))
            if undeclared:
                bad.append(
                    f"{res.value}: {undeclared} both send and receive without being declared relays"
                )
            for hub in sorted(forwarding & set(relays)):
                upstream = {o for o, d in edges if d == hub}
                downstream = {d for o, d in edges if o == hub}
                second_hop = (upstream | downstream) & set(relays)
                if second_hop:
                    bad.append(
                        f"{res.value}: relay {hub} is chained to relay(s) "
                        f"{sorted(second_hop)} -- one hop only"
                    )

        for res in (*MATERIALS, Resource.CROP):
            pairs = {(r.origin, r.destination) for r in plan.rows if r.cargo.get(res, 0) > 0}
            twoway = {p for p in pairs if (p[1], p[0]) in pairs}
            if twoway:
                bad.append(f"{res.value}: two-way pairs {sorted(twoway)}")

        # A budget breach the sheet does not name is the one nobody can act on.
        committed: dict[int, int] = defaultdict(int)
        for r in plan.rows:
            committed[r.origin] += r.merchants
        reported = {b.village_id for b in plan.over_budget}
        for vid, n in committed.items():
            spare = plan.spare_merchants.get(vid)
            if spare is not None and n > spare and vid not in reported:
                bad.append(f"village {vid} commits {n} of {spare} spare, not in over_budget")

        if plan.is_feasible:
            if plan.shortfalls:
                bad.append("feasible with shortfalls")
            if plan.over_budget:
                bad.append(
                    f"feasible while over budget: {[b.village_id for b in plan.over_budget]}"
                )
            if plan.over_allocated:
                bad.append(
                    f"feasible while over-allocated: {[r.value for r in plan.over_allocated]}"
                )

        for r in plan.rows:
            if sum(r.cargo.values()) <= 0:
                bad.append(f"{r.origin}->{r.destination} carries nothing")
            if r.cycle_hours <= 0 or 24 % r.cycle_hours:
                bad.append(
                    f"{r.origin}->{r.destination} cycle {r.cycle_hours}h does not divide a day"
                )
            if r.origin == r.destination:
                bad.append(f"self-loop at {r.origin}")

        assert not bad, "\n".join(bad)


@pytest.mark.slow
@pytest.mark.parametrize("account", list(_determinism_corpus()))
def test_the_same_request_always_produces_the_same_sheet(account):
    """Determinism on identical input. (Invariance under RELABELLING is a
    separate, known-open property -- see TestKnownDefects in the audit.)

    A smaller corpus than the invariants above: this needs TWO plans per
    account, which doubles its cost.
    """
    first = _plan_uncached(account.plan_request)
    second = _plan_uncached(account.plan_request)
    # Not "both None passes": every determinism seed is a random account the
    # endpoint plans, so a pair of refusals here is a regression, not agreement.
    assert first is not None and second is not None, (
        f"{account.name}: the endpoint refused a request it plans today"
    )

    def signature(plan):
        return sorted(
            (r.origin, r.destination, tuple(sorted(r.cargo.items())), r.cycle_hours)
            for r in plan.rows
        )

    assert signature(first) == signature(second)
