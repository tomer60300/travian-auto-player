"""Each row of a fanned-out route has its own departure, and we must read it.

Travian implements "repeat every N hours" as 24/N separate route rows. They share
a destination, a cargo and a merchant count -- everything the reconciler looks at
-- so nothing in the parsed model told them apart. Their departures do.

That matters because it is the only way to confine a route to a profile's hours.
The game offers no window setting, but the rows are individually addressable: read
the 24, keep the ones that depart inside the window, delete the rest. Without the
departure time there is no way to choose which.
"""

from travian_api.parsers.html_parser import parse_trade_routes

FIXTURE = "tests/fixtures/marketplace_trade_routes.html"


def _rows():
    with open(FIXTURE, encoding="utf-8", errors="replace") as handle:
        return parse_trade_routes(handle.read(), map_span=401)


class TestTheDepartureOfEachRowIsRead:
    def test_rows_carry_a_departure_timestamp(self):
        rows = _rows()
        assert rows, "the fixture must still parse"
        timed = [r for r in rows if r.get("departure_at") is not None]
        assert timed, "the page states departureAt per row; it must not be dropped"

    def test_it_is_an_integer_unix_timestamp(self):
        timed = [r["departure_at"] for r in _rows() if r.get("departure_at") is not None]
        for value in timed:
            assert isinstance(value, int)
            assert value > 1_000_000_000, f"not a plausible unix time: {value}"

    def test_rows_to_the_same_destination_are_told_apart_by_it(self):
        # The whole point. Two rows of one fanned-out route are identical in
        # every other field the reconciler reads.
        by_dest: dict[int, list[dict]] = {}
        for row in _rows():
            by_dest.setdefault(row["dest_village_id"], []).append(row)
        fanned = [rows for rows in by_dest.values() if len(rows) > 1]
        assert fanned, "the fixture should contain at least one multi-row destination"
        for rows in fanned:
            departures = [r.get("departure_at") for r in rows]
            assert len(set(departures)) > 1, (
                "rows of one route must be distinguishable by departure"
            )

    def test_a_row_with_no_stated_departure_reads_as_unknown(self):
        # Absent must not become midnight: a row wrongly placed at 00:00 would be
        # deleted or kept for the wrong reason.
        rows = parse_trade_routes(
            """<script>window.Travian.React.TradeRoutes.render({viewData:
            {"ownPlayer":{"village":{"marketplace":{"tradeRoutes":[
            {"from":{"id":1},"to":{"id":2,"mapId":50000},
             "routes":[{"id":9,"enabled":true,"carriedResources":{}}]}]}}}}})</script>""",
            map_span=401,
        )
        assert rows and rows[0]["departure_at"] is None
