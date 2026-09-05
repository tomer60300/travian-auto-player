"""Server-side storage for the resource planner's setup document.

Everything the operator types into the planner is OWNED state -- the game will
not tell us a Trade Office level, a role, a relay tier or what a village spends
-- and it lived only in `localStorage`, which is scoped to an ORIGIN. The same
app on :80, on :8001, on the LAN address and over Tailscale therefore kept four
independent copies, and a cleared origin lost the lot. Export-to-file was the
workaround; this is the shared copy.

Three deliberate decisions, so the next reader does not read them as oversights:

**One saved setup per user per account, not a preset library.** The operator
asked for import and export, not a shelf of named variants: several would need
an "in force" flag, a picker and an answer for what happens when the active one
is deleted, none of which was asked for. The composite primary key on
:class:`~travian_api.web.models.planner_setup.PlannerSetup` makes that a fact of
the schema. Presets, if ever wanted, are a new table.

**The document is stored and returned VERBATIM.** It is validated against the
models below, but what is written to the row is the request body as received.
`plannerSetup.js`'s `buildSetup` omits every field it has no answer for, and a
store that re-serialised a validated model would write `may_relay: null` and
`trade_office_level: 0` onto every row -- turning "nothing declared" into a
declaration, which is the one thing the page reads differently from zero. It
also means the version is stored as given: nothing here silently upgrades a
document on write.

**A document the planner would refuse is refused HERE.** Saved and reloaded a
week later, an unusable setup is a trap: the operator only discovers it when
they try to plan. So the rules are the plan request's OWN, reached by building a
:class:`~travian_api.web.routes.distribution.PlanRequest` out of the document
and letting its validators speak -- the crop-spend refusal, the role template's
remainder refusal, the six relay-tier rules, the merchant bounds -- and by
calling :func:`~travian_api.web.routes.distribution._resolve_roles` for the one
that lives in a handler (a claimed role with no template). Not restated: a
second copy of a rule is a defect waiting to happen.

The half of those rules that is about the ACCOUNT rather than the document
cannot be decided here, and deliberately is not. A setup document carries no
snapshot, so "is this village real" and "does it field that many merchants"
have no answer -- exactly why `plannerSetup.js` refuses to decide them either.
The snapshot handed to the plan request therefore names every village the
document mentions and nothing else, which is the reading under which those two
checks pass and every document-only rule still bites. Both are enforced for
real on the next `/plan` call, and shown on the cells by `unreachableCaps` and
`relayTierProblems`.

Nothing here is logged. The document is the operator's village names,
coordinates and topology -- personal data -- and every row is addressed by the
user it belongs to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictBool, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.roles import Role
from travian_api.web.auth import get_current_user
from travian_api.web.models.db import User, get_db
from travian_api.web.models.planner_setup import PlannerSetup
from travian_api.web.routes.distribution import (
    AllocationInput,
    ForeignTarget,
    PlanRequest,
    RoleTemplate,
    VillageConfig,
    VillageSnapshot,
    _resolve_roles,
)

router = APIRouter(prefix="/api/distribution", tags=["distribution"])

# ─── The document's own constants ─────────────────────────────────────────
#
# These four have no Python home yet: they describe the FILE FORMAT, which
# `frontend/src/utils/plannerSetup.js` owns. Kept as a named second copy rather
# than inferred from anything, so a divergence is one grep away -- the source of
# truth is SETUP_FORMAT, READABLE_VERSIONS and MAX_MERCHANTS_PER_VILLAGE there,
# and a version added on one side must be added on the other or a fresh export
# is refused on save.

# `Final`, because `SetupDocument.format` is `Literal[SETUP_FORMAT]`: without
# it the name is statically `str`, so the literal type is not a literal type
# at all and the one field identifying the document as ours goes unchecked
# by any checker reading this module.
SETUP_FORMAT: Final = "travian-planner-owned-state"

READABLE_VERSIONS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
"""Versions this build can read. A v1 document simply carries no profiles, a v2
one no roles, a v3 one no per-village relay answer, a v4 one no merchant cap, a
v5 one no relay tier and a v6 one no per-profile NPC attendance, so refusing any
of them would strand every export written before those travelled.

v7 carries `npc_attended` per profile, v8 carries `overnight` per profile, and
v9 carries the account-wide `reserved_window`. All three earned a version rather
than riding along as unknown keys -- which they mechanically could, since the
body is stored verbatim and `SetupDocument` ignores extras -- because the
harmful path is identical for each: a new build writes the answer into an older
document, an older build reads it, silently drops it, and the operator saves
from that build. The answer is then gone from the SHARED copy, which is the
whole reason this store exists.

All three are answers the planner refuses to guess, and dropping any of them is
silent. Losing `npc_attended` funds night routes from trading nobody did. Losing
`overnight` un-declares which profile is the night, so a night split across
midnight stops being recognised as one: section 6's completion rule goes
unchecked and the 60% morning floor is measured against the wrong minute. Losing
`reserved_window` puts the operator's manual NPC burst back into competition
with merchants landing -- and that one was carried by NEITHER persistence path
before v9, living only in the page's localStorage, which is per browser origin
and so does not follow the operator between :80, :8001, the LAN address and
Tailscale.

v10 carries `prune_to_window`, on exactly the criterion `reserved_window`
earned v9 for: it was carried by neither persistence path either, and it
decides whether `/execute` DELETES rows from the game -- the only destructive
answer in the whole document."""

MAX_MERCHANTS_PER_VILLAGE = 20
"""Travian's hard ceiling on merchants in one village. The only bound on a
merchant cap a DOCUMENT can check -- the real bound is that village's own
fleet, which lives in the snapshot and not in the document."""

_ClockTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
"""A profile window's `HH:MM`. The backend's own windows are minutes past
midnight, so this shape exists only in the document."""

AccountKey = Annotated[
    str,
    Query(
        min_length=1,
        max_length=256,
        description=(
            "`${serverUrl}|${playerName}` -- the same key the page scopes its own "
            "localStorage to. Village ids are per account, so the key is what keeps "
            "two worlds from overwriting each other."
        ),
    ),
]


# ─── Request / response models ────────────────────────────────────────────


class SetupVillage(VillageConfig):
    """One village row: the plan request's `VillageConfig` plus what the
    document carries that a plan request does not.

    Subclassed rather than restated so the row is validated by the same model
    the planner reads -- the crop-spend refusal, the Trade Office ceiling and
    the stock-floor bounds all arrive with it.
    """

    # A row is written for a village the operator TYPED something about, so the
    # name is carried for the messages. Absent on a hand-edited document.
    name: str = ""
    village_id: int = Field(gt=0)
    crop_ceiling: float | None = Field(default=None, ge=0)
    max_busy_merchants: int | None = Field(
        default=None,
        ge=0,
        le=MAX_MERCHANTS_PER_VILLAGE,
        description=(
            "The most merchants this village may have underway at any instant. "
            "Bounded here by the 20 a village can ever hold -- the plan request "
            "leaves it open because it checks the village's real fleet instead, "
            "which a document has no way to know."
        ),
    )

    @field_validator("ship_only_to", "relay_for")
    @classmethod
    def _lists_hold_village_ids(cls, value: list[int] | None) -> list[int] | None:
        """A village id is a positive integer.

        The document's own rule (`plannerSetup.js` refuses the same), and it has
        to be here: the plan request checks these lists against the snapshot
        instead, which a document does not have. Left unchecked, `0` would
        store cleanly and be refused on the next plan as a village the snapshot
        does not contain.
        """
        if value is not None and any(vid <= 0 for vid in value):
            raise ValueError("every entry must be a village id, so a positive whole number")
        return value

    @field_validator("stock_floor_fraction")
    @classmethod
    def _floor_sits_on_the_grid_the_input_types_on(cls, value: float | None) -> float | None:
        """The operator types a percent, whole or to one decimal.

        The plan request bounds the fraction (0 to 0.95) and stops there, so
        0.3333 plans perfectly well -- but the page's own file parser refuses to
        read it back, which would leave a stored setup the operator cannot load.
        The grid is `isStockFloorFraction`'s.
        """
        if value is None:
            return value
        permille = value * 1000
        if abs(permille - round(permille)) > 1e-6:
            raise ValueError(
                "a stock floor is typed as a percent, whole or to one decimal, so the "
                "fraction must sit on a 0.001 grid"
            )
        return value


class MerchantModelIn(BaseModel):
    """The account-wide merchant levers, as the document carries them.

    Only the shapes are declared here. Every BOUND is the plan request's --
    `merchant_base_capacity` gt 0, `trade_office_bonus_per_level` ge 0,
    `merchant_reserve` 0-20, `merchant_headroom` under 1, `map_span` odd,
    `speed_fields_per_hour` gt 0 -- applied by :func:`_as_plan_request`, so
    there is one copy of each.

    ALL of them optional, absent meaning "use the planner's own", which is how
    the PLAN path already reads a cleared box: `buildPlanPayload` omits the
    field entirely and the default in `PlanRequest` decides. `base_capacity` and
    `bonus_per_to_level` were required here alone, so clearing either made the
    whole setup unsaveable -- a 422 "Field required" over a figure the operator
    had deliberately not supplied, with no cell marked to say which.
    """

    base_capacity: int | None = None
    bonus_per_to_level: float | None = None
    merchant_reserve: int | None = None
    merchant_headroom: float | None = None
    # The world and the merchants that cross it. `buildSetup` writes the whole
    # merchant model, these two included, and nothing here declared them -- so
    # `_as_plan_request` never lifted them and `_span_is_odd` never saw them. An
    # even span saved with a 200, came back out of GET unchanged, and the page's
    # own parser then refused the document forever: "merchant_model.map_span is
    # 400". A document the planner would refuse is refused HERE.
    map_span: int | None = None
    speed_fields_per_hour: float | None = None


class SetupDocument(BaseModel):
    """The planner's owned-state document, exactly as `buildSetup` writes it."""

    format: Literal[SETUP_FORMAT]
    version: int
    # Not parsed. The document round trips verbatim, and the page does not
    # validate its own stamp, so demanding a shape here would refuse a
    # perfectly loadable file over a field nothing reads back.
    exported_at: str | None = None
    account: str | None = None
    villages: list[SetupVillage]
    roles: dict[Role, RoleTemplate] = {}
    # profile name -> resource -> village id -> allocation. Every mode is legal
    # per village, remainder included: exactly one village per resource absorbs
    # the slack, and this is where that is said.
    profiles: dict[str, dict[Resource, dict[Annotated[int, Field(gt=0)], AllocationInput]]] = {}
    profile_windows: dict[str, tuple[_ClockTime, _ClockTime]] = {}
    # The two per-profile ANSWERS, keyed by profile name like the windows above.
    #
    # Declared here rather than left to ride through as unknown keys, which is
    # what they did until 2026-09-04: the body is stored verbatim and this model
    # ignores extras, so a `"yes"` where a boolean belongs saved without
    # complaint and the page then refused to load the document it had just
    # written. That is exactly the trap `_floor_sits_on_the_grid_the_input_types_on`
    # exists to prevent, and the store's own rule is that a document the planner
    # would refuse is refused HERE.
    #
    # Both are answers the planner will not guess, which is why the type matters
    # more than it looks: `npc_attended` guessed wrong funds night routes from
    # trading nobody did, and `overnight` guessed wrong un-declares which
    # profile is the night, so a night split across midnight stops being read as
    # one. Absent is the third state -- "not answered yet" -- and it is the state
    # that refuses a plan rather than the one that invents an answer.
    #
    # `StrictBool`, not `bool`: pydantic's lax bool accepts the STRING "yes",
    # and "no", "on", "off", "1" and "0" besides. Measured -- a document
    # carrying `{"Night": "yes"}` saved with a 200 and came back coerced to
    # `true`. That is a value nobody typed as a boolean being read as an answer
    # to the one question this code refuses to guess, so the coercion is worse
    # here than the refusal. `parseSetup` throws on a non-boolean for the same
    # reason; this is the server half of that rule.
    npc_attended: dict[str, StrictBool] = {}
    overnight: dict[str, StrictBool] = {}
    # Minutes of the day to keep clear of ARRIVALS, so the operator's manual NPC
    # burst is not competing with merchants landing. A PAIR and not a fourth map
    # beside the three per-profile ones, because it is one person at one
    # marketplace: attendance is per profile since the operator is awake for
    # some windows and not others, while when they sit down to trade is not a
    # property of a window at all. Absent means "reserve nothing".
    reserved_window: tuple[_ClockTime, _ClockTime] | None = None
    # Whether a live run TRIMS each route's fan-out to its profile's hours --
    # which means deleting rows from the game, the one destructive answer this
    # document carries. Carried by neither persistence path before v10, which is
    # the criterion `reserved_window` earned v9 for. `StrictBool` for the reason
    # the two maps above carry it: pydantic's lax bool reads the STRING "no" as
    # False, and a value nobody typed as a boolean must not decide whether rows
    # are removed. Absent is "not answered", not "do not prune".
    prune_to_window: StrictBool | None = None
    merchant_model: MerchantModelIn | None = None
    foreign_targets: list[ForeignTarget] = []

    @field_validator("version")
    @classmethod
    def _version_is_one_this_build_reads(cls, value: int) -> int:
        if value in READABLE_VERSIONS:
            return value
        if value > max(READABLE_VERSIONS):
            raise ValueError(
                f"this setup is version {value}, which was written by a NEWER build "
                f"than the one running here (it reads version "
                f"{', '.join(str(v) for v in READABLE_VERSIONS)}). Upgrade the server, "
                f"or re-export from this build -- loading it would drop whatever the "
                f"newer version added and plan a different account without saying so"
            )
        raise ValueError(
            f"this setup is version {value}, which is not a version any build wrote; "
            f"readable versions are {', '.join(str(v) for v in READABLE_VERSIONS)}"
        )

    @field_validator("profiles")
    @classmethod
    def _profiles_are_named(cls, value: dict[str, Any]) -> dict[str, Any]:
        # A blank name is not a profile the operator can ever select again.
        if any(not name.strip() for name in value):
            raise ValueError("a profile has a blank name")
        return value


class StoredSetupOut(BaseModel):
    """What GET and PUT both answer with."""

    account_key: str
    setup: dict[str, Any] = Field(
        description=(
            "The document as it was saved, byte for byte -- the same JSON the "
            "page's own export writes and its `parseSetup` reads."
        )
    )
    saved_at: datetime


# ─── Validation: the plan request's rules, not a second copy of them ──────


def _as_plan_request(doc: SetupDocument) -> PlanRequest:
    """Read the document AS a plan request, so its validators run.

    The snapshot is synthetic and names every village the document mentions --
    its own rows plus every relay downstream. That is what makes the two
    account-relative checks stand down rather than fire wrongly: a merchant
    fleet of 0 is what `/snapshot` writes when it could not READ one, and
    `_merchant_caps_are_reachable` already skips those villages by name
    ("unknown is not zero"); and a downstream with nothing else typed has no
    row of its own, so demanding one would refuse a perfectly good document --
    the reason `plannerSetup.js` does not check downstreams against an account
    either. Everything that is about the document alone still bites.
    """
    mentioned: set[int] = {row.village_id for row in doc.villages}
    for row in doc.villages:
        mentioned.update(row.relay_for or ())
    names = {row.village_id: row.name for row in doc.villages if row.name}
    levers: dict[str, float] = {}
    if doc.merchant_model is not None:
        # Absent means "use the planner's own", so an omitted lever is omitted
        # from the request too rather than sent as None -- the plan path reads a
        # cleared box exactly this way.
        for field, lever in (
            ("base_capacity", "merchant_base_capacity"),
            ("bonus_per_to_level", "trade_office_bonus_per_level"),
            ("merchant_reserve", "merchant_reserve"),
            ("merchant_headroom", "merchant_headroom"),
            ("map_span", "map_span"),
            ("speed_fields_per_hour", "speed_fields_per_hour"),
        ):
            value = getattr(doc.merchant_model, field)
            if value is not None:
                levers[lever] = value
    return PlanRequest(
        snapshot=[
            VillageSnapshot(village_id=vid, name=names.get(vid, ""), x=0, y=0)
            for vid in sorted(mentioned)
        ],
        config=list(doc.villages),
        roles=doc.roles,
        foreign_targets=doc.foreign_targets,
        **levers,
    )


def _sentence(detail: Any) -> str:
    """One pydantic error's message, unwrapped.

    Pydantic prefixes a validator's own text with "Value error, "; stripped so
    the operator reads the sentence the planner wrote, which is the whole point
    of reusing the rule rather than restating it.
    """
    return str(detail["msg"]).removeprefix("Value error, ")


def _refusals(error: ValidationError) -> str:
    """Every refusal in one line, for the errors that have no single field.

    The plan request's cross-row rules -- the relay tier, the merchant caps --
    are model validators, so their `loc` is empty and the message is the whole
    of what there is to say.
    """
    out = []
    for detail in error.errors():
        where = ".".join(str(part) for part in detail["loc"])
        out.append(f"{where}: {_sentence(detail)}" if where else _sentence(detail))
    return "; ".join(out)


def _validate(body: dict[str, Any], account_key: str) -> None:
    """Refuse anything the planner would refuse, or that is not this account's.

    Raises 422 and returns nothing: the document is stored from the raw body,
    never from what came back out of a model.
    """
    try:
        doc = SetupDocument.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": list(e["loc"]), "msg": _sentence(e)} for e in exc.errors()],
        ) from exc
    # `setupMatchesAccount`'s rule: a document that names no account has
    # nothing to contradict (it was exported before one was connected), and one
    # that names a different account must not be adopted -- village ids are per
    # account, so its levels, caps and relay tier are all silently wrong here.
    if doc.account and doc.account != account_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"this setup was exported from account {doc.account}, and would be saved "
                f"under {account_key}. Village ids are per account, so one account's "
                f"Trade Office levels, merchant caps and relay tier are silently wrong "
                f"under another -- connect the account it belongs to, or re-export it "
                f"from this one"
            ),
        )
    try:
        request = _as_plan_request(doc)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_refusals(exc)
        ) from exc
    # The one rule that lives in a handler rather than at the schema: a claimed
    # role with no template. Raises its own 422, worded as the plan does.
    _resolve_roles(request)


# ─── Endpoints ────────────────────────────────────────────────────────────


async def _saved_row(db: AsyncSession, user_id: int, account_key: str) -> PlannerSetup | None:
    result = await db.execute(
        select(PlannerSetup).where(
            PlannerSetup.user_id == user_id,
            PlannerSetup.account_key == account_key,
        )
    )
    return result.scalar_one_or_none()


def _stored(row: PlannerSetup) -> StoredSetupOut:
    # SQLite returns naive datetimes despite the column's timezone=True; the
    # value was stored in UTC, so it is restored rather than served as a local
    # time the caller would read three hours out.
    saved_at = (
        row.updated_at.replace(tzinfo=UTC) if row.updated_at.tzinfo is None else row.updated_at
    )
    return StoredSetupOut(
        account_key=row.account_key,
        setup=json.loads(row.setup_json),
        saved_at=saved_at,
    )


@router.get("/setup", response_model=StoredSetupOut)
async def get_setup(
    account_key: AccountKey,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoredSetupOut:
    """The caller's saved setup for one account.

    404 when nothing is saved, deliberately: an empty setup and no setup are
    different states, and the page has to be able to tell "you have never
    saved" from "you saved a blank sheet" -- the second is a decision to leave
    the account undescribed, the first is an invitation to import a file.
    """
    row = await _saved_row(db, user.id, account_key)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No planner setup is saved for this account.",
        )
    return _stored(row)


@router.put("/setup", response_model=StoredSetupOut)
async def put_setup(
    body: dict[str, Any],
    account_key: AccountKey,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoredSetupOut:
    """Validate and store, replacing whatever was there.

    Idempotent: one row per user per account, so the same document sent twice
    leaves the same single row. The body is stored as received -- see this
    module's docstring on why a re-serialised model would be a different
    document.
    """
    _validate(body, account_key)
    row = await _saved_row(db, user.id, account_key)
    if row is None:
        row = PlannerSetup(
            user_id=user.id,
            account_key=account_key,
            setup_json=json.dumps(body),
        )
        db.add(row)
    else:
        row.setup_json = json.dumps(body)
    await db.commit()
    await db.refresh(row)
    return _stored(row)


@router.delete("/setup", status_code=status.HTTP_204_NO_CONTENT)
async def delete_setup(
    account_key: AccountKey,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Forget the saved setup for one account.

    404 when there is nothing to forget, as `DELETE /presets/{id}` answers:
    "deleted" and "there was never anything there" are different answers, and
    the caller asked to remove something specific.
    """
    row = await _saved_row(db, user.id, account_key)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No planner setup is saved for this account.",
        )
    await db.delete(row)
    await db.commit()
