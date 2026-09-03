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
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from travian_api.exceptions import ActivityBudgetExhausted, NetworkError, TravianError
from travian_api.parsers.html_parser import (
    parse_village_stats_production,
    parse_village_stats_resources,
)
from travian_api.services.distribution import execution_trace
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
from travian_api.services.distribution.findings import (
    Category,
    Diagnostics,
    Finding,
    summarise,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import (
    DAILY_BEAT_CYCLES,
    EUROPE2_TEUTON,
    MerchantModel,
)
from travian_api.services.distribution.night_profile import (
    DEFAULT_BASELINE_FILL,
    DEFAULT_TARGET_FILL,
    MATERIALS,
    NightVillage,
    derive_night_profile,
)
from travian_api.services.distribution.optimizer import (
    DEFAULT_MERCHANT_HEADROOM,
    MAX_IMPROVE_PASSES,
    MAX_RELAY_HOPS,
    MIN_SEND_FILL,
    VillageState,
)
from travian_api.services.distribution.planner import (
    DistributionPlan,
    PlannerConfig,
    SheetRow,
    Verdict,
    assess,
    blockers,
    craft_plan,
)
from travian_api.services.distribution.route_revert import describe, plan_revert
from travian_api.services.distribution.run_history import (
    AccountRollup,
    RunHistory,
    RunSummary,
    summarise_runs,
)
from travian_api.services.distribution.schedule import MINUTES_PER_DAY
from travian_api.services.distribution.storage import (
    ProfileSegment,
    simulate_day,
    simulate_profile_cycle,
    storage_findings,
    store_status,
)
from travian_api.services.trade_route_service import (
    ExistingRoute,
    MarketplaceUnreadable,
    PlannedRoute,
    TradeRouteService,
    cargo_has_drifted,
)
from travian_api.stealth.timing import HumanTiming
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
# Consecutive create refusals before a run gives up. A missing Gold Club
# already stops the run outright on the reasoning that a human would not keep
# firing rejects; the same is true of any repeated refusal. The Gold Club
# per-village ROUTE LIMIT has deliberately never been probed on this account,
# so 'the game refuses this create' is an expected outcome, not an anomaly --
# and firing twenty more after the first is both wasted writes and a signal.
_CONSECUTIVE_FAILURE_LIMIT = 2

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
    stock_floor_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=0.95,
        description=(
            "Fraction of warehouse capacity this village keeps stocked by NPC "
            "trading. The planner may draw that stock down over the profile "
            "window as extra supply of lumber, clay and iron -- never crop, "
            "because a granary is not NPC-fed. None means no stock-funded supply."
        ),
    )
    consumption_per_hour: dict[Resource, float] | None = Field(
        default=None,
        description=(
            "What this village SPENDS per hour, by resource — the building queue "
            "and the troop upkeep. Materials and crop alike, entered as flat "
            "constants and kept up to date by hand. "
            "This is NOT the allocation target, and the three figures are all "
            "different: the TARGET is the rate that must be here (own production "
            "plus whatever is shipped in), the CARGO is target − own production "
            "(the gap, which a route carries and which this never changes), and "
            "CONSUMPTION is what leaves again — so the store nets "
            "target − consumption. "
            "Enter both and a village told to hold exactly what it burns reads "
            "as level, which is what it is. Enter only the target and the plan "
            "assumes the village stockpiles every unit, which is how an army "
            "village came to be reported as losing target × 24 a day at a "
            "warehouse cap it never reaches. "
            "None means no declared spend, which plans exactly as before. A "
            "wrong figure now hides a REAL overflow, so keep it current — the "
            "crop-drift check is what catches a profile that has gone stale."
        ),
    )
    ship_only_to: list[int] | None = Field(
        default=None,
        description=(
            "If set, this village may send to these OWN villages only, for EVERY "
            "resource -- crop included, so a village left off the list is not fed "
            "from here even when its granary is emptying. Foreign targets are "
            "governed separately by their own exclude_origins; a whitelist cannot "
            "stop a tribute, and the plan raises whitelist_vs_tribute when one is "
            "supplied from a restricted village. None means unrestricted."
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
    max_cycle_hours: int | None = Field(
        default=None,
        description=(
            "Longest cycle a route to this target may use, when the obligation is "
            "about CADENCE and not only volume. The optimiser satisfies a rate: "
            "47,167 crop an hour is met by 47,167 hourly and equally by 377,336 "
            "every eight hours, and it prefers the latter because it commits fewer "
            "merchants. For a store those are the same; for an ally being fed they "
            "are not. Set 1 for hourly deliveries. Cadence is bought with "
            "merchants -- an hourly cycle over a seven-hour round trip keeps seven "
            "sends in the air where an eight-hourly one keeps one -- so the cost "
            "lands in the merchant budget where it can be seen."
        ),
    )

    exclude_origins: list[int] = Field(
        default_factory=list,
        description=(
            "Village ids that must not supply this target. Needed once the target "
            "has a cadence: an hourly cycle commits one merchant per send in "
            "flight, so a supplier eight hours away spends nine merchants on that "
            "route however little it carries. The optimiser minimises merchants "
            "across the whole plan and has no way to know those nine are wanted "
            "elsewhere -- that is a judgement about the account. A denylist rather "
            "than a distance rule, because any threshold would be arbitrary."
        ),
    )

    @field_validator("max_cycle_hours")
    @classmethod
    def _cycle_is_one_travian_allows(cls, value: int | None) -> int | None:
        # Travian's repeat interval is a closed set. Accepting 5 here would plan a
        # cadence the create payload cannot express, and the route would come back
        # from the game on some other interval entirely.
        if value is not None and value not in DAILY_BEAT_CYCLES:
            raise ValueError(
                f"max_cycle_hours {value} is not a Travian repeat interval; "
                f"choose one of {sorted(DAILY_BEAT_CYCLES)}"
            )
        return value

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
    merchant_headroom: float = Field(
        default=DEFAULT_MERCHANT_HEADROOM,
        ge=0.0,
        lt=1.0,
        description=(
            "Fraction of each village's merchant budget the plan aims to leave "
            "uncommitted, so load spreads instead of piling onto whichever village "
            "is cheapest to ship from. Soft: exceeding it is reported, never fatal. "
            "0 restores the pre-headroom behaviour exactly."
        ),
    )
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
    prune_to_window: bool = Field(
        default=False,
        description=(
            "After creating a route, delete the rows that depart outside "
            "`dispatch_window`. Travian offers no setting that confines a route to "
            "part of the day, but it does not need one: repeat-every-N-hours is "
            "24/N separate rows, each with its own id and each individually "
            "deletable (measured -- a 1h route made 24 rows, deleting one left 23). "
            "So a windowed profile is enforced by subtraction. Without this the "
            "window is a fiction the game ignores and the destination receives "
            "every firing, about three times the modelled cargo for an 8-hour "
            "profile. Requires dispatch_window; does nothing without one."
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


def _window_minutes(window: tuple[int, int]) -> int:
    """How many minutes of the day a dispatch window covers.

    ``(start, end)`` are minutes past midnight and the window may wrap past it,
    so the length is the difference modulo the day. The ``or MINUTES_PER_DAY``
    reads a zero difference as the whole day rather than nothing; validation
    refuses a zero-width window ahead of this, so that arm is unreachable today
    and exists so the arithmetic cannot silently produce a division by zero if
    it ever stops being.
    """
    return (window[1] - window[0]) % MINUTES_PER_DAY or MINUTES_PER_DAY


# A stock-funded figure below this is rounding, not a dependency worth naming.
# Deliberately not allocation.EPSILON: that is a numeric tolerance (1e-6), and a
# millionth of a resource an hour is not something to warn an operator about.
#
# Named for what it is. It was `_STOCK_DRAW_FLOOR`, in a module where a "stock
# floor" is a warehouse LEVEL the operator maintains -- so the name claimed this
# bounded the draw, when all it does is decide what gets a sentence.
_MIN_REPORTED_STOCK_DRAW = 1.0


class ShortfallResponse(BaseModel):
    village_id: int
    village_name: str = ""
    resource: Resource
    per_hour: float
    reason: str


class UnallocatedResponse(BaseModel):
    resource: Resource
    total_production: float
    # Real production and stock-funded supply, kept apart so neither can be read
    # as the other: the account does not make what its warehouses merely hold.
    total_supplement: float = 0.0
    unallocated: float
    # Optional in meaning (a resource may have no remainder village) and given
    # an explicit default so the model never depends on the caller supplying it.
    remainder_village_id: int | None = None


class FindingResponse(BaseModel):
    """One planner finding, small enough to be a row in a collapsed group."""

    category: str
    severity: str
    message: str
    detail: str
    village: str
    resource: Resource | None
    loss_per_day: float


class FindingGroupResponse(BaseModel):
    """Every finding of one kind about one resource, as a single readable item."""

    key: str
    category: str
    severity: str
    resource: Resource | None
    headline: str
    action: str
    count: int
    loss_per_day: float
    findings: list[FindingResponse]


class ResourceLossResponse(BaseModel):
    resource: Resource
    per_day: float


class DiagnosticsResponse(BaseModel):
    """The finding list ranked, grouped and totalled.

    `warnings` on the parent still carries every line, in order, for anything
    that reads prose. This is the same content with the structure a person needs
    to act on it: what it costs in total, which single finding dominates that
    total, and one shared action per group instead of the same clause 45 times.
    """

    headline: str
    total_loss_per_day: float
    loss_by_resource: list[ResourceLossResponse]
    counts: dict[str, int]
    groups: list[FindingGroupResponse]


def _diagnostics_response(diagnostics: Diagnostics) -> DiagnosticsResponse:
    return DiagnosticsResponse(
        headline=diagnostics.headline,
        total_loss_per_day=diagnostics.total_loss_per_day,
        loss_by_resource=[
            ResourceLossResponse(resource=loss.resource, per_day=loss.per_day)
            for loss in diagnostics.loss_by_resource
        ],
        counts=dict(diagnostics.counts),
        groups=[
            FindingGroupResponse(
                key=group.key,
                category=group.category.value,
                severity=group.severity.value,
                resource=group.resource,
                headline=group.headline,
                action=group.action,
                count=group.count,
                loss_per_day=group.loss_per_day,
                findings=[
                    FindingResponse(
                        category=finding.category.value,
                        severity=finding.severity.value,
                        message=finding.message,
                        detail=finding.detail,
                        village=finding.village,
                        resource=finding.resource,
                        loss_per_day=finding.loss_per_day,
                    )
                    for finding in group.findings
                ],
            )
            for group in diagnostics.groups
        ],
    )


class RelayResponse(BaseModel):
    """A village the plan routes crop THROUGH, which the rows cannot show.

    The sheet lists ``V22 -> V02`` and ``V02 -> V17`` as unrelated lines, so
    without this the operator has no way to know the second row is carrying what
    the first one delivered -- or that the delivery takes both legs' waits.

    Keyed on the hub rather than on a path: the cargo is pooled in the hub's
    granary, so which origin's crop reaches which destination is not something
    the plan decided, and reporting every combination as a delivery would invent
    it (6 real hubs became 41 claimed paths on one audited account).
    """

    hub: int
    hub_name: str
    # Ids and names in parallel, as every other row here does it: the ids join
    # this to the sheet's rows, the names are what the operator reads.
    origins: list[int]
    origin_names: list[str]
    destinations: list[int]
    destination_names: list[str]
    collect_hours: float
    forward_hours: float
    end_to_end_hours: float = Field(
        description=(
            "What the operator waits for a delivery: both legs' worst cases in "
            "turn. The per-route latency target is checked per leg, so two "
            "compliant legs can compose into a delivery that misses it."
        )
    )


class VerdictResponse(BaseModel):
    """``feasible``, with what it weighed and what it did not.

    ``feasible`` alone was rendered as a green badge, so a plan losing 2.4M
    resources a day looked approved. ``clean`` is the question that badge was
    actually being asked.
    """

    executable: bool
    clean: bool
    blockers: list[str]
    covers: list[str]
    unweighed: list[str]
    critical_findings: int


def _verdict_response(verdict: Verdict) -> VerdictResponse:
    return VerdictResponse(
        executable=verdict.executable,
        clean=verdict.clean,
        blockers=list(verdict.blockers),
        covers=list(verdict.covers),
        unweighed=[category.value for category in verdict.unweighed],
        critical_findings=verdict.critical_findings,
    )


class PlanResponse(BaseModel):
    rows: list[SheetRowResponse]
    budgets: list[BudgetResponse]
    shortfalls: list[ShortfallResponse]
    unallocated: list[UnallocatedResponse]
    total_merchants: int
    feasible: bool = Field(
        description=(
            "Whether the sheet can be carried out. Kept as the field every caller "
            "reads, and still what /execute gates on; see `verdict` for what that "
            "word does and does not cover."
        )
    )
    verdict: VerdictResponse
    relays: list[RelayResponse]
    # Every finding as prose, in producer order. Kept because it is the contract
    # the UI and the tests were built on -- but a 25-village account put 132
    # lines in here and the operator stopped reading, so `diagnostics` is what
    # the page actually renders.
    warnings: list[str]
    diagnostics: DiagnosticsResponse


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
    # Whole-day execution: every profile at once. Each segment is planned in its
    # own hours (same shape /day-check uses -- one model, so the two cannot
    # drift), the desired routes are the UNION across segments, and each route
    # is trimmed and reconciled against its own profile window. This is what
    # lets Day and Night rows coexist in the game -- disjoint by departure
    # minute -- so the account runs the whole day with no profile switching.
    segments: list[DaySegmentInput] = Field(
        default=[],
        max_length=MAX_DAY_SEGMENTS,
        description=(
            "One entry per allocation profile. Empty means single-profile "
            "execution using the top-level allocations/dispatch_window."
        ),
    )

    @model_validator(mode="after")
    def _segments_are_coherent(self) -> "ExecuteRequest":
        if not self.segments:
            return self
        # The union depends on disjoint row minutes: without the prune, both
        # profiles' fan-outs cover the whole day and per-route attribution --
        # trim, drift, reconciliation -- has no way to tell whose row is whose.
        if not self.prune_to_window:
            raise ValueError(
                "segments require prune_to_window: without the prune both "
                "profiles' rows cover the whole day and cannot be told apart"
            )
        if self.dispatch_window is not None:
            raise ValueError(
                "dispatch_window belongs to each entry in `segments`, not at "
                "the top level -- every profile runs its own hours"
            )
        if self.allocations:
            raise ValueError(
                "allocations belong to each entry in `segments` for a "
                "whole-day run -- a top-level set would silently be ignored"
            )
        # Two profiles cannot run at the same time -- an overlap would create
        # rows this run itself then classifies as someone else's mismatch.
        covered: set[int] = set()
        for segment in self.segments:
            start, end = segment.window
            span = set(
                range(start, end) if start < end else [*range(start, MINUTES_PER_DAY), *range(end)]
            )
            clash = covered & span
            if clash:
                raise ValueError(
                    f"profile windows overlap around minute {min(clash)}: two "
                    f"profiles cannot run at the same time"
                )
            covered |= span
        return self

    only_origins: list[int] | None = Field(
        default=None,
        description="Run only routes leaving these origin village ids.",
    )
    only_destinations: list[int] | None = Field(
        default=None,
        description="Run only routes arriving at these destination village ids.",
    )
    # Correcting a live route's cargo is a WRITE against a route the operator may
    # have tuned by hand, so it is opt-in rather than a silent side effect of
    # every run. Without it a route keeps the cargo it was created with forever
    # while the plan moves on, and nothing detects the divergence.
    update_drifted: bool = Field(
        default=False,
        description=(
            "Rewrite the cargo of live routes whose amounts have drifted from the "
            "plan. Off by default: a route created earlier may have been adjusted "
            "in-game deliberately, and this overwrites that."
        ),
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
        ge=0,
        le=50,
        description="Routes to CREATE in one run. A human sets up a few at a "
        "time over days, not in one sweep; the rest come back as `remaining` "
        "for a later run. **0 means reconcile only**: read, disable what the "
        "plan no longer wants, and create nothing — the safe first half of a "
        "profile switch.",
    )
    reconcile_all_origins: bool = Field(
        default=False,
        description=(
            "Sweep EVERY village in the snapshot, not only the origins this "
            "plan still uses. Off by default because re-reading every "
            "marketplace on every run is real traffic for nothing: in steady "
            "state there is no staleness to find. Turn it on when the PLAN "
            "changed — switching between a day and a night profile drops some "
            "villages as origins entirely, and those are precisely the ones "
            "holding stale routes and precisely the ones a plan-origin sweep "
            "never visits. A distribution plan is a conservation system, so one "
            "surviving route makes the receiver overflow AND the sender drain: "
            "the account ends up in neither plan."
        ),
    )
    protect_destinations: list[str] = Field(
        default_factory=list,
        description=(
            "Destinations whose live routes are never disabled, however the plan "
            'sees them. Each entry is a village id ("53629") or coordinates '
            '("46|133") — coordinates because a hand-made route to a foreign '
            "target has no usable village id, so an id-only list could not "
            "protect one at all. Exists because the reconciler's rule (active, "
            "identifiable, not wanted by the plan => stale) is right for routes a "
            "previous plan created and wrong for one made by hand: without this, "
            "the app switches such a route off, the operator switches it on, and "
            "the next run switches it off again. Narrows only what is DISABLED, "
            "never what is created."
        ),
    )

    @field_validator("protect_destinations")
    @classmethod
    def _protected_entries_are_parseable(cls, value: list[str]) -> list[str]:
        # Rejected, not dropped. A typo ("4688" for "46|88") that is silently
        # ignored leaves the operator believing a route is protected when it is
        # not, and the very next run switches it off.
        for entry in value:
            text = entry.strip()
            if text.isdigit() and int(text) > 0:
                continue
            if "|" in text:
                left, _, right = text.partition("|")
                if left.strip().lstrip("-").isdigit() and right.strip().lstrip("-").isdigit():
                    continue
            raise ValueError(
                f"protect_destinations entry {entry!r} is neither a village id "
                f'nor coordinates like "46|133"'
            )
        return value

    max_game_rows_per_run: int = Field(
        default=0,
        ge=0,
        le=2000,
        description=(
            "Route ROWS this run may put in the game. 0 is unbounded. This is the "
            "unit the operator actually authorises: Travian turns one 'repeat "
            "every N hours' request into 24/N separate daily rows, so three "
            "routes on a one-hour cycle is seventy-two rows, and removing them "
            "later means deleting every one. The run already reported this "
            "number; nothing bounded it, so what was agreed to and what was "
            "written were different units. A route cannot be created partly, so "
            "one that does not fit is deferred whole."
        ),
    )
    max_origins_per_run: int = Field(
        default=0,
        ge=0,
        le=200,
        description=(
            "Villages to VISIT in one call, when reconciling all origins. 0 is "
            "unbounded. A full sweep cannot fit in one HTTP call: fifty paced "
            "reads already run past a two-minute client timeout before any write "
            "delay, idle browse or session break — and those are what make the "
            "traffic look human. So the caller chunks it and loops while "
            "`unswept_origins` is non-empty, which also puts real gaps between "
            "sessions instead of one long burst."
        ),
    )


class RouteActionResponse(BaseModel):
    origin: int
    origin_name: str
    # Which profile this route belongs to in a whole-day run ("Day"/"Night");
    # empty on single-profile runs. A label for the operator, never a key.
    segment: str = ""

    destination: int
    destination_name: str
    dest_x: int
    dest_y: int
    cargo: dict[Resource, int]
    cycle_hours: int
    merchants: int
    # How many rows this ONE create request is EXPECTED to become in the game.
    # Travian fans a "repeat every N hours" route out into 24/N daily rows, so a
    # request is not a row -- and an operator who authorised "3 routes" on a
    # 1-hour cycle has authorised 72 rows. Reported so that is never a surprise.
    # This is arithmetic, not observation: see observed_game_rows.
    game_rows: int = 1
    # Rows the marketplace read-back actually attributed to THIS action, matched
    # to it by destination the same way the reconciler matches routes. `None`
    # means no measurement exists and none is invented: a dry run observes
    # nothing, and a create whose read-back failed is unconfirmed rather than
    # counted. A `not_created` action carries 0, which IS a measurement.
    observed_game_rows: int | None = None
    # would_create | deferred | created | created_unverified | not_created |
    # re_enabled | updated | skipped | blocked | failed
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
    # Kept apart from disables and re_enables: an update changes what a running
    # route ships without starting or stopping anything, which is a third thing
    # and reads wrongly folded into either.
    updates: list[str] = []
    created: int
    # What a live run of this request would spend against Travian, by kind.
    # Filled on DRY RUNS only -- a live run reports what actually happened
    # instead. Estimates: reads/creates/verifies/trims are arithmetic over the
    # plan, while disables depend on what each marketplace turns out to hold
    # (bounded by one batched PUT per visited origin, counted in the _max).
    requests_forecast: dict[str, int] = {}
    # Creates the game accepted but this run could NOT confirm, and creates it
    # accepted while producing nothing. `created` counts only the VERIFIED ones,
    # so without these two a run whose every read-back failed reported
    # "Created 0 route(s)" as its headline above a problem list saying three
    # routes had just been written -- the summary contradicting the detail.
    created_unverified: int = 0
    not_created: int = 0
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
    # Only populated by a reconcile_all_origins run, and deliberately empty
    # otherwise: an ordinary run visits only the plan's own origins and must not
    # report lists that could be mistaken for an account-wide sweep.
    #
    # `unswept_origins` is the one that carries a promise. A sweep is a guarantee
    # only when it is COMPLETE -- a single unvisited village can still hold a
    # route the plan rejected, and one such route breaks the conservation the
    # whole plan rests on. So a bounded sweep names what it did not reach, and
    # the caller loops until this is empty. Reading a short sweep as a finished
    # one is exactly the false confidence this reports away.
    swept_origins: list[int] = []
    unswept_origins: list[int] = []
    # How long the caller should wait before requesting the next chunk, set only
    # while a sweep is unfinished. The pause between chunks IS the session break
    # a long reconciliation needs -- taking it inside the handler would mean
    # sleeping minutes into an HTTP call no client waits out. Drawn randomly per
    # response because a client returning on a fixed interval is its own
    # signature, however long that interval is.
    next_chunk_wait_seconds: float | None = None
    # Rows in the game as a result of this run's creates, which is not the same
    # number as the creates: see RouteActionResponse.game_rows.
    #
    # On a DRY RUN this is the forecast -- 24/cycle per request -- because
    # nothing has happened yet and a prediction is the only thing available.
    # On a LIVE RUN it is MEASURED: the rows the marketplace read-back
    # attributed to each verified create. Reporting the forecast for a live run
    # printed "put 24 route row(s) in the game" for a create the game had turned
    # into one row, which is a claim about the account nobody had checked.
    # Unconfirmed creates contribute nothing here; they are reported as
    # created_unverified instead of having a row count guessed for them.
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


def _declared_consumption(config: Sequence[VillageConfig]) -> dict[int, dict[Resource, float]]:
    """What each village spends per hour, village-major. Operator-declared.

    One reader for two shapes: the storage replays want it per village (their
    stores are keyed that way) and the allocation layer wants it per resource
    beside ``supplements``. Built from the same config either way, so the two
    cannot disagree about what the operator typed.
    """
    out: dict[int, dict[Resource, float]] = {}
    for cfg in config:
        if not cfg.consumption_per_hour:
            continue
        out[cfg.village_id] = {
            resource: float(amount) for resource, amount in cfg.consumption_per_hour.items()
        }
    return out


def _storage_findings(
    body: PlanRequest,
    plan,
    dispatch_window: tuple[int, int] | None = None,
) -> list[Finding]:
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
    # What each village spends, which is neither production nor cargo. Both
    # checks need it and for the same reason: without it a village's allocation
    # target reads as permanent accumulation, so an army village told to land
    # what it burns is reported as filling up and then as losing target x 24 a
    # day at a cap it never actually reaches.
    consumption = _declared_consumption(body.config)

    # Net rate per village per resource AFTER the plan: own production plus what
    # arrives minus what leaves minus what is spent. That is what the store
    # actually sees.
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
            net = (
                float(own)
                + shipped.get(vid, {}).get(resource, 0.0)
                - consumption.get(vid, {}).get(resource, 0.0)
            )
            stocks.setdefault(vid, {})[resource] = stock
            own_rates.setdefault(vid, {})[resource] = float(own)
            if cap is not None:
                capacities.setdefault(vid, {})[resource] = cap
            statuses.append(store_status(vid, resource, stock, cap, net))

    # When the run will prune the out-of-window rows, simulating them reports
    # traffic that will never move: an hourly 8h night profile keeps 8 rows,
    # and simulating all 24 tripled the reported flows -- false overflow,
    # starvation and loss totals. Without pruning every firing is real (that is
    # the round-the-clock danger the WINDOW findings report), so all are kept.
    window = dispatch_window if getattr(body, "prune_to_window", False) else None
    # A village the operator NPCs back to a floor never runs its warehouse down,
    # so its departures are funded in full. Modelling it as an ordinary store
    # made every stock-funded route ship a fraction of its cargo and understated
    # the receivers' whole day. Materials only -- a granary is not NPC-fed.
    floors: dict[int, dict[Resource, float]] = {}
    for cfg in body.config:
        if cfg.stock_floor_fraction is None:
            continue
        capacity = next(
            (v.warehouse_capacity for v in body.snapshot if v.village_id == cfg.village_id), None
        )
        if capacity is None:
            continue  # already refused in _plan_account, which runs first
        level = cfg.stock_floor_fraction * capacity
        for resource in MATERIALS:
            floors.setdefault(cfg.village_id, {})[resource] = level
    overflows = simulate_day(
        plan.beat,
        stocks,
        capacities,
        own_rates,
        dispatch_window=window,
        floors=floors,
        consumption=consumption,
    )
    names = {v.village_id: v.name for v in body.snapshot if v.name}
    return list(storage_findings(statuses, overflows, names=names))


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


class NightProfileRequest(PlanRequest):
    """Derive a night profile from the account's own shape. Zero game requests.

    Almost everything the derivation needs is already here or inferable, and
    asking for it again would be asking the operator to restate their own account.
    The two things it cannot know are how empty they actually get the stores before
    sleeping, and how full they are willing to wake up to.

    `allocations` is read as the DAY profile: the army villages' shares come from
    what the day already gives them, so the night is the day's plan bounded by the
    stores rather than a second set of numbers to keep in step.
    """

    baseline_fill: float = Field(
        default=DEFAULT_BASELINE_FILL,
        ge=0.0,
        le=0.95,
        description=(
            "How full each store is when the operator goes to bed, as a fraction. "
            "The one number the account cannot supply, and the one everything else "
            "rests on: measured from the baseline the operator RE-ESTABLISHES each "
            "night, a profile holds for weeks, while one measured from whatever a "
            "snapshot caught goes stale within the hour."
        ),
    )
    target_fill: float = Field(
        default=DEFAULT_TARGET_FILL,
        gt=0.0,
        le=1.0,
        description="How full a store may be at dawn, as a fraction.",
    )

    @field_validator("target_fill")
    @classmethod
    def _target_is_above_baseline(cls, value: float, info: ValidationInfo) -> float:
        baseline = (info.data or {}).get("baseline_fill", DEFAULT_BASELINE_FILL)
        if value <= baseline:
            raise ValueError(
                f"target_fill {value} is not above baseline_fill {baseline}; there "
                f"would be no room for anything to arrive in"
            )
        return value


class NightProfileResponse(BaseModel):
    allocations: dict[Resource, dict[int, AllocationInput]]
    # What was inferred rather than asked for, so the operator can see the
    # reasoning instead of trusting it. A derivation whose inputs are invisible is
    # one nobody can check.
    hub: int | None = None
    hub_name: str = ""
    consumers: list[str] = []
    window_hours: float = 0.0
    tribute_per_hour: float = 0.0
    forced_senders: dict[Resource, list[str]] = {}
    drawn_in: dict[Resource, list[str]] = {}
    unmet: dict[Resource, float] = {}
    warnings: list[str] = []


@router.post("/night-profile", response_model=NightProfileResponse)
async def post_night_profile(
    body: NightProfileRequest,
    _user: User = Depends(get_current_user),
):
    """Build a night profile from stores and production. Costs **zero** requests.

    Pure arithmetic over the snapshot the caller already has, so it can be redone
    as often as the operator likes while they settle on a baseline.
    """
    # Unfiltered, unlike the two `if v.name` builders in this module: this one
    # feeds the derived profile's own village labels, where a nameless village
    # must still appear as a row rather than be dropped from the sheet.
    # `village_label` handles the empty string the same way it handles a missing
    # id. Bound once -- it was rebuilt identically 138 lines further down.
    names = {v.village_id: v.name for v in body.snapshot}
    if not body.snapshot:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No villages in the snapshot; fetch account state first.",
        )
    trade_office = {c.village_id: c.trade_office_level for c in body.config}

    # An unreadable crop balance must stop the derivation, not pass as zero.
    # `crop_per_hour or 0.0` made a village whose balance could not be read look
    # like a healthy zero-crop village: it got no break-even allocation and no
    # warning, and if its true balance is negative its granary drains all night
    # while the derived profile claims it needs nothing. Starvation eats troops,
    # which cannot be re-grown from a warehouse -- so this is refused outright
    # rather than warned about, naming every village to fix.
    unreadable_crop = sorted(
        village_label(v.village_id, names) for v in body.snapshot if v.crop_per_hour is None
    )
    if unreadable_crop:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "The crop balance of "
                + ", ".join(unreadable_crop)
                + " could not be read, and a night profile built on a guessed crop "
                "balance can starve the village it guesses wrong about. Fetch "
                "fresh state (the crop balance needs the village's statistics "
                "page), then derive again."
            ),
        )

    # A store whose capacity could not be read cannot be given a ceiling: every
    # night ceiling is a fraction OF the capacity. Reaching the derivation as
    # None it raised TypeError and killed the whole request; guessing a capacity
    # would be worse, because the guess decides how much crop is shipped into
    # that store overnight. Refused by name, exactly as an unreadable crop
    # balance is.
    unreadable_capacity = sorted(
        village_label(v.village_id, names)
        for v in body.snapshot
        if v.granary_capacity is None or v.warehouse_capacity is None
    )
    if unreadable_capacity:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "The store capacity of "
                + ", ".join(unreadable_capacity)
                + " could not be read, and every night ceiling is a fraction of "
                "the capacity. Fetch fresh state so the warehouse and granary "
                "sizes are known, then derive again."
            ),
        )

    villages = [
        NightVillage(
            village_id=v.village_id,
            name=v.name,
            x=v.x,
            y=v.y,
            merchants_total=v.merchants_total,
            trade_office_level=trade_office.get(v.village_id, 0),
            warehouse_capacity=v.warehouse_capacity,
            granary_capacity=v.granary_capacity,
            production={
                Resource.LUMBER: v.lumber_per_hour or 0.0,
                Resource.CLAY: v.clay_per_hour or 0.0,
                Resource.IRON: v.iron_per_hour or 0.0,
                Resource.CROP: v.crop_per_hour or 0.0,
            },
        )
        for v in body.snapshot
    ]

    warnings: list[str] = []

    # The hub is the village the DAY profile already sends its surplus to. Asking
    # for it again would be asking the operator to restate a decision the
    # allocations already record.
    hub = None
    for resource in MATERIALS:
        for vid, alloc in (body.allocations.get(resource) or {}).items():
            if alloc.mode is AllocationMode.REMAINDER:
                hub = vid
                break
        if hub is not None:
            break
    if hub is None:
        # Fall back to the largest producer, and say so: a silently chosen hub
        # would move every material route without the operator knowing why.
        hub = max(
            villages,
            key=lambda v: sum(v.production[r] for r in MATERIALS),
        ).village_id
        warnings.append(
            f"No remainder village is set for materials, so "
            f"{village_label(hub, names)} was used as the hub because it produces "
            f"the most. Set a remainder village to choose deliberately."
        )

    consumers = [v.village_id for v in villages if v.production[Resource.CROP] < 0]

    tribute = 0.0
    tribute_at: tuple[int, int] | None = None
    for target in body.foreign_targets:
        if target.route_eligible:
            tribute += target.crop_per_hour
            if tribute_at is None:
                tribute_at = (target.x, target.y)

    if body.dispatch_window is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This profile has no hours. A night profile is derived FROM its "
                "window -- the ceiling is the room a store has divided by the hours "
                "it has to fill -- so give the profile a window first."
            ),
        )
    window_minutes = _window_minutes(body.dispatch_window)
    window_hours = window_minutes / 60.0

    # Every explicit mode is resolved to the absolute rate it means, with the
    # same arithmetic the UI's remainder figure uses. Copying only ABSOLUTE
    # values silently discarded percentage and sustain targets, so two Day
    # profiles that mean the same thing -- "keep 50%" and its absolute
    # equivalent -- derived different nights. KEEP and REMAINDER stay absent:
    # keep is the default the derivation already assumes, and the remainder is
    # the hub, chosen separately above.
    rate_fields = {
        Resource.LUMBER: "lumber_per_hour",
        Resource.CLAY: "clay_per_hour",
        Resource.IRON: "iron_per_hour",
        Resource.CROP: "crop_per_hour",
    }
    day_retention: dict[Resource, dict[int, float]] = {}
    for resource, per in body.allocations.items():
        own_rates = {
            v.village_id: rate
            for v in body.snapshot
            if (rate := getattr(v, rate_fields[resource])) is not None
        }
        account_total = sum(own_rates.values())
        resolved: dict[int, float] = {}
        for vid, alloc in per.items():
            if alloc.mode is AllocationMode.ABSOLUTE:
                # The plan endpoints refuse this through Allocation's own guard.
                # This path reads the value raw, so it must refuse it itself:
                # let through, a negative retention derives nonsense or dies
                # inside the derivation as a 500.
                if alloc.value < 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"{village_label(vid, names)}: {resource.value} day retention "
                            f"of {alloc.value:g}/h is negative -- a village cannot keep "
                            f"less than nothing"
                        ),
                    )
                resolved[vid] = alloc.value
            elif alloc.mode is AllocationMode.PERCENTAGE:
                resolved[vid] = account_total * alloc.value / 100.0
            # SUSTAIN is deliberately not resolved. It only means something on a
            # crop consumer, and the derivation never reads crop retention: its
            # own crop design makes every consumer break even for the night,
            # which supersedes whatever share the day plan sustained. Resolving
            # it here would be dead code wearing the costume of support --
            # proven by a test that passed identically with the branch deleted.
        day_retention[resource] = resolved

    profile = derive_night_profile(
        villages,
        window_hours=window_hours,
        speed_fields_per_hour=body.speed_fields_per_hour,
        map_span=body.map_span,
        day_retention=day_retention,
        hub_id=hub,
        consumer_ids=consumers,
        tribute_per_hour=tribute,
        tribute_at=tribute_at,
        baseline_fill=body.baseline_fill,
        target_fill=body.target_fill,
        merchant_base_capacity=body.merchant_base_capacity,
        trade_office_bonus_per_level=body.trade_office_bonus_per_level,
        merchant_reserve=body.merchant_reserve,
    )

    # The typed baseline against the stores actually fetched. The derivation
    # still obeys the typed number exactly -- this is a fact, not a correction,
    # because which baseline to plan for is the operator's call and re-
    # establishing it each night is what makes a profile last. The band is wide
    # (20 percentage points) on purpose: stores drift constantly, and a check
    # that fires every few percent is noise nobody reads on the day it matters.
    _BAND = 0.20
    for v in body.snapshot:
        for _resource, _stock, _cap in (
            (Resource.CROP, v.crop_stock, v.granary_capacity),
            (Resource.LUMBER, v.lumber_stock, v.warehouse_capacity),
        ):
            if not _cap or _stock is None:
                continue
            _fill = _stock / _cap
            if _fill > body.baseline_fill + _BAND:
                _room = "past the target too" if _fill >= body.target_fill else "already"
                warnings.append(
                    f"{village_label(v.village_id, names)}: {_resource.value} is "
                    f"{_fill:.0%} full, {_room} much fuller than the "
                    f"{body.baseline_fill:.0%} baseline this profile assumes — the "
                    f"room reserved to fill it will be shipped into a store that "
                    f"cannot hold it. Re-check the baseline, or drain it first."
                )
            elif _fill < body.baseline_fill - _BAND:
                warnings.append(
                    f"{village_label(v.village_id, names)}: {_resource.value} is "
                    f"{_fill:.0%} full, much emptier than the "
                    f"{body.baseline_fill:.0%} baseline this profile assumes — the "
                    f"night is sized to reach {body.target_fill:.0%} from there, so "
                    f"it will fall short of the target rather than overfill."
                )

    for resource, short in profile.unmet.items():
        if short > 1.0:
            warnings.append(
                f"{short:,.0f} {resource.value}/h of demand no village could "
                f"cover. The plan will report it as a shortfall rather than "
                f"quietly leaving a receiver unfed."
            )
    if profile.residual_trimmed:
        # Not "integer rounding": the canonical fixture trims 10,001/h, which no
        # rounding of whole-number retentions can account for. Rounding is a unit
        # or two; anything larger is a village losing crop or a forced sender
        # shedding past what is needed, and saying "rounding" told the operator to
        # ignore a five-figure figure.
        warnings.append(
            f"Trimmed {profile.residual_trimmed:,.0f} crop/h off the largest "
            f"share so the allocation cannot claim more than the account "
            f"produces. A unit or two is each retention rounded to a whole "
            f"number; more than that is a real gap — a village losing crop that "
            f"nothing named a consumer, or a forced sender shedding past what "
            f"its neighbours need."
        )

    return NightProfileResponse(
        allocations={
            resource: {
                vid: AllocationInput(mode=alloc.mode, value=alloc.value)
                for vid, alloc in per.items()
            }
            for resource, per in profile.allocations.items()
        },
        hub=hub,
        hub_name=village_label(hub, names),
        consumers=[village_label(vid, names) for vid in consumers],
        window_hours=window_hours,
        tribute_per_hour=tribute,
        forced_senders={
            r: [village_label(v, names) for v in ids] for r, ids in profile.forced_senders.items()
        },
        drawn_in={r: [village_label(v, names) for v in ids] for r, ids in profile.drawn_in.items()},
        unmet={r: v for r, v in profile.unmet.items() if v > 1.0},
        warnings=warnings,
    )


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
        # Every finding, both halves: a profile needs the allocation-level ones
        # (unallocated slack, a receiver nothing sources) that live on the plan
        # as much as the ones computed here.
        for warning in account.warnings:
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
    # Consumption goes in as a keyword: `step_minutes` is the next positional,
    # and a spend landing there would silently retime the whole replay. The
    # composite needs it for the same reason /plan does, and needs the SAME
    # figure -- one simulation given a parameter and not the other is how the
    # two endpoints came to answer one account differently before.
    trajectories, breaches = await asyncio.to_thread(
        simulate_profile_cycle,
        segments,
        own_rates,
        stocks,
        capacities,
        body.crop_ceilings,
        consumption=_declared_consumption(body.config),
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
    extra_findings: list[Finding]
    """Findings computed HERE rather than by craft_plan: overflow, starvation,
    busy merchants, unfunded tributes. Named for what it is, because a bare
    `findings` reads like the whole list and is not."""

    dropped_allocations: list[str] = field(default_factory=list)
    """Human-readable descriptions of explicit allocations that were IGNORED
    because the village's rate could not be read. A dry run or /plan shows them
    as CRITICAL findings; a live run refuses on them outright, because executing
    silently without an allocation the operator explicitly wrote is executing a
    different plan than the one they approved."""

    @property
    def all_findings(self) -> list[Finding]:
        """Every finding, plan and endpoint alike, in producer order.

        The one list to pass anywhere. Three call sites used to concatenate the
        halves by hand, and `assess` reports a destructive plan as clean if it is
        handed only one of them -- so the correct list is the easy one to reach.
        """
        return [*self.plan.findings, *self.extra_findings]

    @property
    def warnings(self) -> list[str]:
        """The findings as the flat prose list every caller has always read."""
        return [f.message for f in self.all_findings]

    def verdict(self) -> Verdict:
        """What feasibility decided, and what it left to the operator."""
        return assess(self.plan, self.all_findings, self.names)


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
            # Carried so relay can refuse a hub that is losing crop. Passed
            # through as-is, None included: an unreadable rate must not be
            # rounded to a safe-looking zero.
            crop_per_hour=v.crop_per_hour,
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
    cadence_caps: dict[int, int] = {}
    excluded_origins: dict[int, set[int]] = {}
    manual_targets: list[ForeignTarget] = []
    for index, target in enumerate(body.foreign_targets):
        if not target.route_eligible:
            manual_targets.append(target)
            continue
        target_id = -(index + 1)
        foreign_ids[target_id] = target
        if target.exclude_origins:
            excluded_origins[target_id] = set(target.exclude_origins)
        if target.max_cycle_hours is not None:
            # Keyed by the SYNTHETIC id, because that is what the optimizer knows
            # this destination as -- a foreign target has no village id of its own.
            cadence_caps[target_id] = target.max_cycle_hours
        names[target_id] = target.name
        villages[target_id] = VillageState(
            village_id=target_id,
            x=target.x,
            y=target.y,
            merchant_count=0,
            trade_office_level=0,
            name=target.name,
        )

    # A village's whitelist is the same exclusion seen from the origin: it bans
    # that origin from every OWN destination it does not name, so the optimizer
    # sees one denylist whichever side the operator wrote it from. Foreign
    # targets are left alone -- they carry their own exclude_origins, and a
    # whitelist that also starved every tribute would be a second lever nobody
    # asked for; `ship_only_to` takes own village ids and a tribute has none, so
    # binding it would leave "restricted, but keeps paying the tribute"
    # impossible to say. The plan reports WHITELIST_VS_TRIBUTE when one is
    # actually supplied that way, so the exemption is visible on the plan that
    # used it rather than only in a tooltip.
    #
    # Every resource, crop included. Exempting crop was considered -- crop
    # starvation kills troops where a material shortfall only slows building --
    # and rejected: the operator's own spec whitelists the ARMY village, which
    # says they expect the list to bind crop, and an exemption would silently
    # overrule a declared restriction with nothing anywhere to explain the
    # extra route. A starved receiver surfaces as a shortfall naming the
    # whitelist as the cause instead (see `_flows_for_resource`).
    #
    # A village naming itself is harmless and ignored.
    own_ids = {v.village_id for v in body.snapshot}
    for entry in body.config:
        if entry.ship_only_to is None:
            continue
        unknown = sorted(set(entry.ship_only_to) - own_ids)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"{village_label(entry.village_id, names)}: ship_only_to names "
                    + ", ".join(f"village {vid}" for vid in unknown)
                    + ", which the snapshot does not contain. Fix the list, or fetch "
                    "fresh state if the village was settled after the snapshot."
                ),
            )
        for destination in own_ids - set(entry.ship_only_to) - {entry.village_id}:
            excluded_origins.setdefault(destination, set()).add(entry.village_id)

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

    # The window this plan will actually be pruned to, resolved once so the
    # cycle choice below and the scheduler agree on it.
    effective_window = dispatch_window if dispatch_window is not None else body.dispatch_window

    # A stock FLOOR is a level; the allocation layer needs a RATE. Spreading the
    # floor across the window the profile actually runs is the honest
    # conversion: 30% of a 1,200,000 warehouse is 360,000, which over a 16-hour
    # day is 22,500/h of extra supply and over an 8-hour night is 45,000/h.
    # Materials only -- a granary is not NPC-fed, so crop is never supplemented.
    if effective_window is None:
        window_hours = 24.0
    else:
        window_hours = _window_minutes(effective_window) / 60.0
    capacities_by_id = {v.village_id: v.warehouse_capacity for v in body.snapshot}
    supplements: dict[Resource, dict[int, float]] = {}
    for cfg in body.config:
        if cfg.stock_floor_fraction is None:
            continue
        capacity = capacities_by_id.get(cfg.village_id)
        if capacity is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"{village_label(cfg.village_id, names)}: a stock floor of "
                    f"{cfg.stock_floor_fraction:.0%} was set but this village has no "
                    f"warehouse capacity in the snapshot, so the floor cannot be "
                    f"turned into a rate. Fetch capacities first, or clear the floor."
                ),
            )
        allowance = cfg.stock_floor_fraction * capacity / window_hours
        for resource in MATERIALS:
            if cfg.village_id in productions.get(resource, {}):
                supplements.setdefault(resource, {})[cfg.village_id] = allowance

    # Section 2: what each village spends per hour, the operator's own flat
    # constants. Threaded beside the supplements and for the same reason -- both
    # are account state the game will not report, not tunables -- and shaped
    # per resource to match. It moves each village's net and nothing else: the
    # cargo stays the gap between the target and the village's own production.
    #
    # Refused for a village the snapshot does not contain, the same way
    # ship_only_to is: a figure attached to an id that is not being planned is a
    # typo or a chiefed village, and either way the operator's declared spend is
    # not reaching the plan they are reading.
    declared_consumption = _declared_consumption(body.config)
    unknown_consumers = sorted(set(declared_consumption) - own_ids)
    if unknown_consumers:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "consumption_per_hour names "
                + ", ".join(f"village {vid}" for vid in unknown_consumers)
                + ", which the snapshot does not contain. Fix the figure, or fetch "
                "fresh state if the village was settled after the snapshot."
            ),
        )
    consumption: dict[Resource, dict[int, float]] = {}
    for vid, per_resource in declared_consumption.items():
        for resource, amount in per_resource.items():
            # Same gate the supplement uses: a village whose rate for this
            # resource could not be read is dropped from the resource plan
            # entirely (with an UNREADABLE_RATE finding), so a spend recorded
            # against it would 400 the whole plan over one missing reading.
            if vid in productions.get(resource, {}):
                consumption.setdefault(resource, {})[vid] = amount

    # A windowed, pruned profile may only use cycles that divide the window.
    #
    # Travian fans "repeat every N hours" into 24/N daily rows and pruning keeps
    # the in-window ones, so a route delivers batch x survivors per day -- and
    # the batch is sized as rate x cycle. Those only multiply back to
    # rate x window_hours when the cycle divides the window: a 6h cycle in an
    # 8h window keeps 2 of its 4 firings and ships rate x 12 where the profile
    # meant rate x 8, a 50% over-delivery paired with six hours of extra
    # withdrawal at the origin. Equally spaced firings land exactly
    # window/cycle inside ANY window whose length is a multiple of the spacing,
    # whatever the phase, so divisor cycles deliver the modelled amount by
    # construction. A window no candidate divides (odd minute lengths) falls
    # back to the full set -- the WINDOW_PRUNED finding then reports the real
    # ratio rather than the plan silently lying about it.
    allowed_cycles: list[int] | tuple[int, ...] = tuple(DAILY_BEAT_CYCLES)
    if effective_window is not None and getattr(body, "prune_to_window", False):
        window_minutes = _window_minutes(effective_window)
        dividing = [c for c in DAILY_BEAT_CYCLES if window_minutes % (c * 60) == 0]
        if dividing:
            allowed_cycles = dividing

    config = PlannerConfig(
        geometry=MapGeometry(span=body.map_span, speed_fields_per_hour=body.speed_fields_per_hour),
        merchant_model=MerchantModel(
            base_capacity=body.merchant_base_capacity,
            bonus_per_trade_office_level=body.trade_office_bonus_per_level,
        ),
        merchant_reserve=body.merchant_reserve,
        merchant_headroom=body.merchant_headroom,
        cycles=allowed_cycles,
        max_latency_hours=body.max_latency_hours,
        min_arrival_gap_minutes=body.min_arrival_gap_minutes,
        reserved_window=body.reserved_window,
        # The explicit argument wins (the day check passes each segment's own
        # hours); otherwise take what the client sent on the request, which is
        # how /plan and /execute learn the active profile's window.
        dispatch_window=effective_window,
        # Plan-time, because it changes what the plan MEANS: with pruning the
        # window is genuinely enforced and the escaping firings become a note
        # about a dependency, without it they are a critical over-delivery.
        prune_to_window=body.prune_to_window,
        max_cycle_by_destination=cadence_caps,
        excluded_origins_by_destination=excluded_origins,
        min_send_fill=body.min_send_fill,
        max_improve_passes=body.max_improve_passes,
        max_relay_hops=body.max_relay_hops,
    )

    extra_findings: list[Finding] = []
    for target in manual_targets:
        extra_findings.append(
            Finding(
                category=Category.MANUAL_TRANSFER,
                message=(
                    f"{target.name} ({target.x}|{target.y}): Travian only allows Gold Club "
                    f"trade routes to your own, Wonder, or alliance/confederacy artifact "
                    f"villages, so its {target.crop_per_hour:.0f}/h crop obligation is a "
                    f"MANUAL transfer — it is not in the route plan and no merchants are "
                    f"reserved for it. Ship it by hand, or mark it route-eligible if it "
                    f"really is one of those villages."
                ),
                detail=f"{target.name} — {target.crop_per_hour:,.0f}/h crop",
                village=target.name,
                resource=Resource.CROP,
            )
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
        dropped_allocations: list[str] = []
        for resource in sorted(set(allocations) - set(productions), key=lambda r: r.value):
            if allocations.pop(resource):
                dropped_allocations.append(
                    f"every {resource.value} allocation (no rate is known for any village)"
                )
                extra_findings.append(
                    Finding(
                        category=Category.UNREADABLE_RATE,
                        message=(
                            f"{resource.value}: no production rate is known for any village, "
                            f"so its allocations were ignored"
                        ),
                        detail=f"{resource.value} — no rate for any village",
                        resource=resource,
                    )
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
                labels = [village_label(vid, names) for vid in unreadable]
                dropped_allocations.append(
                    f"the {resource.value} allocation(s) of " + ", ".join(labels)
                )
                extra_findings.append(
                    Finding(
                        category=Category.UNREADABLE_RATE,
                        message=(
                            f"{resource.value}: no rate could be read for "
                            + ", ".join(labels)
                            + f", so their {resource.value} allocations were ignored"
                        ),
                        detail=f"{resource.value} — " + ", ".join(labels),
                        resource=resource,
                    )
                )
        # The beat search is pure CPU; off the event loop so it cannot stall
        # WebSocket frames or stealth-timed game requests while the user replans.
        plan = await asyncio.to_thread(
            craft_plan, villages, productions, allocations, config, supplements, consumption
        )
    except AllocationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # What the stock floors are actually funding. An allowance nobody drew on
    # says nothing and is not reported; shipping that DOES exceed production is a
    # standing dependency on the operator's NPC trading, so it is named.
    floors = {
        cfg.village_id: cfg.stock_floor_fraction
        for cfg in body.config
        if cfg.stock_floor_fraction is not None
    }
    for vid, floor in sorted(floors.items()):
        label = village_label(vid, names)
        drawn: dict[Resource, float] = {}
        for resource in MATERIALS:
            rp = plan.resource_plans.get(resource)
            if rp is None:
                continue
            allocation = next((v for v in rp.villages if v.village_id == vid), None)
            if allocation is None:
                continue
            beyond = -allocation.ship_per_hour - allocation.own_per_hour
            if beyond > _MIN_REPORTED_STOCK_DRAW:
                drawn[resource] = beyond
        if not drawn:
            continue
        total_drawn = sum(drawn.values())
        # `Resource` is a StrEnum, so it sorts by its own value; the explicit key
        # was saying the same thing twice.
        named = [r.value for r in sorted(drawn)]
        which = " and ".join(filter(None, [", ".join(named[:-1]), named[-1]]))
        extra_findings.append(
            Finding(
                category=Category.STOCK_FUNDED,
                message=(
                    f"{label} ships {total_drawn:,.0f}/h of {which} beyond its "
                    f"production, funded from its stock floor -- keep the warehouse "
                    f"at least {floor:.0%} full by NPC or these routes under-deliver"
                ),
                detail=f"{label} -- {total_drawn:,.0f}/h stock-funded",
                village=label,
            )
        )
        # NPC trades crop for materials one for one, so the floor refills no
        # faster than the crop this village has left after the plan's crop
        # routes. Beyond that the floor sinks and the routes above go short.
        crop_plan = plan.resource_plans.get(Resource.CROP)
        crop_alloc = (
            next((v for v in crop_plan.villages if v.village_id == vid), None)
            if crop_plan is not None
            else None
        )
        if crop_alloc is not None:
            crop_surplus = crop_alloc.own_per_hour + min(0.0, crop_alloc.ship_per_hour)
        else:
            crop_surplus = next(
                (v.crop_per_hour or 0.0 for v in body.snapshot if v.village_id == vid), 0.0
            )
        deficit = total_drawn - crop_surplus
        if deficit > _MIN_REPORTED_STOCK_DRAW:
            extra_findings.append(
                Finding(
                    category=Category.STOCK_FLOOR_UNSUSTAINABLE,
                    message=(
                        f"{label}'s stock floor is drawn down {deficit:,.0f}/h faster "
                        f"than its crop surplus can replenish by NPC; the floor will "
                        f"not hold and these routes will start arriving short"
                    ),
                    detail=f"{label} -- short {deficit:,.0f}/h of crop to refill",
                    village=label,
                )
            )

    # How much to trust every merchant figure above. `EUROPE2_TEUTON` is pinned
    # on one end only: the base was re-read off the game on 2026-09-02, while
    # the +20%-per-level bonus is carried over from the profile and has never
    # been measured against it. So a village with a Trade Office has a capacity
    # nobody has checked -- and by `VillageConfig.trade_office_level`'s own
    # rule, overstating capacity breaches the merchant budget invisibly where
    # understating it merely over-provisions. Said only when it matters: at
    # level 0 the capacity IS the base, so an account with no Trade Office
    # anywhere never applies the unmeasured multiplier, and an operator who
    # sent their own bonus has already done what this asks for.
    #
    # Names the TO 0 villages because that is the reading that closes it:
    # capacity at level 0 is the base with no inversion, which is the sample
    # `calibrate` prefers, and any second level then pins the bonus.
    if body.trade_office_bonus_per_level == EUROPE2_TEUTON.bonus_per_trade_office_level:
        levelled = sorted(vid for vid, level in trade_office.items() if level > 0)
        if levelled:
            zero_level = sorted(
                v.village_id for v in body.snapshot if trade_office.get(v.village_id, 0) == 0
            )
            where = (
                ", ".join(village_label(vid, names) for vid in zero_level)
                if zero_level
                else "a village with no Trade Office"
            )
            extra_findings.append(
                Finding(
                    category=Category.MERCHANT_MODEL_UNCALIBRATED,
                    message=(
                        f"The merchant model is calibrated on its base only: "
                        f"{body.merchant_base_capacity:,} was read off the game, but the "
                        f"+{body.trade_office_bonus_per_level:.0%} per Trade Office level "
                        f"has never been measured against it, and "
                        f"{len(levelled)} village(s) in this plan have a Trade Office. "
                        f"Read a Marketplace capacity at {where} to settle the base with "
                        f"no inversion, and one at any levelled village to pin the bonus"
                    ),
                    detail=(
                        f"base {body.merchant_base_capacity:,} measured, "
                        f"+{body.trade_office_bonus_per_level:.0%}/level not"
                    ),
                )
            )

    free_now = {v.village_id: v.merchants_free for v in body.snapshot}
    for vid in sorted(plan.merchants_committed):
        committed = plan.merchants_committed[vid]
        if committed > free_now.get(vid, 0):
            label = village_label(vid, names)
            free = free_now.get(vid, 0)
            extra_findings.append(
                Finding(
                    category=Category.MERCHANTS_BUSY,
                    message=(
                        f"{label}: the plan commits {committed} merchants but only "
                        f"{free} are free right now — existing routes or "
                        f"shipments must release the rest before the sheet is executable"
                    ),
                    detail=f"{label} — needs {committed}, {free} free",
                    village=label,
                )
            )

    # Off the loop for the same reason craft_plan is, three lines above: this
    # runs simulate_day, a 14-day discrete replay of the beat. Measured with a
    # 10ms heartbeat alongside the request, it was a single 292ms stall at 23
    # villages and 566ms at 40 -- and the day check calls this once per profile,
    # so three profiles blocked the loop for ~1.6s while stealth-timed game
    # requests and WebSocket frames waited.
    extra_findings.extend(await asyncio.to_thread(_storage_findings, body, plan, effective_window))

    restricted = {c.village_id for c in body.config if c.ship_only_to is not None}
    for target_id, target in foreign_ids.items():
        suppliers = sorted({row.origin for row in plan.rows if row.destination == target_id})
        # From the EMITTED rows, not from the whitelist: an operator who
        # restricted a village and never saw it routed to a tribute has nothing
        # to act on, and a finding raised on the intent would fire on every
        # restricted village in the account.
        for row in sorted(
            (r for r in plan.rows if r.destination == target_id and r.origin in restricted),
            key=lambda r: r.origin,
        ):
            label = village_label(row.origin, names)
            merchants = f"{row.merchants} merchant{'' if row.merchants == 1 else 's'}"
            extra_findings.append(
                Finding(
                    category=Category.WHITELIST_VS_TRIBUTE,
                    message=(
                        f"{label} is restricted by ship_only_to, but the plan still "
                        f"supplies the tribute {target.name} ({target.x}|{target.y}) from "
                        f"it -- {row.one_way_minutes / 60.0:.1f}h each way on "
                        f"{merchants}. A whitelist covers own villages "
                        f"only; put {label} in that target's exclude_origins to keep it off"
                    ),
                    detail=f"{label} -> {target.name} — {merchants}",
                    village=label,
                    resource=Resource.CROP,
                )
            )
        if not suppliers:
            extra_findings.append(
                Finding(
                    category=Category.TRIBUTE_UNFUNDED,
                    message=(
                        f"{target.name} ({target.x}|{target.y}) is owed "
                        f"{target.crop_per_hour:,.0f} crop/h but no village could supply it"
                    ),
                    detail=f"{target.name} — {target.crop_per_hour:,.0f}/h unpaid",
                    village=target.name,
                    resource=Resource.CROP,
                )
            )
            continue
        if len(suppliers) > 1:
            extra_findings.append(
                Finding(
                    category=Category.TRIBUTE_SPLIT,
                    message=(
                        f"{target.name} ({target.x}|{target.y}) is supplied by "
                        + ", ".join(village_label(vid, names) for vid in suppliers)
                        + " — several routes to keep track of; consider raising one "
                        "supplier's share so a single route covers it"
                    ),
                    detail=f"{target.name} — {len(suppliers)} suppliers",
                    village=target.name,
                    resource=Resource.CROP,
                )
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
        extra_findings.append(
            Finding(
                category=Category.TRIBUTE_COLD_START,
                message=note,
                detail=f"{target.name} — up to {first:.1f}h",
                village=target.name,
                resource=Resource.CROP,
            )
        )

    coords = {vid: village.coords for vid, village in villages.items()}
    return _PlannedAccount(
        plan=plan,
        villages=villages,
        coords=coords,
        names=names,
        trade_office=trade_office,
        foreign_ids=foreign_ids,
        config=config,
        extra_findings=extra_findings,
        dropped_allocations=dropped_allocations,
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
    findings = account.all_findings
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
                total_supplement=rp.total_supplement,
                unallocated=rp.unallocated,
                remainder_village_id=rp.remainder_village_id,
            )
            for resource, rp in sorted(plan.resource_plans.items(), key=lambda kv: kv[0].value)
        ],
        total_merchants=plan.total_merchants,
        feasible=plan.is_feasible,
        # Built from the COMPLETE finding list, not plan.findings: overflow,
        # starvation and busy merchants are computed here, and they are precisely
        # what feasibility does not weigh.
        verdict=_verdict_response(account.verdict()),
        relays=[
            RelayResponse(
                hub=relay.hub,
                hub_name=village_label(relay.hub, names),
                origins=list(relay.origins),
                origin_names=[village_label(vid, names) for vid in relay.origins],
                destinations=list(relay.destinations),
                destination_names=[village_label(vid, names) for vid in relay.destinations],
                collect_hours=relay.collect_hours,
                forward_hours=relay.forward_hours,
                end_to_end_hours=relay.end_to_end_hours,
            )
            for relay in plan.relays
        ],
        warnings=[f.message for f in findings],
        # The route count is what lets the headline stop blaming the plan for
        # losses it did not cause -- see _account_headline.
        diagnostics=_diagnostics_response(summarise(findings, routes_planned=len(plan.rows))),
    )


class RevertPlanRequest(BaseModel):
    """Ask what it would take to undo a previous live run."""

    trace_id: str = Field(
        # Interpolated into a filename, so it is constrained to exactly the shape
        # ExecutionTrace generates (uuid4().hex[:12]). Unvalidated it was an
        # authenticated arbitrary-.jsonl read via `../`, and worse than a read:
        # a wrong file becomes the "before" inventory, so every currently-live
        # route looks newly created -- and with apply_disable that disables all
        # of them.
        pattern=r"^[0-9a-f]{12}$",
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
    apply_delete: bool = Field(
        default=False,
        description=(
            "Actually DELETE the routes the run created, removing them for good. "
            "Separate from apply_disable and off by default because it is the one "
            "irreversible action here: a disabled route can be switched back on, "
            "a deleted one cannot. Disabling happens first regardless, so the "
            "routes stop shipping even if the delete then fails."
        ),
    )
    apply_disable: bool = Field(
        default=False,
        description=(
            "Actually disable the routes the run created, stopping them shipping. "
            "Reversible, and applied before any delete, so the resources stop "
            "moving even if the removal then fails."
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
    deleted_now: dict[int, list[int]] = {}
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

    Reverting is deliberately not a single button. Disabling and deleting are
    both available and both verified, but they differ in kind -- a disabled route
    can be switched back on, a deleted one cannot -- so each is its own opt-in and
    disabling always happens first: a created route left enabled while someone
    gets round to removing it keeps shipping resources.

    Every step is confirmed by re-reading the page. A revert that claimed to have
    undone a run while leaving live routes behind would be worse than one that
    names the rows still outstanding.
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
    deleted_now: dict[int, list[int]] = {}
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
            try:
                result = await svc.disable_routes(origin, live)
                requests_used += 1
            except TravianError as exc:
                # Most likely the live opt-in is off. Previously this propagated
                # as a bare 500 and discarded the whole response -- including
                # `must_delete_by_hand`, the half only a human can do. Losing the
                # undo instructions because the automated half was unavailable is
                # the worst possible trade in this endpoint.
                problems.append(
                    f"village {origin}: could not disable the created route(s) "
                    f"{plan.disable_ids} ({exc}). They are STILL RUNNING; the "
                    f"manual steps below still apply."
                )
                continue
            if result is not None and result.status == "disabled":
                # Read back, for the same reason every other write is: the PUT
                # says it was accepted, not that the rows are off. This endpoint
                # exists to make an undo trustworthy, so claiming an unverified
                # disable here would defeat its whole purpose.
                try:
                    after = await svc.confirm_routes(origin, map_span=body.map_span)
                    requests_used += 1
                except (NetworkError, MarketplaceUnreadable) as exc:
                    problems.append(
                        f"village {origin}: disabled {len(plan.disable_ids)} route(s) "
                        f"but could not re-read the page to confirm ({exc}); treat "
                        f"them as still running until you have looked"
                    )
                    continue
                still_on = [
                    e.route_id for e in after if e.route_id in set(plan.disable_ids) and e.active
                ]
                if still_on:
                    problems.append(
                        f"village {origin}: asked the game to disable {plan.disable_ids} "
                        f"and {still_on} are STILL RUNNING"
                    )
                    continue
                disabled_now[origin] = plan.disable_ids
                steps.append(
                    f"village {origin}: disabled {len(plan.disable_ids)} created "
                    f"route(s) - confirmed inert, but they still need deleting"
                )
            else:
                detail = result.detail if result is not None else "no request was made"
                problems.append(
                    f"village {origin}: could not disable created routes "
                    f"{plan.disable_ids} ({detail}); they are STILL RUNNING"
                )

        if body.apply_delete and plan.manual_delete_ids:
            # Deliberately after the disable. Disabling stops the resources
            # moving and is reversible; deleting is neither. If the delete fails
            # the routes are at least already inert.
            targets = [e for e in now if e.route_id in set(plan.manual_delete_ids)]
            try:
                removed = await svc.delete_routes(origin, targets)
                requests_used += 1
            except TravianError as exc:
                problems.append(
                    f"village {origin}: could not delete the created route(s) "
                    f"{plan.manual_delete_ids} ({exc}); they must be removed by hand"
                )
                continue
            if removed is None or removed.status != "deleted":
                detail = removed.detail if removed is not None else "no request was made"
                problems.append(
                    f"village {origin}: delete failed ({detail}); the route(s) "
                    f"{plan.manual_delete_ids} must be removed by hand"
                )
                continue
            # Read back. A delete that reports success and leaves the rows there
            # is the same class of false outcome as an unverified create, and
            # this endpoint exists to make an undo trustworthy.
            try:
                left = await svc.confirm_routes(origin, map_span=body.map_span)
                requests_used += 1
            except (NetworkError, MarketplaceUnreadable) as exc:
                problems.append(
                    f"village {origin}: deleted the route(s) but could not re-read "
                    f"the page to confirm ({exc}); check before assuming they are gone"
                )
                continue
            survivors = sorted(
                e.route_id for e in left if e.route_id in set(plan.manual_delete_ids)
            )
            if survivors:
                problems.append(
                    f"village {origin}: asked the game to delete "
                    f"{plan.manual_delete_ids} and {survivors} are STILL THERE"
                )
                continue
            deleted_now[origin] = plan.manual_delete_ids
            must_delete.pop(origin, None)
            steps.append(
                f"village {origin}: deleted {len(plan.manual_delete_ids)} created "
                f"route(s) - confirmed gone, nothing left to do by hand"
            )

    return RevertPlanResponse(
        trace_id=body.trace_id,
        steps=steps or [f"run {body.trace_id}: nothing to revert"],
        created=created,
        disabled_now=disabled_now,
        deleted_now=deleted_now,
        must_delete_by_hand=must_delete,
        restore_state=restore,
        clean=clean,
        requests_used=requests_used,
        problems=problems,
    )


def _game_rows(cycle_hours: int) -> int:
    """Rows one create request is EXPECTED to become in the game.

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


# ── Reconciliation matching rules ────────────────────────────────────────────
#
# How a desired route is recognised in what a marketplace already holds. These
# lived as closures at depth 5 inside post_execute's per-village loop, which is
# where every reconciliation bug this week lived; they were already pure (three
# took their context through default-argument binding), so they are lifted here
# to be readable and testable on their own terms.
#
# The two key kinds are deliberate and must not be unified. An OWN village is
# matched by village id, which the page states outright. A FOREIGN target has no
# id in the plan -- it is an operator-supplied coordinate carrying a synthetic
# negative id -- so it can only match on coordinates, which are back-derived
# through the world's span. Keying everything on coordinates churns every
# own-village route whenever the span is wrong; keying everything on ids churns
# every foreign one always.


def _desired_key(route: PlannedRoute) -> int | tuple[int, int]:
    """The key a desired route is recognised by: village id, or coordinates."""
    if route.dest_village_id > 0:
        return route.dest_village_id
    return (route.dest_x, route.dest_y)


def _existing_keys(e: ExistingRoute) -> set[int | tuple[int, int]]:
    """Every key this live route could be recognised by.

    Both kinds, because the route itself does not say which kind of plan entry
    (if any) wanted it. An int key and a tuple key cannot collide, and a route
    whose coordinates could not be derived contributes no coordinate key --
    which is why an unplaceable map id no longer reads as a route to nowhere
    that the plan does not want.
    """
    keys: set[int | tuple[int, int]] = {e.dest_village_id}
    if e.dest_x is not None and e.dest_y is not None:
        keys.add((e.dest_x, e.dest_y))
    return keys


def _identifiable(
    e: ExistingRoute,
    ids: set[int],
    foreign: set[tuple[int, int]],
) -> bool:
    """Can this route be matched against the plan at all?

    A FOREIGN target is known only by coordinates -- the plan has no real
    village id for one -- so a live route whose map id could not be placed has
    no key the foreign set can ever match. Judging it "not wanted" disabled it
    and then created a replacement, every single run. When the plan wants
    foreign destinations and the route cannot be placed, the honest answer is
    "I do not know", and the safe action for "I do not know" is to leave it
    alone.
    """
    if e.dest_village_id in ids:
        return True
    if not foreign:
        return True  # nothing matches on coordinates anyway
    return e.dest_x is not None and e.dest_y is not None


def _is_wanted(
    e: ExistingRoute,
    ids: set[int],
    foreign: set[tuple[int, int]],
) -> bool:
    """Does the plan want a route to where this one goes?"""
    return bool({e.dest_village_id} & ids or _existing_keys(e) & foreign)


def _is_protected(
    e: ExistingRoute,
    ids: set[int],
    coords: set[tuple[int, int]],
) -> bool:
    """Declared off-limits by the operator, whatever the plan thinks.

    Matched on the same two keys the reconciler already uses, for the same
    reason: a hand-made route to a foreign target has no usable village id and
    can only be named by where it goes on the map.
    """
    return bool({e.dest_village_id} & ids or _existing_keys(e) & coords)


def _row_minute(e: ExistingRoute) -> int:
    """The minute of the day this live row departs, or -1 if unknown.

    -1 can never equal a planned minute, so a row whose departure could not be
    read reconciles by recreation rather than by trust.
    """
    if e.departure_at is None:
        return -1
    return int(e.departure_at % 86400) // 60


def _planned_minutes(route: PlannedRoute) -> list[int]:
    """The minutes of the day this route's rows will depart, after the trim.

    Travian fans "repeat every N hours" into 24/N daily rows offset by the cycle
    from the Send-at minute (proven live: departure_at % 86400 is exactly the
    payload minute). Each route is judged against ITS OWN profile window -- a
    whole-day run carries several profiles in one pass, and a Night row is not
    stale for keeping Night hours.
    """
    total = _game_rows(route.cycle_hours)
    minutes = [
        (route.dispatch_minute + i * route.cycle_hours * 60) % MINUTES_PER_DAY for i in range(total)
    ]
    if route.window is not None:
        start, end = route.window
        inside = [
            m for m in minutes if ((start <= m < end) if start <= end else (m >= start or m < end))
        ]
        # window_pruning refuses to delete every row, so a route with no
        # in-window departure keeps them all.
        if inside:
            minutes = inside
    return sorted(minutes)


def _off_schedule(e: ExistingRoute, mismatched: Mapping[int | tuple[int, int], str]) -> bool:
    """Part of a destination whose live row set diverges from the plan.

    The WHOLE set is recreated, not just the offending rows: a create fans out
    every row again, so keeping the on-minute survivors and creating would
    duplicate them.
    """
    return bool(_existing_keys(e) & set(mismatched))


def _rows_that_survive(
    cycle_hours: int,
    dispatch_minute: int,
    window: tuple[int, int] | None,
) -> int:
    """Rows this create leaves in the game once the window prune has run.

    Without a prune that is the whole 24/N fan-out. With one it is only the
    departures inside the profile's hours, which for an 8-hour window is a third
    of them -- and the footprint is what the operator authorised and what they
    would have to delete, not the rows that existed for a minute in between.

    Never returns 0: a route whose every departure falls outside the window would
    be pruned away entirely, and window_pruning refuses that rather than deleting
    what the run just made. Charging 0 here would let such a route through a
    budget of any size.
    """
    total = _game_rows(cycle_hours)
    if window is None:
        return total
    start, end = window
    inside = 0
    for i in range(total):
        minute = (dispatch_minute + i * cycle_hours * 60) % MINUTES_PER_DAY
        if (start <= minute < end) if start <= end else (minute >= start or minute < end):
            inside += 1
    return max(1, inside)


# How long a caller should wait before asking for the next chunk of a sweep.
# Drawn per response rather than fixed: a client that comes back on a metronome
# is its own signature, however long the interval. Wide, because the operation
# being imitated is a person working through their villages, not a poller.
#
# SHAPE matters as well as spread, and this was the one gap in the project still
# drawn from a flat uniform. throttler.py and human_delay.py both carry the
# argument for why: a uniform draw is a distinguishable shape in its own right --
# rejectable by a KS test against real human timing even though no single value
# repeats -- which is why every other gap here is heavy-tailed. The frontend
# sweep loop returns for the next chunk automatically, so this is an automated
# cadence, not a human one, and it should look like the rest.
# Calibrated so the MEAN gap is unchanged by this switch: the old uniform
# 45-240s averaged 142.5s, and floor 45 + delay(130) capped at 360 measures 143s
# over 4,000 draws (median 105s -- the shape is now right-skewed, which is the
# whole point). Preserving the mean matters because a stealth change must never
# tighten a cadence: that would send more traffic than the previous behaviour,
# which is the opposite of the intent.
_CHUNK_GAP_FLOOR_S = 45.0
_CHUNK_GAP_MEAN_S = 130.0
_CHUNK_GAP_CAP_S = 360.0  # a longer hold reads as a hung sweep to whoever waits


def _chunk_gap_seconds() -> float:
    """Seconds a client should hold before requesting the next sweep chunk."""
    drawn = HumanTiming.delay(_CHUNK_GAP_MEAN_S, variance_factor=1.0)
    return round(min(_CHUNK_GAP_FLOOR_S + drawn, _CHUNK_GAP_CAP_S), 1)


async def _browse_between_villages(
    svc: TradeRouteService,
    origin: int,
    trace: ExecutionTrace,
    sweeping: bool,
) -> None:
    """Idle browsing between villages, so a sweep does not read as a sweep.

    The throttler already spaces requests and ``SessionTempo`` already drifts the
    pace, but neither changes the SHAPE of the traffic, and the shape is what a
    sweep gives away: N marketplaces in a row with nothing else between them is a
    pattern no player produces. ``NoiseInjector`` exists for exactly this and is
    already used by the farm-list, scouting and build-queue loops -- the
    trade-route path was the only write path in the app not calling it.

    Deliberately does NOT take a session break. A break here is a two-to-ten
    minute sleep inside an HTTP handler, which no client waits out; the pause
    between CHUNKS is the break, and it is a real one because a person is on the
    other end of it. See ``next_chunk_wait_seconds``.

    Never raises. This is camouflage, not the operation -- a failed idle browse
    must not undo writes that already landed, which is the same rule the trace
    follows.
    """
    injector = getattr(getattr(svc, "http_client", None), "noise_injector", None)
    if injector is None:
        return
    try:
        if await injector.maybe_inject_noise(village_id=origin):
            trace.event("noise_injected", origin=origin, during_sweep=sweeping)
    except Exception as exc:  # noqa: BLE001
        trace.event("noise_failed", origin=origin, error=str(exc))


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
    if body.segments:
        # One optimizer pass per profile, each in its own hours -- identical to
        # how /day-check plans them, so execute and the day picture can never
        # disagree about what a profile's routes are. The snapshot is shared,
        # so names/coords/foreign ids are identical across accounts; the first
        # stands in for all of them everywhere a single account was used.
        planned_segments: list[tuple[DaySegmentInput, _PlannedAccount]] = []
        for segment in body.segments:
            per_segment = body.model_copy(
                update={
                    "allocations": segment.allocations,
                    # Latency is judged against the profile's own hours: an
                    # 8-hour night cannot be asked to meet a 16-hour day's
                    # target, nor vice versa.
                    "max_latency_hours": (
                        (
                            (segment.window[1] - segment.window[0]) % MINUTES_PER_DAY
                            or MINUTES_PER_DAY
                        )
                        / 60.0
                    ),
                }
            )
            planned_segments.append(
                (segment, await _plan_account(per_segment, dispatch_window=tuple(segment.window)))
            )
        account = planned_segments[0][1]
    else:
        account = await _plan_account(body)
        planned_segments = [(None, account)]
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
    # Whole-day runs prefix each segment's so the operator can tell whose is whose.
    if body.segments:
        warnings = [
            w if segment is None else f"{segment.name}: {w}"
            for segment, acc in planned_segments
            for w in acc.warnings
        ]
    else:
        warnings = list(account.warnings)

    # Each plan row is one route from a real origin village's marketplace to a
    # destination (a real village or a foreign sink — coords cover both).
    items: list[tuple[SheetRow, PlannedRoute]] = []
    # origin -> every route the FULL plan wants from it, filter or no filter.
    wanted_by_origin: dict[int, list[PlannedRoute]] = {}
    filtered_out = 0
    # (segment, row) pairs across every planned profile. Single-profile runs are
    # the one-segment case of the same shape, so there is exactly one code path
    # from here down -- the union is not a mode, it is the general case.
    segment_rows = [(segment, row) for segment, acc in planned_segments for row in acc.plan.rows]
    for segment, row in segment_rows:
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
        planned = PlannedRoute(
            origin_village_id=row.origin,
            dest_village_id=row.destination,
            dest_x=dest_xy[0],
            dest_y=dest_xy[1],
            dest_name=village_label(row.destination, names),
            cargo=dict(row.cargo),
            cycle_hours=row.cycle_hours,
            merchants=row.merchants,
            # Carry the planner's scheduled send time through to live
            # execution -- without it the beat that spaces arrivals and
            # orders relay hubs is lost.
            dispatch_minute=row.dispatch_minute,
            # The hours this route belongs to. Every window consumer downstream
            # (row budget, planned-minutes reconciliation, the post-verify
            # trim) reads this rather than a request-global window, which is
            # what lets two profiles' routes ride in one run.
            window=(
                tuple(segment.window)
                if segment is not None and body.prune_to_window
                else (
                    tuple(body.dispatch_window)
                    if body.prune_to_window and body.dispatch_window
                    else None
                )
            ),
            segment=segment.name if segment is not None else "",
        )
        # Recorded BEFORE the filter, and used only to decide what counts as
        # wanted. Judging staleness against the narrowed slice instead was
        # actively destructive: `only_destinations` made every other destination
        # of a visited origin look stale, so the documented "safe first live
        # test" quietly switched off the rest of the plan and reported a clean
        # success. The filter narrows what a run CREATES; it must never change
        # what the plan wants.
        wanted_by_origin.setdefault(row.origin, []).append(planned)

        # Applied here, after the boundary guards, so a filtered-out route is
        # never confused with one that was rejected as unexecutable.
        if body.only_origins is not None and row.origin not in body.only_origins:
            filtered_out += 1
            continue
        if body.only_destinations is not None and row.destination not in body.only_destinations:
            filtered_out += 1
            continue
        items.append((row, planned))

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
            segment=route.segment,
            cargo={r: amount for r, amount in row.cargo.items() if amount},
            cycle_hours=row.cycle_hours,
            merchants=row.merchants,
            game_rows=_game_rows(row.cycle_hours),
            status=status_,
            detail=detail,
        )

    def _observed_rows(reported: list[RouteActionResponse]) -> int:
        """Rows this run MEASURED, summed. Never a forecast.

        Only an action a read-back actually attributed rows to contributes, so
        an unconfirmed create adds nothing rather than adding its prediction --
        folding `game_rows` in here is precisely the arithmetic-as-fact this
        replaced. A run that wrote three routes and could not re-read any of
        them reports 0 rows plus created_unverified=3, which is the truth.
        """
        return sum(a.observed_game_rows for a in reported if a.observed_game_rows is not None)

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
    row_cap = body.max_game_rows_per_run  # 0 = unbounded
    protected_ids: set[int] = set()
    protected_coords: set[tuple[int, int]] = set()
    for _entry in body.protect_destinations:
        _text = _entry.strip()
        if "|" in _text:
            _x, _, _y = _text.partition("|")
            protected_coords.add((int(_x.strip()), int(_y.strip())))
        else:
            protected_ids.add(int(_text))

    if body.dry_run:
        # Zero game requests: the exact routes already on each marketplace are
        # unknown here, so this previews the DESIRED plan against a worst-case
        # empty marketplace (first `cap` created, the rest deferred). The live
        # run reads each marketplace and only creates the routes that are
        # actually missing and disables only the ones the plan no longer wants,
        # so it may create/disable fewer than shown.
        actions = [_action(row, route, "would_create") for row, route in items[:cap]]
        actions += [_action(row, route, "deferred") for row, route in items[cap:]]
        # The bill, before it is spent. Reads: one per visited origin (a sweep
        # visits every village; an ordinary run only the capped slice). Verify:
        # one re-read per origin that wrote anything. Trim: one batched delete
        # plus its own confirming read, per origin whose creates fan out past
        # their window. Disables cannot be known without reading, so they only
        # widen the _max by one batched PUT per visited origin.
        _create_origins = {route.origin_village_id for _row, route in items[:cap]}
        _reads = (
            len({v.village_id for v in body.snapshot})
            if body.reconcile_all_origins
            else len(_create_origins)
        )
        _trim_origins = {
            route.origin_village_id
            for _row, route in items[:cap]
            if route.window is not None
            and _rows_that_survive(route.cycle_hours, route.dispatch_minute, route.window)
            < _game_rows(route.cycle_hours)
        }
        _known = len(items[:cap]) + _reads + len(_create_origins) + 2 * len(_trim_origins)
        requests_forecast = {
            "marketplace_reads": _reads,
            "creates": len(items[:cap]),
            "verify_reads": len(_create_origins),
            "trim_deletes": len(_trim_origins),
            "trim_verify_reads": len(_trim_origins),
            "estimated_total": _known,
            "estimated_total_max": _known + _reads,  # + up to one disable PUT per origin
        }
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
            # A forecast, and the only honest number here: this path issues zero
            # game requests, so there is nothing to have measured. The live path
            # reports what the marketplace actually showed instead.
            created_game_rows=sum(a.game_rows for a in actions if a.status == "would_create"),
            requests_forecast=requests_forecast,
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
    # Still gated on is_feasible itself, never on the message helper: if the two
    # ever disagree the authoritative one must be the one that refuses.
    for _segment, _acc in planned_segments:
        if not _acc.plan.is_feasible:
            # The blockers, not every warning. This used to concatenate
            # plan.warnings -- on a 25-village account that is 132 lines in a
            # 422 body, and the two that explain the refusal are
            # indistinguishable from the 130 that do not. Whole-day runs name
            # the profile that blocks, because "the plan" is now several.
            _who = f"{_segment.name}: " if _segment is not None else ""
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"{_who}plan is not executable; refusing to write to the account. "
                    + " ".join(f"{reason}." for reason in blockers(_acc.plan, names))
                ).strip(),
            )
    # An allocation the operator explicitly wrote that the planner had to IGNORE
    # (its village's rate could not be read) makes the executable plan a
    # different plan than the one they approved. A dry run previews it -- the
    # CRITICAL finding names it -- but going live on it silently would ship a
    # plan nobody wrote. Refused with the exact fix.
    _dropped = [
        d if _segment is None else f"{_segment.name}: {d}"
        for _segment, _acc in planned_segments
        for d in _acc.dropped_allocations
    ]
    if _dropped:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Refusing to go live: the plan had to ignore "
                + "; ".join(_dropped)
                + ". Fetch fresh state so those rates can be read, or set the "
                "allocation(s) back to 'Keep own', then run again."
            ),
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
    #   * a destination that already has a route is left untouched unless its
    #     CARGO has drifted from the plan and `update_drifted` is set, in which
    #     case the cargo is rewritten in place (the bulk edit cannot move a
    #     departure time, which is exactly what makes it safe for a fanned-out
    #     route). The drift threshold exists so the plan's arithmetic shifting by
    #     a few crop does not rewrite every route every run;
    #   * hidden entries would be honeypots: invisible to a human, so we would
    #     neither act on them (never disabled) nor let them influence us (never
    #     deduped against). VESTIGIAL today -- the page's React model has no
    #     hidden-entry concept, so the parser marks every route visible and this
    #     branch never fires. Kept because it is the right shape if a gpack ever
    #     grows one; do not read it as an active defence.
    #
    # The run is bounded by `cap` in three dimensions: it reads at most `cap`
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
    if len(planned_segments) > 1:
        # Merchants are one fleet shared across the day. Each profile fits its
        # own budget, but a long round trip started late in one window is still
        # in the air when the next begins -- the sum across profiles is the
        # honest upper bound. Warned, never blocked: a short merchant is a late
        # send, not a disaster, and the windows' separation usually absorbs it.
        _fleet = {v.village_id: v.merchants_total for v in body.snapshot}
        _committed_sum: dict[int, int] = {}
        for _segment, _acc in planned_segments:
            for _vid, _n in _acc.plan.merchants_committed.items():
                _committed_sum[_vid] = _committed_sum.get(_vid, 0) + _n
        for _vid, _total in sorted(_committed_sum.items()):
            _have = _fleet.get(_vid, 0)
            if _total > _have > 0:
                warnings.append(
                    f"{village_label(_vid, names)}: the profiles together commit "
                    f"{_total} merchants against a fleet of {_have}; round trips "
                    f"crossing a window boundary may briefly run short, delaying "
                    f"sends rather than losing them"
                )
    if body.reconcile_all_origins:
        # Every own village, whether the plan still ships from it or not. A
        # village with no desired routes is not skipped -- it is the case this
        # exists for: nothing is wanted there, so everything there is stale.
        # Ordered from the snapshot, then shuffled like any other sweep.
        origins = [v.village_id for v in body.snapshot]
        for origin in desired_by_origin:
            if origin not in origins:
                origins.append(origin)
        # `only_origins` narrows the SWEEP too, not just which planned routes are
        # eligible. That is what makes chunking work with one existing field: a
        # caller feeds back the previous run's `unswept_origins` and gets exactly
        # those villages visited. Without this, every chunk would re-read the
        # whole account and a loop would never terminate.
        if body.only_origins is not None:
            allowed = set(body.only_origins)
            origins = [o for o in origins if o in allowed]
    else:
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
    updates: list[str] = []  # cargo corrected on a route that already existed
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
        # The cap-largest values, not the first `cap` in plan order. Origins are
        # visited in SHUFFLED order, so slicing plan order was not an upper
        # bound at all: a run capped at "1 row" could legitimately create 24.
        max_game_rows_this_run=sum(
            sorted((_game_rows(row.cycle_hours) for row, _ in items), reverse=True)[:cap]
        ),
        # Recorded so a trace can never be read as a full run when it was not.
        filtered_to=filtered_to,
        planned_routes_excluded_by_filter=filtered_out,
    )

    attempts = 0  # create requests fired this run
    updates_done = 0  # cargo-correction PUTs fired this run, bounded by `cap`
    visited = 0  # marketplaces read this run
    rows_written = 0  # route ROWS this run has put in the game
    consecutive_failures = 0  # create refusals in a row, across origins
    swept: list[int] = []  # villages actually reconciled (read) this run
    unswept: list[int] = []  # villages a reconcile sweep did not reach
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
            # (issue #64). check_activity_budget raises; the seconds each
            # request consumed are billed by the SERVICE, as farm_list_service
            # does -- the HttpClient bills nothing, and believing otherwise is
            # how the reads went uncounted for a while.
            try:
                svc.http_client.check_activity_budget()
            except ActivityBudgetExhausted as exc:
                problems.append(f"Activity budget exhausted; no routes were created: {exc}")
                deferred.extend(items)
                origins = []

            # A full reconciliation must not be cut short by the CREATE budget:
            # the whole point is that no village is left holding a route the plan
            # rejected, and the create cap is a pacing dial for writes, not a
            # limit on how much of the account may be inspected. Creates stay
            # bounded individually further down, so spending the budget defers
            # the next route while the sweep carries on.
            sweep_all = body.reconcile_all_origins
            origin_cap = body.max_origins_per_run  # 0 = unbounded
            for origin in origins:
                if sweep_all:
                    # Bounded only by how many villages this CALL may visit, so
                    # the sweep survives spending the create budget.
                    budget_spent = bool(origin_cap) and visited >= origin_cap
                else:
                    budget_spent = attempts >= cap or visited >= cap
                if stopped_early or gold_club_blocked or budget_spent:
                    if sweep_all:
                        # Not reconciled, so it must be named. Includes the runs
                        # that stopped on a captcha or an exhausted budget: those
                        # villages are no more swept than the ones never reached.
                        unswept.append(origin)
                    # Budget spent, Gold Club missing, or the run was stopped
                    # (captcha / budget / a read failure): defer EVERY remaining
                    # origin WITHOUT reading its marketplace, so reads and writes
                    # stay bounded and no later origin's routes are silently lost
                    # from the response (issue #65).
                    trace.event(
                        "origin_deferred",
                        origin=origin,
                        routes=len(desired_by_origin.get(origin, [])),
                        reason=(
                            "run stopped early"
                            if stopped_early
                            else "gold club blocked"
                            if gold_club_blocked
                            else f"per-run cap of {cap} reached "
                            f"(creates={attempts}, marketplaces read={visited})"
                        ),
                    )
                    deferred.extend(desired_by_origin.get(origin, []))
                    continue
                # Don't even read a marketplace if the run is already stopped
                # (captcha resolved / budget exhausted). Fires once — later
                # origins are caught by the top-of-loop guard above.
                reason = _stop_reason()
                if reason:
                    stopped_early = True
                    if sweep_all:
                        unswept.append(origin)
                    problems.append(reason)
                    trace.event(
                        "origin_deferred",
                        origin=origin,
                        routes=len(desired_by_origin.get(origin, [])),
                        reason=reason,
                    )
                    deferred.extend(desired_by_origin.get(origin, []))
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
                            routes_deferred=len(desired_by_origin.get(origin, [])),
                        )
                        problems.append(
                            f"{village_label(origin, names)}: marketplace read failed "
                            f"({exc}); remaining routes deferred"
                        )
                        deferred.extend(desired_by_origin.get(origin, []))
                        # Stop, but do NOT break: `continue` lets the top-of-loop
                        # guard defer every still-unvisited origin too, so they are
                        # counted in `remaining` instead of vanishing (issue #65).
                        stopped_early = True
                        if sweep_all:
                            unswept.append(origin)
                        continue
                    visited += 1
                    # Only a village whose marketplace was actually READ counts
                    # as reconciled. Everything else -- unreached, unreadable,
                    # stopped on a captcha -- stays outstanding, because the
                    # guarantee this list carries is "nothing stale survives here".
                    if sweep_all:
                        swept.append(origin)
                    # A swept village may have no planned routes at all -- that is the
                    # case reconcile_all_origins exists for, so this GETS rather
                    # than indexes. Nothing wanted here means everything here is
                    # stale, which is exactly the right conclusion.
                    desired = desired_by_origin.get(origin, [])
                    # How a desired route is recognised in what is already
                    # there. Own villages match on village id, which the page
                    # states outright. A FOREIGN target has no id in the plan --
                    # it is an operator-supplied coordinate with a synthetic
                    # negative id -- so it can only match on coordinates, which
                    # are back-derived from the page's map id. Mixing the two is
                    # deliberate: keying everything on coordinates churns every
                    # own-village route whenever the world span is wrong, and
                    # keying everything on ids churns every foreign one always.
                    wanted_here = wanted_by_origin.get(origin, [])
                    desired_ids = {
                        route.dest_village_id for route in wanted_here if route.dest_village_id > 0
                    }
                    desired_foreign = {
                        (route.dest_x, route.dest_y)
                        for route in wanted_here
                        if route.dest_village_id < 0
                    }

                    # The desired ROW SET per destination, not just the
                    # destination. Travian fans "repeat every N hours" into 24/N
                    # daily rows offset by the cycle from the Send-at minute
                    # (proven live: departure_at % 86400 is exactly the payload
                    # minute), and the trim then deletes the out-of-window ones.
                    # This is what the game should hold when the run is done --
                    # matching on destination alone let a profile switch leave
                    # the old profile's rows running, never pruned pre-existing
                    # out-of-window rows, and let cargo correction write one
                    # daily batch onto 24 hourly rows.
                    # Minute MULTISETS, merged per destination. A dict
                    # comprehension here silently kept only the LAST route's
                    # minutes, and a whole-day union routinely wants the same
                    # (origin, destination) from two profiles -- every shared
                    # destination would have mismatched forever and churned
                    # disable+recreate on every run.
                    expected_rows: dict[int | tuple[int, int], list[int]] = {}
                    for route in wanted_here:
                        expected_rows.setdefault(_desired_key(route), []).extend(
                            _planned_minutes(route)
                        )
                    for _k in expected_rows:
                        expected_rows[_k] = sorted(expected_rows[_k])

                    active_rows_by_key: dict[int | tuple[int, int], list[ExistingRoute]] = {}
                    for e in existing:
                        if e.visible and e.active:
                            for k in _existing_keys(e) & set(expected_rows):
                                active_rows_by_key.setdefault(k, []).append(e)

                    dormant_rows_by_key: dict[int | tuple[int, int], list[ExistingRoute]] = {}
                    for e in existing:
                        if e.visible and not e.active:
                            for k in _existing_keys(e) & set(expected_rows):
                                dormant_rows_by_key.setdefault(k, []).append(e)

                    # Which desired ROUTES the live rows already satisfy. Keyed
                    # by identity, not destination: a whole-day union routinely
                    # wants two routes to one destination (Day's and Night's),
                    # and satisfying the KEY on the first create skipped the
                    # second route entirely.
                    satisfied_route_ids: set[int] = set()
                    # Destinations whose planned rows ALL exist at their planned
                    # minutes but some are switched off. The fix for those is
                    # re-enabling exactly the off rows -- never disable-and-
                    # recreate, which costs more writes and churns row ids.
                    completable: dict[int | tuple[int, int], list[ExistingRoute]] = {}
                    mismatched: dict[int | tuple[int, int], str] = {}
                    routes_by_key: dict[int | tuple[int, int], list[PlannedRoute]] = {}
                    for _route in wanted_here:
                        routes_by_key.setdefault(_desired_key(_route), []).append(_route)
                    for k, planned in expected_rows.items():
                        rows = active_rows_by_key.get(k, [])
                        dormant = dormant_rows_by_key.get(k, [])
                        if not rows and not dormant:
                            continue  # nothing live; the ordinary create path
                        # Subtract each route whose planned minutes are wholly
                        # present. What survives is stray: rows no route claims,
                        # or the remnants of a torn route -- either way the key
                        # cannot be trusted and reconciles by full recreate.
                        pool = Counter(_row_minute(e) for e in rows)
                        present: list[PlannedRoute] = []
                        for _route in routes_by_key.get(k, []):
                            mine = Counter(_planned_minutes(_route))
                            if all(pool[m] >= n for m, n in mine.items()):
                                pool -= mine
                                present.append(_route)
                        unserved = len(present) < len(routes_by_key.get(k, []))
                        if not +pool:
                            # Nothing stray. Present routes are served; absent
                            # ones go to the create path -- the half-provisioned
                            # account gains its other profile without churn --
                            # UNLESS the dormant rows complete the union, where
                            # one re-enable beats a create-and-trim.
                            satisfied_route_ids.update(id(_route) for _route in present)
                            if (
                                unserved
                                and dormant
                                and sorted(_row_minute(e) for e in rows + dormant) == planned
                            ):
                                completable[k] = dormant
                        elif dormant and sorted(_row_minute(e) for e in rows + dormant) == planned:
                            completable[k] = dormant
                        else:
                            got = sorted(_row_minute(e) for e in rows)
                            mismatched[k] = (
                                f"{len(rows)} live row(s) departing at minutes "
                                f"{sorted(set(got))} where the plan wants {planned}"
                            )

                    # A mismatched destination whose rows the operator protected
                    # cannot be recreated without shipping twice, so it is left
                    # exactly as it is -- counted as served, reported as diverged.
                    for k in list(mismatched):
                        if any(
                            _is_protected(e, protected_ids, protected_coords)
                            for e in active_rows_by_key.get(k, [])
                        ):
                            problems.append(
                                f"{village_label(origin, names)}: protected route(s) to "
                                f"{k} run a different schedule than the plan "
                                f"({mismatched[k]}); left untouched, so the plan's "
                                f"figures for this destination will not match reality"
                            )
                            satisfied_route_ids.update(
                                id(_route) for _route in routes_by_key.get(k, [])
                            )
                            del mismatched[k]

                    if mismatched:
                        trace.event(
                            "schedule_mismatch",
                            origin=origin,
                            destinations={str(k): why for k, why in mismatched.items()},
                        )

                    # Honeypots (hidden) would be ignored entirely — neither
                    # acted on nor treated as occupying a destination. A no-op
                    # as things stand: nothing produces visible=False, because
                    # the page model has no hidden rows to read.
                    visible = [e for e in existing if e.visible]
                    # Route ids this origin asked the game to switch off. Like a
                    # create, a disable is only CLAIMED until the page is read
                    # back -- and an undisabled stale route is worse than an
                    # uncreated one, because it keeps shipping resources.
                    disabled_here: list[int] = []

                    if body.disable_existing:
                        # Disable only ACTIVE visible routes the plan no longer
                        # wants; a route already disabled needs no action.
                        stale = [
                            e
                            for e in visible
                            if e.active
                            and _identifiable(e, desired_ids, desired_foreign)
                            and (
                                not _is_wanted(e, desired_ids, desired_foreign)
                                or _off_schedule(e, mismatched)
                            )
                            and not _is_protected(e, protected_ids, protected_coords)
                        ]
                        # Named separately from `stale`: these ARE unwanted by the
                        # plan and are being left running on purpose, which is a
                        # different fact from "the plan wants this". Silence would
                        # read as a clean reconciliation while they keep shipping.
                        protected_here = [
                            e
                            for e in visible
                            if e.active
                            and _identifiable(e, desired_ids, desired_foreign)
                            and not _is_wanted(e, desired_ids, desired_foreign)
                            and _is_protected(e, protected_ids, protected_coords)
                        ]
                        if protected_here:
                            disables.append(
                                f"{village_label(origin, names)}: left "
                                f"{len(protected_here)} protected route(s) running "
                                f"that this plan does not want: "
                                f"{[e.route_id for e in protected_here]}"
                            )
                            trace.event(
                                "protected_kept",
                                origin=origin,
                                route_ids=[e.route_id for e in protected_here],
                            )
                        unidentifiable = [
                            e
                            for e in visible
                            if e.active and not _identifiable(e, desired_ids, desired_foreign)
                        ]
                        if unidentifiable:
                            # Reported, not acted on. Silence would look like a
                            # clean run while these routes keep shipping.
                            problems.append(
                                f"{village_label(origin, names)}: "
                                f"{len(unidentifiable)} live route(s) could not be "
                                f"matched against the plan (their map id would not "
                                f"place, and this plan has foreign destinations that "
                                f"are matched by coordinates). Left running rather "
                                f"than guessed at: "
                                f"{[e.route_id for e in unidentifiable]}"
                            )
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
                            disabled_here.extend(e.route_id for e in stale)
                            disables.append(line)

                    # Only a destination whose ENABLED rows match the planned
                    # fan-out is satisfied. "Some active row exists" let a 3h
                    # Day route satisfy the Night plan's 1h demand for the same
                    # destination, which is exactly the destination-only matching
                    # this replaces.
                    # Keys with a successful re-enable: every route of the key
                    # is then served (the dormant set completed the whole union).
                    satisfied: set[int | tuple[int, int]] = set()
                    # Re-enabling covers exactly the completable destinations:
                    # every planned row exists at its planned minute and the off
                    # ones only need switching back on. Anything else was
                    # classified mismatched above and reconciles by disable-and-
                    # recreate, because a create fans the whole set again and
                    # would duplicate any surviving rows.
                    disabled_desired = [e for rows in completable.values() for e in rows]
                    blocked: set[int | tuple[int, int]] = set()
                    # Routes this origin switched back ON, and the keys they
                    # cover. A re-enable is a WRITE, so it needs verifying like
                    # any other -- and the desired row it satisfies must not
                    # report "already active" for a route that was disabled
                    # until moments ago.
                    reenabled_here: list[ExistingRoute] = []
                    reenabled_keys: set[int | tuple[int, int]] = set()
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
                                reenabled_keys |= _existing_keys(e)
                                for _k2 in _existing_keys(e) & set(routes_by_key):
                                    satisfied_route_ids.update(
                                        id(_route) for _route in routes_by_key[_k2]
                                    )
                            reenabled_here.extend(disabled_desired)
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

                    # Minutes each destination gained from creates THIS visit.
                    # A later desired route colliding on any of them is the
                    # duplicate the plan should never contain twice -- creating
                    # it would stack two rows on one minute, unattributable
                    # forever after. Distinct-minute routes to the same key are
                    # the union's normal case and pass through.
                    claimed_this_visit: dict[int | tuple[int, int], Counter] = {}
                    # Routes this origin claims to have created, paired with
                    # their action so the verdict can be corrected below.
                    created_here: list[tuple[RouteActionResponse, PlannedRoute]] = []
                    # Rows whose cargo this run rewrote, with what it asked for.
                    updated_here: list[tuple[ExistingRoute, dict]] = []
                    for i, (row, route) in enumerate(desired):
                        destination = _desired_key(route)
                        # A mismatched destination in create-only mode is a dead
                        # end, not a create: the live rows run a schedule the
                        # plan does not want, fixing that requires the disable
                        # the operator withheld, and creating anyway would ship
                        # BOTH schedules at once. Found by a live probe trace --
                        # eight 3h rows, a 6h plan, disable_existing=False --
                        # where this loop happily built the duplicate: created 1,
                        # disabled 0.
                        if destination in mismatched and not body.disable_existing:
                            trace.decision(
                                origin=origin,
                                destination=destination,
                                decision="blocked",
                                reason=(
                                    "schedule mismatch in create-only mode: "
                                    + mismatched[destination]
                                ),
                            )
                            actions.append(
                                _action(
                                    row,
                                    route,
                                    "blocked",
                                    (
                                        f"live rows run a different schedule than the plan "
                                        f"({mismatched[destination]}); creating would ship both "
                                        f"at once — run with 'also disable' to replace them"
                                    ),
                                )
                            )
                            continue
                        _mine_minutes = Counter(_planned_minutes(route))
                        _already = claimed_this_visit.get(destination)
                        if _already is not None and +(_already & _mine_minutes):
                            actions.append(
                                _action(
                                    row,
                                    route,
                                    "skipped",
                                    "duplicate: a route created moments ago already "
                                    "departs at these minutes",
                                )
                            )
                            continue
                        if id(route) in satisfied_route_ids or destination in satisfied:
                            # The destination is served, but is it served with
                            # the RIGHT cargo? A route is created once and the
                            # plan moves every time production or stocks do, so
                            # without this check the live routes keep the cargo
                            # they were born with and slowly come to describe a
                            # different account than the sheet does, with
                            # nothing detecting the divergence.
                            # Only the rows that belong to THIS route: on a
                            # shared destination the other profile's rows carry
                            # a different (correct) batch, and comparing them
                            # against this route's cargo would either flag them
                            # as drifted forever or stamp this profile's batch
                            # onto them. Disjoint windows make the minute an
                            # exact attribution.
                            _mine = set(_planned_minutes(route))
                            live = [
                                e
                                for e in visible
                                if e.active
                                and destination in _existing_keys(e)
                                and _row_minute(e) in _mine
                            ]
                            drifted = [e for e in live if cargo_has_drifted(e.cargo, route.cargo)]
                            if drifted and body.update_drifted and updates_done >= cap:
                                # Bounded like every other write. These fired one
                                # paced PUT per desired route with no cap, so a
                                # drifted account turned a run the operator capped
                                # at three into a long burst of writes.
                                trace.decision(
                                    origin=origin,
                                    destination=destination,
                                    decision="deferred",
                                    reason=(
                                        f"cargo has drifted but the per-run cap of "
                                        f"{cap} update(s) is spent"
                                    ),
                                )
                                actions.append(
                                    _action(
                                        row,
                                        route,
                                        "skipped",
                                        "route active, cargo stale (update cap reached)",
                                    )
                                )
                                continue
                            if drifted and body.update_drifted:
                                reason = _stop_reason()
                                if reason:
                                    stopped_early = True
                                    problems.append(reason)
                                    deferred.extend(desired[i:])
                                    break
                                updated = await svc.update_cargo(
                                    origin,
                                    drifted,
                                    route.cargo,
                                    dest_x=route.dest_x,
                                    dest_y=route.dest_y,
                                    stop_check=_stop_reason,
                                )
                                updates_done += 1
                                if updated is not None and updated.status == "updated":
                                    updated_here.extend((e, dict(route.cargo)) for e in drifted)
                                    # Every row of a fanned-out route shares one
                                    # cargo, so this corrects all of them in one
                                    # request without touching their staggered
                                    # departure times.
                                    trace.decision(
                                        origin=origin,
                                        destination=destination,
                                        decision="updated",
                                        reason=(
                                            f"cargo drifted on {len(drifted)} row(s); "
                                            f"reset to the plan's amounts"
                                        ),
                                        route_ids=[e.route_id for e in drifted],
                                    )
                                    actions.append(
                                        _action(
                                            row,
                                            route,
                                            "updated",
                                            f"cargo reset on {len(drifted)} row(s)",
                                        )
                                    )
                                    updates.append(
                                        f"{village_label(origin, names)} -> "
                                        f"{village_label(row.destination, names)}: "
                                        f"cargo reset on {len(drifted)} row(s)"
                                    )
                                    continue
                                detail = (
                                    updated.detail if updated is not None else "no request made"
                                )
                                if updated is not None and updated.status == "stopped":
                                    stopped_early = True
                                    problems.append(updated.detail)
                                    deferred.extend(desired[i:])
                                    break
                                problems.append(
                                    f"{village_label(origin, names)} -> "
                                    f"{village_label(row.destination, names)}: cargo has "
                                    f"drifted from the plan and could not be corrected "
                                    f"({detail}); the live route is still shipping the old "
                                    f"amounts"
                                )
                                actions.append(
                                    _action(row, route, "skipped", "route active, cargo stale")
                                )
                                continue
                            trace.decision(
                                origin=origin,
                                destination=destination,
                                decision="skipped",
                                reason=(
                                    "a route to this destination is already active"
                                    if not drifted
                                    else f"active, and cargo drift on {len(drifted)} row(s) "
                                    f"was left alone (updates are off for this run)"
                                ),
                                matched_by=(
                                    "village_id" if isinstance(destination, int) else "coords"
                                ),
                            )
                            if destination in reenabled_keys:
                                # It was DISABLED when this run started and this
                                # run switched it on. Reporting "already active"
                                # contradicted re_enables in the same response.
                                actions.append(
                                    _action(
                                        row,
                                        route,
                                        "re_enabled",
                                        "route was disabled; switched back on",
                                    )
                                )
                                continue
                            actions.append(
                                _action(
                                    row,
                                    route,
                                    "skipped",
                                    "route already active"
                                    if not drifted
                                    else "route active, cargo stale (updates off)",
                                )
                            )
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
                        # The footprint budget, in the unit that actually lands in
                        # the game. Checked against the FAN-OUT, not the request:
                        # a route cannot be created partly, so one that does not
                        # fit waits for a later run rather than overshooting what
                        # the operator agreed to.
                        would_add = _rows_that_survive(
                            row.cycle_hours,
                            row.dispatch_minute,
                            route.window,
                        )
                        if row_cap and rows_written + would_add > row_cap:
                            trace.decision(
                                origin=origin,
                                destination=destination,
                                decision="deferred",
                                reason=(
                                    f"row budget: {rows_written}/{row_cap} rows used, "
                                    f"this {row.cycle_hours}h route needs {would_add} more"
                                ),
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
                        rows_written += would_add
                        result = await svc.create_route(route, stop_check=_stop_reason)
                        if result.status == "stopped":
                            # Stopped after the pacing wait, before the POST —
                            # nothing was created; defer the remainder.
                            attempts -= 1
                            rows_written -= would_add
                            stopped_early = True
                            problems.append(result.detail)
                            deferred.extend(desired[i:])
                            break
                        if result.status == "created":
                            action = _action(row, route, "created", result.detail)
                            actions.append(action)
                            created_here.append((action, route))
                            satisfied_route_ids.add(id(route))
                            claimed_this_visit.setdefault(destination, Counter()).update(
                                _mine_minutes
                            )
                            consecutive_failures = 0
                            continue
                        outstanding += 1
                        if result.status == "skipped":
                            # Gold Club is required and missing — an account-level
                            # block. Report it as "blocked" (distinct from an
                            # "already active" skip), then stop the run and defer
                            # the rest — a human would not keep firing rejects.
                            rows_written -= would_add  # nothing was created
                            actions.append(_action(row, route, "blocked", result.detail))
                            gold_club_blocked = True
                            deferred.extend(desired[i + 1 :])
                            break
                        # Nothing reached the game, so the footprint budget must
                        # not stay charged for it. `attempts` deliberately does --
                        # a refused write is still a write attempted, and pacing
                        # counts attempts -- but rows only exist if the game made
                        # them.
                        rows_written -= would_add
                        actions.append(_action(row, route, "failed", result.detail))
                        consecutive_failures += 1
                        if consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                            # Whatever is refusing these is not going to stop
                            # refusing within this run. Give up here rather than
                            # working through the rest of the sheet against it.
                            stopped_early = True
                            problems.append(
                                f"{village_label(origin, names)}: the game refused "
                                f"{consecutive_failures} create(s) in a row "
                                f"({result.detail}). Stopping rather than firing more "
                                f"— check the village's route limit in game before "
                                f"re-running."
                            )
                            trace.event(
                                "consecutive_failures",
                                origin=origin,
                                count=consecutive_failures,
                                detail=result.detail,
                            )
                            deferred.extend(desired[i + 1 :])
                            break

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
                    if created_here or disabled_here or reenabled_here or updated_here:
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
                            wrote = []
                            if created_here:
                                wrote.append(f"created {len(created_here)} route(s)")
                            if disabled_here:
                                wrote.append(f"disabled {len(disabled_here)} route(s)")
                            problems.append(
                                f"{village_label(origin, names)}: "
                                f"{' and '.join(wrote)} but could not re-read the marketplace "
                                f"to confirm ({exc}). Check this village before the next run."
                            )
                        else:
                            before_ids = {e.route_id for e in existing}
                            fresh = [e for e in after if e.route_id not in before_ids]
                            # New rows counted PER DESTINATION, not as one flat
                            # total. `fresh` is everything new at this origin,
                            # while an action is one destination -- an origin
                            # that created two routes would otherwise hand each
                            # action the other's rows as well. Keyed exactly as
                            # the reconciler matches routes (village id for own
                            # villages, coordinates for foreign targets), so
                            # attribution and recognition cannot drift apart.
                            # ...and split per CREATE within the destination: a
                            # whole-day visit can create Day's and Night's route
                            # to the same key back to back, and a key-level
                            # count handed each action the other's rows as well
                            # -- two actions each claiming all twelve. Rows are
                            # assigned EXCLUSIVELY, most-specific route first
                            # (fewest fan-out rows), exact cargo as the
                            # tie-break at shared minutes; an hourly route's
                            # minute set contains every other cycle's, so
                            # minute membership alone over-counts on exactly
                            # the accounts this exists for.
                            fresh_by_key: dict[int | tuple[int, int], list[ExistingRoute]] = {}
                            for e in fresh:
                                for key in _existing_keys(e):
                                    fresh_by_key.setdefault(key, []).append(e)
                            observed_by_action: dict[int, int] = {}
                            _claim_groups: dict[int | tuple[int, int], list] = {}
                            for action, route in created_here:
                                _claim_groups.setdefault(_desired_key(route), []).append(
                                    (action, route)
                                )
                            for key, group in _claim_groups.items():
                                unclaimed = list(fresh_by_key.get(key, []))
                                for action, route in sorted(
                                    group, key=lambda ar: _game_rows(ar[1].cycle_hours)
                                ):
                                    own = {
                                        (route.dispatch_minute + _i * route.cycle_hours * 60)
                                        % MINUTES_PER_DAY
                                        for _i in range(_game_rows(route.cycle_hours))
                                    }
                                    want = _game_rows(route.cycle_hours)
                                    cargo = {r: a for r, a in route.cargo.items() if a}
                                    taken: list[ExistingRoute] = []
                                    for exact in (True, False):
                                        for e in unclaimed:
                                            if len(taken) >= want or e in taken:
                                                continue
                                            if _row_minute(e) not in own:
                                                continue
                                            if exact and not (
                                                e.cargo is not None
                                                and {r: a for r, a in e.cargo.items() if a} == cargo
                                            ):
                                                continue
                                            taken.append(e)
                                    unclaimed = [e for e in unclaimed if e not in taken]
                                    observed_by_action[id(action)] = len(taken)

                            # Confine the fan-out to the profile hours, by
                            # subtraction. Done here because `fresh` is already the
                            # set of rows these creates are CONFIRMED to have made:
                            # pruning against an unverified read would be deleting
                            # rows on a guess. One delete for the whole origin
                            # rather than one per route.
                            if body.prune_to_window and created_here:
                                # Pooled per destination, because per-route
                                # attribution has a hole: an hourly fan-out
                                # contains EVERY minute a 4h one has, so routes
                                # protecting each other's minutes exempted each
                                # other's strays as well. Per key the truth is
                                # simple -- after the creates, the destination
                                # should hold exactly the union's planned
                                # minutes: keep fresh rows to satisfy that
                                # multiset (a row whose cargo matches the route
                                # that planned the minute wins the tie, so the
                                # Night batch survives at a night minute rather
                                # than the Day row sharing it) and delete every
                                # other row the creates made.
                                doomed: list[ExistingRoute] = []
                                _created_keys: dict[int | tuple[int, int], list[PlannedRoute]] = {}
                                # NOT named `_action`: that is the name of the
                                # helper that builds a RouteActionResponse, and a
                                # for-loop target leaks into the enclosing
                                # FUNCTION scope. Binding it here replaced the
                                # helper with a response object, so the deferred
                                # summary at the very end of the run -- outside
                                # this try block, after the trace has closed --
                                # raised "RouteActionResponse object is not
                                # callable" and the whole request 500'd with the
                                # routes already written to the game. It needed a
                                # trim AND deferred routes in the same run to
                                # fire, which is exactly a capped whole-day pass.
                                for _created_action, _route in created_here:
                                    del _created_action  # only _route is used here
                                    if _route.window is not None:
                                        _created_keys.setdefault(_desired_key(_route), []).append(
                                            _route
                                        )
                                for _key, _routes_k in _created_keys.items():
                                    _want: Counter = Counter()
                                    _cargo_at: dict[int, list[dict]] = {}
                                    for _route in _routes_k:
                                        for _m in _planned_minutes(_route):
                                            _want[_m] += 1
                                            _cargo_at.setdefault(_m, []).append(_route.cargo)
                                    _rows_k = [e for e in fresh if _key in _existing_keys(e)]
                                    _keep: set[int] = set()
                                    # Exact-cargo rows first, then anything at a
                                    # still-wanted minute. Rows whose cargo the
                                    # page did not state can only qualify in the
                                    # second pass.
                                    for _exact in (True, False):
                                        for e in _rows_k:
                                            if e.route_id in _keep:
                                                continue
                                            _m = _row_minute(e)
                                            if _want.get(_m, 0) <= 0:
                                                continue
                                            if _exact and not (
                                                e.cargo is not None
                                                and any(
                                                    {r: a for r, a in c.items() if a}
                                                    == {r: a for r, a in e.cargo.items() if a}
                                                    for c in _cargo_at.get(_m, [])
                                                )
                                            ):
                                                continue
                                            _keep.add(e.route_id)
                                            _want[_m] -= 1
                                    _leftover = [e for e in _rows_k if e.route_id not in _keep]
                                    if _leftover and not _keep:
                                        # Deleting every fresh row would destroy
                                        # the routes this run just made -- the
                                        # same refusal window_pruning encodes.
                                        problems.append(
                                            f"{village_label(origin, names)}: refusing "
                                            f"to prune every new row of {_key}; the "
                                            f"planned departure minutes matched none "
                                            f"of what the game created"
                                        )
                                        continue
                                    doomed.extend(_leftover)
                                if doomed:
                                    _seen_ids: set[int] = set()
                                    doomed = [
                                        e
                                        for e in doomed
                                        if not (
                                            e.route_id in _seen_ids or _seen_ids.add(e.route_id)
                                        )
                                    ]
                                    _ids = sorted(e.route_id for e in doomed)
                                    _res = None
                                    try:
                                        _res = await svc.delete_routes(
                                            origin, doomed, stop_check=_stop_reason
                                        )
                                    except TravianError as exc:
                                        problems.append(
                                            f"{village_label(origin, names)}: could not "
                                            f"prune {len(_ids)} out-of-window row(s) "
                                            f"({exc}); the route ships round the clock"
                                        )
                                    if _res is not None and _res.status == "deleted":
                                        # Verified like every other write here: a
                                        # delete that reports success and leaves the
                                        # rows behind is the same class of false
                                        # outcome as an unverified create.
                                        _survivors: list[int] = []
                                        try:
                                            _left = await svc.confirm_routes(
                                                origin, map_span=body.map_span
                                            )
                                            _survivors = sorted(
                                                e.route_id for e in _left if e.route_id in set(_ids)
                                            )
                                        except (NetworkError, MarketplaceUnreadable) as exc:
                                            problems.append(
                                                f"{village_label(origin, names)}: pruned "
                                                f"{len(_ids)} row(s) but could not confirm "
                                                f"({exc})"
                                            )
                                        if _survivors:
                                            problems.append(
                                                f"{village_label(origin, names)}: asked to "
                                                f"prune {_ids} and {_survivors} are STILL "
                                                f"THERE, shipping outside the profile"
                                            )
                                        else:
                                            disables.append(
                                                f"{village_label(origin, names)}: pruned "
                                                f"{len(_ids)} row(s) departing outside the "
                                                f"profile hours"
                                            )
                                    trace.event(
                                        "window_pruned",
                                        origin=origin,
                                        route_ids=_ids,
                                        status=getattr(_res, "status", None),
                                    )
                            trace.event(
                                "verified",
                                origin=origin,
                                claimed=len(created_here),
                                new_rows_found=len(fresh),
                                # The prediction, recorded next to the
                                # measurement so the record shows whether the
                                # 24/N fan-out model held on this account.
                                rows_forecast=sum(a.game_rows for a, _ in created_here),
                                new_route_ids=[e.route_id for e in fresh],
                            )
                            # A stale route we believe we switched off must
                            # actually be off. If it is still active the plan's
                            # arithmetic is wrong AND resources keep moving, so
                            # this is reported as a problem rather than folded
                            # into the disable count.
                            # A re-enable is only a claim until the page says
                            # the row is on. It was previously never checked, so
                            # a service that reported "enabled" without the row
                            # changing produced a clean run.
                            reenable_ids = {e.route_id for e in reenabled_here}
                            still_off = [
                                e.route_id
                                for e in after
                                if e.route_id in reenable_ids and not e.active
                            ]
                            if still_off:
                                problems.append(
                                    f"{village_label(origin, names)}: asked the game to "
                                    f"re-enable route(s) {still_off} and they are STILL "
                                    f"DISABLED. The plan wants them and nothing is "
                                    f"shipping."
                                )
                            if reenabled_here:
                                trace.event(
                                    "verified_reenables",
                                    origin=origin,
                                    claimed=sorted(reenable_ids),
                                    still_off=still_off,
                                )

                            # Same for a cargo rewrite: the point of correcting
                            # drift is that the live amounts match the plan, so
                            # "the PUT was accepted" is not the answer.
                            by_id = {e.route_id: e for e in after}
                            stale_after = [
                                row.route_id
                                for row, wanted in updated_here
                                if row.route_id in by_id
                                and cargo_has_drifted(by_id[row.route_id].cargo, wanted)
                            ]
                            if stale_after:
                                problems.append(
                                    f"{village_label(origin, names)}: rewrote the cargo of "
                                    f"route(s) {stale_after} and the page still shows the "
                                    f"old amounts. They are shipping something the plan "
                                    f"did not ask for."
                                )
                            if updated_here:
                                trace.event(
                                    "verified_updates",
                                    origin=origin,
                                    claimed=sorted(r.route_id for r, _ in updated_here),
                                    still_stale=stale_after,
                                )

                            still_active = [
                                e.route_id
                                for e in after
                                if e.route_id in set(disabled_here) and e.active
                            ]
                            if still_active:
                                problems.append(
                                    f"{village_label(origin, names)}: asked the game to "
                                    f"disable route(s) {still_active} and they are STILL "
                                    f"ACTIVE. They are shipping resources the plan does "
                                    f"not account for."
                                )
                            if disabled_here:
                                trace.event(
                                    "verified_disables",
                                    origin=origin,
                                    claimed=sorted(set(disabled_here)),
                                    still_active=still_active,
                                )
                            for action, route in created_here:
                                key = _desired_key(route)
                                observed = observed_by_action.get(id(action), 0)
                                # Recorded whether it is what was predicted or
                                # not, and recorded even when it is zero: zero
                                # measured is a result, unlike "not measured".
                                action.observed_game_rows = observed
                                if observed:
                                    if observed != action.game_rows:
                                        # The fan-out model is what every other
                                        # number rests on -- merchants tied up,
                                        # shipments per day, the row footprint
                                        # the operator authorised at run start.
                                        # A create that produced 1 row where 24
                                        # were predicted moves a twenty-fourth
                                        # of what the plan believes it moves,
                                        # and no other line of this response
                                        # would say so.
                                        problems.append(
                                            f"{village_label(origin, names)} -> "
                                            f"{action.destination_name}: the game made "
                                            f"{observed} route row(s), not the "
                                            f"{action.game_rows} a {action.cycle_hours}h cycle "
                                            f"predicts. This route does not ship at the rate "
                                            f"the plan assumes — check its schedule in game."
                                        )
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

                # Between VILLAGES, not between requests. The throttler already
                # spaces requests and SessionTempo already drifts the pace, but
                # neither changes the SHAPE of a sweep, and the shape is the tell:
                # twenty-five marketplaces visited back to back with nothing else
                # in between is a pattern no player produces. This was also the
                # only write path in the app not injecting idle browsing at all --
                # farm lists, scouting and the build queue all do.
                await _browse_between_villages(svc, origin, trace, sweep_all)

        # Inside the try, so it beats the fallback close in `finally`. The
        # counts below are the run's actual outcome; the fallback can only say
        # that the run ended.
        trace.close(
            created=sum(1 for a in actions if a.status == "created"),
            created_unverified=sum(1 for a in actions if a.status == "created_unverified"),
            not_created=sum(1 for a in actions if a.status == "not_created"),
            # Measured, not predicted. run_start already recorded the forecast
            # as max_game_rows_this_run; recording the forecast again here as
            # the outcome would make the record agree with itself by
            # construction and prove nothing about the account.
            created_game_rows=_observed_rows(actions),
            disabled=len(disables),
            re_enabled=len(re_enables),
            cargo_updated=len(updates),
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
        # Root cause of the same bug the tracer now also guards against: the
        # service outlives the run, so leaving it pointing at a finished trace
        # means the NEXT direct call to a write method logs into a closed file.
        # The trace belongs to this run; hand it back when the run ends.
        svc.trace = None
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
        updates=updates,
        trace_id=trace.run_id,
        trace_path=str(trace.path) if trace.path else None,
        # `remaining` = work still outstanding for a later run: routes deferred by
        # the cap PLUS any create that did not complete (failed / Gold Club), so
        # the summary never makes a partially-done run look complete.
        created=sum(1 for a in actions if a.status == "created"),
        # Reported alongside `created` rather than left to the action list: a
        # headline of "0 created" over three unconfirmed writes is a summary
        # that contradicts its own detail, and the operator should not have to
        # read prose to resolve it.
        created_unverified=sum(1 for a in actions if a.status == "created_unverified"),
        not_created=sum(1 for a in actions if a.status == "not_created"),
        # What the marketplace showed, not what the cycle length implies.
        created_game_rows=_observed_rows(actions),
        filtered_to=filtered_to,
        remaining=len(deferred) + outstanding,
        # Empty unless this run was a reconcile sweep, so an ordinary run can
        # never be misread as having cleared the account.
        swept_origins=swept,
        unswept_origins=unswept,
        next_chunk_wait_seconds=(_chunk_gap_seconds() if unswept else None),
        warnings=warnings,
        problems=problems,
    )


class RunSummaryResponse(BaseModel):
    """One past live run, as ITS OWN trace recorded it -- not as the game later
    showed it. See `run_history` for the scope this is deliberately limited to:
    what this app wrote and, where verified, saw land; never what the game did
    with a shipment afterwards.
    """

    run_id: str
    started_at: datetime
    live_enabled: bool | None
    complete: bool = Field(
        description="Whether a run_end event was found. False means the run "
        "was truncated -- killed mid-write or otherwise never reached its own "
        "ending -- and the totals below are unknown rather than zero."
    )
    failed: bool
    error: str | None = None
    event_cap_truncated: bool = Field(
        description="The run hit its own event cap (MAX_EVENTS) and stopped "
        "recording further events; distinct from `complete` being False."
    )
    elapsed_s: float | None = None
    created: int | None = None
    created_unverified: int | None = None
    not_created: int | None = None
    created_game_rows: int | None = None
    disabled: int | None = None
    re_enabled: int | None = None
    cargo_updated: int | None = None
    deferred: int | None = None
    outstanding: int | None = None
    problems: int | None = None
    stopped_early: bool | None = None
    gold_club_blocked: bool | None = None
    verify_failures: int
    schedule_mismatch_origins: list[int] = []
    needs_attention: bool = Field(
        description="Something here is worth an operator's look: an unverified "
        "or missing create, a verify failure, a reported problem, a Gold Club "
        "block, an early stop, a schedule mismatch, an outright failure, or a "
        "run that never reached its own ending."
    )


class RepeatProblemVillageResponse(BaseModel):
    village_id: int
    runs: int


class AccountRollupResponse(BaseModel):
    """Totals across the runs in this window, plus villages that keep coming up."""

    runs: int
    total_created: int
    total_created_unverified: int
    total_problems: int
    verify_failures: int
    gold_club_blocked_runs: int
    stopped_early_runs: int
    failed_runs: int
    incomplete_runs: int
    repeat_problem_villages: list[RepeatProblemVillageResponse] = []


class RunHistoryResponse(BaseModel):
    runs: list[RunSummaryResponse]
    rollup: AccountRollupResponse


def _run_summary_response(run: RunSummary) -> RunSummaryResponse:
    return RunSummaryResponse(
        run_id=run.run_id,
        started_at=run.started_at,
        live_enabled=run.live_enabled,
        complete=run.complete,
        failed=run.failed,
        error=run.error,
        event_cap_truncated=run.event_cap_truncated,
        elapsed_s=run.elapsed_s,
        created=run.created,
        created_unverified=run.created_unverified,
        not_created=run.not_created,
        created_game_rows=run.created_game_rows,
        disabled=run.disabled,
        re_enabled=run.re_enabled,
        cargo_updated=run.cargo_updated,
        deferred=run.deferred,
        outstanding=run.outstanding,
        problems=run.problems,
        stopped_early=run.stopped_early,
        gold_club_blocked=run.gold_club_blocked,
        verify_failures=run.verify_failures,
        schedule_mismatch_origins=list(run.schedule_mismatch_origins),
        needs_attention=run.needs_attention,
    )


def _rollup_response(rollup: AccountRollup) -> AccountRollupResponse:
    return AccountRollupResponse(
        runs=rollup.runs,
        total_created=rollup.total_created,
        total_created_unverified=rollup.total_created_unverified,
        total_problems=rollup.total_problems,
        verify_failures=rollup.verify_failures,
        gold_club_blocked_runs=rollup.gold_club_blocked_runs,
        stopped_early_runs=rollup.stopped_early_runs,
        failed_runs=rollup.failed_runs,
        incomplete_runs=rollup.incomplete_runs,
        repeat_problem_villages=[
            RepeatProblemVillageResponse(village_id=village_id, runs=runs)
            for village_id, runs in rollup.repeat_problem_villages
        ],
    )


@router.get("/run-history", response_model=RunHistoryResponse)
async def get_run_history(
    limit: int = Query(default=20, ge=1, le=200),
    _user: User = Depends(get_current_user),
):
    """What recent live /execute runs WROTE, from their own traces. Zero game
    requests, auth-only like /plan -- reading a local trace file costs nothing
    against the game.

    This is a write-history / audit report, NOT a delivery report: a trace
    records what this app decided and put on the wire, and -- only where a run
    verified it -- what the marketplace read-back showed right afterwards. It
    never learns whether a shipment later actually fired, arrived, or was
    changed by hand in-game. "Created" here means the write was made and, when
    verified, seen to land as a route; it says nothing about what happened to
    it after that.
    """
    history: RunHistory = summarise_runs(execution_trace.TRACE_DIR, limit=limit)
    return RunHistoryResponse(
        runs=[_run_summary_response(run) for run in history.runs],
        rollup=_rollup_response(history.rollup),
    )
