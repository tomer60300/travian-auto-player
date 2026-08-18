"""Resource distribution planner endpoints.

Split deliberately into a *fetch* and a *plan* call:

* ``GET /api/distribution/snapshot`` spends game requests and returns raw state.
* ``POST /api/distribution/plan`` spends none -- the caller hands back the
  snapshot it already holds plus its targets, and the planner is pure.

That split is the whole point. Requests to Travian are the scarce resource, so
re-planning while the operator tunes allocation targets must cost nothing, and
every fetch is a deliberate, priced action rather than a side effect of typing.
"""

import asyncio
import logging
import random
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from travian_api.exceptions import TravianError
from travian_api.parsers.html_parser import (
    parse_village_stats_production,
    parse_village_stats_resources,
)
from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationError,
    AllocationMode,
    Resource,
    resolve_resource,
    village_label,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import (
    DAILY_BEAT_CYCLES,
    EUROPE2_TEUTON,
    MerchantModel,
)
from travian_api.services.distribution.optimizer import (
    MAX_IMPROVE_PASSES,
    MAX_RELAY_HOPS,
    MIN_SEND_FILL,
    VillageState,
)
from travian_api.services.distribution.planner import (
    DistributionPlan,
    PlannerConfig,
    SheetRow,
    craft_plan,
)
from travian_api.services.distribution.schedule import MINUTES_PER_DAY
from travian_api.services.distribution.storage import (
    ProfileSegment,
    simulate_day,
    simulate_profile_cycle,
    storage_warnings,
    store_status,
)
from travian_api.services.trade_route_service import PlannedRoute
from travian_api.web.auth import get_current_user
from travian_api.web.models.db import User
from travian_api.web.sessions import TravianSession, get_live_travian_session, session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/distribution", tags=["distribution"])

# Europe 2 is x1 with coordinates running -200..+200. Exposed in the response so
# the UI can show what the distances were computed against.
DEFAULT_MAP_SPAN = 401
DEFAULT_SPEED_FIELDS_PER_HOUR = 12.0

# Merchant travel speed is tribe-specific (fields/hour). Wrong speed inflates
# travel time -> sets_in_flight -> merchant counts, and can flip over_budget.
# Keyed by Travian tribe id; unknown tribes fall back to the Teuton default.
_TRIBE_MERCHANT_SPEED: dict[int, float] = {
    1: 16.0,  # Roman
    2: 12.0,  # Teuton
    3: 24.0,  # Gaul
    6: 16.0,  # Egyptian
    7: 20.0,  # Hun
    8: 16.0,  # Spartan
}


class VillageSnapshot(BaseModel):
    """Per-village state, all of it read from the game."""

    village_id: int
    name: str
    x: int
    y: int
    merchants_total: int = 0
    merchants_free: int = 0
    lumber_per_hour: float = 0
    clay_per_hour: float = 0
    iron_per_hour: float = 0
    crop_per_hour: float | None = Field(
        default=None,
        description=(
            "NET crop per hour, negative while draining. None when it could not "
            "be derived -- never silently zero, since zero reads as healthy."
        ),
    )
    crop_stock: int = 0
    crop_draining: bool = False
    lumber_stock: int = 0
    clay_stock: int = 0
    iron_stock: int = 0
    warehouse_capacity: int | None = Field(
        default=None,
        description=(
            "Per-resource warehouse cap for lumber/clay/iron. None when the "
            "capacity page was not needed by the crop read and so was not "
            "fetched -- storage checks skip the village rather than guess."
        ),
    )
    granary_capacity: int | None = Field(
        default=None, description="Granary cap. None when it could not be read."
    )


class SnapshotResponse(BaseModel):
    villages: list[VillageSnapshot]
    map_span: int = DEFAULT_MAP_SPAN
    speed_fields_per_hour: float = DEFAULT_SPEED_FIELDS_PER_HOUR
    requests_used: int
    warnings: list[str] = []


class AllocationInput(BaseModel):
    mode: AllocationMode
    value: float = 0.0


class VillageConfig(BaseModel):
    """Operator-owned state the game will not tell us."""

    village_id: int
    trade_office_level: int = Field(
        default=0,
        ge=0,
        le=20,
        description=(
            "Unknown levels must default to 0: understating capacity "
            "over-provisions merchants, which is safe, while overstating it "
            "breaches the merchant budget invisibly."
        ),
    )


class ForeignTarget(BaseModel):
    """Crop owed to a village outside the account (profile section 7.3).

    Not a village and deliberately not modelled as one: it has no production, no
    merchants and no stores, so it can receive crop and never send any. Giving it
    a village row would invite treating it as one -- the profile is explicit that
    foreign targets belong in their own section for exactly that reason.

    Supplied by hand because nothing in the game tells us about it: a name to
    recognise it by, where it is, and how much crop per hour was promised.
    """

    name: str = Field(min_length=1)
    x: int
    y: int
    crop_per_hour: float = Field(gt=0)
    safety_margin_pct: float = Field(
        default=0.0,
        ge=0,
        le=100,
        description=(
            "Ship this much above the promise. Travel time and rounding mean a "
            "route sized exactly to the obligation arrives a little short, and "
            "being short on a tribute is worse than sending a few crop spare."
        ),
    )


class PlanRequest(BaseModel):
    snapshot: list[VillageSnapshot]
    foreign_targets: list[ForeignTarget] = []
    config: list[VillageConfig] = []
    # resource -> village_id -> allocation
    allocations: dict[Resource, dict[int, AllocationInput]] = {}
    merchant_base_capacity: int = Field(default=EUROPE2_TEUTON.base_capacity, gt=0)
    trade_office_bonus_per_level: float = Field(
        default=EUROPE2_TEUTON.bonus_per_trade_office_level, ge=0
    )
    merchant_reserve: int = Field(default=2, ge=0)
    max_latency_hours: float | None = 2.0
    min_arrival_gap_minutes: int = Field(default=3, ge=0)
    map_span: int = Field(default=DEFAULT_MAP_SPAN, gt=0)
    speed_fields_per_hour: float = Field(default=DEFAULT_SPEED_FIELDS_PER_HOUR, gt=0)
    min_send_fill: float = Field(
        default=MIN_SEND_FILL,
        ge=0,
        le=1,
        description=(
            "How full a merchant must stay when idle merchants are spent on speed. "
            "Lower it for faster routes on emptier merchants, raise it to keep them "
            "full but slower. This is the latency/fill trade-off dial."
        ),
    )
    max_improve_passes: int = Field(
        default=MAX_IMPROVE_PASSES,
        ge=1,
        description=(
            "Ceiling on route-search passes. Raise it for very large accounts; a "
            "search that stops early says so in the warnings, because a truncated "
            "one overstates how many villages are over budget."
        ),
    )
    max_relay_hops: int = Field(
        default=MAX_RELAY_HOPS,
        ge=0,
        description="Levels of crop relay through a sub-hub; 0 ships everything direct.",
    )
    reserved_window: tuple[int, int] | None = Field(
        default=None,
        description=(
            "Minutes past midnight (start, end) to keep clear of arrivals for the "
            "manual NPC burst. Arrivals avoid it where an alternative exists, and "
            "the plan warns when geometry forces one into it."
        ),
    )

    @field_validator("reserved_window")
    @classmethod
    def _window_within_the_day(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        # A window may wrap past midnight (start > end), so only the bounds are
        # checked -- ordering carries meaning rather than being an error.
        if value is not None and not all(0 <= minute < MINUTES_PER_DAY for minute in value):
            raise ValueError(f"reserved_window minutes must be 0-{MINUTES_PER_DAY - 1}")
        return value


class SheetRowResponse(BaseModel):
    origin: int
    destination: int
    # Display names, resolved server-side. The frontend used to look ids up in
    # its own snapshot, which cannot know foreign tributes -- their negative
    # ids leaked into the sheet as "-1" instead of the name the operator typed.
    origin_name: str = ""
    destination_name: str = ""
    cargo: dict[Resource, int]
    total_cargo: int
    cycle_hours: int
    dispatch: str
    arrival: str
    merchants: int
    first_delivery_hours: float = Field(
        default=0.0,
        description=(
            "Hours from creating this route to its first delivery landing. Every "
            "other figure here is steady-state; on the day the route is created "
            "the cargo still has to accumulate for one cycle and then travel."
        ),
    )


class BudgetLegResponse(BaseModel):
    """One route's contribution to a village's merchant bill."""

    destination: str
    per_hour: float
    distance_fields: float
    one_way_hours: float
    cycle_hours: int
    merchants_per_send: int
    sets_in_flight: int
    merchants: int


class BudgetResponse(BaseModel):
    village_id: int
    committed: int
    spare: int
    free: int
    over_budget: bool
    trade_office_levels_needed: int | None = None
    legs: list[BudgetLegResponse] = []
    """Where the merchants actually went, biggest bill first."""
    explanation: str | None = Field(
        default=None,
        description=(
            "Why this village is over budget, in the operator's terms. 'over by "
            "2' says what happened but not what to do about it: the same excess "
            "means something different when the trip is the cost than when the "
            "Trade Office is."
        ),
    )


class ShortfallResponse(BaseModel):
    village_id: int
    village_name: str = ""
    resource: Resource
    per_hour: float
    reason: str


class UnallocatedResponse(BaseModel):
    resource: Resource
    total_production: float
    unallocated: float
    # Optional in meaning (a resource may have no remainder village) and given
    # an explicit default so the model never depends on the caller supplying it.
    remainder_village_id: int | None = None


class PlanResponse(BaseModel):
    rows: list[SheetRowResponse]
    budgets: list[BudgetResponse]
    shortfalls: list[ShortfallResponse]
    unallocated: list[UnallocatedResponse]
    total_merchants: int
    feasible: bool
    warnings: list[str]


class ExecuteRequest(PlanRequest):
    """Same inputs as /plan (the server recomputes the exact plan rather than
    trust client-sent rows) plus execution controls."""

    dry_run: bool = Field(
        default=True,
        description="Preview only, zero game requests. Must be explicitly set "
        "False to touch the game.",
    )
    disable_existing: bool = Field(
        default=True,
        description="Disable a village's existing routes before creating new ones.",
    )
    max_routes_per_run: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Routes to CREATE in one run. A human sets up a few at a "
        "time over days, not in one sweep; the rest come back as `remaining` "
        "for a later run.",
    )


class RouteActionResponse(BaseModel):
    origin: int
    origin_name: str
    destination: int
    destination_name: str
    dest_x: int
    dest_y: int
    cargo: dict[Resource, int]
    cycle_hours: int
    merchants: int
    status: str  # would_create | deferred | created | skipped | failed
    detail: str = ""


class ExecuteResponse(BaseModel):
    dry_run: bool
    live_enabled: bool
    actions: list[RouteActionResponse]
    disables: list[str]
    created: int
    remaining: int
    warnings: list[str]


@router.get("/snapshot", response_model=SnapshotResponse)
async def get_snapshot(session: TravianSession = Depends(get_live_travian_session)):
    """Read current account state. Costs 3-4 game requests.

    Uses the live session only: this endpoint prices every game request, and
    an auto-reconnect would spend unreported login traffic first.

    Production for lumber/clay/iron comes from the account-wide statistics table,
    where gross equals net -- only crop is consumed. Crop is read separately
    because that table reports GROSS crop, and using it as net inverts the sign
    on any village whose troops outeat its fields.
    """
    if session.auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No auth state — reconnect first.",
        )

    warnings: list[str] = []
    try:
        production = parse_village_stats_production(
            await session.http_client.get_html("/village/statistics/resources/production")
        )
        # Fetch the stocks table once and use it for both merchant counts and
        # the crop derivation, rather than letting the service fetch it again.
        stocks = parse_village_stats_resources(
            await session.http_client.get_html("/village/statistics/resources")
        )
        crop, capacities, crop_requests = await session.building_service.get_all_villages_net_crop(
            stocks=stocks
        )
    except TravianError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Distribution snapshot failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not read account state: {exc}",
        ) from exc

    if not production:
        warnings.append(
            "no production rates could be read from the statistics page (is Travian "
            "Plus active?); lumber/clay/iron default to 0/h, so a plan built from "
            "this snapshot would move nothing"
        )
    else:
        unread = [v.id for v in session.auth_state.villages if v.id not in production]
        if unread:
            warnings.append(
                f"no production rates read for village(s) {unread}; their "
                f"lumber/clay/iron default to 0/h"
            )

    villages: list[VillageSnapshot] = []
    for village in session.auth_state.villages:
        vid = village.id
        rates = production.get(vid, {})
        balance = crop.get(vid)
        merchants = stocks.get(vid, {})
        if balance is None:
            label = village.name or f"village {vid}"
            warnings.append(f"{label} has no crop balance; it will not be routed for crop")
        villages.append(
            VillageSnapshot(
                village_id=vid,
                name=village.name or str(vid),
                x=village.x,
                y=village.y,
                merchants_total=merchants.get("merchants_total", 0),
                merchants_free=merchants.get("merchants_free", 0),
                lumber_per_hour=rates.get("lumber", 0),
                clay_per_hour=rates.get("clay", 0),
                iron_per_hour=rates.get("iron", 0),
                crop_per_hour=balance.net_per_hour if balance else None,
                crop_stock=balance.stock if balance else 0,
                crop_draining=balance.draining if balance else False,
                lumber_stock=merchants.get("lumber", 0),
                clay_stock=merchants.get("clay", 0),
                iron_stock=merchants.get("iron", 0),
                warehouse_capacity=capacities.get(vid, {}).get("warehouse"),
                granary_capacity=capacities.get(vid, {}).get("granary"),
            )
        )

    missing_merchants = [v for v in villages if v.merchants_total == 0]
    if missing_merchants:
        warnings.append(
            "no merchant count read for "
            + ", ".join(v.name or f"village {v.village_id}" for v in missing_merchants)
            + "; they cannot send until it is known"
        )

    return SnapshotResponse(
        villages=villages,
        speed_fields_per_hour=_TRIBE_MERCHANT_SPEED.get(
            session.tribe_id or 0, DEFAULT_SPEED_FIELDS_PER_HOUR
        ),
        # Production + stocks here, plus whatever the crop read actually spent
        # (the capacity page is only fetched when some granary is filling).
        requests_used=2 + crop_requests,
        warnings=warnings,
    )


_STOCK_FIELD = {
    Resource.LUMBER: "lumber_stock",
    Resource.CLAY: "clay_stock",
    Resource.IRON: "iron_stock",
    Resource.CROP: "crop_stock",
}
_RATE_FIELD = {
    Resource.LUMBER: "lumber_per_hour",
    Resource.CLAY: "clay_per_hour",
    Resource.IRON: "iron_per_hour",
    Resource.CROP: "crop_per_hour",
}


def _storage_warnings(body: PlanRequest, plan) -> list[str]:
    """Overflow and starvation checks over the finished plan. Zero requests.

    Every input is already in the snapshot the caller handed back, so this costs
    nothing to run. Villages whose capacity was never read are simply skipped
    rather than assumed -- the capacity page is only fetched when the crop
    derivation needs it, and inventing a cap would produce confident nonsense.
    """
    statuses = []
    stocks: dict[int, dict[Resource, int]] = {}
    capacities: dict[int, dict[Resource, int]] = {}
    # OWN production only. The two checks need different inputs and mixing them
    # up double-counts every route: store_status works on the post-plan net rate
    # (a continuous average, routes folded in), while simulate_day applies the
    # routes itself as discrete dispatches and arrivals. Feeding the net rate to
    # the simulation makes a receiver bank its deliveries twice -- 48,000 a day
    # where 24,000 arrives -- and invent overflows that do not exist.
    own_rates: dict[int, dict[Resource, float]] = {}

    # Net rate per village per resource AFTER the plan: own production plus what
    # arrives minus what leaves. That is what the store actually sees.
    shipped: dict[int, dict[Resource, float]] = {}
    for route in plan.routing.routes:
        for resource, amount in route.cargo_per_hour.items():
            shipped.setdefault(route.destination, {})[resource] = (
                shipped.setdefault(route.destination, {}).get(resource, 0.0) + amount
            )
            shipped.setdefault(route.origin, {})[resource] = (
                shipped.setdefault(route.origin, {}).get(resource, 0.0) - amount
            )

    # Only real villages have stores. A foreign tribute is a sink with no
    # granary to overflow or starve, and it never appears in body.snapshot.
    for village in body.snapshot:
        vid = village.village_id
        for resource in Resource:
            own = getattr(village, _RATE_FIELD[resource])
            if own is None:
                continue  # unreadable rate; the plan already warned about it
            stock = getattr(village, _STOCK_FIELD[resource])
            cap = (
                village.granary_capacity
                if resource is Resource.CROP
                else village.warehouse_capacity
            )
            net = float(own) + shipped.get(vid, {}).get(resource, 0.0)
            stocks.setdefault(vid, {})[resource] = stock
            own_rates.setdefault(vid, {})[resource] = float(own)
            if cap is not None:
                capacities.setdefault(vid, {})[resource] = cap
            statuses.append(store_status(vid, resource, stock, cap, net))

    overflows = simulate_day(plan.beat, stocks, capacities, own_rates)
    names = {v.village_id: v.name for v in body.snapshot if v.name}
    return list(storage_warnings(statuses, overflows, names=names))


def _budget_legs(
    village_id: int,
    plan,
    geometry: MapGeometry,
    names: dict[int, str],
    coords: dict[int, tuple[int, int]],
) -> list[BudgetLegResponse]:
    """Every route this village staffs, dearest first."""
    legs = []
    for route in plan.routing.routes:
        if route.origin != village_id:
            continue
        legs.append(
            BudgetLegResponse(
                destination=village_label(route.destination, names),
                per_hour=route.hourly_total,
                distance_fields=geometry.distance(coords[village_id], coords[route.destination]),
                one_way_hours=route.one_way_minutes / 60.0,
                cycle_hours=route.cycle_hours,
                merchants_per_send=route.merchants_per_send,
                sets_in_flight=route.sets_in_flight,
                merchants=route.merchants_committed,
            )
        )
    return sorted(legs, key=lambda leg: (-leg.merchants, leg.destination))


def _explain_over_budget(
    label: str,
    committed: int,
    spare: int,
    legs: list[BudgetLegResponse],
    trade_office_level: int,
    capacity: int,
    upgrade: int | None,
) -> str:
    """Say why the merchants ran out, in terms that suggest what to do.

    'over by 2' is true and useless. A village can overrun its merchants for two
    quite different reasons and the fix is different for each: when the trip is
    long, merchants are tied up in transit and only a shorter haul or a smaller
    load helps; when the Trade Office is low, each merchant carries little and
    the upgrade is the answer. So this names whichever dominates rather than
    stating the arithmetic back.
    """
    if not legs:
        return f"{label} is over its merchant budget, but no route explains it — this is a bug."

    worst = legs[0]
    parts = [f"{label} needs {committed} merchants but has {spare}."]

    # The two factors multiply, so explain both rather than declaring a winner:
    # merchants = (cargo / capacity, rounded up) x (round trip / cycle, rounded
    # up). Naming only the larger one produced advice that argued with itself --
    # "the Trade Office won't help much... Trade Office +5 would fix it".
    parts.append(
        f"Its biggest haul is {worst.per_hour:,.0f}/h to {worst.destination}, "
        f"{worst.distance_fields:.0f} fields away, and costs {worst.merchants} merchants: "
        f"{worst.merchants_per_send} per send "
        f"(each carries {capacity:,} at Trade Office {trade_office_level}), "
        f"and with a {worst.one_way_hours * 2:.1f}h round trip against a "
        f"{worst.cycle_hours}h cycle, {worst.sets_in_flight} send(s) are in the air at once."
    )
    if len(legs) > 1:
        others = sum(leg.merchants for leg in legs[1:])
        rest = "route takes" if len(legs) == 2 else "routes take"
        parts.append(f"Its other {len(legs) - 1} {rest} {others} more.")

    if upgrade is not None:
        parts.append(
            f"Trade Office +{upgrade} raises what each merchant carries and would make this fit."
        )
    else:
        parts.append(
            "No Trade Office level fixes this: the merchants are tied up in travel time, "
            "not short of carrying capacity. Ship less from here, send it somewhere "
            "nearer, or consume the surplus locally."
        )
    return " ".join(parts)


class DaySegmentInput(BaseModel):
    """One allocation profile plus the hours of the day it actually runs."""

    name: str = Field(min_length=1)
    window: tuple[int, int]
    """Minutes past midnight (start, end); may wrap past midnight."""
    allocations: dict[Resource, dict[int, AllocationInput]] = {}

    @field_validator("window")
    @classmethod
    def _window_in_day(cls, value: tuple[int, int]) -> tuple[int, int]:
        if not all(0 <= minute < MINUTES_PER_DAY for minute in value):
            raise ValueError(f"window minutes must be 0-{MINUTES_PER_DAY - 1}")
        if value[0] == value[1]:
            raise ValueError("window is zero-width; give the profile some hours or omit it")
        return value


class DayCheckRequest(BaseModel):
    """The whole day at once: every profile, each in its own hours.

    Costs zero game requests. Profiles are planned in isolation, but the account
    lives through all of them every day -- what the day profile ships decides
    the stock the night profile starts from, so questions like "does the capital
    cross 90k during the night?" can only be answered by simulating the day as
    the profiles will actually run it.
    """

    snapshot: list[VillageSnapshot]
    segments: list[DaySegmentInput] = Field(min_length=1)
    foreign_targets: list[ForeignTarget] = Field(
        default=[],
        description=(
            "The same tributes POST /plan ships. Each per-profile plan carries "
            "them as absolute targets, so the composite day must drain them too "
            "or it reads optimistic by 24x the tribute."
        ),
    )
    crop_ceilings: dict[int, float] = Field(
        default={},
        description=(
            "Operator alert level for a village's crop stock, below capacity -- "
            "typically an NPC trigger. Crossing it is reported with the hour and "
            "the profile that was running."
        ),
    )


class VillageDayResponse(BaseModel):
    village_id: int
    village_name: str
    resource: Resource
    daily_net: float
    low: float
    high: float
    settled: bool


class DayCheckResponse(BaseModel):
    villages: list[VillageDayResponse]
    warnings: list[str]


def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


@router.post("/day-check", response_model=DayCheckResponse)
async def post_day_check(
    body: DayCheckRequest,
    _user: User = Depends(get_current_user),
):
    """Simulate the full day across every profile. Costs zero game requests.

    Deliberate approximation, stated rather than hidden: each segment
    contributes its plan's NET RATES, not discrete batches. Route phases do not
    survive a profile switch (the operator recreates the routes), so batch
    timing across the boundary is unknowable; the discrete-batch overflow check
    still runs per profile inside POST /plan.
    """
    if not body.snapshot:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Snapshot is empty — fetch account state first.",
        )

    # Overlapping windows would double-ship: two profiles cannot run at once.
    minutes_covered: set[int] = set()
    for segment in body.segments:
        start, end = segment.window
        span = range(start, end) if start < end else [*range(start, MINUTES_PER_DAY), *range(end)]
        span = set(span)
        clash = minutes_covered & span
        if clash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"profile windows overlap around {_clock(min(clash))}: two profiles "
                    f"cannot run at the same time"
                ),
            )
        minutes_covered |= span

    names = {v.village_id: v.name for v in body.snapshot}
    rate_field = {
        Resource.LUMBER: "lumber_per_hour",
        Resource.CLAY: "clay_per_hour",
        Resource.IRON: "iron_per_hour",
        Resource.CROP: "crop_per_hour",
    }
    stock_field = {
        Resource.LUMBER: "lumber_stock",
        Resource.CLAY: "clay_stock",
        Resource.IRON: "iron_stock",
        Resource.CROP: "crop_stock",
    }
    productions: dict[Resource, dict[int, float]] = {}
    own_rates: dict[int, dict[Resource, float]] = {}
    stocks: dict[int, dict[Resource, int]] = {}
    capacities: dict[int, dict[Resource, int]] = {}
    for village in body.snapshot:
        for resource in Resource:
            own = getattr(village, rate_field[resource])
            if own is None:
                continue  # unreadable rate: the village sits this check out
            productions.setdefault(resource, {})[village.village_id] = float(own)
            own_rates.setdefault(village.village_id, {})[resource] = float(own)
            stocks.setdefault(village.village_id, {})[resource] = getattr(
                village, stock_field[resource]
            )
            cap = (
                village.granary_capacity
                if resource is Resource.CROP
                else village.warehouse_capacity
            )
            if cap is not None:
                capacities.setdefault(village.village_id, {})[resource] = cap

    warnings: list[str] = []
    # A village with an unreadable rate sits the check out entirely for that
    # resource -- its allocations are dropped from every segment and its rows
    # (crop alert included) never appear. Silence here would let the response
    # look complete while missing a village, so say so once per resource.
    for resource in Resource:
        missing = sorted(
            v.village_id for v in body.snapshot if getattr(v, rate_field[resource]) is None
        )
        if missing:
            note = (
                f"{resource.value}: no rate could be read for "
                + ", ".join(village_label(vid, names) for vid in missing)
                + f", so their {resource.value} sits out the day check"
            )
            if resource is Resource.CROP and any(vid in body.crop_ceilings for vid in missing):
                note += ", crop alert levels included"
            warnings.append(note)

    # Foreign tributes drain crop in every profile, exactly as POST /plan ships
    # them (absolute targets on negative sink ids, margin included). The sinks
    # get no trajectory of their own -- the drain lives in whoever funds them.
    foreign_ids: dict[int, ForeignTarget] = {}
    if body.foreign_targets and Resource.CROP not in productions:
        warnings.append(
            "crop: no rate could be read for any village, so the foreign tribute "
            "cannot be simulated -- the day picture is missing that drain"
        )
    elif body.foreign_targets:
        crop_rates = productions[Resource.CROP]
        for index, target in enumerate(body.foreign_targets):
            target_id = -(index + 1)
            foreign_ids[target_id] = target
            names[target_id] = target.name
            crop_rates[target_id] = 0.0  # a tribute grows nothing, it only consumes

    segments: list[ProfileSegment] = []
    for segment in body.segments:
        ship: dict[int, dict[Resource, float]] = {}
        for resource in Resource:
            per_village = segment.allocations.get(resource, {})
            known = productions.get(resource, {})
            usable = {
                vid: Allocation(mode=item.mode, value=item.value)
                for vid, item in per_village.items()
                if item.mode is not AllocationMode.KEEP and vid in known
            }
            if resource is Resource.CROP and foreign_ids:
                for target_id, target in foreign_ids.items():
                    owed = target.crop_per_hour * (1.0 + target.safety_margin_pct / 100.0)
                    usable[target_id] = Allocation(mode=AllocationMode.ABSOLUTE, value=owed)
                # The sink's demand is funded through the remainder village; a
                # profile without one leaves the tribute unpaid in those hours,
                # which the composite would otherwise model silently.
                if not any(a.mode is AllocationMode.REMAINDER for a in usable.values()):
                    warnings.append(
                        f"{segment.name}: no crop remainder village, so nothing funds "
                        f"the tribute during it -- modeled as unpaid in those hours"
                    )
            if not usable:
                continue
            try:
                resolved = resolve_resource(resource, known, usable, names)
            except AllocationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{segment.name}: {exc}",
                ) from exc
            # A profile that does not conserve mass (an absolute receiver with
            # no remainder to source it) makes resolve_resource emit a warning
            # and hand back a receiver inflow no route actually supplies.
            # Discarding it would let the day picture credit crop from nothing
            # and still report a green all-clear where /plan would show a
            # shortfall. Surface the warning so the safety check cannot lie.
            for warning in resolved.warnings:
                warnings.append(f"{segment.name}: {warning}")
            for village in resolved.villages:
                if village.village_id < 0:
                    continue  # the sink itself is not simulated; see above
                if abs(village.ship_per_hour) > 0:
                    ship.setdefault(village.village_id, {})[resource] = village.ship_per_hour
        segments.append(
            ProfileSegment(
                name=segment.name,
                start_minute=segment.window[0],
                end_minute=segment.window[1],
                ship_rates=ship,
            )
        )

    trajectories, breaches = simulate_profile_cycle(
        segments, own_rates, stocks, capacities, body.crop_ceilings
    )

    for breach in sorted(breaches, key=lambda b: (b.day, b.minute)):
        label = village_label(breach.village_id, names)
        when = f"at {_clock(breach.minute)} during {breach.segment}" + (
            f" on day {breach.day + 1}" if breach.day else " today"
        )
        if breach.kind == "above":
            ceiling = body.crop_ceilings.get(breach.village_id, 0)
            warnings.append(
                f"{label}: crop is already above its {ceiling:,.0f} alert level as the day starts"
            )
        elif breach.kind == "ceiling":
            ceiling = body.crop_ceilings.get(breach.village_id, 0)
            warnings.append(f"{label}: crop crosses its {ceiling:,.0f} alert level {when}")
        elif breach.kind == "capacity":
            warnings.append(
                f"{label}: {breach.resource.value} hits its store cap {when}; "
                f"everything past it is lost"
            )
        else:
            warnings.append(f"{label}: {breach.resource.value} runs dry {when}")

    return DayCheckResponse(
        villages=[
            VillageDayResponse(
                village_id=t.village_id,
                village_name=village_label(t.village_id, names),
                resource=t.resource,
                daily_net=t.daily_net,
                low=t.low,
                high=t.high,
                settled=t.settled,
            )
            for t in trajectories
        ],
        warnings=warnings,
    )


@dataclass
class _PlannedAccount:
    """Everything /plan and /execute both derive from one PlanRequest.

    Both build the account model, run the pure optimizer, and resolve
    coordinates identically. /execute recomputes server-side through this — it
    does NOT trust client-sent rows — so it acts on exactly the plan /plan
    would display for the same inputs.
    """

    plan: DistributionPlan
    villages: dict[int, VillageState]
    coords: dict[int, tuple[int, int]]
    names: dict[int, str]
    trade_office: dict[int, int]
    foreign_ids: dict[int, ForeignTarget]
    config: PlannerConfig
    warnings: list[str]


async def _plan_account(body: PlanRequest) -> _PlannedAccount:
    """Build the account model, run the optimizer, resolve coords + warnings.

    Pure of game I/O, so it is shared by the zero-request /plan endpoint and by
    the dry-run computation inside /execute.
    """
    # Warnings are read by a person: name villages the way they do, never by id.
    names = {v.village_id: v.name for v in body.snapshot if v.name}
    trade_office = {c.village_id: c.trade_office_level for c in body.config}
    villages = {
        v.village_id: VillageState(
            village_id=v.village_id,
            x=v.x,
            y=v.y,
            merchant_count=v.merchants_total,
            trade_office_level=trade_office.get(v.village_id, 0),
            name=v.name,
        )
        for v in body.snapshot
    }
    if not villages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Snapshot is empty — fetch account state first.",
        )

    # Foreign tributes join the plan as crop SINKS. Negative ids keep them
    # clearly apart from real villages (which are always positive) so a target
    # can never be confused for one, and merchant_count=0 makes it structurally
    # impossible for one to ship anything: it can receive and nothing else.
    foreign_ids: dict[int, ForeignTarget] = {}
    for index, target in enumerate(body.foreign_targets):
        target_id = -(index + 1)
        foreign_ids[target_id] = target
        names[target_id] = target.name
        villages[target_id] = VillageState(
            village_id=target_id,
            x=target.x,
            y=target.y,
            merchant_count=0,
            trade_office_level=0,
            name=target.name,
        )

    rate_field = {
        Resource.LUMBER: "lumber_per_hour",
        Resource.CLAY: "clay_per_hour",
        Resource.IRON: "iron_per_hour",
        Resource.CROP: "crop_per_hour",
    }
    productions: dict[Resource, dict[int, float]] = {}
    for resource, field_name in rate_field.items():
        rates = {
            v.village_id: float(getattr(v, field_name))
            for v in body.snapshot
            # A village whose net crop could not be derived is excluded rather
            # than defaulted to 0, which would read as a healthy village.
            if getattr(v, field_name) is not None
        }
        if resource is Resource.CROP and foreign_ids and rates:
            # Zero production: a tribute grows nothing, it only consumes. Adding
            # it to the crop map is what makes resolve_resource deduct the
            # obligation from the account pool via the remainder village.
            rates.update({vid: 0.0 for vid in foreign_ids})
        if rates:
            productions[resource] = rates

    config = PlannerConfig(
        geometry=MapGeometry(span=body.map_span, speed_fields_per_hour=body.speed_fields_per_hour),
        merchant_model=MerchantModel(
            base_capacity=body.merchant_base_capacity,
            bonus_per_trade_office_level=body.trade_office_bonus_per_level,
        ),
        merchant_reserve=body.merchant_reserve,
        cycles=DAILY_BEAT_CYCLES,
        max_latency_hours=body.max_latency_hours,
        min_arrival_gap_minutes=body.min_arrival_gap_minutes,
        reserved_window=body.reserved_window,
        min_send_fill=body.min_send_fill,
        max_improve_passes=body.max_improve_passes,
        max_relay_hops=body.max_relay_hops,
    )

    extra_warnings: list[str] = []
    try:
        # Explicit `keep` entries mean exactly what an absent entry means, so
        # they are dropped here rather than allowed to 400 the whole plan when
        # they reference a village that has since vanished from the snapshot.
        allocations = {
            resource: {
                vid: Allocation(mode=item.mode, value=item.value)
                for vid, item in per_village.items()
                if item.mode is not AllocationMode.KEEP
            }
            for resource, per_village in body.allocations.items()
        }
        if foreign_ids:
            # The tribute is a fixed retention target at the sink: it must end up
            # holding exactly what was promised (plus margin), and since it grows
            # nothing, all of that has to be shipped in.
            crop_allocations = allocations.setdefault(Resource.CROP, {})
            for target_id, target in foreign_ids.items():
                owed = target.crop_per_hour * (1.0 + target.safety_margin_pct / 100.0)
                crop_allocations[target_id] = Allocation(mode=AllocationMode.ABSOLUTE, value=owed)
        for resource in sorted(set(allocations) - set(productions), key=lambda r: r.value):
            if allocations.pop(resource):
                extra_warnings.append(
                    f"{resource.value}: no production rate is known for any village, "
                    f"so its allocations were ignored"
                )
        # A single village with an unreadable rate (crop_per_hour=None is the
        # normal no-crop-balance snapshot path) must not fail the whole plan:
        # drop just its allocations, say so, and plan the rest.
        for resource, per_village in allocations.items():
            known = productions[resource]
            unreadable = sorted(vid for vid in per_village if vid not in known)
            for vid in unreadable:
                del per_village[vid]
            if unreadable:
                extra_warnings.append(
                    f"{resource.value}: no rate could be read for "
                    + ", ".join(village_label(vid, names) for vid in unreadable)
                    + f", so their {resource.value} allocations were ignored"
                )
        # The beat search is pure CPU; off the event loop so it cannot stall
        # WebSocket frames or stealth-timed game requests while the user replans.
        plan = await asyncio.to_thread(craft_plan, villages, productions, allocations, config)
    except AllocationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    free_now = {v.village_id: v.merchants_free for v in body.snapshot}
    for vid in sorted(plan.merchants_committed):
        committed = plan.merchants_committed[vid]
        if committed > free_now.get(vid, 0):
            extra_warnings.append(
                f"{village_label(vid, names)}: the plan commits {committed} merchants but only "
                f"{free_now.get(vid, 0)} are free right now — existing routes or "
                f"shipments must release the rest before the sheet is executable"
            )

    extra_warnings.extend(_storage_warnings(body, plan))

    for target_id, target in foreign_ids.items():
        suppliers = sorted({row.origin for row in plan.rows if row.destination == target_id})
        if not suppliers:
            extra_warnings.append(
                f"{target.name} ({target.x}|{target.y}) is owed "
                f"{target.crop_per_hour:,.0f} crop/h but no village could supply it"
            )
            continue
        if len(suppliers) > 1:
            extra_warnings.append(
                f"{target.name} ({target.x}|{target.y}) is supplied by "
                + ", ".join(village_label(vid, names) for vid in suppliers)
                + " — several routes to keep track of; consider raising one "
                "supplier's share so a single route covers it"
            )
        # Section 7.3: a tribute must not lapse, and the first delivery only
        # lands after a full one-way trip. Saying so is the difference between
        # a planned gap and an apparent broken promise. With several suppliers
        # the first crop lands at the EARLIEST route's startup; the slowest
        # route only marks when the full rate is flowing.
        firsts = [row.first_delivery_hours for row in plan.rows if row.destination == target_id]
        first = min(firsts, default=0.0)
        full = max(firsts, default=0.0)
        note = (
            f"{target.name} ({target.x}|{target.y}): the first crop lands "
            f"{first:.1f}h after the routes are created"
        )
        if full > first + 0.05:
            note += f" and the full tribute only flows from {full:.1f}h"
        note += ", so it starts late unless covered by hand until then"
        extra_warnings.append(note)

    coords = {vid: village.coords for vid, village in villages.items()}
    return _PlannedAccount(
        plan=plan,
        villages=villages,
        coords=coords,
        names=names,
        trade_office=trade_office,
        foreign_ids=foreign_ids,
        config=config,
        warnings=extra_warnings,
    )


@router.post("/plan", response_model=PlanResponse)
async def post_plan(
    body: PlanRequest,
    _user: User = Depends(get_current_user),
):
    """Compute a plan. Costs **zero** game requests.

    The caller supplies the snapshot it already fetched, so tuning allocation
    targets is free and the planner stays pure. Deliberately auth-only: a
    Travian-session dependency would auto-reconnect (real login traffic) or 403
    for a computation that never touches the game.
    """
    account = await _plan_account(body)
    plan = account.plan
    names = account.names
    trade_office = account.trade_office
    config = account.config
    coords = account.coords
    villages = account.villages
    extra_warnings = account.warnings
    upgrades = {o.village_id: o.trade_office_levels_needed for o in plan.over_budget}
    over = {o.village_id for o in plan.over_budget}

    return PlanResponse(
        rows=[
            SheetRowResponse(
                origin=row.origin,
                origin_name=village_label(row.origin, names),
                destination_name=village_label(row.destination, names),
                destination=row.destination,
                cargo={r: amount for r, amount in row.cargo.items() if amount},
                total_cargo=row.total_cargo,
                cycle_hours=row.cycle_hours,
                dispatch=row.dispatch_clock(),
                arrival=row.arrival_clock(),
                merchants=row.merchants,
                first_delivery_hours=row.first_delivery_hours,
            )
            for row in plan.rows
        ],
        budgets=[
            BudgetResponse(
                village_id=vid,
                committed=plan.merchants_committed.get(vid, 0),
                spare=plan.spare_merchants.get(vid, 0),
                free=plan.free_merchants(vid),
                over_budget=vid in over,
                trade_office_levels_needed=upgrades.get(vid),
                legs=_budget_legs(vid, plan, config.geometry, names, coords),
                explanation=(
                    _explain_over_budget(
                        village_label(vid, names),
                        plan.merchants_committed.get(vid, 0),
                        plan.spare_merchants.get(vid, 0),
                        _budget_legs(vid, plan, config.geometry, names, coords),
                        trade_office.get(vid, 0),
                        config.merchant_model.capacity(trade_office.get(vid, 0)),
                        upgrades.get(vid),
                    )
                    if vid in over
                    else None
                ),
            )
            for vid in sorted(villages)
        ],
        shortfalls=[
            ShortfallResponse(
                village_id=s.village_id,
                village_name=village_label(s.village_id, names),
                resource=s.resource,
                per_hour=s.per_hour,
                reason=s.reason,
            )
            for s in plan.shortfalls
        ],
        unallocated=[
            UnallocatedResponse(
                resource=resource,
                total_production=rp.total_production,
                unallocated=rp.unallocated,
                remainder_village_id=rp.remainder_village_id,
            )
            for resource, rp in sorted(plan.resource_plans.items(), key=lambda kv: kv[0].value)
        ],
        total_merchants=plan.total_merchants,
        feasible=plan.is_feasible,
        warnings=[*plan.warnings, *extra_warnings],
    )


@router.post("/execute", response_model=ExecuteResponse)
async def post_execute(
    body: ExecuteRequest,
    user: User = Depends(get_current_user),
):
    """Create the plan's trade routes in-game — or preview them with dry_run.

    Recomputes the plan server-side from the same inputs /plan uses (it does
    NOT trust client-sent rows), then, per origin village, disables the routes
    the plan no longer wants and creates the ones still missing. Deliberately
    rate-capped to a few routes per run: a human sets routes up over days, not
    in one machine sweep, so the run stops once the cap is reached (leaving the
    rest as ``remaining`` for a later run) and origins are visited in randomized
    order. ``dry_run`` (default) previews with ZERO game requests and, like
    /plan, is auth-only — it never resolves a live session so it works offline.
    """
    account = await _plan_account(body)
    plan = account.plan
    names = account.names
    coords = account.coords
    # Resolve the session lazily (not via a dependency) so dry-run works offline
    # like /plan; the live path below requires a real connection.
    session = session_manager.get(user.id)
    svc = session.trade_route_service if session is not None else None
    live_enabled = bool(svc is not None and svc.live_enabled)
    warnings = list(account.warnings)

    # Each plan row is one route from a real origin village's marketplace to a
    # destination (a real village or a foreign sink — coords cover both).
    items: list[tuple[SheetRow, PlannedRoute]] = []
    for row in plan.rows:
        dest_xy = coords.get(row.destination)
        if dest_xy is None:
            warnings.append(
                f"{village_label(row.origin, names)} → "
                f"{village_label(row.destination, names)}: destination coordinates "
                f"unknown, route skipped"
            )
            continue
        items.append(
            (
                row,
                PlannedRoute(
                    origin_village_id=row.origin,
                    dest_x=dest_xy[0],
                    dest_y=dest_xy[1],
                    dest_name=village_label(row.destination, names),
                    cargo=dict(row.cargo),
                    cycle_hours=row.cycle_hours,
                    merchants=row.merchants,
                ),
            )
        )

    def _action(
        row: SheetRow, route: PlannedRoute, status_: str, detail: str = ""
    ) -> RouteActionResponse:
        return RouteActionResponse(
            origin=row.origin,
            origin_name=village_label(row.origin, names),
            destination=row.destination,
            destination_name=village_label(row.destination, names),
            dest_x=route.dest_x,
            dest_y=route.dest_y,
            cargo={r: amount for r, amount in row.cargo.items() if amount},
            cycle_hours=row.cycle_hours,
            merchants=row.merchants,
            status=status_,
            detail=detail,
        )

    cap = body.max_routes_per_run

    if body.dry_run:
        # Zero game requests: the exact routes already on each marketplace are
        # unknown here, so this previews the DESIRED plan against a worst-case
        # empty marketplace (first `cap` created, the rest deferred). The live
        # run reads each marketplace and only creates the routes that are
        # actually missing and disables only the ones the plan no longer wants,
        # so it may create/disable fewer than shown.
        actions = [_action(row, route, "would_create") for row, route in items[:cap]]
        actions += [_action(row, route, "deferred") for row, route in items[cap:]]
        disables = (
            ["Existing routes not in this plan would be disabled first (read at execution)."]
            if body.disable_existing
            else []
        )
        return ExecuteResponse(
            dry_run=True,
            live_enabled=live_enabled,
            actions=actions,
            disables=disables,
            created=0,
            remaining=max(0, len(items) - cap),
            warnings=warnings,
        )

    # ── Live ───────────────────────────────────────────────────────────
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Not connected. Reconnect first — live trade-route execution "
                "never spends login traffic implicitly. Use dry_run to preview."
            ),
        )
    if not live_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Live trade-route execution is disabled until the "
                "/api/v1/trade-routes request payload is captured and verified. "
                "Use dry_run to preview what would be created."
            ),
        )
    # Feasibility is enforced server-side, not just by the disabled UI button: a
    # direct API call must not commit an over-budget/unroutable plan (dry_run
    # still previews it, warnings and all).
    if not plan.is_feasible:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Plan is not feasible; refusing to execute in-game. " + " ".join(plan.warnings)
            ).strip(),
        )

    # Reconcile the desired plan against what is actually on each marketplace,
    # origin by origin, in randomized order (not a predictable village-id sweep):
    #   * create only routes MISSING in-game, so a create sticks and the next
    #     run naturally advances to the routes still absent — never re-creating
    #     the same routes each run (a daily rebuild-the-same-routes bot signal);
    #   * disable only VISIBLE routes the plan no longer wants — never a route we
    #     are about to (re)create;
    #   * hidden entries are honeypots: a human can't see them, so we neither act
    #     on them (never disabled) nor let them influence us (not deduped
    #     against) — behaving exactly like a human who cannot see them.
    # The run is bounded by `cap` in BOTH dimensions: it reads at most `cap`
    # marketplaces AND creates at most `cap` routes. That second bound (origins
    # visited) is what keeps a fully-provisioned account from re-reading every
    # village on every run — in steady state every route is "skipped" and no
    # create ever fires, so a create-only cap would never stop the sweep. The
    # rest defer to a later run; shuffling means successive runs cover them all.
    #
    # Known limitation: only origins the current plan still uses are visited, so
    # a village dropped from the plan entirely keeps its old routes until it
    # re-enters a plan. Cleaning those would require reading every village's
    # marketplace — the exact full sweep this bound exists to avoid — so it is
    # deliberately left to a plan that still includes the village.
    desired_by_origin: dict[int, list[tuple[SheetRow, PlannedRoute]]] = {}
    for row, route in items:
        desired_by_origin.setdefault(route.origin_village_id, []).append((row, route))
    origins = list(desired_by_origin)
    random.shuffle(origins)

    actions: list[RouteActionResponse] = []
    disables: list[str] = []
    attempts = 0  # create requests fired this run
    visited = 0  # marketplaces read this run
    outstanding = 0  # creates attempted but not completed (failed / Gold Club)
    deferred: list[tuple[SheetRow, PlannedRoute]] = []
    for origin in origins:
        if attempts >= cap or visited >= cap:
            # Budget spent: defer every remaining origin WITHOUT reading its
            # marketplace, so reads and disable writes stay bounded to this run.
            deferred.extend(desired_by_origin[origin])
            continue
        async with svc.origin_lock(origin):
            visited += 1
            existing = await svc.list_existing_routes(origin)
            desired = desired_by_origin[origin]
            desired_coords = {(route.dest_x, route.dest_y) for _, route in desired}
            # Only routes a human can see count as "already there" / disable-able;
            # hidden honeypots are ignored entirely.
            visible_coords = {(e.dest_x, e.dest_y) for e in existing if e.visible}

            if body.disable_existing:
                stale = [
                    e for e in existing if e.visible and (e.dest_x, e.dest_y) not in desired_coords
                ]
                disabled = await svc.disable_routes(origin, stale)
                if disabled is not None:
                    disables.append(
                        f"{village_label(origin, names)}: {disabled.status} {disabled.detail}".strip()
                    )

            for row, route in desired:
                if (route.dest_x, route.dest_y) in visible_coords:
                    actions.append(_action(row, route, "skipped", "route already active"))
                    continue
                if attempts >= cap:
                    deferred.append((row, route))
                    continue
                attempts += 1
                result = await svc.create_route(route)
                if result.status != "created":
                    outstanding += 1  # failed or Gold-Club-skipped → still to do
                actions.append(_action(row, route, result.status, result.detail))

    actions += [_action(row, route, "deferred") for row, route in deferred]

    return ExecuteResponse(
        dry_run=False,
        live_enabled=live_enabled,
        actions=actions,
        disables=disables,
        # `remaining` = work still outstanding for a later run: routes deferred by
        # the cap PLUS any create that did not complete (failed / Gold Club), so
        # the summary never makes a partially-done run look complete.
        created=sum(1 for a in actions if a.status == "created"),
        remaining=len(deferred) + outstanding,
        warnings=warnings,
    )
