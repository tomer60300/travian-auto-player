"""Reading existing trade routes off a real marketplace page.

The fixture is an anonymised capture of a live Europe 2 marketplace, gid=17
tab t=3 (gpack 597.6): real structure, real field names, real counts, with
village ids, route ids, names, map ids and timestamps replaced.

Why JSON and not markup: the page hands React a complete model of the village's
routes, and that payload is what the page itself runs on, so it survives the
cosmetic churn a gpack brings to classes and table layout. The previous parser
scraped `data-route-id` with `data-x`/`data-y` on the same element. Against the
real page that found ZERO routes -- the attribute is `data-trade-route-id`, and
coordinates appear nowhere on the page in any form.
"""

from pathlib import Path

import pytest

from travian_api.parsers.html_parser import (
    coords_to_map_id,
    map_id_to_coords,
    parse_trade_routes,
    trade_route_page_recognised,
)

FIXTURE = Path(__file__).parent / "fixtures" / "marketplace_trade_routes.html"


@pytest.fixture(scope="module")
def page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class TestTheRealPage:
    def test_every_route_is_found(self, page):
        # 6 destination collections holding 83 scheduled departures between them.
        routes = parse_trade_routes(page)
        assert len(routes) == 83
        assert len({r["dest_village_id"] for r in routes}) == 6

    def test_route_ids_are_unique(self, page):
        routes = parse_trade_routes(page)
        assert len({r["route_id"] for r in routes}) == len(routes)

    def test_the_enabled_flag_is_read_not_assumed(self, page):
        # `active` comes from the model's own `enabled`, which is what decides
        # whether a route the plan still wants needs re-enabling rather than
        # creating. The old parser guessed this from CSS classes.
        assert all(r["active"] is True for r in parse_trade_routes(page))

    def test_the_destination_carries_both_a_village_id_and_coordinates(self, page):
        # The page itself has no coordinates -- it names a destination by
        # village id and map id. The coordinates are DERIVED from the map id,
        # which is what lets a route read off the page be matched against a plan
        # expressed in coordinates.
        for route in parse_trade_routes(page):
            assert isinstance(route["dest_village_id"], int)
            assert isinstance(route["dest_x"], int) and isinstance(route["dest_y"], int)

    def test_the_map_id_conversion_matches_captured_ground_truth(self):
        # A captured create request targeted (23|88). The marketplace page then
        # listed that destination with mapId 45136, and the formula agrees --
        # so this is checked against the game, not against itself.
        assert coords_to_map_id(23, 88) == 45136
        assert map_id_to_coords(45136) == (23, 88)

    @pytest.mark.parametrize("x", [-200, -37, 0, 23, 200])
    @pytest.mark.parametrize("y", [-200, -41, 0, 88, 200])
    def test_the_conversion_round_trips_across_the_map(self, x, y):
        assert map_id_to_coords(coords_to_map_id(x, y)) == (x, y)

    @pytest.mark.parametrize("bad", [0, -1, 401 * 401 + 1])
    def test_an_impossible_map_id_yields_no_coordinates(self, bad):
        # None rather than a wrong pair: a bogus coordinate would match the
        # wrong destination, which is worse than matching nothing.
        assert map_id_to_coords(bad) is None

    def test_cargo_carries_all_four_resources(self, page):
        for route in parse_trade_routes(page):
            assert set(route["cargo"]) == {"lumber", "clay", "iron", "crop"}
            assert all(isinstance(v, int) for v in route["cargo"].values())

    def test_the_schedule_fields_are_present(self, page):
        routes = parse_trade_routes(page)
        # Pins what the page SAYS, not the route's real cadence: 82 of the 83
        # rows claim 1 while their departure spacing proves 2h, 3h and 12h
        # cycles. See tests/test_real_page_fanout.py -- the reconciler derives
        # cadence from departure_at and must never read this field.
        assert {r["repeat_hours"] for r in routes} == {1, 2}
        assert all(isinstance(r["merchants"], int) for r in routes)


class TestUnreadablePages:
    """Empty and unreadable must be distinguishable -- that difference is the
    whole reason creating routes is gated."""

    @pytest.mark.parametrize(
        "html",
        [
            "",
            "<html><body>nothing here</body></html>",
            "<html><body><table><tr data-route-id='1'></tr></table></body></html>",
            "<html><script>TradeRoutes.render({viewData: {broken</script></html>",
            "<html><script>TradeRoutes.render({viewData: {}})</script></html>",
        ],
    )
    def test_an_unreadable_page_yields_nothing(self, html):
        assert parse_trade_routes(html) == []

    @pytest.mark.parametrize(
        "html",
        [
            "",
            "<html><body>nothing</body></html>",
            "<html><script>TradeRoutes.render({viewData: {broken</script></html>",
        ],
    )
    def test_and_says_it_was_not_recognised(self, html):
        assert trade_route_page_recognised(html) is False

    def test_a_real_page_with_no_routes_IS_recognised(self):
        # The case the gate must eventually allow: a readable page that
        # genuinely has nothing on it.
        empty = (
            "<script>window.Travian.React.TradeRoutes.render({viewData: "
            '{"ownPlayer":{"id":1,"currentVillageId":2,'
            '"village":{"marketplace":{"tradeRoutes":[]}}}}'
            "})</script>"
        )
        assert trade_route_page_recognised(empty) is True
        assert parse_trade_routes(empty) == []

    def test_the_fixture_carries_no_real_identifiers(self, page):
        # The repo is public; the capture came from a live account.
        for real in ("3866", "53629", "61837", "30540", "51542"):
            assert real not in page
