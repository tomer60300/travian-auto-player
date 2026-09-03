"""Seeded synthetic accounts for the resource-distribution planner audit.

Every account is a pure function of one integer seed, so any failure reproduces
from that integer alone. Village ids and names are stand-ins (V01..V50 /
9000-block ids) -- nothing here touches a real account.

Three families:

* :func:`random_account` -- 50 seeded random accounts, 20 of which carry
  multiple allocation profiles tiling the full 24h.
* :func:`adversarial_accounts` -- deliberately constructed boundaries: a
  production rate exactly equal to the remaining capacity, merchants committed
  exactly equal to the free ones, a round trip exactly one cycle long, tied
  suppliers, a one-minute profile, a window wrapping midnight, one village,
  every village crop-negative, no free merchants, no capacities at all, and
  fifty villages.
* :func:`case_account` -- one uniform mid-sized shape, used by the audit's
  mutation matrix so the accounts cannot be told apart by their shape.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from travian_api.services.distribution.allocation import AllocationMode, Resource
from travian_api.web.routes.distribution import (
    MAX_DAY_SEGMENTS,
    AllocationInput,
    DayCheckRequest,
    DaySegmentInput,
    ForeignTarget,
    PlanRequest,
    VillageConfig,
    VillageSnapshot,
)

MINUTES_PER_DAY = 1440

# Stand-in ids: a 9000 block that cannot collide with a real village id.
BASE_ID = 9000

MATERIALS = (Resource.LUMBER, Resource.CLAY, Resource.IRON)


@dataclass
class Account:
    """One synthetic account, ready to hand to /plan and /day-check."""

    name: str
    seed: int
    plan_request: PlanRequest
    day_request: DayCheckRequest | None = None
    intent: dict[str, object] = field(default_factory=dict)
    """What the generator was trying to express, for the review to judge against."""


# ---------------------------------------------------------------------------
# village state
# ---------------------------------------------------------------------------


def _marketplace_merchants(rng: random.Random) -> tuple[int, int]:
    """(total, free). Marketplace level -> merchant slots; free <= total."""
    level = 20 if rng.random() < 0.75 else rng.choice([8, 11, 13, 16, 19])
    total = level
    if rng.random() < 0.12:
        free = 0
    else:
        free = rng.randint(max(0, total - 6), total)
    return total, free


def _capacity(rng: random.Random, low: int, high: int) -> int | None:
    if rng.random() < 0.12:
        return None
    return rng.randrange(low, high, 1000)


def _village(rng: random.Random, index: int, spread: int) -> VillageSnapshot:
    total, free = _marketplace_merchants(rng)
    warehouse = _capacity(rng, 20_000, 900_000)
    granary = _capacity(rng, 20_000, 900_000)
    # Materials are always positive. Crop may be negative -- an army village
    # eats more than its fields grow, which is the case worth exercising.
    crop: float | None
    roll = rng.random()
    if roll < 0.30:
        crop = -float(rng.randrange(500, 14_000, 100))
    elif roll < 0.36:
        crop = None  # rate could not be derived; never silently zero
    else:
        crop = float(rng.randrange(200, 12_000, 100))
    return VillageSnapshot(
        village_id=BASE_ID + index,
        name=f"V{index:02d}",
        x=rng.randint(-spread, spread),
        y=rng.randint(-spread, spread),
        merchants_total=total,
        merchants_free=free,
        lumber_per_hour=float(rng.randrange(300, 11_000, 100)),
        clay_per_hour=float(rng.randrange(300, 11_000, 100)),
        iron_per_hour=float(rng.randrange(300, 11_000, 100)),
        crop_per_hour=crop,
        crop_stock=rng.randrange(0, granary if granary else 200_000),
        crop_draining=bool(crop is not None and crop < 0),
        lumber_stock=rng.randrange(0, warehouse if warehouse else 200_000),
        clay_stock=rng.randrange(0, warehouse if warehouse else 200_000),
        iron_stock=rng.randrange(0, warehouse if warehouse else 200_000),
        warehouse_capacity=warehouse,
        granary_capacity=granary,
    )


# ---------------------------------------------------------------------------
# allocations
# ---------------------------------------------------------------------------


def _rate(village: VillageSnapshot, resource: Resource) -> float | None:
    return {
        Resource.LUMBER: village.lumber_per_hour,
        Resource.CLAY: village.clay_per_hour,
        Resource.IRON: village.iron_per_hour,
        Resource.CROP: village.crop_per_hour,
    }[resource]


def _stock(village: VillageSnapshot, resource: Resource) -> int:
    return {
        Resource.LUMBER: village.lumber_stock,
        Resource.CLAY: village.clay_stock,
        Resource.IRON: village.iron_stock,
        Resource.CROP: village.crop_stock,
    }[resource]


def _cap(village: VillageSnapshot, resource: Resource) -> int | None:
    return village.granary_capacity if resource is Resource.CROP else village.warehouse_capacity


def _headroom(village: VillageSnapshot, resource: Resource) -> float:
    cap = _cap(village, resource)
    if cap is None:
        return float("inf")
    return max(0.0, cap - _stock(village, resource))


def _known(snapshot: list[VillageSnapshot], resource: Resource) -> list[VillageSnapshot]:
    return [v for v in snapshot if _rate(v, resource) is not None]


def _ordinary_allocations(
    rng: random.Random,
    snapshot: list[VillageSnapshot],
    *,
    over_allocate: bool,
) -> dict[Resource, dict[int, AllocationInput]]:
    """A daytime profile: a remainder village, plus a mix of the other modes."""
    out: dict[Resource, dict[int, AllocationInput]] = {}
    for resource in Resource:
        known = _known(snapshot, resource)
        if not known:
            continue
        total = sum(float(_rate(v, resource)) for v in known)
        # The remainder village is the one with the most room for slack.
        remainder = max(known, key=lambda v: (_headroom(v, resource), v.village_id))
        per: dict[int, AllocationInput] = {
            remainder.village_id: AllocationInput(mode=AllocationMode.REMAINDER)
        }
        for village in known:
            if village.village_id == remainder.village_id:
                continue
            own = float(_rate(village, resource))
            roll = rng.random()
            if own < 0:
                # A starving village: sustain is the mode this exists for.
                per[village.village_id] = AllocationInput(
                    mode=AllocationMode.SUSTAIN, value=float(rng.choice([0, 5, 10, 13, 25]))
                )
            elif roll < 0.12 and total > 0:
                # Deliberately also set sustain on a POSITIVE producer, which is
                # a no-op the planner must report rather than silently honour.
                per[village.village_id] = AllocationInput(
                    mode=AllocationMode.SUSTAIN, value=float(rng.choice([0, 10]))
                )
            elif roll < 0.45 and total > 0:
                per[village.village_id] = AllocationInput(
                    mode=AllocationMode.PERCENTAGE,
                    value=min(100.0, max(0.0, own / total * 100.0 * rng.uniform(0.2, 1.4))),
                )
            elif roll < 0.80:
                factor = rng.uniform(1.2, 3.0) if over_allocate else rng.uniform(0.0, 1.1)
                per[village.village_id] = AllocationInput(
                    mode=AllocationMode.ABSOLUTE, value=max(0.0, own * factor)
                )
            elif roll < 0.90:
                per[village.village_id] = AllocationInput(mode=AllocationMode.KEEP)
            # else: absent, which means KEEP
        if not over_allocate:
            _fit_within_production(per, known, resource, total)
        out[resource] = per
    return out


def _fit_within_production(
    per: dict[int, AllocationInput],
    known: list[VillageSnapshot],
    resource: Resource,
    total: float,
) -> None:
    """Scale the flexible targets down until the remainder village is in credit.

    Without this almost every generated account over-allocates by accident, so
    ``feasible`` is False everywhere and carries no information at all. The
    accounts that over-allocate deliberately skip this, which is what makes
    over-allocation a signal rather than the background.
    """
    if total <= 0:
        return
    fixed = 0.0
    flexible = 0.0
    flexible_ids: list[int] = []
    for village in known:
        item = per.get(village.village_id)
        own = float(_rate(village, resource))
        if item is None or item.mode is AllocationMode.KEEP:
            fixed += own
        elif item.mode is AllocationMode.REMAINDER:
            continue
        elif item.mode is AllocationMode.SUSTAIN:
            fixed += own if own >= 0 else -own * item.value / 100.0
        else:
            target = (
                total * item.value / 100.0 if item.mode is AllocationMode.PERCENTAGE else item.value
            )
            flexible += target
            flexible_ids.append(village.village_id)
    allowed = total * 0.95 - fixed
    if flexible <= allowed or flexible <= 0:
        return
    scale = max(0.0, allowed) / flexible
    for vid in flexible_ids:
        item = per[vid]
        per[vid] = AllocationInput(mode=item.mode, value=item.value * scale)


def _night_allocations(
    snapshot: list[VillageSnapshot],
) -> dict[Resource, dict[int, AllocationInput]]:
    """Night: nothing may overflow.

    Every store keeps what it makes, so no receiver's stock climbs on shipped
    cargo, except the starving villages -- which must still be fed or troops
    die -- and the remainder village, chosen as the one with the most headroom
    so the unavoidable slack lands where there is room for it.
    """
    out: dict[Resource, dict[int, AllocationInput]] = {}
    for resource in Resource:
        known = _known(snapshot, resource)
        if not known:
            continue
        remainder = max(known, key=lambda v: (_headroom(v, resource), v.village_id))
        per: dict[int, AllocationInput] = {
            remainder.village_id: AllocationInput(mode=AllocationMode.REMAINDER)
        }
        for village in known:
            if village.village_id == remainder.village_id:
                continue
            own = float(_rate(village, resource))
            if own < 0:
                per[village.village_id] = AllocationInput(mode=AllocationMode.SUSTAIN, value=5.0)
            else:
                per[village.village_id] = AllocationInput(mode=AllocationMode.KEEP)
        out[resource] = per
    return out


def _fill_allocations(
    snapshot: list[VillageSnapshot], hub_rank: int
) -> dict[Resource, dict[int, AllocationInput]]:
    """Start/end of day: pack the stores as full as they will go.

    Everything is shipped to one hub (``hub_rank``-th emptiest store), which is
    the operator's stated intent for these short windows: arrive at the night
    with the hub as full as it can be.
    """
    out: dict[Resource, dict[int, AllocationInput]] = {}
    for resource in Resource:
        known = _known(snapshot, resource)
        if not known:
            continue
        ordered = sorted(known, key=lambda v: (-_headroom(v, resource), v.village_id))
        hub = ordered[min(hub_rank, len(ordered) - 1)]
        per: dict[int, AllocationInput] = {
            hub.village_id: AllocationInput(mode=AllocationMode.REMAINDER)
        }
        for village in known:
            if village.village_id == hub.village_id:
                continue
            own = float(_rate(village, resource))
            if own < 0:
                per[village.village_id] = AllocationInput(mode=AllocationMode.SUSTAIN, value=0.0)
            else:
                # Ship everything out: keep nothing here, it all goes to the hub.
                per[village.village_id] = AllocationInput(mode=AllocationMode.ABSOLUTE, value=0.0)
        out[resource] = per
    return out


# ---------------------------------------------------------------------------
# profile windows
# ---------------------------------------------------------------------------

PROFILE_SHAPES = ("night8", "night7_start2", "night7_start2_end1")


def profile_windows(shape: str) -> list[tuple[str, tuple[int, int]]]:
    """Windows tiling the full 24h with no gap and no overlap.

    The request model only rejects OVERLAPS, so the no-gap half is the
    generator's own responsibility and is asserted by :func:`assert_tiles_day`.
    """
    if shape == "night8":
        # 22:00-06:00 night, 06:00-22:00 daytime.
        return [("night", (22 * 60, 6 * 60)), ("daytime", (6 * 60, 22 * 60))]
    if shape == "night7_start2":
        # 23:00-06:00 night, 06:00-08:00 startday, 08:00-23:00 daytime.
        return [
            ("night", (23 * 60, 6 * 60)),
            ("startday", (6 * 60, 8 * 60)),
            ("daytime", (8 * 60, 23 * 60)),
        ]
    if shape == "night7_start2_end1":
        # 23:00-06:00 night, 06:00-08:00 startday, 08:00-22:00 daytime,
        # 22:00-23:00 endday.
        return [
            ("night", (23 * 60, 6 * 60)),
            ("startday", (6 * 60, 8 * 60)),
            ("daytime", (8 * 60, 22 * 60)),
            ("endday", (22 * 60, 23 * 60)),
        ]
    raise ValueError(f"unknown profile shape {shape!r}")


def assert_tiles_day(windows: list[tuple[int, int]]) -> None:
    """Every minute of the day covered exactly once."""
    seen: dict[int, int] = {}
    for start, end in windows:
        span = range(start, end) if start < end else [*range(start, MINUTES_PER_DAY), *range(end)]
        for minute in span:
            seen[minute] = seen.get(minute, 0) + 1
    missing = [m for m in range(MINUTES_PER_DAY) if seen.get(m, 0) == 0]
    doubled = [m for m, n in seen.items() if n > 1]
    if missing or doubled:
        raise AssertionError(
            f"windows do not tile the day: {len(missing)} uncovered minutes, "
            f"{len(doubled)} covered twice"
        )


def _segments(
    shape: str, snapshot: list[VillageSnapshot], rng: random.Random
) -> list[DaySegmentInput]:
    windows = profile_windows(shape)
    assert_tiles_day([w for _n, w in windows])
    segments = []
    for name, window in windows:
        if name == "night":
            allocations = _night_allocations(snapshot)
        elif name == "startday":
            allocations = _fill_allocations(snapshot, hub_rank=0)
        elif name == "endday":
            allocations = _fill_allocations(snapshot, hub_rank=1)
        else:
            allocations = _ordinary_allocations(rng, snapshot, over_allocate=False)
        segments.append(DaySegmentInput(name=name, window=window, allocations=allocations))
    assert len(segments) <= MAX_DAY_SEGMENTS
    return segments


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


def _consumption(rng: random.Random, snapshot: list[VillageSnapshot]) -> dict[int, dict]:
    """What some of these villages SPEND per hour, by resource.

    The operator's own flat constants, and a dimension the audit needs: the
    storage replays subtract it, so without it every oracle-agreement run
    checks only the zero case. Deliberately spans both sides of production --
    a share of 1.4 spends more than the village makes -- so the draining branch
    is exercised on a village whose production reads POSITIVE, which is the case
    that misclassifies if the sign is taken off production instead of the net.

    MATERIALS only, matching the schema: crop is refused because the snapshot's
    `crop_per_hour` is net of upkeep already (R3-D1), so a generated crop spend
    would 422 two fifths of these seeds rather than exercise anything.

    Crop is dropped at the STORE, not at the loop, and that is deliberate. The
    loop still runs over every `Resource` and still draws crop's `rng.choice`,
    so the stream of draws is exactly the one that generated this corpus before
    crop became undeclarable. Looping over `MATERIALS` instead took three draws
    per village where there had been four and re-rolled the MATERIAL figures of
    30 of the 80 seed/profile combinations -- a silent corpus re-roll, which
    this module's own docstring forbids: a seed name has to keep meaning the
    same account, or the audit's history stops describing anything.
    """
    if rng.random() < 0.6:
        return {}  # most accounts declare nothing, which must stay the quiet path
    out: dict[int, dict] = {}
    for village in snapshot:
        if rng.random() < 0.5:
            continue
        per = {}
        for resource in Resource:
            rate = _rate(village, resource)
            if rate is None:
                continue  # an unreadable rate sits its resource out entirely
            share = rng.choice([0.0, 0.25, 0.5, 1.0, 1.4])
            if share and resource is not Resource.CROP:
                per[resource] = round(abs(rate) * share, 1)
        if per:
            out[village.village_id] = per
    return out


def random_account(seed: int, *, with_profiles: bool) -> Account:
    """One seeded random account. Everything derives from *seed*."""
    rng = random.Random(seed)
    count = rng.randint(1, 50)
    spread = rng.choice([12, 40, 90, 190])
    snapshot = [_village(rng, i + 1, spread) for i in range(count)]
    # Drawn from its OWN stream, keyed off the same seed. Taking these numbers
    # from `rng` would shift every later draw and re-roll the whole audit
    # corpus -- different allocations, reserves and latency targets under the
    # same seed names -- so the new dimension would have silently replaced the
    # coverage it was meant to add to.
    consumption = _consumption(random.Random(f"consumption-{seed}"), snapshot)
    config = [
        VillageConfig(
            village_id=v.village_id,
            trade_office_level=rng.randint(0, 20),
            consumption_per_hour=consumption.get(v.village_id),
        )
        for v in snapshot
    ]
    over_allocate = rng.random() < 0.20
    tributes: list[ForeignTarget] = []
    if rng.random() < 0.25:
        tributes.append(
            ForeignTarget(
                name="ally-hub",
                x=rng.randint(-spread, spread),
                y=rng.randint(-spread, spread),
                crop_per_hour=float(rng.randrange(500, 6000, 100)),
                safety_margin_pct=float(rng.choice([0, 5, 10])),
                route_eligible=rng.random() < 0.5,
            )
        )
    plan_request = PlanRequest(
        snapshot=snapshot,
        config=config,
        foreign_targets=tributes,
        allocations=_ordinary_allocations(rng, snapshot, over_allocate=over_allocate),
        merchant_reserve=rng.choice([0, 1, 2, 2, 3]),
        max_latency_hours=rng.choice([None, 1.0, 2.0, 2.0, 4.0]),
        min_arrival_gap_minutes=rng.choice([0, 3, 3, 15, 90]),
        reserved_window=None if rng.random() < 0.7 else (rng.randrange(0, 1300), 0),
    )
    day_request = None
    if with_profiles:
        shape = PROFILE_SHAPES[seed % len(PROFILE_SHAPES)]
        day_request = DayCheckRequest(
            snapshot=snapshot,
            config=config,
            foreign_targets=tributes,
            merchant_reserve=plan_request.merchant_reserve,
            max_latency_hours=plan_request.max_latency_hours,
            min_arrival_gap_minutes=plan_request.min_arrival_gap_minutes,
            segments=_segments(shape, snapshot, rng),
            crop_ceilings={
                v.village_id: float(int((v.granary_capacity or 200_000) * 0.75))
                for v in snapshot
                if rng.random() < 0.3
            },
        )
    return Account(
        name=f"random-{seed:03d}",
        seed=seed,
        plan_request=plan_request,
        day_request=day_request,
        intent={
            "villages": count,
            "spread": spread,
            "over_allocate": over_allocate,
            "profiles": (day_request and [s.name for s in day_request.segments]) or [],
            "tributes": len(tributes),
            "consuming": len(consumption),
        },
    )


def random_accounts(count: int = 50, *, profile_count: int = 20) -> list[Account]:
    """*count* seeded accounts; the first *profile_count* also carry profiles.

    The profile shapes rotate on ``seed % 3``, so the three shapes are split
    roughly evenly across the 20.
    """
    return [random_account(seed, with_profiles=seed < profile_count) for seed in range(count)]


# ---------------------------------------------------------------------------
# adversarial cases: boundaries random sampling will not find
# ---------------------------------------------------------------------------


def _snap(
    index: int,
    x: int,
    y: int,
    *,
    merchants: int = 20,
    free: int | None = None,
    lumber: float = 1000.0,
    clay: float = 1000.0,
    iron: float = 1000.0,
    crop: float | None = 1000.0,
    warehouse: int | None = 400_000,
    granary: int | None = 400_000,
    stock: int = 0,
) -> VillageSnapshot:
    return VillageSnapshot(
        village_id=BASE_ID + index,
        name=f"V{index:02d}",
        x=x,
        y=y,
        merchants_total=merchants,
        merchants_free=merchants if free is None else free,
        lumber_per_hour=lumber,
        clay_per_hour=clay,
        iron_per_hour=iron,
        crop_per_hour=crop,
        crop_stock=stock,
        crop_draining=bool(crop is not None and crop < 0),
        lumber_stock=stock,
        clay_stock=stock,
        iron_stock=stock,
        warehouse_capacity=warehouse,
        granary_capacity=granary,
    )


def _absolute(vid: int, value: float) -> dict[int, AllocationInput]:
    return {vid: AllocationInput(mode=AllocationMode.ABSOLUTE, value=value)}


def adversarial_accounts() -> list[Account]:
    """Boundaries built by hand. Each one names the boundary it sits on."""
    out: list[Account] = []

    # 1. Production exactly equal to remaining capacity, over one day.
    #    24 * 1000 = 24,000 of headroom, 1000/h of production.
    a = [
        _snap(1, 0, 0, lumber=1000.0, warehouse=100_000, stock=76_000),
        _snap(2, 3, 0, lumber=1000.0, warehouse=100_000, stock=0),
    ]
    out.append(
        Account(
            name="adv-production-equals-headroom",
            seed=-1,
            plan_request=PlanRequest(
                snapshot=a,
                config=[VillageConfig(village_id=v.village_id) for v in a],
                allocations={
                    Resource.LUMBER: {
                        a[0].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                        **_absolute(a[1].village_id, 0.0),
                    }
                },
            ),
            intent={"boundary": "day of production exactly fills the remaining warehouse"},
        )
    )

    # 2. Merchants committed exactly equal to merchants_free.
    #    2200 capacity at TO 0, 1h cycle, adjacent villages: 1 merchant/send,
    #    1 set -> 1 merchant. Free set to exactly that.
    b = [
        _snap(1, 0, 0, merchants=3, free=1, lumber=2200.0, clay=0.0, iron=0.0, crop=0.0),
        _snap(2, 1, 0, merchants=3, free=3, lumber=0.0, clay=0.0, iron=0.0, crop=0.0),
    ]
    out.append(
        Account(
            name="adv-merchants-exactly-free",
            seed=-2,
            plan_request=PlanRequest(
                snapshot=b,
                config=[VillageConfig(village_id=v.village_id) for v in b],
                merchant_reserve=2,
                allocations={
                    Resource.LUMBER: {
                        **_absolute(b[0].village_id, 0.0),
                        b[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={"boundary": "committed merchants exactly equal merchants_free"},
        )
    )

    # 3. Round trip exactly equal to the cycle length. 12 fields/h, 6 fields
    #    apart -> 30 min one way, 60 min round trip == a 1h cycle.
    c = [
        _snap(1, 0, 0, lumber=500.0, clay=0.0, iron=0.0, crop=0.0),
        _snap(2, 6, 0, lumber=0.0, clay=0.0, iron=0.0, crop=0.0),
    ]
    out.append(
        Account(
            name="adv-roundtrip-equals-cycle",
            seed=-3,
            plan_request=PlanRequest(
                snapshot=c,
                config=[VillageConfig(village_id=v.village_id) for v in c],
                allocations={
                    Resource.LUMBER: {
                        **_absolute(c[0].village_id, 0.0),
                        c[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={"boundary": "round trip exactly one cycle -> sets_in_flight boundary"},
        )
    )

    # 4. Two identical candidate suppliers -- a tie in the optimiser objective.
    d = [
        _snap(1, -5, 0, lumber=3000.0, clay=0.0, iron=0.0, crop=0.0),
        _snap(2, 5, 0, lumber=3000.0, clay=0.0, iron=0.0, crop=0.0),
        _snap(3, 0, 0, lumber=0.0, clay=0.0, iron=0.0, crop=0.0),
    ]
    out.append(
        Account(
            name="adv-tied-suppliers",
            seed=-4,
            plan_request=PlanRequest(
                snapshot=d,
                config=[VillageConfig(village_id=v.village_id) for v in d],
                allocations={
                    Resource.LUMBER: {
                        **_absolute(d[0].village_id, 0.0),
                        **_absolute(d[1].village_id, 0.0),
                        d[2].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={"boundary": "two suppliers identical in distance and surplus"},
        )
    )

    # 5. A one-minute profile window, and 6. a window wrapping midnight.
    e = [
        _snap(1, 0, 0, lumber=4000.0, crop=2000.0),
        _snap(2, 20, 10, lumber=100.0, crop=-3000.0),
    ]
    e_config = [VillageConfig(village_id=v.village_id) for v in e]
    one_minute = DayCheckRequest(
        snapshot=e,
        config=e_config,
        segments=[
            DaySegmentInput(
                name="blink",
                window=(0, 1),
                allocations=_ordinary_allocations(random.Random(11), e, over_allocate=False),
            ),
            DaySegmentInput(
                name="rest",
                window=(1, 0),
                allocations=_ordinary_allocations(random.Random(12), e, over_allocate=False),
            ),
        ],
    )
    assert_tiles_day([(0, 1), (1, 0)])
    out.append(
        Account(
            name="adv-one-minute-window",
            seed=-5,
            plan_request=PlanRequest(snapshot=e, config=e_config),
            day_request=one_minute,
            intent={"boundary": "a 1-minute profile plus a 1439-minute one wrapping midnight"},
        )
    )

    # 7. One village. Nothing can move; the plan must say so, not invent a route.
    f = [_snap(1, 0, 0, crop=-5000.0, stock=10_000)]
    out.append(
        Account(
            name="adv-single-village",
            seed=-7,
            plan_request=PlanRequest(
                snapshot=f,
                config=[VillageConfig(village_id=f[0].village_id)],
                allocations={
                    Resource.CROP: {
                        f[0].village_id: AllocationInput(mode=AllocationMode.SUSTAIN, value=10.0)
                    }
                },
            ),
            intent={"boundary": "one village: a starving account with nowhere to ship from"},
        )
    )

    # 8. Every village crop-negative: the account cannot feed itself.
    g = [_snap(i, i * 7 - 20, 0, crop=-2000.0 - 500 * i, stock=50_000) for i in range(1, 7)]
    out.append(
        Account(
            name="adv-all-crop-negative",
            seed=-8,
            plan_request=PlanRequest(
                snapshot=g,
                config=[VillageConfig(village_id=v.village_id) for v in g],
                allocations={
                    Resource.CROP: {
                        v.village_id: AllocationInput(mode=AllocationMode.SUSTAIN, value=5.0)
                        for v in g
                    }
                },
            ),
            intent={"boundary": "no crop surplus anywhere; percentage mode is illegal here"},
        )
    )

    # 9. Zero free merchants everywhere.
    h = [
        _snap(1, 0, 0, merchants=20, free=0, lumber=5000.0),
        _snap(2, 30, 30, merchants=20, free=0, lumber=100.0),
    ]
    out.append(
        Account(
            name="adv-zero-free-merchants",
            seed=-9,
            plan_request=PlanRequest(
                snapshot=h,
                config=[VillageConfig(village_id=v.village_id) for v in h],
                allocations={
                    Resource.LUMBER: {
                        **_absolute(h[0].village_id, 0.0),
                        h[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={"boundary": "the plan needs merchants that are all busy right now"},
        )
    )

    # 10. All capacities None: storage checks must skip, never guess.
    i = [
        _snap(1, 0, 0, warehouse=None, granary=None, lumber=6000.0, crop=-4000.0),
        _snap(2, 8, 8, warehouse=None, granary=None, lumber=200.0, crop=9000.0),
    ]
    out.append(
        Account(
            name="adv-no-capacities",
            seed=-10,
            plan_request=PlanRequest(
                snapshot=i,
                config=[VillageConfig(village_id=v.village_id) for v in i],
                allocations={
                    Resource.CROP: {
                        i[0].village_id: AllocationInput(mode=AllocationMode.SUSTAIN, value=10.0),
                        i[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    },
                    Resource.LUMBER: {
                        **_absolute(i[0].village_id, 0.0),
                        i[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    },
                },
            ),
            intent={"boundary": "no capacity was ever read; nothing may be assumed"},
        )
    )

    # 11. Fifty villages, spread wide, tight merchants -- the size where relay
    #     and over-budget escalation actually matter.
    rng = random.Random(4242)
    big = [
        _snap(
            n,
            rng.randint(-180, 180),
            rng.randint(-180, 180),
            merchants=rng.choice([8, 13, 20]),
            lumber=float(rng.randrange(2000, 12_000, 100)),
            clay=float(rng.randrange(2000, 12_000, 100)),
            iron=float(rng.randrange(2000, 12_000, 100)),
            crop=(
                -float(rng.randrange(2000, 12_000, 100))
                if rng.random() < 0.4
                else float(rng.randrange(500, 12_000, 100))
            ),
            warehouse=400_000,
            granary=400_000,
            stock=rng.randrange(0, 380_000),
        )
        for n in range(1, 51)
    ]
    out.append(
        Account(
            name="adv-fifty-villages",
            seed=-11,
            plan_request=PlanRequest(
                snapshot=big,
                config=[
                    VillageConfig(village_id=v.village_id, trade_office_level=rng.randint(0, 6))
                    for v in big
                ],
                allocations=_ordinary_allocations(random.Random(99), big, over_allocate=False),
                max_latency_hours=2.0,
            ),
            intent={"boundary": "50 villages, 360-field spread, low Trade Offices"},
        )
    )

    # 12. A search deliberately truncated: the planner must say the figures
    #     below may overstate the shortfall.
    out.append(
        Account(
            name="adv-truncated-search",
            seed=-12,
            plan_request=PlanRequest(
                snapshot=big,
                config=[VillageConfig(village_id=v.village_id) for v in big],
                allocations=_ordinary_allocations(random.Random(99), big, over_allocate=False),
                max_improve_passes=1,
            ),
            intent={"boundary": "max_improve_passes=1 -> the truncation warning must fire"},
        )
    )

    # 13. Over-allocated on purpose: the remainder village would have to ship
    #     more than it makes.
    j = [
        _snap(1, 0, 0, lumber=1000.0),
        _snap(2, 4, 0, lumber=1000.0),
    ]
    out.append(
        Account(
            name="adv-over-allocated",
            seed=-13,
            plan_request=PlanRequest(
                snapshot=j,
                config=[VillageConfig(village_id=v.village_id) for v in j],
                allocations={
                    Resource.LUMBER: {
                        **_absolute(j[0].village_id, 5000.0),
                        j[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={"boundary": "explicit targets exceed production by 3,000/h"},
        )
    )

    # 14. Crop relay: three feeders that cannot staff their own long haul, plus a
    #     midway village with spare merchants. This is the shape relay exists for.
    relay = [
        _snap(1, -150, 0, merchants=4, lumber=0.0, clay=0.0, iron=0.0, crop=9000.0, stock=100_000),
        _snap(2, -150, 6, merchants=4, lumber=0.0, clay=0.0, iron=0.0, crop=9000.0, stock=100_000),
        _snap(3, -150, -6, merchants=4, lumber=0.0, clay=0.0, iron=0.0, crop=9000.0, stock=100_000),
        _snap(4, -80, 0, merchants=20, lumber=0.0, clay=0.0, iron=0.0, crop=500.0, stock=100_000),
        _snap(5, 20, 0, merchants=20, lumber=0.0, clay=0.0, iron=0.0, crop=-2000.0, stock=100_000),
    ]
    out.append(
        Account(
            name="adv-relay-shape",
            seed=-14,
            plan_request=PlanRequest(
                snapshot=relay,
                config=[VillageConfig(village_id=v.village_id) for v in relay],
                allocations={
                    Resource.CROP: {
                        **_absolute(relay[0].village_id, 0.0),
                        **_absolute(relay[1].village_id, 0.0),
                        **_absolute(relay[2].village_id, 0.0),
                        **_absolute(relay[3].village_id, 500.0),
                        relay[4].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
                max_latency_hours=2.0,
            ),
            intent={"boundary": "three under-staffed feeders and a midway hub -> crop relay"},
        )
    )

    # 15. Toroidal wrap: two villages on opposite edges are neighbours.
    k = [
        _snap(1, -199, 0, lumber=3000.0),
        _snap(2, 199, 0, lumber=100.0),
    ]
    out.append(
        Account(
            name="adv-map-wrap",
            seed=-15,
            plan_request=PlanRequest(
                snapshot=k,
                config=[VillageConfig(village_id=v.village_id) for v in k],
                allocations={
                    Resource.LUMBER: {
                        **_absolute(k[0].village_id, 0.0),
                        k[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={"boundary": "x=-199 and x=+199 are 3 fields apart on a 401-wide map"},
        )
    )

    # 16. A NEGATIVE absolute target. Nothing in the request model or the
    #     allocation model forbids it, and it asks a village to retain less
    #     than nothing -- i.e. to ship more than it produces.
    m = [
        _snap(1, 0, 0, lumber=1000.0),
        _snap(2, 4, 0, lumber=1000.0),
    ]
    out.append(
        Account(
            name="adv-negative-absolute-target",
            seed=-16,
            plan_request=PlanRequest(
                snapshot=m,
                config=[VillageConfig(village_id=v.village_id) for v in m],
                allocations={
                    Resource.LUMBER: {
                        **_absolute(m[0].village_id, -4000.0),
                        m[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={"boundary": "absolute target of -4,000/h: retain less than nothing"},
        )
    )

    # 17. A receiver whose store is already full. The allocation says ship to
    #     it; the store cannot hold any of it.
    n = [
        _snap(1, 0, 0, lumber=8000.0, warehouse=400_000, stock=0),
        _snap(2, 6, 0, lumber=200.0, warehouse=100_000, stock=100_000),
    ]
    out.append(
        Account(
            name="adv-ship-into-a-full-store",
            seed=-17,
            plan_request=PlanRequest(
                snapshot=n,
                config=[VillageConfig(village_id=v.village_id) for v in n],
                allocations={
                    Resource.LUMBER: {
                        **_absolute(n[0].village_id, 0.0),
                        n[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={"boundary": "the remainder village's warehouse is already at its cap"},
        )
    )

    # 18. Profile section 5's DECLARED material relay tier. The one shape in
    #     this corpus where a material village legitimately both sends and
    #     receives, which is what the amended no-waterfall invariant exists to
    #     allow -- without an account like this every assertion about the
    #     exemption passes by vacuity.
    #
    #     V01 holds all the lumber and may ship to V02 only (`ship_only_to`), so
    #     V03 and V04 are out of its reach and the plan is infeasible with a
    #     shortfall each. V02 is declared their relay, and forwards.
    tier = [
        _snap(1, 0, 0, lumber=20_000.0, clay=0.0, iron=0.0, crop=1000.0, stock=100_000),
        _snap(2, 3, 0, lumber=0.0, clay=0.0, iron=0.0, crop=1000.0, stock=100_000),
        _snap(3, 6, 0, lumber=0.0, clay=0.0, iron=0.0, crop=1000.0, stock=100_000),
        _snap(4, 3, 4, lumber=0.0, clay=0.0, iron=0.0, crop=1000.0, stock=100_000),
    ]
    out.append(
        Account(
            name="adv-declared-material-relay",
            seed=-18,
            plan_request=PlanRequest(
                snapshot=tier,
                config=[
                    VillageConfig(village_id=tier[0].village_id, ship_only_to=[tier[1].village_id]),
                    VillageConfig(
                        village_id=tier[1].village_id,
                        relay_for=[tier[2].village_id, tier[3].village_id],
                    ),
                    VillageConfig(village_id=tier[2].village_id),
                    VillageConfig(village_id=tier[3].village_id),
                ],
                allocations={
                    Resource.LUMBER: {
                        **_absolute(tier[0].village_id, 0.0),
                        **_absolute(tier[2].village_id, 5000.0),
                        **_absolute(tier[3].village_id, 5000.0),
                        tier[1].village_id: AllocationInput(mode=AllocationMode.REMAINDER),
                    }
                },
            ),
            intent={
                "boundary": "a declared one-hop material relay: V02 forwards V01's lumber "
                "to V03 and V04, which V01's whitelist puts out of its own reach"
            },
        )
    )

    return out


def case_account(index: int) -> Account:
    """One of the review cases: same shape every time, different numbers.

    Deliberately uniform, because the review must not be able to tell the cases
    apart by their shape. Mid-sized, tight Trade Offices so merchants bind,
    every capacity readable so overflow is visible, a third of the villages
    crop-negative, and three profiles tiling the day.
    """
    rng = random.Random(90_000 + index)
    count = rng.randint(12, 20)
    snapshot = []
    for n in range(1, count + 1):
        merchants = rng.choice([13, 19, 20])
        crop = (
            -float(rng.randrange(1000, 9000, 100))
            if rng.random() < 0.33
            else float(rng.randrange(500, 9000, 100))
        )
        snapshot.append(
            VillageSnapshot(
                village_id=BASE_ID + n,
                name=f"V{n:02d}",
                x=rng.randint(-120, 120),
                y=rng.randint(-120, 120),
                merchants_total=merchants,
                # Never above the total: more free than exist is not a state the
                # game can be in, and it would make the busy-merchant check moot.
                merchants_free=min(merchants, rng.randint(4, 20)),
                lumber_per_hour=float(rng.randrange(1000, 9000, 100)),
                clay_per_hour=float(rng.randrange(1000, 9000, 100)),
                iron_per_hour=float(rng.randrange(1000, 9000, 100)),
                crop_per_hour=crop,
                crop_stock=rng.randrange(10_000, 380_000),
                crop_draining=crop < 0,
                lumber_stock=rng.randrange(10_000, 380_000),
                clay_stock=rng.randrange(10_000, 380_000),
                iron_stock=rng.randrange(10_000, 380_000),
                warehouse_capacity=400_000,
                granary_capacity=400_000,
            )
        )
    config = [
        VillageConfig(village_id=v.village_id, trade_office_level=rng.randint(0, 4))
        for v in snapshot
    ]
    plan_request = PlanRequest(
        snapshot=snapshot,
        config=config,
        allocations=_ordinary_allocations(rng, snapshot, over_allocate=False),
        max_latency_hours=2.0,
    )
    day_request = DayCheckRequest(
        snapshot=snapshot,
        config=config,
        max_latency_hours=2.0,
        segments=_segments("night7_start2_end1", snapshot, rng),
    )
    return Account(
        name=f"case-{index:02d}",
        seed=90_000 + index,
        plan_request=plan_request,
        day_request=day_request,
        intent={"villages": count, "shape": "review case", "profiles": 4},
    )


# ---------------------------------------------------------------------------
# village-id permutation
# ---------------------------------------------------------------------------


def id_permutation(request: PlanRequest, seed: int) -> dict[int, int]:
    """A shuffled relabelling of every village id in *request*."""
    ids = sorted(v.village_id for v in request.snapshot)
    shuffled = list(ids)
    random.Random(seed).shuffle(shuffled)
    return dict(zip(ids, shuffled, strict=True))


def permute_ids(request, mapping: dict[int, int]):
    """The same account with its village ids relabelled.

    Nothing about the account changes -- each village keeps its own
    coordinates, production, stores, name and allocation. Only the integer
    handle moves. A planner whose answer depends on dict iteration order or on
    an id-based tie-break will return a different plan for this.
    """
    snapshot = [
        v.model_copy(update={"village_id": mapping[v.village_id]}) for v in request.snapshot
    ]
    config = [c.model_copy(update={"village_id": mapping[c.village_id]}) for c in request.config]
    allocations = {
        resource: {mapping[vid]: item for vid, item in per.items()}
        for resource, per in request.allocations.items()
    }
    update: dict = {"snapshot": snapshot, "config": config, "allocations": allocations}
    segments = getattr(request, "segments", None)
    if segments is not None:
        update["segments"] = [
            segment.model_copy(
                update={
                    "allocations": {
                        resource: {mapping[vid]: item for vid, item in per.items()}
                        for resource, per in segment.allocations.items()
                    }
                }
            )
            for segment in segments
        ]
        update["crop_ceilings"] = {
            mapping[vid]: value for vid, value in request.crop_ceilings.items()
        }
    return request.model_copy(update=update)


def plan_signature(response, mapping: dict[int, int] | None = None) -> dict:
    """A plan reduced to what a relabelling must leave unchanged.

    Route order and the ids themselves are allowed to move; the route SET, the
    cargo on each route, its cycle, its merchant count, and each village's
    merchant bill are not.
    """
    back = {new: old for old, new in (mapping or {}).items()}

    def label(vid: int) -> int:
        return back.get(vid, vid)

    return {
        "rows": sorted(
            (
                label(row.origin),
                label(row.destination),
                tuple(sorted((getattr(k, "value", k), v) for k, v in row.cargo.items())),
                row.cycle_hours,
                row.merchants,
            )
            for row in response.rows
        ),
        "committed": sorted(
            (label(b.village_id), b.committed, b.over_budget) for b in response.budgets
        ),
        "total_merchants": response.total_merchants,
        "feasible": response.feasible,
        "shortfalls": sorted(
            (label(s.village_id), s.resource.value, round(s.per_hour, 6))
            for s in response.shortfalls
        ),
    }
