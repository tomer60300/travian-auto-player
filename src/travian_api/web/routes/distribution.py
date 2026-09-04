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
from fastapi.responses import PlainTextResponse
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
from travian_api.services.distribution.export import plan_digest, render_plan_yaml
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
    is_night_window,
)
from travian_api.services.distribution.npc import (
    NpcPolicy,
    NpcReserve,
    NpcTrigger,
    TriggerKind,
    evaluate_triggers,
    trigger_findings,
)
from travian_api.services.distribution.optimizer import (
    DEFAULT_MERCHANT_HEADROOM,
    MAX_IMPROVE_PASSES,
    MAX_RELAY_HOPS,
    MIN_SEND_FILL,
    VillageState,
    merchant_ceiling_clause,
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
from travian_api.services.distribution.roles import (
    Role,
    crop_drift_findings,
    keeps_a_morning_floor,
)
from travian_api.services.distribution.route_revert import describe, plan_revert
from travian_api.services.distribution.run_history import (
    AccountRollup,
    RunHistory,
    RunSummary,
    summarise_runs,
)
from travian_api.services.distribution.schedule import (
    MINUTES_PER_DAY,
    last_night_dispatch,
    night_overrun_minutes,
)
from travian_api.services.distribution.storage import (
    FillAtSwitch,
    ProfileSegment,
    morning_floor_shortfalls,
    night_state_findings,
    pre_night_overfills,
    relay_buffer_findings,
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


class RoleTemplate(BaseModel):
    """One profile, applied to every village of one role.

    Profile section 2.1 gives ONE consumption profile for FOUR defensive
    villages. Typed per village that is four copies of the same four numbers,
    which is four chances for them to drift apart, and the operator maintains
    those villages as one thing. So a role's profile is written once here and
    every village of that role takes it.

    A default, never a cage: an explicit per-village allocation overrides this
    one PER RESOURCE (so changing a village's lumber does not revert its clay),
    and the plan reports the deviation as :class:`RoleDeviationResponse` so the
    grid can mark the cell rather than silently showing a figure that differs
    from the profile the operator believes is running.
    """

    allocations: dict[Resource, AllocationInput] = Field(
        default={},
        description=(
            "What a village of this role must end up holding per hour, per "
            "resource -- the same target an explicit per-village allocation "
            "sets, so a role is a way of saying it once rather than a second "
            "kind of number. Resources absent here fall through to whatever the "
            "village itself declares, and to KEEP if neither does. "
            "REMAINDER IS REFUSED: exactly one village per resource absorbs the "
            "slack, so a profile shared by four villages cannot say which -- set "
            "it per village instead."
        ),
    )

    @field_validator("allocations")
    @classmethod
    def _remainder_is_per_village(
        cls, value: dict[Resource, AllocationInput]
    ) -> dict[Resource, AllocationInput]:
        """Exactly one village per resource absorbs the slack, so a shared
        profile cannot say which.

        Refused at the schema, like the crop spend above and for the same
        reason: ONE rule covers `/plan`, `/day-check`, `/execute` and
        `/night-profile`, and the error's own location names the role.

        Left to resolution, a template's remainder fans out to every village of
        the role and the allocation layer refuses the plan with a 400 naming
        VILLAGES -- "got 02, 11, 13, 17, 19" for a single mistyped template.
        The operator reads five bad cells and has to work back to the one
        profile that wrote them.
        """
        fanned = sorted(
            resource.value
            for resource, alloc in value.items()
            if alloc.mode is AllocationMode.REMAINDER
        )
        if fanned:
            raise ValueError(
                "a role template's allocations cannot use remainder ("
                + ", ".join(fanned)
                + "): remainder stays per village. Exactly one village per "
                "resource absorbs the slack, and a profile shared by four "
                "villages cannot say which one -- set it on the village itself "
                "with the Rest radio. Every other mode is a figure each village "
                "of the role can hold independently, which is what makes it "
                "shareable."
            )
        return value

    consumption: dict[Resource, float] = Field(
        default={},
        description=(
            "What a village of this role SPENDS per hour -- LUMBER, CLAY and "
            "IRON only. Section 2 calls its figures consumption targets, so the "
            "spend travels with the profile: a template carrying only the "
            "retention would leave the operator restating the same numbers "
            "under a second heading. "
            "CROP IS REFUSED for the reason `VillageConfig.consumption_per_hour` "
            "refuses it -- the snapshot's `crop_per_hour` is already net of "
            "upkeep, so a declared crop spend subtracts the same troops twice. "
            "Say what a village of this role should KEEP of its crop with the "
            "template's crop ALLOCATION instead: an absolute target is retention "
            "above break-even, so 0 holds a crop-negative village level."
        ),
    )

    @field_validator("consumption")
    @classmethod
    def _consumption_is_materials_only(cls, value: dict[Resource, float]) -> dict[Resource, float]:
        """The P1 ruling, restated here because a template is a second door.

        Refused at the schema rather than during resolution so that ONE rule
        covers every planning path: `/plan`, `/day-check`, `/execute` and
        `/night-profile` all carry this model. A check further in would have to
        be repeated in each of the four, which is exactly how `/night-profile`
        came to ignore the per-village field altogether.
        """
        if Resource.CROP in value:
            raise ValueError(
                "a role template's consumption cannot include crop: the "
                "snapshot's crop_per_hour is already net of troop upkeep, so a "
                "declared crop spend subtracts the same troops twice and hides a "
                "real overflow. Declare lumber, clay and iron only, and say what "
                "villages of this role should keep of their crop with the "
                "template's crop allocation instead -- an absolute target is "
                "retention, so 0 holds a crop-negative village level."
            )
        return value

    may_relay: bool | None = Field(
        default=None,
        description=(
            "Whether villages of this role may forward someone else's cargo. "
            "None takes the role's own answer, which is what profile section 5.9 "
            "says: a feeder may relay and no other role may, the capital "
            "included -- section 5 makes it the hub every feeder ships to AND "
            "draws the onward relays from its neighbour set, so it hands off "
            "rather than carrying a leg in transit. Set it only for the account "
            "whose defensive village sits on the only road to a corner of the "
            "map; unset, the role speaks for itself."
        ),
    )
    assumed_crop_per_hour: float | None = Field(
        default=None,
        description=(
            "What a village of this role is BELIEVED to net in crop per hour -- "
            "the operator's own reading, kept as a flat constant beside the rest "
            "of the profile. Section 9: 'Consumption profiles are flat "
            "constants. Drift is expected between manual updates. Flag any "
            "village whose actual net crop deviates >20% from its assumed "
            "profile.' This is that assumption, and the check compares it "
            "against the snapshot's `crop_per_hour` -- which is already net of "
            "troop upkeep, so the two are the same quantity. "
            "NOT AN INSTRUCTION. It moves no target, no cargo and no merchant: "
            "what a village should KEEP of its crop is the template's crop "
            "ALLOCATION, and what it spends is refused outright (a declared "
            "crop spend would subtract the same troops twice). The only thing "
            "this figure can do is raise a `crop_profile_drift` WARNING when "
            "reality has moved more than 20% away from it. "
            "MAY BE NEGATIVE, and usually is on the roles that matter: 01 reads "
            "-5,880/h and is crop-negative BY DESIGN, so -5,880 is the right "
            "value to record for it and a village sitting on its own figure "
            "stays silent however deep the deficit. 0.0 is a real claim -- "
            "'this village breaks even' -- and is checked as one. "
            "None means no assumption, which is not an assumption of zero: the "
            "village is simply not checked, because reading a missing figure as "
            "0/h would flag every village on every account that has never typed "
            "one."
        ),
    )
    crop_negative_by_design: bool = Field(
        default=False,
        description=(
            "This role's villages eat more crop than they grow, on purpose "
            "(sections 9.1-9.2: the Hammer and the troops-only village). Their "
            "granary countdown is then reported as a NOTE rather than a "
            "CRITICAL -- the same rate and the same hours of cover, without the "
            "claim that something has gone wrong. A downgrade and never a "
            "suppression: the hours say how long the granary lasts if the "
            "deliveries stop, which is the one figure worth acting on either "
            "way."
        ),
    )


class VillageConfig(BaseModel):
    """Operator-owned state the game will not tell us."""

    village_id: int
    role: Role | None = Field(
        default=None,
        description=(
            "What this village is FOR (profile section 1). Operator-owned: "
            "nothing in the game says a village is the Hammer. Its "
            "`PlanRequest.roles` template then supplies this village's "
            "allocations and spend for everything it does not state itself, and "
            "the role decides whether it may relay and how loud a designed crop "
            "deficit is. A role named here with no template in `roles` is "
            "refused rather than ignored: ignoring it plans four defensive "
            "villages as keeping their own production, which is a tenth of what "
            "they need, and calls it feasible. None means nothing declared, "
            "which plans exactly as before."
        ),
    )
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
    max_busy_merchants: int | None = Field(
        default=None,
        ge=0,
        description=(
            "The most merchants this village may have underway or returning at "
            "any instant (profile section 5: 'maximum 8 busy at 02', with the "
            "relay leg counting inside the 8). Operator-owned: nothing in the "
            "game states a ceiling. "
            "It is measured in the unit the plan already commits merchants in — "
            "section 8's merchants-per-send × sets-in-flight — so a route eight "
            "fields away on a 1h cycle with a 2h round trip bills 2 sets, and "
            "both sets count. "
            "A CAP, not a reserve. `merchant_reserve` holds N merchants back at "
            "EVERY village, so reaching 8 busy at one village that way costs "
            "every other village the same 12 — and the two are not even the same "
            "number off a full fleet: 19 merchants less a reserve of 12 is 7, "
            "where the cap says 8. "
            "The fleet still applies underneath: the budget is the tighter of "
            "this and merchants_total − merchant_reserve, so a cap at or above "
            "the fleet changes nothing and is not a promise of merchants the "
            "village does not have. A cap ABOVE `merchants_total` is refused "
            "rather than clamped — a ceiling the village cannot reach is a "
            "data-entry error, and clamping it silently would leave the "
            "operator's figure and the plan describing different accounts. "
            "`merchant_headroom` applies to this figure as it does to any "
            "merchant budget, so a cap of 8 is a soft target of 7: a cap set "
            "to exactly what the plan wants comes back feasible AND reported "
            "as crowded. That second line is the headroom's, not a "
            "contradiction -- feasibility is decided against the cap itself, "
            "which is why the over-budget advice names the figure that clears "
            "the cap rather than one that also clears the headroom. Set "
            "`merchant_headroom` to 0 if you want the cap packed tight. "
            "0 does NOT withdraw the village from the plan. This budget is "
            "soft, as every merchant budget here is: its routes are still "
            "built and costed, and every one of them becomes a budget breach "
            "-- which is what marks the village `over_budget`, refuses the "
            "sheet and blocks `/execute`. Use `ship_only_to` to stop a village "
            "shipping. None means no ceiling declared, which plans exactly as "
            "before."
        ),
    )
    stock_floor_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=0.95,
        description=(
            "Fraction of warehouse capacity this village keeps stocked by NPC "
            "trading — LUMBER, CLAY and IRON only, never crop, because a "
            "granary is not NPC-fed. "
            "A BUFFER LEVEL and nothing else: `fraction x warehouse_capacity`, "
            "in resources. It is NOT a supply rate and is never divided by the "
            "window. That was the previous model and it made a shorter window "
            "RAISE the claim — 30% of a 1,200,000 warehouse read as 22,500/h "
            "over a 16-hour day and 45,000/h over an 8-hour night, off the same "
            "warehouse — so a night profile funded routes from stock a day "
            "profile could not. "
            "What the floor DOES is two things. It is the level a departure may "
            "be topped back up to by NPC conversion, and it is this account's "
            "reading of section 7's 'wood is low' trigger: at or below it, the "
            "buffer these routes ship out of is gone. "
            "How much conversion that top-up can draw on is derived, not "
            "declared: it is what the village RETAINS per hour of the resources "
            "it is not shipping — clay and crop at the hub — because NPC "
            "exchanges 1:1 inside one village and cannot create resources. So "
            "the allowance is a rate built from rates and no window length can "
            "move it. "
            "Requires `npc_attended` on the request (or on each segment) "
            "whenever a window is given: the operator sleeps through the night, "
            "and a guessed default would fund night routes from trading nobody "
            "is doing. "
            "0.0 is the same as None — no floor at all, nothing about NPC "
            "applies, and no attendance declaration is needed."
        ),
    )
    npc_feedstock: list[Resource] | None = Field(
        default=None,
        description=(
            "Which of this village's stores NPC may convert FROM, overriding "
            "the derivation. Left None the feedstock is everything the village "
            "is not drawing on, which is the honest default and what section 7 "
            "describes for 02 (clay and crop into wood). "
            "Naming a resource the village is already shipping beyond its own "
            "production is refused rather than trimmed: NPC exchanges one "
            "resource for another and cannot convert a resource into itself. "
            "Only meaningful alongside `stock_floor_fraction`."
        ),
    )
    consumption_per_hour: dict[Resource, float] | None = Field(
        default=None,
        description=(
            "What this village SPENDS per hour — LUMBER, CLAY and IRON only, "
            "the building queue and the troop upkeep, entered as flat constants "
            "and kept up to date by hand. "
            "CROP IS REFUSED: `crop_per_hour` in the snapshot is already NET of "
            "upkeep (it is derived from the village's own crop balance, not from "
            "the gross statistics column), so a declared crop spend subtracts "
            "the same troops a second time. Materials are the opposite case — "
            "the statistics page reports them GROSS, so a village burning lumber "
            "still reads positive and nothing in the game states the spend. To "
            "say what a village should KEEP of its crop, set its crop allocation "
            "target: an absolute target is retention above break-even, so 0 "
            "holds a crop-negative village level and a positive figure lets it "
            "accumulate. "
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

    @field_validator("consumption_per_hour")
    @classmethod
    def _consumption_is_materials_only(
        cls, value: dict[Resource, float] | None
    ) -> dict[Resource, float] | None:
        """Crop cannot be declared, because the snapshot already nets it.

        Refused here rather than in `_resolve_roles` so that ONE rule
        covers every planning path: `/plan`, `/day-check`, `/execute` and
        `/night-profile` all carry this model, and a check further in would
        have to be repeated in each of the four -- which is exactly how
        `/night-profile` came to ignore the field altogether.

        Not clamped and not trimmed. A crop figure in a profile means the
        operator believes it is being applied, and on the account that prompted
        this it deleted a real 204,456/day overflow at village 01 by
        double-counting the same troops.
        """
        if value and Resource.CROP in value:
            raise ValueError(
                "consumption_per_hour cannot include crop: the snapshot's "
                "crop_per_hour is already net of troop upkeep, so a declared "
                "crop spend subtracts the same troops twice and hides a real "
                "overflow. Declare lumber, clay and iron only (the statistics "
                "page reports those gross), and say what the village should "
                "keep of its crop with its crop allocation target instead -- an "
                "absolute target is retention, so 0 holds a crop-negative "
                "village level."
            )
        return value

    may_relay: bool | None = Field(
        default=None,
        description=(
            "Whether THIS village may forward someone else's cargo, overriding "
            "its role's template per village. None takes the template's answer, "
            "and then the role's own (`default_may_relay`). "
            "Per village because the case is singular: the account whose "
            "DEFENSIVE village sits on the only road to a corner of the map wants "
            "that ONE village relaying, not all four of them -- and taking it out "
            "of the role to say so would cost it the profile's four targets and "
            "its spend as well. A village with no role may be told too: the "
            "alternative is a field accepted and silently ignored on the one "
            "village kind that has no template to fall back on."
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
    relay_for: list[int] | None = Field(
        default=None,
        description=(
            "Villages this one FORWARDS the capital's lumber, clay and iron on to "
            "(profile section 5's relay tier). Operator-owned: nothing in the "
            "game states a tier, and section 5 does not ask the planner to find "
            "one -- it says one exists, and says where it may be drawn from. "
            "What it buys: 02 holds the reserved wood and may only reach its own "
            "neighbours, so with a `ship_only_to` on 02 the defensive villages "
            "beyond it are simply unreachable and the plan comes back infeasible "
            "with a shortfall each. Naming a neighbour as their relay gives the "
            "planner the two legs it cannot invent — 02 → this village sized to "
            "the sum of these villages' unmet demand, and this village → each of "
            "them sized to that village's own gap. "
            "MATERIALS ONLY. Crop already relays through a sub-hub wherever the "
            "route search finds it worth doing (`max_relay_hops`), and a second, "
            "declared mechanism for the same resource would be two answers to one "
            "question. Whether a village may be conscripted as a crop hub is "
            "`may_relay`, which is a different field answering a different "
            "question — this one is an instruction, that one is a permission. "
            "ONE HOP. A village named here may not itself be a relay, and the "
            "plan is refused rather than truncated: a chain puts one hub's "
            "forward leg behind another's, which no daily beat can order. "
            "NOT A ROLE VILLAGE (section 5.9). The capital, the Hammer, the "
            "troops village and the defensive villages are refused with their "
            "role named; a feeder or a village with no role declared may relay. "
            "The relay's merchants for the COLLECTING leg are billed to the "
            "village that sends it, so at 02 they count inside its "
            "`max_busy_merchants` — section 5's 'the relay leg counts inside the "
            "8'. Its warehouse must also hold the pass-through between "
            "collecting and forwarding, which the plan checks and reports as "
            "relay_buffer. "
            "`/night-profile` validates this and derives no tier from it, and "
            "that is not an oversight: it derives TARGETS -- what each village "
            "must end up holding -- and a relay holds nothing it forwards, so a "
            "routing instruction cannot move a single figure it produces. The "
            "tier appears when those targets are planned, on `/plan`, "
            "`/day-check` or `/execute`. "
            "None means no tier declared, which plans exactly as before. An "
            "EMPTY list is refused rather than read as None: unlike "
            "`ship_only_to`, where an empty list is the real answer 'ships to "
            "nobody', there is no reading of 'forwards to nobody' that differs "
            "from leaving the field off, so accepting it would let a half-typed "
            "row look like a decision."
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
    roles: dict[Role, RoleTemplate] = Field(
        default={},
        description=(
            "One profile per role, applied to every village that declares it "
            "(profile section 2.1: one profile, four defensive villages). Keyed "
            "by the role, so a template cannot exist for a name that is not one "
            "of the five. Empty means nothing declared, which plans exactly as "
            "before -- and a template no village claims is harmless, so a setup "
            "file may carry the whole account's profiles whatever this snapshot "
            "happens to contain."
        ),
    )
    # resource -> village_id -> allocation
    allocations: dict[Resource, dict[int, AllocationInput]] = {}
    merchant_base_capacity: int = Field(default=EUROPE2_TEUTON.base_capacity, gt=0)
    trade_office_bonus_per_level: float = Field(
        default=EUROPE2_TEUTON.bonus_per_trade_office_level, ge=0
    )
    merchant_reserve: int = Field(
        default=2,
        ge=0,
        le=20,
        description=(
            "Merchants held idle at EVERY village, so a shipment can be sent by "
            "hand without waiting for a route home. Bounded by the 20 a village "
            "can ever hold, the same ceiling `max_busy_merchants` is checked "
            "against: a reserve past it holds back merchants no village has, "
            "which took every budget to 0 and every village over budget while "
            "the request still read as valid. To hold ONE village down, cap it "
            "with `max_busy_merchants` instead -- this costs every village the "
            "same merchants."
        ),
    )
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

    @model_validator(mode="after")
    def _merchant_caps_are_reachable(self) -> "PlanRequest":
        """A merchant ceiling above the village's own fleet is a typo.

        Cross-checked against the snapshot because the bound is per village and
        `VillageConfig` cannot see it. Refused rather than clamped: clamped, "02
        may run 30 busy" is accepted and planned as 18, so the operator's file
        and the plan describe different accounts with nothing saying which is
        being obeyed. Naming the village is the whole of the message's value --
        the figure is one cell in a 26-row table.

        Here rather than in a handler so ONE rule covers all four planning paths
        (`/plan`, `/day-check`, `/execute`, `/night-profile` all carry this
        model), the same reason the crop spend and the template's remainder are
        refused at the schema. Repeated per handler it would be forgotten by
        one, which is exactly how `/night-profile` came to ignore a declared
        spend.

        Skipped on an empty snapshot: there is nothing to check against, and the
        handlers answer that with "fetch account state first", which is the more
        useful of the two things to say.

        And skipped per village on a merchant count of 0, which is what
        `/snapshot` writes when it could not READ one -- it warns about those
        villages by name rather than claiming they have no merchants. Read here
        as a fleet, one failed parse refused every cap on that village from all
        four endpoints, with a message about training merchants, over a plan
        that runs identically without the cap: the budget is already 0 either
        way. Unknown is not zero.
        """
        if not self.snapshot:
            return self
        fleets = {v.village_id: v.merchants_total for v in self.snapshot}
        names = {v.village_id: v.name for v in self.snapshot if v.name}
        unreachable: list[str] = []
        unknown: list[int] = []
        for entry in self.config:
            if entry.max_busy_merchants is None:
                continue
            if entry.village_id not in fleets:
                unknown.append(entry.village_id)
                continue
            fleet = fleets[entry.village_id]
            if fleet == 0:
                continue
            if entry.max_busy_merchants > fleet:
                unreachable.append(
                    f"{village_label(entry.village_id, names)} is capped at "
                    f"{entry.max_busy_merchants} busy merchants but fields {fleet}"
                )
        if unknown:
            raise ValueError(
                "a merchant cap was set for "
                + ", ".join(f"village {vid}" for vid in sorted(unknown))
                + ", which the snapshot does not contain. Clear the cap, or fetch "
                "fresh state if the village was settled after the snapshot."
            )
        if unreachable:
            raise ValueError(
                "; ".join(unreachable)
                + ". A ceiling the village cannot reach plans as its fleet, so the "
                "figure on screen would not be the one in force -- correct the cap, "
                "or fetch fresh state if merchants have been trained since."
            )
        return self

    @model_validator(mode="after")
    def _relay_tier_is_one_hop_of_non_role_villages(self) -> "PlanRequest":
        """Profile section 5's tier, refused wherever it cannot mean anything.

        Six ways a ``relay_for`` list is not a declaration, and each is refused
        with the villages named rather than dropped. A relay silently ignored is
        the worst of the available outcomes: the operator reads a tier on their
        screen while the plan reports the villages beyond it as unreachable, and
        nothing connects the two.

        1. **A downstream the snapshot does not contain.** A typo, or a chiefed
           village. Same refusal `ship_only_to` and `consumption_per_hour` get.
        2. **A relay relaying for itself.** There is no leg to build.
        3. **A role village as the relay** (section 5.9). The role is NAMED,
           because "18 may not relay" is unanswerable and "18 is your Hammer, so
           it may not relay" says what to change. Resolved through the same
           ``role`` the rest of the plan reads, so a village takes its declared
           role and nothing else. A feeder may relay; so may a village with no
           role, which is most accounts.
        4. **A relay feeding a relay.** One hop only, and BOTH villages are
           named: the fix is to move a downstream, and neither half of the pair
           identifies which on its own. It is the same refusal the crop side
           makes in ``_crop_shape_ok`` and for the same reason -- a chain puts
           one hub's forward leg behind another's, and the beat's
           collect-then-ship ordering cannot satisfy both.
        5. **A downstream named twice in one list.** A duplicate is one
           downstream, and the tier sizes itself from the sum of its
           downstreams' gaps: named twice, a village contributes its gap twice,
           the collecting leg is drawn that much bigger, and the forward loop
           hands it its whole target once for every mention. Measured on the
           relay-tier fixture: 16,744/h landed against an 8,372/h target while
           the downstream the duplicate displaced was reported unreachable --
           and reported it with the WHITELIST as the reason, so nothing on the
           sheet pointed at the duplicate.
        6. **One downstream claimed by two relays.** The same over-ship from the
           other direction, and BOTH relays are named for the same reason the
           second hop names both: neither list is wrong on its own, so neither
           identifies which one to edit.

        Rules 5 and 6 are refused here as well as conserved in the solver
        (``_relay_tier_flows`` decrements each gap as it forwards). Belt and
        braces, and the two do different jobs: the solver keeps a duplicate from
        destroying resources, and this keeps the operator from believing a tier
        they typed twice is a tier twice the size.

        Here rather than in a handler so ONE rule covers all four planning paths,
        exactly as the merchant cap above and the crop spend do.

        `may_relay` is deliberately NOT consulted. It answers a different
        question -- may the route search conscript this village as a CROP hub --
        and reading it here would make a permission about one mechanism silently
        veto an instruction about another. Section 5.9's rule is about the role,
        so the role is what is checked.
        """
        declared = {
            entry.village_id: entry.relay_for
            for entry in self.config
            if entry.relay_for is not None
        }
        if not declared:
            return self
        names = {v.village_id: v.name for v in self.snapshot if v.name}
        known = {v.village_id for v in self.snapshot}
        roles = {entry.village_id: entry.role for entry in self.config if entry.role is not None}
        problems: list[str] = []
        for relay in sorted(declared):
            label = village_label(relay, names)
            if not declared[relay]:
                # Refused here rather than with `min_length=1` on the field so
                # the message names the VILLAGE. Pydantic's own wording gives
                # the config index -- "config.6.relay_for" -- which is not a
                # thing the operator can find in a 26-row table.
                problems.append(
                    f"{label} is declared as a relay for nobody. Unlike ship_only_to, where "
                    f"an empty list means 'ships to nobody', there is no reading of "
                    f"'forwards to nobody' that differs from leaving the field off"
                )
                continue
            missing = sorted(vid for vid in declared[relay] if self.snapshot and vid not in known)
            if missing:
                problems.append(
                    f"{label} is declared as the relay for "
                    + ", ".join(f"village {vid}" for vid in missing)
                    + ", which the snapshot does not contain"
                )
            if relay in declared[relay]:
                problems.append(f"{label} is declared as its own relay, which is not a leg")
            role = roles.get(relay)
            if role is not None and role is not Role.FEEDER:
                problems.append(
                    f"{label} is declared as a relay but its role is {role.value}, and "
                    f"profile section 5.9 says role villages may not relay -- only a "
                    f"feeder, or a village with no role declared, may"
                )
            second_hop = sorted(vid for vid in declared[relay] if vid in declared and vid != relay)
            if second_hop:
                problems.append(
                    f"{label} is declared as the relay for "
                    + ", ".join(f"{village_label(vid, names)}" for vid in second_hop)
                    + ", which is itself a declared relay -- a relay may not feed a relay"
                )
            twice = sorted({vid for vid in declared[relay] if declared[relay].count(vid) > 1})
            if twice:
                problems.append(
                    f"{label} names "
                    + ", ".join(village_label(vid, names) for vid in twice)
                    + " more than once in its relay_for. A duplicate is one downstream, and "
                    "the tier draws its collecting leg from the sum of the gaps it forwards "
                    "-- so the village would be shipped its whole target once per mention "
                    "and another downstream would go without"
                )
        # Across the lists rather than inside one, so this runs after them: two
        # relays each naming the same village is the same over-ship, and neither
        # list is wrong on its own.
        claimed: dict[int, list[int]] = {}
        for relay in sorted(declared):
            for vid in dict.fromkeys(declared[relay]):
                if vid != relay:
                    claimed.setdefault(vid, []).append(relay)
        for vid, owners in sorted(claimed.items()):
            if len(owners) > 1:
                problems.append(
                    f"{village_label(vid, names)} is declared as a downstream of "
                    + " and ".join(village_label(owner, names) for owner in owners)
                    + ", and each relay sizes its legs from the whole of that village's gap "
                    "-- so it would be shipped its target twice while their other "
                    "downstreams go without. One relay per downstream"
                )
        if problems:
            raise ValueError(
                "; ".join(problems)
                + ". The tier is one hop from the village holding the material to the "
                "village that needs it, drawn from villages that are not role villages "
                "(profile section 5). Correct relay_for, or clear it and let those "
                "villages be reported as unreachable."
            )
        return self

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
    overnight: bool | None = Field(
        default=None,
        description=(
            "Whether this route set's hours are the ones the operator sleeps "
            "through, so section 6's rules govern them: no latency target, and "
            "every merchant home before `dispatch_window` closes. Left out it "
            "is DERIVED from the window wrapping past midnight, which is right "
            "for a night stated as one 23:00-07:00 window. Send it explicitly "
            "for the half of a night SPLIT at midnight (`[0, 420]` wraps in "
            "neither direction yet carries the whole deadline) and for a "
            "near-24h day profile (`[420, 419]` wraps and is not the night). "
            "Requires `dispatch_window`: a set with no window runs round the "
            "clock and has no switch to be ready for. On /day-check and "
            "whole-day /execute this lives on each entry in `segments`, since "
            "that is where the hours are."
        ),
    )
    npc_attended: bool | None = Field(
        default=None,
        description=(
            "Whether the operator is at the marketplace during these hours, so "
            "section 7's NPC conversion can actually be performed. "
            "REQUIRED — 422 without it — whenever any village in `config` has a "
            "`stock_floor_fraction` above 0 AND `dispatch_window` is set. Not "
            "defaulted, deliberately: this account sleeps through the night "
            "window, and a guessed 'attended' would fund night routes from "
            "trading nobody is doing, which is the plan promising cargo that "
            "does not exist. "
            "False means the conversion allowance is zero for this profile — "
            "the crop keeps growing, but nobody is converting it — so a village "
            "asked to ship beyond its production comes back "
            "NPC_CAPACITY_SHORT and the plan is refused rather than quietly "
            "under-delivering. "
            "With no `dispatch_window` this may be omitted, and is then taken "
            "as UNATTENDED: a route set with no window runs round the clock, "
            "which is all 24 hours including the eight the operator sleeps "
            "through, and Travian offers nothing to confine a repeat interval "
            "to part of the day. Assuming conversion nobody performed would "
            "over-commit; assuming none under-delivers and says so through "
            "NPC_CAPACITY_SHORT. On /day-check attendance lives on each entry "
            "in `segments` instead, since that is where the hours are."
        ),
    )

    @model_validator(mode="after")
    def _npc_attendance_is_stated(self) -> "PlanRequest":
        """A floor plus a window has to say whether anyone is trading.

        Here rather than in the handler so that ONE rule covers every planning
        path -- /plan, /execute and /night-profile all carry this model. The
        day check overrides it: its windows live on `segments`, and each
        segment answers for its own hours.

        0.0 is not a floor (`0.0 is None` at every layer), so a village whose
        fraction is zero declares nothing and asks nothing of the operator.

        A request with no window is not asked, because a setup DOCUMENT is
        validated through this model and carries no window at all -- windows
        live on its profiles. Demanding an answer there refuses to save a
        perfectly good setup. Instead the round-the-clock case is resolved the
        honest way where the policy is built: unattended, not attended. See
        `_npc_policy_for`.
        """
        if self.npc_attended is not None or self.dispatch_window is None:
            return self
        floored = sorted(
            cfg.village_id
            for cfg in self.config
            if cfg.stock_floor_fraction is not None and cfg.stock_floor_fraction > 0.0
        )
        if floored:
            raise ValueError(
                "npc_attended is required: "
                + ", ".join(f"village {vid}" for vid in floored)
                + " has a stock floor, so whether the operator is awake to do the "
                "NPC trading decides whether those routes are funded at all. Send "
                "npc_attended=true for the day profile and false for the night "
                "one. A plan with no window is not exempt: it runs round the "
                "clock, which includes the hours nobody is at the Marketplace."
            )
        return self

    @model_validator(mode="after")
    def _overnight_needs_hours_to_be_overnight(self) -> "PlanRequest":
        """A declared night must say which hours it is.

        Refused rather than ignored: section 6's deadline is measured against
        the window's END, so a declaration with no window decides nothing and
        silently answers the opposite of what the client asked for. On
        /day-check and whole-day /execute the hours live on `segments`, and so
        does the declaration -- a top-level one there is refused by this same
        rule, because those endpoints refuse a top-level `dispatch_window`.
        """
        if self.overnight is not None and self.dispatch_window is None:
            raise ValueError(
                "overnight requires dispatch_window: a route set with no window "
                "runs round the clock, which has no switch for its merchants to "
                "be home for. Give the profile its hours, or put the declaration "
                "on the entry in `segments` that has them."
            )
        return self

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
    spare: int = Field(
        description=(
            "The budget this plan was built to: `merchants_total − "
            "merchant_reserve`, or the village's own `max_busy_merchants` where "
            "that is lower. The tighter of the two, because reporting room the "
            "optimizer was forbidden to use would make `free` a number nothing "
            "can be spent on."
        )
    )
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


class VillageNetResponse(BaseModel):
    """What one village's store does per hour for one resource, once the plan runs.

    Every figure here is read straight off :class:`VillageAllocation`, whose
    ``net_per_hour`` had no production reader at all -- the grid recomputed
    ``target - consumption`` in JavaScript instead. Two implementations of one
    formula drift, and these two already could: the planner drops a declared
    spend whose rate it cannot read, while the page still holds the figure the
    operator typed, so the page would show a net the plan never used.

    Exposed rather than dropped because P2 (role templates) and P6 (the morning
    fill floor) both need the net server-side, and because a UI that reads it
    cannot disagree with the plan it is displaying.
    """

    village_id: int
    resource: Resource
    own_per_hour: float
    """The village's own production, as the snapshot reported it."""
    npc_allowance_per_hour: float
    """Most this village COULD convert into this resource by NPC (section 7).

    A ceiling, not a supply: it is what the village retains of the resources it
    is not shipping, and nothing is obliged to use any of it. Zero without a
    declared `stock_floor_fraction`, and zero when `npc_attended` is false."""
    npc_draw_per_hour: float
    """How much of that allowance this plan actually spends. Never an addend.

    Consumed only against unmet demand, so a floor on a village that needs
    nothing reads zero here."""
    target_per_hour: float
    """What must be HERE: own production, plus any NPC draw, plus what is shipped in."""
    ship_per_hour: float
    """The cargo: what must arrive (positive) or leave (negative).

    `target - own - npc_draw`. The draw is subtracted, so a village funding its
    own demand by conversion has less shipped to it, not more."""
    consumption_per_hour: float
    """What the village SPENDS, from `VillageConfig.consumption_per_hour`."""
    net_per_hour: float
    """target - consumption: the rate the STORE moves at. Zero is level.

    Does NOT subtract what the NPC conversion takes out of a feedstock store --
    that debit belongs to the store being converted FROM, and `npc_triggers`
    carries the figure with it already applied."""


class RoleDeviationResponse(BaseModel):
    """One cell where a village was given a target its role's template did not.

    A template is a default and overriding it is legitimate -- one of four
    defensive villages always has a wall going up. What is not legitimate is
    overriding it invisibly: the operator reads the role's profile, the plan
    ships something else, and nothing on the page says which. So every override
    is named with both figures, and the allocation grid marks the cell.

    Reported by the server rather than left to the page to work out, because the
    resolution is the server's: two implementations of one merge rule drift, and
    a grid that marked the wrong cell would be worse than one that marked none.
    """

    village_id: int
    village_name: str
    role: Role
    resource: Resource
    template_allocation: AllocationInput
    """What the role said this village should hold."""
    village_allocation: AllocationInput
    """What the village was given instead, and what the plan actually used."""


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
    # Real production and NPC conversion, kept apart so neither can be read as
    # the other: the account does not PRODUCE what its operator converts by hand.
    total_npc_allowance: float = 0.0
    """Account-wide ceiling on conversion into this resource. Never spent by
    itself: a plan that needs none of it draws none of it."""
    total_npc_draw: float = 0.0
    """Account-wide conversion this plan actually spends into this resource."""
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
    """A village the plan routes a resource THROUGH, which the rows cannot show.

    The sheet lists ``V22 -> V02`` and ``V02 -> V17`` as unrelated lines, so
    without this the operator has no way to know the second row is carrying what
    the first one delivered -- or that the delivery takes both legs' waits.

    Keyed on the hub AND the resource, never on a path: the cargo is pooled in
    the hub's store, so which origin's crop reaches which destination is not
    something the plan decided, and reporting every combination as a delivery
    would invent it (6 real hubs became 41 claimed paths on one audited
    account). The resource is part of the key because one village can be a crop
    sub-hub the search found and a declared material relay at the same time --
    two pools of cargo, two waits.
    """

    hub: int
    hub_name: str
    resource: Resource = Field(
        default=Resource.CROP,
        description=(
            "What is being relayed. Crop reaches a hub by SEARCH (the optimizer "
            "reroutes a crop flow through a sub-hub wherever it pays); a "
            "material reaches one only where the operator declared the village "
            "with `relay_for` (profile section 5's tier). Defaulted to crop, "
            "which is what every relay was before that tier existed."
        ),
    )
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


class NpcReserveResponse(BaseModel):
    """One village's NPC balancing capacity, as the two-pass solve sized it.

    Every figure here is derived, not declared: the operator states a
    `stock_floor_fraction` and an `npc_attended`, and this is what those come
    to once the first pass has said what the village retains.
    """

    village_id: int
    village_name: str = ""
    floor_level: float = Field(
        description=(
            "The buffer, in resources: `stock_floor_fraction x "
            "warehouse_capacity`. Applies to lumber, clay and iron; a granary "
            "has no floor because it is not NPC-fed. Also this account's "
            "reading of section 7's 'wood is low' trigger."
        )
    )
    allowance_per_day: float = Field(
        description=(
            "How much this village could convert per DAY: 24 x what it retains "
            "per hour of the resources it is not shipping. Zero while "
            "`npc_attended` is false, and zero when a feedstock rate could not "
            "be read. Not a share of the warehouse, and not divided by the "
            "window -- a rate built from rates."
        )
    )
    allowance_per_hour: float
    feedstock: list[Resource] = Field(
        default=[],
        description=(
            "Which stores pay for the conversion, 1:1. Derived as everything "
            "the village is not drawing on unless `npc_feedstock` overrode it."
        ),
    )
    feedstock_shares: list[float] = Field(
        default=[],
        description=(
            "Each feedstock store's share of every conversion, parallel to "
            "`feedstock` and summing to 1. Proportional to the retention that "
            "sized the allowance, so the store funding most of it is debited "
            "most."
        ),
    )
    drawn: list[Resource] = Field(
        default=[],
        description=(
            "Materials this village must ship beyond its own production, so is "
            "converting INTO. The complement of `feedstock`. Empty means the "
            "floor funded nothing in this plan, which costs the account nothing."
        ),
    )


class NpcTriggerResponse(BaseModel):
    """One of section 7's two triggers, fired. Advice, never an action.

    The planner does not press the NPC button; it says when the operator should.
    """

    village_id: int
    village_name: str = ""
    kind: TriggerKind = Field(
        description=(
            "`wood_low` -- the wood buffer is at or below the village's own "
            "floor -- or `crop_banked` -- crop past 700,000, which is feedstock "
            "standing idle."
        )
    )
    resource: Resource
    level: float = Field(description="What the store holds, or is left holding.")
    threshold: float = Field(description="What it was measured against: the floor, or 700,000.")
    projected: bool = Field(
        description=(
            "True when it is where a day of THIS PLAN leaves the store rather "
            "than where the snapshot found it."
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


class NightOverrunResponse(BaseModel):
    """A night route whose merchants are still out when the day profile starts.

    Section 6: all night movements must complete before 07:00, so the morning
    profile starts with a full merchant pool everywhere. This is every route
    that does not manage it, with the arithmetic shown rather than asserted --
    the last departure the profile makes, the round trip that follows it, and by
    how much the two overrun the window.

    Only ever populated for the overnight profile. The rule is the night's: by
    day nothing says a merchant may not be on the road at the switch.
    """

    origin: int
    origin_name: str
    destination: int
    destination_name: str
    cycle_hours: int
    last_dispatch_minute: int = Field(
        description=(
            "Minutes past midnight of the LAST departure inside the profile's "
            "hours, which is the firing the deadline binds on. A route fires "
            "24/N times a day and the others all have more room."
        )
    )
    last_dispatch_clock: str
    round_trip_minutes: float = Field(
        description=(
            "Out and back, unrounded. A merchant is committed for the whole "
            "journey -- which is what `sets_in_flight` already prices -- so a "
            "delivery landing at 06:00 from an hour out still has merchants on "
            "the road at 07:00."
        )
    )
    overrun_minutes: float = Field(
        description="How far past the window's end the last merchant gets home."
    )


class FillAtSwitchResponse(BaseModel):
    """One store's level at a profile boundary, against its capacity.

    Section 6's two state rules read the same record from either end of the
    night: 60% of both stores at 07:00 (a floor the plan must achieve), and no
    more than 25% at 23:00 (an assumption the operator establishes by hand).
    """

    village_id: int
    village_name: str
    resource: Resource
    store: str = Field(
        description=(
            "`granary` for crop, `warehouse` for a material -- the store the "
            "operator reads the bar off. Each material has its own bar against "
            "the warehouse's capacity, which is why a row is per resource "
            "rather than per store: 80% clay and 10% iron is not 60% of a "
            "warehouse, because nothing can be built with clay alone."
        )
    )
    stock: float
    capacity: int
    fill: float = Field(description="`stock / capacity`, as a fraction rather than a percentage.")


def _fill_rows(
    fills: Sequence[FillAtSwitch], names: Mapping[int, str]
) -> list[FillAtSwitchResponse]:
    return [
        FillAtSwitchResponse(
            village_id=fill.village_id,
            village_name=village_label(fill.village_id, names),
            resource=fill.resource,
            store="granary" if fill.resource is Resource.CROP else "warehouse",
            stock=fill.stock,
            capacity=fill.capacity,
            fill=fill.fraction,
        )
        for fill in fills
    ]


def _one_night_run(
    night_segments: Sequence["DaySegmentInput"],
) -> tuple["DaySegmentInput", "DaySegmentInput"] | None:
    """The declared night as ONE run: the profile that opens it and the one that
    closes it. ``None`` when the declarations do not form a single run.

    Section 6 asks two questions about "the night" -- the 25% baseline at the
    minute it opens and the 60% floor at the minute it closes -- and a night is
    legally typed as a PAIR either side of midnight, so both ends have to be
    found rather than assumed to be one segment's.

    Found by CHAINING end-to-start, which is the only reading that cannot
    depend on the order the request happens to list the segments in. Picking
    "the half no other half ends at" looks order-free and is not: two legal
    shapes make every declared-overnight profile qualify as both ends at once,
    and then `next()` decides by list position.

    * A GAP. ``(1380, 0)`` with ``(30, 420)`` leaves 00:00-00:30 to production
      alone, which is legal -- the overlap check refuses overlaps, not gaps --
      and listed the other way round the 25% baseline was read at 00:30 instead
      of 23:00.
    * A SECOND declared-overnight window. An afternoon ``(780, 840)`` marked
      overnight is also legal, and both of section 6's state rules could end up
      measured against the nap.

    Windows here are non-overlapping (validated before this runs), so no two
    can share a start or an end, and the chain therefore cannot revisit a
    segment. The length guard is belt-and-braces against that stopping being
    true.
    """
    by_start = {s.window[0]: s for s in night_segments}
    if len(by_start) != len(night_segments):
        return None
    ends = {s.window[1] for s in night_segments}
    openings = [s for s in night_segments if s.window[0] not in ends]
    if len(openings) != 1:
        return None
    chain = [openings[0]]
    while (following := by_start.get(chain[-1].window[1])) is not None:
        chain.append(following)
        if len(chain) > len(night_segments):
            return None
    if len(chain) != len(night_segments):
        return None
    return chain[0], chain[-1]


def _night_close_minute(segments: Sequence["DaySegmentInput"]) -> int | None:
    """The minute the declared night ends, or None if there is not one night.

    Section 6's completion deadline -- everything home before 07:00 -- belongs
    to the night as a whole, and the night is legally typed as a pair either
    side of midnight, so no single segment knows it. One resolver because
    /day-check and /execute must plan the same night from the same body: they
    build the same per-segment plans, and a beat phased against a different
    closing minute is a different route set.
    """
    night_segments = [s for s in segments if is_night_window(s.window, overnight=s.overnight)]
    run = _one_night_run(night_segments) if night_segments else None
    return None if run is None else run[1].window[1]


def _night_overrun_rows(
    beat,
    window: tuple[int, int] | None,
    names: Mapping[int, str],
    overnight: bool | None = None,
    night_end: int | None = None,
) -> list[NightOverrunResponse]:
    """Section 6's completion rule, measured on the beat that was built.

    Empty for the day profile and for a round-the-clock set, which have no
    switch to be ready for. The overrun itself comes from the same pure function
    the beat scores its placements with, so the table and the finding cannot
    disagree about a number.

    ``overnight`` is the profile's own declaration, threaded so this table and
    the beat that produced it read the same answer to "is this the night" -- a
    row here about a profile the beat scheduled under the day's rules would be
    a deadline nothing tried to meet.

    ``night_end`` travels for the same reason: the deadline is the NIGHT's
    close, and for a night split at midnight that is not this half's window
    end. Passed as the beat was given it, or the table would report an overrun
    against a minute the plan never aimed at.
    """
    if not is_night_window(window, overnight=overnight):
        return []
    rows: list[NightOverrunResponse] = []
    for scheduled in beat.routes:
        overrun = night_overrun_minutes(scheduled, window, night_end)
        last = last_night_dispatch(scheduled, window)
        if overrun <= 0 or last is None:
            continue
        rows.append(
            NightOverrunResponse(
                origin=scheduled.route.origin,
                origin_name=village_label(scheduled.route.origin, names),
                destination=scheduled.route.destination,
                destination_name=village_label(scheduled.route.destination, names),
                cycle_hours=scheduled.route.cycle_hours,
                last_dispatch_minute=last,
                last_dispatch_clock=_clock(last),
                round_trip_minutes=2.0 * scheduled.route.one_way_minutes,
                overrun_minutes=overrun,
            )
        )
    return sorted(rows, key=lambda row: (-row.overrun_minutes, row.origin, row.destination))


def _npc_reserve_rows(plan, names: Mapping[int, str]) -> list[NpcReserveResponse]:
    """Section 7's sized reserves as a table. Empty when no floor is declared."""
    return [
        NpcReserveResponse(
            village_id=vid,
            village_name=village_label(vid, names),
            floor_level=reserve.floor_level,
            allowance_per_day=reserve.allowance_per_day,
            allowance_per_hour=reserve.allowance_per_hour,
            feedstock=list(reserve.sources),
            feedstock_shares=list(reserve.shares),
            drawn=sorted(reserve.drawn, key=lambda r: r.value),
        )
        for vid, reserve in sorted(plan.npc.items())
    ]


def _npc_trigger_rows(
    triggers: tuple[NpcTrigger, ...], names: Mapping[int, str]
) -> list[NpcTriggerResponse]:
    return [
        NpcTriggerResponse(
            village_id=trigger.village_id,
            village_name=village_label(trigger.village_id, names),
            kind=trigger.kind,
            resource=trigger.resource,
            level=trigger.level,
            threshold=trigger.threshold,
            projected=trigger.projected,
        )
        for trigger in triggers
    ]


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
    role_deviations: list[RoleDeviationResponse] = Field(
        default=[],
        description=(
            "Cells where a village was given a target its role's template did "
            "not. Empty for an account that declares no roles, and empty for one "
            "whose explicit figures agree with its templates -- an account that "
            "spelled its profile out before templates existed must not light up "
            "with deviations it does not have."
        ),
    )
    village_nets: list[VillageNetResponse] = Field(
        default=[],
        description=(
            "Per village, per resource: own production, target, cargo, declared "
            "spend and the resulting net. The allocation grid reads the net from "
            "here rather than recomputing it, so the page and the plan cannot "
            "disagree about what a store does. OWN villages only -- a foreign "
            "tribute is a sink with no store, and it appears in `shortfalls` "
            "instead."
        ),
    )
    night_overruns: list[NightOverrunResponse] = Field(
        default=[],
        description=(
            "Routes whose merchants are still on the road when the night "
            "profile ends (section 6). Empty for the day profile and for a "
            "round-the-clock set, which have no switch to be ready for; empty "
            "too for a night whose road is clear, which is the point. Each row "
            "also appears in `diagnostics` as a `night_overrun` finding -- this "
            "is the same fact with the arithmetic in fields instead of prose."
        ),
    )
    npc_reserves: list[NpcReserveResponse] = Field(
        default=[],
        description=(
            "Section 7's NPC balancing, per village that declared a "
            "`stock_floor_fraction`. Empty when none did. What the operator "
            "declared is a floor and an attendance; this is what the planner "
            "made of it -- the buffer level, the conversion budget, which "
            "stores pay for it and which it converts into."
        ),
    )
    npc_triggers: list[NpcTriggerResponse] = Field(
        default=[],
        description=(
            "Section 7's two triggers, where they fired: wood at or below the "
            "village's floor, or crop past 700,000. Reports about when the "
            "operator should trade -- the planner never presses the button. "
            "Each row also appears in `diagnostics` as an `npc_wood_low` or "
            "`npc_crop_banked` finding; this is the same fact with the "
            "arithmetic in fields instead of prose."
        ),
    )
    # Every finding as prose, in producer order. Kept because it is the contract
    # the UI and the tests were built on -- but a 25-village account put 132
    # lines in here and the operator stopped reading, so `diagnostics` is what
    # the page actually renders.
    warnings: list[str]
    diagnostics: DiagnosticsResponse
    plan_digest: str = Field(
        default="",
        description=(
            "sha256 of this whole response, with this field excluded. THIS "
            "PLAN'S IDENTITY, and the thing `/plan/yaml` demands back before it "
            "will render a document: section 10's order is readable plan first, "
            "the operator confirms, and only then is the YAML generated -- so "
            "the export has to be able to tell that the plan it re-computes is "
            "still the one that was read. Over the response rather than the "
            "request, because two requests differing only in a field the "
            "planner ignores are the same plan and must digest the same. It is "
            "not stable across releases and is not meant to be: a planner "
            "change that moves a cargo figure SHOULD move the digest, since the "
            "plan the operator read no longer exists."
        ),
    )


class DaySegmentInput(BaseModel):
    """One allocation profile plus the hours of the day it actually runs."""

    name: str = Field(min_length=1)
    window: tuple[int, int]
    """Minutes past midnight (start, end); may wrap past midnight."""
    allocations: dict[Resource, dict[int, AllocationInput]] = {}
    overnight: bool | None = Field(
        default=None,
        description=(
            "Whether this profile is the one the operator sleeps through, so "
            "section 6's rules govern it: no latency target, and every merchant "
            "home before the window closes. Left out it is DERIVED from the "
            "window wrapping past midnight, which is right for a night stated "
            "as one 23:00-07:00 window and wrong twice over. Send it "
            "explicitly when the night is SPLIT at midnight -- 23:00-00:00 is "
            "`[1380, 0]` and does wrap, but `[0, 420]` is the half that runs up "
            "to the switch and wraps in neither direction -- and when a day "
            "profile covers almost the whole day (`[420, 419]` wraps and is "
            "not the night)."
        ),
    )
    npc_attended: bool | None = Field(
        default=None,
        description=(
            "Whether the operator is at the marketplace during THIS profile's "
            "hours. Required — 422 without it — on every segment as soon as any "
            "village has a `stock_floor_fraction` above 0. The night segment is "
            "the one where it is false; nothing here infers that from the "
            "window, because 'the operator is asleep' is a fact about the "
            "operator and not about the clock."
        ),
    )

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
        # Same rule /day-check applies, for the same reason: on a whole-day run
        # the hours live on the segments, so `PlanRequest`'s own attendance
        # check cannot see them. This is the endpoint that WRITES, so the one
        # place it must not be inferred is here.
        floored = sorted(
            cfg.village_id
            for cfg in self.config
            if cfg.stock_floor_fraction is not None and cfg.stock_floor_fraction > 0.0
        )
        silent = [s.name for s in self.segments if s.npc_attended is None]
        if floored and silent:
            raise ValueError(
                "npc_attended is required on every segment: "
                + ", ".join(f"village {vid}" for vid in floored)
                + " has a stock floor, so whether the operator is awake to do the "
                "NPC trading decides whether each profile's routes are funded. "
                "Missing on: " + ", ".join(silent)
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


@dataclass(frozen=True)
class _ResolvedRoles:
    """Everything a declared role decides, resolved once per request."""

    of_village: dict[int, Role]
    """Which role each village that declared one has. Villages absent from this
    are the whole of today's accounts and every rule below leaves them alone."""

    allocations: dict[Resource, dict[int, AllocationInput]]
    """The request's own allocations with each role's template filled in where
    the village said nothing. What the optimizer is actually given."""

    consumption: dict[int, dict[Resource, float]]
    """What each village spends per hour, village-major: its own figures over
    its role's, per resource. Crop never appears -- both schemas refuse it,
    because ``crop_per_hour`` is already net of upkeep."""

    may_relay: dict[int, bool | None]
    """Per village: its own answer over its role template's. ``None`` leaves the
    role's own answer to
    :func:`~services.distribution.roles.default_may_relay`, and for a village
    with no role at all leaves the crop-sign inference in place."""

    crop_negative_by_design: frozenset[int]
    """Villages whose granary countdown is a NOTE rather than a CRITICAL."""

    assumed_crop: dict[int, float]
    """What each village's role BELIEVES it nets in crop per hour (section 9).

    Only the villages whose template states a figure. A village absent here has
    no assumption and cannot drift -- deliberately not defaulted to 0.0, which
    is a claim of its own ("breaks even") and would flag the whole account."""

    deviations: list[RoleDeviationResponse]
    """Cells where an explicit allocation overrode a template."""


def _resolve_roles(body: PlanRequest) -> _ResolvedRoles:
    """Apply the role templates to the villages that declared a role.

    ONE reader for all four planning paths, and the only place the merge rule
    lives. `/plan`, `/day-check` and `/execute` reach it through
    ``_plan_account``; `/night-profile` calls it itself, because it does not
    share ``_plan_account`` -- which is precisely how it came to ignore a
    declared spend (R3-D2). Pure, cheap and idempotent, so a caller that needs
    two of the six fields asks twice rather than threading a tuple around.

    The merge is per RESOURCE and the village wins. Overriding a defensive
    village's lumber must not revert its clay and iron to whatever it produces,
    and taking it out of the role to change one number would lose it the relay
    rule and the designed-deficit reading along with the other three figures.
    An explicit KEEP counts as a statement, because in this module KEEP means
    "hold your own production" and that is a different answer from the
    template's -- so it overrides, and is reported as the deviation it is.

    Consumption merges the same way and separately: a village may state its own
    spend for one resource and take the rest of its role's. So does
    ``may_relay``, and per village rather than only per role because the case is
    singular -- one defensive village on the only road to a corner of the map,
    not the four the profile covers.

    A role naming a template that is not in ``roles`` is refused (422), not
    ignored. Ignored, four defensive villages revert to keeping their own
    production -- a tenth of what they need -- and the plan reads as feasible.
    """
    names = {v.village_id: v.name for v in body.snapshot if v.name}
    own_ids = {v.village_id for v in body.snapshot}
    of_village: dict[int, Role] = {}
    undeclared: list[tuple[int, Role]] = []
    unknown: list[int] = []
    for cfg in body.config:
        if cfg.role is None:
            continue
        if cfg.village_id not in own_ids:
            unknown.append(cfg.village_id)
            continue
        if cfg.role not in body.roles:
            undeclared.append((cfg.village_id, cfg.role))
            continue
        of_village[cfg.village_id] = cfg.role
    if unknown:
        # The same refusal `ship_only_to` and `consumption_per_hour` get, and
        # needed in its own right: a role carrying no spend would otherwise slip
        # past their checks entirely, and a chiefed village still holding a role
        # means the profile the operator is reading names a village the plan
        # does not contain.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "a role was declared for "
                + ", ".join(f"village {vid}" for vid in sorted(unknown))
                + ", which the snapshot does not contain. Clear the role, or fetch "
                "fresh state if the village was settled after the snapshot."
            ),
        )
    if undeclared:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "no role template was sent for "
                + ", ".join(
                    f"{village_label(vid, names)} (role {role.value})" for vid, role in undeclared
                )
                + ". A role decides that village's targets, its spend and whether it "
                "may relay, so planning without the template would plan a different "
                "account -- add the template, or clear the role."
            ),
        )

    allocations: dict[Resource, dict[int, AllocationInput]] = {
        resource: dict(per_village) for resource, per_village in body.allocations.items()
    }
    consumption: dict[int, dict[Resource, float]] = {}
    deviations: list[RoleDeviationResponse] = []
    for vid, role in sorted(of_village.items()):
        template = body.roles[role]
        for resource, allocation in template.allocations.items():
            stated = allocations.get(resource, {}).get(vid)
            if stated is None:
                allocations.setdefault(resource, {})[vid] = allocation
                continue
            if stated != allocation:
                deviations.append(
                    RoleDeviationResponse(
                        village_id=vid,
                        village_name=village_label(vid, names),
                        role=role,
                        resource=resource,
                        template_allocation=allocation,
                        village_allocation=stated,
                    )
                )
        if template.consumption:
            consumption[vid] = {
                resource: float(rate) for resource, rate in template.consumption.items()
            }
    for cfg in body.config:
        if not cfg.consumption_per_hour:
            continue
        consumption.setdefault(cfg.village_id, {}).update(
            {resource: float(rate) for resource, rate in cfg.consumption_per_hour.items()}
        )

    # The template's answer first, then the village's over it -- the same
    # village-wins merge the consumption above does, and per village for the
    # same reason. The case the override exists for is singular (one defensive
    # village on the only road to a corner of the map), so putting it on the
    # template alone handed the permission to all four.
    #
    # A village with NO role is merged in too. It has no template to fall back
    # on, so the alternative is a field accepted and silently ignored; the
    # predicate reads an explicit answer ahead of the crop-sign inference,
    # which is what the inference has always been the fallback FOR.
    may_relay: dict[int, bool | None] = {
        vid: body.roles[role].may_relay for vid, role in of_village.items()
    }
    for cfg in body.config:
        if cfg.may_relay is not None:
            may_relay[cfg.village_id] = cfg.may_relay

    return _ResolvedRoles(
        of_village=of_village,
        allocations=allocations,
        consumption=consumption,
        may_relay=may_relay,
        crop_negative_by_design=frozenset(
            vid for vid, role in of_village.items() if body.roles[role].crop_negative_by_design
        ),
        # Section 9's assumption, per village that has one. `is not None`
        # rather than a truth test: 0.0 is the claim "this village breaks
        # even", and reading it as "nothing declared" would silence the one
        # assumption a village can drift furthest from.
        assumed_crop={
            vid: body.roles[role].assumed_crop_per_hour
            for vid, role in sorted(of_village.items())
            if body.roles[role].assumed_crop_per_hour is not None
        },
        deviations=deviations,
    )


def _npc_store_deltas(plan) -> dict[int, dict[Resource, float]]:
    """What section 7's conversion does to a floored village's OWN stores, per hour.

    The NPC merchant exchanges resources INSIDE one village, so none of it is
    route cargo and none of it appears in any route's ``cargo_per_hour``: the
    drawn material gains exactly what was drawn, and the feedstock stores are
    debited that same budget, split by the reserve's shares (one budget funds
    every material the village converts into, so one debit is spread across
    them). The two together sum to zero, which is what makes NPC an exchange
    rather than a source.

    Both store checks read the figure from here, so section 7's trigger table
    and the continuous fill/drain status cannot disagree about a floored
    village's net rate. They did: the continuous check folded in NEITHER term,
    so a village drawing 12,000/h of lumber read as a warehouse draining at
    -12,000/h on a store the plan leaves exactly level, and its granary read as
    banking every unit of the crop that pays for it.

    Empty for a village with no declared floor -- no reserve, nothing converted.
    """
    deltas: dict[int, dict[Resource, float]] = {}
    for vid, reserve in plan.npc.items():
        converted = 0.0
        draws: dict[Resource, float] = {}
        for resource in Resource:
            rp = plan.resource_plans.get(resource)
            if rp is None:
                continue
            allocation = next((v for v in rp.villages if v.village_id == vid), None)
            if allocation is None:
                continue
            draws[resource] = allocation.npc_draw_per_hour
            if resource in MATERIALS:
                converted += allocation.npc_draw_per_hour
        deltas[vid] = {
            resource: draws.get(resource, 0.0) - converted * reserve.share_of(resource)
            for resource in Resource
        }
    return deltas


def _npc_store_state(
    body: PlanRequest, plan
) -> tuple[
    dict[int, dict[Resource, float]],
    dict[int, dict[Resource, float]],
    dict[int, dict[Resource, float]],
]:
    """Stocks, capacities and post-conversion net rates for the NPC triggers.

    Only the balancing villages, and only the figures section 7's two triggers
    measure: the level a store holds now, its cap, and the rate the plan leaves
    it moving at ONCE THE CONVERSION IS PAID FOR. That last subtraction is what
    makes the 700,000 crop trigger honest -- crop converted into wood is crop
    the granary no longer banks, and reading the allocation net alone would
    report it as still accumulating.
    """
    stocks: dict[int, dict[Resource, float]] = {}
    capacities: dict[int, dict[Resource, float]] = {}
    nets: dict[int, dict[Resource, float]] = {}
    deltas = _npc_store_deltas(plan)
    for village in body.snapshot:
        vid = village.village_id
        if plan.npc.get(vid) is None:
            continue
        allocations: dict[Resource, object] = {}
        for resource in Resource:
            rp = plan.resource_plans.get(resource)
            if rp is None:
                continue
            allocation = next((v for v in rp.villages if v.village_id == vid), None)
            if allocation is None:
                continue
            allocations[resource] = allocation
        for resource in Resource:
            stocks.setdefault(vid, {})[resource] = float(getattr(village, _STOCK_FIELD[resource]))
            cap = (
                village.granary_capacity
                if resource is Resource.CROP
                else village.warehouse_capacity
            )
            if cap is not None:
                capacities.setdefault(vid, {})[resource] = float(cap)
            allocation = allocations.get(resource)
            # Own production plus the cargo the plan INTENDS to move, less the
            # spend, plus what the conversion does. Written this way rather
            # than as `net_per_hour` so it is the same statement
            # `_storage_findings` makes forty lines below, with the one real
            # difference visible: this reads the intended ship rate and that
            # one reads the cargo the optimizer actually routed. Note
            # `net_per_hour` (= target - consumption) already counts the draw,
            # since `target = own + draw + ship`, which is why the draw arrives
            # here only through the shared delta.
            nets.setdefault(vid, {})[resource] = (
                allocation.own_per_hour + allocation.ship_per_hour - allocation.consumption_per_hour
                if allocation is not None
                else 0.0
            ) + deltas.get(vid, {}).get(resource, 0.0)
    return stocks, capacities, nets


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
    #
    # Read through the roles, so a village takes its role's spend where it
    # states none of its own -- the same figure the allocation layer was given,
    # from the same resolver, because a replay netting a different spend from
    # the one the plan was built with reports overflows that plan never had.
    roles = _resolve_roles(body)
    consumption = roles.consumption
    # Section 7's conversion, from the same function the trigger table reads.
    # It is not route cargo -- NPC exchanges inside one village -- so it appears
    # in neither `shipped` below nor the snapshot's own rates, and the two
    # adjacent checks disagreed about every floored village's net rate until
    # both took it from here. See `_npc_store_deltas`.
    npc_deltas = _npc_store_deltas(plan)

    # Net rate per village per resource AFTER the plan: own production plus what
    # arrives minus what leaves minus what is spent, plus what the operator
    # converts. That is what the store actually sees.
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
                + npc_deltas.get(vid, {}).get(resource, 0.0)
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
    # Section 7's reserves as the two-pass solve sized them, NOT re-derived from
    # the config here: the replay has to top stores up out of the same budget
    # the allocation layer spent, or the day picture contradicts the plan it is
    # built from. The reservoir is finite, so a departure the budget cannot
    # cover really does leave short -- which is the whole point.
    overflows = simulate_day(
        plan.beat,
        stocks,
        capacities,
        own_rates,
        dispatch_window=window,
        npc=plan.npc,
        consumption=consumption,
    )
    names = {v.village_id: v.name for v in body.snapshot if v.name}
    return list(
        storage_findings(
            statuses,
            overflows,
            names=names,
            crop_negative_by_design=roles.crop_negative_by_design,
        )
    ) + relay_buffer_findings(
        # Section 5's declared tier, checked on the SAME replay the overflow
        # findings above come from. A relay holds somebody else's cargo between
        # collecting and forwarding, and a warehouse that tops out in between
        # destroys it -- which the generic overflow line reports as the relay's
        # own problem, naming neither the tier nor the cargo's real owner. This
        # is the check that had material relay deferred once before.
        plan.relays,
        overflows,
        plan.beat,
        capacities,
        names=names,
    )


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
    *,
    max_busy: int | None,
    merchants_total: int,
    fleet_spare: int | None,
) -> str:
    """Say why the merchants ran out, in terms that suggest what to do.

    'over by 2' is true and useless. A village can overrun its merchants for
    three quite different reasons and the fix differs for each: when the trip is
    long, merchants are tied up in transit and only a shorter haul or a smaller
    load helps; when the Trade Office is low, each merchant carries little and
    the upgrade is the answer; and when the OPERATOR capped the village, the
    ceiling is theirs to move. So this names whichever dominates rather than
    stating the arithmetic back.

    That third case is why ``max_busy`` and ``fleet_spare`` are both here.
    "02 needs 16 merchants but has 8" reads as a fact about the fleet, and the
    fleet has 20 — so the operator goes looking at the Trade Office and the map
    for a number they typed themselves. The decision of which is binding is
    made here rather than at the call site, so there is one place the wording
    can be wrong.
    """
    if not legs:
        return f"{label} is over its merchant budget, but no route explains it — this is a bug."

    worst = legs[0]
    # `spare` is already the tighter of the two (see `merchant_budget`), so the
    # cap binds exactly when it is not looser than what the fleet could field.
    # Worded by the optimizer's own helper, which `blockers` also renders: the
    # refusal and the explanation are the same sentence about the same cap.
    clause = merchant_ceiling_clause(max_busy, fleet_spare)
    if clause is not None:
        parts = [f"{label} needs {committed} merchants but {clause}."]
    else:
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
    # Said last, and only where the ceiling is the operator's: it is the one fix
    # that needs no Trade Office, no re-routing and no game action at all, so
    # leaving it unsaid sends them to the expensive options first. The figure is
    # what this plan actually wants, not a round number.
    #
    # And only where the figure is one the request layer would ACCEPT. A cap
    # above `merchants_total` is refused by name -- "capped at 48 busy
    # merchants but fields 20" -- so offering it sent the operator round a
    # loop: follow the advice, get a 422. Where none fits, the haul is what has
    # to move, and the two figures are still worth stating.
    if clause is not None:
        if committed <= merchants_total:
            parts.append(f"Raising {label}'s cap to {committed} would fit this plan as it stands.")
        else:
            parts.append(
                f"No cap fits this plan: it wants {committed} merchants and {label} fields "
                f"{merchants_total}, so a cap that high would be refused. The haul from here "
                f"has to shrink."
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

    @model_validator(mode="after")
    def _every_segment_states_npc_attendance(self) -> "DayCheckRequest":
        """The hours live on the segments, so attendance does too.

        `PlanRequest`'s own rule cannot fire here -- `dispatch_window` is
        refused at the top level -- so this is the same refusal asked of the
        place that actually knows the hours. Every segment, not just the night
        one: which profile the operator sleeps through is theirs to say, and a
        rule that only asked the wrapping segment would be inferring it.
        """
        floored = sorted(
            cfg.village_id
            for cfg in self.config
            if cfg.stock_floor_fraction is not None and cfg.stock_floor_fraction > 0.0
        )
        if not floored:
            return self
        silent = [s.name for s in self.segments if s.npc_attended is None]
        if silent:
            raise ValueError(
                "npc_attended is required on every segment: "
                + ", ".join(f"village {vid}" for vid in floored)
                + " has a stock floor, so whether the operator is awake to do the "
                "NPC trading decides whether each profile's routes are funded. "
                "Missing on: " + ", ".join(silent)
            )
        return self


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
    morning_floor: float = Field(
        default=DEFAULT_TARGET_FILL,
        description=(
            "The fraction of both stores every role village must have reached "
            "at the morning switch (section 6: 60%). The same figure the night "
            "derivation uses as its ceiling, deliberately -- 'never overflow "
            "during the night, never arrive empty at morning' is one statement "
            "seen from either side."
        ),
    )
    pre_night_baseline: float = Field(
        default=DEFAULT_BASELINE_FILL,
        description=(
            "The fraction the night profile ASSUMES each store is down to at "
            "the day-to-night switch (section 6: 25%). The operator spends the "
            "stores down by hand, so this is a trusted starting condition and "
            "never a constraint the plan is refused for missing."
        ),
    )
    morning_shortfalls: list[FillAtSwitchResponse] = Field(
        default=[],
        description=(
            "Role villages -- DEF and both OFF, capital excluded -- below "
            "`morning_floor` on a store when the day profile takes over, "
            "emptiest first. Measured on a repeating day of the composite "
            "replay, so it is where the plan LEAVES them rather than where the "
            "snapshot found them."
        ),
    )
    pre_night_over_baseline: list[FillAtSwitchResponse] = Field(
        default=[],
        description=(
            "Role villages above `pre_night_baseline` on a store when the night "
            "profile takes over, fullest first. A finding and not a refusal: "
            "the manual spend-down is the operator's action, and the planner is "
            "not in the room when it happens."
        ),
    )
    night_overruns: list[NightOverrunResponse] = Field(
        default=[],
        description=(
            "Routes of the overnight profile whose merchants are still on the "
            "road at the switch (section 6). Empty when every night movement "
            "closes, and empty when no segment is an overnight one."
        ),
    )


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
            "snapshot caught goes stale within the hour. "
            "An ASSUMPTION THE OPERATOR OWNS, never a constraint this endpoint "
            "enforces: section 6's spend-down at the switch is a manual action, so "
            "a snapshot that disagrees is reported (see `warnings`, and "
            "/day-check's `pre_night_over_baseline`) and the derivation still "
            "obeys the number it was given."
        ),
    )
    target_fill: float = Field(
        default=DEFAULT_TARGET_FILL,
        gt=0.0,
        le=1.0,
        description=(
            "How full a store may be at dawn, as a fraction. Read as a CEILING "
            "here -- the room between the baseline and this is what the night has "
            "to fill -- and as section 6's morning FLOOR by /day-check, which "
            "checks every role village reached it on both stores at 07:00. One "
            "number, because 'never overflow during the night, never arrive empty "
            "at morning' is one statement seen from either side."
        ),
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
    max_busy = {
        c.village_id: c.max_busy_merchants for c in body.config if c.max_busy_merchants is not None
    }

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

    # Section 2's declared spend, which this path inherited from PlanRequest and
    # then ignored. It is the FOURTH planning path and the one that SEEDS the
    # other three -- the page posts the same payload here and writes the derived
    # allocations straight into the active profile -- so a spend dropped here is
    # a spend missing from every plan built on the night it produced.
    #
    # Refused for a village the snapshot does not contain, word for word as
    # `_plan_account` does it: this endpoint answered such a body with a
    # cheerful 200 and a profile that silently ignored the figure.
    #
    # Through the roles for the same reason: the templates carry both halves of
    # section 2's profile, and this endpoint reads the day RETENTION as well as
    # the spend, so both have to be resolved before the derivation sees them.
    roles = _resolve_roles(body)
    declared_consumption = roles.consumption
    unknown_consumers = sorted(set(declared_consumption) - {v.village_id for v in body.snapshot})
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

    villages = [
        NightVillage(
            village_id=v.village_id,
            name=v.name,
            x=v.x,
            y=v.y,
            merchants_total=v.merchants_total,
            trade_office_level=trade_office.get(v.village_id, 0),
            # Section 5's ceiling on merchants in the air. The night sizes each
            # village's export by what its fleet can actually carry in the hours
            # it has, so a capped village must be sized by the cap or it is
            # handed a retention it cannot honour -- the same defect a declared
            # spend had on this path, from the other side.
            max_busy_merchants=max_busy.get(v.village_id),
            warehouse_capacity=v.warehouse_capacity,
            granary_capacity=v.granary_capacity,
            # Net of what the village spends, MATERIALS only. A village that
            # burns its whole lumber production has none to keep overnight and
            # none to shed, and a derivation reading the gross rate hands it a
            # retention it cannot fund. Crop is never netted here because
            # `crop_per_hour` already is (the schema refuses a crop spend), so
            # subtracting one would double-count the same upkeep.
            production={
                Resource.LUMBER: (v.lumber_per_hour or 0.0)
                - declared_consumption.get(v.village_id, {}).get(Resource.LUMBER, 0.0),
                Resource.CLAY: (v.clay_per_hour or 0.0)
                - declared_consumption.get(v.village_id, {}).get(Resource.CLAY, 0.0),
                Resource.IRON: (v.iron_per_hour or 0.0)
                - declared_consumption.get(v.village_id, {}).get(Resource.IRON, 0.0),
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
        for vid, alloc in (roles.allocations.get(resource) or {}).items():
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
    for resource, per in roles.allocations.items():
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
        # The same two models `_plan_account` builds its `PlannerConfig` from,
        # so the night derivation and the plan that runs it cannot disagree
        # about a distance or about what a merchant carries.
        geometry=MapGeometry(span=body.map_span, speed_fields_per_hour=body.speed_fields_per_hour),
        merchant_model=MerchantModel(
            base_capacity=body.merchant_base_capacity,
            bonus_per_trade_office_level=body.trade_office_bonus_per_level,
        ),
        day_retention=day_retention,
        hub_id=hub,
        consumer_ids=consumers,
        tribute_per_hour=tribute,
        tribute_at=tribute_at,
        baseline_fill=body.baseline_fill,
        target_fill=body.target_fill,
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
    # Section 6's rules are the overnight profile's. Never from the NAME: a
    # profile called "Night" that runs 09:00-17:00 is not one. Each segment's
    # own `overnight` declaration decides, falling back to the window's wrap --
    # and there may be TWO of them, because the operator may type the night as
    # a pair either side of midnight.
    #
    # Resolved BEFORE anything is planned, because the completion deadline is
    # an input to the beat: it belongs to the night rather than to each half,
    # and the beat both phases against it and reports against it. A declaration
    # set that is not one continuous night has no single close, and then the
    # halves fall back to their own ends -- which is the reading section 6's
    # state rules refuse below, for the same reason.
    night_segments = [s for s in body.segments if is_night_window(s.window, overnight=s.overnight)]
    night_run = _one_night_run(night_segments) if night_segments else None
    night_close = _night_close_minute(body.segments)
    night_overruns: list[NightOverrunResponse] = []
    npc_reserves: dict[int, NpcReserve] = {}
    # Kept because the whole-day merchant boundary is a question about the SUM
    # across profiles, which no single segment's plan can answer.
    planned: list[_PlannedAccount] = []
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
        # Attendance travels with the profile's hours, so each segment's plan is
        # funded by the trading that actually happens during it. Without this
        # the night profile would be sized from the day's conversion, which is
        # the exact mis-funding `npc_attended` exists to prevent.
        per_profile = body.model_copy(
            update={"allocations": segment.allocations, "npc_attended": segment.npc_attended}
        )
        # Only the night's own halves carry a completion deadline, so only they
        # are given where it falls; `build_beat` ignores it on any other
        # profile, and passing it anyway would read as a rule the day has.
        segment_is_night = is_night_window(segment.window, overnight=segment.overnight)
        try:
            account = await _plan_account(
                per_profile,
                dispatch_window=segment.window,
                overnight=segment.overnight,
                night_end=night_close if segment_is_night else None,
            )
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=f"{segment.name}: {exc.detail}"
            ) from exc
        planned.append(account)
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

        if segment_is_night:
            # Extended, not assigned: a night split at midnight is two
            # profiles, and a merchant still on the road at 07:00 belongs to
            # whichever half dispatched it.
            night_overruns.extend(
                _night_overrun_rows(
                    account.plan.beat,
                    segment.window,
                    names,
                    overnight=segment.overnight,
                    night_end=night_close,
                )
            )

        segments.append(
            ProfileSegment(
                name=segment.name,
                start_minute=segment.window[0],
                end_minute=segment.window[1],
                routes=account.plan.beat.routes,
                manual_rates=manual,
                # None only where no village declared a floor, in which case no
                # reserve exists and this decides nothing.
                npc_attended=bool(segment.npc_attended),
            )
        )
        # Every segment sizes the same villages' reserves from its own hours, so
        # the composite takes the ATTENDED ones: a night segment's reserve is
        # zero by construction, and the replay gates accrual on the segment
        # anyway. Keeping the day's figures is what lets the replay refill during
        # the day and not overnight, off one set of reserves.
        for vid, reserve in account.plan.npc.items():
            if reserve.allowance_per_day > 0.0 or vid not in npc_reserves:
                npc_reserves[vid] = reserve
    # Unprefixed: this is not one profile's finding but the account's, about the
    # boundary BETWEEN them. Same helper /execute runs, so the endpoint the
    # operator reviews with and the endpoint that writes cannot differ on it.
    warnings.extend(_merchant_boundary_warnings(planned, body.merchant_reserve, names))
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
    # Bound once: the replay needs the declared spend and section 6's checks
    # below need the declared ROLES, and resolving twice in one handler invites
    # the two halves of one answer to be computed from different resolutions.
    roles = _resolve_roles(body)
    trajectories, breaches = await asyncio.to_thread(
        simulate_profile_cycle,
        segments,
        own_rates,
        stocks,
        capacities,
        body.crop_ceilings,
        # Role templates resolved, exactly as each segment's own plan resolved
        # them: the composite replay and the per-profile plans have to be given
        # the same spend, or the day view contradicts the plan view it is built
        # from.
        consumption=roles.consumption,
        # Section 7, threaded for the same reason: a mechanism in one replay and
        # not the other is how the two endpoints came to answer one account
        # differently. Finite here as it is there, and accruing only while an
        # attended profile runs.
        npc=npc_reserves,
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

    # ── Section 6: the state the night hands over, at both ends of it ───────
    #
    # Read off the SAME replay the breaches above come from, deliberately. "No
    # overflow, either direction, at any point in the night" is already what a
    # `capacity` and an `empty` breach say, with the profile that was running
    # attached; a second simulator to answer the fill questions would be a
    # second answer to those as well.
    #
    # Role villages only, and narrower than the relay rule's "role village":
    # section 6 names DEF and both OFF and excludes the capital, which is the
    # storage and NPC hub and is drawn down on purpose.
    floor_villages = [vid for vid, role in roles.of_village.items() if keeps_a_morning_floor(role)]
    morning_short: tuple[FillAtSwitch, ...] = ()
    pre_night_over: tuple[FillAtSwitch, ...] = ()
    if night_segments and floor_villages and night_run is None:
        # Two nights are not one night, and section 6's rules are about ONE:
        # the 25% baseline at the minute it opens and the 60% floor at the
        # minute it closes. Measuring either against an arbitrary piece would
        # answer a question the operator did not ask, so this says what is
        # wrong instead. Loudly, because a silent skip reads as "the floor was
        # met" -- the same reason the missing-morning-profile note below is
        # said rather than skipped.
        warnings.append(
            # In clock order, not request order: the whole finding is that the
            # answer must not depend on how the list happens to be sorted, and
            # a message that does is the same defect in prose.
            "the profiles declared overnight ("
            + ", ".join(
                f"{s.name} {_clock(s.window[0])}-{_clock(s.window[1])}"
                for s in sorted(night_segments, key=lambda s: s.window[0])
            )
            + ") are not one continuous night -- they leave a gap or describe "
            "separate stretches, so section 6's 25% pre-night baseline and 60% "
            "morning floor could not be measured. Give the night one unbroken "
            "run of profiles, split at midnight if you like, and declare only "
            "those as overnight."
        )
    elif night_run is not None and floor_villages:
        # Section 6's two state rules read the night from either END of it, so
        # a night SPLIT at midnight needs both ends named rather than one
        # segment standing in for both: the 25% baseline belongs to the half
        # that OPENS the night (23:00) and the 60% floor to the half that
        # closes it (07:00). Both come from `_one_night_run`, which chains the
        # halves end-to-start, so the answer cannot depend on the order
        # `segments` happens to arrive in. A night stated as one window is the
        # degenerate case: both ends are that window.
        opening, closing = night_run
        pre_night_over = pre_night_overfills(trajectories, capacities, floor_villages, opening.name)
        # Whichever profile takes over at the night's last minute -- 07:00 on the
        # operator's own pair. Found by the minute rather than by position or by
        # name: `segments` need not be given in clock order, and an hour with no
        # profile at all is legal (the day check simulates it on production
        # alone), in which case nothing hands over and the floor cannot be
        # measured. Said out loud rather than skipped, because a silent skip
        # reads as "the floor was met".
        morning = next((s for s in body.segments if s.window[0] == closing.window[1]), None)
        if morning is None:
            warnings.append(
                f"no profile starts at {_clock(closing.window[1])}, where "
                f"{closing.name} ends, so the morning fill floor could not be "
                f"measured -- those hours run on production alone. Give the morning "
                f"profile the night's end as its start."
            )
        else:
            morning_short = morning_floor_shortfalls(
                trajectories, capacities, floor_villages, morning.name
            )
    warnings.extend(
        f.message for f in night_state_findings(morning_short, pre_night_over, names=names)
    )

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
        morning_floor=DEFAULT_TARGET_FILL,
        pre_night_baseline=DEFAULT_BASELINE_FILL,
        morning_shortfalls=_fill_rows(morning_short, names),
        pre_night_over_baseline=_fill_rows(pre_night_over, names),
        # Re-sorted across the halves of a split night, so the worst overrun is
        # first however many profiles contributed rows.
        night_overruns=sorted(
            night_overruns, key=lambda row: (-row.overrun_minutes, row.origin, row.destination)
        ),
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

    role_deviations: list[RoleDeviationResponse] = field(default_factory=list)
    """Cells where an explicit per-village allocation overrode its role's
    template. Legitimate and deliberately not a finding -- one of four
    defensive villages always has a wall going up -- but it must not be
    invisible, so the plan hands the page the cell and both figures."""

    npc_triggers: tuple[NpcTrigger, ...] = ()
    """Section 7's two reporting triggers, as fired. Carried alongside the
    findings they produced so the page can render a table rather than parse the
    prose -- and so both come from one evaluation."""

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


def _merchant_boundary_warnings(
    planned: Sequence[_PlannedAccount],
    reserve: int,
    names: Mapping[int, str],
) -> list[str]:
    """Villages whose profiles TOGETHER commit more than the plan may.

    Merchants are one fleet shared across the day. Each profile fits its own
    ``merchant_budget`` -- the optimizer refuses otherwise -- but a long round
    trip started late in one window is still in the air when the next begins,
    so the sum across profiles is the honest upper bound on what the village
    needs. Warned, never blocked: a short merchant is a late send, not a
    disaster, and the windows' separation usually absorbs it.

    Measured against the BUDGET and not the fleet. Section I.3.5 is
    ``sum(pool) <= merchants_total - reserve``, and the reserve is precisely
    what a boundary overlap eats: against the raw fleet a composite committing
    exactly it passed in silence, as did everything between `fleet - reserve`
    and `fleet`. Section VII.6 is why that matters -- defensive calls come at
    random hours, and a village with zero idle merchants cannot respond to
    anything by hand.

    Shared by /execute and /day-check rather than living in the writer alone.
    Both build the same per-segment plans, so a check only one of them ran left
    the endpoint the operator REVIEWS with silent about it.
    """
    if len(planned) < 2:
        return []
    committed: dict[int, int] = {}
    for account in planned:
        for vid, count in account.plan.merchants_committed.items():
            committed[vid] = committed.get(vid, 0) + count
    # Every segment models the same villages off the same snapshot and config,
    # so the first stands in for all of them -- the same standing-in /execute
    # does for names and coords.
    villages = planned[0].villages
    warnings: list[str] = []
    for vid, total in sorted(committed.items()):
        village = villages.get(vid)
        if village is None:
            continue
        budget = village.merchant_budget(reserve)
        if not total > budget > 0:
            continue
        # Where the operator's own cap is what binds, say so in the words the
        # rest of the planner says it in rather than blaming the reserve.
        clause = merchant_ceiling_clause(
            village.max_busy_merchants, village.spare_merchants(reserve)
        )
        why = clause or (
            f"{village.merchant_count} in the fleet less the {reserve} held in reserve"
        )
        warnings.append(
            f"{village_label(vid, names)}: the profiles together commit "
            f"{total} merchants against a budget of {budget} ({why}); round trips "
            f"crossing a window boundary may briefly run short, delaying sends "
            f"rather than losing them -- and the reserve is what an emergency "
            f"shipment at 01:00 has to come out of"
        )
    return warnings


async def _plan_account(
    body: PlanRequest,
    dispatch_window: tuple[int, int] | None = None,
    overnight: bool | None = None,
    night_end: int | None = None,
) -> _PlannedAccount:
    """Build the account model, run the optimizer, resolve coords + warnings.

    Pure of game I/O, so it is shared by the zero-request /plan endpoint and by
    the dry-run computation inside /execute.

    ``dispatch_window`` is the hours of the day this route set runs, for a plan
    that belongs to one allocation profile rather than to the whole day. It
    phases the sends into those hours; /plan and /execute leave it None and get
    the round-the-clock beat.

    ``overnight`` says whether those hours are the ones the operator sleeps
    through, which decides whether section 6's rules govern the plan. It travels
    with the window for the same reason attendance does -- the caller with the
    segment in hand is the only one that knows -- and left None the window's
    wrap decides it (see :func:`~.night_profile.is_night_window`).

    ``night_end`` is the minute the whole night closes, for a night typed as a
    pair either side of midnight. Section 6's completion deadline is the
    night's and not each half's, and only a caller holding every profile can
    say where the night ends -- left None the window is the night, which is
    what /plan and a single-window night both want.
    """
    # Warnings are read by a person: name villages the way they do, never by id.
    names = {v.village_id: v.name for v in body.snapshot if v.name}
    trade_office = {c.village_id: c.trade_office_level for c in body.config}
    # Read straight off the config, the way `trade_office` is: the cap is owned
    # per village and no role template carries one, so there is nothing to
    # merge. The schema has already refused a cap above the village's fleet.
    max_busy = {
        c.village_id: c.max_busy_merchants for c in body.config if c.max_busy_merchants is not None
    }
    # Profile section 5's declared relay tier, read off the config for the same
    # reason the cap is: no role template carries one, because a tier is a fact
    # about a village's POSITION -- 18 sits between 02 and 11 -- and not about
    # the kind of village it is. The schema has already refused an unknown
    # downstream, a self-reference, a role village and a second hop.
    relay_for = {c.village_id: tuple(c.relay_for) for c in body.config if c.relay_for is not None}
    # Checked before the roles are resolved, so an empty snapshot is reported as
    # the empty snapshot it is rather than as a role naming a village that is
    # not in it. Every snapshot entry becomes a village state below, so this is
    # the same condition the check on `villages` used to make.
    if not body.snapshot:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Snapshot is empty — fetch account state first.",
        )
    # Profile section 1: what each village is FOR, and the template that goes
    # with it. Resolved once, here, because the role decides three separate
    # things and they must all read the same answer -- the targets and spend
    # handed to the allocation layer, whether the optimizer may relay through
    # the village, and how loud its designed crop deficit is.
    roles = _resolve_roles(body)
    villages = {
        v.village_id: VillageState(
            village_id=v.village_id,
            x=v.x,
            y=v.y,
            merchant_count=v.merchants_total,
            trade_office_level=trade_office.get(v.village_id, 0),
            name=v.name,
            # Carried so relay can refuse a hub that is losing crop, WHERE
            # nothing was declared. Passed through as-is, None included: an
            # unreadable rate must not be rounded to a safe-looking zero.
            crop_per_hour=v.crop_per_hour,
            # And the declaration that supersedes it (section 5.9). `None` for
            # a village with no role is the whole of today's behaviour.
            role=roles.of_village.get(v.village_id),
            may_relay=roles.may_relay.get(v.village_id),
            # Section 5's own ceiling. Carried on the village so every reader
            # of the merchant budget takes it from the same place, the way the
            # Trade Office level and the relay permission travel.
            max_busy_merchants=max_busy.get(v.village_id),
            # And section 5's tier, on the village for the same reason: the flow
            # builder, the hub report and the beat all have to read one list.
            relay_for=relay_for.get(v.village_id),
        )
        for v in body.snapshot
    }

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
    # Same precedence, so the declaration cannot end up describing a different
    # profile's hours than the ones it was sent with.
    effective_overnight = overnight if overnight is not None else body.overnight

    # Section 7's declaration, and nothing more than a declaration: the buffer
    # LEVEL per village (only this layer knows the warehouse capacity), whether
    # the operator is trading during these hours, and any feedstock override.
    # How much conversion that funds is derived inside `craft_plan`, from what
    # the first pass says each village retains -- a rate from rates, so no
    # window length can move it.
    #
    # A fraction of 0.0 is no floor at all (`0.0 is None` at every layer), so it
    # never reaches the policy and never asks the operator for an attendance
    # declaration it does not need.
    capacities_by_id = {v.village_id: v.warehouse_capacity for v in body.snapshot}
    floor_level: dict[int, float] = {}
    feedstock: dict[int, frozenset[Resource]] = {}
    for cfg in body.config:
        if not cfg.stock_floor_fraction:
            continue
        capacity = capacities_by_id.get(cfg.village_id)
        if capacity is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"{village_label(cfg.village_id, names)}: a stock floor of "
                    f"{cfg.stock_floor_fraction:.0%} was set but this village has no "
                    f"warehouse capacity in the snapshot, so the buffer level cannot "
                    f"be worked out. Fetch capacities first, or clear the floor."
                ),
            )
        floor_level[cfg.village_id] = cfg.stock_floor_fraction * capacity
        if cfg.npc_feedstock is not None:
            feedstock[cfg.village_id] = frozenset(cfg.npc_feedstock)
    npc_policy = NpcPolicy(
        floor_level=floor_level,
        # None here can only be a round-the-clock set: the validator asks for
        # the flag whenever a floor meets a window. It defaults to UNATTENDED,
        # not attended. "Round the clock has no night hours to mis-fund" was
        # the old reading and it is backwards -- such a set has ALL 24 hours,
        # including the eight nobody is at the Marketplace, and Travian offers
        # nothing to confine a repeat interval to part of the day (which is
        # what `WINDOW_NOT_ENFORCEABLE` and `window_pruning` exist to say).
        #
        # Nothing downstream would have caught the optimistic reading:
        # `simulate_day` tops the store up at every departure minute including
        # 03:00, and `NPC_CAPACITY_SHORT` is measured against that same cap. So
        # the direction of the default is the whole guard. Unattended
        # under-delivers and reports it; attended over-commits in silence.
        attended=body.npc_attended if body.npc_attended is not None else False,
        sources=feedstock,
    )

    # Section 2: what each village spends per hour, the operator's own flat
    # constants. Threaded beside the NPC policy and for the same reason -- both
    # are account state the game will not report, not tunables -- and shaped
    # per resource to match. It moves each village's net and nothing else: the
    # cargo stays the gap between the target and the village's own production.
    #
    # Refused for a village the snapshot does not contain, the same way
    # ship_only_to is: a figure attached to an id that is not being planned is a
    # typo or a chiefed village, and either way the operator's declared spend is
    # not reaching the plan they are reading.
    #
    # Role templates are already folded in (see `_resolve_roles`): a village
    # takes its role's spend for every resource it does not state itself, so
    # section 2's four defensive figures are typed once.
    declared_consumption = roles.consumption
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
            # Same gate the NPC policy uses: a village whose rate for this
            # resource could not be read is dropped from the resource plan
            # entirely (with an UNREADABLE_RATE finding), so a spend recorded
            # against it would 400 the whole plan over one missing reading.
            #
            # It cannot currently fire, and that is worth knowing rather than
            # rediscovering: `crop_per_hour` is the only nullable rate in the
            # snapshot, and a crop spend is refused at the schema, so every
            # surviving spend names a material whose rate is a plain float.
            # Kept as the gate for whoever makes a material rate nullable --
            # and R3-D8 is the finding they must answer first, because a spend
            # dropped HERE would be dropped in silence and leave the village
            # reported as stockpiling everything that lands on it.
            # tests/test_distribution_routes.py has the two guards that fail
            # the moment that becomes possible.
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

    # The profile's own hours may TIGHTEN the standing latency target and never
    # loosen it, and the arithmetic lives here so every caller gets the same
    # answer for the same window -- /plan, /day-check and /execute alike.
    #
    # Both directions were wrong before, in opposite ways. Taking the window as
    # the target loosened a 16h day profile to a 16h target, which no route can
    # miss: the latency pass never fires, flows land on the cheapest (longest)
    # cycles and the batches into every store grow, with nothing having
    # simulated the bursts. Ignoring the window left a 60-minute profile aiming
    # at a 2h delivery lag it has no hours to absorb. So the tighter of the two
    # binds. `None` stays exactly "no target" -- the window cannot invent one.
    latency_target = body.max_latency_hours
    if latency_target is not None and effective_window is not None:
        latency_target = min(latency_target, _window_minutes(effective_window) / 60.0)

    config = PlannerConfig(
        geometry=MapGeometry(span=body.map_span, speed_fields_per_hour=body.speed_fields_per_hour),
        merchant_model=MerchantModel(
            base_capacity=body.merchant_base_capacity,
            bonus_per_trade_office_level=body.trade_office_bonus_per_level,
        ),
        merchant_reserve=body.merchant_reserve,
        merchant_headroom=body.merchant_headroom,
        cycles=allowed_cycles,
        max_latency_hours=latency_target,
        min_arrival_gap_minutes=body.min_arrival_gap_minutes,
        reserved_window=body.reserved_window,
        # The explicit argument wins (the day check passes each segment's own
        # hours); otherwise take what the client sent on the request, which is
        # how /plan and /execute learn the active profile's window.
        dispatch_window=effective_window,
        overnight=effective_overnight,
        night_end_minute=night_end,
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
            # The RESOLVED set: each role's template filled in wherever the
            # village named no target of its own, so section 2.1's one profile
            # reaches all four of its villages. An explicit entry is left exactly
            # as it was and reported as a deviation instead.
            for resource, per_village in roles.allocations.items()
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
            craft_plan, villages, productions, allocations, config, npc_policy, consumption
        )
    except AllocationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # What the NPC conversion is actually funding. An allowance nobody drew on
    # says nothing and is not reported; a DRAW is a standing dependency on the
    # operator's trading, so it is named -- and it is read off the emitted plan
    # rather than off the declaration, because a floor that funded nothing is
    # not a dependency.
    #
    # NPC_CAPACITY_SHORT is not raised here: the allocation layer raises it,
    # where the allowance and the demand are both in hand. This block used to
    # carry its predecessor (STOCK_FLOOR_UNSUSTAINABLE), which compared the draw
    # against the crop surplus after the fact -- the same question asked of the
    # wrong model, because the allowance IS the feedstock surplus now.
    for vid, reserve in sorted(plan.npc.items()):
        label = village_label(vid, names)
        drawn: dict[Resource, float] = {}
        for resource in MATERIALS:
            rp = plan.resource_plans.get(resource)
            if rp is None:
                continue
            allocation = next((v for v in rp.villages if v.village_id == vid), None)
            if allocation is None:
                continue
            if allocation.npc_draw_per_hour > _MIN_REPORTED_STOCK_DRAW:
                drawn[resource] = allocation.npc_draw_per_hour
        if not drawn:
            continue
        total_drawn = sum(drawn.values())
        # `Resource` is a StrEnum, so it sorts by its own value; the explicit key
        # was saying the same thing twice.
        named = [r.value for r in sorted(drawn)]
        which = " and ".join(filter(None, [", ".join(named[:-1]), named[-1]]))
        paid_by = " and ".join(r.value for r in reserve.sources) or "nothing"
        extra_findings.append(
            Finding(
                category=Category.STOCK_FUNDED,
                message=(
                    f"{label} ships {total_drawn:,.0f}/h of {which} beyond its "
                    f"production, funded by converting {paid_by} at the NPC merchant "
                    f"-- keep trading or these routes under-deliver"
                ),
                detail=f"{label} -- {total_drawn:,.0f}/h NPC-funded",
                village=label,
            )
        )

    # Section 7's two triggers, read off what the plan EMITS rather than off
    # what it intended: `net_per_hour` is the rate each store moves at once the
    # routes run, less what the conversion took out of it, so a granary the plan
    # drains to fund wood is reported as the plan leaves it. Reporting only --
    # the planner never presses the NPC button.
    npc_triggers: tuple[NpcTrigger, ...] = ()
    if plan.npc:
        npc_triggers = evaluate_triggers(plan.npc, *_npc_store_state(body, plan))
        extra_findings.extend(trigger_findings(npc_triggers, names))

    # Section 9's staleness check on the operator's own crop constants, read
    # against the snapshot the plan was built from. It changes nothing about the
    # plan -- it is a WARNING about the FIGURES, and never a blocker, because
    # drift between manual updates is what the spec says to expect. Emitted here
    # so all three planning paths through `_plan_account` (/plan, /day-check,
    # /execute) raise it from one call, beside the merchant-calibration nag
    # below, which is the other "how much to trust the numbers" finding.
    extra_findings.extend(
        crop_drift_findings(
            roles.assumed_crop,
            {v.village_id: v.crop_per_hour for v in body.snapshot},
            roles.of_village,
            names,
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
            # Only villages the operator actually SENT a row for. A village
            # with no config row reads 0 through `.get(vid, 0)` -- which is the
            # right default for SIZING, because understating capacity
            # over-provisions merchants and that is the safe direction -- but it
            # is the wrong village to name here. Naming it says "level 0, read
            # the base off this one"; if it is really Trade Office 13 the dialog
            # reads about 9,000, that becomes `merchant_base_capacity`, and
            # every route in the account is then sized to cargo the merchants
            # cannot carry. That is account-killer #8, reached THROUGH the
            # mechanism meant to settle the model.
            declared = {c.village_id for c in body.config}
            zero_level = sorted(
                v.village_id
                for v in body.snapshot
                if v.village_id in declared and trade_office.get(v.village_id, 0) == 0
            )
            where = (
                ", ".join(village_label(vid, names) for vid in zero_level)
                if zero_level
                else "a village you have confirmed in-game has no Trade Office"
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
                        f"no inversion, and one at any levelled village to pin the bonus. "
                        f"Check in-game that the level really is 0 before entering what you "
                        f"read: a level nobody has typed also reads 0 here, and a base taken "
                        f"off a levelled village would be too high for every route"
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
        role_deviations=roles.deviations,
        dropped_allocations=dropped_allocations,
        npc_triggers=npc_triggers,
    )


def _plan_response(account: _PlannedAccount) -> PlanResponse:
    """The plan as the operator reads it, digest included.

    Extracted from `/plan` so `/plan/yaml` renders the SAME response rather
    than assembling a second one: the YAML's whole claim is that it describes
    what was shown, and two assemblers of one response drift -- which is the
    argument `VillageNetResponse` and `RoleDeviationResponse` both already make
    about recomputing a figure in a second place.
    """
    plan = account.plan
    names = account.names
    trade_office = account.trade_office
    config = account.config
    coords = account.coords
    villages = account.villages
    findings = account.all_findings
    upgrades = {o.village_id: o.trade_office_levels_needed for o in plan.over_budget}
    over = {o.village_id for o in plan.over_budget}

    response = PlanResponse(
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
                        max_busy=villages[vid].max_busy_merchants,
                        merchants_total=villages[vid].merchant_count,
                        fleet_spare=villages[vid].spare_merchants(config.merchant_reserve),
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
                total_npc_allowance=rp.total_npc_allowance,
                total_npc_draw=rp.total_npc_draw,
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
                resource=relay.resource,
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
        role_deviations=account.role_deviations,
        village_nets=[
            VillageNetResponse(
                village_id=v.village_id,
                resource=resource,
                own_per_hour=v.own_per_hour,
                npc_allowance_per_hour=v.npc_allowance_per_hour,
                npc_draw_per_hour=v.npc_draw_per_hour,
                target_per_hour=v.target_per_hour,
                ship_per_hour=v.ship_per_hour,
                consumption_per_hour=v.consumption_per_hour,
                net_per_hour=v.net_per_hour,
            )
            for resource, rp in sorted(plan.resource_plans.items(), key=lambda kv: kv[0].value)
            for v in rp.villages
            # Own villages only. A route-eligible foreign target rides through
            # the optimizer as a pseudo-village with a negative id, and it came
            # back out of here as `own 0 / target 500 / net 500` -- a store for
            # a sink that has none. Invisible in the grid, which indexes by real
            # village id, but this list is documented as what one village's
            # STORE does and P2/P6 read it server-side, where a permanent
            # 500/h accumulation is exactly the wrong reading. The obligation
            # is reported as a shortfall, which is about the obligation.
            if v.village_id >= 0
        ],
        # Section 6's closing rule as a table rather than as prose. The window
        # comes off the config the plan was actually built with, so a payload
        # that named no window gets an empty list instead of a rule it never
        # asked for.
        night_overruns=_night_overrun_rows(
            plan.beat,
            config.dispatch_window,
            names,
            overnight=config.overnight,
            night_end=config.night_end_minute,
        ),
        npc_reserves=_npc_reserve_rows(plan, names),
        npc_triggers=_npc_trigger_rows(account.npc_triggers, names),
        warnings=[f.message for f in findings],
        # The route count is what lets the headline stop blaming the plan for
        # losses it did not cause -- see _account_headline.
        diagnostics=_diagnostics_response(summarise(findings, routes_planned=len(plan.rows))),
    )
    # Applied last and computed over the response with this field excluded,
    # which is the only self-consistent way to put a hash of a thing inside it.
    return response.model_copy(
        update={
            "plan_digest": plan_digest(response.model_dump(mode="json", exclude={"plan_digest"}))
        }
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
    return _plan_response(await _plan_account(body))


class PlanYamlRequest(PlanRequest):
    """The plan request, plus the digest of the plan the operator confirmed.

    Section 10's order -- readable plan first, operator confirms, then generate
    the YAML -- only means something if the file describes the plan that was
    READ. See :func:`post_plan_yaml` for why that is enforced with a digest
    rather than by trusting a posted plan.
    """

    expected_plan_digest: str = Field(
        min_length=64,
        max_length=64,
        description=(
            "`plan_digest` from the `/plan` response the operator confirmed. "
            "REQUIRED, and not defaulted to 'whatever comes out': it is the "
            "confirmation step itself, in machine-readable form. A digest that "
            "does not match what this request re-plans to comes back 409 with "
            "both digests named -- never a document, because a document "
            "describing a plan nobody read is worse than no document at all."
        ),
    )

    @field_validator("expected_plan_digest")
    @classmethod
    def _digest_is_a_sha256(cls, value: str) -> str:
        # Refused as malformed rather than reported as a mismatch: a 409 says
        # "the plan moved", which would send the operator re-reading a plan
        # that never moved at all over a mistyped token.
        text = value.lower()
        if any(char not in "0123456789abcdef" for char in text):
            raise ValueError(
                "expected_plan_digest must be the 64-character hex plan_digest from a "
                "/plan response"
            )
        return text


@router.post(
    "/plan/yaml",
    response_class=PlainTextResponse,
    responses={
        200: {
            "content": {"application/yaml": {}},
            "description": "The confirmed plan as a YAML document.",
        },
        409: {"description": "The plan moved since it was read; nothing was rendered."},
    },
)
async def post_plan_yaml(
    body: PlanYamlRequest,
    _user: User = Depends(get_current_user),
):
    """Render an already-confirmed plan as YAML. Costs **zero** game requests.

    Profile section 10 fixes the order: readable plan first, the operator
    confirms, and only then is the YAML generated. So this endpoint's one job is
    to guarantee that the file describes the plan that was READ -- and there are
    only two ways to do that, because nothing on this server holds a computed
    plan. `/plan` is pure and stateless on purpose (that is what makes tuning a
    target free), so there is no plan to fetch by id.

    **Not by trusting a posted plan.** `/execute` recomputes rather than trust
    client-sent rows, and the reason is stated at :class:`_PlannedAccount`: it
    must act on exactly the plan `/plan` would display for the same inputs. The
    argument is stronger for a document the operator keeps as the record of a
    decision, not weaker -- a stale browser tab, or a hand-edited body, would
    produce an authoritative-looking file describing a plan the planner never
    produced, and nothing in it would say so.

    **So it re-plans, and the digest is what stops that being silent.** The
    planner is a pure function of the request with no clock and no randomness in
    it, so the same inputs reproduce the same plan; `/plan` returns
    `plan_digest` over the response it showed, and this endpoint re-computes,
    re-digests, and refuses with **409** -- naming both digests -- unless they
    agree. The document therefore either IS the plan that was confirmed, or it
    does not exist. A caller that reads a 200 has a file it can trust; a caller
    that reads a 409 knows exactly what happened and can re-read the plan.

    Nothing here is logged. The document is the operator's village names,
    coordinates and topology -- their own data, fine in a file they download and
    not something to write into a server log.
    """
    account = await _plan_account(body)
    response = _plan_response(account)
    if response.plan_digest != body.expected_plan_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"this plan is not the one that was confirmed: the request re-plans to "
                f"{response.plan_digest} and the confirmation names "
                f"{body.expected_plan_digest}. Nothing was rendered -- a YAML file "
                f"describing a plan nobody read is worse than no file. Re-read /plan and "
                f"confirm the digest it returns."
            ),
        )
    document = render_plan_yaml(
        # The planning inputs only. `expected_plan_digest` is deliberately
        # excluded: what lands in the file is then a valid /plan body verbatim,
        # which is what lets the operator replay this plan a month later.
        inputs=body.model_dump(mode="json", exclude={"expected_plan_digest"}),
        plan=response.model_dump(mode="json"),
        digest=response.plan_digest,
    )
    return PlainTextResponse(
        content=document,
        media_type="application/yaml",
        headers={
            # Named for the plan rather than for the moment, so two downloads of
            # one plan are one file and a diff between two plans is a diff.
            "Content-Disposition": (
                f'attachment; filename="distribution-plan-{response.plan_digest[:12]}.yaml"'
            ),
            "X-Plan-Digest": response.plan_digest,
        },
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
        # Where the night closes, resolved the same way /day-check resolves it
        # and BEFORE anything is planned: section 6's completion deadline is
        # the night's rather than each half's, and the beat both phases against
        # it and reports against it. `None` where the overnight declarations do
        # not form one continuous night, which leaves each half measured
        # against its own end -- the reading /day-check refuses out loud.
        night_close = _night_close_minute(body.segments)
        for segment in body.segments:
            per_segment = body.model_copy(
                update={
                    "allocations": segment.allocations,
                    # Attendance is judged the same way: this is the endpoint
                    # that WRITES, so a night profile must be funded by the
                    # trading that happens overnight -- none of it.
                    "npc_attended": segment.npc_attended,
                    # Latency is NOT overridden here. It used to be replaced by
                    # the segment's own window length, which /day-check does not
                    # do -- so one body was planned against a 2h target by the
                    # endpoint the operator reviews and a 16h one by the endpoint
                    # that writes, and the route set that landed was not the one
                    # anybody simulated. The window's tightening now happens
                    # inside `_plan_account`, which both endpoints go through.
                }
            )
            planned_segments.append(
                (
                    segment,
                    await _plan_account(
                        per_segment,
                        dispatch_window=tuple(segment.window),
                        # The profile's own declaration, so the run that WRITES
                        # judges section 6 on the same profile /day-check did.
                        overnight=segment.overnight,
                        # And against the same closing minute, or the two would
                        # build different beats for the same night.
                        night_end=(
                            night_close
                            if is_night_window(tuple(segment.window), overnight=segment.overnight)
                            else None
                        ),
                    ),
                )
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
    # Unprefixed: this is not one profile's finding but the account's, about the
    # boundary BETWEEN them. Raised HERE rather than beside the reconciliation
    # it used to sit in, because the dry run returns before that point -- so the
    # preview the operator authorises a live run from never showed it either.
    warnings.extend(
        _merchant_boundary_warnings(
            [acc for _segment, acc in planned_segments], body.merchant_reserve, names
        )
    )

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
