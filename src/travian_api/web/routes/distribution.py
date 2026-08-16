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
from travian_api.services.distribution.planner import PlannerConfig, craft_plan
from travian_api.services.distribution.schedule import MINUTES_PER_DAY
from travian_api.services.distribution.storage import (
    simulate_day,
    storage_warnings,
    store_status,
)
from travian_api.web.auth import get_current_user
from travian_api.web.models.db import User
from travian_api.web.sessions import TravianSession, get_live_travian_session

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
    resource: Resource
    per_hour: float
    reason: str


class UnallocatedResponse(BaseModel):
    resource: Resource
    total_production: float
    unallocated: float
    remainder_village_id: int | None


class PlanResponse(BaseModel):
    rows: list[SheetRowResponse]
    budgets: list[BudgetResponse]
    shortfalls: list[ShortfallResponse]
    unallocated: list[UnallocatedResponse]
    total_merchants: int
    feasible: bool
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
        # a planned gap and an apparent broken promise.
        first = max(
            (row.first_delivery_hours for row in plan.rows if row.destination == target_id),
            default=0.0,
        )
        extra_warnings.append(
            f"{target.name} ({target.x}|{target.y}): first delivery lands "
            f"{first:.1f}h after the route is created, so the tribute starts "
            f"late unless it is covered by hand until then"
        )

    upgrades = {o.village_id: o.trade_office_levels_needed for o in plan.over_budget}
    over = {o.village_id for o in plan.over_budget}
    coords = {vid: village.coords for vid, village in villages.items()}

    return PlanResponse(
        rows=[
            SheetRowResponse(
                origin=row.origin,
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
