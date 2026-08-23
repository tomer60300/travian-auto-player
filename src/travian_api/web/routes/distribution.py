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
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from travian_api.exceptions import ActivityBudgetExhausted, NetworkError, TravianError
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
from travian_api.services.distribution.execution_trace import (
    ExecutionTrace,
    read_inventories,
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
from travian_api.services.distribution.route_revert import describe, plan_revert
from travian_api.services.distribution.schedule import MINUTES_PER_DAY
from travian_api.services.distribution.storage import (
    ProfileSegment,
    simulate_day,
    simulate_profile_cycle,
    storage_warnings,
    store_status,
)
from travian_api.services.trade_route_service import (
    ExistingRoute,
    MarketplaceUnreadable,
    PlannedRoute,
)
from travian_api.web.auth import get_current_user
from travian_api.web.models.db import User
from travian_api.web.operation_gate import active_ops, captcha_stop
from travian_api.web.sessions import TravianSession, get_live_travian_session, session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/distribution", tags=["distribution"])

# Active-op label for a live trade-route execution, so the session-lifecycle
# guards (disconnect/reconnect) see the work and don't close the HttpClient
# underneath it, and the captcha-stop signal can halt it.
_EXECUTE_OP_LABEL = "trade-route-execute"

# Europe 2 is x1 with coordinates running -200..+200. Exposed in the response so
# the UI can show what the distances were computed against.
# Below this a store counts as level rather than drifting. Float residue from
# the settling loop is not a trend worth naming in a warning.
# A day has room for a handful of meaningfully different allocation profiles.
# The real ceiling is cost, not arithmetic: each one is its own optimizer run.
MAX_DAY_SEGMENTS = 12

NEGLIGIBLE_DRIFT_PER_DAY = 1.0

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
    route_eligible: bool = Field(
        default=False,
        description=(
            "Whether Travian will actually let a Gold Club trade route target "
            "this village. Routes are only allowed to your OWN villages, Wonder "
            "of the World villages, or artifact villages in your alliance / "
            "confederacy — not an ordinary ally or sitter village. The operator "
            "must assert this (it cannot be verified server-side). When false the "
            "obligation is reported as a MANUAL transfer and is not emitted as a "
            "route or given merchants in the plan."
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
    # Odd only. A Travian world is centred on 0|0, so its width is always odd;
    # an even span shifts every tile index by half a field, which silently
    # skews every distance MapGeometry computes from it.
    map_span: int = Field(default=DEFAULT_MAP_SPAN, gt=0)
    speed_fields_per_hour: float = Field(default=DEFAULT_SPEED_FIELDS_PER_HOUR, gt=0)

    @field_validator("map_span")
    @classmethod
    def _span_is_odd(cls, value: int) -> int:
        if value % 2 == 0:
            raise ValueError("map_span must be odd: a world is centred on 0|0")
        return value

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
    dispatch_window: tuple[int, int] | None = Field(
        default=None,
        description=(
            "Minutes past midnight (start, end) of the hours this route set "
            "actually runs, so each route's send time is phased into them. Send "
            "this whenever the plan belongs to a profile that does not run all "
            "day: without it the sheet prints -- and /execute CREATES -- routes "
            "whose send time can fall outside the profile's own hours, which in "
            "game means shipping that profile's allocation while a different one "
            "is meant to be running. May wrap past midnight."
        ),
    )

    @field_validator("reserved_window", "dispatch_window")
    @classmethod
    def _window_within_the_day(
        cls, value: tuple[int, int] | None, info: ValidationInfo
    ) -> tuple[int, int] | None:
        # A window may wrap past midnight (start > end), so ordering carries
        # meaning rather than being an error; only the bounds are checked.
        if value is None:
            return value
        field = info.field_name or "window"
        if not all(0 <= minute < MINUTES_PER_DAY for minute in value):
            raise ValueError(f"{field} minutes must be 0-{MINUTES_PER_DAY - 1}")
        # Zero width means something different for each. An empty reserved
        # window simply reserves nothing, which is harmless; an empty DISPATCH
        # window says no minute of the day may carry a send, which build_beat
        # rejects outright -- so accepting it here would turn a client typo into
        # a 500 rather than a validation error.
        if field == "dispatch_window" and value[0] == value[1]:
            raise ValueError(
                "dispatch_window is zero-width; give the profile some hours or omit it"
            )
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
    # Unknown fields are REJECTED here, unlike everywhere else in this module.
    #
    # This is the only endpoint that writes to a real account, and its request
    # carries the safety controls: max_routes_per_run and the only_* filters. A
    # server that silently discarded one of those would run the FULL plan while
    # the operator believed they had narrowed it to a single route.
    #
    # That is not hypothetical. A browser holding a newer bundle sent
    # only_origins/only_destinations to a backend that predated them; Pydantic's
    # default is to ignore unknown fields, so the filter vanished, the run
    # selected a different village pair on a 1-hour cycle (24 game rows rather
    # than the intended 1), and nothing in the response said the filter had been
    # dropped. A 422 is the only safe answer to a parameter this endpoint does
    # not understand.
    model_config = {"extra": "forbid"}

    # Narrow a live run to specific villages. Built for controlled testing: the
    # first live run against a real account should be able to be exactly one
    # route between two chosen villages, not "whichever one the cap happened to
    # reach first". Also useful for re-running a single village after a failure
    # without touching the rest.
    #
    # A filtered run is reported as filtered (see ExecuteResponse.filtered_to),
    # because a partial run mistaken for a complete one is precisely the kind of
    # false confidence that makes an operator stop checking.
    only_origins: list[int] | None = Field(
        default=None,
        description="Run only routes leaving these origin village ids.",
    )
    only_destinations: list[int] | None = Field(
        default=None,
        description="Run only routes arriving at these destination village ids.",
    )

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
    # How many rows this ONE create request becomes in the game. Travian fans a
    # "repeat every N hours" route out into 24/N daily rows, so a request is not
    # a row -- and an operator who authorised "3 routes" on a 1-hour cycle has
    # authorised 72 rows. Reported so that is never a surprise.
    game_rows: int = 1
    # would_create | deferred | created | created_unverified | not_created |
    # skipped | blocked | failed
    #
    # `created` means VERIFIED: the marketplace was read back and the route is
    # there. `created_unverified` means the write was accepted but the read-back
    # failed -- probably fine, not confirmed. `not_created` means the game
    # accepted the write and produced no route, which a 200 with an empty body
    # cannot distinguish from success on its own.
    status: str
    detail: str = ""


class ExecuteResponse(BaseModel):
    dry_run: bool
    live_enabled: bool
    actions: list[RouteActionResponse]
    disables: list[str]
    # Kept apart from `disables` deliberately: re-enabling a route the plan
    # still wants RESTARTS shipments, which is the opposite of disabling one.
    # Folding them together reported a resumed route as a stopped one.
    re_enables: list[str] = []
    created: int
    remaining: int
    # Benign, informational notes computed while planning (coordinate resolution,
    # tribute timing, storage/merchant hints). NOT failures.
    warnings: list[str]
    # Genuine execution problems the operator must act on (a failed disable, a
    # Gold-Club block). Kept separate from `warnings` so a benign planner note
    # never makes a successful run look failed.
    problems: list[str] = []
    # Set when only_origins/only_destinations narrowed the run. A partial run
    # mistaken for a complete one is worse than no run, so this is stated rather
    # than left to be inferred from a short action list.
    filtered_to: str | None = None
    # Rows the game will hold as a result of this run's creates, which is not the
    # same number as the creates: see RouteActionResponse.game_rows.
    created_game_rows: int = 0
    # Where this run's full decision-and-request trace was written. A live run is
    # the one operation here that changes a real account, and the response alone
    # cannot say WHY each route was skipped or disabled -- the trace can.
    trace_id: str | None = None
    trace_path: str | None = None


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


class DayCheckRequest(PlanRequest):
    """The whole day at once: every profile, each in its own hours.

    Costs zero game requests. Profiles are planned in isolation, but the account
    lives through all of them every day -- what the day profile ships decides
    the stock the night profile starts from, so questions like "does the capital
    cross 90k during the night?" can only be answered by simulating the day as
    the profiles will actually run it.

    Inherits every planner input from :class:`PlanRequest` -- snapshot, Trade
    Office levels, merchant model, geometry, tributes -- because each profile is
    routed through the same optimizer /plan uses. Sharing the model is what stops
    the two endpoints drifting into different answers for the same account. Only
    ``allocations`` moves: it lives per segment, since that is what a profile
    *is*.
    """

    segments: list[DaySegmentInput] = Field(
        min_length=1,
        max_length=MAX_DAY_SEGMENTS,
        description=(
            "One entry per allocation profile. Capped because each segment runs a "
            "full optimizer pass plus a storage replay, so the handler's cost is "
            "linear in this length -- and windows only have to be non-overlapping, "
            "so 1,440 one-minute profiles would otherwise validate."
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

    @field_validator("dispatch_window")
    @classmethod
    def _windows_live_on_segments(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        """Each segment carries its own hours; a single top-level one is wrong."""
        if value is not None:
            raise ValueError(
                "dispatch_window belongs to each entry in `segments`, not at the "
                "top level -- every profile runs its own hours"
            )
        return value

    @field_validator("allocations")
    @classmethod
    def _allocations_live_on_segments(cls, value: dict) -> dict:
        """Reject a top-level allocation set rather than silently ignoring it."""
        if value:
            raise ValueError(
                "allocations belong to each entry in `segments`, not at the top "
                "level -- a profile is defined by its own allocations"
            )
        return value


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

    Each profile is planned through the same planner /plan uses, told the hours
    it runs, so its sends are phased into them and its cargo lands as discrete
    batches at the minute it arrives -- under whichever profile owns that
    minute. Obligations no route can carry are the exception: they have no
    dispatch or travel time to model, so they run as a rate confined to their
    profile's hours.
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
    # get no trajectory of their own -- the drain lives in whoever funds them,
    # so all this pass needs is a label to report them under. The simulation runs
    # on `own_rates`, which is built from the snapshot above and never includes a
    # sink; registering a sink's zero rate here would land in `productions`,
    # which nothing downstream reads.
    if body.foreign_targets and Resource.CROP not in productions:
        warnings.append(
            "crop: no rate could be read for any village, so the foreign tribute "
            "cannot be simulated -- the day picture is missing that drain"
        )
    elif body.foreign_targets:
        for index, target in enumerate(body.foreign_targets):
            names[-(index + 1)] = target.name

    segments: list[ProfileSegment] = []
    for segment in body.segments:
        # Route each profile through the SAME planner /plan and /execute use, so
        # the day picture is built from the real route set -- actual cycles,
        # merchant budgets, relays and shortfalls -- rather than from allocation
        # intent that no route may realise. It also means the two endpoints
        # cannot answer the same account differently.
        #
        # The profile's own hours go with it: the beat must phase its sends into
        # them, or every firing can land in hours the profile is not running and
        # the route ships nothing at all.
        per_profile = body.model_copy(update={"allocations": segment.allocations})
        try:
            account = await _plan_account(per_profile, dispatch_window=segment.window)
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=f"{segment.name}: {exc.detail}"
            ) from exc
        # /plan surfaces plan.warnings and account.warnings separately, so a
        # profile needs both: the allocation-level ones (unallocated slack, a
        # receiver nothing sources) live on the plan.
        for warning in (*account.plan.warnings, *account.warnings):
            warnings.append(f"{segment.name}: {warning}")
        # Demand the optimizer could not route is not a rate the day can spend:
        # crediting it would let the composite report a green all-clear over a
        # shortfall /plan is already reporting in red.
        for shortfall in account.plan.shortfalls:
            warnings.append(
                f"{segment.name}: {village_label(shortfall.village_id, names)} is short "
                f"{shortfall.per_hour:,.0f}/h of {shortfall.resource.value} -- "
                f"{shortfall.reason}, so the day runs without it"
            )
        # Tributes Travian will not let a route target are still a real drain on
        # whoever ships them by hand. The optimizer correctly leaves them out of
        # the route set, so carry them as a manual rate instead -- dropping them
        # would flatter the day by the whole obligation, every hour of it.
        manual: dict[int, dict[Resource, float]] = {}
        owed_by_hand = sum(
            target.crop_per_hour * (1.0 + target.safety_margin_pct / 100.0)
            for target in body.foreign_targets
            if not target.route_eligible
        )
        if owed_by_hand:
            crop_allocations = segment.allocations.get(Resource.CROP, {})
            funder = next(
                (
                    vid
                    for vid, item in crop_allocations.items()
                    if item.mode is AllocationMode.REMAINDER
                ),
                None,
            )
            if funder is None:
                warnings.append(
                    f"{segment.name}: no crop remainder village, so nothing funds the "
                    f"tribute ({owed_by_hand:,.0f}/h owed by hand) during it -- modeled "
                    f"as unpaid in those hours"
                )
            else:
                manual.setdefault(funder, {})[Resource.CROP] = -owed_by_hand

        segments.append(
            ProfileSegment(
                name=segment.name,
                start_minute=segment.window[0],
                end_minute=segment.window[1],
                routes=account.plan.beat.routes,
                manual_rates=manual,
            )
        )
    # Off the event loop for the same reason craft_plan is: pure CPU that must
    # not stall WebSocket frames or stealth-timed game requests. The simulation
    # got much heavier when it became discrete -- a tick per dispatch and per
    # arrival on top of a 5-minute grid, ~1,800 ticks a day against 96 before --
    # and it measured ~1.9s of uninterruptible work on a 20-village account.
    trajectories, breaches = await asyncio.to_thread(
        simulate_profile_cycle, segments, own_rates, stocks, capacities, body.crop_ceilings
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

    # `settled` is one flag for the whole day, so an unsettled run marks every
    # row without saying which store is responsible -- and low/high then
    # describe the horizon day rather than a repeating one. Whole-batch cargo
    # rarely equals what a village produced inside a finite profile window, so
    # this is the normal outcome for a part-day plan rather than a rare fault.
    # Name the worst drifters: the daily net already says which they are.
    if trajectories and not all(t.settled for t in trajectories):
        drifting = sorted(
            (t for t in trajectories if abs(t.daily_net) >= NEGLIGIBLE_DRIFT_PER_DAY),
            key=lambda t: -abs(t.daily_net),
        )[:3]
        if drifting:
            named = ", ".join(
                f"{village_label(t.village_id, names)} {t.resource.value} {t.daily_net:+,.0f}/day"
                for t in drifting
            )
            warnings.append(
                f"the day never repeats: {named}. Stocks shown are the last "
                f"simulated day, not a settled one, and a store drifting this way "
                f"crosses its cap or empties eventually — usually a cycle that "
                f"does not divide evenly into its profile's hours"
            )
        else:
            warnings.append(
                "the day never repeats, though no store drifts meaningfully — "
                "the stocks shown are the last simulated day rather than a settled one"
            )

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


async def _plan_account(
    body: PlanRequest, dispatch_window: tuple[int, int] | None = None
) -> _PlannedAccount:
    """Build the account model, run the optimizer, resolve coords + warnings.

    Pure of game I/O, so it is shared by the zero-request /plan endpoint and by
    the dry-run computation inside /execute.

    ``dispatch_window`` is the hours of the day this route set runs, for a plan
    that belongs to one allocation profile rather than to the whole day. It
    phases the sends into those hours; /plan and /execute leave it None and get
    the round-the-clock beat.
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
    #
    # Only route-eligible targets (own / WW / alliance-artifact villages) enter
    # the route optimizer: Travian will not create a Gold Club route to an
    # ordinary foreign village, so emitting one would be an unexecutable row that
    # also reserves merchants and crop the operator cannot actually use that way.
    # Ineligible targets are reported as manual transfers instead.
    foreign_ids: dict[int, ForeignTarget] = {}
    manual_targets: list[ForeignTarget] = []
    for index, target in enumerate(body.foreign_targets):
        if not target.route_eligible:
            manual_targets.append(target)
            continue
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
        # The explicit argument wins (the day check passes each segment's own
        # hours); otherwise take what the client sent on the request, which is
        # how /plan and /execute learn the active profile's window.
        dispatch_window=dispatch_window if dispatch_window is not None else body.dispatch_window,
        min_send_fill=body.min_send_fill,
        max_improve_passes=body.max_improve_passes,
        max_relay_hops=body.max_relay_hops,
    )

    extra_warnings: list[str] = []
    for target in manual_targets:
        extra_warnings.append(
            f"{target.name} ({target.x}|{target.y}): Travian only allows Gold Club "
            f"trade routes to your own, Wonder, or alliance/confederacy artifact "
            f"villages, so its {target.crop_per_hour:.0f}/h crop obligation is a MANUAL "
            f"transfer — it is not in the route plan and no merchants are reserved for "
            f"it. Ship it by hand, or mark it route-eligible if it really is one of "
            f"those villages."
        )
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

    # Off the loop for the same reason craft_plan is, three lines above: this
    # runs simulate_day, a 14-day discrete replay of the beat. Measured with a
    # 10ms heartbeat alongside the request, it was a single 292ms stall at 23
    # villages and 566ms at 40 -- and the day check calls this once per profile,
    # so three profiles blocked the loop for ~1.6s while stealth-timed game
    # requests and WebSocket frames waited.
    extra_warnings.extend(await asyncio.to_thread(_storage_warnings, body, plan))

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
        # Section 7.3: a tribute must not lapse. A Gold Club route sends at its
        # scheduled "Send at" time, so the first delivery lands at the next
        # occurrence of that time plus travel. Worst case — the route is created
        # just after its send time — that is a full cycle plus travel; this is the
        # conservative upper bound on how long the operator must cover it by hand.
        # With several suppliers the earliest route bounds first crop; the slowest
        # bounds when the full rate is flowing.
        firsts = [row.first_delivery_hours for row in plan.rows if row.destination == target_id]
        first = min(firsts, default=0.0)
        full = max(firsts, default=0.0)
        note = (
            f"{target.name} ({target.x}|{target.y}): the first crop can take up to "
            f"{first:.1f}h to land (a full cycle plus travel if the route is created "
            f"just after its scheduled send time)"
        )
        if full > first + 0.05:
            note += f", and the full tribute up to {full:.1f}h"
        note += ", so cover it by hand until the first scheduled send lands"
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


class RevertPlanRequest(BaseModel):
    """Ask what it would take to undo a previous live run."""

    trace_id: str = Field(
        min_length=1,
        description=(
            "The trace_id an /execute response returned. Its recorded pre-write "
            "inventory is the only record of what each village looked like before "
            "the run, because the game returns no id when it creates a route."
        ),
    )
    origins: list[int] | None = Field(
        default=None,
        description=(
            "Limit the check to these origin villages. Each one costs two game "
            "requests to re-read, so a one-village canary should say so rather "
            "than re-reading every village the run touched."
        ),
    )
    apply_disable: bool = Field(
        default=False,
        description=(
            "Actually disable the routes the run created. This is the only half of "
            "a revert the app can perform: deleting a route has never been captured "
            "as a request, so removal stays a manual step in the UI."
        ),
    )
    map_span: int = Field(default=DEFAULT_MAP_SPAN, gt=0)


class RevertPlanResponse(BaseModel):
    trace_id: str
    # Ordered operator instructions, disable-before-delete.
    steps: list[str]
    # Route ids per origin, so a caller can act without parsing prose.
    created: dict[int, list[int]] = {}
    disabled_now: dict[int, list[int]] = {}
    must_delete_by_hand: dict[int, list[int]] = {}
    restore_state: dict[int, list[str]] = {}
    clean: bool
    requests_used: int
    problems: list[str] = []


@router.post("/routes/revert-plan", response_model=RevertPlanResponse)
async def post_revert_plan(
    body: RevertPlanRequest,
    user: User = Depends(get_current_user),
    session: TravianSession | None = Depends(get_live_travian_session),
):
    """What it would take to put things back as they were before a live run.

    Reverting is deliberately not a single button. The app can disable what it
    created; it cannot delete, because that request has never been captured, and
    a revert that claimed to have undone a run while leaving live routes behind
    would be worse than one that names the rows a person still has to remove.

    So this reports both halves, disable first: a created route left enabled
    while someone gets round to deleting it keeps shipping resources.
    """
    try:
        before = read_inventories(body.trace_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No trace for run {body.trace_id}. Without the pre-run inventory "
                f"every existing route would look newly created, so this refuses "
                f"rather than guess."
            ),
        ) from None
    if not before:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {body.trace_id} read no marketplace, so it created nothing "
                f"and there is nothing to revert."
            ),
        )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not connected. Comparing against the game needs a live session.",
        )

    svc = session.trade_route_service
    origins = [o for o in (body.origins or sorted(before)) if o in before]
    if not origins:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"None of those origins appear in run {body.trace_id}.",
        )

    steps: list[str] = []
    problems: list[str] = []
    created: dict[int, list[int]] = {}
    disabled_now: dict[int, list[int]] = {}
    must_delete: dict[int, list[int]] = {}
    restore: dict[int, list[str]] = {}
    requests_used = 0
    clean = True

    for origin in origins:
        try:
            now = await svc.list_existing_routes(origin, map_span=body.map_span)
            requests_used += 2  # dorf2 + the marketplace tab
        except (NetworkError, MarketplaceUnreadable) as exc:
            # Conclude nothing about a village we could not read: an unreadable
            # page would otherwise look like "every route vanished".
            problems.append(
                f"village {origin}: could not re-read the marketplace ({exc}); "
                f"nothing concluded or changed for this village"
            )
            clean = False
            continue

        after = [
            {"route_id": e.route_id, "dest": e.dest_village_id, "active": e.active} for e in now
        ]
        plan = plan_revert(origin, before[origin], after)
        steps.extend(describe(plan))
        if plan.is_clean:
            continue
        clean = False
        created[origin] = plan.manual_delete_ids
        must_delete[origin] = plan.manual_delete_ids
        if plan.to_restore:
            restore[origin] = [
                f"route {rid} -> {'enabled' if was else 'disabled'}" for rid, was in plan.to_restore
            ]

        if body.apply_disable and plan.disable_ids:
            live = [e for e in now if e.route_id in set(plan.disable_ids)]
            result = await svc.disable_routes(origin, live)
            requests_used += 1
            if result is not None and result.status == "disabled":
                disabled_now[origin] = plan.disable_ids
                steps.append(
                    f"village {origin}: disabled {len(plan.disable_ids)} created "
                    f"route(s) - they are inert now, but still need deleting"
                )
            else:
                detail = result.detail if result is not None else "no request was made"
                problems.append(
                    f"village {origin}: could not disable created routes "
                    f"{plan.disable_ids} ({detail}); they are STILL RUNNING"
                )

    return RevertPlanResponse(
        trace_id=body.trace_id,
        steps=steps or [f"run {body.trace_id}: nothing to revert"],
        created=created,
        disabled_now=disabled_now,
        must_delete_by_hand=must_delete,
        restore_state=restore,
        clean=clean,
        requests_used=requests_used,
        problems=problems,
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
    # like /plan. The live path below requires a real connection: this mirrors
    # get_live_travian_session's contract exactly — the live session if present,
    # else 403, and deliberately NO implicit auto-reconnect (live execution must
    # never spend login traffic behind the operator's back).
    session = session_manager.get(user.id)
    svc = session.trade_route_service if session is not None else None
    live_enabled = bool(svc is not None and svc.live_enabled)
    # Same warning set /plan surfaces (both share _plan_account): the optimizer's
    # own notes plus the account-level ones, so the two endpoints never disagree.
    warnings = [*plan.warnings, *account.warnings]

    # Each plan row is one route from a real origin village's marketplace to a
    # destination (a real village or a foreign sink — coords cover both).
    items: list[tuple[SheetRow, PlannedRoute]] = []
    filtered_out = 0
    for row in plan.rows:
        # Fail-closed execution-boundary guard: a live route may only originate
        # at a real, positive account village. The optimizer already excludes
        # non-sender origins, but an impossible negative-id (foreign sink) origin
        # must never reach a marketplace/create request even if a future planner
        # change regressed — drop it loudly rather than fire it at the game.
        if row.origin <= 0:
            warnings.append(
                f"{village_label(row.origin, names)} → "
                f"{village_label(row.destination, names)}: route origin is not a real "
                f"account village, skipped (it can never be executed)"
            )
            continue
        dest_xy = coords.get(row.destination)
        if dest_xy is None:
            warnings.append(
                f"{village_label(row.origin, names)} → "
                f"{village_label(row.destination, names)}: destination coordinates "
                f"unknown, route skipped"
            )
            continue
        # Applied here, after the boundary guards, so a filtered-out route is
        # never confused with one that was rejected as unexecutable.
        if body.only_origins is not None and row.origin not in body.only_origins:
            filtered_out += 1
            continue
        if body.only_destinations is not None and row.destination not in body.only_destinations:
            filtered_out += 1
            continue
        items.append(
            (
                row,
                PlannedRoute(
                    origin_village_id=row.origin,
                    dest_village_id=row.destination,
                    dest_x=dest_xy[0],
                    dest_y=dest_xy[1],
                    dest_name=village_label(row.destination, names),
                    cargo=dict(row.cargo),
                    cycle_hours=row.cycle_hours,
                    merchants=row.merchants,
                    # Carry the planner's scheduled send time through to live
                    # execution — without it the beat that spaces arrivals and
                    # orders relay hubs is lost.
                    dispatch_minute=row.dispatch_minute,
                ),
            )
        )

    def _game_rows(cycle_hours: int) -> int:
        """Rows one create request becomes in the game.

        Travian implements "repeat every N hours" by generating 24/N separate
        daily route rows, each departing at its own time -- measured against a
        real marketplace page, where every destination's row count was exactly
        24 divided by its departure spacing. A cycle the day does not divide
        evenly cannot arise here (DAILY_BEAT_CYCLES is the divisors of 24), but
        round up rather than silently under-report if one ever does.
        """
        if cycle_hours <= 0:
            return 1
        return max(1, -(-24 // cycle_hours))

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
            game_rows=_game_rows(row.cycle_hours),
            status=status_,
            detail=detail,
        )

    def _filter_description() -> str | None:
        if body.only_origins is None and body.only_destinations is None:
            return None
        parts = []
        if body.only_origins is not None:
            parts.append("origins " + ", ".join(village_label(v, names) for v in body.only_origins))
        if body.only_destinations is not None:
            parts.append(
                "destinations " + ", ".join(village_label(v, names) for v in body.only_destinations)
            )
        return (
            f"{' and '.join(parts)} — {filtered_out} other planned route(s) were NOT "
            f"considered by this run"
        )

    filtered_to = _filter_description()

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
            re_enables=[],
            created=0,
            created_game_rows=sum(a.game_rows for a in actions if a.status == "would_create"),
            filtered_to=filtered_to,
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
                "Live trade-route execution is disabled. The request payload is "
                "verified against a captured client request, so this is an explicit "
                "opt-in and not a missing capability: set TRAVIAN_TRADE_ROUTE_LIVE="
                "true on the server to allow it. Use dry_run to preview what would "
                "be created."
            ),
        )
    # Feasibility is enforced server-side, not just by the disabled UI button: a
    # direct API call must not commit an over-budget/unroutable plan (dry_run
    # still previews it, warnings and all).
    if not plan.is_feasible:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Plan is not feasible; refusing to execute in-game. " + " ".join(plan.warnings)
            ).strip(),
        )

    # One clear refusal before the loop, rather than N identical per-route
    # failures once inside it. The service guards create_route too, as defence
    # in depth for any other caller.
    # Not reachable while ROUTE_LIST_MARKUP_VERIFIED is True, which is the
    # service default. Kept as the refusal path for a caller that constructs the
    # service with the flag off, and for the day a gpack moves the model.
    if not svc.reconciler_verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Live execution is blocked: the marketplace route-list markup has "
                "not been confirmed against a real page, so a page this parser "
                "cannot read is indistinguishable from a village with no routes. "
                "Creating on that basis would re-create the whole plan on every "
                "run and accumulate duplicate routes in-game. Capture "
                "/build.php?gid=17&t=3 with at least one route present, confirm "
                "read_trade_routes finds the page's route model, then set "
                "ROUTE_LIST_MARKUP_VERIFIED. dry_run previews are unaffected."
            ),
        )

    # Reconcile the desired plan against what is actually on each marketplace,
    # origin by origin, in randomized order (not a predictable village-id sweep):
    #   * create only routes MISSING in-game, so a create sticks and the next run
    #     advances to the routes still absent — never re-creating the same routes
    #     each run (a daily rebuild-the-same-routes bot signal);
    #   * with `disable_existing`, disable only STALE visible routes (a
    #     destination the plan no longer wants). We NEVER disable a destination we
    #     are about to create, so a failed disable can never leave a duplicate,
    #     and an origin is never stripped of routes we cannot immediately replace;
    #   * a destination that already has a route is left untouched, and its
    #     parameters are NOT updated. The parser does now report cargo, repeat
    #     and merchants, so a changed route COULD be told from an unchanged one
    #     -- what is missing is an update call: Travian has no "edit route"
    #     endpoint we have captured, so applying a change means delete-then-
    #     create, and doing that whenever the plan's arithmetic shifts by a few
    #     crop would churn every route every run. The operator disables a route
    #     in-game to force a rebuild;
    #   * hidden entries would be honeypots: invisible to a human, so we would
    #     neither act on them (never disabled) nor let them influence us (never
    #     deduped against). VESTIGIAL today -- the page's React model has no
    #     hidden-entry concept, so the parser marks every route visible and this
    #     branch never fires. Kept because it is the right shape if a gpack ever
    #     grows one; do not read it as an active defence.
    #
    # The run is bounded by `cap` in BOTH dimensions: it reads at most `cap`
    # marketplaces AND fires at most `cap` creates. The origins-visited bound is
    # what keeps a fully-provisioned account from re-reading every village every
    # run (in steady state every route is skipped and no create fires, so a
    # create-only cap would never stop the sweep). The rest defer to a later run;
    # shuffling means successive runs cover them all.
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

    # One execute run per account at a time: a double-click or a second tab must
    # not fire two concurrent reconciliations that together bypass the per-run
    # caps and burst writes. Reject the overlap rather than queue it.
    if svc.execute_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A trade-route execution is already in progress for this account.",
        )

    actions: list[RouteActionResponse] = []
    disables: list[str] = []
    re_enables: list[str] = []
    problems: list[str] = []  # execution failures (failed disable, Gold Club, …)
    # Every live run gets a trace. This is the only endpoint here that mutates a
    # real account, and it does so through a chain of classification decisions
    # that the response can only summarise; a run that disabled the wrong route
    # would look identical in the response to one that disabled the right one.
    trace = ExecutionTrace()
    svc.trace = trace
    trace.event(
        "run_start",
        user=user.id,
        dry_run=False,
        live_enabled=live_enabled,
        reconciler_verified=svc.reconciler_verified,
        disable_existing=body.disable_existing,
        max_routes_per_run=cap,
        map_span=body.map_span,
        origins=len(origins),
        desired_routes=len(items),
        # What this run is authorised to put in the game at worst, in ROWS.
        max_game_rows_this_run=sum(_game_rows(row.cycle_hours) for row, _ in items[:cap]),
        # Recorded so a trace can never be read as a full run when it was not.
        filtered_to=filtered_to,
        planned_routes_excluded_by_filter=filtered_out,
    )

    attempts = 0  # create requests fired this run
    visited = 0  # marketplaces read this run
    outstanding = 0  # creates attempted but not completed (failed / Gold Club)
    deferred: list[tuple[SheetRow, PlannedRoute]] = []
    gold_club_blocked = False  # account-level: no route can be created at all
    stopped_early = False  # captcha resolved, budget exhausted, or a read failed

    # Register the run so the session-lifecycle guards see it: disconnect/
    # reconnect consults ActiveOpRegistry and will not close this HttpClient
    # underneath the run (issue #63). Registered synchronously before the first
    # game await so a concurrent disconnect can't slip in between. Unregistered
    # in `finally`. `started_at` lets the captcha-stop signal target only this run.
    started_at = time.monotonic()

    def _stop_reason() -> str | None:
        """Why the run must stop right now, or None. Checked before EVERY mutation
        (create/disable/enable) and again inside the service after its pacing wait
        (right before the POST), so no state-changing request slips past a captcha
        resolution (#62) or an exhausted activity budget (#64)."""
        if captcha_stop.should_stop(user.id, started_after=started_at):
            return "captcha resolved — execution stopped"
        try:
            svc.http_client.check_activity_budget()
        except ActivityBudgetExhausted as exc:
            return f"activity budget exhausted: {exc}"
        return None

    active_ops.register(user.id, _EXECUTE_OP_LABEL)
    try:
        async with svc.execute_lock:
            # Refuse to start if the activity budget is already exhausted, and
            # recheck before each origin so exhaustion mid-run stops the rest
            # (issue #64). check_activity_budget raises; per-request activity is
            # accounted by the HttpClient, as for the farm/oasis loops.
            try:
                svc.http_client.check_activity_budget()
            except ActivityBudgetExhausted as exc:
                problems.append(f"Activity budget exhausted; no routes were created: {exc}")
                deferred.extend(items)
                origins = []

            for origin in origins:
                if stopped_early or gold_club_blocked or attempts >= cap or visited >= cap:
                    # Budget spent, Gold Club missing, or the run was stopped
                    # (captcha / budget / a read failure): defer EVERY remaining
                    # origin WITHOUT reading its marketplace, so reads and writes
                    # stay bounded and no later origin's routes are silently lost
                    # from the response (issue #65).
                    trace.event(
                        "origin_deferred",
                        origin=origin,
                        routes=len(desired_by_origin[origin]),
                        reason=(
                            "run stopped early"
                            if stopped_early
                            else "gold club blocked"
                            if gold_club_blocked
                            else f"per-run cap of {cap} reached "
                            f"(creates={attempts}, marketplaces read={visited})"
                        ),
                    )
                    deferred.extend(desired_by_origin[origin])
                    continue
                # Don't even read a marketplace if the run is already stopped
                # (captcha resolved / budget exhausted). Fires once — later
                # origins are caught by the top-of-loop guard above.
                reason = _stop_reason()
                if reason:
                    stopped_early = True
                    problems.append(reason)
                    trace.event(
                        "origin_deferred",
                        origin=origin,
                        routes=len(desired_by_origin[origin]),
                        reason=reason,
                    )
                    deferred.extend(desired_by_origin[origin])
                    continue

                async with svc.origin_lock(origin):
                    # A marketplace read can fail AFTER earlier origins already
                    # committed writes; keep those in the structured response and
                    # stop rather than 500 out and lose the record (issue #65).
                    try:
                        existing = await svc.list_existing_routes(origin, map_span=body.map_span)
                        trace.event(
                            "origin_read",
                            origin=origin,
                            existing=len(existing),
                            active=sum(1 for e in existing if e.active),
                            placeable=sum(1 for e in existing if e.dest_x is not None),
                            destinations=sorted({e.dest_village_id for e in existing}),
                            # The FULL pre-write inventory, not just counts. This
                            # is the "old state" a revert needs: the game returns
                            # no id when it creates a route, so the only way to
                            # identify what a run added is to diff a later read
                            # against exactly what was there beforehand. Captured
                            # here because this read already happened -- deriving
                            # it later would cost another request per village.
                            inventory=[
                                {
                                    "route_id": e.route_id,
                                    "dest": e.dest_village_id,
                                    "active": e.active,
                                }
                                for e in existing
                            ],
                        )
                    except (NetworkError, MarketplaceUnreadable) as exc:
                        # The distinction that matters most in a trace: we do NOT
                        # know what this village already has, so nothing was
                        # created here. An unreadable page must never read as an
                        # empty village.
                        trace.event(
                            "origin_read_failed",
                            origin=origin,
                            error=str(exc),
                            error_type=type(exc).__name__,
                            routes_deferred=len(desired_by_origin[origin]),
                        )
                        problems.append(
                            f"{village_label(origin, names)}: marketplace read failed "
                            f"({exc}); remaining routes deferred"
                        )
                        deferred.extend(desired_by_origin[origin])
                        # Stop, but do NOT break: `continue` lets the top-of-loop
                        # guard defer every still-unvisited origin too, so they are
                        # counted in `remaining` instead of vanishing (issue #65).
                        stopped_early = True
                        continue
                    visited += 1
                    desired = desired_by_origin[origin]
                    # How a desired route is recognised in what is already
                    # there. Own villages match on village id, which the page
                    # states outright. A FOREIGN target has no id in the plan --
                    # it is an operator-supplied coordinate with a synthetic
                    # negative id -- so it can only match on coordinates, which
                    # are back-derived from the page's map id. Mixing the two is
                    # deliberate: keying everything on coordinates churns every
                    # own-village route whenever the world span is wrong, and
                    # keying everything on ids churns every foreign one always.
                    desired_ids = {
                        route.dest_village_id for _, route in desired if route.dest_village_id > 0
                    }
                    desired_foreign = {
                        (route.dest_x, route.dest_y)
                        for _, route in desired
                        if route.dest_village_id < 0
                    }

                    def _desired_key(route: PlannedRoute) -> int | tuple[int, int]:
                        if route.dest_village_id > 0:
                            return route.dest_village_id
                        return (route.dest_x, route.dest_y)

                    def _existing_keys(e: ExistingRoute) -> set[int | tuple[int, int]]:
                        """Every key this live route could be recognised by.

                        Both kinds, because the route itself does not say which
                        kind of plan entry (if any) wanted it. An int key and a
                        tuple key cannot collide, and a route whose coordinates
                        could not be derived contributes no coordinate key --
                        which is why an unplaceable map id no longer reads as a
                        route to nowhere that the plan does not want.
                        """
                        keys: set[int | tuple[int, int]] = {e.dest_village_id}
                        if e.dest_x is not None and e.dest_y is not None:
                            keys.add((e.dest_x, e.dest_y))
                        return keys

                    def _is_wanted(
                        e: ExistingRoute,
                        ids: set[int] = desired_ids,
                        foreign: set[tuple[int, int]] = desired_foreign,
                    ) -> bool:
                        return bool({e.dest_village_id} & ids or _existing_keys(e) & foreign)

                    # Honeypots (hidden) would be ignored entirely — neither
                    # acted on nor treated as occupying a destination. A no-op
                    # as things stand: nothing produces visible=False, because
                    # the page model has no hidden rows to read.
                    visible = [e for e in existing if e.visible]

                    if body.disable_existing:
                        # Disable only ACTIVE visible routes the plan no longer
                        # wants; a route already disabled needs no action.
                        stale = [e for e in visible if e.active and not _is_wanted(e)]
                        trace.event(
                            "stale_classified",
                            origin=origin,
                            stale_route_ids=[e.route_id for e in stale],
                            stale_destinations=[e.dest_village_id for e in stale],
                            wanted_village_ids=sorted(desired_ids),
                            wanted_coords=sorted(str(c) for c in desired_foreign),
                        )
                        if stale:
                            # Gate the disable mutation itself (issues #62/#64):
                            # check before, and again inside the service after its
                            # pacing wait via stop_check.
                            reason = _stop_reason()
                            if reason:
                                stopped_early = True
                                problems.append(reason)
                                deferred.extend(desired)
                                continue
                        disabled = await svc.disable_routes(origin, stale, stop_check=_stop_reason)
                        if disabled is not None:
                            line = (
                                f"{village_label(origin, names)}: "
                                f"{disabled.status} {disabled.detail}"
                            ).strip()
                            if disabled.status == "stopped":
                                # Stopped after the pacing wait, before the POST —
                                # nothing changed; defer this origin.
                                stopped_early = True
                                problems.append(disabled.detail)
                                deferred.extend(desired)
                                continue
                            if disabled.status == "failed":
                                # A failed/ambiguous disable leaves stale routes
                                # live; do NOT add new routes on top for this
                                # origin — defer them and reconcile on a later run
                                # after re-reading state (issue #61).
                                problems.append(
                                    f"Could not disable stale routes — {line}; "
                                    "skipping new routes for this origin this run"
                                )
                                deferred.extend(desired)
                                continue
                            disables.append(line)

                    # Only ENABLED routes satisfy the plan. A desired destination
                    # whose route exists but is DISABLED is re-enabled rather than
                    # duplicated; a desired destination with no route is created.
                    satisfied: set[int | tuple[int, int]] = set()
                    for e in visible:
                        if e.active:
                            satisfied |= _existing_keys(e)
                    disabled_desired = [e for e in visible if not e.active and _is_wanted(e)]
                    blocked: set[int | tuple[int, int]] = set()
                    if disabled_desired:
                        # Gate the re-enable mutation too (issues #62/#64).
                        reason = _stop_reason()
                        if reason:
                            stopped_early = True
                            problems.append(reason)
                            deferred.extend(desired)
                            continue
                        enabled = await svc.enable_routes(
                            origin, disabled_desired, stop_check=_stop_reason
                        )
                        if enabled is not None and enabled.status == "stopped":
                            stopped_early = True
                            problems.append(enabled.detail)
                            deferred.extend(desired)
                            continue
                        if enabled is not None and enabled.status == "enabled":
                            for e in disabled_desired:
                                satisfied |= _existing_keys(e)
                            re_enables.append(
                                f"{village_label(origin, names)}: re-enabled {enabled.detail}"
                            )
                        else:
                            # Couldn't re-enable — don't create a duplicate on top;
                            # report blocked so it isn't mistaken for satisfied.
                            blocked = set()
                            for e in disabled_desired:
                                blocked |= _existing_keys(e)
                            problems.append(
                                f"{village_label(origin, names)}: could not re-enable a "
                                "disabled route the plan still wants"
                            )

                    # Routes this origin claims to have created, paired with
                    # their action so the verdict can be corrected below.
                    created_here: list[tuple[RouteActionResponse, PlannedRoute]] = []
                    for i, (row, route) in enumerate(desired):
                        destination = _desired_key(route)
                        if destination in satisfied:
                            trace.decision(
                                origin=origin,
                                destination=destination,
                                decision="skipped",
                                reason="a route to this destination is already active",
                                matched_by=(
                                    "village_id" if isinstance(destination, int) else "coords"
                                ),
                            )
                            actions.append(_action(row, route, "skipped", "route already active"))
                            continue
                        if destination in blocked:
                            trace.decision(
                                origin=origin,
                                destination=destination,
                                decision="blocked",
                                reason="a disabled route exists here and re-enabling it failed",
                            )
                            actions.append(
                                _action(row, route, "blocked", "route disabled; re-enable failed")
                            )
                            outstanding += 1
                            continue
                        if attempts >= cap:
                            trace.decision(
                                origin=origin,
                                destination=destination,
                                decision="deferred",
                                reason=f"per-run cap of {cap} create(s) already spent",
                            )
                            deferred.append((row, route))
                            continue
                        # Re-check before EVERY create, not once per origin: a
                        # captcha resolved (#62) or the budget exhausted (#64)
                        # during an earlier route of THIS origin must stop the
                        # remaining same-origin writes. The service rechecks again
                        # after its pacing wait (right before the POST).
                        reason = _stop_reason()
                        if reason:
                            stopped_early = True
                            problems.append(reason)
                            deferred.extend(desired[i:])
                            break
                        attempts += 1
                        result = await svc.create_route(route, stop_check=_stop_reason)
                        if result.status == "stopped":
                            # Stopped after the pacing wait, before the POST —
                            # nothing was created; defer the remainder.
                            attempts -= 1
                            stopped_early = True
                            problems.append(result.detail)
                            deferred.extend(desired[i:])
                            break
                        if result.status == "created":
                            action = _action(row, route, "created", result.detail)
                            actions.append(action)
                            created_here.append((action, route))
                            satisfied.add(destination)
                            continue
                        outstanding += 1
                        if result.status == "skipped":
                            # Gold Club is required and missing — an account-level
                            # block. Report it as "blocked" (distinct from an
                            # "already active" skip), then stop the run and defer
                            # the rest — a human would not keep firing rejects.
                            actions.append(_action(row, route, "blocked", result.detail))
                            gold_club_blocked = True
                            deferred.extend(desired[i + 1 :])
                            break
                        actions.append(_action(row, route, "failed", result.detail))

                    # ── Did the game actually make them? ────────────────────
                    #
                    # Everything above trusted a 200 with an EMPTY body. That is
                    # not evidence of creation: the same empty 200 is what
                    # "accepted and silently did nothing" looks like. Reporting
                    # those routes as created would be a false result that also
                    # poisons the next run, which would see them missing and
                    # create them again.
                    #
                    # One request settles it, and it is the request the game's
                    # own UI makes after a create: refresh the list and look.
                    if created_here:
                        try:
                            after = await svc.confirm_routes(origin, map_span=body.map_span)
                        except (NetworkError, MarketplaceUnreadable) as exc:
                            # "I could not check" is NOT "it failed". Say exactly
                            # that, and leave the routes reported as created --
                            # they probably were -- but flag them as unverified
                            # so nobody reads this run as confirmed.
                            trace.event(
                                "verify_failed",
                                origin=origin,
                                error=str(exc),
                                unverified=len(created_here),
                            )
                            for action, _route in created_here:
                                action.status = "created_unverified"
                                action.detail = (
                                    "created, but the read-back failed so this is unconfirmed"
                                )
                            problems.append(
                                f"{village_label(origin, names)}: created "
                                f"{len(created_here)} route(s) but could not re-read the "
                                f"marketplace to confirm them ({exc}). Check this village "
                                f"before the next run."
                            )
                        else:
                            before_ids = {e.route_id for e in existing}
                            fresh = [e for e in after if e.route_id not in before_ids]
                            fresh_keys: set[int | tuple[int, int]] = set()
                            for e in fresh:
                                fresh_keys |= _existing_keys(e)
                            trace.event(
                                "verified",
                                origin=origin,
                                claimed=len(created_here),
                                new_rows_found=len(fresh),
                                new_route_ids=[e.route_id for e in fresh],
                            )
                            for action, route in created_here:
                                key = _desired_key(route)
                                if key in fresh_keys:
                                    continue
                                # The POST said yes and the page says no. Trust
                                # the page: it is the state that matters.
                                action.status = "not_created"
                                action.detail = (
                                    "the create was accepted but no matching route "
                                    "appeared on the marketplace"
                                )
                                outstanding += 1
                                problems.append(
                                    f"{village_label(origin, names)} -> "
                                    f"{action.destination_name}: the game accepted the "
                                    f"create but no route appeared. Nothing was created "
                                    f"here; do not assume otherwise."
                                )

        # Inside the try, so it beats the fallback close in `finally`. The
        # counts below are the run's actual outcome; the fallback can only say
        # that the run ended.
        trace.close(
            created=sum(1 for a in actions if a.status == "created"),
            created_unverified=sum(1 for a in actions if a.status == "created_unverified"),
            not_created=sum(1 for a in actions if a.status == "not_created"),
            created_game_rows=sum(a.game_rows for a in actions if a.status == "created"),
            disabled=len(disables),
            re_enabled=len(re_enables),
            deferred=len(deferred),
            outstanding=outstanding,
            problems=len(problems),
            stopped_early=stopped_early,
            gold_club_blocked=gold_club_blocked,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # An unexpected failure must not throw away the fact that writes already
        # committed. Issue #65 handled the read failures we anticipated
        # (NetworkError, MarketplaceUnreadable); anything else propagated as a
        # bare 500, so a run that had already created routes told the operator
        # nothing about them. Re-raise -- this IS a failure and must not be
        # dressed up as a successful run -- but say what landed and where the
        # evidence is.
        logger.exception("live trade-route execution failed unexpectedly")
        committed = sum(1 for a in actions if a.status == "created")
        trace.event(
            "run_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            created_before_failure=committed,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Execution failed unexpectedly ({type(exc).__name__}: {exc}) after "
                f"creating {committed} route(s). Nothing further was attempted. Every "
                f"write this run made is recorded in trace {trace.run_id}; use "
                f"POST /api/distribution/routes/revert-plan with that trace_id to see "
                f"exactly what is now in the game and how to undo it."
            ),
        ) from exc
    finally:
        # close() is idempotent, so this only writes when the block above did
        # not reach its own close -- i.e. the run raised. A crashed run must
        # still leave a terminated trace rather than one that simply stops.
        trace.close(ended="raised before the run could summarise itself")
        active_ops.unregister(user.id, _EXECUTE_OP_LABEL)

    actions += [_action(row, route, "deferred") for row, route in deferred]
    if gold_club_blocked:
        problems.append(
            "Gold Club is required to create trade routes; no routes were created. "
            "The remaining routes are deferred until Gold Club is active."
        )
    if stopped_early and not gold_club_blocked:
        problems.append(
            "Execution stopped early (captcha resolved, activity budget, or a read "
            "failure); remaining routes are deferred to a later run."
        )

    return ExecuteResponse(
        dry_run=False,
        live_enabled=live_enabled,
        actions=actions,
        disables=disables,
        re_enables=re_enables,
        trace_id=trace.run_id,
        trace_path=str(trace.path) if trace.path else None,
        # `remaining` = work still outstanding for a later run: routes deferred by
        # the cap PLUS any create that did not complete (failed / Gold Club), so
        # the summary never makes a partially-done run look complete.
        created=sum(1 for a in actions if a.status == "created"),
        created_game_rows=sum(a.game_rows for a in actions if a.status == "created"),
        filtered_to=filtered_to,
        remaining=len(deferred) + outstanding,
        warnings=warnings,
        problems=problems,
    )
