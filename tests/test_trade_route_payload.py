"""The trade-route wire format, pinned to a captured real client request.

Captured from Europe 2 on 2026-08-20 (gpack 597.6) with Gold Club active:

    POST /api/v1/trade-routes  -> 201, empty body
    {"action":"traderoute","sourceVillageId":<id>,
     "targetCoordinates":{"x":<x>,"y":<y>},
     "resources":{"lumber":1,"clay":2,"iron":3,"crop":4},
     "mode":"send","hour":15,"minute":27,"deliveries":1,
     "repeatEvery":1,"enabled":true,"useTradeShips":false}

    PUT  /api/v1/trade-routes  -> 200
    {"action":"traderoute","routes":[{"enabled":false,"id":<id>}, ...]}

The village id, coordinates and route ids from that capture are deliberately
NOT reproduced here -- this repository is public, and they identify a real
account. What matters for the wire format is the field names, the nesting and
the types, so stand-in values are used throughout.

The key-set assertions are the point of this file. An extra field the real
client never sends is a bot fingerprint, so "exactly these keys" is a stronger
and more useful property than "at least these keys".
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.exceptions import NetworkError, SessionExpiredError
from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    ExistingRoute,
    PlannedRoute,
    TradeRoutePayloadUnverified,
    TradeRouteService,
)

# Exactly the keys the real client sent, in the create body.
CAPTURED_CREATE_KEYS = {
    "action",
    "sourceVillageId",
    "targetCoordinates",
    "resources",
    "mode",
    "hour",
    "minute",
    "deliveries",
    "repeatEvery",
    "enabled",
    "useTradeShips",
}


class _RecordingClient:
    """Records the verb, url and body of anything sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []

        class _Delay:
            @staticmethod
            async def wait(*_args, **_kwargs) -> None:
                return None

        self.human_delay = _Delay()
        self.settings = SimpleNamespace(base_url="https://ts2.x1.europe.travian.com")
        # Every write feeds the seconds it consumed into the daily activity
        # ceiling. Without this attribute _log_activity raises AttributeError
        # into its own broad catch, so the accounting silently does nothing --
        # and no test notices, because the write itself still succeeds.
        self.logged_activity: list[float] = []
        self.activity_scheduler = SimpleNamespace(log_activity=self.logged_activity.append)

    async def post_json(self, url: str, payload: dict, **_kwargs):
        self.sent.append(("POST", url, payload))
        return {}

    async def put_json(self, url: str, payload: dict, **_kwargs):
        self.sent.append(("PUT", url, payload))
        return {}

    async def delete_json(self, url: str, *, data: dict | None = None, **_kwargs):
        self.sent.append(("DELETE", url, data))
        return {}


def _service() -> tuple[TradeRouteService, _RecordingClient]:
    client = _RecordingClient()
    # reconciler_verified: this file tests the WIRE SHAPE, not the reconciler.
    # The gate that normally blocks a create is exercised in
    # tests/test_trade_route_reconciler_gate.py.
    return TradeRouteService(client, live_enabled=True, reconciler_verified=True), client


def _existing() -> list[ExistingRoute]:
    return [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=23, dest_y=88)]


def _route(*, dispatch_minute: int = 15 * 60 + 27, cycle_hours: int = 1) -> PlannedRoute:
    return PlannedRoute(
        origin_village_id=20031,
        dest_village_id=20044,
        dest_x=23,
        dest_y=88,
        dest_name="capital",
        cargo={Resource.LUMBER: 1, Resource.CLAY: 2, Resource.IRON: 3, Resource.CROP: 4},
        cycle_hours=cycle_hours,
        merchants=4,
        dispatch_minute=dispatch_minute,
    )


class TestCreateBody:
    def test_it_sends_exactly_the_captured_field_set(self):
        service, _ = _service()
        payload = service._build_create_payload(_route())
        assert set(payload) == CAPTURED_CREATE_KEYS

    def test_it_matches_the_capture_field_for_field(self):
        service, _ = _service()
        assert service._build_create_payload(_route()) == {
            "action": "traderoute",
            "sourceVillageId": 20031,
            "targetCoordinates": {"x": 23, "y": 88},
            "resources": {"lumber": 1, "clay": 2, "iron": 3, "crop": 4},
            "mode": "send",
            "hour": 15,
            "minute": 27,
            "deliveries": 1,
            "repeatEvery": 1,
            "enabled": True,
            "useTradeShips": False,
        }

    def test_the_destination_is_nested_not_flat(self):
        # The earlier guess sent top-level x/y. The client nests them.
        service, _ = _service()
        payload = service._build_create_payload(_route())
        assert payload["targetCoordinates"] == {"x": 23, "y": 88}
        assert "x" not in payload and "y" not in payload

    def test_the_merchant_count_is_never_sent(self):
        # The game derives it from the cargo; sending it would be an extra
        # field, and the planner's figure is for budgeting only.
        service, _ = _service()
        assert "merchants" not in service._build_create_payload(_route(cycle_hours=3))

    @pytest.mark.parametrize(
        "dispatch_minute,hour,minute",
        [(0, 0, 0), (90, 1, 30), (15 * 60 + 27, 15, 27), (23 * 60 + 59, 23, 59)],
    )
    def test_the_send_time_is_split_into_hour_and_minute(self, dispatch_minute, hour, minute):
        # This is what makes the planner's beat realisable: the send time is
        # chosen at creation, not fixed to the moment of the click.
        service, _ = _service()
        payload = service._build_create_payload(_route(dispatch_minute=dispatch_minute))
        assert (payload["hour"], payload["minute"]) == (hour, minute)

    def test_the_cycle_is_carried_as_repeat_every(self):
        service, _ = _service()
        assert service._build_create_payload(_route(cycle_hours=8))["repeatEvery"] == 8

    def test_all_four_resources_are_always_present(self):
        service, _ = _service()
        route = _route()
        route = PlannedRoute(
            origin_village_id=route.origin_village_id,
            dest_village_id=route.dest_village_id,
            dest_x=route.dest_x,
            dest_y=route.dest_y,
            dest_name=route.dest_name,
            cargo={Resource.CROP: 5_000},  # crop only
            cycle_hours=route.cycle_hours,
            merchants=route.merchants,
            dispatch_minute=route.dispatch_minute,
        )
        resources = service._build_create_payload(route)["resources"]
        assert resources == {"lumber": 0, "clay": 0, "iron": 0, "crop": 5_000}

    def test_it_posts_to_the_collection_endpoint(self):
        service, client = _service()
        asyncio.run(service.create_route(_route()))
        assert [(verb, url) for verb, url, _ in client.sent] == [("POST", "/api/v1/trade-routes")]


class TestToggleBody:
    def test_it_matches_the_capture(self):
        service, _ = _service()
        assert service._build_toggle_payload([647196, 647197], active=False) == {
            "action": "traderoute",
            "routes": [
                {"enabled": False, "id": 647196},
                {"enabled": False, "id": 647197},
            ],
        }

    def test_enabling_flips_the_same_shape(self):
        # Only the disable direction was captured; the enable direction is the
        # identical body with enabled=true, which is why they share a builder.
        service, _ = _service()
        payload = service._build_toggle_payload([1, 2], active=True)
        assert all(entry["enabled"] is True for entry in payload["routes"])

    def test_a_bulk_enable_states_the_target_state_it_does_not_flip(self):
        """Whether a refused replacement may be undone automatically turns on
        this one property.

        The body names each row's DESIRED state (`enabled: true`), so sending a
        row that is already on is a no-op. Were it a flip -- "toggle these ids"
        -- the same request would switch OFF every row a concurrent session had
        already put back, and the run would have to prove every old row was
        still disabled before it dared write. It cannot prove that: the page it
        read is a moment old. So the restore would not be safe at all.
        """
        service, client = _service()
        routes = [
            ExistingRoute(route_id=i, dest_village_id=20044, dest_x=5, dest_y=6, active=False)
            for i in (11, 12)
        ]
        asyncio.run(service.enable_routes(20031, routes))

        (verb, url, body) = client.sent[0]
        assert (verb, url) == ("PUT", "/api/v1/trade-routes")
        assert body["routes"] == [{"enabled": True, "id": 11}, {"enabled": True, "id": 12}]
        # Every entry carries the state, so the request is idempotent: sending
        # it twice leaves the same rows on.
        assert all(set(entry) == {"enabled", "id"} for entry in body["routes"]), body

    def test_it_is_a_PUT_to_the_collection_not_a_toggle_group_POST(self):
        # The earlier guess was POST /api/v1/trade-routes/toggle-group. Both
        # the verb and the path were wrong.
        service, client = _service()
        routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=5, dest_y=6)]
        asyncio.run(service.disable_routes(20031, routes))
        assert [(verb, url) for verb, url, _ in client.sent] == [("PUT", "/api/v1/trade-routes")]

    def test_every_route_goes_in_one_request(self):
        service, client = _service()
        routes = [
            ExistingRoute(route_id=i, dest_village_id=20044, dest_x=0, dest_y=0) for i in range(24)
        ]
        asyncio.run(service.disable_routes(20031, routes))
        assert len(client.sent) == 1, "the capture toggled 24 routes in a single request"
        assert len(client.sent[0][2]["routes"]) == 24

    def test_nothing_is_sent_when_there_is_nothing_to_toggle(self):
        service, client = _service()
        assert asyncio.run(service.disable_routes(20031, [])) is None
        assert client.sent == []


class TestWritesConsumeActivityBudget:
    """A trade-route write costs time against the daily activity ceiling.

    The ceiling is what keeps the account's total daily traffic inside a human
    range. A whole execute run that reported zero seconds would let the rest of
    the day's automation spend a budget it had already used -- and because
    _log_activity swallows its own failures by design (accounting must never
    break a request that already went out), nothing surfaces when it silently
    stops working. Hence an explicit assertion.
    """

    def test_a_create_reports_the_time_it_took(self):
        service, client = _service()
        asyncio.run(service.create_route(_route()))

        assert len(client.logged_activity) == 1, "the create must be accounted for"
        assert client.logged_activity[0] >= 0.0

    def test_a_toggle_reports_the_time_it_took(self):
        service, client = _service()
        routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=5, dest_y=6)]
        asyncio.run(service.disable_routes(20031, routes))

        assert len(client.logged_activity) == 1

    def test_a_refused_write_costs_nothing(self):
        # Nothing was sent, so nothing was consumed.
        client = _RecordingClient()
        service = TradeRouteService(client, live_enabled=False, reconciler_verified=True)
        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(service.create_route(_route()))

        assert client.logged_activity == []

    @pytest.mark.parametrize(
        "verb,invoke",
        [
            ("post_json", lambda s: s.create_route(_route())),
            ("put_json", lambda s: s.disable_routes(20031, _existing())),
            ("put_json", lambda s: s.enable_routes(20031, _existing())),
            (
                "put_json",
                lambda s: s.update_cargo(
                    20031, _existing(), {Resource.CROP: 1}, dest_x=23, dest_y=88
                ),
            ),
            ("delete_json", lambda s: s.delete_routes(20031, _existing())),
        ],
        ids=["create", "disable", "enable", "cargo-update", "delete"],
    )
    def test_a_failed_write_reports_the_time_it_took_too(self, verb, invoke):
        """A write that failed spent exactly what one that worked spent.

        The same request left the machine and the same throttler gap was
        waited out; the game simply answered badly. Billing only the successes
        made an execute run report a fraction of the traffic it really spent,
        and the ceiling is SHARED with the farm-list and oasis loops -- so
        under-reporting here is a licence for those to overspend. The module
        already makes this argument for the reads in open_marketplace.
        """
        client = _RecordingClient()

        async def refuse(*_args, **_kwargs):
            raise NetworkError("HTTP 500: the game said no")

        setattr(client, verb, refuse)
        service = TradeRouteService(client, live_enabled=True, reconciler_verified=True)

        result = asyncio.run(invoke(service))

        assert result.status == "failed"
        assert len(client.logged_activity) == 1, "a failed write is not a free write"
        assert client.logged_activity[0] >= 0.0

    def test_a_gold_club_refusal_is_billed_as_well(self):
        # "skipped" describes the ROUTE, not the traffic: the create went out
        # and came back refused, at full price.
        client = _RecordingClient()

        async def refuse(*_args, **_kwargs):
            raise NetworkError("HTTP 400: plus.error_goldclub")

        client.post_json = refuse
        service = TradeRouteService(client, live_enabled=True, reconciler_verified=True)

        result = asyncio.run(service.create_route(_route()))

        assert result.status == "skipped"
        assert len(client.logged_activity) == 1

    def test_a_write_that_fails_in_an_unforeseen_way_is_billed_as_well(self):
        # Billing hung on the NetworkError branch alone would keep missing
        # whatever the transport raises next, so it hangs on leaving the
        # request instead -- however that happens.
        client = _RecordingClient()

        async def expire(*_args, **_kwargs):
            raise SessionExpiredError("the session went away mid-write")

        client.post_json = expire
        service = TradeRouteService(client, live_enabled=True, reconciler_verified=True)

        with pytest.raises(SessionExpiredError):
            asyncio.run(service.create_route(_route()))

        assert len(client.logged_activity) == 1


class TestEveryPlannableCycleIsLegalInGame:
    """`repeatEvery` is a dropdown, not a free integer.

    Read off the live create dialog:

        <select name="repeatEvery">
          <option value="24">24 hour(s)</option> ... <option value="1">1 hour(s)</option>
          <option value="0">Send only once</option>

    So the game accepts only the divisors of 24. A plan built on a cycle outside
    that set could never be created, and the failure would arrive one route at a
    time against a live account rather than at planning time -- so the planner's
    own cycle set has to stay a subset of this one.
    """

    # Verbatim from the dialog, excluding 0 ("Send only once"), which is not a cycle.
    GAME_CYCLES = frozenset({1, 2, 3, 4, 6, 8, 12, 24})

    def test_the_planner_cannot_choose_a_cycle_the_game_rejects(self):
        from travian_api.services.distribution.merchants import DAILY_BEAT_CYCLES

        illegal = set(DAILY_BEAT_CYCLES) - self.GAME_CYCLES
        assert not illegal, (
            f"the planner would emit repeatEvery values the dialog has no option for: {sorted(illegal)}"
        )

    def test_the_payload_carries_the_cycle_as_repeat_every(self):
        service, _ = _service()
        for cycle in sorted(self.GAME_CYCLES):
            payload = service._build_create_payload(_route(cycle_hours=cycle))
            assert payload["repeatEvery"] == cycle


class TestTheDeleteBodyMatchesTheClient:
    """Read out of the game's own marketplace bundle, not guessed.

        Travian.api("trade-routes", {data: {action: "traderoute", routes: k}},
                    "DELETE")
        // k = e.routes ?? []   -- a flat array of route IDS

    The shape difference from the toggle is the trap: the bulk PUT sends
    ``routes: [{enabled, id}]`` -- objects -- and DELETE sends ``routes: [id]``
    -- bare ints. Same endpoint, same key, different element type, and a 200
    cannot tell you which one the server wanted.
    """

    def test_the_routes_are_bare_ids_not_objects(self):
        service, _ = _service()
        payload = service._build_delete_payload([671232, 671231])

        assert payload["routes"] == [671231, 671232]
        assert all(isinstance(r, int) for r in payload["routes"])

    def test_it_carries_the_traderoute_action_like_every_other_call(self):
        service, _ = _service()
        assert service._build_delete_payload([1])["action"] == "traderoute"

    def test_it_sends_exactly_two_keys(self):
        service, _ = _service()
        assert set(service._build_delete_payload([1])) == {"action", "routes"}

    def test_every_route_goes_in_one_request(self):
        service, client = _service()
        routes = [
            ExistingRoute(route_id=i, dest_village_id=20044, dest_x=0, dest_y=0) for i in range(12)
        ]
        asyncio.run(service.delete_routes(20031, routes))

        assert len(client.sent) == 1, "the UI deletes the whole selection at once"
        verb, url, payload = client.sent[0]
        assert verb == "DELETE"
        assert url == "/api/v1/trade-routes"
        assert payload["routes"] == list(range(12))

    def test_deleting_nothing_sends_nothing(self):
        service, client = _service()
        assert asyncio.run(service.delete_routes(20031, [])) is None
        assert client.sent == []

    def test_a_delete_is_refused_without_the_live_opt_in(self):
        client = _RecordingClient()
        service = TradeRouteService(client, live_enabled=False, reconciler_verified=True)
        routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=0, dest_y=0)]

        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(service.delete_routes(20031, routes))
        assert client.sent == [], "the one destructive call must not slip the gate"

    def test_a_delete_consumes_activity_budget(self):
        service, client = _service()
        routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=0, dest_y=0)]
        asyncio.run(service.delete_routes(20031, routes))

        assert len(client.logged_activity) == 1


class TestTheDeleteReadsItsOwnResponse:
    """The one destructive call read nothing back from its own answer.

    `delete_routes` discarded `delete_json`'s return, so every 2xx was
    `deleted` -- including a body naming per-route errors, an HTML soft-block,
    or anything that is not an object at all. Both callers verify by re-reading
    the marketplace, but the execute path only re-read on `status == "deleted"`,
    which this always was: one call site away from being the only guard.
    """

    def _service_returning(self, body):
        client = _RecordingClient()

        async def _delete(url, *, data=None, **kw):
            client.sent.append(("DELETE", url, data))
            return body

        client.delete_json = _delete
        return TradeRouteService(client, live_enabled=True, reconciler_verified=True), client

    def _routes(self, n=3):
        return [
            ExistingRoute(route_id=i, dest_village_id=20044, dest_x=0, dest_y=0)
            for i in range(1, n + 1)
        ]

    def test_a_clean_response_is_a_clean_delete(self):
        service, _ = self._service_returning({"routes": [{"id": 1}, {"id": 2}, {"id": 3}]})
        assert asyncio.run(service.delete_routes(20031, self._routes())).status == "deleted"

    def test_a_rejected_route_makes_the_whole_delete_a_failure(self):
        service, _ = self._service_returning(
            {"routes": [{"id": 1}, {"id": 2, "error": "nope"}, {"id": 3}]}
        )
        result = asyncio.run(service.delete_routes(20031, self._routes()))

        assert result.status == "failed"
        assert "1 of 3" in result.detail
        assert "[2]" in result.detail

    def test_a_body_we_cannot_read_is_unverified_not_deleted(self):
        # Same third verdict the toggles carry, for the same reason: the request
        # returned success, so the rows are probably gone -- and "probably" is
        # not "deleted". The caller re-reads the page and decides from state.
        for body in ({}, None, {"other": 1}, "not json at all"):
            service, _ = self._service_returning(body)
            result = asyncio.run(service.delete_routes(20031, self._routes()))
            assert result.status == "unverified", f"body {body!r} must not read as success"
            assert "cannot be confirmed" in result.detail


class TestTheBulkToggleReadsItsOwnResponse:
    """The toggle, unlike the create, answers with a body -- and the game's own
    client reads it, counting entries with an `error`. Ignoring it meant a
    request where some routes were accepted and others rejected reported a clean
    success for all of them: a per-route failure hidden behind an overall 200."""

    def _service_returning(self, body):
        client = _RecordingClient()

        async def _put(url, payload, **kw):
            client.sent.append(("PUT", url, payload))
            return body

        client.put_json = _put
        return TradeRouteService(client, live_enabled=True, reconciler_verified=True), client

    def _routes(self, n=3):
        return [
            ExistingRoute(route_id=i, dest_village_id=20044, dest_x=0, dest_y=0)
            for i in range(1, n + 1)
        ]

    def test_a_clean_response_is_a_clean_disable(self):
        service, _ = self._service_returning({"routes": [{"id": 1}, {"id": 2}, {"id": 3}]})
        result = asyncio.run(service.disable_routes(20031, self._routes()))
        assert result.status == "disabled"

    def test_a_rejected_route_makes_the_whole_toggle_a_failure(self):
        # Reported as failed, not partially-done: the caller's next decision
        # (do NOT create new routes on top of a bad disable) depends on it.
        service, _ = self._service_returning(
            {"routes": [{"id": 1}, {"id": 2, "error": "nope"}, {"id": 3}]}
        )
        result = asyncio.run(service.disable_routes(20031, self._routes()))

        assert result.status == "failed"
        assert "1 of 3" in result.detail
        assert "[2]" in result.detail

    def test_a_readable_body_naming_no_failures_is_a_clean_disable(self):
        # Absence of evidence must not become evidence of failure. `routes: []`
        # is a body we CAN read that names no rejection, so it is success.
        service, _ = self._service_returning({"routes": []})
        result = asyncio.run(service.disable_routes(20031, self._routes()))
        assert result.status == "disabled"

    def test_a_body_we_cannot_read_is_not_reported_as_a_disable(self):
        # RE-SEEDED TWICE. It first asserted that {}, None, {"other": 1} and a
        # bare string all read as "disabled". That conflated two answers the
        # rest of this module is careful to keep apart -- a body naming no
        # failures, and a body we could not read at all -- and the caller reads
        # a clean disable as "all N are off". On the revert path that reported
        # twenty-four rows disabled while twenty-four rows kept shipping.
        #
        # It then asserted "failed", which is the other over-statement: the
        # request returned SUCCESS and the rows are probably off, so a verdict
        # of failure sent /execute to defer the origin and made /revert-plan
        # report created routes as "STILL RUNNING" when the game had switched
        # them off. `unverified` is the third answer, and it is the one the
        # create path already has for identical evidence
        # (`created_unverified`). The caller looks at the marketplace.
        #
        # The original rationale is preserved above, on the case it actually
        # describes: `routes: []` is still success.
        for body in ({}, None, {"other": 1}, "not json at all"):
            service, _ = self._service_returning(body)
            result = asyncio.run(service.disable_routes(20031, self._routes()))
            assert result.status == "unverified", f"body {body!r} must not read as success"
            assert result.status != "disabled", f"body {body!r} is not evidence of a disable"
            assert "cannot be confirmed" in result.detail

    def test_a_rejection_and_an_unreadable_body_are_different_verdicts(self):
        """The distinction the whole vocabulary rests on.

        A body naming a rejection is the game saying NO -- nothing to look at,
        the caller must not create on top. A body that cannot be read is the
        game saying yes in a shape nobody has observed; `docs/15` records the
        create's 200 body as EMPTY and records that `routes[].error` came off
        the game's own `main.js` rather than an observed reply, so on an
        account that has never run live "unreadable" may well be every single
        toggle. Collapsing the two made every disable, enable and cargo update
        a failure.
        """
        rejected, _ = self._service_returning({"routes": [{"id": 2, "error": "nope"}]})
        unreadable, _ = self._service_returning({})

        assert asyncio.run(rejected.disable_routes(20031, self._routes())).status == "failed"
        assert asyncio.run(unreadable.disable_routes(20031, self._routes())).status == "unverified"

    def test_an_unreadable_disable_sends_the_operator_to_look(self):
        # The asymmetry: a disable that may not have happened leaves resources
        # moving, so the message has to say so rather than offering a retry.
        service, _ = self._service_returning({})
        result = asyncio.run(service.disable_routes(20031, self._routes()))
        assert "still be SHIPPING" in result.detail
        assert "checked in-game" in result.detail

    def test_an_unreadable_enable_says_a_later_run_can_repair_it(self):
        service, _ = self._service_returning({})
        result = asyncio.run(service.enable_routes(20031, self._routes()))
        assert result.status == "unverified"
        assert "re-enable" in result.detail
        assert "SHIPPING" not in result.detail, "an enable leaves nothing moving"

    def test_a_rejection_without_a_usable_id_does_not_crash_the_run(self):
        service, _ = self._service_returning(
            {"routes": [{"error": "no id here"}, {"id": "x", "error": "bad id"}]}
        )
        result = asyncio.run(service.disable_routes(20031, self._routes()))
        assert result.status == "disabled", "unnameable rejections cannot be acted on"


class TestTheUpdateBodyMatchesTheClient:
    """The bulk-edit branch of the game's own bundle, verbatim:

        s = {}; t && (s.resources = {lumber, clay, iron, crop})
        k.forEach(e => i.push({...s, id: e}))
        d = {routes: i}

    Only changed fields are sent, and note what is absent from that list:
    ``hour`` and ``minute``. The bulk form CANNOT move a departure time -- only
    ``PUT trade-routes/{id}`` can. That is load-bearing here rather than a
    limitation: what the operator calls one route is 24/N rows at staggered
    times, and correcting their cargo must not collapse them onto one clock.
    """

    def test_each_route_carries_every_field_a_real_bulk_edit_sends(self):
        # The dialog seeds itself with every field the selection AGREES on, and
        # every row here belongs to one destination and one create -- so they
        # agree on all of them. Sending only `resources` was four fields short.
        service, _ = _service()
        payload = service._build_update_payload([9, 7], {Resource.CROP: 1440}, dest_x=23, dest_y=88)

        expected = {
            "targetCoordinates": {"x": 23, "y": 88},
            "resources": {"lumber": 0, "clay": 0, "iron": 0, "crop": 1440},
            "deliveries": 1,
            "enabled": True,
            "useTradeShips": False,
        }
        assert payload["action"] == "traderoute"
        assert payload["routes"] == [{**expected, "id": 7}, {**expected, "id": 9}]

    def test_it_never_sends_a_departure_time(self):
        # Sending hour/minute here would be both unsupported by the bulk form and
        # actively destructive: every row of a fanned-out route would collapse
        # onto one clock.
        service, _ = _service()
        payload = service._build_update_payload([1, 2, 3], {Resource.CROP: 5}, dest_x=23, dest_y=88)
        for entry in payload["routes"]:
            assert "hour" not in entry and "minute" not in entry
        assert "hour" not in payload and "minute" not in payload

    def test_all_four_resources_are_always_present(self):
        service, _ = _service()
        entry = service._build_update_payload([1], {Resource.IRON: 10}, dest_x=23, dest_y=88)[
            "routes"
        ][0]
        assert entry["resources"] == {"lumber": 0, "clay": 0, "iron": 10, "crop": 0}

    def test_every_row_is_corrected_in_one_request(self):
        service, client = _service()
        routes = [
            ExistingRoute(route_id=800 + i, dest_village_id=20044, dest_x=0, dest_y=0)
            for i in range(24)
        ]
        asyncio.run(service.update_cargo(20031, routes, {Resource.CROP: 60}, dest_x=23, dest_y=88))

        assert len(client.sent) == 1, "a fanned-out route is corrected in one call"
        verb, url, payload = client.sent[0]
        assert (verb, url) == ("PUT", "/api/v1/trade-routes")
        assert len(payload["routes"]) == 24

    def test_updating_nothing_sends_nothing(self):
        service, client = _service()
        assert (
            asyncio.run(service.update_cargo(20031, [], {Resource.CROP: 1}, dest_x=23, dest_y=88))
            is None
        )
        assert client.sent == []

    def test_an_update_is_refused_without_the_live_opt_in(self):
        client = _RecordingClient()
        service = TradeRouteService(client, live_enabled=False, reconciler_verified=True)
        routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=0, dest_y=0)]

        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(
                service.update_cargo(20031, routes, {Resource.CROP: 1}, dest_x=23, dest_y=88)
            )
        assert client.sent == []

    def test_a_rejected_row_makes_the_whole_update_a_failure(self):
        # Same reasoning as the toggle: the caller must not believe the live
        # routes now match the plan when some of them still do not.
        client = _RecordingClient()

        async def _put(url, payload, **kw):
            client.sent.append(("PUT", url, payload))
            return {"routes": [{"id": 1}, {"id": 2, "error": "nope"}]}

        client.put_json = _put
        service = TradeRouteService(client, live_enabled=True, reconciler_verified=True)
        routes = [
            ExistingRoute(route_id=i, dest_village_id=20044, dest_x=0, dest_y=0) for i in (1, 2)
        ]

        result = asyncio.run(
            service.update_cargo(20031, routes, {Resource.CROP: 1}, dest_x=23, dest_y=88)
        )
        assert result.status == "failed"
        assert "[2]" in result.detail

    def test_an_unreadable_body_is_unverified_not_failed(self):
        # The same evidence `_toggle_routes` calls `unverified`, on the same
        # endpoint, in the same request shape -- and `docs/15` records the empty
        # 200 as the NORMAL body here, so `failed` was the expected outcome of
        # every cargo correction this app makes. It made `update_drifted` report
        # "0 corrected" and "still shipping the old amounts" over rows it had
        # just rewritten.
        for body in ({}, None, {"other": 1}, "not json at all"):
            client = _RecordingClient()

            async def _put(url, payload, _body=body, _client=client, **kw):
                _client.sent.append(("PUT", url, payload))
                return _body

            client.put_json = _put
            service = TradeRouteService(client, live_enabled=True, reconciler_verified=True)
            routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=0, dest_y=0)]

            result = asyncio.run(
                service.update_cargo(20031, routes, {Resource.CROP: 1}, dest_x=23, dest_y=88)
            )
            assert result.status == "unverified", f"body {body!r}"
            assert "cannot be confirmed" in result.detail
