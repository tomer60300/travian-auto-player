"""The planner's setup document, stored server-side instead of per origin.

`localStorage` is scoped to an ORIGIN, so the same app on :80, on :8001, on the
LAN address and over Tailscale keeps four independent copies of everything the
operator typed. These endpoints are the shared copy. What they must guarantee:

* the document round trips VERBATIM -- the frontend's `buildSetup` deliberately
  omits a field it has no answer for, and a store that wrote defaults back in
  would turn "nothing declared" into a declaration;
* a document the planner would later REFUSE is refused now, with the planner's
  own message. Saved and discovered a week later, an unusable setup is a trap;
* one user's rows are invisible to another, because the document carries the
  operator's village names, coordinates and topology.

Driven over real HTTP rather than by calling the handlers, for the reasons
`test_distribution_http_contract` sets out: the status codes, the query
parameter binding and the 422 shape only exist on the wire.

Every test owns its own account key. `-n 8` distributes individual tests, and
each worker gets its own throwaway database, so a test that leaned on another's
rows would pass or fail on which worker happened to take it.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from travian_api.web.auth import get_current_user

# Whose request the auth override answers with. Mutated per test rather than
# minting two tokens: this is a test of the SETUP contract, and login has its
# own tests.
_CALLER = {"id": 1}

SETUP = "/api/distribution/setup"


@pytest.fixture(scope="module")
def client():
    from travian_api.web.app import app

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=_CALLER["id"])
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _one_caller():
    _CALLER["id"] = 1
    yield
    _CALLER["id"] = 1


def _as(user_id):
    _CALLER["id"] = user_id


@pytest.fixture
def account(request):
    """A key nothing else in the suite writes to."""
    return f"https://example.travian.test|{request.node.name}"


def _put(client, account, doc):
    return client.put(SETUP, params={"account_key": account}, json=doc)


def _get(client, account):
    return client.get(SETUP, params={"account_key": account})


def _delete(client, account):
    return client.delete(SETUP, params={"account_key": account})


def _minimal(account, **extra):
    """The smallest document the frontend can write: nothing typed anywhere."""
    return {
        "format": "travian-planner-owned-state",
        "version": 6,
        "exported_at": "2026-09-03T08:15:00.000Z",
        "account": account,
        "villages": [],
        **extra,
    }


def _realistic(account):
    """A whole account's typed state, in `plannerSetup.js`'s own shape.

    Nine villages, five role templates, a relay tier drawn from a feeder and a
    village with no role, a Day/Night profile pair with windows, the merchant
    model and one foreign tribute. Built the way `buildSetup` writes it: a
    field the operator never answered is ABSENT, not null.
    """
    return {
        "format": "travian-planner-owned-state",
        "version": 6,
        "exported_at": "2026-09-03T08:15:00.000Z",
        "account": account,
        "villages": [
            {
                "village_id": 1,
                "name": "01 Hammer",
                "role": "full_off",
                "trade_office_level": 10,
                "max_busy_merchants": 8,
                "consumption_per_hour": {"lumber": 14750, "clay": 12100, "iron": 9800},
            },
            {
                "village_id": 2,
                "name": "02 Capital",
                "role": "capital",
                "trade_office_level": 20,
                "max_busy_merchants": 8,
                "crop_ceiling": 750000,
                "ship_only_to": [4, 5],
                "stock_floor_fraction": 0.6,
            },
            {
                "village_id": 3,
                "name": "03 Troops",
                "role": "troops_off",
                "trade_office_level": 5,
                "may_relay": False,
                "consumption_per_hour": {"lumber": 8300, "clay": 7100, "iron": 6400},
            },
            {
                "village_id": 4,
                "name": "04 Relay",
                "role": "feeder",
                "trade_office_level": 3,
                "max_busy_merchants": 6,
                "relay_for": [11, 13],
            },
            {
                "village_id": 5,
                "name": "05 Relay",
                "trade_office_level": 2,
                "relay_for": [17, 19],
                "may_relay": True,
            },
            {
                "village_id": 11,
                "name": "11 Def",
                "role": "def",
                "trade_office_level": 4,
                "stock_floor_fraction": 0.25,
            },
            {
                "village_id": 13,
                "name": "13 Def",
                "role": "def",
                "trade_office_level": 4,
                "ship_only_to": [],
            },
            {"village_id": 17, "name": "17 Def", "role": "def", "trade_office_level": 4},
            {"village_id": 19, "name": "19 Def", "role": "def", "trade_office_level": 4},
        ],
        "roles": {
            "capital": {
                "allocations": {
                    "lumber": {"mode": "absolute", "value": 0},
                    "crop": {"mode": "absolute", "value": 0},
                },
                "consumption": {},
                "may_relay": None,
                "crop_negative_by_design": False,
            },
            "full_off": {
                "allocations": {"crop": {"mode": "absolute", "value": 0}},
                "consumption": {},
                "may_relay": None,
                "crop_negative_by_design": True,
            },
            "troops_off": {
                "allocations": {"crop": {"mode": "absolute", "value": 0}},
                "consumption": {},
                "may_relay": None,
                "crop_negative_by_design": True,
            },
            "def": {
                "allocations": {
                    "lumber": {"mode": "absolute", "value": 8372},
                    "clay": {"mode": "absolute", "value": 8372},
                    "iron": {"mode": "absolute", "value": 8372},
                    "crop": {"mode": "absolute", "value": 4000},
                },
                "consumption": {"lumber": 6200, "clay": 6200, "iron": 6200},
                "may_relay": None,
                "crop_negative_by_design": False,
            },
            "feeder": {
                "allocations": {"lumber": {"mode": "keep", "value": 0}},
                "consumption": {},
                "may_relay": None,
                "crop_negative_by_design": False,
            },
        },
        "profiles": {
            "Day": {
                "lumber": {
                    "2": {"mode": "remainder", "value": 0},
                    "11": {"mode": "absolute", "value": 8372},
                },
                "crop": {
                    "1": {"mode": "absolute", "value": 0},
                    "2": {"mode": "remainder", "value": 0},
                },
            },
            "Night": {
                "lumber": {
                    "2": {"mode": "absolute", "value": -12000},
                    "11": {"mode": "percentage", "value": 25},
                },
                "crop": {"2": {"mode": "sustain", "value": 10}},
            },
        },
        "profile_windows": {"Day": ["06:00", "22:00"], "Night": ["22:00", "06:00"]},
        "merchant_model": {
            "base_capacity": 2500,
            "bonus_per_to_level": 0.2,
            "merchant_reserve": 2,
            "merchant_headroom": 0.15,
        },
        "foreign_targets": [
            {
                "name": "ally hub",
                "x": 12,
                "y": -34,
                "crop_per_hour": 25700,
                "safety_margin_pct": 5,
                "route_eligible": False,
                "max_cycle_hours": 8,
                "exclude_origins": [1],
            }
        ],
    }


class TestTheRoundTrip:
    def test_nothing_saved_is_404_not_an_empty_setup(self, client, account):
        # An empty setup and no setup are different states: the page must be
        # able to tell "you have never saved" from "you saved a blank sheet".
        assert _get(client, account).status_code == 404

    def test_a_realistic_document_returns_verbatim(self, client, account):
        doc = _realistic(account)

        saved = _put(client, account, doc)
        assert saved.status_code == 200, saved.text

        got = _get(client, account)
        assert got.status_code == 200, got.text
        body = got.json()
        # VERBATIM. Not a model dump: `buildSetup` omits a field it has no
        # answer for, and a store that wrote `may_relay: null` back onto every
        # row would turn silence into a declaration.
        assert body["setup"] == doc
        assert body["account_key"] == account
        assert body["saved_at"]

    def test_the_put_response_carries_what_get_would(self, client, account):
        doc = _realistic(account)

        saved = _put(client, account, doc).json()

        assert saved == _get(client, account).json()

    def test_the_empty_document_round_trips_too(self, client, account):
        assert _put(client, account, _minimal(account)).status_code == 200

        assert _get(client, account).json()["setup"] == _minimal(account)

    def test_a_second_put_replaces_rather_than_accumulating(self, client, account):
        assert _put(client, account, _realistic(account)).status_code == 200
        replacement = _minimal(
            account, villages=[{"village_id": 7, "name": "07", "trade_office_level": 1}]
        )

        assert _put(client, account, replacement).status_code == 200

        assert _get(client, account).json()["setup"] == replacement

    def test_the_same_put_twice_is_idempotent(self, client, account):
        doc = _realistic(account)

        first = _put(client, account, doc)
        second = _put(client, account, doc)

        assert first.status_code == second.status_code == 200
        assert _get(client, account).json()["setup"] == doc

    def test_delete_forgets_it(self, client, account):
        assert _put(client, account, _realistic(account)).status_code == 200

        assert _delete(client, account).status_code == 204
        assert _get(client, account).status_code == 404

    def test_deleting_nothing_is_404(self, client, account):
        assert _delete(client, account).status_code == 404

    def test_two_accounts_keep_separate_setups(self, client, account):
        other = f"{account}-second-world"
        assert _put(client, account, _minimal(account)).status_code == 200
        theirs = _minimal(other, villages=[{"village_id": 9, "name": "09"}])
        assert _put(client, other, theirs).status_code == 200

        assert _get(client, account).json()["setup"] == _minimal(account)
        assert _get(client, other).json()["setup"] == theirs


class TestOnePerUser:
    """The document holds the operator's village names, coordinates and
    topology. It is personal data, and it never crosses users."""

    def test_another_user_cannot_read_it(self, client, account):
        assert _put(client, account, _realistic(account)).status_code == 200

        _as(2)
        assert _get(client, account).status_code == 404

    def test_another_user_cannot_overwrite_it(self, client, account):
        mine = _realistic(account)
        assert _put(client, account, mine).status_code == 200

        _as(2)
        theirs = _minimal(account, villages=[{"village_id": 99, "name": "not yours"}])
        assert _put(client, account, theirs).status_code == 200

        _as(1)
        assert _get(client, account).json()["setup"] == mine

    def test_another_user_cannot_delete_it(self, client, account):
        assert _put(client, account, _realistic(account)).status_code == 200

        _as(2)
        assert _delete(client, account).status_code == 404

        _as(1)
        assert _get(client, account).status_code == 200


class TestTheVersion:
    def test_a_newer_version_says_so(self, client, account):
        # 10, not 9: v9 became readable when the reserved NPC-burst window
        # started travelling in the document. This case needs a version that is
        # guaranteed to be beyond this build, so it moves whenever
        # READABLE_VERSIONS grows -- and the parametrised case below is what
        # would fail if the two ever disagreed.
        res = _put(client, account, _minimal(account, version=10))

        assert res.status_code == 422, res.text
        assert "NEWER build" in res.text

    @pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8, 9])
    def test_every_readable_version_is_accepted(self, client, account, version):
        assert _put(client, account, _minimal(account, version=version)).status_code == 200

    def test_the_reserved_window_round_trips(self, client, account):
        # The one owned answer neither persistence path carried. It lived only
        # in localStorage, which is per BROWSER ORIGIN -- so it did not follow
        # the operator between :80, :8001, the LAN address and Tailscale, which
        # is the exact failure the page's own copy warns about.
        doc = _minimal(account, version=9, reserved_window=["20:00", "21:00"])
        assert _put(client, account, doc).status_code == 200, doc

        assert _get(client, account).json()["setup"]["reserved_window"] == ["20:00", "21:00"]

    def test_the_version_is_stored_as_given_not_upgraded(self, client, account):
        assert _put(client, account, _minimal(account, version=1)).status_code == 200

        assert _get(client, account).json()["setup"]["version"] == 1

    def test_a_foreign_format_is_refused(self, client, account):
        res = _put(client, account, _minimal(account, format="some-other-tool"))

        assert res.status_code == 422, res.text


class TestTheAccountGuard:
    def test_a_document_from_another_account_is_refused(self, client, account):
        res = _put(client, account, _minimal("https://example.travian.test|somebody-else"))

        assert res.status_code == 422, res.text
        assert "somebody-else" in res.text

    def test_a_document_naming_no_account_is_adopted(self, client, account):
        # `setupMatchesAccount` reads a null account as "nothing to
        # contradict" -- a file exported before an account was connected.
        assert _put(client, account, _minimal(None)).status_code == 200


class TestWhatThePlannerWouldRefuse:
    """Each refusal carries the plan request's own message, because the
    operator meets the same rule from both doors."""

    def test_a_claimed_role_with_no_template(self, client, account):
        doc = _minimal(account, villages=[{"village_id": 11, "name": "11 Def", "role": "def"}])

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "no role template was sent for" in res.text
        assert "11 Def" in res.text

    def test_a_relay_that_feeds_a_relay(self, client, account):
        doc = _minimal(
            account,
            villages=[
                {"village_id": 4, "name": "04", "relay_for": [5]},
                {"village_id": 5, "name": "05", "relay_for": [11]},
            ],
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "a relay may not feed a relay" in res.text

    def test_a_role_village_as_a_relay(self, client, account):
        doc = _minimal(
            account,
            villages=[{"village_id": 2, "name": "02", "role": "capital", "relay_for": [11]}],
            roles={"capital": {"allocations": {}}},
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "section 5.9" in res.text

    def test_a_relay_for_nobody(self, client, account):
        doc = _minimal(account, villages=[{"village_id": 4, "name": "04", "relay_for": []}])

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "relay for nobody" in res.text

    def test_a_downstream_named_twice_in_one_list(self, client, account):
        doc = _minimal(account, villages=[{"village_id": 4, "name": "04", "relay_for": [11, 11]}])

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "more than once in its relay_for" in res.text

    def test_a_downstream_claimed_by_two_relays(self, client, account):
        doc = _minimal(
            account,
            villages=[
                {"village_id": 4, "name": "04", "relay_for": [11]},
                {"village_id": 5, "name": "05", "relay_for": [11]},
            ],
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "One relay per downstream" in res.text

    def test_a_relay_that_names_itself(self, client, account):
        doc = _minimal(account, villages=[{"village_id": 4, "name": "04", "relay_for": [4]}])

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "its own relay" in res.text

    def test_a_declared_crop_spend(self, client, account):
        doc = _minimal(
            account,
            villages=[{"village_id": 1, "name": "01", "consumption_per_hour": {"crop": 5880}}],
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "cannot include crop" in res.text

    def test_a_declared_crop_spend_in_a_role_template(self, client, account):
        doc = _minimal(account, roles={"def": {"allocations": {}, "consumption": {"crop": 5880}}})

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "cannot include crop" in res.text

    def test_remainder_in_a_role_template(self, client, account):
        doc = _minimal(
            account, roles={"def": {"allocations": {"lumber": {"mode": "remainder", "value": 0}}}}
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "remainder stays per village" in res.text

    def test_a_merchant_cap_above_the_most_a_village_can_hold(self, client, account):
        doc = _minimal(
            account, villages=[{"village_id": 2, "name": "02", "max_busy_merchants": 25}]
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_stock_floor_above_the_ceiling(self, client, account):
        doc = _minimal(
            account, villages=[{"village_id": 2, "name": "02", "stock_floor_fraction": 1.5}]
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_stock_floor_off_the_grid_the_input_types_on(self, client, account):
        # The operator types a percent, whole or to one decimal. A figure off
        # that grid is one the page's own file parser refuses to read back.
        doc = _minimal(
            account, villages=[{"village_id": 2, "name": "02", "stock_floor_fraction": 0.3333}]
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_trade_office_level_past_20(self, client, account):
        doc = _minimal(
            account, villages=[{"village_id": 2, "name": "02", "trade_office_level": 21}]
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_village_id_that_is_not_a_village_id(self, client, account):
        doc = _minimal(account, villages=[{"village_id": 0, "name": "nowhere"}])

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_whitelist_entry_that_is_not_a_village_id(self, client, account):
        doc = _minimal(account, villages=[{"village_id": 2, "name": "02", "ship_only_to": [0]}])

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_window_that_is_not_a_clock_time(self, client, account):
        doc = _minimal(account, profile_windows={"Day": ["6am", "22:00"]})

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_reserved_window_that_is_not_a_clock_pair(self, client, account):
        # Same shape and same discipline as `profile_windows` above: a document
        # is the operator asserting an answer, so a malformed pair is refused
        # rather than coerced.
        doc = _minimal(account, version=9, reserved_window=["8pm", "21:00"])

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        # The reason matters: without this the case passed vacuously while v9
        # itself was still being refused as a newer build.
        assert "reserved_window" in res.text, res.text

    def test_a_reserved_window_of_the_wrong_length(self, client, account):
        doc = _minimal(account, version=9, reserved_window=["20:00"])

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        # The reason matters: without this the case passed vacuously while v9
        # itself was still being refused as a newer build.
        assert "reserved_window" in res.text, res.text

    def test_a_merchant_headroom_of_one(self, client, account):
        doc = _minimal(
            account,
            merchant_model={
                "base_capacity": 2500,
                "bonus_per_to_level": 0.2,
                "merchant_headroom": 1.0,
            },
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_tribute_with_a_cadence_travian_cannot_repeat(self, client, account):
        doc = _minimal(
            account,
            foreign_targets=[
                {"name": "ally", "x": 1, "y": 2, "crop_per_hour": 100, "max_cycle_hours": 5}
            ],
        )

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text
        assert "repeat interval" in res.text

    def test_an_unknown_role_name(self, client, account):
        doc = _minimal(account, roles={"hammer": {"allocations": {}}})

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_profile_under_an_empty_name(self, client, account):
        doc = _minimal(account, profiles={"  ": {"lumber": {"2": {"mode": "keep", "value": 0}}}})

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_profile_allocation_mode_the_planner_does_not_have(self, client, account):
        doc = _minimal(account, profiles={"Day": {"lumber": {"2": {"mode": "hoard", "value": 0}}}})

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_refused_document_leaves_the_saved_one_alone(self, client, account):
        doc = _realistic(account)
        assert _put(client, account, doc).status_code == 200

        # 10 for the same reason as TestTheVersion's: v9 is readable now, so a
        # refusal has to be asked for with a version beyond this build.
        assert _put(client, account, _minimal(account, version=10)).status_code == 422

        assert _get(client, account).json()["setup"] == doc

    def test_a_profile_keyed_by_something_that_is_not_a_village(self, client, account):
        doc = _minimal(account, profiles={"Day": {"lumber": {"0": {"mode": "keep", "value": 0}}}})

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_document_with_no_villages_list(self, client, account):
        doc = _minimal(account)
        doc.pop("villages")

        res = _put(client, account, doc)

        assert res.status_code == 422, res.text

    def test_a_body_that_is_not_a_document(self, client, account):
        res = client.put(SETUP, params={"account_key": account}, json=["not", "a", "document"])

        assert res.status_code == 422, res.text


class TestTheAccountKeyIsRequired:
    """Every verb addresses the row the same way. Without a key there is no row
    to address: the planner keys its own state by account, and a village id
    means nothing without one."""

    def test_get_needs_one(self, client):
        assert client.get(SETUP).status_code == 422

    def test_put_needs_one(self, client, account):
        assert client.put(SETUP, json=_minimal(account)).status_code == 422

    def test_delete_needs_one(self, client):
        assert client.delete(SETUP).status_code == 422
