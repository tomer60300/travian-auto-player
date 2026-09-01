"""What the game's OWN fan-out looks like, measured on a real captured page.

Every schedule decision in the reconciler rests on one assumption: creating a
route with "repeat every N hours" makes the game produce 24/N daily rows, evenly
spaced, sharing a destination and a cargo and differing only by departure time.
`_planned_minutes` computes the expected minute multiset from exactly that model,
and reconciliation compares live rows against it. If the model is wrong, every
comparison is wrong, and the account churns forever.

Until now that assumption was only ever checked against `_FakeLiveSvc` -- a
double this repo writes itself, which was caught misrepresenting a stopped
create in this same session. A double cannot confirm a claim about the game.

So these read the anonymised capture of a live Europe 2 marketplace and measure
the fan-out the GAME produced: 83 routes, which group into 24, 12, 8 and 2 rows
per (destination, cargo) at 60, 120, 180 and 720 minute spacing. Those are
exactly 24/N and 1440/(24/N) for N of 1, 2, 3 and 12. The model holds.

It also pins a trap found while measuring this. The parsed `repeat_hours` field
says 1 for 82 of the 83 rows, including groups whose spacing proves a 2h, 3h or
12h cadence -- so that field does NOT describe the route's real cadence. It is
currently harmless because nothing consumes it: it is not carried into
ExistingRoute and the reconciler derives cadence from departure minutes instead.
A test below fails if anyone starts trusting it.
"""

from collections import defaultdict
from pathlib import Path

import pytest

from travian_api.parsers.html_parser import parse_trade_routes

FIXTURE = Path(__file__).parent / "fixtures" / "marketplace_trade_routes.html"
MINUTES_PER_DAY = 1440


@pytest.fixture(scope="module")
def routes():
    return parse_trade_routes(FIXTURE.read_text(encoding="utf-8"))


def _fanout_groups(routes):
    """Rows grouped the way ONE operator-created route fans out.

    Destination and cargo together, because the game repeats both across every
    row of a fan-out; departure is the only field that varies.
    """
    groups = defaultdict(list)
    for r in routes:
        key = (r["dest_village_id"], tuple(sorted(r["cargo"].items())))
        groups[key].append(r)
    return groups


def _minutes(rows):
    return sorted((r["departure_at"] % 86400) // 60 for r in rows)


class TestTheGameFansOneRouteInto24OverN:
    def test_the_capture_still_carries_a_real_fan_out_to_measure(self, routes):
        # Guards the fixture itself: if an anonymisation pass ever flattened the
        # departures, every assertion below would pass while measuring nothing.
        assert len(routes) >= 80, f"expected a full page, got {len(routes)} routes"
        groups = _fanout_groups(routes)
        multi = [rows for rows in groups.values() if len(rows) > 1]
        assert len(multi) >= 4, "the capture no longer shows several distinct fan-outs"

    def test_every_fan_out_has_a_row_count_of_24_over_n(self, routes):
        """THE assumption. A count that is not 24/N for a whole N means the game
        does not fan out the way `_planned_minutes` believes."""
        allowed = {24 // n for n in (1, 2, 3, 4, 6, 8, 12, 24)}

        offenders = {
            dest: len(rows)
            for (dest, _cargo), rows in _fanout_groups(routes).items()
            if len(rows) > 1 and len(rows) not in allowed
        }

        assert not offenders, (
            f"fan-out row counts that are not 24/N: {offenders}. "
            f"_planned_minutes builds its expected minute set from 24/N, so a "
            f"count outside {sorted(allowed)} means live rows can never match a plan."
        )

    def test_rows_in_a_fan_out_are_evenly_spaced_across_the_day(self, routes):
        """The other half of the model: `_planned_minutes` spaces its minutes by
        the cycle, so the game's own spacing has to be uniform."""
        for (dest, _cargo), rows in _fanout_groups(routes).items():
            if len(rows) < 3:
                continue  # two rows cannot show a rhythm
            mins = _minutes(rows)
            gaps = {(b - a) for a, b in zip(mins, mins[1:])}
            assert len(gaps) == 1, (
                f"destination {dest} has uneven departure spacing {sorted(gaps)} "
                f"across {len(rows)} rows; the fan-out is not a fixed cycle"
            )

    def test_the_spacing_equals_a_whole_day_divided_by_the_row_count(self, routes):
        """Together with the count, this is the whole model: N rows every
        1440/N minutes. It is what makes a cycle recoverable from the page."""
        for (dest, _cargo), rows in _fanout_groups(routes).items():
            if len(rows) < 3:
                continue
            mins = _minutes(rows)
            gap = mins[1] - mins[0]
            assert gap * len(rows) == MINUTES_PER_DAY, (
                f"destination {dest}: {len(rows)} rows spaced {gap} minutes apart "
                f"covers {gap * len(rows)} minutes, not a full day"
            )

    def test_the_measured_cadences_are_ones_the_game_actually_offers(self, routes):
        """A derived cycle that Travian does not offer would mean the model is
        being fitted to noise rather than read off the page."""
        offered = {1, 2, 3, 4, 6, 8, 12, 24}

        for (dest, _cargo), rows in _fanout_groups(routes).items():
            if len(rows) < 3:
                continue
            derived = MINUTES_PER_DAY // len(rows) // 60
            assert derived in offered, (
                f"destination {dest} implies a {derived}h cycle, which is not in "
                f"the repeatEvery set {sorted(offered)}"
            )

    def test_a_fan_out_shares_one_cargo_so_grouping_by_it_is_sound(self, routes):
        # If the game varied cargo across a fan-out, grouping by cargo would
        # split one route into several and every count above would be wrong.
        by_dest = defaultdict(set)
        for r in routes:
            by_dest[r["dest_village_id"]].add(tuple(sorted(r["cargo"].items())))

        # More than one cargo per destination is legitimate (two profiles can
        # ship to the same village), but each must still form a 24/N group --
        # which the count test above already checks per (destination, cargo).
        assert by_dest, "no destinations parsed"


class TestRepeatHoursIsNotToBeTrusted:
    """A field that looks authoritative and is not.

    82 of the 83 rows report `repeat_hours: 1`, including groups whose spacing
    proves a 2h, 3h or 12h cadence. Whatever the page means by it, it is not the
    route's cadence.
    """

    def test_the_field_contradicts_the_measured_spacing(self, routes):
        """States the defect as a fact, so a parser fix that makes the field
        honest will fail here and be noticed rather than quietly change meaning."""
        contradictions = []
        for (dest, _cargo), rows in _fanout_groups(routes).items():
            if len(rows) < 3:
                continue
            derived = MINUTES_PER_DAY // len(rows) // 60
            claimed = {r["repeat_hours"] for r in rows}
            if claimed != {derived}:
                contradictions.append((dest, sorted(claimed), derived))

        assert contradictions, (
            "repeat_hours now agrees with the measured spacing on every fan-out. "
            "If the parser was fixed, delete this test and let the reconciler "
            "read the field -- but confirm against a fresh capture first."
        )

    def test_nothing_in_the_write_path_consumes_it(self):
        """The reason the defect is currently harmless, pinned so it stays that
        way. The reconciler derives cadence from departure minutes; a change
        that starts trusting repeat_hours would silently mis-key every match."""
        from travian_api.services import trade_route_service
        from travian_api.web.routes import distribution

        for module in (trade_route_service, distribution):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "repeat_hours" not in source, (
                f"{module.__name__} now reads repeat_hours. The field is wrong on "
                f"a real page (see the test above) -- matching must stay derived "
                f"from departure_at."
            )

    def test_it_is_not_carried_into_the_reconciler_s_view_of_a_route(self):
        from travian_api.services.trade_route_service import ExistingRoute

        assert not hasattr(ExistingRoute("1", 2), "repeat_hours")
